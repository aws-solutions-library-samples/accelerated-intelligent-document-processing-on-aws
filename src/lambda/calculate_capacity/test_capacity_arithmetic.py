# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the arithmetic in the `calculate_capacity` Lambda: the Service Quotas
lookups, the document-complexity factor, the latency distribution, and the
recommendation thresholds.

This function does not process documents. It answers "is your account's Bedrock quota
big enough for the volume you plan to run, and how long will a document take?", and
an operator raises a quota-increase request on the strength of the answer. So the
failure mode that matters is not an exception — a crash surfaces as a red error panel
and gets investigated — but a **plausible wrong number**: a request-per-minute
requirement computed from the wrong denominator, a threshold compared with `>=` where
the configuration meant `>`, or a quota read for a neighbouring model. Those return
`success: true` and are acted on.

Four things shape these tests.

**Every threshold is asserted at the boundary and on both sides of it.** Each of the
complexity, load, latency, token and page thresholds is a `>` comparison against an
environment variable, and every one of them is off-by-one away from a different
recommendation. A single happy value in the middle of a band cannot tell `>` from `>=`,
so each is pinned with the value exactly equal to the threshold (no message) and one
step past it (message).

**No threshold test uses the value the code would pick by default**, because these
variables have no defaults — the function raises without them — and a test using a
round number like `1.0` or `300` cannot distinguish "read from configuration" from
"hardcoded". The fixture therefore configures deliberately unround values
(`PAGE_COMPLEXITY_FACTOR=0.25`, `MEDIUM_COMPLEXITY_THRESHOLD=800`), and the expected
results are worked out from them by hand in each test.

**Where a plausible wrong formula exists, the inputs are chosen so it gives a different
answer.** The page factor is asserted at one page, where the correct `1 + (pages-1)*f`
is exactly `1.0` and the off-by-one `1 + pages*f` is not. The weighted complexity mean
uses two document types with a 9:1 volume split, so an unweighted mean differs. The
effective-capacity test is run twice with the token and the request limit each smaller
in turn, so `min()` cannot be confused with either operand.

**`get_real_latency_metrics` is patched here and exercised for real in
`test_capacity_metrics_and_handler.py`.** This module is about what the distribution
does *with* measured timings; that one is about how the timings are measured.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import index
import pytest
import validation
from botocore.exceptions import ClientError

# Deliberately unround, so a result can only be reproduced from these values and not
# from a literal that happens to look like a sensible default.
CAPACITY_ENV = {
    "TRACKING_TABLE": "capacity-tracking",
    "METERING_TABLE_NAME": "capacity-tracking",
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


@pytest.fixture(autouse=True)
def clean_module_state(monkeypatch):
    """Both modules cache across invocations, so every test starts from cold.

    `index` memoizes measured processing times for five minutes and `validation`
    memoizes the validated environment for the life of the container. Leaving either
    populated makes a test pass on the previous test's data.
    """
    for name, value in CAPACITY_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(index, "_processing_times_cache", {})
    monkeypatch.setattr(index, "_cache_expiry", 0)
    monkeypatch.setattr(validation, "_validated_env_vars", None)
    yield
    monkeypatch.setattr(index, "_processing_times_cache", {})
    monkeypatch.setattr(index, "_cache_expiry", 0)
    monkeypatch.setattr(validation, "_validated_env_vars", None)


def throttling_error():
    return ClientError(
        {"Error": {"Code": "Throttling", "Message": "slow down"}}, "GetServiceQuota"
    )


def access_denied_error():
    return ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
        "GetServiceQuota",
    )


# ==========================================================================
# retry_with_backoff
# ==========================================================================


@pytest.mark.unit
def test_a_call_that_succeeds_is_not_delayed(monkeypatch):
    sleeps = []
    monkeypatch.setattr(index.time, "sleep", sleeps.append)
    assert index.retry_with_backoff(lambda: "quota") == "quota"
    assert sleeps == []


@pytest.mark.unit
def test_throttling_is_retried_with_doubling_delays(monkeypatch):
    """Service Quotas throttles hard when a report asks about a dozen models.

    The delays are asserted, not just the retry count: a fixed delay would keep
    hammering a throttled API and the whole report would fail on a transient
    condition.
    """
    sleeps = []
    monkeypatch.setattr(index.time, "sleep", sleeps.append)
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise throttling_error()
        return "quota"

    assert index.retry_with_backoff(flaky) == "quota"
    assert attempts["n"] == 3
    assert sleeps == [1, 2]  # base_delay * 2**attempt for attempts 0 and 1


@pytest.mark.unit
def test_a_permissions_error_is_raised_at_once_rather_than_retried(monkeypatch):
    """A throttle is transient; a denied `servicequotas:GetServiceQuota` is not.

    Retrying it would add seven seconds of backoff per model to a report that was
    always going to fail, and an operator watching a Lambda time out learns less
    than one who is told the permission is missing.
    """
    sleeps = []
    monkeypatch.setattr(index.time, "sleep", sleeps.append)
    calls = {"n": 0}

    def denied():
        calls["n"] += 1
        raise access_denied_error()

    with pytest.raises(ClientError):
        index.retry_with_backoff(denied)
    assert calls["n"] == 1
    assert sleeps == []


@pytest.mark.unit
def test_a_non_client_error_is_retried_and_then_re_raised(monkeypatch):
    """A connection reset is worth retrying; an endless loop is not.

    Note the asymmetry with the case above: a bare `Exception` is retried but a
    non-throttling `ClientError` is not.
    """
    sleeps = []
    monkeypatch.setattr(index.time, "sleep", sleeps.append)
    calls = {"n": 0}

    def broken():
        calls["n"] += 1
        raise ConnectionResetError("reset by peer")

    with pytest.raises(ConnectionResetError):
        index.retry_with_backoff(broken, max_retries=3)
    assert calls["n"] == 3
    assert sleeps == [1, 2]


