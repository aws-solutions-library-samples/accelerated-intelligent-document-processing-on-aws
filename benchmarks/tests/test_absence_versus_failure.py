# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A measurement that never happened must not read as one that came back zero (#1079).

What was wrong
--------------
Six readers in the benchmark harness returned the same value for "there was nothing
there" and for "I could not tell". ``lib.get_json`` answered ``None`` for a missing
object and an undecryptable one; ``lib.doc_metering`` answered ``{}`` for an unmetered
run and for a DynamoDB failure, and an empty metering map prices to $0.00. The failure
that made the class visible was a release stack whose KMS key entered pending deletion:
every object in its output bucket was present, listable and undecryptable, so
``GetObject`` answered ``KMS.KMSInvalidStateException`` and the grid read as one that
had recorded nothing.

The direction of the bias is the reason this matters more than an ordinary silent
failure. These artifacts are published, and an unread measurement recorded as zero
drags an average toward zero — which makes a configuration look cheaper, faster or less
accurate than it is, in a document a reader takes at face value.

What these tests pin
--------------------
1. ``lib.Reading`` has THREE states and cannot be collapsed to two: ``__bool__``
   raises, ``value`` raises unless the read is present, and ``value_or`` substitutes
   for an absence but not for a failure. Zero is not the sentinel for either empty
   state, because zero is a legitimate value for most of these metrics.
2. ``read_json`` calls a 404 an absence and everything else — access denied, a KMS key
   in pending deletion, a body that is not JSON — a failure.
3. ``read_sections`` reports objects that were LISTED and then would not read, instead
   of dropping them, and refuses to present an empty list when the listing itself
   failed.
4. ``read_metering`` distinguishes a tracking row with no ``Metering`` attribute (a real
   zero, priced) from a missing row and from a read failure (neither priced).
5. ``score_doc`` refuses to price a metering row it did not read: ``cost`` is null and
   ``cost_unread`` names the state. A genuinely unmetered run still prices to 0.0.
6. A document whose sections could not all be read contributes NO metric key at all —
   in particular no ``calibration_curve``, whose presence-and-null is the established
   signal for "measured, nothing to join".
7. ``cell_stats`` counts the excluded rows, so a mean over a thinned sample is visibly
   thinned rather than merely smaller.
8. ``calibration_study`` has an ``unreadable`` bucket, so an undecryptable grid is not
   counted as one that emitted no confidence.
9. ``augment_summary`` counts unreadable rows per row, not once for the first empty
   prefix, and leaves them un-augmented.
10. ``_missing_metric_notes`` reports a missing metric in both directions.
11. The test bootstrap here fails rather than skipping when the harness cannot be
    found, and works from any working directory.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from harness_import import HARNESS, harness_module

lib = harness_module("lib")
analyze = harness_module("analyze")
aggregate = harness_module("aggregate")


# --------------------------------------------------------------------------- #
# Fakes. Small on purpose: the point is which STATE the reader reports, and a
# moto bucket cannot be put into "listable but undecryptable" without a real KMS
# key in pending deletion.
# --------------------------------------------------------------------------- #
def _client_error(code, status=400, op="GetObject"):
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        op,
    )


KMS_PENDING = _client_error("KMS.KMSInvalidStateException")
NO_SUCH_KEY = _client_error("NoSuchKey", 404)


class _Body:
    def __init__(self, raw):
        self._raw = raw

    def read(self):
        return self._raw


class FakeS3:
    """``get_object`` answers per key: bytes, or an exception to raise."""

    def __init__(self, objects, listing=None, listing_error=None):
        self.objects = objects
        self.listing = listing if listing is not None else list(objects)
        self.listing_error = listing_error

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3 casing
        if Key not in self.objects:
            raise NO_SUCH_KEY
        value = self.objects[Key]
        if isinstance(value, Exception):
            raise value
        return {"Body": _Body(value)}

    def get_paginator(self, _name):
        outer = self

        class _Paginator:
            def paginate(self, Bucket, Prefix):  # noqa: N803 - boto3 casing
                if outer.listing_error:
                    raise outer.listing_error
                return [
                    {
                        "Contents": [
                            {"Key": k} for k in outer.listing if k.startswith(Prefix)
                        ]
                    }
                ]

        return _Paginator()


