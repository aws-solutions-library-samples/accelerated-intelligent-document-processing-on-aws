# Threat Model — Executive Summary

## Document Information

| Field | Value |
|-------|-------|
| **Document Version** | 3.3 |
| **Last Updated** | 2026-09-19 |
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
| **UI API operations** | 164 (single `POST /op/{field}` route) |
| **Processing Modes** | 2 (Pipeline, BDA) |
| **RBAC Roles** | 5 (Admin, Author, Reviewer, Annotator, Viewer) + separate M2M OAuth realm for the Jobs API |
| **UI hosting modes** | 2 (CloudFront, API Gateway S3 proxy) |

## 3. Threat Model Results

### 3.1 Threats Identified

| Category | Count |
|----------|-------|
| **Total threats identified** | **104** |
| Critical risk (score 8-9) | 9 |
| High risk (score 6-7) | 31 |
| Medium risk (score 3-5) | 47 |
| Low risk (score 1-2) | 17 |

### 3.2 STRIDE Distribution

| STRIDE Category | Count | Key Concern |
|----------------|-------|-------------|
| **Tampering** | 40 | Prompt injection, configuration manipulation, data/ground-truth poisoning, reviewer-controlled output |
| **Information Disclosure** | 40 | Data exfiltration via extensibility points, object-read scoping, review metadata, token/credential exposure |
| **Elevation of Privilege** | 34 | RBAC bypass, hook/feature privilege escalation, deployment-role breadth, per-object authorization that is opt-in per resolver, parser exploits |
| **Denial of Service** | 18 | Resource exhaustion, cost escalation, redaction loops, service dependency |
| **Spoofing** | 16 | Token theft, caller-identity spoofing, credential compromise |
| **Repudiation** | 5 | Insufficient audit trail, BDA opacity, review history that is not tamper-evident |

> Counts sum to more than 104 because a threat may carry multiple STRIDE categories.

### 3.3 Mitigation Status

| Status | Count | Percentage |
|--------|-------|------------|
| **Mitigated** | 64 | 62% |
| **Partially Mitigated** | 33 | 32% |
| **Open** (real gap, needs work) | **3** | **3%** |
| **Accepted** | 4 | 4% |

