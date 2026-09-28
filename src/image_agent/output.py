"""Optional artifact output. No worker thread performs filesystem writes."""

import asyncio
import json
import os
import uuid
from pathlib import Path

from .errors import OutputError, make_error_info
from .images import image_format
from .models import OUTPUTS, PLATFORMS


def reserve_output(request):
    if request.output_dir is None:
        return None
    root = request.output_dir.resolve()
    directory = root / (request.request_id or "create")
    try:
        root.mkdir(parents=True, exist_ok=True)
        directory.mkdir(exist_ok=False)
    except OSError:
        raise OutputError("输出运行目录已存在或无法创建，请使用新的 request_id") from None
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


async def save_result(result, directory):
    directory = Path(directory).resolve()
    paths = {}
    for asset in result.assets:
        asset.file_path = None
        if asset.status != "succeeded" or asset.image is None:
            continue
        try:
            if (
                asset.platform not in PLATFORMS
                or asset.output_type not in OUTPUTS
                or asset.variant not in (None, "scene", "feature", "closeup")
            ):
                raise OutputError("invalid output target")
            suffix = image_format(asset.image)[0]
            relative = (
                Path(asset.platform) / asset.output_type / (asset.variant + suffix)
                if asset.variant
                else Path(asset.platform) / (asset.output_type + suffix)
            )
            path = (directory / relative).resolve()
            if not path.is_relative_to(directory):
                raise OutputError("output path escapes reserved directory")
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, asset.image)
            asset.file_path = str(path)
            paths[asset.asset_id] = relative.as_posix()
        except (OSError, OutputError):
            error = OutputError("图片文件保存失败: " + asset.asset_id)
            result.output_errors.append(make_error_info(error))
            result.warnings.append(error.message)
        await asyncio.sleep(0)
    manifest = result.model_dump(mode="json")
    for asset in manifest["assets"]:
        asset["image"] = asset["file_path"] = paths.get(asset["asset_id"])
    try:
        atomic_write(
            directory / "result.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    except OSError:
        raise OutputError("result.json 保存失败", result=result) from None
    return result
