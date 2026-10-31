# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Naming a queue in listReviewQueueItems / claimNextReviewItem arguments.

Queues are system-wide, so Review Management opens a queue by ``queueName`` alone.
Requiring ``queueProfile`` alongside it refused every queue page with BAD_ARGUMENT.
"""

import pytest
from pydantic import ValidationError

from idp_common.review_queues.api_models import ClaimNextArgs, ListQueueItemsArgs


@pytest.mark.unit
class TestQueueArgs:
    def test_a_queue_is_named_by_queue_name_alone(self):
        args = ListQueueItemsArgs.model_validate(
            {"queueName": "Default", "claimable": False, "limit": 50}
        )
        assert args.queue_name == "Default"
        assert args.queue_profile is None

    def test_no_queue_means_every_queue(self):
        args = ClaimNextArgs.model_validate({})
        assert args.queue_name is None

    def test_a_profile_is_still_accepted_for_older_callers(self):
        args = ClaimNextArgs.model_validate(
            {"queueProfile": "invoices", "queueName": "Default"}
        )
        assert args.queue_name == "Default"

    def test_unknown_arguments_are_still_refused(self):
        with pytest.raises(ValidationError):
            ListQueueItemsArgs.model_validate({"queueName": "Default", "bogus": 1})
