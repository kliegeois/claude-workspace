"""Deleting a session must protect running agents, shared checkouts and uncommitted work."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.request
import threading

import pytest

from peanut_review import session as sess, session_delete
from peanut_review.web.app import SessionRegistry, make_server


GIT = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid"]


@pytest.fixture
def now():
    # Test file mtimes are older than the quiet window, without sleeps.
    return datetime.now(timezone.utc) + timedelta(minutes=5)


@pytest.fixture
def clone(tmp_path):
    path = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "tracked.txt").write_text("base\n")
    subprocess.run([*GIT, "-C", str(path), "add", "tracked.txt"], check=True)
    subprocess.run([*GIT, "-C", str(path), "commit", "-qm", "base"], check=True)
    return path


@pytest.fixture
def worktree(tmp_path, clone):
    path = tmp_path / "worktrees" / "feature"
    subprocess.run([*GIT, "-C", str(clone), "worktree", "add", "-q", "-b", "feature", str(path)], check=True)
    return path


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "sessions"
    path.mkdir()
    return path


def make_session(root, workspace, name="review"):
    path = root / name
    sess.create_session(workspace=str(workspace), base_ref="HEAD", topic_ref="HEAD",
                        agents=[{"name": "Vera", "model": "test"}], session_dir=path)
    return path


def refs(repo):
    return subprocess.run(["git", "-C", str(repo), "for-each-ref", "--format=%(refname)",
                           "refs/peanut-review/"], capture_output=True, text=True, check=True).stdout.split()


def test_delete_session_only_keeps_worktree_and_drops_retained_refs(root, clone, worktree, now):
    review = make_session(root, worktree)
    (worktree / ".cursor").mkdir()
    (worktree / ".cursor" / "cli.json").write_text("{}")
    assert refs(clone)
    plan = session_delete.deletion_plan(review, [root], now=now)
    assert plan["deletable"] and not plan["blockers"]
    assert plan["worktree"]["removable"]
    assert plan["worktree"]["untracked"] == [".cursor/cli.json"]
    assert plan["worktree"]["notes"] == ["Branch feature is kept."]
    result = session_delete.delete_session(review, [root], now=now)
    assert not review.exists() and worktree.is_dir()
    assert result["worktree"] is None and result["refs_removed"] >= 1
    assert refs(clone) == []


def test_delete_with_worktree_requires_confirmed_untracked_files_and_keeps_branch(root, clone, worktree, now):
    review = make_session(root, worktree)
    (worktree / "build.log").write_text("log")
    with pytest.raises(session_delete.DeletionBlocked, match="untracked"):
        session_delete.delete_session(review, [root], remove_worktree=True, confirmed_untracked=[], now=now)
    assert review.exists() and worktree.exists()
    result = session_delete.delete_session(review, [root], remove_worktree=True,
                                           confirmed_untracked=["build.log"], now=now)
    assert result["worktree"] == str(worktree.resolve())
    assert not review.exists() and not worktree.exists()
    branches = subprocess.run(["git", "-C", str(clone), "branch", "--list", "feature"],
                              capture_output=True, text=True, check=True).stdout
    assert "feature" in branches


def test_tracked_changes_block_worktree_removal(root, worktree, now):
    review = make_session(root, worktree)
    (worktree / "tracked.txt").write_text("edited\n")
    plan = session_delete.deletion_plan(review, [root], now=now)
    assert plan["deletable"] and not plan["worktree"]["removable"]
    assert "tracked files" in plan["worktree"]["reason"]
    with pytest.raises(session_delete.DeletionBlocked, match="worktree cannot be removed"):
        session_delete.delete_session(review, [root], remove_worktree=True, now=now)
    assert review.exists() and (worktree / "tracked.txt").read_text() == "edited\n"


def test_main_clone_and_shared_worktrees_are_not_removable(root, clone, worktree, now):
    plan = session_delete.deletion_plan(make_session(root, clone, "main"), [root], now=now)
    assert "main clone" in plan["worktree"]["reason"]
    first = sess.load_session(make_session(root, worktree, "first")).id
    plan = session_delete.deletion_plan(make_session(root, worktree, "second"), [root], now=now)
    assert plan["deletable"] and plan["worktree"]["reason"] == f"Also used by {first}."


def test_unpushed_commits_are_reported(root, clone, worktree, now):
    default = subprocess.run(["git", "-C", str(clone), "symbolic-ref", "--short", "HEAD"],
                             capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run([*GIT, "-C", str(worktree), "branch", "-q", "--set-upstream-to", default], check=True)
    subprocess.run([*GIT, "-C", str(worktree), "commit", "--allow-empty", "-qm", "local"], check=True)
    plan = session_delete.deletion_plan(make_session(root, worktree), [root], now=now)
    assert plan["worktree"]["notes"] == [f"Branch feature is kept; it has 1 commit(s) not pushed to {default}."]


def test_running_agent_blocks_deletion(root, worktree, now):
    review = make_session(root, worktree)
    meta = review / "log" / "Vera" / "meta.json"
    meta.parent.mkdir(parents=True)
    meta.write_text(json.dumps({"process_state": "running", "start": (now - timedelta(seconds=600)).isoformat(),
                                "heartbeat_at": (now - timedelta(seconds=5)).isoformat()}))
    plan = session_delete.deletion_plan(review, [root], now=now)
    assert not plan["deletable"] and "Vera is running" in plan["blockers"][0]
    assert not plan["worktree"]["removable"]
    with pytest.raises(session_delete.DeletionBlocked):
        session_delete.delete_session(review, [root], now=now)
    assert review.exists()


def test_only_sessions_inside_a_served_root_are_deleted(tmp_path, root, worktree, now):
    outside = make_session(tmp_path / "elsewhere", worktree)
    with pytest.raises(ValueError, match="served review root"):
        session_delete.delete_session(outside, [root], now=now)
    link = root / "link"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="symlinked"):
        session_delete.delete_session(link, [root], now=now)
    assert outside.exists()


def test_web_delete_requires_page_token(root, worktree):
    review = make_session(root, worktree)
    sid = sess.load_session(review).id
    old = time.time() - 600
    for path in [review, *review.rglob("*")]:
        os.utime(path, (old, old))
    registry = SessionRegistry([root])
    http = make_server("127.0.0.1", 0, registry)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{http.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/") as response:
            token = re.search(r'PR_ACTION_TOKEN = "([^"]+)"', response.read().decode()).group(1)
        with urllib.request.urlopen(f"{base}/{sid}/api/session/delete-plan") as response:
            plan = json.load(response)
        assert plan["deletable"] and "common_dir" not in plan["worktree"]

        def post(headers):
            request = urllib.request.Request(
                f"{base}/{sid}/api/session/delete", data=json.dumps({"remove_worktree": False}).encode(),
                headers={"Content-Type": "application/json", **headers}, method="POST")
            try:
                with urllib.request.urlopen(request) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)

        assert post({})[0] == 403
        assert post({"X-Peanut-Queue-Token": token, "Origin": "https://attacker.invalid"})[0] == 403
        assert review.exists()
        status, result = post({"X-Peanut-Queue-Token": token})
        assert status == 200 and result["worktree"] is None
        assert not review.exists() and worktree.exists()
        assert registry.get(sid) is None
    finally:
        http.shutdown()
        http.server_close()
