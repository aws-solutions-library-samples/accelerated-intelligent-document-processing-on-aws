# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1101: rule validation must not answer a transient fault with a result.

Rule validation had two ways of turning a recoverable Bedrock or S3 failure into a
final outcome, and the second is the one that made the first invisible:

1. **A fabricated verdict.** Several ``except`` blocks return a dict carrying
   ``recommendation: "Information Not Found"``, which is one of the configured
   ``recommendation_options`` — a real answer, counted in the document's summary and
   read by downstream features. A throttle was answered with "this document does not
   evidence the rule".
2. **A terminal failure with no exception.** ``validate_document_async`` recorded the
   reason in ``document.errors``, set ``Status.FAILED`` and RETURNED. The handler then
   raised a bare ``Exception`` synthesised from that status, so by the time Step
   Functions saw the failure the original exception — the only thing carrying the
   transient classification — no longer existed. ``Exception`` is in no
   ``Retry.ErrorEquals`` list, so the eight-attempt ladder never ran.

The tests below pin the split at each site: a transient cause is re-raised under
``TransientError`` (the one name the three rule-validation task states list), and a
deterministic cause keeps every bit of today's behaviour, because eight more
attempts cannot validate a document that fails the same way every time.

``test_a_transient_from_one_rule_is_not_swallowed_by_the_document_level_handler`` is
the one to keep. These ``except`` blocks NEST — a per-rule failure travels up through
``asyncio.gather`` into the document-level block — and ``raise_if_transient`` returns
silently for an exception already surfaced under the name, because it is written for
an ``except`` that ends in a bare ``raise``. Neither of these does. Using it at both
sites therefore left each site individually correct and the composition broken: the
inner classification was undone by the outer block, which swallowed the
``TransientError`` and returned a FAILED document exactly as before.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import botocore.exceptions
import pytest

from idp_common.config.models import IDPConfig
from idp_common.models import Document, Page, Section, Status
from idp_common.rule_validation.policy_classification import (
    PolicyClassificationService,
)
from idp_common.rule_validation.service import RuleValidationService
from idp_common.utils.transient_errors import TransientError, is_transient_error

# Transient by error code or by exception type, but under class names no Retry list
# carries. Provenance is per member rather than for the list, because it differs:
# attaching one claim to the set would be wrong for whichever member it does not fit.
TRANSIENT = [
    pytest.param(
        # CONSTRUCTED. Botocore raises a bare `ClientError` when the wire code is not
        # in the service's error map; `ThrottlingException` *is* in bedrock-runtime's,
        # so this pairing cannot be induced there and is built to stand for the
        # services where it can be.
        botocore.exceptions.ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "Converse",
        ),
        id="bare-ClientError-throttle",
    ),
    pytest.param(
        # OBSERVED from a live bedrock-runtime Converse with a 1 ms read timeout.
        botocore.exceptions.ReadTimeoutError(endpoint_url="https://bedrock"),
        id="ReadTimeoutError",
    ),
    pytest.param(
        # OBSERVED from a live bedrock-runtime Converse with a 1 ms connect timeout.
        botocore.exceptions.ConnectTimeoutError(endpoint_url="https://bedrock"),
        id="ConnectTimeoutError",
    ),
]

DETERMINISTIC = [
    pytest.param(ValueError("rule schema is malformed"), id="ValueError"),
    pytest.param(
        botocore.exceptions.ClientError(
            {"Error": {"Code": "ValidationException", "Message": "too long"}},
            "Converse",
        ),
        id="ValidationException",
    ),
]


# The minimum `RuleValidationService.__init__` accepts: it requires a
# `fact_extraction` subsection carrying a model id.
_CONFIG = {
    "rule_validation": {
        "fact_extraction": {
            "model": "us.amazon.nova-lite-v1:0",
            "system_prompt": "s",
            "task_prompt": "{DOCUMENT_TEXT} {rule}",
            "temperature": 0.0,
            "top_k": 1,
            "top_p": 0.1,
            "max_tokens": 100,
        }
    }
}


def _document() -> Document:
    """One section over one page. The page must be present and carry a
    ``parsed_text_uri``, or section processing returns before reaching the model and
    the failure under test is never provoked."""
    return Document(
        id="internal-doc-id",
        input_bucket="in",
        input_key="doc.pdf",
        output_bucket="out",
        status=Status.RULE_VALIDATION,
        num_pages=1,
        pages={"1": Page(page_id="1", parsed_text_uri="s3://out/doc.pdf/1/parsed.txt")},
        sections=[Section(section_id="1", classification="w2", page_ids=["1"])],
    )


