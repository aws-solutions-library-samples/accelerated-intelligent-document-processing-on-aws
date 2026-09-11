# Prepare CHANGELOG — GenAI IDP Accelerator

Use this skill to review and clean up the **`## [Unreleased]`** section of
`CHANGELOG.md` before a release, so it reads as a crisp, user-facing summary of
what actually changed **since the last published release** — not a running log
of the dev cycle.

## Goal

Turn the raw, accreted `[Unreleased]` section into a release-ready changelog with:

1. **Exactly three subsections, in this order: `### Added`, `### Changed`, `### Fixed`.**
   (Fold any `### Removed` content into `### Changed` — a removal is a
   user-facing change. Do not create other subsections.)
2. Entries that describe only the **net change since the last release** — the
   thing a user upgrading from the last published version will experience.
3. **Short entries: a bold lead phrase plus 2–3 sentences.** Say what a customer
   gets, what they'd have hit before, and what (if anything) they must do — then
   stop. Link the doc and/or the PR for anyone who wants the detail.
4. **Only entries a customer can perceive.** Internal housekeeping — CI gates,
   test scaffolding, dev-harness fixes, scanner-suppression bookkeeping — comes
   out entirely.

## The core rule: net-since-release, not intra-cycle churn

The `[Unreleased]` section accumulates entries across the whole dev cycle
(many `dev`/`rc` builds). Much of that churn **cancels out** and must NOT appear
in the released changelog:

- **Drop fixes for bugs that were introduced by other Unreleased work.** If a
  feature added this cycle later needed a fix this cycle, the user upgrading from
  the last release never saw the bug — so the fix is invisible to them. Fold the
  fix silently into the feature's entry (or drop it) rather than listing it.
  - *Example:* an entry like "X no longer gets stuck in PENDING after the
    AppSync→REST migration" is pointless when the AppSync→REST migration is
    itself still Unreleased — the user never had the old AppSync path. Remove it.
- **Collapse a feature that was added then reworked** into a single entry
  describing the final shipped behavior. The intermediate states never shipped.
- **Merge duplicate/overlapping entries** about the same feature into one.
- **Keep** anything that is a genuine delta vs. the last released version —
  including fixes to bugs that existed **in that last release** (those are real,
  user-visible fixes).

When unsure whether a bug pre-dates the last release, check with git:
`git log <last-tag>..HEAD -- <path>` and `git log -S"<symbol>" <last-tag>..HEAD`.
If the buggy code was introduced after the last tag, the fix is intra-cycle → drop/fold.

## The second rule: customer impact, not implementation

A CHANGELOG entry is release communication, not a design record. The commit,
the PR and the docs already hold the reasoning; the entry exists so someone
deciding whether to upgrade — and someone debugging after they did — can see
what changed for them in a few seconds.

**The notice test.** For every entry, ask: *would a customer running this
product notice this, or have to act on it?* If no, drop it.

- **Drop** — CI jobs and gates, new test suites or test tooling, lint/format
  config, scanner-suppression bookkeeping, dev-harness and benchmark-harness
  changes, repo hygiene, "why CI missed it" post-mortems, test counts.
- **Keep** — anything reaching a deployed stack, the Web UI, the CLI, config
  semantics, cost, accuracy, IAM, or the upgrade path. A **security fix on a
  shipped code path counts as customer-visible** even when nothing looks
  different (dependency CVEs, an auth or presigning fix, a removed
  code-execution sink) — say the risk and the version, not the wheel-tag
  mechanics.
- A dropped item is not lost: git history and the PR remain the record. Do not
  invent a fourth subsection or an "internal changes" bucket to shelter them.

**Cut from the entries you keep**, unless the entry makes no sense without it:
internal symbol and file names, function/resolver/Lambda plumbing, root-cause
narration, rejected alternatives ("chosen over…"), self-justifying asides
("deliberately narrow", "which is why…"), measurement minutiae, and counts of
tests, sites or findings. A number stays only when it *is* the impact — a recall
cliff, a cost delta, a 5-hour retry ladder.

**Never cut**, however long the entry gets: `⚠️` breaking-change, migration and
action-required notes, recovery steps for a wedged stack, and statements that a
capability is unavailable in a partition or mode.

## Procedure

