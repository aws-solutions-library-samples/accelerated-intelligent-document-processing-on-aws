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

**DynamoDB is real (moto), and the fixture stores a document the way the pipeline
does.** `Metering` goes in as a **JSON string**, because that is what both writers of
the tracking table produce — `DocumentDynamoDBService` calls
`json.dumps(document.metering, default=str)` for the live item and again for the run
snapshot — so `json.loads` on the way out yields ordinary floats. Numeric attributes
*outside* that payload (`PageCount`, and any timestamp stored as a number) come back
as `Decimal`, which is one of the things a hand-built dictionary of floats would not
reproduce; the others are the paginated `scan`, the `attribute_exists(Metering)`
filter and the string comparison the 24-hour recency window does against stored
timestamps. The `convert_decimal_to_float` calls on the metering payload are
therefore **defensive** rather than on the deployed path: they matter only for a
payload stored as a DynamoDB map, which no writer in this repository produces, and
`Decimal / float` raises rather than coercing. The tests that keep that shape use
`as_decimal_map` and each say why — one per conversion site, plus the `meteringData`
fallback, which has no writer to copy a shape from. Storing it everywhere would make
most of this file a test of a branch production never takes.

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

import ast
import json
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import index
import pytest
import validation
from moto import mock_aws

TRACKING_TABLE = "capacity-tracking"

