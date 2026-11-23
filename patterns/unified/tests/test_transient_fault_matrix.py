# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every converting task state's handler reports a transient model failure as
``TransientError`` (#2049).

``test_workflow_transient_retry.py`` holds the state machine half: which task states
are permitted to invoke a model (derived from the template's IAM grants), and that
each one lists ``TransientError`` in its ``Retry`` or carries a reason for not doing
so. This file is the handler half, over the same universe. For each Lambda behind a
converting state, the real handler runs the real service down to the model call, and
that call fails with each of the shapes a transient Bedrock or Textract failure
arrives in:

* a modeled throttle (botocore builds a ``ClientError`` subclass named
  ``ThrottlingException`` for one),
* a bare HTTP 5xx (a ``ClientError`` whose code is ``"500"``, which is what botocore
  builds when the reply names no error, #2036),
* a read timeout (``botocore.exceptions.ReadTimeoutError``).

Each must leave the handler as ``TransientError`` carrying the injected failure as its
cause, which is the name every converting state's retrier lists. An untyped
``Exception``, a FAILED document returned normally, or the raw botocore error would
each fail the document with no retry, because the states do not list
``ReadTimeoutError`` or ``ClientError``. As a control, a deterministic
``ValidationException`` at the same call must NOT become ``TransientError``, or the
state machine would retry a document that can never succeed.

Where the service fans out (summarization sections, OCR pages), the fault is injected
into one call among several, because a failure recorded for one worker while the
others succeed is the shape the services most often swallowed.

The boundary is ``idp_common.bedrock.invoke_model`` for every Bedrock call these
services make, Textract's ``AnalyzeDocument`` for the default OCR backend, and the
shard runner for the agentic shard runtime, whose model calls go through a Strands
agent rather than ``invoke_model``. Replacing ``invoke_model`` bypasses the Bedrock
client's own retry ladder, so each fault stands for one that outlasted it; that
ladder is tested in ``lib/idp_common_pkg/tests/unit/bedrock``. A malformed or
truncated model reply is not covered here: what a stage should do with one differs
by stage, so it is not a property one assertion can state for all of them.

One test per handler, looping the failure kinds inside it, so the count stays small:
storage, configuration and the document service are stubbed, nothing sleeps, and
nothing leaves the process. ``test_the_matrix_covers_every_converting_handler`` ties
the set of drivers to the derived universe in both directions.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Callable, Dict
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from idp_common.config.merge_utils import merge_config_with_defaults
from idp_common.config.models import IDPConfig
from idp_common.models import Document, Page, RuleValidationResult, Section, Status
from idp_common.utils.transient_errors import TransientError

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[1] / "src"

#: botocore builds a class of this name for a modeled throttle.
ThrottlingException = type("ThrottlingException", (ClientError,), {})


def _throttle() -> BaseException:
    return ThrottlingException(
        {"Error": {"Code": "ThrottlingException", "Message": "Too many tokens"}},
        "Converse",
    )


def _bare_500() -> BaseException:
    return ClientError(
        {
            "Error": {"Code": "500", "Message": "Internal Server Error"},
            "ResponseMetadata": {"HTTPStatusCode": 500},
        },
        "Converse",
    )


def _read_timeout() -> BaseException:
    return ReadTimeoutError(endpoint_url="https://bedrock-runtime.example")


def _deterministic() -> BaseException:
    return ClientError(
        {"Error": {"Code": "ValidationException", "Message": "malformed input"}},
        "Converse",
    )


TRANSIENT_FAULTS: Dict[str, Callable[[], BaseException]] = {
    "modeled throttle": _throttle,
    "bare HTTP 500": _bare_500,
    "read timeout": _read_timeout,
}


def _load(rel: str, name: str):
    """Import a handler with X-Ray disabled and its global patching suppressed."""
    path = _SRC / rel
    with (
        patch.dict(os.environ, {"AWS_XRAY_SDK_ENABLED": "false"}),
        patch("aws_xray_sdk.core.patch_all"),
    ):
        if str(path.parent) not in sys.path:
            sys.path.insert(0, str(path.parent))
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


_PRESET = (
    Path(__file__).resolve().parents[3]
    / "config_library/unified/lending-package-sample/config.yaml"
)


@pytest.fixture(scope="module")
def config() -> IDPConfig:
    """A shipped preset merged onto the system defaults, built once for the module."""
    import yaml

    preset = yaml.safe_load(_PRESET.read_text(encoding="utf-8"))
    # One small class instead of the preset's six. Extraction builds a Pydantic model
    # from the class schema on every call, and the preset's payslip schema made that
    # most of this file's run time while changing nothing about the error paths.
    preset["classes"] = [
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "Payslip",
            "x-aws-idp-document-type": "Payslip",
            "type": "object",
            "description": "An employee's pay statement.",
            "properties": {
                "YTDNetPay": {"type": "string", "description": "Net pay to date."}
            },
        }
    ]
    return IDPConfig.model_validate(merge_config_with_defaults(preset))


class Injector:
    """Stands in for the model call: raises ``fault`` on the calls ``selects`` picks
    and answers the rest with ``answer``. ``hits`` counts the raises, so a driver
    whose stubs never reach the call cannot pass by default."""

    def __init__(self, fault: BaseException, selects=None, answer=None):
        self.fault = fault
        self.selects = selects or (lambda *a, **kw: True)
        self.answer = answer
        self.hits = 0
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.selects(*args, **kwargs):
            self.hits += 1
            raise self.fault
        if self.answer is None:
            raise AssertionError("unexpected model call with no answer scripted")
        return self.answer(*args, **kwargs)


def _check(handler_name: str, run: Callable[[Callable[..., Injector]], None]) -> None:
    """Drive the handler once per fault kind and report every wrong outcome at once.

    ``run(make)`` invokes the handler after installing ``make(**kw)``, an
    :class:`Injector` for the current fault, at the model call. Each kind gets a
    fresh fault and fresh injectors, so no state carries from one kind to the next.
    """
    wrong = []
    for kind, build in {**TRANSIENT_FAULTS, "deterministic": _deterministic}.items():
        fault = build()
        made = []

        def make(**kwargs):
            made.append(Injector(fault, **kwargs))
            return made[-1]

        outcome = None
        try:
            with patch.dict(os.environ, {"AWS_XRAY_SDK_ENABLED": "false"}):
                run(make)
        except Exception as raised:  # noqa: BLE001 - the outcome under test
            outcome = raised
        hits = sum(i.hits for i in made)
        calls = sum(i.calls for i in made)
        if not hits:
            wrong.append(
                f"{kind}: the fault was never raised at the model call "
                f"(the handler ended with {outcome!r})"
            )
        elif kind == "deterministic":
            if isinstance(outcome, TransientError):
                wrong.append(f"{kind}: became TransientError, so it would be retried")
        elif isinstance(outcome, TransientError):
            if fault not in _causes(outcome):
                wrong.append(f"{kind}: TransientError does not carry the fault")
        elif outcome is None:
            wrong.append(f"{kind}: the handler returned normally ({calls} model calls)")
        else:
            wrong.append(
                f"{kind}: left the handler as {type(outcome).__name__}, not "
                "TransientError"
            )
    assert not wrong, f"{handler_name}: " + "; ".join(wrong)


def _causes(exc: BaseException):
    node: BaseException | None = exc
    while node is not None:
        yield node
        node = node.__cause__


def _model_answer(text: str):
    def answer(*args, **kwargs):
        return {
            "response": {"output": {"message": {"content": [{"text": text}]}}},
            "metering": {},
        }

    return answer


def _content_text(kwargs) -> str:
    return "".join(
        block.get("text", "")
        for block in kwargs.get("content") or []
        if isinstance(block, dict)
    )


def _two_section_document(status: Status) -> Document:
    doc = Document(
        id="run1/packet.pdf",
        input_key="run1/packet.pdf",
        input_bucket="in",
        output_bucket="out",
        status=status,
    )
    doc.pages = {
        str(n): Page(
            page_id=str(n),
            image_uri=f"s3://out/run1/packet.pdf/pages/{n}/image.jpg",
            raw_text_uri=f"s3://out/run1/packet.pdf/pages/{n}/raw.json",
            parsed_text_uri=f"s3://out/run1/packet.pdf/pages/{n}/result.json",
            text_confidence_uri=(
                f"s3://out/run1/packet.pdf/pages/{n}/textConfidence.json"
            ),
        )
        for n in (1, 2)
    }
    doc.sections = [
        Section(section_id=str(n), classification="Letter", page_ids=[str(n)])
        for n in (1, 2)
    ]
    return doc


@contextlib.contextmanager
def _offline_storage(json_content=None):
    """S3 reads answered from memory, writes discarded, metrics dropped.

    ``idp_common.image`` imports ``get_binary_content`` by name, so it is patched
    there as well as on ``idp_common.s3``.
    """
    with (
        patch("idp_common.s3.get_text_content", return_value="page text"),
        patch("idp_common.s3.get_binary_content", return_value=_png()),
        patch("idp_common.image.get_binary_content", return_value=_png()),
        patch("idp_common.s3.get_json_content", return_value=json_content or {}),
        patch("idp_common.s3.write_content"),
        patch("idp_common.metrics.put_metric"),
        patch.object(Document, "serialize_document", return_value={}),
    ):
        yield


# ---------------------------------------------------------------------------
# One driver per Lambda behind a converting state
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def summarization_module():
    return _load(
        "summarization_function/index.py", "summarization_function_fault_matrix"
    )


def test_summarization(summarization_module, config):
    """Two shapes, because the service has two paths: a document with sections is
    summarised one section per pool worker, and the fault hits the second section
    only; a document with none is summarised in one call. The first is where the
    service swallowed the failure; the second raises it raw, so only the handler's
    wrapper converts it."""
    module = summarization_module

    for shape, sections in (("sections", True), ("whole document", False)):

        def run(make, sections=sections):
            call = make(
                selects=lambda **kw: (
                    "<page-number>2</page-number>" in _content_text(kw)
                ),
                answer=_model_answer('{"summary": "ok"}'),
            )
            document = _two_section_document(Status.ASSESSING)
            if not sections:
                document.sections = []
            with (
                patch.object(module.Document, "load_document", return_value=document),
                patch.object(module, "get_config", return_value=config),
                patch.object(module, "create_document_service"),
                patch.object(module, "calculate_lambda_metering", return_value={}),
                patch("idp_common.bedrock.invoke_model", call),
                _offline_storage(json_content={"summary": "ok"}),
            ):
                module.handler({"document": {"id": "x"}}, None)

        _check(f"SummarizationFunction ({shape})", run)


def _two_page_pdf() -> bytes:
    import io

    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument.new()
    for _ in range(2):
        pdf.new_page(72, 72)
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def _png() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _first_call_only():
    """Select exactly one call among concurrent ones, whichever arrives first."""
    import threading

    lock = threading.Lock()
    taken = []

    def selects(*args, **kwargs):
        with lock:
            if taken:
                return False
            taken.append(True)
            return True

    return selects


@pytest.fixture(scope="module")
def ocr_module():
    return _load("ocr_function/index.py", "ocr_function_fault_matrix")


def test_ocr(ocr_module, config):
    """A two-page PDF on each backend that calls a service per page: Textract (the
    default) and Bedrock. The fault hits one page while the other succeeds."""
    module = ocr_module
    pdf = _two_page_pdf()
    textract_answer = {"DocumentMetadata": {"Pages": 1}, "Blocks": []}
    bedrock_config = config.model_copy(deep=True)
    bedrock_config.ocr.backend = "bedrock"

    for backend_config in (config, bedrock_config):
        backend = backend_config.ocr.backend

        def run(make, backend_config=backend_config, backend=backend):
            call = make(
                selects=_first_call_only(),
                answer=(
                    (lambda *a, **kw: textract_answer)
                    if backend == "textract"
                    else _model_answer("page text")
                ),
            )
            client = MagicMock()
            client.get_object.return_value = {"Body": MagicMock(read=lambda: pdf)}
            client.analyze_document.side_effect = call
            client.detect_document_text.side_effect = call
            with (
                patch.dict(os.environ, {"AWS_REGION": "us-east-1"}),
                patch.object(
                    module.Document,
                    "load_document",
                    return_value=Document(
                        id="run1/packet.pdf",
                        input_key="run1/packet.pdf",
                        input_bucket="in",
                        output_bucket="out",
                        status=Status.QUEUED,
                    ),
                ),
                patch.object(module, "get_config", return_value=backend_config),
                patch.object(module, "create_document_service"),
                patch.object(module, "discover_existing_ocr_pages", return_value={}),
                patch.object(module, "calculate_lambda_metering", return_value={}),
                patch.object(module, "metrics"),
                patch("idp_common.ocr.service.boto3.client", return_value=client),
                patch("idp_common.bedrock.invoke_model", call),
                _offline_storage(),
            ):
                module.handler({"document": {}, "execution_arn": "a"}, None)

        _check(f"OCRFunction ({backend})", run)

    def config_load_fails(make):
        """Not a model call, but the one route a fault takes through this handler
        without passing the service: the configuration read before OCR starts."""
        with (
            patch.object(
                module.Document, "load_document", return_value=Document(id="d")
            ),
            patch.object(module, "get_config", side_effect=make()),
            patch.object(module, "create_document_service"),
        ):
            module.handler({"document": {}, "execution_arn": "a"}, None)

    _check("OCRFunction (configuration load)", config_load_fails)


@pytest.fixture(scope="module")
def classification_module():
    return _load(
        "classification_function/index.py", "classification_function_fault_matrix"
    )


def test_classification(classification_module, config):
    """Two pages classified in the pool; the fault hits one of them."""
    module = classification_module

    def run(make):
        call = make(
            selects=_first_call_only(),
            answer=_model_answer('{"class": "Payslip"}'),
        )
        document = _two_section_document(Status.OCR)
        document.sections = []
        with (
            patch.object(module.Document, "load_document", return_value=document),
            patch.object(module, "get_config", return_value=config),
            patch.object(module, "create_document_service"),
            patch.object(module, "calculate_lambda_metering", return_value={}),
            patch.object(module, "metrics"),
            patch("idp_common.bedrock.invoke_model", call),
            _offline_storage(),
        ):
            module.handler({"OCRResult": {"document": {}}, "execution_arn": "a"}, None)

    _check("ClassificationFunction", run)


def _one_section_document(status: Status, classification: str) -> Document:
    document = _two_section_document(status)
    document.sections = [
        Section(section_id="1", classification=classification, page_ids=["1", "2"])
    ]
    return document


@pytest.fixture(scope="module")
def extraction_module():
    return _load("extraction_function/index.py", "extraction_function_fault_matrix")


def test_extraction(extraction_module, config):
    """The in-process extraction path, on the preset's default (simple) mode."""
    module = extraction_module

    def run(make):
        call = make(answer=_model_answer("{}"))
        with (
            patch.object(
                module.Document,
                "load_document",
                return_value=_one_section_document(Status.CLASSIFYING, "Payslip"),
            ),
            patch.object(module, "get_config", return_value=config),
            patch.object(module, "create_document_service"),
            patch.object(module, "calculate_lambda_metering", return_value={}),
            patch.object(module, "metrics"),
            patch("idp_common.bedrock.invoke_model", call),
            _offline_storage(),
        ):
            module.handler({"document": {}, "section_id": "1"}, None)

    _check("ExtractionFunction", run)


@pytest.fixture(scope="module")
def agentic_config(config) -> IDPConfig:
    """The preset on the agentic extraction path, which the shard runtime serves."""
    agentic = config.model_copy(deep=True)
    agentic.extraction.mode = "advanced"
    agentic.extraction.agentic.enabled = True
    # One page per shard, so the two-page section plans two shards.
    agentic.extraction.agentic.max_pages_per_shard = 1
    return agentic


@pytest.fixture(scope="module")
def shard_runtime_module():
    return _load(
        "extraction_function/sfn_runtime_handler.py", "sfn_runtime_fault_matrix"
    )


def test_shard_runtime(shard_runtime_module, agentic_config):
    """ShardExtractionStep: one shard, the agent's model call failing.

    Plan, shard and merge share this handler and its wrapper; the shard is the mode
    that calls the model. The boundary here is the shard runner, the callable the
    runtime hands each shard to and which drives the Strands agent's Bedrock calls.
    """
    from idp_common.extraction.runtime import NoopShardPersistence

    module = shard_runtime_module

    def run(make):
        call = make()

        async def shard_runner(**kwargs):
            return call(**kwargs)

        with (
            patch.object(
                module.Document,
                "load_document",
                return_value=_one_section_document(Status.CLASSIFYING, "Payslip"),
            ),
            patch.object(module, "get_config", return_value=agentic_config),
            patch.object(module, "create_document_service"),
            patch.object(module, "_persistence", return_value=NoopShardPersistence()),
            patch(
                "idp_common.extraction.agentic_idp.default_shard_runner", shard_runner
            ),
            _offline_storage(),
        ):
            module.handler(
                {"document": {}, "section_id": "1", "mode": "shard", "shard_index": 0},
                None,
            )

    _check("ShardRuntimeFunction", run)


@pytest.fixture(scope="module")
def assessment_module():
    return _load("assessment_function/index.py", "assessment_function_fault_matrix")


def test_assessment(assessment_module, config):
    """AssessmentStep: one section's confidence call failing.

    A deterministic failure here is degraded to "no confidence scores" and the
    section returns normally, which is the control's expected outcome."""
    module = assessment_module

    def run(make):
        call = make(answer=_model_answer("{}"))
        document = _one_section_document(Status.EXTRACTING, "Payslip")
        result_uri = "s3://out/run1/packet.pdf/sections/1/result.json"
        document.sections[0].extraction_result_uri = result_uri
        with (
            patch.object(module.Document, "load_document", return_value=document),
            patch.object(module, "get_config", return_value=config),
            patch.object(module, "create_document_service"),
            patch.object(module, "calculate_lambda_metering", return_value={}),
            patch("idp_common.bedrock.invoke_model", call),
            _offline_storage(
                json_content={"inference_result": {"YTDNetPay": "1,000.00"}}
            ),
        ):
            module.handler({"document": {"id": "x"}, "section_id": "1"}, None)

    _check("AssessmentFunction", run)


_RULE_PRESET = (
    Path(__file__).resolve().parents[3]
    / "config_library/unified/rule-validation/config.yaml"
)


@pytest.fixture(scope="module")
def rule_config() -> IDPConfig:
    """The rule-validation preset, built once for the module."""
    import yaml

    preset = yaml.safe_load(_RULE_PRESET.read_text(encoding="utf-8"))
    return IDPConfig.model_validate(merge_config_with_defaults(preset))


@pytest.fixture(scope="module")
def rule_validation_module():
    return _load(
        "rule-validation-function/index.py", "rule_validation_function_fault_matrix"
    )


def test_rule_validation(rule_validation_module, rule_config):
    """PolicyRuleValidationStep: the per-rule model calls for one section."""
    module = rule_validation_module

    def run(make):
        call = make(
            selects=_first_call_only(),
            answer=_model_answer('{"recommendation": "Pass", "reasoning": "ok"}'),
        )
        document = _one_section_document(Status.EXTRACTING, "Payslip")
        # Policy classification ran before this state and matched one policy.
        document.rule_validation_result = RuleValidationResult(
            request_id="run1", matched_policy_types=["global_periods"]
        )
        with (
            patch.dict(os.environ, {"AWS_REGION": "us-east-1"}),
            patch.object(module.Document, "load_document", return_value=document),
            patch.object(module, "get_config", return_value=rule_config),
            patch.object(module, "create_document_service"),
            patch.object(module, "calculate_lambda_metering", return_value={}),
            patch("idp_common.bedrock.invoke_model", call),
            _offline_storage(),
        ):
            module.handler({"document": {}, "section_id": "1"}, None)

    _check("RuleValidationFunction", run)


@pytest.fixture(scope="module")
def orchestration_module():
    return _load(
        "rule-validation-orchestration-function/index.py",
        "rule_validation_orchestration_fault_matrix",
    )


def test_rule_validation_orchestration(orchestration_module, rule_config):
    """PolicyRuleConsolidationStep: the consolidation model call over two sections'
    results, which is the only shape that reaches the model (one section is stored
    without one)."""
    module = orchestration_module
    config_dict = rule_config.model_dump()

    def section(n: str) -> Document:
        document = Document(id="run1/packet.pdf", input_key="run1/packet.pdf")
        document.rule_validation_result = RuleValidationResult(
            request_id="run1",
            output_uri=(
                "s3://out/run1/packet.pdf/rule_validation/sections/"
                f"section_{n}_responses.json"
            ),
        )
        return document

    def run(make):
        call = make(
            selects=_first_call_only(),
            answer=_model_answer('{"recommendation": "Pass", "reasoning": "ok"}'),
        )
        documents = {
            "base": _two_section_document(Status.RULE_VALIDATION),
            "1": section("1"),
            "2": section("2"),
        }
        with (
            patch.object(
                module.Document,
                "load_document",
                side_effect=lambda data, *a, **kw: documents[data["which"]],
            ),
            patch.object(module, "get_config", return_value=config_dict),
            patch.object(module, "create_document_service"),
            patch.object(module, "persist_failed_document"),
            patch.object(module, "calculate_lambda_metering", return_value={}),
            patch("idp_common.bedrock.invoke_model", call),
            _offline_storage(
                json_content={
                    "responses": {
                        "global_periods": [
                            {"rule": "minor_surgery_000_010", "recommendation": "Pass"}
                        ]
                    }
                }
            ),
        ):
            module.handler(
                {
                    "Result": {"document": {"which": "base"}},
                    "RuleValidationResults": [
                        {"section_id": n, "document": {"which": n}} for n in ("1", "2")
                    ],
                },
                None,
            )

    _check("RuleValidationOrchestrationFunction", run)


def _is_validation_error(fault: BaseException) -> bool:
    return (
        isinstance(fault, ClientError)
        and fault.response["Error"]["Code"] == "ValidationException"
    )


@pytest.fixture(scope="module")
def evaluation_module():
    return _load("evaluation_function/index.py", "evaluation_function_fault_matrix")


def test_evaluation(evaluation_module, config):
    """EvaluationStep: the LLM-judge comparator's Bedrock call, through the real
    evaluation service and Stickler, for a document of two sections whose one field
    each is judged by the LLM method. The fault hits one section's judge call while
    the other answers.

    The deterministic control is stricter than the matrix's: the handler must
    return normally with the faulted field scored 0.0 and the evaluation recorded
    COMPLETED, which is today's behaviour for a judge error that a retry cannot
    fix. A transient fault must leave without a FAILED evaluation having been
    written first (#2122), since the retry is about to run it again."""
    module = evaluation_module
    eval_config = config.model_copy(deep=True)
    eval_config.evaluation.enabled = True
    eval_config.classes = [
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "Letter",
            "x-aws-idp-document-type": "Letter",
            "type": "object",
            "properties": {
                "Agency": {
                    "type": "string",
                    "description": "The agency the letter is from.",
                    "x-aws-idp-evaluation-method": "LLM",
                }
            },
        }
    ]
    sides = {"actual": "Different Agency", "expected": "AnyCompany Media"}

    def document(side: str) -> Document:
        doc = _two_section_document(Status.SUMMARIZING)
        for section in doc.sections:
            section.extraction_result_uri = f"s3://out/{side}/{section.section_id}.json"
        return doc

    def read_json(uri: str):
        side = uri.split("/")[3]
        return {"inference_result": {"Agency": sides[side]}}

    outcomes = []

    def run(make):
        call = make(
            selects=_first_call_only(),
            answer=_model_answer('{"match": true, "score": 1.0, "reason": "same"}'),
        )
        statuses = []
        service = MagicMock()
        service.update_document.side_effect = lambda doc: (
            statuses.append(doc.evaluation_status) or doc
        )
        actual = document("actual")
        outcomes.append((call, statuses, actual))
        with (
            patch.object(module.Document, "load_document", return_value=actual),
            patch.object(module, "get_config", return_value=eval_config),
            patch.object(module, "_document_service", service),
            patch.object(
                module, "load_baseline_document", return_value=document("expected")
            ),
            patch.object(module, "calculate_lambda_metering", return_value={}),
            patch.object(module, "boto3"),
            patch("idp_common.bedrock.invoke_model", call),
            _offline_storage(),
            patch("idp_common.s3.get_json_content", side_effect=read_json),
        ):
            module.handler({"document": {"id": "x"}}, None)

    _check("EvaluationFunction", run)

    wrong = []
    for call, statuses, actual in outcomes:
        if call.fault is not None and _is_validation_error(call.fault):
            scores = (
                sorted(
                    attribute.score
                    for section in actual.evaluation_result.section_results
                    for attribute in section.attributes
                )
                if actual.evaluation_result
                else None
            )
            if scores != [0.0, 1.0] or statuses[-1:] != ["COMPLETED"]:
                wrong.append(
                    f"deterministic: expected the faulted field scored 0.0 beside a "
                    f"1.0 and COMPLETED recorded, got scores {scores}, statuses "
                    f"{statuses}"
                )
        elif "FAILED" in statuses:
            wrong.append(
                f"{type(call.fault).__name__}: a FAILED evaluation was recorded "
                f"before the retry ({statuses})"
            )
    assert not wrong, "EvaluationFunction: " + "; ".join(wrong)


def _s3_error(code: str, status: int) -> BaseException:
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "GetObject",
    )


