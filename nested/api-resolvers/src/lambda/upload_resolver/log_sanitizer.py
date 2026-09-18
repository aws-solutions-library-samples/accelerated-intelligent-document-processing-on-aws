# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Helpers for redacting sensitive content from log messages.

These helpers exist so that resolvers and pipeline Lambdas can log a
representative subset of their inputs (operation name, record
identifiers, argument keys) without accidentally writing Cognito claims,
JWTs, long OCR text, or extracted PII fields into CloudWatch Logs.

Design goals:

* Never raise. If the input is not a dict, we pass it through unchanged
  and return it so `logger.info(sanitize_event_for_logging(event))`
  is always safe.
* Fail-closed *for the keys it knows about*. Any key whose name matches
  the denylist — regardless of nesting depth — is replaced with the
  literal string `"***REDACTED***"`.

  Note that this is a **denylist**, not an allowlist: a key nobody listed
  is logged. That is deliberate — an allowlist would redact the argument
  and field *names* operators need in order to read the log at all — but
  it means **a new sensitive API field is only protected once its name is
  added to ``_DEFAULT_DENY_KEY_SUBSTRINGS`` below** (or passed per-call
  via ``extra_deny_keys``). ``identity`` is on the list as a whole
  subtree rather than key-by-key, which hedges the commonest case.
* Deep copy. We never mutate the caller's object.
* Preserve structure. Keys and list lengths are preserved so operators
  can still see shapes (how many sections, how many args).
* Truncate long strings. Document-content fields can be several megabytes;
  we cap string values at 500 characters in the sanitized copy.

  Truncation is narrower than redaction, in two ways that are log-volume
  concerns only and never leaks, since redaction is applied first and at
  every depth: it fires only where a ``_DEFAULT_TRUNCATE_KEYS`` name holds
  a ``str`` directly, so ``{"extracted_text": {"page1": <5000 chars>}}``
  keeps the inner string in full; and a long value under a key nobody
  listed is not truncated at all.

Usage::

    from idp_common.utils.log_sanitizer import sanitize_event_for_logging
    logger.info("Invoked with event: %s",
                json.dumps(sanitize_event_for_logging(event)))

If you need to log the full event for debugging, set the Lambda env var
``LOG_FULL_EVENT=true`` and wrap the ``json.dumps(event)`` call in a check
on that flag in the handler — the sanitizer should remain the default.

Vendored copies
---------------

SAM packages each Lambda from its own ``CodeUri`` directory, so a function
that does not carry the ``idp-common`` layer cannot import this module from
the library. Rather than attach a layer carrying Pillow/pypdfium2/requests to
a handful of tiny resolvers just to reach a stdlib-only module — or let each
one hand-roll its own denylist, which is how they fell eight keys behind —
byte-identical copies of this file are committed as ``log_sanitizer.py``
inside the ``nested/api-resolvers/src/lambda/`` function directories that
need it.

**This file is the only copy you edit.** After changing it, re-sync with
``scripts/sync_resolver_log_sanitizer.sh``;
``scripts/tests/test_resolver_log_sanitizer.py`` fails on drift and on any
resolver that reintroduces a local denylist.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Iterable, Set

# Keys (case-insensitive substring match) whose values are redacted wholesale.
# This covers JWTs, Cognito claims, bearer headers, API keys, and signed URLs.
_DEFAULT_DENY_KEY_SUBSTRINGS: Set[str] = {
    "password",
    "passwd",
    "secret",
    "token",  # idToken, accessToken, refreshToken, csrfToken, sessionToken
    "authorization",
    "apikey",
    "api_key",
    "access_key",
    "accesskey",
    "secretkey",
    "secret_key",
    "x-api-key",
    "cookie",
    "credential",
    "privatekey",
    "private_key",
    "claims",  # Cognito claims blob — contains email, groups, sub
    "identity",  # AppSync identity.claims; we'll redact the whole subtree
}

# Keys whose values are string content likely to be long (document text,
# LLM output, OCR results) and which should be truncated for log volume
# hygiene rather than full redaction.
_DEFAULT_TRUNCATE_KEYS: Set[str] = {
    "text",
    "content",
    "body",
    "answer",
    "snippet",
    "ocr_text",
    "markdown",
    "extracted_text",
    "extracted_fields",
    "prompt",
    "response",
}

