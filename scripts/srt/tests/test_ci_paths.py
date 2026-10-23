"""Tests for scripts/srt/ci_paths.py and the committed SRT suppression baseline.

The baseline test is the durable guard: it fails if a finding on a gitignored or
deleted path gets committed into scripts/srt/issues.json again. That pollution
is not harmless — SRT keys suppressions on (path, resourceType, resourceName,
check_id), so an artifact-path entry can never cover the real source template,
and one left at "resolved" re-detects as "reopened", which DOES gate CI.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SRT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SRT_DIR.parent.parent
sys.path.insert(0, str(SRT_DIR))

import ci_paths  # noqa: E402
from ci_paths import (  # noqa: E402
    _normalize,
    is_gating_status,
    is_in_ci_checkout,
    is_test_only_path,
    packaged_source_dirs,
    partition_by_ci_visibility,
    tracked_files,
)

pytestmark = pytest.mark.unit


class TestNormalize:
    def test_plain_relative_path_unchanged(self):
        assert _normalize("nested/api-resolvers/template.yaml") == (
            "nested/api-resolvers/template.yaml"
        )

    def test_strips_dot_slash_prefix(self):
        assert _normalize("./template.yaml") == "template.yaml"

    def test_converts_backslashes(self):
        assert _normalize("nested\\bedrockkb\\template.yaml") == (
            "nested/bedrockkb/template.yaml"
        )

    def test_empty_and_none(self):
        assert _normalize("") is None
        assert _normalize(None) is None


class TestIsInCiCheckout:
    TRACKED = {"template.yaml", "nested/api-resolvers/template.yaml"}

    def test_tracked_path_is_visible(self):
        assert is_in_ci_checkout("template.yaml", self.TRACKED) is True

    def test_build_artifact_is_not_visible(self):
        assert is_in_ci_checkout(".aws-sam/idp-main.yaml", self.TRACKED) is False
        assert (
            is_in_ci_checkout(
                "nested/api-resolvers/.aws-sam/packaged.yaml", self.TRACKED
            )
            is False
        )

    def test_fails_closed_when_tracked_set_unknown(self):
        # If we could not ask git, everything must still gate — never silently
        # drop a finding we failed to classify.
        assert is_in_ci_checkout(".aws-sam/idp-main.yaml", None) is True

    def test_pathless_finding_gates(self):
        assert is_in_ci_checkout(None, self.TRACKED) is True
        assert is_in_ci_checkout("", self.TRACKED) is True


class TestTrackedFiles:
    def test_lists_this_repo(self):
        tracked = tracked_files(PROJECT_ROOT)
        assert tracked is not None
        assert "template.yaml" in tracked
        # Build output is gitignored, so it must never appear.
        assert not any(".aws-sam/" in p for p in tracked)

    def test_returns_none_outside_a_git_repo(self, capsys):
        with tempfile.TemporaryDirectory() as tmp:
            # Isolate from any enclosing repo (e.g. /tmp inside a checkout).
            subprocess.run(
                ["git", "init", "-q", tmp], check=True, capture_output=True
            )
            Path(tmp, ".git").rename(Path(tmp, "not-git"))
            assert tracked_files(tmp) is None
        assert "Warning" in capsys.readouterr().out


class TestGatingStatus:
    def test_open_and_reopened_gate(self):
        assert is_gating_status("Open") is True
        # Regression guard for the 0.6.5 escape: SRT flips a re-detected
        # 'resolved' finding to 'reopened', which must still fail the build.
        # 'resolved' is not a sticky disposition; only 'suppressed' is.
        assert is_gating_status("reopened") is True

    def test_dispositioned_statuses_do_not_gate(self):
        assert is_gating_status("suppressed") is False
        assert is_gating_status("resolved") is False

    def test_missing_status_does_not_gate(self):
        assert is_gating_status(None) is False
        assert is_gating_status("") is False


class TestPartition:
    def test_splits_on_trackedness(self):
        issues = [
            {"check_id": "DDB-002", "path": "template.yaml"},
            {"check_id": "DDB-002", "path": ".aws-sam/idp-main.yaml"},
        ]
        ci_visible, local_only = partition_by_ci_visibility(issues, PROJECT_ROOT)
        assert [i["path"] for i in ci_visible] == ["template.yaml"]
        assert [i["path"] for i in local_only] == [".aws-sam/idp-main.yaml"]


class TestCommittedBaseline:
    """scripts/srt/issues.json must only carry findings CI can actually see."""

    @staticmethod
    def _baseline():
        with open(SRT_DIR / "issues.json", encoding="utf-8") as f:
            return json.load(f)

    def test_no_entries_on_untracked_paths(self):
        _, local_only = partition_by_ci_visibility(self._baseline(), PROJECT_ROOT)
        offenders = sorted(
            {
                f"{i.get('path')} ({i.get('check_id')} {i.get('resourceName')})"
                for i in local_only
            }
        )
        assert not offenders, (
            "scripts/srt/issues.json has entries on gitignored or deleted paths. "
            "These can never match a real finding in CI (the suppression key "
            "includes the path) and a stale 'resolved' one re-detects as "
            "'reopened', which gates the build. Remove them; run 'make srt-clean' "
            "before scanning locally.\n  " + "\n  ".join(offenders)
        )

    def test_suppressed_entries_carry_a_reason(self):
        offenders = [
            f"{i.get('check_id')} {i.get('resourceName')} in {i.get('path')}"
            for i in self._baseline()
            if (i.get("status") or "").lower() == "suppressed"
            and not (i.get("suppressionReason") or "").strip()
        ]
        assert not offenders, (
            "Every suppressed finding needs a suppressionReason so reviewers can "
            "audit the accepted risk:\n  " + "\n  ".join(offenders)
        )


class TestAwsProfileBootstrap:
    """`srt config` needs a profile to exist, so setup.py has to be able to make one.

    The scan is static analysis and needs no credentials, but `srt config` — the
    command that installs the five scanners — enumerates profiles and aborts with
    "✗ No AWS profiles found!" when there are none. setup.py used to create one only
    via `aws configure`, i.e. only when the image happened to ship awscli. It does on
    the GitLab runner and does not in python:3.13-bookworm, so the GitHub job failed
    at setup with every scanner missing. These tests pin the CLI-less path, and that
    it can never clobber a developer's real config.
    """

    @staticmethod
    def _setup_module():
        import importlib.util

        path = PROJECT_ROOT / "scripts" / "srt" / "setup.py"
        spec = importlib.util.spec_from_file_location("srt_setup", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_writes_a_default_profile_when_none_exists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        self._setup_module().write_aws_profile("default", "us-east-1")
        config = tmp_path / ".aws" / "config"
        assert config.exists()
        # `default` is spelled [default]; anything else is [profile name].
        assert "[default]" in config.read_text()
        assert "region = us-east-1" in config.read_text()

    def test_named_profile_uses_the_profile_prefix(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        self._setup_module().write_aws_profile("srtci", "eu-west-1")
        assert "[profile srtci]" in (tmp_path / ".aws" / "config").read_text()

    def test_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        mod = self._setup_module()
        mod.write_aws_profile("default", "us-east-1")
        mod.write_aws_profile("default", "us-west-2")
        text = (tmp_path / ".aws" / "config").read_text()
        assert text.count("[default]") == 1
        # The second call must not rewrite the region either.
        assert "us-east-1" in text and "us-west-2" not in text

    def test_never_clobbers_an_existing_config(self, tmp_path, monkeypatch):
        """A developer's real credentials/settings must survive untouched."""
        monkeypatch.setenv("HOME", str(tmp_path))
        aws = tmp_path / ".aws"
        aws.mkdir()
        (aws / "config").write_text("[profile mine]\nregion = ap-southeast-2\n")
        self._setup_module().write_aws_profile("default", "us-east-1")
        text = (aws / "config").read_text()
        assert "[profile mine]" in text and "ap-southeast-2" in text
        assert "[default]" in text

    def test_file_is_not_world_readable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        self._setup_module().write_aws_profile("default", "us-east-1")
        mode = (tmp_path / ".aws" / "config").stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


