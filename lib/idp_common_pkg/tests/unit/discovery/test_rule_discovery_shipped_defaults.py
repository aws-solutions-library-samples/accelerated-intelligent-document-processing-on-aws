# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The shipped rule-discovery defaults, end to end, with no credentials.

#1305 reported that
``lib/idp_common_pkg/idp_common/config/system_defaults/base-rule-discovery.yaml``
produced discovered rules the shipped validator then rejected, so the
out-of-the-box path failed. The reproducing test was
``tests/integration/test_rules_discovery_smoke.py``, which needs real Bedrock,
and the integration tier runs in neither ``make test`` nor either CI — so the
only tier that exercised this combination was the GitLab nightly.

This module is the offline half. It reads the **shipped** defaults through the
production loader, stubs only the Bedrock call, and runs the whole discovery
path into the rule-validation runtime. The point is the *combination*: the
defaults parsing cleanly and the validator accepting *some* input were both
already covered, and both passed, while the two together did not work.

Everything the offline tier can assert is asserted here. What stays in the
integration tier is the part that genuinely needs a model: that a real policy
PDF yields a useful *number* of real rules.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from idp_common.config.merge_utils import load_system_defaults
from idp_common.config.models import IDPConfig
from idp_common.discovery.rules_discovery import RulesDiscovery, to_policy_class
from idp_common.utils import extract_json_from_text

from .rule_discovery_contract import (
    SHIPPED_DEFAULTS_PATTERN,
    assert_discovered_rules_are_runtime_consumable,
    load_shipped_rule_discovery_config,
)

pytestmark = pytest.mark.unit


def _shipped_output_format_example(config: IDPConfig) -> str:
    """Return the ``<output-format>`` block from the shipped task prompt.

    The stub response below is the shipped prompt's *own* worked example rather
    than a hand-written fixture, and it is returned as raw text so the
    production parser (``extract_json_from_text`` →
    ``_normalize_rules_response``) does the reading. So a prompt edit that makes
    the documented output shape unparseable, or no longer acceptable to the
    validator, fails here — which a fixture of the shape as it stands today
    could not do.
    """
    prompt = config.discovery.rules.task_prompt
    assert prompt, "The shipped defaults carry no discovery.rules.task_prompt"
    match = re.search(r"<output-format>(.*?)</output-format>", prompt, re.DOTALL)
    assert match, (
        "The shipped rule-discovery task prompt no longer has an "
        "<output-format> block, so the example response below is no longer "
        "derived from the shipped artifact."
    )
    return match.group(1)


def _bedrock_response(text: str) -> Dict[str, Any]:
    """Build the invoke_model envelope the Bedrock client returns."""
    return {
        "response": {
            "output": {"message": {"content": [{"text": text}]}},
            "stopReason": "end_turn",
        },
        "metering": {},
    }


def _discovery_with_shipped_defaults(config: IDPConfig) -> RulesDiscovery:
    """A RulesDiscovery on the shipped defaults, with Bedrock stubbed out."""
    discovery = RulesDiscovery(
        input_bucket="placeholder",
        input_prefix="medicare_respiratory_pa_packet.pdf",
        region="us-east-1",
        config=config,
    )
    discovery.bedrock_client = MagicMock()
    discovery.bedrock_client.invoke_model.return_value = _bedrock_response(
        _shipped_output_format_example(config)
    )
    return discovery


# ---------------------------------------------------------------------------
# The end-to-end assertion: shipped defaults in, runtime-consumable rules out
# ---------------------------------------------------------------------------


def test_shipped_defaults_produce_rules_the_runtime_can_evaluate(tmp_path):
    """Shipped defaults in, discovery runs, the consuming runtime accepts.

    This is the assertion #1305 claimed was false. It holds: the failure the
    issue reported came from the smoke test applying the raw-output validator
    to reshaped output (see ``rule_discovery_contract``), not from the defaults.
    """
    config = load_shipped_rule_discovery_config()
    discovery = _discovery_with_shipped_defaults(config)

    policy = tmp_path / "medicare_respiratory_pa_packet.pdf"
    policy.write_bytes(b"%PDF-1.4 stub")

    result = discovery.discovery_rules_from_document_local(str(policy))

    assert result["status"] == "SUCCESS", f"Unexpected status: {result.get('status')}"
    evaluated = assert_discovered_rules_are_runtime_consumable(result["rules"], config)
    assert evaluated >= 1


