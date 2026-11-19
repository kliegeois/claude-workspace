"""Automatic review/edit rounds for local review sessions.

Each round runs the enabled reviewers, then the curator. When unresolved
findings remain, the Editor agent addresses them in a detached checkout
under the session directory; the driver commits its edits as one commit,
fast-forwards the developer's checkout to it, and migrates the session.
Rounds stop when the curator leaves no unresolved findings, when the Editor
changes nothing, or after the requested number of rounds. Nothing is pushed.

State lives in `<session>/loop.json`; each round records `head_before` and
`head_after` so the web UI can show every round's commit as its own diff.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import curator, launch, polling, store
from .models import CommentCategory
from .session import (
    _session_lock,
    create_session,
    load_session,
    repo_path,
    resolve_git_ref,
    retain_review_git,
    retarget_review_head,
    review_git_namespace,
    save_session,
)


LOOP_FILE = "loop.json"
STOP_FILE = "loop.stop"
EDITOR_WORKTREE = "editor-worktree"
LOG_NAME = "auto-rounds.log"
MAX_ROUNDS = 10
WAIT_SLACK_SECONDS = 300
ACTIVE_STATES = {"starting", "running"}
STARTING_GRACE_SECONDS = 120
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
SUMMARY_LIMIT = 4000
DRIVER_TOKEN_ENV = "PEANUT_AUTO_ROUNDS_TOKEN"
PR_BIN = Path(__file__).resolve().parent.parent / "bin" / "peanut-review"
EDITOR_COMMANDS = ("status", "comments **", "add-comment **", "resolve **", "note **", "signal round-done")
# The driver's own git calls in the Editor's checkout must not run hooks or
# helpers the Editor could have configured.
SAFE_GIT = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")


class Stopped(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git(cwd: str | Path, *args: str, check: bool = True, timeout: int = 120,
         env: dict[str, str] | None = None, stdin: str | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True,
        timeout=timeout, env=env, input=stdin,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def read_loop(session_dir: str | Path) -> dict | None:
    try:
        data = json.loads((Path(session_dir) / LOOP_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_loop(session_dir: str | Path, data: dict) -> None:
    path = Path(session_dir) / LOOP_FILE
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{LOOP_FILE}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_live(data: dict) -> bool:
    if data.get("status") == "starting":
        return time.time() - float(data.get("spawned_at") or 0) < STARTING_GRACE_SECONDS
    return data.get("status") == "running" and _pid_alive(data.get("pid"))


def loop_status(session_dir: str | Path) -> dict | None:
    """Return loop.json with `running`; a dead driver shows as interrupted."""
    data = read_loop(session_dir)
    if data is None:
        return None
    running = _is_live(data)
    if data.get("status") in ACTIVE_STATES and not running:
        data = {**data, "status": "interrupted",
                "reason": "the round driver exited unexpectedly; see log/auto-rounds.log"}
    data = {key: value for key, value in data.items() if key != "token"}
    return {**data, "running": running,
            "stop_requested": (Path(session_dir) / STOP_FILE).exists()}


def round_range(session_dir: str | Path, number: int) -> tuple[str, str] | None:
    """Return (head_before, head_after) for a committed round."""
    for record in (read_loop(session_dir) or {}).get("rounds", []):
        if record.get("number") == number and record.get("head_after"):
            return record["head_before"], record["head_after"]
    return None


def request_stop(session_dir: str | Path) -> bool:
    status = loop_status(session_dir)
    if not status or not status["running"]:
        return False
    (Path(session_dir) / STOP_FILE).write_text(_now() + "\n")
    return True


def open_findings(session_dir: str | Path) -> list:
    """Unresolved, undeleted top-level findings; curator approvals do not count."""
    return [
        c for c in store.read_all_comments(session_dir)
        if not c.deleted and not c.resolved and c.reply_to is None
        and c.category != CommentCategory.APPROVE.value
    ]


def _check_runnable(session) -> tuple[list, object]:
    if session.github is not None:
        raise ValueError("automatic rounds run on local review sessions only")
    reviewers = curator.reviewers(session.agents)
    if not reviewers:
        raise ValueError("no enabled reviewer agents")
    if not curator.curators(session.agents):
        raise ValueError("no enabled curator agent")
    editors = curator.editors(session.agents)
    if not editors:
        raise ValueError("no enabled Editor agent; add one in Settings or the session's agent settings")
    if editors[0].ssh_target or editors[0].runner != "cursor":
        raise ValueError("the Editor must use the local cursor runner, which enforces its allow list")
    return reviewers, editors[0]


def _kill(session_dir: Path, names: list[str] | None = None) -> None:
    from . import agent_control
    try:
        agent_control.kill_agents(session_dir, agent_names=names)
    except (OSError, ValueError, RuntimeError):
        pass


def _wait(session_dir: Path, names: list[str], timeout: int, poll: float) -> list[str]:
    deadline = time.monotonic() + timeout
    while True:
        failed, unfinished = polling.wait_round_completion(
            session_dir, names, timeout=min(10, timeout), poll_interval=poll,
        )
        if not unfinished:
            return failed
        if (session_dir / STOP_FILE).exists():
            _kill(session_dir, unfinished)
            raise Stopped()
        if time.monotonic() >= deadline:
            _kill(session_dir, unfinished)
            raise TimeoutError(f"timed out waiting for {', '.join(unfinished)}")


def _check_stop(session_dir: Path) -> None:
    if (session_dir / STOP_FILE).exists():
        raise Stopped()


def _status_without_cursor(worktree: Path) -> str:
    return _git(worktree, *SAFE_GIT, "status", "--porcelain", "--", ".", ":(exclude).cursor").stdout.strip()


def _prepare_editor_worktree(session_dir: Path, repo: str, head: str) -> Path:
    """Detached checkout at `head` that copies the developer's sparse cone."""
    worktree = session_dir / EDITOR_WORKTREE
    if (worktree / ".git").exists():
        if _status_without_cursor(worktree):
            raise RuntimeError(f"Editor checkout has uncommitted changes: {worktree}")
        _git(worktree, *SAFE_GIT, "checkout", "--quiet", "--detach", head)
    else:
        _git(repo, "worktree", "add", "--quiet", "--detach", "--no-checkout", str(worktree), head)
        sparse = _git(repo, "config", "--bool", "core.sparseCheckout", check=False).stdout.strip()
        if sparse == "true":
            # Copy the pattern file itself: `sparse-checkout set` flag syntax
            # and its default mode differ across git versions.
            cone = _git(repo, "config", "--bool", "core.sparseCheckoutCone", check=False).stdout.strip()
            source = _git(repo, "rev-parse", "--path-format=absolute",
                          "--git-path", "info/sparse-checkout").stdout.strip()
            _git(worktree, "sparse-checkout", "init", "--cone" if cone == "true" else "--no-cone")
            target = _git(worktree, "rev-parse", "--path-format=absolute",
                          "--git-path", "info/sparse-checkout").stdout.strip()
            shutil.copyfile(source, target)
        _git(worktree, "checkout", "--quiet", "--detach", head)
    if _git(worktree, "ls-files", "--", ".cursor/cli.json").stdout.strip():
        raise RuntimeError("the repository tracks .cursor/cli.json; the Editor needs its own permissions file")
    (worktree / ".cursor").mkdir(exist_ok=True)
    permissions = json.loads((TEMPLATES_DIR / "editor-cli.json").read_text())
    prefix = f"{shlex.quote(str(PR_BIN))} --session {shlex.quote(str(Path(session_dir).resolve()))}"
    permissions["permissions"]["allow"] += [f"Shell({prefix} {command})" for command in EDITOR_COMMANDS]
    (worktree / ".cursor" / "cli.json").write_text(json.dumps(permissions, indent=2) + "\n")
    return worktree


