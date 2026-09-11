# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pytest configuration for the evaluation test suite."""

import pytest

from idp_common.evaluation import contract


@pytest.fixture(autouse=True)
def _reset_anonymous_root_dedup():
    """Reset the process-wide anonymous-root LRU between every test.

    ``iter_countable_rows`` uses ``contract._seen_anonymous_root_contexts``
    for cross-invocation warning dedup — the LRU is intentionally module-
    level so a warm Lambda's repeated calls share the same dedupe budget.
    In tests, that same module-level state means a fixture with anonymous-
    root rows in one test could bleed a cached context into another test's
    log assertions (finding from #625 review-effort code review — only
    ``test_contract.py`` cleared it manually; tests that indirectly reach
    ``iter_countable_rows`` via ``_run_level_row_aggregates`` or
    ``transform_stickler_result`` did not). Clearing per-test is safe
    because no test relies on cross-test state; each test that cares
    about the LRU still primes it explicitly.
    """
    contract._seen_anonymous_root_contexts.clear()
    yield
    contract._seen_anonymous_root_contexts.clear()
