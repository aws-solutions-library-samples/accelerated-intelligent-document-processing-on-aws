# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Test configuration for chat_with_document_processor.

Sets fake AWS credentials + mocks ``idp_common`` imports that would otherwise
require the Lambda layer. Tests install an emission sink via ``set_sink`` and
patch the Bedrock surface instead of relying on those packages being installed
locally.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Required env vars read at module import / handler entry.
os.environ.setdefault("TRACKING_TABLE_NAME", "tracking-table")
os.environ.setdefault("CONFIGURATION_TABLE_NAME", "config-table")
os.environ.setdefault("OUTPUT_BUCKET", "output-bucket")
os.environ.setdefault("USERS_TABLE_NAME", "")  # RBAC defaults to unrestricted

# Stub `idp_common.*` symbols that the processor imports. The real package is
# delivered via a Lambda layer at deploy time; for unit tests we only need to
# mock the symbols the module references at import time.
sys.modules.setdefault("idp_common", MagicMock())
_cfg_mod = MagicMock()
_cfg_mod.get_config = MagicMock(return_value={})
sys.modules["idp_common.config"] = _cfg_mod

# config_scope is loaded for REAL rather than stubbed: it is the security boundary
# the RBAC test below exercises, and a MagicMock would make that test pass
# regardless of what the matcher does. The module is dependency-free (stdlib
# only), so loading it from the repo costs nothing.
_scope_spec = importlib.util.spec_from_file_location(
    "idp_common.config_scope",
    Path(__file__).resolve().parents[4] / "lib/idp_common_pkg/idp_common/config_scope.py",
)
_scope_mod = importlib.util.module_from_spec(_scope_spec)
_scope_spec.loader.exec_module(_scope_mod)
sys.modules["idp_common.config_scope"] = _scope_mod

# Stub idp_common.bedrock.client — used by the processor for the sampling-param
# and reasoning-effort capability gates, and default_client (passed to the OpenAI
# streaming generator).
#
# These MUST be real callables, not bare MagicMock attributes: a MagicMock
# attribute is truthy, so `if not strips_sampling_params(model)` would evaluate
# False for EVERY model and silently drop `temperature` from every chat request.
# Mirrors the real predicates in idp_common/bedrock/client.py.
_bedrock_mod = MagicMock()
_is_claude_4_7 = lambda model_id: (  # noqa: E731
    "claude-opus-4-7" in model_id
    or "claude-opus-4-8" in model_id
    or "claude-4-7" in model_id
    or "claude-opus-5" in model_id
    or "claude-sonnet-5" in model_id
)
_bedrock_mod.is_claude_4_7_model = _is_claude_4_7
_bedrock_mod.is_grok_model = lambda model_id: "xai.grok" in (model_id or "")
_bedrock_mod.strips_sampling_params = lambda model_id: (
    _is_claude_4_7(model_id) or "xai.grok" in (model_id or "")
)
_bedrock_mod.is_claude_effort_model = lambda model_id: any(
    t in (model_id or "")
    for t in (
        "claude-sonnet-5",
        "claude-sonnet-4-6",
        "claude-opus-4-5",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-opus-5",
        "claude-fable-5",
    )
)
_bedrock_mod.CLAUDE_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_bedrock_mod.GROK_EFFORT_LEVELS = ("none", "low", "medium", "high", "xhigh")
_bedrock_mod.default_client = MagicMock()
# The idp_common.bedrock facade exposes stream_responses_api (OpenAI GPT-5.x
# streaming chat path). Tests patch this generator.
_bedrock_facade = MagicMock()
_bedrock_facade.stream_responses_api = MagicMock()
sys.modules["idp_common.bedrock"] = _bedrock_facade
sys.modules["idp_common.bedrock.client"] = _bedrock_mod

# Stub idp_common.bedrock.openai_responses.is_openai_responses_model — used by
# the processor to route GPT-5.x chat to the non-streaming Responses API.
_openai_responses_mod = MagicMock()


def _fake_is_openai_responses_model(model_id):
    if not model_id:
        return False
    base = model_id.split(".", 1)[-1] if model_id[:3] in ("us.", "eu.") else model_id
    return base.startswith("openai.gpt-5") or model_id.startswith("openai.gpt-5")


_openai_responses_mod.is_openai_responses_model = _fake_is_openai_responses_model
sys.modules["idp_common.bedrock.openai_responses"] = _openai_responses_mod


# Stub idp_common.bedrock.model_utils.parse_model_id — used by the processor
# to split tier suffixes (:priority / :flex) off the model ID and pass the
# tier via performanceConfig.
def _fake_parse_model_id(model_id):
    if not model_id:
        return model_id, None
    parts = model_id.split(":")
    if len(parts) <= 2:
        return model_id, None
    potential_tier = parts[-1].lower().strip()
    if potential_tier in ("flex", "priority"):
        return ":".join(parts[:-1]), potential_tier
    return model_id, None


_model_utils_mod = MagicMock()
_model_utils_mod.parse_model_id = _fake_parse_model_id
sys.modules["idp_common.bedrock.model_utils"] = _model_utils_mod
