---
title: "Schema Restatement — What Turning It Off Actually Costs"
---

<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->

# Dropping the duplicated schema copy — 50 runs, and the cost goes the other way

> **Why this page exists.** Advanced (agentic) extraction sends the class schema
> three times per request, and `extraction.agentic.restate_schema_in_system_prompt:
> false` removes the middle copy
> ([#710](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/710)).
> The obvious question is whether that is a free token saving. The answer has two
> halves that point in opposite directions: the **input-token** saving is real and
> measurable on a list-heavy class, and the **dollar** cost of the change is
> positive — not negative — because output tokens rise by more than the input
> tokens fall. Both halves are below, with their significance.

**Method.** The `schema_restatement` axis of the benchmark matrix, two arms
differing in exactly that one config key, run on one deployed stack with identical
code. Extraction model `us.anthropic.claude-sonnet-4-6`, Advanced (agentic) mode,
Textract `TABLES` + `LAYOUT`, `separate` confidence on Nova Lite. Stack
`idp-int1b-0920`, `us-west-2`, code version `v0.6.10.dev1`, 2026-09-20. Costs are
computed from `config_library/pricing.yaml` (rates retrieved 2026-09-17).

**Corpus and repeats.** 50 runs, 25 per arm, all from the synthetic bank-statement
corpus with exact ground truth:

| Document | Rows | Cells scored per run | Repeats per arm |
|---|---:|---:|---:|
| `valuenoise_100` | 100 | 200 | 5 |
| `longdesc_100` | 100 | 200 | 5 |
| `manylists_400` | 400 | 800 | 15 |

Analysis is **per document**, never pooled across them: the between-document spread
in tokens and cost is several times the arm effect, so a pooled number would be
dominated by which documents happened to be in the grid.

---

## 1. Quality: nothing was lost, and the ceiling is why that is a weak statement

All 50 runs reached `COMPLETED`; there were no failures in either arm. In **every
one** of the 50 runs, `completeness_recall`, `cell_accuracy` and `scalar_accuracy`
were exactly **1.000**. `cells_compared` was constant per document (200 / 200 / 800)
and identical between arms, so the two arms scored the same quantity of data — an
accuracy of 1.000 over a shrunken denominator would not have been visible otherwise.
Every run produced exactly one section, so neither arm split a document differently.

**Both arms sit on the ceiling, which bounds what this design can conclude.** It can
detect a degradation and it did not find one; it cannot demonstrate an improvement,
because there is no headroom above 1.000. Two separate limits are worth stating
rather than conflating:

- **Instrument resolution.** One wrong cell moves `cell_accuracy` to 0.995 on a
  200-cell document and to 0.99875 on `manylists_400`. So a single bad cell anywhere
  in the grid would have been visible.
- **Statistical bound.** With 25 clean runs per arm, the 95% upper bound on the rate
  of *runs* that would exhibit any defect is about **11%**. Treating the 14,000 cells
  per arm as independent gives a far tighter per-cell bound (roughly 1 in 4,700), but
  they are **not** independent — one bad agent turn loses many cells at once — so the
  run-level figure is the honest one. A degradation that shows up in fewer than
  roughly one run in ten is not excluded by this grid.

---

## 2. Input tokens: a real saving on the list-heavy documents, and a reversal on the third

"Input-side extraction tokens" below means the Extraction phase's
`inputTokens + cacheReadInputTokens + cacheWriteInputTokens` — every token billed on
the input side, however it was billed.

| Document | n/arm | restate **on** | restate **off** | Δ | % | Mann-Whitney *p* |
|---|---:|---:|---:|---:|---:|---:|
| `manylists_400` | 15 | 175,912 | 166,731 | −9,181 | **−5.2%** | 1.3 × 10⁻⁸ |
| `longdesc_100` | 5 | 80,810 | 75,388 | −5,422 | **−6.7%** | 0.008 |
| `valuenoise_100` | 5 | 103,429 | 113,294 | +9,865 | **+9.5%** | 0.22 (not significant) |

On the first two documents the arm distributions are **completely separated** — every
`off` run used fewer input-side tokens than every `on` run — which is why the exact
*p* is so small on `manylists_400` and why `longdesc_100` reaches 0.008, the smallest
two-sided value the exact test can produce at 5 versus 5.

⚠️ **On `valuenoise_100` the point estimate goes the other way and is not
significant.** The reason is visible in the raw values: within a single arm on that
document the input-side total ranged from 91,969 to 111,105 — a 20% swing at fixed
configuration — because the agentic loop took a variable number of turns and each
turn re-reads the cached prefix. Input-side total is therefore
`prefix size × turn count`, and on a document where turn count is unstable the arm
effect is swamped. **Read the saving as "5–7% on a document whose agent loop is
stable", not as a property of the knob.**

### The saving is entirely in the cached portion of the prefix

This is what makes the token saving cheap in dollars, and it is measured directly
rather than assumed:

- On `longdesc_100`, uncached `inputTokens` was **18,896 in all five `off` runs and
  in four of the five `on` runs** (the fifth: 19,085), while the input-side *total*
  differed by exactly 5,384 tokens in four of the five pairs. The whole difference
  sat in `cacheReadInputTokens` / `cacheWriteInputTokens`.
- On `manylists_400`, uncached `inputTokens` differed by only **−53 tokens**
  (−0.12%, not significant) while cache reads differed by **−8,163** (−6.3%).

So the restated schema lives inside the cache prefix, and removing it removes cache
reads — billed at a tenth of input price on this model.

---

## 3. Output tokens rise, and the structure is bimodal rather than "chattier"

| Document | n/arm | restate **on** | restate **off** | Δ | % | Mann-Whitney *p* |
|---|---:|---:|---:|---:|---:|---:|
| `manylists_400` | 15 | 13,501 | 15,849 | +2,347 | **+17.4%** | 1.2 × 10⁻⁵ |
| `valuenoise_100` | 5 | 7,602 | 8,677 | +1,075 | **+14.1%** | 0.016 |
| `longdesc_100` | 5 | 5,608 | 5,673 | +66 | +1.2% | 0.095 (not significant) |

The increase replicates on two documents of the three and is significant on both;
the third moves in the same direction by an amount too small to distinguish from
noise. That is stronger evidence than a single-document result, and it is still
**one model** — nothing here says anything about how another extraction model
responds.

**What the mean hides.** On `manylists_400` the `on` arm produced 13,406–13,590
output tokens in all 15 runs. The `off` arm produced two distinct modes: ten runs in
13,520–13,776, and **five runs in 20,201–20,368** — roughly 50% more output, a mode
the `on` arm never entered in 15 runs (Fisher exact on landing in the high band,
5/15 versus 0/15: *p* = 0.042).

So the +17.4% mean is not a uniformly more verbose agent. It is a small consistent
shift of 100–250 tokens in the low band, plus a one-in-three chance of a run that
emits half again as much. **Why that happens is not measured.** A plausible reading
is that an agent without the prose schema in front of it re-emits or re-works part of
the table more often; that is inference from the shape of the distribution, not an
observation of the agent's turns.

---

## 4. Cost: the point estimate is positive on all three documents, and none of it is significant

| Document | n/arm | Extraction $ on | off | % | *p* (MWU / Welch) | Total $ on | off | % |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `manylists_400` | 15 | 0.4252 | 0.4571 | **+7.5%** | 0.56 / 0.13 | 0.5911 | 0.6222 | **+5.3%** |
| `valuenoise_100` | 5 | 0.2669 | 0.2888 | +8.2% | 0.31 / 0.19 | 0.3198 | 0.3410 | +6.6% |
| `longdesc_100` | 5 | 0.1900 | 0.1949 | +2.6% | 0.42 / 0.68 | 0.2598 | 0.2644 | +1.8% |

Every point estimate says turning the restatement **off** is more expensive. **None
of them is statistically significant**, on either test.

**Why cost is so much noisier than the tokens that produce it: cache writes.** A run
that happens to write the cache instead of reading it pays $4.125 per million tokens
instead of $0.33 — 12.5×. On `manylists_400` two of fifteen runs in each arm wrote
the cache, and each of those runs cost **$0.10–0.13 more** than its arm-mates. That
single binary event is roughly four times the size of the arm effect, which is
exactly why the rank test on cost sees nothing while the rank test on the underlying
token counts is decisive.

### The token deltas account for the cost delta exactly

`manylists_400`, Sonnet 4.6 rates from `config_library/pricing.yaml`: input
$3.30/M, output $16.50/M, cache read $0.33/M, cache write $4.125/M.

| Component | Δ tokens (off − on) | Rate | Δ $/doc |
|---|---:|---|---:|
| uncached input | −53 | $3.30/M | −$0.0002 |
| cache read | −8,163 | $0.33/M | −$0.0027 |
| cache write | −965 | $4.125/M | −$0.0040 |
| **input side, total** | **−9,181** | | **−$0.0069** |
| output | +2,347 | $16.50/M | **+$0.0387** |
| **net** | | | **+$0.0319** |

The measured mean extraction-cost difference is $0.4571 − $0.4252 = **+$0.0319 per
document**. The arithmetic and the measurement agree to the cent.

This is the whole story in one line: **the input saving is 9,181 tokens worth about
0.7 cents, because it is nearly all cache reads; the output increase is 2,347 tokens
worth about 3.9 cents, because output is 50× the price of a cache read.**

---

## 5. Guidance

**Turn it off for shard headroom, not for dollars.**

- **Quality:** no cost measured, in 50 runs at 1.000 on every metric. A degradation
  affecting fewer than about one run in ten is not excluded.
- **Input tokens:** a real 5–7% reduction on a list-heavy class whose agent loop is
  stable, highly significant on two documents. Since
  [#775](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/775)
  the pipeline subtracts measured prompt overhead from the shard budget, so those
  tokens are real headroom — whether they move a given document's shard count still
  depends on it sitting near a boundary, and the `max_pages_per_shard` ceiling closes
  shards regardless.
- **Dollars:** not a saving. The point estimate is +5% to +8% on all three documents,
  driven by an output-token increase that outweighs the input saving by about 5.6×.
  None of the cost differences is individually significant, so the honest statement
  is "no saving, and a few percent more is the central estimate" rather than "costs
  more".

The axis is recorded in [config-guidance §7](../config-guidance.md) with the same
numbers.

---

## 6. The harness prices cache reads correctly

Cost claims in this study depend on cache reads being priced as cache reads.
`benchmarks/harness/lib.py::price_metering` matches **both** the pricing-table model
key and the unit name **exactly** — the unit lookup is a dict-key membership test,
never a substring test. Verified against a single run rather than taken from the code
alone.

`restate-on` / `manylists_400`, one run, Extraction phase metered on
`us.anthropic.claude-sonnet-4-6`:

| Unit | Count | Rate | Cost |
|---|---:|---|---:|
| `inputTokens` | 43,182 | $3.30/M | $0.142501 |
| `outputTokens` | 13,437 | $16.50/M | $0.221711 |
| `cacheReadInputTokens` | 104,371 | $0.33/M | $0.034442 |
| `cacheWriteInputTokens` | 28,181 | $4.125/M | $0.116247 |
| | | | **$0.514900** |

The harness's `cost_by_key` entry for that model on that run is **$0.51490** — the
hand recomputation exactly. (The run's `cost_by_phase.Extraction` reads $0.51503;
the extra $0.00013 is the non-Bedrock Lambda metering that the phase total also
carries, not a pricing discrepancy.)

⚠️ **The counterfactual is large, which is why this is checked rather than assumed.**
Both `cacheReadInputTokens` and `cacheWriteInputTokens` end in the string
`inputTokens`, so a substring match binds them to the row's `inputTokens` rate. Under
that behaviour the same run prices at **$0.80163** — **+55.7%**. That is the defect
[#926](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/926)
fixed in the production reporting path
(`idp_common/reporting/save_reporting_data.py::_get_unit_cost`); the two
implementations must stay in step, because a benchmark cost that disagrees with the
reported cost for the same metering map is a bug in one of them.

---

## Reproduce

```bash
python3 benchmarks/harness/gen_corpus.py
python3 benchmarks/harness/make_configs.py --suite restate710 --class bank_statement
AWS_PROFILE=default python3 benchmarks/harness/run_matrix.py \
    --stack <STACK> --suite restate710 --max-inflight 6
AWS_PROFILE=default python3 benchmarks/harness/aggregate.py \
    --run benchmarks/results/run-<stamp> --out benchmarks/results/v0.6.10/restate710
```

Scored data: `benchmarks/results/v0.6.10/restate710/` (3 documents × 5 repeats) and
`benchmarks/results/v0.6.10/restate710b/` (`manylists_400` × 10 further repeats).
The two are pooled per document for the `manylists_400` rows above, giving n = 15 per
arm; `meta.overrides` is `[]` in both, so both ran on the committed `default_cell`
with only the `schema_restatement` axis varying.
