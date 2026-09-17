---
title: "Release Runbook"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Release Runbook

This is the operational procedure for publishing a release of the GenAI IDP Accelerator to
its three public S3 regions. It covers what must be true before you start, the ordered
steps, what success looks like at each step, the failure and recovery paths that actually
exist, what is not automated, and how to verify the release afterwards.

It exists because the step that makes artifacts public — `scripts/aws-release.sh` — is six
lines long, is invoked by a human, and is referenced by no CI job in either pipeline.
Everything around it (validation tiers, changelog, benchmarks) was already documented; the
one irreversible step was not.

## How this document was verified — read this first

Under pressure, a runbook is trusted literally. So every claim below is marked with how it
was established, and the gaps are marked as gaps rather than filled in with plausible
guesses.

| Marker | Meaning |
|---|---|
| ✅ | **Verified by reading code** in this repository, at the cited file and line. |
| 📄 | **Verified from committed documentation** in this repository (a skill, a doc, a CHANGELOG entry) — i.e. someone recorded it after doing it, but this runbook did not re-derive it from code. |
| ⚠️ | **Unverified.** Either it depends on account/organisation state that is not in the repository, or it is a step this document's author could not observe. Treat as a question to answer, not an instruction to follow. |

Nothing in this document was executed. No release was published, no AWS write API was
called, and `publish.py` / `scripts/aws-release.sh` were not run while writing it. The
verification therefore covers *what the code does*, not *what happens when it runs*.

## What a release physically is

✅ `scripts/aws-release.sh` runs `idp-cli publish` three times, once per region, each with
`--public`:

```sh
set -e
set -x

idp-cli publish --source-dir . --bucket-basename aws-ml-blog --prefix artifacts/genai-idp --region us-west-2 --public
idp-cli publish --source-dir . --bucket-basename aws-ml-blog --prefix artifacts/genai-idp --region us-east-1 --public
idp-cli publish --source-dir . --bucket-basename aws-ml-blog --prefix artifacts/genai-idp --region eu-central-1 --public
```

✅ The bucket for each region is `<bucket-basename>-<region>` — the region suffix is
appended by the publisher, not written in the script
(`lib/idp_sdk/idp_sdk/_core/publish.py`, `setup_environment`). So the three buckets are
`aws-ml-blog-us-west-2`, `aws-ml-blog-us-east-1` and `aws-ml-blog-eu-central-1`, all under
prefix `artifacts/genai-idp`.

⚠️ **Why those three regions** is not recorded anywhere in the repository. The same three
are used for the extensions marketplace (`config_library/extensions-marketplace.yaml`) and
for every historical `## Templates` block in `CHANGELOG.md`, so the set is at least
consistent — but the rationale (customer demand, EU data-residency coverage, model
availability) is not written down. Do not add or drop a region on the strength of this
document.

### What lands in each bucket

✅ Derived from `publish.py`. `VERSION` below is the contents of the repo's `VERSION` file,
read from `./VERSION` relative to the process working directory.

| Key | Mutability | What reads it |
|---|---|---|
| `artifacts/genai-idp/idp-main.yaml` | **Overwritten every release** | the "Launch Stack" buttons in `README.md` and `docs/deployment.md` |
| `artifacts/genai-idp/idp-main_<VERSION>.yaml` | write-once per version | the `## Templates` block in `CHANGELOG.md`; every release-validation and benchmark record |
| `artifacts/genai-idp/idp-main-latest.json` | **Overwritten every release** | the Web UI "update available" indicator, via the version-check resolver (`src/lambda/version_check_resolver/index.py`) |
| `artifacts/genai-idp/<VERSION>/layers/idp-common-<name>-<hash>.zip` | content-addressed | the deployed stack's Lambda layers |
| `artifacts/genai-idp/<VERSION>/config_library/…` | per-version | the deploy-time config copy custom resource |
| `artifacts/genai-idp/<VERSION>/…` (SAM-packaged nested/pattern templates, function code zips, UI bundle, unified-pattern source zip, samples, `catalog.json`, `samples-manifest.json`) | per-version | CloudFormation and CodeBuild during stack deployment |
| `artifacts/genai-idp/extensions/<id>/…` | **version-free** | the feature platform's catalog; a sibling of the versioned prefix, not underneath it |

