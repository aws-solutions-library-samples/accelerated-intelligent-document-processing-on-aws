# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tool-schema name sanitization (GitHub #709).

Bedrock rejects ``toolSpec.inputSchema`` property keys outside
``^[a-zA-Z0-9_.-]{1,64}$``, so a document class authored for humans
(``"Account Number"``) cannot be sent as a tool schema. Four shipped presets
contain such names.

The load-bearing properties, in order of how badly getting them wrong would hurt:

1. **Round-trip fidelity.** A response must restore to the authored names
   exactly, or every downstream consumer (evaluation baselines, Athena columns,
   the UI, the SDK ``fields`` contract) sees a renamed field.
2. **Collisions cannot merge two fields.** Two distinct names must never reduce
   to the same key — that silently drops data.
3. **Recursion.** Bedrock only checks the TOP level today. Sanitizing only the
   top level would work now and break the moment a class is wrapped in a list,
   or the day AWS makes the check recursive.
4. **No churn.** A schema Bedrock already accepts must be sent unchanged.
"""

from __future__ import annotations

import json

import pytest

from idp_common.bedrock.tool_schema import (
    MAX_PROPERTY_NAME_LENGTH,
    find_document_metadata_keywords,
    find_invalid_property_names,
    is_valid_tool_property_name,
    restore_names,
    sanitize_tool_schema,
)


# --------------------------------------------------------------------------- #
# the pattern itself
# --------------------------------------------------------------------------- #
class TestNameValidity:
    @pytest.mark.parametrize(
        "name", ["Amount", "account_number", "Total.USD", "a-b", "A1", "_x", "x" * 64]
    )
    def test_accepted(self, name):
        assert is_valid_tool_property_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "Account Number",  # space -- the common real case
            "Purchase Date and Time",
            "Total (USD)",
            "amount%",
            "naïve",
            "a/b",
            "",
            "x" * 65,  # over the length limit
        ],
    )
    def test_rejected(self, name):
        assert is_valid_tool_property_name(name) is False


# --------------------------------------------------------------------------- #
# sanitize + restore round trip
# --------------------------------------------------------------------------- #
class TestRoundTrip:
    SCHEMA = {
        "type": "object",
        "properties": {
            "Account Number": {"type": "string"},
            "Statement Period": {"type": "string"},
            "Amount": {"type": "number"},  # already valid
        },
        "required": ["Account Number", "Amount"],
    }

    def test_every_name_is_wire_valid_after_sanitizing(self):
        clean, _ = sanitize_tool_schema(self.SCHEMA)
        assert find_invalid_property_names(clean) == []

    def test_already_valid_names_are_untouched(self):
        """No gratuitous churn: a name Bedrock accepts keeps its exact spelling."""
        clean, name_map = sanitize_tool_schema(self.SCHEMA)
        assert "Amount" in clean["properties"]
        assert "Amount" not in name_map.renamed

    def test_response_restores_the_authored_names(self):
        clean, name_map = sanitize_tool_schema(self.SCHEMA)
        # what the model would return, keyed by the SANITIZED names
        response = {k: f"v-{k}" for k in clean["properties"]}
        restored = restore_names(response, name_map)
        assert set(restored) == set(self.SCHEMA["properties"])

    def test_required_is_rewritten_to_the_sanitized_names(self):
        """`required` names pre-sanitization keys; left alone it would reference
        properties that no longer exist and Bedrock would reject the schema."""
        clean, _ = sanitize_tool_schema(self.SCHEMA)
        assert set(clean["required"]) <= set(clean["properties"]), clean["required"]

    def test_the_input_is_not_mutated(self):
        before = json.dumps(self.SCHEMA, sort_keys=True)
        sanitize_tool_schema(self.SCHEMA)
        assert json.dumps(self.SCHEMA, sort_keys=True) == before

    def test_a_fully_valid_schema_produces_an_empty_map_and_a_noop_restore(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        clean, name_map = sanitize_tool_schema(schema)
        assert clean == schema
        assert name_map.is_empty()
        payload = {"a": 1}
        assert restore_names(payload, name_map) == payload


# --------------------------------------------------------------------------- #
# collisions -- the failure that would silently DROP a field
# --------------------------------------------------------------------------- #
class TestCollisions:
    def test_two_names_reducing_to_the_same_token_stay_distinct(self):
        schema = {
            "type": "object",
            "properties": {
                "Total USD": {"type": "number"},
                "Total_USD": {"type": "number"},
                "Total-USD": {"type": "number"},
            },
        }
        clean, name_map = sanitize_tool_schema(schema)
        assert len(clean["properties"]) == 3, clean["properties"]
        response = {k: i for i, k in enumerate(clean["properties"])}
        restored = restore_names(response, name_map)
        assert set(restored) == set(schema["properties"])
        assert len(restored) == 3

    def test_an_over_long_name_is_truncated_and_still_unique(self):
        base = "Very Long Field Name That Exceeds The Bedrock Limit For Property Keys"
        schema = {
            "type": "object",
            "properties": {
                base + " One": {"type": "string"},
                base + " Two": {"type": "string"},
            },
        }
        clean, name_map = sanitize_tool_schema(schema)
        assert len(clean["properties"]) == 2
        for k in clean["properties"]:
            assert len(k) <= MAX_PROPERTY_NAME_LENGTH, k
            assert is_valid_tool_property_name(k)
        response = {k: 1 for k in clean["properties"]}
        assert set(restore_names(response, name_map)) == set(schema["properties"])

    def test_a_name_of_only_illegal_characters_still_yields_a_usable_key(self):
        schema = {"type": "object", "properties": {"€ %": {"type": "string"}}}
        clean, name_map = sanitize_tool_schema(schema)
        (key,) = clean["properties"]
        assert is_valid_tool_property_name(key)
        assert restore_names({key: 1}, name_map) == {"€ %": 1}


# --------------------------------------------------------------------------- #
# recursion -- Bedrock only checks the top level TODAY
# --------------------------------------------------------------------------- #
class TestRecursion:
    NESTED = {
        "type": "object",
        "properties": {
            "Account Holder": {
                "type": "object",
                "properties": {
                    "Full Name": {"type": "string"},
                    "ZIP Code": {"type": "string"},
                },
            },
            "Transactions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "Txn Date": {"type": "string"},
                        "Amount": {"type": "number"},
                    },
                },
            },
        },
    }

    def test_nested_object_names_are_sanitized(self):
        clean, _ = sanitize_tool_schema(self.NESTED)
        assert find_invalid_property_names(clean) == []

    def test_nested_object_names_restore(self):
        clean, name_map = sanitize_tool_schema(self.NESTED)
        holder_key = next(k for k in clean["properties"] if k.startswith("Account"))
        inner = clean["properties"][holder_key]["properties"]
        response = {holder_key: {k: "v" for k in inner}}
        restored = restore_names(response, name_map)
        assert set(restored["Account Holder"]) == {"Full Name", "ZIP Code"}

    def test_array_item_names_are_sanitized_and_restore_for_every_row(self):
        clean, name_map = sanitize_tool_schema(self.NESTED)
        txn_key = next(k for k in clean["properties"] if k.startswith("Transactions"))
        item_props = clean["properties"][txn_key]["items"]["properties"]
        response = {txn_key: [{k: i for k in item_props} for i in range(3)]}
        restored = restore_names(response, name_map)
        rows = restored["Transactions"]
        assert len(rows) == 3
        for row in rows:
            assert set(row) == {"Txn Date", "Amount"}

    def test_defs_property_names_are_sanitized(self):
        schema = {
            "type": "object",
            "$defs": {
                "Txn": {
                    "type": "object",
                    "properties": {"Txn Date": {"type": "string"}},
                }
            },
            "properties": {
                "Transactions": {"type": "array", "items": {"$ref": "#/$defs/Txn"}}
            },
        }
        clean, _ = sanitize_tool_schema(schema)
        assert find_invalid_property_names(clean) == []
        # An already-valid definition name is left exactly as it is.
        assert "Txn" in clean["$defs"]
        assert clean["properties"]["Transactions"]["items"]["$ref"] == "#/$defs/Txn"

    def test_a_spaced_defs_name_is_renamed_and_every_ref_rewritten(self):
        """#783: Sonnet 5 does not resolve ``#/$defs/Account Holder Address`` and
        returns the group as a JSON string, making every section schema-invalid.
        Bedrock's validation rule never covered definition names, which is why they
        were left alone; the model still has to RESOLVE the pointer."""
        addr = {"type": "object", "properties": {"City": {"type": "string"}}}
        schema = {
            "type": "object",
            "$defs": {"Account Holder Address": addr},
            "properties": {
                "Account Holder Address": {"$ref": "#/$defs/Account Holder Address"},
                # Percent-encoded spelling of the same pointer must resolve too.
                "Mailing": {"$ref": "#/$defs/Account%20Holder%20Address"},
                "Prior": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/Account Holder Address"},
                },
            },
        }
        clean, name_map = sanitize_tool_schema(schema)
        assert list(clean["$defs"]) == ["Account_Holder_Address"]
        assert name_map.defs_renamed == {
            "Account_Holder_Address": "Account Holder Address"
        }
        props = clean["properties"]
        assert (
            props["Account_Holder_Address"]["$ref"] == "#/$defs/Account_Holder_Address"
        )
        assert props["Mailing"]["$ref"] == "#/$defs/Account_Holder_Address"
        assert props["Prior"]["items"]["$ref"] == "#/$defs/Account_Holder_Address"
        assert find_invalid_property_names(clean) == []
        # The whole wire schema is free of anything that must resolve by luck.
        import json

        assert " " not in json.dumps(clean["$defs"], separators=(",", ":"))
        assert all(
            " " not in p["$ref"] for p in (props["Mailing"], props["Prior"]["items"])
        )

    def test_a_ref_gains_the_definitions_type_as_belt_and_braces(self):
        """Arm C of the #783 repro: an explicit ``type: object`` beside the ``$ref``
        made Sonnet 5 return an object even for the pointer it mis-resolved."""
        schema = {
            "type": "object",
            "$defs": {"Addr": {"type": "object", "properties": {"City": {}}}},
            "properties": {
                "Home": {"$ref": "#/$defs/Addr"},
                "Typed": {"type": "object", "$ref": "#/$defs/Addr"},
            },
        }
        clean, _ = sanitize_tool_schema(schema)
        assert clean["properties"]["Home"]["type"] == "object"
        assert clean["properties"]["Typed"]["type"] == "object"  # untouched
        # Only `type` is copied — never the definition's body.
        assert "properties" not in clean["properties"]["Home"]

    def test_defs_renaming_resolves_collisions_and_restores_inner_names(self):
        schema = {
            "type": "object",
            "$defs": {
                # Both reduce to "Total__USD_"; the second must get a suffix.
                "Total (USD)": {"type": "object", "properties": {"Amt Due": {}}},
                "Total_(USD)": {"type": "object", "properties": {"X": {}}},
            },
            "properties": {
                "T": {"$ref": "#/$defs/Total (USD)"},
                "U": {"$ref": "#/$defs/Total_(USD)"},
            },
        }
        clean, name_map = sanitize_tool_schema(schema)
        assert set(clean["$defs"]) == {"Total__USD_", "Total__USD__2"}
        assert clean["properties"]["T"]["$ref"] == "#/$defs/Total__USD_"
        assert clean["properties"]["U"]["$ref"] == "#/$defs/Total__USD__2"
        # Inner property names inside the renamed definition still restore.
        assert "$defs/Total__USD_" in name_map.children

    def test_invalid_defs_names_are_reported_by_the_diagnostic(self):
        schema = {"type": "object", "$defs": {"Acct Holder": {"type": "object"}}}
        assert find_invalid_property_names(schema) == ["$defs/Acct Holder"]

    def test_anyof_branch_names_are_sanitized(self):
        schema = {
            "anyOf": [
                {"type": "object", "properties": {"A B": {"type": "string"}}},
                {"type": "null"},
            ]
        }
        clean, _ = sanitize_tool_schema(schema)
        assert find_invalid_property_names(clean) == []


# --------------------------------------------------------------------------- #
# restore must not lose data it does not recognize
# --------------------------------------------------------------------------- #
class TestRestoreIsLossless:
    def test_an_unmapped_key_is_kept_not_dropped(self):
        """A model that echoes an unexpected key must stay visible.

        Dropping it is how a hallucinated field becomes invisible instead of
        reviewable -- and the schema-compliance filter, not this function, is
        what decides whether to keep it.
        """
        schema = {"type": "object", "properties": {"A B": {"type": "string"}}}
        clean, name_map = sanitize_tool_schema(schema)
        (key,) = clean["properties"]
        restored = restore_names({key: 1, "surprise": 2}, name_map)
        assert restored == {"A B": 1, "surprise": 2}

    @pytest.mark.parametrize("payload", [None, 5, "str", [], {}, [1, 2]])
    def test_non_object_payloads_pass_through(self, payload):
        schema = {"type": "object", "properties": {"A B": {"type": "string"}}}
        _clean, name_map = sanitize_tool_schema(schema)
        assert restore_names(payload, name_map) == payload

    def test_none_map_is_a_noop(self):
        assert restore_names({"x": 1}, None) == {"x": 1}


# --------------------------------------------------------------------------- #
# The sweep that actually protects the feature: every shipped preset
# --------------------------------------------------------------------------- #
def _shipped_class_schemas():
    """(preset, class label, schema) for every class in config_library/unified."""
    import pathlib

    import yaml

    root = pathlib.Path(__file__).resolve().parents[5] / "config_library" / "unified"
    if not root.is_dir():  # pragma: no cover - packaged install
        pytest.skip(f"{root} not present")
    out = []
    for cfg_path in sorted(root.rglob("*.yaml")):
        try:
            cfg = yaml.safe_load(cfg_path.read_text())
        except Exception:
            continue
        if not isinstance(cfg, dict):
            continue
        for cls in cfg.get("classes") or []:
            if isinstance(cls, dict):
                label = cls.get("$id") or cls.get("x-aws-idp-document-type") or "?"
                out.append((cfg_path.name, label, cls))
    return out


class TestEveryShippedPresetSanitizes:
    def test_the_sweep_finds_classes(self):
        """Guard the guard: a path change must not make this vacuous."""
        schemas = _shipped_class_schemas()
        assert len(schemas) > 20, len(schemas)

    def test_at_least_one_preset_really_needs_sanitizing(self):
        """If none did, the sweep below would pass without exercising anything.

        #709 measured four presets with offending names; this asserts that is
        still true, so the sweep is meaningful.
        """
        offenders = [
            (p, label)
            for p, label, s in _shipped_class_schemas()
            if find_invalid_property_names(s)
        ]
        assert offenders, "no shipped preset has an invalid name -- sweep is vacuous"

    def test_every_shipped_class_becomes_wire_valid(self):
        failures = []
        for preset, label, schema in _shipped_class_schemas():
            clean, _ = sanitize_tool_schema(schema)
            bad = find_invalid_property_names(clean)
            if bad:
                failures.append(f"{preset} :: {label} :: {bad[:5]}")
        assert not failures, "\n".join(failures)

    def test_every_shipped_class_round_trips_its_top_level_names(self):
        """Sanitizing is useless if the names cannot be mapped back."""
        failures = []
        for preset, label, schema in _shipped_class_schemas():
            props = schema.get("properties")
            if not isinstance(props, dict) or not props:
                continue
            clean, name_map = sanitize_tool_schema(schema)
            response = {k: None for k in clean["properties"]}
            restored = restore_names(response, name_map)
            if set(restored) != set(props):
                lost = sorted(set(props) - set(restored))
                failures.append(f"{preset} :: {label} :: lost {lost[:5]}")
        assert not failures, "\n".join(failures)

    def test_no_shipped_class_loses_a_property_to_a_collision(self):
        failures = []
        for preset, label, schema in _shipped_class_schemas():
            props = schema.get("properties")
            if not isinstance(props, dict):
                continue
            clean, _ = sanitize_tool_schema(schema)
            if len(clean.get("properties") or {}) != len(props):
                failures.append(f"{preset} :: {label}")
        assert not failures, "\n".join(failures)


# --------------------------------------------------------------------------- #
# The client refuses an unsanitized schema rather than renaming silently
#
# Sanitizing inside the client would hand the caller a response keyed by names it
# never asked for, with no map to reverse it -- silently renaming every field of
# every extraction. So the client fails locally and names the helper instead,
# turning a ValidationException from inside a retry ladder into a clear error.
# --------------------------------------------------------------------------- #
def _tool_config(schema):
    return {
        "tools": [{"toolSpec": {"name": "extract", "inputSchema": {"json": schema}}}]
    }


class TestClientRejectsUnsanitizedSchema:
    @staticmethod
    def _reject(cfg):
        from idp_common.bedrock.client import BedrockClient

        return BedrockClient._reject_invalid_tool_property_names(cfg)

    def test_a_valid_schema_passes(self):
        self._reject(_tool_config({"type": "object", "properties": {"Amount": {}}}))

    def test_a_space_in_a_top_level_name_raises_and_names_the_helper(self):
        with pytest.raises(ValueError) as exc:
            self._reject(
                _tool_config({"type": "object", "properties": {"Account Number": {}}})
            )
        msg = str(exc.value)
        assert "Account Number" in msg
        assert "sanitize_tool_schema" in msg
        assert "restore_names" in msg

    def test_a_nested_offender_is_reported_even_though_bedrock_would_accept_it(self):
        """The forward-compatibility trap, made loud.

        Bedrock only validates the top level today, so this schema would be
        accepted -- and would break the day that check becomes recursive, or the
        moment the class is wrapped in a list.
        """
        schema = {
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"Txn Date": {}}},
                }
            },
        }
        with pytest.raises(ValueError) as exc:
            self._reject(_tool_config(schema))
        assert "Txn Date" in str(exc.value)
        assert "TOP level" in str(exc.value)

    def test_the_sanitized_schema_passes_the_guard(self):
        """The two halves must actually fit together."""
        schema = {
            "type": "object",
            "properties": {
                "Account Number": {"type": "string"},
                "rows": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"Txn Date": {}}},
                },
            },
        }
        clean, _ = sanitize_tool_schema(schema)
        self._reject(_tool_config(clean))  # must not raise

    def test_a_malformed_tool_config_does_not_raise_on_shape(self):
        """Shape problems are not this function's job; it must not mask them."""
        for cfg in (
            {},
            {"tools": []},
            {"tools": [None]},
            {"tools": [{}]},
            {"tools": [{"toolSpec": {}}]},
        ):
            self._reject(cfg)

    def test_every_shipped_preset_passes_the_guard_after_sanitizing(self):
        """End to end: the four offending presets become sendable."""
        failures = []
        for preset, label, schema in _shipped_class_schemas():
            clean, _ = sanitize_tool_schema(schema)
            try:
                self._reject(_tool_config(clean))
            except ValueError as e:
                failures.append(f"{preset} :: {label} :: {e}")
        assert not failures, "\n".join(failures)


