"""Hard deletion of one review session and, optionally, its linked worktree.

Deletion reuses the read-only cleanup evidence and is recomputed immediately
before anything is removed. A worktree is only removed with `git worktree
remove` when tracked files are clean and the user confirmed its exact untracked
files; its branch is kept so committed work survives.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from . import store
from .cleanup import inspect_cleanup
from .session import load_session, review_git_namespace


class DeletionBlocked(Exception):
    def __init__(self, message: str, plan: dict) -> None:
        super().__init__(message)
        self.plan = plan


def _git(cwd: str | Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )


def _disk_usage(path: Path) -> int | None:
    try:
        result = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True, timeout=120)
        return int(result.stdout.split()[0]) * 1024
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return None


def _session_directory(session_dir: Path, roots: list[Path]) -> Path:
    """Only delete a real session directory directly inside a served root."""
    if session_dir.is_symlink():
        raise ValueError("Refusing to delete a symlinked session directory")
    target = session_dir.resolve()
    if not (target / "session.json").is_file():
        raise ValueError("Not a session directory")
    if target.parent not in {Path(root).resolve() for root in roots}:
        raise ValueError("Session directory is not inside a served review root")
    return target


def _worktree_status(session_id: str, session_dir: Path, evidence: dict) -> dict:
    path = Path(evidence["worktree"])
    info = {"path": str(path), "exists": path.is_dir(), "removable": False, "reason": "",
            "branch": "", "notes": [], "untracked": [], "size_bytes": None, "common_dir": ""}
    if not info["exists"]:
        info["reason"] = "The checkout no longer exists."
        return info
    others = [s["id"] for s in evidence["sessions"] if s["id"] != session_id]
    if others:
        info["reason"] = "Also used by " + ", ".join(others) + "."
        return info
    if evidence["state"] != "idle":
        info["reason"] = "Activity in this checkout's sessions could not be ruled out."
        return info
    try:
        session_dir.relative_to(path)
        info["reason"] = "The session directory lives inside this checkout."
        return info
    except ValueError:
        pass

    def rev_parse(*args: str) -> str:
        result = _git(path, "rev-parse", *args)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "git rev-parse failed")
        return result.stdout.strip()

    try:
        if Path(rev_parse("--show-toplevel")).resolve() != path:
            info["reason"] = "The session reviews a subdirectory of a larger checkout."
            return info
        git_dir = Path(rev_parse("--absolute-git-dir")).resolve()
        common_dir = Path(rev_parse("--path-format=absolute", "--git-common-dir")).resolve()
        info["common_dir"] = str(common_dir)
        if git_dir == common_dir:
            info["reason"] = "This is a main clone, not a linked worktree."
            return info
        listing = _git(common_dir, "worktree", "list", "--porcelain", "-z")
        if listing.returncode:
            raise RuntimeError(listing.stderr.strip() or "git worktree list failed")
        entry, current = None, []
        for field in [*listing.stdout.split("\0"), ""]:
            if field:
                current.append(field)
                continue
            if current and Path(current[0].removeprefix("worktree ")).resolve() == path:
                entry = current
            current = []
        if entry is None:
            info["reason"] = "The checkout is not a registered worktree."
            return info
        if any(field == "locked" or field.startswith("locked ") for field in entry):
            info["reason"] = "The worktree is locked."
            return info
        status = _git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        if status.returncode:
            raise RuntimeError(status.stderr.strip() or "git status failed")
        entries = [entry for entry in status.stdout.split("\0") if entry]
        # Renames add their source path as an extra entry; any tracked change blocks anyway.
        tracked = [entry for entry in entries if not entry.startswith("?? ")]
        if tracked:
            info["reason"] = "It has uncommitted changes to tracked files."
            return info
        # Untracked files (runner config, build logs) are listed for confirmation and removed.
        info["untracked"] = sorted(entry[3:] for entry in entries)
        branch = _git(path, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
        info["branch"] = branch
        if branch:
            upstream = _git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
            ahead = (_git(path, "rev-list", "--count", "@{upstream}..HEAD").stdout.strip()
                     if upstream.returncode == 0 else "")
            if ahead and ahead != "0":
                info["notes"].append(f"Branch {branch} is kept; it has {ahead} commit(s) "
                                     f"not pushed to {upstream.stdout.strip()}.")
            else:
                info["notes"].append(f"Branch {branch} is kept.")
        else:
            refs = _git(path, "for-each-ref", "--count=1", "--contains", "HEAD",
                        "refs/heads", "refs/remotes", "refs/tags").stdout.strip()
            if not refs:
                info["reason"] = "Its detached HEAD has commits that are on no branch."
                return info
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        info["reason"] = f"Cannot inspect the checkout: {exc}"
        return info
    info["removable"] = True
    info["size_bytes"] = _disk_usage(path)
    return info


def deletion_plan(session_dir: Path, roots: list[Path], *, measure: bool = True,
                  now: datetime | None = None) -> dict:
    target = _session_directory(session_dir, roots)
    session = load_session(target)
    evidence = inspect_cleanup(target, roots, now=now)
    blockers = []
    for agent in evidence["agents"]:
        if agent["session"] != session.id:
            continue
        if agent["state"] == "busy":
            blockers.append(f"{agent['name']} is running ({agent['reason']}).")
        elif agent["state"] != "idle":
            blockers.append(f"Cannot confirm that {agent['name']} has stopped ({agent['reason']}).")
    if str(target) in evidence["recent_activity"]:
        blockers.append("The session changed within the last two minutes.")
    if any(job["session_id"] in {session.id, None} for job in evidence["active_jobs"]):
        blockers.append("A queue job may still be working on this session.")
    blockers.extend(error for error in evidence["errors"] if str(target) in error)

    comments = [c for c in store.read_all_comments(target) if not c.deleted]
    worktree = _worktree_status(session.id, target, evidence)
    if not measure:
        worktree["size_bytes"] = None
    return {
        "session": {
            "id": session.id, "path": str(target),
            "size_bytes": _disk_usage(target) if measure else None,
            "github": bool(session.github),
            "comment_count": len(comments),
            "unpublished_count": sum(1 for c in comments if not c.external_id) if session.github else 0,
        },
        "deletable": not blockers,
        "blockers": blockers,
        "worktree": worktree,
    }


def delete_session(session_dir: Path, roots: list[Path], *, remove_worktree: bool = False,
                   confirmed_untracked: list[str] | None = None,
                   now: datetime | None = None) -> dict:
    plan = deletion_plan(session_dir, roots, measure=False, now=now)
    if plan["blockers"]:
        raise DeletionBlocked("The session cannot be deleted right now.", plan)
    worktree = plan["worktree"]
    if remove_worktree and not worktree["removable"]:
        raise DeletionBlocked(f"The worktree cannot be removed: {worktree['reason']}", plan)
    if remove_worktree and sorted(confirmed_untracked or []) != worktree["untracked"]:
        raise DeletionBlocked("The worktree's untracked files changed; review them again.", plan)
    target = Path(plan["session"]["path"])
    session = load_session(target)
    result = {"session": str(target), "worktree": None, "refs_removed": 0, "warnings": []}
    if remove_worktree:
        # --force is needed for untracked files only; tracked changes were rejected above.
        force = ["--force"] if worktree["untracked"] else []
        removed = _git(worktree["common_dir"], "worktree", "remove", *force, worktree["path"], timeout=600)
        if removed.returncode:
            raise RuntimeError(f"git worktree remove failed: {removed.stderr.strip()}")
        result["worktree"] = worktree["path"]
    storage = session.git_common_dir
    if storage and Path(storage).is_dir():
        prefix = f"refs/peanut-review/{review_git_namespace(session)}/"
        refs = _git(storage, "for-each-ref", "--format=%(refname)", prefix)
        for ref in refs.stdout.split():
            if _git(storage, "update-ref", "-d", ref).returncode:
                result["warnings"].append(f"Could not delete {ref}")
            else:
                result["refs_removed"] += 1
    shutil.rmtree(target)
    return result
