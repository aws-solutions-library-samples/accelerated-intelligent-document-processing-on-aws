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
6. A third local decoder fails a guard — two independent rules, because the first
   one alone was walked past: the ``"Metering"`` literal is confined to ``lib.py``,
   and so is deciding whether a ``ddb_to_py`` result is a map. The second is what
   catches a decoder that builds the attribute name at runtime. Their residual is
   written out where they are implemented rather than left to be discovered.

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

One adjacent defect is **filed rather than fixed**: ``lib.ddb_to_py(None)`` raises
``TypeError`` instead of answering ``None``, and ``detection_ab`` calls it unguarded on
``ObjectStatus`` and on ``Sections`` — the second is genuinely optional on a tracking
row, so a document with no sections takes the whole analysis down. Different class,
different function, so the row fixture here carries both attributes rather than the
change growing to cover it.
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

    ``ObjectStatus`` and ``Sections`` are present because ``detection_ab`` reads both
    through ``lib.ddb_to_py`` with no guard, and ``ddb_to_py(None)`` raises
    ``TypeError`` rather than answering ``None``. A real completed row carries both.
    That fragility is a separate defect and is filed rather than fixed here — see the
    module docstring — so this fixture stays a realistic row rather than a minimal one.
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
        that would move is the arm mean, which is printed rather than reported."""
        _report, printed = self._run(tmp_path, monkeypatch, capsys)
        line = next(
            ln for ln in printed.splitlines() if ln.strip().startswith("inputTokens")
        )
        # (1000 + 3000) / 2 = 2,000. Including the broken document's zero would give
        # 4000/3 = 1,333.
        assert "2,000" in line, line
        assert "1,333" not in line

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
        # 2,000 is the mean over the two readable rows; 1,333 would include the zero.
        assert "2,000" in line, line
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
        assert "2,000" in line, line


# --------------------------------------------------------------------------- #
# A third local decoder cannot be written
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestTheDecoderCannotBeDuplicated:
    """The rule is positional and about WHERE the literal may appear, not about
    recognising a bad shape.

    An earlier guard in this repository enumerated the ways a value could be misused
    and a spelling nobody had enumerated walked past it. So this does not try to
    recognise a decoder: reading the attribute at all requires naming it, and the
    name may be written in exactly one module.

    Two **independent** rules, because one was not enough and the gap was found by
    trying rather than by reading: naming the attribute is caught by the first, and a
    *computed* name (``"Meter" + "ing"``) walks past it and is caught by the second,
    which is about decoding rather than about naming. Each rule's reach is written
    where it is implemented.

    ⚠️ **What neither catches**, stated because a guard whose limits are unstated is
    how the last one in this repository failed: a decoder that neither names the
    attribute as a literal nor tests the decoded value with ``isinstance(..., dict)``
    — for example one reaching it through a constant imported from another module and
    checking the type some other way. No ast rule closes that, and the behavioural
    tests above are the primary cover; these two are the cheap extra that name the
    file and line for the shapes somebody writes by accident.
    """

    @staticmethod
    def _modules_naming_the_attribute() -> dict[str, list[int]]:
        out = {}
        for path in sorted(Path(HARNESS).glob("*.py")):
            lines = [
                node.lineno
                for node in ast.walk(ast.parse(path.read_text()))
                if isinstance(node, ast.Constant) and node.value == "Metering"
            ]
            if lines:
                out[path.name] = lines
        return out

    def test_only_lib_names_the_metering_attribute(self):
        naming = self._modules_naming_the_attribute()
        assert "lib.py" in naming, (
            "lib.py no longer names the attribute, so this guard is measuring nothing"
        )
        assert set(naming) == {"lib.py"}, (
            "a second module names the `Metering` attribute, which means a second "
            f"decoder: {naming}. Call lib.metering_of_item(item) instead."
        )

    @staticmethod
    def _modules_decoding_into_a_dict_test() -> dict[str, list[int]]:
        """Modules that call ``ddb_to_py`` and then ask whether the result is a dict.

        The second rule, and it is about the decoding rather than about the attribute
        name — which is what lets it catch a decoder that builds the name at runtime.
        A metering decoder has to do both of these things: get a value out of
        ``ddb_to_py`` and decide whether it came back as a map. Reading
        ``ObjectStatus`` or ``Sections`` through ``ddb_to_py`` does neither, so the
        rule does not touch the legitimate callers.
        """

        def _calls_the_decoder(expression: ast.AST) -> bool:
            """Anywhere in the assigned expression, not just at its root.

            ⚠️ This is the part to keep whole-subtree. Requiring the call to *be* the
            assigned value missed ``m = lib.ddb_to_py(item.get(attr)) if attr in item
            else None`` — an ``IfExp``, and the shape the original decoders were
            written in — while catching the same decoder written without the
            conditional. Measured: the conditional form walked past and the plain
            form did not, which is a guard sensitive to spelling rather than to what
            the code does.
            """
            return any(
                isinstance(node, ast.Call)
                and isinstance(fn := node.func, (ast.Name, ast.Attribute))
                and (fn.id if isinstance(fn, ast.Name) else fn.attr) == "ddb_to_py"
                for node in ast.walk(expression)
            )

        out = {}
        for path in sorted(Path(HARNESS).glob("*.py")):
            tree = ast.parse(path.read_text())
            decoded = {
                target.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Assign) and _calls_the_decoder(node.value)
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            lines = [
                node.lineno
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "isinstance"
                and len(node.args) == 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in decoded
                and isinstance(node.args[1], ast.Name)
                and node.args[1].id == "dict"
            ]
            if lines:
                out[path.name] = lines
        return out

    def test_only_lib_decides_whether_a_decoded_value_is_a_metering_map(self):
        naming = self._modules_decoding_into_a_dict_test()
        assert "lib.py" in naming, (
            "lib.py no longer decodes into a dict test, so this rule measures nothing"
        )
        assert set(naming) == {"lib.py"}, (
            "a second module decodes a DynamoDB attribute and asks whether it is a "
            f"map, which is a metering decoder however it names the attribute: {naming}"
        )

    def test_the_second_rule_sees_a_computed_attribute_name(
        self, tmp_path, monkeypatch
    ):
        """The spelling that walks past the first rule.

        Found by trying it rather than by reading: the first rule was written, the
        defect was reintroduced with ``"Meter" + "ing"``, and the whole suite stayed
        green. That is the same failure the ``_cost`` guard in #1146 had.
        """
        (tmp_path / "lib.py").write_text("m = ddb_to_py(x)\nok = isinstance(m, dict)\n")
        # Written as a conditional expression, which is how both original decoders
        # were written and which an earlier version of this rule did not see.
        (tmp_path / "sneaky_ab.py").write_text(
            'attr = "Meter" + "ing"\n'
            "m = lib.ddb_to_py(item.get(attr)) if attr in item else None\n"
            "out = m if isinstance(m, dict) else {}\n"
        )
        monkeypatch.setattr(
            "test_metering_decode_failure.HARNESS", str(tmp_path), raising=False
        )
        # The first rule does not see it...
        assert set(self._modules_naming_the_attribute()) == set()
        # ...and the second does.
        assert set(self._modules_decoding_into_a_dict_test()) == {
            "lib.py",
            "sneaky_ab.py",
        }

    def test_the_second_rule_leaves_the_legitimate_decoders_alone(
        self, tmp_path, monkeypatch
    ):
        """Reading a non-metering attribute is not a metering decoder."""
        (tmp_path / "lib.py").write_text("m = ddb_to_py(x)\nok = isinstance(m, dict)\n")
        (tmp_path / "fine_ab.py").write_text(
            "status = lib.ddb_to_py(item.get(status_attr))\n"
            "secs = lib.ddb_to_py(item.get(sections_attr)) or []\n"
            "ok = isinstance(secs, list)\n"
        )
        monkeypatch.setattr(
            "test_metering_decode_failure.HARNESS", str(tmp_path), raising=False
        )
        assert set(self._modules_decoding_into_a_dict_test()) == {"lib.py"}

    def test_the_guard_sees_a_second_module_that_names_it(self, tmp_path, monkeypatch):
        """Run through the real collector over a synthetic harness directory."""
        (tmp_path / "lib.py").write_text('X = "Metering"\n')
        (tmp_path / "sneaky_ab.py").write_text(
            'm = lib.ddb_to_py(item.get("Metering"))\n'
        )
        monkeypatch.setattr(
            "test_metering_decode_failure.HARNESS", str(tmp_path), raising=False
        )
        naming = self._modules_naming_the_attribute()
        assert set(naming) == {"lib.py", "sneaky_ab.py"}, naming

    def test_a_degenerate_sample_prints_rather_than_raising(self):
        """``t`` is null when the paired deltas have zero spread.

        Found by this change rather than caused by it, and fixed here rather than
        filed because excluding a document shrinks the sample and so makes a
        degenerate one *more* likely — leaving the crash would mean shipping a new
        route into it. Two arms agreeing exactly on every document is enough on its
        own. ``per_class_ab``'s summary table already printed ``—``; four other sites
        formatted it unconditionally and raised ``TypeError`` after the analysis had
        finished computing.
        """
        degenerate = real_corpus_ab._paired_stats([(1.0, 0.0), (2.0, 1.0)], "cost")
        assert degenerate is not None
        assert degenerate["sd"] == 0.0
        assert degenerate["t"] is None, "the premise of this test no longer holds"
        assert real_corpus_ab._t(degenerate) == "—"
        assert per_class_ab._t(None) == "—"
        # ...and a real one still formats.
        real = real_corpus_ab._paired_stats([(1.0, 0.0), (5.0, 1.0)], "cost")
        assert real is not None and real["t"] is not None
        assert re.fullmatch(r"[+-]\d+\.\d\d", real_corpus_ab._t(real))
        assert re.fullmatch(r"[+-]\d+\.\d\d", per_class_ab._t(2.5))

    def test_no_print_site_formats_a_t_statistic_directly(self):
        """The rule positionally: a ``t`` reaches a format string only through ``_t``.

        The guard that matters is not "does this one line handle None" — the crash
        existed in four places while a fifth in the same file handled it. So no
        harness module may format ``['t']`` or a ``_paired`` tuple's third element
        inside an f-string.
        """
        offenders = []
        for path in sorted(Path(HARNESS).glob("*.py")):
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r"\{s?t?_?\w*\[['\"]t['\"]\]:", line) or re.search(
                    r"\{s_\w+\[2\]:", line
                ):
                    offenders.append(f"{path.name}:{n}: {line.strip()}")
        assert not offenders, (
            "a t statistic is formatted directly; it is null whenever the paired "
            f"deltas have zero spread, so this raises TypeError: {offenders}"
        )

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
