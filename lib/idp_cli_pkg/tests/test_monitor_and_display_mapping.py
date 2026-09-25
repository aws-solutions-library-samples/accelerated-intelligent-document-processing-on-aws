# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for the two pieces of `idp_cli.cli` that turn SDK batch status into screen output.

`_batch_status_to_display_dicts` is a pure translation layer. It takes the
`BatchStatus` pydantic model the SDK returns and produces the two plain dicts every
function in `idp_cli/display.py` consumes: `status_data`, which buckets each document
into `completed` / `running` / `queued` / `failed`, and `stats`, which carries the
counts and the three derived numbers (completion percentage, success rate, average
duration). Nothing validates the result, so a document put in the wrong bucket or a
percentage computed from the wrong denominator is silently wrong output rather than an
error — which is why these tests assert every field the function produces for a
document in each state, rather than checking that the keys exist.

`_monitor_progress` is the polling loop behind `process --monitor`,
`reprocess --monitor` and `status --wait`. It asks the SDK for batch status, renders a
Rich `Live` layout, sleeps, and repeats until the batch reports itself complete. The
tests here drive it with a scripted status sequence and a fake clock, so no test
sleeps and no test depends on wall-clock timing. The interesting questions are when it
stops polling, whether it rendered the states it passed through, and what it does with
the two interruptions it catches by name: `KeyboardInterrupt` (the documented way to
stop watching) and any other exception (a monitoring failure).

Two shaping notes. `_monitor_progress` starts with
`isinstance(client, idp_sdk.IDPClient)` and builds its own client if that is False, so
these tests pass a **real** `IDPClient` — constructing one makes no AWS call — and
replace only `client.batch.get_status`. Patching `idp_sdk.IDPClient` with a `MagicMock`
instead would send every test down the legacy branch, because `isinstance` against a
mock instance raises. And `idp_cli/display.py` has its own module-level Rich console
that the package-wide `unstyled_cli_console` fixture does not reach, so the
`pinned_console` fixture below pins it the same way and points `cli.console` at the
same object; without it the monitoring output is ellipsized at 80 columns and the
assertions here become width-dependent.
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from rich.console import Console

from idp_cli import cli as cli_module
from idp_cli import display as display_module
from idp_sdk import IDPClient
from idp_sdk.models import BatchStatus, DocumentBucket, classify_document_state
from idp_sdk.models.base import IN_FLIGHT_DOCUMENT_STATES, DocumentState
from idp_sdk.models.document import DocumentStatus

#: Every state in flight, derived from the shared authority in
#: `idp_sdk.models.base` rather than listed here. A list here is what let the
#: mapper's own list omit `OCR`, `PREPROCESSING`, `POSTPROCESSING` and
#: `RULE_VALIDATION_POLICY_CLASSIFICATION` while looking complete.
RUNNING_STATES = tuple(sorted(s.value for s in IN_FLIGHT_DOCUMENT_STATES))

#: Every member of the enum, so the coverage test below cannot fall behind it.
ALL_STATES = tuple(sorted(s.value for s in DocumentState))


@pytest.fixture(autouse=True)
def pinned_console(monkeypatch):
    """Point `cli.console` and `display.console` at one 200-column console, and yield it.

    `idp_cli/display.py` builds its own `Console()` at import, so the package-wide
    `unstyled_cli_console` fixture does not cover it. Monitoring output is split
    across the two: the header, the final summary and the monitoring instructions are
    printed by `display.py`, while "Monitoring stopped" and "Monitoring error" are
    printed by `cli.py`. Sharing one console means a test can capture the whole
    session in the order it was written, which is what the interruption tests assert
    on.
    """
    console = Console(width=200, force_terminal=False)
    monkeypatch.setattr(display_module, "console", console)
    monkeypatch.setattr(cli_module, "console", console)
    return console


class Clock:
    """A fake `time` module: `sleep` advances the clock instead of waiting.

    `_monitor_progress` reads elapsed time from `time.time()` and waits with
    `time.sleep()`, both resolved through `idp_cli.cli`'s module global. Substituting
    the whole module makes the loop's timing deterministic and keeps the test at zero
    wall clock, which matters because the loop's grace period is 60 seconds long.
    """

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def doc(
    document_id: str = "batch-1/doc.pdf",
    status: str = "COMPLETED",
    *,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    duration_seconds: Optional[float] = None,
    num_pages: Optional[int] = None,
    num_sections: Optional[int] = None,
    error: Optional[str] = None,
) -> DocumentStatus:
    return DocumentStatus(
        document_id=document_id,
        status=status,  # type: ignore[arg-type]
        start_time=start_time,
        end_time=end_time,
        duration_seconds=duration_seconds,
        num_pages=num_pages,
        num_sections=num_sections,
        error=error,
    )


