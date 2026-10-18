"""Image validation and storage confined to a deployment-owned directory."""

import hashlib
import io
import os
import uuid
import warnings
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from image_agent.output import atomic_write

from .settings import ServerError


class MediaStore:
    def __init__(self, data_dir, max_upload_bytes=20 * 1024 * 1024, max_image_pixels=60_000_000):
        self.root = Path(data_dir).resolve()
        self.max_upload_bytes = max_upload_bytes
        self.max_image_pixels = max_image_pixels

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("media", "tasks"):
            self.path(name).mkdir(exist_ok=True)

    def ready(self):
        return self.root.is_dir() and os.access(self.root, os.W_OK)

    def path(self, ref):
        relative = Path(ref)
        path = (self.root / relative).resolve()
        if relative.is_absolute() or ":" in str(ref) or not path.is_relative_to(self.root):
            raise ServerError(404, "not_found", "Stored media was not found")
        return path

    def read(self, ref):
        try:
            return self.path(ref).read_bytes()
        except OSError:
            raise ServerError(404, "not_found", "Stored media was not found") from None

    def store(self, data):
        if len(data) > self.max_upload_bytes:
            raise ServerError(413, "upload_too_large", "Image exceeds the upload limit")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    if image.width * image.height > self.max_image_pixels:
                        raise ServerError(413, "image_too_large", "Image exceeds the pixel limit")
                    if image.format not in ("PNG", "JPEG", "WEBP", "GIF"):
                        raise ServerError(422, "invalid_image", "Image format is unsupported")
                    mime = Image.MIME[image.format]
                    image.verify()
                with Image.open(io.BytesIO(data)) as image:
                    image.load()
        except (Image.DecompressionBombWarning, Image.DecompressionBombError):
            raise ServerError(413, "image_too_large", "Image exceeds the pixel limit") from None
        except (OSError, ValueError, UnidentifiedImageError):
            raise ServerError(422, "invalid_image", "Upload is not a valid image") from None
        identifier = str(uuid.uuid4())
        ref = f"media/{identifier}.bin"
        try:
            atomic_write(self.path(ref), data)
        except OSError:
            raise ServerError(503, "storage_unavailable", "Media storage is unavailable") from None
        return {
            "id": identifier,
            "storage_ref": ref,
            "mime_type": mime,
            "byte_size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def task_run_ref(self, task_id):
        return f"tasks/{uuid.UUID(task_id)}/run"

    def save_bundle(self, task_id, bundle):
        refs = {}
        for blob_id, data in bundle.blobs.items():
            # Asset identifiers are opaque metadata, never path fragments.
            name = hashlib.sha256(blob_id.encode()).hexdigest()
            ref = f"tasks/{uuid.UUID(task_id)}/blobs/{name}.bin"
            path = self.path(ref)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, data)
            refs[blob_id] = {"storage_ref": ref, "sha256": hashlib.sha256(data).hexdigest()}
        return {"dto": bundle.dto.model_dump(mode="json"), "blobs": refs}
