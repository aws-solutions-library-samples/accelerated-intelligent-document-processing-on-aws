# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The shape of the advisory first-time-user story job.

``ai_ux_test_stories`` runs the stories a merge request's added CHANGELOG entries
select and replies in its code-review thread. It is ``allow_failure: true``, so a
broken job is a quiet one, and each property here is one a later edit to
``.gitlab-ci.yml`` could drop with the job still green. Five of them are security
properties rather than tidiness.

* **The job runs the target branch's harness or nothing.** It extracts
  :data:`ai_ux_test_stories.HARNESS_PATHS`, in order, from the target branch with
  ``git archive`` into a fresh directory and exits 1 when it cannot; it refuses,
  before its first run of the entry, a ``UX_HARNESS`` or ``UX_ENTRY`` that a
  project or pipeline variable has changed; and it runs nothing by a path in the
  checkout.
* **No CI configuration passes a development-only option of the orchestrator**,
  in any spelling argparse could read as one, abbreviations included. The
  orchestrator also refuses it wherever ``CI`` or ``GITLAB_CI`` is set, which
  ``test_ai_ux_test_stories.py`` pins.
* **Posting is opt-in.** The flag that allows it is in the job's arguments, and
  every run is against the review stack, the one stack a reply is posted for.
* **The job holds the scoped variables it reads and no others.** It declares
  ``ai_ux_test``'s environment with ``action: prepare``, which is what hands it
  the persona, the UI env and the account id, and no rule reads a scoped
  variable.
* **It is advisory.** It is not a shared gate, and it does not run on GitHub.

The rest pins the wiring: the rules, evaluated here the way GitLab evaluates them,
with the ``UX_TEST_STORIES`` switch; the timeout against the catalogue's deadline;
and where the job writes its artifacts and how long they are kept.
"""

from __future__ import annotations

import ast
import fnmatch
import functools
import itertools
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
GITLAB_CI = REPO_ROOT / ".gitlab-ci.yml"
GITHUB = REPO_ROOT / ".github"
ORCHESTRATOR = SCRIPTS / "sdlc" / "ai_ux_test_stories.py"
CREDENTIAL_VENDOR = SCRIPTS / "sdlc" / "cfn" / "credential-vendor.yml"
MR_JOB = "ai_ux_test_stories"
STORY_JOBS = (MR_JOB,)
RUN_JOBS = (MR_JOB,)
SETUP_JOB = "ai_ux_test"
HARNESS = "$UX_HARNESS_DIR"
ENTRY_WORD = "$UX_HARNESS_DIR/$UX_ENTRY"
SAMPLE_IID = "812"

Predicate = Callable[[Mapping[str, str]], bool]


@pytest.fixture(scope="module")
def ci_text() -> str:
    return GITLAB_CI.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ci_config(ci_text: str) -> dict[str, Any]:
    return yaml.safe_load(ci_text)


@pytest.fixture(scope="module")
def jobs(ci_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    missing = [name for name in STORY_JOBS if name not in ci_config]
    assert not missing, f"{missing} are gone from .gitlab-ci.yml"
    return {name: ci_config[name] for name in STORY_JOBS}


@pytest.fixture(scope="module")
def orchestrator() -> ModuleType:
    """The orchestrator, imported for its own parser and its names."""
    for entry in (SCRIPTS, SCRIPTS / "sdlc"):
        if str(entry) not in sys.path:
            sys.path.insert(0, str(entry))
    import ai_ux_test_stories

    return ai_ux_test_stories


@pytest.fixture(scope="module")
def catalogue(orchestrator: ModuleType) -> Any:
    return orchestrator.stories.load_catalogue()


def _module_constant(path: Path, name: str) -> Any:
    """The literal value of a module-level assignment, read without importing."""
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


def _flattened(job: Mapping[str, Any]) -> list[str]:
    """The job's before_script and script entries, a resolved anchor list unrolled."""
    entries: list[str] = []
    for entry in [*job.get("before_script", []), *job.get("script", [])]:
        entries.extend(
            str(item) for item in (entry if isinstance(entry, list) else [entry])
        )
    return entries


def _variables(job: Mapping[str, Any]) -> dict[str, str]:
    return {name: str(value) for name, value in (job.get("variables") or {}).items()}


_REFERENCE_RE = re.compile(
    r"\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?P<first>%%/\*)?\}"
    r"|\$(?P<bare>[A-Za-z_][A-Za-z0-9_]*)"
)
_SETUP_VARIABLES = ("UX_ENTRY", "UX_MODE_ARGS", "UX_ARTIFACTS")


def _setup_values(job: Mapping[str, Any]) -> dict[str, str]:
    """The job variables ``&ux_confined_setup`` reads a command through.

    ``$UX_HARNESS_DIR`` stays as written, since every pinned path must start with
    it, and ``$UX_HARNESS`` is read from the job's variables directly.
    """
    variables = _variables(job)
    return {name: variables[name] for name in _SETUP_VARIABLES if name in variables}


def _expanded(command: str, values: Mapping[str, str]) -> str:
    """``command`` with each of ``values`` written out as the shell would.

    ``${NAME%%/*}`` is the value's first path component. A value may name another
    variable, as a GitLab job variable may, so this runs until nothing changes.
    """

    def value(match: re.Match[str]) -> str:
        name = match["braced"] or match["bare"]
        if name not in values:
            return match[0]
        text = values[name]
        return text.split("/", 1)[0] if match["first"] else text

    for _ in range(len(values) + 1):
        expanded = _REFERENCE_RE.sub(value, command)
        if expanded == command:
            return expanded
        command = expanded
    raise AssertionError(f"{command!r} never stops expanding")


