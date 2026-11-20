# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One decision-model failure, classified the same way by both of its consumers (#2112).

The decision-model client says whether a caller may repeat its whole step through
``DecisionModelError.retry_later``. Two steps consume that failure:

* section classification, where ``DecisionModelSectionLabeler`` raises
  ``TransientError`` for a ``retry_later`` failure and the section fails otherwise;
* the assessment Lambda (``patterns/unified/src/assessment_function``), which gets
  the client's exception unchanged from the confidence scorer and retries the step
  when ``is_transient_error`` says so, degrading the section otherwise.

A bare-status Lambda reply (``"500"``, ``"429"``: botocore's code when the reply
names no error type) is one the client does not retry, so it reports
``retry_later=False`` with the ``ClientError`` as the cause. ``is_transient_error``
used to follow that cause and call the bare 5xx transient, so section classification
failed the section while assessment retried the step for the same reply. Each half
was tested on its own; this drives one botocore-built reply through the real client
into both consumers and asserts they agree, and what they agree on.

The assessment handler also matches throttling keywords in an exception's text
before it asks ``is_transient_error``. A decider function error whose text holds one
(``ThrottlingException``, or botocore's ``reached max retries``) used to be re-raised
there under a name no Retry lists, failing the document while classification failed
only the section; the function-error cases pin that it now degrades.
"""

from __future__ import annotations

import io
import json

import pytest
from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError

from idp_common.config.schema_constants import UNCLASSIFIED_CLASS
from idp_common.decision_model import DecisionModelClient, DecisionModelError
from idp_common.utils.transient_errors import TransientError
from tests.unit._botocore_errors import (
    BARE_RETRIED_STATUSES,
    bare_replies,
    named_reply,
    service_error,
)
from tests.unit.classification.test_split_then_classify_transient import (
    _LambdaErrorDecider,
    _run_decision_model,
)
from tests.unit.decision_model.window_contract import (  # noqa: F401 - autouse
    no_oversize_requests,
    window_refusal,
)
from tests.unit.lambdas.test_assessment_function_degradation import (  # noqa: F401
    _invoke_with_failure,
    wired,
)

pytestmark = pytest.mark.unit

_ARN = "arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-decider"


def _lambda_reply(reply):
    """The error a real Lambda client raises for ``reply`` to ``Invoke``."""
    return lambda: service_error(reply, "Invoke", "lambda")


def _function_error(error_type, message):
    """The decider's handler raised: Lambda answers 200 with ``FunctionError``."""
    return lambda: {
        "StatusCode": 200,
        "FunctionError": "Unhandled",
        "Payload": io.BytesIO(
            json.dumps({"errorType": error_type, "errorMessage": message}).encode()
        ),
    }


#: Each case: how one Lambda invocation fails, and whether a step retry is the right
#: answer. The bare statuses are what #2112 is about, each with an empty body and an
#: HTML one; the client does not retry them. The named throttle and the connection
#: that never opened are the client's ``retry_later`` controls, and the read timeout
#: is the call that may still be running.
_CASES = {
    **{
        reply.label: (_lambda_reply(reply), False)
        for status in BARE_RETRIED_STATUSES
        for reply in bare_replies(status)
    },
    "TooManyRequestsException": (
        _lambda_reply(named_reply("TooManyRequestsException", service="lambda")),
        True,
    ),
    "connect timeout": (
        lambda: ConnectTimeoutError(endpoint_url="https://lambda"),
        True,
    ),
    "read timeout": (lambda: ReadTimeoutError(endpoint_url="https://lambda"), False),
    # The decider's own handler failed, with text the assessment handler's
    # throttle-keyword check would match: a refusal the client never retries.
    "function error ThrottlingException": (
        _function_error("ThrottlingException", "Rate exceeded"),
        False,
    ),
    "function error naming botocore's retries": (
        _function_error(
            "ClientError",
            "An error occurred (AccessDenied) when calling the GetObject operation "
            "(reached max retries: 4): Access Denied",
        ),
        False,
    ),
}


class _Decider(_LambdaErrorDecider):
    """``_LambdaErrorDecider``, also able to answer with a function error."""

    def invoke(self, FunctionName, InvocationType, Payload):
        self.requests.append(json.loads(Payload))
        refusal = window_refusal(Payload)
        if refusal:
            return refusal
        outcome = self.fail()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _section_classification_retries(fail) -> bool:
    """True when section classification retries the step, False when the section
    fails; anything else is a test failure."""
    try:
        result, _ = _run_decision_model(_Decider(fail=fail), page_count=1)
    except TransientError:
        return True
    assert result.sections[0].classification == UNCLASSIFIED_CLASS
    return False


class _FailingLambda:
    def __init__(self, fail):
        self.fail = fail

    def invoke(self, **_kwargs):
        outcome = self.fail()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _client_failure(fail, monkeypatch) -> DecisionModelError:
    """The exception the real client raises once ``fail`` exhausts its retries."""
    monkeypatch.setattr("idp_common.decision_model.invoker.time.sleep", lambda s: None)
    client = DecisionModelClient(
        _ARN, lambda_client=_FailingLambda(fail), metrics_enabled=False, max_retries=1
    )
    with pytest.raises(DecisionModelError) as caught:
        client.decide(_request(), "Ctx")
    return caught.value


def _request():
    from idp_common.decision_model import ChoiceQuestion, SystemOneRequest

    return SystemOneRequest(
        state="An invoice.",
        questions={
            "kind": ChoiceQuestion(
                instructions="Which type?", criteria={"invoice": None, "letter": None}
            )
        },
    )


def _assessment_retries(error, monkeypatch) -> bool:
    """True when the assessment Lambda retries the step, False when it degrades."""
    try:
        _invoke_with_failure(monkeypatch, error)
    except TransientError:
        return True
    return False


@pytest.mark.usefixtures("wired")
@pytest.mark.parametrize("case", sorted(_CASES))
def test_both_consumers_classify_one_failure_the_same_way(case, monkeypatch):
    fail, retried = _CASES[case]

    error = _client_failure(fail, monkeypatch)
    assert error.retry_later is retried, case  # the client's verdict

    by_classification = _section_classification_retries(fail)
    by_assessment = _assessment_retries(error, monkeypatch)

    assert by_classification == by_assessment, (
        f"{case}: section classification "
        f"{'retries' if by_classification else 'fails the section'}, assessment "
        f"{'retries' if by_assessment else 'degrades the section'}"
    )
    assert by_assessment is retried, case
