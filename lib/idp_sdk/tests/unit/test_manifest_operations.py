# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The manifest layer: `_core/manifest_parser.py` and `operations/manifest.py`.

A manifest is the batch-processing entry point — a CSV or JSON file naming the
documents to run and, optionally, the baseline directory each one is scored
against. Two pieces of code make up that layer and they fail in different ways.

`ManifestParser` reads a file somebody else wrote. Its whole job is to turn
loose text into a list of `{path, type, filename, baseline_source}` dicts and to
refuse the rows it cannot. That makes it the one place here where a table of
real-shaped inputs — including the malformed ones — is the right test, so most of
this file is exactly that, and every case asserts the **parsed structure** rather
than the absence of an exception. Several of those cases pin behaviour that is
wrong (a value that raises `AttributeError` instead of the documented
`ValueError`, an S3 prefix accepted as a document with no filename); each such
test says so in its docstring and names the line.

`ManifestOperation.generate` walks a local directory or an S3 prefix, optionally
uploads everything to a test-set bucket, and writes the CSV. Its S3 and test-set
paths are exercised against `moto` with real buckets: the test uploads through
the operation and then **lists the bucket and reads the objects back**, because
the interesting failures there are a wrong S3 key or a file that was never sent,
and a `MagicMock` records a call for both. The stack lookup goes through real
`moto` CloudFormation too, so `TestSetBucket` is resolved from a real stack
output rather than from a patched dictionary.
"""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from idp_sdk import IDPClient
from idp_sdk._core.manifest_parser import (
    ManifestParser,
    parse_manifest,
    validate_manifest,
)
from idp_sdk.exceptions import IDPConfigurationError, IDPResourceNotFoundError
from idp_sdk.models import ManifestResult, ManifestValidationResult

# A CloudFormation stack shaped like the parts of an IDP deployment that
# `manifest.generate(test_set=...)` reaches: the test-set bucket is published as
# a stack *output* (that is how `StackInfo` finds it), and `DocumentQueue` has to
# exist because `StackInfo.get_resources` raises without it.
STACK_TEMPLATE = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Resources": {
        "TestSetBucket": {"Type": "AWS::S3::Bucket"},
        "DocumentQueue": {"Type": "AWS::SQS::Queue"},
    },
    "Outputs": {
        "S3TestSetBucketName": {"Value": {"Ref": "TestSetBucket"}},
    },
}

# The same stack with the test-set output removed: a deployment whose pattern
# does not create one.
STACK_TEMPLATE_NO_TEST_SET = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Resources": {"DocumentQueue": {"Type": "AWS::SQS::Queue"}},
}


def _create_stack(name: str, template: dict, region: str) -> str:
    """Create the stack and return the physical name of its test-set bucket."""
    cfn = boto3.client("cloudformation", region_name=region)
    cfn.create_stack(StackName=name, TemplateBody=json.dumps(template))
    for page in cfn.get_paginator("list_stack_resources").paginate(StackName=name):
        for resource in page["StackResourceSummaries"]:
            if resource["LogicalResourceId"] == "TestSetBucket":
                return resource["PhysicalResourceId"]
    return ""


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def _write_json(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _read_csv(path: Path) -> list[dict]:
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))


# --------------------------------------------------------------------------
# ManifestParser: format detection
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestFormatDetection:
    """Which parser a filename selects, and which filenames are refused."""

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("m.csv", "csv"),
            ("m.CSV", "csv"),
            ("m.txt", "csv"),
            ("m.json", "json"),
            ("m.JSON", "json"),
            ("m.jsonl", "json"),
        ],
    )
    def test_the_extension_selects_the_parser(self, tmp_path, filename, expected):
        assert ManifestParser(str(tmp_path / filename)).format == expected

    @pytest.mark.parametrize("filename", ["m.xml", "m.yaml", "manifest", "m.csv.gz"])
    def test_an_unsupported_extension_is_refused_at_construction(
        self, tmp_path, filename
    ):
        """The refusal happens in `__init__`, so a bad name never reaches a read."""
        with pytest.raises(ValueError, match="Unsupported manifest format"):
            ManifestParser(str(tmp_path / filename))


# --------------------------------------------------------------------------
# ManifestParser: well-formed input
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestParsingWellFormedManifests:
    def test_a_csv_row_parses_to_the_documented_structure(self, tmp_path):
        """Assert the whole dict, so a renamed or dropped key fails here."""
        doc = tmp_path / "invoice.pdf"
        doc.write_bytes(b"%PDF-1.4")
        baseline = tmp_path / "baselines" / "invoice.pdf"
        baseline.mkdir(parents=True)

        manifest = _write_csv(
            tmp_path / "m.csv",
            [{"document_path": str(doc), "baseline_source": str(baseline)}],
            ["document_path", "baseline_source"],
        )

        assert parse_manifest(str(manifest)) == [
            {
                "path": str(doc),
                "type": "local",
                "filename": "invoice.pdf",
                "baseline_source": str(baseline),
            }
        ]

    def test_an_absent_baseline_column_yields_none_not_an_empty_string(self, tmp_path):
        """`has_baselines` is computed from truthiness, so "" and None differ."""
        doc = tmp_path / "a.pdf"
        doc.write_bytes(b"%PDF")
        manifest = _write_csv(
            tmp_path / "m.csv", [{"document_path": str(doc)}], ["document_path"]
        )

        assert parse_manifest(str(manifest))[0]["baseline_source"] is None

    def test_an_empty_baseline_cell_is_normalised_to_none(self, tmp_path):
        doc = tmp_path / "a.pdf"
        doc.write_bytes(b"%PDF")
        manifest = _write_csv(
            tmp_path / "m.csv",
            [{"document_path": str(doc), "baseline_source": "   "}],
            ["document_path", "baseline_source"],
        )

        assert parse_manifest(str(manifest))[0]["baseline_source"] is None

    def test_the_two_json_shapes_parse_identically(self, tmp_path):
        """A bare array and `{"documents": [...]}` are both accepted."""
        doc = tmp_path / "a.pdf"
        doc.write_bytes(b"%PDF")
        rows = [{"document_path": str(doc)}]

        as_array = parse_manifest(str(_write_json(tmp_path / "arr.json", rows)))
        as_object = parse_manifest(
            str(_write_json(tmp_path / "obj.json", {"documents": rows}))
        )

        assert as_array == as_object
        assert as_array[0]["type"] == "local"

    def test_the_path_key_is_accepted_as_well_as_document_path(self, tmp_path):
        doc = tmp_path / "a.pdf"
        doc.write_bytes(b"%PDF")

        assert parse_manifest(
            str(_write_json(tmp_path / "m.json", [{"path": str(doc)}]))
        )[0]["path"] == str(doc)

    def test_an_s3_uri_is_classified_without_touching_the_network(self, tmp_path):
        """No S3 call is made — an s3:// row is classified from its text alone."""
        manifest = _write_json(
            tmp_path / "m.json", [{"document_path": "s3://docs/in/statement.pdf"}]
        )

        assert parse_manifest(str(manifest)) == [
            {
                "path": "s3://docs/in/statement.pdf",
                "type": "s3",
                "filename": "statement.pdf",
                "baseline_source": None,
            }
        ]

    def test_a_multi_row_manifest_preserves_order(self, tmp_path):
        names = ["a.pdf", "b.pdf", "c.pdf"]
        for name in names:
            (tmp_path / name).write_bytes(b"%PDF")
        manifest = _write_csv(
            tmp_path / "m.csv",
            [{"document_path": str(tmp_path / n)} for n in names],
            ["document_path"],
        )

        assert [d["filename"] for d in parse_manifest(str(manifest))] == names


