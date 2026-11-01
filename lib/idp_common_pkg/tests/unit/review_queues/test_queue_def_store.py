# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``QueueDefStore`` on a moto HITLTable: seeding, deletion and versioned updates.

Only the Default queue is seeded, and it is re-seeded whenever it is missing, even
on a stack that has other queues. It is the only queue that can't be deleted: an
Escalation queue an older release seeded is an ordinary queue.
"""

import pytest

from idp_common.dynamodb.client import (
    DynamoDBClient,
    DynamoDBError,
    DynamoDBTransactionCanceledError,
)
from idp_common.review_queues import keys
from idp_common.review_queues.exceptions import ReviewConflict, ReviewValidationError
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


def _retired(defs, queue_id):
    item = defs.client.get_item(keys.qdef_retired_key(queue_id), consistent_read=True)
    return None if item is None else item["Version"]


def _raw_item(defs, queue_id):
    return defs.client.get_item(keys.qdef_key(queue_id), consistent_read=True)


@pytest.mark.unit
class TestVersionedUpdates:
    """Optimistic concurrency on a queue definition (#1727).

    These run on moto, so they establish what this code sends and how moto evaluates
    the condition expressions; they are not a measurement of DynamoDB itself.
    """

    def test_a_create_is_version_1_and_each_update_adds_one(self, defs):
        created = defs.put(QueueDefinition(id="claims"), create=True)
        assert created.version == 1
        assert _raw_item(defs, "claims")["Version"] == 1
        updated = defs.put(
            QueueDefinition(id="claims", priority=70), create=False, expected_version=1
        )
        assert updated.version == 2
        assert defs.read_current("claims").version == 2
        assert defs.get("claims").version == 2

    def test_two_updates_from_one_read_refuse_the_second(self, defs):
        """The lost update: both Admins opened the queue at version 1."""
        defs.put(QueueDefinition(id="claims"), create=True)
        read = defs.get("claims")
        first = read.model_copy(update={"priority": 80})
        second = read.model_copy(update={"eligible_annotators": ["b@example.com"]})
        defs.put(first, create=False, expected_version=read.version)
        with pytest.raises(ReviewConflict) as exc:
            defs.put(second, create=False, expected_version=read.version)
        assert exc.value.code == "QUEUE_CHANGED"
        stored = defs.read_current("claims")
        # The first Admin's change survives, and the refused one wrote nothing.
        assert (stored.priority, stored.eligible_annotators, stored.version) == (
            80,
            [],
            2,
        )

    def test_a_refused_update_leaves_no_stale_cache(self, defs):
        defs.put(QueueDefinition(id="claims"), create=True)
        defs.get("claims")  # cached at version 1
        # Another instance writes version 2 behind this one's cache.
        other = QueueDefStore(lambda: defs.client)
        other.put(
            QueueDefinition(id="claims", priority=90), create=False, expected_version=1
        )
        assert defs.get("claims").version == 1  # still the cached read
        with pytest.raises(ReviewConflict):
            defs.put(QueueDefinition(id="claims"), create=False, expected_version=1)
        assert defs.get("claims").version == 2

    def test_an_update_of_a_missing_queue_is_unknown_not_changed(self, defs):
        for expected in (None, 0, 3):
            with pytest.raises(ReviewValidationError) as exc:
                defs.put(
                    QueueDefinition(id="ghost"),
                    create=False,
                    expected_version=expected,
                )
            assert exc.value.code == "UNKNOWN_QUEUE"
        assert _raw_item(defs, "ghost") is None

    def test_a_definition_stored_before_versions_reads_as_0(self, defs):
        item = QueueDefinition(id="legacy", priority=40).to_item()
        del item["Version"]
        defs.client.put_item(item)
        assert defs.read_current("legacy").version == 0
        with pytest.raises(ReviewConflict):
            defs.put(QueueDefinition(id="legacy"), create=False, expected_version=1)
        updated = defs.put(
            QueueDefinition(id="legacy", priority=60), create=False, expected_version=0
        )
        assert updated.version == 1
        # Version 0 matches only a definition that has no Version yet.
        with pytest.raises(ReviewConflict):
            defs.put(QueueDefinition(id="legacy"), create=False, expected_version=0)

    def test_an_update_naming_no_version_replaces_and_still_counts(self, defs):
        defs.put(QueueDefinition(id="claims"), create=True)
        defs.put(QueueDefinition(id="claims"), create=False, expected_version=1)
        replaced = defs.put(QueueDefinition(id="claims", priority=10), create=False)
        assert replaced.version == 3
        assert defs.read_current("claims").priority == 10

    def test_an_unversioned_update_retries_past_a_concurrent_write(self, defs):
        defs.put(QueueDefinition(id="claims"), create=True)
        real = defs.read_current
        calls = []

        def racing_read(queue_id):
            current = real(queue_id)
            if not calls:
                # Another writer lands between this read and the put.
                QueueDefStore(lambda: defs.client).put(
                    QueueDefinition(id="claims", priority=99),
                    create=False,
                    expected_version=current.version,
                )
            calls.append(queue_id)
            return current

        defs.read_current = racing_read
        replaced = defs.put(QueueDefinition(id="claims", priority=5), create=False)
        assert replaced.version == 3
        assert real("claims").priority == 5

    def test_an_unversioned_update_gives_up_under_constant_contention(self, defs):
        defs.put(QueueDefinition(id="claims"), create=True)
        stale = defs.read_current("claims")
        defs.put(QueueDefinition(id="claims"), create=False, expected_version=1)
        defs.read_current = lambda queue_id: stale  # always behind
        with pytest.raises(ReviewConflict) as exc:
            defs.put(QueueDefinition(id="claims"), create=False)
        assert exc.value.code == "QUEUE_CHANGED"

    def test_the_seeded_default_is_version_1(self, defs):
        defs.list()
        assert defs.read_current(DEFAULT_QUEUE_ID).version == 1

    def test_a_re_created_queue_continues_from_its_deleted_version(self, defs):
        defs.put(QueueDefinition(id="claims"), create=True)
        for expected in (1, 2):
            defs.put(
                QueueDefinition(id="claims"), create=False, expected_version=expected
            )
        defs.delete("claims")
        assert defs.client.get_item(keys.qdef_retired_key("claims"))["Version"] == 3
        recreated = defs.put(QueueDefinition(id="claims"), create=True)
        assert recreated.version == 4
        assert defs.read_current("claims").version == 4

    @pytest.mark.parametrize("updates_after", [0, 2])
    def test_a_form_from_before_a_delete_is_refused_by_the_new_queue(
        self, defs, updates_after
    ):
        """Admin A holds a form at version 1; B deletes and re-creates the queue.

        Without the retired version, the new queue restarts at 1, and A's save would
        match it (or match again once B's saves bring it back to A's number).
        """
        defs.put(QueueDefinition(id="claims"), create=True)
        held = defs.read_current("claims")
        defs.delete("claims")
        defs.put(QueueDefinition(id="claims", name="B"), create=True)
        version = defs.read_current("claims").version
        for _ in range(updates_after):
            version = defs.put(
                QueueDefinition(id="claims", name="B"),
                create=False,
                expected_version=version,
            ).version
        for expected in range(held.version, held.version + updates_after + 1):
            if expected == version:
                continue
            with pytest.raises(ReviewConflict):
                defs.put(
                    held.model_copy(update={"name": "A stale edit"}),
                    create=False,
                    expected_version=expected,
                )
        assert defs.read_current("claims").name == "B"
        assert version > held.version + updates_after

    def test_a_create_racing_a_delete_continues_past_it(self, defs):
        """The create read no retired version; a delete completes before it writes."""
        defs.put(QueueDefinition(id="claims"), create=True)
        for expected in (1, 2):
            defs.put(
                QueueDefinition(id="claims"), create=False, expected_version=expected
            )
        other = QueueDefStore(lambda: defs.client)
        real = defs._retired_version
        reads = []

        def racing(queue_id):
            seen = real(queue_id)
            if not reads:
                other.delete("claims")  # lands between this read and the write
            reads.append(seen)
            return seen

        defs._retired_version = racing
        # The queue still existed when the create began, so this is the first read.
        recreated = defs.put(QueueDefinition(id="claims", name="B"), create=True)
        assert reads == [None, 3]
        assert recreated.version == 4
        with pytest.raises(ReviewConflict):
            defs.put(QueueDefinition(id="claims"), create=False, expected_version=3)

    def test_a_create_racing_a_create_and_delete_continues_past_both(self, defs):
        """The retired version moves on (3 -> 4) between this create's read and write."""
        defs.put(QueueDefinition(id="claims"), create=True)
        for expected in (1, 2):
            defs.put(
                QueueDefinition(id="claims"), create=False, expected_version=expected
            )
        defs.delete("claims")
        other = QueueDefStore(lambda: defs.client)
        real = defs._retired_version
        reads = []

        def racing(queue_id):
            seen = real(queue_id)
            if not reads:
                other.put(QueueDefinition(id="claims"), create=True)  # version 4
                other.delete("claims")  # retired 4
            reads.append(seen)
            return seen

        defs._retired_version = racing
        recreated = defs.put(QueueDefinition(id="claims"), create=True)
        assert reads == [3, 4]
        assert recreated.version == 5

    def test_a_delete_racing_an_update_retires_the_newer_version(self, defs):
        defs.put(QueueDefinition(id="claims"), create=True)
        other = QueueDefStore(lambda: defs.client)
        real = defs.read_current
        calls = []

        def racing(queue_id):
            current = real(queue_id)
            if not calls:
                other.put(
                    QueueDefinition(id="claims", priority=9),
                    create=False,
                    expected_version=current.version,
                )
            calls.append(queue_id)
            return current

        defs.read_current = racing
        defs.delete("claims")
        assert real("claims") is None
        assert _retired(defs, "claims") == 2
        assert defs.put(QueueDefinition(id="claims"), create=True).version == 3

    @pytest.mark.parametrize("stored_version", [2, None])
    def test_a_retired_version_is_never_lowered(self, defs, stored_version):
        """Stored below its retired version: written by an older release (no Version).

        That state needs an older release writing under a retired id (a rollback, or
        old and new code side by side during a deploy). The delete still goes through
        and keeps the higher version, so a re-created queue continues past both.
        """
        defs.client.put_item({**keys.qdef_retired_key("claims"), "Version": 10})
        item = QueueDefinition(id="claims", version=stored_version or 0).to_item()
        if stored_version is None:
            del item["Version"]
        defs.client.put_item(item)
        defs.delete("claims")
        assert defs.read_current("claims") is None
        assert _retired(defs, "claims") == 10
        assert defs.put(QueueDefinition(id="claims"), create=True).version == 11

    def test_a_failed_delete_changes_nothing_and_clears_the_cache(
        self, defs, monkeypatch
    ):
        defs.put(QueueDefinition(id="claims"), create=True)
        defs.list()  # cached

        def throttled(*_a, **_k):
            raise RuntimeError("throttled")

        monkeypatch.setattr(defs.client, "transact_write_items", throttled)
        with pytest.raises(RuntimeError, match="throttled"):
            defs.delete("claims")
        assert defs.read_current("claims").version == 1
        assert _retired(defs, "claims") is None
        assert defs._cached is None

    def test_creating_an_existing_queue_is_refused(self, defs):
        defs.put(QueueDefinition(id="claims", priority=7), create=True)
        with pytest.raises(ReviewValidationError) as exc:
            defs.put(QueueDefinition(id="claims"), create=True)
        assert exc.value.code == "QUEUE_EXISTS"
        assert defs.read_current("claims").priority == 7

    @staticmethod
    def _cancelled(code):
        return DynamoDBTransactionCanceledError(
            "cancelled", cancellation_reasons=[{"Code": code}, {"Code": "None"}]
        )

    @pytest.mark.parametrize("operation", ["create", "delete"])
    def test_a_cancellation_for_another_reason_propagates(
        self, defs, monkeypatch, operation
    ):
        if operation == "delete":
            defs.put(QueueDefinition(id="claims"), create=True)
        attempts = []

        def refuse(*_a, **_k):
            attempts.append(1)
            raise self._cancelled("ItemCollectionSizeLimitExceeded")

        monkeypatch.setattr(defs.client, "transact_write_items", refuse)
        with pytest.raises(DynamoDBTransactionCanceledError):
            if operation == "create":
                defs.put(QueueDefinition(id="claims"), create=True)
            else:
                defs.delete("claims")
        assert attempts == [1]

    @pytest.mark.parametrize("operation", ["create", "delete"])
    def test_a_transaction_conflict_is_retried(self, defs, monkeypatch, operation):
        """Another transaction held an item: the next attempt goes through."""
        if operation == "delete":
            defs.put(QueueDefinition(id="claims"), create=True)
        real = defs.client.transact_write_items
        attempts = []

        def once(items):
            attempts.append(1)
            if len(attempts) == 1:
                raise self._cancelled("TransactionConflict")
            return real(items)

        monkeypatch.setattr(defs.client, "transact_write_items", once)
        if operation == "create":
            assert defs.put(QueueDefinition(id="claims"), create=True).version == 1
        else:
            defs.delete("claims")
            assert defs.read_current("claims") is None
        assert len(attempts) == 2

    @pytest.mark.parametrize("operation", ["create", "delete"])
    def test_a_lasting_transaction_conflict_is_queue_changed(
        self, defs, monkeypatch, operation
    ):
        if operation == "delete":
            defs.put(QueueDefinition(id="claims"), create=True)

        def conflict(*_a, **_k):
            raise self._cancelled("TransactionConflict")

        monkeypatch.setattr(defs.client, "transact_write_items", conflict)
        with pytest.raises(ReviewConflict) as exc:
            if operation == "create":
                defs.put(QueueDefinition(id="claims"), create=True)
            else:
                defs.delete("claims")
        assert exc.value.code == "QUEUE_CHANGED"

    def test_an_update_meeting_a_transaction_is_queue_changed(self, defs, monkeypatch):
        defs.put(QueueDefinition(id="claims"), create=True)

        def held(*_a, **_k):
            raise DynamoDBError("in a transaction", "TransactionConflictException")

        monkeypatch.setattr(defs.client, "put_item", held)
        with pytest.raises(ReviewConflict) as exc:
            defs.put(QueueDefinition(id="claims"), create=False, expected_version=1)
        assert exc.value.code == "QUEUE_CHANGED"

    def test_creating_an_existing_queue_racing_a_delete_is_queue_exists(self, defs):
        """Both conditions false: the queue exists, and the retired version moved."""
        defs.put(QueueDefinition(id="claims"), create=True)
        defs._retired_version = lambda queue_id: 7  # not what is stored (none)
        with pytest.raises(ReviewValidationError) as exc:
            defs.put(QueueDefinition(id="claims"), create=True)
        assert exc.value.code == "QUEUE_EXISTS"

    def test_a_create_under_constant_deletes_gives_up(self, defs):
        defs._retired_version = lambda queue_id: 7  # never what is stored
        with pytest.raises(ReviewConflict) as exc:
            defs.put(QueueDefinition(id="claims"), create=True)
        assert exc.value.code == "QUEUE_CHANGED"
        assert defs.read_current("claims") is None

    def test_the_retired_version_is_read_consistently(self, defs, monkeypatch):
        seen = []
        real = defs.client.get_item

        def spy(key, **kwargs):
            if key["PK"] == keys.QDEF_RETIRED_PK:
                seen.append(kwargs.get("consistent_read"))
            return real(key, **kwargs)

        monkeypatch.setattr(defs.client, "get_item", spy)
        defs.put(QueueDefinition(id="claims"), create=True)
        assert seen == [True]

    def test_deleting_a_missing_queue_retires_nothing(self, defs):
        defs.delete("ghost")
        assert defs.client.get_item(keys.qdef_retired_key("ghost")) is None

    def test_a_retired_version_is_not_listed(self, defs):
        defs.put(QueueDefinition(id="claims"), create=True)
        defs.delete("claims")
        assert "claims" not in {d.id for d in defs.list(fresh=True)}

    def test_a_fresh_list_skips_the_cache_and_reads_consistently(
        self, defs, monkeypatch
    ):
        defs.list()  # cached
        QueueDefStore(lambda: defs.client).put(
            QueueDefinition(id="claims"), create=True
        )
        assert "claims" not in {d.id for d in defs.list()}
        seen = []
        real = defs.client.query_pages

        def spy(*args, **kwargs):
            seen.append(kwargs.get("consistent_read"))
            return real(*args, **kwargs)

        monkeypatch.setattr(defs.client, "query_pages", spy)
        assert "claims" in {d.id for d in defs.list(fresh=True)}
        assert seen == [True]

    def test_read_current_is_a_strongly_consistent_read(self, defs, monkeypatch):
        # moto is always consistent, so the request is what can be checked here: an
        # eventually consistent read could return the version a write just replaced.
        defs.put(QueueDefinition(id="claims"), create=True)
        seen = []
        real = defs.client.get_item

        def spy(key, **kwargs):
            seen.append(kwargs.get("consistent_read"))
            return real(key, **kwargs)

        monkeypatch.setattr(defs.client, "get_item", spy)
        defs.read_current("claims")
        assert seen == [True]

    def test_read_current_normalises_the_id(self, defs):
        defs.put(QueueDefinition(id="claims"), create=True)
        assert defs.read_current(" Claims ").id == "claims"
        assert defs.read_current("nope") is None

    def test_other_put_failures_propagate(self, defs, monkeypatch):
        boom = RuntimeError("throttled")

        def fail(*_a, **_k):
            raise boom

        monkeypatch.setattr(defs.client, "put_item", fail)
        monkeypatch.setattr(defs.client, "transact_write_items", fail)
        for kwargs in ({"create": True}, {"create": False, "expected_version": 1}):
            with pytest.raises(RuntimeError, match="throttled"):
                defs.put(QueueDefinition(id="claims"), **kwargs)
