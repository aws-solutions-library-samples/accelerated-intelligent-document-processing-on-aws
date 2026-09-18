Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Governance

How the GenAI Intelligent Document Processing accelerator (GenAIIDP) is run: who
decides, how a change gets accepted, and what the branch and release model is.

Companion documents: [ROADMAP.md](./ROADMAP.md) for direction and non-goals,
[SECURITY.md](./SECURITY.md) for vulnerability reporting, and
[CONTRIBUTING.md](./CONTRIBUTING.md) for the mechanics of building and submitting a
change.

## Decision making

The project operates on a single-maintainer model with subsystem reviewers.
[`.github/CODEOWNERS`](./.github/CODEOWNERS) records who reviews what and is what
GitHub reads when it requests a review, so you do not need to tag anyone yourself;
a reviewer named there is the person a change in their area is best discussed with,
and the maintainer merges. Design disagreements are settled in the issue or pull
request where they surface, in public, on the evidence presented.

The project has a strong bias toward evidence over opinion: proposals that change
extraction behaviour, cost or accuracy are expected to come with measurements, and
the repository carries the tooling to produce them (the `benchmarks/` suite and the
evaluation framework). "It should be faster" or "this is cleaner" is a hypothesis; a
benchmark run against the previous release is an argument.

## How a change gets accepted

1. **Open an issue first for anything substantial,** using the templates in
   [`.github/ISSUE_TEMPLATE/`](./.github/ISSUE_TEMPLATE/). Note the boundary
   `CONTRIBUTING.md` explains: this repository is for the accelerator, not for the
   AWS services it calls. Bedrock model quality, Textract accuracy and service
   quotas belong with AWS Support.
2. **Branch from `develop`** and target your pull request at `develop`. A pull
   request against `main` will be asked to retarget.
3. **Keep the change focused,** and update the documentation and `CHANGELOG.md`
   entry that go with it.
4. **Pass the automated checks,** which run on every pull request and are the floor,
   rather than the review. [CONTRIBUTING.md](./CONTRIBUTING.md) lists them and the
   `make` targets that reproduce them locally. Some validation needs a deployed AWS
   stack and so cannot run on a pull request; the maintainer runs those tiers before
   each release and records the result under
   [`docs/release-validation/`](./docs/release-validation/README.md).
5. **Get a review.** CODEOWNERS routes the request to the subsystem reviewer where
   one exists, and to the maintainer otherwise. Expect review comments to ask for
   evidence where behaviour changed. Maintenance is best-effort and the project
   publishes no response-time commitment; if a change is time-sensitive, say so in
   the pull request description.
6. **The maintainer merges.** Contributors do not merge their own pull requests.

## Branch and release model

`develop` is the integration branch: everything lands there first, and it is the
branch that pull requests target. `main` holds the released state — at release time
`develop` is merged into `main`, the commit is tagged `vX.Y.Z`, and the built
CloudFormation templates are published and recorded in the `## Templates` section of
that release's `CHANGELOG.md` entry, so an older release stays deployable even
though it is not maintained.

Versions follow `MAJOR.MINOR.PATCH` in the pre-1.0 range, which means the minor
number carries real behaviour changes and the `idp_common` library API is not
frozen. `CHANGELOG.md` is the contract: any change a deployed stack would notice is
described there, and upgrade-visible effects are called out explicitly.

Releases are cut when ready; there is no published release calendar. There are no
release branches, no long-term-support line and no backports — a fix lands on
`develop` and ships in the next release, and the supported version is the most
recent one (see [SECURITY.md](./SECURITY.md)). Superseded features are removed with
a note in `CHANGELOG.md` and, where the change is large, a migration guide under
`docs/`; there is no fixed deprecation window.

Each release has a validation record under
[`docs/release-validation/`](./docs/release-validation/README.md), with the security
portion curated into [`security/test-results/`](./security/README.md). Reading the
record for a release is the fastest way to judge whether to upgrade.

## Sibling implementations

The same solution is available in two other forms, each separately maintained by its
own team, in its own GitHub organization, with its own issue tracker and release
schedule:

- [cdklabs/genai-idp](https://github.com/cdklabs/genai-idp) — AWS CDK constructs.
- [awslabs/genai-idp-terraform](https://github.com/awslabs/genai-idp-terraform) —
  a Terraform module.

This repository is the CloudFormation and SAM implementation. Behaviour of the CDK
constructs or the Terraform module belongs in that repository's tracker; behaviour
of the document processing itself — the pipeline, the prompts, the `idp_common`
library, the evaluation framework — belongs here even if you encountered it through
a sibling, because that is where the code lives.

## Changing this document

Changes to it are pull requests like any other.
