# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each rollup ``INSERT INTO ... SELECT`` must emit columns in the Glue
table's own order.

Athena maps an ``INSERT INTO <table> SELECT ...`` by **position**, never by
name: the SELECT's Nth output goes into the table's Nth column, with the
partition columns last in the order ``PartitionKeys`` declares them. So a
column added to a Glue table in ``template.yaml`` without being added at the
matching position of the INSERT does not fail — it writes the wrong value into
every row, or fails only at the point two adjacent columns happen to have
incompatible types. ``n_runs`` was appended to ``metering_docs_hourly`` and
``metering_docs_daily``; had it gone in before ``sum_pages`` in the table and
after it in the SELECT, both are ``bigint``, so pages and runs would simply
have swapped places in the lake with nothing raising.

``TestDocumentClassInRollupSQL`` in ``test_data_mart_rollup.py`` pins the
positions it cares about against integer literals written into the test. This
module derives **both** sides instead — the schema from ``template.yaml``, the
SELECT from the SQL the Lambda actually submits — so neither side can move
alone.
"""

from __future__ import annotations

import functools
import importlib.util
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

pytestmark = pytest.mark.unit


def _find_repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "template.yaml").is_file() and (parent / "patterns").is_dir():
            return parent
    raise RuntimeError("could not locate the repo root holding template.yaml")


_REPO = _find_repo_root()


# --------------------------------------------------------------------------- #
# the schema side: template.yaml
# --------------------------------------------------------------------------- #


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation's ``!Ref``/``!GetAtt`` tags.

    The Glue column lists hold no intrinsics, but the resources around them do,
    so a plain ``safe_load`` of the template raises before reaching them.
    """


