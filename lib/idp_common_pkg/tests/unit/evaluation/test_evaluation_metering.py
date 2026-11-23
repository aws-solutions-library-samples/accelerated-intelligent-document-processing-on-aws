# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Evaluation's LLM comparisons are metered into the document, as `Evaluation`.

`compare_llm` called Bedrock with no `context`, so every comparison was metered
under the client's default `Unspecified`, and the metering the call returned was
dropped, so none of it reached `Document.metering` and from there the reporting
`metering` table and the tracking row.

These tests drive `EvaluationService.evaluate_document` for real — Stickler, the
IDP comparator, `compare_llm` and `BedrockClient.invoke_model` — with only the
boto3 clients stubbed: `bedrock-runtime` by a `MagicMock` standing in for the
client's `_client`, and S3 by moto. The expected metering key is built with the
client's own `metering_model_id`, and the expected token counts from the usage
the stub returns, counted per `converse` call actually made.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

import idp_common.s3 as s3_module
from idp_common.bedrock.client import BedrockClient
from idp_common.bedrock.model_utils import metering_model_id
from idp_common.config.models import EvaluationLLMMethodConfig
from idp_common.evaluation.service import EvaluationService
from idp_common.evaluation.stickler_backend.comparators import compare_llm
from idp_common.models import Document, Section

pytestmark = pytest.mark.unit

_BUCKET = "eval-metering-bucket"
_MODEL = EvaluationLLMMethodConfig().model
# The context is spelled out rather than imported: it is the published name the
# reporting `metering.context` column carries, so a rename must fail here.
EVALUATION_METERING_CONTEXT = "Evaluation"
_KEY = f"{EVALUATION_METERING_CONTEXT}/bedrock/{metering_model_id(_MODEL)}"
_USAGE = {"inputTokens": 11, "outputTokens": 3, "totalTokens": 14}
_PRIOR = {"Extraction/bedrock/some-model": {"inputTokens": 100, "outputTokens": 7}}

_CONFIG = {
    "classes": [
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "Invoice",
            "x-aws-idp-document-type": "Invoice",
            "type": "object",
            "properties": {
                "Agency": {
                    "type": "string",
                    "description": "The agency placing the order",
                    "x-aws-idp-evaluation-method": "LLM",
                }
            },
        }
    ]
}


def _stub_client(reply: str) -> BedrockClient:
    client = BedrockClient(region="us-east-1", metrics_enabled=False)
    client._client = MagicMock()
    client._client.converse.return_value = {
        "output": {"message": {"content": [{"text": reply}]}},
        "stopReason": "end_turn",
        "usage": dict(_USAGE),
    }
    return client


_VERDICT = json.dumps({"match": False, "score": 0.0, "reason": "different"})


def _put(s3, key: str, agency: str) -> str:
    s3.put_object(
        Bucket=_BUCKET,
        Key=key,
        Body=json.dumps({"inference_result": {"Agency": agency}}),
    )
    return f"s3://{_BUCKET}/{key}"


def _document(s3, tag: str, agencies: List[str], *, expected: bool) -> Document:
    side = "expected" if expected else "actual"
    sections = [
        Section(
            section_id=str(i + 1),
            classification="Invoice",
            page_ids=[str(i + 1)],
            extraction_result_uri=_put(s3, f"{tag}/{side}/{i}.json", agency),
        )
        for i, agency in enumerate(agencies)
    ]
    return Document(
        id=f"{tag}-doc",
        input_key=f"{tag}/input.pdf",
        output_bucket=_BUCKET,
        sections=sections,
        metering={} if expected else json.loads(json.dumps(_PRIOR)),
    )


def _evaluate(
    tag: str,
    expected: List[str],
    actual: List[str],
    reply: str = _VERDICT,
    *,
    store_results: bool = False,
    output_bucket: Optional[str] = _BUCKET,
    max_workers: int = 2,
) -> tuple[Document, BedrockClient]:
    """Evaluate one document whose sections each hold one LLM-compared field."""
    client = _stub_client(reply)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_BUCKET)
        with (
            patch.object(s3_module, "_s3_client", s3),
            patch("idp_common.bedrock.invoke_model", client.invoke_model),
        ):
            actual_doc = _document(s3, tag, actual, expected=False)
            actual_doc.output_bucket = output_bucket
            expected_doc = _document(s3, tag, expected, expected=True)
            service = EvaluationService(
                region="us-east-1", config=_CONFIG, max_workers=max_workers
            )
            result = service.evaluate_document(
                actual_doc, expected_doc, store_results=store_results
            )
    return result, client