class TestTheRegionItRecords:
    """The region this setup writes must be the developer's, or nothing.

    The `aws configure set` branch **overwrites**, unlike write_aws_profile above,
    and the caller counts a non-tty stdin as CI — so this ran on any piped or
    tool-driven `make srt-setup`, not only in a pipeline. Reading only
    AWS_DEFAULT_REGION therefore rewrote `[default]` (or whatever AWS_PROFILE
    named) to a hardcoded us-east-1 on the machine of anyone who exports just
    AWS_REGION, which the AWS CLI and every SDK honour equally.
    """

    _setup_module = staticmethod(TestAwsProfileBootstrap._setup_module)

    def test_aws_default_region_wins_when_both_are_set(self, monkeypatch):
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        assert self._setup_module().resolve_aws_region() == "eu-west-1"

    def test_aws_region_is_honoured_when_it_is_the_only_one_set(self, monkeypatch):
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        assert self._setup_module().resolve_aws_region() == "us-west-2"

    def test_an_empty_value_does_not_count_as_a_choice(self, monkeypatch):
        """Exporting the name with no value is not a region; keep looking."""
        monkeypatch.setenv("AWS_DEFAULT_REGION", "")
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        assert self._setup_module().resolve_aws_region() == "us-west-2"

    def test_falls_back_only_when_neither_is_set(self, monkeypatch):
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)
        assert self._setup_module().resolve_aws_region() == "us-east-1"

    def test_a_profile_with_a_region_is_reported_as_having_one(self, monkeypatch):
        mod = self._setup_module()
        monkeypatch.setattr(
            mod.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, "us-west-2\n", ""),
        )
        assert mod.aws_cli_profile_has_region("default") is True

    def test_an_unset_region_is_reported_as_absent(self, monkeypatch):
        """`aws configure get` exits 1 and prints nothing for an unset value."""
        mod = self._setup_module()
        monkeypatch.setattr(
            mod.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""),
        )
        assert mod.aws_cli_profile_has_region("default") is False

    def test_a_zero_exit_with_empty_output_is_also_absent(self, monkeypatch):
        """Do not treat a blank answer as a region just because the exit was 0."""
        mod = self._setup_module()
        monkeypatch.setattr(
            mod.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, "\n", ""),
        )
        assert mod.aws_cli_profile_has_region("default") is False

    @pytest.mark.skipif(
        shutil.which("aws") is None, reason="needs the real AWS CLI to answer"
    )
    def test_the_real_aws_cli_agrees_about_both_answers(self, tmp_path, monkeypatch):
        """Pin the exit-code/empty-output contract against the CLI itself.

        The two mocked cases above encode an assumption about how `aws configure
        get` reports an unset value. If that ever stopped holding, the mocks would
        keep passing while the guard silently reported every profile as having a
        region and stopped writing one at all.
        """
        config = tmp_path / "config"
        config.write_text("[profile hasregion]\nregion = ap-southeast-2\n[profile bare]\noutput = json\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        # The env vars outrank the file for the CLI's own region resolution.
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)
        mod = self._setup_module()
        assert mod.aws_cli_profile_has_region("hasregion") is True
        assert mod.aws_cli_profile_has_region("bare") is False


