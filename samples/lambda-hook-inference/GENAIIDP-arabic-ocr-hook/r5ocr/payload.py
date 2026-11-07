"""Parse the IDP LambdaHook request (see docs/lambda-hook-inference.md)."""

import base64
import io
from dataclasses import dataclass
from urllib.parse import urlparse

from PIL import Image


@dataclass(frozen=True)
class HookRequest:
    system_prompt: str
    task_prompt: str
    image_bytes: bytes
    image_format: str
    width: int
    height: int


def _text(items) -> str:
    return "\n".join(
        i["text"] for i in (items or []) if isinstance(i, dict) and "text" in i
    )


def parse_request(event: dict, s3_client) -> HookRequest:
    content = (event.get("messages") or [{}])[0].get("content") or []
    image = next(
        (c["image"] for c in content if isinstance(c, dict) and "image" in c), None
    )
    if not image:
        raise ValueError("LambdaHook request has no image content")
    source = image.get("source") or {}
    if "bytes" in source:
        raw = source["bytes"]
        data = base64.b64decode(raw) if isinstance(raw, str) else bytes(raw)
    elif "s3Location" in source:
        uri = urlparse(source["s3Location"]["uri"])
        data = s3_client.get_object(Bucket=uri.netloc, Key=uri.path.lstrip("/"))[
            "Body"
        ].read()
    else:
        raise ValueError("LambdaHook image has neither bytes nor s3Location")
    with Image.open(io.BytesIO(data)) as im:
        width, height = im.size
    return HookRequest(
        _text(event.get("system")),
        _text([c for c in content if "text" in c]),
        data,
        image.get("format", "jpeg"),
        width,
        height,
    )
