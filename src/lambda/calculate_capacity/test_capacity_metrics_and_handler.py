# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the three parts of the `calculate_capacity` Lambda that read AWS state:
`get_real_latency_metrics` (how long a document actually takes, derived from the
tracking table), `build_simple_quota_requirements` (how much Bedrock TPM and RPM the
plan needs, derived from recorded metering) and `lambda_handler` (the request shapes,
the aggregation and the error envelopes).

The function's whole purpose is to answer, from measured history rather than from
estimates, whether an account's quotas can carry a planned volume. Every number it
prints is acted on: an operator raises a Service Quotas increase, or decides they do
not need to. So the failure that matters is a number that is wrong but believable.
Three concrete ones this tier is built around: measuring end-to-end duration where
processing time was meant, which blames Bedrock for an SQS backlog; deriving a
per-minute request rate from a per-day total; and counting one Bedrock call per
document where several were made.

Four things shape these tests.

**DynamoDB is real (moto).** The timings are read back out of the tracking table, and
DynamoDB returns every number as a `Decimal`. A hand-built dictionary of floats
would never exercise the `convert_decimal_to_float` call that stands between the
stored value and the `gb_seconds / memory_gb` division — and without that conversion
the division raises, so the mocked version of this test would pass while the deployed
function failed on its first document.

**Timestamps are laid out so that the wrong subtraction gives a different answer.**
Queue delay is `WorkflowStartTime - QueuedTime` and processing time is
`CompletionTime - WorkflowStartTime`; the fixture documents use a 30-second queue wait
and a 120-second processing time, so the end-to-end 150 seconds that a naive
implementation would report is distinguishable from both.

**Each sanity window is asserted at both edges, and the two windows differ.** Queue
delay is accepted on `0 <= d < 86400` and processing time on `0 < d < 3600`, so a
document that completed within the same second as it started is silently dropped from
the processing sample but a zero-second queue wait is kept. Those are easy to write as
one shared range, and the consequence — a fast document contributing nothing to the
median — is invisible.

**Percentile selection is pinned by index, not by approximation.** The percentiles are
positional picks from a sorted list, and a plan's SLA verdict rests on them. Eight- and
ten-document samples are used so that `p50`, `p75` and `p90` each select a different
element, which is what distinguishes the positional pick from an interpolated one and
catches an off-by-one in either index. The accompanying small-sample fallbacks
(`if n > 3`, `n > 9`, `n > 19`, `n > 99`) are **not** separately testable and are noted
as such where they are exercised: `int(n * p)` for `p < 1` never exceeds `n - 1`, and
below each cutoff it lands exactly on `n - 1`, so removing a guard or moving a cutoff
changes no result at any sample size.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal

import index
import pytest
import validation
from moto import mock_aws

TRACKING_TABLE = "capacity-tracking"

CAPACITY_ENV = {
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "TRACKING_TABLE": TRACKING_TABLE,
    "METERING_TABLE_NAME": TRACKING_TABLE,
    "LAMBDA_MEMORY_GB": "2.0",
    "LATENCY_METRICS_HOURS": "24",
    "LATENCY_METRICS_MIN_DOCS": "5",
    "MIN_TOKENS_PER_REQUEST": "1500",
    "MEDIUM_COMPLEXITY_THRESHOLD": "800",
    "HIGH_COMPLEXITY_THRESHOLD": "2400",
    "PAGE_COMPLEXITY_FACTOR": "0.25",
    "HIGH_COMPLEXITY_MULTIPLIER": "3.0",
    "MEDIUM_COMPLEXITY_MULTIPLIER": "1.5",
    "RECOMMENDATION_HIGH_COMPLEXITY_THRESHOLD": "2.5",
    "RECOMMENDATION_MEDIUM_COMPLEXITY_THRESHOLD": "1.5",
    "RECOMMENDATION_HIGH_LOAD_THRESHOLD": "3.0",
    "RECOMMENDATION_MEDIUM_LOAD_THRESHOLD": "2.0",
    "RECOMMENDATION_HIGH_LATENCY_THRESHOLD": "300",
    "RECOMMENDATION_LARGE_DOC_THRESHOLD": "50000",
    "RECOMMENDATION_HIGH_PAGE_THRESHOLD": "20",
    "BEDROCK_MODEL_QUOTA_CODES": json.dumps({"us.amazon.nova-lite-v1:0": "L-TPM"}),
    "BEDROCK_MODEL_RPM_QUOTA_CODES": json.dumps({"us.amazon.nova-lite-v1:0": "L-RPM"}),
}


@pytest.fixture
def aws(monkeypatch):
    for name, value in CAPACITY_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(index, "_processing_times_cache", {})
    monkeypatch.setattr(index, "_cache_expiry", 0)
    monkeypatch.setattr(validation, "_validated_env_vars", None)
    with mock_aws():
        yield
    monkeypatch.setattr(index, "_processing_times_cache", {})
    monkeypatch.setattr(index, "_cache_expiry", 0)
    monkeypatch.setattr(validation, "_validated_env_vars", None)


