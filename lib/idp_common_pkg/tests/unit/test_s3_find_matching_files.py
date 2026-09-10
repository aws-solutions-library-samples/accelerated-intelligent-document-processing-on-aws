# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""How a test-set file pattern selects S3 keys.

Reported as #808: ``folder1/**/*.pdf`` matched nothing although the bucket held two
thousand nested PDFs. The pattern was translated with ``*`` -> ``[^/]*`` and no
``**`` rule, so no wildcard could cross ``/``; literals were not escaped, so a ``.``
matched any character and ``(``/``+``/``[`` in a name mis-matched or raised; and
matching was case-sensitive, so ``*.pdf`` missed ``.PDF``. The current rules were a
deliberate 2025-11 reversal of an earlier ``.*`` + IGNORECASE translation, with no
rationale recorded, and are replaced here by ordinary glob semantics.

The listing is also narrowed to the folder the pattern names (S3 ``Prefix``), which
is exact-case — so the folder path before the first wildcard must match as typed
and everything after it is case-insensitive. That split is asserted explicitly.
"""

from unittest.mock import Mock, patch

import pytest

from idp_common.s3 import find_matching_files


def _s3_with_keys(mock_get_client, keys):
    """One paginator page holding ``keys``; returns the paginator for call asserts."""
    mock_s3 = Mock()
    mock_get_client.return_value = mock_s3
    paginator = Mock()
    mock_s3.get_paginator.return_value = paginator
    paginator.paginate.return_value = [{"Contents": [{"Key": k} for k in keys]}]
    return paginator


@pytest.mark.unit
class TestFindMatchingFiles:
    """The wildcard rules, one per test."""

    @patch("idp_common.s3.get_s3_client")
    def test_matching_ignores_case(self, mock_get_client):
        # A glob treats ``.pdf`` and ``.PDF`` as the same extension; the earlier
        # case-sensitive rule silently dropped the upper-case half of a folder.
        _s3_with_keys(mock_get_client, ["File.txt", "file.txt", "FILE.txt"])

        assert find_matching_files("bucket", "file*") == [
            "FILE.txt",
            "File.txt",
            "file.txt",
        ]

    @patch("idp_common.s3.get_s3_client")
    def test_mixed_case_extension(self, mock_get_client):
        _s3_with_keys(mock_get_client, ["a.pdf", "b.PDF", "c.Pdf", "d.txt"])

        assert find_matching_files("bucket", "*.pdf") == ["a.pdf", "b.PDF", "c.Pdf"]

    @patch("idp_common.s3.get_s3_client")
    def test_wildcard_no_directory_crossing(self, mock_get_client):
        """``*`` still stays within one folder level."""
        _s3_with_keys(
            mock_get_client,
            ["test.txt", "test123.pdf", "test/nested.txt", "test/sub/deep.txt"],
        )

        assert find_matching_files("bucket", "test*") == ["test.txt", "test123.pdf"]

    @patch("idp_common.s3.get_s3_client")
    def test_directory_pattern_matching(self, mock_get_client):
        _s3_with_keys(
            mock_get_client,
            ["dir/file1.txt", "dir/file2.pdf", "dir/other.doc", "dir/sub/nested.txt"],
        )

        assert find_matching_files("bucket", "dir/file*") == [
            "dir/file1.txt",
            "dir/file2.pdf",
        ]

    @patch("idp_common.s3.get_s3_client")
    def test_question_mark_wildcard(self, mock_get_client):
        """``?`` matches one character and not ``/``."""
        _s3_with_keys(
            mock_get_client, ["file1.txt", "file2.txt", "file12.txt", "file/.txt"]
        )

        assert find_matching_files("bucket", "file?.txt") == ["file1.txt", "file2.txt"]

    @patch("idp_common.s3.get_s3_client")
    def test_globstar_matches_nested_at_any_depth(self, mock_get_client):
        """The reported case: every PDF under a folder, however deep."""
        _s3_with_keys(
            mock_get_client,
            [
                "folder1/a.pdf",
                "folder1/x/b.pdf",
                "folder1/x/y/c.pdf",
                "folder1/x/y/d.txt",
                "other/e.pdf",
            ],
        )

        assert find_matching_files("bucket", "folder1/**/*.pdf") == [
            "folder1/a.pdf",
            "folder1/x/b.pdf",
            "folder1/x/y/c.pdf",
        ]

    @patch("idp_common.s3.get_s3_client")
    def test_globstar_slash_matches_zero_directories(self, mock_get_client):
        # ``a/**/b.pdf`` includes ``a/b.pdf``: ``**/`` is zero or more folders, as
        # in every shell that supports it.
        _s3_with_keys(mock_get_client, ["a/b.pdf", "a/c/b.pdf", "a/b.pdfx"])

        assert find_matching_files("bucket", "a/**/b.pdf") == ["a/b.pdf", "a/c/b.pdf"]

    @patch("idp_common.s3.get_s3_client")
    def test_leading_globstar_matches_root_and_nested(self, mock_get_client):
        paginator = _s3_with_keys(
            mock_get_client, ["r.pdf", "d/r.pdf", "d/e/r.pdf", "d/r.txt"]
        )

        assert find_matching_files("bucket", "**/*.pdf") == [
            "d/e/r.pdf",
            "d/r.pdf",
            "r.pdf",
        ]
        # Nothing literal precedes the wildcard, so the whole bucket is listed.
        paginator.paginate.assert_called_once_with(Bucket="bucket")

    @patch("idp_common.s3.get_s3_client")
    def test_literal_dot_is_not_a_wildcard(self, mock_get_client):
        _s3_with_keys(mock_get_client, ["data.json", "dataXjson", "data-json"])

        assert find_matching_files("bucket", "data.json") == ["data.json"]

    @patch("idp_common.s3.get_s3_client")
    def test_regex_metacharacters_in_pattern_are_literal(self, mock_get_client):
        """Document names carry ``(``, ``+`` and ``[``; none of them is syntax."""
        _s3_with_keys(
            mock_get_client,
            ["doc (1)+.pdf", "doc 1.pdf", "doc 11.pdf", "report[1].pdf"],
        )

        assert find_matching_files("bucket", "doc (1)+.pdf") == ["doc (1)+.pdf"]
        # ``[1]`` used to reach re.compile as a character class.
        assert find_matching_files("bucket", "report[1].pdf") == ["report[1].pdf"]

    @patch("idp_common.s3.get_s3_client")
    def test_lists_under_the_literal_folder_prefix(self, mock_get_client):
        """Only the folder the pattern names is listed, not the whole bucket."""
        for pattern, prefix in [
            ("folder1/**/*.pdf", "folder1/"),
            ("a/b/c*.pdf", "a/b/"),
            ("a/b?/c", "a/"),
            ("a/b/c.pdf", "a/b/"),
        ]:
            paginator = _s3_with_keys(mock_get_client, [])
            find_matching_files("bucket", pattern)
            paginator.paginate.assert_called_once_with(Bucket="bucket", Prefix=prefix)

    @patch("idp_common.s3.get_s3_client")
    def test_no_prefix_when_the_pattern_starts_with_a_wildcard(self, mock_get_client):
        # A partial name is never used as a prefix either: ``invoices-*`` has no
        # folder before its wildcard.
        for pattern in ["*.pdf", "?bc/x", "test-set-prefix*/input/*", "invoices-*.pdf"]:
            paginator = _s3_with_keys(mock_get_client, [])
            find_matching_files("bucket", pattern)
            paginator.paginate.assert_called_once_with(Bucket="bucket")

    @patch("idp_common.s3.get_s3_client")
    def test_folder_path_is_exact_case_and_the_rest_is_not(self, mock_get_client):
        """The one asymmetry, stated: S3 prefixes are exact-case, the regex is not.

        The folder before the first wildcard is sent to S3 as the listing prefix,
        so it must be typed as it exists; file names and extensions after it match
        regardless of case.
        """
        paginator = _s3_with_keys(mock_get_client, ["Invoices/A.PDF", "Invoices/b.pdf"])

        assert find_matching_files("bucket", "Invoices/*.pdf") == [
            "Invoices/A.PDF",
            "Invoices/b.pdf",
        ]
        paginator.paginate.assert_called_once_with(Bucket="bucket", Prefix="Invoices/")

    @patch("idp_common.s3.get_s3_client")
    def test_skips_folder_pseudo_objects(self, mock_get_client):
        """Zero-byte '/'-terminated folder placeholders are never matched.

        The S3 console "Create folder" action writes a zero-byte object whose
        key ends in '/'. These are not documents and must be excluded even when
        the pattern would otherwise match them — including under ``**``.
        """
        _s3_with_keys(
            mock_get_client,
            [
                "testfolder/",
                "testfolder/doc1.pdf",
                "testfolder/sub/",
                "testfolder/sub/x.pdf",
            ],
        )

        assert find_matching_files("bucket", "testfolder/*") == ["testfolder/doc1.pdf"]
        assert find_matching_files("bucket", "testfolder/**") == [
            "testfolder/doc1.pdf",
            "testfolder/sub/x.pdf",
        ]
        assert find_matching_files("bucket", "testfolder/") == []

    @patch("idp_common.s3.get_s3_client")
    def test_rule_validation_pattern_with_punctuated_input_key(self, mock_get_client):
        """The other caller: rule validation looks up its section files by pattern.

        The prefix is the document's own input key, which can carry spaces,
        parentheses and dots. Unescaped, ``(2)`` was a regex group and ``.`` any
        character, so a decoy key could match; and the whole output bucket was
        listed per document.
        """
        key = "inv (2).pdf/rule_validation/sections/section_1_responses.json"
        paginator = _s3_with_keys(
            mock_get_client,
            [key, "invX(2).pdf/rule_validation/sections/section_1_responses.json"],
        )

        pattern = "inv (2).pdf/rule_validation/sections/section_*_responses.json"
        assert find_matching_files("bucket", pattern) == [key]
        paginator.paginate.assert_called_once_with(
            Bucket="bucket", Prefix="inv (2).pdf/rule_validation/sections/"
        )

    @patch("idp_common.s3.get_s3_client")
    def test_globstar_slash_needs_its_slash(self, mock_get_client):
        # ``a/**/b`` is ``a/b`` or ``a/<folders>/b``; a body that has consumed
        # characters cannot fall through without a closing ``/``, so ``a/xb``
        # and ``a/x/yb`` stay out.
        _s3_with_keys(mock_get_client, ["a/b", "a/x/b", "a/x/y/b", "a/xb", "a/x/yb"])

        assert find_matching_files("bucket", "a/**/b") == ["a/b", "a/x/b", "a/x/y/b"]

    @patch("idp_common.s3.get_s3_client")
    def test_many_wildcards_against_a_deep_key_stay_fast(self, mock_get_client):
        """The pattern is user input; matching must not be exponential in it.

        A backtracking regex for these rules took 4 s per key at four ``**/``
        groups on a 600-character non-matching key and did not finish in two
        minutes at 25. The matcher walks each key once, so the same work takes
        milliseconds. The bound below is deliberately loose: it separates
        "finishes" from "hangs", and must hold on a slow CI runner under
        coverage instrumentation, not just on a fast laptop.
        """
        import time

        deep_key = "a/" + "/".join("d" for _ in range(300)) + "/y.pdf"
        _s3_with_keys(mock_get_client, [deep_key] * 5)

        started = time.monotonic()
        assert find_matching_files("bucket", "a/" + "**/" * 25 + "z.pdf") == []
        assert find_matching_files("bucket", "*a" * 200) == []
        assert time.monotonic() - started < 10.0
