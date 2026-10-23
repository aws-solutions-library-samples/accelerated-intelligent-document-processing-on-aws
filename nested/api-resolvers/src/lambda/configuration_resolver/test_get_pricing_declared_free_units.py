# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The `getPricing` response carries the free-unit declarations for the UI.

The web UI's document cost table used to decide the absent-unit case itself and
answered `0` for every unit a pricing entry omits, including those the reporting
table records as an explicit NULL because nobody has priced them (issue #1406).
The fix is NOT a second copy of the backend's rule: the resolver now expands the
declarations in ``idp_common.metering_units`` through
:func:`declared_free_units` and sends the result on ``nonChargeableUnits``, so
the panel reads the verdict off the response rather than forming its own.

What this suite asserts is that the field is populated correctly and only from
the shipped registers — not what the panel does with it (that is covered by
``src/ui/src/components/document-panel/__tests__/pricing.test.ts`` and the
corresponding ``metering-cost`` tests), and not what the declarations say (that
is covered by ``tests/unit/reporting/test_declared_free_units.py``).

Properties:

* Every shipped Bedrock entry's answer is present and has the expected units.
  ``totalTokens`` is declared free service-wide, ``requests`` is declared free
  service-wide, and the two cache units are declared per key for the eleven
  pricing entries whose basis is recorded in ``EXPECTED_CACHE_UNIT_OMISSIONS``.
* Textract and ``bda`` entries have no declaration and are therefore absent from
  the answer — not carried as an empty list.
* A user-added pricing entry is covered on the same terms as a shipped one: a
  pricing entry naming a service the registers do name earns the service-scoped
  declaration; one naming a service they do not reports no declared free unit.
* A pricing payload that is malformed in any of the ways JSON can be malformed
  does not raise and does not fabricate a declaration.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("CONFIGURATION_TABLE_NAME", "test-config-table")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


def _load_index():
    spec = importlib.util.spec_from_file_location(
        "config_resolver_index_pricing_free", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["config_resolver_index_pricing_free"] = module
    spec.loader.exec_module(module)
    return module


index = _load_index()


@pytest.mark.unit
def test_a_bedrock_entry_reports_its_service_scoped_free_units():
    """``totalTokens`` and ``requests`` on every Bedrock entry.

    They are declared service-wide — not a billing dimension of Bedrock model
    inference at all — so every ``bedrock/`` entry in the payload reports them,
    including a model id the cache register does not name.
    """
    pricing = {"pricing": [{"name": "bedrock/some.user-added-model", "units": []}]}
    answers = index._non_chargeable_units(pricing)
    assert answers["bedrock/some.user-added-model"] == [
        "requests",
        "totalTokens",
    ]


@pytest.mark.unit
def test_a_bedrock_entry_with_a_cache_zero_rate_adds_the_published_zero_unit():
    """Nova Lite's cache-write is published at exactly $0.00.

    So the per-key cache register declares ``cacheWriteInputTokens`` free on this
    key, on top of the two service-scoped Bedrock declarations. Its cache-READ
    rate is a real priced dimension and so is NOT declared free — a rate turning
    up there has to reach the user as unpriced rather than as zero, which the
    expansion of ``priced_by_aws=True`` entries guarantees.
    """
    pricing = {"pricing": [{"name": "bedrock/amazon.nova-lite-v1:0", "units": []}]}
    answers = index._non_chargeable_units(pricing)
    assert set(answers["bedrock/amazon.nova-lite-v1:0"]) == {
        "cacheWriteInputTokens",
        "requests",
        "totalTokens",
    }


@pytest.mark.unit
def test_a_bedrock_entry_with_a_cache_basis_also_reports_the_cache_units():
    """Haiku has no cache dimension in any region.

    So the cache register declares both cache units free on this key, in
    addition to the two service-scoped Bedrock declarations above. This is the
    per-key branch of the expansion — the UI reader is spared from knowing it
    exists.
    """
    pricing = {
        "pricing": [
            {
                "name": "bedrock/us.anthropic.claude-3-haiku-20240307-v1:0",
                "units": [],
            }
        ]
    }
    answers = index._non_chargeable_units(pricing)
    assert set(answers["bedrock/us.anthropic.claude-3-haiku-20240307-v1:0"]) == {
        "cacheReadInputTokens",
        "cacheWriteInputTokens",
        "requests",
        "totalTokens",
    }


@pytest.mark.unit
def test_a_textract_or_bda_entry_is_absent_from_the_answer():
    """Nothing declared, nothing reported.

    An empty list would be a positive claim that no unit is free, which is
    exactly the per-row question the panel then has to re-decide. Leaving the
    key out means the UI's lookup walks past the empty answer and reads an
    omitted unit as unpriced — the same reading the reporting table records for
    it.
    """
    pricing = {
        "pricing": [
            {"name": "textract/detect_document_text", "units": []},
            {"name": "bda/documents-standard", "units": []},
            {"name": "lambda/duration", "units": []},
        ]
    }
    answers = index._non_chargeable_units(pricing)
    assert "textract/detect_document_text" not in answers
    assert "bda/documents-standard" not in answers
    assert "lambda/duration" not in answers


@pytest.mark.unit
def test_a_lambda_hook_entry_reports_its_two_declared_free_units():
    """Both are judgement entries in the register — a hook proxying Bedrock
    reports a ``totalTokens`` that is already billed via its input and output,
    and the ``requests`` BedrockClient injects for shape parity is not a third
    party's billing dimension.
    """
    pricing = {
        "pricing": [{"name": "lambda_hook/GENAIIDP-mistral-ocr-hook", "units": []}]
    }
    answers = index._non_chargeable_units(pricing)
    assert answers["lambda_hook/GENAIIDP-mistral-ocr-hook"] == [
        "requests",
        "totalTokens",
    ]


@pytest.mark.unit
def test_a_malformed_pricing_payload_does_not_raise_or_fabricate():
    """Robustness in every direction JSON can be malformed.

    This is the response the dispatcher assembles for the UI, and the fallback
    on the browser side is a parse-to-``{}`` — but reaching that fallback means
    the resolver has not raised, and the alternative is a 500 on every panel
    render.
    """
    assert index._non_chargeable_units({}) == {}
    assert index._non_chargeable_units({"pricing": None}) == {}
    assert index._non_chargeable_units({"pricing": "not a list"}) == {}
    assert index._non_chargeable_units({"pricing": [None, "not a dict", 7]}) == {}
    assert index._non_chargeable_units({"pricing": [{"units": []}]}) == {}
    assert index._non_chargeable_units({"pricing": [{"name": "", "units": []}]}) == {}
    assert index._non_chargeable_units({"pricing": [{"name": 42, "units": []}]}) == {}


@pytest.mark.unit
def test_a_sagemaker_entry_the_registers_do_not_name_is_absent():
    """The registers are closed, which is the whole point of them.

    A service that neither register names earns no declared free unit and does
    not appear in the answer. This is what makes the browser's reading of the
    response strict rather than optimistic.
    """
    pricing = {"pricing": [{"name": "sagemaker/endpoint", "units": []}]}
    assert index._non_chargeable_units(pricing) == {}
