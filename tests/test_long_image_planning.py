import io
import json
import re
from dataclasses import dataclass

import pytest
from PIL import Image

from image_agent.config import AgentConfig
from image_agent.errors import AgentError, ProviderError
from image_agent.execution import ExecutionPolicy, call_scope, reserve_post
from image_agent.long_image_models import LongImagePlan, LongImageRequest
from image_agent.long_image_requirements import collect_text_requirements, planning_fingerprint
from image_agent.long_image_store import LongImageRunStore


def png():
    out = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(out, "PNG")
    return out.getvalue()


def plan_for(request, *, count=4, slots=False):
    requirements = collect_text_requirements(request)
    sections = [
        dict(section_id=f"section-{i}", role="hero" if i == 0 else "bullets") for i in range(count)
    ]
    sections[0]["text_units"] = [
        dict(
            unit_id=f"u-{i}-{j}",
            kind="heading" if i == 0 else "body",
            text=r.text,
            source=r.source.model_dump(),
            requirement_ids=[r.requirement_id],
        )
        for i, r in enumerate(requirements)
        for j in range(r.occurrences)
    ]
    data = dict(
        input_fingerprint=planning_fingerprint(request, {}),
        scene=request.scene,
        requirements=[r.model_dump() for r in requirements],
        sections=sections,
    )
    if slots:
        data["image_slots"] = [
            dict(
                slot_id="illustration",
                source="generate",
                role="steps",
                prompt="操作示意图",
                aspect_ratio="1:1",
                section_ids=["section-1"],
            )
        ]
        sections[1]["slot_ids"] = ["illustration"]
    return data


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []

    async def post_json(self, path, payload):
        reserve_post()
        self.payloads.append(payload)
        value = self.responses.pop(0)
        if callable(value):
            value = value(payload)
        if isinstance(value, str):
            content = value
        else:
            content = json.dumps(value, ensure_ascii=False)
        return {"choices": [{"message": {"content": content}}]}


@dataclass
class Deps:
    vision: object

    async def load(self, source):
        return source.data


async def prepared(request, responses, *, policy=None, plan=None, analysis=None, record=None):
    from image_agent.long_image_planning import prepare_long_plan
    from image_agent.long_image_vision import LongImageVisionClient

    config = AgentConfig()
    transport = Transport(responses)
    deps = Deps(LongImageVisionClient(transport, config))
    from image_agent.pipeline import _load

    materials = await _load(request, deps)
    store = LongImageRunStore()
    policy = policy or ExecutionPolicy()
    store.initialize(request, materials, config, policy)
    with store.budget() as ledger:
        result = await prepare_long_plan(
            request,
            config,
            deps,
            store,
            ledger,
            plan=plan,
            analysis=analysis,
            planning_record=record,
        )
        usage = ledger.usage()
    return result, usage, transport, store


@pytest.mark.asyncio
@pytest.mark.parametrize("scene", ["brand_intro", "promotion", "tutorial", "comparison", "custom"])
async def test_nonproduct_scene_plans_without_materials(scene):
    request = LongImageRequest(scene=scene, title="说明", content_brief="介绍内容")
    result, usage, transport, _ = await prepared(
        request, [{"status": "ready", "plan": plan_for(request)}]
    )
    assert result.status == "ready"
    assert result.plan.scene == scene
    assert len(result.plan.sections) == 4
    assert result.plan.image_slots == []
    assert usage == {"image_calls": 0, "vision_calls": 1}
    assert len(transport.payloads) == 1


