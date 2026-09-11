---
title: "API Gateway Hosting Guide"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# API Gateway Hosting Guide

## Overview

The GenAI IDP Accelerator supports an alternative Web UI hosting mode that
serves the React single-page application (SPA) directly from **API Gateway**,
as an S3 proxy on the **same REST API** that already backs the UI's data
operations. This replaces CloudFront for environments that require VPC-based or
private-network hosting.

Set `WebUIHosting=APIGateway` to enable it. The default remains `CloudFront`.

> **Note**: For standard public deployments, CloudFront hosting (the default) is
> recommended. Use API Gateway hosting only when your environment has specific
> requirements that prevent using CloudFront.

> **Migrating from ALB hosting?** Earlier releases offered an `ALB` hosting
> option (an internal Application Load Balancer fronting an S3 VPC Interface
> Endpoint). That option has been **removed** and replaced by API Gateway
> hosting, which is simpler (no ALB, no S3 VPC endpoint, no ACM certificate) and
> reuses the REST API you already deploy. See
> [Migrating from ALB hosting](#migrating-from-alb-hosting) below.

## When to Use API Gateway Hosting

API Gateway hosting is designed for organizations that need the full IDP Web UI
but cannot use CloudFront due to network or compliance constraints:

- **Private network requirements** — combine with `ApiGatewayVisibility=PRIVATE`
  so the UI (and the API) is reachable only through a VPC interface endpoint.
- **Regulated / VPC-only environments** — all traffic traverses private network
  paths; protect the API stage with the built-in WAFv2 IP allow-list.
- **Corporate network restrictions** — users reach the app exclusively over VPN
  or Direct Connect into the VPC.
- **GovCloud-style constraints** where CloudFront is unavailable but you still
  want the UI (contrast with [Headless Deployment](./headless-deployment.md),
  which removes the UI entirely).

## Architecture

### Standard Hosting (CloudFront)

```
Internet Users → CloudFront (Edge) → S3 Origin (WebUI Bucket)
                                  ↘ (data) → API Gateway REST API → Lambda
```

### API Gateway Hosting

```
Users → API Gateway REST API (stage: /api)
          ├── GET  /            → S3 proxy → WebUI bucket / index.html
          ├── GET  /{proxy+}    → S3 proxy → WebUI bucket / <key>
          └── POST /op/{field}  → Lambda dispatcher (data operations, Cognito-authed)
```

The Web UI and the data API are served from **one origin and one stage**, so:

- **No CORS** between the UI and the API (same origin).
- **VPC support is inherited**: `ApiGatewayVisibility=PRIVATE` makes both the UI
  and the API reachable only via the execute-api VPC interface endpoint.
- **WAF is inherited**: the optional stage-level WAFv2 IP allow-list
  (`WAFAllowedIPv4Ranges`) covers the UI routes automatically.
- **No S3 VPC endpoint is required** — API Gateway reaches the Web UI bucket
  over AWS-internal networking via an IAM role, not through a customer VPCE.

> **Security headers**: there is no CloudFront response-headers policy in this
> mode, so the two S3-proxy methods set the headers themselves —
> `Content-Security-Policy`, `Strict-Transport-Security`,
> `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` and
> `Referrer-Policy` — on the SPA document, its assets, and their 404/500 responses. The CSP mirrors the
> CloudFront one (`nested/api-resolvers/template.yaml`, `Mappings ->
> WebUiSecurity`), differing only in `frame-ancestors 'none'` to match the
> stricter `X-Frame-Options: DENY` used here. The Cognito authorizer's 401/403 and
> the other gateway-level errors carry the same headers minus the CSP (they are
> JSON error bodies, already `nosniff`). Keep the two policies in sync when
> either changes.

### How the SPA is served

- The static GET routes use `AuthorizationType: NONE` (a browser fetching the
  SPA shell has no JWT yet); authentication happens inside the app when it calls
  `POST /op/{field}` with a Cognito token.
- `BinaryMediaTypes: */*` is enabled so images/fonts/JS/CSS bytes pass through
  intact. The JSON data path is unaffected — the dispatcher base64-decodes
  request bodies.
- The SPA uses **HashRouter**, so client-side routes live in the URL fragment
  (`/api/#/...`) and need no server-side rewrite — deep links are served by
  `index.html` at `/`, and a genuinely nonexistent asset path returns 404 (the
  `{proxy+}` route maps each path to the matching S3 key; there is no
  rewrite-to-`index.html` because hash routing makes it unnecessary).
- Because the API serves under the stage prefix `/api`, the UI is **built with
  Vite `base=/api/`** (CodeBuild sets `VITE_UI_BASE_PATH=/api/` automatically in
  this mode), so every asset URL resolves under `/api/`.

## Configuration

Minimum (public/regional API Gateway hosting, no VPC):

```
WebUIHosting=APIGateway
```

Private / VPC-only hosting (recommended for the network-restricted use case):

```
WebUIHosting=APIGateway
ApiGatewayVisibility=PRIVATE
DeployInVPC=true
VpcId=vpc-xxxxxxxx
PrivateSubnetIds=subnet-aaa,subnet-bbb
LambdaSubnetIds=subnet-aaa,subnet-bbb
LambdaSecurityGroupId=sg-xxxxxxxx
ApiGatewayVpcEndpointId=vpce-xxxxxxxx      # execute-api interface endpoint
```

Optional WAF IP allow-list (applies to the whole stage — UI + API):

```
WAFAllowedIPv4Ranges=203.0.113.0/24,198.51.100.0/24
```

> `ApiGatewayVisibility=PRIVATE` requires `DeployInVPC=true` and an existing
> **execute-api** VPC interface endpoint (with private DNS enabled) so the
> regional `execute-api` hostname resolves inside the VPC. All Lambdas run in
> the VPC in this mode.

## Prerequisites (PRIVATE / VPC mode)

You need an existing VPC providing:

1. **At least 2 private subnets in different AZs** for the Lambdas.
2. **Egress to AWS services** — either a NAT gateway or the full set of VPC
   interface endpoints (Bedrock, Textract, S3, DynamoDB, SQS, Step Functions,
   etc.). See [Private Network Deployment](./deployment-private-network.md).
3. **An `execute-api` interface endpoint** with `PrivateDnsEnabled: true`,
   passed as `ApiGatewayVpcEndpointId`.
4. **A Lambda security group** allowing outbound HTTPS, passed as
   `LambdaSecurityGroupId`.
5. **DNS resolution enabled** (`enableDnsSupport` + `enableDnsHostnames`).

A ready-made, self-contained test VPC (NAT egress + one execute-api endpoint) is
provided at `scripts/sdlc/apigw-hosting-test-vpc.yaml` and is used by the CI
deployment test.

## OAuth / Cognito callbacks

The UI URL in API Gateway mode is an output of the API resolvers nested stack,
which is created *after* the Cognito user-pool client. To avoid a circular
CloudFormation dependency, the stack registers the API Gateway callback / logout
URLs on the Cognito client via a post-deploy custom resource
(`WebUIClientOAuthUrls`) rather than declaring them inline. This is transparent —
no action is required — but note that if you use an **External IdP**, the OAuth
redirect URL is `https://<api-id>.execute-api.<region>.amazonaws.com/api/`.

If you front the app with a **custom domain**, set `CustomDomainUrl`; the Web UI
then uses `window.location.origin` for OAuth redirects so both the API Gateway
URL and the custom domain work.

## Accessing the UI

The application URL is the `ApplicationWebURL` stack output, e.g.
`https://<api-id>.execute-api.<region>.amazonaws.com/api/`. In PRIVATE mode this
resolves only inside the VPC (via the execute-api endpoint) — reach it from a
VPN/Direct Connect client, a bastion, or a workstation in the VPC.

## Migrating from ALB hosting

If you previously deployed with `WebUIHosting=ALB`:

1. The `ALB*` parameters (`ALBVpcId`, `ALBSubnetIds`, `ALBCertificateArn`,
   `ALBScheme`, `ALBAllowedCIDRs`) have been **removed**. Remove them from your
   parameter file / deployment scripts.
2. Set `WebUIHosting=APIGateway`. For the equivalent private-network posture,
   also set `ApiGatewayVisibility=PRIVATE`, `DeployInVPC=true`, and provide
   `VpcId` / `PrivateSubnetIds` / `LambdaSubnetIds` / `LambdaSecurityGroupId` /
   `ApiGatewayVpcEndpointId`.
3. You no longer need an ACM certificate, an ALB, or an auto-provisioned S3 VPC
   Interface Endpoint. (A **BYO** S3 endpoint for browser presigned-URL uploads
   is still supported via `S3VpcEndpointIdOverride` /
   `S3VpcEndpointDnsNameOverride` + `S3PresignedUrlViaVpcEndpoint=true`.)
4. The application URL changes from the ALB DNS name to the execute-api `/api`
   URL (see `ApplicationWebURL` output). Update any bookmarks / DNS aliases.

## Troubleshooting

- **UI loads but assets 404**: confirm the build ran with
  `VITE_UI_BASE_PATH=/api/` (CodeBuild sets this automatically when
  `WebUIHosting=APIGateway`). Asset URLs in `index.html` must start with `/api/`.
- **UI unreachable in PRIVATE mode**: verify you are inside the VPC and the
  execute-api endpoint has `PrivateDnsEnabled: true`; check the endpoint SG
  allows 443 from your client.
- **403 on the UI with WAF enabled**: your client IP is not in
  `WAFAllowedIPv4Ranges`.
- **OAuth redirect_mismatch with External IdP**: confirm the
  `WebUIClientOAuthUrls` custom resource succeeded (CloudWatch logs) and the
  client's callback URLs include the execute-api `/api/` URL.
