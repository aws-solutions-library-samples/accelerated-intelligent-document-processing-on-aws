Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# IDP Accelerator SDLC Cloudformation
* This directory contains Cloudformation scripts useful to deploy SDLC infrastructure used during SDLC development.

# Prerequisites
* A Unix like operating system (Linux/Mac/WSL/Xenix/SunOS)

## Installation
* Install the `s3-Sourcecode.yml` cloudformation template.
* Install the `credential-vendor.yml` cloudformation template.
    * Enter the gitlab group name (e.g. `genaiic-reusable-assets/engagement-artifacts`)
    * Enter the gitlap project name (e.g. `genaiic-idp-accelerator`)
    * Enter the bucket name created in the last step (e.g. `genaiic-sdlc-source-code-YOUR_AWS_ACCOUNT-YOUR_REGION`)
* Customize the environment variables in your CodePipeline/CodeBuild configuration.
* The deployment will use the new `scripts/sdlc/codebuild_deployment.py` script automatically.
    * This will ensure that an archive is there to install, when 
* Optional: Install the `sdlc-iam-role.yml` for least privilege sdlc operation (coming soon!)
* Install the `codepipeline-s3.yml` cloudformation template.
    * Optional: add the iam role created in the last step (e.g. `arn:aws:iam::YOUR_AWS_ACCOUNT:role/genaiic-sdlc-role`)
    * Be sure to replace the `genaiic-sdlc-source-code-YOUR_AWS_ACCOUNT-YOUR_REGION` with the name of the sourcecode bucket you created.

## A second CI region

The weekly region runs (see `scripts/sdlc/docs/CI_TEST_COVERAGE.md`, "Weekly region
runs") need the per-region half of this setup in each region they test. IAM is
global, so the `sdlc-iam-role.yml` and `credential-vendor.yml` stacks stay in
`us-east-1` and are reused.

1. Deploy `s3-sourcecode.yml` as `genaiic-sdlc-s3-sourcecode` in the region, with
   `BucketName=genaiic-sdlc-sourcecode-<account>-<region>`.
2. Deploy `codepipeline-s3.yml` as `genaiic-sdlc-codepipeline` in the region, with
   the same parameters as the `us-east-1` stack except:
   * `PipelineTriggerRoleName=genaiic-sdlc-pipeline-trigger-role-<region>` (an
     IAM name, so it must not repeat the `us-east-1` stack's);
   * a `TestVpcCidr` that overlaps no other CI region's.

   The template is over CloudFormation's inline size limit, so pass
   `--s3-bucket <the region's source bucket> --s3-prefix cfn-staging`, and remove
   the staged object afterwards. Never stage under `deploy/`: an object there
   triggers the pipeline.
3. Confirm the failure-notification SNS subscription the stack creates.
4. Allow the GitLab role (`idp-sdlc-GitLab`) to `s3:PutObject` to
   `genaiic-sdlc-sourcecode-<account>-<region>/deploy/code.zip`: add the bucket to
   `S3PutObjectPolicy` in `credential-vendor.yml` and update the
   `genaiic-sdlc-credential-vendor` stack in `us-east-1`.
5. Add the region to `REGION_RUN_REGIONS` (`integration_test_deployment.py`) and
   `CI_INFRA_REGIONS` (`codebuild_deployment.py`), then create its weekly schedule
   with `IDP_TEST_REGION=<region>`.
