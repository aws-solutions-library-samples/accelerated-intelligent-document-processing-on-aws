# Threat ID Glossary

## Document Information

| Field | Value |
|-------|-------|
| **Document Version** | 3.1 |
| **Last Updated** | 2026-08-20 |
| **Applies to release** | v0.6.5.dev1 |
| **Classification** | Internal |
| **Total Threat IDs** | 93 |

## 1. Threat ID Naming Convention

Threat IDs follow the pattern: `{CATEGORY}.T{NN}`

| Prefix | Category | Scope | Document |
|--------|----------|-------|----------|
| **PM** | Pipeline Mode | Textract + Bedrock processing threats | [pipeline-mode.md](architecture/pipeline-mode.md) |
| **BDA** | BDA Mode | Bedrock Data Automation processing threats | [architecture/bda-mode.md](architecture/bda-mode.md) |
| **AGT** | Agent Analysis | Multi-agent AI system threats | [feature-threats/agent-analysis.md](feature-threats/agent-analysis.md) |
| **CHAT** | Companion Chat | Conversational AI and streaming threats | [feature-threats/companion-chat.md](feature-threats/companion-chat.md) |
| **MCP** | MCP Integration | Model Context Protocol / external tool threats | [feature-threats/mcp-integration.md](feature-threats/mcp-integration.md) |
| **KB** | Knowledge Base | RAG and knowledge base threats | [feature-threats/knowledge-base.md](feature-threats/knowledge-base.md) |
| **AUTH** | Authentication/RBAC | Identity, authorization, and access threats | [feature-threats/rbac-authentication.md](feature-threats/rbac-authentication.md) |
| **SDK** | SDK/CLI | Programmatic access and automation threats | [feature-threats/sdk-cli.md](feature-threats/sdk-cli.md) |
| **HOOK** | Lambda Hooks | Customer-managed extensibility threats | [feature-threats/lambda-hooks.md](feature-threats/lambda-hooks.md) |
| **UI** | Web UI | Frontend and API threats | [feature-threats/web-ui.md](feature-threats/web-ui.md) |
| **RPT** | Reporting/Analytics | Data analytics, evaluation, discovery, test sets, document versions | [feature-threats/reporting-analytics.md](feature-threats/reporting-analytics.md) |
| **FEAT** | Feature Platform | Installable extension / feature-platform threats | [feature-threats/feature-platform.md](feature-threats/feature-platform.md) |
| **JOB** | Jobs API | Machine-to-machine Jobs REST API threats | [feature-threats/jobs-api.md](feature-threats/jobs-api.md) |
| **PII** | PII Anonymization | Preprocessing hook + PII redaction extension threats | [feature-threats/pii-anonymization.md](feature-threats/pii-anonymization.md) |
| **SELL** | Seller Entitlement Service | Seller-account activation endpoint for paid extensions (only doc whose assets belong to the SELLER, not the customer) | [feature-threats/seller-entitlement-service.md](feature-threats/seller-entitlement-service.md) |

## 2. Complete Threat ID Reference

### PM — Pipeline Mode (8 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| PM.T01 | Prompt injection via document content | Tampering, EoP | 9 (Very High) |
| PM.T02 | OCR manipulation / adversarial documents | Tampering | 4 (Medium) |
| PM.T03 | Model output manipulation / hallucination | Tampering, ID | 6 (High) |
| PM.T04 | Cross-step data poisoning | Tampering | 3 (Medium) |
| PM.T05 | Textract service dependency | DoS | 4 (Medium) |
| PM.T06 | Configuration tampering | Tampering, EoP | 8 (Critical) |
| PM.T07 | Few-shot example poisoning | Tampering | 2 (Low) |
| PM.T08 | Document content sent to a non-Anthropic model family (OpenAI via `bedrock-mantle`) | ID | 4 (Medium) |

### BDA — BDA Mode (5 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| BDA.T01 | BDA service opacity | ID, Repudiation | 4 (Medium) |
| BDA.T02 | BDA output format mapping errors | Tampering | 2 (Low) |
| BDA.T03 | BDA project configuration tampering | Tampering, EoP | 3 (Medium) |
| BDA.T04 | BDA service availability | DoS | 4 (Medium) |
| BDA.T05 | S3 cross-access via BDA | ID | 2 (Low) |

