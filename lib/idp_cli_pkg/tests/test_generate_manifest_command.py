# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for `idp-cli generate-manifest` and `idp-cli validate-manifest`.

`generate-manifest` scans either a local directory or an S3 prefix, optionally
matches a directory of baselines against the documents it found, optionally uploads
both into a Test Studio test set, and writes a CSV manifest. That CSV is the input
to `idp-cli process`, `idp-cli validate-manifest` and the evaluation path, so the
bytes on disk are the contract and that is what these tests assert: they write the
manifest into `tmp_path`, read it back off the filesystem, and check the header, the
column order, the quoting of a value containing a comma, the line terminator and the
row set. Several also feed the generated file straight back into the SDK parser the
downstream commands use (`idp_sdk._core.manifest_parser`), because "the command
exited 0" and "the file it wrote can be read" are different claims.

The other half of the file is the refusals. `generate-manifest` has six mutually
dependent options and validates them in a fixed order, and the order is observable:
invoked with no options at all it complains about `--output` rather than about the
missing input source. Those checks are pinned individually with their exit codes,
because a validation that is skipped or reordered turns a clear refusal into a
confusing failure much further into the run — after files have already been uploaded,
in the `--test-set` case.

Local scanning and S3 scanning now apply the same predicate, `cli._file_pattern_matches`,
which matches the whole pattern against a file's base name and ignores case unless
`--case-sensitive` is given. Both directions are pinned on both paths --
`test_an_uppercase_extension_is_selected_by_a_lowercase_pattern` and its S3 twin for
the default, `test_case_sensitive_restores_exact_matching_locally` and its twin for
the opt-out -- because the case that produced the defect (a corpus of `.PDF` against
the default `*.pdf`) exits 0 either way, so only the row set distinguishes them.

AWS is either `moto` or absent. The `--test-set` path needs a stack lookup, and that
one call (`IDPClient._get_stack_resources`) is patched because it reads a real
CloudFormation stack; everything the command then does with the bucket it was handed
runs against a real moto bucket, and the objects are read back off it.
"""

import csv
import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
from click.testing import CliRunner
from moto import mock_aws

TEST_SET_BUCKET = "idp-test-set-bucket"


@pytest.fixture
def runner():
    return CliRunner()


def _write(path, text="%PDF-1.4 fake\n"):
    """Create a file (and any missing parents) with some non-empty content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _read_manifest_rows(path):
    """Parse a generated manifest back into a list of dicts, via the csv module."""
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


def _patched_stack_resources(resources):
    """Patch `idp_cli.cli.IDPClient` so `_get_stack_resources` returns `resources`.

    `generate-manifest --test-set` builds an IDPClient purely to look the test set
    bucket up out of a deployed stack's resources. That lookup describes a real
    CloudFormation stack, so it is the one thing patched here; the S3 work the command
    does afterwards runs against moto and is asserted by reading the bucket back.
    """
    client = MagicMock()
    client._get_stack_resources.return_value = resources
    return patch("idp_cli.cli.IDPClient", return_value=client)


# --------------------------------------------------------------------------------
# Option validation. Each of these must refuse before doing any work at all.
# --------------------------------------------------------------------------------


@pytest.mark.unit
def test_output_is_required_when_not_creating_a_test_set(runner, tmp_path):
    """Without `--test-set` the command has nowhere to put its result, so it refuses."""
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "a.pdf")

    result = runner.invoke(generate_manifest, ["--dir", str(docs)])

    assert result.exit_code == 1, result.output
    assert "--output is required when not using --test-set" in result.output


@pytest.mark.unit
def test_the_output_check_runs_before_the_input_source_check(runner):
    """Invoked with no options at all, the message names `--output`, not `--dir`.

    The order these validations run in is observable and worth pinning: a user who
    typed nothing is told about the output file first. If that order is ever changed
    the message a bare invocation produces changes with it, so this test is the record
    of which message is the current one.
    """
    from idp_cli.cli import generate_manifest

    result = runner.invoke(generate_manifest, [])

    assert result.exit_code == 1, result.output
    assert "--output is required when not using --test-set" in result.output
    assert "Must specify either --dir or --s3-uri" not in result.output


@pytest.mark.unit
def test_neither_dir_nor_s3_uri_is_refused(runner, tmp_path):
    from idp_cli.cli import generate_manifest

    output = tmp_path / "manifest.csv"
    result = runner.invoke(generate_manifest, ["--output", str(output)])

    assert result.exit_code == 1, result.output
    assert "Must specify either --dir or --s3-uri" in result.output
    assert not output.exists(), (
        "no manifest should be written when the input is refused"
    )


@pytest.mark.unit
def test_both_dir_and_s3_uri_is_refused(runner, tmp_path):
    """Two input sources is ambiguous rather than additive, so it is refused."""
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "a.pdf")
    output = tmp_path / "manifest.csv"

    result = runner.invoke(
        generate_manifest,
        [
            "--dir",
            str(docs),
            "--s3-uri",
            "s3://bucket/prefix/",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 1, result.output
    assert "Cannot specify both --dir and --s3-uri" in result.output
    assert not output.exists()


@pytest.mark.unit
def test_test_set_requires_a_stack_name(runner, tmp_path):
    """The test set bucket is read out of a stack, so the stack name is mandatory."""
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "a.pdf")

    result = runner.invoke(
        generate_manifest, ["--dir", str(docs), "--test-set", "my set"]
    )

    assert result.exit_code == 1, result.output
    assert "--stack-name is required when using --test-set" in result.output


@pytest.mark.unit
def test_test_set_requires_a_baseline_dir(runner, tmp_path):
    """A test set exists to be evaluated, so it refuses to create one with no baselines."""
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "a.pdf")

    result = runner.invoke(
        generate_manifest,
        ["--dir", str(docs), "--test-set", "my set", "--stack-name", "IDP"],
    )

    assert result.exit_code == 1, result.output
    assert "--baseline-dir is required when using --test-set" in result.output


