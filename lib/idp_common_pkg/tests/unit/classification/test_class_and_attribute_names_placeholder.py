# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the optional ``{CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}``
classification prompt placeholder (GitHub issue #262).

These tests cover:
    1. Schema walking — flat, nested object, list-of-object, missing schema.
    2. Soft-cap truncation when a class has more attributes than
       ``MAX_ATTRIBUTES_PER_CLASS``.
    3. End-to-end placeholder substitution in custom task prompts (both the
       page-level XML-list variant and the holistic markdown-table variant),
       including the cost-neutrality property: a prompt that does NOT
       reference the placeholder produces output identical to legacy
       behavior.
"""

# ruff: noqa: E402, I001

import json
from textwrap import dedent
from unittest.mock import patch

import pytest

from idp_common.classification.service import ClassificationService


def _make_class_schema(
    type_name: str,
    description: str,
    properties: dict | None = None,
) -> dict:
    """Build a minimal JSON-Schema-style class config entry."""
    schema: dict = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": type_name,
        "x-aws-idp-document-type": type_name,
        "type": "object",
        "description": description,
        "properties": properties or {},
    }
    return schema


def _make_config(classes: list[dict], task_prompt: str | None = None) -> dict:
    """Build a minimal classification config with the given classes."""
    return {
        "classes": classes,
        "classification": {
            "model": "anthropic.claude-3-sonnet-20240229-v1:0",
            "temperature": 0.0,
            "top_k": 5,
            "system_prompt": "You are a document classification assistant.",
            "task_prompt": task_prompt
            or dedent(
                """
                Classify the following document text into one of the available classes:
                {CLASS_NAMES_AND_DESCRIPTIONS}

                Document text:
                {DOCUMENT_TEXT}

                Respond with JSON: {"class": "..."}
                """
            ),
            "classificationMethod": "multimodalPageLevelClassification",
        },
    }


@pytest.fixture
def appraisal_inspection_config() -> dict:
    """Two visually similar real-estate doc types with distinguishing
    schema fields — the canonical motivating example from issue #262."""
    return _make_config(
        [
            _make_class_schema(
                "appraisal_report",
                "Real estate valuation report",
                properties={
                    "property_address": {"type": "string"},
                    "appraised_value": {"type": "number"},
                    "effective_date": {"type": "string"},
                    "appraiser_name": {"type": "string"},
                    "comparable_sales": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "address": {"type": "string"},
                                "sale_price": {"type": "number"},
                            },
                        },
                    },
                },
            ),
            _make_class_schema(
                "inspection_report",
                "Property condition inspection report",
                properties={
                    "property_address": {"type": "string"},
                    "inspection_date": {"type": "string"},
                    "inspector_name": {"type": "string"},
                    "findings": {"type": "array", "items": {"type": "string"}},
                },
            ),
        ]
    )


def _build_service(config: dict) -> ClassificationService:
    with patch("boto3.Session"):
        return ClassificationService(
            region="us-west-2", config=config, backend="bedrock"
        )