_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)\1")


def _without_heredoc_bodies(command: str) -> str:
    """``command`` with each here-document's body left out; its first line stays."""
    kept: list[str] = []
    closing: str | None = None
    for line in command.splitlines():
        if closing is not None:
            if line == closing:
                closing = None
            continue
        kept.append(line)
        found = _HEREDOC_RE.search(line)
        closing = found["tag"] if found else None
    return "\n".join(kept)


def _words(command: str) -> list[str]:
    """``command`` split as the shell would, its operators as words of their own."""
    lexer = shlex.shlex(
        _without_heredoc_bodies(command), posix=True, punctuation_chars=True
    )
    lexer.whitespace_split = True
    return list(lexer)


def _options(words: Sequence[str]) -> list[str]:
    return list(itertools.takewhile(lambda word: word.startswith("-"), words))


_LAUNCHERS = frozenset({"python3", "python", "bash", "sh"})
_FOREIGN_RUNNERS = frozenset({"make", "source", "."})
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SEARCH_PATH_RUNS = frozenset("cm")


def _python_program(options: Sequence[str]) -> str:
    """What a Python started with ``options`` runs: ``-c``/``-m``, ``-`` or a file."""
    for option in options:
        if option == "-":
            return "-"
        if not option.startswith("--") and _SEARCH_PATH_RUNS & set(option[1:]):
            return "-c/-m"
    return "file"


def _checkout_runs(command: str) -> list[str]:
    """Every word of ``command`` that runs something the job does not pin.

    A word naming a ``.py`` or ``.sh`` file, and the first word after an
    interpreter that is not one of its options, must lie under
    ``$UX_HARNESS_DIR``. A Python reading its program from a here-document runs
    the pipeline definition's own text, so it must run isolated (``-I``), which
    keeps the checkout it starts in off its import path; ``-c`` and ``-m`` are
    refused, as ``make``, ``source`` and ``.`` are, because each runs code the
    job does not pin.
    """
    words = _words(command)
    found: list[str] = []
    for index, word in enumerate(words):
        if word in _FOREIGN_RUNNERS:
            found.append(word)
            continue
        if _ASSIGNMENT_RE.match(word):
            continue
        if word.endswith((".py", ".sh")) and not word.startswith(f"{HARNESS}/"):
            found.append(word)
        if word not in _LAUNCHERS:
            continue
        options = _options(words[index + 1 :])
        rest = words[index + 1 + len(options) :]
        program = _python_program(options) if word.startswith("python") else "file"
        if program == "-c/-m":
            found.append(f"{word} {' '.join(options)}")
        elif program == "-":
            if "-I" not in options:
                found.append(f"{word} {' '.join(options)} without -I")
        elif not rest or not rest[0].startswith(f"{HARNESS}/"):
            found.append(f"{word} {rest[0] if rest else ''}".rstrip())
    return found


def _python_runs(command: str) -> list[list[str]]:
    """The options of every Python ``command`` starts."""
    words = _words(command)
    return [
        _options(words[index + 1 :])
        for index, word in enumerate(words)
        if word in ("python3", "python")
    ]


_SHELL_OPERATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", "<", ">", "<<"})


def _entry_argvs(job: Mapping[str, Any], values: Mapping[str, str]) -> list[list[str]]:
    """The arguments each of the job's commands passes the entry, in order."""
    entry = _variables(job)["UX_ENTRY"]
    found: list[list[str]] = []
    for command in _flattened(job):
        for line in _without_heredoc_bodies(command).splitlines():
            if ENTRY_WORD not in line:
                continue
            words = _words(_expanded(line, values))
            (at,) = [i for i, word in enumerate(words) if word == f"{HARNESS}/{entry}"]
            following = words[at + 1 :]
            found.append(
                list(
                    itertools.takewhile(lambda w: w not in _SHELL_OPERATORS, following)
                )
            )
    return found


def _parsed(orchestrator: ModuleType, argv: list[str]) -> Any:
    try:
        return orchestrator.parse_args(argv)
    except SystemExit as refused:
        raise AssertionError(
            f"the orchestrator's parser refuses {argv} (exit {refused.code})"
        ) from None


def _minutes(timeout: str) -> int:
    match = re.fullmatch(r"(?:([0-9]+)h)?\s*(?:([0-9]+)m)?", timeout.strip())
    assert match and any(match.groups()), f"{timeout!r} is not a timeout this reads"
    return int(match[1] or 0) * 60 + int(match[2] or 0)


_TOKEN_RE = re.compile(
    r"\s*(?:(?P<var>\$[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<string>\"[^\"]*\"|'[^']*')"
    r"|(?P<regex>/(?:[^/\\\n]|\\.)+/[a-z]*)"
    r"|(?P<null>null)\b"
    r"|(?P<op>==|!=|=~|!~|&&|\|\||\(|\)))"
)
_COMPARISONS = frozenset({"==", "!=", "=~", "!~"})
_REGEX_FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}


