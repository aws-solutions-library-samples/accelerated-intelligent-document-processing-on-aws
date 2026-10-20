# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The shape of the two advisory MR UX test jobs, ``ux_test_build`` and ``ai_ux_test``.

Each property here is one a later edit to ``.gitlab-ci.yml`` could drop without any
job going red, because both jobs are ``allow_failure: true``: a broken job is a quiet
one. Four of them are security properties rather than tidiness.

* **The harness comes from the target branch, and the job refuses otherwise.** An MR
  pipeline checks out the MR, so a harness run from the checkout would make the tool
  lists, the egress allowlist and the prompt the MR's own, beside an AWS credential
  and ``GITLAB_REVIEW_TOKEN``. So the job extracts :data:`ai_ux_test.HARNESS_PATHS`
  with ``git archive`` into a fresh directory, exits 1 when it cannot, and runs
  nothing by a checkout-relative path.
* **No MR code runs in the browser job.** The MR's ``npm ci`` and ``vite build`` run
  in ``ux_test_build``; ``ai_ux_test`` receives static files.
* **No CI configuration passes the developer's video opt-in.** It renders another
  stack's unredacted screens into the artifact directory, and a pipeline's
  artifacts can be downloaded. Where it arrives some other way, the orchestrator
  refuses it under CI, and the Docker runner carries CI's own variables into its
  container so that the refusal holds there as well.
* **It is advisory.** Neither job is a shared gate, and neither Makefile target looks
  like one to ``test_ci_gate_parity.py``.

The rest pins the wiring the two jobs depend on: identical rules, so ``needs`` can
never name a job absent from the pipeline; the stage order; the artifacts; and the
``.gitignore``, ``CLAUDE.md`` and dependency-manifest entries the job's output and
toolchain need.
"""

from __future__ import annotations

import ast
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
GITLAB_CI = REPO_ROOT / ".gitlab-ci.yml"
ORCHESTRATOR = REPO_ROOT / "scripts" / "sdlc" / "ai_ux_test.py"
NIGHTLY = REPO_ROOT / "scripts" / "sdlc" / "codebuild_deployment.py"
RUN_IN_DOCKER = REPO_ROOT / "scripts" / "ux_runner" / "run_in_docker.py"
GITHUB_WORKFLOWS = REPO_ROOT / ".github" / "workflows"
DEP_MANIFEST = REPO_ROOT / "scripts" / "generate-dep-manifest.sh"
UX_LOCKFILE = "scripts/ux_runner/package-lock.json"
BUILD_JOB = "ux_test_build"
TEST_JOB = "ai_ux_test"
UX_JOBS = (BUILD_JOB, TEST_JOB)


@pytest.fixture(scope="module")
def ci_text() -> str:
    return GITLAB_CI.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ci_config(ci_text: str) -> dict[str, Any]:
    return yaml.safe_load(ci_text)


@pytest.fixture(scope="module")
def build_job(ci_config: dict[str, Any]) -> dict[str, Any]:
    assert BUILD_JOB in ci_config, f"{BUILD_JOB} is gone from .gitlab-ci.yml"
    return ci_config[BUILD_JOB]


@pytest.fixture(scope="module")
def ux_job(ci_config: dict[str, Any]) -> dict[str, Any]:
    assert TEST_JOB in ci_config, f"{TEST_JOB} is gone from .gitlab-ci.yml"
    return ci_config[TEST_JOB]


def _commands(job: dict[str, Any]) -> list[str]:
    return [str(line) for line in [*job.get("before_script", []), *job["script"]]]


def _module_constant(path: Path, name: str) -> Any:
    """The literal value of a module-level assignment, read without importing.

    No module is imported: ``codebuild_deployment`` is the CodeBuild harness, and
    ``ai_ux_test`` pulls in ``ai_mr_review``, which another suite re-executes under
    its own module name.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return ast.literal_eval(value)
    raise AssertionError(f"{path.relative_to(REPO_ROOT)} assigns no {name}")


