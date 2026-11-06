# Prototype results: CloudFormation → Terraform for `nested/multi-doc-discovery`

Run on 2026-11-05 in a development account in `us-west-2`, against the template
`publish.py` packaged from `develop` at version 0.6.13. Every resource was
throwaway and has been destroyed.

## What was run

The translator (`terraform/translator/`) expanded SAM locally, translated the
packaged template, and wrote a module. A fixture root module
(`terraform/prototype/mdd/`) created the prerequisites the parent stack normally
passes (four buckets, two tables, a KMS key). It then deployed the generated
module and, side by side, the same published nested template through
CloudFormation with the same parameters.

The translator was run in two mappings, and both were deployed:

- **hybrid**: IAM roles, Lambda functions and log groups hand-mapped to
  `hashicorp/aws`; the ECR repository through generated `awscc`; the CodeBuild
  project hand-mapped because `awscc` has no resource for it.
- **awscc**: everything through generated `awscc` except CodeBuild.

## Results against the go/no-go table

| Question | Hybrid | All-awscc |
|---|---|---|
| Hand edits to the generated Terraform | none | none |
| Plan after apply | empty | **not empty**: 4 IAM roles show a change on every plan |
| Property differences vs CloudFormation (18 resources, Cloud Control read of both) | 0 unexplained | 0 unexplained |
| Four container Lambdas invoked with the same event | same result as CloudFormation (handler code reached) | same |
| `Custom::DockerBuild` through the bridge | Create, Update and Delete all worked | same |
| Build time inside the bridge | 1m51s, well inside the 15-minute synchronous limit | same |
| `terraform destroy` | clean | **failed**: ECR repository not emptied |
| OpenTofu 1.13.9 plan against the Terraform-written state | no changes | not run |
| `validate` on Terraform 1.7.5 / 1.13.4 and OpenTofu 1.7.10 / 1.13.9 | passes | passes |

Lifecycle checks on the hybrid deployment:

- Changing `LogRetentionDays` updated the five log groups in place, with nothing
  replaced.
- Changing `BuildHash` re-ran the image build through the bridge's Update path
  (1m51s), as CloudFormation's custom resource did (2m9s stack update). On
  **both** sides the Lambdas kept their previous image digest, because
  `ImageUri` is `<repo>:latest` and does not change. The translation reproduces
  CloudFormation exactly here. Whether the template intends that is a separate
  question about the template, not checked here.
- The label stack: created as `<stack_name>-tf-label` with description
  `AWS GenAI IDP Accelerator (uksb-r8evguc4p9) (SO9027) (v0.6.13) (Terraform)`.
  Bumping the version to 0.6.14 updated the description and the `Version`
  metadata. Setting `enable_cfn_stack_label = false` removed the stack and
  changed nothing else.

## Defects found and fixed during the run

1. **Bridge: missing S3 object read as AccessDenied.** Without `s3:ListBucket`, a
   `GetObject` on a missing key is refused rather than answered `NoSuchKey`.
   Granted `ListBucket` on the bridge bucket, and the bridge now treats both
   codes correctly.
2. **Bridge: IAM propagation.** The bridge's role policy and the first
   invocation happen in the same apply; the first call can be refused while IAM
   propagates. The bridge retries `AccessDenied` for up to 60 seconds.
3. **Bridge: 307 on the response URL.** A pre-signed URL for the global S3
   endpoint is answered with a redirect for a new bucket outside `us-east-1`,
   and handlers PUT their response with `urllib`, which does not follow a
   redirect on PUT. The bridge now signs against the regional endpoint.
4. **JSON written as text.** `awscc` compares such strings byte for byte. ECR
   returns its lifecycle policy minified in the original key order, so
   `jsonencode()` (which sorts keys) never matched. The translator now emits
   the minified JSON in source order.
5. **`log_format` required by the provider** where CloudFormation defaults it.
   Hand mappings can now declare provider-required defaults.

## Two `awscc` gaps, both reasons to keep the hybrid mapping

- **IAM policy documents are compared literally.** IAM stores a one-element
  array as a scalar (`"sts:AssumeRole"` for `["sts:AssumeRole"]`), so every
  SAM-generated role shows a change on every plan. `hashicorp/aws` compares
  policies semantically and has no such diff.
