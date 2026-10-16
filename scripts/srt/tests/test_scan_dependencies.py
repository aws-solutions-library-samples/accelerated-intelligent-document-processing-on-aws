"""The SRT gate refuses to run without the parser its classification needs.

`scripts/srt/ci_paths.py` reads a CodeBuild buildspec to learn which directories a
deployment artifact is built from, and that read needs PyYAML. Neither CI job
installed it, so on every run the parser was absent, the "cannot establish the
packaged set" branch was taken, and the demotion that branch disables is what keeps
Bandit's identifier-name findings (B105/B106) in test files out of the gate. The
result was a findings table and exit 1 on every pull request for a day — for a
missing `pip install`, not for anything in the tree (#1428).

What makes that expensive is not the red mark but its shape: a setup fault and a
real security regression produced the *same* output, so the next genuine finding
would have arrived into a job that had been failing for hours. These tests pin the
three properties that keep them distinguishable:

1. with the parser absent, no classification call returns findings it would
   otherwise have demoted — it raises instead, so there is no path on which a
   degraded answer can be read as a result;
2. the refusal names the distribution and the command that installs it, because
   both states are a non-zero exit and only the message separates them;
3. with the parser **present**, a buildspec that genuinely does not parse still
   fails closed and still gates every finding. That policy is correct and the fix
   for (1) must not weaken it.

Every case drives the real absence, either by hiding `yaml` from the import system
or by the monkeypatch the module's own seam exposes, because PyYAML is installed on
a developer machine and on the host that found this — a green local run proves
nothing about any of it.
"""

import importlib
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

SRT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SRT_DIR.parent.parent
sys.path.insert(0, str(SRT_DIR))

import ci_paths  # noqa: E402
import run as srt_run  # noqa: E402

pytestmark = pytest.mark.unit


TEMPLATE = (
    "AWSTemplateFormatVersion: '2010-09-09'\n"
    "Resources:\n"
    "  DockerBuildProject:\n"
    "    Type: AWS::CodeBuild::Project\n"
    "    Properties:\n"
    "      Source:\n"
    "        Type: S3\n"
    "        BuildSpec: buildspec.yml\n"
)

BUILDSPEC = (
    "version: 0.2\n"
    "phases:\n"
    "  build:\n"
    "    commands:\n"
    '      - export FUNCTION_ocr="src/ocr_function"\n'
)

#: A buildspec that is not a YAML document. Distinct from a missing parser in every
#: way that matters: the environment is fine and the repository is not.
UNPARSEABLE_BUILDSPEC = "phases:\n  build:\n   commands:\n  - [unclosed\n"


def _repo(root, buildspec):
    """A throwaway git repository with one image build and one test file."""
    (root / "template.yaml").write_text(TEMPLATE, encoding="utf-8")
    (root / "buildspec.yml").write_text(buildspec, encoding="utf-8")
    handler = root / "src" / "ocr_function"
    handler.mkdir(parents=True)
    (handler / "index.py").write_text("x = 1\n", encoding="utf-8")
    tests = root / "tests" / "unit"
    tests.mkdir(parents=True)
    (tests / "test_thing.py").write_text(
        'TOKEN = "not-a-real-token"\n', encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True, capture_output=True)
    return root


#: A B105 finding on a test-shaped path outside every packaged tree — i.e. exactly
#: what the demotion exists to classify, and what the ten findings in #1428 were.
FINDING_IN_TEST_CODE = {
    "check_id": "B105",
    "path": "tests/unit/test_thing.py",
    "priority": "HIGH",
    "status": "open",
}


class _BlockYaml:
    """An import hook that makes `import yaml` fail, however PyYAML is installed.

    The module exposes a `_yaml_module` seam that a test can monkeypatch, and most
    cases below use it. At least one has to drive the real import instead: patching
    the seam asserts what every caller of it does, and nothing about whether the
    seam itself still answers None rather than raising. An `ImportError` escaping
    `_yaml_module` would reach a user as a traceback with `yaml` in it — not the
    string anybody types into `pip install`.
    """

    def find_module(self, name, path=None):  # pragma: no cover - legacy protocol
        return self if name == "yaml" else None

    def find_spec(self, name, path=None, target=None):
        if name == "yaml" or name.startswith("yaml."):
            raise ImportError("hidden by test_scan_dependencies")
        return None


@pytest.fixture
def no_yaml(monkeypatch):
    """PyYAML genuinely unimportable for the duration of one test."""
    monkeypatch.delitem(sys.modules, "yaml", raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockYaml(), *sys.meta_path])
    importlib.invalidate_caches()
    assert ci_paths._yaml_module() is None, (  # noqa: SLF001 - the seam under test
        "the import hook did not hide PyYAML, so every assertion below would be "
        "measuring the installed parser"
    )
    yield
    importlib.invalidate_caches()