def _triggering(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [rule for rule in rules if rule.get("when") not in (None, "never")]


@pytest.mark.unit
def test_both_jobs_carry_the_same_rules(build_job, ux_job) -> None:
    """``needs: [ux_test_build]`` breaks pipeline creation if only one job is added.

    A rule set that admitted ``ai_ux_test`` on an MR where ``ux_test_build`` is
    absent would make every such MR's pipeline fail to be created at all, which is
    why the second job reuses the first one's rules through a YAML anchor.
    """
    assert ux_job["rules"] == build_job["rules"]


@pytest.mark.unit
def test_the_one_triggering_rule_is_non_draft_mrs_to_develop_that_change_the_ui(
    build_job,
) -> None:
    rules = build_job["rules"]
    triggering = _triggering(rules)
    assert len(triggering) == 1, f"expected one triggering rule, found {triggering}"
    rule = triggering[0]
    assert rule["when"] == "on_success"
    assert '$CI_MERGE_REQUEST_TARGET_BRANCH_NAME == "develop"' in rule["if"]
    assert "!~ /^Draft:/" in rule["if"], (
        "Drafts are no longer excluded, so every push during the WIP phase pays "
        "for a UX session"
    )
    assert rule["changes"]["paths"] == ["src/ui/**/*"]
    assert rule["changes"]["compare_to"] == "refs/heads/develop"
    assert rules[-1] == {"when": "never"}
    assert not any("schedule" in str(r) for r in rules), (
        "a rule mentions the schedule: the UX test is per merge request, and a "
        "scheduled pipeline has no MR to test"
    )


@pytest.mark.unit
def test_the_ux_test_runs_only_where_the_code_review_it_joins_runs(
    ci_config, build_job
) -> None:
    """The section is appended to the ``ai_mr_review`` note for the same head.

    So the UX trigger must be the review's own trigger narrowed by ``changes``. A
    wider one would post standalone notes on MRs the review never reads; and the
    review must keep its own rules, without the UX job's ``changes`` filter, or
    it would stop reviewing every MR that does not touch ``src/ui``.
    """
    review_rules = ci_config["ai_mr_review"]["rules"]
    (review_trigger,) = _triggering(review_rules)
    (ux_trigger,) = _triggering(build_job["rules"])
    assert ux_trigger["if"] == review_trigger["if"]
    assert not any("changes" in rule for rule in review_rules), (
        "ai_mr_review gained a `changes:` filter; the UX rules belong to the UX jobs"
    )
    assert ci_config["ai_mr_review"]["stage"] == "fast_checks"
    assert ci_config["ai_mr_review"]["needs"] == []


@pytest.mark.unit
@pytest.mark.parametrize("name", UX_JOBS)
def test_each_job_is_advisory_interruptible_and_in_the_ux_review_stage(
    ci_config, name: str
) -> None:
    job = ci_config[name]
    assert job["allow_failure"] is True, f"{name} can now fail a merge request"
    assert job["interruptible"] is True, (
        f"{name} is no longer interruptible, so a burst of pushes pays for a full "
        "run per push"
    )
    assert job["tags"] == ["size:large"]
    assert job["stage"] == "ux_review"
    assert job["variables"]["GIT_DEPTH"] == 0, (
        f"{name} diffs against the merge base with develop, which a shallow "
        "clone does not have"
    )


@pytest.mark.unit
def test_the_timeouts_and_the_dependency_between_the_jobs(build_job, ux_job) -> None:
    assert build_job["timeout"] == "20m"
    assert ux_job["timeout"] == "60m"
    assert build_job["needs"] == []
    assert ux_job["needs"] == [{"job": BUILD_JOB, "artifacts": True}]
    assert ux_job["resource_group"] == "ux_test_stack", (
        "without the resource group two MRs drive the shared review stack at once"
    )


@pytest.mark.unit
def test_the_ux_test_uses_the_code_reviews_model_and_region(ci_config, ux_job) -> None:
    """The GitLab CI role grants Bedrock on one model family, which the review uses."""
    review = ci_config["ai_mr_review"]["variables"]
    assert ux_job["variables"]["ANTHROPIC_MODEL"] == review["ANTHROPIC_MODEL"]
    assert ux_job["variables"]["AWS_REGION"] == review["AWS_REGION"]


#: The harness extraction as the job must spell it: the target branch's tree, the
#: exact path list, a fresh directory, and the tree made writable by root alone,
#: all inside the condition whose failure refuses the run.
_EXTRACTION_RE = re.compile(
    r"^[ \t]*if git archive --format=tar -o (?P<tar>\S+) FETCH_HEAD -- \$UX_HARNESS "
    r'&& tar -xf (?P=tar) -C "\$UX_HARNESS_DIR" '
    r'&& chmod -R go-w "\$UX_HARNESS_DIR"; then\n'
    r"(?P<then>(?:.*\n)*?)[ \t]*else\n(?P<refuse>(?:.*\n)*?)[ \t]*fi$",
    re.MULTILINE,
)


@pytest.mark.unit
def test_the_harness_is_extracted_from_the_target_branch_and_fails_closed(
    ux_job,
) -> None:
    before = "\n".join(str(line) for line in ux_job["before_script"])
    assert 'TARGET="${CI_MERGE_REQUEST_TARGET_BRANCH_NAME:-develop}"' in before
    (extraction,) = list(_EXTRACTION_RE.finditer(before))
    refuse = [line.strip() for line in extraction["refuse"].splitlines()]
    assert refuse and refuse[-1] == "exit 1", (
        "a failed extraction no longer exits 1, so the job may run the MR's own "
        f"copy of the harness instead of refusing: {refuse}"
    )
    assert all(not line.startswith("exit") for line in refuse[:-1]), refuse
    assert "exit" not in extraction["then"], extraction["then"]
    assert "git checkout FETCH_HEAD" not in before, (
        "an in-place checkout leaves MR-added files beside the pinned harness, "
        "and Python imports from the script's own directory first"
    )
    assert ux_job["variables"]["UX_HARNESS_DIR"] == "/tmp/ux-harness"


@pytest.mark.unit
def test_the_extracted_harness_is_exactly_the_orchestrators_harness_paths(
    ux_job,
) -> None:
    """The job's list and the module's must be one list.

    ``test_ai_ux_test.py`` closes ``HARNESS_PATHS`` over the imports of every module
    it names. That closure protects nothing unless the job extracts the same set: a
    module the job leaves out is imported from wherever Python finds it.
    """
    before = "\n".join(str(line) for line in ux_job["before_script"])
    match = re.search(r'UX_HARNESS="([^"]*)"', before)
    assert match, "ai_ux_test no longer declares its UX_HARNESS list"
    harness = tuple(match.group(1).split())
    assert harness == _module_constant(ORCHESTRATOR, "HARNESS_PATHS")


#: Interpreters: the first word after one that is not an option is what it runs.
_LAUNCHERS = frozenset({"python3", "python", "bash", "sh"})
#: Words that run code from somewhere the job does not pin, wherever they appear.
_FOREIGN_RUNNERS = frozenset({"make", "source", "."})
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _words(command: str) -> list[str]:
    """``command`` split as the shell would, its operators as words of their own."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _checkout_runs(command: str) -> list[str]:
    """Every word of ``command`` that runs something by a path not under the harness.

    A word naming a ``.py`` or ``.sh`` file, and the first word after an
    interpreter that is not one of its options, must lie under
    ``$UX_HARNESS_DIR``; ``make``, ``source`` and ``.`` are refused outright, since
    each runs files the job does not pin.
    """
    words = _words(command)
    found: list[str] = []
    for index, word in enumerate(words):
        if word in _FOREIGN_RUNNERS:
            found.append(word)
            continue
        if _ASSIGNMENT_RE.match(word):
            continue
        if word.endswith((".py", ".sh")) and not word.startswith("$UX_HARNESS_DIR/"):
            found.append(word)
        if word in _LAUNCHERS:
            rest = [w for w in words[index + 1 :] if not w.startswith("-")]
            if rest and not rest[0].startswith("$UX_HARNESS_DIR/"):
                found.append(f"{word} {rest[0]}")
    return found


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "python3 -I -B scripts/sdlc/ai_ux_test.py --mr 1",
        "sh scripts/ux_runner/install.sh",
        ". scripts/x.sh",
        "make ai-ux-test-local",
        "source scripts/x.sh",
        "true && python3 -B ./scripts/sdlc/ai_ux_test.py",
    ],
)
def test_the_checkout_scan_catches_every_way_of_running_the_mrs_copy(
    command: str,
) -> None:
    assert _checkout_runs(command), command


@pytest.mark.unit
def test_nothing_in_the_ux_test_job_runs_the_checkouts_own_copy(ux_job) -> None:
    commands = _commands(ux_job)
    offending = {c: found for c in commands if (found := _checkout_runs(c))}
    assert not offending, f"these run something the harness does not pin: {offending}"
    executed = [
        word
        for command in commands
        for word in _words(command)
        if word.startswith("$UX_HARNESS_DIR/") and word.endswith((".py", ".sh"))
    ]
    assert len(executed) == 3, (
        f"ai_ux_test should run the precheck, the install and the full run from "
        f"the harness, and runs {executed}"
    )


@pytest.mark.unit
def test_the_job_sets_its_umask_before_it_makes_anything(ux_job) -> None:
    """GitLab's Docker executor runs a job at umask 0000, under which everything
    below would be writable by the browser's uid; the shell is shared by
    before_script and script, so one line at the top covers both."""
    assert ux_job["before_script"][0] == "umask 022"


@pytest.mark.unit
def test_both_orchestrator_runs_write_no_bytecode_into_the_pinned_harness(
    ux_job,
) -> None:
    """``-B`` stops writing, not reading: with it no ``__pycache__`` is made in the
    harness, and the harness is root's alone, so none can be planted there."""
    runs = [
        (words, at)
        for command in _commands(ux_job)
        for words in [_words(command)]
        for at, word in enumerate(words)
        if word.endswith("ai_ux_test.py")
    ]
    assert len(runs) == 2, runs
    for words, at in runs:
        assert words[at - 2 : at] == ["python3", "-B"], words[max(0, at - 3) : at + 1]
        assert words[at] == "$UX_HARNESS_DIR/scripts/sdlc/ai_ux_test.py"


@pytest.mark.unit
def test_the_checkout_and_the_harness_are_made_root_only_before_any_python_runs(
    ux_job,
) -> None:
    """The runner makes the checkout at its own umask, and once Chrome runs the
    browser's uid must not rename or replace ux-tests, src/ui/build or a harness
    module that root writes into, serves or imports."""
    before = [str(line) for line in ux_job["before_script"]]
    checkout = [
        i for i, c in enumerate(before) if c == 'chmod -R go-w "$CI_PROJECT_DIR"'
    ]
    extracted = [
        i for i, c in enumerate(before) if 'chmod -R go-w "$UX_HARNESS_DIR"' in c
    ]
    fresh = [i for i, c in enumerate(before) if c.startswith("rm -rf ux-tests")]
    prechecks = [i for i, c in enumerate(before) if "--precheck" in c]
    assert checkout and extracted and fresh and prechecks
    assert extracted[0] < checkout[0] < fresh[0] < prechecks[0]


@pytest.mark.unit
def test_the_precheck_runs_before_the_toolchain_install(ux_job) -> None:
    """A no-op head should cost about a minute, not a toolchain install."""
    before = [str(line) for line in ux_job["before_script"]]
    prechecks = [i for i, c in enumerate(before) if "--precheck" in c]
    installs = [i for i, c in enumerate(before) if "install.sh" in c]
    assert prechecks and installs, "the precheck or the install step is gone"
    assert prechecks[0] < installs[0]
    assert "ux-tests/proceed" in before[installs[0]], (
        "the install no longer waits for the precheck's proceed marker"
    )


def _flattened(job: dict[str, Any]) -> list[Any]:
    """The job's before_script and script entries, a resolved anchor list unrolled."""
    entries: list[Any] = []
    for entry in [*job.get("before_script", []), *job["script"]]:
        entries.extend(entry if isinstance(entry, list) else [entry])
    return entries


#: The function the UX job runs third-party steps under, as the job defines it.
_SECRETLESS_RE = re.compile(
    r"^without_job_secrets\(\) \{\n(?P<body>.*?)\n\}$", re.MULTILINE | re.DOTALL
)


@pytest.mark.unit
def test_the_ux_job_runs_no_npm_npx_vite_or_node_setup_of_its_own(
    ci_config, ux_job
) -> None:
    """The MR's npm and vite run in ux_test_build; this job only serves the files.

    install.sh's own pinned ``npm ci``, from the target branch's lockfile, is the
    only npm in the job, so neither the job's commands nor the ``.node_setup``
    anchor may bring in another.
    """
    entries = _flattened(ux_job)
    assert not [e for e in entries if e in ci_config[".node_setup"]], (
        "ai_ux_test uses the .node_setup anchor"
    )
    text = "\n".join(str(entry) for entry in entries)
    for needle in ("npm", "npx", "vite", "node_modules/.bin", "cd src/ui"):
        assert needle not in text, f"ai_ux_test runs {needle!r} itself"


def _secretless_function(ux_job) -> tuple[int, str]:
    entries = [str(entry) for entry in _flattened(ux_job)]
    defined = [i for i, entry in enumerate(entries) if _SECRETLESS_RE.search(entry)]
    assert defined, "ai_ux_test no longer defines without_job_secrets"
    return defined[0], entries[defined[0]]


@pytest.mark.unit
def test_the_secretless_steps_get_an_environment_built_from_nothing(ux_job) -> None:
    """``env -i`` and named variables only: a denylist passes every variable nobody
    named, such as CI_REPOSITORY_URL, which carries the job token, and any
    unprotected project variable."""
    _, definition = _secretless_function(ux_job)
    body = _SECRETLESS_RE.search(definition)
    assert body is not None
    words = body.group("body").replace("\\\n", " ").split()
    assert words[:2] == ["env", "-i"] and words[-1] == '"$@"', words
    passed = {re.sub(r"^\$\{\w+:\+|=.*$", "", word) for word in words[2:-1]}
    expected = {
        "PATH",
        "HOME",
        "LANG",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "UX_RUNNER_PREFIX",
    }
    assert passed == expected, sorted(passed ^ expected)


@pytest.mark.unit
def test_the_secretless_steps_inherit_no_variable_but_the_ones_named(ux_job) -> None:
    """Run as the job runs it, under bash, from a parent holding every secret the
    job can see: what the third-party step receives is the allowlist alone."""
    bash = shutil.which("bash")
    assert bash, "bash runs the job's script and is on every Linux and macOS runner"
    _, definition = _secretless_function(ux_job)
    secrets = {
        "GITLAB_REVIEW_TOKEN": "review-value",
        "CI_JOB_TOKEN": "job-value",
        "CI_REPOSITORY_URL": "https://gitlab-ci-token:job-value@gitlab.example/p.git",
        "CI_DEPLOY_PASSWORD": "deploy-value",
        "CI_REGISTRY_PASSWORD": "registry-value",
        "SLACK_WEBHOOK_URL": "https://hooks.example/webhook-value",
        "UX_TEST_PERSONAS": "personas-value",
        "UX_TEST_UI_ENV": "ui-env-value",
        "UX_TEST_AWS_ACCOUNT_ID": "111122223333",
        "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "aws-secret-value",
        "AWS_SESSION_TOKEN": "aws-session-value",
    }
    parent = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/root",
        "https_proxy": "http://proxy.example:3128",
        **secrets,
    }
    shown = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [bash, "-c", f"{definition}\nwithout_job_secrets env"],
        env=parent,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert shown.returncode == 0, shown.stderr[-400:]
    received = dict(line.split("=", 1) for line in shown.stdout.splitlines() if line)
    assert set(received) == {"PATH", "HOME", "LANG", "https_proxy"}, sorted(received)
    for value in secrets.values():
        assert value not in shown.stdout


