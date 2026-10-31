# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Test bootstrap for the review queue sweeper suite.

The handler builds its store at import, and the store's DynamoDB client resolves a
region, so the suite supplies a region and placeholder credentials itself rather
than relying on the caller's environment. It also asserts ``idp_common`` resolves
from this checkout (#1094); placed in this directory because the Makefile runs the
suite with ``cd <dir>``, which makes this directory the rootdir.
"""

import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

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