@pytest.fixture
def s3(monkeypatch):
    """Install a FakeS3 factory. ``lib.s3()`` is cached, so patch the accessor."""

    def install(objects, listing=None, listing_error=None):
        fake = FakeS3(objects, listing, listing_error)
        monkeypatch.setattr(lib, "s3", lambda: fake)
        return fake

    return install


# --------------------------------------------------------------------------- #
# 1. The Reading contract — three states, and no way back to two
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestReadingIsThreeStated:
    def test_the_three_states_are_distinguishable(self):
        present = lib.Reading.present({"a": 1})
        absent = lib.Reading.absent("no such object")
        failed = lib.Reading.failed("KMS key pending deletion")
        assert (present.is_present, present.is_absent, present.is_failed) == (
            True,
            False,
            False,
        )
        assert (absent.is_present, absent.is_absent, absent.is_failed) == (
            False,
            True,
            False,
        )
        assert (failed.is_present, failed.is_absent, failed.is_failed) == (
            False,
            False,
            True,
        )
        assert [r.state for r in (present, absent, failed)] == [
            "present",
            "absent",
            "failed",
        ]

    def test_zero_and_empty_are_legitimate_present_values(self):
        """The reason zero cannot be the sentinel: it is a real reading."""
        for value in (0, 0.0, {}, [], ""):
            read = lib.Reading.present(value)
            assert read.is_present
            assert read.value == value
            assert read.value_or("substituted") == value

    def test_truth_testing_a_reading_is_an_error(self):
        """``if reading:`` is exactly the two-state test that lost the distinction."""
        for read in (
            lib.Reading.present({"a": 1}),
            lib.Reading.absent(),
            lib.Reading.failed("boom"),
        ):
            with pytest.raises(TypeError, match="three states"):
                bool(read)
            with pytest.raises(TypeError):
                _ = read or {}
            with pytest.raises(TypeError):
                if read:  # noqa: SIM103
                    pass

    def test_value_is_unavailable_unless_the_read_happened(self):
        assert lib.Reading.present(7).value == 7
        for read in (lib.Reading.absent("gone"), lib.Reading.failed("boom")):
            with pytest.raises(lib.Unread):
                _ = read.value

    def test_value_or_substitutes_for_absence_and_refuses_for_failure(self):
        assert lib.Reading.absent("gone").value_or({}) == {}
        with pytest.raises(lib.Unread, match="do not substitute"):
            lib.Reading.failed("KMS pending deletion").value_or({})

    def test_a_reading_is_not_the_thing_it_wraps(self):
        """It has none of a dict's methods, so misuse fails the type check too."""
        read = lib.Reading.present({"a": 1})
        for attr in ("get", "items", "keys", "__getitem__"):
            assert not hasattr(read, attr)

    def test_price_metering_refuses_a_reading(self):
        """`reportArgumentType` is disabled here, so this is the guard that catches it."""
        with pytest.raises(TypeError, match="three states"):
            lib.price_metering(lib.Reading.present({}))


# --------------------------------------------------------------------------- #
# 2. Site 1 — lib.read_json, the root of the class
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestReadJson:
    def test_a_missing_object_is_absent(self, s3):
        s3({})
        read = lib.read_json("b", "k.json")
        assert read.is_absent
        assert read.value_or(None) is None

    def test_an_undecryptable_object_is_a_failure_not_an_absence(self, s3):
        """The KMS-pending-deletion case: present, listable, and not readable."""
        s3({"k.json": KMS_PENDING})
        read = lib.read_json("b", "k.json")
        assert read.is_failed
        assert not read.is_absent
        assert "KMSInvalidState" in str(read.error)

    def test_access_denied_is_a_failure(self, s3):
        s3({"k.json": _client_error("AccessDenied", 403)})
        assert lib.read_json("b", "k.json").is_failed

    def test_a_body_that_is_not_json_is_a_failure(self, s3):
        """The object exists; it is not readable as a result. Not an absence."""
        s3({"k.json": b"<html>error</html>"})
        read = lib.read_json("b", "k.json")
        assert read.is_failed
        assert "not JSON" in str(read.error)

    def test_a_readable_object_is_present(self, s3):
        s3({"k.json": b'{"a": 1}'})
        assert lib.read_json("b", "k.json").value == {"a": 1}

    def test_a_missing_bucket_is_a_failure(self, s3):
        """It says nothing about whether the objects existed."""
        s3({"k.json": _client_error("NoSuchBucket", 404)})
        assert lib.read_json("b", "k.json").is_failed


