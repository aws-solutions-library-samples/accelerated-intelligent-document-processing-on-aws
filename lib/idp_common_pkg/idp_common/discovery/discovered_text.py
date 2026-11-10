# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Plain-text cleanup for text a discovery model wrote.

Discovery turns sample documents into configuration: class and field descriptions,
``examples`` values and rule text, which are saved into a configuration profile,
shown in the web UI, and later placed into classification, extraction and rule
validation prompts for every document processed under that profile. They are
meant to be short plain-language text, so this module keeps them that way:

* markdown images are removed, and a markdown link keeps only its text;
* URLs (``scheme://...``, protocol-relative ``//host/...``, ``www.``-prefixed hosts
  and ``data:`` / ``javascript:`` / ``mailto:`` URIs) are removed;
* HTML tags and comments are removed;
* whitespace runs are collapsed and the result is capped in length.

Only the text-bearing keys are touched (``description``, ``examples`` and, for the
reflection report, the whole text). ``$schema``, ``$id``, types, regex patterns and
every other structural key are left exactly as the model produced them, so a
schema's meaning does not change.

What it does **not** do: remove a bare hostname with no scheme and no ``www.``
(``remote.example/path``). Telling one apart from ordinary text such as ``e.g.`` or
``W-2.pdf`` needs a public-suffix list, and markdown renderers do not turn such a
string into a link. The UI renders these fields as text, not markdown.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

#: Longest description kept, in characters. Discovery is asked for descriptions of
#: well under 50 words; the hand-written presets in ``config_library`` carry a few
#: longer ones, which this never touches because it runs only on discovery output.
MAX_DESCRIPTION_CHARS = 1000

#: Longest single ``examples`` value kept, in characters.
MAX_EXAMPLE_CHARS = 200

#: Most ``examples`` values kept per field.
MAX_EXAMPLES = 10

#: Longest reflection report kept, in characters.
MAX_REPORT_CHARS = 20000

_TRUNCATION_MARK = "…"

_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)|!\[[^\]]*\]\[[^\]]*\]")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_REF_LINK = re.compile(r"\[([^\]]*)\]\[[^\]]*\]")
_MD_REF_DEFINITION = re.compile(r"^[ \t]{0,3}\[[^\]]+\]:[ \t]*\S+.*$", re.MULTILINE)
_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|$)", re.DOTALL)
_HTML_TAG = re.compile(r"</?[A-Za-z!?][^<>]*>")
_URL = re.compile(
    r"(?:\b[A-Za-z][A-Za-z0-9+.\-]*:[/\\]{2,}|(?<![\w/])//(?=[\w\-]+\.)|\bwww\.)[^\s<>()\[\]\"']*"
    r"|\b(?:data|javascript|vbscript|mailto):\S+",
    re.IGNORECASE,
)
_SPACES = re.compile(r"[ \t\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(_TRUNCATION_MARK))].rstrip() + _TRUNCATION_MARK


def clean_text(
    value: Any, limit: int = MAX_DESCRIPTION_CHARS, *, multiline=False
) -> Any:
    """``value`` with images, links' targets, URLs and HTML removed, and capped.

    A non-string is returned unchanged, so a numeric ``examples`` value keeps its
    type. ``multiline`` keeps line breaks (for the markdown reflection report);
    otherwise all whitespace collapses to single spaces.
    """
    if not isinstance(value, str):
        return value
    text = _HTML_COMMENT.sub(" ", value)
    text = _MD_IMAGE.sub(" ", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_REF_LINK.sub(r"\1", text)
    text = _MD_REF_DEFINITION.sub("", text)
    text = _HTML_TAG.sub(" ", text)
    text = _URL.sub(" ", text)
    if multiline:
        text = "\n".join(_SPACES.sub(" ", line).strip() for line in text.split("\n"))
        text = _BLANK_LINES.sub("\n\n", text).strip()
    else:
        text = " ".join(text.split())
    return _cap(text, limit)


def _clean_examples(examples: Any) -> Any:
    if not isinstance(examples, list):
        return clean_text(examples, MAX_EXAMPLE_CHARS)
    cleaned: List[Any] = []
    for item in examples[:MAX_EXAMPLES]:
        if isinstance(item, (dict, list)):
            # A structured example is a sample object for an object-typed field;
            # clean the strings inside it rather than dropping it.
            cleaned.append(_clean_strings(item))
        else:
            cleaned.append(clean_text(item, MAX_EXAMPLE_CHARS))
    return cleaned


def _clean_strings(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _clean_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean_strings(v) for v in value]
    return clean_text(value, MAX_EXAMPLE_CHARS)


def clean_schema_text(node: Any) -> Any:
    """Clean every ``description`` and ``examples`` value in a schema, in place.

    Walks dicts and lists to any depth, so nested ``properties``, array ``items``
    and ``$defs`` are covered. A key that merely *names* a field ``description``
    under ``properties`` is a property name, not text, and is walked into rather
    than cleaned. Returns ``node`` for convenience.
    """
    if isinstance(node, dict):
        for key in list(node.keys()):
            value = node[key]
            if key == "properties" and isinstance(value, dict):
                for prop in value.values():
                    clean_schema_text(prop)
            elif key == "description":
                node[key] = clean_text(value, MAX_DESCRIPTION_CHARS)
            elif key == "examples":
                node[key] = _clean_examples(value)
            else:
                clean_schema_text(value)
    elif isinstance(node, list):
        for item in node:
            clean_schema_text(item)
    return node


def clean_rule_classes(rule_classes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Clean the text of discovered rule classes, in place.

    Rule classes carry their text in ``description`` at class level and in each
    ``rule_properties[*].description``; both are reached by
    :func:`clean_schema_text`, and ``rule_properties`` keys (``rule_1``) are names,
    not text.
    """
    for rule_class in rule_classes or []:
        if not isinstance(rule_class, dict):
            continue
        props = rule_class.get("rule_properties")
        if isinstance(props, dict):
            for name, prop in list(props.items()):
                if isinstance(prop, dict):
                    clean_schema_text(prop)
                else:
                    props[name] = clean_text(prop, MAX_DESCRIPTION_CHARS)
        if "description" in rule_class:
            rule_class["description"] = clean_text(
                rule_class["description"], MAX_DESCRIPTION_CHARS
            )
    return rule_classes


def clean_report(text: Any) -> Any:
    """The reflection report with images, URLs and HTML removed, line breaks kept."""
    return clean_text(text, MAX_REPORT_CHARS, multiline=True)
