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


#: Flat sibling modules a lambda's `index.py` imports by bare name.
#:
#: Each lambda directory carries its **own** copy of these, because the functions are
#: deployed without a layer. They therefore collide in `sys.modules` exactly the way
#: `index` does, and evicting `index` alone is not enough: the first lambda a worker
#: process loads leaves its copy installed under the bare name, and every lambda loaded
#: after it in that process runs *that* copy instead of its own.
#:
#: The visible symptom was a coverage figure nothing about the code could predict: at most
#: one copy of `log_sanitizer.py` per worker process, so which of the eight were measured
#: followed how the 234 tests happened to be distributed. Measured across serial, `-n 2`,
#: `-n 4`, `-n 8` and `-n 16` with repeats: between **one and three** of the eight, and the
#: tree total moving over 95.93 / 95.64 / 95.36.
#:
#: ⚠️ Read that as **non-deterministic, not as a worker-count threshold.** xdist's default
#: `--dist load` hands the next test to whichever worker is free, so the same width gives
#: different answers on different runs — `-n 4` reached `get_feature_launch_url`'s copy on
#: one run of three. A ratchet input of that shape is unusable, and the two CI
#: configurations would not even have agreed with each other, since the worker share
#: follows the runner's CPU count. After this eviction: eight of eight at every width, and
#: the tree total 93.71 on all of them.
#:
#: Derived from the tree rather than listed: any bare-name `.py` sitting beside an
#: `index.py` is one of these, so a new shared helper is covered without being named here.
_SHARED_FLAT_MODULES = frozenset(
    path.stem
    for path in _LAMBDAS_DIR.glob("*/*.py")
    if path.name != "index.py" and not path.name.startswith("_")
)


def _load_module(module_dir: Path, module_alias: str):
    """Load `<module_dir>/index.py` as `module_alias`, forcing a fresh import.

    Each lambda is written as a flat `index.py`, so we add its directory to
    `sys.path` just long enough to import, then remove it again. Using
    `module_alias` keeps the modules from colliding on `index`.

    The sibling helpers each lambda carries its own copy of are evicted too — see
    :data:`_SHARED_FLAT_MODULES` for why, and for what it cost not to.
    """
    sys.path.insert(0, str(module_dir))
    try:
        if module_alias in sys.modules:
            del sys.modules[module_alias]
        for name in ("index", *_SHARED_FLAT_MODULES):
            sys.modules.pop(name, None)
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