# --------------------------------------------------------------------------
# ManifestParser: malformed input
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestParsingMalformedManifests:
    def test_a_row_with_no_path_field_names_both_spellings(self, tmp_path):
        manifest = _write_json(tmp_path / "m.json", [{"unrelated": "x"}])
        with pytest.raises(ValueError, match="document_path.*or.*path"):
            parse_manifest(str(manifest))

    def test_an_absolute_path_that_does_not_exist_is_reported_as_missing(
        self, tmp_path
    ):
        """Distinct from "invalid path": the manifest is well-formed, the file is gone."""
        manifest = _write_json(
            tmp_path / "m.json", [{"document_path": str(tmp_path / "gone.pdf")}]
        )
        with pytest.raises(ValueError, match="Local file not found"):
            parse_manifest(str(manifest))

    def test_a_relative_path_that_does_not_exist_is_reported_as_invalid(self, tmp_path):
        manifest = _write_json(tmp_path / "m.json", [{"document_path": "docs/a.pdf"}])
        with pytest.raises(ValueError, match="Use absolute local path or s3:// URI"):
            parse_manifest(str(manifest))

    def test_an_s3_uri_with_no_key_is_refused(self, tmp_path):
        manifest = _write_json(tmp_path / "m.json", [{"document_path": "s3://bucket"}])
        with pytest.raises(ValueError, match="Invalid S3 URI format"):
            parse_manifest(str(manifest))

    def test_json_that_is_neither_array_nor_documents_object_is_refused(self, tmp_path):
        manifest = _write_json(tmp_path / "m.json", {"docs": []})
        with pytest.raises(ValueError, match="must be an array or object"):
            parse_manifest(str(manifest))

    def test_one_bad_row_fails_the_whole_manifest(self, tmp_path):
        """Partial parsing is not offered: row 2 being bad stops the parse.

        A failure here would mean a batch silently ran a subset of what the
        manifest listed.
        """
        good = tmp_path / "a.pdf"
        good.write_bytes(b"%PDF")
        manifest = _write_csv(
            tmp_path / "m.csv",
            [{"document_path": str(good)}, {"document_path": "nope.pdf"}],
            ["document_path"],
        )

        with pytest.raises(ValueError, match="Invalid path"):
            parse_manifest(str(manifest))


