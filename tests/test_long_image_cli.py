import asyncio
import json
from pathlib import Path

import pytest

from image_agent.config import AgentConfig
from image_agent.execution import ExecutionPolicy
from image_agent.long_image_models import LongImagePlanningResult, LongImageResult
from image_agent.long_image_store import LongImageRunStore
from tests.test_long_image_pipeline import Transport, deps_for, request_plan


def write_request(tmp_path):
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "task_type": "long_image",
                "scene": "tutorial",
                "title": "操作指南",
                "content_brief": "四个步骤",
                "image_size": "1K",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_tutorial_inline_create_real_pipeline(tmp_path, monkeypatch, capsys):
    from image_agent import long_image_runtime as runtime
    from image_agent.__main__ import run_cli

    _, plan = request_plan(slots=False)
    transport = Transport(plan)

    async def enter(self):
        return self

    async def leave(self, *args):
        return None

    monkeypatch.setattr(Transport, "__aenter__", enter, raising=False)
    monkeypatch.setattr(Transport, "__aexit__", leave, raising=False)
    monkeypatch.setattr(runtime, "HttpTransport", lambda *a, **k: transport)
    code = run_cli(
        [
            "long-image",
            "create",
            "--request-json",
            str(write_request(tmp_path)),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert code == 0
    runs = list((tmp_path / "out").glob("*/result.json"))
    assert len(runs) == 1
    saved = json.loads(runs[0].read_text("utf-8"))
    assert saved["status"] == "succeeded"
    assert saved["usage"]["execution"] == {"image_calls": 0, "vision_calls": 1}
    assert "display_total" in capsys.readouterr().out
    assert len(transport.posts) == 1


def test_verified_record_reuse_and_tampered_plan_rejected(tmp_path, capsys):
    from image_agent.__main__ import run_cli

    request, plan = request_plan(tmp_path / "planning", slots=False)
    store = LongImageRunStore(tmp_path / "planning")
    store.initialize(request, (), AgentConfig(), ExecutionPolicy())
    with store.budget():
        record = store.save_planning_result(
            LongImagePlanningResult(planning_id="planning-one", status="ready", plan=plan),
            request,
            (),
            ExecutionPolicy(),
        )
    args = [
        "long-image",
        "create",
        "--request-json",
        str(write_request(tmp_path)),
        "--out",
        str(tmp_path / "out"),
        "--plan-record",
        record.local_record_ref,
    ]
    assert run_cli(args) == 0
    assert "planning-one" in capsys.readouterr().out
    bad = tmp_path / "plan.json"
    data = plan.model_dump(mode="json")
    data["sections"][1]["text_units"][0]["text"] = "tampered"
    bad.write_text(json.dumps(data), encoding="utf-8")
    assert run_cli(args + ["--plan", str(bad)]) == 1
    store.close()


def test_local_render_without_credentials_preserves_options(tmp_path, monkeypatch):
    from image_agent.__main__ import run_cli
    from image_agent.long_image_runtime import create_long_image

    request, plan = request_plan(tmp_path / "out", slots=False)
    result = asyncio.run(create_long_image(request, AgentConfig(), plan=plan))

    def forbidden(*args, **kwargs):
        raise AssertionError("local operation loaded model configuration")

    monkeypatch.setattr(AgentConfig, "from_env", forbidden)
    monkeypatch.setattr(AgentConfig, "from_file", forbidden)
    assert (
        run_cli(
            [
                "long-image",
                "render",
                str(result.run_dir),
                "--width",
                "600",
                "--theme",
                "warm",
                "--slice-height",
                "500",
            ]
        )
        == 0
    )
    assert run_cli(["long-image", "render", str(result.run_dir)]) == 0
    saved = LongImageRunStore(result.run_dir).hydrate_result()
    assert saved.page.width == 600
    assert all(s.height <= 500 for s in saved.slices)
    assert saved.usage.execution == {"image_calls": 0, "vision_calls": 0}
    assert run_cli(["long-image", "render", str(result.run_dir), "--title", "new"]) == 1


def test_accept_requires_reason_and_retains_audit_state(tmp_path, monkeypatch, capsys):
    from image_agent.__main__ import run_cli
    from image_agent.long_image_runtime import execute_long_image

    request, plan = request_plan(tmp_path / "out")
    result = asyncio.run(
        execute_long_image(
            request,
            AgentConfig(),
            plan=plan,
            dependencies=deps_for(Transport(audits=[False])),
            policy=ExecutionPolicy(max_quality_repairs=0),
        )
    )
    candidate = result.image_assets[0].selected_candidate_id
    args = [
        "long-image",
        "accept",
        str(result.run_dir),
        "--slot",
        "illustration",
        "--candidate",
        candidate,
    ]
    assert run_cli(args) == 1
    assert run_cli(args + ["--reason", "   "]) == 1
    monkeypatch.setattr(AgentConfig, "from_env", lambda: pytest.fail("credentials loaded"))
    assert run_cli(args + ["--reason", "已核对示意图"]) == 4
    saved = LongImageRunStore(result.run_dir).hydrate_result()
    selected = saved.image_assets[0].candidates[0]
    assert selected["acceptance"]["reason"] == "已核对示意图"
    assert selected["acceptance"]["at"]
    assert saved.image_assets[0].audit.passed is False
    assert "accepted" in capsys.readouterr().out


@pytest.mark.parametrize(
    "status,code",
    [
        ("succeeded", 0),
        ("failed", 1),
        ("quality_failed", 1),
        ("needs_input", 3),
        ("accepted", 4),
        ("pending_audit", 5),
        ("audit_error", 5),
        ("budget_exhausted", 6),
        ("generation_uncertain", 7),
    ],
)
def test_all_status_exits(status, code, capsys):
    from image_agent.long_image_cli import print_long_result

    assert print_long_result(LongImageResult(status=status)) == code
    assert "unavailable" in capsys.readouterr().out


def test_output_error_precedes_success():
    from image_agent.long_image_cli import print_long_result

    assert (
        print_long_result(
            LongImageResult(
                status="succeeded",
                output_errors=[
                    {"kind": "output", "code": "output_error", "message": "save failed"}
                ],
            )
        )
        == 1
    )


@pytest.mark.parametrize(
    "value",
    [
        "../outside.png",
        "nested/../cup.png",
        "C:/absolute.png",
        "/absolute.png",
        "\\\\host\\share\\cup.png",
        "https://example.com/a.png",
    ],
)
def test_media_path_rejection(tmp_path, value):
    from image_agent.__main__ import run_cli

    path = write_request(tmp_path)
    data = json.loads(path.read_text("utf-8"))
    data["materials"] = [{"material_id": "m1", "media_id": "upload1"}]
    path.write_text(json.dumps(data), encoding="utf-8")
    mapping = tmp_path / "media.json"
    mapping.write_text(json.dumps({"upload1": value}), encoding="utf-8")
    assert (
        run_cli(
            [
                "long-image",
                "create",
                "--request-json",
                str(path),
                "--media-map",
                str(mapping),
                "--media-root",
                str(tmp_path),
                "--out",
                str(tmp_path / "out"),
            ]
        )
        == 1
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_type", "unknown"),
        ("schema_version", "9.0"),
        ("output_dir", "C:/secret"),
        ("usage", {"image_calls": 0}),
        ("planning_record", {"trusted": True}),
    ],
)
def test_request_contract_rejections(tmp_path, field, value):
    from image_agent.__main__ import run_cli

    path = write_request(tmp_path)
    data = json.loads(path.read_text("utf-8")) | {field: value}
    path.write_text(json.dumps(data), encoding="utf-8")
    assert (
        run_cli(["long-image", "create", "--request-json", str(path), "--out", str(tmp_path)]) == 1
    )


@pytest.mark.parametrize(
    "metadata",
    [
        {"version": 1, "task_type": "long_image"},
        {"version": 2, "task_type": "unknown"},
        {"version": 2},
    ],
)
def test_resume_rejects_old_or_unknown_task(tmp_path, metadata):
    from image_agent.__main__ import run_cli

    (tmp_path / "run.json").write_text(json.dumps(metadata), encoding="utf-8")
    assert run_cli(["long-image", "resume", str(tmp_path)]) == 1


def test_examples_validate():
    from image_agent.long_image_contracts import LongImageRequestDTO

    root = Path(__file__).resolve().parents[1] / "examples" / "long-image"
    examples = list(root.glob("*.json"))
    assert len(examples) == 3
    for path in examples:
        LongImageRequestDTO.model_validate_json(path.read_text("utf-8"))


def test_analyze_persists_public_plan_and_reusable_record(tmp_path, monkeypatch, capsys):
    from image_agent import long_image_planning as planning
    from image_agent.__main__ import run_cli

    _, plan = request_plan(slots=False)
    transport = Transport(plan)

    async def enter(self):
        return self

    async def leave(self, *args):
        return None

    monkeypatch.setattr(Transport, "__aenter__", enter, raising=False)
    monkeypatch.setattr(Transport, "__aexit__", leave, raising=False)
    monkeypatch.setattr(planning, "HttpTransport", lambda *a, **k: transport)
    out = tmp_path / "planning"
    args = [
        "long-image",
        "analyze",
        "--request-json",
        str(write_request(tmp_path)),
        "--out",
        str(out),
    ]
    assert run_cli(args) == 0
    data = json.loads((out / "planning.json").read_text("utf-8"))
    assert data["status"] == "ready" and data["usage"]["vision_calls"] == 1
    assert "record" not in data and data["record_id"]
    plan_data = json.loads((out / "plan.json").read_text("utf-8"))
    assert plan_data["task_type"] == "long_image"
    assert "plan_record:" in capsys.readouterr().out
    record = next(out.glob("*/records/*.json"))
    assert (
        run_cli(
            [
                "long-image",
                "create",
                "--request-json",
                str(write_request(tmp_path)),
                "--out",
                str(tmp_path / "generated"),
                "--plan",
                str(out / "plan.json"),
                "--plan-record",
                str(record),
            ]
        )
        == 0
    )
    saved = json.loads(next((tmp_path / "generated").glob("*/result.json")).read_text("utf-8"))
    assert saved["usage"]["execution"] == {"image_calls": 0, "vision_calls": 0}
    assert saved["usage"]["display_total"] == {"image_calls": 0, "vision_calls": 1}
    assert len(transport.posts) == 1


def test_source_only_media_binding_and_font_binding(tmp_path):
    from image_agent.__main__ import run_cli
    from tests.test_long_image_pipeline import png

    _, plan = request_plan(slots=True, source=True)
    path = write_request(tmp_path)
    data = json.loads(path.read_text("utf-8"))
    data["materials"] = [{"material_id": "m1", "media_id": "uploaded"}]
    data["options"] = {"font_id": "body-font"}
    path.write_text(json.dumps(data), encoding="utf-8")
    media = tmp_path / "cup.png"
    media.write_bytes(png())
    mapping = tmp_path / "media.json"
    mapping.write_text(json.dumps({"uploaded": "cup.png"}), encoding="utf-8")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan.model_dump_json(), encoding="utf-8")
    args = [
        "long-image",
        "create",
        "--request-json",
        str(path),
        "--media-map",
        str(mapping),
        "--media-root",
        str(tmp_path),
        "--plan",
        str(plan_path),
        "--out",
        str(tmp_path / "out"),
    ]
    assert run_cli(args) == 1
    assert run_cli(args + ["--font-path", "C:/Windows/Fonts/msyh.ttc"]) == 0
    saved = json.loads(next((tmp_path / "out").glob("*/result.json")).read_text("utf-8"))
    assert saved["image_assets"][0]["source_status"] == "available"
    assert saved["image_assets"][0]["audit"] is None
    assert saved["usage"]["execution"] == {"image_calls": 0, "vision_calls": 0}


