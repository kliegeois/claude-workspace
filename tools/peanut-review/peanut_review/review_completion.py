"""Session-local evidence of completed reviewer and curator runs.

Launch IDs bind completion to a pinned revision, independently of queue jobs.
Signals from older rounds and a subsequent sync cannot certify a new snapshot.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from pathlib import Path

from . import session as sess
from .models import _now_iso


def target(session) -> dict | None:
    pr = session.github
    if not pr or not pr.account:
        return None
    return {"account": asdict(pr.account), "repo": pr.repo.casefold(), "number": pr.number}


def snapshot(session) -> dict:
    return {"head_sha": session.current_head, "base_sha": session.base_ref,
            "base_ref": session.github.base_ref_name}


def _read(directory: Path) -> dict:
    try:
        data = json.loads((directory / "review-completion.json").read_text())
        return data if isinstance(data, dict) and data.get("version") == 1 else {}
    except (OSError, ValueError):
        return {}


def _save(directory: Path, data: dict) -> None:
    # Callers hold the session lock, including across concurrent supervisors.
    tmp = directory / "review-completion.json.tmp"
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(directory / "review-completion.json")


def _signal_time(directory: Path, name: str) -> int | None:
    try:
        return (directory / "signals" / f"{name}.round-done").stat().st_mtime_ns
    except FileNotFoundError:
        return None


def _workspace_matches(session) -> bool:
    if sess.workspace_head(session) != session.current_head:
        return False
    try:
        return not sess._run_git(sess.repo_path(session), "status", "--porcelain", "--untracked-files=no")
    except RuntimeError:
        return False


def begin_launch(directory, session, agents) -> dict[str, str]:
    """Register all selected runs before any supervisor can finish."""
    directory = Path(directory)
    if target(session) is None:
        return {}
    with sess._session_lock(directory):
        latest = sess.load_session(directory)
        if target(latest) != target(session) or snapshot(latest) != snapshot(session):
            raise ValueError("Session changed before launch; retry at the intended snapshot")
        data = _read(directory)
        data.setdefault("version", 1)
        runs = data.setdefault("runs", {})
        reviewers = sess.reviewer_agents(session)
        curators = sess.curator_agents(session)
        if any(a in reviewers for a in agents):
            # A curator from before this reviewer run cannot cover its findings.
            for agent in curators:
                runs.pop(agent.name, None)
        ids = {}
        workspace_matches = _workspace_matches(session)
        for agent in agents:
            ids[agent.name] = uuid.uuid4().hex
            runs[agent.name] = {"id": ids[agent.name], "target": target(session),
                                "snapshot": snapshot(session), "started_at": _now_iso(),
                                "workspace_matches": workspace_matches,
                                "initial_signal": _signal_time(directory, agent.name)}
        for agent in agents:
            if agent in curators:
                runs[agent.name]["reviewer_runs"] = {
                    a.name: runs.get(a.name, {}).get("id") for a in reviewers
                    if runs.get(a.name, {}).get("successful") or (
                        _signal_time(directory, a.name) is not None
                        and _signal_time(directory, a.name) != runs.get(a.name, {}).get("initial_signal"))
                }
        _save(directory, data)
        return ids


def finish_run(directory, agent_name: str, run_id: str | None, *, successful: bool) -> None:
    """Record a supervisor's result and certify only a complete, matching round."""
    if not run_id:
        return
    directory = Path(directory)
    with sess._session_lock(directory):
        session = sess.load_session(directory)
        data = _read(directory)
        runs = data.get("runs", {})
        run = runs.get(agent_name, {})
        if run.get("id") != run_id:
            return  # A late exit from a replaced launch must not finish its successor.
        run["finished_at"] = _now_iso()
        run["successful"] = successful and run.get("workspace_matches", False)
        _certify(directory, session, data)
        _save(directory, data)


def completed_review(directory, session) -> dict | None:
    completed = _read(Path(directory)).get("completed")
    if isinstance(completed, dict) and completed.get("target") == target(session):
        return completed
    return None


