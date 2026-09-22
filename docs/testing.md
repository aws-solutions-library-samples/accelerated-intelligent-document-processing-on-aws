---
title: "Testing"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Testing

Every test layer and tier in this repository: what it proves, how to run it, whether
CI runs it for you, and where its results are recorded. This is a map of the tiers,
not an index of individual test functions — there are thousands of those across
hundreds of test modules, and one added inside a suite that already runs needs no
change here. `make test-list` enumerates the suites themselves, and the section on
[what `make test` does not run](#suites-make-test-does-not-run) is the one place the
suite-level exceptions are written down. If you are looking for **what a given
release was actually validated against**, that is the
[Release Validation Records](./release-validation/README.md); this page describes the
methods, not any particular run of them.

The organising fact: **most of what protects this repo runs on every pull request,
but the tiers that need a deployed stack cannot.** Roughly a dozen methods below run
only when a person asks for them, which is why each one has a written procedure and a
record of its last run.

## The layers at a glance

| Layer | Needs AWS? | Runs in CI? | Entry point |
|---|---|---|---|
| [1. Offline test suites](#1-offline-test-suites) | no | ✅ both CIs, but through two other targets — see below | `make test` |
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
`scripts/`, with the handful of documented exceptions [below](#suites-make-test-does-not-run).
`make test-list` prints the discovered roots without running them, which is the honest
answer to "is my new suite actually being run?".

```bash
make test                                   # everything, auto-discovered
make test-list                              # which roots were discovered
cd lib/idp_common_pkg && make test-unit     # one root, isolated
make test-integration-all                   # only the integration-marked tests (needs AWS)
```

**`make test` itself runs in neither CI.** CI runs the same suites through two other
targets — `make test-cicd -C lib/idp_common_pkg` and `make test-packages-cicd` — and
the second of those is a hand-enumerated recipe, so a suite that `make test`
discovers but nobody adds to it runs locally and on no pull request. 22 registered
roots holding 506 tests were in exactly that state, including the resolver suites
covering the download allow-list and the discovery upload target.
`scripts/tests/test_src_lambda_tests_in_ci.py` now closes that by deriving both
sides — every directory holding a tracked `test_*.py`, against the paths the two
recipes actually run pytest against — so a root that is in neither is a test failure
rather than a gap somebody finds by hand months later. A suite that genuinely cannot
run in the shared gate goes in the excluded registry [below](#suites-make-test-does-not-run)
with a reason.

Both of those targets run every suite with the AWS environment **removed** — no
region, no credentials, no profile, the shared AWS config file neutralised and the
instance metadata service disabled — because that is what a CI runner supplies. The
wrapper is defined once, in `make/hermetic_aws.mk`, and included by the root
`Makefile` and by `lib/idp_common_pkg/Makefile`; `make test-integration` there
deliberately does **not** use it, because integration tests need real credentials.

A suite that needs a region or placeholder credentials must therefore supply them
in its own `conftest.py`, where `os.environ.setdefault` reinstates them after the
wrapper has taken the machine's real ones away. Inheriting either from the shell
means the suite passes where it was written and fails where it is gated, and there
are two distinct ways that happens:

- **A region.** A Lambda handler the suite imports builds a boto3 client at module
  scope, so importing it needs a resolvable region and botocore raises
  `NoRegionError` during collection without one. A developer machine supplies one
  from the shared AWS config file; a runner supplies nothing.
- **Credentials.** botocore freezes the session's credentials object into a client
  when the client is *constructed*, so a client built at import time with none
  resolvable can never sign — no matter how many credentials appear later, including
  the ones `moto` sets when a fixture starts. A developer box on EC2 resolves
  credentials from the instance metadata service without anyone noticing; a runner
  cannot reach it. The symptom is `AttributeError: 'NoneType' object has no
  attribute 'access_key'` out of `botocore/auth.py`, which does not mention
  credentials at all.

A corollary worth knowing: **anything a test module does to `os.environ` at import
time affects the whole session.** `-m "not integration"` deselects tests *after*
collection has imported their modules, so even a file whose tests never run can
change the environment every later module is imported under.

`scripts/tests/test_offline_suites_are_hermetic.py` checks all of this, and checks
the stripping wrapper itself by measurement: it builds an environment that supplies
a region and credentials through every source, puts it through the wrapper, and
requires that neither survives.

There is **no standing failure set** — **Expected standing failures: 0** on a
correctly installed tree, and the enumerated list of accepted failures in
[`full-test-battery`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/.claude/skills/full-test-battery.md)
is empty. So treat any failure as a real regression until proven otherwise.
`scripts/tests/test_standing_failure_baseline.py` holds that claim, this page and the
two skills that repeat it to the same number, so they cannot drift apart again.

Most surprising failures are still a stale virtualenv missing the pinned `[test]`
extras — but **do not expect a broken install to announce itself as an
`ImportError`.** Several Lambdas catch a missing `idp_common` on purpose and degrade
(`feature-platform/main-stack-extensions/lambdas/apply_feature_config_preset/index.py`
logs at ERROR and applies a config preset without recording a revision), so a
suite that exercises the non-degraded path fails on a bare assertion instead. Two
tests in `test_apply_feature_config_preset.py`
(`test_remove_hands_the_pipeline_back_to_default_then_deletes` and
`test_remove_keeps_an_active_profile_when_there_is_no_default_to_fall_back_to`) were
misread as a standing failure of this repo for exactly that reason. With
`idp_common` unimportable that file reports `2 failed, 18 passed`; with
`PYTHONPATH=<checkout>/lib/idp_common_pkg` exported it reports `20 passed`, on the
same interpreter and the same commit. Measured identically under Python 3.12 and
3.13, so it is not a version incompatibility. Check that
`python3 -c "import idp_common; print(idp_common.__file__)"` resolves inside your own
checkout before reading anything else — an editable install can silently point at a
different, or deleted, checkout. The diagnosis
order, the per-suite expected totals, and how to prove a failure is inherited are in
the [`full-test-battery`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/.claude/skills/full-test-battery.md)
procedure. Conventions for **writing** tests — pytest markers, `moto`, conftest
layout — are in
[`testing-qa`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/.claude/skills/testing-qa.md).

### Suites `make test` does not run

Auto-discovery means a suite cannot be *forgotten*, not that every suite is *run*.
`scripts/run_all_tests.py` refuses to run at all when it finds a directory holding a
`test_*.py` that is in neither of its two registries — the roots it runs, and the
roots it excludes with a written reason — so tests in a new location cannot be
silently skipped. An exclusion covers **only the directory named**: nesting under an
excluded directory used to inherit the exclusion, which meant excluding `scripts`
quietly accepted every future test directory beneath it, so each excluded directory
is now listed on its own. That check backs `make test`, which runs in **neither** CI, so
`scripts/tests/test_testing_doc.py` re-derives it on every pull request, where
`pytest scripts/tests` does run.

These are the directories in the excluded registry. They are listed because a suite
that exists and never runs is otherwise indistinguishable from one that passes:

| Not run by `make test` | Why |
|---|---|
| `scripts` | `scripts/test_api_rbac.py` is the live RBAC harness driven by `make api-test` against a deployed stack (layer 6), not a pytest suite; collecting it picks up its `test_email()` helper as a test |
| `src/lambda/ocr_benchmark_deployer` | `test_local.py` needs `huggingface_hub`, which is not a test dependency |
| `nested/bedrockkb/src/s3_vectors_manager` | One stale assertion, not an environment problem. `conftest.py` in that directory stubs `cfnresponse` (a Lambda-runtime-only module) and supplies a region and placeholder credentials, so `handler.py` imports and four of `test_handler.py`'s five tests pass. The fifth mocks `get_index` and asserts `Status == 'Existing'`; `get_s3_vector_info` no longer consults `get_index` — it always attempts `create_index` and reports `Existing` only on `ConflictException` — so it reports `IndexCreated` and the assertion fails. `scripts/tests/test_run_all_tests_registry.py` computes both halves of that claim, so fixing the test fails the guard and asks for the root to be moved into `RUN_ROOTS` |
| `nested/bedrockkb/src/s3_vectors_manager/tests` | Named separately now that an exclusion no longer covers what is nested under it. Not skipped in practice — `make test-packages-cicd` runs it directly, in both CI systems, so CI runs more than `make test` does |
| `samples/lambda-hook-inference/GENAIIDP-chandra-ocr-hook` | `test_local.py` is a manual local-run script and collects zero pytest tests (measured) |
| `lib/idp_sdk/idp_sdk/_core` | source, not tests: `test_studio_processor.py` is the Test Studio processor module, which the `test_` prefix makes look like a suite |
| `lib/idp_common_pkg/manual_tests/agents` | operator-run scripts, not a suite: each one drives real Bedrock, Athena or DynamoDB against a deployed stack and bills model calls. Run by hand (`python manual_tests/agents/test_analytics.py -q "…"`); `norecursedirs` in `lib/idp_common_pkg/pytest.ini` keeps a bare `pytest` from collecting them |

Adding an exclusion, or lifting one of these, fails that guard until this table and
the registry agree — it is checked in both directions, so a row that outlives the
exclusion it describes fails too.

### A run can measure the wrong checkout, and two suites refuse to

`idp_common` and the SDKs are **editable installs**, so `import idp_common` reads
whatever pointer is in the active interpreter's `site-packages` — not necessarily this
checkout. Where more than one checkout is worked on at once and `python3` resolves to a
shared interpreter rather than a per-project virtualenv, that directory is shared and the
last `pip install -e` wins for all of them. `make test-cicd` is itself a writer: its
`test-unit-cicd` target runs `pip install -e ".[test]"` unless `SKIP_INSTALL=1` is set,
so running the gate repoints the pointer as a side effect.

Nothing raises when this happens. The imported package is real and self-consistent, just
a different revision, so it shows up as an unrelated-looking assertion failure or as a
**green run whose coverage number describes another tree**. `scripts/tests/first_party_provenance.py`
turns that into an immediate, explanatory failure, and is called from
`lib/idp_common_pkg/tests/conftest.py`,
`feature-platform/main-stack-extensions/tests/conftest.py`,
`scripts/tests/test_model_surface_consistency.py` and
`lib/idp_sdk/tests/unit/test_config_operations_region.py`.

It compares **checkout identity**, deriving each side's root by walking up to `.git`, so
a git worktree validates against itself and passes while a worktree *nested inside*
another checkout is correctly refused. Pin a one-off run with
`PYTHONPATH=lib/idp_common_pkg`; fix it durably by installing with the interpreter you
actually want (`<your-venv>/bin/python -m pip install -e "lib/idp_common_pkg[test]"`, by
path and never by bare name — see [dependency-confusion.md](dependency-confusion.md)).
`IDP_ALLOW_FOREIGN_FIRST_PARTY=1` downgrades the failure to a warning when you are
deliberately testing an installed copy; it is a per-invocation switch, so it is not in
`scripts/tests/gate_exemptions.json`, for the reason that file records for
`ALLOW_SHARED_BRANCH`.

## 2. Static gates (lint, types, and hand-written scanners)

`make lint` is the local gate; `make lint-cicd` is the same set in check-only mode
and is what both CIs run. Several members of the set are not linters at all but
hand-written scanners for classes of defect that have each shipped at least once:

| Gate | What it catches |
|---|---|
| `make ruff-lint` · `make format` | Python style and formatting (88 cols, Python 3.12) over every tracked `.py` file except a named per-file debt list |
| `make check-lint-debt` | that debt list staying honest: an excluded file that *gained* a finding, one that is now clean and should be delisted, a dead path, a bare directory name in one of the three generated arrays, or a tracked file ruff's walk never reaches (which is what covers the top-level `exclude` array, whose bare names are deliberate). `ruff.toml` excluded five bare directory names until [#975](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/975), which match at any depth, so 442 of 1230 tracked `.py` files were read by neither gate. Use `python3 scripts/check_lint_debt.py --explain <path>` to ask whether one file is linted — no `ruff` invocation answers that correctly |
| `make cfn-lint` | any file declaring `AWSTemplateFormatVersion` — discovered by **content**, so a new template cannot escape it. Fails on errors only |
| `make check-arn-partitions` | hardcoded `arn:aws:` / `amazonaws.com` instead of `${AWS::Partition}` / `${AWS::URLSuffix}` — GovCloud compatibility |
| `make check-filtered-scans` | DynamoDB `Scan` with a filter expression that cannot see all matches |
| `make check-data-plane-tags` | the `idp:plane=data` tag on the Lambdas that must carry it |
| `make check-markdown-links` | a relative Markdown link that resolves to nothing, an `#anchor` naming a heading that has since been retitled, a **published** `docs/` page linking relatively to one `docs-site/setup.sh` does not symlink (resolves on GitHub, 404s on the site), and a code fence that swallows content, which would otherwise leave this gate blind to the rest of the file. Every tracked `.md` file, from `git ls-files`. Anchors are slugified by `github-slugger`'s rule, which `scripts/tests/test_markdown_links.py` verifies against the real package over every heading in the tree. External `http(s)` URLs are **not** fetched, so a green result says nothing about them ([#1068](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1068)) |
| `make validate-buildspec` | malformed CodeBuild buildspecs — otherwise a deploy-time failure |
| `make codegen-check` | generated GraphQL types drifting from the schema |
| `make typecheck` · `make typecheck-pr` | `basedpyright`; CI checks only files the PR changed |
| `make api-test-static` | an API operation added without authorization, and drift between the dispatcher's generated required-groups manifest and `scripts/api_rbac_expectations.yaml` — see [layer 6](#6-live-stack-tiers-manual) for the live half |
| `python3 scripts/check_first_party_deps.py` | a first-party package in the **current environment** that came from a package index rather than from `lib/`, which on public PyPI is [somebody else's code](./dependency-confusion.md) |
| `scripts/tests/test_doc_install_commands.py` (part of `make test-packages-cicd`) | a `pip install` **documented** in a fenced code block that could resolve a first-party name from an index — a bare name, or a path install missing a sibling the package requires by name. The environment checker above cannot see an instruction nobody has run yet |
| `python3 scripts/sdlc/validate_service_role_permissions.py` | the CloudFormation service role missing a permission the templates need |

Three of these gates guard the **gates themselves**:
`scripts/tests/test_ci_gate_parity.py` fails if a gate runs in one CI and not the
other, or if `lint-cicd` becomes weaker than local `make lint`;
`scripts/tests/test_nested_stack_parameters.py` checks parent-to-nested stack
parameter wiring that `cfn-lint`'s own rule cannot see; and
`scripts/tests/test_lint_debt_gate.py` drives `check-lint-debt` through each
failure mode it claims, because a ratchet nobody has watched fail is not a
ratchet. All three exist because every gap they cover was originally found by
hand, months late.

`make typecheck` reads every tracked `.py` file, which
`scripts/tests/test_pyright_config.py` asserts by deriving the set from
`git ls-files` rather than from a list. Its `include` array named six paths and
reached 432 of 1230 files, and two `NameError`-class defects reached `develop`
through the gap.

### Whether any of this actually blocks a merge

Parity means both CIs *run* a gate. Whether a red gate can *stop* a merge is a
repository setting, and today it does not: **neither `develop` nor `main` has any
branch protection** — and `main` is the default branch and the one releases are cut
from — so every gate in this table is advisory. A pull request can be merged with
all checks red, and because the gate workflows are `pull_request`-only, a direct
push to either branch runs none of them.

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
without it reports those five as **unread**, not as satisfied). One invocation reads
one branch, so answering the question for this repository takes two — add
`BRANCH_PROTECTION_ARGS=--branch=main` for the second. With no token or no network it
exits 0 with an explanation; otherwise its steady-state result here is exit 1 with a
single `not_protected` finding on each branch, which is the expected answer rather
than a regression.

The absence of protection is a **known, accepted residual**, not an open task, and
the decision is recorded in closed
[issue #933](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/933):
enabling classic protection needs repository **admin**, which no contributor and no
CI token here has. Nothing in the repository can substitute, because enforcement is
server-side — a merge taken through GitHub's own Merge button runs no code from this
tree. So the condition for making this a required, blocking check, run with
`--fail-on-skip`, is a repository **setting** changing: either somebody with
repository admin enables protection, or an organization or enterprise owner
publishes a **branch ruleset** targeting these branches, which needs no repository
admin at all. Its own parsing and assertion logic is covered offline by
`scripts/tests/test_check_branch_protection.py`.

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

Each of these needs a deployed stack (or deploys its own), each has a written
procedure, and each is mandatory for a release. One of them also runs in CI: the
dynamic API RBAC matrix is step 12 of the GitLab `integration_tests` deployment
(`scripts/sdlc/codebuild_deployment.py` shells out to `make api-test` against the
stack that job deploys), so it is the one row below that a GitLab pipeline covers —
and, being GitLab-only, the one row a GitHub pull request does not. Note when that
job runs automatically: pushes to `develop` and non-Draft merge requests targeting
it, and in both cases only when the change touches a deploy-affecting path, so a
documentation or `CHANGELOG` change does not exercise it. On any other branch it is
manual. The rest of the rows run only when a person asks.

| Tier | What only a live stack can prove | Command | Procedure |
|---|---|---|---|
| API RBAC — dynamic (also runs in the GitLab `integration_tests` job) | that the **deployed** resolver enforces authorization per Cognito group and configuration-profile scope, not just that the code looks right | `make api-test STACK_NAME=…` | [`api-rbac-test`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/.claude/skills/api-rbac-test.md) |
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
[`scripts/README.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/scripts/README.md#ux-review-and-demo-recorder-ux_recorderpy).

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
