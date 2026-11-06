# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Terraform publish, deploy and delete: the logic behind `--terraform`.

Everything here is offline. `subprocess.run`, `shutil.which` and S3 are replaced,
so a test measures what the SDK decides and which commands it would run, never a
real binary or bucket.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from idp_sdk import IDPClient, default_terraform_pointer_url
from idp_sdk._core import terraform_deploy as tf
from idp_sdk.exceptions import IDPConfigurationError, IDPStackError

pytestmark = pytest.mark.unit

REGION = "us-west-2"
BUCKET = "tfspike-111122223333-us-west-2"

MANIFEST = {
    "formatVersion": 1,
    "version": "0.6.13",
    "parameters": {
        "AdminEmail": {
            "variable": "admin_email",
            "type": "String",
            "required": True,
            "sensitive": False,
        },
        "MaxConcurrentWorkflows": {
            "variable": "max_concurrent_workflows",
            "type": "Number",
            "required": False,
            "sensitive": False,
        },
        "SubnetIds": {
            "variable": "subnet_ids",
            "type": "List<AWS::EC2::Subnet::Id>",
            "required": False,
            "sensitive": False,
        },
        "AllowedCidrs": {
            "variable": "allowed_cidrs",
            "type": "CommaDelimitedList",
            "required": False,
            "sensitive": False,
        },
        "ApiKey": {
            "variable": "api_key",
            "type": "String",
            "required": False,
            "sensitive": True,
        },
    },
    "retainedAddresses": [
        "aws_s3_bucket.logging_bucket",
        "module.patternstack.aws_dynamodb_table.tracking",
        "aws_cloudwatch_log_group.never_created",
    ],
}


def _module_zip(manifest=MANIFEST) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("main.tf", "# module\n")
        zf.writestr(tf.MANIFEST_NAME, json.dumps(manifest))
    return buf.getvalue()


# --------------------------------------------------------------------------------------
# URLs, keys and the pointer
# --------------------------------------------------------------------------------------


class TestUrlsAndPointer:
    def test_keys_sit_under_the_version_prefix_and_the_pointer_above_it(self):
        assert (
            tf.module_zip_key("idp", "0.6.13")
            == "idp/0.6.13/terraform/idp-terraform-0.6.13.zip"
        )
        assert tf.pointer_key("idp") == "idp/terraform/idp-terraform-latest.json"

    def test_module_url_is_https_s3_ending_zip(self):
        url = tf.s3_https_url(REGION, BUCKET, tf.module_zip_key("idp", "1.2.3"))
        assert url == (
            f"https://s3.us-west-2.amazonaws.com/{BUCKET}/"
            "idp/1.2.3/terraform/idp-terraform-1.2.3.zip"
        )
        assert url.endswith(".zip")

    def test_pointer_body(self):
        assert tf.build_pointer("1.2.3", "https://x/m.zip", "ab" * 32) == {
            "version": "1.2.3",
            "moduleUrl": "https://x/m.zip",
            "sha256": "ab" * 32,
        }

    def test_sha256_file_is_sha256sum_format(self):
        assert tf.sha256_file_body("cafe", "m.zip") == "cafe  m.zip\n"

    def test_default_pointer_mirrors_the_default_template_url(self):
        template = (
            "https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/"
            "artifacts/genai-idp/idp-main.yaml"
        )
        assert default_terraform_pointer_url(template) == (
            "https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/"
            "artifacts/genai-idp/terraform/idp-terraform-latest.json"
        )

    @pytest.mark.parametrize(
        "url,expected",
        [
            (
                "https://s3.us-west-2.amazonaws.com/b/k/m.zip",
                ("us-west-2", "b", "k/m.zip"),
            ),
            (
                "https://s3-eu-central-1.amazonaws.com/b/m.zip",
                ("eu-central-1", "b", "m.zip"),
            ),
            (
                "https://b.s3.ca-central-1.amazonaws.com/k/m.zip",
                ("ca-central-1", "b", "k/m.zip"),
            ),
            ("https://example.com/m.zip", None),
        ],
    )
    def test_parse_s3_https_url(self, url, expected):
        assert tf.parse_s3_https_url(url) == expected

    def test_module_source_uses_the_s3_getter_in_three_label_form(self):
        src = tf.terraform_module_source("https://s3.us-west-2.amazonaws.com/b/k/m.zip")
        assert src == "s3::https://s3-us-west-2.amazonaws.com/b/k/m.zip"
        assert len(src.removeprefix("s3::https://").split("/")[0].split(".")) == 3

    def test_module_source_leaves_other_urls_alone(self):
        assert tf.terraform_module_source("https://x.test/m.zip") == (
            "https://x.test/m.zip"
        )
        assert tf.terraform_module_source("s3::https://s3-a.amazonaws.com/b/k") == (
            "s3::https://s3-a.amazonaws.com/b/k"
        )


# --------------------------------------------------------------------------------------
# The zip
# --------------------------------------------------------------------------------------


def _tree(root: Path) -> None:
    (root / "modules" / "pattern").mkdir(parents=True)
    (root / "main.tf").write_text("a")
    (root / "modules" / "pattern" / "main.tf").write_text("b")
    (root / tf.MANIFEST_NAME).write_text("{}")
    (root / ".terraform" / "providers").mkdir(parents=True)
    (root / ".terraform" / "providers" / "big").write_text("x")
    (root / "modules" / "pattern" / ".build").mkdir()
    (root / "modules" / "pattern" / ".build" / "scratch").write_text("y")


