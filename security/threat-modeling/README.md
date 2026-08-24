# GenAI IDP Accelerator — Threat Model

## Document Information

| Field | Value |
|-------|-------|
| **Version** | 3.1 |
| **Last Updated** | 2026-08-20 |
| **Applies to release** | v0.6.5.dev1 |
| **System** | GenAI Intelligent Document Processing (IDP) Accelerator |
| **Architecture** | Unified (Pipeline + BDA modes), API Gateway REST transport |
| **Methodology** | STRIDE |
| **Total Threats** | 93 |
| **Classification** | Internal |

## Overview

This directory contains the comprehensive threat model for the GenAI IDP Accelerator — a serverless intelligent document processing solution on AWS. The threat model covers the unified architecture, all processing modes, features, extensibility points, and integrations.

### Key Statistics

| Metric | Value |
|--------|-------|
| Threats identified | **93** |
| Critical risk (8–9) | 8 |
| High risk (6–7) | 29 |
| Medium risk (3–5) | 41 |
| Low risk (1–2) | 15 |
| Mitigated | 62 (67%) |
| Partially mitigated | 22 (24%) |
| Open (real gap, needs work) | **5 (6%)** |
| Accepted risk | 4 (4%) |

> **Counts are generated, not hand-maintained.** `deliverables/threat-model.tc.json`
> and the tallies above are produced by
> [`scripts/build_threat_model.py`](scripts/build_threat_model.py), which parses
> the Markdown corpus and joins it with a single curated status table. Run
> `python3 security/threat-modeling/scripts/build_threat_model.py` after adding
> or editing a threat; `--check` fails if the export has drifted. (Before v3.0
> these numbers disagreed across four documents and the JSON export was 25
> threats behind the corpus.)

### Open items requiring action

The five **Open** threats are gaps with no effective control today:

| ID | Threat | Where |
|----|--------|-------|
| CHAT.T03 | Chat streaming Function URL enforces neither RBAC group nor session ownership | [companion-chat.md](feature-threats/companion-chat.md) |
| CHAT.T06 | `/chat/agent` trusts a client-supplied `callerSub` over the SigV4 identity | [companion-chat.md](feature-threats/companion-chat.md) |
| UI.T06 | Presigned read URLs are bucket-scoped, not key-scoped; callable by any authenticated user | [web-ui.md](feature-threats/web-ui.md) |
| UI.T07 | No CSP emitted in `WebUIHosting=APIGateway` (incl. GovCloud) deployments | [web-ui.md](feature-threats/web-ui.md) |
| JOB.T02 | Jobs API is outside the automated authorization test harness | [jobs-api.md](feature-threats/jobs-api.md) |

## Directory Structure

```
security/threat-modeling/
├── README.md                                    ← You are here
├── threat-id-glossary.md                        ← All 83 threat IDs with cross-references
│
├── architecture/                                ← System architecture & data flows
│   ├── system-overview.md                       ← Unified architecture, components, trust boundaries
│   ├── data-flows.md                            ← All data flow diagrams with security analysis
│   ├── pipeline-mode.md                         ← Pipeline mode (Textract/BDA-OCR + models) threats
│   └── bda-mode.md                              ← BDA mode threats
│
├── feature-threats/                             ← Per-feature threat analysis
│                                                  (incl. seller-entitlement-service.md,
│                                                   the only SELLER-side asset owner)
│   ├── agent-analysis.md                        ← Multi-agent AI system threats (AGT)
│   ├── companion-chat.md                        ← Conversational AI + streaming threats (CHAT)
│   ├── mcp-integration.md                       ← MCP / external tool threats (MCP)
│   ├── knowledge-base.md                        ← RAG / knowledge base threats (KB)
│   ├── rbac-authentication.md                   ← Auth & access control threats (AUTH)
│   ├── sdk-cli.md                               ← SDK/CLI programmatic access threats (SDK)
│   ├── lambda-hooks.md                          ← Customer extensibility threats (HOOK)
│   ├── web-ui.md                                ← Frontend & UI API threats (UI)
│   ├── reporting-analytics.md                   ← Analytics, evaluation, test sets, versions (RPT)
│   ├── feature-platform.md                      ← Installable extension threats (FEAT)   [new in 3.0]
│   ├── jobs-api.md                              ← Machine-to-machine Jobs API threats (JOB) [new in 3.0]
│   └── pii-anonymization.md                     ← Preprocessing hook + PII redaction (PII)  [new in 3.0]
│
├── threat-analysis/                             ← Cross-cutting analysis
│   ├── stride-analysis.md                       ← Full STRIDE analysis across all components
│   └── threat-designer-results/
│       └── ai-generated-threats.md              ← AI-assisted threat identification notes
│
├── risk-assessment/
│   └── risk-matrix.md                           ← Complete risk register with scoring
│
├── scripts/
│   └── build_threat_model.py                    ← Generates the JSON export (source of counts)
│
├── deliverables/                                ← Executive deliverables
│   ├── executive-summary.md                     ← Executive-level summary
│   ├── implementation-guide.md                  ← Security controls implementation details
│   ├── security-review-v0.3.15-to-v0.5.5.md     ← Historical review (pre-v0.6; kept for audit trail)
│   └── threat-model.tc.json                     ← Threat Composer export (GENERATED — do not edit)
│
├── Mitigation Report 04252026.md                ← Talos engagement responses
└── Mitigation Updates Incremental.md            ← Talos incremental deltas
```

