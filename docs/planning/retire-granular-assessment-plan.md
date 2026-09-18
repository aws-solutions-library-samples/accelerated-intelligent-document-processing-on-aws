# Plan: Retire granular assessment safely (+ unify extraction/confidence docs)

**Author:** design, 2026-07-03
**Status:** PROPOSAL → implement in sequenced PRs (do NOT reorder).
**Reproducibility:** planning documents written before September 2026 predate the
project's [reproducibility rules](../benchmarking/index.md#reproducibility--honesty-rules)
— treat their numbers as unaudited. **Cost** figures have a second, independent
problem: `_get_unit_cost` in
`lib/idp_common_pkg/idp_common/reporting/save_reporting_data.py` falls back to a
substring match when a model's pricing entry does not name the metered unit, and
`"inputTokens" in "cacheReadInputTokens"` is true — so cache reads, priced at ~10%
of fresh input, are charged the fresh rate (cache writes, at ~115%, are
undercharged by ~20%). The overstatement is bounded: `1/(0.1 + 1.15f)` for a
cache-write share `f` of cache tokens, i.e. ~1.5× at `f=0.5`, rising toward 10× as
`f` approaches zero. It is exactly zero for the 73 of 96 `config_library/pricing.yaml`
entries that price `cacheReadInputTokens` explicitly, because the exact match wins
and the fallback never runs — so the error is model-dependent, not universal. A cost
figure in this document is therefore citable only if the model it was measured on
prices `cacheReadInputTokens`, or if it was measured after that fallback is fixed.
Citing committed benchmark data is **not** sufficient on its own: committed data
measured on an affected model sits on the mispriced path too.
**Owner note:** this is a *major* update. The hard constraint: **no silent quality
regression for customers on non-agentic (simple) + separate confidence + granular
who process long documents / large lists.**

## Problem & evidence (from this session)
- The **large-list batching + reconcile** logic lives ONLY in the agentic/in-shard
  path (`extraction/service.py::_assess_results_batched` + `_reconcile_assessment_to_data`).
- The **standalone Assessment step** (non-agentic) calls
  `AssessmentService.assess_results` = **one single inference over the whole
  document's values** (`assessment/service.py:1110`). No list batching.
