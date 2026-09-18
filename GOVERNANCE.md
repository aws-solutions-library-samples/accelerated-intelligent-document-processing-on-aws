Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Governance

This document describes how the GenAI Intelligent Document Processing accelerator
(GenAIIDP) is run: who decides, how a change gets from a proposal to a published
release, what the branch and release model is, and how this repository relates to
its CDK and Terraform siblings. The goal is that a contributor can predict what
will happen to their pull request before opening it.

Companion documents: [MAINTAINERS.md](./MAINTAINERS.md) for who reviews what,
[ROADMAP.md](./ROADMAP.md) for direction and non-goals,
[SECURITY.md](./SECURITY.md) for vulnerability reporting, and
[CONTRIBUTING.md](./CONTRIBUTING.md) for the mechanics of setting up, building and
submitting a change.

## Decision making

The project operates on a single-maintainer model with subsystem reviewers. Bob
Strahan ([@rstrahan](https://github.com/rstrahan)) is the primary maintainer: he
takes the final decision on what is accepted, performs the last review, and cuts
and publishes releases. Several subsystems have their own reviewers, listed in
MAINTAINERS.md and encoded in [`.github/CODEOWNERS`](./.github/CODEOWNERS), whose
judgement carries the most weight in their area.

Design disagreements are settled in the issue or pull request where they surface,
in public, on the evidence presented. The project has a strong bias toward evidence
over opinion: proposals that change extraction behaviour, cost or accuracy are
expected to come with measurements, and the repository carries the tooling to
produce them (the `benchmarks/` suite, the evaluation framework, and the live-stack
tiers recorded in
[`docs/release-validation/`](./docs/release-validation/README.md)). "It should be
faster" or "this is cleaner" is a hypothesis; a benchmark run against the previous
release is an argument.

Larger pieces of work are sometimes written up first as a plan under
[`docs/planning/`](./docs/planning/) — a problem statement verified against
`develop`, the evidence, and a sequenced set of pull requests — and then
implemented in that order. That is the closest thing the project has to a design
review, and it is used when a change is risky or spans several releases, not for
routine work.

## How a change gets accepted

1. **Open an issue first for anything substantial.** Bug reports and feature
   requests use the templates in
   [`.github/ISSUE_TEMPLATE/`](./.github/ISSUE_TEMPLATE/). `CONTRIBUTING.md`
   explains the one boundary that matters most: this repository is for the
   accelerator, not for the AWS services it calls. Bedrock model quality, Textract
   accuracy, service quotas and throttling belong with AWS Support.
2. **Branch from `develop`** using a `feature/`, `fix/` or `docs/` prefix, and
   target your pull request at `develop`. A pull request against `main` will be
   asked to retarget.
3. **Keep the change focused,** and update the documentation that goes with it.
   Documentation lives in two tiers and both matter: user and feature docs under
   `docs/` (published to the documentation site) and developer/module docs in
   `lib/idp_common_pkg/**/README.md`. User-visible changes also need a
   `CHANGELOG.md` entry under `[Unreleased]`.
4. **Pass the automated checks.** They run on every pull request and are the
   floor, not the review:
   - `Lint, Type Check, and Test` — ruff lint and format checks, UI lint and
     build, `cfn-lint`, buildspec validation, ARN-partition and data-plane-tag
     checks, type checking of changed files, the Python and package test suites,
     the UI unit tests, the static API RBAC scan, the first-party dependency
     resolution check and CloudFormation service-role permission validation.
   - `SRT Security Review` — the Sample Security Review Tool static scan.
   - `Dependency Audit (SCA)` — the dependency audit against the OSV database.

   You can reproduce all of them locally with `make lint-cicd`,
   `make typecheck-pr`, `make test`, `make srt-scan` and `make dep-audit`. Some
   validation needs a deployed AWS stack and so cannot run on a pull request; the
   maintainer runs those tiers before each release and records the result under
   [`docs/release-validation/`](./docs/release-validation/README.md).
5. **Get a review.** CODEOWNERS routes the request to the subsystem reviewer where
   one exists, and to the primary maintainer otherwise. Expect review comments to
   be specific and to ask for evidence where behaviour changed.
6. **The maintainer merges.** Contributors do not merge their own pull requests.

## Branch and release model

`develop` is the integration branch. Everything lands there first, and it is the
branch that pull requests target. `main` holds the released state: at release time
`develop` is merged into `main`, the commit is tagged `vX.Y.Z`, and the built
CloudFormation templates are published to the three public regions the project
supports (`us-west-2`, `us-east-1`, `eu-central-1`). The template URLs for every
release are recorded in the `## Templates` section of that release's
`CHANGELOG.md` entry, so an older release stays deployable even though it is not
maintained.

Versions follow `MAJOR.MINOR.PATCH` in the pre-1.0 range, which means the minor
number carries real behaviour changes and the `idp_common` library API is not
frozen. `CHANGELOG.md` is the contract: any change a deployed stack would notice is
described there, and upgrade-visible effects are called out explicitly in the entry
rather than left to be discovered.

Cadence is release-when-ready, and in practice that has meant roughly every one to
two weeks. There is no published release calendar. There are no release branches,
no long-term-support line and no backports: a fix lands on `develop` and ships in
the next release, and the supported version is the most recent one (see
[SECURITY.md](./SECURITY.md)).

Before a release is published, the maintainer runs a battery of tiers that need a
live deployed stack — security scans against a running API, deploy-variant and
template-transform deploys, an in-place upgrade from the previous release, a
browser-driven UX review and an accuracy/cost benchmark against the previous
release. The result is written up, once per release and never overwritten, in
[`docs/release-validation/`](./docs/release-validation/README.md), with the
security portion curated into [`security/test-results/`](./security/README.md).
Those records are the project's public evidence that a release was actually
exercised, and reading the one for a release is the fastest way to judge whether to
upgrade.

## Relationship to the CDK and Terraform siblings

The same solution is available in two other forms, in other GitHub organizations:

- [cdklabs/genai-idp](https://github.com/cdklabs/genai-idp) — AWS CDK constructs.
- [awslabs/genai-idp-terraform](https://github.com/awslabs/genai-idp-terraform) —
  a Terraform module.

This repository is the CloudFormation and SAM implementation and, in practice, the
reference one: it is where features are designed and first shipped, and the sibling
ports track its release train. Each sibling is separately maintained by its own team
in its own organization, with its own issue tracker, review process and release
schedule. This project does not gate its releases on theirs, and it cannot accept
changes to their code.

Where to file, therefore: behaviour of the CDK constructs or the Terraform module
belongs in that repository's tracker; behaviour of the document processing itself —
the pipeline, the prompts, the `idp_common` library, the evaluation framework —
belongs here even if you encountered it through a sibling, because that is where
the code lives. There is no published compatibility matrix stating which sibling
version corresponds to which release of this project; if you need a guarantee about
sibling parity, ask in an issue.

## Questions this document does not answer

Stating these explicitly is more useful than filling them with plausible fiction.
Each is genuinely undefined today, and any of them may be defined later.

- **Adding or retiring a maintainer.** There is no nomination or promotion
  process. MAINTAINERS.md records who is doing the work, and changes when that
  changes.
- **Escalation.** There is no tie-break body above the primary maintainer, and no
  appeal route for a rejected change beyond continuing the discussion.
- **Response times.** The project publishes no acknowledgement or triage
  commitment for issues, pull requests or discussions.
- **Deprecation policy.** Superseded features are removed with a note in
  `CHANGELOG.md` and, where the change is large, a migration guide under `docs/`.
  There is no fixed deprecation window.
- **Governance of this document.** Changes to it are pull requests like any other.