# --------------------------------------------------------------------------- #
# 3. Site 1b — read_sections surfaces what it could not read
# --------------------------------------------------------------------------- #
SEC = b'{"document_class": {"type": "bank"}, "inference_result": {}}'


@pytest.mark.unit
class TestReadSections:
    def test_a_listed_object_that_will_not_read_is_counted(self, s3):
        s3(
            {
                "d/sections/1/result.json": SEC,
                "d/sections/2/result.json": KMS_PENDING,
            }
        )
        read = lib.read_sections("b", "d/")
        assert len(read.sections) == 1
        assert read.unreadable == 1
        assert read.complete is False
        assert "unreadable" in read.why

    def test_a_genuinely_empty_prefix_is_complete(self, s3):
        s3({})
        read = lib.read_sections("b", "d/")
        assert read.sections == []
        assert read.unreadable == 0
        assert read.complete is True
        assert read.why == ""

    def test_a_failed_listing_does_not_present_an_empty_list(self, s3):
        s3({}, listing_error=_client_error("AccessDenied", 403, "ListObjectsV2"))
        read = lib.read_sections("b", "d/")
        assert read.complete is False
        assert read.listing_error
        with pytest.raises(lib.Unread, match="listing failed"):
            _ = read.sections

    def test_truth_testing_a_section_read_is_an_error(self, s3):
        s3({})
        with pytest.raises(TypeError, match="SectionRead"):
            bool(lib.read_sections("b", "d/"))

    def test_a_parsed_non_object_counts_as_unreadable(self, s3):
        """The old walk dropped anything falsy, which silently included this."""
        s3({"d/sections/1/result.json": b"null"})
        read = lib.read_sections("b", "d/")
        assert read.sections == []
        assert read.unreadable == 1


# --------------------------------------------------------------------------- #
# 4. Site 3 — read_metering, and the $0.00 that was not a measurement
# --------------------------------------------------------------------------- #
class FakeDDB:
    def __init__(self, item=None, error=None):
        self.item = item
        self.error = error

    def get_item(self, **_kw):
        if self.error:
            raise self.error
        return {"Item": self.item} if self.item is not None else {}


@pytest.fixture
def ddb(monkeypatch):
    def install(item=None, error=None):
        fake = FakeDDB(item, error)
        monkeypatch.setattr(lib, "ddb", lambda: fake)
        return fake

    return install


METERED = {
    "Metering": {
        "M": {"Extraction/bedrock/x": {"M": {"inputTokens": {"N": "100"}}}},
    }
}


@pytest.mark.unit
class TestReadMetering:
    def test_a_row_with_no_metering_attribute_is_a_real_zero(self, ddb):
        """It is a measurement: this document metered nothing. It prices to $0.00."""
        ddb(item={"PK": {"S": "doc#r/d"}})
        read = lib.read_metering("t", "r", "d")
        assert read.is_present
        assert read.value == {}
        assert lib.price_metering(read.value)[0] == 0.0

    def test_no_tracking_row_at_all_is_an_absence(self, ddb):
        ddb(item=None)
        read = lib.read_metering("t", "r", "d")
        assert read.is_absent
        assert not read.is_present

    def test_a_table_read_failure_is_a_failure(self, ddb):
        """Was ``{}``, which priced to $0.00 — indistinguishable from an unmetered run."""
        ddb(error=_client_error("ResourceNotFoundException", 400, "GetItem"))
        read = lib.read_metering("t", "r", "d")
        assert read.is_failed
        assert "ResourceNotFound" in str(read.error)

    def test_metering_that_will_not_decode_is_a_failure(self, ddb):
        ddb(item={"Metering": {"S": "{not json"}})
        assert lib.read_metering("t", "r", "d").is_failed

    @pytest.mark.parametrize(
        "attribute",
        [
            {"L": []},  # a list: ddb_to_py returns [], not a map
            {"BOOL": False},
            {"N": "0"},
            {"B": b"x"},  # a type ddb_to_py does not handle at all, so None
        ],
        ids=["list", "bool", "number", "binary"],
    )
    def test_metering_that_decodes_to_something_other_than_a_map_is_a_failure(
        self, ddb, attribute
    ):
        """Not ``present({})``, which would price to $0.00.

        Reachable without a corrupt table: ``ddb_to_py`` returns ``None`` for any
        attribute type outside ``M/N/S/L/BOOL``, and ``[]`` for a list, so an
        attribute written with the wrong type lands here rather than on the
        JSON-decode branch above.
        """
        ddb(item={"Metering": attribute})
        read = lib.read_metering("t", "r", "d")
        assert read.is_failed, f"{attribute} must not read as a priceable zero"
        assert "not a map" in str(read.error)

    def test_a_readable_metering_map_is_present(self, ddb):
        ddb(item=METERED)
        read = lib.read_metering("t", "r", "d")
        assert read.is_present
        assert "Extraction/bedrock/x" in read.value


