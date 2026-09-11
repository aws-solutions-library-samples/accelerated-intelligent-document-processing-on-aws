# corefast v0.6.7 — NOT a valid release gate: the stack changed mid-run

**Run:** `run-20260903-132030`, 171 runs (19 cells × 3 docs × 3 repeats), stack `IDP1`
(us-west-2, acct 912625584728), scored 2026-09-03T17:46Z.
**Baseline compared against:** `results/baseline.json` — v0.6.6, stack `IDPRel066`,
commit `7fb426b27`.
**Costs** are estimates from `config_library/pricing.yaml`, rates as of 2026-09-02.

## Why this run cannot be used as the release gate

`IDP1` was updated by CloudFormation **twice while the grid was running**, by another
party. The grid therefore spans more than one build and is not a controlled comparison.

| launch window | runs | share | code state |
|---|---|---|---|
| before 13:25 | 5 | 3% | the build present when the run started |
| **13:25–13:47 (update #1 in progress)** | **22** | **13%** | **Lambdas being replaced mid-flight — unreliable** |
| 13:47–16:53 | 144 | 84% | one consistent build |
| 16:53–17:17 (update #2) | 0 | 0% | launching had finished |

84% of the grid did run on a single build, so the numbers are not noise — but 13% were
launched into an active stack update, and the run as a whole mixes builds. **Do not
promote this to `baseline.json`, and do not cite its deltas as release evidence.**

Recorded rather than discarded because the failure pattern below is attributable and
useful, and because the contamination itself is the finding worth remembering: a
benchmark on a shared stack needs the stack quiesced, and the harness cannot currently
detect that it moved underneath a run.

## What is still attributable

**All 9 failures are `force-on`, and all 9 are one known defect.** Every one failed with:

```
ValidationException: The json schema definition at
toolConfig.tools.0.toolSpec.inputSchema is invalid ... $.$id: does not match the
uri-reference pattern
```

That is the defect fixed in PR #744 (`strip_non_wire_keywords`), which reached develop
at 15:17 — *after* the build these runs used was deployed at 13:25. `force-on` runs
launched at 13:03, against the earlier build, got past `$id`; those at 14:41+ did not.
Verified directly that the check is **not** model-dependent: both
`us.anthropic.claude-sonnet-5` and `us.anthropic.claude-sonnet-4-6` reject an
unstripped `$id`.

`extraction.forced_tool.enabled` is **false** by default, so these failures are confined
to an opt-in nobody runs. The 10 baseline-matched `core-*` cells had **0 failures on
both sides**.

## What the deltas looked like — indicative only

Reported so the numbers are not lost, explicitly **not** as a verdict.

| cell | recall | scalar accuracy | cost |
|---|---|---|---|
| core-tt-simple-sep | 1.000 → 1.000 | 0.833 → 1.000 | 0.112 → 0.117 |
| core-tt-simple-int | 1.000 → 1.000 | 1.000 → 1.000 | 0.171 → 0.182 |
| core-tt-simple-off | 1.000 → 1.000 | 0.833 → 1.000 | 0.105 → 0.110 |
| core-tt-adv-sep | 1.000 → 1.000 | 1.000 → 1.000 | 0.360 → 0.474 |
| core-tt-adv-int | 1.000 → 1.000 | 1.000 → 1.000 | 0.509 → 0.480 |
| core-tl-simple-sep | 1.000 → 1.000 | 0.833 → 1.000 | 0.087 → 0.096 |
| core-tl-adv-sep | 1.000 → 1.000 | 1.000 → 1.000 | 0.217 → 0.280 |
| core-bda-simple-sep | 1.000 → 1.000 | 0.833 → 1.000 | 0.110 → 0.116 |
| core-bda-adv-sep | 1.000 → 1.000 | 1.000 → 1.000 | 0.360 → 0.498 |
| core-llm-simple-sep | **0.564 → 0.958** | 1.000 → 0.889 | 0.088 → 0.093 |

`--compare` reported **0 cell-level regressions**, 1 cell-level improvement
(`core-llm-simple-sep` recall +0.393), and put every cost delta **within sampling
spread** — cost CV runs 0.55–0.57 at n=9, so nothing under roughly ±50% is resolvable
here.

Two patterns are worth re-testing on a clean stack rather than believed:

* **`scalar_accuracy` 0.833 → 1.000 on exactly the four simple/non-integrated cells.**
  One of two scalar fields went from wrong to right, consistently, in four independent
  cells. A plausible cause is #740 raising `ocr.image.dpi` 150 → 300: `base-ocr.yaml`
  records that below ~200 dpi Textract "silently omits small, faint or skewed
  characters — page numbers, box numbers, hand-filled values", and an account number is
  exactly that. **Untested.**
* **`core-llm-simple-sep` recall 0.564 → 0.958** — the Bedrock-LLM OCR backend, which
  reads the page image directly, would also benefit from the same DPI change. It moved
  the other way on `scalar_accuracy` (1.000 → 0.889). **Untested.**

## One caveat that is NOT a problem

The baseline's `pricing_sha256` differs from the current file, but the only *removal*
between them is retired `claude-3-5-haiku`; **no rate changed for any model this grid
uses** (sonnet-4-6, nova-lite, nova-2-lite, Textract). Verified by diffing
`config_library/pricing.yaml` across the two commits. Cost deltas are not a pricing
artifact.

The baseline was also measured on a *different stack* (`IDPRel066`), which the
release-cycle procedure in `.claude/skills/run-benchmarks.md` does not sanction — it
prescribes deploying the previous release and upgrading the **same** stack in place.

## To redo this properly

1. Use a stack nobody else is deploying to, or confirm no update is in flight and
   coordinate before starting.
2. Deploy the previous published release, run `corefast --native-upload`, promote that
   to `baseline.json`, upgrade the same stack in place, re-run, compare.
3. Raise repeats for any cost claim: at CV 0.55, n=9 cannot resolve ±50%.

Worth fixing in the harness: `run_matrix.py` reads the stack version once and could
re-check it per launch, aborting (or at minimum tagging affected runs) if the stack
moves mid-run. Nothing currently detects this.
