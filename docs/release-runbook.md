---
title: "Release Runbook"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Release Runbook

This page explains how a release of the GenAI IDP Accelerator is built and published,
so that you can publish your own releases to S3 buckets in an AWS account you own. A
published release is a set of CloudFormation templates and build artifacts in S3 that
anyone with the template URL can deploy with **Launch Stack**, `idp-cli deploy` or the
CloudFormation console.

The page covers what a release consists of, what must be true before you publish, the
ordered steps, how to recover from a failed or broken publish, and how to verify the
result.

## What a release is

A release is one run of `idp-cli publish` per AWS region you want to serve. Each run
builds the solution from the checked-out source tree and uploads it to one bucket in
that region:

```bash
idp-cli publish --source-dir . --bucket-basename <basename> --prefix <prefix> \
  --region <region> --public
```

- `--region` is the region the release is built for and published to.
- `--bucket-basename` names the bucket. The publisher appends the region, so the
  bucket it writes to is `<basename>-<region>`. If you omit the option, a name is
  generated for you.
- `--prefix` is the key prefix every object is written under.
- `--public` makes the published objects anonymously readable, which is what lets
  other accounts deploy the release. Leave it off for a private release that only your
  own account deploys. It covers the standard template and its artifacts only: the
  template variants written by `--headless` and `--govcloud` are uploaded without a
  public ACL, so on a public release they are not anonymously readable unless your bucket
  policy grants anonymous read.
- `--clean-build` forces every component to rebuild instead of reusing cached builds.

`make publish REGION=<region> BUCKET_BASENAME=<basename> PREFIX=<prefix> PUBLIC=1` is a
wrapper around the same command. The repository's own release script,
`scripts/aws-release.sh`, is a list of these commands, one per region, run with `set -e`
so that the first failure stops it. For your own releases, write an equivalent script
that names your buckets and regions.

**Each region's release is self-contained.** The template published to a region points
at the code and nested templates in that same region's bucket, so a stack created from
one region's template never reads another region's bucket. Publish every region you
advertise; a region that was not published cannot be deployed.

### What lands in each bucket

`<VERSION>` is the contents of the repository's `VERSION` file.

| Key | Changes | What reads it |
|---|---|---|
| `<prefix>/idp-main.yaml` | Overwritten by every release | Launch Stack buttons, and `idp-cli deploy` (see below) |
| `<prefix>/idp-main_<VERSION>.yaml` | Written once per version | Anyone who wants to deploy or upgrade to that exact version, such as the `## Templates` links in `CHANGELOG.md` |
| `<prefix>/idp-main-latest.json` | Overwritten by every release | The Web UI's "update available" indicator in deployed stacks |
| `<prefix>/<VERSION>/…` | Written once per version | CloudFormation and CodeBuild while a stack deploys: nested and pattern templates, Lambda code and layers, the Web UI bundle, the configuration library and samples |
| `<prefix>/extensions/<id>/…` | Not versioned | The feature platform's extension catalog |

`idp-main.yaml` and `idp-main-latest.json` are the only keys a release overwrites, so
they are the only keys that move when you publish or roll back.

`idp-cli deploy` does not take a bucket from configuration. When it is not given a
template, it uses the URLs in `TEMPLATE_URLS` in `lib/idp_cli_pkg/idp_cli/cli.py`, which
name the floating `idp-main.yaml` key in each region. To make `idp-cli deploy` use your
releases by default, change those URLs; otherwise pass your template with
`--template-url`.

### What the publisher writes into the template

While packaging, the publisher replaces tokens in `template.yaml`:

- `<VERSION>` becomes the `VERSION` file's contents. The published template's
  `Description` line ends with `(v<VERSION>)`, which is the quickest way to confirm which
  build an object is.
- The artifact bucket and prefix tokens become that region's bucket and
  `<prefix>/<VERSION>`, which is how a deployed stack finds its own code.
- The public-artifacts bucket and prefix tokens become the defaults of the
  `PublicArtifactsBucket` and `PublicArtifactsPrefix` stack parameters, which tell a
  deployed stack where to look for `idp-main-latest.json`. See
  [Version Update Indicator](./version-update-indicator.md).

## 1. Preconditions

### 1.1 Branch and tree state

- **`VERSION` must be a final version**, such as `1.2.3`, not `1.2.3.dev1` or
  `1.2.3rc1`. The publisher accepts any string and will publish a development version as
  a real release, with that string in every key. Check it by hand with `cat VERSION`.