@pytest.mark.unit
def test_test_set_refuses_an_s3_source(runner, tmp_path):
    """`--test-set` uploads local files, so an S3 source is refused rather than ignored."""
    from idp_cli.cli import generate_manifest

    baselines = tmp_path / "baselines"
    baselines.mkdir()

    result = runner.invoke(
        generate_manifest,
        [
            "--s3-uri",
            "s3://bucket/prefix/",
            "--baseline-dir",
            str(baselines),
            "--test-set",
            "my set",
            "--stack-name",
            "IDP",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "--test-set requires --dir (not --s3-uri)" in result.output


@pytest.mark.unit
def test_a_non_s3_uri_is_refused(runner, tmp_path):
    """Anything not starting with `s3://` is refused before a client is built."""
    from idp_cli.cli import generate_manifest

    output = tmp_path / "manifest.csv"
    result = runner.invoke(
        generate_manifest,
        [
            "--s3-uri",
            "https://bucket.s3.amazonaws.com/prefix/",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 1, result.output
    assert "Invalid S3 URI" in result.output
    assert not output.exists()


@pytest.mark.unit
def test_a_nonexistent_dir_is_rejected_by_click_with_exit_2(runner, tmp_path):
    """`--dir` is a `click.Path(exists=True)`, so click refuses it with usage exit 2.

    Worth distinguishing from the command's own exit 1: a caller scripting around this
    CLI sees two different codes for two different classes of mistake.
    """
    from idp_cli.cli import generate_manifest

    result = runner.invoke(
        generate_manifest,
        ["--dir", str(tmp_path / "missing"), "--output", str(tmp_path / "m.csv")],
    )

    assert result.exit_code == 2, result.output


@pytest.mark.unit
def test_a_file_passed_as_dir_is_rejected_by_click(runner, tmp_path):
    """`file_okay=False`: a path that exists but is a file is still a usage error."""
    from idp_cli.cli import generate_manifest

    a_file = _write(tmp_path / "a.pdf")

    result = runner.invoke(
        generate_manifest,
        ["--dir", str(a_file), "--output", str(tmp_path / "m.csv")],
    )

    assert result.exit_code == 2, result.output


# --------------------------------------------------------------------------------
# The CSV the command writes. Read back off disk, byte for byte where it matters.
# --------------------------------------------------------------------------------


@pytest.mark.unit
def test_manifest_header_column_order_and_line_terminator(runner, tmp_path):
    """Pin the exact bytes of a single-document manifest.

    The header names and their order are what every downstream reader keys on, and the
    `csv` module's default dialect writes CRLF line endings, which a hand-rolled
    reader splitting on `\\n` would leave a trailing `\\r` on. Both are part of the
    on-disk contract, so both are asserted literally rather than through a parser that
    would normalise them away.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    doc = _write(docs / "invoice.pdf")
    output = tmp_path / "manifest.csv"

    result = runner.invoke(
        generate_manifest, ["--dir", str(docs), "--output", str(output)]
    )

    assert result.exit_code == 0, result.output
    raw = output.read_bytes()
    assert raw == f"document_path,baseline_source\r\n{doc},\r\n".encode()


@pytest.mark.unit
def test_a_path_containing_a_comma_is_quoted(runner, tmp_path):
    """A comma in a filename must be quoted or every later reader mis-splits the row.

    A failure here means the manifest has three fields where it declared two, and the
    document path silently becomes a truncated prefix of itself.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    doc = _write(docs / "statement,final.pdf")
    output = tmp_path / "manifest.csv"

    result = runner.invoke(
        generate_manifest, ["--dir", str(docs), "--output", str(output)]
    )

    assert result.exit_code == 0, result.output
    assert f'"{doc}",' in output.read_text()

    rows = _read_manifest_rows(output)
    assert len(rows) == 1
    assert rows[0]["document_path"] == str(doc)
    assert rows[0]["baseline_source"] == ""


@pytest.mark.unit
def test_recursive_scan_selects_exactly_the_matching_files(runner, tmp_path):
    """A directory holding a .pdf, a .txt, a subdirectory and a directory named `*.pdf`.

    Four things have to be true at once: the nested `.pdf` is included (the default is
    recursive), the `.txt` is excluded by the pattern, the top-level `.pdf` is included
    even though the glob goes through `**`, and the *directory* called `archive.pdf` is
    excluded by the `os.path.isfile` guard rather than being written in as a document.
    That last one is the interesting case: without the guard the manifest would name a
    directory and the upload step would fail much later.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    top = _write(docs / "top.pdf")
    nested = _write(docs / "sub" / "nested.pdf")
    _write(docs / "notes.txt")
    (docs / "archive.pdf").mkdir(parents=True)
    output = tmp_path / "manifest.csv"

    result = runner.invoke(
        generate_manifest, ["--dir", str(docs), "--output", str(output)]
    )

    assert result.exit_code == 0, result.output
    assert "Found 2 documents" in result.output
    rows = _read_manifest_rows(output)
    assert {row["document_path"] for row in rows} == {str(top), str(nested)}
    assert len(rows) == 2


@pytest.mark.unit
def test_no_recursive_excludes_subdirectories(runner, tmp_path):
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    top = _write(docs / "top.pdf")
    _write(docs / "sub" / "nested.pdf")
    output = tmp_path / "manifest.csv"

    result = runner.invoke(
        generate_manifest,
        ["--dir", str(docs), "--output", str(output), "--no-recursive"],
    )

    assert result.exit_code == 0, result.output
    rows = _read_manifest_rows(output)
    assert [row["document_path"] for row in rows] == [str(top)]


@pytest.mark.unit
def test_an_uppercase_extension_is_selected_by_a_lowercase_pattern(runner, tmp_path):
    """The default `*.pdf` selects `.PDF` too, on the local scan (issue #1231 item 4).

    This is the case that produced the defect: a corpus exported from a system that
    uppercases extensions, scanned with the default pattern, produced a manifest
    missing every document at exit 0 and with no warning. The exit code cannot tell
    the two behaviours apart, so the row set is what is asserted, in both directions
    -- the lowercase file is still selected and the uppercase one now is as well.

    `*.PDF` is asserted as well, because the fix has to be symmetric: folding case on
    only one side of the comparison would make an uppercase pattern the new silent
    filter.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    lower = _write(docs / "lower.pdf")
    upper = _write(docs / "UPPER.PDF")
    mixed = _write(docs / "Mixed.Pdf")
    output = tmp_path / "manifest.csv"

    result = runner.invoke(
        generate_manifest, ["--dir", str(docs), "--output", str(output)]
    )

    assert result.exit_code == 0, result.output
    rows = _read_manifest_rows(output)
    assert {row["document_path"] for row in rows} == {
        str(lower),
        str(upper),
        str(mixed),
    }
    assert "Found 3 documents" in result.output

    # An uppercase pattern has to select the same three, or the fold is one-sided.
    upper_output = tmp_path / "upper.csv"
    upper_result = runner.invoke(
        generate_manifest,
        ["--dir", str(docs), "--output", str(upper_output), "--file-pattern", "*.PDF"],
    )
    assert upper_result.exit_code == 0, upper_result.output
    assert {row["document_path"] for row in _read_manifest_rows(upper_output)} == {
        str(lower),
        str(upper),
        str(mixed),
    }


@pytest.mark.unit
def test_an_uppercase_corpus_still_gets_its_lowercase_baselines_attached(
    runner, tmp_path
):
    """Selecting documents case-insensitively is useless if baselines still match exactly.

    This is the corpus the case-folding fix exists for -- documents exported from a
    system that uppercases extensions -- and folding only the *selection* moved the
    silent loss one layer down instead of removing it. `.PDF` documents beside
    lowercase baseline directories were selected, matched nothing, and produced a test
    set with a full `input/`, baselines under keys nothing referenced, and every
    manifest row's `baseline_source` empty, at exit 0 with `Matched 0/2` mid-output as
    the only sign. Before folding, that combination exited 1 with "No documents found",
    so the fix replaced a refusal the user cannot miss with a success report over a test
    set an evaluation cannot score.

    The S3 keys are asserted, not just the match count, because the backend pairs a
    baseline to its document by the exact prefix `baseline/<input file name>/`
    (`src/lambda/test_file_copier`). A baseline uploaded under the *directory's*
    spelling would satisfy a count-based check and still score nothing, so the upload
    has to land under the document's name.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "W2-A.PDF")
    _write(docs / "W2-B.PDF")
    baselines = tmp_path / "baselines"
    _write(baselines / "w2-a.pdf" / "expected.json", '{"a": 1}')
    _write(baselines / "w2-b.pdf" / "expected.json", '{"b": 2}')
    output = tmp_path / "m.csv"

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(
            Bucket=TEST_SET_BUCKET
        )
        with _patched_stack_resources({"TestSetBucket": TEST_SET_BUCKET}):
            result = runner.invoke(
                generate_manifest,
                [
                    "--dir",
                    str(docs),
                    "--baseline-dir",
                    str(baselines),
                    "--test-set",
                    "set1",
                    "--stack-name",
                    "IDP",
                    "--output",
                    str(output),
                ],
            )

        keys = {
            obj["Key"]
            for obj in boto3.client("s3", region_name="us-east-1")
            .list_objects_v2(Bucket=TEST_SET_BUCKET)
            .get("Contents", [])
        }

    assert result.exit_code == 0, result.output
    assert "Matched 2/2 documents to baselines" in result.output
    assert "none matched a document" not in result.output

    # The baseline objects sit under the *document's* name, which is the prefix the
    # backend pairs on -- not under `w2-a.pdf/`, the directory's spelling.
    assert keys == {
        "set1/input/W2-A.PDF",
        "set1/input/W2-B.PDF",
        "set1/baseline/W2-A.PDF/expected.json",
        "set1/baseline/W2-B.PDF/expected.json",
    }, keys

    rows = {
        row["document_path"]: row["baseline_source"]
        for row in _read_manifest_rows(output)
    }
    assert rows == {
        f"s3://{TEST_SET_BUCKET}/set1/input/W2-A.PDF": f"s3://{TEST_SET_BUCKET}/set1/baseline/W2-A.PDF/",
        f"s3://{TEST_SET_BUCKET}/set1/input/W2-B.PDF": f"s3://{TEST_SET_BUCKET}/set1/baseline/W2-B.PDF/",
    }, rows
    assert "" not in rows.values(), "every row must name a baseline prefix"


@pytest.mark.unit
def test_case_sensitive_governs_baseline_matching_too(runner, tmp_path):
    """One flag, one rule: `--case-sensitive` narrows selection and matching together.

    A flag that folded the selection but not the matching, or the reverse, would give an
    invocation that selects a document and then cannot label it -- which is the defect
    this pair of assertions exists to prevent. With the flag the uppercase document is
    not selected at all, so the run refuses; without it both selection and matching
    fold and the run succeeds.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "W2-A.PDF")
    baselines = tmp_path / "baselines"
    _write(baselines / "w2-a.pdf" / "expected.json", '{"a": 1}')
    output = tmp_path / "m.csv"

    exact = runner.invoke(
        generate_manifest,
        [
            "--dir",
            str(docs),
            "--baseline-dir",
            str(baselines),
            "--output",
            str(output),
            "--case-sensitive",
        ],
    )
    assert exact.exit_code == 1, exact.output
    assert "No documents found" in exact.output

    folded = runner.invoke(
        generate_manifest,
        ["--dir", str(docs), "--baseline-dir", str(baselines), "--output", str(output)],
    )
    assert folded.exit_code == 0, folded.output
    assert "Matched 1/1 documents to baselines" in folded.output
    assert [row["baseline_source"] for row in _read_manifest_rows(output)] == [
        str(baselines / "w2-a.pdf")
    ]


@pytest.mark.unit
def test_two_baseline_directories_differing_only_in_case_are_refused(runner, tmp_path):
    """Two candidate ground truths for one document is ambiguous, so it is refused.

    Folding the baseline index makes `gt.pdf/` and `GT.PDF/` collide, and either choice
    would be arbitrary -- the document would be scored against labels nobody chose. The
    refusal lands before any upload or clear, and it names both directories and the flag
    that matches them exactly.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "gt.pdf")
    baselines = tmp_path / "baselines"
    _write(baselines / "gt.pdf" / "a.json", "{}")
    _write(baselines / "GT.PDF" / "b.json", "{}")
    output = tmp_path / "m.csv"

    result = runner.invoke(
        generate_manifest,
        ["--dir", str(docs), "--baseline-dir", str(baselines), "--output", str(output)],
    )

    assert result.exit_code == 1, result.output
    assert "differ only in case" in result.output
    assert "GT.PDF" in result.output and "gt.pdf" in result.output
    assert "--case-sensitive" in result.output
    assert not output.exists(), "nothing is written when the baselines are ambiguous"

    # `--case-sensitive` is the stated route out, so it must actually work.
    exact = runner.invoke(
        generate_manifest,
        [
            "--dir",
            str(docs),
            "--baseline-dir",
            str(baselines),
            "--output",
            str(output),
            "--case-sensitive",
        ],
    )
    assert exact.exit_code == 0, exact.output
    assert [row["baseline_source"] for row in _read_manifest_rows(output)] == [
        str(baselines / "gt.pdf")
    ]


@pytest.mark.unit
def test_a_test_set_whose_baselines_all_miss_is_refused_before_anything_is_cleared(
    runner, tmp_path, api_calls
):
    """`--test-set` with baselines that match nothing refuses, and writes nothing at all.

    `--baseline-dir` is mandatory with `--test-set`, so baselines that match no document
    are unambiguously a mistake rather than a deliberately unlabeled set. Continuing
    would **clear the existing test set** and then upload one whose every
    `baseline_source` is empty, so the refusal has to come before the clear -- which is
    asserted from the recorded API calls, not from the exit code, because an exit 1
    after the clear destroys the previous test set's baselines just the same.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "invoice.pdf")
    baselines = tmp_path / "baselines"
    _write(baselines / "unrelated.pdf" / "expected.json", "{}")

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)
        s3.put_object(Bucket=TEST_SET_BUCKET, Key="set1/input/old.pdf", Body=b"old")

        with _patched_stack_resources({"TestSetBucket": TEST_SET_BUCKET}):
            result = runner.invoke(
                generate_manifest,
                [
                    "--dir",
                    str(docs),
                    "--baseline-dir",
                    str(baselines),
                    "--test-set",
                    "set1",
                    "--stack-name",
                    "IDP",
                    "--force",
                ],
            )

        keys = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=TEST_SET_BUCKET).get("Contents", [])
        }

    assert result.exit_code == 1, result.output
    assert "none matched a document" in result.output
    assert "invoice.pdf" in result.output, (
        "the message shows the naming that is expected"
    )

    assert api_calls.of("DeleteObjects") == [], (
        "the existing test set must not be cleared"
    )
    # The only recorded PutObject is this test's own seeding of the previous test set,
    # so the command wrote nothing -- not the marker, not an input, not a baseline.
    assert {call.params["Key"] for call in api_calls.of("PutObject")} == {
        "set1/input/old.pdf"
    }, [call.params["Key"] for call in api_calls.of("PutObject")]
    assert keys == {"set1/input/old.pdf"}, "the previous test set survives untouched"


@pytest.mark.unit
def test_case_sensitive_restores_exact_matching_locally(runner, tmp_path):
    """`--case-sensitive` is the route for a pattern whose case is deliberate.

    Folding case by default is the safe direction for the reported defect, but it
    takes a capability away from a user who cased a pattern on purpose -- a corpus
    holding both `Invoice-*.pdf` and `invoice-*.pdf` as different document families
    is the shape. The flag gives that back, and it is asserted here on a *prefix*
    rather than an extension, since the prefix is the part a user is most likely to
    have meant literally.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    capitalised = _write(docs / "Invoice-01.pdf")
    _write(docs / "invoice-02.pdf")
    output = tmp_path / "manifest.csv"

    result = runner.invoke(
        generate_manifest,
        [
            "--dir",
            str(docs),
            "--output",
            str(output),
            "--file-pattern",
            "Invoice-*.pdf",
            "--case-sensitive",
        ],
    )

    assert result.exit_code == 0, result.output
    assert [row["document_path"] for row in _read_manifest_rows(output)] == [
        str(capitalised)
    ]

    # Without the flag the same pattern takes both, which is the behaviour the flag
    # exists to opt out of. Asserted here so the contrast is one test's worth of fact.
    both_output = tmp_path / "both.csv"
    both = runner.invoke(
        generate_manifest,
        [
            "--dir",
            str(docs),
            "--output",
            str(both_output),
            "--file-pattern",
            "Invoice-*.pdf",
        ],
    )
    assert both.exit_code == 0, both.output
    assert len(_read_manifest_rows(both_output)) == 2


@pytest.mark.unit
def test_a_file_pattern_naming_a_directory_is_refused(runner, tmp_path):
    """`--file-pattern "sub/*.pdf"` is refused, on both scan paths.

    The pattern applies to a base name, which is what the S3 scan always did: a
    pattern with a directory component matched nothing there and the only report was
    "No documents found", which is true and explains nothing. The local scan used to
    honour it, by joining the pattern onto the directory before globbing. Unifying the
    two on base-name matching costs that spelling, so the loss is a message naming the
    option that does the job rather than an empty scan the user has to diagnose.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "sub" / "nested.pdf")
    output = tmp_path / "manifest.csv"

    result = runner.invoke(
        generate_manifest,
        ["--dir", str(docs), "--output", str(output), "--file-pattern", "sub/*.pdf"],
    )

    assert result.exit_code == 1, result.output
    assert "--file-pattern matches a file name, not a path" in result.output
    assert "--recursive" in result.output
    assert not output.exists(), "nothing is written when the options are refused"

    # Same refusal on the S3 path, where no client should even be built.
    s3_result = runner.invoke(
        generate_manifest,
        [
            "--s3-uri",
            "s3://docs-bucket/prefix/",
            "--output",
            str(output),
            "--file-pattern",
            "sub/*.pdf",
        ],
    )
    assert s3_result.exit_code == 1, s3_result.output
    assert "--file-pattern matches a file name, not a path" in s3_result.output

    # Where the new check sits in the validation order is observable, and this file
    # pins that order elsewhere, so pin this one too: with no `--output` the message
    # must still be about `--output`, not about the pattern.
    ordered = runner.invoke(
        generate_manifest, ["--dir", str(docs), "--file-pattern", "sub/*.pdf"]
    )
    assert ordered.exit_code == 1, ordered.output
    assert "--output is required" in ordered.output
    assert "--file-pattern matches a file name" not in ordered.output


