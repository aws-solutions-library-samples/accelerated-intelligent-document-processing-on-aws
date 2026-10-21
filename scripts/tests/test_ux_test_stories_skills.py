# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the two first-time-user story skills and their Cline entries."""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_markdown_links  # noqa: E402
import ux_story_mcp  # noqa: E402
from ux_runner import (  # noqa: E402
    changelog_select,
    stories,
    story_relay,
    tool_profiles,
)

SKILLS = REPO_ROOT / ".claude" / "skills"
OPERATOR_SKILL = SKILLS / "ux-test-stories.md"
CI_CONTRACT = SKILLS / "ux-test-stories-ci.md"
MR_UX_CONTRACT = SKILLS / "ux-test-ci.md"
CLINE_SKILLS = REPO_ROOT / ".cline" / "skills"
CLINE_TABLE_SOURCE = SCRIPTS / "tests" / "test_repo_quality_review_skill.py"
MR_UX_TEST_SUITE = SCRIPTS / "tests" / "test_ai_ux_test.py"
REVIEW_STACK_CHECK = SCRIPTS / "ux_runner" / "session.py"
MAKEFILE = REPO_ROOT / "Makefile"
BRIEF_START = "<!-- brief:start -->"
BRIEF_END = "<!-- brief:end -->"
PROVENANCE_LINE = (
    "The method (intent-only stories, docs followed literally, four note and report "
    "tools, verdicts judged against an expectation) is adapted, with its author's "
    "consent, from a teammate's prototype; no code was copied."
)
SECRET_NAMES: tuple[str, ...] = (
    "UX_TEST_PERSONAS",
    "UX_TEST_AWS_ACCOUNT_ID",
    "UX_TEST_UI_ENV",
    "GITLAB_REVIEW_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)
FREE_TEXT_FILES: tuple[str, ...] = (
    "results.json",
    "section.md",
    "report.md",
    "journal.jsonl",
    "screens/",
)
SESSION_HEADING = "## When you are asked to run one"
BOUNDS_HEADING = "## Privacy and bounds"
RUN_MODE_HEADINGS: tuple[str, ...] = (
    "## Unattended runs in Docker",
    "## Pair: a session you start plays the user",
)
TWELVE_DIGITS = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")
SNAKE_WORD = re.compile(r"\b[a-z]+(?:_[a-z]+)+\b")
CODE_SPAN = re.compile(r"(`+)[^\n]*?\1")
MAKE_TARGET = re.compile(r"\bmake (ai-ux-[a-z0-9-]+)")
SCRIPT_PATH = re.compile(r"\b(scripts[\\/][A-Za-z0-9_.\\/-]+\.py)\b")
FIXTURE_CATALOGUE: dict[str, Any] = {
    "version": 1,
    "personas": {"newcomer": {"group": "Author", "background": "A newcomer."}},
    "stories": [
        {
            "id": "pin-the-points",
            "title": "Pin the selector points",
            "intent": "Find out how an entry earns points.",
            "kind": "read",
            "documented_steps": 1,
            "as": {"newcomer": "allowed"},
            "tier": "p0",
            "since": "0.6.0",
            "origin": "smoke",
            "match": {
                "docs": ["web-ui.md#upload-documents"],
                "issues": [12],
                "terms": ["alpha", "beta", "gamma"],
            },
        }
    ],
}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _brief() -> str:
    text = _text(OPERATOR_SKILL)
    start = text.index(BRIEF_START) + len(BRIEF_START)
    return text[start : text.index(BRIEF_END)].strip("\n")


def _headings(text: str) -> set[str]:
    body = check_markdown_links.strip_fences(text)
    return {
        line.lstrip("#").strip().lower()
        for line in body.splitlines()
        if line.startswith("### ")
    }


def _fenced_lines(text: str) -> list[str]:
    inside: list[str] = []
    fence: tuple[str, int] | None = None
    for line in text.split("\n"):
        match = check_markdown_links.FENCE.match(line)
        if fence is None:
            if match:
                fence = (match.group(1)[0], len(match.group(1)))
            continue
        char, length = fence
        if (
            match
            and match.group(1)[0] == char
            and len(match.group(1)) >= length
            and not match.group(2).strip()
        ):
            fence = None
            continue
        inside.append(line)
    return inside


def _section(text: str, heading: str) -> str:
    lines = text.split("\n")
    prose = check_markdown_links.strip_fences(text).split("\n")
    begin = prose.index(heading)
    end = next(
        (
            index
            for index in range(begin + 1, len(prose))
            if prose[index].startswith("## ")
        ),
        len(prose),
    )
    return "\n".join(lines[begin:end])


def _plain_words(text: str) -> set[str]:
    return set(SNAKE_WORD.findall(CODE_SPAN.sub("", text)))


def _tool_vocabulary() -> set[str]:
    arguments = {
        name
        for tool in ux_story_mcp.TOOLS
        for name in tool["inputSchema"]["properties"]
    }
    return (
        set(ux_story_mcp.OWN_TOOL_NAMES)
        | set(tool_profiles.STORY_BROWSER_TOOLS)
        | set(ux_story_mcp.BLOCKED_KINDS)
        | arguments
    )


def _cline_table() -> dict[str, str]:
    tree = ast.parse(_text(CLINE_TABLE_SOURCE))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "CLINE_EXEMPT"
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{CLINE_TABLE_SOURCE.name} no longer defines CLINE_EXEMPT")


def _develop_backend_re() -> re.Pattern[str]:
    """``_DEVELOP_BACKEND_RE`` of the MR UX test's suite, compiled from its source.

    That suite runs it over the MR UX test's prompt and note. Reading it rather
    than restating it means a shape of the claim it learns to refuse is refused in
    the story skills too.
    """
    tree = ast.parse(_text(MR_UX_TEST_SUITE))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and [ast.unparse(target) for target in node.targets]
            == ["_DEVELOP_BACKEND_RE"]
            and isinstance(node.value, ast.Call)
            and ast.unparse(node.value.func) == "re.compile"
        ):
            pattern, *flag_nodes = node.value.args
            flags = 0
            for flag_node in flag_nodes:
                for name in ast.unparse(flag_node).split("|"):
                    module, _, flag = name.strip().partition(".")
                    assert module == "re" and flag, name
                    flags |= getattr(re, flag)
            return re.compile(ast.literal_eval(pattern), flags)
    raise AssertionError(
        f"{MR_UX_TEST_SUITE.name} no longer defines _DEVELOP_BACKEND_RE"
    )


