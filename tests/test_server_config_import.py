import json

import pytest


def test_import_cli_reads_only_explicit_file_and_outputs_public_id(tmp_path, monkeypatch, capsys):
    from cryptography.fernet import Fernet

    from image_agent.server import __main__ as cli
    monkeypatch.chdir(tmp_path)
    (tmp_path / "server.config.json").write_text(json.dumps({
        "database_url": "mysql+asyncmy://u:p@localhost/test", "token": "test-token",
        "secret_key": Fernet.generate_key().decode(), "data_dir": "data",
    }), "utf-8")
    source = tmp_path / "config.json"
    source.write_text(json.dumps({"openrouter_api_key": "raw-secret"}), "utf-8")
    imported = []

    async def import_config(path, name, settings):
        imported.append((path, name))
        return {"id": "agent-id", "name": name, "api_key": "raw-secret"}

    monkeypatch.setattr(cli, "import_config_file", import_config)
    assert cli.main(["import-config", "--file", str(source), "--name", "方案"]) == 0
    assert imported == [(source, "方案")]
    output = capsys.readouterr()
    assert "agent-id" in output.out and "raw-secret" not in output.out + output.err


@pytest.mark.asyncio
async def test_invalid_json_rejected_before_database_access(tmp_path, monkeypatch):
    from image_agent.server.__main__ import import_config_file
    from image_agent.server.settings import ServerError
    source = tmp_path / "invalid.json"
    source.write_text('{"secret": "file-secret"', "utf-8")
    with pytest.raises(ServerError) as caught:
        await import_config_file(source, "方案")
    assert caught.value.code == "invalid_config_file"
    assert "file-secret" not in str(caught.value)


def test_cli_failure_does_not_print_exception_secrets(tmp_path, monkeypatch, capsys):
    from cryptography.fernet import Fernet

    from image_agent.server import __main__ as cli
    monkeypatch.chdir(tmp_path)
    (tmp_path / "server.config.json").write_text(json.dumps({
        "database_url": "mysql+asyncmy://u:p@localhost/test", "token": "test-token",
        "secret_key": Fernet.generate_key().decode(), "data_dir": "data",
    }), "utf-8")

    async def broken(*args):
        raise RuntimeError("raw-provider-key mysql-password")

    monkeypatch.setattr(cli, "import_config_file", broken)
    assert cli.main(["import-config", "--file", "explicit.json", "--name", "方案"]) == 1
    output = capsys.readouterr()
    assert "raw-provider-key" not in output.out + output.err
    assert "mysql-password" not in output.out + output.err


@pytest.fixture
async def import_settings(tmp_path):
    from cryptography.fernet import Fernet

    from image_agent.server.database import Database
    from image_agent.server.db_models import Base
    from image_agent.server.settings import ServerSettings
    settings = ServerSettings(f"sqlite+aiosqlite:///{tmp_path / 'import.db'}", "test-token",
                              Fernet.generate_key().decode(), tmp_path)
    db = Database(settings.database_url)
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield settings
    await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_explicit_file_import_normalizes_full_configuration(import_settings, tmp_path, monkeypatch, legacy):
    from image_agent.server.__main__ import import_config_file
    from image_agent.server.config_service import ConfigService
    from image_agent.server.database import Database
    from image_agent.server.secrets import SecretCipher
    monkeypatch.setenv("HTTP_IMPORT_CREDENTIAL", "env-import-secret")
    mapping = {"services": {
        "image": {"base_url": "https://images.example/v1", "protocol": "openai_images", "api_key_env": "HTTP_IMPORT_CREDENTIAL"},
        "vision": {"base_url": "https://vision.example/v1", "protocol": "chat_completions", "api_key": "vision-import-secret"}},
        "image_models": {"fast": {"service": "image", "model": "image-model"}},
        "stages": {"vision": {"service": "vision", "model": "vision-model"},
                   "review_product": {"service": "vision", "model": "review-model"}},
        "model_concurrency": 3, "model_completion_attempts": 5}
    if legacy:
        mapping = {"openrouter_api_key": "legacy-import-secret", "model_fast": "image-model",
                   "quality_model": "vision-model", "model_concurrency": 3, "model_completion_attempts": 5}
    source = tmp_path / "explicit.json"
    source.write_text(json.dumps(mapping), "utf-8")
    result = await import_config_file(source, "导入方案", import_settings)
    db = Database(import_settings.database_url)
    try:
        config, _ = await ConfigService(db.session_factory, SecretCipher(import_settings.secret_key)).resolve_agent_config(result["id"])
        assert config.model_name("fast") == "image-model"
        assert config.vision_route("vision").model == "vision-model"
        assert config.model_concurrency == 3 and config.model_completion_attempts == 5
        assert config.services and not config.openrouter_api_key
        assert all(service.api_key_env is None for service in config.services.values())
        if not legacy:
            assert config.vision_route("review_product").model == "review-model"
        assert "import-secret" not in json.dumps(result)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_missing_env_credential_import_is_atomic(import_settings, tmp_path, monkeypatch):
    from sqlalchemy import func, select

    from image_agent.server.__main__ import import_config_file
    from image_agent.server.database import Database
    from image_agent.server.db_models import AgentConfigRecord, ModelConfig, ProviderService
    from image_agent.server.settings import ServerError
    monkeypatch.delenv("MISSING_IMPORT_KEY", raising=False)
    mapping = {"services": {
        "image": {"base_url": "https://images.example/v1", "protocol": "openai_images", "api_key": "valid-secret"},
        "vision": {"base_url": "https://vision.example/v1", "protocol": "chat_completions", "api_key_env": "MISSING_IMPORT_KEY"}},
        "image_models": {"fast": {"service": "image", "model": "image-model"}},
        "stages": {"vision": {"service": "vision", "model": "vision-model"}}}
    source = tmp_path / "explicit.json"
    source.write_text(json.dumps(mapping), "utf-8")
    with pytest.raises(ServerError) as caught:
        await import_config_file(source, "方案", import_settings)
    assert caught.value.status_code == 422 and "valid-secret" not in str(caught.value)
    db = Database(import_settings.database_url)
    try:
        async with db.session_factory() as session:
            for model in (AgentConfigRecord, ModelConfig, ProviderService):
                assert await session.scalar(select(func.count()).select_from(model)) == 0
    finally:
        await db.close()
