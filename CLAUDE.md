# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

GenAI Intelligent Document Processing (GenAIIDP) is a serverless solution for automated document processing using AWS services. It combines OCR with generative AI to extract structured data from unstructured documents at scale.

The system uses a modular architecture with nested CloudFormation stacks supporting multiple document processing patterns while maintaining common infrastructure for queueing, tracking, and monitoring.

## Build & Development Commands

### Building and Publishing

Build and publish deployment artifacts to S3:

```bash
# Primary build script (recommended)
python3 publish.py <cfn_bucket_basename> <cfn_prefix> <region> [--verbose]

# Example
python3 publish.py idp-1234567890 idp us-east-1

# With verbose output for debugging build failures
python3 publish.py idp-1234567890 idp us-east-1 --verbose

```

The build process:
- Checks system dependencies (AWS CLI, SAM CLI, Docker, Python 3.12+, Node.js 22.12+)
- Builds CloudFormation templates and assets using SAM
- All pattern functions are built within the unified pattern directory
- Uploads artifacts to S3 bucket named `<cfn_bucket_basename>-<region>`

### Code Quality & Linting

```bash
# Run all linting and formatting (includes UI)
make lint

# Fast lint (skips UI lint if unchanged via checksum)
make fastlint

# Python linting only
make ruff-lint

# Python formatting only
make format

# Re-measure ruff's per-file exclusion baseline (part of lint, fastlint, lint-cicd)
make check-lint-debt

# Type checking with basedpyright
make typecheck
make typecheck-stats

# UI linting (checks for changes via checksum)
make ui-lint

# UI build verification
make ui-build

# CI/CD linting (check-only, no modifications)
make lint-cicd

# CloudFormation template validation (fails on ERRORS; warnings counted, not listed)
make cfn-lint

# Same, but list every advisory warning in full
make cfn-lint-warnings
```

