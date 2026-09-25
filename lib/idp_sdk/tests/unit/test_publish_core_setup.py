# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for ``IDPPublisher``'s construction, console logging, CLI argument
parsing and prerequisite checks.

``idp_sdk._core.publish.IDPPublisher`` is the build/publish orchestrator behind
``idp-cli publish`` (and the ``publish.py`` entry point). The methods covered
here run *before* anything is built: they parse the positional and optional
arguments, derive the bucket name and the versioned S3 prefix from them, delete
the checksum caches when a full rebuild is asked for, discover the
``requirements.txt`` files the build will consume, and verify that ``sam`` is new
enough to run.

What shaped these tests
-----------------------
Almost every method here reads or writes **relative paths** (``./VERSION``,
``nested/``, ``patterns/``, ``.aws-sam/layers``, ``src/lambda``), so every
filesystem test builds a miniature project under ``tmp_path`` and
``monkeypatch.chdir``s into it. Nothing is written outside ``tmp_path``.

Two areas got adversarial rather than confirmatory tests:

* ``version_compare`` decides whether a prerequisite check passes. A wrong
  answer is silent, so the tests cover numeric-vs-lexicographic ordering
  (``1.10`` against ``1.9``), zero-padding of unequal lengths, and the
  non-numeric segment rule — including the two cases where that rule makes a
  malformed version compare as *newer* than any minimum.
* ``check_parameters`` is the whole of this tool's input validation, so the
  tests assert what it accepts as well as what it rejects, and pin the inputs it
  accepts that a reader would expect it to reject.

The console is replaced with a ``rich.Console`` writing to a ``StringIO`` so the
rendered text can be asserted on; that is an instance attribute assignment, not
a change to the module.
"""

from __future__ import annotations

import io
import subprocess
import sys
import types
from pathlib import Path

import pytest
from rich.console import Console

from idp_sdk._core import publish as publish_mod
from idp_sdk._core.publish import IDPPublisher

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_publisher(verbose: bool = False) -> tuple[IDPPublisher, io.StringIO]:
    """Build a publisher whose console renders into a buffer we can assert on.

    ``no_color`` and a wide terminal keep the captured text free of ANSI escapes
    and of wrapping, so an assertion on a substring means what it looks like.
    """
    pub = IDPPublisher(verbose=verbose)
    buf = io.StringIO()
    pub.console = Console(file=buf, width=300, no_color=True, highlight=False)
    return pub, buf


def write(path: Path, content: str = "x") -> Path:
    """Create ``path`` (and its parents) with ``content``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------


def test_a_new_publisher_starts_with_containers_on_and_linting_on():
    """The defaults decide what a bare ``publish`` run does.

    ``pattern2_use_containers`` and ``lint_enabled`` both default to on, and
    ``main_template`` names the template the uploader later rewrites. A silent
    flip of any of these changes what ships.
    """
    pub = IDPPublisher()

    assert pub.verbose is False
    assert pub.pattern2_use_containers is True
    assert pub.lint_enabled is True
    assert pub.skip_validation is False
    assert pub.main_template == "idp-main.yaml"
    assert pub.use_container_flag == ""
    assert pub.public is False
    assert pub.headless is False
    assert pub.govcloud is False
    assert pub.public_sample_udop_model == ""

    # Nothing is resolved until check_parameters/setup_environment run.
    assert pub.bucket_basename is None
    assert pub.prefix is None
    assert pub.region is None
    assert pub.bucket is None
    assert pub.version is None
    assert pub.prefix_and_version is None
    assert pub.account_id is None
    assert pub.s3_client is None
    assert pub.cf_client is None
    assert pub.sts_client is None


def test_verbose_is_taken_from_the_constructor():
    assert IDPPublisher(verbose=True).verbose is True


def test_two_publishers_do_not_share_their_error_and_layer_collections():
    """``build_errors`` and ``_layer_arns`` are per-instance.

    Were either a class attribute, one publisher's failures would show up in
    another's error summary, and a layer ARN built for one region would be
    injected into a template published to a different one.
    """
    first = IDPPublisher()
    second = IDPPublisher()

    first.build_errors.append({"component": "a", "error": "boom"})
    first._layer_arns["SharedLayer"] = "arn:aws:lambda:us-east-1:1:layer:x:1"

    assert second.build_errors == []
    assert second._layer_arns == {}


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def test_log_verbose_is_silent_unless_verbose_is_set():
    quiet, quiet_buf = make_publisher(verbose=False)
    quiet.log_verbose("installing idp_common[extraction]")
    assert quiet_buf.getvalue() == ""

    loud, loud_buf = make_publisher(verbose=True)
    loud.log_verbose("installing idp_common[extraction]")
    assert "installing idp_common[extraction]" in loud_buf.getvalue()


