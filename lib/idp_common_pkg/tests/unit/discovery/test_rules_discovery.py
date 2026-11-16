# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for RulesDiscovery response normalization and validation.

These tests cover the JSON-parsing + validation logic exercised after the
LLM returns a rules response. They do not invoke Bedrock; RulesDiscovery is
constructed with a pre-built config so no network access is required.
"""

# ruff: noqa: E402, I001
# Disable E402 and I001 for this file (imports ordered for readability).

import json
import logging

import pytest
from unittest.mock import MagicMock, patch

from idp_common.config.models import IDPConfig
from idp_common.discovery import rules_discovery as rules_discovery_module
from idp_common.discovery.rules_discovery import (
    RulesDiscovery,
    _is_max_tokens_error,
    RulesTruncatedError,
    to_policy_class,
)
from idp_common.models import Document
from idp_common.rule_validation.policy_classification import (
    PolicyClassificationService,
)


@pytest.fixture
def discovery():
    """Return a RulesDiscovery instance with a minimal in-memory config.

    The bedrock client is mocked out so no AWS call is attempted even if a
    test indirectly touches it.
    """
    d = RulesDiscovery(
        input_bucket="test-bucket",
        input_prefix="test-policy.pdf",
        config=IDPConfig(),
    )
    d.bedrock_client = MagicMock()
    return d


@pytest.mark.unit
class TestNormalizeRulesResponse:
    """Tests for RulesDiscovery._normalize_rules_response."""

    def test_list_passes_through(self, discovery):
        payload = [
            {"x-aws-idp-rule-type": "p", "rule_properties": {"a": {"description": "?"}}}
        ]
        assert discovery._normalize_rules_response(payload) is payload

    def test_single_object_wrapped_in_list(self, discovery):
        payload = {
            "x-aws-idp-rule-type": "p",
            "rule_properties": {"a": {"description": "?"}},
        }
        result = discovery._normalize_rules_response(payload)
        assert result == [payload]

    def test_rule_classes_wrapper_unwrapped(self, discovery):
        inner = [
            {"x-aws-idp-rule-type": "p", "rule_properties": {"a": {"description": "?"}}}
        ]
        payload = {"rule_classes": inner}
        assert discovery._normalize_rules_response(payload) is inner

    def test_arbitrary_wrapper_with_rule_list_unwrapped(self, discovery):
        inner = [
            {"x-aws-idp-rule-type": "p", "rule_properties": {"a": {"description": "?"}}}
        ]
        payload = {"whatever": inner}
        assert discovery._normalize_rules_response(payload) is inner

    def test_unknown_dict_wrapped_as_single(self, discovery):
        # LLM returned something odd; the normalizer should wrap it as a single
        # candidate so _validate_rule_class can then reject it with a useful error.
        payload = {"foo": "bar"}
        result = discovery._normalize_rules_response(payload)
        assert result == [payload]

    def test_invalid_type_raises(self, discovery):
        with pytest.raises(ValueError):
            discovery._normalize_rules_response("not a list or dict")


@pytest.mark.unit
class TestValidateRuleClass:
    """Tests for RulesDiscovery._validate_rule_class."""

    def test_valid_minimal(self, discovery):
        rc = {
            "x-aws-idp-rule-type": "p1",
            "rule_properties": {"a": {"description": "?"}},
        }
        ok, msg = discovery._validate_rule_class(rc)
        assert ok is True
        assert msg == ""

    def test_missing_rule_type(self, discovery):
        rc = {"rule_properties": {"a": {"description": "?"}}}
        ok, msg = discovery._validate_rule_class(rc)
        assert ok is False
        assert "x-aws-idp-rule-type" in msg

    def test_non_string_rule_type(self, discovery):
        rc = {
            "x-aws-idp-rule-type": 123,
            "rule_properties": {"a": {"description": "?"}},
        }
        ok, msg = discovery._validate_rule_class(rc)
        assert ok is False
        assert "must be a string" in msg

    def test_missing_rule_properties(self, discovery):
        rc = {"x-aws-idp-rule-type": "p1"}
        ok, msg = discovery._validate_rule_class(rc)
        assert ok is False
        assert "rule_properties" in msg

    def test_rule_properties_wrong_type(self, discovery):
        rc = {"x-aws-idp-rule-type": "p1", "rule_properties": []}
        ok, msg = discovery._validate_rule_class(rc)
        assert ok is False
        assert "must be an object" in msg

    def test_empty_rule_properties(self, discovery):
        rc = {"x-aws-idp-rule-type": "p1", "rule_properties": {}}
        ok, msg = discovery._validate_rule_class(rc)
        assert ok is False
        assert "at least one" in msg

    def test_rule_without_description(self, discovery):
        rc = {
            "x-aws-idp-rule-type": "p1",
            "rule_properties": {"rule_a": {"page": "1"}},
        }
        ok, msg = discovery._validate_rule_class(rc)
        assert ok is False
        assert "description" in msg


@pytest.mark.unit
class TestValidateRulesResponse:
    """Tests for RulesDiscovery._validate_rules_response (top-level list)."""

    def test_happy_path(self, discovery):
        rules = [
            {
                "x-aws-idp-rule-type": "p1",
                "rule_properties": {"a": {"description": "?"}},
            },
            {
                "x-aws-idp-rule-type": "p2",
                "rule_properties": {"b": {"description": "?"}},
            },
        ]
        ok, msg = discovery._validate_rules_response(rules)
        assert ok is True
        assert msg == ""

    def test_not_a_list(self, discovery):
        ok, msg = discovery._validate_rules_response("oops")
        assert ok is False
        assert "must be a list" in msg

    def test_empty_list(self, discovery):
        ok, msg = discovery._validate_rules_response([])
        assert ok is False
        assert "at least one" in msg

    def test_second_rule_fails_reports_index(self, discovery):
        rules = [
            {
                "x-aws-idp-rule-type": "p1",
                "rule_properties": {"a": {"description": "?"}},
            },
            {"x-aws-idp-rule-type": "p2", "rule_properties": {"b": {}}},
        ]
        ok, msg = discovery._validate_rules_response(rules)
        assert ok is False
        # index 1 is the bad entry
        assert msg.startswith("Rule class 1:")


@pytest.mark.unit
class TestDeriveClassNameFromKey:
    """Tests for RulesDiscovery._derive_class_name_from_key."""

    def test_truncates_and_suffixes(self):
        # 20-char truncation + 8-char hex suffix separated by '_'
        name = RulesDiscovery._derive_class_name_from_key(
            "NCCI Medicare Policy Manual.pdf"
        )
        stem, _, suffix = name.rpartition("_")
        assert len(suffix) == 8
        assert all(c in "0123456789abcdef" for c in suffix)
        assert stem == "NCCI_Medicare_Policy"  # first 20 chars, sanitized

    def test_timestamp_prefix_stripped(self):
        name = RulesDiscovery._derive_class_name_from_key(
            "20260428_221758_Medicare_Manual.pdf"
        )
        assert not name.startswith("20260428_")
        assert name.startswith("Medicare_Manual")

    def test_fallback_on_empty_stem(self):
        name = RulesDiscovery._derive_class_name_from_key("20260428_221758_.pdf")
        assert name.startswith("policy_")

    def test_uniqueness_across_repeated_calls(self):
        # Same input twice must give different hex suffixes
        a = RulesDiscovery._derive_class_name_from_key("policy.pdf")
        b = RulesDiscovery._derive_class_name_from_key("policy.pdf")
        assert a != b


def _bedrock_response(text: str, stop_reason: str = "end_turn") -> dict:
    """Build a Bedrock invoke_model response envelope around `text`."""
    return {
        "response": {
            "output": {"message": {"content": [{"text": text}]}},
            "stopReason": stop_reason,
        },
        "metering": {},
    }


# A response that is truncated mid-ruleset yet still parses: the model got
# through 2 of an intended many rules and the braces happen to close. Every
# structural check passes, which is exactly why this used to persist silently.
_PARTIAL_BUT_VALID = json.dumps(
    [
        {
            "x-aws-idp-rule-type": "ncci_policy",
            "rule_properties": {
                "rule_one": {"description": "Is condition one satisfied?"},
                "rule_two": {"description": "Is condition two satisfied?"},
            },
        }
    ]
)


@pytest.mark.unit
class TestTruncationIsNotSuccess:
    """A ruleset cut off at the token ceiling must not be persisted as complete.

    Regression test for #603: `_validate_rules_response` checks structure, not
    completeness, so a truncated-but-brace-closed response satisfied every check
    and 80 of 140 rules went missing with the job reporting success.
    """

    def test_truncated_response_is_not_reported_as_success(self, discovery):
        discovery.bedrock_client.invoke_model.return_value = _bedrock_response(
            _PARTIAL_BUT_VALID, stop_reason="max_tokens"
        )

        with pytest.raises(RulesTruncatedError) as exc:
            discovery._extract_rules(b"pdf-bytes", "pdf", max_retries=2)

        # The error must be actionable, naming both remedies.
        message = str(exc.value)
        assert "max_tokens" in message
        assert "split" in message.lower()
        # It should retry before giving up, not fail on the first truncation.
        assert discovery.bedrock_client.invoke_model.call_count == 2

    def test_remedy_names_an_action_that_exists_at_the_shipped_default(self, discovery):
        """With ``max_tokens`` unset the call already had the model's whole output.

        Telling the user to raise ``discovery.rules.max_tokens`` then names nothing
        they can do: the field ships empty, which requests the model maximum, and a
        value above it is refused. What remains is a smaller document or a model
        with a larger output limit.
        """
        assert discovery.rules_config.max_tokens is None
        discovery.bedrock_client.invoke_model.return_value = _bedrock_response(
            _PARTIAL_BUT_VALID, stop_reason="max_tokens"
        )
        with pytest.raises(RulesTruncatedError) as exc:
            discovery._extract_rules(b"pdf-bytes", "pdf", max_retries=1)

        message = str(exc.value)
        assert "raise discovery.rules.max_tokens" not in message
        assert "discovery.rules.model" in message
        assert "already empty" in message

    def test_remedy_names_the_cap_when_one_is_set(self, discovery):
        discovery.rules_config.max_tokens = 4096
        discovery.bedrock_client.invoke_model.return_value = _bedrock_response(
            _PARTIAL_BUT_VALID, stop_reason="max_tokens"
        )
        with pytest.raises(RulesTruncatedError) as exc:
            discovery._extract_rules(b"pdf-bytes", "pdf", max_retries=1)

        message = str(exc.value)
        assert "discovery.rules.max_tokens is set to 4096" in message
        assert "clearing it" in message

    def test_truncation_retried_then_succeeds(self, discovery):
        """A transient truncation must not fail the job if a retry completes."""
        complete = json.dumps(
            [
                {
                    "x-aws-idp-rule-type": "ncci_policy",
                    "rule_properties": {
                        f"rule_{i}": {"description": f"Question {i}?"} for i in range(5)
                    },
                }
            ]
        )
        discovery.bedrock_client.invoke_model.side_effect = [
            _bedrock_response(_PARTIAL_BUT_VALID, stop_reason="max_tokens"),
            _bedrock_response(complete),
        ]

        rules = discovery._extract_rules(b"pdf-bytes", "pdf", max_retries=3)

        assert rules is not None
        assert len(rules[0]["rule_properties"]) == 5

    def test_untruncated_response_still_succeeds(self, discovery):
        """The guard must not reject a normal completion."""
        discovery.bedrock_client.invoke_model.return_value = _bedrock_response(
            _PARTIAL_BUT_VALID, stop_reason="end_turn"
        )
        rules = discovery._extract_rules(b"pdf-bytes", "pdf", max_retries=2)
        assert rules is not None
        assert len(rules) == 1

    @pytest.mark.parametrize(
        "stop_reason", ["max_tokens", "max_output_tokens", "incomplete"]
    )
    def test_all_truncation_signals_recognized(self, discovery, stop_reason):
        """Bedrock Converse and the OpenAI Responses adapter word this differently."""
        discovery.bedrock_client.invoke_model.return_value = _bedrock_response(
            _PARTIAL_BUT_VALID, stop_reason=stop_reason
        )
        with pytest.raises(RulesTruncatedError):
            discovery._extract_rules(b"pdf-bytes", "pdf", max_retries=1)


@pytest.mark.unit
class TestToPolicyClass:
    """The reshape shared by the S3 and local paths (#600)."""

    def test_emits_the_discriminator_the_runtime_reads(self):
        result = to_policy_class(
            {
                "x-aws-idp-rule-type": "ncci",
                "description": "NCCI rules",
                "rule_properties": {"r1": {"description": "Q1?"}},
            },
            "ncci",
        )
        assert result["x-aws-idp-policy-type"] == "ncci"
        assert result["$id"] == "ncci"
        assert result["type"] == "object"
        assert result["$schema"].startswith("https://json-schema.org/")
        assert result["description"] == "NCCI rules"
        # rule_properties entries are forced to type string + description
        assert result["rule_properties"]["r1"] == {
            "type": "string",
            "description": "Q1?",
        }

    def test_string_rule_property_is_coerced(self):
        result = to_policy_class(
            {"x-aws-idp-rule-type": "p", "rule_properties": {"r": "just a string"}}, "p"
        )
        assert result["rule_properties"]["r"] == {
            "type": "string",
            "description": "just a string",
        }

    def test_extra_property_keys_are_preserved(self):
        result = to_policy_class(
            {
                "x-aws-idp-rule-type": "p",
                "rule_properties": {
                    "r": {"description": "Q?", "x-aws-idp-custom": "keep-me"}
                },
            },
            "p",
        )
        assert result["rule_properties"]["r"]["x-aws-idp-custom"] == "keep-me"


@pytest.mark.unit
class TestLocalPathReturnsPersistableShape:
    """discovery_rules_from_document_local must return what actually works.

    Regression test for #600: it returned raw LLM output keyed on
    `x-aws-idp-rule-type`, so a notebook user who saved it verbatim got a config
    in which rule validation silently never fired.
    """

    def test_local_result_carries_policy_type(self, discovery, tmp_path):
        policy = tmp_path / "policy.pdf"
        policy.write_bytes(b"%PDF-1.4 fake")
        discovery.bedrock_client.invoke_model.return_value = _bedrock_response(
            _PARTIAL_BUT_VALID
        )

        result = discovery.discovery_rules_from_document_local(str(policy))

        assert result["status"] == "SUCCESS"
        rule_class = result["rules"][0]
        assert rule_class["x-aws-idp-policy-type"] == "ncci_policy"
        assert "$id" in rule_class and rule_class["type"] == "object"

    def test_local_result_is_accepted_by_the_runtime_classifier(
        self, discovery, tmp_path
    ):
        """End-to-end contract: save the local result verbatim into a config that
        ALREADY has a policy class, and the new class must actually match."""
        policy = tmp_path / "policy.pdf"
        policy.write_bytes(b"%PDF-1.4 fake")
        discovery.bedrock_client.invoke_model.return_value = _bedrock_response(
            _PARTIAL_BUT_VALID
        )
        discovered = discovery.discovery_rules_from_document_local(str(policy))["rules"]

        cfg = IDPConfig()
        preexisting = {
            "x-aws-idp-policy-type": "preexisting",
            "x-aws-idp-document-name-regex": r"(?i).*claim.*",
            "rule_properties": {"x": {"type": "string", "description": "Q?"}},
        }
        # Give the discovered class a regex, since >1 class requires one.
        for rc in discovered:
            rc["x-aws-idp-document-name-regex"] = r"(?i).*claim.*"
        cfg.policy_classes = [preexisting, *discovered]

        service = PolicyClassificationService(config=cfg)
        result = service.classify_document(Document(id="claim-123.pdf"))

        assert "ncci_policy" in result.matched_policy_types, (
            "a policy class produced by the local discovery path was not matched "
            "by the runtime classifier"
        )

    def test_duplicate_class_names_within_one_batch_are_disambiguated(
        self, discovery, tmp_path, monkeypatch
    ):
        """Two classes with the same name in one batch must not collide.

        The default prompt asks for a single-object array, so this is defensive:
        _extract_rules is stubbed because extract_json_from_text keeps only the
        FIRST element of a bare top-level JSON array, which would mask the
        behavior under test.
        """
        policy = tmp_path / "policy.pdf"
        policy.write_bytes(b"%PDF-1.4 fake")
        monkeypatch.setattr(
            discovery,
            "_extract_rules",
            lambda *a, **kw: [
                {
                    "x-aws-idp-rule-type": "dup",
                    "rule_properties": {"a": {"description": "Q?"}},
                },
                {
                    "x-aws-idp-rule-type": "dup",
                    "rule_properties": {"b": {"description": "Q?"}},
                },
            ],
        )

        rules = discovery.discovery_rules_from_document_local(str(policy))["rules"]

        assert [rc["x-aws-idp-policy-type"] for rc in rules] == ["dup", "dup_2"]


@pytest.mark.unit
class TestSaveTimeWarnings:
    """The save that creates a broken state must report it (#601, #602)."""

    def test_no_regex_on_multiple_classes_warns(self, caplog):
        classes = [
            {"x-aws-idp-policy-type": "a", "rule_properties": {}},
            {"x-aws-idp-policy-type": "b", "rule_properties": {}},
        ]
        with caplog.at_level(logging.WARNING):
            message = RulesDiscovery._warn_if_no_regex(classes, "v1")

        assert message is not None
        assert "will NOT fire" in message
        assert "v1" in message
        assert "will NOT fire" in caplog.text

    def test_single_class_without_regex_does_not_warn(self):
        classes = [{"x-aws-idp-policy-type": "a", "rule_properties": {}}]
        assert RulesDiscovery._warn_if_no_regex(classes, "v1") is None

    def test_partial_regex_coverage_names_the_unmatched_classes(self, caplog):
        """The likely state AFTER acting on the all-or-nothing warning.

        User adds a regex to policy A, runs discovery again, and new class B has
        none: B's rules silently never fire while A's do.
        """
        classes = [
            {
                "x-aws-idp-policy-type": "policy_a",
                "x-aws-idp-document-name-regex": ".*",
                "rule_properties": {},
            },
            {"x-aws-idp-policy-type": "policy_b", "rule_properties": {}},
        ]
        with caplog.at_level(logging.WARNING):
            message = RulesDiscovery._warn_if_no_regex(classes, "v1")

        assert message is not None, (
            "a regex-less class alongside a regex-bearing one was not reported; "
            "its rules can never fire"
        )
        assert "policy_b" in message
        assert "policy_a" not in message
        assert "never fire" in message
        assert "policy_b" in caplog.text

    def test_no_warning_when_every_class_has_a_regex(self):
        classes = [
            {
                "x-aws-idp-policy-type": "a",
                "x-aws-idp-document-name-regex": ".*",
                "rule_properties": {},
            },
            {
                "x-aws-idp-policy-type": "b",
                "x-aws-idp-document-name-regex": ".*",
                "rule_properties": {},
            },
        ]
        assert RulesDiscovery._warn_if_no_regex(classes, "v1") is None

    def test_page_content_regex_also_counts(self):
        classes = [
            {
                "x-aws-idp-policy-type": "a",
                "x-aws-idp-page-content-regex": "foo",
                "rule_properties": {},
            },
            {
                "x-aws-idp-policy-type": "b",
                "x-aws-idp-page-content-regex": "bar",
                "rule_properties": {},
            },
        ]
        assert RulesDiscovery._warn_if_no_regex(classes, "v1") is None

    def test_overlap_across_policy_classes_is_reported(self, caplog):
        """A rule duplicated across policies is billed twice per document."""
        newly_added = [
            {
                "x-aws-idp-policy-type": "policy_b",
                "rule_properties": {
                    "prior_auth_required": {"description": "?"},
                    "unique_to_b": {"description": "?"},
                },
            }
        ]
        all_classes = [
            {
                "x-aws-idp-policy-type": "policy_a",
                "rule_properties": {
                    "prior_auth_required": {"description": "?"},
                    "unique_to_a": {"description": "?"},
                },
            },
            *newly_added,
        ]
        with caplog.at_level(logging.WARNING):
            message = RulesDiscovery._warn_if_rules_duplicated(all_classes, newly_added)

        assert message is not None, (
            "a rule duplicated across policies will be evaluated twice per "
            "document with no indication to the user"
        )
        assert "prior_auth_required" in message
        assert "unique_to_a" not in message and "unique_to_b" not in message
        assert "prior_auth_required" in caplog.text

    def test_no_overlap_is_silent(self):
        newly_added = [
            {"x-aws-idp-policy-type": "b", "rule_properties": {"only_b": {}}}
        ]
        all_classes = [
            {"x-aws-idp-policy-type": "a", "rule_properties": {"only_a": {}}},
            *newly_added,
        ]
        assert (
            RulesDiscovery._warn_if_rules_duplicated(all_classes, newly_added) is None
        )

    def test_new_class_is_not_compared_against_itself(self):
        """The incoming class is in the saved list too; it must be excluded."""
        newly_added = [{"x-aws-idp-policy-type": "solo", "rule_properties": {"r": {}}}]
        assert (
            RulesDiscovery._warn_if_rules_duplicated(newly_added, newly_added) is None
        ), "the newly-saved class was compared against itself"


@pytest.mark.unit
class TestAgenticTruncationIsNotSuccess:
    """The agentic path must also refuse a partial ruleset (#603).

    Its primary protection differs from the traditional path's: Strands fails
    hard with MaxTokensReachedException on stopReason == "max_tokens" rather than
    returning a truncated result, so there is no partial payload to inspect. The
    job therefore already fails — but it surfaced as an opaque "Agent has reached
    an unrecoverable state" with no remediation. These tests pin the translation.
    """

    @pytest.fixture
    def agentic_discovery(self, monkeypatch):
        """Agentic-mode RulesDiscovery, forced importable.

        tests/conftest.py stubs `strands` with a MagicMock, so
        `from ...agentic_idp import structured_output` fails at import time and
        AGENTIC_AVAILABLE is False for the whole suite — which is precisely why
        this path had no coverage before. Force the flag on and inject the symbol
        so the guard logic under test can run without the real extras installed.
        """
        monkeypatch.setattr(rules_discovery_module, "AGENTIC_AVAILABLE", True)
        cfg = IDPConfig()
        cfg.discovery.rules.agentic.enabled = True
        d = RulesDiscovery(
            input_bucket="test-bucket", input_prefix="test-policy.pdf", config=cfg
        )
        d.bedrock_client = MagicMock()
        return d

    def test_strands_max_tokens_becomes_actionable_error(self, agentic_discovery):
        class MaxTokensReachedException(Exception):
            pass

        with patch(
            "idp_common.discovery.rules_discovery.structured_output",
            create=True,
            side_effect=MaxTokensReachedException(
                "Agent has reached an unrecoverable state due to max_tokens limit."
            ),
        ):
            with pytest.raises(RulesTruncatedError) as exc:
                agentic_discovery._extract_rules(b"pdf-bytes", "pdf")

        message = str(exc.value)
        assert "split" in message.lower()
        assert "max_tokens" in message

    @pytest.mark.parametrize(
        ("max_tokens", "expected"),
        [(None, "already empty"), (2048, "is set to 2048")],
    )
    def test_strands_remedy_follows_the_configured_cap(
        self, agentic_discovery, max_tokens, expected
    ):
        class MaxTokensReachedException(Exception):
            pass

        agentic_discovery.rules_config.max_tokens = max_tokens
        with patch(
            "idp_common.discovery.rules_discovery.structured_output",
            create=True,
            side_effect=MaxTokensReachedException("max_tokens limit"),
        ):
            with pytest.raises(RulesTruncatedError) as exc:
                agentic_discovery._extract_rules(b"pdf-bytes", "pdf")

        message = str(exc.value)
        assert expected in message
        assert "raise discovery.rules.max_tokens" not in message

    def test_unrelated_agentic_failure_is_not_relabeled(self, agentic_discovery):
        """A genuine error must not be reported as a truncation."""
        with patch(
            "idp_common.discovery.rules_discovery.structured_output",
            create=True,
            side_effect=RuntimeError("bedrock exploded"),
        ):
            with pytest.raises(RuntimeError):
                agentic_discovery._extract_rules(b"pdf-bytes", "pdf")

    @pytest.mark.parametrize(
        ("max_tokens", "expected"),
        [(None, "already empty"), (2048, "is set to 2048")],
    )
    def test_envelope_stop_reason_guard_fires_if_ever_populated(
        self, agentic_discovery, max_tokens, expected
    ):
        """The belt-and-braces check works once the envelope carries a stopReason.

        structured_output() does not populate stopReason today, so this guard is
        inert in production — but it must actually work if that changes, or it is
        decoration rather than defence.
        """
        agentic_discovery.rules_config.max_tokens = max_tokens
        structured = MagicMock()
        rc = MagicMock()
        rc.model_dump.return_value = {
            "x-aws-idp-policy-type": "p",
            "rule_properties": {"a": {"description": "Q?"}},
        }
        structured.rule_classes = [rc]

        with patch(
            "idp_common.discovery.rules_discovery.structured_output",
            create=True,
            return_value=(structured, {"response": {"stopReason": "max_tokens"}}),
        ):
            with pytest.raises(RulesTruncatedError) as exc:
                agentic_discovery._extract_rules(b"pdf-bytes", "pdf")
        assert expected in str(exc.value)

    def test_untruncated_agentic_response_succeeds(self, agentic_discovery):
        structured = MagicMock()
        rc = MagicMock()
        rc.model_dump.return_value = {
            "x-aws-idp-policy-type": "p",
            "rule_properties": {"a": {"description": "Q?"}},
        }
        structured.rule_classes = [rc]

        with patch(
            "idp_common.discovery.rules_discovery.structured_output",
            create=True,
            return_value=(structured, {"response": {"stopReason": "end_turn"}}),
        ):
            rules = agentic_discovery._extract_rules(b"pdf-bytes", "pdf")

        assert rules is not None and len(rules) == 1


@pytest.mark.unit
class TestIsMaxTokensError:
    """Detector for Strands' truncation failure, matched without importing it."""

    def test_matches_by_type_name(self):
        class MaxTokensReachedException(Exception):
            pass

        assert _is_max_tokens_error(MaxTokensReachedException("anything"))

    def test_matches_by_message(self):
        assert _is_max_tokens_error(
            Exception(
                "Agent has reached an unrecoverable state due to max_tokens limit"
            )
        )

    def test_does_not_match_unrelated_errors(self):
        for message in (
            "bedrock exploded",
            "ValidationException: input is too long",
            "throttled",
        ):
            assert not _is_max_tokens_error(Exception(message)), message
