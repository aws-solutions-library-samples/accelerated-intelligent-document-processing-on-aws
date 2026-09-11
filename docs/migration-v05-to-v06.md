---
title: "v0.5 → v0.6 Migration (Assessment merged into Extraction)"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# v0.5 → v0.6 Migration (Assessment merged into Extraction)

This note documents the v0.6 configuration change that **retired the standalone
top-level `assessment:` block** and folded its settings into
`extraction.confidence`, `extraction.geometry`, and a top-level `hitl` block. The
short version: **no customer action is required** — pre-v0.6 configurations are
migrated **automatically** on read/update, and the migration is idempotent.

## What changed

In v0.5.x, per-field confidence was configured as if it were a separate
processing stage under a top-level `assessment:` block. v0.6 reframes confidence
as an **optional output of extraction**, so every knob that used to live under
`assessment` now has a new home:

- **Confidence** → `extraction.confidence.*`
- **Geometry (bounding boxes)** → `extraction.geometry.*`
- **Human review (HITL)** → top-level `hitl.*`

The single on/off/mode control is now `extraction.confidence.mode`, which takes
one of three values:

| `confidence.mode` | Meaning |
|---|---|
| `off` | No confidence scoring at all — no extra model pass, no `explainability_info`. |
| `separate` (default) | Confidence scored in a distinct inference (a per-shard second pass for agentic extraction, or the standalone Assessment step for simple extraction). |
| `integrated` | The extraction inference emits each value's confidence in one pass, saving a model call (the standalone Assessment step is bypassed). |

`extraction.confidence.enabled` still exists but is **derived** from `mode`
(`enabled = mode != 'off'`); do not set it directly — use `mode`.

## "Assessment disabled" → `confidence.mode: off`

If your v0.5.x configuration had **Assessment turned off**, it was stored as:

```yaml
# v0.5.x
assessment:
  enabled: false
```

On upgrade this is migrated to:

```yaml
# v0.6
extraction:
  confidence:
    mode: 'off'
```

A falsy `assessment.enabled` (`false`, or the strings `"false"`, `"0"`, `"no"`,
`"off"`) is treated as authoritative and forces `confidence.mode: off`, so a
disabled config stays disabled after the upgrade. This holds even for a sparse
stored delta (`assessment.enabled: false` alone), because the migration runs on
your custom delta **before** it is merged onto the v0.6 defaults (whose default
`mode` is `separate`).

## Full setting mapping (v0.5.x → v0.6)

| v0.5.x (old) | v0.6 (new) | Notes |
|---|---|---|
| `assessment.enabled: false` | `extraction.confidence.mode: off` | Disable → off. |
| `extraction.assessment_integration` | `extraction.confidence.mode` | `separate` / `integrated` folded into the single `mode` knob. |
| `assessment.model` | `extraction.confidence.model` | Passthrough. `LambdaHook` supported. |
| `assessment.model_lambda_hook_arn` | `extraction.confidence.model_lambda_hook_arn` | Passthrough. |
| `assessment.system_prompt` | `extraction.confidence.system_prompt` | Passthrough. |
| `assessment.task_prompt` | `extraction.confidence.task_prompt` | Confidence-only task prompt. |
| `assessment.temperature` / `top_p` / `top_k` / `reasoning_effort` | `extraction.confidence.*` | Passthrough (same names). |
| `assessment.image.*` | `extraction.confidence.image.*` | Passthrough. |
| `assessment.inshard_list_batch_size` | `extraction.confidence.list_batch_size` | Renamed. Default `25`. |
| `assessment.geometry_mode: llm_with_ocr_grounding` | `extraction.geometry.mode: llm_grounded` | Value renamed. |
| `assessment.geometry_mode: llm_only` | `extraction.geometry.mode: llm` | Value renamed. |
| `assessment.geometry_mode: ocr_only` | `extraction.geometry.mode: ocr_only` | Unchanged. |
| `assessment.ground_geometry_in_ocr: false` | `extraction.geometry.mode: llm` | Legacy flag → use LLM boxes as-is. |
| `assessment.hitl_enabled` | `hitl.enabled` | Moved to top-level. |
| `assessment.default_confidence_threshold` | `hitl.confidence_threshold` | Moved to top-level. |
| `assessment.granular.*` | **Dropped** | Granular assessment retired → `list_batch_size` (see below). |
| `assessment.max_tokens` | **Dropped** | v0.6 requests the model's maximum output (from v0.6.8, a confidence call on Nova Lite/Micro requests a row-sized budget instead). |
| any other `assessment.*` key | **Dropped** | The entire `assessment` block is retired; leftover keys are ignored on read. |

When both an explicit v0.6 key and a migrated legacy value are present (a hybrid
config), the **explicit v0.6 key wins** — so a re-seeded default never clobbers a
value you set in the new shape.

## The migration is automatic and idempotent

You do **not** need to hand-edit stored configurations. The v0.5 → v0.6
transform runs at every configuration choke point:

- when a configuration is **read** and validated (`IDPConfig` validation),
- when a stored custom delta is **merged** onto the current defaults,
- when a configuration is **written/updated** (normal update, Save As Version,
  Save As Default).

It short-circuits once a config is stamped `config_format_version: "0.6"` (unless
legacy `assessment.*` keys are still present), so re-applying it is a no-op. In
practice this means a v0.5.x stack that you **update in place** to v0.6 has its
existing configuration migrated on first read — no manual step, and a disabled
Assessment stays disabled.

> **Tip:** Export your current configuration before upgrading (View/Edit Config →
> Export) as a safety backup.

## Related migrations and documentation

- [Granular Assessment Retirement](migration-granular-retirement.md) — the
  companion change that replaced `granular.*` with `list_batch_size`.
- [Extraction & Confidence](extraction-and-confidence.md) — the consolidated
  extraction, confidence, and geometry guide for v0.6.
- [Confidence Assessment](assessment.md) — the three confidence modes, model, and
  prompts in the current shape.
- [Human Review](human-review.md) — the HITL workflow (`hitl.*`).
- [v0.4 → v0.5 Migration](migration-v04-to-v05.md) — the earlier unified-pattern
  migration.