def batch(
    documents: list[DocumentStatus],
    *,
    total: Optional[int] = None,
    success_rate: float = 1.0,
    all_complete: bool = False,
    batch_id: str = "batch-1",
) -> BatchStatus:
    """A `BatchStatus` whose own counters are consistent with `documents` by default.

    `total` is overridable because the mapper uses `batch_status.total` as the
    denominator for the completion percentage rather than counting the documents it
    was given, and that difference is worth testing.
    """
    return BatchStatus(
        batch_id=batch_id,
        documents=documents,
        total=len(documents) if total is None else total,
        completed=sum(1 for d in documents if d.status == "COMPLETED"),
        failed=sum(1 for d in documents if d.status == "FAILED"),
        in_progress=0,
        queued=0,
        success_rate=success_rate,
        all_complete=all_complete,
    )


def monitored_client(statuses: list[BatchStatus]) -> tuple[IDPClient, list[str]]:
    """A real `IDPClient` whose `batch.get_status` replays `statuses`.

    Real so that `_monitor_progress`'s `isinstance` check takes the modern branch.
    The last entry repeats if the loop asks for more, so a test that miscounts polls
    fails on the poll count rather than on an `IndexError`.
    """
    client = IDPClient(stack_name="my-stack", region="us-east-1")
    asked: list[str] = []

    def get_status(batch_id: str) -> BatchStatus:
        asked.append(batch_id)
        return statuses[min(len(asked) - 1, len(statuses) - 1)]

    client.batch.get_status = get_status  # type: ignore[method-assign]
    return client, asked


# ---------------------------------------------------------------------------
# _batch_status_to_display_dicts
# ---------------------------------------------------------------------------


