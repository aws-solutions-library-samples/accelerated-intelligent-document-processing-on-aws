# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the Markdown link gate — ``scripts/check_markdown_links.py`` (#1068).

The gate itself runs from ``make check-markdown-links``, which ``lint``,
``fastlint`` and ``lint-cicd`` all reach, so both CIs run it and
``scripts/tests/test_ci_gate_parity.py`` fails if that stops being true. This
module does not re-assert that the tree is clean — one enforcement path is the
point — it asserts the checker **can fail**, and fails for the right reasons.

Two properties are worth more than the rest, because they are the two ways a gate
like this quietly stops working:

* **Discovery is not narrow.** The defect this repository keeps re-finding is a
  gate whose scope is a path list: ``ruff.toml``'s five bare directory names
  (#975), the template glob list that missed five directories. A link gate that
  reads ``docs/`` only would pass over the ``CHANGELOG``, every ``README.md``
  under ``nested/`` and ``feature-platform/``, and the skill files — all of which
  held findings. So the real tree is measured for reach, not just for cleanliness.
* **The published set is derived, not restated.** A first attempt parsed
  ``docs-site/setup.sh`` and got the answer wrong: the ``README.md`` filter is
  inside the top-level loop only, so three nested ``README.md`` pages *are*
  published. :func:`test_nested_readmes_are_published` pins that, because it is
  the case a plausible reading of the script gets backwards.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_markdown_links as gate  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Trees that hold Markdown a ``docs/``-scoped gate would never read, one entry
#: per tree and each one a place a finding was actually sitting. Reach into each
#: is asserted individually so the failure names the tree that went dark.
REACH_PROBES = (
    "CHANGELOG.md",
    "README.md",
    "CLAUDE.md",
    ".claude/skills/documentation.md",
    "docs/testing.md",
    "lib/idp_common_pkg/idp_common/reporting/README.md",
    "scripts/sdlc/docs/CI_TEST_COVERAGE.md",
    "security/threat-modeling/deliverables/executive-summary.md",
    "benchmarks/results/v0.6.7/detection-real-corpora/README.md",
    "notebooks/usecase-specific-examples/multi-page-bank-statement/README.md",
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A throwaway repository shaped enough for the gate to run in it.

    ``docs-site/setup.sh`` is **copied from this repository**, not written here:
    the point of the site half is that the published set comes from the real
    script, and a fixture that invented its own would test the fixture.
    """
    root = tmp_path / "checkout"
    (root / "docs-site").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "images").mkdir()
    (root / "images/logo.png").write_bytes(b"")
    (root / "docs-site/setup.sh").write_bytes(
        (REPO_ROOT / "docs-site/setup.sh").read_bytes()
    )
    _git(root, "init", "-q")
    return root


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------- check 1: paths


@pytest.mark.unit
def test_a_broken_relative_path_is_reported_with_file_line_and_target(
    checkout: Path,
) -> None:
    _write(
        checkout,
        "README.md",
        "# Title\n\nintro\n\nSee [the guide](./docs/gone.md) for more.\n",
    )
    findings = gate.check(checkout)
    assert len(findings) == 1, findings
    (finding,) = findings
    assert finding.path == "README.md"
    assert finding.line == 5
    assert finding.target == "./docs/gone.md"
    assert finding.kind == "missing-path"
    assert "docs/gone.md" in str(finding)


@pytest.mark.unit
def test_a_resolving_path_is_not_reported(checkout: Path) -> None:
    _write(checkout, "docs/here.md", "# Here\n")
    _write(checkout, "README.md", "See [here](./docs/here.md).\n")
    assert gate.check(checkout) == []


@pytest.mark.unit
def test_a_directory_target_resolves(checkout: Path) -> None:
    _write(checkout, "docs/guide/index.md", "# Index\n")
    _write(checkout, "README.md", "See [the guide dir](./docs/guide/).\n")
    assert gate.check(checkout) == []


@pytest.mark.unit
def test_a_path_that_climbs_out_of_the_repository_is_reported(
    checkout: Path,
) -> None:
    """``../../../../../docs/x.md`` from four levels deep left the checkout.

    It resolved to a real file on the author's machine, one directory above the
    repository root, which is why it was written and why no reader noticed.
    """
    _write(checkout, "a/b/README.md", "See [x](../../../outside.md).\n")
    kinds = [f.kind for f in gate.check(checkout)]
    assert kinds == ["escapes-repository"]


@pytest.mark.unit
def test_an_external_url_is_never_fetched_or_reported(checkout: Path) -> None:
    """No egress, by construction: an http(s) target is not a path at all.

    A green run therefore says nothing about whether external links resolve, and
    the module docstring says so where somebody reading the gate will see it.
    """
    _write(
        checkout,
        "README.md",
        "[a](https://example.invalid/nope)\n"
        "[b](http://example.invalid/nope)\n"
        "[c](mailto:nobody@example.invalid)\n"
        "[d](/absolute/site/path)\n",
    )
    assert gate.check(checkout) == []
    assert "never fetched" in (gate.__doc__ or "")


# ------------------------------------------------------------- check 2: anchors


@pytest.mark.unit
def test_a_missing_anchor_is_reported(checkout: Path) -> None:
    _write(checkout, "docs/target.md", "# Target\n\n## Present\n")
    _write(checkout, "README.md", "See [it](./docs/target.md#absent).\n")
    findings = gate.check(checkout)
    assert [f.kind for f in findings] == ["missing-anchor"]
    assert "absent" in findings[0].detail


@pytest.mark.unit
def test_a_same_page_anchor_is_checked_against_its_own_headings(
    checkout: Path,
) -> None:
    _write(checkout, "README.md", "# T\n\n## Real Heading\n\n[up](#real-heading)\n")
    assert gate.check(checkout) == []
    _write(checkout, "README.md", "# T\n\n## Real Heading\n\n[up](#reel-heading)\n")
    assert [f.kind for f in gate.check(checkout)] == ["missing-anchor"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("heading_line", "expected"),
    [
        # Inline code in a heading slugs from its rendered text, so the backticks
        # vanish and the em dash becomes nothing -- leaving two hyphens from the
        # two spaces that surrounded it. Blanking code spans before reading
        # headings reduced this one to "--what-turns-off".
        ("### `LogLevel` — what `WARN` turns off", "loglevel--what-warn-turns-off"),
        # A numbered heading keeps its number, which is what made two CHANGELOG
        # links to this page wrong in a way no reader would guess.
        ("## 4. Geometry / Bounding Boxes", "4-geometry--bounding-boxes"),
        ("## 8. Large-Document Guidance", "8-large-document-guidance"),
        # A dotted identifier loses the dot rather than the segment.
        (
            "### CHAT.T06: Client-Supplied Caller Identity",
            "chatt06-client-supplied-caller-identity",
        ),
        # The one a hand-rolled slugifier gets wrong, and the reason
        # test_slugification_matches_the_real_package_over_the_whole_tree exists.
        # "⚠️" is U+26A0 *plus* U+FE0F: the slugger drops the first and KEEPS the
        # variation selector, so the slug opens on an invisible character and then
        # the hyphen from the space. A rule built on `\w` drops it, giving a slug
        # one codepoint shorter that looks identical in every diff and terminal.
        # This repository has 26 such headings, and a link written against the
        # shorter form is broken on GitHub and on the site alike.
        ("## ⚠️ Read this first", "️-read-this-first"),
        # Combining marks survive for the same reason, decomposed or not.
        ("## Café notes", "café-notes"),
        # `No` is the other direction: `\w` keeps a superscript, the slugger does
        # not.
        ("## Area m² budget", "area-m-budget"),
        (
            "#### Option B: No-UI (`--headless`) + Jobs API",
            "option-b-no-ui---headless--jobs-api",
        ),
        ("## Underscores_survive", "underscores_survive"),
        ("## Bold **matters** not", "bold-matters-not"),
        # The tag strip runs outside code spans, so `<tag>` a reader SEES is kept
        # while a real HTML tag is not.
        ("## Use `<placeholder>` here", "use-placeholder-here"),
        ("## Line<br/>break", "linebreak"),
    ],
)
def test_slugification_matches_github_slugger(heading_line: str, expected: str) -> None:
    """Both renderers use ``github-slugger``, so one implementation serves both.

    Astro's Markdown pipeline — what Starlight builds these pages with — slugs
    headings with the same package GitHub does, and Starlight leaves the in-body
    ``# Title`` in place beside the front-matter title, so its anchor survives
    too. Where the two could have diverged they do not, which is why this gate
    validates anchors at all: on a blocking gate a wrong slugifier is worse than
    no anchor check.

    Driven through :func:`anchors` rather than :func:`slugify` directly, so the
    heading-recognition half is exercised too: a slugifier that is right about a
    string it is never handed is no use.
    """
    assert gate.anchors(heading_line) == {expected}


def _installed_slugger() -> Path | None:
    """``github-slugger``'s ``index.js``, at the version ``docs-site`` pins.

    ``node_modules`` is not tracked, so this is present on a machine that has run
    ``npm install`` in ``docs-site/`` and absent in CI. A git **worktree** has its
    own ``docs-site`` with no ``node_modules``, so the main checkout's copy is used
    as a fallback — and because that copy could be at any revision, its version is
    checked against *this* tree's lock file rather than assumed. Comparing against
    the wrong version of the authority is worse than not comparing.
    """
    candidates = [REPO_ROOT / "docs-site/node_modules/github-slugger"]
    common = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if common:
        main = (REPO_ROOT / common).resolve().parent
        candidates.append(main / "docs-site/node_modules/github-slugger")

    lock = json.loads((REPO_ROOT / "docs-site/package-lock.json").read_text())
    pinned = lock["packages"]["node_modules/github-slugger"]["version"]
    for candidate in candidates:
        entry = candidate / "index.js"
        manifest = candidate / "package.json"
        if not (entry.is_file() and manifest.is_file()):
            continue
        installed = json.loads(manifest.read_text())["version"]
        assert installed == pinned, (
            f"{manifest} is github-slugger {installed} but docs-site/package-lock."
            f"json pins {pinned}; this comparison would measure the wrong authority"
        )
        return entry
    return None


def _slug_with_the_real_package(rendered: list[str]) -> list[str] | None:
    """``github-slugger``'s answer for each string, or None if it is not here."""
    real = _installed_slugger()
    if real is None or not shutil.which("node"):
        return None
    script = (
        f"import {{slug}} from {json.dumps(str(real))};"
        "let rows='';process.stdin.on('data',d=>rows+=d).on('end',()=>"
        "process.stdout.write(JSON.stringify(JSON.parse(rows).map(s=>slug(s)))));"
    )
    with tempfile.TemporaryDirectory() as temporary:
        entry = Path(temporary) / "slug.mjs"
        entry.write_text(script, encoding="utf-8")
        done = subprocess.run(
            ["node", str(entry)],
            input=json.dumps(rendered),
            capture_output=True,
            text=True,
            check=False,
        )
    if done.returncode != 0:
        pytest.fail(f"could not run github-slugger: {done.stderr.strip()}")
    return json.loads(done.stdout)


@pytest.mark.unit
def test_slugification_matches_the_real_package_over_the_whole_tree() -> None:
    """Every heading in the repository, through both implementations.

    **A table of hand-written cases cannot establish this and must not be read as
    doing so.** Whoever writes the table writes the expected values, so a table
    agrees with the implementation by construction and disagrees with the package
    silently — and the specific way it goes wrong is invisible: ``️-read-this`` and
    ``-read-this`` differ by one zero-width codepoint, identical in a diff, a
    terminal and a review. A gate whose central claim is "slugified the way GitHub
    does it" has to be measured against the thing it claims to match, over real
    input, and the table above is pinned to values read off this same package.

    ``ATX_HEADING``/``SETEXT_RULE`` recognition and :func:`_rendered_text` are
    shared by both sides, deliberately: what is under test is the character class,
    the case folding and the space handling, which is where the divergence was.
    """
    headings: list[tuple[str, int, str]] = []
    for rel in gate.markdown_files(REPO_ROOT):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        body = gate.strip_fences(gate.strip_front_matter(text))
        lines = [gate._undent(line) for line in body.split("\n")]
        for index, line in enumerate(lines):
            atx = gate.ATX_HEADING.match(line)
            if atx:
                headings.append((rel, index + 1, atx.group(2)))
            elif (
                index > 0
                and gate.SETEXT_RULE.match(line)
                and lines[index - 1].strip()
                and not gate.ATX_HEADING.match(lines[index - 1])
            ):
                headings.append((rel, index + 1, lines[index - 1].strip()))

    assert len(headings) > 5000, (
        f"only {len(headings)} headings found; the heading scan is broken, so this "
        f"comparison would pass on almost nothing"
    )
    theirs = _slug_with_the_real_package(
        [gate._rendered_text(heading) for _, _, heading in headings]
    )
    if theirs is None:
        pytest.skip(
            "github-slugger is not installed here (docs-site/node_modules is not "
            "tracked); run `npm install` in docs-site/ to enable this comparison"
        )
    divergent = [
        (rel, line, heading, want, gate.slugify(heading))
        for (rel, line, heading), want in zip(headings, theirs, strict=True)
        if gate.slugify(heading) != want
    ]
    assert not divergent, "slugify() disagrees with github-slugger on:\n" + "\n".join(
        f"  {rel}:{line} {heading!r}\n"
        f"      github-slugger: {want!r}\n"
        f"      slugify():      {mine!r}"
        for rel, line, heading, want, mine in divergent[:10]
    )


@pytest.mark.unit
def test_a_variation_selector_survives_slugging() -> None:
    """The one character whose treatment decides 26 of this repository's anchors.

    They open on an emoji written with U+FE0F, and the slug keeps the selector.
    Dropping it produces a slug that is visually identical in a diff and in a
    terminal, so nothing about the symptom would point at the cause.
    """
    assert gate.slugify("⚠️ Read this") == "️-read-this"
    assert gate.slugify("Area m²") == "area-m"


@pytest.mark.unit
def test_repeated_headings_get_the_suffixes_github_gives_them() -> None:
    text = "## Setup\n\n## Setup\n\n## Setup\n"
    assert gate.anchors(text) == {"setup", "setup-1", "setup-2"}


@pytest.mark.unit
def test_an_explicit_html_anchor_is_a_target() -> None:
    assert "manual" in gate.anchors('<a name="manual"></a>\n\ntext\n')
    assert "by-id" in gate.anchors('<a id="by-id"></a>\n')


@pytest.mark.unit
def test_a_setext_heading_is_a_target() -> None:
    assert "older-style" in gate.anchors("Older Style\n-----------\n")


@pytest.mark.unit
def test_a_file_that_cannot_be_decoded_is_reported_not_skipped(
    checkout: Path,
) -> None:
    """Skipping it would make every check below pass on it, quietly."""
    (checkout / "BROKEN.md").write_bytes(b"# T\n\xff\xfe not utf-8\n")
    findings = gate.check(checkout)
    assert [f.kind for f in findings] == ["unreadable-file"]
    assert findings[0].path == "BROKEN.md"


@pytest.mark.unit
def test_front_matter_is_not_a_heading() -> None:
    """``title: X`` followed by ``---`` is a Setext h2 to anything that does not
    know about front matter, which invented a ``#title-…`` anchor on 123 files
    here. None was linked to, so nothing failed — a false-negative surface rather
    than a wrong answer, and the kind that only grows."""
    page = '---\ntitle: "Web UI"\n---\n\n# Real Heading\n'
    assert gate.anchors(page) == {"real-heading"}


@pytest.mark.unit
def test_a_heading_or_fence_inside_a_blockquote_is_still_one() -> None:
    """10 quoted fences and 4 quoted headings; reading either as prose inverts
    which regions the gate can see."""
    assert gate.anchors("> ## Quoted Heading\n") == {"quoted-heading"}
    swallowed = "> ```bash\n> [x](./gone.md)\n> ```\n\n## After\n"
    assert gate.iter_links(swallowed) == []
    assert gate.anchors(swallowed) == {"after"}


# -------------------------------------------------------------- wrapped link text


@pytest.mark.unit
def test_a_link_whose_text_wraps_is_still_a_link(checkout: Path) -> None:
    """16 relative links here wrap, most of them cross-page anchors.

    Five point from ``docs/creating-custom-test-sets.md`` into
    ``docs/test-studio.md#…``; a heading rename there would have broken all five
    with the gate reporting clean.
    """
    _write(
        checkout,
        "README.md",
        "See [the rather long\nlink text here](./gone.md) for more.\n",
    )
    findings = gate.check(checkout)
    assert [f.kind for f in findings] == ["missing-path"]
    assert findings[0].line == 1, "the line reported is where the link opens"


@pytest.mark.unit
def test_a_bracketed_interval_is_not_paired_with_a_later_link(checkout: Path) -> None:
    """The reason the multi-line pattern is not simply ``re.DOTALL``.

    ``docs/benchmarking/config-guidance.md`` writes ``anywhere in [0.3, 0.5)`` and
    has a real link some lines below. A dot-matches-all pattern joins the two and
    reports the interval's tail as a link target.
    """
    _write(
        checkout,
        "docs/here.md",
        "Values anywhere in [0.3, 0.5) are fine.\n\nsome prose\n\n"
        "and then [a real link](./here.md).\n",
    )
    assert gate.check(checkout) == []
    assert [
        link.target for link in gate.iter_links((checkout / "docs/here.md").read_text())
    ] == ["./here.md"]


@pytest.mark.unit
def test_a_link_does_not_straddle_a_blank_line(checkout: Path) -> None:
    _write(checkout, "README.md", "an [orphan bracket]\n\nand (./gone.md) later\n")
    assert gate.check(checkout) == []


@pytest.mark.unit
def test_the_outer_target_of_a_linked_image_is_checked(checkout: Path) -> None:
    """``[![alt](img.png)](page.md)`` has two targets and both must resolve."""
    _write(checkout, "README.md", "[![alt](./missing.png)](./missing.md)\n")
    targets = {f.target for f in gate.check(checkout)}
    assert targets == {"./missing.png", "./missing.md"}


# -------------------------------------------------------------- code is not prose


@pytest.mark.unit
def test_a_link_inside_a_fenced_block_is_not_a_link(checkout: Path) -> None:
    """This is what keeps the skill files' illustrative placeholders quiet.

    ``.claude/skills/prepare-changelog.md`` shows ``[text](docs/feature.md)`` as
    a worked example of a changelog entry, and ``documentation.md`` shows
    ``./architecture.md``. Both sit in fenced blocks, so they are not links —
    which means no list of placeholder spellings has to be maintained anywhere,
    and a placeholder that moves out of its fence starts being checked.
    """
    _write(
        checkout,
        "README.md",
        "# T\n\n```markdown\nSee [x](docs/never-existed.md) and [y](./gone.md)\n```\n",
    )
    assert gate.check(checkout) == []


@pytest.mark.unit
def test_a_nested_fence_does_not_close_the_outer_block(checkout: Path) -> None:
    """A four-backtick block containing a three-backtick one.

    Normalising every fence to three characters let the inner fence close the
    outer block. The scan then ran inverted for the rest of the file: real
    headings were read as code and ``# comment`` lines inside code blocks were
    read as headings, which silently lost 33 anchor results and invented others.
    """
    _write(
        checkout,
        "README.md",
        "````markdown\n```\n[x](./gone.md)\n```\n````\n\n## Real\n\n[a](#real)\n",
    )
    assert gate.check(checkout) == []


@pytest.mark.unit
def test_a_link_inside_an_inline_code_span_is_not_a_link(checkout: Path) -> None:
    _write(checkout, "README.md", "Write `[x](./gone.md)` to link.\n")
    assert gate.check(checkout) == []


@pytest.mark.unit
def test_a_heading_keeps_its_inline_code_when_slugged(checkout: Path) -> None:
    _write(checkout, "docs/t.md", "### `flag` — what it does\n")
    _write(checkout, "README.md", "[a](./docs/t.md#flag--what-it-does)\n")
    assert gate.check(checkout) == []


# --------------------------------------------- the gate's own reading coverage


@pytest.mark.unit
def test_a_fence_that_never_closes_is_reported(checkout: Path) -> None:
    """Otherwise this gate goes silent over the rest of the file.

    Two pages had one. Everything after the stray fence — links, anchors,
    headings — was invisible to every check here, and invisible is exactly how a
    gate stops working without anybody noticing.
    """
    _write(checkout, "README.md", "# T\n\n```bash\necho hi\n\n[x](./gone.md)\n")
    findings = gate.check(checkout)
    assert [f.kind for f in findings] == ["unreadable-region"]
    assert findings[0].line == 3
    assert "never closes" in findings[0].detail


@pytest.mark.unit
def test_a_missing_closer_before_the_next_block_is_reported(checkout: Path) -> None:
    """``` ```bash ``` inside a ``` ``` ``` block cannot be a closing fence.

    CommonMark forbids an info string on a closer, so the second block's opener
    is swallowed by the first and the prose between them renders as code. Three
    pages had this; on one it hid two headings and a ``sql`` example.
    """
    _write(
        checkout,
        "README.md",
        "```bash\nfirst\n\n## A Heading\n\n```bash\nsecond\n```\n",
    )
    findings = gate.check(checkout)
    assert [f.kind for f in findings] == ["unreadable-region"]
    assert findings[0].line == 6
    assert "still open" in findings[0].detail


@pytest.mark.unit
def test_a_correctly_nested_wider_fence_is_not_reported(checkout: Path) -> None:
    """The legitimate spelling of a fenced block that shows fenced Markdown."""
    _write(
        checkout,
        "README.md",
        "````markdown\n```bash\necho hi\n```\n````\n\n## Real\n\n[a](#real)\n",
    )
    assert gate.check(checkout) == []


@pytest.mark.unit
def test_no_file_in_the_tree_hides_content_behind_a_fence() -> None:
    """A direct read of the real tree, not a by-product of the link results.

    The link findings would be clean either way: content inside a phantom code
    block produces no findings precisely because it is not read.
    """
    unreadable = [
        (rel, line)
        for rel in gate.markdown_files(REPO_ROOT)
        for line, _ in gate.fence_defects(
            (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        )
    ]
    assert not unreadable, f"code fences swallow content at {unreadable}"


# -------------------------------------------------------------------- discovery


@pytest.mark.unit
@pytest.mark.parametrize("rel", REACH_PROBES)
def test_discovery_reaches_markdown_outside_docs(rel: str) -> None:
    """Every one of these is a file a ``docs/``-only gate would not read.

    Parametrised one tree per case so a failure names the tree rather than a
    count. Each of these paths held at least one broken link when the gate was
    written, which is the answer to "would narrowing discovery matter?".
    """
    assert (REPO_ROOT / rel).is_file(), f"{rel} moved; re-point this probe"
    assert rel in gate.markdown_files(REPO_ROOT)


@pytest.mark.unit
def test_discovery_reads_a_plausible_share_of_the_tree() -> None:
    """A count floor, so a discovery change that finds almost nothing fails here.

    ``git ls-files`` returning an empty list is the failure mode that makes every
    other assertion in this module vacuously true.
    """
    files = gate.markdown_files(REPO_ROOT)
    assert len(files) > 250, f"only {len(files)} Markdown files discovered"
    assert all(f.endswith(".md") for f in files)


@pytest.mark.unit
def test_a_readme_deep_under_an_unusual_tree_is_discovered(checkout: Path) -> None:
    """Depth and directory name must not matter. They have before.

    ``nested/``, ``feature-platform/`` and ``samples/`` are three of the five
    trees the template gates' glob list missed, and a link gate with the same
    shape of scope would miss the same three.
    """
    for tree in (
        "nested/api-resolvers/src/lambda/test_runner",
        "feature-platform/pii-anonymizer/hook",
        "samples/lambda-hook-inference",
    ):
        _write(checkout, f"{tree}/README.md", "See [x](./missing-here.md).\n")
    findings = gate.check(checkout)
    assert len(findings) == 3, findings
    assert {f.path.split("/")[0] for f in findings} == {
        "nested",
        "feature-platform",
        "samples",
    }


@pytest.mark.unit
def test_a_symlinked_markdown_file_is_read_once_at_its_real_path() -> None:
    """``.cline/skills/*.md`` are symlinks to ``.claude/skills/*.md``.

    Reading both would report every finding twice, the second time at a path
    whose content nobody wrote.
    """
    files = set(gate.markdown_files(REPO_ROOT))
    symlinked = [
        rel
        for rel in subprocess.run(
            ["git", "ls-files", "--", ".cline/skills"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        if rel.endswith(".md")
    ]
    assert symlinked, ".cline/skills holds no Markdown; re-point this probe"
    assert all((REPO_ROOT / rel).is_symlink() for rel in symlinked)
    assert not files & set(symlinked)
    assert ".claude/skills/documentation.md" in files


@pytest.mark.unit
def test_an_uncommitted_file_is_checked(checkout: Path) -> None:
    """A verdict that flips at ``git add`` time is one nobody can act on.

    The ``checkout`` fixture never commits, so every other test here rests on
    this; making it explicit is what stops the reach of the gate from silently
    becoming "only what is committed".
    """
    _write(checkout, "NEW.md", "[x](./gone.md)\n")
    assert "NEW.md" in gate.markdown_files(checkout)
    assert [f.path for f in gate.check(checkout)] == ["NEW.md"]


@pytest.mark.unit
def test_an_ignored_file_is_not_checked(checkout: Path) -> None:
    _write(checkout, ".gitignore", "scratch/\n")
    _write(checkout, "scratch/notes.md", "[x](./gone.md)\n")
    assert gate.check(checkout) == []


# ------------------------------------------------- check 3: the published site


@pytest.mark.unit
def test_the_published_set_is_derived_and_non_empty() -> None:
    pages = gate.published_pages(REPO_ROOT)
    assert len(pages) > 100, f"only {len(pages)} published pages derived"
    assert "docs/architecture.md" in pages
    assert "docs/testing.md" in pages
    assert "docs/extensions/auto-optimizer.md" in pages
    assert "docs/benchmarking/config-guidance.md" in pages


@pytest.mark.unit
def test_nested_readmes_are_published() -> None:
    """The case a plausible reading of ``setup.sh`` gets backwards.

    The ``README.md`` filter sits inside the top-level ``docs/*.md`` loop, so
    ``docs/README.md`` is not published and the three nested ones are. Deriving
    the set by running the script is what gets this right; a regex over the
    script's globs plus one ``README.md`` rule got it wrong in both directions.
    """
    pages = gate.published_pages(REPO_ROOT)
    assert "docs/README.md" not in pages
    for nested in (
        "docs/benchmarking/releases/README.md",
        "docs/benchmarking/studies/README.md",
        "docs/release-validation/README.md",
    ):
        assert nested in pages, nested


@pytest.mark.unit
def test_pages_outside_the_symlinked_set_are_not_published() -> None:
    """``docs/planning/`` and ``docs/proposals/`` reach no site route.

    Which is exactly why a published page must not link to them relatively:
    ``docs/rbac.md`` and ``docs/well-architected.md`` both cite
    ``docs/planning/identity-pool-group-scoping-plan.md`` through the GitHub
    blob URL, and that is the remedy this check pushes a call site toward.
    """
    pages = gate.published_pages(REPO_ROOT)
    tracked = subprocess.run(
        ["git", "ls-files", "--", "docs/planning", "docs/proposals"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    unpublished = [rel for rel in tracked if rel.endswith(".md")]
    assert unpublished, "docs/planning and docs/proposals are gone; re-point this"
    assert not set(unpublished) & pages


@pytest.mark.unit
def test_a_published_page_linking_to_an_unpublished_page_is_reported(
    checkout: Path,
) -> None:
    """The finding no in-repo check can produce, and review does not see.

    The target exists, so check 1 passes and the link works on GitHub. The
    rewrite plugin leaves it site-relative because it never escapes ``docs/``,
    and there is no page there, so the published reader gets a 404.
    """
    _write(checkout, "docs/published.md", "# P\n\nSee [plan](./planning/p.md).\n")
    _write(checkout, "docs/planning/p.md", "# Plan\n")
    findings = gate.check(checkout)
    assert [f.kind for f in findings] == ["unpublished-target"]
    assert findings[0].path == "docs/published.md"
    assert "404" in findings[0].detail
    assert "blob" in findings[0].detail


@pytest.mark.unit
def test_an_unpublished_page_may_link_to_another_unpublished_page(
    checkout: Path,
) -> None:
    """Check 3 is a property of the *linking* page, not of the target.

    ``docs/planning/a.md`` is read on GitHub or not at all, so a relative link
    between two such pages is correct and must not be reported. Attaching the
    reason to the target instead would have failed both of these.
    """
    _write(checkout, "docs/planning/a.md", "See [b](./b.md).\n")
    _write(checkout, "docs/planning/b.md", "# B\n")
    assert gate.check(checkout) == []


@pytest.mark.unit
def test_a_published_page_may_link_to_another_published_page(
    checkout: Path,
) -> None:
    _write(checkout, "docs/one.md", "See [two](./two.md).\n")
    _write(checkout, "docs/two.md", "# Two\n")
    assert gate.check(checkout) == []


@pytest.mark.unit
def test_a_published_page_linking_outside_docs_is_not_reported(
    checkout: Path,
) -> None:
    """The plugin sends those to the GitHub blob URL, so they resolve on the site."""
    _write(checkout, "docs/page.md", "See [root](../CONTRIBUTING.md).\n")
    _write(checkout, "CONTRIBUTING.md", "# Contributing\n")
    assert gate.check(checkout) == []


@pytest.mark.unit
def test_an_anchor_the_rewrite_plugin_cannot_parse_is_reported(
    checkout: Path,
) -> None:
    """The plugin requires the URL to *end* at ``.md`` or ``.md#…``.

    A query string is the live shape that misses: the path resolves in the
    repository, so checks 1 and 2 pass, and the plugin returns early and leaves the
    link pointing at a ``.md`` file on a site that serves directory URLs. There are
    none today; without this test the branch would be unexercised code claiming a
    check.
    """
    _write(checkout, "docs/a.md", "See [b](./b.md?v=2#real).\n")
    _write(checkout, "docs/b.md", "# B\n\n## Real\n")
    kinds = [f.kind for f in gate.check(checkout)]
    assert kinds == ["unrewritable-on-site"], kinds


@pytest.mark.unit
def test_the_published_set_derivation_fails_loudly_if_setup_sh_goes_missing(
    tmp_path: Path,
) -> None:
    """Silence here would turn check 3 off while every other check stayed green."""
    (tmp_path / "docs").mkdir()
    with pytest.raises(FileNotFoundError):
        gate.published_pages(tmp_path)


@pytest.mark.unit
def test_deriving_the_published_set_does_not_write_to_the_checkout(
    checkout: Path,
) -> None:
    """``setup.sh`` creates symlinks; it must create none of them in here."""
    _write(checkout, "docs/page.md", "# P\n")
    before = sorted(p.relative_to(checkout).as_posix() for p in checkout.rglob("*"))
    gate.published_pages(checkout)
    after = sorted(p.relative_to(checkout).as_posix() for p in checkout.rglob("*"))
    assert before == after
    assert not (checkout / "docs-site/src").exists()


@pytest.mark.unit
def test_the_site_rewrite_pattern_matches_the_plugin_it_mirrors() -> None:
    """:data:`SITE_REWRITABLE` is a copy of a regex in a ``.mjs`` file.

    Two copies of one pattern in two languages is exactly the drift this gate
    exists to catch elsewhere, so the copy is read back out of the plugin rather
    than trusted. Narrowing the anchor class on either side has a specific cost:
    an anchor the plugin will not parse is one it leaves pointing at a ``.md`` path
    on a site that serves directory URLs, and ``github-slugger`` produces anchors
    outside ``[a-zA-Z0-9_-]`` for any heading opening on an emoji.
    """
    plugin = (REPO_ROOT / "docs-site/plugins/remark-rewrite-docs-links.mjs").read_text()
    found = re.search(r"url\.match\(/([^/]+)/\)", plugin)
    assert found, "the plugin no longer matches link URLs with a literal regex"
    assert found.group(1) == gate.SITE_REWRITABLE.pattern, (
        f"the plugin matches {found.group(1)!r} but SITE_REWRITABLE is "
        f"{gate.SITE_REWRITABLE.pattern!r}; the gate would report a link the site "
        f"handles, or pass one it does not"
    )


@pytest.mark.unit
def test_an_emoji_anchor_is_rewritable_for_the_site() -> None:
    """Both halves of the trap have to be open at once for a link to be writable.

    The anchor a reader needs for an emoji heading contains U+FE0F. If the site
    pattern rejected it, that spelling would draw an ``unrewritable-on-site``
    finding while the shorter one drew ``missing-anchor`` — no spelling both working
    and passing, which is a gate with no remedy rather than a gate.
    """
    assert gate.SITE_REWRITABLE.match("./v0.6.6.md#️-deviation-from-the-thing")


# ---------------------------------------------------------------- how it is run


@pytest.mark.unit
@pytest.mark.parametrize("target", ["lint", "fastlint"])
def test_the_gate_is_a_prerequisite_of_the_local_lint_targets(target: str) -> None:
    """``lint-cicd`` coverage is derived *from* ``make lint``, so removing the gate
    from both prerequisite lists is invisible to
    ``scripts/tests/test_ci_gate_parity.py``: its universe shrinks with the change.
    The failure direction is mild — a developer stops seeing it before pushing —
    but ``CLAUDE.md`` states it runs there, so something has to hold it."""
    makefile = (REPO_ROOT / "Makefile").read_text()
    line = re.search(rf"^{target}:[^\n]*", makefile, re.M)
    assert line, f"no `{target}:` target in the Makefile"
    assert "check-markdown-links" in line.group(0), (
        f"`make {target}` no longer depends on check-markdown-links"
    )


@pytest.mark.unit
def test_the_gate_is_invoked_from_lint_cicd() -> None:
    """Which is what puts it in both CI configurations."""
    makefile = (REPO_ROOT / "Makefile").read_text()
    recipe = makefile.split("lint-cicd:", 1)[1].split("\ncheck-lint-debt:", 1)[0]
    assert "make check-markdown-links" in recipe


# ------------------------------------------------------------------- the CLI


@pytest.mark.unit
def test_the_cli_exits_non_zero_and_names_every_finding(
    checkout: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(checkout, "README.md", "line one\n\n[x](./gone.md)\n")
    assert gate.main(["--root", str(checkout)]) == 1
    out = capsys.readouterr().out
    assert "README.md:3" in out
    assert "./gone.md" in out
    assert "1 broken link(s)" in out


@pytest.mark.unit
def test_the_cli_exits_zero_on_a_clean_tree(
    checkout: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(checkout, "README.md", "nothing to see\n")
    assert gate.main(["--root", str(checkout)]) == 0
    assert "every relative link resolves" in capsys.readouterr().out


@pytest.mark.unit
def test_the_gate_is_reachable_as_a_script() -> None:
    """``make check-markdown-links`` runs it this way; an import-time break here
    would surface as a lint failure with no findings, which reads as a defect in
    the tree rather than in the gate."""
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/check_markdown_links.py"), "--count"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Markdown files" in completed.stdout
