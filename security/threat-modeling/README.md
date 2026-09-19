# GenAI IDP Accelerator — Threat Model

## Document Information

| Field | Value |
|-------|-------|
| **Version** | 3.4 |
| **Last Updated** | 2026-09-19 |
| **Applies to release** | v0.6.9 |
| **Last reviewed against version** | 0.6.9 |
| **System** | GenAI Intelligent Document Processing (IDP) Accelerator |
| **Architecture** | Unified (Pipeline + BDA modes), API Gateway REST transport |
| **Methodology** | STRIDE |
| **Total Threats** | 98 |
| **Classification** | Internal |

> **`Last reviewed against version` is a gated field.** It records the release
> whose architecture and code this model was last read against — not the release
> it was last *edited* in. `make check-threat-model-currency` (run from
> `make lint-cicd`, so both CI systems execute it) fails the build when that
> value falls more than one release behind the repository's `VERSION`. Bumping
> the field is the *last* step of a re-review, never a way to clear the gate:
> see "Maintaining This Threat Model" below.

## Overview

This directory contains the comprehensive threat model for the GenAI IDP Accelerator — a serverless intelligent document processing solution on AWS. The threat model covers the unified architecture, all processing modes, features, extensibility points, and integrations.

### Key Statistics

| Metric | Value |
|--------|-------|
| Threats identified | **98** |
| Critical risk (8–9) | 9 |
| High risk (6–7) | 31 |
| Medium risk (3–5) | 43 |
| Low risk (1–2) | 15 |
| Mitigated | 63 (64%) |
| Partially mitigated | 25 (26%) |
| Open (real gap, needs work) | **6 (6%)** |
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

The six **Open** threats are gaps with no effective control today. Four of them
have a fix in flight; that fix is **not** a control until its change merges, so
the status here stays *Open* and the issue number is recorded instead. Note the
distinction in the last column: "closes it" and "partial" are different claims,
and a threat model that reports an in-flight change as a resolution is making the
same mistake as one that reports an intention as a control.

| ID | Threat | Fix in flight | Where |
|----|--------|---------------|-------|
| CHAT.T03 | Chat streaming Function URL enforces neither RBAC group nor session ownership | **issue #920** / PR #954 — **partial, does not close it**: no per-user identity and no group claim on this transport, so the remainder stays open (`GAP-07`) | [companion-chat.md](feature-threats/companion-chat.md) |
| CHAT.T06 | `/chat/agent` prefers a client-supplied `callerSub` over the SigV4-derived one | **issue #920** / PR #954 — **partial**: one shared resolver now 403s a contradicting body value, but the body value is still the effective identity when the transport value is the pool-wide session name | [companion-chat.md](feature-threats/companion-chat.md) |
| HOOK.T07 | `onError: fail` does not halt the workflow at six of the seven hook points | **issue #919** (pending) | [lambda-hooks.md](feature-threats/lambda-hooks.md) |
| SDK.T05 | The shipped CloudFormation deployment service role reaches account administrator | **issue #927** (pending) | [sdk-cli.md](feature-threats/sdk-cli.md) |
| UI.T06 | Presigned read URLs are bucket-scoped, not key-scoped; callable by any authenticated user | — | [web-ui.md](feature-threats/web-ui.md) |
| JOB.T02 | Jobs API is outside the automated authorization test harness | — | [jobs-api.md](feature-threats/jobs-api.md) |

