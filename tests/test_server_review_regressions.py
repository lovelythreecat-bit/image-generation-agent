"""Actual HTTP regressions for provider redaction without protocol corruption."""

import json

import pytest
from pydantic import SecretStr

from image_agent.models import GarmentStructureAudit, MaterialAnalysis
from image_agent.pipeline import prepare_context
from tests import test_server_tasks as task_fixtures
from tests import test_server_worker as worker_fixtures
from tests.fakes import analysis_data
from tests.test_server_tasks import submit
from tests.test_server_worker import once

service = task_fixtures.service
client = task_fixtures.client
core = worker_fixtures.core
HEADERS = {"Authorization": "Bearer test-token"}


def credential(service, value):
    for provider in service.config_service.current.services.values():
        provider.api_key = SecretStr(value)


async def garment_task(service, core, monkeypatch, secret, items):
    from image_agent import pipeline

    credential(service, secret)

    async def garments(*args):
        return GarmentStructureAudit(items=items)

    core.vision.extract_garments = garments

    async def create(request, config, **kwargs):
        context, result = await prepare_context(request, config, dependencies=core)
        assert context is not None
        result.status = "succeeded"
        return result

    monkeypatch.setattr(pipeline, "create_images", create)
    _, request = await submit(service, key="first")
    request.product_name = "shirt"
    request.category = "clothing"
    request.presentation_mode = "model_wear"
    task = await service.submit_task("create_images", "config", request, "garments")
    await once(service)
    await once(service)
    return task


async def test_http_provider_controlled_garment_mapping_keys_never_publish_credentials(
    service, client, core, monkeypatch
):
    secret = "review-provider-key"
    task = await garment_task(service, core, monkeypatch, secret, [{secret: "provider echo"}])
    response = await client.get(f"/api/v1/tasks/{task['task_id']}/result", headers=HEADERS)
    assert response.status_code == 200
    assert secret not in response.text
    result = response.json()
    items = result["product_attributes"]["subjects"]["s1"]["garments"]["items"]
    assert list(items[0].values()) == ["provider echo"]
    stored = await service.repository.get(task["task_id"])
    assert secret not in str(stored.result)