@pytest.mark.unit
def test_a_hidden_file_is_still_excluded_unless_the_pattern_asks_for_one(
    runner, tmp_path
):
    """The dotfile rule survives the change from globbing the pattern to filtering.

    `glob` never matches a leading-dot name unless the pattern's own base name starts
    with one, and the scan now enumerates with `*` and filters afterwards, which would
    have thrown that rule away in both directions: `*.pdf` would start selecting
    editor droppings and lock files, and a pattern that deliberately names hidden
    files would stop finding them. Both halves are asserted, because each is a
    different line of the fix.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    visible = _write(docs / "visible.pdf")
    hidden = _write(docs / ".hidden.pdf")
    output = tmp_path / "manifest.csv"

    result = runner.invoke(
        generate_manifest, ["--dir", str(docs), "--output", str(output)]
    )
    assert result.exit_code == 0, result.output
    assert [row["document_path"] for row in _read_manifest_rows(output)] == [
        str(visible)
    ]

    hidden_output = tmp_path / "hidden.csv"
    hidden_result = runner.invoke(
        generate_manifest,
        [
            "--dir",
            str(docs),
            "--output",
            str(hidden_output),
            "--file-pattern",
            ".hidden*",
        ],
    )
    assert hidden_result.exit_code == 0, hidden_result.output
    assert [row["document_path"] for row in _read_manifest_rows(hidden_output)] == [
        str(hidden)
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("filename", "pattern", "folded", "exact"),
    [
        # (name, pattern, result with the default, result with --case-sensitive).
        # Both columns are written out rather than derived from `fnmatch`, because a
        # test that computes its expectation with the same library call as the code
        # under test agrees with that code however wrong both are.
        ("lower.pdf", "*.pdf", True, True),
        ("UPPER.PDF", "*.pdf", True, False),
        ("Mixed.Pdf", "*.pdf", True, False),
        ("lower.pdf", "*.PDF", True, False),
        ("notes.txt", "*.pdf", False, False),
        ("INVOICE01.PDF", "Invoice*.pdf", True, False),
        ("Invoice01.pdf", "Invoice*.pdf", True, True),
        ("W2-2024.pdf", "W2*.pdf", True, True),
        ("invoice-2024.pdf", "W2*.pdf", False, False),
        # A bracket class keeps working, because both sides are folded rather than the
        # pattern being rewritten into classes.
        ("doc.PDF", "*.[pP]df", True, False),
        ("doc.Pdf", "*.[pP]df", True, True),
        ("lending_package.pdf", "lending_package.pdf", True, True),
    ],
)
def test_the_file_pattern_predicate_folds_case_on_both_sides(
    filename, pattern, folded, exact
):
    """The rule, stated as cases: whole pattern against base name, case ignored."""
    from idp_cli.cli import _file_pattern_matches

    assert _file_pattern_matches(filename, pattern) is folded
    assert _file_pattern_matches(filename, pattern, case_sensitive=True) is exact


@pytest.mark.unit
def test_the_predicate_calls_fnmatchcase_and_not_fnmatch():
    """`fnmatch.fnmatch` and `fnmatch.fnmatchcase` are the same function on Linux.

    `fnmatch.fnmatch` routes through `os.path.normcase`, which is the identity on
    POSIX and lowercases on Windows. So on the host these tests run on, no input at
    all distinguishes the two calls, and a behavioural test of the difference is one
    that cannot fail here -- it would go green against either spelling and quietly
    make the predicate's result depend on the operating system that generated the
    manifest.

    The call site is therefore asserted directly, by reading the predicate's own
    source: `fnmatchcase` must be the attribute called, and `fnmatch.fnmatch` must
    not appear. That is the only shape of this assertion that discriminates.
    """
    import ast
    import inspect

    from idp_cli.cli import _file_pattern_matches

    tree = ast.parse(inspect.cleandoc(inspect.getsource(_file_pattern_matches)))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert called, "no attribute call found at all, so this assertion proves nothing"
    assert "fnmatchcase" in called, called
    assert "fnmatch" not in called, (
        "fnmatch.fnmatch is normcase-dependent, so the result would differ by host"
    )


@pytest.mark.unit
def test_a_custom_file_pattern_narrows_the_scan(runner, tmp_path):
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    w2 = _write(docs / "W2-2024.pdf")
    _write(docs / "invoice-2024.pdf")
    output = tmp_path / "manifest.csv"

    result = runner.invoke(
        generate_manifest,
        ["--dir", str(docs), "--output", str(output), "--file-pattern", "W2*.pdf"],
    )

    assert result.exit_code == 0, result.output
    assert [row["document_path"] for row in _read_manifest_rows(output)] == [str(w2)]


@pytest.mark.unit
def test_no_matching_files_refuses_and_writes_nothing(runner, tmp_path):
    """An empty scan is an error, not a header-only manifest.

    Refusing is the right call — a manifest with no documents is not useful and the
    mistake is nearly always a wrong `--file-pattern` — and the thing worth pinning is
    that no file is created at all, so a stale manifest from a previous run is left
    intact rather than being truncated to a header.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "notes.txt")
    output = tmp_path / "manifest.csv"

    result = runner.invoke(
        generate_manifest, ["--dir", str(docs), "--output", str(output)]
    )

    assert result.exit_code == 1, result.output
    assert "No documents found" in result.output
    assert not output.exists()


