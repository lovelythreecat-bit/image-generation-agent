import base64
import io
import json
import re

import pytest
from PIL import Image

from image_agent.config import AgentConfig
from image_agent.execution import ExecutionPolicy, reserve_post
from image_agent.long_image_models import LongImagePlan, LongImageRequest, LongSlotAsset
from image_agent.long_image_planning import long_image_dependencies
from image_agent.long_image_requirements import planning_fingerprint
from image_agent.run_store import digest
from tests.test_long_image_planning import plan_for


def png(size=768):
    out = io.BytesIO()
    Image.new("RGB", (size, size), "#c5d1dd").save(out, "PNG")
    return out.getvalue()


def request_plan(tmp_path=None, *, slots=True, source=False):
    request = LongImageRequest(
        scene="tutorial",
        title="操作指南",
        content_brief="四个步骤",
        image_size="1K",
        output_dir=tmp_path,
        materials=[{"material_id": "m1", "source": {"data": png()}}] if source else [],
    )
    data = plan_for(request, slots=slots)
    # Actual visible content in every section; empty blocks are layout errors.
    for i, section in enumerate(data["sections"][1:], 1):
        section["text_units"] = [{"unit_id": f"body-{i}", "kind": "body", "text": f"步骤 {i}"}]
    if source:
        data["image_slots"][0].update(
            source="source", prompt="", aspect_ratio=None, material_ids=["m1"]
        )
        data["input_fingerprint"] = planning_fingerprint(request, {"m1": digest(png())})
    return request, LongImagePlan.model_validate(data)


def audit_payload(passed=True):
    return {
        "checks": [
            {
                "kind": kind,
                "passed": passed,
                "reason": "通过" if passed else "需调整",
                "severity": "info" if passed else "critical",
            }
            for kind in ("purpose", "subject", "unrequested_text", "style")
        ],
        "passed": passed,
        "repairable": not passed,
        "repair_prompt": None if passed else "修正配图构图",
    }


class Transport:
    client = None

    def __init__(self, plan=None, audits=None, image=None):
        self.plan, self.audits, self.image = plan, list(audits or [True]), image or png()
        self.posts = []

    async def post_json(self, path, payload):
        reserve_post()
        self.posts.append((path, payload))
        if path == "/images":
            return {"data": [{"b64_json": base64.b64encode(self.image).decode()}]}
        text = json.dumps(payload, ensure_ascii=False)
        if "plan_long_image" in text or (self.plan is not None and len(self.posts) == 1):
            value = {"status": "ready", "plan": self.plan.model_dump(mode="json")}
        else:
            flag = self.audits.pop(0) if len(self.audits) > 1 else self.audits[0]
            if isinstance(flag, Exception):
                raise flag
            value = audit_payload(flag)
            instruction = payload["messages"][0]["content"][0]["text"]
            value["slot_id"] = re.search(r'"slot_id": "([^"]+)"', instruction)[1]
            value["candidate_id"] = re.search(r'"candidate_id": "([^"]+)"', instruction)[1]
        return {"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]}


def deps_for(transport):
    return long_image_dependencies(transport, AgentConfig())


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [False, True])
async def test_zero_slots_and_source_only_success(tmp_path, source):
    from image_agent.long_image_runtime import create_long_image

    request, plan = request_plan(tmp_path, slots=source, source=source)
    result = await create_long_image(request, AgentConfig(), plan=plan)
    assert result.status == "succeeded"
    assert result.page.availability == "current"
    assert result.usage.execution == {"image_calls": 0, "vision_calls": 0}
    if source:
        assert result.image_assets[0].status is None
        assert result.image_assets[0].audit is None


@pytest.mark.asyncio
async def test_inline_and_saved_plan_generation_counts(tmp_path):
    from image_agent.long_image_runtime import execute_long_image

    request, plan = request_plan(tmp_path)
    transport = Transport(plan)
    result = await execute_long_image(request, AgentConfig(), dependencies=deps_for(transport))
    assert result.status == "succeeded"
    assert len(transport.posts) == 3
    assert result.usage.execution == {"image_calls": 1, "vision_calls": 2}
    assert result.usage.linked_planning == {}
    assert result.usage.display_total == result.usage.execution


def test_aggregation_order_and_memory_section_statuses():
    from image_agent.long_image_runtime import aggregate_long_image, render_long_image_memory

    _, plan = request_plan()
    asset = LongSlotAsset(slot_id="illustration", source="generate", image=png(), status="accepted")
    result = render_long_image_memory(plan, {asset.slot_id: asset})
    assert result.status == "accepted"
    assert [s.status for s in result.sections] == [
        "succeeded",
        "accepted",
        "succeeded",
        "succeeded",
    ]
    assert all(s.status == "accepted" for s in result.slices)
    assert (
        aggregate_long_image(
            plan, {asset.slot_id: asset}, checks=[{"passed": False}], output_errors=[]
        )
        == "failed"
    )


