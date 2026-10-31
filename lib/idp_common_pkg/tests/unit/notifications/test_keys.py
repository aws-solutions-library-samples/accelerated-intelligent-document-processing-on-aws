# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Recipient keys, ids and cursors."""

import base64
import json

import pytest

from idp_common.api_adapter import normalize_event
from idp_common.notifications.exceptions import (
    InvalidCursorError,
    NotificationValidationError,
)
from idp_common.notifications.keys import (
    decode_cursor,
    dedupe_sk,
    encode_cursor,
    idempotency_sk,
    new_notification_id,
    normalize_recipient_key,
    partition_key,
    recipient_key_from_claims,
    recipient_key_from_cognito_user,
)

CLAIM_SETS = [
    {"email": "Alice@Example.com", "cognito:username": "alice-u", "sub": "sub-1"},
    {"cognito:username": "Bob-User", "sub": "sub-2"},
    {"sub": "SUB-3"},
]


def _b64(obj) -> str:
    raw = json.dumps(obj).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.mark.unit
class TestRecipientKeys:
    @pytest.mark.parametrize(
        "claims,expected",
        list(zip(CLAIM_SETS, ["alice@example.com", "bob-user", "sub-3"])),
    )
    def test_claim_precedence(self, claims, expected):
        assert recipient_key_from_claims(claims) == expected

    def test_whitespace_and_case_are_normalized(self):
        assert normalize_recipient_key("  MiXed@Case.COM \n") == "mixed@case.com"
        assert partition_key(" A@B.c ") == "USER#a@b.c"

    @pytest.mark.parametrize("claims", [{}, {"email": "  "}, {"sub": ""}])
    def test_empty_claims_raise(self, claims):
        with pytest.raises(NotificationValidationError):
            recipient_key_from_claims(claims)

    def test_cognito_native_user(self):
        user = {
            "Username": "alice-u",
            "Attributes": [
                {"Name": "sub", "Value": "sub-1"},
                {"Name": "email", "Value": "Alice@Example.com"},
            ],
        }
        assert recipient_key_from_cognito_user(user) == "alice@example.com"

    def test_cognito_federated_user_uses_email(self):
        user = {
            "Username": "Provider_xyz",
            "Attributes": [{"Name": "email", "Value": "fed@example.com"}],
        }
        assert recipient_key_from_cognito_user(user) == "fed@example.com"

    def test_cognito_user_without_email_falls_back_to_username(self):
        user = {"Username": "No-Email", "Attributes": [{"Name": "sub", "Value": "s"}]}
        assert recipient_key_from_cognito_user(user) == "no-email"


@pytest.mark.unit
class TestParityWithApiAdapter:
    """The key the API derives must be the key producers write under."""

    @pytest.mark.parametrize("claims", CLAIM_SETS)
    def test_parity_pin(self, claims):
        event = {
            "resource": "/op/{field}",
            "path": "/op/listInboxMessages",
            "httpMethod": "POST",
            "pathParameters": {"field": "listInboxMessages"},
            "body": json.dumps({"arguments": {}}),
            "requestContext": {
                "authorizer": {"claims": {**claims, "cognito:groups": "Viewer"}}
            },
        }
        ev = normalize_event(event)
        assert recipient_key_from_claims(
            ev["identity"]["claims"]
        ) == normalize_recipient_key(ev["identity"]["username"])


@pytest.mark.unit
class TestIds:
    def test_ids_sort_by_time_across_milliseconds(self):
        ids = [
            new_notification_id(1_700_000_000_999, "ffffffffffff"),
            new_notification_id(1_700_000_001_000, "000000000000"),
            new_notification_id(1_700_000_001_001, "123456789abc"),
        ]
        assert ids == sorted(ids)
        assert all(len(i) == 25 for i in ids)

    def test_random_suffix_is_lowercase_hex(self):
        nid = new_notification_id()
        assert len(nid) == 25 and nid[13:] == nid[13:].lower()

    def test_hashed_sort_keys(self):
        assert dedupe_sk("x").startswith("D#") and len(dedupe_sk("x")) == 34
        assert idempotency_sk("x").startswith("I#")
        assert dedupe_sk("x")[2:] == idempotency_sk("x")[2:]


@pytest.mark.unit
class TestCursor:
    def test_round_trip(self):
        nid = new_notification_id(1_700_000_000_000, "abcdefabcdef")
        token = encode_cursor(nid)
        assert "=" not in token
        assert decode_cursor(token) == nid

    @pytest.mark.parametrize(
        "token",
        [
            "not base64 !!",
            _b64("plain string"),
            _b64({"v": 2, "a": "1700000000000abcdefabcdef"}),
            _b64({"v": 1, "a": "1700000000000ABCDEFABCDEF"}),
            _b64({"v": 1, "a": "x"}),
            _b64({"v": 1}),
            "A" * 257,
            "",
        ],
    )
    def test_bad_cursors_raise(self, token):
        with pytest.raises(InvalidCursorError):
            decode_cursor(token)