@pytest.fixture
def tracking(aws):
    import boto3

    resource = boto3.resource("dynamodb")
    resource.create_table(
        TableName=TRACKING_TABLE,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return resource.Table(TRACKING_TABLE)


def ancient(days_ago=30):
    return (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def put_metered(table, key, metering, **attributes):
    """Store a document the way the tracking table really holds one.

    Numbers go in as `Decimal`, because that is what DynamoDB returns and the
    conversion on the way out is load-bearing.
    """
    item = {
        "PK": key,
        "Metering": json.loads(json.dumps(metering), parse_float=Decimal),
    }
    item.update(attributes)
    table.put_item(Item=item)


def timed_document(
    table, key, *, queued=None, started=None, completed=None, metering=None
):
    attributes = {}
    if queued is not None:
        attributes["QueuedTime"] = queued
    if started is not None:
        attributes["WorkflowStartTime"] = started
    if completed is not None:
        attributes["CompletionTime"] = completed
    put_metered(table, key, metering if metering is not None else {}, **attributes)


def durations(**steps):
    """A metering payload of `<Step>/lambda/duration` gb_seconds entries."""
    return {
        f"{step}/lambda/duration": {"gb_seconds": value}
        for step, value in steps.items()
    }


# ==========================================================================
# get_real_latency_metrics: converting recorded metering into seconds
# ==========================================================================


@pytest.mark.unit
def test_gb_seconds_are_divided_by_the_configured_lambda_memory(tracking):
    """`gb_seconds` is memory multiplied by wall time, so the memory must divide out.

    Reported straight through, a 61.6 GB-second OCR step on a 2 GB function looks
    like 61.6 seconds rather than 30.8 — every latency figure in the plan doubled,
    and an SLA that is met reported as breached. The value is stored as a `Decimal`
    by DynamoDB, so this also covers the conversion the division depends on.
    """
    put_metered(tracking, "doc-1", durations(OCR=61.6, Extraction=100.0))
    result = index.get_real_latency_metrics("pattern-2")

    assert result["base_times"]["ocr"] == pytest.approx(30.8)
    assert result["base_times"]["extraction"] == pytest.approx(50.0)
    assert result["base_times"]["classification"] == 0
    assert result["total_processing_time"] == pytest.approx(80.8)
    assert result["data_source"] == "real_lambda_durations"


@pytest.mark.unit
def test_the_per_step_estimate_is_the_upper_middle_sample_not_the_mean(tracking):
    """A single slow document must not drag the estimate up.

    Samples of 1, 5 and 50 seconds give 5 as the positional median; the mean would
    be 18.7 and the maximum 50. With an even count the upper of the two middle
    values is taken rather than their average, which is worth knowing before
    comparing two plans measured over different sample sizes.
    """
    for i, gb_seconds in enumerate([2.0, 10.0, 100.0]):
        put_metered(tracking, f"doc-{i}", durations(OCR=gb_seconds))
    assert index.get_real_latency_metrics("p")["base_times"]["ocr"] == pytest.approx(
        5.0
    )


@pytest.mark.unit
def test_an_even_sample_takes_the_upper_of_the_two_middle_values(tracking):
    for i, gb_seconds in enumerate([2.0, 10.0, 100.0, 200.0]):
        put_metered(tracking, f"doc-{i}", durations(OCR=gb_seconds))
    # [1, 5, 50, 100] -> index 2 -> 50.0, not the 27.5 midpoint.
    assert index.get_real_latency_metrics("p")["base_times"]["ocr"] == pytest.approx(
        50.0
    )


@pytest.mark.unit
def test_granular_assessment_time_is_counted_as_assessment_time(tracking):
    """Granular assessment replaces the assessment step rather than adding a step.

    Its metering lands under a different key, so a report that did not fold it in
    would show a zero-second assessment stage on exactly the configuration where
    assessment is most expensive.
    """
    put_metered(
        tracking,
        "doc-1",
        durations(Assessment=10.0, GranularAssessment=40.0, Summarization=6.0),
    )
    base = index.get_real_latency_metrics("p")["base_times"]
    # Two assessment samples, 5s and 20s; the positional median is the upper one.
    assert base["assessment"] == pytest.approx(20.0)
    assert base["summarization"] == pytest.approx(3.0)


@pytest.mark.unit
def test_metering_keys_that_are_not_lambda_durations_are_ignored(tracking):
    """Bedrock token counts share the payload and are not times."""
    metering = {
        "Extraction/bedrock/nova": {"inputTokens": 5000, "requests": 3},
        "Extraction/lambda/duration": {"gb_seconds": 20.0},
        "Extraction/lambda/invocations": {"count": 1},
    }
    put_metered(tracking, "doc-1", metering)
    base = index.get_real_latency_metrics("p")["base_times"]
    assert base["extraction"] == pytest.approx(10.0)


@pytest.mark.unit
def test_every_pipeline_stage_has_its_own_duration_bucket(tracking):
    """One stage routed to the wrong bucket is invisible in the total.

    The total is the sum, so misrouting classification into extraction keeps the
    plan's headline latency correct while the per-stage bars — the part an operator
    uses to decide what to optimise — point at the wrong step. Each value below is
    distinct so no two buckets can be swapped without failing.
    """
    put_metered(
        tracking,
        "doc-1",
        durations(
            OCR=2.0,
            Classification=8.0,
            Extraction=20.0,
            Assessment=40.0,
            Summarization=60.0,
        ),
    )
    base = index.get_real_latency_metrics("p")["base_times"]
    assert base == {
        "ocr": pytest.approx(1.0),
        "classification": pytest.approx(4.0),
        "extraction": pytest.approx(10.0),
        "assessment": pytest.approx(20.0),
        "summarization": pytest.approx(30.0),
    }


@pytest.mark.unit
def test_the_sample_is_paginated_rather_than_capped_at_one_page(tracking):
    """A hundred documents per scan page, and the estimate needs more than one.

    Stopping after the first page would silently narrow the sample to whatever
    DynamoDB happened to return first, so the median would depend on the table's
    internal ordering. 120 documents are stored, all with the same duration, and
    the reported sample count proves the second page was fetched.
    """
    now = datetime.utcnow()
    for i in range(120):
        timed_document(
            tracking,
            f"doc-{i:03d}",
            started=(now - timedelta(seconds=40)).strftime("%Y-%m-%dT%H:%M:%S"),
            completed=now.strftime("%Y-%m-%dT%H:%M:%S"),
            metering=durations(OCR=10.0),
        )
    result = index.get_real_latency_metrics("p")
    assert result["processing_time_percentiles"]["count"] == 120


@pytest.mark.unit
def test_a_metering_payload_stored_as_a_json_string_is_parsed(tracking):
    """Older documents hold the payload as a string rather than a map."""
    tracking.put_item(Item={"PK": "doc-1", "Metering": json.dumps(durations(OCR=12.0))})
    assert index.get_real_latency_metrics("p")["base_times"]["ocr"] == pytest.approx(
        6.0
    )


@pytest.mark.unit
def test_one_unparseable_payload_does_not_discard_the_rest_of_the_sample(tracking):
    """A single corrupt record must not make capacity planning unavailable."""
    tracking.put_item(Item={"PK": "doc-bad", "Metering": "{not json"})
    put_metered(tracking, "doc-good", durations(OCR=12.0))
    assert index.get_real_latency_metrics("p")["base_times"]["ocr"] == pytest.approx(
        6.0
    )


@pytest.mark.unit
def test_documents_recorded_under_the_older_attribute_name_are_still_found(tracking):
    """`meteringData` predates `Metering` and is scanned for as a fallback.

    Only when the first scan finds nothing, so a table holding both reports on the
    new name alone.
    """
    tracking.put_item(
        Item={
            "PK": "doc-1",
            "meteringData": json.loads(
                json.dumps(durations(OCR=30.0)), parse_float=Decimal
            ),
        }
    )
    assert index.get_real_latency_metrics("p")["base_times"]["ocr"] == pytest.approx(
        15.0
    )


# --------------------------------------------------------------------------
# Separating queue wait from processing time
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_queue_wait_and_processing_time_are_measured_separately(tracking):
    """The distinction that decides which lever an operator pulls.

    A document queued at t, started at t+30s and completed at t+150s waited 30
    seconds and processed for 120. Reporting the 150-second end-to-end figure as
    processing time blames Bedrock for an SQS backlog, and the operator raises a
    model quota when the fix was concurrency. All three numbers differ here, so
    neither substitution can pass.
    """
    now = datetime.utcnow()
    timed_document(
        tracking,
        "doc-1",
        queued=(now - timedelta(seconds=150)).strftime("%Y-%m-%dT%H:%M:%S"),
        started=(now - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%S"),
        completed=now.strftime("%Y-%m-%dT%H:%M:%S"),
        metering=durations(OCR=10.0),
    )
    result = index.get_real_latency_metrics("p")

    assert result["actual_queue_delays"]["p50"] == pytest.approx(30.0)
    assert result["total_processing_time"] == pytest.approx(120.0)
    assert result["processing_time_percentiles"]["p50"] == pytest.approx(120.0)
    assert result["data_source"] == "document_timestamps"


@pytest.mark.unit
def test_without_a_workflow_start_time_the_end_to_end_duration_is_used(tracking):
    """The fallback, and it is explicitly the figure that includes queue wait.

    Documents processed before `WorkflowStartTime` was recorded still have a
    duration worth measuring; it is just a pessimistic one.
    """
    now = datetime.utcnow()
    put_metered(
        tracking,
        "doc-1",
        durations(OCR=10.0),
        InitialEventTime=(now - timedelta(seconds=200)).strftime("%Y-%m-%dT%H:%M:%S"),
        CompletionTime=now.strftime("%Y-%m-%dT%H:%M:%S"),
    )
    result = index.get_real_latency_metrics("p")
    assert result["total_processing_time"] == pytest.approx(200.0)
    assert result["actual_queue_delays"] == {}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("delay_seconds", "kept"),
    [(0, True), (1, True), (86399, True), (86400, False), (-5, False)],
)
def test_the_queue_delay_sanity_window_accepts_zero_and_rejects_a_full_day(
    tracking, delay_seconds, kept
):
    """`0 <= delay < 86400`.

    A zero-second wait is real and common on an idle stack, so excluding it would
    bias the reported queue delay upwards. A day or more, or a negative value,
    means the two timestamps came from different clocks and would corrupt the
    median if kept.
    """
    now = datetime.utcnow()
    started = now - timedelta(seconds=1)
    timed_document(
        tracking,
        "doc-1",
        queued=(started - timedelta(seconds=delay_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        ),
        started=started.strftime("%Y-%m-%dT%H:%M:%S"),
        completed=now.strftime("%Y-%m-%dT%H:%M:%S"),
        metering=durations(OCR=10.0),
    )
    result = index.get_real_latency_metrics("p")
    assert bool(result["actual_queue_delays"]) is kept


@pytest.mark.unit
@pytest.mark.parametrize(
    ("processing_seconds", "kept"),
    [(1, True), (3599, True), (3600, False), (0, False)],
)
def test_the_processing_time_window_excludes_zero_unlike_the_queue_window(
    tracking, processing_seconds, kept
):
    """`0 < duration < 3600`, so a sub-second document is dropped entirely.

    Timestamps are stored to the second, so a document that starts and finishes
    inside one second reports zero and contributes nothing to the sample. On a fast
    single-page configuration that can discard most of the history, and the
    estimate then rests on the slow minority. The asymmetry with the queue window
    above is the point: the two ranges look interchangeable and are not.
    """
    now = datetime.utcnow()
    started = now - timedelta(seconds=processing_seconds)
    timed_document(
        tracking,
        "doc-1",
        started=started.strftime("%Y-%m-%dT%H:%M:%S"),
        completed=now.strftime("%Y-%m-%dT%H:%M:%S"),
        metering=durations(OCR=10.0),
    )
    result = index.get_real_latency_metrics("p")
    if kept:
        assert result["total_processing_time"] == pytest.approx(processing_seconds)
        assert result["data_source"] == "document_timestamps"
    else:
        # Falls back to the per-step sum from gb_seconds.
        assert result["total_processing_time"] == pytest.approx(5.0)
        assert result["data_source"] == "real_lambda_durations"


@pytest.mark.unit
@pytest.mark.parametrize(
    "formatter",
    [
        lambda d: d.strftime("%Y-%m-%dT%H:%M:%S"),
        lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ"),
        lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        lambda d: d.strftime("%Y-%m-%d %H:%M:%S"),
        lambda d: d.strftime("%Y-%m-%dT%H:%M:%S") + "+00:00",
    ],
)
def test_every_timestamp_format_the_pipeline_writes_is_understood(tracking, formatter):
    """Different steps write different shapes, and an unparsed one is silent.

    A format the parser misses is dropped with a log line, so a whole stage's
    documents can vanish from the sample while the report still looks successful.
    The 24-hour recency filter compares these as strings, so the space-separated
    form also has to survive that comparison.
    """
    now = datetime.utcnow().replace(microsecond=123000)
    timed_document(
        tracking,
        "doc-1",
        started=formatter(now - timedelta(seconds=90)),
        completed=formatter(now),
        metering=durations(OCR=10.0),
    )
    result = index.get_real_latency_metrics("p")
    assert result["total_processing_time"] == pytest.approx(90.0, abs=1.0)


@pytest.mark.unit
def test_mixing_an_offset_aware_and_a_naive_timestamp_drops_the_pair_quietly(tracking):
    """Subtracting one from the other raises, and the document is skipped.

    Pinned because the failure is per-document and logged only: a pipeline that
    started stamping one of the two fields with an offset would shrink the sample
    without any visible error.
    """
    now = datetime.utcnow()
    timed_document(
        tracking,
        "doc-1",
        started=(now - timedelta(seconds=90)).strftime("%Y-%m-%dT%H:%M:%S") + "+00:00",
        completed=now.strftime("%Y-%m-%dT%H:%M:%S"),
        metering=durations(OCR=10.0),
    )
    result = index.get_real_latency_metrics("p")
    assert result["processing_time_percentiles"] == {}
    assert result["total_processing_time"] == pytest.approx(5.0)


@pytest.mark.unit
def test_an_epoch_timestamp_read_from_dynamodb_is_not_recognised(tracking):
    """The numeric branch of the parser cannot be reached from the tracking table.

    It tests `isinstance(ts, (int, float))`, and DynamoDB returns every number as a
    `Decimal`, which is neither — and unlike the metering payload, timestamps are
    not passed through `convert_decimal_to_float` first. An epoch value therefore
    falls through to the string formats, matches none of them, and the document
    drops out of the processing sample with a log line. Pinned as current behaviour
    so the branch is not read as working support for epoch timestamps.
    """
    now = datetime.utcnow()
    put_metered(
        tracking,
        "doc-1",
        durations(OCR=10.0),
        CompletionTime=now.strftime("%Y-%m-%dT%H:%M:%S"),
        WorkflowStartTime=Decimal(str(int((now - timedelta(seconds=45)).timestamp()))),
    )
    result = index.get_real_latency_metrics("p")
    assert result["processing_time_percentiles"] == {}
    assert result["total_processing_time"] == pytest.approx(5.0)


@pytest.mark.unit
@pytest.mark.parametrize(
    "queued",
    [
        "not-a-timestamp",  # unrecognised: parses to None and is skipped
        "2026-01-01T00:00:00+00:00",  # recognised but offset-aware: subtraction raises
    ],
)
def test_an_unusable_queue_timestamp_drops_only_the_queue_measurement(tracking, queued):
    """The two measurements are independent, so one bad field must not lose both.

    Both failure shapes are covered because they take different paths: a value the
    parser does not recognise yields `None` and is skipped, while one it recognises
    as offset-aware reaches the subtraction and raises against a naive
    `WorkflowStartTime`. Either way the processing time either side of it is intact
    and still reported.
    """
    now = datetime.utcnow()
    timed_document(
        tracking,
        "doc-1",
        queued=queued,
        started=(now - timedelta(seconds=90)).strftime("%Y-%m-%dT%H:%M:%S"),
        completed=now.strftime("%Y-%m-%dT%H:%M:%S"),
        metering=durations(OCR=10.0),
    )
    result = index.get_real_latency_metrics("p")
    assert result["actual_queue_delays"] == {}
    assert result["total_processing_time"] == pytest.approx(90.0)


@pytest.mark.unit
def test_an_unparseable_end_to_end_pair_leaves_the_step_sum_as_the_estimate(tracking):
    """The fallback path has its own guard, exercised with a mixed-offset pair."""
    now = datetime.utcnow()
    put_metered(
        tracking,
        "doc-1",
        durations(OCR=10.0),
        InitialEventTime=(now - timedelta(seconds=200)).strftime("%Y-%m-%dT%H:%M:%S")
        + "+00:00",
        CompletionTime=now.strftime("%Y-%m-%dT%H:%M:%S"),
    )
    result = index.get_real_latency_metrics("p")
    assert result["processing_time_percentiles"] == {}
    assert result["total_processing_time"] == pytest.approx(5.0)


# --------------------------------------------------------------------------
# Percentiles
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_each_percentile_selects_its_own_sample_with_a_small_sample_fallback(tracking):
    """Ten documents at 10s..100s of processing time.

    `p50` is index 5, `p75` index 7 and `p90` index 9; `p95` and `p99` need more
    than 19 and 99 samples respectively and fall back to the maximum. Every
    expected value below is distinct except the two deliberate fallbacks, so an
    interpolating or off-by-one implementation cannot reproduce the set.
    """
    now = datetime.utcnow()
    for i, seconds in enumerate(range(10, 110, 10)):
        timed_document(
            tracking,
            f"doc-{i}",
            started=(now - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S"),
            completed=now.strftime("%Y-%m-%dT%H:%M:%S"),
            metering=durations(OCR=10.0),
        )
    percentiles = index.get_real_latency_metrics("p")["processing_time_percentiles"]
    assert percentiles["count"] == 10
    assert percentiles["p50"] == pytest.approx(60.0)
    assert percentiles["p75"] == pytest.approx(80.0)
    assert percentiles["p90"] == pytest.approx(100.0)
    assert percentiles["p95"] == pytest.approx(100.0)
    assert percentiles["p99"] == pytest.approx(100.0)


@pytest.mark.unit
def test_a_three_document_sample_reports_the_maximum_for_every_upper_percentile(
    tracking,
):
    """Three samples cannot describe a spread, so every upper percentile is the max.

    The four `if n > 3` / `n > 9` / `n > 19` / `n > 99` fallbacks in the code make no
    difference to the answer at any sample size: `int(n * p)` for `p < 1` never
    exceeds `n - 1`, and for every `n` below each cutoff it lands exactly on `n - 1`,
    which is what the fallback returns. The behaviour worth pinning is therefore the
    outcome — a small sample reports its maximum — rather than the guard, and the
    index formula itself is pinned by the eight-sample test below.
    """
    now = datetime.utcnow()
    for i, seconds in enumerate([20, 40, 90]):
        timed_document(
            tracking,
            f"doc-{i}",
            started=(now - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S"),
            completed=now.strftime("%Y-%m-%dT%H:%M:%S"),
            metering=durations(OCR=10.0),
        )
    percentiles = index.get_real_latency_metrics("p")["processing_time_percentiles"]
    assert percentiles["p50"] == pytest.approx(40.0)
    assert percentiles["p75"] == percentiles["p99"] == pytest.approx(90.0)


@pytest.mark.unit
def test_an_eight_sample_set_pins_the_percentile_index_formula(tracking):
    """Eight samples at 10s..80s, where three of the indices are interior.

    `int(8 * 0.75)` is 6 and `int(8 * 0.90)` is 7, so p75 is 70 and p90 is 80 while
    p50 is 50 — the only sample size in this file where p75 selects neither the
    middle nor the maximum. An off-by-one in either index, or a switch to
    interpolation, gives a different set.
    """
    now = datetime.utcnow()
    for i, seconds in enumerate(range(10, 90, 10)):
        timed_document(
            tracking,
            f"doc-{i}",
            started=(now - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S"),
            completed=now.strftime("%Y-%m-%dT%H:%M:%S"),
            metering=durations(OCR=10.0),
        )
    percentiles = index.get_real_latency_metrics("p")["processing_time_percentiles"]
    assert percentiles["count"] == 8
    assert percentiles["p50"] == pytest.approx(50.0)
    assert percentiles["p75"] == pytest.approx(70.0)
    assert percentiles["p90"] == pytest.approx(80.0)


@pytest.mark.unit
def test_queue_delay_percentiles_are_computed_the_same_way_as_processing_ones(tracking):
    now = datetime.utcnow()
    for i, delay in enumerate(range(1, 11)):
        started = now - timedelta(seconds=30)
        timed_document(
            tracking,
            f"doc-{i}",
            queued=(started - timedelta(seconds=delay)).strftime("%Y-%m-%dT%H:%M:%S"),
            started=started.strftime("%Y-%m-%dT%H:%M:%S"),
            completed=now.strftime("%Y-%m-%dT%H:%M:%S"),
            metering=durations(OCR=10.0),
        )
    delays = index.get_real_latency_metrics("p")["actual_queue_delays"]
    assert delays["count"] == 10
    assert delays["p50"] == pytest.approx(6.0)
    assert delays["p75"] == pytest.approx(8.0)
    assert delays["p90"] == pytest.approx(10.0)


# --------------------------------------------------------------------------
# The recency window
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_large_enough_recent_sample_excludes_older_documents(tracking):
    """Old outliers must not set today's estimate.

    Five recent documents at 20 seconds and five month-old ones at 600. With the
    filter applied the estimate is 20; without it the median of the combined set is
    600 — a thirty-fold error in the direction that makes a healthy account look
    like it is breaching its SLA.
    """
    now = datetime.utcnow()
    for i in range(5):
        timed_document(
            tracking,
            f"new-{i}",
            started=(now - timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%S"),
            completed=now.strftime("%Y-%m-%dT%H:%M:%S"),
            metering=durations(OCR=10.0),
        )
    old = datetime.utcnow() - timedelta(days=30)
    for i in range(5):
        timed_document(
            tracking,
            f"old-{i}",
            started=(old - timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%S"),
            completed=old.strftime("%Y-%m-%dT%H:%M:%S"),
            metering=durations(OCR=10.0),
        )
    result = index.get_real_latency_metrics("p")
    assert result["processing_time_percentiles"]["count"] == 5
    assert result["total_processing_time"] == pytest.approx(20.0)


@pytest.mark.unit
def test_too_few_recent_documents_falls_back_to_the_whole_history(tracking):
    """Four recent documents is under the configured minimum of five.

    A stale estimate beats no estimate, so the window widens rather than the report
    failing — but the whole history is then in scope, including the outliers the
    window exists to exclude.
    """
    now = datetime.utcnow()
    for i in range(4):
        timed_document(
            tracking,
            f"new-{i}",
            started=(now - timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%S"),
            completed=now.strftime("%Y-%m-%dT%H:%M:%S"),
            metering=durations(OCR=10.0),
        )
    old = datetime.utcnow() - timedelta(days=30)
    for i in range(5):
        timed_document(
            tracking,
            f"old-{i}",
            started=(old - timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%S"),
            completed=old.strftime("%Y-%m-%dT%H:%M:%S"),
            metering=durations(OCR=10.0),
        )
    result = index.get_real_latency_metrics("p")
    assert result["processing_time_percentiles"]["count"] == 9
    assert result["total_processing_time"] == pytest.approx(600.0)


@pytest.mark.unit
def test_exactly_the_configured_minimum_of_recent_documents_is_enough(tracking):
    """`>=`, so five recent documents keep the window rather than widening it."""
    now = datetime.utcnow()
    for i in range(5):
        timed_document(
            tracking,
            f"new-{i}",
            started=(now - timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%S"),
            completed=now.strftime("%Y-%m-%dT%H:%M:%S"),
            metering=durations(OCR=10.0),
        )
    timed_document(
        tracking,
        "old-1",
        started=ancient(30),
        completed=ancient(29),
        metering=durations(OCR=10.0),
    )
    assert (
        index.get_real_latency_metrics("p")["processing_time_percentiles"]["count"] == 5
    )


@pytest.mark.unit
def test_a_document_with_no_timestamp_at_all_is_outside_the_recent_window(tracking):
    """Undated documents cannot be shown to be recent, so they are excluded.

    Their `gb_seconds` still reach the per-step estimate via the widened fallback,
    which is how a table of undated documents still produces a plan.
    """
    put_metered(tracking, "doc-1", durations(OCR=30.0))
    result = index.get_real_latency_metrics("p")
    assert result["processing_time_percentiles"] == {}
    assert result["base_times"]["ocr"] == pytest.approx(15.0)


# --------------------------------------------------------------------------
# Reconciling the two sources of timing
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_step_times_are_rescaled_to_add_up_to_the_measured_document_time(tracking):
    """Two measurements of the same thing must not contradict each other on screen.

    The per-step breakdown comes from `gb_seconds` and the total from timestamps;
    the steps are scaled so the bars sum to the total. Here the steps add to 50
    seconds and the document took 200, so each is quadrupled. Without the scaling
    the UI shows a 200-second document whose stages account for 50 seconds.
    """
    now = datetime.utcnow()
    timed_document(
        tracking,
        "doc-1",
        started=(now - timedelta(seconds=200)).strftime("%Y-%m-%dT%H:%M:%S"),
        completed=now.strftime("%Y-%m-%dT%H:%M:%S"),
        metering=durations(OCR=20.0, Extraction=80.0),  # 10s + 40s at 2 GB
    )
    result = index.get_real_latency_metrics("p")

    assert result["total_processing_time"] == pytest.approx(200.0)
    assert sum(result["base_times"].values()) == pytest.approx(200.0)
    assert result["base_times"]["ocr"] == pytest.approx(40.0)
    assert result["base_times"]["extraction"] == pytest.approx(160.0)


@pytest.mark.unit
def test_documents_with_workflow_timestamps_but_no_step_metering_are_refused(tracking):
    """A measured document time is not enough on its own, despite what it says.

    The error text offers two alternatives — `/lambda/duration` gb_seconds *or*
    `WorkflowStartTime`/`CompletionTime` timestamps — but the zero-total check runs
    against the per-step sum only, before the timestamp total is consulted. A
    document with usable timestamps and an empty metering map therefore fails the
    whole report with advice that does not apply, and capacity planning is
    unavailable until some document records step durations. Pinned as the current
    behaviour so a change to it is deliberate.
    """
    now = datetime.utcnow()
    timed_document(
        tracking,
        "doc-1",
        started=(now - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%S"),
        completed=now.strftime("%Y-%m-%dT%H:%M:%S"),
        metering={"Extraction/bedrock/nova": {"requests": 3}},
    )
    with pytest.raises(ValueError, match="No processing time data found"):
        index.get_real_latency_metrics("p")


# --------------------------------------------------------------------------
# Caching and failure
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_second_request_for_the_same_pattern_is_served_from_the_cache(tracking):
    """One report asks for timings repeatedly; one scan per five minutes is enough.

    Proved by making the second scan impossible: the table is emptied between
    calls, so an uncached second call would raise "no processed documents".
    """
    put_metered(tracking, "doc-1", durations(OCR=61.6))
    first = index.get_real_latency_metrics("pattern-2")
    tracking.delete_item(Key={"PK": "doc-1"})
    second = index.get_real_latency_metrics("pattern-2")
    assert second == first


@pytest.mark.unit
def test_the_cache_is_scoped_to_one_pattern(tracking):
    """A different pattern measures different steps and must be measured again."""
    put_metered(tracking, "doc-1", durations(OCR=61.6))
    index.get_real_latency_metrics("pattern-2")
    tracking.delete_item(Key={"PK": "doc-1"})
    with pytest.raises(ValueError, match="No processed documents found"):
        index.get_real_latency_metrics("pattern-1")


@pytest.mark.unit
def test_an_expired_cache_is_not_served(tracking, monkeypatch):
    """Five minutes, so a plan made after new documents land sees them."""
    put_metered(tracking, "doc-1", durations(OCR=61.6))
    index.get_real_latency_metrics("pattern-2")
    monkeypatch.setattr(index, "_cache_expiry", 0)
    tracking.delete_item(Key={"PK": "doc-1"})
    with pytest.raises(ValueError, match="No processed documents found"):
        index.get_real_latency_metrics("pattern-2")


@pytest.mark.unit
def test_an_unset_tracking_table_is_reported_rather_than_guessed(aws, monkeypatch):
    monkeypatch.delenv("TRACKING_TABLE")
    with pytest.raises(ValueError, match="TRACKING_TABLE"):
        index.get_real_latency_metrics("p")


@pytest.mark.unit
def test_an_empty_history_asks_the_operator_to_process_documents_first(tracking):
    """There is no estimate to make, and inventing one is the hazard."""
    with pytest.raises(ValueError, match="Please process some documents first"):
        index.get_real_latency_metrics("p")


@pytest.mark.unit
def test_an_unset_lambda_memory_stops_the_conversion(tracking, monkeypatch):
    """Without it, `gb_seconds` cannot be turned into seconds at all.

    Defaulting to 1 GB would silently misreport every time on the stack's actual
    memory setting, which is the one value the conversion cannot guess.
    """
    monkeypatch.delenv("LAMBDA_MEMORY_GB")
    put_metered(tracking, "doc-1", durations(OCR=61.6))
    with pytest.raises(ValueError, match="LAMBDA_MEMORY_GB"):
        index.get_real_latency_metrics("p")


# ==========================================================================
# build_simple_quota_requirements
# ==========================================================================


def hours(**by_hour):
    """A 24-entry hourly breakdown, with the named hours overridden."""
    breakdown = []
    for hour in range(24):
        entry = {
            "hour": hour,
            "docsPerHour": 0,
            "pagesPerHour": 0,
            "tokensPerHour": 0,
            "ocrTokensPerHour": 0,
            "classificationTokensPerHour": 0,
            "extractionTokensPerHour": 0,
            "assessmentTokensPerHour": 0,
            "summarizationTokensPerHour": 0,
            "documentType": "No processing scheduled",
        }
        entry.update(by_hour.get(str(hour), {}))
        breakdown.append(entry)
    return breakdown


MODEL = "us.amazon.nova-lite-v1:0"


def model_config(**overrides):
    config = {
        "classification_model": "",
        "extraction_model": "",
        "assessment_model": "",
        "summarization_model": "",
        "ocr_model": "",
    }
    config.update(overrides)
    return config


def quota_set(tpm=100000, rpm=200, model=MODEL):
    return {
        "bedrock": tpm,
        "bedrock_models": {model: tpm},
        "bedrock_models_rpm": {model: rpm},
    }


def build(hourly, *, config=None, quotas=None, granular=True, pattern="pattern-2"):
    return index.build_simple_quota_requirements(
        1000,
        50000,
        100,
        quotas or quota_set(),
        600.0,
        pattern,
        config or model_config(extraction_model=MODEL),
        hourly,
        None,
        None,
        granular,
    )


def bedrock(step, requests, model="nova"):
    return {f"{step}/bedrock/{model}": {"requests": requests}}


def by_type(requirements):
    return {(r["usedFor"], r["quotaType"]): r for r in requirements}


@pytest.mark.unit
def test_the_token_requirement_is_the_busiest_hour_plus_a_ten_percent_buffer(tracking):
    """Peak, not total, and not average.

    An account has to survive its busiest hour. 132,000 extraction tokens in hour
    10 is 2,200 a minute with the 10% buffer applied; summing the two scheduled
    hours would ask for 3,300 and averaging across the day for 92. Every one of
    those is a different quota request.
    """
    put_metered(tracking, "doc-1", bedrock("Extraction", 4))
    hourly = hours(
        **{
            "9": {"docsPerHour": 60, "extractionTokensPerHour": 66000},
            "10": {"docsPerHour": 120, "extractionTokensPerHour": 132000},
        }
    )
    requirements = by_type(build(hourly))
    assert requirements[("Extraction", "TPM")]["requiredQuota"] == "2,420"


@pytest.mark.unit
def test_an_hourly_breakdown_missing_the_ocr_key_is_tolerated_but_not_the_others(
    tracking,
):
    """`ocrTokensPerHour` is read with a default; the other four are not.

    A caller assembling the breakdown by hand — the only way to call this function
    other than through the handler — gets a `KeyError` for four of the five stages
    and a silent zero for the fifth. Pinned so the asymmetry is visible rather than
    discovered from a Lambda traceback.
    """
    put_metered(tracking, "doc-1", bedrock("Extraction", 4))
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    for entry in hourly:
        del entry["ocrTokensPerHour"]
    assert build(hourly)  # tolerated

    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    for entry in hourly:
        del entry["assessmentTokensPerHour"]
    with pytest.raises(KeyError, match="assessmentTokensPerHour"):
        build(hourly)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tokens_per_hour", "status", "status_text"),
    [
        (5_000_000, "success", "✅ Sufficient"),
        (6_000_000, "warning", "⚠️ Increase Needed"),
    ],
)
def test_a_shortfall_and_a_fit_are_labelled_differently(
    tracking, tokens_per_hour, status, status_text
):
    """5M tokens an hour fits inside 100,000 TPM with the buffer; 6M does not.

    Both directions, so the verdict cannot pass by always answering one of them.
    """
    put_metered(tracking, "doc-1", bedrock("Extraction", 4))
    hourly = hours(
        **{"9": {"docsPerHour": 60, "extractionTokensPerHour": tokens_per_hour}}
    )
    requirement = by_type(build(hourly))[("Extraction", "TPM")]
    assert requirement["status"] == status
    assert requirement["statusText"] == status_text


@pytest.mark.unit
def test_demand_exactly_equal_to_the_quota_is_sufficient_not_a_shortfall(tracking):
    """The `<=` boundary, hit exactly rather than approached.

    The required TPM is `tokens / 60 * 1.1`, which is not a round number for any
    round token count, so the quota is set to that exact value instead of the
    demand being tuned to a round quota. That makes the comparison land precisely on
    equality, which is the only input that can tell `<=` from `<` — and a `<` here
    would send an operator to raise a quota they already have exactly enough of.

    Deriving the quota from the demand is deliberate and safe: the arithmetic that
    produces the demand is pinned by the peak-hour and buffer tests above, so the
    only thing this case can be reading is the comparison operator.
    """
    tokens_per_hour = 5_000_000
    exact_required_tpm = tokens_per_hour / 60 * 1.1
    put_metered(tracking, "doc-1", bedrock("Extraction", 4))
    hourly = hours(
        **{"9": {"docsPerHour": 60, "extractionTokensPerHour": tokens_per_hour}}
    )
    requirement = by_type(build(hourly, quotas=quota_set(tpm=exact_required_tpm)))[
        ("Extraction", "TPM")
    ]
    assert requirement["status"] == "success"
    assert requirement["statusText"] == "✅ Sufficient"
    assert requirement["utilizationPercent"] == 100


@pytest.mark.unit
def test_a_request_rate_exactly_equal_to_the_rpm_quota_is_sufficient(tracking):
    """The same boundary on the request side, which is a separate comparison.

    Two `<=` operators, two rows in the report, and a fix applied to one has to be
    applied to the other.
    """
    put_metered(tracking, "doc-1", bedrock("Extraction", 4))
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    exact_required_rpm = 4 * 60 / 60 * 1.1
    requirement = by_type(build(hourly, quotas=quota_set(rpm=exact_required_rpm)))[
        ("Extraction", "RPM")
    ]
    assert requirement["status"] == "success"
    assert requirement["statusText"] == "✅ Sufficient"
    assert requirement["utilizationPercent"] == 100


@pytest.mark.unit
def test_utilization_is_capped_at_one_hundred_percent(tracking):
    """The gauge saturates; `requiredQuota` is where the true shortfall shows.

    Worth knowing when reading the report: a plan needing five times its quota and
    one needing 1.01 times both render as a full bar.
    """
    put_metered(tracking, "doc-1", bedrock("Extraction", 4))
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 30_000_000}})
    requirement = by_type(build(hourly))[("Extraction", "TPM")]
    assert requirement["utilizationPercent"] == 100
    assert requirement["requiredQuota"] == "550,000"
    assert requirement["currentQuota"] == "100,000"


@pytest.mark.unit
def test_the_requirement_names_the_model_readably_and_keeps_the_full_id(tracking):
    """The UI groups by `modelId`; the operator reads `service`."""
    put_metered(tracking, "doc-1", bedrock("Extraction", 4))
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    requirement = by_type(build(hourly))[("Extraction", "TPM")]
    assert requirement["service"] == "Extraction (nova-lite-v1) - TPM"
    assert requirement["modelId"] == MODEL
    assert requirement["category"] == "Bedrock Models TPM"


@pytest.mark.unit
def test_a_one_million_context_variant_is_costed_against_its_base_models_quota(
    tracking,
):
    """The `:1m` marker shares a Service Quotas entry with the base model.

    Failing to fall back would report "TPM quota not available" for a model the
    account can use, so the whole plan would fail rather than one row.
    """
    put_metered(tracking, "doc-1", bedrock("Extraction", 4))
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    variant = "us.anthropic.claude-sonnet-4-5-20250929-v1:1m"
    quotas = {
        "bedrock": 400000,
        "bedrock_models": {"us.anthropic.claude-sonnet-4-5-20250929-v1": 400000},
        "bedrock_models_rpm": {"us.anthropic.claude-sonnet-4-5-20250929-v1:0": 250},
    }
    requirements = by_type(
        build(hourly, config=model_config(extraction_model=variant), quotas=quotas)
    )
    assert requirements[("Extraction", "TPM")]["currentQuota"] == "400,000"
    assert requirements[("Extraction", "RPM")]["currentQuota"] == "250"


@pytest.mark.unit
@pytest.mark.parametrize("missing", ["bedrock_models", "bedrock_models_rpm"])
def test_a_model_with_no_configured_quota_code_stops_the_report(tracking, missing):
    """Better a named failure than a row costed against a guessed limit."""
    put_metered(tracking, "doc-1", bedrock("Extraction", 4))
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    quotas = quota_set()
    quotas[missing] = {}
    expected = (
        "TPM quota not available"
        if missing == "bedrock_models"
        else "RPM quota not available"
    )
    with pytest.raises(ValueError, match=expected):
        build(hourly, quotas=quotas)


# --------------------------------------------------------------------------
# Requests per minute, derived from recorded metering
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_requests_per_document_is_averaged_across_the_sampled_documents(tracking):
    """Two documents at 2 and 8 Bedrock calls average 5, not 10 and not 2.

    This average multiplies straight into the RPM requirement, so a sum would
    overstate it by the sample size and picking the first document would make the
    answer depend on scan order.
    """
    put_metered(tracking, "doc-1", bedrock("Extraction", 2))
    put_metered(tracking, "doc-2", bedrock("Extraction", 8))
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    requirement = by_type(build(hourly))[("Extraction", "RPM")]
    # 5 requests/doc x 60 docs / 60 minutes x 1.1 buffer = 5.5 -> 6 (rounded)
    assert requirement["requiredQuota"] == "6"


@pytest.mark.unit
def test_only_the_first_matching_metering_key_per_document_is_counted(tracking):
    """A step that issues several Bedrock calls under distinct keys is undercounted.

    Agentic extraction records one entry per model or tool invocation, so a document
    can carry `Extraction/bedrock/<model-a>` and `Extraction/bedrock/<model-b>`. The
    scan stops at the first match per document, so 3 + 5 calls are counted as 3 and
    the RPM requirement comes out 62% low — the direction that reports a quota as
    sufficient when it is not. Pinned as current behaviour.
    """
    metering = {}
    metering.update(bedrock("Extraction", 3, model="model-a"))
    metering.update(bedrock("Extraction", 5, model="model-b"))
    put_metered(tracking, "doc-1", metering)
    hourly = hours(**{"9": {"docsPerHour": 600, "extractionTokensPerHour": 60000}})
    requirement = by_type(build(hourly))[("Extraction", "RPM")]
    # 3 req/doc x 600 docs / 60 x 1.1 = 33; all 8 would give 88.
    assert requirement["requiredQuota"] == "33"


@pytest.mark.unit
def test_assessment_sums_every_matching_entry_unlike_the_other_steps(tracking):
    """Assessment accumulates across keys, which is what granular assessment needs.

    2 regular plus 8 granular calls is 10 per document. The contrast with the
    extraction case above is deliberate: the two code paths count differently and
    only one of them is right for a multi-call step.
    """
    metering = {}
    metering.update(bedrock("Assessment", 2))
    metering.update(bedrock("GranularAssessment", 8))
    put_metered(tracking, "doc-1", metering)
    hourly = hours(**{"9": {"docsPerHour": 60, "assessmentTokensPerHour": 60000}})
    requirement = by_type(build(hourly, config=model_config(assessment_model=MODEL)))[
        ("Assessment", "RPM")
    ]
    # 10 req/doc x 60 docs / 60 x 1.1 = 11
    assert requirement["requiredQuota"] == "11"


@pytest.mark.unit
def test_disabling_granular_assessment_excludes_its_recorded_requests(tracking):
    """The flag's only job, and it changes a quota requirement by 5x here.

    With granular assessment off the plan needs the 2 regular calls a document, not
    all 10. Ignoring the flag would keep advising a quota increase for work the
    configuration has turned off.
    """
    metering = {}
    metering.update(bedrock("Assessment", 2))
    metering.update(bedrock("GranularAssessment", 8))
    put_metered(tracking, "doc-1", metering)
    hourly = hours(**{"9": {"docsPerHour": 60, "assessmentTokensPerHour": 60000}})
    requirement = by_type(
        build(hourly, config=model_config(assessment_model=MODEL), granular=False)
    )[("Assessment", "RPM")]
    # 2 req/doc x 60 docs / 60 x 1.1 = 2.2 -> 2
    assert requirement["requiredQuota"] == "2"


@pytest.mark.unit
def test_turning_off_granular_assessment_on_an_all_granular_history_fails_the_report(
    tracking,
):
    """A history recorded entirely under granular keys leaves nothing to count.

    The step then has demand but no request data, which is an unconditional failure
    of the whole report — not a skipped row — with a message telling the operator to
    process more documents. Pinned because the trigger is a configuration change
    rather than anything about the documents.
    """
    put_metered(tracking, "doc-1", bedrock("GranularAssessment", 8))
    hourly = hours(**{"9": {"docsPerHour": 60, "assessmentTokensPerHour": 60000}})
    with pytest.raises(ValueError, match="No request count data found for Assessment"):
        build(hourly, config=model_config(assessment_model=MODEL), granular=False)


@pytest.mark.unit
def test_the_request_rate_is_derived_from_the_whole_days_volume_not_the_peak_hour(
    tracking,
):
    """The TPM and RPM halves of one row are scaled from different windows.

    `requiredQuota` for TPM comes from the busiest hour; for RPM it comes from the
    sum of all 24 hours divided by 60. Two schedules with the same 240-document day
    therefore agree on RPM and differ 24-fold on TPM: concentrating the whole day
    into one hour changes the token requirement and not the request requirement,
    although both limits are per-minute limits.

    For a load spread evenly across the day that makes the RPM figure 24 times the
    rate the account will actually see, so the report asks for a request-quota
    increase that the workload does not need. Asserted as the current behaviour of
    both halves.
    """
    put_metered(tracking, "doc-1", bedrock("Extraction", 1))
    concentrated = hours(
        **{"9": {"docsPerHour": 240, "extractionTokensPerHour": 240 * 6000}}
    )
    spread = hours(
        **{
            str(h): {"docsPerHour": 10, "extractionTokensPerHour": 10 * 6000}
            for h in range(24)
        }
    )

    peak = by_type(build(concentrated))
    flat = by_type(build(spread))

    assert peak[("Extraction", "RPM")]["requiredQuota"] == "4"
    assert flat[("Extraction", "RPM")]["requiredQuota"] == "4"
    assert peak[("Extraction", "TPM")]["requiredQuota"] == "26,400"
    assert flat[("Extraction", "TPM")]["requiredQuota"] == "1,100"


@pytest.mark.unit
def test_a_step_with_demand_but_no_recorded_requests_fails_loudly(tracking):
    """No estimate is substituted for a missing measurement."""
    put_metered(tracking, "doc-1", bedrock("Classification", 3))
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    with pytest.raises(ValueError, match="No request count data found for Extraction"):
        build(hourly)


@pytest.mark.unit
def test_a_configured_step_nobody_is_using_is_dropped_quietly(tracking):
    """Zero tokens and no metering means the step is not in the pipeline."""
    put_metered(tracking, "doc-1", bedrock("Extraction", 3))
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    requirements = by_type(
        build(
            hourly,
            config=model_config(extraction_model=MODEL, summarization_model=MODEL),
        )
    )
    assert ("Extraction", "TPM") in requirements
    assert ("Summarization", "TPM") not in requirements


@pytest.mark.unit
def test_a_step_with_neither_demand_nor_recorded_requests_is_skipped_quietly(tracking):
    """Zero configured tokens and nothing in metering means the step is not running.

    This is the path a Textract-based deployment takes for OCR: no Bedrock tokens
    are configured because OCR is not a Bedrock step, so no row is produced.
    """
    put_metered(tracking, "doc-1", bedrock("Extraction", 3))
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    requirements = by_type(
        build(hourly, config=model_config(extraction_model=MODEL, ocr_model=MODEL))
    )
    assert ("OCR", "TPM") not in requirements


@pytest.mark.unit
def test_historical_metering_revives_a_step_that_has_no_configured_demand(tracking):
    """Recorded requests alone are enough to put a step back in the report.

    OCR has no configured token demand here — the deployment uses Textract — but an
    earlier Bedrock-OCR run left metering behind, and that is sufficient for the
    inclusion test (`peak_tpm > 0 or peak_rpm > 1.0`). The row that appears reads
    "✅ No Demand" for TPM while asking for real RPM headroom for a step that is not
    running. Pinned as current behaviour: the report's own two halves disagree
    about whether the step exists.
    """
    metering = {}
    metering.update(bedrock("Extraction", 3))
    metering.update(bedrock("OCR", 2))
    put_metered(tracking, "doc-1", metering)
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    requirements = by_type(
        build(hourly, config=model_config(extraction_model=MODEL, ocr_model=MODEL))
    )
    assert requirements[("OCR", "TPM")]["requiredQuota"] == "0"
    assert requirements[("OCR", "TPM")]["statusText"] == "✅ No Demand"
    assert requirements[("OCR", "TPM")]["utilizationPercent"] == 0
    assert requirements[("OCR", "RPM")]["requiredQuota"] == "2"


@pytest.mark.unit
def test_a_whitespace_only_ocr_model_is_treated_as_unconfigured(tracking):
    put_metered(tracking, "doc-1", bedrock("Extraction", 3))
    hourly = hours(
        **{
            "9": {
                "docsPerHour": 60,
                "extractionTokensPerHour": 60000,
                "ocrTokensPerHour": 60000,
            }
        }
    )
    requirements = by_type(
        build(hourly, config=model_config(extraction_model=MODEL, ocr_model="   "))
    )
    assert ("OCR", "TPM") not in requirements


@pytest.mark.unit
def test_a_configured_ocr_model_with_demand_is_planned_for(tracking):
    metering = {}
    metering.update(bedrock("Extraction", 3))
    metering.update(bedrock("OCR", 2))
    put_metered(tracking, "doc-1", metering)
    hourly = hours(
        **{
            "9": {
                "docsPerHour": 60,
                "extractionTokensPerHour": 60000,
                "ocrTokensPerHour": 120000,
            }
        }
    )
    requirements = by_type(
        build(
            hourly,
            config=model_config(extraction_model=MODEL, ocr_model=f"  {MODEL}  "),
        )
    )
    assert requirements[("OCR", "TPM")]["requiredQuota"] == "2,200"


@pytest.mark.unit
def test_a_step_below_one_request_a_minute_disappears_from_the_report(tracking):
    """`peak_tpm > 0 or peak_rpm > 1.0`, so a low-volume step is dropped silently.

    Assessment here has recorded Bedrock calls and no configured token demand, and
    its request rate works out below one a minute, so no row is produced at all —
    not a zero row. An operator reading the report cannot tell the step from one
    that is switched off.
    """
    metering = {}
    metering.update(bedrock("Extraction", 3))
    metering.update(bedrock("Assessment", 1))
    put_metered(tracking, "doc-1", metering)
    hourly = hours(**{"9": {"docsPerHour": 10, "extractionTokensPerHour": 60000}})
    requirements = by_type(
        build(
            hourly, config=model_config(extraction_model=MODEL, assessment_model=MODEL)
        )
    )
    assert ("Extraction", "TPM") in requirements
    assert ("Assessment", "TPM") not in requirements


@pytest.mark.unit
def test_metering_stored_as_a_json_string_is_parsed_for_the_request_count(tracking):
    """The same two storage shapes the timing scan handles, on the request path."""
    tracking.put_item(
        Item={"PK": "doc-1", "Metering": json.dumps(bedrock("Extraction", 4))}
    )
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    # 4 req/doc x 60 docs / 60 x 1.1 = 4.4 -> 4
    assert by_type(build(hourly))[("Extraction", "RPM")]["requiredQuota"] == "4"


@pytest.mark.unit
def test_the_request_sample_is_paginated_and_capped_at_a_hundred_documents(
    tracking, capsys
):
    """Fifty records a page, two pages, and then it stops.

    Both halves matter. Reading one page would make the average
    requests-per-document depend on DynamoDB's scan order rather than on the
    workload. And the hundred-document cap means a long history is *not* fully
    represented, so a step whose request count changed recently is averaged against
    an arbitrary hundred of its predecessors — worth knowing before trusting the
    RPM figure on a busy stack.
    """
    for i in range(120):
        put_metered(tracking, f"doc-{i:03d}", bedrock("Extraction", 4))
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    build(hourly)
    printed = capsys.readouterr().out
    assert "Found 100 metering records (from 2 pages)" in printed
    assert "Average 4.0 requests/doc from 100 documents" in printed


@pytest.mark.unit
def test_an_unconfigured_metering_table_leaves_the_report_without_request_data(
    tracking, monkeypatch
):
    monkeypatch.delenv("METERING_TABLE_NAME")
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    with pytest.raises(ValueError, match="No request count data found"):
        build(hourly)


@pytest.mark.unit
def test_a_metering_table_that_cannot_be_read_does_not_crash_the_scan(aws, monkeypatch):
    """The table is missing entirely; the read is caught and the step then fails.

    The distinction matters for diagnosis: the operator sees "no request count
    data", which is the same message a genuinely empty history produces, so the
    log line naming the underlying error is the only way to tell them apart.
    """
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    with pytest.raises(ValueError, match="No request count data found"):
        build(hourly)


@pytest.mark.unit
def test_the_reported_page_count_is_a_decaying_average_not_the_mean(tracking, capsys):
    """`(running + next) / 2` weights the last document far above the first.

    Page counts of 1, 10 and 100 give 52.8 rather than the true mean of 37, because
    each step halves the weight of everything before it. The figure is only logged,
    so the consequence is an operator reading a page count that does not match
    their documents while diagnosing a plan.
    """
    for i, pages in enumerate([1, 10, 100]):
        put_metered(
            tracking,
            f"doc-{i}",
            bedrock("Extraction", 3),
            number_of_pages=Decimal(pages),
        )
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    build(hourly)
    printed = capsys.readouterr().out
    assert "Actual pages per document from metering: 52.8" in printed
    assert "37.0" not in printed


@pytest.mark.unit
@pytest.mark.parametrize(
    ("pattern", "expected_steps"),
    [
        ("pattern-1", {"Summarization"}),
        ("pattern-2", {"Classification", "Extraction", "Assessment", "Summarization"}),
        ("pattern-3", {"Extraction", "Assessment", "Summarization"}),
    ],
)
def test_each_pattern_plans_for_only_the_bedrock_steps_it_runs(
    tracking, pattern, expected_steps
):
    """Pattern 1 does everything but summarization inside BDA; pattern 3 classifies
    on SageMaker. Planning Bedrock quota for a step the pattern does not run would
    inflate the request by a whole model's worth.
    """
    metering = {}
    for step in ("Classification", "Extraction", "Assessment", "Summarization"):
        metering.update(bedrock(step, 2))
    put_metered(tracking, "doc-1", metering)
    hourly = hours(
        **{
            "9": {
                "docsPerHour": 60,
                "classificationTokensPerHour": 60000,
                "extractionTokensPerHour": 60000,
                "assessmentTokensPerHour": 60000,
                "summarizationTokensPerHour": 60000,
            }
        }
    )
    config = model_config(
        classification_model=MODEL,
        extraction_model=MODEL,
        assessment_model=MODEL,
        summarization_model=MODEL,
    )
    requirements = build(hourly, config=config, pattern=pattern)
    assert all(r["modelId"] == MODEL for r in requirements)
    assert {r["usedFor"] for r in requirements} == expected_steps
    # Every included step gets both a TPM and an RPM row.
    assert len(requirements) == 2 * len(expected_steps)


@pytest.mark.unit
def test_a_pattern_with_no_models_configured_produces_no_requirements(tracking):
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    assert build(hourly, config=model_config()) == []


@pytest.mark.unit
def test_a_sagemaker_pattern_still_plans_for_a_bedrock_ocr_model(tracking):
    """Pattern 3 classifies on SageMaker but can read pages with a Bedrock model."""
    metering = {}
    metering.update(bedrock("Extraction", 3))
    metering.update(bedrock("OCR", 2))
    put_metered(tracking, "doc-1", metering)
    hourly = hours(
        **{
            "9": {
                "docsPerHour": 60,
                "extractionTokensPerHour": 60000,
                "ocrTokensPerHour": 120000,
            }
        }
    )
    requirements = by_type(
        build(
            hourly,
            config=model_config(extraction_model=MODEL, ocr_model=MODEL),
            pattern="pattern-3",
        )
    )
    assert requirements[("OCR", "TPM")]["requiredQuota"] == "2,200"
    assert ("Classification", "TPM") not in requirements


# ==========================================================================
# lambda_handler
# ==========================================================================


HAPPY_INPUT = {
    "pattern": "pattern-2",
    "maxAllowedLatency": 600,
    "documentConfigs": [
        {
            "type": "invoice",
            "docsPerHour": 60,
            "avgPages": 3,
            "ocrTokens": 0,
            "classificationTokens": 1000,
            "extractionTokens": 4000,
            "assessmentTokens": 1000,
            "summarizationTokens": 0,
        }
    ],
    "timeSlots": [{"hour": 9, "documentType": "invoice", "docsPerHour": 60}],
    "userConfig": json.dumps(
        {
            "classification_model": MODEL,
            "extraction_model": MODEL,
            "assessment_model": MODEL,
        }
    ),
}


@pytest.fixture
def wired(tracking, monkeypatch):
    """A handler whose Service Quotas and timing lookups are supplied, not live."""
    monkeypatch.setattr(index, "get_simple_quotas", lambda: quota_set())
    monkeypatch.setattr(
        index,
        "get_real_latency_metrics",
        lambda _p: {
            "base_times": {"ocr": 10.0, "extraction": 20.0},
            "total_processing_time": 30.0,
            "processing_time_percentiles": {},
            "actual_queue_delays": {},
            "variance_factor": 1.2,
            "data_source": "document_timestamps",
        },
    )
    for step in ("Classification", "Extraction", "Assessment"):
        put_metered(tracking, f"doc-{step}", bedrock(step, 2))
    return tracking


@pytest.mark.unit
def test_a_direct_invocation_returns_a_plan_with_totals_from_the_document_configs(
    wired,
):
    """The headline metrics, which are what the operator reads first.

    60 invoices an hour at 3 pages and 6,000 tokens each is 180 pages and 0.36M
    tokens. The token figure is formatted in millions to two places, so a
    thousand-fold unit error would be visible as `0.00M`.
    """
    result = index.lambda_handler(dict(HAPPY_INPUT), None)

    assert result["success"] is True
    assert result["errorMessage"] is None
    assert result["metrics"] == [
        {"label": "Total Docs", "value": "60"},
        {"label": "Total Pages", "value": "180"},
        {"label": "Total Tokens", "value": "0.36M"},
    ]
    assert result["calculationDetails"]["quotasUsed"]["bedrock_models"] == {
        MODEL: 100000
    }
    assert {r["usedFor"] for r in result["quotaRequirements"]} == {
        "Classification",
        "Extraction",
        "Assessment",
    }
    assert result["recommendations"]


@pytest.mark.unit
def test_large_totals_are_formatted_with_thousands_separators(wired):
    payload = json.loads(json.dumps(HAPPY_INPUT))
    payload["documentConfigs"][0]["docsPerHour"] = 2500
    payload["timeSlots"] = [{"hour": 9, "documentType": "invoice", "docsPerHour": 2500}]
    result = index.lambda_handler(payload, None)
    assert result["metrics"][0]["value"] == "2,500"
    assert result["metrics"][1]["value"] == "7,500"
    assert result["metrics"][2]["value"] == "15.00M"


@pytest.mark.unit
def test_the_unified_pattern_is_planned_as_the_bedrock_pipeline(wired):
    """`unified` replaced `pattern-2` and runs the same Bedrock steps.

    Rejecting it would make the planner unusable on every current deployment, so
    the rewrite is asserted through its consequence: a Classification requirement,
    which only the pipeline pattern produces.
    """
    payload = json.loads(json.dumps(HAPPY_INPUT))
    payload["pattern"] = "unified"
    result = index.lambda_handler(payload, None)
    assert result["success"] is True
    assert "Classification" in {r["usedFor"] for r in result["quotaRequirements"]}
    assert result["latencyDistribution"]["pattern"] == "pattern-2"


@pytest.mark.unit
def test_an_uppercase_pattern_name_is_rejected_before_it_can_be_normalized(wired):
    """The lowercasing step is unreachable: validation runs first and refuses it.

    `validate_capacity_input` accepts only `pattern-2` and `unified`, so a caller
    sending `PATTERN-2` — the spelling the normalization exists for — gets a
    validation error rather than being normalized. Pinned so the dead branch is not
    mistaken for working tolerance.
    """
    payload = json.loads(json.dumps(HAPPY_INPUT))
    payload["pattern"] = "PATTERN-2"
    result = index.lambda_handler(payload, None)
    assert result["success"] is False
    assert "Only pattern-2 and unified patterns are supported" in result["errorMessage"]


@pytest.mark.unit
def test_the_schedule_and_the_volume_totals_come_from_different_inputs(wired):
    """`documentConfigs.docsPerHour` sets the totals; `timeSlots` sets the peaks.

    The two can disagree, and nothing reconciles them: here the schedule says 600
    documents in hour 9 while the document config says 60 an hour. The headline
    metrics follow the config and the peak quota requirement follows the schedule,
    so the report describes two different workloads at once.
    """
    payload = json.loads(json.dumps(HAPPY_INPUT))
    payload["timeSlots"] = [{"hour": 9, "documentType": "invoice", "docsPerHour": 600}]
    result = index.lambda_handler(payload, None)

    assert result["metrics"][0]["value"] == "60"
    extraction_tpm = by_type(result["quotaRequirements"])[("Extraction", "TPM")]
    # 600 docs x 4,000 extraction tokens / 60 x 1.1 buffer = 44,000
    assert extraction_tpm["requiredQuota"] == "44,000"


@pytest.mark.unit
def test_time_slots_supplied_as_a_json_string_are_parsed(wired):
    payload = json.loads(json.dumps(HAPPY_INPUT))
    payload["timeSlots"] = json.dumps(
        [{"hour": 9, "documentType": "invoice", "docsPerHour": 600}]
    )
    result = index.lambda_handler(payload, None)
    assert (
        by_type(result["quotaRequirements"])[("Extraction", "TPM")]["requiredQuota"]
        == "44,000"
    )


@pytest.mark.unit
def test_unparseable_time_slots_are_refused_by_validation_not_silently_dropped(wired):
    """Validation rejects the schedule before the handler's own fallback can run.

    The handler carries a `json.JSONDecodeError` branch that logs and continues with
    an empty schedule, which would produce a successful report describing no load
    at all. It is unreachable: `validate_capacity_input` checks the same string
    first. The loud answer is the better one, and pinning it keeps the quiet
    fallback from becoming reachable unnoticed.
    """
    payload = json.loads(json.dumps(HAPPY_INPUT))
    payload["timeSlots"] = "{not json"
    result = index.lambda_handler(payload, None)
    assert result["success"] is False
    assert "timeSlots must be valid JSON" in result["errorMessage"]


@pytest.mark.unit
def test_a_scheduled_document_type_with_no_page_count_fails_the_plan(wired):
    """Pages drive the page-per-hour total and the complexity factor.

    Defaulting to zero would silently understate both, so the planner refuses and
    names the document type in the message.
    """
    payload = json.loads(json.dumps(HAPPY_INPUT))
    payload["documentConfigs"][0]["avgPages"] = 0
    result = index.lambda_handler(payload, None)
    assert result["success"] is False
    assert "No page data for document type 'invoice'" in result["errorMessage"]
    assert result["metrics"][0] == {"label": "Status", "value": "Error"}


@pytest.mark.unit
def test_an_unscheduled_document_type_still_needs_a_page_count_to_be_counted(wired):
    """The totals loop reads every configured type, not only the scheduled ones.

    A type with an hourly volume and no time slot contributes to the document, page
    and token totals — so its missing page count is caught there rather than in the
    schedule loop. Two separate guards, two separate messages, and only this input
    reaches the second one.
    """
    payload = json.loads(json.dumps(HAPPY_INPUT))
    payload["documentConfigs"].append(
        {
            "type": "unscheduled",
            "docsPerHour": 5,
            "avgPages": 0,
            "extractionTokens": 100,
        }
    )
    result = index.lambda_handler(payload, None)
    assert result["success"] is False
    assert "No page data for document type 'unscheduled'" in result["errorMessage"]


@pytest.mark.unit
def test_a_configured_type_with_no_volume_needs_no_page_count(wired):
    """The zero-volume guard again, this time in the handler's totals loop."""
    payload = json.loads(json.dumps(HAPPY_INPUT))
    payload["documentConfigs"].append(
        {"type": "unused", "docsPerHour": 0, "avgPages": 0, "extractionTokens": 100}
    )
    result = index.lambda_handler(payload, None)
    assert result["success"] is True
    assert result["metrics"][0]["value"] == "60"


@pytest.mark.unit
def test_a_user_config_that_is_not_valid_json_is_refused_by_validation(wired):
    """Same shape as the schedule above: validation wins over the quiet fallback.

    The handler would have carried on with no models configured, producing a
    successful report with no quota requirements at all — indistinguishable from an
    account that runs no Bedrock steps. Validation refuses it first and names the
    field.
    """
    payload = json.loads(json.dumps(HAPPY_INPUT))
    payload["userConfig"] = "{not json"
    result = index.lambda_handler(payload, None)
    assert result["success"] is False
    assert "userConfig must be valid JSON" in result["errorMessage"]


@pytest.mark.unit
def test_a_user_config_supplied_as_a_mapping_is_used_directly(wired):
    payload = json.loads(json.dumps(HAPPY_INPUT))
    payload["userConfig"] = {"extraction_model": MODEL}
    result = index.lambda_handler(payload, None)
    assert {r["usedFor"] for r in result["quotaRequirements"]} == {"Extraction"}


@pytest.mark.unit
def test_the_granular_assessment_flag_defaults_to_on_and_is_honoured_when_off(
    wired, tracking
):
    """Absent means enabled, because granular assessment is the default deployment."""
    put_metered(tracking, "doc-granular", bedrock("GranularAssessment", 8))
    payload = json.loads(json.dumps(HAPPY_INPUT))
    on = by_type(index.lambda_handler(payload, None)["quotaRequirements"])
    payload["granularAssessmentEnabled"] = False
    off = by_type(index.lambda_handler(payload, None)["quotaRequirements"])
    assert (
        on[("Assessment", "RPM")]["requiredQuota"]
        != off[("Assessment", "RPM")]["requiredQuota"]
    )


@pytest.mark.unit
def test_a_ui_supplied_time_range_overrides_the_configured_one_and_clears_the_cache(
    wired, monkeypatch
):
    """The UI's range selector has to invalidate the five-minute timing cache.

    Without the reset, switching from "last 24 hours" to "last hour" would keep
    showing the previous range's numbers. The override is written into the process
    environment, so it also persists for every later invocation on the same warm
    container — a subsequent request that names no range inherits this one.
    """
    monkeypatch.setattr(
        index, "_processing_times_cache", {"pattern-2": {"stale": True}}
    )
    payload = json.loads(json.dumps(HAPPY_INPUT))
    payload["latencyMetricsHours"] = 6

    index.lambda_handler(payload, None)

    import os

    assert os.environ["LATENCY_METRICS_HOURS"] == "6"
    assert index._processing_times_cache == {}


# --------------------------------------------------------------------------
# Request shapes
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_an_api_gateway_request_is_answered_with_a_status_code_and_a_json_body(wired):
    """The two callers need different envelopes off the same computation."""
    response = index.lambda_handler({"body": json.dumps(HAPPY_INPUT)}, None)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["success"] is True
    assert body["metrics"][0]["value"] == "60"


@pytest.mark.unit
def test_an_api_gateway_body_already_parsed_is_accepted(wired):
    response = index.lambda_handler({"body": dict(HAPPY_INPUT)}, None)
    assert response["statusCode"] == 200


@pytest.mark.unit
def test_a_malformed_api_gateway_body_is_a_client_error(wired):
    """400, not 500: the request is wrong, not the service."""
    response = index.lambda_handler({"body": "{not json"}, None)
    assert response["statusCode"] == 400
    assert "Invalid input" in json.loads(response["body"])["errorMessage"]


@pytest.mark.unit
def test_an_oversized_api_gateway_body_is_refused_before_being_parsed(wired):
    """A megabyte cap, so a hostile payload cannot exhaust the function's memory."""
    response = index.lambda_handler({"body": json.dumps({"x": "y" * 1_100_000})}, None)
    assert response["statusCode"] == 400
    assert "maximum size" in json.loads(response["body"])["errorMessage"]


@pytest.mark.unit
def test_a_resolver_request_carries_its_input_as_a_json_string(wired):
    result = index.lambda_handler(
        {"arguments": {"input": json.dumps(HAPPY_INPUT)}}, None
    )
    assert result["success"] is True
    assert "statusCode" not in result


@pytest.mark.unit
def test_a_resolver_request_may_also_carry_a_mapping(wired):
    result = index.lambda_handler({"arguments": {"input": dict(HAPPY_INPUT)}}, None)
    assert result["success"] is True


@pytest.mark.unit
def test_a_malformed_resolver_input_is_reported_without_a_status_code(wired):
    """A resolver response has no HTTP envelope; a status code there is dropped."""
    result = index.lambda_handler({"arguments": {"input": "{not json"}}, None)
    assert result["success"] is False
    assert "statusCode" not in result
    assert "Invalid input" in result["errorMessage"]


@pytest.mark.unit
def test_a_resolver_input_of_the_wrong_type_names_the_type_received(wired):
    result = index.lambda_handler({"arguments": {"input": [1, 2, 3]}}, None)
    assert result["success"] is False
    assert "Unexpected input type: list" in result["errorMessage"]


# --------------------------------------------------------------------------
# Error envelopes
# --------------------------------------------------------------------------


GRAPHQL_FIELDS = {
    "success",
    "errorMessage",
    "metrics",
    "quotaRequirements",
    "latencyDistribution",
    "calculationDetails",
    "recommendations",
}


@pytest.mark.unit
def test_a_configuration_error_returns_a_complete_response_rather_than_null(
    tracking, monkeypatch
):
    """An incomplete response makes the whole GraphQL field null.

    The UI then shows nothing at all — no error, no panel — so every error path has
    to return the full shape. Asserted field by field, because a missing one is
    invisible until the resolver is wired up.
    """
    monkeypatch.delenv("MIN_TOKENS_PER_REQUEST")
    result = index.lambda_handler(dict(HAPPY_INPUT), None)

    assert set(result) >= GRAPHQL_FIELDS
    assert result["success"] is False
    assert "Configuration error" in result["errorMessage"]
    assert "MIN_TOKENS_PER_REQUEST" in result["errorMessage"]
    assert result["metrics"] == [{"label": "Status", "value": "Configuration Error"}]
    assert result["latencyDistribution"]["p50"] == "0s"
    assert result["calculationDetails"] == {"quotasUsed": {"bedrock_models": {}}}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"pattern": "pattern-9"}, "Only pattern-2 and unified"),
        ({"maxAllowedLatency": -5}, "maxAllowedLatency must be positive"),
        ({"maxAllowedLatency": 3601}, "cannot exceed 3600 seconds"),
        ({"maxAllowedLatency": "abc"}, "must be a number"),
        ({"documentConfigs": []}, "documentConfigs is required"),
        ({"documentConfigs": [{"docsPerHour": 1}]}, "type is required"),
        (
            {"documentConfigs": [{"type": "x", "avgPages": -1}]},
            "avgPages cannot be negative",
        ),
        ({"timeSlots": [{"hour": 24}]}, "hour must be between 0 and 23"),
        ({"timeSlots": [{"docsPerHour": -1}]}, "docsPerHour cannot be negative"),
    ],
)
def test_an_invalid_request_is_described_specifically(wired, mutation, expected):
    """Named causes, because the operator is the one who can fix the input."""
    payload = json.loads(json.dumps(HAPPY_INPUT))
    payload.update(mutation)
    result = index.lambda_handler(payload, None)
    assert result["success"] is False
    assert expected in result["errorMessage"]
    assert set(result) >= GRAPHQL_FIELDS


@pytest.mark.unit
def test_a_zero_sla_is_reported_as_missing_rather_than_as_out_of_range(wired):
    """`get("maxAllowedLatency") or get("max_allowed_latency")` treats 0 as absent.

    The "must be positive" check below it is therefore unreachable for the one value
    an operator can actually type to reach it, and the message sends them looking
    for a field they did fill in. Pinned as current behaviour.
    """
    payload = json.loads(json.dumps(HAPPY_INPUT))
    payload["maxAllowedLatency"] = 0
    result = index.lambda_handler(payload, None)
    assert result["success"] is False
    assert (
        "maxAllowedLatency or max_allowed_latency is required" in result["errorMessage"]
    )
    assert "must be positive" not in result["errorMessage"]


@pytest.mark.unit
def test_the_snake_case_spelling_of_the_sla_is_accepted(wired):
    """Both spellings reach this function from different callers."""
    payload = json.loads(json.dumps(HAPPY_INPUT))
    del payload["maxAllowedLatency"]
    payload["max_allowed_latency"] = 600
    assert index.lambda_handler(payload, None)["success"] is True


@pytest.mark.unit
def test_an_invalid_request_over_api_gateway_is_a_400_with_the_full_shape(wired):
    payload = json.loads(json.dumps(HAPPY_INPUT))
    payload["pattern"] = "pattern-9"
    response = index.lambda_handler({"body": json.dumps(payload)}, None)
    assert response["statusCode"] == 400
    assert set(json.loads(response["body"])) >= GRAPHQL_FIELDS


@pytest.mark.unit
def test_an_unreadable_service_quotas_api_is_reported_as_a_failed_calculation(
    wired, monkeypatch
):
    """The most common real failure: the execution role lacks the quota permission."""

    def denied():
        raise ValueError("Cannot access Service Quotas API: AccessDeniedException")

    monkeypatch.setattr(index, "get_simple_quotas", denied)
    result = index.lambda_handler(dict(HAPPY_INPUT), None)
    assert result["success"] is False
    assert "Capacity calculation failed" in result["errorMessage"]
    assert "Service Quotas" in result["errorMessage"]
    assert result["recommendations"][0].startswith("❌ Error:")


@pytest.mark.unit
def test_the_error_detail_metric_is_truncated_for_display(wired, monkeypatch):
    """A 4 KB traceback in a metric tile breaks the layout; 100 characters fit."""

    def long_failure():
        raise ValueError("x" * 500)

    monkeypatch.setattr(index, "get_simple_quotas", long_failure)
    result = index.lambda_handler(dict(HAPPY_INPUT), None)
    details = next(m for m in result["metrics"] if m["label"] == "Details")
    assert len(details["value"]) == 100
    # The full message is still available on the error field itself.
    assert len(result["errorMessage"]) > 100


@pytest.mark.unit
def test_a_failed_calculation_over_api_gateway_is_a_500(wired, monkeypatch):
    """A server-side failure must not be reported to the client as a bad request."""

    def denied():
        raise ValueError("quota lookup exploded")

    monkeypatch.setattr(index, "get_simple_quotas", denied)
    response = index.lambda_handler({"body": json.dumps(HAPPY_INPUT)}, None)
    assert response["statusCode"] == 500
    assert json.loads(response["body"])["success"] is False


@pytest.mark.unit
def test_the_invocation_event_is_logged_with_its_identity_redacted(wired, capsys):
    """The event carries a Cognito identity, and CloudWatch logs are long-lived.

    The 1,000-character cap bounds the volume but redacts nothing, so the sanitizer
    has to run first. Asserted on the value, not on the sanitizer being called.
    """
    event = dict(HAPPY_INPUT)
    event["identity"] = {
        "claims": {"email": "operator@example.com"},
        "token": "abc.def",
    }
    index.lambda_handler(event, None)
    printed = capsys.readouterr().out
    assert "Received event:" in printed
    assert "operator@example.com" not in printed
    assert "abc.def" not in printed
