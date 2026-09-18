# STRIDE Threat Analysis

## Document Information

| Field | Value |
|-------|-------|
| **Document Version** | 3.2 |
| **Last Updated** | 2026-09-17 |
| **Applies to release** | v0.6.9 |
| **Classification** | Internal |
| **Methodology** | STRIDE (Spoofing, Tampering, Repudiation, Information Disclosure, Denial of Service, Elevation of Privilege) |

## 1. Overview

This document provides a comprehensive STRIDE analysis across all components of
the GenAI IDP Accelerator unified architecture. Each STRIDE category is analyzed
for the system's major components: document processing pipeline, AI/ML services,
web UI, agent system, authentication/authorization, extensibility (hooks/MCP), and
data storage/analytics.

> **How to read this document.** It is deliberately **category-level**: rows are
> phrased as threat *classes* rather than as the numbered threat entries, so a
> reader can scan a STRIDE category without holding 98 identifiers in their head.
> The authoritative per-threat record — identifier, score, status, and whether a
> mitigation is present or pending — is the
> [risk register](../risk-assessment/risk-matrix.md), which is derived from the
> generated export. Where a row's mitigation depends on an unmerged change it is
> marked **pending** with its issue number; treat those as absent today.

## 2. Spoofing

Spoofing threats involve an attacker pretending to be something or someone they are not.

### 2.1 Identity Spoofing

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **Stolen JWT tokens** used to impersonate legitimate users | API Gateway REST API, Cognito | High | Short-lived tokens (1hr), Cognito advanced security, TLS enforcement; token-negative + expiry + logout suites in `make api-test` (see AUTH.T10) |
| **Compromised SDK/CLI credentials** on developer machines | IDP SDK/CLI | Medium | Environment variable-based credentials, credential helpers, documentation |
| **Self-registration** of unauthorized accounts | Cognito User Pool | Medium | Self-signup disabled, admin-created accounts only |
| **Refresh token replay** for persistent access | Cognito | Medium | Configurable refresh token expiry, revocation capabilities, anomaly detection |
| **External MCP client impersonation** | AgentCore Gateway / MCP handler | Medium | Cognito authentication required for all clients (dedicated M2M resource server) |
| **Client-supplied caller identity on the agent streaming route** — a request body field is read in preference to the request's signed identity | Lambda Function URL (chat streaming) | High | **Not mitigated today.** The identity available from the signed request is the assumed-role session name rather than a verified Cognito `sub`, and that session name is the same for every user of the deployment, so establishing a *verified* caller is the fix — reordering the two would attribute every turn to one shared identifier. See CHAT.T06; **issue #920** / PR #954 is a partial step and leaves the threat Open |
| **Group assignment from a user-writable attribute** — an external identity provider maps a claim the user can edit onto a Cognito group | Cognito pre-token trigger, external IdP | Medium | Group mapping reads provider-controlled claims; `make verify-idp-federation` exercises a real federated sign-in. Note `Annotator` is absent from the federation `GROUP_MAPPING`, so it cannot be granted by federation at all. See AUTH.T13 |

### 2.2 Service Spoofing

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **MCP tool response spoofing** — external service returns manipulated data | MCP Integration | Medium | Response validation, tool output sanitization |
| **Lambda hook substitution** — replacing legitimate hook with malicious one | Lambda Hooks | Low | CloudFormation-managed hook ARN, IAM restrictions on Lambda updates |

## 3. Tampering

Tampering threats involve unauthorized modification of data or code.

### 3.1 Document/Data Tampering

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **Prompt injection via document content** — adversarial text in uploaded documents | Pipeline Processing | High | Input sanitization, prompt guardrails, output validation, Bedrock Guardrails |
| **Prompt injection via chat messages** — manipulating agent behavior | Companion Chat | High | System prompt hardening, input/output tags, Bedrock Guardrails |
| **Configuration tampering** — malicious modification of prompts/schemas | Configuration System | Critical | RBAC, config versioning, JSON Schema validation, audit logging |
| **Few-shot example poisoning** — biased training examples | Pipeline Processing | Medium | Admin-only management, evaluation framework |
| **Knowledge Base poisoning** — contaminated reference documents | Knowledge Base | High | Admin-only KB management, sync review, evaluation monitoring |
| **RAG context injection** — prompt injection via retrieved KB content | Knowledge Base | High | Context isolation in prompts, output validation |
| **Conversation history poisoning** — multi-turn prompt injection | Companion Chat | Medium | History sanitization, session limits |
| **Evaluation data manipulation** — hiding accuracy degradation | Evaluation | High | S3 versioning, RBAC, integrity checks |
| **Reporting data tampering** — altering analytics records | Reporting | Medium | S3 versioning, write-only IAM, CloudTrail |
| **Discovery prompt injection** — adversarial sample documents | Discovery | High | Draft-only configs, human review required, schema validation |

