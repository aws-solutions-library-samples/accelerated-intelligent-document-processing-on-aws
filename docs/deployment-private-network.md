---
title: "Deploying IDP in a Private Network"
---

# Deploying IDP in a Private Network

This runbook deploys the GenAI IDP Accelerator in a **fully private / air-gapped environment**:

- Web UI served via **API Gateway** — an S3 proxy on the same private REST API that backs the UI's data operations (no CloudFront, no ALB, no public internet)
- REST API (Web UI **and** data operations) reachable **only from inside the VPC** via an **execute-api Interface VPC Endpoint** (no public endpoint)
- All Lambda → AWS service traffic routed through **VPC Interface Endpoints**
- Browser → S3 presigned uploads routed through **global S3** (default, via NAT) or optionally through a **bring-your-own S3 Interface VPC Endpoint** (opt-in via `S3PresignedUrlViaVpcEndpoint=true`)
- Internet-facing features (MCP Gateway, Knowledge Base) **disabled**

> For standard public deployments, see [Deployment Guide](./deployment.md). For the full
> reference on this hosting mode (including public/regional variants), see
> [API Gateway Hosting Guide](./apigateway-hosting.md).

---

## Architecture overview

```
Browser (VPN/DC client)
    │
    │ HTTPS
    ▼
execute-api Interface VPC Endpoint (PrivateDnsEnabled=true)
    │
    ▼
API Gateway REST API (private, stage: /api)
    ├── GET  /            → S3 proxy → Web UI bucket / index.html
    ├── GET  /{proxy+}    → S3 proxy → Web UI bucket / <key>
    └── POST /op/{field}  → Lambda dispatcher (data operations, Cognito-authed)

    browser also calls (via VPN/DC):
        ├── Cognito user-pool          (public — needs egress, see WorkSpaces note)
        └── S3 presigned upload     → global s3.amazonaws.com (default, via NAT)
                                       OR BYO S3 Interface VPC Endpoint (opt-in)

Lambda functions (in VPC subnets, attached to LambdaSecurityGroup)
    │
    └── all AWS API calls → VPC Interface / Gateway Endpoints
        (Bedrock, Textract, SQS, States, KMS, Logs, CloudWatch, SSM,
         Lambda, Events, Athena, STS, S3, DynamoDB, ...)
```

The Web UI and the data API are served from **one origin and one stage** on the private
REST API, so there is no CORS between them and VPC access is inherited from the
execute-api endpoint. **No S3 VPC endpoint is created for UI hosting** — API Gateway reaches
the Web UI bucket over AWS-internal networking via an IAM role (`WebUIProxyRole`), not through
a customer VPCE. Browser uploads use global S3 by default (via NAT); set
`S3PresignedUrlViaVpcEndpoint=true` to route them through a **bring-your-own** S3 Interface VPC
Endpoint instead (requires corporate network DNS/routing to VPCE hostnames).

---

## Two deployment models

API Gateway hosting does **not** create any S3 VPC endpoint — the UI is served by the private
REST API, not by an S3-fronting ALB. The two models below differ only in whether browser
presigned **uploads** route through a bring-your-own S3 Interface VPCE.

| Mode | What you supply | What the stack creates |
|------|-----------------|------------------------|
| **A. Global-S3 uploads (default)** | VPC id, subnet ids, Lambda SG (or stack-managed), execute-api VPCE id. | The private REST API + WebUI S3 proxy. Browser uploads go to global `s3.amazonaws.com` via NAT. `scripts/deploy-vpc-endpoints.py` adds the service endpoints. |
| **B. Customer-managed S3 VPCE uploads** | Everything in A **plus** a pre-existing S3 Interface VPCE in the VPC, its `vpce-id`, its full DNS name (with random suffix), and `S3PresignedUrlViaVpcEndpoint=true`. | Nothing extra — the stack reads the VPCE id and DNS name from parameters and rewrites presigned upload URLs to use it. |

Mode B fits central-network-account topologies where one team manages all VPC endpoints and IDP is one of many tenants.

---

## Prerequisites

### 1. Build tools

See [Deployment Guide → Dependencies](./deployment.md#dependencies) for AWS CLI, SAM CLI, Python 3.12+, Node.js 22+. Docker is **not** required locally — images are built in AWS by CodeBuild.

> **Note**: When `DeployInVPC=true`, all CodeBuild projects (WebUI build, Docker image builds, SDLC pipeline) run inside the VPC. They require either a **NAT Gateway** for public registry access or an **internal artifact repository** (JFrog Artifactory, AWS CodeArtifact, etc.) for air-gapped builds. See [Dependency Mirroring](./dependency-mirroring.md) for generating the full dependency manifest.

### 2. VPC requirements

- **At least 2 private subnets in different Availability Zones** — for the Lambdas.
- **DNS resolution enabled** on the VPC — `enableDnsSupport=true` and `enableDnsHostnames=true`.
- **An `execute-api` Interface VPC Endpoint** with `PrivateDnsEnabled: true`, passed as `ApiGatewayVpcEndpointId`. This is what makes the regional `execute-api.<region>.amazonaws.com` hostname resolve to private IPs inside the VPC — it fronts **both the Web UI and the data API**.
- **(Mode B only) DNS resolution for the BYO S3 Interface VPCE** — `PrivateDnsEnabled` is **off** for S3 VPCEs by AWS design; bucket-vhost addressing routes via the per-endpoint `bucket.<vpce-id>-<random>.s3.<region>.vpce.amazonaws.com` name. **VPC DNS resolution is what makes that name resolvable** for in-VPC clients (Lambdas). Browsers only need to resolve this name when `S3PresignedUrlViaVpcEndpoint=true`; with the default (`false`), browsers use `s3.amazonaws.com` via NAT.
- **A Lambda security group** allowing outbound HTTPS, passed as `LambdaSecurityGroupId` (or let the stack create one).
- **No `0.0.0.0/0 → IGW` or `→ NAT` route** on the Lambda subnet — proves no public-internet egress (verify after deployment).

> **Don't have a VPC?** A ready-made, self-contained test VPC (NAT egress + one execute-api
> endpoint) is provided at `scripts/sdlc/apigw-hosting-test-vpc.yaml` and is used by the CI
> deployment test:
> ```bash
> aws cloudformation deploy \
>   --stack-name IDP-TestVPC \
>   --template-file scripts/sdlc/apigw-hosting-test-vpc.yaml \
>   --capabilities CAPABILITY_IAM \
>   --region us-east-1
> aws cloudformation describe-stacks --stack-name IDP-TestVPC \
>   --query 'Stacks[0].Outputs[*].{Key:OutputKey,Value:OutputValue}' --output table
> ```

### 3. Network connectivity

