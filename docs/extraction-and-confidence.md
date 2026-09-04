---
title: "Extraction & Confidence"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Extraction & Confidence

Information extraction transforms unstructured document content into structured
data. As of **config v0.6**, per-field **confidence** and **geometry (bounding
boxes)** are treated as **outputs of extraction**, not a separate downstream
stage. Their settings live under `extraction.confidence.*` and
`extraction.geometry.*`; human-in-the-loop review is configured under the
top-level `hitl.*` block.

This guide consolidates what used to live across three separate documents
(Customizing Extraction, the Assessment Feature, and Bounding Box Integration)
into one place, reflecting the v0.6 model where extraction produces the value,
its confidence, and its location together.

> **Config v0.6 at a glance**
>
> - `extraction.confidence.{enabled, mode, model, list_batch_size, image, system_prompt, task_prompt, ...}` — confidence scoring (formerly the top-level `assessment` block).
> - `extraction.geometry.{mode, task_prompt_bbox}` — field bounding boxes (formerly `assessment.geometry_mode` / `assessment.ground_geometry_in_ocr`).
> - `hitl.{enabled, confidence_threshold}` — human review routing (top-level).
> - Existing pre-v0.6 configs are **migrated automatically on read** — no manual edit is required. See [Granular Assessment Retirement](migration-granular-retirement.md).

---

## 0. Choosing a configuration (start here)

Two independent choices drive everything below:

1. **Extraction mode** — how values are pulled from the document: **Simple** (one inference) or **Advanced/agentic** (a tool-using agent that can shard, validate, and self-correct).
2. **Confidence mode** — how per-field confidence + bounding boxes are produced: **`separate`** (a dedicated confidence pass), **`integrated`** (confidence rides on the extraction inference), or **`off`**.

They combine freely. The tables below give pros/cons and a recommendation; the rest of the guide is the detail.

### Extraction mode: Simple vs Advanced

| | **Simple** (non-agentic) — *default* | **Advanced** (agentic) |
|---|---|---|
| **How it works** | One Bedrock inference returns the structured result | Strands agent with a structured-output tool; can shard large sections, validate against the schema, and self-correct |
| **Pros** | Cheapest & fastest; fewest moving parts; works with every model incl. OpenAI GPT-5.x | Highest accuracy on complex/nested schemas; guaranteed schema compliance; deterministic **table parsing** for big tables; **sharding** for long docs; model **escalation** on validation failure |
| **Cons** | No built-in validation/retry; a single huge document must fit one inference (context-overflow / read-timeout risk); weaker on deeply nested structures | More inferences → higher per-doc cost & latency; requires a tool-use model (no OpenAI GPT-5.x); still in **preview** |
| **Choose when** | Most documents; small–medium size; simple/flat schemas; lowest cost matters | Complex/nested schemas, strict validation needs, **large documents or big multi-row tables**, business-critical accuracy |

> **Rule of thumb:** start Simple. Move to Advanced when you hit nested-schema accuracy limits, need schema-format validation, or the document is large enough that one inference can't hold it (long tables, 20+ dense pages).

### Confidence mode: separate vs integrated vs off

| | **`separate`** — *default* | **`integrated`** | **`off`** |
|---|---|---|---|
| **How it works** | A dedicated confidence inference (Simple: the standalone Assessment step; Advanced: one pass **inside each shard**) | Confidence rides on the extraction inference — no separate pass. **Simple** uses **1S-TopK** (the model returns top-K guesses + probabilities per field); **Advanced** has the agent emit confidence via its tools | No confidence produced |
| **Inferences added** | +1 (Simple) / +1 per shard (Advanced) | 0 (Simple, single-shot) / 0–1 (Advanced) | 0 |
| **Pros** | Best-calibrated (a fresh look at finalized values); can use a **cheaper model** than extraction (default Nova Lite); large lists batched reliably | Fewest inferences → lowest confidence cost; one round-trip on the simple path | Zero confidence cost |
| **Cons** | Extra inference(s) → more cost/latency than `integrated` | On the **Simple** path everything (context + values + all confidence) must fit **one** response → truncation risk on large docs/tables; can't use a separate cheaper model | No confidence → no HITL routing, no UI confidence/threshold signals |
| **Choose when** | **Default for almost everyone** — the calibration and cheap-model economics usually win | Small documents where one inference comfortably holds values **and** confidence, and you want to minimize round-trips | Confidence genuinely not needed (no HITL, no reliability signal) |

> **Recommended defaults:** **Simple + `separate`** for most workloads; **Advanced + `separate`** for complex or large documents. Reach for `integrated` only on small docs where minimizing inferences matters, and `off` only when you don't consume confidence at all.

