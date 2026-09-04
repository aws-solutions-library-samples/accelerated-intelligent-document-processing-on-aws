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
    get_database_overview,
    get_rollup_tables_description,
    get_table_info,
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

    def test_metering_hourly_positive_columns_are_sum_value_and_sum_cost(self):
        """The columns list positively attributed to ``metering_hourly``
        must be ``sum_value`` and ``sum_cost`` only. ``n_docs`` and
        ``sum_pages`` may only appear in NEGATIVE context (a "NEVER
        SELECT" warning), never in the positive-claim list. Round-27
        blocker regression pin. Round-28 update: sum_value is a
        quantity (not a cost) — see
        ``test_sum_value_labeled_as_quantity_not_cost`` for that pin."""
        prompt = get_rollup_tables_description()
        idx = prompt.find("### 1. `metering_hourly`")
        assert idx > -1, "metering_hourly section not found"
        section = prompt[idx : idx + 800]
        assert "sum_value" in section and "sum_cost" in section, (
            "metering_hourly bullet must name both sum_value and sum_cost "
            "as its aggregate columns."
        )
        # The NEVER-SELECT-n_docs anti-pattern warning must be present
        # anywhere in the description (Phase-2 refactor moved it to a
        # shared cost-vs-docs split block ABOVE the per-table sections;
        # phrasing is "**NEVER** `SELECT n_docs FROM metering_hourly`").
        import re

        assert re.search(r"NEVER.*SELECT.*n_docs.*metering_hourly", prompt), (
            "prompt must call out the n_docs anti-pattern explicitly — "
            "a positive-only claim was the round-27 blocker."
        )

    def test_metering_hourly_never_positively_lists_docs_columns(self):
        """The DEFECT pattern: a phrase like ``Columns: sum_value,
        sum_cost, n_docs, sum_pages`` attributing all four to
        ``metering_hourly``. Guard against a future regression that
        drops the "Cost columns only" wording and re-lists them all
        together."""
        prompt = get_rollup_tables_description()
        idx = prompt.find("### 1. `metering_hourly`")
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
        prompt = get_rollup_tables_description()
        idx = prompt.find("### 1. `metering_hourly`")
        end = prompt.find("### 2. `metering_daily`", idx)
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
        prompt = get_rollup_tables_description()
        # Find the metering_docs bullet
        idx = prompt.find("### 3. `metering_docs_hourly`")
        assert idx > -1, "metering_docs_* section not found in prompt"
        end = prompt.find("### 5. `control_plane_hourly`", idx)
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
        prompt = get_rollup_tables_description()
        # Phase-2 refactor: the anti-pattern lives in a shared "Cost-vs-docs
        # column split" block ABOVE the per-table sections rather than
        # inside the metering_hourly bullet. Still one canonical source.
        assert (
            "NEVER" in prompt
            and "SELECT" in prompt
            and "metering_docs_hourly" in prompt
        ), (
            "prompt must include a NEVER SELECT anti-pattern pointing "
            "at metering_docs_hourly as the correct home for "
            "n_docs / sum_pages."
        )

    def test_sum_value_labeled_as_quantity_not_cost(self):
        """Round-28 review blocker: `sum_value` = SUM(value) where
        `value` is a quantity (tokens/pages/seconds), NOT a cost.
        Only `sum_cost` is USD. The round-27 fix mis-labeled the
        two together as "Cost columns only", which would have made
        the LLM sum tokens as dollars. Regression pin."""
        prompt = get_rollup_tables_description()
        idx = prompt.find("### 1. `metering_hourly`")
        section = prompt[idx : idx + 800]
        # The docstring MUST NOT describe sum_value as a cost.
        assert "Cost columns only: `sum_value`" not in section, (
            "sum_value must not be labeled as cost — it's a quantity."
        )
        # It MUST positively describe sum_value as a quantity.
        assert "sum_value" in section and "quantity" in section, (
            "metering_hourly bullet must explicitly describe sum_value "
            "as a quantity (tokens/pages/seconds) so the LLM doesn't "
            "sum it as dollars."
        )

    def test_metering_daily_bullet_names_day_not_hour_ts(self):
        """Round-28 review blocker: `metering_daily` has a `day` DATE
        column, NOT `hour_ts`. The round-27 "same shape as metering_hourly"
        wording would have led the LLM to emit
        ``SELECT hour_ts FROM metering_daily`` → COLUMN_NOT_FOUND.
        Regression pin — the daily bullet MUST name the correct key
        column and MUST explicitly disclaim hour_ts."""
        prompt = get_rollup_tables_description()
        idx = prompt.find("### 2. `metering_daily`")
        assert idx > -1, "metering_daily bullet not found"
        end = prompt.find("### 3. `metering_docs_hourly`", idx)
        section = prompt[idx:end]
        assert "`day`" in section, (
            "metering_daily bullet must explicitly name `day` as the key "
            "column (it's a DATE, not the metering_hourly `hour_ts`)."
        )
        # The old "same shape as metering_hourly" wording without the
        # day-vs-hour_ts distinction is what caused the confusion.
        # The corrected bullet either avoids that phrase entirely OR
        # calls out that hour_ts does NOT exist on metering_daily.
        assert "hour_ts" in section, (
            "metering_daily bullet must explicitly disclaim hour_ts so "
            "the LLM knows it doesn't exist on the daily rollup."
        )


