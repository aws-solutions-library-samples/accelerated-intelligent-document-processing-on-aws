#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Search and read the published documentation the way a first-time user would.

A standard-library BM25 index over exactly the pages ``docs-site/setup.sh``
publishes, split into sections at H1-H3 headings outside code fences. A story
agent searches and reads what the documentation site serves and nothing else, so
a page the site leaves unpublished can neither help nor mislead it. Section ids are
``page.md#anchor``, with anchors slugged the way the site slugs them, and every
address is the site's own path, so a help link copied off a screen resolves here
when the site serves it. A slug in the wrong case is a miss, as it is a 404 there.
The site's typography sets two hyphens outside code as a dash, which its slugger
drops where GitHub's keeps the hyphens; no published heading holds them, which the
suite checks, so both slug every heading alike.

Everything that decides a result is fixed: the tokenizer (NFKC, casefold, runs of
letters and digits, a fixed stop list, one plural fold), the BM25 parameters, the
heading weight, and the tie-break by page path and then section order. Equal inputs
give identical rankings and byte-identical index files, whatever the hash seed.

Of what a reader of the site cannot see, the index leaves out exactly what the
page's own Markdown and HTML decide, as follows; it evaluates no CSS, so what the
site's own stylesheet hides by element alone is not among it. A section's text,
its heading and its title keep no character in :data:`HIDDEN_CHARACTERS_RE`
(controls, zero-width and bidi characters, variation selectors, Unicode tag
characters), spelled as an HTML character reference or not. They keep none of:

* raw HTML that renders nothing (:data:`_UNRENDERED_RE`): a comment, a processing
  instruction, a declaration or a CDATA section, a comment that never ends hiding
  the rest of the page;
* the content of an element whose content HTML renders as no text, only as a
  fallback, or as a drawing, a formula or a list of choices
  (:data:`_UNSHOWN_ELEMENTS`), of a dialog that is not open, and of any element
  with an attribute outside :data:`_PLAIN_ATTRIBUTES`, so a ``style``, a
  ``class`` or ``hidden`` hides it whatever it says. That content is left out up
  to the element's matching end tag, across paragraphs, code blocks and
  headings, or to the end of the page, and an unclosed ``p`` to its paragraph's
  end;
