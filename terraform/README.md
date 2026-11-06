# Terraform module generation

This directory holds the tooling that turns the accelerator's CloudFormation templates
into a Terraform module, and the evidence that the result deploys. It is the developer
tier of the Terraform documentation; the user guide is
[docs/terraform.md](../docs/terraform.md).

| Path | What it is |
|---|---|
| `translator/` | the `idp_tf` package: CloudFormation in, Terraform module tree out |
| `translator/schemas/awscc.json` | the vendored subset of the `hashicorp/awscc` provider schema the translator reads |
| `translator/scripts/refresh_awscc_schema.py` | regenerates that subset |
| `translator/tests/` | unit tests and the offline per-template gate |
| `bridge/` | the Lambda function that runs CloudFormation custom-resource handlers for Terraform |
| `prototype/` | root-module fixtures and a property comparison script used for live validation, and the raw notes of those runs (`RESULTS.md`) |

## How the translator works

### The pipeline

`python -m idp_tf.cli` (run from `terraform/translator`, or with that directory on
`PYTHONPATH`) runs these stages:

1. **Load.** `expand.load_template` reads the template YAML. Two inputs are supported.
   A *published* main template (the `idp-main.yaml` the publisher packaged, whose code
   locations are already `s3://` URIs) is the release path; its nested templates are
   fetched by their S3 `TemplateURL` into `--nested-cache`. With `--offline`, the
   source `template.yaml` of a checkout is read instead: `offline_placeholders`
   replaces local code paths with placeholders and `local_loader` resolves each
   nested stack's `./<dir>/.aws-sam/packaged.yaml` to `<dir>/template.yaml`. The
   offline form needs no build and no AWS access, which is what makes it usable as a
   per-pull-request gate; its output is not deployable.
2. **Expand.** `expand.expand` runs the SAM transform locally with
   `aws-sam-translator`, so the translator only ever sees plain CloudFormation. It
   normalises the details SAM leaves behind (YAML dates read as text, `arn:aws:`
   managed-policy ARNs made partition-safe).
3. **Plan.** `Translator._plan_all` assigns every resource one of four kinds:
   - `custom` for `Custom::*` and `AWS::CloudFormation::CustomResource`, run through
     the bridge;
   - `module` for `AWS::CloudFormation::Stack`, which becomes a child module, itself
     translated recursively;
   - `aws` for a type with a hand mapping to `hashicorp/aws`, when the mode is
     `hybrid` and the type is in `aws_mappers.HYBRID_TYPES`, or when `awscc` does not
     have the type at all;
   - `awscc` for everything else, generated from the `awscc` schema.
   Resources whose `DeletionPolicy` is anything but `Delete` are recorded as retained.
4. **Render.** Each plan is emitted as HCL, and conditions, mappings, parameters and
   outputs become `locals.tf`, `variables.tf` and `outputs.tf`. Intrinsic functions are
   translated to Terraform expressions.
5. **Write.** `cli.write_module` writes the tree: the root module, `modules/<nested>/`
   per nested stack, `versions.tf`, `bridge.tf` when the template has custom
   resources, `label.tf` when `--label-template` and `--version` are given,
   deterministic zips under `zips/`, a `translation-report.json`, and at the root
   `idp-terraform.json`. That last file is the contract with `idp-cli`:
   `{formatVersion, version, parameters: {<CfnName>: {variable, type, required,
   sensitive}}, retainedAddresses: [...]}`, the parameter-name-to-variable map
   `idp-cli deploy --terraform --parameters` uses and the addresses
   `idp-cli delete --terraform` removes from state before destroying.

There is no best-effort path. A type, property, resource key or intrinsic the
translator does not understand raises `TranslationError` naming the resource and the
property, and the run fails. `--survey` collects every such failure instead of stopping
at the first, which is how to see the whole of what a template change needs.

### Provider mapping

The default mode, `hybrid`, sends the types in `aws_mappers.HYBRID_TYPES` to
hand-written `hashicorp/aws` mappings (`aws_mappers.py` and `aws_mappers_storage.py`),
and everything else the `awscc` schema has to generated `awscc` resources. A type is
hand-mapped for one of three reasons, each found by a live deploy or re-plan rather
than by reading:

- `awscc` has no resource for it (CodeBuild projects, Cognito user pools, the
  CloudFront distribution, the WAF web ACL, SQS queue policies, inline IAM policies);