@pytest.mark.asyncio
async def test_required_date_missing_yields_field_issue():
    request = LongImageRequest(
        scene="promotion", title="活动", content_brief="活动宣传，需展示活动日期，尚未确定"
    )
    result, usage, transport, _ = await prepared(request, [])
    assert result.status == "needs_input"
    assert result.plan is None
    assert result.issues[0].field == "content_brief"
    assert result.issues[0].code == "missing_activity_date"
    assert usage["vision_calls"] == 0
    assert transport.payloads == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"required_copy": [{"requirement_id": "date", "text": "2026年10月12日"}]},
        {"title": "2026年10月12日活动"},
        {"content_brief": "必须展示活动日期；2026-10-12 开售。"},
        {"content_brief": "仅宣传活动亮点，不需要展示活动日期。"},
        {"content_brief": "活动日期不是本次宣传内容，只介绍亮点。"},
        {"content_brief": "介绍活动时间的安排方法。"},
        {"content_brief": "Do not display the event date; focus on highlights."},
        {
            "content_brief": "标题尚未确定；必须展示活动日期，使用原文栏已给日期。",
            "required_copy": [{"requirement_id": "date", "text": "2026年10月12日"}],
        },
    ],
)
async def test_public_promotion_analyze_uses_supplied_date_or_optional_date(monkeypatch, changes):
    import httpx

    from image_agent import analyze_long_image

    request = LongImageRequest.model_validate(
        {
            "scene": "promotion",
            "title": "活动",
            "content_brief": "必须展示活动日期；使用原文栏提供的日期。",
        }
        | changes
    )
    posts = []

    def respond(http_request):
        posts.append(http_request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"status": "ready", "plan": plan_for(request)}, ensure_ascii=False
                            )
                        }
                    }
                ]
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw)
    )
    result = await analyze_long_image(request, AgentConfig(openrouter_api_key="offline"))
    assert result.status == "ready"
    assert result.issues == []
    assert result.usage == {"vision_calls": 1, "image_calls": 0}
    assert len(posts) == 1
    if request.required_copy:
        assert result.plan.sections[0].text_units[-1].text == "2026年10月12日"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "brief,has_stamp",
    [
        ("必须展示活动日期，尚未确定。", True),
        ("不需要展示开售日期，但必须展示活动日期，待补充。", True),
        ("活动宣传，需展示活动日期。", False),
        ("Must display the event date, TBD.", True),
    ],
)
async def test_public_required_unknown_date_has_no_planning_or_image_posts(
    monkeypatch, brief, has_stamp
):
    import httpx

    from image_agent import analyze_long_image, create_long_image

    def forbidden_post(http_request):
        pytest.fail("missing required date must not submit any HTTP request")

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(forbidden_post), **kw),
    )
    request = LongImageRequest(
        scene="promotion",
        title="活动",
        content_brief=brief,
        required_copy=[{"requirement_id": "stamp", "text": "原样保留：１０月１２日，09:30"}]
        if has_stamp
        else [],
    )
    planning = await analyze_long_image(request, AgentConfig())
    result = await create_long_image(request, AgentConfig())
    assert planning.status == result.status == "needs_input"
    assert planning.plan is result.plan is None
    assert planning.issues[0].code == result.issues[0].code == "missing_activity_date"
    assert planning.usage == result.usage.execution == {"vision_calls": 0, "image_calls": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["corrected", "invalid_model", "direct"])
async def test_public_create_isolates_global_copy_before_actual_image_payload(monkeypatch, mode):
    import base64

    import httpx

    from image_agent import create_long_image
    from tests.test_long_image_pipeline import audit_payload
    from tests.test_long_image_pipeline import png as slot_png

    request = LongImageRequest(
        scene="tutorial",
        title="教程",
        content_brief="操作指南",
        required_copy=[{"requirement_id": "date", "text": "10 月 12 日开售"}],
        image_size="1K",
    )
    good = plan_for(request, slots=True)
    good["visual_direction"] = "黑金风格"
    for index, section in enumerate(good["sections"][1:], 1):
        section["text_units"] = [
            {"unit_id": f"body-{index}", "kind": "body", "text": f"步骤 {index}"}
        ]
    bad = good | {"visual_direction": "黑金风格；每张配图写出10 月 12 日开售"}
    config = AgentConfig.from_mapping(
        {
            "services": {
                "vision": {
                    "base_url": "https://vision.invalid",
                    "protocol": "chat_completions",
                    "api_key": "offline",
                },
                "images": {
                    "base_url": "https://images.invalid",
                    "protocol": "openai_images",
                    "api_key": "offline",
                },
            },
            "image_models": {"pro": {"service": "images", "model": "image"}},
            "stages": {"vision": {"service": "vision", "model": "planner"}},
        }
    )
    plans, image_prompts, vision_posts = [], [], []

    def respond(http_request):
        payload = json.loads(http_request.content)
        if http_request.url.host == "images.invalid":
            image_prompts.append(payload["prompt"])
            return httpx.Response(
                200, json={"data": [{"b64_json": base64.b64encode(slot_png()).decode()}]}
            )
        vision_posts.append(payload)
        instruction = payload["messages"][0]["content"][0]["text"]
        if "Audit ONLY" in instruction:
            value = audit_payload()
            value["slot_id"] = "illustration"
            value["candidate_id"] = re.search(r'"candidate_id": "([^"]+)"', instruction)[1]
        else:
            plans.append(payload)
            value = {
                "status": "ready",
                "plan": good if mode == "corrected" and len(plans) == 2 else bad,
            }
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]}
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw)
    )
    result = await create_long_image(
        request, config, plan=LongImagePlan(**bad) if mode == "direct" else None
    )
    if mode == "corrected":
        assert result.status == "succeeded"
        assert len(plans) == 2
        assert len(image_prompts) == 1
        assert "黑金风格" in image_prompts[0]
        assert "10 月 12 日开售" not in image_prompts[0]
        assert any(c["kind"] == "text_coverage" and c["passed"] for c in result.page_checks)
        assert any(u.text == "10 月 12 日开售" for s in result.plan.sections for u in s.text_units)
        assert result.usage.execution == {"vision_calls": 3, "image_calls": 1}
    else:
        assert result.status == "failed"
        assert result.error_info.code == "plan_invalid"
        assert image_prompts == []
        assert len(vision_posts) == (0 if mode == "direct" else 2)