def test_the_s3_path_assigns_a_filename_derived_policy_type(tmp_path, monkeypatch):
    """Pin what the S3 path does with ``x-aws-idp-rule-type`` today.

    ``discovery_rules_from_document`` overwrites the model's value with a name
    derived from the uploaded key, so each upload yields a distinguishable
    policy class. Nothing asserted that, which left the override free to stop
    firing — and because the model also emits the field, the result would still
    validate and still persist, just with every upload collapsing onto the
    model's own label.

    This pins current behaviour, not a preference. Whether the field is the
    model's responsibility or the pipeline's is an open question on #1305; the
    point here is that answering it has to be a visible change rather than a
    silent one.

    Note the guard on the override (``if derived_name:``) can never be false:
    ``_derive_class_name_from_key`` always appends a random hex suffix and falls
    back to ``policy_<suffix>`` for an unusable stem. So the override is already
    unconditional in fact, which is worth knowing before deciding to make it
    unconditional in form.
    """
    config = load_shipped_rule_discovery_config()
    discovery = _discovery_with_shipped_defaults(config)
    monkeypatch.setattr(
        "idp_common.utils.s3util.S3Util.get_bytes",
        staticmethod(lambda bucket, key: b"%PDF-1.4 stub"),
    )

    result = discovery.discovery_rules_from_document(
        input_bucket="placeholder",
        input_prefix="policies/20250101_120000_NCCI_Medicare_Policy.pdf",
    )

    assert result["status"] == "SUCCESS"
    assigned = {rc["x-aws-idp-rule-type"] for rc in result["rules"]}
    assert len(assigned) == 1
    name = assigned.pop()
    assert name != "policy_rules", (
        "The S3 path returned the model's own label, so the filename-derived "
        "override did not fire and every upload would collapse onto one class."
    )
    assert re.fullmatch(r"NCCI_Medicare_Policy_[0-9a-f]{8}", name), (
        f"Unexpected derived policy type {name!r}: the timestamp prefix should "
        "be stripped, the stem kept and a hex suffix appended."
    )

    # Whatever name is assigned, the result must still be runtime-consumable.
    assert_discovered_rules_are_runtime_consumable(
        [to_policy_class(rc, name) for rc in result["rules"]], config
    )


def test_the_derived_policy_type_is_never_empty():
    """``_derive_class_name_from_key`` has no falsy return, for any key.

    The override above is guarded on this value being truthy, so a key shape
    that produced an empty name would skip the override silently. Covers the
    shapes a real upload can take, including the ones where the stem survives
    nothing of the original filename.
    """
    for key in (
        "",
        "/",
        "policies/",
        ".pdf",
        "policies/20250101_120000_.pdf",
        "policies/____.pdf",
        "policies/20250101_120000_NCCI.pdf",
        "a.pdf",
        "policies/nested/dir/éüñ.pdf",
    ):
        derived = RulesDiscovery._derive_class_name_from_key(key)
        assert derived, f"Key {key!r} produced a falsy policy-class name"


def test_the_shipped_prompts_own_example_satisfies_the_shipped_validator():
    """The prompt asks for a shape the extraction gate accepts.

    ``_validate_rules_response`` runs inside ``_extract_rules``, so a documented
    output format it rejects means three wasted retries and a hard failure for a
    perfectly compliant model. Read the example out of the prompt and put it
    through the same parse-and-validate sequence the production path uses.
    """
    config = load_shipped_rule_discovery_config()
    discovery = RulesDiscovery(
        input_bucket="placeholder",
        input_prefix="placeholder.pdf",
        region="us-east-1",
        config=config,
    )
    discovery.bedrock_client = MagicMock()

    example_text = _shipped_output_format_example(config)
    parsed = json.loads(extract_json_from_text(example_text))
    rules = discovery._normalize_rules_response(parsed)

    is_valid, error = discovery._validate_rules_response(rules)
    assert is_valid, (
        f"The shipped task prompt's own <output-format> example is rejected by "
        f"the extraction gate: {error}"
    )