# ---------------------------------------------------------------------------
# 1. Schema-walking helper: _get_attribute_names_for_class
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetAttributeNamesForClass:
    def test_simple_flat_schema(self, appraisal_inspection_config):
        service = _build_service(appraisal_inspection_config)
        names = service._get_attribute_names_for_class("inspection_report")
        # flat scalars + scalar array surface as top-level names
        assert names == [
            "property_address",
            "inspection_date",
            "inspector_name",
            "findings",
        ]

    def test_list_of_objects_unwraps_to_dotted_paths(self, appraisal_inspection_config):
        service = _build_service(appraisal_inspection_config)
        names = service._get_attribute_names_for_class("appraisal_report")
        # comparable_sales is an array of objects → its item properties are
        # surfaced via dotted path WITHOUT explicit []-indexing
        assert "comparable_sales.address" in names
        assert "comparable_sales.sale_price" in names
        # parent name itself should NOT appear when items are objects
        assert "comparable_sales" not in names
        # scalar siblings still appear
        assert "property_address" in names
        assert "appraised_value" in names

    def test_nested_object_uses_dotted_path(self):
        config = _make_config(
            [
                _make_class_schema(
                    "loan_application",
                    "Loan application",
                    properties={
                        "borrower": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "address": {
                                    "type": "object",
                                    "properties": {
                                        "street": {"type": "string"},
                                        "zip": {"type": "string"},
                                    },
                                },
                            },
                        },
                        "loan_amount": {"type": "number"},
                    },
                ),
            ]
        )
        service = _build_service(config)
        names = service._get_attribute_names_for_class("loan_application")
        assert names == [
            "borrower.name",
            "borrower.address.street",
            "borrower.address.zip",
            "loan_amount",
        ]

    def test_missing_class_returns_empty_list(self, appraisal_inspection_config):
        service = _build_service(appraisal_inspection_config)
        assert service._get_attribute_names_for_class("does_not_exist") == []

    def test_class_without_properties_returns_empty_list(self):
        config = _make_config([_make_class_schema("invoice", "An invoice document")])
        service = _build_service(config)
        assert service._get_attribute_names_for_class("invoice") == []

    def test_ref_group_children_surface_like_inline_group(self):
        """A ``$ref`` group must walk exactly like an equivalent inline group.

        The UI schema editor emits every group as ``{"$ref": "#/$defs/Name"}``,
        which carries no ``type``. Before dereferencing, such a group fell to
        the scalar branch and was emitted as a bare leaf, silently dropping all
        of its child names while an identical inline group walked correctly
        (GitHub issue #678).
        """
        schema = _make_class_schema(
            "tax_form",
            "A tax form",
            properties={
                "TaxpayerName": {"type": "string"},
                "Signatures": {"$ref": "#/$defs/Signatures"},
                "InlineGroup": {
                    "type": "object",
                    "properties": {"Child": {"type": "string"}},
                },
            },
        )
        schema["$defs"] = {
            "Signatures": {
                "type": "object",
                "properties": {
                    "Signature-of-taxpayer1": {"type": "boolean"},
                    "Signature-of-taxpayer2": {"type": "boolean"},
                },
            },
        }
        service = _build_service(_make_config([schema]))

        assert service._get_attribute_names_for_class("tax_form") == [
            "TaxpayerName",
            "Signatures.Signature-of-taxpayer1",
            "Signatures.Signature-of-taxpayer2",
            "InlineGroup.Child",
        ]

    def test_ref_array_items_and_ref_chains_resolve(self):
        """``items: {$ref}}``, a ``$ref`` array container, and ref chains resolve."""
        schema = _make_class_schema(
            "statement",
            "A bank statement",
            properties={
                # array declared inline, item shape behind a $ref
                "Transactions": {"type": "array", "items": {"$ref": "#/$defs/Txn"}},
                # the whole array property behind a $ref
                "Fees": {"$ref": "#/$defs/FeeList"},
                # $ref -> $ref chain
                "Holder": {"$ref": "#/$defs/HolderAlias"},
            },
        )
        schema["$defs"] = {
            "Txn": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "amount": {"type": "number"},
                },
            },
            "FeeList": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"label": {"type": "string"}},
                },
            },
            "HolderAlias": {"$ref": "#/$defs/Holder"},
            "Holder": {"type": "object", "properties": {"name": {"type": "string"}}},
        }
        service = _build_service(_make_config([schema]))

        assert service._get_attribute_names_for_class("statement") == [
            "Transactions.date",
            "Transactions.amount",
            "Fees.label",
            "Holder.name",
        ]

    def test_recursive_ref_definition_terminates(self):
        """A self-referential ``$defs`` entry must not recurse forever.

        Dereferencing makes recursive definitions reachable for the first time,
        so the walk guards on the ``$ref`` targets already entered on the
        current branch and emits a re-entered property as a leaf instead.
        """
        schema = _make_class_schema(
            "tree",
            "A recursive tree",
            properties={"root": {"$ref": "#/$defs/Node"}},
        )
        schema["$defs"] = {
            "Node": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "child": {"$ref": "#/$defs/Node"},
                    "kids": {"type": "array", "items": {"$ref": "#/$defs/Node"}},
                },
            },
        }
        service = _build_service(_make_config([schema]))

        assert service._get_attribute_names_for_class("tree") == [
            "root.label",
            "root.child",
            "root.kids",
        ]

    def test_tuple_form_items_list_does_not_raise(self):
        """``items`` as a LIST (draft-07 tuple form) must not be read as a dict."""
        schema = _make_class_schema(
            "tuple_items",
            "Schema using tuple-form items",
            properties={
                "pair": {
                    "type": "array",
                    "items": [{"type": "string"}, {"type": "number"}],
                },
                "plain": {"type": "string"},
            },
        )
        service = _build_service(_make_config([schema]))

        # Tuple-form items carry no single item shape → parent name only.
        assert service._get_attribute_names_for_class("tuple_items") == [
            "pair",
            "plain",
        ]

    def test_combinatorial_ref_dag_is_bounded(self, caplog):
        """A non-cyclic ``$defs`` DAG must not expand without bound.

        The ``$ref`` cycle guard is per-BRANCH, so a definition that is not
        recursive at all is still legitimately re-entered on every sibling
        branch. Dereferencing therefore makes a tiny schema expand
        combinatorially — a ~2 KB schema with a 3-way fan-out 12 deep produced
        ~265K names (16 deep: ~14M, 34s) — and the caller's
        ``MAX_ATTRIBUTES_PER_CLASS`` cap cannot help, because it truncates the
        RESULT after the walk has already built the whole list.
        """
        depth, fanout = 12, 3
        defs = {}
        for i in range(depth):
            props = {"leaf": {"type": "string"}}
            if i + 1 < depth:
                for k in range(fanout):
                    props[f"c{k}"] = {"$ref": f"#/$defs/L{i + 1}"}
            defs[f"L{i}"] = {"type": "object", "properties": props}

        schema = _make_class_schema(
            "dag", "Fan-out DAG", properties={"root": {"$ref": "#/$defs/L0"}}
        )
        schema["$defs"] = defs
        service = _build_service(_make_config([schema]))

        names = service._get_attribute_names_for_class("dag")

        assert len(names) == ClassificationService._MAX_WALK_NAMES
        # Well above the soft cap, so "...(+N more)" stays accurate for any
        # schema anyone would legitimately author.
        assert (
            ClassificationService._MAX_WALK_NAMES
            > ClassificationService.MAX_ATTRIBUTES_PER_CLASS
        )
        assert "walk ceiling" in caplog.text

    def test_unresolvable_refs_degrade_to_leaf_names(self):
        """Dangling / remote / non-dict properties are emitted, never raised on."""
        schema = _make_class_schema(
            "odd",
            "Odd schema",
            properties={
                "dangling": {"$ref": "#/$defs/Nope"},
                "remote": {"$ref": "https://example.com/schema.json"},
                "notadict": "oops",
                "plain": {"type": "string"},
            },
        )
        schema["$defs"] = {}
        service = _build_service(_make_config([schema]))

        assert service._get_attribute_names_for_class("odd") == [
            "dangling",
            "remote",
            "plain",
        ]


