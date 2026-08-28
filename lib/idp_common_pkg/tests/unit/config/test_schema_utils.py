# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the shared ``$ref`` dereferencer (``config/schema_utils.py``).

This helper is the single implementation behind the assessment description
formatter, the classification attribute-name walk, and the assessment
escalation-skip reason — so its contract (sibling override, chain following,
graceful degradation on anything unresolvable) is pinned here rather than
re-asserted through each consumer.
"""

import pytest

from idp_common.config.schema_utils import deref_schema


@pytest.mark.unit
class TestDerefSchema:
    def test_resolves_local_ref_into_defs(self):
        root = {"$defs": {"Group": {"type": "object", "description": "A group"}}}
        assert deref_schema({"$ref": "#/$defs/Group"}, root) == {
            "type": "object",
            "description": "A group",
        }

    def test_node_without_ref_is_returned_unchanged(self):
        node = {"type": "string", "description": "plain"}
        assert deref_schema(node, {"$defs": {}}) is node

    def test_sibling_keys_override_the_definition(self):
        root = {"$defs": {"D": {"type": "object", "description": "generic"}}}
        resolved = deref_schema({"$ref": "#/$defs/D", "description": "specific"}, root)
        assert resolved["description"] == "specific"
        assert resolved["type"] == "object"

    def test_follows_ref_chains(self):
        root = {
            "$defs": {
                "Alias": {"$ref": "#/$defs/Real"},
                "Real": {"type": "object", "description": "the real one"},
            }
        }
        resolved = deref_schema({"$ref": "#/$defs/Alias"}, root)
        assert resolved["description"] == "the real one"
        assert "$ref" not in resolved

    def test_sibling_survives_a_chain(self):
        root = {
            "$defs": {
                "Alias": {"$ref": "#/$defs/Real"},
                "Real": {"type": "object", "description": "generic"},
            }
        }
        resolved = deref_schema({"$ref": "#/$defs/Alias", "description": "mine"}, root)
        assert resolved["description"] == "mine"

    def test_dangling_ref_returns_node_as_is(self, caplog):
        node = {"$ref": "#/$defs/Missing"}
        assert deref_schema(node, {"$defs": {}}) is node
        assert "Dangling $ref" in caplog.text

    def test_remote_ref_returns_node_as_is(self):
        node = {"$ref": "https://example.com/schema.json"}
        assert deref_schema(node, {"$defs": {}}) is node

    def test_cyclic_ref_stops_instead_of_recursing(self, caplog):
        root = {"$defs": {"Loop": {"$ref": "#/$defs/Loop"}}}
        # Terminates and reports rather than blowing the stack.
        assert deref_schema({"$ref": "#/$defs/Loop"}, root) == {"$ref": "#/$defs/Loop"}
        assert "Circular $ref" in caplog.text

    @pytest.mark.parametrize("node", [None, "string", 42, [], True])
    def test_non_dict_node_yields_empty_dict(self, node):
        assert deref_schema(node, {"$defs": {}}) == {}

    def test_non_string_ref_value_is_not_followed(self):
        node = {"$ref": 123, "type": "string"}
        assert deref_schema(node, {"$defs": {}}) is node

    def test_missing_defs_in_root_degrades(self):
        node = {"$ref": "#/$defs/Group"}
        assert deref_schema(node, {}) is node
