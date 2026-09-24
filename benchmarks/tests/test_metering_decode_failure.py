# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A metering row that will not decode is unknown, not free (#1205).

What was wrong
--------------
``lib.read_metering`` welded the DynamoDB fetch to the classification of the
``Metering`` attribute, so a caller that already held a row — anything working from a
``Scan`` rather than a ``GetItem`` — could not reuse it. Two wrote their own, and both
answered ``{}`` for every encoding they could not decode:

* ``real_corpus_ab._metering``, feeding ``_cost`` (a confident, **complete** ``$0.00``
  — ``Priced.complete`` is true, so #1146's shortfall report says nothing) and
  ``_token_classes`` (a zero in each of the four token classes);
* ``detection_ab_teststudio._tokens``, one function below ``_score``, which had
  already been given the three-state treatment in #1079 — so the two halves of the
  same row disagreed about what an unreadable row means.

Both feed **paired means across documents**, where a zero is indistinguishable from a
measurement and drags the mean toward zero in the usual direction.

What these tests pin
--------------------
1. ``lib.metering_of_item`` is the one decoder, and it separates the three states.
   The set of encodings that cannot decode to a map is **derived from**
   ``ddb_to_py``'s own branch structure, not listed here, so a branch added there
   without being classified fails in this file.
2. ``read_metering`` delegates to it — the same item yields the same state through
   both entry points — and adds only the state a row-holding caller cannot reach.
3. ``$0.00`` is still reported when it is real. A row with no ``Metering`` attribute
   is the shipped writer's encoding of "metered nothing" (``if document.metering:``),
   and it must keep pricing to zero; that is the distinction the whole change is
   about, so withholding it too would be the opposite error.
4. Both former decoders withhold **every** figure they would have derived, and name
   the reason — including when the row read fine and one count is not a number,
   because a reason of ``None`` has to mean the number is trustworthy.
5. ⚠️ **The reasons reach the callers**, which is the half of #1079 that stays open
   when a reason is produced and nobody reads it. Driven through both
   ``cmd_analyse`` functions end to end, asserting the *arithmetic*: an excluded
   document changes ``n_pairs`` and the mean, so a test that only checked for a
   printed warning would pass while the zero was still being averaged in.
6. A third local decoder fails a guard, and the guard is about **capability**
   rather than spelling: ``ddb_to_py`` — the only thing that turns a DynamoDB
   attribute value into a Python value — is callable from ``lib.py`` alone, so a
   module that cannot decode cannot write a decoder however it spells one. Two
   earlier spelling-based rules were each walked past; the seven spellings that did
   it are kept as probes. The residual is written where the rule is implemented.

What is NOT claimed
-------------------
**No committed figure is shown to be wrong, and it cannot be from the artifacts.**
The two committed ``real_corpus_ab`` summaries record paired *deltas* only, so a
per-document zero leaves no signature in them — ``n_pairs`` equals the paired count
both when the defect fired and when it did not, which is precisely why the exclusion
list is now in the artifact. The shipped writer stores ``Metering`` as
``json.dumps(..., default=str)``, which always yields a JSON object, so the failure
states need a row some other writer produced. This is a contract fix, not a
correction to a published number.

``lib.ddb_to_py(None)`` still raises ``TypeError`` instead of answering ``None``, and
that is tracked in #1223. What is resolved here is its one reachable consequence: the
two unguarded call sites are gone, because confining ``ddb_to_py`` to ``lib.py`` meant
giving ``ObjectStatus`` and ``Sections`` named readers, and a named reader has to
decide what an absent attribute means. ``Sections`` is written only when non-empty, so
a document that produced none used to take the whole analysis down. The primitive's own
fragility is what remains open.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from harness_import import HARNESS, harness_module

lib = harness_module("lib")
real_corpus_ab = harness_module("real_corpus_ab")
detection_ab = harness_module("detection_ab_teststudio")
per_class_ab = harness_module("per_class_ab")

REPO = Path(lib.REPO)

# A pricing key the shipped table holds, so a "present" reading prices to a real
# non-zero number rather than to the zero these tests are distinguishing it from.
REAL_MODEL = "bedrock/us.anthropic.claude-sonnet-5"


# --------------------------------------------------------------------------- #
# The authority for "what cannot decode": ddb_to_py's own branches
# --------------------------------------------------------------------------- #
def _handled_type_codes() -> list[str]:
    """The DynamoDB attribute-value codes ``ddb_to_py`` handles, read off its source.

    Derived rather than listed: a branch added to ``ddb_to_py`` — a ``"SS"`` case,
    say — changes what can arrive at the decoder, and a hand-written list would go on
    passing while the new shape went unclassified.
    """
    source = Path(HARNESS, "lib.py").read_text()
    tree = ast.parse(source)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "ddb_to_py"
    )
    codes = [
        node.left.value
        for node in ast.walk(fn)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Constant)
        and isinstance(node.left.value, str)
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.In)
    ]
    assert codes, "found no type-code branches in ddb_to_py — the derivation broke"
    return codes


# One attribute value per handled code, with a value that is valid for that code.
_VALUE_FOR_CODE = {
    "M": {"Extraction/x": {"N": "1"}},
    "N": "0",
    "S": '{"Extraction/x": {"inputTokens": 1}}',
    "L": [],
    "BOOL": False,
}

