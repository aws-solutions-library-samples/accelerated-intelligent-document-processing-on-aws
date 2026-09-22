#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Resolve every relative Markdown link in this repository, offline.

Run by ``make check-markdown-links``, which ``lint``, ``fastlint`` and
``lint-cicd`` all depend on, so both CI configurations reach it. Issue #1068.

**Five** finding kinds, over every Markdown file ``git`` reports — no filename
list, no directory list, nothing restated from a second place:

1. ``missing-path`` — ``[x](../foo/bar.md)`` must name a file or directory that
   exists, wherever the link lives: a ``docs/`` page, a ``README.md`` deep under
   ``nested/``, a ``CHANGELOG.md`` entry. ``escapes-repository`` is the same
   check reporting a target that climbed out of the checkout, which resolves on
   the author's machine and nowhere else.
2. ``missing-anchor`` — ``[x](./testing.md#some-heading)`` must name a heading
   ``testing.md`` actually has. A link whose page moved on is broken in the way a
   reader notices first, and it is answerable without a network.
3. ``unpublished-target`` — **a published page links only to published pages.**
   ``docs-site/setup.sh`` symlinks a specific set of ``docs/`` pages into the
   Starlight build. ``docs-site/plugins/remark-rewrite-docs-links.mjs`` rewrites
   a ``.md`` link that stays inside ``docs/`` to a site-relative URL and sends
   one that escapes ``docs/`` to a GitHub blob URL. So a published page linking
   to an *unpublished* page under ``docs/`` — ``docs/planning/``,
   ``docs/proposals/`` — resolves on GitHub and 404s on the site. That is the
   failure mode review cannot see, and check 1 cannot either. The remedy at a
   call site is the GitHub blob URL, which ``docs/threat-model.md`` and
   ``docs/external-idp.md`` use.
4. ``unrewritable-on-site`` — a ``.md`` link from a published page whose spelling
   the rewrite plugin's own pattern does not match, so it keeps its ``.md``
   suffix on a site that serves directory URLs.
5. ``unreadable-region`` — a code fence that swallows content, and
   ``unreadable-file`` for one that will not decode. Both are this file's *own*
   coverage rather than a style rule; see :func:`fence_defects`.

**The published set is derived by running ``docs-site/setup.sh``**, against a
temporary root whose ``docs/`` and ``images/`` are symlinks to this checkout, and
reading back the symlinks it creates. Nothing here parses or restates that
script: a shell parse got the answer wrong on first attempt (the ``README.md``
filter applies to the top-level loop only, so three nested ``README.md`` pages
*are* published), and restating the glob list is the drift this check exists to
catch. The temporary root is discarded; this checkout is never written to.

**External ``http(s)`` URLs are never fetched.** A green result here says nothing
about whether they resolve. Fetching them would make the gate need egress, and a
blocking gate that fails on someone else's outage or redirect is worse than the
gap; ``scripts/tests/test_well_architected_doc.py`` keeps that check opt-in and
off by default for one page, behind ``CHECK_DOC_LINKS=1``.

**Anchors are slugified the way GitHub and Starlight both do it** — lower-case,
punctuation and emoji removed, spaces to hyphens, duplicates suffixed — because
Astro's Markdown pipeline uses the same ``github-slugger`` GitHub does. Where the
two could differ they do not: Starlight renders the front-matter ``title`` as the
page heading and leaves the in-body ``# Title`` in place, so its anchor survives.
The risk is not between those two renderers but between them and :func:`slugify`,
so read the note on :data:`SLUG_KEEP_CATEGORIES` before changing it.

**What this does not answer.** A *non*-``.md`` relative link from a published
page — ``../samples/lending_package.pdf``, ``./releases/`` — is left alone by the
rewrite plugin and so 404s on the site while resolving in the repository, and
check 3 is scoped to ``.md`` targets, so 34 such links pass here today. The fix
for that class belongs in the rewrite plugin, not in 34 call sites, which is why
it is named here rather than enforced here.

Usage::

    python3 scripts/check_markdown_links.py            # the whole checkout
    python3 scripts/check_markdown_links.py --root DIR # another checkout
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from posixpath import dirname as pdirname
from posixpath import join as pjoin
from posixpath import normpath
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parents[1]

#: An opening or closing code fence, with whatever follows it on the line. A
#: fence closes only on the same character, at least as long, and with nothing
#: after it — normalising every fence to three characters let a ```` ``` ````
#: nested inside a ```` ```` ```` block close the outer one, which desynchronised
#: the scan and made shell comments inside code blocks read as headings.
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*(.*)$")

#: An inline or image link, ``[text](target "title")``, whose text may wrap across
#: lines — 16 relative links in this repository do, most of them cross-page
#: anchors, and a single-line pattern reads clean over every one of them.
#:
#: Spanning lines safely takes two restrictions, because a bare ``re.DOTALL`` here
#: mis-paired the ``[0.3, 0.5)`` of a maths interval with a ``](`` several lines
#: below it. ``[`` may not appear in the text except as one balanced nested group
#: (which is what ``[![img](a)](b)`` needs), and a blank line may not appear at
#: all, since a link does not straddle a paragraph break. Note the restriction
#: that stays on :data:`CODE_SPAN` does not belong here: backticks pair across
#: lines and one stray backtick would blank a region of real prose, whereas
#: CommonMark requires ``]`` and ``(`` to be adjacent, so a newline inside link
#: text cannot pair unrelated brackets.
_NO_BLANK_LINE = r"(?!\n[ \t]*\n)"
_FLAT_TEXT = rf"(?:{_NO_BLANK_LINE}(?:[^\[\]\\]|\\.))*"
_NESTED_TEXT = rf"(?:{_NO_BLANK_LINE}(?:[^\[\]\\]|\\.|\[{_FLAT_TEXT}\]))*"
_TAIL = r"\]\(\s*<?([^)>\s]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)"

#: Two passes, unioned on where the target starts. ``[![alt](img.png)](page.md)``
#: has two targets and one pattern can only reach one of them: the flat text class
#: stops at the inner ``]`` and finds the image, the nested one consumes the image
#: whole and finds the page. With one pattern, whichever it is, the other target —
#: in the nested case the one a reader clicks — goes unchecked.
INLINE_LINK_PATTERNS = (
    re.compile(rf"!?\[{_NESTED_TEXT}{_TAIL}"),
    re.compile(rf"!?\[{_FLAT_TEXT}{_TAIL}"),
)

#: A reference definition, ``[label]: target "title"``.
REFERENCE_DEF = re.compile(r"^ {0,3}\[([^\]^]+)\]:\s+<?(\S+?)>?\s*(?:[\"'].*)?$")

#: An inline code span. Its contents are prose to a reader, not a link.
CODE_SPAN = re.compile(r"(`+)[^\n]*?\1")

#: An ATX heading, with any closing run of ``#`` trimmed. A leading run of
#: blockquote markers is consumed first (see :func:`_undent`), so ``> ## Foo`` is
#: a heading and ``> ```bash`` is a fence.
ATX_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")

#: A Setext heading's underline.
SETEXT_RULE = re.compile(r"^(=+|-+)\s*$")

#: Leading blockquote markers, which change nothing about what the line *is*.
BLOCKQUOTE = re.compile(r"^ {0,3}(?:> ?)+")

#: An explicit HTML anchor, which is a fragment target as much as a heading is.
HTML_ANCHOR = re.compile(r"""<a\s+(?:name|id)\s*=\s*["']([^"']+)["']""")

#: Unicode general categories ``github-slugger`` **keeps** in a heading slug, with
#: ``-`` and the space kept literally (``_`` arrives via ``Pc``). Everything else
#: is dropped, which is why ``## 4. Geometry / Bounding Boxes`` slugs to
#: ``4-geometry--bounding-boxes``.
#:
#: ⚠️ **``[\w\- ]`` is the wrong rule and the difference is not cosmetic.** Python's
#: ``\w`` drops combining marks and variation selectors and keeps ``No``; the
#: slugger does the opposite. ``⚠️`` is U+26A0 **plus U+FE0F**, so ``\w`` produced
#: ``-deviation-…`` where the real slug is ``️-deviation-…`` with the selector
#: retained — a broken link in this repository that passed the gate. Derived by
#: running ``github-slugger@2.0.0`` over every codepoint below U+30000 plus
#: U+E0000–E01FF and grouping the survivors by category, not by reading its source.
#:
#: Measured against that same sweep, this rule keeps **everything** the slugger
#: keeps: no fragment can be reported missing because a character was dropped
#: here. It is not the reverse of that, and cannot be without embedding the
#: slugger's 733 generated ranges: 1,478 codepoints the slugger drops are retained
#: here, all of them either script characters added to Unicode after that generated
#: class was cut or one of 130 enclosed-alphanumeric symbols. A heading using one
#: would make a *correct* link report as missing — loudly, naming file, line and
#: target — rather than letting a broken one pass.
SLUG_KEEP_CATEGORIES = frozenset(
    {"Ll", "Lm", "Lo", "Lt", "Lu", "Mc", "Me", "Mn", "Nd", "Nl", "Pc"}
)

#: The four enclosed-alphanumeric ranges the slugger keeps although they are ``So``.
SLUG_KEEP_RANGES = (
    (0x24B6, 0x24E9),
    (0x1F130, 0x1F149),
    (0x1F150, 0x1F169),
    (0x1F170, 0x1F189),
)

#: A URI scheme, which makes a target absolute and none of this file's business.
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")

#: What the rewrite plugin matches before rewriting a ``.md`` link for the site.
#: Mirrored from ``docs-site/plugins/remark-rewrite-docs-links.mjs``; the two are
#: asserted to agree by ``scripts/tests/test_markdown_links.py``.
SITE_REWRITABLE = re.compile(r"^([^#]*\.md)(#.*)?$")

#: YAML front matter, which Starlight reads and a Markdown renderer does not show.
#: Its closing ``---`` is a Setext underline under ``title: X`` to any pattern that
#: does not know about it, inventing a ``#title-…`` anchor on 123 files here.
FRONT_MATTER = re.compile(r"\A---\r?\n.*?\r?\n---[ \t]*(?=\r?\n|\Z)", re.S)


@dataclass(frozen=True)
class Finding:
    """One link this file will not vouch for."""

    path: str
    line: int
    target: str
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.target} — {self.detail}"