### AGT — Agent Analysis (5 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| AGT.T01 | SQL injection via natural language | Tampering, ID | 6 (High) |
| AGT.T02 | Arbitrary code execution via AgentCore | Tampering, EoP | 4 (Medium) |
| AGT.T03 | Agent routing manipulation | EoP | 4 (Medium) |
| AGT.T04 | Conversation history poisoning | Tampering | 2 (Low) |
| AGT.T05 | Cross-user data leakage via Athena | ID | 6 (High) |

### CHAT — Companion Chat (6 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| CHAT.T01 | Prompt injection via chat messages | Tampering, EoP | 9 (Very High) |
| CHAT.T02 | Conversation session hijacking | Spoofing, ID | 3 (Medium) |
| CHAT.T03 | Chat streaming Function URL — missing group + session-ownership enforcement | ID, EoP | 6 (High) |
| CHAT.T04 | Conversation history data exposure | ID | 3 (Medium) |
| CHAT.T05 | Streaming response denial of service | DoS | 4 (Medium) |
| CHAT.T06 | Client-supplied caller identity on the agent streaming route | Spoofing | 3 (Medium) |

### MCP — MCP Integration (6 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| MCP.T01 | Data exfiltration via MCP tools | ID | 8 (Critical) |
| MCP.T02 | Malicious tool injection | Tampering, EoP | 3 (Medium) |
| MCP.T03 | MCP response injection | Tampering | 6 (High) |
| MCP.T04 | Unauthorized external service access | Spoofing, EoP | 6 (High) |
| MCP.T05 | External MCP client abuse | Spoofing, DoS | 4 (Medium) |
| MCP.T06 | AgentCore gateway lifecycle attacks | DoS, Tampering | 2 (Low) |

### KB — Knowledge Base (4 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| KB.T01 | Knowledge Base poisoning | Tampering | 6 (High) |
| KB.T02 | RAG context injection | Tampering, EoP | 6 (High) |
| KB.T03 | OpenSearch Serverless data exposure | ID | 2 (Low) |
| KB.T04 | Excessive RAG retrieval | ID, DoS | 2 (Low) |

### AUTH — Authentication & RBAC (12 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| AUTH.T01 | Privilege escalation via group manipulation | EoP | 4 (Critical severity) |
| AUTH.T02 | JWT token theft / replay | Spoofing | 6 (High) |
| AUTH.T03 | Insufficient authorization granularity | EoP | 6 (High) |
| AUTH.T04 | Cognito user pool misconfiguration | Spoofing, ID | 3 (Medium) |
| AUTH.T05 | Refresh token abuse | Spoofing | 3 (Medium) |
| AUTH.T06 | Cross-tenant data access (multi-stack) | ID | 2 (Low) |
| AUTH.T07 | Config-version scope bypass (fail-open scope lookup) | EoP, ID | 6 (High) |
| AUTH.T08 | Silently-ignored schema authorization directives | EoP | 6 (High) |
| AUTH.T09 | Insecure direct object reference (IDOR / BOLA) | ID, EoP | 6 (High) |
| AUTH.T10 | Token lifecycle — post-logout token reuse (stateless JWT) | Spoofing, EoP | 3 (Medium) |
| AUTH.T11 | Weak transport security (TLS downgrade / cleartext) | ID, Tampering | 3 (Medium) |
| AUTH.T12 | Missing input-shape validation (type confusion via lost schema validation) | Tampering, DoS | 3 (Medium) |

### SDK — SDK/CLI (4 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| SDK.T01 | Credential exposure on developer machines | ID | 6 (High) |
| SDK.T02 | Insecure automation pipelines | Spoofing, ID | 6 (High) |
| SDK.T03 | SDK supply chain attack | Tampering | 3 (Medium) |
| SDK.T04 | Batch processing abuse | DoS | 4 (Medium) |

### HOOK — Lambda Hooks (6 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| HOOK.T01 | Malicious customer code execution | Tampering, EoP | 4 (Critical severity) |
| HOOK.T02 | Data exfiltration via post-processing hook | ID | 8 (Critical) |
| HOOK.T03 | Inference hook result tampering | Tampering | 3 (Medium) |
| HOOK.T04 | Hook Lambda timeout / failure cascade | DoS | 4 (Medium) |
| HOOK.T05 | Privilege escalation via hook IAM role | EoP | 3 (Medium) |
| HOOK.T06 | Preprocessing hook sees raw source document; can halt or replace it | Tampering, ID, DoS | 6 (High) |

