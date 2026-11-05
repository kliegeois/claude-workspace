"""Shared runtime records must protect active work without network or PID lookups."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import subprocess

import pytest

from peanut_review import cleanup, session as sess, runtime
from peanut_review.cli import main
from peanut_review.models import AgentConfig


@pytest.fixture
def now():
    # Test file mtimes are older than the quiet window, without sleeps.
    return datetime.now(timezone.utc) + timedelta(minutes=5)


@pytest.fixture
def agent():
    return AgentConfig(name="Vera", model="test")


def _time(now, seconds):
    return (now - timedelta(seconds=seconds)).isoformat()


def _finished(now):
    return {"process_state": "exited", "start": _time(now, 1200),
            "end": _time(now, 600), "heartbeat_at": _time(now, 600), "exit_code": 0}


@pytest.fixture
def checkout(tmp_path):
    path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "--allow-empty",
                    "-qm", "base"], check=True)
    return path


@pytest.fixture
def review(tmp_path, checkout):
    path = tmp_path / "sessions" / "first"
    sess.create_session(workspace=str(checkout), base_ref="HEAD", topic_ref="HEAD",
                        agents=[{"name": "Vera", "model": "test"}], session_dir=path)
    return path


def _meta(review, fields):
    path = review / "log" / "Vera" / "meta.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields))
    return path


def _files(path):
    return {str(p.relative_to(path)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in path.rglob("*") if p.is_file()}


def _inspect(review, now, **kwargs):
    return cleanup.inspect_cleanup(review, [review.parent], now=now, **kwargs)


def test_terminal_metadata_ignores_ui_status_and_foreign_pid(agent, now):
    agent.status = "running"
    agent.pid = 99999999
    assert cleanup.inspect_agent(agent, _finished(now), None, now)["state"] == "idle"


@pytest.mark.parametrize("age,expected", [(10, "busy"), (1000, "unknown")])
def test_unfinished_heartbeat_never_means_idle(agent, now, age, expected):
    meta = {"process_state": "running", "start": _time(now, 1500), "heartbeat_at": _time(now, age)}
    assert cleanup.inspect_agent(agent, meta, None, now)["state"] == expected


def test_round_done_and_ui_done_do_not_establish_exit(review, now):
    sess.update_agent_status(review, "Vera", "done", pid=99999999)
    (review / "signals" / "Vera.round-done").touch()
    assert _inspect(review, now)["state"] == "unknown"


@pytest.mark.parametrize("change", [
    {"exit_code": None}, {"exit_code": True}, {"end": None},
    {"start": "not-a-time"}, {"end": "2020-01-01T00:00:00+00:00"},
    {"heartbeat_at": "2099-01-01T00:00:00+00:00"},
    {"end": "2020-01-01T00:00:00"},
])
def test_inconsistent_final_records_are_unknown(agent, now, change):
    assert cleanup.inspect_agent(agent, {**_finished(now), **change}, None, now)["state"] == "unknown"


def test_new_launch_invalidates_previous_completion(agent, now):
    run = {"id": "new", "started_at": _time(now, 10)}
    assert cleanup.inspect_agent(agent, _finished(now), run, now)["state"] == "busy"
    run["started_at"] = _time(now, 300)
    assert cleanup.inspect_agent(agent, _finished(now), run, now)["state"] == "unknown"


def test_completed_launch_must_match_terminal_times(agent, now):
    run = {"id": "run", "started_at": _time(now, 1201), "finished_at": _time(now, 599)}
    assert cleanup.inspect_agent(agent, _finished(now), run, now)["state"] == "idle"
    run["started_at"] = _time(now, 1000)
    assert cleanup.inspect_agent(agent, _finished(now), run, now)["state"] == "unknown"


def test_completed_ssh_cleanup_does_not_need_remote_access(agent, now):
    agent.ssh_target = "review-host"
    meta = {**_finished(now), "transport": "ssh", "remote_process_state": "stopped", "ssh_cleanup_required": False}
    assert cleanup.inspect_agent(agent, meta, None, now)["state"] == "idle"
    meta["ssh_cleanup_required"] = True
    assert cleanup.inspect_agent(agent, meta, None, now)["state"] == "unknown"


def test_just_finished_supervisor_is_protected_by_quiet_period(agent, now):
    meta = {**_finished(now), "end": _time(now, 20), "heartbeat_at": _time(now, 20)}
    assert cleanup.inspect_agent(agent, meta, None, now)["state"] == "busy"


def test_other_session_on_same_checkout_protects_candidate(review, checkout, now):
    other = review.parent / "second"
    sess.create_session(workspace=str(checkout), base_ref="HEAD", topic_ref="HEAD",
                        agents=[{"name": "Vera", "model": "test"}], session_dir=other)
    _meta(other, {"process_state": "running", "start": _time(now, 50), "heartbeat_at": _time(now, 5)})
    result = _inspect(review, now)
    assert result["state"] == "busy"
    assert {x["path"] for x in result["sessions"]} == {str(review), str(other)}


def test_recent_preparation_protects_candidate(review, now):
    _meta(review, _finished(now))
    prompt = review / "prompts" / "Vera.md"
    prompt.parent.mkdir(exist_ok=True)
    prompt.write_text("new review prompt")
    timestamp = now.timestamp() - 20
    os.utime(prompt, (timestamp, timestamp))
    result = _inspect(review, now)
    assert result["state"] == "busy"
    assert result["recent_activity"] == [str(review)]


def test_active_queue_job_is_busy_without_mutating_state(review, now):
    queue = review.parent / ".queue" / "state.json"
    queue.parent.mkdir()
    queue.write_text(json.dumps({"items": {"x": {
        "session_id": sess.load_session(review).id, "job": {"status": "preparing"},
    }}}))
    before = _files(review.parent)
    assert _inspect(review, now)["state"] == "busy"
    assert _files(review.parent) == before


def test_poll_updates_and_unrelated_reviews_do_not_protect_candidate(review, checkout, tmp_path, now, monkeypatch):
    other_checkout = tmp_path / "other"
    subprocess.run(["git", "-C", str(checkout), "worktree", "add", "--detach", str(other_checkout)], check=True)
    other = review.parent / "second"
    sess.create_session(workspace=str(other_checkout), base_ref="HEAD", topic_ref="HEAD",
                        agents=[{"name": "Vera", "model": "test"}], session_dir=other)
    _meta(other, {"process_state": "running", "heartbeat_at": _time(now, 2)})
    queue = review.parent / ".queue" / "state.json"
    queue.parent.mkdir()
    queue.write_text(json.dumps({"items": {}, "checked_at": "before"}))
    original = cleanup._queue_jobs

    def poll(root, ids):
        queue.write_text(json.dumps({"items": {}, "checked_at": "after"}))
        return original(root, ids)

    monkeypatch.setattr(cleanup, "_queue_jobs", poll)
    assert _inspect(review, now)["state"] == "idle"


def test_metadata_change_invalidates_observation(review, now, monkeypatch):
    meta = _meta(review, _finished(now))
    original = cleanup._queue_jobs

    def start(root, ids):
        meta.write_text(json.dumps({"process_state": "running"}))
        return original(root, ids)

    monkeypatch.setattr(cleanup, "_queue_jobs", start)
    result = _inspect(review, now)
    assert result["state"] == "unknown"
    assert any("Metadata changed" in error for error in result["errors"])


def test_new_shared_session_invalidates_observation(review, checkout, now, monkeypatch):
    original = cleanup._queue_jobs

    def create(root, ids):
        if not (review.parent / "new").exists():
            sess.create_session(workspace=str(checkout), base_ref="HEAD", topic_ref="HEAD",
                                agents=[], session_dir=review.parent / "new")
        return original(root, ids)

    monkeypatch.setattr(cleanup, "_queue_jobs", create)
    result = _inspect(review, now)
    assert result["state"] == "unknown"
    assert any("sharing the worktree changed" in e for e in result["errors"])


def test_corrupt_metadata_is_unknown(review, now):
    _meta(review, {}).write_text("broken json")
    assert _inspect(review, now)["state"] == "unknown"


def test_removed_agent_cannot_hide_unfinished_runtime(review, now):
    path = review / "log" / "PreviousReviewer" / "meta.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"process_state": "running", "heartbeat_at": _time(now, 10)}))
    result = _inspect(review, now)
    assert result["state"] == "busy"
    orphan = next(a for a in result["agents"] if a["name"] == "PreviousReviewer")
    assert orphan["configured"] is False


def test_removed_agent_launch_record_remains_protected(review, now):
    (review / "review-completion.json").write_text(json.dumps({
        "version": 1, "runs": {"PreviousReviewer": {"id": "run", "started_at": _time(now, 500)}}}))
    assert _inspect(review, now)["state"] == "unknown"


def test_registered_checkout_without_session_can_be_observed(review, checkout, tmp_path, now):
    other = tmp_path / "other"
    subprocess.run(["git", "-C", str(checkout), "worktree", "add", "--detach", str(other)], check=True)
    result = _inspect(review, now, worktree=str(other))
    assert result["sessions"] == []
    assert result["state"] == "idle"
    with pytest.raises(ValueError, match="not registered"):
        _inspect(review, now, worktree=str(tmp_path))


def test_local_cli_reads_shared_records_without_network_or_pid_checks(review, now, monkeypatch, capsys):
    _meta(review, _finished(now))
    old = datetime.now(timezone.utc).timestamp() - 1000
    for path in review.rglob("*"):
        if path.is_file():
            os.utime(path, (old, old))
    before = _files(review.parent)

    def forbidden(*args, **kwargs):
        pytest.fail("cleanup-status must only read shared files")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(runtime, "is_process_live", forbidden)
    monkeypatch.setattr(sess, "refresh_agent_statuses", forbidden)
    monkeypatch.setattr(sess, "save_session", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    assert main(["--session", str(review), "cleanup-status", "--root", str(review.parent)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "idle"
    assert result["evidence"] == "shared-session-metadata"
    assert _files(review.parent) == before