@pytest.mark.unit
def test_a_single_attempt_configuration_does_not_sleep_before_giving_up(monkeypatch):
    sleeps = []
    monkeypatch.setattr(index.time, "sleep", sleeps.append)
    with pytest.raises(ClientError):
        index.retry_with_backoff(
            lambda: (_ for _ in ()).throw(throttling_error()), max_retries=1
        )
    assert sleeps == []


# ==========================================================================
# _lookup_quota
# ==========================================================================


@pytest.mark.unit
def test_an_exact_model_id_is_looked_up_directly():
    assert (
        index._lookup_quota(
            {"us.amazon.nova-lite-v1:0": 40000}, "us.amazon.nova-lite-v1:0"
        )
        == 40000
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stored_key", "expected"),
    [
        ("us.anthropic.claude-sonnet-4-5-20250929-v1", 111),
        ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", 222),
    ],
)
def test_the_local_1m_context_suffix_falls_back_to_the_base_model(stored_key, expected):
    """`:1m` is this repository's marker for a 1M-context variant, not an AWS one.

    Service Quotas knows only the base model, so a report for the 1M variant has to
    read the base model's quota. Returning `None` instead would fail the whole
    report with "TPM quota not available" for a model the account can certainly
    use.
    """
    model_id = "us.anthropic.claude-sonnet-4-5-20250929-v1:1m"
    assert index._lookup_quota({stored_key: expected}, model_id) == expected


@pytest.mark.unit
def test_a_zero_quota_is_reported_rather_than_treated_as_absent():
    """A model with a zero quota is configured-but-unusable, which is actionable.

    Reading it as "not found" would raise "add the model to
    BEDROCK_MODEL_QUOTA_CODES" — advice that does not apply and hides the real
    problem, which is that the account has no allocation.
    """
    assert index._lookup_quota({"m": 0}, "m") == 0


@pytest.mark.unit
def test_a_model_id_without_the_suffix_gets_no_fuzzy_fallback():
    """Only the `:1m` convention falls back; other misses stay misses."""
    assert (
        index._lookup_quota({"us.amazon.nova-lite-v1": 1}, "us.amazon.nova-lite-v1:0")
        is None
    )
    assert index._lookup_quota({}, "us.amazon.nova-lite-v1:1m") is None


# ==========================================================================
# generate_rpm_quota_codes
# ==========================================================================


@pytest.mark.unit
def test_an_exactly_named_model_takes_its_own_rpm_quota_code(monkeypatch):
    monkeypatch.setenv(
        "BEDROCK_MODEL_RPM_QUOTA_CODES",
        json.dumps({"us.amazon.nova-lite-v1:0": "L-89F8391A"}),
    )
    assert index.generate_rpm_quota_codes(["us.amazon.nova-lite-v1:0"]) == {
        "us.amazon.nova-lite-v1:0": "L-89F8391A"
    }


@pytest.mark.unit
def test_a_dated_model_id_matches_the_undated_family_entry(monkeypatch):
    """The intended use of the fuzzy match: a new point release of a known model."""
    monkeypatch.setenv(
        "BEDROCK_MODEL_RPM_QUOTA_CODES",
        json.dumps({"us.anthropic.claude-sonnet-5": "L-D4FBCF4E"}),
    )
    result = index.generate_rpm_quota_codes(
        ["us.anthropic.claude-sonnet-5-20260401-v1:0"]
    )
    assert result == {"us.anthropic.claude-sonnet-5-20260401-v1:0": "L-D4FBCF4E"}


@pytest.mark.unit
def test_the_fuzzy_match_discards_the_region_prefix_that_selects_the_quota(monkeypatch):
    """A `us.` and a `global.` inference profile are separate Service Quotas entries.

    The production configuration lists both `us.anthropic.claude-sonnet-5`
    (`L-D4FBCF4E`) and `global.anthropic.claude-sonnet-5` (`L-DD84E5CA`) precisely
    because their limits are held separately. The cleaning step strips everything
    before the last two dots, so both mapping keys reduce to `claude-sonnet-5` and
    an id that needs the fuzzy path takes whichever entry the dictionary yields
    first — here the `us.` one, for a `global.` model.

    Asserted because the consequence is a confidently wrong number rather than an
    error: the report reads a real quota from a real API for the wrong inference
    profile and can print "✅ Sufficient" against a limit the workload will never
    be measured by.
    """
    monkeypatch.setenv(
        "BEDROCK_MODEL_RPM_QUOTA_CODES",
        json.dumps(
            {
                "us.anthropic.claude-sonnet-5": "L-D4FBCF4E",
                "global.anthropic.claude-sonnet-5": "L-DD84E5CA",
            }
        ),
    )
    model = "global.anthropic.claude-sonnet-5-20260401-v1:0"
    assert index.generate_rpm_quota_codes([model])[model] == "L-D4FBCF4E"


@pytest.mark.unit
def test_an_unmapped_model_names_itself_and_the_variable_to_fix(monkeypatch):
    """Silently defaulting would report a quota the model does not have."""
    monkeypatch.setenv(
        "BEDROCK_MODEL_RPM_QUOTA_CODES", json.dumps({"us.amazon.nova-lite-v1:0": "L-X"})
    )
    with pytest.raises(ValueError, match="cohere.command-r-plus-v1:0"):
        index.generate_rpm_quota_codes(["cohere.command-r-plus-v1:0"])


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "message"),
    [(None, "not set"), ("{not json}", "valid JSON")],
)
def test_a_missing_or_malformed_rpm_mapping_is_refused(monkeypatch, value, message):
    if value is None:
        monkeypatch.delenv("BEDROCK_MODEL_RPM_QUOTA_CODES")
    else:
        monkeypatch.setenv("BEDROCK_MODEL_RPM_QUOTA_CODES", value)
    with pytest.raises(ValueError, match=message):
        index.generate_rpm_quota_codes(["us.amazon.nova-lite-v1:0"])


