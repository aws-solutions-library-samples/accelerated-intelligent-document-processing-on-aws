# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the data-mart rollup Lambda.

Two modes: ``hourly`` (writes ``metering_hourly`` + ``control_plane_hourly``)
and ``daily`` (writes ``metering_daily`` from ``metering_hourly``).

Coverage focus:
- Mode dispatch (hourly vs daily)
- Idempotency (skip if partition already written)
- Time-window math (previous UTC hour / previous UTC day)
- Metering SQL shape (INSERT INTO ... SELECT with correct WHERE)
- Control-plane discovery (all IDP Lambdas minus data-plane)
- CloudWatch metric aggregation → row shape
- Component-label mapping heuristic
"""

import importlib.util
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


# Load the Lambda module by path so we don't need it on the sys.path.
def _load_module():
    spec = importlib.util.spec_from_file_location(
        "data_mart_rollup",
        os.path.join(
            os.path.dirname(__file__),
            "../../../../../src/lambda/data_mart_rollup/index.py",
        ),
    )
    assert spec and spec.loader, "Could not load rollup Lambda module"
    with patch.dict(
        os.environ,
        {
            "REPORTING_DATABASE": "idp-reporting",
            "REPORTING_BUCKET": "test-reporting-bucket",
            "STACK_NAME": "idp-test-stack",
            "ATHENA_WORKGROUP": "primary",
        },
    ):
        with patch("boto3.client"):
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
    # Prepopulate the document_sections_* discovery cache to an empty list.
    # Rationale: the metering_hourly / metering_docs_hourly rollup INSERTs
    # call ``_build_doc_class_cte`` → ``_discover_document_sections_tables``
    # → ``_run_athena_query_with_results`` which paginates via
    # ``athena_client.get_query_results``. Under this file's patched
    # ``boto3.client``, that call returns a MagicMock whose ``get("NextToken")``
    # is always truthy — so the paginator ``while True:`` loop would spin
    # without a sleep and burn CPU + memory (past incident: had to reboot
    # the EC2 host). Prepopulating the cache short-circuits discovery to
    # ``[]``, so ``_build_doc_class_cte`` returns the empty-CTE fragment
    # instantly. Tests that specifically want to verify discovery behavior
    # override this cache directly.
    module._document_sections_tables_cache = []
    return module


@pytest.fixture
def rollup():
    """Reload the module fresh per test so global boto3 mocks don't leak."""
    return _load_module()


@pytest.mark.unit
class TestModeDispatch:
    def test_default_mode_is_hourly(self, rollup):
        """Ad-hoc console invocations with no payload default to hourly
        — the more common case. Failing safe = do the more useful thing."""
        with (
            patch.object(rollup, "_run_hourly", return_value={"mode": "hourly"}) as h,
            patch.object(rollup, "_run_daily") as d,
        ):
            result = rollup.handler({}, None)
        h.assert_called_once()
        d.assert_not_called()
        assert result["mode"] == "hourly"

    def test_explicit_daily_mode(self, rollup):
        with (
            patch.object(rollup, "_run_hourly") as h,
            patch.object(rollup, "_run_daily", return_value={"mode": "daily"}) as d,
        ):
            result = rollup.handler({"mode": "daily"}, None)
        h.assert_not_called()
        d.assert_called_once()
        assert result["mode"] == "daily"

    def test_unknown_mode_raises(self, rollup):
        with pytest.raises(ValueError, match="Unknown rollup mode"):
            rollup.handler({"mode": "weekly"}, None)

    def test_hourly_permanent_failure_reraises_as_valueerror(self, rollup):
        """Round-18 review fix (#209): the ``_run_hourly`` aggregator
        used to ``except Exception → raise RuntimeError`` which
        demoted ValueError (PERMANENT — DLQ immediately) to
        RuntimeError (RETRYABLE — burn both async-retry attempts).
        Now the class is preserved: if ANY sub-rollup raised
        ValueError, the aggregate raises ValueError.
        """
        with (
            patch.object(
                rollup, "_rollup_metering_hourly", side_effect=ValueError("permanent")
            ),
            patch.object(
                rollup, "_rollup_metering_docs_hourly", return_value={"skipped": False}
            ),
            patch.object(
                rollup, "_rollup_control_plane_hourly", return_value={"skipped": False}
            ),
        ):
            with pytest.raises(ValueError, match="permanent"):
                rollup._run_hourly()

    def test_hourly_retryable_failure_stays_runtimeerror(self, rollup):
        """Complement to the permanent test above: a RuntimeError from
        a sub-rollup (transient throttle, etc.) must remain
        RuntimeError so async retry replays.
        """
        with (
            patch.object(
                rollup,
                "_rollup_metering_hourly",
                side_effect=RuntimeError("throttled"),
            ),
            patch.object(
                rollup, "_rollup_metering_docs_hourly", return_value={"skipped": False}
            ),
            patch.object(
                rollup, "_rollup_control_plane_hourly", return_value={"skipped": False}
            ),
        ):
            with pytest.raises(RuntimeError, match="throttled"):
                rollup._run_hourly()


