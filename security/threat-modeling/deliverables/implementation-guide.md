# Security Controls Implementation Guide

## Document Information

| Field | Value |
|-------|-------|
| **Document Version** | 3.0 |
| **Last Updated** | 2026-07-28 |
| **Applies to release** | v0.6.3 |
| **Classification** | Internal |

## 1. Overview

This guide details the security controls implemented in the GenAI IDP Accelerator to mitigate the 83 identified threats. Controls are organized by security domain and mapped to the specific threats they address.

## 2. Authentication & Identity (AUTH)

### 2.1 Amazon Cognito User Pool

**Threats mitigated**: AUTH.T01, AUTH.T02, AUTH.T04, AUTH.T05

| Control | Implementation | Configuration |
|---------|---------------|---------------|
| Self-signup disabled | `AllowAdminCreateUserOnly: true` | CloudFormation |
| Strong password policy | Minimum 8 chars, mixed case, numbers, symbols | Cognito config |
| Token lifetimes | Access: 1 hour, Refresh: configurable | Cognito config |
| Advanced security | Compromised credential detection, adaptive auth | Optional |
| Admin-only group management | IAM policies on `cognito-idp:Admin*` operations | IAM policies |

### 2.2 RBAC (4-Tier Role System)

**Threats mitigated**: AUTH.T01, AUTH.T03, PM.T06, PM.T07, KB.T01, RPT.T05

| Role | Group Name | Allowed Operations |
|------|-----------|-------------------|
| **Admin** | `{StackName}-Admin` | All operations including user management, KB management, BDA project config |
| **Author** | `{StackName}-Author` | Config CRUD, document upload, processing, agent access, test studio |
| **Reviewer** | `{StackName}-Reviewer` | Document review, HITL tasks, view results |
| **Viewer** | `{StackName}-Viewer` | Read-only access to results and dashboards |

**Enforcement layers** (v0.6 — the authorization boundary is the resolver Lambda):
1. **Resolver Lambda authorization**: every resolver checks `cognito:groups` from the
   dispatcher-normalized identity and raises `PermissionError` (→ HTTP 403). The API
   Gateway Cognito authorizer **only authenticates** — it performs no group evaluation.
2. **Config-version scope**: scope-aware resolvers additionally check the caller's
   `allowedConfigVersions` (from the UsersTable) — denials return an in-band
   `Unauthorized` body with HTTP 200.
3. **Object-level authorization**: owner-scoped keys / `ownerSub`-vs-caller checks on
   user-owned resources (chat sessions, agent jobs).
4. **UI authorization**: React components conditionally rendered based on role
   (**presentation only — never a security control**; the API is directly callable).

> `@aws_cognito_user_pools` directives in `schema.graphql` are **documentation and
> defense-in-depth intent only** — no gateway enforces them (AUTH.T08).

### 2.3 JWT Validation

**Threats mitigated**: AUTH.T02, AUTH.T03, AUTH.T10, AUTH.T11

- API Gateway's Cognito User Pools authorizer validates the JWT signature against Cognito JWKS
- Token expiration enforced by the authorizer (401 on expired/invalid)
- Group claims forwarded to the dispatcher and consumed by resolvers for authorization
- HTTPS-only communication (TLS 1.2+); no cleartext HTTP
- **Known gap**: a global sign-out revokes refresh tokens but a still-valid access/ID
  token remains accepted until `exp` (**GAP-SEC-LOGOUT**, WARN — AUTH.T10)

## 3. API Security (UI, SDK)

### 3.1 UI REST API (`POST /op/{field}`)

**Threats mitigated**: UI.T03, AUTH.T03, AUTH.T08, AUTH.T12, CHAT.T02

| Control | Implementation |
|---------|---------------|
| **Authentication** | API Gateway Cognito User Pools authorizer (authenticate only) |
| **Authorization** | Per-operation `cognito:groups` checks **inside each resolver Lambda** |
| **Input-shape validation** | Dispatcher validates `arguments` against a build-time spec generated from `schema.graphql`; rejects unknown/missing/wrong-typed/out-of-enum args with HTTP 400 |
| **Network posture** | Optional `ApiGatewayVisibility=PRIVATE` + VPCe resource policy; optional WAFv2 default-Block IP allow-list on the stage |
| **Rate limiting** | API Gateway stage throttling + Lambda reserved concurrency + CloudWatch alarms |
| **Security headers** | `nosniff`, HSTS, `X-Frame-Options: DENY`, `Referrer-Policy` on `/op` responses and gateway 4xx/5xx |
| **Automated testing** | `make api-test-static` (drift/missing-check scan) + `make api-test` (live op×role, scope, IDOR, token-lifecycle, input-validation, TLS suites) |