@pytest.mark.unit
class TestDocumentMetadataIsStripped:
    """Bedrock META-validates the tool schema, not just its property names.

    An IDP class schema sets ``$id`` to the document-class NAME, so a class called
    ``"Policy Application Form"`` yields ``$id: "Policy Application Form"`` —
    spaces, therefore not an RFC 3986 URI-reference. Converse rejects the entire
    request:

        ValidationException: The json schema definition at
        toolConfig.tools.0.toolSpec.inputSchema is invalid ...
        $.$id: does not match the uri-reference pattern

    This was not caught by the property-name work (#709) because ``$id`` is not a
    property name, and not by any unit test because none asserted what the WIRE
    schema's top-level keys were. It failed on a live stack, on every section.
    """

    CLASS = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "Policy Application Form",  # the exact shape that failed
        "x-aws-idp-document-type": "form",
        "x-aws-idp-examples": [{"x-aws-idp-class-prompt": "big payload"}],
        "type": "object",
        "description": "kept: describes the value",
        "properties": {
            "Policy Number": {"type": "string", "x-aws-idp-hint": "drop me"},
            "Items": {
                "type": "array",
                "items": {
                    "$id": "nested-id",
                    "type": "object",
                    "properties": {"amount": {"type": "number"}},
                },
            },
        },
    }

    def _wire(self):
        clean, _ = sanitize_tool_schema(self.CLASS)
        return clean

    def test_the_failing_id_never_reaches_the_wire(self):
        assert "$id" not in self._wire()

    def test_schema_dialect_and_idp_extensions_are_dropped(self):
        wire = self._wire()
        assert "$schema" not in wire
        assert not [k for k in wire if k.startswith("x-aws-idp-")]

    def test_nested_metadata_is_stripped_too(self):
        """`$id` is legal on any subschema and would fail the same validation."""
        assert "$id" not in self._wire()["properties"]["Items"]["items"]

    def test_per_property_extensions_are_dropped(self):
        prop = next(iter(self._wire()["properties"].values()))
        assert "x-aws-idp-hint" not in prop

    def test_constraints_and_descriptions_survive(self):
        """Stripping must not remove anything that constrains the value — that
        would silently change what the model is asked for."""
        wire = self._wire()
        assert wire["type"] == "object"
        assert wire["description"] == "kept: describes the value"
        assert len(wire["properties"]) == 2
        items = wire["properties"]["Items"]["items"]
        assert items["properties"]["amount"]["type"] == "number"

    def test_a_field_named_like_a_keyword_is_not_stripped(self):
        """`properties` keys are user-authored field names. A document with a
        field genuinely called "$id" or "id" must keep it — filtering inside
        `properties` would delete real extracted data."""
        clean, _ = sanitize_tool_schema(
            {
                "type": "object",
                "properties": {"id": {"type": "string"}, "$id": {"type": "string"}},
            }
        )
        assert set(clean["properties"]) == {"id", "_id"}, clean["properties"]

    def test_the_finder_reports_what_the_stripper_removes(self):
        """The client guard and the stripper must agree, or the guard fires on
        schemas the stripper already fixed (or misses ones it does not)."""
        assert find_document_metadata_keywords(self.CLASS)
        assert find_document_metadata_keywords(self._wire()) == []