def _tokens(expression: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    at, end = 0, len(expression.rstrip())
    while at < end:
        match = _TOKEN_RE.match(expression, at)
        assert match and match.lastgroup, (
            f"cannot read {expression[at:]!r} in {expression!r}"
        )
        tokens.append((match.lastgroup, match[match.lastgroup]))
        at = match.end()
    return tokens


def _any_of(left: Predicate, right: Predicate) -> Predicate:
    def either(variables: Mapping[str, str]) -> bool:
        return left(variables) or right(variables)

    return either


def _all_of(left: Predicate, right: Predicate) -> Predicate:
    def both(variables: Mapping[str, str]) -> bool:
        return left(variables) and right(variables)

    return both


def _is_set(name: str) -> Predicate:
    def defined(variables: Mapping[str, str]) -> bool:
        return bool(variables.get(name))

    return defined


def _regex_flags(flags: str, expression: str) -> re.RegexFlag:
    chosen = re.RegexFlag(0)
    for flag in flags:
        assert flag in _REGEX_FLAGS, f"{expression!r}: /{flag} is not a flag this reads"
        chosen |= _REGEX_FLAGS[flag]
    return chosen


def _compared(
    name: str, operator: str, operand: tuple[str, str], expression: str
) -> Predicate:
    kind, written = operand
    if operator in ("=~", "!~"):
        assert kind == "regex", f"{expression!r}: {operator} takes a /regex/"
        pattern, _, flags = written[1:].rpartition("/")
        compiled = re.compile(pattern, _regex_flags(flags, expression))

        def matched(variables: Mapping[str, str]) -> bool:
            current = variables.get(name)
            found = current is not None and compiled.search(current) is not None
            return found if operator == "=~" else not found

        return matched
    assert kind in ("string", "var", "null"), f"{expression!r}: {written!r} after =="

    def equal(variables: Mapping[str, str]) -> bool:
        if kind == "string":
            other: str | None = written[1:-1]
        elif kind == "var":
            other = variables.get(written[1:])
        else:
            other = None
        same = variables.get(name) == other
        return same if operator == "==" else not same

    return equal


class _Expression:
    """A GitLab ``rules:if`` expression, as GitLab evaluates one.

    Variables are compared with a string, a regex, ``null`` or one another; a
    bare variable is true when it is set and not empty; ``&&`` binds tighter
    than ``||``; and parentheses group. An undefined variable equals only
    ``null`` and matches no regex. Anything else is refused rather than guessed
    at, so a rule this cannot read fails the test that reads it.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = _tokens(text)
        self.at = 0
        self.predicate = self._either()
        assert self.at == len(self.tokens), f"{text!r} runs on after a whole expression"

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def _take(self) -> tuple[str, str]:
        token = self._peek()
        assert token is not None, f"{self.text!r} ends early"
        self.at += 1
        return token

    def _either(self) -> Predicate:
        left = self._both()
        while self._peek() == ("op", "||"):
            self._take()
            left = _any_of(left, self._both())
        return left

    def _both(self) -> Predicate:
        left = self._term()
        while self._peek() == ("op", "&&"):
            self._take()
            left = _all_of(left, self._term())
        return left

    def _term(self) -> Predicate:
        kind, written = self._take()
        if (kind, written) == ("op", "("):
            inner = self._either()
            assert self._take() == ("op", ")"), f"{self.text!r} leaves a ( open"
            return inner
        assert kind == "var", f"{self.text!r}: {written!r} where a variable belongs"
        following = self._peek()
        if following is None or following[1] not in _COMPARISONS:
            return _is_set(written[1:])
        _, operator = self._take()
        return _compared(written[1:], operator, self._take(), self.text)


@functools.cache
def _predicate(expression: str) -> Predicate:
    return _Expression(expression).predicate


@dataclass(frozen=True)
class Pipeline:
    """One pipeline as a job's rules see it: its variables, and what its MR changes."""

    variables: Mapping[str, str]
    changed: tuple[str, ...] = ()


def _merge_request(
    *,
    target: str = "develop",
    title: str = "Add a first-time-user story",
    changed: Sequence[str] = ("CHANGELOG.md",),
    **extra: str,
) -> Pipeline:
    variables = {
        "CI": "true",
        "GITLAB_CI": "true",
        "CI_PIPELINE_SOURCE": "merge_request_event",
        "CI_MERGE_REQUEST_IID": SAMPLE_IID,
        "CI_MERGE_REQUEST_TARGET_BRANCH_NAME": target,
        "CI_MERGE_REQUEST_TITLE": title,
        **extra,
    }
    return Pipeline(variables, tuple(changed))


def _branch_pipeline(source: str, *, branch: str = "develop", **extra: str) -> Pipeline:
    variables = {
        "CI": "true",
        "GITLAB_CI": "true",
        "CI_PIPELINE_SOURCE": source,
        "CI_COMMIT_BRANCH": branch,
        **extra,
    }
    return Pipeline(variables)


def _changes_match(changes: Any, pipeline: Pipeline) -> bool:
    """Whether ``changes`` holds for ``pipeline``.

    Outside a merge request it holds, as GitLab has it hold for a scheduled or a
    Run pipeline, which have no push to compare with.
    """
    if pipeline.variables.get("CI_PIPELINE_SOURCE") != "merge_request_event":
        return True
    globs = changes["paths"] if isinstance(changes, Mapping) else changes
    return any(
        fnmatch.fnmatchcase(path, str(glob))
        for path in pipeline.changed
        for glob in globs
    )


def _outcome(job: Mapping[str, Any], pipeline: Pipeline) -> str:
    """What GitLab does with ``job`` in ``pipeline``: its first matching rule's ``when``.

    ``never`` when no rule matches, which leaves the job out of the pipeline.
    """
    for rule in job["rules"]:
        if "if" in rule and not _predicate(str(rule["if"]))(pipeline.variables):
            continue
        if "changes" in rule and not _changes_match(rule["changes"], pipeline):
            continue
        return str(rule.get("when", "on_success"))
    return "never"


def _triggering(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [rule for rule in rules if rule.get("when") not in (None, "never")]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("expression", "variables", "expected"),
    [
        ('$A == "x"', {"A": "x"}, True),
        ('$A == "x"', {}, False),
        ('$A != "x"', {}, True),
        ("$A", {"A": ""}, False),
        ("$A", {"A": "on"}, True),
        ("$A && $B", {"A": "1"}, False),
        ('$A || $B == "x" && $C', {"B": "x"}, False),
        ('$A || $B == "x" && $C', {"A": "1"}, True),
        ('($A || $B == "x") && $C', {"A": "1"}, False),
        ("$T !~ /^Draft:/", {"T": "Draft: a story"}, False),
        ("$T !~ /^Draft:/", {"T": "A story, not a Draft: yet"}, True),
        ("$T !~ /^Draft:/", {}, True),
        ("$T =~ /^draft:/i", {"T": "Draft: a story"}, True),
        ("$A == null", {}, True),
        ("$A == null", {"A": ""}, False),
        ("$A == $B", {"A": "x", "B": "x"}, True),
    ],
)
def test_the_rule_reader_evaluates_expressions_as_gitlab_does(
    expression: str, variables: dict[str, str], expected: bool
) -> None:
    assert _predicate(expression)(variables) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "expression", ['$A === "x"', "$A == develop", '"x" == $A', "$A && ", '($A == "x"']
)
def test_the_rule_reader_refuses_what_it_cannot_read(expression: str) -> None:
    with pytest.raises(AssertionError):
        _predicate(expression)


