# Dependabot PR Review & Merge Skill

Use this skill when the user asks to **review, triage, retarget, or merge
Dependabot PRs** (e.g. "review the new dependabot PRs", "merge the dependabot
bumps if safe").

Three invariants — never skip any of them:

1. **The base must be `develop`.** Dependabot opens PRs against the repository's
   default branch, which is `develop`, so a new PR already targets it. A PR with
   any other base (an older one opened against `main`, or one moved to another
   branch) is retargeted before anything else, because all work in this repo
   merges to `develop` first.
2. **Always assess risk per PR** before merging (see checklist below). Never
   bulk-merge on green CI alone.
3. **Always run tests after merging** to validate `develop` (see Post-merge
   validation). Merged ≠ done.

## Step 1 — Inventory

```bash
gh pr list --author "app/dependabot" --state open \
  --json number,title,baseRefName,headRefName,createdAt,url --limit 50
```

## Step 2 — Redundancy check against develop

A bump may already exist on `develop` (e.g. done manually in a feature branch,
or the same bump from an older PR against another base). Check before reviewing:

```bash
git fetch origin main develop --quiet
# npm deps — check the lockfile version on develop:
git show origin/develop:src/ui/package-lock.json | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(d['packages']['node_modules/<dep>']['version'])"
# pip deps:
git show origin/develop:<path>/requirements.txt
```

If `develop` already has the new version (or newer): **close the PR** with a
comment explaining it will reach `main` at the next release merge. Do not
retarget or merge it.

## Step 3 — Risk assessment (per PR)

Read `gh pr diff <NN>` and `gh pr view <NN> --json body` (release notes).
Assess:

- **Semver distance**: patch < minor < major. A major bump is never
  "merge-if-safe" territory — report it to the user with the breaking changes.
- **Security content**: CVE/GHSA fixes raise urgency and usually justify the
  merge; note the CVE id in your summary.
- **Blast radius**: is it a `dependencies` or `devDependencies` bump
  (`chore(deps)` vs `chore(deps-dev)` in the title)? Is the package used
  directly by our code (grep `src/` for imports) or only transitively?
- **New transitive deps**: scan the lockfile diff for newly added packages
  (e.g. axios 1.18 pulled in `https-proxy-agent`/`agent-base`) — name them in
  the summary.
- **Behavioral changes**: skim release notes for new defaults, removals, or
  option renames that could affect our usage (grep our code for the affected
  API if unsure).

Safe-to-merge bar: patch/minor bump, no breaking changes affecting our usage,
CI green. Anything else → report to the user instead of merging.

## Step 4 — Retarget to develop (only if the base is not `develop`)

```bash
gh pr edit <NN> --base develop
```

Then re-check mergeability (GitHub recomputes it asynchronously — wait a few
seconds):

```bash
gh pr view <NN> --json mergeable,mergeStateStatus,baseRefName
```

- `CONFLICTING` / `DIRTY`: the branch was cut from another base and conflicts with
  `develop`. Comment `@dependabot rebase` on the PR and poll until Dependabot
  force-pushes a rebased head (~1–2 min), which also restarts CI.
- **Gotcha (this has bitten us):** Dependabot's rebase/recreate can RESET the
  base branch back to the one it was opened against, silently undoing your retarget. After any
  Dependabot rebase, re-run `gh pr view <NN> --json baseRefName` and re-edit
  the base if needed — and re-verify `baseRefName == develop` immediately
  before every merge.
- Retargeting or rebasing re-triggers CI; the "Lint, Type Check, and Test"
  job takes ~9 minutes. Poll `gh pr checks <NN>` until it completes.

## Step 5 — Merge

Only after: risk assessed as safe, base is `develop`, mergeable is
`MERGEABLE`, and required CI checks pass.

```bash
gh pr merge <NN> --squash
```

(`--squash` matches repo convention. Note `mergeStateStatus: UNSTABLE` just
means a non-required check hasn't finished; required checks passing is the
gate.)

## Step 6 — Post-merge validation (mandatory)

Pull the updated `develop` and run the test suites relevant to what changed:

```bash
git checkout develop && git pull

# Python dep changed (any requirements.txt) → run the python test suites:
make test          # idp_common_pkg + idp_cli + srt scan

# UI dep changed (src/ui/package*.json) → reinstall and verify build + lint:
cd src/ui && npm ci && cd ../..
make ui-build
make ui-lint
```

If several PRs merged together, one validation run at the end covers them
all. If validation fails, identify the offending bump, revert that merge
commit on `develop` (`git revert -m 1 <sha>`), push, and report the failure
with the test output. For anything broader, `.claude/skills/full-test-battery.md`
has the full battery and known pre-existing-failure baseline.

## Step 7 — Report

Summarize for the user, per PR: dependency, old→new version, risk verdict
(with CVEs / new transitive deps noted), action taken (merged / closed as
redundant / left open + why), and the post-merge validation result.
