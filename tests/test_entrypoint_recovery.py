import json
import threading

import pytest

from tests.test_output import result


@pytest.mark.parametrize(
    "status,code",
    [
        ("accepted", 4),
        ("audit_error", 5),
        ("pending_audit", 5),
        ("quality_failed", 1),
        ("budget_exhausted", 6),
        ("generation_uncertain", 7),
    ],
)
def test_resume_cli_reports_recovery_status(tmp_path, monkeypatch, capsys, status, code):
    from image_agent import __main__ as cli

    async def resume(directory, config=None):
        assert directory == tmp_path
        r = result()
        r.status = r.assets[0].status = status
        return r

    monkeypatch.setattr(cli, "resume_images", resume, raising=False)
    assert cli.run_cli(["resume", str(tmp_path)]) == code
    assert status in capsys.readouterr().out


def test_accept_cli_requires_explicit_nonempty_reason(tmp_path, monkeypatch):
    from image_agent import __main__ as cli

    calls = []

    async def accept(directory, asset_id, candidate_id, *, reason):
        calls.append((directory, asset_id, candidate_id, reason))
        r = result()
        r.status = r.assets[0].status = "accepted"
        return r

    monkeypatch.setattr(cli, "accept_candidate", accept, raising=False)
    args = ["accept", str(tmp_path), "--asset", "a1", "--candidate", "c0001"]
    assert cli.run_cli(args) == 1
    assert cli.run_cli(args + ["--reason", "  "]) == 1
    assert not calls
    assert cli.run_cli(args + ["--reason", " Reviewed appearance "]) == 4
    assert calls == [(tmp_path, "a1", "c0001", "Reviewed appearance")]


def test_v2_bundle_keeps_candidates_without_local_paths_or_accepted_blobs():
    from image_agent.contracts import result_to_bundle

    r = result()
    # model_copy permits testing the consumer contract before the domain upgrade lands.
    r = r.model_copy(
        update={"schema_version": "2.0", "run_dir": "C:/private/run", "usage": {"image_calls": 2}}
    )
    r.assets[0] = r.assets[0].model_copy(
        update={
            "status": "accepted",
            "stop_reason": "manual_acceptance",
            "candidates": [
                {
                    "candidate_id": "c0001",
                    "index": 1,
                    "status": "quality_failed",
                    "file_path": "C:/private/run/candidates/a.png",
                    "repair_prompt": "fix blur",
                }
            ],
        }
    )
    bundle = result_to_bundle(r)
    assert not bundle.blobs
    data = bundle.dto.model_dump(mode="json")
    assert data["schema_version"] == "2.0"
    assert data["assets"][0]["candidates"][0]["candidate_id"] == "c0001"
    assert data["assets"][0]["stop_reason"] == "manual_acceptance"
    assert "C:/private" not in json.dumps(data)


def test_worker_progress_is_queued_without_invoking_tk():
    from image_agent.desktop_runtime import BackgroundJob

    job = BackgroundJob()

    async def work():
        event = {
            "stage": "audit",
            "asset_id": "a",
            "attempt": 2,
            "message": "checking",
            "run_dir": "run",
        }
        job.report_progress(event)
        event["stage"] = "modified"
        return threading.get_ident()

    job.start(work)
    kind, progress = job.events.get(timeout=3)
    assert kind == "progress" and progress["stage"] == "audit"
    assert job.events.get(timeout=3)[0] == "result"
    job.thread.join(3)


def test_saved_v1_result_loads_preview_and_rejects_path_escape(tmp_path):
    from image_agent.desktop_runtime import load_saved_result

    r = result()
    payload = r.model_dump(mode="json")
    payload["schema_version"] = "1.0"
    (tmp_path / "preview.jpg").write_bytes(r.assets[0].image)
    payload["assets"][0].update(image="preview.jpg", file_path="preview.jpg")
    (tmp_path / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_saved_result(tmp_path)
    assert loaded.assets[0].image == r.assets[0].image
    assert loaded.run_dir == str(tmp_path.resolve())
    payload["assets"][0].update(image="../outside.jpg", file_path="../outside.jpg")
    (tmp_path / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="任务目录"):
        load_saved_result(tmp_path)