@pytest.mark.unit
class TestEveryShippedPresetProducesAWireValidSchema:
    """The regression net: every class in every shipped preset, end to end."""

    def test_no_preset_leaks_metadata(self):
        import glob
        from pathlib import Path

        import yaml

        # Resolved from __file__, not the cwd: pytest may run from the repo root
        # or from lib/idp_common_pkg, and a relative glob silently matched NOTHING
        # from the latter — the sweep passed while checking zero classes.
        repo = Path(__file__).resolve().parents[5]
        assert (repo / "config_library").is_dir(), (
            f"repo root mis-resolved to {repo}; fix the parents[] index"
        )

        checked = 0
        for path in glob.glob(
            str(repo / "config_library/**/config.yaml"), recursive=True
        ):
            with open(path) as fh:
                try:
                    cfg = yaml.safe_load(fh)
                except yaml.YAMLError:
                    continue
            for cls in (cfg or {}).get("classes") or []:
                if not isinstance(cls, dict) or not cls.get("properties"):
                    continue
                clean, _ = sanitize_tool_schema(cls)
                leaks = find_document_metadata_keywords(clean)
                assert not leaks, f"{path} :: {cls.get('$id')} leaks {leaks}"
                checked += 1
        assert checked > 5, f"expected to check many preset classes, checked {checked}"