_MR_CASES = [
    (
        "a non-Draft MR to develop that changes the CHANGELOG",
        _merge_request(),
        "on_success",
    ),
    (
        "one that changes the UI too",
        _merge_request(changed=("CHANGELOG.md", "src/ui/src/App.tsx")),
        "on_success",
    ),
    (
        "one that changes the UI only",
        _merge_request(changed=("src/ui/src/App.tsx",)),
        "never",
    ),
    (
        "one that changes another CHANGELOG",
        _merge_request(changed=("docs/CHANGELOG.md",)),
        "never",
    ),
    ("a Draft", _merge_request(title="Draft: Add a first-time-user story"), "never"),
    ("an MR to main", _merge_request(target="main"), "never"),
    ("a push to develop", _branch_pipeline("push"), "never"),
    ("the scheduled pipeline", _branch_pipeline("schedule"), "never"),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("pipeline", "expected"),
    [(pipeline, expected) for _, pipeline, expected in _MR_CASES],
    ids=[label for label, _, _ in _MR_CASES],
)
def test_the_mr_job_runs_on_non_draft_mrs_to_develop_that_change_the_changelog(
    jobs: dict[str, dict[str, Any]], pipeline: Pipeline, expected: str
) -> None:
    assert _outcome(jobs[MR_JOB], pipeline) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("switch", "mr_runs"),
    [
        (None, True),
        ("", True),
        ("mr", True),
        ("off", False),
        ("OFF", False),
        ("false", False),
        ("0", False),
        ("MR", False),
    ],
)
def test_ux_test_stories_switches_the_jobs_and_any_other_value_stops_them(
    jobs: dict[str, dict[str, Any]],
    switch: str | None,
    mr_runs: bool,
) -> None:
    """Unset, empty or ``mr`` runs the job, and anything else stops it, so a
    mistyped ``off`` still works as one."""
    extra = {} if switch is None else {"UX_TEST_STORIES": switch}
    mr = _outcome(jobs[MR_JOB], _merge_request(**extra))
    assert mr == ("on_success" if mr_runs else "never")


@pytest.mark.unit
def test_the_mr_job_runs_only_where_the_code_review_it_replies_to_runs(
    ci_config: dict[str, Any], jobs: dict[str, dict[str, Any]]
) -> None:
    """The reply goes into the ``ai_mr_review`` note's thread, so the trigger is the
    review's own narrowed by ``changes``; a wider one would post standalone notes
    on MRs the review never reads."""
    (review,) = _triggering(ci_config["ai_mr_review"]["rules"])
    (trigger,) = _triggering(jobs[MR_JOB]["rules"])
    assert trigger["if"] == review["if"]
    assert trigger["changes"] == {
        "paths": ["CHANGELOG.md"],
        "compare_to": "refs/heads/develop",
    }
    assert jobs[MR_JOB]["rules"][-1] == {"when": "never"}


@pytest.mark.unit
def test_every_story_job_is_advisory_and_holds_no_lock(
    jobs: dict[str, dict[str, Any]],
) -> None:
    """No rule makes a job blocking, a manual one included, and no job holds a
    ``resource_group``: each only looks at the review stack, and the MR job
    replies rather than editing the review note, so none waits for
    ``ai_ux_test``."""
    for name, job in jobs.items():
        assert job["allow_failure"] is True, f"{name} can now fail a pipeline"
        for rule in job["rules"]:
            assert rule.get("allow_failure", True) is True, (name, rule)
        assert "resource_group" not in job, (
            f"{name} now waits on {job['resource_group']}"
        )


