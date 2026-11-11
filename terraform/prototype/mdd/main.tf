# Prototype fixture: deploys the generated multi-doc-discovery module and,
# side by side, the same published nested template through CloudFormation, so
# the two can be compared resource by resource. Throwaway resources only.

terraform {
  required_version = ">= 1.7.0"
  required_providers {
    aws   = { source = "hashicorp/aws", version = ">= 6.0" }
    awscc = { source = "hashicorp/awscc", version = ">= 1.50" }
  }
}

variable "region" {
  type    = string
  default = "us-west-2"
}
variable "name" {
  type    = string
  default = "tfspike-mdd"
}
variable "artifact_bucket" { type = string }
variable "source_zipfile" { type = string }
variable "build_hash" { type = string }
variable "cfn_template_url" { type = string }
variable "deploy_cfn_twin" {
  type    = bool
  default = true
}
variable "enable_cfn_stack_label" {
  type    = bool
  default = true
}
variable "log_retention_days" {
  type    = number
  default = 30
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { "idp:prototype" = "terraform-translator" }
  }
}
provider "awscc" {
  region = var.region
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

resource "aws_kms_key" "cmk" {
  description             = "${var.name} prototype key"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "Root"
        Effect    = "Allow"
        Principal = { AWS = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "Services"
        Effect    = "Allow"
        Principal = { Service = ["logs.${data.aws_partition.current.dns_suffix}", "dynamodb.${data.aws_partition.current.dns_suffix}"] }
        Action    = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"]
        Resource  = "*"
      },
    ]
  })
}

resource "aws_s3_bucket" "b" {
  for_each      = toset(["discovery", "input", "testset", "configuration"])
  bucket_prefix = "${var.name}-${each.key}-"
  force_destroy = true
}

resource "aws_dynamodb_table" "configuration" {
  name         = "${var.name}-configuration"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "Configuration"
  attribute {
    name = "Configuration"
    type = "S"
  }
}

resource "aws_dynamodb_table" "discovery_tracking" {
  name         = "${var.name}-discovery-tracking"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "jobId"
  attribute {
    name = "jobId"
    type = "S"
  }
}

locals {
  params = {
    DiscoveryBucketName             = aws_s3_bucket.b["discovery"].id
    DiscoveryBucketArn              = aws_s3_bucket.b["discovery"].arn
    InputBucketName                 = aws_s3_bucket.b["input"].id
    TestSetBucketName               = aws_s3_bucket.b["testset"].id
    ConfigurationTableName          = aws_dynamodb_table.configuration.name
    ConfigurationBucketName         = aws_s3_bucket.b["configuration"].id
    ConfigurationTableArn           = aws_dynamodb_table.configuration.arn
    CustomerManagedEncryptionKeyArn = aws_kms_key.cmk.arn
    LogRetentionDays                = var.log_retention_days
    BuildHash                       = var.build_hash
    ArtifactBucket                  = var.artifact_bucket
    SourceZipfile                   = var.source_zipfile
    DiscoveryTrackingTableName      = aws_dynamodb_table.discovery_tracking.name
  }
}

module "tf" {
  source = "../../build/mdd-module"

  enable_cfn_stack_label = var.enable_cfn_stack_label

  stack_name                          = "${var.name}-tf"
  param_stack_name                    = "${var.name}-tf"
  discovery_bucket_name               = local.params.DiscoveryBucketName
  discovery_bucket_arn                = local.params.DiscoveryBucketArn
  input_bucket_name                   = local.params.InputBucketName
  test_set_bucket_name                = local.params.TestSetBucketName
  configuration_table_name            = local.params.ConfigurationTableName
  configuration_bucket_name           = local.params.ConfigurationBucketName
  configuration_table_arn             = local.params.ConfigurationTableArn
  customer_managed_encryption_key_arn = local.params.CustomerManagedEncryptionKeyArn
  log_retention_days                  = local.params.LogRetentionDays
  build_hash                          = local.params.BuildHash
  artifact_bucket                     = local.params.ArtifactBucket
  source_zipfile                      = local.params.SourceZipfile
  discovery_tracking_table_name       = local.params.DiscoveryTrackingTableName
}

resource "aws_cloudformation_stack" "cfn" {
  count              = var.deploy_cfn_twin ? 1 : 0
  name               = "${var.name}-cfn"
  template_url       = var.cfn_template_url
  capabilities       = ["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"]
  parameters         = merge({ for k, v in local.params : k => tostring(v) }, { StackName = "${var.name}-cfn" })
  timeout_in_minutes = 60
}

output "tf_outputs" {
  value = {
    embed   = module.tf.EmbedFunctionArn
    cluster = module.tf.ClusterFunctionArn
    analyze = module.tf.AnalyzeFunctionArn
    save    = module.tf.SaveFunctionArn
    ecr     = module.tf.ECRRepositoryUri
  }
}
output "cfn_outputs" {
  value = var.deploy_cfn_twin ? aws_cloudformation_stack.cfn[0].outputs : {}
}
