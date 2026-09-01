# Test Set - ConfBench — Feature Platform extension

On-demand deployment of the [amazon/ConfBench](https://huggingface.co/datasets/amazon/ConfBench)
benchmark (75 FCC invoices x up to 21 Augraphy noise variants = 1,346 documents,
**32.71 GB**) into the host's Test Studio.

User-facing documentation: [`docs/extensions/confbench-testset.md`](../../docs/extensions/confbench-testset.md).
This file covers the internals.

## Why an extension

The full dataset is ~42x the combined size of the four benchmark sets the main
stack pre-deploys. As a main-stack deployer it charged every deployment for a
specialist research dataset and put a ~32.7 GB serial transfer inside
CloudFormation's 1-hour custom-resource deadline, where an overrun is a stack
rollback. As an extension the cost is opt-in twice (install, then choose a
subset) and no transfer is ever inside a CFN wait.

Reworked from [PR #583](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/pull/583)
by @sujimart. The streaming multipart transfer and the ground-truth baseline
format are carried over from that contribution.

## Layout

```
feature.yaml            manifest (featureId confbench-testset)
template.yaml           the feature stack
shared/python/          variants.py — the noise-variant catalog, shipped as a Lambda layer
shared/Makefile         SAM makefile build for that layer (see the note below)
ingest/
  planner.py            plan_handler / finalize_handler / fail_handler
  index.py              per-variant ingest worker
feature-api/handler.py  GET /variants, GET /jobs[/id], POST /ingest, DELETE /dataset/{id}
feature-ui/             React UMD bundle — tier + variant picker, job progress
ui-deployer/handler.py  RegisterFeature custom resource + config preset
config-preset/          the Invoice extraction schema
tests/                  91 unit tests
```

## Ingest architecture

```
Plan ──► Map(variants, MaxConcurrency 4) ──► Finalize
 │         │  IngestShard ──► ShardComplete ─┐        │
 │         │       ▲                          │       │
 │         │       └──── done == false ◄───────┘      │
 └─ Catch ─┴──────────────► MarkFailed ──► Failed ◄───┘
```

Three properties are load-bearing:

**Nothing waits on CloudFormation.** The ingest is an on-demand job, so a slow
CDN is a slow job, not a failed stack. This is what retires the original
design's central risk.

**Sharding is by BYTES, not file count.** Variant sizes span `original` at
0.02 GB / 75 files to `custom15` at 7.12 GB / 74 files — near-identical counts,
a 300x size difference. `MAX_WORKER_BYTES` (1.2 GB, sized so even a 2 MB/s
sustained CDN rate clears it inside the 900 s timeout) plus a clock guard end a
pass cleanly and return a resume offset the state machine feeds back. The
original's fixed 100-file chunks were sized against a distribution the dataset
contradicts.

**State lives in S3 and the job table, never in an invoke payload.** The
original accumulated a failed-id list inside its async self-invoke payload,
which silently breaks past Lambda's 256 KB `Event` limit during exactly the
systemic failure you most want reported. Failures here append to a per-variant
S3 object; the payload carries only counts.

Retries are the state machine's job. The worker raises `RetryableIngestError`
for transient faults (5xx, 429, connection resets) and records permanent ones
(404, 403) as per-document failures without aborting the shard.

## The shared layer

`variants.py` holds exact per-variant file counts and byte sizes. The feature API
and the ingest planner **must** agree on them — if they drifted, the cost
estimate an admin approves would stop matching the bytes that land, which is the
problem this extension exists to prevent. Hence one copy, shipped as a layer.

`BuildMethod: makefile`, deliberately:
- `BuildMethod: python3.12` runs pip against a `requirements.txt` and does not
  copy hand-authored source — `variants.py` would be silently dropped.
- **No** `BuildMethod` leaves `ContentUri` as a relative path pointing outside
  `.aws-sam/build`, which breaks `sam package`.

Lambda puts a layer's `python/` directory on `sys.path`, so the module lives at
`shared/python/variants.py` and both functions `import variants`.

## Host integration

Uses only existing exports — **no main-stack changes**:

| Import | For |
|---|---|
| `<MainStackName>-TestSetBucketName` | writing documents + baselines |
| `<MainStackName>-TrackingTableName` | the `testset#<id>` record Test Studio lists |
| `<MainStackName>-CustomerManagedEncryptionKeyArn` | the TestSet bucket is SSE-KMS |
| `<MainStackName>-UserPoolId` / `-UserPoolClientId` | the API's JWT authorizer |
| `<MainStackName>-WebUIBucketName` | the UI bundle |
| `<MainStackName>-RegisterFeatureFunctionArn` | registration |
| `<MainStackName>-ApplyFeatureConfigPresetFunctionArn` | the config preset |

IAM is scoped to `confbench*` and `_confbench_jobs/*` in the shared TestSet
bucket — never the whole bucket. `DELETE /dataset` additionally validates the id
against this extension's own test-set ids, so the route cannot be turned into an
arbitrary-prefix delete against another test set.

## Tests

```bash
pip install -r tests/requirements.txt
python -m pytest                      # 91 unit tests
CONFBENCH_NETWORK_TESTS=1 python -m pytest -m integration   # verify catalog vs HuggingFace
```

The network-gated test re-derives the whole variant table from the live dataset
and diffs it, so an upstream re-publish surfaces as a test failure rather than a
wrong number in the UI.

## Dev loop

```bash
idp-feature-cli build ./feature-platform/confbench-testset
idp-feature-cli deploy --from-code ./feature-platform/confbench-testset \
    --host-stack-name <stack>
```

## Config-version preselection

Test Studio preselects a configuration for a chosen test set in this order:

1. `configVersion` **declared on the test-set record** — what this extension
   writes (`confbench-testset-v<version>`).
2. A config version whose name **equals the test set id** — the convention the
   stack-managed benchmark sets (`fake-w2`, `docsplit`, …) rely on.
3. The active version.

Step 1 is a host change that landed with this extension (`TestSet.configVersion`
in the GraphQL schema, passed through by `test_set_resolver`, consumed by
`TestRunner.tsx`). It exists because step 2 structurally cannot serve extensions:
the Feature Platform names every extension preset `<featureId>-v<version>`, which
can never equal a test set id.

The same `<featureId>-v<version>` string is therefore computed in two places —
`config_version_name()` in `ui-deployer/handler.py` (which creates the config
the feature id) and `CONFIG_VERSION_NAME` in `template.yaml` (which the planner records
on each test-set row). Both derive it from `FeatureId` + the publish-time version
token, so they cannot drift on a version bump, but a rename of either half needs
the other updated.

The planner writes it with `if_not_exists`, so an admin who repoints a ConfBench
test set at their own tuned configuration keeps that choice across re-ingests.
