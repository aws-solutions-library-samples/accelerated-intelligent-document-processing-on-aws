---
title: "Prompt Caching — Measured Behaviour"
---

<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->

# Prompt caching in GenAIIDP — what actually happens, measured

> **Why this page exists.** Several cost claims in this repo rest on one sentence:
> the duplicated schema copies "sit inside the prompt-cache prefix, so they are cache
> reads at roughly a tenth of input price". That was never measured. It turns out to
> be **true for most classes and false for some**, and the exception is invisible —
> no error, no warning, no metric. This page measures the mechanism directly.

**Method:** direct `bedrock-runtime` Converse calls, outside `idp_common`, so the
measurement does not inherit the pipeline's prompt assembly or model routing. Every
number below is read back from `usage.cacheWriteInputTokens` /
`cacheReadInputTokens` / `inputTokens`. Reproduce with
`benchmarks/harness/cache_threshold_probe.py`. Raw data:
`benchmarks/results/v0.6.7/prompt-cache/`.

---

## 1. There is a minimum cacheable prefix, and below it caching silently does nothing

Anthropic publishes a per-model minimum. **It is not monotonic across generations**,
which is the trap:

| Model | Minimum cacheable prefix |
|---|---:|
| Claude Opus 5, Fable 5 | 512 tokens |
| **Claude Sonnet 5, Sonnet 4.6**, Opus 4.8 | **1,024 tokens** |
| Claude Opus 4.7 | 2,048 tokens |
| Claude Opus 4.6, Opus 4.5, **Haiku 4.5** | **4,096 tokens** |

Measured on Bedrock, `us.anthropic.claude-sonnet-4-6`, sweeping prefix size:

| prefix (total input tok) | uncached | cacheWrite | cacheRead | cached? |
|---:|---:|---:|---:|---|
| 585 | 585 | 0 | 0 | **no** |
| 847 | 847 | 0 | 0 | **no** |
| 985 | 985 | 0 | 0 | **no** |
| 1,009 | 1,009 | 0 | 0 | **no** |
| 1,032 | 1,032 | 0 | 0 | **no** |
| 1,059 | 13 | 1,046 | 0 | **yes** |
| 1,114 | 13 | 1,101 | 0 | **yes** |

**The effective boundary lies in (1,032, 1,059] total input tokens** — consistent
with the published 1,024-token minimum plus the ~13-token user turn. Below it,
`cacheWrite` and `cacheRead` are both **0** and no error is raised: the request is
billed entirely at full input price, and nothing in the response distinguishes that
from a cache that simply had not warmed yet.

⚠️ **`idp_common.bedrock.client` inserts a `cachePoint` wherever `<<CACHEPOINT>>`
appears in a prompt template, with no length check.** That is correct behaviour —
there is nothing better to do at that layer — but it means a document class whose
prompt prefix is short simply never caches, permanently and silently.

### Nova is different, and much more forgiving

`us.amazon.nova-2-lite-v1:0`, same sweep: **every** prefix tested cached, including
the smallest (a 355-token prefix produced `cacheWrite: 355`). A write only happens
when the minimum is met, so **Nova's minimum is ≤355 tokens** — well below anything
these presets produce. Combined with Nova's price multipliers in `pricing.yaml`
(read **0.25×**, write **1.0×**, versus Claude's 0.1× / 1.25×), Nova has neither
failure mode: it always caches, and a write that is never read costs exactly what an
uncached read would have cost. **Both mechanisms in this page are Claude-specific.**

This matters for scoping: `ocr-benchmark` **as shipped** pins
`extraction.model: us.amazon.nova-2-lite-v1:0` and therefore does *not* hit the
cliff. The per-class measurements in §2 pin Sonnet 4.6 instead, because the cliff is
a Claude question and Claude is what the docs recommend for extraction. A config
using a Nova extraction model is unaffected by §1–§3.

> **A note on our own instrument.** An earlier version of the sweep appeared to show
> Nova failing to serve reads on 5 of 8 repeat calls. That was an artifact of the
> probe, not of Nova: `filler()` builds text by appending, so `filler(400)` is a
> literal prefix of `filler(700)` and consecutive sweep points share cache state.
> Re-measured with six **identical** calls, both Nova and Claude write once and then
> read on every subsequent call. The flakiness was ours. It is recorded here because
> the same overlap would mislead anyone re-running the sweep.

## 2. Two of the nine classes in the shipped `ocr-benchmark` preset never cache

Measured per class, using each class's **real** prompt prefix (system prompt + the
task prompt up to `<<CACHEPOINT>>` with the actual schema substituted), with the
`cachePoint` placed in the user message exactly as the pipeline places it:

