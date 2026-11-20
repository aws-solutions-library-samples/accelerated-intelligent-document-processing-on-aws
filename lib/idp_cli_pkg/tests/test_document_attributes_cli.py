# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`--attribute` / `--attributes-file` on `process` and `run-inference` (#1851).

The flags must reach `batch.process(attributes=...)` with the merged, validated
set, and an invalid set must be refused before the SDK is reached, so nothing is
uploaded. Without either flag the SDK call carries no attributes at all.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from idp_cli.cli import cli


def _invoke(command, *extra):
    with patch("idp_sdk.IDPClient") as mock_client_cls:
        client = MagicMock()
        mock_client_cls.return_value = client
        result = CliRunner().invoke(
            cli, [command, "--stack-name", "s", "--dir", ".", *extra]
        )
    return result, client


@pytest.mark.unit
@pytest.mark.parametrize("command", ["process", "run-inference"])
def test_attributes_reach_the_sdk_with_pairs_over_the_file(command, tmp_path):
    attrs_file = tmp_path / "a.json"
    attrs_file.write_text(json.dumps({"account_id": "FILE", "amount": 3}))
    result, client = _invoke(
        command,
        "--attributes-file",
        str(attrs_file),
        "--attribute",
        "account_id=ACME-0042",
        "--attribute",
        "region=eu",
    )
    assert client.batch.process.called, result.output
    assert client.batch.process.call_args.kwargs["attributes"] == {
        "account_id": "ACME-0042",
        "amount": 3,
        "region": "eu",
    }


@pytest.mark.unit
def test_without_the_flags_no_attributes_are_sent():
    _result, client = _invoke("process")
    assert "attributes" not in client.batch.process.call_args.kwargs


@pytest.mark.unit
def test_pairs_split_on_the_first_equals_and_may_be_empty():
    _result, client = _invoke("process", "--attribute", "b=x=y", "--attribute", "c=")
    assert client.batch.process.call_args.kwargs["attributes"] == {"b": "x=y", "c": ""}


@pytest.mark.unit
def test_rule_validation_is_left_to_the_sdk():
    """The CLI checks input syntax only; the SDK refuses a set that breaks the
    attribute rules (tests/unit/test_document_attributes_sdk.py in idp_sdk)."""
    _result, client = _invoke("process", "--attribute", "Bad=x")
    assert client.batch.process.call_args.kwargs["attributes"] == {"Bad": "x"}


@pytest.mark.unit
@pytest.mark.parametrize(
    "extra",
    [
        ("--attribute", "novalue"),
        ("--attribute", "a=1", "--attribute", "a=2"),
    ],
)
def test_malformed_input_is_refused_before_the_sdk_is_reached(extra):
    result, client = _invoke("process", *extra)
    assert result.exit_code == 1
    assert "Error" in result.output
    client.batch.process.assert_not_called()


@pytest.mark.unit
def test_a_non_object_attributes_file_is_refused(tmp_path):
    attrs_file = tmp_path / "a.json"
    attrs_file.write_text("[1, 2]")
    result, client = _invoke("process", "--attributes-file", str(attrs_file))
    assert result.exit_code == 1
    client.batch.process.assert_not_called()


@pytest.mark.unit
def test_attributes_with_a_test_set_are_refused():
    with patch("idp_sdk.IDPClient") as mock_client_cls:
        result = CliRunner().invoke(
            cli,
            ["process", "--stack-name", "s", "--test-set", "ts", "--attribute", "a=b"],
        )
    assert result.exit_code == 1
    assert "--test-set" in result.output
    mock_client_cls.return_value.batch.process.assert_not_called()


@pytest.mark.unit
def test_whitespace_around_a_pair_key_is_stripped():
    _result, client = _invoke("process", "--attribute", " account_id =A")
    assert client.batch.process.call_args.kwargs["attributes"] == {"account_id": "A"}