@pytest.fixture
def service() -> RuleValidationService:
    return RuleValidationService(region="us-west-2", config=_CONFIG)


# ---------------------------------------------------------------------------
# The document-level block: it RETURNS, so it is the one that used to make a
# transient failure permanent.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("error", TRANSIENT)
def test_a_transient_document_failure_is_raised_under_the_retried_name(service, error):
    # The page-text read is deliberately unwrapped in `process_one_section`, so it
    # is the shortest real path into the document-level `except` under test.
    with patch(
        "idp_common.rule_validation.service.s3.get_text_content", side_effect=error
    ):
        document = _document()
        with pytest.raises(TransientError) as surfaced:
            asyncio.run(service.validate_document_async(document, _CONFIG))

    assert surfaced.value.__cause__ is error, "the cause must stay readable"
    # The document must NOT have been marked failed on the way out: the step is about
    # to be retried, and a FAILED status persisted here would be a false alarm.
    assert document.status != Status.FAILED


@pytest.mark.unit
@pytest.mark.parametrize("error", DETERMINISTIC)
def test_a_deterministic_document_failure_is_still_recorded_and_returned(
    service, error
):
    """Unchanged behaviour, asserted so the split cannot quietly become "raise
    everything" — which would retry an unparseable document eight times."""
    with patch(
        "idp_common.rule_validation.service.s3.get_text_content", side_effect=error
    ):
        document = _document()
        returned = asyncio.run(service.validate_document_async(document, _CONFIG))

    assert returned.status == Status.FAILED
    assert any("Error validating document" in e for e in returned.errors)


# ---------------------------------------------------------------------------
# The composition. This is the defect a per-site review passes.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_transient_from_one_rule_is_not_swallowed_by_the_document_level_handler():
    """A ``TransientError`` raised for ONE rule must reach the caller.

    Both blocks are correct in isolation and wrong together if the outer one uses
    ``raise_if_transient``: that helper returns silently when the exception already
    is a ``TransientError``, so the outer block would fall through to
    ``document.errors.append`` and return a FAILED document — the exact behaviour
    this issue is about, restored by the fix for it.
    """
    inner = TransientError(
        botocore.exceptions.ReadTimeoutError(endpoint_url="https://bedrock"),
        "rule validation rule 'must be employed'",
    )
    service = RuleValidationService(region="us-west-2", config=_CONFIG)
    # An exception that a site further in has ALREADY surfaced under the name.
    with patch(
        "idp_common.rule_validation.service.s3.get_text_content", side_effect=inner
    ):
        document = _document()
        with pytest.raises(TransientError) as surfaced:
            asyncio.run(service.validate_document_async(document, _CONFIG))

    # Not re-wrapped: one name, one cause, however many handlers it passed through.
    assert surfaced.value is inner
    assert not isinstance(surfaced.value.__cause__, TransientError)
    assert document.status != Status.FAILED


@pytest.mark.unit
def test_reraise_if_transient_is_what_makes_that_composition_work():
    """The helper's contract, stated directly rather than only through the service.

    ``raise_if_transient`` returning silently here is deliberate — it expects a bare
    ``raise`` to follow — and is the reason a swallowing block needs the other one.
    """
    from idp_common.utils.transient_errors import (
        raise_if_transient,
        reraise_if_transient,
    )

    already = TransientError(TimeoutError("x"), "rule 1")
    raise_if_transient(already)  # must NOT raise; the bare `raise` would
    with pytest.raises(TransientError) as surfaced:
        reraise_if_transient(already)
    assert surfaced.value is already, "must not double-wrap"

    # And for something not yet surfaced, the two agree.
    fresh = botocore.exceptions.ReadTimeoutError(endpoint_url="https://bedrock")
    assert is_transient_error(fresh)
    with pytest.raises(TransientError):
        reraise_if_transient(fresh, where="rule 1")

    # A deterministic failure passes through both untouched.
    reraise_if_transient(ValueError("bad schema"))


# ---------------------------------------------------------------------------
# The per-rule block: it returns a real VERDICT, so a transient fault here was a
# wrong compliance answer rather than a failure.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("error", TRANSIENT)
def test_a_transient_rule_failure_is_not_answered_with_a_verdict(service, error):
    with patch.object(RuleValidationService, "_invoke_model_async", side_effect=error):
        with pytest.raises(TransientError):
            asyncio.run(
                service._process_rule_question(
                    rule="must be employed",
                    user_history="text",
                    policy_type="eligibility",
                    config=_CONFIG,
                )
            )


