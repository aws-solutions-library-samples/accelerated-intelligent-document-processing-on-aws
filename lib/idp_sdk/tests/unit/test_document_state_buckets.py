# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for the `DocumentState` -> progress-bucket partition in `idp_sdk.models.base`.

Every document the SDK reports on is shown under exactly one of four buckets —
completed, running, queued, failed — and both consumers (the SDK's own
`ProgressMonitor._categorize_document` and `idp_cli`'s
`_batch_status_to_display_dicts`) used to decide that with an `if/elif` chain ending
in a default. A default makes the mapping *total* without making it *complete*: a
member nobody classified still gets an answer, and it is whichever bucket the author
of the `else` happened to pick. Both possible versions of that mistake have shipped
here. The CLI's chain named eleven members and sent the rest to `queued`, so the
terminal `ABORTED` reported "IN PROGRESS" and exited 2 forever and `PREPROCESSING` —
set for *every* document whenever a preprocessing hook is registered — read as
Queued for a whole run; the monitor's chain defaulted to `running`, which would
absorb a newly added *terminal* state into "still working".

So the property these tests hold is the **rule**, not a list of the states that were
wrong at the time of writing:

* the four bucket sets partition `set(DocumentState)` — every member in exactly one,
  nothing bucketed that is not a member, no bucket empty;
* `classify_document_state` answers for every member, driven by a parametrisation
  derived from the enum rather than typed out, so a member added tomorrow is asked
  about without anybody editing this file;
* a *hypothetical* new member fails loudly. That is the one claim a list of today's
  members cannot support, and it is measured by driving the real fault detector with
  a state set containing a member this codebase does not define — the only way to
  observe what happens to a future addition, since an `Enum` cannot be extended.
