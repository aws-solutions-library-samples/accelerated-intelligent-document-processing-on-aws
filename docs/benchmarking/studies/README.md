---
title: "Benchmark Studies"
---

# Benchmark studies — one question each

Everything in this folder was measured with the same harness, on a deployed stack, under
the same honesty rules as the [Configuration Guidance](../config-guidance.md) — but each
file answers **one** question that came up during development, once, and is not
regenerated per release. The systematic outputs live one level up: the
[Benchmarking Guide](../index.md), the [Configuration Guidance](../config-guidance.md)
(refreshed per release) and the [Release Audit Trail](../releases/README.md). Where a
study's conclusion changed a default or a recommendation, the guidance paper cites it;
the study keeps the full evidence, including any wrong turn taken on the way.

| Study | Question | Measured | Verdict | Where it landed |
|---|---|---|---|---|
| [Prompt Caching — Measured Behaviour](./prompt-caching.md) | Are the duplicated schema copies really "cache reads at a tenth of the price"? | 2026-09-10, v0.6.7 | Not below a 1,024-token prefix: two of nine shipped classes never cache; a one-off document pays +24.9% for a cache it never reuses; the end-to-end effect is smaller than the mechanism suggests | guidance §7 (forcing); the `metadata.prompt_cache` observability shipped in 0.6.8 |
| [Should we pad prompts to clear the cache minimum?](./prompt-cache-padding-proposal.md) | Would padding short prompts up to the cache minimum pay for itself? | 2026-09-10, v0.6.7 | ~20% off extraction *input* cost for an affected class, but the sign flips with workload shape and the accuracy risk is unbounded — **proposal: not automatically** | a product decision, still open |
| [Classification Confidence — Does the Score Carry Signal?](./classification-confidence.md) | When classification reports a confidence, is it worth acting on, and does that depend on the classifier? | 2026-09-02, v0.6.7 (DocSplit-Poly-Seq, 20 documents, 298 pages per model) | Calibration separation 0.044 on Nova 2 Lite vs 0.207 on Haiku 4.5: actionable only on the stronger classifier; ~17% of the classification step | guidance §5.3; `docs/classification.md` |
| [Feature study: multi-instance sections](./feature-multi-instance.md) | Does `x-aws-idp-multi-instance` recover the records a merged section loses, and what does detection cost on real corpora? | 2026-09-03 → 09-11, v0.6.7 | The wrapper recovers them (40 of 40 rows); detection is neutral on the OCR benchmark and −1.3 points on RealKIE, so it ships off; two documented wrong conclusions and how each was caught | guidance §7; issue #894 (the v0.6.8 refresh found the wrapper fails assessment on a long list) |
| [Advanced extraction — seven candidate refinements](./advanced-extraction-refinements.md) | Of seven ideas for making agentic extraction cheaper or more complete, which are real? | 2026-09-12 → 09-15, v0.6.8 | None of the seven is a new default; the defects found while measuring them are worth far more — fixing two took the same 12-run arm from $68.82 to $31.60 (−54%) with recall 0.976 → 1.000. Four defects in all: the "images off" arm wrote a config key nothing read, the sharded path never ran the pre-flight parse, neither image knob worked on the single-pass path (which is why 25-page shards failed outright), and `:1m` pricing reports ~1.9× the tokens actually justify. Prompt caching already works across shards (70–97% cache reads); batch inference is ineligible *and* costs more; Textract `[LAYOUT]`-only is 23% cheaper but drops the densest table to 0.098 recall; and with the fixes in place 25-page shards no longer fail but cost 5× the 5-page default ($158.00 vs $31.60), because wide shards defeat the cache | the four fixes in `extraction/service.py`, `agentic_idp.py`, `tools/table_parser.py` and the harness axis validator (PR #898); the `pricing.yaml` `:1m` reporting bug (#899) fixed in v0.6.9 — metering keys no longer carry the suffix and the `:1m` rates now match the base model's, since the premium applies only above 200K input tokens and nothing downstream sees a single request's size; two follow-ups still open — the missing pre-parsed guidance on the sharded path (#900) and in-shard assessment discarding a successful extraction when it overflows the confidence model (#901) |

Adding a study: put it here with a `title:` frontmatter, a one-paragraph "why this page
exists", a `## Reproduce` section naming the suite and the result directory, and one row in
this table. The docs-site sidebar picks the file up automatically.