@dataclass(frozen=True)
class Link:
    """A link as it appears in a file."""

    line: int
    target: str


def markdown_files(root: Path) -> list[str]:
    """Every Markdown file in ``root``, from ``git``, symlinks left out.

    Untracked-but-not-ignored files are read too, so a new page's links are
    checked before it is committed rather than after — a gate whose verdict
    flips at ``git add`` time is one nobody can act on. ``--exclude-standard``
    keeps build output and ``scratch/`` out.

    ``.cline/skills/*.md`` are symlinks to ``.claude/skills/*.md``; reading both
    would report every finding in them twice, at a path the author did not write.
    """
    listed = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "*.md",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(
        rel for rel in listed.split("\0") if rel and not (root / rel).is_symlink()
    )


def _undent(line: str) -> str:
    """``line`` with any leading blockquote markers removed.

    A fence or a heading inside a blockquote is still a fence or a heading. There
    are 10 of the first and 4 of the second here, and reading them as prose would
    make a quoted code block's contents look like document structure.
    """
    return BLOCKQUOTE.sub("", line, count=1)


def strip_front_matter(text: str) -> str:
    """``text`` with its YAML front matter blanked, line numbering intact."""
    match = FRONT_MATTER.match(text)
    if not match:
        return text
    return "\n" * match.group(0).count("\n") + text[match.end() :]


