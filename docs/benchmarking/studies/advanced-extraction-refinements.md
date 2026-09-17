---
title: "Advanced Extraction — Seven Candidate Refinements, Measured"
---

<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->

# Advanced (agentic) extraction: seven candidate refinements, measured

> **Why this page exists.** After the v0.6.8 release benchmark, seven ideas were on
> the table for making *advanced* extraction cheaper or more complete: getting the
> table rows out of the model's mouth entirely, dropping page images from the
> extraction prompt, moving to Sonnet 5's 1M-token context tier with wider shards,
> confirming prompt caching actually works across shards, passing OCR confidence
> through instead of asking a model, and buying Bedrock batch inference at half
> price. This page measures all of them on one stack, in one sitting, and says
> plainly which ones are real. Several of the seven turned into **defects** rather
> than measurements: the benchmark arm that was supposed to test "images off" had
> never changed anything, the code path that runs by default for multi-page tables
> had never honoured the optimization it shipped with, neither of the two agentic
> image knobs did anything on the single-pass path, wider shards fail outright above
> 800 rows, and the 1M-context tier's apparent 1.8× price is mostly an artifact of
> the cost table rather than of spend. Two of those defects turned out to be worth more
> than any of the seven tuning ideas: fixing them took the same 12-run arm from $68.82
> to $31.60 with completeness improving from 0.976 to 1.000 — 54% cheaper on identical
> inputs (§5). Simple mode is deliberately out of scope throughout: these are
> refinements to the agentic path only.

**Method.** Every number comes from the standard harness
(`benchmarks/harness/run_matrix.py`) against the live stack `IDPUpg067to068`
(us-west-2, v0.6.8), suite `advscale` — one cell, `core-tt-adv-sep` (Textract with
tables, **adv**anced extraction, **sep**arate confidence pass), six synthetic
bank-statement documents from 250 to 3,200 transaction rows (9 to 66 pages), two
repeats each, so 12 runs per arm. Cost is priced from the stack's own metering records
(`lib.price_metering` over the tracking table), not estimated — §1 is about a case
where that pricing is itself wrong. Extraction model is Sonnet 5
(`us.anthropic.claude-sonnet-5`) in every arm unless stated.

---

## 0. First, the noise floor — because most of these deltas are smaller than it

Two arms in this study were run with configurations that are **byte-identical in
behaviour**: the arm labelled `lazy-images-off` set a config key that nothing read
(see §4), so it is a second replicate of the Sonnet 5 baseline rather than a
treatment. That accident is useful, because it measures run-to-run variance with 24
runs of the same thing:

| | 12-run total cost | mean completeness recall | worst single run |
|---|---:|---:|---:|
| Baseline (run `run-20260914-214619`) | $68.82 | 0.976 | 0.804 |
| Same config again (run `run-20260914-214816`) | $75.14 | 1.000 | 1.000 |

Per document, the second replicate cost between **0.84× and 1.57×** the first:

| document (rows) | replicate A $/run | replicate B $/run | B/A |
|---|---:|---:|---:|
| med_narrow (400) | 1.60 | 1.55 | 0.97 |
| large_narrow (800) | 3.23 | 3.29 | 1.02 |
| dense_250 (250) | 1.66 | 1.38 | 0.84 |
| scale_1200 (1,200) | 4.20 | 6.60 | **1.57** |
| scale_1600 (1,600) | 6.88 | 6.98 | 1.01 |
| scale_3200 (3,200) | 16.85 | 17.78 | 1.06 |

An agentic run is a variable-length loop, so its cost has a long tail: the same
document, same config, can take one extra tool round trip and cost 57% more. **Read
every cost delta in this page against that envelope.** A single-doc difference below
about 1.6× is not evidence of anything, and the two dropped-row runs in replicate A
(recall 0.804 on dense_250, 0.908 on scale_3200) are variance, not a property of the
configuration — replicate B recovered every row on the same inputs.

---

## 1. Sonnet 5 on the 1M-token context tier: the 1.8× price gap is mostly a cost-table artifact

Sonnet 5 can be selected as `sonnet5_1m`, which the product turns into a request on
the 1M-token context tier. The hope was that wider shards would remove the shard
boundaries where rows go missing. The arm ran (run `run-20260914-214620`) and the
harness priced it at **$125.42 against the baseline's $68.82, a 1.82× total** —
which was the headline of an earlier draft of this section. That number does not
survive being checked, and the reason is worth more than the original finding.

Start with what the two arms actually consumed. Summing the Extraction-phase metering
records across all 12 runs of each arm:

| Extraction tokens, 12 runs | baseline | `:1m` arm | ratio |
|---|---:|---:|---:|
| fresh input | 7,457,162 | 8,144,158 | 1.09× |
| cache read | 22,087,966 | 23,087,890 | 1.05× |
| cache write | 1,324,126 | 1,402,002 | 1.06× |
| output | 1,512,506 | 1,549,408 | 1.02× |
| **all tokens** | **32,381,760** | **34,183,458** | **1.06×** |