The three **Open** items are CHAT.T03 and CHAT.T06 (the chat streaming Function URL
enforces neither RBAC group nor session ownership, and on the deployed transport the
turn is still attributed to a client-supplied caller identity) and JOB.T02 (the Jobs
API sits outside the automated authorization harness). UI.T07 (no CSP in
API-Gateway/GovCloud hosting mode) was closed in v0.6.x, and UI.T06 is now
**Partially Mitigated**: the browser's Identity Pool role grants no S3, so every
document read is an API call behind a group floor, a fail-closed bucket allow-list
and a per-document configuration-profile check — what remains is that the product
records no document owner, so a caller whose profile scope is unset reads any
document through the resolver. All three Open items are code/config changes; see
[risk-matrix §5](../risk-assessment/risk-matrix.md#5-recommendations).

**What changed since the 0.6.9 review.** Five issues this summary previously
described as in flight have merged, and their effect differs per threat, so each is
stated rather than grouped. **Closed and the threat with it:** #919, so `onError: fail`
is terminal at all seven hook points (HOOK.T07, now Mitigated), and #928, so the API
dispatcher denies by default (AUTH.T16 keeps its Partially Mitigated status on the
remaining 403-mapping fallback). **Closed, threat downgraded not closed:** #927
attaches a permissions boundary to the deployment service role and constrains the
roles it may create, which is real containment, but the role's own `iam:*` grant is
unchanged — SDK.T05 moves from Open to **Partially Mitigated** and is not fixed.
**Closed, threat unchanged:** #920 makes a contradicting client-supplied identifier a
403, but on this transport the SigV4 principal is a pool-wide constant, so
`resolve_caller_sub` still falls back to the body value and that is what a turn is
attributed to — CHAT.T03 and CHAT.T06 stay **Open**, which is the accounting in
[companion-chat CHAT.T06](../feature-threats/companion-chat.md#chatt06-client-supplied-caller-identity-on-the-agent-streaming-route).
#921 (consistent log redaction, AUTH.T15) is likewise closed without changing that
threat's status. The status columns in this model deliberately do not credit an unmerged fix, because a
threat model that counts intentions as controls is worse than one that is merely
out of date.

## 4. Key Risk Areas

### 4.1 Prompt Injection (Highest Impact)

Prompt injection remains the top threat vector across document processing (PM.T01), chat interactions (CHAT.T01), knowledge base retrieval (KB.T02), and discovery (RPT.T05). The system processes untrusted document content through LLM prompts, creating inherent injection risk.

**Mitigations**: Prompt engineering with guardrails, input/output tagging, Bedrock Guardrails, output schema validation, evaluation framework for accuracy monitoring, human review for critical documents.

### 4.2 Data Exfiltration via Extensibility Points

MCP integrations (MCP.T01) and post-processing Lambda hooks (HOOK.T02) can send processed document data to external systems. While this is by design for integration purposes, it creates data exfiltration channels.

**Mitigations**: IAM least-privilege, customer-managed VPC with egress controls, audit logging, security review documentation. Partially mitigated — additional VPC egress controls recommended.

### 4.3 Configuration as Attack Surface

The system's high configurability (prompts, schemas, model selection, agent tools) means configuration tampering (PM.T06) has critical impact. A compromised admin account could alter processing behavior for all documents.

**Mitigations**: five-group RBAC — `Admin`, `Author`, `Reviewer`, `Annotator`, `Viewer`, with configuration writes restricted to `Admin` — plus configuration versioning, JSON Schema validation and audit logging. Note that Cognito's group `Precedence` values order IAM-role selection only; they are not a privilege hierarchy, so no group inherits another's permissions.

### 4.4 Authentication & Authorization

The API Gateway Cognito authorizer only authenticates the JWT. Group evaluation happens in two places: the API dispatcher enforces each operation's declared groups and refuses an undeclared operation, and the resolver Lambdas enforce everything narrower — configuration-profile and test-set scope, ownership, and review-queue eligibility. A resolver missing one of those checks exposes the objects it guards to every caller the operation's groups admit (AUTH.T03, AUTH.T08, AUTH.T16). The system is single-tenant per deployment.

HITL review queues (v0.6.13) add 40 operations and give the `Annotator` group production documents in the queues it is eligible for; their per-item operations check eligibility and profile scope and fail closed, the listings filter each item by its own profile (HITL.T01), and only an item's holder can write it (HITL.T02).

**Mitigations**: A default-deny group floor at the dispatcher and per-operation resolver authorization for all 164 routable operations, config-version scope checks, object-level ownership checks, and — because the boundary is now imperative code rather than a declarative gateway rule — an **automated authorization test harness** (`make api-test` / `make api-test-static`) that fails the CI gate on any missing or regressed check. Cognito advanced security features.

The dispatcher now denies an undeclared operation by default (**issue #928**,
closed), but that floor decides only which groups may call an operation. Every
narrower check — configuration-profile and test-set scope, ownership, and
review-queue eligibility — is still written by hand in the resolver that does the
work, so the harness remains what catches a missing one (AUTH.T16).

## 5. Recommendations

### Immediate (Partially Mitigated Critical/High Risks)

1. **Close the chat streaming authorization gaps (CHAT.T03, CHAT.T06)** — first
   establish a *verified* subject on the Lambda Function URL transport (the
   browser presenting its Cognito ID token alongside the signed request), because
   the identifier available today is a pool-wide constant rather than a per-user
   value; only then can session ownership and the RBAC group be enforced there.
   **Issue #920** (PR #954) is a partial step, not a closure — it rejects a
   contradicting client-supplied `callerSub` but does not create a per-user
   identity
2. **Narrow the deployment service role (SDK.T05)** — **issue #927** attached a
   permissions boundary and constrained the roles it may create, but the role keeps
   its own `iam:*` and broad service wildcards, so possession of it is still close
   to possession of the account
3. **Make per-object authorization structural (AUTH.T16)** — the
   dispatcher's default deny (**issue #928**, closed) covers the group floor only;
   scope, ownership and review-queue eligibility are hand-written per resolver, and
   the 403 status still derives partly from error-message text
4. **Give documents an owner to scope reads against (UI.T06)** — every document
   read is now an API call under a group floor, a fail-closed bucket allow-list and
   three caller-derived scope checks, one of them the owning document's
   configuration profile; the browser's Identity Pool role grants no S3, so there is
   no path around them. What is missing is an ownership or tenancy attribute: the
   profile axis narrows a *scoped* caller only, and scoping is opt-in per user, so a
   caller whose scope is unset still reads any document by key
   (**issue #1033**, partial)
5. **Add bundle integrity verification (SRI) to the Feature Platform (FEAT.T01)** —
   installed extension UI code runs unsandboxed in the host origin with the user's session
6. **Implement VPC egress controls** for MCP Lambda functions to prevent unauthorized data exfiltration
7. **Publish secure hook deployment guide** with reference VPC architecture and IAM templates
8. **Enhance SDK credential management** with credential helper integration
9. **Extend the automated authorization harness to the chat streaming Function URL** — it currently covers `POST /op/{field}` only, leaving that transport's gaps (CHAT.T03/T06) undetectable by CI

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
| [Threat ID Glossary](../threat-id-glossary.md) | All 104 threat IDs with cross-references |