# Codes DynamoDB defines that `ddb_to_py` does not handle, so it answers None for
# them. Not derived, because the absence of a branch cannot be read off the source;
# the derivation above is what guarantees this list stays a subset of "unhandled".
_UNHANDLED = {"B": b"x", "NULL": True, "SS": ["a"], "NS": ["1"], "BS": [b"x"]}


def _item(attribute: dict | None) -> dict:
    """A tracking row, in the attribute-value form a ``Scan`` returns.

    ``ObjectStatus`` and ``Sections`` are here because a real completed row carries
    both, and a fixture that is minimal rather than realistic is how a test ends up
    describing a shape the code never sees. The readers tolerate their absence
    (``test_the_named_readers_survive_an_absent_attribute``), so this is realism
    rather than a workaround.
    """
    row = {
        "PK": {"S": "doc#r/d"},
        "SK": {"S": "none"},
        "ObjectStatus": {"S": "COMPLETED"},
        "Sections": {"L": []},
    }
    if attribute is not None:
        row["Metering"] = attribute
    return row


@pytest.mark.unit
class TestTheOneDecoderSeparatesTheThreeStates:
    def test_every_handled_type_code_is_classified(self):
        """Present exactly when the attribute decodes to a map; failed otherwise."""
        seen = {}
        for code in _handled_type_codes():
            assert code in _VALUE_FOR_CODE, (
                f"ddb_to_py grew a {code!r} branch and this file does not classify it"
            )
            read = lib.metering_of_item(_item({code: _VALUE_FOR_CODE[code]}))
            decoded = lib.ddb_to_py({code: _VALUE_FOR_CODE[code]})
            if isinstance(decoded, str):
                decoded = json.loads(decoded)
            seen[code] = read.state
            assert read.state == ("present" if isinstance(decoded, dict) else "failed")
        # ...and the two that can carry a map are the two the shipped writers use:
        # a native Map, and the JSON string `json.dumps` produces.
        assert {c for c, s in seen.items() if s == "present"} == {"M", "S"}

    @pytest.mark.parametrize("code", sorted(_UNHANDLED))
    def test_a_type_code_ddb_to_py_does_not_handle_is_a_failure(self, code):
        """``ddb_to_py`` answers ``None`` for these, which used to become ``{}``."""
        assert code not in _handled_type_codes()
        read = lib.metering_of_item(_item({code: _UNHANDLED[code]}))
        assert read.is_failed
        assert "not a map" in str(read.error)

    @pytest.mark.parametrize(
        "raw",
        ["{not json", "", "   ", "{'single': 'quotes'}"],
        ids=["truncated", "empty", "whitespace", "python-repr"],
    )
    def test_a_string_that_is_not_json_is_a_failure(self, raw):
        read = lib.metering_of_item(_item({"S": raw}))
        assert read.is_failed
        assert "not JSON" in str(read.error)

    @pytest.mark.parametrize(
        "raw", ["[]", "5", "null", '"a string"', "true"], ids=list("abcde")
    )
    def test_valid_json_that_is_not_an_object_is_a_failure(self, raw):
        """`json.loads` succeeds and hands back something that is not a metering map."""
        read = lib.metering_of_item(_item({"S": raw}))
        assert read.is_failed
        assert "not a map" in str(read.error)

    def test_the_shipped_json_string_encoding_reads_as_present(self):
        """``json.dumps(document.metering, default=str)`` is what production writes,
        so the string branch is the ordinary path rather than an edge case."""
        metering = {f"Extraction/{REAL_MODEL}": {"inputTokens": 1000}}
        read = lib.metering_of_item(_item({"S": json.dumps(metering)}))
        assert read.is_present
        assert read.value == metering

    def test_no_metering_attribute_is_a_real_zero(self):
        """The shipped writer omits the attribute when nothing was metered
        (``if document.metering:``), so this is how "$0.00" arrives — and it must
        keep pricing to zero. Withholding it would be the opposite error."""
        read = lib.metering_of_item(_item(None))
        assert read.is_present
        assert read.value == {}
        priced = lib.price_metering(read.value)
        assert priced.complete
        assert priced.total == 0.0

    def test_no_row_at_all_is_an_absence(self):
        for empty in (None, {}):
            read = lib.metering_of_item(empty)
            assert read.is_absent
            assert not read.is_present

    def test_the_reason_can_name_which_row(self):
        """A caller holding hundreds of scanned rows needs the reason to identify one."""
        failed = lib.metering_of_item(_item({"N": "0"}), where="doc#r/d")
        assert failed.is_failed
        assert "doc#r/d" in str(failed.error)
        absent = lib.metering_of_item(None, where="doc#r/d")
        assert "doc#r/d" in str(absent.error)