1. **Find the release baseline.** The last published version:
   ```bash
   git describe --tags --abbrev=0        # e.g. v0.5.16
   ```
   Cross-check against the second `## [x.y.z]` heading in `CHANGELOG.md` (the one
   below `[Unreleased]`). That heading's version is the baseline; everything in
   `[Unreleased]` should be a change relative to it.

2. **Inventory what really shipped since the baseline** (to catch omissions and
   to date-check "intra-cycle" claims):
   ```bash
   git log --oneline <last-tag>..HEAD
   ```
   Read the current `[Unreleased]` body in full.

3. **Classify every existing entry** as: rewrite-shorter / merge-with-another /
   **drop (intra-cycle churn)** / **drop (no customer impact)**. Apply both rules
   above — assume *rewrite*, not keep-as-is; an entry already at 2–3
   impact-focused sentences is the exception. Move each survivor into the right
   one of the three subsections.

4. **Rewrite every survivor.** Each entry:
   - Starts with a **bold lead phrase** stating the outcome from the customer's
     side — the capability gained, the new behavior, or the symptom now gone —
     matching the house style (`- **Lead phrase.** ...`).
   - Then **2–3 sentences**: what a customer gets or no longer hits, and what
     they must do. Cause only where it changes what someone does about it.
   - Links the relevant **doc** (`[guide](docs/...md)` / module README) **and/or
     the PR** (`(#NNN)`) — that is where implementation detail belongs. Prefer a
     doc link for features; a PR link is fine for pure fixes.
   - Keeps any **migration / action-required / breaking** note. Preserve `⚠️`
     markers and "Request … access" notices. These may push an entry past three
     sentences; that is the only thing that may.
   - **Length check:** if an entry runs past ~4 sentences, something in it is
     implementation detail, root-cause narration, or a rejected alternative.
     Delete that, don't compress it.

5. **Order within each subsection** most-significant first (breaking changes and
   headline features at the top; minor items last).

6. **Keep the intro paragraph** under `## [Unreleased]` if present, but make sure
   it still matches the pruned content.

7. **Do not** add the release date or version number, and do not renumber the
   section — releasing (turning `[Unreleased]` into `## [x.y.z] - DATE` and adding
   the template URLs block) is a separate step the maintainer does at tag time.

## Style reference

```markdown
### Added

- **Short bold lead phrase.** One or two sentences on what a customer can now do
  and what it saves them. See the [Feature guide](docs/feature.md).

### Changed

- **What changed, stated as the new behavior.** One sentence on the effect.
  ⚠️ Migration note if a user must act. (#123)

### Fixed

- **The user-visible symptom that is now fixed.** One sentence on when it hit and
  what it cost; a clause of cause only if it changes what someone does. (#124)
```

### Rewriting: an example

Before — 120 words, most of it internals:

> - **In integrated (1S-TopK) confidence mode, every group field's value was the
>   raw candidate object instead of the extracted value.** `resolve_candidates`
>   handled scalar candidates and array items but had no case for a **group
>   (object)** field whose sub-attributes are candidates, so the group fell
>   through to the pass-through branch: `Address.City` was stored as
>   `{"G1": "Anytown", "P1": 0.95, …}` rather than `"Anytown"`, and because no
>   confidence leaf was emitted the group's fields were also invisible to
>   threshold alerts and HITL. Groups now resolve, recursively (group-in-group,
>   list-in-group, group-in-list-row), with `$ref`s dereferenced against `$defs`
>   at every level so numeric coercion reaches nested sub-attributes too. Nine
>   regression tests.

After — the impact, the blast radius, the fix:

> - **Group fields are extracted correctly in integrated confidence mode, and
>   their confidences now reach threshold alerts and HITL.** Every group field of
>   every document processed in this mode held an unusable candidate object
>   (`Address.City` = `{"G1": "Anytown", …}`) and was skipped by review routing.
>   Nested groups and lists resolve too. (#658)

## Output

Present the proposed rewritten `[Unreleased]` section for review, and — unless
told otherwise — apply it to `CHANGELOG.md` with the Edit tool. Call out, in a
short list, every entry you **dropped as intra-cycle churn**, every entry you
**dropped as having no customer impact**, and every set you **merged**, so the
maintainer can veto any judgment call — dropping is the highest-risk call in this
skill, so name every drop, one line each, and never drop silently. Do not touch
any released section below `[Unreleased]`.