Users must reach the private REST API via VPN, Direct Connect, AWS Client VPN, WorkSpaces, or SSM port forwarding. The regional `execute-api` hostname is **only resolvable inside the VPC** (via the execute-api endpoint's private DNS) or via a VPC DNS forwarder over the VPN. No TLS certificate is required — API Gateway terminates HTTPS with an AWS-managed certificate for the regional `execute-api` domain.

---

## One-click console deployment (CloudFormation)

Every private-mode knob is exposed as a CloudFormation parameter, so the stack can be deployed entirely via the AWS console "Create stack" form — no CLI required.

### Pre-requisites (must complete before clicking Launch)

1. **Publish artifacts** to your S3 bucket once (Step 1 below). One-click reuses `idp-main.yaml` from the artifact bucket. The bucket can live in the same or a different account.
2. **VPC + subnets** — 2+ subnets in 2+ AZs (`enableDnsSupport=true`, `enableDnsHostnames=true`).
3. **execute-api Interface VPC Endpoint** — `vpce-id` with `PrivateDnsEnabled: true` (see Prereq §2), passed as `ApiGatewayVpcEndpointId`.
4. **(Mode B only)** the `vpce-id` and full DNS name (with random suffix) of your existing S3 Interface VPCE.

### Launch URL

Construct the 1-click URL once after publishing:

```
https://<region>.console.aws.amazon.com/cloudformation/home?region=<region>#/stacks/create/review?templateURL=https://s3.<region>.amazonaws.com/<bucket>/idp/idp-main.yaml&stackName=IDP-PRIVATE
```

`idp-cli publish` prints the exact URL at the end of its run. Bookmark it for the customer.

### Parameters to set in the console form

#### Required for both modes

| Parameter | Value |
|-----------|-------|
| `AdminEmail` | admin@example.com |
| `WebUIHosting` | `APIGateway` |
| `ApiGatewayVisibility` | `PRIVATE` |
| `DeployInVPC` | `true` |
| `VpcId` | vpc-xxx |
| `PrivateSubnetIds` | subnet-a,subnet-b *(comma-separated, 2+ AZs)* |
| `LambdaSubnetIds` | subnet-a,subnet-b *(can match `PrivateSubnetIds`)* |
| `ApiGatewayVpcEndpointId` | vpce-xxx *(execute-api interface endpoint, `PrivateDnsEnabled: true`)* |
| `EnableMCP` | `false` |
| `DocumentKnowledgeBase` | `DISABLED` |

#### Optional

| Parameter | When to set |
|-----------|-------------|
| `LambdaSecurityGroupId` | BYO Lambda SG. Empty = stack creates one. |
| `WAFAllowedIPv4Ranges` | Restrict the API stage (UI + API) to specific CIDRs via the built-in WAFv2 IP allow-list. Empty = no IP restriction. |
| `S3PresignedUrlViaVpcEndpoint` | Set to `true` to route browser S3 uploads through a BYO S3 VPCE instead of global S3. Default `false` (uses NAT). |
| `ArtifactsBucketKmsKeyArn` | Required if artifact bucket is KMS-encrypted. |

#### Mode B only (BYO S3 VPC Endpoint)

| Parameter | Value |
|-----------|-------|
| `S3VpcEndpointIdOverride` | vpce-0123... *(your existing S3 Interface VPCE)* |
| `S3VpcEndpointDnsNameOverride` | vpce-0123-abcdef12.s3.us-east-1.vpce.amazonaws.com *(full DNS, random suffix included, no leading `*.`)* |

> **Both must be set together.** A CloudFormation `Rules` assertion blocks change-set creation if only one is provided. Run `aws ec2 describe-vpc-endpoints --vpc-endpoint-ids <vpce-id> --query 'VpcEndpoints[0].DnsEntries[0].DnsName'` and strip the leading `*.` to get the DNS-name value.

### After clicking "Create stack"

CloudFormation provisions the main stack + nested stacks (API resolvers, Pattern, DocumentKB, MultiDocDiscovery). Expect ~25–35 min.

Then complete the **post-stack** operation the console can't do:

1. **Deploy service VPC endpoints** (Step 3 below) — required so Lambdas can reach Bedrock, Textract, etc.

Once that post-stack step completes, the UI is reachable from any VPN/DC client at the URL in the `ApplicationWebURL` stack output (the execute-api `/api/` URL).

---

## Step 1: Build and publish artifacts

```bash
export PATH="/opt/homebrew/opt/node@22/bin:$PATH"  # macOS with brew node@22
node --version  # must be v22.x or later

idp-cli publish \
  --source-dir . \
  --bucket-basename <bucket-basename> \
  --prefix idp \
  --region <region>
# Example: idp-cli publish --source-dir . --bucket-basename idp-<account-id> --prefix idp --region us-east-1
```

Creates `<bucket-basename>-<region>` if needed, builds Lambda layers and templates, uploads. Prints the **Template URL** to use in Step 2.

> `publish.py` is deprecated — use `idp-cli publish`.

### Enterprise artifact bucket hardening (optional)

Pre-create a compliant bucket (KMS, tags, bucket policy, access logging, block public access) and pass it via `--bucket-basename`. When deploying, pass `ArtifactsBucketKmsKeyArn=<key-arn>` so CodeBuild (`DockerBuildRole`) and `ConfigurationCopyFunction` get `kms:Decrypt`.

```bash
aws s3api create-bucket --bucket my-idp-artifacts --region us-east-1
aws s3api put-bucket-encryption --bucket my-idp-artifacts \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms","KMSMasterKeyID":"<key-arn>"}}]}'
idp-cli publish --source-dir . --bucket-basename my-idp-artifacts --prefix idp --region <region>
```

---

## Step 2: Deploy the IDP stack

### Mode A — global-S3 uploads (recommended for first deploys)

The stack deploys the private REST API + WebUI S3 proxy. Browser presigned uploads go to
global `s3.amazonaws.com` via NAT; no S3 VPC endpoint is involved.

```bash
idp-cli deploy \
  --stack-name IDP-PRIVATE \
  --template-url https://s3.<region>.amazonaws.com/<bucket>/idp/idp-main.yaml \
  --admin-email admin@example.com \
  --region <region> \
  --wait \
  --parameters "WebUIHosting=APIGateway,\
ApiGatewayVisibility=PRIVATE,\
DeployInVPC=true,\
VpcId=<vpc-id>,\
PrivateSubnetIds=<subnet-1>,<subnet-2>,\
LambdaSubnetIds=<subnet-1>,<subnet-2>,\
ApiGatewayVpcEndpointId=<execute-api-vpce-id>,\
EnableMCP=false,\
DocumentKnowledgeBase=DISABLED"
```

### Mode B — customer-managed S3 VPCE for uploads (BYO endpoint)

Set `S3PresignedUrlViaVpcEndpoint=true` plus both `S3VpcEndpointIdOverride` **and** `S3VpcEndpointDnsNameOverride`. The stack rewrites presigned upload URLs to use your S3 VPCE. The deploy fails fast at change-set creation if only one of the two override params is set (CFN `Rules` assertion).

Discover your VPCE's full DNS name (the `vpce-<id>-<random>` suffix is AWS-assigned and **cannot be derived from the id alone**):

```bash
VPCE_ID=vpce-0123456789abcdef0
DNS_NAME=$(aws ec2 describe-vpc-endpoints \
  --vpc-endpoint-ids $VPCE_ID \
  --region <region> \
  --query 'VpcEndpoints[0].DnsEntries[0].DnsName' \
  --output text \
  | sed 's|^\*\.||')
echo $DNS_NAME
# vpce-0123456789abcdef0-abcdef12.s3.us-east-1.vpce.amazonaws.com
```

Add to deploy:

```bash
  --parameters "...,\
S3VpcEndpointIdOverride=$VPCE_ID,\
S3VpcEndpointDnsNameOverride=$DNS_NAME"
```

### Key parameters

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `WebUIHosting` | `APIGateway` | Serve the UI from the REST API (S3 proxy) instead of CloudFront |
| `ApiGatewayVisibility` | `PRIVATE` | REST API (UI + data) only inside VPC via execute-api endpoint |
| `DeployInVPC` | `true` | Place Lambdas + CodeBuild in the VPC |
| `VpcId` | vpc-id | VPC for the deployment |
| `PrivateSubnetIds` | subnet ids | Private subnets (2+ AZs) |
| `LambdaSubnetIds` | subnet ids | Subnets where Lambdas run (can match `PrivateSubnetIds`) |
| `ApiGatewayVpcEndpointId` | `vpce-...` | execute-api Interface VPCE (`PrivateDnsEnabled: true`); fronts UI + API |
| `LambdaSecurityGroupId` | (optional) sg-id | BYO Lambda SG; stack creates one if empty |
| `WAFAllowedIPv4Ranges` | (optional) CIDRs | Stage-level WAFv2 IP allow-list (UI + API). Empty = no IP restriction |
| `S3PresignedUrlViaVpcEndpoint` | (optional) `true`/`false` | Route browser S3 uploads via BYO VPCE (`true`) or global S3 via NAT (`false`, default) |
| `S3VpcEndpointIdOverride` | (Mode B) `vpce-...` | BYO S3 VPCE id |
| `S3VpcEndpointDnsNameOverride` | (Mode B) full DNS w/ suffix | BYO S3 VPCE DNS name (required if id override is set) |
| `EnableMCP` | `false` | Disable AgentCore Gateway (public-only) |
| `DocumentKnowledgeBase` | `DISABLED` | Disable KB (cuts extra VPC endpoints) |
| `ArtifactsBucketKmsKeyArn` | (optional) key arn | Required if artifact bucket is KMS-encrypted |

> `--wait` streams stack events and exits non-zero on failure (useful for CI).

---

## Step 3: Deploy service VPC endpoints

The stack itself creates no service VPC endpoints. You supply the **execute-api** interface
endpoint (fronts the private REST API — UI + data) as `ApiGatewayVpcEndpointId`, and the
remaining service endpoints (Bedrock, Textract, S3, SQS, States, KMS, Logs, monitoring, SSM,
Lambda, Events, Athena, STS, secretsmanager, plus DynamoDB Gateway) come from
`scripts/vpc-endpoints.yaml` via the deploy script:

```bash
python scripts/deploy-vpc-endpoints.py \
  --vpc-id <vpc-id> \
  --stack-name IDP-PRIVATE \
  --region <region>
```

The script:

1. Reads `LambdaSubnetIds` and `LambdaVpcSecurityGroupId` from the IDP stack outputs
2. Detects which endpoints already exist in the VPC and skips them (avoids `conflicting DNS domain`)
3. Creates a shared endpoint SG that allows **443 from the Lambda SG**
4. **Adds egress from the Lambda SG to the VPC endpoints SG** — required when the Lambda SG has restricted egress (no default `0.0.0.0/0` rule)
5. Includes the **`monitoring` (CloudWatch) endpoint** required by the `DashboardMerger` custom resource that fires during stack create/update — without it the stack hangs

Pass `--dry-run` to preview without changes.

> **⚠️ Lambda SG egress requirement**: If your Lambda SG does NOT have a default `0.0.0.0/0` egress rule (common in hardened environments), you must also add egress to the S3 and DynamoDB **Gateway endpoint prefix lists**. The `check-vpc-endpoints.sh` script detects this and prints the required `aws ec2 authorize-security-group-egress` commands. Without these rules, custom resource Lambdas (InitializeConcurrencyTable, ConfigurationCopy, dataset deployers) will time out because they cannot reach DynamoDB or send cfnresponse via S3.

> **Mode B note**: if you bring your own VPCEs for some/all services, set the corresponding `Create*Endpoint=false` flags in the `vpc-endpoints.yaml` parameters. The script auto-detects existing ones.

> **Cross-account Bedrock note**: if you use the [Cross-Account Bedrock](./cross-account-bedrock.md) feature (`BedrockHubRoleArn` set), the **STS interface VPC endpoint is mandatory** — Lambdas need it for `sts:AssumeRole` against the hub-account role. The endpoint is already in `scripts/vpc-endpoints.yaml`; just verify it deployed.

---

## Web UI S3 access — security model

API Gateway hosting creates **no S3 VPC endpoint**. The REST API serves the SPA by proxying
GET requests to the Web UI bucket over AWS-internal networking, authenticated with an IAM role
(`WebUIProxyRole`) rather than through a customer VPCE. The proxy integration and the private
endpoint live in the api-resolvers nested stack (`nested/api-resolvers/template.yaml`) and the
main `template.yaml` (`WebUIProxyRole`, and the APIGateway branch of `WebUIBucketPolicy`).

Authorization for *which* buckets can be read or written is enforced at three layers:

| Layer | Mechanism | Where |
|---|---|---|
| API | Stage-level WAFv2 IP allow-list (`WAFAllowedIPv4Ranges`) + Cognito auth on `POST /op/{field}` | `nested/api-resolvers/template.yaml` |
| IAM | `WebUIProxyRole` (read-only, Web UI bucket) + per-Lambda execution-role policies (least-privilege) | `template.yaml` |
| Bucket | Web UI bucket policy restricts reads to the API Gateway proxy role | `template.yaml::WebUIBucketPolicy` (APIGateway branch) |

### Mode B — BYO S3 VPCE for uploads

When `S3PresignedUrlViaVpcEndpoint=true`, browser presigned **uploads** route through your
bring-your-own S3 Interface VPCE. Your central network team owns that endpoint's policy; IDP
just consumes it via `S3VpcEndpointIdOverride` + `S3VpcEndpointDnsNameOverride`. Apply your
own scoping (e.g. `aws:PrincipalAccount`, bucket ARNs) on that endpoint policy as needed. Each
app bucket is additionally pinned to the VPCE via an `aws:sourceVpce` condition in
`template.yaml::*BucketPolicy`.

---

## Security group matrix

The Lambdas attach to `LambdaSecurityGroupId` (BYO or stack-created). The service endpoints in
`scripts/vpc-endpoints.yaml` use a shared endpoint SG with ingress 443 from the Lambda SG. The
**execute-api** endpoint you supply must allow ingress 443 from both the Lambdas and the
browser's source network:

| Source SG / network | Direction | Port | Purpose |
|-----------|-----------|------|---------|
| `LambdaSecurityGroupId` | ingress → endpoint SG | 443 | All app Lambdas → AWS service endpoints |
| Browser source network | ingress → execute-api endpoint SG | 443 | Browser → private REST API (UI + data) |

### Browser/VPN access — additional ingress

VPN clients are **source-NAT'd to the IP of the VPN-association subnet** (AWS Client VPN, Site-to-Site VPN). Add ingress on the relevant SGs from the **NAT subnet's CIDR**, not the VPN client CIDR.

| Target SG | Add ingress | Port | Reason |
|-----------|-------------|------|--------|
| execute-api endpoint SG | <NAT-subnet-CIDR> (e.g. `10.1.3.0/24`) | 443 | Browser → private REST API (UI + data). |
| BYO S3 VPCE endpoint SG | <NAT-subnet-CIDR> | 443 | Browser → S3 VPCE for presigned upload. **Only needed when `S3PresignedUrlViaVpcEndpoint=true`.** |

**AWS Client VPN example**: client tunnel IPs are 172.16.0.0/22, but inbound packets to the endpoint are seen as coming from the VPN association subnet (e.g. 10.1.3.0/24). Authorize 10.1.3.0/24, not 172.16.0.0/22.

**Site-to-Site VPN over VGW + corporate VPN**: source-NAT happens at the customer firewall. Use the post-NAT IP range advertised into the VPC.

> Production with Direct Connect / Transit Gateway: typically no NAT — clients reach VPC with on-prem IPs. Authorize those CIDRs on the execute-api endpoint SG as part of standard onboarding.

> **Note:** When `S3PresignedUrlViaVpcEndpoint=false` (default), browser uploads go to `s3.amazonaws.com` via NAT/internet — no S3 endpoint SG ingress rule needed for browsers.

---

## Step 4: Access the UI

```bash
aws cloudformation describe-stacks --stack-name IDP-PRIVATE \
  --query 'Stacks[0].Outputs[?OutputKey==`ApplicationWebURL`].OutputValue' \
  --output text
```

Open the URL (the execute-api `/api/` URL) via VPN/DC. Confirm:

- ✅ Login succeeds
- ✅ Upload a doc — status updates live (`QUEUED → OCR → CLASSIFICATION → EXTRACTION → ...`)
- ✅ DevTools → Network for upload POST shows:
  - If `S3PresignedUrlViaVpcEndpoint=true`: host = `<bucket>.bucket.vpce-<id>-<random>.s3.<region>.vpce.amazonaws.com`
  - If `S3PresignedUrlViaVpcEndpoint=false` (default): host = `<bucket>.s3.<region>.amazonaws.com` (via NAT)

### Verify upload path (post-deploy)

```bash
# 1) Check presigned URL mode
LG=$(aws cloudformation describe-stack-resources --stack-name <api-resolvers-nested> \
  --query 'StackResources[?LogicalResourceId==`UploadResolverFunction`].PhysicalResourceId' --output text)
aws lambda get-function-configuration --function-name $LG --region <region> \
  --query 'Environment.Variables.S3_ENDPOINT_URL' --output text
# If S3PresignedUrlViaVpcEndpoint=true:
#   Expect: https://bucket.vpce-<id>-<random>.s3.<region>.vpce.amazonaws.com
# If S3PresignedUrlViaVpcEndpoint=false (default):
#   Expect: None (no env var set — presigned URLs use global s3.amazonaws.com)

# 2) Confirm Lambda subnet routing
aws ec2 describe-route-tables \
  --filters "Name=association.subnet-id,Values=<lambda-subnet-id>" \
  --region <region> \
  --query 'RouteTables[0].Routes[].[DestinationCidrBlock,DestinationPrefixListId,GatewayId]' --output text
# For S3PresignedUrlViaVpcEndpoint=false: NAT route (0.0.0.0/0 → nat-xxx) is expected
# For fully air-gapped (=true): no 0.0.0.0/0 route; only "<vpc-cidr> local" + prefix-list rows
```

### Cognito IDP egress (browser-only consideration)

`cognito-idp.<region>.amazonaws.com` has **no VPC Interface Endpoint**. The browser must reach it from outside the VPC, which is normal: end-user browsers on VPN/DC use their own internet via the corporate network. Lambdas inside the VPC do not call Cognito IDP at runtime.

For testing scenarios where the browser runs **inside the VPC** (WorkSpaces, bastion EC2), provide internet egress via a NAT Gateway in a separate public subnet.

### WorkSpaces option (testing)

WorkSpaces runs a Windows desktop inside the VPC. Setup notes:

1. Register an AWS Directory Service (Simple AD) with WorkSpaces in the same VPC.
2. WorkSpaces directory: `EnableInternetAccess=true`. Set **before** launching — toggling it later requires rebuild (~25 min).
3. NAT Gateway in a **separate public subnet** (with `0.0.0.0/0 → IGW`). It cannot egress traffic from the same subnet it lives in. Private subnet route table: `0.0.0.0/0 → NAT GW`.
4. The execute-api endpoint (`PrivateDnsEnabled: true`) must be in the WorkSpaces VPC so the `/api/` URL resolves to the endpoint's private IPs.

Cost: WorkSpaces AutoStop bundle ~$7.25/mo + NAT GW ~$0.045/hr active. Tear down after testing.

### AWS Client VPN option (testing — replicates customer corporate VPN)

```bash
# 1) Create PKI (server + client cert/key, CA)
brew install easy-rsa openvpn      # macOS
easyrsa init-pki && easyrsa build-ca nopass
easyrsa --san=DNS:server build-server-full server.idp-vpn nopass
easyrsa build-client-full client.idp-vpn nopass

# 2) Import server cert to ACM in the same region
SERVER_CERT_ARN=$(aws acm import-certificate \
  --certificate fileb://pki/issued/server.idp-vpn.crt \
  --private-key fileb://pki/private/server.idp-vpn.key \
  --certificate-chain fileb://pki/ca.crt \
  --region <region> --query CertificateArn --output text)

# 3) Create Client VPN endpoint (use 172.16.0.0/22 as client CIDR)
CVPN=$(aws ec2 create-client-vpn-endpoint \
  --client-cidr-block 172.16.0.0/22 \
  --server-certificate-arn $SERVER_CERT_ARN \
  --authentication-options "Type=certificate-authentication,MutualAuthentication={ClientRootCertificateChainArn=$SERVER_CERT_ARN}" \
  --connection-log-options Enabled=false \
  --dns-servers 10.1.0.2 \
  --vpc-id <vpc-id> --security-group-ids <lambda-sg> \
  --region <region> --query ClientVpnEndpointId --output text)

# 4) Associate with Lambda subnet, authorize VPC CIDR
aws ec2 associate-client-vpn-target-network --client-vpn-endpoint-id $CVPN \
  --subnet-id <lambda-subnet-id> --region <region>
aws ec2 authorize-client-vpn-ingress --client-vpn-endpoint-id $CVPN \
  --target-network-cidr 10.1.0.0/16 --authorize-all-groups --region <region>

# 5) Download .ovpn config; append <cert> + <key> blocks for the client cert/key; connect:
sudo openvpn --config idp-vpn.ovpn --daemon --log /tmp/openvpn.log
```

Authorize the VPN association-subnet CIDR (e.g. `10.1.3.0/24`) on the execute-api endpoint SG (and the BYO S3 VPCE endpoint SG when `S3PresignedUrlViaVpcEndpoint=true`) — see SG matrix above.

### SSM port forwarding option (testing)

See [Deployment Guide → SSM tunneling](./deployment.md) — applies unchanged. Required only when not using VPN/WorkSpaces.

---

## What gets automatically configured

When `WebUIHosting=APIGateway` and `ApiGatewayVisibility=PRIVATE`:

- **REST API** → deployed as `PRIVATE` with a resource policy pinning access to the
  `ApiGatewayVpcEndpointId` execute-api endpoint; serves both the UI (S3 proxy) and data ops (`POST /op/{field}`)
- **Cognito callback / logout URLs** → the execute-api `/api/` URL, registered post-deploy via the `WebUIClientOAuthUrls` custom resource; plus `CustomDomainUrl` (with and without trailing slash) when set
- **UI build** → built with Vite `base=/api/` (`VITE_UI_BASE_PATH=/api/`); `VITE_CLOUDFRONT_DOMAIN` set to the execute-api `/api/` URL by default, or `""` when `CustomDomainUrl` is set (the Web UI then uses `window.location.origin` so both URLs work side by side — see [API Gateway Hosting Guide](./apigateway-hosting.md))
- **Web UI bucket policy** → APIGateway branch restricts reads to the `WebUIProxyRole` (API Gateway S3 proxy)
- **App bucket policies** → `aws:sourceVpce` condition restricts access to the BYO S3 VPCE when `S3PresignedUrlViaVpcEndpoint=true`
- **CodeBuild projects** (WebUI build, Docker image builds, SDLC pipeline) → placed in VPC with `LambdaSecurityGroup`; requires NAT or internal artifact repository for dependency resolution
- **Lambda functions (~21)** → placed in `LambdaSubnetIds` with `LambdaSecurityGroup`
- **`S3_ENDPOINT_URL` env** injected on presigner Lambdas (only when `S3PresignedUrlViaVpcEndpoint=true` or BYO endpoint override is set):
  - `UploadResolverFunction` (browser presigned POST)
  - `DiscoveryUploadResolverFunction`
  - `TestSetResolverFunction`
  - `ApiHandlerFunction` (jobs API presigner)
  - All API resolver Lambdas via the api-resolvers nested-stack `S3EndpointUrl` parameter
- **Lambda S3 client** uses `signature_version=s3v4` + `addressing_style=virtual` when `S3_ENDPOINT_URL` is set; falls back to default `path` style when unset (global S3)

---

## Private REST API DNS resolution in cross-VPC and hybrid networks

### The problem

When `ApiGatewayVisibility=PRIVATE`, the IDP stack deploys the REST API (Web UI **and** data operations) as a **private** API Gateway API that is **only reachable via the execute-api VPC Interface Endpoint** (`com.amazonaws.<region>.execute-api`). The frontend (browser) is baked at build time — and the Cognito callback/logout URLs are registered — with the API's **regional** hostname:

```
https://<api-id>.execute-api.<region>.amazonaws.com/api/
```

For the browser to reach the API (and load the SPA the API serves), DNS must resolve this hostname to the **private IP addresses** of the execute-api VPC Interface Endpoint. That endpoint also exposes its own VPCE-specific hostname:

```
<vpce-id>.execute-api.<region>.vpce.amazonaws.com
```

**When does this "just work"?**

If the execute-api VPC endpoint is created **in the same VPC** where the browser's DNS queries are resolved (i.e., the VPC's built-in Route 53 Resolver at `<VPC-CIDR-base>+2`), and `PrivateDnsEnabled: true` is set on that endpoint, then DNS resolution works automatically. `PrivateDnsEnabled: true` makes the regional `execute-api.<region>.amazonaws.com` name resolve to the endpoint's private IPs inside that VPC. This is the case when:

- The user is on a VPN whose DNS server is the VPC's `.2` resolver
- The user is on a WorkSpace or EC2 instance in that VPC
- Lambdas are in the same VPC as the endpoint

This is the happy path assumed by the rest of this runbook — Prereq §2 requires `PrivateDnsEnabled: true` on the execute-api endpoint.

**When does it NOT work?**

`PrivateDnsEnabled` only injects the DNS override into the **VPC that owns the endpoint** (AWS creates a managed Route 53 Private Hosted Zone for `execute-api.<region>.amazonaws.com`, scoped to that VPC). It does NOT propagate across:

- VPC peering connections
- Transit Gateway attachments
- Cross-account VPC associations
- On-premises networks connected via Direct Connect or Site-to-Site VPN (unless DNS is forwarded to the VPC resolver)

This is the **central network account** scenario (Mode B): a networking team manages all VPC endpoints in a shared-services VPC, and the IDP workload VPC connects via Transit Gateway or peering. A browser (on corporate VPN) and the Lambdas (in the workload VPC) cannot resolve `<api-id>.execute-api.<region>.amazonaws.com` because the private DNS override lives only in the endpoint's VPC.

### Why this differs from S3

For S3, the IDP stack solves cross-VPC access by rewriting presigned URLs to use the VPCE-specific DNS name (`bucket.vpce-xxx.s3.<region>.vpce.amazonaws.com`). This works because:

