import base64
import binascii

from .errors import ProviderError
from .transport import compute, model_permit


def extract_image_bytes(payload):
    def decode(value):
        if not isinstance(value, str):
            raise ProviderError("invalid image base64")
        try:
            result = base64.b64decode(value, validate=True)
            if not result:
                raise ValueError
            return result
        except (ValueError, binascii.Error):
            raise ProviderError("invalid image base64") from None

    try:
        for item in payload.get("data") or []:
            if item.get("b64_json") is not None:
                return decode(item["b64_json"])
        messages = [c["message"] for c in payload.get("choices", [])]
        for field in ("images", "content"):
            for message in messages:
                blocks = message.get(field) or []
                if not isinstance(blocks, list):
                    continue
                for item in blocks:
                    if item.get("type") == "image_url":
                        url = item["image_url"]["url"]
                        if (
                            isinstance(url, str)
                            and url.startswith("data:image/")
                            and ";base64," in url
                        ):
                            return decode(url.split(",", 1)[1])
    except (TypeError, AttributeError, KeyError):
        raise ProviderError("malformed image response") from None
    raise ProviderError("response contains no inline image")


class ImageGenerator:
    def __init__(self, transport, config, *, encoder):
        self.transport, self.config, self.encoder = transport, config, encoder

    async def generate(self, *, prompt, model, size, aspect_ratio, references):
        if len(references) > self.config.generation_reference_limit:
            raise ProviderError(
                "required references exceed generation capacity",
                kind="capability",
                code="reference_capacity_exceeded",
            )
        async with model_permit(model, self.config.model_concurrency):
            images, notes = [], []
            for i, ref in enumerate(references, 1):
                image = await compute(self.encoder, ref.data, 1024)
                images.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64," + base64.b64encode(image).decode()
                        },
                    }
                )
                notes.append(
                    f"Reference Image {i}: {ref.material_id or ref.stage_id}; purpose={ref.role}; elements={','.join(ref.element_ids)}. Use only the assigned facts and purpose; scene/style images never replace product identity."
                )
            payload = dict(
                model=model, prompt="\n\n".join([*notes, prompt]), n=1, aspect_ratio=aspect_ratio
            )
            if images:
                payload["input_references"] = images
            if model == "openai/gpt-image-2":
                payload["quality"] = {"1K": "low", "2K": "medium", "4K": "high"}[size]
            else:
                payload["resolution"] = size
            return extract_image_bytes(await self.transport.post_json("/images", payload))