> **No GraphQL engine is in the request path.** Query-depth/complexity limits and
> introspection controls are **not applicable** — do not cite them as controls.

### 3.1a Chat Streaming Function URL

**Threats mitigated**: CHAT.T03 (authentication only), CHAT.T06

| Control | Implementation |
|---------|---------------|
| **Authentication** | `AuthType=AWS_IAM`; browser signs with SigV4 using Cognito Identity Pool credentials; `lambda:InvokeFunctionUrl` granted to `CognitoAuthorizedRole` |
| **CORS** | `AllowCredentials: false`, SigV4 in headers, no cookies |
| **Group authorization** | **NOT ENFORCED** — the Identity Pool role is shared by all four groups (open gap, CHAT.T03) |
| **Session ownership** | **NOT ENFORCED** on this transport (open gap, CHAT.T03) |
| **Rate limiting** | Lambda concurrency only — **not** covered by API Gateway throttling or the WAF WebACL |
| **Automated testing** | **None** — `make api-test` drives `POST /op/{field}` only |

### 3.2 CloudFront Distribution

**Threats mitigated**: UI.T01, UI.T04

| Control | Implementation |
|---------|---------------|
| **Origin Access Control** | OAC for S3 origin (replaces OAI) |
| **HTTPS enforcement** | Redirect HTTP to HTTPS, TLS 1.2 minimum |
| **Response headers** | CSP (see caveat), X-Frame-Options, X-Content-Type-Options, HSTS, Referrer-Policy |
| **S3 bucket policy** | Only CloudFront OAC can read UI bucket |

> **CSP caveat (UI.T01/UI.T07).** The shipped CSP retains `'unsafe-inline'` and
> `'unsafe-eval'` in `script-src` (pending Monaco editor work), so it does **not**
> block injected inline script — treat React escaping as the control of record for
> XSS. The blanket `https:` script source is gone as of v0.6.x; `script-src` is now
> `'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net/npm/`, the last entry
> being the CDN `@monaco-editor/react` loads Monaco from at runtime. The same
> policy is emitted in `WebUIHosting=APIGateway` mode (§3.2a), where it is a static
> header on the two SPA methods rather than a CloudFront policy.

### 3.2a API Gateway Web UI Hosting (`WebUIHosting=APIGateway`)

**Threats mitigated**: UI.T04, UI.T07

| Control | Implementation |
|---------|---------------|
| **Origin access** | S3-proxy integration assumes a dedicated `WebUIProxyRole` scoped to the Web UI bucket |
| **Routes** | `GET /` → `index.html`, `GET /{proxy+}` → asset key; both `AuthorizationType: NONE` (SPA shell/assets are not secrets) |
| **Network posture** | PRIVATE endpoint policy and WAF WebACL still apply to these routes |
| **Security headers** | `Content-Security-Policy`, `nosniff`, HSTS, `X-Frame-Options: DENY`, `Referrer-Policy` set per-method. The CSP mirrors the CloudFront policy from a single `WebUiSecurity` mapping, with `frame-ancestors 'none'` to match the `DENY` above (see caveat in §3.2) |
| **Missing keys** | S3 4xx mapped to 404; HashRouter means deep links need no server-side rewrite |

### 3.3 Presigned URLs

**Threats mitigated**: UI.T02

| Control | Implementation |
|---------|---------------|
| **Short expiration** | Short expiration on upload URLs |
| **Minting authorization** | `uploadDocument` requires the Admin/Author group (server-side) |
| **Read allow-list** | `getFilePresignedUrl`/`getFileContents` restrict the target to this stack's buckets (`_validate_bucket`) |

> **Read-scoping gap (UI.T06).** Presigned **read** URLs are bucket-scoped but
> **not key-scoped**, and both operations are callable by *any authenticated
> user* — so they are not a valid boundary for deployments relying on
> `allowedConfigVersions` to partition users. The allow-list also fails **open**
> if the bucket env vars are unset. Open item.

## 4. Data Protection

### 4.1 Encryption at Rest

**Threats mitigated**: CHAT.T04, KB.T03, RPT.T01

| Resource | Encryption |
|----------|-----------|
| S3 buckets (all) | SSE-S3 (default), SSE-KMS (optional) |
| DynamoDB tables (all) | AWS-managed encryption |
| OpenSearch Serverless | Encryption at rest (AWS-managed) |
| CloudWatch Logs | CloudWatch default encryption |