@pytest.mark.unit
def test_apt_and_the_toolchain_install_run_without_the_jobs_secrets(ux_job) -> None:
    """apt's maintainer scripts, pip and the npm packages' install scripts are
    third-party code that needs none of the token, the personas, the UI env or
    an AWS credential, so each step that runs them goes through the function that
    hands on only the variables it names, defined before its first use."""
    entries = [str(entry) for entry in _flattened(ux_job)]
    defined = [_secretless_function(ux_job)[0]]
    third_party = [
        i
        for i, entry in enumerate(entries)
        if "apt-get" in entry or "install.sh" in entry
    ]
    assert len(third_party) == 3, third_party
    for index in third_party:
        assert index > defined[0], f"{entries[index]!r} runs before the function exists"
        assert re.search(
            r"(?:^|&& |; )without_job_secrets (?:apt-get|bash) ", entries[index]
        ), f"{entries[index]!r} runs with the job's secrets"


@pytest.mark.unit
def test_the_toolchain_install_starts_in_the_harness_not_the_checkout(ux_job) -> None:
    """The checkout is the MR's: a module or .npmrc at its root must not be found.

    install.sh leaves its caller's directory itself (test_ux_runner_docker.py pins
    that); the job starts it inside the extracted harness as well.
    """
    (install,) = [str(e) for e in _flattened(ux_job) if "install.sh" in str(e)]
    assert re.search(
        r'\(cd "\$UX_HARNESS_DIR" && without_job_secrets bash '
        r'"\$UX_HARNESS_DIR/scripts/ux_runner/install\.sh"\)',
        install,
    ), install