def strip_fences(text: str) -> str:
    """Blank every fenced code block, keeping line numbers intact."""
    out: list[str] = []
    fence: tuple[str, int] | None = None
    for raw in text.split("\n"):
        line = _undent(raw)
        match = FENCE.match(line)
        if fence is None:
            if match:
                fence = (match.group(1)[0], len(match.group(1)))
                out.append("")
            else:
                out.append(raw)
            continue
        char, length = fence
        if (
            match
            and match.group(1)[0] == char
            and len(match.group(1)) >= length
            and not match.group(2).strip()
        ):
            fence = None
        out.append("")
    return "\n".join(out)


def fence_defects(text: str) -> list[tuple[int, str]]:
    """Places where a code fence swallows content that is not code.

    This is not a style rule; it is this file's own coverage. Everything inside a
    fence is invisible to every other check here, so a fence that never closes
    turns the rest of the file off — links, anchors and the site check alike —
    and a gate that goes quiet is worse than one that was never written. Both
    shapes below were live when this was added, across three pages, and both are
    also rendering defects: GitHub and Starlight read fences the same way, so
    each one had prose, headings and shell examples showing as one code block.

    Two shapes, and a closing fence may not carry an info string in CommonMark,
    which is what makes the second detectable:

    * A fence still open at end of file.
    * A line that would close the open fence except that it has an info string,
      i.e. ```` ```bash ```` inside a ```` ``` ```` block. Either a closer is
      missing above it, or a closer was given an info string. A genuinely nested
      example is spelled with a wider outer fence and is not reported.
    """
    defects: list[tuple[int, str]] = []
    fence: tuple[str, int, int] | None = None
    for number, raw in enumerate(strip_front_matter(text).split("\n"), 1):
        match = FENCE.match(_undent(raw))
        if not match:
            continue
        marker = match.group(1)
        if fence is None:
            fence = (marker[0], len(marker), number)
            continue
        char, length, opened = fence
        if marker[0] != char or len(marker) < length:
            continue
        if not match.group(2).strip():
            fence = None
            continue
        defects.append(
            (
                number,
                f"a code fence opened at line {opened} is still open here, so "
                f"everything between them renders as code and no link in it is "
                f"checked; the closing fence above is missing",
            )
        )
        fence = (marker[0], len(marker), number)
    if fence is not None:
        defects.append(
            (
                fence[2],
                "this code fence never closes, so the rest of the file renders "
                "as code and no link in it is checked",
            )
        )
    return defects


