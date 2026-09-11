---
title: "Cross-Account Bedrock (Hub Account)"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Cross-Account Bedrock (Hub Account)

Some enterprises require all Amazon Bedrock invocations to be routed through a single, centralized "hub" AWS account so they can centralize budget, audit, and policy controls (model allow-listing, Guardrails, application inference profiles). This page explains how to deploy the GenAI IDP Accelerator so that the workload account assumes a role in a hub account before calling Bedrock.

> **TL;DR**: Set the `BedrockHubRoleArn` parameter at deploy time. All Bedrock data-plane and control-plane traffic from IDP processing Lambdas will then route through `sts:AssumeRole` into that role. When the parameter is left empty, behavior is unchanged.

## When to use this

Enable the hub-role mode when **any** of the following apply:

- Your organization mandates that all Bedrock invocations originate from a designated AI/ML account.
- You want to centralize Bedrock model access governance, application inference profiles, or Guardrails in one account.
- You need a single CloudTrail in the hub account that records every IDP Bedrock invocation for audit.

If your IDP stack and Bedrock invocations live in the same account, leave `BedrockHubRoleArn` empty and ignore this page.

## How it works

When `BedrockHubRoleArn` is set:

1. Each processing Lambda's execution role is granted `sts:AssumeRole` permission on the configured ARN.
2. The Lambda receives `BEDROCK_ASSUME_ROLE_ARN`, `BEDROCK_ASSUME_ROLE_EXTERNAL_ID`, and `BEDROCK_ASSUME_ROLE_SESSION_NAME` environment variables.
3. The `idp_common.bedrock.session.get_bedrock_session()` factory builds a single boto3 session with `botocore.credentials.DeferredRefreshableCredentials`. This means STS credentials are **automatically refreshed** before they expire — warm Lambda containers don't break after the default 1-hour STS session.
4. Every Bedrock client (`bedrock-runtime`, `bedrock` control-plane, embedding model invocations, Strands `BedrockModel` for agentic extraction) is created from this shared session.

When `BedrockHubRoleArn` is empty:

- No new IAM permissions are attached.
- No new env vars are set on Lambdas.
- `get_bedrock_session()` returns a vanilla `boto3.Session` — identical to legacy behavior.

## Coverage

The following Bedrock call paths route through the hub role when configured:

- ✅ **Pipeline mode (Pattern 2)**: classification, extraction (traditional + agentic), assessment, summarization, evaluation, OCR (Bedrock OCR backend), discovery (classes, rules, multi-doc), embeddings, CachePoint inference-profile resolution.
- ✅ **Strands agentic extraction** in `idp_common/extraction/agentic_idp.py` (uses the same factory via `BedrockModel(boto_session=...)`).
- ✅ **Multi-doc discovery** nested stack (Embed, Cluster, Analyze, Save Lambdas).
- ✅ **Chat with Document** — the streaming chat-with-document processor (`src/lambda/chat_with_document_processor`).
- ✅ **Agent Companion** — the agent chat processor (`src/lambda/agent_chat_processor`) and agent processor (`src/lambda/agent_processor`). These build sub-agent boto3 sessions for connection isolation; the shared `create_strands_bedrock_model()` helper detects `BEDROCK_ASSUME_ROLE_ARN` and overrides those sessions for Bedrock calls only — workload-account AWS calls (DynamoDB, S3, Athena, Glue, AppSync) continue to use local credentials.
- ⏸️ **Bedrock Knowledge Bases** (`query_knowledgebase_resolver`): not yet covered. The KB resolver uses the `bedrock-agent-runtime` service which has a different identity surface; cross-account KB warrants a separate design.
- ⏸️ **BDA (formerly Pattern 1)**: out of scope for v1. Cross-account BDA has its own model (project ARNs, BDA service roles) and warrants a separate design. If your deployment uses BDA mode (`use_bda: true`), the BDA runtime calls remain in the calling account.
- ⏸️ **Model fine-tuning utilities** (`idp_common/model_finetuning/`): out of scope for v1.

