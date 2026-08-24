"""Unit tests for test_file_copier Lambda function.

The lynch-pin invariant these tests defend: when the runner enqueues a job
with ``filesToProcess = N``, the copier must actually copy N files, name N
files in DynamoDB ``Files``, and never let S3-folder drift (extra files
uploaded outside the test-set flow) cause "N/K" mismatches in the UI.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


FUNCTION_PATH = os.path.abspath(os.path.dirname(__file__))


def import_index():
    if FUNCTION_PATH not in sys.path:
        sys.path.insert(0, FUNCTION_PATH)
    if "index" in sys.modules:
        del sys.modules["index"]
    import index

    return index


@pytest.fixture(autouse=True)
def mock_env():
    with patch.dict(
        os.environ,
        {
            "TEST_SET_BUCKET": "test-set-bucket",
            "INPUT_BUCKET": "input-bucket",
            "BASELINE_BUCKET": "baseline-bucket",
            "LOG_LEVEL": "INFO",
        },
    ):
        yield


def _make_event(files_to_process=None, number_of_files=None):
    body = {
        "testRunId": "run-123",
        "testSetId": "my-test-set",
        "trackingTable": "tracking",
    }
    if files_to_process is not None:
        body["filesToProcess"] = files_to_process
    if number_of_files is not None:
        body["numberOfFiles"] = number_of_files
    return {"Records": [{"body": json.dumps(body)}]}


@pytest.mark.unit
class TestFileCap:
    """S3 folder can have more files than the test-set's declared fileCount
    (someone uploaded extras outside the API). Copier must never process more
    than filesToProcess or the run's Files list balloons past FilesCount and
    the UI shows "85/10 completed"-style bogus progress.
    """

    def test_caps_at_files_to_process_when_s3_has_more(self, caplog):
        """filesToProcess=10 + 85 files in S3 → 10 files copied, sorted."""
        index = import_index()

        # 85 hash-named files: the exact shape of the real bug on
        # realkie-fcc-verified where the S3 folder had drifted past the
        # test set's fileCount=10.
        s3_input_files = sorted([f"file_{i:03d}.pdf" for i in range(85)])
        s3_baseline_files = sorted(
            [f"file_{i:03d}.pdf/ground_truth.json" for i in range(85)]
        )

        with (
            patch.object(index, "_list_test_set_files") as list_files,
            patch.object(index, "_copy_files_to_bucket") as copy_files,
            patch.object(index, "_update_tracking_in_progress") as update_track,
            patch.object(index, "_update_test_run_status"),
        ):
            list_files.side_effect = [s3_input_files, s3_baseline_files]
            # Simulate every copy succeeding.
            copy_files.side_effect = lambda *a, **kw: list(a[4])  # 5th positional = files

            # Attach caplog to the Lambda's logger — root propagation is
            # off by default when a module owns its own logger via
            # ``logging.getLogger()`` at module scope.
            with caplog.at_level("WARNING", logger=index.logger.name):
                index.handler(_make_event(files_to_process=10), None)

            # Files list written to DynamoDB is capped to 10 — matches
            # FilesCount in the runner's metadata write.
            (_, _, files_written), _ = update_track.call_args
            assert len(files_written) == 10, (
                f"expected 10 files in tracking write, got {len(files_written)}"
            )
            # Deterministic: exactly the lexicographically first 10.
            assert files_written == [f"file_{i:03d}.pdf" for i in range(10)]

            # The drift-warning log line is the operator-facing signal that an
            # S3 test-set folder has grown past the test set's declared
            # fileCount. Pinning it here so a future refactor that silently
            # drops the WARNING (making the drift invisible) fails a test
            # instead of shipping.
            drift_warnings = [
                r for r in caplog.records
                if r.levelname == "WARNING" and "scoped to 10" in r.message
            ]
            assert len(drift_warnings) == 1, (
                f"expected 1 drift-warning log line, got {len(drift_warnings)}: "
                f"{[r.message for r in caplog.records]}"
            )
            # Cross-check the warning names the actual size and cap so a
            # future off-by-one in the log-formatting doesn't slip through.
            assert "contains 85 input files" in drift_warnings[0].message

            # The two copy_files_to_bucket calls (input + baseline) both
            # received capped file lists. Baselines were filtered to match
            # the surviving inputs.
            input_call, baseline_call = copy_files.call_args_list
            assert len(input_call.args[4]) == 10  # input files list
            assert len(baseline_call.args[4]) == 10  # baseline files list
            for bf in baseline_call.args[4]:
                # Every baseline path must sit under one of the 10 kept inputs.
                assert bf.startswith("file_0"), f"unexpected baseline: {bf}"

    def test_no_cap_when_s3_matches_or_below_files_to_process(self):
        """filesToProcess=10 + only 8 files in S3 → all 8 copied, no cap log."""
        index = import_index()

        s3_input_files = [f"doc_{i:03d}.pdf" for i in range(8)]
        s3_baseline_files = [f"doc_{i:03d}.pdf/gt.json" for i in range(8)]

        with (
            patch.object(index, "_list_test_set_files") as list_files,
            patch.object(index, "_copy_files_to_bucket") as copy_files,
            patch.object(index, "_update_tracking_in_progress") as update_track,
            patch.object(index, "_update_test_run_status"),
        ):
            list_files.side_effect = [s3_input_files, s3_baseline_files]
            copy_files.side_effect = lambda *a, **kw: list(a[4])

            index.handler(_make_event(files_to_process=10), None)

            (_, _, files_written), _ = update_track.call_args
            assert len(files_written) == 8

    def test_backward_compatible_falls_back_to_number_of_files(self):
        """Message enqueued before filesToProcess field was added must still
        cap on numberOfFiles as before — no regression for in-flight jobs
        during the deploy window.
        """
        index = import_index()

        s3_input_files = sorted([f"f_{i:03d}.pdf" for i in range(50)])

        with (
            patch.object(index, "_list_test_set_files") as list_files,
            patch.object(index, "_copy_files_to_bucket") as copy_files,
            patch.object(index, "_update_tracking_in_progress") as update_track,
            patch.object(index, "_update_test_run_status"),
        ):
            list_files.side_effect = [s3_input_files, []]
            copy_files.side_effect = lambda *a, **kw: list(a[4])

            # No filesToProcess in the message — must fall back to
            # numberOfFiles like the pre-fix code did.
            index.handler(_make_event(number_of_files=5), None)

            (_, _, files_written), _ = update_track.call_args
            assert len(files_written) == 5

    def test_no_cap_when_neither_field_set(self):
        """If neither filesToProcess nor numberOfFiles is present (very old
        messages predating both), the copier processes every S3 file. This
        preserves current behavior on the untouched path — the runner
        started sending filesToProcess in this MR, so a real deploy will
        always have it, but the fallback matters for messages already in
        the queue.
        """
        index = import_index()

        s3_input_files = [f"g_{i:03d}.pdf" for i in range(20)]

        with (
            patch.object(index, "_list_test_set_files") as list_files,
            patch.object(index, "_copy_files_to_bucket") as copy_files,
            patch.object(index, "_update_tracking_in_progress") as update_track,
            patch.object(index, "_update_test_run_status"),
        ):
            list_files.side_effect = [s3_input_files, []]
            copy_files.side_effect = lambda *a, **kw: list(a[4])

            index.handler(_make_event(), None)

            (_, _, files_written), _ = update_track.call_args
            assert len(files_written) == 20  # no cap applied


@pytest.mark.unit
class TestDraftLabelingSkipsBaselines:
    """A draft-labeling run PRODUCES ground truth, so it must not be scored.

    Staging the current baseline makes the evaluation step compare the new
    extraction against a stale copy of itself. Observed live: a re-run with
    corrected settings reported accuracy 0.47 by scoring its single merged
    section against one section of the previous run's output — and because
    aggregation records confidence-curve observations for any run carrying a
    TestSetId, that meaningless verdict fed the review estimator's calibration.
    """

    def test_draft_labeling_run_does_not_stage_baselines(self, caplog):
        index = import_index()
        inputs = ["a.pdf", "b.pdf"]
        baselines = ["a.pdf/result.json", "b.pdf/result.json"]

        with (
            patch.object(index, "_list_test_set_files") as list_files,
            patch.object(index, "_copy_files_to_bucket") as copy_files,
            patch.object(index, "_update_tracking_in_progress"),
            patch.object(index, "_update_test_run_status"),
        ):
            list_files.side_effect = [inputs, baselines]
            copy_files.side_effect = lambda *a, **kw: list(a[4])

            body = {
                "testRunId": "run-draft",
                "testSetId": "my-test-set",
                "trackingTable": "tracking",
                "purpose": "draft-labeling",
            }
            with caplog.at_level("INFO", logger=index.logger.name):
                index.handler({"Records": [{"body": json.dumps(body)}]}, None)

            # Inputs are copied; baselines are not. One call, not two.
            assert copy_files.call_count == 1, (
                "baseline copy must be skipped for a draft-labeling run"
            )
            assert copy_files.call_args_list[0].args[2] == "input-bucket"
            assert any(
                "not staging baselines" in r.message for r in caplog.records
            ), "the skip should be logged so it is not silently surprising"

    def test_scoring_run_still_stages_baselines(self):
        """The default path is unchanged — a scoring run needs its baseline."""
        index = import_index()
        inputs = ["a.pdf"]
        baselines = ["a.pdf/result.json"]

        with (
            patch.object(index, "_list_test_set_files") as list_files,
            patch.object(index, "_copy_files_to_bucket") as copy_files,
            patch.object(index, "_update_tracking_in_progress"),
            patch.object(index, "_update_test_run_status"),
        ):
            list_files.side_effect = [inputs, baselines]
            copy_files.side_effect = lambda *a, **kw: list(a[4])

            index.handler(_make_event(), None)

            assert copy_files.call_count == 2
            assert copy_files.call_args_list[1].args[2] == "baseline-bucket"

    def test_absent_purpose_defaults_to_scoring(self):
        """Messages enqueued before `purpose` existed must keep working."""
        index = import_index()
        with (
            patch.object(index, "_list_test_set_files") as list_files,
            patch.object(index, "_copy_files_to_bucket") as copy_files,
            patch.object(index, "_update_tracking_in_progress"),
            patch.object(index, "_update_test_run_status"),
        ):
            list_files.side_effect = [["a.pdf"], ["a.pdf/result.json"]]
            copy_files.side_effect = lambda *a, **kw: list(a[4])
            index.handler(_make_event(), None)
            assert copy_files.call_count == 2