✅ `artifacts/genai-idp-mp/` — the *marketplace* extensions prefix — is **not** written by
`scripts/aws-release.sh`. It is published separately by extension authors via
`idp-feature-cli publish`, deliberately kept on a different prefix so the two publish paths
cannot collide (`config_library/extensions-marketplace.yaml`).

### What publish substitutes into the template

✅ `publish.py` rewrites tokens in `template.yaml` as it packages
(`build_main_template`). The ones that matter operationally:

- `<VERSION>` → the `VERSION` file's contents. This is why the published template's
  `Description` line reads `AWS GenAI IDP Accelerator (uksb-r8evguc4p9) (SO9027) (v0.6.8)`
  — checking that string is the cheapest way to confirm *which* build an object is.
- `<ARTIFACT_BUCKET_TOKEN>` / `<ARTIFACT_PREFIX_TOKEN>` → `aws-ml-blog-<region>` and
  `artifacts/genai-idp/<VERSION>`. The deployed stack fetches its own code from the region
  it was published to.
- `<PUBLIC_ARTIFACTS_BUCKET_TOKEN>` / `<PUBLIC_ARTIFACTS_PREFIX_TOKEN>` → the bucket and
  the *version-stripped* prefix, becoming the defaults of the `PublicArtifactsBucket` and
  `PublicArtifactsPrefix` stack parameters. This is what makes the update indicator work
  with no customer action. See [Version Update Indicator](./version-update-indicator.md).

A consequence worth internalising: **each region's artifacts are region-local and not
interchangeable.** A stack created from the `us-east-1` template pulls code from
`aws-ml-blog-us-east-1`. Publishing two of three regions does not give you a
two-thirds-working release; it gives you one region of customers who cannot deploy.

## 1. Preconditions

### 1.1 Branch and tree state

- ✅ `VERSION` must be a **final** version, not `x.y.z.devN` / `rcN` / `aN`. Nothing in the
  publish path enforces this — `check_parameters` and `setup_environment` accept any string
  and `version_compare` deliberately treats a non-numeric segment as `999` so dev builds
  pass the SAM/Python version gates. A `.devN` value would be published as a real release,
  with `.devN` in every key and in the template `Description`. The only guard is the
  changelog skill, which refuses to cut a section over a non-final `VERSION`
  (📄 `.claude/skills/cut-release-changelog.md`). **Check `cat VERSION` by hand.**
- ✅ `VERSION` is set by `make version V=x.y.z`, which validates PEP 440 and rewrites
  `VERSION` plus five package version files and the seller-entitlement-service template's
  `ServiceVersion` output (`Makefile`, `##@ Version Management`). Never hand-edit `VERSION`.
- 📄 `CHANGELOG.md` must have the `[Unreleased]` section already **pruned** to
  net-since-release content in three subsections (`.claude/skills/prepare-changelog.md`),
  and then **cut** to `## [<VERSION>]` with the three-region `## Templates` block appended
  (`.claude/skills/cut-release-changelog.md`). Those URLs are written *before* the publish
  and 404 until it lands — that is expected and is stated in the skill.
- The working tree must be clean and at the commit you intend to tag. ✅ The publish reads
  the working tree, not a tag: `--source-dir .` and `open("./VERSION")` are both relative to
  the process working directory, so whatever is checked out is what ships.

### 1.2 Validation tiers that must have passed

📄 The tier list, the order and the record format are fixed by
[Release Validation](./release-validation/README.md) and its driving skill
`.claude/skills/release-validation.md`. Do not duplicate that list here; run it.

