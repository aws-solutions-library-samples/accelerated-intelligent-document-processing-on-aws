# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
The vocabulary of the SMT-LIB subset rule constraints are written in.

Two places have to agree about what a constraint may contain. ``Z3Validator``
parses one at solve time, and ``RuleJSON`` checks one at construction time so that
a bad translation is rejected before it is persisted and re-read on every later
document (GitHub issue #1058). Sharing one definition is what keeps the second
from becoming stricter or laxer than the first by accident, which is the only way
a construction-time check can be worse than no check at all.

This module holds the part of that definition that needs no solver:
the tokeniser and the operator and boolean-literal vocabularies. It deliberately
does **not** import ``z3``. ``RuleJSON`` is constructed on the configuration
resolver Lambda, whose layer carries ``idp_common`` without the ``rule_validation``
extra and therefore without ``z3-solver`` — the reason
``idp_common.rule_validation.z3.__init__`` imports the solver-dependent modules
lazily. Anything added here must keep that property.

**What a constraint token may be.** After tokenising, a token sits in one of two
positions: the *head* of an S-expression (the token straight after a ``(``) or an
*argument*. A head must name a supported operator. An argument may be a declared
parameter, a boolean literal, a quoted string, or a numeral.

**Why the check is written in terms of identifier shape.** ``constraint_problems``
reports an argument that *looks like a name* — matches ``[A-Za-z_][A-Za-z0-9_]*``
— and is neither declared nor a boolean literal. It says nothing about a token of
any other shape, which leaves numerals out of its scope entirely: ``0.05``,
``-3`` and ``1.5e3`` are not identifier-shaped, so there is no numeral rule here
to drift from the one ``Z3Validator._parse_smt_atom`` applies.

The two shapes overlap in exactly one place: ``nan``, ``inf`` and ``infinity`` are
identifier-shaped and are also spellings Python's numeric parsers accept. They are
reported here as undeclared references, which is the same answer the numeric
coercion a *reading* goes through gives — Z3 has no sort for a non-finite value, so
a constraint naming one could not be evaluated whichever way it were classified.

What this module does **not** check is structure: unbalanced parentheses, a
constraint holding two S-expressions, and operator arity are all
``Z3Validator._parse_smt_constraint``'s business and are reported at solve time.
A token that is neither identifier-shaped nor a valid numeral — ``3x``, ``1/3`` —
likewise reaches the solver, which rejects it. The class this module is about is
the misspelled parameter name.
"""

import re
from typing import Iterable, List

# Every operator Z3Validator._apply_smt_operator dispatches on. That method gates
# on this set before dispatching, so a name here that it cannot handle raises its
# "Unsupported operator" error just as a name absent from both would, and a name
# it handles but that is missing here is refused outright rather than silently
# accepted on one side only.
OPERATORS = frozenset(
    {
        # Arithmetic
        "+",
        "-",
        "*",
        "/",
        "mod",
        "%",
        # Comparison
        "=",
        "<",
        ">",
        "<=",
        ">=",
        "!=",
        "distinct",
        # Logical
        "and",
        "or",
        "not",
        "implies",
        "=>",
        "ite",
    }
)

# The two atoms that are values rather than references.
BOOLEAN_LITERALS = frozenset({"true", "false"})

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

__all__ = [
    "BOOLEAN_LITERALS",
    "OPERATORS",
    "constraint_problems",
    "is_identifier",
    "tokenize",
]


def tokenize(source: str) -> List[str]:
    """
    Tokenize an SMT-LIB expression string.

    Converts ``"(>= x 10)"`` into ``["(", ">=", "x", "10", ")"]``. Quoted strings
    stay one token, quotes included: ``'(= name "John Doe")'`` gives
    ``["(", "=", "name", '"John Doe"', ")"]``. That is what keeps a word inside a
    string literal from being read as a parameter reference, which a regular
    expression over the raw text cannot do.

    Args:
        source: SMT-LIB expression string.

    Returns:
        List of tokens.
    """
    tokens: List[str] = []
    i = 0

    while i < len(source):
        # Skip whitespace
        if source[i].isspace():
            i += 1
            continue

        # Handle opening parenthesis
        if source[i] == "(":
            tokens.append("(")
            i += 1
            continue

        # Handle closing parenthesis
        if source[i] == ")":
            tokens.append(")")
            i += 1
            continue

        # Handle quoted strings
        if source[i] == '"':
            # Find the closing quote
            j = i + 1
            while j < len(source) and source[j] != '"':
                # Handle escaped quotes if needed
                if source[j] == "\\" and j + 1 < len(source):
                    j += 2
                else:
                    j += 1

            if j < len(source):
                # Include the quotes in the token
                tokens.append(source[i : j + 1])
                i = j + 1
            else:
                # Unclosed quote - treat as regular token
                j = i + 1
                while (
                    j < len(source)
                    and not source[j].isspace()
                    and source[j] not in "()"
                ):
                    j += 1
                tokens.append(source[i:j])
                i = j
            continue

        # Handle regular tokens (operators, variables, numbers)
        j = i
        while j < len(source) and not source[j].isspace() and source[j] not in '()"':
            j += 1

        if j > i:
            tokens.append(source[i:j])
            i = j
        else:
            i += 1

    return tokens


def is_identifier(token: str) -> bool:
    """Whether a token has the shape of a parameter name."""
    return _IDENTIFIER.match(token) is not None


def constraint_problems(constraint: str, declared: Iterable[str]) -> List[str]:
    """
    Everything resolvable about a constraint that does not resolve.

    Args:
        constraint: One SMT-LIB constraint string.
        declared: The parameter names the rule declares.

    Returns:
        A list of one-line problem descriptions, empty when nothing is wrong.
        Each names the offending token, because the whole value of catching this
        at construction time is being told which token to fix. The list is in
        token order and holds each **distinct problem** once, so a constraint with
        two different misspellings names both rather than only the first, while one
        misspelling used twice is reported once.
    """
    names = set(declared)
    tokens = tokenize(constraint)
    problems: List[str] = []
    seen: set = set()

    for position, token in enumerate(tokens):
        if token in ("(", ")"):
            continue

        head = position > 0 and tokens[position - 1] == "("
        if head:
            if token not in OPERATORS:
                problem = (
                    f"'{token}' is not a supported operator "
                    f"(expected one of: {', '.join(sorted(OPERATORS))})"
                )
                if problem not in seen:
                    seen.add(problem)
                    problems.append(problem)
            continue

        # Argument position. Only identifier-shaped tokens are claims about a
        # parameter; a numeral or a quoted string is a value.
        if not is_identifier(token):
            continue
        if token in names or token in BOOLEAN_LITERALS:
            continue

        problem = f"'{token}' is not a declared parameter"
        if problem not in seen:
            seen.add(problem)
            problems.append(problem)

    return problems
