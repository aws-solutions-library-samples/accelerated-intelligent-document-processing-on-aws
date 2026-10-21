---
paths:
  - "docs/**"
  - "**/README.md"
  - "CHANGELOG.md"
  - ".claude/skills/**"
---

# Documentation rules

Covers published user docs (`docs/`), developer and module READMEs, the
CHANGELOG, and the assistant skill files.

## Must
- Start every `docs/*.md` page with frontmatter containing `title:`. Follow it
  with the MIT-0 copyright and SPDX lines, then an H1 that matches the title, then
  `##` sections.
- When `idp_common` changes, update both tiers: the subpackage
  `lib/idp_common_pkg/**/README.md` (the canonical source for library behaviour)
  and the matching `docs/*.md` page, which should summarize it and link to it.
- Add a `CHANGELOG.md` `[Unreleased]` entry under Added, Changed or Fixed for
  every user-facing change.
- Write what is true now. Product history that a reader acts on (renames,
  upgrade-visible behaviour) is fine.
- Make every relative link and `#anchor` resolve, with anchors slugged the way
  `github-slugger` does it.
  - A published page that links to an unpublished `docs/` page must use a GitHub
    blob URL.
  - A CHANGELOG entry that cites a deleted page must use a version-pinned blob URL.
- Close every code fence. An unclosed fence hides the rest of the file from the
  link gate.
- In a copyable `pip install`, install first-party packages by path, with every
  sibling they need on the same command.
- Index each new page in `docs/README.md` and keep images in `images/`.
- When you add a skill, put it in `.claude/skills/` and give it a row in the
  `CLAUDE.md` skill table. Then either add a `.cline/skills/` symlink to it or add
  a `CLINE_EXEMPT` row with a reason.

## Must not
- Write commentary about the document's own history ("this page previously
  said", "an earlier draft claimed", "correction:").
- State counts or "as of today" facts that will go stale. Name how to derive
  them instead.
- Create or edit `.cline` copies of skills. They are symlinks to `.claude/skills/`.
- Move `iam-roles/cloudformation-management/README.md` into `docs/`. It is
  repo-only by design.
- Describe AppSync, or Pattern 1/2/3, as current architecture.

## Verify with
- `make check-markdown-links` and `make docs-build`
- `scripts/tests/test_markdown_links.py`, `scripts/tests/test_documented_counts.py`,
  `scripts/tests/test_doc_install_commands.py`
- `scripts/tests/test_repo_quality_review_skill.py` (skill table and symlinks)

## Sources
- [.claude/skills/documentation.md](../skills/documentation.md): the page
  template, both documentation tiers, the sidebar, the CHANGELOG format, and
  the skill symlink map
- [.claude/skills/prepare-changelog.md](../skills/prepare-changelog.md): how to
  shape the `[Unreleased]` section
- [CLAUDE.md](../../CLAUDE.md): "Documentation states what is true now" and how
  the link gate works
- [docs/dependency-confusion.md](../../docs/dependency-confusion.md): rules for
  install commands in docs