## Quick Navigation

### Start Here
- **[Executive Summary](deliverables/executive-summary.md)** — High-level overview for stakeholders
- **[System Overview](architecture/system-overview.md)** — Architecture, components, and trust boundaries

### Architecture & Data Flows
- **[Data Flows](architecture/data-flows.md)** — All data flow diagrams with security analysis
- **[Pipeline Mode](architecture/pipeline-mode.md)** — Textract/BDA-OCR + model processing threats
- **[BDA Mode](architecture/bda-mode.md)** — Bedrock Data Automation threats

### Feature-Specific Threats
- **[Agent Analysis](feature-threats/agent-analysis.md)** — SQL injection, code execution, routing manipulation
- **[Companion Chat](feature-threats/companion-chat.md)** — Prompt injection, streaming-transport authorization
- **[MCP Integration](feature-threats/mcp-integration.md)** — Data exfiltration, tool injection, response injection
- **[Knowledge Base](feature-threats/knowledge-base.md)** — KB poisoning, RAG injection, data exposure
- **[RBAC & Auth](feature-threats/rbac-authentication.md)** — Privilege escalation, token theft, authz gaps
- **[SDK/CLI](feature-threats/sdk-cli.md)** — Credential exposure, supply chain, batch abuse
- **[Lambda Hooks](feature-threats/lambda-hooks.md)** — Hook exfiltration, tampering, preprocessing power
- **[Web UI](feature-threats/web-ui.md)** — XSS, presigned URL scoping, REST API abuse, CSP divergence
- **[Reporting & Analytics](feature-threats/reporting-analytics.md)** — Data tampering, Athena exposure, ground truth, versions
- **[Feature Platform](feature-threats/feature-platform.md)** — Third-party UI bundles in the host origin
- **[Jobs API](feature-threats/jobs-api.md)** — M2M OAuth realm outside the group RBAC model
- **[PII Anonymization](feature-threats/pii-anonymization.md)** — Redaction bypass, re-identification oracle
- **[Seller Entitlement Service](feature-threats/seller-entitlement-service.md)** — Seller-side activation endpoint; identity spoofing, key compromise, wrong-account deploy

### Cross-Cutting Analysis
- **[STRIDE Analysis](threat-analysis/stride-analysis.md)** — Full STRIDE across all components
- **[Risk Matrix](risk-assessment/risk-matrix.md)** — Complete risk register with scoring and recommendations
- **[Threat ID Glossary](threat-id-glossary.md)** — All 83 threat IDs with quick reference

### Implementation & Testing
- **[Implementation Guide](deliverables/implementation-guide.md)** — Security controls, configuration, and checklists
- **[Threat Composer JSON](deliverables/threat-model.tc.json)** — Machine-readable export (generated)
- **[Security test results](../test-results/)** — Per-release SRT / ZAP DAST / RBAC snapshots
- **[security/README.md](../README.md)** — What each security test covers and how to run it

## Threat Categories

| Prefix | Category | Count | Highest Risk | Document |
|--------|----------|-------|-------------|----------|
| PM | Pipeline Mode | 8 | Very High (9) | [pipeline-mode.md](architecture/pipeline-mode.md) |
| BDA | BDA Mode | 5 | Medium (4) | [bda-mode.md](architecture/bda-mode.md) |
| AGT | Agent Analysis | 5 | High (6) | [agent-analysis.md](feature-threats/agent-analysis.md) |
| CHAT | Companion Chat | 6 | Very High (9) | [companion-chat.md](feature-threats/companion-chat.md) |
| MCP | MCP Integration | 6 | Critical (8) | [mcp-integration.md](feature-threats/mcp-integration.md) |
| KB | Knowledge Base | 4 | High (6) | [knowledge-base.md](feature-threats/knowledge-base.md) |
| AUTH | Authentication/RBAC | 12 | High (6) | [rbac-authentication.md](feature-threats/rbac-authentication.md) |
| SDK | SDK/CLI | 4 | High (6) | [sdk-cli.md](feature-threats/sdk-cli.md) |
| HOOK | Lambda Hooks | 6 | Critical (8) | [lambda-hooks.md](feature-threats/lambda-hooks.md) |
| UI | Web UI | 7 | High (6) | [web-ui.md](feature-threats/web-ui.md) |
| RPT | Reporting/Analytics | 8 | High (6) | [reporting-analytics.md](feature-threats/reporting-analytics.md) |
| FEAT | Feature Platform | 4 | Critical (8) | [feature-platform.md](feature-threats/feature-platform.md) |
| JOB | Jobs API | 3 | High (6) | [jobs-api.md](feature-threats/jobs-api.md) |
| PII | PII Anonymization | 5 | High (6) | [pii-anonymization.md](feature-threats/pii-anonymization.md) |
| SELL | Seller Entitlement Service | 10 | Very High (9) | [seller-entitlement-service.md](feature-threats/seller-entitlement-service.md) |

