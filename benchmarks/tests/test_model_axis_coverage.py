# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every model this matrix names must be a model the product can actually run.

A benchmark axis is the only thing that turns a model from *selectable* into
*measured* — ``config_matrix.yaml`` says so itself above the ``extraction_model``
sweep, and `docs/benchmarking/index.md` keeps the ✅/❌ table that the guide's model
section is written from. So a model id in an axis is a promise about two different
files, and nothing checked either:

* **The product must price it.** A cell pinned to a model with no
  ``config_library/pricing.yaml`` entry still runs — the stack accepts the id and
  Bedrock serves it — and every cost figure it produces is silently **zero**,
  because ``_get_unit_cost`` resolves nothing. A suite whose headline is dollars
  then publishes a model that looks free. This is the same failure
  ``scripts/tests/test_model_surface_consistency.py`` guards for the product's own
  surfaces; the benchmark matrix is a seventh surface it does not read.
* **Auto-sizing must recognise it.** A model matched by no
  ``config_library/model_config_limits.yaml`` pattern falls back to a conservative
  window, so shard budgets are derived from the wrong number and a cost or
  completeness delta attributed to the model is partly a sharding artefact.

Both are checked here over **every** model id in the file, derived from the axes
rather than listed, so a model cannot be added to an axis without being covered.