@pytest.mark.unit
class TestReadMeteringDelegates:
    @pytest.fixture
    def ddb(self, monkeypatch):
        def install(item):
            class _DDB:
                def get_item(self, **_kw):
                    return {"Item": item} if item is not None else {}

            monkeypatch.setattr(lib, "ddb", lambda: _DDB())

        return install

    @pytest.mark.parametrize(
        "attribute",
        [None, {"N": "0"}, {"L": []}, {"S": "{not json"}, {"B": b"x"}, {"M": {}}],
        ids=["no-attr", "number", "list", "not-json", "binary", "empty-map"],
    )
    def test_the_same_item_yields_the_same_state_through_both_entry_points(
        self, ddb, attribute
    ):
        """One classifier, so the by-key and by-item paths cannot drift again — which
        is what they had already done."""
        item = _item(attribute)
        ddb(item)
        assert lib.read_metering("t", "r", "d").state == (
            lib.metering_of_item(item).state
        )

    def test_it_still_adds_the_state_an_item_holder_cannot_reach(self, monkeypatch):
        """A failing table read. ``metering_of_item`` never sees it — a caller holding
        a row has already survived the fetch — so this stays here (#1079)."""

        class _Boom:
            def get_item(self, **_kw):
                raise RuntimeError("Throttled")

        monkeypatch.setattr(lib, "ddb", lambda: _Boom())
        read = lib.read_metering("t", "r", "d")
        assert read.is_failed
        assert "Throttled" in str(read.error)

    def test_a_missing_row_is_absent_and_names_the_key(self, ddb):
        ddb(None)
        read = lib.read_metering("t", "r", "doc-x")
        assert read.is_absent
        assert "doc#r/doc-x" in str(read.error)


# --------------------------------------------------------------------------- #
# The two former decoders
# --------------------------------------------------------------------------- #
# Every attribute shape that cannot decode to a metering map, in one list, so each
# assertion below is made against all of them rather than against the one that is
# easiest to construct.
UNDECODABLE = [
    pytest.param({"S": "{not json"}, id="not-json"),
    pytest.param({"S": "[]"}, id="json-list"),
    pytest.param({"S": "5"}, id="json-number"),
    pytest.param({"N": "0"}, id="number"),
    pytest.param({"L": []}, id="list"),
    pytest.param({"BOOL": False}, id="bool"),
    pytest.param({"B": b"x"}, id="binary"),
    pytest.param({"NULL": True}, id="null"),
]

PRICEABLE = {"M": {f"Extraction/{REAL_MODEL}": {"M": {"inputTokens": {"N": "1000"}}}}}


@pytest.mark.unit
class TestRealCorpusAbWithholdsBothFigures:
    @pytest.mark.parametrize("attribute", UNDECODABLE)
    def test_cost_is_withheld_and_named(self, attribute):
        cost, why = real_corpus_ab._cost(_item(attribute))
        assert cost is None, "an undecodable metering row must not price to a number"
        assert why and "metering failed" in why

    @pytest.mark.parametrize("attribute", UNDECODABLE)
    def test_every_token_class_is_withheld_and_named(self, attribute):
        tokens, why = real_corpus_ab._token_classes(_item(attribute))
        assert tokens is None
        assert why and "metering failed" in why

    def test_a_priceable_row_still_gives_both(self):
        cost, why = real_corpus_ab._cost(_item(PRICEABLE))
        assert why is None
        assert cost == pytest.approx(1000 * lib.PRICING[REAL_MODEL]["inputTokens"])
        tokens, why = real_corpus_ab._token_classes(_item(PRICEABLE))
        assert why is None
        assert tokens["inputTokens"] == 1000

    def test_a_genuinely_unmetered_row_still_reports_zero(self):
        """The distinction the change is about: this zero is a measurement."""
        cost, why = real_corpus_ab._cost(_item(None))
        assert (cost, why) == (0.0, None)
        tokens, why = real_corpus_ab._token_classes(_item(None))
        assert why is None
        assert set(tokens.values()) == {0}

    def test_a_token_count_that_is_not_a_number_withholds_the_map(self):
        """A map short by one class is no more reportable than one short by four, so
        the reason stays the only thing that means "do not trust this"."""
        item = _item(
            {"M": {f"Extraction/{REAL_MODEL}": {"M": {"inputTokens": {"S": "lots"}}}}}
        )
        tokens, why = real_corpus_ab._token_classes(item)
        assert tokens is None
        assert why and "not a number" in why
        assert "inputTokens" in why

    def test_the_old_local_decoder_contract_is_gone(self):
        """A copy of the loop body it fed fails loudly rather than seeing ``{}``.

        ``Reading`` has none of a dict's methods and ``__bool__`` raises, so both
        spellings of the old code are errors rather than silent zeros.
        """
        read = real_corpus_ab._metering(_item({"N": "0"}))
        assert isinstance(read, lib.Reading)
        with pytest.raises(AttributeError):
            read.values()  # pyright: ignore[reportAttributeAccessIssue]
        with pytest.raises(TypeError):
            _ = read or {}


