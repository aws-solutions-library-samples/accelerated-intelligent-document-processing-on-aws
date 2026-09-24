# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Provenance guard for the Lambda suites below this directory.

See `scripts/tests/first_party_provenance.py` for the rationale and for the
identity-versus-ancestry distinction the check turns on.

It sits HERE, one level above the handlers, rather than in each handler directory,
because pytest loads every `conftest.py` from the rootdir down to the collected file --
so one file covers every suite beneath it, and a handler added next month is covered
without being listed anywhere.

⚠️ Why these suites needed a guard when they appeared not to. Their test modules do not
import `idp_common` themselves; they import the handler (`from index import ...`), and
the handler imports `idp_common`. The closure gate derived its universe by reading
`test_*.py` files for a literal first-party import, so six directories that reach the
shared library one hop away were outside the universe entirely -- unguarded, and reported
as fully guarded, which is the worse half. The derivation now follows a sibling-module
import one level; this file is what that found.
"""

FIRST_PARTY_UNDER_TEST = ("idp_common",)


# Fail fast if a first-party package resolves outside this checkout. These tests import
# the library as an external dependency, so nothing puts its source on `sys.path` and the
# editable-install pointer alone decides which revision runs. On a machine sharing one
# interpreter between checkouts that pointer is rewritten by whoever last ran
# `make test-cicd`, and the wrong tree produces a green run describing other code.
#
# The imports sit inside the function deliberately: module-level imports in an appended
# block are an E402 lint failure.
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
