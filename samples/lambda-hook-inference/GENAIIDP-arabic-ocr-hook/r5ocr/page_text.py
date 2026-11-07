"""The same full-page OCR call the IDP's Bedrock OCR backend makes (same prompts, same image)."""

from .llm import Usage, converse
from .payload import HookRequest
from .settings import Settings


def transcribe_page(client, settings: Settings, req: HookRequest) -> tuple[str, Usage]:
    content = [
        {"text": req.task_prompt},
        {"image": {"format": req.image_format, "source": {"bytes": req.image_bytes}}},
    ]
    return converse(
        client,
        settings.reader_model_id,
        req.system_prompt,
        content,
        settings.reader_effort,
        settings.max_output_tokens,
    )
