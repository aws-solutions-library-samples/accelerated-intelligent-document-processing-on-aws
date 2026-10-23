# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Test bootstrap for this resolver's suite.

The suite imports ``idp_common`` as an external dependency — the resolver gets it
from the IDPCommon Lambda layer at runtime — so nothing here puts its source on
``sys.path`` and the editable-install pointer alone decides which revision runs.
On a machine sharing one interpreter between checkouts that pointer is rewritten
by whoever last ran ``make test-cicd``, and the wrong tree produces a green run
describing other code (#1094).

Placed in THIS directory rather than at an ancestor on purpose: the Makefile runs
each resolver suite with ``cd <dir> && pytest .``, which makes that directory the
rootdir, and pytest does not load conftests above the rootdir.
"""

# This resolver builds a DynamoDB resource at MODULE scope (it reads the config
# prefix mappings and the caller's scope), and DynamoDB — unlike S3, the only
# client this module built before — cannot resolve an endpoint without a region.
# `make/hermetic_aws.mk` strips every region source a CI runner would not have, so
# without this the suite passes on a developer machine (which supplies one from the
# shared AWS config file) and fails on a runner. That is issue #988's defect class,
# and pinning a region in the suite's own conftest is the remedy that file
# prescribes. A per-test fixture is not enough: a test that calls
# `importlib.reload(index)` re-executes module scope under whatever the fixture it
# happens to use has set, and the pre-existing allow-list suite takes none.
import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

FIRST_PARTY_UNDER_TEST = ("idp_common",)


# Rationale, and the identity-vs-ancestry distinction:
# scripts/tests/first_party_provenance.py
def _assert_first_party_provenance(packages):
    import pathlib as _pathlib
    import sys as _sys

    for ancestor in _pathlib.Path(__file__).resolve().parents:
        gate = ancestor / "scripts" / "tests" / "first_party_provenance.py"
        if not gate.is_file():
            continue
        _sys.path.insert(0, str(gate.parent))
        try:
            from first_party_provenance import assert_resolves_in

            for package in packages:
                assert_resolves_in(package, __file__)
        finally:
            _sys.path.pop(0)
        return


_assert_first_party_provenance(FIRST_PARTY_UNDER_TEST)
