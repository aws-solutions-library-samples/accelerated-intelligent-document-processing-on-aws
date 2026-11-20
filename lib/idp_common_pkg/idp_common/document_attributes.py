# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Caller-supplied document attributes: the rules, and the S3 intake route.

A caller who knows identifiers for a document before submitting it (an account
id, a claim number) attaches them as **attributes**: a flat mapping of key to
value that is stored with the document and returned unchanged with its results.
They are carried on ``Document.attributes``, which is separate from the internal
``Document.metadata``.

Attributes are **untrusted caller input**. Whoever can write to the input bucket
can set them, exactly as they can set ``config-version`` or supply the document
itself, and a trusted producer often copies values from sources it does not
control. So every route validates them against the same rules, which live here
and nowhere else:

* keys match :data:`KEY_PATTERN` (lower-case letters, digits, ``_`` and ``-``,
  starting with a letter or digit, at most 63 characters);
* a value is a string, a finite number, a boolean, or a flat list of those;
* at most :data:`MAX_ATTRIBUTES` attributes, :data:`MAX_STRING_BYTES` per string,
  :data:`MAX_LIST_ITEMS` per list, and :data:`MAX_TOTAL_BYTES` for the whole set
  serialised as compact JSON.

Nothing in the pipeline reads them: they are not included in any prompt, they
build no storage path, and their values are never logged (an account or claim
number can be personal data). The UI renders them as text.

Two input routes at S3 intake, both read by :func:`read_object_attributes`:

1. **Inline user metadata** ``x-amz-meta-idp-attr-<name>``. S3 lower-cases
   metadata names and HTTP header names conventionally use hyphens, so ``-`` in
   ``<name>`` becomes ``_`` in the stored key: ``idp-attr-account-id`` is the
   attribute ``account_id``. Values on this route are always strings.
2. **A JSON sidecar** named by ``x-amz-meta-idp-attributes-uri``. It must be in the
   same bucket as the document, its key must end in :data:`SIDECAR_SUFFIX`, and it
   is read once, at intake, up to :data:`MAX_TOTAL_BYTES`. The suffix is what
   stops the queue sender processing the sidecar as a document of its own
   (:func:`is_sidecar_key`). An inline attribute with the same key wins.

At intake there is no caller to answer, so an attribute that breaks a rule is
**dropped** and the document still processes;
``idp_common.models.attributes_dropped_issue`` builds the
``ingest_attributes_dropped`` warning recorded on the document. (It lives there,
not here, so this module does not import ``models`` and the two do not form an
import cycle.) The SDK and CLI
validate with :func:`validate_attributes` before uploading and refuse invalid
input with :class:`AttributeValidationError` instead.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

#: Attribute key rule. Kept to characters that are safe in an S3 metadata name, a
#: DynamoDB attribute path and a CSV header, so a later index or filter can use a
#: key without escaping it.
KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

#: Most attributes one document may carry.
MAX_ATTRIBUTES = 100
#: Longest string value (or list element), in UTF-8 bytes.
MAX_STRING_BYTES = 1024
#: Most elements in one list value.
MAX_LIST_ITEMS = 50
#: Largest attribute set, as compact JSON, in UTF-8 bytes. Also the largest
#: sidecar file intake reads. Keeps the tracking item well under DynamoDB's
#: 400 KB item limit.
MAX_TOTAL_BYTES = 32 * 1024
#: Largest integer accepted, so a value survives a JSON reader that parses
#: numbers as IEEE doubles (JavaScript, the UI) without silently changing.
MAX_SAFE_INTEGER = 2**53 - 1

#: S3 user-metadata name prefix of an inline attribute (after ``x-amz-meta-``).
METADATA_PREFIX = "idp-attr-"
#: S3 user-metadata name holding the sidecar's ``s3://`` URI.
SIDECAR_URI_METADATA_KEY = "idp-attributes-uri"
#: Required key suffix of a sidecar. The queue sender does not process an object
#: whose key ends in it.
SIDECAR_SUFFIX = ".idp-attributes.json"
#: Largest inline metadata block S3 accepts for a PUT (all user metadata).
S3_METADATA_LIMIT_BYTES = 2 * 1024

#: The ``ProcessingIssue`` code recorded when intake dropped one or more attributes.
#: The issue is built by ``idp_common.models.attributes_dropped_issue``.
ISSUE_ATTRIBUTES_DROPPED = "ingest_attributes_dropped"
#: How many dropped attributes that issue's ``details`` names individually.
MAX_DROPPED_NAMED = 20
#: Characters of an invalid key quoted in a drop reason, so a hostile key cannot
#: grow the issue.
_KEY_QUOTE_CHARS = 64