class TestDefsPointerRewriteEdges:
    """Every rewritten pointer must RESOLVE, so each case validates a sample with
    jsonschema — a dangling pointer surfaces as an exception rather than passing."""

    @staticmethod
    def _resolves(clean, sample):
        import jsonschema

        jsonschema.Draft202012Validator.check_schema(clean)
        return sorted(
            e.message
            for e in jsonschema.Draft202012Validator(clean).iter_errors(sample)
        )

    def test_a_pointer_into_a_renamed_definition_is_rewritten_segment_wise(self):
        """``#/$defs/Account Holder Address/properties/City`` reuses one field of a
        group — legal, human-authored, and previously left dangling."""
        schema = {
            "type": "object",
            "$defs": {
                "Account Holder Address": {
                    "type": "object",
                    "properties": {"City": {"type": "string"}},
                }
            },
            "properties": {
                "C": {"$ref": "#/$defs/Account Holder Address/properties/City"}
            },
        }
        clean, _ = sanitize_tool_schema(schema)
        assert (
            clean["properties"]["C"]["$ref"]
            == "#/$defs/Account_Holder_Address/properties/City"
        )
        assert self._resolves(clean, {"C": 123}) == ["123 is not of type 'string'"]

    def test_a_defs_block_nested_under_a_property_is_renamed_and_its_pointer_rewritten(
        self,
    ):
        schema = {
            "type": "object",
            "properties": {
                "G": {
                    "type": "object",
                    "$defs": {"Inner Def": {"type": "string"}},
                    "properties": {"S": {"$ref": "#/properties/G/$defs/Inner Def"}},
                }
            },
        }
        clean, name_map = sanitize_tool_schema(schema)
        assert "Inner_Def" in clean["properties"]["G"]["$defs"]
        assert clean["properties"]["G"]["properties"]["S"]["$ref"] == (
            "#/properties/G/$defs/Inner_Def"
        )
        assert name_map.defs_renamed == {"Inner_Def": "Inner Def"}
        assert name_map.is_empty() is False
        assert self._resolves(clean, {"G": {"S": 5}}) == ["5 is not of type 'string'"]

    def test_rfc6901_escapes_and_percent_encoding_round_trip(self):
        schema = {
            "type": "object",
            "$defs": {"a/b~c": {"type": "string"}, "A%20B": {"type": "integer"}},
            "properties": {
                "X": {"$ref": "#/$defs/a~1b~0c"},
                "Y": {"$ref": "#/$defs/a%7E1b%7E0c"},
                "Z": {"$ref": "#/$defs/A%20B"},  # literal '%' in the NAME, not encoding
            },
        }
        clean, _ = sanitize_tool_schema(schema)
        assert set(clean["$defs"]) == {"a_b_c", "A_20B"}
        assert clean["properties"]["X"]["$ref"] == "#/$defs/a_b_c"
        assert clean["properties"]["Y"]["$ref"] == "#/$defs/a_b_c"
        assert clean["properties"]["Z"]["$ref"] == "#/$defs/A_20B"
        # Each wrong value is reported twice — once via the pointer, once via the
        # `type` the annotation pass adds beside it — so compare the SET.
        assert set(self._resolves(clean, {"X": 1, "Y": 2, "Z": "s"})) == {
            "'s' is not of type 'integer'",
            "1 is not of type 'string'",
            "2 is not of type 'string'",
        }

    def test_type_annotation_happens_after_the_rewrite_for_a_renamed_definition(self):
        """The two passes are order-dependent: annotation looks the definition up by
        its SANITIZED key. A spaced name is the case that exposes a wrong order."""
        schema = {
            "type": "object",
            "$defs": {"Account Holder Address": {"type": "object", "properties": {}}},
            "properties": {"A": {"$ref": "#/$defs/Account Holder Address"}},
        }
        clean, _ = sanitize_tool_schema(schema)
        assert clean["properties"]["A"] == {
            "$ref": "#/$defs/Account_Holder_Address",
            "type": "object",
        }

    def test_list_typed_definition_is_not_annotated_and_existing_type_is_kept(self):
        schema = {
            "type": "object",
            "$defs": {
                "Maybe": {"type": ["object", "null"], "properties": {}},
                "Obj": {"type": "object", "properties": {}},
            },
            "properties": {
                "M": {"$ref": "#/$defs/Maybe"},
                "O": {"$ref": "#/$defs/Obj", "type": "string"},  # contradictory, kept
            },
        }
        clean, _ = sanitize_tool_schema(schema)
        assert "type" not in clean["properties"]["M"]
        assert clean["properties"]["O"]["type"] == "string"

    def test_a_definition_named_like_a_metadata_keyword_is_kept(self):
        """``id`` is a plausible group name. It used to be deleted from ``$defs`` by
        the metadata strip while its ``$ref`` stayed — a pointer to nothing."""
        schema = {
            "type": "object",
            "$defs": {
                "id": {"type": "object", "properties": {"n": {"type": "string"}}}
            },
            "properties": {"I": {"$ref": "#/$defs/id"}},
        }
        clean, _ = sanitize_tool_schema(schema)
        assert "id" in clean["$defs"]
        assert self._resolves(clean, {"I": {"n": 1}}) == ["1 is not of type 'string'"]

    def test_every_shipped_class_still_resolves_after_sanitizing(self):
        import jsonschema

        for _preset, _label, schema in _shipped_class_schemas():
            clean, _ = sanitize_tool_schema(schema)
            jsonschema.Draft202012Validator.check_schema(clean)
            # Force pointer resolution on every $ref by validating an empty object.
            list(jsonschema.Draft202012Validator(clean).iter_errors({}))