# ==========================================================================
# get_simple_quotas
# ==========================================================================


def fake_quotas_client(tpm_by_code, rpm_by_code, *, list_raises=None):
    values = {**tpm_by_code, **rpm_by_code}
    client = MagicMock()
    if list_raises is not None:
        client.list_service_quotas.side_effect = list_raises
    else:
        client.list_service_quotas.return_value = {"Quotas": [{"QuotaCode": "L-1"}]}

    def get_service_quota(ServiceCode, QuotaCode):  # noqa: N803 - boto3 casing
        return {"Quota": {"Value": values[QuotaCode]}}

    client.get_service_quota.side_effect = get_service_quota
    return client


def run_get_simple_quotas(
    monkeypatch, tpm_codes, rpm_codes, tpm_values, rpm_values, **kw
):
    monkeypatch.setenv("BEDROCK_MODEL_QUOTA_CODES", json.dumps(tpm_codes))
    monkeypatch.setenv("BEDROCK_MODEL_RPM_QUOTA_CODES", json.dumps(rpm_codes))
    monkeypatch.setattr(index.time, "sleep", lambda _s: None)
    client = fake_quotas_client(tpm_values, rpm_values, **kw)
    boto3_stub = MagicMock()
    boto3_stub.client.return_value = client
    boto3_stub.Session.return_value.region_name = "us-east-1"
    with patch.object(index, "boto3", boto3_stub):
        return index.get_simple_quotas(), client


@pytest.mark.unit
def test_the_account_bedrock_ceiling_is_the_largest_model_quota_not_the_first(
    monkeypatch,
):
    """`quotas["bedrock"]` is the divisor the throughput estimate is built on.

    It is taken as the maximum across the configured models, so the estimate
    describes the most generously provisioned model rather than the one a given
    step actually uses. Pinned as a maximum specifically because the three values
    here are ordered so that `min`, `sum` and "first entry" all give a different
    answer.
    """
    quotas, _ = run_get_simple_quotas(
        monkeypatch,
        {"a": "L-A", "b": "L-B", "c": "L-C"},
        {"a": "L-RA", "b": "L-RB", "c": "L-RC"},
        {"L-A": 100000, "L-B": 900000, "L-C": 400000},
        {"L-RA": 50, "L-RB": 250, "L-RC": 120},
    )
    assert quotas["bedrock"] == 900000
    assert quotas["bedrock_models"] == {"a": 100000, "b": 900000, "c": 400000}
    assert quotas["bedrock_models_rpm"] == {"a": 50, "b": 250, "c": 120}


@pytest.mark.unit
def test_a_fractional_quota_value_is_truncated_rather_than_rounded_up(monkeypatch):
    """Rounding a limit up invents headroom the account does not have."""
    quotas, _ = run_get_simple_quotas(
        monkeypatch, {"a": "L-A"}, {"a": "L-RA"}, {"L-A": 199999.9}, {"L-RA": 9.99}
    )
    assert quotas["bedrock_models"]["a"] == 199999
    assert quotas["bedrock_models_rpm"]["a"] == 9


@pytest.mark.unit
def test_an_inaccessible_quotas_api_names_the_permission_to_grant(monkeypatch):
    with pytest.raises(ValueError, match="servicequotas:GetServiceQuota"):
        run_get_simple_quotas(
            monkeypatch,
            {"a": "L-A"},
            {"a": "L-RA"},
            {"L-A": 1},
            {"L-RA": 1},
            list_raises=access_denied_error(),
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "message"),
    [(None, "not set"), ("{nope}", "valid JSON"), ("{}", "is empty")],
)
def test_a_missing_or_malformed_tpm_mapping_is_refused(monkeypatch, value, message):
    if value is None:
        monkeypatch.delenv("BEDROCK_MODEL_QUOTA_CODES")
    else:
        monkeypatch.setenv("BEDROCK_MODEL_QUOTA_CODES", value)
    with pytest.raises(ValueError, match=message):
        index.get_simple_quotas()


@pytest.mark.unit
@pytest.mark.parametrize("failing", ["TPM", "RPM"])
def test_a_quota_that_cannot_be_read_names_the_model_and_the_quota_code(
    monkeypatch, failing
):
    """No fallback default: reporting against a guessed limit is the whole hazard."""
    monkeypatch.setenv("BEDROCK_MODEL_QUOTA_CODES", json.dumps({"nova": "L-A"}))
    monkeypatch.setenv("BEDROCK_MODEL_RPM_QUOTA_CODES", json.dumps({"nova": "L-RA"}))
    monkeypatch.setattr(index.time, "sleep", lambda _s: None)

    calls = {"n": 0}

    def flaky_retry(func, **_kwargs):
        calls["n"] += 1
        if failing == "TPM":
            return None
        return None if calls["n"] == 2 else {"Quota": {"Value": 1000}}

    boto3_stub = MagicMock()
    boto3_stub.Session.return_value.region_name = "us-east-1"
    with (
        patch.object(index, "boto3", boto3_stub),
        patch.object(index, "retry_with_backoff", flaky_retry),
    ):
        with pytest.raises(ValueError, match=rf"{failing} quota for model nova"):
            index.get_simple_quotas()


# ==========================================================================
# calculate_document_complexity_factor
# ==========================================================================


def doc(docs_per_hour=10, pages=1, ocr=0, classification=0, extraction=0, **extra):
    config = {
        "type": extra.pop("type", "invoice"),
        "docsPerHour": docs_per_hour,
        "avgPages": pages,
        "ocrTokens": ocr,
        "classificationTokens": classification,
        "extractionTokens": extraction,
    }
    config.update(extra)
    return config


