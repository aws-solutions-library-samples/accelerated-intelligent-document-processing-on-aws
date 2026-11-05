"""Fixed Terraform files the translator adds to a generated module."""

from __future__ import annotations

from . import hcl
from .translate import STACK_NAME_VAR

VERSIONS_TF = """terraform {
  required_version = ">= 1.7.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0"
    }
    awscc = {
      source  = "hashicorp/awscc"
      version = ">= 1.50"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
  }
}
"""


def bridge_tf(service_tokens: list[str]) -> str:
    """Bridge Lambda, its role and its response bucket.

    ``service_tokens`` are the HCL expressions of every custom-resource
    handler the module invokes; the bridge may invoke exactly those.
    """
    tokens = hcl.lst(sorted(set(service_tokens)))
    return f"""# Runs CloudFormation custom-resource handlers for Terraform. See
# terraform/bridge/index.py in the source repository.

resource "aws_s3_bucket" "cfn_bridge" {{
  bucket_prefix = "${{lower(substr(var.{STACK_NAME_VAR}, 0, 20))}}-cfnbridge-"
  force_destroy = true
}}

resource "aws_s3_bucket_public_access_block" "cfn_bridge" {{
  bucket                  = aws_s3_bucket.cfn_bridge.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}}

resource "aws_s3_bucket_server_side_encryption_configuration" "cfn_bridge" {{
  bucket = aws_s3_bucket.cfn_bridge.id
  rule {{
    apply_server_side_encryption_by_default {{
      sse_algorithm = "AES256"
    }}
  }}
}}

resource "aws_s3_bucket_lifecycle_configuration" "cfn_bridge" {{
  bucket = aws_s3_bucket.cfn_bridge.id
  rule {{
    id     = "expire-responses"
    status = "Enabled"
    filter {{
      prefix = "responses/"
    }}
    expiration {{
      days = 1
    }}
  }}
}}

resource "aws_s3_bucket_policy" "cfn_bridge" {{
  bucket = aws_s3_bucket.cfn_bridge.id
  policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [{{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.cfn_bridge.arn, "${{aws_s3_bucket.cfn_bridge.arn}}/*"]
      Condition = {{ Bool = {{ "aws:SecureTransport" = "false" }} }}
    }}]
  }})
  depends_on = [aws_s3_bucket_public_access_block.cfn_bridge]
}}

resource "aws_iam_role" "cfn_bridge" {{
  name_prefix = "cfnbridge-"
  assume_role_policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [{{
      Effect    = "Allow"
      Principal = {{ Service = "lambda.${{data.aws_partition.current.dns_suffix}}" }}
      Action    = "sts:AssumeRole"
    }}]
  }})
}}

resource "aws_iam_role_policy" "cfn_bridge" {{
  name = "cfn-bridge"
  role = aws_iam_role.cfn_bridge.id
  policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [
      {{
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${{aws_s3_bucket.cfn_bridge.arn}}/*"
      }},
      {{
        # Without ListBucket a missing key reads as AccessDenied, not NoSuchKey.
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.cfn_bridge.arn
      }},
      {{
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = {tokens}
      }},
      {{
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${{aws_cloudwatch_log_group.cfn_bridge.arn}}:*"
      }},
    ]
  }})
}}

resource "aws_cloudwatch_log_group" "cfn_bridge" {{
  name_prefix       = "/${{var.{STACK_NAME_VAR}}}/lambda/CfnBridge-"
  retention_in_days = 30
}}

data "archive_file" "cfn_bridge" {{
  type        = "zip"
  source_file = "${{path.module}}/bridge/index.py"
  output_path = "${{path.module}}/.build/cfn_bridge.zip"
}}

resource "aws_lambda_function" "cfn_bridge" {{
  function_name    = "${{substr(var.{STACK_NAME_VAR}, 0, 40)}}-cfn-bridge"
  role             = aws_iam_role.cfn_bridge.arn
  handler          = "index.handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  timeout          = 900
  memory_size      = 256
  filename         = data.archive_file.cfn_bridge.output_path
  source_code_hash = data.archive_file.cfn_bridge.output_base64sha256
  environment {{
    variables = {{ BRIDGE_BUCKET = aws_s3_bucket.cfn_bridge.id }}
  }}
  logging_config {{
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.cfn_bridge.name
  }}
  depends_on = [aws_iam_role_policy.cfn_bridge]
}}
"""


def label_tf(description: str, version: str) -> str:
    """CloudFormation stack that labels this Terraform deployment.

    It manages no resources. It exists so a Terraform deployment and its
    published version appear in CloudFormation, for cross-referencing.
    """
    desc = f"{description} (Terraform)"
    purpose = (
        "Label only: cross-references a Terraform deployment of the GenAI IDP "
        "Accelerator and its published version. Manages no resources."
    )
    return f"""variable "enable_cfn_stack_label" {{
  type        = bool
  default     = true
  description = <<-EOT
    Create a small CloudFormation stack, named <stack_name>-tf-label, that
    labels this Terraform deployment and its published version so it can be
    cross-referenced in CloudFormation. The stack manages no resources (it holds
    one AWS::CloudFormation::WaitConditionHandle, which creates nothing) and
    costs nothing. Set to false to skip it, for example where the deploying
    role may not use CloudFormation.
  EOT
}}

resource "aws_cloudformation_stack" "tf_label" {{
  count = var.enable_cfn_stack_label ? 1 : 0
  name  = "${{var.{STACK_NAME_VAR}}}-tf-label"
  template_body = jsonencode({{
    AWSTemplateFormatVersion = "2010-09-09"
    Description              = {hcl.string(desc)}
    Resources = {{
      TerraformDeploymentLabel = {{
        Type = "AWS::CloudFormation::WaitConditionHandle"
        Metadata = {{
          Version = {hcl.string(version)}
          Iac     = "terraform"
          Purpose = {hcl.string(purpose)}
        }}
      }}
    }}
  }})
}}
"""