"""

from __future__ import annotations

import pytest

from idp_sdk.models.base import (
    _DOCUMENT_STATE_BUCKETS,
    FAILED_DOCUMENT_STATES,
    IN_FLIGHT_DOCUMENT_STATES,
    NOT_STARTED_DOCUMENT_STATES,
    SUCCESS_DOCUMENT_STATES,
    TERMINAL_DOCUMENT_STATES,
    DocumentBucket,
    DocumentState,
    classify_document_state,
    document_state_partition_faults,
)

#: Derived from the enum, never typed out: a member added to `DocumentState`
#: joins this parametrisation automatically, which is the whole point.
ALL_STATES = sorted(DocumentState, key=lambda s: s.value)


def test_the_enum_is_not_empty_so_the_parametrised_tests_below_are_not_vacuous():
    """A derived parametrisation that comes out empty reports as a skip, not a fail.

    `ALL_STATES` drives the two `parametrize`d tests below. If `DocumentState` were
    ever emptied or renamed, those would collect zero cases and pytest would report
    them as nothing at all — a green run over an unasserted rule. This asserts the
    derivation produced something, and a floor well under the current count so it
    does not have to be edited every time a state is added.
    """
    assert len(ALL_STATES) >= 20


class TestThePartition:
    """The four bucket sets divide `DocumentState` with no gap and no overlap."""

    def test_the_four_buckets_partition_the_enum(self):
        """Union equals the enum, and nothing is in two buckets.

        This is the assertion a new `DocumentState` member fails on: it lands in no
        bucket, `document_state_partition_faults` reports it by name, and this test
        goes red offline before the member reaches a user's progress display.
        """
        assert (
            document_state_partition_faults(set(DocumentState), _DOCUMENT_STATE_BUCKETS)
            == []
        )

    def test_every_bucket_is_populated(self):
        """An empty bucket is a bucket nothing can ever be reported under."""
        for bucket, members in _DOCUMENT_STATE_BUCKETS.items():
            assert members, f"{bucket} holds no states"

    def test_all_four_bucket_names_are_covered(self):
        """Each `DocumentBucket` has a set, so no bucket is unreachable."""
        assert set(_DOCUMENT_STATE_BUCKETS) == set(DocumentBucket)

    def test_terminal_states_are_derived_from_the_two_sets_they_are_the_union_of(self):
        """`TERMINAL_DOCUMENT_STATES` cannot drift from its parts."""
        assert (
            TERMINAL_DOCUMENT_STATES == SUCCESS_DOCUMENT_STATES | FAILED_DOCUMENT_STATES
        )
        assert not TERMINAL_DOCUMENT_STATES & IN_FLIGHT_DOCUMENT_STATES
        assert not TERMINAL_DOCUMENT_STATES & NOT_STARTED_DOCUMENT_STATES


class TestAHypotheticalNewMember:
    """What happens to a `DocumentState` nobody has classified yet.

    `Enum` subclasses are closed, so a real future member cannot be constructed.
    The fault detector is a pure function over a state set and a bucket mapping,
    though, so handing it a set containing one extra name measures exactly the
    situation these tests exist to prevent — and measures it by execution rather
    than by arguing that the rule implies it.
    """

    def test_an_unclassified_member_is_reported_by_name(self):
        faults = document_state_partition_faults(
            set(DocumentState) | {"SOME_FUTURE_STATE"}, _DOCUMENT_STATE_BUCKETS
        )

        assert faults, "a member in no bucket must be a fault"
        assert any("SOME_FUTURE_STATE" in fault for fault in faults), faults

    def test_a_member_in_two_buckets_is_reported(self):
        overlapping = dict(_DOCUMENT_STATE_BUCKETS)
        overlapping[DocumentBucket.QUEUED] = NOT_STARTED_DOCUMENT_STATES | {
            DocumentState.COMPLETED
        }

        faults = document_state_partition_faults(set(DocumentState), overlapping)

        assert any("COMPLETED" in fault for fault in faults), faults

    def test_a_bucketed_name_that_is_not_a_member_is_reported(self):
        stale = dict(_DOCUMENT_STATE_BUCKETS)
        stale[DocumentBucket.FAILED] = FAILED_DOCUMENT_STATES | {"DELETED_STATE"}

        faults = document_state_partition_faults(set(DocumentState), stale)

        assert any("DELETED_STATE" in fault for fault in faults), faults

    def test_an_empty_bucket_is_reported(self):
        emptied = dict(_DOCUMENT_STATE_BUCKETS)
        emptied[DocumentBucket.RUNNING] = frozenset()

        faults = document_state_partition_faults(
            set(DocumentState) - IN_FLIGHT_DOCUMENT_STATES, emptied
        )

        assert any("RUNNING is empty" in fault for fault in faults), faults


class TestClassifyDocumentState:
    """`classify_document_state` is total over the enum and over its string values."""

    @pytest.mark.parametrize("state", ALL_STATES, ids=lambda s: s.value)
    def test_every_member_is_classified(self, state):
        assert classify_document_state(state) in set(DocumentBucket)

    @pytest.mark.parametrize("state", ALL_STATES, ids=lambda s: s.value)
    def test_the_string_value_classifies_the_same_way_as_the_member(self, state):
        """The monitor passes a raw string from the tracking table, not a member."""
        assert classify_document_state(state.value) == classify_document_state(state)

    def test_the_terminal_states_are_the_ones_that_stop_a_wait(self):
        """`status --wait` terminates iff every document is completed or failed.

        `ABORTED` and `REDACTED_SUPERSEDED` are the two that used to read as queued,
        which is why a batch containing either never terminated on its own.
        """
        for state in TERMINAL_DOCUMENT_STATES:
            assert classify_document_state(state) in (
                DocumentBucket.COMPLETED,
                DocumentBucket.FAILED,
            ), state

    def test_a_redacted_original_counts_as_done_rather_than_failed(self):
        """The redacted copy is the requested outcome, not a processing failure."""
        assert (
            classify_document_state(DocumentState.REDACTED_SUPERSEDED)
            is DocumentBucket.COMPLETED
        )

    def test_an_aborted_document_counts_as_failed_rather_than_queued(self):
        """Terminal and unsuccessful, so `status` exits 1 rather than 2 forever."""
        assert classify_document_state(DocumentState.ABORTED) is DocumentBucket.FAILED

    def test_preprocessing_counts_as_running_rather_than_queued(self):
        """Set for every document when a preprocessing hook is registered.

        Under the old chain this one state inverted the whole progress display on a
        PII-anonymization stack: the running count read 0 while every document was
        being worked on.
        """
        assert (
            classify_document_state(DocumentState.PREPROCESSING)
            is DocumentBucket.RUNNING
        )

    def test_a_status_string_the_enum_does_not_define_reads_as_in_flight(self):
        """A stack running a newer pipeline than this SDK.

        Terminal statuses are a small closed set, so an unrecognised one is
        overwhelmingly a mid-pipeline stage; calling it queued is the mistake that
        made `ABORTED` report "IN PROGRESS" forever.
        """
        assert classify_document_state("SOME_NEW_STAGE") is DocumentBucket.RUNNING

    @pytest.mark.parametrize("empty", ["", None])
    def test_an_absent_status_reads_as_queued(self, empty):
        """Falsy means the lookup produced nothing, which is what `UNKNOWN` is for."""
        assert classify_document_state(empty) is DocumentBucket.QUEUED
        assert classify_document_state(empty) == classify_document_state(
            DocumentState.UNKNOWN
        )