def test_generic_compiler_rejects_global_visual_direction_copy():
    from image_agent.long_image_product import compile_long_generic_slot

    request = LongImageRequest(
        scene="tutorial",
        title="教程",
        content_brief="操作",
        required_copy=[{"requirement_id": "date", "text": "10 月 12 日开售"}],
    )
    plan = LongImagePlan(
        **(plan_for(request, slots=True) | {"visual_direction": "配图写出10 月 12 日开售"})
    )
    with pytest.raises(AgentError) as caught:
        compile_long_generic_slot(request, plan.image_slots[0], plan, (), AgentConfig())
    assert caught.value.code == "plan_invalid"


@pytest.mark.asyncio
async def test_corrective_post_is_counted_and_ready_record_uses_real_usage():
    request = LongImageRequest(scene="tutorial", title="教程", content_brief="四步操作")
    result, usage, transport, store = await prepared(
        request, ["broken", {"status": "ready", "plan": plan_for(request)}]
    )
    assert result.status == "ready"
    assert result.usage == usage == {"image_calls": 0, "vision_calls": 2}
    assert len(transport.payloads) == 2
    assert result.record is not None
    assert (
        store.load_planning_record(result.record.record_id, request=request, plan=result.plan)
        == result.record
    )


@pytest.mark.asyncio
async def test_shared_budget_limits_repeated_planning(tmp_path):
    request = LongImageRequest(scene="tutorial", title="教程", content_brief="操作")
    policy = ExecutionPolicy(
        shared_ledger_path=tmp_path / "shared.sqlite", shared_max_vision_calls=1
    )
    first, _, _, _ = await prepared(
        request, [{"status": "ready", "plan": plan_for(request)}], policy=policy
    )
    second, usage, transport, _ = await prepared(
        request, [{"status": "ready", "plan": plan_for(request)}], policy=policy
    )
    assert first.status == "ready"
    assert second.status == "failed"
    assert second.error_info.code == "budget_exhausted"
    assert usage["vision_calls"] == 0
    assert transport.payloads == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "count,brief,want",
    [
        (2, "两段结构：介绍、总结", "ready"),
        (12, "12 sections", "ready"),
        (2, "说明内容", "failed"),
        (9, "说明内容", "failed"),
    ],
)
async def test_default_and_explicit_section_counts(count, brief, want):
    request = LongImageRequest(scene="custom", title="内容", content_brief=brief)
    response = {"status": "ready", "plan": plan_for(request, count=count)}
    result, _, _, _ = await prepared(request, [response, response])
    assert result.status == want