# --------------------------------------------------------------------------
# ManifestParser: defects pinned at their current behaviour
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestParserDefectsPinnedAtCurrentBehaviour:
    """Each test here records behaviour that is wrong, and why it matters.

    None of them is a fix. They exist so that a change to any of these
    behaviours is a deliberate, visible one.
    """

    def test_whitespace_is_stripped_from_path_but_not_from_document_path(
        self, tmp_path
    ):
        """DEFECT — `_core/manifest_parser.py:124`.

        The line is::

            row.get("document_path") or row.get("path", "").strip()

        `.strip()` binds to the *fallback* only, so the two accepted column names
        do not behave the same way: a value with surrounding whitespace is
        accepted under `path` and refused under `document_path`. A CSV exported
        from a spreadsheet is the ordinary way to get that whitespace, and the
        resulting error ("Invalid path ' /abs/a.pdf '") points at the path rather
        than at the spacing.
        """
        doc = tmp_path / "a.pdf"
        doc.write_bytes(b"%PDF")
        padded = f" {doc} "

        under_path = parse_manifest(
            str(_write_json(tmp_path / "ok.json", [{"path": padded}]))
        )
        assert under_path[0]["path"] == str(doc), "the fallback spelling is stripped"

        with pytest.raises(ValueError, match="Invalid path"):
            parse_manifest(
                str(_write_json(tmp_path / "bad.json", [{"document_path": padded}]))
            )

    @pytest.mark.parametrize(
        ("row", "message"),
        [
            ({"path": None}, "'NoneType' object has no attribute 'strip'"),
            ({"document_path": 123}, "'int' object has no attribute 'startswith'"),
        ],
    )
    def test_a_null_or_non_string_value_raises_attributeerror_not_valueerror(
        self, tmp_path, row, message
    ):
        """DEFECT — `_core/manifest_parser.py:124` and `:130`.

        `_validate_and_normalize_row` is documented to signal a bad row with
        `ValueError`, and both `_parse_csv` and `_parse_json` catch only
        `ValueError` in order to log the row number before re-raising. A JSON
        manifest holding `null` or a number therefore escapes as a bare
        `AttributeError` with **no row number**, which for a 500-row manifest is
        the difference between a one-line fix and a bisect.
        """
        manifest = _write_json(tmp_path / "m.json", [row])
        with pytest.raises(AttributeError, match=message.replace("'", "'")):
            parse_manifest(str(manifest))

    def test_a_null_baseline_source_raises_attributeerror(self, tmp_path):
        """DEFECT — `_core/manifest_parser.py:148`.

        `row.get("baseline_source", "").strip()` assumes a string. JSON `null` is
        the natural way to spell "this document has no baseline" and is what
        `json.dumps` produces from a `None`, so a manifest generated by a
        caller's own script fails on a field that is documented as optional.
        """
        doc = tmp_path / "a.pdf"
        doc.write_bytes(b"%PDF")
        manifest = _write_json(
            tmp_path / "m.json",
            [{"document_path": str(doc), "baseline_source": None}],
        )
        with pytest.raises(AttributeError, match="has no attribute 'strip'"):
            parse_manifest(str(manifest))

    def test_an_s3_prefix_is_accepted_as_a_document_with_no_filename(self, tmp_path):
        """DEFECT — `_core/manifest_parser.py:133-135`.

        The S3 check is "at least 8 characters and a `/` after `s3://`", which
        `s3://bucket/` satisfies. `os.path.basename` of it is `""`, so the row
        parses to a document with an empty filename. Two consequences follow:
        `validate_manifest`'s duplicate-filename check reports a collision
        between two such rows, and the test-set upload path builds the S3 key
        `<test_set>/input/` — a directory marker rather than a document.
        """
        parsed = parse_manifest(
            str(_write_json(tmp_path / "m.json", [{"document_path": "s3://bucket/"}]))
        )
        assert parsed == [
            {
                "path": "s3://bucket/",
                "type": "s3",
                "filename": "",
                "baseline_source": None,
            }
        ]

    def test_a_jsonl_extension_is_accepted_but_json_lines_content_is_not(
        self, tmp_path
    ):
        """DEFECT — `_core/manifest_parser.py:39` versus `:83-84`.

        `_detect_format` lists `.jsonl` among the JSON extensions, but
        `_parse_json` calls `json.load` on the whole file, which only reads a
        single JSON document. A genuine JSON Lines manifest — one object per
        line, which is what the extension means — fails with "Extra data: line
        2", an error that says nothing about the format being unsupported.
        """
        doc = tmp_path / "a.pdf"
        doc.write_bytes(b"%PDF")
        manifest = tmp_path / "m.jsonl"
        manifest.write_text(
            "\n".join(json.dumps({"document_path": str(doc)}) for _ in range(2)),
            encoding="utf-8",
        )

        valid, error = validate_manifest(str(manifest))
        assert valid is False
        assert error is not None and "Extra data" in error

    def test_a_relative_path_is_resolved_against_the_process_cwd(
        self, tmp_path, monkeypatch
    ):
        """DEFECT — `_core/manifest_parser.py:136-141`.

        Nothing in the parser knows where the manifest file itself lives, so a
        relative entry is resolved against whatever directory the command was
        run from. The same manifest therefore parses in one shell and fails in
        another, and an entry that escapes the manifest's own directory
        (`../outside/a.pdf`) is accepted whenever it resolves from the caller's
        cwd — the parser offers no containment at all. Both halves are asserted
        below because the asymmetry is the whole point.
        """
        manifest_dir = tmp_path / "manifests"
        manifest_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "a.pdf").write_bytes(b"%PDF")

        manifest = _write_json(
            manifest_dir / "m.json", [{"document_path": "outside/a.pdf"}]
        )

        monkeypatch.chdir(tmp_path)
        assert parse_manifest(str(manifest))[0]["filename"] == "a.pdf"

        # Same manifest, same file on disk, different cwd -> refused.
        monkeypatch.chdir(manifest_dir)
        with pytest.raises(ValueError, match="Invalid path"):
            parse_manifest(str(manifest))

    def test_the_parsed_rows_carry_no_document_id(self, tmp_path):
        """DEFECT (documentation) — `_core/manifest_parser.py:44-53`.

        `parse()`'s docstring promises each dict carries `document_id`. It never
        has; `_validate_and_normalize_row` returns `path`, `type`, `filename` and
        `baseline_source`. A caller written from the docstring gets a `KeyError`.
        """
        doc = tmp_path / "a.pdf"
        doc.write_bytes(b"%PDF")
        parsed = parse_manifest(
            str(_write_json(tmp_path / "m.json", [{"document_path": str(doc)}]))
        )

        assert set(parsed[0]) == {"path", "type", "filename", "baseline_source"}
        assert "document_id" not in parsed[0]


