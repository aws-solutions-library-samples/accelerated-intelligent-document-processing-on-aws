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
well. That repository is a different implementation, and its state cannot be moved
onto this module.

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
  and its related actions unless you turn the label off. A deploying role that is
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
`terraform init` and extracts it. For the public release, the bucket and prefix per
Region are the ones in the Launch Stack table of the
[Deployment Guide](./deployment.md#option-1-one-click-cloudformation-console-deployment-recommended-for-first-time-users).
If `idp-terraform-latest.json` is not present in your Region's bucket, no Terraform
module has been published there yet; you can build and publish one yourself with
`idp-cli publish --terraform` (see [IDP CLI](./idp-cli.md#publish)).

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
  source = "https://s3.us-west-2.amazonaws.com/<bucket>/<prefix>/<version>/terraform/idp-terraform-<version>.zip"

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
constraints above only need to be compatible with them. The module's outputs keep the
CloudFormation output names (`module.idp.ApplicationWebURL`,
`module.idp.S3InputBucketName`, `module.idp.StateMachineArn`, and so on); its
`outputs.tf` lists them all.

As with a CloudFormation deployment, the first apply sends a temporary password to
`admin_email`, and a second email asks you to confirm the alerts subscription. Confirm
it, or alarm notifications reach nobody (see
[Who receives the alerts](./monitoring.md#who-receives-the-alerts)).

A first apply of the full stack takes roughly half an hour. Terraform does not roll
back a failed apply the way CloudFormation rolls back a failed stack: what was created
stays in state. Fix the cause and run `terraform apply` again, or run
`terraform destroy`.

### With `idp-cli deploy --terraform`

`idp-cli` can write and apply a root configuration for you:

```bash
idp-cli deploy --terraform \
    --stack-name idp-tf \
    --admin-email admin@example.com \
    --parameters "MaxConcurrentWorkflows=50,LogLevel=INFO"
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
3. A check of the plan. If any change deletes or replaces a resource that
   CloudFormation would have retained (`DeletionPolicy: Retain`, listed as
   `retainedAddresses` in the module's `idp-terraform.json`, for the installed module
   and the new one), the command stops, lists those addresses, and applies nothing.
   Losing a retained bucket or table loses its data, which CloudFormation never does
   on an update. `--terraform-allow-replace-retained` applies such a plan anyway.
4. `terraform apply` of exactly that saved plan. The plan file is removed afterwards,
   because it holds your settings too.

The options are:

| Option | Meaning |
|---|---|
| `--terraform` | deploy with Terraform instead of CloudFormation |
| `--stack-name` | the deployment name, which becomes `stack_name` |
| `--admin-email` | the administrator's email address, which becomes `admin_email` |
| `--parameters "K=V,K2=V2"` | settings by their CloudFormation parameter names, as on the CloudFormation path; an unknown name is an error |
| `--terraform-module-url URL` | the module source URL; by default the CLI uses the latest published module for the Region, chosen the same way the CloudFormation deploy chooses its default template |
| `--headless` / `--govcloud` | use the latest headless or GovCloud module instead of the standard one; they cannot be combined with each other or with `--terraform-module-url`, and `--admin-email` is ignored with `--headless` |
| `--terraform-dir DIR` | the directory to write, instead of `./idp-terraform/<stack-name>` |
| `--terraform-bin` | `terraform` or `tofu` |
| `--terraform-allow-replace-retained` | apply even if the plan deletes or replaces a resource CloudFormation would retain, losing its data; without it such a plan is refused and nothing is applied |
| `--wait` | accepted as on the CloudFormation path |

`--parameters` takes CloudFormation names, not Terraform variable names. The CLI maps
each one to its variable through the `idp-terraform.json` file shipped in the module,
so `--parameters` lines you already use with CloudFormation deployments work
unchanged.

Terraform state is kept locally in that directory unless you put a backend there
first. Keep the directory: it is the only record Terraform has of what it deployed,
and without it neither `idp-cli` nor Terraform can update or delete the deployment.
To keep the state remotely, create the directory before the first deploy and put a
`terraform { backend "s3" { ... } }` block in a file of its own, such as `backend.tf`.
`idp-cli` writes `main.tf` itself and marks it with a comment; it does not overwrite a
`main.tf` it did not write.

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
`--force` to skip the confirmation. The command empties the deployment's buckets the
way the CloudFormation delete path does, then removes from Terraform state every
resource whose CloudFormation `DeletionPolicy` keeps it (`Retain` or
`RetainExceptOnCreate`), and then runs `terraform destroy`. Removing a resource from
state means Terraform forgets it without deleting it, so the resources CloudFormation
would retain are retained here too: the logging bucket, the data buckets and the other
resources the templates mark that way. The command reports which retained resources
were left, as the CloudFormation path does.

If you deployed with your own root configuration, do the same steps yourself. The
module's `idp-terraform.json` lists the retained resources in `retainedAddresses`,
relative to the module and without instance keys. After `terraform init` the module is
normally extracted under `.terraform/modules/idp/`. The following loop removes every
instance of those resources from state; it does what `idp-cli` does but has not itself
been run against a live deployment, so check its output before you destroy:

```bash
jq -r '.retainedAddresses[]' .terraform/modules/idp/idp-terraform.json > retained.txt
terraform state list | while read -r addr; do
  bare=$(printf '%s' "$addr" | sed -E 's/\[[^]]*\]//g')
  if grep -qxF "${bare#module.idp.}" retained.txt; then
    terraform state rm "$addr"
  fi
done
terraform destroy
```

Empty any bucket that is not retained before `terraform destroy` if it holds objects.
Retained resources keep running up costs (storage, mostly) until you delete them
yourself.

## Upgrading

To upgrade, point the module at the new release's URL and apply:

1. Change `source` in the `module "idp"` block to the new version's zip URL.
2. Run `terraform init`, which downloads the new module. Terraform refuses to plan
   with a changed module source until you do.
3. Run `terraform plan` and read it. Replacements of stateful resources (buckets,
   tables, the user pool) deserve a close look before you apply.
4. Run `terraform apply`.

The label stack's description and metadata move to the new version in the same apply.
With `idp-cli`, run `idp-cli deploy --terraform` again with the same `--stack-name`
and `--terraform-dir`, and the new `--terraform-module-url` (or none, for the latest).
You do not need to repeat `--parameters`: `idp-cli` remembers the values of the
previous deploy, by CloudFormation parameter name, in `idp-cli-parameters.json` in that
directory, and applies only the parameters you pass over them, as a CloudFormation
update keeps the parameters it is not given. A remembered parameter the new module no
longer has is dropped with a warning. The deploy runs `init -upgrade` because the
module source changed, and refuses the plan if it would delete or replace a retained
resource (see [With `idp-cli deploy --terraform`](#with-idp-cli-deploy---terraform)).

A deployment generated before the module named its IAM roles has roles named
`terraform-<timestamp>`. The first upgrade to a module that names them replaces those
roles: the new role is created first, functions and other resources that use it switch
to it in place, and the old role is deleted last. For the seconds between the old
role's policies being removed and a function switching to the new role, calls that
function makes with the old role can be refused, so run that upgrade when the
deployment is idle. Retained roles keep their existing names and are not replaced.

Read the release's `CHANGELOG.md` entries and any migration guide for the release
before upgrading, as for a CloudFormation stack: configuration changes and upgrade
actions apply to both.

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
- The full stack has been applied, re-planned with no changes, and destroyed with both
  Terraform and OpenTofu, in a development account. The headless and GovCloud
  modules are validated offline on every pull request, but neither has been applied.
- `terraform validate` and `plan` print deprecation warnings for `hash_key` and
  `range_key` on the DynamoDB tables. They are warnings from version 6 of the `aws`
  provider and do not affect the deployment.

## Related documentation

- [Migrating from genai-idp-terraform](./terraform-migration.md)
- [Deployment Guide](./deployment.md)
- [IDP CLI](./idp-cli.md)
- [IDP SDK](./idp-sdk.md)
- [How the translator works](../terraform/README.md), for contributors
