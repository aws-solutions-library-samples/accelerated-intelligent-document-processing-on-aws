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

from datetime import datetime
from types import SimpleNamespace
from typing import Optional

import pytest
from rich.console import Console

from idp_cli import cli as cli_module
from idp_cli import display as display_module
from idp_sdk import IDPClient
from idp_sdk.models import BatchStatus
from idp_sdk.models.document import DocumentStatus

#: Every state `_batch_status_to_display_dicts` explicitly treats as in flight.
RUNNING_STATES = (
    "RUNNING",
    "CLASSIFYING",
    "EXTRACTING",
    "ASSESSING",
    "RULE_VALIDATION",
    "RULE_VALIDATION_ORCHESTRATOR",
    "SUMMARIZING",
    "HITL_IN_PROGRESS",
    "EVALUATING",
)

#: States `DocumentState` defines that the mapper does not name at all. Some are in
#: flight, two are terminal, and all of them land in the `queued` bucket — see
#: `test_states_the_mapper_does_not_name_are_all_reported_as_queued`.
UNNAMED_STATES = (
    "PENDING_UPLOAD",
    "STARTED",
    "PREPROCESSING",
    "OCR",
    "RULE_VALIDATION_POLICY_CLASSIFICATION",
    "POSTPROCESSING",
    "IN_PROGRESS",
    "ABORTED",
    "REDACTED_SUPERSEDED",
    "NOT_FOUND",
    "UNKNOWN",
)


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
                "start_time": started,
                "end_time": ended,
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

    @pytest.mark.parametrize("state", UNNAMED_STATES)
    def test_states_the_mapper_does_not_name_are_all_reported_as_queued(self, state):
        """DEFECT, pinned as it behaves today (`cli.py:3311-3324`).

        The bucketing is an `if COMPLETED / elif FAILED / elif <nine in-flight names>
        / else queued` chain, so the eleven `DocumentState` members it does not name
        fall through to `queued`. Three consequences, in rising order of severity:

        * `OCR`, `PREPROCESSING`, `STARTED`, `IN_PROGRESS`, `POSTPROCESSING` and
          `RULE_VALIDATION_POLICY_CLASSIFICATION` are documents actively being worked
          on, and they are displayed under "Queued". `PREPROCESSING` is set for every
          document whenever a preprocessing hook is registered, so on a stack with
          PII anonymization enabled the running count reads 0 for the whole run.
        * `ABORTED` and `REDACTED_SUPERSEDED` are **terminal**. Reported as queued
          they never appear in the failed count, so `status` prints
          "IN PROGRESS (0/1 finished)" and exits 2 for a batch that has stopped.
        * `NOT_FOUND` — the state for a document id that does not exist — also reads
          as queued, so a typo in `--document-id` looks like work in progress.
        """
        status_data, stats = cli_module._batch_status_to_display_dicts(
            batch([doc("batch-1/d.pdf", state)], all_complete=False)
        )

        assert [d["document_id"] for d in status_data["queued"]] == ["batch-1/d.pdf"]
        assert stats["queued"] == 1
        assert stats["running"] == 0
        assert stats["failed"] == 0
        assert stats["completed"] == 0

    def test_end_time_is_a_datetime_when_present_and_a_string_when_not(self):
        """DEFECT, pinned as it behaves today (`cli.py:3297`).

        `doc.end_time or ""` leaves a `datetime` in place and substitutes `str` when
        the field is absent, so one batch can produce both types under the same key.
        `display.create_recent_completions_table` sorts the completed documents by
        that key, and Python will not order a `datetime` against a `str`: a batch
        holding one completed document with an end time and one without raises
        `TypeError` inside the display layer. Both callers catch it broadly, so the
        observable result is `idp-cli status` printing
        "✗ Error: '<' not supported between instances of 'datetime.datetime' and
        'str'" and exiting 1 instead of showing the table, and `--monitor` printing
        "Monitoring error:" and abandoning the watch.

        The assertion below is on the mapper, where the mixed types originate; the
        `TypeError` is then demonstrated through the real display function so the
        consequence is pinned too, not just described.
        """
        status_data, _ = cli_module._batch_status_to_display_dicts(
            batch(
                [
                    doc(
                        "batch-1/with.pdf",
                        "COMPLETED",
                        end_time=datetime(2025, 1, 2, 3, 4, 5),
                        duration_seconds=5.0,
                    ),
                    doc("batch-1/without.pdf", "COMPLETED", duration_seconds=5.0),
                ],
                all_complete=True,
            )
        )

        end_times = [d["end_time"] for d in status_data["completed"]]
        assert end_times == [datetime(2025, 1, 2, 3, 4, 5), ""]

        with pytest.raises(TypeError, match="not supported between instances"):
            display_module.create_recent_completions_table(status_data)

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
            is None
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

    def test_it_returns_none_even_for_a_batch_that_finished_with_failures(
        self, monkeypatch
    ):
        """DEFECT, pinned as it behaves today (`cli.py:3465-3468`).

        The function ends by printing a summary and falling off the end; it has no
        return value and raises nothing, so a caller cannot tell a clean batch from
        one where every document failed. `status --wait` is the case that matters:
        the same batch reported without `--wait` exits 1, and with `--wait` exits 0.
        See `test_results_commands.py::test_wait_exits_zero_even_when_documents_failed`
        for that consequence at the command level.
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
            is None
        )
        assert (
            cli_module._monitor_progress(
                client=clean, batch_id="batch-1", refresh_interval=5
            )
            is None
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

        assert result is None
        assert len(asked) == 1
        assert "Monitoring stopped. Processing continues in background." in out
        assert "idp-cli status --stack-name my-stack --batch-id batch-1" in out
        assert "Traceback" not in out

    def test_a_monitoring_failure_is_reported_and_swallowed(
        self, monkeypatch, pinned_console
    ):
        """Any other exception ends the watch without re-raising and without an exit code.

        Worth knowing rather than admiring: a batch whose status cannot be read at
        all is indistinguishable, in exit code, from one that completed cleanly —
        `process --monitor` still exits 0. The instructions printed afterwards are
        also wrong: `_sn = stack_name or batch_id` on this path, and `stack_name` is
        `None` for every modern caller, so the suggested command reads
        `--stack-name batch-1`, naming the batch where a stack belongs. Copy-pasting
        it fails against a stack that does not exist.
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

        assert result is None
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

        assert result is None
        assert "Batch Processing Complete" in captured.get()