# --------------------------------------------------------------------------
# validate_manifest
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestValidateManifest:
    """`validate_manifest` answers (is_valid, message) and never raises."""

    def test_a_good_manifest_validates_with_no_message(self, tmp_path):
        doc = tmp_path / "a.pdf"
        doc.write_bytes(b"%PDF")
        manifest = _write_csv(
            tmp_path / "m.csv", [{"document_path": str(doc)}], ["document_path"]
        )

        assert validate_manifest(str(manifest)) == (True, None)

    def test_duplicate_filenames_are_refused_and_named(self, tmp_path):
        """Two documents with the same basename collide as one S3 key.

        The upload path keys on `os.path.basename`, so accepting this would mean
        the second document silently overwriting the first in the test-set
        bucket and the batch processing one input twice.
        """
        first = tmp_path / "a.pdf"
        first.write_bytes(b"%PDF")
        nested = tmp_path / "sub"
        nested.mkdir()
        second = nested / "a.pdf"
        second.write_bytes(b"%PDF")

        manifest = _write_csv(
            tmp_path / "m.csv",
            [{"document_path": str(first)}, {"document_path": str(second)}],
            ["document_path"],
        )

        valid, error = validate_manifest(str(manifest))
        assert valid is False
        assert error is not None and "a.pdf" in error and "Duplicate" in error

    @pytest.mark.parametrize(
        ("name", "content", "fragment"),
        [
            ("m.csv", "", "no documents"),
            ("m.csv", "document_path\n", "no documents"),
            ("m.json", "[]", "no documents"),
            ("m.json", "", "Expecting value"),
            ("m.json", "{not json}", "Expecting property name"),
            ("m.xml", "<x/>", "Unsupported manifest format"),
        ],
    )
    def test_degenerate_files_are_reported_rather_than_raised(
        self, tmp_path, name, content, fragment
    ):
        """An empty file, an empty list and a bad extension all answer, not raise."""
        manifest = tmp_path / name
        manifest.write_text(content, encoding="utf-8")

        valid, error = validate_manifest(str(manifest))
        assert valid is False
        assert error is not None and fragment in error

    def test_a_missing_file_is_reported_rather_than_raised(self, tmp_path):
        valid, error = validate_manifest(str(tmp_path / "absent.csv"))
        assert valid is False
        assert error is not None and "absent.csv" in error


