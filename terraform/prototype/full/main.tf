# Prototype fixture: deploys the whole generated module tree (main template plus
# its five nested stacks). Throwaway deployment for end-to-end testing.

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
variable "stack_name" {
  type    = string
  default = "tfidp1"
}
variable "admin_email" { type = string }
variable "parameters" {
  description = "Extra stack parameters, by Terraform variable name."
  type        = map(string)
  default     = {}
}

provider "aws" {
  region = var.region
}
provider "awscc" {
  region = var.region
}

module "idp" {
  source      = "../../build/full"
  stack_name  = var.stack_name
  admin_email = var.admin_email
}

output "label_stack" {
  value = "${var.stack_name}-tf-label"
}
