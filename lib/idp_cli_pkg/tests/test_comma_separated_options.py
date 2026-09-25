# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for `idp-cli`'s comma-separated options, as a class rather than one at a time.

`str.split(",")` never returns an empty list. `"".split(",")` is `[""]` and
`"a,".split(",")` is `["a", ""]`, so a blank segment arrives as a *value* that is the
empty string, an `if not values` guard after a bare split is unreachable code, and a
length check counts a trailing comma as another value. Seven option parsers in `cli.py`
split on a comma; the two `--test-run-ids` sites were corrected, and five others carried
the identical bare split for a further release. That is the defect shape these tests are
aimed at -- **a fix applied to the instance and not to the class** -- so the file has two
layers.

`test_every_comma_split_in_cli_goes_through_the_shared_parser` is the structural one, and
it is the layer that matters. It derives the set of comma-splitting call sites from
`cli.py`'s **AST** rather than from a list written here, and fails if any of them sits
outside `_comma_separated_values`. A site added later is therefore covered without anyone
remembering to add a case below, which is the only property that makes "the class is
fixed" a claim rather than a hope. Its universe is asserted non-empty first: an AST walk
that finds nothing would satisfy every assertion over it, and that is the shape a vacuous
authority takes.

It has **no exemption list**, deliberately. `_parse_tags` was the one candidate -- it
splits each segment again on `=` -- and rather than carve it out, its comma split was
routed through the shared parser as well, which is where the stripping and the
blank-dropping it used to inline now happen. An exemption with one member is still a
list somebody has to keep true; `str.split(",")` appearing exactly once in the module is
not. (`_parse_tags`'s own behaviour over blank segments is covered in
`test_cli_module_helpers.py::TestParseTags`.)

