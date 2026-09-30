"""Optional artifact output. No worker thread performs filesystem writes."""

import asyncio
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from .errors import OutputError, make_error_info
from .images import image_format
from .models import OUTPUTS, PLATFORMS


def reserve_output(request):
    if request.output_dir is None:
        return None
    root = request.output_dir.resolve()
    run_name = request.request_id or f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
    directory = root / run_name
    try:
        root.mkdir(parents=True, exist_ok=True)
        directory.mkdir(exist_ok=False)
    except FileExistsError:
        if request.request_id and directory.is_dir():
            raise OutputError(
                "指定的输出目录已存在；省略 --request-id 可自动保存到新目录"
            ) from None
        raise OutputError("输出路径已被占用，请检查输出目录") from None
    except OSError:
        raise OutputError("无法创建输出目录，请检查输出路径和写入权限") from None
    return directory


def atomic_write(path, data):
    path = Path(path)
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_manifest(result, directory):
    directory = Path(directory).resolve()
    manifest = result.model_dump(mode="json")
    for asset in manifest["assets"]:
        value = asset.get("file_path")
        if value:
            path = Path(value)
            if path.is_absolute():
                if not path.resolve().is_relative_to(directory):
                    raise OutputError("asset path escapes run directory")
                value = path.resolve().relative_to(directory).as_posix()
        asset["image"] = asset["file_path"] = value
        for candidate in asset.get("candidates", []):
            path = Path(candidate["file_path"])
            if path.is_absolute():
                candidate["file_path"] = path.resolve().relative_to(directory).as_posix()
    try:
        atomic_write(
            directory / "result.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    except OSError:
        raise OutputError("result.json 保存失败", result=result) from None


async def save_result(result, directory):
    directory = Path(directory).resolve()
    for asset in result.assets:
        if asset.image is None:
            continue
        try:
            if (
                asset.platform not in PLATFORMS
                or asset.output_type not in OUTPUTS
                or asset.variant not in (None, "scene", "feature", "closeup")
            ):
                raise OutputError("invalid output target")
            for candidate in asset.candidates:
                relative = Path(candidate["file_path"])
                path = (directory / relative).resolve()
                if not path.is_relative_to(directory):
                    raise OutputError("candidate path escapes output directory")
                blobs = getattr(result, "_candidate_images", {})
                payload = blobs.get(candidate["file_path"])
                if payload is None and result.run_dir:
                    source = (Path(result.run_dir) / relative).resolve()
                    if source.is_relative_to(Path(result.run_dir).resolve()):
                        payload = source.read_bytes()
                if payload is not None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write(path, payload)
            if asset.candidates and not asset.file_path:
                chosen = next((c for c in asset.candidates if c.get("selected")), None)
                chosen = chosen or next(
                    (c for c in reversed(asset.candidates) if c["status"] != "stage"), None
                )
                if chosen:
                    asset.file_path = str(directory / chosen["file_path"])
            if asset.file_path and asset.candidates:
                # Engine saved every attempt before auditing; preserve its stable paths.
                existing = Path(asset.file_path)
                if not existing.is_absolute():
                    existing = directory / existing
                if existing.resolve().is_relative_to(directory) and existing.is_file():
                    asset.file_path = str(existing.resolve())
                    continue
            bucket = (
                "approved"
                if asset.status == "succeeded"
                else "accepted"
                if asset.status == "accepted"
                else "candidates"
            )
            suffix = image_format(asset.image)[0]
            relative = (
                Path(bucket)
                / asset.platform
                / asset.output_type
                / ((asset.variant or "default") + suffix)
            )
            path = (directory / relative).resolve()
            if not path.is_relative_to(directory):
                raise OutputError("output path escapes reserved directory")
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, asset.image)
            asset.file_path = str(path)
        except (OSError, OutputError):
            asset.file_path = None
            error = OutputError("图片文件保存失败: " + asset.asset_id)
            result.output_errors.append(make_error_info(error))
            result.warnings.append(error.message)
        await asyncio.sleep(0)
    write_manifest(result, directory)
    return result