### 3.2 Code/Infrastructure Tampering

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **Malicious MCP tool injection** — adding unauthorized tools | MCP Integration | High | IaC-managed tool definitions, admin-only deployment |
| **MCP response injection** — external service injects malicious content | MCP Integration | High | Output sanitization, response size limits |
| **Inference hook result tampering** — corrupted extraction results | Lambda Hooks | High | Output schema validation, assessment step, evaluation |
| **Cross-step data poisoning** — corrupted intermediate pipeline data | Pipeline Processing | High | S3 encryption, IAM role separation, state validation |
| **BDA output mapping errors** — data corruption in format normalization | BDA Mode | Medium | Strict schema validation, defensive parsing |
| **OCR manipulation** — adversarial documents producing incorrect text | Pipeline Processing | Medium | Format validation, confidence thresholds |
| **Glue Catalog manipulation** — altered data schemas/locations | Reporting | Medium | IAM restrictions, CloudTrail, catalog validation |
| **Hook failure does not halt the workflow** — a pipeline hook relied on as a gate fails and processing continues past it | Lambda Hooks | High | `onError: fail` is terminal at the `preprocessing` hook point only; at the other six the state machine's `Catch` block routes forward to the next step. A deployment relying on a hook as a compliance or business gate at one of those points does not have that guarantee. See HOOK.T07; **pending in issue #919** |

## 4. Repudiation

Repudiation threats involve users denying they performed an action.

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **Untracked configuration changes** — no audit trail for config modifications | Configuration System | Medium | CloudWatch logging, config versioning, CloudTrail |
| **Untracked agent actions** — agent tool invocations without logging | Agent System | Medium | All agent/tool invocations logged to CloudWatch |
| **BDA processing opacity** — limited internal processing visibility | BDA Mode | Medium | BDA API call logging via CloudTrail, output validation |
| **Untracked document uploads** — uploads without attribution | S3 Input Bucket | Low | EventBridge events, S3 access logging, CloudTrail |
| **Untracked hook invocations** — customer code execution without logging | Lambda Hooks | Low | Platform logs hook invocations, CloudWatch logs on hook Lambdas |

## 5. Information Disclosure

Information disclosure threats involve exposure of sensitive data to unauthorized parties.

### 5.1 Data Exposure

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **Document data sent to AI services** — PII/sensitive content in LLM prompts | Bedrock, Textract | High | AWS data processing policies (no training), TLS, IAM roles |
| **Cross-user data leakage via Athena** — accessing other users' documents | Analytics Agent | High | RBAC, single-tenant deployment, query scoping |
| **Athena query results exposure** — stored results accessible | Reporting | High | Workgroup restrictions, result lifecycle policies, IAM |
| **Conversation history exposure** — sensitive chat content | Companion Chat | High | DynamoDB encryption, user-scoped access, TTL policies |
| **Data exfiltration via MCP tools** — sending data to external services | MCP Integration | Critical | IAM least-privilege, VPC egress controls, audit logging |
| **Data exfiltration via post-processing hooks** — full results sent externally | Lambda Hooks | Critical | Customer-managed VPC, security review, monitoring |
| **OpenSearch vector store exposure** — KB embeddings accessible | Knowledge Base | Medium | Encryption, IAM/network policies, no public access |
| **Client-side config exposure** — backend endpoints visible in JS | Web UI | Low | All endpoints require auth, security through access control |
| **Credential-shaped values in resolver logs** — divergent redaction denylists across copies of the sanitizer | API resolver Lambdas, CloudWatch Logs | Medium | Every resolver redacts, so the exposure is limited to the key spellings the hand-copied lists omit; log groups are retention-bounded and 108 of 109 use the stack KMS customer-managed key. Converging on the canonical denylist is **pending in issue #921**; giving `HttpApiDispatcherLogGroup` the CMK is a one-line template change. See AUTH.T15 |

### 5.2 Session/Token Exposure

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **JWT token theft via XSS** — browser-stored tokens stolen | Web UI | High | React XSS protection, CSP headers, short token lifetime |
| **Chat stream cross-user access** — reading another user's stream via their `sessionId` | Lambda Function URL (chat streaming) | High | **OPEN GAP** — SigV4 authenticates but no group or session-ownership check on this transport, and the shared `CognitoAuthorizedRole` is common to all five groups so the IAM gate cannot distinguish them. Not covered by `make api-test` or by the WAF WebACL. See CHAT.T03; **pending in issue #920** |
| **Conversation session hijacking** — accessing other users' chats | Companion Chat | High | UUID session IDs, user-scoped DynamoDB queries |
| **SDK credential exposure** — credentials on developer machines | SDK/CLI | High | Env var credentials, short-lived tokens, secure documentation |
| **Presigned URL interception** — captured upload URLs reused | Web UI | Medium | Short expiration, conditions, TLS |

## 6. Denial of Service

