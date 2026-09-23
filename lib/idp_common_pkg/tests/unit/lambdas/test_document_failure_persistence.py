# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Three document-level handlers must record their diagnosis before raising (#1064).

The section-level form was #1049 / PR #1059. This is the document-level analogue:
``rule-validation-function``, ``processresults_function`` and
``rule-validation-orchestration-function`` each held an explanation of why the
document failed and raised without writing it, in a state machine where none of
the three task states has a ``Catch`` and where ``workflow_tracker`` writes only a
bare ``Document`` of status plus completion time for a FAILED execution.

The orchestration handler is the one that most needs pinning, because its failure
recorder **had never executed**. It referenced ``Status.ERROR`` (the member is
``FAILED``) and passed an ``error_message=`` keyword ``update_document_status``
does not accept, and both faults landed in a surrounding ``except`` that logged
and swallowed them. Nothing in the tree ran that block, which is the only reason
two fatal faults in four lines survived.

So two of the tests here deliberately refuse a ``MagicMock`` document service and
use a **real** ``DocumentDynamoDBService`` with only its DynamoDB client mocked: a
mock accepts ``Status.ERROR`` and any keyword you like and reports success, which
is precisely how the broken recorder would have passed a test written the easy
way. Against the real service a non-existent enum member is an ``AttributeError``
and a bad keyword is a ``TypeError``.
"""

from __future__ import annotations

import importlib.util
import json
import os
from typing import Any, List
from unittest.mock import MagicMock, patch

import botocore.exceptions
import pytest

from idp_common.config.models import IDPConfig
from idp_common.document_failure import (
    RULE_VALIDATION_FAILED_CODE,
    RULE_VALIDATION_NOT_CONSOLIDATED_CODE,
    SECTION_PROCESSING_FAILED_CODE,
)
from idp_common.dynamodb.service import DocumentDynamoDBService
from idp_common.models import Document, ProcessingIssue, Section, Status
from idp_common.utils.transient_errors import TransientError

_SRC = os.path.join(os.path.dirname(__file__), "../../../../../patterns/unified/src")


def _load(module_name: str, relative_path: str):
    """Load a deployed handler by path, with X-Ray's import-time work stubbed.

    By path because ``patterns/unified/src`` is not a package on the test path,
    and two of these three directories are not importable names anyway
    (``rule-validation-function`` contains hyphens). Same mechanism as the sibling
    extraction and assessment suites.
    """
    recorder = MagicMock()
    recorder.capture.return_value = lambda fn: fn
    xray_core = MagicMock()
    xray_core.patch_all = lambda: None
    xray_core.xray_recorder = recorder
    os.environ.setdefault("AWS_REGION", "us-east-1")
    with patch.dict(
        "sys.modules",
        {"aws_xray_sdk": MagicMock(), "aws_xray_sdk.core": xray_core},
    ):
        spec = importlib.util.spec_from_file_location(
            module_name, os.path.join(_SRC, relative_path)
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


rule_validation_index = _load(
    "rule_validation_index", "rule-validation-function/index.py"
)
processresults_index = _load("processresults_index", "processresults_function/index.py")
orchestration_index = _load(
    "orchestration_index", "rule-validation-orchestration-function/index.py"
)


class _Context:
    function_name = "fn"
    memory_limit_in_mb = 2048
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:fn"
    aws_request_id = "req-1"

    def get_remaining_time_in_millis(self):
        return 300_000


def _document(status: Status = Status.POSTPROCESSING) -> Document:
    """Two sections, so a test about WHICH section was marked can fail.

    ``id`` is deliberately different from ``input_key``. The tracking table is
    keyed on ``input_key`` (``PK = doc#{input_key}``), and the orchestration
    handler's broken recorder passed ``document.id`` where every other call site
    in this pattern passes ``input_key``. With the two equal — which they are for
    a document built by the pipeline — that fault is invisible.
    """
    return Document(
        id="internal-doc-id",
        input_bucket="in",
        input_key="doc.pdf",
        output_bucket="out",
        status=status,
        num_pages=2,
        pages={},
        sections=[
            Section(section_id="1", classification="w2", page_ids=["1"]),
            Section(section_id="2", classification="bank-statement", page_ids=["2"]),
        ],
    )


def _real_service() -> tuple[DocumentDynamoDBService, MagicMock]:
    """A real document service whose only mock is the DynamoDB client.

    Every method signature, expression build and enum access is the real one, so a
    call the broken recorder would have made fails here instead of being accepted.
    """
    client = MagicMock()
    client.update_item.return_value = {"Attributes": {}}
    return DocumentDynamoDBService(dynamodb_client=client), client


def _issues(section: Section) -> List[ProcessingIssue]:
    return list(section.processing_issues or [])


def _codes(section: Section) -> List[str]:
    return [i.code for i in _issues(section)]


# ---------------------------------------------------------------------------
# 1. rule-validation-function: the per-section rule validation Lambda
# ---------------------------------------------------------------------------

_RV_CAUSE = "Rule validation failed for document internal-doc-id, section 2"


@pytest.fixture
def rv_service(monkeypatch):
    monkeypatch.setenv("WORKING_BUCKET", "")
    monkeypatch.setattr(rule_validation_index, "get_config", lambda **kw: IDPConfig())
    monkeypatch.setattr(rule_validation_index, "metrics", MagicMock())
    doc_service = MagicMock()
    monkeypatch.setattr(
        rule_validation_index, "create_document_service", lambda *a, **kw: doc_service
    )
    return doc_service


def _rv_invoke(monkeypatch, *, fails_with=None, errors=("Z3 solver timed out",)):
    """Drive the handler with a rule-validation service that fails.

    ``validate_document`` mutates the document it is given and returns the SAME
    object; the service records its reason in ``document.errors`` and sets
    ``Status.FAILED`` rather than raising. ``fails_with`` switches to the other
    shape — a service that raises outright.
    """

    def _validate(document: Document, *a: Any, **kw: Any):
        for message in errors:
            document.errors.append(message)
        if fails_with is not None:
            raise fails_with
        document.status = Status.FAILED
        return document

    service = MagicMock()
    service.validate_document.side_effect = _validate
    monkeypatch.setattr(
        rule_validation_index,
        "rule_validation",
        MagicMock(RuleValidationService=lambda **kw: service),
    )
    event = {
        "document": _document(Status.RULE_VALIDATION).to_dict(),
        "section_id": "2",
    }
    return rule_validation_index.handler(event, _Context())


@pytest.mark.unit
def test_rule_validation_failure_persists_the_section(monkeypatch, rv_service):
    """The section is written, at its own index, carrying the service's reason."""
    with pytest.raises(Exception, match="Rule validation failed"):
        _rv_invoke(monkeypatch)

    rv_service.update_document_section.assert_called_once()
    kwargs = rv_service.update_document_section.call_args.kwargs
    # Keyed on input_key, which is the tracking table's partition key, not on
    # the document's `id` attribute.
    assert kwargs["document_id"] == "doc.pdf"
    # Section "2" sits at index 1 in the FULL document. The handler narrows
    # `sections` to one element before calling the service, so an index taken
    # after that narrowing would be 0 and would overwrite section "1".
    assert kwargs["section_index"] == 1
    section = kwargs["section"]
    assert section.section_id == "2"
    assert _codes(section) == [RULE_VALIDATION_FAILED_CODE]
    issue = _issues(section)[0]
    assert issue.severity == "error"
    assert issue.stage == "rule_validation"
    # The service's own explanation, not the handler's synthesised sentence.
    assert "Z3 solver timed out" in issue.root_cause
    assert "1 error" in issue.root_cause


@pytest.mark.unit
def test_rule_validation_exception_propagates_unchanged(monkeypatch, rv_service):
    """The Step Functions cause is not made quieter by the new write."""
    with pytest.raises(Exception) as caught:
        _rv_invoke(monkeypatch)
    assert str(caught.value) == _RV_CAUSE
    assert type(caught.value) is Exception


@pytest.mark.unit
def test_rule_validation_persist_failure_does_not_mask_the_error(
    monkeypatch, rv_service
):
    """A DynamoDB failure must not replace the failure it was trying to reveal."""
    rv_service.update_document_section.side_effect = RuntimeError("table unavailable")
    with pytest.raises(Exception) as caught:
        _rv_invoke(monkeypatch)
    assert str(caught.value) == _RV_CAUSE
    assert "table unavailable" not in str(caught.value)


@pytest.mark.unit
def test_rule_validation_preserves_issues_from_other_stages(monkeypatch, rv_service):
    """An earlier stage's diagnosis is the thing a reader most needs alongside."""
    earlier = ProcessingIssue(
        stage="extraction",
        severity="warning",
        code="extraction_sparse",
        message="Few fields extracted.",
        section_id="2",
    )

    document = _document(Status.RULE_VALIDATION)
    next(s for s in document.sections if s.section_id == "2").processing_issues = [
        earlier
    ]

    def _validate(doc: Document, *a: Any, **kw: Any):
        doc.errors.append("boom")
        doc.status = Status.FAILED
        return doc

    service = MagicMock()
    service.validate_document.side_effect = _validate
    monkeypatch.setattr(
        rule_validation_index,
        "rule_validation",
        MagicMock(RuleValidationService=lambda **kw: service),
    )
    with pytest.raises(Exception):
        rule_validation_index.handler(
            {"document": document.to_dict(), "section_id": "2"}, _Context()
        )

    section = rv_service.update_document_section.call_args.kwargs["section"]
    assert _codes(section) == ["extraction_sparse", RULE_VALIDATION_FAILED_CODE]


@pytest.mark.unit
def test_rule_validation_service_raise_is_also_persisted(monkeypatch, rv_service):
    """A failure that bypasses the FAILED-status path still reaches the record."""
    with pytest.raises(ValueError, match="no policy classes"):
        _rv_invoke(monkeypatch, fails_with=ValueError("no policy classes"), errors=())

    section = rv_service.update_document_section.call_args.kwargs["section"]
    assert _codes(section) == [RULE_VALIDATION_FAILED_CODE]
    # With nothing in document.errors the exception itself is the diagnosis.
    assert _issues(section)[0].root_cause == "ValueError: no policy classes"


@pytest.mark.unit
def test_rule_validation_transient_failure_records_nothing(monkeypatch, rv_service):
    """A retry is coming; a marked section would show red and then clear."""
    transient = TransientError(botocore.exceptions.ReadTimeoutError(endpoint_url="x"))
    with pytest.raises(TransientError):
        _rv_invoke(monkeypatch, fails_with=transient, errors=())
    rv_service.update_document_section.assert_not_called()


@pytest.mark.unit
def test_rule_validation_root_cause_is_bounded(monkeypatch, rv_service):
    """`document.errors` is unbounded in count; `root_cause` must not be.

    The bound lives on ``ProcessingIssue.__post_init__``, so this asserts the
    class is doing its job for a new caller rather than re-implementing a cap.
    """
    with pytest.raises(Exception):
        _rv_invoke(
            monkeypatch, errors=tuple(f"failure {i} " + "x" * 400 for i in range(80))
        )

    issue = _issues(rv_service.update_document_section.call_args.kwargs["section"])[0]
    assert len(issue.root_cause.encode("utf-8")) <= ProcessingIssue.MAX_ROOT_CAUSE_BYTES
    # The count survives the elision because it is written at the front.
    assert issue.root_cause.startswith("80 errors:")


# ---------------------------------------------------------------------------
# 2. processresults_function: the collate step
# ---------------------------------------------------------------------------


@pytest.fixture
def pr_service(monkeypatch):
    monkeypatch.setenv("WORKING_BUCKET", "")
    monkeypatch.setattr(processresults_index, "get_config", lambda **kw: IDPConfig())
    monkeypatch.setattr(processresults_index, "is_hitl_enabled", lambda *a, **kw: False)
    monkeypatch.setattr(
        processresults_index, "create_metadata_file", lambda *a, **kw: None
    )
    doc_service = MagicMock()
    doc_service.get_document.return_value = None
    monkeypatch.setattr(
        processresults_index, "create_document_service", lambda *a, **kw: doc_service
    )
    return doc_service


def _section_result(section_id: str, *, failed: bool, errors=()) -> dict:
    """One entry of the ``ExtractionResults`` array the Map state produces."""
    document = Document(
        id="doc.pdf",
        input_bucket="in",
        input_key="doc.pdf",
        output_bucket="out",
        status=Status.FAILED if failed else Status.EXTRACTING,
        sections=[Section(section_id=section_id, classification="w2", page_ids=["1"])],
    )
    document.errors = list(errors)
    return {"document": document.to_dict()}


def _pr_invoke(document: Document, results: list[dict]):
    return processresults_index.handler(
        {
            "ClassificationResult": {"document": document.to_dict()},
            "ExtractionResults": results,
            "execution_arn": "arn:aws:states:us-east-1:1:execution:sm:exec",
        },
        _Context(),
    )


@pytest.mark.unit
def test_processresults_attributes_the_verdict_to_the_failed_section(pr_service):
    """Which section failed, and why, on the section rather than only in the cause.

    Snapshotted **at call time**, for the reason spelled out in
    ``test_processresults_persists_the_failed_status_it_computes``: this handler is
    the only one of the three that writes on its success path *before* the failure
    write, so it is the only one where reading ``call_args`` afterwards can report
    a state the write did not carry. The other two raise before any other write, so
    there the existence of the call is itself the proof.
    """
    writes = []
    pr_service.update_document.side_effect = lambda document: (
        writes.append(
            {
                s.section_id: [
                    (i.code, i.root_cause) for i in s.processing_issues or []
                ]
                for s in document.sections
            }
        ),
        document,
    )[1]

    with pytest.raises(Exception, match="Processing failed for 1 out of 2 sections"):
        _pr_invoke(
            _document(),
            [
                _section_result("1", failed=False),
                _section_result("2", failed=True, errors=["Schema mismatch on row 4"]),
            ],
        )

    # Three writes: POSTPROCESSING, final status, then the failure write.
    assert len(writes) == 3, "the failure write did not happen"
    final = writes[-1]
    assert [code for code, _ in final["2"]] == [SECTION_PROCESSING_FAILED_CODE]
    assert "Schema mismatch on row 4" in final["2"][0][1]
    # The section that did not fail is not marked.
    assert final["1"] == []
    # And the issue was not already on the section at the success-path writes, so
    # this really is the failure write carrying it.
    assert writes[1]["2"] == []


@pytest.mark.unit
def test_processresults_persists_the_failed_status_it_computes(pr_service):
    """The status assignment was previously computed and thrown away.

    The status is snapshotted **at call time** rather than read off
    ``call_args`` afterwards. ``update_document`` is a mock, so ``call_args``
    holds a live reference to the one document object the handler mutates
    throughout; reading ``.status`` after the fact therefore reports FAILED for
    every call, including the POSTPROCESSING write, and the assertion passes even
    when no failure write happened at all. Confirmed by the neuter experiment,
    where this test was the one that stayed green.
    """
    statuses = []
    pr_service.update_document.side_effect = lambda document: (
        statuses.append(document.status),
        document,
    )[1]

    with pytest.raises(Exception):
        _pr_invoke(_document(), [_section_result("2", failed=True, errors=["x"])])

    assert statuses[-1] == Status.FAILED
    # And the earlier, success-path writes were not retro-labelled.
    assert statuses[0] == Status.POSTPROCESSING


@pytest.mark.unit
def test_processresults_document_scope_errors_record_no_section_issue(pr_service):
    """The documented decision, pinned so it cannot drift into silent attribution.

    ``document.errors`` here is document-scope free text — OCR and classification
    append to it without failing the document — and it is what makes this handler
    raise in practice. It is deliberately NOT attributed to a section: guessing a
    section would be a fabricated claim, and a document-level issue would bump
    ``ProcessingIssueCount`` with no text behind the badge.
    """
    document = _document()
    document.errors = ["Page 7 not found in document"]
    with pytest.raises(Exception, match="Page 7 not found"):
        _pr_invoke(document, [_section_result("1", failed=False)])

    # Reading `call_args_list` afterwards is sound HERE, unlike in the two tests
    # above, because this asserts an ABSENCE: the live reference means an issue
    # attributed at any point during the handler — including after the last write —
    # still shows up and still fails this. Do not copy the pattern to a test that
    # asserts something WAS persisted.
    assert pr_service.update_document.call_args_list, "no write happened at all"
    for call in pr_service.update_document.call_args_list:
        for section in call.args[0].sections:
            assert _codes(section) == []


@pytest.mark.unit
def test_processresults_exception_propagates_unchanged(pr_service):
    expected = (
        "Processing failed for 1 out of 1 sections: Processing failed for "
        "section 1: Schema mismatch"
    )
    with pytest.raises(Exception) as caught:
        _pr_invoke(
            _document(), [_section_result("2", failed=True, errors=["Schema mismatch"])]
        )
    assert str(caught.value) == expected


@pytest.mark.unit
def test_processresults_persist_failure_does_not_mask_the_error(pr_service):
    """Only the FAILURE write fails; the success-path writes must still happen."""
    calls = {"n": 0}

    def _update(document):
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("table unavailable")
        return document

    pr_service.update_document.side_effect = _update
    with pytest.raises(Exception) as caught:
        _pr_invoke(_document(), [_section_result("2", failed=True, errors=["boom"])])
    assert "table unavailable" not in str(caught.value)
    assert "Processing failed for 1 out of 1 sections" in str(caught.value)


# ---------------------------------------------------------------------------
# 3. rule-validation-orchestration-function: the recorder that could not run
# ---------------------------------------------------------------------------


def _orch_invoke(document: Document, service, *, fails_with=None):
    consolidator = MagicMock()
    if fails_with is not None:
        consolidator.consolidate_and_save.side_effect = fails_with
    else:
        consolidator.consolidate_and_save.side_effect = lambda document, **kw: document

    with (
        patch.object(orchestration_index, "get_config", lambda **kw: {}),
        patch.object(
            orchestration_index,
            "rule_validation",
            MagicMock(RuleValidationOrchestratorService=lambda **kw: consolidator),
        ),
        patch.object(
            orchestration_index, "create_document_service", lambda *a, **kw: service
        ),
        patch.dict(os.environ, {"WORKING_BUCKET": "", "REPORTING_BUCKET": ""}),
    ):
        return orchestration_index.handler(
            {
                "Result": {"document": document.to_dict()},
                "RuleValidationResults": [],
            },
            _Context(),
        )


@pytest.mark.unit
def test_orchestration_recorder_actually_executes_against_the_real_service():
    """The test whose absence let two fatal faults ship.

    Nothing executed this ``except`` block. Against a REAL
    ``DocumentDynamoDBService`` the old recorder cannot even be called:
    ``Status.ERROR`` is an ``AttributeError`` and ``error_message=`` is a
    ``TypeError``. Both were swallowed by the surrounding ``except``, so the
    observable assertion is that a write now reaches DynamoDB at all.
    """
    service, client = _real_service()
    with pytest.raises(RuntimeError, match="orchestrator model refused"):
        _orch_invoke(
            _document(Status.RULE_VALIDATION_ORCHESTRATOR),
            service,
            fails_with=RuntimeError("orchestrator model refused"),
        )

    assert client.update_item.called, (
        "the failure recorder did not reach DynamoDB; before #1064 it raised "
        "AttributeError on Status.ERROR into an except that swallowed it"
    )
    key = client.update_item.call_args.kwargs["key"]
    # Keyed on input_key. The broken recorder passed document.id, which for this
    # fixture is a different string and would have addressed a nonexistent item.
    assert key == {"PK": "doc#doc.pdf", "SK": "none"}


@pytest.mark.unit
def test_orchestration_write_carries_the_issue_and_the_failed_status():
    """The real expression build, so a shape DynamoDB would reject fails here."""
    service, client = _real_service()
    with pytest.raises(RuntimeError):
        _orch_invoke(
            _document(Status.RULE_VALIDATION_ORCHESTRATOR),
            service,
            fails_with=RuntimeError("orchestrator model refused"),
        )

    values = client.update_item.call_args.kwargs["expression_attribute_values"]
    assert values[":ObjectStatus"] == Status.FAILED.value
    assert values[":WorkflowStatus"] == "FAILED"
    sections = values[":Sections"]
    assert len(sections) == 2
    for section in sections:
        codes = [i["code"] for i in section["ProcessingIssues"]]
        assert codes == [RULE_VALIDATION_NOT_CONSOLIDATED_CODE]
        assert (
            "orchestrator model refused" in section["ProcessingIssues"][0]["rootCause"]
        )
    # Both sections carry one issue, so the list badge must read 2, not 0.
    assert values[":ProcessingIssueCount"] == 2


@pytest.mark.unit
def test_orchestration_marks_every_section_because_none_has_a_verdict():
    service = MagicMock()
    with pytest.raises(RuntimeError):
        _orch_invoke(
            _document(Status.RULE_VALIDATION_ORCHESTRATOR),
            service,
            fails_with=RuntimeError("boom"),
        )
    written = service.update_document.call_args.args[0]
    assert [s.section_id for s in written.sections] == ["1", "2"]
    for section in written.sections:
        assert _codes(section) == [RULE_VALIDATION_NOT_CONSOLIDATED_CODE]


@pytest.mark.unit
def test_orchestration_exception_propagates_unchanged():
    service = MagicMock()
    original = RuntimeError("orchestrator model refused")
    with pytest.raises(RuntimeError) as caught:
        _orch_invoke(
            _document(Status.RULE_VALIDATION_ORCHESTRATOR), service, fails_with=original
        )
    assert caught.value is original


#: A Bedrock throttle as botocore actually raises it. `botocore.errorfactory` names
#: the dynamic class after the modeled error CODE, so the class name is
#: `ThrottlingException` — which is what Step Functions matches against
#: `RuleValidationOrchestration`'s `Retry.ErrorEquals`. Constructing a bare
#: `ClientError` instead gives the class name `ClientError`, which that list does
#: NOT contain: same error code, opposite retry outcome. See
#: `test_a_transient_that_the_state_machine_will_not_retry_records_nothing`.
_ModeledThrottlingException = type(
    "ThrottlingException", (botocore.exceptions.ClientError,), {}
)


@pytest.mark.unit
def test_orchestration_transient_failure_records_nothing():
    """The one site of the three where the transient carve-out is load-bearing.

    This handler re-raises the caught exception **unchanged**, and its task state
    retries the throttling family eight times at 2.5x backoff from ten seconds.
    Marking the sections would show them failed for most of three hours and then
    clear.

    The error is shaped the way botocore really raises a Bedrock throttle — a class
    *named* after the modeled code, which is what `ThrottlingException` being in
    bedrock-runtime's error map produces.

    That name is one the task state already listed, so this is the case where the
    two classifications happened to agree before #1101. It is re-raised as
    `TransientError` all the same: one name on the wire for every transient cause is
    what makes the state machine's list short enough to be right, and both names sit
    in the same retry tier, so the ladder is identical either way.
    """
    service = MagicMock()
    throttle = _ModeledThrottlingException(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Converse"
    )
    assert type(throttle).__name__ == "ThrottlingException", "fixture precondition"
    names = _retry_names("RuleValidationOrchestration")
    assert "ThrottlingException" in names, "the name that already agreed"
    with pytest.raises(TransientError) as surfaced:
        _orch_invoke(
            _document(Status.RULE_VALIDATION_ORCHESTRATOR), service, fails_with=throttle
        )
    assert type(surfaced.value).__name__ in names
    service.update_document.assert_not_called()


def _retry_names(state_name: str) -> set[str]:
    """Every error name the named task state's ``Retry`` list matches on.

    Read from the shipped definition rather than restated here, because the whole
    point of these two tests is that the library's verdict and the state machine's
    are the same question, and a hardcoded copy could agree with neither.
    """
    import json
    import re

    asl = os.path.join(
        os.path.dirname(__file__),
        "../../../../../patterns/unified/statemachine/workflow.asl.json",
    )
    with open(asl, encoding="utf-8") as fh:
        raw = fh.read()
    # The definition carries ``${...}`` substitution tokens that are not JSON.
    # Anchored on the key's closing quote so quoted ARN values are left intact.
    states = json.loads(re.sub(r'"\s*:\s*\$\{[^}]+\}', '": 1', raw))["States"]

    def find(node: dict) -> dict:
        for key, st in node.items():
            if key == state_name:
                return st
            for branch in st.get("Branches", []):
                if hit := find(branch["States"]):
                    return hit
            for sub in ("ItemProcessor", "Iterator"):
                if sub in st and (hit := find(st[sub]["States"])):
                    return hit
        return {}

    state = find(states)
    assert state, f"{state_name} not found in workflow.asl.json"
    return {n for r in state.get("Retry", []) for n in r["ErrorEquals"]}


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    [
        # A throttling CODE under a class name botocore did not derive from the
        # code — what arrives when the error is not modeled on the operation.
        botocore.exceptions.ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "Converse",
        ),
        botocore.exceptions.ReadTimeoutError(endpoint_url="https://bedrock"),
    ],
    ids=["bare-ClientError", "ReadTimeoutError"],
)
def test_a_transient_under_any_class_name_is_surfaced_as_the_name_that_is_retried(
    error,
):
    """#1101: the library's verdict and the state machine's now answer together.

    These two shapes are transient by error code and by exception type, but neither
    class NAME — ``ClientError``, ``ReadTimeoutError`` — is a name a ``Retry`` list
    can usefully carry. The handler converts them to ``TransientError``, which
    ``RuleValidationOrchestration`` lists, so the eight-attempt ladder runs. Nothing
    is recorded on the sections, and that suppression is now sound rather than
    coincidental: the same predicate decides both, so a record is withheld exactly
    when a retry is genuinely coming.

    The ``Retry`` list is read from the shipped definition, so removing the name
    there fails this test rather than silently restoring the old behaviour.
    """
    from idp_common.utils.transient_errors import is_transient_error

    assert is_transient_error(error), "fixture precondition"
    assert type(error).__name__ not in {
        "TransientError",
        "ThrottlingException",
    }, "fixture precondition: a name the task state does not list directly"

    service = MagicMock()
    with pytest.raises(TransientError) as surfaced:
        _orch_invoke(
            _document(Status.RULE_VALIDATION_ORCHESTRATOR), service, fails_with=error
        )
    # Step Functions matches the CLASS NAME, so that is what has to be in the list.
    assert type(surfaced.value).__name__ in _retry_names("RuleValidationOrchestration")
    assert surfaced.value.__cause__ is error, "the cause must stay readable"
    service.update_document.assert_not_called()


