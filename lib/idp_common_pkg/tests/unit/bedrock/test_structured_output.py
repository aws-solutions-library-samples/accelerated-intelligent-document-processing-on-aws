# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bedrock structured outputs: schema preparation, the capability gate, and the
client wiring (#1785).

The authority for the request shape is botocore's own service model, not a
hand-written expectation: :class:`TestTheRequestBotocoreWouldSend` passes the
real ``converse`` parameters through ``botocore.stub.Stubber``, which validates
them against the installed model exactly as a live call would before it leaves
the process. The authority for the preparation rules is the shipped class schemas
in ``config_library/unified``: :class:`TestEveryShippedClassSchema` runs every one
of them through ``prepare_schema`` and checks the limits on the result.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

import jsonschema
import pytest
import yaml
from botocore.stub import Stubber

from idp_common.bedrock import client as client_mod
from idp_common.bedrock.client import (
    STRUCTURED_OUTPUT_BASE_NAMES,
    BedrockClient,
    structured_output_unsupported_reason,
    supports_structured_output,
)
from idp_common.bedrock.structured_output import (
    MAX_OPTIONAL_PROPERTIES,
    MAX_UNION_PROPERTIES,
    MIN_READ_TIMEOUT_SECONDS,
    converse_output_config,
    invoke_model_output_config,
    prepare_schema,
)
from idp_common.bedrock.tool_schema import sanitize_tool_schema

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[5]
_PRESETS = _REPO / "config_library" / "unified"
_TEMPLATE = _REPO / "patterns" / "unified" / "template.yaml"


def _walk(node):
    """Every dict node in a schema, depth first."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for x in node:
            yield from _walk(x)


def _objects(node):
    return [n for n in _walk(node) if n.get("type") == "object" or "properties" in n]


def _optional(schema):
    return sum(
        1
        for n in _objects(schema)
        for p in (n.get("properties") or {})
        if p not in (n.get("required") or [])
    )


def _unions(schema):
    return sum(
        1
        for n in _walk(schema)
        if (isinstance(n.get("type"), list) and len(n["type"]) > 1) or "anyOf" in n
    )


def _flat(n_props, *, required=()):
    return {
        "type": "object",
        "properties": {
            f"f{i}": {"type": "string", "description": f"field {i}"}
            for i in range(n_props)
        },
        "required": list(required),
    }


# ---------------------------------------------------------------------------
# prepare_schema
# ---------------------------------------------------------------------------


class TestAdditionalPropertiesIsFalseOnEveryObject:
    def test_nested_objects_items_and_defs_all_get_it(self):
        schema = {
            "type": "object",
            "properties": {
                "a": {"type": "object", "properties": {"b": {"type": "string"}}},
                "rows": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/Row"},
                },
            },
            "$defs": {
                "Row": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {"x": {"type": "number"}},
                }
            },
        }
        out = prepare_schema(schema)
        assert out.fits
        objs = _objects(out.schema)
        assert len(objs) == 3
        assert all(o["additionalProperties"] is False for o in objs)

    def test_input_is_not_mutated(self):
        schema = _flat(20)
        before = copy.deepcopy(schema)
        prepare_schema(schema)
        assert schema == before


class TestUnsupportedKeywords:
    @pytest.mark.parametrize(
        "keyword,value",
        [
            ("minimum", 0),
            ("maximum", 10),
            ("exclusiveMinimum", 0),
            ("multipleOf", 2),
            ("minLength", 1),
            ("maxLength", 5),
            ("maxItems", 3),
            ("uniqueItems", True),
            ("data_type", "string"),
            ("examples", ["x"]),
        ],
    )
    def test_removed_and_counted(self, keyword, value):
        schema = {
            "type": "object",
            "properties": {"v": {"type": "string", keyword: value}},
            "required": ["v"],
        }
        out = prepare_schema(schema)
        assert out.fits
        assert keyword not in out.schema["properties"]["v"]
        assert out.relaxed_keywords == {keyword: 1}

    def test_min_items_above_one_is_clamped_not_dropped(self):
        schema = {
            "type": "object",
            "properties": {
                "rows": {"type": "array", "minItems": 100, "items": {"type": "string"}},
                "one": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            },
            "required": ["rows", "one"],
        }
        out = prepare_schema(schema)
        assert out.schema["properties"]["rows"]["minItems"] == 1
        assert out.schema["properties"]["one"]["minItems"] == 1
        assert out.relaxed_keywords == {"minItems": 1}

    def test_unsupported_format_and_lookaround_pattern_removed(self):
        schema = {
            "type": "object",
            "properties": {
                "d": {"type": "string", "format": "date"},
                "c": {"type": "string", "format": "currency"},
                "ok": {"type": "string", "pattern": "^[A-Z]{3}$"},
                "bad": {"type": "string", "pattern": "^(?!x).*$"},
                "backref": {"type": "string", "pattern": "(a)\\1"},
            },
            "required": ["d", "c", "ok", "bad", "backref"],
        }
        p = prepare_schema(schema).schema["properties"]
        assert p["d"]["format"] == "date"
        assert "format" not in p["c"]
        assert p["ok"]["pattern"] == "^[A-Z]{3}$"
        assert "pattern" not in p["bad"]
        assert "pattern" not in p["backref"]

    def test_one_of_becomes_any_of(self):
        schema = {
            "type": "object",
            "properties": {
                "v": {"oneOf": [{"type": "string"}, {"type": "number"}]},
            },
            "required": ["v"],
        }
        out = prepare_schema(schema)
        assert out.schema["properties"]["v"] == {
            "anyOf": [{"type": "string"}, {"type": "number"}]
        }
        assert out.relaxed_keywords == {"oneOf": 1}


class TestDoesNotFit:
    def test_recursive_schema(self):
        schema = {
            "type": "object",
            "properties": {"node": {"$ref": "#/$defs/Node"}},
            "$defs": {
                "Node": {
                    "type": "object",
                    "properties": {"child": {"$ref": "#/$defs/Node"}},
                }
            },
        }
        out = prepare_schema(schema)
        assert not out.fits and out.schema is None
        assert out.reason is not None and "recursive" in out.reason

    def test_external_ref(self):
        schema = {
            "type": "object",
            "properties": {"x": {"$ref": "https://example.com/s.json"}},
        }
        out = prepare_schema(schema)
        assert not out.fits and out.reason is not None
        assert "external" in out.reason

    def test_more_optional_properties_than_both_limits_hold(self):
        n = MAX_OPTIONAL_PROPERTIES + MAX_UNION_PROPERTIES + 1
        out = prepare_schema(_flat(n))
        assert not out.fits and out.schema is None
        assert out.reason is not None
        assert "at most 12 optional and 16 union-typed" in out.reason


class TestTheOptionalAndUnionLimits:
    """Nullable is spent first, then optional.

    Measured live (Claude Haiku 5.5, us-east-1, 2026-11-11): with structured
    outputs a required nullable string the document does not contain came back
    ``null``, while an optional string came back ``""``. So optional properties are
    made required and nullable while the 16-property union allowance lasts, and
    only the overflow stays optional, up to 12.
    """

    def test_a_small_schema_is_made_entirely_nullable(self):
        out = prepare_schema(_flat(5))
        assert out.fits and out.schema is not None
        assert out.made_nullable == 5
        assert _optional(out.schema) == 0
        assert sorted(out.schema["required"]) == sorted(f"f{i}" for i in range(5))
        assert all(
            p["type"] == ["string", "null"] for p in out.schema["properties"].values()
        )

    def test_the_union_allowance_is_spent_before_any_property_stays_optional(self):
        n = MAX_UNION_PROPERTIES + 5
        out = prepare_schema(_flat(n))
        assert out.fits and out.schema is not None
        assert out.made_nullable == MAX_UNION_PROPERTIES
        assert _unions(out.schema) == MAX_UNION_PROPERTIES
        assert _optional(out.schema) == 5
        # Schema order: the first sixteen are the nullable ones.
        assert out.schema["required"] == [f"f{i}" for i in range(16)]

    def test_exactly_at_both_limits_fits(self):
        out = prepare_schema(_flat(MAX_OPTIONAL_PROPERTIES + MAX_UNION_PROPERTIES))
        assert out.fits and out.schema is not None
        assert _optional(out.schema) == MAX_OPTIONAL_PROPERTIES
        assert _unions(out.schema) == MAX_UNION_PROPERTIES

    def test_one_more_than_both_limits_does_not_fit(self):
        out = prepare_schema(_flat(MAX_OPTIONAL_PROPERTIES + MAX_UNION_PROPERTIES + 1))
        assert not out.fits and out.schema is None

    def test_an_already_nullable_optional_is_required_for_free(self):
        schema = _flat(3)
        schema["properties"]["extra"] = {"type": ["string", "null"]}
        out = prepare_schema(schema)
        assert out.fits and out.schema is not None
        assert out.made_required == 1 and out.made_nullable == 3
        assert "extra" in out.schema["required"]
        assert _unions(out.schema) == 4

    def test_existing_unions_count_against_the_allowance(self):
        schema = _flat(MAX_OPTIONAL_PROPERTIES)
        for i in range(MAX_UNION_PROPERTIES):
            schema["properties"][f"u{i}"] = {"type": ["number", "null"]}
            schema["required"].append(f"u{i}")
        out = prepare_schema(schema)
        assert out.fits and out.schema is not None
        assert out.made_nullable == 0
        assert _optional(out.schema) == MAX_OPTIONAL_PROPERTIES

    def test_optional_properties_are_counted_across_nested_objects(self):
        schema = {
            "type": "object",
            "properties": {
                f"g{i}": _flat(7) | {"description": "group"} for i in range(4)
            },
            "required": [f"g{i}" for i in range(4)],
        }
        # 4 groups x 7 optional leaves = 28, though no object holds more than 7.
        out = prepare_schema(schema)
        assert out.fits and out.schema is not None
        assert out.made_nullable == MAX_UNION_PROPERTIES
        assert _optional(out.schema) == MAX_OPTIONAL_PROPERTIES

    def test_a_shared_definition_is_counted_once_per_use(self):
        """Inlined, so the count describes the tree that is sent.

        Measured live, Bedrock is more lenient than this: 14 optional properties
        reached through one definition used twice were accepted (Claude Haiku 5.5,
        us-east-1, 2026-11-11). The user guide counts "across all nested objects",
        so the documented rule is the one relied on.
        """
        schema = {
            "type": "object",
            "properties": {
                "home": {"$ref": "#/$defs/Address"},
                "work": {"$ref": "#/$defs/Address"},
            },
            "required": ["home", "work"],
            "$defs": {"Address": _flat(14)},
        }
        out = prepare_schema(schema)
        assert out.fits and out.schema is not None
        assert "$defs" not in out.schema
        assert out.made_nullable == MAX_UNION_PROPERTIES
        assert _optional(out.schema) == 28 - MAX_UNION_PROPERTIES

    def test_a_nullable_enum_is_wrapped_not_given_a_type_array(self):
        """Bedrock rejects ``{"type": ["string", "null"], "enum": [...]}``
        ("Enum value 'A' does not match declared type"), measured live."""
        schema = {
            "type": "object",
            "properties": {"status": {"type": "string", "enum": ["A", "B"]}},
        }
        out = prepare_schema(schema)
        assert out.schema is not None
        status = out.schema["properties"]["status"]
        assert status == {
            "anyOf": [{"type": "string", "enum": ["A", "B"]}, {"type": "null"}]
        }
        jsonschema.validate({"status": None}, out.schema)
        jsonschema.validate({"status": "A"}, out.schema)

    def test_an_authored_type_array_enum_keeps_only_the_enum(self):
        schema = {
            "type": "object",
            "properties": {
                "s": {"type": ["string", "null"], "enum": ["A", None, 3]},
                "n": {"type": ["number", "null"], "enum": [1, 2.5, "x"]},
            },
            "required": ["s", "n"],
        }
        out = prepare_schema(schema)
        assert out.schema is not None
        assert out.schema["properties"]["s"] == {"enum": ["A", None]}
        assert out.schema["properties"]["n"] == {"enum": [1, 2.5]}

    def test_nullable_object_is_wrapped_and_keeps_its_own_limits(self):
        schema = {
            "type": "object",
            "properties": {
                "addr": {
                    "type": "object",
                    "description": "An address",
                    "properties": {"street": {"type": "string"}},
                    "required": ["street"],
                },
            },
        }
        out = prepare_schema(schema)
        assert out.schema is not None
        addr = out.schema["properties"]["addr"]
        assert addr["description"] == "An address"
        inner, null = addr["anyOf"]
        assert null == {"type": "null"}
        assert inner["additionalProperties"] is False
        assert "addr" in out.schema["required"]
        jsonschema.validate({"addr": None}, out.schema)

    def test_deterministic(self):
        schema = _flat(MAX_OPTIONAL_PROPERTIES + 9)
        a = converse_output_config(prepare_schema(schema).schema or {}, "n")
        b = converse_output_config(
            prepare_schema(copy.deepcopy(schema)).schema or {}, "n"
        )
        assert a == b


class TestAFullyPopulatedInstanceStillValidates:
    """Preparation may relax and widen, never tighten past the original.

    A document that fills every field validly against the authored schema must
    also be valid against the prepared one, or constrained decoding would forbid
    a correct answer.
    """

    def test_bank_statement_shape(self):
        schema = {
            "type": "object",
            "properties": {
                "account": {"type": "string", "pattern": "^[0-9]+$"},
                "balance": {"type": "number", "minimum": 0},
                "txns": {
                    "type": "array",
                    "minItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string", "format": "date"},
                            "amount": {"type": "number"},
                        },
                    },
                },
                **_flat(14)["properties"],
            },
        }
        instance = {
            "account": "123",
            "balance": 5.0,
            "txns": [{"date": "2026-01-0%d" % i, "amount": i} for i in range(1, 4)],
            **{f"f{i}": "v" for i in range(14)},
        }
        jsonschema.validate(instance, schema)
        jsonschema.validate(instance, prepare_schema(schema).schema)


class TestEveryShippedClassSchema:
    @staticmethod
    def _classes():
        out = []
        for cfg in sorted(_PRESETS.glob("*/config.yaml")):
            for cls in yaml.safe_load(cfg.read_text()).get("classes") or []:
                out.append((f"{cfg.parent.name}:{cls.get('$id')}", cls))
        return out

    def test_the_authority_is_not_empty(self):
        assert len(self._classes()) > 0

    def test_every_class_either_fits_within_the_limits_or_says_why(self):
        fits = 0
        for label, cls in self._classes():
            out = prepare_schema(sanitize_tool_schema(cls)[0])
            if not out.fits:
                assert out.reason, label
                assert out.schema is None, label
                continue
            fits += 1
            assert _optional(out.schema) <= MAX_OPTIONAL_PROPERTIES, label
            assert _unions(out.schema) <= MAX_UNION_PROPERTIES, label
            for obj in _objects(out.schema):
                assert obj.get("additionalProperties") is False, label
            for node in _walk(out.schema):
                assert not ({"$ref", "$defs", "$id", "$schema"} & set(node)), label
            # It must still be a valid JSON Schema itself.
            jsonschema.Draft202012Validator.check_schema(out.schema)
        # Most shipped classes fit; if that ever stops being true the feature has
        # lost its main use and the change that caused it deserves a look.
        assert fits > len(self._classes()) // 2


# ---------------------------------------------------------------------------
# Carriers
# ---------------------------------------------------------------------------


class TestCarriers:
    def test_converse_carries_the_schema_as_a_json_string(self):
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        cfg = converse_output_config(schema, "extracted_fields", "desc")
        js = cfg["textFormat"]["structure"]["jsonSchema"]
        assert cfg["textFormat"]["type"] == "json_schema"
        assert isinstance(js["schema"], str)
        assert json.loads(js["schema"]) == schema
        assert js["name"] == "extracted_fields"
        assert js["description"] == "desc"

    def test_property_order_is_kept_as_authored(self):
        """Constrained decoding emits properties in schema order, so sorting them
        changes what the model writes (measured on RealKIE FCC invoices)."""
        schema = {
            "type": "object",
            "properties": {"zeta": {"type": "string"}, "alpha": {"type": "string"}},
            "required": ["zeta", "alpha"],
            "additionalProperties": False,
        }
        js = converse_output_config(schema, "n")["textFormat"]["structure"]
        text = js["jsonSchema"]["schema"]
        assert text.index('"zeta"') < text.index('"alpha"')
        assert list(json.loads(text)["properties"]) == ["zeta", "alpha"]

    def test_invoke_model_carries_the_schema_as_an_object(self):
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        cfg = invoke_model_output_config(schema)
        assert cfg == {"format": {"type": "json_schema", "schema": schema}}
        assert cfg["format"]["schema"] is not schema


# ---------------------------------------------------------------------------
# Capability gate
# ---------------------------------------------------------------------------


def _offered_claude_ids():
    import re

    text = _TEMPLATE.read_text()
    return sorted(
        set(re.findall(r'"((?:[a-z-]+\.)?anthropic\.claude-[A-Za-z0-9.:-]+)"', text))
    )


class TestCapabilityGate:
    def test_every_offered_claude_model_is_supported(self):
        """Every Claude model the template offers lists structured outputs on its
        Bedrock model card, so every one must pass the gate — a new Claude model
        added to the picklist without a decision here fails this."""
        ids = _offered_claude_ids()
        assert ids
        missing = [m for m in ids if not supports_structured_output(m)]
        assert missing == []

    @pytest.mark.parametrize(
        "model_id",
        [
            "us.amazon.nova-pro-v1:0",
            "us.amazon.nova-2-lite-v1:0",
            "us.xai.grok-4.6",
            "us.openai.gpt-6-astra",
            "openai.gpt-5.5",
            "LambdaHook",
            "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        ],
    )
    def test_other_models_are_refused_with_a_reason(self, model_id):
        assert not supports_structured_output(model_id)
        assert structured_output_unsupported_reason(model_id)

    def test_region_prefixes_and_inference_profile_arns_resolve(self):
        for mid in (
            "global.anthropic.claude-sonnet-5-5",
            "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
            "us.anthropic.claude-opus-5:1m",
            "arn:aws:bedrock:us-west-2:111122223333:inference-profile/"
            "us.anthropic.claude-sonnet-5",
        ):
            assert supports_structured_output(mid), mid

    def test_an_old_botocore_is_refused_before_any_request(self):
        with patch.object(client_mod, "_OUTPUT_CONFIG_IN_BOTOCORE", False):
            reason = structured_output_unsupported_reason(
                "us.anthropic.claude-sonnet-5"
            )
        assert reason and "botocore" in reason

    def test_the_botocore_probe_reads_the_installed_service_model(self):
        with patch.object(client_mod, "_OUTPUT_CONFIG_IN_BOTOCORE", None):
            assert client_mod._botocore_supports_output_config() is True

    def test_the_config_ui_lists_exactly_the_supported_models(self):
        """The Web UI's setting description names the models it works with, so a
        user can tell from the editor whether their extraction model qualifies.
        Derived from the gate's own allowlist in both directions: a model added to
        the gate but not the description, or named in the description but not
        allowed by the gate, fails here."""
        import re

        text = _TEMPLATE.read_text()
        start = text.index("sectionLabel: Structured Outputs (experimental)")
        block = text[start : text.index("multi_instance_detection:", start)]
        assert block

        def human(base: str) -> str:
            family, *ver = base.removeprefix("anthropic.claude-").split("-")
            version = ".".join(v for v in ver if len(v) <= 2)
            return f"{family.capitalize()} {version}"

        expected = {human(b) for b in STRUCTURED_OUTPUT_BASE_NAMES}
        assert len(expected) == len(STRUCTURED_OUTPUT_BASE_NAMES)
        for name in expected:
            assert re.search(rf"\b{re.escape(name)}(?![.\d])", block), name
        named = {
            f"{f} {v}"
            for f, v in re.findall(
                r"\b(Haiku|Sonnet|Opus|Fable)\s+(\d+(?:\.\d+)?)", block
            )
        }
        assert named == expected

    def test_base_names_are_anthropic(self):
        assert STRUCTURED_OUTPUT_BASE_NAMES
        assert all(
            n.startswith("anthropic.claude-") for n in STRUCTURED_OUTPUT_BASE_NAMES
        )


def test_the_client_read_timeout_covers_first_request_compilation():
    """The first request for a new schema can spend ~90 s compiling a grammar;
    the user guide asks for a read timeout of at least 120 s."""
    client = BedrockClient(region="us-west-2")
    with patch.object(client_mod, "get_bedrock_session") as sess:
        client.client  # noqa: B018 - property access builds the client
    config = sess.return_value.client.call_args.kwargs["config"]
    assert config.read_timeout >= MIN_READ_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Client wiring
# ---------------------------------------------------------------------------

_SCHEMA = prepare_schema(_flat(3, required=["f0", "f1", "f2"])).schema
_OUTPUT_CONFIG = converse_output_config(_SCHEMA, "extracted_fields")


def _converse_reply(text='{"f0":"a","f1":"b","f2":"c"}'):
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        "metrics": {"latencyMs": 1},
    }


def _invoke(client, **kw):
    return client.invoke_model(
        model_id=kw.pop("model_id", "us.anthropic.claude-sonnet-5"),
        system_prompt="s",
        content=[{"text": "doc"}],
        **kw,
    )


class TestTheRequestBotocoreWouldSend:
    """Stubber validates the parameters against the installed service model, so a
    wrong carrier name or a schema passed as an object instead of a string fails
    here exactly as it would before a live request left the process."""

    def _client(self):
        import boto3

        real = boto3.client("bedrock-runtime", region_name="us-west-2")
        c = BedrockClient(region="us-west-2", metrics_enabled=False)
        c._client = real
        return c, real

    def test_output_config_reaches_converse_and_validates(self):
        c, real = self._client()
        with Stubber(real) as stub:
            stub.add_response(
                "converse",
                _converse_reply(),
                expected_params=None,
            )
            with patch.object(real, "converse", wraps=real.converse) as conv:
                _invoke(c, output_config=_OUTPUT_CONFIG)
            stub.assert_no_pending_responses()
        sent = conv.call_args.kwargs
        assert sent["outputConfig"] == _OUTPUT_CONFIG

    def test_absent_by_default(self):
        c, real = self._client()
        with Stubber(real) as stub:
            stub.add_response("converse", _converse_reply(), expected_params=None)
            with patch.object(real, "converse", wraps=real.converse) as conv:
                _invoke(c)
        assert "outputConfig" not in conv.call_args.kwargs

    def test_a_schema_object_instead_of_a_string_is_refused_locally(self):
        c, _ = self._client()
        bad = copy.deepcopy(_OUTPUT_CONFIG)
        bad["textFormat"]["structure"]["jsonSchema"]["schema"] = _SCHEMA
        with pytest.raises(ValueError, match="converse_output_config"):
            _invoke(c, output_config=bad)

    @pytest.mark.parametrize(
        "model_id", ["us.amazon.nova-pro-v1:0", "LambdaHook", "openai.gpt-5.5"]
    )
    def test_unsupported_models_are_refused_before_routing(self, model_id):
        c, real = self._client()
        with patch.object(real, "converse") as conv:
            with pytest.raises(ValueError, match="structured outputs"):
                _invoke(c, model_id=model_id, output_config=_OUTPUT_CONFIG)
        conv.assert_not_called()

    def test_the_module_level_callable_forwards_it(self):
        with patch.object(
            client_mod.default_client, "invoke_model", return_value={}
        ) as inv:
            client_mod.invoke_model(
                model_id="us.anthropic.claude-sonnet-5",
                system_prompt="s",
                content=[],
                output_config=_OUTPUT_CONFIG,
            )
        assert inv.call_args.kwargs["output_config"] == _OUTPUT_CONFIG
