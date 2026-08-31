# Live Evaluation & Cost Analysis — GenAI IDP Accelerator

Use this when validating a change against **real deployed stacks**: benchmark
A/B tests (model, prompt, config), v0.5→v0.6-style upgrade tests, and
reading accuracy/cost/confidence from a run. This is the "prove it live"
workflow, complementary to unit tests (`testing-qa.md`).

> All AWS + `idp-cli` calls use `AWS_PROFILE=default`. Confirm the account first:
> `AWS_PROFILE=default aws sts get-caller-identity`. Never clobber the
> `Config#default` DynamoDB record on a stack you don't own.

> **PYTHONPATH pin (critical).** A stale sibling checkout (e.g.
> `/home/ec2-user/projects/idp2`) may be on PATH, so `idp-cli` can import an old
> `idp_common` that silently strips v0.6 config fields on upload/download.
> ALWAYS prefix config/eval commands with
> `PYTHONPATH=<repo>/lib/idp_common_pkg` and verify stored configs via DynamoDB
> (`_compressed_config`, gzip+b64), not by trusting `config-download`.

---

## 1. Running a benchmark test-set on a live stack

Test sets live in the stack's `...-testsetbucket-...` under
`<test-set>/input/` (docs) + `<test-set>/baseline/` (ground truth). The stack
auto-registers a test set the first time it's referenced. Config per test set
is a **config version** in the ConfigurationTable (`Config#<version>`); managed
configs (`ocr-benchmark`, `realkie-fcc-verified`, ...) are seeded at deploy and
**inherit the default model** unless they pin one.

### Preferred: the CLI (works on v0.6 / API-Gateway stacks)
```bash
PYTHONPATH=<repo>/lib/idp_common_pkg AWS_PROFILE=default idp-cli run-inference \
  --stack-name <stack> --test-set <test-set> --config-profile <version> \
  --number-of-files <N> --context "<label>" --region us-west-2
# returns testRunId like "OmniAI-OCR-Benchmark-20260704-004852"
```

### Gotcha: the CLI matches `<stack>-APIRESOLVE*` for the TestRunner/TestSetResolver Lambdas
That is the **v0.6** naming. A **v0.5.16** stack (pre-AppSync-removal) names them
`<stack>-APPSYNCSTAC*`, so the current CLI can't drive it (`TestRunnerFunction
not found`). For a v0.5.16 baseline, invoke the Lambda directly: find the
function whose name starts with `<stack>-APPSYNC` and contains
`TestRunnerFunction`, call `getTestSets` on the `...TestSetResolverFunction...`
first (registers the set), then invoke the runner with payload
`{"arguments":{"input":{"testSetId":<id>,"configVersion":<ver>,"numberOfFiles":N,"context":"..."}}}`.

### Uploading an A/B config version
Build variants by editing a **downloaded live config** (more robust than
hand-assembling dicts). Validate before upload:
```bash
PYTHONPATH=<repo>/lib/idp_common_pkg python3 -c "
import yaml; from idp_common.config.merge_utils import merge_config_with_defaults
merge_config_with_defaults(yaml.safe_load(open('variant.yaml')), validate=True)"
PYTHONPATH=<repo>/lib/idp_common_pkg AWS_PROFILE=default idp-cli config-upload \
  --stack-name <stack> --config-file variant.yaml --config-profile <name> \
  --version-description "A/B" --region us-west-2
# cleanup afterwards: idp-cli config-delete --config-profile <name> --force
```

---

## 2. Polling a run to completion (don't trust the metadata counter)

The `testrun#<id>` metadata row's `CompletedFiles` lags — it is reconciled only
when the results resolver is invoked. Poll the **per-doc rows** instead:
scan the TrackingTable (env `TRACKING_TABLE` on the runner Lambda, e.g.
`<stack>-TrackingTable-...`) for `PK begins_with "doc#<runId>"` and count
`ObjectStatus == "COMPLETED"` and `EvaluationStatus == "COMPLETED"`. When
`evalCompleted == filesCount`, the run is done even if metadata still says
`RUNNING`/`EVALUATING`.

Cross-check processing via the Distributed-Map state machine
`<stack>-PATTERNSTACK-*-DocumentProcessingWorkflow` (`list-executions`
SUCCEEDED/RUNNING/FAILED).

Results resolver: field `getTestRun` returns `overallAccuracy`, `totalCost`,
`averageConfidence` once status flips to `COMPLETE`; `getTestRunStatus` gives
live progress. **v0.6 resolvers enforce Cognito group auth** — a raw Lambda
invoke returns `Unauthorized: ... requires Admin or Author group`; pass an
`identity` with `{"groups":["Admin"]}` in the event, or compute metrics yourself
from S3 (next section).

---

## 3. Reading accuracy + cost from a run

**Accuracy (self-computed, resolver-free):** each doc writes
`s3://<outputbucket>/<runId>/<doc>/evaluation/results.json` with
`overall_metrics.weighted_overall_score` and per-section `attributes` (each has
`matched`, `score`, and a `failure_type` like `extraction_parsing_failed`).
Mean the weighted scores across docs; count `parse_failed` as a health signal.
Note **`weighted_overall_score` measures EXTRACTION** — it is unchanged by a
confidence/assessment-only change (assess A/B those via confidence
distribution, below).

