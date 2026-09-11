---
title: "Feature Platform — Developer Guide"
---
# Feature Platform — Developer Guide

How to build a new **extension** (feature) and add it to the catalog. For the
platform overview and runtime behavior, see
[Feature Platform](feature-platform.md).

## OSS vs Marketplace extensions

An extension is an independent CloudFormation stack that an admin installs into
the same account as the main IDP stack. There are two kinds, distinguished by
the catalog `source` field:

| | **OSS** (`source: oss`) | **Marketplace** (`source: marketplace`) |
|---|---|---|
| Audience | bundled with the open-source accelerator | closed-source, sold via AWS Marketplace |
| Where the template lives | the artifacts bucket, version-free base `<prefix>/extensions/<id>/` | a **per-Region seller bucket** you control, same version-free layout |
| Region-scoped? | no | **yes** — one published copy per supported Region (see below) |
| Catalog entry | `config_library/extensions-oss.yaml` (just a `path`) | `config_library/extensions-marketplace.yaml` (full metadata) |
| Install gate | none — installable directly | advisory entitlement check on the Launch button; the real gate is the Marketplace subscription plus your extension's own runtime check |
| UI CTA | **Install** | **Subscribe** → then **Install** |

Authoring the *feature itself* (manifest + UI bundle + CFN template) is
**identical** for both kinds. Only the **catalog registration** (the last step)
differs. Build and test as an OSS extension first; the Marketplace path layers
on entitlement + a per-Region seller bucket later.

### Marketplace catalog schema (1.1)

```yaml
schemaVersion: "1.1"
features:
  - featureId: idp-auto-optimizer
    displayName: "Auto Optimizer"
    description: "…"
    productCode: "q0k0s3zuuga46hle6fecx547"   # seller-side GetEntitlements
    productId: "prod-a5ee62vs2xa72"           # buyer-side Agreement API
    marketplaceListingUrl: "https://aws.amazon.com/marketplace/pp/prodview-…"
    latestVersion: ""                         # seed/fallback only — see below
    regions:
      us-west-2:
        sellerBucket: aws-ml-blog-us-west-2
        templateKey: artifacts/genai-idp-mp/extensions/idp-auto-optimizer/template.yaml
      us-east-1: { … }
      eu-central-1: { … }
```

Four rules the host relies on:

1. **`regions` is an explicit map, one entry per Region you publish to.** You
   need per-Region copies because `sam package` bakes an absolute,
   Region-specific `s3://bucket/key` `CodeUri` into the published template and a
   Lambda's code bucket must be in the function's Region. The host looks the
   caller's Region up and fails closed when it's missing — it **never derives a
   bucket name** from a basename plus the Region, because bucket names are global
   and guessable and a derived name could belong to somebody else.
2. **`templateKey` is version-free.** A version-bearing key goes stale on a stack
   Update. Versioned artifacts live under `<base>/<version>/` and your feature
   stack self-locates them from its baked `FEATURE_VERSION`.
3. **`latestVersion` is a seed/fallback.** The host reads `<base>/latest.json` at
   runtime for the "Update available" badge, so publishing a new extension
   version does **not** require re-releasing the accelerator. Your publisher
   already writes `latest.json` on every release.
4. **Artifacts must be public-read.** AWS Seller Ops fetches the registered Quick
   Launch template URL during listing review, and CloudFormation fetches the
   template and the Lambda code zips from an arbitrary buyer account at deploy
   time. There is no presign (it could only have covered the template, and its
   expiry broke long-running CFN Update sessions). Your commercial gate is the
   subscription plus a runtime entitlement check inside the extension.

Legacy schema 1.0 (flat `sellerBucket` + `sellerBucketRegion` + `templateKey`) is
still accepted as a deprecated single-Region fallback, and is honored **only for
its own declared Region**. Prefer `regions`.

## 1. Scaffold

```bash
pip install -e lib/idp_feature_sdk
idp-feature-cli init ./my-feature --feature-id my-feature --display-name "My Feature"
```

This copies [`feature-platform/feature-template/`](../feature-platform/feature-template/)
and substitutes the `featureId` / `displayName` / `version` placeholders. The
result is a working feature you can iterate on:

```
my-feature/
├── feature.yaml         # manifest (featureId, displayName, version, description, …)
├── template.yaml        # the feature's CloudFormation stack
├── feature-api/         # optional backend Lambda + HTTP API
├── feature-ui/          # React UMD bundle rendered inside the host UI
│   └── src/{entry.tsx, App.tsx}
└── ui-deployer/         # custom resource: copies the UI bundle into the host
                         # WebUIBucket and registers the feature on Create/Delete
```

## 2. Implement the host contract

Three things make a feature work inside the host. The scaffold wires all three;
you fill in the behavior.

