# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A saved pricing table is laid over the shipped defaults per model and unit (#1886).

``get_merged_pricing`` applied CustomPricing with ``deep_update``, which assigns
lists, so the saved table replaced the default ``pricing`` list wholesale. The
Pricing page always saves the full table, so a stack that had saved it once got no
price for any model a later release added to ``config_library/pricing.yaml``; that
model's usage was reported as unpriced. The effective table is now the defaults
with every saved row laid over them: a saved unit replaces the default unit of the
same name, a saved unit or entry the defaults lack is added, and a default entry or
unit the saved table does not mention is kept — which includes a default row the
operator deleted in the editor (accepted).

Every case runs through the real ``ConfigurationManager.get_merged_pricing`` with
DefaultPricing seeded from the shipped ``config_library/pricing.yaml``, and the
saved table built from it the way the Pricing page builds one: the whole table,
edited. Engine: moto's DynamoDB, which matters only for the round trip of the two
records; the merge itself is Python.
"""

import copy
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws

from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.constants import (
    CONFIG_TYPE_CUSTOM_PRICING,
    CONFIG_TYPE_DEFAULT_PRICING,
)
from idp_common.config.merge_utils import overlay_pricing_rows
from idp_common.config.models import IDPConfig, PricingConfig
from idp_common.reporting.save_reporting_data import SaveReportingData

REPO_ROOT = Path(__file__).resolve().parents[5]
PRICING_YAML = REPO_ROOT / "config_library" / "pricing.yaml"
TABLE = "pricing-row-merge-test-table"


def _shipped() -> dict:
    data = yaml.safe_load(PRICING_YAML.read_text())
    # Validated the way the deploy-time loader stores it.
    return PricingConfig(**data).model_dump(mode="python", exclude={"config_type"})


SHIPPED = _shipped()


def _flat(rows) -> dict:
    """{(entry, unit): price} — the key the cost readers price by."""
    return {
        (entry["name"], unit["name"]): unit["price"]
        for entry in rows
        for unit in entry["units"]
    }


def _multi_unit_entry() -> int:
    """Index of the first shipped entry with at least two units."""
    for index, entry in enumerate(SHIPPED["pricing"]):
        if len(entry["units"]) >= 2:
            return index
    raise AssertionError("pricing.yaml has no entry with two units")


@pytest.fixture
def manager(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setenv("CONFIGURATION_TABLE_NAME", TABLE)
        monkeypatch.delenv("CONFIGURATION_BUCKET", raising=False)
        boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "Configuration", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        mgr = ConfigurationManager()
        mgr.save_configuration(CONFIG_TYPE_DEFAULT_PRICING, PricingConfig(**SHIPPED))
        yield mgr


def _merged(manager, saved: dict) -> list:
    manager.save_custom_pricing(PricingConfig(**saved))
    merged = manager.get_merged_pricing()
    assert merged is not None
    return merged.model_dump(mode="python")["pricing"]


@pytest.mark.unit
def test_the_shipped_table_is_large_enough_to_mean_something():
    """Guards every case below against an empty or trivial authority."""
    assert len(SHIPPED["pricing"]) > 10
    assert _multi_unit_entry() >= 0


@pytest.mark.unit
def test_a_model_added_to_the_defaults_is_priced_on_a_stack_that_saved_earlier(
    manager,
):
    """The saved table predates the newest default model, as on an upgraded stack."""
    saved = copy.deepcopy(SHIPPED)
    added_later = saved["pricing"].pop()  # the release added this one afterwards
    saved["pricing"][0]["units"][0]["price"] = "0.5"  # and the operator edited a row

    merged = _flat(_merged(manager, saved))

    for unit in added_later["units"]:
        assert merged[(added_later["name"], unit["name"])] == unit["price"]
    assert len(merged) == len(_flat(SHIPPED["pricing"]))


@pytest.mark.unit
def test_a_saved_rate_overrides_its_default_unit_and_only_that_unit(manager):
    index = _multi_unit_entry()
    saved = copy.deepcopy(SHIPPED)
    entry = saved["pricing"][index]
    first, second = entry["units"][0], entry["units"][1]
    assert first["price"] != "0.123"
    first["price"] = "0.123"
    # Saved with only the edited unit: its other default units must survive.
    entry["units"] = [first]

    merged = _flat(_merged(manager, saved))

    assert merged[(entry["name"], first["name"])] == "0.123"
    assert merged[(entry["name"], second["name"])] == second["price"]
    expected = _flat(SHIPPED["pricing"])
    expected[(entry["name"], first["name"])] = "0.123"
    assert merged == expected


@pytest.mark.unit
def test_a_saved_entry_with_an_extra_unit_keeps_it(manager):
    saved = copy.deepcopy(SHIPPED)
    entry = saved["pricing"][0]
    assert all(u["name"] != "customUnit" for u in entry["units"])
    entry["units"].append({"name": "customUnit", "price": "0.42"})

    merged_rows = _merged(manager, saved)
    merged = _flat(merged_rows)

    assert merged[(entry["name"], "customUnit")] == "0.42"
    for unit in SHIPPED["pricing"][0]["units"]:
        assert merged[(entry["name"], unit["name"])] == unit["price"]
    # Overlaid onto the default entry, not appended as a second entry of that name.
    assert [e["name"] for e in merged_rows].count(entry["name"]) == 1


@pytest.mark.unit
def test_a_saved_entry_the_defaults_lack_is_added(manager):
    saved = copy.deepcopy(SHIPPED)
    saved["pricing"].append(
        {"name": "bedrock/my-custom-model", "units": [{"name": "t", "price": "1.0"}]}
    )

    merged = _flat(_merged(manager, saved))

    assert merged[("bedrock/my-custom-model", "t")] == "1.0"
    assert len(merged) == len(_flat(SHIPPED["pricing"])) + 1


@pytest.mark.unit
def test_a_default_row_deleted_in_the_editor_reappears(manager):
    """Accepted consequence of the per-row merge: a deletion is not stored."""
    index = _multi_unit_entry()
    saved = copy.deepcopy(SHIPPED)
    removed_entry = saved["pricing"].pop(0)
    removed_unit = saved["pricing"][index - 1]["units"].pop()

    merged = _flat(_merged(manager, saved))

    for unit in removed_entry["units"]:
        assert merged[(removed_entry["name"], unit["name"])] == unit["price"]
    target = SHIPPED["pricing"][index]["name"]
    assert merged[(target, removed_unit["name"])] == removed_unit["price"]
    assert merged == _flat(SHIPPED["pricing"])


@pytest.mark.unit
def test_saving_the_unedited_table_is_the_defaults_in_their_order(manager):
    merged_rows = _merged(manager, copy.deepcopy(SHIPPED))
    assert merged_rows == SHIPPED["pricing"]


@pytest.mark.unit
def test_an_empty_saved_table_leaves_the_defaults_in_force(manager):
    merged_rows = _merged(manager, {"pricing": []})
    assert merged_rows == SHIPPED["pricing"]


@pytest.mark.unit
def test_a_repeated_saved_name_resolves_the_way_the_readers_resolve_it(manager):
    """Saved rows are applied in order, so the later one wins for the unit."""
    name = SHIPPED["pricing"][0]["name"]
    unit = SHIPPED["pricing"][0]["units"][0]["name"]
    saved = {
        "pricing": [
            {"name": name, "units": [{"name": unit, "price": "1.0"}]},
            {"name": name, "units": [{"name": unit, "price": "2.0"}]},
        ]
    }
    assert _flat(_merged(manager, saved))[(name, unit)] == "2.0"


@pytest.mark.unit
def test_the_stored_records_keep_their_shape(manager):
    """Rollback safety: a previous release reads the same CustomPricing item.

    Only the reader changed. The saved record is still the submitted table, and
    reading the merge writes nothing back to either record.
    """
    saved = copy.deepcopy(SHIPPED)
    saved["pricing"] = saved["pricing"][:3]
    saved["pricing"][0]["units"][0]["price"] = "0.5"

    _merged(manager, saved)

    table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)
    custom_item = table.get_item(Key={"Configuration": CONFIG_TYPE_CUSTOM_PRICING})
    default_item = table.get_item(Key={"Configuration": CONFIG_TYPE_DEFAULT_PRICING})
    custom = ConfigurationManager._decompress_item(custom_item["Item"])
    default = ConfigurationManager._decompress_item(default_item["Item"])
    assert custom["pricing"] == saved["pricing"]
    assert default["pricing"] == SHIPPED["pricing"]


@pytest.mark.unit
def test_the_cost_report_prices_a_model_the_saved_table_does_not_name(manager):
    """The reader the reporting Lambda uses, fed the merged table it attaches."""
    saved = copy.deepcopy(SHIPPED)
    later = saved["pricing"].pop()
    manager.save_custom_pricing(PricingConfig(**saved))
    merged = manager.get_merged_pricing()
    assert merged is not None

    config = IDPConfig()
    config.pricing = merged.pricing
    reporter = SaveReportingData("test-bucket", config=config)

    unit = later["units"][0]
    assert reporter._get_unit_cost(later["name"], unit["name"]) == float(unit["price"])


@pytest.mark.unit
def test_a_repeated_saved_name_the_defaults_lack_is_one_entry(manager):
    saved = {
        "pricing": [
            {"name": "bedrock/x", "units": [{"name": "t", "price": "1.0"}]},
            {"name": "bedrock/x", "units": [{"name": "t", "price": "2.0"}]},
        ]
    }
    rows = _merged(manager, saved)
    assert [e for e in rows if e["name"] == "bedrock/x"] == [
        {"name": "bedrock/x", "units": [{"name": "t", "price": "2.0"}]}
    ]


@pytest.mark.unit
def test_the_overlay_modifies_neither_input():
    defaults = copy.deepcopy(SHIPPED["pricing"])
    saved = copy.deepcopy(SHIPPED["pricing"][:2])
    saved[0]["units"][0]["price"] = "0.5"
    saved.append({"name": "bedrock/x", "units": [{"name": "t", "price": "1.0"}]})
    saved_before = copy.deepcopy(saved)

    merged = overlay_pricing_rows(defaults, saved)
    merged[0]["units"][0]["price"] = "7.0"
    merged[-1]["units"][0]["price"] = "7.0"

    assert defaults == SHIPPED["pricing"]
    assert saved == saved_before


@pytest.mark.unit
def test_an_appended_unit_is_a_copy_of_the_saved_one():
    saved = [
        {"name": SHIPPED["pricing"][0]["name"], "units": [{"name": "x", "price": "1"}]}
    ]
    merged = overlay_pricing_rows(copy.deepcopy(SHIPPED["pricing"]), saved)
    merged[0]["units"][-1]["price"] = "7.0"
    assert saved[0]["units"][0]["price"] == "1"


@pytest.mark.unit
def test_a_repeated_default_name_takes_the_saved_rate_where_the_readers_look():
    """Both cost readers key by name, so the last of a repeated name prices."""
    defaults = [
        {"name": "bedrock/m", "units": [{"name": "t", "price": "1.0"}]},
        {
            "name": "bedrock/m",
            "units": [{"name": "t", "price": "2.0"}, {"name": "t", "price": "3.0"}],
        },
    ]
    saved = [{"name": "bedrock/m", "units": [{"name": "t", "price": "9.0"}]}]

    merged = overlay_pricing_rows(defaults, saved)

    config = IDPConfig()
    config.pricing = PricingConfig(pricing=merged).pricing
    reporter = SaveReportingData("test-bucket", config=config)
    assert reporter._get_unit_cost("bedrock/m", "t") == 9.0
    assert merged[1]["units"] == [
        {"name": "t", "price": "2.0"},
        {"name": "t", "price": "9.0"},
    ]


@pytest.mark.unit
def test_the_shipped_table_repeats_no_entry_or_unit_name():
    names = [e["name"] for e in SHIPPED["pricing"]]
    assert len(names) == len(set(names))
    for entry in SHIPPED["pricing"]:
        units = [u["name"] for u in entry["units"]]
        assert len(units) == len(set(units)), entry["name"]