Two ordering constraints from that skill are load-bearing for *this* runbook:

- 📄 **Run the SRT security scan before you build.** `make srt-setup` (with `CI=1`) then
  `make srt-scan`, *before* `publish.py` puts `.aws-sam/` output in the tree. Scanning after
  a build is not wrong — build output is labelled LOCAL-ONLY — but the ordering is the
  documented one and avoids arguing about it later.
- 📄 The release-validation battery's "artifact under test" is the **published**
  `idp-main_<VERSION>.yaml`, so most of its tiers can only run *after* this runbook's
  step 3. The offline tiers (`make test`, `make lint-cicd`, `make typecheck`,
  `make dep-audit`, and a `--clean-build` publish to a throwaway bucket) run before.

### 1.3 Local toolchain

✅ `check_prerequisites` in `publish.py` hard-fails on exactly three commands and one
version floor:

| Requirement | Enforced? |
|---|---|
| `aws` on `PATH` | ✅ checked |
| `sam` on `PATH`, version ≥ `1.129.0` | ✅ checked |
| `uv` on `PATH` | ✅ checked |
| Python ≥ 3.12 | ✅ checked |
| `npm` / Node | ✅ **not checked** — but required. `validate_ui_build` runs `npm ci` then `npm run build` in `src/ui`, and a failure there aborts the publish (`sys.exit(1)`) well after uploads have begun. |
| `ruff` | ✅ **not checked** — but required. `_validate_python_linting` runs `ruff check` and `ruff format --check` and fails the publish if either is non-zero. A missing `ruff` binary raises rather than skipping. |
| `cfn-lint` | ✅ **not checked, and silently skipped if absent.** `_validate_cfn_lint` prints `cfn-lint not installed, skipping CloudFormation linting` and returns success. A release published on a machine without `cfn-lint` gets **no** CloudFormation linting at all. |
| Docker | ✅ **not required.** Pattern container images are built at *stack deployment* time by CodeBuild, not at publish time. The claim in `CLAUDE.md` that the build checks for Docker is stale with respect to this code path. |

Because of the `cfn-lint` behaviour, run `make lint` (or at minimum `make cfn-lint`) on the
release commit before publishing rather than relying on the publish to catch a template
error.

### 1.4 Credentials and permissions

✅ The publish calls exactly these AWS APIs (enumerated from `publish.py` and
`lib/idp_sdk/idp_sdk/_core/s3_security.py`), plus whatever `sam package` and `aws s3 sync`
issue against the same bucket:

- `sts:GetCallerIdentity`
- `s3:ListBucket` (`head_bucket`, and `ListObjectsV2` via paginator)
- `s3:GetObject` / `s3:GetObjectAcl`-adjacent reads (`head_object`)
- `s3:PutObject` (`upload_file`, `put_object`, and `aws s3 sync`)
- `s3:PutObjectAcl` (`put_object_acl` with `ACL=public-read`, only when `--public`)
- `s3:GetBucketPolicy` / `s3:PutBucketPolicy` — best-effort only on a pre-existing bucket.
  `setup_artifacts_bucket` tries to add an `EnforceSSLOnly` statement and, if it cannot,
  prints `Could not verify/apply the EnforceSSLOnly bucket policy on the existing bucket —
  add it manually` and **continues**. It is not fatal.
- `s3:CreateBucket` / `s3:PutBucketVersioning` — only on the 404 path, i.e. only if the
  bucket does not exist. For the `aws-ml-blog-*` buckets this path is not taken.
- `cloudformation:ValidateTemplate`

✅ `--public` works by setting **object ACLs** (`ACL=public-read`) on every object under
`artifacts/genai-idp/<VERSION>/` and `artifacts/genai-idp/extensions/`, plus the two main
template keys. That requires the bucket to have ACLs enabled (Object Ownership not set to
*bucket owner enforced*) and `BlockPublicAcls` off in its Public Access Block configuration.