@pytest.mark.unit
def test_no_documents_means_a_neutral_complexity_without_reading_configuration(
    monkeypatch,
):
    """The early return runs before the threshold variables are consulted.

    Asserted with every complexity variable unset, so a regression that moved the
    environment read above the guard would fail here rather than in a deployment
    that has not configured them.
    """
    for name in (
        "MEDIUM_COMPLEXITY_THRESHOLD",
        "HIGH_COMPLEXITY_THRESHOLD",
        "PAGE_COMPLEXITY_FACTOR",
        "HIGH_COMPLEXITY_MULTIPLIER",
        "MEDIUM_COMPLEXITY_MULTIPLIER",
    ):
        monkeypatch.delenv(name)
    assert index.calculate_document_complexity_factor([]) == 1.0


@pytest.mark.unit
def test_a_single_page_document_is_exactly_neutral():
    """`1 + (pages - 1) * factor`, so one page contributes no page penalty.

    With `PAGE_COMPLEXITY_FACTOR=0.25` the off-by-one `1 + pages * factor` would
    give 1.25 here, which is why one page is the value worth pinning.
    """
    assert index.calculate_document_complexity_factor([doc(pages=1)]) == 1.0


@pytest.mark.unit
@pytest.mark.parametrize(("pages", "expected"), [(2, 1.25), (5, 2.0), (21, 6.0)])
def test_each_extra_page_adds_the_configured_page_factor(pages, expected):
    assert index.calculate_document_complexity_factor([doc(pages=pages)]) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tokens_per_page", "expected"),
    [
        (799, 1.0),
        (800, 1.0),  # exactly at the medium threshold is not above it
        (801, 1.5),
        (2399, 1.5),
        (2400, 1.5),  # exactly at the high threshold is not above it
        (2401, 3.0),
    ],
)
def test_the_token_density_bands_are_exclusive_at_their_lower_edge(
    tokens_per_page, expected
):
    """`>` not `>=`, at both thresholds.

    One page, so the page factor is exactly 1.0 and the result *is* the density
    multiplier. A document sitting exactly on a threshold is the common case when
    an operator types a round number into the planner, and reading the band one
    step too wide multiplies every latency estimate by 1.5 or 3.
    """
    result = index.calculate_document_complexity_factor(
        [doc(pages=1, extraction=tokens_per_page)]
    )
    assert result == expected


@pytest.mark.unit
def test_only_the_three_prompt_stages_count_towards_token_density():
    """Density is OCR + classification + extraction, not the whole document.

    Assessment and summarization tokens are excluded, so a document with a large
    summary but a small extraction is not treated as complex. Both excluded values
    here are far past the high threshold, so counting either would move the answer
    from 1.0 to 3.0.
    """
    config = doc(
        pages=1,
        ocr=300,
        classification=200,
        extraction=200,  # 700 total, below the 800 medium threshold
        assessmentTokens=500_000,
        summarizationTokens=500_000,
    )
    assert index.calculate_document_complexity_factor([config]) == 1.0


@pytest.mark.unit
def test_complexity_is_weighted_by_document_volume_not_averaged_per_type():
    """A rare complex type must not dominate the estimate for a common simple one.

    90 simple documents an hour at 1.0 and 10 complex ones at 3.0 give 1.2. An
    unweighted mean over the two types would give 2.0 — a 67% overestimate of every
    latency figure downstream.
    """
    result = index.calculate_document_complexity_factor(
        [
            doc(type="simple", docs_per_hour=90, pages=1, extraction=100),
            doc(type="complex", docs_per_hour=10, pages=1, extraction=5000),
        ]
    )
    assert result == pytest.approx(1.2)


@pytest.mark.unit
def test_a_document_type_nobody_is_sending_is_skipped_before_its_pages_are_needed():
    """Order matters: the zero-volume check precedes the missing-pages check.

    The UI keeps configured-but-unscheduled document types in the list, and they
    routinely have no page count. Checking pages first would make the planner
    unusable until every unused type was filled in.
    """
    result = index.calculate_document_complexity_factor(
        [
            doc(type="unused", docs_per_hour=0, pages=0),
            doc(type="active", docs_per_hour=5, pages=1),
        ]
    )
    assert result == 1.0


@pytest.mark.unit
def test_a_scheduled_document_type_with_no_page_count_is_refused():
    """Silently treating it as one page would understate every estimate."""
    with pytest.raises(ValueError, match="No page data"):
        index.calculate_document_complexity_factor([doc(docs_per_hour=5, pages=0)])


@pytest.mark.unit
def test_a_schedule_with_no_volume_at_all_is_neutral():
    assert (
        index.calculate_document_complexity_factor([doc(docs_per_hour=0, pages=3)])
        == 1.0
    )


@pytest.mark.unit
def test_a_missing_complexity_variable_names_all_five(monkeypatch):
    monkeypatch.delenv("HIGH_COMPLEXITY_MULTIPLIER")
    with pytest.raises(ValueError, match="HIGH_COMPLEXITY_MULTIPLIER"):
        index.calculate_document_complexity_factor([doc()])


# ==========================================================================
# calculate_latency_distribution
# ==========================================================================


def quotas(tpm=60000, rpm=50):
    return {
        "bedrock": tpm,
        "bedrock_models": {"nova": tpm},
        "bedrock_models_rpm": {"nova": rpm},
    }