@pytest.mark.parametrize(
    "status", ["generation_uncertain", "budget_exhausted", "audit_error", "pending_audit"]
)
def test_memory_missing_bytes_retains_candidate_severity(status):
    from image_agent.long_image_runtime import render_long_image_memory

    _, plan = request_plan()
    asset = LongSlotAsset(slot_id="illustration", source="generate", status=status)
    result = render_long_image_memory(plan, {asset.slot_id: asset})
    assert result.status == status
    assert result.page is None


@pytest.mark.asyncio
async def test_shared_slot_is_generated_once(tmp_path):
    from image_agent.long_image_runtime import execute_long_image

    request, plan = request_plan(tmp_path)
    plan.sections[2].slot_ids = ["illustration"]
    plan.image_slots[0].section_ids.append("section-2")
    transport = Transport()
    result = await execute_long_image(
        request, AgentConfig(), plan=plan, dependencies=deps_for(transport)
    )
    assert result.status == "succeeded"
    assert len(result.image_assets) == 1
    assert result.sections[1].dependencies == result.sections[2].dependencies == ["illustration"]
    assert len(transport.posts) == 2


@pytest.mark.asyncio
async def test_repair_posts_are_bounded(tmp_path):
    from image_agent.long_image_runtime import execute_long_image

    request, plan = request_plan(tmp_path)
    transport = Transport(audits=[False, True])
    result = await execute_long_image(
        request,
        AgentConfig(),
        plan=plan,
        dependencies=deps_for(transport),
        policy=ExecutionPolicy(max_quality_repairs=1),
    )
    assert result.status == "succeeded"
    assert result.usage.execution == {"image_calls": 2, "vision_calls": 2}
    assert len(transport.posts) == 4
    assert len(result.image_assets[0].candidates) == 2


@pytest.mark.asyncio
async def test_technical_failure_prevents_paid_audit(tmp_path):
    from image_agent.long_image_runtime import execute_long_image

    request, plan = request_plan(tmp_path)
    transport = Transport(image=png(64))
    result = await execute_long_image(
        request,
        AgentConfig(),
        plan=plan,
        dependencies=deps_for(transport),
        policy=ExecutionPolicy(max_quality_repairs=0),
    )
    assert result.status == "quality_failed"
    assert len(transport.posts) == 1
    assert result.usage.execution == {"image_calls": 1, "vision_calls": 0}


def test_public_long_image_symbols_preserve_legacy():
    import image_agent

    for name in (
        "create_images",
        "resume_images",
        "CreationRequest",
        "create_long_image",
        "resume_long_image",
        "accept_long_image_candidate",
        "render_long_image",
        "render_long_image_memory",
        "analyze_long_image",
        "LongImageRequest",
        "LongImageResult",
        "LongImagePlan",
        "LongImageOptions",
        "LongPlanningRecord",
        "LongImageResultDTO",
        "LongImageRequestDTO",
        "LongImagePlanDTO",
        "LongImagePlanningResultDTO",
        "long_result_to_bundle",
    ):
        assert hasattr(image_agent, name), name


@pytest.mark.asyncio
async def test_trusted_plan_usage_is_linked_once_on_resume_and_export(tmp_path):
    from image_agent.long_image_contracts import long_result_to_bundle
    from image_agent.long_image_planning import prepare_long_plan
    from image_agent.long_image_runtime import (
        execute_long_image,
        render_long_image,
        resume_long_image,
    )
    from image_agent.long_image_store import LongImageRunStore

    request, plan = request_plan(tmp_path)
    planning_store = LongImageRunStore()
    planning_store.initialize(request, (), AgentConfig(), ExecutionPolicy())
    planning_transport = Transport(plan)
    with planning_store.budget() as ledger:
        planning = await prepare_long_plan(
            request, AgentConfig(), deps_for(planning_transport), planning_store, ledger
        )
    planning_store.close()
    execution_transport = Transport()
    result = await execute_long_image(
        request,
        AgentConfig(),
        plan=planning.plan,
        planning_record=planning.record,
        dependencies=deps_for(execution_transport),
    )
    assert result.status == "succeeded"
    assert len(planning_transport.posts) == 1
    assert len(execution_transport.posts) == 2
    assert result.usage.execution == {"image_calls": 1, "vision_calls": 1}
    assert result.usage.linked_planning == {
        planning.planning_id: {"image_calls": 0, "vision_calls": 1}
    }
    assert result.usage.display_total == {"image_calls": 1, "vision_calls": 2}
    resumed = await resume_long_image(result.run_dir)
    rendered = await render_long_image(result.run_dir)
    assert rendered.usage == resumed.usage == result.usage
    assert long_result_to_bundle(rendered).dto.usage.display_total == result.usage.display_total


@pytest.mark.asyncio
async def test_actual_image_budget_prevents_post(tmp_path):
    from image_agent.long_image_runtime import execute_long_image

    request, plan = request_plan(tmp_path)
    transport = Transport()
    result = await execute_long_image(
        request,
        AgentConfig(),
        plan=plan,
        dependencies=deps_for(transport),
        policy=ExecutionPolicy(max_image_calls=0),
    )
    assert result.status == "budget_exhausted"
    assert transport.posts == []
    assert result.usage.execution == {"image_calls": 0, "vision_calls": 0}


