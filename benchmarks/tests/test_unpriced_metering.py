# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A metering entry that cannot be priced must not be recorded as free (#1146).

What was wrong
--------------
``lib.price_metering`` returned ``(total, by_key)`` and silently ``continue``d past
three things: a metering key no ``pricing.yaml`` entry matched, an entry whose value
was not a map, and a count that was not a number. Contributing nothing to a sum is the
same arithmetic as contributing zero, so a model added to a benchmark suite without a
pricing entry made every affected row price **strictly below truth while still
reporting a plausible non-zero total** — the other phases of the document price
normally.

This is the #1079 defect one layer along: there a failed *read* was recorded as a
value, here a successful read whose contents could not be priced was. It is harder to
notice, and the reason is the thing to keep in mind while reading these tests: **the
absence of a zero cost does not rule it out.** ``cost`` is non-zero, ``cost_by_phase``
is populated, and nothing about the row reads as partial.

What these tests pin
--------------------
1. ``lib.Priced`` will not hand over a total it knows to be short: ``total`` raises
   :class:`lib.Unpriced` unless every entry priced, ``partial_total`` is the same
   number under a name that says what it is, and ``__bool__`` raises because
   ``if priced:`` reads as "did it all price" while always being true.
2. All three drops are reported, and each names the metering key.
3. ⚠️ **The boundary in the other direction**, which is the test to break before
   changing the matching rule: a unit a matched pricing entry does not list is
   ``$0.00`` *by design*, not a missing price. Every Bedrock call meters
   ``totalTokens`` and ``requests``, neither of which Bedrock charges for, so
   reporting the unit axis would mark every Bedrock entry in every row unpriceable
   and null every cost in the corpus. Production settled this rule first
   (``idp_common/reporting/README.md``) and the two implementations are required to
   agree.
4. ``analyze.score_doc`` withholds ``cost``, ``cost_by_phase`` and ``cost_by_key``
   from a row it could not fully price and names the entries in ``cost_unpriced`` —
   while keeping ``tokens``, which were read rather than priced.
5. A withheld cost is null rather than zero, so it drops out of a cell mean instead
   of dragging it down, and the exclusion is counted (``n_cost_unpriced``), reaches
   the CSV, and is printed by ``compare_cells``.
6. The other call sites drop and name the observation rather than pooling a short
   one: ``real_corpus_ab._cost``, ``run_classification_bench.classification_cost``
   and ``score_run``, and — reached through the first, so invisible to a grep for
   ``price_metering`` — ``per_class_ab``, which is covered by a class guard over
   every ``_cost`` call in the harness rather than by naming that one line.
7. Every model id the committed config matrix can select is priced, checked before a
   grid is paid for rather than after.