class TestVariantNames:
    @pytest.mark.parametrize("variant", ["headless", "govcloud"])
    def test_variant_keys(self, variant):
        assert tf.module_zip_key("idp", "0.6.13", variant) == (
            f"idp/0.6.13/terraform/idp-terraform-{variant}-0.6.13.zip"
        )
        assert tf.pointer_key("idp", variant) == (
            f"idp/terraform/idp-terraform-{variant}-latest.json"
        )
        assert tf.variant_template_key("idp", variant) == f"idp/idp-{variant}.yaml"

    def test_the_standard_names_are_unchanged(self):
        assert tf.module_zip_key("idp", "1") == "idp/1/terraform/idp-terraform-1.zip"
        assert tf.pointer_key("idp") == "idp/terraform/idp-terraform-latest.json"
        assert tf.variant_template_key("idp") == "idp/idp-main.yaml"

    @pytest.mark.parametrize("variant", [None, "headless", "govcloud"])
    def test_default_pointer_selects_the_variant(self, variant):
        template = (
            "https://s3.us-east-1.amazonaws.com/rel/artifacts/genai-idp/idp-main.yaml"
        )
        stem = f"idp-terraform-{variant}" if variant else "idp-terraform"
        assert default_terraform_pointer_url(template, variant) == (
            f"https://s3.us-east-1.amazonaws.com/rel/artifacts/genai-idp/terraform/"
            f"{stem}-latest.json"
        )

    def test_upload_uses_the_variant_names(self, tmp_path):
        _tree(tmp_path)
        s3 = MagicMock()
        info = tf.upload_module(
            s3,
            module_dir=tmp_path,
            bucket=BUCKET,
            prefix="p",
            region=REGION,
            version="2",
            variant="headless",
        )
        keys = sorted(c.kwargs["Key"] for c in s3.put_object.call_args_list)
        assert keys == [
            "p/2/terraform/idp-terraform-headless-2.zip",
            "p/2/terraform/idp-terraform-headless-2.zip.sha256",
            "p/terraform/idp-terraform-headless-latest.json",
        ]
        assert info["moduleUrl"].endswith("idp-terraform-headless-2.zip")

    def test_unknown_variant_is_refused(self):
        with pytest.raises(IDPConfigurationError, match="unknown Terraform"):
            tf.pointer_key("idp", "minimal")

    def test_admin_email_is_ignored_by_a_module_without_admin_email(self, tmp_path):
        manifest = {
            "parameters": {
                k: v for k, v in MANIFEST["parameters"].items() if k != "AdminEmail"
            }
        }
        values = _write(tmp_path, manifest=manifest)
        assert "admin_email" not in values
        assert "admin_email" not in (tmp_path / "main.tf").read_text()


class TestZip:
    def test_same_files_give_the_same_bytes_whatever_the_mtime_or_order(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        _tree(a)
        _tree(b)
        os.utime(b / "main.tf", (1_000_000_000, 1_000_000_000))
        assert tf.deterministic_module_zip(a) == tf.deterministic_module_zip(b)

    def test_entries_are_sorted_with_fixed_times_and_skip_caches(self, tmp_path):
        _tree(tmp_path)
        with zipfile.ZipFile(io.BytesIO(tf.deterministic_module_zip(tmp_path))) as zf:
            names = zf.namelist()
            assert names == sorted(names)
            assert names == [tf.MANIFEST_NAME, "main.tf", "modules/pattern/main.tf"]
            assert {i.date_time for i in zf.infolist()} == {(1980, 1, 1, 0, 0, 0)}

    def test_content_change_changes_the_bytes(self, tmp_path):
        _tree(tmp_path)
        before = tf.deterministic_module_zip(tmp_path)
        (tmp_path / "main.tf").write_text("changed")
        assert tf.deterministic_module_zip(tmp_path) != before

    def test_manifest_is_read_from_the_zip_root(self):
        assert tf.manifest_from_zip(_module_zip()) == MANIFEST

    def test_a_zip_without_a_manifest_is_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("main.tf", "")
        with pytest.raises(IDPConfigurationError, match=tf.MANIFEST_NAME):
            tf.manifest_from_zip(buf.getvalue())


# --------------------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------------------


class TestUpload:
    def test_uploads_zip_checksum_and_pointer(self, tmp_path):
        _tree(tmp_path)
        s3 = MagicMock()
        info = tf.upload_module(
            s3,
            module_dir=tmp_path,
            bucket=BUCKET,
            prefix="tfspike",
            region=REGION,
            version="0.6.13",
        )
        puts = {c.kwargs["Key"]: c.kwargs for c in s3.put_object.call_args_list}
        zip_key = "tfspike/0.6.13/terraform/idp-terraform-0.6.13.zip"
        assert set(puts) == {
            zip_key,
            zip_key + ".sha256",
            "tfspike/terraform/idp-terraform-latest.json",
        }
        digest = hashlib.sha256(puts[zip_key]["Body"]).hexdigest()
        assert info["sha256"] == digest
        assert puts[zip_key + ".sha256"]["Body"].decode().startswith(digest + "  ")
        pointer = json.loads(
            puts["tfspike/terraform/idp-terraform-latest.json"]["Body"]
        )
        assert pointer == {
            "version": "0.6.13",
            "moduleUrl": f"https://s3.us-west-2.amazonaws.com/{BUCKET}/{zip_key}",
            "sha256": digest,
        }
        assert info["moduleUrl"] == pointer["moduleUrl"]
        assert all("ACL" not in kw for kw in puts.values())

    def test_public_applies_the_acl(self, tmp_path):
        _tree(tmp_path)
        s3 = MagicMock()
        tf.upload_module(
            s3,
            module_dir=tmp_path,
            bucket=BUCKET,
            prefix="p",
            region=REGION,
            version="1",
            public=True,
        )
        assert all(
            c.kwargs.get("ACL") == "public-read" for c in s3.put_object.call_args_list
        )


class TestTranslatorCommand:
    def test_online_translation_with_label_and_version(self, tmp_path):
        (tmp_path / "terraform" / "translator" / "idp_tf").mkdir(parents=True)
        (tmp_path / "terraform" / "translator" / "idp_tf" / "cli.py").write_text("")
        cmd, env = tf.translator_command(str(tmp_path), "t.yaml", "out", "cache", "9")
        assert cmd[1:4] == ["-m", "idp_tf.cli", "t.yaml"]
        assert "--offline" not in cmd
        assert cmd[cmd.index("--nested-cache") + 1] == "cache"
        assert cmd[cmd.index("--label-template") + 1] == "t.yaml"
        assert cmd[cmd.index("--version") + 1] == "9"
        assert env["PYTHONPATH"].split(os.pathsep)[0] == str(
            tmp_path / "terraform" / "translator"
        )

    def test_missing_translator_is_refused(self, tmp_path):
        with pytest.raises(IDPConfigurationError, match="translator not found"):
            tf.translator_command(str(tmp_path), "t", "o", "c", "1")

    def test_translator_failure_is_raised(self, tmp_path, monkeypatch):
        (tmp_path / "terraform" / "translator" / "idp_tf").mkdir(parents=True)
        (tmp_path / "terraform" / "translator" / "idp_tf" / "cli.py").write_text("")
        monkeypatch.setattr(
            tf.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "boom"),
        )
        with pytest.raises(IDPStackError, match="boom"):
            tf.translate_published_template(
                str(tmp_path), "t.yaml", str(tmp_path / "w"), "1"
            )


