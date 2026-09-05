---
title: "Should we pad prompts to clear the cache minimum?"
---

<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->

# Should we pad prompts to clear the cache minimum? — measured, with a proposal

**Short answer: the arithmetic says yes, the measurement agrees, and we should still not
do it automatically.** Padding is a real ~20% saving on extraction *input* cost for an
affected class, but its sign flips on workload shape, its accuracy risk cannot be bounded
tightly on any corpus we own, and at three of the four model tiers the pad would be a
sizeable fraction of the prompt. The right change is to make the problem *visible* and give
operators better levers — not to inject tokens on their behalf.

Background mechanism: [prompt-caching.md](prompt-caching.md). Everything here is measured
on stack `IDPBench` at the v0.6.7 tag. Data: `benchmarks/results/v0.6.7/prompt-cache/`.

---

## 1. How many classes are affected — measured across 32 shipped classes

A `<<CACHEPOINT>>` only creates an entry if the prefix before it clears the model's
minimum. Measured prefix token counts for every class in five shipped presets
(`cache_prefix_survey.py`, real prefixes, counted by Converse not estimated):

| model minimum | models | classes that **never** cache |
|---:|---|---:|
| 512 | Opus 5, Fable 5 | **0 of 32 (0%)** |
| 1,024 | Sonnet 5, Sonnet 4.6, Opus 4.8 | **8 of 32 (25%)** |
| 2,048 | Opus 4.7 | **31 of 32 (97%)** |
| 4,096 | Opus 4.6, Opus 4.5, **Haiku 4.5** | **32 of 32 (100%)** |

The eight that miss at 1,024: `rvl-cdip` × 5 (`language` 874, `generic` 877,
`handwritten` 888, `news_article` 967, `specification` 1021), `ocr-benchmark` ×2
(`GLOSSARY` 949, `SHIFT_SCHEDULE` 1000), `lending-package-sample` ×1
(`Bank-checks` 941). Nine more sit between 1,021 and 1,104 — inside the margin a single
added field would move.

⚠️ **On Haiku 4.5 every `<<CACHEPOINT>>` in the product is inert.** Not "less effective" —
inert, for every shipped class, silently. That is worth knowing before choosing Haiku to
save money on extraction.

## 2. The arithmetic

Billed token-equivalents (multiples of base input price); prefix `P` below minimum `M`:

```
unpadded (never caches):   N · P
padded to M:               1.25·M + 0.10·M·(N−1)
break-even:                N > 1.15·M / (P − 0.10·M)
```

| tier | classes below | median pad needed | pad as % of prefix | steady-state saving | break-even N |
|---:|---:|---:|---:|---:|---:|
| 512 | 0 | — | — | — | nothing to do |
| **1,024** | 8 | **79 tok** | **8%** | **89%** | **1.4** |
| 2,048 | 31 | 870 tok | 74% | 83% | 2.4 |
| 4,096 | 32 | 2,912 tok | **246%** | 65% | 6.1 |

The arithmetic favours padding at *every* tier — even 4,096, because 4096 × 0.1 = 410 is
still less than a ~1,000-token full-price prefix. **The pad *size* is what disqualifies the
upper tiers**: tripling a prompt to win a cache is a different prompt, not an optimisation.

## 3. Measured, end to end

A 201-token block of generic extraction guidance appended before the marker, applied to
**only** the two sub-minimum `ocr-benchmark` classes via `x-aws-idp-extraction-task-prompt`
(surgical, so the other seven are an untouched control). 293 documents, paired against the
existing baseline run.

| | treated (2 classes, 41 docs) | control (7 classes, 241 docs) |
|---|---|---|
| prefix | `GLOSSARY` 949→1150, `SHIFT_SCHEDULE` 1000→1201 | unchanged |
| cacheRead Δ/doc | **+1,094 / +1,129** | ±0 to −213 (cache-warming noise) |
| uncached input Δ/doc | **−943 / −994** | **exactly +0** |
| cost Δ/doc | **−$0.00274, t = −17.4** | −$0.00015, t = −1.35 |
| accuracy Δ | **−0.0081**, 95% CI **[−0.0240, +0.0078]** | +0.0001, t = +0.14 |

**Cost: unambiguous.** −$0.0027/doc is **−20% of extraction input cost** for those classes.
The mechanism is deterministic — `uncached input Δ` is exactly **+0** on all seven control
classes and −943/−994 on the two treated ones, which can only happen if the prefix moved
into the cached span.

**Accuracy: no effect detected, and the bound is loose.** 36 of 41 treated documents scored
**identically**; four moved by ≤0.002. The whole pooled −0.0081 is **one document**
(`GLOSSARY_185.png`, 1.0000 → 0.6667, `pageNumber` 3 → null).

> That looked causal — the pad says "where a field is absent, return null; do not infer it"
> and "prefer the document body over a header or footer", and a glossary page number is a
> footer value. **It does not reproduce.** Re-running that document three times per arm at
> temperature 0 with the page image attached returns `3` in **3/3** calls both with and
> without the pad. (A first attempt at the repro returned `null` in both arms because it
> omitted `{DOCUMENT_IMAGE}` — the page number is read from the image, not the OCR text.
> A broken repro is worse than none.) So the flip is pipeline non-determinism and the pad
> is not implicated.

The 95% CI still admits a **2.4pp regression**. Bounding a 1pp regression needs **n ≥ 105**
paired documents of the treated classes; the corpus contains **49**. *No corpus we own can
tighten this*, which matters for the proposal.

## 4. The sign flips on workload shape

The TTL is 5 minutes. The relevant quantity is *documents of that class per 5 minutes* — a
property of the workload, not the configuration:

