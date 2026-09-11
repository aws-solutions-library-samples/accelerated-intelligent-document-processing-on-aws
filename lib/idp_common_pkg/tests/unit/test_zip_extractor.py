# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# Import the actual Lambda handler module so the tests exercise the real
# baseline-matching logic instead of a duplicated copy.
_LAMBDA_INDEX = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "lambda"
    / "test_set_zip_extractor"
    / "index.py"
)
_spec = importlib.util.spec_from_file_location(
    "test_set_zip_extractor_index", _LAMBDA_INDEX
)
zip_extractor = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = zip_extractor
_spec.loader.exec_module(zip_extractor)


def _collect_names(mock_files):
    """Run the extractor's two-pass partition + baseline resolution.

    Every step that decides *which* files count now goes through the Lambda's own
    ``classify_zip_entry``, not a copy of it. The previous version of this helper
    reimplemented the partition inline as ``if "/input/" in file_path`` and only called
    the real ``_match_baseline_name`` — so the production leading-slash bug, which made
    the documented root-level zip layout extract zero files, was invisible to this
    entire file by construction. A test that mirrors the implementation cannot fail with
    it.
    """
    input_names = set()
    baseline_names = set()
    baseline_files = []

    # First pass: collect input names, stash baseline files.
    for file_info in mock_files:
        if file_info.is_dir():
            continue
        role, relative = zip_extractor.classify_zip_entry(file_info.filename)
        if role == "input":
            input_names.add(relative.split("/")[-1])
        elif role == "baseline":
            baseline_files.append(file_info)

    # Second pass: resolve baseline directory names against known inputs.
    for file_info in baseline_files:
        _role, relative = zip_extractor.classify_zip_entry(file_info.filename)
        if "/" in relative:
            path_parts = relative.split("/")
            if len(path_parts) >= 2:
                name = zip_extractor._match_baseline_name(path_parts, input_names)
                if name:
                    baseline_names.add(name)

    return input_names, baseline_names


@pytest.mark.unit
def test_file_validation_logic():
    """Test the file validation logic for input and baseline matching (PDF)"""

    mock_files = [
        Mock(filename="my-test-set/input/document1.pdf", is_dir=lambda: False),
        Mock(filename="my-test-set/input/document2.pdf", is_dir=lambda: False),
        Mock(
            filename="my-test-set/baseline/document1.pdf/sections/result.json",
            is_dir=lambda: False,
        ),
        Mock(
            filename="my-test-set/baseline/document1.pdf/metadata.json",
            is_dir=lambda: False,
        ),
        Mock(
            filename="my-test-set/baseline/document2.pdf/extraction.json",
            is_dir=lambda: False,
        ),
        # Directory entries (should be ignored)
        Mock(filename="my-test-set/input/", is_dir=lambda: True),
        Mock(filename="my-test-set/baseline/", is_dir=lambda: True),
    ]

    input_names, baseline_names = _collect_names(mock_files)

    assert input_names == {"document1.pdf", "document2.pdf"}
    assert baseline_names == {"document1.pdf", "document2.pdf"}
    assert not (input_names - baseline_names)
    assert not (baseline_names - input_names)


@pytest.mark.unit
def test_complex_file_structure():
    """Test with complex nested structure like the real S3 bucket"""

    mock_files = [
        Mock(
            filename="fcc_benchmark/input/fcc_benchmark/033f718b16cb597c065930410752c294.pdf",
            is_dir=lambda: False,
        ),
        Mock(
            filename="fcc_benchmark/input/fcc_benchmark/03f65053aea282ad8d5e759a9f18bdbb.pdf",
            is_dir=lambda: False,
        ),
        Mock(
            filename="fcc_benchmark/baseline/fcc_benchmark/033f718b16cb597c065930410752c294.pdf/sections/1/result.json",
            is_dir=lambda: False,
        ),
        Mock(
            filename="fcc_benchmark/baseline/fcc_benchmark/03f65053aea282ad8d5e759a9f18bdbb.pdf/sections/1/result.json",
            is_dir=lambda: False,
        ),
    ]

    input_names, baseline_names = _collect_names(mock_files)

    expected_names = {
        "033f718b16cb597c065930410752c294.pdf",
        "03f65053aea282ad8d5e759a9f18bdbb.pdf",
    }
    assert input_names == expected_names
    assert baseline_names == expected_names