1. The IDP stack controls the presigner Lambda and can inject `S3_ENDPOINT_URL`
2. S3 VPCE DNS names are resolvable from any VPC (they are public DNS names that happen to route to VPCE IPs when resolved from within the VPC)
3. No custom headers are needed — S3 uses the `Host` header that the browser sets naturally from the URL

**The private REST API is harder to redirect at the URL layer** because:

1. The SPA origin is fixed at build time (the Vite `/api/` base and `VITE_CLOUDFRONT_DOMAIN`), and the Cognito OAuth callback/logout URLs are registered to the regional `<api-id>.execute-api.<region>.amazonaws.com` origin. Swapping in a VPCE hostname would break the single-origin model and the Cognito redirect URLs.
2. You *can* invoke a private API via the VPCE's own hostname (`<vpce-id>.execute-api.<region>.vpce.amazonaws.com`), but only if the request also carries `Host: <api-id>.execute-api.<region>.amazonaws.com` (or an `x-apigw-api-id: <api-id>` header) so API Gateway can route to the correct private API. A browser sets `Host` from the URL and cannot override it for SPA asset loads and navigations, so this path is impractical for a baked SPA.

> **Note:** Unlike AWS AppSync (which required an `X-AppSync-Domain`/`Host` header and could not be reached over browser WebSockets), the private REST API needs **no** custom-header workaround on the happy path — the DNS-layer fix below makes the ordinary regional hostname resolve to the endpoint and every request works unchanged. Document status is delivered by REST polling, and chat streaming uses a separate Lambda **Function URL** origin that is direct-to-browser and **not** behind the private API, so streaming is unaffected by this DNS problem.