### UI — Web UI (7 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| UI.T01 | Cross-site scripting (XSS) | Tampering, ID | 6 (High) |
| UI.T02 | Presigned upload URL abuse | Spoofing, Tampering | 2 (Low) |
| UI.T03 | UI API abuse (REST dispatcher) | Tampering, ID, DoS | 6 (High) |
| UI.T04 | Hosting origin misconfiguration (CloudFront / API Gateway S3 proxy) | ID | 2 (Low) |
| UI.T05 | Client-side configuration exposure | ID | 2 (Low) |
| UI.T06 | Presigned read URLs are bucket-scoped, not key-scoped | ID, EoP | 6 (High) |
| UI.T07 | Security-header / CSP divergence between hosting modes | Tampering, ID | 3 (Medium) |

### RPT — Reporting & Analytics (8 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| RPT.T01 | Reporting data tampering | Tampering, Repudiation | 2 (Low) |
| RPT.T02 | Athena query data exposure | ID | 6 (High) |
| RPT.T03 | Glue catalog manipulation | Tampering | 2 (Low) |
| RPT.T04 | Evaluation data manipulation | Tampering | 3 (Medium) |
| RPT.T05 | Discovery prompt injection via sample docs | Tampering, EoP | 6 (High) |
| RPT.T06 | Test Studio uncontrolled processing costs | DoS | 4 (Medium) |
| RPT.T07 | Ground-truth tampering via the Test Set visual editor | Tampering, Repudiation | 6 (High) |
| RPT.T08 | Extended data retention via document version history | ID | 3 (Medium) |

### FEAT — Feature Platform (4 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| FEAT.T01 | Feature UI bundle executes unsandboxed in the host origin | Tampering, ID, EoP | 8 (Critical) |
| FEAT.T02 | Feature registry enumeration by any authenticated user | ID | 2 (Low) |
| FEAT.T03 | Feature stack IAM privilege and host resource access | EoP, Tampering | 6 (High) |
| FEAT.T04 | Stale or downgraded feature bundle served to users | Tampering, DoS | 3 (Medium) |

### JOB — Jobs API (3 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| JOB.T01 | Jobs API clients bypass the Cognito group RBAC model | EoP | 6 (High) |
| JOB.T02 | Jobs API is outside the automated authorization test harness | EoP (detection gap) | 3 (Medium) |
| JOB.T03 | Static client secret with no rotation mechanism | Spoofing | 3 (Medium) |

### PII — PII Anonymization & Preprocessing (5 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| PII.T01 | Un-redacted document content reaches the detection model | ID | 4 (Medium) |
| PII.T02 | Redaction loop / re-entrancy via input-bucket writeback | DoS | 3 (Medium) |
| PII.T03 | Redaction bypass — un-redacted document processed on hook failure | ID, Tampering | 6 (High) |
| PII.T04 | Redaction mapping table as a re-identification oracle | ID, EoP | 6 (High) |
| PII.T05 | Companion config-version pairing misconfiguration | ID | 3 (Medium) |

### SELL — Seller Entitlement Service (10 threats)

> The only threat set whose protected assets belong to the **seller** (signing
> key, customer roster, revenue) rather than the deploying customer.

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| SELL.T01 | Service deployed into an account that does not own the product | DoS | 6 (High) |
| SELL.T02 | Spoofed buyer identity in the request body | S, EoP | 9 (Very High) |
| SELL.T03 | Resource-policy over-exposure (`Principal: '*'`) | EoP | 6 (High) |
| SELL.T04 | Cost/quota exhaustion by an arbitrary AWS account | DoS | 4 (Medium) |
| SELL.T05 | Signing-key compromise or trust re-pointing | S, Tampering | 8 (Critical) |
| SELL.T06 | Token misuse — replay, sharing, weak verifier | S, Tampering | 4 (Medium) |
| SELL.T07 | Product-existence oracle | ID | 2 (Low) |
| SELL.T08 | Activation-service outage locks out paying customers | DoS | 6 (High) |
| SELL.T09 | Customer roster disclosure | ID | 4 (Medium) |
| SELL.T10 | Allow-list bypass left enabled in production | EoP | 4 (Medium) |

## 3. STRIDE Abbreviations

| Abbreviation | Full Name |
|-------------|-----------|
| **S** | Spoofing |
| **T** | Tampering |
| **R** | Repudiation |
| **ID** | Information Disclosure |
| **DoS** | Denial of Service |
| **EoP** | Elevation of Privilege |
