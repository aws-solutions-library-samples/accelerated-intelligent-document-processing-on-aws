# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Cohere Embed v4 metering rows are priced, by both cost paths.

The embeddings section labeller meters each Bedrock embedding call under
``SectionClassification/bedrock/<model>`` with ``requests``, ``inputTokens`` and
``totalTokens``. An image is billed as input tokens by its pixel count, and
Bedrock reports that count in the same header as a text call's, so one rate
prices both: $0.12 per million input tokens, the InputTokenCount rate in AWS's
price list (see the note on the rows in ``config_library/pricing.yaml``).

The rows are built by the labeller itself, for the model id it resolves in each
Region family, so a change to the metering key or to the Region defaults reaches
this test rather than a hand-typed copy of them. The expected rate is stated
here, independently of ``pricing.yaml``, so a wrong shipped rate fails too.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from idp_common.classification.embeddings_labeler import EmbeddingSectionLabeler
from idp_common.config.models import IDPConfig
from idp_common.models import Document
from idp_common.reporting import pricing
from idp_common.reporting.save_reporting_data import SaveReportingData

_PRICING_PATH = Path(__file__).resolve().parents[5] / "config_library" / "pricing.yaml"

#: $0.12 per million input tokens.
EMBED_V4_INPUT_RATE = 0.12 / 1_000_000

#: Input-token counts for one text call and one image call. The image count is
#: what Bedrock reported for one US Letter page scaled to 512 pixels (measured
#: during the embeddings-labelling benchmark); the text count is illustrative.
TEXT_TOKENS = 37
IMAGE_TOKENS = 996

#: One Region per family the embedding default is swapped in.
REGIONS = ("us-east-1", "eu-west-1", "ca-central-1")


def _labeler(region: str) -> EmbeddingSectionLabeler:
    service = SimpleNamespace(region=region)
    section_cfg = SimpleNamespace(
        embeddings=SimpleNamespace(model=""), low_confidence=None
    )
    return EmbeddingSectionLabeler(service, section_cfg)


def _metering(region: str) -> tuple[str, dict]:
    """A document's metering after one text and one image embedding call."""
    labeler = _labeler(region)
    text = labeler._metering_for(TEXT_TOKENS)
    image = labeler._metering_for(IMAGE_TOKENS)
    (key,) = text
    assert set(image) == {key}
    merged = {
        unit: text[key][unit] + image[key][unit] for unit in text[key] | image[key]
    }
    return key, {key: merged}


@pytest.fixture(scope="module")
def shipped_config():
    return IDPConfig.model_validate(yaml.safe_load(_PRICING_PATH.read_text()))


def _writer_rows(config, metering):
    reporter = SaveReportingData("test-bucket", config=config)
    document = Document(id="doc.pdf", input_key="doc.pdf", num_pages=1)
    document.metering = metering
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
    return saved["records"], metric.call_args.kwargs


@pytest.mark.unit
def test_the_three_regions_resolve_three_embed_v4_profiles():
    """Guards the parametrisation below: it must cover three distinct ids."""
    ids = {_labeler(region).model_id for region in REGIONS}
    assert ids == {
        "us.cohere.embed-v4:0",
        "eu.cohere.embed-v4:0",
        "global.cohere.embed-v4:0",
    }


@pytest.mark.unit
@pytest.mark.parametrize("region", REGIONS)
def test_the_reporting_writer_prices_embed_v4_input_tokens(region, shipped_config):
    key, metering = _metering(region)
    records, metric = _writer_rows(shipped_config, metering)
    by_unit = {r["unit"]: r for r in records}

    assert set(by_unit) == {"requests", "inputTokens", "totalTokens"}
    tokens = by_unit["inputTokens"]
    assert tokens["value"] == TEXT_TOKENS + IMAGE_TOKENS
    assert tokens["unit_cost"] == pytest.approx(EMBED_V4_INPUT_RATE, rel=1e-12, abs=0)
    assert tokens["estimated_cost"] == pytest.approx(
        (TEXT_TOKENS + IMAGE_TOKENS) * EMBED_V4_INPUT_RATE, rel=1e-12, abs=0
    )
    # Declared non-chargeable for bedrock, so a measured $0.00, not a NULL.
    for unit in ("requests", "totalTokens"):
        assert by_unit[unit]["estimated_cost"] == 0.0, (key, unit)
    assert metric["unpriced_row_count"] == 0
    assert metric["estimated_cost"] == pytest.approx(
        (TEXT_TOKENS + IMAGE_TOKENS) * EMBED_V4_INPUT_RATE, rel=1e-12, abs=0
    )


@pytest.mark.unit
@pytest.mark.parametrize("region", REGIONS)
def test_estimate_metering_cost_prices_embed_v4_input_tokens(region, shipped_config):
    _key, metering = _metering(region)
    result = pricing.estimate_metering_cost(
        metering, pricing.build_pricing_map(shipped_config.pricing)
    )
    assert result.unpriced_rows == set()
    assert result.unpriced_row_count == 0
    assert result.estimated_cost_usd == pytest.approx(
        (TEXT_TOKENS + IMAGE_TOKENS) * EMBED_V4_INPUT_RATE, rel=1e-12, abs=0
    )

    records, metric = _writer_rows(shipped_config, metering)
    assert result.estimated_cost_usd == metric["estimated_cost"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("label", "tokens"), [("text", TEXT_TOKENS), ("image", IMAGE_TOKENS)]
)
def test_a_text_call_and_an_image_call_are_priced_at_the_same_rate(
    label, tokens, shipped_config
):
    labeler = _labeler("us-east-1")
    result = pricing.estimate_metering_cost(
        labeler._metering_for(tokens),
        pricing.build_pricing_map(shipped_config.pricing),
    )
    assert result.unpriced_row_count == 0, label
    assert result.estimated_cost_usd == pytest.approx(
        tokens * EMBED_V4_INPUT_RATE, rel=1e-12, abs=0
    )
