# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every docs-site link in the configuration editor's help text names a published
page, and its anchor exists.

The Web UI's configuration editor renders each ``ConfigSchema`` field description
in ``patterns/unified/template.yaml`` as markdown, and several descriptions link to
the published docs site with an absolute URL. ``make check-markdown-links`` reads
``.md`` files only and ``test_ui_help_panel_doc_links.py`` reads UI source only, so
a renamed page or reworded heading behind one of these links 404s or lands at the
top of the page with every gate green. This resolves each one against ``docs/``
with the link checker's own slugifier and its derived set of published pages.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import check_markdown_links as links  # noqa: E402

TEMPLATE = REPO_ROOT / "patterns" / "unified" / "template.yaml"
SITE = "https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws/"
DOCS_LINK = re.compile(re.escape(SITE) + r"([a-z0-9-]+)/(?:#([A-Za-z0-9_-]+))?")


def _template_doc_links() -> list[tuple[int, str, str | None]]:
    found = []
    for lineno, line in enumerate(TEMPLATE.read_text("utf-8").splitlines(), 1):
        for page, anchor in DOCS_LINK.findall(line):
            found.append((lineno, page, anchor or None))
    return found


LINKS = _template_doc_links()


def test_the_scan_finds_the_configuration_help_links():
    # Non-vacuity: the classification settings and the structured-outputs setting
    # both carry anchored links.
    pages = {p for _, p, a in LINKS if a}
    assert {"classification", "extraction-and-confidence"} <= pages, LINKS


@pytest.fixture(scope="module")
def published():
    return links.published_pages(REPO_ROOT)


@pytest.mark.parametrize(
    "lineno,page,anchor", LINKS, ids=[f"L{n}->{p}#{a or ''}" for n, p, a in LINKS]
)
def test_each_link_resolves_on_the_site(lineno, page, anchor, published):
    target = f"docs/{page}.md"
    where = f"patterns/unified/template.yaml:{lineno}"
    assert (REPO_ROOT / target).is_file(), (
        f"{where} links to {page}, which has no {target}"
    )
    assert target in published, (
        f"{where} links to {target}, which the docs site does not publish"
    )
    if anchor:
        assert anchor in links.anchors((REPO_ROOT / target).read_text("utf-8")), (
            f"{where} links to {target}#{anchor}, which is not a heading there"
        )