# --------------------------------------------------------------------------
# ManifestOperation.validate
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestManifestOperationValidate:
    def test_a_valid_manifest_reports_its_document_count(self, tmp_path):
        docs = ["a.pdf", "b.pdf"]
        for name in docs:
            (tmp_path / name).write_bytes(b"%PDF")
        manifest = _write_csv(
            tmp_path / "m.csv",
            [{"document_path": str(tmp_path / n)} for n in docs],
            ["document_path"],
        )

        result = IDPClient().manifest.validate(str(manifest))

        assert isinstance(result, ManifestValidationResult)
        assert result.valid is True
        assert result.error is None
        assert result.document_count == 2
        assert result.has_baselines is False

    def test_a_baseline_column_sets_has_baselines(self, tmp_path):
        doc = tmp_path / "a.pdf"
        doc.write_bytes(b"%PDF")
        baseline = tmp_path / "baselines" / "a.pdf"
        baseline.mkdir(parents=True)
        manifest = _write_csv(
            tmp_path / "m.csv",
            [{"document_path": str(doc), "baseline_source": str(baseline)}],
            ["document_path", "baseline_source"],
        )

        result = IDPClient().manifest.validate(str(manifest))

        assert result.valid is True
        assert result.has_baselines is True

    def test_an_invalid_manifest_leaves_the_count_unknown(self, tmp_path):
        """`document_count=None` means "not counted", which is not zero.

        Reporting `0` would be indistinguishable from an empty-but-readable
        manifest, and a caller gating on `document_count == 0` would take the
        wrong branch.
        """
        manifest = _write_json(tmp_path / "m.json", [{"document_path": "nope.pdf"}])

        result = IDPClient().manifest.validate(str(manifest))

        assert result.valid is False
        assert result.document_count is None
        assert result.has_baselines is False
        assert result.error is not None


# --------------------------------------------------------------------------
# ManifestOperation.generate — argument refusals
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestGenerateArgumentRefusals:
    def test_neither_directory_nor_s3_uri_is_refused(self):
        with pytest.raises(IDPConfigurationError, match="directory or s3_uri"):
            IDPClient().manifest.generate()

    def test_a_test_set_without_a_stack_is_refused_before_any_scan(self, tmp_path):
        """The refusal is up front, so no directory is walked and no client built."""
        with pytest.raises(IDPConfigurationError, match="stack_name is required"):
            IDPClient().manifest.generate(directory=str(tmp_path), test_set="ts")

    def test_a_uri_that_is_not_s3_is_refused(self):
        with pytest.raises(IDPConfigurationError, match="Invalid S3 URI"):
            IDPClient().manifest.generate(s3_uri="https://example.com/docs/")


