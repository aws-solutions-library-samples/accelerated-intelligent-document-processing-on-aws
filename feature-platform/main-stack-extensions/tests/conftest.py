"""Shared fixtures for feature-platform Lambda unit tests.

All 4 Lambdas are pure `index.handler(event, context)` functions that read their
config from env vars. Tests mock AWS with `moto` and re-import each module fresh
so env-var-at-import-time globals are captured correctly.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

_LAMBDAS_DIR = Path(__file__).resolve().parent.parent / "lambdas"

# These Lambdas import idp_common as an EXTERNAL dependency, and unlike
# lib/idp_common_pkg's own suite nothing here puts the library's checkout on sys.path,
# so the editable-install pointer is the only thing deciding which revision gets tested.
# That went wrong: two failures in test_apply_feature_config_preset.py were read as a
# code defect when the imported tree simply predated
# idp_common/config/hook_reachability.py. The import is wrapped in a degradation path, so
# the only signal was a log line saying the reachability check had been skipped.
# Skipped only when the helper is genuinely absent (an export with no scripts/ tree).
# See the note in lib/idp_common_pkg/tests/conftest.py: a blanket `except ImportError`
# here would also swallow a renamed symbol and silently disable the guard.
_GATE_DIR = Path(__file__).resolve().parents[3] / "scripts" / "tests"
if (_GATE_DIR / "first_party_provenance.py").is_file():
    sys.path.insert(0, str(_GATE_DIR))
    try:
        from first_party_provenance import assert_resolves_in

        assert_resolves_in("idp_common", __file__)
    finally:
        sys.path.pop(0)


def _load_module(module_dir: Path, module_alias: str):
    """Load `<module_dir>/index.py` as `module_alias`, forcing a fresh import.

    Each lambda is written as a flat `index.py`, so we add its directory to
    `sys.path` just long enough to import, then remove it again. Using
    `module_alias` keeps the 4 modules from colliding on `index`.
    """
    sys.path.insert(0, str(module_dir))
    try:
        if module_alias in sys.modules:
            del sys.modules[module_alias]
        if "index" in sys.modules:
            del sys.modules["index"]
        mod = importlib.import_module("index")
        sys.modules[module_alias] = mod
        return mod
    finally:
        if str(module_dir) in sys.path:
            sys.path.remove(str(module_dir))


@pytest.fixture
def aws_credentials(monkeypatch):
    """moto needs fake AWS creds in env to avoid accidentally hitting real AWS."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def installed_features_table(aws_credentials):
    """A mocked DynamoDB 'InstalledFeatures' table. Yields the table name."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table_name = "TestInstalledFeatures"
        ddb.create_table(
            TableName=table_name,
            KeySchema=[{"AttributeName": "featureId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "featureId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        ).wait_until_exists()
        yield table_name


@pytest.fixture
def feature_bucket(aws_credentials):
    """A mocked S3 bucket. Yields the bucket name."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        bucket = "test-feature-bucket"
        s3.create_bucket(Bucket=bucket)
        yield bucket


@pytest.fixture
def configuration_bucket(aws_credentials):
    """A mocked S3 ConfigurationBucket (holds catalog.json). Yields the name."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        bucket = "test-configuration-bucket"
        s3.create_bucket(Bucket=bucket)
        yield bucket


@pytest.fixture
def mock_stack(aws_credentials):
    """Combined DDB + S3 mock for tests that need both."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table_name = "TestInstalledFeatures"
        ddb.create_table(
            TableName=table_name,
            KeySchema=[{"AttributeName": "featureId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "featureId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        ).wait_until_exists()
        s3 = boto3.client("s3", region_name="us-east-1")
        bucket = "test-feature-bucket"
        s3.create_bucket(Bucket=bucket)
        yield {"table_name": table_name, "bucket": bucket}


@pytest.fixture
def load_lambda():
    """Factory: load a lambda module by name, after env vars are set."""

    def _load(lambda_name: str):
        return _load_module(
            _LAMBDAS_DIR / lambda_name, f"feature_platform_{lambda_name}"
        )

    return _load