def _blank_code_spans(text: str) -> str:
    return CODE_SPAN.sub(lambda m: " " * (m.end() - m.start()), text)


def iter_links(text: str) -> list[Link]:
    """Every Markdown link in ``text`` that a reader could click.

    Code is removed first, in both forms. That is what keeps the illustrative
    placeholders in ``.claude/skills/*.md`` — ``docs/...md``,
    ``./<doc>.md#<anchor>`` — out of the results: they sit in fenced blocks, so
    they are not links, and no list of them has to be maintained anywhere.

    Inline links are matched over the whole document rather than line by line,
    because a link whose text wraps is still a link; the reported line is the one
    the link opens on. Reference definitions stay line-anchored, which is what
    their syntax is.
    """
    body = _blank_code_spans(strip_fences(strip_front_matter(text)))
    at_offset: dict[int, Link] = {}
    for pattern in INLINE_LINK_PATTERNS:
        for match in pattern.finditer(body):
            line = body.count("\n", 0, match.start()) + 1
            at_offset.setdefault(match.start(1), Link(line, match.group(1)))
    found = list(at_offset.values())
    for number, line in enumerate(body.split("\n"), 1):
        reference = REFERENCE_DEF.match(line)
        if reference:
            found.append(Link(number, reference.group(2)))
    return sorted(found, key=lambda link: (link.line, link.target))


def _rendered_text(heading: str) -> str:
    """A heading's text as a renderer would show it, which is what gets slugged.

    Code spans are unwrapped rather than removed — ``### `LogLevel` — what `WARN`
    turns off`` slugs from ``LogLevel — what WARN turns off`` — and the HTML-tag
    strip runs **outside** them, because ``<…>`` inside a code span is literal
    text a reader sees. Doing it the other way round deletes it, and 7 headings
    here would then reject a correct link.
    """
    out: list[str] = []
    for index, part in enumerate(re.split(r"(`+[^`]*`+)", heading)):
        if index % 2:
            out.append(part.strip("`"))
            continue
        part = re.sub(r"<[^>]+>", "", part)
        part = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", part)
        for markup in ("**", "*", "~~"):
            part = part.replace(markup, "")
        out.append(part)
    return "".join(out)