**Cost:** per-doc `Metering` (TrackingTable `doc#` rows, or the section
`result.json` metadata) is a nested dict `service -> unit -> count`, e.g.
`Extraction/bedrock/<model> -> {inputTokens, outputTokens,
cacheReadInputTokens, cacheWriteInputTokens, requests}`. Convert to dollars with
`config_library/pricing.yaml` rates (`bedrock/<model>` → units → price).
- `cacheReadInputTokens` is ~10% of `inputTokens` price; `cacheWriteInputTokens`
  ~1.25×.
- **Output tokens usually dominate total cost (79–90% in assessment).** Optimize
  output verbosity / model choice before fussing over input caching.

**Confidence quality (for assessment A/Bs):** extraction accuracy won't move,
so compare the **leaf confidence distribution** from
`sections/<n>/result.json` → `explainability_info` (recurse to every
`{confidence, confidence_reason}` leaf). Track mean confidence and the
`% below threshold` (the alert / HITL rate). A model/prompt that becomes
*over-confident* (fewer alerts) is a calibration regression even if cost drops.

---

## 4. Upgrade test (vN → vN+1 in place)

Verifies a stack update + config auto-migration doesn't break existing
docs/configs. Recipe (as run for v0.5.16 → v0.6):
1. Deploy the **published/public** template to a NEW throwaway stack — this is
   the known baseline version (no `--from-code`):
   `idp-cli deploy --stack-name <baseline> --admin-email <you> --region us-west-2 --wait`.
   (Confirm the version: `curl -s <template-url> | grep -i "Description.*v0"`.)
2. Copy the test sets into the baseline's testset bucket
   (`aws s3 cp --recursive s3://<src-testset>/<set>/ s3://<baseline-testset>/<set>/`).
3. Process the docs + capture baseline accuracy/cost/confidence (sections 1–3).
4. **Update the same stack** to the new build
   (`idp-cli deploy --stack-name <baseline> --template-url <new> --wait`).
5. Verify migration: dump `Config#<version>` (`_compressed_config`) and check
   `config_format_version`, model fields, and that retired blocks are gone.
6. Reprocess the SAME docs + SAME config versions; compare **with one harness**
   (recompute the baseline through the same script so the numbers are
   apples-to-apples — resolver `overallAccuracy` and self-computed weighted
   score use different denominators).

---

## 5. Build/deploy gotchas that bite live testing

- **`publish` checksum cache skips layer rebuilds for data-only edits.** Editing
  a bundled YAML/JSON (e.g. `idp_common/config/system_defaults/*.yaml`) may NOT
  rebuild the `idp-common-base` layer on an incremental publish, so a deployed
  stack keeps the OLD default. Use `idp-cli publish --clean-build` for
  data-only changes, and verify the new layer:
  `unzip -p layers/idp-common-base-<hash>.zip python/idp_common/config/system_defaults/base-extraction.yaml | grep model:`.
- **`--source-dir` must be ABSOLUTE.** Background tasks reset cwd; a relative
  `.` fails with `VERSION file not found`.
- **Empty `units:` in `pricing.yaml` deadlocks stack update AND rollback.** The
  `UpdateDefaultConfig` custom resource re-validates `pricing.yaml` (read from
  `s3://<config-bucket>/config_library/pricing.yaml`) in both directions; a
  Pydantic failure (`units: Input should be a valid list`) can leave the nested
  PATTERNSTACK in `UPDATE_ROLLBACK_FAILED`. Recovery: upload a corrected
  `pricing.yaml` to that S3 key, then
  `aws cloudformation continue-update-rollback --stack-name <PARENT>` (no
  resource-skips; child stacks reject direct rollback).

---

## 6. Model & caching facts (confidence/assessment cost)

- **Bedrock prompt caching is prefix-match from token 0, per-model.** A later
  request reads cache only if byte-identical up to a `<<CACHEPOINT>>`, same
  model, within ~5-min TTL. Put shared content (instructions, image, OCR) BEFORE
  the cachepoint and the varying tail (extraction results) after.
- **Anthropic models won't cache a prefix under ~2048 tokens; Amazon Nova caches
  at a much lower floor.** A short (~450-token) static instruction block never
  caches on Haiku/Sonnet but does on Nova.
- **Confidence/assessment default is Nova Lite (`us.amazon.nova-lite-v1:0`)** —
  measured ~130× cheaper than Sonnet 5 for the assessment pass (higher rate +
  ~2× verbose output), and it reads cache cleanly. **Nova 2 Lite is worse for
  assessment** (caches far fewer image tokens, re-writes instead of reading).
  Extraction default is Sonnet 5 (a quality choice).
- **Reasoning models (Sonnet 5, Sonnet/Opus 4.6+) emit `reasoningContent`
  block(s) before the answer `text`** — parsers must concatenate all `text`
  blocks, not read `content[0]`, or extraction/classification/assessment return
  empty. (Fixed repo-wide; watch for it in new parse sites.)