@pytest.mark.unit
def test_the_artifact_directory_is_recreated_before_the_harness_writes_into_it(
    ux_job,
) -> None:
    """``mkdir -p`` would accept a ``ux-tests`` symlink the MR committed; ``rm -rf``
    removes the symlink itself, and the harness refuses one as well."""
    before = [str(line) for line in ux_job["before_script"]]
    fresh = [
        i for i, c in enumerate(before) if c == "rm -rf ux-tests && mkdir ux-tests"
    ]
    prechecks = [i for i, c in enumerate(before) if "--precheck" in c]
    assert fresh and prechecks and fresh[0] < prechecks[0]
    assert not [c for c in before if "mkdir -p ux-tests" in c]


@pytest.mark.unit
def test_the_harness_fetch_names_the_target_branch_and_not_a_tag(ux_job) -> None:
    """A bare name prefers a tag called like the branch; refs/heads/ cannot."""
    before = "\n".join(str(line) for line in ux_job["before_script"])
    fetches = re.findall(r"^\s*git fetch\b.*$", before, re.MULTILINE)
    assert fetches == ['git fetch -q origin "refs/heads/$TARGET"'], fetches


@pytest.mark.unit
def test_the_build_job_classifies_the_change_before_it_builds(build_job) -> None:
    script = "\n".join(_commands(build_job))
    assert "ui_change.py" in script
    assert "npm ci" in script
    assert script.index("ui_change.py") < script.index("npm ci")
    assert "build:only" in script
    assert "npx vitest run" not in script, (
        "the UI suite is a gate in ui_tests; running it here adds a second, "
        "advisory red mark for the same failure"
    )


