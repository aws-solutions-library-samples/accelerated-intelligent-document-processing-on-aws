# Threat Model — Executive Summary

## Document Information

| Field | Value |
|-------|-------|
| **Document Version** | 3.4 |
| **Last Updated** | 2026-11-09 |
| **Applies to release** | v0.6.9 |
| **Classification** | Internal |
| **System** | GenAI Intelligent Document Processing (IDP) Accelerator |

## 1. Purpose

This document provides an executive-level summary of the threat model for the GenAI IDP Accelerator, a serverless intelligent document processing solution deployed on AWS. The threat model identifies security risks across the system's architecture, features, and integrations, and documents the controls in place to mitigate them.

## 2. System Summary

The GenAI IDP Accelerator automates document processing using generative AI. It processes documents through a configurable pipeline (OCR → Classification → Extraction → Assessment → Validation → Evaluation) with two processing modes:

- **Pipeline Mode**: Amazon Textract + Amazon Bedrock foundation models
- **BDA Mode**: Amazon Bedrock Data Automation (integrated processing)

The system includes a web UI, multi-agent AI assistant, SDK/CLI for automation, human-in-the-loop review, extensibility via Lambda hooks and MCP integrations, and comprehensive analytics/reporting.

### Key Metrics

| Metric | Value |
|--------|-------|
| **AWS Services Used** | 15+ (Bedrock incl. Data Automation/AgentCore, Textract, Lambda, Step Functions, DynamoDB, S3, **API Gateway**, Cognito, Athena, Glue, OpenSearch, CloudFront, WAF, SQS, EventBridge, CloudWatch) |
| **Lambda Functions** | 130+ |
| **DynamoDB Tables** | 15 (parent and nested stacks) |
| **S3 Buckets** | 13 |
| **UI API operations** | 168 (single `POST /op/{field}` route) |
| **Processing Modes** | 2 (Pipeline, BDA) |
| **RBAC Roles** | 5 (Admin, Author, Reviewer, Annotator, Viewer) + separate M2M OAuth realm for the Jobs API |
| **UI hosting modes** | 2 (CloudFront, API Gateway S3 proxy) |

## 3. Threat Model Results

### 3.1 Threats Identified

| Category | Count |
|----------|-------|
| **Total threats identified** | **123** |
| Critical risk (score 8-9) | 8 |
| High risk (score 6-7) | 33 |
| Medium risk (score 3-5) | 63 |
| Low risk (score 1-2) | 19 |

### 3.2 STRIDE Distribution

| STRIDE Category | Count | Key Concern |
|----------------|-------|-------------|
| **Tampering** | 51 | Prompt injection, configuration manipulation, data/ground-truth poisoning, reviewer-controlled output, routing that decides what is reviewed |
| **Information Disclosure** | 49 | Data exfiltration via extensibility points, object-read scoping, review metadata, token/credential exposure, Terraform state on local disk |
| **Elevation of Privilege** | 43 | RBAC bypass, hook/feature privilege escalation, deployment-role breadth, per-object authorization that is opt-in per resolver, parser exploits |
| **Denial of Service** | 22 | Resource exhaustion, cost escalation, redaction loops, service dependency |
| **Spoofing** | 17 | Token theft, caller-identity spoofing, credential compromise |
| **Repudiation** | 7 | Insufficient audit trail, BDA opacity, review history that is not tamper-evident |

> Counts sum to more than 123 because a threat may carry multiple STRIDE categories.

### 3.3 Mitigation Status

| Status | Count | Percentage |
|--------|-------|------------|
| **Mitigated** | 63 | 51% |
| **Partially Mitigated** | 55 | 45% |
| **Open** (real gap, needs work) | **1** | **1%** |
| **Accepted** | 4 | 3% |

