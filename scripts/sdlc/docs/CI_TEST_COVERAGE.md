# CI/CD Automated Test Coverage

## Overview

The CI/CD pipeline runs a comprehensive smoke test suite that validates all major IDP Accelerator features. Tests run in **parallel** with **fail-fast** behavior for rapid feedback.

## Running these tests manually (make target → skill)

Every test below can be run **outside** the pipeline against a live stack (dev
box, `AWS_PROFILE=default` or `idp-ci`). The deploy-variant probes run manually
by default (they are no longer automatic in CI — see the ⚠️ note under
"deployment-variant probe framework"). This table is the map from each test to
its `make` target and the skill that documents how to run it.

| Test / suite | Make target | Skill |
|---|---|---|
| Primary functional suite (Steps 3–14) | *(runs in CI; deploy a stack then use the individual targets below)* | — |
| API RBAC / authorization (Step 12) | `make api-test STACK_NAME=…` (alias `make stacktest-rbac`) · static-only: `make api-test-static` | `.claude/skills/api-rbac-test.md` |
| ZAP DAST scan | `make stacktest-zap STACK_NAME=…` | `.claude/skills/run-stack-tests.md` |
| APIGateway GLOBAL hosting | `make stacktest-hosting-global` | `.claude/skills/run-stack-tests.md` |
| WAF-enabled hosting | `make stacktest-waf` | `.claude/skills/run-stack-tests.md` |
| APIGateway PRIVATE (VPC) hosting | `make stacktest-hosting-private VPC_ID=…` | `.claude/skills/run-stack-tests.md` |
| Jobs API (VPC) | `make stacktest-jobsapi VPC_ID=…` | `.claude/skills/run-stack-tests.md` |
| List deploy-variant stack-tests | `make stacktest-list` | `.claude/skills/run-stack-tests.md` |
| Release-vs-release benchmark | `make benchmark-release …` (alias `make stacktest-benchmark`) | `.claude/skills/run-benchmarks.md` |
| In-place upgrade (X→Y) test | `make stacktest-upgrade` (pointer) | `.claude/skills/test-upgrade.md` |
| Full offline test battery (no AWS) | `make test` | `.claude/skills/full-test-battery.md` |
| Package/Lambda offline suites (idp_cli, idp_sdk, idp_feature_sdk, feature-platform, seller-entitlement-service) | `make test-packages-cicd` | — *(runs per commit in the GitHub `developer-tests` workflow)* |
| Seller-service ownership preflight (read-only) | `make seller-entitlement-service-preflight PRODUCT_REGISTRY='{…}'` | `feature-platform/seller-entitlement-service/README.md` |
| Seller-service **e2e** (deploy → live probe → teardown) | `make stacktest-seller` | `.claude/skills/run-stack-tests.md` |
| Seller-service live activation + payload probe | `python feature-platform/seller-entitlement-service/tests/dynamic_activation_test.py --endpoint … --product-id …` | `feature-platform/seller-entitlement-service/README.md` |
| Seller-service test-stack teardown (incl. retained KMS key + table) | `feature-platform/seller-entitlement-service/tests/teardown_test_stack.sh --stack-name …-citest` | `feature-platform/seller-entitlement-service/README.md` |
| Run security tests + curate a public-safe snapshot | `make security-results [STACK_NAME=… REGION=…]` (offline-only if no stack) | `.claude/skills/curate-security-results.md` |

Once a release has been validated with these tiers, the outcome is recorded — one
file per release, never overwritten — in
[`docs/release-validation/`](../../../docs/release-validation/README.md). That is
the answer to "what was actually run against a live stack for release X, and what
did it find?", since none of these tiers appear in a pipeline log.

VPC stack-tests auto-discover a suitable VPC via the `run-stack-tests` skill
(it lists candidates, confirms with you, then passes `VPC_ID`/`SUBNET_IDS`/
`LAMBDA_SG_ID`/`APIGW_VPCE_ID` as make params).

## Security tests: coverage & auditable results

The four security tests — **SRT** (SAST/deps), **ZAP DAST** (dynamic API scan),
and **RBAC static + dynamic** (authorization) — are documented as a set, with
their goals and threat-model cross-references, in
[`security/README.md`](../../../security/README.md). For a release, run them and
curate a **public-safe, redacted** snapshot into `security/test-results/<version>/`
(one file per test + a `MANIFEST.md` tying the results to a version, git SHA,
and date) with a single command:

```bash
make security-results STACK_NAME=<stack> REGION=<region>   # full (incl. live ZAP + RBAC)
make security-results                                      # offline-only (SRT + RBAC static)
```

(Or ask Claude Code: *"run security tests and update results"*. To curate from
already-run reports without re-running:
`python3 scripts/security/curate_results.py --date <YYYY-MM-DD> [--version <label>]`.)

Raw reports carry environment-specific identifiers (account IDs, Cognito pool
IDs, API hostnames) and stay in gitignored `scratch/`/`.srt/` — only the curated
summaries are committed. See
[`.claude/skills/curate-security-results.md`](../../../.claude/skills/curate-security-results.md)
for the runbook and [`security/test-results/README.md`](../../../security/test-results/README.md)
for the process.

### Additionally: Seller Entitlement Service tests

The **Seller Entitlement Service** carries a fifth, separate set — template-security
assertions, a payload-robustness fuzz corpus, and a live activation probe — because
it deploys into an AWS Marketplace *seller* account rather than a customer's, and its
protected assets belong to the seller. Threats `SELL.T01–T10`.

The first two run **offline per commit** via `make test-packages-cicd` (called by the
GitHub `developer-tests` workflow); the live probe is manual, because issuing a real
token needs product ownership *and* a subscribed buyer account, neither of which a
build account has. They are **not** in the curated `test-results/` snapshot.

**ZAP DAST deliberately does not apply to this service.** ZAP cannot SigV4-sign, so
the `AWS_IAM` authorizer refuses every request *before the Lambda is invoked* — an
active scan would report a clean 403 wall and prove nothing about the parser. The
payload-robustness corpus covers that goal directly. See
[`security/README.md`](../../../security/README.md) and
[the service README](../../../feature-platform/seller-entitlement-service/README.md).

## Pipeline stages & triggers

The GitLab pipeline has three stages, gated so cheap checks run everywhere and
the expensive AWS deploy runs only when it's worth it:

| Stage | Jobs | AWS? | Cost |
|-------|------|------|------|
| **fast_checks** | `code_checks` (lint, typecheck, static RBAC scan, all unit suites, UI vitest) **and** `srt_security_review` (SRT security scan) — run in **parallel** | No | ~minutes |
| **deployment_validation** | IAM service-role permission pre-check | Yes (read-only) | seconds |
| **integration_tests** | Full stack deploy + primary suite (Steps 1–13) on the **primary shared stack only**. The deployment-variant probes no longer run here by default — see the ⚠️ note under "deployment-variant probe framework" (run them manually with `make stacktest-*`, or set `IDP_RUN_PROBES=true`). | Yes (deploys) | ~1 hour |

**Trigger matrix** — what runs, when:

| Event | fast_checks (code + SRT) | deployment_validation | integration_tests |
|-------|:---:|:---:|:---:|
| Push to any branch, **no MR** | ✅ | — | — |
| Push to branch with a **Draft** MR → `develop` | ✅ | ✅¹ | ▶️ **manual** (button on MR) |
| Push to branch with a **non-Draft** MR → `develop` | ✅ | ✅¹ | ✅ auto¹ |
| Push to **`develop`** | ✅ | ✅¹ | ✅ auto¹ |