CAPACITY_ENV = {
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",  # nosec B105 - dummy moto credential
    "AWS_SESSION_TOKEN": "testing",  # nosec B105 - dummy moto credential
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

    `Metering` is a JSON **string**, which is what both writers of this table
    produce: `DocumentDynamoDBService.update_document` sets
    `json.dumps(document.metering, default=str)`, and the run-snapshot writer in the
    same class does the same. Storing it as a DynamoDB map instead would be a more
    generous fixture than production — every number would come back as a `Decimal`
    and most of this file would be exercising the conversion that handles that,
    rather than the arithmetic it is about.

    Attributes passed through `**attributes` are stored as themselves, so a caller
    that wants a real `Decimal` (a `PageCount`, a numeric timestamp) passes one.
    """
    item = {"PK": key, "Metering": json.dumps(metering, default=str)}
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


def as_decimal_map(payload):
    """The same payload as a DynamoDB map of `Decimal`s rather than a JSON string.

    Nothing in this repository writes `Metering` in this shape — both writers call
    `json.dumps` — so every use of this helper is a deliberately defensive fixture
    and says so at the call site. The shape is kept at all because the failure it
    guards against is not graceful: `Decimal` does not coerce against `float`, so
    `gb_seconds / lambda_memory_gb` raises `TypeError` on the first document rather
    than returning a wrong number.
    """
    return json.loads(json.dumps(payload), parse_float=Decimal)


@pytest.mark.unit
def test_a_metering_payload_stored_as_a_dynamodb_map_is_converted_before_division(
    tracking,
):
    """The defensive conversion on the timing path, asserted once.

    A map-shaped payload is not what either writer produces, so this stands in for
    a hand-written item or a future writer that stops serializing. Storing every
    document this way is what made 30 tests in this file fail when the conversion
    was removed while the two that used the production shape stayed green — a count
    that looked like coverage of the deployed path and was not.
    """
    tracking.put_item(
        Item={"PK": "doc-1", "Metering": as_decimal_map(durations(OCR=61.6))}
    )
    assert index.get_real_latency_metrics("p")["base_times"]["ocr"] == pytest.approx(
        30.8
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
    """A second scan on `meteringData` runs when the first finds no `Metering`.

    Nothing in this repository writes that attribute, so the fallback exists for a
    table populated by something outside this tree; it is reached only when the
    first scan comes back empty, which means a table holding both reports on
    `Metering` alone. The payload is stored as a map here because there is no writer
    to take the shape from, and the map is the shape that also exercises the
    conversion on the way out.
    """
    tracking.put_item(
        Item={"PK": "doc-1", "meteringData": as_decimal_map(durations(OCR=30.0))}
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
@pytest.mark.parametrize(
    ("window", "expected_count"),
    [(2, 5), (None, 10)],
)
def test_the_history_window_argument_decides_which_documents_are_in_scope(
    tracking, window, expected_count
):
    """The per-request window has to reach the scan, not just be passed around.

    Five documents an hour old and five five hours old, against a configured default
    of 24 hours. Asking for two hours must see only the recent five; asking for
    nothing must fall back to the deployed 24 and see all ten. Both directions are
    needed: a function that ignored its argument entirely would still pass the
    `None` case, and one that ignored the environment would still pass the `2` case.

    This is the only test that exercises the real function with a window — the
    handler-level ones assert the value a patched lookup received, which says the
    argument was threaded but not that it changes anything.
    """
    now = datetime.utcnow()
    for i in range(5):
        timed_document(
            tracking,
            f"recent-{i}",
            started=(now - timedelta(hours=1, seconds=20)).strftime(
                "%Y-%m-%dT%H:%M:%S"
            ),
            completed=(now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"),
            metering=durations(OCR=10.0),
        )
    for i in range(5):
        timed_document(
            tracking,
            f"older-{i}",
            started=(now - timedelta(hours=5, seconds=600)).strftime(
                "%Y-%m-%dT%H:%M:%S"
            ),
            completed=(now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S"),
            metering=durations(OCR=10.0),
        )

    result = index.get_real_latency_metrics("p", window)

    assert result["processing_time_percentiles"]["count"] == expected_count


@pytest.mark.unit
def test_the_history_window_argument_is_reported_in_the_scan_log(tracking, capsys):
    """The window the scan actually used, named in the line an operator reads.

    Asserted against the configured default as well as the requested value, so a
    function that logged the argument while scanning on the environment value — or
    the reverse — is still caught.
    """
    timed_document(
        tracking,
        "doc-1",
        started=(datetime.utcnow() - timedelta(seconds=20)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        ),
        completed=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
        metering=durations(OCR=10.0),
    )
    index.get_real_latency_metrics("p", 1)
    printed = capsys.readouterr().out
    assert "last 1h" in printed
    assert "last 24h" not in printed


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
@pytest.mark.parametrize(
    ("metering", "label"),
    [
        ({}, "an empty metering map"),
        ({"Extraction/bedrock/nova": {"requests": 3}}, "token counts but no durations"),
    ],
)
def test_a_measured_document_time_is_enough_on_its_own(tracking, metering, label):
    """Either source of timing is sufficient, which is what the advice promises.

    `/lambda/duration` gb_seconds *or* `WorkflowStartTime`/`CompletionTime`
    timestamps: a document carrying only the second is a complete answer for the
    total, so the zero-total check is made after the timestamp total is computed
    rather than against the per-step sum before it. Both metering shapes that reach
    this path are covered — genuinely empty, and carrying keys that are not
    durations — because the per-step sum is zero either way.

    The per-step breakdown stays at zero and is not back-filled from the total: no
    estimate is substituted for a measurement that was never taken.
    """
    now = datetime.utcnow()
    timed_document(
        tracking,
        "doc-1",
        started=(now - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%S"),
        completed=now.strftime("%Y-%m-%dT%H:%M:%S"),
        metering=metering,
    )
    result = index.get_real_latency_metrics("p")

    assert result["total_processing_time"] == pytest.approx(120.0)
    assert result["data_source"] == "document_timestamps"
    assert result["processing_time_percentiles"]["p50"] == pytest.approx(120.0)
    assert set(result["base_times"].values()) == {0}


@pytest.mark.unit
def test_a_document_with_neither_a_duration_nor_a_timestamp_pair_is_refused(tracking):
    """With both sources missing there is nothing to plan from, and the report fails.

    This is the check the per-step-sum one above it used to make unreachable: it is
    the only zero-total raise now, and it is the one that runs after both sources
    have been consulted, so it can name both alternatives truthfully.
    """
    put_metered(tracking, "doc-1", {"Extraction/bedrock/nova": {"requests": 3}})
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


def breakdown_step_token_keys():
    """The per-step token keys the handler's breakdown initialiser writes.

    Derived from `index.py`, and deliberately from the **producer** rather than
    from the reader under test. Collecting the reader's subscripts would shrink
    this universe by exactly the regression the test below guards against — a key
    that went back to `.get(..., 0)` would stop being a subscript, drop out of the
    parametrisation, and the suite would pass by testing one stage fewer. The
    producer keeps listing every stage it populates either way.

    The aggregate `tokensPerHour` is excluded by case: the five per-step keys spell
    it `TokensPerHour`.
    """
    tree = ast.parse(Path(index.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {
            key.value
            for key in node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        if "classificationTokensPerHour" in keys:
            return sorted(key for key in keys if key.endswith("TokensPerHour"))
    return []


STEP_TOKEN_KEYS = breakdown_step_token_keys()


@pytest.mark.unit
def test_the_derived_step_token_key_set_is_the_five_pipeline_stages():
    """Closes the universe the parametrised test below runs over.

    Written out as literals rather than derived a second way, because an empty or
    short derivation makes that test collect fewer cases — and a parametrisation
    with no cases is reported as nothing at all, which reads like a pass. This
    fails instead, and it also catches a sixth stage being added to the
    initialiser without being required.
    """
    assert STEP_TOKEN_KEYS == [
        "assessmentTokensPerHour",
        "classificationTokensPerHour",
        "extractionTokensPerHour",
        "ocrTokensPerHour",
        "summarizationTokensPerHour",
    ]


@pytest.mark.unit
@pytest.mark.parametrize("missing_key", STEP_TOKEN_KEYS)
def test_every_per_step_token_key_in_the_breakdown_is_required(tracking, missing_key):
    """All five stages, one rule: an absent key is a `KeyError` naming it.

    OCR used to be the exception, read with a default of zero while the other four
    were indexed. The asymmetry is the wrong way round for a quota planner — a
    missing key defaulted to zero plans no quota for that stage, and the operator
    meets that as Bedrock throttling in production with nothing pointing back here,
    whereas the four that raised said which key was missing. The sole producer
    writes all five for every hour, so no caller loses a shape it used to have.

    Stated over the derived key set rather than as five hand-written cases, so a
    stage added to the pipeline is covered without this test being edited.
    """
    put_metered(tracking, "doc-1", bedrock("Extraction", 4))
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    for entry in hourly:
        del entry[missing_key]
    with pytest.raises(KeyError, match=missing_key):
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
def test_a_token_shortfall_reports_its_true_size_not_a_saturated_hundred(tracking):
    """A near miss and a disaster have to be different numbers.

    Both cases below are token shortfalls against the same 100,000 TPM quota, and
    both used to read 100: one needing 10% more headroom and one needing five and
    a half times the quota rendered identically, so the field that expresses the
    shortfall as a ratio was the one field that hid its size. An operator decides
    between "nudge the quota" and "escalate this" on exactly that difference.

    The two are asserted to differ from each other as well as to equal their own
    values, because that inequality is the property the cap removed and an
    equality pair alone would also be satisfied by a cap at some other constant.
    """
    put_metered(tracking, "doc-1", bedrock("Extraction", 4))

    def utilization(tokens_per_hour):
        hourly = hours(
            **{"9": {"docsPerHour": 60, "extractionTokensPerHour": tokens_per_hour}}
        )
        return by_type(build(hourly))[("Extraction", "TPM")]

    near_miss = utilization(6_000_000)
    disaster = utilization(30_000_000)

    assert near_miss["utilizationPercent"] != disaster["utilizationPercent"]
    # 6,000,000/60 x 1.1 = 110,000 against 100,000, and 30,000,000 gives 550,000.
    assert near_miss["utilizationPercent"] == pytest.approx(110.0)
    assert near_miss["requiredQuota"] == "110,000"
    assert disaster["utilizationPercent"] == pytest.approx(550.0)
    assert disaster["requiredQuota"] == "550,000"
    assert disaster["currentQuota"] == "100,000"


@pytest.mark.unit
def test_a_request_shortfall_reports_its_true_size_too(tracking):
    """The same uncapping on the request side, which is a separate expression.

    The cap was written out once per row, so removing it from the token figure is
    no evidence about the request figure — and the existing equality cases on
    both sides land on exactly 100, where a cap and no cap agree, so neither of
    them can tell the two apart.
    """

    def utilization(requests_per_doc, docs_per_hour):
        put_metered(tracking, "doc-1", bedrock("Extraction", requests_per_doc))
        hourly = hours(
            **{"9": {"docsPerHour": docs_per_hour, "extractionTokensPerHour": 60000}}
        )
        return by_type(build(hourly))[("Extraction", "RPM")]

    # 4 req/doc x 3000 docs / 60 x 1.1 = 220 RPM against a 200 RPM quota.
    near_miss = utilization(4, 3000)
    # 10 req/doc x 6000 docs / 60 x 1.1 = 1100 RPM against the same quota.
    disaster = utilization(10, 6000)

    assert near_miss["utilizationPercent"] != disaster["utilizationPercent"]
    assert near_miss["utilizationPercent"] == pytest.approx(110.0)
    assert near_miss["requiredQuota"] == "220"
    assert disaster["utilizationPercent"] == pytest.approx(550.0)
    assert disaster["requiredQuota"] == "1,100"
    assert disaster["currentQuota"] == "200"


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
def test_every_matching_metering_key_in_a_document_is_counted(tracking):
    """A step that issues several Bedrock calls under distinct keys counts them all.

    The key format is `{context}/bedrock/{model_id}`, and `merge_metering_data` sums
    repeats of one key rather than adding another, so several matching keys on one
    document means the step invoked Bedrock under more than one context or model. Two
    shapes in this repository produce exactly that for extraction: a per-class model
    override, and an escalation, which records under `Extraction-Escalation` and
    `ExtractionEscalation` — distinct keys that both satisfy the planner's
    `"extraction" in key` filter. Here 3 and 5 calls are 8 requests for the document.
    Stopping at the first match would count 3, understating the RPM requirement — the
    direction that reports a quota as sufficient when it is not.

    The document must still count once towards the per-document average, which is
    the other half of the same code and the reason the two are asserted together:
    counting each key as a document would divide 8 by 2 and land back near the
    undercount by a different route.
    """
    metering = {}
    metering.update(bedrock("Extraction", 3, model="model-a"))
    metering.update(bedrock("Extraction", 5, model="model-b"))
    put_metered(tracking, "doc-1", metering)
    hourly = hours(**{"9": {"docsPerHour": 600, "extractionTokensPerHour": 60000}})
    requirement = by_type(build(hourly))[("Extraction", "RPM")]
    # 8 req/doc x 600 docs / 60 x 1.1 = 88; the first key alone would give 33, and
    # treating each key as its own document would give 44.
    assert requirement["requiredQuota"] == "88"


@pytest.mark.unit
def test_assessment_sums_its_regular_and_granular_entries_together(tracking):
    """Assessment accumulates across keys, which is what granular assessment needs.

    2 regular plus 8 granular calls is 10 per document. Every step now accumulates
    every matching key into one per-document total, so this asserts no contrast with
    the extraction case above; what is still Assessment's own is how it *decides*
    which keys match — the `assessment/` and `granularassessment/` prefixes, and the
    granular flag honoured in the test below — and that is what this covers.
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
def test_turning_off_granular_assessment_costs_its_rpm_row_not_the_whole_report(
    tracking, capsys
):
    """A history recorded entirely under granular keys leaves nothing to count.

    Assessment then has demand and no countable request data — but because the
    configuration excluded every entry it had rather than because none was recorded,
    which is a distinction the operator acts on differently. That costs the step its
    request rate, and every other step is still reported, rather than one
    configuration change making the whole report unavailable. No request figure is
    invented for it.

    The token row survives, and that asymmetry is the point of this test. The TPM
    figure is the operator's own scheduled token demand measured against the Service
    Quotas value; it reads no metering at all, so nothing about it is unknown here.
    Withholding it would have hidden an available answer behind advice to process
    more documents — advice that cannot change it, because no quantity of documents
    is what the TPM figure is missing. The RPM figure is the only one with no
    measurement behind it, and it is the only one withheld.

    The absent RPM row does not say which guard produced it: Assessment here has real
    demand, so the shared no-demand-and-no-metering skip is not the one that could
    have fired, and the printed line is what identifies the branch — it is also the
    only place the cause is stated, so its wording is asserted rather than described.
    """
    metering = {}
    metering.update(bedrock("GranularAssessment", 8))
    metering.update(bedrock("Extraction", 3))
    put_metered(tracking, "doc-1", metering)
    hourly = hours(
        **{
            "9": {
                "docsPerHour": 60,
                "assessmentTokensPerHour": 60000,
                "extractionTokensPerHour": 60000,
            }
        }
    )
    requirements = by_type(
        build(
            hourly,
            config=model_config(assessment_model=MODEL, extraction_model=MODEL),
            granular=False,
        )
    )

    # 60,000 assessment tokens in the peak hour / 60 x 1.1 = 1,100 TPM, which the
    # 100,000 TPM quota covers. None of that needed a request count.
    assessment_tpm = requirements[("Assessment", "TPM")]
    assert assessment_tpm["requiredQuota"] == "1,100"
    assert assessment_tpm["statusText"] == "✅ Sufficient"
    assert assessment_tpm["utilizationPercent"] == pytest.approx(1.1)
    assert ("Assessment", "RPM") not in requirements
    # 3 req/doc x 60 docs / 60 x 1.1 = 3.3 -> 3: the rest of the report survives.
    assert requirements[("Extraction", "RPM")]["requiredQuota"] == "3"
    printed = capsys.readouterr().out
    assert "granular assessment is disabled" in printed
    assert "no demand and no metering data" not in printed


@pytest.mark.unit
def test_a_granular_entry_that_recorded_nothing_does_not_excuse_the_missing_data(
    tracking,
):
    """The skip above rests on a measurement having been excluded, not on a key.

    A `GranularAssessment` entry carrying zero requests would not have contributed
    to the average even with the feature on, so nothing was lost to the
    configuration and the step is in the ordinary "demand but no measurement" state,
    which still fails loudly. Without this, the skip would widen to any history that
    merely mentions a granular key.
    """
    put_metered(tracking, "doc-1", bedrock("GranularAssessment", 0))
    hourly = hours(**{"9": {"docsPerHour": 60, "assessmentTokensPerHour": 60000}})
    with pytest.raises(ValueError, match="No request count data found for Assessment"):
        build(hourly, config=model_config(assessment_model=MODEL), granular=False)


@pytest.mark.unit
def test_a_history_that_did_yield_a_request_average_is_not_covered_by_the_skip(
    tracking,
):
    """The skip also requires that *nothing* countable was found for the step.

    Here the regular assessment entry gives a per-document average, so the step is
    not in the all-granular state the skip is for; the request rate reaches zero
    instead because the schedule asks for assessment tokens in an hour that
    processes no documents. That contradictory schedule raises today and still does
    — the point being pinned is the boundary of the new skip, which would otherwise
    swallow any history that had a granular entry excluded from it.
    """
    metering = {}
    metering.update(bedrock("Assessment", 2))
    metering.update(bedrock("GranularAssessment", 8))
    put_metered(tracking, "doc-1", metering)
    hourly = hours(**{"9": {"docsPerHour": 0, "assessmentTokensPerHour": 60000}})
    with pytest.raises(ValueError, match="No request count data found for Assessment"):
        build(hourly, config=model_config(assessment_model=MODEL), granular=False)


@pytest.mark.unit
def test_both_halves_of_a_row_are_scaled_from_one_peak_hour_not_the_whole_day(
    tracking,
):
    """TPM and RPM are both per-minute limits, so each is scaled from a peak hour.

    Two schedules carrying the same 240-document day — one concentrated into hour 9,
    one spread 10 documents an hour across all 24 — therefore differ 24-fold on
    *both* halves of the extraction row, and the ratio is what is asserted rather
    than the four literals, because that is the property: concentrating a day's work
    raises the token requirement and the request requirement by the same factor.

    Deriving RPM from the 24-hour total instead would make the two schedules agree
    on RPM while still differing 24-fold on TPM, and the evenly spread day — the
    ordinary shape of a scheduled backlog — would be told to raise a request quota
    to 24 times the rate the account will ever see.

    ⚠️ "A" peak hour rather than "the" peak hour: the two maxima are taken over
    different series — TPM over `<step>TokensPerHour`, RPM over `docsPerHour` — so
    on a schedule whose busiest token hour is not its busiest document hour they
    come from different hours. That is the conservative answer and not a defect,
    since each quota has to cover its own worst hour, but it is why this test uses a
    schedule where the two coincide and why the ratio, not a shared hour index, is
    what is asserted.

    60 Bedrock calls a document is used rather than 1 so that neither figure lands
    below the resolution of the whole-number `requiredQuota`: at 1 call a document
    the spread schedule needs under a fifth of a request a minute, which prints as
    `0` and puts the ratio out of reach of the assertion.
    """
    put_metered(tracking, "doc-1", bedrock("Extraction", 60))
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

    def quota(requirements, quota_type):
        return int(
            requirements[("Extraction", quota_type)]["requiredQuota"].replace(",", "")
        )

    assert quota(peak, "RPM") == 24 * quota(flat, "RPM")
    assert quota(peak, "TPM") == 24 * quota(flat, "TPM")
    # 60 req/doc x 240 docs / 60 x 1.1 = 264, and x 10 docs gives 11.
    assert quota(peak, "RPM") == 264
    assert quota(flat, "RPM") == 11
    assert quota(peak, "TPM") == 26400
    assert quota(flat, "TPM") == 1100


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
def test_a_step_with_neither_demand_nor_recorded_requests_is_skipped_quietly(
    tracking, capsys
):
    """Zero configured tokens and nothing in metering means the step is not running.

    This is the path a Textract-based deployment takes for OCR: no Bedrock tokens
    are configured because OCR is not a Bedrock step, so no row is produced.

    The absent row does not say *which* guard produced it. OCR is the one step with
    two of them: the shared no-demand-and-no-metering skip, and an OCR-specific
    escape a few lines below it. Delete the shared one and OCR still skips, via the
    second — so the printed line, which differs between the two, is what identifies
    the branch. (The sibling test above, where the step is `Summarization`, has only
    the shared guard and so fails on the row alone.)
    """
    put_metered(tracking, "doc-1", bedrock("Extraction", 3))
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    requirements = by_type(
        build(hourly, config=model_config(extraction_model=MODEL, ocr_model=MODEL))
    )
    assert ("OCR", "TPM") not in requirements
    printed = capsys.readouterr().out
    assert "Skipping OCR - no demand and no metering data" in printed
    assert "no OCR tokens configured" not in printed


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
@pytest.mark.parametrize("pattern", ["pattern-2", "pattern-3"])
def test_a_whitespace_only_ocr_model_is_treated_as_unconfigured(
    tracking, pattern, capsys
):
    """Unconfigured at demand assembly, not merely dropped a few lines later.

    The absent row on its own proves neither. Weakening the guard to a bare
    `if ocr_model:` admits OCR to the demand map with the model id `"   ".strip()`,
    which is `""`, and the loop below then drops it on its own
    no-model-configured check — the same absent row. The demand map is printed
    before that loop runs, so it is what separates the two, and the loop's own
    skip line is asserted absent for the same reason.

    Parametrized over both patterns because the guard is written out twice, once in
    the pipeline branch and once in the SageMaker one, so neither copy is evidence
    about the other.
    """
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
        build(
            hourly,
            config=model_config(extraction_model=MODEL, ocr_model="   "),
            pattern=pattern,
        )
    )
    assert ("OCR", "TPM") not in requirements
    printed = capsys.readouterr().out
    demands = next(
        line
        for line in printed.splitlines()
        if line.startswith("Processing inference demands:")
    )
    assert "OCR" not in demands, demands
    assert "Skipping OCR - no model configured" not in printed


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
def test_a_step_below_one_request_a_minute_still_appears_in_the_report(tracking):
    """A step that runs rarely is in the report; only one that runs at all is.

    Assessment here has recorded Bedrock calls and no configured token demand, and
    its request rate works out at well under one a minute. A floor of one request
    a minute dropped it entirely — not to a zero row, to no row — which in a
    report whose whole purpose is to list the quotas an operator must check reads
    as "this step is switched off". Nothing distinguished the two, and the step
    that vanished was by construction the one nobody was watching.

    The inclusion rule is now zero on both terms, so the step is reported with the
    small figure it actually has. The figure is asserted rather than just the row's
    presence, because a row carrying an invented number would be worse than the
    silence it replaced: `requiredQuota` rounds the 0.18 RPM ask to "0" while
    `utilizationPercent` keeps the unrounded share of the quota, so between them
    the row says "running, needs no headroom" — which is the distinction the floor
    destroyed.

    Nothing here asserts the absence of a skip message, although that is the usual
    way of showing which branch produced a result. With the threshold at zero the
    drop branch is unreachable — not because the request rate is always positive,
    which the withheld-RPM path makes false, but because the two skips between them
    guarantee that at least one of the two terms is; see the comment at the
    inclusion rule. So "no skip line was printed" is implied by the rows existing
    rather than evidence about them, and asserting it would read as coverage of a
    branch no input can reach.
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
    # 1 req/doc x 10 docs / 60 x 1.1 = 0.183 RPM, which rounds to a "0" ask but is
    # not zero: the status text separates it from a step with no demand at all.
    rpm = requirements[("Assessment", "RPM")]
    assert rpm["requiredQuota"] == "0"
    assert rpm["statusText"] == "✅ Sufficient"
    assert rpm["utilizationPercent"] == pytest.approx(0.0917, rel=1e-3)
    # No assessment tokens are scheduled, so the token half is a true "No Demand".
    tpm = requirements[("Assessment", "TPM")]
    assert tpm["requiredQuota"] == "0"
    assert tpm["statusText"] == "✅ No Demand"


@pytest.mark.unit
def test_the_lowest_request_rate_the_planner_can_derive_still_gets_a_row(tracking):
    """The threshold is pinned at zero, not merely somewhere below one a minute.

    The case above runs at 0.183 RPM, so it is satisfied by any floor under that —
    the symmetry the inclusion rule is built on would still be unpinned against a
    floor of, say, 0.05, which was measured to leave the suite green. This pins the
    smallest rate the planner can actually derive instead.

    That smallest rate is a property of the counting code rather than an arbitrary
    small number: a document only joins the per-document average if it recorded at
    least one request for the step, so `requests_per_doc` cannot fall below 1.0,
    and the peak hour cannot schedule fewer than one document. One request in the
    one document processed in the busiest hour is therefore the floor, and it is
    the case a threshold would silence first.
    """
    metering = {}
    metering.update(bedrock("Extraction", 3))
    metering.update(bedrock("Assessment", 1))
    put_metered(tracking, "doc-1", metering)
    hourly = hours(**{"9": {"docsPerHour": 1, "extractionTokensPerHour": 60000}})
    requirements = by_type(
        build(
            hourly, config=model_config(extraction_model=MODEL, assessment_model=MODEL)
        )
    )
    # 1 req/doc x 1 doc / 60 x 1.1 = 0.01833 RPM against the 200 RPM quota.
    rpm = requirements[("Assessment", "RPM")]
    assert rpm["requiredQuota"] == "0"
    assert rpm["statusText"] == "✅ Sufficient"
    assert rpm["utilizationPercent"] == pytest.approx(0.009167, rel=1e-3)


@pytest.mark.unit
def test_a_map_shaped_metering_payload_is_converted_before_the_rate_arithmetic(
    tracking,
):
    """The second defensive conversion, which is a separate call site.

    The request path has its own `convert_decimal_to_float`, and it fails
    differently from the timing one: a `Decimal` request count survives the
    averaging and only raises at `(requests_per_hour / 60) * BUFFER_FACTOR`, which
    sits *outside* the scan's `try`, so the whole report fails rather than one step
    losing its metering. This is the one place that shape is stored on this path.
    """
    tracking.put_item(
        Item={"PK": "doc-1", "Metering": as_decimal_map(bedrock("Extraction", 4))}
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
    tracking, monkeypatch, capsys
):
    """The report fails, and the log says the scan was never attempted.

    The failure alone does not distinguish the guard from its absence: with the
    table name unset and the guard neutralised, the scan runs against an unnamed
    table, raises into the `except` below it, and produces the same "No request
    count data found". The `else` branch's own line is the only outcome the guard
    can produce on its own, so both it and the absence of the `except`'s line are
    checked.
    """
    monkeypatch.delenv("METERING_TABLE_NAME")
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    with pytest.raises(ValueError, match="No request count data found"):
        build(hourly)
    printed = capsys.readouterr().out
    assert "METERING_TABLE_NAME not configured - using estimation" in printed
    assert "Could not read metering data" not in printed


@pytest.mark.unit
def test_a_metering_table_that_cannot_be_read_does_not_crash_the_scan(
    aws, monkeypatch, capsys
):
    """The table is missing entirely; the read is caught and the step then fails.

    The distinction matters for diagnosis: the operator sees "no request count
    data", which is the same message a genuinely empty history produces, so the log
    line naming the underlying error is the only way to tell them apart — and it is
    asserted here rather than described, since a swallow that logged nothing would
    produce the same exception.
    """
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    with pytest.raises(ValueError, match="No request count data found"):
        build(hourly)
    printed = capsys.readouterr().out
    assert "Could not read metering data for Extraction" in printed
    assert "ResourceNotFoundException" in printed


@pytest.mark.unit
def test_the_reported_page_count_is_a_decaying_average_not_the_mean(tracking, capsys):
    """`(running + next) / 2` weights the last document far above the first.

    Page counts of 1, 10 and 100 give 52.8 rather than the true mean of 37, because
    each step halves the weight of everything before it.

    The attribute read is `number_of_pages`, which is a column of the Athena
    `metering` table and **not** something the tracking table carries — that table
    stores a page count as `PageCount`, per the test below. So this arithmetic
    describes a branch no deployed document reaches, and the fixture has to write
    `number_of_pages` by hand to reach it at all. Pinned rather than deleted because
    the branch is live code: anything that starts stamping that attribute, or a
    change of source table, makes it reachable, and then this is the figure an
    operator reads.
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
def test_the_page_count_the_tracking_table_really_stores_is_not_read(tracking, capsys):
    """`PageCount` is the attribute, and the scan does not look at it.

    This is what a deployed document looks like — `DocumentDynamoDBService` writes
    `PageCount` — so the measured page count is never available and the report falls
    back to the configured page values on every stack. Asserted through both printed
    lines, because the fallback is silent apart from them.
    """
    for i, pages in enumerate([1, 10, 100]):
        put_metered(
            tracking, f"doc-{i}", bedrock("Extraction", 3), PageCount=Decimal(pages)
        )
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    build(hourly)
    printed = capsys.readouterr().out
    assert "Using configured page values (no metering data)" in printed
    assert "Actual pages per document from metering" not in printed


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
def test_a_pattern_with_no_models_configured_produces_no_requirements(tracking, capsys):
    """No demand is assembled at all, which is a stronger statement than no rows.

    Two independent things produce the empty list here, and the list cannot tell
    them apart: every step is filtered out while the demand map is built, and the
    loop over that map has its own no-model-configured skip. The map is printed
    between the two, so asserting it is empty is what pins the first — and the
    second is then a backstop this input cannot reach at all.
    """
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    assert build(hourly, config=model_config()) == []
    assert "Processing inference demands: {}" in capsys.readouterr().out


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
def latency_windows():
    """The history window each `get_real_latency_metrics` call was given.

    One entry per call, in order, so a test can tell "asked for six hours" from
    "asked for the deployed default" — the latter arrives as `None`.
    """
    return []


