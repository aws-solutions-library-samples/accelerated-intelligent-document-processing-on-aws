# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every config operation hands its client's region to ConfigurationManager.

This is the SDK half of the ``idp-cli --region`` fix. ``ConfigurationManager``
now accepts a region (asserted in
``idp_common_pkg/tests/unit/config/test_config_client_region.py``), but that only
helps if the operations layer actually passes it — and the operations layer is
what every ``config-*`` CLI command and ``bootstrap`` go through.

The test is written against the *source* of ``operations/config.py`` rather than
by invoking each operation, because each one first makes a CloudFormation call to
resolve the table name and then talks to DynamoDB; mocking all of that for eight
commands would test the mocks. What matters is the invariant "no construction
site here is region-less", and that is exactly what a reader of the file has to
maintain when adding the ninth command. The construction sites are *discovered*
from the source, not listed here, so a new one is covered the moment it is added.
"""

from __future__ import annotations

import inspect
import re

import pytest

from idp_sdk.operations import config as config_ops

# `ConfigurationManager(` / `ConfigurationReader(` followed by anything up to the
# matching close paren on the same logical construction. Both are region-bearing
# entry points into idp_common's configuration layer.
_CTOR = re.compile(
    r"\b(ConfigurationManager|ConfigurationReader)\(([^()]*(?:\([^()]*\)[^()]*)*)\)",
    re.S,
)


def _construction_sites():
    src = inspect.getsource(config_ops)
    return [(m.group(1), m.group(2)) for m in _CTOR.finditer(src)]


@pytest.mark.unit
def test_construction_sites_are_discoverable():
    """If the regex stops matching, every assertion below passes vacuously."""
    sites = _construction_sites()
    # 8 ConfigurationManager (download/upload/list/revisions/activate/delete/
    # sync_bda, plus the revision branch of download) and 1 ConfigurationReader.
    assert len(sites) >= 9, f"expected >=9 construction sites, found {len(sites)}"


@pytest.mark.unit
def test_every_config_manager_construction_passes_a_region():
    offenders = [
        f"{name}({args.strip()})"
        for name, args in _construction_sites()
        if "region=" not in args
    ]
    assert not offenders, (
        "these construct idp_common's configuration layer without a region, so "
        "they resolve the table name in the requested region and then read or "
        "write it in whatever region the ambient credentials pick: "
        f"{offenders}"
    )


@pytest.mark.unit
def test_region_comes_from_the_client_not_a_literal():
    """A hardcoded region would satisfy the check above while still being wrong."""
    bad = [
        f"{name}({args.strip()})"
        for name, args in _construction_sites()
        if "region=" in args and "self._client._region" not in args
    ]
    assert not bad, f"region must come from self._client._region, got: {bad}"
