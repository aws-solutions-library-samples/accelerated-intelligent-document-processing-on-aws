# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Keep ``docs/release-runbook.md`` honest about ``scripts/aws-release.sh``.

The runbook is a public guide to the release mechanism, written for anyone
publishing releases to buckets in an account they own. It describes the mechanism
with placeholders and deliberately names none of the project's own release
buckets, so this asserts both halves against the script and the Makefile rather
than against a copy of the facts:

* every option the script passes to ``idp-cli publish`` is documented, so a new
  flag in the release cannot land unexplained;
* no bucket basename, bucket name or key prefix the script publishes to appears
  on the page;
* every ``make <target>`` the page cites still exists;
* the artifact keys it names are the keys the publisher writes;
* the number of files ``make version`` stamps is the number the page claims;
* linked skills and docs resolve, and the page is reachable from the sidebar and
  the docs index.

One assertion looks at the page's own structure rather than at a source file: the eight
``### Step N`` headings must be present and in numeric order, and the five top-level
numbered sections must all be there. That catches a step deleted, renumbered or moved —
a runbook that tells you to tag before you publish is worse than none.

None of this covers the prose, and a green run should not be read as if it did. Sentences
inside a section can be edited, and whole paragraphs dropped, without failing anything
here. Guarding 600 lines of prose against itself is not worth the machinery; guarding it
against the code it describes, which is what the rest of this file does, is.

Modelled on ``test_testing_doc.py``, which guards ``docs/testing.md`` the same way.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC = REPO_ROOT / "docs" / "release-runbook.md"
SCRIPT = REPO_ROOT / "scripts" / "aws-release.sh"
PUBLISHER = REPO_ROOT / "lib" / "idp_sdk" / "idp_sdk" / "_core" / "publish.py"
CLI = REPO_ROOT / "lib" / "idp_cli_pkg" / "idp_cli" / "cli.py"
MAKEFILE = REPO_ROOT / "Makefile"
SIDEBAR = REPO_ROOT / "docs-site" / "astro.config.mjs"
DOCS_INDEX = REPO_ROOT / "docs" / "README.md"
VALIDATION_INDEX = REPO_ROOT / "docs" / "release-validation" / "README.md"

TARGET_RE = re.compile(r"^([a-zA-Z0-9_.-]+):", re.MULTILINE)
# Only count deliberate invocations: inline code (`make foo`) or a command at the
# start of a line inside a fenced block. Prose like "make the release" is not a
# target reference and must not be treated as one. The name character class matches
# TARGET_RE's, so a cited target with an underscore, a dot or a capital is seen too.
MAKE_CALL_RE = re.compile(r"(?:`|^)make ([a-zA-Z0-9_.-]+)", re.MULTILINE)
SKILL_PATH_RE = re.compile(r"\.claude/skills/([a-z0-9-]+\.md)")
DOC_LINK_RE = re.compile(r"\]\((\.[^)#]+)")
# The page's own spine: `### Step 1 — …` (em dash) and `## 1. …`.
STEP_HEADING_RE = re.compile(r"^### Step (\d+) — ", re.MULTILINE)
SECTION_HEADING_RE = re.compile(r"^## (\d+)\. ", re.MULTILINE)


