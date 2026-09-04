# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for carrying authored class settings across a class regeneration.

Regression for #764: regenerating an existing class (Discovery, or BDA
blueprint optimization) assigned the generated dict over the existing one and
erased every class-level ``x-aws-idp-*`` key an author had set. The write
reported success and the loss only surfaced in the next document processed.
"""

import logging

import pytest

from idp_common.config.class_settings import carry_forward_authored_settings


@pytest.mark.unit
class TestCarryForwardAuthoredSettings:
    def test_authored_keys_the_generator_did_not_emit_are_preserved(self):
        existing = {
            "$id": "Pay-Statement",
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "x-aws-idp-extraction-model": "us.anthropic.claude-opus-4-1-20250805-v1:0",
            "x-aws-idp-confidence-threshold": 0.9,
            "x-aws-idp-multi-instance": True,
            "x-aws-idp-document-name-regex": r"pay.*stub",
        }
        new = {
            "$id": "Pay-Statement",
            "type": "object",
            "properties": {"b": {"type": "number"}},
        }

        carried = carry_forward_authored_settings(existing, new)

        assert new["x-aws-idp-extraction-model"] == (
            "us.anthropic.claude-opus-4-1-20250805-v1:0"
        )
        assert new["x-aws-idp-confidence-threshold"] == 0.9
        assert new["x-aws-idp-multi-instance"] is True
        assert new["x-aws-idp-document-name-regex"] == r"pay.*stub"
        assert set(carried) == {
            "x-aws-idp-extraction-model",
            "x-aws-idp-confidence-threshold",
            "x-aws-idp-multi-instance",
            "x-aws-idp-document-name-regex",
        }

    def test_the_generator_still_owns_what_it_emitted(self):
        """Fresh properties are the point of re-running discovery."""
        existing = {"$id": "Invoice", "properties": {"old": {"type": "string"}}}
        new = {"$id": "Invoice", "properties": {"new": {"type": "string"}}}

        carry_forward_authored_settings(existing, new)

        assert new["properties"] == {"new": {"type": "string"}}

    def test_a_falsy_authored_value_is_carried_not_skipped(self):
        """``exclude-from-processing: false`` and ``threshold: 0`` are settings.

        A truthiness test here would drop exactly the values an author set to
        turn something off.
        """
        existing = {
            "$id": "Blank-Page",
            "x-aws-idp-exclude-from-processing": False,
            "x-aws-idp-confidence-threshold": 0,
            "x-aws-idp-exclusion-reason": "",
        }
        new = {"$id": "Blank-Page", "properties": {}}

        carry_forward_authored_settings(existing, new)

        assert new["x-aws-idp-exclude-from-processing"] is False
        assert new["x-aws-idp-confidence-threshold"] == 0
        assert new["x-aws-idp-exclusion-reason"] == ""

    def test_synthesized_keys_lose_to_an_authored_value(self):
        """A caller-derived value is not generator output.

        Discovery synthesizes ``description`` from a class id it had to rename;
        an author's own description must win over it.
        """
        existing = {"$id": "Task-cards", "description": "hand-written description"}
        new = {"$id": "Task-cards", "description": "Task cards"}

        carried = carry_forward_authored_settings(
            existing, new, synthesized={"description"}
        )

        assert new["description"] == "hand-written description"
        assert carried == ["description"]

    def test_a_key_the_generator_emitted_is_not_treated_as_synthesized(self):
        existing = {"$id": "Invoice", "description": "old"}
        new = {"$id": "Invoice", "description": "generated"}

        carried = carry_forward_authored_settings(existing, new)

        assert new["description"] == "generated"
        assert carried == []

    def test_replacing_an_authored_extension_key_is_logged(self, caplog):
        """The one loss that remains has to be visible at write time."""
        existing = {"$id": "Invoice", "x-aws-idp-extraction-model": "authored-model"}
        new = {"$id": "Invoice", "x-aws-idp-extraction-model": "generated-model"}

        with caplog.at_level(logging.WARNING):
            carry_forward_authored_settings(existing, new)

        assert "x-aws-idp-extraction-model" in caplog.text
        assert new["x-aws-idp-extraction-model"] == "generated-model"

    def test_an_unchanged_extension_key_is_not_reported_as_replaced(self, caplog):
        existing = {"$id": "Invoice", "x-aws-idp-extraction-model": "same"}
        new = {"$id": "Invoice", "x-aws-idp-extraction-model": "same"}

        with caplog.at_level(logging.WARNING):
            carry_forward_authored_settings(existing, new)

        assert caplog.text == ""

    def test_no_existing_settings_is_a_no_op(self):
        new = {"$id": "Invoice", "properties": {}}

        assert carry_forward_authored_settings({}, new) == []
        assert new == {"$id": "Invoice", "properties": {}}

    def test_property_level_keys_are_out_of_scope(self):
        """Deliberate: a regenerated attribute can change type, and carrying a
        stale per-field evaluation method onto it could be worse than losing
        it. Documented so the boundary is a decision, not an oversight."""
        existing = {
            "$id": "Invoice",
            "properties": {
                "total": {"type": "string", "x-aws-idp-evaluation-method": "EXACT"}
            },
        }
        new = {"$id": "Invoice", "properties": {"total": {"type": "number"}}}

        carry_forward_authored_settings(existing, new)

        assert new["properties"]["total"] == {"type": "number"}


@pytest.mark.unit
class TestKeysCoupledToProperties:
    """The carve-outs from "preserve anything the generator did not emit".

    Each of these keys describes the *content* of ``properties``, which the
    generator just replaced. Carrying them turned a lost setting into a louder
    failure somewhere else — an aborted config save, or a class that fails
    extraction validation on every document. Both were reachable with shipped
    presets, so these tests use their real shapes.
    """

    def test_instance_array_is_dropped_when_its_property_is_gone(self, caplog):
        """`config_library/unified/ocr-benchmark` sets this on BANK_CHECK.

        `IDPConfig.validate_instance_array` RAISES when the named property is
        absent, and the discovery save path constructs `IDPConfig` — so carrying
        it blindly aborts the whole save with a Pydantic error instead of losing
        one setting.
        """
        existing = {
            "$id": "BANK_CHECK",
            "x-aws-idp-instance-array": "checks",
            "properties": {"checks": {"type": "array", "items": {"type": "object"}}},
        }
        new = {"$id": "BANK_CHECK", "properties": {"AccountNumber": {"type": "string"}}}

        with caplog.at_level(logging.WARNING):
            carried = carry_forward_authored_settings(existing, new)

        assert "x-aws-idp-instance-array" not in new
        assert "x-aws-idp-instance-array" not in carried
        assert "instance-array" in caplog.text, "the drop must not be silent"

    def test_instance_array_is_kept_when_its_property_survives(self):
        existing = {
            "$id": "BANK_CHECK",
            "x-aws-idp-instance-array": "checks",
            "properties": {"checks": {"type": "array", "items": {"type": "object"}}},
        }
        new = {
            "$id": "BANK_CHECK",
            "properties": {"checks": {"type": "array", "items": {"type": "object"}}},
        }

        carry_forward_authored_settings(existing, new)

        assert new["x-aws-idp-instance-array"] == "checks"

    def test_the_dropped_instance_array_leaves_a_class_that_validates(self):
        """The point of the carve-out, asserted against the real validator."""
        from idp_common.config.models import IDPConfig

        existing = {
            "$id": "BANK_CHECK",
            "x-aws-idp-instance-array": "checks",
            "properties": {"checks": {"type": "array", "items": {"type": "object"}}},
        }
        new = {
            "$id": "BANK_CHECK",
            "type": "object",
            "properties": {"AccountNumber": {"type": "string"}},
        }

        carry_forward_authored_settings(existing, new)

        IDPConfig(**{"classes": [new]})  # must not raise

    def test_required_is_not_carried(self):
        """A stale `required` naming a property that no longer exists is validated
        against every extracted object, so it reports a missing required property
        on every document, forever. Shipped in lending-package-sample and
        bank-statement-sample."""
        existing = {
            "$id": "Payslip",
            "required": ["PayDate", "CurrentGrossPay"],
            "properties": {"PayDate": {"type": "string"}},
            "x-aws-idp-extraction-model": "us.amazon.nova-pro-v1:0",
        }
        new = {"$id": "Payslip", "properties": {"Total": {"type": "number"}}}

        carried = carry_forward_authored_settings(existing, new)

        assert "required" not in new
        assert carried == ["x-aws-idp-extraction-model"]

    def test_defs_are_not_carried(self):
        """`$defs` holds the group/list-item definitions the OLD properties
        referenced; the regenerated ones reference their own."""
        existing = {
            "$id": "Payslip",
            "$defs": {"Deduction": {"type": "object"}},
            "properties": {"Deductions": {"$ref": "#/$defs/Deduction"}},
        }
        new = {"$id": "Payslip", "properties": {"Total": {"type": "number"}}}

        carry_forward_authored_settings(existing, new)

        assert "$defs" not in new

    def test_multi_instance_flag_is_still_carried(self):
        """The boolean flag is a class-level setting, not a pointer into
        properties — losing it is the #715 record loss re-opening."""
        existing = {"$id": "Pay-Statement", "x-aws-idp-multi-instance": True}
        new = {"$id": "Pay-Statement", "properties": {"a": {"type": "string"}}}

        carry_forward_authored_settings(existing, new)

        assert new["x-aws-idp-multi-instance"] is True