### 4.2 Encryption in Transit

| Resource | Protocol |
|----------|----------|
| All API calls | HTTPS / TLS 1.2+ |
| Chat streaming | HTTPS SSE (`text/event-stream`) over the Lambda Function URL |
| AWS service calls | TLS via AWS SDK |
| CloudFront | HTTPS only |

### 4.3 Data Isolation

**Threats mitigated**: AGT.T05, CHAT.T02, AUTH.T06

| Mechanism | Implementation |
|-----------|---------------|
| **Single-tenant** | One CloudFormation stack per environment |
| **User-scoped queries** | `ChatSessionsTable` is keyed `PK=userId`; `ChatMessagesTable` is keyed `PK=sessionId` and relies on **explicit resolver ownership checks** (`_verify_session_ownership` / `ownerSub`), not the key alone |
| **Session isolation** | UUID-based conversation session IDs |
| **Stack isolation** | Separate Cognito pools, S3 buckets, DynamoDB tables per stack |

## 5. AI/ML Security

### 5.1 Prompt Injection Defense

**Threats mitigated**: PM.T01, CHAT.T01, KB.T02, RPT.T05

| Layer | Control |
|-------|---------|
| **Prompt engineering** | System prompts with clear role boundaries, input/output tags separating user content from instructions |
| **Bedrock Guardrails** | Optional content filtering, topic denial policies, PII detection |
| **Output validation** | JSON Schema validation of all model outputs |
| **Context isolation** | RAG/KB context marked as reference data, not instructions |
| **Assessment step** | Verification layer checking extraction quality |

### 5.2 Model Output Validation

**Threats mitigated**: PM.T03, BDA.T02, HOOK.T03

| Validation | Implementation |
|-----------|---------------|
| **Schema validation** | JSON Schema enforcement on extraction outputs |
| **Confidence thresholds** | Configurable minimum confidence for classification |
| **Type checking** | Field type validation (dates, numbers, strings) |
| **Evaluation framework** | Ground truth comparison for accuracy monitoring |

### 5.3 Agent Security

**Threats mitigated**: AGT.T01, AGT.T02, AGT.T03

| Control | Implementation |
|---------|---------------|
| **Athena read-only** | IAM role with SELECT-only permissions on Glue catalog |
| **AgentCore sandbox** | AWS-managed isolation (no network, no credentials, no persistent storage) |
| **Tool-level auth** | Each agent tool validates caller permissions |
| **Audit logging** | All tool invocations logged with parameters and results |

## 6. Extensibility Security

### 6.1 Lambda Hooks

**Threats mitigated**: HOOK.T01, HOOK.T02, HOOK.T03, HOOK.T04, HOOK.T05

| Control | Implementation |
|---------|---------------|
| **Invocation-only** | Platform IAM role has only `lambda:InvokeFunction` on hook ARN |
| **Separate IAM** | Hook Lambdas use customer-managed IAM roles (not platform roles) |
| **Output validation** | Platform validates hook return value schema |
| **Timeout handling** | Step Functions state timeout on hook invocation |
| **Error handling** | Catch states with DLQ for hook failures |

**Customer recommendations**:
- Use VPC with restrictive egress for post-processing hooks
- Apply least-privilege IAM to hook execution roles
- Implement input validation in hook code
- Enable CloudWatch logging on hook Lambdas

### 6.2 MCP Integration

**Threats mitigated**: MCP.T01, MCP.T02, MCP.T03, MCP.T04, MCP.T05, MCP.T06

| Control | Implementation |
|---------|---------------|
| **IaC-managed tools** | MCP tool definitions in CloudFormation (not runtime-configurable) |
| **Separate IAM roles** | Each MCP Lambda has dedicated execution role |
| **Response sanitization** | Tool output validated and size-limited |
| **Parameter validation** | Input schemas enforced per tool |
| **Authentication** | Cognito auth required for external MCP clients |
| **Gateway monitoring** | CloudWatch alarms on AgentCore Gateway status |

## 7. Infrastructure Security

### 7.1 IAM Least Privilege

**Threats mitigated**: Multiple (all components)

| Principle | Implementation |
|-----------|---------------|
| **Per-function roles** | Each Lambda function has its own execution role |
| **Resource-scoped policies** | IAM policies reference specific resource ARNs |
| **No wildcards** | Avoid `*` in resource specifications where possible |
| **Service-linked roles** | Use AWS-managed roles for Bedrock, Textract, BDA access |

### 7.2 S3 Bucket Security