@pytest.mark.unit
class TestTimeWindows:
    """Time math is load-bearing — a bug here writes the wrong partition
    and either misses a whole hour of data or double-counts it."""

    def test_previous_hour_wraps_over_midnight(self, rollup):
        """00:00 UTC processes the previous day's last hour (23)."""
        with patch.object(rollup, "datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            date, hour = rollup._previous_hour()
        assert date == "2026-08-17"
        assert hour == "23"

    def test_previous_hour_normal_case(self, rollup):
        with patch.object(rollup, "datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 8, 18, 14, 5, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            date, hour = rollup._previous_hour()
        assert date == "2026-08-18"
        assert hour == "13"

    def test_previous_day(self, rollup):
        with patch.object(rollup, "datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 8, 18, 0, 15, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            date = rollup._previous_day()
        assert date == "2026-08-17"

    def test_hour_window_returns_utc_bounds(self, rollup):
        start, end = rollup._hour_window("2026-08-18", "14")
        assert start == datetime(2026, 8, 18, 14, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 8, 18, 15, 0, tzinfo=timezone.utc)

    def test_anchor_from_event_time_pins_retry_to_trigger_time(self, rollup):
        """Async retry must roll up the ORIGINAL trigger's hour, not
        wall-clock. If EventBridge fires at 14:05 UTC for hour 13 and
        the first attempt fails at 15:07 UTC (crossing hour boundary),
        the retry must still target hour 13 — not accidentally start
        rolling up hour 14 by using datetime.now()."""
        event = {"time": "2026-08-18T14:05:00Z"}
        anchor = rollup._parse_anchor_time(event)
        assert anchor == datetime(2026, 8, 18, 14, 5, tzinfo=timezone.utc)
        date, hour = rollup._previous_hour(anchor)
        assert (date, hour) == ("2026-08-18", "13")

    def test_anchor_without_event_time_falls_back_to_now(self, rollup):
        """Manual `aws lambda invoke` payloads don't include a `time`
        field. Fall back to wall-clock so the escape hatch keeps working."""
        with patch.object(rollup, "datetime") as mock_dt:
            mock_dt.now.return_value = datetime(
                2026, 8, 18, 15, 30, tzinfo=timezone.utc
            )
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            # patch also patches datetime.fromisoformat inside the module,
            # so provide a passthrough
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            anchor = rollup._parse_anchor_time({})
        assert anchor.hour == 15

    def test_anchor_malformed_event_time_falls_back_to_now(self, rollup):
        """A garbage `time` field must not crash the rollup — fall back
        to now() with a warning. Prod ain't the place to enforce ISO 8601."""
        with patch.object(rollup, "datetime") as mock_dt:
            mock_dt.now.return_value = datetime(
                2026, 8, 18, 15, 30, tzinfo=timezone.utc
            )
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            # fromisoformat is called on the raw string — make it raise
            mock_dt.fromisoformat.side_effect = ValueError("not iso")
            anchor = rollup._parse_anchor_time({"time": "definitely-not-a-timestamp"})
        assert anchor.hour == 15


@pytest.mark.unit
class TestMeteringHourlyRollup:
    """The core value-add: partition + skip-if-exists + INSERT SQL."""

    def test_skips_when_partition_already_written(self, rollup):
        """Idempotency guard — if the target partition has any rows, we
        MUST NOT run the INSERT again. Duplicate EventBridge fires would
        double-count otherwise."""
        with (
            patch.object(rollup, "_partition_already_written", return_value=True),
            patch.object(rollup, "_run_athena") as mock_athena,
        ):
            result = rollup._rollup_metering_hourly("2026-08-18", "13")
        assert result["skipped"] is True
        assert result["reason"] == "partition_exists"
        mock_athena.assert_not_called()

    def test_insert_sql_filters_to_target_partition(self, rollup):
        """The INSERT ... SELECT must scope to the target (date, hour)
        via a WHERE clause with partition columns — otherwise we'd
        scan (and re-aggregate) the whole table."""
        captured_sql = []
        with (
            patch.object(rollup, "_partition_already_written", return_value=False),
            patch.object(
                rollup,
                "_run_athena",
                side_effect=lambda sql, **_kw: captured_sql.append(sql) or "qid-123",
            ),
        ):
            result = rollup._rollup_metering_hourly("2026-08-18", "13")
        assert result["skipped"] is False
        assert result["query_execution_id"] == "qid-123"
        sql = captured_sql[0]
        assert "INSERT INTO" in sql
        assert '"metering_hourly"' in sql
        assert "FROM" in sql and '"metering"' in sql
        assert "date = '2026-08-18'" in sql
        assert "hour = '13'" in sql
        # Must GROUP BY the rollup dimensions
        assert "GROUP BY" in sql
        # Pages + doc counts DO NOT belong in this table — they fan out
        # by service_api. They live in metering_docs_hourly instead.
        assert "sum_pages" not in sql, (
            "metering_hourly must NOT compute sum_pages — number_of_pages "
            "is stamped on every metering row and grouping by service_api "
            "would multiply pages by the number of (service_api, unit) "
            "combinations a doc touched (6x for a typical doc)."
        )
        assert "n_doc_events" not in sql, (
            "metering_hourly must NOT compute n_doc_events — same "
            "fan-out problem as sum_pages. Doc-grain counts belong "
            "in metering_docs_hourly."
        )

    def test_hourly_insert_passes_idempotency_key(self, rollup):
        """Round-16 review fix: on Lambda async retry, ``start_query_execution``
        must receive a stable ``ClientRequestToken`` so Athena returns the
        SAME QueryExecutionId instead of double-writing the partition
        against propagation-lagged Glue metadata.

        Round-17 review fix: the key MUST also be 32-128 chars —
        Athena's ClientRequestToken rejects shorter tokens at boto3
        client-side validation before the query ever reaches Athena,
        which turned every hourly/daily rollup into a total outage.
        """
        captured_kwargs = {}
        with (
            patch.object(rollup, "_partition_already_written", return_value=False),
            patch.object(
                rollup,
                "_run_athena",
                side_effect=lambda sql, **kw: captured_kwargs.update(kw) or "qid",
            ),
        ):
            rollup._rollup_metering_hourly("2026-08-27", "13")
        # A stable, deterministic idempotency key must have been passed,
        # keyed on (table, date, hour).
        assert "idempotency_key" in captured_kwargs
        tok = captured_kwargs["idempotency_key"]
        assert "metering_hourly" in tok
        assert "2026-08-27" in tok
        assert "13" in tok
        # Athena's hard requirement: 32-128 chars.
        assert 32 <= len(tok) <= 128, (
            f"ClientRequestToken length {len(tok)} outside Athena's 32-128 range: {tok!r}"
        )
        # Alphabet: letters, digits, dash, underscore. Anything else
        # (whitespace, dots from dates, colons from timestamps) has to
        # have been sanitized out.
        import re as _re

        assert _re.fullmatch(r"[A-Za-z0-9_-]+", tok), (
            f"ClientRequestToken has invalid chars: {tok!r}"
        )

    def test_all_four_rollup_idempotency_keys_clear_athena_length_floor(self, rollup):
        """Round-17 blocker regression pin: all four rollup INSERTs
        (metering_hourly, metering_docs_hourly, metering_daily,
        metering_docs_daily) must produce ClientRequestToken values in
        Athena's 32-128 range. The round-16 f-string keys built 25-34
        chars, so 3 of 4 rollup fires threw ParamValidationError at
        boto3 client-side validation on every scheduled invocation.
        """
        for table, date, hour in [
            ("metering_hourly", "2026-08-27", "13"),
            ("metering_docs_hourly", "2026-08-27", "13"),
            ("metering_daily", "2026-08-27", None),
            ("metering_docs_daily", "2026-08-27", None),
        ]:
            k = rollup._idempotency_key(table, date, hour)
            assert 32 <= len(k) <= 128, (
                f"Idempotency key for {table} ({len(k)} chars): {k!r}"
            )

    def test_idempotency_key_distinct_partitions_never_collide_with_long_stack_name(
        self, rollup, monkeypatch
    ):
        """Round-18 review fix (#249): the round-17 implementation
        prepended the (long) stack-scoped prefix and truncated the
        whole key at 128 chars. On a stack whose name is long enough
        (well within CFN's 128-char stack-name limit), truncation
        would chop the trailing (table, date, hour) discriminator and
        distinct partitions could collide on the same Athena
        ClientRequestToken. Athena's dedup would then return the
        earlier query's SUCCEEDED QueryExecutionId and the second
        INSERT would silently no-op — the rollup would appear healthy
        while some partitions were missing.

        Fix: place the discriminator FIRST, then the prefix. Any
        truncation eats stack-name bloat, not distinguishing state.
        """
        # A worst-case stack name that would definitely blow the 128
        # cap if the prefix went first.
        long_stack = "idp-dev-" + ("verylongname" * 12)
        monkeypatch.setattr(rollup, "STACK_NAME", long_stack)
        monkeypatch.setattr(
            rollup, "_IDEMPOTENCY_KEY_PREFIX", f"idp-rollup-{long_stack}"
        )
        keys = set()
        for table, date, hour in [
            ("metering_hourly", "2026-08-27", "13"),
            ("metering_hourly", "2026-08-27", "14"),
            ("metering_hourly", "2026-08-28", "13"),
            ("metering_docs_hourly", "2026-08-27", "13"),
            ("metering_daily", "2026-08-27", None),
            ("metering_docs_daily", "2026-08-27", None),
        ]:
            k = rollup._idempotency_key(table, date, hour)
            assert 32 <= len(k) <= 128, (
                f"{table}/{date}/{hour}: length {len(k)} outside 32-128: {k!r}"
            )
            assert k not in keys, (
                f"COLLISION on {table}/{date}/{hour}: {k!r} — long-stack "
                f"truncation just chopped the discriminator (round-18 #249)"
            )
            keys.add(k)


@pytest.mark.unit
class TestMeteringDocsHourlyRollup:
    """Doc-grain rollup fixes the fan-out bug: number_of_pages is a
    document-level value stamped identically on every metering row for
    that doc, so ``SUM(number_of_pages) GROUP BY service_api`` returns
    6× the true page count for a doc that hit 6 (service_api, unit)
    combinations. metering_docs_hourly aggregates at (hour, config_version)
    grain via a MAX-per-doc subquery.
    """

    def test_docs_rollup_sql_uses_doc_grain_subquery(self, rollup):
        """The SQL MUST use MAX(number_of_pages) per document in an inner
        subquery before aggregating — otherwise pages fan out by
        service_api count."""
        captured = []
        with (
            patch.object(rollup, "_partition_already_written", return_value=False),
            patch.object(
                rollup,
                "_run_athena",
                side_effect=lambda sql, **_kw: captured.append(sql) or "qid-docs",
            ),
        ):
            result = rollup._rollup_metering_docs_hourly("2026-08-18", "13")
        assert result["skipped"] is False
        sql = captured[0]
        assert '"metering_docs_hourly"' in sql
        # Doc-grain subquery — MAX-collapses pages before outer aggregate.
        # The doc_class widening (grain now includes document_class) added a
        # LEFT JOIN with the doc_class CTE, so the metering table is aliased
        # ``m`` and the column reference is now ``m.number_of_pages``.
        assert "MAX(m.number_of_pages)" in sql or "MAX(number_of_pages)" in sql
        # Grain is (hour_ts, config_version, document_class) — NOT service_api
        # / unit. The CTE prefix ``WITH doc_class AS (SELECT ...)`` is stripped
        # so the outer-SELECT slice below isn't polluted by the CTE's inner
        # SELECT (which selects nulls for the fallback-only path).
        _after_insert = sql.split("INSERT", 1)[1]
        _after_cte = (
            _after_insert.split(")", 1)[1]
            if "WITH doc_class" in _after_insert
            else _after_insert
        )
        outer_select_cols = _after_cte.split("SELECT", 1)[1].split("FROM", 1)[0]
        assert "service_api" not in outer_select_cols, (
            "Outer SELECT must not include service_api or unit as dims — "
            "that's the fan-out bug this table exists to avoid."
        )
        assert "date = '2026-08-18'" in sql
        assert "hour = '13'" in sql

    def test_docs_rollup_skips_when_partition_exists(self, rollup):
        """Idempotency guard."""
        with (
            patch.object(rollup, "_partition_already_written", return_value=True),
            patch.object(rollup, "_run_athena") as mock_athena,
        ):
            result = rollup._rollup_metering_docs_hourly("2026-08-18", "13")
        assert result["skipped"] is True
        mock_athena.assert_not_called()


@pytest.mark.unit
class TestMeteringDailyRollup:
    def test_reads_from_metering_hourly_not_raw(self, rollup):
        """The daily rollup reads the already-aggregated ``metering_hourly``
        table, not raw ``metering``. Reading raw would defeat the purpose
        of the hourly rollup (same GB scan, twice the work).
        """
        captured_sql = []
        with (
            patch.object(rollup, "_partition_already_written", return_value=False),
            patch.object(rollup, "_require_hourly_matches_raw_metering"),
            patch.object(
                rollup,
                "_run_athena",
                side_effect=lambda sql, **_kw: captured_sql.append(sql) or "qid-456",
            ),
        ):
            rollup._run_daily()
        # Two INSERTs — one to metering_daily (cost), one to metering_docs_daily
        # (volume/pages). Both read from their sibling hourly rollup, not raw.
        joined = " ".join(captured_sql)
        assert '"metering_daily"' in joined
        assert '"metering_docs_daily"' in joined
        assert '"metering_hourly"' in joined
        assert '"metering_docs_hourly"' in joined
        # metering_daily is cost-only — no fanned-out volume columns
        cost_sql = next(s for s in captured_sql if '"metering_daily"' in s)
        assert "sum_pages" not in cost_sql
        assert "n_doc_events" not in cost_sql
        # metering_docs_daily has doc-grain fields
        docs_sql = next(s for s in captured_sql if '"metering_docs_daily"' in s)
        assert "SUM(n_docs)" in docs_sql
        assert "SUM(sum_pages)" in docs_sql

    def test_daily_skip_when_partition_exists(self, rollup):
        with (
            patch.object(rollup, "_partition_already_written", return_value=True),
            patch.object(rollup, "_require_hourly_matches_raw_metering"),
            patch.object(rollup, "_run_athena") as mock_athena,
        ):
            result = rollup._run_daily()
        assert result["skipped"] is True
        mock_athena.assert_not_called()

    def test_daily_raises_when_hourly_missing_a_populated_hour(self, rollup):
        """Missing any hour that RAW metering has data for must abort the
        daily rollup — that's the "hourly rollup transiently failed" case
        the guard exists to catch. Async retry will replay after the
        hourly catches up."""

        # Simulate: raw has hours 00-23 with data; metering_hourly missing hour 23
        # (the exact boundary case where the 23:xx hourly rollup failed retries).
        raw_hours = [[f"{h:02d}"] for h in range(24)]
        hourly_hours = [[f"{h:02d}"] for h in range(23)]

        def fake_query(sql, **_kwargs):
            return (
                raw_hours
                if '"metering"' in sql and "hourly" not in sql
                else hourly_hours
            )

        with (
            patch.object(rollup, "_partition_already_written", return_value=False),
            patch.object(
                rollup, "_run_athena_query_with_results", side_effect=fake_query
            ),
        ):
            with pytest.raises(RuntimeError, match="missing hours"):
                rollup._run_daily()

    def test_daily_check_passes_when_hourly_matches_raw(self, rollup):
        """Sanity — every hour that has raw data is rolled up. Guard passes
        even when the day is only partially populated (e.g. deploy day)."""
        # Deploy-day scenario: raw metering has hours 12-19 only.
        # metering_hourly has the same hours 12-19. Should PASS the guard.
        partial_hours = [[f"{h:02d}"] for h in range(12, 20)]
        captured_sql = []

        def fake_query(_sql, **_kwargs):
            return partial_hours  # both queries return the same partial set

        with (
            patch.object(rollup, "_partition_already_written", return_value=False),
            patch.object(
                rollup, "_run_athena_query_with_results", side_effect=fake_query
            ),
            patch.object(
                rollup,
                "_run_athena",
                side_effect=lambda sql, **_kw: captured_sql.append(sql) or "qid-789",
            ),
        ):
            result = rollup._run_daily()
        assert result["skipped"] is False
        # The INSERT must actually run when the check passes.
        assert any("INSERT INTO" in s for s in captured_sql)

    def test_daily_check_passes_when_day_has_no_data_at_all(self, rollup):
        """A day with zero raw metering data (idle stack, holiday) should
        NOT block the daily rollup — the INSERT writes zero rows, but the
        partition is 'sealed' with a legitimate empty result."""
        empty = []

        def fake_query(_sql, **_kwargs):
            return empty

        captured_sql = []
        with (
            patch.object(rollup, "_partition_already_written", return_value=False),
            patch.object(
                rollup, "_run_athena_query_with_results", side_effect=fake_query
            ),
            patch.object(
                rollup,
                "_run_athena",
                side_effect=lambda sql, **_kw: captured_sql.append(sql) or "qid-empty",
            ),
        ):
            result = rollup._run_daily()
        assert result["skipped"] is False
        assert any("INSERT INTO" in s for s in captured_sql)

    def test_daily_check_passes_when_hourly_is_empty_deploy_day(self, rollup):
        """Deploy-day case: raw ``metering`` has hours that predate the
        rollup Lambda existing, and ``metering_hourly`` is completely
        empty for the target date. The hourly cron only ever targets
        ``previous_hour(anchor)``, so those pre-deploy hours will never
        be backfilled. The guard must treat empty hourly as "first run"
        and skip — a strict guard here would poison the first-ever daily
        rollup forever (idempotency skip). Regression pin for round-3
        review finding: first-daily-after-deploy fails permanently.

        Round-13 review fix: ``_hourly_ever_written`` now also probes raw
        metering on PRIOR dates — a stack running >24h with a broken
        hourly rollup would previously mask as deploy-day (both hourly
        and target-date-only signals empty). This test simulates the
        genuine day-1 deploy: raw has target-date-only data, and NEITHER
        the hourly-prior probe NOR the raw-prior probe returns anything.
        """
        # Raw metering: 5 hours on TARGET date. Hourly: EMPTY. Prior-date
        # probes (WHERE date < 'X') return empty in the true day-1 case.
        raw_hours = [[f"{h:02d}"] for h in range(0, 5)]
        hourly_hours = []

        calls = {"n": 0}

        def fake_query(sql, **_kwargs):
            calls["n"] += 1
            # Prior-date probes (LIMIT 1 with WHERE date < ...) — the
            # round-13 fix probes BOTH metering_hourly and raw metering
            # here. A genuine day-1 deploy has zero rows on both.
            if "WHERE date <" in sql:
                return []
            if 'FROM "reporting"."metering_hourly"' in sql or "metering_hourly" in sql:
                return hourly_hours
            return raw_hours

        captured_sql = []
        with (
            patch.object(rollup, "_partition_already_written", return_value=False),
            patch.object(
                rollup, "_run_athena_query_with_results", side_effect=fake_query
            ),
            patch.object(
                rollup,
                "_run_athena",
                side_effect=lambda sql, **_kw: (
                    captured_sql.append(sql) or "qid-first-run"
                ),
            ),
        ):
            result = rollup._run_daily()
        assert result["skipped"] is False
        # The INSERT must actually run — the guard must not have blocked us.
        assert any("INSERT INTO" in s for s in captured_sql)

    def test_daily_check_fires_on_multi_day_outage_masking_as_deploy_day(self, rollup):
        """Round-13 review fix: a multi-day hourly outage would previously
        look identical to a legitimate deploy-day (metering_hourly empty
        on target AND on every prior date), and the guard would
        incorrectly SKIP, letting a 0-doc daily land and lock idempotently.

        The fix: ``_hourly_ever_written`` also probes raw ``metering`` on
        prior dates — if raw shows the stack was processing docs before
        the target date, this can't be day-1, so the guard MUST fire on
        empty target-date hourly.
        """
        raw_hours = [[f"{h:02d}"] for h in range(0, 5)]
        hourly_hours = []  # target-date hourly empty

        def fake_query(sql, **_kwargs):
            # Prior-date HOURLY probe (day-1 signal 1) — empty.
            if 'FROM "reporting"."metering_hourly"' in sql and "WHERE date <" in sql:
                return []
            # Prior-date RAW probe (day-1 signal 2) — POPULATED, so we're
            # NOT day-1. The stack was up on prior dates but the hourly
            # rollup wasn't running (outage). Guard MUST fire.
            if 'FROM "reporting"."metering"' in sql and "WHERE date <" in sql:
                return [["1"]]
            if 'FROM "reporting"."metering_hourly"' in sql or "metering_hourly" in sql:
                return hourly_hours
            return raw_hours

        with (
            patch.object(rollup, "_partition_already_written", return_value=False),
            patch.object(
                rollup, "_run_athena_query_with_results", side_effect=fake_query
            ),
            patch.object(rollup, "_run_athena") as mock_athena,
        ):
            with pytest.raises(RuntimeError, match="systematic failure"):
                rollup._run_daily()
        mock_athena.assert_not_called()  # INSERT must not fire

    def test_daily_check_ignores_raw_hours_before_earliest_hourly(self, rollup):
        """Round-3 fix: the guard scopes its "raw ⊆ hourly" check to hours
        ≥ the earliest hour actually written to ``metering_hourly``.
        Any earlier raw hour is treated as pre-deploy history that the
        hourly cron will never backfill (see the deploy-day case above).
        """
        # Raw has 00..09; hourly starts at 05 (deploy at 05:00).
        raw_hours = [[f"{h:02d}"] for h in range(0, 10)]
        hourly_hours = [[f"{h:02d}"] for h in range(5, 10)]

        def fake_query(sql, **_kwargs):
            if "metering_hourly" in sql:
                return hourly_hours
            return raw_hours

        captured_sql = []
        with (
            patch.object(rollup, "_partition_already_written", return_value=False),
            patch.object(
                rollup, "_run_athena_query_with_results", side_effect=fake_query
            ),
            patch.object(
                rollup,
                "_run_athena",
                side_effect=lambda sql, **_kw: captured_sql.append(sql) or "qid-scoped",
            ),
        ):
            result = rollup._run_daily()
        # Hours 00-04 exist in raw but predate hourly rollup — must NOT
        # trigger the missing-hours error. INSERT should run.
        assert result["skipped"] is False
        assert any("INSERT INTO" in s for s in captured_sql)

    def test_dropped_hourly_and_empty_raw_treats_as_day_one(self, rollup):
        """Round-16 review fix (regression of round-15): a dropped
        hourly table with NO raw-metering history → day-1 signal
        (return False so ``is_deploy_day`` becomes True).
        """

        def fake_query(sql, **_kwargs):
            if "metering_hourly" in sql:
                raise RuntimeError("TABLE_NOT_FOUND: metering_hourly does not exist")
            return []  # raw metering has no prior-date rows either

        with patch.object(
            rollup, "_run_athena_query_with_results", side_effect=fake_query
        ):
            assert rollup._hourly_ever_written(before_date="2026-08-27") is False

    def test_dropped_hourly_but_raw_has_history_returns_true(self, rollup):
        """Round-16 review fix: if hourly is missing but raw metering
        still has prior-date rows, we are NOT day-1. Round-15's fix
        returned False here, misclassifying a real outage as deploy-day
        and letting a 0-doc daily land. Now: fall through to raw probe;
        raw with prior data → return True → guard fires as expected.
        """

        def fake_query(sql, **_kwargs):
            if "metering_hourly" in sql:
                raise RuntimeError("TABLE_NOT_FOUND: metering_hourly does not exist")
            # Raw metering has prior-date history — NOT day-1.
            return [["1"]]

        with patch.object(
            rollup, "_run_athena_query_with_results", side_effect=fake_query
        ):
            assert rollup._hourly_ever_written(before_date="2026-08-27") is True

    def test_daily_guards_only_sub_hourly_of_the_daily_being_written(self, rollup):
        """Round-15 review fix #4: when metering_daily is already
        committed but metering_docs_daily is not, an unrelated gap in
        ``metering_hourly`` must NOT block the docs-daily write. Guard
        only ``metering_docs_hourly`` in that case.
        """
        raw_hours = [[f"{h:02d}"] for h in range(0, 5)]
        docs_hourly_hours = [[f"{h:02d}"] for h in range(0, 5)]

        def fake_query(sql, **_kwargs):
            if "WHERE date <" in sql:
                return []  # deploy-day-ish probe
            if 'FROM "reporting"."metering_hourly"' in sql:
                # Intentionally return a GAP — this should NOT be checked
                # because metering_daily is already written.
                return [["00"], ["04"]]
            if 'FROM "reporting"."metering_docs_hourly"' in sql:
                return docs_hourly_hours
            return raw_hours  # raw

        def partition_written(table, date, hour=None):  # noqa: ARG001
            return table == "metering_daily"

        captured_sql = []
        with (
            patch.object(
                rollup, "_partition_already_written", side_effect=partition_written
            ),
            patch.object(
                rollup, "_run_athena_query_with_results", side_effect=fake_query
            ),
            patch.object(
                rollup,
                "_run_athena",
                side_effect=lambda sql, **_kw: captured_sql.append(sql) or "qid",
            ),
        ):
            result = rollup._run_daily()
        # metering_daily skipped (already written); docs-daily INSERT
        # must have fired despite the fake gap in metering_hourly.
        assert result["metering_daily"] == {"skipped": True}
        assert result["metering_docs_daily"]["skipped"] is False
        # DATABASE comes from the test fixture env var; assert only the
        # table-name and INSERT verb.
        assert any(
            "INSERT INTO" in s and "metering_docs_daily" in s for s in captured_sql
        )
        # The metering_hourly gap MUST not have triggered a guard raise
        # (its daily is idempotency-skipped, so the gap can't affect us).
        assert not any(
            "INSERT INTO" in s and '"metering_daily"' in s for s in captured_sql
        )


@pytest.mark.unit
class TestControlPlaneDiscovery:
    """Discovery is subtractive: all IDP Lambdas in the stack TREE minus
    data-plane (allowlist model, §10.3). Nested-stack Lambdas MUST be
    included — a Lambda in a nested stack carries the nested stack's
    aws:cloudformation:stack-name tag, not the root stack's. Filtering by
    root name alone missed 57 of 68 Lambdas on this repo's live topology."""

    def test_stack_tree_bfs_walks_nested_stacks(self, rollup):
        """Regression: enumerate_stack_tree must recurse into nested stacks.
        Skipping this walk was the bug that silently invisible-ed every
        Lambda under nested/api-resolvers/ (test-results, test-runner,
        config-mgmt, capacity-planner, finetuning, user-mgmt, etc.)."""
        # Map of stack name → its list_stack_resources page(s).
        pages_by_stack = {
            "root": [
                {
                    "StackResourceSummaries": [
                        {
                            "ResourceType": "AWS::CloudFormation::Stack",
                            "PhysicalResourceId": "arn:aws:cloudformation:us-east-1:1:stack/root-APIRESOLVERSTACK/abc",
                        },
                        {
                            "ResourceType": "AWS::CloudFormation::Stack",
                            "PhysicalResourceId": "arn:aws:cloudformation:us-east-1:1:stack/root-BEDROCKKB/def",
                        },
                        {
                            "ResourceType": "AWS::Lambda::Function",
                            "PhysicalResourceId": "root-something",
                        },
                    ]
                }
            ],
            "root-APIRESOLVERSTACK": [
                {
                    "StackResourceSummaries": [
                        {
                            "ResourceType": "AWS::CloudFormation::Stack",
                            "PhysicalResourceId": "arn:aws:cloudformation:us-east-1:1:stack/root-APIRESOLVERSTACK-DEEP/xyz",
                        }
                    ]
                }
            ],
        }

        def paginate(**kwargs):
            return iter(
                pages_by_stack.get(
                    kwargs["StackName"], [{"StackResourceSummaries": []}]
                )
            )

        paginator = MagicMock()
        paginator.paginate.side_effect = paginate
        cfn = MagicMock()
        cfn.get_paginator.return_value = paginator

        with patch("boto3.client", return_value=cfn):
            tree = rollup._enumerate_stack_tree("root")
        # BFS: root, then its direct children, then grandchildren.
        assert tree == [
            "root",
            "root-APIRESOLVERSTACK",
            "root-BEDROCKKB",
            "root-APIRESOLVERSTACK-DEEP",
        ]

    def test_discovery_subtracts_data_plane_across_full_tree(self, rollup):
        """Discovery scopes BOTH the ``all_idp`` list AND the ``data_plane``
        subtraction to the full stack tree, not just the root. On a shared
        account with multiple IDP stacks, only THIS stack's data-plane
        Lambdas are subtracted from THIS stack's control-plane list."""
        stack_tree = ["idp-test-stack", "idp-test-stack-APIRESOLVERSTACK-abc"]
        # Root-stack Lambda (previously the only one seen) + a nested one
        # (which the old code missed).
        all_idp = [
            "arn:aws:lambda:us-east-1:1:function:OCRFunction",  # root, data plane
            "arn:aws:lambda:us-east-1:1:function:TestResultsResolver",  # nested, control
            "arn:aws:lambda:us-east-1:1:function:ConfigResolver",  # nested, control
        ]
        data_plane = ["arn:aws:lambda:us-east-1:1:function:OCRFunction"]

        def fake_get(tags):
            if tags == {"aws:cloudformation:stack-name": stack_tree}:
                return list(all_idp)
            if tags == {
                "aws:cloudformation:stack-name": stack_tree,
                "idp:plane": ["data"],
            }:
                return list(data_plane)
            return []

        with (
            patch.object(rollup, "_enumerate_stack_tree", return_value=stack_tree),
            patch.object(rollup, "_get_resources_by_tag", side_effect=fake_get),
        ):
            control = rollup._discover_control_plane_lambdas()
        assert set(control) == {
            "arn:aws:lambda:us-east-1:1:function:TestResultsResolver",
            "arn:aws:lambda:us-east-1:1:function:ConfigResolver",
        }

    def test_stack_tree_and_data_plane_query_run_once_per_invocation(self, rollup):
        """Both hourly plane rollups need the stack tree and the data-plane ARN
        set, and neither can change mid-invocation. They used to be re-derived
        independently — a second ``ListStackResources`` walk across every nested
        stack plus a second Tagging API query on every hourly fire."""
        stack_tree = ["idp-test-stack", "idp-test-stack-APIRESOLVERSTACK-abc"]
        data_plane = ["arn:aws:lambda:us-east-1:1:function:OCRFunction"]

        def fake_get(tags):
            if "idp:plane" in tags:
                return list(data_plane)
            return list(data_plane) + [
                "arn:aws:lambda:us-east-1:1:function:TestResultsResolver"
            ]

        with (
            patch.object(
                rollup, "_enumerate_stack_tree", return_value=stack_tree
            ) as walk,
            patch.object(
                rollup, "_get_resources_by_tag", side_effect=fake_get
            ) as tag_query,
        ):
            control = rollup._discover_control_plane_lambdas()
            data = rollup._discover_data_plane_lambdas()

        assert control == ["arn:aws:lambda:us-east-1:1:function:TestResultsResolver"]
        assert data == data_plane
        assert walk.call_count == 1, (
            f"stack tree walked {walk.call_count}x for one invocation — the "
            f"per-invocation cache is not being used"
        )
        # One query for the full set, one for the idp:plane=data subset. Without
        # the cache the data-plane subset is fetched twice (3 calls total).
        assert tag_query.call_count == 2, (
            f"{tag_query.call_count} tag queries for one invocation; expected 2 "
            f"(all-IDP + data-plane, each fetched once)"
        )

    def test_handler_clears_the_discovery_caches(self, rollup):
        """The caches must NOT survive across invocations: a stack update
        between rollup fires can add or remove Lambdas, and a warm container
        would otherwise keep reporting the old topology."""
        rollup._stack_tree_cache = ["stale-stack"]
        rollup._data_plane_arn_cache = ["arn:aws:lambda:us-east-1:1:function:Stale"]
        with patch.object(rollup, "_run_hourly", return_value={}):
            rollup.handler({"mode": "hourly"}, None)
        assert rollup._stack_tree_cache is None
        assert rollup._data_plane_arn_cache is None

    def test_warns_on_probable_untagged_data_plane(self, rollup, caplog):
        """A Lambda with a doc-processing name that lacks the data tag
        is drift-detector fodder — WARN log for the operator to fix."""
        import logging

        stack_tree = ["idp-test-stack"]
        all_idp = [
            "arn:aws:lambda:us-east-1:1:function:MyExtractionFunctionRedacted",
        ]

        def fake_get(tags):
            if tags == {"aws:cloudformation:stack-name": stack_tree}:
                return list(all_idp)
            return []

        with (
            patch.object(rollup, "_enumerate_stack_tree", return_value=stack_tree),
            patch.object(rollup, "_get_resources_by_tag", side_effect=fake_get),
        ):
            with caplog.at_level(logging.WARNING):
                control = rollup._discover_control_plane_lambdas()

        assert control  # still returned
        assert any("untagged data-plane Lambda" in m for m in caplog.messages), (
            f"Expected WARN about probable untagged data-plane Lambda, "
            f"got: {caplog.messages!r}"
        )

    def test_returns_empty_when_stack_name_missing(self, rollup):
        with patch.object(rollup, "STACK_NAME", ""):
            assert rollup._discover_control_plane_lambdas() == []


@pytest.mark.unit
class TestComponentMapping:
    """Component labels drive the dashboard's drill-down grouping. A
    wrong label makes the row appear under the wrong bucket, not the
    wrong cost total — mild but user-visible."""

    def test_monitoring_dashboard(self, rollup):
        assert (
            rollup._component_for_function("MonitoringMetricsServiceFn")
            == "monitor-dashboard"
        )

    def test_monitor_agent(self, rollup):
        assert (
            rollup._component_for_function("ScheduledMonitorAgentLambda")
            == "monitor-agent"
        )

    def test_test_set_mgmt(self, rollup):
        assert (
            rollup._component_for_function("TestSetResolverFunction") == "test-set-mgmt"
        )

    def test_test_runner(self, rollup):
        assert rollup._component_for_function("TestRunnerFunction") == "test-runner"

    def test_test_file_copier(self, rollup):
        """Regression: ``TestFileCopierFunction`` used to fall through to
        ``other-control`` because the heuristic only matched ``filecopy``
        (missing the 'ier' variant). Now covered explicitly."""
        assert rollup._component_for_function("TestFileCopierFunction") == "test-runner"

    def test_doc_chat_maps_correctly(self, rollup):
        """User chat with a specific document lands under ``doc-chat``,
        separate from the analytics-agent chat (which is SQL-driven)."""
        assert (
            rollup._component_for_function("ChatWithDocumentProcessorFunction")
            == "doc-chat"
        )
        assert (
            rollup._component_for_function("ChatStreamProcessorFunction") == "doc-chat"
        )

    def test_user_mgmt_maps_correctly(self, rollup):
        assert rollup._component_for_function("UserManagementFunction") == "user-mgmt"
        assert rollup._component_for_function("UserSyncFunction") == "user-mgmt"

    def test_api_dispatch_maps_correctly(self, rollup):
        """Every UI page load hits these — high-volume, worth breaking out."""
        assert rollup._component_for_function("LookupFunction") == "api-dispatch"
        assert rollup._component_for_function("ApiHandlerFunction") == "api-dispatch"
        assert (
            rollup._component_for_function("HttpApiDispatcherFunction")
            == "api-dispatch"
        )

    def test_agent_processor_folds_into_analytics_agent(self, rollup):
        assert (
            rollup._component_for_function("AgentProcessorFunction")
            == "analytics-agent"
        )

    def test_agentcore_lambdas_get_their_own_bucket(self, rollup):
        """AgentCore (MCP-based agent runtime) must NOT fall through to
        ``other-control``. Rule is placed before the analytics-agent
        rule so ``AgentCoreMCPHandler`` / ``AgentCoreGatewayManager``
        match ``agentcore`` and not the broader agent patterns (which
        they wouldn't match anyway, but the ordering guards against a
        future rule change)."""
        assert (
            rollup._component_for_function("AgentCoreMCPHandlerFunction")
            == "agent-core"
        )
        assert (
            rollup._component_for_function("AgentCoreGatewayManagerFunction")
            == "agent-core"
        )

    def test_blueprint_optimization(self, rollup):
        assert (
            rollup._component_for_function("BlueprintOptimizationFunction")
            == "blueprint-optimization"
        )

    def test_circuit_breaker(self, rollup):
        assert (
            rollup._component_for_function("CircuitBreakerManagerFunction")
            == "circuit-breaker"
        )

    def test_version_check(self, rollup):
        assert (
            rollup._component_for_function("VersionCheckResolverFunction")
            == "version-check"
        )

    def test_config_mgmt(self, rollup):
        assert rollup._component_for_function("ConfigResolverFunction") == "config-mgmt"

    def test_rollup_self(self, rollup):
        assert (
            rollup._component_for_function("DataMartRollupFunction") == "rollup-lambda"
        )

    def test_unknown_falls_back_to_other_control(self, rollup):
        assert rollup._component_for_function("SomeNewFeatureLambda") == "other-control"

    # Round-24 UI polish: data-plane Lambdas got component labels so
    # data_plane_lambda_hourly rows aren't all "other-control". The
    # CFN-generated names look like ``PATTERNSTACK-2UBGW8-OCRFunction-xxxx``,
    # so the rules match on the embedded stage name.
    def test_data_plane_ocr_labeled_correctly(self, rollup):
        assert (
            rollup._component_for_function(
                "idp-dev-qs1-PATTERNSTACK-2UBGW8A18HIT-OCRFunction-Gy5XmlBGm6Pw"
            )
            == "ocr"
        )

    def test_data_plane_extraction_labeled_correctly(self, rollup):
        assert (
            rollup._component_for_function(
                "idp-dev-qs1-PATTERNSTACK-2UBGW8-ExtractionFunction-HT9q3aE71EHQ"
            )
            == "extraction"
        )

    def test_data_plane_workflow_tracker_labeled_correctly(self, rollup):
        assert (
            rollup._component_for_function("idp-dev-qs1-WorkflowTracker-jZQqRb0JT5pj")
            == "workflow-tracker"
        )

    def test_data_plane_bda_labeled_correctly(self, rollup):
        """Every BDA Lambda labels as ``bda``, and a function whose name merely
        contains the substring ``bda`` (as ``lambda`` does) must NOT.

        The rules name each BDA Lambda explicitly rather than relying on a
        ``(^|[^a-z])bda`` word boundary. That boundary was added in round-24 to
        stop ``lambda`` matching, but it also rejected ``InvokeBDAFunction`` —
        lowercased, ``invoke*bda*function`` has the letter ``e`` before ``bda``
        — so the BDA-mode invoke Lambda fell through to ``other-control``.
        """
        for logical_id in (
            "InvokeBDAFunction",
            "BDAProcessResultsFunction",
            "BDACompletionFunction",
            "BDAOCRProjectFunction",
        ):
            name = f"idp-dev-qs1-PATTERNSTACK-2UB-{logical_id}-oMD38Jhsv6C0"
            assert rollup._component_for_function(name) == "bda", logical_id
        # Regression pin for the failed round-24 attempt where a bare
        # ``bda`` substring matched ``lambda`` and everything unlabeled
        # got labeled as "bda".
        assert rollup._component_for_function("SomeNewFeatureLambda") != "bda"
        assert rollup._component_for_function("idp-dev-qs1-GetDomainLambda-x") != "bda"

    def test_bda_process_results_beats_the_process_results_stage_rule(self, rollup):
        """``BDAProcessResultsFunction`` contains ``processresultsfunction``, so
        the pipeline ``process-results`` rule would claim it if it were ordered
        first. BDA rules are placed above the pipeline stages for this reason."""
        assert (
            rollup._component_for_function(
                "idp-dev-qs1-PATTERNSTACK-2UB-BDAProcessResultsFunction-oMD38J"
            )
            == "bda"
        )
        # …while the genuine pipeline stage still resolves to process-results.
        assert (
            rollup._component_for_function(
                "idp-dev-qs1-PATTERNSTACK-2UB-ProcessResultsFunction-oMD38J"
            )
            == "process-results"
        )

    def test_rule_validation_beats_the_classification_stage_rule(self, rollup):
        """``RuleValidationPolicyClassificationFunction`` contains
        ``classificationfunction`` — the ``rulevalidation`` rule must be ordered
        above ``classificationfunction`` or it is labelled ``classification``."""
        for logical_id in (
            "RuleValidationFunction",
            "RuleValidationOrchestrationFunction",
            "RuleValidationPolicyClassificationFunction",
        ):
            name = f"idp-dev-qs1-PATTERNSTACK-2UB-{logical_id}-oMD38Jhsv6C0"
            assert rollup._component_for_function(name) == "rule-validation", logical_id
        # …while the genuine classification stage is unaffected.
        assert (
            rollup._component_for_function(
                "idp-dev-qs1-PATTERNSTACK-2UB-ClassificationFunction-oMD38J"
            )
            == "classification"
        )

    def test_remaining_ingest_lambdas_are_labeled(self, rollup):
        """The four data-plane Lambdas round-24 missed. Their
        ``data_plane_lambda_hourly`` rows used to carry
        ``component='other-control'`` — a label saying "control" inside the
        data-plane table. See
        ``scripts/tests/test_data_plane_component_labels.py`` for the full
        allowlist ↔ label invariant."""
        expected = {
            "BatchPreProcessorFunction": "batch-ingest",
            "JobTracker": "job-tracker",
            "PostProcessingDecompressor": "post-processing",
            "CompleteSectionReviewFunction": "hitl-review",
        }
        for logical_id, component in expected.items():
            name = f"idp-dev-qs1-{logical_id}-jZQqRb0JT5pj"
            assert rollup._component_for_function(name) == component, logical_id


@pytest.mark.unit
class TestControlPlaneRowBuilding:
    def test_zero_activity_yields_no_row(self, rollup):
        """A control-plane Lambda that didn't run this hour must not
        emit a row — otherwise ``control_plane_hourly`` is padded with
        zero-cost noise the dashboard has to filter."""
        rows = rollup._build_control_plane_rows(
            function_name="MyFn",
            component="monitor-dashboard",
            hour_ts=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
            metrics={"duration_ms": 0.0, "invocations": 0.0},
        )
        assert rows == []

    def test_row_no_bedrock_emits_one_null_model_row(self, rollup):
        """Component that didn't call Bedrock this hour → one row with
        bedrock_model=None capturing Lambda+Athena cost only."""
        with patch.object(
            rollup, "_get_lambda_memory_mb", return_value=(512, "x86_64")
        ):
            rows = rollup._build_control_plane_rows(
                function_name="TestRunnerFunction",
                component="test-runner",
                hour_ts=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
                metrics={
                    "duration_ms": 5000.0,
                    "invocations": 3.0,
                    "athena_bytes": 1_000_000.0,
                    "bedrock_by_model": {},
                },
            )
        assert len(rows) == 1
        r = rows[0]
        assert r["bedrock_model"] is None
        assert r["invocations"] == 3
        assert r["duration_ms_sum"] == 5000
        assert r["athena_bytes_sum"] == 1_000_000
        assert r["bedrock_tokens_in"] == 0
        assert r["bedrock_tokens_out"] == 0
        assert r["est_lambda_cost"] > 0
        assert r["est_athena_cost"] > 0
        assert r["est_bedrock_cost"] == 0.0

    def test_row_per_bedrock_model(self, rollup, monkeypatch):
        """A component that called two Bedrock models emits one row per
        model, each with the correct per-model pricing applied."""
        # Inject per-token prices matching config_library/pricing.yaml.
        monkeypatch.setattr(
            rollup,
            "_bedrock_pricing_map",
            {
                "bedrock/us.anthropic.claude-opus-4-1": {
                    "inputTokens": 15.0e-6,
                    "outputTokens": 75.0e-6,
                },
                "bedrock/us.anthropic.claude-haiku-4-5": {
                    "inputTokens": 1.0e-6,
                    "outputTokens": 5.0e-6,
                },
            },
        )
        with patch.object(
            rollup, "_get_lambda_memory_mb", return_value=(1024, "arm64")
        ):
            rows = rollup._build_control_plane_rows(
                function_name="AnalyticsAgentFn",
                component="analytics-agent",
                hour_ts=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
                metrics={
                    "duration_ms": 10_000.0,
                    "invocations": 5.0,
                    "athena_bytes": 0.0,
                    "bedrock_by_model": {
                        "us.anthropic.claude-opus-4-1": {"in": 1000, "out": 500},
                        "us.anthropic.claude-haiku-4-5": {"in": 2000, "out": 1000},
                    },
                },
            )
        assert len(rows) == 2
        by_model = {r["bedrock_model"]: r for r in rows}
        # Opus: $15/M input, $75/M output → 1000 * 15e-6 + 500 * 75e-6 = 0.0525
        opus = by_model["us.anthropic.claude-opus-4-1"]
        assert opus["bedrock_tokens_in"] == 1000
        assert opus["bedrock_tokens_out"] == 500
        expected_opus = 1000 * 15.0e-6 + 500 * 75.0e-6
        assert abs(opus["est_bedrock_cost"] - expected_opus) < 1e-9
        # Haiku 4.5: $1/M input, $5/M output
        haiku = by_model["us.anthropic.claude-haiku-4-5"]
        expected_haiku = 2000 * 1.0e-6 + 1000 * 5.0e-6
        assert abs(haiku["est_bedrock_cost"] - expected_haiku) < 1e-9

    def test_shared_columns_stamped_once_not_fanned_out_across_models(
        self, rollup, monkeypatch
    ):
        """Round-5 blocker: invocations/duration/athena_bytes/est_lambda_cost/
        est_athena_cost must be stamped on ONE row per (function, hour), not
        replicated across every per-model row. Otherwise
        ``SUM(invocations) GROUP BY function_name`` fans out by the number of
        Bedrock models the function touched — same bug class as the round-2
        sum_pages blocker.
        """
        monkeypatch.setattr(
            rollup,
            "_bedrock_pricing_map",
            {
                "bedrock/model-a": {"inputTokens": 1e-6, "outputTokens": 1e-6},
                "bedrock/model-b": {"inputTokens": 1e-6, "outputTokens": 1e-6},
                "bedrock/model-c": {"inputTokens": 1e-6, "outputTokens": 1e-6},
            },
        )
        with patch.object(
            rollup, "_get_lambda_memory_mb", return_value=(1024, "arm64")
        ):
            rows = rollup._build_control_plane_rows(
                function_name="MultiModelAgentFn",
                component="analytics-agent",
                hour_ts=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
                metrics={
                    "duration_ms": 60_000.0,
                    "invocations": 5.0,
                    "athena_bytes": 50_000_000_000,  # 50 GB → distinctive num
                    "bedrock_by_model": {
                        "model-a": {"in": 10, "out": 10},
                        "model-b": {"in": 20, "out": 20},
                        "model-c": {"in": 30, "out": 30},
                    },
                },
            )
        assert len(rows) == 3
        # SUM across models must equal the function-hour truth — NOT 3× it.
        assert sum(r["invocations"] for r in rows) == 5
        assert sum(r["duration_ms_sum"] for r in rows) == 60_000
        assert sum(r["athena_bytes_sum"] for r in rows) == 50_000_000_000
        # est_lambda_cost + est_athena_cost also non-fanned.
        total_lambda = sum(r["est_lambda_cost"] for r in rows)
        total_athena = sum(r["est_athena_cost"] for r in rows)
        # 60s * 1GB @ arm64 $1.3334e-5/GB-s + 5 * ($0.20/1M) = ~$0.0008
        assert total_lambda > 0
        # 50 GB / 1 TB (decimal, matches AWS billing) * $5 = 0.25.
        # Round-10 review fix: was using TiB (1024**4 ≈ 1.0995e12) and
        # under-counting by ~9.5%. Test now pins the corrected math.
        assert 0.24 < total_athena < 0.26
        # Every model's own per-model column carries its own value on every row.
        by_model = {r["bedrock_model"]: r for r in rows}
        assert by_model["model-a"]["bedrock_tokens_in"] == 10
        assert by_model["model-b"]["bedrock_tokens_in"] == 20
        assert by_model["model-c"]["bedrock_tokens_in"] == 30

    def test_empty_string_model_does_not_steal_shared_columns(
        self, rollup, monkeypatch
    ):
        """Round-15 review fix #5: a malformed CW dimension can emit
        ``Model=""``. That used to sort FIRST under
        ``sorted(bedrock_by_model.keys())`` and steal invocations /
        duration / athena_bytes / est_lambda_cost / est_athena_cost from
        the real model rows, so a downstream
        ``WHERE bedrock_model='us.anthropic.claude-opus-4-1'`` query
        would see 0 for all shared cols. The fix filters empty-string
        model keys before row construction; their tokens are dropped
        (lesser evil) and the real model keeps its shared columns.
        """
        monkeypatch.setattr(
            rollup,
            "_bedrock_pricing_map",
            {
                "bedrock/us.anthropic.claude-opus-4-1": {
                    "inputTokens": 15e-6,
                    "outputTokens": 75e-6,
                },
            },
        )
        with patch.object(
            rollup, "_get_lambda_memory_mb", return_value=(1024, "arm64")
        ):
            rows = rollup._build_control_plane_rows(
                function_name="AgentWithMalformedMetric",
                component="analytics-agent",
                hour_ts=datetime(2026, 8, 27, 13, 0, tzinfo=timezone.utc),
                metrics={
                    "duration_ms": 60_000.0,
                    "invocations": 5.0,
                    "athena_bytes": 0.0,
                    "bedrock_by_model": {
                        "": {"in": 999, "out": 999},  # malformed Model dim
                        "us.anthropic.claude-opus-4-1": {"in": 1000, "out": 500},
                    },
                },
            )
        # Exactly one row (empty-string model filtered), and the real
        # model carries the shared columns.
        assert len(rows) == 1
        r = rows[0]
        assert r["bedrock_model"] == "us.anthropic.claude-opus-4-1"
        assert r["invocations"] == 5  # NOT stolen by ""
        assert r["duration_ms_sum"] == 60_000
        assert r["bedrock_tokens_in"] == 1000
        assert r["bedrock_tokens_out"] == 500

    def test_bedrock_cost_1M_input_sonnet_tokens_is_3_dollars(
        self, rollup, monkeypatch
    ):
        """Regression pin for the earlier 1000× overstate.

        AWS charges $3 per MILLION Sonnet input tokens. Prices in
        ConfigurationTable are per-TOKEN (3e-6). 1M tokens × 3e-6 = $3.00.
        A prior version had the wrong unit and overstated by 1000×.
        """
        monkeypatch.setattr(
            rollup,
            "_bedrock_pricing_map",
            {
                "bedrock/us.anthropic.claude-sonnet-4-20250514": {
                    "inputTokens": 3.0e-6,
                    "outputTokens": 15.0e-6,
                }
            },
        )
        with patch.object(
            rollup, "_get_lambda_memory_mb", return_value=(1024, "arm64")
        ):
            rows = rollup._build_control_plane_rows(
                function_name="AnalyticsAgentFn",
                component="analytics-agent",
                hour_ts=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
                metrics={
                    "duration_ms": 1.0,
                    "invocations": 1.0,
                    "athena_bytes": 0.0,
                    "bedrock_by_model": {
                        "us.anthropic.claude-sonnet-4-20250514": {
                            "in": 1_000_000,
                            "out": 0,
                        },
                    },
                },
            )
        # 1_000_000 * 3e-6 = 3.00 — NOT 3000.
        assert abs(rows[0]["est_bedrock_cost"] - 3.00) < 1e-6

    def test_lambda_cost_scales_with_actual_memory_at_same_arch(self, rollup):
        """Memory contribution: 4 GB → 8× the cost of 512 MB, holding
        architecture (and per-request cost) constant."""
        with patch.object(
            rollup, "_get_lambda_memory_mb", return_value=(4096, "arm64")
        ):
            rows_4gb = rollup._build_control_plane_rows(
                function_name="BigFn",
                component="test-runner",
                hour_ts=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
                metrics={"duration_ms": 5000.0, "invocations": 1.0},
            )
        with patch.object(rollup, "_get_lambda_memory_mb", return_value=(512, "arm64")):
            rows_512 = rollup._build_control_plane_rows(
                function_name="SmallFn",
                component="test-runner",
                hour_ts=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
                metrics={"duration_ms": 5000.0, "invocations": 1.0},
            )
        # Ratio isn't exactly 8 because the per-request cost is a fixed
        # additive term that doesn't scale with memory. Loosening the
        # tolerance to ±0.1 keeps the intent — 4 GB is materially more
        # than 512 MB — while accounting for the constant request term.
        assert (
            abs(rows_4gb[0]["est_lambda_cost"] / rows_512[0]["est_lambda_cost"] - 8)
            < 0.1
        )

    def test_x86_64_priced_higher_than_arm64_at_same_memory(self, rollup):
        """Same memory, same duration, different arch → x86_64 is
        ~25% more per GB-second. Confirms the arch dim isn't ignored."""
        with patch.object(
            rollup, "_get_lambda_memory_mb", return_value=(1024, "arm64")
        ):
            rows_arm = rollup._build_control_plane_rows(
                function_name="ArmFn",
                component="test-runner",
                hour_ts=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
                metrics={"duration_ms": 5000.0, "invocations": 1.0},
            )
        with patch.object(
            rollup, "_get_lambda_memory_mb", return_value=(1024, "x86_64")
        ):
            rows_x86 = rollup._build_control_plane_rows(
                function_name="X86Fn",
                component="test-runner",
                hour_ts=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
                metrics={"duration_ms": 5000.0, "invocations": 1.0},
            )
        ratio = rows_x86[0]["est_lambda_cost"] / rows_arm[0]["est_lambda_cost"]
        # 0.0000166667 / 0.0000133334 ≈ 1.25 (per-request cost is tiny).
        assert 1.20 < ratio < 1.30

    def test_lambda_cost_includes_per_request_price(self, rollup):
        """Per-request cost ($0.20/1M) must be added — previously omitted
        entirely, undercounting by ~20% for high-invocation, short-duration
        Lambdas (LookupFunction: 100+ req/hour, <100ms each)."""
        with patch.object(
            rollup, "_get_lambda_memory_mb", return_value=(128, "x86_64")
        ):
            rows = rollup._build_control_plane_rows(
                function_name="ManyInvokesFn",
                component="api-dispatch",
                hour_ts=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
                metrics={
                    "duration_ms": 0.001,  # essentially zero duration
                    "invocations": 1_000_000.0,
                },
            )
        # 1M invocations × $0.20/1M = $0.20 request cost dominates.
        assert rows[0]["est_lambda_cost"] >= 0.20 - 0.001

    def test_flatten_cw_response_missing_values(self, rollup):
        """CloudWatch returns an empty Values list when there's no data
        for the query. We treat missing as 0.0 — the alternative
        (raising) would fail rollups for any Lambda that idled all hour."""
        response = {
            "MetricDataResults": [
                {"Id": "duration", "Values": [1234.5]},
                {"Id": "invocations", "Values": []},
                {"Id": "athena_bytes", "Values": []},
            ]
        }
        result = rollup._flatten_cw_response(response)
        assert result["duration"] == 1234.5
        assert result["invocations"] == 0.0
        assert result["athena_bytes"] == 0.0

    def test_flatten_cw_response_filters_nan(self, rollup):
        """A NaN value slipping through would poison a downstream int()
        cast. _flatten_cw_response must drop NaN before summing."""
        response = {
            "MetricDataResults": [
                {"Id": "duration", "Values": [1000.0, float("nan"), 500.0]},
            ]
        }
        result = rollup._flatten_cw_response(response)
        assert result["duration"] == 1500.0

    def test_partition_check_reraises_on_transient_error(self, rollup):
        """A transient Athena throttle on the idempotency probe must NOT
        fall through to fail-open — that would let an INSERT run against
        an already-populated partition and permanently double-count."""
        with patch.object(
            rollup,
            "_run_athena_query_with_results",
            side_effect=RuntimeError("ThrottlingException: Rate exceeded"),
        ):
            with pytest.raises(RuntimeError, match="Throttling"):
                rollup._partition_already_written(
                    table="metering_hourly", date="2026-08-18", hour="13"
                )

    def test_partition_check_swallows_only_table_missing(self, rollup):
        """First deploy: the rollup tables exist per the CFN template
        but Athena's Glue catalog view may transiently report them as
        missing until the first partition materializes. TABLE_NOT_FOUND
        is the ONE error we treat as 'not yet written' — everything else
        propagates."""
        with patch.object(
            rollup,
            "_run_athena_query_with_results",
            side_effect=RuntimeError("TABLE_NOT_FOUND: metering_hourly does not exist"),
        ):
            assert (
                rollup._partition_already_written(
                    table="metering_hourly", date="2026-08-18", hour="13"
                )
                is False
            )

    def test_athena_query_uses_full_dim_signature_from_list_metrics(self, rollup):
        """Round-4 regression pin: CloudWatch matches metrics on their
        **full** dimension set. Emitter publishes AthenaBytesScanned with
        ``[Component, FunctionName]``; a GetMetricData with only
        ``[FunctionName]`` matches nothing and silently returns 0.

        The rollup must ListMetrics first (subset filter is fine there) to
        discover the full dim signature the emitter used, then
        GetMetricData with that exact signature. This test pins that the
        emitted 2-dim identity reaches the get_metric_data call intact.
        """
        get_captured: list = []

        emitted_dims = [
            {"Name": "Component", "Value": "analytics-agent"},
            {"Name": "FunctionName", "Value": "ChatStreamProcessorFunction"},
        ]

        def fake_list_metrics(**_kwargs):
            return {
                "Metrics": [
                    {
                        "Namespace": "IDPControlPlane",
                        "MetricName": "AthenaBytesScanned",
                        "Dimensions": emitted_dims,
                    }
                ]
            }

        def fake_get_metric_data(MetricDataQueries, **_kwargs):  # noqa: N803
            get_captured.extend(MetricDataQueries)
            return {
                "MetricDataResults": [
                    {"Id": q["Id"], "Values": [42.0]} for q in MetricDataQueries
                ]
            }

        mock_cw = MagicMock()
        mock_cw.list_metrics.side_effect = fake_list_metrics
        mock_cw.get_metric_data.side_effect = fake_get_metric_data

        with patch.object(rollup, "cloudwatch_client", mock_cw):
            result = rollup._get_cw_metrics_for_function(
                function_name="ChatStreamProcessorFunction",
                hour_start=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
                hour_end=datetime(2026, 8, 18, 14, 0, tzinfo=timezone.utc),
            )
        # Athena query must have used the FULL emitted dim signature —
        # NOT a FunctionName-only subset.
        athena_queries = [
            q
            for q in get_captured
            if q["Id"].startswith("a") and q["Id"] != "athena_bytes"
        ]
        # We might see other queries; grab all queries touching AthenaBytesScanned.
        athena_queries = [
            q
            for q in get_captured
            if q.get("MetricStat", {}).get("Metric", {}).get("MetricName")
            == "AthenaBytesScanned"
        ]
        assert athena_queries, "No AthenaBytesScanned query issued"
        for q in athena_queries:
            dims = q["MetricStat"]["Metric"]["Dimensions"]
            names = {d["Name"] for d in dims}
            assert names == {"Component", "FunctionName"}, (
                f"Expected [Component, FunctionName] (emitter's shape), "
                f"got {names}. Subset match returns 0 datapoints in real CloudWatch."
            )
        # And the value must have reached the caller.
        assert result["athena_bytes"] == 42.0


@pytest.mark.unit
class TestBedrockPricing:
    """Pricing is loaded from the ConfigurationTable (single source of
    truth shared with data-plane cost math). Tests inject a fake pricing
    map into the module cache to bypass the DynamoDB call.
    """

    @pytest.fixture(autouse=True)
    def _pricing_map(self, rollup, monkeypatch):
        # Prices below are per-TOKEN USD, matching config_library/pricing.yaml.
        fake = {
            "bedrock/us.anthropic.claude-opus-4-1-abc": {
                "inputTokens": 15.0e-6,
                "outputTokens": 75.0e-6,
            },
            "bedrock/us.anthropic.claude-sonnet-4-20250514": {
                "inputTokens": 3.0e-6,
                "outputTokens": 15.0e-6,
            },
            "bedrock/eu.anthropic.claude-sonnet-4-5-20250929-v1:0": {
                "inputTokens": 3.0e-6,
                "outputTokens": 15.0e-6,
            },
            "bedrock/global.anthropic.claude-opus-4-7": {
                "inputTokens": 15.0e-6,
                "outputTokens": 75.0e-6,
            },
            "bedrock/us.anthropic.claude-3-5-haiku-20241022-v1:0": {
                "inputTokens": 0.8e-6,
                "outputTokens": 4.0e-6,
            },
            "bedrock/us.amazon.nova-lite-v1:0": {
                "inputTokens": 6.0e-8,
                "outputTokens": 2.4e-7,
            },
            "bedrock/us.amazon.nova-2-lite-v1:0": {
                "inputTokens": 3.0e-7,
                "outputTokens": 2.5e-6,
            },
        }
        monkeypatch.setattr(rollup, "_bedrock_pricing_map", fake)
        yield

    def test_opus_pricing(self, rollup):
        p = rollup._bedrock_price_for_model("us.anthropic.claude-opus-4-1-abc")
        assert p["in"] == 15.0e-6
        assert p["out"] == 75.0e-6

    def test_sonnet_pricing(self, rollup):
        p = rollup._bedrock_price_for_model("us.anthropic.claude-sonnet-4-20250514")
        assert p["in"] == 3.0e-6

    def test_unknown_model_falls_back_to_zero(self, rollup):
        """Round-7 review fix: an unknown model must NOT get Sonnet
        defaults (which silently overcounts Nova-Lite by 50× and
        undercounts Opus by 5×). Instead it gets 0.0 — the missing
        row / zero cost is a clearer signal than a wrong-by-1×-to-50×
        number. An ERROR log surfaces the config gap.
        """
        p = rollup._bedrock_price_for_model("some-brand-new-model")
        assert p == {"in": 0.0, "out": 0.0}

    def test_none_model_falls_back(self, rollup):
        p = rollup._bedrock_price_for_model(None)
        assert p == rollup.DEFAULT_BEDROCK_PRICE_PER_TOKEN

    def test_eu_region_id_matches_when_config_has_it(self, rollup):
        """Config keys are the exact ``bedrock/<full-model-id>`` shape used
        by data-plane cost math — no region-prefix stripping needed here.
        The eu/global variants ARE in config_library/pricing.yaml.
        """
        p = rollup._bedrock_price_for_model(
            "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
        )
        assert p["in"] == 3.0e-6
        assert p["out"] == 15.0e-6

    def test_global_region_id_matches_when_config_has_it(self, rollup):
        p = rollup._bedrock_price_for_model("global.anthropic.claude-opus-4-7")
        assert p["in"] == 15.0e-6
        assert p["out"] == 75.0e-6

    def test_nova_lite_bare_id(self, rollup):
        """Nova Lite v1 config entry — different pricing tier than Nova-2."""
        p = rollup._bedrock_price_for_model("us.amazon.nova-lite-v1:0")
        assert p["in"] == 6.0e-8
        assert p["out"] == 2.4e-7

    def test_nova_2_lite_priced_correctly(self, rollup):
        """Regression pin for round-4 finding: Nova-2 Lite was previously
        priced as Sonnet fallback (10× over on input, 6× on output).
        """
        p = rollup._bedrock_price_for_model("us.amazon.nova-2-lite-v1:0")
        # $0.30/M input, $2.50/M output → 3.0e-7 / 2.5e-6 per token.
        assert p["in"] == 3.0e-7
        assert p["out"] == 2.5e-6


@pytest.mark.unit
class TestIsAthenaTableMissing:
    """Round-20 review fix (#269): unambiguous markers must still bind
    to the specific ``table`` argument — otherwise a probe of
    ``metering_hourly`` would false-positive on a
    ``TABLE_NOT_FOUND: metering_docs_hourly`` error for a different table.
    """

    def test_unambiguous_marker_with_matching_table_returns_true(self, rollup):
        exc = RuntimeError(
            "TABLE_NOT_FOUND: table `awsdatacatalog.reporting.metering_hourly` does not exist"
        )
        assert rollup._is_athena_table_missing(exc, "metering_hourly") is True

    def test_unambiguous_marker_with_different_table_returns_false(self, rollup):
        """Regression pin for the round-20 finding: a
        TABLE_NOT_FOUND error about ``metering_docs_hourly`` must NOT
        satisfy a probe of ``metering_hourly``.
        """
        exc = RuntimeError("TABLE_NOT_FOUND: metering_docs_hourly does not exist")
        assert rollup._is_athena_table_missing(exc, "metering_hourly") is False

    def test_unambiguous_marker_no_table_arg_returns_true(self, rollup):
        """Bare shape probe (no table binding requested) still matches."""
        exc = RuntimeError("EntityNotFoundException: something")
        assert rollup._is_athena_table_missing(exc, None) is True

    def test_column_not_found_does_not_false_positive(self, rollup):
        """A ``does not exist`` error about a COLUMN must not satisfy
        a table-binding probe.
        """
        exc = RuntimeError("Column 'foo' does not exist in table 'metering_hourly'")
        # No unambiguous marker; only the bound-to-table forms match.
        assert rollup._is_athena_table_missing(exc, "metering_hourly") is False


@pytest.mark.unit
class TestCachedFailureRestart:
    """Round-20 review fixes for the cached-failure detection path
    (#1937 probe-swallow, #1949 dropped-token)."""

    def test_probe_transient_error_defaults_to_restart(self, rollup):
        """Round-20 fix (#1937): if the probe itself fails 3× in a row,
        we can't determine whether the returned QID is a cached failure
        — defaulting to trust would loop us right back into the
        cached-failure trap the round-19 fix was meant to break. Fix:
        default to RESTART on probe exhaustion.
        """
        from botocore.exceptions import ClientError

        athena = MagicMock()
        athena.start_query_execution.side_effect = [
            {"QueryExecutionId": "cached-failed-qid"},
            {"QueryExecutionId": "fresh-qid"},
        ]
        athena.get_query_execution.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException"}}, "GetQueryExecution"
        )
        with (
            patch.object(rollup, "athena_client", athena),
            patch.object(rollup, "_wait_for_athena"),
            patch.object(rollup, "time"),
        ):
            qid = rollup._run_athena(
                "SELECT 1",
                idempotency_key="idp-rollup-test-daily-2026-08-27" + "-pad" * 3,
            )
        # We MUST have restarted (2 start_query_execution calls).
        assert athena.start_query_execution.call_count == 2
        assert qid == "fresh-qid"

    def test_probe_exhausted_with_s3_manifest_skips_restart(self, rollup):
        """Design-safe patch for reviewer finding #7 (data_mart_rollup:
        probe-exhaustion + cached-success double-write). When
        ``get_query_execution`` is throttled for all 3 probe attempts,
        the previous default-to-FAILED behavior would restart a query
        that may have already succeeded → permanent 2× rows in the
        partition. The design-safe fix uses an independent success
        signal — Athena writes ``<query_id>-manifest.txt`` to the
        OutputLocation ONLY on INSERT INTO success. If manifest exists,
        we skip the restart (and round-23's cached_success suppresses
        the AthenaBytesScanned re-emit).
        """
        from botocore.exceptions import ClientError

        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "orig-qid"}
        athena.get_query_execution.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException"}}, "GetQueryExecution"
        )
        s3 = MagicMock()
        s3.head_object.return_value = {"ContentLength": 42}  # manifest present
        with (
            patch.object(rollup, "athena_client", athena),
            patch.object(rollup, "s3_client", s3),
            patch.object(rollup, "QUERY_OUTPUT_LOCATION", "s3://b/prefix/"),
            patch.object(rollup, "_wait_for_athena") as wait_mock,
            patch.object(rollup, "time"),
        ):
            qid = rollup._run_athena(
                "SELECT 1",
                idempotency_key="idp-rollup-test-daily-2026-08-30" + "-pad" * 3,
            )
        # Only one start_query_execution — restart branch NOT taken.
        assert athena.start_query_execution.call_count == 1
        assert qid == "orig-qid"
        # Manifest lookup happened at the expected key.
        s3.head_object.assert_called_once_with(
            Bucket="b", Key="prefix/orig-qid-manifest.txt"
        )
        # cached_success → _wait_for_athena called with emit_self_cost=False
        # so AthenaBytesScanned does NOT double-emit.
        wait_mock.assert_called_once()
        assert wait_mock.call_args.kwargs.get("emit_self_cost") is False

    def test_probe_exhausted_without_s3_manifest_still_restarts(self, rollup):
        """The manifest probe MUST fail-safe: if the key is not present
        (query failed, or is still running, or never wrote a manifest),
        we default to the pre-existing restart branch — same behavior
        as before this fix. This guards against the manifest-missing
        case regressing the round-19/20 cached-failure escape."""
        from botocore.exceptions import ClientError

        athena = MagicMock()
        athena.start_query_execution.side_effect = [
            {"QueryExecutionId": "cached-failed-qid"},
            {"QueryExecutionId": "fresh-qid"},
        ]
        athena.get_query_execution.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException"}}, "GetQueryExecution"
        )
        s3 = MagicMock()
        s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404"}}, "HeadObject"
        )
        with (
            patch.object(rollup, "athena_client", athena),
            patch.object(rollup, "s3_client", s3),
            patch.object(rollup, "QUERY_OUTPUT_LOCATION", "s3://b/prefix/"),
            patch.object(rollup, "_wait_for_athena"),
            patch.object(rollup, "time"),
        ):
            qid = rollup._run_athena(
                "SELECT 1",
                idempotency_key="idp-rollup-test-daily-2026-08-30" + "-pad" * 3,
            )
        # Restart DID fire (2 start_query_execution calls).
        assert athena.start_query_execution.call_count == 2
        assert qid == "fresh-qid"

    def test_probe_exhausted_with_s3_error_defaults_to_restart(self, rollup):
        """Any error from ``s3_client.head_object`` (throttle, timeout,
        AccessDenied, transient network) must be treated as "no positive
        confirmation" — the manifest probe fails-safe to the restart
        branch. NEVER fail-open, because a spurious "SUCCEEDED" would
        suppress the restart of a genuinely-cached-failure query and
        lose that partition's data permanently."""
        from botocore.exceptions import BotoCoreError, ClientError

        athena = MagicMock()
        athena.start_query_execution.side_effect = [
            {"QueryExecutionId": "cached-failed-qid"},
            {"QueryExecutionId": "fresh-qid"},
        ]
        athena.get_query_execution.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException"}}, "GetQueryExecution"
        )
        s3 = MagicMock()
        s3.head_object.side_effect = BotoCoreError()
        with (
            patch.object(rollup, "athena_client", athena),
            patch.object(rollup, "s3_client", s3),
            patch.object(rollup, "QUERY_OUTPUT_LOCATION", "s3://b/prefix/"),
            patch.object(rollup, "_wait_for_athena"),
            patch.object(rollup, "time"),
        ):
            qid = rollup._run_athena(
                "SELECT 1",
                idempotency_key="idp-rollup-test-daily-2026-08-30" + "-pad" * 3,
            )
        # Fail-safe: restart fires despite the S3 error.
        assert athena.start_query_execution.call_count == 2
        assert qid == "fresh-qid"

    def test_cached_failure_restart_uses_fresh_token_not_none(self, rollup):
        """Round-20 fix (#1949): on cached-failure restart, DON'T drop
        the ClientRequestToken entirely — that forfeits Athena's dedup
        for the logical write and enables double-INSERT under Lambda
        hard-timeout race. Instead, append a fresh salt so Athena
        treats it as a new logical write while still deduping any
        concurrent retry of THIS attempt.
        """
        athena = MagicMock()
        athena.start_query_execution.side_effect = [
            {"QueryExecutionId": "cached-failed-qid"},
            {"QueryExecutionId": "fresh-qid"},
        ]
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "FAILED"}}
        }
        with (
            patch.object(rollup, "athena_client", athena),
            patch.object(rollup, "_wait_for_athena"),
        ):
            rollup._run_athena(
                "SELECT 1",
                idempotency_key="idp-rollup-test-metering_hourly-2026-08-27-13",
            )
        # First call had the ORIGINAL token; second call MUST have a token,
        # not None, and it MUST be different from the first.
        first_kwargs = athena.start_query_execution.call_args_list[0].kwargs
        second_kwargs = athena.start_query_execution.call_args_list[1].kwargs
        assert "ClientRequestToken" in first_kwargs
        assert "ClientRequestToken" in second_kwargs, (
            "restart MUST preserve dedup — round-20 finding #1949"
        )
        assert first_kwargs["ClientRequestToken"] != second_kwargs["ClientRequestToken"]