- **Granular assessment is currently the ONLY mechanism** that sub-divides large-list
  assessment for the non-agentic path (its README states it exists for "documents
  with hundreds of transactions"). So deleting granular naively regresses exactly
  those customers to the under-enumeration failure ("only 44 of 120 rows scored").
- Granular is also expensive + broken caching: its `<<CACHEPOINT>>` doesn't help
  (per-task content pollutes the cacheable prefix; 20-way ThreadPool causes a
  cacheWrite storm — measured ~1:1 write:read). A/B (RealKIE, 20 docs) showed
  **granular OFF = substantially cheaper per document at equal accuracy/coverage,
  with better calibration**. No percentage is quoted: that A/B's output was never
  committed, so none of its figures can be reproduced or audited.

**Conclusion:** granular should go, but only AFTER the standalone path can batch
large lists on its own. Then deletion is a strict improvement.

## Sequenced PRs (order is load-bearing)

### PR-G1 — Port batched assessment into the standalone path (the safety net)
Wire `AssessmentService.process_document_section` (non-agentic standalone step) to
use the SAME batching + per-column reconcile the agentic path already uses, instead
of the single `assess_results` call:
- Slice large list fields by `extraction.confidence.list_batch_size` (default 25),
  assess each batch with the shared scalars/context, concatenate in order — reuse
  the proven `_assess_results_batched` logic (factor it into a shared location both
  services import; today it's a method on ExtractionService — move to a shared
  helper module, e.g. `assessment/batching.py`, or `assessment/service.py`).
- Apply `_reconcile_assessment_to_data` (index-align, pad/truncate, per-column
  expansion — the fix from #416) so every list cell gets confidence + geometry.
- **Concurrency:** sequential or a SMALL bounded pool (e.g. 2–4), NOT granular's
  20-way ThreadPool. The cost win comes from avoiding the cacheWrite storm; do not
  reintroduce it. (If prompt caching is added later, add ONE cache-warm call before
  any fan-out — see the granular cachepoint post-mortem.)
- Non-agentic + separate + large doc now works WITHOUT granular.
- **Validate:** e2e on a genuinely large-list doc (bank-statement-multipage.pdf,
  ~120 rows) in simple+separate with granular OFF → all rows get confidence +
  geometry, row coverage matches extracted count, and cost per document no higher
  than the SAME document processed with granular ON. Measure both arms in that run
  and record both absolute figures as the baseline: there is no committed baseline
  to validate against, so a relative comparison measured in one run is the only
  cost claim this validation can support.
- Tests: unit test that the standalone path batches a >list_batch_size list and
  reconciles to full per-cell coverage.

### PR-G2 — Default `granular.enabled: false`; migration no-op
- Flip the system-defaults `extraction.confidence.granular.enabled` default → false.
- Update any config_library presets that pin `granular.enabled: true`.
- Migration: `granular.*` in an existing config still VALIDATES (v0.6 migration
  already tolerates it) but is IGNORED at runtime. No customer config edit required.
- On read, log a one-line deprecation note when a config still sets granular.enabled
  true ("granular assessment is retired; large lists are batched by the standalone
  assessment via confidence.list_batch_size").
- **Validate:** the PR-G1 large-doc e2e now runs by DEFAULT (granular off) with full
  coverage.

### PR-G3 — Delete granular_service + slim the assessment path (was PR-C)
- Remove `lib/idp_common_pkg/idp_common/assessment/granular_service.py`,
  `README_GRANULAR.md`, `example_usage.py` granular refs, the GranularAssessmentService
  import/branch in `assessment_function/index.py`, and the `granular` sub-config from
  `GranularAssessmentConfig`/`ConfidenceConfig` models + migration mapping + tests.
- Keep `list_batch_size` (now the single knob) and the batched standalone path.
- **Validate:** unit suite green (granular tests removed cleanly); large-doc e2e
  unchanged; full 5-mode A/B unchanged vs post-PR-G1 numbers.

### PR-G4 — Docs: unify extraction + confidence guide + migration guide
- **Merge** `docs/extraction.md` + `docs/assessment.md` (+ fold in
  `assessment-bounding-boxes.md` as the geometry section) into a single
  **"Extraction & Confidence"** guide reflecting v0.6 (confidence/geometry are
  outputs of extraction, not a separate stage). Present: Simple vs Advanced,
  Confidence off/separate/integrated, Geometry modes, HITL, large-doc guidance.
  Leave stub/redirects at the old paths for existing links.
- **Migration guide** (v0.5→v0.6, or a new v0.6→v0.7 entry): document the granular
  retirement, that `granular.*` is now a no-op, `list_batch_size` is the knob, and
  **mode guidance for large documents**: recommend Advanced (agentic) for large
  docs / big tables (shards extraction AND assessment; best-calibrated per A/B);
  simple+separate still handles large lists via batching but a very large single
  section is better served by Advanced sharding.
- Both doc tiers (docs/*.md + module README) + CHANGELOG.

## Migration handling for customers (the answer to "will this break them?")
- **No config breakage:** granular keys still validate; they're just ignored.
- **No quality regression:** PR-G1 lands the batching safety net BEFORE deletion, so
  non-agentic large-list assessment keeps working (better + cheaper).
- **Guidance, not silent change:** docs recommend Advanced mode for large/complex
  docs, but simple+separate remains viable — we do NOT force a mode change.
- **Deprecation signal:** one-line log when granular.enabled is still set true.

## Guardrails / constraints
- Order: G1 (safety net + large-doc e2e) → G2 (default off) → G3 (delete) → G4 (docs).
  NEVER delete before G1 is validated on a large-list document.
- AWS_PROFILE=default, us-west-2, IDPAgentic; never clobber Config#default; push to
  `github`; PRs → develop; gates (ruff, typecheck-pr, unit, UI build) green per PR;
  CHANGELOG + both doc tiers.
- Reuse the `ab5-*` configs + `scratch/ab5/analyze5.py` for re-validation; the
  large-list e2e target is bank-statement-multipage.pdf (has ~120-row table).

## Open decision for the owner
- Combine the guides now (recommended) vs keep separate: RECOMMEND combine in PR-G4.
- v0.7 config bump when granular is deleted? Suggest yes (granular removal is a
  format-relevant change), stamped via the existing config_format_version mechanism +
  a v06→v07 migration that drops `granular` (idempotent).