@pytest.fixture
def wired(tracking, monkeypatch, latency_windows):
    """A handler whose Service Quotas and timing lookups are supplied, not live."""
    monkeypatch.setattr(index, "get_simple_quotas", lambda: quota_set())

    def timings(_pattern, latency_metrics_hours=None):
        latency_windows.append(latency_metrics_hours)
        return {
            "base_times": {"ocr": 10.0, "extraction": 20.0},
            "total_processing_time": 30.0,
            "processing_time_percentiles": {},
            "actual_queue_delays": {},
            "variance_factor": 1.2,
            "data_source": "document_timestamps",
        }

    monkeypatch.setattr(index, "get_real_latency_metrics", timings)
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
def test_an_uppercase_pattern_name_is_refused(wired):
    """`validate_capacity_input` accepts only `pattern-2` and `unified`.

    `PATTERN-2` is the spelling the handler's lowercasing step exists for, and it
    does not get there: the refusal below is validation's, not the normalization's.
    What that step *is* — unreachable — cannot be read from this outcome, and is
    asserted from the source in the test that follows.
    """
    payload = json.loads(json.dumps(HAPPY_INPUT))
    payload["pattern"] = "PATTERN-2"
    result = index.lambda_handler(payload, None)
    assert result["success"] is False
    assert "Only pattern-2 and unified patterns are supported" in result["errorMessage"]


