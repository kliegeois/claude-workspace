"""Read-only cleanup evidence from shared session files, independent of PID namespaces."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

from .session import load_session, repo_path
from .models import AgentConfig


ACTIVE_JOBS = {"queued", "preparing", "reviewing", "curating"}
TERMINAL_STATES = {"exited", "failed", "timeout", "killed", "stopped"}
HEARTBEAT_MAX_AGE_SECONDS = 120
QUIET_PERIOD_SECONDS = 120
CLOCK_SKEW_SECONDS = 5


def _timestamp(value) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("timestamp is not a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed


def inspect_agent(agent, meta: dict, run: dict | None, now: datetime) -> dict:
    """Classify supervisor evidence without trusting UI status, signals, or PIDs.

    Stale heartbeats cannot establish that a process stopped. A completed run
    needs a consistent final supervisor record, including its exit code.
    """
    result = {"name": agent.name, "state": "unknown", "reason": "Incomplete runtime evidence"}
    try:
        if run is not None and not isinstance(run, dict):
            raise ValueError("invalid launch record")
        run = run or {}
        times = {key: _timestamp(meta.get(key)) for key in
                 ("start", "supervisor_start", "heartbeat_at", "end")}
        started = _timestamp(run.get("started_at"))
        finished = _timestamp(run.get("finished_at"))
        dates = [value for value in [*times.values(), started, finished] if value is not None]
        if any((value - now).total_seconds() > CLOCK_SKEW_SECONDS for value in dates):
            raise ValueError("runtime timestamp is in the future")
        if run and (not run.get("id") or started is None):
            raise ValueError("launch record is missing its ID or start time")
        if finished is not None and (started is None or finished < started):
            raise ValueError("launch completion precedes launch start")
        if meta.get("ssh_cleanup_required"):
            result["reason"] = "Remote cleanup remains unresolved"
            return result
        if not meta and not run and agent.status == "pending" and not agent.pid and not agent.supervisor_pid:
            return {**result, "state": "idle", "reason": "Agent has never launched"}

        state = meta.get("process_state")
        newest_start = max((value for value in (times["start"], times["supervisor_start"], started)
                            if value is not None), default=None)
        end = times["end"]
        heartbeat = times["heartbeat_at"]
        # A newer launch invalidates an old terminal record. An unfinished
        # launch record also covers the gap before its supervisor writes meta.
        unfinished = (state in {"launching", "running"} or (run and finished is None)
                      or (newest_start is not None and (end is None or end < newest_start)))
        if unfinished:
            activity = max((value for value in (heartbeat, newest_start) if value is not None), default=None)
            age = (now - activity).total_seconds() if activity else None
            if age is not None and age <= HEARTBEAT_MAX_AGE_SECONDS:
                return {**result, "state": "busy", "reason": "Recent unfinished launch or heartbeat"}
            result["reason"] = "Unfinished run with a stale or missing heartbeat; not evidence of exit"
            return result
        if state not in TERMINAL_STATES or type(meta.get("exit_code")) is not int:
            result["reason"] = "No final supervisor state and exit code"
            return result
        if newest_start is None or end is None or end < newest_start:
            raise ValueError("missing or inconsistent supervisor start/end times")
        if heartbeat is not None and heartbeat > end:
            raise ValueError("heartbeat is newer than the final supervisor record")
        if run and (finished is None or finished < end or times["start"] is None
                    or times["start"] < started):
            raise ValueError("final metadata does not cover the latest recorded launch")
        if agent.ssh_target or meta.get("transport") == "ssh":
            if (meta.get("ssh_cleanup_required") is not False
                    or meta.get("remote_process_state") not in {"stopped", "exited"}):
                result["reason"] = "Remote exit is not confirmed in shared metadata"
                return result
        if (now - end).total_seconds() < QUIET_PERIOD_SECONDS:
            return {**result, "state": "busy", "reason": "Supervisor just finished; quiet period"}
        return {**result, "state": "idle", "reason": "Consistent final supervisor record", "ended_at": end.isoformat()}
    except (ValueError, TypeError) as exc:
        result["reason"] = str(exc)
        return result


def _registered_worktree(session, requested: str | None) -> Path:
    original = Path(repo_path(session)).resolve()
    if requested is None:
        return original
    candidate = Path(requested)
    if not candidate.is_absolute():
        raise ValueError("worktree must be an absolute local path")
    candidate = candidate.resolve()
    storage = session.git_common_dir or str(original)
    result = subprocess.run(
        ["git", "-C", storage, "worktree", "list", "--porcelain", "-z"],
        capture_output=True, text=True, timeout=10,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if result.returncode:
        raise ValueError("Cannot establish registered worktrees for this session")
    registered = {Path(field.removeprefix("worktree ")).resolve()
                  for field in result.stdout.split("\0") if field.startswith("worktree ")}
    if candidate not in registered:
        raise ValueError("worktree is not registered with this session's repository")
    return candidate


def _queue_jobs(root: Path, ids: set[str]) -> list[dict]:
    try:
        state = json.loads((root / ".queue" / "state.json").read_text())
    except FileNotFoundError:
        return []
    jobs = []
    for item in state.get("items", {}).values():
        job = item.get("job") or {}
        if job.get("status") in ACTIVE_JOBS:
            # An active job without an associated session cannot be ruled out.
            if item.get("session_id") in ids or not item.get("session_id"):
                jobs.append({"session_id": item.get("session_id"), "status": job["status"]})
    return jobs


def _activity_files(directory: Path) -> dict[str, tuple[int, int]]:
    """Include launch preparation and review writes, without scanning runtime caches."""
    paths = [directory / "session.json", directory / "review-completion.json"]
    for name in ("prompts", "signals", "comments", "notes"):
        parent = directory / name
        if parent.exists():
            paths.extend(p for p in parent.iterdir() if p.is_file())
    paths.extend(directory.glob("log/*/meta.json"))
    return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in paths if p.exists()}


def inspect_cleanup(session_dir: Path, roots: list[Path], *,
                    worktree: str | None = None, now: datetime | None = None) -> dict:
    """Observe shared files twice; do not refresh statuses or contact another runtime."""
    now = now or datetime.now(timezone.utc)
    anchor = load_session(session_dir)
    target = _registered_worktree(anchor, worktree)
    errors, revisions, related, agents, jobs, activity = [], {}, [], [], [], {}

    def read(path: Path, *, optional: bool = False) -> dict:
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            if not optional:
                raise
            revisions[path] = None
            return {}
        revisions[path] = hashlib.sha256(content).hexdigest()
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError(f"Expected an object in {path}")
        return value

    roots = sorted({Path(root).resolve() for root in roots} | {session_dir.parent.resolve()})
    candidates = {session_dir.resolve()}
    scanned_roots = []
    for root in roots:
        try:
            candidates.update(p for p in root.iterdir() if (p / "session.json").is_file())
            scanned_roots.append(root)
        except OSError as exc:
            errors.append(f"Cannot discover sessions in {root}: {exc}")
    for directory in sorted(candidates):
        try:
            raw = read(directory / "session.json")
            checkout = (Path(raw["workspace"]) / raw.get("repo_relative", "")).resolve()
            if checkout != target:
                del revisions[directory / "session.json"]
                continue
            session = load_session(directory)
            related.append({"id": session.id, "path": str(directory)})
            activity[directory] = _activity_files(directory)
            completion = read(directory / "review-completion.json", optional=True)
            if completion and completion.get("version") != 1:
                raise ValueError("unsupported review-completion metadata version")
            runs = completion.get("runs", {})
            if not isinstance(runs, dict):
                raise ValueError("invalid launch records")
            configured = {agent.name: agent for agent in session.agents}
            names = set(configured) | set(runs)
            names.update(path.parent.name for path in directory.glob("log/*/meta.json"))
            for name in sorted(names):
                if not name or Path(name).name != name or name in {".", ".."}:
                    raise ValueError("invalid agent name in runtime records")
                # A changed lineup must not hide a previous agent's unfinished run.
                agent = configured.get(name) or AgentConfig(name=name, model="unknown")
                meta = read(directory / "log" / agent.name / "meta.json", optional=True)
                agents.append({"session": session.id, "configured": name in configured,
                               **inspect_agent(agent, meta, runs.get(agent.name), now)})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"Cannot inspect session {directory}: {exc}")

    ids = {s["id"] for s in related}
    queue_jobs = {}
    for root in roots:
        try:
            queue_jobs[root] = _queue_jobs(root, ids)
            jobs.extend(queue_jobs[root])
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            errors.append(f"Cannot inspect queue in {root}: {exc}")

    for path, digest in revisions.items():
        try:
            current = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            if digest != current:
                errors.append(f"Metadata changed during inspection: {path}")
        except OSError as exc:
            errors.append(f"Cannot recheck {path}: {exc}")
    current_related = set()
    for root in scanned_roots:
        try:
            for directory in root.iterdir():
                if (directory / "session.json").is_file():
                    session = load_session(directory)
                    if Path(repo_path(session)).resolve() == target:
                        current_related.add(str(directory.resolve()))
            if root in queue_jobs and queue_jobs[root] != _queue_jobs(root, ids):
                errors.append(f"Relevant queue activity changed during inspection: {root}")
        except (OSError, ValueError, KeyError, AttributeError, TypeError) as exc:
            errors.append(f"Cannot recheck sessions in {root}: {exc}")
    if current_related != {s["path"] for s in related}:
        errors.append("Sessions sharing the worktree changed during inspection")
    recent_activity = []
    for directory, files in activity.items():
        try:
            if files != _activity_files(directory):
                errors.append(f"Review activity changed during inspection: {directory}")
            latest_file = max((mtime / 1e9 for mtime, _ in files.values()), default=0)
            if now.timestamp() - latest_file < QUIET_PERIOD_SECONDS:
                recent_activity.append(str(directory))
        except OSError as exc:
            errors.append(f"Cannot recheck review activity in {directory}: {exc}")
    busy = bool(jobs or recent_activity or any(a["state"] == "busy" for a in agents))
    unknown = bool(errors or any(a["state"] == "unknown" for a in agents))
    return {
        "schema_version": 2, "observed_at": now.isoformat(), "evidence": "shared-session-metadata",
        "worktree": str(target), "sessions": related, "agents": agents, "active_jobs": jobs,
        "recent_activity": recent_activity, "quiet_period_seconds": QUIET_PERIOD_SECONDS,
        "errors": errors, "state": "busy" if busy else "unknown" if unknown else "idle",
    }
