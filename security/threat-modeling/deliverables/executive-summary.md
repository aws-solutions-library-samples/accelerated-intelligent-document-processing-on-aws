# Threat Model — Executive Summary

## Document Information

| Field | Value |
|-------|-------|
| **Document Version** | 3.0 |
| **Last Updated** | 2026-07-28 |
| **Applies to release** | v0.6.3 |
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
| **UI API operations** | 97 (single `POST /op/{field}` route) |
| **Processing Modes** | 2 (Pipeline, BDA) |
| **RBAC Roles** | 4 (Admin, Author, Reviewer, Viewer) + separate M2M OAuth realm for the Jobs API |
| **UI hosting modes** | 2 (CloudFront, API Gateway S3 proxy) |

## 3. Threat Model Results

### 3.1 Threats Identified

| Category | Count |
|----------|-------|
| **Total threats identified** | **83** |
| Critical risk (score 8-9) | 6 |
| High risk (score 6-7) | 26 |
| Medium risk (score 3-5) | 37 |
| Low risk (score 1-2) | 14 |

### 3.2 STRIDE Distribution

| STRIDE Category | Count | Key Concern |
|----------------|-------|-------------|
| **Tampering** | 36 | Prompt injection, configuration manipulation, data/ground-truth poisoning |
| **Information Disclosure** | 35 | Data exfiltration via extensibility points, object-read scoping, token/credential exposure |
| **Elevation of Privilege** | 25 | RBAC bypass, hook/feature privilege escalation, agent routing manipulation |
| **Denial of Service** | 14 | Resource exhaustion, cost escalation, redaction loops, service dependency |
| **Spoofing** | 11 | Token theft, caller-identity spoofing, credential compromise |
| **Repudiation** | 3 | Insufficient audit trail, BDA opacity |

> Counts sum to more than 83 because a threat may carry multiple STRIDE categories.

### 3.3 Mitigation Status

| Status | Count | Percentage |
|--------|-------|------------|
| **Mitigated** | 57 | 68% |
| **Partially Mitigated** | 19 | 23% |
| **Open** (real gap, needs work) | **4** | **5%** |
| **Accepted** | 3 | 4% |

The four **Open** items are CHAT.T03 and CHAT.T06 (chat streaming Function URL
enforces neither RBAC group nor session ownership, and the agent route trusts a
client-supplied caller identity), UI.T06 (presigned object reads are
bucket-scoped but not key-scoped, and callable by any authenticated user), and
JOB.T02 (the Jobs API sits outside the automated authorization harness). UI.T07
(no CSP in API-Gateway/GovCloud hosting mode) was closed in v0.6.x. All four are
code/config changes; see [risk-matrix §5](../risk-assessment/risk-matrix.md#5-recommendations).

## 4. Key Risk Areas

### 4.1 Prompt Injection (Highest Impact)

Prompt injection remains the top threat vector across document processing (PM.T01), chat interactions (CHAT.T01), knowledge base retrieval (KB.T02), and discovery (RPT.T05). The system processes untrusted document content through LLM prompts, creating inherent injection risk.

**Mitigations**: Prompt engineering with guardrails, input/output tagging, Bedrock Guardrails, output schema validation, evaluation framework for accuracy monitoring, human review for critical documents.

### 4.2 Data Exfiltration via Extensibility Points

MCP integrations (MCP.T01) and post-processing Lambda hooks (HOOK.T02) can send processed document data to external systems. While this is by design for integration purposes, it creates data exfiltration channels.

**Mitigations**: IAM least-privilege, customer-managed VPC with egress controls, audit logging, security review documentation. Partially mitigated — additional VPC egress controls recommended.

### 4.3 Configuration as Attack Surface

The system's high configurability (prompts, schemas, model selection, agent tools) means configuration tampering (PM.T06) has critical impact. A compromised admin account could alter processing behavior for all documents.

**Mitigations**: 4-tier RBAC (Admin-only for critical config), configuration versioning, JSON Schema validation, audit logging.

### 4.4 Authentication & Authorization

RBAC is enforced **entirely inside the resolver Lambdas** — the API Gateway Cognito authorizer only authenticates the JWT and performs no group evaluation. Any resolver missing its server-side check exposes a privileged operation to every authenticated user (AUTH.T03, AUTH.T08). The system is single-tenant per deployment.

**Mitigations**: Per-operation resolver authorization for all 97 routable operations, config-version scope checks, object-level ownership checks, and — because the boundary is now imperative code rather than a declarative gateway rule — an **automated authorization test harness** (`make api-test` / `make api-test-static`) that fails the CI gate on any missing or regressed check. Cognito advanced security features.

## 5. Recommendations

### Immediate (Partially Mitigated Critical/High Risks)

1. **Close the chat streaming authorization gaps (CHAT.T03, CHAT.T06)** — enforce
   session ownership and the RBAC group on the Lambda Function URL transport, and
   stop trusting a body-supplied `callerSub` on `/chat/agent`
2. **Scope presigned object reads to the caller (UI.T06)** — today any
   authenticated user can read any object in the stack's buckets by key
3. **Add bundle integrity verification (SRI) to the Feature Platform (FEAT.T01)** —
   installed extension UI code runs unsandboxed in the host origin with the user's session
4. **Implement VPC egress controls** for MCP Lambda functions to prevent unauthorized data exfiltration
5. **Publish secure hook deployment guide** with reference VPC architecture and IAM templates
3. **Enhance SDK credential management** with credential helper integration
4. **Extend the automated authorization harness to the chat streaming Function URL** — it currently covers `POST /op/{field}` only, leaving that transport's gaps (CHAT.T03/T06) undetectable by CI

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
| [Threat ID Glossary](../threat-id-glossary.md) | All 83 threat IDs with cross-references |