_REDACTED = "***REDACTED***"
_TRUNCATE_MAX_CHARS = 500


def _matches_deny(key: str, deny_substrings: Iterable[str]) -> bool:
    k = key.lower()
    return any(ds in k for ds in deny_substrings)


def _truncate_string(value: str, max_chars: int = _TRUNCATE_MAX_CHARS) -> str:
    if len(value) <= max_chars:
        return value
    return (
        value[:max_chars]
        + f"...[TRUNCATED {len(value) - max_chars} chars for log hygiene]"
    )


def sanitize_event_for_logging(
    event: Any,
    *,
    extra_deny_keys: Iterable[str] = (),
    extra_truncate_keys: Iterable[str] = (),
    max_chars: int = _TRUNCATE_MAX_CHARS,
) -> Any:
    """Return a deep-copied version of ``event`` suitable for log output.

    Args:
        event: Any JSON-serializable object.
        extra_deny_keys: Additional key substrings to redact (case-insensitive).
        extra_truncate_keys: Additional key names whose string values to truncate.
        max_chars: Maximum length for truncated string values.

    Returns:
        A copy of ``event`` with denylisted keys redacted and long string
        values truncated. Returns the original input unchanged when it is
        not a dict/list.
    """
    deny_substrings = set(_DEFAULT_DENY_KEY_SUBSTRINGS) | {
        k.lower() for k in extra_deny_keys
    }
    truncate_keys = set(_DEFAULT_TRUNCATE_KEYS) | {
        k.lower() for k in extra_truncate_keys
    }
    try:
        copied = copy.deepcopy(event)
    except Exception:
        # Non-copyable objects (e.g., Lambda context) — coerce to a safe repr.
        return f"<uncopyable {type(event).__name__}>"
    return _walk(copied, deny_substrings, truncate_keys, max_chars)


def _walk(
    obj: Any,
    deny_substrings: Set[str],
    truncate_keys: Set[str],
    max_chars: int,
) -> Any:
    # Recurses into dicts and lists only. A tuple or set nested in the event is
    # returned as-is, so a denylisted key *inside* one would not be redacted:
    # `{"items": ({"password": "p"},)}` passes through untouched. Unreachable for
    # every current caller — Lambda/AppSync events are `json.loads` output, which
    # produces only dict/list/str/int/float/bool/None — so adding tuple and set
    # arms would be dead code. Revisit if a caller ever hands this hand-built
    # Python objects rather than a deserialized event.
    if isinstance(obj, dict):
        result = {}
        for key, value in obj.items():
            if not isinstance(key, str):
                # Non-string keys shouldn't occur in AppSync/Lambda events,
                # but preserve them verbatim if they do.
                result[key] = _walk(value, deny_substrings, truncate_keys, max_chars)
                continue
            if _matches_deny(key, deny_substrings):
                # Preserve a hint about the type so operators can see the
                # field was present without exposing its value.
                if value is None:
                    result[key] = None
                else:
                    result[key] = _REDACTED
                continue
            if key.lower() in truncate_keys and isinstance(value, str):
                result[key] = _truncate_string(value, max_chars)
                continue
            result[key] = _walk(value, deny_substrings, truncate_keys, max_chars)
        return result
    if isinstance(obj, list):
        return [_walk(v, deny_substrings, truncate_keys, max_chars) for v in obj]
    if isinstance(obj, str):
        # Top-level string (not under a truncate key) — leave unchanged;
        # the caller chose to log it explicitly.
        return obj
    return obj


# Convenience regex for scrubbing JWTs that may appear embedded in free-form
# strings (e.g., error messages). Not applied by default — callers can use
# `scrub_jwts_in_string()` if they explicitly log error text.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")


def scrub_jwts_in_string(text: str) -> str:
    """Replace anything that looks like a JWT in ``text`` with a placeholder."""
    if not isinstance(text, str):
        return text
    return _JWT_RE.sub(_REDACTED, text)
