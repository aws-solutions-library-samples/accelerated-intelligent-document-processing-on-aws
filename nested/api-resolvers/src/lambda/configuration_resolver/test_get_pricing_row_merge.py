# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What `getPricing` hands the Pricing page once saved rows are merged per row (#1886).

The page enables **Restore default (All)** when ``pricing`` (the effective table)
differs from ``defaultPricing`` (``pricing-customizations.ts``). The effective
table is now the defaults with the saved rows laid over them per model and unit,
so the differences the page can see are exactly the saved rows that change
something: an overridden rate, or a unit or entry the defaults lack. A default
row missing from the saved table is not a difference, because it is back in force.

Runs the real resolver handler over the real ``ConfigurationManager`` with
DefaultPricing seeded from the shipped ``config_library/pricing.yaml``. Engine:
moto's DynamoDB, which matters only for the round trip of the two records.
"""

from __future__ import annotations

import copy
import importlib.util
import os
import sys
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws

os.environ.setdefault("CONFIGURATION_TABLE_NAME", "test-config-table")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from idp_common.config.configuration_manager import ConfigurationManager  # noqa: E402
from idp_common.config.constants import CONFIG_TYPE_DEFAULT_PRICING  # noqa: E402
from idp_common.config.models import PricingConfig  # noqa: E402

TABLE = "get-pricing-row-merge-table"
PRICING_YAML = Path(__file__).resolve().parents[5] / "config_library" / "pricing.yaml"


def _load_index():
    spec = importlib.util.spec_from_file_location(
        "config_resolver_index_pricing_row_merge", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["config_resolver_index_pricing_row_merge"] = module
    spec.loader.exec_module(module)
    return module


index = _load_index()
SHIPPED = PricingConfig(**yaml.safe_load(PRICING_YAML.read_text())).model_dump(
    mode="python", exclude={"config_type"}
)


def _flat(table) -> dict:
    return {
        (entry["name"], unit["name"]): unit["price"]
        for entry in table["pricing"]
        for unit in entry["units"]
    }


def _differences(response) -> dict:
    """{key: (effective, default)} for every unit the page would count."""
    effective = _flat(response["pricing"])
    default = _flat(response["defaultPricing"])
    return {
        key: (effective.get(key), default.get(key))
        for key in effective.keys() | default.keys()
        if effective.get(key) != default.get(key)
    }


@pytest.fixture
def manager(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.delenv("CONFIGURATION_BUCKET", raising=False)
        boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "Configuration", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        mgr = ConfigurationManager(table_name=TABLE)
        mgr.save_configuration(CONFIG_TYPE_DEFAULT_PRICING, PricingConfig(**SHIPPED))
        yield mgr


def _get_pricing(manager, saved) -> dict:
    manager.save_custom_pricing(PricingConfig(**saved))
    response = index.handle_get_pricing(manager)
    assert response["success"] is True
    assert len(response["defaultPricing"]["pricing"]) == len(SHIPPED["pricing"]) > 10
    return response


def test_an_unedited_saved_table_shows_no_customisation(manager):
    assert _differences(_get_pricing(manager, copy.deepcopy(SHIPPED))) == {}


def test_a_saved_rate_change_is_the_only_customisation_shown(manager):
    saved = copy.deepcopy(SHIPPED)
    entry = saved["pricing"][0]
    unit = entry["units"][0]
    default_price = unit["price"]
    unit["price"] = "0.123"
    del saved["pricing"][1:]  # the rest of the table left out of the record

    differences = _differences(_get_pricing(manager, saved))

    assert differences == {(entry["name"], unit["name"]): ("0.123", default_price)}


def test_an_added_unit_is_shown_as_a_customisation(manager):
    saved = copy.deepcopy(SHIPPED)
    entry = saved["pricing"][0]
    entry["units"].append({"name": "customUnit", "price": "0.42"})

    differences = _differences(_get_pricing(manager, saved))

    assert differences == {(entry["name"], "customUnit"): ("0.42", None)}


def test_a_default_row_missing_from_the_saved_table_is_back_in_force(manager):
    saved = copy.deepcopy(SHIPPED)
    del saved["pricing"][0]

    response = _get_pricing(manager, saved)

    assert _differences(response) == {}
    assert response["pricing"]["pricing"][0] == SHIPPED["pricing"][0]
