variable "stack_name" {
  type = string
  validation {
    condition = length(var.stack_name) <= 25
    error_message = "stack_name must be at most 25 characters."
  }
  validation {
    condition = can(regex("^[A-Za-z][A-Za-z0-9-]*$", var.stack_name))
    error_message = "stack_name must start with a letter and contain only letters, digits and hyphens (a CloudFormation stack name)."
  }
}

variable "admin_email" {
  type = string
  validation {
    condition = can(regex("^(?:^[\\w.+-]+@([\\w-]+\\.)+[\\w-]{2,6}$)$", tostring(var.admin_email)))
    error_message = "admin_email must match ^[\\w.+-]+@([\\w-]+\\.)+[\\w-]{2,6}$."
  }
}

variable "data_retention_in_days" {
  type = number
  default = 365
  validation {
    condition = var.data_retention_in_days >= 1
    error_message = "data_retention_in_days must be at least 1."
  }
}

variable "log_retention_days" {
  type = number
  default = 30
  validation {
    condition = contains(["1", "3", "5", "7", "14", "30", "60", "90", "120", "150", "180", "365", "400", "545", "731", "1827", "3653"], tostring(var.log_retention_days))
    error_message = "log_retention_days must be one of [1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1827, 3653]."
  }
}

variable "external_idp_email_mutable" {
  type = string
  default = "false"
  validation {
    condition = contains(["true", "false"], tostring(var.external_idp_email_mutable))
    error_message = "external_idp_email_mutable must be one of ['true', 'false']."
  }
}

variable "reporting_bucket_name" {
  type = string
  default = ""
}