def latency_data(
    *,
    total=120.0,
    percentiles=None,
    queue=None,
    base=None,
    source="document_timestamps",
):
    data = {
        "base_times": base
        or {
            "ocr": 20.0,
            "classification": 20.0,
            "extraction": 60.0,
            "assessment": 20.0,
        },
        "total_processing_time": total,
        "processing_time_percentiles": percentiles or {},
        "actual_queue_delays": queue or {},
        "variance_factor": 1.2,
        "data_source": source,
    }
    return data


def distribution(
    monkeypatch,
    *,
    docs_per_hour=600,
    tokens=None,
    max_latency=600.0,
    quota=None,
    metrics=None,
    document_configs=None,
):
    if tokens is None:
        tokens = docs_per_hour * 6000
    monkeypatch.setattr(
        index, "get_real_latency_metrics", lambda _p: metrics or latency_data()
    )
    return index.calculate_latency_distribution(
        docs_per_hour,
        docs_per_hour * 3,
        tokens,
        "pattern-2",
        max_latency,
        quota or quotas(),
        document_configs,
    )


@pytest.mark.unit
def test_capacity_is_the_smaller_of_the_token_and_request_limits(monkeypatch):
    """Two independent Bedrock limits; the binding one sets throughput.

    60,000 TPM at 6,000 tokens a document is 10 documents a minute, so a 50 RPM
    limit is not binding and the answer is 10. Reported as `processingRate`, which
    is what an operator compares against their arrival rate.
    """
    result = distribution(monkeypatch, docs_per_hour=60, tokens=60 * 6000)
    assert result["processingRate"] == "10 docs/min"


@pytest.mark.unit
def test_a_low_request_limit_binds_before_the_token_limit_does(monkeypatch):
    """Run with the operands the other way round, so `min` cannot be mistaken.

    Same 10 documents a minute of token headroom, but only 3 requests a minute
    allowed. A report that used the token limit here would advise an operator that
    they have three times the throughput they have.
    """
    result = distribution(
        monkeypatch, docs_per_hour=60, tokens=60 * 6000, quota=quotas(rpm=3)
    )
    assert result["processingRate"] == "3 docs/min"


@pytest.mark.unit
def test_a_tiny_document_is_costed_at_the_configured_request_floor(monkeypatch):
    """Bedrock bills a minimum per request, so a 10-token document is not free.

    With `MIN_TOKENS_PER_REQUEST=1500`, 60,000 TPM buys 40 documents a minute.
    Using the measured 10 tokens instead would claim 6,000 a minute.
    """
    result = distribution(
        monkeypatch, docs_per_hour=60, tokens=600, quota=quotas(rpm=100000)
    )
    assert result["processingRate"] == "40 docs/min"


@pytest.mark.unit
def test_a_plan_with_no_documents_reports_no_demand_without_measuring_anything(
    monkeypatch,
):
    """The early return must precede the DynamoDB scan for measured timings.

    A brand-new deployment has no processed documents, so fetching metrics would
    raise and the planner would show an error instead of an empty plan. The metrics
    function is made to raise here, so reaching it fails the test.
    """

    def must_not_be_called(_pattern):
        raise AssertionError("measured timings were fetched for an empty plan")

    monkeypatch.setattr(index, "get_real_latency_metrics", must_not_be_called)
    result = index.calculate_latency_distribution(
        0, 0, 0, "pattern-2", 600.0, quotas(), []
    )
    assert result["dataSource"] == "no_demand"
    assert result["demandRate"] == "0 docs/min"
    assert result["warningMessage"] == "No processing demand configured"
    assert result["exceedsLimit"] is False
    # With nothing scheduled there is no measured document size, so the rate shown is
    # the best case: 60,000 TPM divided by the 1,500-token request floor.
    assert result["processingRate"] == "40 docs/min"


@pytest.mark.unit
def test_demand_exactly_equal_to_capacity_is_not_reported_as_overloaded(monkeypatch):
    """100% utilization is full, not over.

    10 documents a minute of capacity against 600 an hour is exactly 100%. Reading
    the comparison as `>=` would flag a correctly-sized account as overloaded and
    send the operator to raise a quota they do not need.
    """
    result = distribution(monkeypatch, docs_per_hour=600, tokens=600 * 6000)
    assert result["quotaUtilization"] == "100.0%"
    assert result["quotaOverloaded"] is False
    assert "bottlenecks" not in result


@pytest.mark.unit
def test_one_document_an_hour_past_capacity_is_reported_as_overloaded(monkeypatch):
    """The other side of the same boundary, with the bottleneck named."""
    result = distribution(monkeypatch, docs_per_hour=660, tokens=660 * 6000)
    assert result["quotaOverloaded"] is True
    assert result["loadFactor"] == "1.10x"
    assert result["bottlenecks"] == ["Bedrock Quota (110.0% utilization - OVERLOADED)"]


@pytest.mark.unit
def test_the_reported_percentiles_are_processing_time_plus_queue_delay(monkeypatch):
    """Latency is what a document experiences, which includes waiting in SQS.

    Both halves are also published separately for the stacked bar in the UI, so a
    wrong sum would disagree with its own components on the same screen. The values
    here are chosen so that every percentile differs from every other.
    """
    metrics = latency_data(
        total=30.0,
        percentiles={
            "p50": 30.0,
            "p75": 44.0,
            "p90": 57.0,
            "p95": 68.0,
            "p99": 91.0,
            "count": 40,
        },
        queue={
            "p50": 2.0,
            "p75": 5.0,
            "p90": 11.0,
            "p95": 19.0,
            "p99": 37.0,
            "count": 40,
        },
    )
    result = distribution(monkeypatch, metrics=metrics)

    assert (
        result["p50"],
        result["p75"],
        result["p90"],
        result["p95"],
        result["p99"],
    ) == (
        "32.0s",
        "49.0s",
        "68.0s",
        "87.0s",
        "128.0s",
    )
    assert result["procP99"] == "91.0s"
    assert result["queueP99"] == "37.0s"
    assert result["baseLatency"] == "30.0s"
    assert result["queueLatency"] == "2.00s"
    assert result["totalLatency"] == "32.0s"
    assert result["dataSource"] == "document_timestamps"