- Set the version with `make version V=<x.y.z>`. It validates the version, then rewrites
  `VERSION`, the 9 package version files and the seller entitlement service template's
  version: 11 paths in all, which it lists when it finishes. Do not edit `VERSION` by
  hand.
- `CHANGELOG.md` should have its `[Unreleased]` section turned into a section for the new
  version, with the template URLs for each region you publish. The procedures are in
  `.claude/skills/prepare-changelog.md` and `.claude/skills/cut-release-changelog.md`.
  The URLs return 404 until the publish completes.
- The working tree must be clean and checked out at the commit you intend to tag. The
  publisher reads the working tree and `./VERSION` relative to the current directory, so
  whatever is checked out is what ships.

### 1.2 Validation before you publish

Run the offline checks on the release commit before publishing: `make lint`,
`make test`, `make typecheck` and `make dep-audit`, and the security scan with
`make srt-setup` followed by `make srt-scan`. Run the scan before you build, because
publishing adds build output to the tree.

The fuller validation programme, including the tiers that deploy the published
template, is described in [Release Validation](./release-validation/README.md) and
`.claude/skills/release-validation.md`. Most of its tiers test the published artifact,
so they run after Step 3.

### 1.3 Local toolchain

The publisher checks for `aws`, `sam` (1.129.0 or later) and `uv` on the `PATH`, and for
Python 3.12 or later. It also needs tools it does not check for in advance:

- **Node.js and npm.** The publish builds the Web UI with `npm ci` and `npm run build`,
  and fails if that build fails.
- **`ruff`.** The publish runs `ruff check` and `ruff format --check` and fails on
  findings.
- **`cfn-lint`.** If it is not installed, the publish skips CloudFormation linting
  without failing. Run `make cfn-lint` yourself on the release commit.

Docker is not needed. Container images are built by CodeBuild when a stack deploys.

### 1.4 Account, credentials and buckets

The identity you publish with needs, on each release bucket: `s3:ListBucket`,
`s3:GetObject`, `s3:PutObject`, and `s3:PutObjectAcl` for a public release. It also
needs `sts:GetCallerIdentity` and `cloudformation:ValidateTemplate`. If the bucket does
not exist yet, the publisher creates it and enables versioning, which needs
`s3:CreateBucket` and `s3:PutBucketVersioning`. It also tries to add a bucket policy
that denies non-TLS requests (`s3:GetBucketPolicy` and `s3:PutBucketPolicy`), and only
prints a warning if it cannot.

**A public release works through object ACLs.** `--public` sets `public-read` on every
object it publishes. For that to work, the bucket must have ACLs enabled (Object
Ownership not set to *Bucket owner enforced*), and its Block Public Access settings must
allow public ACLs. New S3 buckets have both protections on by default, and the publisher
does not change them, so configure each bucket before its first public release. If you
do not, the publish fails at the ACL step, after the uploads.

Use a dedicated bucket for public releases, and confirm which account your shell is
using before publishing:

```bash
aws sts get-caller-identity
```

Environment-variable credentials take precedence over `AWS_PROFILE`, so unset
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_SESSION_TOKEN` if you mean to use a
profile.

Enabling versioning on the release buckets is recommended. It lets you recover an
earlier copy of an overwritten key if you ever need to.

## 2. Ordered steps

### Step 1 — Stamp the version

```bash
make version V=<x.y.z>
cat VERSION                      # must print exactly <x.y.z>
git describe --tags --abbrev=0   # should print the previous release tag
```

**Success:** `VERSION` and the 9 package version files read `<x.y.z>`, and the command
lists the 11 paths it changed.

### Step 2 — Cut the CHANGELOG

Follow `.claude/skills/prepare-changelog.md`, then `.claude/skills/cut-release-changelog.md`.
`## [Unreleased]` becomes `## [<x.y.z>]`, followed by a `## Templates` block with one
template URL per region.

```bash
grep -n "^## \[" CHANGELOG.md | head -5              # one section for the new version
grep -n "idp-main_$(cat VERSION).yaml" CHANGELOG.md  # one hit per region you publish
```

**Success:** one versioned template URL per region. They return 404 until Step 3.

### Step 3 — Publish the artifacts

From the repository root, with a clean tree on the release commit, run your release
script and keep its log:

```bash
cd <repo root>
mkdir -p scratch
bash <your release script> 2>&1 | tee scratch/release-<x.y.z>.log
echo "exit status: ${PIPESTATUS[0]}"   # zsh: ${pipestatus[1]}
```

