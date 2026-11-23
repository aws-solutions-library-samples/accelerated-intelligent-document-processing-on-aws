# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``idp_common.reporting.pricing`` is the reporting writer's arithmetic, not a copy.

The SDK's fit check prices documents with ``estimate_metering_cost``, and the
reporting writer stores a per-row ``estimated_cost`` and emits the document total as
``DocumentEstimatedCost``. These tests drive ``SaveReportingData.save_metering_data``
over one metering map holding every pricing outcome (priced, an entry that omits a
unit, no entry at all, a zero count, a declared-free unit, a non-numeric count, a
suffix-walk match) and assert that the stored rows, the emitted metric and the pure
function agree to the last bit, and that the figures are the hand-computed ones.
"""

from unittest.mock import patch

import pytest

from idp_common.config.models import IDPConfig
from idp_common.models import Document
from idp_common.reporting import pricing
from idp_common.reporting.save_reporting_data import SaveReportingData

PRICING = {
    "pricing": [
        {
            "name": "bedrock/us.amazon.nova-lite-v1:0",
            "units": [
                {"name": "inputTokens", "price": "0.00000006"},
                {"name": "outputTokens", "price": "0.00000024"},
            ],
        },
        {
            "name": "textract/analyze_document-Layout+Tables",
            "units": [{"name": "pages", "price": "0.02"}],
        },
    ]
}

METERING = {
    # Priced on both units.
    "Extraction/bedrock/us.amazon.nova-lite-v1:0": {
        "inputTokens": 1000,
        "outputTokens": "250",
        # Entry exists, unit not listed, non-zero count: unpriced (NULL).
        "reasoningTokens": 7,
        # Entry exists, unit not listed, zero count: a measured $0.00.
        "cacheReadInputTokens": 0,
    },
    # A key with extra leading context: priced through the suffix walk.
    "OCR/x/textract/analyze_document-Layout+Tables": {"pages": 3},
    # No pricing entry at all: unpriced.
    "Classification/bedrock/vendor.unpriced-model-v1:0": {"inputTokens": 10},
    # A count that is not a number becomes 1.0.
    "Summarization/bedrock/us.amazon.nova-lite-v1:0": {"outputTokens": "n/a"},
}

EXPECTED_TOTAL = 1000 * 0.00000006 + 250 * 0.00000024 + 3 * 0.02 + 1 * 0.00000024


def _run_writer():
    config = IDPConfig.model_validate(PRICING)
    reporter = SaveReportingData("test-bucket", config=config)
    document = Document(id="doc.pdf", input_key="doc.pdf", num_pages=3)
    document.metering = METERING
    saved = {}

    def capture(records, key, schema):
        saved["records"] = records

    with (
        patch.object(reporter, "_save_records_as_parquet", side_effect=capture),
        patch(
            "idp_common.reporting.save_reporting_data.emit_document_cost_metrics"
        ) as metric,
    ):
        reporter.save_metering_data(document)
    return config, saved["records"], metric.call_args.kwargs


@pytest.mark.unit
def test_the_writer_and_the_pure_function_compute_the_same_cost():
    config, records, metric = _run_writer()
    pure = pricing.estimate_metering_cost(
        METERING, pricing.build_pricing_map(config.pricing)
    )

    priced = [r["estimated_cost"] for r in records if r["estimated_cost"] is not None]
    row_total = None
    for cost in priced:
        row_total = cost if row_total is None else row_total + cost

    assert pure.estimated_cost_usd == row_total == metric["estimated_cost"]
    assert pure.unpriced_row_count == metric["unpriced_row_count"] == 2
    assert pure.estimated_cost_usd == pytest.approx(EXPECTED_TOTAL, rel=1e-12)
    assert pure.unpriced_rows == {
        "bedrock/us.amazon.nova-lite-v1:0 [reasoningTokens]",
        "bedrock/vendor.unpriced-model-v1:0 [inputTokens]",
    }


@pytest.mark.unit
def test_rows_keep_their_context_service_and_unit():
    _config, records, _metric = _run_writer()
    by_key = {(r["context"], r["service_api"], r["unit"]): r for r in records}
    assert len(by_key) == len(records) == 7
    zero = by_key[
        ("Extraction", "bedrock/us.amazon.nova-lite-v1:0", "cacheReadInputTokens")
    ]
    assert zero["unit_cost"] == 0.0 and zero["estimated_cost"] == 0.0
    walked = by_key[("OCR", "x/textract/analyze_document-Layout+Tables", "pages")]
    assert walked["unit_cost"] == 0.02
    coerced = by_key[
        ("Summarization", "bedrock/us.amazon.nova-lite-v1:0", "outputTokens")
    ]
    assert coerced["value"] == 1.0


@pytest.mark.unit
def test_no_priced_row_is_none_not_zero():
    result = pricing.estimate_metering_cost({"A/bedrock/unknown-model": {"x": 5}}, {})
    assert result.estimated_cost_usd is None
    assert result.unpriced_row_count == 1


@pytest.mark.unit
def test_the_pricing_module_imports_without_pyarrow():
    import subprocess
    import sys

    code = (
        "import sys; sys.modules['pyarrow'] = None; "
        "import idp_common.reporting.pricing as p; print(p.unit_cost({}, 'a', 'b'))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "None"


@pytest.mark.unit
def test_the_reporting_package_resolves_its_writer_on_first_use():
    import idp_common.reporting as reporting_package

    assert reporting_package.SaveReportingData is SaveReportingData
    with pytest.raises(AttributeError, match="no attribute 'NotThere'"):
        getattr(reporting_package, "NotThere")


class _Unit:
    def __init__(self, name, price):
        self.name, self.price = name, price


class _Entry:
    def __init__(self, name, units):
        self.name, self.units = name, units


@pytest.mark.unit
def test_an_invalid_price_is_skipped_but_its_entry_is_kept():
    pricing_map = pricing.build_pricing_map(
        [
            _Entry(
                "bedrock/m", [_Unit("inputTokens", "nan"), _Unit("outputTokens", "0.5")]
            )
        ]
    )
    assert pricing_map == {"bedrock/m": {"outputTokens": 0.5}}
    only_invalid = pricing.build_pricing_map([_Entry("bedrock/x", [_Unit("u", "-1")])])
    assert only_invalid == {"bedrock/x": {}}
    # The entry stays, so the unit is reported unpriced rather than priced elsewhere.
    assert pricing.unit_cost(only_invalid, "bedrock/x", "u", 3) is None


@pytest.mark.unit
def test_no_pricing_section_and_a_malformed_one_give_an_empty_map(caplog):
    assert pricing.build_pricing_map(None) == {}
    assert pricing.build_pricing_map([object()]) == {}
    assert "Error processing pricing" in caplog.text


@pytest.mark.unit
def test_a_key_without_context_is_its_own_service_api():
    rows = list(pricing.iter_metering_rows({"textract": {"pages": "2"}}))
    assert rows == [("", "textract", "pages", 2.0)]
