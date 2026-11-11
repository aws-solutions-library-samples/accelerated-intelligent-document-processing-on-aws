# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Comments in prompt templates: ``{# ... #}``, removed before a prompt is sent.

A prompt template can document itself - the output it must ask for, the
placeholders it may use, where ``<<CACHEPOINT>>`` goes - in Jinja-style
comments::

    {# Must return JSON with "class" and "document_boundary". #}
    <task-description> ... </task-description>

The comment is kept where the configuration is stored and edited (the
configuration editor, exports, ``idp-cli config-download``) and removed from
the configuration the processing runtime reads, so no comment text ever
reaches a model.

Where the removal happens, and why there. Services check, splice and split a
prompt template *before* they substitute values into it: classification infers
image/text modality from whether ``{DOCUMENT_IMAGE}`` appears in the template,
the confidence and bounding-box instruction blocks are spliced in next to
``<<CACHEPOINT>>``, and several services split the template on
``{DOCUMENT_IMAGE}`` / ``{FEW_SHOT_EXAMPLES}``. A placeholder written inside a
comment must therefore be gone before any of that, so comments are removed once,
where the runtime loads its configuration
(:meth:`idp_common.config.ConfigurationReader.get_merged_configuration`), rather
than at each of those sites. Removing them later, at the Bedrock client, would
be wrong: by then document text has been substituted in, and a document that
itself contains ``{#`` would be altered. ``BedrockClient.format_prompt`` also
removes comments, for a template handed to it directly.

This module imports only the standard library, so the Bedrock client can use it
without importing ``idp_common.config`` (see the import notes in
``idp_common/bedrock/client.py``).

Rules:

- A comment runs from ``{#`` to the next ``#}`` and may span lines.
- A line holding nothing but a comment (and whitespace) is removed with its
  line break, so commenting a prompt does not add blank lines to it.
- ``{#`` with no closing ``#}`` is not a comment and is left as written.
- A template without ``{#`` is returned unchanged, character for character.
"""

import re
from typing import Any

#: One comment. The body is "tempered" - it may not contain ``#}`` - so a
#: comment always ends at its own first ``#}``. That matters for
#: ``_COMMENT_LINE`` below: with a plain lazy ``.*?`` body under DOTALL, a line
#: such as ``{# a #}Keep{# b #}`` would fail the required line terminator after
#: the first ``#}``, and the engine would extend the comment across that ``#}``
#: to the next one, so ``Keep`` would be removed with the "comment".
_ONE_COMMENT = r"\{#(?:(?!#\}).)*#\}"
#: A whole line that is only comments (and whitespace), removed together with
#: its line break.
_COMMENT_LINE = re.compile(
    rf"^[ \t]*(?:{_ONE_COMMENT}[ \t]*)+(?:\r?\n|\Z)", re.DOTALL | re.MULTILINE
)
#: Any remaining comment, inside a line.
_COMMENT = re.compile(_ONE_COMMENT, re.DOTALL)

_OPEN = "{#"


def strip_prompt_comments(template: Any) -> Any:
    """Return ``template`` with its ``{# ... #}`` comments removed.

    A non-string (``None`` for an unset prompt, for example) is returned as is,
    so callers can pass a config value without checking its type first.
    """
    if not isinstance(template, str) or _OPEN not in template:
        return template
    without_lines = _COMMENT_LINE.sub("", template)
    return _COMMENT.sub("", without_lines)


def is_prompt_key(key: Any) -> bool:
    """True for a configuration key that holds prompt text.

    Every prompt field in the configuration and every prompt key in a class
    schema has ``prompt`` in its name: ``system_prompt``, ``task_prompt``,
    ``task_prompt_bbox``, ``user_prompt``, ``x-aws-idp-class-prompt``,
    ``x-aws-idp-extraction-task-prompt``, the legacy ``classPrompt``, and so on.
    ``custom_prompt_lambda_arn`` names a Lambda, not prompt text, and is excluded.

    The test is deliberately a substring test, so it also matches keys whose value
    is not prompt text (``prompt_cache``, a ``*_prompt_enabled`` flag). That is
    harmless: only string values are stripped, and a non-prompt string carries no
    ``{#`` in practice. A narrower rule would risk missing a prompt key added later.
    """
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return "prompt" in lowered and not lowered.endswith("_arn")


def has_prompt_comments(node: Any) -> bool:
    """True when any prompt value in a nested dict/list contains ``{#``."""
    if isinstance(node, dict):
        for key, value in node.items():
            if is_prompt_key(key) and isinstance(value, str) and _OPEN in value:
                return True
            if isinstance(value, (dict, list)) and has_prompt_comments(value):
                return True
        return False
    if isinstance(node, list):
        return any(has_prompt_comments(item) for item in node)
    return False


def strip_prompt_comments_in_config(node: Any) -> Any:
    """A copy of a nested dict/list with comments removed from every prompt value.

    Only values under prompt keys (see :func:`is_prompt_key`) are touched; every
    other value is copied as is. The input is not modified.
    """
    if isinstance(node, dict):
        return {
            key: (
                strip_prompt_comments(value)
                if is_prompt_key(key) and isinstance(value, str)
                else strip_prompt_comments_in_config(value)
            )
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [strip_prompt_comments_in_config(item) for item in node]
    return node
