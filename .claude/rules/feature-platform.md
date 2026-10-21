---
paths:
  - "feature-platform/**"
---

# Feature Platform rules

Covers the Feature Platform host (`feature-platform/main-stack-extensions/`) and
the features in the repo, each an independent stack that registers with the host.
General template rules are in [infrastructure.md](infrastructure.md).

## Must
- Ship each feature as its own CloudFormation stack, deployed next to the host
  stack.
- Use `idp-feature-cli` for the lifecycle (`init`, `validate`, `build`, `publish`,
  `deploy`). Validate `feature.yaml` against
  `lib/idp_feature_sdk/idp_feature_sdk/schemas/feature-manifest.schema.json`.
- In the UI bundle:
  - Register a UMD bundle with
    `window.IdpFeatures.register(featureId, { Component, version, displayName })`.
  - Set `version` to match `feature.yaml`.
  - Externalize React, ReactDOM, Cloudscape, aws-amplify and react-router-dom.
- Render markdown or HTML from the backend through
  `window.IdpFeatureHost.SafeMarkdown`.
- Register and unregister through the `RegisterFeature` custom resource
  (`ui-deployer/`). It calls the Lambda ARNs the host exports and uploads the UI
  bundle to the host's Web UI bucket.
- Discover host resources from `<MainStackName>-*` exports, and authorize a
  feature API against the host's Cognito User Pool.
- Tag every hook Lambda `idp:feature-id=<featureId>`. In the hook:
  - Load, mutate and return the document with `idp_common.hooks`.
  - Make mutations idempotent.
  - Make whole-document changes at `postClassification` or `postRuleValidation`.
  - Register any gating `onError: fail` hook at `preprocessing`.
- Bump the `version` in `feature.yaml` whenever a feature changes.
- For a paid extension, enforce entitlement in the extension's own runtime or on
  the seller side. Use `entitlementVerified` only to decide whether to warn.

## Must not
- Nest a feature inside the host template, or hardcode host ARNs or resource
  names.
- Register a feature anywhere except through the host's `InstalledFeatures`
  registration, or write to the host's Web UI bucket directly.
- Construct a `Document` from scratch in a hook. Doing so drops metering, errors
  and HITL state.
- Treat `uiAccessAllowed` as a licence gate.
- Bundle your own copy of React or Cloudscape.
- Publish a feature with raw `sam deploy`, `aws cloudformation deploy` or a
  custom publish wrapper.
- Hand-edit the vendored `pii-anonymizer` tree. Re-sync it through its skill.

## Verify with
- `idp-feature-cli validate ./<feature>` and `idp-feature-cli build ./<feature>`
- `cd lib/idp_feature_sdk && python -m pytest -q`
- `scripts/tests/test_lambda_log_groups.py` and `make cfn-lint`

## Sources
- [docs/feature-platform-developer-guide.md](../../docs/feature-platform-developer-guide.md):
  scaffolding, the host contract, entitlement, hooks, the catalog and publishing
- [docs/feature-platform.md](../../docs/feature-platform.md): architecture,
  deployment modes, pipeline hook semantics and config presets
- [docs/architecture.md](../../docs/architecture.md): how hooks, features and
  `idp_common` extras divide up the extension work
- [.claude/skills/sync-pii-anonymizer.md](../skills/sync-pii-anonymizer.md):
  re-syncing the vendored copy