@pytest.mark.unit
def test_without_measured_percentiles_every_percentile_is_the_median(monkeypatch):
    """Honest degradation: one sample cannot describe a tail.

    Publishing a fabricated spread would let a P99 SLA look satisfied on the
    strength of a single measurement.
    """
    result = distribution(monkeypatch, metrics=latency_data(total=45.0))
    assert result["p50"] == result["p99"] == "45.0s"
    assert result["queueP50"] == result["queueP99"] == "0.0s"
    assert result["queueLatency"] == "0.00s"


@pytest.mark.unit
def test_an_empty_percentile_block_is_not_mistaken_for_a_measurement(monkeypatch):
    """`count: 0` means the block was built but holds nothing."""
    metrics = latency_data(
        total=45.0,
        percentiles={"p50": 1.0, "p99": 2.0, "count": 0},
        queue={"p50": 3.0, "p99": 4.0, "count": 0},
    )
    result = distribution(monkeypatch, metrics=metrics)
    assert result["p50"] == result["p99"] == "45.0s"


@pytest.mark.unit
def test_a_latency_exactly_at_the_sla_does_not_breach_it(monkeypatch):
    """Typical latency of 32s against a 32s SLA is inside it, not outside.

    The comparison is on the P50 total, so `exceedsLimit` describes the typical
    document rather than the tail; a plan whose P99 is far past the SLA still
    reports `False` here.
    """
    metrics = latency_data(
        total=30.0,
        percentiles={"p50": 30.0, "p99": 600.0, "count": 40},
        queue={"p50": 2.0, "p99": 3.0, "count": 40},
    )
    result = distribution(monkeypatch, metrics=metrics, max_latency=32.0)
    assert result["exceedsLimit"] is False
    assert "warningMessage" not in result
    assert result["maxAllowed"] == "32.0s"
    assert result["p99"] == "603.0s"  # the tail is published but does not breach


@pytest.mark.unit
def test_a_latency_one_step_past_the_sla_breaches_it_and_says_so(monkeypatch):
    metrics = latency_data(
        total=30.0,
        percentiles={"p50": 30.0, "p99": 30.0, "count": 40},
        queue={"p50": 2.1, "p99": 2.1, "count": 40},
    )
    result = distribution(monkeypatch, metrics=metrics, max_latency=32.0)
    assert result["exceedsLimit"] is True
    assert "exceeds SLA" in result["warningMessage"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("docs_per_hour", "expected"),
    [
        (300, "2.00x"),  # utilization 0.5: below 1.0 adds nothing
        (600, "2.00x"),  # utilization exactly 1.0: still nothing
        (840, "2.40x"),  # utilization 1.4: 2.0 * (1 + 0.4 * 0.5)
    ],
)
def test_variance_grows_with_complexity_and_only_with_overload(
    monkeypatch, docs_per_hour, expected
):
    """`complexity * (1 + max(0, utilization - 1) * 0.5)`.

    The `max(0, ...)` clamp is the part worth pinning: without it an underloaded
    account would report *less* variance than its documents actually have, and the
    recommendation that suggests request queuing would never fire. Complexity is
    held at 2.0 by a five-page document type so the multiplier is visible.
    """
    five_pages = [doc(type="long", docs_per_hour=docs_per_hour, pages=5)]
    result = distribution(
        monkeypatch,
        docs_per_hour=docs_per_hour,
        tokens=docs_per_hour * 6000,
        document_configs=five_pages,
    )
    assert result["complexityFactor"] == "2.00x"
    assert result["varianceFactor"] == expected


@pytest.mark.unit
def test_a_total_time_the_metrics_did_not_supply_falls_back_to_the_step_sum(
    monkeypatch,
):
    metrics = latency_data(base={"ocr": 11.0, "extraction": 22.0})
    del metrics["total_processing_time"]
    result = distribution(monkeypatch, metrics=metrics)
    assert result["baseLatency"] == "33.0s"


@pytest.mark.unit
def test_a_per_step_token_breakdown_is_summed_before_being_divided(monkeypatch):
    """The caller may pass either a total or a per-step mapping."""
    result = distribution(
        monkeypatch,
        docs_per_hour=60,
        tokens={"ocr": 60000, "extraction": 240000, "assessment": 60000},
        quota=quotas(rpm=100000),
    )
    # 360,000 tokens/hour over 60 docs/hour = 6,000 per doc; 60,000 TPM buys 10/min.
    assert result["processingRate"] == "10 docs/min"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("broken", "message"),
    [
        ({"bedrock": None, "bedrock_models_rpm": {"n": 1}}, "TPM quota not available"),
        ({"bedrock": 1000, "bedrock_models_rpm": {}}, "RPM quotas not available"),
    ],
)
def test_a_missing_quota_stops_the_estimate_instead_of_guessing(
    monkeypatch, broken, message
):
    """There is no safe default for a quota; an unquantified plan is worthless."""
    with pytest.raises(ValueError, match=message):
        index.calculate_latency_distribution(
            600, 1800, 3_600_000, "pattern-2", 600.0, broken, []
        )


@pytest.mark.unit
def test_a_missing_token_floor_stops_the_estimate(monkeypatch):
    monkeypatch.delenv("MIN_TOKENS_PER_REQUEST")
    with pytest.raises(ValueError, match="MIN_TOKENS_PER_REQUEST"):
        index.calculate_latency_distribution(
            600, 1800, 3_600_000, "pattern-2", 600.0, quotas(), []
        )


