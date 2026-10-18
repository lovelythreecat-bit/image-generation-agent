"""File-based deployment settings, CLI consumers and offline migrations."""
import io
import json
from pathlib import Path

import pytest

from image_agent.server.settings import ServerSettings


@pytest.fixture
def server_config(tmp_path):
    from cryptography.fernet import Fernet

    path = tmp_path / "deployment" / "server.config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({
        "database_url": "mysql+asyncmy://app:db-private@localhost/image_agent",
        "token": "token-private",
        "secret_key": Fernet.generate_key().decode(),
        "data_dir": "./data",
        "host": "0.0.0.0",
        "port": 8123,
    }), encoding="utf-8-sig")
    return path


def test_file_settings_ignore_environment_and_resolve_paths(server_config, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMAGE_AGENT_SERVER_DATABASE_URL", "invalid-env-value")
    monkeypatch.setenv("IMAGE_AGENT_SERVER_PORT", "9999")
    settings = ServerSettings.from_file(server_config)
    assert settings.database_url == "mysql+asyncmy://app:db-private@localhost/image_agent"
    assert settings.port == 8123
    assert settings.data_dir == server_config.parent / "data"
    assert settings.worker_concurrency == 1
    assert "db-private" not in repr(settings) and "token-private" not in repr(settings)


@pytest.mark.parametrize("change", [
    {"database_url": "sqlite:///private.db"}, {"database_url": "mysql+asyncmy:///db"},
    {"token": " "}, {"secret_key": "private-invalid-key"}, {"data_dir": ""},
    {"data_dir": 42}, {"port": True}, {"port": "8000"}, {"port": 70000},
    {"worker_concurrency": 0}, {"root_path": 5}, {"host": []},
    {"unknown_private_field": "private-value"},
])
def test_invalid_file_fields_fail_without_exposing_values(server_config, change):
    mapping = json.loads(server_config.read_text("utf-8-sig"))
    mapping.update(change)
    server_config.write_text(json.dumps(mapping), "utf-8")
    with pytest.raises(ValueError) as caught:
        ServerSettings.from_file(server_config)
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("content", ["{bad-private-json", "[]", "{}", '{"token":"a","token":"b"}'])
def test_missing_or_malformed_file_never_falls_back_to_environment(server_config, content):
    server_config.write_text(content, "utf-8")
    with pytest.raises(ValueError):
        ServerSettings.from_file(server_config)
    server_config.unlink()
    with pytest.raises(ValueError):
        ServerSettings.from_file(server_config)


@pytest.mark.parametrize("arguments", [[], ["serve"], ["--config", "custom.json"],
    ["serve", "--config", "custom.json", "--host", "127.0.0.1", "--port", "8234"]])
def test_serve_uses_default_or_explicit_file_and_cli_overrides(
    server_config, monkeypatch, arguments,
):
    import uvicorn

    from image_agent.server.__main__ import main

    monkeypatch.chdir(server_config.parent)
    (server_config.parent / "custom.json").write_bytes(server_config.read_bytes())
    launched = []

    # Only the blocking network server is replaced; app construction stays real.
    def run(app, **kwargs):
        launched.append((app.state.settings, kwargs))

    monkeypatch.setattr(uvicorn, "run", run)
    assert main(arguments) == 0
    settings, options = launched[0]
    assert settings.data_dir == server_config.parent / "data"
    assert settings.port == (8234 if "--port" in arguments else 8123)
    assert options["host"] == ("127.0.0.1" if "--host" in arguments else "0.0.0.0")
    assert options["workers"] == 1
    import sys

    assert options['loop'] == ('image_agent.server.event_loop:selector_loop_factory'
                               if sys.platform == 'win32' else 'auto')


def test_uvicorn_loads_buffered_protocol_compatible_event_loop():
    import asyncio

    from uvicorn import Config

    factory = Config('unused:app',
        loop='image_agent.server.event_loop:selector_loop_factory').get_loop_factory()
    loop = factory()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()


def test_cli_missing_file_reports_actionable_error_without_environment_fallback(
    tmp_path, monkeypatch, capsys,
):
    from image_agent.server.__main__ import main

    monkeypatch.chdir(tmp_path)
    assert main([]) == 1
    error = capsys.readouterr().err
    assert "server.config.json" in error and "环境变量" not in error


@pytest.mark.parametrize("explicit", [False, True])
def test_alembic_offline_uses_same_file(server_config, monkeypatch, explicit):
    from alembic import command, context
    from alembic.config import Config

    monkeypatch.chdir(server_config.parent if not explicit else server_config.parent.parent)
    monkeypatch.setattr(context, "get_x_argument", lambda **kwargs:
                        {"config": str(server_config)} if explicit else {})
    output = io.StringIO()
    config = Config(str(Path(__file__).parents[1] / "server-alembic.ini"), output_buffer=output)
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE" in sql and "alembic_version" in sql
    assert "db-private" not in sql


def test_import_command_passes_file_settings_to_real_database_layer(server_config, monkeypatch):
    from image_agent.server import __main__ as cli
    from image_agent.server.database import Database

    source = server_config.parent / "provider.json"
    source.write_text("{}", "utf-8")
    connections = []

    # Stop at the external I/O boundary after the real CLI/import loaders run.
    def database(url):
        connections.append(url)
        raise RuntimeError("stop before connecting")

    monkeypatch.setattr(Database, "__init__", lambda self, url: database(url))
    assert cli.main(["import-config", "--config", str(server_config),
                     "--file", str(source), "--name", "方案"]) == 1
    assert connections == ["mysql+asyncmy://app:db-private@localhost/image_agent"]