# ---------------------------------------------------------------------------
# 1. A missing parser cannot come back as findings.
# ---------------------------------------------------------------------------


def test_a_missing_parser_raises_instead_of_answering_cannot_tell(no_yaml, tmp_path):
    """The seam itself, driven through the real import system.

    ``packaged_source_dirs`` returning None is a documented answer meaning "every
    finding keeps gating", and that is what a missing parser used to produce. The
    answer is now a raise, so the two cannot be confused by a caller or by a reader
    of the output.
    """
    _repo(tmp_path, BUILDSPEC)
    with pytest.raises(ci_paths.ScanDependencyError):
        ci_paths.packaged_source_dirs(tmp_path)


def test_a_missing_parser_does_not_report_the_findings_it_would_have_demoted(
    monkeypatch, tmp_path
):
    """The mechanism, stated as the difference between the two environments.

    With the parser present the finding is demoted and nothing gates. With it
    absent the call does not return a *larger gating list* — it does not return at
    all. That is the property to pin rather than a count: the number of findings
    this shielded was ten on the day and moves with the tree, while "a degraded
    scan cannot produce a findings list" holds whatever the tree holds.
    """
    _repo(tmp_path, BUILDSPEC)

    gating, demoted = ci_paths.partition_by_name_heuristic_scope(
        [FINDING_IN_TEST_CODE], tmp_path
    )
    assert (gating, demoted) == ([], [FINDING_IN_TEST_CODE]), (
        "with PyYAML installed this finding must be demoted, or the comparison "
        "below is not measuring the parser"
    )

    monkeypatch.setattr(ci_paths, "_yaml_module", lambda: None)
    with pytest.raises(ci_paths.ScanDependencyError):
        ci_paths.partition_by_name_heuristic_scope([FINDING_IN_TEST_CODE], tmp_path)


def test_against_this_repository_the_demoted_set_is_not_silently_regated(monkeypatch):
    """The same property on the real tree, with the shielded set derived from it.

    The findings #1428 reported were B105/B106 on test files in `lib/`. Which files
    those are, and how many, is a function of the tree on the day — so the set is
    read from `git ls-files` through the gate's own classifier rather than written
    here, and the assertion is that the parser's absence cannot turn whatever that
    set currently is back into gating findings.
    """
    packaged = ci_paths.packaged_source_dirs(PROJECT_ROOT)
    assert packaged is not None, (
        "the packaged set cannot be established in this checkout, so this test "
        "cannot tell a demotion from a failure to classify"
    )
    tracked = ci_paths.tracked_files(PROJECT_ROOT) or set()
    issues = [
        {"check_id": "B105", "path": rel, "priority": "HIGH", "status": "open"}
        for rel in sorted(tracked)
        if rel.endswith(".py") and ci_paths.is_test_only_path(rel, packaged)
    ][:25]
    assert issues, "no demotable path in this checkout: the premise is gone"

    gating, demoted = ci_paths.partition_by_name_heuristic_scope(issues, PROJECT_ROOT)
    assert gating == [] and len(demoted) == len(issues)

    monkeypatch.setattr(ci_paths, "_yaml_module", lambda: None)
    with pytest.raises(ci_paths.ScanDependencyError):
        ci_paths.partition_by_name_heuristic_scope(issues, PROJECT_ROOT)


def test_the_scan_refuses_before_it_starts(no_yaml, capsys):
    """`run.py` exits on the setup fault rather than scanning for 15 minutes.

    Exit 1 is the same code a real finding produces, which is why the message
    below is asserted too — the exit code alone is the state this replaces.
    """
    with pytest.raises(SystemExit) as exit_info:
        srt_run.abort_on_missing_dependencies()
    assert exit_info.value.code == 1
    assert "PyYAML" in capsys.readouterr().out


def test_the_refusal_is_reached_before_the_assessment_is_launched():
    """Ordering inside `main`, which no behavioural test of the parts can see.

    A preflight placed after `srt assess` would be correct and useless: the
    classification runs on the scan's output, so the degraded answer would already
    have been printed by the time anybody read the refusal.
    """
    source = inspect.getsource(srt_run.main)
    refusal = source.find("abort_on_missing_dependencies()")
    assessment = source.find("./srt assess")
    assert refusal != -1, "main() no longer refuses on a missing dependency at all"
    assert assessment != -1, "the `srt assess` invocation moved out of main()"
    assert refusal < assessment, (
        "main() launches the assessment before checking its dependencies, so a "
        "missing parser costs a full scan before it is reported"
    )