# --------------------------------------------------------------------------- #
# 5. Site 3b — the cost path refuses to price what it did not read
# --------------------------------------------------------------------------- #
@pytest.fixture
def scored_doc(monkeypatch):
    """``score_doc`` with the three reads stubbed, one knob each."""

    def run(metering_read, section_read=None, truth=None):
        monkeypatch.setattr(
            lib,
            "doc_row",
            lambda *a, **k: {"ObjectStatus": "COMPLETED", "PageCount": 2},
        )
        monkeypatch.setattr(lib, "read_metering", lambda *a, **k: metering_read)
        monkeypatch.setattr(
            lib,
            "read_sections",
            lambda *a, **k: (
                section_read if section_read is not None else lib.SectionRead([])
            ),
        )
        return analyze.score_doc("bucket", "table", "run", "doc", truth)

    return run


@pytest.mark.unit
class TestCostRefusesAnUnreadMeteringRow:
    def test_an_unmetered_run_prices_to_zero(self, scored_doc):
        row = scored_doc(lib.Reading.present({}))
        assert row["cost"] == 0.0
        assert row["cost_unread"] is None
        assert row["tokens"] == {}

    def test_a_metered_run_is_priced(self, scored_doc):
        row = scored_doc(
            lib.Reading.present(
                {"OCR/textract/analyze_document": {"pages": 3}},
            )
        )
        assert row["cost"] is not None
        assert row["cost_unread"] is None

    @pytest.mark.parametrize(
        ("read", "state"),
        [
            (lib.Reading.failed("KMS.KMSInvalidStateException"), "failed"),
            (lib.Reading.absent("no tracking row"), "absent"),
        ],
    )
    def test_an_unread_metering_row_yields_no_cost_at_all(
        self, scored_doc, read, state
    ):
        """The whole point: not 0.0, which a reader of an artifact would believe."""
        row = scored_doc(read)
        assert row["cost"] is None, "an unread metering row must not price to a number"
        assert row["cost_unread"] and row["cost_unread"].startswith(state)
        # The token counts come from the same map and are just as unmeasured.
        assert row["tokens"] is None
        assert row["tokens_by_phase"] is None
        assert row["cost_by_phase"] is None
        assert row["cost_by_key"] is None

    def test_an_unread_cost_does_not_drag_a_cell_mean_toward_zero(self):
        """A null drops out of `_stats`; a zero would be averaged in as a measurement."""
        rows = [
            {"cell": "c", "success": True, "cost": 0.30},
            {"cell": "c", "success": True, "cost": 0.30},
            {"cell": "c", "success": True, "cost": None, "cost_unread": "failed: boom"},
        ]
        stats = aggregate.cell_stats(rows)["c"]
        assert stats["cost"]["mean"] == 0.30
        assert stats["cost"]["n"] == 2
        assert stats["n_success"] == 3
        # ...and the gap between the two is not left for the reader to spot.
        assert stats["n_cost_unread"] == 1

    def test_the_unread_keys_reach_the_csv(self):
        """`DictWriter(extrasaction="ignore")` drops any row key absent from the list."""
        for key in (
            "cost_unread",
            "sections_unreadable",
            "sections_unread",
            "eval_unread",
        ):
            assert key in aggregate.CSV_COLS, key