# --------------------------------------------------------------------------------------
# Parameter mapping
# --------------------------------------------------------------------------------------


class TestParameterMapping:
    def test_types_are_converted(self):
        out = tf.map_parameters(
            {
                "MaxConcurrentWorkflows": "50",
                "SubnetIds": "subnet-1, subnet-2",
                "AllowedCidrs": "",
                "AdminEmail": "a@example.com",
            },
            MANIFEST,
        )
        assert out == {
            "max_concurrent_workflows": 50,
            "subnet_ids": ["subnet-1", "subnet-2"],
            "allowed_cidrs": [],
            "admin_email": "a@example.com",
        }

    def test_fractional_number(self):
        out = tf.map_parameters({"MaxConcurrentWorkflows": "2.5"}, MANIFEST)
        assert out["max_concurrent_workflows"] == 2.5

    def test_non_numeric_number_is_refused(self):
        with pytest.raises(IDPConfigurationError, match="Number"):
            tf.map_parameters({"MaxConcurrentWorkflows": "lots"}, MANIFEST)

    def test_unknown_names_are_refused_together(self):
        with pytest.raises(IDPConfigurationError) as exc:
            tf.map_parameters({"Nope": "1", "Also": "2", "AdminEmail": "x"}, MANIFEST)
        assert "Also, Nope" in str(exc.value)

    def test_required_missing(self):
        assert tf.missing_required({"stack_name": "s"}, MANIFEST) == ["AdminEmail"]
        assert tf.missing_required({"admin_email": "a"}, MANIFEST) == []


# --------------------------------------------------------------------------------------
# Root configuration
# --------------------------------------------------------------------------------------

MODULE_URL = f"https://s3.us-west-2.amazonaws.com/{BUCKET}/p/1/terraform/m.zip"
ROLE = "arn:aws:iam::123456789012:role/idp-tf-deployer"


def _write(directory, **kw):
    args = dict(
        region=REGION,
        module_url=MODULE_URL,
        stack_name="tfidp",
        admin_email="a@example.com",
        parameters={},
        manifest=MANIFEST,
    )
    args.update(kw)
    return tf.write_root_config(directory, **args)