def test_a_scan_with_the_parser_present_is_not_refused():
    """Non-vacuity: the refusal is conditional, not a scan that never runs."""
    assert ci_paths.missing_distributions() == ()
    srt_run.abort_on_missing_dependencies()


# ---------------------------------------------------------------------------
# 2. The refusal names the distribution and how to install it.
# ---------------------------------------------------------------------------


def test_the_refusal_names_the_distribution_and_the_install_command(no_yaml):
    missing = ci_paths.missing_distributions()
    assert missing == (ci_paths.YAML_DISTRIBUTION,)

    message = ci_paths.dependency_failure_message(missing)
    install = "pip install PyYAML"
    assert install in message, (
        "the message must carry the command to run. A refusal that names only the "
        "package leaves the reader to guess whether it is a pip, an apt or a "
        "Makefile target"
    )
    assert "PyYAML" in message.replace(install, ""), (
        "the message names the distribution only inside the install command, so a "
        "message built from an empty name list would still satisfy a plain "
        "`'PyYAML' in message` — measured as a surviving mutant. The sentence "
        "saying what is wrong has to name it too"
    )
    assert sys.executable in message, (
        "the interpreter has to be named: `make srt-scan` runs the one on PATH "
        "rather than SRT's managed venv, and installing into the wrong one is the "
        "obvious next mistake"
    )


def test_the_raised_error_carries_the_same_message(no_yaml, tmp_path):
    """So a caller that skips the preflight still reports something actionable."""
    _repo(tmp_path, BUILDSPEC)
    with pytest.raises(ci_paths.ScanDependencyError) as error:
        ci_paths.packaged_source_dirs(tmp_path)
    assert "pip install PyYAML" in str(error.value)


def test_every_required_distribution_has_an_install_command():
    """A name added to the tuple without one would refuse without saying how."""
    for name in ci_paths.REQUIRED_DISTRIBUTIONS:
        assert name in ci_paths._INSTALL_COMMANDS  # noqa: SLF001 - the table itself
        assert name in ci_paths.dependency_failure_message((name,))


# ---------------------------------------------------------------------------
# 3. Fail-closed on an unparseable buildspec is unchanged.
# ---------------------------------------------------------------------------


def test_an_unparseable_buildspec_still_fails_closed_with_the_parser_present(
    tmp_path, capsys
):
    """The policy the fix above must not weaken.

    A buildspec that does not parse really does mean the directories its images
    ship are unknown, and the honest answer is to gate everything. Only the
    *reason* "the parser is absent" has been removed from that branch.
    """
    _repo(tmp_path, UNPARSEABLE_BUILDSPEC)
    assert ci_paths.packaged_source_dirs(tmp_path) is None
    assert "could not parse" in capsys.readouterr().out


def test_an_unparseable_buildspec_keeps_a_demotable_finding_gating(tmp_path):
    """End to end: the same finding demoted above gates when the tree is unreadable.

    This is the half that a fix for the missing-parser case could plausibly break —
    by treating "no packaged set" as "nothing ships" — and it is the direction that
    would hide a credential in a file an artifact carries.
    """
    _repo(tmp_path, UNPARSEABLE_BUILDSPEC)
    gating, demoted = ci_paths.partition_by_name_heuristic_scope(
        [FINDING_IN_TEST_CODE], tmp_path
    )
    assert (gating, demoted) == ([FINDING_IN_TEST_CODE], [])


def test_the_unparseable_branch_is_not_the_missing_parser_branch(
    tmp_path_factory, monkeypatch
):
    """The two states are answered differently, which is the fix in one assertion.

    One well-formed tree and one malformed tree, asked under the same conditions:
    the malformed one answers None and keeps gating, and the well-formed one
    answers a set — until the parser goes away, at which point it refuses. A single
    branch serving both reasons is what collapsed them.
    """
    broken = _repo(tmp_path_factory.mktemp("broken"), UNPARSEABLE_BUILDSPEC)
    fine = _repo(tmp_path_factory.mktemp("fine"), BUILDSPEC)

    assert ci_paths.packaged_source_dirs(broken) is None
    assert ci_paths.packaged_source_dirs(fine) == frozenset({"src/ocr_function"})

    monkeypatch.setattr(ci_paths, "_yaml_module", lambda: None)
    with pytest.raises(ci_paths.ScanDependencyError):
        ci_paths.packaged_source_dirs(fine)
    with pytest.raises(ci_paths.ScanDependencyError):
        ci_paths.packaged_source_dirs(broken)
