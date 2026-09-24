# GenAI IDP Accelerator — Threat Model

## Document Information

| Field | Value |
|-------|-------|
| **Version** | 3.8 |
| **Last Updated** | 2026-09-21 |
| **Applies to release** | v0.6.9 |
| **Last reviewed against version** | 0.6.9 |
| **System** | GenAI Intelligent Document Processing (IDP) Accelerator |
| **Architecture** | Unified (Pipeline + BDA modes), API Gateway REST transport |
| **Methodology** | STRIDE |
| **Total Threats** | 99 |
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
| Threats identified | **99** |
| Critical risk (8–9) | 9 |
| High risk (6–7) | 31 |
| Medium risk (3–5) | 44 |
| Low risk (1–2) | 15 |
| Mitigated | 62 (63%) |
| Partially mitigated | 27 (27%) |
| Open (real gap, needs work) | **6 (6%)** |
| Accepted risk | 4 (4%) |

> **Counts are generated, not hand-maintained.** `deliverables/threat-model.tc.json`
> and the tallies above are produced by
> [`scripts/build_threat_model.py`](scripts/build_threat_model.py), which parses
> the Markdown corpus and joins it with a single curated status table. Run
> `python3 security/threat-modeling/scripts/build_threat_model.py` after adding
> or editing a threat; `--check` fails if the export has drifted, and `make
> check-threat-model-currency` runs it in both CI systems.
>
> `--check` also **reads the corpus's own stated counts back** and fails when one
> disagrees with the export, because the export being right has never stopped a
> document being wrong: it carried 99 threats while eight documents said 98, three
> were a release behind on the status tally, and `AUTH.T14` was missing from the
> register and this file's category table while being present in both the corpus
> and the export.
>
> ⚠️ **A pass is not corpus-wide consistency.** The check matches hand-fitted
> phrasings over a named list of documents. It reads: totals, per-status tallies,
> per-severity bands (including mermaid pie slices), per-component rows and their
> column totals, and STRIDE-category rows. It does **not** read counts written as
> words or with a thousands separator, any table column it does not name, or a
> document absent from its list — and several documents' totals hang on a single
> pattern fitted to one sentence, so **rephrasing a sentence that states a count
> can drop it from the check with no signal**. When you edit such a sentence,
> break the number deliberately and re-run to confirm it is still read.

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
| UI.T06 | Object reads are not scoped per **document**; the two per-user scope axes are enforced on the key, document ownership is not modelled, and the document buckets stay readable directly with the browser's own credentials | **issue #1033** (pending) | [web-ui.md](feature-threats/web-ui.md) |
| JOB.T02 | Jobs API is outside the automated authorization test harness | — | [jobs-api.md](feature-threats/jobs-api.md) |