@pytest.mark.unit
def test_the_mr_job_is_interruptible(
    jobs: dict[str, dict[str, Any]],
) -> None:
    """A newer push supersedes an MR run."""
    assert jobs[MR_JOB]["interruptible"] is True


@pytest.mark.unit
def test_the_timeouts_outlast_the_deadline_the_flows_keep_to(
    jobs: dict[str, dict[str, Any]], catalogue: Any
) -> None:
    """A run in CI keeps to the catalogue's ``deadline_minutes`` from the job's
    start, so the job's own timeout must be longer, or GitLab kills a run that
    would have finished."""
    assert jobs[MR_JOB]["timeout"] == "60m"
    for name in RUN_JOBS:
        assert _minutes(jobs[name]["timeout"]) > catalogue.runs.deadline_minutes, name


@pytest.mark.unit
def test_the_run_jobs_take_the_runner_class_the_mr_ux_test_takes(
    ci_config: dict[str, Any], jobs: dict[str, dict[str, Any]]
) -> None:
    """The same confined browser, so the same runner."""
    tags = ci_config[SETUP_JOB]["tags"]
    assert tags == ["size:large"]
    for name in RUN_JOBS:
        assert jobs[name]["tags"] == tags, name


@pytest.mark.unit
def test_the_jobs_sit_where_no_text_slice_of_another_suite_reaches(
    ci_text: str,
) -> None:
    """``test_after_script_log_capture.py`` slices ``integration_tests`` up to the
    next top-level key, and ``test_ux_test_ci_jobs.py`` finds the setup anchor
    between ``ai_ux_test`` and ``nightly_notification``."""
    names = (
        "integration_tests",
        "srt_security_review",
        SETUP_JOB,
        MR_JOB,
        "nightly_notification",
    )
    positions = [ci_text.index(f"\n{name}:\n") for name in names]
    assert positions == sorted(positions), dict(zip(names, positions, strict=True))


@pytest.mark.unit
def test_the_mr_job_tests_the_mrs_build_only_when_the_pipeline_builds_one(
    ci_config: dict[str, Any], jobs: dict[str, dict[str, Any]]
) -> None:
    """An optional need on a job the pipeline does not hold is dropped, so an MR
    that does not change ``src/ui`` starts the job at once and tests the deployed
    UI, and one that does waits for the build."""
    assert "ux_test_build" in ci_config
    assert jobs[MR_JOB]["needs"] == [
        {"job": "ux_test_build", "artifacts": True, "optional": True}
    ]


@pytest.mark.unit
def test_the_mr_job_clones_the_history_its_inputs_are_diffed_over(
    jobs: dict[str, dict[str, Any]],
) -> None:
    """The MR's story inputs diff its head against the merge base with develop,
    which a shallow clone does not have."""
    assert _variables(jobs[MR_JOB])["GIT_DEPTH"] == "0"


@pytest.mark.unit
def test_the_run_jobs_use_the_orchestrators_model_inside_the_gitlab_roles_grant(
    ci_config: dict[str, Any], jobs: dict[str, dict[str, Any]]
) -> None:
    """The model must be one the run's cost cap prices, and one the GitLab role may
    invoke, or every flow ends in an access error behind an advisory job."""
    model = _module_constant(ORCHESTRATOR, "DEFAULT_MODEL")
    rates = _module_constant(ORCHESTRATOR, "MODEL_RATES")
    grants = re.findall(
        r"inference-profile/([^\"\s]+)\"", CREDENTIAL_VENDOR.read_text(encoding="utf-8")
    )
    assert model in rates, f"{model} has no pinned rates, so its spend is not counted"
    assert any(fnmatch.fnmatchcase(model, grant) for grant in grants), (model, grants)
    region = _variables(ci_config[SETUP_JOB])["AWS_REGION"]
    for name in RUN_JOBS:
        variables = _variables(jobs[name])
        assert variables["ANTHROPIC_MODEL"] == model, name
        assert variables["AWS_REGION"] == region, name


#: The harness extraction as a job must spell it: the target branch's tree, the
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
@pytest.mark.parametrize("name", STORY_JOBS)
def test_each_job_extracts_the_harness_from_the_target_branch_and_fails_closed(
    jobs: dict[str, dict[str, Any]], name: str
) -> None:
    """``git archive`` into a fresh directory, from ``refs/heads/`` so a tag called
    like the branch cannot stand in for it, and ``exit 1`` when it cannot: an
    in-place checkout would leave MR-added files beside the pinned ones."""
    before = "\n".join(str(line) for line in jobs[name]["before_script"])
    assert 'TARGET="${CI_MERGE_REQUEST_TARGET_BRANCH_NAME:-develop}"' in before
    fetches = re.findall(r"^\s*git fetch\b.*$", before, re.MULTILINE)
    assert fetches == ['git fetch -q origin "refs/heads/$TARGET"'], fetches
    (extraction,) = _EXTRACTION_RE.finditer(before)
    refuse = [line.strip() for line in extraction["refuse"].splitlines()]
    assert refuse and refuse[-1] == "exit 1", refuse
    assert all(not line.startswith("exit") for line in refuse[:-1]), refuse
    assert "exit" not in extraction["then"], extraction["then"]
    assert "git checkout FETCH_HEAD" not in before


