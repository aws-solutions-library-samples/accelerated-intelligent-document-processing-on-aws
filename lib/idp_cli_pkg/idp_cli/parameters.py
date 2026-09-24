# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The ``--parameters key=value,key2=value2`` grammar, in one place.

Two commands accept that flag and hand the result to CloudFormation as stack
parameter overrides: ``idp-cli deploy`` and ``idp-feature-cli deploy-pack``.
They live in separate distributions — ``idp-accelerator-cli`` and
``idp-feature-sdk`` — and the Feature Platform SDK deliberately declares no
first-party requirements (see ``docs/dependency-confusion.md``), so it cannot
import this module from the CLI package. This file is therefore committed twice,
**byte-identically**:

* ``lib/idp_cli_pkg/idp_cli/parameters.py``
* ``lib/idp_feature_sdk/idp_feature_sdk/parameters.py``

``lib/idp_feature_sdk/tests/test_parameters_copy_in_sync.py`` compares the two
files and fails if they differ, naming the ``cp`` that re-syncs them. That guard
is the point: the two commands previously carried the same parsing regex written
out twice, and the same three defects reached both because nothing compared them.

What the grammar has to do
--------------------------

A value may legitimately contain a comma (a subnet or security-group list is the
motivating case), so the string cannot simply be split on every comma. A new
pair therefore starts only at a comma that is followed by ``<key> =``, and
everything between two such boundaries is one value — commas, ``=`` signs and
all. The three shapes below are the ones a naive reading gets wrong, and each was
silently mis-parsed rather than rejected, which is worse: the deploy proceeded and
the stack kept its publish-time defaults (issue #1220).

* ``LogLevel = DEBUG`` — whitespace around the ``=`` is tolerated. It is a
  shell-quoting slip, not an instruction, so it is read as the pair the operator
  meant. ``on_warning`` says so, because this is also the one tolerance that can
  change how an unusual *value* is read (see "Ambiguity" below).
* ``Log_Level=DEBUG`` — an underscore is part of the key, not a place to resume
  matching from. Truncating the key to ``Level`` submitted a parameter name the
  operator never typed.
* ``MetadataURL=https://host/md?id=a&v=2`` — an ``=`` inside a value is a value
  character. Splitting on it truncated the URL at the ``?`` *and* invented two
  parameters from its query string.

Ambiguity
---------

"Values may contain commas" and "whitespace may surround the ``=``" cannot both
be unconditional: in ``Note=hello, world = wide`` the text after the comma can be
read either as more of the value or as a second pair, and this parser reads it as
a second pair. That is the same trade the tight ``key=`` boundary already made
for ``Note=hello,world=wide``, so the shape is not new — but it is the reason a
pair written with whitespace around its ``=`` is reported through ``on_warning``
rather than accepted in silence.

Anything that is not part of a pair (``JustAKey``, ``=value``, a key with a
character CloudFormation does not allow in a parameter name, such as
``Log-Level=DEBUG``) is reported through ``on_warning`` and left out of the
result. Nothing here raises: refusing an invocation the previous parser accepted
would break scripts, and every call site prints what it was told.
"""

from __future__ import annotations

import re
from collections.abc import Callable

__all__ = ["PARAMETERS_SYNTAX_HINT", "parse_parameters"]

#: Where a pair begins: the start of the string, or a comma, followed by a key
#: and its ``=``. The key is anchored to the boundary — it may not begin in the
#: middle of a token, which is what truncated ``Log_Level`` to ``Level`` — and
#: the character class is CloudFormation's parameter-name alphabet plus ``_``.
_PAIR_START = re.compile(r"(?:\A|,)\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=")

#: Quoted back to the operator whenever something was not understood.
PARAMETERS_SYNTAX_HINT = "expected key=value,key2=value2"


def parse_parameters(
    parameters: str | None,
    *,
    on_warning: Callable[[str], None] | None = None,
) -> dict[str, str]:
    """Parse a ``--parameters`` string into ``{key: value}``.

    ``None`` and ``""`` both mean "no overrides" and give ``{}``. A repeated key
    takes its last value, so a scripted base set can be overridden by appending.
    A single trailing separator is a paste artefact and is not kept in the value.

    ``on_warning``, when given, is called once per thing worth telling the
    operator: text that formed no pair at all, and pairs whose ``=`` was written
    with whitespace around it. It is never called for input that parsed cleanly,
    and nothing is raised in either case.
    """
    if not parameters:
        return {}

    starts = list(_PAIR_START.finditer(parameters))
    parsed: dict[str, str] = {}
    spaced: list[str] = []

    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(parameters)
        value = parameters[match.end() : end].strip()
        while value.endswith(","):
            value = value[:-1].rstrip()
        key = match.group("key")
        parsed[key] = value
        if parameters[match.end("key") : match.end()] != "=":
            spaced.append(key)

    if on_warning is not None:
        # Text before the first pair belongs to no pair. Text *after* one is part
        # of that pair's value by construction, because a value may contain
        # commas, so there is nothing to report there.
        unparsed = (parameters[: starts[0].start()] if starts else parameters).strip()
        unparsed = unparsed.strip(",").strip()
        if unparsed:
            on_warning(
                f"ignoring {unparsed!r} in --parameters: "
                f"{PARAMETERS_SYNTAX_HINT}. Nothing was submitted for it."
            )
        if spaced:
            on_warning(
                "--parameters: whitespace around '=' was ignored for "
                + ", ".join(spaced)
                + ". Note that a value containing ', <word> = ' is read as the "
                "start of a new parameter rather than as part of the value."
            )

    return parsed
