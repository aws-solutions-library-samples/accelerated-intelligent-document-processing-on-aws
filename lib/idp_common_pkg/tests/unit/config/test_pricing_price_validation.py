# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A price is saved only in the one form every reader values the same way.

The cost reports read a stored price with ``float()`` and the Pricing page read it
with JavaScript's ``Number()``, and nothing checked a price on save, so a value
one parser accepts and the other rejects (``0x10``, ``1_000``, ``nan``, ``inf``)
looked priced on one side and unpriced on the other. ``PricingConfig`` now refuses
any price ``parse_price`` rejects when it is written, keeps and reports one read
back from storage, and both report parsers use ``parse_price``.

Every invalid form below is one at least one of the two parsers accepts; the
comment beside it says which, so the case is a disagreement rather than an
arbitrary string. Engine: the moto cases use moto's DynamoDB, which matters only
for the round trip of a string attribute.
"""

import json
import logging
import re
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws
from pydantic import ValidationError

from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.constants import (
    CONFIG_TYPE_CUSTOM_PRICING,
    CONFIG_TYPE_DEFAULT_PRICING,
)
from idp_common.config.models import (
    PRICE_PATTERN,
    STORED_PRICING_CONTEXT,
    ConfigurationRecord,
    IDPConfig,
    PricingConfig,
    parse_price,
    read_price,
)
from idp_common.reporting.save_reporting_data import SaveReportingData

REPO_ROOT = Path(__file__).resolve().parents[5]
PRICING_YAML = REPO_ROOT / "config_library" / "pricing.yaml"
PRICE_CASES = (
    REPO_ROOT
    / "src"
    / "ui"
    / "src"
    / "components"
    / "pricing-layout"
    / "__tests__"
    / "price-cases.json"
)
UI_PRICE_FORMAT = (
    REPO_ROOT
    / "src"
    / "ui"
    / "src"
    / "components"
    / "pricing-layout"
    / "price-format.ts"
)
TABLE = "price-validation-test-table"

VALID = [
    ("0.0015", 0.0015),
    ("6.0E-8", 6.0e-8),
    ("6.0e-8", 6.0e-8),
    ("2.5E+1", 25.0),
    ("0", 0.0),
    ("0.0", 0.0),
    (".5", 0.5),
    ("5.", 5.0),
    ("1e-400", 0.0),  # underflows to 0 in both parsers; finite, so a price
]

INVALID = [
    "0x10",  # Number() reads 16; float() rejects
    "0b1",  # Number() reads 1; float() rejects
    "0o7",  # Number() reads 7; float() rejects
    "Infinity",  # Number() reads Infinity; float() reads inf
    "inf",  # float() reads inf; Number() rejects
    "nan",  # float() reads NaN; Number() rejects
    "NaN",  # float() reads NaN; Number() reads NaN
    "1_000",  # float() reads 1000; Number() rejects
    "٣",  # ARABIC-INDIC DIGIT THREE: float() reads 3; Number() rejects
    "",  # Number() reads 0; float() rejects
    " 0.5",  # both read 0.5 after trimming; refused, not normalised
    "0.5\n",  # both read 0.5; refused
    "-1",  # both read -1: a negative price
    "+1",  # both read 1: a sign is not part of the form
    "1e999",  # matches the pattern; both read infinity
    "1,000",  # both reject
    "$0.01",  # both reject
]


def _one(price) -> dict:
    return {
        "pricing": [
            {"name": "textract/a", "units": [{"name": "pages", "price": price}]}
        ]
    }


@pytest.mark.unit
class TestParsePrice:
    @pytest.mark.parametrize("text,value", VALID)
    def test_a_valid_price_has_the_value_float_gives_it(self, text, value):
        assert parse_price(text) == value == float(text)

    @pytest.mark.parametrize("text", INVALID)
    def test_a_form_the_two_parsers_disagree_on_is_not_a_price(self, text):
        assert parse_price(text) is None

    @pytest.mark.parametrize("value", [None, 1, 1.5, True, b"1"])
    def test_a_non_string_is_not_a_price(self, value):
        # PricingUnit stringifies on the way in; a raw non-string is a caller bug.
        assert parse_price(value) is None

    def test_the_ui_enforces_the_same_pattern(self):
        """The Pricing page and the backend must refuse the same strings."""
        source = UI_PRICE_FORMAT.read_text(encoding="utf-8")
        match = re.search(r"export const PRICE_PATTERN = /\^(.*)\$/;", source)
        assert match, "PRICE_PATTERN not found in price-format.ts"
        assert match.group(1) == PRICE_PATTERN.pattern


@pytest.mark.unit
class TestReadPrice:
    """Readers tolerate surrounding ASCII whitespace, which both old readers trimmed.

    The Pricing page saved a table cell as typed before prices were checked, so a
    stored ``'0.0015 '`` exists and was priced correctly by ``float()`` and
    ``Number()`` alike; it must not become unpriced on upgrade.
    """

    @pytest.mark.parametrize(
        "text,value",
        [
            (" 0.0015", 0.0015),
            ("0.0015 ", 0.0015),
            ("\t6.0E-8\n", 6.0e-8),
            ("0.5", 0.5),
        ],
    )
    def test_surrounding_ascii_whitespace_is_read(self, text, value):
        assert read_price(text) == value == float(text)

    @pytest.mark.parametrize("text", ["-1", "+1", "0x10", "nan", " ", ""])
    def test_anything_else_the_save_rule_refuses_is_unpriced(self, text):
        assert read_price(text) is None

    def test_save_still_refuses_surrounding_whitespace(self):
        with pytest.raises(ValidationError):
            PricingConfig(**_one(" 0.0015"))


SHARED_CASES = json.loads(PRICE_CASES.read_text(encoding="utf-8"))["cases"]


@pytest.mark.unit
class TestSharedCases:
    """One table of cases, read by this suite and by the UI's price-format.test.ts.

    Both sides assert their own rules against the same expected values, so the
    Python and TypeScript readers cannot drift apart without one suite failing.
    """

    def test_the_table_is_not_empty(self):
        # Non-vacuity: an empty table collects as zero cases, not as a failure.
        assert len(SHARED_CASES) > 50
        assert any(c["read"] is not None and c["save"] is None for c in SHARED_CASES)

    @pytest.mark.parametrize("case", SHARED_CASES, ids=lambda c: repr(c["input"]))
    def test_both_rules(self, case):
        assert parse_price(case["input"]) == case["save"]
        assert read_price(case["input"]) == case["read"]

    @pytest.mark.parametrize(
        "case",
        [c for c in SHARED_CASES if c["read"] is not None],
        ids=lambda c: repr(c["input"]),
    )
    def test_a_priced_case_is_one_float_values_the_same(self, case):
        # The authority on this side: the reader the reports used before.
        assert float(case["input"]) == case["read"]


@pytest.mark.unit
class TestShippedPricing:
    def test_every_shipped_price_is_valid(self):
        data = yaml.safe_load(PRICING_YAML.read_text(encoding="utf-8"))
        prices = [u["price"] for e in data["pricing"] for u in e["units"]]
        # Non-vacuity: an empty or truncated read would pass the loop below.
        assert len(prices) > 300
        refused = [p for p in prices if parse_price(str(p)) is None]
        assert refused == []
        # And the model that deploy-time update_configuration builds accepts it.
        PricingConfig(**{"pricing": data["pricing"]})


@pytest.mark.unit
class TestWriteIsRefused:
    @pytest.mark.parametrize("text", INVALID)
    def test_the_model_refuses_an_invalid_price(self, text):
        with pytest.raises(ValidationError, match="textract/a/pages"):
            PricingConfig(**_one(text))

    @pytest.mark.parametrize("text,value", VALID)
    def test_the_model_accepts_a_valid_price(self, text, value):
        assert PricingConfig(**_one(text)).pricing[0].units[0].price == text

    @pytest.mark.parametrize("number", [6e-08, 0.0015, 1, 0])
    def test_a_number_is_stored_as_a_valid_string(self, number):
        # An unquoted YAML price arrives as a number and is stringified first.
        unit = PricingConfig(**_one(number)).pricing[0].units[0]
        assert parse_price(unit.price) == number

    @pytest.mark.parametrize("number", [float("nan"), float("inf"), -1.0, True])
    def test_a_number_that_stringifies_invalid_is_refused(self, number):
        with pytest.raises(ValidationError):
            PricingConfig(**_one(number))

    def test_a_stored_instance_is_checked_again_when_it_is_written(self):
        """An instance read leniently cannot be written back unchecked."""
        stored = PricingConfig.model_validate(
            _one("0x10"), context=STORED_PRICING_CONTEXT
        )
        with pytest.raises(ValidationError, match="0x10"):
            ConfigurationRecord(
                configuration_type=CONFIG_TYPE_CUSTOM_PRICING, config=stored
            )

    def test_idp_config_still_loads_with_an_invalid_price(self):
        """The check is on PricingConfig only: a processing configuration that
        carries pricing rows must never stop loading over a price."""
        config = IDPConfig.model_validate(_one("0x10"))
        assert config.pricing[0].units[0].price == "0x10"


@pytest.mark.unit
class TestStoredIsKept:
    def test_a_stored_record_with_an_invalid_price_loads_and_warns(self, caplog):
        item = {"Configuration": CONFIG_TYPE_CUSTOM_PRICING, **_one("0x10")}
        with caplog.at_level(logging.WARNING):
            record = ConfigurationRecord.from_dynamodb_item(item)
        assert isinstance(record.config, PricingConfig)
        assert record.config.pricing[0].units[0].price == "0x10"
        assert "textract/a/pages: '0x10'" in caplog.text


def _manager(monkeypatch) -> ConfigurationManager:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", TABLE)
    monkeypatch.delenv("CONFIGURATION_BUCKET", raising=False)
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "Configuration", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ConfigurationManager()


@pytest.mark.unit
@mock_aws
class TestThroughTheManager:
    def test_save_custom_pricing_refuses_and_writes_nothing(self, monkeypatch):
        manager = _manager(monkeypatch)
        with pytest.raises(ValidationError):
            manager.save_custom_pricing(_one("0x10"))
        assert manager.get_configuration(CONFIG_TYPE_CUSTOM_PRICING) is None

    def test_a_legacy_invalid_custom_price_loads_and_is_unpriced(self, monkeypatch):
        """A CustomPricing row saved before the check existed must not break the
        stack: it loads, the Pricing page gets it back for correction, and the
        cost report leaves that unit unpriced while pricing the rest."""
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_DEFAULT_PRICING, PricingConfig(**_one("1.5"))
        )
        # Written the way an older release wrote it: no model check.
        boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE).put_item(
            Item={
                "Configuration": CONFIG_TYPE_CUSTOM_PRICING,
                "pricing": [
                    {
                        "name": "textract/a",
                        "units": [
                            {"name": "pages", "price": "nan"},
                            {"name": "documents", "price": "1_000"},
                            {"name": "forms", "price": "0.002"},
                            {"name": "queries", "price": " 0.004 "},
                        ],
                    }
                ],
            }
        )

        merged = manager.get_merged_pricing()

        assert merged is not None
        prices = {u.name: u.price for u in merged.pricing[0].units}
        assert prices == {
            "pages": "nan",
            "documents": "1_000",
            "forms": "0.002",
            "queries": " 0.004 ",
        }

        config = IDPConfig()
        config.pricing = merged.pricing
        reporter = SaveReportingData("test-bucket", config=config)
        # float() read these as NaN and 1000.0: a NaN cost and a cost the Pricing
        # page (Number('1_000') is NaN) shows as unpriced.
        assert reporter._get_unit_cost("textract/a", "pages") is None
        assert reporter._get_unit_cost("textract/a", "documents") is None
        assert reporter._get_unit_cost("textract/a", "forms") == 0.002
        # Only surrounding spaces wrong: both old readers valued it, so it stays priced.
        assert reporter._get_unit_cost("textract/a", "queries") == 0.004