# --------------------------------------------------------------------------
# ManifestOperation.generate — local directory
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestGenerateFromLocalDirectory:
    def test_a_recursive_scan_finds_nested_documents_and_filters_by_pattern(
        self, tmp_path
    ):
        (tmp_path / "top.pdf").write_bytes(b"%PDF")
        (tmp_path / "notes.txt").write_text("not a document")
        nested = tmp_path / "deep" / "deeper"
        nested.mkdir(parents=True)
        (nested / "buried.pdf").write_bytes(b"%PDF")

        output = tmp_path / "m.csv"
        result = IDPClient().manifest.generate(
            directory=str(tmp_path), output=str(output)
        )

        assert isinstance(result, ManifestResult)
        assert result.document_count == 2
        assert result.test_set_created is False
        assert result.test_set_name is None
        rows = _read_csv(output)
        assert {Path(r["document_path"]).name for r in rows} == {
            "top.pdf",
            "buried.pdf",
        }
        assert all(r["baseline_source"] == "" for r in rows)

    def test_a_non_recursive_scan_stops_at_the_top_level(self, tmp_path):
        (tmp_path / "top.pdf").write_bytes(b"%PDF")
        nested = tmp_path / "deep"
        nested.mkdir()
        (nested / "buried.pdf").write_bytes(b"%PDF")

        result = IDPClient().manifest.generate(
            directory=str(tmp_path), recursive=False, output=None
        )

        assert result.document_count == 1
        assert result.output_path is None

    def test_the_file_pattern_is_honoured(self, tmp_path):
        (tmp_path / "a.pdf").write_bytes(b"%PDF")
        (tmp_path / "b.tiff").write_bytes(b"II*")
        (tmp_path / "c.tiff").write_bytes(b"II*")

        result = IDPClient().manifest.generate(
            directory=str(tmp_path), file_pattern="*.tiff"
        )

        assert result.document_count == 2

    def test_a_directory_with_no_matches_yields_an_empty_manifest(self, tmp_path):
        """Header-only CSV, count 0 — not an error, and not a missing file."""
        output = tmp_path / "m.csv"
        result = IDPClient().manifest.generate(
            directory=str(tmp_path), output=str(output)
        )

        assert result.document_count == 0
        assert output.read_text(encoding="utf-8").strip() == (
            "document_path,baseline_source"
        )

    def test_a_matching_baseline_directory_is_written_into_the_csv(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.pdf").write_bytes(b"%PDF")
        (docs / "b.pdf").write_bytes(b"%PDF")

        baselines = tmp_path / "baselines"
        (baselines / "a.pdf").mkdir(parents=True)

        output = tmp_path / "m.csv"
        result = IDPClient().manifest.generate(
            directory=str(docs), baseline_dir=str(baselines), output=str(output)
        )

        rows = {
            Path(r["document_path"]).name: r["baseline_source"]
            for r in _read_csv(output)
        }
        assert rows["a.pdf"] == str(baselines / "a.pdf")
        assert rows["b.pdf"] == ""
        assert result.baselines_matched == 1

    def test_a_baseline_file_rather_than_a_directory_is_ignored(self, tmp_path):
        """Only sub*directories* of the baseline dir count as baselines."""
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.pdf").write_bytes(b"%PDF")

        baselines = tmp_path / "baselines"
        baselines.mkdir()
        (baselines / "a.pdf").write_text("a stray file, not a baseline directory")

        result = IDPClient().manifest.generate(
            directory=str(docs), baseline_dir=str(baselines)
        )

        assert result.baselines_matched == 0


# --------------------------------------------------------------------------
# ManifestOperation.generate — defects
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestGenerateDefectsPinnedAtCurrentBehaviour:
    def test_baselines_matched_counts_baseline_directories_not_matches(self, tmp_path):
        """DEFECT — `operations/manifest.py:152`.

        `baselines_matched=len(baseline_map)` is the number of subdirectories
        found under `baseline_dir`, whatever they are named. The field is
        documented as "Number of documents with baselines", and the obvious
        check a caller writes — `baselines_matched == document_count`, i.e. every
        document got a baseline — passes here while **no** document has one: one
        document, two unrelated baseline directories, and an empty
        `baseline_source` in every CSV row.
        """
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "z.pdf").write_bytes(b"%PDF")

        baselines = tmp_path / "baselines"
        (baselines / "unrelated-one").mkdir(parents=True)
        (baselines / "unrelated-two").mkdir(parents=True)

        output = tmp_path / "m.csv"
        result = IDPClient().manifest.generate(
            directory=str(docs), baseline_dir=str(baselines), output=str(output)
        )

        assert result.document_count == 1
        assert result.baselines_matched == 2, "counts directories, not matches"
        assert [r["baseline_source"] for r in _read_csv(output)] == [""], (
            "no document actually has a baseline"
        )

    @mock_aws
    def test_a_baseline_dir_is_silently_ignored_for_an_s3_source(
        self, tmp_path, aws_credentials
    ):
        """DEFECT — `operations/manifest.py:95`.

        The baseline scan is guarded by `if baseline_dir and directory`, so
        passing `baseline_dir` alongside `s3_uri` does nothing at all — no
        warning, no error, `baselines_matched=0` and an empty `baseline_source`
        in every row. The caller's evaluation run then scores against nothing
        and reports it as a completed evaluation.
        """
        s3 = boto3.client("s3", region_name=aws_credentials)
        s3.create_bucket(Bucket="docs")
        s3.put_object(Bucket="docs", Key="in/a.pdf", Body=b"%PDF")

        baselines = tmp_path / "baselines"
        (baselines / "a.pdf").mkdir(parents=True)

        output = tmp_path / "m.csv"
        result = IDPClient(region=aws_credentials).manifest.generate(
            s3_uri="s3://docs/in",
            baseline_dir=str(baselines),
            output=str(output),
        )

        assert result.document_count == 1
        assert result.baselines_matched == 0
        assert [r["baseline_source"] for r in _read_csv(output)] == [""]