class TestDisplayDictMapping:
    def test_a_completed_document_maps_every_field_display_reads(self):
        """All eight per-document keys, for the ordinary case.

        `display.py` reads `document_id`, `status`, `start_time`, `end_time`,
        `duration`, `num_pages`, `num_sections` and `error` off these dicts. Note the
        key is `duration`, not the model's `duration_seconds`: a rename here shows up
        as a `KeyError` deep in a Rich table, so the exact key set is the contract.
        """
        started = datetime(2025, 1, 2, 3, 4, 5)
        ended = datetime(2025, 1, 2, 3, 4, 35)
        status_data, stats = cli_module._batch_status_to_display_dicts(
            batch(
                [
                    doc(
                        "batch-1/invoice.pdf",
                        "COMPLETED",
                        start_time=started,
                        end_time=ended,
                        duration_seconds=30.0,
                        num_pages=4,
                        num_sections=2,
                    )
                ],
                all_complete=True,
            )
        )

        assert status_data["completed"] == [
            {
                "document_id": "batch-1/invoice.pdf",
                "status": "COMPLETED",
                # ISO 8601 strings, never `datetime` -- see
                # `test_the_timestamps_are_strings_whether_present_or_absent`.
                "start_time": started.isoformat(),
                "end_time": ended.isoformat(),
                "duration": 30.0,
                "num_pages": 4,
                "num_sections": 2,
                "error": "",
            }
        ]
        assert status_data["running"] == []
        assert status_data["queued"] == []
        assert status_data["failed"] == []
        assert status_data["total"] == 1
        assert stats == {
            "total": 1,
            "completed": 1,
            "failed": 0,
            "running": 0,
            "queued": 0,
            "all_complete": True,
            "success_rate": 100.0,
            "completion_percentage": 100.0,
            "avg_duration_seconds": 30.0,
        }

    def test_a_failed_document_carries_its_error_into_the_failed_bucket(self):
        status_data, stats = cli_module._batch_status_to_display_dicts(
            batch(
                [doc("batch-1/bad.pdf", "FAILED", error="Textract threw")],
                all_complete=True,
                success_rate=0.0,
            )
        )

        assert [d["document_id"] for d in status_data["failed"]] == ["batch-1/bad.pdf"]
        assert status_data["failed"][0]["error"] == "Textract threw"
        assert status_data["completed"] == []
        assert stats["failed"] == 1
        assert stats["success_rate"] == 0.0
        # A failure counts as finished for the completion percentage.
        assert stats["completion_percentage"] == 100.0

    def test_missing_optional_fields_become_empty_strings_and_zero(self):
        """A document the tracking table has barely written yet.

        `None` reaching a Rich `f"{duration:.1f}s"` would raise, so the mapper
        substitutes: `""` for the three text fields and `0` for the duration.
        `num_pages` and `num_sections` are left as `None` — display treats them as
        optional and never formats them numerically.
        """
        status_data, _ = cli_module._batch_status_to_display_dicts(
            batch([doc("batch-1/new.pdf", "QUEUED")])
        )

        (entry,) = status_data["queued"]
        assert entry["start_time"] == ""
        assert entry["end_time"] == ""
        assert entry["error"] == ""
        assert entry["duration"] == 0
        assert entry["num_pages"] is None
        assert entry["num_sections"] is None

    @pytest.mark.parametrize("state", RUNNING_STATES)
    def test_each_in_flight_state_the_mapper_names_goes_to_running(self, state):
        status_data, stats = cli_module._batch_status_to_display_dicts(
            batch([doc("batch-1/d.pdf", state)])
        )

        assert [d["document_id"] for d in status_data["running"]] == ["batch-1/d.pdf"]
        assert stats["running"] == 1
        assert stats["queued"] == 0

    def test_queued_documents_go_to_queued(self):
        status_data, stats = cli_module._batch_status_to_display_dicts(
            batch([doc("batch-1/a.pdf", "QUEUED"), doc("batch-1/b.pdf", "QUEUED")])
        )

        assert stats["queued"] == 2
        assert stats["running"] == 0
        assert [d["document_id"] for d in status_data["queued"]] == [
            "batch-1/a.pdf",
            "batch-1/b.pdf",
        ]

    def test_the_state_list_this_file_derives_is_not_empty(self):
        """Guards the two parametrisations above and below from collecting nothing.

        Both are derived from `DocumentState`, and a derived list that comes out
        empty makes a `parametrize`d test report as zero cases — which reads as a
        green run over an unasserted rule rather than as a failure.
        """
        assert len(ALL_STATES) >= 20
        assert len(RUNNING_STATES) >= 10

    @pytest.mark.parametrize("state", ALL_STATES)
    def test_every_state_lands_in_the_bucket_the_shared_authority_names(self, state):
        """No `DocumentState` reaches a fallback, and none is lost or duplicated.

        The mapper used to be an `if COMPLETED / elif FAILED / elif <nine in-flight
        names> / else queued` chain, so the twelve members it did not name fell
        through to `queued`: `PREPROCESSING`, `OCR`, `STARTED`, `IN_PROGRESS`,
        `POSTPROCESSING` and `RULE_VALIDATION_POLICY_CLASSIFICATION` were shown under
        "Queued" while being actively worked on, and the terminal `ABORTED`,
        `REDACTED_SUPERSEDED` and `NOT_FOUND` were shown there too so a stopped batch
        reported "IN PROGRESS" and exited 2 forever.

        This is parametrised over the enum rather than over a list of the states that
        were wrong at the time, so a state added later is covered without an edit
        here. The bucket *contents* are pinned in
        `lib/idp_sdk/tests/unit/test_document_state_buckets.py`; what this asserts is
        that the mapper routes to them and puts each document in exactly one.
        """
        expected = classify_document_state(state)

        status_data, stats = cli_module._batch_status_to_display_dicts(
            batch([doc("batch-1/d.pdf", state)], all_complete=False)
        )

        assert [d["document_id"] for d in status_data[expected.value]] == [
            "batch-1/d.pdf"
        ]
        assert stats[expected.value] == 1
        # Exactly one bucket, so nothing is counted twice or dropped.
        assert sum(stats[b.value] for b in DocumentBucket) == 1

    def test_a_document_being_preprocessed_is_running_not_queued(self):
        """`PREPROCESSING` is set for *every* document when a hook is registered.

        On a PII-anonymization stack that made the running count read 0 for the whole
        run while every document was in fact being processed — the progress display
        inverted, not merely imprecise.
        """
        _, stats = cli_module._batch_status_to_display_dicts(
            batch([doc("batch-1/d.pdf", "PREPROCESSING")], all_complete=False)
        )

        assert stats["running"] == 1
        assert stats["queued"] == 0

    def test_an_aborted_document_makes_a_finished_batch_report_its_failure(
        self, pinned_console
    ):
        """The headline defect: ALL COMPLETED and exit 0 for a batch that aborted.

        The SDK treats `ABORTED` as terminal, so `all_complete` is True; the mapper
        put it in `queued`, so `stats["failed"]` was 0 and
        `display.show_final_status_summary` printed "ALL COMPLETED" and returned 0
        for a batch that had aborted half its work. Asserted through the real display
        function, so the exit code a caller acts on is what is pinned.
        """
        status_data, stats = cli_module._batch_status_to_display_dicts(
            batch(
                [
                    doc("batch-1/ok.pdf", "COMPLETED", duration_seconds=5.0),
                    doc("batch-1/gone.pdf", "ABORTED"),
                ],
                all_complete=True,
                success_rate=0.5,
            )
        )

        assert stats["failed"] == 1
        assert stats["queued"] == 0

        with pinned_console.capture() as captured:
            exit_code = display_module.show_final_status_summary(status_data, stats)

        assert exit_code == 1
        assert "COMPLETED WITH FAILURES (1 failed)" in captured.get()

    def test_a_single_aborted_document_stops_a_wait_instead_of_exiting_2_forever(self):
        """`status --wait` on an aborted document used to never terminate."""
        status_data, stats = cli_module._batch_status_to_display_dicts(
            batch([doc("batch-1/gone.pdf", "ABORTED")], all_complete=True)
        )

        assert display_module.show_final_status_summary(status_data, stats) == 1

    def test_the_timestamps_are_strings_whether_present_or_absent(self):
        """One type under one key, so the display layer can sort and encode it.

        `doc.end_time or ""` used to leave a `datetime` in place and substitute a
        `str` when the field was absent, which put two types under the same key and
        broke both of the things `display.py` does with it. The mapper now renders
        ISO 8601 in both cases -- lexicographic order over which is chronological
        order, so the sort still means what it was written to mean.
        """
        status_data, _ = cli_module._batch_status_to_display_dicts(
            batch(
                [
                    doc(
                        "batch-1/with.pdf",
                        "COMPLETED",
                        start_time=datetime(2025, 1, 2, 3, 4, 0),
                        end_time=datetime(2025, 1, 2, 3, 4, 5),
                        duration_seconds=5.0,
                    ),
                    doc("batch-1/without.pdf", "COMPLETED", duration_seconds=5.0),
                ],
                all_complete=True,
            )
        )

        entries = status_data["completed"]
        assert [d["end_time"] for d in entries] == ["2025-01-02T03:04:05", ""]
        assert [d["start_time"] for d in entries] == ["2025-01-02T03:04:00", ""]
        for entry in entries:
            assert isinstance(entry["end_time"], str)
            assert isinstance(entry["start_time"], str)

    def test_a_batch_mixing_a_timed_and_an_untimed_completion_renders_its_table(self):
        """The crash a user saw: `status` exited 1 with a comparison TypeError.

        `create_recent_completions_table` sorts the completed documents by
        `end_time`, and Python will not order a `datetime` against the `""`
        substituted for an absent one. Both callers catch broadly, so the observable
        result was `✗ Error: '<' not supported between instances of
        'datetime.datetime' and 'str'` and exit 1 instead of the table, and
        `--monitor` printing "Monitoring error:" and abandoning the watch. Driven
        through the real display function, and the timed document must sort first.
        """
        status_data, _ = cli_module._batch_status_to_display_dicts(
            batch(
                [
                    doc("batch-1/without.pdf", "COMPLETED", duration_seconds=5.0),
                    doc(
                        "batch-1/with.pdf",
                        "COMPLETED",
                        end_time=datetime(2025, 1, 2, 3, 4, 5),
                        duration_seconds=5.0,
                    ),
                ],
                all_complete=True,
            )
        )

        table = display_module.create_recent_completions_table(status_data)
        rendered = [cell for cell in table.columns[0]._cells]

        assert rendered == ["batch-1/with.pdf", "batch-1/without.pdf"]

    def test_a_completed_document_with_an_end_time_can_be_encoded_as_json(self):
        """`status --format json` raised rather than printing.

        The single-document branch of `format_status_json` puts `end_time` straight
        into `json.dumps`, which has no encoder for `datetime`: the same root cause
        as the sort crash, reached by a different command, and the reason the fix
        belongs in the mapper rather than at the sort.
        """
        status_data, stats = cli_module._batch_status_to_display_dicts(
            batch(
                [
                    doc(
                        "batch-1/with.pdf",
                        "COMPLETED",
                        end_time=datetime(2025, 1, 2, 3, 4, 5),
                        duration_seconds=5.0,
                    )
                ],
                all_complete=True,
            )
        )

        payload = json.loads(display_module.format_status_json(status_data, stats))

        assert payload["end_time"] == "2025-01-02T03:04:05"
        assert payload["exit_code"] == 0

    def test_the_completions_table_cannot_be_crashed_by_a_mixed_key(self):
        """The sort key is total over whatever a caller put under `end_time`.

        The mapper is the only producer of `status_data` in this repository, so this
        asserts the display function's own robustness rather than a path a user can
        currently reach -- a hand-built `status_data`, which is what the legacy
        callers of these display helpers pass.
        """
        status_data = {
            "completed": [
                {
                    "document_id": "a.pdf",
                    "end_time": datetime(2025, 1, 1),
                    "duration": 1.0,
                },
                {"document_id": "b.pdf", "end_time": "", "duration": 1.0},
            ]
        }

        table = display_module.create_recent_completions_table(status_data)

        assert [cell for cell in table.columns[0]._cells] == ["a.pdf", "b.pdf"]

    def test_average_duration_counts_only_completed_documents(self):
        """A failed document's duration is excluded, so the average is of successes.

        60 and 20 over two completed documents is 40; the failed document's 900
        seconds must not enter the sum or the count.
        """
        _, stats = cli_module._batch_status_to_display_dicts(
            batch(
                [
                    doc("a.pdf", "COMPLETED", duration_seconds=60.0),
                    doc("b.pdf", "COMPLETED", duration_seconds=20.0),
                    doc("c.pdf", "FAILED", duration_seconds=900.0, error="timeout"),
                    doc("d.pdf", "RUNNING", duration_seconds=5.0),
                ],
                all_complete=True,
            )
        )

        assert stats["avg_duration_seconds"] == 40.0

    def test_a_zero_duration_is_reported_but_left_out_of_the_average(self):
        """`if doc.duration_seconds:` is a truthiness test, so 0.0 is skipped.

        The document's own `duration` is still reported as 0, but it contributes to
        neither the sum nor the divisor. With one 10-second document and one
        0-second document the average is therefore 10.0, not 5.0. Harmless as
        arithmetic — a sub-second document rounds to "0.0s" anyway — but it is the
        behaviour, and a future change to `is not None` would move this number.
        """
        status_data, stats = cli_module._batch_status_to_display_dicts(
            batch(
                [
                    doc("a.pdf", "COMPLETED", duration_seconds=10.0),
                    doc("b.pdf", "COMPLETED", duration_seconds=0.0),
                ],
                all_complete=True,
            )
        )

        assert [d["duration"] for d in status_data["completed"]] == [10.0, 0]
        assert stats["avg_duration_seconds"] == 10.0

    def test_average_duration_is_zero_when_nothing_reports_one(self):
        """The divisor is zero here, and the function must not raise."""
        _, stats = cli_module._batch_status_to_display_dicts(
            batch([doc("a.pdf", "COMPLETED"), doc("b.pdf", "QUEUED")])
        )

        assert stats["avg_duration_seconds"] == 0.0

    def test_completion_percentage_is_taken_against_the_batch_total(self):
        """The denominator is `BatchStatus.total`, not the number of documents given.

        The SDK reports the batch's size separately from the per-document list, so a
        batch of 10 whose status call returned 2 documents is 20% complete rather
        than 100%. Counting the list instead would show a partially-populated
        tracking table as a finished batch.
        """
        _, stats = cli_module._batch_status_to_display_dicts(
            batch(
                [doc("a.pdf", "COMPLETED"), doc("b.pdf", "FAILED", error="x")],
                total=10,
            )
        )

        assert stats["total"] == 10
        assert stats["completion_percentage"] == 20.0

    def test_an_empty_batch_yields_zeroes_rather_than_a_division_error(self):
        _, stats = cli_module._batch_status_to_display_dicts(
            batch([], total=0, all_complete=True)
        )

        assert stats["completion_percentage"] == 0.0
        assert stats["avg_duration_seconds"] == 0.0
        assert stats["total"] == 0

    @pytest.mark.parametrize(
        ("sdk_fraction", "displayed"), [(0.0, 0.0), (0.5, 50.0), (1.0, 100.0)]
    )
    def test_success_rate_is_rescaled_from_a_fraction_to_a_percentage(
        self, sdk_fraction, displayed
    ):
        """The SDK reports 0.0-1.0 and `display.py` formats `{:.1f}%`.

        Dropping the multiplication would render a fully successful batch as
        "1.0%", which reads as an almost total failure.
        """
        _, stats = cli_module._batch_status_to_display_dicts(
            batch([doc("a.pdf", "COMPLETED")], success_rate=sdk_fraction)
        )

        assert stats["success_rate"] == displayed

    def test_all_complete_is_passed_through_from_the_sdk(self):
        """The loop's exit condition is this flag, not a count the mapper derives."""
        for flag in (True, False):
            _, stats = cli_module._batch_status_to_display_dicts(
                batch([doc("a.pdf", "QUEUED")], all_complete=flag)
            )
            assert stats["all_complete"] is flag


