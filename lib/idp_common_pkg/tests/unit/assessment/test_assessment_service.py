# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the AssessmentService class.
"""

# ruff: noqa: E402, I001
# The above line disables E402 (module level import not at top of file) and I001 (import block sorting) for this file

import pytest

# Import standard library modules first
import json
import sys
from textwrap import dedent
from unittest.mock import MagicMock, patch

# Ensure PIL is importable before importing modules that depend on it. Only fall
# back to a MagicMock when PIL is genuinely not installed — injecting one
# unconditionally leaks globally via sys.modules and replaces the real PIL for
# every later test file, breaking tests that manipulate real images (e.g.
# discovery/test_embedding_service.py, discovery/test_discovery_agent.py).
for _name in ("PIL", "PIL.Image"):
    if _name not in sys.modules:
        try:
            __import__(_name)
        except ImportError:
            sys.modules[_name] = MagicMock()

# Now import third-party modules

# Finally import application modules
from idp_common.assessment.service import AssessmentService
from idp_common.config.models import IDPConfig
from idp_common.models import Document, Section, Status, Page


@pytest.mark.unit
class TestAssessmentService:
    """Tests for the AssessmentService class."""

    @pytest.fixture
    def mock_config(self):
        """Fixture providing a mock configuration in JSON Schema format."""
        return {
            "classes": [
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "invoice",
                    "x-aws-idp-document-type": "invoice",
                    "type": "object",
                    "description": "An invoice document",
                    "properties": {
                        "invoice_number": {
                            "type": "string",
                            "description": "The invoice number",
                            "x-aws-idp-confidence-threshold": 0.95,
                        },
                        "invoice_date": {
                            "type": "string",
                            "description": "The invoice date",
                            "x-aws-idp-confidence-threshold": 0.85,
                        },
                        "total_amount": {
                            "type": "string",
                            "description": "The total amount",
                            "x-aws-idp-confidence-threshold": 0.9,
                        },
                    },
                },
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "bank_statement",
                    "x-aws-idp-document-type": "bank_statement",
                    "type": "object",
                    "description": "Monthly bank account statement",
                    "properties": {
                        "account_number": {
                            "type": "string",
                            "description": "Primary account identifier",
                            "x-aws-idp-confidence-threshold": 0.95,
                        },
                        "account_holder_address": {
                            "type": "object",
                            "description": "Complete address information for the account holder",
                            "properties": {
                                "street_number": {
                                    "type": "string",
                                    "description": "House or building number",
                                    "x-aws-idp-confidence-threshold": 0.9,
                                },
                                "street_name": {
                                    "type": "string",
                                    "description": "Name of the street",
                                    "x-aws-idp-confidence-threshold": 0.8,
                                },
                                "city": {
                                    "type": "string",
                                    "description": "City name",
                                    "x-aws-idp-confidence-threshold": 0.9,
                                },
                                "state": {
                                    "type": "string",
                                    "description": "State abbreviation",
                                },
                            },
                        },
                        "transactions": {
                            "type": "array",
                            "description": "List of all transactions in the statement period",
                            "x-aws-idp-list-item-description": "Individual transaction record",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "date": {
                                        "type": "string",
                                        "description": "Transaction date (MM/DD/YYYY)",
                                        "x-aws-idp-confidence-threshold": 0.9,
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": "Transaction description or merchant name",
                                        "x-aws-idp-confidence-threshold": 0.7,
                                    },
                                    "amount": {
                                        "type": "string",
                                        "description": "Transaction amount",
                                        "x-aws-idp-confidence-threshold": 0.95,
                                    },
                                    "balance": {
                                        "type": "string",
                                        "description": "Account balance after transaction",
                                    },
                                },
                            },
                        },
                    },
                },
            ],
            "assessment": {
                "model": "anthropic.claude-3-sonnet-20240229-v1:0",
                "temperature": 0.0,
                "top_k": 5,
                "default_confidence_threshold": 0.8,
                "system_prompt": "You are a document assessment assistant.",
                "task_prompt": dedent("""
                    Assess the confidence of the following extraction results from this {DOCUMENT_CLASS} document:
                    
                    Expected fields:
                    {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
                    
                    Extraction results:
                    {EXTRACTION_RESULTS}
                    
                    Document text:
                    {DOCUMENT_TEXT}
                    
                    Respond with a JSON object containing confidence scores and reasons for each field.
                """),
            },
        }

    @pytest.fixture
    def service(self, mock_config):
        """Fixture providing an AssessmentService instance."""
        return AssessmentService(region="us-west-2", config=mock_config)

    @pytest.fixture
    def sample_document_with_extraction(self):
        """Fixture providing a sample document with extraction results."""
        doc = Document(
            id="test-doc",
            input_key="test-document.pdf",
            input_bucket="input-bucket",
            output_bucket="output-bucket",
            status=Status.ASSESSING,
        )

        # Add pages
        doc.pages["1"] = Page(
            page_id="1",
            image_uri="s3://input-bucket/test-document.pdf/pages/1/image.jpg",
            parsed_text_uri="s3://input-bucket/test-document.pdf/pages/1/parsed.txt",
        )

        # Add section with extraction results
        section = Section(section_id="1", classification="invoice", page_ids=["1"])
        section.extraction_result_uri = (
            "s3://output-bucket/test-document.pdf/sections/1/result.json"
        )
        doc.sections.append(section)

        return doc

    def test_init(self, mock_config):
        """Test initialization with configuration."""
        service = AssessmentService(region="us-west-2", config=mock_config)

        assert service.region == "us-west-2"
        # Config is converted to IDPConfig model, verify it has the expected structure
        # (v0.6: confidence assessment config lives under extraction.confidence)
        assert hasattr(service.config.extraction, "confidence")
        assert (
            service.config.extraction.confidence.model
            == mock_config["assessment"]["model"]
        )

    def test_get_class_schema(self, service):
        """Test getting schema for a document class."""
        # Test with existing class
        invoice_schema = service._get_class_schema("invoice")
        assert invoice_schema.get("x-aws-idp-document-type") == "invoice"
        assert "properties" in invoice_schema
        assert "invoice_number" in invoice_schema["properties"]
        assert "invoice_date" in invoice_schema["properties"]
        assert "total_amount" in invoice_schema["properties"]

        # Test with non-existent class
        unknown_schema = service._get_class_schema("unknown")
        assert unknown_schema == {}

        # Test case insensitivity
        invoice_schema_upper = service._get_class_schema("INVOICE")
        assert invoice_schema_upper.get("x-aws-idp-document-type") == "invoice"

    @patch("idp_common.assessment.service.bedrock.extract_text_from_response")
    @patch("idp_common.assessment.service.bedrock.invoke_model")
    @patch("idp_common.image.prepare_image")
    def test_assess_results_flags_truncation(
        self, mock_prepare_image, mock_invoke_model, mock_extract_text, service
    ):
        """A Converse stopReason of 'max_tokens' marks the core result truncated
        (so the batcher knows to retry over a smaller slice) and does NOT let the
        default 0.5 fallback masquerade as a real score."""
        mock_prepare_image.return_value = b"img"
        # Truncated response: stopReason=max_tokens + an incomplete JSON body.
        mock_invoke_model.return_value = {
            "response": {
                "stopReason": "max_tokens",
                "output": {"message": {"content": [{"text": "```json\n{"}]}},
            },
            "metering": {"Assessment/bedrock/m": {"outputTokens": 10000}},
        }
        mock_extract_text.return_value = "```json\n{"

        core = service.assess_results(
            class_label="invoice",
            extraction_results={"invoice_number": "INV-1"},
            document_text="text",
            page_images=[b"img"],
        )

        assert core.truncated is True
        assert core.parsing_succeeded is False

    @patch("idp_common.assessment.service.bedrock.extract_text_from_response")
    @patch("idp_common.assessment.service.bedrock.invoke_model")
    def test_assess_results_salvages_truncated_prefix(
        self, mock_invoke_model, mock_extract_text, service
    ):
        """B4: a truncated response whose VALID PREFIX parses is salvaged — the
        rows that came back keep their real scores instead of the whole call being
        thrown away as all-default. Still flagged truncated so the caller retries
        only the missing remainder."""
        # Truncated mid-way through the third transaction row, but rows 1-2 are
        # complete and well-formed → repair_truncated_json recovers them.
        partial = (
            '{"transactions": ['
            '{"date": {"confidence": 0.97}, "amount": {"confidence": 0.95}}, '
            '{"date": {"confidence": 0.92}, "amount": {"confidence": 0.9}}, '
            '{"date": {"confidence": 0.8'  # cut off here
        )
        mock_invoke_model.return_value = {
            "response": {
                "stopReason": "max_tokens",
                "output": {"message": {"content": [{"text": partial}]}},
            },
            "metering": {"Assessment/bedrock/m": {"outputTokens": 10000}},
        }
        mock_extract_text.return_value = partial

        core = service.assess_results(
            class_label="bank_statement",
            extraction_results={
                "transactions": [
                    {"date": "1/1", "amount": "10"},
                    {"date": "1/2", "amount": "20"},
                    {"date": "1/3", "amount": "30"},
                ]
            },
            document_text="text",
            page_images=[],
        )

        assert core.truncated is True  # caller still retries the remainder
        # Salvaged the 2 complete rows (real scores, NOT the 0.5 default).
        tx = core.enhanced_assessment.get("transactions")
        assert isinstance(tx, list) and len(tx) >= 2
        assert tx[0]["date"]["confidence"] == 0.97
        assert tx[1]["amount"]["confidence"] == 0.9

    @patch("idp_common.assessment.service.bedrock.extract_text_from_response")
    @patch("idp_common.assessment.service.bedrock.invoke_model")
    def test_ref_wrapped_array_gets_per_row_leaves_and_subfield_thresholds(
        self, mock_invoke_model, mock_extract_text
    ):
        """An array declared as a bare ``$ref`` into ``$defs`` must score per-row.

        ``_assess_core`` read ``type`` off the raw property, so a
        ``{"$ref": "#/$defs/TxnList"}`` array defaulted to ``TYPE_STRING`` →
        ``attr_type == "simple"`` → the per-row list assessment was collapsed to
        one default ``{"confidence": 0.5}`` leaf, which reconciliation then padded
        to N null placeholders that no model could fill (GitHub issue #678).

        Also pins the consequence that only becomes reachable once the property is
        typed correctly: per-sub-field thresholds resolve from the referenced item
        definition instead of silently falling back to the uniform container
        threshold.
        """
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "stmt",
            "x-aws-idp-document-type": "stmt",
            "type": "object",
            "properties": {"txns": {"$ref": "#/$defs/TxnList"}},
            "$defs": {
                "TxnList": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string"},
                            "amount": {
                                "type": "number",
                                "x-aws-idp-confidence-threshold": 0.99,
                            },
                        },
                    },
                }
            },
        }
        svc = AssessmentService(
            region="us-west-2",
            config={
                "classes": [schema],
                "hitl": {"confidence_threshold": 0.8},
                "assessment": {
                    "model": "anthropic.claude-3-sonnet-20240229-v1:0",
                    "default_confidence_threshold": 0.8,
                    "system_prompt": "You are a document assessment assistant.",
                    "task_prompt": (
                        "Assess {DOCUMENT_CLASS}\n"
                        "{ATTRIBUTE_NAMES_AND_DESCRIPTIONS}\n"
                        "{EXTRACTION_RESULTS}\n{DOCUMENT_TEXT}"
                    ),
                },
            },
        )

        body = (
            '{"txns": ['
            '{"date": {"confidence": 0.97}, "amount": {"confidence": 0.95}}, '
            '{"date": {"confidence": 0.92}, "amount": {"confidence": 0.91}}]}'
        )
        mock_invoke_model.return_value = {
            "response": {
                "stopReason": "end_turn",
                "output": {"message": {"content": [{"text": body}]}},
            },
            "metering": {"Assessment/bedrock/m": {"outputTokens": 50}},
        }
        mock_extract_text.return_value = body

        core = svc.assess_results(
            class_label="stmt",
            extraction_results={
                "txns": [
                    {"date": "1/1", "amount": "10"},
                    {"date": "1/2", "amount": "20"},
                ]
            },
            document_text="text",
            page_images=[],
        )

        txns = core.enhanced_assessment.get("txns")
        # Per-row leaves — NOT a single collapsed {"confidence": 0.5} dict.
        assert isinstance(txns, list), f"collapsed to {txns!r}"
        assert len(txns) == 2
        assert txns[0]["date"]["confidence"] == 0.97
        assert txns[1]["amount"]["confidence"] == 0.91
        # Sub-field thresholds resolved through the $ref, not the 0.8 default.
        assert txns[0]["amount"]["confidence_threshold"] == 0.99
        assert txns[0]["date"]["confidence_threshold"] == 0.8
        # 0.95 and 0.91 are both below the 0.99 sub-field threshold → alerts fire
        # per row AND per sub-field, which the collapsed leaf could never produce.
        alerted = {a["attribute_name"] for a in core.confidence_threshold_alerts}
        assert {"txns[0].amount", "txns[1].amount"} <= alerted
        # date (0.97, 0.92) clears its own 0.8 threshold → no alert.
        assert not {a for a in alerted if a.endswith(".date")}

    @patch("idp_common.assessment.service.bedrock.extract_text_from_response")
    @patch("idp_common.assessment.service.bedrock.invoke_model")
    @patch("idp_common.image.prepare_image")
    def test_assess_results_not_truncated_on_normal_stop(
        self, mock_prepare_image, mock_invoke_model, mock_extract_text, service
    ):
        """A normal (end_turn) response with valid JSON is not flagged truncated."""
        mock_prepare_image.return_value = b"img"
        mock_invoke_model.return_value = {
            "response": {
                "stopReason": "end_turn",
                "output": {"message": {"content": [{"text": "{}"}]}},
            },
            "metering": {"Assessment/bedrock/m": {"outputTokens": 50}},
        }
        mock_extract_text.return_value = '{"invoice_number": {"confidence": 0.9}}'

        core = service.assess_results(
            class_label="invoice",
            extraction_results={"invoice_number": "INV-1"},
            document_text="text",
            page_images=[b"img"],
        )

        assert core.truncated is False
        assert core.parsing_succeeded is True

    @patch("idp_common.assessment.service.bedrock.extract_text_from_response")
    @patch("idp_common.assessment.service.bedrock.invoke_model")
    def test_page_images_reach_content_builder_in_every_geometry_mode(
        self, mock_invoke_model, mock_extract_text, mock_config
    ):
        """Page images are handed to the content builder regardless of geometry.mode.

        Whether they are actually attached is decided by the prompt (presence of
        {DOCUMENT_IMAGE}), not by geometry.mode: a confidence pass that is not
        asked for bounding boxes still needs the image to judge visually-evidenced
        fields (signature/checkbox/stamp booleans, handwriting).
        """
        mock_invoke_model.return_value = {
            "response": {
                "stopReason": "end_turn",
                "output": {"message": {"content": [{"text": "{}"}]}},
            },
            "metering": {},
        }
        mock_extract_text.return_value = '{"invoice_number": {"confidence": 0.9}}'

        def _run(mode: str):
            cfg = json.loads(json.dumps(mock_config))  # deep copy
            cfg.setdefault("extraction", {}).setdefault("geometry", {})["mode"] = mode
            svc = AssessmentService(region="us-west-2", config=cfg)
            with patch.object(
                svc,
                "_build_content_with_or_without_image_placeholder",
                return_value=[{"text": "prompt"}],
            ) as builder:
                svc.assess_results(
                    class_label="invoice",
                    extraction_results={"invoice_number": "INV-1"},
                    document_text="text",
                    page_images=[b"img1", b"img2", b"img3"],
                )
            # 7th positional arg is page_images passed into the content builder.
            return builder.call_args.args[6]

        for mode in ("ocr_only", "off", "llm_grounded", "llm"):
            assert _run(mode) == [b"img1", b"img2", b"img3"], mode

    @patch("idp_common.assessment.service.image.prepare_bedrock_image_attachment")
    def test_images_attached_only_when_placeholder_present(
        self, mock_attachment, service
    ):
        """{DOCUMENT_IMAGE} in the template is what decides image attachment."""
        mock_attachment.side_effect = lambda img: {"image": img}

        def _build(template: str):
            return service._build_content_with_or_without_image_placeholder(
                template,
                "text",
                "invoice",
                "attrs",
                "{}",
                "",
                [b"img1", b"img2"],
            )

        with_placeholder = _build("before {DOCUMENT_IMAGE} after")
        assert [item for item in with_placeholder if "image" in item] == [
            {"image": b"img1"},
            {"image": b"img2"},
        ]

        without_placeholder = _build("no image placeholder here")
        assert not any("image" in item for item in without_placeholder)

    def test_format_property_descriptions(self, service):
        """Test formatting property descriptions from JSON Schema."""
        # Get invoice schema
        invoice_schema = service._get_class_schema("invoice")
        formatted = service._format_property_descriptions(invoice_schema)

        assert "invoice_number" in formatted
        assert "The invoice number" in formatted
        assert "invoice_date" in formatted
        assert "The invoice date" in formatted

    def test_format_nested_property_descriptions(self, service):
        """Test formatting nested property descriptions (object and array types)."""
        # Get bank statement schema with nested structures
        bank_statement_schema = service._get_class_schema("bank_statement")
        formatted = service._format_property_descriptions(bank_statement_schema)

        # Test that main attributes are present
        assert "account_number" in formatted
        assert "Primary account identifier" in formatted
        assert "account_holder_address" in formatted
        assert "Complete address information" in formatted
        assert "transactions" in formatted
        assert "List of all transactions" in formatted

        # Test that group nested attributes are properly indented
        assert "  - street_number" in formatted
        assert "House or building number" in formatted
        assert "  - street_name" in formatted
        assert "Name of the street" in formatted
        assert "  - city" in formatted
        assert "City name" in formatted
        assert "  - state" in formatted
        assert "State abbreviation" in formatted

        # Test that list nested attributes are properly formatted
        assert "Each item: Individual transaction record" in formatted
        assert "  - date" in formatted
        assert "Transaction date (MM/DD/YYYY)" in formatted
        assert "  - description" in formatted
        assert "Transaction description or merchant name" in formatted
        assert "  - amount" in formatted
        assert "Transaction amount" in formatted
        assert "  - balance" in formatted
        assert "Account balance after transaction" in formatted

    def test_format_property_descriptions_resolves_refs(self, service):
        """$ref-based groups and lists must render their real descriptions.

        Schemas authored in the UI put groups/list items in ``$defs`` and
        reference them, which previously collapsed every such property to
        ``Name  \t[  ]`` — the confidence model saw no field descriptions at all.
        """
        schema = {
            "$id": "tax_form",
            "type": "object",
            "properties": {
                "Signatures": {"$ref": "#/$defs/Signatures"},
                "Payments": {"$ref": "#/$defs/Payments"},
            },
            "$defs": {
                "Signatures": {
                    "type": "object",
                    "description": "Signatures of the examiner and taxpayer",
                    "properties": {
                        "SignatureOfTaxpayer1": {
                            "type": "boolean",
                            "description": "Does taxpayer 1's signature exist?",
                        },
                        "DateOfSignature1": {"$ref": "#/$defs/SignatureDate"},
                    },
                },
                "SignatureDate": {
                    "type": "string",
                    "description": "Date next to the signature",
                },
                "Payments": {
                    "type": "array",
                    "description": "Payments applied to the balance",
                    "x-aws-idp-list-item-description": "One payment",
                    "items": {"$ref": "#/$defs/Payment"},
                },
                "Payment": {
                    "type": "object",
                    "properties": {
                        "amount": {"type": "string", "description": "Amount paid"},
                    },
                },
            },
        }

        formatted = service._format_property_descriptions(schema)

        # Group behind a $ref: own description plus nested members.
        assert "Signatures  \t[ Signatures of the examiner and taxpayer ]" in formatted
        assert "  - SignatureOfTaxpayer1  \t[ Does taxpayer 1's signature exist? ]" in (
            formatted
        )
        # A nested member that is itself a $ref resolves too.
        assert "  - DateOfSignature1  \t[ Date next to the signature ]" in formatted
        # List behind a $ref, with $ref'd item shape.
        assert "Payments  \t[ Payments applied to the balance ]" in formatted
        assert "Each item: One payment" in formatted
        assert "  - amount  \t[ Amount paid ]" in formatted

    def test_format_property_descriptions_tolerates_bad_refs(self, service):
        """Unresolvable refs degrade gracefully instead of raising."""
        schema = {
            "$id": "odd",
            "type": "object",
            "properties": {
                "dangling": {"$ref": "#/$defs/Nope"},
                "remote": {"$ref": "https://example.com/schema.json"},
                "cyclic": {"$ref": "#/$defs/Loop"},
                "plain": {"type": "string", "description": "A normal field"},
            },
            "$defs": {"Loop": {"$ref": "#/$defs/Loop"}},
        }

        formatted = service._format_property_descriptions(schema)

        for name in ("dangling", "remote", "cyclic", "plain"):
            assert name in formatted
        assert "A normal field" in formatted

    def test_format_property_descriptions_ref_sibling_description_wins(self, service):
        """A description alongside a $ref overrides the definition's."""
        schema = {
            "$id": "override",
            "type": "object",
            "properties": {
                "Group": {
                    "$ref": "#/$defs/Group",
                    "description": "Local override description",
                }
            },
            "$defs": {
                "Group": {
                    "type": "object",
                    "description": "Shared description",
                    "properties": {},
                }
            },
        }

        formatted = service._format_property_descriptions(schema)

        assert "Group  \t[ Local override description ]" in formatted
        assert "Shared description" not in formatted

    def test_confidence_thresholds_in_schema(self, service):
        """Test that confidence thresholds are present in JSON Schema."""
        # Get invoice schema
        invoice_schema = service._get_class_schema("invoice")
        properties = invoice_schema.get("properties", {})

        # Test properties have confidence thresholds
        assert properties["invoice_number"]["x-aws-idp-confidence-threshold"] == 0.95
        assert properties["invoice_date"]["x-aws-idp-confidence-threshold"] == 0.85
        assert properties["total_amount"]["x-aws-idp-confidence-threshold"] == 0.9

    def test_nested_confidence_thresholds_in_schema(self, service):
        """Test that nested confidence thresholds are accessible in JSON Schema."""
        bank_statement_schema = service._get_class_schema("bank_statement")
        properties = bank_statement_schema.get("properties", {})

        # Test top-level property
        assert properties["account_number"]["x-aws-idp-confidence-threshold"] == 0.95

        # Test nested object properties
        address_props = properties["account_holder_address"]["properties"]
        assert address_props["street_number"]["x-aws-idp-confidence-threshold"] == 0.9
        assert address_props["street_name"]["x-aws-idp-confidence-threshold"] == 0.8
        assert address_props["city"]["x-aws-idp-confidence-threshold"] == 0.9
        # state has no threshold - not set

        # Test array item properties
        transaction_props = properties["transactions"]["items"]["properties"]
        assert transaction_props["date"]["x-aws-idp-confidence-threshold"] == 0.9
        assert transaction_props["description"]["x-aws-idp-confidence-threshold"] == 0.7
        assert transaction_props["amount"]["x-aws-idp-confidence-threshold"] == 0.95
        # balance has no threshold - not set

    def test_format_property_descriptions_edge_cases(self, service):
        """Test formatting property descriptions with edge cases."""
        # Test empty schema
        empty_schema = {"properties": {}}
        formatted = service._format_property_descriptions(empty_schema)
        assert formatted == ""

        # Test object with no nested properties
        schema_no_props = {
            "properties": {
                "address": {
                    "type": "object",
                    "description": "Complete address information",
                }
            }
        }
        formatted = service._format_property_descriptions(schema_no_props)
        assert "address" in formatted
        assert "Complete address information" in formatted

        # Test array with no items schema
        schema_no_items = {
            "properties": {
                "items": {
                    "type": "array",
                    "description": "List of items",
                    "x-aws-idp-list-item-description": "Individual item",
                }
            }
        }
        formatted = service._format_property_descriptions(schema_no_items)
        assert "items" in formatted
        assert "List of items" in formatted
        assert "Each item: Individual item" in formatted

    @patch("idp_common.s3.get_json_content")
    @patch("idp_common.s3.get_text_content")
    @patch("idp_common.image.prepare_image")
    @patch("idp_common.bedrock.invoke_model")
    @patch("idp_common.s3.write_content")
    @patch("idp_common.utils.parse_s3_uri")
    @patch("idp_common.utils.merge_metering_data")
    @patch("idp_common.metrics.put_metric")
    def test_process_document_section_success(
        self,
        mock_put_metric,
        mock_merge_metering,
        mock_parse_s3_uri,
        mock_write_content,
        mock_invoke_model,
        mock_prepare_image,
        mock_get_text_content,
        mock_get_json_content,
        service,
        sample_document_with_extraction,
    ):
        """Test successful assessment of a document section."""
        # Mock S3 responses
        mock_get_json_content.side_effect = [
            # Extraction results
            {
                "document_class": {"type": "invoice"},
                "inference_result": {
                    "invoice_number": "INV-123",
                    "invoice_date": "2025-05-08",
                    "total_amount": "$100.00",
                },
                "metadata": {"parsing_succeeded": True},
            }
        ]
        mock_get_text_content.return_value = "Page 1 text"
        mock_prepare_image.return_value = b"image_data"
        mock_parse_s3_uri.return_value = (
            "output-bucket",
            "test-document.pdf/sections/1/result.json",
        )

        # Mock Bedrock response
        mock_invoke_model.return_value = {
            "response": {
                "output": {
                    "message": {
                        "content": [
                            {
                                "text": """{
                                    "invoice_number": {
                                        "confidence": 0.98,
                                        "confidence_reason": "Clear and legible invoice number"
                                    },
                                    "invoice_date": {
                                        "confidence": 0.90,
                                        "confidence_reason": "Date format is standard"
                                    },
                                    "total_amount": {
                                        "confidence": 0.95,
                                        "confidence_reason": "Amount clearly visible"
                                    }
                                }"""
                            }
                        ]
                    }
                }
            },
            "metering": {"tokens": 500},
        }

        # Mock metering merge
        mock_merge_metering.return_value = {"tokens": 500}

        # Process the document section
        result = service.process_document_section(sample_document_with_extraction, "1")

        # Verify the document was processed without errors
        assert len(result.errors) == 0

        # Verify the calls
        mock_get_json_content.assert_called_once()
        mock_get_text_content.assert_called_once()
        mock_invoke_model.assert_called_once()
        mock_write_content.assert_called_once()

        # Verify the content written to S3 includes assessment data
        written_content = mock_write_content.call_args[0][0]
        assert "explainability_info" in written_content
        assert len(written_content["explainability_info"]) == 1

        assessment_data = written_content["explainability_info"][0]

        # Check that confidence thresholds are added to assessment data
        assert "invoice_number" in assessment_data
        assert assessment_data["invoice_number"]["confidence"] == 0.98
        assert assessment_data["invoice_number"]["confidence_threshold"] == 0.95

        assert "invoice_date" in assessment_data
        assert assessment_data["invoice_date"]["confidence"] == 0.90
        assert assessment_data["invoice_date"]["confidence_threshold"] == 0.85

        assert "total_amount" in assessment_data
        assert assessment_data["total_amount"]["confidence"] == 0.95
        assert assessment_data["total_amount"]["confidence_threshold"] == 0.9

    @patch("idp_common.metrics.put_metric")
    def test_process_document_section_no_extraction_results(
        self, mock_put_metric, service, sample_document_with_extraction
    ):
        """Test processing a document section with no extraction results."""
        # Remove extraction result URI
        sample_document_with_extraction.sections[0].extraction_result_uri = None

        # Process the section
        result = service.process_document_section(sample_document_with_extraction, "1")

        # Verify error was added
        assert len(result.errors) == 1
        assert "Section 1 has no extraction results to assess" in result.errors[0]

    @patch("idp_common.metrics.put_metric")
    def test_process_document_section_missing_section(
        self, mock_put_metric, service, sample_document_with_extraction
    ):
        """Test processing a document section that doesn't exist."""
        # Process a non-existent section
        result = service.process_document_section(
            sample_document_with_extraction, "999"
        )

        # Verify error was added
        assert len(result.errors) == 1
        assert "Section 999 not found in document" in result.errors[0]

    @patch("idp_common.s3.get_json_content")
    @patch("idp_common.metrics.put_metric")
    def test_process_document_section_empty_extraction_results(
        self,
        mock_put_metric,
        mock_get_json_content,
        service,
        sample_document_with_extraction,
    ):
        """Test processing a document section with empty extraction results."""
        # Mock empty extraction results
        mock_get_json_content.return_value = {
            "document_class": {"type": "invoice"},
            "inference_result": {},
            "metadata": {"parsing_succeeded": True},
        }

        # Process the section
        result = service.process_document_section(sample_document_with_extraction, "1")

        # Should return without error but log warning
        assert len(result.errors) == 0
        mock_get_json_content.assert_called_once()

    def test_init_with_none_config(self):
        """Test initialization with None config creates default IDPConfig."""
        service = AssessmentService(region="us-east-1", config=None)

        assert service.region == "us-east-1"
        assert isinstance(service.config, IDPConfig)
        # Verify default config has confidence settings (v0.6: extraction.confidence)
        assert hasattr(service.config.extraction, "confidence")

    def test_init_with_dict_config(self, mock_config):
        """Test initialization with dict config converts to IDPConfig."""
        service = AssessmentService(region="us-east-1", config=mock_config)

        assert service.region == "us-east-1"
        assert isinstance(service.config, IDPConfig)
        # Verify config was properly converted
        assert (
            service.config.extraction.confidence.model
            == mock_config["assessment"]["model"]
        )

    def test_init_with_idpconfig_instance(self, mock_config):
        """Test initialization with IDPConfig instance (the previously failing case)."""
        # Create an IDPConfig instance first
        config_instance = IDPConfig(**mock_config)

        # Initialize service with IDPConfig instance
        service = AssessmentService(region="us-east-1", config=config_instance)

        assert service.region == "us-east-1"
        assert isinstance(service.config, IDPConfig)
        # Should use the same instance
        assert service.config is config_instance
        # Verify config properties are accessible
        assert (
            service.config.extraction.confidence.model
            == mock_config["assessment"]["model"]
        )

    def test_init_with_invalid_config_type(self):
        """Test initialization with invalid config type raises ValueError."""
        # Try to initialize with an invalid config type (e.g., a string)
        with pytest.raises(ValueError) as exc_info:
            AssessmentService(region="us-east-1", config="invalid_config")

        # Verify error message mentions the invalid type
        assert "Invalid config type" in str(exc_info.value)
