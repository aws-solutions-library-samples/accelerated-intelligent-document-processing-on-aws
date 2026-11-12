# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``docs-site/add-frontmatter.sh`` recognises frontmatter the way the docs gates do.

The script prepends a ``title:`` frontmatter block to every ``docs/*.md`` page that
does not already have one. It used to decide that from line 1 alone, so a page
opening with a blank line before its ``---`` (``docs/quick-start.md`` is one) was
given a second block. The rule it must agree with is the one
``scripts/tests/test_docs_license_header.py`` applies to the same pages: strip a
byte-order mark, strip leading whitespace, and the page has frontmatter when the
first remaining line is ``---``.

The script is run for real, copied into a throwaway tree, because it resolves
``docs/`` relative to its own location.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "docs-site/add-frontmatter.sh"


def _reference_has_frontmatter(text: str) -> bool:
    """The rule ``test_docs_license_header.header_problem`` uses."""
    lines = text.lstrip("\ufeff").lstrip().splitlines()
    return bool(lines) and lines[0].rstrip() == "---"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "docs-site").mkdir()
    (tmp_path / "docs").mkdir()
    shutil.copy2(SCRIPT, tmp_path / "docs-site/add-frontmatter.sh")
    return tmp_path


def _run(tree: Path) -> None:
    subprocess.run(
        ["bash", str(tree / "docs-site/add-frontmatter.sh")],
        check=True,
        capture_output=True,
    )


CASES = {
    "frontmatter-on-line-1": "---\ntitle: A\n---\n\n# A\n",
    "leading-blank-line": "\n---\ntitle: A\n---\n\n# A\n",
    "leading-blank-lines-with-spaces": "  \n\t\n---\ntitle: A\n---\n# A\n",
    "byte-order-mark": "\ufeff---\ntitle: A\n---\n# A\n",
    "byte-order-mark-then-blank-line": "\ufeff\n---\ntitle: A\n---\n# A\n",
    "crlf-line-endings": "\r\n---\r\ntitle: A\r\n---\r\n# A\r\n",
    "trailing-space-on-fence": "---  \ntitle: A\n---\n# A\n",
    # The reference strips leading whitespace before splitting, so an indented
    # opening fence counts too; the script has to agree rather than double it.
    "indented-fence-after-blank": "\n  ---\ntitle: A\n---\n# A\n",
    # The deciding line is the last one and has no newline, so ``read`` returns
    # non-zero on it; the loop must still look at it.
    "fence-without-trailing-newline": "\n---",
    # A BOM is stripped from the start of the file only, as in the reference.
    "byte-order-mark-not-on-line-1": "\n\ufeff---\ntitle: A\n---\n# A\n",
    "no-frontmatter": "# A Title\n\nBody.\n",
    "no-frontmatter-leading-blank": "\n\n# A Title\n\nBody.\n",
    "horizontal-rule-later": "# A Title\n\n---\n\nBody.\n",
    "empty-file": "",
    "blank-only-file": "\n\n",
}


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(CASES))
def test_script_agrees_with_the_license_header_rule(tree: Path, name: str) -> None:
    text = CASES[name]
    page = tree / "docs/page.md"
    page.write_bytes(text.encode("utf-8"))
    _run(tree)
    after = page.read_bytes().decode("utf-8")
    if _reference_has_frontmatter(text):
        assert after == text, f"{name}: a page that has frontmatter was rewritten"
    else:
        assert after.startswith('---\ntitle: "'), after
        assert after.endswith(text), f"{name}: original content not preserved"
        assert _reference_has_frontmatter(after)


@pytest.mark.unit
def test_running_over_the_real_docs_changes_nothing(tree: Path) -> None:
    """Every published page has frontmatter, so the script must be a no-op on them.

    The pages are the authority here, copied from this checkout, so a page that
    opens in a shape the script misreads fails this whether or not a case above
    anticipated it.
    """
    pages = sorted((REPO_ROOT / "docs").glob("*.md"))
    assert pages
    for page in pages:
        shutil.copy2(page, tree / "docs" / page.name)
    _run(tree)
    changed = [
        page.name
        for page in pages
        if (tree / "docs" / page.name).read_bytes() != page.read_bytes()
    ]
    assert not changed, (
        f"add-frontmatter.sh rewrote pages that have frontmatter: {changed}"
    )