**UI bundle** — `feature-ui/src/entry.tsx` must register the component, and
`vite.config.ts` must externalise the host's shared libraries (React, ReactDOM,
Cloudscape, aws-amplify, react-router-dom) so the bundle shares the host's React
instance:

```ts
window.IdpFeatures.register('my-feature', {
  Component: App,        // receives FeatureContext as props
  version: '0.1.0',      // must match feature.yaml -> version
  displayName: 'My Feature',
});
```

The host-side half of this contract is
[`src/ui/src/components/feature-page/feature-host-globals.ts`](../src/ui/src/components/feature-page/feature-host-globals.ts).

That file also exposes a `window.IdpFeatureHost` helper namespace. Today it
provides `SafeMarkdown` — the host's XSS-sanitizing markdown renderer
(rehype-raw + rehype-sanitize allow-list). Use it to render backend-emitted
markdown/HTML (e.g. rule-validation summaries, which embed `<style>`,
`<colgroup>`, and document-derived content) instead of bundling your own
renderer. The Health Insurance Review sample wraps it in a small
`HostMarkdown` helper that falls back to preformatted text on older hosts —
see `feature-platform/sample-health-insurance-review/feature-ui/src/HostMarkdown.tsx`.

**Backend API (optional)** — if your feature needs a backend, `template.yaml`
creates an HTTP API + Lambda and outputs the endpoint. The ui-deployer writes it
to `InstalledFeatures.featureApiEndpoint`, and the host passes it to your UI as
`FeatureContext.featureApiEndpoint`. Authorize the API against the main stack's
Cognito User Pool (`Fn::ImportValue: <MainStackName>-UserPoolId`); the UI gets a
fresh token via `context.getAuthToken()`.