* a link reference definition, in every title form, ``[//]: # (a note)`` among
  them.

A page holding a stylesheet, a script or a meta element of its own
(:data:`_PAGE_WIDE_ELEMENTS`) is not indexed at all, because those can hide any
of it. A details element's content is indexed, closed or not, because its reader
opens it with a click. Where Markdown reads the same characters as text, the index
can still leave them out, which errs toward hiding: an escaped ``\\<``, a ``<!--``
in a paragraph, an element the browser closes before its end tag, and what follows
a ``<!--`` in a blockquote that ends before its ``-->``, which the site ends with
the quote.

A heading the site renders as none is none here either: one inside a comment or
any other raw HTML block, read as CommonMark reads one
(:func:`_html_block_lines`), gets no section and takes no slug, so the headings
after it are numbered as the site numbers them, and neither does an id inside a
comment count. A heading inside a hidden element is rendered and then hidden, so
it gets no section and still takes its slug. Ids and addresses keep the site's
anchors, whose slugs can hold a variation selector, so every id or address handed
out is written without those characters and :func:`resolve` reads one back either
way.

Where the pages come from
-------------------------
:func:`build` reads a directory holding ``docs/`` and ``docs-site/setup.sh``, and
runs that script with :data:`SETUP_ENV` as its whole environment. The directory is
either a working tree or a snapshot written by :func:`snapshot_from_git`, which
reads each ``docs/**.md`` blob of one commit through :func:`ui_change.git` and
writes only regular-file blobs at safe paths. A symlink committed under ``docs/``
therefore never reaches the index, however it is spelled.

Usage::

    python3 scripts/ux_runner/docs_index.py search "prefix mapping"
    python3 scripts/ux_runner/docs_index.py search QUERY --index INDEX.json --json
    python3 scripts/ux_runner/docs_index.py build --out INDEX.json
    python3 scripts/ux_runner/docs_index.py build --out INDEX.json --git-ref HEAD
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import html
import json
import math
import os
import re
import stat
import sys
import tempfile
import unicodedata
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from posixpath import dirname as posix_dirname
from posixpath import join as posix_join
from posixpath import normpath as posix_normpath
from typing import Any, Literal
from urllib.parse import unquote

_HERE = Path(__file__).resolve().parent
if sys.path and Path(sys.path[0] or ".").resolve() == _HERE:
    sys.path.pop(0)
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

import check_markdown_links  # noqa: E402

from ux_runner import ui_change  # noqa: E402

#: The version of the file :func:`save` writes and :func:`load` reads.
INDEX_FORMAT = 1
#: Where the documentation site serves; the UI's ``DOCS_BASE_URL`` and astro's
#: ``site`` plus ``base``.
DOCS_BASE_URL = "https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws"
#: The site's path below its host, astro's ``base``.
SITE_BASE_PATH = "/accelerated-intelligent-document-processing-on-aws"
#: The repository a GitHub blob link has to name to reach a page here.
GITHUB_REPO = (
    "aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws"
)
#: BM25's term-frequency saturation.
BM25_K1 = 1.2
#: BM25's document-length normalisation.
BM25_B = 0.75
#: How much a token of a section's title path counts, against 1 for one of its body.
HEADING_WEIGHT = 2
#: Hits :func:`search` returns unless told otherwise.
SEARCH_LIMIT = 5
#: The longest excerpt a hit carries.
EXCERPT_CHARS = 420
#: The longest part of a section :func:`read` returns at once.
READ_PART_CHARS = 4000
#: The most parts of one section :func:`read` serves; text past them is never read.
MAX_PARTS = 20
#: How many following sections of the same page a read names.
RELATED_LIMIT = 5
#: How much of a query is read.
QUERY_CHARS = 200
#: How much of an address is read.
REF_CHARS = 300
#: Hex digits in a short hash: the corpus hash, and every hash the stories record.
HASH_CHARS = 12
#: The largest page :func:`build` reads.
PAGE_MAX_BYTES = 1024 * 1024
#: The largest index file :func:`save` writes and :func:`load` reads.
INDEX_MAX_BYTES = 32 * 1024 * 1024
#: The most page bytes :func:`snapshot_from_git` writes.
SNAPSHOT_MAX_BYTES = 8 * 1024 * 1024
#: The most pages :func:`snapshot_from_git` writes.
SNAPSHOT_MAX_FILES = 1000
#: What a read of an address no page answers to returns, as the site's 404 does.
NO_PAGE = "No page at that address on the documentation site"
#: The whole environment ``docs-site/setup.sh`` runs with by default: nothing of
#: this process's, so no job secret reaches the shell.
SETUP_ENV: dict[str, str] = {"PATH": "/usr/bin:/bin", "LANG": "C"}
#: Words dropped from queries and sections alike, since nearly every section holds
#: them and a match on one says nothing.
STOP_WORDS: frozenset[str] = frozenset(
    {
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "for",
        "from",
        "how",
        "if",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "what",
        "when",
        "with",
        "you",
        "your",
    }
)
#: A repository path a snapshot will write: a ``.md`` file under ``docs/``, in
#: characters every page name here uses.
DOC_PATH_RE = re.compile(r"docs/[A-Za-z0-9._/-]+\.md")


class DocsIndexError(Exception):
    """The corpus, an index file or a git snapshot cannot be used."""


class PartOutOfRange(DocsIndexError):
    def __init__(self, parts: int) -> None:
        super().__init__(f"that section has {parts} part(s)")
        self.parts = parts


@dataclass(frozen=True)
class DocsSource:
    kind: str
    ref: str | None
    dirty: bool | None


@dataclass(frozen=True)
class Section:
    id: str
    page: str
    anchor: str
    level: int
    heading: str
    title: str
    address: str
    text: str
    order: int
    anchors: tuple[str, ...]


@dataclass(frozen=True)
class SearchHit:
    id: str
    title: str
    address: str
    excerpt: str
    score: float


@dataclass(frozen=True)
class Related:
    id: str
    title: str
    address: str


@dataclass(frozen=True)
class ReadResult:
    id: str
    title: str
    address: str
    text: str
    part: int
    parts: int
    related: tuple[Related, ...]


@dataclass(frozen=True)
class Index:
    format: int
    source: DocsSource
    corpus_hash: str
    pages: tuple[str, ...]
    sections: tuple[Section, ...]
    _by_id: Mapping[str, int] = field(init=False, repr=False, compare=False)
    _spans: Mapping[str, tuple[int, int]] = field(init=False, repr=False, compare=False)
    _postings: Mapping[str, tuple[tuple[int, int], ...]] = field(
        init=False, repr=False, compare=False
    )
    _lengths: tuple[int, ...] = field(init=False, repr=False, compare=False)
    _average: float = field(init=False, repr=False, compare=False)
    _slugs: Mapping[str, str] = field(init=False, repr=False, compare=False)
    _visible_ids: Mapping[str, str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Derive the lookup tables and the BM25 statistics from the sections."""
        by_id: dict[str, int] = {}
        visible_ids: dict[str, str] = {}
        spans: dict[str, tuple[int, int]] = {}
        postings: dict[str, list[tuple[int, int]]] = {}
        lengths: list[int] = []
        for position, section in enumerate(self.sections):
            if section.id in by_id:
                raise DocsIndexError(
                    f"section {position} repeats the id of an earlier section"
                )
            by_id[section.id] = position
            visible_ids.setdefault(visible(section.id), section.id)
            first, end = spans.get(section.page, (position, position))
            if end != position:
                raise DocsIndexError(
                    f"section {position} is apart from the other sections of its page"
                )
            spans[section.page] = (first, position + 1)
            body = tokenize(_without_addresses(section.text))
            heading = tokenize(section.title)
            counts = Counter(body)
            for token in heading:
                counts[token] += HEADING_WEIGHT
            lengths.append(len(body) + HEADING_WEIGHT * len(heading))
            for token, count in counts.items():
                postings.setdefault(token, []).append((position, count))
        total = sum(lengths)
        object.__setattr__(self, "_by_id", by_id)
        object.__setattr__(self, "_spans", spans)
        object.__setattr__(
            self, "_postings", {token: tuple(hits) for token, hits in postings.items()}
        )
        object.__setattr__(self, "_lengths", tuple(lengths))
        object.__setattr__(self, "_average", total / len(lengths) if total else 1.0)
        object.__setattr__(self, "_slugs", _slug_table(self.pages))
        object.__setattr__(self, "_visible_ids", visible_ids)


@dataclass(frozen=True)
class Snapshot:
    root: Path
    ref: str
    files: int
    bytes: int
    refused: tuple[str, ...]


_WORD_RE = re.compile(r"[^\W_]+")
_FRONT_MATTER_RE = re.compile(r"\A\s*---\r?\n.*?\r?\n---[ \t]*(?=\r?\n|\Z)", re.S)
_TITLE_RE = re.compile(r"^title:[ \t]*(?P<value>.*?)[ \t]*$", re.M)
_LICENSE_LINE_RE = re.compile(
    r"[ \t]*(?:Copyright Amazon\.com, Inc\. or its affiliates\. All Rights Reserved\."
    r"|SPDX-License-Identifier:.*)[ \t]*"
)
_NO_BLANK_LINE = r"(?!\n[ \t]*\n)"
_FLAT_TEXT = rf"(?:{_NO_BLANK_LINE}(?:[^\[\]\\]|\\.))*"
_LINK_TEXT = rf"(?:{_NO_BLANK_LINE}(?:[^\[\]\\]|\\.|\[{_FLAT_TEXT}\]))*"
_LINK_TAIL = (
    r"\(\s*<?(?P<target>[^)>\s]*)>?(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)"
)
_IMAGE_RE = re.compile(rf"!\[{_LINK_TEXT}\](?:{_LINK_TAIL}|\[[^\]\n]*\])")
_LINK_RE = re.compile(rf"\[(?P<text>{_LINK_TEXT})\]{_LINK_TAIL}")
_REFERENCE_LINK_RE = re.compile(rf"\[(?P<text>{_LINK_TEXT})\]\[[^\]\n]*\]")
_AUTOLINK_RE = re.compile(r"<[A-Za-z][A-Za-z0-9+.-]{1,31}:[^<>\s]*>")
_BREAK_RE = re.compile(r"<br\s*/?>", re.I)
_ATTRIBUTES = r"""(?:\s(?:[^<>"']|"[^"]*"|'[^']*')*)?"""
_TAG_RE = re.compile(rf"</?[A-Za-z][A-Za-z0-9-]*{_ATTRIBUTES}/?>")
_URL_RE = re.compile(
    r"(?i:https?://)[^\s<>()\[\]\"'`\x00]*[^\s<>()\[\]\"'`\x00.,;:!?*_~]"
)
_HELD_RE = re.compile(r"\x00(\d+)\x00")
_ADDRESS_SUFFIX_RE = re.compile(r" \(/(?:[a-z0-9._-]+/)*(?:#[^\s()]*)?\)")
_HEADING_CODE_RE = re.compile(r"(`+[^`]*`+)")
_HEADING_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_HEX40_RE = re.compile(r"[0-9a-f]{40}")
_SHORT_HASH_RE = re.compile(r"[0-9a-f]{12}")
_SCHEME_RE = re.compile(r"(?i:https?)://")
_HOST_RE = re.compile(r"[^/?#]*")
_GITHUB_PATH_RE = re.compile(
    r"/(?P<owner>[^/?#]+)/(?P<name>[^/?#]+)/blob/(?P<path>[^?#]*)(?P<tail>[?#].*)?",
    re.S,
)
_UNPRINTABLE_RE = re.compile(
    "[\x00-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]"
)
#: Characters a reader of the published page cannot see. The same class
#: ``story_prompt`` refuses in the texts it inlines, less tab and line break.
HIDDEN_CHARACTERS_RE = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u00ad\u034f\u0600-\u0605\u061c\u06dd\u070f"
    r"\u0890\u0891\u08e2\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f"
    r"\u2028-\u202e\u2060-\u206f\u3164\ud800-\udfff\ufe00-\ufe0f\ufeff\uffa0"
    r"\ufff0-\ufffb\U000110bd\U000110cd\U00013430-\U0001345f"
    r"\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)
#: Raw HTML that renders nothing and holds no element, as CommonMark reads it: a
#: comment, a processing instruction, a declaration or a CDATA section. A comment
#: that never ends hides the rest of the page.
_UNRENDERED_RE = re.compile(
    r"<!-->|<!--->|<!--.*?(?:-->|\Z)|<\?.*?\?>|<![A-Za-z][^>]*>|<!\[CDATA\[.*?\]\]>",
    re.S,
)
#: A line that can open a raw HTML block, in which Markdown reads backticks as text.
_HTML_BLOCK_START_RE = re.compile(r" {0,3}<[A-Za-z/!?]")
#: CommonMark's first five kinds of raw HTML block, each by the line that opens it
#: and what a line holds where it ends: a raw-text element, a comment, a
#: processing instruction, a declaration, a CDATA section.
_HTML_BLOCKS_TO_A_MARKER: tuple[tuple[re.Pattern[str], re.Pattern[str]], ...] = (
    (
        re.compile(r" {0,3}<(?:pre|script|style|textarea)(?:[ \t>]|$)", re.I),
        re.compile(r"</(?:pre|script|style|textarea)>", re.I),
    ),
    (re.compile(r" {0,3}<!--"), re.compile(r"-->")),
    (re.compile(r" {0,3}<\?"), re.compile(r"\?>")),
    (re.compile(r" {0,3}<![A-Za-z]"), re.compile(r">")),
    (re.compile(r" {0,3}<!\[CDATA\["), re.compile(r"\]\]>")),
)
#: The tag names that open CommonMark's sixth kind of raw HTML block, which ends at
#: a blank line and can interrupt a paragraph.
_HTML_BLOCK_NAMES = frozenset(
    {
        "address",
        "article",
        "aside",
        "base",
        "basefont",
        "blockquote",
        "body",
        "caption",
        "center",
        "col",
        "colgroup",
        "dd",
        "details",
        "dialog",
        "dir",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "frame",
        "frameset",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "head",
        "header",
        "hr",
        "html",
        "iframe",
        "legend",
        "li",
        "link",
        "main",
        "menu",
        "menuitem",
        "nav",
        "noframes",
        "ol",
        "optgroup",
        "option",
        "p",
        "param",
        "search",
        "section",
        "summary",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "title",
        "tr",
        "track",
        "ul",
    }
)
_HTML_BLOCK_TAG_RE = re.compile(
    r" {0,3}</?(?P<name>[A-Za-z][A-Za-z0-9-]*)(?:[ \t]|/?>|$)"
)
#: CommonMark's seventh kind: a line holding one whole open or closing tag and
#: nothing else that no kind before it opens, a lone ``</pre>`` or ``<pre/>``
#: among them, which ends at a blank line and cannot interrupt a paragraph.
_HTML_BLOCK_TAG_LINE_RE = re.compile(
    r" {0,3}(?:<[A-Za-z][A-Za-z0-9-]*"
    r"""(?:\s+[A-Za-z_:][\w.:-]*(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?)*"""
    r"\s*/?>|</[A-Za-z][A-Za-z0-9-]*\s*>)[ \t]*"
)
#: Elements whose content HTML renders as no text, renders only when what they
#: embed cannot be shown, or renders as a drawing, a formula or a list of choices,
#: none of which a text index can follow.
_UNSHOWN_ELEMENTS = (
    "audio",
    "canvas",
    "datalist",
    "head",
    "iframe",
    "math",
    "noembed",
    "noframes",
    "noscript",
    "object",
    "rp",
    "select",
    "svg",
    "template",
    "title",
    "video",
)
#: Elements that show their content only with an ``open`` attribute and that a
#: reader cannot open: without a script, nothing on the page can. A closed details
#: element is not one, since its reader opens it with a click.
_CLOSED_ELEMENTS = ("dialog",)
#: Elements that restyle, rewrite or replace the whole page, so that which of its
#: text a reader sees cannot be told from its source.
_PAGE_WIDE_ELEMENTS = ("link", "meta", "script", "style")
#: The attributes that cannot hide an element's text. Every other one can (a style,
#: a class the site's stylesheet styles, ``hidden``, a font colour), and the index
#: does not evaluate CSS, so an element with any other attribute hides its text;
#: ``aria-`` attributes describe an element to assistive technology and change
#: nothing on screen.
_PLAIN_ATTRIBUTES = frozenset(
    {
        "align",
        "alt",
        "cite",
        "colspan",
        "datetime",
        "dir",
        "headers",
        "height",
        "href",
        "id",
        "lang",
        "name",
        "open",
        "rel",
        "reversed",
        "role",
        "rowspan",
        "scope",
        "src",
        "srcset",
        "start",
        "target",
        "title",
        "type",
        "valign",
        "value",
        "width",
    }
)
_VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)
_OPEN_TAG_RE = re.compile(
    rf"<(?P<name>[A-Za-z][A-Za-z0-9-]*)(?P<attrs>{_ATTRIBUTES})/?>", re.S
)
_ATTRIBUTE_RE = re.compile(
    r"""[\s/]*(?P<name>[^\s"'<>/=]+)(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?"""
)
_PARAGRAPH_END_RE = re.compile(r"\n[ \t]*\n")
_REFERENCE_DEF_RE = re.compile(
    r""" {0,3}\[(?!\^)(?:[^\[\]\\]|\\.)+\]:[ \t]*(?:<[^<>\n]*>|[^\s<]\S*)"""
    r"""(?P<title>[ \t]+(?:"[^"]*"|'[^']*'|\([^()]*\)))?[ \t]*"""
)
_TITLE_LINE_RE = re.compile(r"""[ \t]*(?:"[^"]*"|'[^']*'|\([^()]*\))[ \t]*""")
_SITE_HOST = DOCS_BASE_URL.split("://", 1)[1].split("/", 1)[0]
_GITHUB_HOSTS = ("github.com", "www.github.com")
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOCTTY", 0)
    | getattr(os, "O_BINARY", 0)
)
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_TRUNC
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_BINARY", 0)
)
_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_BINARY", 0)
)
_REPO_ROOT = _HERE.parents[1]
_SETUP_SH = _REPO_ROOT / "docs-site" / "setup.sh"
_INDEX_KEYS = ("format", "source", "corpus_hash", "pages", "sections")
_SOURCE_KEYS = ("kind", "ref", "dirty")
_SECTION_KEYS = tuple(item.name for item in fields(Section))
_SECTION_TEXT_KEYS = ("id", "page", "anchor", "heading", "title", "address", "text")


