# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Rollback-safety tests for config DynamoDB serialization (B3 + B1).

Background: a stack rollback reverts the config custom-resource Lambda to the
PRIOR release's code but leaves the current-shape config record in DynamoDB.
The old code then re-reads it. Two current-shape values were proven to break
older Pydantic models and wedge the rollback in UPDATE_ROLLBACK_FAILED:

  * ``None`` on a field the old model coerces with a bare ``int()`` -> ``int(None)``
    ``TypeError`` (e.g. ``max_tokens`` — 0.6 uses None = "request model max").
  * ``0`` on a field the old model constrains with ``gt=0`` -> ``ValidationError``
    (e.g. ``extraction.agentic.shard_token_budget`` — 0.6 uses 0 = "auto-size").

B3 fix: ``ConfigurationRecord.to_dynamodb_item`` omits any scalar whose value
equals its field default AND is ``None`` or integer ``0``. Because absent ==
default for the CURRENT model, this is behavior-neutral on read here, while
sparing the reverted old model from values it can't parse.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel, Field, field_validator

from idp_common.config.models import ConfigurationRecord, IDPConfig

_REPO_ROOT = Path(__file__).resolve().parents[5]
_CONFIG_DIR = _REPO_ROOT / "config_library" / "unified"


def _discover_configs():
    return sorted(_CONFIG_DIR.glob("*/config.yaml"))


def _walk_scalars(obj, path=""):
    """Yield (path, value) for every scalar leaf in a nested dict/list."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_scalars(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_scalars(v, f"{path}[{i}]")
    else:
        yield path, obj


# --------------------------------------------------------------------------- #
# A synthetic "old release" model reproducing the two proven failure modes,    #
# so the test does not need to vendor the entire 0.5.16 models.py.             #
# --------------------------------------------------------------------------- #
class _OldStrictModel(BaseModel):
    """Mimics pre-0.6 validators: bare int() coercion + gt=0 constraint."""

    max_tokens: int = Field(default=64000, gt=0)
    shard_token_budget: int = Field(default=6000, gt=0)

    @field_validator("max_tokens", "shard_token_budget", mode="before")
    @classmethod
    def _bare_int(cls, v: Any) -> int:
        # Reproduces the 0.5.16 pattern: None falls through to int(None) -> TypeError.
        if isinstance(v, str):
            return int(v) if v else 0
        return int(v)


def test_old_strict_model_crashes_on_hostile_values():
    """Guard: the synthetic old model reproduces BOTH proven failure modes."""
    with pytest.raises(TypeError):
        _OldStrictModel(max_tokens=None)  # int(None)
    with pytest.raises(Exception):
        _OldStrictModel(shard_token_budget=0)  # gt=0
    # Absent is always safe (uses default, validator not run).
    assert _OldStrictModel().max_tokens == 64000


@pytest.mark.parametrize(
    "config_path", _discover_configs(), ids=lambda p: p.parent.name
)
def test_serialized_config_has_no_hostile_default_scalars(config_path):
    """to_dynamodb_item() must not emit a None or integer-0 that equals its default."""
    raw = yaml.safe_load(config_path.read_text())
    cfg = IDPConfig(**raw)
    record = ConfigurationRecord(
        configuration_type="Config", version="default", config=cfg
    )
    item = record.to_dynamodb_item()

    # After stringification numbers are strings; the only way a hostile value
    # survives is as a native None (NULL) — bare int-0 defaults are omitted.
    offenders = [path for path, val in _walk_scalars(item) if val is None]
    assert not offenders, (
        f"{config_path.parent.name}: serialized item still carries NULL scalars "
        f"that older models may reject: {offenders}"
    )


@pytest.mark.parametrize(
    "config_path", _discover_configs(), ids=lambda p: p.parent.name
)
def test_roundtrip_is_lossless_in_current_model(config_path):
    """Omitting default None/0 scalars must not change the reconstructed config."""
    raw = yaml.safe_load(config_path.read_text())
    cfg = IDPConfig(**raw)
    record = ConfigurationRecord(
        configuration_type="Config", version="default", config=cfg
    )
    item = record.to_dynamodb_item()
    back = ConfigurationRecord.from_dynamodb_item(item)
    # The omitted fields must resolve back to their defaults on reload.
    assert (
        back.config.extraction.agentic.shard_token_budget
        == cfg.extraction.agentic.shard_token_budget
    )
    assert (
        back.config.classification.combined.llm.max_tokens
        == cfg.classification.combined.llm.max_tokens
    )


def test_omit_helper_preserves_nonhostile_and_bool_values():
    """The omit rule must NOT strip float 0.0, positive defaults, or booleans."""

    class _Sub(BaseModel):
        temperature: float = Field(default=0.0)  # float 0.0 — must be KEPT
        max_workers: int = Field(default=20)  # positive default — KEPT
        flag: bool = Field(default=False)  # bool — KEPT (not treated as 0)
        auto_budget: int = Field(default=0)  # int-0 default — OMITTED
        max_tokens: int = Field(default=0)  # int-0 default — OMITTED

    sub = _Sub()
    dumped = sub.model_dump(mode="python")
    result = ConfigurationRecord._omit_rollback_hostile_defaults(sub, dumped)

    assert "temperature" in result and result["temperature"] == 0.0
    assert "max_workers" in result
    assert "flag" in result and result["flag"] is False
    assert "auto_budget" not in result
    assert "max_tokens" not in result


def test_omit_helper_keeps_nondefault_zero():
    """A 0 that is NOT the field default must be preserved (it's a real choice)."""

    class _Sub(BaseModel):
        threshold: int = Field(default=5)  # default 5; storing 0 is intentional

    sub = _Sub(threshold=0)
    dumped = sub.model_dump(mode="python")
    result = ConfigurationRecord._omit_rollback_hostile_defaults(sub, dumped)
    # 0 != default(5) -> must be kept even though it's a hostile value class.
    assert result.get("threshold") == 0