@pytest.mark.unit
def test_an_output_path_whose_parent_is_missing_exits_1(runner, tmp_path):
    """The directory is not created; the open fails and the command reports it.

    The scan has already happened by this point, so the failure comes after the "Found
    N documents" line. Exit code 1 and a message naming the path is the contract; what
    it must not do is exit 0 having written nothing.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "a.pdf")
    output = tmp_path / "does-not-exist" / "manifest.csv"

    result = runner.invoke(
        generate_manifest, ["--dir", str(docs), "--output", str(output)]
    )

    assert result.exit_code == 1, result.output
    assert "✗ Error:" in result.output
    assert "manifest.csv" in result.output
    assert not output.exists()


@pytest.mark.unit
def test_the_generated_manifest_is_accepted_by_the_downstream_parser(runner, tmp_path):
    """Round-trip: what `generate-manifest` writes, `validate-manifest` must accept.

    This is the one assertion that ties the two halves of this command pair together.
    A failure means the generator and the reader disagree about the format, which is
    the class of defect that surfaces as an unhelpful error in `idp-cli process` long
    after the manifest was produced.
    """
    from idp_cli.cli import generate_manifest
    from idp_sdk._core.manifest_parser import parse_manifest, validate_manifest

    docs = tmp_path / "docs"
    _write(docs / "one.pdf")
    _write(docs / "sub" / "two.pdf")
    output = tmp_path / "manifest.csv"

    result = runner.invoke(
        generate_manifest, ["--dir", str(docs), "--output", str(output)]
    )
    assert result.exit_code == 0, result.output

    is_valid, error = validate_manifest(str(output))
    assert is_valid, error
    parsed = parse_manifest(str(output))
    assert len(parsed) == 2
    assert {doc["type"] for doc in parsed} == {"local"}
    assert {doc["filename"] for doc in parsed} == {"one.pdf", "two.pdf"}


# --------------------------------------------------------------------------------
# Baseline matching.
# --------------------------------------------------------------------------------


@pytest.mark.unit
def test_baselines_match_on_the_full_filename_including_the_extension(runner, tmp_path):
    """A baseline directory must be named `invoice.pdf`, not `invoice`.

    The match is `os.path.basename(document_path) in baseline_map`, and the map is
    keyed on the baseline subdirectory names, so the extension is part of the key. A
    baseline laid out under the document's stem does not match and its row gets an
    empty `baseline_source`, which means the document is processed with no evaluation
    and nothing says so beyond the `Matched 1/2` count. Both cases are in one test so
    the difference is visible side by side.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    matched = _write(docs / "invoice.pdf")
    unmatched = _write(docs / "receipt.pdf")

    baselines = tmp_path / "baselines"
    with_extension = baselines / "invoice.pdf"
    (with_extension / "sections" / "1").mkdir(parents=True)
    (baselines / "receipt").mkdir(parents=True)  # stem only - does not match

    output = tmp_path / "manifest.csv"
    result = runner.invoke(
        generate_manifest,
        [
            "--dir",
            str(docs),
            "--baseline-dir",
            str(baselines),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Found 2 baseline directories" in result.output
    assert "Matched 1/2 documents to baselines" in result.output

    rows = {
        row["document_path"]: row["baseline_source"]
        for row in _read_manifest_rows(output)
    }
    assert rows[str(matched)] == str(with_extension)
    assert rows[str(unmatched)] == ""


@pytest.mark.unit
def test_a_file_in_the_baseline_dir_is_not_treated_as_a_baseline(runner, tmp_path):
    """Only subdirectories become baselines; a loose file next to them is ignored."""
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    doc = _write(docs / "invoice.pdf")
    baselines = tmp_path / "baselines"
    _write(baselines / "invoice.pdf", "{}")  # a FILE, not a directory
    output = tmp_path / "manifest.csv"

    result = runner.invoke(
        generate_manifest,
        ["--dir", str(docs), "--baseline-dir", str(baselines), "--output", str(output)],
    )

    assert result.exit_code == 0, result.output
    assert "Found 0 baseline directories" in result.output
    rows = _read_manifest_rows(output)
    assert rows == [{"document_path": str(doc), "baseline_source": ""}]


@pytest.mark.unit
def test_baseline_dir_is_ignored_for_an_s3_scan_and_says_so(runner, tmp_path):
    """`--baseline-dir` with `--s3-uri` warns and produces empty baseline columns.

    This is a warning rather than a refusal, so the manifest is still written — and
    every row's `baseline_source` is empty. A test that only checked the exit code
    would call this a success, so the column is read back off disk.
    """
    from idp_cli.cli import generate_manifest

    baselines = tmp_path / "baselines"
    (baselines / "a.pdf").mkdir(parents=True)
    output = tmp_path / "manifest.csv"

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="docs-bucket")
        s3.put_object(Bucket="docs-bucket", Key="prefix/a.pdf", Body=b"pdf")

        result = runner.invoke(
            generate_manifest,
            [
                "--s3-uri",
                "s3://docs-bucket/prefix/",
                "--baseline-dir",
                str(baselines),
                "--output",
                str(output),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "--baseline-dir only works with --dir, ignoring" in result.output
    rows = _read_manifest_rows(output)
    assert rows == [
        {"document_path": "s3://docs-bucket/prefix/a.pdf", "baseline_source": ""}
    ]


# --------------------------------------------------------------------------------
# S3 scanning, against a moto-backed bucket holding real objects.
# --------------------------------------------------------------------------------


@pytest.mark.unit
def test_s3_scan_writes_full_uris_and_filters_keys(runner, tmp_path):
    """Scan a prefix holding a match, a nested match, a non-match and a folder marker.

    The manifest must carry fully-qualified `s3://bucket/key` URIs rather than bare
    keys, because that is what the downstream parser classifies as type `s3`. The
    zero-byte key ending in `/` is the "folder" object the S3 console creates, and it
    must not become a document.
    """
    from idp_cli.cli import generate_manifest

    output = tmp_path / "manifest.csv"

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="docs-bucket")
        for key in (
            "prefix/",  # folder marker
            "prefix/a.pdf",
            "prefix/sub/b.pdf",
            "prefix/notes.txt",
            "elsewhere/c.pdf",  # outside the prefix
        ):
            s3.put_object(Bucket="docs-bucket", Key=key, Body=b"pdf")

        result = runner.invoke(
            generate_manifest,
            ["--s3-uri", "s3://docs-bucket/prefix/", "--output", str(output)],
        )

    assert result.exit_code == 0, result.output
    assert "Found 2 documents" in result.output
    rows = _read_manifest_rows(output)
    assert {row["document_path"] for row in rows} == {
        "s3://docs-bucket/prefix/a.pdf",
        "s3://docs-bucket/prefix/sub/b.pdf",
    }


@pytest.mark.unit
def test_an_s3_prefix_without_a_trailing_slash_is_normalised(runner, tmp_path):
    """`s3://bucket/prefix` and `s3://bucket/prefix/` must scan the same objects.

    The command appends the slash before listing. Without that, `prefix` would also
    match a sibling prefix like `prefix-old/`, quietly pulling in documents from
    another test set.
    """
    from idp_cli.cli import generate_manifest

    output = tmp_path / "manifest.csv"

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="docs-bucket")
        s3.put_object(Bucket="docs-bucket", Key="prefix/a.pdf", Body=b"pdf")
        s3.put_object(Bucket="docs-bucket", Key="prefix-old/b.pdf", Body=b"pdf")

        result = runner.invoke(
            generate_manifest,
            ["--s3-uri", "s3://docs-bucket/prefix", "--output", str(output)],
        )

    assert result.exit_code == 0, result.output
    rows = _read_manifest_rows(output)
    assert [row["document_path"] for row in rows] == ["s3://docs-bucket/prefix/a.pdf"]


@pytest.mark.unit
def test_an_s3_uri_with_no_prefix_scans_the_whole_bucket(runner, tmp_path):
    from idp_cli.cli import generate_manifest

    output = tmp_path / "manifest.csv"

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="docs-bucket")
        s3.put_object(Bucket="docs-bucket", Key="a.pdf", Body=b"pdf")
        s3.put_object(Bucket="docs-bucket", Key="deep/b.pdf", Body=b"pdf")

        result = runner.invoke(
            generate_manifest, ["--s3-uri", "s3://docs-bucket", "--output", str(output)]
        )

    assert result.exit_code == 0, result.output
    rows = _read_manifest_rows(output)
    assert {row["document_path"] for row in rows} == {
        "s3://docs-bucket/a.pdf",
        "s3://docs-bucket/deep/b.pdf",
    }


@pytest.mark.unit
def test_s3_no_recursive_keeps_only_keys_directly_under_the_prefix(runner, tmp_path):
    from idp_cli.cli import generate_manifest

    output = tmp_path / "manifest.csv"

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="docs-bucket")
        s3.put_object(Bucket="docs-bucket", Key="prefix/a.pdf", Body=b"pdf")
        s3.put_object(Bucket="docs-bucket", Key="prefix/sub/b.pdf", Body=b"pdf")

        result = runner.invoke(
            generate_manifest,
            [
                "--s3-uri",
                "s3://docs-bucket/prefix/",
                "--output",
                str(output),
                "--no-recursive",
            ],
        )

    assert result.exit_code == 0, result.output
    rows = _read_manifest_rows(output)
    assert [row["document_path"] for row in rows] == ["s3://docs-bucket/prefix/a.pdf"]


@pytest.mark.unit
def test_an_uppercase_extension_is_selected_in_s3_too(runner, tmp_path):
    """The S3 scan takes `.PDF` keys as well, through the same predicate.

    The two scans are the reason `_file_pattern_matches` exists rather than two
    parallel filters: the S3 filter was `fnmatch.fnmatch`, which case-folds via
    `os.path.normcase` and so is a no-op on POSIX, giving a bucket the same silent
    omission as a directory. Both paths are asserted separately because they select
    from different sources and a fix to one says nothing about the other.
    """
    from idp_cli.cli import generate_manifest

    output = tmp_path / "manifest.csv"

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="docs-bucket")
        s3.put_object(Bucket="docs-bucket", Key="prefix/lower.pdf", Body=b"pdf")
        s3.put_object(Bucket="docs-bucket", Key="prefix/UPPER.PDF", Body=b"pdf")
        s3.put_object(Bucket="docs-bucket", Key="prefix/Mixed.Pdf", Body=b"pdf")

        result = runner.invoke(
            generate_manifest,
            ["--s3-uri", "s3://docs-bucket/prefix/", "--output", str(output)],
        )

    assert result.exit_code == 0, result.output
    rows = _read_manifest_rows(output)
    assert {row["document_path"] for row in rows} == {
        "s3://docs-bucket/prefix/lower.pdf",
        "s3://docs-bucket/prefix/UPPER.PDF",
        "s3://docs-bucket/prefix/Mixed.Pdf",
    }


@pytest.mark.unit
def test_case_sensitive_restores_exact_matching_in_s3_too(runner, tmp_path):
    """`--case-sensitive` has to reach the S3 scan, not only the local one.

    The flag is one option feeding one predicate, but it is threaded through two call
    sites, and a flag honoured on one path and dropped on the other is worse than no
    flag at all -- it would mean the same invocation selects different documents from
    a bucket and from a directory.
    """
    from idp_cli.cli import generate_manifest

    output = tmp_path / "manifest.csv"

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="docs-bucket")
        s3.put_object(Bucket="docs-bucket", Key="prefix/lower.pdf", Body=b"pdf")
        s3.put_object(Bucket="docs-bucket", Key="prefix/UPPER.PDF", Body=b"pdf")

        result = runner.invoke(
            generate_manifest,
            [
                "--s3-uri",
                "s3://docs-bucket/prefix/",
                "--output",
                str(output),
                "--case-sensitive",
            ],
        )

    assert result.exit_code == 0, result.output
    assert [row["document_path"] for row in _read_manifest_rows(output)] == [
        "s3://docs-bucket/prefix/lower.pdf"
    ]


@pytest.mark.unit
def test_an_empty_s3_prefix_refuses(runner, tmp_path):
    from idp_cli.cli import generate_manifest

    output = tmp_path / "manifest.csv"

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="docs-bucket")

        result = runner.invoke(
            generate_manifest,
            ["--s3-uri", "s3://docs-bucket/empty/", "--output", str(output)],
        )

    assert result.exit_code == 1, result.output
    assert "No documents found" in result.output
    assert not output.exists()


@pytest.mark.unit
def test_an_s3_error_is_reported_with_exit_1(runner, tmp_path):
    """A bucket that does not exist surfaces as the command's own error, not a traceback."""
    from idp_cli.cli import generate_manifest

    output = tmp_path / "manifest.csv"

    with mock_aws():
        result = runner.invoke(
            generate_manifest,
            ["--s3-uri", "s3://no-such-bucket/prefix/", "--output", str(output)],
        )

    assert result.exit_code == 1, result.output
    assert "✗ Error:" in result.output
    assert not output.exists()


# --------------------------------------------------------------------------------
# The --test-set path: uploads into the test set bucket, then rewrites the manifest
# to point at the uploaded copies.
# --------------------------------------------------------------------------------