def tokenize(text: str) -> list[str]:
    """NFKC, casefold, [^\\W_]+ runs of at least 2 characters, STOP_WORDS dropped, then one plural fold."""
    tokens: list[str] = []
    for word in _WORD_RE.findall(unicodedata.normalize("NFKC", text).casefold()):
        if len(word) < 2 or word in STOP_WORDS:
            continue
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        tokens.append(word)
    return tokens


def site_path(page: str) -> str:
    """The site path of a docs-relative page: ``.md`` stripped, each segment
    lowercased, a final ``index`` dropped, wrapped in slashes."""
    stem = page[:-3] if page.endswith(".md") else page
    segments = [segment.lower() for segment in stem.split("/") if segment]
    if segments and segments[-1] == "index":
        segments.pop()
    return "/" + "".join(f"{segment}/" for segment in segments)


def split_page(page: str, text: str) -> list[Section]:
    """One page into sections, every in-docs ``.md`` link read as a page.

    :func:`build` splits the same way and links only the pages it indexes.
    """
    return _sections(page, text, None)


def changed_sections(
    page: str, base_text: str | None, head_text: str | None
) -> frozenset[str]:
    """Section ids whose text differs, added or removed between two versions of one page."""
    base = {s.id: s.text for s in split_page(page, base_text or "")}
    head = {s.id: s.text for s in split_page(page, head_text or "")}
    return frozenset(
        ident
        for ident in base.keys() | head.keys()
        if base.get(ident) != head.get(ident)
    )


