---
title: "Migrating from genai-idp-terraform"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Migrating from genai-idp-terraform

This page is for teams that deploy the accelerator with the separate
[`awslabs/genai-idp-terraform`](https://github.com/awslabs/genai-idp-terraform)
repository and want to move to the Terraform module this repository publishes with
every release (see [Terraform Deployment](./terraform.md)). It explains why the move is
a side-by-side cutover rather than an in-place change, lays out the cutover steps, and
lists what to check before you remove the old deployment.

The procedure below has not been run end to end. It is assembled from how the two
implementations are built, and the steps that depend on details of the old repository
or on your own setup are marked as unverified. Rehearse it in a non-production account
first.

## Why there is no in-place state migration

The two are different implementations of the same solution, not two versions of one
module. `genai-idp-terraform` is written by hand as a set of Terraform modules
(processor, processing environment, web UI, user identity and others) that you compose
yourself, and it takes some inputs, such as the input, output and working buckets, as
resources you create. The module this repository publishes is generated from the
CloudFormation templates, has one root module with a child module per nested stack,
and creates every resource itself.

As a result, the same logical resource usually has a different Terraform resource
type (some resources are managed through `hashicorp/aws` in one and through
`hashicorp/awscc` in the other), a different address in state, and often a different
physical name. Moving state with `terraform state mv` or `moved` blocks needs a
one-to-one correspondence of addresses and types, and there is none to rely on here.
Importing the old resources into the new module would mean matching hundreds of
resources by hand, and a single mismatch could lead Terraform to replace or delete a
resource holding data. Deploying the new module beside the old one and moving data and
users across is slower but can be checked at every step and undone until the last one.

## The cutover

### 1. Deploy the new module under a new name

Deploy the new module following [Terraform Deployment](./terraform.md), with a
`stack_name` that differs from anything the old deployment uses, so no resource name
collides. Set the stack parameters to match the old deployment's behaviour: the
processing mode, models, log retention, VPC settings, permissions boundary and so on.
The two implementations name their settings differently, so translate each old input
to the corresponding parameter of the main template; the module's `variables.tf`
describes each one. Keep the new deployment's state in its own backend key.

Leave the old deployment running and receiving documents until step 5.

### 2. Copy the configuration and configuration profiles

The new deployment starts with the built-in configuration of the preset you chose. To
carry over your processing configuration, upload it to each configuration profile you
use with `idp-cli config-upload`:

```bash
idp-cli config-upload --stack-name <new-name> \
    --config-file ./config.yaml --config-profile default
idp-cli config-upload --stack-name <new-name> \
    --config-file ./lending.yaml --config-profile lending
```

Where the YAML comes from depends on how you managed the old deployment, and this part
is unverified. If you deployed it from a configuration file (its `config_file_path`),
start from that file, and add any changes made later in the web UI. `idp-cli` finds a
deployment by its CloudFormation stack name, and the old deployment has no
CloudFormation stack, so `idp-cli config-download` cannot read from it directly; export
each profile from the old deployment's web UI or configuration table instead. Validate
each file with `idp-cli config-validate` before uploading, because the configuration
format may differ between the old repository's pinned release and the new module's
release. See [Configuration Profiles](./configuration-profiles.md) for how profiles and
revisions work. Revision history does not carry over; the new deployment starts its
own history at the first upload.

### 3. Move or re-point data in buckets

The new deployment creates its own buckets. Decide, per bucket, whether its contents
need to move:

- Documents waiting to be processed, and inputs you will want to reprocess, can be
  copied to the new input bucket with `aws s3 sync`, or simply uploaded there from the
  source once you switch over.
- Processing results in the old output bucket can stay where they are if you only need
  them for reference. Copy them with `aws s3 sync` if the new web UI or reporting
  should show them; this is unverified, since the document tracking table that the web
  UI reads does not move with the objects, so copied results will not appear in the
  document list without reprocessing.
- Evaluation baselines and test sets can be copied into the new evaluation baseline
  and test set buckets.

Find the new bucket names in the module's outputs (`terraform output`, or
`module.idp.S3InputBucketName` and its siblings). If upstream systems or bucket
notifications write into buckets you own and passed to the old deployment, re-point
them at the new input bucket in step 5 rather than now, so documents are not processed
twice.

### 4. Recreate or import users

The new deployment has its own Cognito user pool, and passwords cannot be exported
from Cognito. There are three options:

- Recreate the users in the new web UI's user management, or with the Cognito API.
  Each user receives a temporary password.
- Import them with a Cognito user import job from a CSV of the old pool's users.
  Imported users must reset their password at first sign-in.
- If you sign in through an external identity provider, configure the same provider
  on the new deployment (see [External Identity Provider](./external-idp.md)), and
  users keep signing in the way they do now. Group membership still has to be set up
  on the new pool.

In each case, recreate group memberships and any per-user scopes, since they decide
what each user may see and do (see [Role-Based Access Control](./rbac.md)).

### 5. Switch over

When the new deployment has processed a representative set of documents correctly
under the copied configuration, move the traffic: point uploaders, integrations and
bucket notifications at the new input bucket, and give users the new web UI address.
Then let the old deployment finish any documents already in flight before you go on.

### 6. Destroy the old deployment

Run `terraform destroy` in the old deployment's configuration only after you have
checked everything in the next section. The old repository's own documentation
describes how its resources are deleted, including deletion protection on its Cognito
resources, which is on by default there and has to be turned off before destroy can
remove the user pool. This step is unverified here. Back up any bucket or table you
might still need before you destroy, because a destroy cannot be undone.

## What to check before destroying the old deployment

- The new deployment processes your document types with the same results. Running the
  same documents through both and comparing, or running an evaluation against your
  baselines (see [Evaluation](./evaluation.md)), is the most reliable check.
- `idp-cli config-list --stack-name <new-name>` shows every profile you expect, and
  each one's content matches what the old deployment used.
- Every user who needs access can sign in, with the right groups.
- Nothing still writes to the old input bucket. Check its recent objects, or its
  request metrics.
- The new deployment's alerts subscription is confirmed (see
  [Who receives the alerts](./monitoring.md#who-receives-the-alerts)), and any
  dashboards or alarms you built on the old deployment's names have been re-pointed.
- Data you decided to keep from the old buckets and tables has been copied or backed
  up.
