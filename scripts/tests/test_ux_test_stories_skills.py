# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the two first-time-user story skills and their Cline entries."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

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
OPERATOR_SKILL = SKILLS / "ux-test-stories" / "SKILL.md"
CI_CONTRACT = SKILLS / "ux-test-stories-ci" / "SKILL.md"
MR_UX_CONTRACT = SKILLS / "ux-test-ci" / "SKILL.md"
MR_UX_TEST_SUITE = SCRIPTS / "tests" / "test_ai_ux_test.py"
MAKEFILE = REPO_ROOT / "Makefile"
BRIEF_START = "<!-- brief:start -->"
BRIEF_END = "<!-- brief:end -->"
SECRET_NAMES: tuple[str, ...] = (
    "UX_TEST_PERSONAS",
    "UX_TEST_AWS_ACCOUNT_ID",
    "UX_TEST_UI_ENV",
    "GITLAB_REVIEW_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)
TWELVE_DIGITS = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")
SNAKE_WORD = re.compile(r"\b[a-z]+(?:_[a-z]+)+\b")
CODE_SPAN = re.compile(r"(`+)[^\n]*?\1")
MAKE_TARGET = re.compile(r"\bmake (ai-ux-[a-z0-9-]+)")
SCRIPT_PATH = re.compile(r"\b(scripts[\\/][A-Za-z0-9_.\\/-]+\.py)\b")


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
def test_the_ci_contract_states_only_how_a_story_run_differs() -> None:
    """It defers to both skills it differs from, restates none of their sections,
    is shorter than the operator skill, and names the tools a run ends with and
    the one it reports a page's instructions through."""
    contract = _text(CI_CONTRACT)
    assert "ux-test-ci.md" in contract
    assert "ux-test-stories.md" in contract
    assert _headings(contract)
    assert not _headings(contract) & _headings(_text(OPERATOR_SKILL))
    assert not _headings(contract) & _headings(_text(MR_UX_CONTRACT))
    assert len(contract) < len(_text(OPERATOR_SKILL))
    injection = [n for n in ux_story_mcp.OWN_TOOL_NAMES if "injection" in n]
    reports = [n for n in ux_story_mcp.OWN_TOOL_NAMES if n.startswith("report_")]
    assert injection and reports, ux_story_mcp.OWN_TOOL_NAMES
    for name in (*reports, *injection):
        assert name in _plain_words(contract), name


@pytest.mark.unit
def test_the_ci_contract_names_only_tools_arguments_and_kinds_the_server_has() -> None:
    unknown = sorted(_plain_words(_text(CI_CONTRACT)) - _tool_vocabulary())
    assert not unknown, f"ux-test-stories-ci.md names {unknown}, which no tool has"


@pytest.mark.unit
def test_the_ci_contracts_numbers_are_the_relays_and_the_servers() -> None:
    """The contract is inlined into every story prompt, so a figure or a message it
    quotes has to be the one the relay and the server actually apply."""
    contract = _flat(_text(CI_CONTRACT))
    assert f"at most {story_relay.IMAGE_CAP} images" in contract
    assert f"at most {story_relay.SNAPSHOT_NODES} elements" in contract
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
def test_the_session_reads_only_the_summary_table() -> None:
    """The judge command the skill gives a session prints the summary table and
    writes nothing, through options the Makefile target really reads."""
    judged = [
        line
        for line in _fenced_lines(_text(OPERATOR_SKILL))
        if "make ai-ux-test-stories-judge" in line
    ]
    assert any("SUMMARY=1" in line and "CHECK=1" in line for line in judged), judged
    recipe = _text(MAKEFILE).split("\nai-ux-test-stories-judge:", 1)[1]
    recipe = recipe.split("\n\n", 1)[0]
    for variable in ("SUMMARY", "CHECK"):
        assert f"$({variable})" in recipe, variable


@pytest.mark.unit
def test_no_command_in_the_operator_skill_reads_a_runs_free_text() -> None:
    reader = re.compile(
        r"\b(?:cat|less|more|head|tail|jq|bat|type|Get-Content)\b[^|\n]*"
        r"(?:results\.json|section\.md|report\.html|report\.md|journal\.jsonl)"
    )
    offending = [
        line for line in _fenced_lines(_text(OPERATOR_SKILL)) if reader.search(line)
    ]
    assert not offending, offending


@pytest.mark.unit
def test_secrets_are_tested_for_presence_and_never_printed() -> None:
    """Every command the skill gives that touches a secret tests it for presence
    or assigns it, and an interactive read of one is masked."""
    lines = _fenced_lines(_text(OPERATOR_SKILL))
    for line in lines:
        assert "printenv" not in line, line
        assert not re.search(r"\benv\s*\|", line), line
        if "Read-Host" in line:
            assert "-MaskInput" in line or "-AsSecureString" in line, line
    for name in SECRET_NAMES:
        presence = re.compile(rf'\[ -n "\$\{{{name}:-\}}" \]')
        assignment = re.compile(rf"^\s*\$env:{name} = (?:Read-Host |'<)")
        shell_read = re.compile(rf"\bread\b(?P<flags>(?:\s+-\w+)*)\s+{name}\b")
        for line in lines:
            if re.search(rf"\$\{{?{name}\b|\$env:{name}\b", line):
                assert presence.search(line) or assignment.match(line), line
            read = shell_read.search(line)
            if read:
                assert "s" in read["flags"], line


@pytest.mark.unit
def test_the_operator_skills_numbers_are_the_catalogues_and_the_selectors() -> None:
    skill = _flat(_text(OPERATOR_SKILL))
    limits = stories.load_catalogue().limits
    assert f"`documented_steps_max` ({limits.documented_steps_max})" in skill
    assert f"never fewer than {limits.step_budget_floor}" in skill
    assert f"({changelog_select.TERM_POINTS_MAX} at most)" in skill
    assert f"selected at {changelog_select.SELECT_THRESHOLD} CHANGELOG points" in skill


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
def test_both_skills_are_registered_in_claude_md() -> None:
    claude_md = _text(REPO_ROOT / "CLAUDE.md")
    for path in (OPERATOR_SKILL, CI_CONTRACT):
        relative = path.relative_to(REPO_ROOT).as_posix()
        assert relative in claude_md, relative