| class | prefix tokens | caches? |
|---|---:|---|
| `DELIVERY_NOTE` | 1,710 | yes |
| `BANK_CHECK` | 1,383 | yes |
| `EQUIPMENT_INSPECTION` | 1,305 | yes |
| `REAL_ESTATE` | 1,246 | yes |
| `PETITION_FORM` | 1,187 | yes |
| `CREDIT_CARD_STATEMENT` | 1,178 | yes |
| `COMMERCIAL_LEASE_AGREEMENT` | 1,335 | yes |
| **`SHIFT_SCHEDULE`** | **1,000** | **NO** |
| **`GLOSSARY`** | **949** | **NO** |

For comparison, `realkie-fcc-verified` → `Invoice` is 1,687 tokens and caches, and
`lending-package-sample` → `Bank-checks` estimates at ~944 and would not.

**Five of the seven caching classes sit between 1,178 and 1,383 — within ~35% of the cliff.** Adding a
few fields to a class, or shortening a task prompt, can move a class across it in
either direction with no visible signal. This is not a corner case.

## 3. A one-off document pays **+24.9%** on the prefix for caching it will never reuse

A cache write costs **1.25×** base input; a read costs **0.1×**. So a prefix that is
written and never read inside the TTL is a *net loss*. Measured against the exact
counterfactual — identical prefix, `cachePoint` versus no `cachePoint`:

| scenario | uncached | cacheWrite | cacheRead | billed token-equivalents |
|---|---:|---:|---:|---:|
| no `cachePoint`, 1 call | 1,637 | 0 | 0 | **1,637** |
| `cachePoint`, 1st call (write) | 9 | 1,628 | 0 | **2,044** |
| `cachePoint`, 2nd call (read) | 9 | 0 | 1,628 | **172** |

**A single document costs 1.249× what it would with no `cachePoint` at all.**
Break-even is exactly the second request within the TTL:

| requests in TTL | with cache | without cache | winner |
|---:|---:|---:|---|
| 1 | 2,044 | 1,637 | **cache loses (+24.9%)** |
| 2 | 2,216 | 3,274 | cache wins |
| 3 | 2,388 | 4,911 | cache wins |
| 5 | 2,731 | 8,185 | cache wins |

The default TTL is 5 minutes, measured from the **start** of the writing request, and
a read refreshes the timer for free. So batch workloads and busy stacks collect;
a stack processing one document every few minutes pays the 25% surcharge on every
one of them.

## 4. What this changes about the schema-duplication claim

The claim "the duplicated copies are cache reads at ~a tenth of input price" is:

* **true** for a class above the minimum on a busy stack — the schema is in the
  cached span and reads at 0.1×, so a token saving there is worth only ~10% of its
  face value;
* **false** for a class below the minimum — the schema is billed at **full price on
  every request**, so a token saving is worth its **full** face value, ~10× more than
  we have been telling people;
* **false in the other direction** for a one-off document — the schema is billed at
  **1.25×**, so removing it saves more than face value.

Any statement of the form "the schema is cheap because it's cached" needs the class's
prefix length and the workload's arrival rate attached, or it is unfalsifiable.

## 5. Consequences for `extraction.forced_tool.enabled` (#744)

A forced toolSpec renders at position 0 — **before** the system prompt — so it
lengthens the cached prefix. Measured per class:

| class | prefix, forcing off | caches? | prefix, forcing on | caches? |
|---|---:|---|---:|---|
| `GLOSSARY` | 949 | **no** | 1,769 | **yes** |
| `SHIFT_SCHEDULE` | 1,000 | **no** | 1,831 | **yes** |
| `CREDIT_CARD_STATEMENT` | 1,178 | yes | 2,178 | yes |
| `BANK_CHECK` | 1,383 | yes | 2,492 | yes |
| `DELIVERY_NOTE` | 1,710 | yes | 2,980 | yes |

So forcing has a **cost effect that flips sign by class**, in steady state (all
prefix tokens, billed token-equivalents per request):

| class | off | on | effect |
|---|---:|---:|---|
| `GLOSSARY` | 949 × 1.0 = **949** | 1,769 × 0.1 = **177** | **−81%** |
| `SHIFT_SCHEDULE` | 1,000 × 1.0 = **1,000** | 1,831 × 0.1 = **183** | **−82%** |
| `CREDIT_CARD_STATEMENT` | 1,178 × 0.1 = **118** | 2,178 × 0.1 = **218** | **+85%** |
| `BANK_CHECK` | 1,383 × 0.1 = **138** | 2,492 × 0.1 = **249** | **+80%** |

