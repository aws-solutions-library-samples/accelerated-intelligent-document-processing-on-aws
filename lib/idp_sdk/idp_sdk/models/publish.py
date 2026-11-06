# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Publish-related models."""

from typing import Optional

from pydantic import BaseModel, Field


class PublishResult(BaseModel):
    """Result of a publish (build + upload) operation."""

    success: bool = Field(description="Whether the publish succeeded")
    template_path: Optional[str] = Field(
        default=None, description="Local path to the built template"
    )
    template_url: Optional[str] = Field(
        default=None, description="S3 URL of the uploaded template"
    )
    headless_template_path: Optional[str] = Field(
        default=None, description="Local path to the headless template (if --headless)"
    )
    headless_template_url: Optional[str] = Field(
        default=None, description="S3 URL of the headless template (if --headless)"
    )
    govcloud_template_path: Optional[str] = Field(
        default=None,
        description="Local path to the GovCloud (CloudFront-free) template (if --govcloud)",
    )
    govcloud_template_url: Optional[str] = Field(
        default=None,
        description="S3 URL of the GovCloud (CloudFront-free) template (if --govcloud)",
    )
    bucket: Optional[str] = Field(
        default=None, description="S3 bucket name used for artifacts"
    )
    prefix: Optional[str] = Field(
        default=None, description="S3 prefix used for artifacts"
    )
    version: Optional[str] = Field(
        default=None, description="Version string from VERSION file"
    )
    terraform_module_url: Optional[str] = Field(
        default=None,
        description="HTTPS S3 URL of the Terraform module zip (if --terraform)",
    )
    terraform_module_sha256: Optional[str] = Field(
        default=None, description="SHA-256 of the Terraform module zip"
    )
    terraform_pointer_url: Optional[str] = Field(
        default=None,
        description="HTTPS S3 URL of idp-terraform-latest.json (if --terraform)",
    )
    error: Optional[str] = Field(
        default=None, description="Error message if publish failed"
    )


class TemplateTransformResult(BaseModel):
    """Result of a headless template transformation."""

    success: bool = Field(description="Whether the transformation succeeded")
    input_path: str = Field(description="Path to the source template")
    output_path: Optional[str] = Field(
        default=None, description="Path to the transformed headless template"
    )
    error: Optional[str] = Field(
        default=None, description="Error message if transformation failed"
    )
