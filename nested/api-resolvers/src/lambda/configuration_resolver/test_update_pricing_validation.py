# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`updatePricing` refuses a price the cost reports would value differently (#1903).

The Pricing page used to send any string, and the resolver stored it. A price
such as ``0x10`` (JavaScript reads 16, the reports' ``float()`` rejects it) then
showed as priced on the page and was unpriced in every cost report. The check
lives in ``PricingConfig`` (``tests/unit/config/test_pricing_price_validation.py``
covers its accepted set); this suite pins that the resolver surfaces it as a
``ValidationError`` response naming the offending unit and writes nothing.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("CONFIGURATION_TABLE_NAME", "test-config-table")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


def _load_index():
    spec = importlib.util.spec_from_file_location(
        "config_resolver_index_update_pricing", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["config_resolver_index_update_pricing"] = module
    spec.loader.exec_module(module)
    return module


index = _load_index()


def _table(price):
    return {
        "pricing": [
            {"name": "bedrock/m", "units": [{"name": "inputTokens", "price": price}]}
        ]
    }


def test_an_invalid_price_is_refused_and_nothing_is_saved():
    manager = MagicMock()
    result = index.handle_update_pricing(manager, json.dumps(_table("0x10")))
    assert result["success"] is False
    assert result["error"]["type"] == "ValidationError"
    messages = " ".join(e["message"] for e in result["error"]["validationErrors"])
    assert "bedrock/m/inputTokens" in messages and "0x10" in messages
    manager.save_custom_pricing.assert_not_called()


def test_a_valid_price_is_saved():
    manager = MagicMock()
    manager.save_custom_pricing.return_value = True
    result = index.handle_update_pricing(manager, json.dumps(_table("6.0E-8")))
    assert result["success"] is True
    manager.save_custom_pricing.assert_called_once()
