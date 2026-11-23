# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`deploy --terraform --terraform-state s3` keeps the state in an S3 backend.

The bucket is created once per account, Region and deployment with a public
access block, versioning, SSE-S3 default encryption and a policy denying
non-TLS requests, and is reused (never recreated or emptied) afterwards; the
DynamoDB lock table likewise. ``local``, the default, writes exactly the files
it wrote before the option existed. Offline: S3, STS and DynamoDB are moto, and
the binary is replaced at ``subprocess.run`` as in the other deploy tests.
"""

from __future__ import annotations

import io
import json
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from idp_sdk import IDPClient
from idp_sdk._core import terraform_deploy as tf
from idp_sdk.exceptions import IDPConfigurationError

pytestmark = pytest.mark.unit

REGION = "us-west-2"
ACCOUNT = "123456789012"  # moto's account
STACK = "tfidp"
BUCKET = f"idp-tf-{STACK}-{ACCOUNT}-{REGION}"
MODULE_URL = "https://s3.us-west-2.amazonaws.com/b/p/2/terraform/m.zip"
MODULE = {
    "formatVersion": 1,
    "version": "0.6.16",
    "parameters": {
        "AdminEmail": {
            "variable": "admin_email",
            "type": "String",
            "required": True,
            "sensitive": False,
        }
    },
}

#: S3 operations that remove data or a bucket, or replace a protective setting.
DESTRUCTIVE_S3 = {
    "DeleteObject",
    "DeleteObjects",
    "DeleteBucket",
    "DeleteBucketPolicy",
    "DeleteBucketEncryption",
    "DeletePublicAccessBlock",
    "PutBucketLifecycleConfiguration",
}


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")  # nosec B105 - moto
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    # moto evaluates a bucket policy without its Condition block, so the TLS
    # deny would refuse every request; real S3 sees TLS and allows them.
    from moto.iam.access_control import PermissionResult
    from moto.s3.models import FakeBucket

    monkeypatch.setattr(
        FakeBucket, "get_permission", lambda self, a, r: PermissionResult.NEUTRAL
    )
    with mock_aws():
        yield


def _recording(service):
    client = boto3.client(service, region_name=REGION)
    calls = []
    client.meta.events.register(
        f"before-call.{service}.*",
        lambda model, **kw: calls.append(model.name),
    )
    return client, calls


def _ensure():
    s3, s3_calls = _recording("s3")
    ddb, ddb_calls = _recording("dynamodb")
    backend = tf.ensure_state_backend(
        REGION, STACK, s3=s3, dynamodb=ddb, sts=boto3.client("sts", REGION)
    )
    return backend, s3_calls, ddb_calls


class TestNames:
    def test_the_bucket_is_named_from_account_region_and_stack(self):
        assert (
            tf.state_bucket_name("111122223333", "eu-central-1", "My-IDP")
            == "idp-tf-my-idp-111122223333-eu-central-1"
        )

    def test_the_key_keeps_the_stacks_case(self):
        assert tf.state_key("My-IDP") == "My-IDP/terraform.tfstate"

    def test_a_stack_name_ending_in_a_hyphen_still_makes_a_valid_name(self):
        name = tf.state_bucket_name("111122223333", "us-east-1", "idp-")
        assert name == "idp-tf-idp--111122223333-us-east-1"

    def test_the_longest_names_fit(self):
        # A label stack name is at most 25 characters.
        name = tf.state_bucket_name("111122223333", "ap-southeast-2", "a" * 25)
        assert len(name) <= 63

    def test_an_invalid_name_is_refused(self):
        with pytest.raises(IDPConfigurationError, match="not a valid S3 bucket"):
            tf.state_bucket_name("111122223333", "us-east-1", "a" * 40)

    def test_the_choice_is_checked(self):
        assert tf.check_terraform_state(None) == "local"
        assert tf.check_terraform_state("S3") == "s3"
        with pytest.raises(IDPConfigurationError, match="must be one of"):
            tf.check_terraform_state("gcs")


class TestEnsureStateBackend:
    def test_a_new_bucket_gets_every_protective_setting(self, aws):
        backend, s3_calls, _ = _ensure()
        assert backend["created_bucket"] is True
        assert backend["state_exists"] is False
        assert "CreateBucket" in s3_calls
        s3 = boto3.client("s3", region_name=REGION)
        assert s3.get_public_access_block(Bucket=BUCKET)[
            "PublicAccessBlockConfiguration"
        ] == {
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        }
        assert s3.get_bucket_versioning(Bucket=BUCKET)["Status"] == "Enabled"
        rule = s3.get_bucket_encryption(Bucket=BUCKET)[
            "ServerSideEncryptionConfiguration"
        ]["Rules"][0]
        assert rule["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
        policy = json.loads(s3.get_bucket_policy(Bucket=BUCKET)["Policy"])
        (statement,) = policy["Statement"]
        assert statement["Effect"] == "Deny"
        assert statement["Principal"] == "*"
        assert statement["Action"] == "s3:*"
        assert statement["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
        assert set(statement["Resource"]) == {
            f"arn:aws:s3:::{BUCKET}",
            f"arn:aws:s3:::{BUCKET}/*",
        }
        assert s3.get_bucket_location(Bucket=BUCKET)["LocationConstraint"] == REGION

    def test_the_lock_table_is_created_with_the_key_the_backend_uses(self, aws):
        backend, _, ddb_calls = _ensure()
        assert backend["created_table"] is True
        table = boto3.client("dynamodb", region_name=REGION).describe_table(
            TableName=f"idp-tf-lock-{STACK}"
        )["Table"]
        assert table["KeySchema"] == [{"AttributeName": "LockID", "KeyType": "HASH"}]
        assert table["BillingModeSummary"]["BillingMode"] == "PAY_PER_REQUEST"

    def test_a_second_deploy_reuses_the_bucket_and_writes_nothing(self, aws):
        _ensure()
        boto3.client("s3", region_name=REGION).put_object(
            Bucket=BUCKET, Key=f"{STACK}/terraform.tfstate", Body=b"{}"
        )
        backend, s3_calls, ddb_calls = _ensure()
        assert backend["created_bucket"] is False
        assert backend["created_table"] is False
        assert backend["state_exists"] is True
        # Reads only: no create, no setting replaced, nothing deleted.
        assert not [c for c in s3_calls if c.startswith(("Create", "Put", "Delete"))]
        assert not DESTRUCTIVE_S3 & set(s3_calls)
        assert "CreateTable" not in ddb_calls
        assert (
            boto3.client("s3", region_name=REGION)
            .get_object(Bucket=BUCKET, Key=f"{STACK}/terraform.tfstate")["Body"]
            .read()
            == b"{}"
        )

    def test_a_reused_bucket_without_the_state_object_is_a_create(self, aws):
        _ensure()
        backend, _, _ = _ensure()
        assert backend["created_bucket"] is False
        assert backend["state_exists"] is False

    @pytest.mark.parametrize(
        "getter",
        [
            "get_public_access_block",
            "get_bucket_versioning",
            "get_bucket_encryption",
            "get_bucket_policy",
            "head_object",
        ],
    )
    def test_an_unreadable_setting_fails_instead_of_being_replaced(self, getter):
        s3 = MagicMock()
        s3.get_bucket_versioning.return_value = {"Status": "Enabled"}
        s3.get_bucket_policy.return_value = {
            "Policy": json.dumps(tf.state_bucket_policy(BUCKET, "aws"))
        }
        getattr(s3, getter).side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "x"}}, getter
        )
        sts = MagicMock()
        sts.get_caller_identity.return_value = {
            "Account": ACCOUNT,
            "Arn": f"arn:aws:iam::{ACCOUNT}:user/x",
        }
        with pytest.raises(ClientError):
            tf.ensure_state_backend(REGION, STACK, s3=s3, sts=sts, dynamodb=MagicMock())
        assert not [c for c in s3.method_calls if c[0].startswith(("put_", "create_"))]

    def test_an_unreadable_lock_table_fails_instead_of_being_created(self, aws):
        ddb = MagicMock()
        ddb.describe_table.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "x"}},
            "DescribeTable",
        )
        with pytest.raises(ClientError):
            tf.ensure_state_backend(
                REGION,
                STACK,
                s3=boto3.client("s3", region_name=REGION),
                sts=boto3.client("sts", region_name=REGION),
                dynamodb=ddb,
            )
        ddb.create_table.assert_not_called()

    def test_a_create_race_lost_to_the_same_account_reuses_the_bucket(self, aws):
        s3, s3_calls = _recording("s3")
        s3.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        real_head = s3.head_bucket
        heads = []

        def head_bucket(**kw):
            heads.append(kw)
            if len(heads) == 1:  # the other deploy has not created it yet
                raise ClientError({"Error": {"Code": "404"}}, "HeadBucket")
            return real_head(**kw)

        s3.head_bucket = head_bucket
        backend = tf.ensure_state_backend(
            REGION,
            STACK,
            s3=s3,
            dynamodb=boto3.client("dynamodb", region_name=REGION),
            sts=boto3.client("sts", region_name=REGION),
        )
        assert "CreateBucket" in s3_calls  # tried, got BucketAlreadyOwnedByYou
        assert backend["created_bucket"] is False
        assert "HeadObject" in s3_calls
        assert s3.get_bucket_versioning(Bucket=BUCKET)["Status"] == "Enabled"

    def test_every_call_on_a_reused_bucket_names_the_owner(self):
        s3 = MagicMock()
        s3.get_bucket_versioning.return_value = {"Status": "Enabled"}
        s3.get_public_access_block.return_value = {
            "PublicAccessBlockConfiguration": dict.fromkeys(
                tf._PUBLIC_ACCESS_BLOCK, True
            )
        }
        s3.get_bucket_policy.return_value = {
            "Policy": json.dumps(tf.state_bucket_policy(BUCKET, "aws"))
        }
        sts = MagicMock()
        sts.get_caller_identity.return_value = {
            "Account": ACCOUNT,
            "Arn": f"arn:aws:iam::{ACCOUNT}:user/x",
        }
        tf.ensure_state_backend(REGION, STACK, s3=s3, sts=sts, dynamodb=MagicMock())
        calls = [c for c in s3.method_calls if c[0] != "get_waiter"]
        assert calls
        for name, _, kwargs in calls:
            assert kwargs.get("ExpectedBucketOwner") == ACCOUNT, name

    def test_a_weakened_public_access_block_is_warned_about(self, aws, caplog):
        _ensure()
        boto3.client("s3", region_name=REGION).put_public_access_block(
            Bucket=BUCKET,
            PublicAccessBlockConfiguration={
                **tf._PUBLIC_ACCESS_BLOCK,
                "RestrictPublicBuckets": False,
            },
        )
        _, s3_calls, _ = _ensure()
        assert "PutPublicAccessBlock" not in s3_calls
        assert "has RestrictPublicBuckets off" in caplog.text

    def test_an_interrupted_creation_is_finished_on_reuse(self, aws):
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        backend, s3_calls, _ = _ensure()
        assert backend["created_bucket"] is False
        assert "CreateBucket" not in s3_calls
        assert {
            "PutPublicAccessBlock",
            "PutBucketVersioning",
            "PutBucketPolicy",
        } <= set(s3_calls)
        s3 = boto3.client("s3", region_name=REGION)
        assert s3.get_bucket_versioning(Bucket=BUCKET)["Status"] == "Enabled"

    def test_a_changed_setting_is_left_alone_with_a_warning(self, aws, caplog):
        _ensure()
        s3 = boto3.client("s3", region_name=REGION)
        s3.put_bucket_versioning(
            Bucket=BUCKET, VersioningConfiguration={"Status": "Suspended"}
        )
        s3.put_bucket_policy(
            Bucket=BUCKET,
            Policy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT}:root"},
                            "Action": "s3:GetObject",
                            "Resource": f"arn:aws:s3:::{BUCKET}/*",
                        }
                    ],
                }
            ),
        )
        _, s3_calls, _ = _ensure()
        assert "PutBucketVersioning" not in s3_calls
        assert "PutBucketPolicy" not in s3_calls
        assert "Versioning on state bucket" in caplog.text
        assert "without a deny on non-TLS" in caplog.text

    def test_a_us_east_1_bucket_has_no_location_constraint(self, aws):
        s3 = boto3.client("s3", region_name="us-east-1")
        tf.ensure_state_backend(
            "us-east-1",
            STACK,
            s3=s3,
            dynamodb=boto3.client("dynamodb", region_name="us-east-1"),
            sts=boto3.client("sts", region_name="us-east-1"),
        )
        bucket = f"idp-tf-{STACK}-{ACCOUNT}-us-east-1"
        assert s3.get_bucket_location(Bucket=bucket)["LocationConstraint"] is None

    @pytest.mark.parametrize("code", ["403", "AccessDenied"])
    def test_a_bucket_of_another_account_is_refused_untouched(self, code):
        s3 = MagicMock()
        s3.head_bucket.side_effect = ClientError(
            {"Error": {"Code": code, "Message": "Forbidden"}}, "HeadBucket"
        )
        sts = MagicMock()
        sts.get_caller_identity.return_value = {
            "Account": ACCOUNT,
            "Arn": f"arn:aws:iam::{ACCOUNT}:user/x",
        }
        with pytest.raises(IDPConfigurationError, match="another account"):
            tf.ensure_state_backend(REGION, STACK, s3=s3, sts=sts, dynamodb=MagicMock())
        assert s3.head_bucket.call_args.kwargs["ExpectedBucketOwner"] == ACCOUNT
        s3.create_bucket.assert_not_called()
        assert not [c for c in s3.method_calls if c[0].startswith("put_")]

    def test_a_name_taken_elsewhere_is_refused(self):
        s3 = MagicMock()
        s3.head_bucket.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadBucket"
        )
        s3.create_bucket.side_effect = ClientError(
            {"Error": {"Code": "BucketAlreadyExists", "Message": "x"}}, "CreateBucket"
        )
        sts = MagicMock()
        sts.get_caller_identity.return_value = {
            "Account": ACCOUNT,
            "Arn": f"arn:aws:iam::{ACCOUNT}:user/x",
        }
        with pytest.raises(IDPConfigurationError, match="taken by another account"):
            tf.ensure_state_backend(REGION, STACK, s3=s3, sts=sts, dynamodb=MagicMock())
        assert not [c for c in s3.method_calls if c[0].startswith("put_")]

    def test_the_policy_names_the_partition(self):
        policy = tf.state_bucket_policy("b", "aws-us-gov")
        assert policy["Statement"][0]["Resource"] == [
            "arn:aws-us-gov:s3:::b",
            "arn:aws-us-gov:s3:::b/*",
        ]


class TestBackendTf:
    def test_the_backend_block(self):
        text = tf.render_backend_tf(
            {
                "bucket": BUCKET,
                "key": "tfidp/terraform.tfstate",
                "region": REGION,
                "dynamodb_table": "idp-tf-lock-tfidp",
            }
        )
        assert text.startswith(tf.ROOT_MARKER + "\n")
        body = text.split("terraform {", 1)[1]
        assert body == (
            "\n"
            '  backend "s3" {\n'
            f'    bucket         = "{BUCKET}"\n'
            '    key            = "tfidp/terraform.tfstate"\n'
            f'    region         = "{REGION}"\n'
            "    encrypt        = true\n"
            '    dynamodb_table = "idp-tf-lock-tfidp"\n'
            "  }\n"
            "}\n"
        )

    def test_a_local_state_is_not_moved_into_s3(self, tmp_path):
        (tmp_path / "terraform.tfstate").write_text("{}")
        with pytest.raises(IDPConfigurationError, match="not supported yet"):
            tf.check_backend_switch(tmp_path, "s3")

    def test_an_s3_directory_is_not_moved_back_to_local(self, tmp_path):
        (tmp_path / "backend.tf").write_text(tf.ROOT_MARKER + "\n")
        with pytest.raises(IDPConfigurationError, match="--terraform-state s3"):
            tf.check_backend_switch(tmp_path, "local")

    def test_a_users_backend_tf_is_not_overwritten(self, tmp_path):
        (tmp_path / "backend.tf").write_text('terraform {\n  backend "gcs" {}\n}\n')
        with pytest.raises(IDPConfigurationError, match="not written by idp-cli"):
            tf.check_backend_switch(tmp_path, "s3")
        # With local state, the user's own backend file is left as today.
        tf.check_backend_switch(tmp_path, "local")


# --------------------------------------------------------------------------------------
# Through `client.stack.deploy_terraform`, with the binary replaced
# --------------------------------------------------------------------------------------


def _zip(manifest) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(tf.MANIFEST_NAME, json.dumps(manifest))
    return buf.getvalue()


def _install(directory: Path) -> None:
    mod = directory / ".terraform" / "modules" / "idp"
    mod.mkdir(parents=True, exist_ok=True)
    (mod / "variables.tf").write_text("")
    (directory / ".terraform" / "modules" / "modules.json").write_text(
        json.dumps({"Modules": [{"Key": "idp", "Dir": ".terraform/modules/idp"}]})
    )


def _deploy(monkeypatch, directory, **kw):
    from idp_sdk.operations.stack import StackOperation

    monkeypatch.setattr(
        StackOperation, "_describe_stack", staticmethod(lambda d, n: None)
    )
    monkeypatch.setattr(tf, "fetch_url", lambda url, s3_factory=None: _zip(MODULE))
    monkeypatch.setattr(tf.shutil, "which", lambda b: "/usr/bin/terraform")
    commands = []

    def run(cmd, **_):
        commands.append(cmd[1:])
        if cmd[1] == "init":
            _install(directory)
        out = json.dumps({"resource_changes": []}) if cmd[1] == "show" else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(tf.subprocess, "run", run)
    result = IDPClient(stack_name=STACK, region=REGION).stack.deploy_terraform(
        admin_email="a@example.com",
        module_url=MODULE_URL,
        terraform_dir=str(directory),
        **kw,
    )
    return result, commands


def _root_files(directory: Path):
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir()) if p.is_file()}


class TestDeployOperation:
    def test_local_writes_exactly_what_the_default_writes(
        self, monkeypatch, tmp_path, aws
    ):
        default, explicit = tmp_path / "default", tmp_path / "explicit"
        _deploy(monkeypatch, default)
        _deploy(monkeypatch, explicit, terraform_state="local")
        assert _root_files(default) == _root_files(explicit)
        assert "backend.tf" not in _root_files(default)
        # No AWS call made a bucket or a table for a local deploy.
        assert boto3.client("s3", region_name=REGION).list_buckets()["Buckets"] == []
        assert (
            boto3.client("dynamodb", region_name=REGION).list_tables()["TableNames"]
            == []
        )
        assert "backend" not in (default / "main.tf").read_text().split("\n", 3)[3]

    def test_s3_writes_the_backend_and_creates_then_reuses(
        self, monkeypatch, tmp_path, aws
    ):
        result, commands = _deploy(monkeypatch, tmp_path, terraform_state="s3")
        assert result.operation == "CREATE"
        text = (tmp_path / "backend.tf").read_text()
        assert f'bucket         = "{BUCKET}"' in text
        assert "encrypt        = true" in text
        assert f'dynamodb_table = "idp-tf-lock-{STACK}"' in text
        assert commands[0][0] == "init"
        # main.tf and terraform.tfvars are what a local deploy writes.
        local = tmp_path.parent / "local"
        _deploy(monkeypatch, local)
        assert (tmp_path / "main.tf").read_bytes() == (local / "main.tf").read_bytes()

        boto3.client("s3", region_name=REGION).put_object(
            Bucket=BUCKET, Key=f"{STACK}/terraform.tfstate", Body=b"{}"
        )
        result, _ = _deploy(monkeypatch, tmp_path, terraform_state="s3")
        assert result.operation == "UPDATE"

    def test_a_refused_switch_creates_nothing(self, monkeypatch, tmp_path, aws):
        (tmp_path / "terraform.tfstate").write_text("{}")
        with pytest.raises(IDPConfigurationError, match="not supported yet"):
            _deploy(monkeypatch, tmp_path, terraform_state="s3")
        assert boto3.client("s3", region_name=REGION).list_buckets()["Buckets"] == []
        assert not (tmp_path / "backend.tf").exists()

    def test_a_refused_bucket_leaves_the_directory_unwritten(
        self, monkeypatch, tmp_path, aws
    ):
        def refuse(region, name):
            raise IDPConfigurationError("taken")

        monkeypatch.setattr(tf, "ensure_state_backend", refuse)
        with pytest.raises(IDPConfigurationError, match="taken"):
            _deploy(monkeypatch, tmp_path, terraform_state="s3")
        assert not tmp_path.exists() or not any(tmp_path.iterdir())

    def test_delete_keeps_the_state_bucket(self, monkeypatch, tmp_path, aws):
        _deploy(monkeypatch, tmp_path, terraform_state="s3")
        s3 = boto3.client("s3", region_name=REGION)
        s3.put_object(Bucket=BUCKET, Key=f"{STACK}/terraform.tfstate", Body=b"{}")
        (tmp_path / tf.MANIFEST_NAME).write_text(json.dumps(MODULE))
        mod = tmp_path / ".terraform" / "modules" / "idp"
        (mod / tf.MANIFEST_NAME).write_text(json.dumps(MODULE))

        def run(cmd, **_):
            out = "{}" if cmd[1] == "show" else ""
            return subprocess.CompletedProcess(cmd, 0, out, "")

        monkeypatch.setattr(tf.subprocess, "run", run)
        IDPClient(stack_name=STACK, region=REGION).stack.delete_terraform(
            terraform_dir=str(tmp_path)
        )
        assert s3.get_object(Bucket=BUCKET, Key=f"{STACK}/terraform.tfstate")
        assert (tmp_path / "backend.tf").exists()