def test_log_verbose_does_not_eat_a_bracketed_extra():
    """``markup=False`` is load-bearing here.

    Verbose output quotes pip requirement strings such as
    ``lib/idp_common_pkg[extraction]``. With Rich markup enabled the
    ``[extraction]`` is parsed as a style tag and vanishes, which would make the
    verbose log name the wrong dependency.
    """
    pub, buf = make_publisher(verbose=True)
    pub.log_verbose("pip install -e ./lib/idp_common_pkg[ocr,classification]")
    assert "[ocr,classification]" in buf.getvalue()


def test_log_phase_upper_cases_the_title_and_draws_two_rules():
    pub, buf = make_publisher()
    pub.log_phase("uploading config library", "📂")
    out = buf.getvalue()

    assert "UPLOADING CONFIG LIBRARY" in out
    assert "📂" in out
    assert out.count("═" * 65) == 2


def test_log_phase_without_an_emoji_still_prints_the_title():
    pub, buf = make_publisher()
    pub.log_phase("build")
    out = buf.getvalue()
    assert "BUILD" in out
    assert out.count("═" * 65) == 2


@pytest.mark.parametrize(
    "method,marker",
    [
        ("log_task", "▶"),
        ("log_detail", "└─"),
        ("log_success", "✓"),
        ("log_cached", "→"),
        ("log_warning", "⚠"),
        ("log_error", "✗"),
    ],
)
def test_each_log_level_prints_its_own_marker_and_the_message(method, marker):
    pub, buf = make_publisher()
    getattr(pub, method)("template packaged")
    out = buf.getvalue()
    assert marker in out
    assert "template packaged" in out


@pytest.mark.parametrize(
    "method",
    ["log_task", "log_detail", "log_success", "log_cached", "log_warning", "log_error"],
)
def test_the_thread_prefix_is_swallowed_by_rich_markup(method):
    """DEFECT, pinned as current behaviour: the ``thread=`` label never prints.

    Each of these six helpers builds ``prefix = f"[{thread}] "`` and passes the
    result to ``Console.print`` with markup left **enabled**
    (``publish.py:198,203,208,213,218,223``). Rich reads ``[patterns/unified]``
    as a style tag, not as literal text, and removes it from the output — so the
    rendered line contains the message and no component name at all.

    The consequence is worst exactly where these helpers were introduced for:
    ``_build_components_concurrently`` logs one line per component from a thread
    pool, including ``log_error("Build failed!", thread=component)``. With the
    label gone, a console showing several interleaved ``✗ Build failed!`` lines
    does not say which pattern failed.

    This test asserts the label is absent. If the helpers are fixed to pass
    ``markup=False`` or to escape the prefix, this test will fail and should be
    replaced by its positive form.
    """
    pub, buf = make_publisher()
    getattr(pub, method)("Building...", thread="patterns/unified")
    out = buf.getvalue()

    assert "Building..." in out
    assert "patterns/unified" not in out


def test_print_usage_names_every_optional_flag_check_parameters_accepts():
    """Usage text and the parser have to agree, or a documented flag is a no-op.

    Every flag asserted here is one ``check_parameters`` handles by name; a flag
    added to the parser without a usage line is a flag nobody can discover.
    """
    pub, buf = make_publisher()
    pub.print_usage()
    out = buf.getvalue()

    for token in (
        "<cfn_bucket_basename>",
        "<cfn_prefix>",
        "<region>",
        "public",
        "--max-workers",
        "--verbose",
        "--no-validate",
        "--clean-build",
        "--lint",
    ):
        assert token in out, token


def test_print_error_summary_prints_nothing_when_there_are_no_errors():
    pub, buf = make_publisher()
    pub.print_error_summary()
    assert buf.getvalue() == ""


def test_the_error_summary_truncates_to_three_lines_and_says_how_many_it_hid():
    """Non-verbose mode shows a three-line preview plus an accurate remainder.

    An off-by-one in the remainder count would send a reader looking for output
    that is not there.
    """
    pub, buf = make_publisher(verbose=False)
    pub.build_errors = [
        {
            "component": "SAM build for patterns/unified",
            "error": "\n".join(f"line{i}" for i in range(1, 8)),
        }
    ]
    pub.print_error_summary()
    out = buf.getvalue()

    assert "1. SAM build for patterns/unified" in out
    assert "line1" in out and "line2" in out and "line3" in out
    assert "line4" not in out
    assert "(4 more lines" in out


def test_the_error_summary_prints_every_line_in_verbose_mode():
    pub, buf = make_publisher(verbose=True)
    pub.build_errors = [
        {"component": "UI build", "error": "line1\nline2\nline3\nline4\nline5"}
    ]
    pub.print_error_summary()
    out = buf.getvalue()

    assert "line5" in out
    assert "more lines" not in out