- **Write-only "on delete" properties are ignored.** `EmptyOnDelete` reached the
  `awscc_ecr_repository` config, but Cloud Control's `DeleteResource` receives
  only the identifier, so the repository was not emptied and destroy failed.
  CloudFormation passes the model on delete, which is why its twin deleted
  cleanly. Fix: hand-map `AWS::ECR::Repository` to `aws_ecr_repository` with
  `force_delete`.

## Expected, explained differences

These are excluded by name in `compare.py`, one entry per property:

- the tags each tool adds on its own: `aws:cloudformation:*`, and the fixture's
  provider default tag;
- empty strings one side stores and the other omits (IAM `Description`, Lambda
  `ImageConfig.WorkingDirectory`);
- two CodeBuild service defaults the provider writes out explicitly.

Two other differences are not about properties:

- **Physical names differ.** CloudFormation generates names like
  `<stack>-<LogicalId>-<random>`. The hand mappings use
  `<stack>-<LogicalId>-<hash>`; Cloud Control generates random lowercase names
  for `awscc` resources that have no name.
- **A CodeBuild log group is left behind on both sides.** The service creates
  `/aws/codebuild/<project>` itself; the template does not manage it.

## The rest of the stack, offline

Running the translator with `--survey` over the other packaged templates:

- `nested/bedrockkb`: translates cleanly, 31 resources, 8 of them custom
  resources through the bridge.
- `patterns/unified`: translates cleanly, 75 resources, 5 of them custom.
- `nested/api-resolvers`: one gap, `AWS::WAFv2::WebACL`, which neither provider
  path covers yet.
- the main `template.yaml`: five construct classes remain, and 61 more failures
  cascade from them:
  - nested stacks (`AWS::CloudFormation::Stack` → module calls);
  - `AWS::Cognito::UserPool` (2);
  - `AWS::CloudFront::Distribution` (1);
  - `AWS::CloudFormation::WaitConditionHandle` (2, no-ops).
  The main template's 22 custom resources already translate.

---

# Full stack: main template and all five nested stacks

Run on 2026-11-06 in the same development account, against the 0.6.13 templates
packaged from `develop`, with the `UserSyncFunction` KMS fix described below
applied. Stack name `tfidp1`, deployed with `PermissionsBoundaryArn` and
`MalwareScanning=CREATE_PLAN`, the parameters the integration suite's own
deploy uses.

## Acceptance results

The final run started from an empty account state and changed nothing by hand:

| Check | Result |
|---|---|
| Translation of the main template and its five nested stacks | 0 failures, 0 hand edits |
| `terraform validate` | valid; 15 deprecation warnings (`hash_key`/`range_key` on DynamoDB, see below) |
| First-try `terraform apply` from empty state | succeeded: 1,595 resources in 28 min 21 s |
| `terraform plan` immediately after | no changes |
| Integration suite (the 14 steps of `scripts/sdlc/codebuild_deployment.py`, unchanged) | see below |
| `terraform destroy` | succeeded in 20 min 39 s, including one retry because two buckets received new log objects after being emptied |

The integration suite ran against Terraform deployments of this stack:

- **First run:** 13/14. Step 12 (API RBAC) failed because it resolved
  resources through the `aws` CLI; see below.
- **Second run:** 14/14 after that fix, on the deployment with an empty
  re-plan.
- **Acceptance run:** 14/14 on the fresh first-try deployment. It ran through `~/tfspike-scripts/run_integ.sh`,
which calls the driver's own step functions with the SDK's Terraform hook
installed and an `idp-cli` pinned to this checkout.

What the generated tree contains:

- 1,369 `hashicorp/aws` resources;
- 142 `awscc` resources across 47 types;
- 142 `time_sleep` resources (IAM propagation waits);
- 36 custom resources, run through the bridge.

## Defects in this repository found by deploying it

**`UserSyncFunction` cannot read its DynamoDB stream.** `UsersTable` is
encrypted with the stack's customer-managed key, and the function's role has no
`kms:Decrypt` on it. Lambda now validates that at creation, so
`CreateEventSourceMapping` is refused. On the long-lived `IDP1` stack, deployed
through CloudFormation, the same mapping has been **disabled since 2026-08-19**
with `not authorized to perform: kms:Decrypt`. So user changes in the Users
table have not been reaching Cognito there. The fix, one IAM statement in
`template.yaml`, is on this branch and should ship as its own PR. I believe a
fresh CloudFormation deploy would also be refused, because the direct API call
is, but I have not run one.