With the output piped to `tee`, `$?` is `tee`'s exit status, not the script's, so read
`PIPESTATUS` (or run with `set -o pipefail`).

How the publish behaves:

- **Run it from the repository root.** `--source-dir .` and `./VERSION` are relative to
  the current directory. If you run the script detached or from elsewhere, pass an
  absolute `--source-dir`.
- **Each region is a full build.** The build cache is keyed on the bucket and region as
  well as the source, so plan for one complete build per region.
- **Public ACLs are applied at the end of each region's run**, after all of that
  region's uploads.

**Success looks like this for each region:** `✅ All builds completed successfully`, then
`✅ Public ACLs set successfully`, then `✅ All advertised keys are anonymously readable`
(listing each key it checked), then a Deployment Outputs block with the Launch Stack URL
and template URL, then `✅ Done!`.

If the readability check reports `❌` for a key, the publish stops: the objects are
uploaded but are not publicly readable. Check the bucket's Object Ownership and Block
Public Access settings, then run the publish again. A `⚠️ ... UNVERIFIED` result means
the machine could not reach the public S3 endpoint to check. Verify by hand with Step 4
before announcing the release.

### Step 4 — Verify the artifacts are public and correct

Run the checklist in [§5](#5-post-release-verification-checklist) before announcing
anything.

### Step 5 — Tag and push

```bash
git tag v<x.y.z>
git push <remote> v<x.y.z>
```

### Step 6 — Create the GitHub Release

Create a release for the tag, using the version as the title and the version's
CHANGELOG section as the body:

```bash
gh release create v<x.y.z> --title "v<x.y.z>" \
  --notes-file <file containing the CHANGELOG section>
```

### Step 7 — Publish the documentation site

```bash
make docs-deploy
```

This builds the documentation site and pushes it to the repository's `gh-pages` branch.
The documentation workflow in CI only builds the site as a check; it does not publish
it.

**Success:** the GitHub Pages site serves the new content.

### Step 8 — Run the live release-validation battery

Deploy and test the published release as described in
[Release Validation](./release-validation/README.md) and
`.claude/skills/release-validation.md`. It covers deploy variants, the `--headless` and
`--govcloud` template transforms, an in-place upgrade from the previous release, API
authorization, security scanning and a benchmark comparison. Expect it to take most of
a working day and to incur Bedrock charges.

## 3. Failure and recovery

### 3.1 The publish fails partway through

**Re-run the same command.** It is safe to repeat:

- The publisher never deletes objects. Anything a failed run uploaded is either
  overwritten by the retry or left unreferenced.
- Build checksums are written only after a successful run, so the retry rebuilds and
  re-uploads whatever did not finish.
- Uploads that already succeeded are cheap to repeat. Layer zips are named by content
  hash and skipped if present, and the configuration library is uploaded with
  `aws s3 sync`, which skips unchanged files.

If you suspect the build cache, for example after editing a data file bundled into a
Lambda layer, add `--clean-build`.

**Re-run promptly.** `idp-main.yaml` and `idp-main-latest.json` are written before the
ACL step at the end of the run. Until a run completes, the floating template can already
name the new version while some of that version's artifacts are not yet public, and a
Launch Stack in that window fails with access errors.

**If one region fails**, the regions before it are published and the regions after it
are untouched, so the floating template and the update indicator disagree by region.
Re-run the whole script. Regions that already finished cost only their rebuild time.

### 3.2 A published template turns out to be broken

There is no unpublish, and the versioned `idp-main_<VERSION>.yaml` should stay in place:
release notes and validation records link to it.

What you can change are the two keys a release overwrites. **The recommended action is
to re-publish the previous release tag**, which points both keys back at a known-good
version consistently:

```bash
git checkout v<PREV>
cat VERSION                       # must read <PREV>
bash <your release script>
```

This rewrites `idp-main.yaml` and `idp-main-latest.json` in every region to point at
`<PREV>`. The previous version's artifacts are already in each bucket, so this is mostly
rebuild and upload time. A region that never had `<PREV>` published gets a full first
publish. Then publish a patch release with the fix.

If you copy an earlier template over `idp-main.yaml` by hand instead, you must also
rewrite `idp-main-latest.json` in each region. Otherwise every deployed Web UI keeps
offering the broken version as an update. The pointer is a JSON object,
`{"version": "<version>", "templateUrl": "<versioned template URL>"}`, and on a public
release it needs `public-read` or no deployed stack can read it:

```bash
aws s3api put-object-acl --bucket <basename>-<region> \
  --key <prefix>/idp-main-latest.json --acl public-read --region <region>
```

### 3.3 Protecting customers who upgrade

A release is also what existing stacks move to with an in-place update. Two checks
prevent the failures that are hardest to recover from:

- **Before changing a configuration default, confirm the previous release accepts the
  new value.** A failed update rolls back to the previous release's code, which then
  reads configuration written by the new release. A value the old code rejects can leave
  the stack unable to finish its rollback.
- **Validate `config_library/pricing.yaml` before publishing.** It is re-validated during
  both update and rollback, so an invalid file can block both:

  ```bash
  PYTHONPATH=lib/idp_common_pkg python3 -c "import yaml; from idp_common.config.models import PricingConfig; PricingConfig(**yaml.safe_load(open('config_library/pricing.yaml')))"
  ```

[Troubleshooting](./troubleshooting.md) covers recovering a stack whose update or
rollback has failed.

## 4. What is not automated

No CI job publishes a release. Every step is run by hand, in this order:

1. `make version V=<x.y.z>`.
2. Preparing and cutting `CHANGELOG.md`.
3. Running the release script. It has no confirmation prompt or dry-run mode, and it
   does not check that `VERSION` is final, that the tree is clean, or which account you
   are using.
4. Tagging and pushing the tag.
5. Creating the GitHub Release.
6. `make docs-deploy`.
7. The live validation battery.

The release script publishes only the standard template. The `--headless` and
`--govcloud` variants are normally generated by `idp-cli deploy --headless` and
`idp-cli deploy --govcloud` at deploy time. A publish run with `--headless` or
`--govcloud` also uploads `<prefix>/idp-headless.yaml` or `<prefix>/idp-govcloud.yaml`,
but without a public ACL, so other accounts cannot read them unless your bucket policy
grants anonymous read.

Marketplace extensions are published separately by their authors with
`idp-feature-cli publish`, under a different prefix so the two cannot collide.

## 5. Post-release verification checklist

Run these as unauthenticated requests from a shell with no AWS credentials. A request
that succeeds without credentials proves the object is public, while a credential in
scope could read a private object and hide the problem. Set `RELEASES` to one
`<region>:<basename>` pair per publish line in your release script, since the
basename can differ from region to region, and `PREFIX` to its `--prefix`:

```bash
V=$(cat VERSION)
for RB in $RELEASES; do
  R="${RB%%:*}"; BASENAME="${RB##*:}"
  B="https://s3.$R.amazonaws.com/$BASENAME-$R/$PREFIX"
  echo "== $R"
  curl -sI "$B/idp-main_$V.yaml"  -o /dev/null -w '  versioned  %{http_code}\n'
  curl -sI "$B/idp-main.yaml"     -o /dev/null -w '  floating   %{http_code}\n'
  curl -s  "$B/idp-main-latest.json"; echo
  curl -s  "$B/idp-main_$V.yaml" | grep -m1 '^Description:'
  Z=$(curl -s "$B/idp-main_$V.yaml" | grep -om1 'idp-common-base-[0-9a-f]*\.zip')
  curl -sI "$B/$V/layers/$Z" -o /dev/null -w "  layer      %{http_code}  $Z\n"
done
```

- [ ] The versioned template returns **200** in every region.
- [ ] The floating `idp-main.yaml` returns **200** in every region.
- [ ] `idp-main-latest.json` reads `{"version": "<VERSION>", "templateUrl": …}`, with
      that region's own bucket in `templateUrl`, in every region.
- [ ] The versioned template's `Description:` line ends with `(v<VERSION>)`.
- [ ] A layer zip from the versioned prefix returns **200** in every region.
- [ ] The template URLs in the new `CHANGELOG.md` section resolve:
      ```bash
      grep -oE "https://s3[^\`]*idp-main_$(cat VERSION)\.yaml" CHANGELOG.md \
        | xargs -n1 -I{} curl -sI {} -o /dev/null -w '%{http_code} {}\n'
      ```
- [ ] A test stack deploys from the published template.
- [ ] The documentation site serves the new content.
- [ ] The GitHub Release exists, is not a draft, and its body matches the CHANGELOG
      section.
- [ ] The live validation battery has run, per
      [Release Validation](./release-validation/README.md).
