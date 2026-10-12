# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Integration-test fixtures.

Credential reconciliation is **not** here. The package-level
``tests/conftest.py`` injects sentinel AWS credentials
(``AWS_ACCESS_KEY_ID=testing`` etc.) so that unit tests using moto don't
accidentally hit AWS, and it is also where they are reconciled for tests that
need the real thing — keyed on the ``integration`` marker rather than on this
directory, so the two integration-marked tests that live under ``tests/unit/``
are covered too. They were not, while this file owned the reconciliation (#1307).

Nothing else in this file is needed, so it holds no fixtures. It is kept for one
reason only, which is this paragraph: a credential problem in this tier sends you
to this file first, and an answer here is cheaper than the search that follows
finding nothing. Collection does not depend on it — the directory has an
``__init__.py`` and pytest needs no conftest to walk it.
"""
