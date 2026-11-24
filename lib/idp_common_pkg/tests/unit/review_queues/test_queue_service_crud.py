# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Queue administration in the review-queue service (moto HITLTable).

Creating, updating and listing the system-wide queue definitions is the
service's, behind an Admin check, so every caller of the service (the API
resolver, the SDK through it) gets the same rules.
"""

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from idp_common.review_queues.api_models import (
    CreateQueueArgs,
    ListQueuesArgs,
    UpdateQueueArgs,
)
from idp_common.review_queues.directory import QueueDirectory
from idp_common.review_queues.exceptions import (
    ReviewAccessDenied,
    ReviewConflict,
    ReviewQueueError,
    ReviewValidationError,
)
from idp_common.review_queues.models import CallerContext
from idp_common.review_queues.queue_defs import (
    DEFAULT_SKIP_REASONS,
    DEFAULT_TIME_LIMIT_MINUTES,
)
from idp_common.review_queues.service import ReviewQueueService
from idp_common.review_queues.store import ReviewStore

from ._table import REGION

pytestmark = pytest.mark.unit

ADMIN = CallerContext(
    reviewer_key="admin@example.com",
    email="admin@example.com",
    groups=frozenset({"Admin"}),
    is_admin=True,
)
REVIEWER = CallerContext(
    reviewer_key="rev@example.com",
    email="rev@example.com",
    groups=frozenset({"Reviewer"}),
)
AUTHOR = CallerContext(
    reviewer_key="author@example.com",
    email="author@example.com",
    groups=frozenset({"Author"}),
)


@pytest.fixture
def service(review_table):
    store = ReviewStore(review_table, region=REGION)
    return ReviewQueueService(
        store,
        QueueDirectory(store.queue_defs),
        notifier=MagicMock(),
        finalize_sender=MagicMock(),
        s3=MagicMock(),
        output_bucket="out",
    )


def _create(service, queue_id, **fields):
    return service.create_queue(ADMIN, CreateQueueArgs(id=queue_id, **fields))


class TestCreate:
    def test_a_queue_is_created_with_the_defaults_for_what_is_left_out(self, service):
        created = _create(service, "claims")

        assert (created.id, created.name, created.version) == ("claims", "Claims", 1)
        assert (created.priority, created.enabled) == (50, True)
        assert created.time_limit_minutes == DEFAULT_TIME_LIMIT_MINUTES
        assert created.skip_reasons == list(DEFAULT_SKIP_REASONS)
        assert service.store.queue_defs.read_current("claims") == created

    def test_the_fields_given_are_stored(self, service):
        created = service.create_queue(
            ADMIN,
            CreateQueueArgs.model_validate(
                {
                    "id": "High-Value",
                    "name": "High value",
                    "description": "Invoices over 10,000",
                    "priority": 80,
                    "checklist": ["Check the total"],
                    "rejectReasons": ["Illegible"],
                    "skipReasons": ["Not mine"],
                    "timeLimitMinutes": 120,
                    "eligibleAnnotators": ["Ann@Example.com"],
                    "enabled": False,
                }
            ),
        )

        assert created.id == "high-value"
        assert (created.name, created.description, created.priority) == (
            "High value",
            "Invoices over 10,000",
            80,
        )
        assert created.checklist == ["Check the total"]
        assert created.reject_reasons == ["Illegible"]
        assert created.skip_reasons == ["Not mine"]
        assert created.time_limit_minutes == 120
        assert created.eligible_annotators == ["ann@example.com"]
        assert created.enabled is False

    def test_a_duplicate_id_is_refused(self, service):
        _create(service, "claims")

        with pytest.raises(ReviewQueueError) as refused:
            _create(service, "claims")

        assert refused.value.code == "QUEUE_EXISTS"

    def test_a_value_out_of_range_is_a_validation_error(self, service):
        with pytest.raises(ValidationError):
            _create(service, "claims", priority=500)

    def test_a_field_the_api_does_not_have_is_refused(self):
        with pytest.raises(ValidationError):
            CreateQueueArgs.model_validate({"id": "claims", "leaseMinutes": 5})

    def test_a_threshold_from_an_older_client_is_accepted_and_ignored(self, service):
        created = service.create_queue(
            ADMIN,
            CreateQueueArgs.model_validate(
                {"id": "claims", "confidenceThreshold": 0.9}
            ),
        )

        assert not hasattr(created, "confidence_threshold")


class TestUpdate:
    def test_an_update_built_from_the_current_version_is_written(self, service):
        created = _create(service, "claims", priority=40)

        updated = service.update_queue(
            ADMIN, UpdateQueueArgs(id="claims", priority=90, version=created.version)
        )

        assert (updated.priority, updated.version) == (90, created.version + 1)

    def test_an_update_built_from_an_older_version_is_refused(self, service):
        created = _create(service, "claims")
        service.update_queue(ADMIN, UpdateQueueArgs(id="claims", priority=60))

        with pytest.raises(ReviewConflict) as refused:
            service.update_queue(
                ADMIN,
                UpdateQueueArgs(id="claims", priority=70, version=created.version),
            )

        assert refused.value.code == "QUEUE_CHANGED"
        assert service.store.queue_defs.read_current("claims").priority == 60

    def test_an_update_with_no_version_replaces_what_is_stored(self, service):
        _create(service, "claims", priority=40)

        updated = service.update_queue(ADMIN, UpdateQueueArgs(id="claims", priority=60))

        assert (updated.priority, updated.version) == (60, 2)

    def test_what_is_left_out_keeps_enabled_and_the_skip_reasons(self, service):
        _create(service, "claims", enabled=False, skip_reasons=["Not mine"])

        updated = service.update_queue(
            ADMIN, UpdateQueueArgs(id="claims", name="Claims 2")
        )

        assert updated.name == "Claims 2"
        assert updated.enabled is False
        assert updated.skip_reasons == ["Not mine"]

    def test_enabled_can_be_set_back(self, service):
        _create(service, "claims", enabled=False)

        updated = service.update_queue(
            ADMIN, UpdateQueueArgs(id="claims", enabled=True)
        )

        assert updated.enabled is True

    def test_a_queue_that_does_not_exist_is_refused(self, service):
        with pytest.raises(ReviewQueueError) as refused:
            service.update_queue(ADMIN, UpdateQueueArgs(id="nope", priority=60))

        assert refused.value.code == "UNKNOWN_QUEUE"

    @pytest.mark.parametrize("version", [True, -1, "2", 1.5])
    def test_a_version_that_is_not_a_whole_number_is_refused(self, version):
        with pytest.raises(ValidationError):
            UpdateQueueArgs.model_validate({"id": "claims", "version": version})


class TestList:
    def _five(self, service):
        for queue_id in ("echo", "alpha", "delta", "bravo"):
            _create(service, queue_id)
        # With the Default queue every stack has: five.
        return ["alpha", "bravo", "default", "delta", "echo"]

    def test_pages_of_two_over_five_queues(self, service):
        expected = self._five(service)

        seen, token, pages = [], None, 0
        while True:
            page, token = service.list_queues(
                ADMIN, ListQueuesArgs(limit=2, next_token=token)
            )
            seen.extend(d.id for d in page)
            pages += 1
            assert len(page) <= 2
            if token is None:
                break

        assert seen == expected
        assert pages == 3

    def test_one_page_holds_them_all_when_the_limit_allows(self, service):
        expected = self._five(service)

        page, token = service.list_queues(ADMIN, ListQueuesArgs())

        assert [d.id for d in page] == expected
        assert token is None

    def test_a_queue_created_between_pages_is_not_lost_or_repeated(self, service):
        self._five(service)
        first, token = service.list_queues(ADMIN, ListQueuesArgs(limit=2))
        _create(service, "aardvark")  # sorts before everything already returned
        _create(service, "charlie")  # sorts after it

        rest = []
        while token is not None:
            page, token = service.list_queues(
                ADMIN, ListQueuesArgs(limit=2, next_token=token)
            )
            rest.extend(d.id for d in page)

        assert [d.id for d in first] == ["alpha", "bravo"]
        assert rest == ["charlie", "default", "delta", "echo"]

    def test_the_list_carries_each_queues_current_version(self, service):
        _create(service, "claims")
        service.update_queue(ADMIN, UpdateQueueArgs(id="claims", priority=60))

        page, _ = service.list_queues(ADMIN, ListQueuesArgs())

        assert {d.id: d.version for d in page}["claims"] == 2

    @pytest.mark.parametrize("token", ["not-a-cursor", "e30", "eyJ2IjoxLCJrIjp7fX0"])
    def test_an_invalid_cursor_is_a_validation_error(self, service, token):
        with pytest.raises(ReviewValidationError) as refused:
            service.list_queues(ADMIN, ListQueuesArgs(next_token=token))

        assert refused.value.code == "BAD_CURSOR"


class TestAdminOnly:
    @pytest.mark.parametrize("caller", [REVIEWER, AUTHOR], ids=["reviewer", "author"])
    def test_create_is_refused(self, service, caller):
        with pytest.raises(ReviewAccessDenied) as refused:
            service.create_queue(caller, CreateQueueArgs(id="claims"))

        assert refused.value.code == "ROLE"
        assert service.store.queue_defs.read_current("claims") is None

    @pytest.mark.parametrize("caller", [REVIEWER, AUTHOR], ids=["reviewer", "author"])
    def test_update_is_refused(self, service, caller):
        _create(service, "claims", priority=40)

        with pytest.raises(ReviewAccessDenied) as refused:
            service.update_queue(caller, UpdateQueueArgs(id="claims", priority=90))

        assert refused.value.code == "ROLE"
        assert service.store.queue_defs.read_current("claims").priority == 40

    @pytest.mark.parametrize("caller", [REVIEWER, AUTHOR], ids=["reviewer", "author"])
    def test_list_is_refused(self, service, caller):
        with pytest.raises(ReviewAccessDenied) as refused:
            service.list_queues(caller, ListQueuesArgs())

        assert refused.value.code == "ROLE"