@pytest.mark.unit
def test_test_set_uploads_inputs_and_baselines_and_rewrites_the_manifest(
    runner, tmp_path
):
    """The whole `--test-set` happy path, read back off the bucket.

    Four separate claims: the input document lands at `<set>/input/<filename>`; every
    file under the matched baseline directory lands at
    `<set>/baseline/<filename>/<relative path>` with its directory structure intact;
    the `.uploading` marker that stops the resolver validating a half-uploaded folder
    is gone by the end; and the manifest now names the uploaded S3 objects rather than
    the local paths it was given. The last one is what makes the manifest usable from
    another machine, and it is the one a mock-based test would not notice.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "invoice.pdf")
    baselines = tmp_path / "baselines"
    _write(baselines / "invoice.pdf" / "sections" / "1" / "result.json", '{"a": 1}')
    _write(baselines / "invoice.pdf" / "summary.json", "{}")
    output = tmp_path / "manifest.csv"

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)

        with _patched_stack_resources({"TestSetBucket": TEST_SET_BUCKET}):
            result = runner.invoke(
                generate_manifest,
                [
                    "--dir",
                    str(docs),
                    "--baseline-dir",
                    str(baselines),
                    "--test-set",
                    "fcc example",
                    "--stack-name",
                    "IDP",
                    "--output",
                    str(output),
                ],
            )

        assert result.exit_code == 0, result.output
        keys = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=TEST_SET_BUCKET).get("Contents", [])
        }

    assert keys == {
        "fcc example/input/invoice.pdf",
        "fcc example/baseline/invoice.pdf/sections/1/result.json",
        "fcc example/baseline/invoice.pdf/summary.json",
    }, keys
    assert "fcc example/.uploading" not in keys, "the upload marker must be removed"

    rows = _read_manifest_rows(output)
    assert rows == [
        {
            "document_path": f"s3://{TEST_SET_BUCKET}/fcc example/input/invoice.pdf",
            "baseline_source": f"s3://{TEST_SET_BUCKET}/fcc example/baseline/invoice.pdf/",
        }
    ]
    assert "Test set 'fcc example' created successfully" in result.output


@pytest.mark.unit
def test_test_set_works_without_an_output_manifest(runner, tmp_path):
    """`--output` is optional with `--test-set`; the upload still happens."""
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "invoice.pdf")
    baselines = tmp_path / "baselines"
    (baselines / "invoice.pdf").mkdir(parents=True)

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)

        with _patched_stack_resources({"TestSetBucket": TEST_SET_BUCKET}):
            result = runner.invoke(
                generate_manifest,
                [
                    "--dir",
                    str(docs),
                    "--baseline-dir",
                    str(baselines),
                    "--test-set",
                    "set1",
                    "--stack-name",
                    "IDP",
                ],
            )

        assert result.exit_code == 0, result.output
        keys = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=TEST_SET_BUCKET).get("Contents", [])
        }

    assert keys == {"set1/input/invoice.pdf"}
    assert "Using manifest:" not in result.output


@pytest.mark.unit
def test_a_missing_test_set_bucket_is_refused_before_any_upload(
    runner, tmp_path, api_calls
):
    """No `TestSetBucket` in the stack's resources means exit 1 and no S3 writes at all."""
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "invoice.pdf")
    baselines = tmp_path / "baselines"
    baselines.mkdir()

    with mock_aws(), _patched_stack_resources({"InputBucket": "some-bucket"}):
        result = runner.invoke(
            generate_manifest,
            [
                "--dir",
                str(docs),
                "--baseline-dir",
                str(baselines),
                "--test-set",
                "set1",
                "--stack-name",
                "IDP",
            ],
        )

    assert result.exit_code == 1, result.output
    assert "TestSetBucket not found in stack resources" in result.output
    assert api_calls.of("PutObject") == []
    assert api_calls.of("DeleteObjects") == []


@pytest.mark.unit
def test_declining_the_overwrite_prompt_makes_no_s3_writes(runner, tmp_path, api_calls):
    """Answering the "Continue? [y/N]" prompt with `n` must leave the bucket untouched.

    The existing objects are read back afterwards to show they survived, and the API
    log is checked for the absence of the three mutating calls rather than trusting a
    mock: a refusal that still deleted the previous test set's files would be far worse
    than one that overwrote them.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "invoice.pdf")
    baselines = tmp_path / "baselines"
    (baselines / "invoice.pdf").mkdir(parents=True)

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)
        s3.put_object(
            Bucket=TEST_SET_BUCKET, Key="set1/input/previous.pdf", Body=b"old"
        )
        # Everything up to here is this test's own setup; only the calls the command
        # itself makes are interesting, so record where it starts.
        before_invoke = len(api_calls)

        with _patched_stack_resources({"TestSetBucket": TEST_SET_BUCKET}):
            result = runner.invoke(
                generate_manifest,
                [
                    "--dir",
                    str(docs),
                    "--baseline-dir",
                    str(baselines),
                    "--test-set",
                    "set1",
                    "--stack-name",
                    "IDP",
                ],
                input="n\n",
            )

        surviving = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=TEST_SET_BUCKET).get("Contents", [])
        }

    assert result.exit_code == 1, result.output
    assert "already exists in bucket" in result.output
    assert "✗ Aborted" in result.output
    assert surviving == {"set1/input/previous.pdf"}
    made_by_the_command = [call.operation for call in api_calls[before_invoke:]]
    assert "PutObject" not in made_by_the_command
    assert "DeleteObjects" not in made_by_the_command
    assert "DeleteObject" not in made_by_the_command
    assert "CopyObject" not in made_by_the_command


@pytest.mark.unit
def test_confirming_the_overwrite_clears_the_previous_test_set(runner, tmp_path):
    """Answering `y` deletes everything under the test set prefix, then re-uploads."""
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "invoice.pdf")
    baselines = tmp_path / "baselines"
    (baselines / "invoice.pdf").mkdir(parents=True)

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)
        s3.put_object(Bucket=TEST_SET_BUCKET, Key="set1/input/stale.pdf", Body=b"old")
        s3.put_object(Bucket=TEST_SET_BUCKET, Key="other/keep.pdf", Body=b"keep")

        with _patched_stack_resources({"TestSetBucket": TEST_SET_BUCKET}):
            result = runner.invoke(
                generate_manifest,
                [
                    "--dir",
                    str(docs),
                    "--baseline-dir",
                    str(baselines),
                    "--test-set",
                    "set1",
                    "--stack-name",
                    "IDP",
                ],
                input="y\n",
            )

        keys = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=TEST_SET_BUCKET).get("Contents", [])
        }

    assert result.exit_code == 0, result.output
    assert "Cleared 1 existing files" in result.output
    assert keys == {"set1/input/invoice.pdf", "other/keep.pdf"}, (
        "only the test set's own prefix may be cleared"
    )


@pytest.mark.unit
def test_the_overwrite_clears_every_object_past_the_first_listing_page(
    runner, tmp_path, api_calls
):
    """Overwriting a test set of more than 1000 objects removes all of them.

    The clear read one `list_objects_v2` response, which stops at 1000 keys, so
    overwriting a test set larger than one page deleted its first 1000 objects and left
    the remainder orphaned under a prefix the command had just reported it cleared —
    with a fresh test set then uploaded on top, mixing one set's baselines into
    another's. **The fixture must exceed one page**: at 1000 objects or fewer, one
    listing returns everything and the fixed and broken code behave identically. The
    second page comes from `moto` producing a genuine truncated response over 1001 real
    objects.

    Beyond the count in the message, the object that is only reachable on the second
    page is named individually, and every `DeleteObjects` request is checked for the
    1000-key limit that S3 enforces and `moto` does not — so the batching is measured
    here rather than deferred to a real bucket.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "invoice.pdf")
    baselines = tmp_path / "baselines"
    (baselines / "invoice.pdf").mkdir(parents=True)

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)
        for i in range(1001):
            s3.put_object(
                Bucket=TEST_SET_BUCKET, Key=f"set1/stale/{i:05d}.json", Body=b"{}"
            )
        s3.put_object(Bucket=TEST_SET_BUCKET, Key="other/keep.pdf", Body=b"keep")
        before_invoke = len(api_calls)

        with _patched_stack_resources({"TestSetBucket": TEST_SET_BUCKET}):
            result = runner.invoke(
                generate_manifest,
                [
                    "--dir",
                    str(docs),
                    "--baseline-dir",
                    str(baselines),
                    "--test-set",
                    "set1",
                    "--stack-name",
                    "IDP",
                ],
                input="y\n",
            )

        paginator = s3.get_paginator("list_objects_v2")
        keys = {
            obj["Key"]
            for page in paginator.paginate(Bucket=TEST_SET_BUCKET)
            for obj in page.get("Contents", [])
        }
        deletes = [
            call
            for call in api_calls[before_invoke:]
            if call.operation == "DeleteObjects"
        ]

    assert result.exit_code == 0, result.output
    assert "Cleared 1001 existing files" in result.output
    assert "set1/stale/01000.json" not in keys, (
        "the object beyond the first listing page survived the overwrite"
    )
    assert keys == {"set1/input/invoice.pdf", "other/keep.pdf"}

    assert len(deletes) == 2, [len(d.params["Delete"]["Objects"]) for d in deletes]
    for delete in deletes:
        assert len(delete.params["Delete"]["Objects"]) <= 1000, (
            "DeleteObjects takes at most 1000 keys; a larger request is rejected by S3"
        )


@pytest.mark.unit
def test_eof_on_the_overwrite_prompt_aborts_without_touching_the_test_set(
    runner, tmp_path, api_calls
):
    """EOF on the confirmation aborts: no answer is not consent to clear baselines.

    `input()` raises `EOFError` on a closed or empty stdin, and `EOFError` is an
    `Exception` — so while the prompt sat inside the listing's `try/except Exception`,
    anything non-interactive (a CI job, a `make` target, a shell with stdin from
    `/dev/null`) had its guard swallowed by that handler and went on to clear and
    overwrite the existing test set, destroying the baselines a previous evaluation was
    measured against, at exit 0 with a yellow warning as the only sign.

    Supplying EOF rather than `n` is the whole point: answering `n` already aborted
    (`test_declining_the_overwrite_prompt_makes_no_s3_writes`), so a test that answers
    cannot tell the fix from the defect. `CliRunner` with `input=""` gives a stdin that
    is immediately at EOF, which is the real non-interactive shape.

    The pre-existing object is read back to show it survived, and the API log is
    checked for the absence of the mutating calls rather than trusting the exit code:
    an abort that had already deleted the previous baselines would be worse than the
    overwrite it replaced.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "invoice.pdf")
    baselines = tmp_path / "baselines"
    (baselines / "invoice.pdf").mkdir(parents=True)

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)
        s3.put_object(Bucket=TEST_SET_BUCKET, Key="set1/input/stale.pdf", Body=b"old")
        before_invoke = len(api_calls)

        with _patched_stack_resources({"TestSetBucket": TEST_SET_BUCKET}):
            result = runner.invoke(
                generate_manifest,
                [
                    "--dir",
                    str(docs),
                    "--baseline-dir",
                    str(baselines),
                    "--test-set",
                    "set1",
                    "--stack-name",
                    "IDP",
                ],
                input="",
            )

        keys = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=TEST_SET_BUCKET).get("Contents", [])
        }

    assert result.exit_code == 1, result.output
    assert "no answer read from stdin" in result.output
    assert "--force" in result.output, "the abort has to name the non-interactive route"
    assert keys == {"set1/input/stale.pdf"}, (
        "the existing test set must survive an unanswered prompt"
    )
    made_by_the_command = [call.operation for call in api_calls[before_invoke:]]
    assert "PutObject" not in made_by_the_command
    assert "DeleteObjects" not in made_by_the_command
    assert "DeleteObject" not in made_by_the_command


@pytest.mark.unit
def test_force_overwrites_an_existing_test_set_with_no_prompt(runner, tmp_path):
    """`--force` is the non-interactive route to the overwrite, and it asks nothing.

    Without it there would be no way to refresh a test set from a script, since EOF on
    the prompt now aborts. Stdin is left empty here, so a prompt that were still
    reached would raise `EOFError` and abort — reaching exit 0 with the stale object
    gone is what shows the confirmation was skipped rather than answered.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "invoice.pdf")
    baselines = tmp_path / "baselines"
    (baselines / "invoice.pdf").mkdir(parents=True)

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)
        s3.put_object(Bucket=TEST_SET_BUCKET, Key="set1/input/stale.pdf", Body=b"old")

        with _patched_stack_resources({"TestSetBucket": TEST_SET_BUCKET}):
            result = runner.invoke(
                generate_manifest,
                [
                    "--dir",
                    str(docs),
                    "--baseline-dir",
                    str(baselines),
                    "--test-set",
                    "set1",
                    "--stack-name",
                    "IDP",
                    "--force",
                ],
                input="",
            )

        keys = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=TEST_SET_BUCKET).get("Contents", [])
        }

    assert result.exit_code == 0, result.output
    assert "Continue? [y/N]" not in result.output
    assert keys == {"set1/input/invoice.pdf"}


