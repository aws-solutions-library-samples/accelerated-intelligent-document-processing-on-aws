# Test Set Generator — Feature Platform extension

> Display name: **Test Set Generator**. Feature id (stable key): `idp-data-generator`.

Packages the SEED synthetic-document generator (the published **`seed-data`**
PyPI package) plus the accelerator's synthesis adapter (`idp_common.synthesis`)
as a **standalone, installable extension** for the GenAI IDP Accelerator — the
same pattern as **IDP AutoTune** and **sample-health-insurance-review**: a
separate CloudFormation stack that attaches to a deployed IDP host stack by
`MainStackName`, builds its AgentCore Runtime image at install, and registers
itself so it appears under **Extensions** in the host UI.

> **STAGED DRAFT — not yet deploy-verified end-to-end on this branch.** The code
> and packaging are complete and locally verified (Python compiles, template
> parses, imports resolve), but the image build (`seed-data` +
> `idp_common[synthesis]` on arm64) and the AgentCore runtime have NOT been run
> through a live CodeBuild → ECR → AgentCore install cycle. That is the remaining
> validation step before release.

## Architecture

The generator is no longer vendored: **`seed-data` is pip-installed from PyPI**
and driven through its typed `Generator` API by `idp_common.synthesis.engine`
(the same adapter the CLI `idp-cli bootstrap` uses). Two execution surfaces:

- **BootstrapProcessor Lambda** (`bootstrap-processor/`) — SQS-driven. Authors a
  schema, writes the config version to the host Configuration table, then
  *invokes* the AgentCore runtime. Installs `idp_common[synthesis]` only (light,
  no `seed-data`) — it doesn't generate documents itself.
- **AgentCore Runtime image** (`Dockerfile` + `agent-source/runtime/handler.py`)
  — the arm64 container that actually runs generation. Installs `seed-data` +
  `idp_common[synthesis]`.

### numpy isolation (important)

`seed-data` requires **numpy 2.x** (opencv/scikit-image/numba), which conflicts
with the `numpy==1.26.4` pin in `idp_common`'s `ocr`/`evaluation`/`all` extras.
The image therefore installs only `idp_common[synthesis]` (jsonschema-only), and
the generator runs in its own container — never co-located with those extras.

## Layout

```
feature-platform/idp-data-generator/
  feature.yaml              manifest (agentSource -> package_agent_source.sh)
  template.yaml             SAM stack (ECR+CodeBuild+AgentCore+Bootstrap+FeatureApi+ui-deployer)
  buildspec.yml  Dockerfile builds the arm64 image (pip install seed-data + idp_common)
  package_agent_source.sh   stages lib/idp_common_pkg into the context + zips agent-source.zip
  agent-source/
    requirements.txt        image deps: idp_common_pkg[synthesis] + AgentCore/SigV4
    runtime/handler.py       AgentCore /invocations + /ping entrypoint
  bootstrap-processor/       SQS processor: authors schema, invokes the runtime
  ui-deployer/handler.py     copies UI bundle to host WebUIBucket + registerFeature
  feature-api/handler.py     POST /generate, /generate-from-config, /estimate-cost,
                             /suggest-scenario; GET /jobs, /jobs/{id}, /config
  feature-ui/                React UMD landing page (points to Test Studio)
```

## Entry points

- **Quick Start** (host, core): the onboarding agent discovers this extension via
  `list_available_extensions` (matches `featureId: idp-data-generator`) and
  delegates generation to it.
- **Test Studio → Test Sets**: a "Generate Synthetic Data" button (shown only
  when this extension is installed) opens a modal that POSTs to the feature API's
  `/generate` or `/generate-from-config`.

Both hit the feature API → BootstrapQueue → BootstrapProcessor → AgentCore
runtime → test set written to the host TestSet bucket (Test Studio auto-discovers
the `{testSetId}/input` + `{testSetId}/baseline` layout).

## Host contract this extension depends on

Stable interface owned by the accelerator (must not break):
- Host exports via `Fn::ImportValue ${MainStackName}-X`: `ConfigurationTableName`,
  `WorkingBucketName`, `TestSetBucketName`, `RegisterFeatureFunctionArn`,
  `CustomerManagedEncryptionKeyArn`, `WebUIBucketName`, `UserPoolId`,
  `UserPoolClientId`. (`TestSetBucketName` is re-exported by the host's
  FeaturePlatformStack — added for this extension.)
- Config storage: `idp_common.config.ConfigurationManager` (gzip-compressed
  Binary format) — the extension installs `idp_common` so it shares the host's
  exact read/write format rather than vendoring or round-tripping.
- Feature registration: direct `lambda:Invoke` of the host's
  `RegisterFeatureFunction` Lambda with the resolver event shape
  `{info: {fieldName}, arguments, identity}`. (AppSync was removed from the
  accelerator; the event shape is unchanged from that era, which is why it still
  looks like a GraphQL resolver event.)
- Per-job status is feature-owned (BootstrapTrackingTable), read via the
  FeatureApi `GET /jobs/{id}` — there is no host status channel.

## Publish & install

```bash
# idp-feature-sdk is first-party (lib/idp_feature_sdk) and is NOT installed from
# PyPI — install it from the local checkout. See docs/dependency-confusion.md.
pip install -e lib/idp_feature_sdk        # run from the repo root
cd feature-platform/idp-data-generator
idp-feature-cli publish --feature-bucket <your-bucket>                 # dev
idp-feature-cli publish --feature-bucket <dist-bucket> --make-public   # release
```

Install = the CloudFormation quick-create URL with `MainStackName=<your IDP stack>`.
`idp-feature-cli build` runs the same build offline (no AWS) to verify first.

## Remaining validation (before release)
- ⏳ Live install cycle: CodeBuild arm64 image build (`seed-data` +
  `idp_common[synthesis]`) → ECR → AgentCore runtime → end-to-end generate.
- ⏳ FeatureApi request/response contract vs. the Test Studio modal + Quick Start.
- The `feature-ui` page is a landing card that points to Test Studio → Test Sets
  → Generate Synthetic Data (the host modal, with version/class dropdowns, is the
  single source of truth); the feature bundle intentionally doesn't duplicate it.
