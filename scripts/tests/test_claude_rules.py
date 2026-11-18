# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Hold the rule files in ``.claude/rules/`` to loading for files that exist.

A rule with a ``paths:`` list loads only while Claude works on a file one of its globs
matches. A glob that matches nothing — a file renamed or moved out from under it —
turns the rule off for that surface with every other gate green, and a rule with no
row in ``.claude/rules/README.md`` is one nobody looking for it there will find. So:

* every glob in every rule matches at least one file in the repository;
* a rule's front matter holds a ``paths:`` list of strings and nothing else; and
* the README's table and the directory name the same rule files, in both directions.

Rule files are read at any depth under ``.claude/rules/``, because Claude Code loads
them recursively, and a table row names a file by its path relative to that directory.
The files a glob may match are the ones git reports, committed or not yet committed,
that still exist on disk, so the verdict does not change at ``git add`` time.
``.claude/`` is not pruned as local work here, because a rule may name files under it.

The globs are read the way the rules use them: ``**/`` spans any number of
directories including none, a trailing ``/**`` everything beneath, and ``*`` stays
inside one path segment. A glob using any other syntax (braces, brackets, ``?``, a
leading ``!``, or ``**`` anywhere else) is refused as unsupported here rather than
reported as matching nothing, because this matcher would read it literally.

That reading is an assumption about how Claude Code matches ``paths:``, written down
from the shapes the rules use and pinned only by this file's own table: nothing in
the tree runs the loader's matcher against it. A glob this test accepts could still
be read differently by Claude Code, so a new glob shape is worth checking against
the loader's documentation before relying on it.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

import gate_premises
import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
RULES = REPO_ROOT / ".claude" / "rules"
README = RULES / "README.md"
UNSUPPORTED = re.compile(r"[{}\[\]?]")
ROW = re.compile(r"^\| \[(?P<name>[^\]]+\.md)\]\((?P<target>[^)]+)\)", re.M)


def _rule_files() -> list[Path]:
    return sorted(path for path in RULES.rglob("*.md") if path != README)


def _name(rule: Path) -> str:
    return rule.relative_to(RULES).as_posix()


def _front_matter(rule: Path) -> dict | None:
    """The YAML between a leading ``---`` line and the next one; None if absent."""
    text = rule.read_text("utf-8")
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    assert end != -1, f"{_name(rule)}: front matter opens with --- and never closes"
    return yaml.safe_load(text[4:end]) or {}


@functools.cache
def _repository_files() -> tuple[str, ...]:
    return tuple(
        path
        for path in gate_premises.tracked_files(
            include_untracked=True, prune_local_work=False
        )
        if (REPO_ROOT / path).is_file()
    )


def _unsupported(glob: str) -> bool:
    if UNSUPPORTED.search(glob) or glob.startswith("!"):
        return True
    rest = glob.replace("**/", "")
    rest = rest[: -len("/**")] if rest.endswith("/**") else rest
    return "**" in rest


def _glob_regex(glob: str) -> re.Pattern[str]:
    parts: list[str] = []
    index = 0
    while index < len(glob):
        if glob.startswith("**/", index):
            parts.append("(?:[^/]+/)*")
            index += 3
        elif glob.startswith("/**", index) and index + 3 == len(glob):
            parts.append("/.+")
            index += 3
        elif glob[index] == "*":
            parts.append("[^/]*")
            index += 1
        else:
            parts.append(re.escape(glob[index]))
            index += 1
    return re.compile("".join(parts) + r"\Z")


def _glob_problems(
    globs: list[str], files: tuple[str, ...]
) -> tuple[list[str], list[str]]:
    """The globs this test cannot read, and the readable ones that match no file."""
    unsupported = [glob for glob in globs if _unsupported(glob)]
    dead = [
        glob
        for glob in globs
        if glob not in unsupported
        and not any(_glob_regex(glob).match(path) for path in files)
    ]
    return unsupported, dead


