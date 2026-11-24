# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Whether an optional pipeline stage is on for a configuration.

One answer per stage, for every caller that has to agree on it: the two document
assembly Lambdas and the apply-review-changes step return it to the state machine,
and the queue processor writes it into the run plan. :func:`step_enabled` gives
the answer of every optional step at once, by step id.

Every function takes an ``IDPConfig`` or its dict form. A setting the
configuration does not carry reads as the model default.
"""

from __future__ import annotations

import logging
from typing import Any

from idp_common.config.flags import flag_is_true

logger = logging.getLogger(__name__)


def _read(source: Any, name: str) -> Any:
    """``source[name]`` for a dict, ``source.name`` for a model, else ``None``."""
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def _section_enabled(config: Any, section: str, *, default: bool) -> bool:
    """``<section>.enabled`` of ``config`` read as a flag; ``default`` when unset."""
    return flag_is_true(_read(_read(config, section), "enabled"), default=default)


def policy_rule_validation_enabled(config: Any) -> bool:
    """True when policy rule validation runs for ``config``.

    It runs when ``rule_validation.enabled`` is on **and** ``policy_classes`` holds
    at least one policy: with no policies there is nothing to validate, so the
    stage is skipped. ``config`` is an ``IDPConfig`` or its dict form. ``None`` is
    False, because no configuration holds no policies. A dict with no
    ``rule_validation.enabled`` reads as the model default, which is on.
    """
    if config is None:
        return False
    rule_validation = _read(config, "rule_validation")
    if rule_validation is not None and not flag_is_true(
        _read(rule_validation, "enabled"), default=True
    ):
        return False
    if not (_read(config, "policy_classes") or []):
        logger.debug(
            "Policy rule validation is skipped: no policy_classes are configured"
        )
        return False
    return True


def review_routing_enabled(config: Any) -> bool:
    """True when Review Routing is on for ``config``: ``hitl.enabled``.

    ``None``, and a configuration that does not set ``hitl.enabled``, are False,
    which is the model default.
    """
    return _section_enabled(config, "hitl", default=False)


def confidence_enabled(config: Any) -> bool:
    """True when per-field confidence is scored for ``config``.

    This is ``extraction.confidence.enabled``, which the model derives from
    ``extraction.confidence.mode``: it is off when the mode is ``off``, and when a
    configuration written before the mode existed set ``enabled: false``. The
    dict form is read by the same rule, so a dict with ``mode: off`` is False. A
    configuration that sets neither is True, which is the model default, and so is
    ``None``. Each flag in this module answers ``None`` with its own default: on
    here, off for Review Routing and for policy rule validation.
    """
    confidence = _read(_read(config, "extraction"), "confidence")
    if not flag_is_true(_read(confidence, "enabled"), default=True):
        return False
    return str(_read(confidence, "mode") or "").strip().lower() != "off"


def step_enabled(config: Any) -> dict[str, bool]:
    """Whether each optional step does its work, by step id.

    The keys are step ids, written as literals because ``config`` must not import
    ``idp_common.run_plan``. Each value is the rule the step itself reads, so a
    list of planned steps agrees with what the run does.
    """
    return {
        "assessment": confidence_enabled(config),
        "policy_rule_validation": policy_rule_validation_enabled(config),
        "review_routing": review_routing_enabled(config),
        "summarization": _section_enabled(config, "summarization", default=True),
        "evaluation": _section_enabled(config, "evaluation", default=True),
    }
