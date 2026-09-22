# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for BedrockErrorMessageHandler, which turns a Bedrock exception into
the message a user sees and the retry decision the caller acts on.

Two things make this worth covering carefully rather than smoke-testing. First,
`retry_recommended` is consumed as a control signal, not just displayed: an error
mapped as retryable when it is not produces a loop against a request that will
never succeed, and one mapped as non-retryable when it is turns a throttle into a
user-visible failure. So every mapping is checked for the pair
(`retry_recommended`, `is_transient`) rather than only for its message text.

Second, `extract_error_code` has four fallbacks of decreasing precision, ending in
a case-insensitive substring scan of the exception's string form. That last one can
match an error code inside unrelated text, so the order the fallbacks are tried in
is asserted directly: a more precise source must win over a less precise one.

Message wording is asserted only where it carries information the caller cannot get
elsewhere — the retry-attempt context — because pinning user-facing prose makes
every copy edit a test failure.
"""

import botocore.exceptions
import pytest

from idp_common.agents.common.bedrock_error_messages import (
    BedrockErrorInfo,
    BedrockErrorMessageHandler,
)

HANDLER = BedrockErrorMessageHandler


def _client_error(code: str, message: str = "boom") -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": message}}, "Converse"
    )


@pytest.mark.unit
class TestErrorMappingsTable:
    """The mapping table itself: shape, and the retry semantics of each entry."""

    def test_every_entry_is_a_bedrock_error_info(self):
        assert all(
            isinstance(v, BedrockErrorInfo) for v in HANDLER.ERROR_MAPPINGS.values()
        )

    def test_every_entry_has_a_user_message_and_an_error_type(self):
        for code, info in HANDLER.ERROR_MAPPINGS.items():
            assert info.user_message.strip(), code
            assert info.error_type.strip(), code

    def test_every_entry_offers_at_least_one_action(self):
        # The action list is what the UI shows under the message; an empty one
        # leaves a user told that something failed and nothing to do about it.
        for code, info in HANDLER.ERROR_MAPPINGS.items():
            assert info.action_recommendations, code

    @pytest.mark.parametrize(
        "code",
        [
            "serviceUnavailableException",
            "ServiceUnavailableException",
            "ThrottlingException",
            "ModelThrottledException",
            "ModelNotReadyException",
            "RequestTimeout",
            "RequestTimeoutException",
            "ServiceQuotaExceededException",
            "TooManyRequestsException",
        ],
    )
    def test_transient_conditions_are_marked_retryable_with_a_delay(self, code):
        # Retrying is only useful if the condition can clear on its own, and a
        # retry with no delay against a throttle makes the throttle worse.
        info = HANDLER.ERROR_MAPPINGS[code]
        assert info.retry_recommended is True
        assert info.is_transient is True
        assert info.retry_delay_seconds and info.retry_delay_seconds > 0

    @pytest.mark.parametrize("code", ["ValidationException", "AccessDeniedException"])
    def test_permanent_conditions_are_not_retryable_and_carry_no_delay(self, code):
        # A malformed request and a missing permission do not fix themselves, so
        # retrying is pure cost and a delay would only postpone the same failure.
        info = HANDLER.ERROR_MAPPINGS[code]
        assert info.retry_recommended is False
        assert info.is_transient is False
        assert info.retry_delay_seconds is None

    def test_the_two_spellings_of_service_unavailable_agree(self):
        # Bedrock returns both capitalisations depending on the API surface, so the
        # two entries must not drift into different advice.
        lower = HANDLER.ERROR_MAPPINGS["serviceUnavailableException"]
        upper = HANDLER.ERROR_MAPPINGS["ServiceUnavailableException"]
        assert lower == upper

    def test_the_two_spellings_of_request_timeout_agree(self):
        assert (
            HANDLER.ERROR_MAPPINGS["RequestTimeout"]
            == HANDLER.ERROR_MAPPINGS["RequestTimeoutException"]
        )

    def test_a_quota_delay_is_longer_than_a_throttle_delay(self):
        # A quota resets on a billing window and a throttle on a rate window, so
        # advising the same wait for both would send the user back too early.
        quota = HANDLER.ERROR_MAPPINGS[
            "ServiceQuotaExceededException"
        ].retry_delay_seconds
        throttle = HANDLER.ERROR_MAPPINGS["ThrottlingException"].retry_delay_seconds
        assert quota is not None and throttle is not None
        assert quota > throttle


@pytest.mark.unit
class TestExtractErrorCode:
    """extract_error_code: four sources, in order of precision."""

    def test_a_client_errors_response_code_is_used(self):
        assert (
            HANDLER.extract_error_code(_client_error("ThrottlingException"))
            == "ThrottlingException"
        )

    def test_an_unmapped_client_error_code_is_still_returned(self):
        # The method reports what the service said; deciding whether it is known is
        # get_error_info's job.
        assert HANDLER.extract_error_code(_client_error("SomeNewException")) == (
            "SomeNewException"
        )

    def test_a_client_error_with_no_code_falls_back_to_the_parenthesised_form(self):
        # EventStreamError is a ClientError subclass whose response carries no
        # Error.Code; the code is only in the message, as "(errorCode) when calling".
        error = botocore.exceptions.ClientError({"Error": {}}, "Converse")
        error.args = (
            "An error occurred (modelStreamErrorException) when calling Converse",
        )
        assert HANDLER.extract_error_code(error) == "modelStreamErrorException"

    def test_an_exception_whose_class_name_is_a_mapped_code_is_matched(self):
        # Boto3 synthesises exception classes named after the error code, so the
        # type name is a legitimate source when there is no response dict.
        exc = type("ThrottlingException", (Exception,), {})("slow down")
        assert HANDLER.extract_error_code(exc) == "ThrottlingException"

    def test_a_mapped_code_appearing_in_the_message_is_matched_case_insensitively(self):
        assert (
            HANDLER.extract_error_code(RuntimeError("caused by a throttlingexception"))
            == "ThrottlingException"
        )

    def test_an_exception_with_no_recognisable_code_returns_none(self):
        assert HANDLER.extract_error_code(RuntimeError("something went wrong")) is None

    def test_the_response_code_wins_over_the_message_text(self):
        # Precision order matters: the service's own code is authoritative, and a
        # message that happens to mention another condition must not override it.
        error = _client_error(
            "AccessDeniedException", message="not a ThrottlingException"
        )
        assert HANDLER.extract_error_code(error) == "AccessDeniedException"

    def test_the_class_name_wins_over_the_message_text(self):
        exc = type("ValidationException", (Exception,), {})(
            "looks like a ThrottlingException"
        )
        assert HANDLER.extract_error_code(exc) == "ValidationException"


@pytest.mark.unit
class TestGetErrorInfo:
    """get_error_info: mapping lookup plus retry-attempt context."""

    def test_a_mapped_error_keeps_its_type_delay_and_actions(self):
        info = HANDLER.get_error_info(_client_error("ThrottlingException"))
        mapped = HANDLER.ERROR_MAPPINGS["ThrottlingException"]
        assert info.error_type == mapped.error_type
        assert info.retry_delay_seconds == mapped.retry_delay_seconds
        assert info.action_recommendations == mapped.action_recommendations
        assert info.is_transient is mapped.is_transient

    def test_an_unmapped_error_gets_a_conservative_default(self):
        # Unknown does not mean unrecoverable, so the default is retryable with a
        # short delay rather than a hard failure.
        info = HANDLER.get_error_info(RuntimeError("novel failure"))
        assert info.error_type == "unknown_error"
        assert info.retry_recommended is True
        assert info.is_transient is True
        assert info.retry_delay_seconds == 30

    def test_the_unmapped_default_carries_the_original_text_for_debugging(self):
        info = HANDLER.get_error_info(RuntimeError("novel failure"))
        assert "novel failure" in info.technical_details

    def test_the_table_entry_is_not_mutated_by_a_call(self):
        # ERROR_MAPPINGS is class-level and shared, so get_error_info must build a
        # new object rather than annotate the table entry. Otherwise the first
        # caller's retry count would leak into every later caller's message.
        before = HANDLER.ERROR_MAPPINGS["ThrottlingException"]
        HANDLER.get_error_info(_client_error("ThrottlingException"), retry_attempts=3)
        assert HANDLER.ERROR_MAPPINGS["ThrottlingException"] == before
        assert "retries" not in before.technical_details

    def test_no_retries_leaves_the_message_and_details_unchanged(self):
        info = HANDLER.get_error_info(_client_error("ThrottlingException"))
        mapped = HANDLER.ERROR_MAPPINGS["ThrottlingException"]
        assert info.user_message == mapped.user_message
        assert info.technical_details == mapped.technical_details

    @pytest.mark.parametrize("attempts", [1, 2, 3])
    def test_the_retry_count_is_recorded_in_the_technical_details(self, attempts):
        info = HANDLER.get_error_info(
            _client_error("ThrottlingException"), retry_attempts=attempts
        )
        assert f"after {attempts} retries" in info.technical_details

    def test_retry_stops_being_recommended_after_three_attempts(self):
        # The ceiling is what stops a caller looping on a condition that is not
        # clearing. Three is the last attempt that still advises another.
        error = _client_error("ThrottlingException")
        assert HANDLER.get_error_info(error, retry_attempts=2).retry_recommended is True
        assert (
            HANDLER.get_error_info(error, retry_attempts=3).retry_recommended is False
        )
        assert (
            HANDLER.get_error_info(error, retry_attempts=9).retry_recommended is False
        )

    def test_a_non_retryable_error_stays_non_retryable_at_zero_attempts(self):
        assert (
            HANDLER.get_error_info(
                _client_error("ValidationException")
            ).retry_recommended
            is False
        )

    def test_is_transient_is_not_affected_by_the_retry_count(self):
        # Whether a condition can clear is a property of the condition, not of how
        # many times we have asked. Only the advice changes.
        info = HANDLER.get_error_info(
            _client_error("ThrottlingException"), retry_attempts=9
        )
        assert info.is_transient is True
        assert info.retry_recommended is False


@pytest.mark.unit
class TestRetryContextMessages:
    """_enhance_message_with_retry_context: the four wordings."""

    def test_zero_attempts_returns_the_base_message_unchanged(self):
        assert HANDLER._enhance_message_with_retry_context("Base.", 0) == "Base."

    def test_one_attempt_is_phrased_in_the_singular(self):
        # "We tried 1 times" is the failure this branch exists to avoid.
        message = HANDLER._enhance_message_with_retry_context("Base.", 1)
        assert "once more" in message
        assert "1 times" not in message

    @pytest.mark.parametrize("attempts", [2, 3])
    def test_two_or_three_attempts_state_the_count(self, attempts):
        assert f"tried {attempts} times" in HANDLER._enhance_message_with_retry_context(
            "Base.", attempts
        )

    def test_more_than_three_attempts_stops_counting_and_changes_the_claim(self):
        # Past the retry ceiling the honest message is that the service has an
        # ongoing problem, not that we will keep trying.
        message = HANDLER._enhance_message_with_retry_context("Base.", 7)
        assert "ongoing issues" in message
        assert "7" not in message

    def test_the_base_message_is_always_preserved(self):
        for attempts in (0, 1, 2, 3, 10):
            assert HANDLER._enhance_message_with_retry_context(
                "Base.", attempts
            ).startswith("Base.")


@pytest.mark.unit
class TestFormatErrorForFrontend:
    """format_error_for_frontend: the camelCase contract the UI reads."""

    def test_every_expected_key_is_present(self):
        payload = HANDLER.format_error_for_frontend(
            _client_error("ThrottlingException")
        )
        assert set(payload) == {
            "errorType",
            "message",
            "technicalDetails",
            "retryRecommended",
            "retryDelaySeconds",
            "actionRecommendations",
            "isTransient",
            "retryAttempts",
        }

    def test_the_values_agree_with_get_error_info(self):
        error = _client_error("ThrottlingException")
        info = HANDLER.get_error_info(error, retry_attempts=2)
        payload = HANDLER.format_error_for_frontend(error, retry_attempts=2)
        assert payload["errorType"] == info.error_type
        assert payload["message"] == info.user_message
        assert payload["technicalDetails"] == info.technical_details
        assert payload["retryRecommended"] is info.retry_recommended
        assert payload["retryDelaySeconds"] == info.retry_delay_seconds
        assert payload["isTransient"] is info.is_transient

    def test_the_retry_attempt_count_is_echoed_back(self):
        payload = HANDLER.format_error_for_frontend(
            _client_error("ThrottlingException"), retry_attempts=2
        )
        assert payload["retryAttempts"] == 2

    def test_actions_are_an_empty_list_rather_than_null_when_absent(self):
        # The UI iterates this field, so None would be a render error rather than
        # an empty section.
        payload = HANDLER.format_error_for_frontend(RuntimeError("novel"))
        assert isinstance(payload["actionRecommendations"], list)

    def test_a_non_retryable_error_reports_a_null_delay(self):
        payload = HANDLER.format_error_for_frontend(
            _client_error("ValidationException")
        )
        assert payload["retryRecommended"] is False
        assert payload["retryDelaySeconds"] is None


@pytest.mark.unit
class TestIsRetryableError:
    """is_retryable_error: the single boolean a caller branches on."""

    @pytest.mark.parametrize(
        "code", ["ThrottlingException", "ModelThrottledException", "RequestTimeout"]
    )
    def test_transient_conditions_are_retryable(self, code):
        assert HANDLER.is_retryable_error(_client_error(code)) is True

    @pytest.mark.parametrize("code", ["ValidationException", "AccessDeniedException"])
    def test_permanent_conditions_are_not_retryable(self, code):
        assert HANDLER.is_retryable_error(_client_error(code)) is False

    def test_an_unknown_error_is_treated_as_retryable(self):
        assert HANDLER.is_retryable_error(RuntimeError("novel")) is True

    def test_it_requires_both_the_recommendation_and_transience(self):
        # The conjunction is what keeps a mapping that sets one flag and not the
        # other from being read as retryable. Asserted over the whole table rather
        # than on one entry, so a future mapping with an inconsistent pair fails.
        for code, info in HANDLER.ERROR_MAPPINGS.items():
            expected = info.retry_recommended and info.is_transient
            assert HANDLER.is_retryable_error(_client_error(code)) is expected, code
