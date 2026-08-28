# Skill: Deploy-test the `--headless` / `--govcloud` template transforms

Use this when the user wants to **prove a template transform actually deploys** —
e.g. "test the headless deploy", "does `--govcloud` still work", "deploy-test the
transforms", "verify headless end to end", or before cutting a release that
touched `template_transform.py`, `template.yaml`, or the nested pattern template.

This is the **only** test tier that deploys a *transformed* template and puts a
real document through it.

## Why this exists (read before reporting a result)

The transforms had three coverage tiers before this, none of which can prove a
transformed template deploys:

| Tier | Catches | Misses |
|---|---|---|
| Offline unit tests (`lib/idp_sdk/tests/unit/`) | dangling refs, cfn-lint errors, GovCloud-illegal resource *types* | anything that only fails at resource-creation time |
| `ValidateTemplate` on the packaged template (#684, integration tier) | malformed template, unresolved references | same |
| Deploy-variant probes (`make stacktest-*`) | parameter combinations on the **standard** template | the transforms entirely |

That last row is the trap: the `jobsapi` probe was once *named* "headless", but it
tests the additive `EnableJobsApi` **parameter**, not the transform. Leftover
`-headless` stack names in the reaper fixtures come from that old name.

Three real defects landed in this gap — templates valid, transforms successful,
failure only once CloudFormation started creating resources:

- **#676** — `BDAOCRProject` created unconditionally; GovCloud BDA rejects it, root stack rolls back.
- **#677** — the `--govcloud` transform removed the chat Function URL but left its LWA-dependent handler; 403 on `lambda:GetLayerVersion` after ~15 resources.
- **`SuppressAdminInvite`** — a condition referencing the removed `AdminEmail` parameter broke **every** `--headless` deploy for six weeks.

## The targets

| Target | What it does |
|---|---|
| `make transform-deploy-test-list` | List variants + caveats |
| `make transform-deploy-test-headless` | Deploy a real `--headless` stack, assert no UI, process a sample doc, tear down |
| `make transform-deploy-test-govcloud` | Deploy a real `--govcloud` stack, assert no CloudFront/Lambda-URL/LWA handler + UI retained, process a sample doc, tear down |
| `make transform-deploy-test-all` | Both, sequentially |

Variables: `REGION=`, `ADMIN_EMAIL=`, `STACK_NAME=` (validate an existing stack;
no deploy, no teardown), `KEEP=1` (leave the stack up), `SKIP_DOC_TEST=1`
(structural only), `JSON_OUT=path`. The script also takes
`--with-knowledge-base` — see the cost section below for why it is off by default.

Always `AWS_PROFILE=default` — see CLAUDE.md. **Confirm the account first:**
`AWS_PROFILE=default aws sts get-caller-identity`.

## What it runs

The **documented user path**, not a reconstruction:

```
idp-cli deploy --headless --from-code . --wait
idp-cli deploy --govcloud --from-code . --wait
```

So publish → transform → deploy is all under test, and the CLI handles the
per-variant parameter differences itself — notably that the headless template has
**no `AdminEmail` parameter**, and passing one is a CloudFormation
`ValidationError`.

## Cost and duration — tell the user before starting

Each run is a **full publish + deploy**: SAM build (Docker images, UI bundle) then
a complete IDP stack, then teardown. **Budget 1–2 hours per variant**, and real
spend for that window — Bedrock/Textract calls for the sample document plus every
resource in the stack.

The dominant avoidable cost is the **Bedrock Knowledge Base**.
`DocumentKnowledgeBase` defaults to `BEDROCK_KNOWLEDGE_BASE (Create)`, and the
`--govcloud` transform forces `KnowledgeBaseVectorStore=OPENSEARCH_SERVERLESS` —
so a naive run stands up an **OpenSearch Serverless collection**: minimum-OCU
billing, and the slowest resource in the stack to both create *and* delete. It has
nothing to do with what a template transform does, so the runner **disables the
Knowledge Base by default** on `--govcloud`. Pass `--with-knowledge-base` (or
`KEEP=1` plus a manual deploy) only when you specifically want full fidelity, and
say so in the report — a run without it has not exercised the KB path.

`--headless` is unaffected: the transform removes the Knowledge Base outright.
Note that both of these parameters are *absent* from the headless template, so the
runner only ever sends them for `--govcloud` — passing one to a headless deploy
would be a CloudFormation `ValidationError`, the same class of mistake as
`AdminEmail`.

`SKIP_DOC_TEST=1` cuts the document-processing time but proves much less; say
which mode was used.

## ⚠️ The caveat you MUST include when reporting a `govcloud` result

Run in a **commercial** account, `govcloud` proves the CloudFront-free /
API-Gateway-hosted template deploys and processes documents. It does **not**
prove GovCloud behaviour — partition-correct ARNs, GovCloud model availability,
and the BDA project rejection are all invisible outside `us-gov-*`. **Two of the
three defects above would not have been caught by a commercial run.**

For a real GovCloud run: `make transform-deploy-test-govcloud REGION=us-gov-west-1`
against a GovCloud account. The script is built to run there unchanged.

Never report a green commercial `govcloud` run as "GovCloud works".

## Not wired into CI

Deliberately on-demand (same reasoning as `stacktest-*`: a ~1h deploy per run,
and these transforms rarely change). The validators and result dicts already
match the probe framework's shape, so wiring in later means calling `run_variant`
from `codebuild_deployment.main` or adding a transform-aware `PROBE_VARIANTS`
row — see the note in `scripts/sdlc/transform_deploy_test.py`.

## How to run and report

1. Confirm the account and region, and tell the user the expected duration.
2. Run the target, streaming output (it is long — run in the background and poll).
3. Report from the printed `TRANSFORM DEPLOY TEST RESULTS` table:
   - PASS/FAIL per variant, the stack name, and every `✓` check that ran;
   - on failure: `failure_type` (`deploy` vs `test`) and the error. A `deploy`
     failure means CloudFormation refused or rolled back — the captured CF events
     name the root cause;
   - whether the **sample document actually processed**, or was skipped via
     `SKIP_DOC_TEST` (a structural-only pass proves much less — say so);
   - the `govcloud` caveat above, unless the region was `us-gov-*`.
4. Confirm teardown. The script always tears down unless `KEEP=1`; if it was
   interrupted, check for a leftover `idp-*-headless` / `idp-*-govcloud` stack and
   its `-iam` companion, and delete both.

## Interpreting failures

- **`deploy` on `headless`** → the transform produced undeployable CloudFormation.
  Check for a surviving reference to something the transform removed (the
  `SuppressAdminInvite` class). The offline gates in
  `lib/idp_sdk/tests/unit/test_template_transform.py` should have caught it —
  if they didn't, add the case there too, because that gate is free and this one
  costs an hour.
- **`deploy` on `govcloud`** → same, plus the GovCloud-only classes if the region
  was `us-gov-*` (partition ARNs, model availability, BDA project shape).
- **`test`** → the stack deployed but a promise was broken: UI resources survived
  `--headless`, CloudFront/LWA survived `--govcloud`, the processing core was
  stripped, or the document did not process. The error names which.