Two further threats are **Partially Mitigated** with the remainder of their
mitigation in flight, and are called out here because the partial state is easy
to over-read: **AUTH.T16** (authorization is opt-in per resolver — there is no
default deny at the dispatcher; **issue #928**) and **AUTH.T15** (divergent
log-redaction denylists across vendored copies; **issue #921**).

### What "reviewed" covers in v3.2

A threat model that claims to have been re-reviewed should say how deeply. In
this pass the following documents were re-derived from the templates and source
rather than edited in place, and their **Applies to release** rows read v0.6.9:
[`architecture/system-overview.md`](architecture/system-overview.md),
[`architecture/data-flows.md`](architecture/data-flows.md),
[`feature-threats/rbac-authentication.md`](feature-threats/rbac-authentication.md),
[`feature-threats/companion-chat.md`](feature-threats/companion-chat.md),
[`feature-threats/lambda-hooks.md`](feature-threats/lambda-hooks.md) and
[`feature-threats/sdk-cli.md`](feature-threats/sdk-cli.md).

The remaining documents were **carried forward**: their content was reconciled
for counts, cross-references and any claim contradicted by the six documents
above, but their threat entries were not individually re-verified against source
in this pass, and their **Applies to release** rows still read the release they
were last verified against. That is deliberate — a blanket version bump across
all 24 documents would assert a review that did not happen, which is the failure
mode the currency gate exists to prevent.

## Directory Structure

```
security/threat-modeling/
├── README.md                                    ← You are here
├── threat-id-glossary.md                        ← All 98 threat IDs with cross-references
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
- **[Threat ID Glossary](threat-id-glossary.md)** — All 98 threat IDs with quick reference

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
| AUTH | Authentication/RBAC | 15 | High (6) | [rbac-authentication.md](feature-threats/rbac-authentication.md) |
| SDK | SDK/CLI | 5 | Critical (8) | [sdk-cli.md](feature-threats/sdk-cli.md) |
| HOOK | Lambda Hooks | 7 | Critical (8) | [lambda-hooks.md](feature-threats/lambda-hooks.md) |
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
| 7 | SDK.T05 | Deployment service role reaches account administrator | 8 | **Open** (fix pending, #927) |
| 8 | CHAT.T03 | Chat streaming Function URL — no group / ownership check | 6 | **Open** (fix pending, #920) |
| 9 | UI.T06 | Presigned read URLs bucket-scoped, not key-scoped | 6 | **Open** |
| 10 | AUTH.T16 | No default deny at the dispatcher; authorization is opt-in per resolver | 6 | Partially Mitigated (#928) |
| 11 | HOOK.T07 | `onError: fail` is not terminal at six of seven hook points | 6 | **Open** (fix pending, #919) |

## Maintaining This Threat Model

### Adding or editing a threat

1. **Add or edit the threat in its feature/architecture Markdown document** — the
   `### <ID>: <Title>` heading followed by the attribute table is the parsed unit.
   Watch the table syntax: a row missing its trailing `|` is silently skipped by
   the parser.
2. **Add the ID to [`threat-id-glossary.md`](threat-id-glossary.md)** and to the
   `STATUS` table in [`scripts/build_threat_model.py`](scripts/build_threat_model.py)
   (risk score + mitigation status).
3. **Regenerate the export**: `python3 security/threat-modeling/scripts/build_threat_model.py`.
   It fails loudly on a duplicate ID or on drift between the corpus and `STATUS`.
4. **Update this README's counts** from the script's output.

Steps 2 and 3 are now gated. `build_threat_model.py --check` runs from
`make check-threat-model-currency`, which `make lint-cicd` calls, so both CI
systems verify the committed export still rebuilds byte-identical. This was
recommended here for two releases and not done, and the export duly broke:
AUTH.T13 was added to the corpus with no `STATUS` entry, and every run of the
builder exited on drift until v3.2 repaired it.

### Re-reviewing for currency

The whole corpus describes a moving system, so it has a shelf life. The
`Last reviewed against version` field in the Document Information table above
records the release the model was last *read against*.
`make check-threat-model-currency` fails when that value is more than one release
behind the repository's `VERSION`.

One release is the threshold because zero would red-line `develop` the moment
`VERSION` is bumped to the next `.devN` — which happens at the *start* of a
development cycle, before there is anything to review — while two or more is how
this model came to be six releases stale in the first place. One release means
the gate fires exactly once per release cycle, at a point where there is real
change to review.

When it fires, the remedy is a review, not an edit:

1. Re-derive [`architecture/system-overview.md`](architecture/system-overview.md)
   and [`architecture/data-flows.md`](architecture/data-flows.md) from
   `template.yaml`, `nested/api-resolvers/template.yaml`,
   `patterns/unified/statemachine/workflow.asl.json` and
   `scripts/api_rbac_expectations.yaml` — read the templates, do not patch the
   prose.
2. Check the `CHANGELOG.md` entries for the releases since the recorded version
   for new entry points, new trust boundaries, and controls that were removed or
   replaced. A removed control that a threat still credits is the most damaging
   kind of staleness, because the document then reads as assurance.
3. Update the affected threat entries, the `STATUS` table, and the counts, and
   regenerate the export.
4. **Only then** set `Last reviewed against version` to the current `VERSION` and
   add a Version History row saying which documents were re-verified and which
   were carried forward.

Editing the field alone will clear the gate and is the one thing not to do: it
converts a stale-documentation signal into a silent false assurance, which is
strictly worse than the red build.

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 3.4 | 2026-09-19 | **AUTH.T03 mitigation strengthened: the document-content API operations now require an assigned Cognito group.** A control change, not a re-review — `Last reviewed against version` deliberately stays at 0.6.9. The dispatcher's default deny (v3.2, AUTH.T16) made every operation declare its groups but did not decide what those groups should be, and 26 of 118 were declared `ANY`, i.e. authenticated but not vetted: with `AllowedSignUpEmailDomain` set the pool allows self-registration, so a caller could hold a valid token with an empty `cognito:groups` claim and satisfy all 26 ([issue #979](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/979)). Eight document-content reads and three mutations are now declared `ANY_GROUP` — any group `template.yaml` creates, resolved at build time from the `AWS::Cognito::UserPoolGroup` resources rather than written per operation — so the posture is 90 explicit group lists, 11 `ANY_GROUP`, 2 `IAM_ONLY` (which is not a group requirement: those reject every Cognito caller), 15 `ANY`, with each remaining `ANY` entry carrying a recorded reason. The distribution table in [`system-overview.md`](architecture/system-overview.md) and the tally in [`rbac-authentication.md`](feature-threats/rbac-authentication.md) are updated to match. **Three residuals are unchanged and are stated in AUTH.T03 rather than implied away**, and the third bounds what the first two are worth: a group check is not a per-document check (UI.T06 stays Open); the floor gates the REST route only, so `POST /chat/document` on the chat Function URL still admits a groupless caller (`GAP-07` / AUTH.T14, now declared so that check S9 reports the divergence instead of it disappearing when the declarations were aligned); and **the floor gates the API, not the buckets** — `CognitoIdentityPoolSetRole` attaches one `authenticated` role with no `RoleMappings`, granting every authenticated user `s3:GetObject`/`ListBucket` on the document buckets and `kms:Decrypt` on the key, which the web UI uses by default, so a groupless caller can still enumerate keys through an `ANY` operation and read the object bytes with no resolver in the path. That last point was previously modelled only as UI.T06's key-scoping gap and is now recorded as reachable without any API operation and by a user in no group. No threat added or retired. |
| 3.2 | 2026-09-17 | **Currency refresh for v0.6.9, and a gate so the next drift is caught.** Re-derived from source: [`system-overview.md`](architecture/system-overview.md) and [`data-flows.md`](architecture/data-flows.md) (five Cognito groups not four; 118 routable operations not 97, with a corrected group distribution; the dispatcher's lack of a default deny; per-bucket encryption and the TLS-deny bucket policies in place of "SSE-S3 / SSE-KMS"; the concurrency admission-control mechanism; deployment-time privilege as trust boundary TB7), plus [`rbac-authentication.md`](feature-threats/rbac-authentication.md), [`companion-chat.md`](feature-threats/companion-chat.md), [`lambda-hooks.md`](feature-threats/lambda-hooks.md) and [`sdk-cli.md`](feature-threats/sdk-cli.md). Added **AUTH.T16** (no default deny at the dispatcher; authorization is opt-in per resolver), **AUTH.T15** (divergent log-redaction denylists), **HOOK.T07** (`onError: fail` is terminal at one of seven hook points) and **SDK.T05** (the shipped deployment service role reaches account administrator). Corrected two over-claims: CHAT.T03's "the caller's Cognito `sub` is derived from the SigV4 identity" (it is an assumed-role session name) and the hook docs' unqualified "`onError: fail` is terminal". Mitigations that depend on an unmerged change are marked **pending** with their issue number (#919, #920, #921, #927, #928) rather than described as present. Metadata is no longer hardcoded in the builder — it is read from this table, which had drifted (export said 3.0/v0.6.3 while this file said 3.1/v0.6.5.dev1) — and a new `Last reviewed against version` field is gated by `make check-threat-model-currency` in both CI systems. Not every document was re-verified; see "What 'reviewed' covers in v3.2". **The dispatcher default-deny threat was first drafted as `AUTH.T14` and renumbered to `AUTH.T16` before publication**, because a concurrent in-review change (PR #954) had already assigned `AUTH.T14` to a different threat — an alternate entry path bypassing an operation's group check on the streaming Function URL — and referenced that identifier from its CHANGELOG entry, from `.claude/skills/api-rbac-test.md` and from a comment in `scripts/api_rbac_expectations.yaml`, in each case beside the coverage-gap id `GAP-07`. (Measured on that branch, those three are the only occurrences outside `security/threat-modeling/`; its code comments name `GAP-07` rather than the threat id.) `AUTH.T14` is therefore **reserved**, not vacant; see the note under the AUTH table in [`threat-id-glossary.md`](threat-id-glossary.md). Three measured counts were corrected in this pass as well (106 of 109 log groups carry `KmsKeyId`, not 108; 17 SQS queues, not 16 or 18; 68 field aliases, not "roughly 55"), and CHAT.T06's recommendation was **withdrawn and replaced** — see that entry. 93 → 98 threats. |
| 3.1 | 2026-08-20 | **Seller Entitlement Service.** Added the `SELL` prefix and [`feature-threats/seller-entitlement-service.md`](feature-threats/seller-entitlement-service.md) (**SELL.T01–T10**) — the first threat set whose protected assets belong to the **seller** (token signing key, customer roster, revenue) rather than the deploying customer, and whose caller is a semi-trusted, internet-reachable buyer account. A separate prefix rather than more `FEAT.*` threats because the trust boundary and the asset owner both differ. Six findings from the accompanying security review were fixed in the same change (crash on hostile input, product-existence oracle, unused KMS grant, missing token `kid`, unbounded body parse, allow-list free-tier mislabelling), plus a reserved-concurrency control. 83 → 93 threats. |
| 3.0 | 2026-07-28 | **AppSec review for v0.6.x.** Corrected controls credited to deleted machinery: UI.T03 (AppSync GraphQL query-depth/introspection limits → REST dispatcher reality), CHAT.T03 (AppSync subscription filters → Lambda Function URL, with newly-identified missing group/ownership checks), UI.T01 CSP (documented `unsafe-inline`/`unsafe-eval` and `https:` script-src as *not* an anti-XSS control). Removed A2I/SageMaker from the HITL flow and trust boundary TB4 (HITL is now a built-in UI portal); removed the AppSync API layer from `system-overview.md`; rewrote all five AppSync sequence diagrams in `data-flows.md`. Added 19 threats for previously-unmodeled surfaces: **FEAT.T01–T04** (Feature Platform / third-party UI bundles in the host origin), **JOB.T01–T03** (Jobs API M2M OAuth realm), **PII.T01–T05** (preprocessing hook + PII redaction), **HOOK.T06** (preprocessing hook power), **PM.T08** (OpenAI GPT-5.x via `bedrock-mantle`), **UI.T06** (presigned-read key scoping), **UI.T07** (CSP divergence by hosting mode), **CHAT.T06** (client-supplied caller identity), **RPT.T07** (ground-truth editor), **RPT.T08** (document version retention). Reconciled counts across all documents (62/64/58 → **83**) and made the JSON export **generated** rather than hand-maintained. 64 → 83 threats. |
| 2.1 | 2026-07-13 | RBAC doc updated to REST-dispatcher architecture (AppSync removed); added AUTH.T07 (config-version scope bypass / fail-open scope lookup) and AUTH.T08 (silently-ignored schema auth directives); documented the automated authorization test harness (`make api-test` / `make api-test-static`) as a control; 62 → 64 threats. **Note:** this update covered `rbac-authentication.md` and the glossary only — the remaining 18 documents were left at v2.0, which v3.0 corrects. |
| 2.0 | 2025-03-19 | Complete rework: unified architecture, removed Pattern 3/SageMaker, added 9 feature-specific threat analyses (agents, chat, MCP, KB, RBAC, SDK, hooks, UI, reporting), expanded from 31 to 62 threats |
| 1.0 | 2024-12-01 | Initial threat model with 3 separate patterns (BDA, Textract+Bedrock, Textract+SageMaker+Bedrock) |
