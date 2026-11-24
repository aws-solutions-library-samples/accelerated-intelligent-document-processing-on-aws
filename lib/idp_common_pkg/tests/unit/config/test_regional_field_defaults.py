# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An absent model key resolves to the stack's Region's profile, as a cleared one does.

A configuration model field declares the US inference profile the system defaults
ship. The deploy-time swap in ``src/lambda/update_configuration`` rewrites a stored
value for the stack's Region, but a key the stored configuration does not carry is
filled from the declaration while the configuration is read, after the swap, so an
EU stack sent a ``us.`` profile that Bedrock rejects there. A cleared classification
model was already translated (``test_classification_default_region.py``); an
absent one was not.

The expected ids are derived from
:data:`idp_common.regional_models.REGIONAL_DEFAULT_MODELS`, whose rows
``src/lambda/update_configuration/test_eu_region.py`` holds equal to the swap tables.
"""

from __future__ import annotations

import inspect
from typing import Any, Dict, Iterator, List, Tuple

import pytest
from pydantic import BaseModel

from idp_common import regional_models
from idp_common.config import models
from idp_common.config.merge_utils import load_system_defaults
from idp_common.config.models import (
    ClassificationModelSettings,
    IDPConfig,
    RegionalModelDefaults,
)

pytestmark = pytest.mark.unit

#: A US, an EU, a Canada, a GovCloud and an Asia Pacific Region. Only the EU and
#: Canada families have rows; the other three keep the declared id, as the swap does.
REGIONS = (
    "us-east-1",
    "us-west-2",
    "eu-west-1",
    "eu-central-1",
    "ca-central-1",
    "us-gov-west-1",
    "ap-southeast-2",
)


def _declared_us_model_fields() -> List[Tuple[type, str, str]]:
    """(class, field, declared US id) for every config model class in ``models``."""
    found = []
    for _name, cls in inspect.getmembers(models, inspect.isclass):
        if not issubclass(cls, BaseModel) or cls.__module__ != models.__name__:
            continue
        for field_name, field in cls.model_fields.items():
            default = field.default
            if isinstance(default, str) and default.startswith("us."):
                found.append((cls, field_name, default))
    return found


DECLARED = _declared_us_model_fields()


def _expected(us_id: str, region: str) -> str:
    """The row's answer for ``region``; the declared id where no family matches."""
    for prefix, row in regional_models.REGIONAL_DEFAULT_MODELS.items():
        if region.startswith(prefix):
            return row.get(us_id, us_id)
    return us_id


@pytest.fixture
def region(monkeypatch: pytest.MonkeyPatch):
    def _set(aws_region: str | None, default_region: str | None = None) -> None:
        for name, value in (
            ("AWS_REGION", aws_region),
            ("AWS_DEFAULT_REGION", default_region),
        ):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)

    return _set


def test_the_scan_finds_the_model_fields_it_is_about():
    """Without this the parametrised tests below could read nothing and pass."""
    names = {(cls.__name__, field) for cls, field, _ in DECLARED}
    for expected in (
        ("ClassificationModelSettings", "model"),
        ("SplittingLLMConfig", "model"),
        ("SectionClassificationLLMConfig", "model"),
        ("ExtractionConfig", "model"),
        ("SummarizationConfig", "model"),
        ("ChatConfig", "model"),
        ("ErrorAnalyzerConfig", "model_id"),
        ("ChatCompanionConfig", "model_id"),
        ("FactExtractionConfig", "model"),
        ("RuleValidationOrchestratorConfig", "model"),
        ("Z3RuleTranslatorConfig", "model"),
        ("Z3ValueExtractionConfig", "model"),
        ("EvaluationLLMMethodConfig", "model"),
        ("DiscoveryModelConfig", "model_id"),
        ("AutoSplitModelConfig", "model_id"),
        ("MultiDocumentDiscoveryConfig", "embedding_model_id"),
        ("MultiDocumentDiscoveryConfig", "analysis_model_id"),
    ):
        assert expected in names


@pytest.mark.parametrize(
    ("cls", "field"),
    [(cls, field) for cls, field, _ in DECLARED],
    ids=[f"{cls.__name__}.{field}" for cls, field, _ in DECLARED],
)
def test_every_us_model_default_is_on_a_class_that_translates_it(cls, field):
    assert issubclass(cls, RegionalModelDefaults), cls.__name__


@pytest.mark.parametrize(
    ("cls", "field", "us_id"),
    DECLARED,
    ids=[f"{cls.__name__}.{field}" for cls, field, _ in DECLARED],
)
def test_every_us_model_default_is_one_the_translation_knows(cls, field, us_id):
    """An id outside the registry would be returned unchanged in an EU stack."""
    assert us_id in regional_models.FALLBACK_MODEL_IDS, (cls.__name__, field, us_id)