The two arms did essentially the same amount of work. Repricing the `:1m` arm's own
measured token mix at the plain Sonnet 5 rate card gives $65.84 of extraction cost
against the baseline's $62.32 — 1.06×, inside the noise floor from §0. Priced at the
`:1m` rate card it is $118.90. **94% of the apparent gap is the rate card, not
spend.**

The rate card is where the problem is. `config_library/pricing.yaml` carries a
separate entry, `bedrock/us.anthropic.claude-sonnet-5:1m`, at exactly 2× input and
1.5× output of the plain entry (`6.6E-6` vs `3.3E-6` per input token, `2.475E-5` vs
`1.65E-5` per output token, and the same 2× on cache read and cache write). It is
applied to **every** token metered under that key, unconditionally. But the long
context premium is a **per-request tier** — it applies to a request whose input
exceeds 200K tokens, not to every request made by a model that *can* accept 200K+.
Two facts make that tier unreachable here:

- The product **strips the `:1m` suffix before invoking anything.** Both
  `bedrock/client.py` (`_strip_region_and_1m`) and `extraction/agentic_idp.py` map
  `…-sonnet-5:1m` to the ordinary `us.anthropic.claude-sonnet-5` inference profile
  and pass `additional_request_fields = {"anthropic_beta":
  ["context-1m-2025-08-07"]}`. There is no `:1m` inference profile in us-west-2 to
  invoke. The suffix survives only in the metering key, which is what the pricing
  table then matches on.
- **No request in the study came close to 200K input tokens.** Over the whole study
  window, across 8,041 metered Bedrock calls in `ShardRuntimeFunction` and 290 in
  `ExtractionFunction`, the largest single request was **34,015** input tokens
  (fresh + cache read) and the mean was 10,613 in the shard runtime and 22,679 in the
  single-pass function. Shards are auto-sized to the model's limits, so enabling a
  1M window does not by itself produce 200K-token requests — it raises a ceiling that
  nothing was pressing against.

So the honest statement is: **measured** token volumes are 1.06× baseline; the
harness *reports* 1.82× because of a flat premium in `pricing.yaml`; and under
tiered long-context pricing none of these requests should have paid that premium, so
real spend is very likely ~1.05× rather than 1.82×. What is **not verified** is the
last step against an actual AWS bill — this was not reconciled against Cost Explorer
or a CUR line item at per-model granularity, and if Bedrock were to charge the
premium for any request carrying the `anthropic_beta` header regardless of size, the
1.82× would be real. That is the one experiment left to run on this item.