def _write_change_patch(session, scratch: Path, head: str) -> None:
    patch = _git(repo_path(session), "diff", "--no-ext-diff", "--no-textconv",
                 f"{session.base_ref}...{head}", timeout=300).stdout
    (scratch / "change.patch").write_text(patch)


def _remove_editor_worktree(session_dir: Path, repo: str) -> bool:
    worktree = session_dir / EDITOR_WORKTREE
    if not (worktree / ".git").exists():
        return True
    if _status_without_cursor(worktree):
        return False
    try:
        (worktree / ".cursor" / "cli.json").unlink()
        (worktree / ".cursor").rmdir()
    except OSError:
        pass
    return _git(repo, "worktree", "remove", str(worktree), check=False).returncode == 0


def _commit_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        env.pop(key, None)
    return env


def _commit_round(session_dir: Path, session, worktree: Path, editor_name: str, number: int) -> str | None:
    _git(worktree, *SAFE_GIT, "add", "--all", "--", ".", ":(exclude).cursor")
    if _git(worktree, *SAFE_GIT, "diff", "--cached", "--quiet", "--no-ext-diff",
            check=False).returncode == 0:
        return None
    summary_path = session_dir / "tmp" / editor_name / "round-summary.md"
    try:
        summary = summary_path.read_text().strip()[:SUMMARY_LIMIT]
        summary_path.unlink()
    except OSError:
        summary = ""
    message = f"Address peanut-review round {number} findings\n\n"
    if summary:
        message += summary + "\n\n"
    message += f"Peanut-Review-Session: {session.id}\nPeanut-Review-Round: {number}\n"
    _git(worktree, *SAFE_GIT, "commit", "--quiet", "--no-verify", "--file", "-",
         stdin=message, env=_commit_env())
    head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    _git(worktree, "update-ref", f"refs/peanut-review/{review_git_namespace(session)}/{head}", head)
    return head


