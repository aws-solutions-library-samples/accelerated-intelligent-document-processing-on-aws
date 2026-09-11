---
title: "Feature Platform"
---
# Feature Platform

> **Status: preview.** The Feature Platform is the *framework* for installable
> extensions; it is still being built out. Today it ships with a single bundled
> demo extension so you can see the mechanism end-to-end — more open-source
> extensions will follow. **AWS Marketplace–delivered (paid) extensions are a
> future capability: the framework supports them, but none exist yet.**
>
> The platform is **on by default** (`EnableFeaturePlatform=true`) in
> **auto-subscribe** mode — every catalog extension is installable directly,
> with no entitlement checks (the only mode exercised today). The
> Marketplace/entitlement path (`FeaturePlatformSimulatorEndpoint`,
> Subscribe → Active flow) is wired but unused until paid extensions ship. Set
> `EnableFeaturePlatform=false` to remove the platform entirely.

The Feature Platform turns the IDP Accelerator main stack into a **host** for
*installable extensions* — add-ons that are discovered and installed at runtime
without rebuilding the host. It is designed to grow into a catalog of
extensions over time.

A "feature" is an independent CloudFormation stack that an admin launches into
the **same AWS account** as the main IDP stack. Once the feature stack creates,
a custom resource uploads the feature's UI bundle into the main stack's
`WebUIBucket` and registers itself in the `InstalledFeatures` DynamoDB table.
From that moment on the feature appears as a new nav item inside the existing
IDP web UI, with its own page backed by a UMD-loaded React bundle.

## Deployment modes

Two **independent** parameters: `FeaturePlatformSubscriptionMode` chooses *which*
API answers "is this account subscribed?", and
`FeaturePlatformSimulatorEndpoint` chooses *where* that call goes. They used to
be coupled — the mode was inferred from whether an endpoint was set — which made
the production path unreachable whenever a simulator was configured, so it could
never be exercised in development.

