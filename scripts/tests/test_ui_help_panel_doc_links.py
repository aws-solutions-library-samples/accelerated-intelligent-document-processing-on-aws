# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every docs-site link in the web UI names a published page, and its anchor exists.

The UI's help panels link to the published docs site as
``${DOCS_BASE_URL}/<page>/#<anchor>``. Those are absolute URLs, so
``make check-markdown-links`` never sees them, and a renamed page or reworded
heading leaves a link that 404s or lands at the top of the page with every gate
green. This resolves each one against ``docs/`` with the link checker's own
slugifier and its derived set of published pages, so the answer is the one the
site gives.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import check_markdown_links as links  # noqa: E402

DOCS_LINK = re.compile(r"\$\{DOCS_BASE_URL\}/([a-z0-9-]+)/(?:#([A-Za-z0-9_-]+))?")


def _ui_sources() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "src/ui/src"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [
        REPO_ROOT / p
        for p in out
        if p.endswith((".ts", ".tsx")) and "__tests__" not in p and ".test." not in p
    ]


def _ui_doc_links() -> list[tuple[str, str, str | None]]:
    found = []
    for path in _ui_sources():
        for page, anchor in DOCS_LINK.findall(path.read_text("utf-8")):
            found.append((str(path.relative_to(REPO_ROOT)), page, anchor or None))
    return found


LINKS = _ui_doc_links()


def test_the_scan_finds_the_help_panel_links():
    # Non-vacuity: the prefix-mappings panel alone carries anchored links.
    assert any(a for _, _, a in LINKS), LINKS
    assert any(
        f.endswith("config-prefix-mappings/tools-panel.tsx") for f, _, _ in LINKS
    )


@pytest.fixture(scope="module")
def published():
    return links.published_pages(REPO_ROOT)


@pytest.mark.parametrize(
    "source,page,anchor", LINKS, ids=[f"{s}->{p}#{a or ''}" for s, p, a in LINKS]
)
def test_each_link_resolves_on_the_site(source, page, anchor, published):
    target = f"docs/{page}.md"
    assert (REPO_ROOT / target).is_file(), (
        f"{source} links to {page}, which has no {target}"
    )
    assert target in published, (
        f"{source} links to {target}, which the docs site does not publish"
    )
    if anchor:
        assert anchor in links.anchors((REPO_ROOT / target).read_text("utf-8")), (
            f"{source} links to {target}#{anchor}, which is not a heading there"
        )