@pytest.mark.unit
def test_the_handlers_pattern_lowercasing_sits_after_validation_and_is_unreachable():
    """Asserted on the source, because no input can tell the branch from its absence.

    The behavioural form of this was measured vacuous: deleting
    `if pattern.startswith("PATTERN-"): pattern = pattern.lower()` leaves every test
    in this file green, because validation has already refused every string that
    would enter it. Position is the only observable property, so position is what is
    checked — the `startswith("PATTERN-")` test must come after the
    `validate_capacity_input` call in `lambda_handler`. A refactor that moves
    validation later makes the branch live, and this fails rather than the tolerance
    silently starting to work.
    """
    source = (Path(__file__).parent / "index.py").read_text()
    tree = ast.parse(source)
    handler = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "lambda_handler"
    )
    validations = [
        node.lineno
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "validate_capacity_input"
    ]
    normalizations = [
        node.lineno
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "startswith"
        and any(
            isinstance(a, ast.Constant) and a.value == "PATTERN-" for a in node.args
        )
    ]
    assert len(validations) == 1, validations
    assert len(normalizations) == 1, normalizations
    assert normalizations[0] > validations[0]


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
    """Absent means enabled, because granular assessment is the default deployment.

    Both figures are asserted, not merely that they differ. The history holds one
    document with 2 regular assessment calls and one with 8 granular ones: with the
    flag on that averages 5 a document across two documents, 300 an hour over the
    60-document schedule, 5.5 a minute with the buffer; with it off only the regular
    document counts, so 2 a document and 2.2 a minute. A mutation that scales the
    accumulated granular requests moves both numbers and leaves them unequal, so an
    inequality is satisfied by an arithmetic error of any size.
    """
    put_metered(tracking, "doc-granular", bedrock("GranularAssessment", 8))
    payload = json.loads(json.dumps(HAPPY_INPUT))
    on = by_type(index.lambda_handler(payload, None)["quotaRequirements"])
    payload["granularAssessmentEnabled"] = False
    off = by_type(index.lambda_handler(payload, None)["quotaRequirements"])
    assert on[("Assessment", "RPM")]["requiredQuota"] == "6"
    assert off[("Assessment", "RPM")]["requiredQuota"] == "2"


