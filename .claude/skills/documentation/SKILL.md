---
name: documentation
description: "Use when writing or changing documentation: the two documentation tiers, page standards, CHANGELOG entries, the docs site, and skill files themselves."
---

# Documentation Conventions — GenAI IDP Accelerator

## Documentation lives in TWO tiers — update BOTH when relevant

1. **User / feature docs** — `docs/*.md`, published to the Starlight site
   (`docs-site/`). For features, deployment, configuration, model guides, etc.
2. **Developer / module docs** — `lib/idp_common_pkg/**/README.md`, one per
   `idp_common` subpackage (bedrock, extraction, classification, ocr, …) plus
   the package-level `lib/idp_common_pkg/README.md` and
   `lib/idp_common_pkg/idp_common/README.md`. These document the library API and
   are NOT on the Starlight site.

A change to `idp_common` behavior (new model family, new client parameter, new
public function) usually needs BOTH: the module README **and** any matching
`docs/*.md`. It is easy to update `docs/` and forget the module README (or vice
versa) — check both. The canonical home for client/SDK behavior is the relevant
module README (e.g. `idp_common/bedrock/README.md`); `docs/*.md` should
summarize and link to it rather than duplicate it.

## Docs Structure
```
docs/                    # Feature documentation (Markdown)
docs-site/               # Astro + Starlight documentation site
  ├── astro.config.mjs   # Site config with sidebar structure
  ├── src/content/docs/  # Symlinks to ../../../docs/
  ├── setup.sh           # One-time setup (creates symlinks)
  └── sync-sidebar.mjs   # Auto-syncs sidebar with new docs
lib/idp_common_pkg/**/README.md   # Per-subpackage developer/module docs
```

## Published Site
- URL: https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws/
- Framework: Astro + Starlight
- Deployment: GitHub Pages via `make docs-deploy`

## Markdown File Template
EVERY doc file must follow this pattern:
```markdown
---
title: "Feature Title"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Feature Title

Brief overview paragraph.

## Section 1

Content...

## Section 2

Content...
```

**Requirements**:
1. YAML frontmatter with `title` field (used by Starlight for page title)
2. Copyright line immediately after frontmatter
3. SPDX license identifier
4. H1 heading matching the frontmatter title
5. Use `##` for major sections, `###` for subsections

## Sidebar Organization (8 categories)
1. **Overview** — README, CONTRIBUTING
2. **Core** — Architecture, Deployment, Configuration, Web UI, CLI, SDK
3. **Processing Modes** — BDA, Pipeline, Discovery
4. **Document Processing Features** — Classification, Extraction, Assessment, OCR, etc.
5. **Evaluation & Testing** — Framework, Enhanced Reporting, Test Studio, MLflow
6. **AI Agents & Analytics** — Agent Analysis, Agent Chat, Code Intelligence, MCP
7. **Integration & Extensions** — Lambda Hooks, Fine-Tuning, Custom Models
8. **Monitoring & Operations** — Monitoring, Reporting, Capacity Planning, Cost

## CHANGELOG.md
Update for ALL user-facing changes. Format:
```markdown
## [x.y.z] - YYYY-MM-DD

### Added
- New feature description

### Changed
- Changed behavior description

### Fixed
- Bug fix description
```

## State what is true now — never what a previous draft said

A document is read as current fact. Its own editing history is in git and is of no
use to a reader, so **never write doc-about-doc commentary**: "an earlier version of
this guide got this wrong", "this page previously quoted −78%", "**Correction.** This
section previously claimed…", "that claim was false and is withdrawn", "four
corrections from review". When you fix a page, fix it — do not annotate it.

Keep the substance, drop the retraction framing. "This page previously quoted a −78%
saving; the figure was withdrawn" becomes "**Why there is no percentage here.** A
figure would have to come from the cost report, which at the time of the run priced
cache reads by substring match…". A withdrawn recommendation becomes a plain
"⚠️ Do **not** do X, because …".

What *is* legitimate is **product** history a reader acts on — "`--log-level INFO`
is now honoured; it used to be silently treated as unset", a rename, an
upgrade-visible behaviour change, and `CHANGELOG.md` entries generally — plus
caveats about a **measurement instrument** and corrections to an **external**
artifact (an AWS doc, a GitHub issue) the reader will also read. The test is whose
history it is: the product's, or this file's. See the fuller rule with examples in
`CLAUDE.md` ("Documentation states what is true now").

## Cross-Referencing
Link between docs using relative paths:
```markdown
See the [Architecture documentation](./architecture.md) for details.
See the [Extraction module](./extraction.md#agentic-extraction) for agentic extraction.
```

## Images
Store in `images/` directory at project root:
```markdown
![Architecture Diagram](../images/IDP.UnifiedPatterns.drawio.png)
```

## Commands
```bash
make docs-setup          # One-time: symlinks + npm install
make docs-build          # Build site
make docs                # Build + serve locally (http://localhost:4321)
make docs-deploy         # Deploy to GitHub Pages
```