def test_a_short_error_gets_no_truncation_notice():
    pub, buf = make_publisher(verbose=False)
    pub.build_errors = [{"component": "lint", "error": "only one line"}]
    pub.print_error_summary()
    out = buf.getvalue()
    assert "only one line" in out
    assert "more lines" not in out


def test_the_error_summary_numbers_every_error():
    pub, buf = make_publisher(verbose=False)
    pub.build_errors = [
        {"component": "alpha", "error": "a"},
        {"component": "beta", "error": "b"},
        {"component": "gamma", "error": "c"},
    ]
    pub.print_error_summary()
    out = buf.getvalue()
    assert "1. alpha" in out
    assert "2. beta" in out
    assert "3. gamma" in out


# ---------------------------------------------------------------------------
# check_parameters
# ---------------------------------------------------------------------------


def test_the_three_positional_arguments_land_where_setup_environment_reads_them():
    pub, _ = make_publisher()
    pub.check_parameters(["idp-artifacts", "idp", "us-west-2"])

    assert pub.bucket_basename == "idp-artifacts"
    assert pub.prefix == "idp"
    assert pub.region == "us-west-2"
    assert pub.public is False
    assert pub.acl == "bucket-owner-full-control"
    assert pub.max_workers is None


def test_a_trailing_slash_is_stripped_from_the_prefix():
    """The prefix is concatenated as ``f"{prefix}/{version}"``.

    A surviving trailing slash would produce ``idp//0.6.8`` — a valid but
    different S3 key space, so templates published under one spelling would not
    be found under the other.
    """
    pub, _ = make_publisher()
    pub.check_parameters(["b", "idp/nightly/", "us-east-1"])
    assert pub.prefix == "idp/nightly"


def test_only_a_trailing_slash_run_is_stripped_not_a_leading_one():
    pub, _ = make_publisher()
    pub.check_parameters(["b", "/idp///", "us-east-1"])
    assert pub.prefix == "/idp"


@pytest.mark.parametrize("args", [[], ["bucket"], ["bucket", "prefix"]])
def test_fewer_than_three_positionals_exits_non_zero_with_usage(args):
    pub, buf = make_publisher()
    with pytest.raises(SystemExit) as exc:
        pub.check_parameters(args)
    assert exc.value.code == 1
    assert "Missing required parameters" in buf.getvalue()
    assert "<cfn_bucket_basename>" in buf.getvalue()


@pytest.mark.parametrize("spelling", ["public", "PUBLIC", "Public"])
def test_the_public_keyword_is_case_insensitive_and_switches_the_acl(spelling):
    """``public`` is what makes the published artifacts world-readable.

    Getting this wrong in either direction is consequential: a missed ``public``
    publishes templates nobody can deploy from, and a spuriously matched one
    exposes a private build.
    """
    pub, buf = make_publisher()
    pub.check_parameters(["b", "p", "us-east-1", spelling])

    assert pub.public is True
    assert pub.acl == "public-read"
    assert "accessible by public" in buf.getvalue()
    assert "NOT be accessible" not in buf.getvalue()


def test_a_private_publish_says_so():
    pub, buf = make_publisher()
    pub.check_parameters(["b", "p", "us-east-1"])
    assert "will NOT be accessible by public" in buf.getvalue()


def test_max_workers_takes_the_following_argument_as_its_value():
    pub, _ = make_publisher()
    pub.check_parameters(["b", "p", "us-east-1", "--max-workers", "4", "--verbose"])

    assert pub.max_workers == 4
    # The value must not be re-read as a flag of its own, and the flag after it
    # must still be seen.
    assert pub.verbose is True


@pytest.mark.parametrize("value", ["0", "-3"])
def test_max_workers_below_one_exits(value):
    pub, buf = make_publisher()
    with pytest.raises(SystemExit) as exc:
        pub.check_parameters(["b", "p", "us-east-1", "--max-workers", value])
    assert exc.value.code == 1
    assert "at least 1" in buf.getvalue()


def test_a_non_numeric_max_workers_exits_with_usage():
    pub, buf = make_publisher()
    with pytest.raises(SystemExit) as exc:
        pub.check_parameters(["b", "p", "us-east-1", "--max-workers", "many"])
    assert exc.value.code == 1
    assert "valid number" in buf.getvalue()


def test_max_workers_as_the_last_argument_exits():
    pub, buf = make_publisher()
    with pytest.raises(SystemExit) as exc:
        pub.check_parameters(["b", "p", "us-east-1", "--max-workers"])
    assert exc.value.code == 1
    assert "requires a number" in buf.getvalue()


@pytest.mark.parametrize("flag", ["--verbose", "-v"])
def test_both_verbose_spellings_enable_verbose(flag):
    pub, buf = make_publisher(verbose=False)
    pub.check_parameters(["b", "p", "us-east-1", flag])
    assert pub.verbose is True
    assert "Verbose mode enabled" in buf.getvalue()


