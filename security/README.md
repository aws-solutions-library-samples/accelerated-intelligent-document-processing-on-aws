<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->

# Security

This directory is the home for the accelerator's security artifacts, so that
coverage and results are **auditable and easy to review**.

```
security/
├── README.md            ← you are here: coverage, goals, how to run each test
├── threat-modeling/     ← STRIDE threat model, mitigations, per-feature threats
└── test-results/        ← curated, public-safe result snapshots, one folder per release
```

## The four security tests

Each test has a clear goal, an owning script/tool, and an operator skill. Raw
output goes to gitignored `scratch/`/`.srt/`; **curated, redacted summaries are
published under [`test-results/`](./test-results/)** (see that directory's
README for the curation process).

| Test | Goal / coverage | Run with | Owning script | Skill |
|------|-----------------|----------|---------------|-------|
| **SRT** — SAST & dependency scan | Static analysis (Bandit, Semgrep, Checkov), dependency inventory (Syft), and a security-matrix review across the whole repo. Gate = any **HIGH** finding whose `status` is `Open` **or `reopened`** — `reopened` is what SRT records when a finding it had marked resolved/suppressed is detected again, so only `suppressed`/`resolved` are accepted dispositions. | `make srt-scan` (or `make srt`) | `scripts/srt/run.py`; suppression register `scripts/srt/issues.json` | [`srt-security-scan.md`](../.claude/skills/srt-security-scan.md) |
| **ZAP DAST** — dynamic API scan | OWASP ZAP baseline/active scan of the deployed UI API (`POST /op/{field}`), seeded from a generated OpenAPI spec of every operation. Gate = any **High** alert. | `make stacktest-zap STACK_NAME=…` | `scripts/sdlc/run_stacktest.py zapdast`; rules `scripts/sdlc/zap-rules.conf` | [`run-stack-tests.md`](../.claude/skills/run-stack-tests.md) |
| **RBAC static** — authorization scan | Offline (no AWS, CI-safe) cross-check of the API op universe vs. schema `@aws_cognito_user_pools` directives vs. the expectations file, catching drift and missing server-side checks. | `make api-test-static` | `scripts/sdlc/scan_api_rbac.py`; expectations `scripts/api_rbac_expectations.yaml` | [`api-rbac-test.md`](../.claude/skills/api-rbac-test.md) |
| **RBAC dynamic** — live authorization tests | Against a deployed stack: temporary Cognito users (one per group + config-version-scoped Author + a second user for IDOR) exercise every op × every role + unauthenticated + malformed/expired tokens, plus the AppSec mandatory-cases checklist (IDOR, token lifecycle, TLS, input validation, deleted-resource). Gate = any hard failure. | `make api-test STACK_NAME=…` | `scripts/test_api_rbac.py` | [`api-rbac-test.md`](../.claude/skills/api-rbac-test.md) |

### Additionally: Seller Entitlement Service tests

The [Seller Entitlement Service](../feature-platform/seller-entitlement-service/README.md)
has its own security tests, kept **separate from the four above** because it is a
different thing: it deploys into an AWS Marketplace **seller** account rather than
a customer's, and its protected assets (token signing key, customer roster,
revenue) belong to the seller. Its threats are `SELL.T01–T10`.

| Test | Goal / coverage | Run with |
|------|-----------------|----------|
| **Template security (static)** | 16 assertions on the invariants that live in CloudFormation, where handler tests cannot see them and `cfn-lint` only checks syntax: SigV4 required, no `Authorization: NONE`, resource policy pinned to `POST /activate`, throttling + reserved concurrency, marketplace grants read-only, `kms:Sign` only, `dynamodb:UpdateItem` only, asymmetric key, no wildcard key-policy principal, retain policies, SSE/PITR, caching explicitly off, no hardcoded account ids. Every assertion is **mutation-tested**. | `make test-packages-cicd` (per commit in CI) |
| **Payload robustness (fuzz)** | ~60 hostile payloads asserting the handler never raises, never 5xx, and never reflects input: wrong JSON types, deep nesting, CRLF/null-byte log forging, injection and template strings, homoglyphs, format specifiers, huge numbers. | `make test-packages-cicd` (per commit in CI) |
| **Live activation (dynamic)** | Against a deployed stack: unsigned → 403, unentitled → 403 with no internal detail, unknown product byte-identical to unentitled, malformed → 400 not 502, hostile corpus over signed HTTP, and a real signature verification against the published public key. | `python feature-platform/seller-entitlement-service/tests/dynamic_activation_test.py --endpoint … --product-id …` (manual) |

These are **not** part of the curated `test-results/` snapshot: the first two are
ordinary unit suites in the per-commit gate, and the third needs a deployed seller
stack plus a subscribed buyer account. Fold them in if a release ever needs
seller-side evidence.