⚠️ **Which AWS account owns the `aws-ml-blog-*` buckets, which role or profile the releaser
assumes, and how that credential is obtained are not recorded anywhere in this
repository.** Nor is the buckets' Object Ownership / Public Access Block / versioning
configuration. This is the single largest gap in this runbook and the one most responsible
for the bus factor the issue describes. Before the next release, the person who has
published before should record: the account id (or its name), the role, how to assume it,
and whether the buckets have versioning enabled. Until then, treat the credential step as
tribal knowledge that this document cannot supply.

Note also ⚠️/📄: the repo's own AWS guidance is that ambient sandbox environment credentials
override `AWS_PROFILE`, so `unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN`
before checking `aws sts get-caller-identity`
(📄 `.claude/skills/release-validation.md`, preflight step 1). Confirm the identity you get
is the release account before running a command that writes to a public bucket.

## 2. Ordered steps

### Step 1 — Stamp the version

```bash
make version V=<x.y.z>
cat VERSION                      # must print exactly <x.y.z>
git describe --tags --abbrev=0   # must print the PREVIOUS release tag
```

**Success:** `VERSION` and the five package version files all read `<x.y.z>`; the Makefile
prints the list of files it touched. ✅ Verified from the `version` target.

### Step 2 — Cut the CHANGELOG

📄 Follow `.claude/skills/prepare-changelog.md` then
`.claude/skills/cut-release-changelog.md`. The result: `## [Unreleased]` becomes
`## [<x.y.z>]` (no date), followed by a `## Templates` block with the three region URLs.

```bash
grep -n "^## \[" CHANGELOG.md | head -5              # exactly one section for the new version
grep -n "idp-main_$(cat VERSION).yaml" CHANGELOG.md  # three hits, one per region
grep -nE " +$" CHANGELOG.md                          # no trailing whitespace
```

**Success:** three URLs, all carrying the new version, all with the region appearing twice
in each URL (host *and* bucket name). The URLs 404 at this point — expected.

### Step 3 — Publish the artifacts

Preconditions from §1 all satisfied, credentials confirmed, on the release commit with a
clean tree.

```bash
cd <repo root>            # required: publish reads ./VERSION relative to cwd
bash scripts/aws-release.sh 2>&1 | tee scratch/release-<x.y.z>.log
```

Notes on how this behaves, all ✅ verified from code:

- **Run it from the repository root.** `--source-dir .` is relative. 📄 `.claude/skills/live-eval-and-cost.md`
  records that a relative `--source-dir` fails with `VERSION file not found` when the cwd is
  reset — which is what happens if you background the script. If you must detach it, pass an
  absolute path instead of using the script as-is.
- **Each region does a full rebuild.** The per-component checksum is computed over the
  source hashes *plus* `bucket + prefix_and_version + region`
  (`_verify_packaged_templates_exist`, `get_components_needing_rebuild`), so region 2 never
  matches the checksum region 1 wrote. Budget three full builds, not one build and two
  uploads.
- **`set -e` means a failure stops the script.** Earlier regions stay published; later ones
  are untouched.
- **`--public` is applied at the very end of each region's run.** `set_public_acls` is
  called from `print_outputs`, i.e. after every upload. See §3.1 for what that implies.

**Success looks like**, per region: `✅ All builds completed successfully`, then
`✅ Public ACLs set successfully`, then a Deployment Outputs block with the 1-Click Launch
URL and the template URL, then `✅ Done!`. Three of those, then the shell exits 0.

### Step 4 — Verify the artifacts are public and correct

Do this before announcing anything. See the checklist in §5.

### Step 5 — Tag and push

```bash
git tag v<x.y.z>
git push github v<x.y.z>
git push origin v<x.y.z>     # origin is GitLab
```

⚠️ **Unverified:** whether both remotes are tagged as a matter of practice. Every `v0.6.x`
tag is present on the GitHub remote (checked with `git ls-remote --tags`), which is why the
GitHub Release exists; whether the GitLab remote is also tagged, and whether that matters
to anything, this document could not determine.