def _certify(directory: Path, session, data: dict) -> None:
    runs = data.get("runs", {})
    reviewers = sess.reviewer_agents(session)
    curators = sess.curator_agents(session)
    expected_target = target(session)
    expected_snapshot = snapshot(session) if expected_target else None
    complete = bool(reviewers) and len(curators) == 1 and expected_target is not None
    if complete:
        curator_run = runs.get(curators[0].name, {})
        complete = all(
            runs.get(a.name, {}).get("successful") is True
            and runs[a.name].get("snapshot") == expected_snapshot
            and runs[a.name].get("target") == expected_target
            for a in [*reviewers, *curators]
        ) and curator_run.get("reviewer_runs") == {
            a.name: runs.get(a.name, {}).get("id") for a in reviewers
        }
    if complete and _workspace_matches(session):
        data["completed"] = {"target": expected_target, "snapshot": expected_snapshot,
                             "completed_at": _now_iso()}


def incomplete_review(directory, session) -> bool:
    """Distinguish attempted reviews without a receipt from untouched sessions."""
    return any(run.get("target") == target(session) for run in
               _read(Path(directory)).get("runs", {}).values())


def recover_reviewer(directory, agent_name: str, *, dry_run: bool = False) -> dict:
    """Recover an orphaned local reviewer only when its completed curator covers it.

    This is explicit operator recovery, not an inferred process exit. Retain the
    missing exit status and record which signal and curator justify completion.
    """
    from datetime import datetime
    from . import runtime

    directory = Path(directory)
    with sess._session_lock(directory):
        session = sess.load_session(directory)
        data = _read(directory)
        runs = data.get("runs", {})
        reviewers = sess.reviewer_agents(session)
        curators = sess.curator_agents(session)
        if target(session) is None or len(curators) != 1 or not _workspace_matches(session):
            raise ValueError("Recovery requires a clean pinned GitHub workspace and one curator")
        reviewer = next((a for a in reviewers if a.name == agent_name), None)
        if reviewer is None or reviewer.ssh_target:
            raise ValueError("Recovery requires a configured local reviewer")
        for agent in session.agents:
            state = runtime.inspect_agent_runtime(directory, agent)
            if state["reviewer_live"] or state["supervisor_live"]:
                raise ValueError(f"Agent {agent.name} is still live")
        run = runs.get(agent_name, {})
        curator = runs.get(curators[0].name, {})
        if "successful" in run or "finished_at" in run:
            raise ValueError("Reviewer already has a terminal result; recovery cannot override it")
        for agent in session.agents:
            recorded = runs.get(agent.name, {})
            if (not recorded.get("id") or recorded.get("target") != target(session)
                    or recorded.get("snapshot") != snapshot(session)
                    or not recorded.get("workspace_matches")
                    or (agent.name != agent_name and recorded.get("successful") is not True)):
                raise ValueError(f"No matching successful run for {agent.name}")
        if curator.get("reviewer_runs") != {a.name: runs[a.name]["id"] for a in reviewers}:
            raise ValueError("Curator did not cover these exact reviewer runs")
        meta = runtime.read_agent_meta(directory, agent_name)
        if (meta.get("transport") == "ssh" or meta.get("ssh_cleanup_required")
                or meta.get("process_state") in {runtime.PROCESS_FAILED, runtime.PROCESS_TIMEOUT,
                                                  runtime.PROCESS_KILLED}
                or meta.get("timed_out") or meta.get("termination_signal")
                or meta.get("exit_code") not in (None, 0)):
            raise ValueError("Reviewer has failed or uncertain remote process evidence")
        signal_time = _signal_time(directory, agent_name)
        try:
            signal_text = (directory / "signals" / f"{agent_name}.round-done").read_text().strip()
            signaled_at = datetime.fromisoformat(signal_text)
            started_at = datetime.fromisoformat(run["started_at"])
            curated_at = datetime.fromisoformat(curator["started_at"])
            # Both the signal content and filesystem timestamp must be from this
            # launch and precede the curator that included this reviewer.
            if (signal_time is None or signal_time == run.get("initial_signal")
                    or not started_at <= signaled_at <= curated_at
                    or not started_at.timestamp() <= signal_time / 1e9 <= curated_at.timestamp()):
                raise ValueError("Signal is stale or outside the curated reviewer run")
        except (OSError, KeyError, TypeError) as exc:
            raise ValueError("Missing or invalid completion signal evidence") from exc
        evidence = {"agent": agent_name, "run_id": run["id"], "signal_at": signal_text,
                    "curator_run_id": curator["id"], "recovered_at": _now_iso()}
        if not dry_run:
            run.update(successful=True, finished_at=signal_text, recovery=evidence)
            _certify(directory, session, data)
            _save(directory, data)
        return evidence
