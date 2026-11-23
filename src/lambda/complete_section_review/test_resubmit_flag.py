# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Skip All Reviews' ``resubmit`` option (on by default)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from test_finished_review_guards import KEY, OBJECT_KEY, mod  # noqa: F401

REASON = "Duplicate document"


def _event(**arguments):
    return {
        "info": {"fieldName": "skipAllSectionsReview"},
        "arguments": {"objectKey": OBJECT_KEY, "reason": REASON, **arguments},
        "identity": {
            "username": "admin",
            "claims": {"email": "admin@example.com", "cognito:groups": ["Admin"]},
        },
    }


def _history(mod):  # noqa: F811
    item = mod.dynamodb.Table(mod.TRACKING_TABLE_NAME).get_item(Key=KEY)["Item"]
    return item["HITLReviewHistory"][-1]


@pytest.mark.unit
class TestResubmitFlag:
    def test_unticked_skips_without_reprocessing(self, mod):  # noqa: F811
        with (
            patch.object(mod, "trigger_reprocessing") as reprocess,
            patch.object(mod, "build_document_response", return_value={}),
        ):
            mod.handler(_event(resubmit=False), None)
        reprocess.assert_not_called()
        assert _history(mod)["resubmitted"] is False

    def test_absent_means_resubmit(self, mod):  # noqa: F811
        with (
            patch.object(mod, "trigger_reprocessing") as reprocess,
            patch.object(mod, "build_document_response", return_value={}),
        ):
            mod.handler(_event(), None)
        reprocess.assert_called_once_with(OBJECT_KEY)
        assert _history(mod)["resubmitted"] is True

    def test_a_non_boolean_is_refused(self, mod):  # noqa: F811
        with pytest.raises(ValueError, match="resubmit must be a boolean"):
            mod.handler(_event(resubmit="yes"), None)
