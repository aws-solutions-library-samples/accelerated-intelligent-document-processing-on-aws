# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared contract for "shipped rule-discovery defaults produce usable rules".

Not a test module (no ``test_`` prefix, so pytest does not collect it). It holds
the two halves of that assertion so the offline tier and the credentialed
integration tier make the *same* claim about the *same* artifact, differing only
in whether Bedrock is real:

* ``load_shipped_rule_discovery_config`` builds the config the way a deployment
  does — through ``load_system_defaults``, which reads the shipped
  ``system_defaults`` files, and then through ``IDPConfig(**...)`` so every
  field validator runs.
* ``assert_discovered_rules_are_runtime_consumable`` asserts the discovery
  output against the code that actually consumes it, rather than against a
  restatement of the contract.

Both points were wrong in the original smoke test and the combination is what
#1305 reported:

1. It assigned the raw YAML values onto an already-constructed model
   (``cfg.discovery.rules.max_tokens = rules["max_tokens"]``).
   ``RuleDiscoveryConfig`` does not set ``validate_assignment``, so pydantic's
   ``mode="before"`` validators never ran and the raw ``""`` reached the Bedrock
   client, which logged ``Failed to convert max_tokens value '' to int``. A
   deployment never sees that, because it constructs the model from the merged
   dict.
2. It checked the result with ``RulesDiscovery._validate_rules_response``, which
   validates **raw LLM output** (keyed on ``x-aws-idp-rule-type``). The method
   under test returns the **persisted** ``policy_classes`` shape (keyed on
   ``x-aws-idp-policy-type``), because ``to_policy_class`` reshapes it. Those are
   two different contracts on either side of one reshape, so the assertion could
   not pass for any model output at all.

Asserting through the consumer is deliberate. A hand-written expected shape is
how a config defect survives a green suite: it drifts from the artifact, and in
this case a restated shape check would still not have noticed that a rule whose
``description`` is empty is dropped by ``_get_rule_metadata`` without an error.
"""

from __future__ import annotations

from typing import Any, Dict, List

from idp_common.config.merge_utils import load_system_defaults
from idp_common.config.models import IDPConfig

#: The pattern whose resolved defaults carry ``discovery.rules``.
#: ``base-rule-discovery.yaml`` is pulled in by ``base.yaml``, which only
#: ``pattern-2.yaml`` inherits — so this is the pattern a rule-discovery
#: deployment reads, and resolving the other one would silently assert about an
#: absent section.
SHIPPED_DEFAULTS_PATTERN = "pattern-2"


def load_shipped_rule_discovery_config() -> IDPConfig:
    """Build an ``IDPConfig`` from the **shipped** system defaults.

    Reads the real ``system_defaults`` tree through the production loader — not
    a fixture copy of it — and constructs the model from the merged dict, which
    is what a deployment does and what makes the field validators run.
    """
    defaults = load_system_defaults(SHIPPED_DEFAULTS_PATTERN)
    rules = defaults.get("discovery", {}).get("rules")
    assert rules, (
        f"The shipped defaults for {SHIPPED_DEFAULTS_PATTERN!r} carry no "
        "discovery.rules section, so nothing below asserts anything about "
        "base-rule-discovery.yaml. Check that base.yaml still inherits it."
    )
    return IDPConfig(**defaults)


def assert_discovered_rules_are_runtime_consumable(
    rules: List[Dict[str, Any]],
    config: IDPConfig,
) -> int:
    """Assert discovery output reaches the rule-validation runtime intact.

    Writes ``rules`` into ``config.policy_classes`` exactly as the discovery
    path persists them, then asks ``RuleValidationService``'s own accessors —
    through ``IDPConfig.to_dict()``, which is the dict ``validate_document``
    hands them — for the policy types and the rule questions it would evaluate.

    Every discovered rule must come back. A rule the runtime drops here is a
    rule missing from a compliance validation set, which is silent: the document
    is validated against a subset of the policy and reported on as though it had
    been validated against all of it.

    Returns:
        The number of individual rules the runtime would evaluate.
    """
    from idp_common.rule_validation.service import RuleValidationService

    assert isinstance(rules, list) and rules, "Expected at least one policy class"

    discovered_rule_names = {
        name for rc in rules for name in (rc.get("rule_properties") or {})
    }
    assert discovered_rule_names, "Discovered policy classes carry no rules at all"

    config.policy_classes = rules
    service = RuleValidationService(config=config)
    config_dict = service.config.to_dict()

    policy_types = service._get_policy_types(config_dict)
    assert len(policy_types) == len(rules), (
        f"The runtime reads {len(policy_types)} policy type(s) from "
        f"{len(rules)} discovered policy class(es): {policy_types}. A class "
        "whose discriminator the runtime cannot read contributes no rules and "
        "fails silently."
    )

    seen_rule_count = 0
    for policy_type in policy_types:
        metadata = service._get_rule_metadata(config_dict, policy_type)
        assert metadata, (
            f"The runtime found no rule questions for policy type "
            f"{policy_type!r}, so rule validation would evaluate nothing."
        )
        for entry in metadata:
            assert entry["description"], (
                f"A rule under {policy_type!r} reached the runtime with an "
                "empty description."
            )
        seen_rule_count += len(metadata)

    assert seen_rule_count == len(discovered_rule_names), (
        f"{len(discovered_rule_names)} rule(s) were discovered but the runtime "
        f"would evaluate {seen_rule_count}. The difference is silently dropped."
    )
    return seen_rule_count
