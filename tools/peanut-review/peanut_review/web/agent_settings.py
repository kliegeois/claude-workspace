"""Enable/disable agents and choose their models from the web UI.

Two scopes share the same update rules: the project review config, which
seeds new sessions, and one session's lineup, which only applies before a
launch or rerun.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time

from .. import curator, runtime
from ..models import AgentConfig, AgentRole
from ..session import _session_lock, load_session, save_session


CONFIG_FILE_NAME = ".peanut-review.json"
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_MODEL_LINE_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._:/-]*)\s+-\s+(.+?)\s*$")
MODELS_TTL_SECONDS = 600
EDITOR_AGENT_NAME = "Editor"

_models_lock = threading.Lock()
_models_cache: tuple[float, list[dict]] | None = None


class SettingsError(ValueError):
    pass


def parse_model_list(text: str) -> list[dict]:
    models = []
    for line in text.splitlines():
        match = _MODEL_LINE_RE.match(line.replace("\u200b", ""))
        if match:
            models.append({"id": match.group(1), "label": match.group(2).strip()})
    return models


def list_models(*, refresh: bool = False) -> list[dict]:
    """Return `cursor-agent --list-models`, cached; empty when unavailable."""
    global _models_cache
    with _models_lock:
        if not refresh and _models_cache and time.monotonic() - _models_cache[0] < MODELS_TTL_SECONDS:
            return list(_models_cache[1])
        try:
            result = subprocess.run(
                ["cursor-agent", "--list-models"], capture_output=True, text=True, timeout=60,
            )
            models = parse_model_list(result.stdout) if result.returncode == 0 else []
        except (OSError, subprocess.TimeoutExpired):
            models = []
        if models:
            _models_cache = (time.monotonic(), models)
        return models


def find_review_configs(primary_root: Path, queue_config: dict | None = None,
                        explicit: str | None = None) -> list[Path]:
    """Locate project review configs: explicit, the queue's, then next to the session root."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    for account in (queue_config or {}).get("accounts", []):
        if account.get("reviewConfig"):
            candidates.append(Path(account["reviewConfig"]).expanduser())
    candidates.append(Path(primary_root).parent / CONFIG_FILE_NAME)
    seen, found = set(), []
    for path in candidates:
        resolved = path.resolve()
        if resolved not in seen and resolved.is_file():
            seen.add(resolved)
            found.append(resolved)
    return found


def _role(agent: dict) -> str:
    return agent.get("role", AgentRole.REVIEWER.value)


def describe_agents(agents: list[dict]) -> list[dict]:
    return [{
        "name": agent.get("name", ""),
        "role": _role(agent),
        "persona": agent.get("persona", ""),
        "model": agent.get("model", ""),
        "runner": agent.get("runner", "cursor"),
        "enabled": agent.get("enabled", True) is not False,
    } for agent in agents]


def _validate_updates(updates) -> dict[str, dict]:
    if not isinstance(updates, list):
        raise SettingsError("agents must be a list")
    by_name: dict[str, dict] = {}
    for update in updates:
        if not isinstance(update, dict) or not isinstance(update.get("name"), str):
            raise SettingsError("each agent update needs a name")
        change = {}
        if "enabled" in update:
            if type(update["enabled"]) is not bool:
                raise SettingsError(f"{update['name']}: enabled must be true or false")
            change["enabled"] = update["enabled"]
        if "model" in update:
            model = update["model"]
            if not isinstance(model, str) or not MODEL_ID_RE.match(model.strip()):
                raise SettingsError(f"{update['name']}: invalid model id {model!r}")
            change["model"] = model.strip()
        by_name[update["name"]] = change
    return by_name


def _apply(agents: list[dict], updates: dict[str, dict]) -> list[dict]:
    names = {agent.get("name") for agent in agents}
    unknown = sorted(set(updates) - names)
    if unknown:
        raise SettingsError(f"unknown agent(s): {', '.join(unknown)}")
    result = []
    for agent in agents:
        agent = dict(agent)
        change = updates.get(agent.get("name"), {})
        if "model" in change:
            agent["model"] = change["model"]
        if "enabled" in change:
            if change["enabled"]:
                agent.pop("enabled", None)
            else:
                agent["enabled"] = False
        result.append(agent)
    enabled_reviewers = [a for a in result if _role(a) == AgentRole.REVIEWER.value
                         and a.get("enabled", True) is not False]
    if not enabled_reviewers:
        raise SettingsError("at least one reviewer must stay enabled")
    return result


def _write_json(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_config(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SettingsError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
        raise SettingsError(f"{path} has no agents list")
    return data


def update_config_agents(path: Path, updates, *, add_editor: bool = False) -> list[dict]:
    """Apply enable/model changes to the project config; other keys are kept as written."""
    data = read_config(path)
    agents = data["agents"]
    if add_editor and not any(_role(a) == AgentRole.EDITOR.value for a in agents):
        template = next((a for a in agents if _role(a) == AgentRole.CURATOR.value), None) or agents[0]
        agents = [*agents, {
            "name": EDITOR_AGENT_NAME,
            "model": template.get("model", ""),
            "runner": template.get("runner", "cursor"),
            "role": AgentRole.EDITOR.value,
        }]
    data["agents"] = _apply(agents, _validate_updates(updates))
    _write_json(Path(path), data)
    return describe_agents(data["agents"])


def update_session_agents(session_dir: Path, updates) -> list[dict]:
    """Change one session's lineup. Refused while any of its agents is live."""
    by_name = _validate_updates(updates)
    with _session_lock(session_dir):
        session = load_session(session_dir)
        live = [agent.name for agent in session.agents
                if runtime.inspect_agent_runtime(session_dir, agent)["process_state"]
                in {runtime.PROCESS_LAUNCHING, runtime.PROCESS_RUNNING}]
        if live:
            raise SettingsError(f"stop or wait for running agent(s) first: {', '.join(live)}")
        updated = _apply([agent.to_dict() | {"name": agent.name} for agent in session.agents], by_name)
        for agent, raw in zip(session.agents, updated):
            agent.model = raw["model"]
            agent.enabled = raw.get("enabled", True) is not False
        save_session(session_dir, session)
    return describe_agents([agent.to_dict() for agent in session.agents])


def session_editor(session) -> AgentConfig | None:
    editors = curator.editors(session.agents)
    return editors[0] if editors else None