@pytest.mark.unit
class TestCarriedValuesAreCopies:
    def test_a_carried_list_is_not_shared_with_the_existing_class(self):
        """`_apply_optimized_schema` returns the regenerated schema to a caller
        that may still hold the existing class dict, so aliasing a mutable value
        would let one mutate the other."""
        examples = [{"name": "ex1"}]
        existing = {"$id": "Invoice", "x-aws-idp-examples": examples}
        new = {"$id": "Invoice", "properties": {}}

        carry_forward_authored_settings(existing, new)

        assert new["x-aws-idp-examples"] == examples
        assert new["x-aws-idp-examples"] is not examples
        new["x-aws-idp-examples"][0]["name"] = "mutated"
        assert examples[0]["name"] == "ex1"


@pytest.mark.unit
class TestReplacementWarning:
    def test_a_replaced_description_is_warned_about(self, caplog):
        """Discovery's prompt asks the model for a `description`, so an authored
        one really is replaced on the ordinary path — and it is functional: the
        classification prompt's class table is built from it."""
        existing = {"$id": "Invoice", "description": "hand-written, load-bearing"}
        new = {"$id": "Invoice", "description": "model-generated"}

        with caplog.at_level(logging.WARNING):
            carry_forward_authored_settings(existing, new)

        assert "description" in caplog.text
        assert new["description"] == "model-generated"

    def test_normalizing_the_class_id_is_not_reported_as_a_replaced_setting(
        self, caplog
    ):
        """The id is rewritten by the caller, not clobbered by the generator, and
        `_normalize_class_id` already logs the rename. Warning here too would fire
        on every id repair — a false alarm on the flow #764 advertises as fixed."""
        existing = {"$id": "Task cards", "x-aws-idp-document-type": "Task cards"}
        new = {"$id": "Task-cards", "x-aws-idp-document-type": "Task-cards"}

        with caplog.at_level(logging.WARNING):
            carry_forward_authored_settings(existing, new)

        assert caplog.text == ""