class TestRootConfig:
    def test_writes_main_tf_and_tfvars(self, tmp_path):
        values = _write(
            tmp_path, parameters={"MaxConcurrentWorkflows": "7", "ApiKey": "${x}"}
        )
        main = (tmp_path / "main.tf").read_text()
        assert main.startswith(tf.ROOT_MARKER)
        assert 'provider "aws" {\n  region = "us-west-2"' in main
        assert 'provider "awscc" {\n  region = "us-west-2"' in main
        assert (
            'source = "s3::https://s3-us-west-2.amazonaws.com/'
            f'{BUCKET}/p/1/terraform/m.zip"'
        ) in main
        assert 'variable "max_concurrent_workflows" {\n  type = number' in main
        assert 'variable "api_key" {\n  type = string\n  sensitive = true' in main
        assert "  stack_name = var.stack_name" in main
        assert 'backend "' not in main
        tfvars = (tmp_path / "terraform.tfvars").read_text()
        assert 'stack_name = "tfidp"' in tfvars
        assert "max_concurrent_workflows = 7" in tfvars
        # A template sequence is escaped, so Terraform reads it literally.
        assert 'api_key = "$${x}"' in tfvars
        assert tf.parse_tfvars(tfvars) == values
        assert values["api_key"] == "${x}"
        assert (tmp_path / "terraform.tfvars").stat().st_mode & 0o077 == 0

    def test_list_values_render_as_hcl_lists(self, tmp_path):
        _write(tmp_path, parameters={"SubnetIds": "s-1,s-2"})
        assert (
            'subnet_ids = ["s-1", "s-2"]' in (tmp_path / "terraform.tfvars").read_text()
        )
        assert "type = list(string)" in (tmp_path / "main.tf").read_text()

    def test_refuses_to_overwrite_a_foreign_main_tf(self, tmp_path):
        (tmp_path / "main.tf").write_text('terraform { backend "s3" {} }\n')
        with pytest.raises(IDPConfigurationError, match="not written by idp-cli"):
            _write(tmp_path)
        assert "backend" in (tmp_path / "main.tf").read_text()
        assert not (tmp_path / "terraform.tfvars").exists()

    def test_rewrites_its_own_main_tf_and_keeps_earlier_values(self, tmp_path):
        _write(tmp_path, parameters={"MaxConcurrentWorkflows": "7"})
        values = _write(tmp_path, admin_email=None, parameters={"SubnetIds": "s-1"})
        assert values == {
            "stack_name": "tfidp",
            "admin_email": "a@example.com",
            "max_concurrent_workflows": 7,
            "subnet_ids": ["s-1"],
        }

    def test_a_renamed_variable_keeps_its_value(self, tmp_path):
        """Values are remembered by CloudFormation name, so a module that
        renames the variable still receives the earlier value."""
        _write(tmp_path, parameters={"MaxConcurrentWorkflows": "7"})
        assert (
            json.loads((tmp_path / tf.PARAMETERS_FILE).read_text())[
                "MaxConcurrentWorkflows"
            ]
            == 7
        )
        renamed = json.loads(json.dumps(MANIFEST))
        renamed["parameters"]["MaxConcurrentWorkflows"]["variable"] = "max_workflows"
        values = _write(tmp_path, admin_email=None, manifest=renamed)
        assert values["max_workflows"] == 7
        assert "max_concurrent_workflows" not in values
        assert (tmp_path / tf.PARAMETERS_FILE).stat().st_mode & 0o077 == 0

    def test_a_parameter_the_new_module_lacks_is_dropped_with_a_warning(
        self, tmp_path, caplog
    ):
        _write(tmp_path, parameters={"MaxConcurrentWorkflows": "7"})
        smaller = json.loads(json.dumps(MANIFEST))
        del smaller["parameters"]["MaxConcurrentWorkflows"]
        with caplog.at_level("WARNING"):
            values = _write(tmp_path, admin_email=None, manifest=smaller)
        assert "max_concurrent_workflows" not in values
        assert "MaxConcurrentWorkflows" in caplog.text

    def test_a_required_name_missing_is_refused(self, tmp_path):
        with pytest.raises(IDPConfigurationError, match="AdminEmail"):
            _write(tmp_path, admin_email=None)
        assert not (tmp_path / "main.tf").exists()

    def test_unknown_parameter_writes_nothing(self, tmp_path):
        with pytest.raises(IDPConfigurationError, match="Bogus"):
            _write(tmp_path, parameters={"Bogus": "1"})
        assert not (tmp_path / "main.tf").exists()

    def test_no_role_writes_no_assume_role_block(self, tmp_path):
        _write(tmp_path)
        assert "assume_role" not in (tmp_path / "main.tf").read_text()

    def test_a_role_goes_into_both_provider_blocks(self, tmp_path):
        _write(tmp_path, assume_role_arn=ROLE)
        main = (tmp_path / "main.tf").read_text()
        for name in ("aws", "awscc"):
            assert (
                f'provider "{name}" {{\n  region = "us-west-2"\n  assume_role {{\n'
                f'    role_arn = "{ROLE}"\n  }}\n}}'
            ) in main
        assert main.count("assume_role {") == 2

    def test_a_malformed_role_is_refused_and_writes_nothing(self, tmp_path):
        with pytest.raises(IDPConfigurationError, match="not an IAM role ARN"):
            _write(tmp_path, assume_role_arn="arn:aws:iam::123456789012:user/x")
        assert not (tmp_path / "main.tf").exists()

    def test_set_assume_role_rewrites_only_the_provider_blocks(self, tmp_path):
        _write(tmp_path)
        before = (tmp_path / "main.tf").read_text()
        tf.set_assume_role(tmp_path / "main.tf", REGION, ROLE)
        after = (tmp_path / "main.tf").read_text()
        assert after.count(f'role_arn = "{ROLE}"') == 2
        strip = lambda t: re.sub(r"  assume_role \{\n.*?\n  \}\n", "", t, flags=re.S)  # noqa: E731
        assert strip(after) == before
        # Idempotent, and replaceable by another role.
        tf.set_assume_role(tmp_path / "main.tf", REGION, ROLE + "2")
        assert (tmp_path / "main.tf").read_text().count("assume_role {") == 2
        assert f'"{ROLE}2"' in (tmp_path / "main.tf").read_text()

    def test_set_assume_role_refuses_a_foreign_main_tf(self, tmp_path):
        (tmp_path / "main.tf").write_text('provider "aws" {}\n')
        with pytest.raises(IDPConfigurationError, match="not written by idp-cli"):
            tf.set_assume_role(tmp_path / "main.tf", REGION, ROLE)

    def test_another_deployments_directory_is_refused(self, tmp_path):
        _write(tmp_path)
        with pytest.raises(IDPConfigurationError, match="'tfidp'"):
            _write(tmp_path, stack_name="other")


