---
title: "GovCloud Deployment Guide"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# GovCloud Deployment Guide

Deploy the GenAI IDP Accelerator to AWS GovCloud (`us-gov-west-1` /
`us-gov-east-1`) with a single `idp-cli` command. There are two deployment
paths — pick one:

| | **Web UI: `--govcloud`** (recommended) | **Headless: `--headless`** |
|---|---|---|
| **Web UI** | ✅ Full React UI, served by API Gateway (no CloudFront) | ❌ Removed |
| **Chat / agents** | ✅ Works, non-streaming — see [Chat in GovCloud](#chat-in-govcloud-non-streaming) | ❌ Removed |
| **Authentication** | Cognito (same as commercial) | IAM; or OAuth2 client credentials for the optional Jobs API |
| **Access methods** | Web UI, S3 upload, `idp-cli`, SDK | S3 upload, `idp-cli`, SDK, optional `/jobs` REST API |
| **Network options** | Public, IP-restricted (WAF), or VPC-only (private API Gateway) | No VPC, or all-in-VPC (+ optional bastion host) |
| **When to choose** | You want the interactive UI in GovCloud | Programmatic-only pipelines, or policy prohibits Cognito / WAF / a UI |

Both paths:

- **Build from local source** (`--from-code .`) — public pre-built templates
  are not published for GovCloud regions.
- Use `arn:${AWS::Partition}:` ARNs throughout, so all references resolve in
  the `aws-us-gov` partition.
- Remove services that do not exist in GovCloud — CloudFront and Lambda
  Function URLs. `--headless` additionally removes the entire UI, Cognito,
  WAF, agents, HITL, and knowledge base. See
  [GovCloud Architecture](./govcloud-architecture.md) for the full removed
  vs. retained resource list.
- Are validated with `cfn-lint` during the build. The `--govcloud` transform
  additionally runs a **region-aware lint** against the target GovCloud region
  right after the transform: if any GovCloud-unsupported resource type
  survives (an `E3006` error), the `publish`/`deploy` fails loudly instead of
  surfacing the problem only at deploy time. (`cfn-lint` runs fully offline;
  if not installed the gate is skipped with a warning.)

The two flags are **mutually exclusive** — `--headless` removes the UI
entirely; `--govcloud` keeps it.

> **Legacy**: The `scripts/generate_govcloud_template.py` script is
> deprecated. Use `idp-cli deploy --govcloud --from-code .` or
> `idp-cli deploy --headless --from-code .` instead.

## Prerequisites

Install on the machine you build from:

1. bash shell (Linux, MacOS, Windows-WSL)
2. aws (AWS CLI)
3. [sam (AWS SAM)](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
4. Python 3.12 (required to generate templates)
5. Node.js >=22.12.0 and npm >=10.0.0
6. A local Docker daemon
7. The IDP CLI and SDK packages — run `make setup-venv` from the project root
   to create a `.venv` with everything installed, then
   `source .venv/bin/activate`.

Also request access to the default Bedrock models in your GovCloud region
before processing documents: `amazon.nova-lite-v1:0`, `amazon.nova-pro-v1:0`,
`us.anthropic.claude-3-5-sonnet-20240620-v1:0`, and
`anthropic.claude-3-7-sonnet-20250219-v1:0`.

> **Note**: The CLI creates the artifacts S3 bucket automatically. Customize
> with `--bucket-basename` and `--prefix`.

> **Note on `--parameters` formatting**: Commas inside multi-value parameters
> (like `PrivateSubnetIds`) don't need escaping — the CLI parses
> `--parameters` by looking for the next `key=` pattern, so commas within
> values are preserved automatically.

## Keeping the Web UI in GovCloud: `--govcloud`

GovCloud lacks two services the standard UI template uses — Amazon CloudFront
and Lambda Function URLs — so the standard template fails to even validate
there (`E3006 Resource type 'AWS::CloudFront::Distribution' does not exist in
'us-gov-west-1'`). The `--govcloud` flag transforms the template to:

- Remove every `AWS::CloudFront::*` resource and force
  `WebUIHosting=APIGateway`, so the Web UI is served as an S3 proxy on the
  same REST API that backs it (see
  [API Gateway Hosting](./apigateway-hosting.md)).
- Remove the `AWS::Lambda::Url` resource (the chat *streaming* endpoint),
  since [Lambda Function URLs are not available in GovCloud](https://docs.aws.amazon.com/govcloud-us/latest/UserGuide/govcloud-lambda.html).
  Chat still works — the UI automatically switches to a non-streaming path
  (see [Chat in GovCloud](#chat-in-govcloud-non-streaming) below).
- Remove the streaming endpoint's **handler** (`ChatStreamProcessorFunction`,
  its log group, its Lambda permission, and the IAM statement that granted
  invoke on it). It runs under the [AWS Lambda Web Adapter](https://github.com/awslabs/aws-lambda-web-adapter),
  which is published **only in the commercial partition** — AWS account IDs do
  not exist across partitions, so `arn:${AWS::Partition}:` substitution cannot
  make the layer ARN resolvable and the deploy fails with a 403 on
  `lambda:GetLayerVersion`. There is no GovCloud LWA publication to point the
  `LambdaWebAdapterLayerArn` override at, so that parameter is dropped from the
  GovCloud template too. This does **not** disable chat: the polling transport
  is served by the retained `AgentChatProcessorFunction` /
  `ChatWithDocumentProcessorFunction` via the UI REST API, and the streaming
  function was never in that path.

Everything else in the UI (Cognito auth, the REST API, WAF, document
processing, extraction, evaluation, Test Studio, discovery, knowledge base,
configuration) works as in commercial regions.

### Web UI, internet-facing

The simplest UI deployment. `--admin-email` is required for new stacks
(Cognito is retained; the initial temporary password is emailed to you).

```bash
idp-cli deploy \
  --stack-name my-idp-govcloud \
  --region us-gov-west-1 \
  --from-code . \
  --govcloud \
  --admin-email your.email@example.com \
  --wait
```

To restrict access by source IP, add a WAF allow-list (the default
`0.0.0.0/0` disables WAF):

```bash
  --parameters "WAFAllowedIPv4Ranges=203.0.113.0/24,198.51.100.0/24"
```

### Web UI, VPC-only (private API Gateway)

Serves the UI and its API only through your VPC's `execute-api` interface
endpoint. Requires `DeployInVPC=true` plus your VPC networking parameters —
see [API Gateway Hosting](./apigateway-hosting.md) and
[VPC Secured Mode](./vpc-secured-mode.md) for prerequisites.

```bash
idp-cli deploy \
  --stack-name my-idp-govcloud \
  --region us-gov-west-1 \
  --from-code . \
  --govcloud \
  --admin-email your.email@example.com \
  --wait \
  --parameters "ApiGatewayVisibility=PRIVATE,DeployInVPC=true,VpcId=vpc-xxxxxxxx,PrivateSubnetIds=subnet-a,subnet-b,LambdaSubnetIds=subnet-a,subnet-b,LambdaSecurityGroupId=sg-xxxxxxxx,ApiGatewayVpcEndpointId=vpce-xxxxxxxx"
```

### Build the GovCloud template without deploying

```bash
idp-cli publish --source-dir . --region us-gov-west-1 --govcloud
```

The transformed template is written to `.aws-sam/idp-govcloud.yaml` and
uploaded as `idp-govcloud.yaml`. Deploy it later with
`idp-cli deploy --template-file .aws-sam/idp-govcloud.yaml ...` or through
the CloudFormation console.

### Chat in GovCloud (non-streaming)

With `--govcloud`, agent chat and document chat work, but without live
token streaming:

- **Commercial (streaming):** the browser opens a streaming connection to a
  Lambda Function URL and renders the answer token-by-token, including
  intermediate agent progress ("calling tool X…").
- **GovCloud (non-streaming):** the browser sends the chat message over the
  REST API, which asynchronously invokes the same chat processor
  (`AgentChatProcessorFunction` / `ChatWithDocumentProcessorFunction` — these
  are *retained* in GovCloud; only the LWA-based streaming front end is
  removed); the UI then **polls** for the final answer. The user sees a spinner
  until the complete answer appears at once. **The final answer is identical to
  streaming.**

This is auto-detected — the UI streams when a Function URL is configured
(`VITE_STREAM_URL`) and polls when it is not; no configuration is needed. The
polling path reuses the Cognito-authed REST API, so it inherits the same
`ApiGatewayVisibility=PRIVATE` / WAF posture as the rest of the UI. Long agent
turns are supported (the UI polls for up to 5 minutes).

## Not Available in GovCloud (all deploy modes)

Unlike the transforms above, these gaps are partition conditions in the
templates themselves, so they apply to `--govcloud`, `--headless` and the
untransformed template alike.

### Bedrock Data Automation as the OCR backend

The `bda` OCR backend needs a stack-scoped BDA **SYNC** project whose
`standardOutputConfiguration` carries a `document` block. BDA itself is
available in `us-gov-west-1`, but that specific project shape is not — the API
rejects it with `ValidationException: Sync project does not support
video/audio/document modality in Standard Output Configuration`.

`BDAOCRProject` is therefore created only in the commercial partition
(condition `ShouldCreateBDAOCRProject`). In GovCloud the project isn't created
and `BDA_OCR_PROJECT_ARN` is empty; the OCR service raises a clear error *only
if* `ocr.backend` is actually set to `bda`. Use `ocr.backend: textract` — the
built-in default. The `lending-package-sample-govcloud` preset sets no `ocr:`
key, so the default applies and no configuration change is needed.

> Before this gate existed the resource was created unconditionally and its
> failure rolled back the entire root stack on **every** GovCloud deployment,
> regardless of deploy mode or the configured `ocr.backend`.

## Headless Deployment: `--headless`

`--headless` removes the Web UI and everything that exists to serve it
(UI REST API resolvers, Cognito UI auth, WAF, agents, HITL, knowledge
base), keeping the full document-processing backend. See the
[Headless Deployment Guide](./headless-deployment.md) for the general
(non-GovCloud-specific) reference.

> **`--headless` vs. `EnableJobsApi=true`** — these are different things:
>
> - `--headless` (CLI flag) transforms the **template**: it strips the UI
>   resource groups above. It does **not** set any stack parameters.
> - `EnableJobsApi=true` (CloudFormation parameter, formerly `EnableHeadless`)
>   is an **additive** switch that deploys the **Batch Jobs REST API** (`/jobs`
>   endpoints on a private API Gateway with OAuth2 machine-to-machine auth) —
>   it does **not** remove the UI. It requires `DeployInVPC=true` plus your
>   VPC parameters — the template rejects it otherwise at changeset creation.
>
> If you want the Jobs API you must pass `EnableJobsApi=true` and the VPC
> parameters explicitly, as in [Option B](#option-b-headless--jobs-rest-api-all-lambdas-in-vpc) below.

### Deployment Packages

| | Option A: Vanilla | Option B: Jobs REST API (VPC) | Option C: + Bastion |
|---|---|---|---|
| **Use case** | Simplest deployment; drive processing via S3 upload or IDP CLI | Production API access with all compute isolated in your VPC | Development/testing: call the private API from your laptop via SSM tunnel |
| **Access methods** | S3 direct upload, IDP CLI, SDK | Vanilla methods + `/jobs` REST API (private API Gateway) | Same, plus local access through the bastion tunnel |
| **Networking** | No VPC required | All Lambdas + private API Gateway in your VPC | Same, plus an EC2 bastion host (SSM only, no inbound rules) |
| **Authentication** | IAM only | Cognito client credentials (OAuth2 bearer tokens) | Same as Option B |
| **Extra parameters** | None | `EnableJobsApi=true`, `DeployInVPC=true`, `VpcId`, `PrivateSubnetIds`, `ApiGatewayVpcEndpointId`, `LambdaSecurityGroupId` | Option B + `DeployBastionHost=true`, `BastionHostSubnetId`, `BastionHostSecurityGroupId` |

#### Option A: Vanilla (no API, no VPC)

```bash
idp-cli deploy \
  --stack-name my-idp-headless \
  --region us-gov-west-1 \
  --from-code . \
  --headless \
  --wait
```

No `--admin-email` is needed — the headless template has no Cognito user
pool. Interact with the stack via direct S3 upload, `idp-cli`, or the SDK
(see [Processing documents](#processing-documents-headless) below).

#### Option B: No-UI (`--headless`) + Jobs REST API (all Lambdas in VPC)

Deploys the `/jobs` REST API as a **private** API Gateway reachable only
through your VPC's `execute-api` interface endpoint, with all Lambda
functions inside your VPC. Make sure the
[VPC Secured Mode prerequisites](./vpc-secured-mode.md) are met first.

```bash
idp-cli deploy \
  --stack-name my-idp-headless \
  --region us-gov-west-1 \
  --from-code . \
  --headless \
  --wait \
  --parameters "EnableJobsApi=true,DeployInVPC=true,VpcId=vpc-xxxxxxxxx,PrivateSubnetIds=subnet-xxxxx,subnet-xxxxx,ApiGatewayVpcEndpointId=vpce-xxxxxxxxx,LambdaSecurityGroupId=sg-xxxxxxxxx,ApiStageName=beta"
```

See [Batch Jobs REST API](./govcloud-batch-api.md) for authentication and
endpoint usage.

#### Option C: Option B + Bastion host (development)

Adds a small EC2 bastion (no inbound rules; access via AWS SSM Session
Manager) so you can tunnel to the private API from your local machine.

```bash
idp-cli deploy \
  --stack-name my-idp-headless \
  --region us-gov-west-1 \
  --from-code . \
  --headless \
  --wait \
  --parameters "EnableJobsApi=true,DeployInVPC=true,VpcId=vpc-xxxxxxxxx,PrivateSubnetIds=subnet-xxxxx,subnet-xxxxx,ApiGatewayVpcEndpointId=vpce-xxxxxxxxx,LambdaSecurityGroupId=sg-xxxxxxxxx,ApiStageName=beta,DeployBastionHost=true,BastionHostSubnetId=subnet-xxxxxxxxx,BastionHostSecurityGroupId=sg-xxxxxxxxx"
```

See [Private API Access via Bastion Tunnel](./govcloud-batch-api.md#private-api-access-via-bastion-tunnel)
for tunnel setup (`./scripts/bastion.sh <STACK_NAME>`).

### Processing documents (headless)

Without the Web UI, use `idp-cli` for the full round trip:

```bash
# Upload and process a directory of documents, monitoring until completion
idp-cli run-inference \
    --stack-name my-idp-headless \
    --dir ./samples/ \
    --monitor

# Check status later (batch ID is printed by run-inference)
idp-cli status --stack-name my-idp-headless --batch-id <batch-id>

# Download the results
idp-cli download-results \
    --stack-name my-idp-headless \
    --batch-id <batch-id> \
    --output-dir ./results/
```

Or upload directly to the input bucket (name is in the stack Outputs) and
monitor via the Step Functions console link in the stack Outputs:

```bash
aws s3 cp my-document.pdf s3://<InputBucket>/my-document.pdf
```

## Updating an Existing Stack

Re-run the same `idp-cli deploy` command (same flags) to build and apply
template or code changes. Parameters you omit keep their previous values.

## Monitoring & Troubleshooting

Monitoring (CloudWatch dashboard `{StackName}-{Region}`, alarms, log groups)
and operational troubleshooting are covered in
[GovCloud Operations](./govcloud-operations.md).

Common deployment issues:

- **Build failures** — re-run with `--verbose` (publish) to see detailed
  errors; ensure Docker is running and Node.js >= 22.12.
- **`E3006` cfn-lint errors during publish/deploy** — a GovCloud-unsupported
  resource type survived the transform; this is a bug worth reporting, not a
  local misconfiguration.
- **Processing failures** — confirm the default Bedrock models (listed under
  [Prerequisites](#prerequisites)) are enabled in your GovCloud region, then
  check CloudWatch logs.
- **"Region '…' is not supported" with `--govcloud`/`--headless` but without
  `--from-code`** — pre-built templates only exist for a few commercial
  regions; in GovCloud always pass `--from-code .` (or `--template-url`).

## Migration from Commercial AWS

1. **Export configuration** from the existing stack (Configuration bucket /
   `idp-cli`).
2. **Export data**: copy any evaluation baseline or reference data.
3. **Deploy to GovCloud** using one of the commands above.
4. **Import configuration** into the new stack (`--custom-config` or the UI).
5. **Validate** with sample documents.

## Cost & Compliance Notes

- GovCloud pricing differs from commercial regions — see
  [GovCloud Pricing](https://aws.amazon.com/govcloud-us/pricing/) and update
  `config_library/pricing.yaml` estimates if you rely on cost reporting.
- Both deployment paths keep customer-managed KMS encryption, data-retention
  lifecycle policies, and process everything within the GovCloud boundary —
  no data egress to commercial regions.

## Related Documentation

- [GovCloud Architecture](./govcloud-architecture.md) — services removed vs.
  retained, limitations, and workarounds
- [Batch Jobs REST API](./govcloud-batch-api.md) — Jobs API reference,
  authentication, bastion tunnel
- [GovCloud Operations](./govcloud-operations.md) — monitoring and
  troubleshooting
- [API Gateway Hosting](./apigateway-hosting.md) — how the Web UI is served
  without CloudFront
- [Headless Deployment Guide](./headless-deployment.md) — headless mode in
  general (Commercial and GovCloud)
- [VPC Secured Mode](./vpc-secured-mode.md) — VPC prerequisites