# ---------------------------------------------------------------------------
# 2. Formatter methods
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFormatClassesAndAttributesList:
    def test_renders_xml_blocks_with_attributes(self, appraisal_inspection_config):
        service = _build_service(appraisal_inspection_config)
        rendered = service._format_classes_and_attributes_list()
        # one block per class
        assert rendered.count("<class ") == 2
        # canonical disambiguation signal from the issue is present
        assert "appraised_value" in rendered
        assert "inspection_date" in rendered
        # description preserved
        assert "Real estate valuation report" in rendered
        # XML structure
        assert '<class name="appraisal_report">' in rendered
        assert "<description>" in rendered
        assert "<attributes>" in rendered

    def test_class_without_schema_renders_no_schema_marker(self):
        config = _make_config([_make_class_schema("invoice", "An invoice document")])
        service = _build_service(config)
        rendered = service._format_classes_and_attributes_list()
        # users with no schema get an explicit marker — easier to debug
        # than silently dropping the attributes line.
        assert "<attributes>(no schema)</attributes>" in rendered

    def test_soft_cap_truncates_and_warns(self, caplog):
        # Build a class with MAX_ATTRIBUTES_PER_CLASS + 5 properties
        cap = ClassificationService.MAX_ATTRIBUTES_PER_CLASS
        overflow = 5
        properties = {
            f"field_{i:03d}": {"type": "string"} for i in range(cap + overflow)
        }
        config = _make_config(
            [_make_class_schema("huge_doc", "A doc with many fields", properties)]
        )
        service = _build_service(config)

        import logging

        with caplog.at_level(logging.WARNING):
            rendered = service._format_classes_and_attributes_list()

        # truncation marker present
        assert f"...(+{overflow} more)" in rendered
        # only first N field names rendered
        assert "field_000" in rendered
        assert f"field_{cap - 1:03d}" in rendered
        assert f"field_{cap:03d}" not in rendered  # the (cap+1)th one is truncated
        # warning logged
        assert any("truncating" in record.message.lower() for record in caplog.records)