@pytest.mark.unit
class TestPricingUnavailableRaises:
    """Round-20 review fix (#1720): when the pricing map failed to
    load AND control-plane rows have bedrock activity, the rollup MUST
    raise so async retry replays with a fresh pricing load — otherwise
    the S3 idempotency skip locks est_bedrock_cost=0 for the hour
    forever.
    """

    def test_raises_when_pricing_unavailable_and_bedrock_rows_present(self, rollup):
        arns = ["arn:aws:lambda:us-east-1:1:function:AgentFn"]

        def cw_side_effect(function_name, hour_start, hour_end):  # noqa: ARG001
            return {
                "duration_ms": 100.0,
                "invocations": 1.0,
                "bedrock_by_model": {"anthropic.claude-opus": {"in": 1, "out": 1}},
            }

        with (
            patch.object(rollup, "_s3_object_exists", return_value=False),
            patch.object(rollup, "_discover_control_plane_lambdas", return_value=arns),
            patch.object(
                rollup, "_get_cw_metrics_for_function", side_effect=cw_side_effect
            ),
            patch.object(rollup, "_get_lambda_memory_mb", return_value=(512, "x86_64")),
            # Simulate pricing-load failure.
            patch.object(rollup, "_load_bedrock_pricing_from_config", return_value={}),
        ):
            # Trip the unavailable flag directly (production sets it inside
            # _load_bedrock_pricing_from_config on the failure/empty path).
            rollup._bedrock_pricing_unavailable = True
            with pytest.raises(RuntimeError, match="pricing map was unavailable"):
                rollup._rollup_control_plane_hourly("2026-08-27", "13")

    def test_writes_when_pricing_unavailable_but_no_bedrock_activity(self, rollup):
        """Rows without bedrock activity — empty pricing is harmless
        (nothing to price), rollup should still write.
        """
        arns = ["arn:aws:lambda:us-east-1:1:function:NonBedrockFn"]

        def cw_side_effect(function_name, hour_start, hour_end):  # noqa: ARG001
            return {
                "duration_ms": 100.0,
                "invocations": 1.0,
                "bedrock_by_model": {},  # no bedrock calls
            }

        with (
            patch.object(rollup, "_s3_object_exists", return_value=False),
            patch.object(rollup, "_discover_control_plane_lambdas", return_value=arns),
            patch.object(
                rollup, "_get_cw_metrics_for_function", side_effect=cw_side_effect
            ),
            patch.object(rollup, "_get_lambda_memory_mb", return_value=(512, "x86_64")),
            patch.object(rollup, "_load_bedrock_pricing_from_config", return_value={}),
            patch.object(rollup, "_write_parquet") as write,
        ):
            rollup._bedrock_pricing_unavailable = True
            result = rollup._rollup_control_plane_hourly("2026-08-27", "13")
        write.assert_called_once()
        assert result["skipped"] is False


