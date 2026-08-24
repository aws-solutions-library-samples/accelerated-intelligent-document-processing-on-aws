# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for Z3-related schema constants.

Validates: Requirements 9.1, 9.2, 9.3
"""

from idp_common.config.schema_constants import (
    VALID_VALIDATION_ENGINES,
    VALIDATION_ENGINE_LLM,
    VALIDATION_ENGINE_Z3,
    X_AWS_IDP_VALIDATION_ENGINE,
)


class TestValidationEngineConstants:
    """Tests for the validation engine schema constants."""

    def test_x_aws_idp_validation_engine_value(self):
        """Verify X_AWS_IDP_VALIDATION_ENGINE equals 'x-aws-idp-validation-engine'."""
        assert X_AWS_IDP_VALIDATION_ENGINE == "x-aws-idp-validation-engine"

    def test_validation_engine_llm_value(self):
        """Verify VALIDATION_ENGINE_LLM equals 'llm'."""
        assert VALIDATION_ENGINE_LLM == "llm"

    def test_validation_engine_z3_value(self):
        """Verify VALIDATION_ENGINE_Z3 equals 'z3'."""
        assert VALIDATION_ENGINE_Z3 == "z3"

    def test_valid_validation_engines_is_frozenset(self):
        """Verify VALID_VALIDATION_ENGINES is a frozenset."""
        assert isinstance(VALID_VALIDATION_ENGINES, frozenset)

    def test_valid_validation_engines_has_exactly_two_members(self):
        """Verify VALID_VALIDATION_ENGINES contains exactly 2 members."""
        assert len(VALID_VALIDATION_ENGINES) == 2

    def test_valid_validation_engines_contains_llm(self):
        """Verify VALID_VALIDATION_ENGINES contains 'llm'."""
        assert VALIDATION_ENGINE_LLM in VALID_VALIDATION_ENGINES

    def test_valid_validation_engines_contains_z3(self):
        """Verify VALID_VALIDATION_ENGINES contains 'z3'."""
        assert VALIDATION_ENGINE_Z3 in VALID_VALIDATION_ENGINES

    def test_valid_validation_engines_members_match_constants(self):
        """Verify VALID_VALIDATION_ENGINES contains exactly the LLM and Z3 constants."""
        expected = frozenset([VALIDATION_ENGINE_LLM, VALIDATION_ENGINE_Z3])
        assert VALID_VALIDATION_ENGINES == expected