def _slug_keeps(char: str) -> bool:
    if char in "- ":
        return True
    if unicodedata.category(char) in SLUG_KEEP_CATEGORIES:
        return True
    point = ord(char)
    return any(low <= point <= high for low, high in SLUG_KEEP_RANGES)


def slugify(heading: str) -> str:
    """The fragment id GitHub and Starlight both give ``heading``.

    Lower-cased, then filtered, then spaces to hyphens — in that order and with no
    trimming, which is ``github-slugger``'s order. It matters: it strips ``⚠️``
    without touching the space beside it, so a heading opening on an emoji slugs
    to a leading hyphen. Trimming first would silently drop that hyphen and report
    every link to such a heading as broken.
    """
    text = _rendered_text(heading).lower()
    return "".join(c for c in text if _slug_keeps(c)).replace(" ", "-")


def anchors(text: str) -> set[str]:
    """Every fragment id a Markdown file exposes.

    Fenced blocks are removed so a ``# comment`` in a shell example is not a
    heading, and front matter is removed so its closing ``---`` is not a Setext
    underline under ``title: X``. Inline code spans are **kept**: ``### `LogLevel`
    — what `WARN` turns off`` slugs from its rendered text, and blanking the spans
    first reduces it to ``--what-turns-off``.
    """
    body = strip_fences(strip_front_matter(text))
    lines = [_undent(line) for line in body.split("\n")]
    seen: dict[str, int] = {}
    found: set[str] = set()
    for index, line in enumerate(lines):
        heading: str | None = None
        atx = ATX_HEADING.match(line)
        if atx:
            heading = atx.group(2)
        elif (
            index > 0
            and SETEXT_RULE.match(line)
            and lines[index - 1].strip()
            and not ATX_HEADING.match(lines[index - 1])
        ):
            heading = lines[index - 1].strip()
        if heading is not None:
            base = slugify(heading)
            repeat = seen.get(base, 0)
            seen[base] = repeat + 1
            found.add(base if repeat == 0 else f"{base}-{repeat}")
        for match in HTML_ANCHOR.finditer(line):
            found.add(match.group(1))
    return found


def is_relative(target: str) -> bool:
    """Whether ``target`` names something inside this repository.

    A bare ``#fragment`` counts: it is a link into the page it sits on, and a
    same-page anchor goes stale exactly as easily as a cross-page one. A
    root-relative ``/path`` does not — on GitHub it addresses the site, not the
    checkout, so resolving it against the repository root would be wrong.
    """
    if not target or SCHEME.match(target):
        return False
    return not target.startswith("/")


def published_pages(root: Path) -> set[str]:
    """The ``docs/`` pages ``docs-site/setup.sh`` publishes, repo-relative.

    Derived by running that script against a throwaway root, then reading the
    symlinks back. This checkout is only ever read.
    """
    # Absolute, because the symlinks below are created inside a temporary
    # directory and point back here. A relative root produced a symlink that
    # resolved relative to the sandbox, so `setup.sh` globbed nothing, every page
    # came back unpublished and check 3 turned itself off without a word.
    root = root.resolve()
    setup = root / "docs-site/setup.sh"
    if not setup.is_file():
        raise FileNotFoundError(f"{setup} is missing; the published set is underivable")
    with tempfile.TemporaryDirectory() as temporary:
        sandbox = Path(temporary)
        (sandbox / "docs-site").mkdir()
        (sandbox / "docs-site/setup.sh").write_bytes(setup.read_bytes())
        for shared in ("docs", "images"):
            os.symlink(root / shared, sandbox / shared)
        completed = subprocess.run(
            ["bash", str(sandbox / "docs-site/setup.sh")],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"docs-site/setup.sh failed, so the published set is unknown:\n"
                f"{completed.stderr.strip()}"
            )
        content = sandbox / "docs-site/src/content/docs"
        pages = set()
        for link in content.rglob("*"):
            if not link.is_symlink():
                continue
            resolved = os.path.normpath(os.path.join(link.parent, os.readlink(link)))
            pages.add(os.path.relpath(resolved, sandbox).replace(os.sep, "/"))
        published = {page for page in pages if page.endswith(".md")}
    if not published and any((root / "docs").glob("*.md")):
        raise RuntimeError(
            f"docs-site/setup.sh published nothing although {root / 'docs'} holds "
            f"pages; check 3 would be silently off"
        )
    return published


