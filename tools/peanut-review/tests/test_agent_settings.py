"""Agent enable/model settings for the project config and for one session."""
from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from peanut_review import curator, launch, session as sess
from peanut_review.models import AgentConfig
from peanut_review.web import agent_settings
from peanut_review.web import app as web_app


CONFIG = {
    "personasDir": "personas",
    "agents": [
        {"name": "Ada", "model": "m1", "persona": "ada.md"},
        {"name": "Bob", "model": "m1", "persona": "bob.md"},
        {"name": "Curator", "model": "m2", "role": "curator", "runner": "cursor"},
    ],
}


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / ".peanut-review.json"
    path.write_text(json.dumps(CONFIG, indent=2))
    path.chmod(0o640)
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    wd = tmp_path / "repo"
    wd.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(wd), *args], check=True)
    (wd / "a.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(wd), "add", "."], check=True)
    subprocess.run(["git", "-C", str(wd), "commit", "-q", "-m", "base"], check=True)
    (wd / "a.txt").write_text("two\n")
    subprocess.run(["git", "-C", str(wd), "commit", "-q", "-am", "topic"], check=True)
    return wd


@pytest.fixture
def session_dir(tmp_path: Path, repo: Path) -> Path:
    sd = tmp_path / "sessions" / "s1"
    sess.create_session(
        workspace=str(repo), base_ref="main~1", topic_ref="main",
        agents=[dict(a) for a in CONFIG["agents"]], session_dir=str(sd),
    )
    return sd


def test_parse_model_list_skips_headers_and_zero_width_chars():
    text = "Available models\n\nauto - Auto\u200b\ngpt-5.6-sol-medium - GPT 5.6 Sol\nnot a model line\n"
    assert agent_settings.parse_model_list(text) == [
        {"id": "auto", "label": "Auto"},
        {"id": "gpt-5.6-sol-medium", "label": "GPT 5.6 Sol"},
    ]


def test_disabled_agents_are_skipped_but_kept_in_the_lineup():
    agents = [
        AgentConfig(name="Ada", model="m"),
        AgentConfig(name="Bob", model="m", enabled=False),
        AgentConfig(name="Curator", model="m", role="curator"),
        AgentConfig(name="Editor", model="m", role="editor"),
    ]
    assert [a.name for a in curator.reviewers(agents)] == ["Ada"]
    assert [a.name for a in curator.editors(agents)] == ["Editor"]
    assert agents[1].to_dict()["enabled"] is False
    assert "enabled" not in agents[0].to_dict()
    assert AgentConfig.from_dict(agents[1].to_dict()).enabled is False


def test_update_config_toggles_models_and_keeps_other_keys(config: Path):
    agents = agent_settings.update_config_agents(
        config, [{"name": "Bob", "enabled": False}, {"name": "Ada", "model": "gpt-5.6-sol-medium"}],
    )
    data = json.loads(config.read_text())
    assert data["personasDir"] == "personas"
    assert data["agents"][1]["enabled"] is False
    assert data["agents"][0]["model"] == "gpt-5.6-sol-medium"
    assert config.stat().st_mode & 0o777 == 0o640
    assert [a["enabled"] for a in agents] == [True, False, True]

    agent_settings.update_config_agents(config, [{"name": "Bob", "enabled": True}])
    assert "enabled" not in json.loads(config.read_text())["agents"][1]


def test_update_config_can_add_an_editor_from_the_curator(config: Path):
    agents = agent_settings.update_config_agents(config, [], add_editor=True)
    editor = agents[-1]
    assert editor["name"] == "Editor" and editor["role"] == "editor"
    assert editor["model"] == "m2"
    agent_settings.update_config_agents(config, [], add_editor=True)
    assert sum(a.get("role") == "editor" for a in json.loads(config.read_text())["agents"]) == 1


@pytest.mark.parametrize("updates, message", [
    ([{"name": "Ada", "enabled": False}, {"name": "Bob", "enabled": False}], "at least one reviewer"),
    ([{"name": "Nobody", "enabled": False}], "unknown agent"),
    ([{"name": "Ada", "enabled": "no"}], "true or false"),
    ([{"name": "Ada", "model": "bad model; rm -rf"}], "invalid model"),
    ({"name": "Ada"}, "must be a list"),
    ([{"name": "Curator", "enabled": False}], "curator cannot be disabled"),
])
def test_update_config_rejects_bad_updates_without_writing(config: Path, updates, message):
    before = config.read_text()
    with pytest.raises(agent_settings.SettingsError, match=message):
        agent_settings.update_config_agents(config, updates)
    assert config.read_text() == before


def test_find_review_configs_dedupes_and_skips_missing(tmp_path: Path, config: Path):
    root = tmp_path / "sessions"
    root.mkdir()
    queue = {"accounts": [{"reviewConfig": str(config)}, {"reviewConfig": str(tmp_path / "nope.json")}]}
    assert agent_settings.find_review_configs(root, queue, str(config)) == [config.resolve()]


def test_update_session_agents_changes_the_next_launch(session_dir: Path):
    agent_settings.update_session_agents(
        session_dir, [{"name": "Bob", "enabled": False}, {"name": "Ada", "model": "m9"}],
    )
    loaded = sess.load_session(session_dir)
    by_name = {a.name: a for a in loaded.agents}
    assert by_name["Bob"].enabled is False
    assert by_name["Ada"].model == "m9"
    assert [a.name for a in launch._select_agents(loaded.agents, None)] == ["Ada"]
    with pytest.raises(ValueError, match="disabled agent"):
        launch._select_agents(loaded.agents, ["Bob"])


def test_update_session_agents_refuses_while_an_agent_runs(session_dir: Path):
    def fake_runtime(_sd, agent):
        return {"process_state": "running" if agent.name == "Ada" else "exited"}
    with patch.object(agent_settings.runtime, "inspect_agent_runtime", fake_runtime):
        with pytest.raises(agent_settings.SettingsError, match="Ada"):
            agent_settings.update_session_agents(session_dir, [{"name": "Bob", "enabled": False}])
    assert all(a.enabled for a in sess.load_session(session_dir).agents)


def _post(url: str, body: dict, token: str | None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Peanut-Queue-Token"] = token
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_settings_endpoints_require_the_page_token(session_dir: Path, config: Path):
    registry = web_app.SessionRegistry([session_dir.parent])
    srv = web_app.make_server("127.0.0.1", 0, registry, review_config=str(config))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(f"{base}/api/settings") as resp:
            data = json.loads(resp.read())
        assert [c["path"] for c in data["configs"]] == [str(config.resolve())]

        body = {"config": str(config.resolve()), "agents": [{"name": "Bob", "enabled": False}]}
        code, _ = _post(f"{base}/api/settings/agents", body, None)
        assert code == 403
        code, _ = _post(f"{base}/api/settings/agents", body, srv.RequestHandlerClass.queue_token)
        assert code == 200
        assert json.loads(config.read_text())["agents"][1]["enabled"] is False

        code, _ = _post(f"{base}/api/settings/agents",
                        {**body, "config": str(session_dir / "session.json")},
                        srv.RequestHandlerClass.queue_token)
        assert code == 400

        session_id = sess.load_session(session_dir).id
        session_url = f"{base}/{session_id}/api/agents/config"
        code, _ = _post(session_url, {"agents": [{"name": "Ada", "model": "m7"}]}, "wrong")
        assert code == 403
        code, data = _post(session_url, {"agents": [{"name": "Ada", "model": "m7"}]},
                           srv.RequestHandlerClass.queue_token)
        assert code == 200
        assert next(a for a in data["agents"] if a["name"] == "Ada")["model"] == "m7"

        with urllib.request.urlopen(f"{base}/settings") as resp:
            assert b"settings.js" in resp.read()
    finally:
        srv.shutdown()


def test_writer_actions_are_localhost_only(session_dir: Path, config: Path):
    registry = web_app.SessionRegistry([session_dir.parent])
    srv = web_app.make_server("127.0.0.1", 0, registry, review_config=str(config))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    token = srv.RequestHandlerClass.queue_token
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/settings/agents",
            data=json.dumps({"config": str(config.resolve()), "agents": []}).encode(),
            headers={"Content-Type": "application/json", "X-Peanut-Queue-Token": token,
                     "Host": "rebound.example:80"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req)
        assert err.value.code == 403
        code, _ = _post(f"http://127.0.0.1:{port}/api/settings/agents", ["not", "an", "object"], token)
        assert code == 400
    finally:
        srv.shutdown()
