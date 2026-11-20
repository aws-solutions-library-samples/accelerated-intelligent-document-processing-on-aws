# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A transient classification failure carries the attempt's completed usage (#2122).

When a call in classification's pools or its packet window loop fails transiently,
the service raises ``TransientError`` so that ``ClassificationStep`` retries the step
(#2034). The calls the attempt had already completed were billed. They now travel
on the raised error (``utils.failure_metering``), so the classification handler can
record that spend before the retry instead of dropping it.

The pools run on one worker here, with the failure on the last call, so every
other call has completed when it fails and the expected figures are fixed rather
than a matter of scheduling (a pool cancels the calls it has not started).
"""

from __future__ import annotations

import pytest

from idp_common.classification.service import ClassificationService
from idp_common.utils import attach_failure_metering, failure_metering
from idp_common.utils.transient_errors import TransientError
from tests.unit.classification.test_split_then_classify import _document, _service
from tests.unit.classification.test_split_then_classify_transient import (
    _Calls,
    _run,
    _throttle,
    _windowed,
)

pytestmark = pytest.mark.unit

SPLITTING = ClassificationService.STAGE_SPLITTING
SECTION_CLASSIFICATION = ClassificationService.STAGE_SECTION_CLASSIFICATION
CLASSIFICATION = ClassificationService.STAGE_CLASSIFICATION

#: Input tokens the fake model reports for each call it answers (``_reply``).
TOKENS_PER_CALL = 10


def _one_worker(**kw):
    service = _service(**kw)
    service.max_workers = 1
    return service


def _tokens(metering, context: str) -> float:
    return float(metering.get(f"{context}/bedrock/nova", {}).get("inputTokens", 0))


class TestSplitThenClassify:
    def test_a_throttled_segment_carries_the_splitting_and_labelling_usage(self):
        calls = _Calls({(SECTION_CLASSIFICATION, "3"): _throttle()})

        with pytest.raises(TransientError) as caught:
            _run(_one_worker(granularity="packet"), _document(3), calls)

        metering = failure_metering(caught.value)
        assert calls.count(SPLITTING) == 1
        assert calls.count(SECTION_CLASSIFICATION) == 3
        assert _tokens(metering, "Splitting") == TOKENS_PER_CALL
        assert _tokens(metering, "SectionClassification") == TOKENS_PER_CALL * 2

    def test_usage_the_failed_call_itself_carried_is_kept(self):
        """A call can fail after a billed reply (a stream cut off mid-answer), and
        then carries that usage on its exception."""
        failed = attach_failure_metering(
            _throttle(), {"SectionClassification/bedrock/nova": {"inputTokens": 4}}
        )
        calls = _Calls({(SECTION_CLASSIFICATION, "3"): failed})

        with pytest.raises(TransientError) as caught:
            _run(_one_worker(granularity="packet"), _document(3), calls)

        assert _tokens(
            failure_metering(caught.value), "SectionClassification"
        ) == TOKENS_PER_CALL * 2 + 4

    def test_a_throttled_page_boundary_call_carries_the_other_pages_usage(self):
        calls = _Calls({(SPLITTING, "3"): _throttle()})

        with pytest.raises(TransientError) as caught:
            _run(
                _one_worker(granularity="page"), _document(3), calls, page_texts=True
            )

        metering = failure_metering(caught.value)
        assert calls.count(SPLITTING) == 3
        assert _tokens(metering, "Splitting") == TOKENS_PER_CALL * 2
        assert "SectionClassification/bedrock/nova" not in metering

    def test_a_throttled_first_window_carries_nothing(self):
        """No call had completed, so there is nothing to record."""
        calls = _Calls({(SPLITTING, "1"): _throttle()})

        with pytest.raises(TransientError) as caught:
            _run(_windowed(), _document(7), calls)

        assert failure_metering(caught.value) == {}


class TestCombinedPacketWindows:
    def test_a_throttled_last_window_carries_the_earlier_windows_usage(self):
        # Windows over 7 pages: [1-3], [3-5], [5-7]. The third throttles.
        calls = _Calls({(CLASSIFICATION, "5"): _throttle()})

        with pytest.raises(TransientError) as caught:
            _run(_windowed(strategy="combined"), _document(7), calls)

        assert calls.count(CLASSIFICATION) == 3
        assert _tokens(failure_metering(caught.value), "Classification") == (
            TOKENS_PER_CALL * 2
        )
