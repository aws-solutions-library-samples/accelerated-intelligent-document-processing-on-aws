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
| **Lambda Functions** | 115+ |
| **DynamoDB Tables** | 12 |
| **S3 Buckets** | 13 |
| **UI API operations** | 118 (single `POST /op/{field}` route) |
| **Processing Modes** | 2 (Pipeline, BDA) |
| **RBAC Roles** | 5 (Admin, Author, Reviewer, Annotator, Viewer) + separate M2M OAuth realm for the Jobs API |
| **UI hosting modes** | 2 (CloudFront, API Gateway S3 proxy) |

## 3. Threat Model Results

### 3.1 Threats Identified

| Category | Count |
|----------|-------|
| **Total threats identified** | **99** |
| Critical risk (score 8-9) | 9 |
| High risk (score 6-7) | 31 |
| Medium risk (score 3-5) | 44 |
| Low risk (score 1-2) | 15 |

### 3.2 STRIDE Distribution

| STRIDE Category | Count | Key Concern |
|----------------|-------|-------------|
| **Tampering** | 39 | Prompt injection, configuration manipulation, data/ground-truth poisoning |
| **Information Disclosure** | 38 | Data exfiltration via extensibility points, object-read scoping, token/credential exposure |
| **Elevation of Privilege** | 32 | RBAC bypass, hook/feature privilege escalation, deployment-role breadth, authorization that is opt-in per resolver |
| **Denial of Service** | 17 | Resource exhaustion, cost escalation, redaction loops, service dependency |
| **Spoofing** | 16 | Token theft, caller-identity spoofing, credential compromise |
| **Repudiation** | 4 | Insufficient audit trail, BDA opacity |

> Counts sum to more than 99 because a threat may carry multiple STRIDE categories.

### 3.3 Mitigation Status

| Status | Count | Percentage |
|--------|-------|------------|
| **Mitigated** | 62 | 63% |
| **Partially Mitigated** | 27 | 27% |
| **Open** (real gap, needs work) | **6** | **6%** |
| **Accepted** | 4 | 4% |

The six **Open** items are CHAT.T03 and CHAT.T06 (chat streaming Function URL
enforces neither RBAC group nor session ownership, and the agent route trusts a
client-supplied caller identity), UI.T06 (presigned object reads are
bucket-scoped but not key-scoped, and the buckets are readable directly by every
authenticated user irrespective of group),
JOB.T02 (the Jobs API sits outside the automated authorization harness),
HOOK.T07 (`onError: fail` halts the workflow at one of the seven hook points, not
all seven) and SDK.T05 (the shipped CloudFormation deployment service role is
broad enough to reach account administrator). UI.T07 (no CSP in
API-Gateway/GovCloud hosting mode) was closed in v0.6.x. All six are code/config
changes; see [risk-matrix §5](../risk-assessment/risk-matrix.md#5-recommendations).

**Four of the six have a change in flight, and none of those changes has merged.**
CHAT.T03 and CHAT.T06 are **partly** addressed by **issue #920** (PR #954) —
partly, because that change makes a contradicting client-supplied identifier a
403 but cannot establish a per-user identity on the streaming transport at all,
so both threats stay Open after it merges; HOOK.T07 is addressed by **issue
#919** and SDK.T05 by **issue #927**; two Partially Mitigated threats depend on
**issue #928** (a default-deny gate at the API dispatcher, AUTH.T16) and **issue
#921** (consistent log redaction, AUTH.T15). Read every one of those as
*pending*, and read #920 as *partial even once merged* — see
[companion-chat CHAT.T06](../feature-threats/companion-chat.md#chatt06-caller-identity-on-the-streaming-transport-is-not-a-verified-subject)
for the accounting. The
status columns in this model deliberately do not credit an unmerged fix, because a
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

RBAC is enforced **entirely inside the resolver Lambdas** — the API Gateway Cognito authorizer only authenticates the JWT and performs no group evaluation. Any resolver missing its server-side check exposes a privileged operation to every authenticated user (AUTH.T03, AUTH.T08). The system is single-tenant per deployment.

**Mitigations**: Per-operation resolver authorization for all 118 routable operations, config-version scope checks, object-level ownership checks, and — because the boundary is now imperative code rather than a declarative gateway rule — an **automated authorization test harness** (`make api-test` / `make api-test-static`) that fails the CI gate on any missing or regressed check. Cognito advanced security features.

The harness is doing work the platform is not: the dispatcher itself does not
default-deny — it resolves any field it can map, and the one dispatcher-level
group check does not deny a field it has no entry for — so the manifest and the
harness are what stand in for a gateway rule. Adding a default deny is **pending
in issue #928** (AUTH.T16).

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
2. **Narrow the deployment service role (SDK.T05)** — the shipped role can
   manipulate permissions boundaries and holds broad service wildcards, so
   possession of it is close to possession of the account (**issue #927**, pending)
3. **Default-deny at the API dispatcher (AUTH.T16)** — reject a field with no
   recorded authorization expectation rather than forwarding it, and stop deriving
   the 403 status from error-message text (**issue #928**, pending)
4. **Make hook failure containment uniform (HOOK.T07)** — `onError: fail` is
   terminal at the preprocessing hook point only; a deployment relying on a hook
   as a gate elsewhere does not have that guarantee (**issue #919**, pending)
5. **Scope object reads to the caller (UI.T06)** — today any authenticated user can
   read any object in the stack's document buckets by key, using the Identity Pool
   credentials the browser already holds, with no API call in the path
6. **Add bundle integrity verification (SRI) to the Feature Platform (FEAT.T01)** —
   installed extension UI code runs unsandboxed in the host origin with the user's session
7. **Implement VPC egress controls** for MCP Lambda functions to prevent unauthorized data exfiltration
8. **Publish secure hook deployment guide** with reference VPC architecture and IAM templates
9. **Enhance SDK credential management** with credential helper integration
10. **Extend the automated authorization harness to the chat streaming Function URL** — it currently covers `POST /op/{field}` only, leaving that transport's gaps (CHAT.T03/T06) undetectable by CI

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
| [Threat ID Glossary](../threat-id-glossary.md) | All 99 threat IDs with cross-references |