## Required deployment parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `BedrockHubRoleArn` | Yes | ARN of the role to assume in the hub account, e.g. `arn:aws:iam::111122223333:role/IDPBedrockHubRole`. |
| `BedrockHubRoleExternalId` | Optional | Pass through to `sts:AssumeRole` as `ExternalId`. Required when the hub-account role's trust policy enforces an `ExternalId`. Set as `NoEcho`. |
| `BedrockHubRoleSessionName` | Optional | Custom STS `RoleSessionName` (max 64 chars; allowed characters: `A-Z a-z 0-9 + = , . @ - _`). Defaults to the calling Lambda's function name for CloudTrail attribution. |

## Hub-account role: example trust policy

In the **hub account**, create a role (for example `IDPBedrockHubRole`) whose trust policy allows the workload account's IDP processing Lambda execution roles to assume it:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::WORKLOAD_ACCOUNT_ID:root"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "your-shared-external-id"
        }
      }
    }
  ]
}
```

The `:root` principal above trusts the **entire** workload account — any principal in it can assume the hub role. This is fine for an initial smoke test, but for production you should scope the trust down. Security scanners (and most org policies) will flag an unscoped account-wide trust.

### Recommended production trust policy: scope by principal

The obvious tightening is to pin `Principal` to the specific IDP Lambda execution-role ARN. In practice that rarely works for this stack: a single IDP deployment creates **many** Lambda execution roles (extraction, classification, assessment, discovery, chat, agent, …), each with a CloudFormation-generated random suffix that **changes on every deploy**, and a real environment often runs several IDP stacks at once. Hardcoding exact ARNs means editing the trust policy on every deploy.

Instead, keep the workload account as the `Principal` but add an `aws:PrincipalArn` condition that allow-lists your IDP execution-role **name patterns**. This scopes the trust to just the IDP roles, survives CloudFormation suffix churn, and requires no trust-policy changes when you redeploy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowIDPWorkloadLambdaRolesOnly",
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::WORKLOAD_ACCOUNT_ID:root"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringLike": {
          "aws:PrincipalArn": [
            "arn:aws:iam::WORKLOAD_ACCOUNT_ID:role/<your-stack-name-prefix>-*"
          ]
        }
      }
    }
  ]
}
```

Set the `aws:PrincipalArn` patterns to match the stack names you deploy (the CloudFormation stack name becomes the prefix of every Lambda execution role it creates). List one entry per prefix if you use several, e.g. `.../role/IDP-*` and `.../role/idp-*`. Note that `aws:PrincipalArn` matching is **case-sensitive**, so include each casing you actually use — this also lets you deliberately exclude unrelated roles (e.g. an admin role) that happen to share a stem.

