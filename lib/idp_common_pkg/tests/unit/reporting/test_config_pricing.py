#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for configuration-based pricing functionality in SaveReportingData.
Tests that pricing is loaded exclusively from configuration dictionary.
"""

import pytest

from idp_common.config.models import IDPConfig
from idp_common.reporting.save_reporting_data import SaveReportingData


@pytest.mark.unit
def test_pricing_from_config_with_valid_configuration():
    """Test that pricing is loaded correctly from configuration dictionary"""

    # Mock configuration data that matches the expected format
    mock_config = {
        "pricing": [
            {
                "name": "textract/detect_document_text",
                "units": [
                    {
                        "name": "pages",
                        "price": "0.002",
                    }  # Different from hardcoded value
                ],
            },
            {
                "name": "bedrock/us.amazon.nova-lite-v1:0",
                "units": [
                    {
                        "name": "inputTokens",
                        "price": "7.0E-8",
                    },  # Different from hardcoded value
                    {"name": "outputTokens", "price": "3.0E-7"},
                ],
            },
        ]
    }

    # Create SaveReportingData instance with IDPConfig
    idp_config = IDPConfig.model_validate(mock_config)
    reporter = SaveReportingData("test-bucket", config=idp_config)

    # Test that pricing is loaded from configuration
    textract_cost = reporter._get_unit_cost("textract/detect_document_text", "pages")
    nova_input_cost = reporter._get_unit_cost(
        "bedrock/us.amazon.nova-lite-v1:0", "inputTokens"
    )
    nova_output_cost = reporter._get_unit_cost(
        "bedrock/us.amazon.nova-lite-v1:0", "outputTokens"
    )

    # Verify the costs match the configuration values
    assert textract_cost == 0.002, f"Expected 0.002, got {textract_cost}"
    assert nova_input_cost == 7.0e-8, f"Expected 7.0e-8, got {nova_input_cost}"
    assert nova_output_cost == 3.0e-7, f"Expected 3.0e-7, got {nova_output_cost}"


@pytest.mark.unit
def test_pricing_returns_unpriced_when_config_fails():
    """A config with no pricing section makes every service explicitly unpriced.

    Previously this returned 0.0, which reported a confident $0.00 for an entire
    deployment whose pricing table failed to load. None (-> NULL cost columns)
    says "we do not know" instead. See GitHub issue #926.
    """

    # Create SaveReportingData instance with config that has no pricing
    invalid_config = {}  # Empty config, no pricing data
    idp_config = IDPConfig.model_validate(invalid_config)
    reporter = SaveReportingData("test-bucket", config=idp_config)

    textract_cost = reporter._get_unit_cost("textract/detect_document_text", "pages")
    nova_input_cost = reporter._get_unit_cost(
        "bedrock/us.amazon.nova-lite-v1:0", "inputTokens"
    )

    assert textract_cost is None, (
        f"Expected None (no valid config), got {textract_cost}"
    )
    assert nova_input_cost is None, (
        f"Expected None (no valid config), got {nova_input_cost}"
    )


@pytest.mark.unit
def test_pricing_without_config_returns_unpriced():
    """Same, for a reporter constructed with no config at all."""

    # Create SaveReportingData instance without config
    reporter = SaveReportingData("test-bucket")

    textract_cost = reporter._get_unit_cost("textract/detect_document_text", "pages")
    nova_input_cost = reporter._get_unit_cost(
        "bedrock/us.amazon.nova-lite-v1:0", "inputTokens"
    )

    assert textract_cost is None, f"Expected None (no config), got {textract_cost}"
    assert nova_input_cost is None, f"Expected None (no config), got {nova_input_cost}"


@pytest.mark.unit
def test_pricing_cache_functionality():
    """Test that pricing data is cached to avoid repeated configuration processing"""

    mock_config = {
        "pricing": [
            {
                "name": "textract/detect_document_text",
                "units": [{"name": "pages", "price": "0.002"}],
            }
        ]
    }

    idp_config = IDPConfig.model_validate(mock_config)
    reporter = SaveReportingData("test-bucket", config=idp_config)

    # Call _get_unit_cost multiple times
    cost1 = reporter._get_unit_cost("textract/detect_document_text", "pages")
    cost2 = reporter._get_unit_cost("textract/detect_document_text", "pages")
    cost3 = reporter._get_unit_cost("textract/detect_document_text", "pages")

    # Verify all calls return the same value
    assert cost1 == cost2 == cost3 == 0.002


@pytest.mark.unit
def test_clear_pricing_cache():
    """Test that clearing the cache forces reload of configuration"""

    mock_config = {
        "pricing": [
            {
                "name": "textract/detect_document_text",
                "units": [{"name": "pages", "price": "0.002"}],
            }
        ]
    }

    idp_config = IDPConfig.model_validate(mock_config)
    reporter = SaveReportingData("test-bucket", config=idp_config)

    # First call loads from config
    cost1 = reporter._get_unit_cost("textract/detect_document_text", "pages")
    assert cost1 == 0.002

    # Clear cache
    reporter.clear_pricing_cache()

    # Second call should still return the same value from config
    cost2 = reporter._get_unit_cost("textract/detect_document_text", "pages")
    assert cost2 == 0.002


@pytest.mark.unit
def test_pricing_with_invalid_price_values():
    """Test that invalid price values are logged and skipped during price loading"""

    mock_config = {
        "pricing": [
            {
                "name": "textract/detect_document_text",
                "units": [
                    {"name": "pages", "price": "invalid_price"},  # Invalid price
                    {"name": "documents", "price": "0.002"},  # Valid price
                ],
            }
        ]
    }

    # Prices are stored as strings, so validation doesn't fail during model creation
    # Invalid prices are handled during float conversion in _get_pricing_from_config
    idp_config = IDPConfig.model_validate(mock_config)
    reporter = SaveReportingData("test-bucket", config=idp_config)

    # The invalid price should be skipped and logged as a warning
    # The valid price should be loaded successfully
    invalid_cost = reporter._get_unit_cost("textract/detect_document_text", "pages")
    valid_cost = reporter._get_unit_cost("textract/detect_document_text", "documents")

    # A price that will not parse is dropped from the unit map, so the unit reads
    # as absent from the entry — and an absent unit nothing declares free is
    # unpriced, i.e. NULL. That is the right answer for this input: an unparseable
    # rate is unknown, not zero, and $0.00 for it understated every page of every
    # document with nothing to query for. See idp_common.metering_units and
    # GitHub issue #1212.
    assert invalid_cost is None, f"Expected NULL for invalid price, got {invalid_cost}"
    # Valid price returns the actual value
    assert valid_cost == 0.002, f"Expected 0.002, got {valid_cost}"