# --------------------------------------------------------------------------- #
# 6. An unread document contributes no metric, and no null calibration key
# --------------------------------------------------------------------------- #
TRUTH = {
    "seq_ids": ["SEQ00001"],
    "rows_typed": {"SEQ00001": {"Amount": 1.0}},
    "list_key": "Transactions",
    "fields": {},
    "expected_sections": 1,
}


@pytest.mark.unit
class TestAnUnreadDocumentReportsNoMetrics:
    @pytest.mark.parametrize("truth", [TRUTH, None], ids=["synthetic", "reference"])
    def test_only_the_bookkeeping_keys_are_returned(self, scored_doc, truth):
        unread = lib.SectionRead([], unreadable=2, errors=("1/result.json: KMS",))
        row = scored_doc(lib.Reading.present({}), section_read=unread, truth=truth)
        assert row["sections_unreadable"] == 2
        assert "KMS" in row["sections_unread"]
        # No metric key at all, and above all no `calibration_curve`: a key that is
        # present and null already means "measured, nothing to join" here.
        assert "calibration_curve" not in row
        assert "calibration_observations" not in row
        for metric in (
            "completeness_recall",
            "cell_accuracy",
            "conf_coverage",
            "weighted_accuracy",
            "mean_confidence",
        ):
            assert metric not in row, metric

    @pytest.mark.parametrize("truth", [TRUTH, None], ids=["synthetic", "reference"])
    def test_no_metric_key_can_drift_into_the_unread_path(self, s3, truth):
        """Derived from the happy path rather than pinned to a list.

        A metric added to a scorer and not to this comparison is what would put a
        null-valued metric back into an unread row, so the measured key set is read
        off the scorer itself: the unread row must share NO key with it.
        """
        s3({"d/sections/1/result.json": SEC})
        measured = set(
            analyze.score_synthetic("b", "d/", truth)
            if truth
            else analyze.score_reference("b", "d/")
        )
        unread = set(
            analyze.unread_sections_row(
                lib.SectionRead([], unreadable=1, errors=("x",))
            )
        )
        assert unread == {"sections_unreadable", "sections_unread"}
        assert not (unread & measured), "an unread row must carry no measured key"
        # ...and the happy path really does produce the keys whose absence the unread
        # row relies on, so "absent means never measured" is a live distinction.
        assert "conf_coverage" in measured
        if truth:
            assert "calibration_curve" in measured

    def test_an_unread_document_falls_out_of_the_quality_stats(self):
        rows = [
            {"cell": "c", "success": True, "cell_accuracy": 0.9},
            {
                "cell": "c",
                "success": True,
                "sections_unreadable": 1,
                "sections_unread": "KMS",
            },
        ]
        stats = aggregate.cell_stats(rows)["c"]
        assert stats["cell_accuracy"]["n"] == 1
        assert stats["cell_accuracy"]["mean"] == 0.9
        assert stats["n_sections_unread"] == 1

    def test_a_failed_evaluation_report_is_named_not_scored_as_clean(self, s3):
        """A report that would not read is not a run with zero parse failures."""
        s3(
            {
                "d/sections/1/result.json": SEC,
                "d/evaluation/results.json": KMS_PENDING,
            },
            listing=["d/sections/1/result.json"],
        )
        row = analyze.score_reference("b", "d/")
        assert row["eval_unread"]
        assert row["weighted_accuracy"] is None
        assert row["parse_failures"] is None

    def test_an_absent_evaluation_report_is_not_flagged(self, s3):
        s3({"d/sections/1/result.json": SEC}, listing=["d/sections/1/result.json"])
        row = analyze.score_reference("b", "d/")
        assert row["eval_unread"] is None


# --------------------------------------------------------------------------- #
# 7. Site 2 — calibration_study's from_s3 path had no read-error probe at all
# --------------------------------------------------------------------------- #
def _summary(tmp_path, rows, stack="stack-x"):
    d = tmp_path / "coresynth__arm"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "summary.json"
    p.write_text(json.dumps({"meta": {"stack": stack}, "rows": rows}))
    return str(p)