@pytest.mark.parametrize(
    ("glob", "path", "matches"),
    [
        ("**/README.md", "README.md", True),
        ("**/README.md", "lib/idp_sdk/README.md", True),
        ("src/lambda/**", "src/lambda/x/index.py", True),
        ("src/lambda/**", "src/lambdas/x.py", False),
        ("src/lambda/**", "src/lambda", False),
        ("src/lambda/*agent*/**", "src/lambda/agent_chat_processor/index.py", True),
        ("src/lambda/*agent*/**", "src/lambda/other/agent/index.py", False),
        ("lib/**/*.py", "lib/a.py", True),
        ("lib/**/*.py", "lib/pkg/sub/a.py", True),
        ("lib/**/*.py", "lib/pkg/a.pyc", False),
        ("docs/quick-start.md", "docs/quick-start.md", True),
        ("docs/quick-start.md", "docs/quick-start.mdx", False),
    ],
)
def test_globs_are_read_the_way_the_rules_use_them(
    glob: str, path: str, matches: bool
) -> None:
    assert bool(_glob_regex(glob).match(path)) is matches


@pytest.mark.parametrize(
    ("glob", "unsupported"),
    [
        ("src/ui/src/**/*.{ts,tsx}", True),
        ("src/[ab]/index.py", True),
        ("src/?.py", True),
        ("!src/ui/**", True),
        ("src/**agent**/index.py", True),
        ("**", True),
        ("src/ui/**", False),
        ("**/test_*.py", False),
        ("lib/**/*.py", False),
    ],
)
def test_syntax_this_matcher_does_not_read_is_recognised(
    glob: str, unsupported: bool
) -> None:
    assert _unsupported(glob) is unsupported


def test_a_glob_is_refused_as_unreadable_or_as_dead_but_never_both() -> None:
    files = ("docs/quick-start.md", "src/ui/src/App.tsx")
    globs = ["docs/*.md", "src/ui/**/*.{ts,tsx}", "nowhere/**", "!docs/**"]
    assert _glob_problems(globs, files) == (
        ["src/ui/**/*.{ts,tsx}", "!docs/**"],
        ["nowhere/**"],
    )


def test_the_rules_directory_holds_rule_files_and_their_table() -> None:
    assert README.is_file(), f"{README} is missing"
    assert _rule_files(), f"no rule files under {RULES}"


@pytest.mark.parametrize("rule", _rule_files(), ids=_name)
def test_a_rules_front_matter_is_a_list_of_paths_and_nothing_else(rule: Path) -> None:
    meta = _front_matter(rule)
    if meta is None:
        return
    assert set(meta) == {"paths"}, f"{_name(rule)}: front matter keys {sorted(meta)}"
    globs = meta["paths"]
    assert isinstance(globs, list) and globs, f"{_name(rule)}: paths is not a list"
    assert all(isinstance(glob, str) and glob for glob in globs), _name(rule)


@pytest.mark.parametrize("rule", _rule_files(), ids=_name)
def test_every_glob_in_a_rule_matches_a_file(rule: Path) -> None:
    globs = (_front_matter(rule) or {}).get("paths", [])
    unsupported, dead = _glob_problems(globs, _repository_files())
    assert not unsupported, (
        f"{_name(rule)}: {unsupported} use glob syntax this test does not read; "
        "extend _glob_regex before relying on it"
    )
    assert not dead, (
        f"{_name(rule)}: {dead} match no file in the repository, so the rule never "
        "loads for them. Point each at the file or tree it was written for, or "
        "remove it."
    )


def test_the_readme_lists_every_rule_file_and_no_other() -> None:
    rows = {
        match["name"]: match["target"] for match in ROW.finditer(README.read_text())
    }
    assert all(name == target for name, target in rows.items()), rows
    on_disk = {_name(path) for path in _rule_files()}
    assert set(rows) == on_disk, (
        f"listed but missing: {sorted(set(rows) - on_disk)}; "
        f"present but not listed: {sorted(on_disk - set(rows))}"
    )