@pytest.mark.parametrize("aws_region", REGIONS)
@pytest.mark.parametrize(
    ("cls", "field", "us_id"),
    DECLARED,
    ids=[f"{cls.__name__}.{field}" for cls, field, _ in DECLARED],
)
def test_an_absent_model_key_is_the_regions_profile(
    region, aws_region, cls, field, us_id
):
    region(aws_region)
    assert getattr(cls(), field) == _expected(us_id, aws_region)


def test_the_eu_rows_move_every_declared_default_off_the_us_profile():
    """The EU family has a row for every declared default, so none stays ``us.``."""
    for cls, field, us_id in DECLARED:
        assert _expected(us_id, "eu-west-1").startswith("eu."), (cls, field, us_id)


@pytest.mark.parametrize(
    ("cls", "field", "us_id"),
    DECLARED,
    ids=[f"{cls.__name__}.{field}" for cls, field, _ in DECLARED],
)
def test_a_named_model_is_not_translated(region, cls, field, us_id):
    """Only a value the declaration supplies is translated, not one the input gives."""
    region("eu-west-1")
    assert getattr(cls(**{field: us_id}), field) == us_id


def test_the_declared_default_stays_the_us_id(region):
    """Introspection still reads the shipped value the parity tests compare."""
    region("eu-west-1")
    for cls, field, us_id in DECLARED:
        assert cls.model_fields[field].default == us_id


@pytest.mark.parametrize("aws_region", REGIONS)
@pytest.mark.parametrize("cleared", [None, "", "   "])
def test_absent_and_cleared_classification_models_agree(region, aws_region, cleared):
    region(aws_region)
    assert (
        ClassificationModelSettings().model
        == ClassificationModelSettings(model=cleared).model
    )


def test_a_local_session_region_is_used_when_aws_region_is_unset(region):
    region(None, "eu-west-3")
    assert models.ExtractionConfig().model == "eu.anthropic.claude-sonnet-5"


def test_no_region_is_the_declared_default(region):
    region(None)
    for cls, field, us_id in DECLARED:
        assert getattr(cls(), field) == us_id


def _string_leaves(node: Any, path: str = "") -> Iterator[Tuple[str, str]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _string_leaves(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _string_leaves(value, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


def _us_leaves(config: IDPConfig) -> Dict[str, str]:
    return {
        path: value
        for path, value in _string_leaves(config.model_dump(mode="python"))
        if value.startswith("us.")
    }


@pytest.mark.parametrize("aws_region", ["eu-west-1", "eu-central-1"])
def test_a_default_configuration_names_no_us_profile_in_the_eu(region, aws_region):
    region(aws_region)
    leaves = _us_leaves(IDPConfig())
    assert leaves == {}


@pytest.mark.parametrize("aws_region", ["eu-west-1", "eu-central-1"])
def test_bda_mode_defaults_absent_model_keys_are_translated(region, aws_region):
    """BDA mode's system defaults leave out chat, classification and extraction.

    So each of those model keys is absent from a BDA-mode configuration resolved
    against them, and every one of them was a ``us.`` profile in an EU stack. The
    keys the defaults DO carry are rewritten by the deploy-time swap, not here.
    """
    region(aws_region)
    defaults = load_system_defaults("pattern-1")
    assert "chat" not in defaults and "classification" not in defaults
    assert "model" not in defaults["extraction"], "BDA defaults name a model now"
    config = IDPConfig(**defaults)
    assert config.chat.model == "eu.anthropic.claude-opus-4-8:1m"
    assert config.extraction.model == "eu.anthropic.claude-sonnet-5"
    assert config.classification.combined.llm.model == "eu.amazon.nova-2-lite-v1:0"


def test_a_z3_block_without_a_model_is_translated(region):
    """The z3 blocks ship no model in the system defaults, so their key is absent."""
    region("eu-west-1")
    rule_validation = load_system_defaults("pattern-2")["rule_validation"]
    assert "z3_rule_translator" not in rule_validation
    config = models.RuleValidationConfig(
        **rule_validation,
        z3_rule_translator={"system_prompt": "s"},
        z3_value_extraction={"system_prompt": "s"},
    )
    assert config.z3_rule_translator is not None
    assert config.z3_value_extraction is not None
    assert config.z3_rule_translator.model == "eu.anthropic.claude-sonnet-4-6"
    assert (
        config.z3_value_extraction.model
        == "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
    )


def test_input_that_is_not_a_mapping_is_passed_through(region):
    """Attribute input is read by pydantic itself; the mixin leaves it alone."""
    region("eu-west-1")

    class Source:
        model = "us.anthropic.claude-opus-4-8"

    config = models.SummarizationConfig.model_validate(Source(), from_attributes=True)
    assert config.model == "us.anthropic.claude-opus-4-8"
