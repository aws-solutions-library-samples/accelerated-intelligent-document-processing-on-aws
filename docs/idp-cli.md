---
title: "IDP CLI - Command Line Interface for Batch Document Processing"
---

# IDP CLI - Command Line Interface for Batch Document Processing

A command-line tool for batch document processing with the GenAI IDP Accelerator.

## Features

✨ **Batch Processing** - Process multiple documents from CSV/JSON manifests  
📊 **Live Progress Monitoring** - Real-time updates with rich terminal UI  
🔄 **Resume Monitoring** - Stop and resume monitoring without affecting processing  
📁 **Flexible Input** - Support for local files and S3 references  
🔍 **Comprehensive Status** - Track queued, running, completed, and failed documents  
📈 **Batch Analytics** - Success rates, durations, and detailed error reporting  
🎯 **Evaluation Framework** - Validate accuracy against baselines with detailed metrics  
💬 **Agent Chat** - Interactive Agent Companion Chat from the terminal with Analytics, Error Analyzer, and more

Demo:

https://github.com/user-attachments/assets/3d448a74-ba5b-4a4a-96ad-ec03ac0b4d7d



## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
  - [Machine-readable output](#machine-readable-output)
- [Commands Reference](#commands-reference)
  - [deploy](#deploy)
  - [publish](#publish)
  - [delete](#delete)
  - [bootstrap](#bootstrap)
  - [process](#process--run-inference)
  - [reprocess](#reprocess--rerun-inference)
  - [status](#status)
  - [download-results](#download-results)
  - [use-as-baseline](#use-as-baseline)
  - [delete-documents](#delete-documents)
  - [generate-manifest](#generate-manifest)
  - [validate-manifest](#validate-manifest)
  - [list-batches](#list-batches)
  - [stop-workflows](#stop-workflows)
  - [load-test](#load-test)
  - [discover](#discover)
  - [discover-multidoc](#discover-multidoc)
  - [remove-deleted-stack-resources](#remove-deleted-stack-resources)
  - [config-create](#config-create)
  - [config-validate](#config-validate)
  - [config-download](#config-download)
  - [config-upload](#config-upload)
  - [config-list](#config-list)
  - [config-revisions](#config-revisions)
  - [config-activate](#config-activate)
  - [config-delete](#config-delete)
  - [test-result](#test-result)
  - [abort-test-run](#abort-test-run)
  - [test-compare](#test-compare)
  - [chat](#chat)
- [Complete Evaluation Workflow](#complete-evaluation-workflow)
  - [Step 1: Deploy Your Stack](#step-1-deploy-your-stack)
  - [Step 2: Initial Processing from Local Directory](#step-2-initial-processing-from-local-directory)
  - [Step 3: Download Extraction Results](#step-3-download-extraction-results)
  - [Step 4: Manual Validation & Baseline Preparation](#step-4-manual-validation--baseline-preparation)
  - [Step 5: Create Manifest with Baseline References](#step-5-create-manifest-with-baseline-references)
  - [Step 6: Process with Evaluation Enabled](#step-6-process-with-evaluation-enabled)
  - [Step 7: Download and Review Evaluation Results](#step-7-download-and-review-evaluation-results)
- [Evaluation Analytics](#evaluation-analytics)
  - [Query Aggregated Results with Athena](#query-aggregated-results-with-athena)
  - [Use Agent Analytics in the Web UI](#use-agent-analytics-in-the-web-ui)
- [Manifest Format Reference](#manifest-format-reference)
- [Advanced Usage](#advanced-usage)
- [Troubleshooting](#troubleshooting)

## Installation

### Prerequisites

- Python 3.12 or higher
- AWS credentials configured (via AWS CLI or environment variables)
- An active IDP Accelerator CloudFormation stack

### Install from source

```bash
make setup-venv
source .venv/bin/activate
```

### Install with test dependencies

Run this from the repository root. The CLI requires `idp-sdk`, which requires
`idp_common`; both names on public PyPI belong to unrelated parties, so all three
packages go in a single command and all three come from a path. See
[Installing First-Party Packages Safely](dependency-confusion.md).

```bash
pip install -e lib/idp_common_pkg -e lib/idp_sdk -e "lib/idp_cli_pkg[test]"
```

## Makefile Shortcuts

The root `Makefile` provides convenience wrappers for the most common `idp-cli` commands. These use the project's `.venv` Python automatically — no need to `source .venv/bin/activate` first.

```bash
# Publish artifacts to S3
make publish REGION=us-east-1
make publish REGION=us-east-1 BUCKET_BASENAME=my-artifacts PREFIX=v1
make publish REGION=us-gov-west-1 HEADLESS=1

# Deploy / update a stack (--wait is the default)
make deploy STACK_NAME=my-idp ADMIN_EMAIL=me@example.com
make deploy STACK_NAME=my-idp-dev ADMIN_EMAIL=me@example.com FROM_CODE=1
make deploy STACK_NAME=my-idp CUSTOM_CONFIG=./my-config.yaml
make deploy STACK_NAME=my-idp TAGS="Owner=docs-team,Environment=prod"

# Delete a stack
make delete-stack STACK_NAME=test-stack FORCE=1 FORCE_DELETE_ALL=1
```

**First-class Make variables** (common flags):

| Variable | Target(s) | Maps to CLI flag |
|---|---|---|
| `REGION` | publish, deploy, delete-stack | `--region` |
| `STACK_NAME` | deploy, delete-stack | `--stack-name` |
| `ADMIN_EMAIL` | deploy | `--admin-email` |
| `FROM_CODE=1` | deploy | `--from-code .` |
| `HEADLESS=1` | publish, deploy | `--headless` |
| `PUBLIC=1` | publish | `--public` |
| `BUCKET_BASENAME` | publish | `--bucket-basename` |
| `PREFIX` | publish | `--prefix` |
| `CUSTOM_CONFIG` | deploy | `--custom-config` |
| `TAGS` | deploy | `--tags` |
| `TEMPLATE_URL` | deploy | `--template-url` |
| `TEMPLATE_FILE` | deploy | `--template-file` |
| `NO_WAIT=1` | deploy, delete-stack | omits `--wait` |
| `FORCE=1` | delete-stack | `--force` |
| `EMPTY_BUCKETS=1` | delete-stack | `--empty-buckets` |
| `FORCE_DELETE_ALL=1` | delete-stack | `--force-delete-all` |

**Uncommon flags** — pass anything else via `EXTRA_ARGS`:

```bash
make deploy STACK_NAME=my-idp EXTRA_ARGS="--role-arn arn:aws:iam::123:role/Foo --no-rollback"
make publish REGION=us-east-1 EXTRA_ARGS="--clean-build --verbose"
```

Run `make help` to see all available targets, or `idp-cli <command> --help` for the full option reference.

---

## Quick Start

### Global Options

The CLI supports an optional `--profile` parameter to specify which AWS credentials profile to use:

```bash
idp-cli --profile my-profile <command> [options]
```

- Can be placed anywhere in the command
- Only affects that specific command execution
- Automatically applies to all AWS SDK calls
- If not specified, uses default AWS credentials

**Examples:**
```bash
# Profile before command
idp-cli --profile production deploy --stack-name my-stack ...

# Profile after command
idp-cli deploy --profile production --stack-name my-stack ...

# Profile at the end
idp-cli deploy --stack-name my-stack --profile production ...
```

#### Region and its precedence

`--region` is a **per-command** option, not a global one, so it goes after the
subcommand:

```bash
idp-cli config-upload --stack-name my-stack --config-file ./config.yaml \
    --config-profile v2 --region eu-west-1
```

The resolution order is:

1. `--region` on the subcommand, if given.
2. Otherwise boto3's own chain: `AWS_REGION`, then `AWS_DEFAULT_REGION`, then the
   `region` configured for the selected `--profile` (or `AWS_PROFILE`), then EC2
   instance metadata.

Nothing substitutes a hardcoded region for the configuration commands, so a
command run with no `--region` and no region resolvable from the environment fails
with boto3's `NoRegionError` rather than guessing.

`--region` applies to every AWS call a command makes, not only to the
CloudFormation lookup that resolves a resource's name. That distinction is the
whole point: a stack's `ConfigurationTable` physical id is not region-qualified, so
a command that looked the name up in one region and then read or wrote it in
another would hit a *different stack's* table on a multi-region account — and
report success. It therefore covers

- the DynamoDB read and write of the Configuration Table,
- the S3 write of configuration revision history,
- the document classes `config-sync-bda` derives from a BDA project, and the BDA
  project calls themselves,
- the schema and rules that `discover` and `discover-multidoc` write back,
- the model-limits read on `config-upload`'s validation path, which would
  otherwise fall back silently to the on-disk defaults and could reject a
  configuration that is legitimately above a default cap.

A whole-tree check (`scripts/tests/test_config_region_threading.py`) asserts that
no code outside a Lambda builds a configuration client without a region, so a new
command cannot reintroduce the gap.

Three commands take no `--region` because they make no AWS calls at all:
`config-create`, `config-validate` and `validate-manifest`.

### Machine-readable output

Every payload the CLI writes to stdout for a program to read is written verbatim:
no colour, no syntax highlighting, and no wrapping to the terminal width. On the
commands below, stdout carries **only** that payload — progress and status lines
go to stderr — so piping and redirecting are safe:

| Command | Payload on stdout |
|---|---|
| `config-revisions --json` | JSON revision history |
| `status --format json` | JSON status document |
| `config-download` without `--output` | configuration YAML |
| `config-create` without `--output` | configuration-template YAML |
| `bootstrap` without `--stack-name` | the authored JSON schema |

```bash
# Parse JSON directly
idp-cli config-revisions --stack-name my-stack --config-profile lending --json \
    | jq -r '.revisions[] | select(.published) | .revision'

# Redirect YAML straight to a file
idp-cli config-download --stack-name my-stack > config.yaml

# Progress is on stderr, so discard it without touching the payload
idp-cli status --stack-name my-stack --batch-id batch-123 --format json 2>/dev/null \
    | jq '.exit_code'
```

`discover` and `discover-multidoc` are the exception. Their schemas are written
unrendered too, but they print a `Discovered schemas:` heading and per-document
progress to stdout alongside them, so **use `-o` / `--output`** to capture a
schema from those two rather than redirecting stdout:

```bash
idp-cli discover-multidoc --dir ./samples/ -o ./schemas/
```

Human-facing output — tables, progress, status lines — is still styled when
stdout is a terminal, and Rich disables the styling itself when it is not. Before
v0.6.9 these payloads were rendered the same way as that human output, so
`--json` carried ANSI escape codes and a long line of downloaded YAML was folded
to the console width ([#905](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/905)).

### Deploy a stack and process documents in 3 commands:

```bash
# 1. Deploy stack (10-15 minutes)
idp-cli deploy \
    --stack-name my-idp-stack \
    --admin-email your.email@example.com \
    --wait

# 2. Process documents from a local directory
idp-cli process \
    --stack-name my-idp-stack \
    --dir ./my-documents/ \
    --monitor

# 3. Download results
idp-cli download-results \
    --stack-name my-idp-stack \
    --batch-id <batch-id-from-step-2> \
    --output-dir ./results/
```

**That's it!** Your documents are processed with OCR, classification, extraction, assessment, and summarization.

For evaluation workflows with accuracy metrics, see the [Complete Evaluation Workflow](#complete-evaluation-workflow) section.

---

## Commands Reference

> **Flag naming:** the named configuration entity is a **Configuration Profile**,
> selected with `--config-profile`. The former name, `--config-version`, is kept
> as an accepted alias so existing scripts keep working — both spellings set the
> same value, and there is no plan to remove the old one. New scripts should use
> `--config-profile`. `--config-revision` is unrelated: it selects a *revision
> within* a profile. See [configuration-profiles.md](configuration-profiles.md#terminology-which-word-means-what).

> **Comma-separated options:** every option documented below as comma-separated
> (`--document-ids`, `--test-run-ids`, `--file-types`, `--check-stack-regions`,
> `--features`, `--tags`) parses through one shared helper that **drops blank
> segments**, so a trailing or doubled comma is
> harmless — `--document-ids "a,b,"` names two documents, not three.
>
> For all of them **except `--tags`**, a value containing **no** non-blank segment is
> **refused** with exit 1 rather than treated as one value that is the empty string.
> That matters most for a list a script built from a variable that turned out to be
> empty: `--document-ids ""` used to ask about a document whose S3 object key was `""`,
> and `--document-ids ","` used to announce "Selected 2 document(s) for deletion".
> `--tags ","` is **not** refused — it sets no tags and proceeds, which is exactly what
> omitting `--tags` does, so there is no wrong action for a refusal to prevent.

### `deploy`

Deploy or update an IDP CloudFormation stack.

**Usage:**
```bash
idp-cli deploy [OPTIONS]
```

**Required for New Stacks:**
- `--stack-name`: CloudFormation stack name
- `--admin-email`: Admin user email

**Optional Parameters:**
- `--from-code`: Deploy from local code by building and publishing artifacts (path to project root)
- `--template-url`: URL to CloudFormation template in S3 (optional, auto-selected based on region)
- `--template-file`: Local path to a pre-built CloudFormation template (from a previous `publish`)
- `--custom-config`: Path to local config file or S3 URI
- `--max-concurrent`: Maximum concurrent workflows (default: 100)
- `--log-level`: Logging level (`DEBUG`, `INFO`, `WARN`, `ERROR`). No CLI default: omit it to take the template default (`WARN`) on a new stack, or to keep an existing stack's current value on an update. `INFO` and `DEBUG` can write presigned URLs, document contents and PII to CloudWatch — see [Monitoring](./monitoring.md#loglevel--what-warn-turns-off)
- `--enable-hitl`: **Deprecated and refused if `true`.** HITL is a configuration
  setting rather than a stack parameter (the `EnableHITL` parameter was removed in
  v0.4.11) — enable it in the Web UI under **Configuration → Assessment & HITL
  Configuration**, or in the config YAML passed to `--custom-config`. The flag is
  still accepted as `false` so existing scripts keep working.
- `--parameters`: Additional CloudFormation parameters as `key=value,key2=value2`. A
  new pair starts at a comma or whitespace followed by `key=` (so a space-separated
  list, as `aws cloudformation deploy --parameter-overrides` takes, also works), and
  everything up to the next pair is one value — so a value may itself contain commas
  (`SubnetIds=subnet-a,subnet-b`) and `=` signs (a metadata URL with a query string,
  a base64 value). Whitespace around the `=` is ignored. Two things it cannot read as
  a pair are printed back to you rather than passing unremarked: text before the first
  pair, which is named and not submitted, and a value that looks like it swallowed a
  pair — a key holding a character CloudFormation does not allow, or pairs separated
  with `;`, `|` or a stray backslash — which is named together with the parameter it
  landed in, since a value may contain commas and so cannot be split back apart. The
  reason for the noise is that a parameter which never reached CloudFormation is
  indistinguishable afterwards from one deliberately left at its default. Pairs
  separated with `&` or `?` are the one case read silently as a value, because that is
  exactly what a query string looks like.
- `--tags`: Stack tags as `key=value,key2=value2`. CloudFormation applies these to the stack and propagates them to all taggable resources and nested stacks — useful for governance/ownership (e.g. `Owner`, `Team`, `Environment`). See [Resource tagging](#resource-tagging) below.
- `--wait`: Wait for stack operation to complete
- `--no-rollback`: Disable rollback on stack creation failure
- `--region`: AWS region (optional, auto-detected)
- `--role-arn`: CloudFormation service role ARN (optional)
- `--headless`: Deploy a **headless (no-UI) stack** — removes CloudFront, the UI REST API (the `APIRESOLVERSTACK` nested stack holding the API Gateway REST API, its dispatcher, and the UI-only resolver Lambdas), Cognito, WAF, agents, HITL, and Test Studio. Required for GovCloud; also valid in Commercial regions for API-only / pipeline integrations. See [Headless Deployment](./headless-deployment.md).
- `--govcloud`: Deploy the **GovCloud template variant** — keeps the full Web UI but removes every `AWS::CloudFront::*` resource (CloudFront does not exist in GovCloud) and forces API Gateway UI hosting. Mutually exclusive with `--headless`. If the GovCloud template cannot be produced, the deploy is **refused** rather than falling back to the commercial template: that template's CloudFront resources cannot exist in a GovCloud partition, so deploying it fails part-way through CREATE on a resource that looks unrelated to the flag. The error names the template that is missing (`.aws-sam/idp-govcloud.yaml`) and the `idp-cli publish --govcloud` command that produces it. See [GovCloud Deployment](./govcloud-deployment.md).
- `--bucket-basename`: S3 bucket basename for build artifacts (used with `--from-code`; region is appended automatically)
- `--prefix`: S3 key prefix for build artifacts (default: `idp-cli`, used with `--from-code`)
- `--public`: Make published S3 artifacts publicly readable (used with `--from-code`)
- `--build-max-workers`: Maximum concurrent build workers (used with `--from-code`)
- `--clean-build`: Force full rebuild by deleting checksum files (used with `--from-code`)
- `--no-validate-template`: Skip CloudFormation template validation

**Note:** `--from-code`, `--template-url`, and `--template-file` are mutually exclusive. Use `--from-code` for development/testing from local source, `--template-url` for production deployments from a pre-published template, and `--template-file` to deploy a locally-built template.

**Headless mode:**

- `--headless` can be combined with `--from-code .` (builds both variants and deploys the headless one) or used standalone against a pre-published template (the CLI downloads, transforms to headless, uploads to a temporary S3 location in your account, and deploys).
- When `--headless` is used without `--from-code`, the region **must** have a pre-published `idp-main.yaml`. For GovCloud (`us-gov-*`) or any unsupported region, add `--from-code .`.
- GovCloud regions are auto-detected and receive GovCloud-appropriate configuration defaults (ARN partition fixes, GovCloud Bedrock models, `lending-package-sample-govcloud` configuration preset).
- See the [Headless Deployment Guide](./headless-deployment.md) and [GovCloud Deployment Guide](./govcloud-deployment.md) for details.

**Auto-Monitoring for In-Progress Operations:**

If you run `deploy` on a stack that already has an operation in progress (CREATE, UPDATE, ROLLBACK), the command automatically switches to monitoring mode instead of failing. This is useful if you forgot to use `--wait` on the initial deploy - simply run the same command again to monitor progress:

```bash
# First run without --wait starts the deployment
$ idp-cli deploy --stack-name my-stack --admin-email user@example.com
✓ Stack CREATE initiated successfully!

# Second run - automatically monitors the in-progress operation
$ idp-cli deploy --stack-name my-stack
Stack 'my-stack' has an operation in progress
Current status: CREATE_IN_PROGRESS

Switching to monitoring mode...

[Live progress display...]

✓ Stack CREATE completed successfully!
```

Supported in-progress states: `CREATE_IN_PROGRESS`, `UPDATE_IN_PROGRESS`, `DELETE_IN_PROGRESS`, `ROLLBACK_IN_PROGRESS`, `UPDATE_ROLLBACK_IN_PROGRESS`, and cleanup states.

**Examples:**

```bash
# Create new stack
idp-cli deploy \
    --stack-name my-idp \
    --admin-email user@example.com \
    --wait

# Update with custom config
idp-cli deploy \
    --stack-name my-idp \
    --custom-config ./updated-config.yaml \
    --wait

# Update parameters
idp-cli deploy \
    --stack-name my-idp \
    --max-concurrent 200 \
    --log-level DEBUG \
    --wait

# Deploy with governance/ownership tags (propagated to all resources)
idp-cli deploy \
    --stack-name my-idp \
    --admin-email user@example.com \
    --tags "Owner=docs-team,Team=idp,Environment=prod" \
    --wait

# Deploy with custom template URL (for regions not auto-supported)
idp-cli deploy \
    --stack-name my-idp \
    --admin-email user@example.com \
    --template-url https://s3.eu-west-1.amazonaws.com/my-bucket/idp-main.yaml \
    --region eu-west-1 \
    --wait

# Deploy with CloudFormation service role and permissions boundary.
# If you use the example service role from iam-roles/cloudformation-management/,
# the stack name must start with its ManagedStackNamePrefix (default `idp`) and
# PermissionsBoundaryArn must match the boundary that role requires — role
# creation is denied otherwise. Both values are stack outputs of that role.
idp-cli deploy \
    --stack-name idp-demo \
    --admin-email user@example.com \
    --role-arn arn:aws:iam::123456789012:role/idp-service-role-CFServiceRole \
    --parameters "PermissionsBoundaryArn=arn:aws:iam::123456789012:policy/MyPermissionsBoundary" \
    --wait

# Deploy from local source code (for development/testing)
idp-cli deploy \
    --stack-name my-idp-dev \
    --from-code . \
    --admin-email user@example.com \
    --wait

# Update existing stack from local code changes
idp-cli deploy \
    --stack-name my-idp-dev \
    --from-code . \
    --wait

# Deploy with rollback disabled (useful for debugging failed deployments)
idp-cli deploy \
    --stack-name my-idp \
    --admin-email user@example.com \
    --no-rollback \
    --wait

# Deploy a HEADLESS stack in a commercial region (no UI — API-only)
# The CLI downloads the published template, transforms it to headless,
# uploads to a temporary S3 location, and deploys.
idp-cli deploy \
    --stack-name my-idp-headless \
    --region us-east-1 \
    --headless \
    --wait

# Deploy a HEADLESS stack from local source (development iteration)
idp-cli deploy \
    --stack-name my-idp-headless-dev \
    --region us-east-1 \
    --from-code . \
    --headless \
    --wait

# Deploy to GovCloud WITH the full Web UI (--govcloud; --from-code is required)
idp-cli deploy \
    --stack-name my-idp-govcloud \
    --region us-gov-west-1 \
    --from-code . \
    --govcloud \
    --admin-email user@example.com \
    --wait

# Deploy to GovCloud headless (no UI; --from-code is required)
idp-cli deploy \
    --stack-name my-idp-govcloud \
    --region us-gov-west-1 \
    --from-code . \
    --headless \
    --wait
```

> **Headless?** See the [Headless Deployment Guide](./headless-deployment.md) for when to use it (not just GovCloud — also API-only / pipeline integrations in Commercial regions) and the [GovCloud Deployment Guide](./govcloud-deployment.md) for GovCloud-specific considerations.

#### Resource tagging

`--tags "key=value,key2=value2"` applies **CloudFormation stack-level tags**. CloudFormation adds them to the stack and automatically propagates them to all taggable resources it creates — including the nested stacks (pattern, API resolvers, KB, discovery, feature platform) and their resources — so you tag the whole deployment in one place.

Notes and caveats:

- **Format:** comma-separated `key=value` pairs. Tag keys may contain spaces and the characters `. : / + - _` (a value may itself contain `=`; only the first `=` splits key from value). Commas inside a tag value are not supported.
- **Update behavior:** re-running `deploy` with `--tags` **replaces** the stack's entire tag set with what you pass. Omitting `--tags` on an update **preserves** the existing tags (unlike a bare AWS API call, which would clear them).
- **Not every resource type accepts propagated tags.** CloudFormation propagation is best-effort — a small number of resource types (e.g. some Cognito, CloudFront, and custom resources) do not receive stack tags. This is an AWS platform limitation, not a configuration option.
- **Cost allocation:** to use these tags in AWS Cost Explorer / cost allocation reports you must additionally activate them as *cost allocation tags* in the Billing console (a one-time, account-level step); this CLI option does not do that for you.

```bash
idp-cli deploy \
    --stack-name my-idp \
    --admin-email user@example.com \
    --tags "Owner=docs-team,Team=idp,Environment=prod" \
    --wait
```

---

### `publish`

Build IDP CloudFormation artifacts locally and publish them to S3. Produces a `template_url` and a 1-click CloudFormation launch URL. Optionally also produces a **headless** (no-UI) template variant.

Use `publish` when you want to build and stage artifacts without deploying immediately — for example, to share the template with other accounts, keep a known-good build, or run a separate deploy step later.

**Usage:**
```bash
idp-cli publish [OPTIONS]
```

**Options:**
- `--source-dir` (default: `.`): Path to the IDP project root directory
- `--region` (required): AWS region where artifacts will be uploaded and deployed
- `--bucket-basename`: S3 bucket basename for artifacts (region is appended automatically; auto-generated if not provided)
- `--prefix`: S3 key prefix for artifacts (default: `idp-cli`)
- `--headless`: Also generate a **headless (no-UI) template variant**. For commercial regions this produces `idp-main.yaml` **and** `idp-headless.yaml`; for GovCloud (`us-gov-*`) the headless template is additionally updated with GovCloud configuration defaults (ARN partition, GovCloud Bedrock models, `lending-package-sample-govcloud` preset).
- `--govcloud`: Also generate the **GovCloud template variant** — the full Web UI with every `AWS::CloudFront::*` resource removed and API Gateway UI hosting forced. Writes `.aws-sam/idp-govcloud.yaml` beside `idp-main.yaml` and uploads it as `idp-govcloud.yaml`. The transform is linted against a GovCloud region, so an unsupported resource type that survived it fails the publish with the `cfn-lint` finding rather than at deploy time. Deploy the result with `idp-cli deploy --template-file .aws-sam/idp-govcloud.yaml`, or build and deploy in one step with `idp-cli deploy --from-code . --govcloud`.
- `--public`: Make S3 artifacts publicly readable (for shared deployments)
- `--max-workers`: Maximum concurrent build workers (default: auto-detect)
- `--clean-build`: Force full rebuild by deleting all checksum files
- `--no-validate`: Skip CloudFormation template validation
- `--lint / --no-lint`: Enable/disable ruff linting and cfn-lint (default: enabled)
- `--verbose`, `-v`: Enable verbose build output

**Prerequisites** (same as `--from-code` deployments):
- AWS SAM CLI, Docker (for container-image Lambdas), Node.js ≥ 22.12, npm ≥ 10, Python 3.12.

**Examples:**

```bash
# Standard build and publish (UI + backend)
idp-cli publish --source-dir . --region us-east-1

# With custom bucket and prefix
idp-cli publish \
    --source-dir . \
    --region us-east-1 \
    --bucket-basename my-idp-artifacts \
    --prefix v1

# Build BOTH standard and headless templates
idp-cli publish --source-dir . --region us-east-1 --headless

# Build a headless template for GovCloud (GovCloud-specific config applied automatically)
idp-cli publish --source-dir . --region us-gov-west-1 --headless

# Full rebuild with verbose output
idp-cli publish --source-dir . --region us-east-1 --clean-build --verbose

# Make artifacts publicly readable (shared template)
idp-cli publish --source-dir . --region us-east-1 --public
```

**Output:**

On success, `publish` prints:

```
📦 Template URL (for updating existing stack):
  https://s3.us-east-1.amazonaws.com/<bucket>/<prefix>/idp-main.yaml

🚀 1-Click Launch (creates new stack):
  https://us-east-1.console.aws.amazon.com/cloudformation/home?...

🔧 Headless Template URL:                    # only with --headless
  https://s3.us-east-1.amazonaws.com/<bucket>/<prefix>/idp-headless.yaml

🚀 Headless 1-Click Launch:                  # only with --headless
  https://us-east-1.console.aws.amazon.com/cloudformation/home?...
```

**Relationship to `deploy --from-code`:**

`deploy --from-code .` internally runs the same build + publish pipeline and then creates/updates the CloudFormation stack. Use `publish` when you want to decouple the build from the deployment step or share the template with other accounts/regions.

> **Legacy**: The standalone `publish.py` script and `scripts/generate_govcloud_template.py` are deprecated. Use `idp-cli publish` (with or without `--headless`) instead.

---

### `delete`

Delete an IDP CloudFormation stack.

**⚠️ WARNING:** This permanently deletes all stack resources.

**Usage:**
```bash
idp-cli delete [OPTIONS]
```

**Options:**
- `--stack-name` (required): CloudFormation stack name
- `--force`: Skip confirmation prompt
- `--empty-buckets`: Empty S3 buckets before deletion (required if buckets contain data)
- `--force-delete-all`: Force delete ALL remaining resources after CloudFormation deletion (S3 buckets, CloudWatch logs, DynamoDB tables)
- `--wait`: Wait for deletion to complete (default: no-wait)
- `--region`: AWS region (optional)

**S3 Bucket Behavior:**
- **LoggingBucket**: `DeletionPolicy: Retain` - Always kept (unless using `--force-delete-all`)
- **All other buckets**: `DeletionPolicy: RetainExceptOnCreate` - Deleted if empty
- CloudFormation can ONLY delete S3 buckets if they're empty
- Use `--empty-buckets` to automatically empty buckets before deletion
- Use `--force-delete-all` to delete ALL remaining resources after CloudFormation completes

**Force Delete All Behavior:**

The `--force-delete-all` flag performs a comprehensive cleanup AFTER CloudFormation deletion completes:

1. **CloudFormation Deletion Phase**: Standard stack deletion
2. **Additional Resource Cleanup Phase** (happens with `--wait` on all deletions and always with `--force-delete-all`): Removes stack-specific resources not tracked by CloudFormation:
   - CloudWatch Log Groups (Lambda functions, Glue crawlers)
   - AppSync APIs and their log groups (only ever present in stacks created before AppSync was removed; current stacks create none)
   - CloudFront distributions (two-phase cleanup - initiates disable, takes 15-20 minutes to propagate globally)
   - CloudFront Response Headers Policies (from previously deleted stacks)
   - IAM custom policies and permissions boundaries
   - CloudWatch Logs resource policies
3. **Retained Resource Cleanup Phase** (only with `--force-delete-all`): Deletes remaining resources in order:
   - DynamoDB tables (disables PITR, then deletes)
   - CloudWatch Log Groups (matching stack name pattern)
   - S3 buckets (regular buckets first, LoggingBucket last)

⚠️ **A CloudFormation deletion that failed exits 1 even under `--force-delete-all`.** The
cleanup phase still runs — that is what the flag is for — and the non-zero exit comes
after it, so you get both. Before this, `--force-delete-all` printed "Stack deletion
failed!" and exited 0, so a CI teardown job could not tell a stack that failed to delete
from one that deleted cleanly.

**Resources Always Cleaned Up (with `--wait` or `--force-delete-all`):**
- IAM custom policies (containing stack name)
- IAM permissions boundary policies
- CloudFront response header policies (custom)
- CloudWatch Logs resource policies (stack-specific)
- AppSync log groups (pre-migration stacks only)
- Additional log groups containing stack name
- Gracefully handles missing/already-deleted resources

**Resources Deleted Only by --force-delete-all:**
- All DynamoDB tables from stack
- All CloudWatch Log Groups (retained by CloudFormation)
- All S3 buckets including LoggingBucket
- Handles nested stack resources automatically

**Examples:**

```bash
# Interactive deletion with confirmation
idp-cli delete --stack-name test-stack

# Automated deletion (CI/CD)
idp-cli delete --stack-name test-stack --force

# Delete with automatic bucket emptying
idp-cli delete --stack-name test-stack --empty-buckets --force

# Force delete ALL remaining resources (comprehensive cleanup)
idp-cli delete --stack-name test-stack --force-delete-all --force

# Delete without waiting
idp-cli delete --stack-name test-stack --force --no-wait
```

**What you'll see (standard deletion):**
```
⚠️  WARNING: Stack Deletion
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Stack: test-stack
Region: us-east-1

S3 Buckets:
  • InputBucket: 20 objects (45.3 MB)
  • OutputBucket: 20 objects (123.7 MB)
  • WorkingBucket: empty

⚠️  Buckets contain data!
This action cannot be undone.

Are you sure you want to delete this stack? [y/N]: _
```

**What you'll see (force-delete-all):**
```
⚠️  WARNING: FORCE DELETE ALL RESOURCES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Stack: test-stack
Region: us-east-1

S3 Buckets:
  • InputBucket: 20 objects (45.3 MB)
  • OutputBucket: 20 objects (123.7 MB)
  • LoggingBucket: 5000 objects (2.3 GB)

⚠️  FORCE DELETE ALL will remove:
  • All S3 buckets (including LoggingBucket)
  • All CloudWatch Log Groups
  • All DynamoDB Tables
  • Any other retained resources

This happens AFTER CloudFormation deletion completes

This action cannot be undone.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Are you ABSOLUTELY sure you want to force delete ALL resources? [y/N]: y

Deleting CloudFormation stack...
✓ Stack deleted successfully!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Starting force cleanup of retained resources...
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Analyzing retained resources...
Found 4 retained resources:
  • DynamoDB Tables: 0
  • CloudWatch Logs: 0
  • S3 Buckets: 3

⠋ Deleting S3 buckets... 3/3

✓ Cleanup phase complete!

Resources deleted:
  • S3 Buckets: 3
    - test-stack-inputbucket-abc123
    - test-stack-outputbucket-def456
    - test-stack-loggingbucket-ghi789

Stack 'test-stack' and all resources completely removed.
```

**Use Cases:**
- Cleanup test/development environments to avoid charges
- CI/CD pipelines that provision and teardown stacks
- Automated testing with temporary stack creation
- Complete removal of failed stacks with retained resources
- Cleanup of stacks with LoggingBucket and CloudWatch logs

**Important Notes:**
- `--force-delete-all` automatically includes `--empty-buckets` behavior
- Cleanup phase runs even if CloudFormation deletion fails
- Includes resources from nested stacks automatically
- Safe to run - only deletes resources that weren't deleted by CloudFormation
- Progress bars show real-time deletion status

**Auto-Monitoring for In-Progress Deletions:**

If you run `delete` on a stack that already has a DELETE operation in progress, the command automatically switches to monitoring mode instead of failing. This is useful if you started a deletion without `--wait` - simply run the command again to monitor:

```bash
# First run without --wait starts the deletion
$ idp-cli delete --stack-name test-stack --force --no-wait
✓ Stack DELETE initiated successfully!

# Second run - automatically monitors the in-progress deletion
$ idp-cli delete --stack-name test-stack
Stack 'test-stack' is already being deleted
Current status: DELETE_IN_PROGRESS

Switching to monitoring mode...

[Live progress display...]

✓ Stack deleted successfully!
```

**Canceling In-Progress Operations:**

If a non-delete operation is in progress (CREATE, UPDATE), the delete command offers options to handle it:

```bash
$ idp-cli delete --stack-name test-stack
Stack 'test-stack' has an operation in progress: CREATE_IN_PROGRESS

Options:
  1. Wait for CREATE to complete first
  2. Cancel the CREATE and proceed with deletion

Do you want to cancel the CREATE and delete the stack? [yes/no/wait]: _
```

- **yes**: Cancel the operation (if possible) and proceed with deletion
- **no**: Exit without making changes
- **wait**: Wait for the current operation to complete, then delete

With `--force` flag, the command automatically cancels the operation and proceeds with deletion:

```bash
# Force mode - automatically cancels and deletes
$ idp-cli delete --stack-name test-stack --force
Force mode: Canceling operation and proceeding with deletion...

✓ Stack reached stable state: ROLLBACK_COMPLETE

Proceeding with stack deletion...
```

**Note:** CREATE operations cannot be cancelled directly - they must complete or roll back naturally. UPDATE operations can be cancelled immediately.

---

### `bootstrap`

Bootstrap a configuration (and optional synthetic test set) from a plain-language
description — the scriptable equivalent of the web UI [Quick Start](./quick-start.md)
widget. Authors a document-class schema from your prompt (reusing a catalog match
when one fits), creates a config profile, and — when the document generator is
available — generates a small labeled synthetic test set attached to it.

**Usage:**
```bash
idp-cli bootstrap --prompt "<description>" [--stack-name <stack>] [OPTIONS]
```

**Example:**
```bash
idp-cli bootstrap \
    --prompt "Invoices with vendor name, invoice number, date, and total amount" \
    --stack-name my-idp-stack
```

**Local mode** — omit `--stack-name` to author and print the schema as JSON without
saving anything to a stack:
```bash
idp-cli bootstrap --prompt "Bank statements with account holder and transactions"
```

**Options:**
- `--prompt`, `-p`: **(required)** Natural-language description of the document type.
- `--stack-name`: Target CloudFormation stack. **Omit for local mode** (print schema, no save).
- `--class-name`: Document class name to use as the schema `$id` / document type.
- `--field-hint`: A field the schema must include. Repeatable: `--field-hint X --field-hint Y`.
- `--config-profile` (alias: `--config-version`): Existing configuration profile to source catalog classes from / merge the new class into.
- `--target-profile` (alias: `--target-version`): Name of the configuration profile to create (default: `bootstrap-<class>`).
- `--count`, `-c`: Number of synthetic documents to generate (default: `3`).
- `--threshold`: Generation quality threshold, 1–10 (default: `7`).
- `--augment`: Apply scan/fax-style image augmentation to generated documents.
- `--model-id`: Bedrock model id override for schema authoring / generation.
- `--region`: AWS region (optional).

**Note:** The created profile is **not** activated automatically (unlike the web UI
Quick Start). Activate it from **Configuration › View/Edit Configuration** in the UI
when you're ready to process documents with it. Synthetic generation is optional and
requires the Test Set Generator extension (deployed stack) or
`pip install -e "lib/idp_common_pkg[synthesis]"` (local); without it, the config is still
created and you can upload your own documents to build a test set. See the
[Quick Start guide](./quick-start.md) for the full workflow.

---

### `process` / `run-inference`

Process a batch of documents.

**Usage:**
```bash
idp-cli process [OPTIONS]
# or (deprecated alias)
idp-cli run-inference [OPTIONS]
```

**Document Source (choose ONE):**
- `--manifest`: Path to manifest file (CSV or JSON)
- `--dir`: Local directory containing documents
- `--s3-uri`: S3 URI in InputBucket
- `--test-set`: Test set ID from test set bucket

**Options:**
- `--stack-name` (required): CloudFormation stack name
- `--batch-id`: Custom batch ID (auto-generated if omitted, ignored with --test-set)
- `--batch-prefix`: Prefix for auto-generated batch ID (default: `cli-batch`)
- `--file-pattern`: File pattern for directory/S3 scanning (default: `*.pdf`)
- `--recursive/--no-recursive`: Include subdirectories (default: recursive)
- `--number-of-files`: Limit number of files to process
- `--config`: **Refused.** A configuration file is not applied to a batch submission, so passing one exits non-zero rather than running under the stack's existing configuration. Upload the file as a profile with [`config-upload`](#config-upload), then pass `--config-profile`.
- `--config-profile` (alias: `--config-version`): Configuration profile to use for processing (e.g., v1, v2)
- `--context`: Context description for test run (used with --test-set, e.g., "Model v2.1", "Production validation")
- `--monitor`: Monitor progress until completion
- `--refresh-interval`: Seconds between status checks (default: 5)
- `--region`: AWS region (optional)

**Test Set Integration:**
For test runs to appear properly in the Test Studio UI, use either:
- `--test-set`: Process test set directly by ID (recommended for test sets)
- `--manifest`: Use manifest file with populated baseline_source column for evaluation tracking

Other options (`--dir`, `--s3-uri`) are for general document processing but won't integrate with test studio tracking.

**Examples:**

```bash
# Process from local directory
idp-cli process \
    --stack-name my-stack \
    --dir ./documents/ \
    --monitor

# Process from manifest with baselines (enables evaluation)
idp-cli process \
    --stack-name my-stack \
    --manifest documents-with-baselines.csv \
    --monitor

# Process from manifest with limited files
idp-cli process \
    --stack-name my-stack \
    --manifest documents-with-baselines.csv \
    --number-of-files 10 \
    --monitor

# Process test set (integrates with Test Studio UI - use test set ID)
idp-cli process \
    --stack-name my-stack \
    --test-set fcc-example-test \
    --monitor

# Process test set with limited files for quick testing
idp-cli process \
    --stack-name my-stack \
    --test-set fcc-example-test \
    --number-of-files 5 \
    --monitor

# Process test set with custom context (for tracking in Test Studio)
idp-cli process \
    --stack-name my-stack \
    --test-set fcc-example-test \
    --context "Model v2.1 - improved prompts" \
    --monitor

# Process S3 URI
idp-cli process \
    --stack-name my-stack \
    --s3-uri archive/2024/ \
    --monitor

# Process with specific configuration profile
idp-cli process \
    --stack-name my-stack \
    --dir ./documents/ \
    --config-profile v2 \
    --monitor

# Process test set with configuration profile
idp-cli process \
    --stack-name my-stack \
    --test-set fcc-example-test \
    --config-profile v1 \
    --context "Testing with config v1" \
    --monitor
```

---

### `reprocess` / `rerun-inference`

Reprocess existing documents from a specific pipeline step.

**Usage:**
```bash
idp-cli reprocess [OPTIONS]
# or (deprecated alias)
idp-cli rerun-inference [OPTIONS]
```

**Use Cases:**
- Test different classification or extraction configurations without re-running OCR
- Fix classification errors and reprocess extraction
- Iterate on prompt engineering rapidly

**Options:**
- `--stack-name` (required): CloudFormation stack name
- `--step` (required): Pipeline step to rerun from (`classification` or `extraction`)
- **Document Source** (choose ONE):
  - `--document-ids`: Comma-separated document IDs
  - `--batch-id`: Batch ID to get all documents from
- `--force`: Skip confirmation prompt (useful for automation)
- `--monitor`: Monitor progress until completion
- `--refresh-interval`: Seconds between status checks (default: 5)
- `--region`: AWS region (optional)

**Step Behavior:**
- `classification`: Clears page classifications and sections, reruns classification → extraction → assessment
- `extraction`: Keeps classifications, clears extraction data, reruns extraction → assessment

**Examples:**

```bash
# Rerun classification for specific documents
idp-cli reprocess \
    --stack-name my-stack \
    --step classification \
    --document-ids "batch-123/doc1.pdf,batch-123/doc2.pdf" \
    --monitor

# Rerun extraction for entire batch
idp-cli reprocess \
    --stack-name my-stack \
    --step extraction \
    --batch-id cli-batch-20251015-143000 \
    --monitor

# Automated rerun (skip confirmation - perfect for CI/CD)
idp-cli reprocess \
    --stack-name my-stack \
    --step classification \
    --batch-id test-set \
    --force \
    --monitor
```

**What Gets Cleared:**

| Step | Clears | Keeps |
|------|--------|-------|
| `classification` | Page classifications, sections, extraction results | OCR data (pages, images, text) |
| `extraction` | Section extraction results, attributes | OCR data, page classifications, section structure |

**Benefits:**
- Leverages existing OCR data (saves time and cost)
- Rapid iteration on classification/extraction configurations
- Perfect for prompt engineering experiments

**Demo:**

https://github.com/user-attachments/assets/28deadbb-378b-42b7-a5e2-f929af9b0e41


---

### `status`

Check status of documents by batch ID, document ID, or search criteria.

**Usage:**
```bash
idp-cli status [OPTIONS]
```

**Document Source (choose ONE):**
- `--batch-id`: Batch identifier or PK substring to search for (searches tracking table)
- `--document-id`: Single document ID (check individual document)

**Optional Filters and Display:**
- `--object-status`: Filter by status (COMPLETED, FAILED, QUEUED, RUNNING, PROCESSING)
- `--show-details`: Show detailed document information in table format
- `--get-time`: Calculate and display timing statistics (processing time, queue time, total time)
- `--include-metering`: Include Lambda metering statistics (GB-seconds by stage) - requires `--get-time`

**Other Options:**
- `--stack-name` (required): CloudFormation stack name
- `--wait`: Wait for all documents to complete
- `--refresh-interval`: Seconds between status checks (default: 5)
- `--format`: Output format - `table` (default) or `json`
- `--region`: AWS region (optional)

**How --batch-id Works:**

The `--batch-id` option performs a PK substring search in the DynamoDB tracking table. This means:
- It searches for all documents where the PK (Primary Key) contains your search string
- You can search for exact batch IDs: `cli-batch-20251015-143000`
- You can search for partial matches: `batch-123` finds all documents with "batch-123" in their path
- You can search across multiple batches: `invoice` finds all documents with "invoice" in their name

**Examples:**

```bash
# Search for all documents in a batch (PK substring search)
idp-cli status \
    --stack-name my-stack \
    --batch-id cli-batch-20251015-143000

# Search for documents across batches with partial match
idp-cli status \
    --stack-name my-stack \
    --batch-id batch-123

# Search for completed documents only
idp-cli status \
    --stack-name my-stack \
    --batch-id batch-123 \
    --object-status COMPLETED

# Search for failed documents with details
idp-cli status \
    --stack-name my-stack \
    --batch-id batch-123 \
    --object-status FAILED \
    --show-details

# Search with timing statistics
idp-cli status \
    --stack-name my-stack \
    --batch-id batch-123 \
    --object-status COMPLETED \
    --get-time

# Search with timing and Lambda metering data
idp-cli status \
    --stack-name my-stack \
    --batch-id test \
    --object-status COMPLETED \
    --get-time \
    --include-metering

# Check single document status
idp-cli status \
    --stack-name my-stack \
    --document-id batch-123/invoice.pdf

# Monitor documents until completion
idp-cli status \
    --stack-name my-stack \
    --batch-id batch-123 \
    --wait

# Get JSON output for scripting
idp-cli status \
    --stack-name my-stack \
    --batch-id batch-123 \
    --format json
```

**Timing Statistics:**

When using `--get-time`, the command calculates:
- **Processing Time**: WorkflowStartTime → CompletionTime (actual processing duration)
- **Queue Time**: QueuedTime → WorkflowStartTime (time waiting in queue)
- **Total Time**: QueuedTime → CompletionTime (end-to-end duration)

For each metric, you'll see:
- Average, Median, Min, Max, Standard Deviation, Total
- ObjectKey for min/max values (helps identify outliers)

**Lambda Metering:**

When using `--include-metering` with `--get-time`, you'll see GB-seconds usage by stage:
- Assessment, OCR, Classification, Extraction, Summarization
- Statistics: Average, Median, Min, Max, Std Dev, Total
- Cost estimates based on AWS Lambda pricing ($0.0000166667 per GB-second)

**Example Output with Timing:**

```bash
$ idp-cli status --stack-name my-stack --batch-id test-batch --object-status COMPLETED --get-time

Searching for documents with PK containing 'test-batch'...
✓ Found 25 matching documents

Timing Statistics:
  Valid documents: 25

Processing Time (WorkflowStartTime → CompletionTime):
┏━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Metric   ┃ Value       ┃ ObjectKey                    ┃
┡━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ Average  │ 45.23s      │                              │
│ Median   │ 43.10s      │                              │
│ Minimum  │ 32.45s      │ test-batch/small-doc.pdf     │
│ Maximum  │ 78.90s      │ test-batch/large-doc.pdf     │
│ Std Dev  │ 12.34s      │                              │
│ Total    │ 18m 50.75s  │                              │
└──────────┴─────────────┴──────────────────────────────┘
```

**Programmatic Use:**

The command returns exit codes for scripting:
- `0` - Document(s) completed successfully
- `1` - Document(s) failed
- `2` - Document(s) still processing, or the outcome could not be established

⚠️ **`--wait` answers the same way as the polled form, and did not always.** Both
now derive the code from the same place, so a batch that finished with failures exits
`1` whether you polled it or waited on it. Before this change `--wait` exited `0` on
that batch while the poll exited `1`, which meant `idp-cli status --wait && deploy`
proceeded after a batch in which every document failed. If you have a script that
relied on `--wait` always exiting `0`, it will now stop on a failed batch — that is
the intended behaviour, but it is a change.

`--wait` also exits `2` when the watch ended without a verdict: a monitoring error, or
Ctrl-C. Nothing about the batch was measured on those paths, so `0` would assert a
success and `1` would report failures that may not exist.

`process --monitor` and `rerun --monitor` deliberately still exit `0` regardless of
what the monitored batch did. Their work is the submission, which succeeded; exiting
non-zero because 1 of 100 documents failed would stop `process --monitor &&
download-results` from collecting the 99 that worked. Ask for the batch's verdict with
`idp-cli status --batch-id <id>`, which answers exactly that.

**JSON Output Format:**

```bash
# Single document
$ idp-cli status --stack-name my-stack --document-id batch-123/invoice.pdf --format json
{
  "document_id": "batch-123/invoice.pdf",
  "status": "COMPLETED",
  "duration": 125.4,
  "start_time": "2025-01-01T10:30:45Z",
  "end_time": "2025-01-01T10:32:50Z",
  "num_sections": 2,
  "exit_code": 0
}

# Table output includes final status summary
$ idp-cli status --stack-name my-stack --document-id batch-123/invoice.pdf
[status table]

FINAL STATUS: COMPLETED | Duration: 125.4s | Exit Code: 0
```

**Scripting Examples:**

```bash
#!/bin/bash
# Wait for document completion and check result
idp-cli status --stack-name prod --document-id batch-001/invoice.pdf --wait
exit_code=$?

if [ $exit_code -eq 0 ]; then
  echo "Document processed successfully"
  # Proceed with downstream processing
else
  echo "Document processing failed"
  exit 1
fi
```

```bash
#!/bin/bash
# Poll document status in script
while true; do
  status=$(idp-cli status --stack-name prod --document-id batch-001/invoice.pdf --format json)
  state=$(echo "$status" | jq -r '.status')
  
  if [ "$state" = "COMPLETED" ]; then
    echo "Processing complete!"
    break
  elif [ "$state" = "FAILED" ]; then
    echo "Processing failed!"
    exit 1
  fi
  
  sleep 5
done
```

---

### `download-results`

Download processing results to local directory.

**Usage:**
```bash
idp-cli download-results [OPTIONS]
```

**Options:**
- `--stack-name` (required): CloudFormation stack name
- `--batch-id`: Batch identifier (mutually exclusive with `--document-id`/`--run-id`)
- `--document-id`: Document object key — required with `--run-id` to download a specific [document version](document-versions.md)
- `--run-id`: Version run id (from `idp-cli list-versions`). Downloads the exact pinned S3 bytes of that processing run. Requires `--document-id`.
- `--output-dir` (required): Local directory to download to
- `--file-types`: File types to download (default: `all`)
  - Options: `pages`, `sections`, `summary`, `evaluation`, or `all`
- `--region`: AWS region (optional)

**Examples:**

```bash
# Download all results
idp-cli download-results \
    --stack-name my-stack \
    --batch-id cli-batch-20251015-143000 \
    --output-dir ./results/

# Download only extraction results
idp-cli download-results \
    --stack-name my-stack \
    --batch-id cli-batch-20251015-143000 \
    --output-dir ./results/ \
    --file-types sections

# Download evaluation results only
idp-cli download-results \
    --stack-name my-stack \
    --batch-id eval-batch-20251015 \
    --output-dir ./eval-results/ \
    --file-types evaluation

# Download a specific document VERSION (exact bytes of one processing run)
idp-cli download-results \
    --stack-name my-stack \
    --document-id loan-12345/package.pdf \
    --run-id 20250707T141530Z-exec-abc \
    --output-dir ./results/
```

**Output Structure:**

```
./results/
└── cli-batch-20251015-143000/
    └── invoice.pdf/
        ├── pages/
        │   └── 1/
        │       ├── image.jpg
        │       ├── rawText.json
        │       └── result.json
        ├── sections/
        │   └── 1/
        │       ├── result.json          # Extracted structured data
        │       └── summary.json
        ├── summary/
        │   ├── fulltext.txt
        │   └── summary.json
        └── evaluation/                  # Only present if baseline provided
            ├── report.json              # Detailed metrics
            └── report.md                # Human-readable report
```

---

### `use-as-baseline`

Promote a processed document's output to the evaluation baseline — the
scriptable equivalent of the web UI's **Use as Evaluation Baseline** button.
Copies every output object for the document into the evaluation baseline bucket
and sets the document's `EvaluationStatus` to `BASELINE_AVAILABLE`. Runs
synchronously (returns once the copy is complete).

Use this to capture a manually validated result as the "ground truth" that
future re-runs of the same document are evaluated against.

**Usage:**
```bash
idp-cli use-as-baseline [OPTIONS]
```

**Options:**
- `--stack-name` (required): CloudFormation stack name
- `--document-id` (required): Document object key (S3 key) of a processed document, e.g. `loan-12345/package.pdf`
- `--region`: AWS region (optional)

**Example:**

```bash
idp-cli use-as-baseline \
    --stack-name my-stack \
    --document-id loan-12345/package.pdf
```

The document must have finished processing (its output prefix must exist); the
caller's IAM credentials need read on the output bucket and write on the
evaluation baseline bucket and tracking table.

---

### `list-versions`

List the retained processing-run [versions](document-versions.md) of a document, newest first. Each successful run of a document is retained as a version whose output bytes are pinned by S3 object version; use a version's `Run ID` with `download-results --run-id` to fetch that exact version.

**Usage:**
```bash
idp-cli list-versions [OPTIONS]
```

**Options:**
- `--stack-name` (required): CloudFormation stack name
- `--document-id` (required): Document object key (its tracking id)
- `--region`: AWS region (optional)

**Example:**

```bash
idp-cli list-versions \
    --stack-name my-stack \
    --document-id loan-12345/package.pdf
```

Output is a table of `Run ID`, `Completed`, `Config Profile`, `Pages`, and `Files`. See the [Document Versions guide](document-versions.md) for how versioning works and the Web UI / API surfaces.

---

### `delete-documents`

Delete documents and all associated data from the IDP system.

**⚠️ WARNING:** This action cannot be undone.

**Usage:**
```bash
idp-cli delete-documents [OPTIONS]
```

**Document Selection (choose ONE):**
- `--document-ids`: Comma-separated list of document IDs (S3 object keys) to delete
- `--batch-id`: Delete all documents in this batch
- `--pattern`: Wildcard pattern to match document keys (e.g. `"batch-123/*.pdf"`, `"*invoice*"`)

**Options:**
- `--stack-name` (required): CloudFormation stack name
- `--status-filter`: Only delete documents with this status (use with --batch-id or --pattern)
  - Options: `FAILED`, `COMPLETED`, `PROCESSING`, `QUEUED`
- `--dry-run`: Show what would be deleted without actually deleting
- `--force`, `-y`: Skip confirmation prompt
- `--region`: AWS region (optional)

**What Gets Deleted:**
- Source files from input bucket
- Processed outputs from output bucket
- DynamoDB tracking records
- List entries in tracking table

**Exit codes:** `No documents found for batch …` with exit 0 means the selector matched
nothing, and that is all it means. A failure while finding the documents — a throttled or
rejected table scan, a table that is not there — prints the cause and exits 1 instead of
reporting that there was nothing to delete.

A run in which **every** deletion failed also exits 1; it used to exit 0 after printing
"Deleted 0/2 document(s)", so an automated cleanup step reported success having deleted
nothing.

⚠️ A **partial** failure still exits 0. Read the per-document "Failed deletions:" list
rather than the exit code when some documents may have survived.

**Examples:**

```bash
# Delete specific documents by ID
idp-cli delete-documents \
    --stack-name my-stack \
    --document-ids "batch-123/doc1.pdf,batch-123/doc2.pdf"

# Delete all documents in a batch
idp-cli delete-documents \
    --stack-name my-stack \
    --batch-id cli-batch-20250123

# Delete only failed documents in a batch
idp-cli delete-documents \
    --stack-name my-stack \
    --batch-id cli-batch-20250123 \
    --status-filter FAILED

# Dry run to see what would be deleted
idp-cli delete-documents \
    --stack-name my-stack \
    --batch-id cli-batch-20250123 \
    --dry-run

# Delete documents matching a wildcard pattern
idp-cli delete-documents \
    --stack-name my-stack \
    --pattern "batch-123/*.pdf"

# Delete all failed invoice documents across batches
idp-cli delete-documents \
    --stack-name my-stack \
    --pattern "*invoice*" \
    --status-filter FAILED

# Dry run with pattern to preview matches
idp-cli delete-documents \
    --stack-name my-stack \
    --pattern "*2024*" \
    --dry-run

# Force delete without confirmation
idp-cli delete-documents \
    --stack-name my-stack \
    --document-ids "batch-123/doc1.pdf" \
    --force
```

**Output Example:**
```
Connecting to stack: my-stack
Getting documents for batch: cli-batch-20250123
Found 15 document(s) in batch
  (filtered by status: FAILED)

⚠️  Documents to be deleted:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  • cli-batch-20250123/doc1.pdf
  • cli-batch-20250123/doc2.pdf
  • cli-batch-20250123/doc3.pdf
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Delete 3 document(s) permanently? [y/N]: y

✓ Successfully deleted 3 document(s)
```

**Use Cases:**
- Clean up failed documents after fixing issues
- Remove test documents from a batch
- Free up storage by removing old processed documents
- Prepare for reprocessing by removing previous results

---

### `generate-manifest`

Generate a manifest file from directory or S3 URI, or create a test set in the test set bucket.

**Usage:**
```bash
idp-cli generate-manifest [OPTIONS]
```

**Options:**
- **Source** (choose ONE):
  - `--dir`: Local directory to scan
  - `--s3-uri`: S3 URI to scan
- `--baseline-dir`: Baseline directory for automatic matching (only with --dir)
- `--output`: Output manifest file path (CSV) - optional when using --test-set
- `--file-pattern`: File pattern (default: `*.pdf`)
- `--case-sensitive/--no-case-sensitive`: Match `--file-pattern` exactly as written (default: `--no-case-sensitive`)
- `--recursive/--no-recursive`: Include subdirectories (default: recursive)
- `--region`: AWS region (optional)
- **Test Set Creation:**
  - `--test-set`: Test set name - creates folder in test set bucket and uploads files
  - `--stack-name`: CloudFormation stack name (required with --test-set)
  - `--force` / `-y`: Overwrite an existing test set without the confirmation prompt

**Examples:**

```bash
# Generate from directory
idp-cli generate-manifest \
    --dir ./documents/ \
    --output manifest.csv

# Generate with automatic baseline matching
idp-cli generate-manifest \
    --dir ./documents/ \
    --baseline-dir ./validated-baselines/ \
    --output manifest-with-baselines.csv

# Create test set and upload files (no manifest needed - use test set name)
idp-cli generate-manifest \
    --dir ./documents/ \
    --baseline-dir ./baselines/ \
    --test-set "fcc example test" \
    --stack-name IDP

# Create test set with manifest output
idp-cli generate-manifest \
    --dir ./documents/ \
    --baseline-dir ./baselines/ \
    --test-set "fcc example test" \
    --stack-name IDP \
    --output test-manifest.csv

# Refresh an existing test set from a script or CI job (asks nothing)
idp-cli generate-manifest \
    --dir ./documents/ \
    --baseline-dir ./baselines/ \
    --test-set "fcc example test" \
    --stack-name IDP \
    --force
```

**How `--file-pattern` selects documents:** the pattern is matched against each
file's **base name**, on both the `--dir` and the `--s3-uri` path, and the match
**ignores case**. So the default `*.pdf` selects `statement.PDF` and `Statement.Pdf`
as well, which is the point — a corpus exported from a system that uppercases
extensions used to produce a valid-looking manifest with no rows in it, at exit 0.
Case folding covers the whole pattern rather than an extension picked out of it, so
`Invoice*.pdf` also selects `INVOICE01.PDF`. Pass `--case-sensitive` for a pattern
whose case is deliberate — distinguishing an `Invoice-*.pdf` family from an
`invoice-*.pdf` one, say. A pattern containing a directory component
(`--file-pattern "sub/*.pdf"`) is **refused**: point `--dir` or `--s3-uri` at the
directory and use `--recursive` / `--no-recursive` to choose the depth.

On the `--dir` path, hidden files follow the usual shell rule and are excluded unless
the pattern itself starts with a dot. The `--s3-uri` path has **no** such rule — it
filters keys by base name only — so `--file-pattern "*"` against a test-set prefix
selects the `.uploading` marker object as though it were a document. Name the extension
you want rather than relying on `*` when scanning a bucket.

⚠️ `--file-pattern` on `process` and `run-inference` is a **different** scan, in
`idp_sdk`, and it is still case-sensitive. Pass the extension's actual case there, or
generate a manifest with this command and process that.

**How `--baseline-dir` is matched:** a baseline sub-directory must be named after the
document file it labels, **extension included** (`invoice.pdf/`, not `invoice/`). The
match uses the same rule as `--file-pattern`, so it ignores case unless
`--case-sensitive` is given, and `W2-A.PDF` is therefore labelled by `w2-a.pdf/`. Two
baseline directories differing only in case are **refused** as ambiguous. A baseline
directory matching no document is named and skipped rather than uploaded; if **no**
document matched a baseline, `--test-set` refuses before anything is cleared or
uploaded, and a manifest-only run warns and leaves `baseline_source` empty for you to
fill in.

**Test Set Creation:**
When using `--test-set`, the command:
1. Requires `--stack-name`, `--baseline-dir`, and `--dir`
2. Uploads input files to `s3://test-set-bucket/{test-set-id}/input/`
3. Uploads baseline files to `s3://test-set-bucket/{test-set-id}/baseline/`
4. Creates proper test set structure for evaluation workflows
5. Test set will be auto-detected by the Test Studio UI

**If the upload fails partway through,** the `.uploading` marker object the command
places under the test set's prefix is removed before it exits. That marker is what
stops the Test Studio resolver registering a folder that is still being filled, so a
marker left behind makes a folder invisible to the backend — and re-running the upload
does not clear it, because the new run writes it again. The command exits non-zero and
names what went wrong.

Be precise about what that leaves behind, because it is not nothing: the objects
uploaded before the failure **stay**, and with the marker gone the resolver may
register them as a partial, unlabeled set (a set with documents and no baselines is a
legitimate shape, so the backend cannot tell the two apart). That is deliberate —
re-running clears the prefix first, so the state is recoverable, whereas a surviving
marker made the folder invisible *permanently*. If you do not want the partial set
visible, delete the prefix before retrying.

In the one case where the marker itself cannot be deleted (an IAM policy with
`s3:PutObject` but not `s3:DeleteObject` on the test set bucket) the command **fails**
rather than reporting success, and the error names the S3 object to delete by hand.

**Overwriting an existing test set:** if the test set name already exists, everything
under its prefix — including the baselines a previous evaluation was scored against —
is deleted before the new files are uploaded, so the command asks for confirmation
first. Answer `y` to proceed, anything else to abort. Run non-interactively (a CI job,
a `make` target, stdin from `/dev/null`) there is no answer to read, and the command
**aborts with exit 1 and changes nothing**; pass `--force` to overwrite without the
prompt. Baselines cleared this way are not recoverable from the CLI.

Process the created test set:
```bash
# Using test set ID (from UI or after creation)
idp-cli process --stack-name IDP --test-set fcc-example-test --monitor

# Or using S3 URI to process input files directly
idp-cli run-inference --stack-name IDP --s3-uri s3://test-set-bucket/fcc-example-test/input/

# Or using manifest if generated
idp-cli run-inference --stack-name IDP --manifest test-manifest.csv
```

---

### `validate-manifest`

Validate a manifest file without processing.

**Usage:**
```bash
idp-cli validate-manifest [OPTIONS]
```

**Options:**
- `--manifest` (required): Path to manifest file to validate (CSV or JSON)

**Examples:**

```bash
# Validate a CSV manifest
idp-cli validate-manifest --manifest documents.csv

# Validate a JSON manifest
idp-cli validate-manifest --manifest documents.json
```

---

### `list-batches`

List recent batch processing jobs.

**Usage:**
```bash
idp-cli list-batches [OPTIONS]
```

**Options:**
- `--stack-name` (required): CloudFormation stack name
- `--limit`: Maximum number of batches to list (default: 10)
- `--region`: AWS region (optional)

**Examples:**

```bash
# List last 10 batches (default)
idp-cli list-batches --stack-name my-stack

# List last 5 batches
idp-cli list-batches --stack-name my-stack --limit 5

# List with specific region
idp-cli list-batches --stack-name my-stack --limit 20 --region us-west-2
```

---

## Complete Evaluation Workflow

This workflow demonstrates how to process documents, manually validate results, and then reprocess with evaluation to measure accuracy.

### Step 1: Deploy Your Stack

Deploy an IDP stack if you haven't already:

```bash
idp-cli deploy \
    --stack-name eval-testing \
    --admin-email your.email@example.com \
    --max-concurrent 50 \
    --wait
```

**What happens:** CloudFormation creates ~120 resources including S3 buckets, Lambda functions, Step Functions, and DynamoDB tables. This takes 10-15 minutes.

---

### Step 2: Initial Processing from Local Directory

Process your test documents to generate initial extraction results:

```bash
# Prepare test documents
mkdir -p ~/test-documents
cp /path/to/your/invoice.pdf ~/test-documents/
cp /path/to/your/w2.pdf ~/test-documents/
cp /path/to/your/paystub.pdf ~/test-documents/

# Process documents
idp-cli run-inference \
    --stack-name eval-testing \
    --dir ~/test-documents/ \
    --batch-id initial-run \
    --monitor
```

**What happens:** Documents are uploaded to S3, processed through OCR, classification, extraction, assessment, and summarization. Results are stored in OutputBucket.

**Monitor output:**
```
✓ Uploaded 3 documents to InputBucket
✓ Sent 3 messages to processing queue

Monitoring Batch: initial-run
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 Status Summary
 ─────────────────────────────────────
 ✓ Completed      3     100%
 ⏸ Queued         0       0%
 ✗ Failed         0       0%
```

---

### Step 3: Download Extraction Results

Download the extraction results (sections) for manual review:

```bash
idp-cli download-results \
    --stack-name eval-testing \
    --batch-id initial-run \
    --output-dir ~/initial-results/ \
    --file-types sections
```

**Result structure:**
```
~/initial-results/initial-run/
├── invoice.pdf/
│   └── sections/
│       └── 1/
│           └── result.json      # Extracted data to validate
├── w2.pdf/
│   └── sections/
│       └── 1/
│           └── result.json
└── paystub.pdf/
    └── sections/
        └── 1/
            └── result.json
```

---

### Step 4: Manual Validation & Baseline Preparation

Review and correct the extraction results to create validated baselines.

**4.1 Review extraction results:**

```bash
# View extracted data for invoice
cat ~/initial-results/initial-run/invoice.pdf/sections/1/result.json | jq .

# Example output:
{
  "attributes": {
    "Invoice Number": "INV-2024-001",
    "Invoice Date": "2024-01-15",
    "Total Amount": "$1,250.00",
    "Vendor Name": "Acme Corp"
  }
}
```

**4.2 Validate and correct:**

Compare extracted values against the actual documents. If you find errors, create corrected baseline files:

```bash
# Create baseline directory structure
mkdir -p ~/validated-baselines/invoice.pdf/sections/1/
mkdir -p ~/validated-baselines/w2.pdf/sections/1/
mkdir -p ~/validated-baselines/paystub.pdf/sections/1/

# Copy and edit result files
cp ~/initial-results/initial-run/invoice.pdf/sections/1/result.json \
   ~/validated-baselines/invoice.pdf/sections/1/result.json

# Edit the baseline to correct any errors
vi ~/validated-baselines/invoice.pdf/sections/1/result.json

# Repeat for other documents...
```

**Baseline directory structure:**
```
~/validated-baselines/
├── invoice.pdf/
│   └── sections/
│       └── 1/
│           └── result.json      # Corrected/validated data
├── w2.pdf/
│   └── sections/
│       └── 1/
│           └── result.json
└── paystub.pdf/
    └── sections/
        └── 1/
            └── result.json
```

---

### Step 5: Create Manifest with Baseline References

Create a manifest that links each document to its validated baseline:

```bash
cat > ~/evaluation-manifest.csv << EOF
document_path,baseline_source
/home/user/test-documents/invoice.pdf,/home/user/validated-baselines/invoice.pdf/
/home/user/test-documents/w2.pdf,/home/user/validated-baselines/w2.pdf/
/home/user/test-documents/paystub.pdf,/home/user/validated-baselines/paystub.pdf/
EOF
```

**Manifest format:**
- `document_path`: Path to original document
- `baseline_source`: Path to directory containing validated sections

**Alternative using auto-matching:**

```bash
# Generate manifest with automatic baseline matching
idp-cli generate-manifest \
    --dir ~/test-documents/ \
    --baseline-dir ~/validated-baselines/ \
    --output ~/evaluation-manifest.csv
```

---

### Step 6: Process with Evaluation Enabled

Reprocess documents with the baseline-enabled manifest. The accelerator will automatically run evaluation:

```bash
idp-cli run-inference \
    --stack-name eval-testing \
    --manifest ~/evaluation-manifest.csv \
    --batch-id eval-run-001 \
    --monitor
```

**What happens:** 
1. Documents are processed through the pipeline as before
2. **Evaluation step is automatically triggered** because baselines are provided
3. The evaluation module compares extracted values against baseline values
4. Detailed metrics are calculated per attribute and per document

**Processing time:** Similar to initial run, plus ~5-10 seconds per document for evaluation.

---

### Step 7: Download and Review Evaluation Results

Download the evaluation results to analyze accuracy:

**✓ Synchronous Evaluation:** Evaluation runs as the final step in the workflow before completion. When a document shows status "COMPLETE", all processing including evaluation is finished - results are immediately available for download.

```bash
# Download evaluation results (no waiting needed)
idp-cli download-results \
    --stack-name eval-testing \
    --batch-id eval-run-001 \
    --output-dir ~/eval-results/ \
    --file-types evaluation

# Verify evaluation data is present
ls -la ~/eval-results/eval-run-001/invoice.pdf/evaluation/
# Should show: report.json and report.md
```

**Review evaluation report:**

```bash
# View detailed evaluation metrics
cat ~/eval-results/eval-run-001/invoice.pdf/evaluation/report.json | jq .
```

**View human-readable report:**

```bash
# Markdown report with visual formatting
cat ~/eval-results/eval-run-001/invoice.pdf/evaluation/report.md
```

---

## Evaluation Analytics

The IDP Accelerator provides multiple ways to analyze evaluation results across batches and at scale.

### Query Aggregated Results with Athena

The accelerator automatically stores evaluation metrics in Athena tables for SQL-based analysis.

**Available Tables:**
- `evaluation_results` - Per-document evaluation metrics
- `evaluation_attributes` - Per-attribute scores
- `evaluation_summary` - Aggregated statistics

**Example Queries:**

```sql
-- Overall accuracy across all batches
SELECT 
    AVG(overall_accuracy) as avg_accuracy,
    COUNT(*) as total_documents,
    SUM(CASE WHEN overall_accuracy >= 0.95 THEN 1 ELSE 0 END) as high_accuracy_count
FROM evaluation_results
WHERE batch_id LIKE 'eval-run-%';

-- Attribute-level accuracy
SELECT 
    attribute_name,
    AVG(score) as avg_score,
    COUNT(*) as total_occurrences,
    SUM(CASE WHEN match = true THEN 1 ELSE 0 END) as correct_count
FROM evaluation_attributes
GROUP BY attribute_name
ORDER BY avg_score DESC;

-- Compare accuracy across different configurations
SELECT 
    batch_id,
    AVG(overall_accuracy) as accuracy,
    COUNT(*) as doc_count
FROM evaluation_results
WHERE batch_id IN ('config-v1', 'config-v2', 'config-v3')
GROUP BY batch_id;
```

**Access Athena:**
```bash
# Get Athena database name from stack outputs
aws cloudformation describe-stacks \
    --stack-name eval-testing \
    --query 'Stacks[0].Outputs[?OutputKey==`ReportingDatabase`].OutputValue' \
    --output text

# Query via AWS Console or CLI
aws athena start-query-execution \
    --query-string "SELECT * FROM evaluation_results LIMIT 10" \
    --result-configuration OutputLocation=s3://your-results-bucket/
```

**For detailed Athena table schemas and query examples, see:**
- [`../docs/reporting-database.md`](../docs/reporting-database.md) - Complete Athena table reference
- [`../docs/evaluation.md`](../docs/evaluation.md) - Evaluation methodology and metrics

---

### Use Agent Analytics in the Web UI

The IDP web UI provides an Agent Analytics feature for visual analysis of evaluation results.

**Access the UI:**

1. Get web UI URL from stack outputs:
```bash
aws cloudformation describe-stacks \
    --stack-name eval-testing \
    --query 'Stacks[0].Outputs[?OutputKey==`ApplicationWebURL`].OutputValue' \
    --output text
```

2. Login with admin credentials (from deployment email)

3. Navigate to **Analytics** → **Agent Analytics**

**Available Analytics:**
- **Accuracy Trends** - Track accuracy over time across batches
- **Attribute Heatmaps** - Visualize which attributes perform best/worst
- **Batch Comparisons** - Compare different configurations side-by-side
- **Error Analysis** - Identify common error patterns
- **Confidence Correlation** - Analyze relationship between assessment confidence and accuracy

**Key Features:**
- Interactive charts and visualizations
- Filter by batch, date range, document type, or attribute
- Export results to CSV for further analysis
- Drill-down to individual document details

**For complete Agent Analytics documentation, see:**
- [`../docs/agent-analysis.md`](../docs/agent-analysis.md) - Agent Analytics user guide

---

## Manifest Format Reference

### CSV Format

**Required Field:**
- `document_path`: Local file path or full S3 URI (s3://bucket/key)

**Optional Field:**
- `baseline_source`: Path or S3 URI to validated baseline for evaluation

**Note:** Document IDs are auto-generated from filenames (e.g., `invoice.pdf` → `invoice`)

**Examples:**

```csv
document_path
/home/user/docs/invoice.pdf
/home/user/docs/w2.pdf
s3://external-bucket/statement.pdf
```

```csv
document_path,baseline_source
/local/invoice.pdf,s3://baselines/invoice/
/local/w2.pdf,/local/validated-baselines/w2/
s3://docs/statement.pdf,s3://baselines/statement/
```

### JSON Format

```json
[
  {
    "document_path": "/local/invoice.pdf",
    "baseline_source": "s3://baselines/invoice/"
  },
  {
    "document_path": "s3://bucket/w2.pdf",
    "baseline_source": "/local/baselines/w2/"
  }
]
```

### Path Rules

**Document Type (Auto-detected):**
- `s3://...` → S3 file (copied to InputBucket)
- Absolute/relative path → Local file (uploaded to InputBucket)

**Document ID (Auto-generated):**
- From filename without extension
- Example: `invoice-2024.pdf` → `invoice-2024`
- Subdirectories preserved: `W2s/john.pdf` → `W2s/john`

**Baseline Source Type (Auto-detected):**
- Local path → the directory is uploaded recursively
- `s3://bucket/prefix/` → every object under the prefix is copied, keeping its
  directory shape. This is the form `generate-manifest --test-set` writes

⚠️ **A `baseline_source` naming a single S3 object copies nothing.** When
`idp-cli process --manifest` consumes the manifest, an `s3://` value is treated as a
**prefix** — a trailing `/` is appended if absent — so `s3://bucket/gt/invoice.json`
becomes a prefix that matches no object and the document is processed with no baseline
to score against. Point `baseline_source` at the directory holding the baseline files,
not at one of them.

**Important:**
- ⚠️ Duplicate filenames not allowed
- ✅ Use directory structure for organization (e.g., `clientA/invoice.pdf`, `clientB/invoice.pdf`)
- ✅ S3 URIs can reference any bucket (automatically copied)

---

## Advanced Usage

### Iterative Configuration Testing

Test different extraction prompts or configurations:

```bash
# Test with configuration v1
idp-cli deploy --stack-name my-stack --custom-config ./config-v1.yaml --wait
idp-cli run-inference --stack-name my-stack --dir ./test-set/ --batch-id config-v1 --monitor

# Download and analyze results
idp-cli download-results --stack-name my-stack --batch-id config-v1 --output-dir ./results-v1/

# Test with configuration v2
idp-cli deploy --stack-name my-stack --custom-config ./config-v2.yaml --wait
idp-cli run-inference --stack-name my-stack --dir ./test-set/ --batch-id config-v2 --monitor

# Compare in Athena
# SELECT batch_id, AVG(overall_accuracy) FROM evaluation_results 
# WHERE batch_id IN ('config-v1', 'config-v2') GROUP BY batch_id;
```

### Large-Scale Batch Processing

Process thousands of documents efficiently:

```bash
# Generate manifest for large dataset
idp-cli generate-manifest \
    --dir ./production-documents/ \
    --output large-batch-manifest.csv

# Validate before processing
idp-cli validate-manifest --manifest large-batch-manifest.csv

# Process in background (no --monitor flag)
idp-cli run-inference \
    --stack-name production-stack \
    --manifest large-batch-manifest.csv \
    --batch-id production-batch-001

# Check status later
idp-cli status \
    --stack-name production-stack \
    --batch-id production-batch-001
```

### CI/CD Integration

Integrate into automated pipelines:

```bash
#!/bin/bash
# ci-test.sh - Automated accuracy testing

# Run processing with evaluation
idp-cli run-inference \
    --stack-name ci-stack \
    --manifest test-suite-with-baselines.csv \
    --batch-id ci-test-$BUILD_ID \
    --monitor

# Download evaluation results
idp-cli download-results \
    --stack-name ci-stack \
    --batch-id ci-test-$BUILD_ID \
    --output-dir ./ci-results/ \
    --file-types evaluation

# Parse results and fail if accuracy below threshold
python check_accuracy.py ./ci-results/ --min-accuracy 0.90

# Exit code 0 if passed, 1 if failed
exit $?
```

---

### `stop-workflows`

Stop all running workflows for a stack. Useful for halting processing during development or when issues are detected.

**Usage:**
```bash
idp-cli stop-workflows [OPTIONS]
```

**Options:**
- `--stack-name` (required): CloudFormation stack name
- `--skip-purge`: Skip purging the SQS queue
- `--skip-stop`: Skip stopping Step Function executions
- `--region`: AWS region (optional)

**Examples:**

```bash
# Stop all workflows (purge queue + stop executions)
idp-cli stop-workflows --stack-name my-stack

# Only purge the queue (don't stop running executions)
idp-cli stop-workflows --stack-name my-stack --skip-stop

# Only stop executions (don't purge queue)
idp-cli stop-workflows --stack-name my-stack --skip-purge
```

---

### `load-test`

Run load tests by copying files to the input bucket at specified rates.

**Usage:**
```bash
idp-cli load-test [OPTIONS]
```

**Options:**
- `--stack-name` (required): CloudFormation stack name
- `--source-file` (required): Source file to copy (local path or s3://bucket/key)
- `--rate`: Files per minute (default: 100)
- `--duration`: Duration in minutes (default: 1)
- `--schedule`: CSV schedule file (minute,count) - overrides --rate and --duration
- `--dest-prefix`: Destination prefix in input bucket (default: load-test)
- `--config-profile` (alias: `--config-version`): Configuration profile to use for processing (default: active profile)
- `--region`: AWS region (optional)

**Examples:**

```bash
# Constant rate: 100 files/minute for 5 minutes
idp-cli load-test --stack-name my-stack --source-file samples/invoice.pdf --rate 100 --duration 5

# High volume: 2500 files/minute for 1 minute
idp-cli load-test --stack-name my-stack --source-file samples/invoice.pdf --rate 2500

# Use schedule file for variable rates
idp-cli load-test --stack-name my-stack --source-file samples/invoice.pdf --schedule schedule.csv

# Use S3 source file
idp-cli load-test --stack-name my-stack --source-file s3://my-bucket/test.pdf --rate 500

# Load test with a specific config profile
idp-cli load-test --stack-name my-stack --source-file samples/invoice.pdf --rate 100 --config-profile v2
```

**Schedule File Format (CSV):**
```csv
minute,count
1,100
2,200
3,500
4,1000
5,500
```

See `lib/idp_cli_pkg/examples/load-test-schedule.csv` for a sample schedule file.

---

### `remove-deleted-stack-resources`

Remove residual AWS resources left behind from deleted IDP CloudFormation stacks.

**⚠️ CAUTION:** This command permanently deletes AWS resources. Always run with `--dry-run` first.

> **Intended Use:** This command is designed for **development and test accounts** where IDP stacks are frequently created and deleted, and where the consequences of accidentally deleting resources or data are low. **Do not use this command in production accounts** where data retention is critical. For production cleanup, manually review and delete resources through the AWS Console.

**Usage:**
```bash
idp-cli remove-deleted-stack-resources [OPTIONS]
```

**How It Works:**

This command safely identifies and removes ONLY resources belonging to IDP stacks that have been deleted:

1. **Multi-region Stack Discovery** - Scans CloudFormation in multiple regions (us-east-1, us-west-2, eu-central-1 by default)
2. **IDP Stack Identification** - Identifies IDP stacks by their Description ("AWS GenAI IDP Accelerator") or naming patterns (IDP-*, PATTERN1/2/3)
3. **Active Stack Protection** - Tracks both ACTIVE and DELETED stacks; resources from active stacks are NEVER touched
4. **Safe Cleanup** - Only targets resources belonging to stacks in DELETE_COMPLETE state

**Safety Features:**
- Resources from ACTIVE stacks are protected and skipped
- Resources from UNKNOWN stacks (not verified as IDP) are skipped
- Interactive confirmation for each resource (unless --yes)
- Options: y=yes, n=no, a=yes to all of type, s=skip all of type
- --dry-run mode shows exactly what would be deleted

**Resources Cleaned:**
- CloudFront distributions and response header policies
- CloudWatch log groups  
- AppSync APIs (leftovers from stacks created before AppSync was removed; current stacks create none)
- IAM policies
- CloudWatch Logs resource policy entries
- S3 buckets (automatically emptied before deletion)
- DynamoDB tables (PITR disabled before deletion)

> **Note:** This command targets resources that remain in AWS after IDP stacks have already been deleted. These are typically resources with RetainOnDelete policies or non-empty S3 buckets that CloudFormation couldn't delete. All resources are identified by their naming pattern and verified against the deleted stack registry before deletion.

**Options:**
- `--region`: Primary AWS region for regional resources (default: us-west-2)
- `--profile`: AWS profile to use
- `--dry-run`: Preview changes without making them **(RECOMMENDED first step)**
- `--yes`, `-y`: Auto-approve all deletions (skip confirmations)
- `--check-stack-regions`: Comma-separated regions to check for stacks (default: us-east-1,us-west-2,eu-central-1)

**Examples:**

```bash
# RECOMMENDED: Always dry-run first to see what would be deleted
idp-cli remove-deleted-stack-resources --dry-run

# Interactive cleanup with confirmations for each resource
idp-cli remove-deleted-stack-resources

# Use specific AWS profile
idp-cli remove-deleted-stack-resources --profile my-profile

# Auto-approve all deletions (USE WITH CAUTION)
idp-cli remove-deleted-stack-resources --yes

# Check additional regions for stacks
idp-cli remove-deleted-stack-resources --check-stack-regions us-east-1,us-west-2,eu-central-1,eu-west-1
```

**CloudFront Two-Phase Cleanup:**

CloudFront requires distributions to be disabled before deletion:
1. **First run:** Disables orphaned distributions (you confirm each)
2. **Wait 15-20 minutes** for CloudFront global propagation
3. **Second run:** Deletes the previously disabled distributions

**Interactive Confirmation:**

```
Delete orphaned CloudFront distribution?
  Resource: E1H6W47Z36CQE2 (exists in AWS)
  Originally from stack: IDP-P2-DevTest1
  Stack status: DELETE_COMPLETE (stack no longer exists)
  Stack was in region: us-west-2

  Options: y=yes, n=no, a=yes to all CloudFront distribution, s=skip all CloudFront distribution
Delete? [y/n/a/s]: 
```

**Important Limitation - 90-Day Window:**

CloudFormation only retains deleted stack information for approximately 90 days. After this period, stacks in `DELETE_COMPLETE` status are removed from the CloudFormation API.

This means:
- Resources from stacks deleted **within the past 90 days** → Identified and offered for cleanup
- Resources from stacks deleted **more than 90 days ago** → Not identified (silently skipped)

**Best Practice:** Run `remove-deleted-stack-resources` promptly after deleting IDP stacks to ensure complete cleanup. For maximum effectiveness, run this command within 90 days of stack deletion.

---

### `config-create`

Generate an IDP configuration template from system defaults.

**Usage:**
```bash
idp-cli config-create [OPTIONS]
```

**Options:**
- `--features`: Feature set (default: `min`)
  - `min`: classification, extraction, classes only (simplest)
  - `core`: min + ocr, assessment
  - `all`: all sections with full defaults
  - Or comma-separated list: `"classification,extraction,summarization"`
- `--output`, `-o`: Output file path (default: stdout)
- `--include-prompts`: Include full prompt templates (default: stripped for readability)
- `--no-comments`: Omit explanatory header comments

**Examples:**

```bash
# Generate minimal config to stdout
idp-cli config-create

# Generate full config with all sections
idp-cli config-create --features all --output full-config.yaml

# Custom section selection
idp-cli config-create --features "classification,extraction,summarization" --output config.yaml
```

---

### `config-validate`

Validate a configuration file against system defaults and Pydantic models. Catches common configuration errors that cause silent pipeline failures.

**Validation Checks:**
- **YAML/JSON Syntax** - Ensures file is well-formed
- **Schema Validation** - Validates against Pydantic models
- **Model IDs** - Verifies Bedrock model IDs are valid (checked against pricing.yaml)
- **Placeholder Validation** - Ensures required placeholders are present in custom task_prompts:
  - `ocr.task_prompt` (bedrock only): Requires `{DOCUMENT_IMAGE}`
  - `classification.task_prompt`: Requires `{DOCUMENT_TEXT}` OR `{DOCUMENT_IMAGE}`
  - `extraction.task_prompt`: Requires `{DOCUMENT_TEXT}` OR `{DOCUMENT_IMAGE}`
  - `assessment.task_prompt`: Requires `{DOCUMENT_IMAGE}`, `{OCR_TEXT_CONFIDENCE}`, `{EXTRACTION_RESULTS}`
  - `summarization.task_prompt`: Requires `{DOCUMENT_TEXT}`, `{EXTRACTION_RESULTS}`
- **JSON Schema Fields** - Warns about non-standard fields (e.g., `data_type`)
- **OpenAI GPT-5.x compatibility** - Errors if an `openai.gpt-5.*` model is paired with **agentic extraction** (`extraction.agentic.enabled: true`, including per-class `x-aws-idp-extraction-model` overrides) or used for **Discovery** (`discovery.*.model_id` / `discovery.rules.model`). These models run on the `bedrock-mantle` Responses API and cannot accept the Strands agent loop or whole-PDF document blocks. See [OpenAI GPT-5.x Models](openai-models.md).
- **xAI Grok compatibility** - Errors if `us.xai.grok-4.6` / `global.xai.grok-4.6` is used for **Discovery** (`discovery.*.model_id` / `discovery.rules.model`), because Grok rejects whole-PDF `document` blocks. Unlike GPT-5.x, Grok **is** valid for agentic extraction — it reaches Converse and supports tool use, so that pairing is deliberately allowed. See [xAI Grok Models](grok-models.md).
- **OpenAI GPT-6 Astra compatibility** - Errors if `us.openai.gpt-6-astra` / `global.openai.gpt-6-astra` is used for **Discovery** (`discovery.*.model_id` / `discovery.rules.model`), because Astra rejects whole-PDF `document` blocks. Like Grok — and unlike its GPT-5.x stablemates — Astra **is** valid for agentic extraction, since it reaches Converse and emits `toolUse`. The agentic check keys on the bedrock-mantle route rather than the `openai.` prefix, so one OpenAI model passes it and the others do not. See [OpenAI Models](openai-models.md#gpt-6-astra-converse).

**Usage:**
```bash
idp-cli config-validate [OPTIONS]
```

**Options:**
- `--config-file`, `-f` (required): Path to configuration file to validate
- `--show-merged`: Show the full merged configuration
- `--strict`: Fail validation if config contains unknown or deprecated fields

**Examples:**

```bash
# Validate a config file
idp-cli config-validate --config-file ./my-config.yaml

# Show full merged config
idp-cli config-validate --config-file ./config.yaml --show-merged

# Strict mode (fails if config has unknown or deprecated fields — useful for CI/CD)
idp-cli config-validate --config-file ./config.yaml --strict
```

**Notes:**
- The `config-upload` command runs validation by default before uploading to protect production stacks.
- Model ID validation requires `config_library/pricing.yaml`. Ensure `idp-cli` is run from the repository root, or set the `IDP_PROJECT_ROOT` environment variable to the repo root for validation to work correctly.

---

### `config-download`

Download configuration from a deployed IDP stack.

**Usage:**
```bash
idp-cli config-download [OPTIONS]
```

**Options:**
- `--stack-name` (required): CloudFormation stack name
- `--output`, `-o`: Output file path (default: stdout)
- `--format`: Output format - `full` (default) or `minimal` (only differences from defaults)
- `--config-profile` (alias: `--config-version`): Configuration profile to download (e.g., v1, v2). If not specified, downloads the active profile
- `--config-revision`: Download an exact **revision** of that profile instead of its current configuration (e.g. `7`). Requires `--config-profile`. Fails if the revision is no longer retained rather than silently returning the current configuration
- `--region`: AWS region (optional)

**Examples:**

```bash
# Download full config from the active profile
idp-cli config-download --stack-name my-stack --output config.yaml

# Download a specific profile
idp-cli config-download --stack-name my-stack --config-profile v2 --output config.yaml

# Download minimal config (only customizations)
idp-cli config-download --stack-name my-stack --format minimal --output config.yaml

# Print to stdout
idp-cli config-download --stack-name my-stack

# Download an exact revision — what an earlier run actually used
idp-cli config-download --stack-name my-stack --config-profile lending \
    --config-revision 7 --output r7.yaml
```

⚠️ **A profile that does not exist is refused, and did not always be.** A typo in
`--config-profile` used to exit 0 having written the YAML null document — so
`config-download --config-profile lendnig > config.yaml` left a file every downstream
step reads as an *empty* configuration, under an exit code that said it worked. All
three spellings (stdout, `--output`, `--format minimal`) now exit 1, name the profile
they could not find, and write no file. A script that swallowed the exit code and
carried on with the downloaded file will now be handed nothing instead of an empty
configuration.

---

### `config-upload`

Upload a configuration file to a deployed IDP stack.

> **Note:** Configurations with `managed: true` cannot be uploaded via CLI. Managed configurations are stack-controlled and automatically overwritten during stack updates. Remove `managed: true` from your configuration file before uploading.

**Usage:**
```bash
idp-cli config-upload [OPTIONS]
```

**Options:**
- `--stack-name` (required): CloudFormation stack name
- `--config-file`, `-f` (required): Path to configuration file (YAML or JSON)
- `--validate/--no-validate`: Validate config before uploading (default: validate)
- `--config-profile` (alias: `--config-version`) **(required)**: Configuration profile to update (e.g., `default`, `v1`, `v2`). If the profile doesn't exist, it will be created automatically. An **empty** value is refused with exit 1 and nothing is written; it used to land the configuration on a key no profile listing can see, reported as "Configuration is now active!" with exit 0
- `--version-description`: Description for the configuration **profile** (persisted on the profile and overwritten by every save)
- `--revision-notes`: What this upload changed, recorded on the **revision** it cuts and shown as *Notes* in the revision history (e.g. `'raised topK to 20'`). Per-revision and immutable, unlike `--version-description`
- `--region`: AWS region (optional)

**Examples:**

```bash
# Upload config to the active profile
idp-cli config-upload --stack-name my-stack --config-file ./config.yaml --config-profile default

# Update an existing profile
idp-cli config-upload --stack-name my-stack --config-file ./config.yaml --config-profile Production

# Create a new profile with a description
idp-cli config-upload --stack-name my-stack --config-file ./config.yaml --config-profile NewProfile --version-description "Test configuration for new feature"

# Skip validation (use with caution)
idp-cli config-upload --stack-name my-stack --config-file ./config.yaml --no-validate
```

**Output:** on success the command prints the **revision** the upload produced,
with the flags to process under it:

```
✓ Configuration uploaded successfully

Configuration profile 'lending' updated!
Revision: r7
Process under it with: --config-profile lending --config-revision 7
```

Nothing is printed when the stack has no revision history. A save that changes
nothing records no new revision, so the number printed is then the revision
already current — which is still the correct one to pin.

---

### `config-list`

List all configuration profiles in a deployed IDP stack.

**Usage:**
```bash
idp-cli config-list [OPTIONS]
```

**Options:**
- `--stack-name` (required): CloudFormation stack name
- `--region`: AWS region (optional)

**Examples:**
```bash
# List all configuration profiles
idp-cli config-list --stack-name my-stack
```

**Output:**
Shows a table with profile names, active status, the current **revision** (`Rev`),
creation/update timestamps, and descriptions. `Rev` is the revision each profile's
configuration currently reflects — the value to pass to `--config-revision`. It is
blank for a profile with no history.

---

### `config-revisions`

List the revision history of a Configuration Profile.

Every save of a profile cuts an immutable revision. This shows the ones still
retained: the last 20, plus anything labeled, pinned by a test run, or currently
in use. See [configuration-profiles.md](configuration-profiles.md#revision-history).

**Usage:**
```bash
idp-cli config-revisions [OPTIONS]
```

**Options:**
- `--stack-name` (required): CloudFormation stack name
- `--config-profile` (alias: `--config-version`) **(required)**: Profile whose history to list
- `--json`: Emit JSON instead of a table, for scripting
- `--region`: AWS region (optional)

**Examples:**
```bash
# Revision history of a profile
idp-cli config-revisions --stack-name my-stack --config-profile lending

# Machine-readable — e.g. pick the current revision
idp-cli config-revisions --stack-name my-stack --config-profile lending --json \
    | jq -r '.revisions[] | select(.published) | .revision'
```

**Output:** one row per retained revision, newest first, with `Rev`, which one the
profile currently reflects, when and by whom it was cut, its label and notes, and
**why it is exempt from pruning** (`labeled` / `test run`). A profile with no
history reports that and exits 0 — a profile untouched since the stack was
upgraded genuinely has none.

#### Iterating on one profile instead of many

`config-upload` (which prints the revision it produced), `config-revisions`, and
`--config-revision` on `config-download` / `process` / `run-inference` together
let an automated tuning loop keep **one** profile and track its attempts as
revisions:

```bash
# Upload attempt N and capture the revision it became
rev=$(idp-cli config-upload --stack-name my-stack --config-file attempt.yaml \
        --config-profile tuning-run-42 --version-description "raised topK to 20" \
      | sed -n 's/^Revision: r//p')

# Score exactly that revision
idp-cli run-inference --stack-name my-stack --test-set my-tests \
    --config-profile tuning-run-42 --config-revision "$rev" --monitor

# Later: retrieve the configuration that produced the best score
idp-cli config-download --stack-name my-stack --config-profile tuning-run-42 \
    --config-revision 7 --output best.yaml
```

Naming a new profile per attempt also works, but every one of them then appears in
the profile pickers and access-control scope lists of the whole deployment.

---

### `config-activate`

Activate a configuration profile in a deployed IDP stack.

**Automatic BDA Sync:** If the configuration profile has `use_bda` enabled, this command will automatically sync the configuration to BDA (Bedrock Data Automation) before activation. This ensures BDA blueprints are up-to-date and matches the UI behavior.

**Usage:**
```bash
idp-cli config-activate [OPTIONS]
```

**Options:**
- `--stack-name` (required): CloudFormation stack name
- `--config-profile` (alias: `--config-version`) **(required)**: Configuration profile to activate
- `--region`: AWS region (optional)

**Examples:**
```bash
# Activate a specific profile
idp-cli config-activate --stack-name my-stack --config-profile v2

# Activate the default profile
idp-cli config-activate --stack-name my-stack --config-profile default
```

**Behavior:**
1. Validates the configuration profile exists
2. If `use_bda` is enabled in the configuration:
   - Syncs IDP document classes to BDA blueprints
   - Creates a new BDA project if none exists
   - Updates BDA sync status
3. Activates the configuration profile
4. All new document processing will use this configuration

**Note:** If BDA sync fails (when `use_bda` is enabled), the activation will be aborted to prevent processing errors.

**An aborted activation can still have left a blueprint behind.** The BDA sync this
command runs is the same replace-mode sync as
[`config-sync-bda`](#config-sync-bda), so the same thing can happen: a blueprint removed
from the BDA project that could not then be deleted. The deletes happen whatever became
of the document classes, which makes an aborted activation the outcome most likely to
have left one. Those ARNs are printed whether the activation succeeded or failed, and
they are not counted as failed classes — the remedy is the orphaned-blueprint cleanup,
[`config-sync-bda --direction cleanup-orphaned`](#--direction-cleanup-orphaned), not a
re-run of this command. See the `config-sync-bda` section for the full explanation.

**Notes:**
- Sets the specified profile as active for all new document processing
- Profile must exist (use `config-list` to see available profiles)

---

### `config-delete`

Delete a configuration profile from a deployed IDP stack.

**Usage:**
```bash
idp-cli config-delete [OPTIONS]
```

**Options:**
- `--stack-name` (required): CloudFormation stack name
- `--config-profile` (alias: `--config-version`) **(required)**: Configuration profile to delete
- `--force`: Skip confirmation prompt
- `--region`: AWS region (optional)

**Examples:**
```bash
# Delete a profile with confirmation
idp-cli config-delete --stack-name my-stack --config-profile old-profile

# Delete without confirmation prompt
idp-cli config-delete --stack-name my-stack --config-profile old-profile --force
```

**Restrictions:**
- Cannot delete the 'default' configuration profile
- Cannot delete the currently active profile (activate another profile first)
- Includes confirmation prompt unless `--force` is used

**What Happens:**
1. Loads and parses your YAML or JSON config file
2. Validates against system defaults (unless `--no-validate`)
3. If the profile exists: Updates it with the uploaded configuration (saved as a complete snapshot)
4. If the profile doesn't exist: Creates a new profile with the uploaded configuration
5. Uploads to the stack's ConfigurationTable in DynamoDB
6. Configuration is immediately available for document processing

**Configuration Profiles:**
- **Existing profile**: Saves the uploaded configuration as the full profile snapshot
- **New profile**: Creates a new independent profile with the uploaded configuration
- **Profile descriptions**: Can be added to new profiles for better organization

`--config-revision <n>` pins an exact revision of `--config-profile` on `process`
and `run-inference`; omit it to process under the profile's current configuration.

For full details on configuration profiles and their revisions, see [configuration-profiles.md](configuration-profiles.md).

This uses the same mechanism as the Web UI configuration management system.

---

### `test-result`

Get test results for a specific Test Studio test run with automatic evaluation triggering.

**Usage:**
```bash
idp-cli test-result [OPTIONS]
```

**Options:**
- `--stack-name` (required): CloudFormation stack name
- `--test-run-id` (required): Test run ID to retrieve results for
- `--wait`: Wait for evaluation to complete (polls until metrics are calculated)
- `--timeout`: Timeout in seconds when using `--wait` (default: 600)
- `--output-dir`: Directory to save results as JSON file
- `--region`: AWS region (optional)

**Examples:**
```bash
# Get results immediately (may show "EVALUATING" status if metrics not ready)
idp-cli test-result \
  --stack-name my-stack \
  --test-run-id fake-w2-20260409-123456

# Wait for evaluation to complete (recommended for CI/CD)
idp-cli test-result \
  --stack-name my-stack \
  --test-run-id fake-w2-20260409-123456 \
  --wait --timeout 900

# Save results to JSON file
idp-cli test-result \
  --stack-name my-stack \
  --test-run-id fake-w2-20260409-123456 \
  --wait --output-dir ./results
```

**Exit codes:** `0` when the run passed, `1` when it did not — `status` is `FAILED`, or
any file failed. The results are printed before the exit either way, so you still get
the accuracy figures for a failed run.

⚠️ This command used to exit `0` for a run with `status="FAILED"` and every file
failed, exactly like a clean pass, so `idp-cli test-result ... && deploy` proceeded on a
failed evaluation. A CI job that relied on that will now stop, which is the point.

**Output:**
- Overall accuracy, precision, recall, F1 score
- Total cost
- Files completed/failed
- Created/completed timestamps
- JSON file: `<test-run-id>-result.json` (when `--output-dir` specified)

**Behavior:**
- Triggers lazy evaluation if metrics not yet calculated (first call after test run completes)
- Polls Lambda every 10 seconds when `--wait` is used
- Returns complete test run data including field-level metrics and cost breakdown

---

### `abort-test-run`

Abort one or more running Test Studio test runs. Stops all document processing workflows and preserves results from completed documents.

**Usage:**
```bash
idp-cli abort-test-run [OPTIONS]
```

**Options:**
- `--stack-name` (required): CloudFormation stack name
- `--test-run-ids` (required): Comma-separated list of test run IDs to abort
- `--force` / `-y`: Skip confirmation prompt
- `--region`: AWS region (optional)

**Examples:**
```bash
# Abort a single test run
idp-cli abort-test-run \
  --stack-name my-stack \
  --test-run-ids "fake-w2-20260409-123456"

# Abort multiple test runs
idp-cli abort-test-run \
  --stack-name my-stack \
  --test-run-ids "run1,run2,run3"

# Skip confirmation prompt
idp-cli abort-test-run \
  --stack-name my-stack \
  --test-run-ids "fake-w2-20260409-123456" \
  --force
```

**Output:**
- Success/failure count for each test run
- Error details for failed aborts (e.g., test run not found, already completed)
- Confirmation prompt unless `--force` is used

**Behavior:**
- Only test runs with status **QUEUED** or **RUNNING** can be aborted
- Completed documents are preserved with their evaluation results
- Test run status is updated to **ABORTED**
- Metrics are calculated for any completed documents
- Document workflows are stopped via Step Functions execution abort

**Limitations:**
- Cannot abort test runs with status **EVALUATING**, **COMPLETED**, **PARTIAL_COMPLETE**, or **FAILED**
- The abort operation waits up to 25 seconds for documents to reach terminal state

---

### `test-compare`

Compare metrics and configurations from multiple Test Studio test runs.

**Usage:**
```bash
idp-cli test-compare [OPTIONS]
```

**Options:**
- `--stack-name` (required): CloudFormation stack name
- `--test-run-ids` (required): Comma-separated list of test run IDs to compare (minimum 2)
- `--output-dir`: Directory to save comparison as JSON and CSV files
- `--region`: AWS region (optional)

**Examples:**
```bash
# Compare two test runs
idp-cli test-compare \
  --stack-name my-stack \
  --test-run-ids "fake-w2-20260409-123456,fake-w2-20260409-234567"

# Compare multiple runs and export to files
idp-cli test-compare \
  --stack-name my-stack \
  --test-run-ids "run1,run2,run3" \
  --output-dir ./comparisons
```

**Output:**
- **Console**: Side-by-side table with accuracy, precision, recall, F1 score, and cost for each test run
- **JSON file**: `comparison-<timestamp>.json` - Complete comparison data with full test results and config differences
- **CSV file**: `comparison-<timestamp>.csv` - Metrics table suitable for spreadsheets

**Configuration Differences:**
- Automatically detects and displays configuration differences between test runs
- Shows nested config paths (e.g., `classification.model`, `extraction.temperature`)
- Highlights values that differ across test runs

**Requirements:**
- All test runs must be in `COMPLETE` or `PARTIAL_COMPLETE` status
- Minimum 2 test runs required for comparison

---

### `discover`

Discover document class schemas from sample documents using Amazon Bedrock.

**Two modes:**
- **Stack-connected** (`--stack-name`): Uses stack's discovery config and saves schema to DynamoDB configuration
- **Local** (no `--stack-name`): Uses system default Bedrock settings, prints schema to stdout without saving

**Ground truth matching:** Ground truth files (`-g`) are auto-matched to documents (`-d`) by filename stem. For example, `invoice.pdf` matches `invoice.json`. Unmatched documents run without ground truth.

- **Single document + single ground truth:** When exactly one `-d` and one `-g` are provided, they are paired by position regardless of filename stem. This supports the common case where ground truth files have generic names (e.g., `baseline/<doc>/sections/1/result.json`).
- **Batch mode (multiple `-d` or multiple `-g`):** Files are matched by stem. If any `-g` file cannot be matched to a document, `discover` exits non-zero with a clear error. This prevents silently running without-GT discovery when the user explicitly requested ground-truth-guided discovery. See [issue #310](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/310).

**Output behavior:**
- Single document: `-o` writes the schema to the specified file
- Batch + `-o` is a directory (or has no extension): writes one `{class_name}.json` per schema
- Batch + `-o` is a file: writes all schemas as a JSON array

```bash
# Single document (local mode — no stack needed)
idp-cli discover -d ./invoice.pdf

# With ground truth (matched by filename stem)
idp-cli discover -d ./invoice.pdf -g ./invoice.json

# Save schema to file
idp-cli discover -d ./form.pdf -o ./form-schema.json

# With class name hint (guides LLM to use specific class name)
idp-cli discover -d ./form.pdf --class-hint "W2 Tax Form"

# Batch with auto-matched ground truth
idp-cli discover -d ./invoice.pdf -d ./w2.pdf -g ./invoice.json -g ./w2.json

# Batch output to directory (one file per schema)
idp-cli discover -d ./invoice.pdf -d ./w2.pdf -o ./schemas/

# Batch output to single file (JSON array)
idp-cli discover -d ./invoice.pdf -d ./w2.pdf -o ./all-schemas.json

# Multi-section: discover specific page ranges from a single PDF
idp-cli discover -d ./lending_package.pdf \
    --page-range "1-2" --page-label "Cover Letter" \
    --page-range "3-5" --page-label "W2 Form" \
    --page-range "6-8" --page-label "Bank Statement" \
    -o ./schemas/

# Auto-detect sections then discover each
idp-cli discover -d ./lending_package.pdf --auto-detect -o ./schemas/

# Only detect section boundaries (no discovery)
idp-cli discover -d ./lending_package.pdf --auto-detect --detect-only

# Auto-detect with output to file
idp-cli discover -d ./lending_package.pdf --auto-detect --detect-only -o sections.json

# Stack mode (saves to config)
idp-cli discover --stack-name my-stack -d ./invoice.pdf --config-profile v2

# Override the Bedrock model (e.g. use Claude Opus instead of the configured default)
idp-cli discover -d ./invoice.pdf -g ./invoice.json \
    --model-id us.anthropic.claude-opus-4-6-v1
```

| Option | Description |
|--------|-------------|
| `--stack-name` | CloudFormation stack name (optional — omit for local mode) |
| `-d, --document` | Path to document file (required, repeatable for batch) |
| `-g, --ground-truth` | Path to JSON ground truth file(s) (repeatable, auto-matched by filename stem) |
| `--config-profile` (alias: `--config-version`) | Configuration profile to save to (stack mode only) |
| `-o, --output` | Output path: file (single/JSON array) or directory (one file per schema) |
| `--class-hint` | Hint for the document class name (e.g., "W2 Form"). The LLM will use this as `$id`. |
| `--page-range` | Page range to discover (e.g., "1-3"). Repeatable for multi-section. Requires PDF. |
| `--page-label` | Label for corresponding `--page-range` (e.g., "W2 Form"). Used as class name hint per range. Optional per range; a label with no range is refused. |
| `--auto-detect` | Auto-detect document section boundaries using AI, then discover each section. Cannot be combined with `--page-range`, `--page-label`, `-g` or `--class-hint`. |
| `--detect-only` | Only detect section boundaries. Requires `--auto-detect`. Prints boundaries without running discovery. |
| `--model-id` | Override the Bedrock model ID used for discovery (e.g., `us.anthropic.claude-opus-4-6-v1`). When omitted, the discovery model from the stack config (stack mode) or system defaults (local mode) is used. Applies to with-ground-truth, without-ground-truth, `--auto-detect`, and `--page-range` modes. |
| `--region` | AWS region |

**Combinations that are refused.** Discovery is paid work and its output is
written to disk and consumed as configuration, so a combination that cannot be
honoured exits non-zero before any Bedrock call rather than proceeding with part
of what was asked for:

| Given | Why it is refused |
|---|---|
| `--auto-detect` with `-g` / `--class-hint` | This mode infers one class per detected section and applies neither, so the run would cost the same and disregard them. Drop `--auto-detect` to discover the whole document, where both apply. |
| `--auto-detect` with `--page-range` | Both decide where the sections are, and there is no basis on which to prefer one. Give one. |
| `--detect-only` without `--auto-detect` | Without it, a full schema inference ran instead of boundary detection — a more expensive operation than the one asked for. |
| More `--page-label` than `--page-range` | Labels pair with ranges in order, so the extras had no range and their class-name hints were lost. Fewer labels than ranges is fine: a label is optional per range. Under `--auto-detect` the remedy is to drop the label, not to add a range — that mode names each section itself. |

**Filenames in directory mode.** When `-o` names a directory, each schema is
written as `<class id>.json`, where the class id is the schema's `$id` (falling
back to `x-aws-idp-document-type`, then to `unknown`) reduced to the character
set every consumer of a class id accepts — `[a-zA-Z0-9_-]`. Anything else becomes
a hyphen, runs of hyphens collapse to one, and leading and trailing hyphens are
trimmed, so a class id of `Bank Statement` is written as `Bank-Statement.json`
and one with no usable character in it at all as `unknown.json`. That is the same
rule the discovered class gets when it is saved to a configuration profile, so
the two agree on the name.

The class id is generated by the model from the content of the document, and a
filename is not the right place to trust it: without that reduction a class id
spelling a path would name a file somewhere else entirely. The directory each
file is written into is therefore checked to be the one you named, and the
command errors rather than writing outside it. A symlink *you* placed at a target
filename inside that directory is followed as usual — what is checked is the
directory, not the file.

The command prints the path it wrote to, and prints a note when a class id had to
be rewritten to produce it.

---

### `discover-multidoc`

Discover document classes from a collection of documents using embedding-based clustering and agentic analysis.

Unlike `discover` (which analyzes one document at a time), `discover-multidoc` analyzes a directory of mixed documents to automatically identify document types, cluster similar documents, and generate JSON Schemas for each discovered class.

**Requires:** `pip install -e "lib/idp_common_pkg[multi_document_discovery]"` (scikit-learn, scipy, numpy, strands-agents)

**Note:** Requires at least **2 documents per expected class**. Clusters with fewer than 2 documents are filtered as noise. For discovering schemas from individual documents, use [`discover`](#discover) instead.

**Usage:**
```bash
idp-cli discover-multidoc [OPTIONS]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--dir` | Directory containing documents to analyze (recursive scan) |
| `-d, --document` | Individual document files (repeatable: `-d doc1.pdf -d doc2.png`) |
| `--embedding-model` | Bedrock embedding model ID (default: `us.cohere.embed-v4:0`) |
| `--analysis-model` | Bedrock LLM for cluster analysis (default: `us.anthropic.claude-sonnet-4-6`) |
| `-o, --output` | Output directory for discovered JSON schemas |
| `--stack-name` | CloudFormation stack name (required for `--save-to-config`) |
| `--config-profile` (alias: `--config-version`) | Configuration profile to save schemas to |
| `--save-to-config` | Save discovered schemas to the stack's configuration |
| `--region` | AWS region |

**Examples:**

```bash
# Discover from a directory of documents
idp-cli discover-multidoc --dir ./samples/

# Discover with explicit files
idp-cli discover-multidoc -d doc1.pdf -d doc2.png -d doc3.jpg

# Save schemas to output directory
idp-cli discover-multidoc --dir ./samples/ -o ./schemas/

# Save to stack configuration
idp-cli discover-multidoc --dir ./samples/ --save-to-config \
    --stack-name IDP --config-profile v2

# Use custom models
idp-cli discover-multidoc --dir ./samples/ \
    --embedding-model us.amazon.titan-embed-image-v1 \
    --analysis-model us.anthropic.claude-sonnet-4-6
```

**Pipeline stages** (shown in Rich progress output):
1. **Document scan** — Finds PDF, PNG, JPG, TIFF files in the directory
2. **Embedding** — Generates image embeddings via Bedrock (Cohere Embed v4)
3. **Clustering** — KMeans + silhouette analysis to find optimal number of clusters
4. **Analysis** — Strands agent analyzes each cluster to identify the document class and generate a JSON Schema
5. **Reflection** — Agent generates a summary report of all discovered classes

**Output:** Results table showing cluster ID, classification, document count, field count, and status. Optionally writes individual JSON schema files and a reflection report.

---

### `config-sync-bda`

Synchronize IDP document class schemas with BDA (Bedrock Data Automation) blueprints.

**Usage:**
```bash
idp-cli config-sync-bda [OPTIONS]
```

**Options:**
- `--stack-name` (required): CloudFormation stack name
- `--direction`: Sync direction — `bidirectional` (default), `bda-to-idp`, `idp-to-bda`, or `cleanup-orphaned` (not a sync — see [below](#--direction-cleanup-orphaned))
- `--mode`: Sync mode — `replace` (default, full alignment) or `merge` (additive, don't delete). Not read by `cleanup-orphaned`
- `--config-profile` (alias: `--config-version`): Configuration profile to sync (default: active profile)
- `--force`: Skip the confirmation prompt. Only `cleanup-orphaned` prompts
- `--region`: AWS region (optional)

**Examples:**

```bash
# Bidirectional sync (default)
idp-cli config-sync-bda --stack-name my-stack

# Import BDA blueprints into IDP config
idp-cli config-sync-bda --stack-name my-stack --direction bda-to-idp

# Push IDP classes to BDA blueprints
idp-cli config-sync-bda --stack-name my-stack --direction idp-to-bda

# Merge mode (additive — don't remove existing items)
idp-cli config-sync-bda --stack-name my-stack --direction bda-to-idp --mode merge

# Sync specific config profile
idp-cli config-sync-bda --stack-name my-stack --config-profile v2
```

**What a failing sync reports.** A sync that cannot read the BDA project fails with an
error instead of proceeding. That matters most in `replace` mode, where the side being
read is the source of truth: an unreadable project is not an empty one, and treating it
as empty would remove every document class from the profile (`bda-to-idp`) or create a
second blueprint for every class (`idp-to-bda`). A transient error — a throttle, or a
missing permission — is therefore safe to retry rather than something to recover from.

A class is also reported failed when its blueprint was created but could not be
associated with the project: the blueprint exists, but BDA does not recognise that
document type until it is in the project's blueprint list.

**Properties BDA cannot represent are reported as warnings.** BDA supports neither
objects nested inside objects nor arrays whose items nest further, so those properties
are dropped from the blueprint and each one is named in the sync's warnings, with the
class it belongs to. A property whose value is not a schema object at all — `null`, or a
string where an object was meant — is reported the same way. Read the warnings on every
sync: a class can succeed with a whole line-items section missing from what it extracts.
To keep such a section, flatten the schema so the nested structure sits in a top-level
`$defs` definition referenced by `$ref`.

**A blueprint that could not be deleted is reported separately from the classes.** In
`replace` mode the sync removes blueprints the profile no longer describes. The order is
forced — BDA refuses to delete a blueprint a project still associates, so the project's
blueprint list is rewritten first and the deletes follow — which means a delete that
fails leaves a blueprint that is already out of the project. It is invisible to
everything that reads the project, it still counts against the account's blueprint
limit, and a name-prefix match can still pick it up. Those ARNs are printed beside the
result, and they do **not** count as failed classes: the classes may all have synced,
and the outstanding work is a cleanup rather than a re-sync. Remove them with
`--direction cleanup-orphaned`, described next.

#### `--direction cleanup-orphaned`

Not a sync. It deletes every BDA blueprint carrying the stack's name prefix that the
named configuration profile's classes do not account for. That is an **account-wide**
scan rather than a project-scoped one, which is exactly why it is the only thing that
can reach a blueprint a replace-mode sync disassociated but could not delete — such a
blueprint is invisible to every read that goes through the project.

```bash
idp-cli config-sync-bda --stack-name my-stack \
    --direction cleanup-orphaned --config-profile v2
```

⚠️ **The profile decides what survives, and the scope is the whole account.** Blueprints
belonging to a *different* profile of the same stack are orphans as far as this command
is concerned, so naming the wrong profile — or letting it fall back to the active one
when you meant another — deletes live blueprints. There is no dry run.

It prompts for confirmation and will not proceed on an empty answer; `--force` skips the
prompt, which is what a script wants. `--mode` is not read. The command reports how many
blueprints it deleted and exits non-zero if any deletion failed, naming the ARNs that
are still orphaned afterwards.

The same operation is available as `config.sync_bda(direction="cleanup_orphaned")` in
the SDK and as the `syncBdaIdp` API operation with direction `cleanup_orphaned`. The Web
UI has no control for it.

---

### `chat`

Interactive Agent Companion Chat from the terminal. Provides access to the full multi-agent orchestrator including Analytics, Error Analyzer, Code Intelligence, and any configured External MCP Agents.

The chat command runs the same orchestrator as the Web UI's Agent Companion Chat, but locally in your terminal — with real-time streaming and multi-turn conversation support.

Agents wrap their private reasoning in `<thinking>...</thinking>` and only the answer is printed. Because the response arrives as a stream of small pieces whose boundaries the service chooses, a reasoning block can be split across two of them; the terminal output is the same either way, and if a response ends mid-thought the incomplete reasoning is discarded rather than shown.

**Usage:**
```bash
idp-cli chat [OPTIONS]
```

**Options:**
- `--stack-name` (required): CloudFormation stack name
- `--region`: AWS region (optional)
- `--prompt`: Single-shot prompt — sends one message, prints the response, and exits. Useful for scripts and CI/CD.
- `--enable-code-intelligence`: Enable the Code Intelligence Agent (disabled by default because it uses external third-party services)

**Examples:**

```bash
# Interactive mode — multi-turn conversation
idp-cli chat --stack-name my-stack

# Single-shot mode — for scripts and automation
idp-cli chat --stack-name my-stack --prompt "What is the avg accuracy for the last test run?"

# With Code Intelligence enabled
idp-cli chat --stack-name my-stack --enable-code-intelligence

# Pipe output in scripts
idp-cli chat --stack-name my-stack --prompt "How many documents failed today?" 2>/dev/null
```

**Interactive session example:**
```
IDP Agent Chat
Stack: my-stack

✓ Ready  Agents: Analytics Agent · Error Analyzer Agent · Code Intelligence Agent
Type /quit to exit

You: What is the avg accuracy for test run Fake-W2-Tax-Forms-20260320?
⟶ Analytics Agent
The average accuracy for test run Fake-W2-Tax-Forms-20260320 is 0.867 (86.7%) across 95 documents.

You: Break that down by document type
⟶ Analytics Agent
...

You: /quit
Goodbye.
```

**SDK usage:**
```python
from idp_sdk import IDPClient

client = IDPClient(stack_name="my-stack")

# Single message
resp = client.chat.send_message("How many documents were processed today?")
print(resp.response)

# Multi-turn conversation
resp2 = client.chat.send_message("Break down by type", session_id=resp.session_id)
print(resp2.response)
```

**Prerequisites:**
- Requires `idp_common[agents]` to be installed: `pip install -e 'lib/idp_common_pkg[agents]'`
- Requires Amazon Bedrock model access (Claude or Nova models)
- Stack must be deployed with Agent Companion Chat resources (DynamoDB tables, Athena database)

---

## Troubleshooting

### Stack Not Found

**Error:** `Stack 'my-stack' is not in a valid state`

**Solution:**
```bash
# Verify stack exists
aws cloudformation describe-stacks --stack-name my-stack
```

### Permission Denied

**Error:** `Access Denied` when uploading files

**Solution:** Ensure AWS credentials have permissions for:
- S3: PutObject, GetObject on InputBucket/OutputBucket
- SQS: SendMessage on DocumentQueue
- Lambda: InvokeFunction on LookupFunction
- CloudFormation: DescribeStacks, ListStackResources

### Manifest Validation Failed

**Error:** `Duplicate filenames found`

**Solution:** Ensure unique filenames or use directory structure:
```csv
document_path
./clientA/invoice.pdf
./clientB/invoice.pdf
```

### Evaluation Not Running

**Issue:** Evaluation results missing even with baselines

**Checklist:**
1. Verify `baseline_source` column exists in manifest
2. Confirm baseline paths are correct and accessible
3. Check baseline directory has correct structure (`sections/1/result.json`)
4. Review CloudWatch logs for EvaluationFunction

### Monitoring Shows "UNKNOWN" Status

**Issue:** Cannot retrieve document status

**Solution:**
```bash
# Verify LookupFunction exists
aws lambda get-function --function-name <LookupFunctionName>

# Check CloudWatch logs
aws logs tail /aws/lambda/<LookupFunctionName> --follow
```

---

## Testing

Run the test suite:

```bash
cd lib/idp_cli_pkg
pytest
```

Run specific tests:

```bash
pytest tests/test_manifest_parser.py -v
```

---

## Support

For issues or questions:
- Check CloudWatch logs for Lambda functions
- Review AWS Console for resource status
- Open an issue on GitHub
