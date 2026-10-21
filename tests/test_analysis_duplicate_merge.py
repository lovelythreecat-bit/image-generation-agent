"""Duplicate model bookkeeping must preserve every compatible observation."""

from copy import deepcopy

import pytest
from image_agent.config import AgentConfig
from image_agent.models import CreationRequest, MaterialAnalysis
from image_agent.vision import VisionClient
from pydantic import ValidationError

from tests.fakes import analysis_data
from tests.test_vision import ScriptTransport, materials


@pytest.mark.parametrize(
    "path",
    [
        ("subjects", 0, "material_ids"),
        ("intent", "proposal", "subject_ids"),
        ("intent", "proposal", "preferred_element_ids"),
        ("intent", "focus_element_ids"),
    ],
)
def test_repeated_references_are_one_reference_without_changing_input(path):
    data = analysis_data()
    row = data
    for part in path[:-1]:
        row = row[part]
    expected = list(row[path[-1]])
    row[path[-1]] *= 2
    original = deepcopy(data)
    result = MaterialAnalysis.model_validate(data).model_dump(mode="json")
    for part in path:
        result = result[part]
    assert result == expected
    assert data == original


def test_duplicate_elements_keep_both_descriptions_and_all_fact_links():
    data = analysis_data()
    data["elements"][0]["description"] = "胸前红色图案"
    data["facts"].append(dict(deepcopy(data["facts"][0]), fact_id="f2"))
    data["elements"].append(
        dict(deepcopy(data["elements"][0]), description="圆领领口", fact_ids=["f2"])
    )
    result = MaterialAnalysis.model_validate(data)
    assert len(result.elements) == 1
    assert "胸前红色图案" in result.elements[0].description
    assert "圆领领口" in result.elements[0].description
    assert result.elements[0].fact_ids == ["f1", "f2"]
    assert result.intent.proposal.preferred_element_ids == ["e1"]
    assert MaterialAnalysis.model_validate(result.model_dump()).model_dump() == result.model_dump()


def test_duplicate_facts_keep_both_descriptions_and_evidence():
    data = analysis_data()
    other = deepcopy(data["facts"][0])
    other.update(description="red pattern on the chest", confidence=0.8)
    other["evidence"][0]["observation"] = "red artwork visible on the same original"
    data["facts"].append(other)
    result = MaterialAnalysis.model_validate(data)
    assert len(result.facts) == 1
    assert "white ceramic cup" in result.facts[0].description
    assert "red pattern on the chest" in result.facts[0].description
    assert [e.observation for e in result.facts[0].evidence] == [
        "white cup visible",
        "red artwork visible on the same original",
    ]
    assert result.facts[0].confidence == 0.8


def test_multiple_observations_on_one_material_preserve_provenance():
    data = analysis_data()
    data["facts"][0]["evidence"].append(
        dict(
            material_id="m1",
            observation="printed logo visible",
            source_type="product_label",
        )
    )
    result = MaterialAnalysis.model_validate(data)
    assert [(e.observation, e.source_type) for e in result.facts[0].evidence] == [
        ("white cup visible", "visual"),
        ("printed logo visible", "product_label"),
    ]


def test_duplicate_materials_merge_summaries_without_losing_order():
    data = analysis_data()
    data["materials"].append(dict(data["materials"][0], summary="red printed artwork"))
    result = MaterialAnalysis.model_validate(data)
    assert [m.material_id for m in result.materials] == ["m1"]
    assert "ceramic cup" in result.materials[0].summary
    assert "red printed artwork" in result.materials[0].summary


def test_duplicate_subjects_merge_views_and_keep_a_valid_representative():
    data = analysis_data()
    data["materials"].append(dict(data["materials"][0], material_id="m2"))
    data["subjects"].append(
        dict(data["subjects"][0], material_ids=["m2"], representative_material_id="m2")
    )
    result = MaterialAnalysis.model_validate(data)
    assert len(result.subjects) == 1
    assert result.subjects[0].material_ids == ["m1", "m2"]
    assert result.subjects[0].representative_material_id == "m1"


def test_required_and_preferred_reference_to_same_element_remains_required():
    data = analysis_data()
    data["intent"]["proposal"]["required_element_ids"] = ["e1", "e1"]
    result = MaterialAnalysis.model_validate(data)
    assert result.intent.proposal.required_element_ids == ["e1"]
    assert result.intent.proposal.preferred_element_ids == []


def test_selected_and_excluded_conflict_is_not_silently_removed():
    data = analysis_data()
    data["intent"]["proposal"]["excluded_element_ids"] = ["e1"]
    with pytest.raises(ValidationError):
        MaterialAnalysis.model_validate(data)


@pytest.mark.parametrize(
    "table,field,value",
    [
        ("materials", "sha256", "different-original"),
        ("subjects", "matches_product", False),
        ("elements", "subject_id", "different-subject"),
        ("elements", "kind", "identity"),
        ("facts", "verifiability", "functional_claim"),
    ],
)
def test_incompatible_metadata_is_not_overwritten(table, field, value):
    data = analysis_data()
    data[table].append(dict(deepcopy(data[table][0]), **{field: value}))
    with pytest.raises(ValidationError, match=field):
        MaterialAnalysis.model_validate(data)