def check(root: Path | None = None) -> list[Finding]:
    """Every finding in ``root``, in file order."""
    root = root or REPO_ROOT
    files = markdown_files(root)
    published = published_pages(root)
    anchor_cache: dict[str, set[str]] = {}

    def anchors_of(rel: str) -> set[str]:
        if rel not in anchor_cache:
            try:
                anchor_cache[rel] = anchors(
                    (root / rel).read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                anchor_cache[rel] = set()
        return anchor_cache[rel]

    findings: list[Finding] = []
    for rel in files:
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            # Reported rather than skipped: a file this cannot read is a file
            # every check below silently passes.
            findings.append(
                Finding(rel, 1, rel, "unreadable-file", f"cannot be read: {error}")
            )
            continue
        here = pdirname(rel)
        for number, detail in fence_defects(text):
            findings.append(Finding(rel, number, "```", "unreadable-region", detail))
        for link in iter_links(text):
            if not is_relative(link.target):
                continue
            path, _, fragment = link.target.partition("#")
            path = path.split("?", 1)[0]
            fragment = unquote(fragment)
            if not path:
                # A same-page fragment: the anchors are this file's own.
                if fragment and fragment not in anchors_of(rel):
                    findings.append(
                        Finding(
                            rel,
                            link.line,
                            link.target,
                            "missing-anchor",
                            f"{rel} has no heading or anchor '{fragment}'",
                        )
                    )
                continue
            resolved = normpath(pjoin(here, unquote(path)))
            if resolved.startswith(".."):
                findings.append(
                    Finding(
                        rel,
                        link.line,
                        link.target,
                        "escapes-repository",
                        "resolves above the repository root",
                    )
                )
                continue
            if not (root / resolved).exists():
                findings.append(
                    Finding(
                        rel,
                        link.line,
                        link.target,
                        "missing-path",
                        f"no such file or directory: {resolved}",
                    )
                )
                continue
            if fragment and resolved.endswith(".md") and (root / resolved).is_file():
                if fragment not in anchors_of(resolved):
                    findings.append(
                        Finding(
                            rel,
                            link.line,
                            link.target,
                            "missing-anchor",
                            f"{resolved} has no heading or anchor '{fragment}'",
                        )
                    )
            if (
                rel in published
                and resolved.endswith(".md")
                and resolved.startswith("docs/")
                and resolved not in published
            ):
                findings.append(
                    Finding(
                        rel,
                        link.line,
                        link.target,
                        "unpublished-target",
                        f"{rel} is published, but {resolved} is not symlinked by "
                        "docs-site/setup.sh, so this link 404s on the docs site; "
                        "use the GitHub blob URL instead",
                    )
                )
            if (
                rel in published
                and resolved.endswith(".md")
                and not SITE_REWRITABLE.match(link.target)
            ):
                findings.append(
                    Finding(
                        rel,
                        link.line,
                        link.target,
                        "unrewritable-on-site",
                        "docs-site/plugins/remark-rewrite-docs-links.mjs leaves "
                        "this link untouched, so it 404s on the docs site",
                    )
                )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="checkout to scan (default: this one)",
    )
    parser.add_argument(
        "--count",
        action="store_true",
        help="print how much was read, even when nothing is wrong",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()

    findings = check(root)
    files = markdown_files(root)
    if args.count or findings:
        print(f"read {len(files)} Markdown files in {root}")
    for finding in findings:
        print(f"  {finding}")
    if findings:
        kinds = sorted({f.kind for f in findings})
        print(
            f"\n{len(findings)} broken link(s) in "
            f"{len({f.path for f in findings})} file(s): {', '.join(kinds)}"
        )
        return 1
    print(f"All {len(files)} Markdown files: every relative link resolves.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