@pytest.mark.unit
class TestControlPlaneRollupFanOut:
    """Round-14 review fixes — total-outage detection and stable row order."""

    def test_total_fetch_failure_raises_instead_of_locking_empty_partition(
        self, rollup
    ):
        """Round-14 finding #1: when EVERY per-function CloudWatch fetch
        fails, ``rows`` stays empty and the old ``no_activity`` skip path
        masked the total outage as a legitimate zero-activity hour. The
        idempotency guard would then lock the partition forever and no
        async retry / DLQ ever fires. Fix: raise so Lambda's retry policy
        replays the hour and the DLQ alarm eventually pages oncall.
        """
        arns = [
            "arn:aws:lambda:us-east-1:1:function:FnA",
            "arn:aws:lambda:us-east-1:1:function:FnB",
        ]
        with (
            patch.object(rollup, "_s3_object_exists", return_value=False),
            patch.object(rollup, "_discover_control_plane_lambdas", return_value=arns),
            patch.object(
                rollup,
                "_get_cw_metrics_for_function",
                side_effect=RuntimeError("CW throttled"),
            ),
            patch.object(rollup, "_write_parquet") as write,
        ):
            with pytest.raises(RuntimeError, match="all .* function fetches failed"):
                rollup._rollup_control_plane_hourly("2026-08-27", "13")
        write.assert_not_called()

    def test_partial_fetch_failure_still_writes_surviving_rows(self, rollup):
        """A single flaky function must NOT tank the whole partition —
        the survivor rows still write to parquet."""
        arns = [
            "arn:aws:lambda:us-east-1:1:function:FnGood",
            "arn:aws:lambda:us-east-1:1:function:FnBad",
        ]

        def cw_side_effect(function_name, hour_start, hour_end):  # noqa: ARG001
            if function_name == "FnBad":
                raise RuntimeError("throttled")
            return {"duration_ms": 100.0, "invocations": 1.0, "bedrock_by_model": {}}

        with (
            patch.object(rollup, "_s3_object_exists", return_value=False),
            patch.object(rollup, "_discover_control_plane_lambdas", return_value=arns),
            patch.object(
                rollup,
                "_get_cw_metrics_for_function",
                side_effect=cw_side_effect,
            ),
            patch.object(rollup, "_get_lambda_memory_mb", return_value=(512, "x86_64")),
            patch.object(rollup, "_write_parquet") as write,
        ):
            result = rollup._rollup_control_plane_hourly("2026-08-27", "13")
        assert result["skipped"] is False
        assert result["rows"] >= 1
        write.assert_called_once()
        # Written rows come from FnGood only.
        written_rows = write.call_args[0][0]
        assert all(r["function_name"] == "FnGood" for r in written_rows)

    def test_rows_are_sorted_by_function_name_then_bedrock_model(self, rollup):
        """Round-14 finding #2: sort must key on ``bedrock_model``, not the
        non-existent ``model`` field. A wrong key silently collapsed the
        sort to function-name-only, hiding a future divergence in
        ``_build_control_plane_rows`` iteration order that would break
        the round-8 shared-columns-on-first-model invariant.
        """
        arns = [
            "arn:aws:lambda:us-east-1:1:function:ZFunction",
            "arn:aws:lambda:us-east-1:1:function:AFunction",
        ]

        def cw_side_effect(function_name, hour_start, hour_end):  # noqa: ARG001
            return {
                "duration_ms": 100.0,
                "invocations": 1.0,
                "bedrock_by_model": {
                    "zz.model": {"in": 1, "out": 1},
                    "aa.model": {"in": 1, "out": 1},
                },
            }

        with (
            patch.object(rollup, "_s3_object_exists", return_value=False),
            patch.object(rollup, "_discover_control_plane_lambdas", return_value=arns),
            patch.object(
                rollup,
                "_get_cw_metrics_for_function",
                side_effect=cw_side_effect,
            ),
            patch.object(rollup, "_get_lambda_memory_mb", return_value=(512, "x86_64")),
            patch.object(
                rollup,
                "_bedrock_pricing_map",
                {"bedrock/zz.model": {"inputTokens": 0.0, "outputTokens": 0.0}},
            ),
            patch.object(rollup, "_write_parquet") as write,
        ):
            rollup._rollup_control_plane_hourly("2026-08-27", "13")
        written_rows = write.call_args[0][0]
        # AFunction rows come before ZFunction; within each function
        # bedrock_model=aa.model precedes zz.model.
        ordered = [(r["function_name"], r["bedrock_model"]) for r in written_rows]
        assert ordered == sorted(ordered)
        assert ordered[0][0] == "AFunction"
        assert ordered[-1][0] == "ZFunction"


