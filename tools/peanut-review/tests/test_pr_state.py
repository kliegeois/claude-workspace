"""Merged-PR detection marks sessions safe to delete without deleting them."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from peanut_review import models, pr_state
from peanut_review import session as sess
from peanut_review.web.app import SessionRegistry
from peanut_review.web.render import _render_session_row

from .test_gh import _make_gh_session, gh_shim  # noqa: F401  (fixture)


PULL = ["api", "repos/acme/foo/pulls/42"]


def _pull(state: str, merged: bool) -> dict:
    return {
        "match": PULL,
        "stdout": json.dumps({
            "state": state, "merged": merged,
            "merged_at": "2026-11-18T10:00:00Z" if merged else None,
            "closed_at": "2026-11-18T10:00:00Z" if state == "closed" else None,
            "head": {"sha": "abc"},
        }),
    }


def _pull_calls(shim) -> list[dict]:
    return [c for c in shim.calls() if c["argv"][:2] == PULL]


def test_merged_pr_is_safe_to_delete_and_not_fetched_again(gh_shim, tmp_path):
    sd = _make_gh_session(tmp_path)
    gh_shim.set_fixtures([_pull("closed", True)])

    state = pr_state.refresh_pr_state(sd)
    assert state["state"] == "merged"
    assert state["merged_at"] == "2026-11-18T10:00:00Z"
    assert state["error"] == ""
    assert pr_state.cleanup_hint(state, "done") == {
        "safe_to_delete": True, "label": "PR merged · safe to delete",
    }

    pr_state.refresh_pr_state(sd)
    assert len(_pull_calls(gh_shim)) == 1
    assert Path(sd).is_dir()


def test_open_pr_respects_min_age_and_force(gh_shim, tmp_path):
    sd = _make_gh_session(tmp_path)
    gh_shim.set_fixtures([_pull("open", False)])

    assert pr_state.refresh_pr_state(sd)["state"] == "open"
    pr_state.refresh_pr_state(sd, min_age_seconds=900)
    assert len(_pull_calls(gh_shim)) == 1

    later = datetime.now(timezone.utc) + timedelta(seconds=901)
    pr_state.refresh_pr_state(sd, min_age_seconds=900, now=later)
    assert len(_pull_calls(gh_shim)) == 2

    pr_state.refresh_pr_state(sd, force=True)
    assert len(_pull_calls(gh_shim)) == 3


def test_closed_unmerged_pr_is_not_marked_safe(gh_shim, tmp_path):
    sd = _make_gh_session(tmp_path)
    gh_shim.set_fixtures([_pull("closed", False)])
    state = pr_state.refresh_pr_state(sd)
    assert state["state"] == "closed"
    assert pr_state.cleanup_hint(state, "done")["safe_to_delete"] is False


def test_running_agents_keep_a_merged_session_unsafe():
    hint = pr_state.cleanup_hint({"state": "merged"}, "running")
    assert hint["safe_to_delete"] is False
    assert "running" in hint["label"]


def test_fetch_error_keeps_last_known_state(gh_shim, tmp_path):
    sd = _make_gh_session(tmp_path)
    gh_shim.set_fixtures([_pull("open", False)])
    pr_state.refresh_pr_state(sd)

    gh_shim.set_fixtures([{"match": PULL, "rc": 1, "stderr": "HTTP 502"}])
    state = pr_state.refresh_pr_state(sd, force=True)
    assert state["state"] == "open"
    assert state["error"]


def test_local_session_has_no_pr_state(tmp_path):
    sd = tmp_path / "local"
    (sd / "comments").mkdir(parents=True)
    sess.save_session(sd, models.Session(
        id="local-review", workspace=str(tmp_path), base_ref="a", topic_ref="b",
    ))
    assert pr_state.refresh_pr_state(sd) == {}


def test_dashboard_marks_merged_session_safe_to_delete(gh_shim, tmp_path):
    sd = _make_gh_session(tmp_path)
    gh_shim.set_fixtures([_pull("closed", True)])
    pr_state.refresh_pr_state(sd)

    registry = SessionRegistry()
    registry.bind(sd)
    [summary] = registry.list_sessions()
    assert summary["is_github"] is True
    assert summary["pr_state"] == "merged"
    assert summary["cleanup"]["safe_to_delete"] is True

    row = _render_session_row(summary)
    assert "safe-to-delete" in row
    assert "PR merged · safe to delete" in row
    assert "session-pr-check" in row
    assert Path(sd).is_dir()