# ---------------------------------------------------------------------------
# _monitor_progress
# ---------------------------------------------------------------------------


class TestMonitorProgress:
    def test_it_polls_until_the_batch_reports_itself_complete(self, monkeypatch):
        """in-progress → in-progress → complete stops after the third poll.

        Three polls and two sleeps: the loop sleeps *after* each non-final poll, so a
        fourth poll would mean it did not notice the terminal state, and a third
        sleep would mean it waited once more before leaving.
        """
        clock = Clock()
        monkeypatch.setattr(cli_module, "time", clock)
        client, asked = monitored_client(
            [
                batch([doc("a.pdf", "RUNNING"), doc("b.pdf", "QUEUED")]),
                batch(
                    [
                        doc("a.pdf", "COMPLETED", duration_seconds=4.0),
                        doc("b.pdf", "RUNNING"),
                    ]
                ),
                batch(
                    [
                        doc("a.pdf", "COMPLETED", duration_seconds=4.0),
                        doc("b.pdf", "COMPLETED", duration_seconds=6.0),
                    ],
                    all_complete=True,
                ),
            ]
        )

        assert (
            cli_module._monitor_progress(
                client=client, batch_id="batch-1", refresh_interval=7
            )
            == 0
        )

        assert asked == ["batch-1", "batch-1", "batch-1"]
        assert clock.slept == [7, 7]

    def test_it_renders_the_state_of_every_poll_it_made(self, monkeypatch):
        """The intervening frames, not just the last one.

        A Rich `Live` writing to a non-terminal console emits only its final frame,
        so the transient states cannot be read back out of the captured output. What
        is observable is the layout the loop asked `display.create_live_display` to
        build on each pass, so this records those calls and delegates to the real
        function — if the loop stopped updating the display mid-run, or rendered the
        same snapshot twice, this is the test that notices.
        """
        clock = Clock()
        monkeypatch.setattr(cli_module, "time", clock)
        rendered = []
        real = display_module.create_live_display

        def recording(*, batch_id, status_data, stats, elapsed_time):
            rendered.append((stats["completed"], stats["running"], elapsed_time))
            return real(
                batch_id=batch_id,
                status_data=status_data,
                stats=stats,
                elapsed_time=elapsed_time,
            )

        monkeypatch.setattr(display_module, "create_live_display", recording)
        client, _ = monitored_client(
            [
                batch([doc("a.pdf", "RUNNING"), doc("b.pdf", "RUNNING")]),
                batch(
                    [
                        doc("a.pdf", "COMPLETED", duration_seconds=1.0),
                        doc("b.pdf", "RUNNING"),
                    ]
                ),
                batch(
                    [
                        doc("a.pdf", "COMPLETED", duration_seconds=1.0),
                        doc("b.pdf", "COMPLETED", duration_seconds=1.0),
                    ],
                    all_complete=True,
                ),
            ]
        )

        cli_module._monitor_progress(
            client=client, batch_id="batch-1", refresh_interval=5
        )

        # (completed, running, elapsed) per poll: elapsed grows by the interval.
        assert rendered == [(0, 2, 0.0), (1, 1, 5.0), (2, 0, 10.0)]

    def test_the_final_summary_reports_the_last_poll_and_the_elapsed_time(
        self, monkeypatch, pinned_console
    ):
        clock = Clock()
        monkeypatch.setattr(cli_module, "time", clock)
        client, _ = monitored_client(
            [
                batch([doc("a.pdf", "RUNNING")]),
                batch(
                    [doc("a.pdf", "COMPLETED", duration_seconds=8.0)], all_complete=True
                ),
            ]
        )

        with pinned_console.capture() as captured:
            cli_module._monitor_progress(
                client=client, batch_id="batch-1", refresh_interval=5
            )
        out = captured.get()

        assert "Monitoring Batch: batch-1" in out
        assert "Batch Processing Complete" in out
        assert "Total Documents" in out
        assert "Success Rate" in out
        # One sleep of 5s happened between the two polls, and the summary reports it.
        assert "5.0s" in out

    def test_a_failed_document_is_named_in_the_final_summary(
        self, monkeypatch, pinned_console
    ):
        clock = Clock()
        monkeypatch.setattr(cli_module, "time", clock)
        client, _ = monitored_client(
            [
                batch(
                    [
                        doc("a.pdf", "COMPLETED", duration_seconds=2.0),
                        doc("b.pdf", "FAILED", error="Bedrock throttled"),
                    ],
                    all_complete=True,
                    success_rate=0.5,
                )
            ]
        )

        with pinned_console.capture() as captured:
            cli_module._monitor_progress(
                client=client, batch_id="batch-1", refresh_interval=5
            )
        out = captured.get()

        assert "Failed Documents" in out
        assert "b.pdf" in out
        assert "Bedrock throttled" in out

    def test_it_returns_a_failing_code_for_a_batch_that_finished_with_failures(
        self, monkeypatch
    ):
        """The guarantee: the batch's outcome is a value the caller can propagate.

        The function used to print a summary and fall off the end, returning nothing,
        so a caller could not tell a clean batch from one in which every document
        failed. `status --wait` was the consequence that mattered — the same batch
        exited 1 when polled and 0 when waited on, and `--wait` is the form a
        pipeline uses (#1230). See
        `test_results_commands.py::test_wait_exits_non_zero_when_documents_failed`
        for that at the command level.

        Both directions are asserted in one test on purpose: a return of a constant
        would satisfy either half alone.
        """
        clock = Clock()
        monkeypatch.setattr(cli_module, "time", clock)
        failed_only, _ = monitored_client(
            [
                batch(
                    [
                        doc("a.pdf", "FAILED", error="x"),
                        doc("b.pdf", "FAILED", error="y"),
                    ],
                    all_complete=True,
                    success_rate=0.0,
                )
            ]
        )
        clean, _ = monitored_client(
            [
                batch(
                    [doc("a.pdf", "COMPLETED", duration_seconds=1.0)], all_complete=True
                )
            ]
        )

        assert (
            cli_module._monitor_progress(
                client=failed_only, batch_id="batch-1", refresh_interval=5
            )
            == 1
        )
        assert (
            cli_module._monitor_progress(
                client=clean, batch_id="batch-1", refresh_interval=5
            )
            == 0
        )

    def test_a_complete_batch_with_nothing_terminal_waits_out_the_grace_period(
        self, monkeypatch
    ):
        """`all_complete` alone does not end the loop while everything is still queued.

        The SDK reports `all_complete=True` for a batch whose documents have not
        reached the tracking table yet — no document is in a non-terminal state
        because there are no documents — and exiting there would declare a batch
        finished seconds after submitting it. The loop therefore also requires either
        one completed/failed document or 60 seconds elapsed. Here nothing ever
        becomes terminal, so it is the 60-second floor that ends it: at a 5-second
        interval that is 13 polls (the first at elapsed 0, then 12 sleeps to reach
        exactly 60). A regression that dropped the grace period would stop at 1.
        """
        clock = Clock()
        monkeypatch.setattr(cli_module, "time", clock)
        client, asked = monitored_client(
            [batch([doc("a.pdf", "QUEUED")], all_complete=True)]
        )

        cli_module._monitor_progress(
            client=client, batch_id="batch-1", refresh_interval=5
        )

        assert len(asked) == 13
        assert sum(clock.slept) == 60

    def test_one_terminal_document_ends_the_loop_immediately(self, monkeypatch):
        """The other half of the grace period: a real completion needs no wait."""
        clock = Clock()
        monkeypatch.setattr(cli_module, "time", clock)
        client, asked = monitored_client(
            [
                batch(
                    [doc("a.pdf", "COMPLETED", duration_seconds=3.0)], all_complete=True
                )
            ]
        )

        cli_module._monitor_progress(
            client=client, batch_id="batch-1", refresh_interval=5
        )

        assert len(asked) == 1
        assert clock.slept == []

    def test_ctrl_c_while_waiting_exits_cleanly_with_instructions(
        self, monkeypatch, pinned_console
    ):
        """`KeyboardInterrupt` must not surface as a traceback.

        Ctrl+C lands in `time.sleep` in practice, which is where this raises it. The
        contract is that monitoring stops, the user is told processing continues, and
        they are given the `idp-cli status` command to resume watching — with the
        *stack* name, which the function recovers from the client it was handed.
        """
        clock = Clock()

        def interrupt(_seconds):
            raise KeyboardInterrupt

        monkeypatch.setattr(
            cli_module, "time", SimpleNamespace(time=clock.time, sleep=interrupt)
        )
        client, asked = monitored_client(
            [batch([doc("a.pdf", "RUNNING"), doc("b.pdf", "QUEUED")])]
        )

        with pinned_console.capture() as captured:
            result = cli_module._monitor_progress(
                client=client, batch_id="batch-1", refresh_interval=5
            )
        out = captured.get()

        # 2, not 0: the watch ended without establishing what the batch did, and 2 is
        # this CLI's code for that. 0 would report a success nothing measured.
        assert result == 2
        assert len(asked) == 1
        assert "Monitoring stopped. Processing continues in background." in out
        assert "idp-cli status --stack-name my-stack --batch-id batch-1" in out
        assert "Traceback" not in out

    def test_a_monitoring_failure_is_reported_and_swallowed(
        self, monkeypatch, pinned_console
    ):
        """Any other exception ends the watch without re-raising, and answers 2.

        A batch whose status cannot be read at all is not a batch that succeeded, so
        the code is 2 — outcome not established — and `status --wait` exits on it.
        `process --monitor` still exits 0 whatever this returns, deliberately: its
        work is the submission, and the batch's verdict is a separate query.

        The instructions printed afterwards are wrong, and that is a separate defect:
        `_sn = stack_name or batch_id` on this path, and `stack_name` is `None` for
        every modern caller, so the suggested command reads `--stack-name batch-1`,
        naming the batch where a stack belongs. Copy-pasting it fails against a stack
        that does not exist.
        """
        clock = Clock()
        monkeypatch.setattr(cli_module, "time", clock)
        client = IDPClient(stack_name="my-stack", region="us-east-1")

        def explode(_batch_id):
            raise RuntimeError("DynamoDB is unavailable")

        client.batch.get_status = explode  # type: ignore[method-assign]

        with pinned_console.capture() as captured:
            result = cli_module._monitor_progress(
                client=client, batch_id="batch-1", refresh_interval=5
            )
        out = captured.get()

        assert result == 2
        assert "Monitoring error: DynamoDB is unavailable" in out
        assert "idp-cli status --stack-name batch-1 --batch-id batch-1" in out

    def test_a_legacy_caller_passing_a_stack_name_builds_its_own_client(
        self, monkeypatch, pinned_console
    ):
        """The pre-SDK signature took a stack name where `client` now sits.

        `_monitor_progress("my-stack", "batch-1", 5)` is the old call shape, and the
        `isinstance` guard exists to keep it working: anything that is not an
        `IDPClient` is treated as a stack name and a client is constructed from it.
        This patches the SDK operation rather than the client class so that the
        construction inside the function is the real one.
        """
        clock = Clock()
        monkeypatch.setattr(cli_module, "time", clock)
        monkeypatch.setattr(
            "idp_sdk.operations.batch.BatchOperation.get_status",
            lambda self, batch_id: batch(
                [doc("a.pdf", "COMPLETED", duration_seconds=1.0)], all_complete=True
            ),
        )

        with pinned_console.capture() as captured:
            result = cli_module._monitor_progress("my-stack", "batch-1", 5)

        assert result == 0
        assert "Batch Processing Complete" in captured.get()