@pytest.mark.unit
class TestDataPlaneLambdaRollup:
    """Round-23 review coverage for the round-22 additions: the new
    ``data_plane_lambda_hourly`` rollup and its sibling helpers.
    """

    def test_discover_data_plane_lambdas_uses_data_tag(self, rollup):
        with (
            patch.object(rollup, "_enumerate_stack_tree", return_value=["root"]),
            patch.object(
                rollup, "_get_resources_by_tag", return_value=["arn:...:function:X"]
            ) as gr,
        ):
            arns = rollup._discover_data_plane_lambdas()
        # The tag filter MUST include idp:plane=data and be scoped to
        # the discovered stack tree.
        gr.assert_called_once()
        (tags,) = gr.call_args.args
        assert tags == {
            "aws:cloudformation:stack-name": ["root"],
            "idp:plane": ["data"],
        }
        assert arns == ["arn:...:function:X"]

    def test_data_plane_rollup_skips_when_partition_exists(self, rollup):
        with (
            patch.object(rollup, "_s3_object_exists", return_value=True),
            patch.object(rollup, "_write_parquet") as write,
        ):
            result = rollup._rollup_data_plane_lambda_hourly("2026-08-27", "13")
        assert result == {"skipped": True, "reason": "partition_exists"}
        write.assert_not_called()

    def test_data_plane_rollup_writes_expected_row_shape(self, rollup):
        """A single active Lambda produces one Lambda-only row (no
        bedrock_model, no athena_bytes_sum, no bedrock_tokens_*, no
        est_bedrock_cost / est_athena_cost)."""
        arns = ["arn:aws:lambda:us-east-1:1:function:OCRFunction"]

        cw = MagicMock()
        # Duration + Invocations for one hour.
        cw.get_metric_data.return_value = {
            "MetricDataResults": [
                {"Id": "d", "Values": [12000.0]},
                {"Id": "i", "Values": [3.0]},
            ]
        }
        with (
            patch.object(rollup, "_s3_object_exists", return_value=False),
            patch.object(rollup, "_discover_data_plane_lambdas", return_value=arns),
            patch.object(rollup, "cloudwatch_client", cw),
            patch.object(rollup, "_get_lambda_memory_mb", return_value=(1024, "arm64")),
            patch.object(rollup, "_write_parquet") as write,
        ):
            result = rollup._rollup_data_plane_lambda_hourly("2026-08-27", "13")
        assert result["skipped"] is False
        assert result["rows"] == 1
        # Verify _write_parquet was called with the data_plane_lambda schema.
        write.assert_called_once()
        _, kwargs = write.call_args
        assert kwargs.get("schema_name") == "data_plane_lambda"
        written_rows = write.call_args[0][0]
        assert len(written_rows) == 1
        r = written_rows[0]
        # Only Lambda-only columns present. Bedrock/Athena keys forbidden
        # so a future accidental schema drift is caught at test time.
        assert set(r.keys()) == {
            "hour_ts",
            "function_name",
            "component",
            "invocations",
            "duration_ms_sum",
            "est_lambda_cost",
        }
        assert r["function_name"] == "OCRFunction"
        assert r["invocations"] == 3
        assert r["duration_ms_sum"] == 12000
        # 12s at 1GB arm64 → 12 * 1.3334e-5 = ~$0.00016 + 3 * $0.0000002 ≈ $0.00016
        assert r["est_lambda_cost"] > 0

    def test_data_plane_rollup_drops_zero_activity_rows(self, rollup):
        arns = [
            "arn:aws:lambda:us-east-1:1:function:IdleFn",
            "arn:aws:lambda:us-east-1:1:function:ActiveFn",
        ]
        cw = MagicMock()

        def cw_side_effect(**kwargs):
            # queries=[Duration(Id=d), Invocations(Id=i)]; return zeros for
            # IdleFn, real numbers for ActiveFn (identified via dim).
            fn = kwargs["MetricDataQueries"][0]["MetricStat"]["Metric"]["Dimensions"][
                0
            ]["Value"]
            if fn == "IdleFn":
                return {
                    "MetricDataResults": [
                        {"Id": "d", "Values": []},
                        {"Id": "i", "Values": []},
                    ]
                }
            return {
                "MetricDataResults": [
                    {"Id": "d", "Values": [100.0]},
                    {"Id": "i", "Values": [1.0]},
                ]
            }

        cw.get_metric_data.side_effect = cw_side_effect
        with (
            patch.object(rollup, "_s3_object_exists", return_value=False),
            patch.object(rollup, "_discover_data_plane_lambdas", return_value=arns),
            patch.object(rollup, "cloudwatch_client", cw),
            patch.object(rollup, "_get_lambda_memory_mb", return_value=(512, "x86_64")),
            patch.object(rollup, "_write_parquet") as write,
        ):
            result = rollup._rollup_data_plane_lambda_hourly("2026-08-27", "13")
        assert result["skipped"] is False
        # IdleFn dropped, ActiveFn written.
        written_rows = write.call_args[0][0]
        assert [r["function_name"] for r in written_rows] == ["ActiveFn"]

    def test_data_plane_rollup_no_lambdas_soft_skips(self, rollup):
        """Fresh install has no data-plane Lambdas yet — must NOT raise."""
        with (
            patch.object(rollup, "_s3_object_exists", return_value=False),
            patch.object(rollup, "_discover_data_plane_lambdas", return_value=[]),
        ):
            result = rollup._rollup_data_plane_lambda_hourly("2026-08-27", "13")
        assert result == {"skipped": True, "reason": "no_data_lambdas"}

    def test_data_plane_rollup_total_outage_raises(self, rollup):
        """Round-14 pattern: if EVERY function's fetch fails, refuse to
        write an empty partition — S3 idempotency would lock the hour
        into a permanent hole. Async retry must replay."""
        arns = ["arn:aws:lambda:us-east-1:1:function:FnA"]
        cw = MagicMock()
        cw.get_metric_data.side_effect = RuntimeError("CW throttled")
        with (
            patch.object(rollup, "_s3_object_exists", return_value=False),
            patch.object(rollup, "_discover_data_plane_lambdas", return_value=arns),
            patch.object(rollup, "cloudwatch_client", cw),
        ):
            with pytest.raises(RuntimeError, match="all .* function fetches failed"):
                rollup._rollup_data_plane_lambda_hourly("2026-08-27", "13")

    def test_write_parquet_data_plane_schema_dispatch(self, rollup):
        """The ``schema_name='data_plane_lambda'`` dispatch was
        added in this commit; verify it uses the minimal Lambda-only
        schema (round-22 audit caught the missing dispatch that would
        have silently applied the control-plane schema to data-plane
        rows, filling Bedrock/Athena columns with null junk)."""
        import io as _io

        import pyarrow.parquet as _pq

        rows = [
            {
                "hour_ts": datetime(2026, 8, 27, 13, 0, tzinfo=timezone.utc),
                "function_name": "OCRFunction",
                "component": "ocr",
                "invocations": 5,
                "duration_ms_sum": 50000,
                "est_lambda_cost": 0.001,
            }
        ]
        s3 = MagicMock()
        with (
            patch.object(rollup, "_s3_object_exists", return_value=False),
            patch.object(rollup, "s3_client", s3),
        ):
            rollup._write_parquet(rows, "test-key", schema_name="data_plane_lambda")
        # Read back the uploaded bytes and inspect the schema.
        put_kwargs = s3.put_object.call_args.kwargs
        body = put_kwargs["Body"]
        pf = _pq.ParquetFile(_io.BytesIO(body))
        col_names = set(pf.schema_arrow.names)
        # Must NOT contain control-plane-only columns.
        for forbidden in (
            "bedrock_model",
            "athena_bytes_sum",
            "bedrock_tokens_in",
            "bedrock_tokens_out",
            "est_athena_cost",
            "est_bedrock_cost",
        ):
            assert forbidden not in col_names, (
                f"data_plane_lambda schema leaked control-plane column: {forbidden}"
            )
        assert col_names == {
            "hour_ts",
            "function_name",
            "component",
            "invocations",
            "duration_ms_sum",
            "est_lambda_cost",
        }

    def test_write_parquet_rejects_unknown_schema_name(self, rollup):
        with (
            patch.object(rollup, "_s3_object_exists", return_value=False),
            patch.object(rollup, "s3_client", MagicMock()),
        ):
            with pytest.raises(ValueError, match="unknown schema_name"):
                rollup._write_parquet([], "test-key", schema_name="bogus")


