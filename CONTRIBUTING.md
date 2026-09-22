Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Contributing to GenAI Intelligent Document Processing (GenAIIDP)

Thank you for your interest in contributing. This guide is written so that a
contributor who has never opened this repository before can get from a fresh
clone to a green pull request without guessing. Every command below is either
run routinely by the maintainers or verified against the `Makefile`; if
something here does not work, that is a bug in this file and worth an issue.

Two facts to get right before anything else: pull requests target **`develop`**,
not `main`, and first-party Python packages must always be installed **from this
checkout by path**, never by bare name. Both are explained below.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Reporting a security issue](#reporting-a-security-issue)
- [Reporting bugs and requesting features](#reporting-bugs-and-requesting-features)
- [Getting started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Fork, clone, and install](#fork-clone-and-install)
  - [Installing the shared library safely](#installing-the-shared-library-safely)
  - [Modular extras, and why Lambda package size matters](#modular-extras-and-why-lambda-package-size-matters)
- [Repository layout](#repository-layout)
- [Development workflow](#development-workflow)
  - [Branching](#branching)
  - [Where the domain conventions live](#where-the-domain-conventions-live)
  - [Documentation lives in two tiers](#documentation-lives-in-two-tiers)
  - [CHANGELOG entries](#changelog-entries)
- [The local gate set](#the-local-gate-set)
  - [Before every commit](#before-every-commit)
  - [Before opening a pull request](#before-opening-a-pull-request)
  - [Tests](#tests)
  - [Security gates](#security-gates)
  - [Type checking](#type-checking)
- [What CI runs on your pull request](#what-ci-runs-on-your-pull-request)
  - [Coverage is reported, not enforced](#coverage-is-reported-not-enforced)
- [Pull request process](#pull-request-process)
- [Make target reference](#make-target-reference)
- [Coding standards](#coding-standards)
- [AWS-specific considerations](#aws-specific-considerations)

## Code of Conduct

This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact
opensource-codeofconduct@amazon.com with any additional questions or comments.

## Reporting a security issue

**Do not open a public GitHub issue for anything exploitable.** Report it to
AWS/Amazon Security through the
[vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/),
which is monitored and gives the project a chance to ship a fix before the
problem is public.

Hardening suggestions that are not exploitable — a missing header, a permission
that could be narrower, a dependency worth bumping — are fine as ordinary
issues. Both channels are also stated in [SECURITY.md](SECURITY.md) at the
repository root, which is where GitHub surfaces them in the "Report a
vulnerability" affordance; that file also records which releases receive fixes.
The two paragraphs above are the whole policy, so if you only read this page you
have not missed anything.

## Reporting bugs and requesting features

Use the GitHub issue tracker, with one important distinction: **this repository
is for the GenAIIDP solution, not for the AWS services it calls.** Deployment
failures in the CloudFormation templates, bugs in the Step Functions workflows,
Web UI problems, and feature requests specific to this accelerator belong here.
The accuracy of an Amazon Bedrock model, Amazon Textract extraction quality,
service quotas and throttling, and feature requests for AWS services themselves
belong with AWS Support or the relevant service forum — nobody working on this
repository can fix those.

Two templates make this easier:

- [Bug Report](.github/ISSUE_TEMPLATE/bug_report.yml)
- [Feature Request](.github/ISSUE_TEMPLATE/feature_request.yml)

Please check existing open and recently closed issues first. For a bug, the
details that actually shorten the round trip are a reproducible sequence of
steps, the version of the solution you deployed (see `VERSION`), any
modifications you made, and anything unusual about your environment. For a
feature request, describe the value and how it fits the project's direction, and
sketch an implementation if you have one in mind.

## Getting started

### Prerequisites

The authoritative check is `check_prerequisites()` in
`lib/idp_sdk/idp_sdk/_core/publish.py`, which the publish flow runs before it
builds anything. It requires:

| Requirement | Minimum | Where the minimum comes from |
|---|---|---|
| `bash` shell | — | Linux, macOS, or Windows + WSL |
| Python | **3.12** | `check_prerequisites()`; `ruff.toml` targets `py312` and `pyrightconfig.json` sets `pythonVersion: 3.12` |
| AWS SAM CLI | **1.129.0** | `check_prerequisites()` compares `sam --version` against it and exits if lower |
| AWS CLI (`aws`) | any | must be on `PATH` |
| `uv` | any | must be on `PATH` — it is a hard requirement of the publish flow and is easy to miss |
| Node.js | **22.12.0** | `engines` in `package.json` and `src/ui/package.json` |
| npm | **11.0.0** | `engines` in root `package.json` (the UI's own floor is lower, 10.0.0, so the root value is the one to meet) |

Two things the table above does not make obvious:

- **Docker is not required on your workstation.** Lambda code is packaged as
  source and uploaded to S3; AWS CodeBuild builds the container images during
  stack deployment. See
  [deployment.md](docs/deployment.md#option-3-build-and-deploy-from-source-code).
- **Node.js is not version-checked by the publish script**, but the publish flow
  runs `npm ci` and `npm run build` over `src/ui`, so an older Node will fail
  there instead. Treat 22.12+ as required.

Both CI systems build on **Python 3.13** (`python:3.13-bookworm`) and Node 22,
so 3.12 is the floor rather than the tested version. Per-platform setup walkthroughs:
[Linux](docs/setup-development-env-linux.md), [macOS](docs/setup-development-env-macos.md),
[Windows/WSL](docs/setup-development-env-WSL.md).

### Fork, clone, and install

```bash
git clone <your-fork-url> genaiic-idp-accelerator
cd genaiic-idp-accelerator
git checkout develop

make setup-venv                # create .venv and install every first-party package into it
source .venv/bin/activate      # required — the gate targets need .venv/bin on PATH
# or, to install into the Python environment you are already in:
make setup

npm install -g basedpyright    # the type-check gate; neither target above installs it
```

`make setup` and `make setup-venv` install `idp_common`, `idp_cli`, `idp_sdk`,
`idp_mcp_connector`, `idp_feature_sdk`, the capacity-planning test dependencies,
and the pinned `cfn-lint`, then verify that every first-party package resolved
from the checkout. Use them in preference to hand-rolling `pip install` lines.

**Activate the virtual environment if you used `make setup-venv`.** The
`Makefile` resolves `$(PYTHON)` and `$(PIP)` to `.venv/bin/` when `.venv`
exists, and it says so when `setup-venv` finishes, but it does not add
`.venv/bin` to `PATH` — and several gate recipes invoke their tool as a bare
command rather than through `$(PYTHON)`. Without activation, `make ruff-lint`
and `make format` fail with `make: ruff: No such file or directory` and
`Error 127`, and `make cfn-lint` exits 1 advising you to `Run 'make setup' or
pip install cfn-lint==1.51.0` even though `setup-venv` already installed
`cfn-lint` at `.venv/bin/cfn-lint`. `make setup` does not have this problem,
because it installs into the environment that is already active.

**`basedpyright` comes from npm, not from either setup target.** It is a
devDependency of the root `package.json` — which exists only to pin it — and
both CI systems provision it with `npm install -g basedpyright`. The `make`
type-check targets invoke it as a bare command, so it has to be on `PATH`: a
plain `npm install` at the repository root puts it in `node_modules/.bin/`,
which is enough for `npx basedpyright` or `npm run typecheck` but not for
`make typecheck` or `make typecheck-pr`.

### Installing the shared library safely

> ⚠️ **Never install a first-party package by bare name.** `idp_common`,
> `idp_sdk` and their siblings are **not published to PyPI**, but some of those
> names *do* exist on public PyPI owned by unrelated parties. So
> `pip install idp_common` does not fail — it silently installs somebody else's
> code into the environment that holds your AWS deployment credentials, and the
> mismatch usually surfaces much later as a confusing, unrelated error. Always
> install from a path.

A second rule follows from the first, and is less obvious: install the first-party
packages **together, in a single `pip install` invocation**. They depend on each
other by name, so installing them one at a time lets pip go looking for a sibling
that is not on disk yet and fall back to the index. `make setup` deliberately does
it in one pass for this reason. Both rules together give one command — every
requirement a path, every sibling present:

```bash
# Run from the repository root
pip install -e "lib/idp_common_pkg[extraction]" -e lib/idp_sdk -e lib/idp_cli_pkg
```

You can check any environment at any time:

```bash
python3 scripts/check_first_party_deps.py
```

Exit code 0 means every installed first-party package came from source. This
runs in both CI systems and inside `make setup`. The full explanation is in
[docs/dependency-confusion.md](docs/dependency-confusion.md).

### Modular extras, and why Lambda package size matters

`idp_common` is installed through extras so that a Lambda function pulls in only
what it uses. AWS Lambda caps deployment package size, and this library's full
dependency set — OCR, several model SDKs, evaluation and analytics — does not
fit comfortably in one package, so each function's `requirements.txt` asks for
the narrowest extra that works:

```bash
pip install -e "lib/idp_common_pkg[core]"            # minimal
pip install -e "lib/idp_common_pkg[ocr]"            # OCR
pip install -e "lib/idp_common_pkg[classification]" # classification
pip install -e "lib/idp_common_pkg[extraction]"     # extraction, incl. agentic mode
pip install -e "lib/idp_common_pkg[evaluation]"     # evaluation
pip install -e "lib/idp_common_pkg[all]"            # everything (local development)
```

Inside a Lambda source directory the same thing is expressed as a relative path,
which is already immune to the bare-name problem above:

```
../../lib/idp_common_pkg[extraction]
```

If you add a dependency, add it to the narrowest extra that needs it rather than
to `core`.

## Repository layout

| Path | Contents |
|---|---|
| `template.yaml` | Main CloudFormation/SAM stack — buckets, queues, tables, auth, Web UI infrastructure |
| `patterns/unified/` | The single processing stack: Step Functions state machine plus the OCR, classification, extraction, assessment and summarization Lambdas |
| `nested/` | Nested stacks kept separate to stay under CloudFormation resource limits (e.g. `nested/api-resolvers/`, `nested/bedrockkb/`) |
| `lib/idp_common_pkg/` | `idp_common`, the shared library that powers every Lambda |
| `lib/idp_sdk/`, `lib/idp_cli_pkg/`, `lib/idp_feature_sdk/`, `lib/idp_mcp_connector_pkg/` | The publish/deploy SDK, the `idp-cli` command line tool, and the feature-platform and MCP packages |
| `src/lambda/` | Lambda functions belonging to the main stack |
| `src/ui/` | React + Cloudscape web UI (Vite, Amplify v6) |
| `feature-platform/` | Optional feature extensions installed on top of a deployed stack |
| `config_library/` | Shipped configuration presets, pricing, and model limits |
| `docs/` | User-facing documentation, published to the docs site |
| `docs-site/` | Astro + Starlight site that publishes `docs/` |
| `scripts/` | Development, validation and release tooling, including `scripts/tests/` (tests of the tooling itself) |
| `benchmarks/` | Empirical accuracy/cost benchmark suite |
| `samples/`, `notebooks/` | Sample documents, sample Lambda hooks, and notebooks |
| `iam-roles/`, `security/` | Deployment IAM roles and published security-test snapshots |

> The separate `patterns/pattern-1/`, `patterns/pattern-2/` and
> `patterns/pattern-3/` directories no longer exist. Both processing modes —
> Bedrock Data Automation and the Textract-plus-Bedrock pipeline — now live in
> `patterns/unified/`, selected by the `use_bda` configuration flag.
> [pattern-1.md](docs/pattern-1.md) and [pattern-2.md](docs/pattern-2.md) are
> kept for historical reference.

## Development workflow

### Branching

The main development branch is **`develop`**. `main` tracks releases, so a pull
request opened against `main` will be asked to retarget.

```bash
git checkout develop
git pull
git checkout -b feature/your-feature-name
```

Use the prefix that matches the change: `feature/`, `fix/`, or `docs/`. Keep a
branch focused on one issue or feature — a reviewer can approve a small change
quickly and cannot do much with a large mixed one.

Install the push guard once per clone:

```bash
make install-git-hooks
```

It adds a `pre-push` hook that refuses a push whose destination is `develop` or
`main`, so that a change reaching a shared branch without a pull request takes a
deliberate act rather than a slip. Override it with `ALLOW_SHARED_BRANCH=1 git push
…` — put the assignment in front of the command. git does not clone hooks, which is
why this is a step rather than something the repository can do for you.

It is a convention, not a control, and it is worth knowing where it stops.
`git push --no-verify` skips it. Branch protection on this repository is off
(enabling it needs repository admin), so nothing prevents a merge made through
GitHub's web interface. And if your machine sets `core.hooksPath` system-wide — some
managed developer machines point it at a directory of hook runners — git runs those
instead, and this hook is reached only because they chain to it. They do not forward
the ref list, so it falls back to judging by `HEAD`, and a push whose destination
*is* `develop` or `main` while `HEAD` is on your feature branch is then allowed
through. On such a machine, treat the hook as a reminder rather than a guard. `make
install-git-hooks` tells you when it detects the redirect.

One refusal you may see on any machine, with no redirect involved: git also supplies
no ref list for a push that has **nothing to send**, so an up-to-date `git push` made
while `HEAD` happens to sit on `develop` or `main` is refused. Every refusal says
which basis it used, so you can tell that case from a real one — and the override
gets you past it.

### Where the domain conventions live

The per-domain conventions, checklists and gotchas are in **`.claude/skills/`**.
They are written for a coding assistant but they are the real conventions, and
reading the one covering your change is the fastest way to avoid a review
round-trip:

| Skill file | Read it when you are changing |
|---|---|
| `.claude/skills/backend-lambda.md` | Lambda handlers or `idp_common` Python code |
| `.claude/skills/frontend-ui.md` | React / TypeScript / Cloudscape UI code |
| `.claude/skills/infrastructure.md` | CloudFormation or SAM templates, nested stacks, GovCloud, log-group naming |
| `.claude/skills/extraction-pipeline.md` | The processing pipeline, configuration, or agentic extraction |
| `.claude/skills/testing-qa.md` | Tests — pytest patterns, `moto`, conftest layout |
| `.claude/skills/documentation.md` | Documentation, the CHANGELOG, or the docs site |
| `.claude/skills/code-review.md` | Anything — it is the self-review checklist to run before you push |

`.claude/skills/` is **canonical**. The Cline assistant's `.cline/skills/` files
are **symlinks** to them (under different filenames, e.g. `.cline/skills/docs.md`
→ `.claude/skills/documentation.md`), so editing a `.claude` skill updates both.
**Do not create a parallel copy under `.cline/skills/`** — add a symlink instead:

```bash
ln -s ../../.claude/skills/<name>.md .cline/skills/<name>.md
```

On a clone with `core.symlinks=false` (some Windows setups) those symlinks
materialize as text files containing a path. If a skill file looks like a bare
path, run `git config core.symlinks true` and re-checkout.

### Documentation lives in two tiers

Both tiers are real documentation and they cover different things:

1. **User and feature docs** — `docs/*.md`, published to the
   [Starlight documentation site](https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws/).
   Every file needs YAML frontmatter with a `title`, the copyright and SPDX
   lines, and an H1 matching the title.
2. **Developer and module docs** — `lib/idp_common_pkg/**/README.md`, one per
   `idp_common` subpackage. These are the canonical home for library and client
   behavior and are *not* on the published site.

**A change to `idp_common` behavior usually needs both:** the module README for
the API detail, and any matching `docs/*.md` page that summarizes it. It is easy
to update one and forget the other, so check both. Adding or changing a
selectable Bedrock model touches many more files than you would expect — work
through the checklist in `.claude/skills/documentation.md` rather than from
memory.

To preview the site locally:

```bash
make docs-setup     # one-time: symlinks + npm install
make docs           # build and serve at http://localhost:4321
```

### CHANGELOG entries

Any user-visible change needs one entry in `CHANGELOG.md` under
**`[Unreleased]`**, in the right section — `### Added`, `### Changed`, or
`### Fixed`. Do not add a version number or a date; the maintainers relabel the
section at release time.

Write the entry for someone deciding whether to upgrade: say what now works or
behaves differently, name any upgrade-visible consequence, link the relevant
`docs/*.md` page, and reference the issue or PR number. A one-line entry that
only names a file is not useful to that reader.

## The local gate set

Two commands cover most of it: **`make lint` and `make test`**, which
`make all` runs together. `make help` prints every target with its description.
The rest of this section is about which parts are fast enough to run constantly
and which to save for just before you open the PR.

Timings below were measured on a developer workstation against a clean
`develop`; treat them as orders of magnitude rather than promises.

### Before every commit

These are all seconds, so there is no reason not to run them:

```bash
make fastlint        # ~4s  — ruff lint + format, ARN partitions, filtered scans,
                     #        data-plane tags, buildspec validation
```

`make fastlint` is `make lint` minus the three slow members: `cfn-lint`,
`ui-lint` and `codegen-check`. Its individual pieces are also available on their
own — `make ruff-lint` (auto-fixing lint) and `make format` (formatter) each
finish in well under a second, and `ruff` is configured in `ruff.toml` targeting
Python 3.12 with a line length of 88.

Two things about that configuration are worth knowing before you rely on it.
`ruff.toml`'s `line-length = 88` is a **formatter** setting, not an enforced
check: `E501` is not in the `[lint] select` list, so `ruff check` passes over
thousands of lines longer than 88 columns that the formatter chose not to split
(the longest currently in the linted set is 585 characters).

The second is that `ruff` still does not read everything, and what it skips is
now a **named list of individual files** rather than a directory. Any file you
add, anywhere in the repository, is linted and format-checked from the moment it
exists. The files that are skipped are the ones that already carried findings
when the exclusions were narrowed: `ruff.toml`'s `[lint] exclude` names 84 files
holding 193 pre-existing findings, and `[format] exclude` names 180 files that
`ruff format` has never been run over. Both counts fall as files are paid off, and
`scripts/tests/test_contributing_doc.py` reads them out of
`scripts/lint_debt.json`, so they cannot drift from it. Two further entries in the
top-level
`extend-exclude` are scope decisions rather than debt — the vendored
`pii-anonymizer` tree, and `**/*.ipynb`, because `E402`/`F811`/`I001` describe a
module and a notebook is a document. Both arrays and both scope entries are
generated from `scripts/lint_debt.json`.

**A listed file is not unexamined forever.** `make check-lint-debt` (part of
`make lint`, `make fastlint` and `make lint-cicd`, so both CIs run it) re-measures
every tracked `.py` file with the exclusions bypassed and fails if a listed file
has *gained* a finding, if it is now clean and should be delisted, or if the path
has gone. So if you touch one of those files and introduce a new `F401`, you will
hear about it there even though `ruff check` itself stays quiet on that file.

Two practical consequences:

- To see whether a specific file is read by either gate, ask:

  ```bash
  python3 scripts/check_lint_debt.py --explain <file>
  ```

  **Do not ask `ruff`.** Every ruff-native probe misreports at least one class of
  file here, and the misleading probe is the reason the original gap survived
  inspection for as long as it did. A plain `ruff check <file>` bypasses the
  exclusions altogether, so it reports on a file the gate never reads.
  `--force-exclude` restores only `exclude`/`extend-exclude`, which are
  *discovery* settings, while `[lint] exclude` and `[format] exclude` filter after
  discovery — so `ruff check --force-exclude <file>` prints `All checks passed!`
  and exits 0 for every lint-excluded file, and
  `ruff format --check --force-exclude <file>` prints **nothing at all** for a
  format-excluded one rather than the `No Python files found` warning that a
  discovery-level exclusion produces. `ruff check --show-files` does not honour
  `[lint] exclude` either, so a file appearing there is not evidence it is linted.
- To pay one down, fix its findings and then
  `python3 scripts/check_lint_debt.py --write`, which re-records
  `scripts/lint_debt.json` and regenerates the three arrays in `ruff.toml`. For a
  formatting entry, spell the path out — `ruff format <that path>` — because bare
  `ruff format` honours the exclusion and will skip the very file you are trying
  to fix. Never hand-edit either exclusion array. `--write` refuses to *grow*
  either list, naming the paths, unless you pass
  `--allow-new-debt "<reason>"`, which records the reason in the baseline;
  `--summary` prints the current per-tree counts.

The formatting debt is deliberately unpaid. Running `ruff format` over those 182
files is a large, mechanical, conflict-generating diff, so it belongs in its own
change rather than riding along with the one that narrowed the exclusions
([issue #975](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/975)).

Note that `make ruff-lint` and `make format` **modify your files** — they auto-fix
rather than only report. `make lint-cicd`, which is what CI runs, uses their
check-only equivalents (`ruff check`, `ruff format --check`), so a formatting
change that `make lint` silently fixed for you locally still needs committing or
CI will fail on it.

`make lint` is therefore a mixture, and worth knowing as one: its Python half fixes
(`ruff-lint`, `format`) while its UI half only reports (`ui-lint`). A `make lint` that
comes back clean may have edited your Python and will not have edited your
TypeScript. If you want the UI's auto-fixable findings applied, that is a separate
command, `make ui-lint-fix`.

**UI lint** is check-only on both paths. `make ui-lint` — reached from `make lint`
and `make lint-cicd` alike — runs `npm run lint` and `npm run typecheck` without
fixing anything, and `npm run lint` carries `--max-warnings 0`, so a `warn`-level
rule fails it too. `make ui-lint-fix` is the auto-fixing entry point: it applies
`eslint --fix` and then always re-runs the strict check, so its exit status and its
last block of output are the gate's verdict on what is left rather than the fixer's
on what it repaired. The one thing `ui-lint` itself writes is `src/ui/.checksum`,
which is how it skips itself when `src/ui` has not changed.

⚠️ **`make lint-cicd` is not check-only, and you should expect it to modify tracked
files.** It invokes `make codegen-check`, whose recipe runs `npm run codegen`
*before* comparing the result — so it regenerates everything under
`src/ui/src/graphql/generated/`. Outside CI it leaves those regenerated files in
place and prints "Generated GraphQL files were out of date — auto-updated. Please
commit the changes above"; inside CI (it branches on `$CI`/`$GITHUB_ACTIONS`) the
same difference is an error telling you to run `make codegen` and commit. It also
runs `ui-build-only`, i.e. a full `vite build`. So after a local `make lint-cicd`,
check `git status` before assuming your tree is unchanged.

### Before opening a pull request

```bash
make lint            # fastlint + cfn-lint + ui-lint + codegen-check
make test            # every offline test suite
```

or, matching CI more exactly:

```bash
make lint-cicd                                 # the target both CIs run — but see the
                                               # note below: it writes tracked files locally
make typecheck                                  # ~58s  basedpyright over the whole tree
make api-test-static                            # ~0s   authorization scan of every API operation
python3 scripts/check_first_party_deps.py       # ~0s   dependency-confusion check
python3 scripts/sdlc/validate_service_role_permissions.py   # ~5s  static IAM check, no AWS needed
make ui-test                                    # Vitest (jsdom, no browser)
```

Worth knowing about the slower members of `make lint`:

- **`make cfn-lint`** (~25s) validates every file that declares
  `AWSTemplateFormatVersion` — templates are discovered by *content*, not by
  filename, so a new template cannot slip past it. It fails on **errors only**;
  roughly 112 pre-existing advisory warnings are counted and tallied per rule
  rather than printed. `make cfn-lint-warnings` prints them in full. The linter
  is pinned (`CFN_LINT_VERSION` in the `Makefile`, currently 1.51.0) and both CI
  configs must pin the same version — `scripts/tests/test_ci_gate_parity.py`
  fails if they drift.
- **`make ui-lint`** runs ESLint plus `tsc --noEmit` over `src/ui`
  (configuration in `src/ui/eslint.config.js`, not an `.eslintrc*` file). It
  reports and does not fix, and it runs ESLint with `--max-warnings 0`, so
  anything ESLint has an opinion about fails it. It caches on a checksum and
  skips itself when `src/ui` has not changed; use `FORCE=1` to make it run
  anyway. The first run in a fresh clone includes `npm ci`, which takes minutes.
  When it reports something mechanical, `make ui-lint-fix` applies the
  auto-fixable part and re-runs the check.
- **`make codegen-check`** verifies the generated GraphQL types still match the
  schema. `make codegen` regenerates them.

### Tests

Tests use pytest markers `unit` and `integration`, registered in the repo-root
`pytest.ini`:

```python
@pytest.mark.unit          # no AWS, no network — the default expectation
@pytest.mark.integration   # requires real AWS resources
```

The repository's Python tests live in **many separate roots** — one per package
and per Lambda directory, each with its own `conftest.py` — so a bare `pytest`
from the repository root fails on colliding conftest files. `scripts/run_all_tests.py`
discovers every root and runs each as an isolated pytest invocation, which is
what `make test` calls:

```bash
make test                                    # every non-integration suite, auto-discovered
make test-list                               # which roots were discovered, and which are quarantined
cd lib/idp_common_pkg && make test-unit      # a single root, quickly
make test-integration-all                    # only integration-marked tests (needs AWS)
```

`make test-list` is the honest answer to "is my new suite actually being run?" —
`run_all_tests.py` fails if it finds a test directory registered as neither run
nor quarantined, so a new suite cannot be silently skipped.

One registered root is easy to overlook because the thing it tests is not Python:
`patterns/unified/tests` holds structural assertions about the Step Functions
definition in `patterns/unified/statemachine/workflow.asl.json` — retry policies
and failure routing that no Python test can see. It is registered only in
`scripts/run_all_tests.py`, so `make test` runs it and `make test-list` lists it,
but neither CI configuration file names it; whether a CI job reaches it depends
on which `make` target that job calls, and `make -n test-packages-cicd` will tell
you. If you edit the state machine definition, run that root.

The project's intent is that there is **no standing failure set**, so treat a
failure as a real regression until you have shown otherwise. Two things to check
before you conclude you caused it: a stale virtualenv missing the pinned
`[test]` extras is by far the most common cause of a surprising failure, and
`make test` on a clean `develop` is not always completely green — at the time of
writing, `feature-platform/pii-anonymizer/feature-api/tests` fails on an
unrelated `moto` client-ordering problem. To tell an inherited failure from a new
one, run the same root against an unmodified `develop` — a second checkout or
`git worktree add ../baseline develop` is the least disruptive way — and compare.

**[docs/testing.md](docs/testing.md) is the map of every test layer and tier in the
repository** — what each one proves, its `make` entry point, whether either CI runs
it, and where its results are recorded. It maps tiers rather than indexing test
functions, so a test added inside a suite that already runs needs no edit there; what
does need one is a new test *directory* or a suite the gate stops running, both of
which `scripts/tests/test_testing_doc.py` fails on. Read the page before adding a
test tier or concluding that something is untested.

### Security gates

Two gates, covering different things, and neither needs AWS:

```bash
make srt-setup && make srt-scan   # SAST over the checkout (Sample Security Review Tool)
make dep-audit                    # every pinned Python + Node dependency against OSV
```

`make srt-scan` needs `make srt-setup` to have run first, which downloads and
configures the tool. It is the slowest gate here — CI allows it 50 minutes — so
it is a before-the-PR command, not a per-commit one. `make srt` runs clean,
setup, scan and an optional interactive fix in sequence.

`make dep-audit` fails on HIGH or above and finishes in seconds when the
dependency manifests are already generated (`make dep-audit-fast` reuses them
explicitly). The two gates do not overlap: SRT's `syft` stage builds an SBOM —
an inventory with no vulnerability matching — so dependency CVEs are
`dep-audit`'s job alone.

Findings are triaged in place rather than waved through. A bandit finding takes
a line-scoped `# nosec <ID> - <reason>`; an unreachable advisory goes in
`scripts/security/dep_audit_allowlist.json` with a justification; a
security-matrix or Checkov finding goes in `scripts/srt/issues.json`. Adding a
suppression without a stated reason will be asked about in review. The procedure
is in `.claude/skills/srt-security-scan.md`.

### Type checking

```bash
make typecheck       # basedpyright over the whole repository — the gate, ~1 min
make typecheck-stats # the same, with per-file statistics
make typecheck-pr    # fast local check of only the files changed vs TARGET_BRANCH
```

`make typecheck` is the gate. It is what both CI systems run, and it reads
`pyrightconfig.json`'s 12-entry `include` — whose closure over every tracked
`.py` file `scripts/tests/test_pyright_config.py` derives from `git ls-files`. The
number of files it analyses is exactly `git ls-files '*.py' | wc -l`, and exactly
the `filesAnalyzed` it reports; run either if you want the figure, because it grows
with the tree. It takes **about a minute** through `make` (48–60 s measured across
several trees; the bare `basedpyright` binary is ~47 s, but the `make` figure is the
one CI pays).

It also resolves this repository's own packages, via `pyrightconfig.json`'s
`extraPaths`. That matters more than it sounds: without it `idp_common` did not
resolve, `reportMissingImports` is configured `"none"`, and so **no call into the
shared library could produce a diagnostic** — the gate read every file and proved
much less than that suggests. If you add a `lib/<something>` distribution, add its
package root to `extraPaths`; the suite fails until you do.

Errors fail it and warnings do not. To see today's warning split, run
`make typecheck` and read the tally it prints rather than a list written here — it
moves as files are added. Two things about it that do not move: `reportCallIssue` and
`reportReturnType` are **errors** repo-wide and sit at zero, and the handful still
reported as *warnings* come from the vendored `feature-platform/pii-anonymizer` tree,
which is relaxed to warning level on purpose because its annotations are upstream's
to fix.

`make typecheck-pr` is a **convenience, not a gate**. It narrows `basedpyright`
to the files you are editing so the answer comes back in a second or two, which
is worth having in an inner loop. What it cannot do is see a break your change
caused in a file it did not select — change a signature and the error appears at
the callers, which are usually outside the diff — so a green `typecheck-pr` is
not a substitute for `make typecheck` before you push. It defaults to comparing
against `develop`; override with `make typecheck-pr TARGET_BRANCH=<branch>`.

All three targets need `basedpyright` on `PATH`, and neither `make setup` nor
`make setup-venv` installs it; `npm install -g basedpyright`, which is what both
CI systems do, is the command that supplies it. Note that `make typecheck-pr`
exits 0 without `basedpyright` present when your branch changes no Python files
at all, so a documentation-only branch will not tell you the tool is missing.
`make typecheck` always tries to run it and so always says.

## What CI runs on your pull request

Both CI systems run the **same** non-integration gate set, and
`scripts/tests/test_ci_gate_parity.py` fails the build if a gate appears in one
and not the other, if `lint-cicd` becomes weaker than local `make lint`, or if
the `cfn-lint` pin drifts between the `Makefile` and either config.

**GitHub Actions** (`.github/workflows/`), on every pull request:

| Workflow | Job | What it runs |
|---|---|---|
| `developer-tests.yml` | `developer_tests` | `make lint-cicd`, `scripts/check_first_party_deps.py`, `make api-test-static`, `scripts/sdlc/validate_service_role_permissions.py`, `make typecheck`, `make test-cicd -C lib/idp_common_pkg`, `make test-packages-cicd`, and the UI `npx vitest run` |
| `security-checks.yml` | `srt_security_review` | `make srt-setup` then `make srt-scan` |
| `security-checks.yml` | `dep_audit` | `scripts/security/dep_audit.py` |
| `build-docs.yml` | `build` | Builds the Starlight site — only when the PR touches `docs/**`, `docs-site/**` or `images/**` |
| `generate-dep-manifest.yml` | `generate-manifests` | `make dep-manifest` — only when the PR touches a dependency manifest input |

**GitLab CI** (`.gitlab-ci.yml`) runs the same set in its `fast_checks` stage —
`code_checks`, `srt_security_review` and `dep_audit` — plus two jobs that need
real AWS credentials and therefore cannot run on GitHub: `deployment_validation`
and `integration_tests`, the latter deploying a stack and driving fourteen
end-to-end steps through it. **A change merged through a GitHub pull request has
not run the integration suite.** Its per-step detail is in
`scripts/sdlc/docs/CI_TEST_COVERAGE.md`.

Two asymmetries are worth stating plainly, because both have caused a gate to be
skipped in the past:

- ⚠️ **GitHub's quality and security workflows are `pull_request`-only.** A
  direct push to `develop` runs no lint, type check, test, SRT or dependency
  audit on GitHub. (The two path-filtered workflows above do have `push:`
  triggers, but neither runs any of those gates.) GitLab runs `code_checks` on
  **every push** as well as on merge requests, so the same push is not
  unchecked everywhere — it is simply unchecked on the side most contributors
  are looking at. Work through a pull request.
- **A check being visible is not the same as it being blocking.** Whether each
  of these is a *required* status check on `develop` is a branch-protection
  setting, not something this repository can assert.

### Coverage is reported, not enforced

Both CIs publish a coverage report — GitHub as a job-summary table and a badge,
GitLab as a Cobertura artifact — and **neither fails a build on it.** GitHub's
step sets `fail_below_min: false` deliberately; the `thresholds: "60 80"` beside
it colours the badge and gates nothing.

That is a considered position rather than an oversight, and the reason is scope:
coverage is measured for `lib/idp_common_pkg` alone, which is one of the ~64 test
roots `scripts/run_all_tests.py` discovers. A repository-wide minimum computed
from it would be a number about one package presented as a number about the
repository — and it would move whenever that package's share of the code changed,
with no relation to whether a pull request was tested. So there is no percentage
to satisfy here: what review asks for is a test that would have caught the bug,
which is a question about your change and not about a ratio.

Widening the measurement is the prerequisite for making it a gate, in that order.

## Pull request process

1. **Target `develop`.** Not `main`.
2. **Run the gates.** `make lint` and `make test` at minimum; the CI-matching
   list above if your change touches templates, the UI, dependencies, or
   authorization.
3. **Update the documentation your change affects** — both tiers if you touched
   `idp_common`, and `README.md` if you added something significant.
4. **Add one `CHANGELOG.md` entry** under `[Unreleased]` in the right section.
5. **Write a description a reviewer can act on:** what the change does, why it
   is needed, how you verified it, and anything you deliberately left out. If
   you could not run a gate — no AWS account for an integration test, for
   instance — say so rather than leaving it ambiguous.
6. **Use a conventional-commit subject** (`fix:`, `feat:`, `docs:`,
   `build(deps):`), matching the existing history.
7. **Link the issue** the PR closes (`Fixes #123`).
8. **Respond to review feedback** by pushing follow-up commits to the same
   branch; CI re-runs on each push.

A note on CI status: if GitHub reports "no checks reported" after you push, the
usual cause is that the PR has become unmergeable against `develop`. Merge
`develop` into your branch and push again.

There is no Contributor License Agreement for this project; it is licensed
MIT-0 (see `LICENSE`), and your pull request is contributed under those terms.

## Make target reference

`make help` prints the current, authoritative list with descriptions grouped by
category. The tables below cover the targets a contributor most often needs;
targets that deploy or modify AWS resources are deliberately omitted here and
documented in [docs/deployment.md](docs/deployment.md) and
[docs/idp-cli.md](docs/idp-cli.md).

### Setup

| Command | Description |
|---|---|
| `make setup` | Install all first-party packages into the current Python environment |
| `make setup-venv` | Create `.venv` and install everything into it |
| `make install-first-party` | Install just the first-party editables in one pip pass |

### Code quality

| Command | Description |
|---|---|
| `make all` | `lint` + `test` (the default target) |
| `make lint` | Everything: ruff, format, ARN partitions, filtered scans, data-plane tags, buildspec, `cfn-lint`, UI lint, codegen check |
| `make fastlint` | `lint` without `cfn-lint`, UI lint, or codegen check |
| `make check-lint-debt` | Re-measure `ruff.toml`'s per-file exclusions against the tree (part of `lint`, `fastlint` and `lint-cicd`) |
| `make lint-cicd` | `lint`'s set plus `ui-build-only`, which `lint` does not run — what both CIs run. Check-only for Python and the UI linter, but `codegen-check` regenerates `src/ui/src/graphql/generated/` and leaves it regenerated outside CI |
| `make ruff-lint` | Ruff lint with auto-fix |
| `make format` | Ruff formatter |
| `make cfn-lint` | Validate every CloudFormation template (fails on errors) |
| `make cfn-lint-warnings` | The same, listing every advisory warning in full |
| `make check-arn-partitions` | Reject hardcoded ARN partitions and service principals (GovCloud) |
| `make validate-buildspec` | Validate the CodeBuild buildspec files |
| `make codegen-check` | Verify generated GraphQL types are current (`make codegen` regenerates) |

### Types and tests

| Command | Description |
|---|---|
| `make typecheck` | basedpyright over the whole repository — the CI gate |
| `make typecheck-pr` | basedpyright on files changed vs `TARGET_BRANCH` (default `develop`) — fast local feedback, not a gate |
| `make test` | Every offline test suite, auto-discovered |
| `make test-list` | List the discovered and quarantined test roots |
| `make test-integration-all` | Integration-marked tests only (**requires AWS**) |
| `make api-test-static` | Static authorization scan of every API operation |
| `make ui-test` | UI unit tests (Vitest, jsdom) |

### Security

| Command | Description |
|---|---|
| `make srt-setup` | Download and configure the security review tool |
| `make srt-scan` | Run the SAST assessment |
| `make srt` | Clean, setup, scan, then optionally fix |
| `make dep-audit` | Audit every pinned Python and Node dependency against OSV |
| `make dep-audit-fast` | The same, reusing existing manifests |

### UI and documentation

| Command | Description |
|---|---|
| `make ui-start STACK_NAME=<name>` | Run the UI dev server against a deployed stack |
| `make ui-lint` | ESLint (`--max-warnings 0`) + `tsc --noEmit`, check-only (checksum-cached; `FORCE=1` to override) |
| `make ui-lint-fix` | `eslint --fix` over `src/ui`, then re-runs `ui-lint` |
| `make ui-build` | Lint, typecheck, and production Vite build |
| `make docs-setup` | One-time docs site setup |
| `make docs` | Build and serve the docs site locally |

## Coding standards

**Python.** PEP 8, checked by `ruff` (`ruff.toml`), target Python 3.12. Write to
88 columns, but be aware that 88 is the *formatter's* wrapping preference and not
an enforced rule — `E501` is not among the selected lint rules, and `ruff.toml`
still excludes a named list of 84 files from the linter and 182 from the
formatter. Both caveats are explained under
[the local gate set](#before-every-commit), along with how to pay one of those
files off. Types are checked
with `basedpyright` (`pyrightconfig.json`), which is installed separately with
`npm install -g basedpyright`. Prefer adding to the narrowest `idp_common` extra
rather than to `core`.

**JavaScript and TypeScript.** ESLint, configured in `src/ui/eslint.config.js`;
`npm run lint` inside `src/ui`, or `make ui-lint` from the root. Prettier
configuration is in `src/ui/.prettierrc`. The UI uses React with the Cloudscape
Design System, Vite, and AWS Amplify v6.

**Commits.** Conventional-commit subjects. Each Git-tracked change should be
explainable on its own.

**Comments and docstrings.** Explain non-obvious logic and the reason a
constraint exists, which is the part a future reader cannot recover from the
code.

## AWS-specific considerations

**GovCloud compatibility is enforced, not aspirational.** In CloudFormation and
in Python, write `arn:${AWS::Partition}:` rather than `arn:aws:`, and
`${AWS::URLSuffix}` rather than `amazonaws.com`. `make check-arn-partitions`
fails the build on violations, and it runs in both CIs via `make lint-cicd`.

**IAM.** Grant the minimum a new feature needs. Do not widen an existing
policy's resource scope as a shortcut; `scripts/tests/test_iam_privilege_escalation.py`
and the service-role validator both exist because that shortcut has been taken
before.

**Cost.** This solution calls foundation models, and an apparently small change
to a prompt, a shard size or a retry ladder can multiply the cost of processing
a document. If your change affects how many model calls happen or how large they
are, say so in the PR description.

**Region and service availability.** Not every Bedrock model is available in
every Region. If you add one, follow the model checklist in
`.claude/skills/documentation.md` — it covers the template enums, pricing,
limits, client routing, UI, both documentation tiers, and the CHANGELOG.

**Service quotas.** Bedrock and Textract throughput, Lambda concurrency, and the
IAM role-per-account limit have all been the real constraint on a change at some
point. Consider them before assuming a scaling problem is in the code.

---

Thank you for contributing to the GenAI Intelligent Document Processing
accelerator.