> **Simple + `integrated` uses 1S-TopK.** On the Simple path, `integrated` mode asks the model for its **top-K guesses with probabilities** per field (`G1/P1` … `GK/PK`) in one call; the top guess becomes the value and its probability the confidence. Enumerating alternatives yields better-calibrated, less-overconfident scores than a single value + a single confidence number (Tian et al., *"Just Ask for Calibration"*, EMNLP 2023). The output `result.json` is identical in shape to `separate` mode (same `inference_result` + `explainability_info`), so HITL, evaluation, reporting, and the UI are unchanged, and the standalone Assessment step auto-skips. The prompt is editable in the UI ("Task prompt (1-Stage TopK extraction + confidence — simple)") / `extraction.task_prompt_extraction_with_confidence_topk`. See the [reference config](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/config_library/unified/realkie-fcc-verified/config-1s-topk-with-ocr-image.yaml) and the [extraction library README](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/lib/idp_common_pkg/idp_common/extraction/README.md#1s-topk-single-stage-extraction--confidence-simple-mode).

> ⚠️ **Simple + `integrated` truncates long lists — prefer `separate` for list-heavy documents.** Benchmarked on a 100-row transaction list, Simple + `integrated` returned **10 of 100 rows** while Simple + `separate` returned 100 of 100 in every repeat, at ~1.5–2× the cost. The mechanism is output volume: the TopK envelope costs several guesses *per cell*, so a list that fits comfortably in a plain extraction can exceed what the model will emit in one response, and it stops emitting rows rather than erroring. Two changes reduce it — list cells are now asked for a **single** guess (`G1/P1` only) instead of four, and the prompt no longer told the model to make each guess *"as short as possible"* (which was also causing it to return shortened *values*) — but the fundamental single-response limit remains. **On list-bearing schemas use `separate`.** See [config-guidance](./benchmarking/config-guidance.md) for the measured comparison.

> **Large lists & truncation are handled on every path.** Whichever combination you pick, long list fields are assessed in sequential batches (`list_batch_size`), and if the confidence model truncates a batch at its output-token ceiling the batch is **recursively split until it fits** — so you get complete per-cell coverage without tuning. See [Large-list batching](#large-list-batching-list_batch_size). For documents that are large because of a *very large single section* (not just a long list), prefer **Advanced sharding** — see [Large-Document Guidance](#8-large-document-guidance).

---

## 1. Extraction Configuration

The extraction service supports two modes (see
[§0 Choosing a configuration](#0-choosing-a-configuration-start-here) for
pros/cons and a recommendation):

| Mode | Also called | Default | Best for |
|---|---|---|---|
| **Simple** | non-agentic / traditional | ✅ default | Most documents; single-inference extraction |
| **Advanced** | agentic | opt-in | Complex/nested schemas, strict validation, large documents and big tables (shards extraction **and** confidence assessment) |

### Simple (non-agentic) extraction

Simple extraction sends the system prompt and task prompt to Bedrock in a single
inference and parses the structured (JSON or YAML) response. It is the default
and is a good fit for the majority of documents.

```yaml
extraction:
  agentic:
    enabled: false            # Simple mode (default)
  model: anthropic.claude-3-haiku-20240307-v1:0
  temperature: 0.0
  reasoning_effort: low       # reasoning-capable models only (see note below)
```

> **Output tokens:** extraction and the confidence pass always request the
> selected model's **maximum** output — there is no `max_tokens` config knob for
> them. Bedrock's default-when-omitted truncates, so the client sets it
> explicitly from the per-model limits (seeded from
> `config_library/model_config_limits.yaml` and editable in the web UI under
> **View / Edit Model Limits**); completeness matters more than an output cap
> here. (`classification` / `summarization` keep their `max_tokens` knob.)
>
> **Reasoning effort:** for reasoning-capable models — Claude Sonnet 5 / Sonnet
> 4.6 / Opus 4.5–4.8 / Fable 5 (`low`|`medium`|`high`|`xhigh`|`max`), OpenAI
> GPT-5.x (`minimal`|`low`|`medium`|`high`), and xAI Grok
> (`none`|`low`|`medium`|`high`|`xhigh`, **not** `max`) — `reasoning_effort` controls how much
> the model reasons before answering. Extraction **defaults to `low`**: a full
> effort sweep found higher effort adds output-token cost with negligible
> extraction-accuracy gain. Raise it per-config for reasoning-heavy documents.
> Ignored by Nova, Sonnet 4.5, and Haiku 4.5.

### Advanced (agentic) extraction

Advanced (agentic) extraction uses the Strands agent framework with tools for
structured output, giving superior accuracy and consistency — especially for
complex documents with nested structures or strict schema requirements. It
**shards** large sections and runs both extraction and confidence assessment
per shard.

> **Preview Status**: Agentic extraction is currently in preview. While it
> demonstrates significant improvements in accuracy and reliability, we
> recommend thorough testing in your specific use case before production
> deployment.

**When to enable agentic extraction** — when you need:

- **Schema Compliance**: Guaranteed adherence to defined data structures
- **Data Validation**: Automatic validation with retry mechanisms
- **Complex Structures**: Proper handling of nested objects and arrays
- **Date Standardization**: Consistent date formatting
- **Self-Correction**: Automatic fixing of extraction errors
- **Production Reliability**: Higher accuracy for business-critical data
- **Extensibility**: Future integration with Model Context Protocol (MCP) servers for advanced validation, enrichment, and external data lookups during extraction

```yaml
extraction:
  agentic:
    enabled: true             # Advanced mode
  model: us.anthropic.claude-sonnet-4-20250514-v1:0
```

#### Supported models for agentic extraction

Agentic extraction requires models with tool-use support:

- **Anthropic Claude Sonnet** models (recommended for optimal performance)
  - `anthropic.claude-3-5-sonnet-20241022-v2:0` — Best balance of speed and accuracy
  - `anthropic.claude-3-7-sonnet-20250219-v1:0` — Latest with enhanced capabilities
- **Anthropic Claude Opus** models (for highest accuracy requirements)
- **Amazon Nova Pro** (AWS native alternative)
- **Amazon Nova Premier** (for complex multi-modal extraction)

> **⚠️ OpenAI GPT-5.x cannot be used with agentic extraction.** All
> `openai.gpt-5.*` models (`openai.gpt-5.4`, `openai.gpt-5.5`, and GPT-5.6
> Sol/Terra/Luna) run on the `bedrock-mantle` Responses API and do not support
> the Converse-based Strands agent loop. Pairing an OpenAI model with
> `extraction.agentic.enabled: true` is a **hard error** in
> `idp-cli config-validate` and raises at runtime. OpenAI models are fully
> supported for **Simple (non-agentic) extraction**. See
> [OpenAI GPT-5.x Models](openai-models.md).

> **✅ xAI Grok CAN be used with agentic extraction.** Grok 4.6
> (`us.xai.grok-4.6`, `global.xai.grok-4.6`) is served on the standard Converse
> API and accepts a `toolConfig` with all three `toolChoice` modes, so the Strands
> agent loop works — making it the only non-Claude/non-Nova option for Advanced
> extraction. Its 500K context also yields a larger shard budget (~90K tokens)
> than a 200K-context Claude model (~18K). Note that `temperature` / `top_p` are
> rejected by Grok and silently omitted; tune it with `reasoning_effort`
> (`none`|`low`|`medium`|`high`|`xhigh`) instead. See
> [xAI Grok Models](grok-models.md).

#### Cost considerations

Agentic extraction may have slightly higher costs due to additional processing
for validation and correction, tool-use requiring more capable models, and
retry attempts. The benefits typically outweigh the costs: agentic extraction
improves model performance significantly — for example, Claude Sonnet 3.5 gains
over 20% in accuracy on the [getomni-ai benchmark](https://getomni.ai/blog/ocr-benchmark).

- **100% schema compliance** vs frequent validation failures
- **Reduced manual review** and correction efforts
- **Automatic caching**: For supported models, prompt and tool caching is automatically enabled, reducing costs for repeated extractions with the same configuration

##### Dropping the duplicated schema (`restate_schema_in_system_prompt`)

Agentic extraction sends your class schema **three times** per request: as the
tool's input schema (required by the API), restated as JSON in the system prompt,
and again in the schema-reminder tool. Copies 2 and 3 are byte-identical, so the
system-prompt copy is pure duplication — measured at **2,600 of 6,680 schema
tokens (38%)** on the lending `Payslip` class.

```yaml
extraction:
  agentic:
    restate_schema_in_system_prompt: true   # default; false removes copy 2
```

Left **on by default** deliberately. Restating a schema in prose often improves
adherence, so this is a token/adherence trade rather than a free saving: on a
list-heavy document, an agent that drifts from the schema returns fewer rows. If
you turn it off, judge the result on **completeness**, not on the token count.
The schema-reminder tool is unaffected either way, so the agent can always ask for
the schema again mid-run.

> **What to expect if you turn it off: nothing much, and that is measured.** On the
> benchmark suite it cost no completeness and no accuracy — and it did not save
> anything measurable either. Two reasons, both worth knowing before you tune:
> the copies sit inside the **prompt cache**, so they are billed at roughly a tenth
> of input price; and reclaiming them does **not** make a long document split into
> fewer parts, because the schema text is not counted when the pipeline decides how
> to split a document — the tokens come out of a safety margin that was already
> unused. Earlier versions of this page and of the setting's own description said
> the payoff was context-window headroom; that was wrong, and making it true is
> tracked in
> [#775](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/775).
> Treat this as a setting for measuring the question on your own documents, not as
> a recommended optimisation.

**Visible in the Prompt Preview.** With Extraction mode **Advanced**, the
**Configuration → Prompt Preview → System Prompt** tab ends with the
`Expected Schema:` block this setting controls, and it is included in the token
total — so turning the setting off shows the total drop instead of leaving it
unchanged. One caveat the preview states rather than hides: the real block is
generated from a Pydantic model built from your class (every field gains a
`title`, every optional field an `anyOf` with `{"type": "null"}`), so the browser
can only approximate it from the class schema. Expect the real block to be
**larger** than the preview's — roughly 1.7–2.9x on the shipped lending presets —
which makes the previewed saving a floor, not a ceiling.

> **How agentic uses your configuration:** Agentic extraction automatically
> converts your document class configuration (classes, attributes, descriptions,
> types) into Pydantic models internally. Improving your configuration directly
> improves extraction accuracy. A future enhancement will let you define custom
> Pydantic models with validators, custom types, and business logic — including
> **MCP server integration** for real-time external lookups.

#### Automatic retry handling

Agentic extraction automatically handles throttling and transient errors:

- **Automatic retries**: Up to 7 retry attempts with exponential backoff (matching bedrock client behavior)
- **Adaptive retry mode**: Intelligently adjusts retry timing based on error types
- **Step Functions integration**: If retries are exhausted, `ThrottlingException` is propagated to Step Functions for workflow-level retry handling
- **No configuration needed**: Retry logic is transparent

#### Deterministic table parsing

For documents with large tables (bank statements, brokerage holdings,
transaction logs), enable the deterministic **table parsing tool** so the agent
extracts tabular data by parsing Markdown tables directly from OCR — instead of
having the LLM regenerate every row (slow, costly, and error-prone for hundreds
of rows):

```yaml
extraction:
  agentic:
    enabled: true
    table_parsing:
      enabled: true                  # give the agent a deterministic parse_table tool
      min_confidence_threshold: 95.0 # OCR-confidence target (Textract only)
      min_parse_success_rate: 0.90   # below this, fall back to LLM extraction
      max_empty_line_gap: 3          # tolerate page-break gaps inside a table
      auto_merge_adjacent_tables: true
      lazy_images: true              # skip pre-loading page images when the table
                                     # parse succeeds (big cost saver; see below)
```

> **`lazy_images` (default `true`) — cost optimization for table documents.** When
> a pre-flight table parse succeeds, page images are **not** attached to the
> extraction prompt. The deterministic table tool is text/markdown-driven and never
> reads images, and the agent can still fetch a specific page on demand via the
> `view_image` tool. Because the agentic loop re-sends the prompt on every turn,
> pre-loaded images are re-transmitted repeatedly and dominate cost on multi-page
> documents (and push large documents toward context-window limits). A controlled
> A/B measured no change in list completeness or field accuracy with images off on
> the table path. Set `lazy_images: false` for **image-dependent corpora** where the
> model must see page layout/marks even when a table is present.

> **Requires Markdown tables in the OCR output.** Table parsing only engages when
> OCR emits Markdown pipe-tables — i.e. **Amazon Textract with the `TABLES`
> feature enabled** (keep `LAYOUT` + `TABLES` in `ocr.features`), or another OCR
> backend that produces Markdown tables. With plain-text OCR the agent falls back
> to LLM extraction (use sharding, below, for large tables in that case).

#### Schema validation and model escalation

Agentic extraction can validate its output against the **full class JSON
Schema** — including `format` keywords (`date`, `email`, `uuid`, …) that
type-checking alone misses — and, on failure, **escalate the failing fields to a
stronger model**:

```yaml
extraction:
  agentic:
    enabled: true
    validation:
      enabled: true
      check_formats: true        # enforce JSON-Schema 'format' (ISO-8601 dates, etc.)
      fail_action: escalate      # warn | escalate | reject
      escalation_model: "us.anthropic.claude-opus-4-8"  # stronger tier; blank = retry same model
      min_population_ratio: 0.5  # advisory: warn if <50% of fields populated (silent-loss guard)
```

- **`fail_action: escalate`** re-extracts only the failing top-level fields with `escalation_model` and merges them back (kept only if valid or fewer errors) — far cheaper than human review. `warn` records the outcome and proceeds; `reject` marks the section failed for HITL.
- A per-class override `x-aws-idp-extraction-escalation-model` takes precedence over the global `escalation_model`.
- **`min_population_ratio`** is an advisory completeness heuristic: it flags suspiciously sparse results (e.g. a table that returned zero rows) without failing extraction.
- Outcomes are recorded per section under `metadata.validation` and `metadata.population_check`, and surfaced in the Web UI **Processing Report** tab.

> **`format: date` caveat.** JSON-Schema `format: date` means ISO-8601
> (`YYYY-MM-DD`). The default extraction prompt asks the model for `MM/DD/YYYY`,
> which will fail format validation — set `check_formats: false` or use a
> `pattern` for non-ISO dates.

#### Scalable extraction for large documents (sharding)

For long or dense documents, agentic extraction **shards** a section's pages into
token- and page-budgeted ranges and extracts them concurrently, then merges the
results (list rows concatenated in page order; scalars resolved first-non-null).
This bounds each agent's context — preventing read-timeout / context-overflow
failures a single huge request would hit — and runs shards in parallel.

> **"Shard," not "chunk."** A *shard* is a non-overlapping page range handed to
> one concurrent extraction agent — the term carries the distributed-systems
> sense of *partitioning for parallelism*. It is intentionally distinct from
> *chunking*, which elsewhere in IDP means overlapping text windows (RAG-style)
> or sequential list sub-batches; shards do not overlap and run in parallel.

```yaml
extraction:
  context_buffer: 0.30            # ONE knob: keep 30% of each model window free (auto-sizes everything below)
  agentic:
    enabled: true
    runtime: step_functions       # DEFAULT for agentic: per-shard Lambdas defeat the 900s timeout + resume
    max_concurrent_batches: 10    # >1 enables sharding (upper bound on parallelism & shard count)
    shard_token_budget: 0         # 0 = AUTO-size from the model's context window (minus context_buffer)
    max_pages_per_shard: 5        # page ceiling per shard (timeout-critical; fixed default, not model-derived)
```

- **Model-aware auto-sizing (default).** `shard_token_budget: 0` means the per-shard OCR-token budget is derived from the extraction model's context window minus `context_buffer` — a 1M-context model (`:1m`) shards much larger than a 200K one, automatically. The confidence list-batch size is likewise auto-derived from the confidence model's output cap. You set only `context_buffer`; the derived sizes are logged and shown in the **Processing Report**. Non-zero values pin an explicit override.
- **`max_pages_per_shard` is the timeout lever.** It stays a small fixed default (5) rather than model-derived: the 900s Lambda limit is about *sequential agent turns per shard* (wall-clock), not context tokens, so a roomy token budget must not collapse a large doc back into one giant shard. Fewer pages/shard ⇒ fewer turns ⇒ each shard Lambda finishes well under 900s.
- **Advanced defaults to the resumable runtime.** `runtime: step_functions` is now the agentic default: each shard is its own Lambda iteration in a nested Step Functions **Distributed Map**, so a very large section is not bound by the single-Lambda 15-minute limit and Step Functions **retries only the incomplete shards** (completed shards are reused from S3). `in_process` (asyncio within one Lambda) remains available but is still bound by that one Lambda's 900s.
- **Confidence *and* bounding-box grounding are sharded too.** Each shard runs its confidence assessment **and grounds its own rows' bounding boxes against only its own pages** — so both scale per-shard and run concurrently. The final merge only concatenates already-scored, already-grounded rows (plus a fast top-up for any rows the assessment LLM omitted); it does **not** re-assess or re-ground the whole section. This keeps the merge step fast even on very large tables (previously a single full-section grounding sweep over thousands of rows could approach the merge Lambda's 900s limit).
- **Large-document guidance.** The defaults above are tuned to work out-of-the-box on large documents. Validated at scale on 100- and 200-page single- and multi-table documents (exact row counts, no loss/duplication, no timeouts).

See [Large-Document Guidance](#8-large-document-guidance) for choosing between
Simple + separate confidence and Advanced sharding.

---

## 2. Document Classes, Attributes & Prompts

### Document classes and attributes

Specify document classes and the fields to extract from each using JSON Schema
format:

```yaml
classes:
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: Invoice
    x-aws-idp-document-type: Invoice
    type: object
    description: "A billing document listing items/services, quantities, prices, payment terms, and transaction totals"
    properties:
      InvoiceNumber:
        type: string
        description: "The unique identifier for this invoice, typically labeled as 'Invoice #', 'Invoice Number', or similar"
      InvoiceDate:
        type: string
        description: "The date when the invoice was issued, typically labeled as 'Date', 'Invoice Date', or similar"
      DueDate:
        type: string
        description: "The date by which payment is due, typically labeled as 'Due Date', 'Payment Due', or similar"
```

The solution ships predefined attributes for common document types (Invoices,
Forms, Letters, Bank Statements, etc.). You can add or edit attributes through
the Web UI: **Configuration → Extraction Attributes tab → select class → Add New
Attribute** (name, display name, description, optional formatting hints).

### Per-class extraction model override

By default all classes use `extraction.model`. Override on a per-class basis with
`x-aws-idp-extraction-model` — useful when certain document types benefit from a
different model. The override works with both Simple and Advanced modes; classes
without it continue to use the global model.

```yaml
extraction:
  model: us.amazon.nova-pro-v1:0  # Default for most classes

classes:
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: simple-receipt
    x-aws-idp-document-type: simple-receipt
    type: object
    properties:
      total:
        type: string
        description: "Total amount"

  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: complex-financial-form
    x-aws-idp-document-type: complex-financial-form
    x-aws-idp-extraction-model: us.anthropic.claude-sonnet-4-20250514-v1:0  # Override!
    type: object
    properties:
      account_number:
        type: string
        description: "Account number"
```

When active it is logged at INFO level:
`Using per-class extraction model override for 'complex-financial-form': ...`

### Per-class extraction prompt overrides

By default every class uses the global `extraction.system_prompt` and
`extraction.task_prompt`. Override either (or both) per class with:

- **`x-aws-idp-extraction-system-prompt`** — overrides the system prompt for that class.
- **`x-aws-idp-extraction-task-prompt`** — overrides the task prompt for that class.

This is useful when individual classes were independently optimized (e.g. via
separate AutoTune runs). Because the pipeline classifies first and extracts
per-class, the extraction step always knows which class it is processing. Classes
without these extensions use the global prompts. The override task prompt
supports the same placeholders: `{DOCUMENT_CLASS}`,
`{ATTRIBUTE_NAMES_AND_DESCRIPTIONS}`, `{FEW_SHOT_EXAMPLES}`, `{DOCUMENT_TEXT}`,
`{DOCUMENT_IMAGE}`, `<<CACHEPOINT>>`.

```yaml
extraction:
  model: us.amazon.nova-pro-v1:0
  system_prompt: "You are a document extraction assistant."
  task_prompt: |
    Extract fields from this {DOCUMENT_CLASS} document:
    {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
    Document text: {DOCUMENT_TEXT}

classes:
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: W2
    x-aws-idp-document-type: W2
    x-aws-idp-extraction-system-prompt: "You are an expert W2 tax form data extractor."
    x-aws-idp-extraction-task-prompt: |
      Extract the following attributes from this {DOCUMENT_CLASS} form:
      {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
      Document text: {DOCUMENT_TEXT}
    type: object
    properties:
      employee_name:
        type: string
        description: "Employee name"
```

> **Note:** These overrides also compose with
> `extraction.custom_prompt_lambda_arn`. When a custom prompt Lambda is
> configured, the per-class prompts are resolved first and passed to the Lambda
> as defaults; any prompts the Lambda returns still take final precedence.

### Model and prompt configuration

```yaml
extraction:
  agentic:
    enabled: true            # Advanced mode recommended for production
  model: anthropic.claude-3-5-sonnet-20241022-v2:0
  temperature: 0.0           # Keep low for consistency
  top_p: 0.1
  top_k: 5
  reasoning_effort: low      # reasoning-capable models only; output is always model-max

  system_prompt: |
    You are an expert in extracting structured information from documents.
    Focus on accuracy in identifying key fields based on their descriptions.
    For each field, look for both the field label and the associated value.
    When a field is not present, indicate this explicitly rather than guessing.

  task_prompt: |
    Extract the following fields from this {DOCUMENT_CLASS} document:

    {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}

    <few_shot_examples>
    {FEW_SHOT_EXAMPLES}
    </few_shot_examples>

    <<CACHEPOINT>>

    Here is the document to analyze:
    {DOCUMENT_TEXT}

    Format your response as valid JSON:
    {
      "field_name": "extracted value",
      ...
    }
```

**How prompts apply in each mode.** Both modes use the same `system_prompt` and
`task_prompt` configuration; they are applied differently under the hood:

- **Simple:** `system_prompt` is the Bedrock system message; `task_prompt` is the user message with document content; the model responds with JSON/YAML text that is parsed. No validation or retry.
- **Advanced (agentic):** `system_prompt` is passed via `custom_instruction` and appended to the agentic system prompt; `task_prompt` is sent as the user message (text/images as content blocks); the agent returns a validated Pydantic model with automatic retry and self-correction.

You do not need separate prompts for agentic extraction — the better you define
your classes and attributes, the more accurate agentic extraction becomes.

### Skipping extraction for excluded classes

If a document class is marked with `x-aws-idp-exclude-from-processing: true` (see
[Excluding Static Pages in the Classification docs](classification.md#excluding-static-pages-eg-instructions-legal-boilerplate)),
`ExtractionService.process_document_section` short-circuits for any section
classified as that class: **no** prompt is built and **no** LLM call is made. A
small stub `result.json` is written so downstream stages behave exactly as they
would for a real section:

```json
{
  "status": "skipped_excluded_class",
  "stage": "extraction",
  "section_id": "1",
  "classification": "PassportApplicationInstructions",
  "excluded": true,
  "exclusion_reason": "instructions",
  "page_ids": ["1", "2", "3", "4"],
  "message": "Section 1 classified as 'PassportApplicationInstructions' ..."
}
```

`section.extraction_result_uri` is set to the stub URI. The stub is produced by
`idp_common.section_exclusion.build_skipped_stub_result` and written by
`idp_common.section_exclusion.write_skipped_stub`. Confidence assessment also
short-circuits for excluded sections (no LLM call, no additional stub — the
extraction stub is authoritative). See the demo at
`notebooks/usecase-specific-examples/ds11-passport-application/demo.ipynb`.

---

## 2b. Output Correctness: Coercion, Validation & Multi-Document Sections

Three things happen to an extraction result after the model returns it and before
it is stored. All are configurable under **Configuration → Extraction** in the Web
UI, so any of them can be turned off without a redeploy if it causes trouble.

### Value coercion (`extraction.coercion`)

Repairs type/format mismatches deterministically — **no model call, no cost**:

| Input | Field type | Becomes |
|-------|-----------|---------|
| `"$1,234.00"` | `number` | `1234.0` |
| `"1.234,56"` (European) | `number` | `1234.56` |
| `"12.5%"` | `number` | `12.5` (magnitude preserved, **not** divided by 100) |
| `"03/15/2024"` | `string` + `format: date` | `"2024-03-15"` |
| `"March 15, 1980"` | `string` + `format: date` | `"1980-03-15"` |
| `"Yes"` | `boolean` | `true` |

Every change is recorded in the section's `metadata.coercion`, so nothing is
silently rewritten and you can audit exactly what was changed and why.

**What it refuses to do.** Anything genuinely ambiguous is left untouched and
recorded as a refusal rather than guessed:

- `"01/02/2024"` — January 2nd or February 1st? (`ambiguous_date`)
- `"03/15/24"` — a 2-digit year cannot be assigned a century (1924 or 2024 matters
  for a date of birth)
- `"2024-03-15T09:00:00Z"` into a `date` field — dropping a time component is data loss
- `"1,234.56"` into an `integer` field — rounding would discard data
- Anything across a type family — never string→object, never a scalar wrapped in an array

If your corpus has a known day/month convention, `date_order: MDY` or `DMY`
resolves the all-numeric ambiguous case. It **never** overrides a value that is
already unambiguous (a `15` cannot be a month whatever you set).

> ⚠️ **The refusal covers coercion, not the pipeline.** `ambiguous_date` only
> engages when the model hands back the **raw string**. If the model normalizes
> the date itself, it picks a day/month order silently — and there is **no
> refusal, no ProcessingIssue, and no `metadata.coercion` entry at all**. The
> guess is indistinguishable from a correct reading.
>
> Measured on a single-page invoice containing `Shipment Date: 03/04/1985`
> extracted into a `format: date` field, ground truth `1985-04-03` (D/M/Y):
>
> | extraction model | coercion | value returned | refusal recorded? |
> |---|---|---|---|
> | `us.anthropic.claude-sonnet-4-6` | off | `1985-03-04` | — |
> | `us.anthropic.claude-sonnet-4-6` | **on** | `1985-03-04` | **none** |
> | `us.amazon.nova-lite-v1:0` | off | `1985-03-04` | — |
> | `us.amazon.nova-lite-v1:0` | **on** | `1985-03-04` | **none** |
>
> Both models resolved the ambiguity to M/D/Y on their own and emitted ISO
> directly, so coercion never saw a string and had nothing to refuse. Note that
> `date_order` does not help here either — it also only applies to values
> coercion actually processes.
>
> **What this means for you.** The property that holds is "*coercion* never
> guesses a day/month order", not "the pipeline never does". For date-of-birth,
> effective-date or any field where a transposed day and month is a correctness
> problem rather than a formatting one, do not treat an unflagged date as
> verified.
>
> **Two mitigations, both free.** Instruct the model in the class or field
> description to return dates **verbatim**, which puts the value back on
> coercion's path and makes the refusal authoritative; and/or set `date_order` to
> your corpus convention so that path resolves rather than refuses. Note the
> order matters — `date_order` alone does nothing here, because a
> pre-normalized ISO value never reaches the code `date_order` governs. If your
> corpus mixes D/M/Y and M/D/Y sources, neither mitigation is sufficient and
> numeric dates need review.
>
> **This is a documented limitation, not planned work** — see
> [#717](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/717)
> for the measurement and the reasoning. Detecting it automatically was
> prototyped and rejected: the check can only fire on evidence that the *document*
> is ambiguous, which on a normal US or EU corpus is a large fraction of all
> dates, so it produced warnings faster than anyone could act on them. It cannot
> tell you which reading is right — only that one was chosen.

> **How much does coercion actually change?** Measured on a live stack: modern
> models (Claude Sonnet 4.6, Nova Lite) already return correctly-typed values for
> scalar fields, so coercion often fires **zero** times and changes nothing. It
> fires substantially on **long repetitive list rows** (81 coercions across
> 100-row transaction lists in one benchmark), where model output drifts. Treat it
> as a **safety net for messy output and non-format-tolerant consumers** — Athena
> column typing, rule validation, API clients — rather than as an accuracy
> improver. It did not move evaluation accuracy in either A/B we ran.

### Schema validation (`extraction.validation`)

Validates the result against the **full class JSON Schema** — most importantly the
`format` keywords (`date`, `email`, `uri`, `uuid`) that type validation alone does
not enforce. Runs on both Simple and Advanced extraction.

`fail_action` decides what happens when validation fails:

| `fail_action` | Behaviour | Extra inference? |
|---|---|---|
| `warn` (default) | Records the outcome and raises an `extraction_validation_failed` **warning** on the section; the data is kept | **No — free** |
| `reject` | Same, but the issue is an **error** and the section is marked failed so downstream/HITL can act | **No — free** |
| `escalate` | Re-extracts **only the failing fields** with `escalation_model`, merged back over the fields that already validated | **Yes** |

Validation is **on by default** precisely because the default action is free: it
turns an otherwise-silent schema violation into something visible at no cost —
the issue reaches the document list's **Processing Issues** column and the
**Processing Report** tab, naming the failing fields and the first few concrete
violations, so you do not have to open the section result JSON.
`escalate` is the opt-in that spends money. Only the failing fields are
re-extracted and only those are merged back, so an over-eager escalation cannot
overwrite fields that already validated; if escalation fails, the original
extraction is kept unchanged.

> **Moved in v0.7.** This block was `extraction.agentic.validation`. Stored
> configurations are migrated automatically on read — no action required.

### Forced tool use (`extraction.forced_tool`) — experimental, off by default

Coercion and validation repair or report a bad result *after* the fact. Forced
tool use tries to make one shape of bad result impossible in the first place: the
class schema is sent to the model as a **required tool** whose input *is* your
schema, rather than described in prose in the prompt, and the API constrains the
reply to that shape.

```yaml
extraction:
  forced_tool:
    enabled: false             # experimental; measure before turning it on
    fallback_to_prompt: true   # a prose answer still gets parsed (recommended)
```

Applies to **Simple** extraction only — Advanced (agentic) extraction already
sends a tool schema. Configurable under **Configuration → Extraction → Schema
Enforcement (experimental)**.

**Why it is off by default.** Forcing constrains the *structure* of the reply, not
the *accuracy* of the values in it. A model that would have emitted a stray key
may instead emit a worse value that fits the schema, so this is not a free
accuracy win and it is not yet proven on real corpora. Measure completeness and
field accuracy on your own documents before enabling it.

**When it is skipped automatically.** Not every route can carry a tool
configuration. Models reached through a custom Lambda hook and the GPT-5.x
(Responses API) route fall back to the prompt, and the reason is recorded — so a
before/after comparison can tell "forcing changed nothing" from "forcing never
ran", which are very different results.

**What gets recorded.** Each section's `metadata.forced_tool` holds `requested`,
`honored` (the model can accept a tool configuration and still answer in prose),
`renamed_properties`, and `skipped` with a reason where applicable. `honored` is
the number to look at first: forcing that is not honored has not been tested.

**Visible in the Prompt Preview.** With Extraction mode **Simple** and this
setting on, **Configuration → Prompt Preview** gains a **Tool Schema** tab showing
the exact `toolSpec` — tool name, tool description, and input schema — and its
tokens are added to the previewed total, so the cost of turning enforcement on is
no longer invisible. The tab shows property names **as you authored them** and
says so, because the wire-safe rewriting below is reversed in the stored result;
the token estimate is taken from the compact form actually serialized onto the
request, not from the indentation added for readability.

> **Field names with spaces are handled.** Bedrock restricts top-level tool
> property names to `^[a-zA-Z0-9_.-]{1,64}$`, which several shipped configuration
> presets violate (`"Invoice Number"`). Such names are rewritten to a wire-safe
> form for the request and restored to exactly what you authored in the stored
> result — you never see the sanitized names, and no configuration change is
> needed.

`fallback_to_prompt: false` turns an unhonored force into a **parse failure**
instead of falling back. That loses data by design and exists only to measure how
often forcing is actually honored; leave it on in production.

### Multi-document sections (`instance_count`)

Classification splits sections by document *type*. When a packet concatenates
several records of the **same** type with no separator, there is no type change to
split on, so they land in one section — and extraction, whose class schema
describes one document, may return only the first record.

Each section now reports an **instance count**. In the Sections panel it is shown
only when there is something to say — a badge beside the section's class when the
count exceeds 1, hoverable for detail. A normal single-document section shows just
its class name; so does a section whose count was never determined (older
documents, or extraction that failed before producing a result). The raw value is
on the API as `Section.InstanceCount` either way.

If a class's schema is **already modelled as a packet of records** (one top-level
array, one element per record), name that array so the count can be derived:

```yaml
classes:
  - $id: patient_packet
    type: object
    x-aws-idp-instance-array: records   # each element is one document
    properties:
      records:
        type: array
        items:
          type: object
          properties:
            patient_name: { type: string }
```

This changes nothing about extraction output — it only tells the pipeline which
existing array is the instance axis — so it is safe to add to a working config.

If the model returns a JSON **array** for a single-document schema, every record is
now preserved (the first becomes the section result, the rest are recorded in
`metadata.recovered_instances`) and the section is flagged for review rather than
failed.

#### The silent case, and the warning that catches it

The hard case is the model returning a **single object** for a several-document
section. Only that one record exists in the response, so nothing can recover the
others — and until recently nothing reported it either: the section was `SUCCESS`,
the document `COMPLETED`, `ProcessingIssueCount` was `0`, and the instance count
was `1`. One record out of three, silently.

Extraction now asks the model, in the **same inference**, how many separate
documents of the class the supplied pages contain. When that answer exceeds the
number of records in the result, the section raises
`extraction_multi_instance_suspected` (severity **warning**) naming both numbers,
reports the model's count as the section's instance count, and emits a
`MultiInstanceSectionsSuspected` CloudWatch metric. The badge in the Sections
panel says the records are *missing*, not that they were extracted.

```yaml
extraction:
  multi_instance_detection:
    enabled: true    # OFF by default — turn it on for multi-record corpora
    question: ""     # blank = the shipped wording; supports {DOCUMENT_CLASS}
```

The question is editable, like every other prompt in the system — it is sent as the
description of the auxiliary property. **Two clauses are load-bearing** and should
survive any edit: *"do not count pages, sections or repeated headers"* (without it,
a document carrying an identical banner on each of four pages reads as four
documents) and *"DIAGNOSTIC METADATA, not extracted document data"* (so the model
does not treat the field as something to extract from the page).

- **Detection only.** It never changes the extracted data, never fails a section,
  and never turns on a schema flag for you. Fixing the loss is
  `x-aws-idp-multi-instance` (below) or splitting the section.
- **OFF by default — but it is very good at its job.** Measured on two real
  labeled corpora, 80 paired Test Studio runs (same documents both sides, only the
  toggle differing):

  | on 40 real bank-check images, against committed ground truth | |
  |---|---|
  | multi-check images found | **18 of 18** |
  | false alarms on the 22 single-check images | **0** |
  | precision / recall | **1.000 / 1.000** |
  | count reported *exactly* right | **18 of 18** (2 to 8 checks) |
  | token cost | input **+1.8%**, output **−0.5%** |

  It found every multi-check sheet and counted it correctly. **But be precise about
  what those 18 flags mean** — they are a *configuration* finding on this corpus, not
  averted data loss. The extracted rows were counted afterwards: **0 checks missing
  in either arm**. `BANK_CHECK`'s schema is a single `checks` array, so the class
  already models several checks per sheet and nothing was collapsing. What detection
  correctly spotted is that the class never declares its instance axis, so the
  section reports 1 instance for a sheet holding 6 documents — fixed for free with
  `x-aws-idp-instance-array: checks`, no schema change and no baseline migration.

  The distinction matters, because "probe says 6, section says 1" **cannot by itself
  tell you whether records were lost**: `instance_count` is 1 for any class with no
  declared instance axis, whether the records are missing or sitting inside a
  declared array. When detection fires, check the extracted data before concluding
  anything was dropped.

- **Why it is off anyway.** On a corpus with *no* multi-record documents to find, it
  is pure cost: `RealKIE-FCC-Verified` lost about **1.3 accuracy points** with it on
  (0.7678 → 0.7552; worse on 14 of 40 documents, better on 1, sign test
  **p = 0.001**), spread diffusely over four attributes rather than any single
  failure mode. A default has to be safe for the deployment that gets no benefit,
  so the default is off.

- **So: turn it on when a section can hold several records of one class *and the
  class schema describes only one*.** That combination is what loses records; either
  half alone does not. Leave it off otherwise. It is per configuration profile, so a
  multi-record corpus can have it while the rest of your deployment does not.

- **Two uses, and only one is a setting.** As a **diagnostic**, turn it on once, act
  on what it names, turn it off — that is how the `BANK_CHECK` misconfiguration
  above was found. As a **standing guard**, leave it on where a corpus genuinely
  keeps producing merged same-class sections and you want a warning on each one.

### Multi-instance sections (`x-aws-idp-multi-instance`)

Detection tells you records were lost. This **extracts all of them**.

Set `x-aws-idp-multi-instance: true` on a class and its *effective* schema becomes
a list of that class:

```yaml
classes:
  - $id: Pay-Statement
    type: object
    x-aws-idp-multi-instance: true      # <- the only change
    properties:
      CheckNumber: { type: string }
      NetPay:      { type: string }
```

Extraction then requests, and `inference_result` then holds:

```json
{"instances": [
  {"CheckNumber": "77310468", "NetPay": "4,104.59"},
  {"CheckNumber": "77298351", "NetPay": "4,657.95"},
  {"CheckNumber": "77284207", "NetPay": "16,487.56"}
]}
```

You keep authoring the class as **one record** — the wrapper is synthesized, so
you do not degrade a single-record schema by hand.

In the web UI both modes are one control, under **Configuration → Document
Schema →** the class:

> **Documents per section**
> - ( ) One document — *the normal case; nothing changes*
> - ( ) Several — this class already lists them → **Record array:** `records ▾`
> - ( ) Several — wrap my single-record class → `instances[ ] → <Class> → { … }`

The two modes are alternatives, so they are one question with three answers rather
than two separate toggles. The record-array picker appears only in the middle
branch, and the shape preview and the baseline-migration warning appear only in the
last one.

**Opt-in per class, never automatic.** Auto-detecting the shape would make every
single-document section pay for an extra nesting level and would move the
detection problem one stage later. Nothing changes for a class that does not set
the flag.

Two known gaps, both filed:

- **Discovery does not suggest it.** Discovery sees the pages and authors the
  schema, so it is the best place to notice "this sample holds three
  Pay-Statements" — but it does not, so today you have to already know the
  feature exists. Tracked in GitHub #765 (suggest, with a one-click apply; never
  set it silently, because the shape change invalidates baselines).
- **Re-running Discovery on a class erases the flag** — along with every other
  class-level `x-aws-idp-*` setting, because the merge replaces the class
  wholesale. Tracked in GitHub #764. Until it is fixed, re-check the class's
  settings after any Discovery run that targets a class you have configured by
  hand.

#### Designate or Synthesize?

The two keys are mutually exclusive (config validation rejects both on one class)
and answer opposite questions:

| Your class describes… | Key | Schema change | Downstream impact |
|---|---|---|---|
| **one record** (the normal case) | `x-aws-idp-multi-instance: true` | wrapper synthesized | shape of `inference_result` changes — **migrate baselines** |
| **a packet that already holds an array of records** | `x-aws-idp-instance-array: <property>` | none | none |

If you already hand-authored a `List of DocTypeX` class, stay on
`x-aws-idp-instance-array` — it costs nothing and changes nothing. Migrating to
Synthesize mode is optional; the payoff is correctly-keyed per-instance
confidence, Hungarian record matching in evaluation, and a per-attribute (rather
than one-giant-attribute) evaluation report.

> **An internal list is not a reason to avoid this.** An invoice class with
> `line_items[]` is a *single-instance* document with an internal list.
> `x-aws-idp-multi-instance: true` on it is correct and gives
> `instances[i].line_items[j]` — three invoices in one section. Configuration
> validation only *warns* when a class's top level is nothing but one record
> array, because that is the case that would double-wrap.

#### ⚠ Evaluation baselines must be migrated

This is the one part of the feature that can break a working deployment.
Evaluation compares a prediction against a stored baseline **of the same shape**.
A wrapped prediction against a flat baseline scores every field as
missing-on-one-side, so the class's accuracy collapses to ~0 — measured live: a
correctly-migrated baseline scored **1.000** on the same document where a flat one
scored **0.000**.

Evaluation now logs a warning naming the class and the exact migration command when
it sees the two shapes disagree, in either direction — so it is no longer *silent*.
But the warning is in the evaluation Lambda's log, not on the report, and the score
is still 0: treat it as a safety net, not a substitute for migrating.

Migrate in the same change as the flag:

```bash
# Dry run (default — nothing is written)
python3 scripts/migrate_multi_instance_baselines.py --stack-name MyStack

# Apply, keeping a copy of each original
python3 scripts/migrate_multi_instance_baselines.py --stack-name MyStack \
    --apply --backup-suffix .pre-multi-instance

# Rolling the flag back off again
python3 scripts/migrate_multi_instance_baselines.py --stack-name MyStack \
    --direction unwrap --apply
```

The script is idempotent (safe to re-run after an interruption), touches only
classes that set the flag, and refuses to flatten a multi-record baseline on
rollback because that would discard ground truth you authored.

**It migrates shape, not content.** Wrapping a one-record baseline gives
`instances` of length 1. If the document really contains three records — which is
the reason you turned the flag on — the other two were never in the baseline,
because the old pipeline could not extract them. You have to add them, or
evaluation will score the newly-found records as false positives. The script
lists every document it touched so that work is visible.

#### What else changes

| Area | With the flag on |
|---|---|
| Confidence | keyed `instances[i].Field`; each record scored independently |
| HITL review | alert labels become `instances[i].Field` — correct, but review task labels change |
| Reporting / Athena | **one row per document**, not per section, with a new `record_index` column. Existing column names are unchanged, so existing queries keep working — but `(document_id, section_id)` is no longer unique |
| Analytics agent | told about `record_index` for flagged classes |
| Z3 rules | address a record explicitly: `…inference_result.instances[0].NetPay`. A rule whose path no longer resolves logs what to write instead — a miss otherwise reads as "optional parameter absent" and the rule quietly stops firing |
| Public SDK | `fields` is unchanged (the raw shape), plus a new `instances` list; `confidence` now walks lists and groups |
| BDA mode | not applicable — this is a pipeline-mode (`use_bda: false`) feature |
| Advanced (agentic) extraction | the wrapper applies, but the #753 detection probe does not |

#### What DEGRADES with the flag on — read this before turning it on

The wrapper moves every one of your properties one level down, and three checks
walk only the **top level** of a class schema. None of them is a correctness bug,
but all three quietly stop doing anything for a flagged class:

| Check | What stops applying |
|---|---|
| BLANK vs MISSING field handling (`x-aws-idp-source-page-types`) | Nothing at the top level carries it any more, so the distinction is not applied at all for a flagged class. |
| "a declared list came back empty" (`extraction_incomplete`) | The only top-level array is now `instances`, so an **inner** list of a record coming back empty no longer raises it — and that is the largest silent-data-loss shape this pipeline has. |
| Confidence-prompt property descriptions | The prompt builder descends one level under an array, so a nested group or list *inside* a record loses its sub-field descriptions. |
| Evaluation report granularity | The per-attribute breakdown becomes **one** attribute (`instances`) carrying every field's rows, instead of one per field. Every row is still there and still drillable — only the grouping is coarser. |

Two more things to know:

- **Few-shot examples are not rewritten.** `x-aws-idp-examples` prompts are
  hand-authored text. If yours show a flat record they now contradict the requested
  `{"instances": […]}` shape — and a flat answer is salvaged as exactly **one**
  instance, so the loss looks like success. Re-author them wrapped. Configuration
  validation warns when a flagged class carries examples.
- **The Prompt Preview shows the un-transformed schema**, and says so — for both
  the wrapper and the detection probe. The preview is
  built in the browser from the class schema as stored, so for a flagged class the
  prompt it renders is not the one the pipeline sends; it now carries a warning to
  that effect rather than quietly misleading you. (It also omits the detection
  probe when that is on.) The section's stored metadata is the authoritative record
  of what was sent. Duplicating the transform in TypeScript was considered and
  rejected: two implementations that must stay in sync are a worse liability than
  one documented divergence.

## 3. Confidence Assessment

Confidence assessment produces a per-field confidence score (0.0–1.0) and an
explanatory reason for each extracted attribute, so you can gauge the reliability
of automated extractions and route uncertain fields to human review. In v0.6 it
is configured under `extraction.confidence`.

### Key features

- **Per-attribute scoring**: Individual confidence scores for each field, recursively for nested groups and list items. To keep responses compact, the default prompts ask the model to include a `confidence_reason` **only for lower-confidence leaves (below 0.9)** — a high-confidence value emits just `{"confidence": <score>}`. This cuts assessment output tokens substantially (output tokens dominate assessment cost) without affecting the scores or thresholds. Widen or remove the threshold in the confidence `task_prompt` if you want a reason on every field.
- **Multimodal analysis**: Optionally combines OCR text with document images.
- **Token-optimized**: Uses condensed OCR text confidence data for 80–90% token reduction versus full OCR results.
- **Large-list batching**: Long list fields (hundreds of rows) are automatically assessed in sequential batches (`list_batch_size`) so every row is scored — see below.
- **UI integration**: Results appear in the web interface with color-coded confidence indicators and (with geometry) bounding-box overlays.

### The three confidence modes

`extraction.confidence.mode` controls *where* per-field confidence and bounding
boxes are produced. For **pros/cons and a recommendation**, see
[§0 Choosing a configuration](#0-choosing-a-configuration-start-here); the table
below is the mechanical reference for *where each combination runs*:

| `confidence.mode` | Extraction mode | Where confidence runs | Standalone Assessment step |
|---|---|---|---|
| `separate` (default) | Simple (non-agentic) | the standalone Assessment step | runs |
| `separate` | Advanced (agentic) | a second inference **inside each extraction shard** | bypassed (skip) |
| `integrated` | Simple (non-agentic) | the single extraction inference itself, in one pass | bypassed (skip) |
| `integrated` | Advanced (agentic) | within the extraction agent's turn (see strategy note) | bypassed (skip) |
| `off` | (any) | nowhere (`enabled: false` is equivalent) | bypassed |

```yaml
extraction:
  confidence:
    enabled: true             # false disables confidence entirely (zero LLM cost)
    mode: separate            # off | separate (default) | integrated
    model: us.amazon.nova-lite-v1:0   # default; far cheaper for the confidence pass
    temperature: 0.0
    top_k: 5
    top_p: 0.1
    reasoning_effort: low     # only used if a reasoning-capable model is selected here
    list_batch_size: 25       # rows per assessment batch for large lists
    system_prompt: |
      You are an expert document analyst specializing in assessing the confidence
      and accuracy of document extraction results.
    task_prompt: |
      # ... see prompt placeholders below
```

**Behavior when disabled** (`enabled: false` or `mode: off`): the assessment
Lambda is still invoked (minimal overhead) but returns immediately with logging
"Assessment is disabled via configuration" — no LLM calls, no S3 operations.
Defaults to enabled when the property is missing.

> **Migration note:** The previous `IsAssessmentEnabled` CloudFormation parameter
> has been removed in favor of this configuration-based control.

### Running confidence inside extraction (in-shard / integrated)

Confidence does not have to run as a separate downstream step:

- **`separate` on the agentic path** runs a second inference **inside each extraction shard** (over that shard's pages and extracted values) and collates on merge — per-field, page-ordered for list items, first-shard-wins for scalars — then grounds once in OCR geometry over the whole section. This reuses the *same* `AssessmentService.assess_results` core and `ground_assessment_geometry` the standalone step uses, so the `explainability_info` output is identical; only the execution location differs. A single post-merge assessment would re-introduce exactly the context-window pressure sharding removes, hence per-shard.
- **`integrated` on the simple path** is a true single inference: one Bedrock call returns values **and** confidence together. The prompt asks for a `{"extraction": {...}, "confidence": {...}}` envelope; the service splits that (values → `inference_result`, confidence → `explainability_info` after threshold-enrichment + OCR grounding), so the standalone Assessment step auto-skips (the single-inference cost win). For robustness the split also recognizes a `field_assessment` (or `confidence`) **sibling** key placed next to the extracted fields — a shape some models emit — and lifts it the same way (and strips it from `inference_result` so it never leaks). Only a genuinely flat response with no recognizable confidence falls back to the standalone step. Best for **smaller documents** where the whole doc fits one inference; for large docs prefer agentic or `separate`. Any list rows the single inference leaves unscored are retried (missing rows only, bounded) so large-list coverage still reaches 100%.
- **`integrated` on the agentic path** produces confidence within the agent's turn. Because extraction is delivered through a tool call, a hidden experimental setting `extraction.agentic.integrated_confidence_strategy` controls how confidence is produced. It is **deliberately not exposed in the config UI**; set it via `idp-cli config-upload` on a throwaway config version to A/B the trade-off. Both values yield identical `explainability_info`:
  - **`two_step`** *(default)*: the agent extracts, then calls `provide_field_assessment` in a **follow-up inference** — a dedicated reflection pass over finalized values (often better-calibrated). ~3 inferences: extract → assess → close.
  - **`single_shot`**: the agent emits values **and** per-field confidence in **one combined tool call**, saving the middle inference. ~2 inferences. Multi-step (patched) extractions may still call `provide_field_assessment` once at the end so every row is assessed (unassessed rows padded with neutral `confidence: null`).

  Both reuse the same prompt cache, so the saving from `single_shot` is one fewer inference, not a change in cached-token economics.

**Automatic bypass of the standalone step.** When extraction has already written
`explainability_info` to the section result, the Assessment Lambda's *intelligent
skip* detects it and returns immediately without a second LLM call — no duplicate
cost and **no state-machine change is required**. The document status stays
`EXTRACTING` while in-shard assessment runs.

### Prompt placeholders

The confidence prompts support the following placeholders:

| Placeholder | Description |
|-------------|-------------|
| `{DOCUMENT_CLASS}` | The classified document type |
| `{EXTRACTION_RESULTS}` | JSON string of extraction results to assess |
| `{ATTRIBUTE_NAMES_AND_DESCRIPTIONS}` | Formatted list of attribute names and descriptions |
| `{DOCUMENT_TEXT}` | Full document text (markdown) from OCR |
| `{OCR_TEXT_CONFIDENCE}` | Condensed OCR confidence data (80-90% token reduction) |
| `{DOCUMENT_IMAGE}` | **Optional** — inserts document images at the specified position (see [Image Placement](#7-image-placement-cachepoint-output-formats--few-shot)) |

A representative confidence `task_prompt` requesting scores (and geometry, when
using an LLM geometry mode — see §4):

```yaml
extraction:
  confidence:
    task_prompt: |
      <background>
      You are an expert document analysis assessment system. Evaluate the
      confidence of extraction results for a document of class {DOCUMENT_CLASS}.
      </background>

      <assessment-guidelines>
      For each attribute, provide:
      - A confidence score between 0.0 and 1.0 where:
         - 1.0 = Very high confidence, clear and unambiguous evidence
         - 0.8-0.9 = High confidence, strong evidence with minor uncertainty
         - 0.6-0.7 = Medium confidence, reasonable evidence but some ambiguity
         - 0.4-0.5 = Low confidence, weak or unclear evidence
         - 0.0-0.3 = Very low confidence, little to no supporting evidence
      - A clear explanation of the confidence reasoning
      </assessment-guidelines>

      <<CACHEPOINT>>

      <document-image>
      {DOCUMENT_IMAGE}
      </document-image>

      <ocr-text-confidence-results>
      {OCR_TEXT_CONFIDENCE}
      </ocr-text-confidence-results>

      <<CACHEPOINT>>

      <attributes-definitions>
      {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
      </attributes-definitions>

      <extraction-results>
      {EXTRACTION_RESULTS}
      </extraction-results>

      Provide confidence assessments in JSON format. Include "confidence_reason"
      only when confidence is below 0.9:
      {
        "high_confidence_attr": { "confidence": 0.97 },
        "uncertain_attr": {
          "confidence": 0.72,
          "confidence_reason": "faint text, partial OCR match"
        }
      }
```

### Large-list batching (`list_batch_size`)

For documents with large lists (bank statements with hundreds of transactions,
line-item tables, brokerage holdings), a single confidence inference over the
whole section under-enumerates or omits the list, leaving most rows unassessed.
**Every confidence path batches large lists automatically** — the standalone
Assessment step (Simple + `separate`), the in-shard pass (Advanced + `separate`),
and the inline-confidence retry (`integrated`) all share one implementation:

1. It slices the largest list field into `extraction.confidence.list_batch_size`
   chunks (default **25**).
2. Each chunk is assessed **sequentially**, sharing the scalars/context.
3. The concatenated per-row assessments are **reconciled** so **every** list cell
   receives its own confidence and (in an OCR-backed geometry mode) its own
   bounding box.

So large lists are handled with no extra configuration regardless of the
Simple/Advanced or separate/integrated choice. The one knob is `list_batch_size`
— **lower** it if a model still struggles to enumerate a full chunk; **raise** it
to reduce the number of inference calls.

```yaml
extraction:
  confidence:
    enabled: true
    mode: separate            # separate (default) | integrated | off
    list_batch_size: 25       # rows per assessment batch for large lists
```

> **Automatic recovery when the model truncates (self-healing).** `list_batch_size`
> is a *row* count, but the model's real limit is its **max output tokens**. When
> per-row output is large — most notably with `geometry.mode: llm`/`llm_grounded`
> (a bounding box per cell, ~3× the output) — a batch can overflow a small-cap
> model's ceiling (e.g. Amazon Nova Lite caps at 10,000 output tokens). A
> truncated response is unparseable, which used to silently assign a default
> `0.5` to every field and leave list rows unscored. Advanced mode now heals this
> automatically, cheapest-first:
>
> 1. **Token-aware first-pass sizing.** The first batch is sized to the confidence
>    model's output cap — so Nova Lite + `llm_grounded` starts at ~6–9 rows
>    instead of truncating at 25. This only ever *shrinks* `list_batch_size`; a
>    large-output model keeps your configured size.
> 2. **Recursive splitting.** Any batch that still truncates is halved and
>    re-assessed until it fits.
> 3. **Model escalation.** If rows are *still* unscored after shrinking + retries,
>    the remaining rows are re-assessed on a stronger confidence model (bigger
>    output cap). ON by default; configure with:
>
>    ```yaml
>    extraction:
>      confidence:
>        escalation_enabled: true
>        escalation_model: "us.anthropic.claude-sonnet-4-20250514-v1:0"
>        max_escalation_rounds: 2
>    ```
>
>    Per-class override: `x-aws-idp-confidence-escalation-model`. Set
>    `escalation_model: null` to skip the model step.
>
> This activity is recorded in the section's
> `metadata.assessment_batch_split_stats` (`derived_batch_size`,
> `escalation_model`, `rows_recovered_by_escalation`, `unrecoverable_rows`, …) and
> (for agentic) an `⚠ Assessment Batch Splitting` block in the processing report.
> If rows remain unscored even after escalation, the durable fix is to reduce
> per-row output — e.g. set `geometry.mode: ocr_only` (the default) so boxes come
> from OCR value-matching rather than the model.

> **Surfaced in the UI.** Whatever the self-healing ladder does (or can't do) is
> recorded as a structured **processing issue** on the section — `severity`
> (error / warning / info), `code` (e.g. `assessment_incomplete`,
> `assessment_recovered_with_retries`, `assessment_deadline_reached`), a
> user-facing `message`, and a technical `root_cause`. These are persisted to
> DynamoDB and shown in the Web UI: a **Status** column on the document's Sections
> panel (hover for the message + root cause), a **Processing Issues** count column
> on the document list, and a structured list at the top of the section
> **Processing Report** tab. A document that quietly self-healed — or one where a
> row genuinely couldn't be scored — is therefore visible at a glance.

> **This replaces granular assessment.** The former "granular assessment"
> service (a separate thread-pool fan-out with DynamoDB caching) has been
> **retired and deleted**. Large-list batching is its full replacement: complete
> per-cell confidence and geometry at roughly **−78% Bedrock cost** on a 120-row
> bank statement, with equal accuracy (granular actually produced 0% geometry).
> Any legacy `granular.*` keys still validate but are ignored — no config edit is
> required. See [Granular Assessment Retirement](migration-granular-retirement.md).

> **For very large or complex documents**, prefer **Advanced (agentic)
> extraction** — it shards both extraction and assessment and produced the
> best-calibrated confidence in A/B testing. Simple + separate remains fully
> viable for large lists via the batching above. See
> [Large-Document Guidance](#8-large-document-guidance).

---

## 4. Geometry / Bounding Boxes

Geometry gives each extracted field a **bounding box** locating it in the
document, which the Web UI renders as an overlay linking form fields to their
place on the page. Geometry is an output of extraction, configured under
`extraction.geometry`. The UI-compatible geometry format matches BDA mode:
`geometry` is an array of `{ boundingBox: {top, left, width, height}, page }`
with normalized 0–1 coordinates and 1-based page numbers, supporting nested
group attributes and list items recursively.

### The four geometry modes

`extraction.geometry.mode` controls where field bounding boxes come from:

| `geometry.mode` | Source of boxes | Notes |
|---|---|---|
| **`ocr_only`** *(default)* | Real OCR lines only | Model is **not** asked for boxes; cheapest and most accurate |
| **`llm_grounded`** | LLM estimates, grounded in OCR | Model emits boxes; service replaces them with real OCR boxes where the value matches |
| **`llm`** | LLM estimates as-is | Escape hatch; no grounding |
| **`off`** | None | No geometry produced |

```yaml
extraction:
  geometry:
    mode: ocr_only            # ocr_only (default) | llm_grounded | llm | off
    task_prompt_bbox: |       # only used by llm_grounded / llm modes
      # ... spatial-localization guidelines (see below)
```

> **v0.6 rename.** The LLM-box modes were previously `llm_with_ocr_grounding` /
> `llm_only`, and the legacy `assessment.ground_geometry_in_ocr: false` maps to
> `llm`. Old configs are migrated on read.

### `ocr_only` (default) — OCR grounding via `pageData.json`

In `ocr_only` mode the model is **not** asked for boxes at all. Each field's
geometry is derived by matching the extracted value text against real OCR lines
in the consolidated `pageData.json` artifact (Amazon Textract, or the Mistral OCR
LambdaHook). This is **cheaper** (no bbox tokens in the response) and **more
accurate** (OCR boxes beat LLM-estimated boxes, which models frequently
hallucinate). A field with no OCR match simply has no geometry (geometry is
advisory).

**Repeated values are disambiguated by row order** — when the same value appears
on multiple rows (e.g. a repeated amount in a table), the i-th assessed list item
maps to the i-th occurrence in reading order.

**Format-aware matching.** Extraction often canonicalizes a value to a schema
format that differs from how it appears in the document (a date extracted as
`2022-04-04` but printed `04/04/2022`; an amount `1234.00` printed `$1,234.00`; a
phone `+15551234567` printed `(555) 123-4567`). Matching is bridged three ways:

- **Format variants** — the value is re-rendered in common surface forms and each is matched.
- **Type-aware equality** — value and OCR line are parsed as the same logical date/number/phone and compared (robust to any rendering).
- **Character-level Levenshtein** — last resort for OCR noise (e.g. `Acme` vs `Acrne`).

The field's logical type comes from its JSON-Schema `type`/`format` when present,
else inferred from the value. Format-bridged matches are tagged
`geometry_source: "ocr-normalized"` and Levenshtein near-misses `"ocr-fuzzy"`,
distinct from exact `"ocr"` hits so they stay auditable.

### `llm_grounded` / `llm` — LLM-estimated boxes

In these modes the assessment/confidence prompt asks the model for boxes.
Include spatial-localization guidelines and request `bbox` + `page` per field:

```yaml
extraction:
  geometry:
    mode: llm_grounded
    task_prompt_bbox: |
      <spatial-localization-guidelines>
      For each field, provide bounding box coordinates:
      - bbox: [x1, y1, x2, y2] coordinates in normalized 0-1000 scale
      - page: Page number where the field appears (starting from 1)

      Coordinate system:
      - Use normalized scale 0-1000 for both x and y axes
      - x1, y1 = top-left corner; x2, y2 = bottom-right corner
      - Ensure x2 > x1 and y2 > y1
      - Make bounding boxes tight around the actual text content
      </spatial-localization-guidelines>

      Provide confidence assessments with spatial localization in JSON:
      {
        "attribute_name": {
          "confidence": 0.85,
          "confidence_reason": "Clear text with high OCR confidence",
          "bbox": [100, 200, 300, 250],
          "page": 1
        }
      }
```

**Automatic coordinate conversion.** When the LLM returns bbox data, the service
automatically detects `bbox`/`page`, converts from the 0–1000 normalized scale to
0–1 decimals, transforms `[x1, y1, x2, y2]` into `{top, left, width, height}`,
and processes nested/list attributes recursively. Reversed coordinates are
auto-corrected; invalid or incomplete boxes are dropped (confidence assessment
continues).

```python
# LLM Response Format
{"StatementDate": {"confidence": 0.95, "bbox": [100, 200, 400, 250], "page": 1}}

# Automatically Converted to UI Format
{"StatementDate": {
  "confidence": 0.95,
  "geometry": [{
    "boundingBox": {"top": 0.2, "left": 0.1, "width": 0.3, "height": 0.05},
    "page": 1
  }]
}}
```

- **`llm_grounded`**: the LLM box is grounded in real OCR coordinates where the value matches, falling back to the LLM box otherwise. The LLM box also disambiguates repeated values by **spatial proximity**.
- **`llm`**: the model's boxes are used as-is with no grounding.

### Grounding in real OCR geometry (`pageData.json`)

For `ocr_only` and `llm_grounded`, grounding is a **post-LLM, server-side
enrichment** step (it does *not* change the prompt or its token budget). After
extraction/assessment, the service reads the consolidated per-page
[`pageData.json`](../lib/idp_common_pkg/idp_common/ocr/README.md) directly from
S3 and matches each extracted value to an OCR line.

For each assessed leaf field, the service:

1. Loads `pageData.json` for the section's pages via `Page.ocr_page_data_uri` (older documents without this artifact are skipped — see fallback).
2. Matches the value against the page's OCR `lines[]`, in precision order: **exact** line → **value contained in a line** → **multi-line span** (boxes unioned) → **single-line fragment** → **token-overlap fuzzy** (Jaccard ≥ 0.6).
3. **Disambiguates repeated values spatially** (for `llm_grounded`): all candidates are collected and the one whose box center is nearest the LLM-estimated box is chosen. Without a usable LLM reference box, an ambiguous match keeps the LLM box rather than risk attaching the wrong row's geometry.
4. On a confident match, **replaces** the field's `geometry` with the real OCR box and adds provenance keys.

**Output additions** on grounded fields:

```json
{
  "account_number": {
    "confidence": 0.95,
    "confidence_reason": "Clear text with high OCR confidence",
    "confidence_threshold": 0.9,
    "geometry": [
      { "boundingBox": { "top": 0.052, "left": 0.447, "width": 0.061, "height": 0.011 }, "page": 1 }
    ],
    "geometry_source": "ocr",
    "ocr_confidence": 0.992
  }
}
```

- **`geometry_source`** — `"ocr"` (grounded in a real OCR line), `"ocr-paragraph"` (grounded in a paragraph-level box shared by sibling lines, as the Mistral hook produces), `"ocr-normalized"` / `"ocr-fuzzy"` (format-bridged / Levenshtein matches), or `"llm"` (kept the LLM-estimated box).
- **`ocr_confidence`** — the matched OCR line's confidence (0–1), when available. **Informational only**; the LLM `confidence`/`confidence_reason` are never overwritten, so HITL triggering and threshold alerts are unaffected.

**Safe fallback (backward compatibility).** Grounding degrades to exactly the
prior LLM-only behavior — the worst case is no change:

- No `pageData.json` (older documents, or a missing URI) → keep the LLM box.
- OCR backend provides no geometry (plain Bedrock LLM OCR, Chandra hook, `none` backend; `geometryAvailable: false`) → keep the LLM box.
- Value doesn't confidently match any OCR line, or repeated values can't be disambiguated → keep the LLM box.

Because grounding reads `pageData.json` from S3 (not the prompt), it adds **zero
tokens** to the request.

---

## 5. HITL (Human Review)

Fields whose confidence falls below a threshold can be routed to human review.
HITL is configured under the top-level `hitl` block in v0.6:

```yaml
hitl:
  enabled: true
  confidence_threshold: 0.85   # fields below this are flagged for review
```

Human review integrates confidence scores and geometry so reviewers see exactly
which fields need attention and where they appear on the page. For the full
workflow, the review UI, and Amazon A2I integration, see
[Human Review](human-review.md).

---

## 6. Confidence Thresholds

Confidence thresholds identify extraction results that may require review.
Thresholds can be set globally or per-attribute, and the UI provides immediate
color-coded feedback. The system automatically adds `confidence_threshold` values
to the `explainability_info` based on configuration.

**Global threshold** — system-wide requirement for all attributes:

```json
{
  "explainability_info": [
    {
      "global_confidence_threshold": 0.85,
      "YTDNetPay": { "confidence": 0.92, "confidence_reason": "Clear match found in document" },
      "PayPeriodStartDate": { "confidence": 0.75, "confidence_reason": "Moderate OCR confidence" }
    }
  ]
}
```

**Per-attribute threshold** — override global settings for specific fields:

```json
{
  "explainability_info": [
    {
      "YTDNetPay": { "confidence": 0.92, "confidence_threshold": 0.95, "confidence_reason": "Financial data requires high confidence" },
      "PayPeriodStartDate": { "confidence": 0.75, "confidence_threshold": 0.70, "confidence_reason": "Date fields can accept moderate confidence" }
    }
  ]
}
```

**Mixed** — global default plus per-attribute overrides:

```json
{
  "explainability_info": [
    {
      "global_confidence_threshold": 0.80,
      "CriticalField": { "confidence": 0.85, "confidence_threshold": 0.95, "confidence_reason": "Override: higher threshold for critical data" },
      "StandardField": { "confidence": 0.82, "confidence_reason": "Uses global threshold of 0.80" }
    }
  ]
}
```

### Thresholds inside lists (arrays)

For `type: array` attributes, each **item sub-field** carries its own threshold,
resolved from the item schema. This works whether the item schema is inline
under `items.properties` or referenced with `$ref` into `$defs`:

```json
{
  "w2_copies": {
    "type": "array",
    "items": { "$ref": "#/$defs/W2CopyItem" }
  },
  "$defs": {
    "W2CopyItem": {
      "type": "object",
      "properties": {
        "w2_box_a_employee_ssn": { "type": "string", "x-aws-idp-confidence-threshold": "0.8" },
        "w2_box_1_wages":        { "type": "number", "x-aws-idp-confidence-threshold": "0.9" },
        "w2_form_year":          { "type": "string" }
      }
    }
  }
}
```

Each row of `w2_copies` is scored per column: `w2_box_a_employee_ssn` against
`0.8`, `w2_box_1_wages` against `0.9`, and `w2_form_year` — which declares no
threshold — against `hitl.confidence_threshold`.

Resolution order for any field is:

1. The field's own `x-aws-idp-confidence-threshold` (for list columns, the one
   inside the resolved `items` / `$defs` schema).
2. The `x-aws-idp-confidence-threshold` on the array attribute itself, if set.
3. `hitl.confidence_threshold`.

This applies to both processing modes. In BDA mode, per-field thresholds are
resolved for the blueprint-driven `custom_output` (the section results that match
your class schema). Page-level `standard_output` confidence is generic document
analysis rather than named schema fields, so it uses `hitl.confidence_threshold`.

> **Note:** if `hitl.confidence_threshold` is `0.0`, any field without an explicit
> `x-aws-idp-confidence-threshold` is effectively never flagged. Set a meaningful
> default (e.g. `0.8`) so unannotated fields still get reviewed.

> **Note — nesting depth.** Two resolvers are involved, and they differ below the
> first array level. The path-based resolver (`resolve_threshold_for_path`, used by
> the BDA HITL alert path) walks arbitrary nesting — objects inside array items,
> arrays inside groups, nested arrays. The enrichment function
> (`enrich_assessment_with_thresholds`, used by the pipeline assessment and BDA
> `result.json` paths) resolves only **one** level of array item sub-fields, so a
> threshold declared on a field *below* an array item (e.g.
> `w2_copies[].address.zip`) falls back to `hitl.confidence_threshold` there while
> the HITL alert path honors it. For the common case — flat array items with
> per-field thresholds, which is what `$defs` item schemas normally describe —
> both paths agree exactly. This is pinned by
> `test_resolvers_diverge_on_nesting_below_array_items`, so if you need the deeper
> case, unify the two paths and update that test rather than working around it.

### UI visual feedback

The UI renders confidence with color coding:

- 🟢 **Green**: Confidence meets or exceeds threshold (high confidence)
- 🔴 **Red**: Confidence falls below threshold (requires review)
- ⚫ **Black**: Confidence available but no threshold for comparison

Interface coverage includes the **Visual Editor** tab (split-pane document image
+ form editing, bounding-box overlays, recursive nested display, inline editing
with change tracking), the **JSON Editor** tab, the **Edit history** tab
(audit trail with field-level diffs), **smart filters** (low-confidence,
evaluation mismatches, collapsible tree), and nested-data support:

```
FederalTaxes[0]:
  ├── YTD: 2111.2 [Confidence: 67.6% / Threshold: 85.0% - RED]
  └── Period: 40.6 [Confidence: 75.8% - BLACK]

StateTaxes[0]:
  ├── YTD: 438.36 [Confidence: 84.4% / Threshold: 80.0% - GREEN]
  └── Period: 8.43 [Confidence: 83.2% / Threshold: 80.0% - GREEN]
```

**Threshold best practices.** Set higher thresholds (0.90+) for critical
financial or personal data; use per-attribute thresholds for different data
types; establish reasonable global defaults (0.75–0.85); start conservative and
tune based on accuracy analysis. Route below-threshold extractions to
[Human Review](human-review.md).

---

## 7. Image Placement, CachePoint, Output Formats & Few-Shot

### Image placement with `{DOCUMENT_IMAGE}`

Both extraction and confidence prompts support precise control over where
document images appear, using the `{DOCUMENT_IMAGE}` placeholder.

- **Without the placeholder**: images are automatically appended after the text content.
- **With the placeholder**: images are inserted exactly where `{DOCUMENT_IMAGE}` appears.

```yaml
extraction:
  task_prompt: |
    Extract the following fields from this {DOCUMENT_CLASS} document:

    {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}

    Examine this document image:
    {DOCUMENT_IMAGE}

    Text content:
    {DOCUMENT_TEXT}

    Respond with valid JSON containing the extracted values.
```

Common patterns: **visual-first** (image before instructions), **verification**
(image after text to correct OCR errors), and **mixed content** (image shows
tables/stamps/signatures the text misses). The placeholder works seamlessly with
few-shot examples. For **confidence** prompts, images are only processed when
`{DOCUMENT_IMAGE}` is explicitly present (text-only assessment otherwise) — and
exactly **one** occurrence is required when present. The prompt is the *only*
thing that decides this: `geometry.mode` does not affect whether page images are
attached, because a confidence pass that is never asked for bounding boxes still
needs the image to judge visually-evidenced fields (signature / checkbox / stamp
booleans, handwriting, struck-through values). To make the confidence pass
text-only — e.g. to cut input tokens on a long table — remove `{DOCUMENT_IMAGE}`
from the confidence `task_prompt`.

**Multi-page handling.** Images are processed in page order with no image count
limits (following the Bedrock API removal of image count restrictions); documents
of any length are processed without truncation.

**Benefits:** enhanced accuracy, better table/form handling, improved handwritten
content and visual-only elements (stamps, logos, checkboxes), and OCR-error
verification.

### Image processing configuration

Both extraction and confidence support configurable image dimensions under an
`image:` sub-block. **Empty strings or unspecified dimensions preserve the
original document resolution** for maximum accuracy (this is the default and
recommended for critical extraction):

```yaml
extraction:
  image:
    target_width: ""          # Empty string = no resizing (recommended)
    target_height: ""         # Empty string = no resizing (recommended)
  confidence:
    image:
      target_width: ""
      target_height: ""
```

Configure specific dimensions when optimizing for performance:

```yaml
extraction:
  image:
    target_width: "1200"      # Resize to 1200px wide
    target_height: "1600"     # Resize to 1600px tall
```

**Features**: original-resolution preservation, aspect-ratio-preserving resize,
smart scaling (only downsizes when scale < 1.0), high-quality resampling.

> **Migration from previous versions.** The old behavior defaulted empty strings
> to `951x1268` resizing; the current behavior preserves original resolution. To
> keep the old behavior, set `target_width: "951"` / `target_height: "1268"`
> explicitly.

### Using CachePoint

CachePoint caches partial computations to improve performance and reduce costs.
Enable it by placing `<<CACHEPOINT>>` tags in prompt templates to mark where the
model should cache preceding prompt components:

```yaml
extraction:
  model: us.amazon.nova-pro-v1:0   # Must be a CachePoint-compatible model
  task_prompt: |
    <background>
    You are an expert in business document analysis and information extraction.
    </background>

    <<CACHEPOINT>>  # Cache the instruction portion

    Here is the document to analyze:
    {DOCUMENT_TEXT}
```

**Supported models** include:

- `us.anthropic.claude-haiku-4-5-20251001-v1:0`
- `us.anthropic.claude-sonnet-5` (and the other Claude 4.x/5 Sonnet/Opus IDs)
- `us.amazon.nova-lite-v1:0`
- `us.amazon.nova-pro-v1:0`

`CACHEPOINT_SUPPORTED_MODELS` in
[`idp_common/bedrock/client.py`](../lib/idp_common_pkg/idp_common/bedrock/client.py)
is the authoritative list.

**Optimal placement**: separate **static** content (system instructions,
few-shot examples — cacheable) from **dynamic** content (document text — not
cacheable), placing the tag right before the document text. Cached input tokens
are billed at a reduced `cacheReadInputTokens` rate (roughly 10× cheaper than
standard input tokens).

### JSON and YAML output

The extraction service auto-detects and parses both JSON and YAML LLM responses
via `extract_structured_data_from_text()`. YAML is more token-efficient (~10–30%
fewer tokens; ~25% typical): no quotes around keys, compact nested syntax,
natural multiline support. All existing JSON prompts continue to work unchanged —
no configuration changes required, with intelligent fallback between formats if
parsing fails.

```yaml
# JSON response (traditional)
extraction:
  system_prompt: "You are a document assistant. Respond only with JSON. Never make up data."
  task_prompt: |
    Extract the following fields from this {DOCUMENT_CLASS} document and return a JSON object:
    {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
    Document text: {DOCUMENT_TEXT}
    JSON response:

# YAML response (more token-efficient)
extraction:
  system_prompt: "You are a document assistant. Respond only with YAML. Never make up data."
  task_prompt: |
    Extract the following fields from this {DOCUMENT_CLASS} document and return YAML:
    {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
    Document text: {DOCUMENT_TEXT}
    YAML response:
```

### Few-shot extraction

Improve accuracy by providing examples within each document class configuration
(class-specific — only examples from the same class being processed are
included). Use the `{FEW_SHOT_EXAMPLES}` placeholder in the task prompt:

```yaml
classes:
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: Invoice
    x-aws-idp-document-type: Invoice
    type: object
    description: "A billing document for goods or services"
    properties:
      InvoiceNumber:
        type: string
        description: "The unique identifier for this invoice"
    x-aws-idp-examples:
      - name: "SampleInvoice1"
        x-aws-idp-attributes-prompt: |
          Expected attributes are:
            "InvoiceNumber": "INV-12345"
            "InvoiceDate": "2023-04-15"
            "TotalAmount": "$1,234.56"
        x-aws-idp-image-path: "config_library/unified/examples/invoice-samples/invoice1.jpg"
```

See [Few-Shot Examples](few-shot-examples.md) for detailed guidance.

### Custom prompt generator Lambda

The extraction service supports custom Lambda functions for advanced prompt
generation — injecting business logic (document type-specific processing,
external system integration, conditional processing, compliance requirements,
multi-tenant customization) while leveraging existing IDP infrastructure.

```yaml
extraction:
  model: us.amazon.nova-pro-v1:0
  system_prompt: "Your default system prompt..."
  task_prompt: "Your default task prompt..."
  custom_prompt_lambda_arn: "arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-my-extractor"
```

**Lambda requirements:**

- Function name must start with `GENAIIDP-` (required for IAM permissions).
- Must return valid JSON with `system_prompt` and `task_prompt_content` fields.
- Available in Pipeline mode (Patterns 2 and 3 historically).

**Input payload** provides `config`, `prompt_placeholders` (DOCUMENT_TEXT,
DOCUMENT_CLASS, ATTRIBUTE_NAMES_AND_DESCRIPTIONS, DOCUMENT_IMAGE as S3 URIs),
`default_task_prompt_content`, and `serialized_document`. **Required output:**

```json
{
  "system_prompt": "Your custom system prompt based on document analysis",
  "task_prompt_content": [
    { "text": "Your custom task prompt with business logic applied" },
    { "image_uri": "<preserved_placeholder>" },
    { "cachePoint": true }
  ]
}
```

**Error handling is fail-fast**: Lambda invocation failures, invalid response
format, function errors, and timeouts all cause extraction to fail with detailed
messages. Image bytes are never sent (S3 URIs only) to minimize payload size.
Only `GENAIIDP-*` functions can be invoked (scoped IAM), and all invocations are
logged. For complete examples see `notebooks/examples/demo-lambda/README.md` and
`notebooks/examples/step3_extraction_with_custom_lambda.ipynb`.

---

## 8. Large-Document Guidance

For large documents and big tables:

- **Advanced (agentic) extraction is generally the better fit.** It shards both extraction **and** confidence assessment into token-/page-budgeted ranges, bounding each inference's context and preventing read-timeout / context-overflow failures. It produced the **best-calibrated confidence** in A/B testing. For 100+ page documents prefer `runtime: step_functions` and raise `max_concurrent_batches` (e.g. 10). For large tables specifically, enable **table parsing** (§1) when OCR emits Markdown tables.
- **Simple + `separate` confidence still handles large lists** via [large-list batching](#large-list-batching-list_batch_size) (`list_batch_size`) — full per-cell confidence and geometry with no extra configuration. This is the direct replacement for the retired granular assessment.
- **Rule of thumb:** a document that is large only because it contains a long *list* (e.g. a 300-row bank statement) is well served by Simple + separate + batching. A document with a **very large single section** (dense multi-page narrative or multiple large tables) is better served by **Advanced sharding**, which also splits the extraction work, not just the confidence pass.

### Detecting a truncated list

Extraction can lose rows **silently**: a benchmarked simple-mode run returns
complete lists (recall 1.000) up to ~800 rows, then **0.199 at 1,200 rows and
0.009 at 3,200** — and reports success every time. Two things make this
particularly easy to miss:

- **Scalar accuracy is unaffected.** The document's non-list fields extract
  perfectly whether the table came back complete, partial, or not at all — so a
  quality metric based on field accuracy looks healthy.
- **A truncated run is *cheaper*.** Cost fell from $1.78 to $1.04 when a run
  truncated, so cost monitoring will not flag it either.

So it must be detected structurally. Three signals are now raised as
[processing issues](#surfaced-in-the-ui), on **both** Simple and Advanced modes:

| Code | Severity | Fires when |
|---|---|---|
| `extraction_incomplete` | warning | A schema-declared list came back **empty, null, or absent from the response entirely**. |
| `extraction_list_truncated` | warning | A list returned **fewer rows than its schema `minItems`** — the one unambiguous truncation signal available without ground truth. |
| `extraction_sparse` | info | Fewer than `min_population_ratio` of the schema's leaf fields were populated. |

A fourth issue is raised by [schema validation](#schema-validation-extractionvalidation)
rather than the completeness checks:

| Code | Severity | Fires when |
|---|---|---|
| `extraction_validation_failed` | warning (error under `fail_action: reject`) | The result still violates the class JSON Schema after extraction (and after escalation, if enabled). |

**Add `minItems` to list fields you care about.** It costs nothing at extraction
time and turns an invisible truncation into a visible warning:

```yaml
Transactions:
  type: array
  minItems: 1        # or a realistic floor for your corpus
  items: { … }
```

Without it, only the empty/absent and sparse signals apply — a list that returns
10 of 1,200 rows cannot be distinguished from a document that genuinely has 10.
For corpora where large tables are expected, also prefer **Advanced** mode, which
holds recall 1.000 through 3,200 rows by sharding.

#### Advanced mode: an empty list is retried when the OCR proves there were rows

In Advanced (agentic) mode, one case does **not** need `minItems` to be caught.
The OCR pre-flight scan already counts Markdown pipe-table rows in the section, so
when it finds a substantial table (>30 rows) and **every** declared list field
comes back with no rows, the extraction loop rejects the result and gives the
agent an explicit correction round naming the field and the row count. If some
list is populated, the check stays quiet — the detected tables plausibly belong to
that one, and an empty sibling may be genuinely absent.

**This check needs no configuration.** It runs on every Advanced-mode section, and
in particular it is *not* behind `extraction.validation.enabled` — a guard against
silent data loss that has to be switched on protects nobody who did not already
know to look. (That argument is also why `extraction.validation.enabled` itself now
defaults to **on** as of v0.7; this check stays ungated regardless, so explicitly
turning validation off does not also disable a check that costs nothing.) Its only
effect is one more agent turn; it can never fail a document.

This closes a real failure mode: an agent declined the deterministic table parser
because one column was OCR-corrupted, then returned the whole 100-row list as
`null` — treating *"I cannot map this cleanly"* as *"therefore no rows"*. The
result was schema-valid, scalar accuracy was 1.000, and the section was reported
COMPLETED. The prompt now states the rule explicitly: declining the tool obliges
the agent to extract the table directly, and one unreadable column means that
cell is `null`, not that the row or the list is dropped.

The **Processing Report** also stops contradicting itself here. It previously
printed `✓ Completeness Validation: All schema constraints satisfied` immediately
above the warning that the list was empty, because with no `minItems` no
constraint *was* broken. It now reads `⚠` and says which list returned no rows,
how many rows the OCR found, whether the table tool ran, and that `minItems` would
make it a hard constraint.

---

## 9. Output Format

Confidence and geometry are appended to extraction results in the
`explainability_info` format expected by the UI. The format matches the structure
of `inference_result`, with three attribute shapes:

- **Simple attributes** — a single `{confidence, confidence_reason, confidence_threshold, geometry}` object.
- **Group attributes** — nested objects, one confidence object per sub-attribute.
- **List attributes** — an array with one confidence object per field per item (assess **each item** separately, not as an aggregate).

Complete example (all three shapes, with geometry):

```json
{
  "inference_result": {
    "StatementDate": "2024-01-31",
    "AccountDetails": { "AccountNumber": "1234567890", "RoutingNumber": "021000021" },
    "Transactions": [
      { "Date": "2024-01-15", "Description": "Direct Deposit - Salary", "Amount": "3500.00" },
      { "Date": "2024-01-20", "Description": "ATM Withdrawal", "Amount": "-200.00" }
    ]
  },
  "explainability_info": [
    {
      "StatementDate": {
        "confidence": 0.95,
        "confidence_reason": "Statement date clearly printed in header",
        "confidence_threshold": 0.85,
        "geometry": [{ "boundingBox": {"top": 0.1, "left": 0.1, "width": 0.15, "height": 0.03}, "page": 1 }]
      },
      "AccountDetails": {
        "AccountNumber": {
          "confidence": 0.90,
          "confidence_reason": "Account number clearly visible in account section",
          "confidence_threshold": 0.90,
          "geometry": [{ "boundingBox": {"top": 0.15, "left": 0.2, "width": 0.25, "height": 0.04}, "page": 1 }]
        },
        "RoutingNumber": {
          "confidence": 0.85,
          "confidence_reason": "Routing number printed clearly below account number",
          "confidence_threshold": 0.90,
          "geometry": [{ "boundingBox": {"top": 0.2, "left": 0.2, "width": 0.2, "height": 0.03}, "page": 1 }]
        }
      },
      "Transactions": [
        {
          "Date": {
            "confidence": 0.95, "confidence_reason": "Transaction date clearly printed", "confidence_threshold": 0.80,
            "geometry": [{ "boundingBox": {"top": 0.3, "left": 0.1, "width": 0.12, "height": 0.025}, "page": 1 }]
          },
          "Description": {
            "confidence": 0.88, "confidence_reason": "Description text is clear and complete", "confidence_threshold": 0.75,
            "geometry": [{ "boundingBox": {"top": 0.3, "left": 0.25, "width": 0.35, "height": 0.025}, "page": 1 }]
          },
          "Amount": {
            "confidence": 0.92, "confidence_reason": "Amount properly aligned in currency format", "confidence_threshold": 0.85,
            "geometry": [{ "boundingBox": {"top": 0.3, "left": 0.65, "width": 0.15, "height": 0.025}, "page": 1 }]
          }
        }
      ]
    }
  ],
  "metadata": {
    "assessment_time_seconds": 4.12,
    "assessment_parsing_succeeded": true
  }
}
```

**Response requirements:** match the extraction structure exactly; assess each
list item separately; provide confidence for each sub-attribute of a group; each
assessment includes `confidence` (0.0–1.0) and optionally `confidence_reason`;
the system automatically adds `confidence_threshold` from configuration.

---

## 10. Best Practices

**Extraction**

1. **Prefer Advanced (agentic) for production** and for complex/nested schemas, strict validation, and large documents/tables.
2. **Write clear attribute descriptions** — detail where and how information appears; more specific descriptions yield better extraction (and, in agentic mode, stronger Pydantic validation).
3. **Balance precision vs recall** based on whether false positives or false negatives hurt more for your use case.
4. **Optimize few-shot examples** — diverse, representative examples covering common variations and edge cases.
5. **Use CachePoint strategically** — cache static content, isolate dynamic content, place the tag right before document text.
6. **Optimize image dimensions** — original resolution for forms/tables; smaller for simple text at high volume.
7. **Separate classes for very different layouts** of the same document type.
8. **Test end-to-end** with the full OCR → classification → extraction pipeline.
9. **Choose models by task** — Nova Pro for complex few-shot extraction; Claude Haiku for balanced cost; Claude Sonnet for agentic and highest accuracy.

**Confidence & geometry**

1. **Be specific about high vs low confidence** in prompts; include reasoning examples.
2. **Cost management** — disable confidence for non-critical classes (`enabled: false`); start text-only before adding images; monitor token usage.
3. **Model selection** — Claude Haiku/Sonnet class models with `temperature: 0` for deterministic scoring.
4. **Risk-based thresholds** — 0.90+ for critical data, 0.75–0.85 global defaults, per-attribute overrides where needed.
5. **Keep `ocr_only` geometry** (the default) unless you have a specific reason — it is cheaper and more accurate than LLM boxes.
6. **Tune `list_batch_size`** for large lists — lower if a chunk under-enumerates, raise to cut inference count.
7. **Route below-threshold fields to [Human Review](human-review.md).**

---

## 11. Troubleshooting

**Confidence not running**

- Verify `extraction.confidence.enabled: true` and `mode` is not `off`.
- Confirm the assessment Lambda deployed successfully.
- For agentic + `separate`/`integrated`, remember the standalone step is intentionally bypassed (intelligent skip) once extraction writes `explainability_info` — this is expected, not a failure.

**Template errors**

- Ensure `task_prompt` is defined.
- Validate placeholder syntax; use **exactly one** `{DOCUMENT_IMAGE}` when using images (`"found N occurrences, but exactly 1 is required"`).

**Poor confidence scores**

- Review prompt clarity/specificity; add domain-specific guidance.
- Validate OCR quality and the `{OCR_TEXT_CONFIDENCE}` data.

**High costs**

- Monitor token usage in CloudWatch logs.
- Prefer text-only assessment; trim unnecessary prompt context.
- Use large-list **batching** (Simple + separate) rather than one oversized inference.

**No bounding boxes generated**

- In `ocr_only`/`llm_grounded`, geometry is advisory — a field with no confident OCR match simply has none. Check that OCR provides geometry (`geometryAvailable`) and that `pageData.json` exists for the document.
- In `llm`/`llm_grounded`, confirm the prompt requests `bbox`/`page` data and the model returns valid `[x1, y1, x2, y2]` in 0–1000 scale with 1-based page numbers.
- `geometry.mode: off` produces no geometry by design.

**Invalid coordinates**

- Ensure LLM boxes are in 0–1000 range; reversed coordinates are auto-corrected, malformed ones dropped.

**Confidence threshold / UI issues**

- Verify `confidence_threshold` values are between 0.0 and 1.0 and present in `explainability_info`.
- Confirm color coding (green/red/black) and nested-data display.

**Key metrics to monitor**

- `InputDocumentsForAssessment`, `assessment_time_seconds`, `assessment_parsing_succeeded`, and token-consumption logs in CloudWatch.

---

## Related Documentation

- [Human Review](human-review.md) — HITL workflow and A2I integration
- [Few-Shot Examples](few-shot-examples.md) — building example sets
- [Configuration Guide](configuration.md) — configuration schema details
- [Granular Assessment Retirement](migration-granular-retirement.md) — v0.6 migration notes
- [OpenAI GPT-5.x Models](openai-models.md) — non-agentic-only model support
- [Classification](classification.md) — excluding static pages
- [Web UI](web-ui.md) — UI integration and display
- [Pattern 2 Reference](pattern-2.md) — Pipeline mode details
