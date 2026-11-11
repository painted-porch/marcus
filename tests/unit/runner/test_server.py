"""Unit tests for marcus_runner.server (no server is started here)."""

import json

import pytest
from marcus_runner import server

pytestmark = pytest.mark.unit


def test_write_config_switches_the_model_off_with_an_empty_key(tmp_path):
    """A placeholder key fires one real call on startup; empty key, flag off."""
    path = tmp_path / "config.json"
    config = server.write_config(
        path,
        port=4777,
        project_name="war-rehearsal",
        sqlite_db_path=tmp_path / "kanban.db",
    )
    on_disk = json.loads(path.read_text())
    assert on_disk == config
    assert config["ai"] == {
        "provider": "anthropic",
        "enabled": False,
        "anthropic_api_key": "",
    }
    assert config["kanban"]["provider"] == "sqlite"
    assert config["kanban"]["sqlite_db_path"].endswith("kanban.db")
    assert config["transport"]["http_port"] == 4777
    assert config["single_project_mode"] is False
    assert all(v is False for v in config["features"].values())


def test_build_env_strips_every_provider_key_and_sets_the_paths(tmp_path):
    """The server can make no paid call because it has no key to make one with."""
    base = {
        "ANTHROPIC_API_KEY": "k",
        "CLAUDE_API_KEY": "k",
        "OPENAI_API_KEY": "k",
        "HOME": "/h",
    }
    env = server.build_env(
        tmp_path / "checkout", tmp_path / "data", tmp_path / "c.json", base
    )
    assert not any(k in env for k in server.STRIPPED_KEYS)
    assert env["HOME"] == "/h"
    assert env["MARCUS_DATA_DIR"].endswith("data")
    assert env["MARCUS_CONFIG"].endswith("c.json")
    assert env["MARCUS_TELEMETRY"] == "off"
    assert env["PYTHONPATH"].endswith("checkout")


def test_model_calls_in_log_counts_provider_hosts(tmp_path):
    """Zero is the only acceptable count for a model-free run."""
    log = tmp_path / "server.log"
    assert server.model_calls_in_log(log) == 0
    log.write_text(
        "INFO AI Engine running in fallback mode\n"
        "INFO:httpx:HTTP Request: POST https://api.anthropic.com/v1/messages"
        ' "HTTP/1.1 200 OK"\n'
    )
    assert server.model_calls_in_log(log) == 1


def test_start_refuses_a_port_someone_else_holds(monkeypatch, tmp_path):
    """Never run against another session's Marcus."""
    monkeypatch.setattr(server, "port_in_use", lambda port, host="127.0.0.1": True)
    under_test = server.ServerUnderTest(
        tmp_path, tmp_path / "data", tmp_path / "c.json", 4777, tmp_path / "server.log"
    )
    with pytest.raises(RuntimeError, match="already in use"):
        under_test.start()


@pytest.mark.allows_loopback_network(
    "free_port binds a loopback socket to find a free port"
)
def test_free_port_is_free():
    """The port handed out is not in use at that moment."""
    port = server.free_port()
    assert 1024 < port < 65536
    assert server.port_in_use(port) is False
