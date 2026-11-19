"""Track whether a reviewed GitHub PR is still open, closed, or merged.

The state is stored next to the session so the dashboard can mark merged
reviews as safe to delete. Nothing here deletes a session or a worktree.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Iterable

from . import gh
from .models import _now_iso
from .session import load_session


STATE_FILE = "pr-state.json"
DEFAULT_POLL_SECONDS = 15 * 60
# A merged PR cannot reopen, so it is only fetched again on an explicit refresh.
TERMINAL_STATES = {"merged"}
BUSY_PROGRESS = {"running"}


def read_pr_state(session_dir: str | Path) -> dict:
    try:
        value = json.loads((Path(session_dir) / STATE_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_pr_state(session_dir: str | Path, state: dict) -> None:
    directory = Path(session_dir)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".pr-state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, directory / STATE_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _parse_pull(raw: dict) -> dict:
    merged = bool(raw.get("merged") or raw.get("merged_at"))
    state = "merged" if merged else ("closed" if raw.get("state") == "closed" else "open")
    return {
        "state": state,
        "merged_at": raw.get("merged_at") or "",
        "closed_at": raw.get("closed_at") or "",
        "head_sha": (raw.get("head") or {}).get("sha") or "",
    }


def fetch_pr_state(ghpr) -> dict:
    """Read the PR's state from GitHub with the session's bound account."""
    with gh.pr_auth(ghpr):
        raw = json.loads(gh._api(f"repos/{ghpr.repo}/pulls/{ghpr.number}"))
    return _parse_pull(raw)


def refresh_pr_state(session_dir: str | Path, *, force: bool = False,
                     min_age_seconds: float = 0, now: datetime | None = None) -> dict:
    """Fetch and store the PR state for a GitHub session.

    Returns the stored state, unchanged when the session is not a GitHub
    review, the state is terminal, or it was checked within `min_age_seconds`.
    A failed fetch is recorded as `error` and keeps the last known state.
    """
    session = load_session(session_dir)
    if not session.github or not session.github.account:
        return {}
    previous = read_pr_state(session_dir)
    now = now or datetime.now(timezone.utc)
    if not force:
        if previous.get("state") in TERMINAL_STATES:
            return previous
        checked = previous.get("checked_at")
        if checked and min_age_seconds:
            try:
                age = (now - datetime.fromisoformat(checked.replace("Z", "+00:00"))).total_seconds()
            except ValueError:
                age = min_age_seconds
            if age < min_age_seconds:
                return previous
    try:
        current = fetch_pr_state(session.github)
    except (gh.GhError, gh.RepoAccountError, RuntimeError, ValueError, KeyError) as exc:
        state = {**previous, "error": str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__,
                 "checked_at": _now_iso()}
    else:
        state = {**current, "error": "", "checked_at": _now_iso()}
    _write_pr_state(session_dir, state)
    return state


def cleanup_hint(pr_state: dict, progress_status: str) -> dict:
    """Describe whether the session can be deleted without losing an open review."""
    state = pr_state.get("state", "")
    if state == "merged" and progress_status not in BUSY_PROGRESS:
        return {"safe_to_delete": True, "label": "PR merged · safe to delete"}
    if state == "merged":
        return {"safe_to_delete": False, "label": "PR merged · agents still running"}
    if state == "closed":
        return {"safe_to_delete": False, "label": "PR closed without merge"}
    return {"safe_to_delete": False, "label": ""}


def refresh_sessions(session_dirs: Iterable[str | Path], *, min_age_seconds: float,
                     stop: threading.Event | None = None) -> int:
    """Refresh every GitHub session that is due. Returns how many were fetched."""
    fetched = 0
    for session_dir in session_dirs:
        if stop is not None and stop.is_set():
            break
        try:
            before = read_pr_state(session_dir).get("checked_at")
            after = refresh_pr_state(session_dir, min_age_seconds=min_age_seconds).get("checked_at")
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if after and after != before:
            fetched += 1
    return fetched


class PRStatePoller:
    """Background thread that keeps `pr-state.json` current for the dashboard."""

    def __init__(self, sessions, *, poll_seconds: float = DEFAULT_POLL_SECONDS) -> None:
        self._sessions = sessions
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pr-state-poller", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            refresh_sessions(self._sessions(), min_age_seconds=self._poll_seconds, stop=self._stop)
            self._stop.wait(self._poll_seconds)
