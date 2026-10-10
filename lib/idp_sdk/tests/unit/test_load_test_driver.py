# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ``idp_sdk._core.load_test``.

The module under test drives synthetic load at a deployed IDP stack by repeatedly
putting the *same* document into the stack's input bucket under a fresh key, so
that each copy is ingested as a separate document. It offers two shapes:
``run_constant_load`` (a target rate for a number of minutes, with an adaptive
batch-size controller) and ``run_scheduled_load`` (a CSV of ``minute,count`` rows).
``CopyStats`` is the lock-protected counter both share.

Two decisions shaped these tests.

**The destination keys are the observable output.** A load test's whole product is
a set of S3 objects: how many there are, what they are named, and what metadata
they carry — the ``config-version`` metadata in particular, because that is what
decides which configuration profile the stack processes each file with. So the
tests run against a real ``moto`` S3 and read the bucket back, rather than
asserting that ``copy_object`` was called. A mock would accept a copy with no
``MetadataDirective``, which silently drops the config version, and a key
collision, which silently halves the load.

**Both drivers are wall-clock loops, so the clock is the test input.** Each is
`while` over ``time.time()`` with a ``time.sleep`` at the bottom, and the adaptive
controller reads a rate derived from elapsed time. Every test here therefore
replaces the module's ``time`` reference with a fake clock that only advances when
the code under test sleeps. That makes the iteration count exact rather than
load-dependent: a run is a chosen number of loop passes, the file count is
arithmetic, and nothing depends on how fast this host happens to be. The
substitution is scoped to ``idp_sdk._core.load_test`` — patching the real
``time.time`` would also reach botocore and ``concurrent.futures``.