@pytest.mark.unit
def test_each_job_names_the_entrys_own_harness_paths_and_entry(
    ci_text: str, ci_config: dict[str, Any], jobs: dict[str, dict[str, Any]]
) -> None:
    """The job's list and the module's must be one list: ``test_ai_ux_test_stories``
    closes ``HARNESS_PATHS`` over its imports, which protects nothing unless the job
    extracts that set, and the precheck refuses any other."""
    harness = _module_constant(ORCHESTRATOR, "HARNESS_PATHS")
    entry = _module_constant(ORCHESTRATOR, "ENTRY")
    assert entry == ORCHESTRATOR.relative_to(REPO_ROOT).as_posix()
    harness_dir = _variables(ci_config[SETUP_JOB])["UX_HARNESS_DIR"]
    for name, job in jobs.items():
        variables = _variables(job)
        assert tuple(variables["UX_HARNESS"].split()) == harness, name
        assert variables["UX_ENTRY"] == entry, name
        assert variables["UX_HARNESS_DIR"] == harness_dir, name
        before = "\n".join(str(line) for line in job["before_script"])
        assert not re.search(r"^\s*UX_HARNESS=", before, re.MULTILINE), name
    assert ci_text.count("&ux_story_harness") == 1
    assert ci_text.count("*ux_story_harness") == len(STORY_JOBS) - 1


@pytest.mark.unit
def test_the_mr_job_takes_the_confined_setup_whole(
    ci_text: str, ci_config: dict[str, Any], jobs: dict[str, dict[str, Any]]
) -> None:
    """The same steps in the same order as ``ai_ux_test``, driven by the job's own
    variables: the umask first, the secretless apt, the pin, the root-only
    checkout, a fresh artifact directory, and the precheck before the install."""
    setup = ci_config[SETUP_JOB]["before_script"]
    for name in RUN_JOBS:
        assert jobs[name]["before_script"] == setup, name
    assert ci_text.count("before_script: *ux_confined_setup") == len(RUN_JOBS)
    assert setup[0] == "umask 022"