@pytest.mark.unit
def test_a_baseline_directory_holding_no_files_is_reported_as_uploading_none(
    runner, tmp_path
):
    """An empty baseline directory is a warning, not an "Uploaded baseline" line.

    `--baseline-dir` matches by directory name, so a directory that exists but holds no
    files at its top level — empty, or nested one level deeper than expected — matches a
    document and contributes nothing. The upload loop's report sat after it
    unconditionally, so the command printed `Uploaded baseline: invoice.pdf`, rewrote the
    manifest row to name `baseline/invoice.pdf/`, and printed the baseline location, for
    a prefix with nothing in it. The whole run then reads as a complete test set that an
    evaluation cannot score.

    Read off the bucket rather than the console for the objects, and off the console for
    what the operator is told, since the defect was entirely in the second.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "invoice.pdf")
    baselines = tmp_path / "baselines"
    (baselines / "invoice.pdf").mkdir(parents=True)  # matched, and empty

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)

        with _patched_stack_resources({"TestSetBucket": TEST_SET_BUCKET}):
            result = runner.invoke(
                generate_manifest,
                [
                    "--dir",
                    str(docs),
                    "--baseline-dir",
                    str(baselines),
                    "--test-set",
                    "set1",
                    "--stack-name",
                    "IDP",
                ],
            )

        keys = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=TEST_SET_BUCKET).get("Contents", [])
        }

    assert result.exit_code == 0, result.output
    assert keys == {"set1/input/invoice.pdf"}
    assert "Uploaded baseline: invoice.pdf" not in result.output, (
        "nothing was uploaded, so nothing may claim it was"
    )
    assert "Warning: no baseline files found for invoice.pdf" in result.output
    assert "(0 objects)" in result.output


@pytest.mark.unit
def test_the_baseline_upload_counts_the_files_it_uploaded(runner, tmp_path):
    """The per-baseline line and the summary both carry counts read off the uploads.

    The count is the only thing that distinguishes a test set whose baselines arrived
    from one whose did not, since every other line of the output is identical either
    way. Two files in one baseline directory, one of them nested, so the number cannot
    come from counting matched directories or manifest rows.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "invoice.pdf")
    baselines = tmp_path / "baselines"
    _write(baselines / "invoice.pdf" / "result.json", "{}")
    _write(baselines / "invoice.pdf" / "sections" / "1.json", "{}")

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)

        with _patched_stack_resources({"TestSetBucket": TEST_SET_BUCKET}):
            result = runner.invoke(
                generate_manifest,
                [
                    "--dir",
                    str(docs),
                    "--baseline-dir",
                    str(baselines),
                    "--test-set",
                    "set1",
                    "--stack-name",
                    "IDP",
                ],
            )

        keys = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=TEST_SET_BUCKET).get("Contents", [])
        }

    assert result.exit_code == 0, result.output
    assert keys == {
        "set1/input/invoice.pdf",
        "set1/baseline/invoice.pdf/result.json",
        "set1/baseline/invoice.pdf/sections/1.json",
    }
    assert "Uploaded baseline: invoice.pdf (2 files)" in result.output
    assert "(2 objects)" in result.output
    assert "Warning: no baseline files found" not in result.output


@pytest.mark.unit
def test_the_uploading_marker_is_written_before_the_documents(
    runner, tmp_path, api_calls
):
    """The `.uploading` marker must precede the first input upload and be deleted last.

    The marker is what stops the test set resolver validating a folder that is still
    being filled (issue #193). Ordering is the whole point of it, so this reads the
    recorded API calls in order: the marker `PutObject` has to come before the first
    input object's, and the `DeleteObject` that removes it has to come after.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "invoice.pdf")
    baselines = tmp_path / "baselines"
    (baselines / "invoice.pdf").mkdir(parents=True)

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(
            Bucket=TEST_SET_BUCKET
        )
        with _patched_stack_resources({"TestSetBucket": TEST_SET_BUCKET}):
            result = runner.invoke(
                generate_manifest,
                [
                    "--dir",
                    str(docs),
                    "--baseline-dir",
                    str(baselines),
                    "--test-set",
                    "set1",
                    "--stack-name",
                    "IDP",
                ],
            )

    assert result.exit_code == 0, result.output

    puts = api_calls.of("PutObject")
    marker_puts = [call for call in puts if call.params["Key"] == "set1/.uploading"]
    assert len(marker_puts) == 1, [call.params["Key"] for call in puts]
    # botocore wraps a bytes Body in a BytesIO before the call is made, so read it back.
    assert marker_puts[0].params["Body"].getvalue() == b"upload-in-progress"

    deleted = api_calls.only("DeleteObject")
    assert deleted.params["Key"] == "set1/.uploading"

    # `upload_file` also goes out as a PutObject for a small file, so the ordering is
    # asserted on positions in the recorded sequence rather than on call counts.
    keys_in_order = [
        call.params["Key"] for call in api_calls if call.operation == "PutObject"
    ]
    assert keys_in_order == ["set1/.uploading", "set1/input/invoice.pdf"]
    assert api_calls.operations().index("DeleteObject") > api_calls.operations().index(
        "PutObject"
    )


@pytest.mark.unit
def test_an_unreachable_test_set_bucket_warns_twice_and_then_fails(runner, tmp_path):
    """A bucket the command cannot list produces two warnings before the real failure.

    Both the "does this test set already exist" check and the "clear the old files" step
    are wrapped in their own `except Exception`, so a wrong bucket name or a missing
    `s3:ListBucket` grant degrades each of them to a warning. Only the marker
    `PutObject` afterwards is unguarded, and that is what actually stops the run. The
    sequence is worth pinning because the two yellow lines look like the problem while
    the red one below them is the problem, and a reader who stops at the first warning
    will chase the wrong grant.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "invoice.pdf")
    baselines = tmp_path / "baselines"
    (baselines / "invoice.pdf").mkdir(parents=True)

    with mock_aws(), _patched_stack_resources({"TestSetBucket": "no-such-bucket"}):
        result = runner.invoke(
            generate_manifest,
            [
                "--dir",
                str(docs),
                "--baseline-dir",
                str(baselines),
                "--test-set",
                "set1",
                "--stack-name",
                "IDP",
            ],
        )

    assert result.exit_code == 1, result.output
    assert "Warning: Could not check existing test set:" in result.output
    assert "Warning: Could not clear existing files:" in result.output
    assert "✗ Error:" in result.output


@pytest.mark.unit
def test_a_marker_that_cannot_be_deleted_fails_the_command(runner, tmp_path):
    """A marker that cannot be removed is exit 1, not a warning above a success line.

    The `.uploading` marker used to be removed inside its own `except Exception`, so a
    failed delete — an IAM policy granting `s3:PutObject` but not `s3:DeleteObject` on
    the test set bucket is the realistic shape — printed a yellow warning and then
    "✓ Test set created successfully" at exit 0. The test set resolver skips any folder
    carrying that marker, so what the user was told had worked was a test set complete
    in S3 and permanently invisible to the backend, recoverable only by deleting that
    object by hand.

    The command now exits 1 and the message names the object, which is the recovery.
    Three separate claims are asserted, because the first two are each satisfiable
    without the others: the exit code, the object's name in the message, and that no
    success line was printed.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "invoice.pdf")
    baselines = tmp_path / "baselines"
    (baselines / "invoice.pdf").mkdir(parents=True)

    class NoDeleteObject:
        """A real S3 client with `delete_object` denied, as an IAM policy would."""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def delete_object(self, **kwargs):
            raise RuntimeError("AccessDenied: s3:DeleteObject")

    real_client = boto3.client

    with mock_aws():
        real_client("s3", region_name="us-east-1").create_bucket(Bucket=TEST_SET_BUCKET)

        def _client(service_name, **kwargs):
            built = real_client(service_name, **kwargs)
            return NoDeleteObject(built) if service_name == "s3" else built

        with (
            patch("idp_cli.cli.boto3.client", side_effect=_client),
            _patched_stack_resources({"TestSetBucket": TEST_SET_BUCKET}),
        ):
            result = runner.invoke(
                generate_manifest,
                [
                    "--dir",
                    str(docs),
                    "--baseline-dir",
                    str(baselines),
                    "--test-set",
                    "set1",
                    "--stack-name",
                    "IDP",
                ],
            )

        keys = {
            obj["Key"]
            for obj in real_client("s3", region_name="us-east-1")
            .list_objects_v2(Bucket=TEST_SET_BUCKET)
            .get("Contents", [])
        }

    assert result.exit_code == 1, result.output
    assert f"s3://{TEST_SET_BUCKET}/set1/.uploading" in result.output, result.output
    assert "invisible to the backend" in result.output, result.output
    assert "created successfully" not in result.output, (
        "success must not be reported for a test set the backend cannot see"
    )
    # The delete really did fail, so the marker is genuinely still there and the input
    # genuinely did arrive: the failure is about visibility, not about a broken upload.
    assert "set1/.uploading" in keys
    assert "set1/input/invoice.pdf" in keys


class _FailingS3:
    """A real S3 client with named operations denied, as an IAM policy would deny them.

    `allow_first` exists so a test can say *which* call of an operation fails. Denying
    every `upload_file` cannot distinguish the input upload from the baseline upload --
    both raise, so a test asserting only "exit 1" passes even if one of the two call
    sites has stopped reporting its errors at all. That is not hypothetical: it was
    measured, as a mutation swallowing the input upload's exception that left the suite
    green because the baseline upload then failed in its place.
    """

    def __init__(self, inner, denied, allow_first=None):
        self._inner = inner
        self._denied = denied
        self._allow_first = allow_first or {}
        self._calls = {}

    def __getattr__(self, name):
        if name in self._denied:

            def _maybe_deny(*args, **kwargs):
                seen = self._calls.get(name, 0)
                self._calls[name] = seen + 1
                if seen < self._allow_first.get(name, 0):
                    return getattr(self._inner, name)(*args, **kwargs)
                raise RuntimeError(f"AccessDenied: {self._denied[name]}")

            return _maybe_deny
        return getattr(self._inner, name)


def _run_generate_manifest_with_denied(
    runner, tmp_path, denied, allow_first=None, baseline_files=True, set_name="set1"
):
    """Invoke `generate-manifest --test-set` against a client with `denied` operations.

    Returns `(result, keys)` -- the click result and the keys actually in the bucket,
    read back with an unrestricted client after the command has finished.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "invoice.pdf")
    baselines = tmp_path / "baselines"
    (baselines / "invoice.pdf").mkdir(parents=True)
    if baseline_files:
        _write(baselines / "invoice.pdf" / "expected.json", '{"a": 1}')

    real_client = boto3.client

    with mock_aws():
        real_client("s3", region_name="us-east-1").create_bucket(Bucket=TEST_SET_BUCKET)

        def _client(service_name, **kwargs):
            built = real_client(service_name, **kwargs)
            if service_name != "s3":
                return built
            return _FailingS3(built, denied, allow_first)

        with (
            patch("idp_cli.cli.boto3.client", side_effect=_client),
            _patched_stack_resources({"TestSetBucket": TEST_SET_BUCKET}),
        ):
            result = runner.invoke(
                generate_manifest,
                [
                    "--dir",
                    str(docs),
                    "--baseline-dir",
                    str(baselines),
                    "--test-set",
                    set_name,
                    "--stack-name",
                    "IDP",
                ],
            )

        keys = {
            obj["Key"]
            for obj in real_client("s3", region_name="us-east-1")
            .list_objects_v2(Bucket=TEST_SET_BUCKET)
            .get("Contents", [])
        }

    return result, keys