**Product implication, and it is not benchmark-only.** The same `pricing.yaml`
prices the stack's own cost reporting and the UI. Any deployment that selects
`sonnet5_1m` (or `opus-5:1m`, priced the same way) will see roughly double the
extraction cost it is billed, on every document, with no indication why. The fix is
one of two things: make the pricing entry threshold-aware, or stop carrying the
`:1m` suffix into the metering key when the suffix is stripped before invocation. The
second is much simpler and matches what is actually invoked; it would also mean the
long-context premium is never charged, which is correct only as long as requests stay
under 200K. Filed as
[issue #899](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/899).

**Verdict on the refinement itself: still not a default,** for the completeness
reason rather than the price. The `:1m` arm returned every row in 12 of 12 runs — but
so did the baseline's own second replicate. 24 baseline runs produced two short runs,
12 `:1m` runs produced none, and at those sample sizes the difference is one or two
tail events either way. There is no measured completeness gain to buy, at any price.
And the natural way to use the wider window — wider shards — makes things worse, which
is §2.

---

## 2. Wider shards on the 1M tier: they failed outright, and once fixed they cost 5×

The point of a 1M-token context is to stop cutting documents into shards. The arm
that tests that is `sonnet5_1m` combined with `shard_pages=wide25`, which sets
`extraction.agentic.max_pages_per_shard` to 25 instead of the shipped 5. Measured
first on pre-fix code, it was the worst result in this study:

| document (rows, pages) | status | recall | wall | $/run | baseline $/run |
|---|---|---:|---:|---:|---:|
| med_narrow (400, 9) | COMPLETED ×2 | 1.000 | 497 s | 4.18 | 1.60 |
| large_narrow (800, 17) | COMPLETED ×2 | 1.000 | 1,561 s | 4.06 | 3.23 |
| dense_250 (250, 26) | COMPLETED ×2 | 1.000 | 758 s | 6.90 | 1.66 |
| scale_1200 (1,200, 25) | **FAILED ×2** | 0.000 | 29 s | 0.40 | 4.20 |
| scale_1600 (1,600, 33) | **FAILED ×2** | 0.000 | 34 s | 0.53 | 6.88 |
| scale_3200 (3,200, 66) | **FAILED ×2** | 0.000 | 55 s | 1.06 | 16.85 |

Six of twelve runs failed, in 28–56 seconds, with
`States.ExceedToleratedFailureThreshold` in the state machine over an
`ExtractionInputTooLarge` error. The arm's $34.25 total is not a saving — half of it
is documents that produced nothing, and the three that did complete were both slower
(large_narrow 1,561 s against the baseline's 296 s; dense_250 758 s against 143 s)
and dearer (dense_250 $6.90 against $1.66, 4.2×; med_narrow 2.6×).

The failure chain is mechanical and was reproduced offline. A 25-page section of these
documents is only about 19,000 tokens of text, so `plan_shards` concludes one shard is
enough and the section takes the **single-pass** agentic path rather than the sharded
one. That path attaches every page image of the section — 25 unresized 300-DPI JPEGs,
roughly 32 MB — and Bedrock rejects the request with `ValidationException: Input is
too long for requested model.` The configuration that exists to prevent exactly this,
`extraction.agentic.max_images_per_agent` (default 20), did not trim anything, because
on that path it was being applied to the wrong copy of the images. That is the third
defect in §4, and it is the reason a wide-shard setting fails instead of degrading.

### The re-run with the fix in place: the failures move, and the cost gets worse

The arm above was re-run on the same stack with the §4 and §5 fixes deployed
(`advfix2__sonnet5-1m-wide25`, 2026-09-15). Comparing it against the post-fix default
5-page arm rather than the pre-fix baseline, since both sides now run the same code:

| document (rows, pages) | wide25 status | wide25 recall | wide25 wall | wide25 $/run | 5-page $/run |
|---|---|---:|---:|---:|---:|
| med_narrow (400, 9) | COMPLETED ×2 | 1.000 | 259 s | 1.19 | 0.80 |
| large_narrow (800, 17) | COMPLETED ×2 | 1.000 | 500 s | 2.29 | 1.38 |
| dense_250 (250, 26) | COMPLETED ×2 | 1.000 | 455 s | 3.69 | 2.08 |
| scale_1200 (1,200, 25) | **FAILED ×2** (assessment) | 1.000 extraction | 625 s | 12.59 | 2.02 |
| scale_1600 (1,600, 33) | COMPLETED ×2 | 0.9997 | 1,199 s | 23.99 | 2.71 |
| scale_3200 (3,200, 66) | COMPLETED ×2 | 0.807 | 2,263 s | 35.24 | 6.81 |

The `ExtractionInputTooLarge` failures are gone: the image cap now trims the first turn
and all six of the previously dead runs get through extraction, three of them with recall
1.000. So the diagnosis in §4 was right about the cause. It was wrong about the
consequence — the arm's total went from $34.25 (half of it documents that produced
nothing) to **$158.00 against the 5-page default's $31.60 for the same twelve runs**, and
recall on the largest document fell to 0.807. Widening shards is worse with the fix than
the fix's absence made it look.

The mechanism is prompt caching, and it is the mirror image of §3. A 25-page shard is one
agent conversation covering ~1,200 rows instead of ~240, so the history grows past the
cached prefix and each tool turn re-reads a longer uncached tail. Per run, extraction
tokens (means over the two repeats):

| document | fresh input, 5-page | fresh input, wide25 | cache reads, 5-page | cache reads, wide25 |
|---|---:|---:|---:|---:|
| scale_1200 | 151,887 | 1,351,398 | 402,848 | 1,586,871 |
| scale_1600 | 204,954 | 2,933,867 | 556,854 | 1,827,560 |
| scale_3200 | 708,944 | 4,030,287 | 1,167,778 | 3,394,190 |
| dense_250 | 198,594 | 266,248 | 500,488 | 452,472 |

Fresh (uncached) input rises 8.9× on scale_1200 and 14.3× on scale_1600, while the
cache-read share falls from 0.73 to 0.38 — that is where the money goes. `dense_250`,
which fits a single shard either way, barely moves, which is the control.

The two remaining `scale_1200` failures are a **different** defect and worth naming
separately. Extraction succeeded completely on both runs — 1,200 of 1,200 rows, cell
accuracy 1.000, $17.34 and $7.05 spent — and then the document failed as a whole with
`ValidationException: Input is too long for requested model.` from the *assessment* step.
Assessment runs in-shard on the agentic path, so a 25-page shard hands one Nova Lite
confidence call five times as many rows as a 5-page shard does, and nothing catches that
`ValidationException`: a perfectly extracted, already-paid-for document is discarded. The
two documents that did complete show the quieter form of the same problem — `scale_1600`
scored 1,146 confidence leaves against 4,807 at the default shard width, `scale_3200`
2,275 against 9,605 — a ~76% drop in confidence coverage with recall unchanged. Why
coverage drops rather than fails is not established; the deadline-bounded self-healing
ladder keeping partial results is the obvious candidate but was not confirmed. Filed as
[issue #901](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/901).

**Verdict: do not widen shards, and the per-document sizing is not the knob it looks
like.** The fix turns a hard failure into an expensive success, which is the right
direction for robustness and the wrong one for anybody hoping wide shards would save
money. §1 already showed there was nothing to gain — the auto-sized default returns every
row — and this re-run puts a number on the loss: 5× the cost and 0.807 recall at 3,200
rows.

---

## 3. Prompt caching does survive sharding

The concern was mechanical: each shard is a separate agent conversation, so a
50-page document might be paying to write the same schema prefix five times and
reading it back zero times. It is not. Measured from the metering records, as the
cache-read share of all Extraction input tokens:

| cell | extraction model | cache-read share of input tokens |
|---|---|---:|
| `core-tt-adv-sep` | Sonnet 5 | 0.71 |
| `sonnet5-adv-sep` | Sonnet 5 | 0.70 |
| `core-tt-adv-int` | Sonnet 5 (integrated confidence) | 0.70 |
| `core-tl-adv-sep` | Sonnet 5 (Textract layout) | 0.71 |
| `core-bda-adv-sep` | Sonnet 5 (BDA branch) | 0.72 |
| `astra-adv-sep` | Astra | **0.97** |

Roughly 70% of every input token billed in an advanced Claude run is a cache read at
a tenth of the fresh rate, and on Astra it is 97%. Per document the share ranges
0.50–0.86 (smallest documents lowest, because a one-shot run has nothing to re-read).
So the shard structure is not defeating the cache — within a shard the prefix is
reused across the agent's tool turns, which is where the volume is. No change
recommended; the `metadata.prompt_cache` observability shipped in 0.6.8 is what made
this checkable at all.

---

## 4. "Images off" was not a measurement, it was three bugs

The idea was ordinary: for a table document the agent works from OCR text and can
fetch a page with `view_image` when it needs to, so attaching every page image to
every agent turn may be pure cost. The product already had a knob for exactly this —
`extraction.agentic.table_parsing.lazy_images`, default `true`, which suppresses
up-front image attachment when a pre-flight deterministic table parse has already
succeeded. The benchmark axis existed too. The first arm came back showing *better*
recall with images off, which made no mechanical sense, so it was checked instead of
believed. Nothing about the idea was measurable until three separate defects were
fixed, and the third one is also the cause of §2.

**Bug one: the benchmark axis wrote a key nothing reads.** The matrix set
`extraction.agentic.lazy_images`; the real field is
`extraction.agentic.table_parsing.lazy_images`. `make_configs.set_path` creates
missing dictionaries as it walks, so a mistyped path is not an error — it writes a key
no consumer looks at, and the arm silently becomes a duplicate of its own control.
That is where the 24 identical runs in §0 came from. The fix validates every axis path
against the `IDPConfig` model tree before writing it:

```
$ python3 benchmarks/harness/make_configs.py --suite advscale --set lazy_images=off
axis path does not exist in IDPConfig — 'extraction.agentic.lazy_images':
AgenticConfig has no field 'lazy_images'
```

All 26 axis paths already in the matrix pass the new check; only the new one failed.

**Bug two: the sharded agentic path never ran the pre-flight parse at all.** There are
two agentic code paths in `extraction/service.py` — the single-pass path inside
`_invoke_extraction_model`, and `_build_agentic_shard_plan`, which is what
`ShardRuntimeFunction` executes and which is the **default for any multi-page table
document**. The pre-flight table parse and the `lazy_images` decision lived only in the
single-pass path. The sharded path therefore never knew a table had already been parsed
and never suppressed anything: the shipped default was dead exactly where it mattered
most.

Log evidence over a 55-minute window covering ~36 advanced runs on the live stack,
before the fix:

| log line | count |
|---|---:|
| `Pre-flight table parsing complete` | **0** |
| `Skipping up-front image attachment` | **0** |
| `Attaching N images to extraction prompt` | **2,072** |

The fix extracts `_preflight_table_parse` and `_apply_lazy_images` as shared helpers
and calls them from both paths, so there is one rule with one home. Unit coverage went
in with it (`TestShardPlanLazyImages` in
`lib/idp_common_pkg/tests/unit/extraction/test_shard_payloads.py`): the sharded plan
must produce image-free shard payloads when the pre-flight parse succeeds and
`lazy_images` is true, image-bearing ones when it is false, and must keep the page
bytes it hands to the in-shard assessment pass either way. Reverting the service fix
turns the first of those tests red, which is the check that matters.

**Bug three: on the single-pass path, both image knobs were applied to the wrong copy
of the images.** Fixing bug two made the sharded path honour `lazy_images`, which
raised the obvious question of whether the single-pass path — the one that had always
carried the feature — honoured it either. It did not, for a reason that also explains
§2.

The prompt content is composed before the agentic branch forms any opinion about
images. `_prepare_section_context` → `_build_extraction_content` →
`_build_prompt_content` substitutes `{DOCUMENT_IMAGE}` into the template, which turns
the placeholder into real image content blocks. Only then does the agentic branch run,
and what it manipulated was the separate `page_images=` argument passed alongside that
content — a **second** copy of the same bytes. Three consequences followed from one
root cause:

- `lazy_images` suppressed nothing. It emptied `page_images`, while every page image
  remained in the content that was actually sent.
- `max_images_per_agent` capped nothing. `_cap_agent_images` trimmed the same
  argument, so a 25-page section sent 25 images against a default cap of 20 — the
  oversized first turn that the cap exists to prevent, and precisely the
  `ExtractionInputTooLarge` failure in §2.
- With suppression off, every page image was attached **twice** — once by the
  substitution and again by `_prepare_prompt_content` appending `page_images`.

There was a fourth, subtler effect. `create_view_image_tool` is registered only when
`page_images` is non-empty, so on the occasions when emptying that argument did have
an effect, it removed the agent's ability to look at a page at all — the fallback that
made lazy images safe in the first place.

Both offline composition and live logs agree. Composing the first turn for a two-page
section yields 2 image blocks when `lazy_images` claimed to have suppressed them —
the two the substitution placed, untouched — and 4 when it did not, which is the
doubling. On the live stack, `agentic_idp`'s own
"Attaching images to agentic extraction prompt" line appears **0** times while
`service.py`'s "Attaching N images to extraction prompt" appears 8 times in
`ExtractionFunction` and 2,546 times in `ShardRuntimeFunction`: every image that
reached the model came from the substitution, never from the argument the knobs
controlled.

The fix makes the **content** authoritative. A new
`ExtractionService._limit_content_images` enforces an image budget on the
already-rendered content (limit 0 for `lazy_images`, otherwise
`max_images_per_agent`, `None` for unlimited), and all three `structured_output` call
sites now pass `attach_page_images=False` — `page_images` still travels, but only as
the pool `view_image` draws from, so the agent keeps its fallback while the first turn
stays inside the budget. `_prepare_prompt_content`, `structured_output` and
`structured_output_async` gained the `attach_page_images` parameter, defaulting to
`True` so callers whose prompt carries no images of its own are unaffected.

Coverage is in `test_image_cap.py` (`TestLimitContentImages`, `TestNoDoubleAttachment`)
and a new `tests/unit/extraction/agentic_idp/test_single_pass_image_policy.py` that
runs the real branch with Bedrock stubbed and counts images in the composed first
turn. Reverting the service fix fails all three of the latter with exactly the counts
this section predicts: 3 images sent where 0 were expected under `lazy_images`, 7
where the cap allowed 2, and 8 where 4 pages should have produced 4.

---

## 5. Parse-first: the table tool's rows were being thrown away over date formats

The most valuable form of "parse first" is already implemented: `parse_table` →
`map_table_to_schema` → `finalize_table_extraction` keeps the full parsed rows in
agent state and shows the model only a head/tail summary, so the rows never have to
pass through the model's output tokens. The question was whether it was working.

It was failing, almost always, for one reason. A JSON-Schema `format: date` field
becomes a real `datetime.date` in the generated Pydantic model, so a `MM/DD/YYYY`
string — what bank statements contain and what the parser faithfully extracted —
can never validate. `finalize_table_extraction` then rejected the whole batch. Over
2026-09-12 00:00 → 09-14 12:00 on the live stack, counting
`finalize_table_extraction validation failed` log events in both agentic functions:

| `finalize_table_extraction` validation failures | `ShardRuntimeFunction` | `ExtractionFunction` |
|---|---:|---:|
| mentioning "Input should be a valid date or datetime" | **961** | **183** |
| everything else | 1 | 18 |

98.4% of the 1,163 failure events were date formats, and each event threw away a whole
batch: the individual row-level errors inside them number 291,498, between 4 and 1,989
per event. A failure is expensive twice: the
deterministic path yields nothing, and the agent falls back to emitting rows as
output tokens — the exact cost the tool exists to avoid. The old failure message did
not help it recover either; it reported the validation errors and the agent's natural
response was to re-emit the same rows.

Two changes address it. `map_table_to_schema` gained explicit date transforms —
`date_to_iso` (auto-detect), `date_to_iso_mdy`, `date_to_iso_dmy` — so the conversion
happens in the deterministic layer, with per-call refusal accounting and a cap of
three warnings so a bad column cannot flood the transcript. And
`finalize_table_extraction`'s failure message now names the offending fields
explicitly (`date_format_fields`), names the transform to apply, and tells the agent
to re-map rather than re-emit. The unit suite pins all four behaviours, including
that a structural failure does **not** claim a date remedy.

One further asymmetry was found while fixing §4 and was **left unfixed on purpose for
the duration of this study**. When the pre-flight parse succeeds, the single-pass path
appends a "PRE-PARSED TABLE DATA AVAILABLE" block to the agent's instructions: the
table count, the total row count, the column list, and a four-step workflow ending in
`finalize_table_extraction`. That block is written for shards — it explains the
`--- PAGE N ---` markers and tells the agent to extract only its assigned page range
— but `_build_agentic_shard_plan` never added it, so the agents that need it were the
ones that did not receive it. Adding it is a prompt change whose cost and accuracy
effect would have to be measured on a fresh arm, and doing that mid-study would have
broken comparability with every number on this page, so it was filed as
[issue #900](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/900)
rather than folded in. The pre-flight logging *was* moved into the shared
`_preflight_table_parse` helper, so both paths already left evidence that they ran;
that is observability only, with no effect on what the model sees.

> **Resolved after this study closed (#900).** The block now lives in one shared
> helper, `ExtractionService._append_preflight_table_guidance`, called from both the
> single-pass path and `_build_agentic_shard_plan`, so shard agents receive it too.
> The single-pass text is byte-identical to what produced the numbers on this page —
> a unit test asserts the block against the pre-refactor literal — so those numbers
> remain valid as the pre-change baseline. **The token saving on the sharded path is
> unmeasured**: confirming it needs the `advscale` arm re-run before and after on a
> live stack. The expected direction (fewer Extraction output tokens, recall
> unchanged at 1.000) is inferred from the 1,512,506 → 681,222 output-token drop
> measured below for the analogous single-pass fix, not observed for this change.

### What the two fixes are worth, measured

Both fixes — the date transforms from this section and the sharded-path `lazy_images`
fix from §4 — were deployed to the live stack's extraction Lambdas, and the same 12-run
arm was re-run twice: once with `lazy_images` forced **off**, which leaves the §4 fix
inert and isolates the date fix, and once with the shipped default **on**. Together
with the pre-fix baseline that gives a three-point decomposition on identical inputs:

| arm (12 runs each) | total cost | mean recall | Extraction input tokens | Extraction output tokens |
|---|---:|---:|---:|---:|
| pre-fix baseline | $68.82 | 0.976 | 30,869,254 | 1,512,506 |
| post-fix, images on (`lazy_images: false`) | $35.44 | 0.917 | 15,361,412 | 663,203 |
| post-fix, shipped default (`lazy_images: true`) | **$31.60** | **1.000** | **9,530,718** | 681,222 |

The date fix is the large one: **48% off the arm total** ($68.82 → $35.44) with
`lazy_images` still doing nothing, and the mechanism is visible in the token mix —
output tokens fall 56% (1,512,506 → 663,203) because the rows now leave through
`finalize_table_extraction` instead of the model's mouth, and input tokens fall 50%
because the agent no longer loops re-emitting rejected batches. Per document the saving
runs from 20% (scale_1600) to 63% (scale_3200), 41–49% on the two smallest; the
biggest table saves the most, as the mechanism predicts, but the spread is wide enough
that only the arm total is worth quoting.

The `lazy_images` fix adds a further **11%** ($35.44 → $31.60). Its effect on tokens
is much larger than its effect on cost: input tokens drop another 38% (15.4M → 9.5M),
almost entirely cache reads (11.35M → 6.10M) and cache writes (−52%), while output is
unchanged. Cache reads bill at a tenth of the fresh rate, so removing 5.2M of them
saves real money but not proportionally. Wall time also improves on the larger
documents (scale_1600 mean 497 s → 257 s). Per document the cost ratio is 0.84–0.86 on
four of six documents, 0.49 on scale_1600, and 1.08 on scale_3200 — the first four are
consistent, the last two are inside the §0 noise envelope in opposite directions.

The same log lines that showed the old behaviour now show the new one. Over the two
post-fix lanes, `Skipping up-front image attachment` fires **89** times where it fired
0 before, and date-format rejections collapse: normalising by runs, the three pre-fix
lanes (36 runs, 21:46–22:41) produced **202** date-format rejection events destroying
**53,774** rows, while the two post-fix lanes (24 runs, 23:31–00:07) produced **7**
events destroying **46** rows — 5.6 events per run down to 0.29, and 1,494 rejected
rows per run down to 1.9. The seven survivors reject 4 to 12 rows each rather than
hundreds, which is the agent recovering on a real edge case rather than the whole
batch dying on a format.

One caveat on the middle row: the images-on arm lost one run of dense_250 to a
`FAILED` status (recall 0.000), which is why its mean recall is 0.917 and why its
dense_250 token counts are a single run rather than two. That failure is the same
class of tail event as §0's short runs, not a property of the configuration — the
shipped-default arm returned every row on the same document in both repeats.

**Everything above this subsection was measured on the pre-fix stack.** The §0 noise
floor, the §1 and §2 arms, the §3 cache shares, the §6 Textract comparison and the §7
phase shares all predate the date fix, so their absolute costs on this corpus are
roughly twice what the same runs would cost today. The comparisons within each arm are
unaffected, since both sides of every one of them ran on the same image.

---

## 6. Textract feature set: `[LAYOUT]` alone is 23% cheaper and breaks the parser on the densest document

This was not one of the seven, but it fell out of the same corpus and it is the only
real cost lever measured here, so it belongs on the page. The `advscalelayout` suite
runs the same six documents through cell `core-tl-adv-sep`, which asks Textract for
`[LAYOUT]` only instead of `[TABLES, LAYOUT]`. Textract's table feature is billed
separately, so dropping it is a direct saving on the OCR phase:

| document (rows) | `[TABLES, LAYOUT]` $/run | `[LAYOUT]` $/run | ratio | recall with TABLES | recall with LAYOUT |
|---|---:|---:|---:|---:|---:|
| med_narrow (400) | 1.60 | 1.45 | 0.91 | 1.000 | 1.000 |
| large_narrow (800) | 3.23 | 2.92 | 0.90 | 1.000 | 1.000 |
| dense_250 (250) | 1.66 | 1.56 | 0.94 | 0.902 (mean) | **0.098 (mean)** |
| scale_1200 (1,200) | 4.20 | 3.43 | 0.82 | 1.000 | 1.000 |
| scale_1600 (1,600) | 6.88 | 4.99 | 0.73 | 1.000 | 1.000 |
| scale_3200 (3,200) | 16.85 | 12.09 | 0.72 | 0.954 (mean) | 1.000 |
| **12-run total** | **$68.82** | **$52.89** | **0.77** | 0.976 | 0.850 |

The saving is real and grows with document size — 23% overall, 28% on the largest
document — and it is outside the noise envelope from §0 because it is a per-page
Textract charge, not a variable-length agent loop. Five of the six documents lost
nothing: recall 1.000, cell accuracy 1.000.

The sixth is the problem. dense_250, the document with the tightest table layout,
scored 0.196 and 0.000 recall on its two repeats — a mean of 0.098 against 0.902 with
tables enabled. The mechanism is the deterministic parser's input: with `[TABLES,
LAYOUT]`, Textract's table blocks are rendered as Markdown pipe tables, which
`parse_table` consumes. With `[LAYOUT]` alone there are no table blocks, the page
renders as tab-separated text, `parse_table` finds no table, and the agent is left
extracting a 250-row table through its own output tokens — where it truncates.

**Verdict: a real saving, but not a safe default.** `[LAYOUT]` only is the right
choice when the documents are known to be layout-simple or when the deterministic
table path is not in use; it is the wrong choice for the exact workload advanced
extraction exists for. If it were ever made a default it would need the pre-flight
parse result as a guard — if no table parses, the feature set is wrong for the
document — which is a larger change than this study's scope.

---

## 7. Passing OCR confidence through instead of asking a model: not a cost lever

The proposal was to report Textract's per-cell OCR confidence for cells the
deterministic parser produced, instead of running a separate model pass to score
them. As a **cost** argument it does not survive contact with the phase breakdown.
Per-document cost by phase, baseline arm, two runs per document:

| document | total $ | OCR | Classification | Extraction | Assessment |
|---|---:|---:|---:|---:|---:|
| dense_250 | 1.66 | 24% | 1% | 74% | 1% |
| med_narrow | 1.60 | 8% | 1% | 90% | 1% |
| large_narrow | 3.23 | 8% | 1% | 90% | 1% |
| scale_1200 | 4.20 | 9% | 1% | 89% | 2% |
| scale_1600 | 6.88 | 7% | 0% | 91% | 1% |
| scale_3200 | 16.85 | 6% | 0% | 93% | 1% |

The separate confidence pass is **1–2% of document cost** (it runs on Nova Lite, and
0.6.8 already cut it by a third by fixing a repetition loop). Extraction is 74–93%.
Removing the confidence pass entirely would be inside the noise envelope from §0.
Nor is there an accuracy case on this corpus: cell accuracy is 1.000 and mean
confidence 0.96–0.99, so there is no miscalibration left to correct.

**Verdict: not for cost.** The remaining argument is honesty — an OCR confidence is a
measurement of the OCR, while a model's self-reported confidence on a cell it did not
read is closer to a guess — and reliability, since the model pass has historically
truncated and looped. Those are real arguments, but they are not this study's, and
they would have to be made on a corpus with actual OCR errors.

## 8. Bedrock batch inference at 50%: the agentic path cannot use it, and it would not pay

Two independent findings, either one sufficient.

First, eligibility. The Bedrock documentation is explicit: *"Batch inference does not
support tool calling (function calling) or structured output (`response_format`). Each
record in the input JSONL file is processed independently without multi-turn
interaction, so features that require back-and-forth exchanges between the model and
client are not available."*
([Process multiple prompts with batch inference](https://docs.aws.amazon.com/bedrock/latest/userguide/batch-inference.html)).
Advanced extraction is tool calling and multi-turn by construction — the three-table
pipeline, `view_image`, the escalation retries. There is no version of the agentic
path that fits in a batch record. The operational constraints point the same way: a
minimum of 100 records per job and best-effort completion within 24 hours do not fit a
per-document pipeline whose SLA is minutes.

Second, price, ignoring eligibility entirely. Batch is 50% off, but batch records are
independent, so **there is no prompt cache** — every input token is billed fresh at
half the fresh rate, against on-demand's mix of fresh, cache-write at 1.25× and
cache-read at 0.10×. Counterfactual over every advanced run available (cost priced
from the measured token mix per cell):

| cell | cache-read share | on-demand $ | batch-at-50%, no cache $ | delta |
|---|---:|---:|---:|---:|
| `astra-adv-sep` | 0.97 | 107.77 | 209.04 | **+94%** |
| `core-bda-adv-sep` | 0.72 | 117.45 | 124.71 | +6% |
| `core-tt-adv-sep` | 0.71 | 466.29 | 485.78 | +4% |
| `core-tt-adv-int` | 0.70 | 140.60 | 144.32 | +3% |
| `sonnet5-adv-sep` | 0.70 | 76.01 | 77.21 | +2% |
| `core-tl-adv-sep` | 0.71 | 112.68 | 102.48 | −9% |

The sign follows the cache-read share: on a well-cached workload the 50% discount does
not cover losing a 10× discount on 70–97% of input tokens, and the one cell where
batch wins does so by less than the noise floor. An earlier draft of this section
claimed batch was worse in 11 of 12 cells by up to +100%; measured per cell across all
available runs it is roughly a wash for Claude arms (+2% to +6%), clearly worse only
for the highest-caching arm, and marginally better for one. Either way it is not the
50% saving the price sheet suggests.

## 9. Deliberately not measured

Two items were dropped before spending anything, and both deserve a sentence so the
next person does not re-run them.

**OCR DPI.** The only cost-motivated move here would be *lowering* DPI back to 150,
and that direction is contraindicated already. v0.6.7 raised `ocr.image.dpi` from 150
to 300 (#740), and the v0.6.7 release audit names that change as the likely cause of
an LLM-OCR completeness jump from 0.564 to 1.000 on 9 of 9 runs — likely, but
[explicitly untested](../releases/v0.6.7.md). Meanwhile the whole OCR phase is 6–24%
of document cost against extraction's 74–93%, so the best case is shaving part of a
phase that is not the problem while risking faint characters that Textract drops below
about 200 DPI. Not worth an arm; if anyone does test it, the question to answer is
whether the 0.564 → 1.000 attribution is real, not whether 150 is cheaper.

**Larger-context models generally.** §1 and §2 are the measurement. On this corpus a
bigger window buys nothing that a retry does not: the auto-sized default already
returns every row in 12 of 12 runs, the `:1m` arm's token consumption is 1.06× the
baseline's, and the one configuration that genuinely exploits a wider window — 25-page
shards — fails on every document above 800 rows for reasons that have nothing to do
with the context limit. There is no reason to expect a different answer from a
different large-context model.

---

## Reproduce

```bash
source .venv/bin/activate && export PYTHONPATH=$PWD/lib/idp_common_pkg
export AWS_PROFILE=default

# §0/§1 — baseline, its replicate, and the :1m arm (12 runs each)
python3 benchmarks/harness/make_configs.py --suite advscale --class bank_statement \
    --set extraction_model=sonnet5
python3 benchmarks/harness/run_matrix.py --stack <STACK> --suite advscale \
    --set extraction_model=sonnet5 --native-upload --max-inflight 4
python3 benchmarks/harness/run_matrix.py --stack <STACK> --suite advscale \
    --set extraction_model=sonnet5_1m --native-upload --max-inflight 4
python3 benchmarks/harness/aggregate.py --run <runId> \
    --out benchmarks/results/v0.6.8/advscale__extraction-model-sonnet5

# §1 — Extraction token volumes per arm, to separate rate card from spend
python3 scratch/adv-refine/tokens.py base=benchmarks/results/<run-dir> \
    1m=benchmarks/results/<run-dir>

# §2 — the wide-shard arm. Pre-fix, 6 of 12 runs fail with ExtractionInputTooLarge;
# post-fix, the same command produces the $158.00 arm (2 assessment failures).
python3 benchmarks/harness/run_matrix.py --stack <STACK> --suite advscale \
    --set extraction_model=sonnet5_1m --set shard_pages=wide25 \
    --native-upload --max-inflight 4

# §3/§7/§8 — phase shares, cache-read shares and the batch counterfactual, from the
# tracking table's metering records for an existing run
python3 scratch/adv-refine/phase_cost.py benchmarks/results/<run-dir>
python3 scratch/adv-refine/batch_vs_cache.py benchmarks/results/<run-dir>

# §4 — the axis-path validator (should refuse a path IDPConfig does not have)
python3 benchmarks/harness/make_configs.py --suite advscale --set lazy_images=off

# §5 — the two post-fix arms that decompose the date fix from the lazy_images fix
# (same suite, on extraction Lambdas carrying both fixes)
python3 benchmarks/harness/run_matrix.py --stack <STACK> --suite advscale \
    --set extraction_model=sonnet5 --set lazy_images=off --native-upload --max-inflight 4
python3 benchmarks/harness/run_matrix.py --stack <STACK> --suite advscale \
    --set extraction_model=sonnet5 --native-upload --max-inflight 4

# §6 — the Textract [LAYOUT]-only arm
python3 benchmarks/harness/run_matrix.py --stack <STACK> --suite advscalelayout \
    --set extraction_model=sonnet5 --native-upload --max-inflight 4

# §4/§5 — the unit coverage that pins all four fixes
python3 -m pytest lib/idp_common_pkg/tests/unit/extraction/test_shard_payloads.py \
    lib/idp_common_pkg/tests/unit/extraction/test_image_cap.py \
    lib/idp_common_pkg/tests/unit/extraction/test_finalize_table_extraction.py \
    lib/idp_common_pkg/tests/unit/extraction/test_map_table_to_schema.py -q
python3 -m pytest -m agentic \
    lib/idp_common_pkg/tests/unit/extraction/agentic_idp/test_single_pass_image_policy.py -q
```

Result sets, all under `benchmarks/results/v0.6.8/`:
`advscale__extraction-model-sonnet5` (pre-fix baseline),
`advscale__lazy-images-off` (the accidental replicate, §0),
`advscale__extraction-model-sonnet5-1m` (§1),
`advscale__sonnet5-1m-wide25` (§2, pre-fix),
`advfix2__sonnet5-1m-wide25` (§2, the post-fix re-run),
`advscalelayout__extraction-model-sonnet5` (§6), and the two post-fix arms
`advfix__lazy-images-off` and `advfix__lazy-images-on` (§5).

---
> See [Configuration Guidance](../config-guidance.md) for which settings to pick, and
> the [Benchmarking Guide](../index.md) for how the suite is designed.
