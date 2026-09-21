---
title: "Release Validation Records"
---

# Release Validation Records

One record per release, capturing **every test that needs a real deployed stack** — the tiers
that cannot run in the CI pipeline and therefore do not appear in any build log. Each record
is written once and never overwritten.

If you are evaluating whether to deploy or upgrade to a given release, this is the record of
what was actually exercised against live AWS infrastructure, and what was found.

This directory covers **validating** a release. For the procedure that **publishes** one —
`scripts/aws-release.sh`, the three public buckets, and the failure/recovery paths — see the
[Release Runbook](../release-runbook.md).

| Release | Verdict | Record |
|---------|---------|--------|
| **v0.6.9** | ✅ Ship — 11 of 12 tiers pass (UX review NOT RUN, no browser available); 2 findings in the solution, both fixed — including the optional deployment role being unable to create the solution's first nested stack, found and fixed here | [v0.6.9.md](./v0.6.9.md) |
| **v0.6.8** | ✅ Ship — 18 of 18 tiers pass (typecheck baseline unchanged); 4 findings, none in shipped product code — one account quota, three test-tooling (two false-red gates, one unrepeatable test) | [v0.6.8.md](./v0.6.8.md) |
| **v0.6.6** | ✅ Ship — 14 of 14 tiers pass; 3 findings, none in shipped product code | [v0.6.6.md](./v0.6.6.md) |

<!-- APPEND NEW ROWS ABOVE THIS LINE (newest first). -->

## Producing a record

The whole battery is driven by one request to the assistant — e.g. *"validate the 0.6.8
release"* — via the
[`release-validation`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/.claude/skills/release-validation.md)
skill, which fixes the tier list below, the order, the stacks, the redaction rules, and the
two PRs that carry the results. The per-tier make targets remain runnable on their own.

## What is covered

| Tier | What only a live stack can prove | Make target |
|---|---|---|
| Offline suites, lint, typecheck, dependency audit | — (recorded here for completeness) | `make test` · `make lint-cicd` · `make typecheck` · `make dep-audit` |
| Build + package | the published template lints and validates | `python3 publish.py …` |
| SRT (SAST + deps) | — | `make srt-scan` |
| RBAC static + dynamic | that every API operation's authorization is enforced by the *deployed* resolver, per Cognito group and config-version scope | `make api-test STACK_NAME=…` |
| ZAP DAST | that the deployed API surface has no exploitable HTTP-layer finding | `make stacktest-zap STACK_NAME=…` |
| UX review (browser) | that a person can complete each web-UI flow against this build, and how it feels doing so — the only tier that opens a browser | `make ux-test STACK_NAME=…` |
| Deploy variants (APIGateway GLOBAL / PRIVATE, WAF, Jobs API) | that each hosting/parameter combination actually creates and serves | `make stacktest-hosting-global` · `-waf` · `-hosting-private` · `-jobsapi` |
| Template transforms (`--headless`, `--govcloud`) | that a **transformed** template deploys and processes a document — the only tier that can | `make transform-deploy-test-all` |
| Seller Entitlement Service e2e | that the service deploys into a seller account and refuses correctly | `make stacktest-seller` |
| In-place upgrade (X→Y) | that a customer's existing stack survives `update-stack` without rollback **and keeps working** | see [`test-upgrade`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/.claude/skills/test-upgrade.md) |
| Release benchmark A/B | accuracy / completeness / cost / latency vs the previous published release | `make benchmark-release` |

Every method above, plus the layers that *do* run in CI, is described in
[Testing](../testing.md) — this table is only the live-stack subset.

Note the entry point in the first row. `make test` is the local way to run the offline
suites; the targets a pull request actually runs are `make test-cicd -C
lib/idp_common_pkg` and `make test-packages-cicd`. Run the row as written for a
release, but do not read a green pipeline as having covered it, because the two sets
differ in both directions. `make typecheck` is whole-repository where CI runs
`make typecheck-pr` over the files a branch changes, so that half is genuinely
stricter here. The **test** half is not: every suite `make test` runs is now also in
one of the two CI targets, and CI additionally runs one directory `make test`
excludes. `make test-list` prints the split, and `docs/testing.md` records which
targets a pull request runs.

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