Combine this with `sts:ExternalId` (see [`BedrockHubRoleExternalId`](#required-deployment-parameters)) for defense in depth when you need it.

> **Note on prod/non-prod separation:** some security frameworks (e.g. AWS-internal Palisade `ProdIAMRoleMustNotTrustNonProdPrincipal`) forbid a **production** hub role from trusting **any** principal in a **non-production** workload account — regardless of how tightly the trust is scoped. `aws:PrincipalArn` scoping reduces blast radius but does **not** satisfy that class of control, because the scanner evaluates the trusted account's prod/non-prod status, not the condition. If your hub account is production and your workload account is not, you will need a policy exemption, or to align the two accounts' environment classification. Keep the hub and workload accounts at the same production tier where such controls apply.

## Hub-account role: example permission policy

Attach the Bedrock permissions to the hub-account role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:GetInferenceProfile",
        "bedrock:ApplyGuardrail"
      ],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:*:HUB_ACCOUNT_ID:inference-profile/*",
        "arn:aws:bedrock:*:HUB_ACCOUNT_ID:application-inference-profile/*",
        "arn:aws:bedrock:*:HUB_ACCOUNT_ID:guardrail/*"
      ]
    },
    {
      "Comment": "Required only if using OpenAI GPT-5.x (bedrock-mantle Responses API). NOT needed for OpenAI GPT-6 Astra, which uses the bedrock-runtime Converse statement above",
      "Effect": "Allow",
      "Action": [
        "bedrock-mantle:CreateInference",
        "bedrock-mantle:GetProject",
        "bedrock-mantle:ListProjects",
        "bedrock-mantle:ListTagsForResources"
      ],
      "Resource": "*"
    }
  ]
}
```

## Resource-scoping caveats

When the hub role is enabled, *some* Bedrock resource identifiers must refer to the **hub account**, not the workload account:

| Resource type | Where it must live |
|---------------|--------------------|
| System-defined cross-region inference profiles (`us.*`, `eu.*`, `global.*`) | Either account — these resolve from any account with model access. |
| Application inference profile ARNs (configured via the IDP `model` field) | **Hub account** — application inference profiles are account-scoped. |
| Bedrock Guardrail IDs (`BedrockGuardrailId`) | **Hub account** — Guardrails are account-scoped. |
| Bedrock Knowledge Base IDs | **Hub account** if you want them invoked via the assumed role. |

If you set `BedrockGuardrailId` in the workload account but enable the hub role, the Guardrail call **will fail** because the assumed role does not have access to the workload-account Guardrail. Move the Guardrail to the hub account, or leave the hub role disabled.

## Private (VPC-secured) deployments

If you also use `UsePrivateAppSync=true` or otherwise deploy IDP into a VPC, ensure the VPC has an **STS interface VPC endpoint** in addition to the existing Bedrock and AppSync endpoints. Without it, `sts:AssumeRole` calls from the Lambda cannot reach the AWS STS regional service.

See [Deployment in a Private Network](./deployment-private-network.md) for the full list of required VPC endpoints.

## How to verify

After deploying with `BedrockHubRoleArn` set, run through the following checklist to confirm cross-account routing is in effect end-to-end.

### 1. Backwards-compatibility test (most important)

Deploy with `BedrockHubRoleArn=""` (or omit it). All existing flows should behave exactly as before. If extraction, classification, summarization, evaluation, discovery, chat-with-document, and agent companion all work, the additive change is confirmed safe.

### 2. CloudTrail in the hub account — AssumeRole calls

Each Lambda invocation should produce an `AssumeRole` event in the hub account. Note that `lookup-events` returns the top-level `Username` as `null` for cross-account `AssumeRole` events — the source account and session name live inside the embedded `CloudTrailEvent` JSON. Decode it with a small Python snippet:

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRole \
  --max-results 20 \
  --region <region-where-stack-is-deployed> \
  --output json --profile hub | python3 -c "
import json, sys
for e in json.load(sys.stdin)['Events']:
    ct = json.loads(e['CloudTrailEvent'])
    req = ct.get('requestParameters') or {}
    role_arn = req.get('roleArn', '')
    if 'IDPBedrockHubRole' not in role_arn:  # adjust to your role name
        continue
    src_acct = ct.get('userIdentity', {}).get('accountId', '?')
    print(f\"{e['EventTime']}  src={src_acct}  session={req.get('roleSessionName','?')}\")
"
```

You should see one event per Lambda invocation, with `RoleSessionName` matching the Lambda function name (e.g. `mystack-ExtractionFunction-abc123`). This proves per-Lambda CloudTrail attribution is working.

### 3. CloudTrail in the hub account — Bedrock data-plane calls

The actual Bedrock invocations should show up under the assumed role's session principal:

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=Converse \
  --max-results 20 --profile hub | \
  jq '.Events[].CloudTrailEvent | fromjson | .userIdentity.sessionContext.sessionIssuer.userName'
```

### 4. Negative test: workload account has no Bedrock permissions

Strip the local `bedrock:InvokeModel` from the workload account (or apply a permissions boundary that blocks Bedrock). With `BedrockHubRoleArn` set, processing should still succeed via the assumed role — proving the traffic actually transits the hub.

### 5. Warm-Lambda credential refresh

The implementation uses `DeferredRefreshableCredentials` so STS credentials auto-refresh before the 1-hour expiry. Two ways to verify:

- Trigger one of the processing Lambdas, wait > 60 minutes, trigger again. A successful call after the wait proves the refresher worked.
- Look at the hub-account CloudTrail for **multiple** `AssumeRole` events from a single Lambda invocation lifetime (each refresh triggers a new `AssumeRole`).

### 6. Coverage matrix — exercise each surface

| Surface | How to exercise it | Expected `RoleSessionName` (CloudTrail) |
|---------|---------------------|------------------------------------------|
| Pipeline (extraction, classification, summarization, evaluation, OCR, assessment) | Upload a sample document via the IDP UI. | `*-ClassificationFunction-*`, `*-ExtractionFunction-*`, `*-AssessmentFunction-*`, `*-SummarizationFunction-*`, etc. |
| Discovery | Run a discovery job from the UI. | `*-MultiDocDiscoveryEmbedFunction-*`, `*-AnalyzeFunction-*`, `*-SaveFunction-*` |
| Chat with Document | Open a processed document and use the "Chat with Document" feature. | `*-ChatWithDocumentProcessor-*` |
| Agent Companion / Agent Chat | Open the agent chat panel and ask an analytics question. | `*-AgentChatProcessor-*` (sub-agents reuse the same session, so they appear under the same `RoleSessionName`). |
| Agent processor | Trigger an agent invocation from the UI. | `*-AgentProcessor-*` |
| Strands agentic extraction | Set `extraction.agentic.enabled: true` in the configuration and run extraction on a document with large tables. | `*-ExtractionFunction-*` |

### 7. Failure-mode probes

Force-fail each likely misconfiguration to verify error messages are useful:

- **Bad ARN**: deploy with a non-existent role ARN — Lambdas should fail with a clear `AccessDenied` on `sts:AssumeRole`.
- **Missing ExternalId**: enable an `ExternalId` requirement in the trust policy, but unset `BedrockHubRoleExternalId` in the stack. Confirm the failure points at `ExternalId`.
- **Application inference profile in wrong account**: set the IDP `model` field to an application inference profile ARN that lives in the **workload** account while the hub role is enabled. Confirm the failure clearly indicates the resource is not accessible from the assumed role.

## Operational considerations

- **Credential expiry**: STS session credentials default to 1 hour. The session factory uses `DeferredRefreshableCredentials` so credentials are refreshed transparently before expiry. No code changes are required to your service classes.
- **Per-Lambda CloudTrail attribution**: STS `RoleSessionName` defaults to the Lambda's `AWS_LAMBDA_FUNCTION_NAME`. Searching CloudTrail in the hub account for `userIdentity.sessionContext.sessionIssuer.userName` or `userIdentity.principalId` will surface which IDP function made the call.
- **Backward compatibility**: setting `BedrockHubRoleArn=""` (the default) is fully backward-compatible. The local `bedrock:InvokeModel` IAM statements remain attached to processing Lambdas and continue to work.

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `AccessDenied` on `sts:AssumeRole` from the Lambda execution role itself (error message references `User: arn:aws:sts::<workload-acct>:assumed-role/...` is not authorized) | The Lambda execution role is missing the `sts:AssumeRole` IAM stmt. Most common root cause: the `BedrockHubRoleArn` parameter was deployed with the role ARN and the ExternalId concatenated into a single string (e.g. `arn:...:role/Foo ExternalId: xyz`). The IAM stmt is then synthesized with a corrupt Resource. **Fix**: redeploy the stack passing `BedrockHubRoleArn` and `BedrockHubRoleExternalId` as **separate** parameters. Verify with `aws iam get-role-policy ... --query 'PolicyDocument.Statement[?Action==\`sts:AssumeRole\`]'`. |
| `AccessDenied` on `sts:AssumeRole` blamed on the trust policy (error message references the resource role ARN, not the caller) | Hub-account trust policy missing the workload account/role; missing `ExternalId`; permissions boundary in the workload account blocking AssumeRole. |
| `AccessDenied` on `bedrock:InvokeModel` after AssumeRole succeeds | Hub-account role missing model access (e.g., not allow-listed for the requested foundation model). |
| `AccessDenied` on `bedrock:GetInferenceProfile` | The inference profile is in the workload account, not the hub account. Move it, or use a system-defined cross-region profile. |
| `AccessDenied` on `bedrock:ApplyGuardrail` | The Guardrail is in the workload account. Move it to the hub account or unset `BedrockGuardrailId`. |
| Errors after ~1 hour of warm Lambda activity | Should not happen — credentials auto-refresh. If it does, file a bug; the factory may have been bypassed. |
| `EndpointConnectionError` on `sts.<region>.amazonaws.com` from a VPC Lambda | Missing STS interface VPC endpoint. |

## Implementation notes

- Single source of truth: `lib/idp_common_pkg/idp_common/bedrock/session.py`.
- The factory is process-cached per region; same-process callers share the session and credential refresher.
- `BedrockClient`'s `lambda_client` and `s3_client` properties intentionally use the **calling-account** session — Lambda invocations and S3 operations stay local.
