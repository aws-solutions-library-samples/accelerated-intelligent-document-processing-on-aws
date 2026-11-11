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
| `translator/schemas/create_only.json` | the CloudFormation registry's create-only properties for each type the templates use, read from `cfn-lint` |
| `translator/scripts/refresh_create_only.py` | regenerates that list |
| `translator/renames.yaml` | logical-id and nested-stack renames, from which a release writes `moved` blocks |
| `bridge/` | the Lambda function that runs CloudFormation custom-resource handlers for Terraform |
| `tools/parity.py` | the release-time property comparison of a Terraform deployment with its CloudFormation twin (`make terraform-parity`), and its explained differences, `tools/parity_baseline.json` |
| `tools/replay_releases.py` | replays the current translator over the templates of past release tags |
| `prototype/` | root-module fixtures used for live validation, and the raw notes of those runs (`RESULTS.md`) |

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
   deterministic zips under `zips/`, a `translation-report.json`, `moved.tf` when
   the previous release's manifest was given and an address changed (see
   [Upgrade safety](#upgrade-safety)), and at the root `idp-terraform.json`. That
   last file is the contract with `idp-cli` and with the next release's
   translation. Format version 2 has these keys:

   | Key | Read by | What it is |
   |---|---|---|
   | `parameters` | `idp-cli deploy --terraform --parameters` | `{<CfnName>: {variable, type, required, sensitive}}` |
   | `retainedAddresses` | `idp-cli delete --terraform`, and the deploy's plan guard | addresses of resources with a `DeletionPolicy` other than `Delete`, removed from state before destroying |
   | `statefulAddresses` | the deploy's plan guard | addresses of every resource of a stateful type |
   | `customOutputEdges` | reviewers | every create-only property fed by a custom resource's output, and whether it is handled |
   | `resources` | the next release's translation | each resource's nested-stack path, logical id, type and every address it became |
   | `moves` | the next release's translation | every `moved` block written so far |

   Version 1 had only the first two; `idp-cli` reads either.
6. **Check.** `cli.tree_checks` runs the whole-tree checks described under
   [Upgrade safety](#upgrade-safety) and [Completeness](#completeness), and any
   failure fails the run.

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

## Upgrade safety

A Terraform upgrade replaces a resource whenever its address changes or a
property it cannot change in place does, and CloudFormation's semantics do not
protect it: there is no `DeletionPolicy`, and a value CloudFormation reads at
deploy time can be unknown to Terraform at plan time. Three mechanisms keep an
upgrade from losing data, and the plan guard in `idp-cli` backs them up.

### Stateful addresses and the plan guard

`idp_tf/safety.py` declares `STATEFUL_TYPES`, every type whose replacement or
deletion loses something the template cannot recreate, each with what is lost,
and `NOT_STATEFUL`, the types a reader might expect there with the reason each is
not. An `AWS::SSM::Parameter` with a `LIFECYCLE_OVERRIDES` entry is stateful too,
because a second writer owns its value. The translator computes
`statefulAddresses` from those lists; nothing is listed by hand.
`idp-cli deploy --terraform` refuses a plan that deletes or replaces any address in
`statefulAddresses` or `retainedAddresses`, of the installed module or the new one
(`protected_deletions` in `lib/idp_sdk/idp_sdk/_core/terraform_deploy.py`), unless
given `--terraform-allow-replace-stateful` (the earlier name,
`--terraform-allow-replace-retained`, still works). A deletion of an old address
whose resource moved without a `moved` block is refused the same way.

### Create-only properties fed by a custom resource

A custom resource is an `aws_lambda_invocation`. When any of its inputs changes,
its `result` is unknown until apply, so a property that reads it is unknown at plan,
and if that property cannot change in place Terraform plans a replacement.
CloudFormation sees the handler's actual answer and updates nothing. Measured on a
live upgrade: renaming the roles updated the custom resource that creates the S3
Vectors knowledge base, and the knowledge base's data source, whose
`KnowledgeBaseId` is create-only, was replaced.

`safety.custom_output_edges` finds every such edge at translation time. It
propagates "may be unknown" from each custom resource's outputs through
parameters, nested-stack outputs and other resources (a resource whose create-only
property is tainted is replaced, so its identity is tainted too; any attribute of
a resource with a tainted input is treated as tainted), and reports each
create-only property it reaches. Create-only comes from
`schemas/create_only.json`, the registry's `createOnlyProperties` for each type
(the `awscc` provider is generated from the same registry), plus each hand
mapper's `force_new` list where the `hashicorp/aws` provider replaces on more
properties than the registry lists (a DynamoDB key schema, a user pool's name and
alias attributes, a rule's event bus). The edges are printed
(`custom-output edge: ...`) and written to `customOutputEdges`.

An edge into a stateful resource fails the translation unless
`STABLE_CUSTOM_OUTPUTS` in `translate.py` says why the handler returns the same
value on every Update. For each entry the attribute is emitted with
`ignore_changes`, plus `replace_triggered_by` a `terraform_data` holding the same
value with every custom-resource output replaced by that resource's name, so a
change in anything else the value is made of still replaces the resource, as it
would under CloudFormation. `PASS_THROUGH_ATTRS` lets that key read through a
resource that only carries a value on (an SSM parameter's `Value`). Both
behaviours were checked offline with `terraform_data` on Terraform 1.7.5 and
OpenTofu 1.7.10: creating the key resource on the upgrade that introduces it does
not trigger a replacement, and changing its input does.

### `moved` blocks and the rename table

The translator takes `--previous-manifest <path or https URL>`, the previous
release's `idp-terraform.json`, and pairs each of its `resources` with the current
tree (`idp_tf/moved.py`): by identity (nested-stack path and logical id), through
`renames.yaml` for a logical-id or nested-stack rename, or by inference when a
logical id and type left one nested stack and appear in exactly one other. Each
pair whose addresses differ becomes a `moved` block in `moved.tf`, for the main
resource and every companion it became (a bucket's configuration resources, a
role's policies). A renamed nested stack moves as one `module` block. Moves are
kept in `moves` and written again by every later release, so a deployment that
skips releases still follows them. A resource whose Terraform type changed cannot
be moved and is reported. The publish path (`idp-cli publish --terraform`, and
`publish.py`) reads the previous manifest from the module the bucket's
`idp-terraform-latest.json` pointer names before uploading the new one.

To rename a logical id or a nested stack of a resource that exists in released
templates, add an entry to `renames.yaml` in the same pull request:

```yaml
renames:
  - from: PATTERNSTACK/OldBucket
    to: PATTERNSTACK/NewBucket
    since: 0.7.0
    reason: why it was renamed
```

The translation fails if `to` names nothing, or if `from` still exists. Leave
entries in place after the release that needed them.

## Completeness

`idp_tf/completeness.py` checks that every property value in the template reaches
the HCL emitted for its resource: each non-empty literal appears as HCL would
spell it, each `Ref` to a parameter appears as its variable, each reference to a
resource or nested stack names it, each condition appears as its local, and the
literal text of an `Fn::Sub` appears around its references. Nested-stack
parameters are checked on the `module` block. A value found nowhere fails the
translation: that is how a setting is lost silently, a guardrail wired to an empty
string being the example that prompted the check. It is a presence check; a value
that lands in the wrong attribute passes it.

A value that legitimately does not appear has an entry in `NOT_APPLICABLE`, keyed
by type and property path, saying where it went (a DynamoDB key type is the
attribute name `hash_key`; inline Lambda source is a zip). Measured on 2026-11-07
against the standard template, the check found 53 values, all on DynamoDB tables
and in three classes: the key type of a table's and of a GSI's `KeySchema`, and
`SSESpecification.SSEType`. All three are declared, and the mapper now refuses
any `SSEType` but `KMS`. Writing the check also showed that the table mapper read
`PointInTimeRecoverySpecification`, `StreamSpecification`,
`TimeToLiveSpecification` and a GSI's `Projection` key by key and dropped any other
key without a word; it now refuses one, and maps `RecoveryPeriodInDays`. No
template sets such a key today.

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
- **A type missing from `create_only.json`.** Run
  `python3 terraform/translator/scripts/refresh_create_only.py` with the pinned
  `cfn-lint` installed, and commit the file.
- **A stateful custom-output edge** (`a stateful resource takes a custom
  resource's output as a create-only property`). Read the handler. If it returns
  the same value on every Update, add the property to `STABLE_CUSTOM_OUTPUTS`
  with that reason; otherwise the template change replaces the resource on
  upgrade and needs a different design.
- **A value not in the emitted Terraform.** Fix the mapper so the value is
  written. Declare it in `completeness.NOT_APPLICABLE` only when it is represented
  some other way, and say how.
- **A `renames.yaml` entry naming nothing.** The `to` side was renamed or removed
  again; update or remove the entry.

Add a test in `translator/tests/test_translate.py` for the mapping, and run the suites:

```bash
cd terraform/translator && python3 -m pytest -q
cd terraform/bridge && python3 -m pytest -q tests
```

Both suites are registered in `scripts/run_all_tests.py`, so `make test` runs them,
and the `test-packages-cicd` target runs them in both CIs.

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

These tables in `translator/idp_tf/translate.py` record the places where a mechanical
translation is wrong or incomplete. Each entry is keyed narrowly and carries its
reason. `STABLE_CUSTOM_OUTPUTS` and `PASS_THROUGH_ATTRS` are described under
[Upgrade safety](#upgrade-safety), and `completeness.NOT_APPLICABLE` under
[Completeness](#completeness).

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
  resource rewrites at deploy time. An entry naming a resource the standard template
  no longer has is reported, and `tests/test_upgrade_safety.py` fails on it, so an
  override cannot outlive what it described.
- **A hand mapper's `ignore_changes`** (`SpecMapper.ignore_changes` in
  `aws_mappers.py`, or a lifecycle block a mapper emits itself), for a property the
  service cannot change after creation. Today: a Cognito user pool's
  `username_configuration`, which Cognito fixes at creation and CloudFormation never
  updates, and a pool user's attributes, delivery mediums and message action, which are
  create-only. The second differs from CloudFormation in one case: CloudFormation
  replaces a user whose attributes change, Terraform leaves it. Both let an existing
  pool be adopted with an `import {}` block instead of being replaced.
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
6. Compare properties with a CloudFormation twin deployed from the same template with
   the same parameters: `make terraform-parity CFN_STACK=<name> TF_STACK=<name>
   [REGION=<region>]` (`tools/parity.py`). It reads every resource of both, main and
   nested stacks, the Terraform side through the label stack's resource directory,
   through Cloud Control (functions also through the Lambda API), and diffs them
   after replacing the names that necessarily differ. A difference fails it unless
   `tools/parity_baseline.json` explains it, one entry per type and property path
   with its reason; so does a resource on one side only and an entry that explained
   nothing. It is a release-validation tier
   (`.claude/skills/release-validation.md`).
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
| `validate` on Terraform 1.7.5 / 1.13.4 and OpenTofu 1.7.10 / 1.13.9, the releases the gate pinned at the time | passes | passes |

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

### OpenTofu: main template and all five nested stacks

Run in a development account with the same templates and parameters as the Terraform
run above, with OpenTofu instead of Terraform from an empty state.

| Check | Result |
|---|---|
| `tofu apply` from empty state | succeeded: 1,584 resources |
| `tofu plan` immediately after | no changes |
| Delete (`idp-cli delete --terraform --terraform-bin tofu`) | succeeded |

The resource count differs from the Terraform run's because the templates changed
between the two runs.

### Offline, on every pull request

`make terraform-check` translates the standard template and its `headless` and
`govcloud` variants (the translator's `TEMPLATE_VARIANTS`) and runs `init` and
`validate` on each under every Terraform and OpenTofu release it pins
(`TERRAFORM_CHECK_BINARIES` in the `Makefile`: the oldest release `versions.tf` admits
and a recent one, of each). It plans and applies nothing. It then runs
`make terraform-scan-diff`, below.

### Three ways the translation is checked

No offline check proves a translation equivalent; every defect found so far was
found by a live deploy, a re-plan or a CI run. Three checks look at different
things, and none subsumes another:

| Check | Runs | What it catches | What it cannot see |
|---|---|---|---|
| Translator checks and `terraform validate` (`make test`, `make terraform-check`) | every pull request | unmapped types and properties, values that do not reach the HCL, stateful custom-output edges, malformed HCL | anything AWS decides at apply time |
| Static scan diff (`make terraform-scan-diff`) | every pull request, inside `make terraform-check` | a Terraform resource softer than its CloudFormation source under a rule both scanners share | what no `checkov` rule looks at (an empty guardrail id) |
| Property parity (`make terraform-parity`) | release validation, by hand | any property the two deployments store differently | resources Cloud Control cannot read, and behaviour |

### The static scan diff

`scripts/terraform_scan_diff.py` runs a pinned `checkov` (`CHECKOV_VERSION` in the
`Makefile`, installed into its own venv under the Terraform check cache) over the
source templates, which carry the review's `# checkov:skip` comments, over the same
templates SAM-expanded, and over the module generated offline from the standard
template. Each failed Terraform check is reduced to (resource identity, check id),
the identity coming from the manifest's `resources`, and compared only for checks
`checkov` implements for both frameworks. A finding the CloudFormation side passes
(softer), one on a resource the translator adds, or one with no counterpart fails
the gate unless `scripts/terraform_scan_baseline.json` has an entry for that check
on that resource with its reason. Findings on conditional resources the
CloudFormation scan skips (it evaluates conditions at the parameter defaults) are
counted, and the count is pinned. An entry that matches nothing fails.

Measured on 2026-11-07 with `checkov` 3.4.0 on the standard template:

| Class of failed Terraform check | Findings |
|---|---|
| the CloudFormation side fails or skips the same check on the same resource | 449 |
| softer than CloudFormation | 0 |
| on the custom-resource bridge the translator adds | 28 (7 checks, on each of 4 bridges) |
| no counterpart | 0 |
| on a conditional resource the CloudFormation scan skipped | 25 |
| on a check only the Terraform framework has (not compared) | 381 |

The 28 bridge findings are the baseline's entries. Two of them are gaps rather than
decisions, and the entries say so: the bridge's log group is not encrypted with
the deployment's customer-managed key, and its bucket has no access logging.

### Replay over past releases

`tools/replay_releases.py` extracts each release tag's templates with
`git archive`, following the nested `TemplateURL`s, and translates them with the
current translator `--offline --survey`. Measured on 2026-11-07, translator at this
branch:

| Release | Types | New AWS types | New properties on hand-mapped types | Unmapped types | Failures |
|---|---|---|---|---|---|
| v0.5.0 | 75 | - | - | 5 | 212 |
| v0.5.3 | 86 | `Cognito::UserPoolResourceServer`, and 7 for ALB hosting (EC2 security group and egress rule, VPC endpoint, four ELBv2 types) | - | 11 | 242 |
| v0.5.6 | 89 | `Cognito::UserPoolIdentityProvider` | `Function.VpcConfig` | 11 | 259 |
| v0.5.9 | 95 | `ApiGateway::{Deployment,RestApi,Stage}`, `EC2::{Instance,LaunchTemplate}`, `IAM::InstanceProfile` | `Project.VpcConfig` | 11 | 263 |
| v0.5.12 | 98 | `EC2::SecurityGroupIngress` (and `SNS::Subscription` in 0.5.10) | - | 12 | 292 |
| v0.5.16 | 98 | `CloudFront::OriginAccessControl` | - | 11 | 335 |
| v0.6.0 | 90 | `ApiGateway::{Account,Authorizer,GatewayResponse,Method,Resource}`, `Lambda::Url` | `Permission.FunctionUrlAuthType` | 0 | 0 |
| v0.6.1 | 91 | - | `UserPoolUser.MessageAction` | 0 | 0 |
| v0.6.2 | 91 | - | `Repository.RepositoryPolicyText` | 0 | 0 |
| v0.6.3 – v0.6.6 | 91 | - | - | 0 | 0 |
| v0.6.7 | 93 | `Lambda::EventInvokeConfig` | `Function.ReservedConcurrentExecutions` | 0 | 0 |
| v0.6.8 | 93 | - | `UserPool.UserAttributeUpdateSettings` | 0 | 0 |
| v0.6.9 – v0.6.11 | 94 | - | - | 0 | 0 |
| v0.6.12 | 95 | `GuardDuty::MalwareProtectionPlan` | - | 0 | 0 |
| v0.6.13, v0.6.14 | 95 | - | - | 0 | 0 |

Rows for 0.5.x releases that added nothing are omitted; their failure counts sit
between the neighbouring rows'. Custom-resource types, which need no mapping, are
not counted as new types. Every 0.5.x template was read; none failed for a reason
other than mapping.

What it shows about per-release cost: across the fifteen 0.6 releases the templates
added two AWS resource types, both translated through the vendored `awscc` schema
with no hand mapping, and five properties on hand-mapped types, four of them a
spec entry and one (an ECR repository policy) a separate resource; every 0.6
release translates with no failure. The cost concentrates at
architectural changes: every 0.5 release fails on the same twelve types, the
AppSync API and the ALB hosting option that 0.6 removed (each failure line is a
resource of one of those types, or a reference to one). Those types are absent from
the vendored `awscc` subset, so the least they would need is a schema refresh;
whether they would then deploy is not known, because only a live run says that.
Two limits: the translator was written against 0.6.13 and 0.6.14, so a clean 0.6
row shows coverage, not what each release would have cost at the time; and a
translation that passes offline is not a deployment that passes.

### Not yet covered

- Neither CI runs a live Terraform tier, and `make terraform-parity` has not yet
  been run against the full stack; the parity numbers above are from the
  prototype run on one nested stack.
- An apply of the `--headless` and `--govcloud` modules; they are validated offline
  only. The feature-platform extensions install as their own CloudFormation stacks.
- An apply with a deploying role scoped to a role-name prefix and a required
  permissions boundary, rather than an administrator role.
- Moving DynamoDB tables from `hash_key`/`range_key` to `key_schema`, which needs a
  higher `aws` provider floor.
