# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`config.upload` returns the warnings validation produced for the file.

`ConfigUploadResult.warnings` existed but nothing populated it, so every finding
`validate_config` reports for a valid configuration (an unread key with the field it
was meant to be, a prompt-cache finding, ...) was dropped on the upload path and
reached only callers of `config.validate`. These tests run the real validation over
a real file and compare against what `config.validate` says for the same file, so
the expectation is derived from the validator rather than restated here. Only the
DynamoDB write is mocked.
"""

from unittest.mock import patch

import pytest

from idp_sdk import IDPClient

#: Valid, and carries an unread nested key: `ocr.dpi` is not a field, the model's
#: field is `ocr.image.dpi`, so validation warns and names it.
_VALID_WITH_UNREAD_KEY = """\
classes:
  - name: invoice
    description: An invoice
ocr:
  dpi: 150
"""

#: The same unread key, plus a model id no pricing entry knows, which is an error.
_INVALID_WITH_UNREAD_KEY = """\
extraction:
  model: us.anthropic.claude-nope-v9:0
classes:
  - name: invoice
    description: An invoice
ocr:
  dpi: 150
"""

_STACK_RESOURCES = [
    {
        "StackResourceSummaries": [
            {
                "LogicalResourceId": "ConfigurationTable",
                "PhysicalResourceId": "test-table",
            }
        ]
    }
]


@pytest.fixture(autouse=True)
def _contain_the_bridged_environment(monkeypatch):
    """Restore every variable `upload` writes, including its absence.

    `_configure_config_env` sets the configuration table and bucket and both
    Region variables with a bare `os.environ[...] = ...`. Setting each through
    `monkeypatch` first makes it record the prior state, so teardown undoes the
    write; without that, a later test in the same worker runs with this file's
    `us-east-1` in `AWS_REGION` and `AWS_DEFAULT_REGION`.
    """
    for name in (
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "CONFIGURATION_TABLE_NAME",
        "CONFIGURATION_BUCKET",
        "STACK_NAME",
    ):
        monkeypatch.setenv(name, "set-by-fixture-so-teardown-restores-it")
        monkeypatch.delenv(name)


def _upload(tmp_path, text, **kwargs):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    with (
        patch("boto3.client") as mock_boto3,
        patch(
            "idp_common.config.configuration_manager.ConfigurationManager"
        ) as mock_manager_class,
    ):
        mock_boto3.return_value.get_paginator.return_value.paginate.return_value = (
            _STACK_RESOURCES
        )
        manager = mock_manager_class.return_value
        manager.get_configuration.return_value = None
        manager.handle_update_custom_configuration.return_value = True
        manager.resolve_published_revision.return_value = 1
        client = IDPClient(stack_name="test-stack", region="us-east-1")
        result = client.config.upload(
            config_file=str(path), config_profile="p1", **kwargs
        )
        return result, manager, client.config.validate(str(path))


@pytest.mark.unit
def test_a_successful_upload_returns_the_warnings_validate_reports(tmp_path):
    result, manager, validated = _upload(tmp_path, _VALID_WITH_UNREAD_KEY)

    assert result.success is True, result.error
    manager.handle_update_custom_configuration.assert_called_once()
    assert validated.valid is True
    assert validated.warnings, "fixture must produce warnings or this proves nothing"
    assert result.warnings == validated.warnings
    assert any("ocr.image.dpi" in w for w in result.warnings), result.warnings


@pytest.mark.unit
def test_a_refused_upload_still_returns_the_warnings(tmp_path):
    result, manager, validated = _upload(tmp_path, _INVALID_WITH_UNREAD_KEY)

    assert result.success is False
    assert result.error and result.error.startswith("Validation failed")
    manager.handle_update_custom_configuration.assert_not_called()
    assert validated.valid is False
    assert any("ocr.image.dpi" in w for w in validated.warnings), validated.warnings
    assert result.warnings == validated.warnings


@pytest.mark.unit
def test_an_unvalidated_upload_reports_no_warnings(tmp_path):
    result, manager, validated = _upload(
        tmp_path, _VALID_WITH_UNREAD_KEY, validate=False
    )

    assert result.success is True, result.error
    assert validated.warnings, "the file does warn when it is checked"
    assert result.warnings == []


@pytest.mark.unit
def test_warnings_survive_a_failed_write(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(_VALID_WITH_UNREAD_KEY, encoding="utf-8")
    with (
        patch("boto3.client") as mock_boto3,
        patch(
            "idp_common.config.configuration_manager.ConfigurationManager"
        ) as mock_manager_class,
    ):
        mock_boto3.return_value.get_paginator.return_value.paginate.return_value = (
            _STACK_RESOURCES
        )
        manager = mock_manager_class.return_value
        manager.get_configuration.return_value = None
        manager.handle_update_custom_configuration.side_effect = RuntimeError("boom")
        client = IDPClient(stack_name="test-stack", region="us-east-1")
        result = client.config.upload(config_file=str(path), config_profile="p1")

    assert result.success is False
    assert result.error == "boom"
    assert any("ocr.image.dpi" in w for w in result.warnings), result.warnings