This is a mechanism the existing guidance for #744 does not mention, and it is the
opposite of the intuition that "forcing adds a toolSpec, so it costs more". On a
sub-minimum class forcing is a **large prefix saving**; on a comfortably-caching
class it is a **moderate prefix increase**.

⚠️ **The prefix is only part of a request.** On `ocr-benchmark` a document carries
~7,800 input tokens in total, of which the prefix is ~1,000–2,900 — the rest is
document text and page images, after the cachePoint and therefore never cached. So
these percentages apply to a fraction of the bill, not to the bill. The end-to-end
magnitude is measured separately (§6).

## 6. End-to-end magnitude — measured, and smaller than the mechanism suggests

The §2 and §5 predictions were registered **before** the run and then confirmed on 293
real documents (stack `IDPBench` at the v0.6.7 tag, `ocr-benchmark`, Sonnet 4.6):

| class | forcing off: cacheRead/doc | verdict | forcing on: cacheRead/doc | verdict |
|---|---:|---|---:|---|
| `GLOSSARY` (23 docs) | **0** | **never cached** | **1,839** | caching active |
| `SHIFT_SCHEDULE` (18 docs) | **0** | **never cached** | **1,901** | caching active |
| 7 other classes | 1,000–1,706 | caching | 1,839–2,670 | caching |

Extraction-phase read share **28.4% → 48.1%**. So the cliff is real in production, not just
in a probe: 41 of 293 documents (14%) were paying full price on their entire prompt prefix,
on every request, with nothing reporting it.

**But the dollar effect is small, and honesty requires saying so.** Decomposing the −2.3%
cost delta that forcing produced on this corpus:

| token class | Δ/doc | $/MTok | Δ$ | share |
|---|---:|---:|---:|---:|
| `outputTokens` | −34 | 16.50 | −0.000561 | **87%** |
| `inputTokens` | −113 | 3.30 | −0.000373 | 58% |
| `cacheReadInputTokens` | +1,221 | 0.33 | +0.000403 | −63% |
| `cacheWriteInputTokens` | −27 | 4.12 | −0.000111 | 17% |

The whole input-plus-cache shift nets **13%** of the saving; **87% is fewer output tokens**.
The reason is arithmetic: moving a token from uncached input (3.30) to cache read (0.33)
saves 2.97/MTok, but forcing also *adds* ~1,000 prefix tokens that are then read at 0.33 —
and those two nearly cancel.

**So: the cliff is worth fixing for its own sake, not for its bill.** A class that never
caches is paying ~10× per prefix token what a caching class pays, which matters much more on
a workload whose prefix is a large fraction of the request. On `ocr-benchmark` the prefix is
~1,000–2,900 of ~7,800 input tokens; on a corpus of short documents with a big schema it
could be most of the request, and there the same mechanism would dominate.

Full A/B: [`config-guidance.md`](config-guidance.md) §7. Data:
`benchmarks/results/v0.6.7/forcing-real-corpus/`.

## 7. Instrumentation note — why nobody noticed

`benchmarks/harness/detection_ab_teststudio.py` (the previous real-corpus A/B tool)
computes input tokens as *every metering key whose name contains both "token" and
"input"* — which sums `inputTokens`, `cacheReadInputTokens` **and**
`cacheWriteInputTokens` into one number. A change that moves tokens *between* those
classes therefore reports a delta of ≈0, and a 12.5× price difference (1.25× write vs
0.1× read) is invisible. Every prior real-corpus A/B in this repo was run with that
instrument.

`benchmarks/harness/real_corpus_ab.py` keeps the four classes separate and reports a
per-class cache verdict, and `benchmarks/harness/cache_audit.py` can be pointed at
any existing run to classify it as *caching normally* / *write-only (paying 1.25× for
nothing)* / *never cached*.

## Reproduce

```bash
source .venv/bin/activate && export PYTHONPATH=$PWD/lib/idp_common_pkg

# §1 the threshold itself
AWS_PROFILE=default python3 benchmarks/harness/cache_threshold_probe.py \
    --model us.anthropic.claude-sonnet-4-6 --targets 600 650 700 720 740 760 1100 2000

# §3 the write-without-read penalty (no-cachePoint counterfactual)
#     see the probe embedded in this page's commit message for the exact call shape

# audit any existing run for its cache verdict, per phase and per class
AWS_PROFILE=default python3 benchmarks/harness/cache_audit.py \
    --stack <STACK> --run <runId> --label "as-shipped"
```

---
> See [Configuration Guidance](config-guidance.md) for which settings to pick, and
> the [Benchmarking Guide](index.md) for how the suite is designed.
