---
title: "Migrating from genai-idp-terraform"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Migrating from genai-idp-terraform

This page is for teams that deploy the accelerator with the separate
[`awslabs/genai-idp-terraform`](https://github.com/awslabs/genai-idp-terraform)
repository and want to move to the Terraform module this repository publishes with
every release (see [Terraform Deployment](./terraform.md)).

The main path is an **in-place migration**: a new deployment of this repository's
module takes over the old deployment's buckets, tables, KMS key and Cognito user pool
with Terraform `import` blocks, so documents, results, configuration, users and their
passwords stay where they are. Everything else (functions, queues, the state machine,
the web UI, the knowledge base) is created afresh by the new deployment and destroyed
with the old one. The `idp-cli terraform migrate-from-genai-idp-terraform` command
does the mapping and each step, one phase at a time, and every phase that changes
something is a dry run until you pass `--apply`.

A [side-by-side cutover](#fallback-side-by-side-cutover), which keeps nothing and
copies data across, is described at the end as the fallback.

## How much of this has been tested

The command has been run end to end against a live deployment of upstream tag
`v0.6.9-tf.0` (its `examples/unified-processor` configuration) by the repeatable
live test `make stacktest-terraform-migration`
(`scripts/sdlc/terraform_migration_test.py`), which is part of release validation.
That test deploys upstream, seeds it with two processed documents, two users with
passwords, a configuration edit, an evaluation baseline and a test set, runs every
phase below with `--apply`, checks that nothing kept was lost, and destroys both
deployments. It is still only that one upstream configuration, so rehearse the
migration in a non-production account first.

What the passing run measured:

- The adoption plan was `22 to import, 1558 to add, 21 to change, 0 to destroy`,
  with one refused change, the agent-jobs time-to-live rename the
  [hazards](#hazards) describe, accepted.
- Every object in the kept buckets and every item in the kept tables was still there
  and unchanged, apart from the changes the migration is meant to make: the two
  configuration counters `fix-config` removes, the profile `restore-config` put back,
  the schema, pricing and model-limit rows every deploy rewrites, and the attributes
  the new deployment's index backfill adds to tracking rows that lacked them.
- Both users signed in to the new deployment's app client with their old passwords,
  and each kept its per-user scope; the configuration edit was present; a new upload
  was processed once, by the new deployment.
- The old deployment's destroy plan touched no kept resource, and after the old
  deployment was destroyed every adopted bucket and table still stood and both users
  still signed in.

How long each step took in that run, end to end about 2 hours 45 minutes. Two of the
waits are DynamoDB's, not the command's, and a migration of a deployment whose tables
are older than an hour has only the second of them:

| Step | Minutes |
|---|---|
| Plan, retention check, freeze, configuration fix | under 1 |
| Turn the old agent-jobs time-to-live off ([hazards](#hazards)) | 15, waiting for DynamoDB to accept it: a table whose time-to-live was enabled less than about an hour ago refuses the change (only the test's freshly created deployment meets this) |
| Adopt the user pool | under 1 |
| Wait before `apply` | 61, because DynamoDB refuses the module's time-to-live setting until about an hour after the disable |
| `apply` (plan, check, apply) | 33 |
| Restore the configuration, verify | 3 |
| Detach, destroy plan, `check-destroy` | 1 |
| Destroy the old deployment | 8 |

The [resource table](#what-happens-to-each-resource) marks each kind of resource as
**measured** (adopted or recreated in a live run) or **inferred** (handled the same
way for the same reason, but not exercised). Other upstream example configurations
(`bda-processor`, `bedrock-llm-processor` and the rest) use the same resource names
for most resources, but have not been tried.

## Before you start

You need:

- Terraform 1.7 or later (or OpenTofu), and `idp-cli` from this repository (see
  [IDP CLI](./idp-cli.md#installation)).
- Credentials that can read and change both deployments' resources in the account,
  and access to the old deployment's Terraform directory and state. If Terraform
  should assume a role (`--terraform-assume-role-arn` on `plan`), the command's own
  AWS calls in every phase assume the same role, read back from the providers in the
  new deployment's `main.tf`, so they act in the account Terraform deploys to.
- A release of the module that adopts an existing user pool without replacing it.
  Cognito fixes a pool's username case-sensitivity when the pool is created, and an
  older module plans to replace a pool whose setting differs from its own, which
  would delete every user. The change that prevents this is in [pull request
  #1848](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/pull/1848),
  which ships in the first release after 0.6.14; no published release has it yet.
  With an older module the `apply` phase finds the replacement and refuses.
- A name for the new deployment that no CloudFormation stack uses (at most 25
  characters), and the old deployment's admin username.
- A maintenance window. From the freeze step on, the old deployment stops taking new
  documents, and nothing processes uploads until the new deployment is applied.

Take backups you can restore without either Terraform state: an on-demand DynamoDB
backup of each table in the [resource table](#what-happens-to-each-resource) that
is adopted, and a copy of anything in the adopted buckets you cannot afford to lose.
Export the old deployment's CloudWatch logs if you need them, because they are
destroyed with it.

Save the old deployment's state to a file, in its directory:

```bash
cd old-idp
terraform state pull > ../old-idp.tfstate
```

In the examples below the new deployment is called `my-idp`, the region is
`us-west-2`, the old deployment's directory is `./old-idp`, and the new one's root
configuration is written to `./idp-terraform/my-idp` (the default, as for
`idp-cli deploy --terraform`).

## The migration

### 1. Plan

```bash
idp-cli terraform migrate-from-genai-idp-terraform plan \
    --stack-name my-idp --region us-west-2 \
    --old-state old-idp.tfstate --admin-email admin@example.com
```

`plan` reads the old state and maps every resource in it to a verdict: adopted,
recreated, or kept but managed by neither deployment. It writes the new deployment's
`main.tf` and `terraform.tfvars` (as `idp-cli deploy --terraform` does), runs
`terraform init`, and then writes:

- `adopt.tf`, one `import` block per adopted resource;
- `genai-idp-terraform-migration/migration-plan.json`, which the later phases read;
- `genai-idp-terraform-migration/detach-addresses.txt`, the old-state addresses to
  remove from the old state at the end, and prints the exact `terraform state`
  commands for it (the state backup they write goes into
  `genai-idp-terraform-migration/`, readable by you only).

It changes nothing in AWS. It reads the old user pool, to find out whether a user
named exactly `--admin-email` exists there: if one does, it is adopted as the new
deployment's admin user, and otherwise the module creates one. Usernames in the
adopted pool are case-sensitive, so pass the admin's username with the exact case it
was created with.

It adds two stack parameters the old deployment needs: `ReportingBucketName`, naming
the old reporting bucket so the new deployment uses it instead of creating its own,
and `ExternalIdPEmailMutable=true` when the old pool's email attribute is mutable,
because a pool's attribute mutability cannot change after it is created. Pass any
other parameters you want with `--parameters`, as for `idp-cli deploy`; the phase
refuses a value that contradicts one it derived.

`plan` **refuses**, and writes no adoption files, when the old state holds a resource
that holds data and has no verdict, a resource the command does not handle yet (a
fine-tuning data bucket, or a Cognito hosted-UI domain, which would block the
module's own domain on the adopted pool), or a resource of a type it does not know to
be safe to destroy with the old deployment. The types it knows are every type the
upstream repository declares at tag `v0.6.9-tf.0`, except a VPC and its parts (the
new deployment may have been pointed at them) and `aws_cloudformation_stack` (its
custom resources run on delete). It also refuses a setting resource whose state does
not record which bucket, table, key or pool it belongs to. It names each one. Nothing
is skipped silently.

Some resources are kept rather than destroyed, so that destroying the old deployment
does not take something the new one uses: the KMS key's alias, settings of the
adopted user pool (UI customisation, resource servers, risk configuration), BDA
blueprints, and any `aws_lambda_invocation` whose `lifecycle_scope` is not
`CREATE_ONLY`, because destroying one of those invokes its function with a delete
action (upstream's BDA OCR project manager does exactly that). Each is detached from
the old state and left in the account, managed by neither deployment.

Read the printed table before going on. Each adopted resource shows its old and new
address and whether the live run measured it.

To back out: delete `adopt.tf` and the `genai-idp-terraform-migration` directory.

### 2. Check what the expiry rules would delete

```bash
idp-cli terraform migrate-from-genai-idp-terraform check-retention \
    --stack-name my-idp --region us-west-2
```

The module puts a lifecycle rule on every bucket it manages, and adopting a bucket
applies that rule to the objects already in it. With the default parameters the
input, output, test-set and discovery buckets expire objects after 365 days
(`DataRetentionInDays`), the working bucket after 30 days (`LogRetentionDays`), and
the access-log bucket after a fixed 180 days. The old deployment may have had no
such rule.

This phase reads every lifecycle configuration in the installed module, at any
module depth, and works out which bucket each one targets and the period of each
rule. Then, for each bucket the migration keeps (adopted, or named in
`ReportingBucketName`), it lists every object and finds the oldest, and for a
versioned bucket it lists every object version and finds the one that has been
noncurrent longest (dated by the version or delete marker that superseded it). It
fails for each bucket that holds an object, or a noncurrent version, older than the
shortest rule the module would put on it. For a rule set by a parameter, raise the
parameter: run `plan` again with, for example, `--parameters
DataRetentionInDays=1000`. For a fixed rule, copy the objects you need elsewhere
first, then pass `--accept-expiry <kind>` here and to `apply`.

It also fails when it cannot see what it is checking:

- a rule whose period is not a number or a variable it can follow (for example
  `local.x`, an `expiration` with a `date`, or a `dynamic` block), or an inline
  `lifecycle_rule` on a kept bucket. Find the period yourself, then pass
  `--accept-expiry <kind>` for that bucket;
- a lifecycle configuration whose `bucket` it cannot resolve to a bucket, or a module
  call whose source is not a local directory it can read. Either could put a rule on
  a kept bucket, and there is no override, because nothing names the bucket to accept.

The result says what it did not check: rule filters (each rule is treated as covering
the whole bucket, so a finding may be stricter than the rule),
`newer_noncurrent_versions`, storage-class transitions and incomplete multipart
uploads, and the bucket's current lifecycle configuration, which the module's
replaces.

It reads only. Listing a large bucket takes time and costs one request per 1,000
objects, or per 1,000 versions for a versioned bucket.

### 3. Freeze the old deployment

```bash
idp-cli terraform migrate-from-genai-idp-terraform freeze \
    --stack-name my-idp --region us-west-2 --apply --wait
```

Once the new deployment adopts the input bucket, both deployments would receive its
upload events and process every document twice. `freeze --apply` disables the old
deployment's EventBridge rule on the input bucket. It then scans the whole tracking
table. A document is finished when its status is COMPLETED, FAILED, ABORTED,
REJECTED, REDACTED_SUPERSEDED, QUARANTINED or MALWARE_SCAN_FAILED; a document in
HITL_IN_PROGRESS is waiting for a reviewer and is reported separately, because
whether the new deployment can complete a review the old one started has not been
verified, so finish or reject those reviews in the old web UI first. Every other
document is in flight. `--wait` polls until none is in flight or awaiting review, for
up to `--timeout` seconds (default 1800). A document you have decided to abandon can
be named with `--ignore-document <object key>` (repeatable, here and on `apply`); it
is listed but does not block. Without `--apply` the phase only reports the rule and
the documents. A document's status is read from its `doc#<key>` row with sort key
`none`; a tracking table holding `doc#` rows none of which has that sort key is
reported as a finding rather than as having no documents, because the check could not
read it.

When the old state has no EventBridge rule on the input bucket, `freeze` (and later
`apply`) fails, because the command then cannot tell what starts the old
deployment's workflows.

**From this step on, never run `terraform apply` in the old deployment's directory.**
Its state still records the rule as enabled, and an apply would turn it back on and
fight the new deployment over the resources they now share. The only Terraform
command the old deployment receives from here is the final `destroy`, unless you
back out.

The old deployment's input-bucket notification may also invoke its
knowledge-base ingestion function on upload. The new deployment's apply replaces the
bucket's notification configuration with its own, which removes that.

To back out: the phase prints the `aws events enable-rule` command that turns the
rule back on.

### 4. Fix the configuration table

```bash
idp-cli terraform migrate-from-genai-idp-terraform fix-config \
    --stack-name my-idp --region us-west-2 --apply
```

The old deployment writes `LatestRevision` and `PublishedRevision` counters on its
`Config#<profile>` rows, but never writes the revision bodies they count (its
configuration functions point at a bucket it never creates). On its first deploy the
new deployment reads those counters as a revision history that exists, and writes the
default configuration over yours. This phase backs up every `Config#` row to
`genai-idp-terraform-migration/configuration-table-backup-<time>.json` (readable by
you only), then removes the two counters from each row. The new deployment then
starts each profile's revision history from your existing configuration: its first
deploy records what each profile holds as that profile's first revision. For a
profile the module itself ships (`default`, at least), the same deploy then saves the
module's preset over it as the next revision, as every stack deployment does, so
after `apply` that profile runs the preset and your content is the revision before
it. Step 6a puts it back.

It refuses a table that already holds a revision index (`ConfigRevIndex#` rows):
that means this repository's code has already run against the table, so its counters
are real. Run this phase before the first apply, never after.

To back out: `fix-config --restore <backup file> --apply` writes the backed-up rows
back. Once the first apply has run, the new deployment keeps a revision history for
those profiles (`ConfigRevIndex#<profile>` rows), and writing the old rows back would
replace whatever has been saved since and put the old counters on rows they no longer
describe, so `--restore` is refused then. To get a profile's old content back after
the first apply, use `restore-config` (step 6a); `--restore-after-apply` overrides the
refusal when you mean to discard the new history, as when going back to the old
deployment.

### 5. Adopt the user pool

```bash
idp-cli terraform migrate-from-genai-idp-terraform adopt-user-pool \
    --stack-name my-idp --region us-west-2 --apply
```

An imported user pool's state records every detail Cognito reports about each schema
attribute, such as `string_attribute_constraints` with a minimum length of `0` on
`email`, while the module declares only the attributes themselves. The provider reads
that difference as removing a schema item, Cognito refuses it, and the first apply
fails with `cannot modify or remove schema items`. No change to the module can satisfy
both an imported pool and one the module creates, so this phase fixes the state once,
deliberately, instead:

1. It imports the pool with `terraform import` (a state-only operation), rather than
   with an `import` block, so nothing is applied yet.
2. It saves a plan, reads from it the schema the module declares, and pulls the new
   deployment's state.
3. It backs the state up to `genai-idp-terraform-migration/new-state-backup-<time>.tfstate`,
   rewrites the pool's `schema` in state to the module's form of each attribute the
   module also declares, and pushes it with `terraform state push`.

An attribute the pool has and the module does not declare is dropped from state, not
from the pool. An attribute only the module declares (such as `idp_groups`) is left out
of state, so the apply adds it, which Cognito allows. A difference Cognito cannot
apply at all (an attribute's data type, mutability or required flag) is refused
instead of hidden, naming the parameter to set where there is one.

Without `--apply` the phase prints the `terraform import` it would run and, once the
pool is imported, each schema change.

To back out: the phase prints `terraform state push -force <backup>` and
`terraform state rm` for the pool. Neither changes anything in AWS.

### 6. Review and apply the adoption

```bash
# Plan and check, applying nothing:
idp-cli terraform migrate-from-genai-idp-terraform apply \
    --stack-name my-idp --region us-west-2

# Then apply exactly the plan it checked:
idp-cli terraform migrate-from-genai-idp-terraform apply \
    --stack-name my-idp --region us-west-2 --apply
```

`apply` first checks the earlier phases again: no kept bucket would expire objects
(unless accepted with `--accept-expiry`), the old upload rule exists and is disabled,
nothing is in flight or awaiting review (but the documents named with
`--ignore-document`), and the configuration counters are gone. It then saves a plan
in the new deployment's directory, checks it, and with `--apply` applies exactly that
saved plan. The plan file is deleted afterwards, because it holds the variable values.

A good plan has these properties, and the phase refuses any plan without them:

- Every resource in `adopt.tf` appears as an import, with the import id the plan
  phase wrote, and its action is no change or an in-place update. In-place updates
  are expected: the module's key policy replaces the old one, the tracking table gains
  the module's second index (which builds on adoption), and the adopted buckets get
  the module's encryption, logging, lifecycle and notification settings.
- The user pool is already in state, and its update removes or modifies no existing
  schema item.
- No adopted resource, and no setting resource the plan creates or updates on a kept
  bucket, loses a protection. The phase compares each one's `before` and `after` and
  refuses, among others: point-in-time recovery or deletion protection turned off, a
  secondary index removed, a stream turned off or the time-to-live setting changed on
  a table; versioning suspended, the KMS key removed, a public-access block relaxed or
  `force_destroy` set on a bucket; deletion protection off, multi-factor
  authentication relaxed, a Lambda trigger or a schema attribute removed on the user
  pool; key rotation off, the key disabled or its deletion window shortened. The full
  list is `REFUSED_CHANGES` in `lib/idp_sdk/idp_sdk/_core/terraform_migrate.py`. An
  attribute one of these checks reads that the plan does not record is refused too.
  Each finding names the flag that accepts it, `--accept-change <address>:<rule>`, for
  a change you have reviewed and intend. Every other in-place change to those
  resources is allowed and printed under "In-place changes to review"; read it.
- **Nothing is destroyed, replaced or forgotten**: `0 to destroy`, and no resource
  removed from state by a `removed` block. An adoption has no reason to
  delete anything. Any replace or destroy, of an adopted resource above all, means the
  new deployment would delete data the old deployment owns, so stop and find out why
  rather than working around it. The usual causes are a module that predates the
  user-pool change described under [Before you start](#before-you-start), or a stack
  parameter that differs from what the adopted resource was created with.

Everything else in the plan is created: the new deployment's functions, queues,
state machine, web UI, app client, identity pool, knowledge base and so on.

The apply is not rolled back if it fails part way, as for any Terraform apply. Fix
the cause and run `apply` again; it re-plans from what was created.

After a successful apply, delete `adopt.tf` (keep a copy if you like). Later updates
of the new deployment are meant to use `idp-cli deploy --terraform` from the same
directory, as for any Terraform deployment. **This is unverified**: whether the first
such deploy after the schema rewrite in step 5 plans cleanly against the adopted user
pool has not been measured yet. Run it without applying first and read the plan.

### 6a. Restore the configuration profiles the first deploy replaced

```bash
idp-cli terraform migrate-from-genai-idp-terraform restore-config \
    --stack-name my-idp --region us-west-2 --apply
```

For each profile in the `fix-config` backup whose published revision was cut by the
first deploy (`createdBy: stack-deployment`) right after the revision recording what
the profile held before the migration, this restores that earlier revision as a new
revision. History is not rewritten: the preset stays in the history, and
`idp-cli config-revisions` shows both. A profile the deploy did not replace is left
alone and listed as such. So is a profile with any revision between the pre-migration
one and the published one that was not cut by `stack-deployment`: that is a change
someone made on purpose, which a restore would undo, so the phase names the revision,
who cut it, and the pre-migration revision to restore by hand if wanted. A profile
that has revisions but no pre-migration one (the first deploy could not record it) is
a finding naming the `fix-config` backup file, which is then the only copy of the old
content. Without `--apply` it lists what it would restore.

In the measured run the `default` profile was the one replaced, and a configuration
edit made on the old deployment was missing from the new one until this phase ran.

### 7. Verify the new deployment

Before you detach anything, check:

- Existing users sign in to the new web UI with their usual username and password,
  typed with the exact case of their username, and see what their role and per-user
  scope allow (see [Role-Based Access Control](./rbac.md)).
- `idp-cli config-list --stack-name my-idp` shows every configuration profile you
  had, with your content rather than the default configuration.
- A representative document uploaded to the input bucket is processed once, by the
  new deployment, and appears in the new web UI's document list. Documents processed
  before the migration also appear there, because the tracking table was adopted.
- Processing results match what the old deployment produced for the same documents.
  An [evaluation](./evaluation.md) against your baselines is the most reliable check.
- The new deployment's alerts subscription is confirmed (see
  [Who receives the alerts](./monitoring.md#who-receives-the-alerts)).
- The knowledge base, if you use it, is re-synced from the adopted output bucket. It
  is a new knowledge base on S3 Vectors, so it starts empty; start an ingestion job
  on its data source to index existing documents. This step is unverified.

### 8. Detach the adopted resources from the old state

```bash
idp-cli terraform migrate-from-genai-idp-terraform detach \
    --stack-name my-idp --region us-west-2 --old-terraform-dir ./old-idp --apply
```

Both states now record the adopted resources, so destroying the old deployment would
delete them. This phase removes them from the old state: the adopted resources, the
resources kept but unmanaged (such as the BDA project), and the settings resources of
each kept bucket and table (public-access blocks, encryption, CORS, notifications,
lifecycle rules, bucket policies), because destroying one of those strips that setting
from the bucket even though the bucket stays. It refuses until the new deployment's
state holds every adopted resource, and refuses an old directory whose state is not
the one `plan` read (its lineage differs) or holds none of the addresses to detach
(either the detach already ran, or it is the wrong directory). With `--apply` it saves
the old state to
`genai-idp-terraform-migration/old-state-backup-before-detach-<time>.tfstate` first,
then runs `terraform state rm` in the old directory.

To back out: `terraform state push -force <backup>` in the old deployment's directory.

### 9. Destroy the old deployment

Check the old deployment's destroy plan before you run it:

```bash
cd old-idp
terraform plan -destroy -out=destroy.tfplan
terraform show -json destroy.tfplan > ../destroy-plan.json
cd ..
idp-cli terraform migrate-from-genai-idp-terraform check-destroy \
    --stack-name my-idp --region us-west-2 --destroy-plan destroy-plan.json
```

`check-destroy` first refuses anything that is not the old deployment's destroy plan:
a file without `format_version` and a non-empty `resource_changes` (a state file, an
empty object), a plan Terraform marked as errored, a plan with any change other than
a delete, and a plan none of whose deletions is an address `plan` recorded for the old
deployment. It then fails if the plan deletes anything the migration kept: an address
that was to be detached (the detach did not run), an adopted or kept bucket, table,
key, user pool, or BDA project, a setting on one of those, or a user or group
membership in the adopted pool. It also fails on a deletion of an address `plan`
never saw, because nothing decided whether that may be destroyed; run `plan` again.
The old deployment's own app client, identity pool and WAF association are expected
in the plan. When it passes, apply the saved destroy plan in the old directory
(`terraform apply destroy.tfplan`).

The upstream repository documents how its resources are deleted, including deletion
protection on its Cognito resources. The adopted pool is no longer in the old state,
so its deletion protection does not matter here. This step cannot be undone, which
is why the backups under [Before you start](#before-you-start) matter.

## What happens to each resource

**Adopt** means an `import` block (or, for the user pool, `terraform import`) puts
the existing resource under the new deployment. **Recreate** means the new deployment
creates its own and the old one is destroyed with the old deployment. **Keep
unmanaged** means it stays in the account, managed by neither.

| Old resource | Verdict | New address (under `module.idp`) | Evidence |
|---|---|---|---|
| Input, output, working buckets | Adopt | `aws_s3_bucket.input_bucket`, `output_bucket`, `working_bucket` | measured |
| Evaluation baseline bucket | Adopt | `aws_s3_bucket.evaluation_baseline_bucket[0]` | measured |
| Test-set bucket | Adopt | `aws_s3_bucket.test_set_bucket` | measured |
| Discovery bucket | Adopt | `aws_s3_bucket.discovery_bucket` | measured |
| Access-log bucket | Adopt (fixed 180-day expiry) | `aws_s3_bucket.logging_bucket` | measured |
| Reporting bucket | Kept by naming it in `ReportingBucketName` | none (not imported) | measured |
| KMS key | Adopt; the module's key policy replaces the old one | `awscc_kms_key.customer_managed_encryption_key` | measured |
| KMS key alias | Keep unmanaged, still pointing at the adopted key | none | inferred |
| Tracking table | Adopt; the module's second index builds on adoption | `aws_dynamodb_table.tracking_table` | measured |
| Configuration table | Adopt after `fix-config` | `aws_dynamodb_table.configuration_table` | measured |
| Users table | Adopt | `aws_dynamodb_table.users_table` | measured |
| Agent chat sessions table | Adopt | `aws_dynamodb_table.chat_sessions_table` | measured |
| Agent chat messages table | Adopt | `aws_dynamodb_table.chat_messages_table` | measured |
| Chat memory table | Adopt | `aws_dynamodb_table.id_helper_chat_memory_table` | measured |
| Document-chat sessions table | Adopt | `module.apiresolverstack.aws_dynamodb_table.chat_document_sessions_table` | measured |
| Agent jobs table | Adopt (see the time-to-live note below) | `aws_dynamodb_table.agent_table` | measured |
| Cognito user pool | Adopt with `adopt-user-pool` | `aws_cognito_user_pool.user_pool` | measured |
| Admin, Author, Reviewer, Viewer groups | Adopt | `awscc_cognito_user_pool_group.<role>_group` | measured |
| Admin user and its Admin membership | Adopt, when the username is exactly `--admin-email` | `aws_cognito_user.admin_user`, `aws_cognito_user_in_group.admin_user_to_group_attachment` | measured |
| Other users and groups in the pool | Keep in the pool, unmanaged | none | measured |
| Discovery tracking table | Recreate (its key schema differs) | | measured |
| Concurrency table | Recreate | | measured |
| Queues | Recreate, after `freeze` drains them | | measured |
| Web UI bucket, app client, identity pool, settings parameters | Recreate | | measured |
| Knowledge base (OpenSearch Serverless collection and index) | Recreate on S3 Vectors; re-sync from the output bucket | | measured |
| Reporting Glue database and tables | Recreate (catalog metadata only) | | measured |
| Installed-features table | Recreate; features installed on the old deployment do not carry over | | measured |
| Test-sets table | Recreate; test-set definitions do not carry over, their documents stay in the adopted bucket | | measured |
| BDA project | Keep unmanaged; the `bda` configuration row names it | none | measured |
| Fine-tuning data bucket, hosted-UI domain | Not handled; `plan` refuses | | |

Functions, roles, policies, log groups, the state machine, event rules and the other
resources without data are destroyed with the old deployment and recreated by the new
one.

## Hazards

- **Usernames are case-sensitive.** The old pool was created with case-sensitive
  usernames, and Cognito cannot change that, so the adopted pool keeps it: each user
  signs in with the exact case of their username. The same applies to
  `--admin-email`.
- **Expiry rules apply to old objects.** See [step 2](#2-check-what-the-expiry-rules-would-delete).
- **Both deployments would process uploads** once the input bucket is shared, unless
  the old upload rule is off and in-flight documents have finished. See
  [step 3](#3-freeze-the-old-deployment).
- **The old deployment must never be applied again** after the freeze, and after the
  detach it may only be destroyed. An apply would re-enable its upload rule and
  rewrite settings on resources the new deployment now owns.
- **Agent jobs keep their old time-to-live attribute.** The old agent jobs table
  expires rows on `expiresAfter`; the module sets `ExpiresAfter`, so rows written
  before the migration no longer expire. `apply` refuses that time-to-live change
  until you accept it with `--accept-change
  module.idp.aws_dynamodb_table.agent_table:ttl-changed`. DynamoDB cannot move a
  table's time-to-live from one attribute to another while it is on, so `apply` also
  refuses until the old setting is off, and prints the command: `aws dynamodb
  update-time-to-live --table-name <agent jobs table> --time-to-live-specification
  Enabled=false,AttributeName=expiresAfter`. Then wait **at least an hour** before
  `apply`: turning it off can take up to an hour, and DynamoDB also refuses a second
  time-to-live change on a table within about an hour of the first ("Time to live has
  been modified multiple times within a fixed interval"), even once
  `aws dynamodb describe-time-to-live` reports `DISABLED`. `apply` can check the
  status but not that interval, so an `apply` run too soon fails part way on this
  table; run it again later. Disable it before the maintenance window, since nothing
  else depends on it: the old deployment's agent jobs simply stop expiring early.
- **Some history does not carry over**: discovery jobs, test-set definitions,
  installed features, and the old knowledge base's index (re-sync it).

## Backing out

Until step 6 nothing the old deployment uses has changed, apart from the disabled
upload rule and the removed configuration counters, and each phase prints how to undo
its own change.

After step 6 the adopted resources carry the new deployment's settings (key policy,
bucket settings, the tracking table's second index, the pool's new attribute, app
client and domain), and both states record them. To return to the old deployment
before step 8: remove the adopted addresses from the new deployment's state with
`terraform state rm`, so that destroying it leaves them alone; destroy the new
deployment; restore the configuration rows with `fix-config --restore <backup file>
--restore-after-apply --apply`; then run
`terraform apply` in the old deployment's directory, which re-enables the upload rule
and puts back its settings. This route has not been tested, and the schema attribute
the new deployment added to the pool stays, because Cognito cannot remove one.

After step 8, push the old-state backup back first (see step 8). After step 9 there
is nothing to go back to.

## Fallback: side-by-side cutover

If the in-place migration does not fit (an old deployment from another upstream
example the command refuses, for example), deploy the new module beside the old one
and move data and users across. This keeps nothing in place, and every step can be
checked and undone until the last one.

1. **Deploy the new module under a new name**, following [Terraform
   Deployment](./terraform.md), with a `stack_name` no resource of the old deployment
   uses, and stack parameters that match the old deployment's behaviour (processing
   mode, models, log retention, VPC settings, permissions boundary). The module's
   `variables.tf` describes each one. Keep its state in its own backend key.
2. **Copy the configuration.** Upload each configuration profile with
   `idp-cli config-upload --stack-name <new-name> --config-file <file>
   --config-profile <profile>`, validating each file with `idp-cli config-validate`
   first. `idp-cli` cannot read the old deployment's configuration, because it has no
   CloudFormation stack; start from the old deployment's configuration file and
   export later web UI changes from its web UI or configuration table.
3. **Move data.** Copy pending documents, baselines and test sets into the new
   buckets with `aws s3 sync`. Copied results do not appear in the new web UI's
   document list, because the tracking table does not move with them; reprocess the
   documents you need there.
4. **Recreate users.** Passwords cannot be exported from Cognito, so recreate users
   (each gets a temporary password), import them with a Cognito user import job (each
   resets their password at first sign-in), or configure the same external identity
   provider (see [External Identity Provider](./external-idp.md)). Recreate group
   memberships and per-user scopes either way.
5. **Switch over** when the new deployment has processed a representative set of
   documents correctly: point uploaders and integrations at the new input bucket, give
   users the new web UI address, and let the old deployment finish its in-flight
   documents.
6. **Destroy the old deployment** with `terraform destroy` once nothing writes to it,
   after backing up any bucket or table you might still need.

## Related documentation

- [Terraform Deployment](./terraform.md)
- [IDP CLI: `terraform migrate-from-genai-idp-terraform`](./idp-cli.md#terraform-migrate-from-genai-idp-terraform)
- [IDP SDK: Terraform migration](./idp-sdk.md#terraform-migration)
- [Configuration Profiles](./configuration-profiles.md)
- [Role-Based Access Control](./rbac.md)