Therefore, the solution operates at the **DNS layer** — making the standard regional REST API hostname resolve to the correct private IPs regardless of which VPC the client is in.

---

### Solution A: Route 53 Private Hosted Zone (recommended)

This is the AWS-recommended approach for cross-VPC and cross-account private API Gateway access — associate a Route 53 Private Hosted Zone for the API's `execute-api` domain with the VPCs that need to resolve it, pointing at the execute-api Interface VPC Endpoint. It is the direct analogue of the pattern AWS documents for reaching any Interface-VPC-Endpoint-fronted service across VPCs (see [Centralized access to VPC private endpoints](https://docs.aws.amazon.com/whitepapers/latest/building-scalable-secure-multi-vpc-network-infrastructure/centralized-access-to-vpc-private-endpoints.html)).

#### How it works

1. Create an **API-ID-scoped** Route 53 Private Hosted Zone (PHZ) named for the exact regional hostname (`<api-id>.execute-api.<region>.amazonaws.com`)
2. In that zone, create an apex `A`-record **alias** to the execute-api VPC Interface Endpoint (`com.amazonaws.<region>.execute-api`)
3. Associate the PHZ with every VPC that needs to resolve the API hostname (workload VPC, shared-services VPC, etc.)
4. For on-premises/hybrid browsers: set up a Route 53 Resolver Inbound Endpoint and configure the corporate DNS to conditionally forward **only this FQDN** to it

> **Why scope to the API ID instead of the whole `execute-api.<region>.amazonaws.com` domain?** Route 53 resolves the **most specific** matching private hosted zone first. A PHZ named for the exact API hostname overrides DNS for *only this API*; every other private API in the region — yours or another team's — keeps resolving normally. A PHZ created at the regional `execute-api.<region>.amazonaws.com` apex would instead intercept **all** API Gateway DNS in every associated VPC, which can break unrelated applications. The scoped approach below is the least-blast-radius default; see [Important considerations](#important-considerations) for the regional-zone alternative and when it makes sense.

#### When to use

- Central network account manages VPC endpoints in a shared VPC
- Workload VPC connects to shared VPC via Transit Gateway or VPC peering
- On-premises users access the IDP UI via Direct Connect or Site-to-Site VPN
- Multiple VPCs need to reach the same private REST API

#### Prerequisites

- The execute-api VPC Interface Endpoint (`com.amazonaws.<region>.execute-api`) must exist in at least one VPC (the central/shared VPC)
- `PrivateDnsEnabled` should be set to **false** on that endpoint. Enabling it makes AWS create a managed PHZ for the *regional* `execute-api.<region>.amazonaws.com` domain inside the endpoint's VPC, which reintroduces the blanket interception the scoped zone is designed to avoid. The scoped PHZ below replaces its function for this API only. (This differs from the single-VPC happy path in Prereq §2, where `PrivateDnsEnabled: true` is exactly what you want.)
- The VPC endpoint must have a security group allowing inbound HTTPS (443) from the source networks
- The API's resource policy must permit access via this endpoint. When `ApiGatewayVisibility=PRIVATE`, IDP pins the policy to the `ApiGatewayVpcEndpointId` you passed; a second execute-api endpoint in a shared VPC must be the same one, or the policy must be widened to include it.

> **Single hostname, single zone.** Unlike AWS AppSync (which exposed a separate `appsync-realtime-api` WebSocket hostname and therefore needed a second zone), the REST API is a single regional hostname. One scoped PHZ covers the UI and all data operations. Document status is delivered by REST polling over this same hostname; chat streaming uses a separate Lambda **Function URL** origin that is not behind the private API and does not need a zone here.

#### Manual steps (no script)

These steps are performed by the networking team or the person managing Route 53 in the account that owns the VPC endpoint.

**Step 1: Identify the API ID and region**

The IDP stack outputs the application URL (`ApplicationWebURL`), which is the regional `execute-api` URL. Extract the API ID:

```bash
# From the IDP stack outputs:
API_URL=$(aws cloudformation describe-stacks --stack-name IDP-PRIVATE \
  --query 'Stacks[0].Outputs[?OutputKey==`ApplicationWebURL`].OutputValue' \
  --output text --region <region>)
echo $API_URL
# Example: https://abcdef1234.execute-api.us-east-1.amazonaws.com/api/

# Strip scheme + path, then split the hostname:
API_HOST=$(echo $API_URL | sed -E 's#https?://##; s#/.*##')
API_ID=$(echo $API_HOST | cut -d. -f1)
REGION=$(echo $API_HOST | cut -d. -f3)
echo "API_ID=$API_ID  REGION=$REGION"
```

**Step 2: Identify the execute-api VPC Endpoint**

```bash
EXECAPI_VPCE_ID=$(aws ec2 describe-vpc-endpoints \
  --filters "Name=service-name,Values=com.amazonaws.${REGION}.execute-api" \
             "Name=vpc-id,Values=<central-vpc-id>" \
  --query 'VpcEndpoints[0].VpcEndpointId' \
  --output text --region $REGION)
echo $EXECAPI_VPCE_ID
```

**Step 3: Create the API-ID-scoped Private Hosted Zone**

Create a PHZ named for the exact regional hostname. Because the zone name is the full API FQDN, Route 53 overrides DNS for *only this API* — other private APIs in the region are unaffected.

```bash
PHZ_ID=$(aws route53 create-hosted-zone \
  --name "${API_ID}.execute-api.${REGION}.amazonaws.com" \
  --vpc VPCRegion=${REGION},VPCId=<central-vpc-id> \
  --caller-reference "idp-execapi-$(date +%s)" \
  --hosted-zone-config Comment="Private DNS for IDP REST API ${API_ID}",PrivateZone=true \
  --query 'HostedZone.Id' --output text | sed 's|/hostedzone/||')

echo "PHZ_ID=$PHZ_ID"
```

**Step 4: Create an apex alias record in the scoped zone**

The zone gets a single record at its **apex** (the record name equals the zone name) aliased to the execute-api VPC endpoint.

```bash
# Get the VPC endpoint's regional DNS name + its hosted zone ID (the alias target).
# Both values come from the same DnsEntries[0] element so they stay consistent.
VPCE_HZ_ID=$(aws ec2 describe-vpc-endpoints \
  --vpc-endpoint-ids $EXECAPI_VPCE_ID \
  --query 'VpcEndpoints[0].DnsEntries[0].HostedZoneId' \
  --output text --region $REGION)

VPCE_DNS=$(aws ec2 describe-vpc-endpoints \
  --vpc-endpoint-ids $EXECAPI_VPCE_ID \
  --query 'VpcEndpoints[0].DnsEntries[0].DnsName' \
  --output text --region $REGION | sed 's|^\*\.||')
# VPCE_DNS looks like: <vpce-id>.execute-api.<region>.vpce.amazonaws.com

# Apex alias in the scoped zone → execute-api VPC endpoint
# EvaluateTargetHealth is false: a VPC interface endpoint is highly available by
# design and exposes no Route 53-evaluable health signal, so true adds no benefit.
aws route53 change-resource-record-sets --hosted-zone-id $PHZ_ID --change-batch '{
  "Changes": [
    {
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "'${API_ID}'.execute-api.'${REGION}'.amazonaws.com",
        "Type": "A",
        "AliasTarget": {
          "HostedZoneId": "'${VPCE_HZ_ID}'",
          "DNSName": "'${VPCE_DNS}'",
          "EvaluateTargetHealth": false
        }
      }
    }
  ]
}'
```

**Step 5: Associate the PHZ with the workload VPC**

The zone must be associated with every VPC where DNS queries originate.

If the workload VPC is in the **same account**:

```bash
aws route53 associate-vpc-with-hosted-zone \
  --hosted-zone-id $PHZ_ID \
  --vpc VPCRegion=${REGION},VPCId=<workload-vpc-id>
```

If the workload VPC is in a **different account** (cross-account), run the authorize → associate → cleanup sequence:

```bash
# In the account that owns the PHZ (central/networking account):
aws route53 create-vpc-association-authorization \
  --hosted-zone-id $PHZ_ID \
  --vpc VPCRegion=${REGION},VPCId=<workload-vpc-id-in-other-account>

# In the workload account (use that account's credentials/profile):
aws route53 associate-vpc-with-hosted-zone \
  --hosted-zone-id $PHZ_ID \
  --vpc VPCRegion=${REGION},VPCId=<workload-vpc-id-in-other-account>

# Back in the PHZ-owner account (cleanup — recommended):
aws route53 delete-vpc-association-authorization \
  --hosted-zone-id $PHZ_ID \
  --vpc VPCRegion=${REGION},VPCId=<workload-vpc-id-in-other-account>
```

