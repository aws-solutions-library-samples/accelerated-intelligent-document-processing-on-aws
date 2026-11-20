---
title: "Solution Profile"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Solution Profile

From the first release after 0.6.15, each release of the GenAI IDP Accelerator
publishes a **solution profile**: a small, versioned, machine-readable description of
the accelerator. It says what the
accelerator does, where it can be extended, where it stops, which questions test
whether it fits a need, what sizes a deployment, and how a delivery usually starts.

The profile is built from the source at each release tag and attached to that
release on GitHub, so a reader can pin exactly one version and move to the next
through review. It lives in the repository at
[`solution-profile/`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/tree/main/solution-profile).

## Who it is for

The profile is for anyone deciding, before deploying anything, whether the
accelerator fits a document-processing need and what would have to be built around
it. That is often an AI assistant working with a person early in a project. The
documentation answers the same questions across many pages that change every week;
the profile answers them in a few files, each statement pointing at the page or source
file that supports it, for one release at a time.

Everything in the profile may be shown to a customer. It names no customer and no
consumer, and its discovery questions are written to be asked as they stand.

The profile describes; it does not support. The accelerator is a community sample:
the customer deploys, owns and operates it.

## Download and verify it

From the first release after 0.6.15, each GitHub Release has two assets:

- `solution-profile-genai-idp-accelerator-<version>.tar.gz`
- `solution-profile-genai-idp-accelerator-<version>.tar.gz.sha256`

```bash
VERSION=<x.y.z>
REPO=aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws
gh release download "v$VERSION" --repo "$REPO" --pattern 'solution-profile-*'
sha256sum -c "solution-profile-genai-idp-accelerator-$VERSION.tar.gz.sha256"
tar -xzf "solution-profile-genai-idp-accelerator-$VERSION.tar.gz"
cat solution-profile/build.json
```

The tarball has one top-level directory, `solution-profile/`. `build.json` records the
profile `id`, `version`, release `tag`, release `commit` and `schema_version`, and the
same tag and commit are stamped into `solution-profile/solution-profile.yaml` under
`source`. The archive is deterministic: the same commit always produces the same tar
stream. The gzip layer adds no timestamp or file name, so with the same Python and
zlib build the compressed bytes, and so the published digest, reproduce from a
checkout of the tag; a different zlib can compress the same stream differently.

To check the profile against its contract, validate it with `--repo-root` pointing at
a checkout of the release tag. Its evidence paths are relative to that checkout, and
the tarball does not contain the repository:

```bash
git clone --depth 1 --branch "v$VERSION" "https://github.com/$REPO" idp
python3 idp/solution-profile/schema/v1/validate_profile.py solution-profile \
  --repo-root idp --release
```

The validator needs PyYAML and jsonschema.

## Read it

| File | What it holds |
|---|---|
| `solution-profile.yaml` | The manifest: the release it describes (`version`), the release a person last reviewed it against (`reviewed_for_version`), a summary, the signals that suggest a need it serves or does not, the licence and the support model |
| `capabilities.yaml` | What the accelerator does, with the processing mode (`pipeline` or `bda`) and status (`ga`, `preview`, `experimental`) of each capability |
| `extension-points.yaml` | Each documented way to change its behaviour, with a gap category, effort, upgrade risk, constraints and modes |
| `limitations.yaml` | Where it stops, with numbers and their conditions, how much each limit matters, the questions that detect it and what mitigates it |
| `discovery-questions.yaml` | The questions that decide fit and configuration, and what each answer implies |
| `sizing.yaml` | The inputs that size a deployment, and the accelerator's own estimators (the Capacity Planning page and the cost calculator) |
| `deployment.yaml` | The deployment options, where each deploys, and the prerequisites |
| `delivery.yaml` | Day one, a walking skeleton and typical work items, with effort |
| `changes.yaml` | What each recent release changed, and what an upgrade requires someone to act on |
| `skills/idp-solution-fit/SKILL.md` | An Agent Skill that tells an assistant how to read the profile, ask the questions and classify each gap |

Every item has `evidence`: a repository path at the release commit, or a URL. Items
are cited as `<kind>:<id>`, for example `limitation:simple-extraction-row-limit`, and
an id keeps its meaning across releases.