def test_no_validate_sets_skip_validation():
    pub, buf = make_publisher()
    pub.check_parameters(["b", "p", "us-east-1", "--no-validate"])
    assert pub.skip_validation is True
    assert "validation will be skipped" in buf.getvalue()


@pytest.mark.parametrize(
    "value,expected", [("on", True), ("off", False), ("ON", True), ("OFF", False)]
)
def test_lint_on_and_off_are_accepted_case_insensitively(value, expected):
    pub, _ = make_publisher()
    pub.check_parameters(["b", "p", "us-east-1", "--lint", value])
    assert pub.lint_enabled is expected


def test_the_lint_value_is_not_reparsed_as_an_unknown_argument():
    """``--lint off`` must consume its value.

    If the ``i += 1`` that skips the value were dropped, ``off`` would fall to
    the unknown-argument branch and emit a spurious warning — which is how a
    reader learns to ignore that warning.
    """
    pub, buf = make_publisher()
    pub.check_parameters(["b", "p", "us-east-1", "--lint", "off"])
    assert "Unknown argument" not in buf.getvalue()


def test_an_invalid_lint_value_exits():
    pub, buf = make_publisher()
    with pytest.raises(SystemExit) as exc:
        pub.check_parameters(["b", "p", "us-east-1", "--lint", "maybe"])
    assert exc.value.code == 1
    assert "must be 'on' or 'off'" in buf.getvalue()


def test_lint_as_the_last_argument_exits():
    pub, buf = make_publisher()
    with pytest.raises(SystemExit) as exc:
        pub.check_parameters(["b", "p", "us-east-1", "--lint"])
    assert exc.value.code == 1
    assert "requires 'on' or 'off'" in buf.getvalue()


def test_clean_build_deletes_the_checksum_cache_during_parsing(tmp_path, monkeypatch):
    """``--clean-build`` has its effect while arguments are being parsed.

    The test uses a real checksum file so the deletion is observed on disk
    rather than as a call on a mock.
    """
    monkeypatch.chdir(tmp_path)
    write(tmp_path / ".checksum", "deadbeef")

    pub, _ = make_publisher()
    pub.check_parameters(["b", "p", "us-east-1", "--clean-build"])

    assert not (tmp_path / ".checksum").exists()


def test_an_unknown_argument_is_warned_about_and_ignored():
    pub, buf = make_publisher()
    pub.check_parameters(["b", "p", "us-east-1", "--turbo"])
    assert "Unknown argument '--turbo' ignored" in buf.getvalue()
    # Parsing continues rather than aborting.
    assert pub.region == "us-east-1"


def test_positional_arguments_are_accepted_without_any_validation():
    """Pinned gap: nothing here validates the bucket name, prefix or region.

    ``check_parameters`` is the only place a ``publish`` invocation's arguments
    are inspected, and it checks arity and the optional flags only. An empty
    prefix, an upper-case bucket basename (illegal in an S3 bucket name) and a
    region that does not exist are all accepted here and fail later — the region
    at client-construction or API-call time in ``setup_environment``, the bucket
    name at ``CreateBucket``.

    This is recorded rather than asserted-as-good: the observable consequence is
    that the failure surfaces after work has been done, with a message about S3
    rather than about the command line.
    """
    pub, _ = make_publisher()
    pub.check_parameters(["Not_A_Valid_Bucket", "", "not-a-region"])

    assert pub.bucket_basename == "Not_A_Valid_Bucket"
    assert pub.prefix == ""
    assert pub.region == "not-a-region"


def test_all_optional_arguments_can_be_combined():
    pub, _ = make_publisher()
    pub.check_parameters(
        [
            "bkt",
            "idp",
            "us-west-2",
            "public",
            "--max-workers",
            "2",
            "--verbose",
            "--no-validate",
            "--lint",
            "off",
        ]
    )

    assert (pub.public, pub.acl) == (True, "public-read")
    assert pub.max_workers == 2
    assert pub.verbose is True
    assert pub.skip_validation is True
    assert pub.lint_enabled is False


# ---------------------------------------------------------------------------
# setup_environment
# ---------------------------------------------------------------------------


def test_setup_environment_derives_the_bucket_and_versioned_prefix(
    tmp_path, monkeypatch, aws_credentials
):
    """The two strings every upload in this module is keyed on.

    ``bucket`` is ``<basename>-<region>`` and ``prefix_and_version`` is
    ``<prefix>/<VERSION>``. A wrong value here does not fail — it publishes a
    complete, working artifact set to the wrong place.
    """
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "VERSION", "0.6.8\n")

    pub, _ = make_publisher()
    pub.check_parameters(["idp-artifacts", "idp", "us-west-2"])
    pub.setup_environment()

    assert pub.version == "0.6.8"
    assert pub.bucket == "idp-artifacts-us-west-2"
    assert pub.prefix_and_version == "idp/0.6.8"