@pytest.mark.unit
@pytest.mark.parametrize(
    ("which", "allow_first", "baseline_files", "expected_input_key"),
    [
        # The two upload call sites inside the marker's window, reached one at a time.
        # Denying every `upload_file` reaches only the first of them, so a fix applied
        # to one and not the other would go unnoticed -- which is exactly what a
        # mutation of the input-upload call site demonstrated before this was split.
        ("the input upload", {}, False, None),
        ("the baseline upload", {"upload_file": 1}, True, "set1/input/invoice.pdf"),
    ],
)
def test_an_upload_failure_removes_the_marker_and_still_exits_1(
    runner, tmp_path, which, allow_first, baseline_files, expected_input_key
):
    """A denied `PutObject` partway through leaves no `.uploading` marker behind.

    This path had no test at all, and it is the one a user is most likely to hit: an
    IAM policy short of `s3:PutObject` on the test set bucket, or a file that moved
    between the scan and the upload. The marker used to be removed by a plain statement
    after the loops, so the exception went past it and the half-filled folder stayed
    hidden from the resolver — including after the policy was fixed and the upload
    re-run, because the new run wrote the marker again.

    Both halves are asserted and neither implies the other. The failure has to have
    happened (exit 1 with the command's error line), because the marker is removed on
    the success path too and the marker assertion alone would pass on a clean run; and
    the marker has to be gone, because exit 1 was already the behaviour before the fix.

    The two call sites are reached separately. `expected_input_key` is what pins that:
    in the baseline case the input object must be present, which is the evidence that
    the first upload really did go through and the failure really is the second one.
    """
    result, keys = _run_generate_manifest_with_denied(
        runner,
        tmp_path,
        {"upload_file": "s3:PutObject"},
        allow_first=allow_first,
        baseline_files=baseline_files,
    )

    assert result.exit_code == 1, result.output
    assert "✗ Error:" in result.output
    assert "AccessDenied: s3:PutObject" in result.output

    if expected_input_key is None:
        assert keys == set(), (
            f"{which}: nothing should have landed, and the marker must be gone"
        )
    else:
        assert keys == {expected_input_key}, (
            f"{which}: the earlier upload went through and only the marker is gone"
        )

    assert "set1/.uploading" not in keys, (
        f"the marker outlived the failure at {which} and would hide the test set"
    )
    assert "created successfully" not in result.output


@pytest.mark.unit
def test_an_upload_failure_whose_cleanup_also_fails_says_so_and_keeps_the_real_error(
    runner, tmp_path
):
    """When the upload fails *and* the marker cannot be removed, both are reported.

    The cleanup runs on the way out of a failure, so it must not be able to replace the
    exception that caused the failure -- a `delete_object` error surfacing instead of
    the `PutObject` one would send the user after the wrong problem. It is reported as
    a warning naming the object to delete, and the original error is what the command
    exits on.
    """
    result, keys = _run_generate_manifest_with_denied(
        runner,
        tmp_path,
        {"upload_file": "s3:PutObject", "delete_object": "s3:DeleteObject"},
    )

    assert result.exit_code == 1, result.output
    # The original cause, not the cleanup's.
    assert "AccessDenied: s3:PutObject" in result.output, result.output
    # And the cleanup failure as a warning that names the object to delete by hand.
    assert f"s3://{TEST_SET_BUCKET}/set1/.uploading" in result.output, result.output
    assert "delete that object before retrying" in result.output, result.output

    # Nothing removed it, so it is genuinely still there -- which is what makes the
    # warning the only route to recovery and therefore load-bearing.
    assert "set1/.uploading" in keys