Denial of service threats involve making the system unavailable.

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **Processing pipeline saturation** — flooding with documents | Document Processing | Medium | SQS buffering plus DynamoDB **admission control**: a slot is taken by a conditional counter increment (`active_count < max`), so over-limit work is rejected atomically rather than by an in-memory comparison; drift is reconciled and emitted as a metric, a circuit breaker can stop admission entirely, and execution names are derived deterministically so a redelivered message cannot start a second execution. The failure mode to watch is a *leaked slot* (capacity lost until reconciliation), not over-admission. CloudWatch alarms |
| **Bedrock quota exhaustion** — excessive model invocations | AI/ML Services | Medium | Token limits, rate limiting, capacity planning, alarms |
| **Textract throttling** — exceeding OCR API limits | Pipeline Mode | Medium | Retry with backoff, DLQ, service quota management |
| **BDA service unavailability** — BDA outage or throttling | BDA Mode | Medium | Mode switching fallback, retry/DLQ, alarms |
| **Lambda concurrency exhaustion** — all functions saturated | All Lambdas | Medium | Reserved concurrency, SQS buffering, alarms |
| **Expensive agent operations** — complex tool chains consuming resources | Agent System | Medium | Lambda timeouts, token limits, rate limiting |
| **Batch processing abuse via SDK** — overwhelming pipeline | SDK/CLI | Medium | Concurrency controls, rate limiting, alarms |
| **Test Studio cost escalation** — excessive interactive testing | Test Studio | Medium | RBAC, rate limiting, cost monitoring |
| **Hook Lambda failure cascade** — customer hooks blocking pipeline | Lambda Hooks | Medium | Step Functions timeouts, error handling, DLQ |
| **AgentCore gateway disruption** — MCP infrastructure unavailable | MCP Integration | Medium | Health monitoring, alarms, lifecycle management |

## 7. Elevation of Privilege

Elevation of privilege threats involve gaining capabilities beyond what was authorized.

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **Cognito group manipulation** — self-promoting to Admin role | Authentication | Critical | IAM-protected Cognito admin APIs, no self-service groups, CloudTrail |
| **API authorization bypass** — exploiting resolver gaps | RBAC | High | Per-op resolver group checks (the authorization boundary — the gateway authorizer only authenticates); `make api-test`/`api-test-static` gate regressions; see AUTH.T03/T08 |
| **Authorization is opt-in per operation** — the dispatcher forwards a field whether or not anything behind it enforces a group | API Gateway dispatcher | High | The manifest (`scripts/api_rbac_expectations.yaml`, all 118 operations) plus `make api-test-static` in both CI systems is what stands in for a gateway rule; the one dispatcher-level group check does not deny a field it has no entry for, and the 403 mapping keys partly on error-message text. A default-deny gate is **pending in issue #928**. See AUTH.T16 |
| **Deployment role breadth** — the shipped CloudFormation service role can manipulate permissions boundaries and holds broad service wildcards | Deployment / SDLC | Critical | **Not mitigated today.** Possession of the role is close to possession of the account, and its trust policy carries no conditions. Narrowing it is **pending in issue #927**. Until then, treat who may assume it as the control. See SDK.T05 |
| **Agent routing manipulation** — tricking orchestrator to invoke restricted agents | Agent System | Medium | Agent-level auth, tool access controls, audit logging |
| **Configuration-driven privilege escalation** — malicious config enabling unauthorized model access | Configuration | High | Config schema validation, RBAC, model allowlisting |
| **MCP tool parameter manipulation** — causing tools to access unauthorized resources | MCP Integration | High | Input validation, parameter schemas, least-privilege credentials |
| **Hook IAM role over-privilege** — customer hooks accessing platform resources | Lambda Hooks | High | Separate IAM roles, platform resource policies, documentation |
| **Arbitrary code execution via AgentCore** — AI-generated malicious code | Agent System | Medium | AgentCore sandbox (no network, no credentials), output validation |
| **BDA project configuration tampering** — altering BDA processing behavior | BDA Mode | High | IAM restrictions, CloudTrail, admin-only access |

## 8. Cross-Cutting Threats

These threats span multiple STRIDE categories and components:

### 8.1 Supply Chain Threats

| Threat | Impact | Mitigations |
|--------|--------|-------------|
| **SDK dependency compromise** | Code execution on developer machines | Pinned versions, dependency scanning |
| **Lambda layer compromise** | Code execution in processing pipeline | Verified layers, integrity checks |
| **React dependency compromise** | XSS in web UI | Lock files, dependency scanning, CSP |

### 8.2 AI/ML-Specific Threats

| Threat | Impact | Mitigations |
|--------|--------|-------------|
| **Model hallucination** | Incorrect business decisions | Output validation, evaluation, human review |
| **Training data contamination** (upstream) | Degraded model performance | Use well-known models, evaluation framework |
| **Indirect prompt injection** (via documents, KB, MCP responses) | Model behavior manipulation | Input sanitization, context isolation, Bedrock Guardrails |

### 8.3 Infrastructure Threats

| Threat | Impact | Mitigations |
|--------|--------|-------------|
| **CloudFormation stack manipulation** | Full system compromise | IAM and stack policies. Note the CloudFormation **service role** is currently part of the problem rather than the mitigation: as shipped it is broad enough to reach account administrator (SDK.T05, **pending in issue #927**), so it does not narrow the blast radius of a stack operation the way a scoped service role would |
| **S3 bucket policy misconfiguration** | Data exposure | IaC-defined policies, security reviews, automated checks |
| **DynamoDB over-permissions** | Cross-table data access | Per-table IAM policies, least-privilege Lambda roles |
