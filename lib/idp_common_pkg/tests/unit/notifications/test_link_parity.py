# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The links idp_common builds equal the UI's, byte for byte.

Inbox messages store their link verbatim, so a link built differently from the
route the UI decodes opens nothing. The expected values are in one fixture,
``tests/resources/link_parity.json``, which the UI's
``src/ui/src/routes/__tests__/link-parity.test.ts`` reads too.
"""

import json
from pathlib import Path

import pytest

from idp_common.notifications.links import document_detail_link, document_edit_link
from idp_common.review_queues.testset_docs import item_link

FIXTURE = Path(__file__).resolve().parents[2] / "resources" / "link_parity.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.unit
@pytest.mark.parametrize("case", CASES, ids=[c["objectKey"] for c in CASES])
class TestLinkParity:
    def test_document_detail_link(self, case):
        assert document_detail_link(case["objectKey"]) == case["documentDetail"]

    def test_document_edit_link(self, case):
        assert document_edit_link(case["objectKey"]) == case["documentEdit"]

    def test_test_set_item_link(self, case):
        assert item_link(case["testSetId"], case["objectKey"]) == case["testSetItem"]


@pytest.mark.unit
def test_the_fixture_has_the_hard_keys():
    keys = {c["objectKey"] for c in CASES}
    assert any("%" in k for k in keys)
    assert any("/" in k for k in keys)
    assert any(not k.isascii() for k in keys)