> **ZAP DAST does not apply to this service, and that is a finding rather than an
> omission.** ZAP cannot SigV4-sign, so every request it sends is refused by the
> `AWS_IAM` authorizer *before the Lambda is invoked* — an active scan would report
> a clean 403 wall and prove nothing about the parser. The payload-robustness
> corpus covers that goal directly instead.

### How the tests relate to the threat model

The RBAC suites map to specific threat IDs in
[`threat-modeling/feature-threats/rbac-authentication.md`](./threat-modeling/feature-threats/rbac-authentication.md)
— e.g. AUTH.T09 (IDOR), AUTH.T10 (token lifecycle), AUTH.T11 (TLS), AUTH.T12
(input-shape validation). SRT and ZAP provide broad SAST/DAST coverage
complementary to the per-feature threat analysis. The full threat register (93
threats) is in
[`threat-modeling/threat-id-glossary.md`](./threat-modeling/threat-id-glossary.md),
and [`threat-modeling/README.md`](./threat-modeling/README.md) lists the currently
**Open** items.

#### Known coverage gaps in the automated tests

The threat model records where these four tests do **not** reach, so a green
gate is not mistaken for full coverage:

| Surface | Covered? | Threat |
|---------|----------|--------|
| UI API `POST /op/{field}` (97 ops × 4 roles) | **Yes** — RBAC static + dynamic | AUTH.T03, AUTH.T08 |
| Chat streaming **Lambda Function URL** (`/chat/*`) | **No** — the harness drives `/op` only | CHAT.T03, CHAT.T06 |
| **Jobs API** (`/jobs`, M2M OAuth realm) | **No** scope-negative test in the gate | JOB.T02 |
| Object-read key scoping (`getFilePresignedUrl`) | **No** out-of-scope-key case | UI.T06 |
| CSP in `WebUIHosting=APIGateway` mode | ZAP scans the API, not that hosting mode's SPA responses | UI.T07 |
| Feature UI bundle integrity | **No** — no SRI/digest check exists to test | FEAT.T01 |
| Seller activation endpoint — auth, IAM, payload robustness | **Yes** — template-security + payload-fuzz suites, per commit | SELL.T02, T03, T05, T07, T09 |
| Seller activation endpoint — deployed stage behaviour | **Manual only** — the live test needs a deployed seller stack | SELL.T02, T07 |
| Seller activation — positive path (token issued and verifies) | **No** — needs product ownership *and* a subscribed buyer account, so it cannot run in a build account | SELL.T06 |
| Buyer-side token verification and grace period | **No** — implemented in the closed-source extension repo, outside this gate | SELL.T06, T08 |

Closing these is tracked in
[`threat-modeling/risk-assessment/risk-matrix.md` §5](./threat-modeling/risk-assessment/risk-matrix.md#5-recommendations).

### Regenerating the threat model export

`threat-modeling/deliverables/threat-model.tc.json` is **generated** from the
Markdown corpus — do not hand-edit it:

```bash
python3 security/threat-modeling/scripts/build_threat_model.py          # regenerate
python3 security/threat-modeling/scripts/build_threat_model.py --check  # CI drift gate
```

## CI gating (where these run automatically)

- **SRT** runs in the GitLab CI `security_review` stage on MRs targeting
  `develop`; the pipeline fails on any open-or-reopened HIGH finding.
- **RBAC static** is CI-safe and runs offline.
- **ZAP DAST** and **RBAC dynamic** need a live stack; they are on-demand
  `make stacktest-*` / `make api-test` targets (see
  [`run-stack-tests.md`](../.claude/skills/run-stack-tests.md) for why they were
  moved out of the always-on pipeline).
- **Seller-service template-security + payload-fuzz** run offline per commit via
  `make test-packages-cicd`, called by the GitHub `developer-tests` workflow. Note
  that workflow previously ran only `lib/idp_common_pkg` plus the UI, so the
  package/Lambda suites — `idp_cli`, `idp_sdk`, `idp_feature_sdk`, the
  feature-platform resolvers, and this service — were **ungated** and ran only on
  developers' machines. The seller-service **live** test is manual (it needs a
  deployed seller stack; see that service's README for why it cannot run in a
  build account).

## Publishing a result snapshot

**One command** runs the tests and curates a public-safe snapshot into
`security/test-results/<version>/`:

```bash
make security-results STACK_NAME=<stack> REGION=<region>   # full (incl. live ZAP + RBAC)
make security-results                                      # offline-only (SRT + RBAC static)
```

**Or ask Claude Code:** *"run security tests and update results"* — it follows
the [`curate-security-results`](../.claude/skills/curate-security-results.md)
skill.

To curate from already-run reports (no re-run):

```bash
python3 scripts/security/curate_results.py --date <YYYY-MM-DD> [--version <label>]
```

See [`test-results/README.md`](./test-results/README.md) for the full runbook
(and the step-by-step breakdown of what the one command does).