On the fixtures
---------------
The pricing table under test is the real ``config_library/pricing.yaml``, and the
metering maps go through ``lib.read_metering`` in DynamoDB attribute-value form, which
is the shape the harness actually consumes. The unpriced model id is **derived at run
time** and asserted absent from the real file at every suffix the matcher tries, so
this suite fails rather than going quiet if that id is ever given a price.
"""

from __future__ import annotations

import ast
import io
import json
import re
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import yaml
from harness_import import harness_module

lib = harness_module("lib")
analyze = harness_module("analyze")
aggregate = harness_module("aggregate")
real_corpus_ab = harness_module("real_corpus_ab")
run_classification_bench = harness_module("run_classification_bench")

REPO = Path(lib.REPO)
PRICING_YAML = REPO / "config_library" / "pricing.yaml"
CONFIG_MATRIX = REPO / "benchmarks" / "matrices" / "config_matrix.yaml"

# A pricing key the shipped table really holds, used wherever a test needs an entry
# that prices. Asserted present by `test_the_authority_is_the_shipped_pricing_file`,
# so a rename shows up there rather than as a puzzling failure somewhere else.
REAL_MODEL = "bedrock/us.anthropic.claude-sonnet-5"
REAL_OCR = "textract/analyze_document-Tables"


# --------------------------------------------------------------------------- #
# The authority: the shipped pricing file, and an id genuinely absent from it
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_the_authority_is_the_shipped_pricing_file():
    """Everything below is only meaningful against a real, populated table."""
    assert Path(lib.PRICING_PATH) == PRICING_YAML
    assert lib.PRICING, "pricing table is empty — the rest of this file proves nothing"
    for key in (REAL_MODEL, REAL_OCR):
        assert key in lib.PRICING, key
    assert "inputTokens" in lib.PRICING[REAL_MODEL]
    assert "pages" in lib.PRICING[REAL_OCR]


@pytest.fixture(scope="module")
def unpriced_model() -> str:
    """A model id with NO pricing entry, derived from the shipped file.

    The matcher walks ``/``-delimited suffixes, so "absent" has to hold for every
    suffix of the metering key, not just the whole thing. This fixture asserts that
    against the real table rather than assuming it — if the id is ever given a price,
    this fails and names itself instead of quietly turning every test below into one
    about a priced model.
    """
    model = "us.anthropic.claude-sonnet-5-unreleased"
    meter_key = f"Extraction/bedrock/{model}"
    parts = meter_key.split("/")
    for start in range(len(parts)):
        candidate = "/".join(parts[start:])
        assert candidate not in lib.PRICING, (
            f"{candidate!r} is now priced in {PRICING_YAML.name}, so this fixture no "
            "longer supplies an unpriced model — pick another id"
        )
    return model


def _metering_item(metering: dict) -> dict:
    """A tracking row in DynamoDB attribute-value form, the shape the harness reads.

    Built through the same ``M``/``N``/``S`` encoding ``read_metering`` decodes, so
    these tests exercise the real consumption path rather than a hand-made dict that
    encodes a belief about what a metering entry looks like.
    """

    def av(value):
        if isinstance(value, dict):
            return {"M": {k: av(v) for k, v in value.items()}}
        if isinstance(value, bool):
            return {"BOOL": value}
        if isinstance(value, (int, float)):
            return {"N": str(value)}
        if isinstance(value, list):
            return {"L": [av(v) for v in value]}
        return {"S": str(value)}

    return {"Metering": av(metering)}


def _unbound_cost_calls(tree: ast.AST) -> list[int]:
    """Line numbers of ``*_cost(...)`` calls NOT bound by tuple unpacking.

    Used by the class guard below and by its own probes, so the two cannot drift —
    a guard tested through a re-implementation of itself proves nothing about the
    guard.
    """
    unpacked = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Tuple)
        and isinstance(node.value, ast.Call)
    }
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else ""
        )
        if name.endswith("_cost") and id(node) not in unpacked:
            out.append(node.lineno)
    return out


@pytest.fixture
def read_metering(monkeypatch):
    """``lib.read_metering`` over a fake table holding one row."""

    def install(metering: dict):
        item = _metering_item(metering)

        class _DDB:
            def get_item(self, **_kw):
                return {"Item": item}

        monkeypatch.setattr(lib, "ddb", lambda: _DDB())
        read = lib.read_metering("t", "r", "d")
        assert read.is_present, read.error
        return read.value

    return install


# --------------------------------------------------------------------------- #
# 1. The Priced contract — a short total is not available by accident
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestPricedWillNotHandOverAShortTotal:
    def test_a_complete_result_yields_its_total(self, read_metering):
        priced = lib.price_metering(read_metering({f"OCR/{REAL_OCR}": {"pages": 3}}))
        assert priced.complete
        assert priced.unpriced == ()
        assert priced.why == ""
        assert priced.total == pytest.approx(3 * lib.PRICING[REAL_OCR]["pages"])
        assert priced.total == priced.partial_total

    def test_an_incomplete_result_refuses_its_total(
        self, read_metering, unpriced_model
    ):
        priced = lib.price_metering(
            read_metering(
                {
                    f"OCR/{REAL_OCR}": {"pages": 3},
                    f"Extraction/bedrock/{unpriced_model}": {"inputTokens": 100000},
                }
            )
        )
        assert not priced.complete
        with pytest.raises(lib.Unpriced, match="BELOW truth"):
            _ = priced.total

    def test_the_partial_total_is_reachable_under_a_name_that_says_so(
        self, read_metering, unpriced_model
    ):
        """A caller may report it — but only after saying it is partial."""
        priced = lib.price_metering(
            read_metering(
                {
                    f"OCR/{REAL_OCR}": {"pages": 3},
                    f"Extraction/bedrock/{unpriced_model}": {"inputTokens": 100000},
                }
            )
        )
        assert priced.partial_total == pytest.approx(3 * lib.PRICING[REAL_OCR]["pages"])
        # ...and it is strictly below what the same map would cost if priced.
        assert priced.partial_total > 0

    def test_truth_testing_a_priced_result_is_an_error(self, read_metering):
        """``if priced:`` reads as "did it all price" and is always True."""
        for metering in ({}, {f"OCR/{REAL_OCR}": {"pages": 1}}):
            priced = lib.price_metering(read_metering(metering))
            with pytest.raises(TypeError, match="Test .complete"):
                bool(priced)
            with pytest.raises(TypeError):
                if priced:  # noqa: SIM103
                    pass

    def test_the_old_two_tuple_shape_is_gone(self, read_metering):
        """A caller written against ``total, by = price_metering(...)`` must break
        loudly rather than bind ``total`` to something else."""
        priced = lib.price_metering(read_metering({f"OCR/{REAL_OCR}": {"pages": 1}}))
        with pytest.raises(TypeError):
            _total, _by = priced  # pyright: ignore[reportGeneralTypeIssues]
        with pytest.raises(TypeError):
            _ = priced[0]  # pyright: ignore[reportIndexIssue]

    def test_the_per_key_breakdown_adds_up_to_the_total(self, read_metering):
        priced = lib.price_metering(
            read_metering(
                {
                    f"OCR/{REAL_OCR}": {"pages": 3},
                    f"Extraction/{REAL_MODEL}": {"inputTokens": 1000},
                    f"Assessment/{REAL_MODEL}": {"outputTokens": 500},
                }
            )
        )
        assert priced.complete
        assert sum(priced.by_key.values()) == pytest.approx(priced.total)
        assert sum(priced.by_meter_key.values()) == pytest.approx(priced.total)
        # by_key is per MODEL (the two Bedrock phases share one), by_meter_key per
        # metering key, which is what a phase breakdown needs.
        assert set(priced.by_key) == {REAL_OCR, REAL_MODEL}
        assert len(priced.by_meter_key) == 3

    def test_a_priced_key_that_costs_nothing_still_appears_per_meter_key(
        self, read_metering
    ):
        """Otherwise a phase whose entry cost 0.00 vanishes from the breakdown, which
        reads as a phase that did not run — and `cost_by_phase` carried it as 0.0
        before this change, so its absence would also make grids incomparable.

        ⚠️ The discriminating input is a key that resolves to a pricing entry and
        whose units that entry does **not** list. A ``pages: 0`` entry does not
        discriminate: the unit prices, to zero, so the accumulation records the key
        whether or not it was pre-seeded. Measured — the pre-seeding mutation
        survived the ``pages: 0`` form of this test.
        """
        no_chargeable_unit = {"totalTokens": 5000, "requests": 1}
        assert not set(no_chargeable_unit) & set(lib.PRICING[REAL_MODEL])
        priced = lib.price_metering(
            read_metering(
                {
                    f"Extraction/{REAL_MODEL}": no_chargeable_unit,
                    f"OCR/{REAL_OCR}": {"pages": 0},
                }
            )
        )
        assert priced.complete
        assert priced.total == 0.0
        assert priced.by_meter_key == {
            f"Extraction/{REAL_MODEL}": 0.0,
            f"OCR/{REAL_OCR}": 0.0,
        }

    def test_a_phase_whose_entry_cost_nothing_survives_into_score_doc(
        self, monkeypatch
    ):
        """The consumer's shape: `cost_by_phase` names the phase, carrying 0.0.

        This pins the output rather than the pre-seeding — `score_doc` defaults a
        missing `by_meter_key` entry to 0.0, so it survives that regression on its
        own. The test above is the one that discriminates it.
        """
        monkeypatch.setattr(
            lib, "doc_row", lambda *a, **k: {"ObjectStatus": "COMPLETED"}
        )
        monkeypatch.setattr(
            lib,
            "read_metering",
            lambda *a, **k: lib.Reading.present(
                {
                    f"Extraction/{REAL_MODEL}": {"totalTokens": 5000},
                    f"OCR/{REAL_OCR}": {"pages": 2},
                }
            ),
        )
        monkeypatch.setattr(lib, "read_sections", lambda *a, **k: lib.SectionRead([]))
        row = analyze.score_doc("b", "t", "r", "d", None)
        assert row["cost_by_phase"] == {
            "Extraction": 0.0,
            "OCR": round(2 * lib.PRICING[REAL_OCR]["pages"], 5),
        }


# --------------------------------------------------------------------------- #
# 2. All three drops are reported, and each names the entry
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestEveryDropIsReported:
    def test_a_model_with_no_pricing_entry(self, read_metering, unpriced_model):
        """The headline case: adding a model to a suite and not to pricing.yaml."""
        priced = lib.price_metering(
            read_metering({f"Extraction/bedrock/{unpriced_model}": {"inputTokens": 9}})
        )
        assert not priced.complete
        assert len(priced.unpriced) == 1
        assert unpriced_model in priced.unpriced[0]
        assert "no pricing.yaml entry" in priced.unpriced[0]
        assert unpriced_model in priced.why

    def test_the_total_it_would_have_reported_is_the_plausible_wrong_one(
        self, read_metering, unpriced_model
    ):
        """Why detection has to be structural: the number looks fine.

        The map below spends real money on an unpriced model and the partial total is
        neither zero nor obviously short — which is the whole reason the absence of a
        zero cost does not rule this defect out.
        """
        priced = lib.price_metering(
            read_metering(
                {
                    f"OCR/{REAL_OCR}": {"pages": 20},
                    f"Extraction/bedrock/{unpriced_model}": {
                        "inputTokens": 500000,
                        "outputTokens": 50000,
                    },
                }
            )
        )
        assert priced.partial_total > 0.0, (
            "a partial total of exactly zero would be noticeable; the hazard is that "
            "it is not"
        )
        assert not priced.complete

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("not-a-map", "a str, not a map"),
            # `ddb_to_py` decodes every DynamoDB `N` as a float, which is why the
            # shape has to come through the real reader rather than be asserted by
            # hand: a map written as a number arrives as one, not as an int.
            (12345, "a float, not a map"),
            ([{"inputTokens": 1}], "a list, not a map"),
        ],
        ids=["string", "number", "list"],
    )
    def test_an_entry_that_is_not_a_map(self, read_metering, value, expected):
        priced = lib.price_metering(read_metering({f"Extraction/{REAL_MODEL}": value}))
        assert not priced.complete
        assert expected in priced.unpriced[0]
        assert REAL_MODEL in priced.unpriced[0]

    @pytest.mark.parametrize(
        ("count", "described"),
        [("1000", "a str"), (True, "a bool"), ([1], "a list")],
        ids=["string", "bool", "list"],
    )
    def test_a_count_that_is_not_a_number(self, read_metering, count, described):
        """A ``bool`` is included deliberately: ``isinstance(True, int)`` is true, so
        a count written as a DynamoDB ``BOOL`` used to price as 1 or 0 — neither of
        which was metered."""
        priced = lib.price_metering(
            read_metering({f"Extraction/{REAL_MODEL}": {"inputTokens": count}})
        )
        assert not priced.complete
        assert described in priced.unpriced[0]
        assert "inputTokens" in priced.unpriced[0]
        # The metering key too: the reason has to say WHICH entry, or a reader of a
        # grid with several phases cannot act on it.
        assert f"Extraction/{REAL_MODEL}" in priced.unpriced[0]
        assert priced.partial_total == 0.0

    def test_several_unpriceable_entries_are_all_named(
        self, read_metering, unpriced_model
    ):
        priced = lib.price_metering(
            read_metering(
                {
                    f"Extraction/bedrock/{unpriced_model}": {"inputTokens": 1},
                    f"Assessment/bedrock/{unpriced_model}-b": {"inputTokens": 1},
                    f"Summarization/{REAL_MODEL}": "nope",
                }
            )
        )
        assert len(priced.unpriced) == 3
        assert priced.why.startswith("3 unpriceable metering entries:")

    def test_one_entry_is_described_in_the_singular(
        self, read_metering, unpriced_model
    ):
        priced = lib.price_metering(
            read_metering({f"Extraction/bedrock/{unpriced_model}": {"inputTokens": 1}})
        )
        assert priced.why.startswith("1 unpriceable metering entry:")


# --------------------------------------------------------------------------- #
# 3. The boundary in the other direction — the false positive that would be worse
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestAUnitAnEntryDoesNotListIsARealZero:
    """⚠️ Break these before relaxing or widening the matching rule.

    A unit absent from a pricing entry that EXISTS means the unit is not chargeable
    for that service, not that a price is missing. Production decided this first and
    documents it in ``idp_common/reporting/README.md``; the two implementations are
    required to agree, so reporting the unit axis here would also put the benchmark
    cost and the product's reported cost out of step (#926's docstring).
    """

    def test_no_pricing_entry_anywhere_lists_totaltokens(self):
        """Derived from the shipped file, because the next assertion depends on it."""
        raw = yaml.safe_load(PRICING_YAML.read_text())
        lists_it = [
            entry["name"]
            for entry in raw["pricing"]
            if any(u["name"] == "totalTokens" for u in entry.get("units") or [])
        ]
        assert lists_it == [], (
            "totalTokens is now priced somewhere, so it is no longer an example of a "
            "metered-but-not-chargeable unit"
        )

    def test_the_units_every_bedrock_call_meters_do_not_make_a_row_unpriceable(
        self, read_metering
    ):
        """``totalTokens`` and ``requests`` are metered on every Bedrock call.

        Reporting them would mark every Bedrock entry in every row unpriceable and
        null every cost in the corpus — a far worse failure than the one this change
        fixes, and in the same direction of being wrong while looking fine.
        """
        priced = lib.price_metering(
            read_metering(
                {
                    f"Extraction/{REAL_MODEL}": {
                        "inputTokens": 1000,
                        "outputTokens": 100,
                        "totalTokens": 1100,
                        "requests": 1,
                    }
                }
            )
        )
        assert priced.complete, priced.why
        assert priced.total == pytest.approx(
            1000 * lib.PRICING[REAL_MODEL]["inputTokens"]
            + 100 * lib.PRICING[REAL_MODEL]["outputTokens"]
        )

    def test_a_real_corpus_metering_map_prices_completely(self, read_metering):
        """The 14 pricing keys the committed artifacts use, in one map.

        If any of them stops pricing, every historical grid becomes unreadable by
        current code — so this is the regression that matters most about the change.
        """
        keys = set()
        for path in (REPO / "benchmarks" / "results").rglob("*.json"):
            doc = json.loads(path.read_text())
            if not isinstance(doc, dict):  # a few artifacts are a bare list
                continue
            for row in doc.get("rows") or []:
                keys.update(row.get("cost_by_key") or {})
        keys = sorted(keys)
        assert len(keys) >= 14, f"only found {len(keys)} keys in the artifacts"
        metering = {
            f"Extraction/{key}": {
                unit: 1 for unit in (*lib.PRICING.get(key, {}), "totalTokens")
            }
            for key in keys
        }
        priced = lib.price_metering(read_metering(metering))
        assert priced.complete, priced.why


# --------------------------------------------------------------------------- #
# 4. score_doc withholds the cost and names what it could not price
# --------------------------------------------------------------------------- #
@pytest.fixture
def scored_doc(monkeypatch):
    """``analyze.score_doc`` with its three reads stubbed."""

    def run(metering: dict):
        monkeypatch.setattr(
            lib,
            "doc_row",
            lambda *a, **k: {"ObjectStatus": "COMPLETED", "PageCount": 2},
        )
        monkeypatch.setattr(
            lib, "read_metering", lambda *a, **k: lib.Reading.present(metering)
        )
        monkeypatch.setattr(lib, "read_sections", lambda *a, **k: lib.SectionRead([]))
        return analyze.score_doc("bucket", "table", "run", "doc", None)

    return run


@pytest.mark.unit
class TestScoreDocWithholdsAPartialCost:
    def test_a_fully_priced_row_reports_every_cost_figure(self, scored_doc):
        row = scored_doc(
            {
                f"OCR/{REAL_OCR}": {"pages": 3},
                f"Extraction/{REAL_MODEL}": {"inputTokens": 1000, "totalTokens": 1000},
            }
        )
        assert row["cost_unpriced"] is None
        assert row["cost"] > 0
        assert set(row["cost_by_phase"]) == {"OCR", "Extraction"}
        assert set(row["cost_by_key"]) == {REAL_OCR, REAL_MODEL}

    # Three metering keys in ONE phase, with counts chosen so that rounding each
    # accumulation step to five places gives a different answer from rounding the sum
    # once. `round(a, 5)` then `round(a + b, 5)` yields 0.00009 here; `round(a + b +
    # c, 5)` yields 0.00010. The keys are three that appear together on real rows.
    #
    # ⚠️ A fixture with one key per phase cannot discriminate the rounding order at
    # all — there is no accumulation to round. Measured: with one key per phase,
    # changing `score_doc` to accumulate unrounded and round once left the suite
    # green.
    _ROUNDING_SENSITIVE = {
        "Extraction/bedrock/us.amazon.nova-lite-v1:0": {"inputTokens": 1000},
        "Extraction/lambda/requests": {"requests": 9},
        "Extraction/lambda/duration": {"gb_seconds": 2},
    }

    @staticmethod
    def _per_step_phases(metering: dict) -> dict[str, float]:
        """`cost_by_phase` computed the way it was before `by_meter_key` existed:
        one `price_metering` call per metering key, rounded at every accumulation."""
        out: dict[str, float] = {}
        for key, units in metering.items():
            phase = key.split("/")[0]
            out[phase] = round(
                out.get(phase, 0.0) + lib.price_metering({key: units}).total, 5
            )
        return out

    def test_the_fixture_below_really_does_discriminate_the_rounding_order(self):
        """Otherwise the next assertion is about nothing.

        Rounding once at the end is the natural way to write the replacement, and it
        would change `cost_by_phase` for a row like this — making a freshly scored
        grid incomparable with every committed one. This asserts the fixture can see
        that difference before asserting the code does not make it.
        """
        metering = self._ROUNDING_SENSITIVE
        for key in metering:
            assert key.split("/", 1)[1] in lib.PRICING, key
        per_step = self._per_step_phases(metering)
        at_end = round(
            sum(lib.price_metering({k: u}).total for k, u in metering.items()), 5
        )
        assert per_step["Extraction"] != at_end, (
            "the counts no longer produce a rounding difference, so the assertion "
            f"below cannot fail: {per_step} vs {at_end}"
        )

    def test_the_phase_breakdown_is_arithmetically_unchanged(self, scored_doc):
        """Deriving phases from ``by_meter_key`` must give the same numbers as the
        per-entry re-pricing it replaced, or every committed grid's `cost_by_phase`
        becomes incomparable with a freshly scored one."""
        for metering in (
            self._ROUNDING_SENSITIVE,
            {
                f"OCR/{REAL_OCR}": {"pages": 3},
                f"Extraction/{REAL_MODEL}": {"inputTokens": 1000, "outputTokens": 70},
                f"Assessment/{REAL_MODEL}": {"inputTokens": 900},
            },
        ):
            assert scored_doc(metering)["cost_by_phase"] == self._per_step_phases(
                metering
            ), metering

    def test_an_unpriced_entry_withholds_every_cost_figure(
        self, scored_doc, unpriced_model
    ):
        """Not a partial cost: a reader cannot see that a number is partial, and it
        is wrong in a known direction."""
        row = scored_doc(
            {
                f"OCR/{REAL_OCR}": {"pages": 3},
                f"Extraction/bedrock/{unpriced_model}": {"inputTokens": 500000},
            }
        )
        assert row["cost"] is None
        assert row["cost_by_phase"] is None
        assert row["cost_by_key"] is None
        assert row["cost_unpriced"] and unpriced_model in row["cost_unpriced"]
        # Not the #1079 state: the metering row read perfectly well.
        assert row["cost_unread"] is None

    def test_the_tokens_are_kept_because_they_were_read_not_priced(
        self, scored_doc, unpriced_model
    ):
        """Tokens are a measurement independent of the pricing table — and they are
        the evidence a reader needs in order to tell how much cost went missing."""
        row = scored_doc(
            {f"Extraction/bedrock/{unpriced_model}": {"inputTokens": 500000}}
        )
        assert row["cost"] is None
        assert row["tokens"] == {"inputTokens": 500000}
        assert row["tokens_by_phase"] == {"Extraction": {"inputTokens": 500000}}


# --------------------------------------------------------------------------- #
# 5. The withheld cost is visible downstream rather than merely missing
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestTheExclusionIsVisibleDownstream:
    def test_the_key_reaches_the_csv(self):
        """`DictWriter(extrasaction="ignore")` drops any row key absent from the
        list, so a reader comparing grids in a spreadsheet would see a blank cost
        cell and no reason for it."""
        assert "cost_unpriced" in aggregate.CSV_COLS

    def test_an_unpriced_cost_does_not_drag_a_cell_mean_toward_zero(self):
        rows = [
            {"cell": "c", "success": True, "cost": 0.30},
            {"cell": "c", "success": True, "cost": 0.30},
            {
                "cell": "c",
                "success": True,
                "cost": None,
                "cost_unpriced": "1 unpriceable metering entry: ...",
            },
        ]
        stats = aggregate.cell_stats(rows)["c"]
        assert stats["cost"]["mean"] == 0.30
        assert stats["cost"]["n"] == 2
        assert stats["n_success"] == 3
        assert stats["n_cost_unpriced"] == 1
        # Counted apart from the unread ones: the remedies differ, an unread row
        # needs the stack back and an unpriced one needs a pricing entry.
        assert stats["n_cost_unread"] == 0

    def test_the_count_is_over_successful_runs_only(self):
        """A run that FAILED contributes no cost figure for a different reason, so
        counting it here would make the shortfall look bigger than the sample it
        actually thinned."""
        rows = [
            {"cell": "c", "success": True, "cost": 0.30},
            {
                "cell": "c",
                "success": False,
                "cost": None,
                "cost_unpriced": "1 unpriceable metering entry: ...",
            },
        ]
        stats = aggregate.cell_stats(rows)["c"]
        assert stats["n_success"] == 1
        assert stats["n_cost_unpriced"] == 0

    def test_compare_cells_prints_the_shortfall_and_names_its_cause(self, tmp_path):
        cur, base = tmp_path / "cur.json", tmp_path / "base.json"
        rows = [
            {"cell": "c", "success": True, "cost": 0.30, "repeat": 0},
            {
                "cell": "c",
                "success": True,
                "cost": None,
                "cost_unpriced": "1 unpriceable metering entry: nope",
                "repeat": 1,
            },
        ]
        for path in (cur, base):
            path.write_text(
                json.dumps({"rows": rows, "cells": aggregate.cell_stats(rows)})
            )
        out = io.StringIO()
        with redirect_stdout(out):
            aggregate.compare_cells(str(cur), str(base))
        printed = out.getvalue()
        assert "MEASURED OVER FEWER RUNS THAN IT LOOKS" in printed
        assert "cannot price" in printed
        assert "cost_unpriced" in printed
        # The unread wording must not be reused for it — the causes are different.
        assert "the read failed, the run did not" not in printed


# --------------------------------------------------------------------------- #
# 6. The other two call sites
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestTheOtherCallSites:
    def test_real_corpus_ab_drops_and_names_an_unpriceable_document(
        self, unpriced_model
    ):
        item = _metering_item(
            {f"Extraction/bedrock/{unpriced_model}": {"inputTokens": 1000}}
        )
        cost, why = real_corpus_ab._cost(item)
        assert cost is None
        assert why and unpriced_model in why

    def test_real_corpus_ab_still_prices_a_clean_document(self):
        item = _metering_item({f"OCR/{REAL_OCR}": {"pages": 4}})
        cost, why = real_corpus_ab._cost(item)
        assert why is None
        assert cost == pytest.approx(4 * lib.PRICING[REAL_OCR]["pages"])

    def test_classification_cost_withholds_the_cost_and_keeps_the_tokens(
        self, unpriced_model
    ):
        cost, tokens, why = run_classification_bench.classification_cost(
            {
                f"Classification/bedrock/{unpriced_model}": {
                    "inputTokens": 3000,
                    "outputTokens": 40,
                }
            }
        )
        assert cost is None
        assert why and unpriced_model in why
        assert tokens["inputTokens"] == 3000

    def test_classification_cost_prices_a_known_model(self):
        cost, tokens, why = run_classification_bench.classification_cost(
            {f"Classification/{REAL_MODEL}": {"inputTokens": 3000}}
        )
        assert why is None
        assert cost == pytest.approx(3000 * lib.PRICING[REAL_MODEL]["inputTokens"])
        assert tokens["inputTokens"] == 3000

    def test_classification_cost_ignores_other_phases(self, unpriced_model):
        """Only the Classification step is priced here, so an unpriced model in
        another phase is not this function's business."""
        cost, _tokens, why = run_classification_bench.classification_cost(
            {
                f"Classification/{REAL_MODEL}": {"inputTokens": 100},
                f"Extraction/bedrock/{unpriced_model}": {"inputTokens": 100},
            }
        )
        assert why is None
        assert cost is not None

    def test_score_run_reports_no_pooled_cost_when_one_document_is_unpriced(
        self, monkeypatch, unpriced_model, capsys
    ):
        """A pooled sum is short by an unknown amount, and per-page cost is the
        figure this suite compares classification models on."""
        metering = {
            "clean": {f"Classification/{REAL_MODEL}": {"inputTokens": 1000}},
            "broken": {
                f"Classification/bedrock/{unpriced_model}": {"inputTokens": 1000}
            },
        }
        monkeypatch.setattr(
            lib, "list_doc_prefixes", lambda *a, **k: ["r/clean/", "r/broken/"]
        )
        monkeypatch.setattr(lib, "read_json", lambda *a, **k: lib.Reading.present({}))
        monkeypatch.setattr(
            lib,
            "read_metering",
            lambda _t, _r, doc: lib.Reading.present(metering[doc]),
        )
        monkeypatch.setattr(lib, "doc_row", lambda *a, **k: {"PageCount": 2})
        out = run_classification_bench.score_run(
            {"output_bucket": "b", "tracking_table": "t"}, "r"
        )
        assert out["classification_cost"] is None
        assert out["classification_cost_per_page"] is None
        assert out["classification_cost_unpriced"]
        assert unpriced_model in out["classification_cost_unpriced"][0]
        # The token figures survive, and so does the accuracy pooling.
        assert out["classification_tokens"]["inputTokens"] == 2000
        assert out["n_docs"] == 2
        assert "cannot price" in capsys.readouterr().out

    def test_every_cost_call_in_the_harness_is_bound_by_tuple_unpacking(self):
        """A class guard over the call sites, not a check on the known ones.

        ``per_class_ab`` read ``rc._cost(ib) - rc._cost(ia)`` — a site a grep for
        ``price_metering`` does not find, because it goes through ``real_corpus_ab``.

        **The rule is positional, not a list of bad shapes:** a ``*_cost`` call must be
        the value of an assignment whose target is a tuple. Enumerating the ways a
        result can be misused does not work, and the reason is measured rather than
        argued — a guard that flagged a call appearing directly inside a ``BinOp``,
        ``Compare`` or ``UnaryOp`` missed ``(rc._cost(b)[0] or 0.0) - (rc._cost(a)[0]
        or 0.0)``, which is the *silent* spelling: it pools a withheld cost as zero,
        which is #1146 restored. ``basedpyright`` does not object to it either —
        subscripting is legal on the tuple and ``or 0.0`` removes the ``None`` — so it
        passed both covers while the two louder spellings passed neither.

        Requiring tuple unpacking is deliberately stricter than "is not used as a
        number": it also rejects stashing the whole tuple in a list to unpack later.
        That is a shape nothing here needs, and the strictness is the point — the
        reason has to be handled where the cost is obtained.
        """
        harness = Path(lib.__file__).parent
        files = sorted(harness.glob("*.py"))
        assert len(files) > 5, f"harness not found at {harness}"
        offenders = []
        for path in files:
            offenders += [
                f"{path.name}:{lineno}"
                for lineno in _unbound_cost_calls(ast.parse(path.read_text()))
            ]
        assert not offenders, (
            "a (cost, unpriced_reason) result is being used without unpacking its "
            "reason, so a withheld cost can be pooled as a number: "
            f"{offenders}"
        )

    @pytest.mark.parametrize(
        "source",
        [
            "x = rc._cost(b) - rc._cost(a)",
            "x = rc._cost(b)[0] - rc._cost(a)[0]",
            "x = (rc._cost(b)[0] or 0.0) - (rc._cost(a)[0] or 0.0)",
            "g.cost.append(rc._cost(b))",
            "if rc._cost(b) > rc._cost(a): pass",
            "total += classification_cost(m)[0]",
        ],
        ids=["subtract", "subscript", "subscript-or-zero", "stash", "compare", "sum"],
    )
    def test_the_guard_rejects_every_way_of_dropping_the_reason(self, source):
        """Run through the real collector, including the spelling that defeated the
        first version of this guard and the type checker at the same time."""
        assert _unbound_cost_calls(ast.parse(source + "\n"))

    @pytest.mark.parametrize(
        "source",
        [
            "cb, pb = rc._cost(ib)",
            "cost, tokens, why = classification_cost(m)",
        ],
        ids=["two", "three"],
    )
    def test_the_guard_accepts_tuple_unpacking(self, source):
        assert not _unbound_cost_calls(ast.parse(source + "\n"))

    def test_score_run_prices_a_clean_run(self, monkeypatch):
        monkeypatch.setattr(lib, "list_doc_prefixes", lambda *a, **k: ["r/clean/"])
        monkeypatch.setattr(lib, "read_json", lambda *a, **k: lib.Reading.present({}))
        monkeypatch.setattr(
            lib,
            "read_metering",
            lambda *a, **k: lib.Reading.present(
                {f"Classification/{REAL_MODEL}": {"inputTokens": 1000}}
            ),
        )
        monkeypatch.setattr(lib, "doc_row", lambda *a, **k: {"PageCount": 2})
        out = run_classification_bench.score_run(
            {"output_bucket": "b", "tracking_table": "t"}, "r"
        )
        assert out["classification_cost"] > 0
        assert out["classification_cost_per_page"] > 0
        assert out["classification_cost_unpriced"] is None
        # The count of documents whose metering could not be READ keeps its own key,
        # named for what it counts so that it is not read as the new one.
        assert "classification_cost_unread_docs" in out
        assert out["classification_cost_unread_docs"] is None


