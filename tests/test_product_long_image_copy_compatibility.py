"""Provider ingestion must retain source-bound copy and failed planning evidence."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from image_agent.config import AgentConfig
from image_agent.errors import OutputError, ProviderError
from image_agent.product_long_image import (
    ProductLongImageWorkflow,
    validate_product_long_image_plan,
)
from image_agent.product_long_image_models import ProductLongImagePlan, ProductLongImageSection
from image_agent.run_store import RunStore
from image_agent.vision import VisionClient, _parse_vision_response
from tests.fakes import run_reviewed_pipeline as run_pipeline
from tests.test_product_long_image_pipeline import long_setup
from tests.test_product_long_image_planning import authored_plan, planning_context
from tests.test_vision import ScriptTransport
from tests.test_vision_resilience import RawTransport


def provider_response(plan):
    return {"choices": [{"message": {"content": json.dumps(plan, ensure_ascii=False)}}]}


@pytest.mark.parametrize("field", ["copy", "copy_items"])
async def test_provider_parser_keeps_title_and_source_bound_copy(request_data, field):
    request, target, context = await planning_context(request_data)
    payload = authored_plan(request, target, context).model_dump(mode="json")
    for section in payload["sections"]:
        section["title"] = "white ceramic cup"
        if field == "copy":
            section["copy"] = section.pop("copy_items")
    parsed = _parse_vision_response(provider_response(payload), ProductLongImagePlan)
    validated = validate_product_long_image_plan(request, target, context, parsed)
    assert validated.sections[0].copy_items[0].text == "white ceramic cup"
    assert validated.sections[0].copy_items[0].source_quote == "white ceramic cup"
    assert validated.sections[0].copy_items[1].text == "  超强99.9%\n原文  "


async def test_canonical_copy_wins_when_both_fields_are_supplied(request_data):
    request, target, context = await planning_context(request_data)
    payload = authored_plan(request, target, context).model_dump(mode="json")
    payload["sections"][0]["copy"] = [{"text": "invalid legacy draft"}]
    section = ProductLongImageSection.model_validate(payload["sections"][0])
    parsed = _parse_vision_response(provider_response(payload), ProductLongImagePlan)
    assert section.copy_items[0].text == "white ceramic cup"
    assert parsed.sections[0].copy_items == section.copy_items


async def test_independent_title_does_not_trigger_a_correction(request_data):
    request, target, context = await planning_context(request_data)
    valid = authored_plan(request, target, context).model_dump(mode="json")
    valid["sections"][0]["title"] = "white ceramic cup"
    invalid = deepcopy(valid)
    invalid["sections"][0]["title"] = "白色陶瓷杯"
    transport = ScriptTransport([invalid, valid])
    result = await VisionClient(transport, AgentConfig()).plan_product_long_image(
        request, target, context
    )
    assert len(transport.calls) == 1
    assert result.sections[0].title == "白色陶瓷杯"


@pytest.mark.parametrize("damage", ["title_whitespace", "forged_source"])
async def test_title_and_source_notes_are_not_extra_generation_gates(request_data, damage):
    request, target, context = await planning_context(request_data)
    plan = authored_plan(request, target, context)
    plan.sections[0].title = "white ceramic cup"
    if damage == "title_whitespace":
        plan.sections[0].title += " "
    else:
        plan.sections[0].copy_items[0].source_quote = "invented certification"
    result = validate_product_long_image_plan(request, target, context, plan)
    assert result.sections[0].title == plan.sections[0].title
    assert result.sections[0].copy_items[0].text == "white ceramic cup"


async def test_planner_prompt_uses_canonical_copy_items(request_data):
    request, target, context = await planning_context(request_data)
    transport = ScriptTransport([authored_plan(request, target, context).model_dump(mode="json")])
    await VisionClient(transport, AgentConfig()).plan_product_long_image(request, target, context)
    instruction = transport.calls[0][1]["messages"][0]["content"][0]["text"].split(
        "Return one JSON object", 1
    )[0]
    assert "copy_items" in instruction and "exact" in instruction
    assert "also appear in copy." not in instruction


async def planning_workflow(request_data, store, transport, *, attempts=2):
    request, target, context = await planning_context(request_data)
    vision = VisionClient(transport, AgentConfig(model_completion_attempts=attempts))
    nodes = SimpleNamespace(
        request=request,
        target=lambda state: target,
        context=lambda state: context,
        asset=lambda state: SimpleNamespace(asset_id="taobao.long_image.default"),
        dependencies=SimpleNamespace(vision=vision),
        store=store,
    )
    return ProductLongImageWorkflow(nodes), request, target, context


async def test_failed_attempt_diagnostics_survive_retry_and_final_failure(request_data, tmp_path):
    store = RunStore(tmp_path)
    request, target, context = await planning_context(request_data)
    invalid = authored_plan(request, target, context).model_dump(mode="json")
    invalid["sections"][0]["title"] = "missing title"
    invalid["sections"].pop()
    transport = RawTransport([{"content": "{bad json"}, {"content": json.dumps(invalid)}])
    workflow, _, _, _ = await planning_workflow(request_data, store, transport)
    with pytest.raises(ProviderError, match="attempt=2/2"):
        await workflow.plan({})
    paths = sorted(tmp_path.glob("diagnostics/vision/**/*.json"))
    assert len(paths) == 2
    first, last = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    assert first["stage"] == last["stage"] == "plan_product_long_image"
    assert [first["attempt"], last["attempt"]] == [1, 2]
    assert first["response_content"] == "{bad json" and first["parsed_plan"] is None
    assert "invalid JSON" in first["validation_problem"]
    assert last["parsed_plan"]["sections"][0]["title"] == "missing title"
    assert last["parsed_plan"]["sections"][0]["copy_items"][1]["text"] == "  超强99.9%\n原文  "
    assert "expected 6 sections, received 5" in last["validation_problem"]
    assert last["asset_id"] == "taobao.long_image.default"
    # A later planning call must retain the earlier attempt evidence.
    transport.messages = [{"content": "bad again"}] * 2
    with pytest.raises(ProviderError):
        await workflow.plan({})
    assert len(list(tmp_path.glob("diagnostics/vision/**/*.json"))) == 4


async def test_diagnostics_do_not_persist_credentials_or_inline_images(request_data, tmp_path):
    store = RunStore(tmp_path)
    transport = RawTransport(
        [
            {
                "content": '{bad "Authorization": "Bearer secret-credential", data:image/png;base64,c2VjcmV0'
            }
        ]
    )
    workflow, _, _, _ = await planning_workflow(request_data, store, transport, attempts=1)
    with pytest.raises(ProviderError):
        await workflow.plan({})
    files = list(tmp_path.glob("diagnostics/vision/**/*.json"))
    assert len(files) == 1
    saved = files[0].read_text(encoding="utf-8")
    assert "secret-credential" not in saved and "c2VjcmV0" not in saved
    assert "data:image" not in saved


async def test_title_validation_diagnostic_redacts_credentials_in_problem(request_data, tmp_path):
    store = RunStore(tmp_path)
    request, target, context = await planning_context(request_data)
    invalid = authored_plan(request, target, context).model_dump(mode="json")
    invalid["sections"][0]["title"] = "password=reviewer-fake-password"
    invalid["sections"].pop()
    workflow, _, _, _ = await planning_workflow(
        request_data, store, ScriptTransport([invalid]), attempts=1
    )
    with pytest.raises(ProviderError, match="expected 6 sections"):
        await workflow.plan({})
    paths = list(tmp_path.glob("diagnostics/vision/**/*.json"))
    assert len(paths) == 1
    saved = json.loads(paths[0].read_text(encoding="utf-8"))
    assert saved["parsed_plan"] is not None
    assert "expected 6 sections" in saved["validation_problem"]
    assert "reviewer-fake-password" not in saved["response_content"]
    assert "reviewer-fake-password" not in saved["parsed_plan"]["sections"][0]["title"]
    assert "reviewer-fake-password" not in saved["validation_problem"]


async def test_diagnostic_write_failure_keeps_primary_provider_error(request_data, caplog):
    class BrokenDiagnosticStore(RunStore):
        def write(self, relative, data):
            if relative.startswith("diagnostics/"):
                raise OutputError("diagnostic disk is unavailable")
            return super().write(relative, data)

    store = BrokenDiagnosticStore()
    transport = RawTransport([{"content": "bad json"}])
    workflow, _, _, _ = await planning_workflow(request_data, store, transport, attempts=1)
    with pytest.raises(ProviderError, match="invalid JSON"):
        await workflow.plan({})
    assert "Planning diagnostic persistence failed (OutputError)" in caplog.text


async def test_in_memory_store_retains_failed_planning_evidence(request_data):
    store = RunStore()
    workflow, _, _, _ = await planning_workflow(
        request_data, store, RawTransport([{"content": "bad json"}]), attempts=1
    )
    with pytest.raises(ProviderError):
        await workflow.plan({})
    assert len([name for name in store.memory if name.startswith("diagnostics/")]) == 1


async def test_pipeline_planning_failure_keeps_diagnostics_without_generating_images(
    request_data, tmp_path
):
    request, dependencies, vision, generator = long_setup(request_data)
    transport = RawTransport([{"content": "{broken plan"}] * 2)
    config = AgentConfig(model_completion_attempts=2)
    vision.plan_product_long_image = VisionClient(transport, config).plan_product_long_image
    result = await run_pipeline(
        request, config, dependencies=dependencies, store=RunStore(tmp_path)
    )
    assert result.status == "failed"
    assert not generator.calls
    assert "plan_product_long_image" in result.assets[0].error_info.message
    paths = sorted(tmp_path.glob("diagnostics/vision/**/*.json"))
    assert len(paths) == 2
    assert [json.loads(path.read_text(encoding="utf-8"))["attempt"] for path in paths] == [1, 2]
