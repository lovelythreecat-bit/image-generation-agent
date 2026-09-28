import pytest

from tests.test_output import result


@pytest.mark.parametrize(
    "status,code", [("succeeded", 0), ("partial", 2), ("failed", 1), ("needs_input", 3)]
)
def test_exit_codes_and_order(tmp_path, monkeypatch, status, code, capsys):
    from image_agent import __main__ as cli

    received = []

    async def create(request, config=None, *, analysis=None):
        received.append(request)
        r = result()
        r.status = status
        return r

    monkeypatch.setattr(cli, "create_images", create)
    out = cli.run_cli(
        [
            "create",
            "--material",
            "first.png",
            "--material",
            "second.png",
            "--name",
            "cup",
            "--category",
            "kitchen",
            "--platform",
            "taobao",
            "--output",
            "main_image",
            "--out",
            str(tmp_path),
        ]
    )
    assert out == code
    assert [m.material_id for m in received[0].materials] == ["m1", "m2"]
    assert received[0].materials[1].source.path.name == "second.png"
    assert "STRUCTURED" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["create"],
        ["create", "--unknown"],
        [
            "create",
            "--product",
            "https://a/image.png",
            "--name",
            "cup",
            "--category",
            "kitchen",
            "--platform",
            "taobao",
            "--output",
            "main_image",
            "--out",
            "out",
        ],
    ],
)
def test_invalid_cli_returns_one(args):
    from image_agent.__main__ import run_cli

    assert run_cli(args) == 1


def test_ctrl_c_130(tmp_path, monkeypatch):
    from image_agent import __main__ as cli

    async def create(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "create_images", create)
    assert (
        cli.run_cli(
            [
                "create",
                "--product",
                "cup.png",
                "--name",
                "cup",
                "--category",
                "kitchen",
                "--platform",
                "taobao",
                "--output",
                "main_image",
                "--out",
                str(tmp_path),
            ]
        )
        == 130
    )


def test_json_media_root_boundary(tmp_path, monkeypatch):
    import json

    from image_agent import __main__ as cli
    from tests.test_contracts import dto_data

    request = tmp_path / "request.json"
    mapping = tmp_path / "media.json"
    request.write_text(json.dumps(dto_data()), encoding="utf-8")
    calls = []

    async def create(request, **kwargs):
        calls.append(request)
        return result()

    monkeypatch.setattr(cli, "create_images", create)
    args = [
        "create",
        "--request-json",
        str(request),
        "--media-map",
        str(mapping),
        "--media-root",
        str(tmp_path),
        "--out",
        str(tmp_path / "out"),
    ]
    for path in ["../outside.png", "C:/absolute.png", "/absolute.png"]:
        mapping.write_text(json.dumps({"upload1": path}), encoding="utf-8")
        assert cli.run_cli(args) == 1
    assert not calls
    mapping.write_text(json.dumps({"upload1": "cup.png"}), encoding="utf-8")
    assert cli.run_cli(args) == 0 and calls[0].materials[0].source.path == tmp_path / "cup.png"
    assert cli.run_cli(args + ["--material", "other.png"]) == 1
