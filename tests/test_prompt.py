from image_agent.models import AssetElementPlan, CreationRequest


def test_source_derived_prompt_fixtures(request_data):
    import json
    from pathlib import Path

    from image_agent import AgentConfig
    from image_agent.compliance import apply_compliance
    from image_agent.prompt import build_prompt, build_targets
    from image_agent.selection import compile_element_plan
    from tests.test_selection import context

    cases = json.loads((Path(__file__).parent / "fixtures/prompt_cases.json").read_text("utf-8"))[
        "cases"
    ]
    for case in cases:
        request = CreationRequest(
            **(
                request_data
                | dict(
                    platforms=[case["platform"]],
                    output_types=[case["output"]],
                    presentation_mode=case["presentation"],
                    model_preference=case["preference"],
                    style_hint=case["style"],
                )
            )
        )
        target = build_targets(request, case["presentation"])[0]
        ctx = context(request)
        plan = compile_element_plan(request, target, ctx, AgentConfig())
        prompt = apply_compliance(request, target, build_prompt(request, target, ctx, plan)).prompt
        for token in case["contains"]:
            assert token in prompt, case["name"]
        for token in case["absent"]:
            assert token not in prompt, case["name"]


def test_targets_order_and_white_override(request_data):
    from image_agent.prompt import build_targets, resolve_presentation

    r = CreationRequest(
        **(
            request_data
            | dict(
                platforms=["taobao", "pinduoduo"],
                output_types=["pdd_white_background", "detail_page", "main_image"],
                model_preference="female",
            )
        )
    )
    targets = build_targets(r, resolve_presentation(r))
    assert len(targets) == 9
    assert [t.variant for t in targets[2:5]] == ["scene", "feature", "closeup"]
    assert targets[-1].presentation_mode == "product_only"
    assert targets[-1].model_preference == "auto"
    assert (
        resolve_presentation(r.model_copy(update={"presentation_mode": "product_only"}), "shirt")
        == "product_only"
    )


def test_prompt_respects_plan_and_shot(request_data):
    from types import SimpleNamespace

    from image_agent.prompt import build_prompt, build_targets

    r = CreationRequest(
        **(
            request_data
            | dict(model_preference="no_face", style_hint="江南巷弄", output_types=["detail_page"])
        )
    )
    target = build_targets(r, "model_wear")[-1]
    plan = AssetElementPlan(
        target_key=target.target_key,
        subject_ids=["s1"],
        requirements=[
            dict(
                target_kind="element",
                target_id="e4",
                origin="user_required",
                applicability="must_show",
                reason="retain closure",
            )
        ],
    )
    from image_agent.models import MaterialAnalysis
    from tests.fakes import analysis_data

    ctx = SimpleNamespace(
        analysis=MaterialAnalysis(**analysis_data()), product_attributes={}, style_prompt=None
    )
    prompt = build_prompt(r, target, ctx, plan, "strict")
    assert "face must not be visible" in prompt
    assert "full-body" not in prompt
    assert "must_show" in prompt and "e4" in prompt
    r = r.model_copy(update={"platforms": ["amazon"], "output_types": ["main_image"]})
    target = build_targets(r, "model_wear")[0]
    assert "江南巷弄" not in build_prompt(r, target, ctx, plan)