def test_evaluation_s3_reads(evaluation_module, config):
    """EvaluationStep's S3 reads, as opposed to its judge call (#2129).

    The handler reads the document it was handed, the baseline document's pages
    and sections (the real ``Document.from_s3`` over a stubbed listing), and each
    section's result once for the doc split metrics and once more through
    ``EvaluationService._prepare_stickler_data`` for the field comparison. A
    transient failure at any of those reads must leave as ``TransientError``
    carrying the fault, with no FAILED evaluation written first. Each reader has a
    fallback for a failed read (empty data, a dropped page or section, a recorded
    metrics error, or a ``ValueError``), and a transient fault that reaches one
    produces a COMPLETED evaluation with depressed scores, or a failure with no
    retry when the ``ValueError`` does not chain its cause for
    ``is_transient_error`` to find.

    The control is a missing object (``NoSuchKey``) at the same read, which keeps
    its fallback: an extraction result that is not there scores its section's
    field as a miss, a baseline section that is not there leaves its actual
    section unscored, a baseline page or a doc split read that fails changes no
    field score, and a document that cannot be loaded fails as a ``ValueError``."""
    module = evaluation_module
    eval_config = config.model_copy(deep=True)
    eval_config.evaluation.enabled = True
    eval_config.classes = [
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "Letter",
            "x-aws-idp-document-type": "Letter",
            "type": "object",
            "properties": {
                "Agency": {
                    "type": "string",
                    "description": "The agency the letter is from.",
                    "x-aws-idp-evaluation-method": "EXACT",
                }
            },
        }
    ]
    key = "run1/packet.pdf"
    baseline_uri = f"s3://baseline/{key}/sections/1/result.json"
    actual_uri = "s3://out/actual/1.json"
    value = {"inference_result": {"Agency": "AnyCompany Media"}}

    def actual_document() -> Document:
        doc = _two_section_document(Status.SUMMARIZING)
        for section in doc.sections:
            section.extraction_result_uri = f"s3://out/actual/{section.section_id}.json"
        return doc

    def baseline_client():
        client = MagicMock()

        def paginate(Prefix, **kwargs):
            kind = Prefix.rstrip("/").rsplit("/", 1)[-1]
            prefixes = [{"Prefix": f"{key}/{kind}/{n}/"} for n in (1, 2)]
            return [{"CommonPrefixes": prefixes}]

        client.get_paginator.return_value.paginate.side_effect = paginate
        return client

    def nth_read_of(uri: str, n: int):
        seen = []

        def selects(read_uri, *args, **kwargs):
            if read_uri == uri:
                seen.append(read_uri)
                return len(seen) == n
            return False

        return selects

    # Each result.json is read once per reader, in this order: Document.from_s3
    # (baseline only), the doc split metrics, then _prepare_stickler_data.
    sites = {
        "actual document load": None,
        "baseline document load": (baseline_uri, 1),
        "baseline page read": (f"s3://baseline/{key}/pages/1/result.json", 1),
        "doc split section read": (actual_uri, 1),
        "actual extraction result": (actual_uri, 2),
        "baseline extraction result": (baseline_uri, 3),
    }
    faults = {
        "S3 SlowDown": lambda: _s3_error("SlowDown", 503),
        "bare HTTP 503": lambda: _s3_error("503", 503),
        "read timeout": lambda: ReadTimeoutError(endpoint_url="https://s3.example"),
        "NoSuchKey (control)": lambda: _s3_error("NoSuchKey", 404),
    }
    # What a missing object yields today at each read: the sorted field scores of a
    # COMPLETED evaluation, or the exception type the handler fails with.
    control = {
        "actual document load": ValueError,
        "actual extraction result": [0.0, 1.0],
        "baseline document load": [1.0],
        "baseline page read": [1.0, 1.0],
        "doc split section read": [1.0, 1.0],
        "baseline extraction result": [0.0, 1.0],
    }

    wrong = []
    for site, read_at in sites.items():
        for kind, build in faults.items():
            fault = build()
            selects = read_at and nth_read_of(*read_at)
            read = Injector(
                fault,
                selects=selects or (lambda *a, **kw: False),
                answer=lambda uri: value,
            )
            load = (
                Injector(fault)
                if selects is None
                else MagicMock(return_value=actual_document())
            )
            statuses = []
            service = MagicMock()
            service.update_document.side_effect = lambda doc: (
                statuses.append(doc.evaluation_status) or doc
            )
            outcome = None
            with (
                patch.dict(os.environ, {"AWS_XRAY_SDK_ENABLED": "false"}),
                patch.object(module.Document, "load_document", load),
                patch.object(module, "BASELINE_BUCKET", "baseline"),
                patch.object(module, "get_config", return_value=eval_config),
                patch.object(module, "_document_service", service),
                patch.object(module, "calculate_lambda_metering", return_value={}),
                patch.object(module, "boto3"),
                patch("boto3.client", return_value=baseline_client()),
                _offline_storage(),
                patch("idp_common.s3.get_json_content", read),
            ):
                try:
                    module.handler({"document": {"id": "x"}}, None)
                except Exception as raised:  # noqa: BLE001 - the outcome under test
                    outcome = raised
            where = f"{site} / {kind}"
            injected = load if selects is None else read
            if not injected.hits:
                wrong.append(f"{where}: the fault was never raised ({outcome!r})")
            elif kind.endswith("(control)"):
                expected = control[site]
                if isinstance(expected, type):
                    if type(outcome) is not expected:
                        wrong.append(
                            f"{where}: expected {expected.__name__}, got {outcome!r}"
                        )
                    continue
                result = load.return_value.evaluation_result
                scores = (
                    sorted(
                        a.score for s in result.section_results for a in s.attributes
                    )
                    if result
                    else None
                )
                if (
                    outcome is not None
                    or scores != expected
                    or statuses[-1:] != ["COMPLETED"]
                ):
                    wrong.append(
                        f"{where}: expected scores {expected} and COMPLETED, got "
                        f"{outcome!r}, scores {scores}, statuses {statuses}"
                    )
            elif not isinstance(outcome, TransientError):
                wrong.append(
                    f"{where}: ended with {outcome!r} (statuses {statuses}), not "
                    "TransientError, so EvaluationStep does not retry"
                )
            elif fault not in _causes(outcome):
                wrong.append(f"{where}: TransientError does not carry the fault")
            elif "FAILED" in statuses:
                wrong.append(f"{where}: FAILED was recorded before the retry")
    assert not wrong, "EvaluationFunction S3 reads:\n" + "\n".join(wrong)


