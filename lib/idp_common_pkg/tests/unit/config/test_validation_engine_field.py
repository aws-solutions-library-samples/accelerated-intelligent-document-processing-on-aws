# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for x-aws-idp-validation-engine field validation in IDPConfig.

Tests that the policy_classes field validator correctly accepts "llm" and "z3"
and rejects any other values with a clear error message.
"""

import pytest
from pydantic import ValidationError

from idp_common.config.models import IDPConfig
from idp_common.config.schema_constants import (
    X_AWS_IDP_VALIDATION_ENGINE,
)


class TestValidationEngineFieldValidation:
    """Tests for engine field validation in policy_classes."""

    def test_accepts_llm_engine(self):
        """Valid: engine field set to 'llm' should be accepted."""
        config = IDPConfig(
            policy_classes=[
                {
                    "x-aws-idp-policy-type": "compliance",
                    "rule_properties": {
                        "check_amount": {
                            "type": "string",
                            "description": "Check amount",
                            X_AWS_IDP_VALIDATION_ENGINE: "llm",
                        }
                    },
                }
            ]
        )
        assert len(config.policy_classes) == 1

    def test_accepts_z3_engine(self):
        """Valid: engine field set to 'z3' should be accepted."""
        config = IDPConfig(
            policy_classes=[
                {
                    "x-aws-idp-policy-type": "compliance",
                    "rule_properties": {
                        "check_amount": {
                            "type": "string",
                            "description": "Check amount",
                            X_AWS_IDP_VALIDATION_ENGINE: "z3",
                        }
                    },
                }
            ]
        )
        assert len(config.policy_classes) == 1

    def test_accepts_absent_engine_field(self):
        """Valid: absent engine field should be accepted (defaults to 'llm' at runtime)."""
        config = IDPConfig(
            policy_classes=[
                {
                    "x-aws-idp-policy-type": "compliance",
                    "rule_properties": {
                        "check_amount": {
                            "type": "string",
                            "description": "Check amount",
                        }
                    },
                }
            ]
        )
        assert len(config.policy_classes) == 1

    def test_rejects_invalid_engine_value(self):
        """Invalid: engine field with value other than 'llm' or 'z3' should be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            IDPConfig(
                policy_classes=[
                    {
                        "x-aws-idp-policy-type": "compliance",
                        "rule_properties": {
                            "check_amount": {
                                "type": "string",
                                "description": "Check amount",
                                X_AWS_IDP_VALIDATION_ENGINE: "invalid_engine",
                            }
                        },
                    }
                ]
            )
        error_msg = str(exc_info.value)
        assert "invalid_engine" in error_msg
        assert "llm" in error_msg
        assert "z3" in error_msg

    def test_rejects_uppercase_llm(self):
        """Invalid: 'LLM' (uppercase) should be rejected - values are case-sensitive."""
        with pytest.raises(ValidationError) as exc_info:
            IDPConfig(
                policy_classes=[
                    {
                        "x-aws-idp-policy-type": "compliance",
                        "rule_properties": {
                            "check_amount": {
                                "type": "string",
                                X_AWS_IDP_VALIDATION_ENGINE: "LLM",
                            }
                        },
                    }
                ]
            )
        error_msg = str(exc_info.value)
        assert "LLM" in error_msg

    def test_rejects_uppercase_z3(self):
        """Invalid: 'Z3' (uppercase) should be rejected - values are case-sensitive."""
        with pytest.raises(ValidationError) as exc_info:
            IDPConfig(
                policy_classes=[
                    {
                        "x-aws-idp-policy-type": "compliance",
                        "rule_properties": {
                            "check_amount": {
                                "type": "string",
                                X_AWS_IDP_VALIDATION_ENGINE: "Z3",
                            }
                        },
                    }
                ]
            )
        error_msg = str(exc_info.value)
        assert "Z3" in error_msg

    def test_rejects_empty_string_engine(self):
        """Invalid: empty string should be rejected."""
        with pytest.raises(ValidationError):
            IDPConfig(
                policy_classes=[
                    {
                        "x-aws-idp-policy-type": "compliance",
                        "rule_properties": {
                            "check_amount": {
                                "type": "string",
                                X_AWS_IDP_VALIDATION_ENGINE: "",
                            }
                        },
                    }
                ]
            )

    def test_accepts_mixed_engines_in_same_policy_class(self):
        """Valid: different rules can have different valid engine values."""
        config = IDPConfig(
            policy_classes=[
                {
                    "x-aws-idp-policy-type": "compliance",
                    "rule_properties": {
                        "amount_check": {
                            "type": "string",
                            "description": "Check amount",
                            X_AWS_IDP_VALIDATION_ENGINE: "z3",
                        },
                        "signature_check": {
                            "type": "string",
                            "description": "Check signature",
                            X_AWS_IDP_VALIDATION_ENGINE: "llm",
                        },
                        "date_check": {
                            "type": "string",
                            "description": "Check date",
                            # No engine field - defaults to llm at runtime
                        },
                    },
                }
            ]
        )
        assert len(config.policy_classes) == 1

    def test_rejects_invalid_engine_in_second_policy_class(self):
        """Invalid: validation catches errors across multiple policy classes."""
        with pytest.raises(ValidationError) as exc_info:
            IDPConfig(
                policy_classes=[
                    {
                        "x-aws-idp-policy-type": "compliance-1",
                        "rule_properties": {
                            "valid_rule": {
                                "type": "string",
                                X_AWS_IDP_VALIDATION_ENGINE: "llm",
                            }
                        },
                    },
                    {
                        "x-aws-idp-policy-type": "compliance-2",
                        "rule_properties": {
                            "invalid_rule": {
                                "type": "string",
                                X_AWS_IDP_VALIDATION_ENGINE: "gpt",
                            }
                        },
                    },
                ]
            )
        error_msg = str(exc_info.value)
        assert "gpt" in error_msg
        assert "invalid_rule" in error_msg

    def test_error_message_includes_property_name(self):
        """Error message should include the property name for debugging."""
        with pytest.raises(ValidationError) as exc_info:
            IDPConfig(
                policy_classes=[
                    {
                        "x-aws-idp-policy-type": "compliance",
                        "rule_properties": {
                            "my_special_rule": {
                                "type": "string",
                                X_AWS_IDP_VALIDATION_ENGINE: "bad_value",
                            }
                        },
                    }
                ]
            )
        error_msg = str(exc_info.value)
        assert "my_special_rule" in error_msg
        assert "bad_value" in error_msg

    def test_accepts_empty_policy_classes(self):
        """Valid: empty policy_classes list should be accepted."""
        config = IDPConfig(policy_classes=[])
        assert config.policy_classes == []

    def test_accepts_policy_class_without_rule_properties(self):
        """Valid: policy class without rule_properties should be accepted."""
        config = IDPConfig(
            policy_classes=[
                {
                    "x-aws-idp-policy-type": "compliance",
                }
            ]
        )
        assert len(config.policy_classes) == 1