# --------------------------------------------------------------------------------------
# Retained addresses and state
# --------------------------------------------------------------------------------------

STATE_LIST = [
    "module.idp.aws_s3_bucket.logging_bucket",
    "module.idp.aws_s3_bucket.input_bucket",
    'module.idp.module.patternstack.aws_dynamodb_table.tracking["a"]',
    'module.idp.module.patternstack.aws_dynamodb_table.tracking["b"]',
    "module.idp.data.aws_region.current",
]

STATE = {
    "values": {
        "root_module": {
            "child_modules": [
                {
                    "address": "module.idp",
                    "resources": [
                        {
                            "address": "module.idp.aws_s3_bucket.logging_bucket",
                            "mode": "managed",
                            "type": "aws_s3_bucket",
                            "values": {"bucket": "tfidp-logging"},
                        },
                        {
                            "address": "module.idp.aws_s3_bucket.input_bucket",
                            "mode": "managed",
                            "type": "aws_s3_bucket",
                            "values": {"bucket": "tfidp-input"},
                        },
                        {
                            "address": "module.idp.awscc_s3_bucket.output",
                            "mode": "managed",
                            "type": "awscc_s3_bucket",
                            "values": {"bucket_name": "tfidp-output"},
                        },
                    ],
                    "child_modules": [
                        {
                            "resources": [
                                {
                                    "address": 'module.idp.module.patternstack.aws_dynamodb_table.tracking["a"]',
                                    "mode": "managed",
                                    "type": "aws_dynamodb_table",
                                    "values": {"name": "tfidp-tracking-a"},
                                }
                            ]
                        }
                    ],
                }
            ]
        }
    }
}


class TestRetained:
    def test_prefixes_matches_every_instance_and_skips_absent(self):
        remove, absent = tf.retained_state_addresses(
            MANIFEST["retainedAddresses"], STATE_LIST
        )
        assert remove == [
            "module.idp.aws_s3_bucket.logging_bucket",
            'module.idp.module.patternstack.aws_dynamodb_table.tracking["a"]',
            'module.idp.module.patternstack.aws_dynamodb_table.tracking["b"]',
        ]
        assert absent == ["module.idp.aws_cloudwatch_log_group.never_created"]

    def test_unprefixed_state_entries_do_not_match(self):
        remove, absent = tf.retained_state_addresses(
            ["aws_s3_bucket.logging_bucket"], ["aws_s3_bucket.logging_bucket"]
        )
        assert remove == []
        assert absent == ["module.idp.aws_s3_bucket.logging_bucket"]

    def test_strip_instance_keys(self):
        assert tf.strip_instance_keys('module.a[0].x.y["k[1]"]') == "module.a.x.y"

    def test_buckets_to_empty_leave_out_retained_ones(self):
        buckets = tf.state_buckets(
            STATE, exclude=["module.idp.aws_s3_bucket.logging_bucket"]
        )
        assert [b["bucket_name"] for b in buckets] == ["tfidp-input", "tfidp-output"]

    def test_report_has_the_cloudformation_shape(self):
        remove, _ = tf.retained_state_addresses(
            MANIFEST["retainedAddresses"], STATE_LIST
        )
        report = tf.retained_report(STATE, remove)
        assert set(report) == {"dynamodb_tables", "log_groups", "s3_buckets", "other"}
        assert [r["physical_id"] for r in report["s3_buckets"]] == ["tfidp-logging"]
        assert [r["physical_id"] for r in report["dynamodb_tables"]] == [
            "tfidp-tracking-a"
        ]


# --------------------------------------------------------------------------------------
# The SDK operations, with the binary and AWS replaced
# --------------------------------------------------------------------------------------


class _Calls:
    def __init__(self, outputs=None):
        self.cmds = []
        self.outputs = outputs or {}

    def __call__(self, cmd, **kwargs):
        self.cmds.append(cmd[1:])
        key = " ".join(cmd[1:])
        return subprocess.CompletedProcess(cmd, 0, self.outputs.get(key, ""), "")


@pytest.fixture
def no_cfn_stack(monkeypatch):
    from idp_sdk.operations.stack import StackOperation

    monkeypatch.setattr(
        StackOperation, "_describe_stack", staticmethod(lambda d, n: None)
    )