Three further threats are **Partially Mitigated** with the remainder of their
mitigation outstanding, and are called out here because the partial state is easy
to over-read: **AUTH.T16** (authorization is opt-in per resolver — there is no
default deny at the dispatcher; **issue #928**), **AUTH.T15** (divergent
log-redaction denylists across vendored copies; **issue #921**) and **AUTH.T07**
(config-version scope — two consumers now deny a lookup they cannot evaluate, but
**five** named scope-aware resolvers — six source files — still read one as
"unrestricted"; the scope is keyed on an email that can diverge from the row it
should match; what identifier the claims yield to the lookup is not constrained at
the adapter; and Chat-with-Document is unrestricted on the
streaming transport, which forwards no verified per-user caller — that last half
closes with `GAP-07` / **issue #920**).

### What "reviewed" covers

A threat model that claims to have been re-reviewed should say how deeply. In the
v3.2 pass the following documents were re-derived from the templates and source
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

v3.3 is narrower still: a targeted correction to AUTH.T07 and the counts that
follow from it. No document's **Applies to release** row moved, because no
document was re-reviewed.

## Directory Structure

```
security/threat-modeling/
├── README.md                                    ← You are here
├── threat-id-glossary.md                        ← All 99 threat IDs with cross-references
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
- **[Threat ID Glossary](threat-id-glossary.md)** — All 99 threat IDs with quick reference

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
| AUTH | Authentication/RBAC | 16 | High (6) | [rbac-authentication.md](feature-threats/rbac-authentication.md) |
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
| 9 | UI.T06 | Object reads not scoped per document (per-user axes are enforced) | 6 | **Open** (fix pending, #1033) |
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
| 3.8 | 2026-09-21 | **AUTH.T03: the API group floor extended to the operations that lead to document content, after the chain was measured end to end.** A control change, not a re-review — `Last reviewed against version` is unchanged. v3.4 recorded the floor as covering "document-content reads", which was the right category read one operation at a time and the wrong one read as a chain: on a live stack a self-signed-up caller **in no group** went from `listDocumentsDateShard` (200, an object key and its queued time) to `listDocumentVersions` (200, 17,527 bytes — every section's `OutputJSONUri`, every page's `ImageUri`/`TextUri`/`OcrPageDataUri`, the confidence-alert list naming each extracted attribute, and a `ClassReason` reading "employee personal information (name, address, Social Security Number), pay period dates") to `getStepFunctionExecution` (200, 317,614 bytes of execution input and step history), needing no prior knowledge of the deployment at any step ([issue #1033](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1033)). Seven operations move to `ANY_GROUP` — those three plus `getDocumentCount`, `listDocumentsDateHour`, `getChatMessages` and `getCircuitBreakerStatus` — so the posture is 90 explicit group lists, 18 `ANY_GROUP`, 2 `IAM_ONLY`, 8 `ANY`. The last two are not part of the chain and each has its own reason: `getChatMessages` is the only operation of the seven whose existing control fails **open** (`_verify_session_ownership` returns *true* when `CHAT_SESSIONS_TABLE` is unset, and an operator can disable it outright), so the floor is the layer that holds when that check does not, at nil cost because neither session-creating operation admits a groupless caller; and `getCircuitBreakerStatus` returns a `lastError` that carries `Manual pause by <administrator email>: <reason>` after any manual pause, which is an administrator's address served to any authenticated caller. The failure mode being corrected across all of them is the one this corpus keeps meeting: **a justification attached to a set where it is a property of individual members.** Two of the moved operations carried a reason written for a group rather than for themselves — `listDocumentsDateShard` recorded only as "see `listDocumentsDateHour` — same index-partition enumeration, same decision", and `getChatMessages` as "see `listChatSessions` — own-sessions-only ownership scope, same decision" (a different control, as above) — a third such reason sat on `getFinetuningJob`, which needed no tightening but whose premise had to be computed instead of asserted, and two more of the moved operations carried individual reasons that were simply false: `listDocumentVersions` was recorded as needing a key "the caller already has to know", which step 1 supplies, and `getStepFunctionExecution` as gaining nothing from a group floor because per-object checks bound it, when `resolve_allowed_config_versions` deliberately returns *unrestricted* for a caller no UsersTable row matches, which is exactly what self-registration produces. Every remaining `ANY` entry now carries a reason bounded to one operation, including the three not changed on those grounds (`listChatSessions`, whose DynamoDB key condition cannot address another caller's partition; the two fine-tuning reads, platform state — and the premise there is now *computed* rather than asserted, since `getFinetuningJob` had carried the same set-shaped "see `listFinetuningJobs`" reason and the decision rests entirely on one dict literal's key set, which a new test pins). UI.T06's Attack Vector and Likelihood, RPT.T08's residual, [`data-flows.md`](architecture/data-flows.md) and the distribution table in [`system-overview.md`](architecture/system-overview.md) are updated. **The residual that matters is unchanged and is the reason this is not a fix:** the floor gates the API, not the buckets. The same measurement continued past the API — `cognito-identity get-credentials-for-identity` with that groupless token, then `s3api get-object` on the URI from step 2, returned 15,953 bytes of extracted output. Closing that needs group-scoped Identity Pool `RoleMappings` or a narrowed role with a resolver-only read path; the two shapes and their obstacles are in [Identity Pool group scoping](../../docs/planning/identity-pool-group-scoping-plan.md), and #1033 stays open for it. No threat added or retired; no count changed. |
| 3.3 | 2026-09-19 | **AUTH.T07 re-derived from the code; status corrected to Partially Mitigated.** The entry recorded the config-version scope lookup as querying an `EmailIndex`/`SubIndex` GSI, and the register recorded the threat as *Mitigated*. There is no `SubIndex`: the UsersTable declares one GSI, `EmailIndex`, keyed on `email` — the only identifier that joins a Cognito principal to a user row, since the row's own `PK`/`SK` are `USER#<userId>` with `userId` a `uuid4` unrelated to the Cognito `sub`. Chat-with-Document's lookup named the absent index, so every query raised and the surrounding handler read the failure as "unrestricted"; that lookup now queries `EmailIndex` from the verified claims its resolver forwards and denies the turn on any failure to evaluate the scope, and a unit check ties the index it names to the one [`template.yaml`](../../template.yaml) declares. Residuals are now recorded on the entry rather than absent from it, four in all: the scope-aware resolvers still read a failed lookup as unrestricted, and Chat-with-Document is **not** scope-restricted on the streaming Lambda Function URL transport, which forwards no verified per-user caller (`GAP-07`, **issue #920**) — the route reports an explicitly null caller identity and the check stands down with a per-turn log line instead of appearing to have consulted the table. AUTH.T07's Mitigations no longer imply the live `make api-test` scope suite covers chat; it does not, and the processor's unit suite does. Its Residual risk now enumerates **five** fail-open resolvers rather than four — `get_stepfunction_execution_resolver` was missing, though its own docstring names this threat — and adds two residuals that were absent: the scope is keyed on an email that can diverge from the row it should match (see [`docs/external-idp.md`](../../docs/external-idp.md), which reaches the same conclusion), and nothing constrains which identifier a claims set yields to the lookup. **Counts reconciled across the whole corpus** against the generated export, which every document had drifted from by hand: 98 → 99 threats in seven places, Medium 43 → 44, Mitigated 63 → 62, Partially 25 → 27, Spoofing 15 → 16, Elevation of Privilege 31 → 32, and `AUTH.T14` — present in the corpus and the export since v3.2 — added to the register and the glossary, which is where the missing threat had gone. `make check-threat-model-currency` now **reads those prose counts back and fails on a mismatch**; it previously checked only release distance and that the export rebuilds byte-identically, so nothing ever compared a stated count to a computed one. Reported in [issue #970](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/970). |
| 3.4 | 2026-09-19 | **AUTH.T03 mitigation strengthened: the document-content API operations now require an assigned Cognito group.** A control change, not a re-review — `Last reviewed against version` deliberately stays at 0.6.9. The dispatcher's default deny (v3.2, AUTH.T16) made every operation declare its groups but did not decide what those groups should be, and 26 of 118 were declared `ANY`, i.e. authenticated but not vetted: with `AllowedSignUpEmailDomain` set the pool allows self-registration, so a caller could hold a valid token with an empty `cognito:groups` claim and satisfy all 26 ([issue #979](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/979)). Eight document-content reads and three mutations are now declared `ANY_GROUP` — any group `template.yaml` creates, resolved at build time from the `AWS::Cognito::UserPoolGroup` resources rather than written per operation — so the posture is 90 explicit group lists, 11 `ANY_GROUP`, 2 `IAM_ONLY` (which is not a group requirement: those reject every Cognito caller), 15 `ANY`, with each remaining `ANY` entry carrying a recorded reason. The distribution table in [`system-overview.md`](architecture/system-overview.md) and the tally in [`rbac-authentication.md`](feature-threats/rbac-authentication.md) are updated to match. **The corpus was swept for the eleven operation names rather than for the counts**, which is how the rest was found: [`web-ui.md`](feature-threats/web-ui.md)'s UI.T06 described both file reads as `groups: ANY` (its Description, Attack Vector, Mitigations and Open Items are corrected, and the Attack Vector now names the direct-S3 path explicitly, since that is what actually bounds the threat), and [`reporting-analytics.md`](feature-threats/reporting-analytics.md) and [`pii-anonymization.md`](feature-threats/pii-anonymization.md) each attributed a read to "any authenticated user". Both `rbac-authentication.md` and `system-overview.md` also claimed the expectations file records **one** accepted gap; it records two (GAP-02 and GAP-07), and GAP-02 is now scoped to the *resolver* rather than the operation, which does carry a dispatcher group check. `deliverables/security-review-v0.3.15-to-v0.5.5.md` is deliberately untouched: it is a version-pinned review of what was true between those releases, like the snapshots under `security/test-results/`. **Three residuals are unchanged and are stated in AUTH.T03 rather than implied away**, and the third bounds what the first two are worth: a group check is not a per-document check (UI.T06 stays Open); the floor gates the REST route only, so `POST /chat/document` on the chat Function URL still admits a groupless caller (`GAP-07` / AUTH.T14, now declared so that check S9 reports the divergence instead of it disappearing when the declarations were aligned); and **the floor gates the API, not the buckets** — `CognitoIdentityPoolSetRole` attaches one `authenticated` role with no `RoleMappings`, granting every authenticated user `s3:GetObject`/`ListBucket` on the document buckets and `kms:Decrypt` on the key, which the web UI uses by default, so a groupless caller can still enumerate keys through an `ANY` operation and read the object bytes with no resolver in the path. That last point was previously modelled only as UI.T06's key-scoping gap and is now recorded as reachable without any API operation and by a user in no group. No threat added or retired. |
| 3.6 | 2026-09-20 | **AUTH.T07: the config-version scope join moved off the mutable email onto the immutable Cognito `sub`, and PII.T04's record-existence oracle closed.** A control change, not a re-review — `Last reviewed against version` is unchanged. A scope row is now reached by two **disjoint key spaces** on `UsersTable`: a `SUB#<sub>` pointer item read with `GetItem`, tried first, and the `EmailIndex` GSI, tried second. Neither identifier is ever substituted for the other. This narrows AUTH.T07's residual (1): an address that diverges from the row it should match — a case difference, an external-IdP `AttributeMapping` re-applied on every federated sign-in, a self-service change on a mutable pool — used to read as "no row", and "no row" means *unrestricted*, so a divergence silently lifted the restriction. It is narrowed rather than closed: the back-fill matches a Cognito account to its row on the recorded `sub`, the exact address, or the case-folded address, so a row whose address changed beyond case before this shipped is not reached and keeps the email-only exposure — see the rewritten residual (1). A pointer exists **only for a row carrying `allowedConfigVersions`**, which is what makes the preferred leg incapable of widening a scope rather than only of failing to narrow one. The pointer reuses the table's existing `PK`/`SK` key schema rather than adding a GSI, so no deployment takes a table update and there is no index-backfill window in which a fail-closed lookup would deny every scoped caller; `dynamodb:GetItem` was already granted everywhere alongside `Query`, so no IAM changed either. `scripts/tests/test_scope_lookup_fail_closed.py` gains rule **SCOPE4** (an identifier put to the wrong key space — the `SubIndex` shape this threat is named after), counts a `get_item` as a scope read so the new key space is policed rather than invisible, resolves a claim key written as a module constant as well as a literal, and brings `lib/idp_common_pkg/idp_common` into `SCAN_ROOTS` — without which the **canonical** lookup every other consumer imports had no rule applied to it at all. That last change makes `idp_common/testset_scope.py` visible, and it is carried in `PENDING_FIX` with its two rules named; AUTH.T07's residual (4) is rewritten to record that rather than to assert an absence. **PII.T04** recorded, as a residual, that both `/report/{docId}` routes resolved the redaction record *before* the caller's scope, answering 404 for an absent id and 403 for a present one — an existence oracle over the audit table, one bit per request. Both routes now resolve scope first, and an out-of-scope record answers **identically to an absent one**, which is the half that the ordering alone did not cover and that a scoped caller (rather than an unresolvable one) could exercise. Status fields and counts are unchanged: 99 threats, and AUTH.T07 stays *Partially Mitigated* on its remaining residuals. |
| 3.5 | 2026-09-20 | **AUTH.T07 closed at the class, and the limits of the gate that closes it recorded.** The config-version scope lookup is now one fail-closed implementation in `idp_common/config_scope.py` used by every REST consumer: the lookup key comes from the `email` claim alone (an absent claim denies, and issues no query), and an unwired UsersTable, a missing key or any failed `dynamodb:Query` raises rather than reading as "unrestricted". An **empty page still means unrestricted** — scoping is opt-in per user — so the distinction the entry now draws is between an *answer* from the table and a failure to get one. AUTH.T07's Description and Attack Vector are re-derived accordingly, and two routes are added that needed no unresolvable caller at all: `getDocumentCount` was declared scope-filtered while resolving no caller, and `reprocessDocument` gated its whole check on the caller supplying a `version` argument — so omitting it reprocessed any object key in the deployment, and left the document to be re-run under the *globally active* profile, which nothing scope-checks. Both directions are now enforced. Four residuals are recorded rather than absent: the email-versus-`sub` join; which Cognito token types the authorizer admits, which is **inferred from the template and unmeasured**; the class gate's scope — it checks key provenance and failure handling, has no rule about the *matcher*, and does not scan `lib/idp_common_pkg`, which leaves `idp_common/testset_scope.py` unpoliced for the independent `allowedTestSets` axis; and the two Chat-with-Document modules, which that gate **discovers and suppresses** under an entry naming the specific rules, because a concurrent change owns them — a test asserts the suppression is still load-bearing and fails the moment they comply. `pii-anonymization.md` 1.0 → 1.1 in the same pass: PII.T03's "the opposite of the host's AUTH.T07 fail-open defect" was half true, since that handler's own key derivation carried the same substitution; its report list also answered HTTP 200 with an empty row set where a scope lookup failed, which on an audit view is indistinguishable from "nothing was redacted", and now returns the 403 its two sibling routes already returned. No threat added or removed; no count changed. |
| 3.7 | 2026-09-21 | **UI.T06 re-scoped: the object-read path now authorizes the KEY for the two buckets partitioned per user, and the Configuration bucket is off the browser's Identity Pool role.** A control change, not a re-review — `Last reviewed against version` is unchanged. The threat was modelled as a per-document gap, and it was also defeating both **per-user scope axes**, which is a different and sharper failure: `getFileContents`/`getFilePresignedUrl` validated the bucket and never the key, so a config-version-scoped Author could read every other profile's full revision history — `config_revisions/<profile>/<nnnnnn>.json.gz`, prompts and few-shot examples included — and a scoped Annotator could read other test sets' source documents and ground truth. The configuration-revision store appeared nowhere in this corpus at all, which is why neither `web-ui.md` nor `rbac-authentication.md` connected the two. Worse, the revision bodies needed **no API call**: `CognitoAuthorizedRole` granted `s3:GetObject` **and `s3:ListBucket`** on the Configuration bucket to every authenticated user, so the profile names did not have to be known. Both halves are addressed. Key scope is enforced in `_parse_and_validate_uri` — the one function both fields call before any S3 call, chosen because the capability-issuing field must not be servable without it — matching a `config_revisions/<profile>/` key against `allowedConfigVersions` and every Test Set bucket key (`<test_set_id>/…`) against `allowedTestSets`, via the canonical fail-closed lookups, with a non-disclosing 403 and no S3 call on refusal. The Configuration bucket is removed from the authenticated role, which the UI never read directly: the bucket name is referenced nowhere in `src/ui/`, the browser holds no S3 API client, and the sample documents and config library it does display come from resolvers. **AUTH.T03 residual (1) is corrected rather than restated**: "a caller holding any group may read any document that group can see" is true of an unscoped caller and was false of a scoped one, for whom the whole point of the scope is content their group must not see. UI.T06 **stays Open**, and its title changes from "Presigned Read URLs Are Bucket-Scoped, Not Key-Scoped" to "Object Reads Are Not Scoped Per Document" to say what is actually left: documents have no owner or tenant in any record this deployment keeps, so there is no per-user axis for a key check to consult on the Input and Output buckets, and those two stay on the browser's role because the UI genuinely signs their GETs — closing that needs a data-model change and a new read path ([issue #1033](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1033)). `security/README.md`'s coverage table recorded "**No** out-of-scope-key case"; both axes now have one offline (`get_file_contents_resolver/test_key_scope.py`), and the browser's remaining direct-read reach is pinned in both directions by `scripts/tests/test_browser_s3_grants.py` so the residual cannot widen unnoticed. The configuration-revision store is added to the asset table in `data-flows.md`. Scope-enforcing operations 13 → 15; no threat added or retired, no status or risk-band tally changed. |
| 3.2 | 2026-09-17 | **Currency refresh for v0.6.9, and a gate so the next drift is caught.** Re-derived from source: [`system-overview.md`](architecture/system-overview.md) and [`data-flows.md`](architecture/data-flows.md) (five Cognito groups not four; 118 routable operations not 97, with a corrected group distribution; the dispatcher's lack of a default deny; per-bucket encryption and the TLS-deny bucket policies in place of "SSE-S3 / SSE-KMS"; the concurrency admission-control mechanism; deployment-time privilege as trust boundary TB7), plus [`rbac-authentication.md`](feature-threats/rbac-authentication.md), [`companion-chat.md`](feature-threats/companion-chat.md), [`lambda-hooks.md`](feature-threats/lambda-hooks.md) and [`sdk-cli.md`](feature-threats/sdk-cli.md). Added **AUTH.T16** (no default deny at the dispatcher; authorization is opt-in per resolver), **AUTH.T15** (divergent log-redaction denylists), **HOOK.T07** (`onError: fail` is terminal at one of seven hook points) and **SDK.T05** (the shipped deployment service role reaches account administrator). Corrected two over-claims: CHAT.T03's "the caller's Cognito `sub` is derived from the SigV4 identity" (it is an assumed-role session name) and the hook docs' unqualified "`onError: fail` is terminal". Mitigations that depend on an unmerged change are marked **pending** with their issue number (#919, #920, #921, #927, #928) rather than described as present. Metadata is no longer hardcoded in the builder — it is read from this table, which had drifted (export said 3.0/v0.6.3 while this file said 3.1/v0.6.5.dev1) — and a new `Last reviewed against version` field is gated by `make check-threat-model-currency` in both CI systems. Not every document was re-verified; see "What 'reviewed' covers in v3.2". **The dispatcher default-deny threat was first drafted as `AUTH.T14` and renumbered to `AUTH.T16` before publication**, because a concurrent in-review change (PR #954) had already assigned `AUTH.T14` to a different threat — an alternate entry path bypassing an operation's group check on the streaming Function URL — and referenced that identifier from its CHANGELOG entry, from `.claude/skills/api-rbac-test.md` and from a comment in `scripts/api_rbac_expectations.yaml`, in each case beside the coverage-gap id `GAP-07`. (Measured on that branch, those three are the only occurrences outside `security/threat-modeling/`; its code comments name `GAP-07` rather than the threat id.) `AUTH.T14` is therefore **reserved**, not vacant; see the note under the AUTH table in [`threat-id-glossary.md`](threat-id-glossary.md). Three measured counts were corrected in this pass as well (106 of 109 log groups carry `KmsKeyId`, not 108; 17 SQS queues, not 16 or 18; 68 field aliases, not "roughly 55"), and CHAT.T06's recommendation was **withdrawn and replaced** — see that entry. 93 → 98 threats. |
| 3.1 | 2026-08-20 | **Seller Entitlement Service.** Added the `SELL` prefix and [`feature-threats/seller-entitlement-service.md`](feature-threats/seller-entitlement-service.md) (**SELL.T01–T10**) — the first threat set whose protected assets belong to the **seller** (token signing key, customer roster, revenue) rather than the deploying customer, and whose caller is a semi-trusted, internet-reachable buyer account. A separate prefix rather than more `FEAT.*` threats because the trust boundary and the asset owner both differ. Six findings from the accompanying security review were fixed in the same change (crash on hostile input, product-existence oracle, unused KMS grant, missing token `kid`, unbounded body parse, allow-list free-tier mislabelling), plus a reserved-concurrency control. 83 → 93 threats. |
| 3.0 | 2026-07-28 | **AppSec review for v0.6.x.** Corrected controls credited to deleted machinery: UI.T03 (AppSync GraphQL query-depth/introspection limits → REST dispatcher reality), CHAT.T03 (AppSync subscription filters → Lambda Function URL, with newly-identified missing group/ownership checks), UI.T01 CSP (documented `unsafe-inline`/`unsafe-eval` and `https:` script-src as *not* an anti-XSS control). Removed A2I/SageMaker from the HITL flow and trust boundary TB4 (HITL is now a built-in UI portal); removed the AppSync API layer from `system-overview.md`; rewrote all five AppSync sequence diagrams in `data-flows.md`. Added 19 threats for previously-unmodeled surfaces: **FEAT.T01–T04** (Feature Platform / third-party UI bundles in the host origin), **JOB.T01–T03** (Jobs API M2M OAuth realm), **PII.T01–T05** (preprocessing hook + PII redaction), **HOOK.T06** (preprocessing hook power), **PM.T08** (OpenAI GPT-5.x via `bedrock-mantle`), **UI.T06** (presigned-read key scoping), **UI.T07** (CSP divergence by hosting mode), **CHAT.T06** (client-supplied caller identity), **RPT.T07** (ground-truth editor), **RPT.T08** (document version retention). Reconciled counts across all documents (62/64/58 → **83**) and made the JSON export **generated** rather than hand-maintained. 64 → 83 threats. |
| 2.1 | 2026-07-13 | RBAC doc updated to REST-dispatcher architecture (AppSync removed); added AUTH.T07 (config-version scope bypass / fail-open scope lookup) and AUTH.T08 (silently-ignored schema auth directives); documented the automated authorization test harness (`make api-test` / `make api-test-static`) as a control; 62 → 64 threats. **Note:** this update covered `rbac-authentication.md` and the glossary only — the remaining 18 documents were left at v2.0, which v3.0 corrects. |
| 2.0 | 2025-03-19 | Complete rework: unified architecture, removed Pattern 3/SageMaker, added 9 feature-specific threat analyses (agents, chat, MCP, KB, RBAC, SDK, hooks, UI, reporting), expanded from 31 to 62 threats |
| 1.0 | 2024-12-01 | Initial threat model with 3 separate patterns (BDA, Textract+Bedrock, Textract+SageMaker+Bedrock) |
