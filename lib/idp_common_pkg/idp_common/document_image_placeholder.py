# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The ``{DOCUMENT_IMAGE}`` placeholder rule shared by every prompt builder.

Also the placeholder removal classification applies for an explicit
``modality`` flag, which the validator needs too. Imports only the standard
library, so classification, extraction, the confidence
pass and ``validate_config`` can all use it without importing one another.
"""

import re
from typing import Optional

DOCUMENT_IMAGE_PLACEHOLDER = "{DOCUMENT_IMAGE}"


class DocumentImagePlaceholderError(ValueError):
    """A prompt carries ``{DOCUMENT_IMAGE}`` more than once.

    A configuration error rather than a per-document one: every document sent with
    the prompt fails the same way. It is its own type so a caller that falls back to
    a default prompt on an ordinary formatting ``ValueError`` can let this one out
    instead of sending a prompt nobody configured.
    """


def document_image_placeholder_error(prompt: str) -> Optional[str]:
    """Why ``prompt`` is refused, or None if it is not.

    The rule every service that splits a prompt on ``{DOCUMENT_IMAGE}`` applies
    when the prompt carries it — extraction, classification (page, context-pages
    and packet prompts) and the separate confidence pass: the page images go in at
    the placeholder, so it must appear exactly once. A prompt without it is sent
    without images and never reaches this rule. ``validate_config`` applies it to
    the same prompts, so a configuration refused on every document is refused up
    front.
    """
    count = prompt.count(DOCUMENT_IMAGE_PLACEHOLDER)
    if count == 1:
        return None
    return (
        f"Invalid DOCUMENT_IMAGE placeholder usage: found {count} occurrences, "
        "but exactly 1 is required. The DOCUMENT_IMAGE placeholder must appear "
        "exactly once in the template."
    )


def check_document_image_placeholder(prompt: str, where: str = "") -> None:
    """Raise ``DocumentImagePlaceholderError`` if ``prompt`` repeats ``{DOCUMENT_IMAGE}``.

    A prompt without the placeholder passes: it is sent without images. Check the
    whole template, before it is split on ``{FEW_SHOT_EXAMPLES}``: one placeholder
    either side of the examples is two placeholders, and the builders render each
    side separately, so neither side would see the repeat. ``where`` names the
    prompt in the message.
    """
    if DOCUMENT_IMAGE_PLACEHOLDER not in prompt:
        return
    error = document_image_placeholder_error(prompt)
    if error:
        raise DocumentImagePlaceholderError(f"{where}: {error}" if where else error)


def strip_placeholder(template: str, placeholder: str, element: str) -> str:
    """Remove ``placeholder``, and its wrapping ``<element>`` if that is all it held.

    What classification does to a prompt whose ``modality`` flag for that content is
    explicitly off. When any wrapped occurrence is found only the wrapped ones are
    removed, so a bare repeat beside one survives; ``validate_config`` applies this
    same function so it judges the prompt the service sends.
    """
    wrapped = re.compile(
        rf"[ \t]*<{re.escape(element)}>\s*{re.escape(placeholder)}\s*"
        rf"</{re.escape(element)}>[ \t]*\n?",
        re.IGNORECASE,
    )
    stripped, count = wrapped.subn("", template)
    if count:
        return stripped
    return template.replace(placeholder, "")


def section_prompt_can_be_sent(method: str, on_low_confidence: str) -> bool:
    """Whether split-then-classify's section-classification LLM prompt is ever sent.

    Always under ``method: llm``. Under ``method: decision_model`` only when a
    low-confidence section falls back to the LLM (``on_low_confidence: llm``).
    """
    return method != "decision_model" or on_low_confidence == "llm"