| `FeaturePlatformSubscriptionMode` | API called | Notes |
|---|---|---|
| `marketplace-live` *(default)* | **Buyer-side** `marketplace-agreement:SearchAgreements` | The production path. See [Subscription checks](#subscription-checks-for-paid-extensions). |
| `marketplace` | **Seller-side** `marketplace-entitlement:GetEntitlements` | Only meaningful against a simulator — the real API returns an empty list from a buyer account. |
| `auto` | none | Every catalog extension is treated as subscribed; the UI goes straight to Install. |

`EnableFeaturePlatform=false` removes the platform entirely (no resources
created), regardless of the above.

| `FeaturePlatformSimulatorEndpoint` | Effect |
|---|---|
| `''` *(default)* | Calls go to real AWS Marketplace. |
| `https://…` | Base URL of a marketplace-simulator. Used for the buyer-console redirect **and** as the endpoint override for the emulated AWS APIs (`AWS_ENDPOINT_URL_MARKETPLACE_AGREEMENT` / `…_MARKETPLACE_ENTITLEMENT_SERVICE`), so a simulator can back either mode. |

The marketplace simulator is **not** bundled with the open-source distribution.
It is shipped separately and can be bolted onto a running stack with no rebuild.
See [Simulator fidelity contract](feature-platform-developer-guide.md#simulator-fidelity-contract)
for what a faithful simulator has to implement — in particular, a simulator that
makes `GetEntitlements` *succeed* for a buyer-side caller reproduces the opposite
of real behavior and will validate a design that fails silently in production.

## Two kinds of extensions

| | **OSS extension** | **Marketplace extension** |
|---|---|---|
| `source` | `oss` | `marketplace` |
| Example | `docs-by-status`, `sample-health-insurance-review` (the bundled samples) | `idp-auto-optimizer` (Auto Optimizer) |
| Where the template lives | the artifacts bucket, under a version-free `<prefix>/extensions/<id>/` base | a **per-Region seller bucket**, under a version-free base — see [Region availability](#region-availability) |
| Region-scoped? | no — published with the host | **yes** — one published copy per supported Region |
| Subscribe step | none — installable directly | UI links to the AWS Marketplace listing; buyer subscribes there |
| How `getFeatureLaunchUrl` produces the template URL | bare public S3 HTTPS URL | bare public S3 HTTPS URL for the **Region's** bucket, looked up in the catalog's `regions` map (no presign) |
| Install gate | none | advisory entitlement check on the Launch button; the real gate is the subscription + the extension's runtime check |

## Catalog & discovery

Discovery is **manifest-driven** — the host never lists buckets (the artifacts
and seller buckets permit `GetObject` only, not `ListObjectsV2`).

- A single **`catalog.json`** lists every feature, OSS and marketplace, with
  the metadata the UI needs (displayName, version, `source`, and — for
  marketplace features — `productCode` + `marketplaceListingUrl`).
- `catalog.json` is produced by **`idp-cli publish`**, which merges the
  open-source features it bundles with the curated closed-source list in
  **`config_library/extensions-marketplace.yaml`** (the single checked-in source
  of truth for marketplace extensions).
- At **deploy time** the main stack's `ConfigurationCopyFunction` copies
  `catalog.json` (with the rest of `config_library/`) into the stack's own
  **ConfigurationBucket**. At **runtime** `listCatalogFeatures` reads it from
  ConfigurationBucket with one `GetObject` — so the **deployed stack does not
  depend on the artifacts bucket** for the catalog.
- To add a marketplace extension: add an entry to
  `config_library/extensions-marketplace.yaml`, re-publish, and run a stack update
  (the catalog is refreshed into ConfigurationBucket on create/update). The
  feature then appears in the "Extensions" nav with a Subscribe CTA (unless
  its entry sets `showInNav: false`, in which case it's discoverable under
  **Extensions → Browse catalog** only until installed — the bundled reference
  samples do this).

### Region availability

Marketplace extensions are **Region-scoped**. `sam package` bakes an absolute,
Region-specific `s3://bucket/key` `CodeUri` into the published template, and a
Lambda's code bucket must live in the function's own Region — so each supported
Region needs its own published copy. Catalog schema **1.1** therefore maps each
Region to an *explicit* bucket + template key:

```yaml
regions:
  us-west-2:    { sellerBucket: aws-ml-blog-us-west-2,    templateKey: artifacts/genai-idp-mp/extensions/idp-auto-optimizer/template.yaml }
  us-east-1:    { sellerBucket: aws-ml-blog-us-east-1,    templateKey: … }
  eu-central-1: { sellerBucket: aws-ml-blog-eu-central-1, templateKey: … }
```

`getFeatureLaunchUrl` **looks the caller's Region up** in that map and fails
closed ("not available in `<region>`") when it's absent, and
`listCatalogFeatures` reports `availableInRegion` / `availableRegions` so the UI
shows that up front instead of a Subscribe button that dead-ends.

> **The host never derives a bucket name** by concatenating a basename with the
> Region. S3 bucket names are global and guessable, so a derived name in a Region
> we don't publish to could resolve to a bucket somebody else owns — and the
> customer would be handed a CloudFormation template we did not write. This is a
> security property, not tidiness.

`templateKey` is **version-free** and must stay that way: a version-bearing key
goes stale the moment a customer runs a stack Update. Versioned artifacts live
under `<base>/<version>/` and the feature stack self-locates them from the
`FEATURE_VERSION` baked into its template at publish time.

Marketplace artifacts are **public-read**, which is forced rather than chosen:
the registered Quick Launch template URL is fetched by AWS Seller Ops during
listing review and by CloudFormation in an arbitrary buyer account, and the
Lambda code zips are fetched from the buyer's account at deploy time. There is
therefore **no presign** — it could only ever have covered the template, while
its expiry broke long-running CFN "Update stack" sessions. The commercial gate is
the Marketplace subscription plus the extension's own runtime entitlement check;
the host's entitlement check on the Launch button is an **advisory UX gate**.

### Subscription checks for paid extensions

Set by `FeaturePlatformSubscriptionMode` (only consulted when no simulator
endpoint is configured):

| Mode | What it does |
|---|---|
| `marketplace-live` *(default)* | Queries the **buyer-side** AWS Marketplace Agreement API (`SearchAgreements`) for an ACTIVE `PurchaseAgreement` on the extension's `productId`. |
| `auto` | Skips the check; every catalog extension is treated as subscribed. |

**Why not `GetEntitlements`?** It's the obvious API and it does not work here —
in the most misleading way possible. Called from a buyer account it returns HTTP
200 with an *empty* list rather than an error, for two independent reasons:

1. It's a **seller-side** API. AWS's SaaS guidance is that these calls "must be
   signed by credentials from your AWS Marketplace Seller account".
2. Entitlement records only exist for SaaS **Contract** products. A usage-based
   SaaS *Subscription* meters instead and has no entitlements at all. Verified
   against the live listing **from the seller account** with the correct product
   code — it still returns an empty list, so this is a property of the pricing
   model, not a permissions problem.

A fail-closed gate on that would deny every legitimate customer while logging
nothing — and would look perfectly healthy in CI against the simulator. AWS
License Manager (`ListReceivedLicenses`) was also ruled out: it fails with
`AccessDeniedException: Service role not found` until a service role is created
in the buyer account, which we can't require of a customer.

Verified against the live listing across **every** caller/subscription combination:

| Caller | Subscribed to this product? | `GetEntitlements` | `SearchAgreements` |
|---|---|---|---|
| Buyer account | no | `[]` | `[]` (correct negative) |
| Buyer account | **yes** | **`[]`** | **ACTIVE agreement** (correct positive) |
| **Seller** account | n/a (product owner) | **`[]`** | ACTIVE agreement via `PartyType=Proposer` |

`GetEntitlements` returns an empty list in **every** case — including from the
seller account, and including for a genuinely subscribed buyer. There is no
configuration in which it answers this question for a usage-based SaaS listing,
because such a listing has no entitlement records at all. `SearchAgreements`
answers correctly in every case.


The three outcomes are deliberately distinct:

| Outcome | State | Meaning |
|---|---|---|
| ACTIVE agreement found | `ACTIVE` (`source: marketplace-live`) | Subscribed. |
| Call succeeded, nothing matched | `NONE` (`source: marketplace-live`) | Authoritative — `SearchAgreements` is scoped to the caller's own account. UI shows Subscribe. |
| Call **errored** | `ACTIVE` (`source: advisory`) | Indistinguishable from "not subscribed", so we allow and log loudly. |

That last row is the important one: failing closed on a permissions gap or an
unsupported partition would lock a paying customer out of an extension they
bought. Each paid extension performs its own runtime entitlement check, so a
permissive host gate costs nothing while a restrictive one is a support incident.
For the same reason, the host's check on the Launch button is **advisory**.

> **Known false-negative.** If an AWS Organization holds the subscription in the
> management account while the IDP stack runs in a member account,
> `SearchAgreements` from the member account reports nothing. `NONE` therefore
> routes to Subscribe rather than hard-blocking.

To reproduce any of this against a live account:

```bash
scripts/marketplace/verify_entitlement.sh          # Auto Optimizer defaults
scripts/marketplace/verify_entitlement.sh <productCode> <productId> [listingId]
```

It runs both APIs side by side with a positive control, so an empty result is
distinguishable from a broken one.

### The authority is per extension, not per stack

Each extension declares a **`licenseMode`** naming the authority that must confirm
its subscription — `none`, `simulated`, or `marketplace-live` — and one stack
resolves different extensions against different authorities:

| Mode | Who answers | Reported source |
|---|---|---|
| `marketplace-live` | buyer-side `SearchAgreements` against **real AWS** | `marketplace-live` (verified) |
| `simulated` | seller-side `GetEntitlements` against the stack's marketplace-simulator | `simulated` |
| `none` | nobody | `oss` for an OSS extension, `auto` for a paid one |

That is what lets one stack host a **listed, published** extension confirmed
against real AWS Marketplace *alongside* **in-development** extensions checked
against a simulator, and OSS extensions checked against nothing. Choosing the
authority once for the whole stack made that impossible: pointing the stack at a
simulator pointed it there for everything, including a live listed product — so
the host showed a simulator-backed "Subscription active" for an extension that
only honours real Marketplace, and the extension correctly disagreed.

It is declared in two places, deliberately:

| Where | Governs |
|---|---|
| `config_library/extensions-marketplace.yaml` | the **host's** check for that extension |
| the extension's own `template.yaml` (from its `feature.yaml` manifest) | the **extension's** own check |

The extension's value is propagated to the host through `registerFeature` at
install and stored on the `InstalledFeatures` row, which is what makes the
catalog entry verifiable rather than aspirational: the host prefers it, so its
check lands on the authority the extension actually honours, and it reports a
mismatch when the two disagree.

**Resolution order** for a feature's host-side authority:

1. `licenseMode` on the `InstalledFeatures` row (propagated at install), else
2. `licenseMode` in the catalog entry, else
3. the legacy stack-wide `FeaturePlatformSubscriptionMode`, else
4. `marketplace-live` for a marketplace catalog entry / `none` for OSS.

Steps 2–3 are what keep an existing stack working: current catalogs always carry
an explicit value (publish.py bakes one), so step 3 is reached only by a
catalog.json published before the field existed.

**The two defaults are deliberately opposite.** Missing on the *host* side means
`marketplace-live`; missing on the *extension* side means `none`. The failure
directions differ: an extension must never lock a paying customer out of something
they bought, so it degrades to serve-and-declare; a host must never *over-claim*
verification for something in the marketplace catalog, so it degrades to the
strictest authority. Please keep both.

`licenseMode` is **not** inferred from `marketplaceListingUrl`. That would break
the simulator dev loop for paid extensions — the case this design exists to
support, since a listed product still has to be developed against a simulator
before release — and listing-URL presence tracks "somebody filled in the entry
template", not "this listing is live".

**What stays stack-scoped:** where the simulator *lives*
(`FeaturePlatformSimulatorEndpoint`), and a kill switch
(`FeaturePlatformSubscriptionMode=auto`, "check nothing on this stack"). Location
is a property of the deployment; authority is a property of the extension.

**When the two disagree,** the host warns rather than enforcing on the extension's
behalf: it stops *claiming* an authority the extension does not use — no green
"Subscription active" sourced from a simulator for that extension, and no
simulator Subscribe button for it — but it does not add a second gate. Two
independent gates that can disagree is the original problem in mirror image, and
the extension's own gate is already the answer.

> **Anything simulator-backed counts as UNVERIFIED — including a
> `marketplace-live` extension whose call was redirected.** A feature's
> `licenseMode` chooses *which API* the host calls;
> `FeaturePlatformSimulatorEndpoint` says *where a simulated call goes*. Only the
> endpoint the call actually used decides what the host may claim: an answer from
> anything other than real AWS is reported as `entitlementSource: simulated`. That reports `entitlementVerified: false`, raises the "Access allowed
> without a verified subscription" banner, and fires the
> `UnverifiedEntitlementGrant` metric — the same treatment as `auto` and
> `advisory`. Only a `marketplace-live` check against **real AWS** counts as
> checked. Expect the banner in development; if you see it in production, the
> stack is pointed at a simulator.
>
> Deriving this from the endpoint rather than from a parameter is deliberate:
> previously the source was the `FeaturePlatformSubscriptionMode` parameter, so
> `marketplace-live` plus a simulator endpoint reported simulator answers as a
> *verified live Marketplace check* — silently fooling any extension following the
> documented advice to trust `entitlementVerified`. The live authority is now
> pinned to real AWS by construction, and the reported source is anchored to the
> endpoint each individual call used, so the invariant survives a stack that
> resolves different extensions against different authorities.
>
> **Developing against the simulator.** The bundled marketplace-simulator
> implements a subset of the Agreement API: it rejects the buyer-side `PartyType`
> filter, and it records agreements under the product **code** rather than the
> product entity id (its buyer console is keyed on `productCode`). The resolver
> accommodates both — but only when an endpoint override is in effect, since real
> AWS accepts `PartyType` and rejects the reduced filter set outright. The
> production query is therefore never weakened, and nothing found via those
> retries can be reported as verified.

### "Update available" badges

The badge compares the version in the `InstalledFeatures` table against the
extension's **live** `latestVersion`, resolved in this order:

1. **`<base>/latest.json`, read at runtime.** The extension publisher rewrites
   this object on every release (`{featureId, version, displayName,
   bundleSha256, publishedAt}`), so a new extension version reaches customers
   **without re-releasing the accelerator and without a stack update**. This
   applies to OSS and marketplace extensions alike.
2. **The catalog's `latestVersion`** — the fallback, used when `latest.json`
   isn't reachable.

The runtime read is designed to be invisible when it fails, because it runs on
every page load:

- **Fail soft** — unreachable object, bad JSON, blocked egress, or an
  unpublished Region all fall back to the catalog value and ultimately to no
  badge. It never surfaces an error.
- **Cached** — memoized per (bucket, key) for `LATEST_JSON_TTL_SECONDS`
  (default 300s); failures are cached for a shorter negative TTL so a missing
  object doesn't cost a round trip every time. Set `LATEST_JSON_LOOKUP=false` to
  disable the lookup and use the catalog only.
- **Anonymous first** — the GET is unsigned, since published artifacts are
  public-read. That needs no IAM grant on the host role and no bucket-policy
  grant from the publisher, so enabling it cannot regress an existing
  deployment. If public read is refused the host retries *signed*, which lets an
  OSS extension self-published to a private bucket still get badges — list that
  bucket's object ARN in `SellerBucketObjectArns` to allow it.

Comparison is proper SemVer, and a badge appears only when the published version
is strictly **newer**. A feature installed with `idp-feature-cli deploy
--from-code` can legitimately be *ahead* of what's published; treating "differs"
as "update available" would invite a downgrade.

Consequently `latestVersion` in `config_library/extensions-marketplace.yaml` is
only a seed/fallback. Keeping it roughly current is still useful — it's what a
stack sees when `latest.json` is unreachable — but shipping an extension release
no longer requires touching this repo.

The separate Build Info **"update available"** indicator for the *accelerator
itself* works differently: `idp-cli publish` writes a small pointer object,
`<prefix>/idp-main-latest.json` (`{version, templateUrl}`), to the public
artifacts bucket on every release, and the `getLatestPublishedVersion` resolver
reads that one known key with a single `GetObject` (no `ListObjectsV2`, so it
works against the public release bucket). The check is disabled when
`PUBLIC_ARTIFACTS_BUCKET` is unset.

## Architecture

```mermaid
flowchart LR
    subgraph MainStack [Main IDP Accelerator Stack]
        UI[Web UI<br/>nav + FeaturePage]
        AppSync[(AppSync API<br/>feature-platform resolvers)]
        InstalledDDB[(InstalledFeatures<br/>DDB table)]
        WebBucket[(WebUIBucket<br/>features/&lt;id&gt;/v&lt;ver&gt;/)]
        FeatureBucket[(FeatureBucket<br/>catalog artifacts)]
    end

    subgraph FeatureStack [Feature Stack<br/>e.g. 'docs-by-status']
        FCR[Custom Resource<br/>uploads UI + registers]
        FAPI[HTTP API<br/>+ Lambda]
        FData[DDB / S3]
    end

    subgraph Marketplace [AWS Marketplace<br/>or simulator (optional)]
        ENT[Entitlements]
    end

    UI -- listCatalogFeatures --> AppSync
    UI -- listInstalledFeatures --> AppSync
    UI -- checkFeatureEntitlement --> AppSync
    UI -- getFeatureLaunchUrl --> AppSync
    AppSync --> InstalledDDB
    AppSync --> FeatureBucket
    AppSync -. only when endpoint set .-> ENT
    FCR --> InstalledDDB
    FCR --> WebBucket
    UI -- dynamic UMD load --> WebBucket
    UI -- feature REST calls --> FAPI
```

### Moving pieces

| Component | Lives in | Purpose |
|-----------|----------|---------|
| `FeaturePlatformStack` | nested stack from `feature-platform/main-stack-extensions/template.yaml` | Owns the `InstalledFeatures` table, the feature-platform Lambdas, and AppSync data sources / resolvers |
| `FeatureBucket` | main `template.yaml`, condition-gated on `EnableFeaturePlatform` | Holds the catalog of published features (CFN template + UI bundle + `feature.yaml` manifest per feature). Auto-created and pre-populated with the bundled sample feature unless `FeaturePlatformFeatureBucket` is supplied. |
| Pipeline hooks | `patterns/unified/` (`PipelineHooksDispatcherFunction` + `preprocessing` / `postprocessing` / `postHook` config) | Lets features inject Lambdas at the `preprocessing` and `postprocessing` points (which bracket the pipeline) plus five post-step extension points in between. Inert when no hooks are registered. |
| Feature stack | standalone CFN template published by the author via `idp-feature-cli publish` | Creates the feature's own resources + registers into the main stack |

### GraphQL surface

| Operation | Auth | Purpose |
|-----------|------|---------|
| `listCatalogFeatures: [CatalogFeature]` | Cognito user | Features published to the feature bucket (includes not-yet-installed) |
| `listInstalledFeatures: [InstalledFeature]` | Cognito user | Features whose stack has been launched & registered |
| `checkFeatureEntitlement(featureId): FeatureEntitlement` | Cognito user | `NONE` / `ACTIVE` / `EXPIRED`, with `expiresAt` + `source`. Returns `ACTIVE`/`auto` in auto-subscribe mode. |
| `getFeatureLaunchUrl(featureId): FeatureLaunchUrl` | Cognito user (Admin for launching) | Pre-signed CFN quick-create URL |
| `subscribeFeature(featureId): FeatureEntitlement` | Admin group | Calls the marketplace/simulator admin API (errors in auto-subscribe mode) |
| `unsubscribeFeature(featureId): FeatureEntitlement` | Admin group | Calls the marketplace/simulator admin API |
| `registerFeature(input): InstalledFeature` | IAM (feature stack CR) | Feature stack registers itself on create |
| `registerFeatureHooks(input): FeatureHooksRegistration` | IAM (feature stack CR) | Feature stack registers pipeline hooks |

Each GraphQL operation is backed by a Lambda under
`feature-platform/main-stack-extensions/lambdas/`.

## Pipeline hooks

Features can inject custom Lambdas at extension points in the unified
processing workflow. There are two kinds:

- **Two standalone single-hook points** that bracket the pipeline:
  - **`preprocessing`** — runs **FIRST**, before the BDA/pipeline routing
    decision, so it fires in both processing modes and even when OCR is
    disabled. It operates on the *source document* (before any OCR output
    exists) and can **halt** the execution by returning `halt: true` — used by
    the [PII Anonymization extension](extensions/pii-anonymizer.md) to
    short-circuit an original whose only purpose was to spawn a redacted copy.
    While it runs, the document's status shows **`PREPROCESSING`**.
  - **`postprocessing`** — runs **LAST**, after evaluation and before the
    workflow's terminal state, also on the shared tail so it fires in both
    processing modes. It operates on the *finished document*, and a mutation
    there is the last word: it reaches the persisted tracking row, the
    reporting/Athena rows, and the UI.
- **Five post-step points** — `postOcr`, `postClassification`,
  `postExtraction`, `postRuleValidation`, `postSummarization` — invoked after
  the corresponding step. (`postAssessment` was removed in v0.6 when
  assessment folded into extraction.)

At each point the Step Functions workflow invokes
`PipelineHooksDispatcherFunction`, which runs any hook Lambdas registered for
that point.

**Inert by default** — hooks are stored inline in the active configuration
version. With none registered the dispatcher returns after a single DynamoDB
read and the pipeline is unchanged.

**`preprocessing` / `postprocessing` shape** — each is a standalone top-level
config section holding ONE flat hook (no list; the hook's own settings travel in
generic `args` key/value pairs, keeping the platform hook-agnostic). Both are
editable in the View/Edit Configuration UI — Preprocessing appears just above
OCR, Postprocessing just below Evaluation:

```yaml
preprocessing:
  enabled: true               # default false
  featureId: pii-anonymizer   # owner label (for traceability)
  arn: <hook-lambda-arn>      # Lambda to invoke
  onError: fail               # continue | fail — use fail when the hook MUST
                              # gate processing (a failure ends the execution
                              # via a terminal Fail state; it never falls
                              # through to processing the unprocessed original)
  args:                       # hook-specific settings, opaque to the platform
    - { key: mode, value: redactcopy_and_stop }
  allowDocumentUpdate: true   # default true — see "Modifying the document"

postprocessing:
  enabled: true               # default false
  featureId: my-delivery      # owner label (for traceability)
  arn: <hook-lambda-arn>      # Lambda to invoke
  onError: continue           # continue (recommended) | fail — `fail` marks an
                              # otherwise-successful document FAILED, so use it
                              # only when the hook genuinely gates delivery
  args:
    - { key: endpoint, value: "https://erp.example/ingest" }
  allowDocumentUpdate: true   # default true
```

Because each is a *single* hook, only one feature can own it — the host refuses
a registration that would overwrite another feature's hook, rather than silently
disabling it.

**`postHook` entry shape** (per step, in the active config version):

```yaml
extraction:
  postHook:
    - featureId: my-feature     # owner label (for traceability)
      arn: <hook-lambda-arn>    # Lambda to invoke
      order: 100                # lower runs first within a point (default 100)
      onError: continue         # continue | skip-remaining | fail (default continue)
      enabled: true             # default true
      allowDocumentUpdate: true # default true — may this hook return an
                                # `updatedDocument`? Set false to pin it to
                                # observe-only (see "Modifying the document")
```

**Hook Lambda contract** — invoked synchronously (`RequestResponse`) with:

```json
{ "hookPoint": "postExtraction", "featureId": "my-feature",
  "document": { ... }, "section": { ... }, "executionArn": "arn:aws:states:...",
  "args": [ { "key": "...", "value": "..." } ], "argsMap": { "...": "..." } }
```

It returns any JSON result (surfaced under `$.HookResults`). A `preprocessing`
hook may include `"halt": true` in its result to end the execution (the
document is marked according to the hook's semantics — e.g.
`REDACTED_SUPERSEDED` for PII redaction). `halt` is **only** actionable at
`preprocessing`; at any later point — `postprocessing` especially — there is
nothing left to skip, so the dispatcher logs it and reports `haltIgnored: true`
rather than appearing to act on it. `onError` controls failure handling:
`continue` (log and proceed), `skip-remaining` (stop later hooks at that
point), or `fail` (fail the workflow — for `preprocessing` this stops the
execution in a terminal `PreprocessingHookFailed` state rather than continuing
to normal processing).

**At `postprocessing`, prefer `onError: continue`** (the default). By the time it
runs, every expensive step has succeeded and the output objects are written, so
`fail` turns a delivery-integration error into a FAILED document. It also runs
*inside* the execution: a slow hook adds directly to per-document latency and
holds its concurrency slot until it returns. For fire-and-forget downstream
delivery that must not do either, use the
[EventBridge post-processing hook](post-processing-lambda-hook.md) instead —
the two mechanisms coexist and are compared there.

**HITL and `postprocessing`.** The workflow does *not* skip the hook while a
human review is pending: `MarkHITLPending` lets the execution complete, and the
document is re-queued after review (so the hook fires again). The hook decides
what to do from the document's own HITL fields — `hitl_status`
(`PendingReview` | `InProgress` | `Completed` | `Skipped`), `hitl_triggered`,
`hitl_sections_pending`, `hitl_sections_completed`, and `hitl_metadata`. Note
these are **omitted when falsy**, so treat an absent field as "no HITL": gate
delivery on `hitl_status in (None, "Completed", "Skipped")` rather than
inspecting `hitl_status == "PendingReview"` alone, and keep the hook idempotent
because it will be invoked once per pass.

**Modifying the document (optional)** — a hook is not limited to observing. To
change what the *next* workflow step consumes, return the modified document
under `updatedDocument`:

```json
{ "updatedDocument": { ...document... }, "myOwnField": "whatever" }
```

This is how a hook injects business logic into the pipeline itself —
relabelling a section's classification, adding or dropping sections, correcting
extracted attributes, adjusting confidence alerts, or appending metering — as
opposed to only rewriting the S3 objects the document points at.

The document may be returned either **inline** (the dispatcher spills it to the
working bucket for you) or as a **compressed reference** the hook wrote itself
(`{compressed: true, s3_uri, document_id, sections, num_pages, config_version}`),
which has no size ceiling. Use
[`idp_common.hooks`](feature-platform-developer-guide.md#writing-a-mutating-hook)
to get the round-trip right in two calls.

Omit `updatedDocument` and nothing changes — the document passes through
byte-identical, which is why every hook written before this capability existed
keeps working unmodified.

Guardrails the dispatcher enforces (a violation is **refused**, leaving the
document at its pre-hook value and recording the reason in
`$.HookResults.<point>.Payload.results[].documentUpdateRejected` — it never
fails the workflow):

| Rule | Why |
|---|---|
| `id` / `input_key` / `input_bucket` / `output_bucket` are immutable | The tracking-table row and output S3 prefixes are keyed off them. A hook that needs a *different* document should spawn one and `halt`. |
| `sections` in a compressed reference must be a list of section-id strings | The workflow's `ProcessSections` Map iterates it directly, so a malformed value would fail the whole execution. |
| `config_version` is preserved | It resolves hooks for the rest of the pipeline; a changed value is restored (the content change is still honored). |
| A compressed reference's `s3_uri` must be under `compressed_documents/` in the stack's working bucket | Downstream, `Document.decompress()` parses the URI but *discards its bucket*, reading the key against the consumer's own working bucket — so an unconstrained URI is a key-injection vector, not just a cross-bucket read. |
| Inline documents are capped at 5 MB | Bounded by Lambda's own 6 MB synchronous response limit. Return a compressed reference instead. |

Some fields the state machine reads by JSONPath are **not** `Document` model
fields, so a hook's load → mutate → return round-trip drops them — and an
absent one fails the execution outright rather than degrading. The dispatcher
back-fills these from the inbound document, and a hook that sets one explicitly
keeps its own value:

- `use_bda`, `bda_project_arn` — the BDA/pipeline routing Choice and the BDA
  invoke parameters.
- `num_pages`, `status`, `sections` — the compressed wrapper's own metadata,
  read by `BDA_CheckExistingData` and by `ProcessSections`' `ItemsPath`. The
  `idp_common.hooks` helper and the dispatcher's inline path always emit these;
  the back-fill covers a hand-rolled compressed reference that omits them.

**Write idempotent mutations.** The workflow retries a hook dispatch on
transient Lambda faults, which re-invokes the hook — so a mutation that
*appends* (`classification += "-SUFFIX"`) can apply twice, while one that *sets*
(`classification = "Invoice"`) is safe. Guard append-style logic against
re-application.

Chained hooks at the same point **compose**: hook #2 receives hook #1's
document, in `order`. Set `allowDocumentUpdate: false` on a hook entry to pin it
to observe-only.

**Where a mutation reaches** — the hook point determines scope:

| Point | Document scope | Propagates to |
|---|---|---|
| `preprocessing` | Whole document, pre-OCR | Everything downstream |
| `postOcr` | Whole document + page results | Classification onward |
| `postClassification` | Whole document | The `ProcessSections` Map fan-out (section adds/removes/relabels) and everything after |
| `postExtraction` | **A single section** (runs inside the Map) | Assessment, then `sections[0]` + `metering` are merged into the final document — top-level and page-level changes made here are discarded |
| `postRuleValidation` | Whole document | Summarization, evaluation, final output |
| `postSummarization` | Whole document | Evaluation and the final workflow output |
| `postprocessing` | Whole document, fully processed | Nothing downstream in the workflow — but it *is* the workflow output, so it reaches the persisted tracking row, the reporting/Athena rows, and the UI |

One thing a `postprocessing` mutation **cannot** do is set a terminal status: the
workflow tracker forces `COMPLETED` on a successful execution (only
`REDACTED_SUPERSEDED` is honored, which the preprocessing halt path sets). Record
delivery outcomes in your own store or in the document's `metadata`, not in
`status`.

**Security** — the dispatcher's `lambda:InvokeFunction` is scoped so a hook
Lambda must either carry the `idp:feature-id` resource tag (ABAC, used by
installed features) or follow the `GENAIIDP-*` naming convention; anything else
fails closed with `AccessDenied`.

Features register hooks at install time via the `registerFeatureHooks`
mutation (declared in the manifest's `pipelineHooks` field); admins can also
edit a config version's `postHook` lists directly. See the
[Developer Guide → Pipeline hooks](feature-platform-developer-guide.md#optional-pipeline-hooks).

## Config presets

A feature can bundle an accelerator configuration (custom classes, prompts,
rule-validation policy classes, …) and apply it at install via the manifest's
`configPreset` field. The feature stack calls the host's
`applyFeatureConfigPreset` operation, which writes the preset as a **non-active
[Configuration Profile](configuration-profiles.md) named after the feature** —
`<featureId>`, no version suffix. Each release of the feature is recorded as a
**revision** of that one profile, so an upgrade produces a diff an admin can read
instead of a second profile. (An upgrade that does not change the preset records
nothing at all.) Installation never changes the active configuration — an admin
reviews and activates the preset from the **Configuration** page.

Uninstall calls `removeFeatureConfigPreset`, which deletes `<featureId>` **and**
any legacy `<featureId>-v*` profiles from before this changed, along with their
revision history. If the feature's profile is the active one, `default` is
activated first: a feature's configuration carries that feature's pipeline hooks
inline, so leaving it active after uninstall would point every subsequent document
at deleted hook Lambdas.

This is how a vertical feature ships "the configuration it needs" alongside its
UI and hooks. See the
[Developer Guide → ship a configuration preset](feature-platform-developer-guide.md#optional-ship-a-configuration-preset).

> **Extensions that write the ConfigurationTable directly** (rather than calling
> `applyFeatureConfigPreset`) did not get this fix, and a Delete handler that
> removes only the version being uninstalled leaves every earlier release's profile
> orphaned — undeletable from the Web UI if it was written `Managed`. Such an
> extension should write `Config#<featureId>` (no version suffix) and, on Delete,
> remove that profile **and** any legacy `<featureId>-v*` rows — preferably by
> invoking the host's `removeFeatureConfigPreset`, which also drops each profile's
> revision history. To clear profiles already orphaned, use
> `idp-cli config-delete` (the Web UI refuses stack-managed profiles; the CLI warns
> and proceeds).

## Two reference samples

Both bundled extensions are **samples** — reference implementations for feature
authors, not production products. They're labelled accordingly (each display
name starts with `Sample:`) and set `showInNav: false`, so they appear on the
**Browse catalog** page rather than as nav entries until installed. In
particular, *Sample: Health Insurance Review* is a minimal demo of a use-case
extension; it is **not** the planned Claims Processing marketplace product.

| Sample (nav label) | featureId | Kind | Demonstrates |
| ------------------ | --------- | ---- | ------------ |
| [Sample: Document Status (feature add-on)](extensions/sample-document-status.md) | `docs-by-status` | feature add-on | The minimal contract: UI bundle, Cognito-auth HTTP API over the tracking table, registration. |
| [Sample: Health Insurance Review](extensions/sample-health-insurance-review.md) | `sample-health-insurance-review` | use-case add-on | An advanced vertical: a bundled config preset, a `postRuleValidation` pipeline hook computing claim status, host-GraphQL Rules Discovery, and a multi-route feature API — built on [rule validation](rule-validation.md). |

## Deployment

```bash
# Default — feature platform on, auto-subscribe mode (no entitlement endpoint)
idp-cli deploy
```

```bash
# Bolt entitlement checks onto a running stack (no rebuild) — deploy the
# standalone marketplace-simulator separately, then point the stack at it:
idp-cli deploy --params FeaturePlatformSimulatorEndpoint=https://simulator.example.com
```

The default brings up:

- the main IDP stack,
- the `InstalledFeatures` DDB table + feature-platform Lambdas,
- the `FeatureBucket` pre-loaded with the bundled sample feature.

To turn the feature platform off entirely, set `EnableFeaturePlatform=false` —
no platform resources are created, and the Extensions nav section is empty
(apart from the Browse catalog link, whose page reports no extensions).

### Tear-down

```bash
idp-cli delete
```

All feature-platform resources carry `DeletionPolicy: Delete`, including the
DDB table and the auto-created feature bucket, so the stack tears down
cleanly. Any feature stacks the admin launched separately must be deleted by
the admin (they live in the same account but are outside the main stack's
dependency graph).

## Authoring a feature

> **Full walkthrough:** the [Feature Platform Developer Guide](feature-platform-developer-guide.md)
> covers the whole lifecycle for both OSS and Marketplace extensions — scaffold,
> host contract, adding to the catalog, publishing, and local testing. The
> summary below is the quick version.

Scaffold a new feature project from the bundled template with one CLI command:

```bash
pip install -e lib/idp_feature_sdk
idp-feature-cli init ./my-feature \
    --feature-id my-feature \
    --display-name "My Feature"
```

This copies `feature-platform/feature-template/` into `./my-feature` and
substitutes the placeholder featureId / displayName / version literals
throughout (`feature.yaml`, `template.yaml`, `entry.tsx`, `App.tsx`,
`package.json`, `handler.py`, `README.md`), giving you a working feature you can
iterate on. Then:

```bash
idp-feature-cli validate ./my-feature       # validate against the manifest schema
idp-feature-cli build ./my-feature           # build CFN + Lambda + UMD UI bundle
idp-feature-cli publish ./my-feature \        # upload artifacts + print Launch Stack URL
    --bucket-basename <your-feature-bucket> \   # region appended automatically (matches `idp-cli publish`)
    --region us-east-1
```

Once published, the new feature appears in the IDP nav automatically (the UI
fetches the catalog via `listCatalogFeatures` — no main-stack rebuild needed).
Set `showInNav: false` in `feature.yaml` to keep it off the nav until
installed (discoverable via **Extensions → Browse catalog** instead), as the
bundled reference samples do.

The host contract a feature must satisfy:

- **UI bundle** — a UMD module that calls
  `window.IdpFeatures.register(featureId, { Component, version, displayName })`
  and resolves React / ReactDOM / Cloudscape / aws-amplify / react-router-dom
  from `window.*` externals (so it shares the host's React instance — see
  `src/ui/src/components/feature-page/feature-host-globals.ts`).
- **CFN template** — registers itself via the `registerFeature` mutation from a
  custom resource on create, and uploads its UI bundle into
  `WebUIBucket/features/<id>/v<ver>/`.
- **`feature.yaml` manifest** — validated against
  `lib/idp_feature_sdk/idp_feature_sdk/schemas/feature-manifest.schema.json`.

## Cost

In the default (auto-subscribe) mode:

- Extra cost: pennies/month (DDB on-demand + feature-platform Lambdas at idle +
  S3 feature bucket).
- Extra resources: S3 feature bucket, DDB `InstalledFeatures` table,
  feature-platform Lambdas + log groups + IAM roles, the pipeline-hooks
  dispatcher Lambda.
- No EC2.

With `EnableFeaturePlatform=false`, none of the above are created.
