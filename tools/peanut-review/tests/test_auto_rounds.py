"""Automatic review/edit rounds on local sessions, with agents faked out."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from peanut_review import auto_rounds, session as sess, store
from peanut_review.models import Comment, GitHubPR


AGENTS = [
    {"name": "Ada", "model": "m", "persona": "ada.md"},
    {"name": "Curator", "model": "m", "role": "curator"},
    {"name": "Editor", "model": "m", "role": "editor"},
]


def _git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    wd = tmp_path / "repo"
    wd.mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", str(wd))
    _git(wd, "config", "user.email", "dev@example.com")
    _git(wd, "config", "user.name", "Dev")
    (wd / "src").mkdir()
    (wd / "docs").mkdir()
    (wd / "src" / "a.py").write_text("x = 1\n")
    (wd / "docs" / "big.txt").write_text("not needed\n")
    _git(wd, "add", ".")
    _git(wd, "commit", "-q", "-m", "base")
    (wd / "src" / "a.py").write_text("x = 2\n")
    _git(wd, "commit", "-q", "-am", "topic")
    return wd


@pytest.fixture
def session_dir(tmp_path: Path, repo: Path) -> Path:
    sd = tmp_path / "sessions" / "local-1"
    sess.create_session(workspace=str(repo), base_ref="main~1", topic_ref="main",
                        agents=[dict(a) for a in AGENTS], session_dir=str(sd), session_id="local-1")
    return sd


class FakeAgents:
    """Reviewers post a finding until the Editor has fixed it; the Editor edits and resolves."""

    def __init__(self, session_dir: Path, *, editor_edits: bool = True):
        self.session_dir = session_dir
        self.editor_edits = editor_edits
        self.calls: list[tuple[str, str | None]] = []

    def rerun(self, sdir, *, agent_names, workspace_override=None, **_):
        self.calls.append((agent_names[0], workspace_override))
        if workspace_override:
            worktree = Path(workspace_override)
            assert (worktree / ".cursor" / "cli.json").is_file()
            if self.editor_edits:
                (worktree / "src" / "a.py").write_text("x = 3\n")
            (sdir / "tmp" / "Editor" / "round-summary.md").write_text("c1: fixed - use 3\n")
            for c in auto_rounds.open_findings(sdir):
                store.resolve_comment(sdir, c.id, "Editor")
            return []
        current = (Path(sess.load_session(sdir).workspace) / "src" / "a.py").read_text()
        if current != "x = 3\n":
            store.append_comment(sdir, Comment(author="Ada", file="src/a.py", line=1, body="use 3"))
        return []

    def curate(self, sdir, **_):
        self.calls.append(("Curator", None))
        return []


@pytest.fixture
def fake(monkeypatch, session_dir: Path) -> FakeAgents:
    agents = FakeAgents(session_dir)
    monkeypatch.setattr(auto_rounds.launch, "rerun_agents", agents.rerun)
    monkeypatch.setattr(auto_rounds.launch, "launch_curator", agents.curate)
    monkeypatch.setattr(auto_rounds, "_wait", lambda *a, **k: [])
    return agents


def test_rounds_commit_fast_forward_and_converge(session_dir: Path, repo: Path, fake: FakeAgents):
    head0 = _git(repo, "rev-parse", "HEAD")
    state = auto_rounds.run(session_dir, 3)

    assert state["status"] == "converged"
    assert [r["status"] for r in state["rounds"]] == ["committed", "converged"]
    first = state["rounds"][0]
    assert first["head_before"] == head0 and first["open_findings"] == 1
    head1 = first["head_after"]
    assert _git(repo, "rev-parse", "HEAD") == head1
    assert (repo / "src" / "a.py").read_text() == "x = 3\n"
    message = _git(repo, "log", "-1", "--format=%an%n%B")
    assert message.startswith("Dev\nAddress peanut-review round 1 findings")
    assert "c1: fixed - use 3" in message and "Peanut-Review-Session: local-1" in message

    loaded = sess.load_session(session_dir)
    assert loaded.current_head == head1
    assert auto_rounds.round_range(session_dir, 1) == (head0, head1)
    assert auto_rounds.round_range(session_dir, 2) is None
    assert state["editor_worktree"] is None
    assert not (session_dir / auto_rounds.EDITOR_WORKTREE).exists()
    assert [c[0] for c in fake.calls] == ["Ada", "Curator", "Editor", "Ada", "Curator"]


def test_editor_runs_in_a_sparse_copy_of_a_sparse_checkout(session_dir: Path, repo: Path, monkeypatch):
    _git(repo, "sparse-checkout", "init", "--cone")
    _git(repo, "sparse-checkout", "set", "src")
    seen = {}

    def rerun(sdir, *, agent_names, workspace_override=None, **_):
        if workspace_override:
            seen["files"] = sorted(p.name for p in Path(workspace_override).iterdir())
        else:
            store.append_comment(sdir, Comment(author="Ada", file="src/a.py", line=1, body="x"))
        return []

    monkeypatch.setattr(auto_rounds.launch, "rerun_agents", rerun)
    monkeypatch.setattr(auto_rounds.launch, "launch_curator", lambda *a, **k: [])
    monkeypatch.setattr(auto_rounds, "_wait", lambda *a, **k: [])
    state = auto_rounds.run(session_dir, 1)
    assert "docs" not in seen["files"] and "src" in seen["files"]
    assert state["status"] == "blocked" and state["rounds"][0]["status"] == "no-changes"
    assert "1 finding(s) open for you" in state["reason"]
    assert _git(repo, "sparse-checkout", "list") == "src"


def test_non_cone_sparse_patterns_are_copied(session_dir: Path, repo: Path):
    _git(repo, "sparse-checkout", "init", "--no-cone")
    info = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "info/sparse-checkout"))
    info.write_text("/src/\n")
    _git(repo, "sparse-checkout", "reapply")
    worktree = auto_rounds._prepare_editor_worktree(session_dir, str(repo), _git(repo, "rev-parse", "HEAD"))
    try:
        assert sorted(p.name for p in worktree.iterdir() if p.name != ".git") == [".cursor", "src"]
        cone = subprocess.run(["git", "-C", str(worktree), "config", "--bool", "core.sparseCheckoutCone"],
                              capture_output=True, text=True).stdout.strip()
        assert cone != "true"
    finally:
        auto_rounds._remove_editor_worktree(session_dir, str(repo))


def test_dirty_developer_checkout_blocks_the_fast_forward(session_dir: Path, repo: Path, fake: FakeAgents,
                                                          monkeypatch):
    original = fake.rerun

    def rerun(sdir, **kwargs):
        if kwargs.get("workspace_override"):
            (repo / "src" / "a.py").write_text("x = 99\n")
        return original(sdir, **kwargs)

    monkeypatch.setattr(auto_rounds.launch, "rerun_agents", rerun)
    head0 = _git(repo, "rev-parse", "HEAD")
    state = auto_rounds.run(session_dir, 3)
    assert state["status"] == "blocked"
    assert "uncommitted changes" in state["reason"]
    assert state["rounds"][0]["status"] == "not-applied"
    assert _git(repo, "rev-parse", "HEAD") == head0
    assert (repo / "src" / "a.py").read_text() == "x = 99\n"
    kept = state["rounds"][0]["head_after"]
    assert f"git merge --ff-only {kept}" in state["reason"]
    refs = _git(repo, "for-each-ref", "--format=%(objectname)", "refs/peanut-review/")
    assert kept in refs.split()


def test_editor_failure_keeps_its_checkout(session_dir: Path, fake: FakeAgents, monkeypatch):
    calls = iter([[], [], ["Editor"]])
    monkeypatch.setattr(auto_rounds, "_wait", lambda *a, **k: next(calls))
    with pytest.raises(RuntimeError, match="Editor failed"):
        auto_rounds.run(session_dir, 2)
    state = auto_rounds.read_loop(session_dir)
    assert state["status"] == "failed"
    assert Path(state["editor_worktree"]).is_dir()


def test_stop_request_ends_the_loop(session_dir: Path, fake: FakeAgents, monkeypatch):
    def wait(sdir, names, timeout, poll):
        (sdir / auto_rounds.STOP_FILE).write_text("now")
        raise auto_rounds.Stopped()

    monkeypatch.setattr(auto_rounds, "_wait", wait)
    state = auto_rounds.run(session_dir, 2)
    assert state["status"] == "stopped"
    assert not (session_dir / auto_rounds.STOP_FILE).exists()


def test_refuses_github_sessions_and_missing_editor(tmp_path: Path, repo: Path, session_dir: Path):
    sd = tmp_path / "sessions" / "gh"
    sess.create_session(workspace=str(repo), base_ref="main~1", topic_ref="main",
                        agents=[dict(a) for a in AGENTS], session_dir=str(sd),
                        github=GitHubPR(repo="acme/foo", number=1))
    with pytest.raises(ValueError, match="local review sessions only"):
        auto_rounds.run(sd, 1)

    data = json.loads((session_dir / "session.json").read_text())
    data["agents"] = [a for a in data["agents"] if a.get("role") != "editor"]
    (session_dir / "session.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Editor"):
        auto_rounds.run(session_dir, 1)
    with pytest.raises(ValueError, match="between 1 and"):
        auto_rounds.run(session_dir, 0)


def test_loop_status_marks_a_dead_driver_interrupted(session_dir: Path):
    auto_rounds._write_loop(session_dir, {"status": "running", "pid": 2 ** 22 + 12345, "rounds": []})
    status = auto_rounds.loop_status(session_dir)
    assert status["status"] == "interrupted" and status["running"] is False
    assert auto_rounds.request_stop(session_dir) is False


def test_create_local_session_requires_reviewer_and_editor(tmp_path: Path, repo: Path):
    cfg = {"reviewRoot": str(tmp_path / "root"), "personasDir": None}
    (tmp_path / "root").mkdir()
    with pytest.raises(ValueError, match="Editor"):
        auto_rounds.create_local_session(cfg, str(repo), "main~1", AGENTS[:2])
    with pytest.raises(ValueError, match="nothing to review"):
        auto_rounds.create_local_session(cfg, str(repo), "HEAD", AGENTS)
    sd = auto_rounds.create_local_session(cfg, str(repo / "src"), "main~1", AGENTS)
    loaded = sess.load_session(sd)
    assert loaded.id == "repo-local-main"
    assert loaded.github is None and loaded.workspace == str(repo)
    assert (repo / ".cursor" / "cli.json").is_file()
    again = auto_rounds.create_local_session(cfg, str(repo), "main~1", AGENTS)
    assert again != sd and again.name.startswith("repo-local-main-")


def _serve(roots, **kwargs):
    import threading
    from peanut_review.web import app as web_app
    srv = web_app.make_server("127.0.0.1", 0, web_app.SessionRegistry(roots), **kwargs)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", srv.RequestHandlerClass.queue_token


def _request(url: str, body: dict | None = None, token: str | None = None) -> tuple[int, str]:
    import urllib.error
    import urllib.request
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Peanut-Queue-Token"] = token
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_session_page_shows_rounds_and_each_round_diff(session_dir: Path, repo: Path, fake: FakeAgents):
    auto_rounds.run(session_dir, 3)
    srv, base, _token = _serve([session_dir.parent])
    try:
        code, page = _request(f"{base}/local-1/")
        assert code == 200
        assert 'href="/local-1?round=1"' in page and "Automatic rounds" in page
        assert "converged" in page

        code, page = _request(f"{base}/local-1/?round=1")
        assert code == 200
        assert "Showing the Editor commit of round 1 only" in page
        assert 'data-file="src/a.py"' in page
        assert "window.PR_ROUND_VIEW = 1" in page

        assert _request(f"{base}/local-1/?round=2")[0] == 404
        assert _request(f"{base}/local-1/?round=abc")[0] == 404

        code, raw = _request(f"{base}/local-1/api/session")
        assert json.loads(raw)["loop"]["status"] == "converged"
    finally:
        srv.shutdown()


def test_loop_endpoints_need_the_token(session_dir: Path, monkeypatch):
    started = []
    monkeypatch.setattr(auto_rounds, "spawn_driver", lambda sd, n: started.append(n) or 4242)
    srv, base, token = _serve([session_dir.parent])
    try:
        assert _request(f"{base}/local-1/api/loop/start", {"rounds": 2})[0] == 403
        assert _request(f"{base}/local-1/api/loop/start", {"rounds": 0}, token)[0] == 409
        assert _request(f"{base}/local-1/api/loop/start", {"rounds": "2"}, token)[0] == 409
        assert _request(f"{base}/local-1/api/loop/start", {"rounds": 2}, token)[0] == 202
        assert started == [2]
        assert _request(f"{base}/local-1/api/loop/stop", {}, token)[0] == 409
    finally:
        srv.shutdown()


def test_local_start_creates_a_session_and_starts_rounds(tmp_path: Path, repo: Path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    personas = tmp_path / "personas"
    personas.mkdir()
    for name in ("ada.md", "bob.md"):
        (personas / name).write_text("persona\n")
    config = tmp_path / ".peanut-review.json"
    config.write_text(json.dumps({
        "reviewRoot": str(root), "workspaceRoot": str(tmp_path), "repoRelative": "repo",
        "personasDir": str(personas),
        "agents": [dict(a) for a in AGENTS] + [{"name": "Bob", "model": "m", "persona": "bob.md"}],
    }))
    started = []
    monkeypatch.setattr(auto_rounds, "spawn_driver", lambda sd, n: started.append((sd, n)) or 1)
    srv, base, token = _serve([root], review_config=str(config))
    body = {"workspace": str(repo), "base": "main~1", "config": str(config.resolve()), "rounds": 2,
            "agents": [{"name": "Bob", "enabled": False}, {"name": "Ada", "model": "m2"}]}
    try:
        assert _request(f"{base}/api/local/start", body, None)[0] == 403
        assert _request(f"{base}/api/local/start", {**body, "config": "/etc/passwd"}, token)[0] == 400
        assert _request(f"{base}/api/local/start", {**body, "rounds": 11}, token)[0] == 400
        code, raw = _request(f"{base}/api/local/start", body, token)
        assert code == 201, raw
        session_id = json.loads(raw)["session_id"]
        loaded = sess.load_session(root / session_id)
        by_name = {a.name: a for a in loaded.agents}
        assert by_name["Bob"].enabled is False and by_name["Ada"].model == "m2"
        assert started == [(root / session_id, 2)]
        code, page = _request(f"{base}/local")
        assert f'href="/{session_id}"' in page
    finally:
        srv.shutdown()


def test_editor_launch_uses_its_checkout_and_prompt(session_dir: Path, repo: Path, tmp_path: Path):
    from peanut_review import launch
    worktree = auto_rounds._prepare_editor_worktree(session_dir, str(repo), _git(repo, "rev-parse", "HEAD"))
    try:
        [result] = launch.launch_agents(session_dir, dry_run=True, agent_names=["Editor"],
                                        workspace_override=str(worktree))
        cmd = result["cmd"]
        assert cmd[cmd.index("--workspace") + 1] == str(worktree)
        prompt = (session_dir / "prompts" / "Editor.md").read_text()
        assert "non-interactive code editor agent" in prompt
        assert f"Workspace: `{worktree}`" in prompt
        assert result["supervisor_cmd"][result["supervisor_cmd"].index("--cwd") + 1] == str(worktree)
        allow = json.loads((worktree / ".cursor" / "cli.json").read_text())["permissions"]["allow"]
        shell = [entry for entry in allow if entry.startswith("Shell(")]
        prefix = f"Shell({auto_rounds.PR_BIN} --session {session_dir.resolve()} "
        assert shell and all(entry.startswith(prefix) for entry in shell)
        commands = [entry[len(prefix):-1] for entry in shell]
        assert sorted(commands) == sorted(auto_rounds.EDITOR_COMMANDS)
    finally:
        assert auto_rounds._remove_editor_worktree(session_dir, str(repo))


def test_stop_signal_and_stop_during_startup_are_kept(session_dir: Path, fake: FakeAgents, monkeypatch):
    handler = auto_rounds.stop_on_signal(session_dir)
    handler(15, None)
    assert (session_dir / auto_rounds.STOP_FILE).exists()

    auto_rounds._write_loop(session_dir, {"status": "starting", "spawned_at": __import__("time").time(),
                                          "token": "t1", "rounds": []})
    assert auto_rounds.request_stop(session_dir) is True
    monkeypatch.setenv(auto_rounds.DRIVER_TOKEN_ENV, "t1")
    state = auto_rounds.run(session_dir, 2)
    assert state["status"] == "stopped"
    assert fake.calls == []


def test_second_driver_is_refused_while_one_is_starting(session_dir: Path, fake: FakeAgents, monkeypatch):
    auto_rounds._write_loop(session_dir, {"status": "starting", "spawned_at": __import__("time").time(),
                                          "token": "t1", "rounds": []})
    monkeypatch.delenv(auto_rounds.DRIVER_TOKEN_ENV, raising=False)
    with pytest.raises(ValueError, match="already running"):
        auto_rounds.run(session_dir, 1)
    assert "token" not in auto_rounds.loop_status(session_dir)


def test_editor_is_allowlist_only_and_cursor_only(session_dir: Path, repo: Path):
    from peanut_review import launch, validation
    worktree = auto_rounds._prepare_editor_worktree(session_dir, str(repo), _git(repo, "rev-parse", "HEAD"))
    try:
        [editor] = launch.launch_agents(session_dir, dry_run=True, agent_names=["Editor"],
                                        workspace_override=str(worktree))
        assert "--allowlist-only" in editor["cmd"]
        (repo / ".cursor").mkdir(exist_ok=True)
        (repo / ".cursor" / "cli.json").write_text(
            (auto_rounds.TEMPLATES_DIR / "cli.sample.json").read_text())
        [reviewer] = launch.launch_agents(session_dir, dry_run=True, agent_names=["Ada"])
        assert "--allowlist-only" not in reviewer["cmd"]
        allow = json.loads((worktree / ".cursor" / "cli.json").read_text())["permissions"]["allow"]
        assert f"Shell({auto_rounds.PR_BIN} --session {session_dir.resolve()} resolve **)" in allow
    finally:
        auto_rounds._remove_editor_worktree(session_dir, str(repo))
    errors: list[str] = []
    validation._validate_agent_configs(
        [{"name": "E", "model": "m", "role": "editor", "runner": "codex"}],
        personas_dir=None, ssh_target_names=set(), errors=errors,
    )
    assert any("must be 'cursor' for editor" in e for e in errors)


def test_rebuttals_get_another_review_and_history_is_kept(session_dir: Path, repo: Path, fake: FakeAgents):
    fake.editor_edits = False
    state = auto_rounds.run(session_dir, 2)
    assert [r["status"] for r in state["rounds"]] == ["rebutted", "rebutted"]
    assert state["status"] == "done" and "have not checked" in state["reason"]

    fake.editor_edits = True
    state = auto_rounds.run(session_dir, 1)
    assert [r["number"] for r in state["rounds"]] == [1, 2, 3]
    assert state["rounds"][-1]["status"] == "committed"
    assert auto_rounds.round_range(session_dir, 3)[1] == _git(repo, "rev-parse", "HEAD")


def test_checkout_moved_between_rounds_blocks(session_dir: Path, repo: Path, fake: FakeAgents, monkeypatch):
    original = fake.curate

    def curate(sdir, **kwargs):
        if not any(c[0] == "Curator" for c in fake.calls):
            _git(repo, "checkout", "-q", "-b", "elsewhere", "HEAD~1")
        return original(sdir, **kwargs)

    monkeypatch.setattr(auto_rounds.launch, "launch_curator", curate)
    state = auto_rounds.run(session_dir, 3)
    assert state["status"] == "blocked"


def test_timeout_kills_the_agents_it_waited_for(session_dir: Path, monkeypatch):
    killed = []
    monkeypatch.setattr(auto_rounds.polling, "wait_round_completion", lambda *a, **k: ([], ["Ada"]))
    monkeypatch.setattr(auto_rounds, "_kill", lambda sdir, names=None: killed.append(names))
    with pytest.raises(TimeoutError):
        auto_rounds._wait(session_dir, ["Ada"], 0, 0)
    assert killed == [["Ada"]]


def test_round_commit_skips_hooks(session_dir: Path, repo: Path, fake: FakeAgents):
    hooks = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n")
    (hooks / "pre-commit").chmod(0o755)
    state = auto_rounds.run(session_dir, 1)
    assert state["rounds"][0]["status"] == "committed"


def test_round_view_folds_use_the_round_diff(session_dir: Path, fake: FakeAgents):
    auto_rounds.run(session_dir, 3)
    srv, base, _token = _serve([session_dir.parent])
    try:
        code, raw = _request(f"{base}/local-1/api/diff/fold?file=src/a.py&start=0&end=5&round=1")
        assert code == 200, raw
        assert _request(f"{base}/local-1/api/diff/fold?file=src/a.py&start=0&end=5&round=9")[0] == 404
    finally:
        srv.shutdown()
