# Phase A — Main-stack extensions

Additive pieces that the main IDP stack gains when the feature platform is turned on.
Nothing here modifies the existing main stack; this directory is self-contained and can be reviewed in isolation.

## What this adds to the main stack

| Piece | Purpose |
|-------|---------|
| `InstalledFeatures` DDB table | One row per installed feature stack. pk = `featureId`. Holds stackName, version, uiBundlePath, featureApiEndpoint, installedBy, installedAt. |
| 6 UI-facing resolver Lambdas | `listCatalogFeatures`, `listInstalledFeatures`, `checkFeatureEntitlement`, `getFeatureLaunchUrl`, `subscribeFeature`, `unsubscribeFeature`. Their ARNs are plain (un-exported) stack Outputs that the main `template.yaml` forwards into the host REST dispatcher's field→function map, so the UI reaches them at `POST /op/<field>` on the host's API Gateway REST API. |
| 3 install-hook resolver Lambdas | `registerFeature`/`unregisterFeature`, `registerFeatureHooks`/`unregisterFeatureHooks`, `applyFeatureConfigPreset`/`removeFeatureConfigPreset` (one Lambda per pair). These are **not** reachable through the REST dispatcher — a feature stack's own `ui-deployer` custom resource invokes them directly with `lambda:InvokeFunction`. |
| `WebUIBucket` prefix-scoped policy | Allow same-account principals with session tag `idp:feature-id=<id>` to write under `features/<id>/*`. Used by the feature-stack's UI-deployer custom resource. |
| Extra stack Exports | The three install-hook function ARNs (`<MainStackName>-RegisterFeatureFunctionArn`, `-RegisterFeatureHooksFunctionArn`, `-ApplyFeatureConfigPresetFunctionArn`), plus re-exports of host resources feature stacks need: `-UserPoolId`, `-UserPoolClientId`, `-WebUIBucketName`/`-WebUIBucketArn`, `-InstalledFeaturesTableName`/`-InstalledFeaturesTableArn`, `-TrackingTableName`/`-TrackingTableArn`, `-ConfigurationTableName`/`-ConfigurationTableArn`, `-CustomerManagedEncryptionKeyArn`, and the Input/Output/Working/Discovery/Reporting/TestSet bucket and UsersTable names. |

## Files

```
main-stack-extensions/
├── template.yaml                    Self-contained nested stack (parameters reference existing main-stack resources)
├── appsync/                         Vestigial directory name — AppSync itself was removed
│   └── feature-platform.graphql     Schema fragment (merged into the schema of record with marker comments)
├── lambdas/
│   ├── list_catalog_features/
│   ├── list_installed_features/
│   ├── check_feature_entitlement/
│   ├── get_feature_launch_url/
│   ├── subscribe_feature/
│   ├── unsubscribe_feature/
│   ├── register_feature/
│   ├── register_feature_hooks/
│   └── apply_feature_config_preset/
├── tests/                           Pytest unit tests (moto-based)
├── apply-to-main-stack.md           Step-by-step instructions to wire these in
└── README.md                        (this file)
```

## Architecture

```mermaid
flowchart TD
    subgraph MainUI[Main Web UI]
        FP[FeaturePage<br/>7-state renderer]
    end

    subgraph HostApi["Host API Gateway REST API<br/>POST /op/(field), Cognito authorizer"]
        DISP[HttpApiDispatcherFunction<br/>field to function map]
    end

    subgraph MainLambdas[Feature-platform resolver Lambdas]
        L1[list_installed_features]
        L2[check_feature_entitlement]
        L3[get_feature_launch_url]
        L4[register_feature]
    end

    DDB[(InstalledFeatures<br/>DynamoDB)]
    MKT[AWS Marketplace<br/>or simulator<br/>GetEntitlements]
    SDK[idp-feature-cli<br/>publishes feature<br/>to feature bucket]

    FP --> DISP
    DISP -- listInstalledFeatures --> L1 --> DDB
    DISP -- checkFeatureEntitlement --> L2 --> MKT
    DISP -- "getFeatureLaunchUrl (admin-only)" --> L3
    L3 -. reads .-> DDB
    L3 -. reads feature bucket latest.json .-> SDK
    CR[Feature-stack<br/>RegisterFeature CR] -- "direct lambda:InvokeFunction" --> L4 --> DDB
```

The UI-facing fields travel over the host's REST API: the browser `POST`s to
`/op/<field>`, the Cognito User Pools authorizer authenticates the caller, and
the dispatcher Lambda looks the field up in its field→function map and invokes
the resolver Lambda with the resolver event shape
`{info:{fieldName}, arguments, identity}`. Install-hook fields skip the API
entirely — a feature stack's `ui-deployer` custom resource calls
`lambda:InvokeFunction` on the exported resolver ARN with that same event shape.
Each resolver enforces its own Cognito-group check, because the authorizer only
authenticates. See [`docs/migration-appsync-to-rest.md`](../../docs/migration-appsync-to-rest.md)
§5 for the full transport description.

## Hook registration is checked against the processing mode

`registerFeatureHooks` writes a feature's hooks inline into the **active** config
version, and three of the seven hook points — `postOcr`, `postClassification`,
`postExtraction` — exist only on the Pipeline branch of the unified state machine.
So a registration at one of them while the active configuration sets
`use_bda: true` produces a hook that is never invoked.

With `onError: fail` that registration is **refused** (a `ValueError`, which fails
the feature stack's install): the policy declares a gate, and a hook that cannot
run cannot gate. Any other policy is advisory, so it registers and the response
carries a `warnings` entry, which the calling custom resource logs. The point-mode
table comes from `lambdas/register_feature_hooks/hook_point_reachability.py`,
generated from the state machine definition by
`scripts/generate_hook_point_reachability.py`.

Registration time cannot be the whole check, because `use_bda` can change after a
hook is registered — the dispatcher repeats the audit on every document and
records it at `$.HookResults.preprocessing.Payload.unreachableHooks`. Both halves
are described in
[`docs/feature-platform.md`](../../docs/feature-platform.md#not-every-hook-point-exists-in-every-processing-mode).

## Why additive + flag-gated?

The main `template.yaml` declares an `EnableFeaturePlatform` parameter (default `'true'`), and everything in this directory is deployed (or not) by a single nested-stack `AWS::CloudFormation::Stack` resource — `FeaturePlatformStack` — guarded by the `IsFeaturePlatformEnabled` condition. The GraphQL schema fragment in `appsync/feature-platform.graphql` — a vestigial directory name, kept only so existing references still resolve — is merged into `nested/api-resolvers/src/api/schema.graphql`, wrapped in clearly-marked `# === Feature Platform (optional) ===` block comments so it can be lifted back out if needed. That schema file is no longer served by AppSync — AppSync was removed — but it remains the authoritative baseline for field-level RBAC (`scripts/sdlc/scan_api_rbac.py`) and for the dispatcher's argument validation spec (`scripts/sdlc/generate_api_validation_spec.py`), so a new feature field still has to be declared there.

See [`apply-to-main-stack.md`](apply-to-main-stack.md) for exact integration steps.
