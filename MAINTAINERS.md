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

Ownership below was measured from the commit history rather than assigned. A
subsystem is listed here when a named engineer other than the primary maintainer
wrote the majority, or a near-majority co-share, of it.

Every percentage on this page is a share of that path's **file revisions** —
(commit × file) pairs — which is a proxy for sustained involvement and not a
measure of contribution value. Each one reproduces from this command, run once
per path:

```bash
git log --full-history --pretty=format:'@@A@@%an' --name-only -- <path> \
  | sed 's/ \[C\]//' \
  | awk '/^@@A@@/{a=substr($0,6); next} NF{n[a]++; t++}
         END{for (k in n) printf "%5.1f%%  %4d/%d  %s\n", 100*n[k]/t, n[k], t, k}' \
  | sort -rn
```

Three details of it are load-bearing, and the figures do not reproduce without
them:

- **`--full-history`.** Git's default history simplification prunes commits that
  reached the path through a merge. Omitting the flag silently changes the answer
  for five of the paths on this page — `publish.py` reads 50% rather than 70%,
  and the agent framework reads 34%/31%/6% rather than 40%/30%/5% — and it
  makes a path's number depend on how many pathspecs you happened to pass in the
  same invocation. With the flag, a path measures the same either way. No path
  here needs `--follow`; none of them was renamed.
- **File revisions rather than commits.** A plain commit count
  (`git log --format='%an' -- <path> | sort | uniq -c | sort -rn`) reproduces
  none of these figures, because the authors differ sharply in commit size. The
  Python SDK row below shows both metrics precisely because they disagree there:
  39% of file revisions against 14% of commits for the same person.
- **The `sed`,** which merges the corporate-alias git identity
  `Mansoor [C] Malik` into `Mansoor Malik`. Both carry the same commit email, so
  leaving them split understates that author on every path he touched. The same
  `[C]` alias form exists for several other contributors.

Merge commits contribute no file revisions, because `--name-only` prints no diff
for them.

| Subsystem | Path(s) | Owner | Share of history |
|---|---|---|---|
| Agent framework and agent tooling (Agent Analysis, Agent Chat, Code Intelligence, MCP tools) | `lib/idp_common_pkg/idp_common/agents/`, `lib/idp_common_pkg/tests/unit/agents/` | David Kaleko ([@kaleko](https://github.com/kaleko)) and Mansoor Malik ([@webarch-ai](https://github.com/webarch-ai)) | 40% and 30% of the module's 625 file revisions; @rstrahan 5% |
| Business rule validation, including the Z3 solver path | `lib/idp_common_pkg/idp_common/rule_validation/`, `docs/rule-validation*.md` | Behrad Gharedaghloo ([@Behrad-Gh](https://github.com/Behrad-Gh)) | 77% of the module's 78 file revisions |
| Python SDK | `lib/idp_sdk/` | co-owned by @rstrahan and Mansoor Malik ([@webarch-ai](https://github.com/webarch-ai)) | 46% / 39% of 660 file revisions; 63% / 14% of 196 commits |
| Workshop content | `workshop/` | Tom Ron ([@tomron-aws](https://github.com/tomron-aws)) | 100% of 53 file revisions |

The Python SDK row carries both metrics because they disagree there more than
anywhere else, and only one of the two readings would be misleading on its own.
Mansoor Malik's 27 commits to `lib/idp_sdk/` carry 255 file touches between them
— about nine files per commit, against the primary maintainer's two and a half —
so a commit count makes him look like an occasional contributor at 14% while the
work itself is a co-owner's share at 39%. The routing decision is the same under
either reading, which is why the line asks both people.

Everything else — the unified pattern and its Step Functions workflow, the
CloudFormation templates, the web UI, the CLI, extraction and confidence, the
evaluation and Test Studio surface, the feature platform, benchmarks, security
artifacts and documentation — sits with the primary maintainer under the default
CODEOWNERS rule. That is a description of the current state, not an invitation to
leave those areas alone: see ROADMAP.md for where help is most useful.

## Subsystems with a known owner but no resolvable GitHub username

Five paths, in the three groups below, are clearly majority-owned by one of two
engineers — Jeremy Feldman for four of them, Taniya Mathur for `publish.py` — but
both engineers' commits carry a corporate email address that is not attached to
any GitHub account. The GitHub API returns no `author.login` for those commits and
neither author appears in the repository's contributor list at all, so there is no
evidence-based way to derive their handles. Guessing would be worse than omitting
them: a wrong handle in CODEOWNERS routes review requests to nobody, or to an
unrelated person with a similar name, and does so silently.

| Path(s) | Principal author (git) | Share of history |
|---|---|---|
| `lib/idp_common_pkg/idp_common/synthesis/`, `lib/idp_common_pkg/tests/unit/synthesis/` | Jeremy Feldman | 86% of 28 file revisions / 63% of 35 |
| `feature-platform/idp-data-generator/`, `docs/extensions/idp-data-generator.md` | Jeremy Feldman | 86% of 128 file revisions / 100% of 7 |
| `publish.py` | Taniya Mathur | 70% of 162 file revisions |

These paths currently fall to the default owner. To fix, add the correct handles
to CODEOWNERS and to this table together. Note that by commit count Taniya Mathur
(726) and Jeremy Feldman (301) are the second and third largest contributors to
the project as a whole, out of 8,965 commits, so this gap affects attribution well
beyond CODEOWNERS routing.

## CODEOWNERS routing depends on write access

A review request is only routed to a user who has **write access** to this
repository. GitHub silently ignores a CODEOWNERS entry for anyone with read
access only: the line stays in the file, the pattern still matches, and the
review request simply goes to the remaining owners instead — with no warning
anywhere. Because every specific line in `.github/CODEOWNERS` also names
`@rstrahan`, that failure mode looks exactly like normal routing to the default
owner, which is why it can persist unnoticed.

So each handle named in CODEOWNERS must hold write access for the line that names
it to do anything. Check the effective permission — which includes access derived
from organization team membership, not just direct collaborator grants — with:

```bash
gh api repos/<owner>/<repo>/collaborators/<handle>/permission --jq .permission
```

If that returns `read` for a named owner, the corresponding CODEOWNERS lines are
inert until an administrator grants write access. Granting it is a repository
settings change, not a code change, so it cannot be fixed in a pull request. When
a subsystem owner is added to or removed from this page, re-check their permission
at the same time.

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
