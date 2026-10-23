# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``QueueDefStore`` on a moto HITLTable: seeding and deletion.

Only the Default queue is seeded, and it is re-seeded whenever it is missing, even
on a stack that has other queues. It is the only queue that can't be deleted: an
Escalation queue an older release seeded is an ordinary queue.
"""

import pytest

from idp_common.dynamodb.client import DynamoDBClient
from idp_common.review_queues import keys
from idp_common.review_queues.exceptions import ReviewValidationError
from idp_common.review_queues.queue_defs import (
    DEFAULT_QUEUE_ID,
    QueueDefinition,
    QueueDefStore,
)

from ._table import REGION


@pytest.fixture
def defs(review_table):
    client = DynamoDBClient(table_name=review_table, region=REGION)
    return QueueDefStore(lambda: client)


def _stored_ids(defs):
    items = []
    for page in defs.client.query_pages(
        "PK = :pk", expression_attribute_values={":pk": keys.QDEF_PK}
    ):
        items.extend(page.get("Items", []))
    return sorted(i["QueueId"] for i in items)


@pytest.mark.unit
class TestSeeding:
    def test_an_empty_stack_gets_only_an_enabled_default(self, defs):
        listed = defs.list()
        assert [(d.id, d.enabled) for d in listed] == [(DEFAULT_QUEUE_ID, True)]
        assert _stored_ids(defs) == [DEFAULT_QUEUE_ID]

    def test_default_is_reseeded_when_other_queues_exist(self, defs):
        defs.put(QueueDefinition(id="claims"), create=True)
        assert _stored_ids(defs) == ["claims"]
        assert {d.id for d in defs.list()} == {"claims", DEFAULT_QUEUE_ID}
        assert _stored_ids(defs) == ["claims", DEFAULT_QUEUE_ID]

    def test_an_existing_default_is_not_overwritten(self, defs):
        defs.put(QueueDefinition(id=DEFAULT_QUEUE_ID, enabled=False), create=True)
        assert defs.get(DEFAULT_QUEUE_ID).enabled is False

    def test_new_fields_survive_an_update(self, defs):
        defs.list()
        defs.put(
            QueueDefinition(id=DEFAULT_QUEUE_ID, enabled=False, priority=80),
            create=False,
        )
        default = defs.get(DEFAULT_QUEUE_ID)
        assert (default.enabled, default.priority) == (False, 80)


@pytest.mark.unit
class TestDelete:
    def test_default_is_protected(self, defs):
        defs.list()
        with pytest.raises(ReviewValidationError) as exc:
            defs.delete(DEFAULT_QUEUE_ID)
        assert exc.value.code == "PROTECTED_QUEUE"
        assert _stored_ids(defs) == [DEFAULT_QUEUE_ID]

    def test_an_escalation_queue_is_ordinary(self, defs):
        defs.put(QueueDefinition(id="escalation", priority=90), create=True)
        defs.delete("escalation")
        assert "escalation" not in _stored_ids(defs)