**The Python lint gates read every tracked `.py` file, and what they skip is a
named list of files rather than a directory.** `ruff.toml` used to exclude five
**bare directory names** — `notebooks`, `options`, `patterns`, `src`, `scripts` —
and a bare name in ruff's exclusion patterns matches at **any** path depth, so
`src` also excluded `nested/*/src` and `patterns/*/src`. 442 of 1230 tracked `.py`
files were read by neither `ruff check` nor `ruff format`, including all 138 files
under `scripts/` (this repository's own gate layer) and 76 under `nested/*/src/`
that nobody had counted. A clean `ruff check` on one of them meant the file was
never opened. Issue #975.

The exclusions are now per-file, generated from `scripts/lint_debt.json`, and
ratcheted: `ruff.toml`'s `[lint] exclude` names 85 files holding 196 pre-existing
findings, `[format] exclude` names 184 files `ruff format` has never run over, and
`make check-lint-debt` (in `lint`, `fastlint` **and** `lint-cicd`, so both CIs)
re-measures every tracked file with the exclusions bypassed. It fails if a listed
file *gained* a finding, if a listed file is now clean and should be delisted, if a
listed path is gone, if one of the three **generated** arrays grows a bare
directory name, or if ruff's walk misses a tracked file no `scope` entry accounts
for. That last check is what covers the top-level `exclude` array, which is bare
directory names **on purpose** (a `build/` at any depth is build output) and so is
deliberately outside the bare-name check — #975 would otherwise be re-openable
through it with every other check green.

⚠️ **To find out whether a given file is linted, run `python3
scripts/check_lint_debt.py --explain <path>`. Do not ask ruff.** Every ruff-native
probe misreports at least one class of file: a plain `ruff check <path>` bypasses
the exclusions, and `--force-exclude` restores only the *discovery* ones, so
`ruff check --force-exclude <path>` prints `All checks passed!` and exits 0 for all
85 lint-excluded files. `ruff check --show-files` does not honour `[lint] exclude`
either. A misleading probe is the stated reason #975 survived inspection.

Pay a file down by fixing its findings and running `python3
scripts/check_lint_debt.py --write` — for a formatting entry spell the path out,
`ruff format <that path>`, because bare `ruff format` honours the exclusion and
skips the file you are fixing. Never hand-edit either array. `--write` **refuses to
grow** either list, naming the paths, unless given `--allow-new-debt "<reason>"`,
which records the reason in the baseline; without that refusal `--write` would
launder a brand-new finding into a permanent exclusion. `--summary` prints the
current split. Two `extend-exclude` entries are scope decisions rather than debt —
the vendored `pii-anonymizer` tree and `**/*.ipynb` — and each carries a premise
the gate evaluates against the tree.

The **formatting** debt is deliberately unpaid: `ruff format` over those 184 files
is a mechanical, conflict-generating sweep that belongs in its own change.

`basedpyright` covers **every tracked `.py` file**, and the property to rely on is
the identity rather than the number: `git ls-files '*.py' | wc -l` and
`filesAnalyzed` agree exactly, which is what
`scripts/tests/test_pyright_config.py` asserts by deriving the closure from `git
ls-files` — so a new tree holding Python fails there rather than being silently
uncovered.

**No count is quoted here on purpose.** The identity is the durable fact and any
figure is stale within a day: it moved four times (1,314 → 1,318 → 1,330 → 1,333)
across four `develop` merges during a single change, and three documents were
carrying three different wrong numbers when that was noticed. Measure it:

```bash
git ls-files '*.py' | wc -l          # must equal basedpyright's filesAnalyzed
```

(For scale, `include` once named six paths and reached 432 of the 1,230 tracked at
that time — a historical measurement, not a current one.)

⚠️ **Reading every file is not checking every call.** `basedpyright` honours
`PYTHONPATH`, and `make typecheck` and both CIs invoke it without one; with
`reportMissingImports` at `"none"`, `idp_common` did not resolve and **no call into
the shared library could produce a diagnostic** — the boundary most of this
repository uses to reach its own core. `filesAnalyzed` was 1,314 either way, so the
gate read every file, matched `git ls-files`, reported zero errors, and proved far
less than that looks like. Eleven errors were behind it, three of them statements
that raise on every execution (a `Status.ERROR` that is not in the enum, a keyword
no parameter matches, a required argument omitted). Issue #1109.

`pyrightconfig.json`'s **`extraPaths`** now names the five first-party package
roots, so resolution is a property of the configuration rather than of how the gate
was invoked. The entries are **relative** on purpose: pyright resolves them against
the directory holding the config, so they cannot name another checkout — which
matters because this environment carries editable installs of `idp_common` and
`idp_sdk` pointing at a sibling worktree and at a different project entirely
(#1094), and an `extraPaths` naming one of those resolves perfectly while saying
nothing about this tree. Confident wrong answers are worse than silent ones.
`test_pyright_config.py` asserts both halves — the entries stay relative and inside
the repo, and a 0.5s live basedpyright probe confirms all five actually resolve
through the values the config carries — and fails on the specific combination of
`reportMissingImports: "none"` plus unresolvable first-party packages.

`reportMissingImports` **stays** at `"none"`, measured rather than assumed: raised to
`"warning"` with `extraPaths` live it reports 44 findings over 25 modules and **none
is first-party**. About 38 are sibling-module imports in script and Lambda trees that
are not packages (`from index import ...`, `processors.pdf_image_processor`), correct
at runtime because the handler's own directory is on `sys.path`; the rest are
genuinely uninstalled third-party distributions. So the rule is unusable above
`"none"` here, and the gap it leaves is covered by the resolution assertions instead.

⚠️ **basedpyright has no ignore-file support**, so its walk reads gitignored build
output — the one asymmetry with ruff, which honours the ignore file natively. `exclude`
is the only mechanism available, so the same file asserts the other direction too: the
walk must reach **nothing an ignore rule covers**, and a tree that appears inside it
fails there naming the directory. Two staged copies of `lib/idp_common_pkg` under
`feature-platform/idp-data-generator/` are excluded on that basis, one entry each in
`STAGED_BUILD_OUTPUT_EXEMPT` with a premise `gate_premises.vcs_ignored_build_output`
computes per path. Before that check existed those copies put 20 errors on
`make typecheck` for anyone who had packaged that feature locally, and none in CI.

**`make cfn-lint`** discovers templates by **content** (anything declaring
`AWSTemplateFormatVersion`), not by filename, so a new template cannot be added
without being covered. `make check-arn-partitions` uses the **same** discovery
(`scripts/discover_templates.sh cfn`) and both targets fail outright
if it returns nothing, so the two gates see the same set — 30 templates today. The
hardcoded glob list that once missed `nested/`, `samples/`, `notebooks/`, `scripts/`
and `iam-roles/` is gone; that directory list survives only as the historical note in
the Makefile comments. **Both gates now scan all 30 templates: no template is skipped
at path scope by either.** The ARN gate's one carve-out, `ARN_PARTITION_EXEMPT`, is
per **line**: entries are `<path>:<line-pattern>` (the shape
`scripts/sdlc/retired_services.json` uses), and the single entry today hides the two
statements in `scripts/sdlc/cfn/credential-vendor.yml` that trust a named role in the
commercial CI account — cross-partition IAM trust does not exist, so those two cannot
be parameterised. Everything else in those templates now is. `cfn-lint`
exempts nothing at **path** scope: no template is skipped. It does exempt specific
*rules*, which is a different axis — it runs with `--ignore-checks
$(CFN_LINT_IGNORE)` (E3043 disabled repo-wide, see below) and E1161/E3031 are
suppressed at resource scope on three layer resources in `template.yaml`. Both
targets run from `lint`, `fastlint` **and** `lint-cicd`, so local and CI gate sets
match.

It fails on **errors only**: ~112 pre-existing warnings (empty-string parameter
defaults, unreachable `Fn::If` branches) would otherwise have to be suppressed
wholesale. Those warnings are **counted but not listed** by default — printing
them buried the one line that matters — so `make lint` shows a single per-rule
tally (`W1030 x88, ...`) and `make cfn-lint-warnings` (or
`CFN_LINT_SHOW_WARNINGS=1`) prints them in full. The rules stay enabled: a
genuinely malformed hardcoded id or ARN is still detected, and every `E*` line is
always printed. The six `<ARTIFACT_BUCKET_TOKEN>` findings are suppressed at
**resource** scope via `Metadata: cfn-lint:` on the three layer resources in
`template.yaml` — not by disabling E1161/E3031 repo-wide, which would have hidden
a genuinely malformed name anywhere else. `publish.py` substitutes those tokens.

The linter is **pinned** (`CFN_LINT_VERSION` in the Makefile, mirrored in both CI
configs and asserted by `scripts/tests/test_ci_gate_parity.py`). An unpinned
linter on a blocking gate red-lines the branch whenever a release promotes a check
to ERROR class, with no code change.

**E3043** (parent's `Parameters` vs the nested stack's) is disabled: `TemplateURL`
points at `.aws-sam/packaged.yaml`, a build artifact, so the rule is skipped
entirely in CI and reports false positives against a stale copy locally — noise in
both. `scripts/tests/test_nested_stack_parameters.py` asserts that wiring directly
against the **source** templates instead, and also covers the reverse direction
(a required nested parameter the parent never passes) that E3043 ignores.

### Every gate exemption is registered — `scripts/tests/gate_exemptions.json`

**If you turn a gate off for anything, you register it.** Adding an exemption list
without a registry entry fails
`scripts/tests/test_gate_exemption_registry.py::test_every_discovered_exemption_is_registered`,
and the failure names your constant and tells you what to write.

The reason is a defect class that has shipped repeatedly here: **one justification
attached to a set, where the justification is a property of individual members.**
Four exemption lists stated a premise that was false for at least one member, and in
each case the false member was the one the gate most needed to see — a nested stack
exempted as independently deployed, a Lambda tree exempted as built separately that
the publisher builds in the same run, a `LogLevel` exclusion resting on an installer
manifest one excluded directory does not have, four templates exempted for naming a
commercial-only principal one of which contains no ARN at all. Read in aggregate
("does this reason hold broadly?") all four pass. That is why reading them did not
catch them.

So:

- **One entry, one member's worth of reason.** Never exempt a directory where a file
  will do, or a file where a line will do. `ARN_PARTITION_EXEMPT` entries are
  `<path>:<line-pattern>`; `scripts/sdlc/retired_services.json` uses the same shape.
  Bounding a reason to one file is what makes the mismatch show up while you are
  writing it rather than in an audit later.
- **If the premise is computable, compute it.** The predicates live in
  `scripts/tests/gate_premises.py` — `not_a_nested_stack_of_parent`,
  `built_separately_from_main_stack`, `file_absent_or_untracked`,
  `installer_manifest_pins_parameter` — each taking **one** member and returning a
  verdict. Name the predicate in your registry entry and parametrise your gate over
  the members; a named predicate the gate never calls is itself a test failure.
- **If it genuinely is not computable, say `JUDGEMENT` and write the reason.** That is
  a legitimate answer (a foreign account's partition, another assistant's
  capabilities, an acknowledged backlog). It is not an exemption from scrutiny: the
  ratchets still apply.
- **Give it a ratchet.** *Non-vacuity* — it must currently shield at least one finding,
  or it is dead and pre-exempting whatever next occupies the path. *Count pinning* —
  store how many sites it shielded when written, so a new site inside an exempt tree
  still fails. *Universe closure* — derive the universe and fail if any member is in
  neither the enforced nor the exempt set; this is what makes an exemption list
  trustworthy at all. *Staleness* — a dead entry fails.
- **If it can have none, say what is unprotected** in `ratchetGap`. Those are the
  honest residuals and they are counted: `MAX_UNRATCHETED` in the meta-test may shrink
  and not grow, so declaring a gap cannot quietly become the default answer.

Membership is **derived** and only the judgement is authored:
`scripts/tests/exemption_discovery.py` finds exemption surfaces by constant name, by
the prose of the attached comment (a constant whose comment argues for an exclusion is
one, whatever it is called), in the `Makefile` and `make/*.mk`, in `scripts/*.sh`, in
`ruff.toml` and `pyrightconfig.json`, and in the three JSON baselines. It reads
**source**, not imported modules, because two of these constants change after import.
It discovers through `git ls-files`, so it cannot report findings against build output
or a sibling worktree. The meta-test fails in **both** directions — unregistered, and
registered-but-vanished.

### CI parity between GitHub and GitLab

GitLab and GitHub now run the **same** non-integration gates. Integration tests
(`integration_tests`) remain GitLab-only, as they need AWS credentials.

Historically several gates ran on GitLab only, so a change merged via a GitHub PR
skipped them — the same class of gap as the SRT/dep-audit note below. Now on both:
`make lint-cicd` (which itself covers `cfn-lint`, `validate-buildspec`,
`check-arn-partitions`, filtered-scan and data-plane-tag checks),
`make typecheck`, `make api-test-static`, `make test-cicd -C lib/idp_common_pkg`,
`make test-packages-cicd`, the UI vitest suite,
`scripts/check_first_party_deps.py` and
`scripts/sdlc/validate_service_role_permissions.py`.

The type gate in both CIs is the **whole-tree** `make typecheck`, not
`make typecheck-pr`. The PR-scoped form is a developer convenience and is in
**neither** CI — a file-scoped check passes on a signature change whose broken caller
sits in a file the diff did not touch, which is the ordinary shape of a type error.
`scripts/sdlc/tests/test_typecheck_pr_changes.py::test_neither_ci_config_runs_this_script`
asserts that, so naming it here as a CI gate would be a document contradicting a
passing test; both CI configurations mention it only in comments recording that the
whole-tree form replaced it.

`make cfn-lint` and `make validate-buildspec` were in **neither** CI before — they
sat in `lint`/`fastlint` but not `lint-cicd`, so a template or buildspec error
could reach deploy time. (Narrow exception: one unit test,
`test_govcloud_pattern_template.py`, already ran cfn-lint against a single
template asserting only zero E3006.)

**`scripts/tests/test_ci_gate_parity.py` enforces this.** It fails if a gate
appears in one CI and not the other, if `lint-cicd` becomes weaker than local
`make lint`, or if the cfn-lint pin drifts between the Makefile and either CI
config. Every parity gap listed above was found by hand, months late, because
nothing checked.

⚠️ **Two asymmetries remain by design.** GitLab runs `code_checks` on **every
push** as well as MRs; GitHub's workflows are `pull_request`-only, so a direct push
to `develop` runs nothing on GitHub.

### Visible is not blocking — `make check-branch-protection`

Parity between the two CIs only means both *run* the gates. Whether a red gate can
actually stop a merge is a **repository setting**, not anything in this tree, and
today it does not: **neither `develop` nor `main` has any branch protection**, so
every gate above is advisory. A pull request can be merged with all checks red.
`main` matters as much as `develop` here — it is the repository's default branch
and the one releases are cut from — and one invocation reads one branch, so
answering the question takes two:

```bash
make check-branch-protection                                       # develop
make check-branch-protection BRANCH_PROTECTION_ARGS=--branch=main   # main
```

Do not take the state on trust from this file. Those commands read the live
setting via the GitHub API.

The command derives the expected required-check list by **parsing**
`.github/workflows/*.yml` for job names (a hardcoded inventory would drift the
moment a job is renamed), then asserts against the live API that protection is on,
that every check a PR produces is required, that stale approvals are dismissed,
that force-push and deletion are blocked, that an approving review is required,
and that `enforce_admins` is on. It also reports which contexts must stay
advisory: `build-docs.yml` and `generate-dep-manifest.yml` are path-filtered, and
`Test Results` is an action-created check run behind an `if:`, so requiring any of
them would leave a check pending forever and block every merge.

Three things about what it reads. All eight shared gates are *steps* in one job
(`developer_tests`), so they are **one** requireable context sharing one red mark,
not three and not eight. It reads classic branch protection **and** rulesets,
because a branch can be governed entirely by a ruleset while the classic endpoint
reports nothing. And it separates "not protected" from "cannot see": the classic
endpoint needs repository admin and answers 404 without it, so `GET
.../branches/<branch>` (readable with `pull`) is cross-checked, and `--json`
reports `protected: null` rather than `false` when the answer is genuinely
unknown.

It is **opt-in and non-blocking on purpose**: it needs network access and a token
(`pull` suffices for a verified answer and for the required-check comparison, which
comes from the nested `protection.required_status_checks` object on
`GET .../branches/<branch>`; `administration:read` is what the other five
assertions need, and without it those five are reported **unread** rather than
satisfied), and on this repository it reports "not protected" on both branches, so
in `lint-cicd` it would red-line every branch for a condition nobody in the tree
can fix. It is therefore in neither `lint-cicd` nor `test_ci_gate_parity.py`'s
`SHARED_GATES`, and `scripts/tests/test_check_branch_protection.py` fails if it is
added to either or invoked from either CI configuration. With no token or no
network it exits 0 with an explanation; `--fail-on-skip` turns that into an error,
which is how to run it once it is a blocking gate. Its steady-state result today is
**exit 1 with one `not_protected` finding** per branch — the expected answer, not a
regression.

**The absence of protection is a known, accepted residual, not an open task.**
Enabling classic protection needs repository **admin**, which no contributor and no
CI token here has, so it cannot be done from the tree or from tooling; the decision
to stop pursuing it from inside the repository is recorded in closed
[issue #933](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/933).
Cite that issue as the decision record; do not treat it as pending. **Nothing in
this repository can substitute**, and that is structural rather than a matter of
effort: enforcement is server-side, so a merge taken through GitHub's own Merge
button runs no code from this tree and no hook, script or gate here can turn a red
check into a refused merge.

The trigger for making this a required, blocking gate is therefore a repository
setting changing, by one of two routes that are **not the same permission**: either
somebody with repository **admin** enables classic branch protection, or an
organization or enterprise owner publishes a **branch ruleset** targeting these
branches. The second needs no repository admin, and the mechanism is demonstrably
available here — the repository already inherits five enterprise rulesets, four
`target=repository` and one `target=tag`, none of which targets a branch. Neither
route is actionable from this tree, and neither announces itself: running the
command is how either would be noticed.

What the tree *can* do, and does, is make a direct write to a shared branch a
deliberate act on the machine it is typed on — see
[the shared-branch guard](#the-check-shared-branch-guard) below. Read it against
the paragraph above rather than as a weaker form of it: it refuses an accidental
`git commit`, `git push` or red `gh pr merge` from a checkout, and it changes
nothing about what GitHub will accept.

### Testing

**Every test layer and tier in this repo — what it proves, its `make` entry point,
whether either CI runs it, and where its results are recorded — is mapped in
[docs/testing.md](docs/testing.md)** (published). That page is a map of tiers, not an
index of test functions: there are thousands of those across hundreds of test modules,
and a method added inside a suite that already runs correctly needs no page edit.
`scripts/tests/test_testing_doc.py` enforces the **mechanical** part of that — every
`stacktest-*` and `transform-deploy-test-*` target and each layer's named entry point
appears on the page, every `make` target and link the page cites resolves, every
directory holding a `test_*.py` is registered in `scripts/run_all_tests.py`, and every
suite that registry excludes from `make test` is named on the page, in both
directions. What each tier *proves*, whether either CI runs it, and where its results
are recorded are prose and are **not** checked — do not read a green gate as
confirming those. It also fails if this paragraph, or any other document pointing at
the page, goes back to promising per-method coverage in one of the literal phrasings it
matches (a paraphrase would get past it), because the previous wording did and nothing
noticed ([#986](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/986)).
The per-tier procedures stay in
`.claude/skills/`, listed in the skill table below; pipeline-internal detail stays in
`scripts/sdlc/docs/CI_TEST_COVERAGE.md`.

```bash
# Run every non-integration suite (auto-discovered; see scripts/run_all_tests.py).
# Note this does NOT run the SRT security scan - that is `make srt-scan`, and it is
# in neither the lint nor the test gate set, so Bandit findings surface only in CI.
make test

# Run tests in idp_common_pkg only
cd lib/idp_common_pkg && make test

# Run unit tests only
cd lib/idp_common_pkg && make test-unit

# Run integration tests (requires AWS resources)
cd lib/idp_common_pkg && make test-integration

# Run idp_cli tests
cd lib/idp_cli_pkg && python -m pytest -v

# Run specific test markers
pytest -m "unit"
pytest -m "integration"
```

### Security Scanning

The project includes automated security scanning with the [Sample Security Review Tool (SRT)](https://github.com/aws-samples/sample-security-review-tool):

```bash
# Run full SRT workflow (setup → scan → optional fix)
make srt

# Or run individual steps:
make srt-setup     # Download and configure SRT
make srt-scan      # Run security assessment
make srt-fix       # Interactive fix mode
```

**CI/CD Integration:**
- SRT runs on every push and MR in GitLab CI (`srt_security_review`, `fast_checks`)
  **and** on every GitHub pull request (`.github/workflows/security-checks.yml`).
  A change merged on GitHub used to skip it entirely — see the note in that
  workflow. ⚠️ Being visible is not being blocking: run
  `make check-branch-protection` to see whether this check is actually required
  on `develop` or `main`. It is required on neither, and enabling protection is
  out of this repository's reach — see "Visible is not blocking" above.
- Does not run on feature branch pushes to avoid blocking development
- Pipeline fails if high-priority security findings are detected
- Provides security gate before code is merged to `develop`

**SRT does NOT cover dependency CVEs.** Its `syft` stage builds an SBOM
(inventory only, no vulnerability matching), so a separate gate handles SCA:

```bash
make dep-audit        # audit every pinned Python + Node dep against OSV (fails on HIGH+)
make dep-audit-fast   # reuse existing dist/manifests instead of regenerating
```

Gated in CI by the `dep_audit` job — GitLab (`fast_checks`, every push and MR)
and GitHub (`.github/workflows/security-checks.yml`, every pull request). No AWS
needed either side. Triage unreachable advisories in
`scripts/security/dep_audit_allowlist.json` with a justification — the same
pattern `scripts/srt/issues.json` uses for SRT. See
`.claude/skills/srt-security-scan.md`.

### IDP CLI Commands

The IDP CLI is used for programmatic deployment and batch processing:

```bash
# Install all packages into current Python environment
make setup
# Or create an isolated .venv first
make setup-venv

# Deploy a new stack
idp-cli deploy \
    --stack-name my-idp-stack \
    --pattern pattern-2 \
    --admin-email your.email@example.com \
    --max-concurrent 100 \
    --wait

# Deploy with custom configuration
idp-cli deploy \
    --stack-name my-idp-stack \
    --pattern pattern-2 \
    --custom-config ./config_library/unified/bank-statement-sample/config.yaml \
    --wait

# Process documents in batch
idp-cli run-inference \
    --stack-name <your-stack-name> \
    --dir ./samples/ \
    --monitor

# Download results
idp-cli download-results \
    --stack-name <your-stack-name> \
    --batch-id <batch-id> \
    --output-dir ./results/
```

### Local Lambda Testing

```bash
cd patterns/unified/
sam build
sam local invoke OCRFunction -e ../../testing/OCRFunction-event.json --env-vars ../../testing/env.json
```

### Development Setup

```bash
# Install idp_common library in edit mode with all dependencies
cd lib/idp_common_pkg && make dev
```

## Architecture Overview

### Nested Stack Architecture

The solution uses a modular architecture with the main template (`template.yaml`) and nested pattern stacks:

**Main Stack** (`template.yaml`) - Pattern-agnostic resources:
- S3 Buckets (Input, Output, Working, Configuration, Evaluation Baseline)
- SQS Queues and Dead Letter Queues
- DynamoDB Tables (Execution Tracking, Concurrency, Configuration)
- Lambda Functions (Queue Processing, Queue Sending, Workflow Tracking, Document Status Lookup, Evaluation, UI Integration)
- CloudWatch Alarms and Dashboard
- Web UI Infrastructure (CloudFront, S3 for static assets, CodeBuild)
- Authentication (Cognito User Pool, Identity Pool)
- API Gateway REST API + dispatcher Lambda (for UI-backend communication)

**Unified Pattern Stack** (`patterns/unified/template.yaml`) - Processing resources:
- Step Functions State Machine (BDA branch + Pipeline branch + shared tail)
- Lambda Functions (OCR, Classification, Extraction, Assessment, Summarization, Evaluation, etc.)
- CloudWatch Dashboard

### Processing Modes

The unified architecture supports two processing modes, controlled by the `use_bda` configuration flag:

1. **BDA Mode** (formerly Pattern 1)
   - Uses AWS Bedrock Data Automation for end-to-end processing
   - Handles packet or media documents with integrated OCR, classification, and extraction

2. **Pipeline Mode** (formerly Pattern 2)
   - OCR with Amazon Textract
   - Classification with Bedrock (page-level or holistic)
   - Extraction with Bedrock (traditional or agentic)
   - Supports few-shot examples
   - Optional agentic extraction with deterministic table parsing

> **Note**: The separate `patterns/pattern-1/`, `patterns/pattern-2/`, and `patterns/pattern-3/` directories have been removed. All processing is now in `patterns/unified/`. See [pattern-1.md](docs/pattern-1.md) and [pattern-2.md](docs/pattern-2.md) for historical reference.

### Agentic Extraction with Table Parsing

The extraction service supports an optional **agentic extraction mode** with intelligent table parsing:

**When to Use**:
- Documents with large tables (100+ rows) where completeness is critical
- Bank statements, transaction logs, brokerage statements
- Multi-page tables that may split across OCR page breaks
- Documents where OCR artifacts (empty lines, missing characters) cause data loss

**Key Features**:
- **Intelligent Lookahead Recovery**: Tolerates OCR artifacts (empty lines, missing pipes) by looking ahead to detect table continuation
- **Auto-Merge Table Fragments**: Automatically merges tables with identical columns that were split by page breaks
- **Smart Warnings**: Agent receives actionable warnings (⚠️ fragmentation, ℹ️ recovery) to verify completeness
- **Hybrid Extraction**: Agent uses deterministic parsing for well-structured tables, falls back to LLM for complex layouts
- **Completeness Validation**: Service validates extracted data against schema constraints (e.g., `minItems`)

**Configuration**:
```yaml
extraction:
  model: "us.anthropic.claude-sonnet-4-20250514-v1:0"
  agentic:
    enabled: true
    table_parsing:
      enabled: true  # Enable deterministic table parser tool
      max_empty_line_gap: 3  # Tolerate up to 3 empty lines in tables (0-10)
      auto_merge_adjacent_tables: true  # Merge table fragments
      min_confidence_threshold: 95.0  # OCR confidence target (Textract only)
      min_parse_success_rate: 0.90  # Quality threshold for parsed results
```

**Tuning**:
- **High-quality OCR**: `max_empty_line_gap: 2`
- **Standard quality**: `max_empty_line_gap: 3` (default)
- **Complex/noisy documents**: `max_empty_line_gap: 5-7`

See `lib/idp_common_pkg/idp_common/extraction/README.md` for detailed documentation.

### Document Processing Flow

1. Documents uploaded to Input S3 bucket trigger EventBridge events
2. Queue Sender Lambda records event in tracking table and sends to SQS
3. Queue Processor Lambda:
   - Picks up messages in batches
   - Manages workflow concurrency using DynamoDB counter
   - Starts Step Functions executions
4. Step Functions workflow runs pattern-specific processing steps
5. Results written to Output S3 bucket
6. Workflow completion events update tracking and metrics

### Key Libraries

**`idp_common_pkg`** (`lib/idp_common_pkg/`):
- Core shared library powering the accelerator
- Modular installation: Install only needed components to minimize Lambda package size
  - ⚠️ Always install first-party packages **from the local checkout**, never by
    bare name — those names on public PyPI belong to unrelated parties, so a bare
    `pip install` fetches someone else's code. See
    `docs/dependency-confusion.md`.
  - `pip install -e "lib/idp_common_pkg[core]"` - minimal dependencies
  - `pip install -e "lib/idp_common_pkg[ocr]"` - OCR support
  - `pip install -e "lib/idp_common_pkg[classification]"` - Classification support
  - `pip install -e "lib/idp_common_pkg[extraction]"` - Extraction support (includes optional agentic mode with deterministic table parsing tool)
  - `pip install -e "lib/idp_common_pkg[evaluation]"` - Evaluation support
  - `pip install -e "lib/idp_common_pkg[all]"` - everything
- Components: OCR, Classification, Extraction (supports traditional and agentic modes with intelligent table parsing), Evaluation, Summarization, API adapter (`idp_common.api_adapter`, the REST dispatcher's resolver-event adapter), Reporting, BDA integration
- Configuration management via DynamoDB
- Document models and data structures
- Extraction features:
  - Traditional LLM-based extraction with few-shot examples
  - Agentic extraction with tool-based structured output (Strands framework)
  - Deterministic Markdown table parser for robust tabular data extraction
  - Intelligent recovery from OCR artifacts (empty lines, missing pipes)
  - Automatic merging of table fragments split by page breaks
  - Hybrid extraction: agent uses parsing for tables, LLM for complex layouts

**`idp_cli`** (`lib/idp_cli_pkg/idp_cli/`):
- Command-line interface for deployment and batch processing
- Stack deployment and updates
- Batch document processing
- Evaluation workflows
- Analytics integration

### Web UI

- React-based interface using Cloudscape Design System
- Vite build system
- Node.js 22.12+ and npm required
- Authentication via AWS Amplify v6 and Cognito
- Document status via REST polling of the tracking table (`src/ui/src/hooks/use-polling.ts`); chat tokens stream from a Lambda Function URL
- Location: `src/ui/`

## Configuration System

Configuration is managed via DynamoDB Configuration Table with two record types:
- **Default**: Built-in pattern configurations from `config_library/`
- **Custom**: User-provided overrides (via CustomConfigPath parameter or CLI)

Configuration presets available:
- **Pattern 1**: `lending-package-sample`, `realkie-fcc-verified`
- **Pattern 2**: `lending-package-sample`, `rvl-cdip`, `rvl-cdip-with-few-shot-examples`, `bank-statement-sample`, `realkie-fcc-verified`
- **Pattern 3**: `rvl-cdip`

Custom configurations override selected pattern presets when specified.

## Development Practices

### Code Quality Standards

- **Python**:
  - PEP 8 style guidelines
  - Linting with `ruff` configured in `ruff.toml`
  - Type checking with `basedpyright` configured in `pyrightconfig.json`
  - Line length: 88 characters
  - Target version: Python 3.12

- **JavaScript/TypeScript**:
  - ESLint configuration in `src/ui/.eslintrc`
  - Run `npm run lint` in `src/ui/` to verify

### Testing Standards

- Tests located in `lib/idp_common_pkg/tests/` and `lib/idp_cli_pkg/tests/`
- Use pytest markers: `@pytest.mark.unit` and `@pytest.mark.integration`
- Integration tests require AWS resources

### Git Workflow

- Main development branch: `develop`
- Create feature branches with prefixes: `feature/`, `fix/`, `docs/`
- PRs should target `develop` branch

### Commit messages and PR descriptions are published text

This repository is public, and both are effectively permanent: a merged commit
message cannot be edited, and force-pushing a branch does **not** retract one —
GitHub keeps a merged PR's commits and its "Files changed" view independently of
any branch, so the only remedy is a GitHub Support request. Write both as if they
were a published document, because they are.

- **Keep internal-only references out.** Corporate email addresses, hostnames that
  resolve only on the internal network, and internal review or ticket identifiers
  mean nothing to a reader of this repository and do not belong in its history.
  A `PreToolUse` hook blocks the common cases before the command runs — see below.
- **Write at summary altitude.** Say what the change accomplishes and why, not an
  inventory of the individual strings it touched. "Trim the governance docs to
  community-facing guidance" is the right altitude for a documentation cleanup; the
  line-by-line detail belongs in the diff, which is where a reader will look for
  it and where it stays accurate.
- **Third-party and personal information is not ours to publish.** Contributor
  names, contribution metrics or rankings, and individual repository permissions
  should not appear on someone else's behalf.
- **Exploitable security findings go through the channel in `SECURITY.md`,** which
  is private for a reason. A commit message, roadmap entry or changelog line is a
  public disclosure.
- **Facts that rot get dated or left out.** "As of today" counts, live permission
  tables and in-flight PR states are stale within a week.

`.claude/skills/code-review.md` carries the same points as a pre-submit checklist.

#### The `check-commit-text` hook

`.claude/settings.json` registers a `PreToolUse` hook on `Bash` that runs
`scripts/hooks/check_commit_text.py`. It inspects `git commit`, `git tag`,
`gh pr create`, `gh pr edit`, `gh pr comment`, `gh issue create` and `gh release
create` invocations — including heredoc and `-m` bodies, which appear in the
command text — and denies the call when it finds an internal address, hostname or
identifier, naming what it matched.

It is deliberately narrow — it matches the mechanical cases and leaves the altitude
judgment above to you. The patterns live in the script itself rather than being
restated here. If it blocks a string that is legitimately public, add that string
to the allowlist in the script with a comment saying why, rather than loosening the
pattern. Run its tests with `make test-hooks`.

#### The `check-shared-branch` guard

Changes reach `develop` and `main` through a pull request. Nothing on GitHub
enforces that (see [Visible is not blocking](#visible-is-not-blocking--make-check-branch-protection)),
so two client-side halves do, and they are deliberately separate because they cover
different routes:

| Half | Covers | Install |
|---|---|---|
| `scripts/hooks/check_shared_branch.py` — a second `PreToolUse` hook on `Bash` | `git commit` and `git push` run **through the assistant's Bash tool**, plus `gh pr merge` | none; registered in `.claude/settings.json` |
| `scripts/hooks/pre-push` — a real git `pre-push` hook | pushes the first half never sees: a plain shell, an IDE button, and the `git push` inside `make commit` | `make install-git-hooks`, once per clone |

The `PreToolUse` half refuses a `git commit` whose commit would land on a shared
branch, and a `git push` whose destination resolves to one. Both questions are
answered about the *whole* command rather than the state it started in, so `git
switch -c fix/x && git commit` is allowed and `git switch develop && git commit` is
refused — which matters because the remedy the refusal prints is usually typed as
one line. A `cd <path>` or `pushd <path>` earlier in the command is followed too, so
`cd ../other && git commit` is judged against `other`.

Destination resolution is the load-bearing part: it covers the forms that
never name the branch (a bare `git push` with an upstream, `git push origin HEAD`,
`git push origin @`), the delete form `git push origin :develop`, `--all`/`--mirror`,
and a bare push under `push.default=matching`, which sends every same-named branch.
A bare push is read **through `push.default`** rather than as the union of the
branch name and the upstream: `current` targets the branch name and
`upstream`/`tracking` the upstream, so taking both would be a false refusal under
either. `--tags` with no refspec writes no branch and is allowed; `--follow-tags`
sends the branch as well and is not. `git switch --track origin/develop` and `git
checkout -t origin/develop` name no new branch — git takes the remote ref's leaf —
so HEAD lands on local `develop` and a commit after one of them is refused.

It also refuses `gh pr merge` when a check has **concluded** as failing. Two ways of
misreading that command are silent, so both are guarded and tested: an option that
takes a separate value must not have its value read as the pull request number
(`gh pr merge --subject "x" 1055`), and `--repo` is accepted on **either** side of
the subcommand, because gh registers it on the root command — `gh -R owner/name pr
merge 1055` is valid, and reading `owner/name` as the subcommand leaves the command
unrecognised so no check runs at all. In both cases `gh pr checks` then errors or
answers about the wrong pull request, which reads as "cannot tell" and allows the
merge with nothing printed.

A commit on a branch that merely *tracks* `origin/develop` is allowed. `git
checkout -b fix/x origin/develop` sets that upstream, so refusing it would refuse
the ordinary way of starting work; the bare `git push` from such a branch is the
thing that would reach `develop`, and that is refused.

Overrides, one per decision: `ALLOW_SHARED_BRANCH=1` for a commit or push,
`ALLOW_RED_MERGE=1` for a merge. Put the assignment in front of the command
(`ALLOW_SHARED_BRANCH=1 git push origin develop`). The environment is read too, and
that is the form to be careful with: a variable exported in one tool call is gone by
the next, but one exported by a shell profile, an IDE or a CI runner persists and
turns the check off for **every** command in that environment. Because that is
invisible by construction, both halves print one line to stderr naming the check an
honoured override disabled. Only an affirmative value (`1`, `true`, `yes`, `y`, `on`)
counts: `ALLOW_SHARED_BRANCH=0` leaves the guard on.

Neither variable is registered in `scripts/tests/gate_exemptions.json`, and that is
a decision rather than an oversight — the reasoning is written out in the hook's
header. That registry governs a **gate** turned off for a file, a line or a rule,
because such a reason is written once and outlives what it described. These are
per-invocation switches on a local convention, decided by whoever runs the command
and recorded nowhere, so an entry would be one no ratchet can test and no audit can
act on. What they can do quietly — be exported once and disable everything
afterwards — is handled where it happens, by the stderr line above.

**What neither half covers.** The claim is bounded, and these are the routes
around it:

- A merge performed through GitHub's own Merge button, which runs no code here.
- A pull request whose checks never ran (a fork PR gets no GitHub CI here).
- `git push --no-verify`, which skips the `pre-push` hook outright.
- Anything that reaches `git` other than as the first word of a segment: a script
  file (`sh deploy.sh`), `bash -c`, `eval`, `xargs`, a wrapper that takes options of
  its own (`env`/`nice`/`sudo`), an absolute path, or a shell function shadowing
  `git`. The `PreToolUse` half reads the command text it is given, and none of those
  spell out what will run. The `pre-push` hook is what catches them. A leading run
  of **shell keywords** is a different matter and *is* handled (`SHELL_KEYWORDS`,
  plus `time` and `command`): `if make test; then git push origin develop; fi` is an
  ordinary thing to type, and it puts `then` first in the segment.
- A **git alias** that runs a shell command — `git -c alias.p='!git push origin
  develop' p`. `git` is the first word, but the subcommand is `p`, and resolving
  aliases would mean reading configuration the hook does not read.
- `git checkout <branch> --` with nothing after the `--`. A trailing `--` normally
  introduces a pathspec, which makes the command a file restore, and that is the
  reading worth having; git treats this particular spelling as a branch switch, so a
  commit after it is judged against the branch HEAD was on.
- `cd -`, bare `pushd` and `popd`, which depend on a directory stack the hook does
  not keep. A segment after one of them is judged against the directory in force
  before it, and that misses in **either** direction depending on which way the
  stack was moving: `cd -` back into a `develop` checkout is not seen, and `cd -`
  back out of one refuses a commit that was fine.
- History written onto a shared branch by anything other than `git commit` —
  `merge`, `cherry-pick`, `revert`, `rebase`, `am`. Those are local until pushed,
  and the push is what gets refused.

Neither half looks at *which* remote, so pushing `develop` to a personal fork is
refused too, and both key on the branch *name*, so a commit onto `main` in an
unrelated repository visited in the same session is refused. Both are overridable.

Four properties worth knowing before relying on it:

- **It fails open.** An unparseable command, `git` or `gh` unavailable, a network
  failure — all allow the command. A guard that wedges the session is worse than one
  that misses a case, which is the same choice `check_commit_text.py` makes. Failing
  open is about what cannot be *read*, though, not about where the command runs: an
  explicit refspec names its destination on the command line, so `git push origin
  develop` is refused even in a directory that is not a repository.
- **Only *concluded* failures block a merge.** `pending` is the steady state for the
  two path-filtered workflows and the one conditional check, so refusing on pending
  would refuse every merge. A pull request whose checks **never ran** — a fork PR
  gets no GitHub CI here — is not refused either, since refusing it would block the
  only route a fork contribution has.
- **`make install-git-hooks` writes to `$(git rev-parse --git-common-dir)/hooks`,
  not `git rev-parse --git-path hooks`.** The latter honours `core.hooksPath`, and a
  managed developer machine may set that system-wide (in `/etc/gitconfig`) to a
  root-owned directory of hook runners belonging to a security tool, so it resolves
  to a path the target must never write to. The common dir is also the right answer
  inside a worktree, where hooks are shared with the main checkout.
- ⚠️ **Under a system-wide `core.hooksPath`, the `pre-push` hook judges by `HEAD`
  rather than by the refs.** git runs the *runner's* hooks, and the repository's own
  hook is reached only because those runners chain to it. They forward the hook's
  arguments but **not its stdin**, so the hook receives no ref list — and exiting 0
  on an empty ref list is how a hook can be installed, reported successful, and
  refuse nothing. It therefore falls back to `HEAD` and its upstream, and says which
  basis it used. **That substitution is wrong in both directions, and the two
  directions arise on different machines.** The over-refusal is not confined to a
  redirected machine: git supplies an empty ref list for any **up-to-date** push as
  well, measured on the direct path with no runner involved, so a no-op push while
  `HEAD` sits on `develop` is refused on an ordinary machine — which is why the
  message names both possible causes rather than blaming a runner the user may not
  have. The under-refusal *is* specific to the redirect: there the destination stops
  being checked for pushes that do have work to send, so one whose destination *is*
  a shared branch while `HEAD` is not on one (`HEAD:refs/heads/develop`,
  `HEAD:develop`, `origin develop`, `--all`) goes through although all four are
  refused where the ref list arrives. The `PreToolUse` half resolves destinations
  from the command line and refuses all four, so what stays uncovered on such a
  machine is a push typed into a plain shell rather than one the assistant runs. The
  destination really is unknowable in that state — with no ref list a `pre-push`
  hook is given only the remote's name and URL — and refusing every push there would
  make the hook unusable. `make install-git-hooks` prints the implication when it
  detects the redirect;
  `test_pre_push_still_refuses_through_a_runner_that_drops_stdin`,
  `test_the_head_fallback_is_wrong_in_both_directions` and
  `test_an_up_to_date_push_supplies_no_refs_on_an_ordinary_machine` measure all
  three claims, because a suite that only points `core.hooksPath` at the
  repository's own hooks cannot see any of them.

Run the tests for both halves with `make test-hooks`.

## Important Implementation Details

### Pattern-2 Container Deployment

Pattern-2 functions are deployed as container images (not ZIP files) due to size constraints. The build process:
1. Builds container images using Docker
2. Pushes images to ECR
3. Lambda functions reference ECR image URIs

Ensure Docker is running and you have ECR permissions when building Pattern-2.

### GovCloud Compatibility

The codebase maintains GovCloud compatibility:
- Use `arn:${AWS::Partition}:` instead of hardcoded `arn:aws:`
- Use `${AWS::URLSuffix}` instead of hardcoded `amazonaws.com`
- Validation enforced via `make check-arn-partitions`, which runs in `lint`,
  `fastlint` and `lint-cicd` (so both CIs) over **every** template discovered by
  content. No template is skipped; two individual lines are, via the per-line
  `ARN_PARTITION_EXEMPT` — see the note above

### Nested Stacks

The solution is split into nested stacks to stay under CloudFormation resource
limits. Notably, `nested/api-resolvers/` holds the UI API resolver Lambdas plus
the API Gateway REST API + dispatcher that the web UI calls (logical id
`APIRESOLVERSTACK`). (This stack was historically named `nested/appsync` /
`APPSYNCSTACK` when the UI used AWS AppSync, which has since been removed.)

### Lambda Layer Dependencies

Lambda functions reference the `idp_common_pkg` library:
- In `requirements.txt`: `../../lib/idp_common_pkg[extraction]`
- Use modular installation to minimize package size
- The library path is relative from Lambda source directories

### UI Checksum Optimization

The build system uses checksums to avoid rebuilding UI unnecessarily:
- Checksum stored in `src/ui/.checksum` and root `.checksum`
- `make ui-lint` skips linting if checksum unchanged
- Speeds up CI/CD and local development

## Sample Documents

Testing samples available in `samples/`:
- **Pattern 1 & 2**: `lending_package.pdf`
- **Pattern 3**: `rvl_cdip_package.pdf`

## Validation Scripts

- `scripts/sdlc/validate_buildspec.py` - Validates CodeBuild buildspec files
- `scripts/sdlc/validate_service_role_permissions.py` - Verifies IAM service role permissions
- `scripts/sdlc/typecheck_pr_changes.py` - Type checks only changed files in PRs
- `scripts/sdlc/check_branch_protection.py` - Checks that a branch's required
  status checks match the jobs the workflows actually run (`make
  check-branch-protection`; opt-in, read-only GitHub API, one branch per run).
  Neither `develop` nor `main` is protected and enabling it is out of this
  repository's reach — see "Visible is not blocking" above

## AWS Access for Live Troubleshooting

The `default` AWS CLI profile is configured with credentials for the active
deployment account. Use it (not the runtime sandbox's ambient credentials,
which may point at a different account) to inspect deployed resources when
troubleshooting:

```bash
AWS_PROFILE=default aws sts get-caller-identity   # confirm the account first
AWS_PROFILE=default aws logs tail /aws/lambda/<fn> --since 1h
```

For the CloudWatch MCP tools, pass `profile_name: "default"` (and the stack's
region). Lambda log groups take one of **three** shapes, so list on both
prefixes before concluding a function has no logs:

| Shape | Used by |
|---|---|
| `/<StackName>/lambda/<FunctionLogicalId>` | `patterns/unified` and every feature-platform extension — the pipeline-hooks dispatcher, feature hook Lambdas, `FeatureApiFunction`, `UiDeployerFunction` |
| `/aws/lambda/<StackName>-<Name>` | 5 groups in the parent `template.yaml` (`CircuitBreakerManager`, `CalculateCapacity`, `CalculateCapacityResolver`, `VersionCheckResolver`, `AgentProcessor`) and 2 in `nested/api-resolvers/` |
| `/aws/lambda/<fn>` (Lambda's default) | **custom-resource-only Lambdas**, which deliberately keep the auto-created group — they run only during a stack operation, so indefinite retention is an accepted cost. Includes `nested/bedrockkb/` (all 5), the `Custom::` handlers in `template.yaml`, and the feature-platform install hooks (`...-RegisterFeature...`, `...-RegisterFeatureHooks...`, `...-ApplyFeatureConfigPreset...`). Enforced by `scripts/tests/test_lambda_log_groups.py` |
| `<StackName>-<LogicalId>-<hash>` — **no prefix at all** | the ~84 groups that declare no `LogGroupName` and so take CloudFormation's generated name. This is the single most common shape in the repo and it does **not** start with `/`, so neither a `/aws/lambda/` nor a `/<StackName>/` prefix listing finds it. `aws logs describe-log-groups --log-group-name-prefix '<StackName>-'` is the third listing you need |

Note the first shape is `/<StackName>/`, **not** `/aws/lambda/<StackName>-`, and
the fourth has no leading `/` at all — so a single `/aws/lambda/` prefix listing
misses the dispatcher, every feature Lambda, *and* the ~84 generated-name groups.
Listing on all three prefixes (`/aws/lambda/`, `/<StackName>/`, `<StackName>-`)
is the only way to be sure a function has no logs. See the log-group naming rules
in `.claude/skills/infrastructure.md`.

## AWS Service Requirements

### Required Bedrock Model Access

Request access to these models in Amazon Bedrock before deployment:
- **Amazon**: All Nova models, Titan Text Embeddings V2
- **Anthropic**: Claude 3.x models, Claude 4.x models

### Key AWS Services Used

- Amazon Bedrock (Foundational Models, Data Automation, Knowledge Bases)
- Amazon Textract
- Amazon SageMaker (for Pattern-3 UDOP endpoint)
- AWS Lambda
- AWS Step Functions
- Amazon S3
- Amazon SQS
- Amazon DynamoDB
- Amazon CloudWatch
- Amazon API Gateway (UI ⇄ backend REST API; optionally the UI's S3-proxy host)
- Amazon Cognito
- Amazon CloudFront
- Amazon EventBridge
- Amazon SNS
- Amazon Glue (for evaluation analytics)
- Amazon Athena (for evaluation reporting)

## Troubleshooting

### Build Failures

Use `--verbose` flag with publish.py to see detailed error messages:
```bash
python3 publish.py idp-1234567890 idp us-east-1 --verbose
```

### Pattern-2 Container Build Issues

Ensure:
- Docker daemon is running
- AWS credentials have ECR permissions
- Sufficient disk space for container images

### UI Build Issues

Check:
- Node.js version >= 22.12.0
- Run `npm ci` in `src/ui/` to install dependencies
- Check `src/ui/.checksum` if builds are being skipped unexpectedly

## Skill Files

Domain-specific coding conventions, checklists, and workflows live in
`.claude/skills/`. Consult the relevant skill file whenever a task touches
that domain:

| Skill File | When to Use |
|------------|-------------|
| `.claude/skills/backend-lambda.md` | Writing Lambda handlers or `idp_common` Python code |
| `.claude/skills/frontend-ui.md` | React / TypeScript / Cloudscape UI changes |
| `.claude/skills/infrastructure.md` | CloudFormation / SAM templates, nested stacks, GovCloud |
| `.claude/skills/extraction-pipeline.md` | Document processing pipeline, configuration, agentic extraction |
| `.claude/skills/code-review.md` | Pre-commit self-review checklist for your own changes |
| `.claude/skills/srt-security-scan.md` | Running the SRT security scan (`make srt-scan`), triaging HIGH findings, and mitigating (`# nosec`/code fix) or suppressing (`scripts/srt/issues.json`) them |
| `.claude/skills/curate-security-results.md` | Publishing a public-safe, auditable snapshot of the four security tests (SRT, ZAP DAST, RBAC static/dynamic) into `security/test-results/<version>/` via `scripts/security/curate_results.py` |
| `.claude/skills/api-rbac-test.md` | Verifying API authorization (Cognito groups + config-version scope) via `make api-test` / `make api-test-static`; adding a new API operation |
| `.claude/skills/live-auth-checks.md` | Changing the Cognito pre-token IdP group-mapping trigger, `getStepFunctionExecution`, or `UserPoolClient` attribute permissions — `make live-auth-checks` (throwaway resources, no stack) and `make verify-idp-federation` (a real federated sign-in via a throwaway OIDC provider). Includes the Cognito behaviours the docs get wrong |
| `.claude/skills/ux-test.md` | Browser-driven UX testing of the web UI against a live stack (`make ux-test`) — functional pass/fail per flow **plus** usability findings. The only test layer here that opens a browser; flows live in `scripts/ux_flows.yaml`. Optionally **recorded** as a narrated, captioned mp4 via `scripts/ux_recorder.py` (Polly generative voice, idle time compressed) |
| `.claude/skills/product-demo.md` | Recording a **product demo video** of changelog entries, a PR/MR or a named feature against a live stack, for the team or `docs/demo-videos.md`. Proposes three storyboards, records the one the user picks with `scripts/ux_recorder.py start --kind demo` (narrated, captioned mp4 ending on a Key-takeaways card), drafts the docs entry; confirmed storyboards live in `scripts/demo_storyboards.yaml`. Sibling of `ux-test.md`: that one judges the UI, this one shows it |
| `.claude/skills/run-stack-tests.md` | Running the deploy-variant stack-tests (`make stacktest-*`: ZAP DAST, Jobs API, WAF, APIGateway hosting variants) manually against a live stack — they no longer run automatically in CI. Includes VPC auto-discovery + confirm for the VPC-requiring ones |
| `.claude/skills/transform-deploy-test.md` | Deploy-testing the `--headless` / `--govcloud` template **transforms** (`make transform-deploy-test-*`) — the only tier that deploys a transformed template and processes a real document. Includes the commercial-vs-GovCloud caveat you must report |
| `.claude/skills/pr-review.md` | Reviewing an external GitHub PR or GitLab MR at a URL (e.g. `review <url>`) |
| `.claude/skills/repo-quality-review.md` | Holistic **whole-repository** quality review, re-runnable as periodic QA ("review the whole repo", "how healthy is this codebase?") — ten dimensions fanned out one subagent each, the offline measurement commands that produce the baseline numbers, and the two recurring defect classes (a control that exists but is never consulted; a fix applied to the instance and not the class). Read-only by construction; needs the Agent tool authorized explicitly |
| `.claude/skills/dependabot-prs.md` | Triaging Dependabot PRs — retarget to `develop`, per-PR risk assessment, redundancy check vs develop, merge-if-safe, mandatory post-merge test validation |
| `.claude/skills/sync-pii-anonymizer.md` | Re-syncing the **vendored** copy of `awslabs/pii-anonymizer` at `feature-platform/pii-anonymizer/hook/vendor/` after upstream fixes a bug or adds a feature — diff against the commit pinned in `PROVENANCE.md`, re-copy only the documented document closure via `resync.sh` (never audio, handlers, infra or observability), chase newly-added intra-project imports that grow the closure, then verify nothing excluded leaked in |
| `.claude/skills/create-hf-dataset-pr.md` | Contributing a data/label correction to an external HuggingFace dataset via a community PR (parquet key-order gotcha, verification, review artifacts) |
| `.claude/skills/testing-qa.md` | Writing tests, pytest patterns, moto, conftest setup |
| `.claude/skills/release-validation.md` | **Validating a published release end to end in one request** ("validate the 0.6.8 release") — every live tier (security snapshot, deploy variants, `--headless`/`--govcloud` transforms, in-place upgrade, release benchmark A/B) plus the offline battery; writes `docs/release-validation/v<X>.md`, `security/test-results/<X>/` and the benchmark audit, and opens the two PRs. The umbrella over the per-tier skills below |
| `.claude/skills/full-test-battery.md` | Running the FULL test battery (all suites + lint/typecheck) to validate a branch/merge; includes the known pre-existing-failure baseline so real regressions stand out |
| `.claude/skills/test-upgrade.md` | Validating an in-place CloudFormation stack upgrade between two published releases (X→Y) — deploy the FROM template, `update-stack` to the TO template, watch the `UpdateDefaultConfig` custom resource, diagnose/recover a rollback deadlock, tear down |
| `.claude/skills/live-eval-and-cost.md` | Live benchmark A/B, upgrade testing, reading accuracy/cost/confidence + prompt-cache/model cost facts |
| `.claude/skills/run-benchmarks.md` | Running the empirical benchmark suite in `benchmarks/` (config × doc-size matrix with exact ground truth; success/completeness/accuracy/calibration/time/tokens/cost) to produce the guidance paper or gate a change vs baseline |
| `.claude/skills/documentation.md` | Documentation standards, two doc tiers, CHANGELOG, docs-site, and the "adding a Bedrock model" checklist |
| `.claude/skills/add-model.md` | Adding or changing a selectable Bedrock model (model IDs, regions, limits, pricing, template enums, client routing, UI, both doc tiers, tests) — the full expanded checklist |
| `.claude/skills/prepare-changelog.md` | Preparing the `[Unreleased]` CHANGELOG section for release — three-section shape (Added/Changed/Fixed), net-since-release entries (drop intra-cycle churn), compact entries with doc/PR links |
| `.claude/skills/cut-release-changelog.md` | Cutting the release section at tag time — relabel `[Unreleased]` to the `VERSION` number (no date) and append the version-pinned `## Templates` URLs for the three published regions |

> **Note:** `.claude/skills/` is canonical. The Cline assistant's
> `.cline/skills/` files are **symlinks** to these (different filenames), so
> editing a `.claude` skill updates both — do not create separate `.cline`
> copies. See the "Two skill systems, one source of truth" section in
> `.claude/skills/documentation.md` for the filename map and portability caveat.

### Documentation lives in two tiers

When changing `idp_common` or adding/changing models, update **both** tiers:
1. **User/feature docs** — `docs/*.md` (published to the Starlight site).
2. **Developer/module docs** — `lib/idp_common_pkg/**/README.md` (one per
   subpackage; the canonical home for library/client behavior).

Adding a selectable Bedrock model touches many files (template enums,
`config_library/pricing.yaml`, `model_config_limits.yaml`, config models,
`update_configuration`, UI dropdown, the bedrock client, IAM, **both doc
tiers**, and `CHANGELOG.md`). Follow the checklist in
`.claude/skills/documentation.md` so none are missed.

### Documentation states what is true now, not what a previous draft said

Every document in this repository is read as a statement of current fact. A reader
needs to know what is true; they have no use for the editing history of the page
they are reading, and git already records it. So **do not write doc-about-doc
commentary**:

- ❌ "Two clarifications that the older version of this guide got wrong:"
- ❌ "This page previously quoted a −78% cost saving."
- ❌ "**Correction.** This section previously claimed EC2 access was limited."
- ❌ "An earlier draft of this document claimed it did. That claim was false."
- ❌ "An earlier revision of this entry recommended exactly that; it is withdrawn."

Write the true statement instead, and keep whatever substance the retraction
carried by reframing it as guidance:

- ✅ "Two things the table above does not make obvious:"
- ✅ "**Why there is no percentage here.** A figure would have to come from the
  cost report, which at the time of the run priced cache reads by substring
  match …"
- ✅ "`ec2:*` is the whole grant, and it is not narrowed to a VPC-only action list,
  because …"
- ✅ "**Detection numbers alone cannot establish data loss.** … is the conclusion
  they invite, and it is wrong: counting the extracted rows gives 0 missing."
- ✅ "⚠️ Do **not** prefer the SigV4-derived value: it is a pool-wide constant, so …"

The distinction that matters is **whose** history it is:

| Legitimate — keep | Not legitimate — rewrite |
|---|---|
| **Product/behaviour history** a reader acts on: "`--log-level INFO` is now honoured; it used to be silently treated as unset", "renamed from *Configuration Versions*", upgrade-visible changes, `CHANGELOG.md` entries | The document's own drafts, revisions and mistakes: "this page previously said", "an earlier draft claimed", "that was wrong and is withdrawn" |
| A **measurement or instrument** caveat: "an earlier version of the sweep shared cache state between points, so re-measure with identical calls" | Self-narration about the writing or review process: "four corrections from review", "it took a fourth look to catch" |
| A correction to an **external** artifact a reader will also read — an AWS doc, a GitHub issue, an upstream changelog | A correction to this repository's own prose, stated as a correction |

Two exceptions, both deliberate ledgers rather than prose: the threat model's
revision table in `security/threat-modeling/README.md` (an audit artifact for a
versioned security deliverable) and the withdrawn-findings table in
`.claude/skills/repo-quality-review.md` (whose whole purpose is to stop the next
review re-reporting a non-defect). Keep those; do not add a third.

### Reviewing External PRs / MRs

When the user asks `review <MR/PR URL>` (or similar), load
`.claude/skills/pr-review.md` and produce a structured review answering:

1. Is this a good PR?
2. Safe? No regressions?
3. Good UX?
4. No security issues?
5. Well documented?
6. Safe to merge?

The expected target branch for this repo is `develop` — flag any PR/MR that
targets a different branch as the first finding. The review is read-only: do
not push, merge, approve, or post comments on the PR/MR unless the user
explicitly asks.