def _calls(client: BedrockClient) -> int:
    return client._client.converse.call_count


def _one_call_metering() -> Dict[str, Any]:
    """What the client itself meters for one evaluation call on the stub."""
    metering = _stub_client(_VERDICT).invoke_model(
        model_id=_MODEL,
        system_prompt="s",
        content=[{"text": "t"}],
        context=EVALUATION_METERING_CONTEXT,
    )["metering"]
    assert list(metering) == [_KEY]
    assert metering[_KEY]["inputTokens"] == _USAGE["inputTokens"]
    return metering[_KEY]


def _expected_metering(calls: int) -> Dict[str, Dict[str, Any]]:
    return {
        **_PRIOR,
        _KEY: {unit: value * calls for unit, value in _one_call_metering().items()},
    }


def test_two_sections_in_parallel_are_both_metered_as_evaluation() -> None:
    """Each section runs in a pool thread; both comparisons reach the document."""
    result, client = _evaluate(
        "parallel",
        expected=["AnyCompany Media", "Example Corp"],
        actual=["Different Agency One", "Different Agency Two"],
    )
    assert _calls(client) == 2
    assert not result.errors
    assert result.metering == _expected_metering(2)


def test_no_evaluation_call_is_metered_as_unspecified() -> None:
    result, client = _evaluate(
        "context", expected=["AnyCompany Media"], actual=["Another Agency"]
    )
    assert _calls(client) == 1
    assert client._client.converse.call_args.kwargs["modelId"] == _MODEL
    assert not [k for k in result.metering if k.startswith("Unspecified/")]
    assert result.metering == _expected_metering(1)


def test_a_judge_answer_that_cannot_be_parsed_is_still_metered() -> None:
    """The call was billed even though its verdict was unusable."""
    result, client = _evaluate(
        "unparseable",
        expected=["AnyCompany Media"],
        actual=["Another Agency"],
        reply="I am unable to decide.",
    )
    assert _calls(client) == 1
    assert result.metering == _expected_metering(1)


def test_metering_survives_an_evaluation_that_fails_after_the_calls() -> None:
    """A document with no output bucket fails after scoring; the spend stays."""
    result, client = _evaluate(
        "failed",
        expected=["AnyCompany Media"],
        actual=["Another Agency"],
        store_results=True,
        output_bucket=None,
    )
    assert _calls(client) == 1
    assert any("Output bucket is required" in e for e in result.errors)
    assert result.metering == _expected_metering(1)


def test_comparisons_that_need_no_call_add_no_metering() -> None:
    """Identical values short-circuit: nothing billed, nothing recorded."""
    result, client = _evaluate(
        "identical", expected=["AnyCompany Media"], actual=["anycompany  media"]
    )
    assert _calls(client) == 0
    assert result.metering == _PRIOR


def test_metering_is_not_shared_between_concurrent_scopes() -> None:
    """Two collection scopes on two threads each see only their own calls."""
    from idp_common.evaluation.stickler_backend.comparators import (
        collect_evaluation_metering,
    )

    client = _stub_client(_VERDICT)
    results: Dict[str, Dict[str, Any]] = {}
    barrier = threading.Barrier(2)

    def run(name: str, calls: int) -> None:
        with collect_evaluation_metering() as collector:
            barrier.wait()
            for i in range(calls):
                compare_llm(
                    f"{name} expected {i}",
                    f"{name} actual {i}",
                    bedrock_invoker=client.invoke_model,
                )
            results[name] = collector.snapshot()

    threads = [
        threading.Thread(target=run, args=("one", 1)),
        threading.Thread(target=run, args=("three", 3)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results["one"][_KEY]["inputTokens"] == _USAGE["inputTokens"]
    assert results["three"][_KEY]["inputTokens"] == 3 * _USAGE["inputTokens"]


def test_a_call_outside_any_scope_is_recorded_nowhere() -> None:
    from idp_common.evaluation.stickler_backend.comparators import (
        collect_evaluation_metering,
    )

    client = _stub_client(_VERDICT)
    with collect_evaluation_metering() as collector:
        pass
    matched, _score, _reason = compare_llm(
        "AnyCompany Media", "Another Agency", bedrock_invoker=client.invoke_model
    )
    assert matched is False
    assert _calls(client) == 1
    assert collector.snapshot() == {}
