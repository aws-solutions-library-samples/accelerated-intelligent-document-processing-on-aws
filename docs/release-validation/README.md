---
title: "Release Validation Records"
---

# Release Validation Records

One record per release, capturing **every test that needs a real deployed stack** — the tiers
that cannot run in the CI pipeline and therefore do not appear in any build log. Each record
is written once and never overwritten.

If you are evaluating whether to deploy or upgrade to a given release, this is the record of
what was actually exercised against live AWS infrastructure, and what was found.

| Release | Verdict | Record |
|---------|---------|--------|
| **v0.6.6** | ✅ Ship — 14 of 14 tiers pass; 3 findings, none in shipped product code | [v0.6.6.md](./v0.6.6.md) |

<!-- APPEND NEW ROWS ABOVE THIS LINE (newest first). -->

## What is covered

| Tier | What only a live stack can prove | Make target |
|---|---|---|
| Offline suites, lint, typecheck, dependency audit | — (runs in CI; recorded here for completeness) | `make test` · `make lint-cicd` · `make typecheck` · `make dep-audit` |
| Build + package | the published template lints and validates | `python3 publish.py …` |
| SRT (SAST + deps) | — | `make srt-scan` |
| RBAC static + dynamic | that every API operation's authorization is enforced by the *deployed* resolver, per Cognito group and config-version scope | `make api-test STACK_NAME=…` |
| ZAP DAST | that the deployed API surface has no exploitable HTTP-layer finding | `make stacktest-zap STACK_NAME=…` |
| Deploy variants (APIGateway GLOBAL / PRIVATE, WAF, Jobs API) | that each hosting/parameter combination actually creates and serves | `make stacktest-hosting-global` · `-waf` · `-hosting-private` · `-jobsapi` |
| Template transforms (`--headless`, `--govcloud`) | that a **transformed** template deploys and processes a document — the only tier that can | `make transform-deploy-test-all` |
| Seller Entitlement Service e2e | that the service deploys into a seller account and refuses correctly | `make stacktest-seller` |
| In-place upgrade (X→Y) | that a customer's existing stack survives `update-stack` without rollback **and keeps working** | see [`test-upgrade`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/.claude/skills/test-upgrade.md) |
| Release benchmark A/B | accuracy / completeness / cost / latency vs the previous published release | `make benchmark-release` |

Two companion records hold the detail this one summarises:

- **Security** — the redacted per-test snapshots (SRT, ZAP, RBAC static + dynamic) live in
  [`security/test-results/<version>/`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/tree/develop/security/test-results).
- **Benchmarks** — the release-vs-release A/B lives in the
  [Release Benchmark Audit Trail](../benchmarking/releases/README.md).

## Redaction

These records are **public-safe by construction**: account IDs, VPC/subnet/security-group
ids, API hostnames, Cognito pool ids, stack physical ids and local paths are replaced with
placeholders (`<ACCOUNT_ID>`, `<VPC_ID>`, `<API_HOST>`, …). Raw logs stay in gitignored
`scratch/`. Never paste a raw probe log into this directory — the same rule the
[security curator](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/.claude/skills/curate-security-results.md)
enforces mechanically.
