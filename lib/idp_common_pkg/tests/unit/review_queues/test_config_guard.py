# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import logging

import pytest

from idp_common.review_queues.config_guard import (
    MAX_EMAIL_LOOKUPS,
    unknown_member_emails,
    warnings_for,
)


def _rq(*emails):
    return {"queues": [{"name": "default", "member_emails": list(emails)}]}


@pytest.mark.unit
class TestUnknownMemberEmails:
    def test_only_added_emails_are_checked(self):
        calls = []

        def lookup(email):
            calls.append(email)
            return None

        out = unknown_member_emails(
            [("p", _rq("old@x.com"), _rq("old@x.com", "new@x.com"))], lookup
        )
        assert out == ["new@x.com"]
        assert calls == ["new@x.com"]

    def test_persona_must_be_queue_reviewer_or_admin(self):
        rows = {
            "qr@x.com": {"persona": "QueueReviewer"},
            "ad@x.com": {"persona": "Admin"},
            "vw@x.com": {"persona": "Viewer"},
        }
        out = unknown_member_emails([("p", _rq(), _rq(*rows))], lambda e: rows.get(e))
        assert out == ["vw@x.com"]

    def test_lookups_are_bounded(self):
        calls = []

        def lookup(email):
            calls.append(email)
            return {"persona": "Admin"}

        emails = [f"u{i:02d}@x.com" for i in range(MAX_EMAIL_LOOKUPS + 1)]
        out = unknown_member_emails([("p", _rq(), _rq(*emails))], lookup)
        assert len(calls) == MAX_EMAIL_LOOKUPS
        assert out == [emails[-1]]

    def test_lookup_error_counts_as_unknown_and_warns(self, caplog):
        def lookup(email):
            raise RuntimeError("boom")

        with caplog.at_level(logging.WARNING):
            out = unknown_member_emails([("p", _rq(), _rq("a@x.com"))], lookup)
        assert out == ["a@x.com"]
        assert "lookup failed" in caplog.text
        assert "a@x.com" not in caplog.text

    def test_warning_text(self):
        assert warnings_for(["a@x.com"]) == [
            "Queue member a@x.com is not a QueueReviewer or Admin user; they will not "
            "be able to work the queue until assigned one of those roles."
        ]
