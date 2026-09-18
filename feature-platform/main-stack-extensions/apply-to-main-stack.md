# Applying the Feature Platform to the main IDP stack

This document describes the exact, minimal changes needed to wire the
`feature-platform/main-stack-extensions/` pieces into the real `template.yaml`
and `nested/api-resolvers/`.

> **Status:** this integration has since been applied — `template.yaml` declares
> `EnableFeaturePlatform`, the `IsFeaturePlatformEnabled` condition and the
> `FeaturePlatformStack` nested stack, and the schema fragment is merged into
> `nested/api-resolvers/src/api/schema.graphql`. The steps below are kept as the
> reference for what the wiring consists of. They have also been updated because
> AWS AppSync was removed: the UI ⇄ backend transport is now an API Gateway REST
> API with a dispatcher Lambda (see
> [`docs/migration-appsync-to-rest.md`](../../docs/migration-appsync-to-rest.md)),
> and no AppSync resources exist in any template.

All changes are gated by the **`EnableFeaturePlatform`** parameter so the
main stack behaves identically for existing deployments that don't opt in.

---

## 1. New parameter + condition in `template.yaml`

```yaml
Parameters:
  # ... existing parameters ...

  EnableFeaturePlatform:
    Type: String
    Default: 'false'
    AllowedValues: ['true', 'false']
    Description: When 'true', the main stack deploys the Feature Platform
      extensions — InstalledFeatures table, the feature resolver Lambdas, and a
      prefix-scoped WebUIBucket policy allowing feature stacks to publish
      their UI bundles. Off by default.

  FeaturePlatformFeatureBucket:
    Type: String
    Default: ''
    Description: (Optional) S3 bucket that publishers push feature bundles to.
      Same bucket the marketplace-simulator uses in dev.

  FeaturePlatformFeatureBucketRegion:
    Type: String
    Default: 'us-east-1'

  FeaturePlatformSimulatorEndpoint:
    Type: String
    Default: ''
    Description: (Optional) Override for marketplace-entitlement. Leave blank
      to use the real AWS Marketplace endpoint.

  FeaturePlatformDefaultCustomerIdentifier:
    Type: String
    Default: ''

Conditions:
  # ... existing conditions ...
  IsFeaturePlatformEnabled: !Equals [!Ref EnableFeaturePlatform, 'true']
```

## 2. Nested-stack invocation

Add near the existing `APIRESOLVERSTACK` nested-stack resource:

```yaml
Resources:
  FeaturePlatformStack:
    Type: AWS::CloudFormation::Stack
    Condition: IsFeaturePlatformEnabled
    Properties:
      TemplateURL: ./feature-platform/main-stack-extensions/.aws-sam/packaged.yaml
      Parameters:
        MainStackName: !Ref AWS::StackName
        # No API id/ARN/URL is passed: the six UI-facing fields are wired the
        # other way round. The nested stack returns its resolver function ARNs
        # as plain Outputs, and the main template forwards them into
        # APIRESOLVERSTACK's dispatcher field→function map parameters.
        UserPoolId: !Ref UserPool
        WebUIBucketName: !Ref WebUIBucket
        WebUIBucketArn: !GetAtt WebUIBucket.Arn
        FeatureBucketName: !Ref FeaturePlatformFeatureBucket
        FeatureBucketRegion: !Ref FeaturePlatformFeatureBucketRegion
        SimulatorEntitlementEndpoint: !Ref FeaturePlatformSimulatorEndpoint
        DefaultCustomerIdentifier: !Ref FeaturePlatformDefaultCustomerIdentifier
        AdminGroupName: 'Admin'
        LogLevel: !Ref LogLevel
```

The template file must be uploaded alongside the Lambda bundles to the same
artifact bucket as the rest of the main-stack assets (same mechanism that
already publishes `nested/api-resolvers/template.yaml` et al.).

## 3. Schema merge

The schema of record, `nested/api-resolvers/src/api/schema.graphql`, gains the
feature platform types. Copy the contents of
[`appsync/feature-platform.graphql`](appsync/feature-platform.graphql) — the
`appsync/` directory name is vestigial, kept only to avoid churning the path —
into that file, keeping the BEGIN/END marker comments intact so the block can be
lifted back out. The fragment uses `extend type Query` and `extend type
Mutation`, so it can be appended at the bottom without conflicting with the
existing `Query` and `Mutation` definitions.

Nothing serves this schema at runtime any more; it is the source of truth for
two build-time gates. `scripts/sdlc/scan_api_rbac.py` derives each field's
required Cognito groups from its `@aws_cognito_user_pools(cognito_groups: [...])`
directive and checks the resolvers enforce them, and
`scripts/sdlc/generate_api_validation_spec.py` compiles the argument signatures
into the dispatcher's `api_validation_spec.json`. A feature field that is not
declared here therefore has no RBAC expectation and no argument validation.

> **Gotcha**: if `EnableFeaturePlatform=false` at deploy time, the extra type
> definitions are harmless — nothing parses the schema at runtime, and the two
> gates above read the source tree rather than a deployed stack, so they are
> unaffected by the toggle. The feature resolvers in the `FeaturePlatformStack` are the only things that break
> without the nested stack, and those are condition-guarded.

## 4. WebUIBucket policy merge

The existing `WebUIBucketPolicy` statement list (in `template.yaml`, near
line 3907) must have one additional statement inserted when
`IsFeaturePlatformEnabled`:

