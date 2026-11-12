# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every model-ID field in the config is checked against ``pricing.yaml``.

``_validate_model_ids`` used to check only the section-level ``model`` of OCR,
classification, extraction and summarization, plus an ``assessment.model`` that no
merged configuration carries any more. A typo anywhere else — an escalation model,
a per-class override, a confidence, chat, agent, discovery, rule-validation or
evaluation model — passed ``config-validate``. So did an OpenAI GPT-5.x ID written
with a leading space or in upper case, which also slipped past the rule refusing
those models for agentic extraction, because the predicate behind that rule matches
IDs as written and so does the Bedrock client that routes on it.

Every test here goes through ``validate_config``, which is what ``idp-cli
config-validate`` calls, against the repository's real ``config_library/pricing.yaml``.
"""

import copy
import glob
import typing
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml
from pydantic import BaseModel

from idp_common.config import schema_constants
from idp_common.config.merge_utils import (
    _CATALOG_MODEL_FIELDS,
    _load_valid_bedrock_models,
    merge_config_with_defaults,
    validate_config,
)
from idp_common.config.models import IDPConfig

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[5]

# Model fields that are checked per classification call by `_classification_calls`
# rather than through `_CATALOG_MODEL_FIELDS`.
CLASSIFICATION_CALL_FIELDS = {
    "classification.combined.llm.model",
    "classification.split_then_classify.splitting.llm.model",
    "classification.split_then_classify.section_classification.llm.model",
}

# Model fields the catalog check leaves alone. Each premise is evaluated below.
UNPRICED_MODEL_FIELDS = {
    # Embedding models: pricing.yaml prices none, so the default is not in it.
    "classification.split_then_classify.section_classification.embeddings.model",
    "discovery.multi_document.embedding_model_id",
}

# Per-class keys whose value is a Bedrock model ID, and the one that is not.
CHECKED_CLASS_KEYS = (
    schema_constants.X_AWS_IDP_EXTRACTION_MODEL,
    schema_constants.X_AWS_IDP_EXTRACTION_ESCALATION_MODEL,
    schema_constants.X_AWS_IDP_CONFIDENCE_ESCALATION_MODEL,
)
# The name of the Stickler model class the evaluator builds, not a Bedrock model.
CLASS_KEY_NOT_A_BEDROCK_MODEL = schema_constants.X_AWS_IDP_EVALUATION_MODEL_NAME

GOOD_MODEL = "us.anthropic.claude-sonnet-4-6"


def _bad_spellings(good: str) -> Dict[str, str]:
    return {
        "typo": good.replace("sonnet", "sonet"),
        "leading-space": " " + good,
        "trailing-space": good + " ",
        "upper-case": good.upper(),
        "openai-leading-space": " openai.gpt-5.4",
        "openai-upper-case": "OPENAI.GPT-5.4",
    }


def _sub_models(annotation: Any) -> List[type]:
    found: List[type] = []
    for arg in typing.get_args(annotation) or ():
        found += _sub_models(arg)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        found.append(annotation)
    return found


def _idpconfig_model_fields() -> Dict[str, Any]:
    """Dotted path -> default for every scalar ``*model*`` field of ``IDPConfig``.

    ``*_lambda_hook_arn`` fields name a Lambda, not a model, and are left out.
    """
    out: Dict[str, Any] = {}

    def visit(model: type, prefix: List[str], chain: List[type]) -> None:
        for name, field in model.model_fields.items():
            path = prefix + [name]
            subs = _sub_models(field.annotation)
            if not subs and "model" in name and not name.endswith("lambda_hook_arn"):
                out[".".join(path)] = field.default
            for sub in subs:
                if sub not in chain:
                    visit(sub, path, chain + [sub])

    visit(IDPConfig, [], [IDPConfig])
    return out


def _set_path(config: Dict[str, Any], path: str, value: str) -> None:
    *parents, leaf = path.split(".")
    node = config
    for key in parents:
        node = node.setdefault(key, {})
    node[leaf] = value


def _base_config(**class_keys: str) -> Dict[str, Any]:
    return {
        "classes": [
            {
                # Distinct from the document type, so an error naming the class
                # by its `$id` is told apart from one naming its type.
                "$id": "InvoiceSchema",
                "x-aws-idp-document-type": "Invoice",
                "type": "object",
                "properties": {"total": {"type": "string"}},
                **class_keys,
            }
        ],
        # Simple mode, so the agentic OpenAI rule cannot be what reports the ID.
        "extraction": {"mode": "simple"},
    }


def _model_errors(result: Dict[str, Any], label: str) -> List[str]:
    return [
        e for e in result["errors"] if e.startswith(f"{label} has invalid model ID:")
    ]


@pytest.fixture(scope="module", autouse=True)
def _catalog_present():
    """Fail rather than pass vacuously if the catalog did not load."""
    assert _load_valid_bedrock_models(), "config_library/pricing.yaml did not load"


# --- every checked field refuses a bad spelling and accepts a good one ----------


@pytest.mark.parametrize("path", _CATALOG_MODEL_FIELDS)
@pytest.mark.parametrize("kind", list(_bad_spellings(GOOD_MODEL)))
def test_section_field_refuses_bad_spelling(path: str, kind: str) -> None:
    config = _base_config()
    bad = _bad_spellings(GOOD_MODEL)[kind]
    _set_path(config, path, bad)
    result = validate_config(config, "pattern-2")
    assert result["valid"] is False
    assert _model_errors(result, path), result["errors"]


@pytest.mark.parametrize("path", _CATALOG_MODEL_FIELDS)
def test_section_field_accepts_catalog_model(path: str) -> None:
    config = _base_config()
    _set_path(config, path, GOOD_MODEL)
    result = validate_config(config, "pattern-2")
    assert not _model_errors(result, path), result["errors"]


@pytest.mark.parametrize("key", CHECKED_CLASS_KEYS)
@pytest.mark.parametrize("kind", list(_bad_spellings(GOOD_MODEL)))
def test_class_override_refuses_bad_spelling(key: str, kind: str) -> None:
    config = _base_config(**{key: _bad_spellings(GOOD_MODEL)[kind]})
    result = validate_config(config, "pattern-2")
    assert result["valid"] is False
    assert _model_errors(result, f"Class 'Invoice' {key}"), result["errors"]


@pytest.mark.parametrize("key", CHECKED_CLASS_KEYS)
def test_class_override_accepts_catalog_model(key: str) -> None:
    result = validate_config(_base_config(**{key: GOOD_MODEL}), "pattern-2")
    assert not _model_errors(result, f"Class 'Invoice' {key}"), result["errors"]


def test_opaque_arn_in_a_newly_checked_field_warns_rather_than_fails() -> None:
    """A custom or provisioned model is named by an opaque ARN; that stays a warning."""
    config = _base_config()
    arn = "arn:aws:bedrock:us-west-2:123456789012:custom-model-deployment/abc123"
    config["extraction"]["validation"] = {"escalation_model": arn}
    result = validate_config(config, "pattern-2")
    assert not _model_errors(result, "extraction.validation.escalation_model")
    assert any(
        w.startswith("extraction.validation.escalation_model names a Bedrock ARN")
        for w in result["warnings"]
    ), result["warnings"]


def test_openai_spelling_variant_is_refused_in_agentic_mode() -> None:
    """The bypass of the agentic OpenAI rule now meets the catalog check instead."""
    config = _base_config(
        **{schema_constants.X_AWS_IDP_EXTRACTION_ESCALATION_MODEL: "OPENAI.GPT-5.4"}
    )
    config["extraction"] = {
        "mode": "advanced",
        "validation": {
            "enabled": True,
            "fail_action": "escalate",
            "escalation_model": " openai.gpt-5.4",
        },
    }
    result = validate_config(config, "pattern-2")
    assert result["valid"] is False
    assert _model_errors(result, "extraction.validation.escalation_model")
    assert _model_errors(
        result,
        f"Class 'Invoice' {schema_constants.X_AWS_IDP_EXTRACTION_ESCALATION_MODEL}",
    )


# --- the set of checked fields is closed over the config models -----------------


def test_every_model_field_is_checked_or_named_as_unpriced() -> None:
    universe = set(_idpconfig_model_fields())
    covered = set(_CATALOG_MODEL_FIELDS) | CLASSIFICATION_CALL_FIELDS
    assert not covered & UNPRICED_MODEL_FIELDS
    assert universe - covered - UNPRICED_MODEL_FIELDS == set(), (
        "A model field is neither catalog-checked nor named as unpriced. Add it to "
        "_CATALOG_MODEL_FIELDS in merge_utils.py if pricing.yaml prices its models."
    )
    assert (covered | UNPRICED_MODEL_FIELDS) - universe == set(), (
        "A listed model field no longer exists in IDPConfig."
    )


@pytest.mark.parametrize("path", sorted(UNPRICED_MODEL_FIELDS))
def test_unpriced_field_premise_holds(path: str) -> None:
    """Each exclusion rests on its own default being absent from the catalog."""
    default = _idpconfig_model_fields()[path]
    merged = merge_config_with_defaults({}, "pattern-2", validate=False)
    node: Any = merged
    for key in path.split("."):
        node = node.get(key) if isinstance(node, dict) else None
    shipped = node or default
    if not shipped:
        # No default anywhere: the field is an embeddings model by declaration.
        assert "embed" in path
        return
    assert shipped not in _load_valid_bedrock_models(), (
        f"{path}'s default {shipped!r} is now priced; move it to _CATALOG_MODEL_FIELDS"
    )


def test_every_per_class_model_key_is_classified() -> None:
    keys = {
        value
        for name, value in vars(schema_constants).items()
        if name.startswith("X_AWS_IDP_") and isinstance(value, str) and "model" in value
    }
    assert keys == set(CHECKED_CLASS_KEYS) | {CLASS_KEY_NOT_A_BEDROCK_MODEL}


# --- shipped presets are not refused by the wider check -------------------------


@pytest.mark.parametrize(
    "preset",
    sorted(glob.glob(str(_REPO_ROOT / "config_library/unified/*/config.yaml"))),
    ids=lambda p: Path(p).parent.name,
)
def test_shipped_preset_has_no_model_id_error(preset: str) -> None:
    config = yaml.safe_load(Path(preset).read_text())
    result = validate_config(copy.deepcopy(config), "pattern-2")
    assert [e for e in result["errors"] if "has invalid model ID" in e] == []


def test_preset_glob_is_not_empty() -> None:
    assert glob.glob(str(_REPO_ROOT / "config_library/unified/*/config.yaml"))
