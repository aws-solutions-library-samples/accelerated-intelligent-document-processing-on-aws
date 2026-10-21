# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Scrub what identifies the review stack, its personas and its account.

Everything that leaves the UX job passes through one :class:`Redactor`:
``section.md``, ``report.md``, ``summary.json``, ``refusals.jsonl``, the note, the
reply and every printed line. The job holds a persona's username and password, the
review stack's Cognito pool and client ids, its API, stream and bucket names, the
GitLab token and an AWS session, and the text it publishes is written partly by a
model that browsed pages rendered by the MR's own code. So redaction is not
applied to the fields somebody thought might carry a secret; it is applied to all
of it, at the boundary.

Two layers, in this order
-------------------------
1. **Registered values**, replaced verbatim wherever they occur, longest first, so
   a value that contains a shorter one is removed whole instead of leaving its
   tail behind. The harness registers the UI environment's identifying values
   (:func:`values_from_ui_env`), the persona credentials, the token and the AWS
   key material it holds. Values shorter than ``min_length`` are ignored: a
   persona name such as ``Admin`` is not a secret, and replacing every occurrence
   of a short common word would make a report unreadable.
2. **Shapes**, scrubbed whether or not anybody registered them: a 12-digit run
   (an account id), an AWS access key id, an AWS session token by the prefixes
   STS issues them with, a JWT, an email address, the signing values of a
   presigned URL, the value of a line or field named as an AWS secret key or
   session token, a bearer value, a GitLab token header's value and a GitLab token
   by its prefix. These catch what arrives without having been registered, such as
   an account id in an AWS error message, a token the model copied off a debug
   panel, the browser's own Cognito-vended session shown on a page, or a presigned
   S3 link to a document the page showed.

A presigned URL keeps its path and every parameter name; only the values of
``X-Amz-Credential``, ``X-Amz-Security-Token`` and ``X-Amz-Signature`` (SigV4) and
of ``AWSAccessKeyId``, ``Signature`` and ``x-amz-security-token`` (SigV2) go, so a
reader can still tell which object was linked but nobody can fetch it. The
``X-Amz-Security-Token: <value>`` header spelling goes the same way. A bearer
value counts from 16 characters, so prose such as "bearer authentication" stays.

Redacting text twice gives what redacting it once gives: no shape reads a
placeholder as a value, so a placeholder is never extended or wrapped again.

The shape patterns only start a match where the shape could start (a JWT and an
email address are anchored to the start of their run, the rest to a literal
prefix), so a long base64 blob in a report costs linear time rather than a scan
from every offset.

Bounds worth knowing
--------------------
Matching is exact and case-sensitive. A value split across lines, percent-encoded
or JSON-escaped does not match its registered form, which is why :meth:`Redactor.tree`
exists: redact a structure's leaves *before* serialising it, not the serialised
text. Keys are left alone, because keys are the schema the harness wrote, not data.

