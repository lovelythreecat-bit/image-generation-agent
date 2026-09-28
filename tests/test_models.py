import pytest
from pydantic import ValidationError


def test_request_normalization_and_legacy(request_data):
    from image_agent.models import CreationRequest
    request_data["platforms"] = [" TAOBAO ", "taobao"]
    r = CreationRequest(**request_data)
    assert r.platforms == ["taobao"]
    assert r.market == "CN"
    assert r.materials[0].role_hint == "auto"
    request_data.pop("materials")
    r = CreationRequest(**request_data, product_image={"data": b"x"}, reference_images=[{"data": b"y"}])
    assert [(m.material_id, m.role_hint) for m in r.normalized_materials()] == [("product", "identity"), ("ref-1", "style")]


@pytest.mark.parametrize("changes", [
    {"materials": []}, {"product_name": " "}, {"category": ""},
    {"platforms": []}, {"platforms": ["unknown"]}, {"output_types": ["social"]},
    {"output_types": ["pdd_white_background"]}, {"product_image": {"data": b"x"}},
    {"request_id": "../escape"}, {"request_id": "CON.txt"}, {"request_id": "foo."},
    {"request_id": "a/b"}, {"request_id": "a\\b"}, {"request_id": "x "},
    {"selection": {"mode": "auto", "subject_ids": ["s1"]}},
    {"selection": {"mode": "explicit", "subject_ids": ["s1"], "required_element_ids": ["e1"], "excluded_element_ids": ["e1"]}},
    {"unknown": True},
])
def test_reject_invalid_requests(request_data, changes):
    from image_agent.models import CreationRequest
    with pytest.raises(ValidationError):
        CreationRequest(**(request_data | changes))


@pytest.mark.parametrize("count,valid", [(0, False), (1, True), (8, True), (9, False)])
def test_material_count(request_data, count, valid):
    from image_agent.models import CreationRequest
    request_data["materials"] = [dict(material_id=f"m{i}", source={"data": b"x"}) for i in range(count)]
    if valid:
        assert len(CreationRequest(**request_data).materials) == count
    else:
        with pytest.raises(ValidationError):
            CreationRequest(**request_data)


@pytest.mark.parametrize("source", [{}, {"path": "a", "data": b"x"}, {"url": "file:///tmp/a"}, {"url": "https://"}])
def test_source_exclusive_http_only(source):
    from image_agent.models import ImageSource
    with pytest.raises(ValidationError):
        ImageSource(**source)


def test_duplicate_material_and_bad_id(request_data):
    from image_agent.models import CreationRequest
    request_data["materials"] *= 2
    with pytest.raises(ValidationError):
        CreationRequest(**request_data)
    request_data["materials"] = [{"material_id": "../bad", "source": {"data": b"x"}}]
    with pytest.raises(ValidationError):
        CreationRequest(**request_data)


def test_analysis_references_and_evidence():
    from image_agent.models import MaterialAnalysis
    from tests.fakes import analysis_data
    a = MaterialAnalysis(**analysis_data())
    assert a.intent.proposal.subject_ids == ["s1"]
    for mutate in [
        lambda x: x["facts"][0].update(evidence=[]),
        lambda x: x["subjects"][0].update(identity_fact_ids=["missing"]),
        lambda x: x["subjects"][0].update(representative_material_id="missing"),
        lambda x: x["elements"][0].update(fact_ids=["missing"]),
        lambda x: x["facts"].append(x["facts"][0]),
        lambda x: x["intent"]["proposal"].update(subject_ids=["missing"]),
    ]:
        data = analysis_data()
        mutate(data)
        with pytest.raises(ValidationError):
            MaterialAnalysis(**data)


def test_scores_and_presence_are_strict():
    from image_agent.models import SubjectCheck, FactCheck
    for value in [True, -1, 101, "90"]:
        with pytest.raises(ValidationError):
            SubjectCheck(subject_id="s1", score=value, same_product=True, reason="ok")
    with pytest.raises(ValidationError):
        FactCheck(fact_id="f1", presence="not_applicable", fidelity_score=100, reason="hidden")