**Step 6 (hybrid/on-prem only): Route 53 Resolver Inbound Endpoint**

If users access the IDP UI from on-premises (via Direct Connect or VPN) and their DNS does not use the VPC's `.2` resolver, you need a Route 53 Resolver Inbound Endpoint:

```bash
# Create resolver inbound endpoint in the VPC associated with the scoped PHZ
RESOLVER_EP=$(aws route53resolver create-resolver-endpoint \
  --creator-request-id "idp-execapi-inbound-$(date +%s)" \
  --name "idp-execapi-inbound" \
  --security-group-ids <sg-allowing-dns-from-onprem> \
  --direction INBOUND \
  --ip-addresses SubnetId=<subnet-1> SubnetId=<subnet-2> \
  --region $REGION \
  --query 'ResolverEndpoint.Id' --output text)

# Get the resolver endpoint IPs
aws route53resolver list-resolver-endpoint-ip-addresses \
  --resolver-endpoint-id $RESOLVER_EP --region $REGION \
  --query 'IpAddresses[].Ip' --output text
```

Then configure your on-premises DNS server (Active Directory, BIND, Unbound, etc.) to **conditionally forward** the **API-specific FQDN** to the resolver endpoint IPs:

- `<api-id>.execute-api.<region>.amazonaws.com`

Scoping the forwarder to this exact name (rather than the regional `execute-api.<region>.amazonaws.com` parent) keeps on-premises resolution of every other API Gateway API on its normal public path. Windows DNS, BIND, and Unbound all support conditional forwarding keyed to a full FQDN.

> **Note**: If your DNS appliance can only forward at a broader suffix, you may forward `execute-api.<region>.amazonaws.com` instead. The blast radius stays contained on the AWS side — the scoped PHZ only answers for this API's hostname, so any other execute-api name forwarded in still resolves via public DNS through the VPC resolver.

> **Note**: If you already have a Route 53 Resolver Inbound Endpoint for other AWS services (e.g., S3, STS), you can reuse it. Just add the API FQDN to your on-premises conditional forwarder configuration.

#### Verification

From a machine using the VPC DNS (or the forwarded DNS path):

```bash
# Should resolve to private IPs (10.x.x.x or 172.x.x.x) of the VPC endpoint ENIs
nslookup ${API_ID}.execute-api.${REGION}.amazonaws.com

# Scoping check — a DIFFERENT private API must still resolve to PUBLIC IPs,
# proving the scoped zone did not capture the whole regional domain:
nslookup some-other-api-id.execute-api.${REGION}.amazonaws.com
# Expect: public API Gateway IPs (not the VPC endpoint ENIs)
```

From the browser (DevTools → Network tab):
- Requests to `<api-id>.execute-api.<region>.amazonaws.com/api/` should succeed (SPA assets load, `POST /op/{field}` data operations return)
- The response should carry the execute-api endpoint id in the `x-amzn-vpce-id` header
- Document status updates should appear as the UI polls, without manual page reload

#### Important considerations

| Consideration | Detail |
|---------------|--------|
| **Scoped zone does not affect other APIs** | Because the PHZ is named for the exact API FQDN, Route 53's most-specific-match means the zone overrides DNS for **only this API**. Other API Gateway APIs in the region (public or private, yours or another team's) keep resolving through normal public DNS. This is the key advantage over a regional-domain PHZ. |
| **One zone per private API** | Each `ApiGatewayVisibility=PRIVATE` API needs its own scoped zone (`<api-id>.execute-api.<region>.amazonaws.com`). Deploying multiple private IDP stacks in the same region simply means another scoped zone per stack — there is no shared zone to maintain and no record-enumeration burden. |
| **Regional-zone alternative** | If you genuinely want a single PHZ to cover **every** private API in a VPC, you can instead create one PHZ at the regional apex `execute-api.<region>.amazonaws.com` and add an alias record per API ID inside it. This is simpler to reason about for a fleet of private APIs, but it **intercepts all API Gateway DNS** in associated VPCs — any private API you do not add a record for will fail to resolve, and any public API in those VPCs breaks. Use it only when every execute-api consumer in those VPCs is private and accounted for. |
| **Endpoint in multiple AZs** | Deploy the execute-api VPC endpoint in at least 2 AZs for high availability. The alias record automatically load-balances across all endpoint ENIs. |
| **PrivateDnsEnabled conflict** | If `PrivateDnsEnabled=true` is set on the execute-api VPC endpoint, AWS creates a managed PHZ for the regional `execute-api.<region>.amazonaws.com` domain in the endpoint's VPC. For VPCs associated with both that managed zone and your scoped zone, the scoped zone wins (most-specific match) — but to avoid confusion and unintended blanket interception, set `PrivateDnsEnabled=false` when using the PHZ approach for cross-VPC access. |
| **No IDP stack changes required** | This is purely a DNS/networking configuration. The IDP stack and frontend code work unchanged — the browser calls the same regional hostname it was built with. |

---

### Summary: which approach to choose

| Scenario | Recommended approach |
|----------|---------------------|
| Single VPC — endpoint in same VPC as browser/Lambdas | No action needed. `PrivateDnsEnabled=true` on the execute-api endpoint handles everything. |
| Cross-VPC (Transit Gateway / peering) — same account | Scoped PHZ associated with every VPC that resolves the API |
| Cross-account — central networking account owns endpoints | Scoped PHZ with cross-account VPC association |
| Hybrid (on-premises browsers via DC/VPN) | Scoped PHZ + Route 53 Resolver Inbound Endpoint + corporate DNS conditional forwarder (scoped to the API FQDN) |

---

## Optional: custom (vanity) domain for the Web UI

By default the UI is reached at the regional execute-api `/api/` URL (the `ApplicationWebURL`
output). You can front it with a vanity hostname (e.g. `https://idp.example.gov`) that resolves
only inside the VPC. This is **optional** and — unlike the former ALB hosting — it uses the
**API Gateway private custom domain** feature. The IDP stack does **not** create the domain,
DNS, or routing; you provision those with the steps below and tell IDP about the hostname via
the `CustomDomainUrl` parameter.

### What changed from ALB hosting

| | v0.5.x (ALB) | v0.6.x (private API Gateway) |
|---|---|---|
| UI served at | root path `/` on an internal ALB | the `/api` **stage path** on the private REST API |
| TLS certificate | on the ALB HTTPS listener (`ALBCertificateArn`) | ACM cert on an API Gateway **private** custom domain name |
| DNS target | alias/CNAME to the internal ALB DNS | Route 53 PHZ **A-alias to the execute-api VPCE DNS name** |
| Extra resources | none | private domain name + **domain name access association** + base path mapping + a domain resource policy |
| OAuth origin | matched root — "just worked" | app under `/api`, OAuth returns to root — see the base-path note below |

The `ALB*` parameters (`ALBVpcId`, `ALBSubnetIds`, `ALBCertificateArn`, `ALBScheme`,
`ALBAllowedCIDRs`) were **removed** in v0.6.

### What `CustomDomainUrl` does (and does not) do

- **Does**: adds `https://idp.example.gov` (with and without a trailing slash) to the Cognito
  App Client callback/logout URLs; adds it to every browser-accessed S3 bucket's CORS origins;
  sets `VITE_CLOUDFRONT_DOMAIN=""` so the SPA's OAuth `redirectSignIn`/`redirectSignOut` use
  `window.location.origin` (see `src/ui/src/aws-exports.js`).
- **Does not**: create the ACM certificate, the API Gateway custom domain name, the base path
  mapping, the domain name access association, or any DNS record.

Value format: **host only** — `https://<host>` with no path or trailing slash (e.g.
`https://idp.example.gov`). Enforced by the parameter's `AllowedPattern`.

### Required AWS resources

1. **ACM certificate** in this region covering the hostname. Private custom domains support
   **RSA-2048** or **ECDSA P-256/P-384** certificates only, and enforce **TLS 1.2**.
