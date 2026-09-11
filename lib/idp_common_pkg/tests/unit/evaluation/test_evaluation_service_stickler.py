# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the Stickler-based evaluation service.

These tests focus on the public API and Stickler integration functionality.
"""

import warnings
from typing import Dict
from unittest.mock import MagicMock, patch

import pytest

from idp_common.evaluation.models import (
    AttributeEvaluationResult,
    SectionEvaluationResult,
)
from idp_common.evaluation.service import EvaluationService
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
class TestSticklerEvaluationService:
    """Tests for the Stickler-based EvaluationService class."""

    @pytest.fixture
    def mock_config(self):
        """Fixture providing a mock configuration with evaluation extensions."""
        return {
            "classes": [
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "invoice",
                    "x-aws-idp-document-type": "Invoice",
                    "x-aws-idp-evaluation-model-name": "Invoice",
                    "x-aws-idp-evaluation-match-threshold": 0.8,
                    "type": "object",
                    "description": "An invoice document",
                    "properties": {
                        "invoice_number": {
                            "type": "string",
                            "description": "The invoice number",
                            "x-aws-idp-evaluation-method": "EXACT",
                            "x-aws-idp-evaluation-weight": 3.0,
                        },
                        "invoice_date": {
                            "type": "string",
                            "description": "The invoice date",
                            "x-aws-idp-evaluation-method": "FUZZY",
                            "x-aws-idp-evaluation-threshold": 0.9,
                            "x-aws-idp-evaluation-weight": 1.5,
                        },
                        "total_amount": {
                            "type": "number",
                            "description": "The total amount",
                            "x-aws-idp-evaluation-method": "NUMERIC_EXACT",
                            "x-aws-idp-evaluation-threshold": 0.01,
                            "x-aws-idp-evaluation-weight": 2.0,
                        },
                    },
                }
            ],
            "evaluation": {
                "llm_method": {
                    "model": "anthropic.claude-3-sonnet-20240229-v1:0",
                    "temperature": 0.0,
                    "top_k": 5,
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
                classification="Invoice",
                page_ids=["1", "2"],
                extraction_result_uri="s3://input-bucket/test-document.pdf/sections/1/result.json",
            )
        )

        return doc

    def test_init(self, mock_config):
        """Test initialization with configuration."""
        service = EvaluationService(
            region="us-west-2", config=mock_config, max_workers=5
        )

        assert service.region == "us-west-2"
        assert service.max_workers == 5
        assert len(service.stickler_models) == 1
        assert "invoice" in service.stickler_models

    def test_stickler_model_creation(self, service):
        """Test that Stickler models are created correctly."""
        # Get Stickler model for invoice class
        model_class = service._get_stickler_model("Invoice")

        assert model_class is not None
        assert model_class.__name__ == "Invoice"

        # Test caching
        model_class_2 = service._get_stickler_model("Invoice")
        assert model_class is model_class_2  # Same instance from cache

    def test_top_level_match_threshold_applied_to_model_class(self):
        """R2: The document-level match_threshold in config must be applied to
        the built model class. `create_model` doesn't route through Stickler's
        ModelFactory, so without this the ClassVar default (0.7 in 0.5.0) would
        remain, silently overriding x-aws-idp-evaluation-match-threshold."""
        config = {
            "classes": [
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "form",
                    "x-aws-idp-document-type": "Form",
                    "x-aws-idp-evaluation-match-threshold": 0.85,
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                }
            ]
        }
        svc = EvaluationService(region="us-east-1", config=config, max_workers=1)
        model = svc._get_stickler_model("Form")
        assert model.match_threshold == 0.85

    def test_idp_llm_comparator_registered_via_public_api(self):
        """R5: IDPLLMComparator is registered under a distinct name in Stickler's
        registry (no private-dict rewrite of the built-in LLMComparator)."""
        # The service __init__ side-effect registers IDPLLMComparator
        # regardless of whether any class declares an LLM method.
        EvaluationService(region="us-east-1", config={"classes": []}, max_workers=1)
        from stickler.structured_object_evaluator.models.comparator_registry import (
            _global_registry,
        )

        assert _global_registry.is_registered("IDPLLMComparator")
        # The built-in stickler LLMComparator must not have been overwritten by
        # IDP's — before R5 the private-dict rewrite made
        # _global_registry.get("LLMComparator") return IDP's class. Now they
        # live under distinct names, so Stickler's built-in stays put.
        idp_cls = _global_registry.get("IDPLLMComparator")
        stickler_cls = _global_registry.get("LLMComparator")
        assert idp_cls is not stickler_cls
        assert idp_cls.__module__.startswith("idp_common.")
        assert stickler_cls.__module__.startswith("stickler.")

    def test_llm_config_per_service_no_shared_state(self):
        """R6: Two services with different LLM configs must not share state.

        The mapper surfaces ``evaluation.llm_method`` per-schema as
        ``x-aws-stickler-comparator-config`` so each service's LLM-method fields
        carry their own config. The previous module-level singleton would have
        overwritten svc_a's config with svc_b's.
        """
        config_a = {
            "classes": [
                {
                    "$id": "a",
                    "x-aws-idp-document-type": "A",
                    "type": "object",
                    "properties": {
                        "notes": {
                            "type": "string",
                            "x-aws-idp-evaluation-method": "LLM",
                        }
                    },
                }
            ],
            "evaluation": {"llm_method": {"model": "svc-a-model", "temperature": 0.1}},
        }
        config_b = {
            "classes": [
                {
                    "$id": "b",
                    "x-aws-idp-document-type": "B",
                    "type": "object",
                    "properties": {
                        "notes": {
                            "type": "string",
                            "x-aws-idp-evaluation-method": "LLM",
                        }
                    },
                }
            ],
            "evaluation": {"llm_method": {"model": "svc-b-model", "temperature": 0.9}},
        }
        svc_a = EvaluationService(region="us-east-1", config=config_a, max_workers=1)
        svc_b = EvaluationService(region="us-east-1", config=config_b, max_workers=1)

        cfg_a = svc_a.stickler_models["a"]["schema"]["properties"]["notes"][
            "x-aws-stickler-comparator-config"
        ]
        cfg_b = svc_b.stickler_models["b"]["schema"]["properties"]["notes"][
            "x-aws-stickler-comparator-config"
        ]
        # Only the service-specific keys need to differ — IDPConfig expands
        # llm_method with defaults for the rest, but the model + temperature
        # each service supplied must not have crossed over.
        assert cfg_a["model"] == "svc-a-model"
        assert cfg_a["temperature"] == 0.1
        assert cfg_b["model"] == "svc-b-model"
        assert cfg_b["temperature"] == 0.9

    def test_array_items_match_threshold_reaches_element_class(self):
        """R2: match_threshold on a structured array must reach the list
        element class (via the items schema), not stay as the ClassVar default."""
        import typing

        config = {
            "classes": [
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "inv",
                    "x-aws-idp-document-type": "Inv",
                    "type": "object",
                    "properties": {
                        "LineItems": {
                            "type": "array",
                            "x-aws-idp-evaluation-match-threshold": 0.55,
                            "items": {
                                "type": "object",
                                "properties": {"sku": {"type": "string"}},
                            },
                        }
                    },
                }
            ]
        }
        svc = EvaluationService(region="us-east-1", config=config, max_workers=1)
        model = svc._get_stickler_model("Inv")
        # Unwrap Optional[List[X]] -> X
        annotation = model.model_fields["LineItems"].annotation
        while typing.get_args(annotation):
            annotation = typing.get_args(annotation)[0]
        assert getattr(annotation, "match_threshold", None) == 0.55

    def test_stickler_model_not_found(self, service):
        """Test error when Stickler model not found for class."""
        with pytest.raises(ValueError, match="No schema configuration"):
            service._get_stickler_model("UnknownClass")

    @patch("idp_common.s3.get_json_content")
    def test_prepare_stickler_data(self, mock_get_json_content, service):
        """Test preparing data for Stickler."""
        # Test with inference_result wrapper
        mock_get_json_content.return_value = {
            "inference_result": {"invoice_number": "INV-123", "total_amount": 100.00},
            "explainability_info": [{"invoice_number": {"confidence": 0.95}}],
        }

        extraction_data, confidence_scores = service._prepare_stickler_data(
            "s3://bucket/path"
        )

        assert extraction_data == {"invoice_number": "INV-123", "total_amount": 100.00}
        assert "invoice_number" in confidence_scores

    def test_flatten_confidence_scores(self, service):
        """Test flattening confidence scores from assessment explainability format."""
        # Real-world confidence structure from assessment service with geometry and thresholds
        confidence_scores = {
            "Agency": {
                "confidence": 0.99,
                "geometry": [
                    {
                        "boundingBox": {
                            "top": 0.215,
                            "left": 0.15,
                            "width": 0.249,
                            "height": 0.014,
                        },
                        "page": 1,
                    }
                ],
                "confidence_threshold": 0.8,
            },
            "Advertiser": {
                "confidence": 1.0,
                "geometry": [
                    {
                        "boundingBox": {
                            "top": 0.183,
                            "left": 0.46,
                            "width": 0.158,
                            "height": 0.012,
                        },
                        "page": 1,
                    }
                ],
                "confidence_threshold": 0.8,
            },
            "GrossTotal": {
                "confidence": 0.99,
                "geometry": [
                    {
                        "boundingBox": {
                            "top": 0.725,
                            "left": 0.873,
                            "width": 0.07,
                            "height": 0.012,
                        },
                        "page": 2,
                    }
                ],
                "confidence_threshold": 0.8,
            },
            "LineItems": [
                {
                    "LineItemRate": {
                        "confidence": 1.0,
                        "geometry": [
                            {
                                "boundingBox": {
                                    "top": 0.47,
                                    "left": 0.754,
                                    "width": 0.052,
                                    "height": 0.013,
                                },
                                "page": 1,
                            }
                        ],
                        "confidence_threshold": 0.9,
                    },
                    "LineItemDays": {
                        "confidence": 0.8,
                        "geometry": [
                            {
                                "boundingBox": {
                                    "top": 0.47,
                                    "left": 0.5,
                                    "width": 0.05,
                                    "height": 0.013,
                                },
                                "page": 1,
                            }
                        ],
                        "confidence_threshold": 0.9,
                    },
                    "LineItemDescription": {
                        "confidence": 0.9,
                        "geometry": [
                            {
                                "boundingBox": {
                                    "top": 0.47,
                                    "left": 0.228,
                                    "width": 0.098,
                                    "height": 0.013,
                                },
                                "page": 1,
                            }
                        ],
                        "confidence_threshold": 0.9,
                    },
                },
                {
                    "LineItemRate": {
                        "confidence": 1.0,
                        "geometry": [
                            {
                                "boundingBox": {
                                    "top": 0.469,
                                    "left": 0.754,
                                    "width": 0.05,
                                    "height": 0.013,
                                },
                                "page": 1,
                            }
                        ],
                        "confidence_threshold": 0.9,
                    },
                    "LineItemDays": {
                        "confidence": 0.8,
                        "geometry": [
                            {
                                "boundingBox": {
                                    "top": 0.469,
                                    "left": 0.5,
                                    "width": 0.049,
                                    "height": 0.013,
                                },
                                "page": 1,
                            }
                        ],
                        "confidence_threshold": 0.9,
                    },
                },
            ],
        }

        # Test Rich Value conversion
        inference_result = {
            "Agency": "BUYING TIME, LLC",
            "Advertiser": "ACME CORP",
            "GrossTotal": 15185.0,
            "LineItems": [
                {
                    "LineItemRate": 600.0,
                    "LineItemDays": "MTWT---",
                    "LineItemDescription": "Ad spot",
                },
                {"LineItemRate": 500.0, "LineItemDays": "MTWT---"},
            ],
        }

        rich_values = service._convert_to_rich_values(
            inference_result, confidence_scores
        )

        # Verify top-level fields have rich values with confidence
        assert rich_values["Agency"] == {
            "_value": "BUYING TIME, LLC",
            "_confidence": 0.99,
        }
        assert rich_values["Advertiser"] == {"_value": "ACME CORP", "_confidence": 1.0}
        assert rich_values["GrossTotal"] == {"_value": 15185.0, "_confidence": 0.99}

        # Verify array items have rich values with confidence
        assert rich_values["LineItems"][0]["LineItemRate"] == {
            "_value": 600.0,
            "_confidence": 1.0,
        }
        assert rich_values["LineItems"][0]["LineItemDays"] == {
            "_value": "MTWT---",
            "_confidence": 0.8,
        }
        assert rich_values["LineItems"][0]["LineItemDescription"] == {
            "_value": "Ad spot",
            "_confidence": 0.9,
        }
        assert rich_values["LineItems"][1]["LineItemRate"] == {
            "_value": 500.0,
            "_confidence": 1.0,
        }
        assert rich_values["LineItems"][1]["LineItemDays"] == {
            "_value": "MTWT---",
            "_confidence": 0.8,
        }

    def test_flatten_confidence_scores_with_wrapper_keys(self, service):
        """Test flattening confidence scores when array elements have wrapper keys.

        This reproduces the production scenario where explainability_info contains
        wrapper keys like 'Item #6' that don't exist in the inference_result.
        """
        # Production data structure: LineItems[5] has a wrapper key 'Item #6'
        confidence_scores = {
            "Agency": {
                "confidence": 0.99,
                "geometry": [
                    {
                        "boundingBox": {
                            "top": 0.215,
                            "left": 0.15,
                            "width": 0.249,
                            "height": 0.014,
                        },
                        "page": 1,
                    }
                ],
                "confidence_threshold": 0.8,
            },
            "LineItems": [
                {
                    "LineItemRate": {
                        "confidence": 1.0,
                        "geometry": [],
                        "confidence_threshold": 0.9,
                    },
                    "LineItemDays": {
                        "confidence": 0.8,
                        "geometry": [],
                        "confidence_threshold": 0.9,
                    },
                },
                {
                    "LineItemRate": {
                        "confidence": 1.0,
                        "geometry": [],
                        "confidence_threshold": 0.9,
                    },
                    "LineItemDays": {
                        "confidence": 0.8,
                        "geometry": [],
                        "confidence_threshold": 0.9,
                    },
                },
                {
                    "LineItemRate": {
                        "confidence": 1.0,
                        "geometry": [],
                        "confidence_threshold": 0.9,
                    },
                    "LineItemDays": {
                        "confidence": 0.8,
                        "geometry": [],
                        "confidence_threshold": 0.9,
                    },
                },
                {
                    "LineItemRate": {
                        "confidence": 1.0,
                        "geometry": [],
                        "confidence_threshold": 0.9,
                    },
                    "LineItemDays": {
                        "confidence": 0.8,
                        "geometry": [],
                        "confidence_threshold": 0.9,
                    },
                },
                {
                    "LineItemRate": {
                        "confidence": 1.0,
                        "geometry": [],
                        "confidence_threshold": 0.9,
                    },
                    "LineItemDays": {
                        "confidence": 0.8,
                        "geometry": [],
                        "confidence_threshold": 0.9,
                    },
                },
                # Element 5: Has wrapper key 'Item #6'
                {
                    "Item #6": {
                        "LineItemRate": {
                            "confidence": 1.0,
                            "geometry": [],
                            "confidence_threshold": 0.9,
                        },
                        "LineItemDays": {
                            "confidence": 0.8,
                            "geometry": [],
                            "confidence_threshold": 0.9,
                        },
                    }
                },
            ],
        }

        # Inference result: Clean data without wrapper keys
        inference_result = {
            "Agency": "BUYING TIME, LLC",
            "LineItems": [
                {"LineItemRate": 1000.0, "LineItemDays": "MTWT---"},
                {"LineItemRate": 1000.0, "LineItemDays": "MTWT---"},
                {"LineItemRate": 1000.0, "LineItemDays": "MTWT---"},
                {"LineItemRate": 1000.0, "LineItemDays": "MTWT---"},
                {"LineItemRate": 1000.0, "LineItemDays": "MTWT---"},
                {"LineItemRate": 1000.0, "LineItemDays": "------S"},  # Element 5
            ],
        }

        # This should handle the wrapper key scenario without KeyError
        rich_values = service._convert_to_rich_values(
            inference_result, confidence_scores
        )

        # Verify top-level field
        assert rich_values["Agency"] == {
            "_value": "BUYING TIME, LLC",
            "_confidence": 0.99,
        }

        # Verify clean array elements (0-4)
        for i in range(5):
            assert rich_values["LineItems"][i]["LineItemRate"]["_value"] == 1000.0
            assert rich_values["LineItems"][i]["LineItemRate"]["_confidence"] == 1.0
            assert rich_values["LineItems"][i]["LineItemDays"]["_confidence"] == 0.8

        # Verify element 5 with wrapper key - should unwrap automatically
        assert rich_values["LineItems"][5]["LineItemRate"]["_value"] == 1000.0
        assert rich_values["LineItems"][5]["LineItemRate"]["_confidence"] == 1.0
        assert rich_values["LineItems"][5]["LineItemDays"]["_value"] == "------S"
        assert rich_values["LineItems"][5]["LineItemDays"]["_confidence"] == 0.8

    def test_production_scenario_exact_data(self, service):
        """Test with exact production data that caused KeyError: 0.

        Uses actual data structure from production S3 file where evaluation failed.
        """
        # Exact production confidence structure (explainability_info is a list with one element)
        explainability_info = [
            {
                "Agency": {
                    "confidence": 0.99,
                    "geometry": [
                        {
                            "boundingBox": {
                                "top": 0.215,
                                "left": 0.15,
                                "width": 0.249,
                                "height": 0.014,
                            },
                            "page": 1,
                        }
                    ],
                    "confidence_threshold": 0.8,
                },
                "Advertiser": {
                    "confidence": 1.0,
                    "geometry": [
                        {
                            "boundingBox": {
                                "top": 0.183,
                                "left": 0.46,
                                "width": 0.158,
                                "height": 0.012,
                            },
                            "page": 1,
                        }
                    ],
                    "confidence_threshold": 0.8,
                },
                "GrossTotal": {
                    "confidence": 0.99,
                    "geometry": [
                        {
                            "boundingBox": {
                                "top": 0.725,
                                "left": 0.873,
                                "width": 0.07,
                                "height": 0.012,
                            },
                            "page": 2,
                        }
                    ],
                    "confidence_threshold": 0.8,
                },
                "LineItems": [
                    {
                        "LineItemRate": {
                            "confidence": 1.0,
                            "geometry": [
                                {
                                    "boundingBox": {
                                        "top": 0.47,
                                        "left": 0.754,
                                        "width": 0.052,
                                        "height": 0.013,
                                    },
                                    "page": 1,
                                }
                            ],
                            "confidence_threshold": 0.9,
                        },
                        "LineItemDays": {
                            "confidence": 0.8,
                            "geometry": [
                                {
                                    "boundingBox": {
                                        "top": 0.47,
                                        "left": 0.5,
                                        "width": 0.05,
                                        "height": 0.013,
                                    },
                                    "page": 1,
                                }
                            ],
                            "confidence_threshold": 0.9,
                        },
                        "LineItemDescription": {
                            "confidence": 0.9,
                            "geometry": [
                                {
                                    "boundingBox": {
                                        "top": 0.47,
                                        "left": 0.228,
                                        "width": 0.098,
                                        "height": 0.013,
                                    },
                                    "page": 1,
                                }
                            ],
                            "confidence_threshold": 0.9,
                        },
                    },
                    {
                        "LineItemRate": {
                            "confidence": 1.0,
                            "geometry": [],
                            "confidence_threshold": 0.9,
                        },
                        "LineItemDays": {
                            "confidence": 0.8,
                            "geometry": [],
                            "confidence_threshold": 0.9,
                        },
                    },
                    {
                        "LineItemRate": {
                            "confidence": 1.0,
                            "geometry": [],
                            "confidence_threshold": 0.9,
                        },
                        "LineItemDays": {
                            "confidence": 0.8,
                            "geometry": [],
                            "confidence_threshold": 0.9,
                        },
                    },
                    {
                        "LineItemRate": {
                            "confidence": 1.0,
                            "geometry": [],
                            "confidence_threshold": 0.9,
                        },
                        "LineItemDays": {
                            "confidence": 0.8,
                            "geometry": [],
                            "confidence_threshold": 0.9,
                        },
                    },
                    {
                        "LineItemRate": {
                            "confidence": 1.0,
                            "geometry": [],
                            "confidence_threshold": 0.9,
                        },
                        "LineItemDays": {
                            "confidence": 0.8,
                            "geometry": [],
                            "confidence_threshold": 0.9,
                        },
                    },
                    {
                        "Item #6": {
                            "LineItemRate": {
                                "confidence": 1.0,
                                "geometry": [],
                                "confidence_threshold": 0.9,
                            },
                            "LineItemDays": {
                                "confidence": 0.8,
                                "geometry": [],
                                "confidence_threshold": 0.9,
                            },
                        }
                    },
                ],
            }
        ]

        # Exact production inference_result
        inference_result = {
            "Agency": "BUYING TIME, LLC",
            "Advertiser": "ACME CORP",
            "GrossTotal": 15185.0,
            "LineItems": [
                {
                    "LineItemRate": 1000.0,
                    "LineItemDays": "MTWT---",
                    "LineItemDescription": "Ad spot",
                },
                {"LineItemRate": 1000.0, "LineItemDays": "MTWT---"},
                {"LineItemRate": 1000.0, "LineItemDays": "MTWT---"},
                {"LineItemRate": 1000.0, "LineItemDays": "MTWT---"},
                {"LineItemRate": 1000.0, "LineItemDays": "MTWT---"},
                {"LineItemRate": 1000.0, "LineItemDays": "------S"},
            ],
        }

        # This should NOT raise KeyError: 0
        # Pass the unwrapped dict (mimicking what _prepare_stickler_data does)
        rich_values = service._convert_to_rich_values(
            inference_result, explainability_info[0]
        )

        # Verify structure is correct
        assert rich_values["Agency"]["_value"] == "BUYING TIME, LLC"
        assert rich_values["Agency"]["_confidence"] == 0.99
        assert len(rich_values["LineItems"]) == 6
        assert rich_values["LineItems"][5]["LineItemRate"]["_value"] == 1000.0

    def test_get_nested_value(self, service):
        """Test getting nested values from Stickler model instances."""
        # Create a mock object with nested attributes
        mock_obj = MagicMock()
        mock_obj.invoice_number = "INV-123"
        mock_obj.address = MagicMock()
        mock_obj.address.city = "Seattle"

        # Test simple attribute
        value = service._get_nested_value(mock_obj, "invoice_number")
        assert value == "INV-123"

        # Test nested attribute
        value = service._get_nested_value(mock_obj, "address.city")
        assert value == "Seattle"

        # Test with real dict (not MagicMock which always returns values)
        dict_obj = {"invoice_number": "INV-123", "address": {"city": "Seattle"}}

        # Test non-existent attribute with dict
        value = service._get_nested_value(dict_obj, "nonexistent")
        assert value is None

    def test_resolve_leaf_schema(self, service):
        """Leaf schema resolution follows array items and object properties."""
        from idp_common.evaluation.stickler_backend.results import resolve_leaf_schema

        field_schema = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "Amount": {"x-aws-stickler-comparator": "NumericComparator"},
                    "bankInfo": {
                        "type": "object",
                        "properties": {
                            "bank": {"x-aws-stickler-comparator": "FuzzyComparator"},
                        },
                    },
                },
            },
        }

        # Flat list-item field
        leaf = resolve_leaf_schema(field_schema, "LineItems[0].Amount")
        assert leaf is not None
        assert leaf["x-aws-stickler-comparator"] == "NumericComparator"

        # Deeply nested object field within a list item
        leaf = resolve_leaf_schema(field_schema, "LineItems[1].bankInfo.bank")
        assert leaf is not None
        assert leaf["x-aws-stickler-comparator"] == "FuzzyComparator"

        # Unknown field resolves to None
        assert resolve_leaf_schema(field_schema, "LineItems[0].Missing") is None

    def test_annotate_nested_comparison_methods(self, service):
        """Nested comparisons get per-field evaluation_method and weight."""
        from idp_common.evaluation.service import _format_evaluation_method
        from idp_common.evaluation.stickler_backend.results import (
            annotate_nested_comparison_methods,
        )

        field_schema = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "Description": {
                        "x-aws-stickler-comparator": "FuzzyComparator",
                        "x-aws-stickler-threshold": 0.9,
                        "x-aws-stickler-weight": 2.0,
                    },
                    "Rate": {
                        "x-aws-stickler-comparator": "NumericComparator",
                        # no weight configured -> defaults to 1.0
                    },
                },
            },
        }
        comparisons = [
            {
                "expected_key": "LineItems[0].Description",
                "expected_value": "Widget",
                "actual_value": "Widgets",
            },
            {
                "expected_key": "LineItems[0].Rate",
                "expected_value": 10.0,
                "actual_value": 10.0,
            },
        ]

        annotate_nested_comparison_methods(
            comparisons,
            field_schema=field_schema,
            match_threshold=0.8,
            format_evaluation_method=_format_evaluation_method,
        )

        assert comparisons[0]["evaluation_method"] == "Fuzzy (threshold: 0.90)"
        assert comparisons[0]["weight"] == 2.0
        assert comparisons[1]["evaluation_method"] == "NumericExact"
        assert comparisons[1]["weight"] == 1.0  # default

    def test_bare_method_annotation_shows_applied_threshold(self):
        """Fix D: bare FUZZY (no evaluation-threshold) displays Stickler's
        applied 0.5, not the old hardcoded 0.7 fallback.

        The operator wrote ``x-aws-idp-evaluation-method: FUZZY`` and no
        ``evaluation-threshold``. The mapper translates to
        ``x-aws-stickler-comparator: FuzzyComparator`` with no
        ``x-aws-stickler-threshold``. Stickler's JSON-schema converter then
        constructs ``ComparableField(threshold=0.5)`` (its own hardcoded
        default), so scoring uses 0.5 and the reason string reads
        ``"below threshold (X < 0.5)"``. Before Fix D, the Method column
        showed ``"Fuzzy (threshold: 0.70)"`` because ``_format_evaluation_method``
        guessed 0.7 from a hardcoded per-method table (now removed). Fix D
        reads ``model_fields[...].json_schema_extra._threshold`` (== 0.5)
        instead, so the Method column matches Stickler's applied value.
        """
        from pydantic import create_model
        from stickler import StructuredModel
        from stickler.structured_object_evaluator.models.json_schema_field_converter import (  # noqa: E501
            JsonSchemaFieldConverter,
        )

        from idp_common.evaluation.service import _format_evaluation_method
        from idp_common.evaluation.stickler_backend.mapper import SticklerConfigMapper
        from idp_common.evaluation.stickler_backend.results import (
            annotate_nested_comparison_methods,
            applied_threshold_from_field_info,
            resolve_leaf_model_field,
        )

        # Build a schema where LineItems[].Description is FUZZY with NO threshold.
        idp_schema = {
            "$id": "Bill",
            "x-aws-idp-document-type": "Bill",
            "type": "object",
            "properties": {
                "LineItems": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "Description": {
                                "type": "string",
                                "x-aws-idp-evaluation-method": "FUZZY",
                                # deliberately no x-aws-idp-evaluation-threshold
                            },
                            "Amount": {
                                "type": "number",
                                "x-aws-idp-evaluation-method": "NUMERIC_EXACT",
                                "x-aws-idp-evaluation-threshold": 0.01,
                            },
                        },
                    },
                },
            },
        }
        translated = SticklerConfigMapper.build_stickler_model_config(idp_schema)[
            "schema"
        ]
        conv = JsonSchemaFieldConverter(translated)
        fields = conv.convert_properties_to_fields(
            translated.get("properties", {}), translated.get("required", [])
        )
        BillModel = create_model(  # type: ignore  # pyright: reportCallIssue=false
            "Bill", **fields, __base__=StructuredModel
        )

        # Sanity: resolve_leaf_model_field walks into List[LineItem] and reads
        # _threshold off the leaf FieldInfo. 0.5 == Stickler's applied value.
        leaf = resolve_leaf_model_field(BillModel, "LineItems[0].Description")
        assert leaf is not None, "resolve_leaf_model_field did not reach the leaf"
        assert applied_threshold_from_field_info(leaf) == pytest.approx(0.5), (
            "_threshold on the model should be Stickler's applied 0.5, not 0.7"
        )

        # Run annotate_nested_comparison_methods with the model class threaded
        # through — this is the Fix D wiring in transform_stickler_result.
        # field_schema is the translated LineItems array schema; leaf lookup
        # for threshold now prefers the model over the (absent) schema
        # extension.
        line_items_schema = translated["properties"]["LineItems"]
        comparisons = [
            {
                "expected_key": "LineItems[0].Description",
                "expected_value": "Widget",
                "actual_value": "Something else entirely",
            },
            {
                "expected_key": "LineItems[0].Amount",
                "expected_value": 10.0,
                "actual_value": 10.0,
            },
        ]
        annotate_nested_comparison_methods(
            comparisons,
            field_schema=line_items_schema,
            match_threshold=0.8,
            format_evaluation_method=_format_evaluation_method,
            root_model_cls=BillModel,
        )
        # The whole point of Fix D: 0.50, not 0.70.
        assert comparisons[0]["evaluation_method"] == "Fuzzy (threshold: 0.50)"
        # NumericExact never shows a threshold suffix, so it's unchanged.
        assert comparisons[1]["evaluation_method"] == "NumericExact"

    def test_bare_method_backward_compat_without_model_cls(self):
        """Regression: with ``root_model_cls=None`` the schema-only fallback
        path is used (the previous behavior). Ensures Fix D didn't break
        callers that don't thread the model class through.
        """
        from idp_common.evaluation.service import _format_evaluation_method
        from idp_common.evaluation.stickler_backend.results import (
            annotate_nested_comparison_methods,
        )

        field_schema = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "Description": {
                        "x-aws-stickler-comparator": "FuzzyComparator",
                        "x-aws-stickler-threshold": 0.85,
                    },
                },
            },
        }
        comparisons = [
            {
                "expected_key": "LineItems[0].Description",
                "expected_value": "A",
                "actual_value": "B",
            }
        ]
        annotate_nested_comparison_methods(
            comparisons,
            field_schema=field_schema,
            match_threshold=0.8,
            format_evaluation_method=_format_evaluation_method,
            # root_model_cls omitted — legacy schema-only lookup.
        )
        assert comparisons[0]["evaluation_method"] == "Fuzzy (threshold: 0.85)"

    def test_generate_reason(self, service):
        """Test reason generation."""
        # Test exact match
        reason = service._generate_reason("field", "val", "val", 1.0, True, "Exact")
        assert "Exact match" in reason

        # Test partial match
        reason = service._generate_reason("field", "val1", "val2", 0.85, True, "Fuzzy")
        assert "above threshold" in reason

        # Test no match
        reason = service._generate_reason("field", "val1", "val2", 0.5, False, "Exact")
        assert "do not match" in reason

        # Test both empty
        reason = service._generate_reason("field", None, None, 1.0, True, "Exact")
        assert "empty" in reason.lower()

    @patch("idp_common.s3.write_content")
    @patch("idp_common.evaluation.service.EvaluationService._process_section")
    def test_evaluate_document_api(
        self, mock_process_section, mock_write_content, service, sample_document
    ):
        """Test the public evaluate_document API."""
        # Create expected document
        expected_document = sample_document

        # Configure mock for _process_section
        section_result = SectionEvaluationResult(
            section_id="1",
            document_class="Invoice",
            attributes=[
                AttributeEvaluationResult(
                    name="invoice_number",
                    expected="INV-123",
                    actual="INV-123",
                    matched=True,
                    score=1.0,
                    evaluation_method="STICKLER",
                    weight=3.0,
                )
            ],
            metrics={"precision": 1.0, "recall": 1.0, "f1_score": 1.0},
        )

        mock_process_section.return_value = (
            section_result,
            {"tp": 1, "fp": 0, "fn": 0, "tn": 0, "fp1": 0, "fp2": 0},
        )

        # Evaluate document. Document-level metrics are now derived inline
        # from the summed Stickler counts (R10 removed the calculate_metrics
        # helper module the previous test used to patch).
        result = service.evaluate_document(
            actual_document=sample_document,
            expected_document=expected_document,
            store_results=True,
        )

        # Verify API contract
        assert result.id == "test-doc"
        assert result.status == Status.COMPLETED
        assert result.evaluation_report_uri is not None
        assert result.evaluation_results_uri is not None
        assert result.evaluation_result is not None

        # Verify Stickler enhancements
        assert result.evaluation_result.section_results[0].attributes[0].weight == 3.0

    @patch("idp_common.s3.write_content")
    @patch("idp_common.evaluation.service.EvaluationService._process_section")
    def test_evaluate_document_error_handling(
        self, mock_process_section, mock_write_content, service, sample_document
    ):
        """Test error handling in evaluate_document."""
        expected_document = sample_document

        # Configure mock to raise exception
        mock_process_section.side_effect = Exception("Test error")

        # Evaluate document
        result = service.evaluate_document(
            actual_document=sample_document, expected_document=expected_document
        )

        # Check error was captured
        assert len(result.errors) > 0
        assert "Test error" in result.errors[0]

    def test_evaluate_section_with_stickler(self, service):
        """Test evaluate_section with Stickler comparison."""
        section = Section(section_id="1", classification="Invoice", page_ids=["1"])

        expected_results = {
            "invoice_number": "INV-123",
            "invoice_date": "2023-05-08",
            "total_amount": 100.00,
        }

        actual_results = {
            "invoice_number": "INV-123",
            "invoice_date": "2023-05-08",
            "total_amount": 100.00,
        }

        # Mock Stickler model and comparison
        with patch.object(service, "_get_stickler_model") as mock_get_model:
            # Create mock Stickler model
            mock_model_class = MagicMock()
            mock_instance = MagicMock()

            # Configure comparison result
            mock_instance.compare_with.return_value = {
                "overall_score": 1.0,
                "field_scores": {
                    "invoice_number": 1.0,
                    "invoice_date": 1.0,
                    "total_amount": 1.0,
                },
                "match": True,
            }

            mock_model_class.return_value = mock_instance
            mock_get_model.return_value = mock_model_class

            # Mock _get_nested_value to return the values
            with patch.object(service, "_get_nested_value") as mock_nested:

                def nested_side_effect(obj, field_name):
                    if "expected" in str(obj):
                        return expected_results.get(field_name)
                    return actual_results.get(field_name)

                mock_nested.side_effect = nested_side_effect

                # Evaluate section
                result = service.evaluate_section(
                    section=section,
                    expected_results=expected_results,
                    actual_results=actual_results,
                )

                # Verify result
                assert result.section_id == "1"
                assert result.document_class == "Invoice"
                assert len(result.attributes) == 3

                # Verify Stickler was used
                mock_get_model.assert_called_once()
                mock_instance.compare_with.assert_called_once()

    @pytest.fixture
    def nested_optional_config(self):
        """Config with a nested object whose fields have no 'required' array.

        Mirrors real auto/manual schemas (e.g. URLA) where every field is
        optional and may be None.
        """
        return {
            "classes": [
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "form",
                    "x-aws-idp-document-type": "Form",
                    "type": "object",
                    "properties": {
                        "Contact": {"$ref": "#/$defs/Contact"},
                    },
                    "$defs": {
                        "Contact": {
                            "type": "object",
                            "properties": {
                                "HomePhone": {"type": "string"},
                                "WorkPhone": {"type": "string"},
                                "Email": {"type": "string"},
                            },
                        }
                    },
                }
            ]
        }

    def test_evaluate_section_none_in_nested_optional_with_confidence(
        self, nested_optional_config
    ):
        """Regression: None values in nested optional fields must not fail eval.

        Stickler's JsonSchemaFieldConverter builds optional fields with a None
        default but a non-nullable annotation (e.g. ``str``). The confidence
        path round-trips data through ``from_json``/``model_dump``, which
        materializes None for missing fields and re-validates, previously
        raising "Input should be a valid string [input_value=None]" and
        surfacing as a misleading "Schema configuration error". The service
        widens every field to Optional so this path succeeds.
        """
        service = EvaluationService(
            region="us-west-2", config=nested_optional_config, max_workers=1
        )
        section = Section(section_id="1", classification="Form", page_ids=["1"])

        # WorkPhone is None (not extracted) in both baseline and prediction
        expected = {
            "Contact": {"HomePhone": "555-1", "WorkPhone": None, "Email": "a@b.c"}
        }
        actual = {
            "Contact": {"HomePhone": "555-1", "WorkPhone": None, "Email": "a@b.c"}
        }
        confidence = {
            "Contact": {
                "HomePhone": {"confidence": 0.95},
                "Email": {"confidence": 0.95},
            }
        }

        result = service.evaluate_section(
            section=section,
            expected_results=expected,
            actual_results=actual,
            confidence_scores=confidence,
        )

        # Must not be flagged as a failed evaluation
        assert not result.metrics.get("evaluation_failed")
        assert result.document_class == "Form"

    def test_single_object_property_confidence_reaches_stickler(
        self, nested_optional_config
    ):
        """Regression (#713): confidence must survive to Stickler's scoring.

        ``Contact`` is this class's only top-level property, so the old
        key-count wrapper heuristic unwrapped it and the rich-value conversion
        emitted plain values. Stickler therefore saw no confidence at all:
        ``prediction_confidences`` was absent and every calibration metric
        (AUROC / ECE / Brier) came back ``None`` with coverage 0.0 - an
        unmeasurable section that looked merely uninteresting.
        """
        service = EvaluationService(
            region="us-west-2", config=nested_optional_config, max_workers=1
        )
        section = Section(section_id="1", classification="Form", page_ids=["1"])

        expected = {"Contact": {"HomePhone": "555-1", "Email": "a@b.c"}}
        # Email is wrong and was low-confidence: a well-calibrated prediction,
        # which is only visible if the confidence reaches Stickler.
        actual = {"Contact": {"HomePhone": "555-1", "Email": "wrong@example.com"}}
        confidence = {
            "Contact": {
                "HomePhone": {"confidence": 0.95},
                "Email": {"confidence": 0.20},
            }
        }

        result = service.evaluate_section(
            section=section,
            expected_results=expected,
            actual_results=actual,
            confidence_scores=confidence,
        )

        blob = result.stickler_comparison_result or {}
        assert blob.get("prediction_confidences") == {
            "Contact.HomePhone": 0.95,
            "Contact.Email": 0.20,
        }

        # Calibration is computable rather than empty.
        coverage = blob["confidence_metrics"]["coverage"]
        assert coverage["fields_with_confidence"] == 2
        assert coverage["ratio"] > 0
        assert blob["confidence_metrics"]["overall"]["brier_score"]["value"] is not None

    def test_evaluate_section_blast_radius_limited_to_bad_field(self):
        """A single unparseable field must not zero out the whole section.

        The tolerant build drops only the offending field (from both sides) and
        still evaluates the rest, instead of raising and failing every attribute.
        """
        config = {
            "classes": [
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "t",
                    "x-aws-idp-document-type": "T",
                    "type": "object",
                    "properties": {
                        "Good": {"type": "string"},
                        # numeric field; a dict value can't be coerced and will
                        # fail Pydantic validation
                        "Amount": {"type": "number"},
                    },
                }
            ]
        }
        service = EvaluationService(region="us-west-2", config=config, max_workers=1)
        section = Section(section_id="1", classification="T", page_ids=["1"])

        expected = {"Good": "hello", "Amount": {"nested": "oops"}}
        actual = {"Good": "hello", "Amount": {"nested": "oops2"}}

        result = service.evaluate_section(section, expected, actual, None)

        # Section did NOT hard-fail; the good field was still scored
        assert not result.metrics.get("evaluation_failed")
        good = next((a for a in result.attributes if a.name == "Good"), None)
        assert good is not None and good.matched
        # The bad field is reported as skipped, not silently dropped
        assert result.metrics.get("skipped_field_count") == 1
        assert any(a.name.startswith("__SKIPPED__") for a in result.attributes)

    def test_drop_field_at_path_nested_and_list(self):
        """_drop_field_at_path removes the right leaf for dict and list paths."""
        service = EvaluationService(
            region="us-west-2", config={"classes": []}, max_workers=1
        )
        data = {
            "A": {"B": {"C": "x", "D": "y"}},
            "Items": [{"k": 1}, {"k": 2}],
        }

        # Nested dict path
        out = service._drop_field_at_path(data, ("A", "B", "C"))
        assert "C" not in out["A"]["B"]
        assert out["A"]["B"]["D"] == "y"
        assert data["A"]["B"]["C"] == "x"  # original untouched (deep copy)

        # List index path
        out2 = service._drop_field_at_path(data, ("Items", 0, "k"))
        assert "k" not in out2["Items"][0]
        assert out2["Items"][1]["k"] == 2

        # Non-existent path is a no-op
        out3 = service._drop_field_at_path(data, ("Nope", "Missing"))
        assert out3 == data


@pytest.mark.unit
def test_section_evaluation_result_includes_stickler_comparison():
    """Test that SectionEvaluationResult can store stickler_comparison_result."""
    stickler_result = {
        "overall_score": 0.85,
        "confusion_matrix": {
            "overall": {"tp": 5, "fp": 1, "tn": 2, "fn": 1},
            "fields": {"field1": {"tp": 3, "fp": 0, "tn": 1, "fn": 0}},
        },
    }

    section_result = SectionEvaluationResult(
        section_id="1",
        document_class="Invoice",
        attributes=[],
        metrics={"accuracy": 0.85},
        stickler_comparison_result=stickler_result,
    )

    assert section_result.stickler_comparison_result is not None
    assert section_result.stickler_comparison_result["overall_score"] == 0.85
    assert "confusion_matrix" in section_result.stickler_comparison_result


@pytest.mark.unit
def test_section_evaluation_result_optional_stickler_comparison():
    """Test that stickler_comparison_result is optional."""
    section_result = SectionEvaluationResult(
        section_id="1",
        document_class="Invoice",
        attributes=[],
        metrics={"accuracy": 0.85},
    )

    assert section_result.stickler_comparison_result is None


@pytest.mark.unit
def test_document_evaluation_serializes_stickler_comparison():
    """Test that DocumentEvaluationResult.to_dict() includes stickler_comparison_result."""
    from idp_common.evaluation.models import DocumentEvaluationResult

    stickler_result = {
        "overall_score": 0.85,
        "confusion_matrix": {"overall": {"tp": 5, "fp": 1}},
    }

    attr_result = AttributeEvaluationResult(
        name="field1",
        expected="value1",
        actual="value1",
        matched=True,
        score=1.0,
        reason="Exact match",
    )

    section_result = SectionEvaluationResult(
        section_id="1",
        document_class="Invoice",
        attributes=[attr_result],
        metrics={"accuracy": 0.85},
        stickler_comparison_result=stickler_result,
    )

    doc_result = DocumentEvaluationResult(
        document_id="test-doc",
        section_results=[section_result],
        overall_metrics={"accuracy": 0.85},
    )

    result_dict = doc_result.to_dict()

    assert "section_results" in result_dict
    assert len(result_dict["section_results"]) == 1
    assert "stickler_comparison_result" in result_dict["section_results"][0]
    assert (
        result_dict["section_results"][0]["stickler_comparison_result"]
        == stickler_result
    )


@pytest.mark.unit
def test_document_rollup_weight_does_not_double_count_fps():
    """Regression: field-count rollup weight must not double-count FPs.

    Stickler's invariant: ``fp == fa + fd`` (every FA row is ``{fa:1, fp:1}``;
    every FD row is ``{fd:1, fp:1}``). An earlier implementation summed
    ``tp + fa + fd + fp + tn + fn`` for the section-weight in the document
    rollup, which counted every FP twice and biased ``weighted_overall_score``
    toward low-scoring sections with more errors.

    Drives ``evaluate_document`` with two sections whose Stickler counts have
    FPs, asserts the rollup weight equals ``tp + fp + tn + fn`` (i.e. each
    real field counted once).
    """
    config = {
        "classes": [
            {
                "$id": "t",
                "x-aws-idp-document-type": "T",
                "type": "object",
                "properties": {"a": {"type": "string"}},
            }
        ]
    }
    svc = EvaluationService(region="us-east-1", config=config, max_workers=1)

    # Section A: tp=3, fa=1, fd=1, fp=2, tn=0, fn=0 -> 5 real fields
    section_a = SectionEvaluationResult(
        section_id="1",
        document_class="T",
        attributes=[],
        metrics={
            "weighted_overall_score": 0.6,
            "_stickler_counts": {
                "tp": 3,
                "fa": 1,
                "fd": 1,
                "fp": 2,
                "tn": 0,
                "fn": 0,
            },
        },
    )
    # Section B: tp=4, fp=0, tn=1, fn=1 -> 6 real fields
    section_b = SectionEvaluationResult(
        section_id="2",
        document_class="T",
        attributes=[],
        metrics={
            "weighted_overall_score": 0.9,
            "_stickler_counts": {
                "tp": 4,
                "fa": 0,
                "fd": 0,
                "fp": 0,
                "tn": 1,
                "fn": 1,
            },
        },
    )

    doc = Document(
        id="doc-fp-rollup",
        input_key="doc.pdf",
        input_bucket="in-bucket",
        output_bucket="out-bucket",
        status=Status.EXTRACTING,
    )
    doc.sections.append(Section(section_id="1", classification="T", page_ids=["1"]))
    doc.sections.append(Section(section_id="2", classification="T", page_ids=["2"]))

    def fake_process_section(actual_section, expected_section, document_context=""):
        if actual_section.section_id == "1":
            return section_a, {
                "tp": 3,
                "fp": 2,
                "fn": 0,
                "tn": 0,
                "fp1": 1,
                "fp2": 1,
            }
        return section_b, {
            "tp": 4,
            "fp": 0,
            "fn": 1,
            "tn": 1,
            "fp1": 0,
            "fp2": 0,
        }

    with patch.object(svc, "_process_section", side_effect=fake_process_section):
        with patch("idp_common.s3.write_content"):
            result = svc.evaluate_document(doc, doc, store_results=False)

    # Expected weight per section (fp counted ONCE, not doubled):
    #   A: tp=3 + fp=2 + tn=0 + fn=0 = 5   (bug would give 3+1+1+2+0+0 = 7)
    #   B: tp=4 + fp=0 + tn=1 + fn=1 = 6
    # Weighted mean: (0.6*5 + 0.9*6) / (5 + 6) = (3.0 + 5.4) / 11 ~= 0.76364
    # Under the bug: (0.6*7 + 0.9*6) / (7 + 6) = (4.2 + 5.4) / 13 ~= 0.73846
    expected_weighted = (0.6 * 5 + 0.9 * 6) / (5 + 6)
    actual_weighted = result.evaluation_result.overall_metrics["weighted_overall_score"]
    assert abs(actual_weighted - expected_weighted) < 1e-9, (
        f"weighted_overall_score={actual_weighted!r} does not match expected "
        f"{expected_weighted!r} — FP double-counting regression?"
    )


# ---------------------------------------------------------------------------
# Regression: sections whose class has no extractable schema must not be
# scored as 0.0. They should be flagged ``evaluation_skipped=True`` and drop
# out of the document- and run-level weighted means entirely. Previously such
# sections dragged the weighted score to 0.0, producing the 0.0-0.1 histogram
# spike users saw for test sets that mixed real classes with no-op classes
# (OtherDocument / DeliveryNote where only Invoice had a schema).
# ---------------------------------------------------------------------------


def _invoice_only_config():
    """Config used by the no-op-exclusion tests: only ``Invoice`` has a schema."""
    return {
        "classes": [
            {
                "$id": "invoice",
                "x-aws-idp-document-type": "Invoice",
                "type": "object",
                "properties": {
                    "invoice_number": {"type": "string"},
                    "total_amount": {"type": "number"},
                },
            }
        ]
    }


@pytest.mark.unit
def test_evaluate_section_class_not_in_config_and_no_expected_is_skipped():
    """A class absent from the config with no expected data is a scoring no-op.

    Previously ``_get_stickler_model`` raised ``ValueError`` and the caller
    returned a 0.0-scored ``SectionEvaluationResult``. That 0.0 dragged the
    document-level weighted mean down for docs whose sections were all
    non-invoice classes. The fix returns an ``evaluation_skipped`` stub with
    ``weighted_overall_score=None`` so the aggregator drops the section from
    the weighted mean entirely.
    """
    svc = EvaluationService(
        region="us-east-1", config=_invoice_only_config(), max_workers=1
    )
    section = Section(section_id="1", classification="OtherDocument", page_ids=["1"])

    result = svc.evaluate_section(
        section=section,
        expected_results={},
        actual_results={},
    )

    assert result.metrics.get("evaluation_skipped") is True
    assert result.metrics.get("weighted_overall_score") is None
    assert result.metrics.get("exclusion_reason") == "no_extractable_schema"
    # No attributes are surfaced — Stickler was never invoked.
    assert result.attributes == []


@pytest.mark.unit
def test_evaluate_section_class_with_empty_properties_is_skipped():
    """A class configured with zero properties is also a scoring no-op.

    Some configs define a class name (so classification can label a page)
    but leave the extractable attribute list empty. Comparing two empty
    dicts through Stickler produces a garbage 0.0; skip instead.
    """
    config = {
        "classes": [
            {
                "$id": "cover-sheet",
                "x-aws-idp-document-type": "CoverSheet",
                "type": "object",
                "properties": {},
            }
        ]
    }
    svc = EvaluationService(region="us-east-1", config=config, max_workers=1)
    section = Section(section_id="1", classification="CoverSheet", page_ids=["1"])

    result = svc.evaluate_section(
        section=section,
        expected_results={},
        actual_results={},
    )

    assert result.metrics.get("evaluation_skipped") is True
    assert result.metrics.get("weighted_overall_score") is None


@pytest.mark.unit
def test_document_rollup_ignores_skipped_sections_and_weights_only_scored():
    """Mixed doc: 1 scored Invoice section + 2 no-op sections.

    Weighted mean should equal the Invoice section's score exactly — the
    no-op sections neither contribute to the numerator nor the denominator.
    Under the previous behavior each no-op section contributed 0.0 with
    fn=1, pulling the average down and inflating false-negatives.
    """
    svc = EvaluationService(
        region="us-east-1", config=_invoice_only_config(), max_workers=1
    )

    invoice_result = SectionEvaluationResult(
        section_id="1",
        document_class="Invoice",
        attributes=[],
        metrics={
            "weighted_overall_score": 0.85,
            "_stickler_counts": {"tp": 4, "fa": 0, "fd": 0, "fp": 0, "tn": 1, "fn": 1},
        },
    )
    other_result = SectionEvaluationResult(
        section_id="2",
        document_class="OtherDocument",
        attributes=[],
        metrics={
            "weighted_overall_score": None,
            "evaluation_skipped": True,
            "exclusion_reason": "no_extractable_schema",
        },
    )
    delivery_result = SectionEvaluationResult(
        section_id="3",
        document_class="DeliveryNote",
        attributes=[],
        metrics={
            "weighted_overall_score": None,
            "evaluation_skipped": True,
            "exclusion_reason": "no_extractable_schema",
        },
    )

    doc = Document(
        id="mixed-doc",
        input_key="mixed.pdf",
        input_bucket="in-bucket",
        output_bucket="out-bucket",
        status=Status.EXTRACTING,
    )
    doc.sections.append(
        Section(section_id="1", classification="Invoice", page_ids=["1"])
    )
    doc.sections.append(
        Section(section_id="2", classification="OtherDocument", page_ids=["2"])
    )
    doc.sections.append(
        Section(section_id="3", classification="DeliveryNote", page_ids=["3"])
    )

    def fake_process_section(actual_section, _expected_section, _document_context=""):
        if actual_section.section_id == "1":
            return invoice_result, {
                "tp": 4,
                "fp": 0,
                "fn": 1,
                "tn": 1,
                "fp1": 0,
                "fp2": 0,
            }
        if actual_section.section_id == "2":
            return other_result, {
                "tp": 0,
                "fp": 0,
                "fn": 0,
                "tn": 0,
                "fp1": 0,
                "fp2": 0,
            }
        return delivery_result, {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "fp1": 0, "fp2": 0}

    with patch.object(svc, "_process_section", side_effect=fake_process_section):
        with patch("idp_common.s3.write_content"):
            result = svc.evaluate_document(doc, doc, store_results=False)

    weighted = result.evaluation_result.overall_metrics["weighted_overall_score"]
    assert abs(weighted - 0.85) < 1e-9, (
        f"weighted_overall_score={weighted!r} — skipped sections must not "
        f"contribute to the weighted mean; expected 0.85 from Invoice alone."
    )
    # Excluded-flag is NOT set: at least one section was scored.
    assert not result.evaluation_result.overall_metrics.get("evaluation_excluded")
    assert result.evaluation_result.overall_metrics.get("skipped_section_count") == 2


@pytest.mark.unit
def test_document_rollup_all_sections_skipped_yields_none_score():
    """Doc whose every section is a no-op → weighted score is ``None``, not 0.0.

    Guarantees the Test Studio histogram / lowest-scores tables never see a
    0.000 for these docs — the aggregation Lambda drops None entries out of
    ``weightedOverallScores`` before the UI reads them.
    """
    svc = EvaluationService(
        region="us-east-1", config=_invoice_only_config(), max_workers=1
    )

    other_result = SectionEvaluationResult(
        section_id="1",
        document_class="OtherDocument",
        attributes=[],
        metrics={
            "weighted_overall_score": None,
            "evaluation_skipped": True,
            "exclusion_reason": "no_extractable_schema",
        },
    )
    delivery_result = SectionEvaluationResult(
        section_id="2",
        document_class="DeliveryNote",
        attributes=[],
        metrics={
            "weighted_overall_score": None,
            "evaluation_skipped": True,
            "exclusion_reason": "no_extractable_schema",
        },
    )

    doc = Document(
        id="all-noop-doc",
        input_key="all-noop.pdf",
        input_bucket="in-bucket",
        output_bucket="out-bucket",
        status=Status.EXTRACTING,
    )
    doc.sections.append(
        Section(section_id="1", classification="OtherDocument", page_ids=["1"])
    )
    doc.sections.append(
        Section(section_id="2", classification="DeliveryNote", page_ids=["2"])
    )

    def fake_process_section(actual_section, _expected_section, _document_context=""):
        if actual_section.section_id == "1":
            return other_result, {
                "tp": 0,
                "fp": 0,
                "fn": 0,
                "tn": 0,
                "fp1": 0,
                "fp2": 0,
            }
        return delivery_result, {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "fp1": 0, "fp2": 0}

    with patch.object(svc, "_process_section", side_effect=fake_process_section):
        with patch("idp_common.s3.write_content"):
            result = svc.evaluate_document(doc, doc, store_results=False)

    overall = result.evaluation_result.overall_metrics
    assert overall["weighted_overall_score"] is None, (
        f"weighted_overall_score should be None for a fully-excluded doc, "
        f"got {overall['weighted_overall_score']!r}"
    )
    assert overall.get("evaluation_excluded") is True
    assert overall.get("exclusion_reason") == "no_extractable_schema"
    assert overall.get("skipped_section_count") == 2


@pytest.mark.unit
def test_evaluate_section_class_not_in_config_but_expected_has_data_still_evaluates():
    """A class missing from config *with* expected data still gets auto-inferred.

    Guards against the exclusion check being too aggressive: when there IS
    expected data for a missing class, the service can auto-infer a schema
    and evaluate normally (existing behavior). The no-op path only kicks in
    when there is literally nothing to compare.
    """
    svc = EvaluationService(
        region="us-east-1", config=_invoice_only_config(), max_workers=1
    )
    section = Section(section_id="1", classification="Receipt", page_ids=["1"])

    result = svc.evaluate_section(
        section=section,
        expected_results={"amount": 42.0},
        actual_results={"amount": 42.0},
    )

    # NOT skipped — the schema was auto-inferred and evaluation ran.
    assert not result.metrics.get("evaluation_skipped")


@pytest.mark.unit
def test_has_no_extractable_schema_helper():
    """Direct coverage of ``_has_no_extractable_schema`` boolean matrix."""
    svc = EvaluationService(
        region="us-east-1", config=_invoice_only_config(), max_workers=1
    )

    # Class in config, has properties → NOT no-op
    assert svc._has_no_extractable_schema("Invoice", expected_results={}) is False

    # Class not in config, no expected data → no-op
    assert svc._has_no_extractable_schema("OtherDocument", expected_results={}) is True
    assert (
        svc._has_no_extractable_schema("OtherDocument", expected_results=None) is True
    )

    # Class not in config, expected has data → NOT no-op (auto-infer path)
    assert svc._has_no_extractable_schema("Receipt", expected_results={"a": 1}) is False

    # Class in config with empty properties → no-op
    svc_empty = EvaluationService(
        region="us-east-1",
        config={
            "classes": [
                {
                    "$id": "cover",
                    "x-aws-idp-document-type": "Cover",
                    "type": "object",
                    "properties": {},
                }
            ]
        },
        max_workers=1,
    )
    assert svc_empty._has_no_extractable_schema("Cover", expected_results={}) is True


# --------------------------------------------------------------------------
# Regression tests for issue #625: list-field parent verdict + section metrics
# https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/625
# --------------------------------------------------------------------------


def _list_of_five_partial_failure_service():
    """Config + gt/pred producing a 5-item list where 4 items are entirely
    wrong. Stickler reports tp=1, fd=4, fp=4 at the field level."""
    config = {
        "classes": [
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "doc",
                "x-aws-idp-document-type": "Doc",
                "x-aws-idp-evaluation-model-name": "Doc",
                "type": "object",
                "properties": {
                    "Items": {
                        "type": "array",
                        "x-aws-idp-evaluation-match-threshold": 0.5,
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "x-aws-idp-evaluation-method": "EXACT",
                                },
                                "amount": {
                                    "type": "number",
                                    "x-aws-idp-evaluation-method": "NUMERIC_EXACT",
                                    "x-aws-idp-evaluation-threshold": 0.01,
                                },
                            },
                        },
                    }
                },
            }
        ]
    }
    svc = EvaluationService(region="us-east-1", config=config, max_workers=1)
    section = Section(section_id="s", classification="Doc", page_ids=["1"])
    gt = {
        "Items": [
            {"name": "A", "amount": 1.0},
            {"name": "B", "amount": 2.0},
            {"name": "C", "amount": 3.0},
            {"name": "D", "amount": 4.0},
            {"name": "E", "amount": 5.0},
        ]
    }
    pred = {
        "Items": [
            {"name": "A", "amount": 1.0},
            {"name": "X", "amount": 99.0},
            {"name": "Y", "amount": 88.0},
            {"name": "Z", "amount": 77.0},
            {"name": "W", "amount": 66.0},
        ]
    }
    return svc, section, gt, pred


@pytest.mark.unit
def test_list_parent_verdict_reflects_item_failures_not_just_hits():
    """Issue #625 defect 1: a list field with any matching item was marked ✓
    even when the majority of items failed, because ``matched`` was
    ``(tp > 0) or (tn > 0)`` and Stickler's ``tp`` counts matched items.
    Post-fix: the parent verdict is ``False`` whenever any list item was a
    false discovery. Guarantees the parent ✓/✗ can't contradict its children
    the way sirirako reported."""
    svc, section, gt, pred = _list_of_five_partial_failure_service()
    result = svc.evaluate_section(section, gt, pred)
    items = next(a for a in result.attributes if a.name == "Items")
    assert items.matched is False, (
        f"parent Items should be ✗ when 4 of 5 items failed; got matched={items.matched}"
    )
    cell = (
        (result.stickler_comparison_result or {})
        .get("confusion_matrix", {})
        .get("fields", {})
        .get("Items", {})
        .get("overall", {})
    )
    # Sanity: the underlying counts confirm the failures Stickler reported.
    # (``all_fields_matched`` isn't present on per-field cells — only on
    # ``cm.overall`` — so the verdict falls through to the counts-based
    # branch, which is exactly the path that used to be wrong.)
    assert cell.get("fd", 0) == 4
    assert cell.get("tp", 0) == 1


@pytest.mark.unit
def test_section_metrics_include_list_item_false_discoveries():
    """Issue #625 defect 2: section metrics were derived from
    ``cm.aggregate``, which is Stickler's threshold-gated rollup of leaf
    fields INSIDE matched list items only — unmatched items were dropped
    entirely, so their false discoveries never reached the counts. A
    document with 80% of list values wrong was reporting 100% precision,
    recall, F1, and accuracy. Post-fix (v2.0): counts are derived from
    Stickler's row-level ``field_comparisons`` — one row per leaf comparison,
    threshold-gated per user config, so every failure mode is visible."""
    svc, section, gt, pred = _list_of_five_partial_failure_service()
    result = svc.evaluate_section(section, gt, pred)
    metrics = result.metrics

    # Stickler emits 10 leaf rows here — item [0] matches (2 tp), items
    # [1..4] Hungarian-paired even at score 0.0 with both leaves wrong (8 fd).
    #   Pre-fix (cm.aggregate):        tp=2, fd=0 → precision=1.0 (defect 2)
    #   v0.6.3 intermediate (overall): tp=1, fd=4 → precision=0.2 (item-level)
    #   v2.0 (rows):                   tp=2, fd=8 → precision=0.2 (leaf-level)
    # Headline number stayed at 0.2 across the last two — but the count
    # semantics now match what the drilldown UI shows.
    assert metrics["precision"] == pytest.approx(0.2), (
        "precision must reflect list-item FDs — pre-fix was 1.0 on 80% wrong data"
    )
    assert metrics["accuracy"] == pytest.approx(0.2)
    assert metrics["f1_score"] == pytest.approx(1.0 / 3.0)
    assert metrics["false_discovery_rate"] == pytest.approx(0.8)
    counts: Dict[str, int] = metrics["_stickler_counts"]  # type: ignore[assignment]
    assert counts.get("tp") == 2
    assert counts.get("fd") == 8
    assert counts.get("fp") == 8


def _list_of_five_kept_but_wrong_leaves_service():
    """CASE 5 config + gt/pred: 5-item list where every per-item score is
    ≈ 0.667 (two right children, one wrong) so all items pass the
    ``match_threshold=0.5`` Hungarian gate with strict `>` OR inclusive `>=`
    (finding 13 from #625 adversarial review — a previous 2-leaf shape sat
    exactly at 0.5 and would flip under any future Stickler gate change).
    4 of the 15 leaves inside are wrong. Sirikaro's original bug shape:
    parent used to show ✓ while the drilldown showed leaf ✗s."""
    config = {
        "classes": [
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "doc",
                "x-aws-idp-document-type": "Doc",
                "x-aws-idp-evaluation-model-name": "Doc",
                "type": "object",
                "properties": {
                    "Items": {
                        "type": "array",
                        "x-aws-idp-evaluation-match-threshold": 0.5,
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "x-aws-idp-evaluation-method": "EXACT",
                                },
                                "amount": {
                                    "type": "number",
                                    "x-aws-idp-evaluation-method": "NUMERIC_EXACT",
                                    "x-aws-idp-evaluation-threshold": 0.01,
                                },
                                "unit": {
                                    "type": "string",
                                    "x-aws-idp-evaluation-method": "EXACT",
                                },
                            },
                        },
                    }
                },
            }
        ]
    }
    svc = EvaluationService(region="us-east-1", config=config, max_workers=1)
    section = Section(section_id="s", classification="Doc", page_ids=["1"])
    gt = {
        "Items": [
            {"name": "Widget A", "amount": 1.0, "unit": "ea"},
            {"name": "Widget B", "amount": 2.0, "unit": "ea"},
            {"name": "Widget C", "amount": 3.0, "unit": "ea"},
            {"name": "Widget D", "amount": 4.0, "unit": "ea"},
            {"name": "Widget E", "amount": 5.0, "unit": "ea"},
        ]
    }
    pred = {
        "Items": [
            {"name": "Widget A", "amount": 1.0, "unit": "ea"},  # right
            {"name": "Foo", "amount": 2.0, "unit": "ea"},  # name wrong
            {"name": "Bar", "amount": 3.0, "unit": "ea"},  # ditto
            {"name": "Baz", "amount": 4.0, "unit": "ea"},  # ditto
            {"name": "Qux", "amount": 5.0, "unit": "ea"},  # ditto
        ]
    }
    return svc, section, gt, pred


@pytest.mark.unit
def test_case5_parent_verdict_reflects_leaf_failures_inside_kept_items():
    """Issue #625, sirikaro's original bug shape: every item is Hungarian-
    paired above ``match_threshold`` (per-item score ≈ 0.667, comfortably
    above threshold 0.5 with room for a future Stickler `>` vs `>=` gate
    change), but 4 of the 15 leaves inside are wrong. Under Stickler's
    item-level rollups (``cm.overall`` and ``all_fields_matched`` at the
    field cell), the parent reads ✓ — contradicting the drilldown's ✗ rows.
    Post-fix (v2.0): parent verdict comes from row-level ``field_comparisons``
    so parent = ✗ whenever any drilldown row under it is red."""
    svc, section, gt, pred = _list_of_five_kept_but_wrong_leaves_service()
    result = svc.evaluate_section(section, gt, pred)
    items = next(a for a in result.attributes if a.name == "Items")
    assert items.matched is False, (
        f"parent Items must be ✗ when 4 of 15 leaves failed — pre-fix showed "
        f"✓ because Stickler's item-level rollup masked leaf failures inside "
        f"paired items; got matched={items.matched}"
    )
    # Sanity: the field-cell view Stickler exposes (which we used to trust)
    # still says "all matched" — proving row-level derivation is the only
    # way to catch this shape.
    field_cell = (
        (result.stickler_comparison_result or {})
        .get("confusion_matrix", {})
        .get("fields", {})
        .get("Items", {})
        .get("overall", {})
    )
    assert field_cell.get("fd", 0) == 0, (
        "Stickler's field cell would say fd=0 here (item-level view)"
    )
    # But rows disagree — 4 red rows under Items.
    rows = (result.stickler_comparison_result or {}).get("field_comparisons") or []
    red_under_items = [
        fc
        for fc in rows
        if (fc.get("expected_key") or fc.get("field_path") or "").startswith("Items[")
        and fc.get("match") is False
    ]
    assert len(red_under_items) == 4, (
        f"expected 4 red drilldown rows under Items; got {len(red_under_items)}"
    )


@pytest.mark.unit
def test_case5_section_metrics_reflect_leaf_failures_inside_kept_items():
    """Same CASE 5 scenario. Section-level precision/F1/accuracy must reflect
    the 4 wrong leaves — under v0.6.3–v0.6.5 semantics these all read 1.0
    on this doc, silently claiming perfect extraction on ~73%-correct data.
    v2.0 counts every ``field_comparisons`` row, so failures surface."""
    svc, section, gt, pred = _list_of_five_kept_but_wrong_leaves_service()
    result = svc.evaluate_section(section, gt, pred)
    metrics = result.metrics
    # 15 leaf rows: 11 correct (5 amounts + 5 units + 1 name) + 4 wrong (4 names).
    # precision = 11/15 ≈ 0.733. F1 = 2*11 / (2*11 + 4 + 0) = 22/26 ≈ 0.846.
    assert metrics["precision"] == pytest.approx(11.0 / 15.0)
    assert metrics["recall"] == pytest.approx(1.0)  # no FN — all items paired
    assert metrics["f1_score"] == pytest.approx(22.0 / 26.0)
    assert metrics["accuracy"] == pytest.approx(11.0 / 15.0)
    assert metrics["false_discovery_rate"] == pytest.approx(4.0 / 15.0)
    counts: Dict[str, int] = metrics["_stickler_counts"]  # type: ignore[assignment]
    assert counts.get("tp") == 11
    assert counts.get("fd") == 4
    assert counts.get("fp") == 4
    assert counts.get("fn") == 0


@pytest.mark.unit
def test_section_metrics_unchanged_on_flat_all_pass_doc():
    """Sanity: switching from ``cm.aggregate`` to ``cm.overall`` must not
    affect flat (no-list) documents. Stickler yields identical numbers for
    the two nodes when no arrays are present."""
    config = {
        "classes": [
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "w2",
                "x-aws-idp-document-type": "W2",
                "x-aws-idp-evaluation-model-name": "W2",
                "type": "object",
                "properties": {
                    "ssn": {
                        "type": "string",
                        "x-aws-idp-evaluation-method": "EXACT",
                    },
                    "wages": {
                        "type": "number",
                        "x-aws-idp-evaluation-method": "NUMERIC_EXACT",
                        "x-aws-idp-evaluation-threshold": 0.01,
                    },
                },
            }
        ]
    }
    svc = EvaluationService(region="us-east-1", config=config, max_workers=1)
    section = Section(section_id="s", classification="W2", page_ids=["1"])
    result = svc.evaluate_section(
        section,
        {"ssn": "123-45-6789", "wages": 50000.0},
        {"ssn": "123-45-6789", "wages": 50000.0},
    )
    assert result.metrics["precision"] == pytest.approx(1.0)
    assert result.metrics["accuracy"] == pytest.approx(1.0)
    flat_counts: Dict[str, int] = result.metrics["_stickler_counts"]  # type: ignore[assignment]
    assert flat_counts.get("tp") == 2
    assert flat_counts.get("fd") == 0


@pytest.mark.unit
def test_empty_field_comparisons_logs_warning_and_reports_zero(monkeypatch, caplog):
    """When Stickler emits a populated ``confusion_matrix`` but zero
    ``field_comparisons`` rows (a shape change we do NOT silently paper over),
    section counts are honestly zero and a warning surfaces the mismatch.

    Previously (v0.6.6-dev) the code fell back to ``cm.aggregate`` on this
    shape — but that stamped v2.0-semantics on v1.0-semantics counts, silently
    bypassing the drift-warning gate downstream (finding 2 from #625
    adversarial review). Better to loudly report zeros and let the operator
    investigate than to fabricate plausible-looking numbers under the wrong
    counting model."""
    import logging

    config = {
        "classes": [
            {
                "$id": "d",
                "x-aws-idp-document-type": "D",
                "x-aws-idp-evaluation-model-name": "D",
                "type": "object",
                "properties": {
                    "a": {"type": "string", "x-aws-idp-evaluation-method": "EXACT"}
                },
            }
        ]
    }
    svc = EvaluationService(region="us-east-1", config=config, max_workers=1)
    section = Section(section_id="s", classification="D", page_ids=["1"])

    # Patch Stickler's compare_with to return a result with rows stripped —
    # the "cm present, rows absent" shape we want to catch loudly.
    from stickler import StructuredModel

    orig = StructuredModel.compare_with

    def _strip_rows(self, other, **kwargs):
        r = orig(self, other, **kwargs)
        r["field_comparisons"] = []
        return r

    monkeypatch.setattr(StructuredModel, "compare_with", _strip_rows)
    with caplog.at_level(
        logging.WARNING, logger="idp_common.evaluation.stickler_backend.results"
    ):
        result = svc.evaluate_section(section, {"a": "x"}, {"a": "x"})

    counts: Dict[str, int] = result.metrics["_stickler_counts"]  # type: ignore[assignment]
    # Counts are honestly zero — no silent fabrication from cm.aggregate.
    assert counts.get("tp", 0) == 0
    assert result.metrics["precision"] == 0.0
    # Warning surfaced the shape mismatch.
    assert any(
        "empty field_comparisons" in r.message.lower() for r in caplog.records
    ), (
        f"expected shape-mismatch warning, got records: {[r.message for r in caplog.records]}"
    )


@pytest.mark.unit
def test_no_rows_fallback_verdict_uses_score_threshold(monkeypatch):
    """Fallback path (no rows for a field) defers to Stickler's
    ``field_scores`` compared against the field's match threshold.

    Rationale (finding from #625 review-effort code review): the
    ``AttributeEvaluationResult`` carries BOTH ``matched`` and ``score``.
    Unconditional ``matched=False`` produced a "✗ with score 1.0"
    contradiction on any field whose row list was empty but whose
    Stickler score was above threshold. Deferring to the score keeps
    the two values on the same row consistent, and the section-count
    source is unaffected either way (no rows contributed to it).
    """
    config = {
        "classes": [
            {
                "$id": "d",
                "x-aws-idp-document-type": "D",
                "x-aws-idp-evaluation-model-name": "D",
                "type": "object",
                "properties": {
                    "a": {"type": "string", "x-aws-idp-evaluation-method": "EXACT"}
                },
            }
        ]
    }
    svc = EvaluationService(region="us-east-1", config=config, max_workers=1)
    section = Section(section_id="s", classification="D", page_ids=["1"])

    from stickler import StructuredModel

    orig = StructuredModel.compare_with

    def _clean_no_rows(self, other, **kwargs):
        r = orig(self, other, **kwargs)
        # Wipe rows so the fallback fires; leave field_scores populated
        # (a real Stickler run produces both) so the fallback has a
        # non-zero score to consult.
        r["field_comparisons"] = []
        return r

    monkeypatch.setattr(StructuredModel, "compare_with", _clean_no_rows)
    # Matching values → Stickler score 1.0 → over the 0.8 threshold → ✓
    result = svc.evaluate_section(section, {"a": "x"}, {"a": "x"})
    a = next(attr for attr in result.attributes if attr.name == "a")
    assert a.matched is True
    assert a.score == pytest.approx(1.0)


@pytest.mark.unit
def test_no_rows_fallback_verdict_reports_false_below_threshold(monkeypatch):
    """Complement to the above: a low field_score in the no-rows fallback
    still reports ✗, so a mismatch doesn't accidentally read ✓."""
    config = {
        "classes": [
            {
                "$id": "d",
                "x-aws-idp-document-type": "D",
                "x-aws-idp-evaluation-model-name": "D",
                "type": "object",
                "properties": {
                    "a": {"type": "string", "x-aws-idp-evaluation-method": "EXACT"}
                },
            }
        ]
    }
    svc = EvaluationService(region="us-east-1", config=config, max_workers=1)
    section = Section(section_id="s", classification="D", page_ids=["1"])

    from stickler import StructuredModel

    orig = StructuredModel.compare_with

    def _no_rows_low_score(self, other, **kwargs):
        r = orig(self, other, **kwargs)
        r["field_comparisons"] = []
        # Force a low score for field ``a`` regardless of Stickler's raw
        # exact-match verdict (some comparators emit both a score and a
        # boolean; here we exercise the score path).
        r.setdefault("field_scores", {})["a"] = 0.0
        return r

    monkeypatch.setattr(StructuredModel, "compare_with", _no_rows_low_score)
    result = svc.evaluate_section(section, {"a": "x"}, {"a": "y"})
    a = next(attr for attr in result.attributes if attr.name == "a")
    assert a.matched is False
