# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The ``--parameters`` parser is committed twice, and must stay one file.

``idp-cli deploy`` and ``idp-feature-cli deploy-pack`` accept the same
``--parameters key=value,key2=value2`` flag and both hand the result to
CloudFormation. They are separate distributions, and this one — ``idp-feature-sdk``
— deliberately declares **no first-party requirements**: the names of the
in-repository packages are squatted on public PyPI, so a bare requirement on one of
them is a dependency-confusion surface (``docs/dependency-confusion.md``), and the
documented install for this package is a single ``pip install -e
lib/idp_feature_sdk``. Importing the CLI package's module is therefore not
available, and the grammar is committed twice instead:

* ``lib/idp_cli_pkg/idp_cli/parameters.py`` — the canonical file
* ``lib/idp_feature_sdk/idp_feature_sdk/parameters.py`` — a byte-identical copy

That is the same guarded-vendoring shape as the committed copies of
``idp_common``'s log sanitizer under the Lambda trees, and it exists here for a
measured reason. The two commands previously each carried the parsing regex written
out inline, character for character the same, and the three defects of issue #1220
reached both of them: a space before the ``=`` dropped the pair, an underscore in
the key truncated it, and an ``=`` in the value split it into two parameters. Every
one was silent. Nothing compared the copies, so fixing either alone would have left
the other wrong — which is the failure this file is here to prevent, not the
parsing behaviour itself (that is asserted in ``test_cli_argument_resolution.py``
and, in full, in ``lib/idp_cli_pkg/tests/test_parse_parameters.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from idp_feature_sdk import parameters as vendored

pytestmark = pytest.mark.unit

#: ``lib/idp_feature_sdk/tests/<this file>`` → ``lib/``.
_LIB = Path(__file__).resolve().parents[2]

CANONICAL = _LIB / "idp_cli_pkg" / "idp_cli" / "parameters.py"
COPY = _LIB / "idp_feature_sdk" / "idp_feature_sdk" / "parameters.py"

RESYNC = f"cp {CANONICAL.relative_to(_LIB.parent)} {COPY.relative_to(_LIB.parent)}"


def test_both_files_are_present() -> None:
    """The premise of the comparison below, asserted rather than assumed.

    A missing path would otherwise make the byte comparison unrunnable, and the
    tempting repair for that — skip when the sibling package is absent — is how a
    guard goes quiet: this suite runs from the repository, where both files are
    tracked, so neither can be missing for a legitimate reason.
    """
    assert CANONICAL.is_file(), CANONICAL
    assert COPY.is_file(), COPY


def test_the_copy_is_byte_identical_to_the_canonical_file() -> None:
    assert COPY.read_bytes() == CANONICAL.read_bytes(), (
        "the two --parameters parsers have drifted apart, which is the defect "
        f"class of #1220. Re-sync with:\n    {RESYNC}"
    )


def test_this_package_imports_its_own_copy_and_not_the_other_one() -> None:
    """Byte-identity is only interesting if the copy is what actually runs.

    Both files define the same names, so an accidental import of the CLI
    package's module would make every behavioural assertion in this suite pass
    while the shipped wheel — which contains no such module — carried whatever the
    copy happened to say.
    """
    assert Path(vendored.__file__).resolve() == COPY


def test_the_copy_is_the_parser_the_cli_calls() -> None:
    """``deploy-pack`` must reach the vendored module, not a private reimplementation.

    The inline copy this replaced is exactly what let the defect exist in two
    places, so a second implementation appearing in ``cli.py`` is the regression
    worth naming here.
    """
    import inspect

    from idp_feature_sdk import cli

    assert cli.parse_parameters is vendored.parse_parameters
    assert "re.finditer" not in inspect.getsource(cli)


def test_the_vendored_copy_reads_the_three_shapes_correctly() -> None:
    """Exercised through the copy, so the file that ships is the one measured."""
    assert vendored.parse_parameters("LogLevel = DEBUG") == {"LogLevel": "DEBUG"}
    assert vendored.parse_parameters("Log_Level=DEBUG") == {"Log_Level": "DEBUG"}
    assert vendored.parse_parameters("Query=a=b=c") == {"Query": "a=b=c"}
    # And the two properties those three must not cost: a value may contain
    # commas, and whitespace separates pairs as well as a comma does.
    assert vendored.parse_parameters("Ids=a,b,c,Vpc=v1") == {
        "Ids": "a,b,c",
        "Vpc": "v1",
    }
    assert vendored.parse_parameters("Log=DEBUG Max=10") == {
        "Log": "DEBUG",
        "Max": "10",
    }