**Registration** — `template.yaml` must include the `RegisterFeature` custom
resource (provided by the scaffold's `ui-deployer/`) that calls the host AppSync
`registerFeature` mutation on Create/Update and unregisters on Delete. Without
it the feature never appears in the nav.

Validate the manifest against the schema any time:

```bash
idp-feature-cli validate ./my-feature
idp-feature-cli show-schema          # full feature.yaml schema reference
```

### Entitlement enforcement is the extension's job

If you are shipping a **paid** extension, read this before you rely on anything
the host tells you about subscriptions.

The host passes your component a `uiAccessAllowed` boolean, and the reference
samples use it (`disabled={!uiAccessAllowed}`). That is a **UX affordance, not a
licence gate**, and it is important not to confuse the two.

> **Renamed in this release.** This field was called `subscriptionActive`, which
> invited precisely the wrong reading. If your extension destructures
> `subscriptionActive`, rename it — see the upgrade note in `CHANGELOG.md`.

Why it is not a gate:

- It is computed by the host and delivered to **code running in the end user's
  browser**, inside the **customer's own AWS account**.
- It reads `true` whenever `FeaturePlatformSubscriptionMode=auto` (checks off),
  whenever a marketplace simulator is configured, and whenever the live check was
  unreachable and the host allowed rather than locking out a possibly-paying
  customer (`advisory`). All three are under the account admin's control.
- Even with an honest host, a browser boolean is bypassable with devtools, and
  calling your feature's API directly skips it entirely.

Two extra fields let you tell the cases apart:

| Field | Meaning |
|---|---|
| `entitlementSource` | How the host reached its verdict — see the table below |
| `entitlementVerified` | `true` **only** when `entitlementSource` is `marketplace-live` and the state is ACTIVE. That is the only verified source |

The host also returns `licenseMode`, `declaredLicenseMode`, `catalogLicenseMode`
and `licenseModeMismatch` on `checkFeatureEntitlement`. Those are host-side
diagnostics rendered by the host's own feature page; they are **not** added to
`FeatureContext`, so the contract your extension consumes is unchanged.

`entitlementSource` reports **what happened when the host checked**, which is a
different axis from what kind of extension you are (that's the catalog's
`oss` / `marketplace`). One extension yields different sources depending on the
deployment:

| Source | Means | Verified |
|---|---|---|
| `marketplace-live` | Real buyer-side `SearchAgreements` answered — **against real AWS**. Reported only when no `AWS_ENDPOINT_URL_MARKETPLACE_*` override is in effect; the host derives this from the endpoint, not from `FeaturePlatformSubscriptionMode`, so no parameter combination can make a simulator answer claim this source | ✅ |
| `oss` | Not a paid extension — reported in *every* deployment mode | n/a |
| `simulated` | The answer did not come from real AWS Marketplace: either seller-side `GetEntitlements` (which returns an empty list from a buyer account, so it proves nothing), or **any** check made while a Marketplace endpoint override points boto3 at a simulator — including `SubscriptionMode=marketplace-live` | ✗ |
| `advisory` | The live check was **attempted and failed** (missing `aws-marketplace:SearchAgreements`, or the API is unavailable in the Region) and access was allowed rather than locking out a possibly-paying customer | ✗ |
| `auto` | Subscription checks are switched **off** stack-wide | ✗ |
| `none` | No `productCode` registered — state is `NONE`, access denied | ✗ |

There is no separate source for "pre-release": a `Limited`-visibility Marketplace
listing is still a real listing and reports `marketplace-live`. `simulated` means a
simulator, i.e. development.

> **`marketplace-live` cannot be forged by configuration.** The reported source is
> derived from the endpoint the host's call actually used, not from any parameter.
> A simulator-backed check reports `simulated`, so `entitlementVerified` stays
> `false` and your warning still fires. If you match on the union literally, note
> that this **narrows** what `marketplace-live` means; it adds no new value, so no
> code change is required.

### Declare which authority YOU enforce against

Set `marketplace.licenseMode` in your `feature.yaml`:

```yaml
marketplace:
  productCode: <your product code>
  listingUrl: https://aws.amazon.com/marketplace/pp/prodview-XYZ
  licenseMode: marketplace-live   # none | simulated | marketplace-live
```

It is baked into your template at publish time and forwarded to the host through
`registerFeature` at install, where it lands on your `InstalledFeatures` row. The
host **prefers your value over its own catalog entry**, so its check lands on the
same authority you honour rather than on whatever the stack happens to be pointed
at — and it surfaces a mismatch when its catalog disagrees, instead of showing a
simulator-backed "Subscription active" you are going to ignore.

Omitting it means `none`: an extension that says nothing is not claiming to
enforce anything. That default is the opposite of the host catalog's
(`marketplace-live`) on purpose — you must never lock a paying customer out, and
the host must never over-claim verification.

**Or declare it wherever you already keep it.** The host does not care where the
value came from — only that `licenseMode` is in the `registerFeature` payload your
ui-deployer sends, alongside the `productCode` and `marketplaceListingUrl` it
already sends. If your template already holds the mode as a `Mappings` constant
that your functions read, use that and *omit* `marketplace.licenseMode`: two
declarations of one fact is the drift this field exists to detect, and the
mismatch detector would then be firing on a bug you introduced by having two
sources. The manifest route exists because it is where `productCode` and
`listingUrl` already live, not because it is privileged.

This does not change what you enforce or where. Your own runtime check against
your seller-side service remains the authoritative gate; `licenseMode` only tells
the host which authority to agree with.

Use `entitlementVerified` to decide whether to **warn**; never to decide whether
to **serve**.

> **The structural fact.** Software that executes in the customer's own AWS
> account cannot enforce its own licence. The customer owns the Lambda, its
> environment variables, and its code. No host-side design changes this.
> Enforcement requires the **seller** to hold something the customer needs at
> runtime.

What actually works, in descending order of strength:

1. **Seller-hosted activation — [the platform ships a ready-made service for
   this](../feature-platform/seller-entitlement-service/README.md).** Deploy it in
   your own seller account, register your `productId`, and your extension
   exchanges "I am AWS account X" for a short-lived, account-bound signed token.
   Entitlement is validated **in the seller account**, which is the only place
   `SearchAgreements` as `Proposer` answers. (`GetEntitlements` does not answer
   even there for a usage-based listing — verified — so it is only useful for SaaS
   *Contract* products.) The query shape is verified against a live seller account
   with positive and negative controls. Authentication uses API Gateway `AWS_IAM`, so the seller
   learns the caller's *verified* account without knowing buyer accounts in
   advance and buyers need no seller-issued credentials.

   This only bites if the token gates something of real value — a prompt/strategy
   config, a model-routing service, a hosted planner. If it gates nothing valuable
   it is deterrence, not enforcement; the README's threat model is explicit about
   that. Note a paid SaaS listing needs seller-side `BatchMeterUsage` for billing
   anyway, so this channel has to exist regardless — enforcement comes nearly free
   once metering does.
2. **Seller-hosted compute for the valuable part.** Strongest and heaviest.
3. **Detect, don't prevent.** Reconcile activations against subscriptions
   seller-side and handle it commercially.

The host does its part by making unverified access **visible** rather than
silent: the feature page shows a single "Access allowed without a verified
subscription · source: `<source>`" warning — and *only* that one, never alongside
a green "Subscription active" — and the `UnverifiedEntitlementGrant` CloudWatch
metric (namespace `GENAIDP`, dimensions `FeatureId` + `EntitlementSource`) fires
whenever a paid extension is served from `auto`, `advisory` or `simulated`. Note this is **customer-side observability** — it lands in
the customer's CloudWatch, not the seller's — so it helps an admin notice a
misconfigured or unsubscribed stack. It is not revenue protection.

### Optional: pipeline hooks

A feature can run a Lambda at **extension points** in the document-processing
workflow. There are two kinds:

- **`preprocessing`** — a **single** hook that runs FIRST, before the
  BDA/pipeline routing decision (so it fires in both processing modes, even
  with OCR disabled), operating on the *source document*. While it runs the
  document's visible status is `PREPROCESSING`.
- **`postprocessing`** — a **single** hook that runs LAST, after evaluation and
  before the workflow's terminal state (also on the shared tail, so it too fires
  in both processing modes), operating on the *finished document*. Its mutation
  becomes the workflow output, so it reaches the tracking row, reporting, and UI.
