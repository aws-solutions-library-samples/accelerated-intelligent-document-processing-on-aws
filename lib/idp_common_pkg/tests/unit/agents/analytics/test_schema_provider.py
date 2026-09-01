# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression tests for the analytics agent's schema-context prompt.

The prompt is user-invisible plumbing until it's wrong — a wrong claim
about a column's home table makes the LLM emit invalid SQL, which
returns COLUMN_NOT_FOUND to the user rather than an answer. These
tests pin the specific columns-per-table contract so the prompt cannot
drift out of sync with the actual Glue table shapes.
"""

import pytest

from idp_common.agents.analytics.schema_provider import (
    get_metering_table_description,
)


@pytest.mark.unit
class TestMeteringHourlyColumnsInPrompt:
    """Round-27 review blocker: the prompt used to tell the agent that
    ``metering_hourly`` had ``n_docs`` and ``sum_pages`` columns. It
    does not — those live on ``metering_docs_hourly`` (the Phase-1
    doc-vs-cost split). The LLM was emitting
    ``SELECT sum_pages FROM metering_hourly`` and getting
    ``COLUMN_NOT_FOUND``.
    """

    def test_metering_hourly_positive_columns_list_has_only_cost_cols(self):
        """The columns list positively attributed to ``metering_hourly``
        must be cost-only (``sum_value``, ``sum_cost``). ``n_docs`` and
        ``sum_pages`` may only appear in NEGATIVE context (a "NEVER
        SELECT" warning), never in the positive-claim list."""
        prompt = get_metering_table_description()
        idx = prompt.find("**`metering_hourly`**")
        assert idx > -1, "metering_hourly section not found"
        # The positive claim is "Cost columns only: `sum_value`, `sum_cost`"
        assert "Cost columns only" in prompt[idx : idx + 500], (
            "metering_hourly bullet must positively declare 'Cost columns "
            "only' so the LLM doesn't infer n_docs/sum_pages from context"
        )
        # And the NEVER SELECT anti-pattern warning must be present
        assert "NEVER SELECT" in prompt[idx : idx + 500], (
            "metering_hourly bullet must call out the n_docs/sum_pages "
            "anti-pattern explicitly — a positive-only claim was the "
            "round-27 blocker."
        )

    def test_metering_hourly_never_positively_lists_docs_columns(self):
        """The DEFECT pattern: a phrase like ``Columns: sum_value,
        sum_cost, n_docs, sum_pages`` attributing all four to
        ``metering_hourly``. Guard against a future regression that
        drops the "Cost columns only" wording and re-lists them all
        together."""
        prompt = get_metering_table_description()
        idx = prompt.find("**`metering_hourly`**")
        section = prompt[idx : idx + 500]
        # A "Columns:" sub-phrase must not enumerate n_docs OR sum_pages
        # in the metering_hourly bullet.
        import re

        m = re.search(r"Columns:\s*([^.]*)", section)
        if m:
            columns_phrase = m.group(1)
            assert "n_docs" not in columns_phrase, (
                f"metering_hourly Columns: phrase still lists n_docs: "
                f"{columns_phrase!r}"
            )
            assert "sum_pages" not in columns_phrase, (
                f"metering_hourly Columns: phrase still lists sum_pages: "
                f"{columns_phrase!r}"
            )

    def test_metering_hourly_partitioning_mentions_hour(self):
        """The prompt must describe the partition as ``date`` + ``hour``.
        Omitting ``hour`` was one of the round-27 blocker fixes — a
        query with ``WHERE date = 'X'`` alone is a full-day scan
        instead of one hour partition."""
        prompt = get_metering_table_description()
        idx = prompt.find("**`metering_hourly`**")
        end = prompt.find("- **`metering_daily`**", idx)
        section = prompt[idx:end]
        # Match either "date + hour" or "date` + `hour" formatting
        assert "hour" in section, (
            "metering_hourly section does not mention the `hour` "
            "partition key. Without it, the LLM emits day-scan queries."
        )

    def test_docs_tables_still_claim_n_docs_sum_pages(self):
        """The doc-grain tables — ``metering_docs_hourly`` and
        ``metering_docs_daily`` — MUST still document ``n_docs`` and
        ``sum_pages`` (those are the columns that actually live there)."""
        prompt = get_metering_table_description()
        # Find the metering_docs bullet
        idx = prompt.find("**`metering_docs_hourly` / `metering_docs_daily`**")
        assert idx > -1, "metering_docs_* section not found in prompt"
        end = prompt.find("Query patterns:", idx)
        section = prompt[idx:end]
        assert "n_docs" in section, (
            "metering_docs_* section is missing the n_docs description"
        )
        assert "sum_pages" in section, (
            "metering_docs_* section is missing the sum_pages description"
        )

    def test_metering_hourly_bullet_calls_out_anti_pattern(self):
        """The specific defense against round-27's blocker: the
        metering_hourly bullet must include the ``NEVER SELECT
        n_docs or sum_pages`` warning verbatim. This is what makes
        the LLM avoid the wrong-table pitfall, not just the absence
        of a positive claim."""
        prompt = get_metering_table_description()
        idx = prompt.find("**`metering_hourly`**")
        section = prompt[idx : idx + 500]
        assert "NEVER SELECT" in section and "metering_docs_hourly" in section, (
            "The metering_hourly bullet must include a NEVER SELECT "
            "anti-pattern pointing at metering_docs_hourly as the "
            "correct home for n_docs / sum_pages."
        )
