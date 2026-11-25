---
name: update-solution-profile
description: "Use at release time, after the CHANGELOG is cut, to review the solution profile, add its changes.yaml entry and set reviewed_for_version."
---

# Update the solution profile for a release — GenAI IDP Accelerator

Use this skill **at release time**, after the CHANGELOG section for the release is
cut ([cut-release-changelog](../cut-release-changelog/SKILL.md)) and before publishing. It
reviews the curated files in `solution-profile/` against what the release changed,
records the release in `changes.yaml`, and marks the profile as reviewed for it.

Trigger phrases: "update the solution profile for the release", "review the profile
for 0.6.16", or the failure message `make solution-profile-check` prints on a
release-prep commit, which names this file.

The profile is described in [docs/solution-profile.md](../../../docs/solution-profile.md).
Its contract is vendored in `solution-profile/schema/v1/`; read its `README.md` for
the meaning of every field. Do not edit the vendored files.

## Why this step exists

`make solution-profile-check` applies the contract's **release rules** whenever
`VERSION` is a final `X.Y.Z`: `reviewed_for_version` must equal `version`, and
`changes.yaml` must have an entry for it. So once `make version V=<x.y.z>` has run,
the check (and with it `make lint`, `make lint-cicd` and both CIs) stays red until
this skill has been followed. That is deliberate: the release cadence is weekly, and
nothing else would stop the profile quietly describing an older release.

The cross-checks (hook points, presets, file formats, selectable models, launch
Regions, recent releases) have already been enforcing coverage on every pull request,
so what is left here is judgement about content that no gate can derive.

## Step 1 — Confirm the version

```bash
cat VERSION                                              # final X.Y.Z, not .devN or rcN
grep -n '^version:' solution-profile/solution-profile.yaml   # make version wrote the same value
make solution-profile-check                              # expect the release-rule failures only
```

If the manifest's `version` does not equal `VERSION`, `make version` was not used;
run it rather than editing either file.

## Step 2 — Review the curated files against the release

Read the release's CHANGELOG section in full. For each entry, decide whether it
changes something the profile states, and edit the item that states it:

| CHANGELOG entry is about | Review |
|---|---|
| a new feature or a feature leaving preview | `capabilities.yaml` (`status`, `since`, `description`, `configuration`) |
| a limit, a measured size, or a refusal | `limitations.yaml` (numbers and their conditions, `impact`, `mitigations`) |
| a hook, a configuration route, the extension framework | `extension-points.yaml` (`constraints`, `modes`, `effort`, `upgrade_risk`) |
| a new Region, partition or deployment option | `deployment.yaml` |
| a new question a delivery must now ask | `discovery-questions.yaml` |
| the Capacity Planning page or the cost calculator | `sizing.yaml` |

Rules for the content, which review is the only thing that enforces:

- **Every number keeps its conditions.** Copy a measured figure with what it was
  measured on ("on the v0.6.14 grid, at the shipped default model"). Never round a
  measurement into a guarantee, and say "not measured" where the docs do.
- **Evidence must still say what the item says.** An item whose evidence page was
  rewritten may now be wrong while every path still resolves.
- **Customer-safe and generic.** Nothing in the profile may name a customer, a
  consumer, an internal programme, team, person, tool or URL.
- **Ids are stable.** Never rename or reuse an id. A capability that goes away
  stays in its file with `status: retired` for at least one release.
- **No counts of repository files or tests** in descriptions.

## Step 3 — Add the release to `changes.yaml`

Add one entry at the **top** of `releases` (newest first):

```yaml
  - version: <x.y.z>
    date: <YYYY-MM-DD>          # the day the release is tagged
    summary: <one or two sentences: the headline changes>
    action_required:            # every non-"none" Action item in the CHANGELOG section
      - text: <what a user must do when upgrading>
        affects: [capability:<id>]
    affects: [capability:<id>, deployment:<id>]
    evidence: [CHANGELOG.md]
```

`action_required` mirrors the CHANGELOG's `**Action:**` and `⚠️ **Action:**` items.
Leave out an item whose action is simply "none"; keep one that says "none, except …"
and write the exception as the text. Keep at least the latest ten releases; the
cross-check fails if one of the latest ten in `CHANGELOG.md` has no entry, or has an
action item in the CHANGELOG and an empty `action_required`.

## Step 4 — Mark the profile reviewed

Set `reviewed_for_version: <x.y.z>` in `solution-profile/solution-profile.yaml`.
Only do this after Steps 2 and 3: the field is a person's statement that the curated
files were checked against this release, and consumers rely on it.

## Step 5 — Verify

```bash
make solution-profile-check            # release rules ON, validator "valid", cross-checks passed
make solution-profile-build            # writes dist/solution-profile-genai-idp-accelerator-<x.y.z>.tar.gz
make solution-profile-build            # again: the .sha256 must not change
```

After the GitHub Release is created (`docs/release-runbook.md` Step 6), the
**Solution profile release asset** workflow attaches the tarball and its `.sha256` to
it. If that run failed or did not start, run it for the tag:

```bash
gh workflow run solution-profile-release.yml -f tag=v<x.y.z>
```

## After the release

The next `make version V=<x.y.z+1>.dev1` writes the development version into the
manifest and turns the release rules off again. `reviewed_for_version` stays at the
release until the next time this skill runs.
