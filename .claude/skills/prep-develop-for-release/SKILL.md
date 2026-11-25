---
name: prep-develop-for-release
description: "Use for 'prep develop for release <x.y.z>' or 'prepare the release': stamp the version, prune and cut the CHANGELOG, review the solution profile and run the offline gates, so develop is ready to merge to main and publish."
---

# Prep develop for a release — GenAI IDP Accelerator

Use this skill to take `develop` from a development build to a commit that is ready to
merge to `main`, tag and publish. It runs, in order, the steps that
[docs/release-runbook.md](../../../docs/release-runbook.md) lists before publishing
(its sections 1.1, 1.2 and Steps 1–2), using the existing skills for each. It stops
before publishing: nothing here uploads, tags, creates a GitHub Release or merges to
`main`.

Trigger phrases: "prep develop for release", "prepare the 0.6.17 release", "get develop
ready to release".

Each sub-step has its own skill, which stays the authority for that step. Read each one
when you reach it and follow it in full; this file only says the order, how the version
is chosen, and what else has to be true before the merge.

## Step 0 — Start from a synced, clean `develop`

```bash
git switch develop
git fetch origin && git fetch github
git rev-parse origin/develop github/develop HEAD   # all three must match
git status --short                                 # nothing unexpected
```

`origin` is GitLab and `github` is GitHub. If the two remote `develop`s differ, sync them
first, as `.claude/rules/security-and-published-text.md` describes (merge locally and
push `develop` to both with `ALLOW_SHARED_BRANCH=1`). A fetch from GitLab that fails
leaves a stale `origin/develop` that only looks in sync, so check that the fetch
succeeded before you compare. If the tree holds changes you did not make, stop and ask
whose they are.

## Step 1 — Choose the version

Use the first of these that applies, and say which rule chose it:

1. **The version the user gave.** It must be a final `X.Y.Z`.
2. **The base of `VERSION`'s pre-release**, when `VERSION` is a development or release
   candidate build of a version that has not been released: `0.6.17.dev3` or
   `0.6.17rc1` gives `0.6.17`.
3. **The previous release plus one patch**, read from the first `## [x.y.z]` heading
   below `## [Unreleased]` in `CHANGELOG.md`: `0.6.16` gives `0.6.17`.

Then check it:

```bash
cat VERSION
git describe --tags --abbrev=0         # the previous release; the new version must be greater
grep -n "^## \[" CHANGELOG.md | head -3 # the new version must not have a section yet
```

If a `## [<x.y.z>]` section already exists, or a tag `v<x.y.z>` exists, this release was
already cut: stop and ask. If `VERSION` is already the final `<x.y.z>`, Step 2 is a
no-op, but run it anyway, because it is safe to repeat and it rewrites every file it
stamps.

## Step 2 — Stamp the version

```bash
make version V=<x.y.z>
cat VERSION                            # exactly <x.y.z>
```

Use `make version` and nothing else: it rewrites `VERSION`, the package version files,
the seller entitlement service template, the `idp_common` lockfile entry and the
solution profile manifest, and lists every path it changed. Do not edit any of them by
hand. Once `VERSION` is final, `make solution-profile-check`, and so `make lint`, stays
red until Step 5 is done. That is expected.

## Step 3 — Catch up the French (Canada) translations, and take the figures