def _doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def _script_body() -> str:
    """The script's executable lines, without comments or blank padding."""
    return "\n".join(
        line.rstrip()
        for line in SCRIPT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


@pytest.mark.unit
def test_the_page_exists_with_frontmatter_and_licence() -> None:
    text = _doc_text()
    assert text.startswith('---\ntitle: "Release Runbook"\n---'), (
        "docs/*.md needs YAML frontmatter with a title; the docs site keys off it"
    )
    assert "SPDX-License-Identifier: MIT-0" in text.split("# Release Runbook")[0]


@pytest.mark.unit
def test_the_ordered_procedure_keeps_its_shape() -> None:
    """The eight steps are all present and in order, and so are the five sections.

    Reordering is the mutation with the worst consequences and the smallest diff:
    swapping "Publish the artifacts" and "Tag and push" produces a runbook that reads
    perfectly well and tags a release that was never published.
    """
    text = _doc_text()

    steps = [int(n) for n in STEP_HEADING_RE.findall(text)]
    assert steps == list(range(1, 9)), (
        f"the runbook's `### Step N` headings read {steps}; expected 1 through 8 in "
        "numeric order. A step was deleted, renumbered, or moved relative to its "
        "neighbours — if deliberate, change this expectation in the same commit"
    )

    sections = [int(n) for n in SECTION_HEADING_RE.findall(text)]
    assert sections == list(range(1, 6)), (
        f"the top-level numbered sections read {sections}; expected 1 through "
        "5 (Preconditions, Ordered steps, Failure and recovery, What is not automated, "
        "Post-release verification checklist)"
    )


def _script_buckets() -> dict[str, str]:
    """Region -> ``<basename>-<region>``, read off each publish line.

    The basename is per line rather than global, so a bucket name built from one
    basename and every region would miss a region that publishes elsewhere.
    """
    buckets: dict[str, str] = {}
    for line in _script_body().splitlines():
        region = re.search(r"--region\s+([a-z0-9-]+)", line)
        basename = re.search(r"--bucket-basename\s+(\S+)", line)
        if region and basename:
            buckets[region.group(1)] = f"{basename.group(1)}-{region.group(1)}"
    return buckets


@pytest.mark.unit
def test_the_publish_options_the_script_uses_are_documented() -> None:
    """A flag added to the release is a change to the mechanism the page explains."""
    body = _script_body()
    assert "idp-cli publish" in body, "release script no longer runs idp-cli publish"
    options = set(re.findall(r"(--[a-z][a-z-]*)", body))
    assert options, "no idp-cli publish options parsed out of the release script"

    text = _doc_text()
    assert "scripts/aws-release.sh" in text, (
        f"{DOC.relative_to(REPO_ROOT)} must name scripts/aws-release.sh"
    )
    missing = sorted(o for o in options if f"`{o}`" not in text)
    assert not missing, (
        f"{DOC.relative_to(REPO_ROOT)} does not explain the publish options the "
        f"release script passes: {missing}"
    )


@pytest.mark.unit
def test_the_page_names_no_release_bucket_or_prefix() -> None:
    """The page is a public guide; the project's own buckets stay out of it."""
    buckets = _script_buckets()
    assert buckets, "no publish lines with --region and --bucket-basename in the script"
    body = _script_body()
    names = set(buckets.values())
    names |= set(re.findall(r"--bucket-basename\s+(\S+)", body))
    names |= set(re.findall(r"--prefix\s+(\S+)", body))

    text = _doc_text()
    named = sorted(n for n in names if n in text)
    assert not named, (
        f"{DOC.relative_to(REPO_ROOT)} names release buckets or prefixes {named}; "
        "describe them with placeholders such as <basename>-<region> and <prefix>"
    )
    assert not re.search(r"(?<!\d)\d{12}(?!\d)", text), (
        f"{DOC.relative_to(REPO_ROOT)} contains a 12-digit number that may be an AWS "
        "account id"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    [
        "idp-main.yaml",  # overwritten every release
        "idp-main-latest.json",  # the version pointer the Web UI reads
    ],
)
def test_mutable_keys_the_publisher_writes_are_documented(key: str) -> None:
    """These are the only two keys a release overwrites — the only rollback lever."""
    assert key in PUBLISHER.read_text(encoding="utf-8"), (
        f"{key!r} no longer appears in {PUBLISHER.relative_to(REPO_ROOT)}; "
        f"the runbook's rollback section describes a key that may no longer exist"
    )
    assert key in _doc_text(), (
        f"{key!r} must be documented in {DOC.relative_to(REPO_ROOT)}"
    )


@pytest.mark.unit
def test_the_cli_is_documented_as_a_consumer_of_the_floating_key() -> None:
    """`idp-cli deploy` reads the same overwritten key the Launch Stack buttons do.

    Derived from the CLI source rather than restated: if `TEMPLATE_URLS` is renamed,
    moved, or repointed at a versioned key, this fails and forces a decision about the
    runbook's "What reads it" cell and its §3.2 rollback reasoning — which discusses
    moving that key and would otherwise not mention that the CLI follows it too.
    """
    cli_text = CLI.read_text(encoding="utf-8")
    block = re.search(r"^TEMPLATE_URLS\s*=\s*\{(.*?)^\}", cli_text, re.S | re.M)
    assert block, (
        f"TEMPLATE_URLS is no longer a module-level dict in "
        f"{CLI.relative_to(REPO_ROOT)}; the runbook names it as a consumer of the "
        f"floating template key — re-check that cell"
    )

    # https://<host>/<bucket>/<key> — capture only the object key.
    keys = set(
        re.findall(r"https://[^/\s\"']+/[^/\s\"']+/([\w./-]+\.yaml)", block.group(1))
    )
    assert keys, f"no template object keys parsed out of TEMPLATE_URLS in {CLI.name}"
    assert keys == {"artifacts/genai-idp/idp-main.yaml"}, (
        f"TEMPLATE_URLS now points at {sorted(keys)} rather than the floating "
        f"idp-main.yaml key; the runbook's mutability table and §3.2 rollback "
        f"reasoning both assume the CLI follows the floating key"
    )

    text = _doc_text()
    cli_rel = str(CLI.relative_to(REPO_ROOT))
    assert cli_rel in text, (
        f"{cli_rel} must be named in {DOC.relative_to(REPO_ROOT)} as a consumer of "
        f"artifacts/genai-idp/idp-main.yaml — an operator moving that key needs "
        f"to know `idp-cli deploy` is affected"
    )
    assert "TEMPLATE_URLS" in text, (
        f"{DOC.relative_to(REPO_ROOT)} should name TEMPLATE_URLS so the reader can "
        f"find the hardcoded URLs in {cli_rel}"
    )


@pytest.mark.unit
def test_every_make_target_the_page_cites_exists() -> None:
    targets = set(TARGET_RE.findall(MAKEFILE.read_text(encoding="utf-8")))
    missing = sorted(set(MAKE_CALL_RE.findall(_doc_text())) - targets)
    assert not missing, (
        f"{DOC.relative_to(REPO_ROOT)} cites unknown make targets: {missing}"
    )


@pytest.mark.unit
def test_the_version_stamp_counts_match_the_makefile() -> None:
    """Step 1's success criterion counts files, so the count must come from the target.

    `make version` stamps the version into a set of files that has grown twice. A wrong
    count on a ✅-badged line is the kind of small untruth that makes a reader stop
    trusting the badges.
    """
    text = MAKEFILE.read_text(encoding="utf-8")
    start = text.index("\nversion:")
    recipe = text[start : text.index("\n##@ ", start)]

    package_files = recipe.count("sed -i.bak")
    printed_paths = len(re.findall(r'^\t@echo "  - ', recipe, re.MULTILINE))
    assert package_files and printed_paths, (
        "could not parse the `version` target's file list out of the Makefile"
    )

    doc = _doc_text()
    assert f"{package_files} package version files" in doc, (
        f"`make version` rewrites {package_files} package version files; "
        f"{DOC.relative_to(REPO_ROOT)} §1.1 and Step 1 must say so"
    )
    assert f"{printed_paths} paths" in doc, (
        f"the `version` target reports {printed_paths} stamped paths; "
        f"{DOC.relative_to(REPO_ROOT)} must say so"
    )


@pytest.mark.unit
def test_linked_skills_and_docs_resolve() -> None:
    text = _doc_text()
    missing_skills = sorted(
        name
        for name in set(SKILL_PATH_RE.findall(text))
        if not (REPO_ROOT / ".claude" / "skills" / name).exists()
    )
    assert not missing_skills, f"referenced skills do not exist: {missing_skills}"

    missing_docs = sorted(
        target
        for target in set(DOC_LINK_RE.findall(text))
        if not (DOC.parent / target).exists()
    )
    assert not missing_docs, f"relative doc links do not resolve: {missing_docs}"


@pytest.mark.unit
def test_the_page_is_reachable() -> None:
    """An unlinked runbook is one nobody finds under pressure."""
    assert '{ label: "Release Runbook", slug: "release-runbook" }' in SIDEBAR.read_text(
        encoding="utf-8"
    ), "add docs/release-runbook.md to the Monitoring & Operations sidebar group"
    assert "(./release-runbook.md)" in DOCS_INDEX.read_text(encoding="utf-8"), (
        "add docs/release-runbook.md to the docs/README.md index"
    )
    assert "release-runbook.md" in VALIDATION_INDEX.read_text(encoding="utf-8"), (
        "docs/release-validation/README.md should point at the publish procedure"
    )