def _fast_forward(repo: str, head_before: str, head_after: str) -> str | None:
    """Move the developer's checkout to the round commit; return why not, or None."""
    live = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if live != head_before:
        return f"your checkout moved to {live[:12]} during the round"
    if _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return "your checkout has uncommitted changes"
    result = _git(repo, "merge", "--ff-only", "--quiet", head_after, check=False)
    if result.returncode != 0:
        return f"fast-forward failed: {result.stderr.strip()}"
    return None


def _migrate(session_dir: Path, new_head: str) -> None:
    with _session_lock(session_dir):
        session = load_session(session_dir)
        if session.current_head == new_head:
            return
        retarget_review_head(session, new_head)
        store.mark_stale(session_dir)
        try:
            retain_review_git(session)
        except RuntimeError:
            pass
        save_session(session_dir, session)


def run(session_dir: str | Path, max_rounds: int, *, poll: float = 5.0) -> dict:
    """Drive the rounds in the foreground; returns the final loop state."""
    sdir = Path(session_dir).resolve()
    if not 1 <= max_rounds <= MAX_ROUNDS:
        raise ValueError(f"rounds must be between 1 and {MAX_ROUNDS}")
    session = load_session(sdir)
    reviewers, editor = _check_runnable(session)
    repo = repo_path(session)
    state = {
        "status": "running", "pid": os.getpid(), "started_at": _now(), "finished_at": None,
        "max_rounds": max_rounds, "reason": "", "rounds": [],
        "editor_worktree": str(sdir / EDITOR_WORKTREE),
    }
    with _session_lock(sdir):
        existing = read_loop(sdir)
        spawned_for_us = bool(
            existing and existing.get("status") == "starting"
            and existing.get("token") and existing.get("token") == os.environ.get(DRIVER_TOKEN_ENV)
        )
        if existing and _is_live(existing) and not spawned_for_us:
            raise ValueError("automatic rounds are already running for this session")
        if not spawned_for_us:
            try:
                (sdir / STOP_FILE).unlink()
            except FileNotFoundError:
                pass
        state["rounds"] = list((existing or {}).get("rounds", []))
        _write_loop(sdir, state)
    first = max((r.get("number", 0) for r in state["rounds"]), default=0) + 1
    expected_head = None

    def update_round(record: dict, **changes) -> None:
        record.update(changes)
        _write_loop(sdir, state)

    timeout = session.timeout + WAIT_SLACK_SECONDS
    try:
        for number in range(first, first + max_rounds):
            _check_stop(sdir)
            session = load_session(sdir)
            reviewers, editor = _check_runnable(session)
            head_before = resolve_git_ref(repo, "HEAD")
            if expected_head and head_before != expected_head:
                state.update(status="blocked", reason=(
                    f"your checkout moved to {head_before[:12]} after round {number - 1}; "
                    "start new rounds when it is where you want them"))
                break
            if head_before != session.current_head:
                _migrate(sdir, head_before)
            record = {"number": number, "status": "reviewing", "head_before": head_before,
                      "head_after": None, "started_at": _now(), "failed_reviewers": [],
                      "open_findings": None}
            state["rounds"].append(record)
            _write_loop(sdir, state)

            names = [agent.name for agent in reviewers]
            launch.rerun_agents(sdir, agent_names=names)
            failed = _wait(sdir, names, timeout, poll)
            update_round(record, failed_reviewers=failed)
            if len(failed) == len(names):
                raise RuntimeError("every reviewer failed; see the agent logs")

            _check_stop(sdir)
            update_round(record, status="curating")
            curator_name = launch.ensure_curator_agent(sdir)
            launch.launch_curator(sdir)
            if _wait(sdir, [curator_name], timeout, poll):
                raise RuntimeError("the curator failed; see its log")

            remaining = open_findings(sdir)
            update_round(record, open_findings=len(remaining))
            if not remaining:
                update_round(record, status="converged", finished_at=_now())
                reason = "the curator left no unresolved findings"
                if failed:
                    reason += f", but these reviewers failed this round: {', '.join(failed)}"
                state.update(status="converged", reason=reason)
                break

            _check_stop(sdir)
            update_round(record, status="editing")
            worktree = _prepare_editor_worktree(sdir, repo, head_before)
            scratch = sdir / "tmp" / editor.name
            scratch.mkdir(parents=True, exist_ok=True)
            _write_change_patch(load_session(sdir), scratch, head_before)
            launch.rerun_agents(sdir, agent_names=[editor.name], workspace_override=str(worktree))
            if _wait(sdir, [editor.name], timeout, poll):
                raise RuntimeError(f"the Editor failed; its partial edits are in {worktree}")

            head_after = _commit_round(sdir, load_session(sdir), worktree, editor.name, number)
            if head_after is None:
                left = len(open_findings(sdir))
                if left:
                    update_round(record, status="no-changes", finished_at=_now())
                    state.update(status="blocked", reason=(
                        f"the Editor made no changes and left {left} finding(s) open for you"))
                    break
                # Rebuttals only end the loop once reviewers have seen them.
                update_round(record, status="rebutted", finished_at=_now())
                expected_head = head_before
                continue
            update_round(record, head_after=head_after)
            blocked = _fast_forward(repo, head_before, head_after)
            if blocked:
                update_round(record, status="not-applied", finished_at=_now())
                state.update(status="blocked", reason=(
                    f"{blocked}; round {number} commit {head_after[:12]} was not applied. "
                    f"Apply it with: git merge --ff-only {head_after}"))
                break
            _migrate(sdir, head_after)
            expected_head = head_after
            update_round(record, status="committed", finished_at=_now())
        else:
            reason = f"ran the maximum of {max_rounds} rounds"
            if state["rounds"] and state["rounds"][-1].get("status") in {"committed", "rebutted"}:
                reason += "; reviewers have not checked the last round's edits or rebuttals"
            state.update(status="done", reason=reason)
    except Stopped:
        _kill(sdir)
        state.update(status="stopped", reason="stopped from the dashboard")
    except Exception as exc:  # recorded for the dashboard, then re-raised for the log
        _kill(sdir)
        state.update(status="failed", reason=str(exc))
        state["finished_at"] = _now()
        _write_loop(sdir, state)
        raise
    finally:
        if state["status"] != "failed" and _remove_editor_worktree(sdir, repo):
            state["editor_worktree"] = None
        state["finished_at"] = state["finished_at"] or _now()
        if state["status"] in ACTIVE_STATES:
            state["status"] = "failed"
        _write_loop(sdir, state)
        try:
            (sdir / STOP_FILE).unlink()
        except FileNotFoundError:
            pass
    return state