## Top Priority Threats

| # | ID | Threat | Risk | Status |
|---|-----|--------|------|--------|
| 1 | PM.T01 | Prompt injection via document content | 9 | Mitigated |
| 2 | CHAT.T01 | Prompt injection via chat messages | 9 | Mitigated |
| 3 | PM.T06 | Configuration tampering | 8 | Mitigated |
| 4 | MCP.T01 | Data exfiltration via MCP tools | 8 | Partially Mitigated |
| 5 | HOOK.T02 | Data exfiltration via post-processing hook | 8 | Partially Mitigated |
| 6 | FEAT.T01 | Feature UI bundle executes unsandboxed in host origin | 8 | Partially Mitigated |
| 7 | CHAT.T03 | Chat streaming Function URL — no group / ownership check | 6 | **Open** |
| 8 | UI.T06 | Presigned read URLs bucket-scoped, not key-scoped | 6 | **Open** |

## Maintaining This Threat Model

1. **Add or edit the threat in its feature/architecture Markdown document** — the
   `### <ID>: <Title>` heading followed by the attribute table is the parsed unit.
2. **Add the ID to [`threat-id-glossary.md`](threat-id-glossary.md)** and to the
   `STATUS` table in [`scripts/build_threat_model.py`](scripts/build_threat_model.py)
   (risk score + mitigation status).
3. **Regenerate the export**: `python3 security/threat-modeling/scripts/build_threat_model.py`.
   It fails loudly on a duplicate ID or on drift between the corpus and `STATUS`.
4. **Update this README's counts** from the script's output.

Consider wiring `build_threat_model.py --check` into CI so the export cannot
silently fall behind the corpus again.

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 3.1 | 2026-08-20 | **Seller Entitlement Service.** Added the `SELL` prefix and [`feature-threats/seller-entitlement-service.md`](feature-threats/seller-entitlement-service.md) (**SELL.T01–T10**) — the first threat set whose protected assets belong to the **seller** (token signing key, customer roster, revenue) rather than the deploying customer, and whose caller is a semi-trusted, internet-reachable buyer account. A separate prefix rather than more `FEAT.*` threats because the trust boundary and the asset owner both differ. Six findings from the accompanying security review were fixed in the same change (crash on hostile input, product-existence oracle, unused KMS grant, missing token `kid`, unbounded body parse, allow-list free-tier mislabelling), plus a reserved-concurrency control. 83 → 93 threats. |
| 3.0 | 2026-07-28 | **AppSec review for v0.6.x.** Corrected controls credited to deleted machinery: UI.T03 (AppSync GraphQL query-depth/introspection limits → REST dispatcher reality), CHAT.T03 (AppSync subscription filters → Lambda Function URL, with newly-identified missing group/ownership checks), UI.T01 CSP (documented `unsafe-inline`/`unsafe-eval` and `https:` script-src as *not* an anti-XSS control). Removed A2I/SageMaker from the HITL flow and trust boundary TB4 (HITL is now a built-in UI portal); removed the AppSync API layer from `system-overview.md`; rewrote all five AppSync sequence diagrams in `data-flows.md`. Added 19 threats for previously-unmodeled surfaces: **FEAT.T01–T04** (Feature Platform / third-party UI bundles in the host origin), **JOB.T01–T03** (Jobs API M2M OAuth realm), **PII.T01–T05** (preprocessing hook + PII redaction), **HOOK.T06** (preprocessing hook power), **PM.T08** (OpenAI GPT-5.x via `bedrock-mantle`), **UI.T06** (presigned-read key scoping), **UI.T07** (CSP divergence by hosting mode), **CHAT.T06** (client-supplied caller identity), **RPT.T07** (ground-truth editor), **RPT.T08** (document version retention). Reconciled counts across all documents (62/64/58 → **83**) and made the JSON export **generated** rather than hand-maintained. 64 → 83 threats. |
| 2.1 | 2026-07-13 | RBAC doc updated to REST-dispatcher architecture (AppSync removed); added AUTH.T07 (config-version scope bypass / fail-open scope lookup) and AUTH.T08 (silently-ignored schema auth directives); documented the automated authorization test harness (`make api-test` / `make api-test-static`) as a control; 62 → 64 threats. **Note:** this update covered `rbac-authentication.md` and the glossary only — the remaining 18 documents were left at v2.0, which v3.0 corrects. |
| 2.0 | 2025-03-19 | Complete rework: unified architecture, removed Pattern 3/SageMaker, added 9 feature-specific threat analyses (agents, chat, MCP, KB, RBAC, SDK, hooks, UI, reporting), expanded from 31 to 62 threats |
| 1.0 | 2024-12-01 | Initial threat model with 3 separate patterns (BDA, Textract+Bedrock, Textract+SageMaker+Bedrock) |