@pytest.mark.parametrize("command", ["render", "resume"])
def test_malformed_run_json_returns_error(tmp_path, command):
    from image_agent.__main__ import run_cli

    (tmp_path / "run.json").write_text("[]", encoding="utf-8")
    assert run_cli(["long-image", command, str(tmp_path)]) == 1


def test_malformed_record_returns_error(tmp_path):
    from image_agent.__main__ import run_cli

    request, _ = request_plan(slots=False)
    store = LongImageRunStore(tmp_path / "planning")
    store.initialize(request, (), AgentConfig(), ExecutionPolicy())
    records = tmp_path / "planning" / "records"
    records.mkdir()
    (records / "record-one.json").write_text("[]", encoding="utf-8")
    assert (
        run_cli(
            [
                "long-image",
                "create",
                "--request-json",
                str(write_request(tmp_path)),
                "--out",
                str(tmp_path / "out"),
                "--plan-record",
                str(records / "record-one.json"),
            ]
        )
        == 1
    )


def test_product_record_requires_matching_analysis_snapshot(tmp_path):
    from image_agent.__main__ import run_cli
    from image_agent.long_image_product import prepare_long_product
    from image_agent.models import MaterialAnalysis
    from tests.fakes import FakeVision
    from tests.test_long_image_product import inputs, product_plan

    request, materials = inputs("明确使用两段介绍商品")
    product = asyncio.run(
        prepare_long_product(request, materials, analysis=None, vision=FakeVision())
    )
    plan = product_plan(request, materials, product)
    plan.image_slots = [
        plan.image_slots[0].model_copy(
            update={"source": "source", "prompt": "", "aspect_ratio": None, "material_ids": ["m1"]}
        )
    ]
    plan.sections[1].text_units = [{"unit_id": "closing", "kind": "body", "text": "商品介绍"}]
    store = LongImageRunStore(tmp_path / "planning")
    store.initialize(request, materials, AgentConfig(), ExecutionPolicy())
    with store.budget():
        record = store.save_planning_result(
            LongImagePlanningResult(
                planning_id="product-plan", status="ready", plan=plan, analysis=product.analysis
            ),
            request,
            materials,
            ExecutionPolicy(),
        )
    path = tmp_path / "product.json"
    data = request.model_dump(mode="json", exclude={"output_dir", "materials"})
    data["options"].pop("font_path")
    data["materials"] = [{"material_id": "m1", "media_id": "photo"}]
    path.write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / "cup.png").write_bytes(materials[0].data)
    mapping = tmp_path / "media.json"
    mapping.write_text('{"photo": "cup.png"}', encoding="utf-8")
    args = [
        "long-image",
        "create",
        "--request-json",
        str(path),
        "--media-map",
        str(mapping),
        "--media-root",
        str(tmp_path),
        "--out",
        str(tmp_path / "out"),
        "--plan-record",
        record.local_record_ref,
    ]
    assert run_cli(args) == 1
    snapshot = tmp_path / "analysis.json"
    modified = product.analysis.model_dump(mode="json")
    modified["facts"][0]["description"] = "changed cup description"
    snapshot.write_text(
        MaterialAnalysis.model_validate(modified).model_dump_json(), encoding="utf-8"
    )
    assert run_cli(args + ["--analysis", str(snapshot)]) == 1
    snapshot.write_text(product.analysis.model_dump_json(), encoding="utf-8")
    assert run_cli(args + ["--analysis", str(snapshot)]) == 0
    saved = json.loads(next((tmp_path / "out").glob("*/result.json")).read_text("utf-8"))
    assert saved["usage"]["execution"] == {"image_calls": 0, "vision_calls": 0}
    assert saved["usage"]["linked_planning"] == {
        "product-plan": {"image_calls": 0, "vision_calls": 0}
    }
    # The actual file binding must still agree with the verified plan's original material hash.
    (tmp_path / "cup.png").write_bytes(b"broken image")
    assert run_cli(args + ["--analysis", str(snapshot)]) == 1