def test_the_discovery_defaults_reach_the_model_call_coerced(tmp_path):
    """No raw YAML string reaches the Bedrock client through the shipped path.

    ``max_tokens: ""`` is the repo-wide encoding for "no explicit cap — use the
    model's maximum output"; ``_parse_optional_max_tokens`` turns it into
    ``None`` and the client then resolves the model limit. That coercion happens
    on *construction*, and ``RuleDiscoveryConfig`` does not set
    ``validate_assignment``, so a caller that assigns the raw value onto a
    built model instead gets the string through untouched and the client logs
    ``Failed to convert max_tokens value '' to int``. That is what the #1305
    report observed, and it came from the test rather than from a deployment.
    """
    config = load_shipped_rule_discovery_config()
    assert config.discovery.rules.max_tokens is None, (
        "The shipped defaults' max_tokens did not coerce to None (unset) when "
        "the config was constructed the way a deployment constructs it."
    )

    discovery = _discovery_with_shipped_defaults(config)
    policy = tmp_path / "policy.pdf"
    policy.write_bytes(b"%PDF-1.4 stub")
    discovery.discovery_rules_from_document_local(str(policy))

    kwargs = discovery.bedrock_client.invoke_model.call_args.kwargs
    assert kwargs["max_tokens"] is None, (
        f"max_tokens reached the Bedrock client as {kwargs['max_tokens']!r}; "
        "an unset cap must arrive as None so the client resolves the model's "
        "maximum output limit."
    )
    for name in ("temperature", "top_p"):
        assert isinstance(kwargs[name], float), (
            f"{name} reached the Bedrock client as {kwargs[name]!r}, not a float"
        )
    assert kwargs["model_id"] == config.discovery.rules.model
    assert kwargs["system_prompt"] == config.discovery.rules.system_prompt

    # The *shipped* prompt is the one sent, not the hardcoded fallback. Both
    # `task_prompt` and `system_prompt` are read through an `or` chain onto an
    # in-code default, so a shipped prompt that fails to load is not an error —
    # the discovery runs against ``_prompt_rules_discovery()`` instead, which
    # asks for a different output shape (multiple categorised rule classes
    # rather than one flat list) and would be invisible without this.
    sent_prompt = next(block["text"] for block in kwargs["content"] if "text" in block)
    assert _shipped_output_format_example(config) in sent_prompt, (
        "The prompt sent to Bedrock does not contain the shipped task prompt's "
        "<output-format> block, so the request was built from the in-code "
        "fallback prompt rather than from base-rule-discovery.yaml."
    )


def test_every_shipped_max_tokens_default_is_unset_or_a_positive_int():
    """No shipped default persists a value a bare ``int()`` would choke on.

    Derived from the shipped tree rather than from a list of files, so a new
    ``max_tokens`` default is covered without being registered here. The
    encoding under test is the one the repo uses: the YAML carries ``""`` for
    "unset", and the config model is what has to turn that into ``None``. An
    upgrade that reads a stored ``null`` or ``""`` for a field whose consumer
    does a bare ``int()`` is a shape this repository has been wedged by before.
    """
    checked = 0
    for pattern in ("pattern-1", SHIPPED_DEFAULTS_PATTERN):
        defaults = load_system_defaults(pattern)
        config = IDPConfig(**defaults)
        for section_name, section in _sections_with_max_tokens(defaults):
            checked += 1
            resolved = _resolve_attr(config, section_name)
            assert resolved is None or (isinstance(resolved, int) and resolved > 0), (
                f"{pattern}: {section_name}.max_tokens resolved to "
                f"{resolved!r} (raw {section['max_tokens']!r}); it must be None "
                "(unset) or a positive int."
            )
    assert checked, (
        "No shipped default carries a max_tokens key, so this check asserted "
        "nothing. Either the encoding changed or the traversal is broken."
    )


