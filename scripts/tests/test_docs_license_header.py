# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every page under ``docs/`` carries the MIT-0 licence header where the rule puts it.

``.claude/skills/documentation.md`` requires each documentation page to open with
its YAML frontmatter, then the copyright line and the SPDX line, then the H1. The
per-page doc tests (``test_testing_doc.py``, ``test_well_architected_doc.py`` and
others) checked that for the page each one owns, and nothing checked it for the
rest, so pages were added without the header and others carried it after the H1
or as an HTML comment (issue #1891).

The page set is derived, never listed: ``git ls-files --cached --others
--exclude-standard -- 'docs/*.md'``. A git pathspec ``*`` matches ``/``, so that
reaches every Markdown file at any depth under ``docs/`` — published or not — and
the ``--others`` half means a page counts the moment it is written, not when it is
staged. There is no exemption list.

What "carries the header" means here, exactly:

* a leading byte-order mark and leading whitespace are ignored (``quick-start.md``
  opens with a newline);
* if the page opens with a ``---`` line, everything through the next ``---`` line
  is frontmatter and is skipped; a page without frontmatter is checked from its
  first line;
* blank lines are then skipped, and the next two lines must be, verbatim, the two
  lines in ``LICENSE_LINES``.

Bounds, so a green run is not over-read: this does not check that the frontmatter
has a ``title:``, that a heading follows, or what comes after the two lines.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

LICENSE_LINES = (
    "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.",
    "SPDX-License-Identifier: MIT-0",
)


def header_problem(text: str) -> str | None:
    """Return why ``text`` lacks the licence header in place, or None if it has it."""
    lines = text.lstrip("\ufeff").lstrip().splitlines()
    start = 0
    if lines and lines[0].rstrip() == "---":
        closing = next(
            (i for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None
        )
        if closing is None:
            return "frontmatter opened with '---' is never closed"
        start = closing + 1
    while start < len(lines) and not lines[start].strip():
        start += 1
    found = tuple(lines[start : start + 2])
    if found == LICENSE_LINES:
        return None
    if any(line in lines for line in LICENSE_LINES):
        return (
            "licence lines present but not immediately after the frontmatter "
            f"(found {list(found)!r} there)"
        )
    return f"licence lines missing (expected {list(LICENSE_LINES)!r})"


def docs_pages() -> list[str]:
    out = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "docs/*.md",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    # A tracked file deleted in the working tree is not a page any more.
    return sorted({p for p in out.splitlines() if (REPO_ROOT / p).is_file()})


@pytest.mark.unit
def test_the_page_set_is_not_vacuous() -> None:
    pages = docs_pages()
    # One top-level page, one nested published page, one unpublished page: if any
    # of these drops out, the derivation has narrowed, not the docs tree.
    for expected in (
        "docs/testing.md",
        "docs/benchmarking/index.md",
        "docs/planning/retire-granular-assessment-plan.md",
    ):
        assert expected in pages, f"{expected} not in the derived docs page set"


@pytest.mark.unit
def test_every_docs_page_carries_the_licence_header() -> None:
    failures = []
    for page in docs_pages():
        problem = header_problem((REPO_ROOT / page).read_text(encoding="utf-8"))
        if problem:
            failures.append(f"{page}: {problem}")
    assert not failures, (
        "Every docs/ page needs, after its frontmatter and before the H1:\n  "
        + "\n  ".join(LICENSE_LINES)
        + "\n(see .claude/skills/documentation.md). Pages that do not:\n"
        + "\n".join(failures)
    )


HEADER = "\n".join(LICENSE_LINES)


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        f'---\ntitle: "A"\n---\n\n{HEADER}\n\n# A\n',
        f'---\ntitle: "A"\n---\n{HEADER}\n# A\n',
        f'\n---\ntitle: "Quick Start"\n---\n\n{HEADER}\n\n# Quick Start\n',
        f"{HEADER}\n\n# No frontmatter\n",
        f'---\r\ntitle: "A"\r\n---\r\n\r\n{HEADER}\r\n\r\n# A\r\n',
        f'\ufeff---\ntitle: "A"\n---\n\n{HEADER}\n\n# A\n',
        f'--- \ntitle: "A"\n---  \n\n{HEADER}\n\n# A\n',
    ],
    ids=[
        "standard",
        "no-blank-lines",
        "leading-newline",
        "no-frontmatter",
        "crlf",
        "byte-order-mark",
        "fence-trailing-space",
    ],
)
def test_a_header_in_place_is_accepted(text: str) -> None:
    assert header_problem(text) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ('---\ntitle: "A"\n---\n\n# A\n\nBody.\n', "missing"),
        ("# A\n\nBody.\n", "missing"),
        (f'---\ntitle: "A"\n---\n\n# A\n\n{HEADER}\n', "not immediately after"),
        (f'---\ntitle: "A"\n---\n\n{LICENSE_LINES[0]}\n\n# A\n', "not immediately"),
        (f'---\ntitle: "A"\n---\n\n{LICENSE_LINES[1]}\n# A\n', "not immediately"),
        (
            f'---\ntitle: "A"\n---\n\n{LICENSE_LINES[1]}\n{LICENSE_LINES[0]}\n# A\n',
            "not immediately",
        ),
        (
            '---\ntitle: "A"\n---\n\n<!-- Copyright Amazon.com, Inc. or its '
            "affiliates. All Rights Reserved. -->\n"
            "<!-- SPDX-License-Identifier: MIT-0 -->\n\n# A\n",
            "missing",
        ),
        (f'---\ntitle: "A"\n\n{HEADER}\n\n# A\n', "never closed"),
        (f'---\ntitle: "A"\n{HEADER}\n---\n\n# A\n', "not immediately"),
    ],
    ids=[
        "frontmatter-only",
        "bare-page",
        "after-h1",
        "copyright-only",
        "spdx-only",
        "reversed",
        "html-comment",
        "unclosed-frontmatter",
        "inside-frontmatter",
    ],
)
def test_a_header_missing_or_out_of_place_is_refused(text: str, reason: str) -> None:
    problem = header_problem(text)
    assert problem is not None
    assert reason in problem
