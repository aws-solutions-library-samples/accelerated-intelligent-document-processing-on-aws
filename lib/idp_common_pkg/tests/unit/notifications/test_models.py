# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Request validation bounds and deep-link rules."""

import pytest
from pydantic import ValidationError

from idp_common.notifications.exceptions import NotificationValidationError
from idp_common.notifications.links import document_detail_link, validate_link
from idp_common.notifications.models import NotificationRequest, PostResult


def _req(**overrides):
    base = {"type_id": "test.sample", "title": "t", "source": "tests", "users": ["u"]}
    base.update(overrides)
    return NotificationRequest(**base)


@pytest.mark.unit
class TestBounds:
    def test_title_200_passes_201_fails(self):
        assert len(_req(title="x" * 200).title) == 200
        with pytest.raises(ValidationError):
            _req(title="x" * 201)

    def test_title_is_stripped_and_rejects_control_characters(self):
        assert _req(title="  hi  ").title == "hi"
        with pytest.raises(ValidationError):
            _req(title="a\x07b")
        with pytest.raises(ValidationError):
            _req(title="   ")

    def test_body_4000_passes_4001_fails(self):
        assert len(_req(body="x" * 4000).body) == 4000
        with pytest.raises(ValidationError):
            _req(body="x" * 4001)
        with pytest.raises(ValidationError):
            _req(body="a\x00b")

    def test_metadata_key_count(self):
        _req(metadata={f"k{i}": i for i in range(20)})
        with pytest.raises(ValidationError):
            _req(metadata={f"k{i}": i for i in range(21)})

    def test_metadata_size(self):
        # {"k":"<v>"} is 8 bytes of JSON around the value.
        _req(metadata={"k": "x" * 2040})
        with pytest.raises(ValidationError):
            _req(metadata={"k": "x" * 2041})

    def test_metadata_values_must_be_scalar(self):
        with pytest.raises(ValidationError):
            _req(metadata={"k": [1, 2]})
        with pytest.raises(ValidationError):
            _req(metadata={"k": {"nested": 1}})

    def test_metadata_key_pattern(self):
        with pytest.raises(ValidationError):
            _req(metadata={"1bad": 1})

    def test_severity_none_is_allowed(self):
        assert _req().severity is None

    def test_recipients_required_and_bounded(self):
        with pytest.raises(ValidationError):
            _req(users=[])
        with pytest.raises(ValidationError):
            _req(users=[f"u{i}" for i in range(101)])
        with pytest.raises(ValidationError):
            _req(users=[], groups=[f"g{i}" for i in range(11)])
        assert _req(users=[], groups=["Admin"]).groups == ["Admin"]

    def test_users_are_normalized(self):
        assert _req(users=[" Alice@X.com "]).users == ["alice@x.com"]

    def test_source_and_group_patterns(self):
        with pytest.raises(ValidationError):
            _req(source="Bad Source")
        with pytest.raises(ValidationError):
            _req(groups=["bad group"])

    def test_post_result_counts(self):
        result = PostResult(
            deliveries=[
                {"recipient_hash": "a", "outcome": "delivered"},
                {"recipient_hash": "b", "outcome": "muted"},
                {"recipient_hash": "c", "outcome": "failed"},
                {"recipient_hash": "d", "outcome": "duplicate"},
            ],
            recipients_considered=4,
            truncated=False,
        )
        assert (result.delivered, result.muted, result.failed, result.duplicates) == (
            1,
            1,
            1,
            1,
        )


@pytest.mark.unit
class TestLinks:
    @pytest.mark.parametrize(
        "link",
        [
            "https://x",
            "//x",
            "/\\x",
            "javascript:alert(1)",
            "/a b",
            "/a\x01",
            "/a\x7f",
            "/a\x85",
            "/a\u2003b",
            "/x?u=https://y",
            "/x?u=HTTPS://y",
            "x/y",
            "",
            "/" + "a" * 1024,
        ],
    )
    def test_rejects(self, link):
        with pytest.raises(NotificationValidationError):
            validate_link(link)

    @pytest.mark.parametrize(
        "link", ["/documents/a%2Fb", "/inbox", "/x?u=https%3A%2F%2Fy", "/" + "a" * 1023]
    )
    def test_accepts(self, link):
        assert validate_link(link) == link

    def test_request_rejects_external_link(self):
        with pytest.raises(ValidationError):
            _req(link="https://evil.example")

    def test_document_detail_link_matches_encode_uri_component(self):
        assert document_detail_link("a/b c!.pdf") == "/documents/a%2Fb%20c!.pdf"
        assert document_detail_link("x~*'()_-.y") == "/documents/x~*'()_-.y"