What this file deliberately does **not** assert is the reverse direction — that
every model the product offers appears in an axis. That would be a useful gate and
it cannot be written honestly as a pass/fail: most of the ~60 priced ids are region
variants, dated foundation ids and deliberately-unswept models, so the universe
would need a per-member exemption list larger than the thing it protects, and the
real answer for each is a date and a finding rather than a boolean. That direction
is tracked where a date can live: the "Which models are actually measured" table in
`docs/benchmarking/index.md`, whose ❌ rows are the honest residual.
"""

from __future__ import annotations

import os
import re

import pytest

yaml = pytest.importorskip("yaml")

BENCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(BENCH)
CFG_MATRIX = os.path.join(BENCH, "matrices", "config_matrix.yaml")
DOC_MATRIX = os.path.join(BENCH, "matrices", "doc_matrix.yaml")
PRICING = os.path.join(REPO, "config_library", "pricing.yaml")
LIMITS = os.path.join(REPO, "config_library", "model_config_limits.yaml")

#: Config keys whose value is a Bedrock model id. Named rather than pattern-matched
#: on ``*.model`` because ``ocr.model_id`` does not end in ``.model`` and
#: ``extraction.model`` does — a suffix rule would miss one of them.
_MODEL_KEYS = frozenset(
    {
        "extraction.model",
        "classification.model",
        "assessment.model",
        "summarization.model",
        "ocr.model_id",
        "extraction.confidence.model",
    }
)


@pytest.fixture(scope="module")
def matrix():
    with open(CFG_MATRIX) as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def priced_models():
    with open(PRICING) as fh:
        data = yaml.safe_load(fh)
    prefix = "bedrock/"
    return {
        entry["name"][len(prefix) :]
        for entry in data["pricing"]
        if entry.get("name", "").startswith(prefix)
    }


@pytest.fixture(scope="module")
def limit_patterns():
    with open(LIMITS) as fh:
        data = yaml.safe_load(fh)
    return [entry["pattern"] for entry in data["model_limits"]]


def _axis_models(matrix):
    """Every (axis, value, model_id) the matrix pins, derived from the axes.

    Returns a sorted list so the parametrised ids are stable, and asserts
    non-emptiness at the call site — a traversal that silently collects nothing
    would make every test below vacuously green.
    """
    found = []
    for axis, values in (matrix.get("axes") or {}).items():
        for value_name, overrides in (values or {}).items():
            for key, val in (overrides or {}).items():
                if key in _MODEL_KEYS and isinstance(val, str) and val:
                    found.append((axis, value_name, val))
    return sorted(set(found))


@pytest.fixture(scope="module")
def axis_models(matrix):
    found = _axis_models(matrix)
    assert found, (
        "no model-pinned axis values were found in config_matrix.yaml. Either the "
        "axes were restructured or _MODEL_KEYS no longer names the keys they use — "
        "either way every check in this file just stopped looking at anything."
    )
    return found


class TestEveryNamedModelIsReal:
    def test_the_sweep_is_not_vacuous(self, axis_models):
        """Pin the size of what is being swept. A traversal that quietly narrowed
        (a renamed key, a restructured axis) would leave the two checks below
        passing over fewer models, which is the shape that hides."""
        assert len(axis_models) >= 12

    def test_every_named_model_is_priced(self, axis_models, priced_models):
        """An unpriced model reports cost ZERO rather than failing, so a cost suite
        would publish it as free."""
        missing = [
            f"{axis}={value} -> {model}"
            for axis, value, model in axis_models
            if model not in priced_models
        ]
        assert not missing, (
            "these benchmark axis values name a model with no "
            "config_library/pricing.yaml entry, so every cost they report resolves "
            "to zero:\n  " + "\n  ".join(missing)
        )

    def test_every_named_model_matches_a_limits_pattern(
        self, axis_models, limit_patterns
    ):
        """An unmatched model auto-sizes off a fallback window, so shard budgets —
        and therefore cost and completeness — are partly an artefact."""
        unmatched = [
            f"{axis}={value} -> {model}"
            for axis, value, model in axis_models
            if not any(re.search(p, model, re.I) for p in limit_patterns)
        ]
        assert not unmatched, (
            "these benchmark axis values name a model matched by no "
            "config_library/model_config_limits.yaml pattern, so auto-sizing uses a "
            "fallback window:\n  " + "\n  ".join(unmatched)
        )


class TestOpus55IsMeasured:
    """The Opus 5.5 arms specifically — the model this file was added with.

    Each of these is a thing that can be half-done: an axis value nothing sweeps, a
    sweep entry naming no axis value, or a head-to-head with one arm.
    """

    def test_the_axis_value_exists_and_names_opus_5_5(self, matrix):
        assert matrix["axes"]["extraction_model"]["opus55"] == {
            "extraction.model": "us.anthropic.claude-opus-5-5"
        }

    def test_it_is_in_the_sweep_that_feeds_the_published_guide(self, matrix):
        """The `extraction_model` sweep is what `full` runs and what the
        Configuration Guidance paper's model section is computed from. An axis value
        outside it is reachable only by hand and appears in no guidance."""
        assert "opus55" in matrix["sweeps"]["extraction_model"]

    def test_opus_5_is_still_swept_alongside_it(self, matrix):
        """The cheaper-than-Opus-5 claim needs its denominator. Dropping opus5 when
        opus55 arrived would leave the sweep unable to state the comparison at all."""
        assert "opus5" in matrix["sweeps"]["extraction_model"]

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("opus5-simple-sep", "opus55-simple-sep"),
            ("opus5-adv-sep", "opus55-adv-sep"),
        ],
    )
    def test_the_head_to_head_pairs_differ_only_in_the_model(self, matrix, a, b):
        """A matched pair is the whole method: if the two arms differ in anything
        else, the delta is not the model. This is the ``boundaryctl`` failure mode in
        a different place — a pair that looks like an A/B and is not."""
        cells = {c["id"]: c for c in matrix["model_premium_cells"]}
        assert a in cells and b in cells
        left = {
            k: v for k, v in cells[a].items() if k not in ("id", "extraction_model")
        }
        right = {
            k: v for k, v in cells[b].items() if k not in ("id", "extraction_model")
        }
        assert left == right, (
            f"{a} and {b} are meant to be a matched pair but differ outside "
            f"extraction_model: {left} vs {right}"
        )
        assert cells[a]["extraction_model"] == "opus5"
        assert cells[b]["extraction_model"] == "opus55"

    def test_the_suite_runs_both_arms_of_both_modes(self, matrix):
        cells = set(matrix["suites"]["opus55value"]["cells"])
        assert cells == {
            "opus5-simple-sep",
            "opus55-simple-sep",
            "opus5-adv-sep",
            "opus55-adv-sep",
        }

    def test_the_suite_is_repeated_because_its_headline_is_cost(self, matrix):
        """Agentic cost is non-deterministic (turn-count spreads ~4x observed), so a
        single draw per cell cannot resolve a cost DIFFERENCE — which is the only
        thing this suite exists to report."""
        assert matrix["suites"]["opus55value"].get("repeats", 1) >= 5

    def test_its_documents_are_ones_both_arms_can_actually_take(self):
        """`opus_docs` is a subset of `astra_docs` on purpose. Opus 5 and Opus 5.5
        share one window and one sizing budget, so on the two large astra_docs
        entries both simple-mode arms would be refused — an A/B whose arms fail
        identically measures nothing and is still billed."""
        with open(DOC_MATRIX) as fh:
            docm = yaml.safe_load(fh)
        opus_docs = set(docm["groups"]["opus_docs"])
        astra_docs = set(docm["groups"]["astra_docs"])
        assert opus_docs, "opus_docs is empty, so the suite would run nothing"
        assert opus_docs < astra_docs
        assert not opus_docs & {"large_narrow", "dense_250", "scale_3200"}