def test_setup_environment_strips_whitespace_from_the_version_file(
    tmp_path, monkeypatch, aws_credentials
):
    """A stray newline in ``VERSION`` would become part of every S3 key."""
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "VERSION", "  0.7.0-rc1 \n\n")

    pub, _ = make_publisher()
    pub.check_parameters(["b", "idp", "us-east-1"])
    pub.setup_environment()

    assert pub.version == "0.7.0-rc1"
    assert pub.prefix_and_version == "idp/0.7.0-rc1"


def test_setup_environment_points_the_region_and_clients_at_the_requested_region(
    tmp_path, monkeypatch, aws_credentials
):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "VERSION", "1.0.0")

    pub, _ = make_publisher()
    pub.check_parameters(["b", "idp", "eu-west-1"])
    pub.setup_environment()

    import os

    assert os.environ["AWS_DEFAULT_REGION"] == "eu-west-1"
    assert pub.s3_client.meta.region_name == "eu-west-1"
    assert pub.cf_client.meta.region_name == "eu-west-1"
    assert pub.s3_client.meta.service_model.service_name == "s3"
    assert pub.cf_client.meta.service_model.service_name == "cloudformation"


def test_setup_environment_builds_a_region_local_udop_model_url(
    tmp_path, monkeypatch, aws_credentials
):
    """The sample UDOP model is fetched from a per-region blog bucket.

    A region-less URL would make every non-``us-east-1`` deployment pull the
    model cross-region, or fail outright.
    """
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "VERSION", "1.0.0")

    pub, _ = make_publisher()
    pub.check_parameters(["b", "idp", "ap-southeast-2"])
    pub.setup_environment()

    assert pub.public_sample_udop_model == (
        "s3://aws-ml-blog-ap-southeast-2/artifacts/genai-idp/"
        "udop-finetuning/rvl-cdip/model.tar.gz"
    )


def test_a_missing_version_file_exits_one(tmp_path, monkeypatch, aws_credentials):
    monkeypatch.chdir(tmp_path)

    pub, buf = make_publisher()
    pub.check_parameters(["b", "idp", "us-east-1"])
    with pytest.raises(SystemExit) as exc:
        pub.setup_environment()

    assert exc.value.code == 1
    assert "VERSION file not found" in buf.getvalue()


# ---------------------------------------------------------------------------
# check_prerequisites
# ---------------------------------------------------------------------------


def _fake_subprocess(stdout: str = "SAM CLI, version 1.140.0\n", raise_called=False):
    """A stand-in for the ``subprocess`` module as ``publish`` uses it.

    Only ``run`` and ``CalledProcessError`` are reached by
    ``check_prerequisites``; patching a namespace rather than the real module
    keeps the real ``subprocess`` untouched for every other test in the session.
    """
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        if raise_called:
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    ns = types.SimpleNamespace(
        run=run, CalledProcessError=subprocess.CalledProcessError, calls=calls
    )
    return ns


def _patch_prereq_env(
    monkeypatch,
    *,
    which=lambda cmd: f"/usr/bin/{cmd}",
    sam_stdout="SAM CLI, version 1.140.0\n",
    raise_called=False,
    python=(3, 13),
):
    fake_sub = _fake_subprocess(sam_stdout, raise_called)
    monkeypatch.setattr(publish_mod, "shutil", types.SimpleNamespace(which=which))
    monkeypatch.setattr(publish_mod, "subprocess", fake_sub)
    monkeypatch.setattr(
        publish_mod,
        "sys",
        types.SimpleNamespace(
            version_info=types.SimpleNamespace(major=python[0], minor=python[1]),
            exit=sys.exit,
        ),
    )
    return fake_sub


def test_check_prerequisites_passes_and_asks_sam_for_its_version(monkeypatch):
    fake_sub = _patch_prereq_env(monkeypatch)
    pub, _ = make_publisher()

    pub.check_prerequisites()

    assert fake_sub.calls == [["sam", "--version"]]


@pytest.mark.parametrize("missing", ["aws", "sam", "uv"])
def test_a_missing_required_command_exits_one_and_names_it(monkeypatch, missing):
    """All three of ``aws``, ``sam`` and ``uv`` are required.

    Dropping one from the list would let a publish start and fail much later
    inside a build step, with an error naming a subprocess rather than a missing
    tool.
    """
    _patch_prereq_env(
        monkeypatch, which=lambda cmd: None if cmd == missing else f"/usr/bin/{cmd}"
    )
    pub, buf = make_publisher()

    with pytest.raises(SystemExit) as exc:
        pub.check_prerequisites()

    assert exc.value.code == 1
    assert f"{missing} is required but not installed" in buf.getvalue()


