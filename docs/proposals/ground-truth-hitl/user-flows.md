# Test Sets & Ground-Truth Datasets — User Flows

**What this is:** the proposed functionality expressed as user stories in **priority
order** (P0 → P3), each with the step-by-step flow through the UI. Companion to [data-model.md](data-model.md) (the shape of the data) and
[implementation/](implementation/) (per-slice build notes).

> (test stack `sr-testing`; sign in as the stack admin. Screens are addressable via
> `?step=<id>` — which itself demonstrates story 1's shareable deep link.)

**Personas**

| Persona | Role | Goal |
|---|---|---|
| **Config owner** | Admin/Author | Build a trusted, versioned test set; use it to measure and improve a config |
| **Annotator** | Reviewer (often external / onboarded for one effort) | Correct machine labels quickly, lowest-confidence first |
| **Evaluator** | Admin/Author | Score config/model changes against a stable reference and trust the comparison |

**A design rule that applies throughout:** annotation and revision UIs **reuse the
existing document review widgets** (the Visual Document Editor: page image +
bounding boxes, confidence-alert field filter, inline editing, section navigation,
claim/complete lifecycle). New surfaces only add *queueing, scoping, and progress*
around those widgets — no new editor is built.

---

## P0 — the annotation loop (this is the core value)

### 1. "As an annotator, I want a link that drops me straight into my queue, with the most suspect documents first, so my time removes the most error."

1. The config owner shares a **direct queue URL** for the workstream, e.g.
   `…/#/test-studio/sets/{testSetId}/annotate` — annotators bookmark it, or land on
   it from their onboarding note. After login they are **in the queue**: no console
   navigation, no Document List, no hunting.
2. The workspace is scoped to that one test set. A banner states the goal
   ("Target 99% — review the 62 lowest-confidence docs"), progress, and the deadline.
3. Left rail: the **review queue**, sorted lowest-confidence first, each doc showing
   its confidence chip. Claiming a doc locks it so annotators don't collide.
4. Opening a doc opens the **existing review editor, unchanged** — page image with
   bounding boxes, fields filtered to **Confidence Alerts Only**, model value +
   confidence vs threshold on each field, edited inline exactly as reviewers do today.
5. Correct or accept the flagged fields → **Save & next in queue →** — the next
   lowest-confidence doc opens automatically. Repeat until the queue (or the
   time-box) is done.

*Behind the scenes each correction also persists to the test set's own baseline (not
just the document's output) — that's what turns review into reusable ground truth.*

### 2. "As a config owner, I want to send a test set for review and choose how much effort to spend, so I get trustworthy labels without over-reviewing."

**Note:** includes the shareable queue URL and, under "Show the math", the target-accuracy estimator + error burndown

1. On the test-set detail page, click **Set up team annotation →**.
2. Choose review depth — three plain presets: **review lowest-confidence docs**
   (recommended) / **review everything** / **accept machine labels as-is**.
3. Optionally expand **"Show the math"** to target a specific label accuracy: set a
   target (99%), see estimated current accuracy (≈94%), the **minimum docs to
   review** (≈62 of 120), the implied confidence cutoff, and an **error-burndown
   chart** (residual error falling as lowest-confidence docs are reviewed). Clearly
   labeled a rough estimate that self-corrects as reviews come in.
4. Continue → the queue opens (story 1); **copy the queue URL** from here to share
   with annotators.

### 3. "As a config owner, I want to upload unlabeled documents and have the system draft labels, so I don't have to label from scratch."

**Note:** click "⚡ Generate draft labels" to see the before/after

*Closes today's biggest creation gap: a test set currently cannot exist without labels.*

1. **Test Sets → New Test Set → Upload documents only**; drop the files.
2. The set is created, docs **Unlabeled**, stage **Draft**.
3. Click **⚡ Generate draft labels** — runs the **active config** by default (no
   config-binding step; changeable when testing a specific version).
4. Docs gain machine labels + per-field confidence; the list sorts
   **worst-confidence first**; label source becomes **Draft (machine)**.
5. Fix the worst rows solo (story 5), or send to team annotation (story 2).

---

## P1 — trust, versioning, and the evaluation loop

### 4. "As a config owner, I want to freeze reviewed labels as an immutable version, so evaluation has a stable reference."

**Note:** *backend already built & verified on this stack*

1. From the detail page (or the end of the queue), click **Publish version**.
2. Review the summary: new version number, doc count, labels **Reviewed (human)**
   vs still **Draft (machine)**.
3. One decision: **use this version as the active reference?** (default yes) → Publish.
4. The version is **immutable**, per-field provenance recorded (source, reviewer,
   time); the draft stays editable toward the next version. Publishing is allowed
   **before 100% reviewed** — unreviewed fields keep machine labels, flagged as such
   — supporting time-boxed "first pass" golden datasets.

### 5. "As a config owner working alone, I want to fix a few bad labels without setting up a team workflow."

1. On the detail page, docs sort worst-first; click any row.
2. The **same existing editor** opens for that document; correct, save, return.
3. No queue or team setup — annotation is an *opt-in*, never a gate.

### 6. "As an evaluator, I want every test run to record which test-set version it scored against, so comparisons never silently mix label changes with config changes."

**Note:** note the ⚠ row showing an incomparable pair · *run-pinning backend already built & verified*

1. Start a test run as today (test set + config version).
2. The run **pins the test set's active reference version** automatically —
   symmetric to the existing config-version pin.
3. Results/comparisons show both `configVersion` and `testSetVersion`; two runs are
   only apples-to-apples when both match.

### 7. "As a config owner, I want to see where each set came from and which version is trusted, so I can manage many sets without confusion."

**Note:** *Stage/Source/Version columns already built & verified on the real Test Sets page*

1. **Test Studio → Test Sets** — one row per set: **Stage** (Draft → In review →
   Published), **Source** (Uploaded / Synthetic / Mixed), **Version** badge, and
   **Active reference**.
2. Click through to the detail page: per-document **confidence** and **label
   source** (Synthetic / Draft (machine) / ✓ Reviewed (human) / Unlabeled), bound
   config, versions panel.

*Label source is the trust model — machine-drafted ≠ human-verified ≠ synthetic-by-construction, and the UI never colors them the same.*

---

## P2 — faster creation on-ramps

### 8. "As a config owner, I want to generate a labeled test set from my existing config, so I can test it without sourcing documents."

1. **New Test Set → From an existing config** (recommended synthetic on-ramp).
2. Pick a config version + document class(es); choose how many docs to generate.
3. Documents arrive **already labeled**, schema-matched to the config exactly.
4. Land on the detail page: source **Synthetic**, stage **Draft** — publishable
   immediately or refinable.

### 9. "As a config owner, I want to upload documents I already have labels for, so existing ground truth becomes a managed test set."

**Note:** one upload surface covers stories 9 and 3 (labels auto-detected)

1. **New Test Set → Upload labeled docs**; labels **auto-detected** from the upload.
2. Land on the detail page fully labeled — ready to publish.

### 10. "As a config owner, I want to describe a document type in words and get a synthetic test set, so I can start with no config and no documents."

1. **New Test Set → Describe it**; type a description ("vendor invoices with line
   items, tax, PO number").
2. A schema is authored, a config version created, synthetic labeled docs generated
   and registered as a test set → detail page, stage **Draft**.

---

## P3 — maintenance over time

### 11. "As a config owner, I want to add or remove documents, so a set can grow and shed bad samples without starting over."

**Note:** *remove API already built & unit-tested*

1. Detail page → **Manage documents**: add via any on-ramp, or select rows → **Remove**.
2. Edits change the **draft** only; published versions are untouched. Publishing
   after an edit cuts the next version, preserving lineage.

### 12. "As a config owner, I want to merge two test sets, so per-vendor sets can become one golden set."

**Note:** includes the conflict-resolution question we need answered

1. Select two sets → **Merge**; resolve label conflicts where docs overlap.
2. The merged set starts as a new draft with provenance carried per document.

---

## The core loop, end to end

```
CREATE (any on-ramp)
   → DRAFT LABELS (machine, per-field confidence)
      → ANNOTATE (optional; confidence-guided queue via shared URL, existing editor)
         → PUBLISH vN (immutable; becomes active reference)
            → SCORE config/model changes against vN
               → fix config → re-run → compare (same vN = trustworthy delta)
                  → grow/refine the set → publish vN+1
```

## Status legend (what's real today)

Every story has a prototype screen (the ▶ links above); this table is about the
*underlying implementation*. Each prototype screen also shows its own status badge
in the "About this screen" panel.

| Story | Implementation status |
|---|---|
| 7 (list columns), 4 (publish/version backend + button), 6 (run pins version) | **Built & verified live** on the real Test Sets page of this stack |
| 1 backend (send-to-review trigger, correction→baseline write-back), 3 draft-labels backend, 11 remove (API) | **Built & unit-tested; UI is the prototype** |
| 1 deep-link queue URL (prototype demoes it via `?step=`), 8/10 synthetic wiring, 9 auto-detect upload, 12 merge | **Proposed** |

---

## Status

This document is the design record for the work, not a progress tracker. For
what actually shipped and what remains, see the Test Studio entries in
[CHANGELOG.md](../../../CHANGELOG.md) and the user-facing documentation in
[docs/test-studio.md](../../test-studio.md). The one design question still open —
whether a published version should pin label *bytes* (S3 manifest vs
copy-on-publish) rather than metadata — is recorded as a known limitation there,
because the choice is coupled to `DataRetentionInDays`.