CALIB_ROW = {
    "cell": "c",
    "doc": "kv_form",
    "run_id": "r1",
    "success": True,
    "resolved": {"assessment": "separate", "confidence_model": "nova_lite"},
}


@pytest.mark.unit
class TestCalibrationStudyCountsUnreadRows:
    @pytest.fixture
    def study(self, monkeypatch, tmp_path):
        def run(section_read, truth_dir=None):
            monkeypatch.setattr(
                aggregate, "_resolve_output_bucket", lambda _stack: "bucket"
            )
            monkeypatch.setattr(
                aggregate, "_truth_for", lambda *_a: {"rows_typed": TRUTH["rows_typed"]}
            )
            monkeypatch.setattr(lib, "read_sections", lambda *a, **k: section_read)
            path = _summary(tmp_path, [dict(CALIB_ROW)])
            return aggregate.calibration_study([path], corpus_dir=truth_dir)

        return run

    def test_an_unreadable_grid_is_not_counted_as_having_no_confidence(self, study):
        report = study(lib.SectionRead([], unreadable=1, errors=("1: KMS pending",)))
        assert report["skipped"]["unreadable"] == 1
        assert report["skipped"]["no_confidence"] == 0
        assert report["skipped"]["no_joinable_cell"] == 0
        assert report["unreadable_errors"]

    def test_a_failed_listing_is_counted_too(self, study):
        report = study(lib.SectionRead([], listing_error="AccessDenied"))
        assert report["skipped"]["unreadable"] == 1

    def test_a_genuinely_unassessed_run_is_still_no_confidence(self, study):
        """The bucket that is a fact about the run keeps working."""
        report = study(lib.SectionRead([{"inference_result": {}}]))
        assert report["skipped"]["unreadable"] == 0
        assert report["skipped"]["no_confidence"] == 1

    def test_the_printed_report_names_the_unread_rows(self, study, capsys):
        study(lib.SectionRead([], unreadable=1, errors=("1: KMS pending",)))
        out = capsys.readouterr().out
        assert "UNREAD" in out
        assert "KMS pending" in out

    def test_an_arm_that_pooled_anything_carries_unread_on_its_own_row(
        self, monkeypatch, tmp_path, capsys
    ):
        """The grid total is not enough: the arm's own figures are the provisional ones.

        An arm with no readable row at all is dropped from the table entirely (nothing
        pooled), so the case that needs the marker is the MIXED one — some rows read,
        some did not — and that is the row a reader takes numbers off.
        """
        monkeypatch.setattr(aggregate, "_resolve_output_bucket", lambda _s: "bucket")
        monkeypatch.setattr(
            aggregate, "_truth_for", lambda *_a: {"rows_typed": TRUTH["rows_typed"]}
        )
        readable = lib.SectionRead([{"inference_result": {}}])
        unread = lib.SectionRead([], unreadable=1, errors=("2: KMS pending",))
        reads = {"r1/a/": readable, "r1/b/": unread}
        monkeypatch.setattr(lib, "read_sections", lambda _b, prefix: reads[prefix])
        monkeypatch.setattr(
            analyze,
            "score_calibration",
            lambda *_a, **_k: {"calibration_curve": {"n": 1}},
        )
        monkeypatch.setattr(
            analyze,
            "pool_calibration",
            lambda payloads: (
                {
                    "observations": 10,
                    "correct": 9,
                    "accuracy": 0.9,
                    "ece": 0.01,
                    "ece_mean_conf": 0.01,
                    "auroc": None,
                    "auroc_unbinned": None,
                    "brier": 0.01,
                    "bin_coverage": 1,
                    "degenerate": False,
                    "overconfident": False,
                    "undiscriminating": False,
                }
                if payloads
                else None
            ),
        )
        path = _summary(
            tmp_path,
            [
                {**CALIB_ROW, "run_id": "r1", "doc": "a"},
                {**CALIB_ROW, "run_id": "r1", "doc": "b"},
            ],
        )
        report = aggregate.calibration_study([path], corpus_dir=str(tmp_path))
        (arm,) = report["arms"]
        assert arm["excluded"]["unreadable"] == 1
        assert arm["runs"] == 1
        arm_line = next(
            line
            for line in capsys.readouterr().out.splitlines()
            if line.startswith("separate|")
        )
        assert "UNREAD 1" in arm_line, arm_line


