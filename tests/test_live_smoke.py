import json

import pytest

from tests.fakes import FakeGenerator, FakeVision
from tests.test_images import picture


@pytest.mark.parametrize("entrypoint", ["smoke", "cli"])
def test_repeated_command_without_id_saves_both_runs(tmp_path, monkeypatch, entrypoint):
    from image_agent import pipeline
    from image_agent.__main__ import run_cli
    from scripts.live_smoke import main

    # Replace provider boundaries only; exercise the real graph and durable saving.
    async def load(source):
        return source.path.read_bytes()

    monkeypatch.setattr(
        pipeline,
        "_dependencies",
        lambda *args, **kwargs: pipeline.Dependencies(FakeVision(), FakeGenerator(), load),
    )
    monkeypatch.setenv("IMAGE_AGENT_OPENROUTER_API_KEY", "offline-test-key")
    monkeypatch.delenv("IMAGE_AGENT_HTTP_PROXY", raising=False)
    source = tmp_path / "cup.png"
    source.write_bytes(picture())
    output = tmp_path / "out"
    args = [
        "--material",
        str(source),
        "--name",
        "cup",
        "--category",
        "kitchen",
        "--platform",
        "taobao",
        "--output",
        "main_image",
        "--out",
        str(output),
    ]
    run = main if entrypoint == "smoke" else run_cli
    args.insert(0, "--live" if entrypoint == "smoke" else "create")
    assert run(args) == 0
    assert run(args) == 0
    manifests = list(output.glob("*/result.json"))
    assert len(manifests) == 2
    for manifest in manifests:
        data = json.loads(manifest.read_text("utf-8"))
        assert data["status"] == "succeeded"
        assert data["assets"][0]["image"].startswith("approved/")
        assert (manifest.parent / data["assets"][0]["image"]).read_bytes() == picture((1600, 1600))


def test_smoke_missing_key_reports_error_without_traceback(tmp_path, monkeypatch, capsys):
    from scripts.live_smoke import main

    monkeypatch.delenv("IMAGE_AGENT_OPENROUTER_API_KEY", raising=False)
    output = tmp_path / "out"
    args = [
        "--live",
        "--material",
        "cup.png",
        "--name",
        "cup",
        "--category",
        "kitchen",
        "--out",
        str(output),
    ]
    assert main(args) == 1
    captured = capsys.readouterr()
    assert "IMAGE_AGENT_OPENROUTER_API_KEY" in captured.err
    assert "Traceback" not in captured.err
    assert not output.exists()


def test_smoke_reports_discovery_rejection_without_suggesting_saved_images(
    tmp_path, monkeypatch, capsys
):
    import httpx

    from scripts.live_smoke import main

    original = httpx.AsyncClient
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(403, json={"error": {"message": "Model access denied"}})

    def client(**kwargs):
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setenv("IMAGE_AGENT_OPENROUTER_API_KEY", "offline-test-key")
    monkeypatch.delenv("IMAGE_AGENT_HTTP_PROXY", raising=False)
    source = tmp_path / "cup.png"
    source.write_bytes(picture())
    output = tmp_path / "out"
    assert (
        main(
            [
                "--live",
                "--material",
                str(source),
                "--name",
                "cup",
                "--category",
                "kitchen",
                "--out",
                str(output),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "403" in captured.err and "Model access denied" in captured.err
    assert "Inspect every saved image" not in captured.out
    assert len(requests) == 1 and requests[0].url.path.endswith("/chat/completions")
    manifest = next(output.glob("*/result.json"))
    saved = json.loads(manifest.read_text("utf-8"))
    assert saved["assets"] == []
    assert "Model access denied" in saved["error_info"]["message"]