def test_explicit_product_selection_requires_snapshot(tmp_path, capsys):
    from image_agent.__main__ import run_cli

    path = write_request(tmp_path)
    data = json.loads(path.read_text("utf-8")) | {
        "scene": "product_detail",
        "product_context": {"product_name": "cup", "category": "home"},
        "materials": [{"material_id": "m1", "media_id": "upload"}],
        "selection": {"mode": "explicit", "subject_ids": ["s1"]},
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    mapping = tmp_path / "media.json"
    mapping.write_text('{"upload": "cup.png"}', encoding="utf-8")
    assert (
        run_cli(
            [
                "long-image",
                "create",
                "--request-json",
                str(path),
                "--media-map",
                str(mapping),
                "--media-root",
                str(tmp_path),
                "--out",
                str(tmp_path / "out"),
            ]
        )
        == 1
    )
    assert "explicit selection requires --analysis" in capsys.readouterr().err


def test_planning_summary_shows_short_safe_section_text(capsys):
    from image_agent.long_image_cli import print_long_result

    _, plan = request_plan(slots=False)
    plan.sections[1].text_units[0].text = (
        "API key=sk-secret123 data:image/png;base64,abc " + "长文" * 200
    )
    print_long_result(LongImageResult(status="succeeded", plan=plan))
    output = capsys.readouterr().out
    assert "操作指南" in output
    assert "sk-secret123" not in output and "data:image" not in output
    assert "长文" * 80 not in output
