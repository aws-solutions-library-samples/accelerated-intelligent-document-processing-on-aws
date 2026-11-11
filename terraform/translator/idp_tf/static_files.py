"""Fixed Terraform files the translator adds to a generated module."""

from __future__ import annotations

from . import hcl
from .aws_mappers import GENERATED_NAME_ROLE_LIFECYCLE, default_name
from .translate import STACK_NAME_VAR

VERSIONS_TF = """terraform {
  required_version = ">= 1.7.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      # The release the generated modules were validated and deployed with.
      version = ">= 6.72.0, < 7.0.0"
    }
    awscc = {
      source = "hashicorp/awscc"
      # The translator reads this provider's schema (vendored, see
      # terraform/translator/schemas/awscc.json), so the floor is that version.
      version = ">= AWSCC_VERSION, < 2.0.0"
    }
    time = {
      source  = "hashicorp/time"
      version = ">= 0.9, < 1.0"
    }
  }
}
"""


def bridge_tf(
    boundary_var: str | None = None,
    kms_key: str | None = None,
    access_log_bucket: str | None = None,
) -> str:
    """Bridge Lambda, its role and its response bucket.

    The bridge's own policy grants no ``lambda:InvokeFunction``: each handler
    grants it through an ``aws_lambda_permission`` naming the bridge role.

    The role is named like the template's roles (``<stack>-CfnBridge-<hash>``)
    so a deploying role scoped to ``role/<stack prefix>*`` can create it, and
    ``boundary_var`` (the module's ``PermissionsBoundaryArn`` variable, when it
    has one) is applied as its permissions boundary when set, as the template's
    ``HasPermissionsBoundary`` condition does for every other role.

    ``kms_key`` and ``access_log_bucket`` are HCL expressions
    (``Translator.bridge_kms_key`` and ``bridge_access_log_bucket``): the key the
    template's own log groups are encrypted with and the bucket its own buckets
    log to. With a key, the bridge's log group is encrypted with it, as the
    template's are; the key policy must already let CloudWatch Logs use it,
    which it does wherever the template's own log groups use the same key. With
    a bucket, the bridge bucket's server access logs go to it under
    ``cfn-bridge-logs/<stack_name>/``. None leaves that setting off.
    """
    role_lines = [f"name = {default_name('CfnBridge', 64, global_name=True)}"]
    if boundary_var:
        v = f"var.{boundary_var}"
        role_lines.append(
            f'permissions_boundary = {v} == null ? null : ({v} == "" ? null : {v})'
        )
    role_head = "\n".join(f"  {line}" for line in role_lines)
    role_lifecycle = "\n".join(
        f"  {line}" for line in GENERATED_NAME_ROLE_LIFECYCLE.splitlines()
    )
    log_group_kms = f"\n  kms_key_id        = {kms_key}" if kms_key else ""
    access_logging = (
        f"""
resource "aws_s3_bucket_logging" "cfn_bridge" {{
  bucket        = aws_s3_bucket.cfn_bridge.id
  target_bucket = {access_log_bucket}
  target_prefix = "cfn-bridge-logs/${{var.{STACK_NAME_VAR}}}/"
}}
"""
        if access_log_bucket
        else ""
    )
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
{access_logging}
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
  rule {{
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {{}}
    abort_incomplete_multipart_upload {{
      days_after_initiation = 1
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
{role_head}
  assume_role_policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [{{
      Effect    = "Allow"
      Principal = {{ Service = "lambda.${{local.pseudo.dns_suffix}}" }}
      Action    = "sts:AssumeRole"
    }}]
  }})
{role_lifecycle}
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
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${{aws_cloudwatch_log_group.cfn_bridge.arn}}:*"
      }},
    ]
  }})
}}

# IAM is eventually consistent: Lambda refuses a permission naming a role it
# cannot see yet ("The provided principal was invalid").
resource "time_sleep" "cfn_bridge_iam" {{
  create_duration = "15s"
  depends_on      = [aws_iam_role.cfn_bridge, aws_iam_role_policy.cfn_bridge]
}}

resource "aws_cloudwatch_log_group" "cfn_bridge" {{
  name_prefix       = "/${{var.{STACK_NAME_VAR}}}/lambda/CfnBridge-"
  retention_in_days = 30{log_group_kms}
}}

