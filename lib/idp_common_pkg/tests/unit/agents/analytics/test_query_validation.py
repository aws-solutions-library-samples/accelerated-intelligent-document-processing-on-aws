# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The analytics agent's Athena tool runs exactly one read-only statement.

The corpus below is the specification: every refused case names the statement shape
it stands for, and every accepted case is a query shape the agent's own prompt
teaches (``agents/analytics/agent.py``) or a refused keyword appearing only as data.
"""

from unittest.mock import MagicMock, patch

import pytest

from idp_common.agents.analytics.query_validation import (
    REFUSED_KEYWORDS,
    QueryNotAllowedError,
    validate_read_only_query,
)

ACCEPTED = [
    pytest.param("SELECT 1", id="plain-select"),
    pytest.param("select count(*) from metering", id="lowercase"),
    pytest.param("SELECT 1;", id="one-trailing-terminator"),
    pytest.param("  SELECT 1 ;  \n", id="terminator-with-whitespace"),
    pytest.param(
        'WITH t AS (SELECT "document_id" FROM metering) SELECT * FROM t',
        id="with-select",
    ),
    pytest.param("(SELECT 1) UNION ALL (SELECT 2)", id="parenthesised-select"),
    pytest.param("SHOW TABLES", id="show-tables"),
    pytest.param("SHOW COLUMNS IN metering", id="show-columns"),
    pytest.param("DESCRIBE metering", id="describe"),
    pytest.param(
        'SELECT "update", "delete" FROM t WHERE "create" = 1',
        id="refused-words-as-quoted-identifiers",
    ),
    pytest.param(
        "SELECT * FROM t WHERE x = 'DROP TABLE t; INSERT INTO t'",
        id="refused-words-and-semicolon-in-a-literal",
    ),
    pytest.param(
        "SELECT * FROM t WHERE x = 'it''s; DROP'", id="escaped-quote-in-literal"
    ),
    pytest.param("SELECT 1 -- DROP TABLE t; \n", id="refused-words-in-line-comment"),
    pytest.param("SELECT /* ; DELETE */ 1", id="refused-words-in-block-comment"),
    pytest.param(
        'SELECT "document_class.type" FROM document_sections_w2 WHERE "date" = CAST(CURRENT_DATE AS VARCHAR)',
        id="prompt-example-classification",
    ),
    pytest.param(
        "SELECT * FROM t WHERE REGEXP_LIKE(LOWER(\"service_api\"), 'classification') "
        "AND starts_with(\"x\", 'a')",
        id="functions-whose-names-contain-refused-words",
    ),
]

REFUSED = [
    pytest.param("", id="empty"),
    pytest.param("   ;  ", id="only-terminator"),
    pytest.param("SELECT 1; SELECT 2", id="two-statements"),
    pytest.param("SELECT 1;;", id="doubled-terminator"),
    pytest.param("SELECT 1; DROP TABLE metering", id="chained-ddl"),
    pytest.param("DROP TABLE metering", id="ddl-drop"),
    pytest.param("CREATE TABLE x AS SELECT * FROM metering", id="ctas"),
    pytest.param("ALTER TABLE metering ADD COLUMNS (x string)", id="ddl-alter"),
    pytest.param("INSERT INTO metering SELECT * FROM metering", id="dml-insert"),
    pytest.param("DELETE FROM metering WHERE 1 = 1", id="dml-delete"),
    pytest.param("UPDATE metering SET x = 1", id="dml-update"),
    pytest.param(
        "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE", id="dml-merge"
    ),
    pytest.param(
        "UNLOAD (SELECT * FROM metering) TO 's3://remote.example/out/' "
        "WITH (format = 'CSV')",
        id="unload",
    ),
    pytest.param(
        "USING EXTERNAL FUNCTION f(x VARCHAR) RETURNS VARCHAR "
        "LAMBDA 'some-function' SELECT f(x) FROM t",
        id="external-function",
    ),
    pytest.param(
        "WITH FUNCTION f(x INT) RETURNS INT RETURN x SELECT f(1)",
        id="inline-function",
    ),
    pytest.param(
        "SELECT * FROM TABLE(system.query(query => 'SELECT 1'))",
        id="table-function",
    ),
    pytest.param(
        "WITH t AS (SELECT 1) INSERT INTO x SELECT * FROM t", id="write-after-with"
    ),
    pytest.param("EXPLAIN SELECT 1", id="explain"),
    pytest.param("PREPARE s FROM SELECT 1", id="prepare"),
    pytest.param("EXECUTE s", id="execute"),
    pytest.param("MSCK REPAIR TABLE metering", id="msck"),
    pytest.param("SET SESSION query_max_run_time = '1h'", id="session"),
    pytest.param("SHOW CREATE TABLE metering", id="show-create"),
    pytest.param("VALUES 1", id="values-statement"),
    pytest.param("SELECT 'unterminated", id="unterminated-literal"),
    pytest.param('SELECT "unterminated', id="unterminated-identifier"),
    pytest.param("SELECT 1 /* unterminated", id="unterminated-comment"),
    pytest.param("SELECT update FROM t", id="refused-word-as-bare-identifier"),
    pytest.param("/* SELECT */ DROP TABLE t", id="leading-keyword-only-in-comment"),
    pytest.param("'SELECT' DROP TABLE t", id="leading-keyword-only-in-literal"),
]


@pytest.mark.unit
@pytest.mark.parametrize("query", ACCEPTED)
def test_accepted(query):
    assert validate_read_only_query(query) == query.strip()


@pytest.mark.unit
@pytest.mark.parametrize("query", REFUSED)
def test_refused(query):
    with pytest.raises(QueryNotAllowedError):
        validate_read_only_query(query)


@pytest.mark.unit
def test_the_corpus_covers_the_shapes_the_tool_must_refuse():
    """Non-vacuity: an empty corpus would make the parametrized tests above vacuous,
    so both are held to a floor, and every refused keyword is refused when it appears
    bare in a SELECT, which is the one position the leading-keyword rule alone does
    not cover."""
    assert len(REFUSED) >= 20
    assert len(ACCEPTED) >= 10
    for keyword in sorted(REFUSED_KEYWORDS):
        with pytest.raises(QueryNotAllowedError):
            validate_read_only_query(f"SELECT x FROM t WHERE {keyword} = 1")


@pytest.mark.unit
class TestRunAthenaQueryRefusesBeforeCallingAthena:
    """A refused statement never reaches Athena."""

    @patch("idp_common.agents.analytics.tools.athena_tool.boto3")
    def test_refused_query_makes_no_athena_call(self, mock_boto3):
        from idp_common.agents.analytics.tools.athena_tool import run_athena_query

        result = run_athena_query(
            "SELECT 1; DROP TABLE metering",
            {"aws_region": "us-east-1", "athena_database": "db"},
        )

        assert result["success"] is False
        assert result["state"] == "NOT_RUN"
        assert "one SQL statement" in result["error"]
        mock_boto3.client.assert_not_called()

    @patch("idp_common.agents.analytics.tools.athena_tool.boto3")
    def test_accepted_query_is_sent(self, mock_boto3):
        from idp_common.agents.analytics.tools.athena_tool import run_athena_query

        athena = MagicMock()
        mock_boto3.client.return_value = athena
        athena.start_query_execution.return_value = {"QueryExecutionId": "q-1"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {
                "Status": {"State": "FAILED", "StateChangeReason": "fixture"},
            }
        }

        run_athena_query(
            "SELECT 1",
            {
                "aws_region": "us-east-1",
                "athena_database": "db",
                "athena_output_location": "s3://results-bucket/athena-results/",
            },
        )

        athena.start_query_execution.assert_called_once()
        assert (
            athena.start_query_execution.call_args.kwargs["QueryString"] == "SELECT 1"
        )