def _top_level(path: Path) -> list[ast.stmt]:
    return ast.parse(_text(path), filename=str(path)).body


def _function_source(path: Path, name: str) -> str:
    """The source of a module-level function, read without importing."""
    source = _text(path)
    for node in ast.parse(source, filename=str(path)).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{path.name} no longer defines {name}")


def _assigned_literal(path: Path, name: str) -> object:
    """The value of a module-level ``name = <literal>``, read without importing."""
    for node in _top_level(path):
        if isinstance(node, ast.Assign) and [
            ast.unparse(target) for target in node.targets
        ] == [name]:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path.name} no longer assigns {name}")


def _changelog_points(story: stories.Story, entry_line: str) -> int:
    text = f"## [Unreleased]\n\n### Added\n\n{entry_line}\n"
    entry = changelog_select.parse_entries(text)[0]
    added = [
        changelog_select.AddedEntry(
            entry=entry, modified=False, in_released_section=False
        )
    ]
    score = changelog_select.score_story(
        story, added, edited_sections=(), changed_files=()
    )
    return score.changelog_points


@pytest.mark.unit
def test_both_skills_exist_and_neither_holds_a_twelve_digit_run() -> None:
    for path in (OPERATOR_SKILL, CI_CONTRACT):
        assert path.is_file(), path
        assert not TWELVE_DIGITS.search(_text(path)), path.name


@pytest.mark.unit
@pytest.mark.parametrize("skill", [OPERATOR_SKILL, CI_CONTRACT], ids=lambda p: p.name)
def test_neither_skill_says_the_review_stack_runs_develops_backend(skill: Path) -> None:
    """The review stack runs whatever it was last deployed with, and a story reply
    names its backend by the version and build the UI showed. The CI contract is
    inlined whole into every story prompt and the brief is part of the operator
    skill, so neither may say the backend is develop's, in any shape the MR UX
    test's suite refuses in its own prompt and note."""
    claim = _develop_backend_re().search(_text(skill))
    assert claim is None, f"{skill.name} says the backend is develop's: {claim[0]!r}"