@pytest.mark.unit
def test_unavailable_timings_are_reported_as_such_rather_than_estimated(monkeypatch):
    """No synthetic fallback: a made-up processing time is the failure to avoid."""

    def no_documents(_pattern):
        raise ValueError("No processed documents found with metering data")

    monkeypatch.setattr(index, "get_real_latency_metrics", no_documents)
    with pytest.raises(ValueError, match="Unable to get processing time metrics"):
        index.calculate_latency_distribution(
            600, 1800, 3_600_000, "pattern-2", 600.0, quotas(), []
        )


# ==========================================================================
# generate_adaptive_recommendations
# ==========================================================================


def recommend(
    monkeypatch,
    latency_overrides=None,
    *,
    quota_requirements=None,
    docs_per_hour=100,
    pattern="pattern-2",
    document_configs=None,
):
    latency = {
        "complexityFactor": "1.00x",
        "loadFactor": "1.00x",
        "varianceFactor": "1.00x",
        "p99": "10.0s",
        "exceedsLimit": False,
        "dataSource": "document_timestamps",
    }
    latency.update(latency_overrides or {})
    return index.generate_adaptive_recommendations(
        latency,
        quota_requirements or [],
        docs_per_hour,
        pattern,
        document_configs or [],
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("factor", "expected_fragment"),
    [
        ("1.50x", None),  # exactly at the medium threshold
        ("1.51x", "Medium document complexity"),
        ("2.50x", "Medium document complexity"),  # exactly at the high threshold
        ("2.51x", "High document complexity"),
    ],
)
def test_the_complexity_advice_bands_are_exclusive_at_their_edges(
    monkeypatch, factor, expected_fragment
):
    """Both comparisons are `>`, against 1.5 and 2.5 from configuration.

    "Split your documents" and "monitor and consider optimization" ask for
    different work, and a band read one step wide sends an operator to do the wrong
    one.
    """
    text = " ".join(recommend(monkeypatch, {"complexityFactor": factor}))
    if expected_fragment is None:
        assert "complexity" not in text
    else:
        assert expected_fragment in text


@pytest.mark.unit
@pytest.mark.parametrize(
    ("factor", "expected_fragment"),
    [
        ("2.00x", None),
        ("2.01x", "Moderate system load"),
        ("3.00x", "Moderate system load"),
        ("3.01x", "High system load"),
    ],
)
def test_the_load_advice_bands_are_exclusive_at_their_edges(
    monkeypatch, factor, expected_fragment
):
    text = " ".join(recommend(monkeypatch, {"loadFactor": factor}))
    if expected_fragment is None:
        assert "system load" not in text
    else:
        assert expected_fragment in text


@pytest.mark.unit
@pytest.mark.parametrize(
    ("p99", "flagged"), [("299.9s", False), ("300.0s", False), ("300.1s", True)]
)
def test_the_tail_latency_warning_fires_only_past_the_configured_threshold(
    monkeypatch, p99, flagged
):
    """300 seconds from configuration, compared with `>`."""
    text = " ".join(recommend(monkeypatch, {"p99": p99}))
    assert ("High P99 latency" in text) is flagged


@pytest.mark.unit
def test_the_latency_warning_quotes_the_measured_p99(monkeypatch):
    """A warning that does not say how bad it is cannot be prioritised."""
    text = " ".join(recommend(monkeypatch, {"p99": "412.6s"}))
    assert "413s" in text  # formatted with no decimals


@pytest.mark.unit
def test_an_sla_breach_is_called_out_separately_from_the_latency_threshold(monkeypatch):
    """The two are independent: a fast document can still miss a tight SLA."""
    text = " ".join(recommend(monkeypatch, {"exceedsLimit": True, "p99": "10.0s"}))
    assert "exceeds SLA" in text
    assert "High P99 latency" not in text


@pytest.mark.unit
def test_the_quota_advice_counts_every_shortfall_but_names_only_the_first_three(
    monkeypatch,
):
    """The count is the actionable part and must not be truncated with the list.

    Five models need an increase; the message lists three to stay readable. A count
    of three would understate the work by two quota requests.
    """
    requirements = [
        {"status": "warning", "modelId": f"model-{i}"} for i in range(5)
    ] + [{"status": "success", "modelId": "fine"}]
    text = " ".join(recommend(monkeypatch, quota_requirements=requirements))
    assert "5 quota increases needed" in text
    assert "model-0, model-1, model-2" in text
    assert "model-3" not in text
    assert "fine" not in text


@pytest.mark.unit
def test_a_plan_within_every_quota_gets_no_quota_advice(monkeypatch):
    requirements = [{"status": "success", "modelId": "nova"}]
    assert "quota increases" not in " ".join(
        recommend(monkeypatch, quota_requirements=requirements)
    )


@pytest.mark.unit
def test_named_bottlenecks_are_passed_through_for_scaling_advice(monkeypatch):
    text = " ".join(
        recommend(monkeypatch, {"bottlenecks": ["Bedrock Quota (140%)", "Textract"]})
    )
    assert "Bedrock Quota (140%), Textract" in text


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ocr", "extraction", "flagged"),
    [(25000, 25000, False), (25000, 25001, True)],
)
def test_the_large_document_warning_counts_ocr_and_extraction_tokens(
    monkeypatch, ocr, extraction, flagged
):
    """50,000 from configuration, and only these two stages contribute.

    Exactly at the threshold is not over it. The classification tokens below are
    large enough that including them would trip the warning in both rows, so this
    also pins which stages are summed.
    """
    configs = [doc(pages=1, ocr=ocr, extraction=extraction, classification=900_000)]
    text = " ".join(recommend(monkeypatch, document_configs=configs))
    assert ("document types exceed 50000 tokens" in text) is flagged