@pytest.mark.unit
def test_the_test_set_resolver_is_invoked_and_its_absence_only_warns(runner, tmp_path):
    """With no resolver Lambda deployed, registration is skipped with a warning, not an error.

    Under moto there are no Lambda functions at all, which is the same thing the
    command sees against a stack whose API-resolver nested stack has not been deployed.
    The upload must still be treated as a success, because the files really are in the
    bucket — the only thing missing is the tracking-table registration.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "invoice.pdf")
    baselines = tmp_path / "baselines"
    (baselines / "invoice.pdf").mkdir(parents=True)

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(
            Bucket=TEST_SET_BUCKET
        )
        with _patched_stack_resources({"TestSetBucket": TEST_SET_BUCKET}):
            result = runner.invoke(
                generate_manifest,
                [
                    "--dir",
                    str(docs),
                    "--baseline-dir",
                    str(baselines),
                    "--test-set",
                    "set1",
                    "--stack-name",
                    "IDP",
                ],
            )

    assert result.exit_code == 0, result.output
    assert "TestSetResolverFunction not found, skipping auto-detection" in result.output
    assert "created successfully" in result.output


# --------------------------------------------------------------------------------
# validate-manifest: the reader half.
# --------------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_accepts_a_well_formed_manifest(runner, tmp_path):
    from idp_cli.cli import validate_manifest_cmd

    manifest = tmp_path / "m.csv"
    manifest.write_text(
        "document_path,baseline_source\r\n"
        "s3://bucket/a.pdf,s3://baselines/a/\r\n"
        "s3://bucket/b.pdf,\r\n"
    )

    result = runner.invoke(validate_manifest_cmd, ["--manifest", str(manifest)])

    assert result.exit_code == 0, result.output
    assert f"✓ Manifest is valid: {manifest}" in result.output


@pytest.mark.unit
def test_validate_accepts_a_manifest_with_only_the_required_column(runner, tmp_path):
    """`baseline_source` is optional, so a single-column manifest is valid."""
    from idp_cli.cli import validate_manifest_cmd

    manifest = tmp_path / "m.csv"
    manifest.write_text("document_path\ns3://bucket/a.pdf\n")

    result = runner.invoke(validate_manifest_cmd, ["--manifest", str(manifest)])

    assert result.exit_code == 0, result.output
    assert "✓ Manifest is valid" in result.output


@pytest.mark.unit
def test_validate_rejects_a_manifest_missing_the_required_column(runner, tmp_path):
    """A manifest with only `baseline_source` names no documents, so it must fail.

    This is the case the command exists for. Exiting 0 here would let `idp-cli
    process` be run against a manifest that cannot produce a single document.
    """
    from idp_cli.cli import validate_manifest_cmd

    manifest = tmp_path / "m.csv"
    manifest.write_text("baseline_source\ns3://baselines/a/\n")

    result = runner.invoke(validate_manifest_cmd, ["--manifest", str(manifest)])

    assert result.exit_code == 1, result.output
    assert "✗ Manifest validation failed:" in result.output
    assert "Missing required field 'document_path' or 'path'" in result.output


@pytest.mark.unit
def test_validate_rejects_an_empty_file(runner, tmp_path):
    """A zero-byte `.csv` has no header and no rows, so it validates as "no documents"."""
    from idp_cli.cli import validate_manifest_cmd

    manifest = tmp_path / "m.csv"
    manifest.write_text("")

    result = runner.invoke(validate_manifest_cmd, ["--manifest", str(manifest)])

    assert result.exit_code == 1, result.output
    assert "Manifest contains no documents" in result.output


@pytest.mark.unit
def test_validate_rejects_a_header_only_manifest(runner, tmp_path):
    from idp_cli.cli import validate_manifest_cmd

    manifest = tmp_path / "m.csv"
    manifest.write_text("document_path,baseline_source\r\n")

    result = runner.invoke(validate_manifest_cmd, ["--manifest", str(manifest)])

    assert result.exit_code == 1, result.output
    assert "Manifest contains no documents" in result.output


@pytest.mark.unit
def test_validate_rejects_duplicate_filenames(runner, tmp_path):
    """Two documents with the same basename would collide as S3 keys during upload."""
    from idp_cli.cli import validate_manifest_cmd

    manifest = tmp_path / "m.csv"
    manifest.write_text(
        "document_path\ns3://bucket/one/invoice.pdf\ns3://bucket/two/invoice.pdf\n"
    )

    result = runner.invoke(validate_manifest_cmd, ["--manifest", str(manifest)])

    assert result.exit_code == 1, result.output
    assert "Duplicate filenames found: invoice.pdf" in result.output


@pytest.mark.unit
def test_validate_rejects_a_relative_local_path(runner, tmp_path):
    """A path that is neither absolute nor an existing file cannot be resolved later."""
    from idp_cli.cli import validate_manifest_cmd

    manifest = tmp_path / "m.csv"
    manifest.write_text("document_path\ndocuments/invoice.pdf\n")

    result = runner.invoke(validate_manifest_cmd, ["--manifest", str(manifest)])

    assert result.exit_code == 1, result.output
    assert "Use absolute local path or s3:// URI" in result.output


@pytest.mark.unit
def test_validate_rejects_an_absolute_path_that_does_not_exist(runner, tmp_path):
    from idp_cli.cli import validate_manifest_cmd

    missing = tmp_path / "gone.pdf"
    manifest = tmp_path / "m.csv"
    manifest.write_text(f"document_path\n{missing}\n")

    result = runner.invoke(validate_manifest_cmd, ["--manifest", str(manifest)])

    assert result.exit_code == 1, result.output
    assert f"Local file not found: {missing}" in result.output


@pytest.mark.unit
def test_validate_rejects_an_unsupported_extension(runner, tmp_path):
    from idp_cli.cli import validate_manifest_cmd

    manifest = tmp_path / "m.xml"
    manifest.write_text("<manifest/>")

    result = runner.invoke(validate_manifest_cmd, ["--manifest", str(manifest)])

    assert result.exit_code == 1, result.output
    assert "Unsupported manifest format" in result.output


@pytest.mark.unit
def test_validate_rejects_a_nonexistent_file_with_exit_2(runner, tmp_path):
    """`--manifest` is a `click.Path(exists=True)`, so a missing file is a usage error.

    Exit 2 rather than 1, and the message comes from click, so a caller distinguishing
    "you typed the wrong path" from "the manifest is broken" can do so on the code.
    """
    from idp_cli.cli import validate_manifest_cmd

    result = runner.invoke(
        validate_manifest_cmd, ["--manifest", str(tmp_path / "nope.csv")]
    )

    assert result.exit_code == 2, result.output
    assert "does not exist" in result.output


@pytest.mark.unit
def test_validate_reports_a_short_row_as_a_failure_with_an_opaque_message(
    runner, tmp_path
):
    """DEFECT (pinned, not fixed): a row with too few fields fails with a Python error.

    `csv.DictReader` fills a missing trailing column with `None`, and the parser then
    calls `.strip()` on it. The resulting `AttributeError` is swallowed by
    `validate_manifest`'s blanket `except Exception` and its `str()` is printed as the
    validation error, so the verdict is right — exit 1, the manifest is rejected — but
    the message is `'NoneType' object has no attribute 'strip'`, which names neither
    the row nor the column and gives the user nothing to act on.

    The verdict is the important half and it is asserted first; the message is pinned
    so that improving it is a visible change rather than an accident.
    """
    from idp_cli.cli import validate_manifest_cmd

    manifest = tmp_path / "m.csv"
    manifest.write_text("document_path,baseline_source\ns3://bucket/a.pdf\n")

    result = runner.invoke(validate_manifest_cmd, ["--manifest", str(manifest)])

    assert result.exit_code == 1, result.output
    assert "✗ Manifest validation failed:" in result.output
    assert "'NoneType' object has no attribute 'strip'" in result.output
    assert "row" not in result.output.lower().replace("wrong", "")


@pytest.mark.unit
def test_validate_ignores_extra_fields_on_a_row(runner, tmp_path):
    """A row with MORE fields than the header is accepted; the surplus is discarded.

    `csv.DictReader` collects extras under a `None` key which the parser never reads.
    Recorded rather than judged: it means a manifest edited by hand into three columns
    validates cleanly while the third column is ignored everywhere downstream.
    """
    from idp_cli.cli import validate_manifest_cmd

    manifest = tmp_path / "m.csv"
    manifest.write_text("document_path,baseline_source\ns3://bucket/a.pdf,base,extra\n")

    result = runner.invoke(validate_manifest_cmd, ["--manifest", str(manifest)])

    assert result.exit_code == 0, result.output
    assert "✓ Manifest is valid" in result.output


@pytest.mark.unit
def test_validate_accepts_a_json_manifest(runner, tmp_path):
    from idp_cli.cli import validate_manifest_cmd

    manifest = tmp_path / "m.json"
    manifest.write_text(
        json.dumps(
            [
                {"document_path": "s3://bucket/a.pdf", "baseline_source": "s3://b/a/"},
                {"document_path": "s3://bucket/b.pdf"},
            ]
        )
    )

    result = runner.invoke(validate_manifest_cmd, ["--manifest", str(manifest)])

    assert result.exit_code == 0, result.output
    assert "✓ Manifest is valid" in result.output


@pytest.mark.unit
def test_validate_rejects_malformed_json(runner, tmp_path):
    from idp_cli.cli import validate_manifest_cmd

    manifest = tmp_path / "m.json"
    manifest.write_text('{"documents": ')

    result = runner.invoke(validate_manifest_cmd, ["--manifest", str(manifest)])

    assert result.exit_code == 1, result.output
    assert "✗ Manifest validation failed:" in result.output


@pytest.mark.unit
def test_validate_reports_an_unexpected_error_with_exit_1(runner, tmp_path):
    """If the SDK raises rather than returning a verdict, the command still exits 1.

    `ManifestOperation.validate` is written to return a result object, so the outer
    handler in the command is only reached if something below it breaks — a bad install,
    say. Pinned because the alternative is a traceback and exit 1 from Python itself,
    which is much harder to read in a pipeline log.
    """
    from idp_cli.cli import validate_manifest_cmd

    manifest = tmp_path / "m.csv"
    manifest.write_text("document_path\ns3://bucket/a.pdf\n")

    client = MagicMock()
    client.manifest.validate.side_effect = RuntimeError("sdk exploded")

    with patch("idp_cli.cli.IDPClient", return_value=client):
        result = runner.invoke(validate_manifest_cmd, ["--manifest", str(manifest)])

    assert result.exit_code == 1, result.output
    assert "✗ Error: sdk exploded" in result.output


@pytest.mark.unit
def test_validate_makes_no_aws_call(runner, tmp_path, api_calls):
    """Validation is purely local, and that is worth pinning.

    A future change that made the validator resolve a stack or read from S3 would turn
    an offline sanity check into something that needs credentials and a deployment. The
    API log being empty is the assertion.
    """
    from idp_cli.cli import validate_manifest_cmd

    manifest = tmp_path / "m.csv"
    manifest.write_text("document_path\ns3://bucket/a.pdf\n")

    result = runner.invoke(validate_manifest_cmd, ["--manifest", str(manifest)])

    assert result.exit_code == 0, result.output
    assert api_calls.operations() == []


@pytest.mark.unit
def test_generate_then_validate_a_test_set_manifest_round_trip(runner, tmp_path):
    """The S3-flavoured manifest that `--test-set` writes is itself valid.

    `--test-set` rewrites every `document_path` to an `s3://` URI before writing the
    manifest, which takes it down a different branch of the parser than the local-path
    manifests above. Checking the round trip on that shape too is what proves the
    rewrite produced well-formed URIs rather than, say, a double slash.
    """
    from idp_cli.cli import generate_manifest, validate_manifest_cmd

    docs = tmp_path / "docs"
    _write(docs / "invoice.pdf")
    baselines = tmp_path / "baselines"
    (baselines / "invoice.pdf").mkdir(parents=True)
    output = tmp_path / "manifest.csv"

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(
            Bucket=TEST_SET_BUCKET
        )
        with _patched_stack_resources({"TestSetBucket": TEST_SET_BUCKET}):
            generated = runner.invoke(
                generate_manifest,
                [
                    "--dir",
                    str(docs),
                    "--baseline-dir",
                    str(baselines),
                    "--test-set",
                    "set1",
                    "--stack-name",
                    "IDP",
                    "--output",
                    str(output),
                ],
            )
    assert generated.exit_code == 0, generated.output

    validated = runner.invoke(validate_manifest_cmd, ["--manifest", str(output)])
    assert validated.exit_code == 0, validated.output
    assert "✓ Manifest is valid" in validated.output


@pytest.mark.unit
def test_next_steps_guidance_differs_by_what_was_produced(runner, tmp_path):
    """Three mutually exclusive closing messages, one per mode.

    The closing block is the only thing that tells a user what to run next, and which
    of the three branches fires depends on `test_set` and then on whether any baseline
    matched. A plain manifest gets the "edit it, then process" instructions; a manifest
    with baselines gets the evaluation message instead and NOT the editing
    instructions. Asserting the negative matters here: both branches printing would
    give contradictory advice.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "invoice.pdf")

    plain = tmp_path / "plain.csv"
    plain_result = runner.invoke(
        generate_manifest, ["--dir", str(docs), "--output", str(plain)]
    )
    assert plain_result.exit_code == 0, plain_result.output
    assert "Next steps:" in plain_result.output
    assert "Edit manifest to add baseline_source" in plain_result.output
    assert "Baseline matching complete" not in plain_result.output

    baselines = tmp_path / "baselines"
    (baselines / "invoice.pdf").mkdir(parents=True)
    matched = tmp_path / "matched.csv"
    matched_result = runner.invoke(
        generate_manifest,
        [
            "--dir",
            str(docs),
            "--baseline-dir",
            str(baselines),
            "--output",
            str(matched),
        ],
    )
    assert matched_result.exit_code == 0, matched_result.output
    assert "Baseline matching complete" in matched_result.output
    assert "Ready to process with evaluations!" in matched_result.output
    assert "Edit manifest to add baseline_source" not in matched_result.output


@pytest.mark.unit
def test_an_unmatched_baseline_dir_does_not_claim_the_manifest_is_ready_to_evaluate(
    runner, tmp_path
):
    """A baseline directory that matched nothing warns, and the closing guidance is right.

    The closing branch is `elif baseline_map:`, and `baseline_map` is now keyed on the
    documents that *matched* rather than on the directories that were *found*, so a
    baseline directory named so that nothing matches no longer reaches it. It used to:
    every row's `baseline_source` was empty and the command still printed "Ready to
    process with evaluations!", with the `Matched 0/1` line above as the only
    contradicting evidence on screen.

    Both warnings are asserted, because they say different things — one names the
    directory that will not be uploaded, the other says no document got a baseline at
    all — and a user who supplied baselines needs the second to know the manifest is
    unlabeled.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    _write(docs / "invoice.pdf")
    baselines = tmp_path / "baselines"
    (baselines / "unrelated").mkdir(parents=True)
    output = tmp_path / "m.csv"

    result = runner.invoke(
        generate_manifest,
        ["--dir", str(docs), "--baseline-dir", str(baselines), "--output", str(output)],
    )

    assert result.exit_code == 0, result.output
    assert "Matched 0/1 documents to baselines" in result.output
    assert "matched no document and will not be uploaded: unrelated" in result.output
    assert "none matched a document" in result.output
    assert "Ready to process with evaluations!" not in result.output, (
        "nothing is ready to evaluate: every baseline_source is empty"
    )
    assert [row["baseline_source"] for row in _read_manifest_rows(output)] == [""]
    # Without --test-set the manifest is still written, because the user can edit it --
    # which is what the guidance that does print tells them to do.
    assert "Edit manifest to add baseline_source" in result.output


@pytest.mark.unit
def test_region_is_threaded_into_the_s3_client(runner, tmp_path):
    """`--region` must reach the S3 client, or the scan hits the wrong partition.

    Asserted on the client boto3 was asked to build rather than on the request, because
    the region is a property of the client's endpoint and moto answers either way.
    """
    from idp_cli.cli import generate_manifest

    output = tmp_path / "m.csv"
    built = []
    real_client = boto3.client

    def _recording(service, **kwargs):
        built.append((service, kwargs.get("region_name")))
        return real_client(service, **kwargs)

    with mock_aws():
        real_client("s3", region_name="us-west-2").create_bucket(
            Bucket="docs-bucket",
            CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
        )
        real_client("s3", region_name="us-west-2").put_object(
            Bucket="docs-bucket", Key="a.pdf", Body=b"pdf"
        )
        with patch("idp_cli.cli.boto3.client", side_effect=_recording):
            result = runner.invoke(
                generate_manifest,
                [
                    "--s3-uri",
                    "s3://docs-bucket",
                    "--output",
                    str(output),
                    "--region",
                    "us-west-2",
                ],
            )

    assert result.exit_code == 0, result.output
    assert ("s3", "us-west-2") in built


@pytest.mark.unit
def test_documents_are_counted_and_reported_before_the_manifest_is_written(
    runner, tmp_path
):
    """The progress lines are the only feedback for a long scan, so pin their content.

    A user watching `generate-manifest` over a large bucket has nothing else to tell
    them whether the pattern matched anything, so the scan line, the count and the
    written-file confirmation are all asserted together.
    """
    from idp_cli.cli import generate_manifest

    docs = tmp_path / "docs"
    for index in range(3):
        _write(docs / f"doc{index}.pdf")
    output = tmp_path / "m.csv"

    result = runner.invoke(
        generate_manifest, ["--dir", str(docs), "--output", str(output)]
    )

    assert result.exit_code == 0, result.output
    assert f"Scanning directory: {docs}" in result.output
    assert "Found 3 documents" in result.output
    assert f"✓ Generated manifest: {output}" in result.output
    assert len(_read_manifest_rows(output)) == 3