# --------------------------------------------------------------------------
# ManifestOperation.generate — S3 source, against a real (moto) bucket
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestGenerateFromS3:
    @mock_aws
    def test_a_prefix_without_a_trailing_slash_is_normalised(
        self, aws_credentials, tmp_path
    ):
        """`s3://docs/in` must not also match `s3://docs/input-archive/`.

        The operation appends the slash before listing. Without it the listing
        is a prefix match on the raw string and picks up sibling prefixes that
        merely start with the same characters.
        """
        s3 = boto3.client("s3", region_name=aws_credentials)
        s3.create_bucket(Bucket="docs")
        for key in ["in/a.pdf", "input-archive/old.pdf"]:
            s3.put_object(Bucket="docs", Key=key, Body=b"%PDF")

        output = tmp_path / "m.csv"
        result = IDPClient(region=aws_credentials).manifest.generate(
            s3_uri="s3://docs/in", output=str(output)
        )

        assert result.document_count == 1
        assert [r["document_path"] for r in _read_csv(output)] == ["s3://docs/in/a.pdf"]

    @mock_aws
    def test_directory_markers_and_non_matching_names_are_skipped(
        self, aws_credentials
    ):
        s3 = boto3.client("s3", region_name=aws_credentials)
        s3.create_bucket(Bucket="docs")
        for key in ["in/a.pdf", "in/sub/", "in/notes.txt", "in/deep/b.pdf"]:
            s3.put_object(Bucket="docs", Key=key, Body=b"")

        result = IDPClient(region=aws_credentials).manifest.generate(
            s3_uri="s3://docs/in/"
        )

        assert result.document_count == 2

    @mock_aws
    def test_a_non_recursive_s3_scan_excludes_deeper_keys(self, aws_credentials):
        s3 = boto3.client("s3", region_name=aws_credentials)
        s3.create_bucket(Bucket="docs")
        for key in ["in/a.pdf", "in/b.pdf", "in/deep/c.pdf"]:
            s3.put_object(Bucket="docs", Key=key, Body=b"%PDF")

        result = IDPClient(region=aws_credentials).manifest.generate(
            s3_uri="s3://docs/in/", recursive=False
        )

        assert result.document_count == 2

    @mock_aws
    def test_a_bucket_root_uri_scans_every_key(self, aws_credentials):
        """`s3://docs` with no prefix leaves the prefix empty rather than "/"."""
        s3 = boto3.client("s3", region_name=aws_credentials)
        s3.create_bucket(Bucket="docs")
        for key in ["a.pdf", "in/b.pdf", "in/deep/c.pdf"]:
            s3.put_object(Bucket="docs", Key=key, Body=b"%PDF")

        result = IDPClient(region=aws_credentials).manifest.generate(s3_uri="s3://docs")

        assert result.document_count == 3

    @mock_aws
    def test_the_s3_listing_pages(self, aws_credentials):
        """More keys than one `list_objects_v2` page returns.

        The operation uses a paginator; a plain `list_objects_v2` call would
        silently stop at 1000 objects and report a short manifest as complete.
        """
        s3 = boto3.client("s3", region_name=aws_credentials)
        s3.create_bucket(Bucket="docs")
        for index in range(1005):
            s3.put_object(Bucket="docs", Key=f"in/doc-{index:05d}.pdf", Body=b"%PDF")

        result = IDPClient(region=aws_credentials).manifest.generate(
            s3_uri="s3://docs/in/"
        )

        assert result.document_count == 1005