@pytest.mark.asyncio
async def test_forged_or_stale_plan_rejected_before_post():
    request = LongImageRequest(scene="tutorial", title="教程", content_brief="操作")
    plan = LongImagePlan(**plan_for(request))
    changed = request.model_copy(update={"title": "新教程"})
    with pytest.raises(AgentError, match="match") as caught:
        await prepared(changed, [], plan=plan)
    assert caught.value.code == "plan_invalid"


@pytest.mark.asyncio
async def test_verified_record_reuses_plan_without_post():
    request = LongImageRequest(scene="tutorial", title="教程", content_brief="操作")
    first, _, _, _ = await prepared(request, [{"status": "ready", "plan": plan_for(request)}])
    reused, usage, transport, _ = await prepared(request, [], plan=first.plan, record=first.record)
    assert reused.record == first.record
    assert reused.planning_id == first.planning_id
    assert usage["vision_calls"] == 0
    assert transport.payloads == []


def audit_response():
    return dict(
        slot_id="illustration",
        candidate_id="candidate-1",
        passed=True,
        repairable=False,
        checks=[
            dict(kind=k, passed=True, reason="符合", severity="info")
            for k in ("purpose", "subject", "style", "unrequested_text")
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["missing", "unknown", "inconsistent", "wrong_candidate"])
async def test_unknown_or_missing_audit_checks_raise_audit_error(bad):
    from image_agent.execution import BudgetLedger
    from image_agent.long_image_vision import LongImageVisionClient

    request = LongImageRequest(scene="tutorial", title="教程", content_brief="操作")
    plan = LongImagePlan(**plan_for(request, slots=True))
    response = audit_response()
    if bad == "missing":
        response["checks"].pop()
    elif bad == "unknown":
        response["checks"][0]["requirement_id"] = "forged"
    elif bad == "wrong_candidate":
        response["candidate_id"] = "other"
    else:
        response["checks"][0].update(passed=False, severity="critical")
    transport = Transport([response, response])
    ledger = BudgetLedger(None, ExecutionPolicy())
    with call_scope(ledger, "vision"):
        with pytest.raises(ProviderError) as caught:
            await LongImageVisionClient(transport, AgentConfig()).audit(
                plan.image_slots[0], plan, png(), candidate_id="candidate-1", materials=()
            )
    assert caught.value.code == "audit_error"
    assert ledger.usage()["vision_calls"] == 2
    ledger.close()


@pytest.mark.asyncio
async def test_real_http_retry_and_json_correction_share_ledger():
    import httpx

    from image_agent.execution import BudgetLedger
    from image_agent.long_image_vision import LongImageVisionClient
    from image_agent.transport import HttpTransport

    request = LongImageRequest(scene="tutorial", title="教程", content_brief="操作")
    posts = []

    def respond(http_request):
        posts.append(http_request)
        if len(posts) == 1:
            return httpx.Response(503, json={"error": "temporary"})
        content = (
            "bad"
            if len(posts) == 2
            else json.dumps({"status": "ready", "plan": plan_for(request)}, ensure_ascii=False)
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    async def no_sleep(seconds):
        pass

    config = AgentConfig(openrouter_api_key="offline")
    ledger = BudgetLedger(None, ExecutionPolicy(max_vision_calls=3))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        async with HttpTransport(config, client=client, sleep=no_sleep) as transport:
            with call_scope(ledger, "vision"):
                result = await LongImageVisionClient(transport, config).plan(
                    request, (), collect_text_requirements(request)
                )
    assert result.status == "ready"
    assert len(posts) == 3
    assert ledger.usage()["vision_calls"] == 3
    ledger.close()


@pytest.mark.asyncio
async def test_page_copy_cannot_leak_into_slot_prompt_or_repair():
    from image_agent.execution import BudgetLedger
    from image_agent.long_image_vision import LongImageVisionClient

    request = LongImageRequest(
        scene="tutorial", title="教程", content_brief="必须展示“10 月 12 日开售”"
    )
    bad_plan = plan_for(request, slots=True)
    bad_plan["image_slots"][0]["prompt"] += "，配图写10 月 12 日开售"
    result, _, _, _ = await prepared(request, [{"status": "ready", "plan": bad_plan}] * 2)
    assert result.status == "failed"
    assert result.error_info.code == "plan_invalid"
    plan = LongImagePlan(**plan_for(request, slots=True))
    response = audit_response()
    response.update(passed=False, repairable=True, repair_prompt="修复后加入10 月 12 日开售")
    response["checks"][0].update(passed=False, severity="warning")
    ledger = BudgetLedger(None, ExecutionPolicy())
    with call_scope(ledger, "vision"):
        with pytest.raises(ProviderError) as caught:
            await LongImageVisionClient(Transport([response] * 2), AgentConfig()).audit(
                plan.image_slots[0], plan, png(), candidate_id="candidate-1", materials=()
            )
    assert caught.value.code == "audit_error"
    ledger.close()


@pytest.mark.asyncio
async def test_unbound_external_plan_history_usage_is_unavailable():
    request = LongImageRequest(scene="tutorial", title="教程", content_brief="操作")
    result, usage, transport, _ = await prepared(
        request, [], plan=LongImagePlan(**plan_for(request))
    )
    assert result.status == "ready"
    assert result.usage is None
    assert result.record is None
    assert usage["vision_calls"] == 0
    assert transport.payloads == []


@pytest.mark.asyncio
async def test_public_analyze_creates_independent_record_and_new_ids(monkeypatch, tmp_path):
    from pathlib import Path

    import httpx

    import image_agent.long_image_planning as module
    from image_agent.transport import HttpTransport

    request = LongImageRequest(
        scene="tutorial", title="教程", content_brief="操作", output_dir=tmp_path / "planning"
    )
    posts = []

    def response(http_request):
        posts.append(http_request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"status": "ready", "plan": plan_for(request)}, ensure_ascii=False
                            )
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        monkeypatch.setattr(
            module, "HttpTransport", lambda config, **kw: HttpTransport(config, client=client)
        )
        result = await module.analyze_long_image(request, AgentConfig(openrouter_api_key="offline"))
        memory = await module.analyze_long_image(
            request.model_copy(update={"output_dir": None}),
            AgentConfig(openrouter_api_key="offline"),
        )
    assert result.status == memory.status == "ready"
    assert result.planning_id != memory.planning_id
    assert result.record.local_record_ref is not None
    assert memory.record.local_record_ref is None
    assert result.record.usage == {"vision_calls": 1, "image_calls": 0}
    assert len(posts) == 2
    planning_directory = Path(result.record.local_record_ref).parents[1]
    assert planning_directory.parent == request.output_dir
    assert (planning_directory / "planning-result.json").is_file()


@pytest.mark.asyncio
async def test_native_source_slots_plan_without_audit_or_generation():
    import hashlib

    request = LongImageRequest(
        scene="brand_intro",
        title="品牌",
        content_brief="公司介绍",
        materials=[
            {
                "material_id": "logo",
                "source": {"data": png()},
                "subject_hint": "花朵品牌标志",
                "element_hints": ["保留原生花瓣图案"],
            }
        ],
    )
    data = plan_for(request)
    data["input_fingerprint"] = planning_fingerprint(
        request, {"logo": hashlib.sha256(png()).hexdigest()}
    )
    data["image_slots"] = [
        dict(
            slot_id="original",
            source="source",
            role="logo",
            material_ids=["logo"],
            section_ids=["section-0"],
        )
    ]
    data["sections"][0]["slot_ids"] = ["original"]
    result, usage, transport, _ = await prepared(request, [{"status": "ready", "plan": data}])
    assert result.status == "ready"
    assert result.plan.image_slots[0].source == "source"
    assert usage == {"image_calls": 0, "vision_calls": 1}
    assert len(transport.payloads) == 1
    planner_input = transport.payloads[0]["messages"][0]["content"][0]["text"]
    assert "花朵品牌标志" in planner_input
    assert "保留原生花瓣图案" in planner_input


def test_long_image_config_routes_override_and_fallback():
    config = AgentConfig.from_mapping(
        {
            "services": {
                "v": {"base_url": "https://vision.invalid", "protocol": "chat_completions"},
                "g": {"base_url": "https://images.invalid", "protocol": "openai_images"},
            },
            "image_models": {"pro": {"service": "g", "model": "image"}},
            "stages": {
                "vision": {"service": "v", "model": "default"},
                "plan_long_image": {"service": "v", "model": "planner"},
                "audit_long_image_asset": {"service": "v", "model": "auditor"},
            },
        }
    )
    assert config.vision_route("plan_long_image").model == "planner"
    assert config.vision_route("audit_long_image_asset").model == "auditor"
    fallback = config.model_copy(update={"stages": {"vision": config.stages["vision"]}})
    assert fallback.vision_route("plan_long_image").model == "default"


def test_generic_slot_compiles_real_references_and_no_product_fields():
    import hashlib

    from image_agent.long_image_product import compile_long_generic_slot
    from image_agent.models import LoadedMaterial

    request = LongImageRequest(
        scene="tutorial",
        title="教程",
        content_brief="介绍操作",
        materials=[{"material_id": "m1", "source": {"data": png()}}],
    )
    data = plan_for(request, slots=True)
    data["input_fingerprint"] = planning_fingerprint(
        request, {"m1": hashlib.sha256(png()).hexdigest()}
    )
    data["image_slots"][0]["material_ids"] = ["m1"]
    plan = LongImagePlan(**data)
    materials = (
        LoadedMaterial(
            material_id="m1",
            data=png(),
            sha256=hashlib.sha256(png()).hexdigest(),
            order=0,
            role_hint="auto",
        ),
    )
    compiled = compile_long_generic_slot(
        request, plan.image_slots[0], plan, materials, AgentConfig()
    )
    assert compiled.element_plan is None
    assert compiled.context is None
    assert compiled.references[0].data == png()
    assert compiled.prompt.endswith("操作示意图")
    assert "介绍操作" not in compiled.prompt


@pytest.mark.asyncio
async def test_explicit_visual_check_is_required_and_bound_to_real_id():
    from image_agent.long_image_vision import LongImageVisionClient

    request = LongImageRequest(scene="tutorial", title="教程", content_brief="主体应为绿色杯子")
    data = plan_for(request, slots=True)
    data["requirements"].append(
        dict(
            requirement_id="green",
            kind="slot_visual",
            text="绿色杯子",
            source={"pointer": "/content_brief", "start": 4, "end": 8},
            slot_ids=["illustration"],
        )
    )
    data["image_slots"][0]["requirement_ids"] = ["green"]
    plan = LongImagePlan(**data)
    response = audit_response()
    response["checks"].append(
        dict(requirement_id="green", kind="subject", passed=True, reason="绿色", severity="info")
    )
    report = await LongImageVisionClient(Transport([response]), AgentConfig()).audit(
        plan.image_slots[0], plan, png(), candidate_id="candidate-1", materials=()
    )
    assert report.passed
    assert report.checks[-1].requirement_id == "green"


@pytest.mark.asyncio
async def test_named_explicit_structure_allows_two_sections():
    request = LongImageRequest(scene="custom", title="内容", content_brief="结构：介绍、总结")
    result, _, _, _ = await prepared(
        request, [{"status": "ready", "plan": plan_for(request, count=2)}] * 2
    )
    assert result.status == "ready"


@pytest.mark.asyncio
async def test_copy_repair_is_rejected_after_initial_plan_is_valid():
    from image_agent.execution import BudgetLedger
    from image_agent.long_image_vision import LongImageVisionClient

    request = LongImageRequest(
        scene="tutorial", title="教程", content_brief="必须展示“10 月 12 日开售”"
    )
    plan = LongImagePlan(**plan_for(request, slots=True))
    response = audit_response()
    response.update(passed=False, repairable=True, repair_prompt="修复后加入10 月 12 日开售")
    response["checks"][0].update(passed=False, severity="warning")
    ledger = BudgetLedger(None, ExecutionPolicy())
    with call_scope(ledger, "vision"):
        with pytest.raises(ProviderError) as caught:
            await LongImageVisionClient(Transport([response] * 2), AgentConfig()).audit(
                plan.image_slots[0], plan, png(), candidate_id="candidate-1", materials=()
            )
    assert caught.value.code == "audit_error"
    assert ledger.usage()["vision_calls"] == 2
    ledger.close()


@pytest.mark.asyncio
async def test_trusted_plan_reuse_preflights_replaced_font(tmp_path):
    request = LongImageRequest(scene="tutorial", title="教程", content_brief="操作")
    result, _, _, _ = await prepared(request, [{"status": "ready", "plan": plan_for(request)}])
    bad_font = tmp_path / "bad.ttf"
    bad_font.write_bytes(b"broken")
    changed = request.model_copy(
        update={"options": request.options.model_copy(update={"font_path": bad_font})}
    )
    with pytest.raises(AgentError) as caught:
        await prepared(changed, [], plan=result.plan, record=result.record)
    assert caught.value.code == "font_invalid"


def test_detached_slot_cannot_use_valid_plan_as_cover():
    from image_agent.long_image_product import compile_long_generic_slot

    request = LongImageRequest(scene="tutorial", title="教程", content_brief="操作")
    plan = LongImagePlan(**plan_for(request, slots=True))
    forged = plan.image_slots[0].model_copy(update={"prompt": "完全不同的生成指令"})
    with pytest.raises(AgentError) as caught:
        compile_long_generic_slot(request, forged, plan, (), AgentConfig())
    assert caught.value.code == "plan_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "brief", ["教程分两步：准备、使用", "1. 准备\n2. 使用", "two steps: prepare, use"]
)
async def test_explicit_steps_allow_three_sections(brief):
    request = LongImageRequest(scene="tutorial", title="教程", content_brief=brief)
    result, _, _, _ = await prepared(
        request, [{"status": "ready", "plan": plan_for(request, count=3)}] * 2
    )
    assert result.status == "ready"