class TestDeployOperation:
    def test_missing_binary_is_refused_before_anything_else(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(tf.shutil, "which", lambda b: None)
        client = IDPClient(stack_name="tfidp", region=REGION)
        with pytest.raises(IDPConfigurationError, match="'tofu' was not found"):
            client.stack.deploy_terraform(
                admin_email="a@example.com",
                module_url=MODULE_URL,
                terraform_dir=str(tmp_path),
                terraform_bin="tofu",
            )
        assert list(tmp_path.iterdir()) == []

    def test_resolves_the_pointer_writes_and_applies(
        self, monkeypatch, tmp_path, no_cfn_stack
    ):
        data = _module_zip()
        pointer = tf.build_pointer("1", MODULE_URL, hashlib.sha256(data).hexdigest())
        fetched = []

        def fetch(url, s3_factory=None):
            fetched.append(url)
            return json.dumps(pointer).encode() if url.endswith(".json") else data

        monkeypatch.setattr(tf, "fetch_url", fetch)
        monkeypatch.setattr(tf.shutil, "which", lambda b: f"/usr/bin/{b}")
        calls = _Calls()
        monkeypatch.setattr(tf.subprocess, "run", calls)
        client = IDPClient(stack_name="tfidp", region=REGION)
        result = client.stack.deploy_terraform(
            admin_email="a@example.com",
            parameters={"MaxConcurrentWorkflows": "3"},
            pointer_url="https://s3.us-west-2.amazonaws.com/b/p/terraform/ptr.json",
            terraform_dir=str(tmp_path),
        )
        assert fetched[-1] == MODULE_URL
        assert calls.cmds == [
            ["init", "-input=false", "-upgrade"],
            ["apply", "-auto-approve", "-input=false"],
        ]
        assert result.success and result.operation == "CREATE"
        assert (
            "max_concurrent_workflows = 3"
            in (tmp_path / "terraform.tfvars").read_text()
        )

    def test_a_pointer_checksum_mismatch_is_refused(
        self, monkeypatch, tmp_path, no_cfn_stack
    ):
        pointer = tf.build_pointer("1", MODULE_URL, "0" * 64)
        monkeypatch.setattr(
            tf,
            "fetch_url",
            lambda url, s3_factory=None: (
                json.dumps(pointer).encode() if url.endswith(".json") else _module_zip()
            ),
        )
        monkeypatch.setattr(tf.shutil, "which", lambda b: "/usr/bin/terraform")
        client = IDPClient(stack_name="tfidp", region=REGION)
        with pytest.raises(IDPConfigurationError, match="sha256"):
            client.stack.deploy_terraform(
                admin_email="a@example.com",
                pointer_url="https://x.test/ptr.json",
                terraform_dir=str(tmp_path),
            )

    def test_a_cloudformation_stack_of_that_name_is_refused(
        self, monkeypatch, tmp_path
    ):
        from idp_sdk.operations.stack import StackOperation

        monkeypatch.setattr(tf.shutil, "which", lambda b: "/usr/bin/terraform")
        monkeypatch.setattr(
            StackOperation,
            "_describe_stack",
            staticmethod(lambda d, n: {"Description": "IDP stack"}),
        )
        client = IDPClient(stack_name="tfidp", region=REGION)
        with pytest.raises(IDPConfigurationError, match="is a CloudFormation stack"):
            client.stack.deploy_terraform(
                admin_email="a@example.com",
                module_url=MODULE_URL,
                terraform_dir=str(tmp_path),
            )


class TestDeleteOperation:
    def _initialised(self, directory: Path) -> None:
        _write(directory)
        mod = directory / ".terraform" / "modules" / "idp"
        mod.mkdir(parents=True)
        (mod / tf.MANIFEST_NAME).write_text(json.dumps(MANIFEST))
        (directory / ".terraform" / "modules" / "modules.json").write_text(
            json.dumps({"Modules": [{"Key": "idp", "Dir": ".terraform/modules/idp"}]})
        )

    def test_empties_removes_retained_then_destroys(self, monkeypatch, tmp_path):
        from idp_sdk._core.stack import StackDeployer

        self._initialised(tmp_path)
        monkeypatch.setattr(tf.shutil, "which", lambda b: "/usr/bin/terraform")
        calls = _Calls(
            {"show -json": json.dumps(STATE), "state list": "\n".join(STATE_LIST)}
        )
        monkeypatch.setattr(tf.subprocess, "run", calls)
        emptied = []
        monkeypatch.setattr(
            StackDeployer,
            "_empty_buckets",
            lambda self, info: emptied.append(
                (len(calls.cmds), [b["bucket_name"] for b in info])
            ),
        )
        client = IDPClient(stack_name="tfidp", region=REGION)
        result = client.stack.delete_terraform(terraform_dir=str(tmp_path))
        assert calls.cmds == [
            ["init", "-input=false", "-upgrade"],
            ["show", "-json"],
            ["state", "list"],
            [
                "state",
                "rm",
                "module.idp.aws_s3_bucket.logging_bucket",
                'module.idp.module.patternstack.aws_dynamodb_table.tracking["a"]',
                'module.idp.module.patternstack.aws_dynamodb_table.tracking["b"]',
            ],
            ["destroy", "-auto-approve", "-input=false"],
        ]
        # Emptied after reading the state and before `state rm`.
        assert emptied == [(3, ["tfidp-input", "tfidp-output"])]
        assert result.success
        assert [r["physical_id"] for r in result.retained_resources["s3_buckets"]] == [
            "tfidp-logging"
        ]

    def test_a_role_is_written_into_main_tf_before_init(self, monkeypatch, tmp_path):
        from idp_sdk._core.stack import StackDeployer

        self._initialised(tmp_path)
        monkeypatch.setattr(tf.shutil, "which", lambda b: "/usr/bin/terraform")
        seen = []

        def _run(cmd, **kw):
            if cmd[1:2] == ["init"]:
                seen.append((tmp_path / "main.tf").read_text().count(ROLE))
            out = {"show": json.dumps(STATE), "state": "\n".join(STATE_LIST)}
            return subprocess.CompletedProcess(cmd, 0, out.get(cmd[1], ""), "")

        monkeypatch.setattr(tf.subprocess, "run", _run)
        monkeypatch.setattr(StackDeployer, "_empty_buckets", lambda self, info: None)
        client = IDPClient(stack_name="tfidp", region=REGION)
        client.stack.delete_terraform(terraform_dir=str(tmp_path), assume_role_arn=ROLE)
        assert seen == [2]

    def test_missing_directory_is_refused(self, tmp_path):
        client = IDPClient(stack_name="tfidp", region=REGION)
        with pytest.raises(IDPConfigurationError, match="no Terraform configuration"):
            client.stack.delete_terraform(terraform_dir=str(tmp_path / "none"))


class TestPublishOperation:
    def test_build_publishes_the_module_after_the_templates(
        self, monkeypatch, tmp_path
    ):
        from idp_sdk.operations import publish as pub

        calls = {}

        def fake_publish(self, **kw):
            calls.update(kw)
            return {
                "moduleUrl": "https://s3.us-west-2.amazonaws.com/b/m.zip",
                "sha256": "ab",
                "pointerUrl": "https://s3.us-west-2.amazonaws.com/b/p.json",
                "version": "1",
            }

        monkeypatch.setattr(pub.PublishOperation, "publish_terraform", fake_publish)
        publisher = MagicMock()
        monkeypatch.setattr(
            "idp_sdk._core.publish.IDPPublisher", lambda verbose=False: publisher
        )
        (tmp_path / "VERSION").write_text("1\n")
        client = IDPClient(region=REGION)
        result = client.publish.build(
            source_dir=str(tmp_path), bucket="b", prefix="p", terraform=True
        )
        assert result.success
        assert result.terraform_module_url.endswith(".zip")
        assert result.terraform_pointer_url.endswith(".json")
        assert calls == {
            "source_dir": str(tmp_path),
            "bucket": "b-us-west-2",
            "prefix": "p",
            "region": REGION,
            "version": "1",
            "public": False,
            "variant": None,
        }
        assert result.terraform_variant_modules == {}

    def _variant_build(self, monkeypatch, tmp_path, fake_publish, **flags):
        from idp_sdk.models.publish import TemplateTransformResult
        from idp_sdk.operations import publish as pub

        def transformed(self, source_template, output_path=None, **kw):
            return TemplateTransformResult(
                success=True, input_path=source_template, output_path=output_path
            )

        monkeypatch.setattr(pub.PublishOperation, "publish_terraform", fake_publish)
        monkeypatch.setattr(
            pub.PublishOperation, "transform_template_headless", transformed
        )
        monkeypatch.setattr(
            pub.PublishOperation, "transform_template_govcloud", transformed
        )
        monkeypatch.setattr(pub.boto3, "client", lambda *a, **k: MagicMock())
        monkeypatch.setattr(
            "idp_sdk._core.publish.IDPPublisher", lambda verbose=False: MagicMock()
        )
        (tmp_path / "VERSION").write_text("1\n")
        return IDPClient(region=REGION).publish.build(
            source_dir=str(tmp_path),
            bucket="b",
            prefix="p",
            terraform=True,
            no_validate=True,
            **flags,
        )

    def test_each_requested_variant_gets_its_own_module(self, monkeypatch, tmp_path):
        seen = []

        def fake_publish(self, **kw):
            seen.append(kw["variant"])
            stem = (
                f"idp-terraform-{kw['variant']}" if kw["variant"] else "idp-terraform"
            )
            return {
                "moduleUrl": f"https://s3.us-west-2.amazonaws.com/b/{stem}-1.zip",
                "sha256": "ab",
                "pointerUrl": f"https://s3.us-west-2.amazonaws.com/b/{stem}-latest.json",
                "version": "1",
            }

        result = self._variant_build(
            monkeypatch, tmp_path, fake_publish, headless=True, govcloud=True
        )
        assert result.success, result.error
        assert seen == [None, "headless", "govcloud"]
        assert result.terraform_module_url.endswith("/idp-terraform-1.zip")
        assert set(result.terraform_variant_modules) == {"headless", "govcloud"}
        assert result.terraform_variant_modules["govcloud"]["pointerUrl"].endswith(
            "idp-terraform-govcloud-latest.json"
        )

    def test_without_terraform_variant_flags_only_the_standard_module(
        self, monkeypatch, tmp_path
    ):
        seen = []

        def fake_publish(self, **kw):
            seen.append(kw["variant"])
            return {"moduleUrl": "u", "sha256": "s", "pointerUrl": "p"}

        result = self._variant_build(monkeypatch, tmp_path, fake_publish, headless=True)
        assert result.success
        assert seen == [None, "headless"]

    def test_a_variant_failure_fails_the_publish_naming_the_variant(
        self, monkeypatch, tmp_path
    ):
        def fake_publish(self, **kw):
            if kw["variant"] == "govcloud":
                raise IDPStackError("Terraform translation failed: CloudFront")
            return {"moduleUrl": "u", "sha256": "s", "pointerUrl": "p"}

        result = self._variant_build(
            monkeypatch, tmp_path, fake_publish, headless=True, govcloud=True
        )
        assert not result.success
        assert "Terraform govcloud module publish failed" in (result.error or "")

    def test_a_variant_template_this_run_did_not_publish_is_not_read(
        self, monkeypatch, tmp_path
    ):
        from idp_sdk.models.publish import TemplateTransformResult
        from idp_sdk.operations import publish as pub

        seen = []

        def fake_publish(self, **kw):
            seen.append(kw["variant"])
            return {"moduleUrl": "u", "sha256": "s", "pointerUrl": "p"}

        def failed(self, source_template, output_path=None, **kw):
            return TemplateTransformResult(
                success=False, input_path=source_template, error="x"
            )

        monkeypatch.setattr(pub.PublishOperation, "transform_template_headless", failed)
        monkeypatch.setattr(pub.PublishOperation, "publish_terraform", fake_publish)
        monkeypatch.setattr(pub.boto3, "client", lambda *a, **k: MagicMock())
        monkeypatch.setattr(
            "idp_sdk._core.publish.IDPPublisher", lambda verbose=False: MagicMock()
        )
        (tmp_path / "VERSION").write_text("1\n")
        result = IDPClient(region=REGION).publish.build(
            source_dir=str(tmp_path),
            bucket="b",
            prefix="p",
            terraform=True,
            headless=True,
            no_validate=True,
        )
        assert not result.success
        assert "headless" in (result.error or "")
        assert seen == [None]

    def test_publish_terraform_reads_the_variant_template(self, monkeypatch, tmp_path):
        from idp_sdk.operations import publish as pub

        s3 = MagicMock()
        monkeypatch.setattr(pub.boto3, "client", lambda *a, **k: s3)
        translated = []
        monkeypatch.setattr(
            tf,
            "translate_published_template",
            lambda src, tpl, work, ver: translated.append((tpl, work)) or Path(work),
        )
        uploaded = {}
        monkeypatch.setattr(
            tf, "upload_module", lambda s3c, **kw: uploaded.update(kw) or {"ok": 1}
        )
        IDPClient(region=REGION).publish.publish_terraform(
            str(tmp_path), "b-us-west-2", "p", REGION, "1", variant="govcloud"
        )
        args = s3.download_file.call_args.args
        assert args[:2] == ("b-us-west-2", "p/idp-govcloud.yaml")
        work = tmp_path / ".aws-sam" / "terraform" / "govcloud"
        assert translated == [(str(work / "idp-govcloud.yaml"), str(work))]
        assert uploaded["variant"] == "govcloud"

    def test_publish_terraform_refuses_an_unknown_variant(self, tmp_path):
        with pytest.raises(IDPConfigurationError, match="unknown Terraform"):
            IDPClient(region=REGION).publish.publish_terraform(
                str(tmp_path), "b", "p", REGION, "1", variant="minimal"
            )

    def test_a_translation_failure_fails_the_publish(self, monkeypatch, tmp_path):
        from idp_sdk.operations import publish as pub

        def boom(self, **kw):
            raise IDPStackError("translation failed: x")

        monkeypatch.setattr(pub.PublishOperation, "publish_terraform", boom)
        monkeypatch.setattr(
            "idp_sdk._core.publish.IDPPublisher", lambda verbose=False: MagicMock()
        )
        (tmp_path / "VERSION").write_text("1\n")
        result = IDPClient(region=REGION).publish.build(
            source_dir=str(tmp_path), bucket="b", prefix="p", terraform=True
        )
        assert not result.success
        assert "Terraform module publish failed" in (result.error or "")


class TestFetch:
    def test_an_s3_url_is_read_through_the_s3_api_in_its_region(self):
        s3 = MagicMock()
        s3.get_object.return_value = {"Body": io.BytesIO(b"zip")}
        regions = []

        def factory(region):
            regions.append(region)
            return s3

        data = tf.fetch_url(
            "https://s3.eu-central-1.amazonaws.com/b/p/m.zip", s3_factory=factory
        )
        assert data == b"zip"
        assert regions == ["eu-central-1"]
        s3.get_object.assert_called_once_with(Bucket="b", Key="p/m.zip")

    def test_a_non_https_url_is_refused(self):
        with pytest.raises(IDPConfigurationError, match="HTTPS"):
            tf.fetch_url("http://example.com/m.zip")


def test_a_retained_buckets_settings_are_reported_with_the_bucket_not_as_other():
    state = {
        "values": {
            "root_module": {
                "child_modules": [
                    {
                        "address": "module.idp",
                        "resources": [
                            {
                                "address": "module.idp.aws_s3_bucket.b",
                                "type": "aws_s3_bucket",
                                "values": {"bucket": "stk-b-1"},
                            },
                            {
                                "address": "module.idp.aws_s3_bucket_versioning.b_versioning",
                                "type": "aws_s3_bucket_versioning",
                                "values": {"bucket": "stk-b-1"},
                            },
                        ],
                    }
                ]
            }
        }
    }
    report = tf.retained_report(
        state,
        [
            "module.idp.aws_s3_bucket.b",
            "module.idp.aws_s3_bucket_versioning.b_versioning",
        ],
    )
    assert [r["physical_id"] for r in report["s3_buckets"]] == ["stk-b-1"]
    assert report["other"] == []
