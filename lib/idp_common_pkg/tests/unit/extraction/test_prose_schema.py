# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The three prose-schema renderings, and the claim that justifies dropping one (#710).

``{ATTRIBUTE_NAMES_AND_DESCRIPTIONS}`` puts the whole class schema into the task
prompt. When the same schema is also on the wire as a tool input schema, that prose
is a second copy — but "second copy" is a claim about information, not about bytes,
and the two extraction paths do NOT make it equally:

* **Simple.** The toolSpec is built from the class schema directly and
  ``sanitize_tool_schema`` removes only non-constraining document metadata, so the
  tool carries every description including the class's own. Dropping the prose is a
  clean deletion. Asserted in :class:`TestTheSimplePathToolSchemaIsLossless`.
* **Advanced.** The toolSpec is derived from a generated Pydantic model, and that
  round trip has nowhere to put a description belonging to an OBJECT — so the root
  class description and every nested-group description are lost. Dropping the prose
  there removes information the model has nowhere else, which is why the ``minimal``
  rendering exists. Asserted in :class:`TestTheAdvancedPathLosesObjectDescriptions`.

If either of those two classes ever fails, the corresponding knob's rationale has
changed and its default should be revisited — they are the evidence, not decoration.
"""

from __future__ import annotations

import json
import pathlib

import pytest
import yaml

from idp_common.config.models import IDPConfig
from idp_common.extraction.prose_schema import (
    DEFAULT_PROSE_SCHEMA_MODE,
    PROSE_SCHEMA_MODES,
    describe_saving,
    normalize_prose_schema_mode,
    render,
)

pytestmark = pytest.mark.unit

_LENDING = (
    pathlib.Path(__file__).resolve().parents[5]
    / "config_library"
    / "unified"
    / "lending-package-sample"
    / "config.yaml"
)

_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "invoice",
    "type": "object",
    "description": "An invoice issued by a supplier.",
    "properties": {
        "invoice_number": {"type": "string", "description": "The invoice number"},
        "issued": {"type": "string", "format": "date", "description": "Issue date"},
        "total": {"type": "number", "description": "Grand total"},
        "supplier": {
            "type": "object",
            "description": "Who issued the invoice.",
            "properties": {"name": {"type": "string", "description": "Legal name"}},
        },
        "lines": {
            "type": "array",
            "description": "One entry per billed line.",
            "items": {
                "type": "object",
                "description": "A single billed line.",
                "properties": {"sku": {"type": "string", "description": "SKU"}},
            },
        },
    },
}


def _lending_class(name):
    if not _LENDING.exists():  # pragma: no cover - packaged install
        pytest.skip(f"{_LENDING} not present")
    cfg = yaml.safe_load(_LENDING.read_text())
    return next(c for c in cfg["classes"] if (c.get("$id") or "") == name)


class TestFullIsUnchanged:
    """`full` must reproduce pre-#710 behaviour byte for byte.

    Every shipped configuration renders `full`, so a change here silently rewrites
    the prompt (and invalidates the prompt-cache prefix) of every deployment.
    """

    def test_full_is_the_indented_json_schema(self):
        assert render(_SCHEMA, "full") == json.dumps(_SCHEMA, indent=2)

    def test_full_is_the_default_mode(self):
        assert DEFAULT_PROSE_SCHEMA_MODE == "full"
        assert render(_SCHEMA, DEFAULT_PROSE_SCHEMA_MODE) == render(_SCHEMA, "full")


class TestNames:
    def test_it_lists_every_top_level_attribute(self):
        out = render(_SCHEMA, "names")
        for name in _SCHEMA["properties"]:
            assert name in out

    def test_it_carries_no_field_descriptions(self):
        out = render(_SCHEMA, "names")
        assert "The invoice number" not in out
        assert "Grand total" not in out

    def test_it_is_not_empty(self):
        """The placeholder sits inside `<attributes> … </attributes>` in prompt text
        whose next sentence refers to "a list of attribute names". An empty
        substitution would leave dangling prose and make that sentence false."""
        assert render(_SCHEMA, "names").strip()

    def test_it_is_much_smaller_than_full(self):
        assert len(render(_SCHEMA, "names")) < len(render(_SCHEMA, "full")) / 3

    def test_it_says_where_the_types_and_descriptions_are(self):
        """`names` is only ever rendered when a tool schema IS on the wire, so the
        prompt should point the model at it rather than leave the omission
        unexplained."""
        assert "tool schema" in render(_SCHEMA, "names")


class TestMinimal:
    def test_it_keeps_the_class_description(self):
        assert "An invoice issued by a supplier." in render(_SCHEMA, "minimal")

    def test_it_keeps_nested_object_descriptions(self):
        out = render(_SCHEMA, "minimal")
        assert "Who issued the invoice." in out
        assert "A single billed line." in out

    def test_it_drops_per_field_descriptions(self):
        """The whole point: per-FIELD descriptions survive the Pydantic round trip
        that builds the tool schema, so restating them is the redundant part."""
        out = render(_SCHEMA, "minimal")
        assert "The invoice number" not in out
        assert "Grand total" not in out
        assert "Legal name" not in out

    def test_it_still_lists_the_attribute_names(self):
        out = render(_SCHEMA, "minimal")
        for name in _SCHEMA["properties"]:
            assert name in out

    def test_it_sits_between_names_and_full(self):
        sizes = {m: len(render(_SCHEMA, m)) for m in PROSE_SCHEMA_MODES}
        assert sizes["names"] < sizes["minimal"] < sizes["full"]

    def test_a_shared_def_is_named_once_per_property_that_uses_it(self):
        """`EmployeeAddress` and `CompanyAddress` both $ref one `Address` def in the
        shipped lending config. Naming the def once would leave the reader unable to
        tell which property the sentence is about, so both are named."""
        out = render(_lending_class("Payslip"), "minimal")
        assert "EmployeeAddress" in out
        assert "CompanyAddress" in out

    def test_a_self_referential_def_terminates(self):
        """A recursive schema must not spin the walker."""
        recursive = {
            "type": "object",
            "description": "A node.",
            "$defs": {
                "Node": {
                    "type": "object",
                    "description": "A child node.",
                    "properties": {"child": {"$ref": "#/$defs/Node"}},
                }
            },
            "properties": {"root": {"$ref": "#/$defs/Node"}},
        }
        out = render(recursive, "minimal")
        assert "A node." in out
        assert "A child node." in out


class TestDeterminism:
    @pytest.mark.parametrize("mode", PROSE_SCHEMA_MODES)
    def test_the_same_schema_renders_identically(self, mode):
        """The rendering precedes `<<CACHEPOINT>>`, so anything that varied run to
        run would invalidate the cached prompt prefix on every single request."""
        assert render(_SCHEMA, mode) == render(json.loads(json.dumps(_SCHEMA)), mode)


class TestModeNormalization:
    @pytest.mark.parametrize("blank", [None, "", "   "])
    def test_blank_resolves_to_full(self, blank):
        """The config editor has persisted nulls for scalar fields before. The safe
        resolution of "unknown" is to keep sending the whole schema."""
        assert normalize_prose_schema_mode(blank) == "full"

    @pytest.mark.parametrize("value", ["FULL", " Minimal ", "NAMES"])
    def test_case_and_whitespace_are_tolerated(self, value):
        assert normalize_prose_schema_mode(value) in PROSE_SCHEMA_MODES

    def test_an_unknown_mode_is_rejected_loudly(self):
        """Silently falling back would degrade a prompt without saying so."""
        with pytest.raises(ValueError, match="prose_schema"):
            normalize_prose_schema_mode("none")


class TestEdgeCases:
    def test_a_class_with_no_properties_renders_empty(self):
        """Handled by the caller (an attribute-less class skips LLM extraction
        entirely), asserted here so a future change cannot start emitting a bare
        header with nothing under it."""
        for mode in ("minimal", "names"):
            assert render({"type": "object", "properties": {}}, mode) == ""

    def test_a_class_with_no_descriptions_at_all_still_lists_names(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        assert "a" in render(schema, "minimal")

    def test_describe_saving_never_reports_a_negative(self):
        assert describe_saving("short", "much much longer").startswith("0 chars")


class TestTheSimplePathToolSchemaIsLossless:
    """The evidence for `forced_tool.drop_prose_schema`.

    The Simple-path toolSpec is the class schema with `sanitize_tool_schema` applied,
    and that function removes only document metadata ($id/$schema/$anchor/$comment/id)
    and `x-aws-idp-*` extensions. So the prose copy carries NOTHING the tool does not
    — including the class's own description, which the Advanced path loses.
    """

    @pytest.mark.parametrize(
        "cls", ["Payslip", "W2", "Bank-Statement", "Homeowners-Insurance-Application"]
    )
    def test_every_description_survives_the_tool_schema(self, cls):
        from idp_common.bedrock.tool_schema import sanitize_tool_schema

        schema = _lending_class(cls)
        tool, _name_map = sanitize_tool_schema(schema)
        rendered = json.dumps(tool)
        for text in _descriptions(schema):
            assert text in rendered, f"{cls}: tool schema lost {text[:60]!r}"

    def test_the_class_description_survives(self):
        """Named separately because it is the ONE description the Advanced path
        drops on every class, so the two paths differ exactly here."""
        from idp_common.bedrock.tool_schema import sanitize_tool_schema

        schema = _lending_class("W2")
        tool, _ = sanitize_tool_schema(schema)
        assert tool.get("description") == schema["description"]


class TestTheAdvancedPathLosesObjectDescriptions:
    """The evidence for `agentic.prose_schema: minimal` existing at all.

    Strands derives the extraction tool's inputSchema from a generated Pydantic
    model. `Field(description=...)` carries a scalar field's description, but a
    description that belongs to an OBJECT (the class itself, or a nested group) has
    nowhere to go — so it is absent from `model_json_schema()` and never reaches the
    model. That is why `names` is not the only alternative to `full`.
    """

    @pytest.mark.parametrize("cls", ["Payslip", "W2", "Bank-Statement"])
    def test_the_class_description_is_lost(self, cls):
        from idp_common.schema import create_pydantic_model_from_json_schema

        schema = _lending_class(cls)
        generated = create_pydantic_model_from_json_schema(
            schema=schema, class_label=cls, clean_schema=False
        ).model_json_schema()
        assert schema["description"] not in json.dumps(generated), (
            f"{cls}: the class description now survives the Pydantic round trip — "
            "prose_schema: 'minimal' may no longer be needed, re-measure before "
            "changing its default"
        )

    def test_nested_group_descriptions_are_lost(self):
        from idp_common.schema import create_pydantic_model_from_json_schema

        schema = _lending_class("Payslip")
        generated = json.dumps(
            create_pydantic_model_from_json_schema(
                schema=schema, class_label="Payslip", clean_schema=False
            ).model_json_schema()
        )
        lost = [t for t in _descriptions(schema) if t not in generated]
        # The root plus the two shared $defs (Address, EmployeeName).
        assert len(lost) == 3, f"expected 3 lost descriptions, got {lost}"

    def test_minimal_restates_exactly_what_was_lost(self):
        """`minimal` is defined as the compensating preamble, so it must actually
        cover the gap it was built for."""
        from idp_common.schema import create_pydantic_model_from_json_schema

        schema = _lending_class("Payslip")
        generated = json.dumps(
            create_pydantic_model_from_json_schema(
                schema=schema, class_label="Payslip", clean_schema=False
            ).model_json_schema()
        )
        minimal = render(schema, "minimal")
        for text in _descriptions(schema):
            if text not in generated:
                assert text in minimal, f"minimal does not restate {text[:60]!r}"

    def test_the_saving_is_material_on_a_real_class(self):
        """Guards against the knob being pointless on real schemas."""
        schema = _lending_class("Payslip")
        full = len(render(schema, "full")) // 4
        assert full > 1_000, f"Payslip prose is only {full} tokens"
        assert len(render(schema, "minimal")) // 4 < full / 4
        assert len(render(schema, "names")) // 4 < full / 5


class TestConfigWiring:
    def test_the_agentic_field_defaults_to_full(self):
        assert IDPConfig().extraction.agentic.prose_schema == "full"

    def test_the_forced_tool_field_defaults_to_off(self):
        assert IDPConfig().extraction.forced_tool.drop_prose_schema is False

    @pytest.mark.parametrize("mode", PROSE_SCHEMA_MODES)
    def test_each_mode_validates(self, mode):
        cfg = IDPConfig(**{"extraction": {"agentic": {"prose_schema": mode}}})
        assert cfg.extraction.agentic.prose_schema == mode

    def test_a_bad_mode_fails_config_validation(self):
        with pytest.raises(Exception, match="prose_schema"):
            IDPConfig(**{"extraction": {"agentic": {"prose_schema": "verbose"}}})

    def test_a_blank_mode_does_not_break_the_runtime(self):
        cfg = IDPConfig(**{"extraction": {"agentic": {"prose_schema": ""}}})
        assert cfg.extraction.agentic.prose_schema == "full"

    def test_both_keys_are_in_the_shipped_system_defaults(self):
        """A key that exists only as a Pydantic default is invisible in
        `Config#default`, so an operator cannot discover it in the config editor."""
        path = (
            pathlib.Path(__file__).resolve().parents[3]
            / "idp_common"
            / "config"
            / "system_defaults"
            / "base-extraction.yaml"
        )
        defaults = yaml.safe_load(path.read_text())["extraction"]
        assert defaults["agentic"]["prose_schema"] == "full"
        assert defaults["forced_tool"]["drop_prose_schema"] is False


def _descriptions(schema):
    """Every ``description`` string anywhere in a schema, de-duplicated."""
    out = []
    stack = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            text = node.get("description")
            if isinstance(text, str) and text.strip():
                out.append(text)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return sorted(set(out))