class TestPackagedSourceDirsFromAContainerBuild:
    """A `PackageType: Image` function's source tree reaches the packaged set.

    Such a function declares no `CodeUri` — its template carries an `ImageUri` naming
    an ECR tag — so the directory it is built from is read from the buildspec its
    CodeBuild project points at. Before that, 15 of the 16 source trees under
    `patterns/unified/src/` were absent from the packaged set, which is the premise
    that makes `is_test_only_path`'s test-shape heuristic safe (#1389).

    Each case builds a throwaway repository rather than asserting against this one,
    so the failure modes that matter — a buildspec that cannot be found, one that
    cannot be parsed, PyYAML absent — are reachable at all. None of them exists in
    this tree, and a guard nothing can reach is a guard nobody has measured.
    """

    TEMPLATE = (
        "AWSTemplateFormatVersion: '2010-09-09'\n"
        "Resources:\n"
        "  DockerBuildProject:\n"
        "    Type: AWS::CodeBuild::Project\n"
        "    Properties:\n"
        "      Source:\n"
        "        Type: S3\n"
        "        BuildSpec: {spec}\n"
    )

    #: The `-` before `export` is the point: these are YAML list items, so a pattern
    #: anchored to leading whitespace followed by `export` matches none of them and
    #: reports zero, which reads exactly like the key being absent.
    BUILDSPEC = (
        "version: 0.2\n"
        "phases:\n"
        "  build:\n"
        "    commands:\n"
        '      - echo "Building..."\n'
        '      - export FUNCTION_ocr="src/ocr_function"\n'
    )

    @staticmethod
    def _repo(root, files, dirs=(), untracked=()):
        """A git repository holding `files`, with everything staged but `untracked`."""
        for rel, body in files.items():
            path = Path(root, rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        for rel in dirs:
            Path(root, rel).mkdir(parents=True, exist_ok=True)
            Path(root, rel, "index.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(
            ["git", "init", "-q", str(root)], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "add", "-A"], cwd=str(root), check=True, capture_output=True
        )
        for rel in untracked:
            path = Path(root, rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(files.get(rel, "version: 0.2\n"), encoding="utf-8")
        return root

    def test_a_tree_named_only_by_the_buildspec_is_packaged(self, tmp_path):
        self._repo(
            tmp_path,
            {
                "template.yaml": self.TEMPLATE.format(spec="buildspec.yml"),
                "buildspec.yml": self.BUILDSPEC,
            },
            dirs=("src/ocr_function",),
        )
        assert packaged_source_dirs(tmp_path) == frozenset({"src/ocr_function"})

    def test_the_tree_then_keeps_a_test_shaped_file_inside_it_gating(self, tmp_path):
        """The behaviour the packaged set exists to produce, end to end."""
        self._repo(
            tmp_path,
            {
                "template.yaml": self.TEMPLATE.format(spec="buildspec.yml"),
                "buildspec.yml": self.BUILDSPEC,
            },
            dirs=("src/ocr_function",),
        )
        packaged = packaged_source_dirs(tmp_path)
        assert not is_test_only_path("src/ocr_function/test_index.py", packaged)
        assert is_test_only_path("tests/unit/test_index.py", packaged)

    def test_a_bare_buildspec_name_resolves_beside_its_template(self, tmp_path):
        """A CodeBuild source zip whose root is the extension's own directory.

        Both spellings are live in this repository: the unified pattern names
        `patterns/unified/buildspec.yml`, repository-relative, while the data
        generator names a bare `buildspec.yml` sitting beside its template. Reading
        only the first makes the second unresolvable, which fails the whole answer
        closed and blinds the demotion for every finding in the repository.
        """
        self._repo(
            tmp_path,
            {
                "ext/template.yaml": self.TEMPLATE.format(spec="buildspec.yml"),
                "ext/buildspec.yml": self.BUILDSPEC,
            },
            dirs=("ext/src/ocr_function",),
        )
        assert packaged_source_dirs(tmp_path) == frozenset({"ext/src/ocr_function"})

    def test_an_inline_buildspec_contributes_nothing_and_is_a_known_answer(
        self, tmp_path
    ):
        """`BuildSpec: |` is a document, not a path, so there is nothing to resolve.

        Three templates here use that form. Reading the block-scalar introducer as a
        filename would make every one of them an unresolvable buildspec, and the
        answer would be None — every finding in the repository gating — rather than a
        set.
        """
        self._repo(
            tmp_path,
            {
                "template.yaml": (
                    "AWSTemplateFormatVersion: '2010-09-09'\n"
                    "Resources:\n"
                    "  DockerBuildProject:\n"
                    "    Type: AWS::CodeBuild::Project\n"
                    "    Properties:\n"
                    "      Source:\n"
                    "        BuildSpec: |\n"
                    "          version: 0.2\n"
                    "          phases:\n"
                    "            build:\n"
                    "              commands:\n"
                    "                - docker build -t x .\n"
                )
            },
            dirs=("src/ocr_function",),
        )
        assert packaged_source_dirs(tmp_path) == frozenset()

    def test_a_computed_buildspec_path_is_a_known_answer_too(self, tmp_path):
        """A `!Sub` names no file on disk, so there is nothing to fail to read."""
        self._repo(
            tmp_path,
            {
                "template.yaml": self.TEMPLATE.format(
                    spec='!Sub "${Prefix}/buildspec.yml"'
                )
            },
        )
        assert packaged_source_dirs(tmp_path) == frozenset()

    def test_a_buildspec_this_checkout_does_not_have_is_unknown(self, tmp_path, capsys):
        """Fail closed: the trees its images ship would otherwise be unknowable."""
        self._repo(
            tmp_path,
            {"template.yaml": self.TEMPLATE.format(spec="no-such-buildspec.yml")},
            dirs=("src/ocr_function",),
        )
        assert packaged_source_dirs(tmp_path) is None
        assert "no-such-buildspec.yml" in capsys.readouterr().out

    def test_an_untracked_buildspec_is_unknown(self, tmp_path, capsys):
        """CI checks out tracked files only, so an untracked one is not there.

        Reading it anyway would put directories in the packaged set locally that CI
        cannot derive, and the local and CI answers would differ silently.
        """
        self._repo(
            tmp_path,
            {"template.yaml": self.TEMPLATE.format(spec="buildspec.yml")},
            dirs=("src/ocr_function",),
            untracked=("buildspec.yml",),
        )
        assert packaged_source_dirs(tmp_path) is None
        assert "not a tracked file" in capsys.readouterr().out

    def test_an_unparseable_buildspec_is_unknown(self, tmp_path, capsys):
        self._repo(
            tmp_path,
            {
                "template.yaml": self.TEMPLATE.format(spec="buildspec.yml"),
                "buildspec.yml": "phases:\n  build:\n   commands:\n  - [unclosed\n",
            },
            dirs=("src/ocr_function",),
        )
        assert packaged_source_dirs(tmp_path) is None
        assert "could not parse" in capsys.readouterr().out

    def test_a_missing_yaml_library_refuses_rather_than_answering_at_all(
        self, tmp_path, monkeypatch
    ):
        """A missing parser is a setup fault, so it is neither answer this returns.

        It used to be None, the same "cannot tell" answer an unreadable buildspec
        gets, which is correct for the tree and wrong for the environment: the gate
        then reported every identifier-name finding in test code as a gating HIGH
        and exited 1, which is what a real regression looks like (#1428). The whole
        failure class is covered in `test_scan_dependencies.py`; this case stays
        here because the answers of this function belong together.
        """
        self._repo(
            tmp_path,
            {
                "template.yaml": self.TEMPLATE.format(spec="buildspec.yml"),
                "buildspec.yml": self.BUILDSPEC,
            },
            dirs=("src/ocr_function",),
        )
        monkeypatch.setattr(ci_paths, "_yaml_module", lambda: None)
        with pytest.raises(ci_paths.ScanDependencyError, match="PyYAML"):
            packaged_source_dirs(tmp_path)

    def test_a_commented_out_export_is_not_read_as_a_live_one(self, tmp_path):
        """What parsing the loaded YAML buys over scanning the raw lines."""
        self._repo(
            tmp_path,
            {
                "template.yaml": self.TEMPLATE.format(spec="buildspec.yml"),
                "buildspec.yml": (
                    "version: 0.2\n"
                    "phases:\n"
                    "  build:\n"
                    "    commands:\n"
                    '      # - export FUNCTION_ocr="src/ocr_function"\n'
                    '      - echo "nothing to build"\n'
                ),
            },
            dirs=("src/ocr_function",),
        )
        assert packaged_source_dirs(tmp_path) == frozenset()

    def test_an_export_assembled_from_another_variable_adds_nothing(self, tmp_path):
        """And is not a failure: the answer is a set, just without that tree in it."""
        self._repo(
            tmp_path,
            {
                "template.yaml": self.TEMPLATE.format(spec="buildspec.yml"),
                "buildspec.yml": (
                    "version: 0.2\n"
                    "phases:\n"
                    "  build:\n"
                    "    commands:\n"
                    '      - export FUNCTION_ocr="${BASE}/src/ocr_function"\n'
                ),
            },
            dirs=("src/ocr_function",),
        )
        assert packaged_source_dirs(tmp_path) == frozenset()

    def test_an_export_naming_a_directory_that_is_not_there_adds_nothing(
        self, tmp_path
    ):
        self._repo(
            tmp_path,
            {
                "template.yaml": self.TEMPLATE.format(spec="buildspec.yml"),
                "buildspec.yml": self.BUILDSPEC,
            },
        )
        assert packaged_source_dirs(tmp_path) == frozenset()

    def test_the_buildspec_route_adds_to_the_template_route(self, tmp_path):
        """Both packaging types in one template, which is the shape on this tree."""
        self._repo(
            tmp_path,
            {
                "template.yaml": (
                    self.TEMPLATE.format(spec="buildspec.yml") + "  HookFunction:\n"
                    "    Type: AWS::Serverless::Function\n"
                    "    Properties:\n"
                    "      CodeUri: src/hooks_function\n"
                ),
                "buildspec.yml": self.BUILDSPEC,
            },
            dirs=("src/ocr_function", "src/hooks_function"),
        )
        assert packaged_source_dirs(tmp_path) == frozenset(
            {"src/ocr_function", "src/hooks_function"}
        )

    def test_the_real_unified_buildspec_yields_every_tree_it_names(self):
        """Against this repository, so the parse is measured on the live spelling.

        The number is derived from the file rather than written here: every
        `export FUNCTION_` line in the buildspec the unified pattern's build project
        points at has to come back as a directory.
        """
        buildspec = PROJECT_ROOT / "patterns" / "unified" / "buildspec.yml"
        declared = {
            match.group("path")
            for match in ci_paths._FUNCTION_EXPORT_RE.finditer(
                buildspec.read_text(encoding="utf-8")
            )
        }
        assert len(declared) >= 10, f"only {len(declared)} exports found: {declared}"

        found = ci_paths._function_source_dirs(
            PROJECT_ROOT,
            "patterns/unified/buildspec.yml",
            buildspec.read_text(encoding="utf-8"),
        )
        assert found == declared, (
            "the parse did not return every tree the buildspec names. Missing: "
            f"{sorted(declared - (found or set()))}"
        )

    def test_a_quoted_buildspec_path_resolves(self, tmp_path):
        """YAML permits quoting a scalar, and the quotes are not part of the path.

        Reading them as part of it makes the file unresolvable, which fails the whole
        answer closed — every finding in the repository gating, for a template that
        is perfectly well formed.
        """
        self._repo(
            tmp_path,
            {
                "template.yaml": self.TEMPLATE.format(spec='"buildspec.yml"'),
                "buildspec.yml": self.BUILDSPEC,
            },
            dirs=("src/ocr_function",),
        )
        assert packaged_source_dirs(tmp_path) == frozenset({"src/ocr_function"})

    def test_a_buildspec_path_built_from_a_parameter_is_a_known_answer(self, tmp_path):
        """``BuildSpec: ${Thing}/buildspec.yml`` names no file in this checkout.

        Treating it as a path to look for would make it a buildspec that cannot be
        found, and the answer None rather than a set.
        """
        self._repo(
            tmp_path,
            {
                "template.yaml": self.TEMPLATE.format(spec="${Prefix}/buildspec.yml"),
                "buildspec.yml": self.BUILDSPEC,
            },
            dirs=("src/ocr_function",),
        )
        assert packaged_source_dirs(tmp_path) == frozenset()

    def test_an_export_naming_a_file_rather_than_a_directory_adds_nothing(
        self, tmp_path
    ):
        """What goes in the set is a directory; a file in it would never match."""
        self._repo(
            tmp_path,
            {
                "template.yaml": self.TEMPLATE.format(spec="buildspec.yml"),
                "buildspec.yml": (
                    "version: 0.2\n"
                    "phases:\n"
                    "  build:\n"
                    "    commands:\n"
                    '      - export FUNCTION_ocr="src/handler.py"\n'
                ),
                "src/handler.py": "x = 1\n",
            },
        )
        assert packaged_source_dirs(tmp_path) == frozenset()

    def test_two_buildspecs_in_one_template_both_contribute(self, tmp_path):
        """The per-buildspec results accumulate rather than replacing one another.

        No template here declares two today, which is exactly why this is driven
        synthetically: the difference between accumulating and overwriting is
        invisible against a tree where every template has one.
        """
        self._repo(
            tmp_path,
            {
                "template.yaml": (
                    self.TEMPLATE.format(spec="buildspec-a.yml") + "  SecondProject:\n"
                    "    Type: AWS::CodeBuild::Project\n"
                    "    Properties:\n"
                    "      Source:\n"
                    "        BuildSpec: buildspec-b.yml\n"
                ),
                "buildspec-a.yml": self.BUILDSPEC,
                "buildspec-b.yml": self.BUILDSPEC.replace(
                    "src/ocr_function", "src/classification_function"
                ),
            },
            dirs=("src/ocr_function", "src/classification_function"),
        )
        assert packaged_source_dirs(tmp_path) == frozenset(
            {"src/ocr_function", "src/classification_function"}
        )

    def test_a_buildspec_that_cannot_be_read_is_unknown(self, tmp_path, capsys):
        """Present and tracked but unreadable — a third way of not knowing."""
        self._repo(
            tmp_path,
            {
                "template.yaml": self.TEMPLATE.format(spec="buildspec.yml"),
                "buildspec.yml": self.BUILDSPEC,
            },
            dirs=("src/ocr_function",),
        )
        spec = Path(tmp_path, "buildspec.yml")
        spec.chmod(0o000)
        try:
            if spec.read_bytes() is not None:  # pragma: no cover - running as root
                pytest.skip(
                    "this process can read a 0o000 file, so OSError is unreachable"
                )
        except OSError:
            pass
        assert packaged_source_dirs(tmp_path) is None
        assert "could not read" in capsys.readouterr().out
        spec.chmod(0o644)

    def test_the_two_candidate_paths_are_deduplicated_and_never_none(self):
        """A path that escapes the repository root yields nothing, not a ``None`` entry.

        Both halves are structural rather than hypothetical: the two candidates are
        identical whenever the base is the repository root, and ``_repo_relative``
        answers ``None`` for a value that climbs out of the tree. Either leaking into
        the list makes the caller index a path built from ``None``.
        """
        assert ci_paths._resolve_candidates("buildspec.yml", "") == ["buildspec.yml"]
        assert ci_paths._resolve_candidates("buildspec.yml", "ext") == [
            "buildspec.yml",
            "ext/buildspec.yml",
        ]
        assert ci_paths._resolve_candidates("../../escape.yml", "") == []
        assert None not in ci_paths._resolve_candidates("../x.yml", "ext")