- Cloud Control fails on or ignores a property (an internal error creating EventBridge
  rules; `EmptyOnDelete` ignored on an ECR repository, because Cloud Control's delete
  receives only the identifier);
- the service normalises what it stores, so `awscc`, which compares literally, shows a
  change on every plan (sorted lists on S3 buckets and DynamoDB tables, re-encoded
  bucket policies, a function name returned as an ARN on event source mappings, an
  account principal returned as the root ARN on Lambda permissions, dashboard bodies,
  Cognito user attributes, and IAM policy documents, where IAM stores a one-element
  array as a scalar). `hashicorp/aws` compares these semantically.

`--mode awscc` sends everything `awscc` supports to `awscc`. It exists for comparison;
it does not produce an empty re-plan or a clean destroy.

`awscc` attribute names are the CloudFormation property names in snake_case, because
the provider is generated from the CloudFormation registry. `awscc_schema.py` matches
them on a normalised key, exactly rather than fuzzily, and an ambiguous match is an
error.

CloudFormation does several things implicitly that the translation has to do
explicitly:

- **Plan-time conditions across modules.** A nested stack's condition can read a
  parameter the parent fills with a resource attribute, which is unknown at plan time,
  while `count` must be known. The parent therefore also passes a `<variable>__cond`
  stand-in that is known at plan and never empty, which is exactly what
  CloudFormation's condition sees.
- **Dependencies.** A reference to a nested stack waits for the whole module.
- **Pseudo-parameters** are read once at the root and passed down, because a data
  source inside a module with `depends_on` is deferred to apply.
- **`Ref` on composite identifiers.** See `AWSCC_REF_ATTR` below.
- **IAM propagation.** A `time_sleep` after each role and its policies, and one after
  the bridge role before Lambda accepts it as a principal.
- **`cfnresponse`.** CloudFormation makes it importable for inline Python functions;
  a stand-in is packaged with each inline function that imports it.
- **Deterministic zips.** Inline-function and bridge zips are built at translation
  time, because a zip built at plan time inside a module with `depends_on` is unknown
  on every plan.

### The bridge

`bridge/index.py` runs CloudFormation custom-resource handlers. Each custom resource
becomes an `aws_lambda_invocation` with `lifecycle_scope = "CRUD"` against the bridge,
plus an `aws_lambda_permission` on the handler naming the bridge's role (the handler
grants the permission, rather than the bridge's policy listing every handler, so the
bridge does not depend on handlers that themselves depend on custom resources). The
bridge builds the event CloudFormation would have sent, gives the handler a pre-signed
S3 URL as `ResponseURL`, invokes it asynchronously, waits for the response object and
returns it; a `FAILED` response fails the apply with the handler's reason. Because
Terraform passes the previous input but not the previous result on update and delete,
the bridge keeps each resource's physical ID in its bucket between calls.

Three behaviours of the bridge exist because a live run needed them. It is granted
`s3:ListBucket` on its bucket, since without it a missing response object reads as
`AccessDenied` rather than `NoSuchKey`. It retries `AccessDenied` for up to 60 seconds
on the first call, while its own role propagates. And it signs the response URL against
the regional S3 endpoint, because the global endpoint answers a new bucket outside
`us-east-1` with a 307, which `urllib` does not follow on `PUT`.

The handler runs inside the bridge's synchronous invocation, so the bridge's 15-minute
Lambda limit bounds every custom resource. The slowest one measured, the container
image build, took under two minutes.

### The label stack