@pytest.mark.unit
class TestTheMonitorCallersThatDiscardTheCode:
    """`process --monitor` and `rerun --monitor` exit 0 whatever the batch did.

    That is a deliberate narrowing of #1230 and it lived only in a code comment, which
    is not a place a decision survives. The argument: these commands' work is the
    *submission*, which succeeded; `--monitor` is a view of what happens afterwards.
    Exiting non-zero because 1 of 100 documents failed would stop
    `process --monitor && download-results` from collecting the 99 that worked. The
    batch's verdict is a separate question and `idp-cli status --batch-id` answers it.

    `status --wait`, by contrast, *is* that question, which is why it exits on the code.

    Pinned here so that "make the helper's return value consistent across its callers"
    is a decision someone has to revisit rather than a tidy-up they can do by
    inspection.
    """

    def _run(self, args, monitor_code):
        client = MagicMock()
        client.batch.process.return_value = SimpleNamespace(
            batch_id="batch-1",
            documents_queued=2,
            documents_uploaded=2,
            documents_failed=0,
        )
        client.batch.reprocess.return_value = SimpleNamespace(
            documents_queued=2,
            documents_failed=0,
            failed_documents=[],
        )
        with (
            patch("idp_sdk.IDPClient", return_value=client),
            patch("idp_cli.cli.IDPClient", return_value=client),
            patch(
                "idp_cli.cli._monitor_progress", return_value=monitor_code
            ) as monitor,
        ):
            run = CliRunner().invoke(cli_module.cli, args)
        return run, monitor

    def test_process_monitor_exits_zero_on_a_batch_that_failed(self, tmp_path):
        document = tmp_path / "a.pdf"
        document.write_bytes(b"%PDF-1.4\n")
        run, monitor = self._run(
            [
                "process",
                "--stack-name",
                "my-stack",
                "--dir",
                str(tmp_path),
                "--monitor",
            ],
            monitor_code=1,
        )

        monitor.assert_called_once()
        assert run.exit_code == 0, run.output

    def test_reprocess_monitor_exits_zero_on_a_batch_that_failed(self):
        run, monitor = self._run(
            [
                "reprocess",
                "--stack-name",
                "my-stack",
                "--batch-id",
                "batch-1",
                "--step",
                "extraction",
                "--force",
                "--monitor",
            ],
            monitor_code=1,
        )

        monitor.assert_called_once()
        assert run.exit_code == 0, run.output
