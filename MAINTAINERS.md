Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Maintainers

This page says who looks after the GenAI Intelligent Document Processing
accelerator. It is the human-readable companion to
[`.github/CODEOWNERS`](./.github/CODEOWNERS), which is what GitHub reads when it
decides whom to ask for a review. If the two ever disagree, CODEOWNERS is the one
with effect — please fix both in the same pull request.

For how decisions get made and how a change gets accepted, see
[GOVERNANCE.md](./GOVERNANCE.md). For where the project is going, see
[ROADMAP.md](./ROADMAP.md). For reporting a security problem, see
[SECURITY.md](./SECURITY.md).

## Primary maintainer

**Bob Strahan** ([@rstrahan](https://github.com/rstrahan)) maintains the project.
He performs the final review on incoming changes, cuts and publishes the releases,
and is the default owner in CODEOWNERS for any path without a more specific rule.

## Reviewers

Several subsystems have contributors who know them best and who are the right
reviewers for changes in their area. `.github/CODEOWNERS` routes review requests
to them automatically — you do not need to tag anyone yourself:

- [@kaleko](https://github.com/kaleko)
- [@webarch-ai](https://github.com/webarch-ai)
- [@Behrad-Gh](https://github.com/Behrad-Gh)
- [@tomron-aws](https://github.com/tomron-aws)

A reviewer is the person a change in their area should be discussed with, and the
person best placed to say whether a proposed change is a good idea. They are not
gatekeepers with a veto: the primary maintainer merges, and where a reviewer and
the maintainer see a change differently they work it out in the pull request.

This list tracks who is actually doing the work and is updated when that changes.
Contributions from outside it are equally welcome — see
[ROADMAP.md](./ROADMAP.md) for where help is most useful.

## Response expectations

The project does not publish a response-time commitment for issues or pull
requests. Maintenance is best-effort: pull requests are typically reviewed within
the current release cycle, but nothing here is a service-level agreement. If a
change is time-sensitive, say so in the pull request description.

Concierge support for customization, deployment, and integration of production use
cases is available through
[AWS Professional Services](https://aws.amazon.com/professional-services/), which
is a separate path from this repository's best-effort maintenance.