class AttributeValidationError(ValueError):
    """Raised by :func:`validate_attributes` when the input breaks a rule."""


@dataclass
class AttributeIntake:
    """What intake accepted, and what it dropped and why."""

    attributes: Dict[str, Any] = field(default_factory=dict)
    #: ``(key as quoted, reason)`` per dropped attribute, in the order found.
    dropped: List[Tuple[str, str]] = field(default_factory=list)


def _quote_key(key: Any) -> str:
    text = key if isinstance(key, str) else repr(key)
    if len(text) > _KEY_QUOTE_CHARS:
        return text[:_KEY_QUOTE_CHARS] + "..."
    return text


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _scalar_problem(value: Any) -> Optional[str]:
    """Why ``value`` is not an acceptable scalar, or None when it is."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if _utf8_len(value) > MAX_STRING_BYTES:
            return f"string longer than {MAX_STRING_BYTES} bytes"
        return None
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            return f"integer outside +/-{MAX_SAFE_INTEGER}"
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return "number is not finite"
        return None
    return f"unsupported value type {type(value).__name__}"


def value_problem(value: Any) -> Optional[str]:
    """Why ``value`` is not an acceptable attribute value, or None when it is."""
    if isinstance(value, list):
        if len(value) > MAX_LIST_ITEMS:
            return f"list longer than {MAX_LIST_ITEMS} items"
        for item in value:
            # A nested list or object fails here as an unsupported scalar type.
            problem = _scalar_problem(item)
            if problem:
                return f"list item: {problem}"
        return None
    return _scalar_problem(value)


def key_problem(key: Any) -> Optional[str]:
    """Why ``key`` is not an acceptable attribute key, or None when it is."""
    if not isinstance(key, str) or not KEY_PATTERN.match(key):
        return f"key does not match {KEY_PATTERN.pattern}"
    return None


def serialized_size(attributes: Mapping[str, Any]) -> int:
    """Bytes of ``attributes`` as compact JSON, the measure the total bound uses."""
    return _utf8_len(json.dumps(attributes, separators=(",", ":"), ensure_ascii=False))


def sanitize_attributes(candidate: Mapping[Any, Any]) -> AttributeIntake:
    """Keep every attribute that satisfies the rules; report the rest.

    Deterministic: attributes are considered in sorted key order (invalid keys
    last), so which ones survive the count and size bounds does not depend on the
    order a producer happened to write them in.
    """
    result = AttributeIntake()
    valid_keys = sorted(k for k in candidate if isinstance(k, str))
    other_keys = [k for k in candidate if not isinstance(k, str)]
    for key in valid_keys + other_keys:
        value = candidate[key]
        problem = key_problem(key) or value_problem(value)
        if problem:
            result.dropped.append((_quote_key(key), problem))
            continue
        if len(result.attributes) >= MAX_ATTRIBUTES:
            result.dropped.append(
                (_quote_key(key), f"more than {MAX_ATTRIBUTES} attributes")
            )
            continue
        trial = dict(result.attributes)
        trial[key] = value
        if serialized_size(trial) > MAX_TOTAL_BYTES:
            result.dropped.append(
                (_quote_key(key), f"attribute set larger than {MAX_TOTAL_BYTES} bytes")
            )
            continue
        result.attributes = trial
    return result


def validate_attributes(candidate: Any) -> Dict[str, Any]:
    """Return ``candidate`` as a validated attribute dict, or raise.

    The strict form, for routes with a caller to answer (SDK, CLI). Every
    problem is reported in one message rather than the first only.
    """
    if not isinstance(candidate, Mapping):
        raise AttributeValidationError(
            "attributes must be a JSON object of key to value"
        )
    intake = sanitize_attributes(candidate)
    if intake.dropped:
        reasons = "; ".join(f"{key!r}: {why}" for key, why in intake.dropped)
        raise AttributeValidationError(f"invalid document attributes: {reasons}")
    return intake.attributes


def metadata_name_to_key(name: str) -> str:
    """``idp-attr-account-id`` -> ``account_id``. Caller checks the prefix."""
    return name[len(METADATA_PREFIX) :].replace("-", "_")


def is_sidecar_key(key: str) -> bool:
    """True for an object key that names an attributes sidecar, not a document."""
    return key.endswith(SIDECAR_SUFFIX)


def _parse_sidecar_uri(uri: str, bucket: str) -> Tuple[Optional[str], Optional[str]]:
    """``(key, None)`` for an acceptable sidecar URI, else ``(None, reason)``."""
    if not uri.startswith("s3://"):
        return None, "sidecar URI is not an s3:// URI"
    uri_bucket, _, key = uri[len("s3://") :].partition("/")
    if uri_bucket != bucket:
        return None, "sidecar is not in the document's input bucket"
    if not key or not is_sidecar_key(key):
        return None, f"sidecar key does not end in {SIDECAR_SUFFIX}"
    return key, None


def _read_sidecar(
    s3_client: Any, bucket: str, key: str
) -> Tuple[Optional[Dict[Any, Any]], Optional[str]]:
    """The sidecar's JSON object, or ``(None, reason)``. Reads at most the bound."""
    try:
        response = s3_client.get_object(
            Bucket=bucket, Key=key, Range=f"bytes=0-{MAX_TOTAL_BYTES}"
        )
        body = response["Body"].read(MAX_TOTAL_BYTES + 1)
    except Exception as e:  # noqa: BLE001 - any read failure drops the sidecar
        return None, f"sidecar could not be read ({type(e).__name__})"
    if len(body) > MAX_TOTAL_BYTES:
        return None, f"sidecar larger than {MAX_TOTAL_BYTES} bytes"
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        # RecursionError: a body within the size bound can still nest deeply
        # enough to exhaust the parser's stack.
        return None, "sidecar is not valid UTF-8 JSON"
    if not isinstance(data, dict):
        return None, "sidecar is not a JSON object"
    return data, None