@pytest.mark.unit
class TestDetectionAbWithholdsBothCounts:
    @pytest.mark.parametrize("attribute", UNDECODABLE)
    def test_both_counts_are_withheld_and_named(self, attribute):
        inp, outp, why = detection_ab._tokens(_item(attribute))
        assert (inp, outp) == (None, None)
        assert why and "metering failed" in why

    def test_a_readable_row_gives_both_counts(self):
        item = _item(
            {
                "M": {
                    f"Extraction/{REAL_MODEL}": {
                        "M": {"inputTokens": {"N": "100"}, "outputTokens": {"N": "7"}}
                    }
                }
            }
        )
        assert detection_ab._tokens(item) == (100.0, 7.0, None)

    def test_a_genuinely_unmetered_row_still_reports_zero(self):
        assert detection_ab._tokens(_item(None)) == (0.0, 0.0, None)

    def test_cache_tokens_are_summed_into_the_input_count_on_purpose(self):
        """⚠️ Do not "fix" this: a published page depends on it.

        This tool matches token units by substring, so ``inputTokens``,
        ``cacheReadInputTokens`` and ``cacheWriteInputTokens`` all land in ``in_tok``.
        That looks like the substring defect removed from production pricing in #926
        and it is not the same thing — ``docs/benchmarking/studies/prompt-caching.md``
        §7 documents it as this instrument's known limitation and names
        ``real_corpus_ab.py`` as the one that keeps the four classes separate. Making
        the matching exact here would make that page wrong. Measured before this test
        existed: narrowing it to the two exact names was undetected.
        """
        item = _item(
            _av(
                {
                    f"Extraction/{REAL_MODEL}": {
                        "inputTokens": 100,
                        "cacheReadInputTokens": 20,
                        "cacheWriteInputTokens": 3,
                        "outputTokens": 7,
                        "totalTokens": 130,
                    }
                }
            )
        )
        inp, outp, why = detection_ab._tokens(item)
        assert why is None
        assert inp == 123.0, "the three input-side classes are summed, by design"
        assert outp == 7.0
        page = (REPO / "docs/benchmarking/studies/prompt-caching.md").read_text()
        assert "contains both" in page and "cacheReadInputTokens" in page, (
            "the page documenting this behaviour has changed; re-read it before "
            "relying on the assertion above"
        )

    def test_a_token_count_that_is_not_a_number_withholds_both(self):
        item = _item(
            {"M": {f"Extraction/{REAL_MODEL}": {"M": {"inputTokens": {"S": "lots"}}}}}
        )
        inp, outp, why = detection_ab._tokens(item)
        assert (inp, outp) == (None, None)
        assert why and "not a number" in why


# --------------------------------------------------------------------------- #
# The reasons reach the callers — asserted on the arithmetic, not on the warning
# --------------------------------------------------------------------------- #
def _av(value):
    """Python value to a DynamoDB attribute value."""
    if isinstance(value, dict):
        return {"M": {k: _av(v) for k, v in value.items()}}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    return {"S": str(value)}


def _priced_row(input_tokens: int) -> dict:
    return _item(_av({f"Extraction/{REAL_MODEL}": {"inputTokens": input_tokens}}))