2. **API Gateway private custom domain name** — `EndpointConfiguration.Types=[PRIVATE]`,
   routing mode "API mappings only", with the ACM cert and a **resource policy** allowing
   `execute-api:Invoke` only from your execute-api VPC endpoint. (This is a *separate* policy
   from the private API's own resource policy — both must permit the endpoint.)
3. **Base path mapping(s)** from the custom domain to the IDP REST API + the `api` stage (see
   the base-path note below).
4. **Domain name access association** between the private custom domain and your execute-api
   VPC endpoint (the `ApiGatewayVpcEndpointId`). Takes ~15 minutes to become ready.
5. **Route 53 private hosted zone** for the hostname, associated to the VPC, with an
   **A-record (Alias)** to the execute-api VPCE DNS name (add an AAAA alias for dualstack
   endpoints).

> Multi-level base path mappings (e.g. `/team/app`) are not supported for private custom
> domains; single-level base paths (empty or `api`) are fine.

### The `/api` base-path consideration (read before mapping)

The v6 SPA is built with `VITE_UI_BASE_PATH=/api/`, so its assets are referenced under `/api/`
(this matches the `api` stage on the raw execute-api URL). But with a custom domain the OAuth
redirect is the **domain root** — `window.location.origin + '/'` → `https://idp.example.gov/` —
because `window.location.origin` never includes a path. That interaction drives the base path
mapping:

- A single mapping with base path **`api`** (app at `https://idp.example.gov/api/`) loads the
  SPA and its assets, **but OAuth returns to `https://idp.example.gov/` (root), which that
  mapping does not serve → `Forbidden`.**
- A single **empty** base path mapping serves the shell at the root (good for the OAuth return)
  **but the SPA's `/api/assets/...` requests then fail to resolve.**

To satisfy both, create **two base path mappings to the same `api` stage**:

| Base path | Serves |
|---|---|
| *(empty)* | the app shell at `https://idp.example.gov/` — the OAuth redirect target |
| `api` | the SPA assets at `https://idp.example.gov/api/...` **and the data transport** `POST /api/op/{field}` |

> The `api` mapping is **load-bearing for data operations**, not only for assets: the bundle's
> API base is `https://idp.example.gov/api` (see below). A deployment that maps only the root
> will load the SPA and then 404 every `/op/{field}` call. Verify both mappings with
> `aws apigateway get-base-path-mappings --domain-name <host>` — you should see one entry with
> `basePath: "(none)"` and one with `basePath: "api"`, both on stage `api`.

`VITE_API_BASE_URL` is built as **`https://idp.example.gov/api`** whenever `CustomDomainUrl` is
set under APIGateway hosting, so backend data operations (`POST /op/{field}`) travel to the same
origin the browser is on and resolve through the `api` base-path mapping. This is required, not
cosmetic: the raw `<api-id>.execute-api.<region>.amazonaws.com` hostname usually does **not**
resolve for a client outside the VPC that owns the execute-api endpoint, so a bundle pinned to
it loads the SPA shell from the vanity domain and then fails every data call — Test Studio's
`getTestSets`, document lists, everything — while looking like a backend outage.

Consequence: once `CustomDomainUrl` is set, **access the app via the vanity domain only**. The
raw execute-api URL will serve the shell but its data calls now target the vanity host (which
is the intended single-origin model — the same conclusion the troubleshooting table below
already reaches for OAuth).

Under **CloudFront** hosting the API base stays the execute-api URL: the distribution has one S3
origin and no `/api` behaviour, so a custom domain in front of it cannot route to the API.

> **Validate in a non-production deployment first.** Confirm the full login round-trip *and*
> that the SPA loads and routes correctly through the vanity domain before requesting any
> production DNS change. If a clean single-mapping experience is required, the alternative is
> CloudFront hosting (which builds the UI with `base=/`) — but CloudFront is public-facing and
> is generally not used in strict private-network deployments.

### Implementation steps

```bash
REGION=us-east-1
HOST=idp.example.gov
API_ID=<your-rest-api-id>            # from ApplicationWebURL
EXECAPI_VPCE_ID=<execute-api-vpce>   # the ApiGatewayVpcEndpointId

# 1) ACM certificate for the hostname (request + validate, or import). Capture the ARN:
CERT_ARN=<acm-cert-arn>

# 2) Resource policy allowing only your execute-api VPCE (save as domain-policy.json):
cat > domain-policy.json <<EOF
{ "Version":"2012-10-17","Statement":[
  {"Effect":"Allow","Principal":"*","Action":"execute-api:Invoke","Resource":["execute-api:/*"]},
  {"Effect":"Deny","Principal":"*","Action":"execute-api:Invoke","Resource":["execute-api:/*"],
   "Condition":{"StringNotEquals":{"aws:SourceVpce":"$EXECAPI_VPCE_ID"}}}
]}
EOF

# 3) Create the PRIVATE custom domain name:
DN_ID=$(aws apigateway create-domain-name \
  --domain-name "$HOST" \
  --certificate-arn "$CERT_ARN" \
  --security-policy TLS_1_2 \
  --endpoint-configuration '{"types":["PRIVATE"]}' \
  --policy file://domain-policy.json \
  --region $REGION --query domainNameId --output text)

# 4) Base path mappings → the "api" stage (empty + api, per the base-path note):
aws apigateway create-base-path-mapping --domain-name "$HOST" --domain-name-id $DN_ID \
  --rest-api-id $API_ID --stage api --region $REGION                      # empty base path
aws apigateway create-base-path-mapping --domain-name "$HOST" --domain-name-id $DN_ID \
  --rest-api-id $API_ID --stage api --base-path api --region $REGION      # /api

# 5) Associate the domain with the execute-api VPC endpoint:
aws apigateway create-domain-name-access-association \
  --domain-name-arn arn:aws:apigateway:$REGION:<account-id>:/domainnames/$HOST+$DN_ID \
  --access-association-source $EXECAPI_VPCE_ID \
  --access-association-source-type VPCE --region $REGION

# 6) Route 53 private hosted zone + A-alias to the execute-api VPCE DNS name
#    (same alias mechanics as "Solution A" above — reuse VPCE_DNS / VPCE_HZ_ID there).
```

> **GovCloud / non-standard partitions:** the `--domain-name-arn` above uses the
> `arn:aws:` partition. In AWS GovCloud (US) use `arn:aws-us-gov:` (and in China
> `arn:aws-cn:`); substitute the partition that matches your deployment account.

### DNS target for your DNS team

The vanity hostname must ultimately resolve to the **execute-api VPC endpoint's regional DNS
name**:

```
vpce-XXXXXXXXXXXX.execute-api.<region>.vpce.amazonaws.com
```

Recommended: a **Route 53 private hosted zone** for the hostname with an **A-record, Alias=ON**
targeting that VPCE DNS name (and an AAAA alias for dualstack endpoints). If the record must
live in enterprise DNS instead, a **CNAME** to the same VPCE DNS name works — but because the
execute-api endpoint has `PrivateDnsEnabled: true`, a plain CNAME can fail to resolve inside the
VPC that hosts the private API; the Route 53 private-hosted-zone A-alias avoids that interaction.

### After the domain is live

1. Set `CustomDomainUrl=https://idp.example.gov` on the stack (triggers a UI rebuild that
   switches the OAuth origin to the domain).
2. Confirm the Cognito App Client callback/logout lists include the domain (added automatically
   when the parameter is set).
3. Access the app **only via the vanity domain** — not the raw execute-api `/api/` URL. With a
   custom domain configured, that raw URL will not complete OAuth (its origin has no `/api/`).
4. **Okta/SAML is unchanged**: the IdP still posts its assertion to the Cognito hosted-UI
   `/saml2/idpresponse` endpoint; only the app-facing origin changes.

---

## Troubleshooting

| Issue | Resolution |
|-------|-----------|
| `ModuleNotFoundError: No module named 'boto3'` | Use venv Python. `make setup && source .venv/bin/activate` then retry. |
| `npm error engine Unsupported engine` | Node.js 22.12+ required. `brew install node@22 && export PATH="/opt/homebrew/opt/node@22/bin:$PATH"` |
| Stack fails with `conflicting DNS domain` | A VPC endpoint already exists for that service. Re-run `deploy-vpc-endpoints.py` — it auto-detects and skips existing ones. |
| **Browser upload fails: `ERR_NAME_NOT_RESOLVED` on `bucket.vpce-...vpce.amazonaws.com`** | (a) Browser is not on the VPN — VPCE host is VPC-DNS-only. (b) Mode B: `S3VpcEndpointDnsNameOverride` was set without the AWS-assigned random suffix. Re-deploy with the full DNS from `aws ec2 describe-vpc-endpoints ... DnsEntries[0].DnsName` minus the `*.` prefix. |
| **Browser upload fails: `Failed to fetch` / connect timeout** | (a) S3 VPCE endpoint SG missing ingress for VPN NAT-subnet CIDR (see SG matrix). (b) VPN association subnet differs from S3 VPCE subnets — return path is fine if SG has the NAT CIDR. |
| **Backend processing fails with `ConnectTimeoutError` on an AWS service endpoint** | Shared endpoint SG (`scripts/vpc-endpoints.yaml`) does not allow ingress from this stack's Lambda SG. Re-run `deploy-vpc-endpoints.py`, or add ingress 443 from the Lambda SG manually. Common when reusing a shared endpoint SG across stacks. |
| **`ConfigurationCopyFunction` times out (5 min) during stack create** | Shared service-endpoint SG (`scripts/vpc-endpoints.yaml`) missing ingress from the Lambda SG, so the function can't reach S3/DynamoDB. Confirm `LambdaSecurityGroupId` is passed and re-run `deploy-vpc-endpoints.py`. Manual unblock: `aws ec2 authorize-security-group-ingress --group-id <endpoint-sg> --protocol tcp --port 443 --source-group <lambda-sg>`. |
| **`DashboardMerger` custom resource times out** | Missing `monitoring` (CloudWatch) Interface VPCE. Re-run `deploy-vpc-endpoints.py` — script now includes it. |
| **OCR Lambda times out / Textract calls hang** | Missing `textract` VPC endpoint. Re-run `deploy-vpc-endpoints.py`. |
| **BDA Lambda fails STS `AssumeRole`** | Missing `sts` VPC endpoint. Re-run `deploy-vpc-endpoints.py`. |
| **403 Forbidden loading the UI** | WAF is enabled and your client IP is not in `WAFAllowedIPv4Ranges`, or (PRIVATE mode) the request did not arrive via the `ApiGatewayVpcEndpointId` execute-api endpoint the API resource policy pins to. |
| **UI unreachable in PRIVATE mode** | The execute-api endpoint's private DNS is off, or its SG blocks your client. Confirm `PrivateDnsEnabled: true` and 443 ingress from your source network. |
| **UI assets 404** | The UI was not built with `VITE_UI_BASE_PATH=/api/`. CodeBuild sets this automatically when `WebUIHosting=APIGateway`; asset URLs in `index.html` must start with `/api/`. |
| **Login hangs on `cognito-idp.amazonaws.com` (browser inside VPC)** | No VPCE for Cognito IDP. Browser needs internet egress: NAT GW in a public subnet + private route `0.0.0.0/0 → NAT GW`. End-user browsers on VPN don't need this. |
| **CodeBuild fails: timeout on `pip install` / `npm ci`** | CodeBuild is in the VPC but has no route to package registries. Add a NAT Gateway, or configure an internal artifact repository and set registry environment variables. See [Dependency Mirroring](./dependency-mirroring.md). |
| **CodeBuild fails: `AccessDenied: kms:Decrypt`** | Artifact bucket is KMS-encrypted but `ArtifactsBucketKmsKeyArn` was not passed. Redeploy with the key ARN. |
| **`UpdateDefaultConfig` custom resource fails: `NoSuchKey`** | Same as above — `ConfigurationCopyFunction` silently skipped due to missing `kms:Decrypt`. Pass `ArtifactsBucketKmsKeyArn`. |
| **Mode B: change-set fails with `S3VpcEndpointDnsNameOverride is required when S3VpcEndpointIdOverride is set`** | Working as intended. Both override params must be supplied together. Set both, or clear both to use Mode A. |
| **Browser UI/API fails: `ERR_NAME_NOT_RESOLVED` on `<api-id>.execute-api.<region>.amazonaws.com`** | Browser cannot resolve the regional REST API hostname. In cross-VPC/hybrid deployments, the execute-api VPC endpoint's `PrivateDnsEnabled` only works within the VPC that owns the endpoint. See "Private REST API DNS resolution" section above — create the API-ID-scoped Route 53 PHZ (`<api-id>.execute-api.<region>.amazonaws.com`), add an apex alias to the execute-api endpoint, and associate it with the VPC where DNS queries originate. |
| **UI loads but data operations (`POST /op/{field}`) fail / time out** | (a) The execute-api endpoint SG is missing ingress 443 from the browser's source network (see SG matrix). (b) The API resource policy pins access to a different `ApiGatewayVpcEndpointId` than the endpoint the request arrived through. Confirm the request carries the pinned endpoint id in `x-amzn-vpce-id`. |
| **`nslookup` resolves the API to public IPs (not VPC endpoint IPs)** | The scoped PHZ is not associated with the VPC where the DNS query runs, or `PrivateDnsEnabled` interference. Verify the association: `aws route53 get-hosted-zone --id <phz-id>` → check the `VPCs` list. |
| **Custom domain: `Forbidden` after login (redirect lands on the domain root)** | The custom domain has only a base-path=`api` mapping, so the OAuth return to `https://<host>/` (root) isn't served. Add the **empty** base path mapping to the `api` stage as well (see "The `/api` base-path consideration"). |
| **Custom domain: shell loads at root but assets 404 under `/api/`** | The **`api`** base path mapping is missing. Add it (in addition to the empty mapping) so `https://<host>/api/...` resolves the SPA assets. |
| **Custom domain: OAuth returns to root but raw execute-api `/api/` URL now fails** | Expected once `CustomDomainUrl` is set — the SPA uses `window.location.origin` (root), which the raw execute-api host has no `/api/` for. Access the app via the vanity domain only. |
| **Custom domain doesn't resolve inside the VPC** | A plain CNAME plus `PrivateDnsEnabled: true` on the execute-api endpoint can fail to resolve within the endpoint's VPC. Use a Route 53 **private hosted zone** with an **A-alias** to the VPCE DNS name instead. |
| **Test Studio: `POST .../op/getTestSets 504` (empty body), other pages fine** | A resolver's own S3 calls were aimed at the S3 **interface VPC endpoint** hostname from a Lambda that is not VPC-attached, so they hang on unroutable private addresses until API Gateway abandons the integration at 29s. Fixed in the accelerator by splitting the presign client (keeps `S3_ENDPOINT_URL`) from the data-plane client (never uses it) in `test_set_resolver` and `upload_resolver`. If you see it on an older build, confirm with `aws lambda get-function-configuration --function-name <stack>-…TestSetResolver… --query '{s3:Environment.Variables.S3_ENDPOINT_URL, vpc:VpcConfig.SubnetIds}'` — an endpoint set with no subnets is the signature — and check the resolver log group for `Task timed out` with no `Returning N test sets` line. Since the same release the dispatcher answers this class of failure with a 504 that *names* the field instead of a bodiless one. |
| **A data operation returns 504 `{"errorType":"Timeout"}`** | The named field took longer than the 29s REST API integration ceiling. That ceiling is not raisable without an account quota increase, so the operation needs to become a start/poll pair rather than a longer timeout. Check the named resolver's log group for what it was waiting on. |

---

## Verification checklist (post-deploy)

- [ ] Stack status `CREATE_COMPLETE`
- [ ] `ApplicationWebURL` output is the execute-api `/api/` URL and loads the UI from inside the VPC
- [ ] (Mode B only) `S3_ENDPOINT_URL` env on `UploadResolverFunction` is `https://bucket.vpce-<id>-<random>.s3.<region>.vpce.amazonaws.com` — it is consumed **only** for presigned URLs handed to the browser; the resolvers' own S3 calls deliberately use the regular S3 endpoint (see the 504 row above)
- [ ] (Custom domain only) the built bundle's API base is the vanity domain: `grep -o 'https://[^"]*/api' index-*.js` in `WebUIBucket` shows `https://<your-host>/api`, not the execute-api host
- [ ] Lambda subnet route table has no `0.0.0.0/0` route (fully air-gapped) — or the expected NAT route for Mode A uploads
- [ ] Browser DevTools → request to the `/api/` origin succeeds (execute-api endpoint id in `x-amzn-vpce-id`)
- [ ] Login succeeds and live document status updates appear
- [ ] Document upload + processing pipeline completes end-to-end

---

## References

- API Gateway hosting guide — [API Gateway Hosting](./apigateway-hosting.md) (private REST API, WebUI S3 proxy, execute-api endpoint)
- API resolvers stack template — `nested/api-resolvers/template.yaml` (private REST API, WebUI S3 proxy integration, execute-api endpoint wiring)
- VPC endpoints template — `scripts/vpc-endpoints.yaml` (17 services + 2 gateways)
- Test VPC template — `scripts/sdlc/apigw-hosting-test-vpc.yaml` (NAT egress + execute-api endpoint)
- Lambda S3 client pattern — `nested/api-resolvers/src/lambda/upload_resolver/index.py`, `src/lambda/api_handler/index.py`
- AWS docs: [Create a private REST API in API Gateway](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-private-apis.html) (private API, execute-api VPC endpoint, resource policy)
- AWS docs: [Custom domain names for private APIs](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-private-custom-domains.html) and the [private custom domain tutorial](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-private-custom-domains-tutorial.html) (private domain name, domain name access association, base path mapping, Route 53 A-alias to the VPCE)
- AWS docs: [Centralized access to VPC private endpoints](https://docs.aws.amazon.com/whitepapers/latest/building-scalable-secure-multi-vpc-network-infrastructure/centralized-access-to-vpc-private-endpoints.html) (Route 53 PHZ pattern for cross-VPC/cross-account/hybrid access to Interface VPC Endpoints)