# --------------------------------------------------------------------------- #
# 8. Site 4 — the backfill asks every row, not just the first empty prefix
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestAugmentCountsEveryUnreadableRow:
    @pytest.fixture
    def augment(self, monkeypatch, tmp_path):
        def run(reads_by_prefix, rows):
            monkeypatch.setattr(
                aggregate, "_resolve_output_bucket", lambda _stack: "bucket"
            )
            monkeypatch.setattr(aggregate, "_truth_for", lambda *_a: None)
            monkeypatch.setattr(
                lib, "read_sections", lambda _b, prefix: reads_by_prefix[prefix]
            )
            path = _summary(tmp_path, rows)
            return path, aggregate.augment_summary(path, str(tmp_path), dry_run=True)

        return run

    def test_a_grid_where_one_section_reads_and_the_next_does_not(self, augment):
        """The partiality `_first_read_error` documented: it reported no error here."""
        rows = [
            {**CALIB_ROW, "run_id": "r1", "doc": "a"},
            {**CALIB_ROW, "run_id": "r1", "doc": "b"},
        ]
        reads = {
            "r1/a/": lib.SectionRead([]),  # genuinely empty
            "r1/b/": lib.SectionRead([], unreadable=1, errors=("b: KMS",)),
        }
        path, (updated, skipped) = augment(reads, rows)
        assert (updated, skipped) == (0, 2)
        summary = json.load(open(path))
        # Untouched on disk (dry run), and neither row gained a calibration key.
        for row in summary["rows"]:
            assert "calibration_curve" not in row

    def test_an_unreadable_row_is_not_counted_as_having_no_sections(
        self, augment, capsys
    ):
        rows = [{**CALIB_ROW, "run_id": "r1", "doc": "b"}]
        reads = {"r1/b/": lib.SectionRead([], unreadable=3, errors=("b: KMS pending",))}
        augment(reads, rows)
        out = capsys.readouterr().out
        assert "unreadable" in out
        assert "KMS pending" in out
        assert "no_sections" not in out

    def test_a_genuinely_empty_prefix_is_still_no_sections(self, augment, capsys):
        rows = [{**CALIB_ROW, "run_id": "r1", "doc": "a"}]
        augment({"r1/a/": lib.SectionRead([])}, rows)
        out = capsys.readouterr().out
        assert "no_sections" in out
        assert "could not be READ" not in out


# --------------------------------------------------------------------------- #
# 9. Site 6 — fixed in #1062; pinned here so the class stays closed
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestMissingMetricNotesReportsBothDirections:
    def test_both_directions_are_reported(self):
        with_metric = {"calibration": {"observations": 100}}
        without = {}
        assert aggregate._missing_metric_notes(with_metric, without) == [
            (aggregate.LATE_ADDED_METRICS["calibration"], "baseline")
        ]
        assert aggregate._missing_metric_notes(without, with_metric) == [
            (aggregate.LATE_ADDED_METRICS["calibration"], "current")
        ]

    def test_absent_from_both_sides_says_nothing_about_either(self):
        assert aggregate._missing_metric_notes({}, {}) == []


# --------------------------------------------------------------------------- #
# 10. Site 5 — the bootstrap of this very directory
# --------------------------------------------------------------------------- #
REPO = os.path.dirname(os.path.dirname(HARNESS))