@pytest.mark.unit
@pytest.mark.parametrize(
    "ext",
    ["png", "jpg", "jpeg", "tiff", "tif"],
)
def test_non_pdf_documents_flat(ext):
    """Regression for issue #380: non-PDF documents must match baselines."""

    mock_files = [
        Mock(filename=f"imgset/input/document1.{ext}", is_dir=lambda: False),
        Mock(filename=f"imgset/input/document2.{ext}", is_dir=lambda: False),
        Mock(
            filename=f"imgset/baseline/document1.{ext}/sections/1/result.json",
            is_dir=lambda: False,
        ),
        Mock(
            filename=f"imgset/baseline/document2.{ext}/sections/1/result.json",
            is_dir=lambda: False,
        ),
    ]

    input_names, baseline_names = _collect_names(mock_files)

    assert input_names == {f"document1.{ext}", f"document2.{ext}"}
    assert baseline_names == input_names
    assert not (input_names - baseline_names)


@pytest.mark.unit
def test_non_pdf_documents_nested_with_dotted_category():
    """Non-PDF docs in a nested layout under a dotted category folder.

    A category folder containing a dot must not be mistaken for the document
    directory; matching against known input names avoids that false positive.
    """

    mock_files = [
        Mock(
            filename="ds/input/my.category/scan1.png",
            is_dir=lambda: False,
        ),
        Mock(
            filename="ds/baseline/my.category/scan1.png/sections/1/result.json",
            is_dir=lambda: False,
        ),
    ]

    input_names, baseline_names = _collect_names(mock_files)

    assert input_names == {"scan1.png"}
    assert baseline_names == {"scan1.png"}


@pytest.mark.unit
def test_orphaned_non_pdf_baseline_reported_as_extra():
    """A baseline with no matching input is still surfaced (fallback path)."""

    mock_files = [
        Mock(filename="ds/input/document1.png", is_dir=lambda: False),
        Mock(
            filename="ds/baseline/document1.png/sections/1/result.json",
            is_dir=lambda: False,
        ),
        Mock(
            filename="ds/baseline/orphan.tiff/sections/1/result.json",
            is_dir=lambda: False,
        ),
    ]

    input_names, baseline_names = _collect_names(mock_files)

    assert input_names == {"document1.png"}
    # orphan.tiff has no matching input but is caught by the extension fallback
    assert baseline_names == {"document1.png", "orphan.tiff"}
    assert (baseline_names - input_names) == {"orphan.tiff"}


@pytest.mark.unit
class TestClassifyZipEntry:
    """The partition, which decides whether a zip yields any documents at all.

    The bug this pins: ``'/input/' in file_path`` accepted only zips with a wrapping
    folder, so the layout the wizard's own diagram documents — ``input/`` at the zip root
    — matched nothing and the set reported 0 documents. Every fixture in this file was
    wrapped, so nothing failed.
    """

    def test_the_documented_root_level_layout_is_accepted(self):
        # Exactly the wizard's REQUIRED_STRUCTURE diagram. This is the case that
        # silently produced an empty test set.
        assert zip_extractor.classify_zip_entry("input/document1.pdf") == (
            "input",
            "document1.pdf",
        )
        assert zip_extractor.classify_zip_entry(
            "baseline/document1.pdf/sections/1/result.json"
        ) == ("baseline", "document1.pdf/sections/1/result.json")

    def test_a_wrapped_layout_still_works(self):
        # What the four pre-deployed HuggingFace sets ship, and the only shape that
        # worked before. Breaking it would break every benchmark deploy.
        assert zip_extractor.classify_zip_entry("my-test-set/input/document1.pdf") == (
            "input",
            "document1.pdf",
        )
        assert zip_extractor.classify_zip_entry(
            "fcc_benchmark/baseline/fcc_benchmark/a.pdf/sections/1/result.json"
        ) == ("baseline", "fcc_benchmark/a.pdf/sections/1/result.json")

    def test_nested_folders_below_the_role_are_preserved(self):
        # The relative path is what the destination key is built from, so a category
        # folder has to survive intact.
        assert zip_extractor.classify_zip_entry("s/input/category/doc.png") == (
            "input",
            "category/doc.png",
        )

    def test_macos_archive_noise_is_not_taken_for_a_document(self):
        """`__MACOSX/input/._doc.pdf` contains `/input/`, so it used to be accepted.

        It would then fail validation as a baseline missing for a document nobody
        added — the next thing a macOS user would hit after the partition fix.
        """
        assert zip_extractor.classify_zip_entry("__MACOSX/input/._document1.pdf") == (
            None,
            "",
        )
        assert zip_extractor.classify_zip_entry("input/._document1.pdf") == (None, "")

    def test_entries_in_neither_folder_are_rejected(self):
        assert zip_extractor.classify_zip_entry("README.md") == (None, "")
        assert zip_extractor.classify_zip_entry("notes/readme.txt") == (None, "")

    def test_a_bare_role_directory_entry_yields_nothing(self):
        # `input/` on its own names no file; accepting it would add an empty filename
        # to input_names and demand a baseline for it.
        assert zip_extractor.classify_zip_entry("input/") == (None, "")
        assert zip_extractor.classify_zip_entry("wrapper/baseline/") == (None, "")

    def test_the_first_role_segment_wins(self):
        # A document legitimately named "baseline" inside input/ must stay an input.
        assert zip_extractor.classify_zip_entry("input/baseline/doc.pdf") == (
            "input",
            "baseline/doc.pdf",
        )