Host names are removed only when they are registered. That is deliberate for the
proxy's ``refusals.jsonl``: a bucket the page needed but ``UX_TEST_BUCKETS`` does
not name shows up there by its exact host, and that log is how an operator finds
the exact names to add, so no shape rule rewrites an S3 host.
"""

from __future__ import annotations

import functools
import re
import urllib.parse
from collections.abc import Iterable, Mapping

REDACTED = "[redacted]"

#: The UX_TEST_UI_ENV keys whose values identify the review stack.
_UI_ENV_KEYS = (
    "VITE_USER_POOL_ID",
    "VITE_USER_POOL_CLIENT_ID",
    "VITE_IDENTITY_POOL_ID",
    "VITE_SETTINGS_PARAMETER",
    "VITE_INPUT_BUCKET_NAME",
    "VITE_TEST_SET_BUCKET_NAME",
    "VITE_COGNITO_DOMAIN",
    "VITE_CLOUDFRONT_DOMAIN",
    "VITE_API_BASE_URL",
    "VITE_STREAM_URL",
)

#: Where a query or header value ends: a separator, whitespace, a quote, markup,
#: an escape, a bracket (which is also how a placeholder is never read as a value),
#: or the close of a Markdown link.
_VALUE = r"[^&\s\"'<>\\)\[\]]+"

#: Shapes scrubbed after the registered values, in this order, each match becoming
#: its placeholder. A ``keep`` group's text stays in front of the placeholder, so a
#: presigned URL keeps its parameter names and a header its name.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)(?P<keep>X-Amz-(?:Credential|Security-Token|Signature)"
            r"(?:=|%3D|:[ \t]*))" + _VALUE
        ),
        REDACTED,
    ),
    (
        re.compile(r"(?i)(?P<keep>[?&;](?:AWSAccessKeyId|Signature)=)" + _VALUE),
        REDACTED,
    ),
    (
        re.compile(
            r"(?i)\b(?P<keep>(?:aws_?)?(?:secret_?access_?key|session_?token)"
            r"[\"']?[ \t]*[:=][ \t]*[\"']?)" + _VALUE
        ),
        REDACTED,
    ),
    (
        re.compile(
            r"(?i)\b(?P<keep>(?:PRIVATE|JOB|DEPLOY)-TOKEN[\"']?\s*[:=]\s*[\"']?)"
            + _VALUE
        ),
        "[token]",
    ),
    (re.compile(r"(?i)\b(?P<keep>Bearer\s+)[A-Za-z0-9._~+/=-]{16,}"), "[token]"),
    (
        re.compile(
            r"\b(?:glpat|gloas|gldt|glrt|glrtr|glcbt|glptt|glft|glimt|glagent|glsoat"
            r"|glffct)-[A-Za-z0-9_-]{8,}|\bGR1348941[A-Za-z0-9_-]{8,}"
        ),
        "[token]",
    ),
    (
        re.compile(
            r"(?<![A-Za-z0-9+/%=_-])(?:IQoJb3JpZ2luX2Vj|FwoGZXIvYXdzE|AQoDYXdzE)"
            r"[A-Za-z0-9+/=%_-]{20,}"
        ),
        "[token]",
    ),
    (re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])"), "[account]"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[access-key]"),
    (
        re.compile(
            r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]{8,}){2,}"
        ),
        "[token]",
    ),
    (
        re.compile(
            r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
        ),
        "[email]",
    ),
)


def _placed(placeholder: str, match: re.Match[str]) -> str:
    """The placeholder for one shape match, after the text its ``keep`` group held."""
    return (match.groupdict().get("keep") or "") + placeholder


class Redactor:
    """Replace registered values, then known shapes, in any text it is given."""

    def __init__(self, values: Iterable[str] = (), *, min_length: int = 6) -> None:
        self._min_length = min_length
        self._values: set[str] = set()
        self._ordered: list[str] = []
        self.add(*values)

    def add(self, *values: str) -> None:
        """Register more values.

        Surrounding whitespace is dropped first, so a value read from a file with a
        trailing newline still matches where it appears without one; then anything
        shorter than ``min_length`` is ignored.
        """
        for value in values:
            stripped = value.strip()
            if len(stripped) >= self._min_length:
                self._values.add(stripped)
        self._ordered = sorted(self._values, key=lambda value: (-len(value), value))

    def __call__(self, text: str) -> str:
        """``text`` with every registered value and every known shape replaced."""
        for value in self._ordered:
            text = text.replace(value, REDACTED)
        for pattern, placeholder in _PATTERNS:
            text = pattern.sub(functools.partial(_placed, placeholder), text)
        return text

    def tree(self, value: object) -> object:
        """A copy of a JSON-like structure with every string leaf redacted.

        Dict values and list (or tuple) items are redacted; dict keys, numbers,
        booleans and ``None`` are returned unchanged. The input is not modified.
        """
        if isinstance(value, str):
            return self(value)
        if isinstance(value, dict):
            return {key: self.tree(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.tree(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.tree(item) for item in value)
        return value


def values_from_ui_env(env: Mapping[str, str]) -> list[str]:
    """The identifying values of a parsed UX_TEST_UI_ENV, for a :class:`Redactor`.

    The Cognito pool, client and identity pool ids, the settings parameter, the
    bucket names (including every ``UX_TEST_BUCKETS`` name), the Cognito and
    CloudFront domains, and the API and stream URLs, each URL both whole and as
    its bare hostname, since a log line or an error usually names only the host.
    The region and the UI tuning values are not identifying and are left out, so
    ``us-east-1`` in a report survives. Absent and empty keys are skipped.
    """
    found: list[str] = []
    for key in _UI_ENV_KEYS:
        value = env.get(key, "").strip()
        if not value:
            continue
        found.append(value)
        if "://" in value:
            host = urllib.parse.urlsplit(value).hostname
            if host:
                found.append(host)
    for name in env.get("UX_TEST_BUCKETS", "").split(","):
        if name.strip():
            found.append(name.strip())
    return list(dict.fromkeys(found))
