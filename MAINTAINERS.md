Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Maintainers

This page says who looks after which part of the GenAI Intelligent Document
Processing accelerator, and where that answer comes from. It is the human-readable
companion to [`.github/CODEOWNERS`](./.github/CODEOWNERS), which is what GitHub
actually reads when it decides whom to ask for a review. If the two ever disagree,
CODEOWNERS is the one with effect and this page is the one that is wrong — please
fix both in the same pull request.

For how decisions get made and how a change gets accepted, see
[GOVERNANCE.md](./GOVERNANCE.md). For where the project is going, see
[ROADMAP.md](./ROADMAP.md). For reporting a security problem, see
[SECURITY.md](./SECURITY.md).

## Primary maintainer

**Bob Strahan** ([@rstrahan](https://github.com/rstrahan)) maintains the project.
He authored the majority of the history in nearly every directory, performs the
final review on incoming changes, cuts and publishes the releases, and is the
default owner in CODEOWNERS for anything not listed in the table below. In
practice this is a single-maintainer project with subsystem specialists rather
than a committee; GOVERNANCE.md describes what that means for a contributor.

## Subsystem owners

Ownership below was measured from the commit history rather than assigned. For
each candidate subsystem we counted per-author file revisions over the whole
history (`git log --format='%an' -- <path> | sort | uniq -c | sort -rn`) and
listed a subsystem here when a named engineer other than the primary maintainer
wrote the majority, or a near-majority co-share, of it. Percentages are shares of
file revisions touching that path, which is a proxy for sustained involvement and
not a measure of contribution value.

| Subsystem | Path(s) | Owner | Share of history |
|---|---|---|---|
| Agent framework and agent tooling (Agent Analysis, Agent Chat, Code Intelligence, MCP tools) | `lib/idp_common_pkg/idp_common/agents/`, `lib/idp_common_pkg/tests/unit/agents/` | David Kaleko ([@kaleko](https://github.com/kaleko)) and Mansoor Malik ([@webarch-ai](https://github.com/webarch-ai)) | 40% and 29% respectively; @rstrahan 6% |
| Business rule validation, including the Z3 solver path | `lib/idp_common_pkg/idp_common/rule_validation/`, `docs/rule-validation*.md` | Behrad Gharedaghloo ([@Behrad-Gh](https://github.com/Behrad-Gh)) | 77% |
| Python SDK | `lib/idp_sdk/` | co-owned by @rstrahan (46%) and Mansoor Malik ([@webarch-ai](https://github.com/webarch-ai), 39%) | — |
| Workshop content | `workshop/` | Tom Ron ([@tomron-aws](https://github.com/tomron-aws)) | 100% |

Everything else — the unified pattern and its Step Functions workflow, the
CloudFormation templates, the web UI, the CLI, extraction and confidence, the
evaluation and Test Studio surface, the feature platform, benchmarks, security
artifacts and documentation — sits with the primary maintainer under the default
CODEOWNERS rule. That is a description of the current state, not an invitation to
leave those areas alone: see ROADMAP.md for where help is most useful.

## Subsystems with a known owner but no resolvable GitHub username

Four paths are clearly majority-owned by one engineer, but that engineer's commits
carry a corporate email address that is not attached to any GitHub account. The
GitHub API returns no `author.login` for those commits and the authors do not
appear in the repository's contributor list at all, so there is no evidence-based
way to derive their handles. Guessing would be worse than omitting them: a wrong
handle in CODEOWNERS routes review requests to nobody, or to an unrelated person
with a similar name, and does so silently.

| Path(s) | Principal author (git) | Share of history |
|---|---|---|
| `lib/idp_common_pkg/idp_common/synthesis/`, `lib/idp_common_pkg/tests/unit/synthesis/` | Jeremy Feldman | 86% / 63% |
| `feature-platform/idp-data-generator/`, `docs/extensions/idp-data-generator.md` | Jeremy Feldman | 86% / 100% |
| `publish.py` | Taniya Mathur | 70% |

These paths currently fall to the default owner. To fix, add the correct handles
to CODEOWNERS and to this table together. Note that by commit count Taniya Mathur
and Jeremy Feldman are the second and third largest contributors to the project as
a whole, so this gap affects attribution well beyond CODEOWNERS routing.

## What being a maintainer means here

A subsystem owner is the person a change in that area should be reviewed by, and
the person best placed to say whether a proposed change is a good idea. Owners are
not gatekeepers with a veto and there is no formal escalation path: the primary
maintainer merges, and where an owner and the maintainer disagree they work it out
in the pull request. There is no formal process today for adding or retiring a
maintainer — the table above tracks who is actually doing the work, and is updated
when that changes.

## Response expectations

The project does not publish a response-time commitment for issues or pull
requests, and this page will not invent one. Recent history is the honest guide: a
release goes out roughly every one to two weeks and pull requests are typically
reviewed within that cycle, but nothing here is a service-level agreement. If a
change is time-sensitive, say so in the pull request description.

Concierge support for customization, deployment, and integration of production use
cases is available through
[AWS Professional Services](https://aws.amazon.com/professional-services/), which
is a separate path from this repository's best-effort maintenance.