@pytest.mark.unit
def test_the_operator_skill_holds_story_videos_to_the_mr_ux_tests_stack_check() -> None:
    """A video's frames are not redacted, so the MR UX test renders one only of the
    review stack, in code, and a story run's video path calls the same check, which
    both take from ``session.py``. The operator skill's bounds name that check, the
    three statements of the stack it compares, the stack as that code spells it and
    the reason a page that shows no stack name gives, so a renamed check or stack
    or a reworded reason fails here, and so does a bound left to the operator's
    choice of stack alone."""
    check = _function_source(REVIEW_STACK_CHECK, "review_stack_problem")
    unconfirmed = "could not be confirmed as the review stack"
    assert unconfirmed in check, "the page's reason no longer says it is unconfirmed"
    review_stack = _assigned_literal(REVIEW_STACK_CHECK, "REVIEW_STACK")
    assert isinstance(review_stack, str) and review_stack, review_stack
    bounds = _flat(_section(_text(OPERATOR_SKILL), BOUNDS_HEADING))
    for phrase in (
        "`review_stack_problem`",
        "`--stack`",
        "the settings parameter `UX_TEST_UI_ENV` names",
        "the stack the signed-in page reports",
        f"all say `{review_stack}`",
        "the video was withheld",
        unconfirmed,
    ):
        assert phrase in bounds, phrase


@pytest.mark.unit
def test_the_ci_contract_states_only_how_a_story_run_differs() -> None:
    contract = _text(CI_CONTRACT)
    operator = _text(OPERATOR_SKILL)
    assert "ux-test-ci.md" in contract
    assert "ux-test-stories.md" in contract
    assert _headings(contract)
    assert not _headings(contract) & _headings(operator)
    assert not _headings(contract) & _headings(_text(MR_UX_CONTRACT))
    assert len(contract) < len(operator)
    for word in (
        "untrusted",
        "note_injection",
        "look",
        "report_done",
        "report_blocked",
        "stopped_at_commit_point",
    ):
        assert word in contract, word


@pytest.mark.unit
def test_the_ci_contract_names_only_tools_arguments_and_kinds_the_server_has() -> None:
    unknown = sorted(_plain_words(_text(CI_CONTRACT)) - _tool_vocabulary())
    assert not unknown, f"ux-test-stories-ci.md names {unknown}, which no tool has"


@pytest.mark.unit
def test_the_ci_contracts_numbers_are_the_relays_and_the_servers() -> None:
    contract = _flat(_text(CI_CONTRACT))
    assert f"at most {story_relay.IMAGE_CAP} images" in contract
    assert f"at most {story_relay.SNAPSHOT_NODES} elements" in contract
    assert story_relay.LOOKS_PER_ACTION == 2
    assert "free up to twice that number" in contract
    assert f"after {story_relay.REFUSAL_LIMIT} refused calls" in contract
    assert f"The first {ux_story_mcp.INJECTIONS_RECORDED} attempts" in contract
    assert story_relay.OUT_OF_STEPS.startswith("Out of steps")
    assert '"Out of steps"' in contract
    quoted = "This would change something on a shared system"
    assert story_relay.COMMIT_POINT_TEXT.startswith(quoted)
    assert f'"{quoted}"' in contract


@pytest.mark.unit
def test_the_brief_sits_between_its_markers_once() -> None:
    text = _text(OPERATOR_SKILL)
    lines = text.split("\n")
    assert text.count(BRIEF_START) == 1
    assert text.count(BRIEF_END) == 1
    assert lines.index(BRIEF_START) < lines.index(BRIEF_END)
    assert _brief().strip()
    assert "<!--" not in _brief()


@pytest.mark.unit
def test_the_brief_names_no_command_path_or_link() -> None:
    brief = _brief().lower()
    for fragment in ("make ", "scripts/", "docker", "docs/", "](", "://", "`"):
        assert fragment not in brief, fragment
    assert not TWELVE_DIGITS.search(brief)


