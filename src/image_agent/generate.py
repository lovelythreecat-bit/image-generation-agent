import base64
import binascii
from urllib.parse import urlsplit

from .errors import ProviderError
from .image_protocols import generate_image, image_endpoint
from .images import generated_image
from .transport import compute, model_permit


def _image_value_diagnostic(value):
    # Describe the wire shape, never echo response content or signed image URLs.
    if not isinstance(value, str):
        return f"不是字符串 (non_string); type={type(value).__name__}"
    stripped = value.strip()
    if not stripped:
        reason = "内容为空 (empty)"
    elif stripped.startswith("data:image/"):
        reason = "含图片数据前缀 (data_url)"
    elif stripped.startswith(("https://", "http://")):
        reason = "返回图片网址，未下载 (remote_url)"
    elif any(char.isspace() for char in value):
        reason = "含空白或换行 (whitespace)"
    else:
        reason = "编码或填充无效 (invalid_encoding)"
    return f"{reason}; type=str; chars={len(value)}; mod4={len(value) % 4}"


def extract_image_bytes(payload):
    return _extract_image_source(payload, allow_remote=False)


def _extract_image_source(payload, *, allow_remote):
    def decode(value, location, *, sibling_url=False):
        try:
            if not isinstance(value, str):
                raise ValueError
            result = base64.b64decode(value, validate=True)
            if not result:
                raise ValueError
            return result
        except (ValueError, binascii.Error):
            raise ProviderError(
                f"invalid image base64; {location}: {_image_value_diagnostic(value)}"
                + ("; 同时包含图片网址 (sibling_url=True)" if sibling_url else "")
            ) from None

    remote_image = None
    remote_source = None
    try:
        for index, item in enumerate(payload.get("data") or []):
            inline = item.get("b64_json")
            empty_with_url = (
                allow_remote and isinstance(inline, str) and not inline.strip() and item.get("url")
            )
            if inline is not None and not empty_with_url:
                return decode(
                    item["b64_json"],
                    f"data[{index}].b64_json",
                    sibling_url=bool(item.get("url")),
                )
            if item.get("url"):
                remote_image = f"data[{index}].url: {_image_value_diagnostic(item['url'])}"
                if remote_source is None:
                    remote_source = item["url"]
        messages = [c["message"] for c in payload.get("choices", [])]
        for field in ("images", "content"):
            for choice_index, message in enumerate(messages):
                blocks = message.get(field) or []
                if not isinstance(blocks, list):
                    continue
                for block_index, item in enumerate(blocks):
                    if item.get("type") == "image_url":
                        url = item["image_url"]["url"]
                        if (
                            isinstance(url, str)
                            and url.startswith("data:image/")
                            and ";base64," in url
                        ):
                            return decode(
                                url.split(",", 1)[1],
                                f"choices[{choice_index}].message.{field}[{block_index}].image_url",
                            )
                        remote_image = (
                            f"choices[{choice_index}].message.{field}[{block_index}].image_url: "
                            + _image_value_diagnostic(url)
                        )
                        if remote_source is None:
                            remote_source = url
    except (TypeError, AttributeError, KeyError):
        raise ProviderError("malformed image response") from None
    if allow_remote and isinstance(remote_source, str) and remote_source:
        return remote_source
    raise ProviderError(
        "response contains no inline image"
        + (f"; {remote_image}" if remote_image else "; 缺少内嵌图片字段或字段为空")
    )


class ImageGenerator:
    def __init__(self, transport, config, *, encoder, alias=None):
        self.transport, self.config, self.encoder = transport, config, encoder
        self.alias = alias

    async def generate(self, *, prompt, model, size, aspect_ratio, references):
        if len(references) > self.config.generation_reference_limit:
            raise ProviderError(
                "required references exceed generation capacity",
                kind="capability",
                code="reference_capacity_exceeded",
            )
        service, transport = None, self.transport
        if self.config.services:
            routes = (
                [self.config.image_models[self.alias]]
                if self.alias
                else [r for r in self.config.image_models.values() if r.model == model]
            )
            if not routes or len({r.service for r in routes}) != 1:
                from .errors import ConfigurationError

                raise ConfigurationError("image model requires an unambiguous configured alias")
            route = routes[0]
            service = self.config.services[route.service]
            transport = self.transport.for_service(route.service)
        permit = (service.base_url, model) if service else model
        async with model_permit(permit, self.config.model_concurrency):
            images, notes = [], []
            for i, ref in enumerate(references, 1):
                image = await compute(self.encoder, ref.data, 1024)
                images.append(image)
                notes.append(
                    f"Reference Image {i}: {ref.material_id or ref.stage_id}; purpose={ref.role}; elements={','.join(ref.element_ids)}. Use only the assigned facts and purpose; scene/style images never replace product identity."
                )
            response = await generate_image(
                transport,
                service,
                model=model,
                prompt="\n\n".join([*notes, prompt]),
                size=size,
                aspect_ratio=aspect_ratio,
                images=images,
            )
            try:
                source = _extract_image_source(response, allow_remote=True)
                if isinstance(source, bytes):
                    return source
                data = await self.transport.download_image(source)
                await compute(generated_image, data)
                return data
            except ProviderError as error:
                base_url = service.base_url if service else self.config.openrouter_base_url
                address = urlsplit(base_url)
                endpoint, body_format = image_endpoint(service, bool(images))
                path = (address.path.rstrip("/") + endpoint).strip("/").replace("/", " > ")
                protocol = service.protocol if service else "openrouter_images"
                context = (
                    f"POST {address.hostname}; {path}; {body_format}; "
                    f"protocol={protocol}; model={model}; refs={len(images)}"
                )
                raise ProviderError(
                    f"{error.message}\n请求：{context}",
                    code=error.code,
                    kind=error.kind,
                    status_code=error.status_code,
                ) from None