@pytest.mark.unit
def test_root_level_zip_partitions_into_both_roles():
    """End-to-end over the helper, in the shape the reported zip actually had."""
    mock_files = [
        Mock(filename="input/document1.pdf", is_dir=lambda: False),
        Mock(filename="input/document2.pdf", is_dir=lambda: False),
        Mock(
            filename="baseline/document1.pdf/sections/1/result.json",
            is_dir=lambda: False,
        ),
        Mock(
            filename="baseline/document2.pdf/sections/1/result.json",
            is_dir=lambda: False,
        ),
        Mock(filename="input/", is_dir=lambda: True),
        Mock(filename="__MACOSX/input/._document1.pdf", is_dir=lambda: False),
    ]

    input_names, baseline_names = _collect_names(mock_files)

    assert input_names == {"document1.pdf", "document2.pdf"}
    assert baseline_names == {"document1.pdf", "document2.pdf"}
    # Which is what makes validation pass: no missing and no extra baselines.
    assert input_names - baseline_names == set()
    assert baseline_names - input_names == set()


def _zip_bytes(entries):
    """A real in-memory zip, so the extraction path runs against zipfile itself."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in entries.items():
            zf.writestr(name, body)
    return buf.getvalue()


def _extract_and_capture(entries, test_set_id="ts1"):
    """Run _extract_uploaded_zip over `entries`, returning the S3 keys written."""
    payload = _zip_bytes(entries)

    def fake_download_fileobj(bucket, key, fileobj):
        fileobj.write(payload)

    fake_s3 = Mock()
    fake_s3.download_fileobj.side_effect = fake_download_fileobj
    with patch.object(zip_extractor, "s3", fake_s3):
        zip_extractor._extract_uploaded_zip("bkt", test_set_id, f"{test_set_id}/a.zip")
    return sorted(c.kwargs["Key"] for c in fake_s3.put_object.call_args_list)


@pytest.mark.unit
class TestExtractionWritesFiles:
    """That the files are actually written, not merely classified.

    The extraction loops carried the same leading-slash split as the partition, guarded
    by ``if len(parts) == 2``. For a root-level zip that was false, so put_object was
    skipped in silence — validation would have passed and the set would still have been
    empty. Fixing the partition alone would not have fixed the bug.
    """

    _RESULT = json.dumps({"inference_result": {"f": "v"}})

    def test_a_root_level_zip_writes_input_and_baseline_objects(self):
        keys = _extract_and_capture(
            {
                "input/document1.pdf": b"pdf",
                "baseline/document1.pdf/sections/1/result.json": self._RESULT,
            }
        )

        assert keys == [
            "ts1/baseline/document1.pdf/sections/1/result.json",
            "ts1/input/document1.pdf",
        ]

    def test_a_wrapped_zip_writes_the_same_keys(self):
        # The wrapping folder must not leak into the destination key, which is what
        # the pre-deployed sets already rely on.
        keys = _extract_and_capture(
            {
                "my-set/input/document1.pdf": b"pdf",
                "my-set/baseline/document1.pdf/sections/1/result.json": self._RESULT,
            }
        )

        assert keys == [
            "ts1/baseline/document1.pdf/sections/1/result.json",
            "ts1/input/document1.pdf",
        ]

    def test_macos_noise_is_not_written(self):
        keys = _extract_and_capture(
            {
                "input/document1.pdf": b"pdf",
                "__MACOSX/input/._document1.pdf": b"junk",
                "baseline/document1.pdf/sections/1/result.json": self._RESULT,
            }
        )

        assert not any("._document1" in k or "__MACOSX" in k for k in keys)
        assert len(keys) == 2
