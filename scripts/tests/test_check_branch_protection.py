# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for scripts/sdlc/check_branch_protection.py.

The script under test answers "do this repo's CI gates actually block a merge?"
by deriving the expected required-check list from ``.github/workflows/*.yml`` and
comparing it against the live GitHub API. These tests cover both halves —
parsing and assertion — **entirely offline**, using recorded API payloads, so
they run in CI with no token and no network.

Two behaviours are worth stating outright because getting either wrong makes the
script pass vacuously:

* ``on:`` is a YAML 1.1 boolean, so ``yaml.safe_load`` yields the key ``True``,
  not ``"on"``. Read only ``"on"`` and every workflow looks trigger-less, the
  expected list comes out empty, and nothing is ever reported as missing.
* a path-filtered ``pull_request`` trigger must NOT become a required check. Such
  a workflow does not run on a PR touching no matching path, so the check is
  never reported and a required one would sit pending forever, blocking every
  merge. This repo has two of those (``build-docs.yml``,
  ``generate-dep-manifest.yml``).

The recorded payloads follow the shape of
``GET /repos/{owner}/{repo}/branches/{branch}/protection``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from textwrap import dedent
from typing import Any, Dict

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "sdlc" / "check_branch_protection.py"


def _load_script():
    """Load the checker by path (scripts/sdlc isn't on sys.path).

    Registered in ``sys.modules`` *before* execution: the module defines
    ``@dataclass`` types, and dataclasses resolves ``cls.__module__`` through
    ``sys.modules``, so an unregistered module fails to import with an obscure
    ``AttributeError: 'NoneType' object has no attribute '__dict__'``.
    """
    spec = importlib.util.spec_from_file_location("check_branch_protection", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = _load_script()

EXPECTED = ["Dependency Audit (SCA)", "Lint, Type Check, and Test"]


def _fully_protected(contexts: list[str] | None = None) -> Dict[str, Any]:
    """A recorded protection payload with everything this repo should require."""
    return {
        "required_status_checks": {
            "strict": True,
            "contexts": list(EXPECTED if contexts is None else contexts),
            "checks": [
                {"context": name, "app_id": 15368}
                for name in (EXPECTED if contexts is None else contexts)
            ],
        },
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": True,
            "require_code_owner_reviews": False,
            "required_approving_review_count": 1,
        },
        "enforce_admins": {"enabled": False},
        "allow_force_pushes": {"enabled": False},
        "allow_deletions": {"enabled": False},
        "required_conversation_resolution": {"enabled": True},
    }


def _write_workflow(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(dedent(body), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Deriving the expected check list from workflow YAML
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_context_is_job_name_when_set_else_job_id(tmp_path: Path) -> None:
    """GitHub names the status check after the job's `name:`, else the job id."""
    _write_workflow(
        tmp_path,
        "w.yml",
        """
        name: W
        on:
          pull_request:
            branches: ["**"]
        jobs:
          named_job:
            name: Human Readable Name
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
          unnamed_job:
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
        """,
    )
    assert mod.expected_contexts(mod.discover_check_contexts(tmp_path)) == [
        "Human Readable Name",
        "unnamed_job",
    ]


@pytest.mark.unit
def test_on_key_parsed_as_yaml_boolean_is_handled(tmp_path: Path) -> None:
    """`on:` loads as the key True; missing that empties the expected list."""
    _write_workflow(
        tmp_path,
        "w.yml",
        """
        name: W
        on:
          pull_request:
        jobs:
          j:
            name: J
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
        """,
    )
    contexts = mod.discover_check_contexts(tmp_path)
    assert mod.expected_contexts(contexts) == ["J"], (
        "a bare `pull_request:` trigger must still yield a required-eligible "
        "context — if this is empty, the True-vs-'on' key handling regressed and "
        "the script would report a pass against no expectation at all"
    )


@pytest.mark.unit
def test_quoted_on_key_is_also_handled(tmp_path: Path) -> None:
    """`"on":` loads as the string key; both spellings must work."""
    _write_workflow(
        tmp_path,
        "w.yml",
        """
        name: W
        "on":
          pull_request:
            branches: ["**"]
        jobs:
          j:
            name: J
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
        """,
    )
    assert mod.expected_contexts(mod.discover_check_contexts(tmp_path)) == ["J"]


@pytest.mark.unit
@pytest.mark.parametrize("filter_key", ["paths", "paths-ignore"])
def test_path_filtered_pull_request_is_not_required_eligible(
    tmp_path: Path, filter_key: str
) -> None:
    """A required check that never reports blocks every merge forever."""
    _write_workflow(
        tmp_path,
        "w.yml",
        f"""
        name: W
        on:
          pull_request:
            {filter_key}:
              - 'docs/**'
        jobs:
          j:
            name: J
            runs-on: ubuntu-latest
            steps: [{{run: "true"}}]
        """,
    )
    contexts = mod.discover_check_contexts(tmp_path)
    assert mod.expected_contexts(contexts) == []
    assert "pending forever" in contexts[0].reason


@pytest.mark.unit
def test_non_pull_request_workflow_is_not_required_eligible(tmp_path: Path) -> None:
    """A push/schedule-only job never reports on a PR, so it cannot be required."""
    _write_workflow(
        tmp_path,
        "w.yml",
        """
        name: W
        on:
          push:
            branches: [main]
        jobs:
          j:
            name: J
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
        """,
    )
    contexts = mod.discover_check_contexts(tmp_path)
    assert mod.expected_contexts(contexts) == []
    assert contexts[0].reason == "not triggered by pull_request"


@pytest.mark.unit
def test_matrix_job_is_excluded_because_context_is_suffixed(tmp_path: Path) -> None:
    """GitHub appends the matrix leg to the context, so it isn't static."""
    _write_workflow(
        tmp_path,
        "w.yml",
        """
        name: W
        on:
          pull_request:
            branches: ["**"]
        jobs:
          j:
            name: J
            runs-on: ubuntu-latest
            strategy:
              matrix:
                python: ["3.12", "3.13"]
            steps: [{run: "true"}]
        """,
    )
    contexts = mod.discover_check_contexts(tmp_path)
    assert mod.expected_contexts(contexts) == []
    assert "matrix" in contexts[0].reason


@pytest.mark.unit
def test_gate_attribution_ignores_prose_that_looks_like_a_target(
    tmp_path: Path,
) -> None:
    """`apt-get install make curl` must not be reported as a gate `make curl`."""
    _write_workflow(
        tmp_path,
        "w.yml",
        """
        name: W
        on:
          pull_request:
            branches: ["**"]
        jobs:
          j:
            name: J
            runs-on: ubuntu-latest
            steps:
              - run: apt-get install make curl -y
              - run: make lint-cicd
        """,
    )
    gates = mod.discover_check_contexts(tmp_path)[0].gates
    assert "make lint-cicd" in gates, "a real Makefile target must be attributed"
    assert "make curl" not in gates


@pytest.mark.unit
def test_invalid_yaml_raises_rather_than_silently_reporting_no_jobs(
    tmp_path: Path,
) -> None:
    _write_workflow(tmp_path, "bad.yml", "name: W\n  bad: [indent\n")
    with pytest.raises(ValueError, match="invalid YAML"):
        mod.discover_check_contexts(tmp_path)


@pytest.mark.unit
def test_missing_workflows_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        mod.discover_check_contexts(tmp_path / "nope")


# --------------------------------------------------------------------------- #
# The real workflows in this repo (offline: reads files, no network)
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_this_repos_workflows_yield_a_non_empty_expected_list() -> None:
    """Guards the vacuous-pass failure mode against the real workflow files."""
    contexts = mod.discover_check_contexts(mod.WORKFLOWS_DIR)
    expected = mod.expected_contexts(contexts)
    assert expected, (
        "no required-eligible check context derived from .github/workflows/ — "
        "the script would then have nothing to assert and would report a pass"
    )
    # The gates job must be in the list: it carries lint, typecheck and the unit
    # suites. Asserted via its gate commands, not its display name, so renaming
    # the job does not silently empty this test.
    lint_jobs = [c for c in contexts if "make lint-cicd" in c.gates]
    assert lint_jobs, "no workflow job runs `make lint-cicd`"
    assert all(c.required_eligible for c in lint_jobs), (
        "the job running `make lint-cicd` is not required-eligible; if its "
        "pull_request trigger gained a paths: filter, that gate now silently "
        "skips on PRs that touch no matching path"
    )


@pytest.mark.unit
def test_path_filtered_repo_workflows_are_reported_as_advisory_only() -> None:
    """build-docs and generate-dep-manifest are path-filtered on purpose."""
    contexts = mod.discover_check_contexts(mod.WORKFLOWS_DIR)
    advisory = {c.workflow for c in contexts if not c.required_eligible}
    assert {"build-docs.yml", "generate-dep-manifest.yml"} <= advisory


# --------------------------------------------------------------------------- #
# Asserting against recorded API payloads
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_unprotected_branch_is_a_finding_that_names_the_issue() -> None:
    """The 404 case: what this repo looks like today."""
    findings = mod.evaluate(None, EXPECTED, "develop", status=404)
    assert [f.key for f in findings] == ["not_protected"]
    assert "#933" in findings[0].remedy
    for name in EXPECTED:
        assert name in findings[0].message, (
            "the finding must name the checks that should be required, so the "
            "reader can act on it without re-deriving the list"
        )


@pytest.mark.unit
def test_fully_configured_protection_has_no_findings() -> None:
    assert mod.evaluate(_fully_protected(), EXPECTED, "develop") == []


@pytest.mark.unit
def test_missing_required_check_is_reported_by_name() -> None:
    """The drift case: a job renamed, or a new gate never added to protection."""
    payload = _fully_protected(contexts=["Lint, Type Check, and Test"])
    findings = mod.evaluate(payload, EXPECTED, "develop")
    keys = [f.key for f in findings]
    assert keys == ["missing_required_checks"]
    assert "Dependency Audit (SCA)" in findings[0].message


@pytest.mark.unit
def test_required_check_no_workflow_produces_is_reported() -> None:
    """A stale required name never reports, so it blocks merges indefinitely."""
    payload = _fully_protected(contexts=EXPECTED + ["Renamed Away"])
    findings = mod.evaluate(payload, EXPECTED, "develop")
    assert [f.key for f in findings] == ["unknown_required_checks"]
    assert "Renamed Away" in findings[0].message


@pytest.mark.unit
def test_checks_read_from_either_response_shape() -> None:
    """The API returns both `checks:` and the deprecated flat `contexts:`."""
    checks_only = _fully_protected()
    checks_only["required_status_checks"]["contexts"] = []
    assert mod.evaluate(checks_only, EXPECTED, "develop") == []

    contexts_only = _fully_protected()
    contexts_only["required_status_checks"]["checks"] = []
    assert mod.evaluate(contexts_only, EXPECTED, "develop") == []


@pytest.mark.unit
def test_protection_with_no_required_checks_is_a_finding() -> None:
    """ "Protected" without required checks still does not gate a merge."""
    payload = _fully_protected()
    del payload["required_status_checks"]
    findings = mod.evaluate(payload, EXPECTED, "develop")
    assert "no_required_status_checks" in [f.key for f in findings]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mutate", "expected_key"),
    [
        (
            lambda p: p["required_status_checks"].__setitem__("strict", False),
            "not_strict",
        ),
        (
            lambda p: p["required_pull_request_reviews"].__setitem__(
                "dismiss_stale_reviews", False
            ),
            "stale_reviews_kept",
        ),
        (
            lambda p: p["required_pull_request_reviews"].__setitem__(
                "required_approving_review_count", 0
            ),
            "no_approving_review",
        ),
        (
            lambda p: p.__setitem__("required_pull_request_reviews", None),
            "no_pull_request_reviews",
        ),
        (
            lambda p: p["allow_force_pushes"].__setitem__("enabled", True),
            "force_pushes_allowed",
        ),
        (
            lambda p: p["allow_deletions"].__setitem__("enabled", True),
            "deletions_allowed",
        ),
    ],
)
def test_each_weakened_setting_is_reported(mutate, expected_key: str) -> None:
    payload = _fully_protected()
    mutate(payload)
    assert expected_key in [f.key for f in mod.evaluate(payload, EXPECTED, "develop")]


# --------------------------------------------------------------------------- #
# Input validation and the offline/no-token contract
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize("repo", ["not-a-slug", "owner/name/extra", "owner/na me"])
def test_invalid_repo_slug_is_rejected_before_any_request(repo: str) -> None:
    """The slug is interpolated into an API URL, so it is validated first."""
    with pytest.raises(ValueError, match="invalid repo slug"):
        mod.fetch_protection(repo, "develop", "unused-token")  # noqa: S106


@pytest.mark.unit
def test_invalid_branch_name_is_rejected_before_any_request() -> None:
    with pytest.raises(ValueError, match="invalid branch name"):
        mod.fetch_protection(mod.DEFAULT_REPO, "bad branch", "unused-token")  # noqa: S106


@pytest.mark.unit
def test_no_token_skips_cleanly_and_fail_on_skip_makes_it_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Offline default is exit 0; --fail-on-skip is the future blocking mode."""
    monkeypatch.setattr(mod, "resolve_token", lambda: None)

    assert mod.main([]) == 0
    out = capsys.readouterr().out
    assert "SKIPPED" in out and "no GitHub token" in out

    assert mod.main(["--fail-on-skip"]) == 2


@pytest.mark.unit
def test_unreachable_api_skips_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No network must not be reported as a protection failure."""
    monkeypatch.setattr(mod, "resolve_token", lambda: "t")  # noqa: S106

    def _boom(*_args: object, **_kwargs: object):
        raise mod.NetworkUnavailable("Name or service not known")

    monkeypatch.setattr(mod, "fetch_protection", _boom)
    assert mod.main([]) == 0
    assert "unreachable" in capsys.readouterr().out
    assert mod.main(["--fail-on-skip"]) == 2


@pytest.mark.unit
def test_main_exit_codes_and_json_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """1 on findings, 0 when protection is correct; --json stays machine-readable."""
    monkeypatch.setattr(mod, "resolve_token", lambda: "t")  # noqa: S106
    real_expected = mod.expected_contexts(
        mod.discover_check_contexts(mod.WORKFLOWS_DIR)
    )

    monkeypatch.setattr(mod, "fetch_protection", lambda *_a, **_k: (None, 404))
    assert mod.main(["--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["protected"] is False
    assert report["issue"] == 933
    assert report["expected_required_checks"] == real_expected

    monkeypatch.setattr(
        mod,
        "fetch_protection",
        lambda *_a, **_k: (_fully_protected(contexts=real_expected), 200),
    )
    assert mod.main([]) == 0
    assert "✅" in capsys.readouterr().out


@pytest.mark.unit
def test_empty_expectation_refuses_to_report_a_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty derived list means the parser broke, not that all is well."""
    monkeypatch.setattr(mod, "discover_check_contexts", lambda _dir: [])
    assert mod.main([]) == 3
    assert "refusing to report a pass" in capsys.readouterr().out


def _recipe(makefile: str, target: str) -> str:
    """The prerequisites plus recipe body of one make target.

    Sliced precisely — recipe lines are the TAB-indented ones directly after the
    target line. Slicing to the next ``##@`` section header (as
    ``test_ci_gate_parity.py`` does) would swallow every following target in the
    section, so an assertion about ``lint-cicd`` would match text belonging to a
    neighbouring target instead.
    """
    lines = makefile.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{target}:"))
    body = [lines[start]]
    for line in lines[start + 1 :]:
        if line.startswith("\t") or not line.strip():
            body.append(line)
        else:
            break
    return "\n".join(body)


@pytest.mark.unit
def test_the_script_is_wired_into_the_makefile_but_not_into_lint() -> None:
    """It must be runnable, and must NOT be a blocking gate until #933 closes.

    Both halves matter. Without the target nobody can run it; inside ``lint-cicd``
    it would fail every branch for a condition no contributor can fix.
    """
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "\ncheck-branch-protection:" in makefile
    assert "scripts/sdlc/check_branch_protection.py" in makefile

    for target in ("lint", "fastlint"):
        assert "check-branch-protection" not in _recipe(makefile, target), (
            f"check-branch-protection must not be a prerequisite of `{target}`"
        )

    assert "check-branch-protection" not in _recipe(makefile, "lint-cicd"), (
        "check-branch-protection needs network + a token and reports 'not "
        "protected' until issue #933 is closed; in lint-cicd it would red-line "
        "every branch. Re-enable it there only once #933 is closed."
    )

    parity = (REPO_ROOT / "scripts" / "tests" / "test_ci_gate_parity.py").read_text(
        encoding="utf-8"
    )
    assert "check-branch-protection" not in parity, (
        "adding this to SHARED_GATES would require it in both CIs, which is "
        "exactly what it must not be yet"
    )