`static_files.label_tf` writes `label.tf`: an `aws_cloudformation_stack` named exactly
`stack_name`, so `idp-cli --stack-name` and the deployment's own lookups by stack name
find it, and a suffixed name would break both. Its description is the main template's
description with `(Terraform)` and a read-only notice appended, keeping the tracking
marker first and intact. It holds one `AWS::CloudFormation::WaitConditionHandle`, the
deployment's non-`NoEcho` settings as parameters whose `AllowedValues` is exactly the
current value (so a CloudFormation update cannot change them), and the main template's
outputs plus `TerraformResourceDirectory` and `TerraformResourceDirectoryVersion`. The
template is passed by URL from the bridge bucket, because with its parameters and
outputs it exceeds the 51,200 bytes CloudFormation accepts inline; the key carries a
hash of the content, so a change is a new URL and Terraform updates the stack.
`enable_cfn_stack_label = false` turns it off. The user-facing description is in
[docs/terraform.md](../docs/terraform.md#the-read-only-label-stack).

### The resource directory

`TerraformResourceDirectory` names `terraform/cfn-resource-directory.json` in the
bridge bucket. It is a CloudFormation-shaped list of every module's resources (logical
ID, type, and physical ID with CloudFormation's `Ref` semantics), with child modules
standing in for nested stacks. The translator writes it in a mode
(`_directory_mode`) that keeps CloudFormation's `Ref` semantics exactly, which can
differ from the Terraform attribute the HCL uses elsewhere.

`lib/idp_sdk/idp_sdk/_core/terraform_stack.py` reads it. Importing `idp_sdk` installs
a hook on every CloudFormation client botocore creates; for a label stack, and for the
synthetic nested stacks the directory describes, it answers `ListStackResources`,
`DescribeStackResource`, `DescribeStackResources` and the nested stacks'
`DescribeStacks` from the directory, and passes everything else through. So no
`idp-cli` or SDK call site needed to change. A caller that does not go through
botocore in a process that imported `idp_sdk` does not see it: a subprocess running the
`aws` CLI, for example, sees only the label. `scripts/rbac_common.py` routes its
lookups through `boto3` for that reason. `StackDeployer._stack_exists` in
`lib/idp_sdk/idp_sdk/_core/stack.py` refuses a CloudFormation deploy or delete of a
label stack.

## When the gate fails: adding a hand mapping

`translator/tests/test_offline_templates.py` translates every main template variant in
`cli.TEMPLATE_VARIANTS` offline with `--survey` and fails on any failure, so a
template change that uses a type, property or intrinsic the translator does not handle
fails on the pull request that introduces it. Reproduce a failure with:

```bash
cd terraform/translator
python3 -m idp_tf.cli ../../template.yaml --offline --survey --out /tmp/idp-tf
```

Each failure line names the resource and what is missing. The usual cases:

- **An unmapped property on a hand-mapped type** (`unmapped property 'X'`). Add the
  property to that type's spec in `aws_mappers.py` or `aws_mappers_storage.py`. A
  `SpecMapper` lists every CloudFormation property it understands, as an entry of one
  of these shapes: `("attr", tf_name)`, `("json", tf_name)` for a `jsonencode()`d
  value, `("map", tf_name)`, `("tags", tf_name)` for CloudFormation's
  `[{Key, Value}]`, `("block", tf_name, spec)` or `("blocks", tf_name, spec)` for
  nested blocks, and `("flatten", spec)` to hoist sub-properties into the parent.
  `LOG_GROUP` in `aws_mappers.py` is a short example to copy.
- **An unmapped `GetAtt`** (`unmapped GetAtt X`). Add the attribute to the mapper's
  `attrs`, with the Terraform attribute and, where CloudFormation's value differs in
  shape, a format string (the log group's `Arn` adds CloudFormation's `:*`).
- **A new type with no mapping and no `awscc` resource.** Add a mapper and register it
  in `MAPPERS` or `STORAGE_MAPPERS`. If `awscc` does support it and the type is merely
  missing from the vendored schema, refresh the schema instead (next section).
- **A provider-required attribute CloudFormation defaults silently**, such as
  `log_format` on a Lambda function. Declare it in the spec under the `DEFAULTS` key.
- **A type that translates but will not re-plan clean or destroy clean on `awscc`.**
  Hand-map it and add it to `HYBRID_TYPES`. This is found only by a live deploy (see
  [Testing live](#testing-live)).

Add a test in `translator/tests/test_translate.py` for the mapping, and run the suites:

```bash
cd terraform/translator && python3 -m pytest -q
cd terraform/bridge && python3 -m pytest -q tests
```

Both suites are registered in `scripts/run_all_tests.py`, so `make test` runs them. As
of 2026-11-06 neither CI configuration runs them; check the `test-packages-cicd`
target in the `Makefile` for the current state.

## Refreshing the awscc schema

The full `awscc` provider schema is about 17 MB; the translator needs only the types
the templates use. `translator/scripts/refresh_awscc_schema.py` reads a full schema,
finds the types the templates need by translating them offline, and writes only those,
without their documentation fields, to `translator/schemas/awscc.json` together with
the provider version. Run it when the translator reports a type missing from the
vendored subset, or to move to a newer provider:

```bash
# A directory whose versions.tf pins hashicorp/awscc to the version you want.
terraform -chdir=<dir> init
terraform -chdir=<dir> providers schema -json > /tmp/awscc-full.json
python3 terraform/translator/scripts/refresh_awscc_schema.py \
    /tmp/awscc-full.json --version <awscc version>
```

Commit the regenerated file. The version it records is written into every generated
`versions.tf` as the floor of the `awscc` constraint, so a module never runs against a
provider older than the schema it was generated from.
`test_vendored_schema_covers_every_awscc_type_the_templates_use` checks that the subset
is not missing a type the translator would send to `awscc`. A newer provider can change
an attribute's shape, so after a refresh run the offline gate and, before release, a
live deploy.

## Declared overrides

Four tables in `translator/idp_tf/translate.py` record the places where a mechanical
translation is wrong or incomplete. Each entry is keyed narrowly and carries its reason.

- **`AWSCC_PROPERTY_FIXUPS`**, keyed by `(type, property)`. For properties where
  `awscc` stores what the service returns and the service returns a different spelling
  of the same value than templates write, it names a rewrite applied before rendering.
  For example, a Glue crawler's `Role` is given as an ARN and returned as a name
  (`role_name`), and Cognito keeps one copy of a callback URL the template lists twice
  (`distinct`). Add an entry when a re-plan after apply shows a change on an `awscc`
  resource that the service caused, and the type is otherwise fine on `awscc`.
- **`AWSCC_REF_ATTR`**, keyed by type. CloudFormation's `Ref` returns one member of a
  composite `primaryIdentifier`, while `awscc`'s `id` joins all of them with `|`; for
  `AWS::ApiGateway::Resource`, `Ref` is only the `ResourceId`. Each entry names the
  `awscc` attribute that equals `Ref`, read from the CloudFormation registry
  (`aws cloudformation describe-type`) per type. A composite-identifier type that is
  not listed keeps `id`, which is right only where `Ref` also returns the joined value.
- **`LIFECYCLE_OVERRIDES`**, keyed by `(type, logical ID)`. CloudFormation never
  notices that a resource has a second writer; Terraform does, and reverts it. Each
  entry gives the attributes for `lifecycle { ignore_changes = [...] }` and the reason.
  The one entry today is `SettingsParameter`, whose value the `UpdateSettings` custom
  resource rewrites at deploy time. The translator fails if an entry names a resource
  the template no longer has, so an override cannot outlive what it described.
- **`TERRAFORM_DIRECTORY_READERS`**, keyed by function logical ID. These functions walk
  the stack tree at run time through CloudFormation; today that is
  `DataMartRollupFunction` alone. In the root module each gets
  `IDP_TF_RESOURCE_DIRECTORY` (the S3 URL of the resource directory) and an inline
  policy allowing `s3:GetObject` on that one object.
  `idp_common.utils.terraform_stack.install_if_terraform()` installs the lookup hook
  only when that variable is set, so CloudFormation deployments are unaffected. The
  rollup also finds the deployment's functions by tag, and CloudFormation's
  `aws:cloudformation:stack-name` is reserved, so the translator tags every function
  `idp:stack-name` with its module's `stack_name` (`<root>-<LogicalId>` for a nested
  module, the same name the resource directory gives the nested stack). A variant that
  removes a reader skips it, and `tests/test_offline_templates.py` fails if a reader
  is not a function in the standard template.

## Testing live

There is no `make` target for a live Terraform deployment yet. The procedure used for
the validation below is:

1. Publish the templates: `idp-cli publish --source-dir . --region <region>`, which
   packages `.aws-sam/idp-main.yaml` and uploads it with its artifacts. Use a
   development account and a stack name you will tear down.
2. Translate the published template, with its nested templates fetched from S3:

   ```bash
   cd terraform/translator
   python3 -m idp_tf.cli ../../.aws-sam/idp-main.yaml \
       --nested-cache /tmp/idp-tf-nested \
       --label-template ../../.aws-sam/idp-main.yaml --version <version> \
       --out ../build/full
   ```

   `terraform/build/` is gitignored.
3. Deploy with the fixture root module in `prototype/full/`, which calls
   `../../build/full` and sets the Region, `stack_name`, `admin_email`, the
   permissions boundary and malware scanning:
   `terraform -chdir=terraform/prototype/full init`, then `apply`.
4. Run `terraform plan` immediately after. It must report no changes; a change on every
   plan is a mapping defect (see the third reason under
   [Provider mapping](#provider-mapping)).
5. Run the integration steps of `scripts/sdlc/codebuild_deployment.py` against the
   deployment name, with `idp-cli` and the SDK installed from the same checkout so the
   SDK's hook is in place.
6. Optionally compare properties with a CloudFormation twin deployed from the same
   template with the same parameters:
   `python prototype/compare.py --cfn-stack <name> --tf-dir <dir> --module <module.tf>
   --report translation-report.json` reads both copies of each resource through Cloud
   Control and diffs them, with the expected differences excluded by name, one entry
   per property.
7. `terraform destroy`, and check for leftovers: CodeBuild creates
   `/aws/codebuild/<project>` log groups itself, on both the Terraform and the
   CloudFormation side.

`prototype/mdd/` is the equivalent fixture for translating one nested stack
(`nested/multi-doc-discovery`) on its own; it creates the buckets, tables and KMS key
the parent stack would otherwise pass in.

## Validation

These are the measured results of the live runs. The raw notes, including the defects
each run found, are in [prototype/RESULTS.md](prototype/RESULTS.md).

### 2026-11-05: `nested/multi-doc-discovery`, prototype

Run in a development account in `us-west-2` against the 0.6.13 templates, with the
generated module and a CloudFormation twin deployed side by side with the same
parameters, in both mappings.

| Question | Hybrid | All-awscc |
|---|---|---|
| Hand edits to the generated Terraform | none | none |
| Plan after apply | empty | not empty: 4 IAM roles show a change on every plan |
| Property differences vs CloudFormation (18 resources, Cloud Control read of both) | 0 unexplained | 0 unexplained |
| Four container Lambdas invoked with the same event | same result as CloudFormation | same |
| `Custom::DockerBuild` through the bridge | Create, Update and Delete worked | same |
| Build time inside the bridge | 1m51s | same |
| `terraform destroy` | clean | failed: ECR repository not emptied |
| OpenTofu 1.13.9 plan against the Terraform-written state | no changes | not run |
| `validate` on Terraform 1.7.5 / 1.13.4 and OpenTofu 1.7.10 / 1.13.9 | passes | passes |

Changing `LogRetentionDays` updated five log groups in place. Changing `BuildHash`
re-ran the image build through the bridge's update path, as CloudFormation's custom
resource did; on both sides the Lambdas kept their previous image digest, because
`ImageUri` is `<repo>:latest` and does not change.

### 2026-11-06: main template and all five nested stacks

Run in the same account against the 0.6.13 templates, with `PermissionsBoundaryArn`
and `MalwareScanning=CREATE_PLAN`, the parameters the integration suite's own deploy
uses. The final run started from an empty state and changed nothing by hand.

| Check | Result |
|---|---|
| Translation of the main template and its five nested stacks | 0 failures, 0 hand edits |
| `terraform validate` | valid; 15 deprecation warnings (`hash_key`/`range_key` on DynamoDB) |
| First-try `terraform apply` from empty state | succeeded: 1,595 resources in 28 min 21 s |
| `terraform plan` immediately after | no changes |
| Integration suite (the 14 steps of `scripts/sdlc/codebuild_deployment.py`, unchanged) | 14/14 |
| `terraform destroy` | succeeded in 20 min 39 s, with one retry because two buckets received new log objects after being emptied |

The generated tree contained 1,369 `hashicorp/aws` resources, 142 `awscc` resources
across 47 types, 142 `time_sleep` resources and 36 custom resources run through the
bridge. Those figures are for that run's templates and move with every template
change; regenerate the module and count again rather than relying on them.

Deploying found one defect in the templates themselves: `UserSyncFunction` had no
`kms:Decrypt` on the customer-managed key that encrypts `UsersTable`, so Lambda refused
to create its DynamoDB stream event source mapping. The statement is now in
`template.yaml`.

### Not yet covered

- Neither CI runs a live Terraform tier.
- A full-stack apply with OpenTofu.
- The `--headless` and `--govcloud` variants, and the feature-platform extensions,
  which install as their own CloudFormation stacks.
- Moving DynamoDB tables from `hash_key`/`range_key` to `key_schema`, which needs a
  higher `aws` provider floor.
