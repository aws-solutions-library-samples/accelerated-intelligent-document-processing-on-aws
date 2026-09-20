---
title: "Greedy Decoding and Invalid Tool-Use Sequences"
---

<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->

# Does `topK: 1` fix the invalid tool-use failures? No — and here is what does cause them

> **Why this page exists.** Nova Lite fails mid-stream on Advanced (agentic)
> extraction with Bedrock's `Model produced invalid sequence as part of ToolUse`.
> AWS's own
> [Nova tool-use troubleshooting guide](https://docs.aws.amazon.com/nova/latest/userguide/tools-troubleshooting.html)
> attributes that error largely to inference parameters, and recommends greedy
> decoding — `temperature: 0` **and** `topP: 1` **and** `topK: 1`. The accelerator
> sends the first and not the last two on this path, so "we have not tried the thing
> the vendor recommends" was a live and reasonable objection
> ([#956](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/956)).
> It has now been tried, at enough sample size to say something. Greedy decoding does
> not help. Separately, the request shape that triggers the failure is identified,
> which is the part
> [#895](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/895)
> left open.

**The knob is deliberately not shipped.** There is no
`extraction.agentic.top_k` configuration option, because a user-facing setting whose
measured effect is null would be worse than not having one. What is worth keeping is
the routing table in §1 — otherwise the next person rediscovers it by trial and error
— and the trigger in §4.

Measured 2026-09-20, `us-west-2`, code version `v0.6.10.dev1`, stack
`idp-int1b-0920`.

---

## 1. `topK` is not a Converse field, and every family carries it differently

`inferenceConfig.topK` does not exist. **botocore rejects it client-side, for every
model**, before any request is sent:

```
ParamValidationError: Unknown parameter in inferenceConfig: "topK",
must be one of: maxTokens, temperature, topP, stopSequences
```

So `topK` can only travel in `additionalModelRequestFields`, and the shape it must
take there is per-provider. **Method: a 200 does not prove the key was read** —
some providers silently ignore unknown `additionalModelRequestFields` keys. Every
model was therefore also sent a deliberately bogus control key
(`{"bogus_zzz_param": 1}`) and an out-of-range value (`−5`). A carrier counts as
working only where the control is *rejected* (proving the provider validates its
field set) **and** the out-of-range value is rejected with a message naming the
field (proving the value is parsed, not swallowed).

| Family | Carrier that works | Valid range | The other carrier |
|---|---|---|---|
| Nova Lite, Nova Pro, Nova 2 Lite | `additionalModelRequestFields.inferenceConfig.topK` | `1`–`128` | `top_k` → `extraneous key [top_k] is not permitted` |
| Claude ≤ 4.6 (Haiku 4.5, Sonnet 4.5, Sonnet 4.6, Opus 4.5) | `additionalModelRequestFields.top_k` | `-1`–`100,000,000` | `inferenceConfig` → `Extra inputs are not permitted` |
| Claude 4.7+ (Opus 4.7, Opus 5, Sonnet 5) | none | — | `` `top_k` is deprecated for this model `` |
| OpenAI GPT-6 Astra | none | — | `unknown_parameter: Unknown parameter: 'top_k'` |
| xAI Grok 4.6 | **unverifiable** | — | returns 200 for *any* unknown key, including the control |

Three things to take from this table.

**The two families that do accept it reject each other's spelling.** There is no
single request shape that sets `topK` across Nova and Claude; a helper would have to
branch on the model family, which is one more per-model capability gate to keep
correct.

**Grok cannot be checked this way at all.** Grok 4.6 returned 200 for
`top_k`, for `inferenceConfig.topK` *and* for `bogus_zzz_param`. Because the control
passed, a 200 on the real key carries no information — the value may have been read
or silently discarded, and this probe cannot distinguish those.

**`strips_sampling_params()` is confirmed correct for all five families it covers.**
Claude 4.7+, Grok and Astra all reject the sampling group, and each rejection was
verified in isolation with `temperature` removed from the request — the first probe
round masked the `topK` answer for those models, because they rejected the request on
`temperature` before reaching `topK`.

> **Side finding, unrelated to `topK`.** `us.amazon.nova-premier-v1:0` answers every
> call — including the plain baseline — with `ResourceNotFoundException: This model
> version has reached the end of its life`, while still appearing in the model
> dropdown. It cannot be used for extraction, confidence or classification.

---

## 2. The A/B on Nova Lite: a well-powered null

### 2a. Direct-Bedrock micro-probe — the layer the conclusion rests on

Direct `bedrock-runtime.converse_stream` calls outside `idp_common`, so the
measurement does not inherit the pipeline's prompt assembly or model routing.
`us.amazon.nova-lite-v1:0`, one page of real Textract OCR text (5,164 characters),
the 401-character schema from §4 (the cheapest shape that reproduces the failure),
`maxTokens: 10000`, `temperature: 0`. Arms differ in **exactly one field**. Jobs were
interleaved and shuffled with a fixed seed before dispatch, so any drift in Bedrock
capacity over the run hits both arms equally. Two batches of 80 calls per arm.

| Batch | Arm | Invalid ToolUse | Rate | Fisher exact vs the same batch's no-topK arm |
|---|---|---:|---:|---:|
| 1 | no topK | 60/80 | 0.750 | — |
| 1 | `topK: 1` | 65/80 | 0.812 | 0.44 |
| 2 | no topK | 66/80 | 0.825 | — |
| 2 | `topK: 1` | 65/80 | 0.812 | 1.00 |
| 2 | `temperature 0` + `topP 1` + `topK 1` | 67/80 | 0.838 | 1.00 |
| pooled | no topK | 126/160 | 0.787 [0.716, 0.848] | — |
| pooled | `topK: 1` | 130/160 | 0.812 [0.743, 0.870] | 0.68 |

The **full greedy triple** was tested as its own arm on purpose: varying only `topK`
would leave "but you did not send `topP: 1`" as a live objection to a null result.
AWS's complete recommendation was sent, and it did not help either. It ran only in
batch 2, so the comparison quoted for it is the **within-batch** one (67/80 versus
66/80, *p* = 1.00); comparing it against the pooled no-topK arm instead mixes batches
and gives *p* = 0.39 — a number to prefer only if you also accept pooling two batches
whose shared arm differed by 7.5 points.

Every successful call in every arm returned **30 transaction rows**, so an "OK" was
never a call that succeeded while emitting nothing.

⚠️ **The no-topK arm itself moved 7.5 points between the two batches** (0.750 →
0.825), three times the 2.5-point pooled difference between arms. Within either batch
the arms are not distinguishable (*p* = 0.44 and *p* = 1.00). Treat the pooled row as
the generous reading: the movement of a single arm between batches bounds how much
meaning a 2.5-point difference between arms can carry.

### 2b. Power — read this beside the null every time

At the observed base rate of 0.787 and n = 160 per arm (two-sided α = 0.05), 80%
power detects a drop in the failure rate only as far as **0.646** — 14 percentage
points absolute, **18% relative**. To detect smaller effects:

| Effect to detect | n per arm for 80% power |
|---|---:|
| 10-point absolute drop (0.787 → 0.687) | **303** |
| 5-point absolute drop (0.787 → 0.737) | **1,136** |

**So: a large improvement from greedy decoding is ruled out, and a small one is not.**
Every point estimate in §2a moved in the *unhelpful* direction, none of them
significantly. This is a null result with a stated floor, not a demonstration that the
two settings are identical.

### 2c. Pipeline A/B — two units of observation, disagreeing in direction

64 pipeline runs on the deployed stack (32 per arm), Nova Lite extraction, Advanced
mode, across three documents at unequal repeat counts: `longdesc_100` and
`manylists_400` 12 runs per arm each, `small_narrow` 8.

**Document level** — did the pipeline deliver the document, which is what a user sees:

| Outcome | no topK | `topK: 1` |
|---|---:|---:|
| SUCCEEDED | 14 | 9 |
| `ExtractionShardMapFailed` | 12 | 12 |
| `MaxTokensReachedException` | 5 | 2 |
| `ModelInvalidToolUseSequence` | 1 | 7 |
| `Sandbox.Timedout` | 0 | 2 |
| **total** | **32** | **32** |

Fisher exact on success, 14/32 versus 9/32: *p* = 0.30. On document-level
`ModelInvalidToolUseSequence`, 1/32 versus 7/32: *p* = 0.053 — pointing at `topK: 1`
being **worse**.

**Shard level** — one agentic call chain, which is the denominator for a per-chain
error rate. 48 observed chains per arm. Chains the Distributed Map aborted because a
sibling failed are counted separately: they were cut short, so they are neither a
success nor an observed failure.

| Outcome | no topK | `topK: 1` |
|---|---:|---:|
| SUCCEEDED | 0 | 1 |
| `ModelInvalidToolUseSequence` | 14 | 10 |
| `MaxTokensReachedException` | 17 | 18 |
| ABORTED (sibling failed) | 17 | 19 |

Fisher exact on `ModelInvalidToolUseSequence`: *p* = 0.48 — pointing the **other
way**, at `topK: 1` being better.

Two units of observation on the same 64 runs disagreeing in *direction* is what noise
looks like, not a signal.

⚠️ **One confound specific to this layer.** In both batches every no-topK run was
launched before any `topK: 1` run, over windows of about 12 and 42 minutes, so the arm
is confounded with launch time. The micro-probe in §2a was interleaved precisely to
remove that, and is the layer the conclusion rests on; §2c is corroboration, not
independent evidence.

---

## 3. Nova Lite's completion rate on the Advanced path, and why the survivor quality must not be quoted

Of the 64 pipeline runs, **23 reached `COMPLETED`** — 14 without `topK`, 9 with
`topK: 1`. One further run was still `EXTRACTING` when the set was scored and is
counted with the 40 failures. **`manylists_400` completed zero runs in either arm.**

Among the 23 survivors, quality was not reliable: `cell_accuracy` ranged from
**0.19 to 1.000**, and one run reported `COMPLETED` with `completeness_recall`
**0.04** — 8 of 200 cells scored, i.e. a run that finished having extracted almost
nothing.

⚠️ **These survivor numbers are survivorship-biased and must not be quoted as Nova
Lite accuracy on the Advanced path.** 41 of 64 runs are missing, and they are missing
non-randomly: the largest document never finished at all, so the surviving set is
biased toward the easiest work.

---

## 4. The trigger: a list-of-objects that must be closed before further keys, with content to fill it

Walking the request shape from trivial to real, one property group at a time.
`us.amazon.nova-lite-v1:0`, the same one page of OCR text throughout.

Two user prompts were used and the distinction matters when reading the table. The
**plain** prompt is *"Extract the Bank Statement fields and call extraction_tool
once."*; the **rows-demanded** prompt adds *"including EVERY transaction row"*. A
variant run under the rows-demanded prompt whose schema has nowhere to put the rows is
asking the model for something the tool cannot express, which is its own possible
cause of a malformed call.

| Request shape | Schema chars | Prompt | n/arm | Outcome |
|---|---:|---|---:|---|
| flat two-string schema, no array | 141 | plain | 2 | all valid, both arms |
| synthetic array-only schema | 284 | rows demanded | 3 | all valid, 30 rows, both arms |
| real Bank Statement schema stripped to the `Transactions` array only | 1,696 | rows demanded | 3 | all valid, 30 rows, both arms |
| the same synthetic array **plus three sibling scalar properties** | 401 | rows demanded | 3 | 3/3 **invalid** (no topK); 1/3 invalid (`topK: 1`) |
| real schema, full | 2,181 | plain **and** rows demanded | 2 + 3 | all **invalid**, both arms, under both prompts |
| real schema minus the nested address group (array and scalars remain) | — | rows demanded | 3 | all **invalid**, both arms |
| real schema, `format` / `pattern` / `anyOf` stripped, or `$defs` inlined | — | plain | 2 | no change — still invalid |
| real schema, 1-page and 2-page OCR (5,164 / 10,785 chars) | 2,181 | rows demanded | 4 | all **invalid** at both sizes, both arms |
| real schema, **schema restatement removed from the system prompt** | 2,181 | plain | 2 (no-topK arm only) | no change — still invalid |
| real schema with the **OCR text removed** | 2,181 | plain | 2 (no-topK arm only) | all valid |
| real schema **minus** the `Transactions` array — no array anywhere in the schema | 1,964 | rows demanded | 3 | all **invalid**, both arms — but see the caveat below |

### What is ruled out

**Schema size is not the trigger.** A 1,696-character schema passes and a
401-character one fails. **JSON Schema vocabulary is not the trigger** either:
removing `format` and `pattern`, removing `anyOf`, and inlining `$defs` each changed
nothing. Nor is the **system-prompt schema restatement**, which was also removed with
no effect. Nor is the **amount of input**: doubling the OCR text did not change the
outcome. Content is nonetheless *necessary* — with the OCR text removed, so there is
nothing to extract, the real schema passes.

### What is established, and what is not

**One shape is demonstrably sufficient to trigger it: a list-of-objects property with
at least one sibling scalar property, and content to fill the list.** That is the
clean comparison in the table, because the 284-character and 401-character variants
share one prompt, one document and one model and differ only by three scalar
properties added after the array. The 284-character version passes and emits 30 rows;
the 401-character version fails. The plain-prompt pair points the same way: a
two-scalar schema with no array passes, an array-only schema passes, the real schema
with both fails.

⚠️ **That shape is not established as *necessary*, and one variant argues against it.**
The real schema with `Transactions` removed contains **no array at all** — three
scalar and nested-object properties only — and it still failed 3/3 in both arms. Read
literally that would refute the mechanism. It is not clean evidence either way,
because that variant ran under the rows-demanded prompt: the model was told to extract
every transaction row using a tool with no field to hold them. Its failure has an
alternative explanation that has nothing to do with arrays, and separating the two
would need the same variant re-run under the plain prompt, which was not done.

**So the honest scope of this section is:** the trigger is a property of the request
*shape* rather than of its size, vocabulary or input volume; an array-plus-siblings
schema with content is one shape that reliably produces it; and whether that is the
*only* such shape is open.

### Two sample-size limits on the table

- Every "all valid" row is **n = 2 or n = 3**. A shape that truly failed 20% of the
  time passes 3/3 about half the time, so these rows establish that those shapes are
  *usually* fine, not that they never fail.
- The 401-character variant's 3/3-versus-1/3 split between arms **did not
  reproduce**: at n = 80 per arm (§2a, batch 1) it failed 0.750 without `topK` and
  0.812 with it. That n = 3 split is the reason the micro-probe was run at all. The
  401-character shape is the cheapest request that reproduces the failure at a rate
  around 0.8 — not a shape that always fails.

---

## 5. What this changes

- **Greedy decoding is not a remedy.** `topK: 1`, and the full
  `temperature 0` + `topP 1` + `topK 1` triple, leave the invalid-tool-use rate
  statistically unchanged at n = 160 per arm — with 80% power only down to an 18%
  relative improvement. The escape hatch of "we never sent the parameters the vendor
  recommends" is closed.
- **The remedies that remain are the ones already documented**: emit less per call
  (`extraction.agentic.shard_token_budget` / `max_pages_per_shard`), choose an
  extraction model measured on this path, or `extraction.mode: simple`. §4 suggests
  why the first is only a partial remedy — fewer rows is not the same as a schema the
  model can close — but it does not predict a threshold, so shard sizing remains
  something to try on your own documents rather than a setting with a known safe
  value.
- **No configuration option was added.** See
  `lib/idp_common_pkg/idp_common/extraction/README.md` for where a `topK` would have
  to be plumbed if this is revisited, and why it cannot go in
  `_get_inference_params`.

---

## Reproduce

The routing probe and the micro-probe are standalone scripts against
`bedrock-runtime`; neither needs a deployed stack, and both cost under a dollar. The
pipeline A/B used the `topkab` suite at `--set extraction_model=nova_lite`:

```bash
python3 benchmarks/harness/make_configs.py --suite topkab --class bank_statement \
    --set extraction_model=nova_lite
AWS_PROFILE=default python3 benchmarks/harness/run_matrix.py \
    --stack <STACK> --suite topkab --set extraction_model=nova_lite --max-inflight 6
```

Scored data: `benchmarks/results/v0.6.10/topkab__extraction-model-nova-lite/` (2
documents × 4 repeats) and `benchmarks/results/v0.6.10/topkab2__extraction-model-nova-lite/`
(3 documents × 8 repeats), pooled to 32 runs per arm. `summary.json` records the
terminal status per run; the per-error breakdowns in §2c come from the Step Functions
execution history keyed back through the runmap, which is not part of the scored
output.
