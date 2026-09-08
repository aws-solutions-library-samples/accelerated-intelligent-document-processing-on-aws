# Cut the release CHANGELOG section — GenAI IDP Accelerator

Use this skill at **tag time**, to turn the `## [Unreleased]` heading into a
numbered release section and append that release's published template URLs.
This is the step [prepare-changelog](prepare-changelog.md) deliberately stops
short of.

Trigger phrases: "label the Unreleased section to the current VERSION", "cut the
release changelog", "add the Templates block for this release".

## Order of operations

1. **[prepare-changelog](prepare-changelog.md) first** — prune `[Unreleased]` to
   net-since-release content in three subsections. Do not cut the release
   section over an unpruned body.
2. Then this skill: rename the heading, append the Templates block.

If the user asks for both in one go, do the prune, show it, then cut.

## Step 1 — Read the version from `VERSION`

`VERSION` is the single source of truth. Do **not** infer the version from
`git describe`, from the branch name, or from the previous section + 1.

```bash
cat VERSION                    # e.g. 0.6.4
git describe --tags --abbrev=0  # sanity check: must be the PREVIOUS release
```

Two checks before proceeding:

- **The version must be final, not a dev/rc build.** A `VERSION` of
  `0.6.4.dev4`, `0.6.4rc1`, `0.6.4a1` means the release has not been stamped
  yet. Stop and tell the user to run `make version V=0.6.4` first (that target
  rewrites `VERSION` plus the eight package version files — see the
  `##@ Version Management` block in the `Makefile`). Never hand-edit `VERSION`
  as part of this skill, and never strip the suffix yourself to guess the
  release number.
  - Note that `git status` may show `VERSION` already modified from
    `x.y.z.devN` → `x.y.z`. That is the expected state at tag time; use the
    working-tree value.
- **The version must not already have a section.** `grep -n "^## \[" CHANGELOG.md`
  — if `## [x.y.z]` exists, this release was already cut. Stop and ask.

## Step 2 — Rename the heading

```markdown
## [Unreleased]     →     ## [0.6.4]
```

**No date.** Every section in this file is a bare `## [x.y.z]` — do not add
` - 2026-08-14` or any Keep-a-Changelog date suffix, even though the format
otherwise resembles it.

Do **not** add a fresh empty `## [Unreleased]` above it. The next cycle's first
changelog-worthy commit re-creates that heading; an empty one just accumulates.

## Step 3 — Append the Templates block

Immediately after the last entry of the new section (i.e. the last `### Fixed`
bullet) and before the next `## [x.y.z]` heading, add:

```markdown
## Templates
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main_0.6.4.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main_0.6.4.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main_0.6.4.yaml`
```

Conventions to match exactly — copy the previous release's block and change the
three version numbers rather than retyping it:

- `## Templates` is an `h2`, deliberately **not** nested under the release
  heading; that is the existing file's shape.
- Each URL line is indented **three spaces** before the `-`.
- Three regions only, in this order: `us-west-2`, `us-east-1`, `eu-central-1`.
  The bucket name embeds the region (`aws-ml-blog-<region>`) *and* the host does
  too — a copy-paste that updates one but not the other is the classic error
  here.
- **No trailing whitespace** on any line — the `eu-central-1` line ends at the
  closing backtick — then exactly one blank line before the next `## [x.y.z]`.
  (No block in the file carries the trailing spaces an earlier version of this
  skill claimed; don't add them.)
- The filename is `idp-main_<version>.yaml` — the version-pinned artifact, not
  the `idp-main.yaml` floating pointer used by the README launch buttons.

## Step 4 — Verify

```bash
# Exactly one section for the new version, no Unreleased left behind
grep -n "^## \[" CHANGELOG.md | head -5
# All three URLs carry the new version, and no stale one slipped in
grep -n "idp-main_$(cat VERSION).yaml" CHANGELOG.md
# No trailing whitespace crept into the new block
grep -nE " +$" CHANGELOG.md
# Only the top of the file moved
git diff --stat CHANGELOG.md
git diff CHANGELOG.md | grep -E "^@@" | tail -3
```

The diff must not touch any previously released section. If it does, you
rewrote history — revert and redo.

## What this skill does NOT do

Deliberately out of scope; mention them to the user, don't do them unasked:

- **Bumping `VERSION` / package versions** — that is `make version V=x.y.z`.
- **Publishing the artifacts** — `python3 publish.py <bucket-basename> <prefix> <region>`.
  The Templates URLs are a *record* of where the release will be / was
  published; writing them does not publish anything, and they 404 until the
  publish lands.
- **Touching README.md / docs/deployment.md.** Their launch-stack buttons point
  at the unversioned `idp-main.yaml`, which always resolves to the newest
  release — there is no per-release edit to make there. `CHANGELOG.md` is the
  only file in the repo with version-pinned template URLs.
- **Tagging or pushing.** Do not `git tag`, commit, or push unless the user asks.