@pytest.mark.unit
def test_the_artifacts_are_the_build_and_the_ux_tests_directory(
    build_job, ux_job
) -> None:
    assert build_job["artifacts"]["paths"] == ["src/ui/build/", "ui-change.json"]
    assert build_job["artifacts"]["expire_in"] == "1 day"
    assert ux_job["artifacts"]["paths"] == ["ux-tests/"]
    assert ux_job["artifacts"]["when"] == "always"
    assert ux_job["artifacts"]["expire_in"] == "1 week"
    for job in (build_job, ux_job):
        for path in job["artifacts"]["paths"]:
            for word in ("scratch", "recordings", "aws_after_script"):
                assert word not in path, (
                    f"artifact path {path!r} names {word!r}: the recorder session, "
                    "the run directory and the credentials file never leave the job"
                )


@pytest.mark.unit
def test_the_ux_review_stage_sits_between_the_deploy_and_notify_stages(
    ci_config,
) -> None:
    stages = ci_config["stages"]
    assert "ux_review" in stages
    assert (
        stages.index("integration_tests")
        < stages.index("ux_review")
        < stages.index("notify")
    )


@pytest.mark.unit
def test_the_jobs_sit_after_the_code_review_and_before_the_nightly_notification(
    ci_text: str,
) -> None:
    """Placed where no text slice of another job's tests can swallow them.

    ``test_after_script_log_capture.py`` slices ``integration_tests`` up to the next
    top-level key, so nothing may sit between it and ``srt_security_review``.
    """
    position = {
        name: ci_text.index(f"\n{name}:\n")
        for name in (
            "integration_tests",
            "srt_security_review",
            "ai_mr_review",
            BUILD_JOB,
            TEST_JOB,
            "nightly_notification",
        )
    }
    assert position["integration_tests"] < position["srt_security_review"]
    assert position["srt_security_review"] < position[BUILD_JOB]
    assert (
        position["ai_mr_review"]
        < position[BUILD_JOB]
        < position[TEST_JOB]
        < position["nightly_notification"]
    )