### Step 6 — Create the GitHub Release

⚠️ **Not automated, and the mechanism is unverified.** No GitHub Actions workflow and no
GitLab job creates a release — verified by reading all four workflows and the full
`.gitlab-ci.yml`. What is observable is the outcome: every tag `v0.6.4` … `v0.6.8` has a
non-draft, non-prerelease GitHub Release authored by the repo owner, titled with the bare
version, whose body is the CHANGELOG section for that version verbatim. For v0.6.8 the
release was created 43 minutes before it was published, i.e. drafted then published.

A command consistent with that outcome — ⚠️ **not** confirmed to be the one actually used:

```bash
gh release create v<x.y.z> \
  -R aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws \
  --title "v<x.y.z>" --notes-file <file containing the CHANGELOG section>
```

### Step 7 — Publish the documentation site

```bash
make docs-deploy
```

✅ This runs `docs-build` (which runs `docs-setup`, then `node sync-sidebar.mjs`, then
`npm run build`) and pushes `docs-site/dist` to the `gh-pages` branch with `npx gh-pages`.
It is entirely manual: `.github/workflows/build-docs.yml` builds the site as a check on
pushes and PRs touching `docs/**`, `docs-site/**` or `images/**`, and explicitly does not
publish.

**Success:** the site at
<https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws/>
serves the new content.

### Step 8 — Run the live release-validation battery

📄 One request, per `.claude/skills/release-validation.md`; the record lands in
`docs/release-validation/v<x.y.z>.md` and a row is appended to
[the index](./release-validation/README.md). Budget most of a working day and real Bedrock
spend. Do not treat the release as validated until that record exists.

## 3. Failure and recovery

### 3.1 The publish fails partway through

✅ **Re-running the same command is the recovery, and it is safe.** The reasoning, from
code:

- **Nothing is deleted.** There is no `delete_object` or `delete_objects` call anywhere in
  `publish.py`. Objects from a failed run are either overwritten by the retry or left
  unreferenced and inert.
- **Checksums are only written on success.** `update_component_checksum` is one of the last
  steps of `run()`, after all uploads and after cfn-lint. A crash before it leaves the
  checksum files untouched, so the next run rebuilds and re-uploads the same components.
  `build_main_template` additionally deletes `.checksum` on any failure, forcing a main
  template rebuild.
- **Two independent safety nets catch a half-deleted build directory.**
  `_verify_layer_zips_exist` forces a layer rebuild if the zips are gone, and
  `_verify_packaged_templates_exist` adds any component whose
  `<component>/.aws-sam/packaged.yaml` is missing back onto the rebuild list even when its
  checksum says it is current.
- **Uploads are cheap on retry.** Layer zips are content-addressed
  (`idp-common-<name>-<sourcehash>.zip`) and skipped with `head_object` if already present;
  the config library goes up with `aws s3 sync`, which skips unchanged files.

