# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Statement checks for the SQL the analytics agent sends to Athena.

The analytics agent writes its own SQL, so the statement Athena receives is model
output. The agent's job is to read the reporting tables, and this module holds it to
that: a query is accepted only when it is exactly one read-only statement.

Accepted:

- one ``SELECT`` statement, including one that opens with ``WITH`` (common table
  expressions) or with parentheses;
- one ``SHOW ...`` or ``DESCRIBE ...`` statement, which the agent's prompt permits for
  schema discovery and which read only catalog metadata.

Refused:

- more than one statement (any ``;`` other than one trailing terminator);
- any statement that does not start with one of the keywords above (DDL, DML,
  ``UNLOAD``, ``EXPLAIN``, ``PREPARE``/``EXECUTE``, session statements, ...);
- any of the keywords in :data:`REFUSED_KEYWORDS` *anywhere* in the statement,
  outside string literals, quoted identifiers and comments. That is what refuses
  ``USING EXTERNAL FUNCTION``, inline ``WITH FUNCTION`` definitions, table functions
  (``TABLE(...)``) and a write statement placed after a ``WITH`` clause;
- a quoted identifier naming a Lambda-backed catalog (``"lambda:<function>"``);
- a backslash in a ``SHOW`` or ``DESCRIBE`` statement, which Athena runs on its Hive
  engine, where a backslash escapes a quote.

The check is lexical rather than a full SQL parse. It tokenizes the statement the way
Trino does for the parts that matter here (``'...'`` literals with ``''`` escapes,
``"..."`` and backtick identifiers with doubled-quote escapes, ``--`` and ``/* */``
comments) and refuses anything it cannot tokenize, such as an unterminated literal or
comment. A keyword that appears only inside a literal or a quoted identifier is data,
not syntax, so it is not refused: ``SELECT "update" FROM t WHERE x = 'drop'`` is
accepted. A column whose name is one of the refused keywords must be double-quoted,
which the agent's prompt already requires for every column.
"""

from __future__ import annotations

from typing import List, Tuple

#: The first keyword an accepted statement may start with.
LEADING_KEYWORDS = frozenset({"SELECT", "WITH", "SHOW", "DESCRIBE"})

#: Keywords that may not appear anywhere in an accepted statement (outside literals,
#: quoted identifiers and comments). Each names a statement or clause that writes,
#: changes the catalog or session, or runs code other than the query itself.
REFUSED_KEYWORDS = frozenset(
    {
        # Data and catalog changes.
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "TRUNCATE",
        "CREATE",
        "DROP",
        "ALTER",
        "RENAME",
        "COMMENT",
        "MSCK",
        "REPAIR",
        "VACUUM",
        "OPTIMIZE",
        "REFRESH",
        "GRANT",
        "REVOKE",
        "UNLOAD",
        "INTO",
        # Statements that run something other than the query text.
        "PREPARE",
        "EXECUTE",
        "DEALLOCATE",
        "CALL",
        # User-defined and external functions, and table functions.
        "EXTERNAL",
        "FUNCTION",
        "TABLE",
        # Session and transaction control.
        "SET",
        "RESET",
        "USE",
        "START",
        "COMMIT",
        "ROLLBACK",
    }
)


class QueryNotAllowedError(ValueError):
    """The statement is not exactly one read-only query."""


Token = Tuple[str, str]  # (kind, text); kind is "word", "ident", "semi" or "punct"


def _read_quoted(sql: str, start: int, quote: str) -> int:
    """Return the index just past the closing ``quote`` of a quoted run.

    A doubled quote inside the run is an escaped quote character, as Trino reads both
    string literals and quoted identifiers.
    """
    i = start + 1
    n = len(sql)
    while i < n:
        if sql[i] == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    raise QueryNotAllowedError(
        f"The query has an unterminated {quote}...{quote} section."
    )


def _tokenize(sql: str) -> List[Token]:
    """Split ``sql`` into the tokens the checks read.

    Literals, quoted identifiers and comments are consumed and produce no token, so a
    keyword inside one of them can never be mistaken for syntax.
    """
    tokens: List[Token] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end == -1 else end + 1
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end == -1:
                raise QueryNotAllowedError("The query has an unterminated comment.")
            i = end + 2
        elif ch == "'":
            i = _read_quoted(sql, i, ch)
        elif ch in ('"', "`"):
            end = _read_quoted(sql, i, ch)
            tokens.append(("ident", sql[i + 1 : end - 1].replace(ch * 2, ch)))
            i = end
        elif ch == ";":
            tokens.append(("semi", ch))
            i += 1
        elif ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            tokens.append(("word", sql[i:j].upper()))
            i = j
        else:
            tokens.append(("punct", ch))
            i += 1
    return tokens


def validate_read_only_query(query: str) -> str:
    """Return ``query`` stripped of surrounding whitespace if it is exactly one
    read-only statement, and raise :class:`QueryNotAllowedError` otherwise.

    The message on the exception is written for the agent, so it can correct the
    query and try again.
    """
    if not isinstance(query, str) or not query.strip():
        raise QueryNotAllowedError("The query is empty.")

    tokens = _tokenize(query)

    # One statement: a single trailing terminator is allowed and nothing else.
    while tokens and tokens[-1][0] == "semi":
        tokens.pop()
        if tokens and tokens[-1][0] == "semi":
            raise QueryNotAllowedError("Send exactly one SQL statement per query.")
    if any(kind == "semi" for kind, _ in tokens):
        raise QueryNotAllowedError("Send exactly one SQL statement per query.")
    if not tokens:
        raise QueryNotAllowedError("The query is empty.")

    # The leading keyword, skipping opening parentheses: "(SELECT ...)" is a SELECT.
    leading = next((text for kind, text in tokens if text != "("), "")
    first_kind = next((kind for kind, text in tokens if text != "("), "")
    if first_kind != "word" or leading not in LEADING_KEYWORDS:
        raise QueryNotAllowedError(
            "Only a single SELECT (optionally starting with WITH), SHOW or DESCRIBE "
            f"statement can be run; this query starts with {leading or 'a symbol'!r}."
        )

    # A quoted identifier naming a Lambda-backed catalog ("lambda:<function>") makes
    # Athena invoke that function.
    if any(
        kind == "ident" and text.strip().lower().startswith("lambda:")
        for kind, text in tokens
    ):
        raise QueryNotAllowedError(
            "The query names a Lambda-backed catalog; only the analytics database "
            "can be queried."
        )

    # SHOW and DESCRIBE run on Athena's Hive engine, which reads a backslash inside a
    # literal as an escape. The tokenizer above reads literals the way Trino does, so
    # a backslash is refused in those statements rather than read two ways.
    if leading in ("SHOW", "DESCRIBE") and "\\" in query:
        raise QueryNotAllowedError(
            "SHOW and DESCRIBE statements cannot contain a backslash."
        )

    disallowed = sorted(
        {text for kind, text in tokens if kind == "word"} & REFUSED_KEYWORDS
    )
    if disallowed:
        raise QueryNotAllowedError(
            "The query uses keywords that are not available to the analytics agent: "
            f"{', '.join(disallowed)}. Only read-only queries can be run. If one of "
            'these is a column name, enclose it in double quotes, e.g. "update".'
        )

    return query.strip()
