
---
title: "Quick Start"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Quick Start

**Quick Start** helps a first-time user go from a plain-language description of
their document type to a working, active IDP configuration — entirely through
conversation, with no manual schema editing. It solves the "cold-start" problem:
a fresh deployment has no configuration tuned to *your* documents, and writing
one by hand means learning the [JSON-Schema document-class format](json-schema-migration.md)
first.

A conversational agent:

1. **Understands your document type** from a natural-language description (asking
   brief clarifying questions when needed).
2. **Reuses an existing template** when one fits — it searches the document
   classes already in your configuration versions and adapts a close match rather
   than starting from scratch.
3. **Authors an extraction schema** and shows you the fields in readable form for
   review; you refine it in chat until it's right.
4. **Creates and activates a configuration version**, so you can upload a document
   and start processing immediately — no trip to the Configuration page required.

For the **highest-fidelity** result, you can attach your own example documents in
the chat; they run through [Discovery](discovery.md), which infers schemas from
the *real* documents instead of a prompt-only draft.

Quick Start is available two ways:

- **Web UI** — a floating Quick Start chat widget and a Welcome page (gated by the
  `EnableQuickStartWidget` parameter, default `true`).
- **CLI** — `idp-cli bootstrap`, for scripted or headless cold-start.

> **Related:** Quick Start handles *setup* (schema + config). For questions about
> your processed documents, analytics, errors, or the codebase, use the
> [Agent Companion Chat](agent-companion-chat.md). To infer a config directly from
> real documents, see [Discovery](discovery.md). To improve an existing config's
> accuracy/cost, see the AutoTune extension on the Extensions page.




https://github.com/user-attachments/assets/7096c812-08be-4716-ad37-7ae4f3797fa7



---

## Web UI

When `EnableQuickStartWidget=true` (the default), the web UI surfaces Quick Start
in two places:

- **Welcome page** — new users land on a Welcome screen with a **Quick Start**
  button (alongside *Take the tour* and *Enter IDP Console*). It explains the
  three common starting points: no config yet → Quick Start; already have a config
  → Configuration › View/Edit Configuration; want to test a document → Upload
  Document.
- **Floating Quick Start widget** — a chat launcher available from the Documents
  view, so you can open Quick Start at any time.

### A typical conversation

1. **Describe your document.** For example: *"I process California driver's
   licenses and need the license number, name, date of birth, and expiration
   date."*