resource "aws_lambda_function" "cfn_bridge" {{
  function_name    = "${{substr(var.{STACK_NAME_VAR}, 0, 40)}}-cfn-bridge"
  role             = aws_iam_role.cfn_bridge.arn
  handler          = "index.handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  timeout          = 900
  memory_size      = 256
  filename         = "${{path.module}}/zips/cfn_bridge.zip"
  source_code_hash = filebase64sha256("${{path.module}}/zips/cfn_bridge.zip")
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


READ_ONLY_NOTE = (
    "READ-ONLY. Set by Terraform, shown here for visibility only. Changing it "
    "on this stack has no effect on the deployment: change it in the Terraform "
    "configuration and run terraform apply."
)


def label_tf(
    description: str,
    version: str,
    parameters: list[tuple[str, str, str]] | None = None,
) -> str:
    """CloudFormation stack that labels this Terraform deployment.

    It manages no resources. It exists so a Terraform deployment and its
    published version appear in CloudFormation, for cross-referencing. It is
    named exactly like the deployment and carries the main template's outputs
    plus the location of the resource directory, so ``idp-cli --stack-name``
    and the SDK work against a Terraform deployment unchanged.
    """
    # The tracking marker stays first and intact; the notice follows it.
    desc = (
        f"{description} (Terraform) - READ-ONLY label for a Terraform deployment. "
        "Updated only by Terraform; do not update this stack in CloudFormation."
    )
    purpose = (
        "Label only: cross-references a Terraform deployment of the GenAI IDP "
        "Accelerator and its published version. Manages no resources. Its "
        "parameters show the deployment's settings and are read-only: Terraform "
        "owns them, and each allows only its current value, so an update to this "
        "stack cannot change them."
    )
    parameters = parameters or []
    values = hcl.obj(
        # coalesce() refuses all-empty arguments, and "" is a common value here.
        [(name, f'{value} == null ? "" : {value}') for name, value, _ in parameters],
        quote_keys=True,
    )
    descriptions = hcl.obj(
        [
            (name, hcl.string(f"{READ_ONLY_NOTE} {text}".strip()[:3900]))
            for name, _, text in parameters
        ],
        quote_keys=True,
    )
    return f"""variable "enable_cfn_stack_label" {{
  type        = bool
  default     = true
  description = <<-EOT
    Create a small CloudFormation stack, named <stack_name>, that labels this
    Terraform deployment and its published version so it can be
    cross-referenced in CloudFormation. The stack manages no resources (it holds
    one AWS::CloudFormation::WaitConditionHandle, which creates nothing) and
    costs nothing. It also carries the deployment's outputs, its settings as
    READ-ONLY parameters (each allows only its current value, so they cannot be
    changed through CloudFormation; NoEcho settings are left out), and the
    location of its resource directory, which is how idp-cli and the SDK find a
    Terraform deployment's resources by --stack-name. Set to false to skip it, for
    example where the deploying role may not use CloudFormation; idp-cli then
    cannot address this deployment.
  EOT
}}

resource "aws_s3_object" "cfn_resource_directory" {{
  bucket       = aws_s3_bucket.cfn_bridge.id
  key          = "terraform/cfn-resource-directory.json"
  content      = jsonencode(local.cfn_resource_directory_root)
  content_type = "application/json"
}}

locals {{
  # The deployment's effective settings, by CloudFormation parameter name, so
  # the label stack shows them as a CloudFormation stack would. NoEcho
  # parameters are not included.
  cfn_label_parameter_values = {values}
  cfn_label_parameter_descriptions = {descriptions}
  cfn_resource_directory_root = {{
    version   = 1
    stackName = var.{STACK_NAME_VAR}
    root      = local.cfn_resource_directory_self
  }}
  cfn_label_outputs = merge(
    {{
      for k, v in local.cfn_resource_directory_self.outputs :
      k => {{ Value = v }} if v != null && v != ""
    }},
    {{
      TerraformResourceDirectory = {{
        Value = "s3://${{aws_s3_bucket.cfn_bridge.id}}/${{aws_s3_object.cfn_resource_directory.key}}"
      }}
      # A hash of the directory content: known at plan time and stable,
      # unlike the object's ETag.
      TerraformResourceDirectoryVersion = {{
        Value = md5(aws_s3_object.cfn_resource_directory.content)
      }}
    }},
  )
}}

resource "aws_cloudformation_stack" "tf_label" {{
  count = var.enable_cfn_stack_label ? 1 : 0
  name  = var.{STACK_NAME_VAR}
  # Read-only by construction: each parameter allows only the value Terraform
  # set, so an update made through CloudFormation with another value is refused.
  parameters = local.cfn_label_parameter_values
  tags = {{
    ManagedBy = "Terraform"
    ReadOnly  = "true"
    # Tag values allow letters, digits, spaces and + - = . _ : / @ only.
    Purpose = "Read-only label for a Terraform deployment - update it only with terraform apply"
  }}
  # The template is passed by URL: with the parameters and outputs it is larger
  # than the 51,200 bytes CloudFormation accepts inline. The key carries a hash
  # of the content, so a change is a new URL and Terraform updates the stack.
  template_url = "https://${{aws_s3_bucket.cfn_bridge.id}}.s3.${{local.pseudo.region}}.${{local.pseudo.dns_suffix}}/${{aws_s3_object.cfn_label_template.key}}"
}}

resource "aws_s3_object" "cfn_label_template" {{
  bucket       = aws_s3_bucket.cfn_bridge.id
  key          = "terraform/label-template-${{md5(local.cfn_label_template)}}.json"
  content      = local.cfn_label_template
  content_type = "application/json"
}}

locals {{
  cfn_label_template = jsonencode({{
    AWSTemplateFormatVersion = "2010-09-09"
    Description              = {hcl.string(desc)}
    Metadata = {{
      "AWS::CloudFormation::Interface" = {{
        ParameterGroups = [{{
          Label      = {{ default = "READ-ONLY: deployment settings owned by Terraform. Change them in Terraform, not here." }}
          Parameters = keys(local.cfn_label_parameter_values)
        }}]
      }}
    }}
    Parameters = {{
      for k, v in local.cfn_label_parameter_values : k => {{
        Type                  = "String"
        Description           = local.cfn_label_parameter_descriptions[k]
        AllowedValues         = [v]
        ConstraintDescription = "is read-only on this label stack. Terraform owns this value: change it in the Terraform configuration and run terraform apply."
      }}
    }}
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
    Outputs = local.cfn_label_outputs
  }})
}}
"""
