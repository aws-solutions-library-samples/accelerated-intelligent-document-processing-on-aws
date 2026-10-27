# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Where the stack's ``CustomClassificationModelARN`` lands in a 0.9 config.

The custom-resource handler merges the shipped preset with the system defaults
(which migrates it to the nested shape) and then writes the ARN. The ARN is a
model trained to assign a class, so it goes to the call that assigns the class
under the active strategy and nowhere else — boundary detection keeps its own model.
"""

import copy
import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from idp_common.config.merge_utils import merge_config_with_defaults
from idp_common.config.migrations import migrate_config
from idp_common.config.models import IDPConfig

_REPO_ROOT = Path(__file__).resolve().parents[5]
_LAMBDA_DIR = _REPO_ROOT / "src" / "lambda" / "update_configuration"

ARN = "arn:aws:bedrock:us-west-2:123456789012:custom-model-deployment/abc123"


@pytest.fixture
def update_config_module(monkeypatch):
    sys.modules.setdefault("cfnresponse", MagicMock())
    monkeypatch.syspath_prepend(str(_LAMBDA_DIR))
    sys.modules.pop("index", None)
    module = importlib.import_module("index")
    yield module
    sys.modules.pop("index", None)


def _merged(classification):
    return merge_config_with_defaults(
        {"classification": classification}, validate=False
    )


@pytest.mark.unit
class TestCustomClassificationModelArn:
    def test_single_pass_writes_the_one_stage_call(self, update_config_module):
        config = _merged({"strategy": "single_pass"})
        update_config_module.apply_custom_model_arns(
            config, {"CustomClassificationModelARN": ARN}
        )
        resolved = IDPConfig(**config).classification
        assert resolved.one_stage.llm.model == ARN
        assert resolved.two_stage.boundary_detection.llm.model != ARN
        assert resolved.two_stage.section_classification.llm.model != ARN

    def test_two_stage_writes_section_classification_only(self, update_config_module):
        config = _merged({"strategy": "two_stage"})
        update_config_module.apply_custom_model_arns(
            config, {"CustomClassificationModelARN": ARN}
        )
        resolved = IDPConfig(**config).classification
        assert resolved.two_stage.section_classification.llm.model == ARN
        assert resolved.two_stage.boundary_detection.llm.model != ARN
        assert resolved.one_stage.llm.model != ARN

    def test_a_flat_preset_lands_in_the_migrated_call(self, update_config_module):
        """A 0.6.12-shaped preset (flat keys) is migrated by the merge, so the ARN
        still reaches the call that runs — not a flat `model` key nobody reads."""
        config = _merged({"model": "us.amazon.nova-lite-v1:0", "granularity": "page"})
        update_config_module.apply_custom_model_arns(
            config, {"CustomClassificationModelARN": ARN}
        )
        assert "model" not in config["classification"]
        assert IDPConfig(**config).classification.one_stage.llm.model == ARN

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_an_empty_arn_changes_nothing(self, update_config_module, value):
        config = _merged({})
        before = IDPConfig(**config).classification.model_dump()
        update_config_module.apply_custom_model_arns(
            config, {"CustomClassificationModelARN": value}
        )
        assert IDPConfig(**config).classification.model_dump() == before

    def test_the_extraction_arn_is_still_applied(self, update_config_module):
        config = _merged({})
        update_config_module.apply_custom_model_arns(
            config, {"CustomExtractionModelARN": ARN}
        )
        assert config["extraction"]["model"] == ARN


@pytest.mark.unit
class TestMergeFallbackMigrates:
    """When the merge with defaults fails, the unmerged config is still migrated.

    Returned raw, a flat preset keeps ``classification.model``; the ARN writer
    then adds ``one_stage.llm.model`` beside it, and on the next read the flat
    key wins the hybrid, so the deployed custom model is silently dropped.
    """

    @pytest.mark.parametrize(
        "error", [FileNotFoundError("no defaults"), ValueError("x")]
    )
    def test_the_arn_survives_a_failed_merge(
        self, update_config_module, monkeypatch, error
    ):
        def fail(*args, **kwargs):
            raise error

        monkeypatch.setattr(update_config_module, "merge_config_with_defaults", fail)
        custom = {
            "config_format_version": "0.8",
            "classification": {"model": "us.amazon.nova-lite-v1:0"},
        }

        config = update_config_module.merge_custom_with_defaults(
            custom, pattern="pattern-2"
        )
        update_config_module.apply_custom_model_arns(
            config, {"CustomClassificationModelARN": ARN}
        )

        assert "model" not in config["classification"]
        reread = migrate_config(copy.deepcopy(config))
        assert reread["classification"]["one_stage"]["llm"]["model"] == ARN
        # The caller's dict is not mutated by the fallback.
        assert custom["classification"] == {"model": "us.amazon.nova-lite-v1:0"}