@pytest.mark.unit
class TestFormatClassesAndAttributesTable:
    def test_renders_three_column_markdown_table(self, appraisal_inspection_config):
        service = _build_service(appraisal_inspection_config)
        rendered = service._format_classes_and_attributes_table()
        # markdown table header
        assert rendered.startswith("| type | description | attributes |")
        # both classes present
        assert "appraisal_report" in rendered
        assert "inspection_report" in rendered
        # attributes column populated
        assert "appraised_value" in rendered
        assert "inspection_date" in rendered

    def test_pipe_in_description_is_escaped(self):
        config = _make_config(
            [
                _make_class_schema(
                    "weird",
                    "Has | pipe in description",
                    properties={"foo": {"type": "string"}},
                ),
            ]
        )
        service = _build_service(config)
        rendered = service._format_classes_and_attributes_table()
        # pipe inside a cell must be escaped or the markdown table breaks
        assert "Has \\| pipe in description" in rendered


# ---------------------------------------------------------------------------
# 3. End-to-end substitution — cost-neutral when not referenced
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPlaceholderSubstitutionEndToEnd:
    def test_substitution_dict_includes_new_placeholder(
        self, appraisal_inspection_config
    ):
        """The substitutions dict always carries the new placeholder; the
        underlying format_prompt only substitutes referenced keys, so
        prompts that don't use it incur no extra cost."""
        service = _build_service(appraisal_inspection_config)
        subs = service._build_classification_substitutions(
            document_text="hello",
            class_names_and_descriptions="dummy classes",
        )
        assert "DOCUMENT_TEXT" in subs
        assert "CLASS_NAMES_AND_DESCRIPTIONS" in subs
        assert "CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS" in subs
        # value is the XML-list variant (page-level default)
        assert "<class name=" in subs["CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS"]

    def test_legacy_prompt_unchanged_when_placeholder_absent(
        self, appraisal_inspection_config
    ):
        """Cost-neutrality check: a prompt that does NOT reference the new
        placeholder must render exactly as before."""
        service = _build_service(appraisal_inspection_config)
        legacy_template = (
            "Classify into one of:\n{CLASS_NAMES_AND_DESCRIPTIONS}\n\n"
            "Text:\n{DOCUMENT_TEXT}"
        )
        content = service._build_content_without_image_placeholder(
            prompt_template=legacy_template,
            document_text="some doc",
            class_names_and_descriptions="invoice [ desc ]",
            image_content=None,
        )
        rendered_text = content[0]["text"]
        # legacy placeholders substituted
        assert "invoice [ desc ]" in rendered_text
        assert "some doc" in rendered_text
        # new placeholder NOT in output (it wasn't in the template)
        assert "<class name=" not in rendered_text
        assert "appraised_value" not in rendered_text

    def test_custom_prompt_referencing_new_placeholder_gets_substituted(
        self, appraisal_inspection_config
    ):
        """Power users who opt in via a custom prompt see the schema
        attribute names rendered in the final text content."""
        service = _build_service(appraisal_inspection_config)
        custom_template = (
            "Classify into one of:\n{CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}\n\n"
            "Text:\n{DOCUMENT_TEXT}"
        )
        content = service._build_content_without_image_placeholder(
            prompt_template=custom_template,
            document_text="Appraised Value: $450,000",
            # legacy classes string is irrelevant here — not referenced
            class_names_and_descriptions="UNUSED",
            image_content=None,
        )
        rendered_text = content[0]["text"]
        # the disambiguating schema field is now in the prompt
        assert "appraised_value" in rendered_text
        assert "inspection_date" in rendered_text
        # XML structure rendered
        assert '<class name="appraisal_report">' in rendered_text
        # legacy unused placeholder string is not in rendered output (the
        # template doesn't reference {CLASS_NAMES_AND_DESCRIPTIONS})
        assert "UNUSED" not in rendered_text

    def test_holistic_path_uses_table_variant(self, appraisal_inspection_config):
        """The holistic classify_document path should render the
        markdown-TABLE variant of the placeholder (matches the
        surrounding {CLASS_NAMES_AND_DESCRIPTIONS} table format)."""
        # Switch to holistic mode and use a custom task_prompt that
        # references the placeholder.
        config = json.loads(json.dumps(appraisal_inspection_config))
        config["classification"]["classificationMethod"] = (
            "textbasedHolisticClassification"
        )
        config["classification"]["task_prompt"] = (
            "Classes:\n{CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}\n\n"
            "Text:\n{DOCUMENT_TEXT}\n"
            'Respond with JSON: {"segments": [...]}'
        )

        # Build a minimal Document with one page so holistic_classify_document
        # gets past the "no pages" guard. Use a fake parsed_text_uri with a
        # patched s3.get_text_content.
        from idp_common.models import Document, Page

        doc = Document(
            id="test-doc",
            input_bucket="bucket",
            input_key="key.pdf",
            pages={
                "1": Page(
                    page_id="1",
                    parsed_text_uri="s3://bucket/text/1.txt",
                    image_uri=None,
                    raw_text_uri="s3://bucket/raw/1.txt",
                ),
                "2": Page(
                    page_id="2",
                    parsed_text_uri="s3://bucket/text/2.txt",
                    image_uri=None,
                    raw_text_uri="s3://bucket/raw/2.txt",
                ),
            },
        )

        captured_content: list = []

        def _fake_invoke_bedrock_model(self, content, config):  # noqa: ARG001
            captured_content.extend(content)
            return {
                "response": {
                    "output": {
                        "message": {
                            "content": [
                                {
                                    "text": json.dumps(
                                        {
                                            "segments": [
                                                {
                                                    "ordinal_start_page": 1,
                                                    "ordinal_end_page": 2,
                                                    "type": "appraisal_report",
                                                }
                                            ]
                                        }
                                    )
                                }
                            ]
                        }
                    }
                },
                "metering": {},
            }

        with (
            patch("boto3.Session"),
            patch(
                "idp_common.s3.get_text_content",
                return_value="Appraised Value: $450,000",
            ),
            patch.object(
                ClassificationService,
                "_invoke_bedrock_model",
                _fake_invoke_bedrock_model,
            ),
        ):
            service = ClassificationService(
                region="us-west-2", config=config, backend="bedrock"
            )
            service.holistic_classify_document(doc)

        assert captured_content, "Bedrock model was not invoked"
        rendered_prompt = captured_content[0]["text"]
        # markdown table header is the holistic variant's signature
        assert "| type | description | attributes |" in rendered_prompt
        # disambiguating attribute is rendered
        assert "appraised_value" in rendered_prompt
