# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``ConfigOperation.validate`` reports unread keys at every depth, and only real ones.

``deprecated_fields`` and ``unknown_fields`` used to be computed here as
``set(config) - set(IDPConfig.model_fields)``. That set difference sees only the top
level — where a typo is least likely — and knows nothing about the configuration
tree, so it named two keys the loader honours: ``description``, which
``update_configuration`` pops and stores, and ``rule_classes``, which is renamed to
``policy_classes``. Both are now taken from ``validate_config``'s findings, so the
knowledge lives in one place
([#1134](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1134)).
"""

from __future__ import annotations

import logging

import pytest
import yaml

from idp_sdk.operations.config import ConfigOperation

pytestmark = pytest.mark.unit


def _validate(tmp_path, config: dict):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    logging.disable(logging.CRITICAL)
    try:
        return ConfigOperation.validate(None, str(path), pattern="pattern-2")
    finally:
        logging.disable(logging.NOTSET)


def test_a_nested_key_is_reported_by_its_dotted_path(tmp_path):
    result = _validate(
        tmp_path,
        {
            "classes": [{"name": "invoice"}],
            "extraction": {"max_tokens": 4096, "validation": {"enabld": False}},
        },
    )
    assert result.valid is True, result.errors
    assert result.unknown_fields == ["extraction.validation.enabld"]
    assert result.deprecated_fields == ["extraction.max_tokens"]
    joined = "\n".join(result.warnings)
    assert "Did you mean 'extraction.validation.enabled'?" in joined


@pytest.mark.parametrize(
    "key,value",
    [("description", "a profile"), ("rule_classes", [{"name": "policyA"}])],
)
def test_a_key_read_elsewhere_or_renamed_on_load_is_not_listed(tmp_path, key, value):
    result = _validate(tmp_path, {"classes": [{"name": "invoice"}], key: value})
    assert result.unknown_fields == []
    assert result.deprecated_fields == []
    assert [w for w in result.warnings if f"'{key}'" in w] == []


def test_a_top_level_typo_is_listed_once(tmp_path):
    result = _validate(tmp_path, {"classes": [{"name": "invoice"}], "notes_typo": "x"})
    assert result.unknown_fields == ["notes_typo"]
    assert len([w for w in result.warnings if "notes_typo" in w]) == 1, result.warnings