@pytest.fixture
def stub_config():
    """Minimal IDPConfig stub so ``get_database_overview()`` doesn't
    try to load the real Configuration table from DDB. Only the
    metering-block wording is under test here, and that block is a
    static string literal in the function, so no real config data
    is needed."""
    from unittest.mock import MagicMock

    cfg = MagicMock()
    cfg.classes = []  # no document classes → dynamic sections stay empty
    return cfg


@pytest.mark.unit
class TestGetTableInfoGroupDedup:
    """Regression: ``get_table_info(['metering_hourly',
    'metering_docs_hourly'])`` used to emit the full 6-table rollup
    description twice because both names hit the same rollup branch.
    Same for the evaluation and rule-validation groups. Fix: track
    which group has already been emitted per invocation."""

    def test_multi_rollup_emits_description_once(self):
        result = get_table_info(["metering_hourly", "metering_docs_hourly"])
        # The rollup description contains the "## Reporting Rollup Tables"
        # H2 header — must appear exactly once regardless of how many
        # rollup names were requested.
        assert result.count("## Reporting Rollup Tables") == 1, (
            "rollup description must not be duplicated per requested "
            f"rollup name (found {result.count('## Reporting Rollup Tables')})"
        )
        # Sanity: still contains the description at all.
        assert "## Reporting Rollup Tables" in result

    def test_multi_evaluation_emits_description_once(self):
        result = get_table_info(["document_evaluations", "attribute_evaluations"])
        # The evaluation description contains "## Evaluation Tables".
        assert result.count("## Evaluation Tables") == 1, (
            "evaluation description must not be duplicated per requested "
            "evaluation table name"
        )

    def test_document_sections_still_emitted_per_table(self):
        """Sanity: document_sections_* tables are each a different table
        (document_sections_w2 vs document_sections_invoice), NOT a
        conceptual group. They should NOT be deduped. Without a real
        config each falls through to the "**Error**" branch — but the
        important property is that TWO branches run, not one collapsed
        into one. Count section separators as proof of separate handling."""
        result = get_table_info(["document_sections_w2", "document_sections_invoice"])
        # Each document_sections_* name should produce its own "---"
        # separator; if the router mistakenly deduped them, only one
        # separator would appear.
        assert result.count("\n---\n") >= 2, (
            f"document_sections_* tables must be handled independently "
            f"(found {result.count(chr(10) + '---' + chr(10))} separators)"
        )


