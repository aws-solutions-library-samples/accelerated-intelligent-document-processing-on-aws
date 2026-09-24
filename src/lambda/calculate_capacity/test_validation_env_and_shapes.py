# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the parts of `validation.py` that decide *what an operator is told* when
the capacity planner cannot run: the per-variable environment checks and the per-field
request-shape checks.

This module is the only thing between a misconfigured stack or a malformed request and
a capacity report built on guesses. Its design decision is that it **collects** errors
rather than raising on the first one, so a deployment missing four environment
variables learns about all four in one attempt instead of one per redeploy. That
property is invisible in a single-defect test — first-error-only behaviour passes every
one of them — so it is asserted directly here, on both the environment and the input
side.

The complementary tests in `test_validation.py` cover the happy paths, the
size/type guards on `sanitize_json_input`, and one representative failure per
category. This module fills in the branches that produce a *specific* message: each of
the twelve required numeric variables by name, each of them with an unparseable value,
and each of the request-shape checks whose input is the wrong Python type rather than
an out-of-range value. Those are the messages an operator reads and acts on, and a
generic "validation failed" in place of any of them costs a support round trip.
"""

from __future__ import annotations

import json

import pytest
import validation
from validation import (
    ValidationError,
    validate_capacity_input,
    validate_required_env_vars,
)

VALID_ENV = {
    "TRACKING_TABLE": "tracking",
    "METERING_TABLE_NAME": "tracking",
    "LAMBDA_MEMORY_GB": "2.0",
    "MIN_TOKENS_PER_REQUEST": "1500",
    "RECOMMENDATION_HIGH_COMPLEXITY_THRESHOLD": "2.5",
    "RECOMMENDATION_MEDIUM_COMPLEXITY_THRESHOLD": "1.5",
    "RECOMMENDATION_HIGH_LOAD_THRESHOLD": "3.0",
    "RECOMMENDATION_MEDIUM_LOAD_THRESHOLD": "2.0",
    "RECOMMENDATION_HIGH_LATENCY_THRESHOLD": "300",
    "RECOMMENDATION_LARGE_DOC_THRESHOLD": "50000",
    "RECOMMENDATION_HIGH_PAGE_THRESHOLD": "20",
    "MEDIUM_COMPLEXITY_THRESHOLD": "800",
    "HIGH_COMPLEXITY_THRESHOLD": "2400",
    "PAGE_COMPLEXITY_FACTOR": "0.25",
    "HIGH_COMPLEXITY_MULTIPLIER": "3.0",
    "MEDIUM_COMPLEXITY_MULTIPLIER": "1.5",
    "BEDROCK_MODEL_QUOTA_CODES": json.dumps({"nova": "L-TPM"}),
    "BEDROCK_MODEL_RPM_QUOTA_CODES": json.dumps({"nova": "L-RPM"}),
}

# The variables whose value has to parse as a number, and the type name the error
# message must quote so an operator knows whether a decimal point is allowed.
NUMERIC_VARS = {
    "RECOMMENDATION_HIGH_COMPLEXITY_THRESHOLD": "float",
    "RECOMMENDATION_MEDIUM_COMPLEXITY_THRESHOLD": "float",
    "RECOMMENDATION_HIGH_LOAD_THRESHOLD": "float",
    "RECOMMENDATION_MEDIUM_LOAD_THRESHOLD": "float",
    "RECOMMENDATION_HIGH_LATENCY_THRESHOLD": "int",
    "RECOMMENDATION_LARGE_DOC_THRESHOLD": "int",
    "RECOMMENDATION_HIGH_PAGE_THRESHOLD": "int",
    "MEDIUM_COMPLEXITY_THRESHOLD": "int",
    "HIGH_COMPLEXITY_THRESHOLD": "int",
    "PAGE_COMPLEXITY_FACTOR": "float",
    "HIGH_COMPLEXITY_MULTIPLIER": "float",
    "MEDIUM_COMPLEXITY_MULTIPLIER": "float",
}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    """A fully valid environment, with the cold-start cache cleared."""
    for name, value in VALID_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(validation, "_validated_env_vars", None)
    yield monkeypatch
    monkeypatch.setattr(validation, "_validated_env_vars", None)


def message(payload=None):
    """The collected error text from validating `payload` (or the environment)."""
    with pytest.raises(ValidationError) as caught:
        if payload is None:
            validate_required_env_vars()
        else:
            validate_capacity_input(payload)
    return str(caught.value)


def base_input(**overrides):
    payload = {
        "pattern": "pattern-2",
        "maxAllowedLatency": 600,
        "documentConfigs": [{"type": "invoice", "avgPages": 3}],
    }
    payload.update(overrides)
    return payload


# ==========================================================================
# Environment variables
# ==========================================================================


@pytest.mark.unit
def test_a_fully_configured_environment_is_returned_parsed_not_just_accepted(env):
    """The parsed values are the ones the Lambda then uses, so their types matter.

    An `int` threshold left as a string compares lexically against a number and
    gives a silently wrong verdict, so the returned mapping is checked for value and
    type rather than only for the absence of an exception.
    """
    validated = validate_required_env_vars()
    assert validated["lambda_memory_gb"] == 2.0
    assert isinstance(validated["lambda_memory_gb"], float)
    assert validated["min_tokens_per_request"] == 1500
    assert isinstance(validated["min_tokens_per_request"], int)
    assert validated["recommendation_high_latency_threshold"] == 300
    assert isinstance(validated["recommendation_high_latency_threshold"], int)
    assert validated["page_complexity_factor"] == 0.25
    assert validated["bedrock_model_quota_codes"] == {"nova": "L-TPM"}
    assert validated["tracking_table"] == "tracking"
    assert validated["metering_table"] == "tracking"


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(VALID_ENV))
def test_every_required_variable_is_named_individually_when_it_is_missing(env, name):
    """Eighteen variables, eighteen distinct messages.

    A generic "configuration error" would leave an operator comparing the stack's
    environment against the source, so each is asserted to appear in the text by
    name.
    """
    env.delenv(name)
    assert name in message()


@pytest.mark.unit
@pytest.mark.parametrize(("name", "type_name"), sorted(NUMERIC_VARS.items()))
def test_an_unparseable_numeric_variable_quotes_the_type_and_the_bad_value(
    env, name, type_name
):
    """The value is echoed back, which is how a stray unit or quote gets spotted.

    `"20 pages"` and `"20"` differ only in what the operator typed, so the message
    has to show it.
    """
    env.setenv(name, "20 pages")
    text = message()
    assert f"{name} must be a valid {type_name}" in text
    assert "20 pages" in text


@pytest.mark.unit
def test_an_integer_threshold_rejects_a_decimal_value(env):
    """`int("2.5")` raises, so a decimal in an int-typed variable is caught.

    Worth pinning because the neighbouring `float` variables accept exactly this
    spelling, so the two look interchangeable in the template.
    """
    env.setenv("RECOMMENDATION_HIGH_PAGE_THRESHOLD", "20.5")
    assert "RECOMMENDATION_HIGH_PAGE_THRESHOLD must be a valid int" in message()


@pytest.mark.unit
@pytest.mark.parametrize("value", ["0", "-2"])
def test_a_non_positive_lambda_memory_is_refused(env, value):
    """Zero would make the `gb_seconds / memory` conversion divide by zero.

    Parsing succeeds for both of these, so the range check is a separate guard from
    the type check and needs its own test.
    """
    env.setenv("LAMBDA_MEMORY_GB", value)
    assert "LAMBDA_MEMORY_GB must be a positive number" in message()


@pytest.mark.unit
@pytest.mark.parametrize("value", ["0", "-100"])
def test_a_non_positive_token_floor_is_refused(env, value):
    """A zero floor divides into the TPM quota and reports infinite capacity."""
    env.setenv("MIN_TOKENS_PER_REQUEST", value)
    assert "MIN_TOKENS_PER_REQUEST must be a positive integer" in message()


@pytest.mark.unit
@pytest.mark.parametrize("value", ["1500.5", "lots"])
def test_an_unparseable_token_floor_is_distinguished_from_an_invalid_one(env, value):
    """ "Not an integer" and "not positive" are different fixes.

    A decimal is included because the floor is a token count and `1500.5` is the
    kind of thing that survives a copy from a `float` variable next to it.
    """
    env.setenv("MIN_TOKENS_PER_REQUEST", value)
    text = message()
    assert "MIN_TOKENS_PER_REQUEST must be a valid integer" in text
    assert value in text


@pytest.mark.unit
@pytest.mark.parametrize(
    "name", ["BEDROCK_MODEL_QUOTA_CODES", "BEDROCK_MODEL_RPM_QUOTA_CODES"]
)
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("[]", "must be a JSON object"),
        ('["nova"]', "must be a JSON object"),
        ("{}", "cannot be empty"),
        ("{nope}", "must be valid JSON"),
    ],
)
def test_a_quota_code_map_must_be_a_non_empty_json_object(env, name, value, expected):
    """A JSON array parses cleanly and is useless: there is nothing to look up by.

    All four shapes are checked on both variables, because the two blocks are
    separate copies of the same logic and a fix applied to one has been known to
    miss the other.
    """
    env.setenv(name, value)
    assert f"{name} {expected}" in message()


@pytest.mark.unit
def test_every_environment_problem_is_reported_in_one_pass(env):
    """First-error-only would cost one redeploy per missing variable.

    Four independent defects are introduced — two absent, one unparseable, one
    malformed JSON — and all four have to appear in the single message. This is the
    assertion that a refactor to an early `raise` would fail; no single-defect test
    above can see it.
    """
    env.delenv("TRACKING_TABLE")
    env.delenv("MEDIUM_COMPLEXITY_MULTIPLIER")
    env.setenv("RECOMMENDATION_HIGH_LATENCY_THRESHOLD", "five minutes")
    env.setenv("BEDROCK_MODEL_QUOTA_CODES", "{nope}")

    text = message()
    assert "TRACKING_TABLE environment variable is required" in text
    assert "MEDIUM_COMPLEXITY_MULTIPLIER environment variable is required" in text
    assert "RECOMMENDATION_HIGH_LATENCY_THRESHOLD must be a valid int" in text
    assert "BEDROCK_MODEL_QUOTA_CODES must be valid JSON" in text
    assert text.count("\n  - ") == 4


# ==========================================================================
# Request shape
# ==========================================================================


@pytest.mark.unit
def test_document_configs_given_as_a_string_are_refused_by_type(env):
    """A non-empty string passes the emptiness check and then has no `.get`.

    Without this guard the iteration below would walk the string a character at a
    time and report every character as a bad entry.
    """
    assert "documentConfigs must be a list, got str" in message(
        base_input(documentConfigs="invoice")
    )


@pytest.mark.unit
def test_a_document_config_entry_that_is_not_an_object_names_its_position(env):
    """The index is what lets an operator find the row in the UI."""
    payload = base_input(documentConfigs=[{"type": "invoice"}, "invoice", 7])
    text = message(payload)
    assert "documentConfigs[1] must be an object" in text
    assert "documentConfigs[2] must be an object" in text


@pytest.mark.unit
@pytest.mark.parametrize(
    "field",
    [
        "avgPages",
        "ocrTokens",
        "classificationTokens",
        "extractionTokens",
        "assessmentTokens",
        "summarizationTokens",
    ],
)
def test_every_numeric_document_field_rejects_a_non_numeric_value(env, field):
    """All six feed multiplications; a string among them fails much later.

    Each is checked because the list of fields is a literal in the validator and a
    field added to the UI without being added there is validated by nothing.
    """
    payload = base_input(documentConfigs=[{"type": "invoice", field: "many"}])
    assert f"documentConfigs[0].{field} must be a number" in message(payload)


@pytest.mark.unit
def test_an_empty_string_in_a_numeric_field_is_accepted_as_unset(env):
    """The UI sends `""` for a field the operator has not filled in.

    Treating it as a bad number would make an untouched form unsubmittable.
    """
    validate_capacity_input(
        base_input(
            documentConfigs=[{"type": "invoice", "avgPages": "", "ocrTokens": ""}]
        )
    )


@pytest.mark.unit
def test_a_numeric_document_field_given_as_a_numeric_string_is_accepted(env):
    """Form inputs arrive as strings, so `"3"` has to pass."""
    validate_capacity_input(
        base_input(documentConfigs=[{"type": "invoice", "avgPages": "3"}])
    )


@pytest.mark.unit
def test_a_time_slot_that_is_not_an_object_names_its_position(env):
    payload = base_input(timeSlots=[{"hour": 1, "docsPerHour": 2}, "nine"])
    assert "timeSlots[1] must be an object" in message(payload)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("hour", "hour must be an integer"),
        ("docsPerHour", "docsPerHour must be an integer"),
    ],
)
def test_a_non_numeric_slot_field_is_distinguished_from_an_out_of_range_one(
    env, field, expected
):
    """Two different problems with two different fixes, so two messages.

    "must be an integer" tells the operator the value is not a number at all;
    "must be between 0 and 23" tells them it is a number in the wrong place.
    """
    payload = base_input(timeSlots=[{field: "nine"}])
    assert expected in message(payload)


@pytest.mark.unit
def test_time_slots_supplied_as_a_json_string_are_validated_after_parsing(env):
    """The resolver forwards the schedule as a string, and it still gets checked.

    Parsing and then skipping validation would let an out-of-range hour through to
    the handler, where it would index the 24-entry breakdown and raise a `KeyError`.
    """
    assert "timeSlots[0].hour must be between 0 and 23" in message(
        base_input(timeSlots=json.dumps([{"hour": 99}]))
    )


@pytest.mark.unit
def test_a_valid_schedule_string_passes(env):
    validate_capacity_input(
        base_input(timeSlots=json.dumps([{"hour": 9, "docsPerHour": 60}]))
    )


@pytest.mark.unit
def test_an_empty_or_placeholder_user_config_is_not_treated_as_malformed(env):
    """The UI sends `"{}"`, or nothing, before any model has been chosen."""
    for value in ("", "   ", "{}", " {} "):
        validate_capacity_input(base_input(userConfig=value))


@pytest.mark.unit
def test_every_request_problem_is_reported_in_one_pass(env):
    """The same collect-don't-raise contract as the environment side.

    Three unrelated defects across three different fields; a validator that stopped
    at the first would send the operator round the loop three times.
    """
    payload = {
        "pattern": "pattern-7",
        "maxAllowedLatency": 99999,
        "documentConfigs": [{"avgPages": -1}],
        "timeSlots": [{"hour": 25}],
    }
    text = message(payload)
    assert "Only pattern-2 and unified patterns are supported, got: pattern-7" in text
    assert "cannot exceed 3600 seconds" in text
    assert "documentConfigs[0].type is required" in text
    assert "documentConfigs[0].avgPages cannot be negative" in text
    assert "timeSlots[0].hour must be between 0 and 23" in text
    assert text.count("\n  - ") == 5
