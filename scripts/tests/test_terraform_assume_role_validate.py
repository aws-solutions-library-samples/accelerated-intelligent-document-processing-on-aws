# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The provider blocks `idp-cli deploy --terraform --terraform-assume-role-arn`
writes are accepted by real Terraform with the pinned providers.

`hashicorp/aws` takes `assume_role` as a nested block and `hashicorp/awscc` as an
object attribute; a string-level test asserting one spelling for both passed while
`terraform validate` refused the file. So this renders the root configuration with
the SDK's own writer and runs the pinned Terraform's `init` and `validate` over it.

Offline by construction: the binary and the providers come from the offline
Terraform gate's cache (`scripts/terraform_check.py`, default
`~/.cache/idp-terraform-check`, or `IDP_TERRAFORM_CHECK_CACHE`), installed with
`init -plugin-dir`, so nothing is downloaded. Without that cache the test skips and
says so; `make terraform-check` (or the gate in CI) fills it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
ROLE = "arn:aws:iam::123456789012:role/idp-0101-000000-iam-TFDeployRole"
MODULE_URL = "https://s3.us-east-1.amazonaws.com/b/p/1/terraform/idp-terraform-1.zip"
TERRAFORM = "terraform@1.17.1"


def _gate():
    scripts = str(REPO_ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import terraform_check

    return terraform_check


@pytest.fixture(scope="module")
def offline_terraform():
    """(binary, plugin dir, provider pins), or a skip naming what is missing."""
    check = _gate()
    cache = Path(
        os.environ.get("IDP_TERRAFORM_CHECK_CACHE", "~/.cache/idp-terraform-check")
    ).expanduser()
    try:
        plat = check.current_platform()
    except check.GateError as e:
        pytest.skip(f"no pinned Terraform for this platform: {e}")
    binary = check.Binary.parse(TERRAFORM)
    exe = cache / "bin" / binary.tool / binary.version / plat / binary.tool
    if not exe.is_file():
        pytest.skip(
            f"{binary} is not in the offline gate's cache at {exe}; run "
            "`python3 scripts/terraform_check.py` once to fill it"
        )
    # Installed only when the cached digest matches; never downloads here.
    exe = check.install(binary, cache, plat)
    providers = check.gate_providers([])
    plugins = cache / "plugins"
    for source, version in providers.items():
        provider_dir = plugins / "registry.terraform.io" / source / version / plat
        if not provider_dir.is_dir():
            pytest.skip(f"{source} {version} is not in the cache at {provider_dir}")
    return exe, plugins, check.override_tf(providers)


def _root(tmp_path, offline_terraform, role, rewrite=None):
    """The SDK-rendered root configuration, pointed at a local stand-in module."""
    from idp_sdk._core import terraform_deploy as tf  # noqa: TID251

    _, _, pins = offline_terraform
    main = tf.render_main_tf("us-east-1", MODULE_URL, {}, {}, role)
    if rewrite:
        main = rewrite(main)
    main = re.sub(r'source = "s3::[^"]+"', 'source = "./mod"', main)
    (tmp_path / "main.tf").write_text(main)
    (tmp_path / "zz_pins_override.tf").write_text(pins)
    module = tmp_path / "mod"
    module.mkdir()
    (module / "main.tf").write_text(
        "terraform {\n  required_providers {\n"
        '    aws   = { source = "hashicorp/aws" }\n'
        '    awscc = { source = "hashicorp/awscc" }\n'
        "  }\n}\n"
        # One use of each provider. Terraform validates a provider block only for
        # a provider something uses, so a module using neither would pass any
        # provider configuration at all, the invalid one included.
        'data "aws_partition" "p" {}\n'
        'resource "awscc_logs_log_group" "g" {}\n'
    )
    return tmp_path


def _validate(directory, offline_terraform):
    exe, plugins, _ = offline_terraform
    env = {
        **os.environ,
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
        "CHECKPOINT_DISABLE": "1",
    }
    env.pop("TF_CLI_ARGS", None)
    init = subprocess.run(  # noqa: S603 - fixed argv
        [
            str(exe),
            "init",
            "-backend=false",
            "-input=false",
            "-no-color",
            f"-plugin-dir={plugins}",
        ],
        cwd=directory,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=300,
    )
    assert init.returncode == 0, init.stdout + init.stderr
    return subprocess.run(  # noqa: S603 - fixed argv
        [str(exe), "validate", "-no-color"],
        cwd=directory,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=300,
    )


def test_the_rendered_role_validates_with_both_pinned_providers(
    tmp_path, offline_terraform
):
    result = _validate(_root(tmp_path, offline_terraform, ROLE), offline_terraform)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_rewritten_delete_configuration_validates(tmp_path, offline_terraform):
    """`delete --terraform --terraform-assume-role-arn` rewrites a role-less file."""
    from idp_sdk._core import terraform_deploy as tf  # noqa: TID251

    directory = _root(tmp_path, offline_terraform, None)
    main = directory / "main.tf"
    main.write_text(tf.ROOT_MARKER + "\n" + main.read_text())
    tf.set_assume_role(main, ROLE, "us-east-1")
    result = _validate(directory, offline_terraform)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_block_on_awscc_is_refused_so_this_check_can_fail(
    tmp_path, offline_terraform
):
    """The negative control: the spelling this replaced is refused by validate."""

    def _awscc_as_block(text):
        return text.replace(
            'provider "awscc" {\n  region = "us-east-1"\n  assume_role = {',
            'provider "awscc" {\n  region = "us-east-1"\n  assume_role {',
        )

    directory = _root(tmp_path, offline_terraform, ROLE, rewrite=_awscc_as_block)
    assert (
        "assume_role {"
        in (directory / "main.tf").read_text().split('provider "awscc"')[1]
    )
    result = _validate(directory, offline_terraform)
    assert result.returncode != 0
    assert "assume_role" in result.stdout + result.stderr