## What it took, by class

Every item below was found by a live deploy or re-plan, not by reading.

**Types hand-mapped to `hashicorp/aws`,** in addition to IAM roles, Lambda
functions and log groups:

- *`awscc` has no resource for them:* CodeBuild projects, Cognito user pools,
  the CloudFront distribution, the WAF web ACL, SQS queue policies and inline
  IAM policies.
- *Cloud Control fails or ignores a property:*
  - EventBridge rules: an internal error on create.
  - ECR repositories: `EmptyOnDelete` is ignored on delete.
- *`awscc` shows a change on every plan because the service normalises what it
  stores:*
  - S3 buckets and DynamoDB tables: lists come back sorted.
  - Bucket policies: re-encoded.
  - Lambda event source mappings: a function name comes back as an ARN.
  - Lambda permissions: an account principal comes back as the root ARN.
  - CloudWatch dashboards: the body is compared as text.
  - Cognito users: the attribute set is compared literally.

**Semantics CloudFormation provides implicitly, reproduced explicitly:**

- **Plan-time conditions across modules.** A nested stack's condition can read a
  parameter the parent fills with a resource attribute. The parent also passes
  a placeholder that is known at plan time and never empty, which is exactly
  what CloudFormation's condition sees.
- **Dependencies.** A reference to a nested stack waits for the whole module.
- **Pseudo-parameters** are read once at the root and passed down. A data source
  inside a module that has `depends_on` is deferred to apply.
- **`Ref` on composite identifiers.** Thirteen types have a composite
  `primaryIdentifier`, for example `AWS::ApiGateway::Resource`, whose `Ref` is
  only the `ResourceId`. Read from the CloudFormation registry per type.
- **IAM propagation.** A 10-second wait after each role and its policies; one
  after the bridge role, before Lambda accepts it as a principal.
- **`cfnresponse`.** It is importable by CloudFormation's inline Python
  functions, so a stand-in is packaged with each one that imports it.
- **Inline-function and bridge zips** are built deterministically at translation
  time, because a zip built at plan time inside a module with `depends_on` is
  unknown on every plan.

**One declared override.** CloudFormation never notices that a resource has a
second writer, and Terraform does. `SettingsParameter`'s value is rewritten by
the `UpdateSettings` custom resource at deploy time, so it carries
`ignore_changes = [value]`. It is declared in `LIFECYCLE_OVERRIDES` with that
reason, and the translator fails if the resource ever disappears.

## `idp-cli` and the SDK against a Terraform deployment

No call site changed. The label stack is named exactly like the deployment and
carries its outputs plus `TerraformResourceDirectory`, an S3 object holding a
CloudFormation-shaped directory of every module's resources (logical ID, type,
and physical ID with CloudFormation's `Ref` semantics). The SDK attaches a hook
to every CloudFormation client botocore creates. For a label stack it answers:

- `ListStackResources`;
- `DescribeStackResource(s)`;
- nested-stack `DescribeStacks`.

Everything else goes to CloudFormation as usual. `idp-cli deploy` and `delete`
refuse a Terraform-labelled stack and name the Terraform command to run.

The API RBAC test called the `aws` CLI in a subprocess for these lookups, which
the hook cannot see. `scripts/rbac_common.py` now routes those through `boto3`;
the result for a CloudFormation stack is unchanged.

## Not covered yet

- **Runtime callers that walk the stack tree inside the deployment.** These are
  the error-analyzer agent's `idp_common.monitoring.stack_utils` and the
  `data_mart_rollup` Lambda. Neither is exercised by the integration suite, and
  their roles would also need read access to the directory object.
- **CI.** The offline gates and a nightly Terraform tier are not wired into
  either CI yet, and `publish.py` does not yet emit the module.
- **OpenTofu.** Validated offline (1.7.10 and 1.13.9) and planned against
  Terraform-written state on the prototype, but the full stack has not been
  applied with it.
- **The two other deployment variants** (`--headless`, `--govcloud`) and the
  feature-platform extensions, which install as their own CloudFormation stacks.
- **DynamoDB `hash_key`/`range_key`** are deprecated in provider 6.x in favour
  of `key_schema`. Moving needs a higher provider floor; it is a warning today.