@pytest.mark.unit
def test_a_ui_supplied_time_range_overrides_the_configured_one_and_clears_the_cache(
    wired, monkeypatch
):
    """The UI's range selector has to invalidate the five-minute timing cache.

    Without the reset, switching from "last 24 hours" to "last hour" would keep
    showing the previous range's numbers: the cache is keyed by pattern alone, so
    an entry computed over the old window is indistinguishable from one computed
    over the new.
    """
    monkeypatch.setattr(
        index, "_processing_times_cache", {"pattern-2": {"stale": True}}
    )
    payload = json.loads(json.dumps(HAPPY_INPUT))
    payload["latencyMetricsHours"] = 6

    index.lambda_handler(payload, None)

    assert index._processing_times_cache == {}


@pytest.mark.unit
def test_a_ui_supplied_time_range_reaches_the_lookup_as_an_argument(
    wired, latency_windows
):
    """The chosen window is threaded down the call, not stashed in the environment.

    Asserted on the value the lookup received, because that is the only place the
    choice has to arrive for the plan to be built over the right history.
    """
    payload = json.loads(json.dumps(HAPPY_INPUT))
    payload["latencyMetricsHours"] = 6

    index.lambda_handler(payload, None)

    assert latency_windows == [6]


@pytest.mark.unit
def test_a_ui_supplied_time_range_does_not_outlive_the_request(wired, latency_windows):
    """The leak this guards is a warm container, which is the normal case in Lambda.

    A request naming six hours is followed here by one naming nothing, in the same
    process, exactly as two invocations on one container would be. The second must
    ask for the deployed default — `None`, meaning "no choice made" — rather than
    inheriting six. Both halves are needed: asserting only the environment variable
    would pass if the handler stopped honouring the override altogether.
    """
    chose_six = json.loads(json.dumps(HAPPY_INPUT))
    chose_six["latencyMetricsHours"] = 6
    index.lambda_handler(chose_six, None)

    index.lambda_handler(json.loads(json.dumps(HAPPY_INPUT)), None)

    assert latency_windows == [6, None]
    assert os.environ["LATENCY_METRICS_HOURS"] == "24"


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
def test_a_zero_sla_is_refused_as_out_of_range_rather_than_reported_as_missing(wired):
    """Zero is a value an operator can type, and it is the wrong value, not a gap.

    The key is passed **explicitly** rather than omitted, which is the whole point:
    omitting it exercises the missing-field path, which always worked. Zero is
    refused rather than accepted — a latency budget of zero seconds cannot be met by
    any plan, so every report built on one would say the same thing — and the message
    now names the field's range instead of sending the operator to look for a field
    they did fill in.
    """
    payload = json.loads(json.dumps(HAPPY_INPUT))
    payload["maxAllowedLatency"] = 0
    result = index.lambda_handler(payload, None)
    assert result["success"] is False
    assert "maxAllowedLatency must be positive" in result["errorMessage"]
    assert "is required" not in result["errorMessage"]