@pytest.mark.unit
class TestTheExclusionReachesTheReport:
    """Two good documents and one whose metering will not decode.

    ``_paired_stats`` needs two pairs, so two good ones is the minimum that produces
    a figure at all — which is also what makes this discriminating: with the zero
    averaged back in there would be three pairs and a different mean.
    """

    ITEMS = {
        "good1": _priced_row(1000),
        "good2": _priced_row(3000),
        "broken": _item({"S": "{not json"}),
    }

    def _run(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            real_corpus_ab,
            "_resources",
            lambda _stack: {
                "output_bucket": "b",
                "testset_bucket": "tb",
                "tracking_table": "t",
            },
        )
        monkeypatch.setattr(
            real_corpus_ab, "_docs_of_run", lambda _t, _r: dict(self.ITEMS)
        )
        monkeypatch.setattr(real_corpus_ab, "_score", lambda *_a: (0.5, None))
        monkeypatch.setattr(real_corpus_ab.cache_audit, "audit_run", lambda *_a: [])
        (tmp_path / real_corpus_ab.STATE).write_text(
            json.dumps(
                [
                    {
                        "corpus": "c",
                        "arm": "A",
                        "n": 3,
                        "run_id": "ra",
                        "profile": "pa",
                    },
                    {
                        "corpus": "c",
                        "arm": "B",
                        "n": 3,
                        "run_id": "rb",
                        "profile": "pb",
                    },
                ]
            )
        )

        class _Args:
            stack = "S"
            outdir = str(tmp_path)
            counter = None

        real_corpus_ab.cmd_analyse(_Args())
        printed = capsys.readouterr().out
        return json.loads((tmp_path / "summary.json").read_text())["c"], printed

    def test_the_broken_document_contributes_to_neither_cost_nor_tokens(
        self, tmp_path, monkeypatch, capsys
    ):
        report, _printed = self._run(tmp_path, monkeypatch, capsys)
        assert report["paired"] == 3
        # Two pairs, not three: the arithmetic is the assertion. A printed warning
        # alone would be satisfied while the zero was still averaged in.
        assert report["cost"]["n_pairs"] == 2
        for unit in real_corpus_ab.UNITS:
            stats = report["tokens"][unit]
            if stats is not None:
                assert stats["n_pairs"] == 2, unit

    def test_the_token_mean_is_over_the_remainder(self, tmp_path, monkeypatch, capsys):
        """Both arms use the same items here, so every paired delta is 0 — the mean
        that would move is the arm mean, which is printed rather than reported.

        ⚠️ **Both columns are asserted, by position.** The line carries the A mean and
        the B mean, and this fixture makes them equal, so a substring test for the
        expected figure is satisfied by either one. Measured: making the A column drop
        a document left ``"2,000" in line`` true and the suite green.
        """
        _report, printed = self._run(tmp_path, monkeypatch, capsys)
        line = next(
            ln for ln in printed.splitlines() if ln.strip().startswith("inputTokens")
        )
        # class, A, B, delta, % — (1000 + 3000) / 2 = 2,000 in BOTH arms. Including
        # the broken document's zero would give 4000/3 = 1,333.
        columns = line.split()
        assert columns[0] == "inputTokens", line
        assert columns[1] == "2,000", f"A column: {line}"
        assert columns[2] == "2,000", f"B column: {line}"
        assert "1,333" not in line

    def test_the_token_table_prints_its_surviving_denominator(
        self, tmp_path, monkeypatch, capsys
    ):
        """Otherwise a thinned mean and a whole one look identical on the console.

        ⚠️ Asserted against the token table's **own** line. Both the cost line and
        this one print the same sentence, so a search of the whole output is satisfied
        by the other one — measured: hardcoding this numerator to the paired count
        left the suite green, because the cost line still said `over 2 of 3`.
        """
        _report, printed = self._run(tmp_path, monkeypatch, capsys)
        own_line = next(
            (ln for ln in printed.splitlines() if ln.strip().startswith("over ")), None
        )
        assert own_line is not None, printed
        assert own_line.strip().startswith("over 2 of 3 paired document(s)"), own_line
        assert "would not read" in own_line, own_line

    def test_the_cost_line_prints_its_surviving_denominator_too(
        self, tmp_path, monkeypatch, capsys
    ):
        """The token table said this and the cost line did not, which is the same
        asymmetry within one report that the change argues against elsewhere."""
        _report, printed = self._run(tmp_path, monkeypatch, capsys)
        cost_line = next(ln for ln in printed.splitlines() if "COST/doc:" in ln)
        assert "over 2 of 3 paired document(s)" in cost_line, cost_line

    def test_the_reason_is_in_the_artifact_and_not_only_on_the_console(
        self, tmp_path, monkeypatch, capsys
    ):
        report, printed = self._run(tmp_path, monkeypatch, capsys)
        assert report["n_excluded"], report
        joined = " ".join(report["excluded"])
        assert "broken" in joined
        assert "metering failed" in joined
        # Both halves of the row are named, because they are excluded separately.
        assert "cost:" in joined
        assert "tokens:" in joined
        assert "EXCLUDED" in printed

    def test_a_clean_run_records_no_exclusion(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setitem(self.ITEMS, "broken", _priced_row(2000))
        try:
            report, _printed = self._run(tmp_path, monkeypatch, capsys)
            assert report["excluded"] is None
            assert report["n_excluded"] is None
            assert report["cost"]["n_pairs"] == 3
        finally:
            self.ITEMS["broken"] = _item({"S": "{not json"})


@pytest.mark.unit
class TestTheExclusionReachesTheDetectionReport:
    def _run(self, tmp_path, monkeypatch, capsys, broken=True):
        items = {
            "good1": _priced_row(1000),
            "good2": _priced_row(3000),
            "third": _item({"S": "{not json"}) if broken else _priced_row(2000),
        }
        monkeypatch.setattr(
            detection_ab,
            "_resources",
            lambda _stack: {"output_bucket": "b", "tracking_table": "t"},
        )
        monkeypatch.setattr(detection_ab, "_docs_of_run", lambda _t, _r: dict(items))
        monkeypatch.setattr(detection_ab, "_score", lambda *_a: (0.5, None))
        (tmp_path / detection_ab.STATE).write_text(
            json.dumps(
                [
                    {
                        "corpus": "c",
                        "arm": "off",
                        "n": 3,
                        "run_id": "r0",
                        "profile": "p-off-x",
                    },
                    {
                        "corpus": "c",
                        "arm": "on",
                        "n": 3,
                        "run_id": "r1",
                        "profile": "p-on-x",
                    },
                ]
            )
        )

        class _Args:
            stack = "S"
            outdir = str(tmp_path)

        detection_ab.cmd_analyse(_Args())
        return capsys.readouterr().out

    def test_the_token_mean_excludes_the_unreadable_row(
        self, tmp_path, monkeypatch, capsys
    ):
        printed = self._run(tmp_path, monkeypatch, capsys)
        assert "METERING could not be read" in printed
        assert "over 2 of 3 scored document(s)" in printed
        # The FIGURES line, not the "over N of M" warning, which also starts
        # with the label — matching the wrong one is how this assertion first
        # passed against a line carrying no number at all.
        line = next(ln for ln in printed.splitlines() if "INPUT tokens: off" in ln)
        # ⚠️ BOTH arms asserted, by label. The line is
        # "INPUT tokens: off 2,000  on 2,000  (+0.00%)" and this fixture makes the two
        # equal, so a substring test for the figure is satisfied by either column.
        # Measured: making the `off` arm drop a document left `"2,000" in line` true
        # and the whole suite green.
        assert "off 2,000" in line, line
        assert "on 2,000" in line, line
        assert "1,333" not in line

    def test_a_clean_run_says_nothing_and_averages_all_three(
        self, tmp_path, monkeypatch, capsys
    ):
        printed = self._run(tmp_path, monkeypatch, capsys, broken=False)
        assert "METERING could not be read" not in printed
        assert "scored document(s)" not in printed
        # The FIGURES line, not the "over N of M" warning, which also starts
        # with the label — matching the wrong one is how this assertion first
        # passed against a line carrying no number at all.
        line = next(ln for ln in printed.splitlines() if "INPUT tokens: off" in ln)
        assert "off 2,000" in line, line
        assert "on 2,000" in line, line


# --------------------------------------------------------------------------- #
# A third local decoder cannot be written
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestTheDecoderCannotBeDuplicated:
    """The rule is structural: **outside ``lib.py``, nothing decodes.**

    An earlier form of this guard confined the ``"Metering"`` string literal to
    ``lib.py``, and a second confined deciding whether a ``ddb_to_py`` result is a
    map. Both were about *spelling*, and both were walked past — the first by
    ``"Meter" + "ing"``, the second by a conditional-expression assignment and then
    by an annotated one, each time with the whole suite green. A rule that enumerates
    how a thing might be written loses to the next spelling; that is the lesson this
    file is carrying forward from the ``_cost`` guard in #1146.

    So the rule is about **capability** instead. ``ddb_to_py`` is the only thing in
    the harness that turns a DynamoDB attribute value into a Python value, and it is
    callable from ``lib.py`` only. A module that cannot decode cannot write a
    decoder, however it spells one. The named per-attribute readers
    (``metering_of_item``, ``status_of_item``, ``sections_of_item``) are what other
    modules use, and adding one is the way to read a new attribute.

    ⚠️ **The residual, stated rather than left to be discovered:** a module could
    re-implement ``ddb_to_py``'s body inline — ``v["M"]``, ``float(v["N"])`` — and
    decode without calling it. That is a much larger thing to write by accident than
    a four-line local reader, and no ast rule closes it. The behavioural tests above
    are the primary cover; this pins the layering.

    The collectors use ``rglob``, not ``glob``: ``benchmarks/harness/compat/``
    already exists and already talks to DynamoDB, and a non-recursive walk did not
    look at it. A verbatim copy of the #1205 decoder placed there passed both earlier
    rules with the suite byte-identically green.
    """

    @staticmethod
    def _harness_modules() -> list[Path]:
        """Every harness module, including the ones in subdirectories."""
        files = sorted(Path(HARNESS).rglob("*.py"))
        assert len(files) > 5, f"harness not found at {HARNESS}"
        assert any(f.parent != Path(HARNESS) for f in files), (
            "no module in a harness subdirectory, so rglob is indistinguishable from "
            "glob here and this guard has lost the property it was fixed for"
        )
        return files

    @staticmethod
    def _modules_calling(name: str, files: list[Path]) -> dict[str, list[int]]:
        """``{module: line numbers}`` for calls to ``name``, however it is reached.

        Covers a bare call, an attribute call (``lib.ddb_to_py``) and
        ``getattr(lib, "ddb_to_py")``, because the last is a one-line way around a
        rule that only looks at call syntax.
        """
        out: dict[str, list[int]] = {}
        for path in files:
            lines = []
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if isinstance(fn, ast.Name) and fn.id == name:
                    lines.append(node.lineno)
                elif isinstance(fn, ast.Attribute) and fn.attr == name:
                    lines.append(node.lineno)
                elif (
                    isinstance(fn, ast.Name)
                    and fn.id == "getattr"
                    and any(
                        isinstance(a, ast.Constant) and a.value == name
                        for a in node.args
                    )
                ):
                    lines.append(node.lineno)
            if lines:
                out[path.name] = sorted(lines)
        return out

    def test_only_lib_decodes_a_dynamodb_attribute_value(self):
        calling = self._modules_calling("ddb_to_py", self._harness_modules())
        assert "lib.py" in calling, (
            "lib.py no longer calls ddb_to_py, so this rule measures nothing"
        )
        assert set(calling) == {"lib.py"}, (
            "a module outside lib.py decodes a DynamoDB attribute value, which is "
            "what every local metering decoder was built out of. Add a named reader "
            f"to lib.py (see metering_of_item / status_of_item) and call that: {calling}"
        )

    def test_only_lib_names_the_metering_attribute(self):
        """Kept as a second, cheaper rule: it names the file and line directly, and
        an accidental copy trips it before the structural one is consulted."""
        naming = {}
        for path in self._harness_modules():
            lines = [
                node.lineno
                for node in ast.walk(ast.parse(path.read_text()))
                if isinstance(node, ast.Constant) and node.value == "Metering"
            ]
            if lines:
                naming[path.name] = lines
        assert "lib.py" in naming, (
            "lib.py no longer names the attribute, so this rule measures nothing"
        )
        assert set(naming) == {"lib.py"}, (
            "a second module names the `Metering` attribute, which means a second "
            f"decoder: {naming}. Call lib.metering_of_item(item) instead."
        )

    # Each entry is a list of LINES, joined at run time — an embedded newline in a
    # source-code fixture is the kind of thing that survives review and then does not
    # parse. Five of these seven were green against an earlier version of this guard.
    DECODER_SPELLINGS = [
        pytest.param(
            "sneaky_ab.py",
            [
                'm = lib.ddb_to_py(item.get("Metering")) if "Metering" in item else None',
                "out = m if isinstance(m, dict) else {}",
            ],
            id="verbatim",
        ),
        pytest.param(
            "sneaky_ab.py",
            [
                'a = "Meter" + "ing"',
                "m = lib.ddb_to_py(item.get(a)) if a in item else None",
                "out = m if isinstance(m, dict) else {}",
            ],
            id="computed-name",
        ),
        pytest.param(
            "sneaky_ab.py",
            [
                'a = "".join(["Meter", "ing"])',
                "m: dict | None = lib.ddb_to_py(item[a]) if a in item else None",
                "out = m if isinstance(m, dict) else {}",
            ],
            id="annotated-assign",
        ),
        pytest.param(
            "sneaky_ab.py",
            [
                "ok = isinstance(m := lib.ddb_to_py(item[attr]), dict)",
                "out = m if ok else {}",
            ],
            id="walrus",
        ),
        pytest.param(
            "sneaky_ab.py",
            [
                "m = lib.ddb_to_py(item[attr])",
                "out = m if type(m) is dict else {}",
            ],
            id="type-is-dict",
        ),
        pytest.param(
            "sneaky_ab.py",
            [
                'm = getattr(lib, "ddb_to_py")(item[attr])',
                "out = m if isinstance(m, abc.Mapping) else {}",
            ],
            id="getattr",
        ),
        pytest.param(
            "compat/rollup.py",
            [
                'm = lib.ddb_to_py(item.get("Metering")) if "Metering" in item else None',
                "out = m if isinstance(m, dict) else {}",
            ],
            id="subdirectory",
        ),
    ]

    @pytest.mark.parametrize(("filename", "lines"), DECODER_SPELLINGS)
    def test_the_rule_rejects_every_way_of_writing_a_second_decoder(
        self, tmp_path, monkeypatch, filename, lines
    ):
        """Seven spellings, five of which got past an earlier version of this guard.

        Run through the real collector. The point of a capability rule rather than a
        spelling rule is that this list does not have to be complete — but it is the
        record of what was tried, and every entry was green at some point.
        """
        (tmp_path / "lib.py").write_text(
            'X = "Metering"' + "\n" + "m = ddb_to_py(v)" + "\n"
        )
        target = tmp_path / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n")
        monkeypatch.setattr(
            "test_metering_decode_failure.HARNESS", str(tmp_path), raising=False
        )
        files = sorted(Path(tmp_path).rglob("*.py"))
        calling = self._modules_calling("ddb_to_py", files)
        assert set(calling) - {"lib.py"}, (
            f"a decoder written as {filename}: {lines!r} is not seen by the rule"
        )

    def test_the_rule_leaves_a_module_that_decodes_nothing_alone(
        self, tmp_path, monkeypatch
    ):
        """It is about decoding, not about touching a tracking row at all."""
        (tmp_path / "lib.py").write_text("m = ddb_to_py(v)\n")
        (tmp_path / "fine_ab.py").write_text(
            "status = lib.status_of_item(item)\n"
            "secs = lib.sections_of_item(item)\n"
            "read = lib.metering_of_item(item)\n"
        )
        monkeypatch.setattr(
            "test_metering_decode_failure.HARNESS", str(tmp_path), raising=False
        )
        files = sorted(Path(tmp_path).rglob("*.py"))
        assert set(self._modules_calling("ddb_to_py", files)) == {"lib.py"}

    def test_the_named_readers_exist_and_are_what_the_callers_use(self):
        """The rule is only satisfiable because these exist; a rule nobody can
        satisfy gets deleted rather than obeyed."""
        for name in ("metering_of_item", "status_of_item", "sections_of_item"):
            assert callable(getattr(lib, name)), name
        source = Path(HARNESS, "detection_ab_teststudio.py").read_text()
        assert "lib.status_of_item(" in source
        assert "lib.sections_of_item(" in source

    def test_the_named_readers_survive_an_absent_attribute(self):
        """The membership tests inside them are load-bearing, not padding.

        ``ddb_to_py(None)`` raises ``TypeError`` rather than answering ``None``
        (#1223), so ``ddb_to_py(item.get(attr))`` is the crash these readers exist to
        remove. Measured before this test existed: dropping the membership test from
        ``status_of_item`` left the whole suite green.
        """
        for empty in ({}, None, {"PK": {"S": "doc#r/d"}}):
            assert lib.status_of_item(empty) is None, empty
            assert lib.sections_of_item(empty) == [], empty
        assert lib.status_of_item({"ObjectStatus": {"S": "COMPLETED"}}) == "COMPLETED"
        # ...and the underlying primitive really does raise, so the above is not
        # asserting something that would hold either way.
        with pytest.raises(TypeError):
            lib.ddb_to_py(None)

    def test_sections_absent_is_a_real_empty_list_not_a_collapsed_state(self):
        """⚠️ The one place a two-state reader is correct, so it is pinned here.

        ``idp_common/dynamodb/service.py`` writes ``Sections`` only when non-empty,
        so an absent attribute IS a document with no sections. Making this
        three-stated would report a real measurement as unknown — the opposite of
        #1205 — and ``ddb_to_py(None)`` raising is what made the old unguarded call
        crash instead (#1223).
        """
        assert lib.sections_of_item({"PK": {"S": "x"}}) == []
        assert lib.sections_of_item(None) == []
        assert lib.sections_of_item(
            {"Sections": {"L": [{"M": {"a": {"N": "1"}}}]}}
        ) == [{"a": 1.0}]

    def test_a_degenerate_sample_prints_rather_than_raising(self):
        """``t`` is null when the paired deltas have zero spread.

        Found while testing this change rather than caused by it, and fixed here
        because excluding a document shrinks the sample and so makes a degenerate one
        *more* likely — leaving it would mean shipping a new route into a crash. Two
        arms agreeing exactly on every document is enough on its own.
        """
        degenerate = real_corpus_ab._paired_stats([(1.0, 0.0), (2.0, 1.0)], "cost")
        assert degenerate is not None
        assert degenerate["sd"] == 0.0
        assert degenerate["t"] is None, "the premise of this test no longer holds"
        assert lib.format_t(degenerate["t"]) == "—"
        assert lib.format_t(None) == "—"
        # ...and a real one still formats.
        real = real_corpus_ab._paired_stats([(1.0, 0.0), (5.0, 1.0)], "cost")
        assert real is not None and real["t"] is not None
        assert re.fullmatch(r"[+-]\d+\.\d\d", lib.format_t(real["t"]))

    def test_there_is_one_formatter_and_it_takes_the_scalar(self):
        """Two modules that import each other had ``_t`` with different argument
        types — the stats dict in one, the scalar in the other — so either mis-call
        rendered a real t as an em-dash. That is a computed figure reported as a
        missing one, which is this change's own defect class applied to its reporting.
        """
        for module in (real_corpus_ab, per_class_ab, detection_ab):
            assert not hasattr(module, "_t"), module.__name__
        assert lib.format_t(2.5) == "+2.50"
        assert lib.format_t({"t": 2.5}) == "—", (
            "format_t must take the scalar; accepting a dict would make the "
            "mis-call it exists to prevent silent again"
        )

    def test_no_print_site_formats_a_t_statistic_at_all(self):
        """Positional, and over the whole subtree: a ``t`` reaches a format string
        only after ``lib.format_t`` has turned it into a string.

        ⚠️ The earlier version of this rule was two regexes, one of which required an
        ``s_`` prefix, and it missed ``per_class_ab``'s own summary table — two sites
        formatting a ``_paired`` tuple's third element inside a nested f-string, which
        the PR describing the rule cited as already safe. Measured: removing their
        null guards reintroduced the crash with the suite green. So the rule is now
        about the **shape of the access** rather than about the variable's name — any
        subscript by ``'t'`` or by ``2`` appearing inside an f-string interpolation,
        anywhere in harness code including the nested f-strings the old rule could
        not see.
        """
        offenders = []
        for path in sorted(Path(HARNESS).rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.FormattedValue):
                    continue
                for inner in ast.walk(node):
                    if not isinstance(inner, ast.Subscript):
                        continue
                    key = inner.slice
                    if isinstance(key, ast.Constant) and key.value in ("t", 2):
                        offenders.append(
                            f"{path.name}:{inner.lineno}: subscript [{key.value!r}] "
                            "inside an f-string"
                        )
        assert not offenders, (
            "a t statistic looks to be formatted inside an f-string; it is null "
            "whenever the paired deltas have zero spread, so this raises TypeError. "
            f"Call lib.format_t() first and interpolate the string: {offenders}"
        )

    def test_that_rule_sees_the_nested_f_string_the_regex_missed(self):
        """The exact spelling from ``per_class_ab``'s summary table."""
        probe = tmp = None  # noqa: F841 - documents that no fixture is needed
        source = (
            "print(\n"
            '    f"{cls:28} "\n'
            "    f\"{(f'{acc[2]:+.2f}' if acc and acc[2] is not None else '-'):>6} \"\n"
            ")\n"
        )
        found = [
            inner.lineno
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FormattedValue)
            for inner in ast.walk(node)
            if isinstance(inner, ast.Subscript)
            and isinstance(inner.slice, ast.Constant)
            and inner.slice.value in ("t", 2)
        ]
        assert found, "the rule cannot see a subscript nested inside an inner f-string"
        del probe, tmp

    def test_neither_former_decoder_survives_in_any_form(self):
        """The two function bodies must go through the shared reader.

        Checked on the source rather than on behaviour because a re-introduced
        decoder would be a *new* function with its own name, which no behavioural
        test of the two existing ones would reach.
        """
        for module in ("real_corpus_ab.py", "detection_ab_teststudio.py"):
            source = Path(HARNESS, module).read_text()
            assert "metering_of_item" in source, module
            assert not re.search(r"except ValueError:\s*\n\s*m = None", source), (
                f"{module} still swallows a JSON decode error into a sentinel"
            )