def _bash(
    script: str, *, cwd: Path, environ: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash, "bash runs the job's script and is on every Linux and macOS runner"
    return subprocess.run(
        [bash, "-c", script],
        cwd=cwd,
        env=dict(environ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


@pytest.mark.unit
@pytest.mark.parametrize("name", RUN_JOBS)
def test_a_run_job_runs_its_flows_only_once_the_precheck_said_proceed(
    jobs: dict[str, dict[str, Any]], name: str
) -> None:
    (block,) = [str(entry) for entry in jobs[name]["script"]]
    head, separator, rest = block.partition("\nfi\n")
    assert separator, block
    assert head.startswith('if [ ! -f "$UX_ARTIFACTS/proceed" ]; then\n'), head
    assert head.rstrip().endswith("exit 0"), head
    assert ENTRY_WORD not in head and ENTRY_WORD in rest


@pytest.mark.unit
def test_the_runs_record_the_commit_their_harness_was_extracted_from(
    ci_config: dict[str, Any], jobs: dict[str, dict[str, Any]], tmp_path: Path
) -> None:
    """The extracted tree has no git of its own, so the commit is read from the
    archive it came out of, which ``git archive`` stamps with it; FETCH_HEAD is no
    record of it by then, because an MR precheck fetches the MR head and the
    target branch again."""
    setup = "\n".join(str(step) for step in ci_config[SETUP_JOB]["before_script"])
    (extraction,) = _EXTRACTION_RE.finditer(setup)
    line = f'export UX_HARNESS_SHA="$(git get-tar-commit-id < {extraction["tar"]})"'
    for name in RUN_JOBS:
        (block,) = [str(entry) for entry in jobs[name]["script"]]
        lines = [text.strip() for text in block.splitlines()]
        assert lines.count(line) == 1, (name, lines)
        runs = [i for i, text in enumerate(lines) if ENTRY_WORD in text]
        assert runs and lines.index(line) < min(runs), name
    archive = tmp_path / "harness.tar"
    git = shutil.which("git")
    assert git, "git is how the job extracts its harness"
    made = subprocess.run(
        [git, "-C", str(REPO_ROOT), "archive", "--format=tar", "-o", str(archive)]
        + ["HEAD", "--", ".gitlab-ci.yml"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert made.returncode == 0, made.stderr[-400:]
    head = subprocess.run(
        [git, "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout.strip()
    probe = line.replace(extraction["tar"], shlex.quote(str(archive)))
    shown = _bash(
        f'{probe}\nprintf %s "$UX_HARNESS_SHA"',
        cwd=tmp_path,
        environ={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)},
    )
    assert shown.returncode == 0, shown.stderr[-400:]
    assert shown.stdout == head


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "python3 -I -B scripts/sdlc/ai_ux_test_stories.py --mr 812",
        "python3 -B ./scripts/sdlc/ai_ux_test_stories.py --judge a",
        "sh scripts/ux_runner/install.sh",
        "make ai-ux-test-stories-local",
        ". scripts/x.sh",
        "source scripts/x.sh",
        "python3 -B - <<'PY'\nimport ai_ux_test_stories\nPY",
        "python3 -I -B -c 'import ai_ux_test_stories'",
        "python3 -I -B -m ai_ux_test_stories --mr 812",
    ],
)
def test_the_checkout_scan_catches_every_way_of_running_the_mrs_copy(
    command: str,
) -> None:
    assert _checkout_runs(command), command


@pytest.mark.unit
@pytest.mark.parametrize("name", STORY_JOBS)
def test_nothing_in_a_story_job_runs_the_checkouts_own_copy(
    jobs: dict[str, dict[str, Any]], name: str
) -> None:
    """Read as written and through the job's variables, both of which must hold."""
    job = jobs[name]
    values = _setup_values(job)
    commands = [*_flattened(job), *(_expanded(c, values) for c in _flattened(job))]
    offending = {c: found for c in commands if (found := _checkout_runs(c))}
    assert not offending, f"{name} runs something the harness does not pin: {offending}"


@pytest.mark.unit
@pytest.mark.parametrize("name", STORY_JOBS)
def test_every_python_a_story_job_starts_writes_no_bytecode(
    jobs: dict[str, dict[str, Any]], name: str
) -> None:
    """``-B`` stops Python writing ``__pycache__`` into the root-only harness, and a
    program read from a here-document runs isolated, off the checkout's path."""
    runs = [
        options
        for command in _flattened(jobs[name])
        for options in _python_runs(command)
    ]
    assert len(runs) == 2, (name, runs)
    for options in runs:
        assert "-B" in options, (name, options)
        if "-" in options:
            assert "-I" in options, (name, options)


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
    """Every option word argparse could read as ``flag``: the flag, and each prefix
    of it no other declared option shares, which argparse takes as an abbreviation
    unless the parser is built with ``allow_abbrev=False``."""
    rivals = others - {flag}
    return {
        flag[:end]
        for end in range(len("--") + 1, len(flag) + 1)
        if not any(other.startswith(flag[:end]) for other in rivals)
    }


#: An option word as a shell line spells one, up to an ``=`` or a space.
_OPTION_WORD_RE = re.compile(r"--[A-Za-z0-9][A-Za-z0-9-]*")


def _github_texts() -> dict[str, str]:
    paths = sorted(GITHUB.glob("workflows/*.y*ml"))
    actions = sorted(GITHUB.glob("actions/*/action.y*ml"))
    assert paths and actions, f"no workflows or composite actions under {GITHUB}"
    return {
        path.relative_to(REPO_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in [*paths, *actions]
    }


@pytest.mark.unit
def test_no_ci_configuration_passes_a_development_only_option(ci_text: str) -> None:
    """``--dev-unconfined`` drops the managed policy and the uid switch, so it is
    for a development host's own Chrome alone. The parser is built with
    ``allow_abbrev=False`` today, so only the whole flag would be read, but the
    scan refuses every unambiguous prefix of it as well, so it holds whatever
    that setting becomes; and it covers every ``--dev-`` option the parser
    declares, so a second one is held to the same rule."""
    declared = _declared_long_options(ORCHESTRATOR)
    development = sorted(option for option in declared if option.startswith("--dev-"))
    assert "--dev-unconfined" in development, development
    texts = {".gitlab-ci.yml": ci_text, **_github_texts()}
    for flag in development:
        spellings = _spellings(flag, declared)
        assert flag in spellings, sorted(spellings)
        for where, text in texts.items():
            carried = sorted(set(_OPTION_WORD_RE.findall(text)) & spellings)
            assert not carried, f"{where} passes {flag} as {carried}"


@pytest.mark.unit
def test_only_the_mr_job_can_post(jobs: dict[str, dict[str, Any]]) -> None:
    """Posting is opt-in: the MR job's arguments carry the flag, for its precheck's
    one-line replies and for its run's."""
    declared = _declared_long_options(ORCHESTRATOR)
    spellings = _spellings("--post", declared)
    assert "--post" in spellings, sorted(spellings)
    mode_args = _variables(jobs[MR_JOB])["UX_MODE_ARGS"]
    assert "--post" in _OPTION_WORD_RE.findall(mode_args), mode_args


def _parsed_runs(
    jobs: dict[str, dict[str, Any]], orchestrator: ModuleType
) -> dict[str, list[Any]]:
    """Every command line each job hands the entry, read by the entry's own parser."""
    parsed: dict[str, list[Any]] = {}
    for name, job in jobs.items():
        values = {**_setup_values(job), "CI_MERGE_REQUEST_IID": SAMPLE_IID}
        parsed[name] = [
            _parsed(orchestrator, argv) for argv in _entry_argvs(job, values)
        ]
    return parsed


@pytest.mark.unit
def test_each_story_command_line_is_one_the_orchestrators_parser_reads_as_meant(
    jobs: dict[str, dict[str, Any]], orchestrator: ModuleType
) -> None:
    """Read with the orchestrator's own ``parse_args``: the mode, the run
    directory and the posting each command means, and in none of them the
    development-only option or a stack other than the review stack, the one stack
    a story video is made of and a reply is posted for."""
    parsed = _parsed_runs(jobs, orchestrator)
    review_stack = orchestrator.session.REVIEW_STACK
    for name, runs in parsed.items():
        for args in runs:
            assert args.dev_unconfined is False, name
            assert args.post is (name == MR_JOB), name
            assert args.stack == review_stack, name
            assert args.video == "findings", name
    precheck, run = parsed[MR_JOB]
    for args in (precheck, run):
        assert args.mr == int(SAMPLE_IID)
        assert args.artifact_dir == Path("ux-test-stories")
    assert precheck.precheck is True and precheck.inputs is None
    assert run.precheck is False and run.inputs == Path("ux-test-stories/inputs.json")


@pytest.mark.unit
def test_each_job_keeps_its_artifacts_where_it_writes_them(
    jobs: dict[str, dict[str, Any]], orchestrator: ModuleType
) -> None:
    """Nothing kept holds a session or a credential."""
    root = orchestrator.DEFAULT_RUN_DIR_NAME
    kept = {
        MR_JOB: ([f"{root}/"], "1 week"),
    }
    for name, job in jobs.items():
        artifacts = job["artifacts"]
        paths, expire_in = kept[name]
        assert artifacts["when"] == "always", name
        assert artifacts["paths"] == paths, name
        assert artifacts["expire_in"] == expire_in, name
        written = _variables(job)["UX_ARTIFACTS"]
        assert artifacts["paths"] == [f"{written.split('/', 1)[0]}/"], name
        for path in paths:
            for word in ("scratch", "recordings", "aws_after_script"):
                assert word not in path, (name, path)


@pytest.mark.unit
def test_no_story_job_is_a_shared_gate() -> None:
    from test_ci_gate_parity import SHARED_GATES

    for gate in SHARED_GATES:
        for name in (*STORY_JOBS, "ai-ux-test-stories"):
            assert name not in gate, (
                f"SHARED_GATES names {gate!r}, which asserts the advisory "
                "first-time-user stories run as a gate in both CIs"
            )


@pytest.mark.unit
def test_the_story_jobs_are_gitlab_only() -> None:
    for where, text in _github_texts().items():
        for needle in ("ai_ux_test_stories", "ai-ux-test-stories", "ux_stories.toml"):
            assert needle not in text, f"{where} names {needle}"


#: The variables the browser jobs' environment scopes, which the run jobs read.
BROWSER_SCOPED_VARIABLES = (
    "UX_TEST_UI_ENV",
    "UX_TEST_PERSONAS",
    "UX_TEST_AWS_ACCOUNT_ID",
)


@pytest.mark.unit
def test_the_run_jobs_declare_the_environment_the_mr_ux_test_declares(
    ci_config: dict[str, Any], jobs: dict[str, dict[str, Any]]
) -> None:
    """The run jobs read the variables ``ai_ux_test`` reads, the personas, the UI
    env and the account id among them, and a variable set at an environment's
    scope reaches only a job that declares that environment. A run job declaring
    another than ``ai_ux_test``'s would stop at its precheck with ``SKIPPED:`` and
    exit 0 on every merge request, which nothing else notices.
    ``prepare`` is the action that hands a job the variables without recording a
    deployment, and the declaration is the job's own, not inherited."""
    expected = ci_config[SETUP_JOB].get("environment")
    assert isinstance(expected, dict) and expected.get("action") == "prepare", expected
    assert isinstance(expected.get("name"), str) and expected["name"], expected
    for name in RUN_JOBS:
        assert jobs[name].get("environment") == expected, name
        assert "extends" not in jobs[name], name


@pytest.mark.unit
@pytest.mark.parametrize("name", STORY_JOBS)
def test_no_story_job_rule_reads_a_variable_an_environment_scopes(
    jobs: dict[str, dict[str, Any]], name: str
) -> None:
    """GitLab can leave an environment-scoped variable undefined while it evaluates
    ``rules`` at pipeline creation, so a rule reading one would quietly decide that
    the job does not exist."""
    rules = yaml.safe_dump(jobs[name]["rules"])
    read = [variable for variable in BROWSER_SCOPED_VARIABLES if variable in rules]
    assert not read, f"{name}'s rules read {read}"


@pytest.mark.unit
def test_the_story_jobs_comments_name_the_environment_the_run_jobs_declare(
    ci_text: str, ci_config: dict[str, Any]
) -> None:
    """The note above ``ai_ux_test_stories`` and the CI coverage doc tell the
    operator which environment the run jobs take their variables from, by the name
    the jobs declare, so storing them at another scope is not an option either
    offers."""
    name = ci_config[SETUP_JOB]["environment"]["name"]
    start = ci_text.index(
        "# Advisory first-time-user stories (scripts/ux_stories.toml)"
    )
    note = " ".join(
        line.lstrip("#").strip()
        for line in ci_text[start : ci_text.index(f"\n{MR_JOB}:", start)].splitlines()
    )
    assert f"environment scope `{name}`" in note, note
    assert f"`{name}` with `action: prepare`" in note, note
    coverage = (REPO_ROOT / "scripts/sdlc/docs/CI_TEST_COVERAGE.md").read_text(
        encoding="utf-8"
    )
    flat = " ".join(coverage.split())
    assert f"declares `ai_ux_test`'s environment, `{name}`, with `action: prepare`" in (
        flat
    )


@pytest.mark.unit
def test_the_job_whose_earlier_runs_the_allowance_counts_is_the_mr_job(
    jobs: dict[str, dict[str, Any]], orchestrator: ModuleType
) -> None:
    assert orchestrator.MR_JOB == MR_JOB and MR_JOB in jobs
    variables = _variables(jobs[MR_JOB])
    assert "--mr" in variables["UX_MODE_ARGS"].split()
