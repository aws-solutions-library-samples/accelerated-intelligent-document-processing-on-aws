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