# ---------------------------------------------------------------------------
# Backfill mode — repopulate rollup partitions for a caller-supplied window
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBackfillMode:
    """``mode: 'backfill'`` iterates hours in [start, end) and re-runs the
    four per-document hourly rollups. Idempotent — already-written
    partitions are no-ops via the existing HeadObject skip.
    """

    def test_backfill_mode_dispatches_to_run_backfill(self, rollup):
        with patch.object(
            rollup, "_run_backfill", return_value={"mode": "backfill"}
        ) as bf:
            result = rollup.handler(
                {
                    "mode": "backfill",
                    "start": "2026-09-01T00:00:00Z",
                    "end": "2026-09-02T00:00:00Z",
                },
                None,
            )
        bf.assert_called_once()
        assert result["mode"] == "backfill"

    def test_backfill_requires_start_and_end(self, rollup):
        with pytest.raises(ValueError, match="requires 'start' and 'end'"):
            rollup.handler({"mode": "backfill"}, None)
        with pytest.raises(ValueError, match="requires 'start' and 'end'"):
            rollup.handler({"mode": "backfill", "start": "2026-09-01T00:00:00Z"}, None)

    def test_backfill_iterates_hours_and_calls_all_four_rollups(self, rollup):
        """A 3-hour window must invoke all four hourly rollups per hour —
        12 calls total. Order matters only within an hour (all four
        run before advancing) so the accumulator's per-hour bookkeeping
        stays consistent."""
        calls = []

        def _rec(label):
            def _fn(date, hour):
                calls.append((label, date, hour))
                return {"skipped": False}

            return _fn

        with (
            patch.object(rollup, "_rollup_metering_hourly", side_effect=_rec("mh")),
            patch.object(
                rollup, "_rollup_metering_docs_hourly", side_effect=_rec("mdh")
            ),
            patch.object(
                rollup, "_rollup_control_plane_hourly", side_effect=_rec("cph")
            ),
            patch.object(
                rollup, "_rollup_data_plane_lambda_hourly", side_effect=_rec("dph")
            ),
        ):
            result = rollup._run_backfill(
                "2026-09-01T00:00:00Z", "2026-09-01T03:00:00Z"
            )
        # 3 hours × 4 rollups = 12 calls.
        assert len(calls) == 12
        # Each hour must have all four labels present.
        for hour in ("00", "01", "02"):
            hour_calls = {label for label, _d, h in calls if h == hour}
            assert hour_calls == {"mh", "mdh", "cph", "dph"}, (
                f"Hour {hour} missing rollups: {hour_calls}"
            )
        # Every call must have the correct date (no boundary bugs).
        assert all(d == "2026-09-01" for _l, d, _h in calls)
        # Result accumulator: 3 hours, all succeeded.
        assert result["hours_attempted"] == 3
        assert result["hours_succeeded"] == 3
        assert result["hours_failed"] == 0
        assert result["hours_partial"] == 0
        assert result["failures"] == []

    def test_backfill_records_partial_failures_and_continues(self, rollup):
        """If one rollup fails for one hour, the other three still write
        and the accumulator classifies the hour as 'partial'. Iteration
        continues to the next hour rather than aborting the whole
        backfill — matches the reconciler's use case, where an
        individual gap shouldn't block filling in others."""

        def _ok(_d, _h):
            return {"skipped": False}

        def _fail_first_hour_only(date, hour):
            if hour == "00":
                raise RuntimeError("transient athena hiccup")
            return {"skipped": False}

        with (
            patch.object(
                rollup, "_rollup_metering_hourly", side_effect=_fail_first_hour_only
            ),
            patch.object(rollup, "_rollup_metering_docs_hourly", side_effect=_ok),
            patch.object(rollup, "_rollup_control_plane_hourly", side_effect=_ok),
            patch.object(rollup, "_rollup_data_plane_lambda_hourly", side_effect=_ok),
        ):
            result = rollup._run_backfill(
                "2026-09-01T00:00:00Z", "2026-09-01T02:00:00Z"
            )
        assert result["hours_attempted"] == 2
        assert result["hours_succeeded"] == 1  # hour 01
        assert result["hours_partial"] == 1  # hour 00: mh failed, other 3 ok
        assert result["hours_failed"] == 0
        assert len(result["failures"]) == 1
        assert result["failures"][0]["hour"] == "00"
        assert result["failures"][0]["table"] == "metering_hourly"
        assert "transient athena hiccup" in result["failures"][0]["error"]

    def test_backfill_rejects_reversed_range(self, rollup):
        with pytest.raises(ValueError, match="strictly before"):
            rollup._run_backfill("2026-09-02T00:00:00Z", "2026-09-01T00:00:00Z")

    def test_backfill_rejects_malformed_timestamp(self, rollup):
        with pytest.raises(ValueError, match="ISO 8601"):
            rollup._run_backfill("not a timestamp", "2026-09-01T00:00:00Z")

    def test_backfill_bound_naive_datetime_treated_as_utc(self, rollup):
        """Timezone-naive ISO strings must be treated as UTC (matches the
        pipeline's queue-time convention) — not raise on the missing tz."""
        parsed = rollup._parse_backfill_bound("2026-09-01T00:00:00", "start")
        assert parsed.tzinfo is not None
        assert parsed.utcoffset().total_seconds() == 0


@pytest.mark.unit
class TestBackfillMigrateDeprecatedNoOp:
    """``mode: 'backfill_migrate'`` is deprecated — the migration moved
    to the ``DataMartMigrationStateMachine`` in 2.0. The mode is kept
    as a NO-OP so any queued async retries from the pre-SFN Lambda-only
    design succeed cleanly (rather than DLQ-firing an alarm the operator
    has to interpret). See CHANGELOG + docs/reporting-sql-layer.md."""

    def test_deprecated_backfill_migrate_returns_noop(self, rollup):
        result = rollup.handler({"mode": "backfill_migrate", "days": 30}, None)
        assert result.get("deprecated") is True
        assert "no-op" in result.get("action", "").lower()
        assert result["mode"] == "backfill_migrate"

    def test_deprecated_mode_does_not_touch_ssm_s3_or_athena(self, rollup):
        """Whatever the payload, the deprecated no-op must not modify
        any live state — an accidental invocation must not perturb
        production."""
        with (
            patch.object(rollup, "boto3", MagicMock()) as mock_boto3,
            patch.object(rollup, "_purge_s3_prefix") as purge,
            patch.object(rollup, "_run_backfill") as bf,
        ):
            rollup.handler(
                {"mode": "backfill_migrate", "days": 30, "start": "x", "end": "y"},
                None,
            )
        # No AWS SDK client instantiated for a no-op path.
        # (boto3 module mock — client() may not even be called.)
        purge.assert_not_called()
        bf.assert_not_called()
        # get_parameter / put_parameter must not have been touched either.
        # (The boto3 module is patched, so any client.get_parameter call
        # would go through mock_boto3.client(...).get_parameter().)
        client_calls = mock_boto3.client.mock_calls
        assert all("ssm" not in str(c).lower() for c in client_calls), (
            f"deprecated mode must not touch SSM; got {client_calls}"
        )