def build(
    root: Path,
    *,
    source: DocsSource,
    pages: Collection[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Index:
    """Index ``root/docs``: the published pages unless ``pages`` names them.

    The published set is derived by running ``root/docs-site/setup.sh`` with
    ``env`` as its whole environment, :data:`SETUP_ENV` when it is None.
    """
    _check_source(source)
    root = Path(root)
    if pages is None:
        try:
            published = check_markdown_links.published_pages(
                root, env=SETUP_ENV if env is None else env
            )
        except (OSError, RuntimeError) as error:
            raise DocsIndexError(
                f"the published pages cannot be derived: {error}"
            ) from error
        pages = [page[len("docs/") :] for page in published if page.startswith("docs/")]
    chosen = sorted(set(pages))
    for page in chosen:
        if not _safe_page(page):
            raise DocsIndexError(f"not a page name this index holds: {page[:120]!r}")
    if not chosen:
        raise DocsIndexError(f"{root / 'docs'} publishes no page")
    texts = {page: _read_page(root / "docs", page) for page in chosen}
    corpus_hash = _short_hash(
        [
            [page, hashlib.sha256(texts[page].encode("utf-8")).hexdigest()]
            for page in chosen
        ]
    )
    linkable = frozenset(chosen)
    sections: list[Section] = []
    for page in chosen:
        sections.extend(_sections(page, texts[page], linkable))
    return Index(
        format=INDEX_FORMAT,
        source=source,
        corpus_hash=corpus_hash,
        pages=tuple(chosen),
        sections=tuple(sections),
    )


def search(index: Index, query: str, *, limit: int = SEARCH_LIMIT) -> list[SearchHit]:
    """BM25 over the sections: the best ``limit`` with a positive score.

    Ties go to the page path and then the section order, and each score is
    rounded to 4 decimals before it is compared.
    """
    terms = list(dict.fromkeys(tokenize(query[:QUERY_CHARS])))
    if limit < 1 or not terms:
        return []
    total = len(index.sections)
    scores: dict[int, float] = {}
    for term in terms:
        hits = index._postings.get(term, ())
        if not hits:
            continue
        idf = math.log(1 + (total - len(hits) + 0.5) / (len(hits) + 0.5))
        for position, count in hits:
            norm = BM25_K1 * (
                1 - BM25_B + BM25_B * index._lengths[position] / index._average
            )
            gain = idf * count * (BM25_K1 + 1) / (count + norm)
            scores[position] = scores.get(position, 0.0) + gain
    ranked = sorted(
        (
            -rounded,
            index.sections[position].page,
            index.sections[position].order,
            position,
        )
        for position, score in scores.items()
        if (rounded := round(score, 4)) > 0
    )
    wanted = frozenset(terms)
    found: list[SearchHit] = []
    for negative, _, _, position in ranked[:limit]:
        section = index.sections[position]
        found.append(
            SearchHit(
                id=visible(section.id),
                title=section.title,
                address=visible(section.address),
                excerpt=_excerpt(section.text, wanted),
                score=-negative,
            )
        )
    return found


def resolve(index: Index, ref: str) -> str | None:
    """The section id ``ref`` names, or None for a miss.

    In order: an exact section id or page id; the site's URL, base path or a
    leading ``/`` stripped; a GitHub blob link into this repository's ``docs/``;
    a docs-relative ``.md`` path; a lowercase site slug or its slugged alias.
    The anchor then picks a section, the section holding it, or the page's first.
    """
    ref = ref[:REF_CHARS].strip()
    if not ref:
        return None
    if ref in index._by_id:
        return ref
    if ref in index._visible_ids:
        return index._visible_ids[ref]
    if ref in index._spans:
        return index.sections[index._spans[ref][0]].id
    located = _locate(index, ref)
    if located is None:
        return None
    return _section_for(index, *located)


def read(index: Index, ref: str, *, part: int = 1) -> ReadResult | None:
    """None for a miss; raises PartOutOfRange for a part outside 1..min(parts, MAX_PARTS)."""
    found = resolve(index, ref)
    if found is None:
        return None
    position = index._by_id[found]
    section = index.sections[position]
    pieces = _parts(section.text)
    parts = min(len(pieces), MAX_PARTS)
    if not 1 <= part <= parts:
        raise PartOutOfRange(parts)
    end = min(index._spans[section.page][1], position + 1 + RELATED_LIMIT)
    return ReadResult(
        id=visible(section.id),
        title=section.title,
        address=visible(section.address),
        text=pieces[part - 1],
        part=part,
        parts=parts,
        related=tuple(
            Related(
                id=visible(other.id),
                title=other.title,
                address=visible(other.address),
            )
            for other in index.sections[position + 1 : end]
        ),
    )


def save(index: Index, path: Path) -> Path:
    """Write ``index`` to ``path`` as compact JSON, never through a symlink."""
    document = {
        "format": index.format,
        "source": asdict(index.source),
        "corpus_hash": index.corpus_hash,
        "pages": list(index.pages),
        "sections": [asdict(section) for section in index.sections],
    }
    data = (
        json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(data) > INDEX_MAX_BYTES:
        raise DocsIndexError(f"the index is larger than {INDEX_MAX_BYTES} bytes")
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(os.open(path, _WRITE_FLAGS, 0o644), "wb") as handle:
            handle.write(data)
    except OSError as error:
        raise DocsIndexError(f"cannot write {path}: {_reason(error)}") from error
    return path


def load(path: Path) -> Index:
    """Read an index :func:`save` wrote, checking its whole shape, and rebuild
    its statistics."""
    try:
        with open(os.open(path, _READ_FLAGS), "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise DocsIndexError(f"{path} is not a regular file")
            data = handle.read(INDEX_MAX_BYTES + 1)
    except OSError as error:
        raise DocsIndexError(f"cannot read {path}: {_reason(error)}") from error
    if len(data) > INDEX_MAX_BYTES:
        raise DocsIndexError(f"{path} is larger than {INDEX_MAX_BYTES} bytes")
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (ValueError, RecursionError) as error:
        raise DocsIndexError(f"{path} is not an index file: {error}") from error
    return _index_from_json(document)


def snapshot_from_git(
    repo_dir: Path,
    ref: str,
    dest: Path,
    *,
    setup_sh: Path,
    git: Callable[..., str] | None = None,
) -> Snapshot:
    """Write ``ref``'s ``docs/**.md`` blobs and ``setup_sh`` under ``dest``.

    Only mode-100644 blobs at a path :data:`DOC_PATH_RE` matches, with no ``.``
    or ``..`` segment, are written; every other ``.md`` entry is listed in
    ``refused``. Each blob is read with ``cat-file blob`` through ``git``, which is
    :func:`ui_change.git` unless given.
    """
    run = git or ui_change.git
    if (
        not ref
        or ref.startswith("-")
        or len(ref) > 200
        or any(char.isspace() or not char.isprintable() for char in ref)
    ):
        raise DocsIndexError("not a git revision this reads")
    try:
        commit = run(repo_dir, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
        if not _HEX40_RE.fullmatch(commit):
            raise DocsIndexError(f"git rev-parse did not print a commit for {ref}")
        listing = run(repo_dir, "ls-tree", "-r", "-z", commit, "--", "docs")
    except (ui_change.UiChangeError, OSError) as error:
        raise DocsIndexError(f"git cannot read the docs at {ref}: {error}") from error
    blobs, refused = _tree_entries(listing)
    if len(blobs) > SNAPSHOT_MAX_FILES:
        raise DocsIndexError(
            f"{ref} holds {len(blobs)} pages, more than the {SNAPSHOT_MAX_FILES} "
            "a snapshot takes"
        )
    dest = Path(dest)
    _fresh(dest)
    size = 0
    for path, oid in blobs:
        try:
            data = run(repo_dir, "cat-file", "blob", oid).encode("utf-8")
        except (ui_change.UiChangeError, OSError) as error:
            raise DocsIndexError(f"git cannot read {path}: {error}") from error
        size += len(data)
        if size > SNAPSHOT_MAX_BYTES:
            raise DocsIndexError(
                f"the docs at {ref} exceed the {SNAPSHOT_MAX_BYTES} bytes a "
                "snapshot takes"
            )
        _write_new(dest, path, data)
    try:
        script = Path(setup_sh).read_bytes()
    except OSError as error:
        raise DocsIndexError(f"cannot read {setup_sh}: {_reason(error)}") from error
    _write_new(dest, "docs-site/setup.sh", script)
    return Snapshot(
        root=dest, ref=commit, files=len(blobs), bytes=size, refused=tuple(refused)
    )


def main(argv: list[str] | None = None) -> int:
    """Search the published documentation, or write an index file."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "build":
        if args.repo is not None and args.git_ref is None:
            parser.error("--repo needs --git-ref")
        if args.root is not None and args.git_ref is not None:
            parser.error("--root and --git-ref name two different sources; give one")
    try:
        if args.command == "search":
            return _search_main(args)
        return _build_main(args)
    except DocsIndexError as error:
        print(f"FAILED: {_printable(str(error))}", file=sys.stderr)
        return 1


def _sections(page: str, text: str, linkable: Collection[str] | None) -> list[Section]:
    body, front_title = _front_matter(text.replace("\r\n", "\n").replace("\r", "\n"))
    written = body.split("\n")
    written_fenced = _fence_mask(written)
    raw_html = _html_block_lines(written, written_fenced)
    line_anchors = _line_anchors(
        _unfenced(_rendered(written), written_fenced), raw_html
    )
    shown_body = _shown(written)
    if shown_body is None:
        return []
    raw = shown_body.split("\n")
    fenced = _fence_mask(raw)
    heads: list[tuple[int, int, str]] = []
    for number, line in enumerate(_unfenced(raw, fenced)):
        atx = check_markdown_links.ATX_HEADING.match(line)
        if atx and len(atx.group(1)) <= 3 and not raw_html[number]:
            heads.append((number, len(atx.group(1)), _heading_text(atx.group(2))))
    first_h1 = next((heading for _, level, heading in heads if level == 1), "")
    title = front_title or first_h1 or (page[:-3] if page.endswith(".md") else page)
    path = site_path(page)
    sections: list[Section] = []
    start = heads[0][0] if heads else len(raw)
    preamble = _clean(page, raw[:start], fenced[:start], linkable)
    if preamble:
        sections.append(
            Section(
                id=page,
                page=page,
                anchor="",
                level=0,
                heading="",
                title=title,
                address=path,
                text=preamble,
                order=0,
                anchors=_flatten(line_anchors[:start]),
            )
        )
    parent = ""
    for number, (start, level, heading) in enumerate(heads):
        end = heads[number + 1][0] if number + 1 < len(heads) else len(raw)
        if level == 1:
            parent = ""
            label = title if heading == title else _title_path(title, heading)
        elif level == 2:
            parent = heading
            label = _title_path(title, heading)
        else:
            label = _title_path(title, parent, heading)
        anchor = line_anchors[start][0]
        sections.append(
            Section(
                id=f"{page}#{anchor}",
                page=page,
                anchor=anchor,
                level=level,
                heading=heading,
                title=label,
                address=f"{path}#{anchor}",
                text=_clean(
                    page, raw[start + 1 : end], fenced[start + 1 : end], linkable
                ),
                order=len(sections),
                anchors=_flatten(line_anchors[start:end]),
            )
        )
    return sections


def _front_matter(text: str) -> tuple[str, str]:
    match = _FRONT_MATTER_RE.match(text)
    if match is None:
        return text, ""
    block = match.group(0)
    found = _TITLE_RE.search(block)
    title = _unquote_title(found.group("value")) if found else ""
    return "\n" * block.count("\n") + text[match.end() :], title


def _unquote_title(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    elif len(value) >= 2 and value[0] == value[-1] == "'":
        value = value[1:-1].replace("''", "'")
    return " ".join(visible(value).split())


def _fence_mask(lines: Sequence[str]) -> list[bool]:
    """Which lines :func:`check_markdown_links.strip_fences` blanks: every line of
    a fenced block, its fences included."""
    mask: list[bool] = []
    fence: tuple[str, int] | None = None
    for raw in lines:
        match = check_markdown_links.FENCE.match(
            check_markdown_links.BLOCKQUOTE.sub("", raw, count=1)
        )
        if fence is None:
            if match:
                fence = (match.group(1)[0], len(match.group(1)))
            mask.append(match is not None)
            continue
        if (
            match
            and match.group(1)[0] == fence[0]
            and len(match.group(1)) >= fence[1]
            and not match.group(2).strip()
        ):
            fence = None
        mask.append(True)
    return mask


def _html_block_lines(lines: Sequence[str], fenced: Sequence[bool]) -> list[bool]:
    """Which lines outside fences are in a raw HTML block, as CommonMark reads one.

    The first five kinds (:data:`_HTML_BLOCKS_TO_A_MARKER`) run from the line that
    opens one to the line holding its end marker, which may be the same line or
    none, so the rest of the page. The sixth (:data:`_HTML_BLOCK_NAMES`) and the
    seventh (:data:`_HTML_BLOCK_TAG_LINE_RE`, not straight after a paragraph's
    line) run to the next blank line. A block opened inside a blockquote is read
    past the quote's markers, and ends with the quote: at the first later line
    with fewer markers, since a raw HTML block has no lazy continuation line. Any
    other takes every later line whole, ``>`` included. A fence leaves a block
    open, as CommonMark reads its lines as part of the block, and ends a
    paragraph. A heading written inside one is text on the site.
    """
    mask = [False] * len(lines)
    marker: re.Pattern[str] | None = None
    to_blank = False
    quotes = 0
    paragraph = False
    for number, line in enumerate(lines):
        if fenced[number]:
            paragraph = False
            continue
        quote = check_markdown_links.BLOCKQUOTE.match(line)
        depth = quote.group(0).count(">") if quote is not None else 0
        content = line[quote.end() :] if quote is not None else line
        if depth < quotes:
            marker, to_blank, quotes = None, False, 0
        inside = content if quotes else line
        if marker is not None:
            mask[number] = True
            if marker.search(inside):
                marker = None
            continue
        if to_blank:
            if inside.strip():
                mask[number] = True
                continue
            to_blank = False
        if not content.strip():
            paragraph = False
            continue
        kind = _html_block_kind(content, paragraph)
        if kind is None:
            paragraph = not check_markdown_links.ATX_HEADING.match(content)
            continue
        mask[number] = True
        paragraph = False
        quotes = depth
        if kind is True:
            to_blank = True
        elif not kind.search(content):
            marker = kind
    return mask


def _html_block_kind(
    content: str, paragraph: bool
) -> re.Pattern[str] | Literal[True] | None:
    """The end marker of the raw HTML block ``content`` opens, True for one that
    ends at a blank line, or None when it opens none."""
    for opening, end in _HTML_BLOCKS_TO_A_MARKER:
        if opening.match(content):
            return end
    named = _HTML_BLOCK_TAG_RE.match(content)
    if named is not None and named.group("name").casefold() in _HTML_BLOCK_NAMES:
        return True
    if not paragraph and _HTML_BLOCK_TAG_LINE_RE.fullmatch(content) is not None:
        return True
    return None


def _rendered(lines: Sequence[str]) -> list[str]:
    """``lines`` with the raw HTML that renders nothing blanked, as :func:`_shown`
    blanks it: what the site's slugger and its ids are read from."""
    view = list(_markup_view(lines, _fence_mask(lines)))
    shown = list("\n".join(lines))
    for found in _UNRENDERED_RE.finditer("".join(view)):
        _blank(shown, *found.span())
    return "".join(shown).split("\n")


def _unfenced(lines: Sequence[str], fenced: Sequence[bool]) -> list[str]:
    """Each line as headings are read from it: blank in a fence, else its first
    blockquote marker removed."""
    return [
        "" if fenced[number] else check_markdown_links.BLOCKQUOTE.sub("", line, count=1)
        for number, line in enumerate(lines)
    ]


def _line_anchors(
    lines: Sequence[str], raw_html: Sequence[bool] | None = None
) -> list[tuple[str, ...]]:
    """The fragment ids each line defines, as :func:`check_markdown_links.anchors`
    finds them, with github-slugger's numbering of repeated headings.

    A line of a raw HTML block (``raw_html``) is text on the site, not a heading,
    so it takes no slug and no place in the numbering; an id its HTML gives still
    counts.
    """
    occurrences: dict[str, int] = {}
    found: list[tuple[str, ...]] = []
    raw = list(raw_html) if raw_html is not None else [False] * len(lines)
    for number, line in enumerate(lines):
        here: list[str] = []
        heading: str | None = None
        atx = None if raw[number] else check_markdown_links.ATX_HEADING.match(line)
        if atx:
            heading = atx.group(2)
        elif (
            number > 0
            and not raw[number]
            and not raw[number - 1]
            and check_markdown_links.SETEXT_RULE.match(line)
            and lines[number - 1].strip()
            and not check_markdown_links.ATX_HEADING.match(lines[number - 1])
        ):
            heading = lines[number - 1].strip()
        if heading is not None:
            here.append(_numbered(check_markdown_links.slugify(heading), occurrences))
        here.extend(m.group(1) for m in check_markdown_links.HTML_ANCHOR.finditer(line))
        found.append(tuple(here))
    return found


def _numbered(base: str, occurrences: dict[str, int]) -> str:
    slug = base
    while slug in occurrences:
        occurrences[base] += 1
        slug = f"{base}-{occurrences[base]}"
    occurrences[slug] = 0
    return slug


def _flatten(groups: Iterable[tuple[str, ...]]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(anchor for group in groups for anchor in group))


def _title_path(*parts: str) -> str:
    return " > ".join(part for part in parts if part)


def _heading_text(raw: str) -> str:
    rendered: list[str] = []
    for number, part in enumerate(_HEADING_CODE_RE.split(raw)):
        if number % 2:
            rendered.append(part.strip("`"))
            continue
        part = _TAG_RE.sub("", part)
        part = _HEADING_LINK_RE.sub(r"\1", part)
        for markup in ("**", "*", "~~"):
            part = part.replace(markup, "")
        rendered.append(html.unescape(part))
    return " ".join(visible("".join(rendered)).split())


def _clean(
    page: str,
    lines: Sequence[str],
    fenced: Sequence[bool],
    linkable: Collection[str] | None,
) -> str:
    rows: list[tuple[str, bool]] = []
    start = 0
    while start < len(lines):
        end = start
        while end < len(lines) and fenced[end] == fenced[start]:
            end += 1
        if fenced[start]:
            rows.extend((visible(line), True) for line in lines[start:end])
        else:
            prose = _clean_prose(page, lines[start:end], linkable)
            rows.extend((line.rstrip(), False) for line in prose.split("\n"))
        start = end
    return _layout(rows)


def _clean_prose(
    page: str, lines: Sequence[str], linkable: Collection[str] | None
) -> str:
    text = "\n".join(_without_definitions(lines)).replace("\x00", "\ufffd")
    held: list[str] = []
    text = check_markdown_links.CODE_SPAN.sub(lambda m: _hold(held, m.group(0)), text)
    text = _IMAGE_RE.sub("", text)
    text = _LINK_RE.sub(
        lambda m: _link_text(page, m.group("text"), m.group("target"), linkable), text
    )
    text = _REFERENCE_LINK_RE.sub(lambda m: m.group("text"), text)
    text = _AUTOLINK_RE.sub("", text)
    text = _BREAK_RE.sub(" ", text)
    text = _TAG_RE.sub("", text)
    text = _URL_RE.sub("", text)
    text = _HELD_RE.sub(lambda m: held[int(m.group(1))], html.unescape(text))
    return visible(text)


def visible(text: str) -> str:
    """``text`` without the characters a reader of the page cannot see."""
    return HIDDEN_CHARACTERS_RE.sub("", text)


def _without_definitions(lines: Sequence[str]) -> list[str]:
    """``lines`` with the license lines and the link reference definitions blanked.

    A definition renders nothing whatever its title is spelled with, so the
    ``[//]: # (a note)`` idiom goes too, and so does a title on the next line.
    """
    kept = list(lines)
    for number, line in enumerate(lines):
        if _LICENSE_LINE_RE.fullmatch(line):
            kept[number] = ""
            continue
        found = _REFERENCE_DEF_RE.fullmatch(line)
        if found is None and not check_markdown_links.REFERENCE_DEF.match(line):
            continue
        kept[number] = ""
        if found is None:
            continue
        following = number + 1
        if (
            found.group("title") is None
            and following < len(lines)
            and _TITLE_LINE_RE.fullmatch(lines[following])
        ):
            kept[following] = ""
    return kept


def _shown(lines: Sequence[str]) -> str | None:
    """The page as a reader of the site sees it: what its HTML hides blanked out.

    ``lines`` are the page's lines below its front matter. What is hidden becomes
    spaces, with every line break kept, so the page keeps its lines and nothing
    hidden at the start of a line can turn what follows into a heading or a fence.
    Hidden is: a comment and the other raw HTML that renders nothing
    (:data:`_UNRENDERED_RE`); an element of :data:`_UNSHOWN_ELEMENTS`; a dialog that
    is not open; and an element carrying any attribute outside
    :data:`_PLAIN_ATTRIBUTES`. A hidden element ends at its matching end tag, across
    paragraphs, fences and headings, or at the end of the page; an unclosed ``p``
    ends with its paragraph, as the paragraph's own end tag closes it. None for a
    page holding an element of :data:`_PAGE_WIDE_ELEMENTS`.
    """
    body = "\n".join(lines)
    view = list(_markup_view(lines, _fence_mask(lines)))
    hidden: list[tuple[int, int]] = []
    for found in _UNRENDERED_RE.finditer("".join(view)):
        hidden.append(found.span())
        _blank(view, *found.span())
    markup = "".join(view)
    for opening in _OPEN_TAG_RE.finditer(markup):
        if opening.group("name").casefold() in _PAGE_WIDE_ELEMENTS:
            return None
    hidden.extend(_hidden_ranges(markup))
    shown = list(body)
    for start, end in hidden:
        _blank(shown, start, end)
    return "".join(shown)


def _markup_view(lines: Sequence[str], fenced: Sequence[bool]) -> str:
    """The page as its tags are read: every fenced line blanked, and every code span
    blanked but on a line of a raw HTML block, where Markdown reads backticks as
    text. A raw HTML block is taken to run from a line that starts with a tag
    to the next blank line, which reads more lines as raw than Markdown does."""
    out: list[str] = []
    raw_block = False
    for line, fence in zip(lines, fenced, strict=True):
        content = check_markdown_links.BLOCKQUOTE.sub("", line, count=1)
        if fence or not content.strip():
            raw_block = False
            out.append(" " * len(line) if fence else line)
            continue
        raw_block = raw_block or bool(_HTML_BLOCK_START_RE.match(content))
        if raw_block:
            out.append(line)
            continue
        out.append(
            check_markdown_links.CODE_SPAN.sub(lambda m: " " * len(m.group(0)), line)
        )
    return "\n".join(out)


def _hidden_ranges(markup: str) -> list[tuple[int, int]]:
    """The spans of ``markup`` that hidden elements cover."""
    found: list[tuple[int, int]] = []
    position = 0
    end = len(markup)
    while position < end:
        opening = _OPEN_TAG_RE.search(markup, position)
        if opening is None:
            break
        name = opening.group("name").casefold()
        names = _attribute_names(opening.group("attrs"))
        plain = names is not None and all(
            attribute in _PLAIN_ATTRIBUTES or attribute.startswith("aria-")
            for attribute in names
        )
        closed = name in _CLOSED_ELEMENTS and "open" not in (names or ())
        if plain and name not in _UNSHOWN_ELEMENTS and not closed:
            position = opening.end()
            continue
        if name in _VOID_ELEMENTS:
            found.append(opening.span())
            position = opening.end()
            continue
        close = _element_end(markup, name, opening.end(), end)
        found.append((opening.start(), close))
        position = close
    return found


def _attribute_names(attrs: str) -> list[str] | None:
    """The attribute names of a tag, casefolded, or None when they do not parse."""
    names: list[str] = []
    position = 0
    while attrs[position:].strip(" \t\n\r\f/"):
        found = _ATTRIBUTE_RE.match(attrs, position)
        if found is None:
            return None
        names.append(found.group("name").casefold())
        position = found.end()
    return names


def _element_end(markup: str, name: str, start: int, end: int) -> int:
    """Where the element opened just before ``start`` ends: after its matching end
    tag, counting nested elements of the same name, else at ``end``, or for a
    ``p`` at the end of its paragraph. A nested start tag ending ``/>`` opens one
    too, because HTML ignores that slash on an element that is not void."""
    tag = re.compile(rf"<(/?){re.escape(name)}(?=[\s/>])[^<>]*>", re.I)
    depth = 1
    for found in tag.finditer(markup, start, end):
        depth += -1 if found.group(1) else 1
        if depth == 0:
            return found.end()
    if name == "p":
        paragraph = _PARAGRAPH_END_RE.search(markup, start, end)
        if paragraph is not None:
            return paragraph.start()
    return end


def _blank(chars: list[str], start: int, end: int) -> None:
    for position in range(start, end):
        if chars[position] != "\n":
            chars[position] = " "


def _hold(held: list[str], value: str) -> str:
    held.append(value)
    return f"\x00{len(held) - 1}\x00"


def _link_text(
    page: str, text: str, target: str, linkable: Collection[str] | None
) -> str:
    address = _docs_address(page, target, linkable)
    if address is None:
        return text
    return f"{text} ({address})" if text.strip() else ""


def _docs_address(
    page: str, target: str, linkable: Collection[str] | None
) -> str | None:
    """The site address of an in-docs ``.md`` link, resolved from ``page``'s
    directory as ``remark-rewrite-docs-links.mjs`` resolves it; None for any
    other link, and for a page outside ``linkable`` when that is given."""
    if (
        not target
        or target.startswith(("http", "#"))
        or ".md" not in target
        or check_markdown_links.SCHEME.match(target)
    ):
        return None
    match = check_markdown_links.SITE_REWRITABLE.match(target)
    if match is None:
        return None
    within = posix_normpath(posix_join(posix_dirname(page), match.group(1)))
    if not _safe_page(within) or (linkable is not None and within not in linkable):
        return None
    return site_path(within) + (match.group(2) or "")


def _layout(rows: Sequence[tuple[str, bool]]) -> str:
    lines: list[str] = []
    blanks = 0
    for line, fenced in rows:
        if not fenced and not line.strip():
            blanks += 1
            if blanks <= 2:
                lines.append("")
            continue
        blanks = 0
        lines.append(line)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _without_addresses(text: str) -> str:
    return _ADDRESS_SUFFIX_RE.sub("", text)


def _excerpt(text: str, wanted: Collection[str]) -> str:
    """The :data:`EXCERPT_CHARS` window of the single-lined text that starts at a
    word start and holds the most whole query-token hits, leftmost on a tie."""
    flat = " ".join(_without_addresses(text).split())
    if len(flat) <= EXCERPT_CHARS:
        return flat
    words = list(_WORD_RE.finditer(flat))
    hit_starts: list[int] = []
    hit_ends: list[int] = []
    for word in words:
        if any(token in wanted for token in tokenize(word.group())):
            hit_starts.append(word.start())
            hit_ends.append(word.end())
    best, most = 0, -1
    for start in [0, *(word.start() for word in words if word.start() > 0)]:
        held = bisect.bisect_right(
            hit_ends, start + EXCERPT_CHARS
        ) - bisect.bisect_left(hit_starts, start)
        if held > most:
            best, most = start, held
    return flat[best : best + EXCERPT_CHARS].strip()


def _parts(text: str) -> list[str]:
    """``text`` cut into pieces of at most :data:`READ_PART_CHARS`, each ending
    after the last newline or space that fits; joined, they are ``text``."""
    pieces: list[str] = []
    start = 0
    while len(text) - start > READ_PART_CHARS:
        limit = start + READ_PART_CHARS
        cut = max(text.rfind("\n", start, limit), text.rfind(" ", start, limit))
        end = cut + 1 if cut > start else limit
        pieces.append(text[start:end])
        start = end
    pieces.append(text[start:])
    return pieces


def _slug_table(pages: Iterable[str]) -> dict[str, str]:
    """Every lowercase slug that names a page: the site's own form first, then
    each segment through :func:`check_markdown_links.slugify`, then ``<dir>/index``."""
    ordered = sorted(pages)
    table: dict[str, str] = {}
    for page in ordered:
        table.setdefault(site_path(page).strip("/"), page)
    for page in ordered:
        stem = page[:-3] if page.endswith(".md") else page
        alias = [check_markdown_links.slugify(segment) for segment in stem.split("/")]
        if alias and alias[-1] == "index":
            alias.pop()
        table.setdefault("/".join(alias), page)
    for page in ordered:
        stem = page[:-3] if page.endswith(".md") else page
        if stem.rsplit("/", 1)[-1].lower() == "index":
            table.setdefault(stem.lower(), page)
    table.pop("", None)
    return table


def _locate(index: Index, ref: str) -> tuple[str, str] | None:
    """The page ``ref`` addresses and its fragment, or None (:func:`resolve`)."""
    rest = ref
    scheme = _SCHEME_RE.match(rest)
    if scheme:
        rest = rest[scheme.end() :]
    host_match = _HOST_RE.match(rest)
    raw_host = host_match.group(0) if host_match else ""
    host = raw_host.lower()
    if host in _GITHUB_HOSTS:
        return _github_page(index, rest[len(raw_host) :])
    if scheme or host == _SITE_HOST:
        if host != _SITE_HOST:
            return None
        rest = rest[len(raw_host) :]
        if not _under(rest, SITE_BASE_PATH):
            return None
        rest = rest[len(SITE_BASE_PATH) :]
    elif _under(rest, SITE_BASE_PATH):
        rest = rest[len(SITE_BASE_PATH) :]
    path, _, fragment = rest.partition("#")
    path = unquote(path.split("?", 1)[0]).lstrip("/")
    if path.endswith(".md"):
        page = path[2:] if path.startswith("./") else path
        page = page[len("docs/") :] if page.startswith("docs/") else page
        return (page, fragment) if page in index._spans else None
    slug = path.strip("/")
    if not slug or slug != slug.lower():
        return None
    page = index._slugs.get(slug)
    return None if page is None else (page, fragment)


def _github_page(index: Index, rest: str) -> tuple[str, str] | None:
    match = _GITHUB_PATH_RE.fullmatch(rest)
    if match is None:
        return None
    if f"{match.group('owner')}/{match.group('name')}".lower() != GITHUB_REPO.lower():
        return None
    path = match.group("path")
    marker = path.find("/docs/")
    if marker < 0:
        return None
    page = unquote(path[marker + len("/docs/") :])
    fragment = (match.group("tail") or "").partition("#")[2]
    return (page, fragment) if page in index._spans else None


def _under(path: str, prefix: str) -> bool:
    return path == prefix or (path.startswith(prefix) and path[len(prefix)] in "/?#")


def _section_for(index: Index, page: str, fragment: str) -> str | None:
    span = index._spans.get(page)
    if span is None:
        return None
    first, end = span
    anchor = unquote(fragment)
    if anchor:
        direct = index._by_id.get(f"{page}#{anchor}")
        if direct is not None:
            return index.sections[direct].id
        shown = visible(anchor)
        for section in index.sections[first:end]:
            if anchor in section.anchors or any(
                visible(own) == shown for own in section.anchors
            ):
                return section.id
    return index.sections[first].id


def _safe_page(page: str) -> bool:
    return _safe_path(f"docs/{page}")


def _safe_path(path: str) -> bool:
    return DOC_PATH_RE.fullmatch(path) is not None and all(
        segment not in ("", ".", "..") for segment in path.split("/")
    )


def _check_source(source: DocsSource) -> None:
    if (
        source.kind == "git"
        and isinstance(source.ref, str)
        and _HEX40_RE.fullmatch(source.ref)
        and source.dirty is None
    ):
        return
    if (
        source.kind == "worktree"
        and source.ref is None
        and isinstance(source.dirty, bool)
    ):
        return
    raise DocsIndexError(
        "a docs source is git with a 40-hex commit and no dirty flag, or a worktree "
        "with a dirty flag and no commit"
    )


def _short_hash(value: object) -> str:
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:HASH_CHARS]


def _read_page(docs: Path, page: str) -> str:
    """``docs/<page>`` as text: opened without following a final symlink, inside
    ``docs`` after every directory link is resolved, a regular file, and at most
    :data:`PAGE_MAX_BYTES`."""
    path = docs / page
    home = os.path.realpath(docs)
    parent = os.path.realpath(path.parent)
    if parent != home and not parent.startswith(home + os.sep):
        raise DocsIndexError(f"docs/{page} is reached through a link out of docs/")
    try:
        with open(os.open(path, _READ_FLAGS), "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise DocsIndexError(f"docs/{page} is not a regular file")
            data = handle.read(PAGE_MAX_BYTES + 1)
    except OSError as error:
        raise DocsIndexError(f"cannot read docs/{page}: {_reason(error)}") from error
    if len(data) > PAGE_MAX_BYTES:
        raise DocsIndexError(f"docs/{page} is larger than {PAGE_MAX_BYTES} bytes")
    text = data.decode("utf-8-sig", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _tree_entries(listing: str) -> tuple[list[tuple[str, str]], list[str]]:
    blobs: list[tuple[str, str]] = []
    refused: list[str] = []
    for record in listing.split("\0"):
        if not record:
            continue
        meta, tab, path = record.partition("\t")
        parts = meta.split(" ")
        if not tab or len(parts) != 3:
            raise DocsIndexError("git ls-tree printed an entry this does not read")
        mode, kind, oid = parts
        if not path.endswith(".md"):
            continue
        if (
            mode == "100644"
            and kind == "blob"
            and _HEX40_RE.fullmatch(oid)
            and DOC_PATH_RE.fullmatch(path)
            and all(segment not in ("", ".", "..") for segment in path.split("/"))
        ):
            blobs.append((path, oid))
        else:
            refused.append(path)
    return blobs, refused


def _fresh(dest: Path) -> None:
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise DocsIndexError(f"cannot make {dest}: {_reason(error)}") from error
    for name in ("docs", "docs-site"):
        if os.path.lexists(dest / name):
            raise DocsIndexError(f"{dest / name} already exists")


def _write_new(base: Path, relative: str, data: bytes) -> None:
    """Create ``base/relative`` with ``data``: directories 0o755 and the file
    0o644, never through a symlink and never over an existing file."""
    *directories, name = relative.split("/")
    target = base
    for directory in directories:
        target = target / directory
        try:
            os.mkdir(target, 0o755)
        except FileExistsError:
            if not stat.S_ISDIR(os.lstat(target).st_mode):
                raise DocsIndexError(f"{target} is not a directory") from None
        except OSError as error:
            raise DocsIndexError(f"cannot make {target}: {_reason(error)}") from error
    try:
        with open(os.open(target / name, _CREATE_FLAGS, 0o644), "wb") as handle:
            handle.write(data)
    except OSError as error:
        raise DocsIndexError(
            f"cannot write {target / name}: {_reason(error)}"
        ) from error


def _index_from_json(document: object) -> Index:
    data = _closed(document, _INDEX_KEYS, "the index")
    if type(data["format"]) is not int or data["format"] != INDEX_FORMAT:
        raise DocsIndexError(f"format is not {INDEX_FORMAT}")
    source_data = _closed(data["source"], _SOURCE_KEYS, "source")
    source = DocsSource(
        kind=source_data["kind"], ref=source_data["ref"], dirty=source_data["dirty"]
    )
    _check_source(source)
    corpus_hash = data["corpus_hash"]
    if not isinstance(corpus_hash, str) or not _SHORT_HASH_RE.fullmatch(corpus_hash):
        raise DocsIndexError(f"corpus_hash is not {HASH_CHARS} lowercase hex digits")
    pages = data["pages"]
    if not isinstance(pages, list) or not all(
        isinstance(page, str) and _safe_page(page) for page in pages
    ):
        raise DocsIndexError("pages is not a list of docs-relative .md pages")
    if pages != sorted(set(pages)):
        raise DocsIndexError("pages is not sorted and unique")
    listed = data["sections"]
    if not isinstance(listed, list):
        raise DocsIndexError("sections is not a list")
    page_set = frozenset(pages)
    sections: list[Section] = []
    for position, item in enumerate(listed):
        section = _section_from_json(item, f"sections[{position}]", page_set)
        previous = sections[-1] if sections else None
        if previous is not None and previous.page == section.page:
            in_order = section.order == previous.order + 1
        else:
            in_order = section.order == 0 and (
                previous is None or previous.page < section.page
            )
        if not in_order:
            raise DocsIndexError(f"sections[{position}] is out of (page, order) order")
        sections.append(section)
    return Index(
        format=INDEX_FORMAT,
        source=source,
        corpus_hash=corpus_hash,
        pages=tuple(pages),
        sections=tuple(sections),
    )


def _section_from_json(item: object, where: str, page_set: Collection[str]) -> Section:
    data = _closed(item, _SECTION_KEYS, where)
    for name in _SECTION_TEXT_KEYS:
        if not isinstance(data[name], str):
            raise DocsIndexError(f"{where}.{name} is not a string")
    for name in ("level", "order"):
        if type(data[name]) is not int or data[name] < 0:
            raise DocsIndexError(f"{where}.{name} is not a non-negative integer")
    anchors = data["anchors"]
    if not isinstance(anchors, list) or not all(isinstance(a, str) for a in anchors):
        raise DocsIndexError(f"{where}.anchors is not a list of strings")
    page, anchor, level = data["page"], data["anchor"], data["level"]
    if page not in page_set:
        raise DocsIndexError(f"{where}.page is not one of the pages")
    if level == 0:
        consistent = (
            anchor == ""
            and data["heading"] == ""
            and data["order"] == 0
            and data["id"] == page
            and data["address"] == site_path(page)
        )
    else:
        consistent = (
            level <= 3
            and anchor in anchors
            and data["id"] == f"{page}#{anchor}"
            and data["address"] == f"{site_path(page)}#{anchor}"
        )
    if not consistent:
        raise DocsIndexError(f"{where} does not agree with its page, level and anchor")
    return Section(
        id=data["id"],
        page=page,
        anchor=anchor,
        level=level,
        heading=data["heading"],
        title=data["title"],
        address=data["address"],
        text=data["text"],
        order=data["order"],
        anchors=tuple(anchors),
    )


def _closed(value: object, keys: Sequence[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DocsIndexError(f"{where} is not an object")
    if set(value) != set(keys):
        unknown = sorted(str(key)[:40] for key in set(value) - set(keys))[:5]
        missing = [key for key in keys if key not in value]
        raise DocsIndexError(
            f"{where} does not have exactly its keys "
            f"(unknown: {', '.join(unknown) or 'none'}; "
            f"missing: {', '.join(missing) or 'none'})"
        )
    return value


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key, value in pairs:
        if key in mapping:
            raise DocsIndexError("an object in the index repeats a key")
        mapping[key] = value
    return mapping


def _reason(error: OSError) -> str:
    return error.strerror or type(error).__name__


def _printable(text: str) -> str:
    return _UNPRINTABLE_RE.sub("", text)


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docs_index.py",
        description=(__doc__ or "").split("\n\n", 1)[0],
    )
    commands = parser.add_subparsers(dest="command", required=True)
    finder = commands.add_parser("search", help="search the published documentation")
    finder.add_argument("query", help="what a first-time user would type")
    where = finder.add_mutually_exclusive_group()
    where.add_argument(
        "--root", type=Path, help="a checkout to index (default: this one)"
    )
    where.add_argument("--index", type=Path, help="an index file build wrote")
    finder.add_argument(
        "--limit",
        type=_positive,
        default=SEARCH_LIMIT,
        help=f"how many hits (default: {SEARCH_LIMIT})",
    )
    finder.add_argument("--json", action="store_true", help="print the hits as JSON")
    maker = commands.add_parser("build", help="write an index file")
    maker.add_argument("--out", type=Path, required=True, help="where to write it")
    maker.add_argument(
        "--root", type=Path, help="a checkout to index (default: this one)"
    )
    maker.add_argument(
        "--git-ref", help="index this commit's docs, read from git objects"
    )
    maker.add_argument(
        "--repo", type=Path, help="the repository --git-ref is read from"
    )
    return parser


def _worktree_source(root: Path) -> DocsSource:
    try:
        status = ui_change.git(
            root, "--no-optional-locks", "status", "--porcelain", "--", "docs"
        )
    except ui_change.UiChangeError:
        return DocsSource(kind="worktree", ref=None, dirty=True)
    return DocsSource(kind="worktree", ref=None, dirty=bool(status.strip()))


def _search_main(args: argparse.Namespace) -> int:
    if args.index is not None:
        index = load(args.index)
    else:
        root = args.root if args.root is not None else _REPO_ROOT
        index = build(root, source=_worktree_source(root))
    hits = search(index, args.query, limit=args.limit)
    if args.json:
        print(json.dumps([asdict(hit) for hit in hits], ensure_ascii=False, indent=2))
        return 0
    for number, hit in enumerate(hits, 1):
        print(f"{number}. {_printable(hit.title)}")
        print(f"   {_printable(hit.address)}")
        print(f"   {_printable(hit.excerpt)}")
    if not hits:
        print("No section of the published documentation matches that query.")
    return 0


def _build_main(args: argparse.Namespace) -> int:
    if args.git_ref is not None:
        repo = args.repo if args.repo is not None else _REPO_ROOT
        with tempfile.TemporaryDirectory(prefix="docs-index-") as scratch:
            snapshot = snapshot_from_git(
                repo, args.git_ref, Path(scratch) / "snapshot", setup_sh=_SETUP_SH
            )
            index = build(
                snapshot.root,
                source=DocsSource(kind="git", ref=snapshot.ref, dirty=None),
            )
        for path in snapshot.refused:
            print(f"refused: {_printable(path)}", file=sys.stderr)
        label = snapshot.ref
    else:
        root = args.root if args.root is not None else _REPO_ROOT
        source = _worktree_source(root)
        index = build(root, source=source)
        label = "worktree+dirty" if source.dirty else "worktree"
    written = save(index, args.out)
    print(
        f"docs index: {len(index.pages)} page(s), {len(index.sections)} section(s), "
        f"corpus {index.corpus_hash}, docs {label} -> {written}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
