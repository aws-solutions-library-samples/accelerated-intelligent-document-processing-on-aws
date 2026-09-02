# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the evaluation service module.
"""

# ruff: noqa: E402, I001
# The above line disables E402 (module level import not at top of file) and I001 (import block sorting) for this file

# Mock munkres module before importing any modules that depend on it
import sys
from unittest.mock import MagicMock

# Create mock for munkres module and its components
munkres_mock = MagicMock()
munkres_mock.Munkres = MagicMock
munkres_mock.make_cost_matrix = MagicMock(return_value=[[0, 1], [1, 0]])
sys.modules["munkres"] = munkres_mock

# Import standard library modules first
import warnings
from unittest.mock import patch

# Now import third-party modules
import pytest

# Finally import application modules
from idp_common.evaluation.service import EvaluationService
from idp_common.evaluation.models import (
    AttributeEvaluationResult,
    SectionEvaluationResult,
)
from idp_common.models import Document, Section, Status


@pytest.fixture(autouse=True)
def suppress_datetime_warning():
    """Fixture to suppress the datetime.utcnow() deprecation warning from botocore."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="datetime.datetime.utcnow\\(\\) is deprecated",
            category=DeprecationWarning,
        )
        yield


@pytest.mark.unit
class TestEvaluationService:
    """
    Tests for the EvaluationService class.

    NOTE: Many tests in this class are skipped because they test internal methods
    that were removed during the Stickler migration. The public API tests
    (test_evaluate_document, test_evaluate_document_error) still pass.

    For Stickler-based tests, see test_evaluation_service_stickler.py.
    """

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
                            "x-aws-idp-evaluation-method": "EXACT",
                        },
                        "invoice_date": {
                            "type": "string",
                            "description": "The invoice date",
                            "x-aws-idp-evaluation-method": "FUZZY",
                            "evaluation_threshold": 0.9,
                        },
                        "total_amount": {
                            "type": "string",
                            "description": "The total amount",
                            "x-aws-idp-evaluation-method": "LLM",
                        },
                    },
                },
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "receipt",
                    "x-aws-idp-document-type": "receipt",
                    "type": "object",
                    "description": "A receipt document",
                    "properties": {
                        "receipt_number": {
                            "type": "string",
                            "description": "The receipt number",
                        },
                        "date": {
                            "type": "string",
                            "description": "The receipt date",
                        },
                    },
                },
            ],
            "evaluation": {
                "llm_method": {
                    "model": "anthropic.claude-3-sonnet-20240229-v1:0",
                    "temperature": 0.0,
                    "top_k": 5,
                    "system_prompt": "You are an evaluator that helps determine if values match.",
                    "task_prompt": "Compare {EXPECTED_VALUE} and {ACTUAL_VALUE} for {ATTRIBUTE_NAME}.",
                }
            },
        }

    @pytest.fixture
    def service(self, mock_config):
        """Fixture providing an EvaluationService instance."""
        return EvaluationService(region="us-west-2", config=mock_config, max_workers=5)

    @pytest.fixture
    def sample_document(self):
        """Fixture providing a sample document with sections."""
        doc = Document(
            id="test-doc",
            input_key="test-document.pdf",
            input_bucket="input-bucket",
            output_bucket="output-bucket",
            status=Status.EXTRACTING,
        )

        # Add sections
        doc.sections.append(
            Section(
                section_id="1",
                classification="invoice",
                page_ids=["1", "2"],
                extraction_result_uri="s3://input-bucket/test-document.pdf/sections/1/result.json",
            )
        )

        doc.sections.append(
            Section(
                section_id="2",
                classification="receipt",
                page_ids=["3"],
                extraction_result_uri="s3://input-bucket/test-document.pdf/sections/2/result.json",
            )
        )

        return doc

    @patch("idp_common.s3.get_json_content")
    @patch("idp_common.evaluation.service.EvaluationService._process_section")
    @patch("idp_common.s3.write_content")
    def test_evaluate_document(
        self,
        mock_write_content,
        mock_process_section,
        mock_get_json_content,
        service,
        sample_document,
    ):
        """Test evaluating a document."""
        # Create expected document
        expected_document = sample_document

        # Configure mock for _process_section
        section_result = SectionEvaluationResult(
            section_id="1",
            document_class="invoice",
            attributes=[
                AttributeEvaluationResult(
                    name="invoice_number",
                    expected="INV-123",
                    actual="INV-123",
                    matched=True,
                    score=1.0,
                    reason="Exact match",
                    evaluation_method="EXACT",
                )
            ],
            metrics={"precision": 1.0, "recall": 1.0, "f1_score": 1.0},
        )

        mock_process_section.return_value = (
            section_result,
            {"tp": 1, "fp": 0, "fn": 0, "tn": 0, "fp1": 0, "fp2": 0},
        )

        # Document-level metrics now come from Stickler counts (R10 removed
        # the calculate_metrics module the previous test used to patch).
        result = service.evaluate_document(
            actual_document=sample_document, expected_document=expected_document
        )

        # Check result
        assert result.evaluation_report_uri is not None
        assert result.status == Status.COMPLETED
        assert result.evaluation_result is not None

        # Verify write_content was called twice (for JSON and Markdown)
        assert mock_write_content.call_count == 2

    @patch("idp_common.s3.get_json_content")
    @patch("idp_common.evaluation.service.EvaluationService._process_section")
    def test_evaluate_document_error(
        self, mock_process_section, mock_get_json_content, service, sample_document
    ):
        """Test evaluating a document with an error."""
        # Create expected document
        expected_document = sample_document

        # Configure mock for _process_section to raise an exception
        mock_process_section.side_effect = Exception("Processing error")

        # Evaluate document
        result = service.evaluate_document(
            actual_document=sample_document, expected_document=expected_document
        )

        # Check result
        assert len(result.errors) > 0
        assert "Processing error" in result.errors[0]

    def test_flatten_confidence_scores_with_item_wrappers(self, service):
        """Test that _flatten_confidence_scores correctly unwraps Item_N wrappers in array elements."""
        # Simulate the structure seen in RealKIE-FCC-Verified extraction results
        # where some LineItems have direct fields and others are wrapped in Item_N keys
        confidence_scores = {
            "Agency": {"confidence": 0.99, "geometry": []},
            "LineItems": [
                {
                    "LineItemRate": {"confidence": 1.0, "geometry": []},
                    "LineItemDays": {"confidence": 0.8, "geometry": []},
                },
                {
                    "LineItemRate": {"confidence": 1.0, "geometry": []},
                    "LineItemDays": {"confidence": 0.95, "geometry": []},
                },
                {
                    "Item_6": {
                        "LineItemRate": {"confidence": 1.0, "geometry": []},
                        "LineItemDays": {"confidence": 0.95, "geometry": []},
                        "LineItemStartDate": {"confidence": 1.0, "geometry": []},
                    },
                    "confidence_threshold": 0.9,
                },
                {
                    "Item_7": {
                        "LineItemRate": {"confidence": 0.95, "geometry": []},
                        "LineItemDays": {"confidence": 0.9, "geometry": []},
                    },
                    "confidence_threshold": 0.9,
                },
            ],
        }

        # Test Rich Value conversion with wrapper keys
        inference_result = {
            "Agency": "BUYING TIME, LLC",
            "LineItems": [
                {"LineItemRate": 600.0, "LineItemDays": "MTWT---"},
                {"LineItemRate": 500.0, "LineItemDays": "MTWT---"},
                {
                    "LineItemRate": 450.0,
                    "LineItemDays": "MTWT---",
                    "LineItemStartDate": "10/09/12",
                },
                {"LineItemRate": 400.0, "LineItemDays": "MTWT---"},
            ],
        }

        rich_values = service._convert_to_rich_values(
            inference_result, confidence_scores
        )

        # Verify Agency field has rich value with confidence
        assert rich_values["Agency"] == {
            "_value": "BUYING TIME, LLC",
            "_confidence": 0.99,
        }

        # Verify LineItems array items have rich values with confidence
        assert rich_values["LineItems"][0]["LineItemRate"] == {
            "_value": 600.0,
            "_confidence": 1.0,
        }
        assert rich_values["LineItems"][0]["LineItemDays"] == {
            "_value": "MTWT---",
            "_confidence": 0.8,
        }
        assert rich_values["LineItems"][2]["LineItemStartDate"] == {
            "_value": "10/09/12",
            "_confidence": 1.0,
        }

    def test_convert_to_rich_values_with_various_wrapper_patterns(self, service):
        """Test that _convert_to_rich_values handles nested structures."""
        # Test nested confidence structures
        confidence_scores = {
            "Records": [
                {
                    "RecordID": {"confidence": 0.99, "geometry": []},
                    "RecordDate": {"confidence": 1.0, "geometry": []},
                },
                {
                    "RecordID": {"confidence": 0.95, "geometry": []},
                    "RecordDate": {"confidence": 0.98, "geometry": []},
                    "RecordAmount": {"confidence": 0.97, "geometry": []},
                },
                {
                    "RecordID": {"confidence": 0.92, "geometry": []},
                    "RecordDate": {"confidence": 0.94, "geometry": []},
                },
            ]
        }

        inference_result = {
            "Records": [
                {"RecordID": "R001", "RecordDate": "2024-01-01"},
                {"RecordID": "R002", "RecordDate": "2024-01-02", "RecordAmount": 100.0},
                {"RecordID": "R003", "RecordDate": "2024-01-03"},
            ]
        }

        rich_values = service._convert_to_rich_values(
            inference_result, confidence_scores
        )

        # Verify array items have rich values with confidence
        assert rich_values["Records"][0]["RecordID"] == {
            "_value": "R001",
            "_confidence": 0.99,
        }
        assert rich_values["Records"][1]["RecordAmount"] == {
            "_value": 100.0,
            "_confidence": 0.97,
        }
        assert rich_values["Records"][2]["RecordDate"] == {
            "_value": "2024-01-03",
            "_confidence": 0.94,
        }

    def test_convert_to_rich_values_preserves_legitimate_nesting(self, service):
        """Test that nested objects are correctly converted to rich values."""
        confidence_scores = {
            "Invoice": [
                {
                    "InvoiceDetails": {
                        "InvoiceID": {"confidence": 0.99, "geometry": []},
                    },
                }
            ]
        }

        inference_result = {
            "Invoice": [
                {
                    "InvoiceDetails": {
                        "InvoiceID": "INV001",
                    }
                }
            ]
        }

        rich_values = service._convert_to_rich_values(
            inference_result, confidence_scores
        )

        # InvoiceDetails nesting should be preserved in rich value structure
        assert rich_values["Invoice"][0]["InvoiceDetails"]["InvoiceID"] == {
            "_value": "INV001",
            "_confidence": 0.99,
        }

    # ------------------------------------------------------------------
    # Confidence-envelope unwrapping (issue #713)
    #
    # The unwrap exists because assessment output sometimes nests an
    # object's confidence map under a synthetic key ("Item #6") that has no
    # counterpart in the extracted value; left in place, every confidence
    # lookup misses. It used to fire on key count alone, which also
    # unwrapped any legitimately single-property object and silently
    # discarded all of its confidence.
    # ------------------------------------------------------------------

    def test_single_top_level_object_property_keeps_its_confidence(self, service):
        """Regression (#713): a class with ONE top-level object property.

        By key count this is indistinguishable from a wrapper, so the old
        heuristic unwrapped it and every confidence score underneath was
        dropped. The property name appears in the extracted value, which is
        the evidence that it is a declared field rather than an envelope.
        """
        inference_result = {
            "InvoiceDetails": {
                "InvoiceID": "INV001",
                "InvoiceDate": "2024-01-01",
            }
        }
        confidence_scores = {
            "InvoiceDetails": {
                "InvoiceID": {"confidence": 0.99, "geometry": []},
                "InvoiceDate": {"confidence": 0.88, "geometry": []},
            }
        }

        rich_values = service._convert_to_rich_values(
            inference_result, confidence_scores
        )

        assert rich_values["InvoiceDetails"]["InvoiceID"] == {
            "_value": "INV001",
            "_confidence": 0.99,
        }
        assert rich_values["InvoiceDetails"]["InvoiceDate"] == {
            "_value": "2024-01-01",
            "_confidence": 0.88,
        }

    def test_single_object_property_in_list_element_keeps_its_confidence(self, service):
        """Same misfire one level down: a list element with one object field."""
        inference_result = {
            "Records": [
                {"Detail": {"Amount": 100.0, "Currency": "USD"}},
                {"Detail": {"Amount": 200.0, "Currency": "EUR"}},
            ]
        }
        confidence_scores = {
            "Records": [
                {
                    "Detail": {
                        "Amount": {"confidence": 0.91, "geometry": []},
                        "Currency": {"confidence": 0.92, "geometry": []},
                    }
                },
                {
                    "Detail": {
                        "Amount": {"confidence": 0.93, "geometry": []},
                        "Currency": {"confidence": 0.94, "geometry": []},
                    }
                },
            ]
        }

        rich_values = service._convert_to_rich_values(
            inference_result, confidence_scores
        )

        assert rich_values["Records"][0]["Detail"]["Amount"] == {
            "_value": 100.0,
            "_confidence": 0.91,
        }
        assert rich_values["Records"][1]["Detail"]["Amount"] == {
            "_value": 200.0,
            "_confidence": 0.93,
        }
        assert rich_values["Records"][1]["Detail"]["Currency"] == {
            "_value": "EUR",
            "_confidence": 0.94,
        }

    def test_synthetic_wrapper_key_is_still_unwrapped(self, service):
        """The case the unwrap was written for must not regress.

        ``Item #6`` exists only on the confidence side, and its contents name
        fields that do exist in the value - so it is an envelope and comes off.
        """
        inference_result = {
            "LineItems": [
                {"LineItemRate": 1000.0, "LineItemDays": "------S"},
            ]
        }
        confidence_scores = {
            "LineItems": [
                {
                    "Item #6": {
                        "LineItemRate": {"confidence": 1.0, "geometry": []},
                        "LineItemDays": {"confidence": 0.8, "geometry": []},
                    }
                },
            ]
        }

        rich_values = service._convert_to_rich_values(
            inference_result, confidence_scores
        )

        assert rich_values["LineItems"][0]["LineItemRate"] == {
            "_value": 1000.0,
            "_confidence": 1.0,
        }
        assert rich_values["LineItems"][0]["LineItemDays"] == {
            "_value": "------S",
            "_confidence": 0.8,
        }

    def test_synthetic_wrapper_around_single_field_is_unwrapped(self, service):
        """A wrapper holding ONE field is now unwrapped too.

        The old ``>= 2 confidence children`` rule dropped this element's
        confidence for exactly the reason the unwrap exists. The evidence
        (key absent from the value, contents naming a real field) does not
        depend on how many children the envelope has.
        """
        inference_result = {"LineItems": [{"LineItemRate": 450.0}]}
        confidence_scores = {
            "LineItems": [
                {
                    "Item_9": {"LineItemRate": {"confidence": 0.77, "geometry": []}},
                    "confidence_threshold": 0.9,
                }
            ]
        }

        rich_values = service._convert_to_rich_values(
            inference_result, confidence_scores
        )

        assert rich_values["LineItems"][0]["LineItemRate"] == {
            "_value": 450.0,
            "_confidence": 0.77,
        }

    def test_unmatched_single_key_is_left_alone(self, service):
        """Ambiguous case: a lone key that matches nothing on either side.

        It is absent from the value, but nothing inside it names a field of
        the value either - so unwrapping would recover no confidence and
        there is no evidence it is an envelope. Left as-is; the object simply
        scores without confidence, exactly as it did before.
        """
        inference_result = {"Alpha": "a", "Beta": "b"}
        confidence_scores = {
            "Mystery": {
                "Gamma": {"confidence": 0.5, "geometry": []},
                "Delta": {"confidence": 0.5, "geometry": []},
            }
        }

        rich_values = service._convert_to_rich_values(
            inference_result, confidence_scores
        )

        assert rich_values == {"Alpha": "a", "Beta": "b"}

    def test_lone_field_confidence_entry_is_not_treated_as_wrapper(self, service):
        """A leaf confidence entry is a field's own scores, not an envelope.

        Handled before the envelope check, but assert it: a single-field
        object whose one confidence entry is a leaf must still be annotated.
        """
        inference_result = {"Agency": "BUYING TIME, LLC"}
        confidence_scores = {
            "Agency": {"confidence": 0.99, "geometry": [], "confidence_threshold": 0.8}
        }

        rich_values = service._convert_to_rich_values(
            inference_result, confidence_scores
        )

        assert rich_values["Agency"] == {
            "_value": "BUYING TIME, LLC",
            "_confidence": 0.99,
        }

    def test_clean_null_descriptions(self, service):
        """Test that null descriptions are replaced with empty strings."""
        schema = {
            "$id": "Invoice",
            "type": "object",
            "properties": {
                "Agency": {"type": "string", "description": None},
                "ValidDescription": {
                    "type": "string",
                    "description": "A valid description",
                },
                "LineItems": {
                    "type": "array",
                    "description": None,
                    "items": {
                        "type": "object",
                        "properties": {
                            "Description": {"type": "string", "description": None}
                        },
                    },
                },
            },
            "$defs": {
                "SomeGroup": {
                    "type": "object",
                    "properties": {"Field1": {"type": "string", "description": None}},
                }
            },
        }

        from idp_common.evaluation.stickler_backend.model_factory import (
            clean_null_descriptions,
        )

        cleaned = clean_null_descriptions(schema)

        # Null descriptions should be replaced with empty strings
        assert cleaned["properties"]["Agency"]["description"] == ""
        assert cleaned["properties"]["LineItems"]["description"] == ""
        assert (
            cleaned["properties"]["LineItems"]["items"]["properties"]["Description"][
                "description"
            ]
            == ""
        )
        assert (
            cleaned["$defs"]["SomeGroup"]["properties"]["Field1"]["description"] == ""
        )

        # Valid descriptions should be unchanged
        assert (
            cleaned["properties"]["ValidDescription"]["description"]
            == "A valid description"
        )