def _cfn_tag(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{tag_suffix}": loader.construct_sequence(node, deep=True)}
    return {f"Fn::{tag_suffix}": loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _cfn_tag)


@functools.lru_cache(maxsize=None)
def _glue_column_order(table_name: str) -> tuple[str, ...]:
    """The table's data columns in declared order, then its partition keys.

    This is the exact tuple shape an ``INSERT ... SELECT`` has to produce.
    """
    template = yaml.load((_REPO / "template.yaml").read_text(), Loader=_CfnLoader)  # noqa: S506 - _CfnLoader subclasses SafeLoader
    for resource in template["Resources"].values():
        if resource.get("Type") != "AWS::Glue::Table":
            continue
        table_input = resource["Properties"]["TableInput"]
        if table_input.get("Name") != table_name:
            continue
        data = [
            c["Name"] for c in table_input["StorageDescriptor"].get("Columns") or []
        ]
        parts = [c["Name"] for c in table_input.get("PartitionKeys") or []]
        # Tuple, not list: the lru_cache above hands the SAME object to every
        # caller, and a list would let one test's mutation reach the next.
        return tuple(data + parts)
    raise AssertionError(f"no AWS::Glue::Table named {table_name!r} in template.yaml")


# --------------------------------------------------------------------------- #
# the SQL side: what the Lambda submits
# --------------------------------------------------------------------------- #


def _strip_with_clauses(sql: str) -> str:
    """Drop any leading ``WITH <name> AS ( ... )`` so the outer SELECT is first.

    Walks paren depth rather than matching a regex: the CTE body contains its
    own ``SELECT``/``FROM``, and a non-greedy regex ends at the wrong paren.
    """
    out = sql
    while True:
        m = re.search(r"\bWITH\b", out, re.IGNORECASE)
        if not m or out[: m.start()].strip():
            return out
        j = out.find("(", m.end())
        if j < 0:
            return out
        depth, k = 1, j + 1
        while k < len(out) and depth:
            if out[k] == "(":
                depth += 1
            elif out[k] == ")":
                depth -= 1
            k += 1
        rest = out[k:].lstrip()
        if rest.startswith(","):  # a second CTE in the same WITH
            out = "WITH " + rest[1:].lstrip()
            continue
        out = rest


def _select_output_names(sql: str) -> list[str]:
    """The output name of each item in the outer SELECT list, in order.

    ``<expr> AS <name>`` yields ``<name>``; a bare column yields the column.
    """
    # Drop ``INSERT INTO <table>`` first: the CTE sits AFTER it, so a
    # WITH-stripper anchored at the start of the string sees a non-empty
    # prefix and leaves the CTE in place — which puts the CTE's own inner
    # SELECT list where the outer one should be.
    body = re.sub(r"^\s*INSERT\s+INTO\s+\S+\s*", "", sql, flags=re.IGNORECASE)
    assert body != sql, f"not an INSERT ... SELECT:\n{sql}"
    body = _strip_with_clauses(body)
    m = re.search(r"\bSELECT\b", body, re.IGNORECASE)
    assert m, f"no SELECT in:\n{sql}"
    i, depth = m.end(), 0
    items: list[str] = [""]
    while i < len(body):
        ch = body[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0:
            if ch == ",":
                items.append("")
                i += 1
                continue
            # The outer FROM at depth 0 ends the SELECT list. A ``FROM`` inside
            # a subquery or a function call sits at depth > 0 and is skipped.
            if re.match(r"FROM\b", body[i:], re.IGNORECASE):
                break
        items[-1] += ch
        i += 1
    else:  # pragma: no cover - a SELECT with no FROM is not a shape we emit
        raise AssertionError(f"no outer FROM terminating the SELECT list:\n{sql}")

    names = []
    for item in items:
        tokens = item.split()
        assert tokens, f"empty SELECT item in:\n{sql}"
        if len(tokens) >= 2 and tokens[-2].upper() == "AS":
            names.append(tokens[-1].strip('"'))
        else:
            # A bare ``m.config_version`` is output as ``config_version`` —
            # Athena drops the table qualifier from the result column name.
            names.append(tokens[-1].strip('",').rsplit(".", 1)[-1].strip('"'))
    return names


@pytest.fixture
def rollup():
    """Import ``src/lambda/data_mart_rollup/index.py`` under a unique name."""
    path = _REPO / "src" / "lambda" / "data_mart_rollup" / "index.py"
    spec = importlib.util.spec_from_file_location("_rollup_colorder_index", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with patch.dict(
        "os.environ",
        {
            "ATHENA_DATABASE": "db",
            "ATHENA_OUTPUT_LOCATION": "s3://bucket/prefix/",
            "METERING_TABLE": "metering",
            "AWS_DEFAULT_REGION": "us-east-1",
        },
    ):
        spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _capture_hourly(rollup, fn_name: str) -> str:
    captured: list[str] = []
    with (
        patch.object(rollup, "_partition_already_written", return_value=False),
        patch.object(
            rollup,
            "_run_athena",
            side_effect=lambda sql, **_kw: captured.append(sql) or "qid-x",
        ),
        patch.object(rollup, "_partition_produced_rows", return_value=True),
    ):
        getattr(rollup, fn_name)("2026-09-22", "13")
    assert captured, f"{fn_name} submitted no Athena query"
    return captured[0]


def _capture_daily(rollup) -> list[str]:
    captured: list[str] = []
    with (
        patch.object(rollup, "_partition_already_written", return_value=False),
        patch.object(rollup, "_require_hourly_matches_raw_metering", return_value=None),
        patch.object(rollup, "_partition_produced_rows", return_value=True),
        patch.object(
            rollup,
            "_run_athena",
            side_effect=lambda sql, **_kw: captured.append(sql) or "qid-x",
        ),
    ):
        rollup._run_daily(datetime(2026, 9, 22, 0, 15, 0, tzinfo=timezone.utc))
    assert len(captured) >= 2, "_run_daily must submit both daily INSERTs"
    return captured


def _sql_for(rollup, table: str) -> str:
    if table == "metering_hourly":
        return _capture_hourly(rollup, "_rollup_metering_hourly")
    if table == "metering_docs_hourly":
        return _capture_hourly(rollup, "_rollup_metering_docs_hourly")
    for sql in _capture_daily(rollup):
        if f'"{table}"' in sql.split("SELECT")[0]:
            return sql
    raise AssertionError(f"no INSERT into {table} among the daily queries")


_TABLES = (
    "metering_hourly",
    "metering_daily",
    "metering_docs_hourly",
    "metering_docs_daily",
)


class TestInsertColumnOrderMatchesTheGlueSchema:
    @pytest.mark.parametrize("table", _TABLES)
    def test_select_emits_the_schema_order(self, rollup, table: str) -> None:
        expected = list(_glue_column_order(table))
        actual = _select_output_names(_sql_for(rollup, table))
        assert actual == expected, (
            f"the INSERT into {table} emits columns in a different order than "
            f"the Glue table declares them, and Athena maps them by POSITION:\n"
            f"  template.yaml: {expected}\n"
            f"  SELECT list:   {actual}\n"
            "Every row written takes the wrong column's value wherever the two "
            "lists diverge, and no query fails where the mismatched columns "
            "share a type."
        )

    @pytest.mark.parametrize("table", ("metering_docs_hourly", "metering_docs_daily"))
    def test_the_docs_tables_carry_n_runs(self, rollup, table: str) -> None:
        """``n_runs`` is on both halves or figures built from it go wrong.

        The daily table SUMs the hourly one, so a column present on one and
        absent from the other is not a cosmetic gap: the daily rollup would
        either fail to compile or (if also dropped from the daily table) leave
        every ``> 24h`` reader with no run count at all.
        """
        assert "n_runs" in _glue_column_order(table), (
            f"{table} declares no n_runs column"
        )
        assert "n_runs" in _select_output_names(_sql_for(rollup, table)), (
            f"the INSERT into {table} does not populate n_runs"
        )


class TestRunsAreCountedPerDocumentNotPerCell:
    """⚠️ The run count has to be distinct-per-document, then summed.

    A batch upload stamps several documents with the same ``timestamp``, so
    ``COUNT(DISTINCT m."timestamp")`` taken at the cell level collapses those
    into one and loses a run for every pair of documents sharing an instant —
    measured as 89 runs short of the true total over one real stack's table.
    The inner subquery already groups by ``document_id``, so the distinct count
    belongs there and the outer aggregate just sums it.

    Both spellings compile, both return plausible numbers, and the wrong one is
    only ever slightly low. That is why this is pinned rather than reviewed.
    """

    def test_the_distinct_count_is_inside_the_per_document_subquery(
        self, rollup
    ) -> None:
        sql = " ".join(_sql_for(rollup, "metering_docs_hourly").split())
        inner = sql[sql.index("FROM (") :]
        assert 'COUNT(DISTINCT m."timestamp")' in inner, (
            "the per-document run count is not in the inner subquery; a "
            "cell-level COUNT(DISTINCT timestamp) under-counts runs whenever a "
            f"batch shares a timestamp across documents:\n{sql}"
        )
        outer = sql[: sql.index("FROM (")]
        assert "DISTINCT" not in outer.upper(), (
            "the outer aggregate takes a DISTINCT of its own — it must only SUM "
            f"the per-document counts:\n{outer}"
        )
        assert "SUM(runs)" in outer, (
            f"the outer aggregate does not SUM the per-document run counts:\n{outer}"
        )

    def test_the_daily_rollup_propagates_an_incomplete_day(self, rollup) -> None:
        """⚠️ A bare ``SUM(n_runs)`` is wrong here, and quietly.

        A run falls in exactly one hour, so summing hours is the right
        arithmetic — but only over hours that recorded a run count. Rows
        written before the column existed carry no value, and Athena's ``SUM``
        ignores NULLs, so a day whose hourly cells are PARTLY pre-bump returns
        an ordinary-looking number that is low by whatever those hours held.

        A routine migration does not produce such a day: the window's low end
        is truncated to top-of-day so every day in range gets 24 hours of
        coverage. The mixed day comes from a chunk that failed partway, an
        operator backfilling a narrowed hour range by hand, or ``Days`` being
        raised later so an older day is re-aggregated from hours of which only
        some were replayed. A wholly-unrecorded day reads NULL and is visibly
        unknown; this makes a half-recorded one read the same way.
        """
        sql = " ".join(_sql_for(rollup, "metering_docs_daily").split())
        assert "COUNT(n_runs) = COUNT(*)" in sql, (
            "the daily rollup does not check that every hourly cell in the "
            f"group recorded a run count before summing:\n{sql}"
        )
        assert "SUM(n_runs)" in sql, (
            f"the daily rollup does not sum the hourly n_runs at all:\n{sql}"
        )
        # The bare form, with nothing guarding it, is the defect.
        assert "SUM(n_runs) AS n_runs" not in sql, (
            "the daily rollup sums n_runs unguarded, so a day straddling the "
            f"schema bump understates instead of reading unknown:\n{sql}"
        )
        assert "DISTINCT" not in sql.upper(), (
            "the daily rollup reads an already-aggregated table; a DISTINCT "
            f"there would discard runs that happen to tie:\n{sql}"
        )
