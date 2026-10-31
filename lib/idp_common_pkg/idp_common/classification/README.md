Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Document Classification for IDP Accelerator

This module provides document classification capabilities for the IDP Accelerator project, allowing classification of documents based on their text and image content. It supports multiple classification backends including Bedrock LLMs and SageMaker UDOP models.

## Features

- Classification of documents using multiple backend options:
  - Amazon Bedrock LLMs
  - SageMaker UDOP models
- **Optional regex-based classification for enhanced performance**
  - Document name regex matching when all pages should be classified as the same class
  - Page content regex matching for the `combined` strategy at page granularity
- **Valid-class enforcement with re-prompt/retry** on every call that assigns a
  class — the combined call at page *and* packet granularity, and
  split-then-classify's section classification — rejects out-of-vocabulary predictions and re-prompts the model
  with the allowed classes
- Direct integration with the Document data model
- Support for both text and image content
- Concurrent processing of multiple pages
- Structured data models for results
- Grouping of pages into sections by classification
- Comprehensive error handling and retry mechanisms
- **DynamoDB caching for resilient page-level classification**
- **Sequence segmentation using BIO-like approach for section boundary detection**

## Configuration layout and how the service reads it

`ClassificationConfig` (config format 0.9) nests every setting inside the model
call that uses it:

| Block | Used when | Holds |
|---|---|---|
| `strategy` | always | `combined` \| `split_then_classify` |
| `fallback_class` | always | the one class assigned when no class can be decided; extraction reads it too |
| `combined` | `combined` | `granularity`, `page`, `packet`, `llm` (class-assigning model settings) |
| `split_then_classify.splitting` | `split_then_classify` | `method`, `llm` (`granularity`, `page`, `packet`, `boundary_hints`, model settings), `boundary_model` (declared, not implemented) |
| `split_then_classify.section_classification` | `split_then_classify` | `method`, `pages`, `k`, `llm` (prompts + class-assigning model settings), `embeddings`, `decision_model`, `low_confidence` (declared, not implemented) |
| `document_shape` | always | `sectionSplitting`, `maxPagesForClassification` |

A model-settings block (`ClassificationModelSettings`) is the model, its
parameters, `modality` and `image`. The class-assigning variant
(`ClassificationClassAssigningSettings`) adds `validation` and `confidence`;
the splitting call uses the plain one because it returns no class.
`ClassificationConfig.page_input_call()` and `class_assigning_llm()` return the
blocks in force under the configured strategy.

The service resolves every read through a handful of methods, so the
strategy-dependent choice of block is made in one place:

- `_reader_stage()` — the stage whose call reads pages: the splitting call under
  `split_then_classify`, the combined call otherwise.
- `_call(stage)` — that stage's model-settings block. Model, parameters, modality,
  image size, validation and confidence are all read from it, so each call uses
  its own.
- `_page_input()` — the block holding `granularity`, `page` and `packet` for the
  page-reading call. `self.granularity` is resolved from it.
- `_document_shape()` — `sectionSplitting` and the page limit.

`_stage_prompt(stage, field)` resolves prompts. The combined call and the splitting
call read `page` or `packet` by their own granularity; section classification is not
granularity-dependent, because it is always one call about one segment's pages.
An empty prompt falls back to the shipped default for that block (read from
`system_defaults` by `_shipped_classification_defaults`), except the combined
page prompt, whose absence is refused with `No task_prompt found` as it always
was.

`modality` flags are tri-state. `None` — the default — means *infer from this
call's own task prompt placeholders*. Setting one explicitly rewrites that
prompt to match: `_apply_modality_to_prompt` removes a placeholder together with
its wrapping element when that element held nothing else, or appends a wrapped
placeholder when the prompt has none. Both directions are logged with the
call's path.

The page limit (`document_shape.maxPagesForClassification`) is applied only when
`sectionSplitting` is `disabled`; with boundary detection on it is set to `ALL`
at construction, with a warning.

### Reading older configurations

`migrations/v08_to_v09.py` converts the flat keys of formats 0.8 and earlier on
every read, so the service reads the nested shape only. `ClassificationConfig`
runs the same conversion in a `before` validator, so a caller building it
directly from flat keys is converted too. Four rows of the conversion are what
keep behaviour exactly as it was:

- **Model parameters, modality and image settings are copied into every call.**
  The split-then-classify calls had no parameters of their own in 0.8 and always
  inherited the flat ones.
- **An empty stage model means inherit**, so it becomes the flat model; a stage
  model that was set is kept.
- **The packet window is copied into the splitting call too**, which read the shared
  window.
- **For a split-then-classify configuration the inferred modality is written onto
  the stage blocks** whenever the flat prompt it was inferred from is present,
  because each call now infers from its own prompt.

A page limit together with boundary detection is converted to
`sectionSplitting: disabled`, with a warning. In a hybrid of flat and nested keys
the flat key wins, because a hybrid only arises when a flat key is applied on top
of a nested configuration. The trigger is the presence of a flat key
(`FLAT_KEYS`), not the version stamp. The `v07_to_v08` hop before it converts
`classificationMethod` to `granularity` and copies a packet-shaped holistic
prompt into `packet`; it does not write `modality`, because an unset flag is
inferred from the prompt.

### Windowing and merging