The one **Open** item is JOB.T02 (the Jobs API sits outside the automated
authorization harness). CHAT.T03 and CHAT.T06 are Mitigated: the chat streaming
Function URL verifies the caller's Cognito ID token, applies the RBAC group checks
from it, and binds each chat session and its memory to the verified `sub`. UI.T07 (no CSP in
API-Gateway/GovCloud hosting mode) was closed in v0.6.x, and UI.T06 is now
**Partially Mitigated**: the browser's Identity Pool role grants no S3, so every
document read is an API call behind a group floor, a fail-closed bucket allow-list
and a per-document configuration-profile check — what remains is that the product
records no document owner, so a caller whose profile scope is unset reads any
document through the resolver. The Open item is a code/config change; see
[risk-matrix §5](../risk-assessment/risk-matrix.md#5-recommendations).

**Status of the recently merged fixes.** Their effect differs per threat, so each
is stated rather than grouped. **Closed, and the threat with it:** #919, so `onError:
fail` is terminal at all seven hook points (HOOK.T07, Mitigated). **Closed, threat
kept Partially Mitigated:** #928, so the API dispatcher denies by default (AUTH.T16
remains Partially Mitigated on the remaining 403-mapping fallback), and #927, which
confines the deployment service role's IAM writes and `iam:PassRole` to names
starting with the stack-name prefix and requires every role it creates to carry a
permissions boundary; the role's service wildcards stay broad by design, so SDK.T05
is Partially Mitigated at score 3. **Closed, and the threats with it:** CHAT.T03 and
CHAT.T06, by the chat streaming endpoint verifying the caller's Cognito ID token and
recording an owner per chat session; see
[companion-chat CHAT.T06](../feature-threats/companion-chat.md#chatt06-client-supplied-caller-identity-on-the-agent-streaming-route).
#921 (consistent log redaction, AUTH.T15) is likewise closed without changing that
threat's status. The status columns in this model deliberately do not credit an unmerged fix, because a
threat model that counts intentions as controls is worse than one that is merely
out of date.

## 4. Key Risk Areas

### 4.1 Prompt Injection (Highest Impact)

Prompt injection remains the top threat vector across document processing (PM.T01), chat interactions (CHAT.T01), knowledge base retrieval (KB.T02), and discovery (RPT.T05). The system processes untrusted document content through LLM prompts, creating inherent injection risk. PM.T01 is Partially Mitigated: the optional Bedrock Guardrail is not applied to the Classification, OCR or Assessment functions, and human review of low-confidence values is the backstop.

**Mitigations**: Prompt engineering with guardrails, input/output tagging, Bedrock Guardrails, output schema validation, evaluation framework for accuracy monitoring, human review for critical documents.

### 4.2 Data Exfiltration via Extensibility Points

MCP integrations (MCP.T01) and post-processing Lambda hooks (HOOK.T02) can send processed document data to external systems. While this is by design for integration purposes, it creates data exfiltration channels.

**Mitigations**: IAM least-privilege, customer-managed VPC with egress controls, audit logging, security review documentation. Partially mitigated — additional VPC egress controls recommended.

### 4.3 Configuration as Attack Surface

The system's high configurability (prompts, schemas, model selection, agent tools) means configuration tampering (PM.T06) has critical impact. A compromised admin account could alter processing behavior for all documents.

**Mitigations**: five-group RBAC — `Admin`, `Author`, `Reviewer`, `Annotator`, `Viewer`, with configuration writes restricted to `Admin` — plus configuration versioning, JSON Schema validation and audit logging. Note that Cognito's group `Precedence` values order IAM-role selection only; they are not a privilege hierarchy, so no group inherits another's permissions.

### 4.4 Authentication & Authorization

The API Gateway Cognito authorizer only authenticates the JWT. Group evaluation happens in two places: the API dispatcher enforces each operation's declared groups and refuses an undeclared operation, and the resolver Lambdas enforce everything narrower — configuration-profile and test-set scope, ownership, and review-queue eligibility. A resolver missing one of those checks exposes the objects it guards to every caller the operation's groups admit (AUTH.T03, AUTH.T08, AUTH.T16). A deployment can be one tenant's, or shared by several tenants (v0.6.14), in which case those same scope checks are what separate one tenant from another (TEN.T01–T05).

HITL review queues (v0.6.13) add 40 operations and give the `Annotator` group production documents in the queues it is eligible for; their per-item operations check eligibility and profile scope and fail closed, the listings and a scoped caller's queue counts filter each item by its own profile (HITL.T01), and only an item's holder can write it (HITL.T02).

Tenant isolation (v0.6.14) lets several tenants share one stack. Tenant declarations tie Input-bucket prefixes to a profile name pattern and are enforced when mappings, tenants and scopes are written. `TenantIsolationMode=Strict` makes isolation fail closed, and `TenantGrantSource` moves who grants a user's tenant to the identity provider's claim or an entitlement system. Isolation is enforced by the application rather than by IAM, Admins are cross-tenant, and the defaults (`Off`, `Manual`) leave an unscoped non-Admin unrestricted, so a shared stack should run `Strict` (TEN.T01). Under `Strict` the bucket policies delegate to every S3 Access Point the account owns, so restricting access point creation is a prerequisite the template cannot enforce (TEN.T02).

**Mitigations**: A default-deny group floor at the dispatcher and per-operation resolver authorization for all 168 routable operations, config-version scope checks, object-level ownership checks, and — because the boundary is now imperative code rather than a declarative gateway rule — an **automated authorization test harness** (`make api-test` / `make api-test-static`) that fails the CI gate on any missing or regressed check. Cognito advanced security features.

The dispatcher now denies an undeclared operation by default (**issue #928**,
closed), but that floor decides only which groups may call an operation. Every
narrower check — configuration-profile and test-set scope, ownership, and
review-queue eligibility — is still written by hand in the resolver that does the
work, so the harness remains what catches a missing one (AUTH.T16).

## 5. Recommendations

### Immediate (Partially Mitigated Critical/High Risks)

1. **Run the live authorization cells for the chat streaming Function URL
   (CHAT.T03, CHAT.T06)** — the endpoint verifies the caller's ID token and the
   session owner, and `make api-test` has cells for it that have not yet been run
   against a deployment
2. **Plan where Terraform state lives (SDK.T06)** — `idp-cli deploy --terraform`
   writes `terraform.tfstate` unencrypted to a local working directory with no
   backend, and that state holds account IDs, ARNs and secrets such as the MCP
   client secrets; configure an encrypted, locked remote backend before the first
   apply
3. **Make per-object authorization structural (AUTH.T16)** — the
   dispatcher's default deny (**issue #928**, closed) covers the group floor only;
   scope, ownership and review-queue eligibility are hand-written per resolver, and
   the 403 status still derives partly from error-message text
4. **Give documents an owner to scope reads against (UI.T06)** — every document
   read is now an API call under a group floor, a fail-closed bucket allow-list and
   three caller-derived scope checks, one of them the owning document's
   configuration profile; the browser's Identity Pool role grants no S3, so there is
   no path around them, and every operation on a single document applies the
   profile scope. Under the default `TenantIsolationMode: Off` the profile axis
   narrows a *scoped* caller only, and scoping is opt-in per user, so a caller whose
   scope is unset still reads any document by key; `Strict` refuses such a caller
   every document, and tenant declarations confine each user's scope to one tenant.
   What is still missing is a per-*user* ownership attribute (**issue #1033**,
   partial)
5. **Add bundle integrity verification (SRI) to the Feature Platform (FEAT.T01)** —
   installed extension UI code runs unsandboxed in the host origin with the user's session
6. **Implement VPC egress controls** for MCP Lambda functions to prevent unauthorized data exfiltration
7. **Publish secure hook deployment guide** with reference VPC architecture and IAM templates
8. **Enhance SDK credential management** with credential helper integration
9. **Schedule the chat streaming Function URL cells** — `make api-test` covers that transport (`run_chat_session_idor_suite`), but it runs only when invoked, so a runtime regression there is caught at the next live run rather than by CI

### Ongoing

1. **Monitor evaluation metrics** for accuracy degradation indicating prompt injection attacks
2. **Periodic authorization review** via `make api-test` against a live stack, reviewing the op×role matrix report and any WARN gaps
3. **Athena query pattern monitoring** for anomalous data access
4. **Agent usage analytics** to detect tool invocation anomalies

## 6. Compliance

The threat model has been developed using:
- **STRIDE methodology** for systematic threat identification
- **Risk scoring** (Likelihood × Severity) for prioritization
- **AWS Well-Architected Framework** security pillar alignment
- **AWS Threat Model Template** requirements

## 7. Document References

| Document | Description |
|----------|-------------|
| [System Overview](../architecture/system-overview.md) | Unified architecture, components, trust boundaries |
| [Data Flows](../architecture/data-flows.md) | All data flow diagrams with security analysis |
| [STRIDE Analysis](../threat-analysis/stride-analysis.md) | Full STRIDE analysis across all components |
| [Risk Matrix](../risk-assessment/risk-matrix.md) | Complete risk register with scoring |
| [Implementation Guide](implementation-guide.md) | Security controls implementation details |
| [Threat ID Glossary](../threat-id-glossary.md) | All 123 threat IDs with cross-references |