@pytest.mark.unit
class TestCheckMarkerState:
    """``mode: 'check_marker_state'`` — SFN's first task. Reads the SSM
    marker, parses it, returns a routing decision the state machine's
    Choice state consumes. Three outcomes:
      * completed matching days → short_circuit
      * in_progress matching days → skip_purge
      * absent / unrecognised / different days → full flow
    """

    def test_absent_marker_returns_full_flow_routing(self, rollup):
        with patch.object(rollup, "boto3", MagicMock()) as mock_boto3:
            ssm_mock = mock_boto3.client.return_value
            ssm_mock.exceptions.ParameterNotFound = type(
                "ParameterNotFound", (Exception,), {}
            )
            ssm_mock.get_parameter.side_effect = ssm_mock.exceptions.ParameterNotFound()
            result = rollup.handler({"mode": "check_marker_state", "days": 30}, None)
        assert result["state"] == "absent"
        assert result["should_short_circuit"] is False
        assert result["should_skip_purge"] is False

    def test_completed_marker_matching_days_returns_short_circuit(self, rollup):
        with patch.object(rollup, "boto3", MagicMock()) as mock_boto3:
            ssm_mock = mock_boto3.client.return_value
            ssm_mock.exceptions.ParameterNotFound = type(
                "ParameterNotFound", (Exception,), {}
            )
            ssm_mock.get_parameter.return_value = {
                "Parameter": {
                    "Value": "days=30;state=completed;completed_at=2026-09-04T12:00:00+00:00"
                }
            }
            result = rollup.handler({"mode": "check_marker_state", "days": 30}, None)
        assert result["state"] == "completed"
        assert result["should_short_circuit"] is True
        assert result["should_skip_purge"] is False

    def test_in_progress_marker_matching_days_returns_skip_purge(self, rollup):
        """THE KEY retry-safety routing test. A prior state-machine run
        purged and started backfill, then Lambda timed out mid-chunk.
        A restart reads the marker and MUST route to skip_purge (not
        short_circuit and not full_flow)."""
        with patch.object(rollup, "boto3", MagicMock()) as mock_boto3:
            ssm_mock = mock_boto3.client.return_value
            ssm_mock.exceptions.ParameterNotFound = type(
                "ParameterNotFound", (Exception,), {}
            )
            ssm_mock.get_parameter.return_value = {
                "Parameter": {
                    "Value": "days=30;state=in_progress;started_at=2026-09-04T12:00:00+00:00"
                }
            }
            result = rollup.handler({"mode": "check_marker_state", "days": 30}, None)
        assert result["state"] == "in_progress"
        assert result["should_short_circuit"] is False
        assert result["should_skip_purge"] is True

    def test_different_days_treated_as_fresh_migration(self, rollup):
        """Marker says days=30 completed, request is for days=45 → not
        the same migration → full flow."""
        with patch.object(rollup, "boto3", MagicMock()) as mock_boto3:
            ssm_mock = mock_boto3.client.return_value
            ssm_mock.exceptions.ParameterNotFound = type(
                "ParameterNotFound", (Exception,), {}
            )
            ssm_mock.get_parameter.return_value = {
                "Parameter": {"Value": "days=30;state=completed;completed_at=x"}
            }
            result = rollup.handler({"mode": "check_marker_state", "days": 45}, None)
        assert result["state"] == "unrecognised"
        assert result["should_short_circuit"] is False
        assert result["should_skip_purge"] is False

    def test_ssm_read_error_propagates(self, rollup):
        """A non-``ParameterNotFound`` SSM error (throttle, IAM denial,
        service outage) MUST propagate — coercing it to ``state=absent``
        routes the state machine's Choice into the destructive
        InitialPurge branch on a stack whose migration was already
        completed. Preferred behaviour: the state machine's Lambda-error
        Retry catches transient SDK errors up to 6× with backoff, and a
        persistent failure fails the execution visibly rather than
        silently destroying customer rollup data on a routine
        CustomResource re-fire."""
        with patch.object(rollup, "boto3", MagicMock()) as mock_boto3:
            ssm_mock = mock_boto3.client.return_value
            ssm_mock.exceptions.ParameterNotFound = type(
                "ParameterNotFound", (Exception,), {}
            )
            ssm_mock.get_parameter.side_effect = RuntimeError("SSM regional outage")
            with pytest.raises(RuntimeError, match="SSM regional outage"):
                rollup.handler({"mode": "check_marker_state", "days": 30}, None)


@pytest.mark.unit
class TestWriteMarker:
    """``mode: 'write_marker'`` — writes the SSM marker to either
    ``state=in_progress`` (called after purge, before backfill) or
    ``state=completed`` (called at the terminal WriteCompletedMarker
    step in the state machine)."""

    def test_write_in_progress_marker(self, rollup):
        with patch.object(rollup, "boto3", MagicMock()) as mock_boto3:
            ssm_mock = mock_boto3.client.return_value
            result = rollup.handler(
                {"mode": "write_marker", "state": "in_progress", "days": 30},
                None,
            )
        ssm_mock.put_parameter.assert_called_once()
        put_kwargs = ssm_mock.put_parameter.call_args.kwargs
        assert "state=in_progress" in put_kwargs["Value"]
        assert "days=30" in put_kwargs["Value"]
        assert put_kwargs["Overwrite"] is True
        assert result["state"] == "in_progress"
        assert result["days"] == 30
        assert "state=in_progress" in result["marker"]

    def test_write_completed_marker(self, rollup):
        with patch.object(rollup, "boto3", MagicMock()) as mock_boto3:
            ssm_mock = mock_boto3.client.return_value
            result = rollup.handler(
                {"mode": "write_marker", "state": "completed", "days": 45},
                None,
            )
        put_kwargs = ssm_mock.put_parameter.call_args.kwargs
        assert "state=completed" in put_kwargs["Value"]
        assert "days=45" in put_kwargs["Value"]
        assert result["state"] == "completed"

    def test_write_marker_rejects_invalid_state(self, rollup):
        with patch.object(rollup, "boto3", MagicMock()):
            with pytest.raises(ValueError, match="'in_progress' or 'completed'"):
                rollup.handler(
                    {"mode": "write_marker", "state": "bogus", "days": 30},
                    None,
                )


@pytest.mark.unit
class TestPurgeRollupPrefixes:
    """``mode: 'purge_rollup_prefixes'`` — Task-mode wrapper around
    ``_purge_s3_prefix`` that the state machine invokes for the initial
    purge. Deletes every S3 object under the four per-document rollup
    prefixes."""

    def test_purge_covers_all_four_prefixes(self, rollup):
        purged: list = []

        def _fake_purge(bucket, prefix):
            purged.append((bucket, prefix))
            return 7  # arbitrary — assertion is on which prefixes, not counts

        with patch.object(rollup, "_purge_s3_prefix", side_effect=_fake_purge):
            result = rollup.handler({"mode": "purge_rollup_prefixes"}, None)
        prefixes = sorted(p for _b, p in purged)
        assert prefixes == [
            "metering_daily/",
            "metering_docs_daily/",
            "metering_docs_hourly/",
            "metering_hourly/",
        ]
        assert result["total"] == 4 * 7
        assert set(result["purged"].keys()) == {
            "metering_hourly/",
            "metering_daily/",
            "metering_docs_hourly/",
            "metering_docs_daily/",
        }

    def test_purge_task_does_not_touch_control_plane_prefixes(self, rollup):
        """`control_plane/` and `data_plane_lambda/` prefixes are OUT
        of scope — they're written by different rollup arms with
        different schemas and are not affected by the document_class
        widening. Regression pin."""
        purged: list = []
        with patch.object(
            rollup,
            "_purge_s3_prefix",
            side_effect=lambda b, p: (purged.append(p), 0)[1],
        ):
            rollup.handler({"mode": "purge_rollup_prefixes"}, None)
        assert "control_plane/" not in purged
        assert "data_plane_lambda/" not in purged


@pytest.mark.unit
class TestPlanMigrationChunks:
    """``mode: 'plan_migration_chunks'`` — returns the list of
    ``(start, end)`` time ranges the state machine's Map iterates.
    Each chunk covers ``chunk_hours`` of the retention window."""

    def test_plan_produces_expected_chunk_count(self, rollup):
        result = rollup.handler(
            {
                "mode": "plan_migration_chunks",
                "days": 30,
                "chunk_hours": 24,
                "time": "2026-09-22T00:00:00Z",
            },
            None,
        )
        # 30 days × 24 h / 24 h per chunk = 30 chunks.
        assert result["count"] == 30
        assert len(result["chunks"]) == 30

    def test_plan_chunks_cover_full_window_with_no_gaps(self, rollup):
        """The Map iterator's chunks must cover [now-days, now-hour]
        contiguously with no gaps and no overlaps."""
        result = rollup.handler(
            {
                "mode": "plan_migration_chunks",
                "days": 5,
                "chunk_hours": 24,
                "time": "2026-09-22T00:00:00Z",
            },
            None,
        )
        chunks = result["chunks"]
        # Each chunk's end == next chunk's start (contiguous).
        for prev, nxt in zip(chunks, chunks[1:]):
            assert prev["end"] == nxt["start"], (
                f"chunks must be contiguous; got prev.end={prev['end']!r} "
                f"nxt.start={nxt['start']!r}"
            )

    def test_plan_partial_last_chunk_when_chunk_size_doesnt_divide(self, rollup):
        """If ``days × 24 / chunk_hours`` is not integer, the LAST chunk
        must be shorter — not overflow past the anchor."""
        result = rollup.handler(
            {
                "mode": "plan_migration_chunks",
                "days": 1,  # 24 hours total
                "chunk_hours": 10,  # 3 chunks: 10 + 10 + 4
                "time": "2026-09-22T00:00:00Z",
            },
            None,
        )
        assert result["count"] == 3
        # Anchor is 2026-09-22T00:00:00Z, window is [now-1d, now] =
        # [2026-09-21T00, 2026-09-22T00]. Chunks (in order):
        assert result["chunks"][0]["start"].startswith("2026-09-21T00:00:00")
        assert result["chunks"][-1]["end"].startswith("2026-09-22T00:00:00")

    def test_plan_rejects_out_of_range_days(self, rollup):
        for bad in (0, -1, 91, 365):
            with pytest.raises(ValueError, match="out of range"):
                rollup.handler({"mode": "plan_migration_chunks", "days": bad}, None)

    def test_plan_rejects_out_of_range_chunk_hours(self, rollup):
        for bad in (0, -1, 169, 1000):
            with pytest.raises(ValueError, match="chunk_hours=.*out of range"):
                rollup.handler(
                    {
                        "mode": "plan_migration_chunks",
                        "days": 30,
                        "chunk_hours": bad,
                    },
                    None,
                )


@pytest.mark.unit
class TestCheckHoursFailed:
    """``mode: 'check_hours_failed'`` — aggregates the Map state's
    chunk results. Returns all_hours_clean=True only if EVERY chunk
    reported hours_failed=0."""

    def test_all_clean_chunks_returns_all_hours_clean(self, rollup):
        chunk_results = [
            {
                "backfill": {
                    "hours_attempted": 24,
                    "hours_succeeded": 24,
                    "hours_failed": 0,
                    "hours_partial": 0,
                }
            }
            for _ in range(30)
        ]
        result = rollup.handler(
            {"mode": "check_hours_failed", "chunk_results": chunk_results},
            None,
        )
        assert result["all_hours_clean"] is True
        assert result["total_failed"] == 0
        assert result["total_succeeded"] == 24 * 30
        assert result["failing_chunks"] == []

    def test_one_failing_chunk_marks_not_clean(self, rollup):
        """Any chunk with hours_failed > 0 → all_hours_clean=False.
        Partial-hour failures (some rollup arms wrote, some didn't for
        one hour) do NOT block completion."""
        chunk_results = [
            {"backfill": {"hours_attempted": 24, "hours_failed": 0}},
            {
                "backfill": {
                    "hours_attempted": 24,
                    "hours_failed": 2,
                    "start": "2026-08-24T00:00:00Z",
                    "end": "2026-08-25T00:00:00Z",
                    "failures": [{"hour": "13"}, {"hour": "14"}],
                }
            },
            {"backfill": {"hours_attempted": 24, "hours_failed": 0}},
        ]
        result = rollup.handler(
            {"mode": "check_hours_failed", "chunk_results": chunk_results},
            None,
        )
        assert result["all_hours_clean"] is False
        assert result["total_failed"] == 2
        assert len(result["failing_chunks"]) == 1
        assert result["failing_chunks"][0]["hours_failed"] == 2

    def test_partial_hours_block_migration_completion(self, rollup):
        """2.1.2 fix: hours_partial > 0 (some rollup arms wrote, others
        didn't) MUST block WriteCompletedMarker. Before this fix the
        aggregator only inspected hours_failed, so 38 chunks reporting
        partial (all metering arms fell on the Athena Idempotent-
        parameters error while control_plane/data_plane arms cached-
        succeeded from a prior execution) would have written a
        completed-migration marker over an empty metering_hourly table.
        The migration IS the reconciler for these tables; a partial
        chunk maps directly to customer rows silently missing."""
        chunk_results = [
            {
                "backfill": {
                    "hours_attempted": 24,
                    "hours_succeeded": 20,
                    "hours_partial": 4,
                    "hours_failed": 0,
                }
            }
        ]
        result = rollup.handler(
            {"mode": "check_hours_failed", "chunk_results": chunk_results},
            None,
        )
        assert result["all_hours_clean"] is False
        assert result["total_partial"] == 4
        assert len(result["failing_chunks"]) == 1
        assert result["failing_chunks"][0]["hours_partial"] == 4

    def test_empty_chunk_results_returns_clean(self, rollup):
        """Degenerate case — no chunks means no failures. State machine
        contract should never reach this state (0-day window is
        rejected by _plan_migration_chunks) but the aggregator must
        not blow up on the edge."""
        result = rollup.handler(
            {"mode": "check_hours_failed", "chunk_results": []},
            None,
        )
        assert result["all_hours_clean"] is True

    def test_accepts_bare_payload_or_backfill_wrapper(self, rollup):
        """Depending on the Map state's ResultSelector, the chunk
        result could arrive as {backfill: {...}} or bare {...}. The
        aggregator must handle both."""
        bare = [{"hours_attempted": 5, "hours_failed": 1}]
        wrapped = [{"backfill": {"hours_attempted": 5, "hours_failed": 1}}]
        r1 = rollup.handler({"mode": "check_hours_failed", "chunk_results": bare}, None)
        r2 = rollup.handler(
            {"mode": "check_hours_failed", "chunk_results": wrapped}, None
        )
        assert r1["total_failed"] == 1
        assert r2["total_failed"] == 1

    def test_daily_result_included_in_aggregation(self, rollup):
        """State machine passes daily_result as a separate field
        (ASL has no ArrayConcat). Aggregator must combine both."""
        chunk_results = [
            {"backfill": {"hours_attempted": 24, "hours_failed": 0}} for _ in range(30)
        ]
        daily_result = {
            "hours_attempted": 30,
            "hours_succeeded": 30,
            "hours_failed": 0,
        }
        result = rollup.handler(
            {
                "mode": "check_hours_failed",
                "chunk_results": chunk_results,
                "daily_result": daily_result,
            },
            None,
        )
        assert result["all_hours_clean"] is True
        # 30 chunk × 24 hours attempted + 30 daily attempted
        assert result["total_attempted"] == 24 * 30 + 30

    def test_daily_failure_blocks_completion(self, rollup):
        """If daily has any failed days, migration is not clean → marker
        stays in_progress → operator can restart to resume."""
        chunk_results = [
            {"backfill": {"hours_attempted": 24, "hours_failed": 0}} for _ in range(30)
        ]
        daily_result = {
            "hours_attempted": 30,
            "hours_failed": 3,
        }
        result = rollup.handler(
            {
                "mode": "check_hours_failed",
                "chunk_results": chunk_results,
                "daily_result": daily_result,
            },
            None,
        )
        assert result["all_hours_clean"] is False
        assert result["total_failed"] == 3


@pytest.mark.unit
class TestBackfillDailyRange:
    """``mode: backfill_daily_range`` iterates each day in the retention
    window and invokes ``_run_daily`` per day. Fills metering_daily and
    metering_docs_daily during migration — without this, those tables
    stay empty for 30 days waiting for scheduled 00:15 UTC runs."""

    def test_iterates_each_day_in_range(self, rollup):
        called_anchors: list = []

        def _fake_run_daily(anchor):
            called_anchors.append(anchor)
            return {
                "metering_daily": {"skipped": False},
                "metering_docs_daily": {"skipped": False},
            }

        with patch.object(rollup, "_run_daily", side_effect=_fake_run_daily):
            result = rollup.handler(
                {
                    "mode": "backfill_daily_range",
                    "days": 3,
                    "time": "2026-09-22T00:00:00Z",
                },
                None,
            )
        # 3 days → 3 _run_daily invocations.
        assert len(called_anchors) == 3
        assert result["hours_attempted"] == 3
        assert result["hours_succeeded"] == 3
        assert result["hours_failed"] == 0

    def test_day_error_recorded_not_aborted(self, rollup):
        """One failing day should be recorded as hours_failed, not
        block subsequent days."""

        def _fake_run_daily(anchor):
            if anchor.day == 21:
                raise RuntimeError("simulated Athena error")
            return {
                "metering_daily": {"skipped": False},
                "metering_docs_daily": {"skipped": False},
            }

        with patch.object(rollup, "_run_daily", side_effect=_fake_run_daily):
            result = rollup.handler(
                {
                    "mode": "backfill_daily_range",
                    "days": 3,
                    "time": "2026-09-22T00:00:00Z",
                },
                None,
            )
        assert result["hours_attempted"] == 3
        assert result["hours_failed"] == 1
        assert result["hours_succeeded"] == 2
        assert len(result["failures"]) == 1

    # ``test_partial_day_counted_as_partial`` was removed with the
    # ``elif md_ok or mdd_ok`` branch in ``_run_backfill_daily_range``
    # — the branch was unreachable in production (``_run_daily`` raises
    # on any sub-INSERT failure, so it never returns a shape with an
    # ``error`` key) and the test only passed by mocking a shape the
    # code doesn't produce. Real per-table failures land in the
    # ``except`` branch below via ``_run_daily``'s raise and are
    # covered by ``test_daily_range_days_that_raise_count_as_failed``.

    def test_rejects_out_of_range_days(self, rollup):
        for bad in (0, -1, 91, 365):
            with pytest.raises(ValueError, match="out of range"):
                rollup.handler({"mode": "backfill_daily_range", "days": bad}, None)