```yaml
WebUIBucketPolicy:
  Type: AWS::S3::BucketPolicy
  Properties:
    Bucket: !Ref WebUIBucket
    PolicyDocument:
      Version: '2012-10-17'
      Statement:
        # --- existing statements: CloudFront OAI read, etc. ---

        # --- BEGIN feature-platform addition (drop with IsFeaturePlatformEnabled=false) ---
        - !If
          - IsFeaturePlatformEnabled
          - Sid: AllowFeatureStackUiBundleWrites
            Effect: Allow
            Principal:
              AWS: !Sub 'arn:${AWS::Partition}:iam::${AWS::AccountId}:root'
            Action:
              - s3:PutObject
              - s3:PutObjectAcl
              - s3:DeleteObject
            Resource: !Sub '${WebUIBucket.Arn}/features/*'
            Condition:
              StringLike:
                aws:PrincipalTag/idp:feature-id: '*'
          - !Ref AWS::NoValue
        # --- END feature-platform addition ---
```

> **Note**: the `Principal: arn:...:root` combined with the
> `aws:PrincipalTag/idp:feature-id=*` condition restricts writes to roles
> *in this same account* that carry a session tag. Each feature stack's
> UI-deployer Lambda assumes a role that sets
> `idp:feature-id=<theirFeatureId>` — the tag matches for their prefix only
> because the UI-deployer code passes a path prefix that matches, and the
> condition uses `StringLike`. (An even tighter `s3:prefix` condition is
> possible but requires more changes in the feature stack; this policy is
> already sufficient for a trusted-admin-installs-feature scenario.)

## 5. IAM: let feature stacks invoke the install-hook resolvers

Feature stacks call the install-hook resolvers **directly**: the feature
stack's `ui-deployer` custom-resource Lambda issues `lambda:Invoke` on the
resolver function ARN. (This replaced the older indirection, where the feature
stack signed a SigV4 GraphQL mutation against the main stack's AppSync API.)

The feature-stack author grants their own custom-resource role
`lambda:InvokeFunction` on the ARN imported from the host — see
`feature-platform/feature-template/template.yaml`, which imports
`<MainStackName>-RegisterFeatureFunctionArn` both as the policy `Resource` and
as the `REGISTER_FEATURE_FUNCTION_ARN` environment variable. The same pattern
applies to `-RegisterFeatureHooksFunctionArn` and
`-ApplyFeatureConfigPresetFunctionArn`. No resource-based policy is needed on
the host side, because the caller is in the same account.

The invocation payload is unchanged from the AppSync era — the resolver event
shape `{info: {fieldName}, arguments, identity}` — so the resolver Lambdas did
not have to be rewritten.

## 6. (Optional) Main-stack outputs used by feature stacks

Already covered: the nested `FeaturePlatformStack` publishes these Exports:

| Export                                 | Consumed by           |
|----------------------------------------|-----------------------|
| `<MainStackName>-WebUIBucketName`      | Feature ui-deployer   |
| `<MainStackName>-WebUIBucketArn`       | Feature ui-deployer   |
| `<MainStackName>-RegisterFeatureFunctionArn` | Feature ui-deployer CR (direct invoke) + its role |
| `<MainStackName>-RegisterFeatureHooksFunctionArn` | Feature CR that registers pipeline hooks |
| `<MainStackName>-ApplyFeatureConfigPresetFunctionArn` | Feature CR that bundles a config preset |
| `<MainStackName>-UserPoolId`           | Feature API authorizer|
| `<MainStackName>-UserPoolClientId`     | Feature API authorizer (JWT audience) |
| `<MainStackName>-InstalledFeaturesTableName` | Feature/host reads of the install registry |
| `<MainStackName>-InstalledFeaturesTableArn`  | Scoped DynamoDB grants on that table |

The nested stack also re-exports the host's Tracking/Configuration/Users tables
and the Input/Output/Working/Discovery/Reporting/TestSet bucket names for
features that need scoped access to them; see the `Outputs` section of
`template.yaml` for the authoritative list. Note that the six **UI-facing**
resolver ARNs are deliberately *not* exported — they are plain Outputs the main
template reads with `!GetAtt FeaturePlatformStack.Outputs.<X>` and forwards into
the dispatcher's field→function map.

Feature stacks declare parameters for `MainStackName` and use
`Fn::ImportValue` to pull the rest.

---

## Checklist to apply (once reviewed)

- [ ] Add `EnableFeaturePlatform` + 5 related parameters to `template.yaml`
- [ ] Add `IsFeaturePlatformEnabled` condition
- [ ] Add `FeaturePlatformStack` nested-stack resource
- [ ] Append BEGIN/END-bracketed schema fragment into `nested/api-resolvers/src/api/schema.graphql`
- [ ] Forward the six UI-facing resolver ARNs from `FeaturePlatformStack.Outputs` into `APIRESOLVERSTACK`'s dispatcher field→function map parameters
- [ ] Insert feature-platform statement into `WebUIBucketPolicy`
- [ ] Publish `feature-platform/main-stack-extensions/template.yaml` + the lambda directories to the artifact bucket during `publish.py`
- [ ] Deploy stack with `EnableFeaturePlatform=true` in a dev account and run Phase E e2e tests

## Rollback

If the feature platform needs to be removed:

1. Redeploy with `EnableFeaturePlatform=false` — the nested stack is deleted,
   the DDB table is **retained** (`DeletionPolicy: Retain`) so installed
   features are preserved for future re-enable.
2. Remove the `StringLike` statement from `WebUIBucketPolicy`.
3. (Optional) Remove the BEGIN/END block from `schema.graphql`.
