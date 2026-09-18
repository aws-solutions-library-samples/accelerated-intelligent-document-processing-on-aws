#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Pricing-lookup tests for ``SaveReportingData._get_unit_cost`` (GitHub issue #926).

Three things are pinned here, and they exist because none of them was covered
before:

1. **No substring fallback.** The lookup used to accept a pricing key that was
   merely a substring of the requested ``service_api`` (or the reverse), and a
   unit name that was merely a substring of the requested unit. Since
   ``inputTokens`` is the first unit in every pricing row, ``cacheReadInputTokens``
   bound to it and cache reads were priced at the FRESH INPUT rate.

2. **Cache reads are priced at the cache-read rate.** Every expected number below
   is written out by hand from ``config_library/pricing.yaml`` and multiplied by
   hand, never by calling the code under test. The old
   ``test_cost_calculation.py`` asserted only ``> 0``, which is exactly why a
   10x error survived in it.

3. **Forward coverage.** Every Bedrock model the repository can actually select
   must have an exact pricing entry. That is what stops the next added model from
   silently re-entering the substring era.
"""

import re
from pathlib import Path

import pytest
import yaml

from idp_common.config.models import IDPConfig
from idp_common.reporting.save_reporting_data import SaveReportingData

# repo root: lib/idp_common_pkg/tests/unit/reporting/<this file> -> up 5 parents.
_REPO_ROOT = Path(__file__).resolve().parents[5]
_PRICING_PATH = _REPO_ROOT / "config_library" / "pricing.yaml"

TOKEN_UNITS = (
    "inputTokens",
    "outputTokens",
    "cacheReadInputTokens",
    "cacheWriteInputTokens",
)


@pytest.fixture(scope="module")
def shipped_pricing_raw():
    """The shipped pricing table, as parsed YAML."""
    return yaml.safe_load(_PRICING_PATH.read_text())


@pytest.fixture(scope="module")
def shipped_reporter(shipped_pricing_raw):
    """A reporter loaded with the real shipped pricing table."""
    return SaveReportingData(
        "test-bucket", config=IDPConfig.model_validate(shipped_pricing_raw)
    )


# --------------------------------------------------------------------------- #
# 1. The substring fallback is gone                                           #
# --------------------------------------------------------------------------- #


@pytest.fixture
def toy_reporter():
    """Two models whose IDs are in a prefix relationship, plus a 3-unit entry.

    ``bedrock/toy-model`` is a strict substring of ``bedrock/toy-model-xl``, which
    is the shape that let the old lookup bind one model to another.
    """
    return SaveReportingData(
        "test-bucket",
        config=IDPConfig.model_validate(
            {
                "pricing": [
                    {
                        "name": "bedrock/toy-model",
                        "units": [
                            {"name": "inputTokens", "price": "1.0E-6"},
                            {"name": "outputTokens", "price": "5.0E-6"},
                            {"name": "cacheReadInputTokens", "price": "1.0E-7"},
                        ],
                    },
                    {
                        "name": "textract/detect_document_text",
                        "units": [{"name": "pages", "price": "0.0015"}],
                    },
                ]
            }
        ),
    )


@pytest.mark.unit
def test_longer_model_id_does_not_bind_to_shorter_pricing_key(toy_reporter):
    """'bedrock/toy-model-xl' must NOT be priced off 'bedrock/toy-model'.

    The old rule accepted it because the pricing key is a substring of the model
    ID. That is how eu.amazon.nova-2-lite-v1:0:flex — a real template-selectable
    ID with no pricing row — was silently priced off the base-tier row.
    """
    assert toy_reporter._get_unit_cost("bedrock/toy-model-xl", "inputTokens") is None
    assert (
        toy_reporter._get_unit_cost("bedrock/toy-model-xl", "cacheReadInputTokens")
        is None
    )


@pytest.mark.unit
def test_shorter_model_id_does_not_bind_to_longer_pricing_key(toy_reporter):
    """The reverse direction: model ID a substring of the pricing key."""
    assert toy_reporter._get_unit_cost("bedrock/toy", "inputTokens") is None


@pytest.mark.unit
def test_cache_read_never_falls_back_to_the_input_rate(toy_reporter):
    """A unit absent from an existing entry must not inherit a sibling unit's price.

    ``bedrock/toy-model`` lists no ``cacheWriteInputTokens``. The old unit-level
    substring rule matched 'inputtokens' inside 'cachewriteinputtokens' and
    returned 1.0E-6. It must be $0.00 — not chargeable — instead.
    """
    assert (
        toy_reporter._get_unit_cost("bedrock/toy-model", "cacheWriteInputTokens") == 0.0
    )
    # ...and the cache-read unit that IS listed still resolves to its own rate,
    # a tenth of the input rate, not to the input rate.
    assert (
        toy_reporter._get_unit_cost("bedrock/toy-model", "cacheReadInputTokens")
        == 1.0e-7
    )


@pytest.mark.unit
def test_unpriced_service_returns_none_not_zero(toy_reporter):
    """An absent service is None ("unpriced"), which the caller records as NULL.

    A silent 0.0 is indistinguishable from something genuinely free and quietly
    understates spend, so a miss has to be a different value, not a cheap one.
    """
    assert (
        toy_reporter._get_unit_cost("bedrock/not.in.the.table", "inputTokens") is None
    )
    assert toy_reporter._get_unit_cost("sagemaker/endpoint", "seconds") is None


@pytest.mark.unit
def test_unit_not_chargeable_for_an_existing_service_is_zero(toy_reporter):
    """Every Bedrock call meters 'totalTokens' and 'requests'; neither is charged.

    These are the routine case that rules out raising on a miss: they occur on
    every single Bedrock invocation, so an exception would abort metering for the
    whole document. The entry exists, so this is not a pricing gap -> $0.00.
    """
    assert toy_reporter._get_unit_cost("bedrock/toy-model", "totalTokens") == 0.0
    assert toy_reporter._get_unit_cost("bedrock/toy-model", "requests") == 0.0


@pytest.mark.unit
def test_leading_context_components_are_stripped_longest_suffix_first(toy_reporter):
    """Same longest-suffix rule as benchmarks/harness/lib.py::price_metering."""
    assert (
        toy_reporter._get_unit_cost("OCR/textract/detect_document_text", "pages")
        == 0.0015
    )
    assert (
        toy_reporter._get_unit_cost("textract/detect_document_text", "pages") == 0.0015
    )


# --------------------------------------------------------------------------- #
# 2. Cache-read rates, by hand, against the shipped table                     #
# --------------------------------------------------------------------------- #

# (model id, inputTokens, cacheReadInputTokens, cacheWriteInputTokens) — read off
# config_library/pricing.yaml by hand. The read/write columns are NOT computed
# from the input column: they are transcribed, so a bad multiplier in the YAML
# fails here rather than being reproduced by the test.
CACHE_RATE_CASES = [
    # Claude: cache read 0.1x input, cache write 1.25x input.
    ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", 3.3e-6, 3.3e-7, 4.125e-6),
    ("us.anthropic.claude-sonnet-4-6", 3.3e-6, 3.3e-7, 4.125e-6),
    ("us.anthropic.claude-sonnet-5", 3.3e-6, 3.3e-7, 4.125e-6),
    ("us.anthropic.claude-sonnet-4-6:1m", 6.6e-6, 6.6e-7, 8.25e-6),
    ("us.anthropic.claude-3-5-sonnet-20240620-v1:0", 3.0e-6, 3.0e-7, 3.75e-6),
    ("us.anthropic.claude-3-5-sonnet-20241022-v2:0", 3.0e-6, 3.0e-7, 3.75e-6),
    ("us.anthropic.claude-3-7-sonnet-20250219-v1:0", 3.0e-6, 3.0e-7, 3.75e-6),
    ("us.anthropic.claude-sonnet-4-20250514-v1:0", 3.0e-6, 3.0e-7, 3.75e-6),
    ("us.anthropic.claude-haiku-4-5-20251001-v1:0", 1.1e-6, 1.1e-7, 1.4e-6),
    ("eu.anthropic.claude-sonnet-4-5-20250929-v1:0", 3.3e-6, 3.3e-7, 4.125e-6),
    ("global.anthropic.claude-sonnet-4-5-20250929-v1:0", 3.0e-6, 3.0e-7, 3.75e-6),
    # Nova: cache read 0.25x input, cache write 1.0x input.
    ("us.amazon.nova-lite-v1:0", 6.0e-8, 1.5e-8, 6.0e-8),
    ("us.amazon.nova-pro-v1:0", 8.0e-7, 2.0e-7, 8.0e-7),
    ("us.amazon.nova-2-lite-v1:0", 3.0e-7, 7.5e-8, 3.0e-7),
    ("us.amazon.nova-2-lite-v1:0:flex", 1.5e-7, 3.75e-8, 1.5e-7),
    ("us.amazon.nova-2-lite-v1:0:priority", 5.25e-7, 1.31e-7, 5.25e-7),
    ("eu.amazon.nova-2-lite-v1:0", 3.9e-7, 9.75e-8, 3.9e-7),
    # Added by this change; the two IDs the substring rule was mis-binding.
    ("eu.amazon.nova-2-lite-v1:0:flex", 1.95e-7, 4.88e-8, 1.95e-7),
    ("eu.amazon.nova-2-lite-v1:0:priority", 6.83e-7, 1.71e-7, 6.83e-7),
    ("global.amazon.nova-2-lite-v1:0:flex", 1.5e-7, 3.75e-8, 1.5e-7),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    "model_id,input_rate,cache_read_rate,cache_write_rate", CACHE_RATE_CASES
)
def test_cache_read_priced_at_the_cache_read_rate(
    shipped_reporter, model_id, input_rate, cache_read_rate, cache_write_rate
):
    """Each cache unit resolves to its OWN rate, and each is cheaper/dearer than input.

    ``cache_read_rate != input_rate`` is asserted explicitly: it is the single
    assertion that would have caught issue #926, because the bug's whole signature
    was cache reads coming back equal to the input rate.
    """
    service_api = f"bedrock/{model_id}"
    assert shipped_reporter._get_unit_cost(service_api, "inputTokens") == input_rate
    assert (
        shipped_reporter._get_unit_cost(service_api, "cacheReadInputTokens")
        == cache_read_rate
    )
    assert (
        shipped_reporter._get_unit_cost(service_api, "cacheWriteInputTokens")
        == cache_write_rate
    )
    assert cache_read_rate != input_rate, (
        "test data error: a cache-read rate equal to the input rate cannot "
        "detect the substring mis-binding"
    )


@pytest.mark.unit
def test_cache_heavy_document_cost_computed_by_hand(shipped_reporter):
    """A cache-heavy Sonnet 4.5 metering map, costed by hand.

    Numbers below are arithmetic done here, not by the code under test:

        inputTokens            2,000 x 3.3E-6  = 0.0066
        cacheReadInputTokens 100,000 x 3.3E-7  = 0.033
        cacheWriteInputTokens  8,000 x 4.125E-6 = 0.033
        outputTokens           1,500 x 1.65E-5 = 0.02475
                                                --------
                                                 0.09735

    Under the substring fallback the 100,000 cache reads were priced at the
    3.3E-6 input rate, i.e. 0.33 instead of 0.033 — a $0.297 overcharge on this
    one document, and a reported total of 0.39435 instead of 0.09735 (+305%).
    """
    api = "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    counts = {
        "inputTokens": 2_000,
        "cacheReadInputTokens": 100_000,
        "cacheWriteInputTokens": 8_000,
        "outputTokens": 1_500,
    }
    total = sum(n * shipped_reporter._get_unit_cost(api, u) for u, n in counts.items())
    assert total == pytest.approx(0.09735, rel=1e-12)

    # The specific mis-binding, pinned: cache reads are a tenth of input, and the
    # overcharge the old rule produced is exactly 10x on this unit.
    cache_read = shipped_reporter._get_unit_cost(api, "cacheReadInputTokens")
    input_rate = shipped_reporter._get_unit_cost(api, "inputTokens")
    assert cache_read == 3.3e-7
    assert input_rate == 3.3e-6
    assert input_rate / cache_read == pytest.approx(10.0)


@pytest.mark.unit
def test_govcloud_claude_cache_read_is_zero_not_the_input_rate(shipped_reporter):
    """The sharpest same-model instance of the bug, pinned.

    ``bedrock/us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0`` deliberately
    lists no cache units (verified live: cachePoint does not reduce input tokens
    through that inference profile). The old unit-level substring rule returned
    its 3.3E-6 inputTokens price for ``cacheReadInputTokens`` — the fresh-input
    rate, 10x the commercial cache-read rate. It must be $0.00.
    """
    api = "bedrock/us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0"
    assert shipped_reporter._get_unit_cost(api, "inputTokens") == 3.3e-6
    assert shipped_reporter._get_unit_cost(api, "cacheReadInputTokens") == 0.0
    assert shipped_reporter._get_unit_cost(api, "cacheWriteInputTokens") == 0.0


@pytest.mark.unit
def test_unpriced_service_records_null_cost_in_the_metering_row():
    """An unpriced service must reach the reporting table as NULL, not $0.00."""
    from datetime import datetime, timezone
    from unittest.mock import patch

    from idp_common.models import Document

    reporter = SaveReportingData(
        "test-bucket",
        config=IDPConfig.model_validate(
            {
                "pricing": [
                    {
                        "name": "bedrock/priced-model",
                        "units": [{"name": "inputTokens", "price": "1.0E-6"}],
                    }
                ]
            }
        ),
    )
    document = Document(
        id="doc-unpriced",
        input_key="doc.pdf",
        num_pages=1,
        initial_event_time=datetime.now(timezone.utc).isoformat(),
        metering={
            "Extraction/bedrock/priced-model": {"inputTokens": 1_000, "requests": 1},
            "Extraction/bedrock/unpriced-model": {"inputTokens": 1_000},
        },
    )

    with patch.object(reporter, "_save_records_as_parquet") as mock_save:
        reporter.save_metering_data(document)

    records = {(r["service_api"], r["unit"]): r for r in mock_save.call_args.args[0]}

    priced = records[("bedrock/priced-model", "inputTokens")]
    assert priced["unit_cost"] == 1.0e-6
    assert priced["estimated_cost"] == pytest.approx(0.001)

    # 'requests' on a priced Bedrock entry: not chargeable, so a real $0.00.
    not_chargeable = records[("bedrock/priced-model", "requests")]
    assert not_chargeable["unit_cost"] == 0.0
    assert not_chargeable["estimated_cost"] == 0.0

    # No pricing entry at all: NULL, so `WHERE unit_cost IS NULL` finds the gap
    # and nobody reads the row as "this was free".
    unpriced = records[("bedrock/unpriced-model", "inputTokens")]
    assert unpriced["unit_cost"] is None
    assert unpriced["estimated_cost"] is None
    # The token count is still recorded — the metering data is not lost.
    assert unpriced["value"] == 1_000.0


# --------------------------------------------------------------------------- #
# 3. Forward coverage: every selectable model has an exact pricing entry       #
# --------------------------------------------------------------------------- #

_TEMPLATES = (
    _REPO_ROOT / "template.yaml",
    _REPO_ROOT / "patterns" / "unified" / "template.yaml",
    # Read so the three embedding models in _COVERAGE_EXEMPT are actually
    # ENCOUNTERED and then exempted. While this template went unread the
    # exemption filtered nothing and its staleness guard could not mean anything
    # (see test_coverage_exempt_has_no_stale_entries).
    _REPO_ROOT / "nested" / "bedrockkb" / "template.yaml",
)

# A Bedrock model ID: an optional cross-region/geo prefix, a provider, then the
# model name (which may carry ':0', ':1m', ':flex', ':priority' suffixes).
_MODEL_ID_RE = re.compile(
    r"^(?:(?:us|eu|us-gov|global)\.)?"
    r"(?:anthropic|amazon|openai|xai|meta|qwen|google|nvidia|deepseek|mistral"
    r"|cohere|ai21|writer|twelvelabs)"
    r"\.[A-Za-z0-9][A-Za-z0-9.:+_-]*$"
)

# Model IDs excluded from the coverage requirement, each with the reason. This is
# deliberately an ALLOWLIST rather than a loosened rule: adding a model without a
# price has to be a visible, justified edit here.
_COVERAGE_EXEMPT = {
    # Embedding models (nested/bedrockkb/template.yaml). BedrockClient's
    # generate_embedding() returns a bare vector and emits no metering entry, and
    # Knowledge Base ingestion is billed by the KB service outside this pipeline,
    # so no 'bedrock/<embedding-model>' metering key is ever produced and there is
    # nothing for a pricing row to price. That template IS read by the
    # enumeration below (it was not until PR #952, which made this exemption a
    # no-op), so these three are genuinely encountered and genuinely exempted —
    # test_coverage_exempt_has_no_stale_entries asserts exactly that.
    "amazon.titan-embed-text-v2:0": "embedding model — never metered",
    "cohere.embed-english-v3": "embedding model — never metered",
    "cohere.embed-multilingual-v3": "embedding model — never metered",
}

# Entries that legitimately carry only inputTokens/outputTokens, with the reason
# each cache unit is absent. Every one of these model IDs is absent from
# ``bedrock.client.CACHEPOINT_SUPPORTED_MODELS``, so the client strips cache
# markers before the call and cacheReadInputTokens is always 0 — the rate is
# unreachable, not merely unpublished. None of them caches implicitly either
# (the implicit cachers are the OpenAI gpt-5.4/5.5/6-astra models, and none of
# those is selectable here). If any of these is later added to
# CACHEPOINT_SUPPORTED_MODELS, it must get real cache rates and come off this
# list — which is what this test will then demand.
_NO_CACHE_UNITS_EXPECTED = {
    "us.amazon.nova-premier-v1:0": "not in CACHEPOINT_SUPPORTED_MODELS",
    "amazon.nova-lite-v1:0": "bare GovCloud ID; GovCloud caching unverified",
    "amazon.nova-pro-v1:0": "bare GovCloud ID; GovCloud caching unverified",
    "us.anthropic.claude-3-haiku-20240307-v1:0": ("not in CACHEPOINT_SUPPORTED_MODELS"),
    "eu.anthropic.claude-3-haiku-20240307-v1:0": ("not in CACHEPOINT_SUPPORTED_MODELS"),
    "us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0": (
        "verified live: cachePoint does not reduce input tokens through this "
        "GovCloud inference profile"
    ),
    "qwen.qwen3-vl-235b-a22b": "not in CACHEPOINT_SUPPORTED_MODELS",
    # The four below were invisible until PR #952 widened the enumeration to read
    # configuration-schema ``enum:`` blocks; all four are absent from
    # CACHEPOINT_SUPPORTED_MODELS, so like the entries above their cache rate is
    # unreachable rather than merely unrecorded.
    "us.meta.llama4-maverick-17b-instruct-v1:0": "not in CACHEPOINT_SUPPORTED_MODELS",
    "us.meta.llama4-scout-17b-instruct-v1:0": "not in CACHEPOINT_SUPPORTED_MODELS",
    "google.gemma-3-27b-it": "not in CACHEPOINT_SUPPORTED_MODELS",
    "nvidia.nemotron-nano-12b-v2": "not in CACHEPOINT_SUPPORTED_MODELS",
}


# Both keys that enumerate a closed set of choices in these templates. CFN
# parameter dropdowns use ``AllowedValues:``; the configuration-schema dropdowns
# the UI renders live under ``Metadata`` and are JSON Schema, so they use
# ``enum:``. Reading only the first is why this guard used to see ZERO model IDs
# from patterns/unified/template.yaml, hiding 56 selectable IDs including
# global.amazon.nova-pro-v1:0, which was wholly unpriced.
_ENUM_KEYS = ("AllowedValues", "enum")


def _enumerated_values(path: Path) -> set:
    """Every value listed under an ``AllowedValues:`` or ``enum:`` key.

    Parsed as text on purpose rather than with yaml.safe_load: these templates
    carry CFN intrinsics (``!If``, ``!Ref``, ``!Sub``) that safe_load rejects
    outright, and the ``enum:`` blocks in patterns/unified/template.yaml embed
    them mid-list.

    Both YAML sequence forms appear in these files and both are handled:
    the block form (``enum:`` then ``- "value"`` lines) and the inline flow form
    (``enum: ["a", "b"]``). Values that are CFN intrinsics rather than literals
    are returned as-is; callers filter by _MODEL_ID_RE, which no intrinsic
    matches.
    """
    keys = "|".join(_ENUM_KEYS)
    header_re = re.compile(rf"^(\s*)(?:{keys}):\s*(.*?)\s*$")
    values = set()
    lines = path.read_text().splitlines()
    i = 0
    while i < len(lines):
        header = header_re.match(lines[i])
        if not header:
            i += 1
            continue
        indent, inline = len(header.group(1)), header.group(2)
        i += 1
        if inline:
            # Inline flow form: enum: ["none", "minimal", ...]
            for item in inline.strip("[]").split(","):
                item = item.strip().strip("'\"")
                if item:
                    values.add(item)
            continue
        while i < len(lines):
            item = re.match(r"^(\s*)-\s*(.+?)\s*$", lines[i])
            if not item or len(item.group(1)) <= indent:
                break
            values.add(item.group(2).strip().strip("'\""))
            i += 1
    return values


def _all_model_ids() -> set:
    """Every Bedrock model ID the repository can select, exemptions included.

    Two sources, because either one alone misses real models: the template
    enumerations (what the deploy-time and configuration-schema dropdowns offer)
    and the ``model:`` keys in ``config_library/`` (what the shipped presets pin,
    which is how us.anthropic.claude-3-5-sonnet-20240620-v1:0 reached production
    unpriced).

    Kept separate from ``_selectable_model_ids`` so the _COVERAGE_EXEMPT
    staleness test can ask whether an exempt ID is still enumerated at all.
    """
    ids = set()
    for template in _TEMPLATES:
        ids |= {v for v in _enumerated_values(template) if _MODEL_ID_RE.match(v)}

    for path in sorted((_REPO_ROOT / "config_library").rglob("*.y*ml")):
        for line in path.read_text(errors="replace").splitlines():
            match = re.match(r"^\s*model\s*:\s*[\"']?([^\"'#\s]+)", line)
            # Inference-profile and custom-model-deployment ARNs are skipped: they
            # are account-specific, so a shared pricing table cannot name them.
            if match and _MODEL_ID_RE.match(match.group(1)):
                ids.add(match.group(1))

    return ids


def _selectable_model_ids() -> set:
    """``_all_model_ids()`` minus the documented exemptions."""
    return _all_model_ids() - set(_COVERAGE_EXEMPT)


@pytest.mark.unit
def test_selectable_model_enumeration_is_not_vacuous():
    """Guard the guard: a regex or path drift that finds nothing must fail loudly."""
    ids = _selectable_model_ids()
    assert len(ids) >= 80, f"only found {len(ids)} selectable model IDs: {sorted(ids)}"
    # Spot-check one ID from each source so a broken source is not masked by the
    # others still working. The three are deliberately distinct SHAPES of source:
    # a CFN parameter AllowedValues list, a configuration-schema ``enum:`` block
    # under Metadata (which this guard read none of until PR #952), and a preset.
    assert "us.anthropic.claude-sonnet-4-5-20250929-v1:0" in ids  # AllowedValues
    assert "global.amazon.nova-pro-v1:0" in ids  # Metadata schema enum:
    assert "us.anthropic.claude-3-5-sonnet-20240620-v1:0" in ids  # config_library


@pytest.mark.unit
def test_coverage_exempt_has_no_stale_entries():
    """Every exemption must still exempt something.

    _COVERAGE_EXEMPT was a no-op: it named three embedding models from
    nested/bedrockkb/template.yaml, which _TEMPLATES did not read, so subtracting
    it removed nothing and an ID could sit here forever without meaning anything.
    Requiring each entry to appear in the raw enumeration makes a stale exemption
    fail instead of lingering — and makes a real one demonstrably load-bearing.
    """
    enumerated = _all_model_ids()
    stale = sorted(set(_COVERAGE_EXEMPT) - enumerated)
    assert not stale, (
        "_COVERAGE_EXEMPT entries that no longer match any enumerated model ID "
        f"(so they exempt nothing): {stale}. Remove them, or fix _TEMPLATES / "
        "_MODEL_ID_RE if the ID should still be found."
    )


@pytest.mark.unit
def test_every_selectable_model_has_an_exact_pricing_entry(shipped_pricing_raw):
    """No selectable model may rely on a fuzzy match to get a price.

    This is the forward-coverage gate. Before issue #926 a model added without a
    pricing row still reported a (wrong) cost, because the substring fallback
    borrowed a related model's row; now it reports NULL. Either way nobody
    noticed, so the check has to be here rather than in the reviewer's head.
    """
    priced = {entry["name"] for entry in shipped_pricing_raw["pricing"]}
    missing = sorted(
        model_id
        for model_id in _selectable_model_ids()
        if f"bedrock/{model_id}" not in priced
    )
    assert not missing, (
        "selectable Bedrock models with no exact entry in "
        f"config_library/pricing.yaml: {missing}. Add a pricing entry (do NOT "
        "rely on a fuzzy match — there isn't one any more), or add the ID to "
        "_COVERAGE_EXEMPT with a reason."
    )


@pytest.mark.unit
def test_every_selectable_model_prices_all_four_token_units(shipped_pricing_raw):
    """All four token units, or an explicit entry in _NO_CACHE_UNITS_EXPECTED.

    A missing cache unit is no longer a 10x overcharge, but it is still a $0.00
    under-report if the model really does cache — so the omission has to be a
    stated decision, not an oversight.
    """
    units_by_name = {
        entry["name"]: {u["name"] for u in entry.get("units") or []}
        for entry in shipped_pricing_raw["pricing"]
    }

    incomplete = {}
    for model_id in sorted(_selectable_model_ids()):
        units = units_by_name.get(f"bedrock/{model_id}")
        if units is None:
            continue  # reported by the exact-entry test above
        absent = [u for u in TOKEN_UNITS if u not in units]
        if not absent:
            continue
        if model_id in _NO_CACHE_UNITS_EXPECTED:
            # Documented omission — but only the cache units may be absent.
            assert set(absent) <= {"cacheReadInputTokens", "cacheWriteInputTokens"}, (
                f"{model_id} is missing non-cache units {absent}, which "
                f"_NO_CACHE_UNITS_EXPECTED does not excuse"
            )
            continue
        incomplete[model_id] = absent

    assert not incomplete, (
        f"selectable Bedrock models missing token units: {incomplete}. Add the "
        "rates to config_library/pricing.yaml, or add the model to "
        "_NO_CACHE_UNITS_EXPECTED with the reason its cache rate is unreachable."
    )


@pytest.mark.unit
def test_no_cache_units_allowlist_has_no_stale_entries(shipped_pricing_raw):
    """An allowlist that outlives its reason quietly stops protecting anything."""
    units_by_name = {
        entry["name"]: {u["name"] for u in entry.get("units") or []}
        for entry in shipped_pricing_raw["pricing"]
    }
    selectable = _selectable_model_ids()
    stale = sorted(
        model_id
        for model_id in _NO_CACHE_UNITS_EXPECTED
        if model_id not in selectable
        or "cacheReadInputTokens" in units_by_name.get(f"bedrock/{model_id}", set())
    )
    assert not stale, (
        f"_NO_CACHE_UNITS_EXPECTED entries that are no longer needed (model is "
        f"not selectable, or now has cache rates): {stale}"
    )


@pytest.mark.unit
def test_no_cache_units_allowlist_matches_cachepoint_support():
    """The condition the allowlist's own comment claims, actually asserted.

    _NO_CACHE_UNITS_EXPECTED says every entry is absent from
    ``CACHEPOINT_SUPPORTED_MODELS`` and that "if any of these is later added to
    CACHEPOINT_SUPPORTED_MODELS, it must get real cache rates and come off this
    list — which is what this test will then demand". Nothing demanded it: the
    stale-entry test above looks only at selectability and at the pricing rows, so
    adding a listed model to CACHEPOINT_SUPPORTED_MODELS left the whole suite
    green while the model began emitting cache tokens with no rate to price them —
    a silent $0.00 under-report, the mirror image of the overcharge #926 fixed.

    Membership in CACHEPOINT_SUPPORTED_MODELS is therefore the rule, with one
    escape hatch: an entry whose reason begins with "verified live:" documents a
    MEASURED observation that the profile does not actually cache despite being
    listed, which no static check can derive. Anything else must be absent.
    """
    from idp_common.bedrock.client import CACHEPOINT_SUPPORTED_MODELS

    # Sanity-check the import target before drawing conclusions from it: an empty
    # or renamed list would make every assertion below pass vacuously.
    assert len(CACHEPOINT_SUPPORTED_MODELS) > 20, (
        "CACHEPOINT_SUPPORTED_MODELS looks wrong "
        f"({len(CACHEPOINT_SUPPORTED_MODELS)} entries); this test would pass "
        "vacuously"
    )

    contradictory = sorted(
        model_id
        for model_id, reason in _NO_CACHE_UNITS_EXPECTED.items()
        if model_id in CACHEPOINT_SUPPORTED_MODELS
        and not reason.startswith("verified live:")
    )
    assert not contradictory, (
        "these models are in bedrock.client.CACHEPOINT_SUPPORTED_MODELS, so the "
        "client will send cachePoint markers and they WILL emit "
        "cacheReadInputTokens/cacheWriteInputTokens — but "
        f"_NO_CACHE_UNITS_EXPECTED excuses them from having cache rates: "
        f"{contradictory}. Add the real cache rates to "
        "config_library/pricing.yaml and remove the entry, or — if you have "
        "MEASURED that this profile does not cache in practice — restate the "
        "reason starting with 'verified live:'."
    )


# --------------------------------------------------------------------------- #
# 4. Non-Bedrock key shapes: Lambda hooks, the BDA skip counter, Lambda cost   #
# --------------------------------------------------------------------------- #
#
# These are the pricing keys that are NOT `bedrock/<model-id>`, and every one of
# them was broken or unpriced before PR #952. Nothing covered them, which is why
# removing the substring fallback silently turned two shipped per-page prices into
# NULL. All of the assertions below run against the REAL config_library/pricing.yaml
# (the ``shipped_reporter`` fixture), not a fixture table, because the failure being
# guarded against is a mismatch between the shipped data and the shipped code.


@pytest.mark.unit
@pytest.mark.parametrize(
    ("hook_reference", "expected"),
    [
        # The form every example in docs/lambda-hook-inference.md uses.
        (
            "arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-mistral-ocr-hook",
            0.004,
        ),
        (
            "arn:aws:lambda:us-west-2:210987654321:function:GENAIIDP-cohere-parse-hook",
            0.0015,
        ),
        # An alias/version-qualified ARN: the suffix must not become part of the
        # pricing key, or the row stops matching the moment someone pins a stage.
        (
            "arn:aws:lambda:us-east-1:123456789012:function:"
            "GENAIIDP-mistral-ocr-hook:PROD",
            0.004,
        ),
        # A GovCloud ARN — a different partition must not change the key.
        (
            "arn:aws-us-gov:lambda:us-gov-west-1:123456789012:function:"
            "GENAIIDP-cohere-parse-hook",
            0.0015,
        ),
        # A bare function name, which the config also accepts.
        ("GENAIIDP-mistral-ocr-hook", 0.004),
        ("GENAIIDP-cohere-parse-hook", 0.0015),
    ],
)
def test_shipped_lambda_hook_rows_resolve_to_their_per_page_price(
    shipped_reporter, hook_reference, expected
):
    """Both shipped Lambda-hook rows must price, from every configurable ARN form.

    This is the regression PR #952 introduced and this test locks shut. The hook
    metering key is built by ``bedrock.client`` as
    ``{context}/lambda_hook/{name}``, and both shipped pricing rows used to be
    keyed on a bare ``GENAIIDP-<name>`` with no '/' at all — the only two such keys
    in the whole table. Exact match could therefore never succeed and the old
    substring fallback was the sole reason they priced. Removing the fallback took
    Mistral OCR from $0.004/page to NULL and Cohere Parse from $0.0015/page to
    NULL, unnoticed because nothing tested this key shape.

    The full path is exercised deliberately — ARN normalisation, key construction,
    the context split SaveReportingData performs, then the lookup — rather than
    asserting on a hand-written key, because the bug lived in the JOIN between
    those steps and each one in isolation looked correct.
    """
    from idp_common.bedrock.client import lambda_hook_metering_name

    metering_key = (
        f"classification/lambda_hook/{lambda_hook_metering_name(hook_reference)}"
    )
    # SaveReportingData splits the context off the metering key on the FIRST '/'.
    _context, service_api = metering_key.split("/", 1)
    assert service_api.startswith("lambda_hook/")

    assert shipped_reporter._get_unit_cost(service_api, "pages") == expected

    # The hook also meters a 'requests' count, which these rows deliberately do
    # not price. That must read as 0.0 (metered, not chargeable) and not as NULL,
    # which would mean "we have no idea what this costs".
    assert shipped_reporter._get_unit_cost(service_api, "requests") == 0.0


@pytest.mark.unit
def test_no_shipped_pricing_key_lacks_a_slash(shipped_pricing_raw):
    """Every shipped key must be fully qualified, i.e. carry a '/'.

    A key with no '/' cannot be reached by the suffix walk from any metering key
    that has a context prefix, because the walk only ever strips leading
    '/'-delimited components — it can never strip a ':' or invent a delimiter. The
    two Lambda-hook rows were exactly this shape and were dead on exact-match
    alone. Asserting the invariant here means the next such row fails at review
    time rather than becoming a silent NULL in the cost table.
    """
    unqualified = sorted(
        entry["name"]
        for entry in shipped_pricing_raw["pricing"]
        if "/" not in entry["name"]
    )
    assert not unqualified, (
        f"pricing keys with no '/' cannot be resolved from a metering key: "
        f"{unqualified}. Key them as '<service>/<name>' — see "
        "reporting.save_reporting_data._get_unit_cost."
    )


@pytest.mark.unit
def test_bda_skip_counter_is_priced_at_zero(shipped_reporter):
    """The BDA skip counter must price as $0.00, not as NULL.

    ``patterns/unified/src/bda_processresults_function/index.py`` emits
    ``BDAProject/bda/documents-skip`` = 1 when it skips BDA processing. Nothing is
    invoked, so $0 is the truthful price — but a MISSING row now records NULL and
    trips the 'reported spend is incomplete' warning on every skipped document.
    Stating the zero in pricing.yaml keeps the intent in the data instead of
    special-casing counters in the warning code.
    """
    assert shipped_reporter._get_unit_cost("bda/documents-skip", "documents") == 0.0


@pytest.mark.unit
def test_lambda_metering_unit_name_matches_the_shipped_pricing_unit(shipped_reporter):
    """The unit ``lambda_metering`` emits must be the unit ``pricing.yaml`` prices.

    ``utils.lambda_metering`` emitted ``invocations`` while the ``lambda/requests``
    row prices ``requests``: the KEY matched, so no 'unpriced' warning ever fired,
    but the UNIT did not, so every Lambda invocation costed $0.00. Asserting the
    emitted name against the shipped price closes the naming gap rather than
    restoring a log line about it.
    """
    import time
    from types import SimpleNamespace

    from idp_common.utils.lambda_metering import calculate_lambda_metering

    metering = calculate_lambda_metering(
        "OCR",
        SimpleNamespace(memory_limit_in_mb=1024),
        start_time=time.time() - 2.0,
    )
    # calculate_lambda_metering swallows exceptions and returns {} — which would
    # make the loop below iterate zero times and pass vacuously.
    assert set(metering) == {"OCR/lambda/requests", "OCR/lambda/duration"}, metering

    for metering_key, units in metering.items():
        _context, service_api = metering_key.split("/", 1)
        for unit_name in units:
            cost = shipped_reporter._get_unit_cost(service_api, unit_name)
            assert cost is not None, (
                f"lambda_metering emits {metering_key!r} unit {unit_name!r}, which "
                "has no pricing entry at all — it will record NULL"
            )
            assert cost > 0, (
                f"lambda_metering emits {metering_key!r} unit {unit_name!r}, but "
                f"config_library/pricing.yaml prices that unit at {cost}. The unit "
                "name almost certainly does not match the shipped one, so every "
                "Lambda invocation costs $0.00."
            )
