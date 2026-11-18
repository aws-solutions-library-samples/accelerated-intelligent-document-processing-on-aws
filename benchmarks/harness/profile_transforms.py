"""Profile transforms for ablations a dotted-path override cannot express.

An ablation's `transform` list names one function here per value (or null).
run_guidance_ablation.py calls it on the profile after the baseline and the value's
`set` overrides, and the function edits the profile in place.
"""

from __future__ import annotations

import re
from typing import Any


def _strip_property_descriptions(schema: Any) -> int:
    """Remove `description` from every field of a JSON Schema; return how many."""
    removed = 0
    if isinstance(schema, dict):
        for key in ("properties", "$defs"):
            for sub in (schema.get(key) or {}).values():
                if isinstance(sub, dict):
                    if key == "properties" and sub.pop("description", None) is not None:
                        removed += 1
                    removed += _strip_property_descriptions(sub)
        if isinstance(schema.get("items"), dict):
            removed += _strip_property_descriptions(schema["items"])
    return removed


def strip_field_descriptions(cfg: dict[str, Any]) -> None:
    """Field names only: drop every field's description, keep the class descriptions."""
    removed = sum(_strip_property_descriptions(c) for c in cfg.get("classes") or [])
    if not removed:
        raise SystemExit(
            "strip_field_descriptions: the profile has no field descriptions"
        )


_IMAGE_BLOCK = re.compile(
    r"\s*<document_image>\s*\{DOCUMENT_IMAGE\}\s*</document_image>"
)


def drop_extraction_images(cfg: dict[str, Any]) -> None:
    """Text only: take {DOCUMENT_IMAGE} out of every extraction task prompt.

    Single-pass extraction attaches page images only where its task prompt holds
    the placeholder (ExtractionService._build_extraction_content), so removing it
    sends the OCR text alone. The confidence step's prompts are left as they are.
    """
    extraction = cfg.get("extraction") or {}
    removed = 0
    for key in (
        "task_prompt",
        "task_prompt_extraction_with_confidence",
        "task_prompt_extraction_with_confidence_topk",
    ):
        text = extraction.get(key)
        if isinstance(text, str) and "{DOCUMENT_IMAGE}" in text:
            text = _IMAGE_BLOCK.sub("", text).replace("{DOCUMENT_IMAGE}", "")
            extraction[key] = text
            removed += 1
    if "{DOCUMENT_IMAGE}" in str(extraction.get("task_prompt", "")) or not removed:
        raise SystemExit(
            "drop_extraction_images: no {DOCUMENT_IMAGE} in the task prompt"
        )


_TEXT_BLOCK = re.compile(r"\s*<document-text>\s*\{DOCUMENT_TEXT\}\s*</document-text>")


def drop_extraction_text(cfg: dict[str, Any]) -> None:
    """Image only: take {DOCUMENT_TEXT} out of every extraction task prompt.

    Extraction sends a page's OCR text only where its task prompt holds the
    placeholder (ExtractionService._substitutions), so removing it sends the page
    images alone. Classification and the confidence step still read the text.
    """
    extraction = cfg.get("extraction") or {}
    removed = 0
    for key in (
        "task_prompt",
        "task_prompt_extraction_with_confidence",
        "task_prompt_extraction_with_confidence_topk",
    ):
        text = extraction.get(key)
        if isinstance(text, str) and "{DOCUMENT_TEXT}" in text:
            text = _TEXT_BLOCK.sub("", text).replace("{DOCUMENT_TEXT}", "")
            extraction[key] = text
            removed += 1
    task = str(extraction.get("task_prompt", ""))
    if "{DOCUMENT_TEXT}" in task or not removed:
        raise SystemExit("drop_extraction_text: no {DOCUMENT_TEXT} in the task prompt")
    if "{DOCUMENT_IMAGE}" not in task:
        raise SystemExit(
            "drop_extraction_text: the task prompt has no {DOCUMENT_IMAGE}"
        )


# One-line class descriptions for the DocSplit (RVL-CDIP) classes, written for what a
# scanned page looks like. They are the descriptions the Strands Decider's own
# RVL-CDIP-N benchmark used, plus one line each for the preset's two extra classes.
SHORT_DOCSPLIT_DESCRIPTIONS = {
    "budget": "a budget, financial statement or table of costs and appropriations",
    "email": "an email message or printed email thread, with From/To/Subject headers",
    "form": "a fill-in form with fields, boxes and blanks to complete",
    "handwritten": "a mostly handwritten document, such as handwritten notes or a handwritten letter",
    "invoice": "an invoice or bill requesting payment for goods or services",
    "language": "a document written mainly in a language other than English, such as Arabic or Hindi",
    "letter": "a typed formal letter with a salutation (Dear ...) and a signature",
    "memo": "an internal memorandum with To/From/Date/Subject (Re:) lines",
    "news_article": "a news article or newspaper/magazine story",
    "questionnaire": "a questionnaire or survey with questions for a respondent to answer",
    "resume": "a resume or curriculum vitae describing a person's education and work history",
    "scientific_publication": "a scientific or academic paper with abstract, methods and references",
    "specification": "a technical specification, data sheet or requirements document",
    "advertisement": "an advertisement, flyer or promotional page for a product or service",
    "generic": "any other document that is none of the types above",
}


def short_class_descriptions(cfg: dict[str, Any]) -> None:
    """Short descriptions: replace every DocSplit class's description with one line.

    The classes are matched by `$id` (or `x-aws-idp-document-type`); every class must
    have a line, so a preset with other classes fails rather than half-changing.
    """
    classes = cfg.get("classes") or []
    for cls in classes:
        name = cls.get("$id") or cls.get("x-aws-idp-document-type")
        if name not in SHORT_DOCSPLIT_DESCRIPTIONS:
            raise SystemExit(f"short_class_descriptions: no line for class {name!r}")
        cls["description"] = SHORT_DOCSPLIT_DESCRIPTIONS[name]
    if not classes:
        raise SystemExit("short_class_descriptions: the profile has no classes")