def read_object_attributes(
    metadata: Mapping[str, str], bucket: str, s3_client: Any
) -> AttributeIntake:
    """The attributes an uploaded object carries, from its S3 user metadata.

    ``metadata`` is ``head_object``'s ``Metadata`` map. Reads the sidecar when one
    is named. Never raises for bad input: anything out of bounds is dropped and
    reported in :attr:`AttributeIntake.dropped`.
    """
    try:
        return _read_object_attributes(metadata, bucket, s3_client)
    except Exception as e:  # noqa: BLE001 - this route never raises; see docstring
        # Nothing in the rules should get here. If something does, the document
        # still processes, without attributes and with the warning saying so.
        return AttributeIntake(
            dropped=[("(all)", f"attributes could not be read ({type(e).__name__})")]
        )


def _read_object_attributes(
    metadata: Mapping[str, str], bucket: str, s3_client: Any
) -> AttributeIntake:
    candidate: Dict[Any, Any] = {}
    early_drops: List[Tuple[str, str]] = []

    uri = metadata.get(SIDECAR_URI_METADATA_KEY)
    if uri:
        key, problem = _parse_sidecar_uri(uri, bucket)
        data = None
        if key is not None:
            data, problem = _read_sidecar(s3_client, bucket, key)
        if data is not None:
            candidate.update(data)
        else:
            early_drops.append(("(sidecar)", problem or "sidecar rejected"))

    for name, value in metadata.items():
        if name.startswith(METADATA_PREFIX):
            candidate[metadata_name_to_key(name)] = value

    intake = sanitize_attributes(candidate)
    intake.dropped = early_drops + intake.dropped
    return intake


def loggable_metadata(metadata: Mapping[str, str]) -> Dict[str, str]:
    """``metadata`` with every attribute value removed, safe to log."""
    return {
        name: ("<redacted>" if name.startswith(METADATA_PREFIX) else value)
        for name, value in metadata.items()
    }


def encode_for_upload(
    attributes: Mapping[str, Any], base_metadata: Optional[Mapping[str, str]] = None
) -> Tuple[Dict[str, str], Optional[bytes]]:
    """How to send ``attributes`` with an upload: ``(metadata, sidecar_body)``.

    ``attributes`` must already be validated. Inline metadata is used when it
    reproduces the set exactly at intake: every value a printable-ASCII string,
    no key containing ``-`` (which the inline route turns into ``_``), and the
    whole metadata block within S3's limit. Otherwise the set goes in a sidecar
    and ``sidecar_body`` is its JSON; the caller uploads it to
    ``<document key>`` + :data:`SIDECAR_SUFFIX` *before* the document and adds
    :data:`SIDECAR_URI_METADATA_KEY` to the returned metadata.
    """
    metadata = dict(base_metadata or {})
    if not attributes:
        return metadata, None
    inline = all(
        isinstance(v, str) and v.isascii() and v.isprintable() and "-" not in k
        for k, v in attributes.items()
    )
    if inline:
        trial = dict(metadata)
        for k, v in attributes.items():
            trial[METADATA_PREFIX + k.replace("_", "-")] = v
        size = sum(len(n) + len(v) for n, v in trial.items())
        if size <= S3_METADATA_LIMIT_BYTES:
            return trial, None
    body = json.dumps(dict(attributes), ensure_ascii=False, sort_keys=True)
    return metadata, body.encode("utf-8")
