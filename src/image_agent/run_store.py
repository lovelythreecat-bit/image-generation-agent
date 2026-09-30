"""Versioned input/candidate storage, crash journal and exclusive OS run locks."""

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path

from .errors import AgentError, ConfigurationError, OutputError, ProviderError
from .models import CreationRequest, LoadedMaterial
from .output import atomic_write


def digest(data):
    return hashlib.sha256(data).hexdigest()


def config_fingerprint(config):
    payload = config.model_dump(
        mode="json",
        exclude={
            "openrouter_api_key",
            "http_proxy",
            "request_timeout_seconds",
            "model_concurrency",
            "services",
        },
    )
    payload["services"] = {
        name: service.model_dump(mode="json", exclude={"api_key", "api_key_env"})
        for name, service in config.services.items()
    }
    return digest(json.dumps(payload, sort_keys=True).encode())


class RunStore:
    def __init__(self, directory=None):
        self.directory = Path(directory).resolve() if directory else None
        self.memory = {}
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def lock(self):
        if not self.directory:
            yield
            return
        stream = (self.directory / ".run.lock").open("a+b")
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise AgentError("run is already active", code="run_locked") from None
            else:
                import fcntl

                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise AgentError("run is already active", code="run_locked") from None
            yield
        finally:
            stream.close()

    def path(self, relative):
        if not self.directory:
            return None
        path = (self.directory / relative).resolve()
        if not path.is_relative_to(self.directory):
            raise OutputError("persisted path escapes run directory")
        return path

    def write(self, relative, data):
        if self.directory:
            path = self.path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, data)
        else:
            self.memory[relative] = data
        return {"path": relative, "sha256": digest(data)}

    def remove(self, relative):
        if self.directory:
            self.path(relative).unlink(missing_ok=True)
        else:
            self.memory.pop(relative, None)

    def read(self, ref):
        try:
            data = (
                self.path(ref["path"]).read_bytes() if self.directory else self.memory[ref["path"]]
            )
        except (OSError, KeyError):
            raise OutputError("persisted file missing; integrity validation failed") from None
        if digest(data) != ref["sha256"]:
            raise OutputError("persisted file integrity validation failed")
        return data

    def json(self, name, value=None):
        if value is not None:
            self.write(name, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))
            return value
        try:
            data = self.path(name).read_bytes() if self.directory else self.memory[name]
            return json.loads(data)
        except (OSError, KeyError, ValueError):
            raise OutputError("run metadata missing or corrupt") from None

    def exists(self, name):
        return self.path(name).exists() if self.directory else name in self.memory

    def save_inputs(self, request, materials, config, policy, analysis):
        refs = []
        payload = request.model_dump(mode="json")
        for material in materials:
            ref = self.write(
                f"inputs/{material.order:02d}-{material.material_id}.bin", material.data
            )
            refs.append(material.model_dump(mode="json") | ref)
        sources = [
            {"path": str(self.path(r["path"])) if self.directory else r["path"]} for r in refs
        ]
        if request.materials:
            for material, source in zip(payload["materials"], sources):
                material["source"] = source
        else:
            payload["product_image"], payload["reference_images"] = sources[0], sources[1:]
        self.json(
            "run.json",
            {
                "version": 2,
                "request": payload,
                "inputs": refs,
                "config_fingerprint": config_fingerprint(config),
                "policy": policy.model_dump(mode="json"),
                "analysis": analysis.model_dump(mode="json") if analysis is not None else None,
            },
        )

    def load_metadata(self, config=None):
        data = self.json("run.json")
        if data.get("version") != 2:
            raise AgentError("only version 2 runs can be resumed")
        if config and config_fingerprint(config) != data["config_fingerprint"]:
            raise ConfigurationError(
                "execution configuration changed; restore model routes and capacities"
            )
        for ref in data["inputs"]:
            self.read(ref)
        return data

    def materials(self):
        return tuple(
            LoadedMaterial(
                **{key: ref[key] for key in ("material_id", "sha256", "order", "role_hint")},
                data=self.read(ref),
            )
            for ref in self.json("run.json")["inputs"]
        )

    def request(self):
        return CreationRequest.model_validate(self.json("run.json")["request"])

    def mark_operation(self, operation, status, **extra):
        journal = self.json("operations.json") if self.exists("operations.json") else {}
        current = journal.get(operation, {})
        if status == "submitted":
            extra["submissions"] = current.get("submissions", 0) + 1
        journal[operation] = current | {"status": status} | extra
        self.json("operations.json", journal)

    def operation(self, operation):
        return (
            self.json("operations.json").get(operation) if self.exists("operations.json") else None
        )

    def validate_operations(self, ledger):
        expected = ledger.image_operations()
        if not expected:
            return
        try:
            journal = self.json("operations.json")
            for operation, count in expected.items():
                row = journal.get(operation)
                if not operation or not isinstance(row, dict) or row.get("submissions") != count:
                    raise ValueError
                if row.get("status") not in ("submitted", "saved", "failed"):
                    raise ValueError
                if row["status"] == "saved" and not isinstance(row.get("raw"), dict):
                    raise ValueError
        except (OutputError, ValueError, AttributeError, TypeError):
            raise ProviderError(
                "generation operation journal missing or inconsistent; cannot resume safely",
                code="generation_uncertain",
                kind="transport",
            ) from None

    def validate_candidates(self, state):
        for asset in state["result"]["assets"]:
            for candidate in asset.get("candidates", []):
                self.read({"path": candidate["file_path"], "sha256": candidate["sha256"]})
        if state.get("raw"):
            self.read(state["raw"])