@pytest.mark.unit
class TestTheBootstrapCannotSkipSilently:
    def test_the_harness_path_is_absolute(self):
        assert os.path.isabs(HARNESS)
        assert os.path.isdir(HARNESS)

    def test_a_missing_harness_module_is_an_error_not_a_skip(self):
        """A skip here is how a broken bootstrap reports green.

        Not written with ``pytest.raises``, and the reason is the defect itself:
        ``pytest.skip`` raises ``Skipped``, which derives from ``BaseException`` and
        so passes straight through ``pytest.raises(RuntimeError)`` — the test would
        then report SKIPPED, and a green run would again mean nothing. Measured: a
        mutant that skips here left a ``pytest.raises`` version of this test
        undetected.

        ⚠️ **Catching at ``BaseException`` is only half of it, and the other half is
        what does the work.** Widening the catch alone converts the skip into a
        PASS — quieter than a skip, not louder — because the exception is swallowed
        and nothing then objects. The assertion on the *type* is the check; the wide
        catch only stops the outcome escaping before it can be examined.

        This is not a repository-wide hazard, and the negative is worth recording so
        nobody widens it into one. No production code here calls a pytest outcome
        function, so no other ``pytest.raises`` site can receive one. The condition is
        structural to this test: the thing under test *is* a test bootstrap, whose
        failure mode is skipping. Note also that only skip-shaped outcomes are
        silently green — ``pytest.fail``'s ``Failed`` is ``BaseException``-derived too
        and surfaces loudly.
        """
        raised: BaseException | None = None
        try:
            harness_module("no_such_harness_module")
        except BaseException as exc:  # noqa: BLE001 - a skip must not pass through
            raised = exc
        assert isinstance(raised, RuntimeError), (
            f"a missing harness module must be an ERROR, got "
            f"{type(raised).__name__}: a skip here reports green"
        )
        assert "broken test bootstrap" in str(raised)

    @pytest.mark.parametrize(
        ("missing", "may_skip"),
        [
            ("boto3", True),  # a bare checkout may lack it; skipping is intended
            ("yaml", True),
            ("matplotlib", True),
            ("idp_common", False),  # first-party: the measurements are calls into it
            ("idp_common.evaluation", False),  # judged on the root package
            ("idp_sdk", False),
            ("idp_cli", False),
            ("idp_feature_sdk", False),
            ("idp_mcp_connector", False),
            ("lib", False),  # a harness module
            ("analyze", False),
            ("aggregate", False),
            (None, False),  # an ImportError that does not name a module
        ],
    )
    def test_only_an_environment_problem_may_become_a_skip(self, missing, may_skip):
        """Decided from the name, so the rule cannot go stale in either direction."""
        import harness_import

        assert harness_import.skippable(missing) is may_skip, missing

    def test_every_first_party_package_is_covered(self):
        """Derived from the tree, and cross-checked against the installer's own list.

        An authored list would err PERMISSIVE on a package added later — an
        unrecognised name is exactly what becomes a skip — so the set is read off
        ``lib/*/<import name>/`` and compared here against the distributions
        ``FIRST_PARTY_EDITABLES`` installs, which is the authority on what ships.
        """
        import harness_import

        derived = harness_import.first_party_import_names()
        makefile = (Path(REPO) / "Makefile").read_text()
        block = makefile.split("FIRST_PARTY_EDITABLES", 1)[1].split("\n\n", 1)[0]
        distributions = re.findall(r"lib/([A-Za-z0-9_]+)", block)
        assert len(distributions) == 5, distributions
        for dist in distributions:
            # lib/<dist>/<import name>/ — the import name may differ from the
            # distribution directory (idp_common_pkg holds idp_common).
            names = {
                p.parent.name for p in (Path(REPO) / "lib" / dist).glob("*/__init__.py")
            } - {"tests"}
            assert names, f"no import package under lib/{dist}"
            assert names <= derived, f"lib/{dist} ships {names - derived}, uncovered"
            for name in names:
                assert not harness_import.skippable(name), name

    def test_the_suite_runs_from_a_foreign_working_directory(self, tmp_path):
        """The half of #1079 instance 5 that CI could not see, measured directly.

        Run from anywhere but the repository root the old relative ``sys.path.insert``
        collapsed a whole file to ``1 skipped`` with a green exit. This invokes one
        real test of this file from ``tmp_path`` and requires a PASS — a skip fails.
        """
        target = (
            f"{os.path.relpath(__file__, REPO)}"
            "::TestTheBootstrapCannotSkipSilently::test_the_harness_path_is_absolute"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [os.path.join(REPO, "lib", "idp_common_pkg"), env.get("PYTHONPATH", "")]
        )
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", os.path.join(REPO, target), "-q", "-rs"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert "1 passed" in proc.stdout, proc.stdout[-3000:]
        assert "skipped" not in proc.stdout, proc.stdout[-3000:]