@pytest.mark.unit
def test_an_explicit_zero_is_not_replaced_by_the_other_spelling_of_the_field(wired):
    """A request carrying both spellings, one of them zero, is contradictory.

    Which spelling supplies the value is decided on presence, so the canonical
    `maxAllowedLatency` wins and its zero is refused, rather than the request being
    answered against the other spelling's number and a report returned for an SLA
    nobody asked for. This is the one input whose outcome the presence rule changes
    from success to refusal.
    """
    payload = json.loads(json.dumps(HAPPY_INPUT))
    payload["maxAllowedLatency"] = 0
    payload["max_allowed_latency"] = 600
    result = index.lambda_handler(payload, None)
    assert result["success"] is False
    assert "maxAllowedLatency must be positive" in result["errorMessage"]


@pytest.mark.unit
def test_an_sla_left_blank_still_reads_as_a_missing_field(wired):
    """A cleared field is a missing value, not a malformed number.

    `""` is the shape a hand-built request or a non-console client sends, and the
    numeric per-document fields in the same request treat it the same way. The
    console's own cleared field arrives as `null` instead — it builds the payload
    with `parseFloat`, and `JSON.stringify(NaN)` is `null` — and that shape is not
    parametrised in here: it reaches the same message through the `is None` check
    below the selection, so no mutation of the selection distinguishes it and a case
    for it could not fail. `dict.get` answers `None` for a `null` and for an absent
    key alike, so the selection step it does exercise is the one the snake-case test
    above covers — a `None` under one spelling must not stop the other spelling from
    supplying the value.
    """
    payload = json.loads(json.dumps(HAPPY_INPUT))
    payload["maxAllowedLatency"] = ""
    result = index.lambda_handler(payload, None)
    assert result["success"] is False
    assert (
        "maxAllowedLatency or max_allowed_latency is required" in result["errorMessage"]
    )
    assert "must be a number" not in result["errorMessage"]


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
        "token": "abc.def",  # nosec B105 - an opaque request id, not a credential
    }
    index.lambda_handler(event, None)
    printed = capsys.readouterr().out
    assert "Received event:" in printed
    assert "operator@example.com" not in printed
    assert "abc.def" not in printed