| documents of the class per TTL | unpadded | padded | Δ$/doc | verdict |
|---:|---:|---:|---:|---|
| **1 (one at a time)** | 949 | 1,280 | **+$0.00109** | **padding LOSES 35%** |
| 2 | 949 | 691 | −$0.00085 | padding wins |
| 6 | 949 | 299 | −$0.00215 | padding wins |
| 60 (batch) | 949 | 122 | −$0.00273 | padding wins |

Annualised steady-state, one affected class: **$28/yr** at 10k docs, **$335/yr** at 10k/mo,
**$2,794/yr** at 1M.

**A configuration cannot observe arrival rate.** Auto-padding would therefore make
low-volume and interactive deployments ~35% *worse* on the affected prefix, to save a busy
batch deployment ~$300/yr per class. That asymmetry is the core argument against automating
it.

---

## 5. Proposal

### 5.1 Do — make it visible (no risk, unblocks everything else)

1. **Warn at configuration-validate time.** For each class, compute the prompt prefix and
   compare against the *configured extraction model's* minimum. Emit:
   *"class `GLOSSARY`: prompt prefix ≈949 tokens; `us.anthropic.claude-sonnet-4-6` requires
   ≥1024 to cache. This class will never use prompt caching; every request pays full input
   price on its prefix."* Cheap and deterministic — `cache_prefix_survey.py` already does
   the measurement; it needs a token estimate rather than a live call in-product.
2. **Surface cache efficiency per class** in the processing report and the cost report.
   `cacheReadInputTokens` / `cacheWriteInputTokens` are already in the metering map and
   already priced; nothing reads them back to the operator. `cache_audit.py` shows the three
   states worth distinguishing: *caching* / *write-only (paying 1.25× for nothing)* /
   *never cached*.
3. **Document the per-model minimum table** next to `extraction.model`, and state that it is
   **not monotonic** across generations (512 → 4,096 → 1,024 as you move Opus 4.6 → 4.8).

### 5.2 Do — offer better levers than padding

4. **Model choice is the strongest lever and touches no prompt.** Opus 5 / Fable 5 have a
   512-token minimum: *all 32* shipped classes cache. If the concern is caching, that is a
   one-line config change with no prompt risk.
5. **Enrich the class instead of padding it.** The eight affected classes are short
   *because they are thin* — `rvl-cdip/language` has almost no field descriptions. Adding
   real descriptions raises the prefix **and** improves extraction. Same tokens, two
   benefits, no behaviour-change gamble.
6. **`extraction.forced_tool.enabled` also lifts a sub-minimum class** (+~820 prefix tokens,
   measured accuracy-neutral over 282 paired documents — see
   [config-guidance §7](config-guidance.md)). Note it *taxes* already-caching classes
   (+6% to +23% prefix cost), so it is a per-config decision, not a per-class one.

### 5.3 Do — add the knob that is currently missing

7. **`extraction.prompt_cache: auto | off`.** Today the cachePoint is unconditional, and for
   a one-off document that is a measured **+24.9%** on the prefix (write 1.25×, no read
   follows). A low-volume or interactive deployment has no way to turn that off. This is the
   one place where the product actively costs money it needn't, and the fix is a flag.

### 5.4 Do NOT — pad automatically

8. **No auto-injected padding**, filler or guidance. Three reasons, in order of weight:
   - **The sign flips on workload shape** (§4) and the config cannot see it. Auto-padding
     penalises exactly the deployments least able to notice.
   - **The accuracy bound cannot be tightened** (§3): ±2.4pp is the best any corpus we own
     supports, and "probably fine" is not the standard this repo holds other prompt changes
     to.
   - **Guidance-shaped padding is behaviour-changing by construction.** My 201-token block
     instructs the model to prefer nulls and to distrust headers and footers. It did not
     measurably hurt — but it is not *inert*, and shipping ~200 tokens of behaviour-changing
     text to win a cache would need per-class revalidation of every config it touched.
9. **Never pad at the 2,048 / 4,096 tiers.** The pad would be 74%–246% of the prompt.
   Document that caching is effectively unavailable there and move on.

### 5.5 Optional — explicit, per-class, opt-in

10. If 4–6 do not fit, a documented recipe (not a feature): append your own content to that
    class's `x-aws-idp-extraction-task-prompt` until the prefix clears the minimum, then
    **re-validate that class against ground truth**. `cache_prefix_survey.py --pad-file`
    measures the result. This is what was tested in §3; it works, and the operator owns the
    prompt change and the revalidation.

## 6. What would change the recommendation

* A **cheap prefix-length estimate in-product** (5.1) removes the discovery problem, after
  which most of this becomes an operator decision rather than a product one.
* If the affected classes were a large share of a real bill rather than 8 of 32 thin ones,
  auto-padding gated on an explicit "batch workload" declaration would be worth revisiting.
* If Anthropic lowered the Sonnet minimum to 512 (as Opus 5 did), §1 collapses to zero
  affected classes and only 5.3 survives.

## Reproduce

```bash
source .venv/bin/activate && export PYTHONPATH=$PWD/lib/idp_common_pkg

# §1 which classes cache, on which model tier
AWS_PROFILE=default python3 benchmarks/harness/cache_prefix_survey.py \
    --preset ocr-benchmark --preset lending-package-sample --preset rvl-cdip \
    --preset realkie-fcc-verified --preset bank-statement-sample

# §3 the padded arm, and the per-class paired comparison against a baseline run
AWS_PROFILE=default python3 benchmarks/harness/per_class_ab.py --stack <STACK> \
    --arm-a <baselineRunId> --arm-b <paddedRunId> \
    --treated GLOSSARY --treated SHIFT_SCHEDULE
```

---
> Mechanism: [prompt caching, measured](prompt-caching.md). Config advice:
> [Configuration Guidance](config-guidance.md).