Follow the French section of [prepare-changelog](../prepare-changelog/SKILL.md#the-web-uis-french-canada-before-the-cut):

```bash
make ui-i18n-coverage                               # fr-CA missing and stale must be 0
python3 scripts/check_ui_string_debt.py --summary   # hardcoded strings left
```

If `check_ui_string_debt.py` reports a skip because `src/ui/node_modules` is out of
date, run `npm ci` in `src/ui` and repeat it. A skip is not a figure. If `fr-CA` has
missing or stale entries, the catch-up needs Bedrock credentials and a person to read
the review. Report that to the user and wait; do not cut the CHANGELOG without it.

## Step 4 — Prune and cut the CHANGELOG

1. Follow [prepare-changelog](../prepare-changelog/SKILL.md) over `[Unreleased]`,
   putting the Step 3 figures in the entry about the web UI's languages. Show the user
   every entry you dropped and every set you merged, as that skill requires.
2. Follow [cut-release-changelog](../cut-release-changelog/SKILL.md): the heading becomes
   `## [<x.y.z>]` and the `## Templates` block is added, one URL per Region, with the
   Regions and bucket basenames taken from `scripts/aws-release.sh`.

If the user asked for the whole preparation in one go, apply the prune and continue. The
list of drops and merges is what they review afterwards.

## Step 5 — Review the solution profile

Follow [update-solution-profile](../update-solution-profile/SKILL.md) against the
section you just cut. Add a `changes.yaml` entry whose `action_required` mirrors every
`⚠️` and `**Action:**` item in the section, update the curated files the section
changes, and only then set `reviewed_for_version`. The check also reports evidence
anchors that a documentation change broke during the cycle. Fix those as well, because
the check fails on them.

```bash
make solution-profile-check    # release rules ON, "valid", cross-checks passed
make solution-profile-build    # run twice: the .sha256 must not change
```

## Step 6 — Check what a reader of the release needs

- **A migration page for every action item.** If the section has `⚠️ **Action:**` items,
  `docs/migration-v<prev>-to-v<new>.md` (dots removed, such as
  `migration-v0615-to-v0616.md`) should exist and be linked from the section. If the
  items need one and it is missing, tell the user. Do not write it inside this skill.
- **Links resolve.** Run `make check-markdown-links`. The new section links many
  anchors, and an anchor renamed during the cycle breaks one quietly.
- **No counts in prose outside the CHANGELOG.** Run
  `python3 -m pytest -q scripts/tests/test_documented_counts.py`.

## Step 7 — Run the offline gates on the release commit

Run these **one after another**, not in parallel. They share one checkout and one
`node_modules`, and two at once can fail each other.

```bash
make lint            # includes solution-profile-check, the UI and i18n gates
make typecheck
make test
make dep-audit
make srt-setup && make srt-scan   # before any publish: publish output is scanned too
```

Read the SRT findings table, not only its exit code. Report every failure with its
output. A failure that is already present on `develop` and not caused by this change is
still reported, labelled as pre-existing (`.claude/skills/full-test-battery/SKILL.md`
holds the known baseline). The gates are advisory: report the result and do not refuse
to continue on one.

## Step 8 — Commit the release preparation

The release commit holds `VERSION`, the stamped version files, `CHANGELOG.md`,
`solution-profile/` and any link or anchor fixes, and nothing else. Check with
`git status --short`.

```bash
git add -A -- VERSION CHANGELOG.md solution-profile lib feature-platform/seller-entitlement-service
git status --short          # review: only release-preparation paths
git commit -m "Prepare release <x.y.z>"
```

Ask before you commit or push unless the user's request already said to.

- **Directly on `develop`**, if the user said so: prefix the commit and the push with
  `ALLOW_SHARED_BRANCH=1`, then push `develop` to both remotes
  (`git push origin develop` and `git push github develop`), so that GitLab and GitHub
  stay identical. Before the GitHub push, scan the commit for internal references.
- **Otherwise**, commit on `release/<x.y.z>` and open a pull request to `develop`.

The commit message is published text: write it at summary level.

## Step 9 — Report, and hand over what is next

Report the version and the rule that chose it, the CHANGELOG drops and merges, the
French figures, the solution profile changes, each gate's result, and the commit SHA and
where it was pushed. Then list what this skill does not do, which the maintainer runs
from [docs/release-runbook.md](../../../docs/release-runbook.md), and which the
[release](../release/SKILL.md) skill runs with `make release`:

1. Merge `develop` into `main`.
2. Publish the artifacts with `scripts/aws-release.sh` (runbook Step 3). The CHANGELOG
   template URLs return 404 until it lands.
3. Verify the artifacts are public (Step 4 and section 5).
4. Tag `v<x.y.z>` and push the tag (Step 5).
5. Create the GitHub Release (Step 6). The solution profile workflow attaches its
   tarball.
6. `make docs-deploy` (Step 7).
7. The live validation battery, `.claude/skills/release-validation/SKILL.md` (Step 8).

After the release, `make version V=<x.y.z+1>.dev1` turns the solution profile's release
rules off again for the next cycle.
