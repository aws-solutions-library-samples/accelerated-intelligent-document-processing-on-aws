# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`CustomerManagedKmsKeyArn` is fixed when a deployment is created (#2016).

The template's own check refuses a change and rolls the update back; the SDK
refuses it before CloudFormation or Terraform is called, on both deploy paths.
Terraform matters most, because it has no rollback: without this refusal a
changed key would be applied part-way before the check failed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from idp_sdk._core import terraform_deploy as tf
from idp_sdk._core.stack import (
    CREATION_FIXED_PARAMETERS,
    guard_creation_fixed_parameters,
)
from idp_sdk.exceptions import IDPConfigurationError

pytestmark = pytest.mark.unit

NAME = "CustomerManagedKmsKeyArn"
KEY = "arn:aws:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab"
OTHER = "arn:aws:kms:us-east-1:111122223333:key/0987dcba-09fe-87dc-65ba-ab0987654321"


def test_registered_with_the_empty_default():
    assert CREATION_FIXED_PARAMETERS[NAME] == ""


@pytest.mark.parametrize(
    "current,asked",
    [({}, KEY), ({NAME: ""}, KEY), ({NAME: KEY}, ""), ({NAME: KEY}, OTHER)],
    ids=["pre-feature-stack", "set-on-existing", "cleared", "swapped"],
)
def test_a_change_on_update_is_refused(current, asked):
    with pytest.raises(ValueError, match="chooses the KMS key"):
        guard_creation_fixed_parameters(
            {NAME: asked}, stack_exists=True, current_params=current
        )


def test_the_same_key_passes_on_update():
    out = guard_creation_fixed_parameters(
        {NAME: KEY}, stack_exists=True, current_params={NAME: KEY}
    )
    assert out == {NAME: KEY}


def test_a_key_for_a_template_without_the_parameter_is_refused_not_dropped():
    with pytest.raises(ValueError, match="does not declare it"):
        guard_creation_fixed_parameters(
            {NAME: KEY}, stack_exists=False, declared_params={"AdminEmail"}
        )


def test_the_empty_default_for_such_a_template_is_dropped():
    out = guard_creation_fixed_parameters(
        {NAME: "", "AdminEmail": "a@b.c"},
        stack_exists=False,
        declared_params={"AdminEmail"},
    )
    assert out == {"AdminEmail": "a@b.c"}


MODULE = {
    "formatVersion": 1,
    "version": "0.6.16",
    "parameters": {
        "AdminEmail": {"variable": "admin_email", "type": "String", "required": True},
        NAME: {"variable": "customer_managed_kms_key_arn", "type": "String"},
        "LogLevel": {"variable": "log_level", "type": "String"},
    },
}


def _write(directory: Path, parameters: dict, recorded: dict | None):
    return tf.write_root_config(
        directory,
        region="us-east-1",
        module_url="https://s3.us-east-1.amazonaws.com/b/p/terraform/m.zip",
        stack_name="tfidp",
        admin_email="a@b.c",
        parameters=parameters,
        manifest=MODULE,
        recorded=recorded,
    )


@pytest.mark.parametrize(
    "recorded,asked",
    [({NAME: ""}, KEY), ({NAME: KEY}, ""), ({NAME: KEY}, OTHER), ({}, KEY)],
    ids=["set-on-existing", "cleared", "swapped", "recorded-before-the-parameter"],
)
def test_terraform_refuses_a_changed_key_on_an_existing_deployment(
    tmp_path, recorded, asked
):
    with pytest.raises(IDPConfigurationError, match="cannot be changed"):
        _write(tmp_path, {NAME: asked}, recorded)
    assert not (tmp_path / "terraform.tfvars").exists()


def test_terraform_keeps_an_unchanged_key_and_allows_it_on_a_new_deployment(
    tmp_path,
):
    values = _write(tmp_path / "upgrade", {NAME: KEY, "LogLevel": "INFO"}, {NAME: KEY})
    assert values["customer_managed_kms_key_arn"] == KEY
    values = _write(tmp_path / "new", {NAME: KEY}, None)
    assert values["customer_managed_kms_key_arn"] == KEY


def test_terraform_does_not_police_other_parameters(tmp_path):
    values = _write(tmp_path, {"LogLevel": "DEBUG"}, {NAME: KEY, "LogLevel": "INFO"})
    assert values["log_level"] == "DEBUG"
    assert values["customer_managed_kms_key_arn"] == KEY


def test_terraform_leaves_the_migration_derived_flag_alone(tmp_path):
    """terraform_migrate derives ExternalIdPEmailMutable from the adopted pool,
    so the Terraform path polices only the key, not every creation-fixed name."""
    module = {
        **MODULE,
        "parameters": {
            **MODULE["parameters"],
            "ExternalIdPEmailMutable": {
                "variable": "external_id_p_email_mutable",
                "type": "String",
            },
        },
    }
    values = tf.write_root_config(
        tmp_path,
        region="us-east-1",
        module_url="https://s3.us-east-1.amazonaws.com/b/p/terraform/m.zip",
        stack_name="tfidp",
        admin_email="a@b.c",
        parameters={"ExternalIdPEmailMutable": "true"},
        manifest=module,
        recorded={"ExternalIdPEmailMutable": "false"},
    )
    assert values["external_id_p_email_mutable"] == "true"