def issue(message):
    return dict(
        issue_id="i1",
        code="ambiguous_subject",
        resolution="selection",
        message=message,
        subject_ids=["s1", "s1"],
    )


def test_issue_in_both_buckets_preserves_both_messages_and_remains_blocking():
    from image_agent.selection import finalize_selection, resolve_selection

    data = analysis_data()
    data["issues"] = [issue("choose the product")]
    data["intent"]["unmet_requirements"] = [issue("confirm the selected subject")]
    result = MaterialAnalysis.model_validate(data)
    issues = result.issues + result.intent.unmet_requirements
    assert len(issues) == 1
    assert "choose the product" in issues[0].message
    assert "confirm the selected subject" in issues[0].message
    request = CreationRequest(
        product_name="Cup",
        category="kitchen",
        platforms=["taobao"],
        output_types=["main_image"],
        materials=[dict(material_id="m1", source={"data": b"x"})],
    )
    assert finalize_selection(resolve_selection(request, result)).status == "needs_input"


def test_duplicate_constraints_preserve_both_instructions():
    data = analysis_data()
    base = dict(
        constraint_id="c1",
        kind="appearance",
        subject_ids=["s1"],
        element_ids=["e1"],
        source="brief",
        source_quote="preserve details",
        source_material_id=None,
        priority="required",
    )
    data["intent"]["constraints"] = [
        dict(base, instruction="preserve the red pattern"),
        dict(base, instruction="preserve the round collar"),
    ]
    result = MaterialAnalysis.model_validate(data)
    assert len(result.intent.constraints) == 1
    assert "preserve the red pattern" in result.intent.constraints[0].instruction
    assert "preserve the round collar" in result.intent.constraints[0].instruction


async def test_real_vision_parser_accepts_duplicate_references_on_first_response():
    data = analysis_data()
    data["intent"]["proposal"]["subject_ids"] *= 2
    data["elements"].append(dict(data["elements"][0], description="visible printed logo"))
    transport = ScriptTransport([data])
    request = CreationRequest(
        product_name="Cup",
        category="kitchen",
        platforms=["taobao"],
        output_types=["main_image"],
        materials=[dict(material_id="m1", source={"data": b"x"})],
    )
    result = await VisionClient(transport, AgentConfig()).analyze_materials(request, materials())
    assert result.status == "ready"
    assert "ceramic texture" in result.elements[0].description
    assert "visible printed logo" in result.elements[0].description
    assert len(transport.calls) == 1


def test_later_analysis_assignment_keeps_typed_nested_records():
    result = MaterialAnalysis.model_validate(analysis_data())
    result.analysis_id = "new-analysis"
    assert result.materials[0].material_id == "m1"
    assert result.intent.proposal.subject_ids == ["s1"]


def test_duplicate_fact_cannot_promote_default_appearance_into_a_functional_claim():
    data = analysis_data()
    del data["facts"][0]["verifiability"]
    other = dict(deepcopy(data["facts"][0]), verifiability="functional_claim")
    data["facts"].append(other)
    with pytest.raises(ValidationError, match="verifiability"):
        MaterialAnalysis.model_validate(data)


def test_malformed_issue_identifier_produces_validation_error():
    data = analysis_data()
    data["issues"] = [dict(issue("choose"), issue_id=["bad"])]
    with pytest.raises(ValidationError):
        MaterialAnalysis.model_validate(data)


@pytest.mark.parametrize("output,count", [("main_image", 1), ("detail_page", 3)])
async def test_merged_content_reaches_generation_and_saved_result(tmp_path, output, count):
    import json
    from pathlib import Path

    from image_agent.pipeline import run_pipeline

    from tests.test_pipeline import setup

    data = analysis_data()
    data["elements"][0]["description"] = "红色图案"
    data["elements"].append(dict(data["elements"][0], description="圆领细节"))
    data["intent"]["proposal"]["subject_ids"] *= 2
    transport = ScriptTransport([data])
    request, deps, _, generator = setup(
        dict(
            product_name="Cup",
            category="kitchen",
            platforms=["taobao"],
            output_types=[output],
            materials=[dict(material_id="m1", source={"data": b"x"})],
            output_dir=tmp_path,
        )
    )
    deps.vision.analyze_materials = VisionClient(transport, AgentConfig()).analyze_materials
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded"
    assert len(transport.calls) == 1
    assert len(generator.calls) == count
    assert any("红色图案" in c["prompt"] and "圆领细节" in c["prompt"] for c in generator.calls)
    saved = json.loads((Path(result.run_dir) / "result.json").read_text(encoding="utf-8"))
    assert "红色图案" in saved["analysis"]["elements"][0]["description"]
    assert "圆领细节" in saved["analysis"]["elements"][0]["description"]
