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

import ast
import inspect
import json
import pathlib
import textwrap
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
    # A region and dummy credentials, because three of the quota tests construct a real
    # Service Quotas client before failing on the mapping. A developer machine supplies a
    # region from ~/.aws/config and CI does not, so without these the file passes locally
    # and fails in CI with "You must specify a region" -- and the repo's hermeticity gate
    # does not catch it, because that gate asserts a suite COLLECTS without a region, and
    # collection is not where a client is built. Set here rather than in the three tests
    # so the whole module is region-independent by construction.
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",  # nosec B105 - dummy moto credential
    "AWS_SESSION_TOKEN": "testing",  # nosec B105 - dummy moto credential
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
def test_the_fuzzy_match_keeps_the_region_prefix_that_selects_the_quota(monkeypatch):
    """A `us.` and a `global.` inference profile are separate Service Quotas entries.

    The production configuration lists both `us.anthropic.claude-sonnet-5`
    (`L-D4FBCF4E`) and `global.anthropic.claude-sonnet-5` (`L-DD84E5CA`) precisely
    because their limits are held separately, so the prefix has to survive the
    cleaning step: an id reaching the fuzzy path must match only entries for its
    own inference profile, whatever order the mapping happens to be in.

    Asserted because the failure it guards is a confidently wrong number rather
    than an error: the report would read a real quota from a real API for the
    wrong inference profile and print "✅ Sufficient" against a limit the
    workload will never be measured by.

    Both directions are checked so that the result cannot come from dictionary
    order — cleaning that discarded the prefix would reduce both keys to
    `claude-sonnet-5` and return whichever came first for both ids.
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
    global_model = "global.anthropic.claude-sonnet-5-20260401-v1:0"
    us_model = "us.anthropic.claude-sonnet-5-20260401-v1:0"
    codes = index.generate_rpm_quota_codes([global_model, us_model])
    assert codes[global_model] == "L-DD84E5CA"
    assert codes[us_model] == "L-D4FBCF4E"


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
def test_the_per_model_quotas_are_carried_through_uncollapsed(monkeypatch):
    """No account-wide scalar is published beside the per-model mappings.

    This function used to add one, as the *maximum* across the models, and the
    throughput estimate divided by it — so a plan was costed against the
    account's most generously provisioned model whatever models its steps
    actually called. Nothing reduces the mapping here now; the two consumers
    reduce it themselves, `calculate_latency_distribution` to the binding minimum
    and `build_simple_quota_requirements` to each step's own model.

    The absence is asserted rather than left implicit because a collapsed scalar
    is a silent failure: reintroducing one produces a plausible number from real
    quotas read from a real API, with no error anywhere. The three TPM values are
    kept ordered so that `min`, `max`, `sum` and "first entry" all give a
    different answer, so a reintroduction cannot coincide with any of them.
    """
    quotas, _ = run_get_simple_quotas(
        monkeypatch,
        {"a": "L-A", "b": "L-B", "c": "L-C"},
        {"a": "L-RA", "b": "L-RB", "c": "L-RC"},
        {"L-A": 100000, "L-B": 900000, "L-C": 400000},
        {"L-RA": 50, "L-RB": 250, "L-RC": 120},
    )
    assert quotas["bedrock_models"] == {"a": 100000, "b": 900000, "c": 400000}
    assert quotas["bedrock_models_rpm"] == {"a": 50, "b": 250, "c": 120}
    assert set(quotas) == {"bedrock_models", "bedrock_models_rpm"}


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


def quotas(tpm=60000, rpm=50, extra_tpm=None):
    """The shape `get_simple_quotas` returns: per-model mappings and nothing else.

    `extra_tpm` adds further models to the TPM mapping, which is how a fixture
    makes the reduction over it observable — with one entry every reduction
    (`min`, `max`, `sum`, "first") returns the same number.
    """
    tpm_by_model = {"nova": tpm}
    tpm_by_model.update(extra_tpm or {})
    return {
        "bedrock_models": tpm_by_model,
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
    pattern="pattern-2",
    model_config=None,
):
    if tokens is None:
        tokens = docs_per_hour * 6000
    monkeypatch.setattr(
        index,
        "get_real_latency_metrics",
        lambda _p, _hours=None: metrics or latency_data(),
    )
    return index.calculate_latency_distribution(
        docs_per_hour,
        docs_per_hour * 3,
        tokens,
        pattern,
        max_latency,
        quota or quotas(),
        document_configs,
        latency_metrics_hours=None,
        model_config=model_config,
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
def test_the_token_limit_is_the_most_constrained_model_not_the_best_provisioned(
    monkeypatch, capsys
):
    """Throughput is capped by the narrowest TPM quota the plan can reach.

    The account here holds three different model limits, and the reported
    throughput has to come from the smallest of them: a document that touches a
    60,000 TPM model is throttled there regardless of how much headroom some
    other model has. The figure an operator compares against their arrival rate
    is `processingRate`, and `quotaUtilization` is derived from the same capacity,
    so both are asserted.

    The three values are chosen so that every plausible reduction gives a
    different answer at 6,000 tokens a document — 60,000 (min) is 10 docs/min,
    300,000 (the first entry) is 50, 600,000 (max) is 100 and their sum is 160 —
    which is what makes this test able to fail. Costing the plan against the
    largest, as an account-wide ceiling amounts to, claims ten times the
    throughput the account has, and the RPM limit is set far out of the way so
    that the TPM half is unambiguously what is being measured.
    """
    result = distribution(
        monkeypatch,
        docs_per_hour=60,
        tokens=60 * 6000,
        quota=quotas(
            tpm=300000, rpm=100000, extra_tpm={"narrow": 60000, "wide": 600000}
        ),
    )
    assert result["processingRate"] == "10 docs/min"
    assert result["quotaUtilization"] == "10.0%"
    # Which model binds is the actionable half: the operator's next step is a
    # quota increase request, and it has to name one model. The number alone does
    # not say which of the three to ask for.
    assert "60000 TPM for narrow" in capsys.readouterr().out


@pytest.mark.unit
def test_the_request_limit_is_also_the_most_constrained_model(monkeypatch):
    """The RPM half of the same `min` is reduced the same way, and is measured.

    Stated because the two halves disagreed for a long time — the token limit was
    taken from an account-wide ceiling that collapsed the per-model quotas to
    their *maximum* while this one took the minimum — so "both take the binding
    model" is a claim worth an assertion rather than a comment. Three RPM values
    again, ordered so that min, max, sum and first entry differ: 3 (min) is the
    answer, 40 would be the first entry, 120 the max and 163 their sum, against a
    token limit held far out of the way at 10,000 documents a minute.
    """
    quota = quotas(tpm=60_000_000, rpm=40)
    quota["bedrock_models_rpm"].update({"narrow": 3, "wide": 120})
    result = distribution(monkeypatch, docs_per_hour=60, tokens=60 * 6000, quota=quota)
    assert result["processingRate"] == "3 docs/min"


@pytest.mark.unit
def test_only_the_models_the_plan_calls_constrain_its_throughput(monkeypatch, capsys):
    """The reduction is over the plan's step models, not the whole account mapping.

    `BEDROCK_MODEL_QUOTA_CODES` lists every model the stack supports — fourteen on
    a default deployment — so reducing over all of them answers a question nobody
    asked: it reports the limit of whichever model the account is least
    provisioned for, whether or not the pipeline ever calls it. That is the mirror
    image of the account-wide maximum this replaced, understating instead of
    overstating, and it sends the operator to request an increase for a model that
    is not in their pipeline.

    The fixture separates the two readings and every plausible variant of each. The
    account holds three models; the plan calls two of them, and the third —
    `tiny`, at 6,000 TPM — is the narrowest in the account and is called by no
    step. At 6,000 tokens a document the answers are: 10 docs/min over the plan's
    binding model (`narrow`, asserted), 1 over the account's binding model, 100
    over either maximum *and* over the plan's first step's model, 110 over the
    plan's sum and 111 over the account's. Classification is given the *generous*
    model precisely so that the plan's first step and the plan's binding step are
    different.
    """
    quota = quotas(tpm=600000, rpm=100000, extra_tpm={"narrow": 60000, "tiny": 6000})
    result = distribution(
        monkeypatch,
        docs_per_hour=60,
        tokens=60 * 6000,
        quota=quota,
        model_config={"classification_model": "nova", "extraction_model": "narrow"},
    )
    assert result["processingRate"] == "10 docs/min"
    assert "60000 TPM for narrow" in capsys.readouterr().out


@pytest.mark.unit
def test_a_planned_model_with_the_local_1m_suffix_finds_its_base_quota(monkeypatch):
    """Resolution goes through `_lookup_quota`, as the step-level builder's does.

    The `:1m` suffix is a local convention for a 1M context window and shares the
    base model's Service Quotas entry, so a plan naming the suffixed id must find
    the base entry. Failing to would drop the model from the reduction and silently
    fall back to the whole account mapping, which here would report the 6,000 TPM
    of a model the plan does not call — a plausible wrong number rather than an
    error.
    """
    quota = quotas(tpm=600000, rpm=100000, extra_tpm={"tiny": 6000})
    result = distribution(
        monkeypatch,
        docs_per_hour=60,
        tokens=60 * 6000,
        quota=quota,
        model_config={"extraction_model": "nova:1m"},
    )
    assert result["processingRate"] == "100 docs/min"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("pattern", "model_config"),
    [
        ("pattern-2", None),  # an older caller that passes no configuration
        ("pattern-1", {"extraction_model": "nova"}),  # a step pattern-1 never runs
        ("pattern-2", {"ocr_model": "   "}),  # whitespace-only means Textract
    ],
)
def test_a_plan_that_names_no_bedrock_model_falls_back_to_the_full_mapping(
    monkeypatch, pattern, model_config, capsys
):
    """With no step to narrow by, the account-wide binding minimum is the answer.

    Pessimistic rather than wrong, and it is the only answer available without
    knowing the steps. Asserted at 1 doc/min — the 6,000 TPM `tiny` model — rather
    than at the 600,000 TPM of `nova`, because the failure worth catching is
    falling back to something optimistic.

    The absence of the "no usable TPM quota" warning is asserted as well, and it is
    what separates these three cases from the different one below: a plan that
    *does* name models, none of which the mapping can price. Both end at the same
    reduction over the same mapping, so the reported rate alone cannot tell them
    apart — the OCR case is the one that needs it, since a whitespace-only value
    treated as a model id would reach the mapping, resolve to nothing, and arrive
    at this same figure by the other route.
    """
    quota = quotas(tpm=600000, rpm=100000, extra_tpm={"tiny": 6000})
    result = distribution(
        monkeypatch,
        docs_per_hour=60,
        tokens=60 * 6000,
        quota=quota,
        pattern=pattern,
        model_config=model_config,
    )
    assert result["processingRate"] == "1 docs/min"
    assert "No usable TPM quota" not in capsys.readouterr().out


@pytest.mark.unit
def test_planned_models_the_quota_mapping_cannot_price_fall_back_and_say_so(
    monkeypatch, capsys
):
    """A model missing from the mapping is skipped, not a refusal, and it is logged.

    `build_simple_quota_requirements` raises on exactly this case a few steps
    later, naming the model and the environment variable to add it to, so refusing
    here would replace a specific message with a vaguer one. The estimate therefore
    falls back to the account-wide minimum — and says that it did, because a
    silently pessimistic figure is the harder kind of wrong to notice.
    """
    quota = quotas(tpm=600000, rpm=100000, extra_tpm={"tiny": 6000})
    result = distribution(
        monkeypatch,
        docs_per_hour=60,
        tokens=60 * 6000,
        quota=quota,
        model_config={"extraction_model": "a-model-nobody-mapped"},
    )
    assert result["processingRate"] == "1 docs/min"
    printed = capsys.readouterr().out
    assert "No usable TPM quota" in printed
    assert "a-model-nobody-mapped" in printed


def model_config_keys_the_step_builder_reads():
    """Every `model_config` key `build_simple_quota_requirements` looks up.

    Read from that function's source rather than from `STEP_MODEL_CONFIG_KEYS`,
    because it is the independent side of the comparison below: a universe taken
    from the constant under test cannot notice a key leaving it.
    """
    source = textwrap.dedent(inspect.getsource(index.build_simple_quota_requirements))
    keys = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "model_config"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)
    return keys


@pytest.mark.unit
def test_the_divergence_check_actually_consults_the_independent_derivation():
    """The union is what closes the check; its presence is pinned in source.

    `test_the_plan_model_set_agrees_with_the_step_level_quota_builder` builds its
    candidate universe as the union of `STEP_MODEL_CONFIG_KEYS`' values and the keys
    derived from the builder's source, and the derived half is what stops the check
    reading one dictionary on both sides. Collapsing that union back to the constant
    alone reopens a live defect — a config key re-pointed at another step's model
    drops a model from the plan set entirely — with the whole suite green.

    ⚠️ **Asserted against source because no input detects the collapse itself.**
    The two halves of the union are *equal* on the tree as it stands — the builder
    reads exactly the five keys the constant names — so a collapse changes no value
    any assertion can read, and the derived half's present worth is entirely
    insurance against a divergence that does not exist yet. Insurance that is never
    consulted is the shape this whole test group exists to catch.

    This does **not** make the behavioural assertion in that test redundant, and the
    two are deliberately kept side by side: `assert derived <= set(keys)` cannot see
    a collapse on its own, but it does refuse every form of the collapse once a key
    mapping is wrong — which is the case the collapse would otherwise let through —
    whereas this test refuses the collapse and cannot see the union *bypassed*
    rather than deleted. Neither dominates; do not delete one as covered by the
    other.

    Delete this test if the two ever genuinely differ, and assert the difference
    instead — that is the stronger check, and it will be available then.
    """
    this_file = pathlib.Path(__file__).read_text(encoding="utf-8")
    target = "test_the_plan_model_set_agrees_with_the_step_level_quota_builder"
    body = next(
        node
        for node in ast.walk(ast.parse(this_file))
        if isinstance(node, ast.FunctionDef) and node.name == target
    )

    # Two conditions, because the chain has two links and each can be broken on its
    # own: the derivation must be *called*, and its result must *reach* the
    # universe. Scoped to the two assignments rather than to the whole function,
    # since a call whose result is discarded, or one parked in a nested helper
    # nobody invokes, keeps the name present while losing its effect — both measured
    # green over a live wrong answer before this was scoped. Accepting either
    # assignment as the site of the call is not enough either: that lets the call
    # stay in `derived` while `| derived` leaves the universe.
    def assignment_to(name):
        return next(
            (
                node
                for node in ast.walk(body)
                if isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
            ),
            None,
        )

    derivation = assignment_to("derived")
    universe = assignment_to("keys")
    assert derivation is not None and universe is not None, (
        f"{target} no longer assigns both `derived` and `keys`, so this check "
        "cannot locate the derivation or the universe it must feed"
    )
    called = {
        node.func.id
        for node in ast.walk(derivation)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    reaches_universe = any(
        isinstance(node, ast.Name) and node.id == "derived"
        for node in ast.walk(universe)
    )
    assert reaches_universe, (
        f"{target} assigns `derived` but its candidate universe no longer reads it, "
        "so the universe comes from the constant the check is meant to be "
        "independent of"
    )
    assert "model_config_keys_the_step_builder_reads" in called, (
        f"{target} no longer consults the independent derivation of the builder's "
        "model keys, so its candidate universe comes from the constant it checks"
    )


@pytest.mark.unit
def test_the_source_derivation_of_the_builders_model_keys_is_not_vacuous():
    """An empty derivation would make the divergence test below trivially true.

    Pinned as a lower bound rather than an equality, so adding a Bedrock step to
    the builder does not fail here — it fails in the comparison below, which is
    where the divergence actually is.
    """
    keys = model_config_keys_the_step_builder_reads()
    assert {"classification_model", "extraction_model", "ocr_model"} <= keys


@pytest.mark.unit
@pytest.mark.parametrize("pattern", ["pattern-1", "pattern-2", "pattern-3"])
def test_the_plan_model_set_agrees_with_the_step_level_quota_builder(pattern):
    """Two functions now decide which models a plan calls; they must not diverge.

    `plan_model_ids` answers it for the throughput reduction and
    `build_simple_quota_requirements` answers it for the per-step quota table, and
    a step added to one and not the other is a silent divergence — the table would
    report a requirement for a model the throughput estimate ignores.

    The step-level answer is **derived by running that function**, not by reading
    it: for each candidate model key in turn it is given a configuration setting
    only that key and an empty quota mapping. A key the pattern uses reaches
    `_lookup_quota`, finds nothing and raises naming the model; a key it does not
    use leaves the demand set empty and raises nothing.

    ⚠️ **The empty quota mapping is what keeps this probe free of AWS**, and it is
    load-bearing rather than incidental: `build_simple_quota_requirements`
    constructs a `boto3.resource("dynamodb")` once per selected step, before it
    even reads `METERING_TABLE_NAME`, and the probe never reaches that only because
    `_lookup_quota` raises first. Giving the probe a real mapping to "improve" it
    would silently make it need credentials and a table.

    ⚠️ **The candidate universe must not come from the constant under test.** Taking
    it from `STEP_MODEL_CONFIG_KEYS` alone makes the comparison read one dictionary
    on both sides, so a key removed from it, or re-pointed at another step's key,
    leaves the probe's universe at the same moment it leaves the expectation and
    the test stays green. Measured, on the pattern the handler actually accepts:
    re-pointing `"Assessment"` at `"extraction_model"` drops the assessment model
    from the plan set entirely, so it can never constrain the estimate — 5 docs/min
    becomes 50 — and the whole suite stayed green. The universe is therefore the
    **union** of the keys that constant names and the keys the builder's own source
    reads, which closes the comparison in both directions: a key only the builder
    uses is probed and found missing from the expectation, and a key only the
    constant names is probed, found unused, and missing from the derived set.
    """
    derived = model_config_keys_the_step_builder_reads()
    keys = sorted(set(index.STEP_MODEL_CONFIG_KEYS.values()) | derived)
    # The complement of the source assertion above: it refuses *deletion* of the
    # union and cannot see it *bypassed*, while this refuses every form of the
    # collapse the moment a key mapping is wrong — which is when it matters. The two
    # halves become unequal exactly then, so on unmutated source this holds
    # trivially and its value is entirely in the mutated case.
    assert derived <= set(keys)
    used_by_builder = set()
    for key in keys:
        config = {key: f"model-for-{key}"}
        try:
            index.build_simple_quota_requirements(
                50000,
                500000,
                1000,
                {"bedrock_models": {}},
                600.0,
                pattern,
                config,
                [
                    {
                        f"{stage}TokensPerHour": 60000
                        for stage in (
                            "ocr",
                            "classification",
                            "extraction",
                            "assessment",
                            "summarization",
                        )
                    }
                ],
            )
        except ValueError as exc:
            assert f"model-for-{key}" in str(exc)
            used_by_builder.add(key)

    from_plan_helper = {
        index.STEP_MODEL_CONFIG_KEYS[step]
        for step in index.PATTERN_BEDROCK_STEPS[pattern]
    }
    assert from_plan_helper == used_by_builder
    # Non-vacuity: a probe that raised for nothing would make the comparison
    # trivially true between two empty sets.
    assert used_by_builder


@pytest.mark.unit
def test_one_model_with_no_quota_at_all_does_not_bind_the_whole_plan(monkeypatch):
    """A zero TPM entry is left out of the reduction rather than allowed to bind it.

    The account's mapping lists every model the deployment can be configured to
    use, not the ones a given plan calls, so a model the account cannot call at
    all must not take the capacity report down with it — and the maximum this
    reduction replaced could not, because a zero never wins a maximum. Preserving
    that is the point: switching to the minimum without this filter would refuse
    every plan on such an account, a failure this change has no business
    introducing.

    The binding value is therefore 60,000 and not 0, and the surviving refusal is
    the all-zero mapping, asserted below so that "left out" cannot become "never
    refused".
    """
    result = distribution(
        monkeypatch,
        docs_per_hour=60,
        tokens=60 * 6000,
        quota=quotas(tpm=60000, rpm=100000, extra_tpm={"unavailable": 0}),
    )
    assert result["processingRate"] == "10 docs/min"

    with pytest.raises(ValueError, match="TPM quota not available"):
        index.calculate_latency_distribution(
            600,
            1800,
            3_600_000,
            "pattern-2",
            600.0,
            quotas(tpm=0, rpm=100000, extra_tpm={"unavailable": 0}),
            [],
        )


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

    def must_not_be_called(_pattern, _hours=None):
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
    """32s against a 32s SLA is inside it, not outside: the comparison is `>`.

    The boundary is the whole subject here, so the distribution is flat — every
    percentile is 32s — and the test says nothing about which percentile the
    comparison reads. That separation is deliberate: whichever statistic defines
    the SLA, landing exactly on the limit must not be reported as a breach, and
    a fixture with a spread in it would couple the two questions and lose this
    one the next time the statistic changes.

    The neighbouring one-step-past test does not cover this: it is past the
    limit, not on it, and an off-by-one that turned `>` into `>=` would leave it
    green.
    """
    metrics = latency_data(
        total=30.0,
        percentiles={"p50": 30.0, "p99": 30.0, "count": 40},
        queue={"p50": 2.0, "p99": 2.0, "count": 40},
    )
    result = distribution(monkeypatch, metrics=metrics, max_latency=32.0)
    assert result["p50"] == result["p99"] == "32.0s"  # flat: no spread to read
    assert result["exceedsLimit"] is False
    assert "warningMessage" not in result
    assert result["maxAllowed"] == "32.0s"


@pytest.mark.unit
def test_a_fast_median_with_a_slow_tail_breaches_the_sla(monkeypatch):
    """The SLA is judged on P99, so a slow tail breaches it on a fast median.

    An SLA is a promise about the documents that go slowly, and the median cannot
    express it: comparing the P50 reports a plan as compliant when one document in
    a hundred takes twenty times the limit, which is the shape of a real
    deployment with an occasional very large packet.

    The fixture is skewed on purpose — a 32s P50 exactly at the limit against a
    603s P99 — so the two statistics cannot coincide. With them equal the test
    could not tell the two comparisons apart. The P50 sits *on* the boundary
    rather than under it so that the median reading gives `False` here by the
    boundary rule above, making this the strictest position from which the P99
    reading can be distinguished.

    The message has to carry both figures: the gap between them is what tells an
    operator to chase a slow tail rather than a slow pipeline.
    """
    metrics = latency_data(
        total=30.0,
        percentiles={"p50": 30.0, "p99": 600.0, "count": 40},
        queue={"p50": 2.0, "p99": 3.0, "count": 40},
    )
    result = distribution(monkeypatch, metrics=metrics, max_latency=32.0)
    assert result["p50"] == "32.0s"
    assert result["p99"] == "603.0s"
    assert result["exceedsLimit"] is True
    assert (
        "P99 processing time (10.1min) exceeds SLA (0.5min)"
        in (result["warningMessage"])
    )
    assert "the median is 0.5min" in result["warningMessage"]


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
        (
            {"bedrock_models": {}, "bedrock_models_rpm": {"n": 1}},
            "TPM quota not available",
        ),
        (
            {"bedrock_models": {"n": 1000}, "bedrock_models_rpm": {}},
            "RPM quotas not available",
        ),
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

    def no_documents(_pattern, _hours=None):
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
    """The two are independent: a fast document can still miss a tight SLA.

    The advice names the percentile it is about, matched in full here rather than
    on the bare phrase "exceeds SLA": the flag is set by comparing the P99 and a
    sentence that does not say so leaves an operator looking at a median that
    sits comfortably inside their limit with no idea why they are being warned.
    """
    text = " ".join(recommend(monkeypatch, {"exceedsLimit": True, "p99": "10.0s"}))
    assert "P99 processing time exceeds SLA" in text
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
def test_the_variance_advice_fires_just_past_a_threefold_spread(monkeypatch, factor):
    """The boundary is strictly above 3.0, asserted from both sides of it.

    `3.00x` is at the threshold and not over it, so it must stay quiet; `3.01x` is
    over and must speak. Those two strings are written here rather than derived
    from `index.HIGH_LATENCY_VARIANCE_FACTOR`, so retuning the constant reddens
    this test instead of moving its expectation along with the code. Unlike the
    `RECOMMENDATION_*` bands this one is a fixed constant on purpose — it gates a
    sentence of advice and no reported figure, and the reasoning is recorded where
    it is defined.
    """
    text = " ".join(recommend(monkeypatch, {"varianceFactor": factor}))
    assert ("High latency variance" in text) is (factor == "3.01x")


@pytest.mark.unit
def test_the_variance_threshold_is_a_named_constant_at_three():
    """Names the value independently of the behaviour asserted above.

    The pair is what makes either useful: this one fails if the constant is
    retuned, the boundary test fails if the comparison stops honouring it, and a
    rename that left a stray literal `3.0` behind in the comparison would fail the
    boundary test while this one still passed.
    """
    assert index.HIGH_LATENCY_VARIANCE_FACTOR == 3.0


@pytest.mark.unit
def test_the_headline_names_the_volume_and_the_pattern(monkeypatch):
    first = recommend(monkeypatch, docs_per_hour=2500)[0]
    assert "Processing 2500 documents/hour using PATTERN-2" in first


DATA_SOURCE_FIELD_NAMES = frozenset({"dataSource", "data_source"})


def data_source_binding_sites():
    """Every place `index.py` binds a value to the `dataSource` field.

    The point of walking the source rather than listing the values is that a list
    is what the prose mapping itself used to be: it recognised one spelling, and
    the check that it was complete was somebody reading it. But collecting the
    shapes that *are* recognised only moves that problem — it makes the gate as
    complete as the list of shapes, and a producer written in a shape nobody
    thought of passes silently. So the rule is inverted: this finds every binding
    site, and each one's value must be **readable**. A value this cannot read is
    a failure naming the line, not a site quietly skipped.

    Two value shapes are readable:

    * a string literal, which contributes that value; and
    * a bare `data_source`/`dataSource` name, which *forwards* a value the
      literals above already account for — `{"dataSource": data_source}` is how
      both result dicts publish the field, so this has to be allowed.

    Anything else — an f-string, a `%` or `+` expression, a conditional
    expression, a call, or a name that is not the tracked variable — is
    unreadable. Following those would mean interpreting the module rather than
    reading it, and the honest answer is to refuse: `chosen = "x"` followed by
    `{"dataSource": chosen}` is exactly the shape that would otherwise slip a new
    source past the gate, and `"..." if flag else "..."` is the shape this module
    is most likely to grow next, since it already picks between two sources on a
    condition.

    Returns `(values, unreadable)`: a mapping of value to the `(line, shape)`
    sites producing it, and a list of `(line, shape, dump)` for sites whose value
    could not be read.
    """
    tree = ast.parse(
        pathlib.Path(index.__file__).read_text(encoding="utf-8"),
        filename=index.__file__,
    )

    def is_field_literal(node):
        """True if this node is a string literal naming the dataSource field."""
        return (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in DATA_SOURCE_FIELD_NAMES
        )

    def names_the_field(node):
        """True if this target or key designates the dataSource field."""
        if isinstance(node, ast.Name):
            return node.id in DATA_SOURCE_FIELD_NAMES
        if is_field_literal(node):
            return True
        if isinstance(node, ast.Subscript):
            return is_field_literal(node.slice)
        return False

    def reads_the_field(node):
        """True if this node reads the field back out of a mapping.

        `data_source = latency_data.get("data_source", "unknown")` binds the
        variable from a *read*, and the only literal that read can yield — the
        `.get` default — is collected at the call itself, so the assignment
        forwards a value already accounted for rather than introducing one.

        Only a **literal** first argument counts.
        `DATA_SOURCE_DESCRIPTIONS.get(data_source, ...)` is a lookup *keyed by*
        the source, whose default is prose rather than a source value; treating
        that as a binding would collect the prose as though it were a
        `dataSource`, and its f-string default as an unreadable one.
        """
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "setdefault"}
            and len(node.args) == 2
            and is_field_literal(node.args[0])
        )

    values: dict[str, list[tuple[int, str]]] = {}
    unreadable: list[tuple[int, str, str]] = []

    def record(value_node, line, shape):
        if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
            values.setdefault(value_node.value, []).append((line, shape))
        elif (
            isinstance(value_node, ast.Name)
            and value_node.id in DATA_SOURCE_FIELD_NAMES
        ):
            pass  # forwards the tracked variable; its own literals are collected
        elif reads_the_field(value_node):
            pass  # reads the field back; that read's own default is collected below
        else:
            unreadable.append((line, shape, ast.dump(value_node)[:120]))

    def bind(target, value_node, line, shape):
        """Bind one target to one value, unpacking a same-length tuple pairwise."""
        if isinstance(target, (ast.Tuple, ast.List)):
            elements = target.elts
            if isinstance(value_node, (ast.Tuple, ast.List)) and len(
                value_node.elts
            ) == len(elements):
                for element, element_value in zip(elements, value_node.elts):
                    bind(element, element_value, line, shape)
            elif any(names_the_field(element) for element in elements):
                unreadable.append(
                    (
                        line,
                        f"{shape} (unpaired tuple target)",
                        ast.dump(value_node)[:120],
                    )
                )
            return
        if names_the_field(target):
            record(value_node, line, shape)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                bind(target, node.value, node.lineno, "assignment")
        elif isinstance(node, ast.AnnAssign):
            if node.value is not None:
                bind(node.target, node.value, node.lineno, "annotated assignment")
        elif isinstance(node, ast.NamedExpr):
            bind(node.target, node.value, node.lineno, "walrus")
        elif isinstance(node, ast.AugAssign):
            if names_the_field(node.target):
                unreadable.append(
                    (node.lineno, "augmented assignment", ast.dump(node.value)[:120])
                )
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if key is not None and names_the_field(key):
                    record(value, key.lineno, "dict literal")
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in DATA_SOURCE_FIELD_NAMES:
                    record(keyword.value, node.lineno, "call keyword")
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in {"get", "setdefault"}
                and len(node.args) == 2
                and is_field_literal(node.args[0])
            ):
                record(node.args[1], node.lineno, f".{func.attr} default")
    return values, unreadable


def data_sources_this_module_can_produce():
    """The readable half of `data_source_binding_sites`, for the checks below."""
    values, _ = data_source_binding_sites()
    return values


@pytest.mark.unit
def test_the_derivation_of_producible_sources_is_not_vacuous():
    """The authority the checks below rest on is not empty.

    Without this, a walker that silently matched nothing — a renamed field, an
    `ast` API change, a `Constant` check that rejects every node — would leave
    the checks below iterating an empty set and *passing*, which is the shape of
    a green mark that means nothing. The three shapes the module uses today are
    each asserted to have contributed, so the walker cannot degrade to
    recognising only the easiest one and still look complete.
    """
    values, _ = data_source_binding_sites()
    assert values, "derived no dataSource values at all"
    shapes = {shape for found in values.values() for _, shape in found}
    # A superset rather than an equality: the three shapes the module uses today
    # must each have been read, so the walk cannot degrade to recognising only the
    # easiest one, but a legitimate fourth shape appearing later is not a failure
    # *here* — it is covered by the closure check below, which is the test that
    # should speak about shapes.
    assert shapes >= {"assignment", "dict literal", ".get default"}, shapes


@pytest.mark.unit
def test_no_data_source_is_bound_by_a_shape_the_derivation_cannot_read():
    """The gate is closed by refusing what it cannot read, not by listing shapes.

    This is the assertion that makes "exhaustive" mean something. Collecting the
    recognised shapes and ignoring the rest would make the mapping's closure only
    as good as the list of shapes, so a source introduced as `f"..."`, as
    `"a" if flag else "b"`, via `dict(dataSource=...)`, through a subscript, or
    through an intermediate variable would pass every check here while rendering
    as an unrecognised source in the report. Each of those was measured to slip
    through a shape-collecting version of this walk.

    So an unreadable binding fails here and names its line. The remedy is either
    to write the value as a literal, or — if it genuinely has to be computed — to
    widen this walk deliberately, which is a decision someone makes rather than
    one a new syntax makes for them.
    """
    _, unreadable = data_source_binding_sites()
    assert not unreadable, (
        "these sites bind a dataSource value this walk cannot read, so the prose "
        "mapping's closure says nothing about them — make the value a literal or "
        f"widen `data_source_binding_sites`: {unreadable}"
    )


@pytest.mark.unit
def test_every_source_this_module_can_produce_has_prose_and_no_prose_is_unproducible():
    """The mapping is closed against the module, in both directions.

    Forwards: a source added to `index.py` without an entry here is what put
    "(based on unknown source)" on the most trustworthy plan the planner can
    build, so a new one fails this rather than reaching an operator.

    Backwards, which is the half that catches the original defect at its root:
    an entry nothing can produce is dead, and a mapping allowed to carry dead
    entries is one whose coverage cannot be read off it. `"environment_config"`
    was the only value the prose recognised and nothing has ever assigned it, so
    the mapping looked populated while describing no reachable plan at all.
    """
    sites = data_sources_this_module_can_produce()
    described = set(index.DATA_SOURCE_DESCRIPTIONS)
    undescribed = {value: sites[value] for value in set(sites) - described}
    assert not undescribed, (
        "index.py can produce these dataSource values and "
        f"DATA_SOURCE_DESCRIPTIONS has no prose for them: {undescribed}"
    )
    unproducible = described - set(sites)
    assert not unproducible, (
        "DATA_SOURCE_DESCRIPTIONS describes values nothing in index.py assigns, "
        f"so they can never be read by an operator: {sorted(unproducible)}"
    )


# What each source must be *called*, written out here rather than read back out of
# `index.DATA_SOURCE_DESCRIPTIONS`. Reading the expectation from the mapping makes
# the keys closed and the prose circular: swapping the descriptions of
# `document_timestamps` and `real_lambda_durations` — which mislabels the
# provenance of every plan the planner builds, the same defect this item is about —
# was measured to leave the whole suite green. The wording is what an operator acts
# on, so it is pinned, and the cost is deliberate: adding a source means editing
# this table, which is the point at which its wording gets read by someone.
EXPECTED_SOURCE_PROSE = {
    "document_timestamps": "measured document timestamps",
    "real_lambda_durations": "measured Lambda durations",
    "no_demand": "no configured processing demand",
    "unknown": "a source the measurement step did not report",
}


@pytest.mark.unit
def test_the_expected_prose_table_covers_exactly_the_shipped_mapping():
    """The pinned wording and the shipped mapping describe the same sources.

    Without this the table above could drift out of step with `index.py` — an
    entry renamed there and not here would simply stop being checked, which is the
    quiet direction. Compared as whole dicts so that a changed *description* fails
    here too, rather than only a changed key.
    """
    assert index.DATA_SOURCE_DESCRIPTIONS == EXPECTED_SOURCE_PROSE


@pytest.mark.unit
@pytest.mark.parametrize("data_source", sorted(EXPECTED_SOURCE_PROSE))
def test_each_described_source_reaches_the_headline_as_its_prose(
    monkeypatch, data_source
):
    """Having an entry is not the same as the entry being used.

    The closure checks above compare sets of keys and would pass over a mapping
    the headline never consults, so every entry is also driven through the
    function and found in the text — against the literal wording above rather
    than against whatever the mapping happens to say.
    """
    first = recommend(monkeypatch, {"dataSource": data_source})[0]
    assert f"(based on {EXPECTED_SOURCE_PROSE[data_source]})" in first


@pytest.mark.unit
def test_a_source_from_outside_this_module_is_named_rather_than_called_unknown(
    monkeypatch,
):
    """The fallback is diagnosable, and it is reachable.

    This function takes the distribution as an argument, so a value the mapping
    does not hold can arrive from a caller however closed the mapping is against
    `index.py` — which is why the fallback is not dead code and why it names the
    value verbatim. Flattening it to "unknown source" was what made the original
    defect invisible: the field was carrying `"document_timestamps"` all along
    and nothing in the report said so.

    Asserted with a value chosen to be absent from the mapping, and that absence
    is asserted rather than assumed, so the case cannot quietly start testing
    the mapped path if the spelling is ever adopted.
    """
    novel = "a_source_added_after_this_test_was_written"
    assert novel not in index.DATA_SOURCE_DESCRIPTIONS
    first = recommend(monkeypatch, {"dataSource": novel})[0]
    assert f"(based on unrecognised source '{novel}')" in first


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