def spawn_driver(session_dir: str | Path, max_rounds: int) -> int:
    """Start `auto-rounds` detached from the web server; return its pid."""
    sdir = Path(session_dir).resolve()
    if not 1 <= max_rounds <= MAX_ROUNDS:
        raise ValueError(f"rounds must be between 1 and {MAX_ROUNDS}")
    _check_runnable(load_session(sdir))
    with _session_lock(sdir):
        existing = loop_status(sdir)
        if existing and existing["running"]:
            raise ValueError("automatic rounds are already running for this session")
        (sdir / "log").mkdir(exist_ok=True)
        try:
            (sdir / STOP_FILE).unlink()
        except FileNotFoundError:
            pass
        token = secrets.token_hex(16)
        starting = {"status": "starting", "pid": None, "spawned_at": time.time(),
                    "token": token, "started_at": _now(), "finished_at": None,
                    "max_rounds": max_rounds, "reason": "",
                    "rounds": (existing or {}).get("rounds", [])}
        _write_loop(sdir, starting)
        env = {**os.environ, DRIVER_TOKEN_ENV: token}
        try:
            with open(sdir / "log" / LOG_NAME, "a") as log:
                proc = subprocess.Popen(
                    [str(PR_BIN), "--session", str(sdir), "auto-rounds", "--rounds", str(max_rounds)],
                    stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    cwd=str(sdir), start_new_session=True, env=env,
                )
        except OSError as exc:
            _write_loop(sdir, {**starting, "status": "failed", "token": None,
                               "reason": f"could not start the round driver: {exc}",
                               "finished_at": _now()})
            raise
    return proc.pid


