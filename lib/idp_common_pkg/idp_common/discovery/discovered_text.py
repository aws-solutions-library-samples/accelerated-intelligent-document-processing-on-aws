# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Plain-text cleanup for text a discovery model wrote.

Discovery turns sample documents into configuration: class and field descriptions,
``examples`` values, prompts, rule text and the like, which are saved into a
configuration profile, shown in the web UI, and later placed into classification,
extraction, assessment and rule validation prompts for every document processed
under that profile. They are meant to be plain-language text, so this module keeps
every string the model wrote that way:

* the text is normalised first: HTML entities are decoded (``&lt;img`` is a tag),
  compatibility forms folded (fullwidth ``ｈｔｔｐｓ`` is ``https``) and invisible
  format characters such as zero-width spaces removed, so a spelling cannot hide a
  URL from the patterns below;
* markdown images are removed, and a markdown link keeps only its text;
* URLs are removed: any ``scheme:`` followed by a slash or backslash, a
  protocol-relative ``//host``, a ``www.`` host, and the ``http``, ``ftp``,
  ``file``, ``data``, ``javascript``, ``vbscript``, ``mailto``, ``blob`` and ``ws``
  schemes written with no slash at all;
* HTML tags and comments are removed, including a tag left unclosed;
* whitespace runs are collapsed and the result is capped in length.

**Which strings.** In a schema, every string value is cleaned except the ones that
are machine syntax rather than text: ``$schema``, ``$id``, ``$ref``, ``type``,
``format``, ``pattern`` and any key ending in ``-regex``; property names are left
as they are. Everything else — ``description``, ``title``, ``examples``, ``enum``,
``const``, ``default``, ``x-aws-idp-*-prompt``, ``x-aws-idp-list-item-description``,
the few-shot ``x-aws-idp-examples`` and anything a model adds — is text that can
reach a prompt or the page, so it is cleaned. A model-written image path is a URL
and so is removed: discovery does not produce few-shot images.

What it does **not** do: remove a bare hostname with no scheme and no ``www.``
(``remote.example/path``) or a defanged one (``remote[.]example``). Telling one apart
from ordinary text such as ``e.g.`` or ``W-2.pdf`` needs a public-suffix list, and
neither a markdown renderer nor a browser turns such a string into a link. The UI
renders these fields as text, not markdown.
"""

from __future__ import annotations

import html
import re
import unicodedata
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

_MD_IMAGE = re.compile(r"!\[[^\]]*\]\s*\([^)]*\)|!\[[^\]]*\]\s*\[[^\]]*\]")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_REF_LINK = re.compile(r"\[([^\]]*)\]\[[^\]]*\]")
_MD_REF_DEFINITION = re.compile(r"^[ \t]{0,3}\[[^\]]+\]:[ \t]*\S+.*$", re.MULTILINE)
_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|$)", re.DOTALL)
# A tag runs to its `>` or, left unclosed, to the end of the text.
_HTML_TAG = re.compile(r"</?[A-Za-z!?][^<>]*(?:>|$)")
_URL = re.compile(
    # any scheme followed by one or more slashes or backslashes; no word boundary,
    # so `_https://` and `9https:\\` are found too
    r"[A-Za-z][A-Za-z0-9+.\-]*:[/\\]+[^\s<>()\[\]\"']*"
    # protocol-relative //host.
    r"|(?<![\w/:])//(?=[\w\-]+\.)[^\s<>()\[\]\"']*"
    # a www. host, after any character
    r"|www\.[^\s<>()\[\]\"']*"
    # the schemes that mean something with no slash at all
    r"|(?<![A-Za-z])(?:https?|ftps?|file|data|javascript|vbscript|mailto|blob|wss?):\S+",
    re.IGNORECASE,
)
_SPACES = re.compile(r"[ \t\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def _normalise(value: str) -> str:
    """Entities decoded, compatibility forms folded, invisible characters removed.

    Decoded repeatedly (bounded) so that ``&amp;lt;`` is not a way to keep a ``<``.
    Line breaks and tabs are kept: they are control characters (``Cc``), not the
    format characters (``Cf``) this removes.
    """
    text = value
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    text = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


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
    text = _normalise(value)
    text = _HTML_COMMENT.sub(" ", text)
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


#: Keys whose string values are machine syntax, not text: never cleaned.
SYNTAX_KEYS = frozenset({"$schema", "$id", "$ref", "type", "format", "pattern"})

#: Longest prompt-like value kept (a key containing ``prompt``), in characters.
MAX_PROMPT_CHARS = 4000


def _is_syntax_key(key: Any) -> bool:
    return isinstance(key, str) and (key in SYNTAX_KEYS or key.endswith("-regex"))


def _limit_for(key: Any) -> int:
    name = key.lower() if isinstance(key, str) else ""
    if "prompt" in name:
        return MAX_PROMPT_CHARS
    if "description" in name or name == "title":
        return MAX_DESCRIPTION_CHARS
    return MAX_EXAMPLE_CHARS


def _clean_value(key: Any, value: Any) -> Any:
    """``value`` (found under ``key``) with every string in it cleaned."""
    if isinstance(value, str):
        return clean_text(value, _limit_for(key))
    if isinstance(value, list):
        items = value[:MAX_EXAMPLES] if key == "examples" else value
        return [_clean_value(key, item) for item in items]
    if isinstance(value, dict):
        clean_schema_text(value)
    return value


def clean_schema_text(node: Any) -> Any:
    """Clean every string a model wrote into a schema, in place.

    Walks dicts and lists to any depth. Every string value is cleaned except under
    a syntax key (:data:`SYNTAX_KEYS`, or a key ending in ``-regex``), and the keys
    of ``properties`` are field names, so they are walked into and never changed.
    ``examples`` keeps at most :data:`MAX_EXAMPLES` values. Returns ``node``.
    """
    if isinstance(node, dict):
        for key in list(node.keys()):
            value = node[key]
            if _is_syntax_key(key):
                continue
            if key == "properties" and isinstance(value, dict):
                for prop in value.values():
                    clean_schema_text(prop)
            else:
                node[key] = _clean_value(key, value)
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