async def test_http_enum_credential_overlap_keeps_succeeded_and_redacts_provider_echo(
    service, client, core, monkeypatch
):
    task = await garment_task(
        service, core, monkeypatch, "succeeded", [{"succeeded": "provider echo"}]
    )
    response = await client.get(f"/api/v1/tasks/{task['task_id']}/result", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    items = response.json()["product_attributes"]["subjects"]["s1"]["garments"]["items"]
    assert "succeeded" not in items[0]
    assert (await service.get_task(task["task_id"]))["core_status"] == "succeeded"


async def test_http_analysis_ready_enum_survives_credential_overlap(service, client, core):
    credential(service, "ready")
    core.vision.discovery = analysis_data() | {"warnings": ["echo ready"]}
    task, _ = await submit(service, kind="analyze_materials")
    await once(service)
    response = await client.get(f"/api/v1/tasks/{task['task_id']}/result", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["warnings"] == ["echo [redacted]"]


@pytest.mark.parametrize(
    "secret", ["taobao.main_image.default", "c0001", "m1", "s1", "f1", "tasks", "image/png"]
)
async def test_http_identifier_and_storage_overlap_preserves_dto_and_blob_download(
    service, client, core, secret
):
    credential(service, secret)
    task, _ = await submit(service)
    await once(service)
    response = await client.get(f"/api/v1/tasks/{task['task_id']}/result", headers=HEADERS)
    assert response.status_code == 200
    result = response.json()
    asset = result["assets"][0]
    assert asset["asset_id"] == asset["blob_id"] == "taobao.main_image.default"
    assert asset["candidates"][0]["candidate_id"] == "c0001"
    assert asset["reference_bindings"][0]["material_id"] == "m1"
    # Provider-created IDs may be anonymized when they overlap a credential;
    # preserve referential semantics, not the provider's original literal.
    analysis = MaterialAnalysis.model_validate(result["analysis"])
    subject_id = analysis.subjects[0].subject_id
    fact_id = analysis.facts[0].fact_id
    assert subject_id in result["product_attributes"]["subjects"]
    assert fact_id in result["product_attributes"]["subjects"][subject_id]
    assert analysis.intent.proposal.primary_subject_id == subject_id
    assert subject_id in asset["element_plan"]["subject_ids"]
    assert fact_id in asset["element_plan"]["required_fact_ids"]
    if secret in {"s1", "f1"}:
        assert secret not in subject_id and secret not in fact_id
    image = await client.get(
        f"/api/v1/tasks/{task['task_id']}/blobs/{asset['blob_id']}", headers=HEADERS
    )
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert len(image.content) == asset["byte_length"]


async def test_untrusted_mapping_key_collisions_retain_all_values_without_secrets(
    service, client, core, monkeypatch
):
    secret = "review-provider-key"
    raw = {
        secret: "first",
        "[redacted]": "second",
        f"prefix-{secret}": "third",
        "[redacted]#2": "fourth",
        "free_text": [f"echo {secret}"],
    }
    task = await garment_task(service, core, monkeypatch, secret, [raw])
    response = await client.get(f"/api/v1/tasks/{task['task_id']}/result", headers=HEADERS)
    assert response.status_code == 200 and secret not in response.text
    item = response.json()["product_attributes"]["subjects"]["s1"]["garments"]["items"][0]
    assert len(item) == 5
    assert {value for value in item.values() if isinstance(value, str)} == {
        "first",
        "second",
        "third",
        "fourth",
    }
    assert item["free_text"] == ["echo [redacted]"]
    assert item["[redacted]"] == "second" and item["[redacted]#2"] == "fourth"


async def test_invalid_saved_public_dto_fails_before_marking_completed(
    service, client, core, monkeypatch
):
    original = service.media_store.save_bundle

    def invalid(task_id, bundle):
        payload = original(task_id, bundle)
        payload["dto"]["status"] = "not-a-business-status"
        return payload

    monkeypatch.setattr(service.media_store, "save_bundle", invalid)
    task, _ = await submit(service)
    await once(service)
    assert (await service.get_task(task["task_id"]))["status"] == "failed"
    response = await client.get(f"/api/v1/tasks/{task['task_id']}/result", headers=HEADERS)
    assert response.status_code == 409


async def test_invalid_analysis_fails_before_marking_completed(service, client, monkeypatch):
    from image_agent import pipeline

    async def invalid(*args):
        return MaterialAnalysis.model_construct(**(analysis_data() | {"status": "invalid"}))

    monkeypatch.setattr(pipeline, "analyze_materials", invalid)
    task, _ = await submit(service, kind="analyze_materials")
    await once(service)
    assert (await service.get_task(task["task_id"]))["status"] == "failed"
    assert (
        await client.get(f"/api/v1/tasks/{task['task_id']}/result", headers=HEADERS)
    ).status_code == 409


async def test_schema_key_overlap_preserves_fixed_fields_but_redacts_arbitrary_provider_keys(
    service, client, core, monkeypatch
):
    task = await garment_task(service, core, monkeypatch, "status", [{"status": "echo status"}])
    response = await client.get(f"/api/v1/tasks/{task['task_id']}/result", headers=HEADERS)
    assert response.status_code == 200 and response.json()["status"] == "succeeded"
    item = response.json()["product_attributes"]["subjects"]["s1"]["garments"]["items"][0]
    assert item == {"[redacted]": "echo [redacted]"}


async def test_id_keyed_plan_maps_preserve_only_proven_typed_ids_and_redact_unknown_keys(
    service, client, core, monkeypatch
):
    from image_agent import pipeline

    secret = "review-provider-key"
    credential(service, secret)
    original = pipeline.create_images

    async def create(*args, **kwargs):
        result = await original(*args, **kwargs)
        plan = result.assets[0].element_plan
        plan.excluded_reasons = {secret: "echo reason", "e1": "expected reason"}
        plan.subject_presentations = {secret: "echo value", "s1": "product_only"}
        return result

    monkeypatch.setattr(pipeline, "create_images", create)
    task, _ = await submit(service)
    await once(service)
    response = await client.get(f"/api/v1/tasks/{task['task_id']}/result", headers=HEADERS)
    assert response.status_code == 200 and secret not in response.text
    plan = response.json()["assets"][0]["element_plan"]
    assert plan["excluded_reasons"]["e1"] == "expected reason"
    assert plan["subject_presentations"]["s1"] == "product_only"


@pytest.mark.parametrize("original_id", ["f1", "s1", "e1", "a1"])
async def test_http_provider_authored_pattern_ids_cannot_publish_credentials(
    service, client, core, original_id
):
    secret = "review-provider-key"
    credential(service, secret)
    core.vision.discovery = json.loads(
        json.dumps(analysis_data()).replace(json.dumps(original_id), json.dumps(secret))
    )
    task, _ = await submit(service)
    await once(service)
    response = await client.get(f"/api/v1/tasks/{task['task_id']}/result", headers=HEADERS)
    assert response.status_code == 200 and secret not in response.text
    analysis = MaterialAnalysis.model_validate(response.json()["analysis"])
    subject, fact, element = analysis.subjects[0], analysis.facts[0], analysis.elements[0]
    assert fact.subject_id == subject.subject_id == element.subject_id
    assert fact.fact_id in subject.identity_fact_ids and fact.fact_id in element.fact_ids
    attrs = response.json()["product_attributes"]["subjects"]
    assert subject.subject_id in attrs and fact.fact_id in attrs[subject.subject_id]
    asset = response.json()["assets"][0]
    assert subject.subject_id in asset["element_plan"]["subject_ids"]
    assert fact.fact_id in asset["element_plan"]["required_fact_ids"]
    assert {check["subject_id"] for check in asset["quality"]["subject_checks"]} == {
        subject.subject_id
    }
    assert (await service.repository.get(task["task_id"])).status == "completed"


async def test_http_provider_fact_id_credential_echo_in_real_garment_attributes(
    service, client, core, monkeypatch
):
    secret = "review-provider-key"
    core.vision.discovery = json.loads(
        json.dumps(analysis_data()).replace('"f1"', json.dumps(secret))
    )
    task = await garment_task(service, core, monkeypatch, secret, [{secret: "provider echo"}])
    response = await client.get(f"/api/v1/tasks/{task['task_id']}/result", headers=HEADERS)
    assert response.status_code == 200 and secret not in response.text
    analysis = MaterialAnalysis.model_validate(response.json()["analysis"])
    fact_id = analysis.facts[0].fact_id
    attrs = response.json()["product_attributes"]["subjects"][analysis.subjects[0].subject_id]
    assert fact_id in attrs and fact_id in analysis.subjects[0].identity_fact_ids
    assert attrs["garments"]["items"] == [{"[redacted]": "provider echo"}]


async def test_http_provider_constraint_issue_and_option_ids_cannot_leak_credentials(
    service, client, core
):
    secret = "review-provider-key"
    credential(service, secret)
    data = analysis_data()
    data["intent"]["constraints"] = [
        {
            "constraint_id": secret,
            "kind": "placement",
            "subject_ids": ["s1"],
            "element_ids": ["e1"],
            "instruction": "keep view",
            "source": "brief",
            "source_quote": "keep view",
            "source_material_id": None,
            "priority": "preferred",
        }
    ]
    data["issues"] = [
        {
            "issue_id": "issue-" + secret,
            "code": "choice",
            "resolution": "selection",
            "message": "choose view",
            "subject_ids": ["s1"],
            "options": [
                {
                    "id": "option-" + secret,
                    "label": "choose view",
                    "selection": {"mode": "explicit", "subject_ids": ["s1"]},
                }
            ],
        }
    ]
    core.vision.discovery = data
    _, request = await submit(service, key="seed")
    request.creative_brief = "keep view"
    task = await service.submit_task("analyze_materials", "config", request, "analysis-identities")
    await once(service)
    await once(service)
    response = await client.get(f"/api/v1/tasks/{task['task_id']}/result", headers=HEADERS)
    assert response.status_code == 200 and secret not in response.text
    analysis = MaterialAnalysis.model_validate(response.json())
    assert secret not in analysis.intent.constraints[0].constraint_id
    assert secret not in analysis.issues[0].issue_id
    assert secret not in analysis.issues[0].options[0].id
    assert analysis.issues[0].options[0].selection.subject_ids == [analysis.subjects[0].subject_id]


@pytest.mark.parametrize("secret", ["review-provider-key", "f1", "s1"])
async def test_http_anonymized_analysis_ids_remain_reusable_for_explicit_selection(
    service, client, core, secret
):
    credential(service, secret)
    if secret == "review-provider-key":
        data = json.dumps(analysis_data())
        for original in ("f1", "s1", "e1"):
            data = data.replace(json.dumps(original), json.dumps(secret))
        core.vision.discovery = json.loads(data)
    analyzed, request = await submit(service, kind="analyze_materials")
    await once(service)
    response = await client.get(f"/api/v1/tasks/{analyzed['task_id']}/result", headers=HEADERS)
    assert response.status_code == 200
    if secret == "review-provider-key":
        assert secret not in response.text
    analysis = MaterialAnalysis.model_validate(response.json())
    subject_id, fact_id = analysis.subjects[0].subject_id, analysis.facts[0].fact_id
    assert secret not in subject_id and secret not in fact_id
    element_id = analysis.elements[0].element_id
    body = {
        "kind": "create_images",
        "agent_config_id": "config",
        "analysis_task_id": analyzed["task_id"],
        "request": request.model_dump(mode="json")
        | {
            "selection": {
                "mode": "explicit",
                "subject_ids": [subject_id],
                "required_element_ids": [element_id],
            }
        },
    }
    submitted = await client.post(
        "/api/v1/tasks", headers=HEADERS | {"Idempotency-Key": "explicit-reuse"}, json=body
    )
    assert submitted.status_code == 202
    task_id = submitted.json()["task_id"]
    await once(service)
    result = await client.get(f"/api/v1/tasks/{task_id}/result", headers=HEADERS)
    assert result.status_code == 200 and result.json()["status"] == "succeeded"
    reused = MaterialAnalysis.model_validate(result.json()["analysis"])
    assert reused.subjects[0].subject_id == subject_id
    assert reused.facts[0].fact_id == fact_id and reused.elements[0].element_id == element_id
    assert result.json()["assets"][0]["element_plan"]["subject_ids"] == [subject_id]
    # The service-owned analysis snapshot is reused; no second discovery is
    # needed. Whether evidence requires a further recheck belongs to core policy.
    assert core.vision.calls.count("discover") == 1
    assert "audit:taobao.main_image.default" in core.vision.calls


async def test_provider_error_stage_cannot_use_semantic_field_exemption(
    service, client, monkeypatch
):
    from image_agent import pipeline

    secret = "review-provider-key"
    credential(service, secret)

    async def analyze(*args):
        data = analysis_data() | {
            "status": "failed",
            "intent": None,
            "error_info": {
                "code": "provider_protocol",
                "kind": "protocol",
                "message": "error",
                "stage": secret,
            },
        }
        return MaterialAnalysis.model_validate(data)

    monkeypatch.setattr(pipeline, "analyze_materials", analyze)
    task, _ = await submit(service, kind="analyze_materials")
    await once(service)
    response = await client.get(f"/api/v1/tasks/{task['task_id']}/result", headers=HEADERS)
    assert response.status_code == 200 and secret not in response.text
    assert response.json()["error_info"]["stage"] == "[redacted]"


async def test_provider_fact_id_matching_client_material_is_anonymized_in_its_own_namespace(
    service, client, core
):
    credential(service, "m1")
    core.vision.discovery = json.loads(json.dumps(analysis_data()).replace('"f1"', '"m1"'))
    task, _ = await submit(service)
    await once(service)
    response = await client.get(f"/api/v1/tasks/{task['task_id']}/result", headers=HEADERS)
    assert response.status_code == 200
    analysis = MaterialAnalysis.model_validate(response.json()["analysis"])
    assert analysis.materials[0].material_id == "m1"
    assert analysis.facts[0].fact_id != "m1" and "m1" not in analysis.facts[0].fact_id
    assert analysis.facts[0].evidence[0].material_id == "m1"
    assert analysis.subjects[0].material_ids == ["m1"]
    assert analysis.subjects[0].identity_fact_ids == [analysis.facts[0].fact_id]
    assert response.json()["assets"][0]["reference_bindings"][0]["material_id"] == "m1"
    assert analysis.facts[0].fact_id in response.json()["product_attributes"]["subjects"]["s1"]


async def test_provider_identity_aliases_are_stable_across_explicit_resume_and_accept(
    service, client, core, monkeypatch
):
    from image_agent import pipeline
    from image_agent.graph_runtime import resume_pipeline

    secret = "review-provider-key"
    credential(service, secret)
    core.vision.discovery = json.loads(
        json.dumps(analysis_data()).replace('"f1"', json.dumps(secret))
    )
    core.vision.scores = {"main": [{"output_intent": 30}]}
    task, _ = await submit(service)
    await once(service)
    initial = await client.get(f"/api/v1/tasks/{task['task_id']}/result", headers=HEADERS)
    assert initial.status_code == 200 and secret not in initial.text
    fact_id = initial.json()["analysis"]["facts"][0]["fact_id"]
    paid_calls = len(core.generator.calls)

    async def resume(run_dir, config, **kwargs):
        return await resume_pipeline(run_dir, config, dependencies=core, **kwargs)

    monkeypatch.setattr(pipeline, "resume_images", resume)
    await service.recover(task["task_id"])
    await once(service)
    resumed = await client.get(f"/api/v1/tasks/{task['task_id']}/result", headers=HEADERS)
    assert (
        resumed.status_code == 200 and resumed.json()["analysis"]["facts"][0]["fact_id"] == fact_id
    )
    asset = resumed.json()["assets"][0]
    await service.recover(
        task["task_id"],
        "accept",
        {
            "asset_id": asset["asset_id"],
            "candidate_id": asset["candidates"][0]["candidate_id"],
            "reason": "reviewed",
        },
    )
    await once(service)
    accepted = await client.get(f"/api/v1/tasks/{task['task_id']}/result", headers=HEADERS)
    assert accepted.status_code == 200 and accepted.json()["status"] == "accepted"
    assert accepted.json()["analysis"]["facts"][0]["fact_id"] == fact_id
    assert len(core.generator.calls) == paid_calls
    assert secret not in accepted.text


async def test_provider_failed_analysis_cannot_publish_credential_as_untrusted_sha256(
    service, client, core
):
    secret = "0123456789abcdef" * 4
    credential(service, secret)
    data = json.loads(json.dumps(analysis_data()).replace('"m1"', '"provider-material"'))
    data["status"] = "failed"
    data["intent"] = None
    data["materials"][0]["sha256"] = secret
    core.vision.discovery = data
    task, _ = await submit(service, kind="analyze_materials")
    await once(service)
    response = await client.get(f"/api/v1/tasks/{task['task_id']}/result", headers=HEADERS)
    assert response.status_code == 200 and response.json()["status"] == "failed"
    assert secret not in response.text
    assert response.json()["materials"][0]["sha256"] == "[redacted]"


async def test_provider_identity_aliases_are_private_to_persisted_analysis_context(
    service, client, core
):
    secret = "review-provider-key"
    credential(service, secret)
    core.vision.discovery = json.loads(
        json.dumps(analysis_data()).replace('"f1"', json.dumps(secret))
    )
    first, request = await submit(service, kind="analyze_materials")
    second = await service.submit_task(
        "analyze_materials", "config", request, "second-private-context"
    )
    await once(service)
    await once(service)
    first_response = await client.get(f"/api/v1/tasks/{first['task_id']}/result", headers=HEADERS)
    second_response = await client.get(f"/api/v1/tasks/{second['task_id']}/result", headers=HEADERS)
    assert first_response.status_code == second_response.status_code == 200
    first_id = first_response.json()["facts"][0]["fact_id"]
    second_id = second_response.json()["facts"][0]["fact_id"]
    # Public IDs cannot be an unkeyed credential digest shared across independent
    # analysis contexts. Within each persisted context retrieval remains stable.
    assert first_id != second_id
    repeated = await client.get(f"/api/v1/tasks/{first['task_id']}/result", headers=HEADERS)
    assert repeated.json()["facts"][0]["fact_id"] == first_id
    assert secret not in first_response.text + second_response.text


async def test_provider_free_numeric_metadata_cannot_publish_numeric_credentials(
    service, client, core, monkeypatch
):
    task = await garment_task(service, core, monkeypatch, "90210", [{"measurement": 90210}])
    response = await client.get(f"/api/v1/tasks/{task['task_id']}/result", headers=HEADERS)
    assert response.status_code == 200 and "90210" not in response.text
    item = response.json()["product_attributes"]["subjects"]["s1"]["garments"]["items"][0]
    assert item["measurement"] == "[redacted]"


async def test_numeric_credential_keeps_real_budget_counter_as_protocol_metadata(
    service, client, core
):
    from image_agent.execution import reserve_post

    credential(service, "1")
    original = core.generator.generate

    async def counted(**kwargs):
        reserve_post()
        return await original(**kwargs)

    core.generator.generate = counted
    task, _ = await submit(service)
    await once(service)
    response = await client.get(f"/api/v1/tasks/{task['task_id']}/result", headers=HEADERS)
    assert response.status_code == 200 and response.json()["status"] == "succeeded"
    assert response.json()["usage"]["image_calls"] == 1
    assert response.json()["assets"][0]["reference_bindings"][0]["material_id"] == "m1"
