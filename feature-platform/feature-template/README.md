# Feature Template — copy this to start a new feature

This is a scaffold for building an installable IDP Accelerator subscription
feature. The recommended way to use it is via the SDK CLI:

```bash
pip install -e lib/idp_feature_sdk
idp-feature-cli init ./my-feature \
    --feature-id my-feature \
    --display-name "My Feature"
```

That copies this directory and substitutes the placeholder `featureId` /
`displayName` / `version` literals throughout. You can also `cp -r` the
whole directory and find-replace `my-feature` / `My Feature` / `0.1.0`
manually if you prefer.

## Structure

```
feature-template/
├── feature.yaml              # Manifest — edit featureId, displayName, etc.
├── template.yaml             # CloudFormation stack for the feature
├── feature-api/              # Optional backend Lambda(s) + HTTP API Gateway
│   ├── handler.py
│   └── tests/
│       └── test_handler.py   # Pytest stub — `cd feature-api && pytest`
├── feature-ui/               # React UMD bundle — rendered inside the host UI
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── src/
│   │   ├── entry.tsx         # Calls window.IdpFeatures.register(...)
│   │   └── App.tsx           # Your feature's root React component
│   └── index.html            # Needed by Vite dev server; not published
├── ui-deployer/              # Custom-resource Lambda that copies the UMD
│                             # bundle from the feature bucket into the main
│                             # stack's WebUIBucket. Registers the feature by
│                             # invoking the host's registerFeature resolver
│                             # Lambda directly on stack Create/Delete.
│   └── handler.py
└── publish.py                # Thin wrapper that calls idp-feature-cli publish
```

## Workflow

```mermaid
sequenceDiagram
    participant Dev as Developer
    participant CLI as idp-feature-cli
    participant S3 as Feature Bucket
    participant Admin as IDP Admin
    participant Main as Main Stack

    Dev->>CLI: idp-feature-cli publish .
    CLI->>CLI: validate feature.yaml
    CLI->>CLI: npm run build (UMD bundle)
    CLI->>CLI: static-validate bundle
    CLI->>S3: upload template.yaml, ui-bundle.js, manifest.json
    CLI->>S3: update latest.json
    CLI-->>Dev: print Launch Stack URL

    Admin->>Main: opens "Extensions" → My Feature
    Main-->>Admin: shows InstallPrompt (admin + ACTIVE)
    Admin->>Main: clicks Launch Stack → opens CFN Console
    Admin->>Main: Create Stack
    Main->>Main: deploys feature stack
    Main->>Main: ui-deployer CR copies bundle to WebUIBucket
    Main->>Main: register-feature CR puts row in InstalledFeatures DDB
    Admin->>Main: reloads page — feature UI is now live
```

## Implementing the host contract

### UI bundle
Your `feature-ui/src/entry.tsx` **must** call:

```ts
window.IdpFeatures.register('my-feature', {
  Component: App,              // receives FeatureContext as props
  version: '0.1.0',            // must match feature.yaml -> version
  displayName: 'My Feature',
});
```

And your `feature-ui/vite.config.ts` **must** externalise React/ReactDOM/
Cloudscape/aws-amplify (see the provided example).

### Backend API (optional)
If your feature needs a backend, `template.yaml` should create an HTTP API
Gateway + Lambda(s) and output the endpoint. The ui-deployer CR reads that
output and writes it to `InstalledFeatures.featureApiEndpoint` so the
host passes it to your UI via `FeatureContext.featureApiEndpoint`.

Cognito JWT auth: the UI calls `context.getAuthToken()` to get a fresh
Bearer token — configure your API Gateway to verify against the main stack's
User Pool (import `<MainStackName>-UserPoolId`).

### Main-stack registration
Your `template.yaml` must include a `RegisterFeature` custom resource
(see `ui-deployer/handler.py`) that invokes the host's `registerFeature`
resolver Lambda on Create/Update and its `unregisterFeature` field on Delete.
Without this, your feature never shows up in the UI's nav.

The call is a plain `lambda:Invoke` on the function ARN the host exports as
`<MainStackName>-RegisterFeatureFunctionArn`, with the payload
`{"info": {"fieldName": "registerFeature"}, "arguments": {...}, "identity": {...}}`.
Import that ARN both as the `Resource` of a `lambda:InvokeFunction` statement on
your custom-resource role and as the handler's
`REGISTER_FEATURE_FUNCTION_ARN` environment variable, as this template's
`template.yaml` does. (This replaced a SigV4-signed AppSync mutation; AppSync has
been removed from the solution, but the resolver event shape is unchanged, which
is why the payload still looks like a GraphQL resolver event.)

## Add to the catalog & publish

Adding the feature to the catalog is what makes it discoverable in the UI:

- **OSS** — add `{ path: feature-platform/<your-feature> }` to
  `config_library/extensions-oss.yaml`, then `idp-cli publish`.
- **Marketplace** — publish to your private seller bucket and add an entry to
  `config_library/extensions-marketplace.yaml`.

In the default **auto-subscribe** mode, OSS features skip the subscribe step and
go straight to **Install**.

See the full walkthrough — host contract, catalog registration for both kinds,
and local end-to-end testing — in the
[Feature Platform Developer Guide](../../docs/feature-platform-developer-guide.md).