Two defects are pinned rather than fixed, each in a test whose docstring says so:
a scheduled run from a *local* source ignores its schedule entirely, and the
adaptive controller's floor of one copy per loop pass makes any requested rate
below roughly 600 files/minute unachievable.
"""

import json

import boto3
import pytest
from moto import mock_aws

from idp_sdk._core import load_test
from idp_sdk._core.load_test import CopyStats, LoadTester

STACK_NAME = "idp-load-stack"
INPUT_BUCKET = "idp-load-input"
SOURCE_BUCKET = "idp-load-source"
SOURCE_KEY = "samples/lending_package.pdf"

# The queue is the one resource ``StackInfo`` looks up by logical id rather than
# reading from an output, and it raises if it is absent — so every stack variant
# here needs it even though the load tester itself never touches the queue.
_STACK_RESOURCES = {
    "DocumentQueue": {
        "Type": "AWS::SQS::Queue",
        "Properties": {"QueueName": "idp-load-documents"},
    },
}


def _template(outputs: dict) -> str:
    return json.dumps(
        {
            "AWSTemplateFormatVersion": "2010-09-09",
            "Resources": dict(_STACK_RESOURCES),
            "Outputs": {key: {"Value": value} for key, value in outputs.items()},
        }
    )


class FakeClock:
    """A clock that only moves when the code under test sleeps.

    ``advance`` stands in for the wall-clock time one ``sleep`` would really take,
    which is what makes an iteration count selectable: with ``advance`` of 60 and a
    one-minute run the loop executes exactly once.
    """

    def __init__(self, advance: float, start: float = 1_000_000.0):
        self.advance = advance
        self.now = start
        self.sleeps = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += self.advance


@pytest.fixture
def clock(monkeypatch):
    """Install a fake clock into the module under test and let a test tune it."""

    def install(advance: float) -> FakeClock:
        fake = FakeClock(advance)
        monkeypatch.setattr(load_test, "time", fake)
        return fake

    return install


@pytest.fixture
def aws(aws_credentials):
    """A moto-backed AWS for the whole test, with the stack already deployed."""
    with mock_aws():
        cfn = boto3.client("cloudformation", region_name=aws_credentials)
        cfn.create_stack(
            StackName=STACK_NAME,
            TemplateBody=_template(
                {
                    "S3InputBucketName": INPUT_BUCKET,
                    "S3OutputBucketName": "idp-load-output",
                    "LambdaLookupFunctionName": "idp-load-Lookup",
                }
            ),
        )
        cfn.create_stack(
            StackName="idp-load-stack-no-input",
            TemplateBody=_template({"S3OutputBucketName": "idp-load-output"}),
        )
        s3 = boto3.client("s3", region_name=aws_credentials)
        s3.create_bucket(Bucket=INPUT_BUCKET)
        s3.create_bucket(Bucket=SOURCE_BUCKET)
        s3.put_object(
            Bucket=SOURCE_BUCKET,
            Key=SOURCE_KEY,
            Body=b"%PDF-1.7 lending package",
            Metadata={"origin": "corpus"},
        )
        yield s3


@pytest.fixture
def tester(aws, aws_credentials):
    return LoadTester(STACK_NAME, region=aws_credentials)


def _keys(s3) -> list:
    return sorted(
        obj["Key"]
        for obj in s3.list_objects_v2(Bucket=INPUT_BUCKET).get("Contents", [])
    )


@pytest.mark.unit
class TestCopyStats:
    """The shared counter. Every rate decision in the module reads from it."""

    def test_increment_returns_the_running_sequence_number(self, clock):
        clock(advance=0)
        stats = CopyStats()

        assert [stats.increment(), stats.increment(), stats.increment()] == [1, 2, 3]
        assert stats.get_total() == 3

    def test_a_minute_of_zero_is_not_recorded_against_any_minute(self, clock):
        """Minute 0 means "constant mode", not "the zeroth minute".

        ``increment`` only touches ``copies_by_minute`` for a truthy minute, which
        is what lets the constant-rate driver share the counter with the scheduled
        one. Recording under key 0 would be harmless here but would make
        ``get_minute_copies(0)`` mean two different things.
        """
        clock(advance=0)
        stats = CopyStats()

        stats.increment()
        stats.increment(minute=0)

        assert stats.get_total() == 2
        assert dict(stats.copies_by_minute) == {}

    def test_copies_are_attributed_to_the_minute_they_were_asked_for(self, clock):
        clock(advance=0)
        stats = CopyStats()

        stats.increment(minute=1)
        stats.increment(minute=1)
        stats.increment(minute=3)

        assert stats.get_minute_copies(1) == 2
        assert stats.get_minute_copies(3) == 1
        # An unvisited minute reads as zero rather than raising: the scheduled
        # driver asks about the current minute before anything has run in it.
        assert stats.get_minute_copies(2) == 0

    def test_current_rate_is_copies_per_minute_of_elapsed_time(self, clock):
        fake = clock(advance=30)
        stats = CopyStats()
        for _ in range(5):
            stats.increment()

        fake.sleep(0.1)  # 30 simulated seconds

        assert stats.get_current_rate() == pytest.approx(10.0)

    def test_current_rate_is_zero_before_any_time_has_passed(self, clock):
        """Guards the division: the first loop pass reads the rate at elapsed 0."""
        clock(advance=0)
        stats = CopyStats()
        stats.increment()

        assert stats.get_current_rate() == 0

    def test_elapsed_time_is_split_into_whole_minutes_and_seconds(self, clock):
        fake = clock(advance=125)
        stats = CopyStats()

        fake.sleep(0.1)

        assert stats.get_elapsed_time() == (2, 5)


@pytest.mark.unit
class TestCopyFile:
    """One S3-to-S3 copy: the unit both drivers submit to the thread pool."""

    def test_a_copy_lands_under_the_prefix_with_a_sequence_numbered_name(
        self, tester, aws, clock
    ):
        clock(advance=0)
        stats = CopyStats()

        assert tester._copy_file(SOURCE_BUCKET, SOURCE_KEY, "load-test", stats) is True
        assert tester._copy_file(SOURCE_BUCKET, SOURCE_KEY, "load-test", stats) is True

        assert _keys(aws) == [
            "load-test/lending_package_000001.pdf",
            "load-test/lending_package_000002.pdf",
        ]

    def test_the_minute_appears_in_the_name_in_scheduled_mode(self, tester, aws, clock):
        """The filename is the only record of which schedule minute produced a file.

        Nothing else persists the attribution, so an operator correlating an
        ingestion spike with a schedule row has the key and nothing else.
        """
        clock(advance=0)
        stats = CopyStats()

        tester._copy_file(
            SOURCE_BUCKET,
            SOURCE_KEY,
            "load-test",
            stats,
            current_minute=7,
            target_copies=1,
        )

        assert _keys(aws) == ["load-test/lending_package_007_000001.pdf"]

    def test_without_a_config_version_the_sources_own_metadata_is_preserved(
        self, tester, aws, clock
    ):
        clock(advance=0)
        tester._copy_file(SOURCE_BUCKET, SOURCE_KEY, "load-test", CopyStats())

        head = aws.head_object(
            Bucket=INPUT_BUCKET, Key="load-test/lending_package_000001.pdf"
        )
        assert head["Metadata"] == {"origin": "corpus"}

    def test_a_config_version_is_written_as_metadata_and_replaces_the_rest(
        self, tester, aws, clock
    ):
        """``config-version`` metadata is how a load test selects a config profile.

        The stack reads this key off the input object at queue time, so getting it
        wrong does not fail — it silently processes the load with the default
        profile, and the run measures the wrong thing. The second assertion
        records the cost of the ``MetadataDirective: REPLACE`` needed to set it:
        any other metadata on the source object is dropped.
        """
        clock(advance=0)
        tester._copy_file(
            SOURCE_BUCKET, SOURCE_KEY, "load-test", CopyStats(), config_version="v0.6.9"
        )

        head = aws.head_object(
            Bucket=INPUT_BUCKET, Key="load-test/lending_package_000001.pdf"
        )
        assert head["Metadata"] == {"config-version": "v0.6.9"}

    def test_a_copy_beyond_this_minutes_target_is_refused_before_it_is_made(
        self, tester, aws, clock
    ):
        """This check is the whole enforcement of a schedule's per-minute count.

        The scheduled driver submits ``target - done`` copies at a time but several
        threads may be in flight, so the last word on whether a minute has had
        enough files is here. A failure would mean a schedule row of 100 delivers
        more than 100.
        """
        clock(advance=0)
        stats = CopyStats()
        stats.increment(minute=2)
        stats.increment(minute=2)

        refused = tester._copy_file(
            SOURCE_BUCKET,
            SOURCE_KEY,
            "load-test",
            stats,
            current_minute=2,
            target_copies=2,
        )

        assert refused is False
        assert _keys(aws) == []
        # The refusal must not consume a sequence number either.
        assert stats.get_total() == 2

    def test_an_s3_failure_is_reported_as_false_rather_than_raised(
        self, tester, aws, clock
    ):
        """A failed copy must not kill the run.

        Copies execute inside a ``ThreadPoolExecutor``; an exception would be
        re-raised at ``future.result()`` in the driver loop and abort the whole
        load test, so a single throttled copy would end a twenty-minute run.
        """
        clock(advance=0)
        stats = CopyStats()

        assert (
            tester._copy_file(SOURCE_BUCKET, "no/such/key.pdf", "load-test", stats)
            is False
        )
        assert _keys(aws) == []
        # The sequence number was already taken before the copy was attempted, so
        # a failed copy leaves a gap in the numbering rather than reusing it.
        assert stats.get_total() == 1


@pytest.mark.unit
class TestUploadLocalFile:
    """The local-file variant, used when the source is a path rather than an s3 URI."""

    def test_the_file_content_reaches_the_input_bucket(
        self, tester, aws, clock, tmp_path
    ):
        clock(advance=0)
        source = tmp_path / "statement.pdf"
        source.write_bytes(b"%PDF-1.7 statement")

        assert tester._upload_local_file(str(source), "load-test", CopyStats()) is True

        body = aws.get_object(
            Bucket=INPUT_BUCKET, Key="load-test/statement_000001.pdf"
        )["Body"].read()
        assert body == b"%PDF-1.7 statement"

    def test_a_config_version_is_attached_to_a_local_upload_too(
        self, tester, aws, clock, tmp_path
    ):
        clock(advance=0)
        source = tmp_path / "statement.pdf"
        source.write_bytes(b"%PDF")

        tester._upload_local_file(
            str(source), "load-test", CopyStats(), config_version="v0.6.9"
        )

        head = aws.head_object(
            Bucket=INPUT_BUCKET, Key="load-test/statement_000001.pdf"
        )
        assert head["Metadata"] == {"config-version": "v0.6.9"}

    def test_a_missing_local_file_is_reported_as_false(
        self, tester, aws, clock, tmp_path
    ):
        clock(advance=0)

        assert (
            tester._upload_local_file(
                str(tmp_path / "gone.pdf"), "load-test", CopyStats()
            )
            is False
        )
        assert _keys(aws) == []


@pytest.mark.unit
class TestRunConstantLoad:
    """The constant-rate driver, including its adaptive batch-size controller."""

    def test_a_stack_without_an_input_bucket_output_fails_before_copying(
        self, aws, aws_credentials, clock
    ):
        """``StackInfo`` renders a missing output as ``""``, not as a missing key.

        So the guard has to be a truthiness check, and this is the shape of a
        stack that has not finished deploying its buckets.
        """
        clock(advance=60)
        tester = LoadTester("idp-load-stack-no-input", region=aws_credentials)

        result = tester.run_constant_load("s3://%s/%s" % (SOURCE_BUCKET, SOURCE_KEY))

        assert result == {
            "success": False,
            "error": "Input bucket not found in stack outputs",
        }
        assert _keys(aws) == []

    def test_a_missing_local_source_fails_before_copying(
        self, tester, aws, clock, tmp_path
    ):
        clock(advance=60)
        missing = tmp_path / "absent.pdf"

        result = tester.run_constant_load(str(missing))

        assert result["success"] is False
        assert str(missing) in result["error"]
        assert _keys(aws) == []

    def test_an_s3_source_is_copied_at_the_batch_size_derived_from_the_rate(
        self, tester, aws, clock
    ):
        """One loop pass at 120 files/minute copies the 30-batches-per-minute share.

        ``batch_size`` starts at ``rate / 30`` = 4, and the first pass sees an
        elapsed time of zero, so the controller's "below target" branch multiplies
        by 1.2 and truncates — back to 4. With the clock advanced a whole minute by
        the single sleep, the run is exactly one pass and exactly four files, which
        is what makes the reported ``average_rate`` checkable arithmetic rather
        than a timing artefact.
        """
        fake = clock(advance=60)

        result = tester.run_constant_load(
            "s3://%s/%s" % (SOURCE_BUCKET, SOURCE_KEY),
            rate=120,
            duration=1,
            dest_prefix="constant",
            config_version="v0.6.9",
        )

        assert _keys(aws) == [
            "constant/lending_package_00000%d.pdf" % n for n in range(1, 5)
        ]
        assert result == {
            "success": True,
            "total_files": 4,
            "duration_seconds": 60.0,
            "average_rate": 4.0,
        }
        assert fake.sleeps == [0.1]
        head = aws.head_object(
            Bucket=INPUT_BUCKET, Key="constant/lending_package_000001.pdf"
        )
        assert head["Metadata"] == {"config-version": "v0.6.9"}

    def test_a_local_source_is_uploaded_rather_than_copied(
        self, tester, aws, clock, tmp_path
    ):
        clock(advance=60)
        source = tmp_path / "bank_statement.pdf"
        source.write_bytes(b"%PDF-1.7 bank statement")

        result = tester.run_constant_load(
            str(source), rate=60, duration=1, dest_prefix="constant"
        )

        assert _keys(aws) == [
            "constant/bank_statement_000001.pdf",
            "constant/bank_statement_000002.pdf",
        ]
        assert result["total_files"] == 2
        body = aws.get_object(
            Bucket=INPUT_BUCKET, Key="constant/bank_statement_000001.pdf"
        )["Body"].read()
        assert body == b"%PDF-1.7 bank statement"

    def test_a_multi_pass_run_keeps_growing_the_batch_while_it_is_under_target(
        self, tester, aws, clock
    ):
        """The controller's growth branch compounds across passes.

        At 1200 files/minute the batch starts at 40; pass one grows it to 48 and
        pass two — still far under target, because only 48 files have gone in 30
        simulated seconds — grows it to 57. 105 files over two passes. A failure
        here means the controller is not reacting to the measured rate at all,
        which on a real run shows up as a load test that never reaches the rate
        it was asked for.
        """
        clock(advance=30)

        result = tester.run_constant_load(
            "s3://%s/%s" % (SOURCE_BUCKET, SOURCE_KEY),
            rate=1200,
            duration=1,
            dest_prefix="constant",
        )

        assert result["total_files"] == 105
        assert len(_keys(aws)) == 105

    def test_the_batch_floor_makes_a_low_requested_rate_unachievable(
        self, tester, aws, clock
    ):
        """DEFECT: rates below ~600 files/minute cannot be honoured.

        The controller shrinks ``batch_size`` when it is over target, but
        ``batch_size = max(1, ...)`` floors it at one copy per loop pass, and the
        loop sleeps a fixed 0.1s. One file per 0.1s is 600 files/minute, so that
        is the *minimum* throughput of ``run_constant_load`` regardless of the
        ``rate`` argument: a request for 10 files/minute delivers roughly 600, a
        60x over-delivery, and there is no slower setting available.
        ``load_test.py:245`` is the floor and ``load_test.py:276`` the fixed sleep.

        Measured here with a 5-second simulated sleep so the run is 12 passes
        rather than 600: 10 files/minute were asked for and 12 were delivered,
        with the controller shrinking on every pass after the first and being
        clamped back to 1 each time. Pinning the over-delivery, not fixing it —
        honouring a low rate needs a sleep derived from the rate, which is
        production code.
        """
        clock(advance=5)

        result = tester.run_constant_load(
            "s3://%s/%s" % (SOURCE_BUCKET, SOURCE_KEY),
            rate=10,
            duration=1,
            dest_prefix="constant",
        )

        assert result["total_files"] == 12
        assert result["total_files"] > 10  # i.e. more than rate * duration
        assert result["average_rate"] == pytest.approx(12.0)
        assert len(_keys(aws)) == 12


@pytest.mark.unit
class TestRunScheduledLoad:
    """The CSV-driven driver: a per-minute count instead of a flat rate."""

    def test_a_stack_without_an_input_bucket_output_fails_before_reading_the_csv(
        self, aws, aws_credentials, clock, tmp_path
    ):
        clock(advance=60)
        tester = LoadTester("idp-load-stack-no-input", region=aws_credentials)

        result = tester.run_scheduled_load(
            "s3://%s/%s" % (SOURCE_BUCKET, SOURCE_KEY),
            str(tmp_path / "never-read.csv"),
        )

        assert result == {
            "success": False,
            "error": "Input bucket not found in stack outputs",
        }

    def test_the_header_row_and_malformed_rows_are_skipped(
        self, tester, aws, clock, tmp_path
    ):
        """A schedule CSV is hand-written, so the parser has to tolerate its shapes.

        The header, a three-column row and a row whose minute is not a number are
        all dropped; only ``minute,count`` pairs survive. A parser that accepted
        the header would raise on ``int("minute")`` and lose the whole run.
        """
        clock(advance=60)
        schedule = tmp_path / "schedule.csv"
        # A BOM, because a CSV saved from Excel has one and the module opens the
        # file as utf-8-sig for exactly that reason.
        schedule.write_text(
            "﻿minute,count\n1,2\nnot-a-minute,4\n2,1,extra\n\n", encoding="utf-8"
        )

        result = tester.run_scheduled_load(
            "s3://%s/%s" % (SOURCE_BUCKET, SOURCE_KEY),
            str(schedule),
            dest_prefix="sched",
        )

        assert result["planned_files"] == 2
        assert _keys(aws) == [
            "sched/lending_package_001_000001.pdf",
            "sched/lending_package_001_000002.pdf",
        ]

    def test_a_csv_with_no_usable_rows_is_refused(self, tester, aws, clock, tmp_path):
        clock(advance=60)
        schedule = tmp_path / "schedule.csv"
        schedule.write_text("minute,count\n#comment\n", encoding="utf-8")

        result = tester.run_scheduled_load(
            "s3://%s/%s" % (SOURCE_BUCKET, SOURCE_KEY), str(schedule)
        )

        assert result == {"success": False, "error": "No valid schedule entries found"}
        assert _keys(aws) == []

    def test_a_missing_local_source_is_refused_after_the_csv_is_read(
        self, tester, aws, clock, tmp_path
    ):
        clock(advance=60)
        schedule = tmp_path / "schedule.csv"
        schedule.write_text("minute,count\n1,1\n", encoding="utf-8")
        missing = tmp_path / "absent.pdf"

        result = tester.run_scheduled_load(str(missing), str(schedule))

        assert result["success"] is False
        assert str(missing) in result["error"]
        assert _keys(aws) == []

    def test_each_minute_delivers_exactly_its_row_and_is_named_for_it(
        self, tester, aws, clock, tmp_path
    ):
        """The schedule is honoured exactly, and the attribution is in the key.

        This is the driver working as intended, and it is the comparison the
        local-source defect below is measured against.
        """
        fake = clock(advance=60)
        schedule = tmp_path / "schedule.csv"
        schedule.write_text("minute,count\n1,2\n2,3\n", encoding="utf-8")

        result = tester.run_scheduled_load(
            "s3://%s/%s" % (SOURCE_BUCKET, SOURCE_KEY),
            str(schedule),
            dest_prefix="sched",
            config_version="v0.6.9",
        )

        assert _keys(aws) == [
            "sched/lending_package_001_000001.pdf",
            "sched/lending_package_001_000002.pdf",
            "sched/lending_package_002_000003.pdf",
            "sched/lending_package_002_000004.pdf",
            "sched/lending_package_002_000005.pdf",
        ]
        assert result == {
            "success": True,
            "total_files": 5,
            "duration_seconds": 120.0,
            "planned_files": 5,
        }
        # Two passes: one per scheduled minute, then the loop breaks.
        assert fake.sleeps == [0.01, 0.01]

    def test_a_minute_absent_from_the_schedule_delivers_nothing(
        self, tester, aws, clock, tmp_path
    ):
        """A gap in the schedule is a quiet minute, not an error or a stop.

        ``schedule.get(minute, 0)`` is what makes a sparse CSV mean "idle then
        resume"; without the default the run would either raise or stop at the
        gap and never reach minute 3.
        """
        clock(advance=60)
        schedule = tmp_path / "schedule.csv"
        schedule.write_text("minute,count\n1,1\n3,1\n", encoding="utf-8")

        result = tester.run_scheduled_load(
            "s3://%s/%s" % (SOURCE_BUCKET, SOURCE_KEY),
            str(schedule),
            dest_prefix="sched",
        )

        assert _keys(aws) == [
            "sched/lending_package_001_000001.pdf",
            "sched/lending_package_003_000002.pdf",
        ]
        assert result["total_files"] == 2

    def test_a_local_source_ignores_the_schedule_and_floods_the_bucket(
        self, tester, aws, clock, tmp_path
    ):
        """DEFECT: a scheduled run from a local file honours no schedule at all.

        ``load_test.py:403-412`` submits ``_upload_local_file`` for the local case,
        and that function calls ``stats.increment()`` with no minute. So
        ``copies_by_minute[current_minute]`` is never incremented,
        ``stats.get_minute_copies(current_minute)`` at ``load_test.py:383`` stays 0
        forever, and the loop re-submits the *full* ``target`` every pass until the
        wall clock rolls into the next minute. The per-minute guard inside
        ``_copy_file`` that would otherwise catch this is not in the local path.

        Consequence: a schedule asking for 2 files in minute 1 uploads one batch of
        2 per 0.01s sleep for a whole minute — roughly 12,000 documents at real
        wall-clock speed — so a scheduled load test against a local corpus file
        measures nothing it was asked to measure and can cost real money on a
        deployed stack. Measured here with a 10-second simulated sleep, giving 6
        passes and 12 files for a planned 2.

        Pinned, not fixed: the fix is to pass the minute and target through to the
        local path, which is production code. If this test starts failing because
        the count dropped to 2, the defect has been fixed and the test should be
        rewritten as the positive assertion.
        """
        fake = clock(advance=10)
        source = tmp_path / "statement.pdf"
        source.write_bytes(b"%PDF")
        schedule = tmp_path / "schedule.csv"
        schedule.write_text("minute,count\n1,2\n", encoding="utf-8")

        result = tester.run_scheduled_load(
            str(source), str(schedule), dest_prefix="sched"
        )

        assert result["planned_files"] == 2
        assert result["total_files"] == 12
        assert len(_keys(aws)) == 12
        assert len(fake.sleeps) == 6
        # And the keys carry no minute at all, so the over-delivery is not even
        # attributable to a schedule row after the fact.
        assert _keys(aws)[0] == "sched/statement_000001.pdf"