@pytest.mark.unit
def test_the_rules_are_shared_by_anchor_and_the_file_uses_no_reference_tags(
    ci_text: str,
) -> None:
    """Several suites ``yaml.safe_load`` this file, which rejects ``!reference``."""
    assert "rules: &ux_test_rules" in ci_text
    assert "rules: *ux_test_rules" in ci_text
    assert "!reference" not in ci_text


@pytest.mark.unit
def test_neither_job_is_a_shared_gate() -> None:
    from test_ci_gate_parity import SHARED_GATES  # type: ignore[import-not-found]

    for gate in SHARED_GATES:
        for name in (*UX_JOBS, "ai-ux-test", "ai-ux-browser"):
            assert name not in gate, (
                f"SHARED_GATES names {gate!r}, which asserts the advisory UX test "
                "runs as a gate in both CIs"
            )


@pytest.mark.unit
def test_the_makefile_targets_are_outside_every_gate_section() -> None:
    """The local entry points must not enter ``test_ci_gate_parity.py``'s universe.

    That module derives its gate universe from ``##@`` sections and check-shaped
    names, and everything in it must run in both CIs. These targets drive a live
    review stack and are advisory, so the answer is for them not to look like gates.
    """
    from test_ci_gate_parity import (  # type: ignore[import-not-found]
        GATE_SECTION_PREFIXES,
        _is_check_shaped,
        _makefile_sections,
    )

    sections = _makefile_sections()
    targets = sorted(t for t in sections if t.startswith("ai-ux-"))
    assert targets == ["ai-ux-browser", "ai-ux-test-local"], targets
    for target in targets:
        assert not sections[target].startswith(GATE_SECTION_PREFIXES), (
            f"`{target}` is declared under the gate section {sections[target]!r}"
        )
        assert not _is_check_shaped(target), (
            f"`{target}` is check-shaped, which puts it in the gate universe"
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "posts"),
    [
        ("", False),
        ("0", False),
        ("no", False),
        ("false", False),
        ("1", True),
        ("true", True),
        ("yes", True),
    ],
)
def test_ai_ux_test_local_posts_only_for_an_affirmative_post(
    value: str, posts: bool
) -> None:
    """Posting is the target's one public side effect, so ``POST=0`` must not post.

    Read from what ``make`` would run (``--dry-run`` executes nothing), because the
    question is about the expanded command rather than the recipe's spelling.
    """
    shown = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "make",
            "--no-print-directory",
            "--dry-run",
            "ai-ux-test-local",
            "MR=812",
            f"POST={value}",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert shown.returncode == 0, shown.stderr[-400:]
    assert "run_in_docker.py review --mr 812" in shown.stdout
    assert ("--post" in shown.stdout.split()) is posts


