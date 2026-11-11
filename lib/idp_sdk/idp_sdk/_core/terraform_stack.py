# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The SDK's Terraform-deployment hook. The implementation lives in
``idp_common.utils.terraform_stack`` so that run-time code inside a deployment
can use it too; this module re-exports it, including the module state the
tests inspect."""

from idp_common.utils.terraform_stack import (  # noqa: F401
    DIRECTORY_ENV,
    DIRECTORY_OUTPUT,
    SERVED,
    TERRAFORM_SUFFIX,
    _load_root,
    _lock,
    _nested,
    _register_nested,
    _roots,
    attach,
    install,
    install_if_terraform,
    is_terraform_label,
    is_terraform_stack,
    reset_cache,
)