@pytest.mark.unit
class TestPurgeS3PrefixHelper:
    """``_purge_s3_prefix`` — used by ``purge_rollup_prefixes`` mode
    and (previously) by ``_run_backfill_migrate``. Batches S3
    DeleteObjects at 1000 keys per call (the AWS hard cap)."""

    def test_purge_batches_deletes_over_1000(self, rollup):
        """S3 DeleteObjects caps at 1000 keys per call. A prefix with
        2500 objects must issue 3 batched deletes (1000, 1000, 500)."""
        big_page = {
            "Contents": [{"Key": f"metering_hourly/f{i}.parquet"} for i in range(2500)]
        }
        with patch.object(rollup, "s3_client", MagicMock()) as mock_s3:
            mock_s3.get_paginator.return_value.paginate.return_value = [big_page]
            deleted = rollup._purge_s3_prefix("test-bucket", "metering_hourly/")
        assert deleted == 2500
        assert mock_s3.delete_objects.call_count == 3
        batch_sizes = [
            len(call.kwargs["Delete"]["Objects"])
            for call in mock_s3.delete_objects.call_args_list
        ]
        assert batch_sizes == [1000, 1000, 500]

    def test_purge_requires_bucket_and_prefix(self, rollup):
        with pytest.raises(ValueError, match="requires bucket and prefix"):
            rollup._purge_s3_prefix("", "metering_hourly/")
        with pytest.raises(ValueError, match="requires bucket and prefix"):
            rollup._purge_s3_prefix("test-bucket", "")

    def test_migrate_handler_rejects_unknown_mode(self, rollup):
        """Ensure new modes don't silently swallow typos."""
        with pytest.raises(ValueError, match="Unknown rollup mode"):
            rollup.handler({"mode": "planmigrationchunks"}, None)  # typo


@pytest.mark.unit
class TestMarkerVersionComparison:
    """The SSM migration marker now carries a ``version=<x>`` segment,
    and ``_check_marker_state`` compares BOTH ``days`` AND ``version``.
    Without this, a future schema-widening release that bumps
    MigrationVersion but keeps Days=30 would short-circuit against a
    completed marker from the prior version — the exact silent-no-op
    bug this fix prevents."""

    def test_completed_marker_same_version_short_circuits(self, rollup):
        """Matching days AND version → should_short_circuit=True."""
        with patch.object(rollup, "boto3") as mock_boto3:
            mock_ssm = MagicMock()
            mock_boto3.client.return_value = mock_ssm
            mock_ssm.get_parameter.return_value = {
                "Parameter": {
                    "Value": "days=30;version=v1;state=completed;completed_at=2026-09-22T00:00:00Z"
                }
            }
            result = rollup._check_marker_state(30, version="v1")
        assert result["should_short_circuit"] is True
        assert result["should_skip_purge"] is False
        assert result["state"] == "completed"
        assert result["version"] == "v1"

    def test_completed_marker_different_version_full_flow(self, rollup):
        """Days match but version differs → must NOT short-circuit — this
        is the case where the next schema-widening release ships and needs
        a fresh migration despite an existing completed marker."""
        with patch.object(rollup, "boto3") as mock_boto3:
            mock_ssm = MagicMock()
            mock_boto3.client.return_value = mock_ssm
            mock_ssm.get_parameter.return_value = {
                "Parameter": {
                    "Value": "days=30;version=v1;state=completed;completed_at=2026-09-22T00:00:00Z"
                }
            }
            result = rollup._check_marker_state(30, version="v2")
        assert result["should_short_circuit"] is False
        assert result["should_skip_purge"] is False

    def test_in_progress_marker_same_version_skips_purge(self, rollup):
        """Matching days AND version + state=in_progress → skip purge
        (resume mid-migration retry semantics)."""
        with patch.object(rollup, "boto3") as mock_boto3:
            mock_ssm = MagicMock()
            mock_boto3.client.return_value = mock_ssm
            mock_ssm.get_parameter.return_value = {
                "Parameter": {
                    "Value": "days=30;version=v1;state=in_progress;started_at=2026-09-22T00:00:00Z"
                }
            }
            result = rollup._check_marker_state(30, version="v1")
        assert result["should_short_circuit"] is False
        assert result["should_skip_purge"] is True

    def test_in_progress_marker_different_version_full_flow(self, rollup):
        """A stuck in_progress marker from a prior version MUST NOT resume
        — its purge state is irrelevant to a different-version migration."""
        with patch.object(rollup, "boto3") as mock_boto3:
            mock_ssm = MagicMock()
            mock_boto3.client.return_value = mock_ssm
            mock_ssm.get_parameter.return_value = {
                "Parameter": {
                    "Value": "days=30;version=v1;state=in_progress;started_at=2026-09-22T00:00:00Z"
                }
            }
            result = rollup._check_marker_state(30, version="v2")
        assert result["should_short_circuit"] is False
        assert result["should_skip_purge"] is False

    def test_legacy_marker_without_version_full_flow(self, rollup):
        """A marker written before version support (no ``version=`` segment)
        is treated as MISMATCH — routes to full flow. Silent-accept was
        rejected because it would reintroduce the original bug on the
        NEXT schema change."""
        with patch.object(rollup, "boto3") as mock_boto3:
            mock_ssm = MagicMock()
            mock_boto3.client.return_value = mock_ssm
            mock_ssm.get_parameter.return_value = {
                "Parameter": {
                    "Value": "days=30;state=completed;completed_at=2026-09-22T00:00:00Z"
                }
            }
            result = rollup._check_marker_state(30, version="v1")
        assert result["should_short_circuit"] is False
        assert result["state"] == "unrecognised"

    def test_version_none_ignores_version_axis(self, rollup):
        """When the caller doesn't pass a version (direct SFN invocations
        with pre-versioning input shape), fall back to days-only
        comparison — preserves the original contract."""
        with patch.object(rollup, "boto3") as mock_boto3:
            mock_ssm = MagicMock()
            mock_boto3.client.return_value = mock_ssm
            mock_ssm.get_parameter.return_value = {
                "Parameter": {
                    "Value": "days=30;state=completed;completed_at=2026-09-22T00:00:00Z"
                }
            }
            result = rollup._check_marker_state(30, version=None)
        assert result["should_short_circuit"] is True

    def test_write_marker_includes_version_segment(self, rollup):
        """``_write_marker(state='completed', days=30, version='v1')``
        must produce a payload whose ``_check_marker_state`` round-trips."""
        with patch.object(rollup, "boto3") as mock_boto3:
            mock_ssm = MagicMock()
            mock_boto3.client.return_value = mock_ssm
            rollup._write_marker("completed", 30, version="v1")
            call_kwargs = mock_ssm.put_parameter.call_args.kwargs
            value = call_kwargs["Value"]
        assert "days=30" in value
        assert "version=v1" in value
        assert "state=completed" in value

    def test_write_marker_no_version_omits_segment(self, rollup):
        """Backward-compat: when version isn't supplied, no ``version=``
        segment is emitted (preserves the pre-versioning payload shape)."""
        with patch.object(rollup, "boto3") as mock_boto3:
            mock_ssm = MagicMock()
            mock_boto3.client.return_value = mock_ssm
            rollup._write_marker("completed", 30, version=None)
            value = mock_ssm.put_parameter.call_args.kwargs["Value"]
        assert "version=" not in value
        assert "days=30" in value
        assert "state=completed" in value


@pytest.mark.unit
class TestBackfillArmsFilter:
    """2.1.5 fix: ``_run_backfill(arms=...)`` restricts which per-hour
    rollup arms fire. The migration state machine passes
    ``arms=["metering_hourly", "metering_docs_hourly"]`` because those
    are the ONLY two rollup tables whose schema the widening deploy
    changed; running the other two during migration was scope creep
    that (a) doubled per-chunk time and (b) surfaced empty-CW-hour
    partial failures that blocked WriteCompletedMarker."""

    def test_arms_none_runs_all_four(self, rollup):
        """Backward-compat: the reconciler calls without ``arms`` and
        must still get all four arms fired (its contract)."""
        with (
            patch.object(rollup, "_rollup_metering_hourly") as mh,
            patch.object(rollup, "_rollup_metering_docs_hourly") as mdh,
            patch.object(rollup, "_rollup_control_plane_hourly") as cph,
            patch.object(rollup, "_rollup_data_plane_lambda_hourly") as dph,
        ):
            r = rollup._run_backfill(
                "2026-08-23T14:00:00+00:00", "2026-08-23T15:00:00+00:00"
            )
        assert mh.call_count == 1
        assert mdh.call_count == 1
        assert cph.call_count == 1
        assert dph.call_count == 1
        assert set(r["arms"]) == {
            "metering_hourly",
            "metering_docs_hourly",
            "control_plane_hourly",
            "data_plane_lambda_hourly",
        }

    def test_arms_subset_runs_only_listed(self, rollup):
        """Migration's arms=[metering_hourly, metering_docs_hourly]
        must skip the two non-metering arms entirely — not even called."""
        with (
            patch.object(rollup, "_rollup_metering_hourly") as mh,
            patch.object(rollup, "_rollup_metering_docs_hourly") as mdh,
            patch.object(rollup, "_rollup_control_plane_hourly") as cph,
            patch.object(rollup, "_rollup_data_plane_lambda_hourly") as dph,
        ):
            r = rollup._run_backfill(
                "2026-08-23T14:00:00+00:00",
                "2026-08-23T16:00:00+00:00",  # 2 hours
                arms=["metering_hourly", "metering_docs_hourly"],
            )
        assert mh.call_count == 2
        assert mdh.call_count == 2
        assert cph.call_count == 0
        assert dph.call_count == 0
        assert r["arms"] == ["metering_hourly", "metering_docs_hourly"]
        assert r["hours_succeeded"] == 2
        assert r["hours_partial"] == 0
        assert r["hours_failed"] == 0

    def test_arms_unknown_label_rejected(self, rollup):
        """A typo in the ``arms`` list should be rejected loudly — a
        silent skip would produce chunks that report 'succeeded' without
        actually writing the intended tables."""
        with pytest.raises(ValueError, match="unknown labels"):
            rollup._run_backfill(
                "2026-08-23T14:00:00+00:00",
                "2026-08-23T15:00:00+00:00",
                arms=["metering_hourly", "metrring_docs_hourly"],  # typo
            )

    def test_handler_rejects_non_list_arms(self, rollup):
        """Payload validation — arms must be a list, not a scalar."""
        with pytest.raises(ValueError, match="'arms' must be a list"):
            rollup.handler(
                {
                    "mode": "backfill",
                    "start": "2026-08-23T14:00:00+00:00",
                    "end": "2026-08-23T15:00:00+00:00",
                    "arms": "metering_hourly",  # should be a list
                },
                None,
            )


@pytest.mark.unit
class TestBuildDocClassCteDateFilter:
    """2.1.4 fix: ``_build_doc_class_cte(target_date=...)`` MUST inject a
    ``WHERE date BETWEEN <D-1> AND <D+1>`` on every UNION arm so Athena
    partition-prunes the ``document_sections_*`` scans.

    Without this, one hourly rollup INSERT fanned out 20 tables × 30-day
    partitions = 600 concurrent S3 GETs, and 720 migration chunks stacked
    against S3's per-prefix 5500 GET/s cap → HIVE_S3_THROTTLING on every
    chunk (2026-09-22 live incident on idp-dev-qs1)."""

    def test_target_date_injects_partition_filter(self, rollup):
        """CTE with target_date must include a partition-pruning WHERE."""
        rollup._document_sections_tables_cache = [
            "document_sections_invoice",
            "document_sections_w2",
        ]
        cte = rollup._build_doc_class_cte(target_date="2026-08-23")
        # Both UNION arms must carry the filter, with the ±1 day slop.
        assert cte.count("WHERE date BETWEEN '2026-08-22' AND '2026-08-24'") == 2
        assert '"document_sections_invoice"' in cte
        assert '"document_sections_w2"' in cte

    def test_no_target_date_omits_filter(self, rollup):
        """Backward-compat: calling without target_date leaves the CTE
        unfiltered (matches pre-2.1.4 behavior for any callers that don't
        yet pass target_date)."""
        rollup._document_sections_tables_cache = ["document_sections_invoice"]
        cte = rollup._build_doc_class_cte()
        assert "WHERE date BETWEEN" not in cte
        # The inner ``WHERE doc_type IS NOT NULL`` on the outer SELECT stays.
        assert "WHERE doc_type IS NOT NULL" in cte

    def test_date_slop_covers_month_boundary(self, rollup):
        """Date arithmetic must handle month/year boundaries — a naive
        string bump would break at the end of the month."""
        rollup._document_sections_tables_cache = ["document_sections_invoice"]
        cte = rollup._build_doc_class_cte(target_date="2026-08-31")
        assert "WHERE date BETWEEN '2026-08-30' AND '2026-09-01'" in cte

    def test_no_tables_returns_empty_cte(self, rollup):
        """Zero doc_sections tables (fresh stack) — CTE yields no rows;
        outer LEFT JOIN produces NULL → COALESCE falls through to
        ``'unknown'``. Empty-CTE behavior must be preserved when
        target_date is provided too (defensive)."""
        rollup._document_sections_tables_cache = []
        cte = rollup._build_doc_class_cte(target_date="2026-08-23")
        assert "WHERE 1 = 0" in cte


@pytest.mark.unit
class TestReconcileMode:
    """``mode: 'reconcile'`` re-runs the four per-doc hourly rollups over
    the trailing 24 h. Idempotent — already-written partitions are no-ops.
    Fires at :35 of each hour on a separate EventBridge rule so it can
    fill in gaps left by a missed :05 hourly.
    """

    def test_reconcile_dispatches_to_run_reconcile(self, rollup):
        with patch.object(
            rollup,
            "_run_reconcile",
            return_value={"mode": "backfill", "hours_attempted": 24},
        ) as recon:
            result = rollup.handler(
                {"mode": "reconcile", "time": "2026-09-21T12:35:00Z"}, None
            )
        recon.assert_called_once()
        # Anchor passed to _run_reconcile is the trigger time.
        (anchor_arg,), _ = recon.call_args
        assert anchor_arg.isoformat() == "2026-09-21T12:35:00+00:00"
        assert result["hours_attempted"] == 24

    def test_reconcile_scans_trailing_24_hours(self, rollup):
        """Bounds: end = trigger truncated to top of hour; start = end - 24 h.
        This ensures the reconciler never chases the CURRENT hour (still
        in flight) and covers a full day of possibly-missed partitions."""
        captured_bounds = []

        def _fake_backfill(start_raw, end_raw):
            captured_bounds.append((start_raw, end_raw))
            return {"hours_attempted": 24, "hours_succeeded": 24}

        with patch.object(rollup, "_run_backfill", side_effect=_fake_backfill):
            # Trigger at 12:35 UTC → end should truncate to 12:00,
            # start should be 24 h earlier at 12:00 of the prior day.
            anchor = datetime(2026, 9, 21, 12, 35, 0, tzinfo=timezone.utc)
            rollup._run_reconcile(anchor)
        assert captured_bounds == [
            ("2026-09-20T12:00:00+00:00", "2026-09-21T12:00:00+00:00")
        ]

    def test_reconcile_is_a_thin_wrapper_over_backfill(self, rollup):
        """The reconciler is deliberately unopinionated — it just delegates
        to `_run_backfill`. That's the whole design: same idempotency,
        same failure handling, same result shape. Changes to backfill
        semantics automatically apply to the reconciler."""
        with patch.object(
            rollup,
            "_run_backfill",
            return_value={
                "mode": "backfill",
                "hours_attempted": 24,
                "hours_succeeded": 24,
            },
        ) as bf:
            anchor = datetime(2026, 9, 21, 12, 35, 0, tzinfo=timezone.utc)
            result = rollup._run_reconcile(anchor)
        bf.assert_called_once()
        # Reconciler returns whatever backfill returned — no wrapping.
        assert result["hours_attempted"] == 24