**Threats mitigated**: PM.T04, BDA.T05, RPT.T01, RPT.T04

| Control | Implementation |
|---------|---------------|
| **Block public access** | `BlockPublicAcls`, `BlockPublicPolicy`, `IgnorePublicAcls`, `RestrictPublicBuckets` all enabled |
| **Bucket policies** | Restrict access to specific IAM roles and CloudFront OAC |
| **Versioning** | Enabled on reporting and configuration buckets |
| **Lifecycle policies** | Automatic cleanup of temporary files and query results |
| **Server-side encryption** | SSE-S3 default, SSE-KMS optional |

### 7.3 DynamoDB Security

**Threats mitigated**: CHAT.T04, AGT.T04

| Control | Implementation |
|---------|---------------|
| **Encryption** | AWS-managed encryption at rest |
| **Per-table IAM** | Lambda roles scoped to specific tables |
| **Point-in-time recovery** | Optional PITR for critical tables |
| **TTL** | Conversation records with configurable TTL |

### 7.4 Monitoring & Alerting

**Threats mitigated**: Multiple (detection and response)

| Control | Implementation |
|---------|---------------|
| **CloudWatch Alarms** | 60+ alarms on Lambda errors, SQS depth, Step Functions failures |
| **CloudWatch Logs** | All Lambda functions log to CloudWatch |
| **CloudTrail** | API-level audit trail for AWS service calls |
| **Custom metrics** | Processing success rates, latency, costs |
| **Dashboard** | CloudWatch dashboard with operational metrics |

## 8. Network Security

### 8.1 Default Configuration

| Control | Status |
|---------|--------|
| **Minimal public Lambda exposure** | Lambda functions are invoked via API Gateway, SQS, EventBridge, or Step Functions. **One exception**: `ChatStreamProcessorFunction` has a public Function URL for SSE chat streaming — gated by `AuthType=AWS_IAM` (SigV4), but **not** behind API Gateway, the WAF, or a VPC endpoint. See CHAT.T03. |
| **HTTPS everywhere** | All communication over TLS 1.2+ |
| **CloudFront as WAF endpoint** | Optional WAF integration for additional protection |

### 8.2 Optional VPC Configuration

For deployments requiring network-level isolation:

| Control | Implementation |
|---------|---------------|
| **VPC Lambda** | Lambda functions deployed in customer VPC |
| **Private subnets** | Processing Lambdas in private subnets |
| **VPC endpoints** | PrivateLink endpoints for AWS service access |
| **NAT gateway** | Controlled internet egress through NAT |
| **Security groups** | Fine-grained network access control |

## 9. Compliance Mapping

### AWS Well-Architected Security Pillar

| Principle | Controls |
|-----------|----------|
| **Identity and access management** | Cognito, RBAC, IAM least-privilege |
| **Detection** | CloudWatch alarms, CloudTrail, custom metrics |
| **Infrastructure protection** | VPC (optional), security groups, TLS |
| **Data protection** | Encryption at rest/in transit, S3 bucket policies |
| **Incident response** | CloudWatch alarms, DLQ monitoring, operational dashboards |

## 10. Implementation Checklist

### Pre-Deployment

- [ ] Review and customize RBAC role permissions for your organization
- [ ] Configure Cognito password policy and advanced security settings
- [ ] Plan S3 encryption strategy (SSE-S3 vs SSE-KMS)
- [ ] Review Lambda hook security requirements
- [ ] Plan VPC configuration if network isolation is required

### Post-Deployment

- [ ] Create Cognito users with appropriate group assignments
- [ ] Run `make api-test-static` and `make api-test` — verify every operation's resolver-side group/scope check passes and no new WARN gaps appeared
- [ ] Confirm any newly added API operation has an entry in `scripts/api_rbac_expectations.yaml` (the static scan fails on drift)
- [ ] Test RBAC permissions across all four roles
- [ ] Configure CloudWatch alarm notifications
- [ ] Review CloudTrail logging coverage
- [ ] Document Lambda hook deployment procedures
- [ ] Establish evaluation baseline for accuracy monitoring

### Ongoing Operations

- [ ] Periodic API authorization audit (`make api-test` against a live stack; review the op×role matrix report)
- [ ] Review the chat streaming Function URL path separately — it is **outside** the automated harness (CHAT.T03)
- [ ] Review CloudWatch alarm history for security events
- [ ] Monitor Athena query patterns for anomalies
- [ ] Review and rotate SDK/CLI credentials
- [ ] Update Bedrock Guardrails policies as needed
- [ ] Maintain evaluation ground truth data integrity
