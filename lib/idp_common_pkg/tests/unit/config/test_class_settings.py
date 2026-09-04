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
        existing = {"$id": "Invoice", "x-aws-idp-document-type": "Invoice-Old"}
        new = {"$id": "Invoice", "x-aws-idp-document-type": "Invoice"}

        with caplog.at_level(logging.WARNING):
            carry_forward_authored_settings(existing, new)

        assert "x-aws-idp-document-type" in caplog.text
        assert new["x-aws-idp-document-type"] == "Invoice"

    def test_an_unchanged_extension_key_is_not_reported_as_replaced(self, caplog):
        existing = {"$id": "Invoice", "x-aws-idp-document-type": "Invoice"}
        new = {"$id": "Invoice", "x-aws-idp-document-type": "Invoice"}

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