def test_a_sam_older_than_the_minimum_exits_one(monkeypatch):
    _patch_prereq_env(monkeypatch, sam_stdout="SAM CLI, version 1.128.9\n")
    pub, buf = make_publisher()

    with pytest.raises(SystemExit) as exc:
        pub.check_prerequisites()

    assert exc.value.code == 1
    out = buf.getvalue()
    assert "sam version >= 1.129.0 is required" in out
    assert "1.128.9" in out


def test_sam_exactly_at_the_minimum_is_accepted(monkeypatch):
    """The comparison is ``< 0``, so the minimum itself must pass.

    An off-by-one to ``<= 0`` would reject the exact version the message tells
    the user to install.
    """
    _patch_prereq_env(monkeypatch, sam_stdout="SAM CLI, version 1.129.0\n")
    pub, _ = make_publisher()
    pub.check_prerequisites()


def test_a_sam_that_cannot_report_its_version_exits_one(monkeypatch):
    _patch_prereq_env(monkeypatch, raise_called=True)
    pub, buf = make_publisher()

    with pytest.raises(SystemExit) as exc:
        pub.check_prerequisites()

    assert exc.value.code == 1
    assert "Could not determine SAM version" in buf.getvalue()


def test_a_python_older_than_3_12_exits_one(monkeypatch):
    _patch_prereq_env(monkeypatch, python=(3, 11))
    pub, buf = make_publisher()

    with pytest.raises(SystemExit) as exc:
        pub.check_prerequisites()

    assert exc.value.code == 1
    assert "Python version >= 3.12 is required" in buf.getvalue()
    assert "3.11" in buf.getvalue()


def test_an_unexpectedly_shaped_sam_version_line_raises_indexerror(monkeypatch):
    """DEFECT, pinned as current behaviour: ``publish.py:538`` is unguarded.

    The version is read as ``result.stdout.split()[3]``, and the ``except``
    below it catches only ``subprocess.CalledProcessError``. A ``sam --version``
    that exits 0 but prints fewer than four whitespace-separated tokens — a
    wrapper script, a localised build, or a future SAM whose banner changes —
    raises ``IndexError`` out of ``check_prerequisites``.

    The observable consequence is a bare traceback in place of the actionable
    "Could not determine SAM version" message the adjacent handler exists to
    print.
    """
    _patch_prereq_env(monkeypatch, sam_stdout="1.140.0\n")
    pub, _ = make_publisher()

    with pytest.raises(IndexError):
        pub.check_prerequisites()


# ---------------------------------------------------------------------------
# version_compare
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "left,right,expected",
    [
        # Equality, including across differing segment counts.
        ("1.129.0", "1.129.0", 0),
        ("1.2", "1.2.0", 0),
        ("1.2.0", "1.2", 0),
        ("3.12", "3.12.0.0", 0),
        # Ordering within a segment.
        ("1.129.0", "1.130.0", -1),
        ("1.130.0", "1.129.0", 1),
        ("2.0.0", "1.999.999", 1),
        # The classic failure: segments must compare numerically, not as text.
        # "1.10" < "1.9" lexicographically and "1.10" > "1.9" numerically.
        ("1.10", "1.9", 1),
        ("1.9", "1.10", -1),
        ("1.129.0", "1.9.0", 1),
        ("3.9", "3.12", -1),
        ("3.12", "3.9", 1),
        # A shorter version is smaller only when the padding zeros decide it.
        ("1.2", "1.2.1", -1),
        ("1.2.1", "1.2", 1),
    ],
)
def test_version_compare_orders_versions_numerically(left, right, expected):
    """A wrong answer here silently admits or rejects a SAM/Python version.

    ``check_prerequisites`` is the only consumer, and it compares ``< 0``, so an
    inverted result either blocks a valid toolchain or lets an unsupported one
    through to fail mid-build.
    """
    assert IDPPublisher().version_compare(left, right) == expected


def test_version_compare_is_antisymmetric_across_a_sorted_ladder():
    """Property check over a known ordering, rather than one pair at a time.

    Every pair drawn from an ascending list must compare ``-1`` one way and
    ``1`` the other, and ``0`` against itself. This catches an asymmetry that a
    handful of hand-picked pairs can miss.
    """
    pub = IDPPublisher()
    ladder = ["0.9.9", "1.0.0", "1.0.1", "1.9.0", "1.10.0", "1.129.0", "2.0.0"]

    for i, lower in enumerate(ladder):
        assert pub.version_compare(lower, lower) == 0
        for higher in ladder[i + 1 :]:
            assert pub.version_compare(lower, higher) == -1, (lower, higher)
            assert pub.version_compare(higher, lower) == 1, (higher, lower)


