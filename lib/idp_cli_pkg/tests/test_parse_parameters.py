# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The ``--parameters`` grammar, asserted on the parsed dict key by key.

``idp_cli.parameters.parse_parameters`` is the single implementation behind
``idp-cli deploy --parameters`` and ``idp-feature-cli deploy-pack --parameters``
(the second package holds a byte-identical copy — see
``lib/idp_feature_sdk/tests/test_parameters_copy_in_sync.py``). What it returns
is submitted to CloudFormation as stack parameter overrides.

Every assertion below is on the **whole** returned dict, never on "no exception
was raised". That is the lesson of issue #1220: all three defects it reported were
silent. The parser returned a dict, the deploy proceeded, CloudFormation filled
each unsubmitted parameter in from the template default, and afterwards a
parameter that was dropped looks exactly like one submitted at its default value.
A test that asserts only that parsing did not raise passes against every one of
them.

The table is one list on purpose. The three corrected shapes and the shapes that
already worked are checked by the same assertion, so a fix for one cannot regress
another — the ``=``-in-value fix, for instance, is a change to where a pair ends,
which is exactly what the comma-bearing subnet list depends on.
"""

from __future__ import annotations

import pytest

from idp_cli.parameters import parse_parameters

pytestmark = pytest.mark.unit


# (label, --parameters string, expected dict). The label is what a failure prints.
CASES: list[tuple[str, str | None, dict[str, str]]] = [
    # ---- the three shapes #1220 reported, all silent before the fix ----
    (
        "a space before the = is a quoting slip, not an instruction to drop the pair",
        "LogLevel = DEBUG",
        {"LogLevel": "DEBUG"},
    ),
    (
        "whitespace on only one side of the = is the same slip",
        "LogLevel =DEBUG",
        {"LogLevel": "DEBUG"},
    ),
    (
        "spaced-out pairs in a longer string all survive",
        "Log_Level = DEBUG,Max_Concurrent = 10",
        {"Log_Level": "DEBUG", "Max_Concurrent": "10"},
    ),
    (
        "an underscore is part of the key, not a place to resume matching from",
        "Log_Level=DEBUG",
        {"Log_Level": "DEBUG"},
    ),
    (
        "a leading underscore likewise: CloudFormation will reject the name, loudly, "
        "which beats submitting a different name the operator never typed",
        "_Level=DEBUG",
        {"_Level": "DEBUG"},
    ),
    (
        "a single = inside a value is a value character",
        "Tags=a=b",
        {"Tags": "a=b"},
    ),
    (
        "and so is every later one — a query string carries several",
        "Query=a=b=c",
        {"Query": "a=b=c"},
    ),
    (
        "base64 padding is the everyday case of a value ending in =",
        "Secret=YWJjZA==",
        {"Secret": "YWJjZA=="},
    ),
    (
        "the documented SAML shape: a metadata URL with a query string used to be "
        "truncated at the ? and to invent two parameters from the query",
        "ExternalIdPMetadataURL=https://idp.example.invalid/md?id=a1&v=2",
        {"ExternalIdPMetadataURL": "https://idp.example.invalid/md?id=a1&v=2"},
    ),
    (
        "= in a value and a following pair: the pair boundary is the comma, not the =",
        "Expression=a=1,LogLevel=INFO",
        {"Expression": "a=1", "LogLevel": "INFO"},
    ),
    # ---- shapes that already worked and must keep working ----
    ("the ordinary single pair", "LogLevel=DEBUG", {"LogLevel": "DEBUG"}),
    (
        "two pairs",
        "LogLevel=DEBUG,MaxConcurrent=10",
        {"LogLevel": "DEBUG", "MaxConcurrent": "10"},
    ),
    (
        "a comma-bearing value is ONE value — the whole reason this is not a split(',')",
        "SubnetIds=subnet-a,subnet-b,subnet-c,VpcId=vpc-1",
        {"SubnetIds": "subnet-a,subnet-b,subnet-c", "VpcId": "vpc-1"},
    ),
    (
        "a key present with an empty value is the third case, distinct from absent",
        "LogLevel=",
        {"LogLevel": ""},
    ),
    (
        "a trailing separator is a paste artefact, not part of the value",
        "LogLevel=INFO,",
        {"LogLevel": "INFO"},
    ),
    ("an empty segment between two pairs is skipped", "A=1,,B=2", {"A": "1", "B": "2"}),
    (
        "whitespace around the whole string is noise",
        " LogLevel=DEBUG ",
        {"LogLevel": "DEBUG"},
    ),
    ("whitespace after a separator is noise", "A=1, B=2", {"A": "1", "B": "2"}),
    (
        "a backslash-continued shell string arrives with the newline collapsed away, "
        "and with one that survives the pair boundary still holds",
        "A=1,\n    B=2",
        {"A": "1", "B": "2"},
    ),
    (
        "a repeated key takes its last value, so a scripted base set can be appended to",
        "LogLevel=INFO,LogLevel=DEBUG",
        {"LogLevel": "DEBUG"},
    ),
    (
        "the documented multi-parameter federation string, unchanged by the fix",
        "ExternalIdPType=SAML,"
        "ExternalIdPGroupAttributeName=http://schemas.xmlsoap.org/claims/Group,"
        "ExternalIdPAdminGroupName=IDP-Admins",
        {
            "ExternalIdPType": "SAML",
            "ExternalIdPGroupAttributeName": (
                "http://schemas.xmlsoap.org/claims/Group"
            ),
            "ExternalIdPAdminGroupName": "IDP-Admins",
        },
    ),
    ("an explicit empty string means no overrides", "", {}),
    # ---- shapes that form no pair: reported, not submitted ----
    (
        "a bare key names no value, so there is nothing to submit for it",
        "JustAKey",
        {},
    ),
    ("an empty key names no parameter", "=value", {}),
    (
        "a key CloudFormation could not accept anyway is not silently truncated to "
        "one it would",
        "Log-Level=DEBUG",
        {},
    ),
]


@pytest.mark.parametrize(
    ("parameters", "expected"),
    [pytest.param(text, expected, id=label) for label, text, expected in CASES],
)
def test_the_parsed_dict_is_exactly_this(
    parameters: str | None, expected: dict[str, str]
) -> None:
    assert parse_parameters(parameters) == expected


def test_no_parameters_at_all_is_an_empty_dict() -> None:
    """``None`` is what click passes when the flag is absent, and it must not
    become a dict with one blank key — ``deploy`` submits whatever it gets."""
    assert parse_parameters(None) == {}


def test_a_dropped_key_is_distinguishable_from_one_set_to_the_empty_string() -> None:
    """The distinction the three defects kept collapsing."""
    assert "LogLevel" not in parse_parameters("Other=x")
    assert parse_parameters("LogLevel=") == {"LogLevel": ""}


class TestWhatTheOperatorIsTold:
    """``on_warning`` is the alternative to both silence and a refusal.

    Refusing a shape the previous parser accepted would break scripts that run
    today, so nothing here raises. But input that formed no pair, and input whose
    ``=`` needed whitespace tolerated around it, are both things an operator can
    act on, and neither was mentioned before.
    """

    def _warnings(self, parameters: str | None) -> list[str]:
        collected: list[str] = []
        parse_parameters(parameters, on_warning=collected.append)
        return collected

    @pytest.mark.parametrize(
        "parameters", ["JustAKey", "=value", "Log-Level=DEBUG", "JustAKey,A=1"]
    )
    def test_text_that_formed_no_pair_is_named_back(self, parameters: str) -> None:
        warnings = self._warnings(parameters)
        assert len(warnings) == 1, warnings
        assert "key=value" in warnings[0]
        # The offending text itself, so a long --parameters string can be searched.
        assert parameters.split(",")[0] in warnings[0]

    def test_tolerating_whitespace_around_the_equals_is_reported(self) -> None:
        warnings = self._warnings("LogLevel = DEBUG")
        assert len(warnings) == 1, warnings
        assert "LogLevel" in warnings[0]
        assert "whitespace" in warnings[0]

    def test_every_spaced_key_is_named_in_one_message(self) -> None:
        warnings = self._warnings("A = 1,B = 2,C=3")
        assert len(warnings) == 1, warnings
        assert "A" in warnings[0] and "B" in warnings[0]

    @pytest.mark.parametrize(
        "parameters",
        [
            None,
            "",
            "LogLevel=DEBUG",
            "SubnetIds=subnet-a,subnet-b,VpcId=vpc-1",
            "Query=a=b=c",
            "LogLevel=INFO,",
            "A=1,,B=2",
            " LogLevel=DEBUG ",
            "Log_Level=DEBUG",
        ],
    )
    def test_input_that_parsed_cleanly_says_nothing(
        self, parameters: str | None
    ) -> None:
        """A warning on every ordinary invocation is a warning nobody reads."""
        assert self._warnings(parameters) == []

    def test_a_value_that_reads_as_a_second_pair_is_not_silent_about_it(self) -> None:
        """The one ambiguity the whitespace tolerance introduces, pinned.

        "values may contain commas" and "whitespace may surround the ``=``" cannot
        both be unconditional: the text after the comma here could be more of the
        value or a second pair, and it is read as a second pair — as it already
        was when written without the spaces. It is not silent, which is the point:
        the operator gets told which key that was.
        """
        collected: list[str] = []
        parsed = parse_parameters(
            "Note=hello, world = wide", on_warning=collected.append
        )
        assert parsed == {"Note": "hello", "world": "wide"}
        assert len(collected) == 1, collected
        assert "world" in collected[0]

    def test_nothing_raises_on_any_input_in_the_table(self) -> None:
        """No shape the previous parser accepted became an error.

        Asserted as a property of the table rather than case by case: every entry
        returns, warning or not, so ``deploy`` never exits non-zero on a string it
        used to accept. (The *values* are asserted above; this is only about the
        exit path.)
        """
        for _label, text, _expected in CASES:
            parse_parameters(text, on_warning=lambda _message: None)
