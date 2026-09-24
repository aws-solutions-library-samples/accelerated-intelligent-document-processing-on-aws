# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for `log_sanitizer`, the redactor this Lambda runs over its invocation event
before printing it.

The event reaches this function from the web UI's resolver and carries the caller's
Cognito identity, including the ID token. CloudWatch log groups on a deployed stack are
long-lived and readable by anyone with `logs:FilterLogEvents`, so a credential printed
once is a credential disclosed for the group's whole retention period. The redactor is
therefore a security control, and its failure mode is silent: the handler still logs,
the report still succeeds, and nobody notices until the logs are audited.

Two things shape these tests.

**Redaction is asserted on the value, not on the function having been called.** A
mutation that replaced the redactor with the identity function would satisfy any
"was it invoked" check while publishing every token. Each case here therefore asserts
both that the secret is gone and that the surrounding structure survived — a redactor
that flattened the event would also hide the secret, and would make the log useless.

**The recursion's boundaries are pinned, including where it does not reach.** Denylisted
keys are matched case-insensitively and by substring at every depth of a dict or list,
but a value nested inside a tuple or a set is returned untouched. That is safe for this
caller — a Lambda event is `json.loads` output, which has neither — and it is exactly
the assumption that would quietly break if the function were reused for hand-built
Python objects, so it is stated as a test rather than only as a comment.
"""

from __future__ import annotations

import json

import pytest
from log_sanitizer import sanitize_event_for_logging, scrub_jwts_in_string

REDACTED = "***REDACTED***"

JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NSIsImVtYWlsIjoiYUBiLmNvbSJ9"
    ".dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
)


@pytest.mark.unit
def test_the_whole_identity_subtree_is_replaced_rather_than_walked():
    """`identity` is denied as a key, so nothing inside it is inspected at all.

    That is the stronger choice for this event shape: the subtree holds the Cognito
    claims blob, whose contents vary by pool configuration, so walking it and
    matching key names would redact only the keys somebody thought of. The caller's
    email and groups are inside it and must not reach the log either.
    """
    event = {
        "arguments": {"input": "{}"},
        "identity": {
            "username": "operator",
            "claims": {"email": "operator@example.com", "cognito:groups": ["Admin"]},
            "sourceIp": ["10.0.0.1"],
        },
    }
    clean = sanitize_event_for_logging(event)

    assert clean["identity"] == REDACTED
    assert "operator@example.com" not in json.dumps(clean)
    # The part of the event an operator needs is intact.
    assert clean["arguments"] == {"input": "{}"}


@pytest.mark.unit
def test_a_credential_nested_below_the_top_level_is_redacted_at_any_depth():
    """Nothing a Lambda receives is flat, so a top-level-only check protects nothing.

    The bearer header here sits inside a dict inside a list inside a dict, and the
    match is on a substring of the key and case-insensitive, so `Authorization` and
    `x-api-key` are both caught.
    """
    event = {
        "request": {
            "headers": [
                {"Authorization": f"Bearer {JWT}"},
                {"X-Api-Key": "abcd1234", "content-type": "application/json"},
            ]
        },
        "detail": {"nested": {"deeper": {"refreshToken": JWT}}},
    }
    clean = sanitize_event_for_logging(event)

    assert clean["request"]["headers"][0]["Authorization"] == REDACTED
    assert clean["request"]["headers"][1]["X-Api-Key"] == REDACTED
    assert clean["detail"]["nested"]["deeper"]["refreshToken"] == REDACTED
    assert JWT not in json.dumps(clean)
    # A header that is not a credential is preserved, or the log says nothing useful.
    assert clean["request"]["headers"][1]["content-type"] == "application/json"


@pytest.mark.unit
def test_the_original_event_is_not_modified():
    """The handler goes on to use the event after logging it.

    Redacting in place would hand the rest of the function `"***REDACTED***"` where
    it expected the caller's input, so every report from an authenticated caller
    would fail.
    """
    event = {"identity": {"token": JWT}}
    sanitize_event_for_logging(event)
    assert event["identity"]["token"] == JWT


@pytest.mark.unit
def test_a_present_but_empty_credential_is_distinguishable_from_an_absent_one():
    """`None` is preserved; any other value becomes the placeholder.

    That is deliberate and worth keeping: "the field was there and empty" and "the
    field was there with a value" are different diagnoses for an authorization
    failure, and collapsing both to the placeholder loses the one an operator can
    act on.
    """
    payload = {
        # nosec B105 - the key NAME is the input under test; there is no secret here
        "password": None,
        "apiKey": "",
    }
    clean = sanitize_event_for_logging(payload)
    assert clean["password"] is None
    assert clean["apiKey"] == REDACTED


@pytest.mark.unit
def test_a_long_free_text_value_is_truncated_with_the_dropped_length_reported():
    """OCR text in an event would otherwise dominate the log group's volume.

    The count of dropped characters is part of the message, so a reader can tell a
    truncated value from a short one — without it, a 500-character prefix looks like
    the whole value.
    """
    clean = sanitize_event_for_logging({"ocr_text": "a" * 650})
    assert clean["ocr_text"].startswith("a" * 500)
    assert "TRUNCATED 150 chars" in clean["ocr_text"]
    assert len(clean["ocr_text"]) < 650


@pytest.mark.unit
def test_a_value_exactly_at_the_limit_is_left_whole():
    clean = sanitize_event_for_logging({"prompt": "b" * 500})
    assert clean["prompt"] == "b" * 500


@pytest.mark.unit
def test_truncation_applies_only_to_string_values_under_a_named_key():
    """A `text` key holding a structure is walked, not stringified and cut."""
    clean = sanitize_event_for_logging({"text": {"password": JWT, "pages": 3}})
    assert clean["text"] == {"password": REDACTED, "pages": 3}


@pytest.mark.unit
def test_a_caller_can_add_its_own_denied_and_truncated_keys():
    """Both extension points are case-insensitive on the key."""
    clean = sanitize_event_for_logging(
        {"customerRef": "C-12345", "notes": "n" * 40},
        extra_deny_keys=["CUSTOMERREF"],
        extra_truncate_keys=["NOTES"],
        max_chars=10,
    )
    assert clean["customerRef"] == REDACTED
    assert clean["notes"].startswith("n" * 10)
    assert "TRUNCATED 30 chars" in clean["notes"]


@pytest.mark.unit
def test_a_non_string_key_is_preserved_and_its_value_still_walked():
    """Not reachable from `json.loads`, but the recursion must not lose data.

    The key is kept verbatim and the subtree under it is still redacted, so a
    hand-built dict cannot smuggle a credential past the walk by keying it with an
    integer.
    """
    clean = sanitize_event_for_logging({7: {"password": JWT}, "ok": 1})
    assert clean[7] == {"password": REDACTED}
    assert clean["ok"] == 1


@pytest.mark.unit
def test_a_credential_inside_a_tuple_is_not_redacted():
    """The documented boundary of the walk, asserted rather than assumed.

    Tuples and sets are returned as-is because a deserialized Lambda event contains
    neither, so the arms would be dead code. Pinned so that reusing this redactor on
    hand-built Python objects fails a test instead of shipping a disclosure.
    """
    event = {"items": ({"password": JWT},), "tags": {"plain"}}
    clean = sanitize_event_for_logging(event)
    assert clean["items"][0]["password"] == JWT
    assert clean["tags"] == {"plain"}


@pytest.mark.unit
@pytest.mark.parametrize("value", ["a bare string", 42, None, True, 3.5])
def test_a_scalar_event_is_returned_unchanged(value):
    """The caller chose to log it explicitly, so it is not second-guessed."""
    assert sanitize_event_for_logging(value) == value


@pytest.mark.unit
def test_an_uncopyable_object_is_described_rather_than_logged():
    """The Lambda context object cannot be deep-copied.

    Falling back to a type name keeps one bad argument from raising inside the log
    statement, which would fail the invocation for a diagnostic.
    """

    class Context:
        def __deepcopy__(self, _memo):
            raise TypeError("not copyable")

    assert sanitize_event_for_logging(Context()) == "<uncopyable Context>"


@pytest.mark.unit
def test_a_token_embedded_in_free_text_can_be_scrubbed_separately():
    """Key-based redaction cannot see a token inside an error message.

    The surrounding words are kept, because the message is the reason the line is
    being logged at all.
    """
    text = f"Unauthorized request with token {JWT} from 10.0.0.1"
    scrubbed = scrub_jwts_in_string(text)
    assert JWT not in scrubbed
    assert REDACTED in scrubbed
    assert scrubbed.startswith("Unauthorized request with token ")
    assert scrubbed.endswith(" from 10.0.0.1")


@pytest.mark.unit
def test_scrubbing_a_non_string_returns_it_untouched():
    """Callers pass whatever an exception handler gave them."""
    assert scrub_jwts_in_string(None) is None
    assert scrub_jwts_in_string(7) == 7