`_packet_windows` splits a packet above the call's `packet.window_pages` with
`packet.overlap_pages` repeated between neighbours; `window_pages: 0` never
windows. `_merge_window_segments` projects each window's segments onto per-page
`(class, boundary)` verdicts and re-runs `_fold_page_verdicts_to_segments` — the
same fold page-level classification uses, so the two granularities cannot
disagree about how verdicts become segments. **The earlier window wins for an
overlapping page**: it is the one that saw the pages before them, which is what a
boundary decision mostly rests on. A single window is used directly rather than
folded, so the common case is byte-identical to the unwindowed path — unless its
segments overlap. **Within one call the segment reported first wins**, which is the
order the per-page claim already walks for a windowed packet, so a single call whose
segments share pages goes through the same claim and fold and every page lands in
exactly one segment (#1654). A segment that lost pages to an earlier segment of the
**same call** opens a new document at the next page it keeps, so two same-class
documents that share a page stay two sections; a page lost to an **earlier window**
is the leading overlap, and the pages after it continue that window's document. The
fold carries no `candidates`, so a merged segment built entirely from one reported
segment gets that segment's list back, and one assembled from several gets none.

A window the model refuses as too long (`is_input_token_overflow`) is split in two
by `_split_overflowing_window` and both halves are retried, recursively, with up to
`overlap_pages` repeated at the junction; the halves are queued in page order so
the merge's "earlier window wins" still holds. A one-page window that does not fit
is reported like any other window failure, and `window_pages: 0` is never split,
because one call over the whole packet is what that setting asks for (#1676). The
repeated overlap is capped at about half the first half (and at least one page), so
each split shrinks the window by a fraction rather than by a page; a sub-window
already requested is not requested again; and the calls made for split windows stop
at twice the packet's page count, after which the remaining sub-windows are reported
as not classified. A class-validation re-prompt that overflows keeps the answer
already received, with its metering, and assigns the fallback rather than splitting.

A combined packet call validates every segment's class like the page path does:
a reply naming an invalid class re-prompts the window up to
`validation.maxValidationRetries` times, and a segment still invalid afterwards is
given `fallback_class`.

### The segments contract

Every path produces `list[Segment]` and one builder reads it:

```python
@dataclass
class Segment:
    page_ids: List[str]
    doc_class: Optional[str] = None   # None until section classification fills it
    confidence: Optional[float] = None
```

`_sections_from_segments(document, segments, page_results=None)` is the only
place `document.sections` is built, for every strategy and granularity.

`page_results` is passed explicitly rather than read back off `document.pages`
because only **one** granularity has a per-page signal at all. A segment's score
is the minimum across its pages, so building per-page sections from segments
alone would report every page of a run as being as uncertain as its worst page —
in the one strategy whose entire purpose is per-page granularity.

### Split-then-classify

`strategy: split_then_classify` runs `_detect_boundaries` (the splitting call, metered
as `Splitting`) then `_classify_segments` (section classification, metered as
`SectionClassification`). The combined call is metered as `Classification`. Each stage
reads its own model settings through `_call(stage)`. A forced class
(document-name regex match) skips section classification entirely. Under
`sectionSplitting: disabled` or `page`, `_segments_from_document_shape` builds the
segments directly and the splitting call is not made. A segment whose section
classification call fails keeps `doc_class = None` and is reported — its
boundaries are still good, and an invented class would propagate into extraction
as though it had been decided.

#### Which placeholders a stage is allowed to render

The prefix saving is the whole justification for split-then-classify, so **the
splitting call renders `{CLASS_NAMES_AND_DESCRIPTIONS}`, `{CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}`
and `{FEW_SHOT_EXAMPLES}` as empty**, whichever granularity it runs at and
whatever `contextPagesCount` is set to. That is decided by the stage, in
`_stage_withholds_class_vocabulary` and `_build_classification_substitutions`,
rather than by the content builder that happens to render the prompt — the page
builder, the context-page builder and the packet builder are each shared between
the combined call, the splitting call and section classification, so a per-builder
answer is three answers. `boundary_hints: class_names` stays the one supported way
to put class **names** into a splitting prompt.

Symmetrically, **`{BOUNDARY_HINTS}` renders as empty for every stage other than
the splitting call.** The setting lives under `split_then_classify.splitting.llm` and
so is inert under `strategy: combined`, but that subtree is readable either way;
the block it produces ends "do NOT report a type, only where each document
starts and ends", which is the one thing a combined or section-classification
call exists to do.

None of this is observable with the shipped prompts — the shipped splitting
prompts reference neither vocabulary placeholder, and the shipped combined
prompts do not reference `{BOUNDARY_HINTS}` — so it matters for an edited
prompt, which every one of these is.

`stage` is a **keyword-only parameter with no default** on every content
builder and on `_build_classification_substitutions`, which is the point worth
preserving through a refactor: a call site that omitted it would get the full
vocabulary back with no type error and no failing test, and the only symptom
would be the bill.

The UI's prompt preview applies the same rules about which placeholder is empty
for which step, so a step's preview does not promise content the call will not
carry. They are restated in `buildSubstitutions` in
`src/ui/src/components/configuration-layout/PromptPreview.tsx`, and asserted
against a real render in
`src/ui/src/components/configuration-layout/__tests__/PromptPreview.test.tsx`
(`PromptPreview placeholder scoping by step`) — so **if you change which
placeholders a stage renders, change that function too**, and those tests will
tell you if you forget.

The preview is nevertheless not byte-equal to the request, and is not meant to
be: it renders a shorter `<document-type-hints>` block than the service emits,
and substitutes a descriptive marker for `{DOCUMENT_TEXT}` and
`{DOCUMENT_IMAGE}`. Read it for what a step *includes*, not as the exact bytes.

## Sequence Segmentation Approach

Page granularity implements a sequence segmentation approach similar to BIO (Begin-Inside-Outside) tagging commonly used in NLP. This enables accurate splitting of packets into sections, where a single document may contain several distinct sections.

### How It Works

Each page receives two pieces of information:
1. **Class**: The classification label (e.g., "invoice", "letter", "financial_statement")
2. **Section Boundary** (`document_boundary`): A boundary indicator that signals section transitions:
   - `"start"`: Indicates the beginning of a new section (similar to "Begin" in BIO)
   - `"continue"`: Indicates continuation of the current section (similar to "Inside" in BIO)

### Benefits

- **Packet Support**: Accurately splits packets into their sections
- **Type-Aware Boundaries**: Detects when a new section of the same class begins
- **Automatic Section Creation**: Pages are grouped into sections based on both type and boundaries
- **Improved Accuracy**: Context-aware classification that considers document flow

### Example Segmentation

Consider a packet with 6 pages containing two invoices and one letter:

```
Page 1: type="invoice", boundary="start"      → Section 1 (Invoice #1)
Page 2: type="invoice", boundary="continue"   → Section 1 (Invoice #1)
Page 3: type="letter", boundary="start"       → Section 2 (Letter)
Page 4: type="letter", boundary="continue"    → Section 2 (Letter)
Page 5: type="invoice", boundary="start"      → Section 3 (Invoice #2)
Page 6: type="invoice", boundary="continue"   → Section 3 (Invoice #2)
```

The system automatically creates three sections, properly separating the two invoices despite them having the same class.

### The boundary rules (`<boundary-detection-rules>` in `task_prompt`)

Pagination first (`Page 2 of 2` ⇒ `continue`), then the opening header block ⇒
`start`, then **table continuation** ⇒ `continue` — a page of table rows whose column
headings are reprinted at the top is a continuation page, because repeated column
headings are not a document title (#726: page 2/3 of a 3-page single-class statement
was called `start` in ~half the runs; 8/15 → 15/15 correct section counts after the
rule, other fixtures unchanged) — then other continuation evidence, then the two
paired CRITICAL clauses (#653). The block is pinned by
`tests/unit/classification/test_boundary_prompt_contract.py`, and every committed
copy of the prompt (presets, sample configs, notebooks) is checked to carry the same
rules block by `scripts/tests/test_classification_prompt_copies_in_sync.py`. A
configuration profile a user saved on a running stack is an independent snapshot and
does not receive an updated default prompt on upgrade.

### Where the `document_boundary` signal lives

The boundary indicator is carried in `PageClassification.classification.metadata["document_boundary"]` and is consumed by `_fold_page_verdicts_to_segments` / `_group_consecutive_pages`. It is also copied onto the declared `Page.document_boundary` field, which **is** persisted — it appears in the `document.json` page dict, in the DynamoDB page record as `Boundary`, in the `create_document_run` snapshot, and on the GraphQL `Page` type. So a merge decision can be inspected after the fact rather than re-derived from Lambda logs.

(Note the older `setattr(page, "metadata", ...)` call next to it stashes the whole metadata dict on an attribute that is *not* a dataclass field, so that one does not survive serialization — `Page.document_boundary` is the durable one.)

To keep merges auditable, `_log_boundary_signals` logs the complete page → boundary map on one line before the fold builds segments:

```
Page document_boundary signals: {'1': 'start', '2': 'continue', '3': '(absent)'}
```

`(absent)` means the model omitted the field, in which case the code defaults it to `"continue"`. That is deliberately reported as distinct from an explicit `"continue"`: an omitted signal merges pages *by accident*, while an explicit `"continue"` is the model's judgement. Distinguishing the two is what makes an unexpected merge diagnosable.

### A class description can override the boundary rules (`PRECEDENCE`)

`<boundary-detection-rules>` in the default combined page prompt (`classification.combined.page.task_prompt`) ends with a `PRECEDENCE:` clause that promotes a document type's **own** boundary instructions above the generic rules. `_format_classes_list()` puts every class's `description` into `{CLASS_NAMES_AND_DESCRIPTIONS}`, so that clause makes the description the supported place to state a per-class rule — no code path or config key is involved.

This is the fix for the one shape the generic rules get wrong ([#750](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/750)): a long table that reprints its title and column headers on every page and paginates with a bare number. Rule 3 (opening header block ⇒ `start`) outranks rule 4 (continuation evidence ⇒ `continue`), the running header satisfies rule 3, and a lone `7` matches none of rule 1's pagination patterns — so continuation pages are read as new sections. Measured on `us.amazon.nova-2-lite-v1:0` at `temperature: 0`, a 16-page fund table split into 2–5 sections in 10 of 10 runs; with a `BOUNDARY:` sentence in the class description, 1 section in 10 of 10.

Working example: `scripts/sdlc/config/nuveen.yaml` (the CI Step 8 config), guarded by `scripts/tests/test_nuveen_boundary_precedence.py` — which also asserts the `PRECEDENCE:` clause still exists, since removing it would silently disable every class-level instruction that depends on it. Name `"start"` / `"continue"` explicitly in the description; those are the values `document_boundary` takes.

Two things this is **not** a workaround for, both measured: prompt-level wording changes that generalise the rule (they regress the no-pagination over-split case) and `contextPagesCount: 1` (it regresses back-to-back duplicates). See [docs/classification.md](../../../../docs/classification.md#known-failure-mode-repeating-running-headers) for the numbers.

### Classification confidence (`confidence`, `classification_reason`)

Two OPTIONAL keys in the model's response are parsed alongside `class` and
`document_boundary` ([#673](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/673)).
Both are read and persisted whenever the model returns them, whatever asked for
them — the shipped `confidence.mode` composes the instruction, the **default**
task prompt asks for `classification_reason`, and a hand-written prompt asking
for either is honoured because the parser is not gated on the setting. The table
below is where each one lands.

| Key | Lands on | Persisted as |
|-----|----------|--------------|
| `confidence` | `DocumentClassification.confidence` → `Page.confidence` | `ClassConfidence` (DynamoDB), `ClassConfidence` (GraphQL `Page`), `confidence` (document.json) |
| `classification_reason` | `metadata["classification_reason"]` → `Page.classification_reason` | `ClassReason` (DynamoDB + GraphQL), `classification_reason` (document.json) |

`parse_confidence` does the reading: it accepts `0.95`, `"0.95"`, `95` and
`"95%"`, and returns `None` for anything unparseable or out of range rather than
raising — a malformed confidence must not fail a classification that otherwise
worked.

**`None` means NOT SCORED, and is never serialized as a number.** This is the
whole point of the change: `Page.confidence` used to default to a fabricated
`1.0`/`0.0`, and that value flowed to the reporting lake's `section_confidence`
column where it was indistinguishable from a real one. `1.0` now appears only
where something genuinely asserts the class — a document-name regex match
(`_pin_page_class`), a single-class configuration, a page-content regex match, or
a human correction in the Web UI — and `0.0` only on an errored page.

Three paths deliberately produce no score: the SageMaker/UDOP backend (the
endpoint returns none), pages beyond `document_shape.maxPagesForClassification` (their class is
extrapolated from a sample, not predicted), and a page whose invalid class was
replaced by `fallback_class` (the stored class is no longer the model's, so
neither its score nor its reasoning describes it — both are dropped, including
across a validation retry).

Section confidence is `aggregate_page_confidence`: the **minimum** across the
section's pages, and `None` if any page is unscored. Min because a mean hides the
one page the classifier was unsure about, and because a section cannot be more
certain than its weakest page; `None`-absorbing because the min of a *scored
subset* would present a partial aggregate as a whole-section number.

⚠️ Page-level classification is one inference **per page**, so anything added to
its output format multiplies by page count — unlike extraction's confidence, which
is per section. That is why the default was measured before being turned on:
`topk` costs +17 % of the classification step, which is ~3 % of total document cost
on the default model (so ~0.5 % of the bill) and changes accuracy by nothing
consistent. `mode: off` restores the zero-cost path exactly. See
`docs/benchmarking/studies/classification-confidence.md`, and note the finding that a
*small* classifier's score is a coarse two-level flag while a mid-tier one's is
graded — the mode being on does not make the number equally useful everywhere.

`_apply_page_result` is the single place that copies a result onto the declared
`Page` fields (class, confidence, reason, candidates, boundary), shared by the
cache-hit and fresh-inference branches so a cache hit cannot yield a page with
fewer signals than a miss.

#### Asking for it: the call's `confidence.mode`

`topk` (**default**), `verbalized`, or `off`. `class_confidence.py` owns both halves:

- **Prompt assembly.** The block lives on each class-assigning call
  (`combined.llm.confidence`, `split_then_classify.section_classification.llm.confidence`).
  `append_class_confidence_block` splices its `task_prompt_topk` /
  `task_prompt_verbalized` into
  the task prompt **before** the first `<<CACHEPOINT>>` / `<document-ocr-data>` /
  `{DOCUMENT_TEXT}` marker, so the static instruction stays inside the
  prompt-cache prefix — the same rule, for the same reason, as
  `extraction/prompt_assembly.py::_append_bbox_block`, and it matters more here
  because classification runs per page. The splice is idempotent (a prompt that
  already carries a `<class-confidence>` block is left alone). A combined packet
  call takes the per-segment `task_prompt_topk_packet` /
  `task_prompt_verbalized_packet` instead, because its response is a segment list;
  section classification always takes the page-shaped block, being one call about
  one segment. The splitting call is never given a block: it decides no class.
  Consequently the section-classification call carries **no** `_packet` blocks:
  `base-classification.yaml` ships none there and `migrations.v08_to_v09` does not
  copy them into it
  (`SECTION_CLASSIFICATION_UNREAD_CONFIDENCE_KEYS`), so the keys an operator can
  see on that call are the ones it can actually read. The combined call keeps
  both, because it can run at packet granularity.
- **Resolution.** `resolve_class_and_confidence` prefers an explicit
  `confidence`, else `confidence_from_candidates`, which returns the probability
  of the class being **stored** — not the top of the list. Those differ when the
  model's `class` contradicts its own ranking, and reporting the top probability
  would then describe a class the page was never given; a stored class absent
  from its own candidate list leaves the page unscored rather than inferring a
  number from the leftover mass.

`parse_candidates` drops out-of-vocabulary classes (a class that cannot be stored
cannot be reported), deduplicates on the highest probability, and rescales **only
when the probabilities sum to more than 1.0** — a distribution cannot exceed 1,
whereas a sum below 1 legitimately means "possibly some other class" and
inflating the top candidate to absorb it would manufacture confidence.

`resolve_top_k` clamps the requested count —
the call's `confidence.top_k_candidates` (default **3**) — to
`[2, len(valid_doc_types)]`: asking for more candidates than there are classes
invites invented ones, and a single candidate is a verbalized confidence with
extra syntax — the calibration benefit comes precisely from having to rank
alternatives.

Ranked candidates land on `Page.classification_candidates`, persist as
`ClassCandidates` (a list of `{Class, Probability}` maps), and are exposed as
`Page.ClassCandidates` on the API and in the UI popover.

#### Measuring whether the number means anything

A confidence nobody checks is worse than none. `attach_page_confidence`
(`evaluation/stickler_backend/doc_split.py`) annotates each row of the evaluation
report's `doc_split_metrics.page_details` with `predicted_confidence`, next to the
`correct` flag that is already there. The benchmark harness
(`benchmarks/harness/analyze.py::score_classification`) turns those rows into
`class_calibration_separation` = mean(conf | correct) − mean(conf | wrong), with
`class_accuracy` and `n_class_scored_pages` beside it, and `aggregate.py` treats a
−0.03 move as a regression on the same footing as the field-level separation.
Near-zero separation means the model is equally confident right and wrong — do not
route work on it.

### Section splitting values

`classification.document_shape.sectionSplitting` controls how classified pages become sections:

| Value | Behavior |
|-------|----------|
| `llm_determined` (default) | Group pages using the `document_boundary` signal described above |
| `page` | One section per page — no same-type joining is ever possible |
| `disabled` | One section spanning the whole document (class chosen by a page-weighted vote across the segments that were detected) |

The vote is cast by `_single_section_from_segments`, and it is **per page but
over segments**: each segment carrying a configured class contributes
`len(segment.page_ids)` votes, so a 5-page segment outweighs a 1-page one. That
distinction is invisible at page granularity, where segments are mostly single
pages, and decides the answer at packet granularity, where the call returns
multi-page segments. A tie goes to the earliest segment carrying a tied class,
and `segments[0].doc_class` is used only when no segment carries a configured
class at all. ⚠️ A page whose own verdict was not the winning class has its
confidence cleared, and a section's score is `None` if any of its pages is
unscored — so one disagreeing page leaves the whole section without a
confidence. A page that produced no verdict of its own inherits its segment's
class *and* confidence, so silence does not count as disagreement.

`document_shape.maxPagesForClassification` applies only under `disabled`. There,
`_limit_pages_for_classification` classifies the first N pages and the whole document
takes the class that vote returns; a document at or under the limit is classified in
full. Under `llm_determined` or `page` the limit is ignored with a warning at
construction, because applying it would collapse only documents longer than N while
sectioning shorter ones normally.

Under split-then-classify, `disabled` and `page` also skip the splitting call: the
segments are known, so `_segments_from_document_shape` builds them and section classification runs
on each.

### Single-class configurations

When the configuration defines exactly **one** document class, the *class* decision is predetermined — there is nothing for the model to choose. But section **boundaries** are a separate question, and the backend is only skipped when the configured `sectionSplitting` value genuinely needs no model output (GitHub issue [#686](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/686) — previously the short-circuit hard-coded one all-pages section and `sectionSplitting` was silently ignored):

| `sectionSplitting` | Single-class behavior | Backend call? |
|--------------------|-----------------------|---------------|
| `disabled` | One section over all pages | No |
| `page` | One section per page | No |
| `llm_determined`, single-page document | One section | No — one page cannot be split |
| `llm_determined`, multi-page document | Real boundary detection | **Yes** |

`llm_determined` asks for LLM boundary detection, so it performs it. A knob that silently does nothing is a bug, not an optimization — and the old behaviour was actively harmful for the case it mattered most: a packet holding several separate statements of the single class became one section, and extraction then returned only the first record.

**Cost note.** `llm_determined` is the *default*, so a **multi-page** single-class deployment that never set `sectionSplitting` now performs classification inference where it previously performed none. Single-page documents are unaffected (nothing to split, so the call is skipped), which covers the common "one form per document" deployment at zero cost.

If one input document is always exactly one section and you want to keep the zero-cost path on multi-page documents, set it explicitly:

```yaml
classification:
  document_shape:
    sectionSplitting: disabled   # one document is always one section
```

`page` remains available when you want one section per page without inference.

## Regex-Based Classification for Enhanced Performance

The classification service now supports optional regex-based pattern matching to provide significant performance improvements and deterministic classification for known document patterns. This feature enables instant classification without LLM API calls when regex patterns match.

### Document Name Regex Classification

When you want all pages of a document to be classified the same way, document name regex patterns can instantly classify entire documents based on their filename or ID:

```yaml
classes:
  - $schema: https://json-schema.org/draft/2020-12/schema
    $id: Payslip
    type: object
    x-aws-idp-document-type: Payslip
    description: Employee wage statement showing earnings and deductions
    x-aws-idp-document-name-regex: "(?i).*(payslip|paystub|salary|wage).*"
    properties:
      EmployeeName:
        type: string
        description: Name of the employee
```

**How it works:**
- Works with any number of document classes defined in configuration
- When document ID matches the regex pattern, all pages are classified as that class
- Skips the LLM *class* decision entirely — the name is authoritative, and it stays authoritative even when the model does run (see below)
- Provides info-level logging when matches occur

#### …and `sectionSplitting` still applies

A filename tells you **what** the packet holds; it never tells you **where** one section ends and the next begins. So the name match short-circuits the class decision only — section boundaries follow `sectionSplitting`, exactly as for [single-class configurations](#single-class-configurations) (GitHub issue [#705](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/705) — previously this path hard-coded one all-pages section and `sectionSplitting` was silently ignored, the same defect as [#686](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/686) reached through a different trigger):

| `sectionSplitting` | Name-matched behavior | Backend call? |
|--------------------|-----------------------|---------------|
| `disabled` | One section over all pages | No |
| `page` | One section per page | No |
| `llm_determined`, single-page document | One section | No — one page cannot be split |
| `llm_determined`, multi-page document | Real boundary detection, class pinned to the matched class | **Yes** |

In the last row the model is invoked for the per-page `document_boundary` signal (or, at packet granularity, for the segment *ranges*) and its class output is discarded in favour of the regex match — so a packet of five payslips becomes five sections, all classified `Payslip`.

**Cost note.** `llm_determined` is the *default*, so a name-matched **multi-page** document now performs classification inference where it previously performed none: for those documents the regex is no longer a "skip the LLM" shortcut. Single-page matches are unaffected and keep the zero-inference path. To keep multi-page documents at zero cost when one document is always one section, set it explicitly:

```yaml
classification:
  document_shape:
    sectionSplitting: disabled   # one document is always one section
```

`page` also stays inference-free when you want one section per page.

### Page Content Regex Classification

Under the `combined` strategy at page granularity, page content regex patterns can classify individual pages based on text content:

```yaml
classes:
  - $schema: https://json-schema.org/draft/2020-12/schema
    $id: Invoice
    type: object
    x-aws-idp-document-type: Invoice
    description: Business invoice document
    x-aws-idp-document-page-content-regex: "(?i)(invoice\\s+number|bill\\s+to|amount\\s+due)"
    properties:
      InvoiceNumber:
        type: string
        description: Invoice number
  - $schema: https://json-schema.org/draft/2020-12/schema
    $id: Payslip
    type: object
    x-aws-idp-document-type: Payslip
    description: Employee wage statement
    x-aws-idp-document-page-content-regex: "(?i)(gross\\s+pay|net\\s+pay|employee\\s+id)"
    properties:
      EmployeeName:
        type: string
        description: Employee name
```

⚠️ **The class name and both regexes must be written as the `x-aws-idp-*` schema
extension keys.** `ClassificationService` builds its document-type list from
`x-aws-idp-document-type`, `x-aws-idp-document-name-regex` and
`x-aws-idp-document-page-content-regex` only. `classes` is free-form, so a class
written in the pre-JSON-Schema style — a bare `name:` plus
`document_page_content_regex:` and an `attributes:` list — is stored and merged
unchanged, with no validation error and no warning, and yields a document type
whose name is the empty string and whose regexes are `None`. Nothing matches, and
the only signal is that classification silently never takes the regex path.

**How it works:**
- Only applies to the `combined` strategy at page granularity (`classification.combined.granularity: page`)
- Each page's text content is checked against all class regex patterns
- First matching pattern wins and classifies the page instantly
- Falls back to LLM classification when no patterns match
- Provides info-level logging when matches occur

### Configuration Options

Both regex types are optional and can be used together:

```yaml
classes:
  - $schema: https://json-schema.org/draft/2020-12/schema
    $id: W2-Form
    type: object
    x-aws-idp-document-type: W2-Form
    description: W2 tax form with wage and tax information
    # Both regex types can be specified
    x-aws-idp-document-name-regex: "(?i).*w-?2.*"  # For single-class scenarios
    x-aws-idp-document-page-content-regex: "(?i)(form\\s+w-?2|wage\\s+and\\s+tax)"  # For page-level
    properties:
      EmployerEIN:
        type: string
        description: Employer identification number
```

### Performance Benefits

**Speed Improvements:**
- Regex matching is nearly instantaneous compared to LLM calls
- Document name regex: ~100-1000x faster (entire document classified instantly)
- Page content regex: ~10-50x faster per matched page

**Cost Savings:**
- Zero token usage for regex-matched classifications
- No Bedrock/SageMaker API calls for matched patterns
- Significant cost reduction for documents with recognizable patterns

**Deterministic Results:**
- Consistent classification results for pattern-matched documents
- Eliminates LLM variability for known document types
- Reliable classification for high-volume processing scenarios

### Best Practices for Regex Patterns

1. **Case-Insensitive Matching**: Use `(?i)` flag for robust matching
   ```regex
   (?i).*(invoice|bill).*  # Matches "Invoice", "INVOICE", "bill", "BILL"
   ```

2. **Flexible Whitespace**: Use `\\s+` for varying whitespace
   ```regex
   (?i)(gross\\s+pay|net\\s+pay)  # Matches "gross pay", "gross  pay", "GROSS PAY"
   ```

3. **Multiple Alternatives**: Use `|` for different possible terms
   ```regex
   (?i).*(payslip|paystub|salary|wage).*  # Matches any of these terms
   ```

4. **Specific Enough**: Balance specificity to avoid false matches
   ```regex
   # Good: Specific to payslips
   (?i)(gross\\s+pay|employee\\s+id|pay\\s+period)
   
   # Too broad: Could match many document types
   (?i)(pay|id|period)
   ```

### Error Handling

The regex system includes comprehensive error handling:

- **Compilation Errors**: Invalid regex patterns are logged and ignored, fallback to LLM
- **Runtime Errors**: Regex matching failures fallback to standard classification
- **Graceful Degradation**: System continues to work normally even with invalid patterns
- **Detailed Logging**: Debug and error logs help with pattern troubleshooting

### Integration Example

```python
from idp_common import classification, get_config
from idp_common.models import Document

# Load configuration with regex patterns
config = get_config()

# Initialize service - regex patterns are automatically used
service = classification.ClassificationService(
    region="us-east-1",
    config=config,
    backend="bedrock"
)

# Classification automatically uses regex when patterns match
document = service.classify_document(document)

# Check if regex was used
for page_id, page in document.pages.items():
    metadata = getattr(page, 'metadata', {})
    if metadata.get('regex_matched', False):
        print(f"Page {page_id} was classified using regex patterns")
    else:
        print(f"Page {page_id} was classified using LLM")
```

### Demonstration Notebook

See `notebooks/examples/step2_classification_with_regex.ipynb` for interactive demonstrations of:
- Document name regex classification
- Page content regex classification  
- Performance comparisons between regex and LLM methods
- Configuration examples and best practices
- Error handling scenarios

## Enforcing a Valid Class Vocabulary (Validation + Retry)

For every call that assigns a class, the service can guarantee the predicted
class is always one of the configured classes. After each LLM call, the
predicted class is validated against `self.valid_doc_types` (built from the
configured `classes`). On an out-of-vocabulary prediction the service
re-prompts the model — re-sending the original request content with an appended
correction message that lists the allowed classes — and retries up to a
configurable limit. Because classification runs at `temperature=0`, this
single-turn re-prompt (rather than an identical re-send) is what lets the model
change its answer.

Implemented in `ClassificationService.classify_page_bedrock` (page granularity),
`_classify_one_packet_window` (packet granularity) and
`_invoke_with_class_validation` (split-then-classify section classification), with the
helper `_build_validation_retry_content`. Metering is aggregated across all
attempts. The settings are read from the class-assigning call of the configured
strategy (`_class_validation_enforced`, `_class_validation_retries`).

```yaml
classification:
  fallback_class: unclassified        # default: unclassified
  combined:
    llm:
      validation:
        enforceValidClasses: true     # default: true
        maxValidationRetries: 2       # default: 2
```

- `validation.enforceValidClasses` — when `false`, an invalid class is logged and
  used as-is; the loop runs exactly once.
- `validation.maxValidationRetries` — number of re-prompts after the initial
  attempt (`0` = no retries).
- `fallback_class` — class assigned when all retries are exhausted; the
  resulting `PageClassification.classification.metadata` then carries a
  `validation_error` string, and a warning-severity
  `classification_invalid_class_fallback` issue is recorded on the section (see
  below). The document is **not** failed.

> At **packet granularity** a reply naming an invalid class for any segment
> re-prompts the whole window, and a segment still invalid after the retries gets
> the fallback class. **Section classification under split-then-classify**
> re-prompts its one segment. A class outside the vocabulary is exactly as wrong
> whether a page or a segment produced it. The fallback carries no confidence
> score, because it is not a prediction.

See `notebooks/misc/classification-valid-class-enforcement.ipynb` for a
deterministic, mock-driven walkthrough of all three scenarios (retry-then-valid,
retries-exhausted-fallback, and enforcement-disabled).

## Reporting a Page That Could Not Be Classified

`_record_unclassified_page_issues` runs once per document on the combined
page-granularity path, immediately after section splitting, and
turns each page classification that carries an `error` or a `validation_error` into
a `ProcessingIssue` on the section holding that page.

⚠️ **It has one call site, in `_classify_pages_multimodal`.**
`packet_classify_document` does not call it and records none of these: it decides
per *segment* rather than per page and never builds the `List[PageClassification]`
this takes, so covering it is real work rather than an extra call. A page that
falls inside no segment's range is appended to no section either, producing
neither data nor a record; the range is now *logged* when it covers no page of
the document, but the per-page issue is still follow-up work.

**Why it exists.** Nothing in this service produced a `ProcessingIssue` before, and
neither of the two places the reason was written reaches a user:

- `DocumentClassification.metadata` is copied onto the `Page` object with
  `setattr`, and `Page` has no `metadata` field — so it is absent from
  `Document.to_dict()` and never survives the Step Functions hop or reaches
  DynamoDB;
- the matching `document.errors` line is read only inside
  `processresults_function`'s `Status.FAILED` branch, which a document that
  completes never enters.

So a page whose classification failed after retries became `unclassified`, its
section took extraction's empty-schema route, and the document finished green —
which is the same defect as `#1006` one stage upstream.

**Why the section and not the document.** `ProcessingIssues` is a field of
`Section` in the API schema; the document carries only `ProcessingIssueCount`. A
document-level issue would raise that count and then have no text to show for it.

| `metadata` key | `unclassified_reason` | Issue code | Severity | Reached from |
|---|---|---|---|---|
| `error` | `no_content` | `classification_page_no_content` | warning | `classify_page_bedrock` |
| `error` | `failed` (the default) | `classification_failed` | error | `classify_page_sagemaker` only |
| `validation_error` | — | `classification_invalid_class_fallback` | warning | `classify_page_bedrock` |

`_create_unclassified_result` sets `unclassified_reason`, defaulting to `failed` so
a call site added later is loud rather than quiet by omission.

**`classification_failed` does not fire on the Bedrock backend.** All four
default-reason call sites are in `classify_page_sagemaker`; `classify_page_bedrock`
re-raises from its `except`, and the thread-pool handler that catches it does not
append the page to `all_page_results`, so this function never sees it. No signal is
lost — that path marks the document `FAILED`, and `document.errors` *is* read on a
failed document — but the error-severity code is a UDOP-backend code in practice.
Note that a test mocking `classify_page`'s **return value** cannot surface this,
since a returned unclassified result is SageMaker-shaped by construction.

Severity is what keeps the signal usable: an error indicator on most documents is
one nobody reads. The fallback case is the commonest of the three, because it is
where a page the model cannot place ends up, and a class *was* assigned — just not
the model's. `classification_page_no_content` needs **both** the page's OCR text and
its page image to be unusable (`classify_page_bedrock` checks both), so a blank page
that still has an image does not land there.

One issue per (section, cause) with the page IDs listed, so a 50-page run of
unclassifiable pages does not produce 50 identical rows. The write replaces only
this stage's issues **of the same code**, because the DynamoDB writer replaces the
whole section map: a failed page and an unclassifiable page in one section are two
different facts and neither may evict the other.

Two bounds on the `root_cause`, both because the text is model output or a service
error message and the issue rides to DynamoDB inside the section map — whose 400 KB
item ceiling the assessment path guards explicitly while
`serialize_processing_issues` truncates nothing. Each page's detail is capped at
`_MAX_ISSUE_DETAIL_CHARS`, and at most `_MAX_ISSUE_DETAILS` **distinct** details are
rendered, with a count for the remainder. The details are tracked **per page**, not
per code: two pages can fail the same way for different reasons, and keeping only
the first detail made every section's `root_cause` blame one of them for all of
them — which listing the affected page ids beside it turns from a vague message into
an actively wrong one.

No CloudWatch metric is published from here — see
[`docs/classification.md`](../../../../docs/classification.md#pages-classification-could-not-classify)
for the reasoning and the operator-facing table.

## Usage Example

### Using with Bedrock LLMs (Default)

```python
from idp_common import classification, get_config
from idp_common.models import Document

# Load configuration
config = get_config()

# Initialize classification service with Bedrock backend
service = classification.ClassificationService(
    region="us-east-1",
    config=config,
    backend="bedrock"  # This is the default
)

# Create or get a Document object
document = Document(
    id="doc-123",
    input_bucket="input-bucket",
    input_key="document.pdf",
    output_bucket="output-bucket",
    pages={
        "1": {
            "page_id": "1",
            "parsed_text_uri": "s3://bucket/document/pages/1/result.json",
            "image_uri": "s3://bucket/document/pages/1/image.jpg",
            "raw_text_uri": "s3://bucket/document/pages/1/rawText.json"
        }
    }
)

# Classify the document - updates the Document object directly
document = service.classify_document(document)

# Document now contains classification results
print(f"Document has {len(document.sections)} sections")
for section in document.sections:
    print(f"Section {section.section_id}: {section.classification}")
    print(f"Pages: {section.page_ids}")
```

### Using with SageMaker UDOP Models

```python
import os

from idp_common import classification, get_config
from idp_common.models import Document

# The SageMaker backend reads its endpoint name from the SAGEMAKER_ENDPOINT_NAME
# environment variable, not from the configuration, and raises ValueError at
# construction when it is unset.
os.environ["SAGEMAKER_ENDPOINT_NAME"] = "udop-classification-endpoint"
config = get_config()

# Initialize classification service with SageMaker backend
service = classification.ClassificationService(
    region="us-east-1",
    config=config,
    backend="sagemaker"
)

# Create or get a Document object
document = Document(
    id="doc-123",
    input_bucket="input-bucket",
    input_key="document.pdf",
    output_bucket="output-bucket",
    pages={
        "1": {
            "page_id": "1",
            "parsed_text_uri": "s3://bucket/document/pages/1/result.json",
            "image_uri": "s3://bucket/document/pages/1/image.jpg",
            "raw_text_uri": "s3://bucket/document/pages/1/rawText.json"
        }
    }
)

# Classify the document using SageMaker
document = service.classify_document(document)

# Access classification results from the Document
print(f"Document status: {document.status}")
for page_id, page in document.pages.items():
    print(f"Page {page_id} classified as: {page.classification}")
```

### Legacy Method (Still Supported)

```python
# Classify a single page
page_result = service.classify_page(
    page_id="1",
    text_uri="s3://bucket/document/pages/1/result.json",
    image_uri="s3://bucket/document/pages/1/image.jpg"
)

# Print the classification result
print(f"Page classified as: {page_result.classification.doc_type}")

# Classify multiple pages concurrently
pages = {
    "1": {"parsedTextUri": "s3://bucket/document/pages/1/result.json", "imageUri": "s3://bucket/document/pages/1/image.jpg"},
    "2": {"parsedTextUri": "s3://bucket/document/pages/2/result.json", "imageUri": "s3://bucket/document/pages/2/image.jpg"}
}

result = service.classify_pages(pages)

# Print the sections
for section in result.sections:
    print(f"Section {section.section_id}: {section.classification.doc_type}")
    for page in section.pages:
        print(f"  - Page {page.page_id}")

# Convert to dictionary for API response
api_response = result.to_dict()
```

## Configuration

The classification service uses the following configuration structure. Each class
is a JSON Schema whose `x-aws-idp-document-type` is the class name. The Bedrock
model is set per call (here `classification.combined.llm.model`); there is no
top-level model key. The SageMaker backend takes its endpoint name from the
`SAGEMAKER_ENDPOINT_NAME` environment variable rather than from the configuration.

```json
{
  "classes": [
    {
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "$id": "invoice",
      "x-aws-idp-document-type": "invoice",
      "type": "object",
      "description": "An invoice that specifies an amount of money to be paid.",
      "properties": {}
    },
    {
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "$id": "financial_statement",
      "x-aws-idp-document-type": "financial_statement",
      "type": "object",
      "description": "Documents that summarize financial performance, such as income statements, balance sheets, or cash flow statements.",
      "properties": {}
    }
  ],
  "classification": {
    "strategy": "combined",
    "combined": {
      "granularity": "page",
      "page": {
        "system_prompt": "You are a document classification expert...",
        "task_prompt": "Classify the following document into one of these types: {CLASS_NAMES_AND_DESCRIPTIONS}...\n\nDocument text:\n{DOCUMENT_TEXT}"
      },
      "llm": {
        "model": "us.amazon.nova-2-lite-v1:0",
        "temperature": 0,
        "top_k": 5
      }
    },
    "document_shape": {
      "sectionSplitting": "llm_determined" // "llm_determined" (default) | "page" | "disabled" - see "Section splitting values"
    }
  }
}
```

## Integration with Lambda Functions

### Using with Bedrock Backend

```python
from idp_common import classification, get_config
from idp_common.models import Document, Status

def handler(event, context):
    # Extract document from event
    document = Document.from_dict(event["OCRResult"]["document"])
       
    # Initialize classification service
    config = get_config()
    service = classification.ClassificationService(config=config) 
    
    # Classify document
    document = service.classify_document(document)
    
    # Return response
    return {
        "document": document.to_dict()
    }
```

### Using with SageMaker Backend

```python
from idp_common import classification, get_config
from idp_common.models import Document, Status

def handler(event, context):
    # Extract document from event
    document = Document.from_dict(event["OCRResult"]["document"])
    
    # The SageMaker backend reads the endpoint name from the
    # SAGEMAKER_ENDPOINT_NAME environment variable set on the function.
    config = get_config()
    
    # Initialize classification service with SageMaker backend
    service = classification.ClassificationService(
        config=config,
        backend="sagemaker"
    )
    
    # Classify document using SageMaker
    document = service.classify_document(document)
    
    # Return response
    return {
        "document": document.to_dict()
    }
```

## Data Models

- `DocumentType`: Definition of a document type with name and description
- `DocumentClassification`: Classification result with document type and an optional confidence (`None` = not scored)
- `PageClassification`: Classification result for a single page
- `DocumentSection`: A section of consecutive pages with the same classification
- `ClassificationResult`: Overall result of a classification operation
- `Document`: Core document data model used throughout the IDP pipeline

## DynamoDB Caching for Resilient Classification

The classification service now supports optional DynamoDB caching to improve efficiency and resilience when processing documents with multiple pages. This feature addresses throttling scenarios where some pages succeed while others fail, avoiding the need to reclassify already successful pages on retry.

### How It Works

1. **Cache Check**: Before processing, the service checks for cached classification results for the document
2. **Selective Processing**: Only pages without cached results are classified
3. **Exception-Safe Caching**: Successful page results are cached even when other pages fail
4. **Retry Efficiency**: Subsequent retries only process previously failed pages

### Configuration

#### Via Constructor Parameter
```python
from idp_common import classification, get_config

config = get_config()
service = classification.ClassificationService(
    region="us-east-1",
    config=config,
    backend="bedrock",
    cache_table="classification-cache-table"  # Enable caching
)
```

#### Via Environment Variable
```bash
export CLASSIFICATION_CACHE_TABLE=classification-cache-table
```

```python
# Cache table will be automatically detected from environment
service = classification.ClassificationService(
    region="us-east-1",
    config=config,
    backend="bedrock"
)
```

### DynamoDB Table Schema

The cache uses the following DynamoDB table structure:

- **Primary Key (PK)**: `classcache#{document_id}#{workflow_execution_arn}`
- **Sort Key (SK)**: `none`
- **Attributes**:
  - `page_classifications` (String): JSON-encoded successful page results
  - `cached_at` (String): Unix timestamp of cache creation
  - `document_id` (String): Document identifier
  - `workflow_execution_arn` (String): Workflow execution ARN
  - `ExpiresAfter` (Number): TTL attribute for automatic cleanup (24 hours)

#### Example DynamoDB Item
```json
{
  "PK": "classcache#doc-123#arn:aws:states:us-east-1:123456789012:execution:MyWorkflow:abc-123",
  "SK": "none",
  "page_classifications": "{\"1\":{\"doc_type\":\"invoice\",\"confidence\":null,\"metadata\":{\"metering\":{...}},\"image_uri\":\"s3://...\",\"text_uri\":\"s3://...\",\"raw_text_uri\":\"s3://...\"},\"2\":{...}}",
  "cached_at": "1672531200",
  "document_id": "doc-123",
  "workflow_execution_arn": "arn:aws:states:us-east-1:123456789012:execution:MyWorkflow:abc-123",
  "ExpiresAfter": 1672617600
}
```

### Benefits

- **Cost Reduction**: Avoids redundant API calls to Bedrock/SageMaker for already-classified pages
- **Improved Resilience**: Handles partial failures gracefully during concurrent processing
- **Faster Retries**: Subsequent attempts only process failed pages, not the entire document
- **Automatic Cleanup**: TTL ensures cache entries don't accumulate indefinitely
- **Thread Safety**: Safe for concurrent page processing within the same document

### Example: Resilient Processing Flow

```python
from idp_common import classification, get_config
from idp_common.models import Document

config = get_config()
service = classification.ClassificationService(
    region="us-east-1",
    config=config,
    backend="bedrock",
    cache_table="classification-cache-table"
)

# Create document with 5 pages
document = Document(
    id="doc-123",
    workflow_execution_arn="arn:aws:states:us-east-1:123456789012:execution:MyWorkflow:abc-123",
    pages={
        "1": {...},
        "2": {...},
        "3": {...},
        "4": {...},
        "5": {...}
    }
)

try:
    # First attempt: pages 1,2,4 succeed, pages 3,5 fail due to throttling
    document = service.classify_document(document)
except Exception as e:
    # Pages 1,2,4 are cached automatically before exception is raised
    print(f"Classification failed: {e}")

try:
    # Retry: only pages 3,5 are processed (1,2,4 loaded from cache)
    document = service.classify_document(document)
    print("Document classified successfully on retry")
except Exception as e:
    print(f"Retry failed: {e}")
```

### Cache Lifecycle

1. **Creation**: Cache entries are created when `classify_document()` completes successfully or encounters exceptions
2. **Retrieval**: Cache is checked at the start of each `classify_document()` call
3. **Update**: Cache entries are updated with new successful results from each processing attempt
4. **Expiration**: Entries automatically expire after 24 hours via DynamoDB TTL

### Important Notes

- Caching only applies to the `classify_document()` method, not individual `classify_page()` calls
- Cache entries are scoped to specific document and workflow execution combinations
- Only successful page classifications (without errors in metadata) are cached
- The cache is transparent - existing code continues to work without modifications

## Backend Options

### Bedrock Backend

The Bedrock backend uses Amazon Bedrock LLMs to classify documents:

- Supports multiple model options (Claude, Titan, etc.)
- Works with both text and image content 
- Uses natural language understanding for classification
- Configurable system prompts and parameters

### SageMaker Backend

The SageMaker backend uses custom UDOP (Unified Document Processing) models:

- Uses vision-language models specifically trained for document understanding
- Requires both image and raw text URIs to be available
- Better performance for document-specific classification tasks
- Requires a deployed SageMaker endpoint

## Few Shot Example Feature

The classification service supports few shot learning through example-based prompting. This feature allows you to provide concrete examples of documents with their expected classifications and attribute extractions, significantly improving model accuracy and consistency.

### Overview

Few shot examples work by including reference documents with known classifications and expected attribute values in the prompts sent to the AI model. This helps the model understand the expected format and accuracy requirements for your specific use case.

### Configuration

Few shot examples are configured in the document class definitions within your configuration file:

```yaml
classes:
  - name: letter
    description: "A formal written correspondence..."
    attributes:
      - name: sender_name
        description: "The name of the person who wrote the letter..."
      # ... other attributes
    examples:
      - x-aws-idp-class-prompt: "This is an example of the class 'letter'"
        name: "Letter1"
        x-aws-idp-attributes-prompt: |
          expected attributes are:
              "sender_name": "Will E. Clark",
              "sender_address": "206 Maple Street P.O. Box 1056 Murray Kentucky 42071-1056",
              "recipient_name": "The Honorable Wendell H. Ford",
              # ... other expected attributes
        x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/letter1.jpg"
      - x-aws-idp-class-prompt: "This is an example of the class 'letter'" 
        name: "Letter2"
        x-aws-idp-attributes-prompt: |
          expected attributes are:
              "sender_name": "William H. W. Anderson",
              # ... other expected attributes
        x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/letter2.png"
```

### Configuration Parameters

Each few shot example includes:

- **x-aws-idp-class-prompt**: A description identifying this as an example of the document class
- **name**: A unique identifier for the example (for reference and debugging)
- **x-aws-idp-attributes-prompt**: The expected attribute extraction results in a structured format
- **x-aws-idp-image-path**: Path to example document image(s) - supports single files, local directories, or S3 prefixes

#### Image Path Options

The `x-aws-idp-image-path` field now supports multiple formats for maximum flexibility:

**Single Image File (Original functionality)**:
```yaml
x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/letter1.jpg"
```

**Local Directory with Multiple Images (New)**:
```yaml
x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/"
```

**S3 Prefix with Multiple Images (New)**:
```yaml
x-aws-idp-image-path: "s3://my-config-bucket/few-shot-examples/letter/"
```

**Direct S3 Image URI**:
```yaml
x-aws-idp-image-path: "s3://my-config-bucket/few-shot-examples/letter/example1.jpg"
```

When pointing to a directory or S3 prefix, the system automatically:
- Discovers all image files with supported extensions (`.jpg`, `.jpeg`, `.png`, `.gif`, `.bmp`, `.tiff`, `.tif`, `.webp`)
- Sorts them alphabetically by filename for consistent ordering
- Includes each image as a separate content item in the few-shot examples
- Gracefully handles individual image loading failures without breaking the entire process

#### Environment Variables for Path Resolution

The system uses these environment variables for resolving relative paths:

- **`CONFIGURATION_BUCKET`**: S3 bucket name for configuration files
  - Used when `x-aws-idp-image-path` doesn't start with `s3://`
  - The path is treated as a key within this bucket

- **`ROOT_DIR`**: Root directory for local file resolution
  - Used when `CONFIGURATION_BUCKET` is not set
  - The path is treated as relative to this directory

### Benefits

Using few shot examples provides several advantages:

1. **Improved Accuracy**: Models perform better when given concrete examples
2. **Consistent Formatting**: Examples help ensure consistent output structure
3. **Domain Adaptation**: Examples help models understand domain-specific terminology
4. **Reduced Hallucination**: Examples reduce the likelihood of made-up data
5. **Better Edge Case Handling**: Examples can demonstrate how to handle unusual cases

### Best Practices

When creating few shot examples:

#### 1. Quality over Quantity
- Use 1-3 high-quality examples per document class
- Ensure examples are representative of real-world documents
- Include diverse examples that cover different variations

#### 2. Clear and Complete Examples
```yaml
# Good example - specific and complete
x-aws-idp-attributes-prompt: |
  expected attributes are:
      "invoice_number": "INV-2024-001",
      "invoice_date": "01/15/2024",
      "vendor_name": "ACME Corp",
      "total_amount": "$1,250.00"

# Avoid incomplete examples
x-aws-idp-attributes-prompt: |
  expected attributes are:
      "invoice_number": "INV-2024-001"
      # Missing other important attributes
```

#### 3. Handle Null Values Appropriately
```yaml
x-aws-idp-attributes-prompt: |
  expected attributes are:
      "sender_name": "John Smith",
      "cc": null,  # Explicitly show when fields are not present
      "reference_number": null
```

#### 4. Use Realistic Examples
- Choose examples that represent typical documents in your use case
- Include examples with both common and edge case scenarios
- Ensure image quality is good and text is clearly readable

### Usage with Classification Service

The few shot examples are automatically integrated when using the classification service:

```python
from idp_common import classification, get_config
from idp_common.models import Document

# Load configuration with few shot examples
config = get_config()

# Initialize service - few shot examples are automatically used
service = classification.ClassificationService(
    region="us-east-1", 
    config=config
)

# Examples are automatically included in prompts during classification
document = service.classify_document(document)
```

The service automatically:
1. Loads few shot examples from the configuration
2. Includes them in classification prompts using the `{FEW_SHOT_EXAMPLES}` placeholder
3. Formats examples appropriately for both classification and extraction tasks

The shipped default `classification.combined.page.task_prompt` in
`config/system_defaults/base-classification.yaml` **already contains
`{FEW_SHOT_EXAMPLES}`**, positioned after `<document-types>` and before
`<<CACHEPOINT>>` (examples are static per config, so they belong in the cacheable
prefix — which matters here because classification runs per page). Defining
`x-aws-idp-examples` on a class is therefore enough; a config that supplies its own
`task_prompt` must include the placeholder itself, exactly once.

Classification uses each example's `x-aws-idp-class-prompt` (legacy alias:
`classPrompt`) plus `x-aws-idp-image-path`; examples whose class-prompt is empty are
skipped, and with no examples at all the placeholder contributes nothing. Unlike
extraction, classification includes examples from **all** classes, not just one.

### Example Configuration Structure

Here's a complete example showing how few shot examples integrate with document class definitions:

```yaml
classes:
  - name: email
    description: "A digital message with email headers..."
    attributes:
      - name: from_address
        description: "The email address of the sender..."
      - name: to_address  
        description: "The email address of the primary recipient..."
      - name: subject
        description: "The topic of the email..."
      - name: date_sent
        description: "The date and time when the email was sent..."
    examples:
      - x-aws-idp-class-prompt: "This is an example of the class 'email'"
        name: "Email1"
        x-aws-idp-attributes-prompt: |
          expected attributes are: 
             "from_address": "john.doe@company.com",
             "to_address": "jane.smith@client.com", 
             "subject": "FW: Meeting Notes 4/20",
             "date_sent": "04/18/2024"
        x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/email1.jpg"

classification:
  combined:
    page:
      task_prompt: |
        Classify this document into exactly one of these categories:
    
        {CLASS_NAMES_AND_DESCRIPTIONS}
    
        <few_shot_examples>
        {FEW_SHOT_EXAMPLES}
        </few_shot_examples>
    
        <document_ocr_data>
        {DOCUMENT_TEXT}
        </document_ocr_data>
```

### Optional `{CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}` placeholder

In addition to the standard `{CLASS_NAMES_AND_DESCRIPTIONS}` placeholder,
classification prompts support an opt-in
`{CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}` placeholder that expands
to each class's name, description, **and** the schema attribute names
(extraction fields) declared for that class. This gives the classifier
extra disambiguation signal in domains where multiple classes share
similar names but have very different extraction schemas (e.g.
`appraisal_report` vs `inspection_report`).

- **Page-granularity** prompts get an XML-tagged listing.
- **Packet-granularity** prompts get a three-column markdown table
  (`type | description | attributes`). Split-then-classify section classification uses
  the packet builders, so it gets the table too.
- The placeholder is fully **opt-in** — token usage and cost are
  unchanged for users who don't reference it. The library only
  substitutes placeholders that actually appear in the template.
- Per-class attribute counts are soft-capped at
  `ClassificationService.MAX_ATTRIBUTES_PER_CLASS` (default 50) to
  prevent pathologically large schemas from bloating prompts.
- Groups and list-item shapes declared as a local `$ref` into the class's
  `$defs` (what the UI's schema editor emits) are dereferenced via
  `idp_common.config.schema_utils.deref_schema` before their `type` is
  read, so they contribute their child names just like an inline group.
  Unresolvable refs degrade to the property name; a recursive definition is
  walked until it re-enters itself, and the re-entered member becomes a leaf.
- The walk itself stops at `_MAX_WALK_NAMES` (10 x the soft cap above).
  `MAX_ATTRIBUTES_PER_CLASS` truncates the walk's *result*, so it cannot bound
  the walk — and because a `$defs` definition can be re-entered on every
  sibling branch, dereferencing lets a ~2 KB schema expand to hundreds of
  thousands of names. Hitting the ceiling logs a warning.
- `PromptPreview.tsx` in the Web UI carries a deliberate port of this walk
  (`getAttributeNamesForClass` + `derefSchema`) so the prompt preview matches
  what the backend builds. Change both together; the TS tests mirror the
  Python expectations case for case.

Example:

```yaml
classification:
  combined:
    page:
      task_prompt: |
        Classify this page using the schema attribute names as a
        disambiguation signal:

        {CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}

        Document text:
        {DOCUMENT_TEXT}

        Respond with JSON: {"class": "...", "document_boundary": "start|continue"}
```

See [`docs/classification.md`](../../../../docs/classification.md) for
schema-walking rules, rendered output examples, and recommended use
cases. Tracked in GitHub issue #262.

### Troubleshooting

Common issues and solutions:

1. **Images Not Found**: Ensure image paths are correct and files exist
2. **Inconsistent Results**: Review example quality and ensure they're representative
3. **Poor Performance**: Consider adding more diverse examples or improving example quality
4. **Format Errors**: Ensure x-aws-idp-attributes-prompt follows exact JSON-like format expected by your prompts

## Future Enhancements

- ✅ Support for SageMaker UDOP models
- ✅ Direct integration with Document data model
- ✅ Improved error handling and retry mechanisms
- ✅ Few shot example support for improved accuracy
- 🔲 Better confidence score estimation
- 🔲 More advanced document structure analysis
- 🔲 Support for additional classification backends (custom models)
- 🔲 Multi-model classification for improved accuracy
- 🔲 Dynamic few shot example selection based on document similarity