def _slug(value: str, limit: int) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9]+", "-", value)).strip("-").lower()[:limit].strip("-")


def create_local_session(cfg: dict, workspace: str, base: str, agents: list[dict]) -> Path:
    """Create a local review session for `workspace` from a validated project config."""
    enabled = [a for a in agents if a.get("enabled", True) is not False]
    roles = [a.get("role", "reviewer") for a in enabled]
    if "reviewer" not in roles:
        raise ValueError("enable at least one reviewer")
    if "editor" not in roles:
        raise ValueError("enable an Editor agent; add one in Settings first")
    if "curator" not in roles:
        raise ValueError("the config needs an enabled curator agent")
    workspace_path = Path(workspace).expanduser().resolve()
    if not workspace_path.is_dir():
        raise ValueError(f"not a directory: {workspace_path}")
    top = _git(workspace_path, "rev-parse", "--show-toplevel", check=False)
    if top.returncode != 0:
        raise ValueError(f"not a git checkout: {workspace_path}")
    repo = top.stdout.strip()
    try:
        base_sha = resolve_git_ref(repo, base)
        head_sha = resolve_git_ref(repo, "HEAD")
    except RuntimeError as exc:
        raise ValueError(f"cannot resolve {base!r}: {exc}") from exc
    if base_sha == head_sha:
        raise ValueError("the base commit equals HEAD; there is nothing to review")
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    name = branch if branch and branch != "HEAD" else head_sha[:12]
    stem = f"{_slug(Path(repo).name, 40) or 'repo'}-local-{_slug(name, 60) or head_sha[:12]}"
    root = Path(cfg["reviewRoot"])
    session_id = stem
    while (root / session_id).exists():
        session_id = f"{stem}-{secrets.token_hex(2)}"

    cli_json = Path(repo) / ".cursor" / "cli.json"
    if any(a.get("runner", "cursor") == "cursor" for a in agents) and not cli_json.exists():
        cli_json.parent.mkdir(exist_ok=True)
        shutil.copyfile(TEMPLATES_DIR / "cli.sample.json", cli_json)

    _session, session_dir = create_session(
        workspace=repo, base_ref=base_sha, topic_ref=head_sha, agents=agents,
        personas_dir=cfg.get("personasDir"),
        timeout=int(cfg.get("reviewAgentTimeoutSeconds", 1200)),
        session_dir=str(root / session_id), session_id=session_id,
    )
    return Path(session_dir)


def stop_on_signal(session_dir: str | Path):
    """Signal handler that asks the driver to stop at its next safe point."""
    stop_file = Path(session_dir) / STOP_FILE

    def handler(_signum, _frame) -> None:
        stop_file.write_text(_now() + "\n")
    return handler