@pytest.mark.unit
def test_a_deterministic_failure_is_still_recorded_and_keeps_its_own_name():
    """The other half of #1101, and the property that makes the split worth having.

    A failure a retry cannot fix must not be dressed up as retryable: it keeps its
    own class name, which no ``Retry`` list carries, and it is recorded so the
    Sections panel says why the document failed.
    """
    service = MagicMock()
    with pytest.raises(ValueError, match="rule schema is malformed"):
        _orch_invoke(
            _document(Status.RULE_VALIDATION_ORCHESTRATOR),
            service,
            fails_with=ValueError("rule schema is malformed"),
        )
    assert "ValueError" not in _retry_names("RuleValidationOrchestration")
    service.update_document.assert_called_once()


@pytest.mark.unit
def test_orchestration_sectionless_document_records_nothing_and_still_raises():
    """A whole-map section write with nothing to write must not happen."""
    service = MagicMock()
    document = _document(Status.RULE_VALIDATION_ORCHESTRATOR)
    document.sections = []
    with pytest.raises(RuntimeError, match="boom"):
        _orch_invoke(document, service, fails_with=RuntimeError("boom"))
    service.update_document.assert_not_called()


@pytest.mark.unit
def test_orchestration_persist_failure_does_not_mask_the_error():
    service = MagicMock()
    service.update_document.side_effect = RuntimeError("table unavailable")
    with pytest.raises(RuntimeError) as caught:
        _orch_invoke(
            _document(Status.RULE_VALIDATION_ORCHESTRATOR),
            service,
            fails_with=RuntimeError("orchestrator model refused"),
        )
    assert str(caught.value) == "orchestrator model refused"


# ---------------------------------------------------------------------------
# The class-level guard: no handler may name a Status member that is not there
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_every_status_member_named_by_a_unified_handler_exists():
    """``Status.ERROR`` was the only such reference in the tree, and it was fatal.

    A test of one handler's behaviour would not have found it — nothing executed
    that line. This reads every handler under ``patterns/unified/src`` statically,
    so the next ``Status.<typo>`` fails whether or not its branch is reachable.
    """
    import ast

    members = {m.name for m in Status}
    offenders = []
    for root, _dirs, files in os.walk(_SRC):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "Status"
                    and node.attr not in members
                    # Enum's own API, not a member reference.
                    and node.attr not in {"value", "name", "__members__"}
                ):
                    offenders.append(
                        f"{os.path.relpath(path, _SRC)}:{node.lineno} "
                        f"Status.{node.attr}"
                    )
    assert offenders == [], (
        "these handlers name Status members that do not exist, so the line raises "
        "AttributeError if it is ever reached: " + json.dumps(offenders, indent=2)
    )