@pytest.mark.unit
@pytest.mark.parametrize("error", DETERMINISTIC)
def test_a_deterministic_rule_failure_still_yields_the_per_rule_fallback(
    service, error
):
    """One bad rule must not discard the other rules' answers — the partial-failure
    tolerance the surrounding ``gather`` exists for."""
    with patch.object(RuleValidationService, "_invoke_model_async", side_effect=error):
        result = asyncio.run(
            service._process_rule_question(
                rule="must be employed",
                user_history="text",
                policy_type="eligibility",
                config=_CONFIG,
            )
        )

    assert result["recommendation"] == "Information Not Found"
    assert result["policy_type"] == "eligibility"


# ---------------------------------------------------------------------------
# The orchestrator's outermost block, which returned an empty result NORMALLY and
# so made the handler's whole failure path unreachable.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("error", TRANSIENT)
def test_a_transient_consolidation_failure_does_not_complete_the_document(error):
    """Before the fix this returned a document carrying an empty
    ``RuleValidationResult``, and the handler then wrote it and reported success — so
    the document COMPLETED with no verdicts and no diagnosis anywhere."""
    from idp_common.rule_validation.orchestrator import (
        RuleValidationOrchestratorService,
    )

    orchestrator = RuleValidationOrchestratorService(config={})
    document = _document()
    with patch.object(
        RuleValidationOrchestratorService, "load_section_results", side_effect=error
    ):
        with pytest.raises(TransientError):
            asyncio.run(orchestrator.consolidate_and_save_all(document, {}))


@pytest.mark.unit
def test_a_deterministic_consolidation_failure_still_returns_an_empty_result():
    from idp_common.rule_validation.orchestrator import (
        RuleValidationOrchestratorService,
    )

    orchestrator = RuleValidationOrchestratorService(config={})
    document = _document()
    with patch.object(
        RuleValidationOrchestratorService,
        "load_section_results",
        side_effect=ValueError("malformed section result"),
    ):
        returned = asyncio.run(orchestrator.consolidate_and_save_all(document, {}))

    assert returned.rule_validation_result is not None


# ---------------------------------------------------------------------------
# Policy classification: no Bedrock, but a page it could not read is a page whose
# regexes never ran, which silently changes which policies match.
# ---------------------------------------------------------------------------


def _page_content_classifier() -> PolicyClassificationService:
    """A classifier that can only decide by reading page text.

    Two policy classes, because one short-circuits without any regex check, and
    neither carries a document-name regex, so the page read is the only evidence
    available and skipping it changes the answer.
    """
    cfg = IDPConfig()
    cfg.policy_classes = [
        {
            "x-aws-idp-policy-type": "medicare",
            "x-aws-idp-document-page-content-regex": r"(?i)medicare number",
        },
        {
            "x-aws-idp-policy-type": "invoice",
            "x-aws-idp-document-page-content-regex": r"(?i)invoice number",
        },
    ]
    return PolicyClassificationService(config=cfg)


@pytest.mark.unit
@pytest.mark.parametrize("error", TRANSIENT)
def test_a_transient_page_read_does_not_silently_change_the_classification(error):
    """Drive the real classifier, failing only the page read.

    A page whose text could not be read is a page whose regexes never ran, so
    `medicare` — evidenced only by that page's content — goes unmatched and none of
    its rules are ever validated. Skipping the page is the right answer for a missing
    or unparseable object, and
    `test_policy_classification.py::test_page_content_regex_read_failure_swallowed`
    holds that half in place. For a transient fault it silently changes the answer.
    """
    document = Document(id="unknown.pdf")
    document.pages["1"] = Page(page_id="1", parsed_text_uri="s3://bucket/1.txt")

    with patch(
        "idp_common.rule_validation.policy_classification.s3.get_text_content",
        side_effect=error,
    ):
        with pytest.raises(TransientError) as surfaced:
            _page_content_classifier().classify_document(document)

    assert surfaced.value.__cause__ is error


@pytest.mark.unit
def test_the_page_read_is_reached_at_all():
    """Guard for the test above: if the fixture stopped exercising the page read, that
    test would pass for the wrong reason on a `TransientError` raised elsewhere."""
    document = Document(id="unknown.pdf")
    document.pages["1"] = Page(page_id="1", parsed_text_uri="s3://bucket/1.txt")

    with patch(
        "idp_common.rule_validation.policy_classification.s3.get_text_content",
        return_value="Claim submitted with Medicare Number 12345",
    ) as read:
        result = _page_content_classifier().classify_document(document)

    read.assert_called_once()
    assert result.matched_policy_types == ["medicare"]