- **Five post-step points** — `postOcr`, `postClassification`,
  `postExtraction`, `postRuleValidation`, `postSummarization` — a **list** of
  hooks invoked after the corresponding step, to enrich, validate, or react to
  results mid-pipeline. (`postAssessment` was removed in v0.6 when assessment
  folded into extraction.)

The host's `PipelineHooksDispatcherFunction` invokes registered hooks at each
point; the mechanism is inert until a feature registers one.

**1. Write the hook Lambda.** It's invoked synchronously with:

```json
{ "hookPoint": "postExtraction", "featureId": "my-feature",
  "document": { ... }, "section": { ... }, "executionArn": "arn:aws:states:...",
  "args": [ { "key": "...", "value": "..." } ], "argsMap": { "...": "..." } }
```

`args` is the hook entry's generic key/value settings (string values, opaque to
the platform); `argsMap` is the same list flattened to `{key: value}` for
convenience. Do your work and return any JSON result (surfaced to the workflow
under `$.HookResults`). A `preprocessing` hook may return `"halt": true` to
short-circuit the execution — the document ends in a terminal state instead of
being processed (e.g. `REDACTED_SUPERSEDED` when the hook spawned a redacted
copy). The Lambda **must** be tagged `idp:feature-id=<featureId>` (the host's
dispatcher only invokes tagged or `GENAIIDP-*`-named functions — the scaffold
tags feature Lambdas for you).

#### Writing a mutating hook

A hook can also **change the document** for the next step to consume, which is
how a feature injects business logic into the pipeline rather than just
reacting to it. Return the modified document under `updatedDocument`; use
`idp_common.hooks` so the round-trip is two calls:

```python
from idp_common.hooks import load_hook_document, updated_document_result

def lambda_handler(event, context):
    document = load_hook_document(event)      # resolves compressed refs for you

    # Business logic: anything on the Document model is fair game.
    for section in document.sections:
        if section.classification == "Unknown":
            section.classification = classify_with_my_rules(section)

    return updated_document_result(document, rulesApplied=True)
```

Requires `idp_common[core]` in the hook's `requirements.txt`, plus the
`WORKING_BUCKET` env var (import the host's `<MainStackName>-WorkingBucketName`
export) so compressed documents resolve.

Three things to know:

- **Load, don't build.** Constructing a `Document` from scratch drops
  `metering`, `errors`, `hitl_metadata`, and `processing_issues`. Always
  load → mutate → return.
- **Omitting `updatedDocument` changes nothing.** The document passes through
  byte-identical, so read-only hooks need no changes.
- **`postExtraction` is section-scoped** (it runs inside the section Map), so
  only section-level changes propagate there. Use `postClassification` or
  `postRuleValidation` for whole-document changes.
- **`postprocessing` has nothing downstream**, so its mutation matters only
  because it *is* the workflow output — that is what the tracker persists to
  DynamoDB, reporting, and the UI. It cannot set a terminal `status`, though: a
  successful execution is forced to `COMPLETED`.
- **Make mutations idempotent.** The workflow retries a hook dispatch on
  transient Lambda faults, so a mutation that *appends* can apply twice while
  one that *sets* is safe.

