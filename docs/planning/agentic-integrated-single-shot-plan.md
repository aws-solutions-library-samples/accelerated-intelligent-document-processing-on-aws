# Plan: make agentic "integrated" confidence truly single-shot

**Author:** design proposal, 2026-07-02
**Status:** IMPLEMENTED behind a hidden default-off flag (branch
`feature/agentic-integrated-single-shot`). Shipping strategy revised per review:
single_shot is gated behind `extraction.agentic.integrated_confidence_strategy`
(default `two_step` = current behavior, zero user-visible change). A/B on IDPAgentic
picks the eventual default. See §10 (revised shipping plan) at the end.
**Goal:** In advanced/agentic extraction with `confidence.mode: integrated`, have the
extraction agent emit **values + per-field confidence in the SAME inference** — the way
simple/non-agentic integrated already works — instead of the current
extract-then-assess two-tool sequence. This removes one Bedrock inference per agent turn
(and per shard) and honors the stated intent of integrated mode ("one shot, saves a
model call").

---

## 1. Problem statement (verified against develop)

Agentic integrated is implemented as **two sequential tool calls in one agent turn**:

- Extraction is delivered via `extraction_tool` (`agentic_idp.py:357`), whose input schema
  is *exactly the data model* (`extraction: model_class`) — there is **no slot for
  confidence**.
- Confidence is bolted on as a **separate** `provide_field_assessment` tool
  (`agentic_idp.py:542`, registered only when `emit_field_assessment`), and the integrated
  task prompt (`base-extraction.yaml` `task_prompt_extraction_with_confidence`) instructs:
  *"After the extraction is complete and correct, call the provide_field_assessment tool
  exactly once…"*

Because the Converse protocol ends an inference at each `tool_use`, this forces the
minimum 3-inference sequence: (1) `extraction_tool` → (2) `provide_field_assessment` →
(3) `end_turn`. The confidence pass is a *distinct* inference, not single-shot.

Contrast: **simple/non-agentic integrated** (`service.py:2443`) is a single
`bedrock.invoke_model` — values + confidence in one response. That is the behavior we want
the agentic path to match.

**Target:** fold confidence into the `extraction_tool` call so the agent emits both in one
`tool_use`. That collapses the turn to the same 2-inference floor as extraction-only
(propose-via-tool → close), with confidence riding along "for free" on inference 1.

---

## 2. Downstream contract we must preserve (do NOT change)

The current inline assessment already flows through a well-defined path; the single-shot
change must feed the SAME slots so nothing downstream changes:

- Inline assessment is read from `agent.state["field_assessment"]`
  (`agentic_idp.py:1881`) → surfaced in metering as `_integrated_field_assessment`
  (`:1883`).
- Service lifts it into `_merged_assessment` (`service.py:2437-2439`) → `_save_results`
  pops it (`:2752`) → `_attach_explainability` (`:2650`) → `_reconcile_assessment_to_data`
  (`:3132`, index-aligns `assessment[field][i]` with `inference_result[field][i]`, pads/
  truncates rows) → OCR grounding → emitted as `explainability_info[0]` +
  `section.confidence_threshold_alerts`.
- This is byte-identical to what separate mode produces, so HITL / UI / evaluation /
  reporting consume it unchanged.

**Design rule:** the combined tool must populate `agent.state["field_assessment"]` with the
exact same dict shape `provide_field_assessment` records today. Then every line after that
is untouched.

---

## 3. Design — combined extraction+assessment tool (Option A)

Add confidence as a **second parameter on the extraction tool** (a sibling of the data
model), NOT by wrapping every field as `{value, confidence}`. This keeps the extracted
data model clean and validated exactly as today; confidence rides alongside.

### 3.1 New dynamic tool (integrated mode only)
In `create_dynamic_extraction_tool_and_patch_tool` (or a new
`create_extraction_tool_with_confidence`), when integrated is active, build a tool with two
args:

```python
@tool
def extract_with_confidence(
    extraction: model_class,          # unchanged: the validated data model
    field_assessment: dict[str, Any], # NEW: mirrors extraction structure, per-field
                                      # {"confidence": 0-1, "confidence_reason": "..."}
                                      # lists -> one entry per row, in row order
    agent: Agent,
) -> str:
    # 1. validate + record extraction EXACTLY as extraction_tool does today
    agent.state.set("current_extraction", validated_extraction_dict)
    # 2. record assessment EXACTLY as provide_field_assessment does today
    agent.state.set("field_assessment", field_assessment)
    return "..."
```

- The extraction validation/normalization logic is identical to today's `extraction_tool`
  (reuse it — factor the body into a shared helper so both the plain and the
  with-confidence tool call it).
- `field_assessment` is a free `dict` param (same as `provide_field_assessment`'s
  `assessment` arg today), so no rigid schema explosion; the prompt describes the shape.

### 3.2 Tool registration (`agentic_idp.py` ~1685-1710)
- When `emit_field_assessment` is True: register the **combined** tool INSTEAD of the plain
  `extraction_tool`, and do **NOT** register `provide_field_assessment`.
- When False (separate/off): unchanged — plain `extraction_tool`, no assessment tool.
- Keep the `apply_json_patches` / buffer tools as-is (see §4 for the batched interaction).

### 3.3 Prompt change (`base-extraction.yaml` `task_prompt_extraction_with_confidence`)
Rewrite the `<confidence-assessment>` block to instruct a **single combined call**:
*"Call `extract_with_confidence` ONCE with BOTH the extracted values AND a
`field_assessment` mirroring the structure — one confidence entry per field / per list
row, in the same order. Do not make a second call for assessment."*
(3 editable-prompt files ship this; migration not needed — it's a default-prompt text
change, users on the old prompt still work via the fallback in §4.)

### 3.4 Result surfacing (`agentic_idp.py` ~1880)
Unchanged: read `agent.state["field_assessment"]`, put in
`metering["_integrated_field_assessment"]`. The combined tool writes that state key, so this
just works.

---

## 4. The batched-extraction edge (must handle explicitly)

The agent uses `apply_json_patches` / buffer tools to extract large docs in chunks
(SYSTEM_PROMPT: "batched extraction … >50 fields"). Two realities:

1. **Sharded path (the norm for large docs):** each shard is small and extracts in one
   `extract_with_confidence` call — single-shot works per shard, and shard assessments
   already collate downstream. This is the main win.
2. **Single-agent batched (patches after the first extract):** if the agent adds rows via
   patch *after* the combined call, those later rows won't be in `field_assessment`.
   - This is not a correctness break: `_reconcile_assessment_to_data` already pads missing
     rows with neutral `confidence: null` and truncates extras. So late-patched rows get a
     null-confidence placeholder (same as an under-counting assessment today).
   - **Fallback to preserve full coverage:** keep `provide_field_assessment` available as an
     OPTIONAL tool the agent MAY call at the end IF it did a multi-step/patched extraction,
     to refresh confidence over the final data. Prompt: "If you extracted in multiple steps
     via patches, call provide_field_assessment once at the end to (re)assess all rows."
     - Net: small docs → one combined call (2 inferences, single-shot). Large/patched docs
       → combined call + optional final assessment (same as today's behavior, no
       regression). We never do WORSE than today; we do better on the common single-call
       case.

Decision to confirm with reviewer: ship the fallback tool (safe, no regression) vs. drop it
(simpler, relies on reconcile-padding for patched rows). Recommend **keep the fallback**.

---

## 5. Inference-count outcome

| Case (agentic, integrated, ocr_only, clean) | Today | After |
|---|---|---|
| Small doc, single extraction call | 3 (extract → assess → close) | **2 (extract+confidence → close)** |
| Large doc, sharded (per shard) | 3 / shard | **2 / shard** |
| Single-agent, patched/batched | 3+ | 2 + optional final-assess (≈ today) |

Caching is unchanged and still applies: inference 1 writes the system+tools+document
prefix to cache; the closing inference 2 reads it. The saving here is eliminating the whole
middle inference, not a caching change.

Note: getting to **1** inference is out of scope — it would require abandoning tool-based
extraction for `structured_output` (final-text) extraction, a much larger rearchitecture of
the agentic path. 2 is the floor for any tool-based agent (tool call + close).

---

## 6. Implementation steps

1. **Factor** the `extraction_tool` body (validation/normalization/state-set) into a shared
   `_record_extraction(agent, extraction)` helper.
2. **Add** `create_extraction_tool_with_confidence(model_class)` returning a tool with the
   `extraction` + `field_assessment` params; both call `_record_extraction` and set
   `field_assessment` state.
3. **Wire** registration in the agent builder: integrated → combined tool (no
   `provide_field_assessment` as a required step); optionally register
   `provide_field_assessment` as the end-of-batch fallback (§4).
4. **Rewrite** the `<confidence-assessment>` block of
   `task_prompt_extraction_with_confidence` (single combined call; fallback note). Update
   all 3 system_defaults copies.
5. **No downstream changes** — verify `_integrated_field_assessment` → `_merged_assessment`
   → reconcile → ground → explainability_info still runs (it reads the same state key).
6. **Prompt-assembly**: `select_extraction_task_prompt` already returns the integrated
   template for `mode==integrated`; no change (ocr_only appends no bbox block — good).

---

## 7. Testing

- **Unit:** combined tool records both `current_extraction` and `field_assessment` from one
  call; extraction validation identical to plain tool; reconcile still index-aligns; the
  `provide_field_assessment` fallback still works when called.
- **Inference-count assertion:** mock/agent-trace test that a clean small-doc integrated run
  makes 2 model calls, not 3 (guard against regression to the two-tool sequence).
- **E2E on IDPAgentic** (`AWS_PROFILE=default`, us-west-2; throwaway `Config#*` version):
  bank-statement-multipage.pdf, advanced + integrated + ocr_only →
  - every data row has confidence (no null-padding gaps on the single-call path),
  - `explainability_info` shape byte-identical to current integrated output,
  - CloudWatch/log check: extraction turns show 2 inferences where they showed 3.
  - Compare a sharded large-table run: row coverage unchanged vs. today.
- **Regression:** separate mode and off mode untouched (combined tool only built when
  integrated).

## 8. Risks / call-outs

- **Model compliance:** some models may still split into two calls despite the prompt. The
  fallback tool (§4) makes that a graceful no-worse-than-today case, not a failure.
- **Prompt is user-editable:** users who customized `task_prompt_extraction_with_confidence`
  keep the old two-call behavior until they adopt the new default text — acceptable, no
  migration required (behavior degrades to current, not broken).
- **`field_assessment` as a free dict** means no schema-enforced shape; same trade-off the
  current `provide_field_assessment` already makes. Reconcile + validation downstream absorb
  malformed shapes as they do today.
- Scope: agentic path only. Simple/non-agentic integrated already single-shot — untouched.

## 9. Constraints
`AWS_PROFILE=default` (us-west-2); never clobber `Config#default`; push to
`github` only; PR → `develop`; gates (ruff, typecheck-pr, unit, UI build) green; CHANGELOG +
both doc tiers (extraction README + docs/assessment.md) updated.

---

## 10. Revised shipping plan (hidden flag + A/B gate) — as implemented

Instead of replacing the two-step path, single_shot ships **behind a hidden,
default-off knob** so the user-facing `integrated` option never changes or
multiplies, and we can measure calibration vs. cost before choosing a default.

- **Config:** `AgenticConfig.integrated_confidence_strategy: two_step | single_shot`
  (default `two_step`). Validated (unknown → error; blank/None → two_step). **NOT
  added to `template.yaml`** UI schema → invisible to normal users; set via
  `idp-cli config-upload` on a throwaway `Config#*` version for testing.
- **Runtime:** `structured_output_async` builds the combined
  `extraction_with_confidence_tool` and registers it as the primary extraction tool
  only when `integrated AND strategy == single_shot`; otherwise the plain
  `extraction_tool` + `provide_field_assessment` (two_step). Both write
  `agent.state["field_assessment"]` → identical downstream path. `provide_field_assessment`
  stays registered in single_shot as the end-of-batch fallback (§4).
- **Prompt:** the shared `task_prompt_extraction_with_confidence` describes intent
  ("if a combined tool is available, pass values + field_assessment together;
  otherwise call provide_field_assessment after extraction; for multi-step
  extractions call it once at the end"). No migration — default-prompt text change.
- **Tests:** `tests/unit/extraction/test_integrated_confidence_strategy.py` — config
  default/validation, combined tool records both extraction+confidence in one call,
  plain tool records only extraction, and both strategies produce the same state
  shape. (Inference-count assertion deferred to the live A/B; it needs a real agent
  loop, not a unit mock.)
- **Docs:** extraction README `#integrated-confidence-strategy` + docs/assessment.md
  strategy note. Hidden-but-documented.

### A/B GATE (before flipping the default) — the go/no-go
Run on IDPAgentic, same doc (bank-statement-multipage.pdf), advanced + integrated +
ocr_only, two throwaway config versions (`two_step` vs `single_shot`). Compare:
1. **Confidence calibration** — do genuinely low-quality/ambiguous fields still get
   low scores? (The core risk: single_shot self-rates inline, without the dedicated
   reflection pass.) This is the deciding metric.
2. **Row coverage** on the large table (no null-padding gaps on the single-call path).
3. **Inference count / latency / tokens** (expect ~1 fewer inference/shard).
- **If calibration holds** → flip default to `single_shot`, keep `two_step` as escape
  hatch. **If it degrades** → keep `two_step` default; single_shot stays opt-in or is
  dropped. Either way the UI `integrated` option is unchanged.

### Status of steps
- [x] Config field + validator (default two_step).
- [x] Combined tool + shared `_record_extraction` helper; strategy-based registration.
- [x] Prompt updated; fallback wired.
- [x] Unit tests (both strategies) green; existing factory-unpack test fixed (5-tuple).
- [x] Docs (README + assessment.md) + this plan.
- [ ] A/B on IDPAgentic → data-driven default decision (post-merge, supervised).