2. **Review the draft schema.** The agent checks for a matching template, drafts a
   document-class schema, and lists the fields for you to confirm or change.
   Refining is cheap — iterate freely ("add the issuing state", "make expiration a
   date", "remove the address").
3. **(Optional) Start from a bundled sample.** If a curated
   [sample document](web-ui.md#upload-documents) of the same type exists, the
   agent links it inline so you can open the real document as a reference.
4. **(Optional) Attach an example document.** Use the upload control in the chat to
   attach your own document(s). They run through multi-document Discovery in the
   background, and the agent summarizes the discovered document types when they're
   ready. A schema inferred from a real document is higher-fidelity than a
   prompt-only draft.
5. **Activate.** When you approve, the agent creates a configuration version and
   **activates** it, then confirms it's ready. Upload a document from **Upload
   Document** and it's processed with your new configuration.

You can review or switch configurations at any time from
**Configuration › View/Edit Configuration** (see
[Configuration Profiles](configuration-profiles.md)).

### Disabling the widget

Set `EnableQuickStartWidget=false` at deploy time to hide the Welcome-page button
and the floating widget (for example, on a mature deployment where every user
already has a tuned configuration).

---

## CLI: `idp-cli bootstrap`

`idp-cli bootstrap` is the scriptable, headless equivalent. It authors a
document-class schema from your prompt (reusing a catalog match when one fits),
creates a configuration version, and — when the optional document generator is
available — generates a small labeled synthetic **test set** attached to that
version.

```bash
idp-cli bootstrap \
    --prompt "Invoices with vendor name, invoice number, date, and total amount" \
    --stack-name my-idp-stack
```

On success it prints the created config version, the resolution tier
(`catalog_adapt` when it adapted a template, `pure_llm` when authored from
scratch), any catalog match, and — if the generator is installed — the test set id
and document count.

### Local mode (no save)

Omit `--stack-name` to author and **print** the schema as JSON without touching any
stack — useful for previewing what the agent would create before committing:

```bash
idp-cli bootstrap --prompt "Bank statements with account holder and transactions"
```

### Options

| Option | Description |
|--------|-------------|
| `--prompt`, `-p` | **(required)** Natural-language description of the document type. |
| `--stack-name` | Target CloudFormation stack. **Omit for local mode** (print schema, no save). |
| `--class-name` | Document class name to use as the schema `$id` / document type. |
| `--field-hint` | A field the schema must include. Repeatable: `--field-hint X --field-hint Y`. |
| `--config-profile` (alias: `--config-version`) | Existing configuration profile to source catalog classes from / merge the new class into. |
| `--target-profile` (alias: `--target-version`) | Name of the configuration profile to create (default: `bootstrap-<class>`). |
| `--count`, `-c` | Number of synthetic documents to generate (default: `3`). |
| `--threshold` | Generation quality threshold, 1–10 (default: `7`). |
| `--augment` | Apply scan/fax-style image augmentation to generated documents. |
| `--model-id` | Bedrock model id override for schema authoring / generation. |
| `--region` | AWS region (optional). |

> **Note:** Unlike the web UI, `idp-cli bootstrap` does not activate the created
> version. Activate it from **Configuration › View/Edit Configuration** in the UI,
> or with the SDK, when you're ready to process documents with it.

---

## Synthetic document generation (optional)

Beyond authoring a config, Quick Start can generate a small **labeled synthetic
test set** — realistic sample documents plus ground-truth `result.json` — so you
can evaluate the configuration before you have real documents.

This capability is **optional and degrades gracefully**: schema authoring, config
creation, and activation always work. Only document generation requires the
generator, which is provided by the **Test Set Generator** extension. When it isn't
installed, the CLI and agent tell you so and continue without a test set — you can
upload your own documents instead.

- **In a deployed stack**, install the **Test Set Generator** extension from the
  **Extensions** page (Feature Platform). The Quick Start agent discovers it at
  runtime and offers generation only when it's present.
- **Locally**, install the generator alongside the library:
  `pip install -e "lib/idp_common_pkg[synthesis]"`.

Generation is slow (minutes) and costs money — roughly a few dollars and several
minutes per document at the default quality threshold, more at higher thresholds
— so the agent always confirms with a cost/time estimate before starting.

---

## How schema resolution works

1. **Catalog match.** Your existing configuration versions' document classes are
   indexed and an LLM picks the single best match for your description (only when
   its confidence clears a threshold). A match seeds the authored schema; no match
   just means authoring from scratch.
2. **Authoring.** The schema is authored from your prompt (plus any `--field-hint`
   fields and the seed), producing a JSON-Schema document class in the standard
   IDP format.
3. **Config version.** The new class is merged into the target version (creating it
   if needed; default name `bootstrap-<class>`), replacing any class with the same
   id. In the UI, the agent then activates the version.
4. **(Optional) Test set.** If the generator is available, N documents are
   generated, their field names validated against the schema (extras pruned), and
   the packet is registered as a [test set](creating-custom-test-sets.md) named for
   the config version.

---

## Requirements

- **Bedrock model access.** Schema authoring and catalog matching use a Claude
  model on Amazon Bedrock; request access before use (see
  [AWS Service Requirements](../README.md)).
- **Synthetic generation** additionally requires the Test Set Generator extension
  (deployed stack) or `idp_common[synthesis]` (local), as above.