# ---------------------------------------------------------------------------
# Closure: one driver per Lambda in the derived universe, in both directions
# ---------------------------------------------------------------------------

#: The Lambda logical id each driver above exercises. Several states can share one
#: Lambda (plan, shard and merge all run ShardRuntimeFunction), and the handler is
#: what converts, so the universe is the set of Lambdas rather than of states.
DRIVERS = {
    "OCRFunction": test_ocr,
    "ClassificationFunction": test_classification,
    "ExtractionFunction": test_extraction,
    "ShardRuntimeFunction": test_shard_runtime,
    "AssessmentFunction": test_assessment,
    "RuleValidationFunction": test_rule_validation,
    "RuleValidationOrchestrationFunction": test_rule_validation_orchestration,
    "SummarizationFunction": test_summarization,
    "EvaluationFunction": test_evaluation,
}


def test_the_matrix_covers_every_converting_handler():
    """The universe is derived by ``test_workflow_transient_retry.py``: every task
    state the template permits to invoke a model and whose Retry lists
    ``TransientError``. A Lambda that joins it without a driver here fails, and so
    does a driver whose Lambda left it, which would otherwise keep passing while
    guarding nothing the state machine retries."""
    from test_workflow_transient_retry import converting_model_states

    universe = set(converting_model_states().values())
    assert len(universe) >= 8, f"the derived universe is suspiciously small: {universe}"
    missing = sorted(universe - set(DRIVERS))
    stale = sorted(set(DRIVERS) - universe)
    assert not missing, (
        f"these Lambdas back a converting, model-capable state and have no driver "
        f"in this file: {missing}"
    )
    assert not stale, f"these drivers name a Lambda outside the universe: {stale}"
