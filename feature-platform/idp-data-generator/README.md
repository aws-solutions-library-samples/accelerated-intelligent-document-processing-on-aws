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
  schema, writes the configuration profile to the host Configuration table, then
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

## What the generator's model can do

The AgentCore runtime installs `seed-data` **0.0.7**, pinned exactly in the
`Dockerfile`. The tools SEED gives its models can change between releases, so
re-read this section against the new release's source before moving the pin.
The inventory below was taken by reading the 0.0.7 wheel, not by running it.
The `Dockerfile` also pins `xhtml2pdf==0.2.28` and installs SEED's `augment`
extra, which the optional scan/fax effects need. `strands-agents-tools` is not
pinned here (SEED requires `>=0.8`; 0.9.0 was read).

`idp_common.synthesis.engine` calls only `Generator(...).generate_batch`, and it
names the PDF backend explicitly (`SEED_RENDERER = "xhtml2pdf"`). These are the
agents that path builds and the tools each one holds:

| Agent (SEED module) | Tools |
|---|---|
| Scenario planner (`stages/batch.py`) | none |
| Data generator (`stages/data.py`) | `save_json_file`, `calculator`, `random_roll` |
| Data critic (`stages/data.py`) | `calculator` |
| Document generator (`stages/document.py`) | `file_write`, `editor` (both from `strands-agents-tools`), `read_json_file`, `random_roll`, `render_html_to_pdf` |
| Document critic (`critique.py`) | none |
| Augmentor (`stages/augment_stage.py`) | `apply_augmentation` |
| Augmentation critic (`augment.py`) | none |

What those tools can and cannot do:

- **No tool takes a URL as an argument or makes an HTTP request of its own.**
  `calculator` evaluates arithmetic over an AST allowlist rather than with
  `eval`, and `random_roll` returns a boolean. Rendering is the one place a
  fetch can happen, described below.
- **`shell` is not in the set.** SEED gives the document generator the
  `strands-agents-tools` `shell` tool only with its `reportlab` backend, so that
  the model can run the Python script it writes. The adapter names `xhtml2pdf`
  so that SEED's default changing cannot add it.
  `tests/unit/synthesis/test_engine.py` in `idp_common_pkg` checks that.
- **`file_write` and `editor` read and write any path the image's `appuser` can.**
  `editor`'s `view` command returns a file's contents to the model, so anything
  `appuser` can read can enter the model's context. `BYPASS_TOOL_CONSENT=true`
  on the runtime turns off their interactive confirmation, which would otherwise
  cancel every call in the headless runtime. Neither one makes a network call.
- **Rendering loads local and inline resources only.** `render_html_to_pdf`
  renders the HTML the document generator wrote. SEED calls `xhtml2pdf` with no
  access policy, and xhtml2pdf's default policy fetches public `http(s)`
  references (images, stylesheets, `@import`, fonts) while the PDF renders. The
  adapter therefore replaces SEED's `xhtml2pdf` backend before generation with
  one that passes `ResourceAccessPolicy(allow_remote=False)`
  (`_render_without_remote_fetches` in `engine.py`). A remote reference is then
  logged by xhtml2pdf and left out of the PDF, and the render carries on;
  SEED's generator prompt does not ask for remote resources. Inline `data:`
  resources still load. Local reads stay confined, as with SEED's own call, to
  the process's working directory, which is the runtime user's home directory
  (`/home/appuser`). A file there, including one the model writes there or
  augraphy's cache, can be loaded; a file the model writes beside its HTML in
  the job's temporary directory is not. That policy exists
  in xhtml2pdf 0.2.27 and 0.2.28 and not in 0.2.17. If the installed xhtml2pdf has no
  `xhtml2pdf.config.resources`, or SEED no longer keeps its backends in
  `seed_data.tools._HTML_RENDERERS`, the adapter logs
  `Render fetch policy not applied` and SEED's own renderer is used, which
  fetches remote references.
- **The model has no AWS tool.** The runtime's role can read, write and delete
  objects in the TestSet bucket, read and write the Working bucket, read the
  configuration table, and write both this extension's job tracking table and
  the host's tracking table. Only the runtime handler and the adapter use those
  permissions; no tool the model holds calls S3, DynamoDB or any other AWS API.

The model's context holds the document class schema (from the configuration
profile, or authored from your description), the scenario text, the data it
generates, and whatever it reads with `editor`.

### Network mode

The runtime is created with `networkMode: PUBLIC`
(`agentcore_runtime_lambda/index.py`), which gives it outbound internet access.
It needs that access, or an endpoint per service, to reach Amazon Bedrock
Runtime, Amazon S3, Amazon DynamoDB, AWS KMS, Amazon ECR, CloudWatch Logs and
metrics, AWS X-Ray and AgentCore workload identity. VPC mode with no internet
route would need a VPC endpoint for each of those, and it has not been tested
with this extension, so the extension does not offer it. When the adapter has
applied its render policy, no tool the model holds makes a network request; when
it has not, it logs `Render fetch policy not applied`. If your
account requires that the runtime itself have no internet route, do not install
this extension until a VPC option exists.

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
  `UserPoolClientId`, `ConfigurationBucketName`. (`TestSetBucketName` and
  `ConfigurationBucketName` are re-exported by the host's FeaturePlatformStack —
  added for this extension. `ConfigurationBucketName` lets the bootstrap
  processor record the revision of each profile it saves; a host that predates
  that export cannot install this extension from version 0.1.5 on.)
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
  → Generate Synthetic Data (the host modal, with profile/class dropdowns, is the
  single source of truth); the feature bundle intentionally doesn't duplicate it.