class TestRestoreNamesParsesSerializedGroups:
    def test_a_group_returned_as_a_json_string_is_parsed_and_its_inner_names_restored(
        self,
    ):
        """The #783 payload, end to end: the model serialized the group and used the
        SANITIZED inner names. Leaving it as text for coercion to parse later would
        put wire spellings into inference_result."""
        schema = {
            "type": "object",
            "$defs": {
                "Account Holder Address": {
                    "type": "object",
                    "properties": {
                        "Street Number": {"type": "string"},
                        "Street Name": {"type": "string"},
                        "ZIP Code": {"type": "string"},
                    },
                    "required": ["Street Name"],
                }
            },
            "properties": {
                "Account Number": {"type": "string"},
                "Account Holder Address": {"$ref": "#/$defs/Account Holder Address"},
            },
        }
        clean, name_map = sanitize_tool_schema(schema)
        wire = {
            "Account_Number": "123",
            "Account_Holder_Address": (
                '{"Street_Number": "100", "Street_Name": "Main Street", "ZIP_Code": "90210"}'
            ),
        }
        restored = restore_names(wire, name_map)
        assert restored == {
            "Account Number": "123",
            "Account Holder Address": {
                "Street Number": "100",
                "Street Name": "Main Street",
                "ZIP Code": "90210",
            },
        }

    def test_a_string_that_is_not_json_is_left_as_is(self):
        schema = {
            "type": "object",
            "$defs": {"G": {"type": "object", "properties": {"A B": {}}}},
            "properties": {"X": {"$ref": "#/$defs/G"}, "Note Text": {"type": "string"}},
        }
        _, name_map = sanitize_tool_schema(schema)
        restored = restore_names({"X": "just text", "Note_Text": "{not json"}, name_map)
        assert restored == {"X": "just text", "Note Text": "{not json"}

    def test_a_lossy_json_string_is_not_parsed(self):
        schema = {
            "type": "object",
            "$defs": {"G": {"type": "object", "properties": {"A B": {}}}},
            "properties": {"X": {"$ref": "#/$defs/G"}},
        }
        _, name_map = sanitize_tool_schema(schema)
        for raw in ('{"A_B": NaN}', '{"A_B": 1, "A_B": 2}', '{"A_B": 1e400}'):
            assert restore_names({"X": raw}, name_map) == {"X": raw}
