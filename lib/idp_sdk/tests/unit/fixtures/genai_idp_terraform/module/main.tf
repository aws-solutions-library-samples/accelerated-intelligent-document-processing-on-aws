# Reduced copy of the published module, for tests.

resource "aws_s3_bucket" "mcp_content_bucket" {
  bucket_prefix = lower(substr("${var.stack_name}-MCPContentBucket", 0, 36))
  lifecycle {
    ignore_changes = [bucket_prefix]
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "mcp_content_bucket_lifecycle" {
  bucket = aws_s3_bucket.mcp_content_bucket.id
  rule {
    filter {
      prefix = "mcp-temp/"
    }
    expiration {
      days = 7
    }
    noncurrent_version_expiration {
      noncurrent_days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
    id = "DeleteMCPTempFiles"
    status = "Enabled"
  }
}

resource "aws_s3_bucket" "finetuning_data_bucket" {
  bucket_prefix = lower(substr("${var.stack_name}-FinetuningDataBucket", 0, 36))
  lifecycle {
    ignore_changes = [bucket_prefix]
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "finetuning_data_bucket_lifecycle" {
  bucket = aws_s3_bucket.finetuning_data_bucket.id
  rule {
    expiration {
      days = var.data_retention_in_days
    }
    noncurrent_version_expiration {
      noncurrent_days = var.data_retention_in_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
    id = "DeleteAfterNDays"
    status = "Enabled"
    filter {}
  }
}

resource "aws_cognito_user_pool" "api_user_pool" {
  count = local.c_deploy_api_gateway ? 1 : 0
  name = "${var.stack_name}-api"
  lifecycle {
    ignore_changes = [username_configuration]
  }
}

resource "aws_dynamodb_table" "chat_sessions_table" {
  name = "${substr("${var.stack_name}-ChatSessionsTable", 0, 246)}-${substr(md5("${var.stack_name}-ChatSessionsTable"), 0, 8)}"
  hash_key = "userId"
  lifecycle {
    ignore_changes = [name]
  }
}

resource "aws_dynamodb_table" "users_table" {
  name = "${substr("${var.stack_name}-UsersTable", 0, 246)}-${substr(md5("${var.stack_name}-UsersTable"), 0, 8)}"
  hash_key = "PK"
  lifecycle {
    ignore_changes = [name]
  }
}

resource "aws_dynamodb_table" "bootstrap_tracking_table" {
  name = "${substr("${var.stack_name}-BootstrapTrackingTable", 0, 246)}-${substr(md5("${var.stack_name}-BootstrapTrackingTable"), 0, 8)}"
  hash_key = "jobId"
  lifecycle {
    ignore_changes = [name]
  }
}

resource "aws_s3_bucket" "discovery_bucket" {
  bucket_prefix = lower(substr("${var.stack_name}-DiscoveryBucket", 0, 36))
  lifecycle {
    ignore_changes = [bucket_prefix]
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "discovery_bucket_lifecycle" {
  bucket = aws_s3_bucket.discovery_bucket.id
  rule {
    expiration {
      days = var.data_retention_in_days
    }
    noncurrent_version_expiration {
      noncurrent_days = var.data_retention_in_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
    id = "DeleteAfterNDays"
    status = "Enabled"
    filter {}
  }
}

resource "awscc_cognito_user_pool_group" "viewer_group" {
}

resource "aws_s3_bucket" "input_bucket" {
  bucket_prefix = lower(substr("${var.stack_name}-InputBucket", 0, 36))
  lifecycle {
    ignore_changes = [bucket_prefix]
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "input_bucket_lifecycle" {
  bucket = aws_s3_bucket.input_bucket.id
  rule {
    expiration {
      days = var.data_retention_in_days
    }
    noncurrent_version_expiration {
      noncurrent_days = var.data_retention_in_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
    id = "DeleteAfterNDays"
    status = "Enabled"
    filter {}
  }
}

resource "aws_dynamodb_table" "discovery_tracking_table" {
  name = "${substr("${var.stack_name}-DiscoveryTrackingTable", 0, 246)}-${substr(md5("${var.stack_name}-DiscoveryTrackingTable"), 0, 8)}"
  hash_key = "jobId"
  lifecycle {
    ignore_changes = [name]
  }
}

resource "aws_s3_bucket" "output_bucket" {
  bucket_prefix = lower(substr("${var.stack_name}-OutputBucket", 0, 36))
  lifecycle {
    ignore_changes = [bucket_prefix]
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "output_bucket_lifecycle" {
  bucket = aws_s3_bucket.output_bucket.id
  rule {
    expiration {
      days = var.data_retention_in_days
    }
    noncurrent_version_expiration {
      noncurrent_days = var.data_retention_in_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
    id = "DeleteAfterNDays"
    status = "Enabled"
    filter {}
  }
}

resource "awscc_cognito_user_pool_group" "admin_group" {
}

resource "aws_dynamodb_table" "hitl_table" {
  name = "${substr("${var.stack_name}-HITLTable", 0, 246)}-${substr(md5("${var.stack_name}-HITLTable"), 0, 8)}"
  hash_key = "PK"
  lifecycle {
    ignore_changes = [name]
  }
}

resource "aws_cognito_user_pool" "user_pool" {
  name = "${var.stack_name}-users"
  lifecycle {
    ignore_changes = [username_configuration]
  }
}

resource "aws_s3_bucket" "test_set_bucket" {
  bucket_prefix = lower(substr("${var.stack_name}-TestSetBucket", 0, 36))
  lifecycle {
    ignore_changes = [bucket_prefix]
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "test_set_bucket_lifecycle" {
  bucket = aws_s3_bucket.test_set_bucket.id
  rule {
    expiration {
      days = var.data_retention_in_days
    }
    noncurrent_version_expiration {
      noncurrent_days = var.data_retention_in_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
    id = "DeleteAfterNDays"
    status = "Enabled"
    filter {}
  }
}

resource "awscc_cognito_user_pool_group" "reviewer_group" {
}

resource "aws_dynamodb_table" "notifications_table" {
  name = "${substr("${var.stack_name}-NotificationsTable", 0, 246)}-${substr(md5("${var.stack_name}-NotificationsTable"), 0, 8)}"
  hash_key = "PK"
  lifecycle {
    ignore_changes = [name]
  }
}

resource "aws_dynamodb_table" "configuration_table" {
  name = "${substr("${var.stack_name}-ConfigurationTable", 0, 246)}-${substr(md5("${var.stack_name}-ConfigurationTable"), 0, 8)}"
  hash_key = "Configuration"
  lifecycle {
    ignore_changes = [name]
  }
}

module "apiresolverstack" {
  source = "./modules/apiresolverstack"
}

resource "aws_s3_bucket" "staging_bucket" {
  count = local.c_deploy_api_gateway ? 1 : 0
  bucket_prefix = lower(substr("${var.stack_name}-StagingBucket", 0, 36))
  lifecycle {
    ignore_changes = [bucket_prefix]
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "staging_bucket_lifecycle" {
  count = (local.c_deploy_api_gateway) ? 1 : 0
  bucket = aws_s3_bucket.staging_bucket[0].id
  rule {
    expiration {
      days = var.data_retention_in_days
    }
    noncurrent_version_expiration {
      noncurrent_days = var.data_retention_in_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
    id = "DeleteAfterNDays"
    status = "Enabled"
    filter {}
  }
}

resource "aws_s3_bucket" "reporting_bucket" {
  count = local.c_should_create_reporting_bucket ? 1 : 0
  bucket_prefix = lower(substr("${var.stack_name}-ReportingBucket", 0, 36))
  lifecycle {
    ignore_changes = [bucket_prefix]
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "reporting_bucket_lifecycle" {
  count = (local.c_should_create_reporting_bucket) ? 1 : 0
  bucket = aws_s3_bucket.reporting_bucket[0].id
  rule {
    filter {
      prefix = "athena-results/"
    }
    expiration {
      days = 30
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
    id = "DeleteAthenaQueryResults"
    status = "Enabled"
  }
  rule {
    filter {
      prefix = "athena_query_results/"
    }
    expiration {
      days = 30
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
    id = "DeleteAthenaRollupQueryResults"
    status = "Enabled"
  }
  rule {
    expiration {
      days = var.data_retention_in_days
    }
    noncurrent_version_expiration {
      noncurrent_days = var.data_retention_in_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
    id = "DeleteAfterNDays"
    status = "Enabled"
    filter {}
  }
}

resource "aws_dynamodb_table" "tracking_table" {
  name = "${substr("${var.stack_name}-TrackingTable", 0, 246)}-${substr(md5("${var.stack_name}-TrackingTable"), 0, 8)}"
  hash_key = "PK"
  lifecycle {
    ignore_changes = [name]
  }
}

resource "aws_cognito_user" "admin_user" {
  username = var.admin_email
  lifecycle {
    ignore_changes = [attributes, desired_delivery_mediums, message_action]
  }
}

resource "aws_dynamodb_table" "agent_table" {
  name = "${substr("${var.stack_name}-AgentTable", 0, 246)}-${substr(md5("${var.stack_name}-AgentTable"), 0, 8)}"
  hash_key = "PK"
  lifecycle {
    ignore_changes = [name]
  }
}

resource "aws_dynamodb_table" "concurrency_table" {
  name = "${substr("${var.stack_name}-ConcurrencyTable", 0, 246)}-${substr(md5("${var.stack_name}-ConcurrencyTable"), 0, 8)}"
  hash_key = "counter_id"
  lifecycle {
    ignore_changes = [name]
  }
}

resource "aws_dynamodb_table" "id_helper_chat_memory_table" {
  name = "${substr("${var.stack_name}-IdHelperChatMemoryTable", 0, 246)}-${substr(md5("${var.stack_name}-IdHelperChatMemoryTable"), 0, 8)}"
  hash_key = "PK"
  lifecycle {
    ignore_changes = [name]
  }
}

resource "aws_s3_bucket" "configuration_bucket" {
  bucket_prefix = lower(substr("${var.stack_name}-ConfigurationBucket", 0, 36))
  lifecycle {
    ignore_changes = [bucket_prefix]
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "configuration_bucket_lifecycle" {
  bucket = aws_s3_bucket.configuration_bucket.id
  rule {
    expiration {
      days = var.data_retention_in_days
    }
    noncurrent_version_expiration {
      noncurrent_days = var.data_retention_in_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
    id = "DeleteAfterNDays"
    status = "Enabled"
    filter {}
  }
}

resource "aws_s3_bucket" "logging_bucket" {
  bucket_prefix = lower(substr("${var.stack_name}-LoggingBucket", 0, 36))
  lifecycle {
    ignore_changes = [bucket_prefix]
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logging_bucket_lifecycle" {
  bucket = aws_s3_bucket.logging_bucket.id
  rule {
    expiration {
      days = 180
    }
    filter {
      prefix = ""
    }
    noncurrent_version_expiration {
      noncurrent_days = 180
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
    id = "DeleteOldLogs"
    status = "Enabled"
  }
}

resource "aws_s3_bucket" "web_ui_bucket" {
  bucket_prefix = lower(substr("${var.stack_name}-WebUIBucket", 0, 36))
  lifecycle {
    ignore_changes = [bucket_prefix]
  }
}

resource "awscc_kms_key" "customer_managed_encryption_key" {
}

resource "aws_s3_bucket" "working_bucket" {
  bucket_prefix = lower(substr("${var.stack_name}-WorkingBucket", 0, 36))
  lifecycle {
    ignore_changes = [bucket_prefix]
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "working_bucket_lifecycle" {
  bucket = aws_s3_bucket.working_bucket.id
  rule {
    expiration {
      days = var.log_retention_days
    }
    noncurrent_version_expiration {
      noncurrent_days = var.log_retention_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
    id = "DeleteAfterNDays"
    status = "Enabled"
    filter {}
  }
}

resource "awscc_cognito_user_pool_group" "annotator_group" {
}

resource "awscc_cognito_user_pool_group" "author_group" {
}

resource "aws_dynamodb_table" "chat_messages_table" {
  name = "${substr("${var.stack_name}-ChatMessagesTable", 0, 246)}-${substr(md5("${var.stack_name}-ChatMessagesTable"), 0, 8)}"
  hash_key = "PK"
  lifecycle {
    ignore_changes = [name]
  }
}

resource "awscc_kms_key" "bastion_key" {
  count = local.c_should_deploy_bastion_host ? 1 : 0
}

resource "aws_s3_bucket" "evaluation_baseline_bucket" {
  count = local.c_should_create_evaluation_baseline_bucket ? 1 : 0
  bucket_prefix = lower(substr("${var.stack_name}-EvaluationBaselineBucket", 0, 36))
  lifecycle {
    ignore_changes = [bucket_prefix]
  }
}

resource "aws_cognito_user_in_group" "admin_user_to_group_attachment" {
  username = aws_cognito_user.admin_user.username
}
