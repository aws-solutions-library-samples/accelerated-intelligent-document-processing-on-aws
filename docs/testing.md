---
title: "Testing"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Testing

Every test method in this repository, what it proves, how to run it, and whether CI
runs it for you. If you are looking for **what a given release was actually
validated against**, that is the
[Release Validation Records](./release-validation/README.md); this page is the map of
the methods themselves.

The organising fact: **most of what protects this repo runs on every pull request,
but the tiers that need a deployed stack cannot.** Roughly a dozen methods below run
only when a person asks for them, which is why each one has a written procedure and a
record of its last run.

## The layers at a glance

| Layer | Needs AWS? | Runs in CI? | Entry point |
|---|---|---|---|
| [1. Offline test suites](#1-offline-test-suites) | no | ✅ both CIs | `make test` |
| [2. Static gates](#2-static-gates-lint-types-and-hand-written-scanners) | no | ✅ both CIs | `make lint-cicd` · `make typecheck-pr` |
| [3. Web UI unit tests](#3-web-ui-unit-tests) | no | ✅ both CIs | `make ui-test` |
| [4. Security scanning](#4-security-scanning-sast-and-sca) | no | ✅ both CIs | `make srt-scan` · `make dep-audit` |
| [5. Integration smoke suite](#5-integration-smoke-suite-ci-only) | **yes** | ⚠️ GitLab only | pipeline `integration_tests` |
| [6. Live-stack tiers](#6-live-stack-tiers-manual) | **yes** | ❌ manual | see the table |
| [7. Benchmarks](#7-benchmarks) | **yes** | ❌ manual | `make benchmark-release` |

`make test` and `make lint` are the two commands to run before opening a pull
request. `make all` is both.

## 1. Offline test suites

`make test` discovers and runs every non-integration suite in the repo — the
`idp_common` library, `idp_cli`, `idp_sdk`, `idp_feature_sdk`, the feature platform,
the per-Lambda suites, the config library, and the repo's own tooling tests under
`scripts/`. `make test-list` prints the discovered roots without running them, which
is the honest answer to "is my new suite actually being run?".

```bash
make test                                   # everything, auto-discovered
make test-list                              # which roots were discovered
cd lib/idp_common_pkg && make test-unit     # one root, isolated
make test-integration-all                   # only the integration-marked tests (needs AWS)
```

CI runs the same suites split across two targets — `make test-cicd -C
lib/idp_common_pkg` and `make test-packages-cicd` — so a suite that exists but is
wired into neither is invisible to CI even though `make test` runs it locally.

There is **no standing failure set**: a correctly installed tree is green, so treat
any failure as a real regression until proven otherwise. Nearly every surprising
failure is a stale virtualenv missing the pinned `[test]` extras. The diagnosis
order, the per-suite expected totals, and how to prove a failure is inherited are in
the [`full-test-battery`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/.claude/skills/full-test-battery.md)
procedure. Conventions for **writing** tests — pytest markers, `moto`, conftest
layout — are in
[`testing-qa`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/.claude/skills/testing-qa.md).

## 2. Static gates (lint, types, and hand-written scanners)

`make lint` is the local gate; `make lint-cicd` is the same set in check-only mode
and is what both CIs run. Several members of the set are not linters at all but
hand-written scanners for classes of defect that have each shipped at least once:

| Gate | What it catches |
|---|---|
| `make ruff-lint` · `make format` | Python style and formatting (88 cols, Python 3.12) |
| `make cfn-lint` | any file declaring `AWSTemplateFormatVersion` — discovered by **content**, so a new template cannot escape it. Fails on errors only |
| `make check-arn-partitions` | hardcoded `arn:aws:` / `amazonaws.com` instead of `${AWS::Partition}` / `${AWS::URLSuffix}` — GovCloud compatibility |
| `make check-filtered-scans` | DynamoDB `Scan` with a filter expression that cannot see all matches |
| `make check-data-plane-tags` | the `idp:plane=data` tag on the Lambdas that must carry it |
| `make validate-buildspec` | malformed CodeBuild buildspecs — otherwise a deploy-time failure |
| `make codegen-check` | generated GraphQL types drifting from the schema |
| `make typecheck` · `make typecheck-pr` | `basedpyright`; CI checks only files the PR changed |
| `make api-test-static` | an API operation added without authorization — see [layer 6](#6-live-stack-tiers-manual) for the live half |
| `python3 scripts/check_first_party_deps.py` | a first-party package installed by bare name, which on public PyPI is [somebody else's code](./dependency-confusion.md) |
| `python3 scripts/sdlc/validate_service_role_permissions.py` | the CloudFormation service role missing a permission the templates need |

Two of these gates guard the **gates themselves**:
`scripts/tests/test_ci_gate_parity.py` fails if a gate runs in one CI and not the
other, or if `lint-cicd` becomes weaker than local `make lint`; and
`scripts/tests/test_nested_stack_parameters.py` checks parent-to-nested stack
parameter wiring that `cfn-lint`'s own rule cannot see. Both exist because every
parity gap they cover was originally found by hand, months late.

### Whether any of this actually blocks a merge

Parity means both CIs *run* a gate. Whether a red gate can *stop* a merge is a
repository setting, and today it does not: `develop` has no branch protection, so
every gate in this table is advisory — a pull request can be merged with all checks
red, and because the GitHub workflows are `pull_request`-only, a direct push to
`develop` runs none of them.

```bash
make check-branch-protection    # reads the live setting via the GitHub API
```

The check derives the expected required-check list by parsing
`.github/workflows/*.yml` for the job names GitHub turns into status-check contexts,
rather than from a hardcoded list that would drift on the next rename. It then
asserts protection is enabled, that every context a PR produces is required, that
stale approvals are dismissed, that force-push and deletion are blocked, that an
approving review is required, and that `enforce_admins` is on — without it an
administrator can push straight past everything else. It also names the contexts
that must **stay** advisory: the docs and dependency-manifest workflows are
path-filtered, and `Test Results` is a check run an action creates behind an `if:`,
so none of them reports on every PR and requiring one would leave a check pending
forever and block every merge.

Three details are worth knowing about what it reads. All eight shared gates are
*steps* inside one job, so they collapse to a single requireable context and share
a single red mark — a required-check failure does not say which of the eight
failed. It reads **both** enforcement mechanisms, classic branch protection and
rulesets, because a branch can be fully governed by a ruleset while the classic
endpoint reports nothing. And it distinguishes "not protected" from "cannot see":
the classic endpoint needs repository **admin** and answers 404 without it, so the
tool cross-checks `GET /repos/{slug}/branches/{branch}`, which carries a
`protected` boolean and is readable with plain `pull` access. `--json` therefore
reports `protected: null` — not `false` — when the state genuinely could not be
determined.

It is opt-in and blocks nothing: it needs network access and a token (`pull` access
is enough to reach a verified answer about whether the branch is protected *and* to
compare the required-check list, because `GET /repos/{slug}/branches/{branch}`
carries a nested `protection.required_status_checks` object at that scope;
`administration:read` is what the other five assertions — approvals, stale-review
dismissal, force-push and deletion blocks, `enforce_admins` — need, and a run
without it reports those five as **unread**, not as satisfied), and it reports "not
protected" until
[issue #933](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/933)
is closed, since enabling protection needs repository **admin**. With no token or no
network it exits 0 with an explanation. Once #933 closes it should become a required,
blocking check, run with `--fail-on-skip`. Its own parsing and assertion logic is
covered offline by `scripts/tests/test_check_branch_protection.py`.

## 3. Web UI unit tests

```bash
make ui-test        # Vitest, jsdom — no browser
make ui-build       # lint + typecheck + production Vite build
```

Both CIs run the Vitest suite. Nothing in this layer opens a browser — that is
layer 6's UX review, and it is the only tier in the repo that does.

## 4. Security scanning (SAST and SCA)

```bash
make srt-scan       # Sample Security Review Tool: SAST over the checkout
make dep-audit      # every pinned Python + Node dependency against OSV (fails on HIGH+)
```

Both run on every pull request in **both** CIs, and neither needs AWS. They cover
different things and one does not imply the other: SRT's `syft` stage builds an SBOM
(inventory only, no vulnerability matching), so dependency CVEs are `dep-audit`'s
job alone.

Findings are triaged in place rather than waved through: bandit findings take a
line-scoped `# nosec <ID> - <reason>`, unreachable advisories go in
`scripts/security/dep_audit_allowlist.json` with a justification, and
security-matrix/Checkov findings go in `scripts/srt/issues.json`. Procedures:
[`srt-security-scan`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/.claude/skills/srt-security-scan.md).

Published, redacted snapshots of the four security tests per release live in
[`security/test-results/<version>/`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/tree/develop/security/test-results),
produced by `make security-results`.

## 5. Integration smoke suite (CI only)

The GitLab pipeline's `integration_tests` job deploys a stack and drives fourteen
numbered steps through it — default config, BDA mode, rule validation, concurrent
batch processing, Test Studio evaluation, agentic extraction on a large table,
single- and multi-document discovery, test comparison, API RBAC, IAM permissions
boundary, and pipeline hooks. It needs AWS credentials, so it is **GitLab-only**: a
change merged through a GitHub pull request has not run it.

Per-step detail, what each step asserts, and how to reproduce a single step by hand
are in [`scripts/sdlc/docs/CI_TEST_COVERAGE.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/scripts/sdlc/docs/CI_TEST_COVERAGE.md).

## 6. Live-stack tiers (manual)

None of these run in CI. Each needs a deployed stack (or deploys its own), each has
a written procedure, and each is mandatory for a release.

| Tier | What only a live stack can prove | Command | Procedure |
|---|---|---|---|
| API RBAC — dynamic | that the **deployed** resolver enforces authorization per Cognito group and configuration-profile scope, not just that the code looks right | `make api-test STACK_NAME=…` | [`api-rbac-test`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/.claude/skills/api-rbac-test.md) |
| Cognito authorization behaviour | that the pre-token group-mapping trigger and client attribute permissions behave as the docs claim — they do not always | `make live-auth-checks` · `make verify-idp-federation` | [`live-auth-checks`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/.claude/skills/live-auth-checks.md) |
| **UX review (browser)** | that a person can actually complete each flow in the web UI, and how it feels doing so — functional pass/fail **plus** usability findings. The only tier here that opens a browser | `make ux-test STACK_NAME=…` | [`ux-test`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/.claude/skills/ux-test.md) |
| ZAP DAST | that the deployed HTTP surface has no exploitable finding | `make stacktest-zap STACK_NAME=…` | [`run-stack-tests`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/.claude/skills/run-stack-tests.md) |
| Deploy variants | that each hosting/parameter combination actually creates and serves | `make stacktest-hosting-global` · `make stacktest-waf` · `make stacktest-hosting-private` (VPC) · `make stacktest-jobsapi` (VPC) — list them with `make stacktest-list` | [`run-stack-tests`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/.claude/skills/run-stack-tests.md) |
| Template transforms | that a **transformed** template deploys and processes a real document — the only tier that can | `make transform-deploy-test-headless` · `make transform-deploy-test-govcloud` · both: `make transform-deploy-test-all` | [`transform-deploy-test`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/.claude/skills/transform-deploy-test.md) |
| Seller Entitlement Service e2e | that the service deploys into a seller account, grants correctly, and **refuses** correctly | `make stacktest-seller` | [`run-stack-tests`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/.claude/skills/run-stack-tests.md) |
| In-place upgrade (X→Y) | that an existing customer stack survives `update-stack` without rollback **and still works afterwards** | `make stacktest-upgrade` (pointer) | [`test-upgrade`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/.claude/skills/test-upgrade.md) |

### The UX review, and recording it

`make ux-test STACK_NAME=…` prepares a throwaway Cognito user and a session, then a
person or the assistant drives the flows in `scripts/ux_flows.yaml` in a real browser
and reports functional pass/fail per flow plus ranked usability findings. It is
deliberately not a script: a shell script can assert that a page returned 200, not
that the page made sense.

The review can also be **recorded as a narrated, captioned video**, so a finding can
be shown to the team instead of re-demonstrated live:

```bash
make ux-record-deps                                    # ffmpeg, ffprobe, boto3, Pillow
./scripts/ux_recorder.py start --stack <STACK> --persona Admin --url-contains cloudfront \
    --say "We start on the Test Studio sets tab."
./scripts/ux_recorder.py mark "Open the annotation queue" --say "We open the queue …"
./scripts/ux_recorder.py stop --say "That ends the review."
AWS_PROFILE=default ./scripts/ux_recorder.py render --voice Ruth   # --dry-run first
```

The recorder attaches a second DevTools session to the tab being driven, captures
frames only when the screen changes, and re-paces the result for a viewer: idle
gaps clamped, paused stretches dropped, narration spoken by Amazon Polly, every
click drawn on the frame that was on screen when it happened. Output — `review.mp4`,
`review.srt`, `segments.json`, `review.md` — lands under gitignored
`scratch/ux-recordings/`. **A recording of a live stack shows real documents and
nothing is redacted: never commit one or attach it to a pull request.** Details:
[`scripts/README.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/scripts/README.md#ux-review-recorder-ux_recorderpy).

## 7. Benchmarks

`make benchmark-release VERSION=… PREV=…` runs the release-vs-release A/B —
accuracy, completeness, cost, latency and confidence calibration against the
previous published release. The wider suite in `benchmarks/` (a config × document-size
matrix with exact ground truth) is what produces the published
[Configuration Guidance](./benchmarking/config-guidance.md) and is the gate for any
change that could move accuracy or cost.

Start at the [Benchmarking Guide](./benchmarking/index.md); read
`benchmarks/matrices/METHODOLOGY.md` before writing down a number, and the
[`run-benchmarks`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/.claude/skills/run-benchmarks.md)
procedure before running one.

## Where results are recorded

A test that leaves no record cannot be cited later, so three of the layers above
write one:

| Record | What it holds |
|---|---|
| [Release Validation Records](./release-validation/README.md) | one file per release, never overwritten: every tier that needs a live stack, its verdict, and what it found |
| [`security/test-results/<version>/`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/tree/develop/security/test-results) | redacted per-test snapshots of SRT, ZAP DAST and RBAC static + dynamic |
| [Release Benchmark Audit Trail](./benchmarking/releases/README.md) | the release-vs-release A/B, with `n` and spread on every claim |

Everything else — raw logs, probe output, UX recordings — stays in gitignored
`scratch/`. Records are public-safe by construction: account ids, VPC/subnet/security-group
ids, API hostnames, pool ids, stack physical ids and local paths are replaced with
placeholders.

## Running the whole battery

One release validation exercises every layer on this page in a fixed order, on real
stacks, and writes the three records above. It is a two-day job, driven by the
[`release-validation`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/.claude/skills/release-validation.md)
procedure — the umbrella over every per-tier procedure linked here. The per-tier
`make` targets stay runnable on their own, which is how you validate a single change
without validating a release.