# --------------------------------------------------------------------------
# Module-level invariants that no behaviour reveals
# --------------------------------------------------------------------------


def dotted_name(node):
    """`a.b.c` for an attribute chain rooted in a plain name, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


@pytest.mark.unit
def test_dynamodb_conditions_are_reached_by_import_not_through_the_boto3_attribute():
    """`Attr` is imported by name; no expression reaches it through `boto3`.

    `boto3.dynamodb.conditions` becomes an attribute of `boto3` only because
    creating a `dynamodb` **resource** imports that subpackage as a side effect. The
    metering scan's filter expression used that path while sitting a few lines below
    the resource call, so it worked by adjacency: replacing the resource with a
    cached client — the ordinary thing to do to a helper called once per request —
    would leave `boto3.dynamodb` undefined and the scan raising `AttributeError`.

    There is no behaviour to invert here, so this is asserted on the source, in
    three parts that close each other's gaps. The import is at **module scope**, not
    merely present somewhere — searched over `tree.body` rather than `ast.walk`,
    because an import inside a never-called function is still an `ImportFrom` node
    and satisfied a walk. The name is **bound at runtime**, which no source check
    can establish and which a non-executing import cannot fake. And nothing reaches
    into `boto3.dynamodb`, matched over the parsed tree rather than the text so the
    comment explaining the import cannot satisfy it.

    Those three together refuse the combination that passed the first version of
    this test with the defect fully restored: the import moved inside a function and
    the call spelled `getattr(boto3, 'dynamodb').conditions.Attr(...)`, which is
    rooted in a `Call` and so has no dotted name for the walker to see.
    """
    tree = ast.parse(Path(index.__file__).read_text(encoding="utf-8"))

    imported = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "boto3.dynamodb.conditions"
        for alias in node.names
    }
    assert "Attr" in imported

    assert hasattr(index, "Attr")

    reached_through_boto3 = sorted(
        name
        for name in (dotted_name(node) for node in ast.walk(tree))
        if name is not None and name.startswith("boto3.dynamodb")
    )
    assert reached_through_boto3 == []
