"""Abrupt process death and fresh-interpreter recovery through the real graph."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from tests.test_images import picture

_OFFLINE_CLIENTS = r"""
import asyncio
import json
import os
import socket
import sys
from pathlib import Path

# Asyncio's local socketpair is allowed; all actual outbound sockets are denied.
_original_connect = socket.socket.connect

def deny_network(sock, *args, **kwargs):
    caller = sys._getframe(1)
    if caller.f_globals.get('__name__') == 'socket' and 'socketpair' in caller.f_code.co_name:
        return _original_connect(sock, *args, **kwargs)
    raise AssertionError('subprocess recovery tests must not use network')

socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network

from image_agent.config import AgentConfig
from image_agent.execution import ExecutionPolicy, reserve_post
from image_agent.graph_runtime import resume_pipeline
from image_agent.models import CreationRequest
from image_agent.pipeline import Dependencies, run_pipeline
from tests.fakes import FakeGenerator, FakeVision

class BudgetedVision(FakeVision):
    async def analyze_materials(self, *args):
        reserve_post()
        return await super().analyze_materials(*args)

    async def validate_evidence(self, *args):
        reserve_post()
        return await super().validate_evidence(*args)

    async def audit_image(self, *args):
        reserve_post()
        return await super().audit_image(*args)

class BudgetedGenerator(FakeGenerator):
    async def generate(self, **kwargs):
        # Exercise the real durable submission accounting without contacting a provider.
        reserve_post()
        return await super().generate(**kwargs)

async def load_local(source):
    assert source.path is not None and source.url is None
    return source.path.read_bytes()
"""

_CRASH_AFTER_SAVE = r"""
source, output = map(Path, sys.argv[1:])
request = CreationRequest(
    product_name='Cup', category='kitchen', platforms=['taobao'],
    output_types=['main_image'], request_id='process-recovery', output_dir=output,
    materials=[{'material_id': 'm1', 'source': {'path': source}}],
)

def crash_after_candidate_saved(event):
    if event['stage'] == 'audit':
        # This bypasses finally, graceful cancellation, and SQLite connection cleanup.
        os._exit(23)

asyncio.run(run_pipeline(
    request, AgentConfig(),
    dependencies=Dependencies(vision=BudgetedVision(), generator=BudgetedGenerator(), load=load_local),
    policy=ExecutionPolicy(max_image_calls=1, max_vision_calls=2),
    on_progress=crash_after_candidate_saved,
))
raise AssertionError('the process never reached the saved-candidate audit boundary')
"""

_RESUME_IN_FRESH_PROCESS = r"""
class AuditOnlyVision(BudgetedVision):
    async def analyze_materials(self, *args):
        raise AssertionError('resume must not repeat discovery')

    async def validate_evidence(self, *args):
        raise AssertionError('resume must not repeat input validation')

class ForbiddenGenerator:
    async def generate(self, **kwargs):
        raise AssertionError('saved candidate must never be generated again')

async def forbidden_load(source):
    raise AssertionError('resume must use persisted inputs, not original input loading')

result = asyncio.run(resume_pipeline(
    Path(sys.argv[1]), AgentConfig(),
    dependencies=Dependencies(vision=AuditOnlyVision(), generator=ForbiddenGenerator(), load=forbidden_load),
))
print(json.dumps(result.model_dump(mode='json')))
"""


def _fresh_python(script, arguments, tmp_path):
    root = Path(__file__).resolve().parents[1]
    # Pass only OS/runtime environment; never inherit provider or tracing credentials.
    environment = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH")
        if key in os.environ
    }
    environment.update(
        PYTHONPATH=os.pathsep.join((str(root / "src"), str(root))),
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
    )
    return subprocess.run(
        [sys.executable, "-c", _OFFLINE_CLIENTS + script, *map(str, arguments)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=45,
        check=False,
    )


def _budget_rows(run_dir):
    with sqlite3.connect(run_dir / "budget.sqlite") as connection:
        return dict(connection.execute("SELECT kind, used FROM budget"))


def test_abrupt_process_exit_after_save_resumes_audit_without_regeneration(tmp_path):
    source = tmp_path / "source.png"
    source.write_bytes(picture())
    output = tmp_path / "runs"
    crashed = _fresh_python(_CRASH_AFTER_SAVE, (source, output), tmp_path)
    assert crashed.returncode == 23, crashed.stdout + crashed.stderr

    runs = list(output.iterdir())
    assert len(runs) == 1
    run_dir = runs[0]
    state = json.loads((run_dir / "state.json").read_text("utf-8"))
    assert state["phase"] == "audit"
    asset = state["result"]["assets"][0]
    assert asset["status"] == "pending_audit"
    assert len(asset["candidates"]) == len(asset["attempts"]) == 1
    candidate = asset["candidates"][0]
    assert candidate["status"] == "pending_audit" and candidate["quality"] is None
    saved_candidate = (run_dir / candidate["file_path"]).read_bytes()
    assert hashlib.sha256(saved_candidate).hexdigest() == candidate["sha256"]
    assert state["result"]["usage"] == {"image_calls": 1, "vision_calls": 1}
    assert _budget_rows(run_dir) == {"image": 1, "vision": 1}
    assert (run_dir / "checkpoints.sqlite").is_file()
    assert not list((run_dir / "approved").glob("**/*.*"))

    # Original sources need not remain available in the new process.
    source.unlink()
    resumed = _fresh_python(_RESUME_IN_FRESH_PROCESS, (run_dir,), tmp_path)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    result = json.loads(resumed.stdout)
    assert result["status"] == result["assets"][0]["status"] == "succeeded"
    assert result["usage"] == {"image_calls": 1, "vision_calls": 2}
    assert _budget_rows(run_dir) == {"image": 1, "vision": 2}
    approved = result["assets"][0]
    assert len(approved["candidates"]) == len(approved["attempts"]) == 1
    assert approved["candidates"][0]["candidate_id"] == candidate["candidate_id"]
    assert approved["candidates"][0]["quality"]["passed"]
    approved_path = Path(approved["file_path"])
    assert approved_path.is_relative_to(run_dir / "approved")
    assert approved_path.read_bytes() == saved_candidate
    final_state = json.loads((run_dir / "state.json").read_text("utf-8"))
    assert final_state["phase"] == "done"
    assert final_state["result"]["usage"] == result["usage"]