The dispatcher refuses an update that changes the document's identity, breaks
the `sections` list the Map iterates, or exceeds 5 MB inline — keeping the
pre-hook document and recording the reason under
`$.HookResults.<point>.Payload.results[].documentUpdateRejected` rather than
failing the workflow. Full contract and the per-point propagation table:
[Feature Platform → Pipeline hooks](feature-platform.md#pipeline-hooks).

**2. Declare it in `template.yaml` + the manifest.** Add the hook Lambda to your
feature's CloudFormation template, then map the hook point to that Lambda's
logical resource name in `feature.yaml`:

```yaml
# feature.yaml
pipelineHooks:
  postExtraction: MyExtractionHookFunction   # logical resource name in template.yaml
```

**3. Register at install (primary path).** The feature stack resolves those
logical names to ARNs and calls the host's `registerFeatureHooks` mutation on
Create (and clears them on Delete) — the same custom-resource pattern as
`registerFeature`. The host writes them into the active config version's
`<step>.postHook` lists. Each entry is
`{ featureId, arn, order (default 100), onError (default continue), enabled, args }`;
`onError` is `continue` | `skip-remaining` | `fail`.

**`preprocessing` / `postprocessing` shape.** Unlike the post-step lists, these
two are standalone top-level config sections each holding ONE flat hook (its
fields live directly on the section, no list), editable in the View/Edit
Configuration UI:

```yaml
preprocessing:
  enabled: true               # default false
  featureId: pii-anonymizer   # owner label (for traceability)
  arn: <hook-lambda-arn>
  onError: fail               # continue | fail
  args:
    - { key: mode, value: redactcopy_and_stop }

postprocessing:
  enabled: true               # default false
  featureId: my-delivery
  arn: <hook-lambda-arn>
  onError: continue           # continue (recommended) | fail
  args:
    - { key: endpoint, value: "https://erp.example/ingest" }
```

For `preprocessing`, `onError: fail` is terminal: a failed hook ends the
execution in a `PreprocessingHookFailed` Fail state and **never** falls through
to processing the un-preprocessed original (essential when the hook gates
processing, e.g. PII redaction). Use `fail` whenever the hook must gate.

For `postprocessing`, keep the `continue` default unless the hook truly gates
delivery — `fail` marks a document FAILED after every processing step already
succeeded. Two more things to know about this point:

- It runs **inside** the execution, so a slow hook extends per-document latency
  and holds a concurrency slot. For fire-and-forget delivery use the
  [EventBridge post-processing hook](post-processing-lambda-hook.md) instead.
- It fires **while HITL review is pending** (and again after review completes),
  so branch on the document's `hitl_status` / `hitl_triggered` /
  `hitl_sections_pending` fields — remembering they are omitted when falsy, so
  absent means "no HITL" — and make the hook idempotent.

Because each of these points holds a *single* hook, only one feature can own it:
registering over another feature's hook is refused rather than silently
disabling it.

**Escape hatch (no feature install).** For custom business logic outside the
feature-install flow, an admin can add `postHook` entries — or fill in the
`preprocessing` / `postprocessing` sections — in a config version directly (same
shapes as above).
The hook Lambda still needs the `idp:feature-id` tag or a `GENAIIDP-*` name to
clear the dispatcher's IAM check. This is handy for one-off integrations, but
installable features should use `registerFeatureHooks` so hooks are
added/removed with the stack.

**Verifying a hook actually fired.** Asserting on your feature's own output
cannot distinguish "the hook ran and decided nothing" from "the hook was never
invoked" — from the feature's data store the two look identical. The dispatcher
reports both the count and the config version it resolved, so check those
instead:

```python
# From the workflow execution history, at the hook state's exit:
payload = output["HookResults"]["postRuleValidation"]["Payload"]
assert payload.get("invoked", 0) > 0, "the dispatcher never invoked the hook"
assert payload["configVersion"] == expected_version
```

`invoked: 0` with a `configVersion` you did not expect means the hooks are
registered in a different config version than the one the document resolved —
activate the right version, or pin it per document via `config_version`.

See [Feature Platform → Pipeline hooks](feature-platform.md#pipeline-hooks) for
the full contract.

### Optional: ship a configuration preset

A vertical feature often needs a specific accelerator configuration (custom
document classes, extraction prompts, rule-validation policy classes, …). A
feature can **bundle that configuration** and apply it at install:

**1. Add the preset file and declare it in the manifest.**

```yaml
# feature.yaml
configPreset:
  path: config-preset/my-config.yaml   # repo-relative; uploaded verbatim by the publisher
```

**2. Apply at install.** The feature stack's ui-deployer downloads the preset
and calls the host's `applyFeatureConfigPreset` operation, which writes it as a
**non-active [Configuration Profile](configuration-profiles.md) named after the
feature**: `<featureId>`, with no version suffix.

**One profile per feature, one revision per release.** This used to be
`<featureId>-v<version>` — a new *profile* for every release, and a profile is an
access-control object (an admin has to add it to every scoped user's
`allowedConfigVersions`), a document-visibility partition, and a permanent row in
the Configuration Profiles table. Twelve releases meant twelve of those. Your
feature's releases are now revisions of one profile, visible in its **Revision
history**, and an upgrade that does not change the preset records nothing.

The preset is merged over the host's `Config#default` at install time and stored
as a full configuration, so the recorded revision is the same shape a later admin
edit produces — otherwise the first diff after an admin's edit would show the
entire configuration as new.

Installation **never changes the active configuration** — an admin reviews the
preset on the **Configuration** page and activates it deliberately. On uninstall
the feature calls `removeFeatureConfigPreset`, which deletes `<featureId>` and any
legacy `<featureId>-v*` profiles together with their revision history. If the
feature's profile is active, `default` is activated first — a feature's config
carries its hooks inline, so leaving it active after uninstall would leave every
document pointing at deleted Lambdas. The one case where the profile survives is
an active profile with no `default` to fall back to: no active configuration fails
*all* processing, which is worse than dangling hooks.

This pairs naturally with pipeline hooks: ship the configuration the vertical
needs *and* the hook that reacts to its results.

### Host exports for features that read processing results

Features that read pipeline output import these host exports (in addition to
the always-available `<MainStackName>-TrackingTableName` and
`-CustomerManagedEncryptionKeyArn`):

| Export                              | For                                                            |
| ----------------------------------- | -------------------------------------------------------------- |
| `<MainStackName>-OutputBucketName`  | Reading processed-document results (e.g. consolidated summaries) |
| `<MainStackName>-WorkingBucketName` | Loading the compressed document payload a pipeline hook receives |
| `<MainStackName>-DiscoveryBucketName` | Driving the host's Rules Discovery flow from a feature UI     |

## 3. Add to the catalog

This is the step that makes the feature discoverable. Choose based on kind.

**OSS** — add the project directory to
[`config_library/extensions-oss.yaml`](../config_library/extensions-oss.yaml):

```yaml
features:
  - path: feature-platform/sample-feature
  - path: feature-platform/my-feature        # ← your feature (committed to the repo)
```

`idp-cli publish` then builds it and emits a `source: oss` catalog entry
automatically. UI metadata comes from your `feature.yaml`.

**Marketplace** — publish the feature artifacts to your seller bucket **in every
Region you support** (see step 4), create the AWS Marketplace listing, then add an
entry to
[`config_library/extensions-marketplace.yaml`](../config_library/extensions-marketplace.yaml):

```yaml
features:
  - featureId: my-feature
    displayName: "My Feature"
    description: "One-line description shown in the nav and on the feature page."
    productCode: "<marketplace-product-code>"   # seller-side GetEntitlements
    productId: "prod-<id>"                      # buyer-side Agreement API
    marketplaceListingUrl: "https://aws.amazon.com/marketplace/pp/<id>"
    latestVersion: "0.1.0"                      # seed/fallback; latest.json is authoritative
    regions:
      us-east-1:
        sellerBucket: "<your-seller-bucket-us-east-1>"
        templateKey: "extensions/my-feature/template.yaml"   # version-free
      us-west-2:
        sellerBucket: "<your-seller-bucket-us-west-2>"
        templateKey: "extensions/my-feature/template.yaml"
```

See [Marketplace catalog schema (1.1)](#marketplace-catalog-schema-11) for the
four rules the host relies on. In short:

`templateKey` is **version-free**: each publish overwrites
`extensions/<id>/template.yaml`, and its directory `extensions/<id>` is the
version-free base the host passes to the feature stack as `FeatureArtifactPrefix`.
Versioned artifacts (UI bundle, config preset, agent source) live under
`extensions/<id>/<version>/`; the stack derives the `<version>` subfolder from its
baked `FEATURE_VERSION`, so no version-bearing value is stored as a stale-able CFN
parameter.

One entry per Region is required rather than one entry with a bucket basename,
because `sam package` bakes an absolute, Region-specific `s3://` `CodeUri` into
the published template. The host resolves the Region by **lookup only** and fails
closed ("not available in `<region>`") when it's absent — it never derives a
bucket name, since bucket names are global and a guessed name could belong to
someone else.

Your artifacts must be **public-read** (the AWS Seller Ops review and the buyer's
CloudFormation both fetch them from outside your account), so the host needs no
seller-bucket grant to launch. `SellerBucketObjectArns` is only needed if you
keep a **private** artifacts bucket and want the host to still read `latest.json`
for update badges via a signed request.

> **Unadvertised features.** A catalog entry only adds a feature to the
> *available-to-install* list. A feature deployed directly via CloudFormation
> self-registers (its `RegisterFeature` custom resource writes to the
> `InstalledFeatures` table) and appears in the **Extensions** nav once
> installed — no catalog entry needed. Use this for private/internal features
> you don't want surfaced as installable to every admin.

### Nav visibility before install (`showInNav`)

A catalog feature that is **not yet installed** gets its own entry in the
**Extensions** side nav (with an Install or Subscribe badge) by default. Set
`showInNav: false` — in `feature.yaml` for OSS features, or on the entry in
`extensions-marketplace.yaml` for marketplace features — to keep it off the
nav until it's installed; it stays discoverable on the **Browse catalog** page
(`/features`). The two bundled reference samples set `showInNav: false` so
fresh deployments don't advertise them in the nav. Installed features always
get a nav entry regardless of this flag.

### Feature documentation (the "Learn more" link)

Each feature can expose a **Learn more** link, shown in its nav hover tooltip
and on its not-yet-installed page. It's driven by the manifest/catalog
`docsUrl` field, with a fallback:

- **OSS features** — write a markdown doc under `docs/extensions/<slug>.md` and
  set `docsUrl: extensions/<slug>` in `feature.yaml`. `make docs-deploy`
  publishes `docs/extensions/*.md` to the **Extensions** section of the docs
  site, and the UI resolves the slug to that published page. (The bundled Demo
  Extension is the worked example: [`docs/extensions/sample-document-status.md`](extensions/sample-document-status.md),
  `docsUrl: extensions/sample-document-status`.) An absolute `https://…` URL also works.
- **Marketplace features** — closed-source docs aren't in this repo's docs
  site, so omit `docsUrl` and the UI uses your `marketplaceListingUrl` (the AWS
  Marketplace listing already hosts usage instructions). If you'd rather link
  to your own hosted docs, set an absolute `docsUrl` in
  `extensions-marketplace.yaml`.

## 4. Build & publish artifacts

```bash
idp-feature-cli build ./my-feature                     # build + validate the UMD UI bundle
idp-feature-cli publish ./my-feature \                 # sam package + upload + latest.json + Launch URL
    --bucket-basename <bucket> --region us-east-1        # region appended automatically (matches `idp-cli publish`)
```

- **OSS**: artifacts ride along with the accelerator's normal `idp-cli publish`;
  the catalog is regenerated and deployed into the host's ConfigurationBucket on
  the next stack create/update.
- **Marketplace**: publish once **per supported Region** (`--region` each time),
  with `--public` so the artifacts are readable by AWS Seller Ops and by
  CloudFormation in the buyer's account. Ensure every Region you published to has
  a matching entry under `regions:` in the catalog. `latestVersion` in the
  catalog is only a fallback — the `latest.json` your publisher writes is what
  customers actually see, so an extension release needs no host release.

### Iterate one extension against a running host (`deploy`)

To push a single extension from source into an **already-running** host stack —
without redeploying the whole accelerator or clicking the console Launch URL —
use `idp-feature-cli deploy` (the per-extension analogue of `idp-cli deploy`):

```bash
# A) From source — publish then deploy (the inner dev loop):
idp-feature-cli deploy --from-code ./my-feature \
    --host-stack-name IDP-FeaturePlatform
# --region defaults to the AWS session region (like `idp-cli deploy`)
# --bucket-basename defaults to idp-accelerator-artifacts-<account>-<region>
# --wait (opt-in) blocks until the feature stack reaches a terminal state

# B) From an already-published template — no rebuild (the feature bucket is
#    parsed from the URL unless --bucket-basename is given):
idp-feature-cli deploy \
    --template-url https://<bucket>.s3.<region>.amazonaws.com/extensions/<id>/template.yaml \
    --host-stack-name IDP-FeaturePlatform
```

`--from-code` and `--template-url` are mutually exclusive (mirroring
`idp-cli deploy`'s `--from-code` / `--template-url`); pass exactly one.

> The `--from-code` path requires the **AWS SAM CLI** (`sam`) on `PATH`: both
> `publish` and `deploy --from-code` run `sam build` + `sam package` to rewrite
> the template's Lambda `CodeUri:` paths to `s3://...` (CloudFormation runs the
> SAM transform server-side when deploying via TemplateURL and rejects local
> paths). The `--template-url` path needs neither SAM nor Docker.

With `--from-code` it publishes the feature (version-free layout, version +
artifact-prefix tokens baked into the template); either way it then
create-or-updates the feature stack
`<host-stack-name>-feature-<feature-id>` — the same name the host's
`getFeatureLaunchUrl` resolver uses for a console install, so re-running it
upgrades that stack in place rather than creating a duplicate (override with
`--stack-name`). The template's `RegisterFeature` custom resource runs on every
deploy, self-registering the feature and copying its UI bundle into the host's
WebUIBucket — exactly as a console install does. This is the recommended inner
loop when developing an extension against a live deployment.

How the catalog reaches the host: `idp-cli publish` writes a single
`catalog.json` (merging both `extensions-*.yaml` files) under `config_library/`;
at deploy time it is copied into the stack's own ConfigurationBucket, and the
host reads it at runtime with one `GetObject` — no bucket listing, no
artifacts-bucket dependency post-deploy. See
[Feature Platform → Catalog & discovery](feature-platform.md#catalog--discovery).

## 5. Local end-to-end test (no real Marketplace)

Use the standalone marketplace-simulator (shipped separately from the OSS repo)
to exercise the Subscribe → Install → Active flow without a real listing:

1. Deploy/run the simulator and note its endpoint.
2. Publish with simulator registration:
   ```bash
   idp-feature-cli publish ./my-feature \
       --bucket-basename <bucket> --region us-east-1 \
       --register-with-simulator <simulator-endpoint> \
       --simulator-product-code <product-code>
   ```
3. Deploy the main stack with `EnableFeaturePlatform=true`,
   `FeaturePlatformSimulatorEndpoint=<simulator-endpoint>`, and the
   `FeaturePlatformSubscriptionMode` matching the API your simulator implements
   (`marketplace-live` for `SearchAgreements`, `marketplace` for
   `GetEntitlements`).
4. Open the IDP web UI — your feature appears under **Extensions**.

With `FeaturePlatformSubscriptionMode=auto` OSS features skip the subscription
step entirely and go straight to **Install**.

### Simulator fidelity contract

The simulator's job is to fail the way AWS fails. Where it diverges, it doesn't
just under-test — it *actively certifies* designs that break in production, and
it does so in the silent direction: green in dev, silently denying every customer
in prod.

The canonical example, and the reason this section exists: a simulator that makes
**`GetEntitlements` succeed for a buyer-side caller reproduces the opposite of
real behavior.** Against real AWS, that call returns HTTP 200 with
`{"Entitlements": []}` — never an error — because it is a seller-side API *and*
because entitlement records exist only for SaaS **Contract** products (a
usage-based SaaS Subscription has none). An entitlement gate built and "verified"
against a generous simulator therefore denies every real customer while logging
nothing.

A faithful simulator should implement, in priority order:

1. **Buyer-side `marketplace-agreement:SearchAgreements`** — the API the host
   actually calls in production (`marketplace-live`). Response shape:
   `agreementViewSummaries[].{agreementId, status, startTime, endTime,
   proposalSummary.resources[].{id, type}, proposalSummary.offerId}` with
   `status` in `ACTIVE | CANCELLED | …`. Point the host at it with
   `AWS_ENDPOINT_URL_MARKETPLACE_AGREEMENT` (botocore derives that name from the
   service model, so no client code is needed). Without this, `marketplace-live`
   has no development coverage at all.
2. **Filter-combination validation.** Real `SearchAgreements` rejects
   unsupported combinations with `ValidationException` ("Provided combination of
   filters is not supported"), which is a genuine failure mode worth reproducing.
   The combination the host uses is `PartyType=Acceptor` +
   `AgreementType=PurchaseAgreement` + `ResourceIdentifier=<productId>` +
   `Status=ACTIVE`.
3. **`ResourceIdentifier` keys on the product ENTITY id** (`prod-…`), not the
   product code. A simulator that only models product codes cannot serve this
   filter — which is why catalog schema 1.1 carries `productId` separately.
4. **Pricing model on product registration.** `POST /admin/products` should
   accept the pricing model (`FREE` / usage-based / contract) and offer terms,
   because that is what determines whether entitlements exist at all.
5. **Honest `GetEntitlements`.** Return 200-with-empty-list for a product
   registered as usage-based/no-contract, and for any caller that is not the
   registered seller account. This turns the simulator from a trap into a
   teaching tool: a developer who builds a `GetEntitlements` gate sees it fail in
   dev exactly as it would in prod.
6. **Separate buyer and seller identities.** The structural fact behind all of
   the above: seller-side APIs are signed by the seller account, buyer-side by
   the buyer. Conflating them is what makes divergence 5 possible.
7. *(Optional)* **Discovery API** — `GetListing` / `GetOffer` / `GetOfferTerms`.
   This is how a developer confirms product identity and pricing model without
   guessing, and it is cheap to stub.

To check any of this against real AWS from a buyer account, read-only:

```bash
scripts/marketplace/verify_entitlement.sh <productCode> <productId> [listingId]
```

It runs the seller-side and buyer-side calls side by side with a positive
control, so "empty because not subscribed" is distinguishable from "empty because
the API can never answer this".

## Reference

- [Feature Platform overview](feature-platform.md)
- [`idp_feature_sdk` README](../lib/idp_feature_sdk/README.md) — CLI + library API
- [`feature-platform/feature-template/`](../feature-platform/feature-template/) — the scaffold you start from
- [`feature-platform/sample-feature/`](../feature-platform/sample-feature/) — minimal reference OSS feature (`docs-by-status`): UI + API + registration only
- [`feature-platform/sample-health-insurance-review/`](../feature-platform/sample-health-insurance-review/) — advanced reference OSS feature (`sample-health-insurance-review`): adds a config preset, a `postRuleValidation` pipeline hook, and host-GraphQL calls from the UI ([docs](extensions/sample-health-insurance-review.md))
- [`feature-platform/confbench-testset/`](../feature-platform/confbench-testset/) — reference for a **long-running background job** owned by an extension (`confbench-testset`): a Step Functions `Map` ingest that streams a 32.71 GB dataset into the host's Test Set bucket, a shared-catalog Lambda layer (with the SAM `BuildMethod` gotcha documented), and an Admin-gated feature API. Useful pattern when work is too big or too slow to sit inside a CloudFormation custom resource ([docs](extensions/confbench-testset.md))
- Manifest schema: `lib/idp_feature_sdk/idp_feature_sdk/schemas/feature-manifest.schema.json` (or `idp-feature-cli show-schema`)