@pytest.mark.unit
@pytest.mark.parametrize(("pages", "flagged"), [(20, False), (21, True)])
def test_the_page_count_warning_fires_only_past_the_configured_threshold(
    monkeypatch, pages, flagged
):
    configs = [doc(pages=pages)]
    text = " ".join(recommend(monkeypatch, document_configs=configs))
    assert ("have >20 pages" in text) is flagged


@pytest.mark.unit
def test_the_document_advice_counts_how_many_types_are_affected(monkeypatch):
    configs = [doc(type="a", pages=30), doc(type="b", pages=40), doc(type="c", pages=2)]
    text = " ".join(recommend(monkeypatch, document_configs=configs))
    assert "2 document types have >20 pages" in text


@pytest.mark.unit
@pytest.mark.parametrize("factor", ["3.00x", "3.01x"])
def test_the_variance_advice_uses_a_literal_three_not_a_configured_threshold(
    monkeypatch, factor
):
    """Unlike every other band here, this threshold is hardcoded in the function.

    Pinned so the inconsistency is visible: an operator who tunes the
    `RECOMMENDATION_*` variables cannot move this one, and the value is asserted at
    and past 3.0 rather than at any configured value.
    """
    text = " ".join(recommend(monkeypatch, {"varianceFactor": factor}))
    assert ("High latency variance" in text) is (factor == "3.01x")


@pytest.mark.unit
def test_the_headline_names_the_volume_and_the_pattern(monkeypatch):
    first = recommend(monkeypatch, docs_per_hour=2500)[0]
    assert "Processing 2500 documents/hour using PATTERN-2" in first


@pytest.mark.unit
@pytest.mark.parametrize(
    ("data_source", "described_as"),
    [
        ("environment_config", "environment configuration"),
        ("document_timestamps", "unknown source"),
    ],
)
def test_only_an_environment_derived_plan_has_its_source_described(
    monkeypatch, data_source, described_as
):
    """`document_timestamps` is the *good* case and is still called unknown.

    The mapping recognises one value, so a plan built entirely from measured
    document timestamps tells the operator its provenance is unknown — the least
    trustworthy-sounding label attached to the most trustworthy data. Pinned as
    current behaviour so a change to it is deliberate.
    """
    first = recommend(monkeypatch, {"dataSource": data_source})[0]
    assert f"based on {described_as}" in first


@pytest.mark.unit
def test_a_missing_threshold_variable_degrades_the_advice_without_losing_it(
    monkeypatch,
):
    """Recommendations are advisory, so a configuration gap must not blank the panel.

    The headline computed before the failure has to survive, and the reason has to
    be reported rather than the list silently ending early — an operator reading a
    short list has no way to tell it was cut off.
    """
    monkeypatch.delenv("RECOMMENDATION_HIGH_LOAD_THRESHOLD")
    result = recommend(monkeypatch, {"complexityFactor": "2.60x"})
    assert "Processing 100 documents/hour" in result[0]
    assert "High document complexity" in result[1]
    assert "could not be fully generated" in result[-1]
    assert "RECOMMENDATION_HIGH_LOAD_THRESHOLD" in result[-1]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("missing", "latency", "configs"),
    [
        ("RECOMMENDATION_HIGH_COMPLEXITY_THRESHOLD", {}, None),
        ("RECOMMENDATION_MEDIUM_COMPLEXITY_THRESHOLD", {}, None),
        ("RECOMMENDATION_HIGH_LOAD_THRESHOLD", {}, None),
        ("RECOMMENDATION_MEDIUM_LOAD_THRESHOLD", {}, None),
        ("RECOMMENDATION_HIGH_LATENCY_THRESHOLD", {}, None),
        ("RECOMMENDATION_LARGE_DOC_THRESHOLD", {}, [doc(pages=1)]),
        ("RECOMMENDATION_HIGH_PAGE_THRESHOLD", {}, [doc(pages=1)]),
    ],
)
def test_each_recommendation_threshold_is_required_and_named_when_absent(
    monkeypatch, missing, latency, configs
):
    """Seven separate variables, and any one of them truncates the advice.

    Each is checked individually rather than as a set, so the message names the one
    an operator has to add. The `LARGE_DOC` and `HIGH_PAGE` checks only run when
    there are document configurations, which is why those two rows supply one.
    """
    monkeypatch.delenv(missing)
    result = recommend(monkeypatch, latency, document_configs=configs)
    assert missing in result[-1]
    assert "could not be fully generated" in result[-1]


@pytest.mark.unit
def test_a_numeric_factor_rather_than_a_suffixed_string_degrades_the_advice(
    monkeypatch,
):
    """The factors are read as `"1.00x"` strings and `.rstrip("x")` is applied.

    A caller that passed a float would lose every recommendation after the
    headline. Pinned because the distribution and the recommendations are built by
    separate functions, so the string contract between them is easy to break.
    """
    result = recommend(monkeypatch, {"complexityFactor": 2.6})
    assert "could not be fully generated" in result[-1]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("pattern", "docs", "fragment"),
    [
        ("pattern-1", 101, "High volume BDA processing"),
        ("pattern-1", 100, None),
        ("pattern-3", 51, "High volume SageMaker classification"),
        ("pattern-3", 50, None),
        ("pattern-2", 100000, None),
    ],
)
def test_the_pattern_specific_advice_has_its_own_volume_thresholds(
    monkeypatch, pattern, docs, fragment
):
    """Reachable only by calling this function directly.

    `lambda_handler` rejects everything but `pattern-2` and `unified` before
    reaching here, so neither branch can fire in the deployed Lambda. Pinned
    anyway, because the thresholds are the only record of what they were for.
    """
    text = " ".join(recommend(monkeypatch, pattern=pattern, docs_per_hour=docs))
    if fragment is None:
        assert "High volume" not in text
    else:
        assert fragment in text
