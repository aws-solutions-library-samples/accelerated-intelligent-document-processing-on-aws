# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`ExternalIdPEmailMutable` sets a Cognito schema flag that is fixed at pool
creation (#835). Changing it on an existing stack fails the update and can leave
the stack in UPDATE_ROLLBACK_FAILED — the exact failure that got the earlier
unconditional fix reverted. The SDK's deploy path must therefore refuse a
differing value on update, and must not fail a create against a template that
predates the parameter when a caller injects the safe default for new stacks.
"""

import pytest

from idp_sdk._core.stack import (
    CREATION_FIXED_PARAMETERS,
    guard_creation_fixed_parameters,
)

NAME = "ExternalIdPEmailMutable"


def test_the_parameter_is_registered_with_the_template_default():
    assert CREATION_FIXED_PARAMETERS[NAME] == "false"


class TestUpdate:
    def test_differing_value_on_update_is_refused(self):
        with pytest.raises(ValueError, match="cannot be changed on an existing stack"):
            guard_creation_fixed_parameters(
                {NAME: "true"}, stack_exists=True, current_params={NAME: "false"}
            )

    def test_stack_created_before_the_parameter_counts_as_the_default(self):
        """A pre-#835 stack has no such parameter; its pool was created with the
        template's historical `false`, so asking for `true` must be refused."""
        with pytest.raises(ValueError, match="the stack has 'false'"):
            guard_creation_fixed_parameters(
                {NAME: "true"}, stack_exists=True, current_params={"LogLevel": "INFO"}
            )

    def test_identical_value_passes_through(self):
        out = guard_creation_fixed_parameters(
            {NAME: "true", "LogLevel": "DEBUG"},
            stack_exists=True,
            current_params={NAME: "true"},
        )
        assert out == {NAME: "true", "LogLevel": "DEBUG"}

    def test_default_value_on_a_pre_existing_stack_passes_through(self):
        out = guard_creation_fixed_parameters(
            {NAME: "false"}, stack_exists=True, current_params={}
        )
        assert out == {NAME: "false"}

    def test_absent_parameter_is_untouched(self):
        out = guard_creation_fixed_parameters(
            {"LogLevel": "DEBUG"}, stack_exists=True, current_params={NAME: "false"}
        )
        assert out == {"LogLevel": "DEBUG"}


class TestCreate:
    def test_dropped_when_the_template_does_not_declare_it(self):
        out = guard_creation_fixed_parameters(
            {NAME: "true", "AdminEmail": "a@b.c"},
            stack_exists=False,
            declared_params={"AdminEmail", "LogLevel"},
        )
        assert out == {"AdminEmail": "a@b.c"}

    def test_kept_when_the_template_declares_it(self):
        out = guard_creation_fixed_parameters(
            {NAME: "true"}, stack_exists=False, declared_params={NAME, "AdminEmail"}
        )
        assert out == {NAME: "true"}

    def test_kept_when_the_declared_set_is_unknown(self):
        """validate_template failed or was skipped: do not second-guess the caller."""
        out = guard_creation_fixed_parameters(
            {NAME: "true"}, stack_exists=False, declared_params=set()
        )
        assert out == {NAME: "true"}
        out = guard_creation_fixed_parameters(
            {NAME: "true"}, stack_exists=False, declared_params=None
        )
        assert out == {NAME: "true"}


def test_never_mutates_the_input():
    given = {NAME: "true"}
    guard_creation_fixed_parameters(given, stack_exists=False, declared_params={"X"})
    assert given == {NAME: "true"}
