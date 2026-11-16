# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Hold the rule files in ``.claude/rules/`` to loading for files that exist.

A rule with a ``paths:`` list loads only while Claude works on a file one of its globs
matches. A glob that matches nothing — a file renamed or moved out from under it —
turns the rule off for that surface with every other gate green, and a rule with no
row in ``.claude/rules/README.md`` is one nobody looking for it there will find. So:

* every glob in every rule matches at least one tracked file;
* a rule's front matter holds a ``paths:`` list of strings and nothing else; and
* the README's table and the directory name the same rule files, in both directions.

Rule files are read at any depth under ``.claude/rules/``, because Claude Code loads
them recursively, and a table row names a file by its path relative to that directory.

The globs are read the way the rules use them: ``**/`` spans any number of
directories including none, a trailing ``/**`` everything beneath, and ``*`` stays
inside one path segment. A glob using any other syntax (braces, brackets, ``?``)
is refused as unsupported here rather than reported as matching nothing, because
this matcher would read it literally.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

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


def _front_matter(text: str) -> dict | None:
    """The YAML between a leading ``---`` line and the next one; None if absent."""
    if not text.startswith("---\n"):
        return None
    end = text.index("\n---\n", 4)
    return yaml.safe_load(text[4:end]) or {}


def _tracked() -> list[str]:
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    return [name for name in listing.stdout.decode().split("\0") if name]


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
        ("src/ui/**", False),
        ("**/test_*.py", False),
    ],
)
def test_syntax_this_matcher_does_not_read_is_recognised(
    glob: str, unsupported: bool
) -> None:
    assert bool(UNSUPPORTED.search(glob)) is unsupported


@pytest.mark.parametrize("rule", _rule_files(), ids=_name)
def test_a_rules_front_matter_is_a_list_of_paths_and_nothing_else(rule: Path) -> None:
    meta = _front_matter(rule.read_text("utf-8"))
    if meta is None:
        return
    assert set(meta) == {"paths"}, f"{_name(rule)}: front matter keys {sorted(meta)}"
    globs = meta["paths"]
    assert isinstance(globs, list) and globs, f"{_name(rule)}: paths is not a list"
    assert all(isinstance(glob, str) and glob for glob in globs), _name(rule)


@pytest.mark.parametrize("rule", _rule_files(), ids=_name)
def test_every_glob_in_a_rule_matches_a_tracked_file(rule: Path) -> None:
    meta = _front_matter(rule.read_text("utf-8")) or {}
    tracked = _tracked()
    globs = meta.get("paths", [])
    unsupported = [glob for glob in globs if UNSUPPORTED.search(glob)]
    assert not unsupported, (
        f"{_name(rule)}: {unsupported} use glob syntax this test does not read; "
        "extend _glob_regex before relying on it"
    )
    dead = [
        glob
        for glob in globs
        if not any(_glob_regex(glob).match(path) for path in tracked)
    ]
    assert not dead, (
        f"{_name(rule)}: {dead} match no tracked file, so the rule never loads for "
        "them. Point each at the file or tree it was written for, or remove it."
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