# --------------------------------------------------------------------------
# ManifestOperation.generate — test set upload, against real (moto) AWS
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestGenerateTestSet:
    @mock_aws
    def test_documents_and_baselines_are_uploaded_and_the_manifest_rewritten(
        self, tmp_path, aws_credentials
    ):
        """The full test-set path, verified by reading the bucket back.

        Three things have to be true together and only the third is visible in
        the returned model: the objects exist under the expected keys, their
        bodies are the local files' bytes, and every `document_path` in the CSV
        now points at S3 rather than at the developer's disk. A run that uploaded
        nothing would still return `test_set_created=True`.
        """
        bucket = _create_stack("idp-ts", STACK_TEMPLATE, aws_credentials)

        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.pdf").write_bytes(b"%PDF-a")
        (docs / "b.pdf").write_bytes(b"%PDF-b")

        baselines = tmp_path / "baselines"
        (baselines / "a.pdf" / "nested").mkdir(parents=True)
        (baselines / "a.pdf" / "results.json").write_text('{"Total": "1"}')
        (baselines / "a.pdf" / "nested" / "extra.json").write_text("{}")

        output = tmp_path / "m.csv"
        result = IDPClient(region=aws_credentials).manifest.generate(
            directory=str(docs),
            baseline_dir=str(baselines),
            output=str(output),
            test_set="run-7",
            stack_name="idp-ts",
        )

        assert result.test_set_created is True
        assert result.test_set_name == "run-7"
        assert result.document_count == 2

        s3 = boto3.client("s3", region_name=aws_credentials)
        stored = {
            obj["Key"] for obj in s3.list_objects_v2(Bucket=bucket).get("Contents", [])
        }
        assert stored == {
            "run-7/input/a.pdf",
            "run-7/input/b.pdf",
            "run-7/baseline/a.pdf/results.json",
            "run-7/baseline/a.pdf/nested/extra.json",
        }, "the nested baseline file is walked, not just the top level"

        body = s3.get_object(Bucket=bucket, Key="run-7/input/a.pdf")["Body"].read()
        assert body == b"%PDF-a", "the object holds the local file's bytes"

        rows = {Path(r["document_path"]).name: r for r in _read_csv(output)}
        assert rows["a.pdf"]["document_path"] == f"s3://{bucket}/run-7/input/a.pdf"
        assert rows["a.pdf"]["baseline_source"] == (
            f"s3://{bucket}/run-7/baseline/a.pdf/"
        )
        assert rows["b.pdf"]["baseline_source"] == ""

    @mock_aws
    def test_a_stack_with_no_test_set_bucket_is_reported(
        self, tmp_path, aws_credentials
    ):
        """A deployment whose pattern publishes no test-set bucket.

        `StackInfo` maps the missing output to `""`, so the check has to treat an
        empty string as absent; a truthiness bug here would send every upload to
        a bucket named `""`.
        """
        _create_stack("idp-no-ts", STACK_TEMPLATE_NO_TEST_SET, aws_credentials)

        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.pdf").write_bytes(b"%PDF")

        with pytest.raises(IDPResourceNotFoundError, match="TestSetBucket not found"):
            IDPClient(region=aws_credentials).manifest.generate(
                directory=str(docs), test_set="run-1", stack_name="idp-no-ts"
            )

    @mock_aws
    def test_a_test_set_with_no_baselines_uploads_only_the_documents(
        self, tmp_path, aws_credentials
    ):
        bucket = _create_stack("idp-ts2", STACK_TEMPLATE, aws_credentials)

        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "only.pdf").write_bytes(b"%PDF")

        result = IDPClient(
            stack_name="idp-ts2", region=aws_credentials
        ).manifest.generate(directory=str(docs), test_set="bare", stack_name="idp-ts2")

        assert result.baselines_matched == 0
        assert result.output_path is None
        s3 = boto3.client("s3", region_name=aws_credentials)
        assert [
            obj["Key"] for obj in s3.list_objects_v2(Bucket=bucket)["Contents"]
        ] == ["bare/input/only.pdf"]

    @mock_aws
    def test_the_clients_default_stack_does_not_satisfy_the_test_set_guard(
        self, tmp_path, aws_credentials
    ):
        """DEFECT — `operations/manifest.py:55-56`.

        The guard is `if test_set and not stack_name`, testing the *parameter*
        rather than the resolved stack. Twenty lines later the same method calls
        `self._client._require_stack(stack_name)`, which would happily have used
        the client's default — so a client constructed with `stack_name=` (the
        documented usage pattern, and what every other stack operation accepts)
        is refused here, and the message tells the caller to supply the very
        thing they already supplied.

        The test asserts both halves: the refusal, and that passing the same
        name through as an argument succeeds against the same client and the same
        stack. Only the guard is wrong; the machinery behind it is fine.
        """
        _create_stack("idp-default", STACK_TEMPLATE, aws_credentials)

        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.pdf").write_bytes(b"%PDF")
        client = IDPClient(stack_name="idp-default", region=aws_credentials)

        with pytest.raises(IDPConfigurationError, match="stack_name is required"):
            client.manifest.generate(directory=str(docs), test_set="ts")

        assert (
            client.manifest.generate(
                directory=str(docs), test_set="ts", stack_name="idp-default"
            ).test_set_created
            is True
        )


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_generated_manifest_validates(tmp_path):
    """The two halves of this module have to agree on the CSV they exchange.

    `generate` writes the file and `validate` reads it; a column rename on
    either side would pass that side's own tests and break this.
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.pdf").write_bytes(b"%PDF")
    (docs / "b.pdf").write_bytes(b"%PDF")
    baselines = tmp_path / "baselines"
    (baselines / "a.pdf").mkdir(parents=True)

    client = IDPClient()
    output = tmp_path / "m.csv"
    client.manifest.generate(
        directory=str(docs), baseline_dir=str(baselines), output=str(output)
    )

    result = client.manifest.validate(str(output))
    assert result.valid is True, result.error
    assert result.document_count == 2
    assert result.has_baselines is True
    assert os.path.isfile(output)
