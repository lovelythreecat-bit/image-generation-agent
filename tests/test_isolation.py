import ast
import json
import os
import subprocess
import sys
from pathlib import Path


def test_runtime_import_boundaries():
    root = Path(__file__).resolve().parents[1] / "src/image_agent"
    prohibited = {
        "mediaforge",
        "celery",
        "langchain",
        "minio",
        "milvus",
        "sqlalchemy",
        "openrouter",
        "dotenv",
    }
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            assert not prohibited.intersection(names), path.name


def test_import_has_no_network_or_file_writes(tmp_path):
    script = """
import builtins, os, socket
def denied(*a, **kw): raise AssertionError("import caused I/O")
socket.socket.connect = denied
os.mkdir = denied
original = builtins.open
def opened(file, mode="r", *a, **kw):
    if any(c in mode for c in "wax+"): denied()
    return original(file, mode, *a, **kw)
builtins.open = opened
import image_agent
assert callable(image_agent.create_images)
"""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert not list(tmp_path.iterdir())


def test_schema_files_are_current_and_external():
    from image_agent import CreationRequestDTO, CreationResultDTO, ErrorDTO, MaterialAnalysis

    root = Path(__file__).resolve().parents[1] / "schema"
    for name, model in (
        ("creation-request-v1", CreationRequestDTO),
        ("creation-result-v2", CreationResultDTO),
        ("material-analysis-v1", MaterialAnalysis),
        ("error-v1", ErrorDTO),
    ):
        assert json.loads((root / (name + ".json")).read_text("utf-8")) == model.model_json_schema()
    request_schema = json.dumps(CreationRequestDTO.model_json_schema())
    assert '"output_dir"' not in request_schema and '"ImageSource"' not in request_schema
    historical = json.loads((root / "creation-result-v1.json").read_text("utf-8"))
    assert historical["properties"]["schema_version"]["const"] == "1.0"