## Contributing Guide
See `CONTRIBUTING.md` for:
- Branching strategy: `feature/`, `fix/`, `docs/` from `develop`
- PR process and review requirements
- AWS-specific considerations (GovCloud, security scanning)

## Checklist: adding (or changing) a Bedrock model

> **See `.claude/skills/add-model/SKILL.md`** for the full expanded workflow (gather
> ground truth from the model card first, sibling-grep to find touchpoints,
> per-file map, validation, common traps). The list below is the quick version.

A new selectable model touches MANY files — it is easy to miss one. Work
through this list (skip items that genuinely don't apply, and note why):

**Config / template / UI**
- [ ] `patterns/unified/template.yaml` — add the model ID to every service
      `model` / `model_id` enum where it's valid (ocr, classification,
      extraction + per-class `extraction_model`, assessment, summarization,
      evaluation `llm_method`, chat). Add to the correct region sub-list (US /
      EU / global).
- [ ] `config_library/pricing.yaml` — add `bedrock/<model-id>` with input /
      output / cache-read / cache-write token units.
- [ ] `config_library/model_config_limits.yaml` — add a `max_output_tokens`
      pattern if the family's limit differs.
- [ ] `lib/idp_common_pkg/idp_common/config/models.py` — defaults / new fields
      if the model needs a new inference parameter.
- [ ] `src/lambda/update_configuration/index.py` — region mapping / filtering
      (e.g. US-only models hidden in EU deployments).
- [ ] `src/ui/.../SchemaInspector.tsx` — any hardcoded UI model dropdown.

**Client behavior (`idp_common`)**
- [ ] `lib/idp_common_pkg/idp_common/bedrock/client.py` (and
      `openai_responses.py` for mantle models) — routing, params, cachepoint
      support list.
- [ ] IAM — Lambda execution roles in `template.yaml` and
      `patterns/unified/template.yaml` (and nested templates) if a new endpoint
      / action is required.

**Docs (BOTH tiers — see top of this file)**
- [ ] `lib/idp_common_pkg/idp_common/bedrock/README.md` — module-level behavior,
      caveats, examples.
- [ ] `docs/*.md` — feature docs that list models / caveats (e.g.
      `discovery.md`, `extraction.md`, `service-tiers.md`, `web-ui.md`,
      `configuration.md`, `idp-cli.md`, `eu-region-model-support.md`,
      `canada-region-model-support.md`), plus a dedicated model guide if the
      model is materially different (see `docs/openai-models.md`).
- [ ] `docs/README.md` index — link any new doc.
- [ ] `CHANGELOG.md` — `[Unreleased]` entry stating what IS and IS NOT supported.

**Tests**
- [ ] Unit tests for routing / params; config-validate guards for unsupported
      combinations (e.g. `tests/unit/config/test_validation.py`).

## Skill format: one directory per skill, read by every assistant

Each skill is a directory holding one `SKILL.md`:
`.claude/skills/<name>/SKILL.md`. This is the open
[Agent Skills](https://agentskills.io) layout, and it is the only copy of the
skill. Claude Code and Cline both read `.claude/skills/` directly, and
`.agents/skills` is a symlink to it for assistants that look there (Codex,
OpenCode and others). There is no `.cline/skills/` and no per-assistant copy, so
editing a skill changes it for every assistant at once.

`SKILL.md` opens with YAML front matter:

```yaml
---
name: <name>            # must equal the directory name
description: "Use when ..."   # one or two sentences, at most 1024 characters
disable-model-invocation: true  # optional, see below
---
```

The `description` is what an assistant reads to decide whether to load the skill,
so it says when to use it, in the words a user would type. Keep it short: every
description is loaded into every session. The longer "When to Use" text belongs in
the `CLAUDE.md` skill table and in the skill body.

Set `disable-model-invocation: true` on a skill that a program or another skill
reads by path and that an assistant should not load on its own: the unattended CI
contracts (`pr-review-ci`, `ux-test-ci`, `ux-test-stories-ci`) and
`work-the-backlog-hazards`. Claude Code then loads it only when a user names it.

When **adding** a skill: create `.claude/skills/<name>/SKILL.md` with that front
matter, give it a row in the `CLAUDE.md` skill table, and link to sibling skills as
`../<other>/SKILL.md`. `scripts/tests/test_skill_format.py` fails on a flat
`.md` file under `.claude/skills/`, a missing or mismatched `name`, a missing or
over-long `description`, or an `.agents/skills` that no longer points here.

Every skill is visible to every assistant that reads the directory. A skill that
drives live AWS stacks or depends on Claude Code mechanisms (subagents, background
runs, `claude --mcp-config`) says so in its own opening paragraph, which is where an
assistant reading it will see it.

> **Portability caveat:** `.agents/skills` is a symlink (git mode 120000). On a
> clone with `core.symlinks=false` (some Windows setups) it materialises as a text
> file containing the target path. Claude Code and Cline are unaffected because
> they read `.claude/skills/` itself; for another assistant, run
> `git config core.symlinks true` and re-checkout.