### Cross-check tags

Some items carry tags that the repository's own checks derive from the code, so they
also tell a reader exactly what the release covers:

| Tag | On | Meaning |
|---|---|---|
| `hook-point:<point>` | an extension point | The pipeline hook point the item describes, one item per point |
| `preset:<name>` | a capability | A configuration preset in `config_library/unified/<name>` |
| `format:<ext>` | a capability, or a limitation | A file extension the OCR step routes: on a capability when it is processed, on a limitation when it is routed and then refused |
| `model:<id>` | a capability | A model the deployment templates offer, without its Region prefix or variant suffix |

## The gap taxonomy

For each requirement the accelerator does not meet as it stands, choose the most
preferred category that closes it:

| Category | Meaning | Who owns the change |
|---|---|---|
| `configuration` | Settings, configuration profiles and versions, presets, prompts and JSON Schema classes. No code | The deployment's operator |
| `hook` | Customer-owned code at a documented hook point, which the accelerator calls | The customer |
| `extension` | An installable add-on on the extension framework, deployed beside the accelerator | The customer or a partner |
| `core-enhancement` | A change to the accelerator itself, contributed upstream; it ships when the maintainers release it | The maintainers |
| `fork` | A private copy with code changes; every upgrade then has to be merged by hand | The customer, permanently |
| `other` | Work outside the accelerator: AWS service quotas and model availability, integration, data, process, security review or infrastructure | Varies |

A requirement that nothing closes, not even a fork, is **not a fit**. That verdict is
not a category, so it never appears in `extension-points.yaml`.

## How maintainers keep it current

The profile's contract is vendored, unchanged and hash-pinned, in
[`solution-profile/schema/v1/`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/tree/main/solution-profile/schema).
`make solution-profile-check` runs on every pull request in both CI systems, as part of
`make lint-cicd`, and does two things:

1. It runs the contract's reference validator: the schema, ids and references, that
   every evidence path exists, and that `version` equals the repository's version.
2. It cross-checks the profile against the code, and fails, naming the missing item
   and where it belongs, when the code has something the profile does not cover. It
   checks every pipeline hook point and the modes that reach it, every configuration
   preset, every routed file extension, every selectable model, the Regions the
   release publishes to, and that each of the latest releases in `CHANGELOG.md` has a
   `changes.yaml` entry with its action items. A tag for something the code no longer
   has fails as well.

At release time the contract's **release rules** apply as well:
`reviewed_for_version` must equal `version`, and `changes.yaml` must have an entry for
it. They apply automatically when the repository's version is a final `X.Y.Z`, so the
release-prep commit stays red until a maintainer has reviewed the curated files
against the release's CHANGELOG, added the `changes.yaml` entry and set
`reviewed_for_version`. The procedure is `.claude/skills/update-solution-profile.md`,
and the [Release Runbook](./release-runbook.md) names the step. When the GitHub
Release is published, the **Solution profile release asset** workflow checks out the
tag, applies the release rules again, builds the tarball, validates the unpacked copy
and attaches both assets to the release.

### The version form

The repository's version is a PEP 440 version, which between releases carries a
development or release-candidate suffix such as `0.6.16.dev1`. The contract accepts
only `X.Y.Z` with an optional `-suffix` or `+suffix`, so the profile carries the
hyphenated form, `0.6.16-dev1`, and the check compares the manifest against that form
of the repository's version. A final release needs no conversion, so at a release the
manifest's `version` is exactly the repository's version. `make version` writes the
converted form into the manifest.

### Recommended contract changes

Two properties of the contract made it harder to apply here, and the profile works
around both rather than changing its vendored copy:

- **Accept PEP 440 development and release-candidate suffixes** in `version` (for
  example `0.6.16.dev1` and `0.6.16rc1`), so a repository whose version file uses
  them can pass that file to the validator directly.
- **Let a consumer validate an unpacked profile without the repository.** The
  validator resolves evidence paths against the directory above the profile, which in
  an unpacked release asset holds no repository files, so validating the asset needs
  `--repo-root` and a checkout of the tag. An option to skip the evidence-path check
  would let a consumer validate the asset alone.