@pytest.mark.unit
def test_the_unattended_skill_is_registered_in_claude_md() -> None:
    claude_md = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert ".claude/skills/ux-test-ci.md" in claude_md


@pytest.mark.unit
def test_dep_audit_reads_the_ux_toolchain_lockfile() -> None:
    """``install.sh`` runs ``npm ci`` from this lockfile, so its tree must be audited."""
    text = DEP_MANIFEST.read_text(encoding="utf-8")
    match = re.search(r"^NODE_LOCKFILES=\((.*?)^\)", text, re.MULTILINE | re.DOTALL)
    assert match, "generate-dep-manifest.sh no longer declares NODE_LOCKFILES"
    lockfiles = re.findall(r'"([^"]+)"', match.group(1))
    assert f"$REPO_ROOT/{UX_LOCKFILE}" in lockfiles, lockfiles
    assert (REPO_ROOT / UX_LOCKFILE).is_file()


@pytest.mark.unit
def test_the_gitignore_keeps_the_jobs_output_and_the_run_options_out_of_the_tree() -> (
    None
):
    lines = {
        line.strip()
        for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    }
    for entry in ("ux-tests/", "ui-change.json", "ci_run_options.json"):
        assert entry in lines, f".gitignore does not list {entry}"


@pytest.mark.unit
def test_the_review_stack_is_the_one_the_nightly_build_updates() -> None:
    """The one stack whose video the orchestrator renders, read as AST constants.

    The Docker runner's review mode is the CI-equivalent local run, so it tests the
    same stack by default.
    """
    review_stack = _module_constant(ORCHESTRATOR, "REVIEW_STACK")
    nightly_stack = _module_constant(NIGHTLY, "UX_NIGHTLY_STACK_NAME")
    docker_default = _module_constant(RUN_IN_DOCKER, "DEFAULT_STACK")
    assert isinstance(review_stack, str)
    assert review_stack == nightly_stack == docker_default