@pytest.mark.asyncio
async def test_three_sections_without_structure_remains_invalid():
    request = LongImageRequest(scene="tutorial", title="教程", content_brief="介绍如何使用")
    result, _, _, _ = await prepared(
        request, [{"status": "ready", "plan": plan_for(request, count=3)}] * 2
    )
    assert result.status == "failed"
    assert result.error_info.code == "plan_invalid"


@pytest.mark.asyncio
async def test_repeated_public_planning_keeps_separate_ledgers_in_output_root(
    monkeypatch, tmp_path
):
    from pathlib import Path

    import httpx

    import image_agent.long_image_planning as module
    from image_agent.transport import HttpTransport

    request = LongImageRequest(
        scene="tutorial", title="教程", content_brief="操作", output_dir=tmp_path
    )

    def response(http_request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"status": "ready", "plan": plan_for(request)}, ensure_ascii=False
                            )
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        monkeypatch.setattr(
            module, "HttpTransport", lambda config, **kw: HttpTransport(config, client=client)
        )
        one = await module.analyze_long_image(request, AgentConfig(openrouter_api_key="offline"))
        two = await module.analyze_long_image(request, AgentConfig(openrouter_api_key="offline"))
    assert one.planning_id != two.planning_id
    assert one.record.local_record_ref != two.record.local_record_ref
    assert one.record.ledger_ref != two.record.ledger_ref
    assert Path(one.record.ledger_ref).is_file()
    assert Path(two.record.ledger_ref).is_file()
    assert one.usage == two.usage == {"image_calls": 0, "vision_calls": 1}


@pytest.mark.asyncio
async def test_public_planning_requires_only_active_vision_credentials(monkeypatch):
    import httpx

    import image_agent.long_image_planning as module
    from image_agent.transport import HttpTransport

    request = LongImageRequest(scene="tutorial", title="教程", content_brief="操作")
    config = AgentConfig.from_mapping(
        {
            "services": {
                "plan": {
                    "base_url": "https://vision.invalid",
                    "protocol": "chat_completions",
                    "api_key": "offline",
                },
                "unused": {"base_url": "https://audit.invalid", "protocol": "chat_completions"},
                "image": {"base_url": "https://images.invalid", "protocol": "openai_images"},
            },
            "image_models": {"pro": {"service": "image", "model": "image"}},
            "stages": {
                "vision": {"service": "plan", "model": "planner"},
                "audit_long_image_asset": {"service": "unused", "model": "audit"},
            },
        }
    )

    def response(http_request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"status": "ready", "plan": plan_for(request)}, ensure_ascii=False
                            )
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:

        def transport(config, *, services):
            config.validate_credentials(services)
            return HttpTransport(config, client=client, services=services)

        monkeypatch.setattr(module, "HttpTransport", transport)
        result = await module.analyze_long_image(request, config)
    assert result.status == "ready"
