"""UI-independent request construction and a cancellable, single-job worker."""

import asyncio
import json
import threading
from collections.abc import Callable, Coroutine, Mapping, Sequence
from pathlib import Path, PureWindowsPath
from queue import Queue
from typing import Any

from pydantic import SecretStr

from .config import AgentConfig
from .models import CreationRequest, CreationResult, ImageSource, MaterialInput


def saved_image_path(run_dir: Path, value: str) -> Path:
    """Resolve a manifest reference without allowing traversal or absolute paths."""
    root = run_dir.resolve()
    relative = Path(value)
    if relative.is_absolute() or PureWindowsPath(value).drive or PureWindowsPath(value).root:
        raise ValueError("图片路径必须位于任务目录内。")
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("图片路径不能超出任务目录。")
    return path


def load_saved_result(run_dir: Path) -> CreationResult:
    """Read v1/v2 local manifests for review; this does not promise resumability."""
    root = Path(run_dir).resolve()
    payload = json.loads((root / "result.json").read_text("utf-8"))
    for asset in payload.get("assets", []):
        image_path = asset.pop("image", None) or asset.get("file_path")
        if image_path:
            path = saved_image_path(root, image_path)
            asset["file_path"] = str(path)
            asset["image"] = path.read_bytes() if path.is_file() else None
        for candidate in asset.get("candidates", []):
            if candidate.get("file_path"):
                saved_image_path(root, candidate["file_path"])
    payload["run_dir"] = str(root)
    return CreationResult.model_validate(payload)


def build_request(paths: Sequence[Path], **fields) -> CreationRequest:
    return CreationRequest(
        materials=[
            MaterialInput(material_id=f"m{i}", source=ImageSource(path=path))
            for i, path in enumerate(paths, 1)
        ],
        **fields,
    )


def with_credentials(config: AgentConfig, keys: Mapping[str, str]) -> AgentConfig:
    """Override only the named services, without changing environment or source config."""
    updated = config.model_copy(deep=True)
    if updated.services:
        for name, key in keys.items():
            if name in updated.services and key.strip():
                updated.services[name].api_key_env = None
                updated.services[name].api_key = SecretStr(key.strip())
    elif keys.get("legacy", "").strip():
        updated.openrouter_api_key = SecretStr(keys["legacy"].strip())
    return updated


class BackgroundJob:
    """Own one asyncio loop per job. Tk only consumes events on its own thread."""

    def __init__(self):
        self.events: Queue[tuple[str, Any]] = Queue()
        self.thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._cancelled = False
        self._loop = None
        self._task = None

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, factory: Callable[[], Coroutine[Any, Any, Any]]):
        with self._lock:
            if self.running:
                raise RuntimeError("已有任务运行，请等待完成或取消。")
            self._cancelled = False
            self.thread = threading.Thread(target=self._run, args=(factory,), daemon=False)
            self.thread.start()

    def cancel(self):
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            if self._loop is not None and self._task is not None:
                self._loop.call_soon_threadsafe(self._task.cancel)

    def report_progress(self, event: Mapping[str, Any]):
        self.events.put(("progress", dict(event)))

    def _run(self, factory):
        async def invoke():
            with self._lock:
                self._loop = asyncio.get_running_loop()
                self._task = asyncio.current_task()
                cancelled = self._cancelled
            if cancelled:
                raise asyncio.CancelledError
            return await factory()

        try:
            # Runner also waits for asynchronous generators and executor cleanup.
            with asyncio.Runner() as runner:
                try:
                    outcome = ("result", runner.run(invoke()))
                finally:
                    with self._lock:
                        self._loop = self._task = None
        except asyncio.CancelledError:
            outcome = ("cancelled", None)
        except Exception as error:
            outcome = ("error", error)
        self.events.put(outcome)