def _declared_long_options(path: Path) -> set[str]:
    """Every literal long option the module's parsers declare, read without importing."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        arg.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
        for arg in node.args
        if isinstance(arg, ast.Constant)
        and isinstance(arg.value, str)
        and arg.value.startswith("--")
    }


def _spellings(flag: str, others: set[str]) -> set[str]:
    """Every option word argparse reads as ``flag``: the flag and each prefix of it
    that no other declared option shares, since argparse accepts an unambiguous
    abbreviation."""
    rivals = others - {flag}
    return {
        flag[:end]
        for end in range(len("--") + 1, len(flag) + 1)
        if not any(other.startswith(flag[:end]) for other in rivals)
    }


#: An option word as a shell line spells one, up to an ``=`` or a space.
_OPTION_WORD_RE = re.compile(r"--[A-Za-z0-9][A-Za-z0-9-]*")


@pytest.mark.unit
def test_no_ci_configuration_passes_the_developer_video_opt_in(
    ci_text, build_job, ux_job
) -> None:
    """The opt-in renders another stack's unredacted screens, and a pipeline's
    artifacts can be downloaded, so it belongs to a local dry run alone.

    The flag is read from the orchestrator, so a renamed one is still the one
    looked for, and so is every abbreviation of it the orchestrator's parser would
    accept, such as ``--dev-v``. Neither job's commands may carry one, nor
    anything else in either CI configuration; the orchestrator also refuses the
    parsed flag, however spelled, where ``CI`` is set.
    """
    flag = _module_constant(ORCHESTRATOR, "DEV_VIDEO_FLAG")
    assert isinstance(flag, str) and flag.startswith("--dev-"), flag
    spellings = _spellings(flag, _declared_long_options(ORCHESTRATOR))
    assert flag in spellings, sorted(spellings)
    texts = {
        name: "\n".join(str(entry) for entry in _flattened(job))
        for name, job in ((BUILD_JOB, build_job), (TEST_JOB, ux_job))
    }
    texts[".gitlab-ci.yml"] = ci_text
    workflows = sorted(GITHUB_WORKFLOWS.glob("*.y*ml"))
    assert workflows, f"no workflows under {GITHUB_WORKFLOWS}"
    for workflow in workflows:
        texts[workflow.name] = workflow.read_text(encoding="utf-8")
    for name, text in texts.items():
        carried = sorted(set(_OPTION_WORD_RE.findall(text)) & spellings)
        assert not carried, f"{name} passes {flag} as {carried}"


@pytest.mark.unit
def test_the_docker_runner_forwards_what_the_opt_in_is_refused_under() -> None:
    """The check above reads files, and ``make ai-ux-test-local`` takes
    ``EXTRA_ARGS`` from the environment, so what holds on that route is the
    orchestrator's own refusal. It runs inside the Docker runner's review
    container, which sees a host variable only when it is forwarded by name, so the
    runner forwards exactly the variables the refusal reads."""
    refused_under = _module_constant(ORCHESTRATOR, "CI_VARIABLES")
    forwarded = _module_constant(RUN_IN_DOCKER, "CI_ENV_NAMES")
    assert refused_under == forwarded == ("CI", "GITLAB_CI")