def _sections_with_max_tokens(
    defaults: Dict[str, Any], prefix: str = ""
) -> List[tuple]:
    """Every (dotted path, dict) in the merged defaults carrying ``max_tokens``."""
    found: List[tuple] = []
    for key, value in defaults.items():
        if not isinstance(value, dict):
            continue
        path = f"{prefix}{key}"
        if "max_tokens" in value:
            found.append((path, value))
        found.extend(_sections_with_max_tokens(value, prefix=f"{path}."))
    return found


def _resolve_attr(config: IDPConfig, dotted: str) -> Any:
    """Follow a dotted config path on the model and return ``max_tokens``."""
    node: Any = config
    for part in dotted.split("."):
        node = getattr(node, part)
    return node.max_tokens


# ---------------------------------------------------------------------------
# The two validators are not interchangeable, and must stay that way
# ---------------------------------------------------------------------------


def test_the_raw_output_validator_rejects_a_reshaped_policy_class():
    """Pin the distinction that #1305 was a misreading of.

    ``_validate_rules_response`` checks raw LLM output, keyed on
    ``x-aws-idp-rule-type``. ``to_policy_class`` emits the persisted shape,
    keyed on ``x-aws-idp-policy-type``. Loosening the first one to accept the
    second — so that the smoke test's original assertion would pass — would
    make the extraction gate accept output that carries no discriminator at all,
    which is the shape that once made rule validation silently never fire. The
    fix is to assert the right contract, not to widen this one.
    """
    discovery = RulesDiscovery(
        input_bucket="b", input_prefix="k.pdf", config=IDPConfig()
    )
    discovery.bedrock_client = MagicMock()

    raw = {
        "x-aws-idp-rule-type": "policy_rules",
        "rule_properties": {"a_rule": {"description": "Is it so?"}},
    }
    assert discovery._validate_rules_response([raw])[0]

    reshaped = to_policy_class(raw, "policy_rules")
    is_valid, error = discovery._validate_rules_response([reshaped])
    assert not is_valid, (
        "The raw-output validator now accepts a reshaped policy class. If that "
        "was deliberate it means the extraction gate no longer requires a "
        "discriminator it can read."
    )
    # Rejected *for being absent*, and reported that way. Widening only the
    # presence check leaves the type check to refuse it a line later with
    # "must be a string", which still rejects but reports the wrong cause into
    # the retry prompt — so asserting the cause is what makes a half-applied
    # loosening visible rather than silently near-equivalent.
    assert error == "Rule class 0: Missing required field: x-aws-idp-rule-type", (
        f"A reshaped class is still rejected, but not as a missing "
        f"discriminator: {error!r}"
    )


def test_a_rule_whose_description_is_empty_is_refused_rather_than_dropped():
    """An empty description must fail the gate, not vanish downstream.

    The gate tested presence (``"description" not in rule_def``) while the
    consumer tests truthiness (``RuleValidationService._get_rule_metadata``
    keeps a rule only ``if description``). A rule emitted with
    ``"description": ""`` therefore passed discovery, was persisted, and was
    then dropped at evaluation time with nothing reporting it — a rule missing
    from a compliance validation set, which is the failure mode this module's
    siblings exist to prevent. Refusing it instead puts the model through the
    existing retry-with-feedback loop.
    """
    discovery = RulesDiscovery(
        input_bucket="b", input_prefix="k.pdf", config=IDPConfig()
    )
    discovery.bedrock_client = MagicMock()

    is_valid, error = discovery._validate_rules_response(
        [
            {
                "x-aws-idp-rule-type": "policy_rules",
                "rule_properties": {
                    "answerable": {"description": "Is it so?"},
                    "blank": {"description": ""},
                },
            }
        ]
    )
    assert not is_valid, (
        "A rule with an empty description passed the extraction gate. The "
        "runtime drops it silently, so the ruleset persists one rule short."
    )
    assert "blank" in error