@pytest.mark.unit
def test_the_brief_never_states_an_expectation() -> None:
    brief = _brief().lower()
    for word in (
        "expectation",
        "not_offered",
        "not offered",
        "allowed",
        "refused",
        "permission",
    ):
        assert word not in brief, word


@pytest.mark.unit
def test_the_brief_names_only_tools_and_kinds_the_server_has() -> None:
    words = _plain_words(_brief())
    assert {"note_confusion", "note_dead_end", "report_done", "report_blocked"} <= words
    unknown = sorted(words - _tool_vocabulary())
    assert not unknown, f"the brief names {unknown}, which no tool has"


@pytest.mark.unit
def test_the_operator_skill_carries_the_provenance_line() -> None:
    assert _flat(_text(OPERATOR_SKILL)).count(PROVENANCE_LINE) == 1
    assert PROVENANCE_LINE not in _flat(_brief())


@pytest.mark.unit
def test_the_session_reads_only_the_summary_table() -> None:
    section = _section(_text(OPERATOR_SKILL), SESSION_HEADING)
    commands = _fenced_lines(section)
    assert any(
        line.strip() == 'make ai-ux-test-stories-judge RUN="$RUN" CHECK=1 SUMMARY=1'
        for line in commands
    ), commands
    for line in commands:
        for name in FREE_TEXT_FILES:
            assert name not in line, line
        if "stories-session.log" in line:
            assert line.strip().startswith(
                ("> scratch/ux-docker/stories-session.log", "grep -E '^(FAILED")
            ), line
    prose = check_markdown_links.strip_fences(section)
    assert "Do not read" in prose
    for name in ("results.json", "section.md", "report.md", "journals", "screens"):
        assert name in prose, name


@pytest.mark.unit
def test_no_command_in_the_operator_skill_reads_a_runs_free_text() -> None:
    reader = re.compile(
        r"\b(?:cat|less|more|head|tail|jq|bat|type|Get-Content)\b[^|\n]*"
        r"(?:results\.json|section\.md|report\.md|journal\.jsonl)"
    )
    offending = [
        line for line in _fenced_lines(_text(OPERATOR_SKILL)) if reader.search(line)
    ]
    assert not offending, offending


@pytest.mark.unit
def test_secrets_are_tested_for_presence_and_never_printed() -> None:
    lines = _fenced_lines(_text(OPERATOR_SKILL))
    for line in lines:
        assert "printenv" not in line, line
        assert not re.search(r"\benv\s*\|", line), line
    for name in SECRET_NAMES:
        presence = re.compile(rf'\[ -n "\$\{{{name}:-\}}" \]')
        assignment = re.compile(rf"^\s*\$env:{name} = ")
        for line in lines:
            if re.search(rf"\$\{{?{name}\b|\$env:{name}\b", line):
                assert presence.search(line) or assignment.match(line), line
    for name in ("UX_TEST_PERSONAS", "UX_TEST_AWS_ACCOUNT_ID"):
        assert any(f'[ -n "${{{name}:-}}" ]' in line for line in lines), name
    assert "Read-Host -MaskInput" in _text(OPERATOR_SKILL)
    assert "read -rs UX_TEST_PERSONAS" in _text(OPERATOR_SKILL)


@pytest.mark.unit
@pytest.mark.parametrize("heading", RUN_MODE_HEADINGS)
def test_every_run_mode_has_a_make_form_and_a_windows_form(heading: str) -> None:
    section = _section(_text(OPERATOR_SKILL), heading)
    assert "make ai-ux-test-stories-" in section, heading
    assert "py scripts\\ux_runner\\run_in_docker.py stories" in section, heading