¹ **Doc-only commits skip the deploy stages.** `deployment_validation` and the
auto `integration_tests` only run when the commit/MR touches a **deploy-affecting
path** (the `.deploy_affecting_changes` allowlist in `.gitlab-ci.yml`:
`template.yaml`, `publish.py`, `requirements*.txt`, `patterns/`, `nested/`,
`src/`, `lib/`, `config_library/`, `feature-platform/`, `iam-roles/`, `scripts/`,
`.gitlab-ci.yml`). A commit that changes only `VERSION`, `CHANGELOG.md`,
`**/*.md`, `docs/`, `images/`, etc. skips the ~1h deploy entirely. The allowlist
is deliberately generous (a false "run" wastes CI minutes; a false "skip" could
merge a broken deploy). The **Draft-MR manual button ignores this filter** — you
can always force a deploy by clicking it, even on a doc-only branch.

Notes:
- **Every push runs fast_checks** (code checks + SRT), so lint/typecheck/unit and
  security feedback is immediate on any branch — **including doc-only commits**
  (fast_checks has no changes: filter). GitLab emails the committer on failure.
- The **~1h integration deploy runs only** on `develop` and on **non-Draft** MRs
  targeting `develop` — **and only when deploy-affecting files changed** (see ¹).
  On a **Draft** MR it's a **manual play button** on the MR page — run it on
  demand, not on every WIP push.
- A `workflow:` rule prevents **duplicate** branch+MR pipelines (a branch with an
  open MR runs only the MR pipeline).
- integration_tests uses a **per-branch** `resource_group`
  (`integration_deploy_$CI_COMMIT_REF_SLUG`) + `interruptible`, so rapid pushes
  to the *same* MR still serialize (a newer run supersedes an older queued one),
  while *different* MRs deploy concurrently. Concurrency is bounded by the number
  of active MRs (typically ≤5) — not a hard cap. If concurrent deploys ever
  exhaust account quotas, revert to a single shared `resource_group:
  integration_deploy`.
- **Auto-cancel is disabled on `develop`** (`workflow` rule with
  `auto_cancel: on_new_commit: none`). Previously, *any* new push to develop —
  including a doc-only commit whose pipeline skips the deploy stages — would
  auto-cancel the in-flight ~1h integration deploy of the prior commit, and
  that deploy was never re-run (the merge went permanently untested). Now every
  develop pipeline runs to completion; back-to-back deploys serialize via the
  resource_group rather than superseding each other. MR pipelines keep the
  default supersede-on-push behavior (there, `changes:` compares against the
  target branch, so a doc push to an MR that still touches deploy-affecting
  files re-runs the deploy anyway).

## Test Execution Strategy

### Parallel Execution (Steps 3-11, 13, 14)
- **11 tests run concurrently** to minimize pipeline runtime (Step 13, the
  read-only permissions-boundary check, and Step 14, the pipeline-hook test,
  join the parallel pool)
- **Fail-fast enabled**: If any test fails, remaining tests are cancelled and cleanup begins
- **Expected runtime**: ~25-35 minutes (vs 60+ minutes sequential)

### Sequential Execution (Step 12)
- **Step 12 (API RBAC + security-focused suites) runs alone after the parallel
  pool drains**
  - **Reason**: Its dynamic harness temporarily flips `ADMIN_USER_PASSWORD_AUTH`
    on the shared UI app client (a stack-wide auth mutation) and restores it, and
    its logout suite performs a global sign-out — interleaving with API-hitting
    parallel tests would corrupt them.
  - **Coverage**: beyond the role permission matrix, Step 12 runs the mandatory
    AppSec API security checklist — IDOR (2.1), token expiry/logout (2.3/2.4),
    deleted-resource (2.5), input validation (3), and TLS (4). See the mapping
    table in `.claude/skills/api-rbac-test.md`. Input validation runs in
    **tolerant** mode here (a 5xx on malformed input is WARNed, not failed) until
    central schema validation lands; set `IDP_SECTEST_STRICT_INPUT=true` to gate
    on a clean 4xx.

### Concurrent deployment-variant probes (own stacks)
- The **deployment-variant probe framework** (below) deploys SECOND,
  independent stacks — one per probe. Five probes run by default (GLOBAL APIGW,
  WAF, PRIVATE APIGW, Jobs API, ZAP DAST), **concurrently with the primary suite
  AND with each other** on their own threads, so their ~30-min deploys overlap the
  primary deploy instead of running back-to-back. Each opts out of the primary
  suite's fail-fast abort machinery, so a primary failure never kills a probe's
  in-flight deploy. VPC-requiring probes share one persistent pipeline-owned
  test VPC (so VPCs don't bound concurrency); fan-out is capped by
  `IDP_PROBE_MAX_CONCURRENCY` (default 8) to bound simultaneous stack/IAM usage.
- **Launch stagger + transient-race retry (AWS eventual-consistency resilience).**
  Standing up the primary + N probe stacks at once bursts hundreds of
  `CreateLogGroup`/`CreateProject` calls; two AWS control-plane races can then
  surface as `CREATE_FAILED` and roll a stack back: CloudWatch Logs
  "The specified log group does not exist" (CreateLogGroup→PutRetentionPolicy
  consistency gap) and CodeBuild "not authorized to perform: sts:AssumeRole on
  service role" (IAM trust-policy propagation lagging the role's `CREATE_COMPLETE`
  — not fixable by `DependsOn`, the ordering is already correct). Two mitigations:
  (1) probe launches are **staggered** `IDP_PROBE_LAUNCH_STAGGER_SECS` (default
  10s) × index to flatten the burst and avoid the race up front (cheaper than a
  rollback); (2) a **one-shot fresh-stack retry** (`_is_transient_deploy_race`,
  scoped tightly to those two resource+message signatures so genuine config
  errors still surface) backstops both the primary deploy and each probe. A
  THIRD burst symptom is IAM throttling at Step 0 — `iam:CreatePolicy`
  "Throttling: Rate exceeded (reached max retries: 4)" while creating each
  stack's permissions boundary — which fails BEFORE any deploy (so the
  fresh-stack retry can't catch it); the IAM/CFN clients in
  `create_iam_resources`/`cleanup_iam_resources` therefore use **boto3 adaptive
  retry** (`max_attempts: 10`) to ride through the account-wide IAM rate limit.
  See `scratch/aws-service-issue-cfn-eventual-consistency.md` for the AWS
  service-issue evidence package.

## Test Coverage

### Step 1: Stack Deployment
**What it tests**: CloudFormation stack deployment
- Template validation
- Nested stack creation (AppSync, Pattern, DocumentKB, MultiDocDiscovery)
- Resource creation and initialization
- Stack outputs verification

**Duration**: ~15-20 minutes

---

### Step 2: Stack Health Check
**What it tests**: Stack readiness
- All nested stacks in `CREATE_COMPLETE` or `UPDATE_COMPLETE` status
- Critical resources accessible
- No rollback or failed states

**Duration**: <1 minute

---

### Step 3: Default Config Test (Pipeline Mode) ⚡ *Parallel*
**What it tests**: Default pipeline configuration
- Document processing with default config
- Amazon Textract OCR
- Bedrock classification (page-level)
- Bedrock extraction (traditional LLM-based)
- **Verification**:
  - Extraction fields present in output
  - Classification results exist
  - Document status = `COMPLETED`

**Test Document**: `samples/lending_package.pdf`  
**Duration**: ~5-7 minutes

---

### Step 4: BDA Mode Test ⚡ *Parallel*
**What it tests**: Bedrock Data Automation end-to-end processing
- BDA config upload and sync (without activation)
- BDA blueprint creation via `config-sync-bda`
- Packet/media document processing using `--config-version`
- Integrated OCR + classification + extraction via BDA
- **Verification**:
  - BDA output structure
  - Document processing completion
  - Results match expected format

**Test Document**: `samples/lending_package.pdf`  
**Duration**: ~6-8 minutes  
**Implementation**: Uses `idp-cli config-sync-bda` to create BDA project/blueprints, then runs inference with `--config-version` parameter (no activation needed)

---

### Step 5: Rule Validation Test ⚡ *Parallel*
**What it tests**: Business rule validation engine
- Rule execution on extracted data
- Rule statistics (passed/failed/skipped counts)
- **Verification**:
  - Rule validation results present
  - Statistics calculated correctly
  - Rules applied to extracted fields