**If the retry itself is suspect** — e.g. you edited a bundled data file such as a
`system_defaults/*.yaml` and the layer did not rebuild — 📄 use `--clean-build`. The
checksum cache is known to miss data-only edits, leaving the deployed stack on the old
default (`.claude/skills/live-eval-and-cost.md`, and the same warning appears in the
project's memory of a real occurrence). `scripts/aws-release.sh` does not pass
`--clean-build`; add it by hand for that case, or run the three `idp-cli publish` commands
individually with the flag.

**The window you should care about.** ✅ `idp-main.yaml` (the floating key the README Launch
Stack buttons use) and `idp-main-latest.json` (the key the Web UI update indicator reads)
are both written inside `build_main_template`, which runs **before** `set_public_acls`. So
between those two uploads and the end of a successful run, the floating pointer already
names the new version while some of that version's artifacts are still private. A customer
who clicks Launch Stack in that window can get a template whose nested stacks or code zips
answer 403. The window is minutes, not hours, and it only bites on the first publish of a
version — but it is why §5 says verify before announcing, and why a publish that dies
between those points should be re-run promptly rather than left overnight.

**If a middle region fails**, `set -e` leaves you with, say, `us-west-2` published,
`us-east-1` half-published, `eu-central-1` untouched — and `eu-central-1`'s
`idp-main-latest.json` still naming the previous version, so the update indicator is
inconsistent by region until you finish. Re-run the whole script; the completed region
short-circuits on `head_object` and `s3 sync` and costs only its rebuild time.

### 3.2 A published template turns out to be broken

There is no "unpublish". ✅ Publish never deletes, and the version-pinned
`idp-main_<VERSION>.yaml` is referenced permanently by `CHANGELOG.md`, the release-validation
records and the benchmark records — treat it as an immutable historical artifact and do not
try to remove it.

What you can move are the two mutable keys. **The preferred, fully verified action is to
re-publish the previous tag**, because it rewrites both of them consistently and from a
known-good source:

```bash
git checkout v<PREV>
cat VERSION                       # must read <PREV>
bash scripts/aws-release.sh
```

✅ That rewrites `idp-main.yaml` and `idp-main-latest.json` in all three regions to point at
`<PREV>`, while `<PREV>`'s versioned artifacts under `artifacts/genai-idp/<PREV>/` are
already there (so it is mostly a template re-upload plus per-region rebuild time). Then
publish a patch release with the fix.

⚠️ **Do not assume you can recover the previous `idp-main.yaml` bytes from S3 object
versioning.** `publish.py` enables versioning only on buckets it creates; the
`aws-ml-blog-*` buckets pre-exist, so they take the `head_bucket` success path and their
versioning state is whatever the account configured. This document could not determine it.

If you must move the floating pointer without a rebuild, note the trap: a server-side copy
of the previous versioned template over `idp-main.yaml` fixes the Launch Stack buttons but
**does not** touch `idp-main-latest.json`, which will keep advertising the bad version as an
available update in every deployed Web UI. ✅ That pointer's body is
`{"version": …, "templateUrl": …}` written by `_upload_version_pointer`; rewriting it by
hand means putting a correct JSON object at
`artifacts/genai-idp/idp-main-latest.json` in each region and re-applying `public-read`.
Prefer the re-publish above.

### 3.3 Customers who already upgraded, and stacks wedged in rollback

These are the failure modes with real committed evidence in this repository. They are not
release-time failures — they are what a bad release does to a customer running
`update-stack`, which is why the pre-release guard in each case matters more than the
recovery.

**A bad `pricing.yaml` deadlocks the update *and* the rollback.**
📄 `.claude/skills/live-eval-and-cost.md` and `.claude/skills/test-upgrade.md`: the
`UpdateDefaultConfig` custom resource re-validates `pricing.yaml`, read from
`s3://<config-bucket>/config_library/pricing.yaml`, in **both** directions. A Pydantic
failure (the recorded case: an empty `units:` list) leaves the nested `PATTERNSTACK` in
`UPDATE_ROLLBACK_FAILED`. Recovery, as recorded:

```bash
# validate a candidate locally first
PYTHONPATH=lib/idp_common_pkg python3 -c "import yaml; from idp_common.config.models import PricingConfig; PricingConfig(**yaml.safe_load(open('config_library/pricing.yaml')))"
# upload the corrected file to the config bucket key config_library/pricing.yaml, then:
aws cloudformation continue-update-rollback --stack-name <PARENT> --region <region>
```

No `--resources-to-skip`: child stacks reject a direct skip, and fixing the S3 object lets
the parent's rollback re-validate and complete.

**A rollback-hostile config value wedges the rollback.**
📄 `lib/idp_common_pkg/idp_common/config/README.md`: a rollback reverts the config
custom-resource Lambda to the *previous release's* code but leaves current-shape records in
DynamoDB. Two value classes break older models — `None` on a field an older model coerces
with a bare `int()`, and `0` on a field an older model constrains with `gt=0`. Mitigated in
code (`_omit_rollback_hostile_defaults`, plus rollback detection that returns SUCCESS on a
parse error when the stored `config_format_version` is newer than the running code), but
the mitigation only covers values equal to their declared default.

**The pre-release guard this implies:** before changing a configuration default, confirm the
**previous** release's code accepts the new value, because a rollback runs that code against
the new records. This has bitten the project more than once.

**S3 `OperationAborted` during a rollback.** 📄 `CHANGELOG.md` (the #576 entry) and
`template.yaml`: S3 permits one conditional bucket-config operation per bucket at a time,
and CloudFormation deleted `TestSetBucketPolicy`, `TestSetBucketAutoDelete` and
`TestSetBucketNotificationConfiguration` simultaneously, so
`PutBucketNotificationConfiguration` returned `OperationAborted` ("A conflicting conditional
operation is currently in progress"). With no retry, a recoverable rollback became
`ROLLBACK_FAILED`. Fixed three ways: a bounded retry ladder on transient S3 codes, a
**Delete** that always reports success, and `DependsOn: TestSetBucketPolicy` to serialise
the two conditional writes. If you see `OperationAborted` on a release older than that fix,
the identical call typically succeeds minutes later — retry the rollback.

**`iam:UpdateAssumeRolePolicy` AccessDenied, then a wedge.**
📄 `docs/troubleshooting.md`: affects upgrades from before v0.6.2 to v0.6.2–v0.6.4 when
deploying with a CloudFormation service role lacking that permission. The rollback needs the
same permission, so the stack cannot self-recover:

```bash
aws cloudformation continue-update-rollback --stack-name <StackName> \
  --resources-to-skip <StackName>-CognitoAuthorizedRole
```

This is the one case where `--resources-to-skip` is the documented answer. 📄 Note also that
`continue-update-rollback` applies **only** to `UPDATE_ROLLBACK_FAILED` — it is invalid from
a CREATE rollback (`scripts/sdlc/tests/test_codebuild_failure_trail.py` asserts exactly
this).

## 4. What is not automated

Everything. ✅ Verified by reading all of `.gitlab-ci.yml` and all four
`.github/workflows/*.yml`: no job in either CI is keyed on a git tag, and neither
`scripts/aws-release.sh` nor `--public` appears anywhere in either. The GitLab
`integration_tests` job does deploy to AWS, but into an internal test pipeline, not to the
public buckets.

Done by hand, in order:

1. `make version V=x.y.z` — the only automated *part* of this list, and you still invoke it.
2. Pruning and cutting `CHANGELOG.md` (two skills, no script).
3. `bash scripts/aws-release.sh` — the release itself. No confirmation prompt, no dry-run
   mode, no check that `VERSION` is final, no check that the tree is clean, no check that
   you are in the right account.
4. `git tag` and pushing the tag.
5. The GitHub Release (⚠️ mechanism unverified — see step 6).
6. `make docs-deploy`.
7. The live validation battery, and committing its record.
8. ✅ **No headless or GovCloud template is published publicly.** `scripts/aws-release.sh`
   passes neither `--headless` nor `--govcloud`, so `idp-headless.yaml` and
   `idp-govcloud.yaml` are never written to the release prefix. Customers needing those
   build them from source. (Related: if the script *did* pass those flags, the transformed
   templates would be uploaded by the operations layer **after** `set_public_acls` has
   already run inside the publisher, so they would land private. See the PR that added this
   runbook for the follow-up.)
9. ✅ Marketplace extensions under `artifacts/genai-idp-mp/` — a separate `idp-feature-cli
   publish`, by the extension author.
10. ✅ The seller entitlement service template — deployed directly by a seller via
    `sam deploy` / `idp-feature-cli`, which is why `make version` bakes a literal version
    into it rather than leaving a token.
11. ⚠️ The two sibling repositories that pin themselves to a release
    (`cdklabs/genai-idp`, `awslabs/genai-idp-terraform`) — whether they need a bump, who
    does it, and on what timeline is not recorded in this repository.

## 5. Post-release verification checklist

All of these are unauthenticated HTTP requests, which is the point: if they succeed without
credentials, the objects are genuinely public. Run them for **all three regions**.

```bash
V=$(cat VERSION)
for R in us-west-2 us-east-1 eu-central-1; do
  B="https://s3.$R.amazonaws.com/aws-ml-blog-$R/artifacts/genai-idp"
  echo "== $R"
  curl -sI  "$B/idp-main_$V.yaml"     -o /dev/null -w '  versioned  %{http_code}\n'
  curl -sI  "$B/idp-main.yaml"        -o /dev/null -w '  floating   %{http_code}\n'
  curl -s   "$B/idp-main-latest.json"
  curl -s   "$B/idp-main_$V.yaml" | grep -m1 '^Description:'
done
```

- [ ] Versioned template returns **200** in all three regions.
- [ ] Floating `idp-main.yaml` returns **200** in all three regions.
- [ ] `idp-main-latest.json` reads `{"version": "<VERSION>", "templateUrl": …}` with the
      **region-local** bucket in `templateUrl`, in all three regions. ✅ A failure to write
      this pointer is non-fatal to the publish — it prints a yellow warning and continues —
      so this is a check the publish itself will not make for you.
- [ ] The `Description:` line of the versioned template ends `(v<VERSION>)`. 📄 The
      release-validation skill makes the same check its preflight step 4.
- [ ] The three `## Templates` URLs in `CHANGELOG.md` resolve:
      ```bash
      grep -oE "https://s3[^\`]*idp-main_$(cat VERSION)\.yaml" CHANGELOG.md \
        | xargs -n1 -I{} curl -sI {} -o /dev/null -w '%{http_code} {}\n'
      ```
- [ ] A versioned sub-artifact is public — spot-check one layer zip, whose exact name you
      can read out of the published template:
      ```bash
      curl -s "$B/idp-main_$V.yaml" | grep -om1 'idp-common-base-[0-9a-f]*\.zip'
      curl -sI "$B/$V/layers/<that name>" -o /dev/null -w '%{http_code}\n'
      ```
      This is the check that would have caught a publish that died before
      `set_public_acls`.
- [ ] The docs site serves the new content.
- [ ] The GitHub Release exists, is not a draft, and its body matches the CHANGELOG section.
- [ ] The live tiers: run the battery per
      [Release Validation](./release-validation/README.md) /
      `.claude/skills/release-validation.md`, and commit
      `docs/release-validation/v<VERSION>.md` plus its index row. Deploy variants, the
      `--headless` / `--govcloud` transforms, the in-place `PREV → VERSION` upgrade, RBAC,
      ZAP, the browser UX review and the benchmark A/B all live there. Do not re-derive them
      here.

## Known gaps in this runbook

Collected so they are not buried:

1. ⚠️ **The release credential.** Account, role, and how to assume it. Not in the repo.
2. ⚠️ **The `aws-ml-blog-*` bucket configuration.** Object Ownership, Public Access Block,
   and versioning — the last of which determines whether a bad `idp-main.yaml` is
   recoverable by any means other than re-publishing.
3. ⚠️ **Why these three regions**, and what it would take to add a fourth.
4. ⚠️ **How the GitHub Release is created.** The outcome is observable; the command is not.
5. ⚠️ **Whether the tag goes to both remotes**, and whether the GitLab tag is used for
   anything.
6. ⚠️ **The sibling repositories' pinning.** Whether `cdklabs/genai-idp` and
   `awslabs/genai-idp-terraform` need a coordinated bump.
7. ⚠️ **Nothing here has been executed.** The step ordering and the success criteria are
   read out of the code; the first person to follow this runbook end to end should correct
   it where reality differs.