The behavioural layer is one refusal per site, because the structural test cannot say
what the user sees. `--document-ids` on `delete-documents` is the exception: it lives in
`test_delete_commands.py::test_a_document_ids_value_of_only_commas_is_refused`, where the
recorded-API-call fixtures already needed for that command show that the refusal lands
before any `DeleteObject` is attempted.
"""

from __future__ import annotations

import ast
import inspect
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from idp_cli.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


# The string-splitting methods that can take a separator, and the module-level
# splitters. All of them are matched, because "the next comma-separated option" is not
# guaranteed to be spelled `.split(",")`.
_SPLIT_METHODS = frozenset({"split", "rsplit", "partition", "rpartition"})


def _comma_split_sites():
    """Every comma-splitting call in `cli.py`, as `(line, source)`.

    Read out of the AST rather than by matching text, so a reformatting of the call or a
    different variable name cannot hide one. Four things this deliberately does *not*
    restrict, each of which was a hole when this walked only function bodies looking for
    a positional `.split(",")`:

    - The walk is over the **whole module**, so a split at module scope (a constant
      built at import) or inside an `ast.Lambda` is seen. Neither is inside a
      `FunctionDef`.
    - The separator is read from the **positional or the `sep=` keyword** argument, so
      `value.split(sep=",")` is seen.
    - `partition` and `rpartition` count as splitting on a comma, because they are how
      somebody would write "the part before the first comma".
    - `re.split` with a comma in its pattern counts too.

    No enclosing-function name is returned, because filtering on one is what let a
    *second* bare split inside `_comma_separated_values` pass. The caller pins the count
    instead.
    """
    from idp_cli import cli as cli_module

    source = inspect.getsource(cli_module)
    tree = ast.parse(source)

    def comma_separator(call):
        """The separator constant this call splits on, or None if it is not one."""
        candidates = list(call.args)
        candidates += [
            keyword.value
            for keyword in call.keywords
            if keyword.arg in {"sep", "pattern"}
        ]
        for node in candidates:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "," in node.value:
                    return node.value
        return None

    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        is_str_split = func.attr in _SPLIT_METHODS
        is_re_split = (
            func.attr == "split"
            and isinstance(func.value, ast.Name)
            and func.value.id == "re"
        )
        if not (is_str_split or is_re_split):
            continue
        if comma_separator(node) is None:
            continue
        sites.append((node.lineno, ast.unparse(node)))
    return sorted(sites)


@pytest.mark.unit
def test_every_comma_split_in_cli_goes_through_the_shared_parser():
    """No comma-separated option in `cli.py` is parsed with a bare `str.split(",")`.

    The universe is derived from the AST, so this covers a call site added after this
    test was written -- which is the whole point, because five sites carried this defect
    through the release in which two others were fixed.

    The assertion is a **count pin**: `cli.py` contains exactly one comma-splitting
    call, and it is the one inside `_comma_separated_values`. Counting rather than
    filtering by the enclosing function's name is deliberate -- a name filter exempts
    *any* split inside the helper, so a second bare split added there passed, and it
    says nothing at all about a split at module scope or inside a lambda, neither of
    which is inside a `FunctionDef`.

    The count also gives the non-vacuity guard for free: an AST walk that quietly stopped
    matching would report zero, not one, and fail here rather than passing over an empty
    universe.
    """
    sites = _comma_split_sites()

    assert len(sites) == 1, (
        "cli.py must contain exactly one comma-splitting call, inside "
        "`_comma_separated_values`. Anything else is an option parser splitting for "
        "itself, so a blank segment reaches it as a value that is the empty string. "
        f"Found {len(sites)}: {sites}"
    )

    from idp_cli.cli import _comma_separated_values

    helper_source, helper_first_line = inspect.getsourcelines(_comma_separated_values)
    helper_lines = range(helper_first_line, helper_first_line + len(helper_source))
    (lineno, src) = sites[0]
    assert lineno in helper_lines, (
        f"the one comma split is at line {lineno} ({src}), which is outside "
        f"_comma_separated_values (lines {helper_lines.start}-{helper_lines.stop - 1})"
    )


@pytest.mark.unit
def test_the_tag_parser_reaches_the_shared_split_and_still_drops_blanks():
    """`_parse_tags` routes its comma split through the shared parser.

    It was the one plausible carve-out, so what it gets instead is a positive
    assertion: the behaviour it used to inline -- strip each segment, drop the blank
    ones -- must survive the delegation, over a trailing comma, a doubled comma and an
    all-commas value. The structural test above is what keeps it delegating; this is
    what says the delegation did not change the answer.
    """
    from idp_cli.cli import _parse_tags

    assert _parse_tags("a=1,") == {"a": "1"}
    assert _parse_tags("a=1,,b=2") == {"a": "1", "b": "2"}
    assert _parse_tags(" a = 1 , b = 2 ") == {"a": "1", "b": "2"}
    assert _parse_tags(",") == {}
    assert _parse_tags(" , ") == {}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", []),
        (",", []),
        (" ", []),
        (",,", []),
        (" , , ", []),
        ("a", ["a"]),
        ("a,", ["a"]),
        (",a", ["a"]),
        ("a, ,b", ["a", "b"]),
        (" a , b ", ["a", "b"]),
    ],
)
def test_the_shared_parser_drops_blanks_and_strips(value, expected):
    """The parser's contract, including that it can return an empty list.

    Returning `[]` is the point rather than an edge case: it is what makes each caller's
    own emptiness check reachable, which a bare `split` never did.
    """
    from idp_cli.cli import _comma_separated_values

    assert _comma_separated_values(value) == expected


# ================================================================================
# One refusal per call site. `delete-documents --document-ids` is covered in
# test_delete_commands.py, where the recorded-call fixtures for that command show the
# refusal landing before any DeleteObject.
# ================================================================================


@pytest.mark.unit
@pytest.mark.parametrize("command", ["reprocess", "rerun-inference"])
def test_reprocess_refuses_a_document_ids_value_of_only_commas(runner, command):
    """Two commands share `_rerun_inference_impl`, so both reach the same parser.

    `--document-ids ","` used to be announced as "Processing 2 specified documents" and
    then sent to the SDK as two document IDs that are the empty string. It is refused
    before the SDK is asked to do anything, which is asserted as well as the exit code --
    a refusal printed after the reprocess was requested would not be one.
    """
    with patch("idp_sdk.IDPClient") as client_cls:
        client = MagicMock()
        client_cls.return_value = client
        result = runner.invoke(
            cli,
            [
                command,
                "--stack-name",
                "my-stack",
                "--document-ids",
                ",",
                "--step",
                "classification",
                "--force",
            ],
        )

    assert result.exit_code == 1, result.output
    assert "--document-ids contains no document IDs" in result.output
    assert "Processing" not in result.output, (
        "no count may be announced for a list that named nothing"
    )
    assert client.batch.rerun_inference.called is False
    assert client.batch.reprocess.called is False


@pytest.mark.unit
def test_download_results_refuses_a_file_types_value_of_only_commas(runner, tmp_path):
    """`--file-types ","` used to reach the SDK as two artifact types named `""`.

    The SDK filters the S3 listing by that value, so the download quietly produced an
    empty output directory and the command reported the files it did not write.
    """
    with patch("idp_sdk.IDPClient") as client_cls:
        client = MagicMock()
        client_cls.return_value = client
        result = runner.invoke(
            cli,
            [
                "download-results",
                "--stack-name",
                "my-stack",
                "--batch-id",
                "b1",
                "--output-dir",
                str(tmp_path / "out"),
                "--file-types",
                ",",
            ],
        )

    assert result.exit_code == 1, result.output
    assert "--file-types contains no file types" in result.output
    assert client.batch.download_results.called is False


@pytest.mark.unit
def test_remove_deleted_stack_resources_refuses_a_regions_value_of_only_commas(runner):
    """`--check-stack-regions ","` used to search a region whose name is `""`.

    The refusal is the first thing the command does, before any client is built, so this
    test needs no AWS at all -- which is itself the assertion worth having: the value is
    rejected at parse time rather than by whatever a region named `""` does to boto3.
    """
    result = runner.invoke(
        cli, ["remove-deleted-stack-resources", "--check-stack-regions", ",", "--yes"]
    )

    assert result.exit_code == 1, result.output
    assert "--check-stack-regions contains no regions" in result.output
    assert "CLEANUP SUMMARY" not in result.output


@pytest.mark.unit
def test_config_create_refuses_a_features_value_of_only_commas(runner):
    """`--features ","` used to ask for two configuration sections named `""`.

    The comma is what tells a list apart from a preset name, so that branch is unchanged
    and `--features min` still reaches the preset path. Only a list whose every segment
    is blank is refused -- asserted against both, because a refusal that also caught the
    presets would break the default invocation.
    """
    refused = runner.invoke(cli, ["config-create", "--features", ","])
    assert refused.exit_code == 1, refused.output
    assert "--features contains no section names" in refused.output

    # The preset path is untouched: no comma, so it never reaches the parser.
    preset = runner.invoke(cli, ["config-create", "--features", "min"])
    assert preset.exit_code == 0, preset.output
    assert preset.stdout.strip(), "the preset path must still generate a template"


@pytest.mark.unit
def test_config_create_still_accepts_a_list_with_a_stray_trailing_comma(runner):
    """`--features "classification,extraction,"` keeps both real sections.

    A trailing comma is the ordinary shape of a shell-built list, and the fix must drop
    the blank rather than the value beside it. Compared against the same list without
    the comma, so this cannot pass by both being empty.
    """
    with_comma = runner.invoke(
        cli, ["config-create", "--features", "classification,extraction,"]
    )
    without = runner.invoke(
        cli, ["config-create", "--features", "classification,extraction"]
    )

    assert with_comma.exit_code == 0, with_comma.output
    assert without.exit_code == 0, without.output
    assert without.stdout.strip(), "the comparison would be vacuous against no output"
    assert with_comma.stdout == without.stdout