@pytest.mark.asyncio
async def test_product_and_generic_audits_are_separate(tmp_path):
    from image_agent.long_image_models import LongImagePlanningResult
    from image_agent.long_image_product import compile_long_product_slot, prepare_long_product
    from image_agent.long_image_runtime import execute_long_image
    from image_agent.long_image_store import LongImageRunStore
    from tests.fakes import FakeVision
    from tests.test_long_image_product import inputs, product_plan

    request, materials = inputs()
    request = request.model_copy(
        update={"image_size": "1K", "output_dir": tmp_path, "content_brief": "两段"}
    )
    product = await prepare_long_product(request, materials, analysis=None, vision=FakeVision())
    plan = product_plan(request, materials, product)
    from image_agent.long_image_models import LongImageTextUnit

    plan.sections[1].text_units = [
        LongImageTextUnit(unit_id="end-copy", kind="body", text="使用说明")
    ]
    planning_store = LongImageRunStore()
    planning_store.initialize(request, materials, AgentConfig(), ExecutionPolicy())
    planning = LongImagePlanningResult(
        planning_id="product-planning", status="ready", plan=plan, analysis=product.analysis
    )
    record = planning_store.save_planning_result(planning, request, materials, ExecutionPolicy())
    prepared = compile_long_product_slot(request, plan.image_slots[0], plan, product)
    audit = await FakeVision().audit_image(
        prepared.evidence_request, prepared.target, prepared.context, prepared.element_plan, png()
    )

    class ProductTransport(Transport):
        async def post_json(self, path, payload):
            if path == "/images":
                return await super().post_json(path, payload)
            reserve_post()
            self.posts.append((path, payload))
            return {"choices": [{"message": {"content": audit.model_dump_json()}}]}

    transport = ProductTransport()
    result = await execute_long_image(
        request,
        AgentConfig(),
        plan=plan,
        analysis=product.analysis,
        planning_record=record,
        dependencies=deps_for(transport),
    )
    assert result.status == "succeeded", (
        result.error_info,
        result.output_errors,
        [a.error_info for a in result.image_assets],
    )
    assert result.image_assets[0].audit.deterministic_checks.passed
    assert "fact_checks" not in result.image_assets[0].audit.model_dump()
    assert len(transport.posts) == 2
    assert "expected_check_ids" in json.dumps(transport.posts[1][1])
    assert result.usage.execution == {"image_calls": 1, "vision_calls": 1}
    planning_store.close()


@pytest.mark.asyncio
async def test_public_http_retries_and_correction_count_actual_posts(tmp_path, monkeypatch):
    import httpx

    import image_agent.long_image_runtime as runtime
    from image_agent.transport import HttpTransport

    request, plan = request_plan(tmp_path)
    calls = []
    audit = audit_payload() | {"slot_id": "illustration", "candidate_id": "c0001"}

    def response(http_request):
        payload = json.loads(http_request.content)
        calls.append(payload)
        if http_request.url.path.endswith("/images"):
            return httpx.Response(
                200, json={"data": [{"b64_json": base64.b64encode(png()).decode()}]}
            )
        if len(calls) == 2:
            return httpx.Response(503, json={"error": "retry"})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "broken" if len(calls) == 3 else json.dumps(audit)}}
                ]
            },
        )

    async def no_sleep(delay):
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        monkeypatch.setattr(
            runtime,
            "HttpTransport",
            lambda config, **kw: HttpTransport(config, client=client, sleep=no_sleep, **kw),
        )
        result = await runtime.create_long_image(request, AgentConfig(), plan=plan)
    assert result.status == "succeeded"
    assert len(calls) == 4
    assert result.usage.execution == {"image_calls": 1, "vision_calls": 3}


@pytest.mark.asyncio
async def test_zero_generated_inline_plan_needs_only_planning_credentials(tmp_path, monkeypatch):
    import httpx

    from image_agent.long_image_runtime import create_long_image

    request, plan = request_plan(tmp_path, slots=False)
    config = AgentConfig.from_mapping(
        {
            "services": {
                "plan": {
                    "base_url": "https://plan.invalid",
                    "protocol": "chat_completions",
                    "api_key": "offline",
                },
                "audit": {"base_url": "https://audit.invalid", "protocol": "chat_completions"},
                "images": {"base_url": "https://images.invalid", "protocol": "openai_images"},
            },
            "image_models": {"pro": {"service": "images", "model": "image"}},
            "stages": {
                "vision": {"service": "plan", "model": "planner"},
                "audit_long_image_asset": {"service": "audit", "model": "auditor"},
            },
        }
    )
    posts = []

    def handler(req):
        posts.append(req)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"status": "ready", "plan": plan.model_dump(mode="json")},
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    result = await create_long_image(request, config)
    assert result.status == "succeeded"
    assert len(posts) == 1
    assert posts[0].url.host == "plan.invalid"
    assert result.usage.execution == {"image_calls": 0, "vision_calls": 1}