**Test Document**: `samples/lending_package.pdf`  
**Duration**: ~5-7 minutes

---

### Step 6: Multi-Document Concurrent Batch Processing ⚡ *Parallel*
**What it tests**: Concurrent document processing at scale
- Multiple documents processed simultaneously
- Concurrency management (DynamoDB counter)
- Batch tracking
- **Verification**:
  - All documents complete successfully
  - No concurrency conflicts
  - Tracking table updated correctly

**Test Documents**: Multiple files from `samples/` directory  
**Duration**: ~6-8 minutes

---

### Step 7: Test Studio Evaluation ⚡ *Parallel*
**What it tests**: Test Studio evaluation workflow
- Test set processing (limited to 3 documents)
- Evaluation trigger via `idp-cli test-result`
- Metrics calculation (accuracy, precision, recall, F1)
- Cost tracking
- **Verification**:
  - Test run completes successfully
  - Evaluation metrics calculated
  - Overall accuracy > 30% threshold
  - Results retrievable via CLI

**Test Set**: `fake-w2` or `realkie-fcc-verified`  
**Duration**: ~8-10 minutes  
**Implementation**: Uses `idp-cli test-result --wait` command to trigger evaluation

**Architecture**: 
- Calls `getTestRunStatus` Lambda repeatedly (triggers SQS evaluation on first call)
- Polls until status changes from `EVALUATING` to `COMPLETE`
- Retrieves full results with `getTestRun` Lambda
- See [Test Studio Architecture](#test-studio-architecture) below

---

### Step 8: Agentic Extraction with Large Table ⚡ *Parallel*
**What it tests**: Agentic extraction with deterministic table parsing
- Agent-based extraction (Strands framework)
- Deterministic Markdown table parser
- Large table handling (532 fund items)
- OCR artifact recovery (empty lines, missing pipes)
- **Verification**:
  - All 532 fund items extracted
  - Table structure preserved
  - No data loss from OCR artifacts
  - Agent tool usage logged

**Test Document**: `samples/Nuveen.pdf`  
**Config**: `agentic-nuveen` (enables agentic mode + table parsing)  
**Duration**: ~9-11 minutes

---

### Step 9: Single-Document Discovery ⚡ *Parallel*
**What it tests**: Single-document schema discovery
- Dynamic schema generation
- Knowledge Base creation and ingestion
- Bedrock agent invocation
- **Verification**:
  - Discovery workflow completes
  - Knowledge Base ingestion triggered
  - Schema generated successfully

**Test Document**: Single sample document  
**Duration**: ~5-7 minutes  
**Cleanup**: Ingestion jobs cancelled before stack deletion

---

### Step 10: Multi-Document Discovery ⚡ *Parallel*
**What it tests**: Multi-document schema discovery
- Batch schema discovery
- Knowledge Base multi-file ingestion
- Consolidated schema generation
- **Verification**:
  - All documents processed
  - Knowledge Base ingestion triggered
  - Consolidated schema accurate

**Test Documents**: Multiple sample documents  
**Duration**: ~6-8 minutes  
**Cleanup**: Ingestion jobs cancelled before stack deletion

---

### Step 11: Test Compare ⚡ *Parallel*
**What it tests**: Test comparison CLI command
- Multiple test run execution
- Test result comparison via `idp-cli test-compare`
- Comparison output formatting
- **Verification**:
  - Two test runs complete successfully
  - Comparison output contains expected fields (Test Run ID, Accuracy, Precision, Recall, F1 Score)
  - Side-by-side metrics display works

**Test Set**: `fake-w2` or `realkie-fcc-verified`  
**Duration**: ~10-12 minutes (2 test runs + comparison)  
**Execution**: Runs in the parallel pool (only runs inferences, no shared-stack
mutation — safe to interleave).  
**Implementation**: 
- Runs 2 test inferences (2 documents each)
- Waits for both to complete and evaluate
- Calls `idp-cli test-compare` to compare results

---

### Step 13: IAM Permissions Boundary Attachment ⚡ *Parallel*
**What it tests**: that the deployed IAM roles actually carry the permissions
boundary the stack was deployed with. The primary suite deploys with a non-empty
`PermissionsBoundaryArn`, so the template's `HasPermissionsBoundary` condition
should attach that boundary to every `AWS::IAM::Role` it creates.
- **Why it exists**: this is the only place boundary attachment is verified
  end-to-end. The deploy-variant stack-tests now deploy *without* a boundary
  (to avoid a per-stack `iam:CreatePolicy`/`DeletePolicy` in any concurrent
  burst), so if the primary suite didn't check it, a template change that dropped
  the boundary from a role would ship silently.
- **Verification**:
  - Collects `AWS::IAM::Role` physical ids from the stack + its nested stacks
  - Samples up to 25 roles and asserts each has a `PermissionsBoundary` attached
  - Fails if any sampled role is missing its boundary

**Duration**: seconds (read-only `cloudformation:ListStackResources` + `iam:GetRole`)  
**Execution**: Runs in the parallel pool (read-only — no shared-stack mutation).  
**Implementation**: `test_step13_permission_boundaries` in
`scripts/sdlc/codebuild_deployment.py`.

---

### Step 14: Pipeline Hooks End-to-End ⚡ *Parallel*
**What it tests**: the [pipeline-hook](../../../docs/feature-platform.md#pipeline-hooks)
mechanism — the platform's supported way for a feature or an admin to inject
business logic into document processing — at both standalone hook points
(`preprocessing` and `postprocessing`).
- **Why it exists**: before this step, **none of the seven hook points had any
  end-to-end coverage.** Unit tests exercise the dispatcher with fakes, but four
  things only exist on a real stack: the dispatcher reading a hook out of a real
  config version, `UpdateSchemaConfig` publishing the hook config sections (so
  the fields an admin edits exist on the deployed stack), a hook Lambda clearing
  the dispatcher's tag/name IAM condition, and a hook's `updatedDocument`
  surviving into the **persisted** document. The gap mattered because a hook that
  silently never fires is indistinguishable from one that ran and did nothing —
  the workflow still succeeds and the document still reaches `COMPLETED`. That
  exact failure mode shipped once already (#599).
- **Verification**:
  - The hook sections survive the config write/read round-trip (they are dropped
    if a config-model field is missing — the regression the offline config-model
    tests guard)
  - `HookResults.<point>.Payload.invoked >= 1` for **both** points, read from the
    Step Functions execution history, plus the `configVersion` the dispatcher
    resolved — the only assertion that distinguishes "ran and decided nothing"
    from "never invoked"
  - `documentUpdatedBy` is non-empty, i.e. the hook's `updatedDocument` was
    **accepted** rather than refused by the dispatcher's guardrails
  - the hook's marker is present in the **persisted** document (tracking row),
    proving the mutation survived past the workflow

**Test Document**: `samples/lending_package.pdf`
**Duration**: ~5-7 minutes
**Execution**: Runs in the parallel pool. It registers its hook in its **own**
config version (`test-pipeline-hooks`) and never activates it, so the other steps
sharing this stack are unaffected.
**Implementation**: `test_step14_pipeline_hooks` in
`scripts/sdlc/codebuild_deployment.py`. Deploys a real `idp-citest-hook-*` Lambda
built from `idp_common.hooks` — the documented helper pair — so the test also
exercises the contract we publish to feature authors. **Teardown runs in a
`finally` and is not optional**: a leftover hook ARN pointing at a deleted Lambda
is the stale-ARN state that fails every subsequent document at a flat hook point.

**Resource naming satisfies two IAM policies at once**, which is why it is not
simply `GENAIIDP-*`: the CI CodeBuild role scopes `iam:*` to `role/idp-*` and
`lambda:*` to `function:idp-*` (so a `GENAIIDP-` prefixed role or function is
AccessDenied — the step's first pipeline failure), while the host dispatcher only
invokes a hook that is *either* named `GENAIIDP-*` *or* carries the
`idp:feature-id` tag. The resources are therefore named `idp-*` and the
dispatcher is satisfied via the **tag** path. That is also the more
representative test: tagging is how installed Feature Platform features clear the
check, while the `GENAIIDP-*` name is the admin escape hatch. The step applies
the tag unconditionally (not just via `create_function`, whose `Tags` are skipped
on the reuse-an-existing-function path) and verifies it before proceeding.

**Known asymmetry**: at `preprocessing` the document has no sections yet (OCR and
classification have not run), so that point's marker can only land in the
document-level backstop field — which `postprocessing`, running later, overwrites.
The persisted-marker assertion therefore checks the **postprocessing** marker,
which is correctly the final value; `preprocessing` is proven to have run by its
own `invoked` count.

#### Validating Step 14 without a pipeline round-trip

A pipeline round-trip is ~70 minutes, so debug the hook **outside** it. Deploy the
zip the step builds to a throwaway Lambda and invoke it with a synthetic
dispatcher event pointing at a **real** compressed document from a live stack's
working bucket:

```python
# build the exact zip the step would upload
import importlib.util, sys
spec = importlib.util.spec_from_file_location("cb", "scripts/sdlc/codebuild_deployment.py")
cb = importlib.util.module_from_spec(spec); spec.loader.exec_module(cb)
cb._build_hook_zip("/tmp/hook.zip")
```

Then `aws lambda create-function` it with `MARKER_KEY` + `WORKING_BUCKET`, tagged
`idp:feature-id=<anything>`, and invoke with:

```json
{"hookPoint": "postprocessing", "document": {"compressed": true,
 "s3_uri": "s3://<working-bucket>/compressed_documents/<doc>/<...>_evaluation_state.json",
 "document_id": "<doc>", "num_pages": 1, "sections": ["1"]},
 "args": [{"key": "note", "value": "manual"}]}
```

This is how the missing **PyYAML** dependency was found: the zip imported fine
locally but died at Lambda cold start with `ModuleNotFoundError: No module named
'yaml'`, because `load_hook_document -> Document.decompress ->
idp_common.utils -> idp_common.config.models -> configuration_manager` imports it.
Iterating this way is ~1 minute per attempt instead of ~70. Remember to delete
the function and role afterwards.

Two gotchas the same technique surfaced, worth knowing before writing any hook:

- `Document.metadata` is dropped by `to_dict()`, and the tracking row stores only
  a reduced section view (`Id`/`Class`/`PageIds`/`OutputJSONUri`) — so a mutation
  written to either is invisible to a persistence assertion. `summary_report_uri`
  IS persisted (as `SummaryReportUri`).
- `boto3`/`botocore` must NOT be vendored into a hook zip: the Lambda runtime
  supplies them, and including them pushed the package from 4.4MB to 62MB, past
  the 50MB direct-upload limit.

---

## Additional Deployment Tests: the deployment-variant probe framework

> **⚠️ Default OFF in CI (changed 2026-07).** These probes no longer run
> automatically in the integration pipeline. Standing up the primary + 5 probe
> stacks *at once* burst the account-wide AWS control planes — CloudWatch Logs
> create-consistency, CodeBuild role-trust propagation, and the IAM
> `CreatePolicy` rate limit — producing flaky pipeline failures unrelated to the
> code under test. Since these are infra-variant *deploy smoke tests* that rarely
> change, they now run **manually, one stack at a time**, via
> `make stacktest-*` (see `.claude/skills/run-stack-tests.md` and
> `scripts/sdlc/run_stacktest.py`). The CI pipeline runs the **primary shared
> stack only** (Steps 3–14). Set `IDP_RUN_PROBES=true` to re-enable them in a pipeline
> run. When they DO run (manual sweep or opt-in), launches are staggered
> (`IDP_PROBE_LAUNCH_STAGGER_SECS`, default 120s) and each deploys **without** a
> permissions boundary (only the primary suite creates + tests one — Step 13).

Separate from the shared-stack suite above (Steps 3–14, which run against ONE
stack deployed with default hosting — CloudFront), a **deployment-variant probe
framework** validates alternative deployment permutations, each on its **own
throwaway IDP stack**. When enabled the probes run **concurrently** with the
shared-stack suite *and with each other* (overlapping the ~30-min deploys) and
each tears its stack down afterward.

Each probe is a self-contained *deploy-a-config-variant + smoke-check-its-
distinguishing-feature* unit. The framework is a table of
`Probe(name, stack_suffix, deploy_params, validate_fn, requires_vpc)` rows that
a concurrent launcher iterates — adding a new permutation is **one table row +
a validator**, not a copy-pasted deploy/validate/cleanup function.

> **Scope (important):** probes are **deploy + feature-smoke only, NOT full
> functional coverage.** A variant can deploy clean yet still have a
> doc-processing regression that only the shared-stack suite (Steps 3–14) would
> catch. Don't read a green probe as "this variant processes documents
> correctly" — only as "this variant deploys and its distinguishing feature
> responds."

### The default probes (all run every pipeline)

| Probe | `stack_suffix` | Distinguishing params | VPC? | Validator asserts |
|-------|----------------|-----------------------|------|-------------------|
| **APIGateway hosting (GLOBAL)** | `apigw` | `WebUIHosting=APIGateway`, `ApiGatewayVisibility=GLOBAL` | no | REST API is **REGIONAL**, `ApplicationWebURL` is the execute-api `/api` URL, **HTTP GET → 200** (internet-reachable, so a real end-to-end UI fetch) |
| **WAF-enabled (IP allow-list)** | `waf` | `WAFAllowedIPv4Ranges` set (+ APIGateway/GLOBAL hosting to have a stage) | no | REGIONAL WebACL `{stack}-api-acl` **exists and is associated** with an API-Gateway stage |
| **APIGateway hosting (PRIVATE)** | `apigwpriv` | `WebUIHosting=APIGateway`, `ApiGatewayVisibility=PRIVATE` | yes | REST API endpoint type is **PRIVATE** and carries a **resource policy** (VPC-only → structural check; CodeBuild can't fetch a private endpoint) |
| **Jobs API** | `jobsapi` | `EnableJobsApi=true` | yes | stack exposes the **`ApiGatewayEndpoint`** output and its REST API exists (private → structural check) |
| **ZAP DAST scan** | `zapdast` | *(default hosting)* | no | authenticated **OWASP ZAP** dynamic scan of the deployed UI API — injection/headers/TLS/info-leak classes; **WARN-only** (reports, does not gate yet) |

> **Note:** the Jobs API probe exercises the *additive* `EnableJobsApi` CFN
> parameter on the STANDARD published template — it does **not** exercise the
> `idp-cli deploy --headless` template transform (which removes the UI). (It was
> itself once *named* the "headless" probe, which is why leftover `-headless`
> stack names appear in the reaper fixtures; it was renamed `jobsapi` precisely
> to stop implying transform coverage.) The `--headless` transform is now gated
> at two tiers: **offline** unit tests in `lib/idp_sdk/tests/unit/`
> (dangling-parameter + zero-`Error` `cfn-lint`), and **`validate_headless_
> template`** in the integration tier, which transforms the *packaged*
> `.aws-sam/idp-main.yaml` and validates it through real CloudFormation
> `ValidateTemplate` (one S3 put + one API call — no second SAM build, no
> deploy). A `--headless` *deploy* probe remains a follow-up: these gates prove
> the template is well-formed, not that a headless stack stands up.

Validators live in `scripts/sdlc/codebuild_deployment.py`:
`validate_apigw_global_hosting`, `validate_waf_enabled`,
`validate_apigw_private_hosting`, `validate_jobs_api`,
`validate_zap_dast`.

#### ZAP DAST probe (dynamic security testing)

The `zapdast` probe adds the one class of coverage neither SRT (static code) nor
the RBAC harness (authorization semantics) provides: **dynamic** application
security testing of the *running* UI REST API — reflected/persistent XSS, SQL/OS/
code injection, missing security headers (CSP, HSTS, X-Frame-Options,
X-Content-Type-Options), cookie flags, TLS issues, and information disclosure.

It runs the official **OWASP ZAP** Docker image (`ghcr.io/zaproxy/zaproxy`)
via `zap-api-scan.py` — which requires **`PrivilegedMode: true`** on the
`app-sdlc` CodeBuild project (`scripts/sdlc/cfn/codepipeline-s3.yml`; live once
that SDLC pipeline stack is deployed). **Docker-in-CodeBuild note:** the bind
mount (`docker run -v {workdir}:/zap/wrk`) works fine from `/tmp`, but CodeBuild
runs the build as **root** while the ZAP image runs `zap-api-scan.py` as the
non-root **`zap`** user — which can read the seeded spec but **cannot write the
report** into a root-owned workdir (`PermissionError: /zap/wrk/zap-report.html`,
which surfaces as no report). The probe therefore **`chmod 0o777`s the workdir**
so the container can write its report (proven end-to-end in a real CodeBuild
project). The probe is also defensive: a `docker info` preflight runs first, and
if the daemon is genuinely unavailable (or the scan produces no report) it
records a **SKIP (pass)** rather than failing the build — mirroring how
VPC-requiring probes skip
when their infra is absent. Because
the UI API is a single Cognito-gated route `POST /op/{field}` with **no OpenAPI
spec**, ZAP's spider finds nothing to crawl, so the probe **seeds** the scan
with a minimal OpenAPI doc generated from `scripts/api_rbac_expectations.yaml`
(the same op source-of-truth the RBAC tests use — one authenticated request per
operation). Authentication reuses `scripts/rbac_common.py` (shared with
`test_api_rbac.py`): it mints a Cognito **ID token** and injects it on every
request via ZAP's `replacer`; the temporary `ADMIN_USER_PASSWORD_AUTH` app-client
flip is safe here because it happens on the probe's **own** throwaway stack, and
is always restored.

- **Passive baseline** (spider + passive rules, no attack payloads) runs every
  time. The **active scan** (real injection payloads) is opt-in via
  **`IDP_ZAP_ACTIVE=true`** — run it on demand/nightly, not on every MR, and only
  ever against these throwaway probe stacks.
- **WARN-only today:** `validate_zap_dast` reports alert counts (in the
  consolidated summary) and uploads the full HTML/JSON report to
  `s3://<sourcecode-bucket>/deploy/zap/…`, but never fails the build. Rule
  actions live in `scripts/sdlc/zap-rules.conf` (IGNORE/WARN/FAIL). Promote
  high-confidence rules to FAIL and flip the `# TODO promote` gate in
  `validate_zap_dast` once the baseline is triaged — the same maturity path SRT
  took.
- **Gating flag:** set **`IDP_TEST_ZAP=false`** to skip only this probe (the
  other probes still run).

**Lifecycle** (every probe): creates per-stack IAM/boundary → (for `requires_vpc`
probes) injects the persistent-test-VPC params → deploys with the probe's extra
CFN params → validates → captures CF failure events before teardown →
**always** tears down the IDP stack (in a `finally`). Each probe runs on its own
thread and opts that thread out of the shared suite's fail-fast abort machinery
(`_thread_local.never_abort`), so a shared-suite failure's kill sweep can never
terminate a probe's in-flight deploy, and one probe failing never affects the
others or the already-completed shared-suite result.

### The persistent test VPC (why VPC probes are now quota-safe)

VPC-requiring probes (PRIVATE hosting, Jobs API) no longer stand up a throwaway
VPC per run. A **single persistent test VPC is owned by the pipeline
CloudFormation stack** (`scripts/sdlc/cfn/codepipeline-s3.yml`, parameter
`CreateTestVpc`, default `true`) and reused by every run. Its ids are handed to
CodeBuild as env vars (`IDP_TEST_VPC_ID`, `IDP_TEST_PRIVATE_SUBNET_IDS`,
`IDP_TEST_LAMBDA_SG_ID`, `IDP_TEST_APIGW_VPCE_ID`); `_test_vpc_params()` maps
them to the CFN params (`DeployInVPC`, `VpcId`, `PrivateSubnetIds` /
`LambdaSubnetIds`, `LambdaSecurityGroupId`, `ApiGatewayVpcEndpointId`) that a
`requires_vpc` probe injects at deploy time.

Because probes **reference** the VPC (never create/destroy/mutate it):
- **No VPC quota pressure** — the account's 5-VPC limit is never approached no
  matter how many VPC variants or concurrent pipelines run.
- **No per-run VPC churn or ENI-leak teardown failures** — the incident that
  removed the PRIVATE/VPC variant from CI simply can't recur.
- **Fully parallel** — VPCs no longer bound concurrency, so all probes run
  at once.

If the pipeline is deployed with `CreateTestVpc=false`, the VPC env vars are
empty and each `requires_vpc` probe **skips itself** (recorded as *skipped*, not
*failed*) — the no-VPC probes (GLOBAL, WAF) still run.

The NAT gateway in the persistent VPC carries a small standing cost
(~US$32/mo + data) — the deliberate trade for quota-safe, fully-parallel VPC
probes. The retired per-run VPC template
(`scripts/sdlc/apigw-hosting-test-vpc.yaml`) and the
`delete_apigw_test_vpc` / `cleanup_stale_apigw_test_vpcs` age-gated reaper are
retained for out-of-band/manual VPC testing.

### Concurrency budget

The launcher fans out to at most `IDP_PROBE_MAX_CONCURRENCY` probes at once
(default `DEFAULT_PROBE_MAX_CONCURRENCY = 8`, clamped to `[1, num_probes]`; a
malformed/≤0 override falls back to the default). Each probe deploys a full IDP
stack (+ IAM role/boundary) concurrently with the shared-stack deploy and any
other in-flight pipeline, so the cap still guards **bounded stack/IAM quota** —
but **VPCs no longer bound it** (one shared persistent VPC). The default is set
high enough to run the whole default table in parallel.

### Gating & implementation

**Gating**: the probes run by default; set `IDP_TEST_APIGW_HOSTING=false` to
skip them all (the env name is kept for backward compatibility).
**Implementation** in `scripts/sdlc/codebuild_deployment.py`: `PROBE_VARIANTS`
(the table), `deploy_and_test_probe()` (one probe's lifecycle, incl. VPC-param
injection + skip), `run_variant_probes()` (the concurrent launcher),
`resolve_probe_concurrency()` (the budget), and `_test_vpc_params()` (env → CFN
params). Launched from `main()` on its own supervisor thread concurrently with
the shared-stack suite. Mock-based unit coverage:
`scripts/sdlc/tests/test_variant_probes.py` (quota cap, single-probe
lifecycle, fail-fast isolation, VPC-param injection + skip, the hosting-variant
validators, consolidated summary) and `scripts/sdlc/tests/test_zap_dast_probe.py`
(ZAP probe registration, IDP_TEST_ZAP gate, OpenAPI seed, alert parsing,
WARN-only + always-restore).
**Duration**: ~20–30 minutes per probe (full nested-stack create + teardown);
all run in parallel by default.

### Adding a future variant

Add a `Probe(...)` row to `PROBE_VARIANTS` and supply a
`validate_fn(stack_name) -> {"success": bool, ...}`. Set `requires_vpc=True` to
get the persistent-test-VPC params injected automatically. Keep the
deploy+feature-smoke scope in mind (see above).

**Candidate future variants**: BYO S3 VPC endpoint
(`S3VpcEndpointIdOverride`/`…DnsNameOverride`), custom domain, `--govcloud`
(deploy-only where the account allows — an offline transform + region-aware
`cfn-lint` gate already exists as a fast-gate unit test; see the Gap Backlog).

---

## End-of-run summary (every pipeline, pass or fail)

Every run produces a report in the GitLab job log, uploaded to S3 and emailed via
SNS on failure. It has two layers:

- **A deterministic status table** listing every test — the build/publish step,
  each primary-suite step, and each deployment-variant probe — as passed / failed
  / cancelled / skipped, with an **OVERALL: PASS/FAIL** verdict. It always
  renders, even if the AI layer is unavailable.
- **An AI (Bedrock) narrative** on both pass and fail — a short PASS report, or a
  grounded root-cause analysis for an infrastructure or test failure.

The summary is uploaded **progressively** as steps complete (not just at the
end), so the result is available even when a run is long.

**Watching long runs.** The CodeBuild pipeline runs ~60–70 min and is not
time-capped. The GitLab monitor that watches it has credentials capped at 1 hour
(a hard AWS limit on role-chained sessions), so it **refreshes them mid-run** to
keep watching to ~110 min. If a run outlives that, the monitor hands off
gracefully — the pipeline finishes on its own and the authoritative result still
arrives via the S3 summary + SNS email. A failure detected at handoff fails the
GitLab job (it isn't masked by a green handoff).

---

## Test Studio Architecture

### Lazy Evaluation Design

Test Studio uses **lazy/on-demand evaluation** rather than automatic evaluation. This means metrics are only calculated when explicitly requested.

**Flow**:

1. **Test Run Creation**:
   ```bash
   idp-cli run-inference --test-set fake-w2
   ```
   - Creates DynamoDB record: `PK=testrun#{test_run_id}`, `SK=metadata`
   - Status: `QUEUED` → `RUNNING`

2. **Document Processing**:
   - Files processed through IDP pipeline
   - Each document: `ObjectStatus=COMPLETED`, `EvaluationStatus=COMPLETED`
   - Test run metadata: `Status=COMPLETE`, `CompletedFiles=N`
   - **Note**: `testRunResult` field does NOT exist yet

3. **Evaluation Trigger** (via CLI):
   ```bash
   idp-cli test-result --test-run-id <id> --wait
   ```
   - Invokes `TestResultsResolverFunction` Lambda with `getTestRunStatus`
   - Lambda detects `Status=COMPLETE` but no `testRunResult`
   - Sends SQS message to trigger evaluation
   - Returns `display_status=EVALUATING`

4. **Async Evaluation** (SQS worker):
   - SQS triggers same Lambda with `handle_cache_update_request()`
   - Calls `_aggregate_test_run_metrics()`:
     - Queries Athena for evaluation data
     - Calculates accuracy, precision, recall, F1, cost
   - Writes `testRunResult` to DynamoDB

5. **Polling for Completion**:
   - CLI polls `getTestRunStatus` every 10 seconds
   - When `testRunResult` exists, status changes to `COMPLETE`
   - CLI calls `getTestRun` to retrieve full results

**Why This Design**:
- Avoids expensive Athena queries on every batch completion
- Allows UI to show "EVALUATING" status while metrics calculate
- Evaluation only runs when results are actually needed

**CI/CD Implementation**:
```python
# Old approach (BROKEN): Direct DynamoDB polling
# This never triggered evaluation!
dynamodb.query(TableName=tracking_table, Key=...)

# New approach (WORKING): Use idp-cli test-result
run_command("idp-cli test-result --stack-name {stack} --test-run-id {id} --wait")
```

---

## Test Cleanup

### Bedrock Ingestion Job Cleanup
**Problem**: Discovery tests (Steps 9, 10) start Bedrock Knowledge Base ingestion jobs that take 30+ minutes. If fail-fast triggers early, cleanup runs while jobs are `IN_PROGRESS`, blocking stack deletion.

**Solution**: `cancel_bedrock_ingestion_jobs()` function:
1. Scans all stack resources for `AWS::Bedrock::DataSource`
2. Lists ingestion jobs for each data source
3. Stops any `IN_PROGRESS` jobs
4. Then proceeds with stack deletion

**IAM Permissions**: `bedrock:ListIngestionJobs`, `bedrock:StopIngestionJob`

### Stack Deletion
- Cancels all Bedrock ingestion jobs
- Deletes nested stacks first (AppSync, Pattern, DocumentKB, MultiDocDiscovery)
- Deletes main stack
- Cleans up S3 buckets, DynamoDB tables, Lambda functions

### Startup reapers (converge leaks from interrupted prior runs)

Each run tears down its own stacks and buckets, but an interrupted teardown (e.g.
credentials expiring mid-cleanup) can leak them — and leaked test resources had
previously exhausted the account's IAM-role quota and piled up thousands of
buckets. To stay self-healing, every run first reaps **stale** leftovers from
prior runs: test VPCs, IDP stacks (and their IAM helper stacks), and orphaned S3
buckets. All reapers are **age-gated and skip anything a concurrent pipeline is
still using**, so they never touch a live run.

## Success Criteria

### Test Pass Criteria
- All tests return `{"success": True}`
- No exceptions or errors
- Verification checks pass
- Expected outputs present

### Accuracy Thresholds
- **Test Studio (Step 7)**: Overall accuracy > 30%
- **Agentic Extraction (Step 8)**: All 532 fund items extracted (100% completeness)

### Performance Thresholds
- **Total pipeline runtime**: < 60 minutes (with parallel execution)
- **Stack deployment**: < 25 minutes
- **Test execution**: < 35 minutes
- **Cleanup**: < 5 minutes

## Verification Methods

### Output Verification
- **Extraction**: Checks for specific extracted fields (e.g., `applicant_name`, `loan_amount`)
- **Classification**: Verifies classification results exist
- **Rule Validation**: Validates rule statistics (passed, failed, skipped counts)
- **Agentic Extraction**: Counts extracted items (e.g., 532 fund items)

### Status Verification
- **Document Status**: Confirms `ObjectStatus=COMPLETED`
- **Batch Status**: Verifies all documents in batch complete
- **Test Run Status**: Checks test evaluation status via Lambda invocation
- **Stack Status**: Ensures `CREATE_COMPLETE` or `UPDATE_COMPLETE`

### CLI Command Verification
- **Test Studio**: Uses `idp-cli test-result` to trigger evaluation and retrieve metrics
- **Discovery**: Monitors workflow execution via tracking table
- **Config Management**: Validates config upload, activation, and retrieval

## CLI Commands Reference

### Key Commands Used

```bash
# Deploy stack
idp-cli deploy --stack-name <stack> --pattern pattern-2 --admin-email <email> --wait

# Run inference tests
idp-cli run-inference --stack-name <stack> --dir samples/ --file-pattern <pattern>

# Test Studio workflow
idp-cli run-inference --stack-name <stack> --test-set <test-set> --number-of-files 3
idp-cli test-result --stack-name <stack> --test-run-id <id> --wait --timeout 600

# Test comparison (future)
idp-cli test-compare --stack-name <stack> --test-run-ids "id1,id2" --output-dir ./results

# Config management
idp-cli config-upload --stack-name <stack> --config-file <file> --config-version <version>
idp-cli config-activate --stack-name <stack> --config-version <version>
idp-cli config-sync-bda --stack-name <stack> --config-version <version>

# Discovery workflows
idp-cli discover --stack-name <stack> --dir samples/ --file-pattern <pattern>
idp-cli discover-multidoc --stack-name <stack> --dir samples/
```

### New CLI Commands (v0.5.6)

#### `idp-cli test-result`
Get test results for a specific test run. Triggers evaluation if needed.

```bash
# Get results immediately (may show evaluating status)
idp-cli test-result --stack-name my-stack --test-run-id fake-w2-20260409-123456

# Wait for evaluation to complete (recommended for CI/CD)
idp-cli test-result --stack-name my-stack --test-run-id fake-w2-20260409-123456 --wait --timeout 900

# Save results to JSON file
idp-cli test-result --stack-name my-stack --test-run-id fake-w2-20260409-123456 --wait --output-dir ./results
```

**Output**:
- Overall Accuracy, Precision, Recall, F1 Score
- Total Cost
- File completion statistics
- Test run metadata

**Output File** (when `--output-dir` specified):
- `<test-run-id>-result.json` - Full test results including all metrics

#### `idp-cli test-compare`
Compare metrics and configurations from multiple test runs.

```bash
# Compare two test runs
idp-cli test-compare --stack-name my-stack \
  --test-run-ids "fake-w2-20260409-123456,fake-w2-20260409-234567"

# Compare and save to files
idp-cli test-compare --stack-name my-stack \
  --test-run-ids "run1,run2,run3" --output-dir ./comparisons
```

**Output**:
- Side-by-side metrics comparison table
- Configuration differences between runs
- Cost comparison

**Output Files** (when `--output-dir` specified):
- `comparison-<timestamp>.json` - Full comparison data
- `comparison-<timestamp>.csv` - Metrics table (for spreadsheets)

---

## Monitoring and Debugging

### CloudWatch Logs
- **CodeBuild Logs**: `/aws/codebuild/<project-name>`
- **Lambda Logs**: `/aws/lambda/<function-name>`
- **Step Functions**: View execution history in console
- **Test Results Resolver**: `/aws/lambda/TestResultsResolverFunction`

### Tracking Table
- **Location**: DynamoDB table from stack output `DynamoDBTrackingTableConsoleURL`
- **Records**:
  - Documents: `PK=doc#{document_id}`, `SK=none`
  - Test Runs: `PK=testrun#{test_run_id}`, `SK=metadata`
  - Batches: `PK=batch#{batch_id}`, `SK=metadata`

### Common Failure Points
1. **Step 7 (Test Studio)**: Evaluation timeout - ensure `idp-cli test-result --wait` is used
2. **Step 8 (Agentic)**: Table parsing failures if OCR quality poor
3. **Steps 9-10 (Discovery)**: Ingestion job cleanup failures if permissions missing
4. **Step 4 (BDA)**: BDA sync failures or blueprint creation errors
5. **Step 11 (test-compare)**: Requires TestResultsResolverFunctionArn in stack outputs
6. **Parallel Tests**: Fail-fast cancellation if any test fails

### Debugging Test Studio Issues

If Test Studio test fails:

1. **Check test run exists**:
   ```bash
   aws dynamodb query --table-name <tracking-table> \
     --key-condition-expression "PK = :pk AND SK = :sk" \
     --expression-attribute-values '{":pk":{"S":"testrun#<test-run-id>"},":sk":{"S":"metadata"}}'
   ```

2. **Manually trigger evaluation**:
   ```bash
   idp-cli test-result --stack-name <stack> --test-run-id <id> --wait --timeout 900
   ```

3. **Check Lambda logs**:
   - CloudWatch Logs: `/aws/lambda/<stack>-TestResultsResolverFunction-*`
   - Look for SQS message sending and metric aggregation

4. **Check SQS queue**:
   ```bash
   aws sqs get-queue-attributes --queue-url <TEST_RESULT_CACHE_UPDATE_QUEUE_URL> \
     --attribute-names ApproximateNumberOfMessages
   ```

---

## TODO / Gap Backlog

Remaining CI test-coverage gaps, ranked. Nothing here is blocking; these are
follow-ups to harden the pipeline against regressions on `develop`. Several
earlier gaps are **already closed** — see "Done (this cycle)" at the end.

The full unit/package gate (`make test`, 34 auto-discovered roots) is green.
What's left below is mostly integration/e2e depth plus a few cheap fast-gate
additions.

### Fast-gate additions (cheap, no live AWS account needed)

- [x] **`--govcloud` transform + cfn-lint gate (unit-level).** `GovCloudTemplate
      Transformer` runs against the committed `template.yaml` and the result is
      linted with real `cfn-lint --region us-gov-west-1`, asserting zero **E3006**
      ("resource type does not exist in region"). Fails the gate if a
      GovCloud-unsupported resource (CloudFront, Lambda Function URL, etc.) is
      reintroduced — strictly stronger than the transformer's own hardcoded
      resource check. Offline/no-credentials. See
      `lib/idp_sdk/tests/unit/test_govcloud_template_transform.py::test_real_
      template_passes_govcloud_region_cfn_lint`. *(Note: the raw repo template
      carries SAM short-form tags, so a full lint of the **published/SAM-baked**
      template still needs the publish pipeline — deferred to the integration
      tier.)*
- [x] **`--headless` template-transform smoke.** Closed at two tiers after this
      gap shipped a headless template CloudFormation rejects outright — the
      `SuppressAdminInvite` condition referenced the `AdminEmail` parameter the
      transform removes, so for six weeks every headless deploy died at
      validation (`Unresolved dependencies [AdminEmail]`) before creating a
      single resource. **Fast gate (offline, no AWS):**
      `test_headless_transform_leaves_no_unresolved_parameter_reference`
      (structural — no surviving `Ref`/`Fn::Sub` in Conditions/Rules/Outputs may
      point at a removed parameter) and
      `test_real_template_headless_has_no_cfn_lint_errors` (zero `Error`-level
      findings; warnings stay ungated since headless legitimately leaves unused
      conditions and unreachable `Fn::If` branches). Note the pre-existing
      headless lint probe gates only `E3006`, which is exactly why an `E1020`
      slipped through. **Integration tier:** `validate_headless_template`
      transforms the *packaged* `.aws-sam/idp-main.yaml` and validates it via
      real CloudFormation `ValidateTemplate`, closing the source-vs-packaged gap
      the offline tests cannot (SAM expansion, nested-stack URLs). Costs one S3
      put + one API call — it reuses the template the publish step already built.
      A full headless *deploy* e2e now exists as an ON-DEMAND runner rather
      than a CI job: `make transform-deploy-test-headless` /
      `-govcloud` (`scripts/sdlc/transform_deploy_test.py`) deploy the
      TRANSFORMED template via the documented `idp-cli deploy
      --headless|--govcloud --from-code .` path, assert each transform's
      structural promises, and process a real sample document. Deliberately
      not wired into CI (~1h+ per variant); the validators and result shape
      already match the probe framework so wiring in is additive. See
      `.claude/skills/transform-deploy-test.md`. ⚠️ A COMMERCIAL `govcloud`
      run does NOT prove GovCloud behaviour (partition ARNs, model
      availability, the BDA project rejection) — that needs
      `REGION=us-gov-west-1` against a GovCloud account, which the runner
      supports unchanged.
- [x] **Register the `pytest.mark.unit` marker repo-wide.** A minimal repo-root
      `pytest.ini` registers the `unit` / `integration` markers, so the ~12
      per-Lambda/resolver dirs without their own config no longer emit
      `PytestUnknownMarkWarning`. Suites with their own `pytest.ini` are
      unaffected (closer config wins).

### Integration / e2e depth (need the CI account; run in the CodeBuild suite)

- [x] **Jobs API deploy e2e (deploy + feature-smoke).** A deployment-variant
      probe (`jobsapi`): deploys the STANDARD template with `EnableJobsApi=true`
      against the persistent test VPC and asserts the Jobs API deployed
      (`validate_jobs_api`). *Deploy + smoke only* — the private Jobs API isn't
      call-tested from CodeBuild (not in-VPC), and full doc-processing through
      the Jobs API path is still not exercised, so the deeper
      `scripts/e2e_test_headless.py` flow remains a follow-up. NOTE: this probe
      exercises the additive `EnableJobsApi` CFN parameter, **not** the
      `idp-cli deploy --headless` template transform — a `--headless` *deploy*
      probe is a separate follow-up (see below).
- [x] **APIGW hosting: GLOBAL variant + HTTP smoke.** The GLOBAL/no-VPC APIGW
      hosting probe deploys `WebUIHosting=APIGateway` + `ApiGatewayVisibility=GLOBAL`
      and does a real HTTP `GET` of the served UI (`validate_apigw_global_hosting`
      asserts HTTP 200 from the execute-api `/api` URL, proving the S3-proxy path
      returns bytes). Now the first row of the deployment-variant probe framework.
- [ ] **Upgrade-in-place test. (HIGH VALUE.)** Deploy the previous released
      version, then update the stack to the current build, then smoke. This is the
      gap that would have caught the pricing-units rollback deadlock and the
      GSI-projection-immutable issues. No current test exercises an update path.
- [ ] **Deepen e2e assertions.** Most steps assert `status==COMPLETE` /
      sections-exist. Broaden to assert expected field *values* from the known
      sample docs (e.g. `samples/lending_package.pdf`) — near-zero added runtime,
      catches silent accuracy regressions.
- [ ] **`save_reporting_data` reporting-path e2e.** Unit tests were mocked off real
      AWS; there is no e2e that actually exercises the Glue/Athena reporting write
      path end-to-end.
- [ ] **CloudFront (default) hosting HTTP smoke.** The shared-stack suite deploys
      with CloudFront hosting but never HTTP-smokes the CloudFront URL.

### UI

- [ ] **Browser/e2e UI test (Playwright) against a deployed stack.** Today only
      vitest units run (jsdom, no browser). A thin smoke (login → list docs → open
      a doc) would catch integration/auth regressions the units can't.

### Quarantined test roots — promote when unblocked

Tracked in `scripts/run_all_tests.py` (`QUARANTINE`). Correctly excluded today,
but revisit:

- [ ] `src/lambda/ocr_benchmark_deployer` — needs `huggingface_hub` as a test dep.
- [ ] `nested/bedrockkb/src/s3_vectors_manager` — needs the Lambda-runtime-only
      `cfnresponse` module available under test.
- [ ] `scripts/test_api_rbac.py` — the live RBAC harness (not a pytest suite);
      leave excluded unless refactored.
- [ ] `samples/lambda-hook-inference/GENAIIDP-chandra-ocr-hook` — `test_local.py`
      is a manual run script; leave excluded or convert to real tests.
- [ ] `lib/idp_sdk/idp_sdk/_core` — source tree with helper modules named
      `test_*`; leave excluded (not tests).

### Done (this cycle — no longer gaps)

- [x] **Pipeline hooks end-to-end.** Added as Step 14: deploys a real hook
      Lambda, registers it at `preprocessing` + `postprocessing` in a dedicated
      config version, and asserts the dispatcher invoked it AND that its
      `updatedDocument` reached the persisted document. Previously **no** hook
      point had live coverage.

- [x] **Dynamic security testing (DAST).** OWASP ZAP scan of the deployed UI API
      added as the `zapdast` deployment-variant probe — authenticated scan seeded
      from `api_rbac_expectations.yaml`, WARN-only, report to
      `s3://…/deploy/zap/…`. Requires `PrivilegedMode: true` on `app-sdlc`.
      Complements SRT (static) + RBAC (authorization) with injection/headers/TLS
      coverage. *(feature/zap-dast-ci-probe)*
- [x] **Mandatory AppSec API security test cases.** Extended the live API harness
      (`make api-test`, CodeBuild Step 12) with the AppSec checklist suites:
      IDOR/BOLA (2.1), token expiry + logout revocation (2.3/2.4), deleted-resource
      inaccessibility (2.5), input validation (3, tolerant→strict), and TLS
      downgrade/cleartext (4). Implemented in `scripts/api_security_cases.py` with
      unit tests; threat entries AUTH.T09–T11. The stateless-JWT post-logout reuse
      is surfaced as a documented gap (GAP-SEC-LOGOUT). *(feature/zap-dast-ci-probe)*
- [x] `idp_common` `-m "unit"` filter → `-m "not integration"` (recovered ~810
      silently-skipped tests; fixed 28 rotted tests). *(PR #493)*
- [x] Missing package/Lambda suites added to `developer_tests` via
      `make test-packages-cicd` (~665 tests). *(PR #494)*
- [x] `check-arn-partitions` (GovCloud ARN guard) wired into `lint-cicd`. *(PR #494)*
- [x] API RBAC: static scan in the MR gate + live harness as CodeBuild Step 12.
      *(PR #494)*
- [x] SRT security scan runs fail-fast (before the ~2h integration stage) on
      branch pipelines. *(PR #494)*
- [x] Deployment-failure AI root-cause summary (incl. Step 4b) + failure email
      (`FailureNotificationEmail` SNS). *(PR #494)*
- [x] `make test` auto-discovers all test roots; a new/unregistered test dir
      hard-fails so suites can't be silently skipped. *(PR #495)*
- [x] Fixed the Step Functions execution-view "Running vs Failed" resolver bug and
      `save_reporting_data` stale/real-AWS tests; both promoted into the gate.
      *(PR #495)*
- [x] CodeBuild role granted EC2/VPC permissions for the Step 4b hosting test.
      *(PR #497)*
- [x] Stopped `idp_sdk` `test_create_config` writing a stray `config.yaml` to the
      repo root. *(PR #498)*
- [x] Generalized the single APIGW hosting test into the **deployment-variant
      probe framework** (`PROBE_VARIANTS` table + `run_variant_probes` launcher +
      `resolve_probe_concurrency` quota budget), with mock-based unit tests in
      `scripts/sdlc/tests/`. *(fix/ci-variant-probe-framework)*
- [x] Repo-root `pytest.ini` registering the shared `unit`/`integration` markers
      (silences `PytestUnknownMarkWarning` in ~12 per-Lambda dirs).
      *(fix/ci-variant-probe-framework)*
- [x] Real `cfn-lint --region us-gov-west-1` E3006 gate on the transformed
      committed template (offline fast-gate unit test).
      *(fix/ci-variant-probe-framework)*
- [x] **Persistent pipeline-owned test VPC** (`codepipeline-s3.yml`,
      `CreateTestVpc`) reused by every run → VPC-requiring probes are quota-safe
      and fully parallel; no per-run VPC create/destroy or ENI leaks.
      *(fix/ci-variant-probe-framework)*
- [x] **Three new probes** — WAF-enabled (IP allow-list), PRIVATE APIGW hosting,
      and the Jobs API (`EnableJobsApi=true`) — added to `PROBE_VARIANTS` (all default-on), plus
      `DEFAULT_PROBE_MAX_CONCURRENCY` raised so the whole table runs in parallel.
      *(fix/ci-variant-probe-framework)*
- [x] **Every-run consolidated summary** (`build_consolidated_summary`) listing
      publish + every primary step + every probe with status + OVERALL PASS/FAIL,
      always rendered to the GitLab log and uploaded to S3/SNS; Bedrock now writes
      a grounded report on **pass as well as fail**.
      *(fix/ci-variant-probe-framework)*
- [x] **Startup reapers for leaked test stacks, IAM roles, and buckets** —
      age-gated and concurrent-run-safe, so an interrupted cleanup can't exhaust
      the account's IAM-role quota or pile up buckets again.
- [x] **Handoff FAIL verdict** — the monitor fails the GitLab job when the
      summary shows OVERALL: FAIL, instead of exiting green.
- [x] **Progressive summary upload** — a current result reaches S3 before the
      monitor handoff even when a run is long (fixed a finished run showing "No
      summary found").
- [x] **Mid-run monitor credential refresh** — the monitor refreshes its
      1h-capped credentials to watch long runs to ~110 min. *(Needs a live >1h
      run to fully validate.)*

---

## Related Documentation

- [CHANGELOG.md](../../CHANGELOG.md) - Feature changes and test additions
- [CLAUDE.md](../../CLAUDE.md) - Project architecture and build commands
- [docs/test-studio.md](../../docs/test-studio.md) - Test Studio user guide
- [scripts/sdlc/README.md](../README.md) - SDLC infrastructure setup
- [scripts/sdlc/cfn/codepipeline-s3.yml](../cfn/codepipeline-s3.yml) - CodeBuild IAM permissions

