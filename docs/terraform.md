---
title: "Terraform Deployment (Experimental)"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Terraform Deployment (Experimental)

The accelerator can be deployed with Terraform or OpenTofu as well as with
CloudFormation. This page explains where the Terraform module comes from, how to
deploy, change, upgrade and delete it, and how a Terraform deployment differs from a
CloudFormation one. Terraform support is experimental: the module has been deployed
end to end and has passed the integration suite, but it has had far less use than the
CloudFormation templates, and some features are not yet available for it. Those are
listed under [Differences from CloudFormation and current limitations](#differences-from-cloudformation-and-current-limitations).

If you deploy the accelerator today with the separate `awslabs/genai-idp-terraform`
repository, read [Migrating from genai-idp-terraform](./terraform-migration.md) as
well. That repository is a different implementation, so its state cannot simply be
moved onto this module; a new deployment of the module adopts its buckets, tables,
KMS key and user pool instead, with `idp-cli terraform migrate-from-genai-idp-terraform`.

Demo video (about 3.5 minutes): deploying from the published module and from a local
clone, updating, and planning a migration from `genai-idp-terraform`, all with
`idp-cli`.

https://github.com/user-attachments/assets/e207110a-3301-4242-9f41-4edc8dd7ade9

## What it is

The Terraform module is generated from the accelerator's CloudFormation templates,
the same templates the [Deployment Guide](./deployment.md) deploys. A translator in
this repository reads the published main template and its nested templates and writes
a Terraform module tree: a root module, plus one child module for each nested stack.
A release publish (`publish.py`) generates it from the templates it has just
published and uploads it beside them, unless run with `--no-terraform`, so a module
and the CloudFormation templates of the same version deploy the same resources with
the same settings. `idp-cli publish` does the same when given `--terraform`.

The generated module uses three providers:

- `hashicorp/aws`, version 6, for most resources;
- `hashicorp/awscc`, version 1, which manages resources through the AWS Cloud Control
  API, for the resource types that have no `hashicorp/aws` mapping in the translator;
- `hashicorp/time`, for the short waits that let IAM changes propagate before they are
  used.

The exact version constraints are in the module's `versions.tf`. The module requires
Terraform 1.7 or later, or OpenTofu 1.7 or later.

CloudFormation custom resources, which run a Lambda function during a stack operation,
are reproduced by a small bridge Lambda function that the module creates. It calls each
handler the way CloudFormation would and passes the handler's answer back to Terraform.
You do not interact with it, but you will see it among the deployment's resources.

## Prerequisites

You need the following before you deploy:

- An AWS account and credentials with administrator-level permissions, as for a
  CloudFormation deployment (see the
  [administrator access requirements](./deployment.md#administrator-access-requirements)).
  The `awscc` provider works through the Cloud Control API, so the credentials also
  need its actions (`cloudformation:CreateResource`, `GetResource`, `UpdateResource`,
  `DeleteResource`, `ListResources` and `GetResourceRequestStatus`) in addition to the
  permissions of the underlying services. The module also creates one CloudFormation
  stack (the label stack described below), which needs `cloudformation:CreateStack`
  and its related actions unless you turn the label off. When the deployment uses a
  customer-managed KMS key, the credentials also need `kms:Decrypt` and
  `kms:GenerateDataKey` on that key: the label stack's template is stored in the
  custom-resource bridge's bucket, which is encrypted with the key, and CloudFormation
  reads it with the caller's credentials. A deploying role that is
  scoped rather than administrator-level is described under
  [Deploying with a scoped role](#deploying-with-a-scoped-role).
- Terraform 1.7 or later, or OpenTofu 1.7 or later, on your path.
- Access to the Amazon Bedrock models the solution uses, as listed in the
  [Deployment Guide](./deployment.md).
- Read access to the S3 bucket the module was published to. The module refers to the
  Lambda code and other artifacts that the CloudFormation publish uploaded beside it,
  exactly as the CloudFormation template does.
- For the `idp-cli` route, `idp-cli` installed from a checkout of this repository, as
  described in [IDP CLI](./idp-cli.md#installation).

### Deploying with a scoped role

A Terraform deployment has no CloudFormation service role: every call is made with
the credentials Terraform runs with, so those credentials need everything a
CloudFormation service role for the solution needs. A role modelled on the example in
[`iam-roles/cloudformation-management/`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/tree/develop/iam-roles/cloudformation-management)
needs the following:

- The service-wide grants that role holds for the services the solution uses. The
  `aws` provider reads back more of each resource than CloudFormation does (every
  configuration of a bucket, for example), and a grant narrowed to the actions
  CloudFormation calls refuses those reads.
- The Cloud Control actions listed above, which are in the `cloudformation:`
  namespace and so are covered by that role's `cloudformation:*`.
- IAM writes on the roles the module creates. Every role is named after the
  deployment, as CloudFormation names an unnamed role: `<stack>-<LogicalId>-<hash>`,
  cut to IAM's 64 characters, and `<stack>-CfnBridge-<hash>` for the
  custom-resource bridge's role. A role CloudFormation retains also ends in its
  creation time, so deploying the same stack name again after a delete does not
  collide with the role the delete kept. A grant of `iam:CreateRole`, `iam:PassRole` and the
  role-policy actions on `role/<stack prefix>*` therefore covers them, provided the
  stack name starts with that prefix.
- If the role allows creating roles only with a given permissions boundary (the
  example role's `iam:PermissionsBoundary` condition), pass that boundary as
  `PermissionsBoundaryArn`. The module applies it to every role it creates, the
  bridge's role included, the way the template's `HasPermissionsBoundary` condition
  does.

A complete deployment with such a role has not been run; the list above follows
from what the module creates, not from a measured run.

Plan where Terraform state will live before the first apply. The state holds every
setting you pass, including sensitive ones, and some of the module's outputs are
credentials (the MCP client secrets, for example). A remote backend with encryption
and locking, such as the `s3` backend, is the usual choice for a deployment you intend
to keep.

## Getting the module

Each release that includes the Terraform module publishes it as a zip archive in the
same S3 bucket and under the same prefix as that release's CloudFormation templates.
For a release version `<version>`, the files are:

| Object | Content |
|---|---|
| `<prefix>/<version>/terraform/idp-terraform-<version>.zip` | the module tree |
| `<prefix>/<version>/terraform/idp-terraform-<version>.zip.sha256` | the SHA-256 checksum of the archive |
| `<prefix>/terraform/idp-terraform-latest.json` | `{"version", "moduleUrl", "sha256"}` for the newest published module |

The headless and GovCloud template variants, when a publish produces them, are
published as modules of their own beside the standard one, with the variant in each
name: `<prefix>/<version>/terraform/idp-terraform-<variant>-<version>.zip`, its
`.zip.sha256`, and `<prefix>/terraform/idp-terraform-<variant>-latest.json`, where
`<variant>` is `headless` or `govcloud`. The headless module has no web UI, Cognito,
agents or knowledge base, and so no `admin_email` variable; the GovCloud module keeps
the UI, has no CloudFront resources, and serves the UI through API Gateway.

The module source URL is the HTTPS S3 URL of the zip, for example
`https://s3.us-west-2.amazonaws.com/<bucket>/<prefix>/<version>/terraform/idp-terraform-<version>.zip`.
Terraform accepts an HTTPS URL ending in `.zip` as a module source, downloads it at
`terraform init` and extracts it. The public release is published to the same bucket
and prefix as the Launch Stack templates of the
[Deployment Guide](./deployment.md#option-1-one-click-cloudformation-console-deployment-recommended-for-first-time-users),
so for a release `<version>` (for example `0.6.15`) the module source in each Region
is:

| Region | Module source |
|---|---|
| us-west-2 | `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/<version>/terraform/idp-terraform-<version>.zip` |
| us-east-1 | `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/<version>/terraform/idp-terraform-<version>.zip` |
| eu-central-1 | `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/<version>/terraform/idp-terraform-<version>.zip` |
| ca-central-1 | `https://s3.ca-central-1.amazonaws.com/aws-bigdata-blog-replica-ca-central-1/artifacts/genai-idp/<version>/terraform/idp-terraform-<version>.zip` |

The newest module's URL in a Region is the `moduleUrl` of the pointer beside it,
`.../artifacts/genai-idp/terraform/idp-terraform-latest.json` in the same bucket. If
that pointer is not present in your Region's bucket, no Terraform module has been
published there yet; you can build and publish one yourself with
`idp-cli publish --terraform` (see [IDP CLI](./idp-cli.md#publish)).

You do not need a checkout of this repository to deploy a published module from your
own root configuration. The module declares no provider blocks of its own, so it uses
the `aws` and `awscc` providers your root configuration sets up (and the `time`
provider, which needs no settings), as the example under
[Your own root configuration](#your-own-root-configuration) shows.

The published zip is the only form the module is distributed in. It has no Terraform
Registry entry, and it cannot be used as a `git::` source, because the module is
generated from the templates when a release is published and is not committed to
the repository. Serving the zip from a private module registry has not been tested.

A plain HTTPS module source is fetched without AWS credentials, so it works only for
an object that is publicly readable. For a module you published to a private bucket,
prefix the URL with `s3::` and use the dash form of the S3 host
(`s3::https://s3-<region>.amazonaws.com/<bucket>/<key>`), which makes Terraform fetch
it with your AWS credentials. Terraform's S3 getter reads the dotted form
(`s3.<region>.amazonaws.com`) as a bucket named `s3`; `idp-cli deploy --terraform`
writes the dash form for you.

### Verifying the archive

Terraform does not check the checksum of a module archive it downloads. If you want
to verify the module, download the archive and its checksum yourself and compare them:

```bash
url="https://s3.<region>.amazonaws.com/<bucket>/<prefix>/<version>/terraform/idp-terraform-<version>.zip"
curl -fsSLO "$url"
curl -fsSLO "$url.sha256"
sha256sum "idp-terraform-<version>.zip"   # on macOS: shasum -a 256
cat "idp-terraform-<version>.zip.sha256"
```

The two hexadecimal digests must be identical. The `sha256` field of
`idp-terraform-latest.json` carries the same value for the newest module.

Verifying a file you downloaded says nothing about the copy `terraform init` downloads
later. To deploy exactly the bytes you verified, extract the archive into a directory
in your configuration and use that directory as the module source:

```bash
mkdir -p modules/idp-<version>
unzip -q "idp-terraform-<version>.zip" -d modules/idp-<version>
```

```hcl
module "idp" {
  source = "./modules/idp-<version>"
  # ...
}
```

## Deploying

There are two ways to deploy. Writing your own root configuration gives you full
control over the backend, provider settings and how the deployment fits into the rest
of your Terraform. `idp-cli deploy --terraform` writes a root configuration for you
and applies it, which is quicker for a trial or a test deployment.

### Your own root configuration

The module has two required variables, `stack_name` and `admin_email`. Every other
variable has a default. A complete minimal configuration is:

```hcl
terraform {
  required_version = ">= 1.7.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.72.0, < 7.0.0"
    }
    awscc = {
      source  = "hashicorp/awscc"
      version = "< 2.0.0"
    }
  }

  # Recommended for any deployment you keep. Replace with your own backend.
  # use_lockfile needs Terraform or OpenTofu 1.10 or later; on 1.7 to 1.9,
  # replace it with dynamodb_table = "<lock table>".
  # backend "s3" {
  #   bucket       = "<state bucket>"
  #   key          = "idp/terraform.tfstate"
  #   region       = "us-west-2"
  #   use_lockfile = true
  # }
}

# Both providers must point at the same Region: the module deploys into one Region.
provider "aws" {
  region = "us-west-2"
}

provider "awscc" {
  region = "us-west-2"
}

module "idp" {
  source = "https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/<version>/terraform/idp-terraform-<version>.zip"

  stack_name  = "idp-tf"
  admin_email = "admin@example.com"
}

output "web_ui_url" {
  value = module.idp.ApplicationWebURL
}
```

Then run:

```bash
terraform init
terraform plan -out idp.tfplan
terraform apply idp.tfplan
```

The module's version constraints on the providers apply in addition to yours, so the
constraints above only need to be compatible with them. The S3 backend's
`use_lockfile` setting is newer than the module's Terraform floor: it needs Terraform
or OpenTofu 1.10 or later, and an older release refuses the backend block. On 1.7 to
1.9, lock the state with a DynamoDB table (`dynamodb_table`) instead.

The module's outputs keep the CloudFormation output names (`module.idp.ApplicationWebURL`,
`module.idp.S3InputBucketName`, `module.idp.StateMachineArn`, and so on); its
`outputs.tf` lists them all.

#### Tagging the deployment

The module has a `tags` variable, a `map(string)` that is empty by default. It plays
the role of CloudFormation stack tags. Set it on the module and give the same map to
the `aws` provider's `default_tags`:

```hcl
locals {
  idp_tags = { CostCenter = "42", Owner = "document-team" }
}

provider "aws" {
  region = "us-west-2"
  default_tags {
    tags = local.idp_tags
  }
}

module "idp" {
  # ...
  tags = local.idp_tags
}
```

Both halves are needed, because `default_tags` reaches only the `aws` resources. The
`hashicorp/awscc` provider has no `default_tags`, so a tag set there does not reach any
`awscc` resource: the alarms, queues, SSM parameters, state machines, the REST API and
its stage, the KMS key and the others the module creates through Cloud Control. The
module merges `tags` into each `awscc` resource that accepts tags instead, in that
resource's own tag shape. Which resources those are is read from the `awscc` provider
schema when the module is generated, so a resource type gains the merge when the
provider adds tags to it.

A tag key a template sets on a resource itself, such as `Name` on the Lambda security
group, keeps the template's value, as it does with `default_tags`. Keys starting with
`aws:` are refused at plan, because AWS reserves them. OpenSearch Serverless
collections accept tags only when they are created, so a collection gets the tags the
module has when it is created, and a later change to `tags` does not change them,
rather than replacing the collection and its vector index. With `tags` left empty, the
module plans exactly what it did before it had the variable.

Two limits follow from how the tags are applied. `default_tags` also tags the
module's S3 objects (the label stack's template and the resource directory), and S3
allows an object at most 10 tags, so keep the map to 10 tags; `idp-cli` refuses more.
And emptying `tags` after it held some is not a reliable way to remove them from the
`awscc` resources: a resource whose template sets no tags is then given `null`, which
the `awscc` provider may treat as "keep what is there". Removing a tag from the map
does remove it, because the attribute then carries the remaining tags.

As with a CloudFormation deployment, the first apply sends a temporary password to
`admin_email`, and a second email asks you to confirm the alerts subscription. Confirm
it, or alarm notifications reach nobody (see
[Who receives the alerts](./monitoring.md#who-receives-the-alerts)).

A first apply of the full stack takes roughly half an hour. Terraform does not roll
back a failed apply the way CloudFormation rolls back a failed stack: what was created
stays in state. Fix the cause and run `terraform apply` again, or run
`terraform destroy`.

#### Reviewing a plan before it is applied

`idp-cli deploy --terraform` plans, checks and applies in one invocation, and has no
option to stop after the plan. Where a change must be reviewed and approved before it
is applied, run the steps from your own root configuration and apply the saved plan
file, so that what is applied is exactly what was reviewed:

```bash
# Plan job
terraform init
terraform plan -out=idp.tfplan
terraform show -json idp.tfplan > idp-plan.json

# Every planned deletion or replacement of a resource that holds data or that
# CloudFormation would retain, which is what idp-cli's own check refuses:
jq -r --slurpfile m .terraform/modules/idp/idp-terraform.json '
  ($m[0].statefulAddresses + $m[0].retainedAddresses) as $protected
  | .resource_changes[]
  | select(.change.actions | index("delete"))
  | select((.address | gsub("\\[[^]]*\\]"; "") | ltrimstr("module.idp."))
      as $bare | $protected | index($bare))
  | "\(.change.actions | join(",")) \(.address)"' idp-plan.json

# Apply job, after approval, from the same configuration and lock file
terraform init
terraform apply idp.tfplan
```

The `jq` command prints nothing when the plan deletes and replaces none of those
resources; an action of `delete,create` or `create,delete` is a replacement. It
assumes the module block is named `idp`, as in the example above. Review the rest of
the plan as for any change (`terraform show idp.tfplan` prints it readably).

A saved plan file holds every input value, sensitive ones included, so store it as a
protected artifact and delete it after the apply. Terraform refuses to apply a saved
plan once the state has changed since the plan was made, so a plan that waited for
approval while something else was applied has to be made again.

#### Using the module beside your own resources

Three things behave differently from an ordinary module when the `module "idp"` block
shares a root configuration with your own resources.

**Inputs that a template condition reads must be known at plan time.** The
templates' conditions become `count` and `for_each` expressions, and Terraform must
know those when it plans. An input such as `reporting_bucket_name` decides whether the
module creates a bucket of its own, so setting it to an attribute that is not known
until apply (`reporting_bucket_name = aws_s3_bucket.analytics.id`, for a bucket
created in the same apply) leaves the condition unknown, and `terraform plan` fails
with `Invalid count argument` or `Invalid for_each argument`.

The same holds for an input that only a nested module's conditions read, which is
easier to miss because no condition in the root module names it. `log_level` and
`waf_allowed_ipv4_ranges` are in that position: they decide inside the API resolver
stack whether the API's access logs and its web ACL exist, so each must be known at
plan time. The headless module has no API resolver stack, so neither applies there.

`vpc_id` can come from a VPC created in the same apply. The pattern stack asks only
whether it is empty, and the template's `DeployInVPCRequiresParams` rule (below)
refuses an empty `vpc_id` whenever `deploy_in_vpc = "true"`, so the module answers
that question without the value: with `deploy_in_vpc = "true"` the pattern stack
creates its VPC-only resources, and with `"false"` it creates none and is given an
empty `vpc_id` whatever you set.

The other inputs that only nested conditions read,
`pattern2_custom_classification_model_arn`, `pattern2_custom_extraction_model_arn`,
`enable_ecr_image_scanning` and `s3_vpc_endpoint_dns_name_override`, decide only
attribute values (a list in a policy or a schema, an image-scanning setting, an
environment variable), not whether a resource exists. `lambda_subnet_ids` and `lambda_security_group_id` decide nothing
and can come from resources created in the same apply. Create anything whose
identifier feeds an input listed here in an earlier apply, or, for a value you choose
yourself such as a bucket name, pass it to the module as a literal.

**The template's `Rules` are refused at plan.** These are the cross-parameter checks
CloudFormation applies when a stack is submitted, such as "`VpcId` is required when
`DeployInVPC=true`" and "the S3 VPC endpoint's id and DNS name are supplied
together". Each rule becomes a `terraform_data` resource in the root module's
`rules.tf`, named `cfn_rule_<rule>`, with one precondition per assertion, so a
combination CloudFormation refuses fails `terraform plan` with that assertion's
description as the error, before anything is created:

```text
Error: Resource precondition failed
...
VpcId is required when DeployInVPC=true
```

Preconditions are used rather than variable validations because a validation that
reads a second variable needs Terraform 1.9, and the module supports 1.7. A
precondition whose inputs are not known at plan time, such as a `vpc_id` taken from
a VPC created in the same apply, is checked during the apply instead. It still
refuses an empty value then, but by that point resources that do not wait on the
check may already have been created, so a value that could turn out empty at apply
is better checked in your own configuration.

The translator declares a variable with the suffix `__cond` for every input a
condition reads, in each module of the tree, so this lists them (a nested module's
variable can be named differently from the root input that feeds it):

```bash
grep -rho 'variable "[a-z0-9_]*__cond"' --include=variables.tf .terraform/modules/idp | sort -u
```

Those `__cond` variables are how the module passes known values to its nested
modules, and setting one yourself is not a dependable way around the rule: it is read
only by the conditions of the module that declares it, and the input still reaches
the nested modules with its unknown value, so a condition there stays unknown.

**Do not put `depends_on` on the `module "idp"` block.** The module reads the
Region, partition and account from data sources, and several conditions compare them
(whether the partition is GovCloud, for example), so those conditions also decide
`count`. Terraform defers every data source inside a module that has `depends_on`
until apply whenever what it depends on has changes to make, which on a first apply is
always, so those conditions are unknown at plan time and the plan fails the same way.
Pass the outputs or attributes of the resources the module needs as its inputs
instead, within the rule above; Terraform orders the work by those references. A resource of yours that
must wait for the deployment can still carry `depends_on = [module.idp]` itself.

**The module's provider constraints apply to the whole root configuration.**
Terraform installs one version of each provider for a root configuration, chosen to
satisfy every constraint in it, so the `aws` and `awscc` constraints in the module's
`versions.tf` bind every resource in your root, not only the module's. A root that
pins `aws` to a major version outside the module's range cannot run `terraform init`
with the module. Deploy the module from a root configuration of its own in that case,
and read its outputs from your other configurations with a `terraform_remote_state`
data source.

### With `idp-cli deploy --terraform`

`idp-cli` can write and apply a root configuration for you:

```bash
idp-cli deploy --terraform \
    --stack-name idp-tf \
    --admin-email admin@example.com \
    --parameters "MaxConcurrentWorkflows=50,LogLevel=INFO"
```

To deploy from your own checkout instead of a published release, add `--from-code .`:
it builds and publishes the templates and the Terraform module to your artifact bucket
first, exactly as `idp-cli publish --source-dir . --terraform` does, then deploys that
module.

```bash
idp-cli deploy --terraform --from-code . \
    --stack-name idp-tf \
    --admin-email admin@example.com
```

The command writes a directory, `./idp-terraform/<stack-name>` by default, containing
a `main.tf` with the `aws` and `awscc` providers configured for the Region and a
`module "idp"` block, plus a `terraform.tfvars` holding your settings and an
`idp-cli-parameters.json` recording them by CloudFormation parameter name. Both files
can hold sensitive values and are created readable by their owner only. It then runs,
in that directory:

1. `terraform init`, with `-upgrade` only when the module source differs from the one
   the directory was last initialised with. Otherwise the providers stay at the
   versions recorded in `.terraform.lock.hcl` instead of moving to the newest release
   the module allows on every deploy.
2. `terraform plan -out=<file>`, then `terraform show -json` on that plan.
3. A check of the plan. If any change deletes or replaces a resource that holds data
   or that CloudFormation would have retained, the command stops, lists those
   addresses, and applies nothing. The module's `idp-terraform.json` lists both,
   for the installed module and the new one: `statefulAddresses` is every bucket,
   table, user pool, KMS key, knowledge base and data source, OpenSearch Serverless
   collection, secret and vector-store resource, and `retainedAddresses` every
   resource with `DeletionPolicy: Retain`. Replacing one of these loses its data.
   The check is stricter than CloudFormation in one case: a parameter change that
   turns off a feature deletes that feature's tables or buckets under
   CloudFormation, and is refused here until you confirm it.
   `--terraform-allow-replace-stateful` applies such a plan anyway.
4. `terraform apply` of exactly that saved plan. The plan file is removed afterwards,
   because it holds your settings too.

The options are:

| Option | Meaning |
|---|---|
| `--terraform` | deploy with Terraform instead of CloudFormation |
| `--stack-name` | the deployment name, which becomes `stack_name` |
| `--admin-email` | the administrator's email address, which becomes `admin_email` |
| `--parameters "K=V,K2=V2"` | settings by their CloudFormation parameter names, as on the CloudFormation path; an unknown name is an error |
| `--tags "K=V,K2=V2"` | tags for every taggable resource, as CloudFormation stack tags are: `main.tf` gives them to the module's `tags` variable and to the `aws` provider's `default_tags` (see [Tagging the deployment](#tagging-the-deployment)), and `terraform.tfvars` records them. Passing tags replaces the deployment's tags; omitting them keeps the ones `terraform.tfvars` in the directory holds from the previous deploy, as a CloudFormation update keeps a stack's. A deploy from a new directory has no record of earlier tags, so pass them again there. At most 10 tags (see [Tagging the deployment](#tagging-the-deployment)); a module published before the module had a `tags` variable refuses them |
| `--terraform-module-url URL` | the module source URL; by default the CLI uses the latest published module for the Region, chosen the same way the CloudFormation deploy chooses its default template |
| `--from-code DIR` | build the templates from a local checkout, publish them with the Terraform module (`idp-cli publish --terraform`), and deploy that module. The build options `--bucket-basename`, `--prefix`, `--public`, `--build-max-workers`, `--clean-build` and `--no-validate-template` apply as on the CloudFormation path. With `--headless` or `--govcloud` it builds and deploys that variant's module. It cannot be combined with `--terraform-module-url` |
| `--headless` / `--govcloud` | use the latest headless or GovCloud module instead of the standard one; they cannot be combined with each other or with `--terraform-module-url`, and `--admin-email` is ignored with `--headless` |
| `--terraform-dir DIR` | the directory to write, instead of `./idp-terraform/<stack-name>` |
| `--terraform-bin` | `terraform` or `tofu` |
| `--terraform-assume-role-arn ARN` | an IAM role both providers assume, written into the `aws` and `awscc` provider blocks of `main.tf` in each provider's own `assume_role` syntax (a block for `aws`, an attribute for `awscc`), so the resources are created as that role instead of as you. You need `sts:AssumeRole` on it. The role needs permission to create, change and delete every resource the deployment creates, including `cloudcontrol:*` for the `awscc` resources and `iam:CreateRole`/`iam:PassRole` on the roles the module creates. A new deployment names every role starting with the deployment name (`stack_name`); a deployment first applied with an earlier module keeps the names its roles were created with (Terraform's generated `terraform-<timestamp>` names among them), because an upgrade does not rename a role, so a role scoped to a name prefix must also cover those. If its role creation is conditioned on a permissions boundary, as the [CloudFormation service role](../iam-roles/cloudformation-management/README.md)'s is, pass that boundary as `PermissionsBoundaryArn` in `--parameters` so the module attaches it to every role it creates |
| `--terraform-allow-replace-stateful` | apply even if the plan deletes or replaces a resource that holds data or that CloudFormation would retain, losing its data; without it such a plan is refused and nothing is applied. `--terraform-allow-replace-retained` is the earlier name and still works |
| `--terraform-state local\|s3` | where Terraform keeps the state. `local`, the default, keeps it in `terraform.tfstate` in the directory. `s3` keeps it in an encrypted, versioned S3 bucket that `idp-cli` creates and locks it with a DynamoDB table; see [Keeping the state in S3](#keeping-the-state-in-s3) |
| `--wait` | accepted as on the CloudFormation path |

`--parameters` takes CloudFormation names, not Terraform variable names. The CLI maps
each one to its variable through the `idp-terraform.json` file shipped in the module,
so `--parameters` lines you already use with CloudFormation deployments work
unchanged.

Terraform state is kept locally in that directory unless you pass
`--terraform-state s3` or put a backend there first. With a local state, keep the
directory: it is the only record Terraform has of what it deployed, and without it
neither `idp-cli` nor Terraform can update or delete the deployment. To use a backend
of your own, create the directory before the first deploy and put a
`terraform { backend "..." { ... } }` block in a file of its own, such as `backend.tf`.
`idp-cli` writes `main.tf` itself and marks it with a comment; it does not overwrite a
`main.tf` or a `backend.tf` it did not write.

#### Keeping the state in S3

`idp-cli deploy --terraform --terraform-state s3` keeps the state in S3 instead of in
the directory, so the deployment survives the loss of the directory and a second
operator, whose credentials are in the same account, can deploy from another
directory against the same state:

```bash
idp-cli deploy --terraform --terraform-state s3 \
  --stack-name idp-tf --admin-email you@example.com --region us-east-1
```

Before it writes anything into the directory, `idp-cli` makes sure two resources exist
in the deployment's Region and in the account of the credentials running it, creating
them when they are missing:

- **The state bucket**, `idp-tf-<stack-name>-<account-id>-<region>` (the stack name
  lowercased). A new bucket gets a public access block with all four settings on,
  versioning, SSE-S3 default encryption, and a bucket policy that denies every request
  not made over TLS, because the state holds the deployment's settings and some of its
  outputs (the MCP client secrets, for example) in clear. The state object is
  `<stack-name>/terraform.tfstate`.
- **The lock table**, a DynamoDB table named `idp-tf-lock-<stack-name>` with the
  `LockID` key the S3 backend uses, billed on demand.

`idp-cli` then writes `backend.tf` in the directory with `bucket`, `key`, `region`,
`encrypt = true` and `dynamodb_table`, marked as written by `idp-cli`, and rewrites it
on every deploy with `--terraform-state s3`.

The lock is the DynamoDB table rather than the S3 backend's own `use_lockfile`,
because `use_lockfile` needs Terraform or OpenTofu 1.10 and the module supports 1.7
and later; two operators whose binaries locked in different ways would not exclude each
other, so every deploy uses the table. Terraform 1.11 and later print a deprecation
warning for `dynamodb_table`; the lock still works.

A later deploy reuses both. The bucket must belong to your account: a bucket of that
name in another account is refused and nothing is written to it. A reused bucket is
never recreated or emptied. Of its four protective settings, a deploy applies only one
the bucket lacks entirely, which finishes a creation an earlier deploy was interrupted
in. A setting someone changed is left as it is, with a warning, for versioning
(suspended), the public access block (any of its four settings off) and the bucket
policy (one without a deny on non-TLS requests). Every call on the bucket names your
account as its expected owner.

Two directory changes are refused rather than carried out. A directory that already
holds a local `terraform.tfstate` cannot be switched to `s3`, because moving an
existing state into S3 (`terraform init -migrate-state`) is not supported yet; it is
planned as a follow-up. And a directory whose `backend.tf` `idp-cli` wrote must keep
being deployed with `--terraform-state s3`. A deploy from a new directory with
`--terraform-state s3` and the same stack name uses the same bucket and state.

The identity running `idp-cli` needs these permissions in addition to the deployment's
own:

| Permission | Resource | Why |
|---|---|---|
| `sts:GetCallerIdentity` | — | the account id in the bucket name |
| `s3:ListBucket` | the state bucket | `HeadBucket`, and the backend listing the state |
| `s3:CreateBucket`, `s3:PutBucketVersioning`, `s3:PutBucketPublicAccessBlock`, `s3:PutEncryptionConfiguration`, `s3:PutBucketPolicy` | the state bucket | creating it, on the first deploy |
| `s3:GetBucketVersioning`, `s3:GetBucketPublicAccessBlock`, `s3:GetEncryptionConfiguration`, `s3:GetBucketPolicy` | the state bucket | checking a reused bucket's settings |
| `s3:GetObject`, `s3:PutObject` | `<bucket>/<stack-name>/terraform.tfstate` | reading and writing the state |
| `dynamodb:CreateTable`, `dynamodb:DescribeTable` | the lock table | creating it, on the first deploy |
| `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:DeleteItem` | the lock table | taking and releasing the lock |

`--terraform-assume-role-arn` does not apply to the backend: the bucket and the table
are created, and the state is read and written, with your own credentials, and the
bucket name carries your credentials' account rather than the role's. Two operators
who assume the same role from different accounts therefore compute different bucket
names and do not share a state; the second one's deploy would plan the whole
deployment as new. A shared state needs operators whose own credentials are in one
account. Each deploy logs the bucket it created or reused.

`idp-cli delete --terraform` deletes neither the bucket nor the table, so the state's
history stays recoverable from the bucket's versions. To remove them once the
deployment is deleted and you no longer need its state, empty the bucket of every
object version (the S3 console's **Empty** action on the bucket does that), then delete
the bucket and the table:

```bash
aws s3api delete-bucket --bucket idp-tf-<stack-name>-<account-id>-<region>
aws dynamodb delete-table --table-name idp-tf-lock-<stack-name>
```

Emptying it from the command line means deleting each version by its `VersionId`;
`aws s3 rm --recursive` removes only the current versions and leaves the bucket
undeletable. `delete-objects` takes at most 1000 keys per request and refuses an empty
list, so loop over `list-object-versions` pages if you script it.

## Parameters

Each CloudFormation parameter of the main template is a variable of the root module,
named in snake_case: `AdminEmail` is `admin_email`, `MaxConcurrentWorkflows` is
`max_concurrent_workflows`, `PermissionsBoundaryArn` is `permissions_boundary_arn`.
A run of capitals is one word, so `WAFAllowedIPv4Ranges` is
`waf_allowed_ipv4_ranges`, `ExternalIdPOIDCClientId` is `external_idp_oidc_client_id`
and `CloudFrontPriceClass` is `cloudfront_price_class`. The module's `variables.tf`, and
the `parameters` map in its `idp-terraform.json`, give every exact name. A `CommaDelimitedList` parameter
is a `list(string)` variable, so `LambdaSubnetIds` is written
`lambda_subnet_ids = ["subnet-a", "subnet-b"]`. A `NoEcho` parameter is a `sensitive`
variable.

Each variable carries the CloudFormation parameter's description and default, and its
constraints as Terraform `validation` blocks: allowed values, allowed pattern, minimum
and maximum value, with the parameter's constraint description in the error message.
A value CloudFormation would refuse is therefore refused at `terraform plan`, before
anything is changed.

`stack_name` is the name of the deployment. It plays the role of the CloudFormation
stack name: it is the value of `AWS::StackName` everywhere in the templates, the
prefix of generated resource names, and the name of the label stack. It must start
with a letter, contain only letters, digits and hyphens, and be at most 25 characters,
the same limit the CloudFormation template enforces.

To change a setting, change the variable in your configuration (or in
`terraform.tfvars` in the `idp-cli` directory), then run `terraform plan` and
`terraform apply`. Do not change settings on the label stack: it does not deploy
anything, and its parameters refuse any other value, as the next section explains.

## The read-only label stack

Every Terraform deployment creates one small CloudFormation stack, named exactly like
the deployment (`stack_name`). It is a label, not a deployment: it manages no
resources, and Terraform is the only thing that writes to it. It exists for two
reasons. It lets you find and cross-reference Terraform deployments of the
accelerator, and their versions, from CloudFormation alongside CloudFormation
deployments. And it is how `idp-cli` and the SDK find a Terraform deployment's
resources by `--stack-name`.

The label stack has:

- A description made of the main template's description, which carries the
  accelerator's tracking marker and version (for example
  `AWS GenAI IDP Accelerator (uksb-r8evguc4p9) (SO9027) (v<version>)`), then
  `(Terraform)`, then a notice that it is a read-only label updated only by Terraform.
  A search on the tracking marker finds both kinds of deployment; `(Terraform)` tells
  them apart.
- The tags `ManagedBy=Terraform`, `ReadOnly=true`, and a `Purpose` tag saying to update
  it only with `terraform apply`.
- One resource, an `AWS::CloudFormation::WaitConditionHandle`, which creates nothing
  and costs nothing. Its metadata records the version and that the deployment is
  managed by Terraform.
- The deployment's settings as parameters, under their CloudFormation names
  (`AdminEmail`, `LogLevel`, and so on), with the values Terraform applied, so the
  console's Parameters tab shows them as it would for a CloudFormation stack. Each
  description starts with `READ-ONLY`, and the console groups them under a read-only
  heading. They are read-only by construction, not only by convention: each
  parameter's allowed values are exactly its current value, so a CloudFormation update
  that tries another value is refused, with a message saying to change it in
  Terraform. `NoEcho` parameters are left out.
- The main template's outputs, plus `TerraformResourceDirectory`, which names an S3
  object listing every resource of the deployment in CloudFormation's shape (logical
  ID, type and physical ID, per module), and `TerraformResourceDirectoryVersion`, a
  hash of that object's content.

The variable `enable_cfn_stack_label` controls the label. It defaults to `true`. Set
it to `false` to skip the stack, for example where the deploying role may not use
CloudFormation. Without the label, `idp-cli` and the SDK cannot address the
deployment, and every operation on this page that takes `--stack-name` stops working
for it.

## What still uses CloudFormation

A Terraform deployment does not deploy through CloudFormation, but it is not free of
CloudFormation's API either. Three things use it:

- **The label stack**, the module's one `aws_cloudformation_stack` resource, described
  in the previous section. It is created, updated and deleted with the credentials
  Terraform runs with, using the `cloudformation:` stack actions. Setting
  `enable_cfn_stack_label = false` removes it, and with it the ability of `idp-cli`
  and the SDK to address the deployment by `--stack-name`. Running `idp-cli` against
  a deployment that has the label also needs read access to it
  (`cloudformation:DescribeStacks`).
- **The Cloud Control API**, through which the `awscc` provider manages the resources
  that have no `hashicorp/aws` mapping in the translator (queues, state machines,
  alarms, the KMS key and others; the module's `.tf` files show which). Cloud Control
  creates no stacks, but its actions are in the `cloudformation:` IAM namespace
  (`cloudformation:CreateResource`, `GetResource`, `UpdateResource`, `DeleteResource`,
  `ListResources` and `GetResourceRequestStatus`), so they are needed for every
  deployment and cannot be turned off.
- **Feature-platform extensions** (see [Feature Platform](./feature-platform.md)),
  which install as CloudFormation stacks of their own. The extension templates in this
  repository read the main deployment's values through CloudFormation exports
  (`Fn::ImportValue` of `<stack name>-...`), and the Terraform module creates no
  CloudFormation exports, so those imports have nothing to resolve to. Installing an
  extension on a Terraform deployment has not been tested. `EnableFeaturePlatform`
  set to `false` removes the platform.

The CloudFormation custom resources in the templates do not need CloudFormation: the
module's bridge function calls their handlers itself.

So the module cannot be deployed where every `cloudformation:` action is denied,
because that denies Cloud Control as well. Where only the stack actions are denied
and the Cloud Control actions are allowed, the module needs
`enable_cfn_stack_label = false`, which leaves the deployment without `idp-cli` and
SDK access by `--stack-name` and without extensions. Neither case has been tried.

## Using `idp-cli` and the SDK

Pass the deployment name as `--stack-name`, exactly as for a CloudFormation stack:

```bash
idp-cli process --stack-name idp-tf --dir ./samples/
idp-cli config-download --stack-name idp-tf --output config.yaml
```

This works because the [IDP SDK](./idp-sdk.md), which `idp-cli` is built on, installs
a hook on the CloudFormation clients it creates when `idp_sdk` is imported. For a
label stack, the hook answers the resource lookups (`ListStackResources`,
`DescribeStackResource`, `DescribeStackResources`, and `DescribeStacks` for nested
stacks) from the resource directory the label points at. Every other call, and every
call about a CloudFormation stack, goes to CloudFormation unchanged. Your own Python
code that uses `boto3` in the same process as `idp_sdk` gets the same behaviour.

`idp-cli deploy` and `idp-cli delete` without `--terraform` refuse a Terraform
deployment. A CloudFormation update or delete would act on the label stack, not on
the deployment, so the CLI stops and names the Terraform command to run instead.

## Deleting

To delete a deployment made with `idp-cli`, run:

```bash
idp-cli delete --terraform --stack-name idp-tf
```

Add `--terraform-dir DIR` if you deployed with a directory other than the default, and
`--force` to skip the confirmation. A deployment applied with
`--terraform-assume-role-arn` keeps that role in `main.tf`, so the delete uses it
too; pass `--terraform-assume-role-arn` to the delete to set or change it, which
rewrites the two provider blocks before `init`. The command empties the deployment's buckets the
way the CloudFormation delete path does, then removes from Terraform state every
resource whose CloudFormation `DeletionPolicy` keeps it (`Retain` or
`RetainExceptOnCreate`), and then runs `terraform destroy`. Removing a resource from
state means Terraform forgets it without deleting it, so the resources CloudFormation
would retain are retained here too: the logging bucket, the data buckets, the KMS key
that encrypts them, and the other resources the templates mark that way. The command
reports which retained resources were left, as the CloudFormation path does, naming the
Region-wide API Gateway account setting and the KMS key for what they are rather than
by their identifiers. For the key it also prints the `aws kms schedule-key-deletion`
command to run once none of the retained data is needed; the key costs about $1 a
month until then. See [The retained KMS key](./idp-cli.md#the-retained-kms-key).

It also lists the log groups AWS services created on the deployment's behalf, which
neither `terraform destroy` nor a CloudFormation stack delete removes, because neither
created them: the `/aws/lambda/<function>` group of each function that logs to Lambda's
default group (the custom-resource handlers), the `/aws/codebuild/<project>` group of a
build project that logs to CodeBuild's default, and the Glue crawler's log streams.
Each group exists only if its resource ran, and the service creates it with no
retention. The crawler writes to `/aws-glue/crawlers`, which every crawler in the
Region shares, so delete only the streams named after this deployment's crawler, not
the group.

If you deployed with your own root configuration, do the same steps yourself. The
module's `idp-terraform.json` lists the retained resources in `retainedAddresses`,
relative to the module and without instance keys. After `terraform init` the module is
normally extracted under `.terraform/modules/idp/`. The following commands collect
every instance of those resources in state into `remove.txt`, then remove them all
with one `terraform state rm`, as `idp-cli` does; each call reads and writes the
whole state, so one call per address is slow with a remote backend. They do
what `idp-cli` does, and select the same addresses as `idp-cli` from a state list
built from the generated module, but have not themselves been run against a live
deployment, so read `remove.txt` before you run the last two commands.

```bash
jq -r '.retainedAddresses[]' .terraform/modules/idp/idp-terraform.json > retained.txt
terraform state list | while IFS= read -r addr; do
  bare=$(printf '%s' "$addr" | sed -E 's/\[[^]]*\]//g')
  if grep -qxF "${bare#module.idp.}" retained.txt; then
    printf '%s\n' "$addr"
  fi
done > remove.txt
# Read remove.txt, then:
addrs=(); while IFS= read -r a; do addrs+=("$a"); done < remove.txt
terraform state rm "${addrs[@]}"
```

Before `terraform destroy`, empty every bucket it will delete that holds objects, or
its deletion fails. The module's own custom-resource bridge buckets (`cfn_bridge`)
are created with `force_destroy` and need nothing. Run after the `terraform state rm`
above, this lists the module's other buckets still in state, such as the Jobs API
staging bucket when `EnableJobsApi` is `true`:

```bash
terraform state list | grep -E '^module\.idp\..*(aws|awscc)_s3_bucket\.' \
  | grep -v '\.cfn_bridge$'
```

Buckets of your own in the same root configuration need the same treatment. Then run
`terraform destroy`. Retained resources keep running up costs (storage, mostly) until
you delete them yourself.

## Upgrading

To upgrade, point the module at the new release's URL and apply:

1. Change `source` in the `module "idp"` block to the new version's zip URL.
2. Run `terraform init`, which downloads the new module. Terraform refuses to plan
   with a changed module source until you do.
3. Run `terraform plan` and read it. Replacements of stateful resources (buckets,
   tables, the user pool, keys, the knowledge base) deserve a close look before you
   apply; `jq -r '.statefulAddresses[]'
   .terraform/modules/idp/idp-terraform.json` lists them.
4. Run `terraform apply`.

The label stack's description and metadata move to the new version in the same apply.
With `idp-cli`, run `idp-cli deploy --terraform` again with the same `--stack-name`
and `--terraform-dir`, and the new `--terraform-module-url` (or none, for the latest).
You do not need to repeat the `--parameters` you passed before: as a CloudFormation
update does, the upgrade keeps the value the deployment has for every parameter you
do not pass, whether you set it or it took the default, and applies the parameters
you pass now over them. A parameter that is new in the release takes its default.

The values come from the deployment's [label stack](#the-read-only-label-stack),
whose parameters Terraform writes at every apply with each parameter's value in
effect. The label stack lives with the deployment rather than in your directory, so
a second operator who deploys from another directory against the same state backend
gets the deployment's current settings rather than the module's defaults. It is
also what lets a deployment made with an earlier `idp-cli`, which recorded only the
values that were passed, keep its defaulted values on its first upgrade: in
particular, release 0.6.15 changed the default of `EnableMCP` from `true` to
`false`, and a deployment that never passed it keeps `EnableMCP=true`. The label
stack leaves `NoEcho` parameters out, so their values come only from the directory's
`idp-cli-parameters.json`; a second operator must pass any that the deployment set.
If the label stack and the directory disagree (another operator changed a setting
since), the label stack's value is used, because it is the value last applied.

After each apply, `idp-cli-parameters.json` records every parameter's value in
effect, defaulted ones included, so a later upgrade from that directory keeps them
even where the label stack cannot be read. Before the plan, the output lists the
values the upgrade kept that differ from the new module's defaults, and the new
parameters that took their defaults. A parameter no longer in the new module is
dropped with a message.

⚠️ Without a label stack (`enable_cfn_stack_label = false`), a deployment whose directory has only the passed
values, as an earlier `idp-cli` wrote it, falls back to those: every parameter never
passed takes the new module's default. The deploy then prints a warning naming each
such parameter whose default differs between the installed module and the new one,
so you can pass the old value with `--parameters`. A second operator in a fresh
directory without the label stack gets the module's defaults with no warning,
because nothing distinguishes that from a first deploy; pass every setting the
deployment has.

Your own root configuration does not get this: a variable you do not set takes the
new module's default, so read the release's `CHANGELOG.md` for changed defaults
before upgrading it. The `idp-cli` deploy runs `init -upgrade` because the module
source changed, and refuses the plan if it would delete or replace a stateful
or retained resource (see
[With `idp-cli deploy --terraform`](#with-idp-cli-deploy---terraform)).

### What keeps an upgrade from replacing a resource

Three things in the module do what CloudFormation does implicitly on an update:

- **`moved` blocks.** When a release renames a resource or moves it between nested
  stacks, its Terraform address changes, and without help Terraform would destroy
  the old resource and create a new one. Each release's module carries a `moved.tf`
  with a block for every address that changed since the previous release, and for
  every earlier release, so an upgrade that skips releases is covered too.
  Terraform applies them as part of the plan; there is nothing to run.
- **Generated names are kept.** A name the module chose (a table's, a bucket's
  prefix, a role's) is kept for the life of the resource, as CloudFormation keeps a
  name it generated, so a later release that would derive it differently does not
  replace the resource.
- **Values that only a custom resource knows.** Some properties are filled from a
  custom resource's result, which Terraform cannot know until apply whenever that
  custom resource's inputs change. For a property that cannot change in place, such
  as the knowledge base id of the knowledge base's data source, Terraform would
  plan a replacement where CloudFormation, which sees the result, changes nothing.
  Where the result is known to be stable, the module keeps the stored value and
  replaces the resource only when one of its other inputs changes, which is when
  CloudFormation would.

The plan check in `idp-cli deploy --terraform` is the backstop for anything these
miss. If you deploy the module from your own configuration, read the plan for
deletions of the addresses in `statefulAddresses` before applying.

A role the template leaves unnamed keeps the name it was created with for the life of
the deployment, as CloudFormation keeps a name it generated. So a deployment created
from a module that predates stack-prefixed role names keeps its `terraform-<timestamp>`
role names across upgrades; only a new deployment gets names a scoped deploying role
can create (see [Deploying with a scoped role](#deploying-with-a-scoped-role)).
Replacing a role is not contained to the role: custom resources that take its ARN are
updated, and their results are unknown until apply, which is the case the previous
section's third item handles.

In release 0.6.15 and earlier, any upgrade that changed the CloudFront
security-headers policy (its Content-Security-Policy, for example)
stopped part way through the apply at
`awscc_cloudfront_response_headers_policy.security_headers_policy`, with a Cloud
Control `ValidationException` naming `XSSProtection`: the provider sent back the
empty object it had stored for that unset header. The policy now sets
`X-XSS-Protection: 0` explicitly, so upgrading to a release with the fix updates
the policy in place, including on a deployment that an earlier failure left part
way, and so do later changes to it. CloudFormation deployments were never
affected.

Read the release's `CHANGELOG.md` entries and any migration guide for the release
before upgrading, as for a CloudFormation stack: configuration changes and upgrade
actions apply to both.

## How the translation is checked

A translation from CloudFormation to Terraform cannot be proved equivalent by
reading it, so three independent checks look at different things:

| Check | When it runs | What it shows |
|---|---|---|
| The translator's own checks and `terraform validate` on every pinned Terraform and OpenTofu release | every pull request | every type, property and value in the templates is translated and reaches the Terraform; no data-holding resource depends on a value Terraform cannot know at plan time without a declared reason; the module loads and type-checks |
| A static security scan of both, compared resource by resource | every pull request | no Terraform resource fails a security rule that its CloudFormation source passes, beyond a short list of explained exceptions (mostly on the custom-resource bridge the module adds); findings on rules only the Terraform scanner has are pinned per rule, so a new one fails too. The repository's SRT scan does not cover the generated module, which is never committed; this scan is what covers it |
| A property comparison of a Terraform deployment with a CloudFormation deployment of the same release and parameters, every resource of both read back from AWS | release validation | the two deployments store the same configuration, beyond explained differences |

None of the three plans or applies on a pull request. The full stack has been
applied, re-planned and destroyed in a development account (see the developer
notes linked below); the property comparison has been run on one nested stack and
not yet on the full stack.

## Security scanner findings on the module

The CloudFormation templates record the scanner findings that were reviewed and
accepted, either as a `# checkov:skip=<rule>: <reason>` comment inside a resource or
as a cfn_nag `rules_to_suppress` entry in its `Metadata`. The module carries a
checkov suppression over as `#checkov:skip=<rule>:<reason>` inside every Terraform
resource block generated from that CloudFormation resource, with the same reason,
when checkov's Terraform rule inspects the same setting and fails in the same cases
as its CloudFormation rule. That covers the Lambda dead-letter, concurrency and VPC
rules, the ECR, DynamoDB, CloudWatch Logs, CloudFront, WAF and S3 access-logging
rules the templates suppress, and the IAM write-access rule, which checkov numbers
`CKV_AWS_111` for CloudFormation and `CKV_AWS_290` for Terraform. The list of
rules, and the reason each pair is the same rule, is
[`SUPPRESSION_EQUIVALENTS` in the translator](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/terraform/translator/idp_tf/suppressions.py).

So checkov run over the module reports those findings as skipped, with the reason
the template gives. It still reports:

- the suppressions in the table below, which are not carried. cfn_nag has no
  Terraform rules, and a checkov rule whose Terraform version fails in cases its
  CloudFormation version passes is not carried, because the recorded review did not
  consider those cases. The rules in that position are named above the table;
- findings on rules that exist only for Terraform, such as X-Ray tracing on Lambda
  functions. Their accepted counts and reasons are pinned in
  [`scripts/terraform_scan_tf_only_baseline.json`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/scripts/terraform_scan_tf_only_baseline.json);
- findings the CloudFormation templates fail without a recorded suppression, which
  a checkov scan of the templates reports too.

Other scanners, such as Trivy, read their own suppression comments with their own
rule identifiers. The module does not carry suppressions for them, so they report
every finding they detect.

<details>
<summary>Suppressions recorded in the templates and not carried into the module</summary>

The table is generated from the templates by `python -m idp_tf.suppressions`, run
in `terraform/translator`, and a test fails when it is out of date.

<!-- uncarried-suppressions:begin (generated; see terraform/translator/idp_tf/suppressions.py) -->

- checkov `CKV_AWS_173` is not carried: checkov's Terraform rule also fails a function with a KMS key and no environment variables, which the CloudFormation rule passes.
- checkov `CKV_AWS_99` is not carried: checkov's Terraform rule requires KMS modes and keys the CloudFormation rule does not.

| Rule | Reason recorded in the template | Resources |
|---|---|---|
| cfn_nag `F3` | bedrock-agentcore:* covers the AgentCore Gateway and gateway target lifecycle (list, get, create, update, delete); the CloudWatch Logs actions are the deletes that remove the log group and delivery resources older gateways created, and are not wildcards | `AgentCoreGatewayManagerFunction` |
| cfn_nag `F38` | PassRole is required to delegate the execution role to the Bedrock AgentCore Gateway service, and is scoped to the single AgentCoreGatewayExecutionRole ARN | `AgentCoreGatewayManagerFunction` |
| cfn_nag `W11` | CloudWatch Logs require wildcard for log group creation | `MULTIDOCDISCOVERYSTACK/DockerBuildRunFunctionRole` |
| cfn_nag `W11` | CloudWatch does not support resource-level permissions | `APIRESOLVERSTACK/SyncBdaIdpResolverFunction`, `PATTERNSTACK/MLflowLoggerFunction` |
| cfn_nag `W11` | Cloudwatch does not support resource-level permissions | `PATTERNSTACK/BDACompletionFunction`, `PATTERNSTACK/BDAProcessResultsFunction`, `PATTERNSTACK/ClassificationFunction`, `PATTERNSTACK/InvokeBDAFunction`, `PATTERNSTACK/ProcessResultsFunction`, `PATTERNSTACK/RuleValidationFunction`, `PATTERNSTACK/RuleValidationOrchestrationFunction`, `PATTERNSTACK/RuleValidationPolicyClassificationFunction`, `PATTERNSTACK/SummarizationFunction`, `PATTERNSTACK/TestExecutionAggregationFunction` |
| cfn_nag `W11` | Cloudwatch does not support resource-level permissions, and Bedrock should support any enabled Bedrock model_id or inference profile | `PATTERNSTACK/AssessmentFunction`, `PATTERNSTACK/ExtractionFunction`, `PATTERNSTACK/ShardRuntimeFunction` |
| cfn_nag `W11` | Every statement is scoped to the InputBucket, the CMK, or the GuardDuty-managed rule name prefix; no wildcard resources | `MalwareScanRole` |
| cfn_nag `W11` | Role requires * for Bedrock marketplace and CloudWatch Metrics | `ChatStreamProcessorFunction`, `ChatWithDocumentProcessorFunction` |
| cfn_nag `W11` | Role requires * resource access for CloudWatch Dashboards | `DashboardMergerFunction` |
| cfn_nag `W11` | Role requires * resource access for CloudWatch Logs and CloudFormation custom resource operations | `PATTERNSTACK/CodeBuildExecutionRole`, `StartUICodeBuildExecutionRole` |
| cfn_nag `W11` | Role requires * resource access for CloudWatch Metrics and Logs | `AgentChatProcessorFunction`, `AgentProcessorFunction`, `BlueprintOptimizationFunction`, `DiscoveryProcessorFunction`, `PATTERNSTACK/EvaluationFunction`, `SaveReportingDataFunctionV2`, `WorkflowTracker` |
| cfn_nag `W11` | Role requires * resource access for Marketplace | `DOCUMENTKB/KnowledgeBaseServiceRole` |
| cfn_nag `W11` | Role requires * resource access for Marketplace and CloudWatch Metrics and Logs | `APIRESOLVERSTACK/QueryKnowledgeBaseResolverFunction` |
| cfn_nag `W11` | Role requires * resource access for Marketplace, CloudWatch Metrics and Logs | `APIRESOLVERSTACK/AgentChatResolverFunction` |
| cfn_nag `W11` | Specified permissions do not appy to specific resources | `DOCUMENTKB/OpenSearchLambdaExecutionRole` |
| cfn_nag `W11` | Textract DetectDocumentText API and Cloudwatch APIs do not support resource-level permissions | `PATTERNSTACK/OCRFunction` |
| cfn_nag `W11` | The s3vectors bucket statements are scoped to this account and Region with the bucket segment left as a wildcard (bucket/*): the handler sanitizes the bucket name it derives from the stack name, and a template-side copy of that name (S3VectorBucketNameLowercase) matches it only for stack names the parent stack produces, so narrowing the create grant to it would make Create fail for any other name. The index name is exact. ListIndexes, and DeleteIndex on any index, are granted only on this stack's own bucket, named exactly, because a mismatch there falls back to deleting the named index. What remains unscoped is the Bedrock knowledge base control plane (Create/Delete/Get/Update/ListKnowledgeBases), whose knowledge base id is service-generated and does not exist at create time. iam:PassRole is not granted here; S3VectorManagerPassRolePolicy grants it on KnowledgeBaseServiceRole alone. | `DOCUMENTKB/S3VectorManagerRole` |
| cfn_nag `W11` | Wildcard permissions required for CloudWatch Logs creation | `PATTERNSTACK/DockerBuildRole` |
| cfn_nag `W11` | Wildcard permissions required for CloudWatch Logs creation and ECR auth | `MULTIDOCDISCOVERYSTACK/CodeBuildRole` |
| cfn_nag `W11` | Wildcard resource required as AgentCore Gateway ARNs are dynamically generated and unknown at deployment time | `AgentCoreGatewayManagerFunction` |
| cfn_nag `W11` | X-Ray daemon actions and CloudWatch Logs log-delivery actions genuinely require Resource=*: the X-Ray tracing API accepts no resource ARN, and CloudWatch Logs log-delivery calls (logs:CreateLogDelivery / GetLogDelivery / UpdateLogDelivery / DeleteLogDelivery / ListLogDeliveries) also accept no resource ARN. The sibling DataMartRollupFunction role at line 5497 carries an equivalent W12 suppression for the same reason. | `DataMartMigrationStateMachineRole` |
| cfn_nag `W11` | aws-marketplace and logs:DescribeLogGroups APIs do not support resource-level permissions | `AgentCoreMCPHandlerFunction` |
| cfn_nag `W11` | cloudwatch:PutMetricData takes no resource-level permissions; it is scoped by namespace condition | `ReviewQueueSweeperFunction`, `TestRunSweeperFunction` |
| cfn_nag `W11` | servicequotas:GetServiceQuota and ListServiceQuotas APIs do not support resource-level permissions | `CalculateCapacityFunction` |
| cfn_nag `W12` | Amazon Inspector ListCoverage and ListFindings require wildcard resource per AWS documentation | `PATTERNSTACK/DockerBuildRole` |
| cfn_nag `W12` | Lambda requires CloudWatch logs permissions | `APIRESOLVERSTACK/AbortTestRunsResolverFunction`, `APIRESOLVERSTACK/AbortWorkflowResolverFunction`, `APIRESOLVERSTACK/BulkReviewResolverFunction`, `APIRESOLVERSTACK/BulkReviewWorkerFunction`, `APIRESOLVERSTACK/ConfigurationResolverFunction`, `APIRESOLVERSTACK/CopyToBaselineResolverFunction`, `APIRESOLVERSTACK/CreateDocumentResolverFunction`, `APIRESOLVERSTACK/DeleteDocumentResolverFunction`, `APIRESOLVERSTACK/DeleteTestsResolverFunction`, `APIRESOLVERSTACK/DiscoveryUploadResolverFunction`, `APIRESOLVERSTACK/DocumentVersionsResolverFunction`, `APIRESOLVERSTACK/GetFileContentsResolverFunction`, `APIRESOLVERSTACK/GetSampleDocumentResolverFunction`, `APIRESOLVERSTACK/ListDocumentsByDateRangeResolverFunction`, `APIRESOLVERSTACK/NotificationsResolverFunction`, `APIRESOLVERSTACK/ProcessChangesResolverFunction`, `APIRESOLVERSTACK/QueryKnowledgeBaseResolverFunction`, `APIRESOLVERSTACK/ReprocessDocumentResolverFunction`, `APIRESOLVERSTACK/ReviewQueuesResolverFunction`, `APIRESOLVERSTACK/SendChatDocumentMessageResolverFunction`, `APIRESOLVERSTACK/TestResultsResolverFunction`, `APIRESOLVERSTACK/TestRunnerFunction`, `APIRESOLVERSTACK/TestSetResolverFunction`, `APIRESOLVERSTACK/UploadResolverFunction`, `BlueprintOptimizationFunction`, `DiscoveryProcessorFunction`, `TestFileCopierFunction`, `TestSetFileCopierFunction` |
| cfn_nag `W12` | cloudwatch:GetMetricData and resourcegroupstaggingapi:GetResources require * resource — no partition-level scoping available | `DataMartRollupFunction` |
| cfn_nag `W35` | This is the logging destination bucket - does not require its own access logging | `LoggingBucket` |
| cfn_nag `W58` | DLQ not required for Cfn Custom Resource function | `AgentChatProcessorFunction`, `AgentProcessorFunction`, `DOCUMENTKB/S3VectorManagerFunction`, `DOCUMENTKB/StartIngestionJobFunction` |
| cfn_nag `W58` | DLQ not required: invoked synchronously by the API dispatcher, which returns errors to the caller | `APIRESOLVERSTACK/AgentChatResolverFunction`, `APIRESOLVERSTACK/GetStepFunctionExecutionResolverFunction` |
| cfn_nag `W58` | Lambda function has permission to write CloudWatch Logs via the attached IAM role | `APIRESOLVERSTACK/SyncBdaIdpResolverFunction` |
| cfn_nag `W70` | Minimum TLS version is determined by CloudFrontDefaultCertificate true | `CloudFrontDistribution` |
| cfn_nag `W76` | Suppressing W76: SPCM for IAM policy document is higher than 25 | `AgentChatProcessorFunction`, `AgentProcessorFunction`, `PATTERNSTACK/ExtractionFunction`, `PATTERNSTACK/ShardRuntimeFunction` |
| cfn_nag `W78` | Point-in-time recovery not required for concurrency tracking table as data is transient | `ConcurrencyTable` |
| cfn_nag `W84` | LogGroup does not require KMS Key Id | `ReadPreviousIDPPatternFunctionLogGroup` |
| cfn_nag `W84` | LogGroup does not require KMS Key Id, since it's only logging checks on stack name length | `StacknameCheckFunctionLogGroup` |
| cfn_nag `W84` | Logs one key ARN verdict per stack operation; encrypting it with the key it checks would make the check wait for that key | `CustomerManagedKmsKeyCheckFunctionLogGroup` |
| cfn_nag `W89` | Dispatcher only calls SFN StartExecution + SSM DeleteParameter + urllib to CFN — no VPC needed | `DataMartMigrationDispatcherFunction` |
| cfn_nag `W89` | Function does not require VPC access | `APIRESOLVERSTACK/ListDocumentsGSIResolverFunction`, `BackfillTriggerFunction`, `CalculateCapacityResolverFunction`, `CompleteSectionReviewFunction`, `CustomerManagedKmsKeyCheckFunction`, `FinetuningDeploymentHandlerFunction`, `FinetuningJobCreatorFunction`, `FinetuningJobStatusCheckerFunction`, `FinetuningJobsResolverFunction`, `FinetuningListDocumentsFunction`, `FinetuningMergeDataFunction`, `FinetuningProcessDocumentFunction`, `MULTIDOCDISCOVERYSTACK/DockerBuildRunFunction`, `MULTIDOCDISCOVERYSTACK/MultiDocDiscoveryAnalyzeFunction`, `MULTIDOCDISCOVERYSTACK/MultiDocDiscoveryClusterFunction`, `MULTIDOCDISCOVERYSTACK/MultiDocDiscoveryEmbedFunction`, `MULTIDOCDISCOVERYSTACK/MultiDocDiscoverySaveFunction`, `MultiDocDiscoveryPrepareFunction`, `ReadPreviousIDPPatternFunction`, `UserManagementFunction`, `UserSyncFunction` |
| cfn_nag `W89` | Function does not require VPC access as it has no network access | `StacknameCheckFunction` |
| cfn_nag `W89` | Function does not require VPC access as it only interacts with AWS services via APIs | `APIRESOLVERSTACK/AbortTestRunsResolverFunction`, `APIRESOLVERSTACK/AbortWorkflowResolverFunction`, `APIRESOLVERSTACK/AgentChatResolverFunction`, `APIRESOLVERSTACK/AgentRequestHandlerFunction`, `APIRESOLVERSTACK/ConfigurationResolverFunction`, `APIRESOLVERSTACK/CopyToBaselineResolverFunction`, `APIRESOLVERSTACK/CreateDocumentResolverFunction`, `APIRESOLVERSTACK/DeleteAgentChatSessionFunction`, `APIRESOLVERSTACK/DeleteDocumentResolverFunction`, `APIRESOLVERSTACK/DeleteTestsResolverFunction`, `APIRESOLVERSTACK/DiscoveryUploadResolverFunction`, `APIRESOLVERSTACK/DocumentVersionsResolverFunction`, `APIRESOLVERSTACK/GetAgentChatMessagesFunction`, `APIRESOLVERSTACK/GetFileContentsResolverFunction`, `APIRESOLVERSTACK/GetSampleDocumentResolverFunction`, `APIRESOLVERSTACK/GetStepFunctionExecutionResolverFunction`, `APIRESOLVERSTACK/ListAgentChatSessionsFunction`, `APIRESOLVERSTACK/ListDocumentsByDateRangeResolverFunction`, `APIRESOLVERSTACK/ProcessChangesResolverFunction`, `APIRESOLVERSTACK/QueryKnowledgeBaseResolverFunction`, `APIRESOLVERSTACK/ReprocessDocumentResolverFunction`, `APIRESOLVERSTACK/SendChatDocumentMessageResolverFunction`, `APIRESOLVERSTACK/TestResultsResolverFunction`, `APIRESOLVERSTACK/TestRunnerFunction`, `APIRESOLVERSTACK/TestSetResolverFunction`, `APIRESOLVERSTACK/UploadResolverFunction`, `AgentChatProcessorFunction`, `AgentCoreGatewayManagerFunction`, `AgentCoreMCPHandlerFunction`, `AgentProcessorFunction`, `BlueprintOptimizationFunction`, `CalculateCapacityFunction`, `ChatStreamProcessorFunction`, `ChatWithDocumentProcessorFunction`, `CognitoUserPoolEmailDomainVerifyFunction`, `DOCUMENTKB/CreateOSSIndexLambdaFunction`, `DOCUMENTKB/GetAdjustedStackNameFunction`, `DOCUMENTKB/GetSeedUrlsFunction`, `DOCUMENTKB/S3VectorManagerFunction`, `DOCUMENTKB/StartIngestionJobFunction`, `DashboardMergerFunction`, `DiscoveryProcessorFunction`, `ExternalIdPGroupMappingFunction`, `FccDatasetDeployerFunction`, `GetDomainLambda`, `InitializeConcurrencyTableLambda`, `LookupFunction`, `McpCredentialManagerFunction`, `McpDefaultCredentialFunction`, `McpIdentityInterceptorFunction`, `McpInterceptorAttachFunction`, `MeteringHourMigrationFunction`, `OcrBenchmarkDeployerFunction`, `PATTERNSTACK/BDAOCRProjectFunction`, `PATTERNSTACK/CodeBuildTrigger`, `PATTERNSTACK/EvaluationFunction`, `PostProcessingDecompressor`, `QueueProcessor`, `QueueSender`, `ResolveOIDCClientSecretFunction`, `ReviewQueueSweeperFunction`, `RvlCdipNmpDeployerFunction`, `SaveReportingDataFunctionV2`, `StartUICodeBuild`, `TestFileCopierFunction`, `TestRunSweeperFunction`, `TestSetBucketAutoDeleteFunction`, `TestSetBucketNotificationFunction`, `TestSetFileCopierFunction`, `UpdateConfigurationFunction`, `UpdateSettingsFunction`, `W2DatasetDeployerFunction`, `WorkflowTracker` |
| cfn_nag `W89` | Function does not require VPC access as it only interacts with DynamoDB | `BackfillWorkerFunction` |
| cfn_nag `W89` | Function does not require VPC access as it only interacts with DynamoDB via AWS APIs | `APIRESOLVERSTACK/NotificationsResolverFunction` |
| cfn_nag `W89` | Function does not require VPC access as it only interacts with DynamoDB, S3, SQS and Cognito via AWS APIs | `APIRESOLVERSTACK/BulkReviewResolverFunction`, `APIRESOLVERSTACK/BulkReviewWorkerFunction`, `APIRESOLVERSTACK/ReviewQueuesResolverFunction` |
| cfn_nag `W89` | Function does not require VPC access as it only interacts with S3 | `ConfigurationCopyFunction` |
| cfn_nag `W89` | Function does not require VPC access as it only interacts with local agent factory | `APIRESOLVERSTACK/ListAvailableAgentsFunction` |
| cfn_nag `W89` | Function does not require VPC access — only reads from public S3 bucket via unsigned requests | `VersionCheckResolverFunction` |
| cfn_nag `W89` | Function interacts only with AWS APIs; no VPC access required | `APIRESOLVERSTACK/HttpApiDispatcherFunction` |
| cfn_nag `W89` | Function only calls Cognito control-plane APIs; no VPC access required | `WebUIClientOAuthUrlsFunction` |
| cfn_nag `W89` | Function only interacts with AWS APIs (Athena, Glue, CloudWatch, S3) — no VPC needed | `DataMartRollupFunction` |
| cfn_nag `W89` | Lambda function does not need to be deployed inside a VPC | `APIRESOLVERSTACK/SyncBdaIdpResolverFunction` |
| cfn_nag `W89` | Lambda function does not need to be in VPC | `TestSetZipExtractorFunction` |
| cfn_nag `W89` | This Lambda function does not require VPC access as it only interacts with AWS services via AWS APIs | `PATTERNSTACK/AssessmentFunction`, `PATTERNSTACK/BDACompletionFunction`, `PATTERNSTACK/ClassificationFunction`, `PATTERNSTACK/ExtractionFunction`, `PATTERNSTACK/InvokeBDAFunction`, `PATTERNSTACK/MLflowLoggerFunction`, `PATTERNSTACK/OCRFunction`, `PATTERNSTACK/ProcessResultsFunction`, `PATTERNSTACK/RuleValidationFunction`, `PATTERNSTACK/RuleValidationOrchestrationFunction`, `PATTERNSTACK/RuleValidationPolicyClassificationFunction`, `PATTERNSTACK/ShardRuntimeFunction`, `PATTERNSTACK/SummarizationFunction`, `PATTERNSTACK/TestExecutionAggregationFunction` |
| cfn_nag `W89` | This Lambda function does not require VPC access as it only interacts with AWS services via AWS APIs — placed in VPC when ApiGatewayVisibility=PRIVATE | `PATTERNSTACK/BDAProcessResultsFunction` |
| cfn_nag `W89` | VPC attachment is conditional on ApiGatewayVisibility=PRIVATE (see VpcConfig below). The resolver must live in the same VPC as the private REST API so its DynamoDB reads can traverse a gateway endpoint. On the default/public path the resolver only talks to DynamoDB and Lambda over AWS-managed networking and VPC access is not required. | `APIRESOLVERSTACK/CircuitBreakerResolverFunction` |
| cfn_nag `W89` | VPC attachment is conditional: required when ApiGatewayVisibility=PRIVATE (the private REST API and its callers only resolve inside the VPC) or DeployInVPC=true (DynamoDB reached via gateway endpoint). On the default/public path the function only talks to DynamoDB, SNS, and CloudWatch over AWS-managed networking and VPC access is not required. | `CircuitBreakerManagerFunction` |
| cfn_nag `W92` | Concurrency is bounded by the event source mapping's MaximumConcurrency and the job lock, not reserved concurrency | `APIRESOLVERSTACK/BulkReviewWorkerFunction` |
| cfn_nag `W92` | Custom resource scales on demand; no reserved concurrency needed | `WebUIClientOAuthUrlsFunction` |
| cfn_nag `W92` | Function does not require concurrent execution limits as it is designed to scale based on demand | `PATTERNSTACK/AssessmentFunction`, `PATTERNSTACK/BDACompletionFunction`, `PATTERNSTACK/BDAProcessResultsFunction`, `PATTERNSTACK/ClassificationFunction`, `PATTERNSTACK/ExtractionFunction`, `PATTERNSTACK/InvokeBDAFunction`, `PATTERNSTACK/MLflowLoggerFunction`, `PATTERNSTACK/OCRFunction`, `PATTERNSTACK/ProcessResultsFunction`, `PATTERNSTACK/RuleValidationFunction`, `PATTERNSTACK/RuleValidationOrchestrationFunction`, `PATTERNSTACK/RuleValidationPolicyClassificationFunction`, `PATTERNSTACK/ShardRuntimeFunction`, `PATTERNSTACK/SummarizationFunction`, `PATTERNSTACK/TestExecutionAggregationFunction` |
| cfn_nag `W92` | Function does not require reserved concurrency | `APIRESOLVERSTACK/ListDocumentsGSIResolverFunction`, `BackfillTriggerFunction`, `BackfillWorkerFunction`, `CalculateCapacityResolverFunction`, `CircuitBreakerManagerFunction`, `CompleteSectionReviewFunction`, `FinetuningDeploymentHandlerFunction`, `FinetuningJobCreatorFunction`, `FinetuningJobStatusCheckerFunction`, `FinetuningJobsResolverFunction`, `FinetuningListDocumentsFunction`, `FinetuningMergeDataFunction`, `FinetuningProcessDocumentFunction`, `MULTIDOCDISCOVERYSTACK/DockerBuildRunFunction`, `MULTIDOCDISCOVERYSTACK/MultiDocDiscoveryAnalyzeFunction`, `MULTIDOCDISCOVERYSTACK/MultiDocDiscoveryClusterFunction`, `MULTIDOCDISCOVERYSTACK/MultiDocDiscoveryEmbedFunction`, `MULTIDOCDISCOVERYSTACK/MultiDocDiscoverySaveFunction`, `MultiDocDiscoveryPrepareFunction`, `UserManagementFunction`, `UserSyncFunction`, `VersionCheckResolverFunction` |
| cfn_nag `W92` | Function does not require reserved concurrency (one-shot CFN custom resource) | `MeteringHourMigrationFunction` |
| cfn_nag `W92` | Function does not require reserved concurrency as it scales based on demand | `APIRESOLVERSTACK/AbortTestRunsResolverFunction`, `APIRESOLVERSTACK/AbortWorkflowResolverFunction`, `APIRESOLVERSTACK/AgentChatResolverFunction`, `APIRESOLVERSTACK/AgentRequestHandlerFunction`, `APIRESOLVERSTACK/CircuitBreakerResolverFunction`, `APIRESOLVERSTACK/ConfigurationResolverFunction`, `APIRESOLVERSTACK/CopyToBaselineResolverFunction`, `APIRESOLVERSTACK/CreateDocumentResolverFunction`, `APIRESOLVERSTACK/DeleteAgentChatSessionFunction`, `APIRESOLVERSTACK/DeleteDocumentResolverFunction`, `APIRESOLVERSTACK/DeleteTestsResolverFunction`, `APIRESOLVERSTACK/DiscoveryUploadResolverFunction`, `APIRESOLVERSTACK/DocumentVersionsResolverFunction`, `APIRESOLVERSTACK/GetAgentChatMessagesFunction`, `APIRESOLVERSTACK/GetFileContentsResolverFunction`, `APIRESOLVERSTACK/GetSampleDocumentResolverFunction`, `APIRESOLVERSTACK/GetStepFunctionExecutionResolverFunction`, `APIRESOLVERSTACK/ListAgentChatSessionsFunction`, `APIRESOLVERSTACK/ListAvailableAgentsFunction`, `APIRESOLVERSTACK/ListDocumentsByDateRangeResolverFunction`, `APIRESOLVERSTACK/ProcessChangesResolverFunction`, `APIRESOLVERSTACK/QueryKnowledgeBaseResolverFunction`, `APIRESOLVERSTACK/ReprocessDocumentResolverFunction`, `APIRESOLVERSTACK/SendChatDocumentMessageResolverFunction`, `APIRESOLVERSTACK/TestResultsResolverFunction`, `APIRESOLVERSTACK/TestRunnerFunction`, `APIRESOLVERSTACK/TestSetResolverFunction`, `APIRESOLVERSTACK/UploadResolverFunction`, `AgentChatProcessorFunction`, `AgentCoreGatewayManagerFunction`, `AgentCoreMCPHandlerFunction`, `AgentProcessorFunction`, `BlueprintOptimizationFunction`, `CalculateCapacityFunction`, `ChatStreamProcessorFunction`, `ChatWithDocumentProcessorFunction`, `CognitoUserPoolEmailDomainVerifyFunction`, `DOCUMENTKB/CreateOSSIndexLambdaFunction`, `DOCUMENTKB/GetAdjustedStackNameFunction`, `DOCUMENTKB/GetSeedUrlsFunction`, `DOCUMENTKB/S3VectorManagerFunction`, `DOCUMENTKB/StartIngestionJobFunction`, `DashboardMergerFunction`, `DiscoveryProcessorFunction`, `ExternalIdPGroupMappingFunction`, `FccDatasetDeployerFunction`, `GetDomainLambda`, `InitializeConcurrencyTableLambda`, `LookupFunction`, `McpCredentialManagerFunction`, `McpIdentityInterceptorFunction`, `OcrBenchmarkDeployerFunction`, `PATTERNSTACK/BDAOCRProjectFunction`, `PATTERNSTACK/CodeBuildTrigger`, `PATTERNSTACK/EvaluationFunction`, `PostProcessingDecompressor`, `QueueProcessor`, `QueueSender`, `ResolveOIDCClientSecretFunction`, `RvlCdipNmpDeployerFunction`, `SaveReportingDataFunctionV2`, `StartUICodeBuild`, `TestFileCopierFunction`, `TestSetBucketAutoDeleteFunction`, `TestSetBucketNotificationFunction`, `TestSetFileCopierFunction`, `UpdateConfigurationFunction`, `UpdateSettingsFunction`, `W2DatasetDeployerFunction`, `WorkflowTracker` |
| cfn_nag `W92` | Function does not require reserved concurrency as it scales with UI polling demand | `APIRESOLVERSTACK/NotificationsResolverFunction` |
| cfn_nag `W92` | Function does not require reserved concurrency as it scales with reviewer UI demand | `APIRESOLVERSTACK/BulkReviewResolverFunction`, `APIRESOLVERSTACK/ReviewQueuesResolverFunction` |
| cfn_nag `W92` | Function does not require reserved concurrency; it runs only during stack operations | `McpDefaultCredentialFunction`, `McpInterceptorAttachFunction` |
| cfn_nag `W92` | Lambda function does not need reserved concurrency | `APIRESOLVERSTACK/SyncBdaIdpResolverFunction`, `TestSetZipExtractorFunction` |
| cfn_nag `W92` | One-shot per stack update; no ReservedConcurrentExecutions required | `DataMartMigrationDispatcherFunction` |
| cfn_nag `W92` | Reserved concurrency not required | `ApiHandlerFunction`, `BatchPreProcessorFunction`, `ConfigurationCopyFunction`, `CustomerManagedKmsKeyCheckFunction`, `ReadPreviousIDPPatternFunction`, `StacknameCheckFunction` |
| cfn_nag `W92` | Reserved concurrency not required; scales on demand | `APIRESOLVERSTACK/HttpApiDispatcherFunction` |
| cfn_nag `W92` | ReservedConcurrentExecutions=12 pins concurrency (was 1, bumped to accommodate the migration state machine's Map MaxConcurrency=8 + 3 scheduled crons + 1 headroom); the setting is present, not absent | `DataMartRollupFunction` |
| checkov `CKV_AWS_173` | Env vars are non-sensitive (table names, ARNs) | `APIRESOLVERSTACK/HttpApiDispatcherFunction` |
| checkov `CKV_AWS_173` | Environment variables contain non-sensitive configuration | `FinetuningDeploymentHandlerFunction`, `FinetuningJobCreatorFunction`, `FinetuningJobStatusCheckerFunction`, `FinetuningJobsResolverFunction`, `FinetuningListDocumentsFunction`, `FinetuningMergeDataFunction`, `FinetuningProcessDocumentFunction` |
| checkov `CKV_AWS_173` | Environment variables contain non-sensitive configuration values | `APIRESOLVERSTACK/AbortTestRunsResolverFunction`, `APIRESOLVERSTACK/DeleteTestsResolverFunction`, `APIRESOLVERSTACK/TestResultsResolverFunction`, `APIRESOLVERSTACK/TestSetResolverFunction`, `MeteringHourMigrationFunction`, `TestSetZipExtractorFunction` |
| checkov `CKV_AWS_173` | Environment variables contain non-sensitive configuration values (Cognito pool and group IDs) | `UserManagementFunction`, `UserSyncFunction` |
| checkov `CKV_AWS_173` | Environment variables contain non-sensitive configuration values (DynamoDB table name) | `CompleteSectionReviewFunction` |
| checkov `CKV_AWS_173` | Environment variables contain non-sensitive configuration values (bucket name, prefix, log level) | `VersionCheckResolverFunction` |
| checkov `CKV_AWS_173` | Environment variables contain non-sensitive configuration values (bucket names and log level) | `TestFileCopierFunction` |
| checkov `CKV_AWS_173` | Environment variables contain non-sensitive configuration values (log level and feature flags) | `CalculateCapacityFunction` |
| checkov `CKV_AWS_173` | Environment variables contain non-sensitive configuration values (log level only) | `BackfillTriggerFunction`, `CalculateCapacityResolverFunction`, `CustomerManagedKmsKeyCheckFunction`, `ReadPreviousIDPPatternFunction` |
| checkov `CKV_AWS_173` | Environment variables contain only configuration | `DataMartMigrationDispatcherFunction`, `DataMartRollupFunction` |
| checkov `CKV_AWS_173` | Environment variables contain only non-sensitive configuration | `ChatStreamProcessorFunction`, `ChatWithDocumentProcessorFunction` |
| checkov `CKV_AWS_173` | Environment variables contain only non-sensitive configuration values (log levels, table names) - no secrets or PII | `APIRESOLVERSTACK/ListDocumentsGSIResolverFunction` |
| checkov `CKV_AWS_173` | Environment variables do not contain sensitive data | `APIRESOLVERSTACK/CircuitBreakerResolverFunction`, `APIRESOLVERSTACK/SendChatDocumentMessageResolverFunction`, `BackfillWorkerFunction`, `CircuitBreakerManagerFunction`, `PATTERNSTACK/ShardRuntimeFunction` |
| checkov `CKV_AWS_173` | Environment variables do not contain sensitive data - only configuration values | `APIRESOLVERSTACK/ListDocumentsByDateRangeResolverFunction`, `PATTERNSTACK/MLflowLoggerFunction`, `PATTERNSTACK/RuleValidationOrchestrationFunction`, `PATTERNSTACK/TestExecutionAggregationFunction` |
| checkov `CKV_AWS_173` | Environment variables do not contain sensitive data - only configuration values like feature flags and non-sensitive settings | `APIRESOLVERSTACK/AbortWorkflowResolverFunction`, `APIRESOLVERSTACK/AgentChatResolverFunction`, `APIRESOLVERSTACK/AgentRequestHandlerFunction`, `APIRESOLVERSTACK/ConfigurationResolverFunction`, `APIRESOLVERSTACK/CopyToBaselineResolverFunction`, `APIRESOLVERSTACK/CreateDocumentResolverFunction`, `APIRESOLVERSTACK/DeleteAgentChatSessionFunction`, `APIRESOLVERSTACK/DeleteDocumentResolverFunction`, `APIRESOLVERSTACK/DiscoveryUploadResolverFunction`, `APIRESOLVERSTACK/DocumentVersionsResolverFunction`, `APIRESOLVERSTACK/GetAgentChatMessagesFunction`, `APIRESOLVERSTACK/GetStepFunctionExecutionResolverFunction`, `APIRESOLVERSTACK/ListAgentChatSessionsFunction`, `APIRESOLVERSTACK/ListAvailableAgentsFunction`, `APIRESOLVERSTACK/ProcessChangesResolverFunction`, `APIRESOLVERSTACK/QueryKnowledgeBaseResolverFunction`, `APIRESOLVERSTACK/ReprocessDocumentResolverFunction`, `APIRESOLVERSTACK/SyncBdaIdpResolverFunction`, `APIRESOLVERSTACK/TestRunnerFunction`, `APIRESOLVERSTACK/UploadResolverFunction`, `AgentChatProcessorFunction`, `AgentCoreGatewayManagerFunction`, `AgentCoreMCPHandlerFunction`, `AgentProcessorFunction`, `BlueprintOptimizationFunction`, `CognitoUserPoolEmailDomainVerifyFunction`, `ConfigurationCopyFunction`, `DOCUMENTKB/CreateOSSIndexLambdaFunction`, `DOCUMENTKB/GetAdjustedStackNameFunction`, `DOCUMENTKB/GetSeedUrlsFunction`, `DOCUMENTKB/S3VectorManagerFunction`, `DOCUMENTKB/StartIngestionJobFunction`, `DashboardMergerFunction`, `DiscoveryProcessorFunction`, `FccDatasetDeployerFunction`, `InitializeConcurrencyTableLambda`, `LookupFunction`, `OcrBenchmarkDeployerFunction`, `PATTERNSTACK/AssessmentFunction`, `PATTERNSTACK/BDACompletionFunction`, `PATTERNSTACK/BDAProcessResultsFunction`, `PATTERNSTACK/ClassificationFunction`, `PATTERNSTACK/EvaluationFunction`, `PATTERNSTACK/ExtractionFunction`, `PATTERNSTACK/InvokeBDAFunction`, `PATTERNSTACK/OCRFunction`, `PATTERNSTACK/ProcessResultsFunction`, `PATTERNSTACK/RuleValidationFunction`, `PATTERNSTACK/RuleValidationPolicyClassificationFunction`, `PATTERNSTACK/SummarizationFunction`, `PostProcessingDecompressor`, `QueueProcessor`, `QueueSender`, `RvlCdipNmpDeployerFunction`, `SaveReportingDataFunctionV2`, `StacknameCheckFunction`, `TestSetFileCopierFunction`, `UpdateConfigurationFunction`, `W2DatasetDeployerFunction`, `WorkflowTracker` |
| checkov `CKV_AWS_173` | Environment variables do not contain sensitive data - only group name mappings | `ExternalIdPGroupMappingFunction` |
| checkov `CKV_AWS_173` | Environment variables hold no sensitive data | `McpDefaultCredentialFunction`, `McpInterceptorAttachFunction` |
| checkov `CKV_AWS_173` | Environment variables hold only a table name, log level and stack name - no sensitive data | `APIRESOLVERSTACK/NotificationsResolverFunction` |
| checkov `CKV_AWS_173` | Environment variables hold only resource names and settings - no sensitive data | `ReviewQueueSweeperFunction`, `TestRunSweeperFunction` |
| checkov `CKV_AWS_173` | Environment variables hold only resource names, log level and stack name - no sensitive data | `APIRESOLVERSTACK/BulkReviewResolverFunction`, `APIRESOLVERSTACK/BulkReviewWorkerFunction`, `APIRESOLVERSTACK/ReviewQueuesResolverFunction` |
| checkov `CKV_AWS_173` | Environment variables hold resource names and ids only | `McpCredentialManagerFunction`, `McpIdentityInterceptorFunction` |
| checkov `CKV_AWS_173` | No sensitive env vars | `MULTIDOCDISCOVERYSTACK/DockerBuildRunFunction`, `MULTIDOCDISCOVERYSTACK/MultiDocDiscoveryAnalyzeFunction`, `MULTIDOCDISCOVERYSTACK/MultiDocDiscoveryClusterFunction`, `MULTIDOCDISCOVERYSTACK/MultiDocDiscoveryEmbedFunction`, `MULTIDOCDISCOVERYSTACK/MultiDocDiscoverySaveFunction`, `MultiDocDiscoveryPrepareFunction` |
| checkov `CKV_AWS_99` | Encryption is already enabled with SSE-KMS | `DocumentSectionsCrawlerSecurityConfigurationV2` |

<!-- uncarried-suppressions:end -->

</details>

## Differences from CloudFormation and current limitations

A Terraform deployment runs the same functions with the same configuration as a
CloudFormation deployment of the same version, but these things differ:

- The feature-platform extensions (see [Feature Platform](./feature-platform.md)) still
  install as their own CloudFormation stacks, beside the Terraform deployment.
- A headless or GovCloud module is published only by a publish that also produced
  that template variant (`idp-cli publish --terraform --headless --govcloud`). If
  `idp-terraform-<variant>-latest.json` is not present in your Region's bucket, that
  variant has not been published there as a module.
- `aws cloudformation list-stack-resources --stack-name <name>` shows only the label's
  one resource. The translation to the deployment's real resources is done by the
  SDK's hook, not by AWS, so the AWS CLI, the CloudFormation console's Resources tab
  and any tool that does not import `idp_sdk` see only the label. Use `terraform state
  list`, or the resource directory named by `TerraformResourceDirectory`.
- The reporting data-mart rollup function looks the deployment's stacks up through
  CloudFormation from inside the deployment, and finds its functions by a stack-name
  tag. In a Terraform deployment the module gives that function the resource
  directory's location (`IDP_TF_RESOURCE_DIRECTORY`) and read access to that one
  object, `idp_common` answers its stack lookups from it, and every function carries
  an `idp:stack-name` tag in place of CloudFormation's reserved
  `aws:cloudformation:stack-name`. A CloudFormation deployment never sets that
  variable and is unaffected.
- Physical names differ. CloudFormation generates names like
  `<stack>-<LogicalId>-<random>`; the module generates `<stack>-<LogicalId>-<hash>`
  for most resources, and Cloud Control generates its own names for `awscc` resources
  the template leaves unnamed.
- The module adds resources of its own: the custom-resource bridge (a Lambda function
  named `<stack>-cfn-bridge`, its role, log group and an S3 bucket), the label stack
  with its template and resource directory in that bucket, and the IAM-propagation
  waits.
- A failed apply is not rolled back, as described under [Deploying](#deploying).
- The template's `Rules` are checked at plan rather than at submit, and a rule whose
  inputs are not known until apply is checked during the apply; see
  [Using the module beside your own resources](#using-the-module-beside-your-own-resources).
- The full stack has been applied, re-planned with no changes, and destroyed with both
  Terraform and OpenTofu, in a development account. The standard module has also been
  applied with `DeployInVPC=true` into a VPC with private subnets, NAT egress and an
  `execute-api` interface endpoint, in `us-east-1` with Terraform: it re-planned with no
  changes, processed a sample document end to end with the pipeline's functions in the
  VPC, and was destroyed. The headless and GovCloud modules are validated offline on
  every pull request, but neither has been applied.
- `terraform validate` and `plan` print deprecation warnings for `hash_key` and
  `range_key` on the DynamoDB tables. They are warnings from version 6 of the `aws`
  provider and do not affect the deployment.

## Related documentation

- [Migrating from genai-idp-terraform](./terraform-migration.md)
- [Deployment Guide](./deployment.md)
- [IDP CLI](./idp-cli.md)
- [IDP SDK](./idp-sdk.md)
- [How the translator works](../terraform/README.md), for contributors
