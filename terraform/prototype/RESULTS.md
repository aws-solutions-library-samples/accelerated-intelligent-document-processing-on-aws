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
