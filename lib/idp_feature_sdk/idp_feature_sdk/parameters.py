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
pair therefore starts only at a comma **or whitespace** that is followed by
``<key> =``, and everything between two such boundaries is one value — commas,
``=`` signs and all. The three shapes below are the ones a naive reading gets
wrong, and each was silently mis-parsed rather than rejected, which is worse: the
deploy proceeded and the stack kept its publish-time defaults (issue #1220).

Whitespace is a boundary as well as a comma because that is what the previous
pattern did, by accident and usefully: it looked for the next ``key=`` at *any*
offset, so ``LogLevel=DEBUG MaxConcurrentWorkflows=200`` parsed as two pairs.
``aws cloudformation deploy`` and ``sam deploy`` take their own parameter
overrides space-separated, so an operator or script carrying that habit here got
the right answer, and a comma-only boundary would have swallowed every pair after
the first into the first one's value — silently, which is the defect class this
module exists to remove rather than to relocate. (Their flag name is deliberately
not spelled out above: ``scripts/tests/test_script_deployed_template_parameters.py``
reads that literal as evidence that a module deploys a stack, which this one does
not, and a substring detector cannot tell prose from a call.)

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

"Values may contain commas and spaces" and "whitespace may surround the ``=``"
cannot both be unconditional: in ``Note=hello, world = wide`` the text after the
comma can be read either as more of the value or as a second pair, and this parser
reads it as a second pair. That is the same trade the previous pattern already
made for ``Note=hello,world=wide`` and ``Note=hello world=wide``, so the shape is
not new — but it is the reason a pair written with whitespace around its ``=`` is
reported through ``on_warning`` rather than accepted in silence.

Text that forms no pair is reported through ``on_warning`` rather than dropped in
silence, in the two places it can appear. Before the first pair (``JustAKey``,
``=value``, ``Log-Level=DEBUG``) it is left out of the result. *Inside* a value it
cannot be — a value may contain commas, so there is no way to tell a swallowed
pair from the value the operator meant — and what is reported there is a
separator followed by something ending in ``=``, which is how a key
CloudFormation would not accept (``,Log-Level=TRACE``) and a pair separated with
``;``, ``|`` or a stray backslash both look from inside a value.

Nothing here raises. Refusing an invocation the previous parser accepted would
break scripts that run today, so every one of these is a printed warning and
every call site prints what it was told.
"""

from __future__ import annotations

import re
from collections.abc import Callable

__all__ = ["PARAMETERS_SYNTAX_HINT", "parse_parameters"]

#: Where a pair begins: the start of the string, a comma, or whitespace, followed
#: by a key and its ``=``. The key is anchored to the boundary — it may not begin
#: in the middle of a token, which is what truncated ``Log_Level`` to ``Level`` —
#: and the character class is CloudFormation's parameter-name alphabet plus ``_``.
_PAIR_START = re.compile(r"(?:\A|[,\s])\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=")

#: A separator and an ``=`` left *inside* a parsed value. Reaching here means the
#: text was not a pair — its key holds a character CloudFormation does not allow,
#: or the pairs were separated with something that is neither a comma nor
#: whitespace. Deliberately narrow: ``?`` and ``&`` are absent, so a query string
#: (``?id=a&v=2``) is a value rather than a warning, which is the shape #1220's
#: third defect was about.
_SWALLOWED_PAIR = re.compile(r"[,;|\\][^,;|]*=")

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
    A trailing separator is a paste artefact and is not kept in the value.

    ``on_warning``, when given, is called once per thing worth telling the
    operator: text before the first pair that formed no pair at all, a value that
    looks like it swallowed one, and pairs whose ``=`` was written with whitespace
    around it. It is never called for input that parsed cleanly, and nothing is
    raised in any of those cases.
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
        # Text before the first pair belongs to no pair at all.
        unparsed = (parameters[: starts[0].start()] if starts else parameters).strip()
        unparsed = unparsed.strip(",").strip()
        if unparsed:
            on_warning(
                f"ignoring {unparsed!r} in --parameters: "
                f"{PARAMETERS_SYNTAX_HINT}. Nothing was submitted for it."
            )
        # Text after the first pair is part of a value by construction. It cannot
        # be taken out of one — a value may contain commas — so a value that looks
        # like it swallowed a pair is named instead, with the key it landed in.
        for key, value in parsed.items():
            swallowed = _SWALLOWED_PAIR.search(value)
            if swallowed:
                on_warning(
                    f"--parameters: {swallowed.group(0)!r} was read as part of the "
                    f"value for {key}, not as another parameter. Separate pairs "
                    "with a comma, and use only letters, digits and underscores "
                    "in a key."
                )
        if spaced:
            on_warning(
                "--parameters: whitespace around '=' was ignored for "
                + ", ".join(spaced)
                + ". Note that a separator followed by '<word> =' is read as the "
                "start of a new parameter rather than as part of a value."
            )

    return parsed