def test_a_nightly_build_string_compares_above_any_release():
    """Documented intent: a non-numeric segment becomes 999 so nightlies pass.

    ``sam`` nightly builds report versions like ``1.140.0.dev202606120901``, and
    the normaliser turns any non-integer segment into 999 specifically so the
    prerequisite check does not reject them.
    """
    pub = IDPPublisher()
    assert pub.version_compare("1.140.0.dev202606120901", "1.129.0") == 1
    assert pub.version_compare("dev202606120901", "1.129.0") == 1


def test_a_prerelease_segment_compares_above_its_own_release():
    """Consequence of the 999 rule, pinned so a change to it is visible.

    ``1.130.0-rc1`` splits to ``["1", "130", "0-rc1"]``, and the last segment
    normalises to 999 — so a release candidate reads as ``1.130.999``, above the
    final ``1.130.0`` it precedes *and above every real patch release in that
    minor line*. Prerequisite checks only ask "at least", so this direction is
    harmless there; it is pinned because the same helper would give the wrong
    answer if it were reused to pick the newer of two versions.
    """
    pub = IDPPublisher()
    assert pub.version_compare("1.130.0-rc1", "1.130.0") == 1
    assert pub.version_compare("1.130.0-rc1", "1.130.1") == 1
    assert pub.version_compare("1.130.0-rc1", "1.130.998") == 1
    # Only the next minor outranks it.
    assert pub.version_compare("1.130.0-rc1", "1.131.0") == -1


def test_an_empty_version_string_passes_every_minimum(monkeypatch):
    """DEFECT, pinned as current behaviour: ``publish.py:564-573``.

    ``"".split(".")`` is ``[""]``, ``int("")`` raises, and the ``except
    ValueError`` branch substitutes 999 — so the empty string compares as
    *greater than* any real minimum.

    The observable consequence is in ``check_prerequisites``: if ``sam
    --version`` ever emits a line whose fourth token is empty, the version gate
    passes rather than printing "Could not determine SAM version". The test
    drives that through the real call path as well as the comparison, so it
    pins the reachable effect and not just the arithmetic.
    """
    pub = IDPPublisher()
    assert pub.version_compare("", "1.129.0") == 1
    assert pub.version_compare("1.0.", "1.0.5") == 1

    # The same input reaching check_prerequisites is accepted silently.
    _patch_prereq_env(monkeypatch, sam_stdout="SAM CLI, version  extra\n")
    reachable, _ = make_publisher()
    reachable.check_prerequisites()


# ---------------------------------------------------------------------------
# clean_checksums
# ---------------------------------------------------------------------------


def test_clean_checksums_deletes_every_cache_it_knows_and_nothing_else(
    tmp_path, monkeypatch
):
    """The deletion set is the whole point: a missed file means a stale artifact.

    The fixture holds one of each thing the method is meant to delete — the root
    and ``lib`` checksums, a per-nested-stack and a per-pattern checksum, and
    cached layer zips — alongside decoys that must survive: a non-zip file in the
    layers directory, real source files, and a checksum belonging to a *file*
    (not a directory) under ``nested/``.
    """
    monkeypatch.chdir(tmp_path)

    write(tmp_path / ".checksum", "root")
    write(tmp_path / "lib" / ".checksum", "lib")
    write(tmp_path / "nested" / "api-resolvers" / ".checksum", "nested-a")
    write(tmp_path / "nested" / "bedrockkb" / ".checksum", "nested-b")
    write(tmp_path / "patterns" / "unified" / ".checksum", "pattern")
    write(tmp_path / ".aws-sam" / "layers" / "shared.zip", "zipbytes")
    write(tmp_path / ".aws-sam" / "layers" / "ocr.zip", "zipbytes")

    # Decoys.
    write(tmp_path / ".aws-sam" / "layers" / "manifest.json", "{}")
    write(tmp_path / "nested" / "README.md", "not a directory")
    write(tmp_path / "patterns" / "unified" / "template.yaml", "Resources: {}")
    write(tmp_path / "src" / ".checksum", "not in the known set")

    pub, buf = make_publisher()
    pub.clean_checksums()

    for gone in [
        tmp_path / ".checksum",
        tmp_path / "lib" / ".checksum",
        tmp_path / "nested" / "api-resolvers" / ".checksum",
        tmp_path / "nested" / "bedrockkb" / ".checksum",
        tmp_path / "patterns" / "unified" / ".checksum",
        tmp_path / ".aws-sam" / "layers" / "shared.zip",
        tmp_path / ".aws-sam" / "layers" / "ocr.zip",
    ]:
        assert not gone.exists(), gone

    for kept in [
        tmp_path / ".aws-sam" / "layers" / "manifest.json",
        tmp_path / "nested" / "README.md",
        tmp_path / "patterns" / "unified" / "template.yaml",
        tmp_path / "src" / ".checksum",
    ]:
        assert kept.exists(), kept

    assert "Deleted 7 cache files" in buf.getvalue()


