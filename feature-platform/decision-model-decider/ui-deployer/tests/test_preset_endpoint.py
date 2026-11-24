# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The installed profile selects the extension's function for section classification.

The ui-deployer fills the function's alias ARN into the bundled preset before it
hands the preset to the host. These tests run the shipped preset file through the
handler's own ``fill_endpoint`` and then through the accelerator's configuration
validator, the same models the host's ``applyFeatureConfigPreset`` stores against,
so a preset key the models do not read, or an endpoint written at the wrong depth,
fails here rather than at install.
"""

from __future__ import annotations

import copy
import importlib
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

_FEATURE_DIR = Path(__file__).resolve().parents[2]
_HANDLER_DIR = _FEATURE_DIR / "ui-deployer"
_PRESET = _FEATURE_DIR / "config-preset" / "section-classification.yaml"
_MANIFEST = _FEATURE_DIR / "feature.yaml"
_TEMPLATE = _FEATURE_DIR / "template.yaml"

ALIAS_ARN = "arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-decider:live"


@pytest.fixture
def handler(monkeypatch):
    for name, value in {
        "FEATURE_ID": "decision-model-decider",
        "FEATURE_DISPLAY_NAME": "Decision Model: Strands Decider",
        "FEATURE_VERSION": "0.1.0",
        "WEBUI_BUCKET": "webui",
        "FEATURE_BUCKET": "artifacts",
        "FEATURE_ARTIFACT_PREFIX": "idp/extensions/decision-model-decider",
        "REGISTER_FEATURE_FUNCTION_ARN": "arn:aws:lambda:us-east-1:1:function:R",
        "APPLY_FEATURE_CONFIG_PRESET_FUNCTION_ARN": "arn:aws:lambda:us-east-1:1:function:A",
        "DECIDER_ENDPOINT": ALIAS_ARN,
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)
    sys.path.insert(0, str(_HANDLER_DIR))
    sys.modules.pop("handler", None)
    try:
        yield importlib.import_module("handler")
    finally:
        sys.path.remove(str(_HANDLER_DIR))
        sys.modules.pop("handler", None)


def _preset() -> dict:
    return yaml.safe_load(_PRESET.read_text(encoding="utf-8"))


def _validate(config: dict) -> dict:
    from idp_common.config.merge_utils import validate_config

    return validate_config(copy.deepcopy(config))


def test_the_manifest_names_the_preset_the_handler_reads(handler):
    manifest = yaml.safe_load(_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["configPreset"]["path"] == handler._CONFIG_PRESET_RELATIVE_KEY
    assert (_FEATURE_DIR / manifest["configPreset"]["path"]).is_file()


def test_the_filled_preset_selects_the_decision_model_at_the_endpoint(handler):
    filled = handler.fill_endpoint(_preset(), ALIAS_ARN)
    result = _validate(filled)
    assert result["valid"], result["errors"]
    assert not result.get("ignored_keys"), result["ignored_keys"]
    from idp_common.config.models import IDPConfig

    config = IDPConfig.model_validate(result["merged_config"])
    stc = config.classification.split_then_classify
    assert config.classification.strategy == "split_then_classify"
    assert stc.section_classification.method == "decision_model"
    assert stc.section_classification.decision_model.endpoint == ALIAS_ARN
    # Images, because the function is built with the vision tower by default and
    # page images measured best; a text-only preset would read OCR text alone.
    assert stc.section_classification.decision_model.input == "images"


def test_the_unfilled_preset_is_refused(handler):
    """The shipped file carries no endpoint, so it is valid only once filled."""
    result = _validate(_preset())
    assert not result["valid"]
    assert any("decision_model.endpoint is empty" in e for e in result["errors"]), (
        result["errors"]
    )


def test_fill_keeps_every_other_setting_and_overwrites_a_stale_endpoint(handler):
    preset = _preset()
    preset["classification"]["split_then_classify"]["section_classification"][
        "decision_model"
    ]["endpoint"] = "arn:aws:lambda:us-east-1:1:function:GENAIIDP-old:live"
    expected = copy.deepcopy(preset)
    expected["classification"]["split_then_classify"]["section_classification"][
        "decision_model"
    ]["endpoint"] = ALIAS_ARN
    assert handler.fill_endpoint(preset, ALIAS_ARN) == expected


def test_fill_creates_the_path_in_an_empty_preset(handler):
    filled = handler.fill_endpoint({}, ALIAS_ARN)
    node = filled
    for key in handler.ENDPOINT_PATH:
        node = node[key]
    assert node == ALIAS_ARN


def test_an_empty_endpoint_fails_the_install(handler):
    with pytest.raises(RuntimeError, match="DECIDER_ENDPOINT is empty"):
        handler.fill_endpoint(_preset(), "")


def test_the_template_hands_the_alias_arn_to_the_handler():
    """The endpoint is the alias, which the client accepts and meters by name."""
    loader = _FEATURE_DIR.parents[1] / "lib" / "idp_sdk" / "idp_sdk" / "_core"
    spec = importlib.util.spec_from_file_location(
        "_cfn_yaml_for_decider_ui_deployer", loader / "cfn_yaml.py"
    )
    assert spec and spec.loader
    cfn_yaml = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfn_yaml)
    template = cfn_yaml.load_cfn_yaml(_TEMPLATE.read_text(encoding="utf-8"))
    resources = template["Resources"]
    env = resources["UiDeployerFunction"]["Properties"]["Environment"]["Variables"]
    assert env["DECIDER_ENDPOINT"] == {"!Ref": "DeciderFunctionAlias"}
    assert resources["DeciderFunctionAlias"]["Type"] == "AWS::Lambda::Alias"
    # A change of endpoint re-runs the custom resource, which re-applies the preset.
    props = resources["RegisterFeatureResource"]["Properties"]
    assert props["DeciderEndpoint"] == {"!Ref": "DeciderFunctionAlias"}


def test_the_decision_model_client_accepts_the_alias_arn():
    from idp_common.decision_model import invoker

    assert invoker.function_name(ALIAS_ARN) == "GENAIIDP-decider"
    client = invoker.DecisionModelClient(endpoint=ALIAS_ARN, lambda_client=object())
    assert client.metering_key("SectionClassification") == (
        "SectionClassification/decision_model/GENAIIDP-decider"
    )