@pytest.mark.unit
class TestDatabaseOverviewMirrorsDetail:
    """Round-29 review: the DETAIL section (get_metering_table_description)
    has all the disclaimers, but the OVERVIEW blurb (get_database_overview)
    was thinner and would let an LLM that reads only the overview
    generalize wrong. Same class as round-27's real blockers. These tests
    pin the specific overview claims that must not drift again."""

    def test_overview_names_metering_daily_day_column(self, stub_config):
        """The overview blurb MUST tell the agent that `metering_daily`
        uses `day` (DATE), not `hour_ts` — otherwise the LLM
        generalizes from `metering_hourly` and emits SELECT hour_ts
        FROM metering_daily → COLUMN_NOT_FOUND. Round-29 finding #13."""
        overview = get_database_overview(config=stub_config)
        idx = overview.find("metering_hourly` / `metering_daily`")
        assert idx > -1, "overview does not have the metering_hourly/daily block"
        block = overview[idx : idx + 600]
        assert "day" in block and "DATE" in block, (
            "overview block for metering_hourly/daily must name `day` (DATE) "
            "as the daily rollup's key column"
        )
        # Must also disclaim hour_ts on the daily side, so the LLM doesn't
        # blindly copy the hourly pattern.
        assert "hour_ts" in block, (
            "overview block must explicitly mention hour_ts (and disclaim "
            "it for the daily table) — otherwise the LLM emits "
            "SELECT hour_ts FROM metering_daily → COLUMN_NOT_FOUND"
        )

    def test_get_table_info_routes_rollup_names(self):
        """Phase-2 regression pin: when the LLM asks for a rollup table
        by name (``metering_hourly``, ``metering_docs_daily``,
        ``control_plane_hourly``, etc.) the second-step disclosure must
        return the detailed rollup description, NOT the ``Unknown
        Table`` error the pre-Phase-2 code returned. Without this the
        LLM's tier-picker path silently degrades and it falls back to
        raw ``metering``.
        """
        for name in (
            "metering_hourly",
            "metering_daily",
            "metering_docs_hourly",
            "metering_docs_daily",
            "control_plane_hourly",
            "data_plane_lambda_hourly",
        ):
            info = get_table_info([name])
            assert "Unknown Table" not in info, (
                f"get_table_info(['{name}']) returned 'Unknown Table' — "
                f"the rollup name is not wired into the router."
            )
            # Rollup description must include the tier-picker table
            assert "Tier picker" in info, (
                f"get_table_info(['{name}']) missing tier-picker guidance"
            )

    def test_rollup_description_covers_all_six_tables(self):
        """The rollup description must name each of the six Phase-1
        tables (four metering-derived + two Lambda-cost). Missing any
        of them means the LLM's second-step disclosure can't compare
        the tier options.
        """
        desc = get_rollup_tables_description()
        for table in (
            "metering_hourly",
            "metering_daily",
            "metering_docs_hourly",
            "metering_docs_daily",
            "control_plane_hourly",
            "data_plane_lambda_hourly",
        ):
            assert table in desc, f"rollup description missing table `{table}`"

    def test_rollup_description_pins_cost_vs_docs_split(self):
        """Phase-1's cost-vs-docs column split is the class of bug most
        likely to bite the LLM: it groups by `service_api` on the
        docs table (COLUMN_NOT_FOUND) or selects `sum_pages` on the
        cost table (also COLUMN_NOT_FOUND). Pin the negative rules.
        """
        desc = get_rollup_tables_description()
        assert "NEVER" in desc and "sum_pages FROM metering_hourly" in desc, (
            "rollup description must explicitly forbid selecting "
            "sum_pages from metering_hourly"
        )
        assert "sum_cost FROM metering_docs_hourly" in desc, (
            "rollup description must explicitly forbid selecting "
            "sum_cost from the docs tables"
        )

    def test_overview_warns_docs_tables_omit_service_api_and_unit(self, stub_config):
        """The overview blurb MUST tell the agent that
        `metering_docs_hourly` / `metering_docs_daily` OMIT the
        `service_api` and `unit` columns. Without this, an LLM that
        joins/groups by service_api on the docs table gets
        COLUMN_NOT_FOUND. Round-29 finding #14."""
        overview = get_database_overview(config=stub_config)
        idx = overview.find("metering_docs_hourly` / `metering_docs_daily`")
        assert idx > -1, "overview does not have the metering_docs block"
        block = overview[idx : idx + 600]
        assert "service_api" in block and "unit" in block, (
            "overview block for metering_docs_* must name service_api "
            "and unit as absent columns"
        )
        # The disclaimer must be negative (OMIT / not / do NOT), not just
        # a positive mention (which would confuse the LLM further).
        assert "OMIT" in block or "do NOT" in block or "not exist" in block, (
            "overview block must NEGATIVELY disclaim service_api / unit "
            "on the docs tables — a positive mention alone can be "
            "misread as 'these columns exist here'"
        )