def test_clean_checksums_reports_a_clean_tree_rather_than_a_deletion(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    pub, buf = make_publisher()
    pub.clean_checksums()

    out = buf.getvalue()
    assert "No cache files found to delete" in out
    assert "Deleted" not in out


def test_clean_checksums_survives_a_tree_with_no_nested_or_patterns_directory(
    tmp_path, monkeypatch
):
    """Both directory scans are guarded by ``os.path.exists``.

    A publish run from a partial checkout — or from the SDK's own package
    directory — must not raise here.
    """
    monkeypatch.chdir(tmp_path)
    write(tmp_path / ".checksum", "root")

    pub, buf = make_publisher()
    pub.clean_checksums()

    assert not (tmp_path / ".checksum").exists()
    assert "Deleted 1 cache files" in buf.getvalue()


# ---------------------------------------------------------------------------
# _find_all_requirements_files
# ---------------------------------------------------------------------------


def test_requirements_discovery_covers_all_three_lambda_trees(tmp_path, monkeypatch):
    """Lambda requirements live in three differently shaped trees.

    ``src/lambda/<fn>/``, ``nested/<stack>/src/<fn>/`` and
    ``patterns/<pattern>/src/<fn>/`` — note the extra ``src`` level in the last
    two. A tree missed here is a Lambda whose dependency changes do not
    invalidate any checksum.
    """
    monkeypatch.chdir(tmp_path)

    write(tmp_path / "src" / "lambda" / "queue_processor" / "requirements.txt", "boto3")
    write(tmp_path / "src" / "lambda" / "queue_sender" / "requirements.txt", "boto3")
    write(
        tmp_path / "nested" / "api-resolvers" / "src" / "resolver" / "requirements.txt",
        "boto3",
    )
    write(
        tmp_path / "patterns" / "unified" / "src" / "ocr_function" / "requirements.txt",
        "../../lib/idp_common_pkg[ocr]",
    )

    found = set(IDPPublisher()._find_all_requirements_files())

    assert found == {
        "src/lambda/queue_processor/requirements.txt",
        "src/lambda/queue_sender/requirements.txt",
        "nested/api-resolvers/src/resolver/requirements.txt",
        "patterns/unified/src/ocr_function/requirements.txt",
    }


def test_requirements_discovery_does_not_descend_past_the_function_directory(
    tmp_path, monkeypatch
):
    """Only the immediate ``<fn>/requirements.txt`` is collected.

    A ``requirements.txt`` nested one level deeper, or one sitting beside the
    function directories rather than inside one, is not returned. This pins the
    depth so a change to the walk is visible.
    """
    monkeypatch.chdir(tmp_path)

    write(tmp_path / "src" / "lambda" / "fn" / "requirements.txt", "boto3")
    write(tmp_path / "src" / "lambda" / "fn" / "vendor" / "requirements.txt", "deeper")
    write(tmp_path / "src" / "lambda" / "requirements.txt", "beside")
    write(tmp_path / "nested" / "stack" / "requirements.txt", "no src level")

    found = IDPPublisher()._find_all_requirements_files()

    assert found == ["src/lambda/fn/requirements.txt"]


def test_requirements_discovery_ignores_directories_without_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    write(tmp_path / "src" / "lambda" / "with_reqs" / "requirements.txt", "boto3")
    (tmp_path / "src" / "lambda" / "without_reqs").mkdir(parents=True)
    write(tmp_path / "src" / "lambda" / "without_reqs" / "index.py", "pass")
    (tmp_path / "nested" / "no_src_dir").mkdir(parents=True)
    (tmp_path / "patterns" / "no_src_dir").mkdir(parents=True)

    assert IDPPublisher()._find_all_requirements_files() == [
        "src/lambda/with_reqs/requirements.txt"
    ]


def test_requirements_discovery_returns_empty_for_a_tree_with_none(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    assert IDPPublisher()._find_all_requirements_files() == []


# ---------------------------------------------------------------------------
# _prepare_for_build_at_start
# ---------------------------------------------------------------------------


def test_prepare_for_build_at_start_only_reports_in_verbose_mode():
    """The method is a documented placeholder; its one effect is a verbose line.

    Asserted so that adding a real startup check here — which is what the
    docstring anticipates — shows up as a change rather than as new behaviour
    nothing observes.
    """
    quiet, quiet_buf = make_publisher(verbose=False)
    quiet._prepare_for_build_at_start()
    assert quiet_buf.getvalue() == ""

    loud, loud_buf = make_publisher(verbose=True)
    loud._prepare_for_build_at_start()
    assert "Build startup checks complete" in loud_buf.getvalue()
