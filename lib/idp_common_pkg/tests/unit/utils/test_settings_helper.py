# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The SSM client behind ``get_settings`` is built on first use, not at import.

``idp_common.utils`` is imported by ``idp_common.s3`` and thence by most of this
repository's Lambda handlers, so a module-scope ``boto3.client("ssm")`` here made
importing the shared library at all require a resolvable AWS region. The Lambda
runtime always supplies one, so production was never affected — but a unit test
run on a machine with no region is not, and botocore raised ``NoRegionError``
while pytest was still collecting, which is how a whole suite could fail on a CI
runner while passing everywhere it was written (#988).

The import-safety assertion runs in a subprocess with the region deliberately
removed, because this process already has one and cannot answer the question.
"""

import importlib
import subprocess
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

from idp_common.utils import settings_helper

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def reset_module_state():
    """Give every test a cold module, and leave one behind."""
    settings_helper._ssm_client = None
    settings_helper.clear_cache()
    yield
    settings_helper._ssm_client = None
    settings_helper.clear_cache()


def _run_without_a_region(statement: str) -> subprocess.CompletedProcess:
    """Run one statement in a subprocess that has no way to resolve a region."""
    return subprocess.run(
        [sys.executable, "-c", statement],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": ":".join(sys.path),
            # Neutralise every region source: the variables, the shared config
            # file, and the instance metadata service.
            "AWS_CONFIG_FILE": "/dev/null",
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "AWS_EC2_METADATA_DISABLED": "true",
        },
    )


def test_importing_the_library_needs_no_aws_region():
    """The regression from #988, asserted where it actually bites: at import."""
    result = _run_without_a_region("import idp_common.utils, idp_common.s3")
    assert result.returncode == 0, (
        "importing idp_common with no resolvable AWS region failed. Something on "
        "that import path builds a boto3 client at module scope again; every "
        "offline suite that imports a handler using this library breaks on a CI "
        "runner when it does (#988).\n\n" + result.stderr[-3000:]
    )


def test_executing_the_module_builds_no_client():
    """Re-execute the module and watch ``boto3.client``, which is what the
    subprocess check above proves indirectly and this one pins precisely."""
    with patch("boto3.client") as client:
        importlib.reload(settings_helper)
        client.assert_not_called()
    assert settings_helper._ssm_client is None


def test_the_client_is_built_once_and_reused():
    with patch("boto3.client", return_value=MagicMock()) as client:
        first = settings_helper._get_ssm_client()
        second = settings_helper._get_ssm_client()
    assert first is second
    client.assert_called_once_with("ssm")


def test_concurrent_first_use_builds_exactly_one_client():
    """``boto3.client()`` is not documented as thread-safe, and this is library code
    reachable from any caller, so construction is once-only by construction rather
    than by auditing call sites."""
    start = threading.Barrier(8)
    seen = []

    def build():
        start.wait()
        seen.append(settings_helper._get_ssm_client())

    with patch("boto3.client", return_value=MagicMock()) as client:
        threads = [threading.Thread(target=build) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert len({id(obj) for obj in seen}) == 1, (
        "concurrent first use produced more than one SSM client"
    )
    client.assert_called_once_with("ssm")


def test_clear_cache_drops_the_settings_but_keeps_the_client():
    """``clear_cache`` is documented as clearing the *settings* cache. Dropping the
    client with them would make it a reconnect, which no caller asks for."""
    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": '{"Key": "value"}'}}
    settings_helper._ssm_client = ssm

    assert settings_helper.get_settings(parameter_name="/idp/test") == {"Key": "value"}
    settings_helper.clear_cache()
    assert settings_helper._ssm_client is ssm

    assert settings_helper.get_settings(parameter_name="/idp/test") == {"Key": "value"}
    assert ssm.get_parameter.call_count == 2