# --------------------------------------------------------------------------- #
# 7. Before the grid is paid for, not after
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestEverySelectableModelIsPriced:
    """The cheapest half of this: a model can only enter a suite through an axis in
    ``config_matrix.yaml``, so the ids it can select are readable offline.

    This does not make the detection above redundant — a metering key can name a
    Textract feature, a Lambda hook or a Bedrock model no axis mentions — but it
    turns the commonest way in (adding a model arm) into a failing test rather than
    a grid whose cost is quietly low.
    """

    @staticmethod
    def _axis_models() -> dict[str, str]:
        """``{axis_value_label: model_id}`` for every axis that sets a ``*.model``
        config path, derived from the file rather than from a list of axis names."""
        axes = yaml.safe_load(CONFIG_MATRIX.read_text())["axes"]
        found = {}
        for axis, values in axes.items():
            for label, settings in (values or {}).items():
                if not isinstance(settings, dict):
                    continue
                for path, value in settings.items():
                    if re.fullmatch(r"[\w.]*\.model", str(path)) and isinstance(
                        value, str
                    ):
                        found[f"{axis}.{label}"] = value
        return found

    def test_the_rule_finds_the_model_axes_at_all(self):
        """Vacuity guard: an empty result would make the next test pass forever."""
        models = self._axis_models()
        assert len(models) >= 5, models
        assert {a.split(".")[0] for a in models} >= {
            "extraction_model",
            "classification_model",
            "confidence_model",
        }

    def test_every_model_a_suite_can_select_has_a_pricing_entry(self):
        missing = {
            axis: model
            for axis, model in self._axis_models().items()
            if f"bedrock/{model}" not in lib.PRICING
        }
        assert not missing, (
            f"{len(missing)} model arm(s) in {CONFIG_MATRIX.name} have no "
            f"'bedrock/<id>' entry in {PRICING_YAML.name}, so any cell using one "
            f"reports a cost below truth: {missing}"
        )