@pytest.mark.unit
def test_the_operator_skills_numbers_are_the_catalogues_and_the_selectors() -> None:
    skill = _flat(_text(OPERATOR_SKILL))
    catalogue = stories.load_catalogue()
    limits = catalogue.limits
    assert f"`documented_steps_max` ({limits.documented_steps_max})" in skill
    assert limits.step_budget_multiple == 10
    assert "ten actions a documented step" in skill
    assert f"never fewer than {limits.step_budget_floor}" in skill
    assert story_relay.LOOKS_PER_ACTION == 2
    assert "free up to twice the budget" in skill
    assert f"({changelog_select.TERM_POINTS_MAX} at most)" in skill
    assert f"selected at {changelog_select.SELECT_THRESHOLD} CHANGELOG points" in skill


@pytest.mark.unit
def test_the_selector_points_the_operator_skill_states_are_the_selectors() -> None:
    story = stories.parse_catalogue(FIXTURE_CATALOGUE).stories[0]
    section = "- **Section.** See [it](docs/web-ui.md#upload-documents)."
    page = "- **Page.** See [it](docs/web-ui.md#document-list)."
    issue = "- **Issue.** Fixed (#12)."
    terms = "- **Terms.** Alpha, beta and gamma all changed."
    one_term = "- **Term.** Only gamma changed."
    assert _changelog_points(story, section) == 3
    assert _changelog_points(story, page) == 1
    assert _changelog_points(story, issue) == 2
    assert _changelog_points(story, terms) == changelog_select.TERM_POINTS_MAX == 2
    assert _changelog_points(story, one_term) == 1
    assert (
        "scores 3 for a link to a declared section, 2 for a shared issue, and 1 for "
        "each declared term it contains (2 at most); links to other sections of the "
        "declared pages add 1 point to a story however many entries carry them"
        in _flat(_text(OPERATOR_SKILL))
    )


@pytest.mark.unit
def test_every_link_and_anchor_in_both_skills_resolves() -> None:
    for skill in (OPERATOR_SKILL, CI_CONTRACT):
        text = _text(skill)
        relative = [
            link.target
            for link in check_markdown_links.iter_links(text)
            if check_markdown_links.is_relative(link.target)
        ]
        assert relative, skill.name
        for target in relative:
            path, _, fragment = target.partition("#")
            resolved = (skill.parent / path).resolve() if path else skill
            assert resolved.exists(), f"{skill.name}: {target}"
            if fragment and resolved.suffix == ".md":
                assert fragment in check_markdown_links.anchors(_text(resolved)), (
                    f"{skill.name}: {target}"
                )


@pytest.mark.unit
def test_every_make_target_and_script_the_skills_cite_exists() -> None:
    targets = set(re.findall(r"^([a-zA-Z0-9_.-]+):", _text(MAKEFILE), re.M))
    for skill in (OPERATOR_SKILL, CI_CONTRACT):
        text = _text(skill)
        missing_targets = sorted(set(MAKE_TARGET.findall(text)) - targets)
        assert not missing_targets, f"{skill.name} cites {missing_targets}"
        missing_scripts = sorted(
            {
                path.replace("\\", "/")
                for path in SCRIPT_PATH.findall(text)
                if not (REPO_ROOT / path.replace("\\", "/")).is_file()
            }
        )
        assert not missing_scripts, f"{skill.name} cites {missing_scripts}"


@pytest.mark.unit
def test_the_ci_contract_has_a_cline_symlink() -> None:
    entry = CLINE_SKILLS / "ux-test-stories-ci.md"
    assert entry.is_symlink()
    assert os.readlink(entry) == "../../.claude/skills/ux-test-stories-ci.md"
    assert entry.resolve() == CI_CONTRACT.resolve()


@pytest.mark.unit
def test_the_operator_skill_is_claude_code_only_and_says_why() -> None:
    linked = {
        Path(os.path.realpath(entry)).name
        for entry in CLINE_SKILLS.iterdir()
        if entry.name.endswith(".md")
    }
    assert OPERATOR_SKILL.name not in linked
    reason = _cline_table().get(OPERATOR_SKILL.name, "")
    assert "--mcp-config" in reason, reason
    assert "run_in_background" in reason, reason


@pytest.mark.unit
def test_both_skills_are_registered_in_claude_md() -> None:
    claude_md = _text(REPO_ROOT / "CLAUDE.md")
    for path in (OPERATOR_SKILL, CI_CONTRACT):
        assert f".claude/skills/{path.name}" in claude_md, path.name
