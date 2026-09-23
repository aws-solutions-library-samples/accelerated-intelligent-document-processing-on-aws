# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
ConfigurationManager read/write paths that were previously unmeasured.

This module is the DynamoDB configuration table's whole read/write surface: the
stack-seeded `Config#default` record, the named Configuration Profiles that
override it, the separate DefaultPricing/CustomPricing and
DefaultModelConfigLimits/CustomModelConfigLimits pairs, and the BDA project
tracking attributes that live on a profile's head item alongside its
configuration.

The failures worth testing here are the ones that produce no error. A profile
that is stored as a legacy sparse delta and is *not* merged with the default
loads as Pydantic defaults rather than as the deployment's configuration, and
the pipeline then runs models nobody chose. A null in an incoming delta that is
not recognised as "restore this field to its default" persists as a null, and a
`False` that *is* mistaken for a null silently reverts a setting the user just
turned off. A save that replaces the head item drops the BDA project ARN written
by a separate `update_item`. A decompression failure that returns the raw item
yields a configuration of pure defaults with no exception raised. Every test
below asserts the value an operator would observe, not that a code path ran.

Existing coverage that is deliberately not repeated: revision cut/prune/publish
semantics (`test_config_revisions.py`), the v0.5→v0.6 and v0.6→v0.7 key
relocations and `get_raw_configuration`'s migration behaviour
(`test_v05_to_v06_migration.py`, `test_v06_to_v07_migration.py`,
`test_merge_migration_order.py`), gzip round-tripping and class-count capacity
(`test_compression.py`, `test_compression_matrix.py`), inert gating hook refusal
(`test_hook_reachability.py`), and `activate_version` /
`list_config_versions` pagination (`test_configuration_manager.py`).
"""

import base64
import gzip
import json
import os
from decimal import Decimal
from unittest.mock import Mock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from idp_common.config import configuration_manager as cm
from idp_common.config.configuration_manager import (
    ConfigurationManager,
    _is_full_config,
)
from idp_common.config.constants import (
    ACTIVE_POINTER_KEY,
    CONFIG_TYPE_CONFIG,
    CONFIG_TYPE_CUSTOM_MODEL_CONFIG_LIMITS,
    CONFIG_TYPE_CUSTOM_PRICING,
    CONFIG_TYPE_DEFAULT_MODEL_CONFIG_LIMITS,
    CONFIG_TYPE_DEFAULT_PRICING,
    CONFIG_TYPE_SCHEMA,
    DEFAULT_VERSION,
)
from idp_common.config.models import (
    IDPConfig,
    ModelConfigLimitsConfig,
    PricingConfig,
    SchemaConfig,
)

TABLE = "cfgmgr-coverage-table"
BUCKET = "cfgmgr-coverage-bucket"

# A model id that no Pydantic default uses, so "the seeded default was read"
# cannot be confused with "Pydantic filled its own default".
SEEDED_MODEL = "us.anthropic.claude-3-5-haiku-20241022-v1:0"
PYDANTIC_DEFAULT_MODEL = "us.amazon.nova-pro-v1:0"


def _make_table(with_bucket: bool = False):
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "Configuration", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    if with_bucket:
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    return ddb.Table(TABLE)


def _manager(monkeypatch, with_bucket: bool = False) -> ConfigurationManager:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", TABLE)
    if with_bucket:
        monkeypatch.setenv("CONFIGURATION_BUCKET", BUCKET)
    else:
        monkeypatch.delenv("CONFIGURATION_BUCKET", raising=False)
    return ConfigurationManager()


def _seed_default(manager: ConfigurationManager) -> IDPConfig:
    """Write a `Config#default` distinguishable from IDPConfig's own defaults."""
    default = IDPConfig(
        notes="seeded-default-notes",
        classification={"model": SEEDED_MODEL},
        summarization={"model": SEEDED_MODEL},
    )
    manager.save_configuration(CONFIG_TYPE_CONFIG, default, version=DEFAULT_VERSION)
    return default


def _mock_table_manager(mock_table: Mock) -> ConfigurationManager:
    """A manager whose table is a Mock, for injecting botocore failures."""
    with patch("idp_common.config.configuration_manager.boto3") as mock_boto3:
        mock_boto3.resource.return_value.Table.return_value = mock_table
        return ConfigurationManager(table_name=TABLE)


def _client_error(code: str, op: str = "GetItem") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


# ---------------------------------------------------------------------------
# _is_full_config: the single decision that routes a stored profile to either
# "return it as-is" or "merge it over the default".
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIsFullConfig:
    def test_empty_dict_is_not_full(self):
        """An empty raw read must not be classified as a complete config.

        If it were, get_merged_configuration would return a defaults-only
        IDPConfig for a profile whose record failed to decompress.
        """
        assert _is_full_config({}) is False

    def test_explicit_marker_wins_over_a_thin_body(self):
        """One section plus the marker is full: the writer said so."""
        assert _is_full_config({"_config_format": "full", "ocr": {}}) is True

    def test_four_sections_reach_the_heuristic_threshold(self):
        assert (
            _is_full_config(
                {"ocr": {}, "classification": {}, "extraction": {}, "classes": []}
            )
            is True
        )

    def test_three_sections_are_still_sparse(self):
        """Exactly at the boundary: 3 sections must route to the merge path."""
        assert (
            _is_full_config({"ocr": {}, "classification": {}, "extraction": {}})
            is False
        )

    def test_non_section_keys_do_not_count_toward_the_threshold(self):
        """`notes`/`use_bda`/`config_format_version` are not config sections.

        migrate_config() stamps config_format_version onto every raw read, so if
        arbitrary keys counted, a two-key sparse delta would start reading as
        full and would never be merged with the default again.
        """
        raw = {
            "notes": "x",
            "use_bda": True,
            "config_format_version": "0.7",
            "managed": False,
            "test_set": "a",
            "ocr": {},
        }
        assert _is_full_config(raw) is False

    def test_a_wrong_marker_value_does_not_assert_fullness(self):
        assert _is_full_config({"_config_format": "sparse", "ocr": {}}) is False


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInit:
    def test_missing_table_name_raises_rather_than_defaulting(self, monkeypatch):
        """No implicit table name: a wrong table is a silently wrong config."""
        monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
        with patch("idp_common.config.configuration_manager.boto3"):
            with pytest.raises(ValueError, match="Configuration table name"):
                ConfigurationManager()

    def test_empty_env_var_is_treated_as_absent(self, monkeypatch):
        monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "")
        with patch("idp_common.config.configuration_manager.boto3"):
            with pytest.raises(ValueError, match="Configuration table name"):
                ConfigurationManager()

    def test_explicit_table_name_overrides_the_env_var(self, monkeypatch):
        monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "from-env")
        with patch("idp_common.config.configuration_manager.boto3") as mock_boto3:
            manager = ConfigurationManager(table_name="explicit")
        assert manager.table_name == "explicit"
        mock_boto3.resource.return_value.Table.assert_called_once_with("explicit")

    def test_region_is_forwarded_to_the_dynamodb_resource(self, monkeypatch):
        """A caller that resolved the table in one region must read it there.

        Passing region=None instead would let boto3 resolve a different region
        and hit a same-named table in the wrong account/stack.
        """
        monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "t")
        with patch("idp_common.config.configuration_manager.boto3") as mock_boto3:
            manager = ConfigurationManager(region="us-west-2")
        assert manager.region == "us-west-2"
        mock_boto3.resource.assert_called_once_with("dynamodb", region_name="us-west-2")


# ---------------------------------------------------------------------------
# Reads: absent vs error vs empty
# ---------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
class TestReadPaths:
    def test_get_configuration_returns_none_for_an_absent_record(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        assert manager.get_configuration(CONFIG_TYPE_CONFIG, "nope") is None

    def test_get_raw_configuration_returns_none_for_an_absent_record(self, monkeypatch):
        """None, not {}: `{}` would read as a valid empty delta downstream."""
        _make_table()
        manager = _manager(monkeypatch)
        assert manager.get_raw_configuration(CONFIG_TYPE_CONFIG, "nope") is None

    def test_get_raw_configuration_uses_the_bare_key_when_version_is_empty(
        self, monkeypatch
    ):
        """Pricing/Schema records are stored under an unversioned key.

        With a "#" appended the read would miss and every caller would see an
        unpriced deployment rather than an error.
        """
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_DEFAULT_PRICING,
            PricingConfig(
                pricing=[
                    {"name": "textract/x", "units": [{"name": "pages", "price": "1"}]}
                ]
            ),
        )
        raw = manager.get_raw_configuration(CONFIG_TYPE_DEFAULT_PRICING, "")
        assert raw is not None
        assert raw["pricing"][0]["name"] == "textract/x"

    def test_get_raw_configuration_strips_head_metadata_from_the_config_body(
        self, monkeypatch
    ):
        """BDA/revision head attributes are not configuration.

        Leaking them into the raw delta would make them part of a subsequent
        save's body, and IDPConfig forbids nothing at the top level, so they
        would silently become config keys.
        """
        table = _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="n"), version="p"
        )
        manager.set_bda_project_arn("p", "arn:aws:bedrock:us-east-1:1:project/x")
        table.update_item(
            Key={"Configuration": f"{CONFIG_TYPE_CONFIG}#p"},
            UpdateExpression="SET LatestRevision = :r, PublishedRevision = :r",
            ExpressionAttributeValues={":r": 3},
        )

        raw = manager.get_raw_configuration(CONFIG_TYPE_CONFIG, "p")

        assert raw is not None
        for leaked in (
            "Configuration",
            "BdaProjectArn",
            "BdaSyncStatus",
            "BdaLastSyncedAt",
            "LatestRevision",
            "PublishedRevision",
            "CreatedAt",
            "UpdatedAt",
            "Description",
            "IsActive",
        ):
            assert leaked not in raw
        assert raw["notes"] == "n"

    def test_get_configuration_reraises_a_dynamodb_failure(self):
        """A throttled read must not look like "no configuration"."""
        mock_table = Mock()
        mock_table.get_item.side_effect = _client_error("ProvisionedThroughputExceeded")
        manager = _mock_table_manager(mock_table)
        with pytest.raises(ClientError):
            manager.get_configuration(CONFIG_TYPE_CONFIG, "p")

    def test_get_raw_configuration_reraises_a_dynamodb_failure(self):
        mock_table = Mock()
        mock_table.get_item.side_effect = _client_error("ProvisionedThroughputExceeded")
        manager = _mock_table_manager(mock_table)
        with pytest.raises(ClientError):
            manager.get_raw_configuration(CONFIG_TYPE_CONFIG, "p")


# ---------------------------------------------------------------------------
# get_merged_configuration: the legacy sparse path and active-version
# resolution. This is the runtime read for every document.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
class TestMergedConfigurationLegacySparsePath:
    @staticmethod
    def _put_sparse(table, version: str, body: dict):
        """Write a pre-full-format profile: inline attributes, no marker."""
        table.put_item(
            Item={"Configuration": f"{CONFIG_TYPE_CONFIG}#{version}", **body}
        )

    def test_a_sparse_profile_is_merged_over_the_seeded_default(self, monkeypatch):
        """The failure this prevents: the profile loads with Pydantic defaults.

        A legacy profile that stored only `notes` must still run under the
        stack's default models. Reading it as a full config would give
        classification.model = IDPConfig's own default, which is a model the
        operator never selected.
        """
        table = _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        self._put_sparse(table, "legacy", {"notes": "profile-note"})

        merged = manager.get_merged_configuration("legacy")

        assert merged is not None
        assert merged.notes == "profile-note", "the delta must win"
        assert merged.classification.model == SEEDED_MODEL, (
            "unset keys must come from Config#default, not from IDPConfig's "
            "field defaults"
        )
        assert merged.classification.model != PYDANTIC_DEFAULT_MODEL

    def test_the_delta_overrides_the_default_within_a_nested_section(self, monkeypatch):
        """Sibling keys inside an overridden section survive the merge."""
        table = _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        self._put_sparse(
            table, "legacy", {"classification": {"temperature": Decimal("0.7")}}
        )

        merged = manager.get_merged_configuration("legacy")

        assert merged is not None
        assert merged.classification.temperature == pytest.approx(0.7)
        assert merged.classification.model == SEEDED_MODEL, (
            "overriding one key in `classification` must not drop the rest of "
            "the section"
        )
        assert merged.summarization.model == SEEDED_MODEL

    def test_the_merge_is_persisted_so_the_next_read_takes_the_fast_path(
        self, monkeypatch
    ):
        table = _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        self._put_sparse(table, "legacy", {"notes": "profile-note"})

        manager.get_merged_configuration("legacy")

        raw = manager.get_raw_configuration(CONFIG_TYPE_CONFIG, "legacy")
        assert raw is not None
        assert _is_full_config(raw) is True
        assert raw["classification"]["model"] == SEEDED_MODEL

        # Second read must not rewrite the record.
        with patch.object(manager, "save_configuration") as spy:
            again = manager.get_merged_configuration("legacy")
        spy.assert_not_called()
        assert again is not None
        assert again.notes == "profile-note"

    def test_a_failed_full_config_read_falls_back_to_the_merge_path(self, monkeypatch):
        """A transient read failure must not fail the document.

        get_configuration re-raises a ClientError, and the full-config attempt
        is wrapped so that a throttled or otherwise failing read drops through
        to the default + raw merge rather than propagating.
        """
        table = _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        self._put_sparse(table, "legacy", {"notes": "profile-note"})
        real_get = manager.get_configuration

        def flaky(config_type, version=None):
            if version == "legacy":
                raise _client_error("ProvisionedThroughputExceeded")
            return real_get(config_type, version)

        with patch.object(manager, "get_configuration", side_effect=flaky):
            merged = manager.get_merged_configuration("legacy")

        assert merged is not None
        assert merged.notes == "profile-note"
        assert merged.classification.model == SEEDED_MODEL

    def test_a_non_idpconfig_default_record_returns_none_not_a_partial_merge(
        self, monkeypatch
    ):
        """Defensive, and the right answer is None.

        Merging a profile's deltas onto something that is not an IDPConfig
        would produce a config whose provenance nobody can reconstruct.
        """
        table = _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        self._put_sparse(table, "legacy", {"notes": "profile-note"})
        real_get = manager.get_configuration

        def wrong_default(config_type, version=None):
            if version == DEFAULT_VERSION:
                return SchemaConfig()
            return real_get(config_type, version)

        with patch.object(manager, "get_configuration", side_effect=wrong_default):
            assert manager.get_merged_configuration("legacy") is None

    def test_the_merged_config_is_returned_even_if_persisting_it_fails(
        self, monkeypatch
    ):
        """A failed format rewrite must not fail the document."""
        table = _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        self._put_sparse(table, "legacy", {"notes": "profile-note"})

        with patch.object(
            manager, "save_configuration", side_effect=RuntimeError("write denied")
        ):
            merged = manager.get_merged_configuration("legacy")

        assert merged is not None
        assert merged.notes == "profile-note"
        assert merged.classification.model == SEEDED_MODEL

    def test_a_sparse_profile_with_no_default_returns_none(self, monkeypatch):
        """Not a half-merged config: None, so the caller fails loudly."""
        table = _make_table()
        manager = _manager(monkeypatch)
        self._put_sparse(table, "legacy", {"notes": "profile-note"})

        assert manager.get_merged_configuration("legacy") is None

    def test_an_unknown_profile_raises_instead_of_returning_the_default(
        self, monkeypatch
    ):
        """A typo'd profile name must not silently process under `default`.

        This is the whole point of the ValueError: a document pinned to a
        profile that does not exist would otherwise run on the default
        configuration and look successful.
        """
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)

        with pytest.raises(ValueError, match="No Version ghost configuration found"):
            manager.get_merged_configuration("ghost")

    def test_the_format_marker_is_not_carried_into_the_merged_body(self, monkeypatch):
        """A stray marker in a sparse record must not reach IDPConfig."""
        table = _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        self._put_sparse(
            table, "legacy", {"notes": "profile-note", "_config_format": "full"}
        )

        # The marker makes _is_full_config true, so this reads as full; assert
        # the marker never lands in the model's fields either way.
        merged = manager.get_merged_configuration("legacy")
        assert merged is not None
        assert not hasattr(merged, "_config_format")


@pytest.mark.unit
@mock_aws
class TestMergedConfigurationVersionResolution:
    def test_an_empty_version_resolves_to_the_active_profile(self, monkeypatch):
        """Not to `default`: an activated profile is the operator's choice."""
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="active-profile"), version="lending"
        )
        manager.activate_version("lending")

        merged = manager.get_merged_configuration("")

        assert merged is not None
        assert merged.notes == "active-profile"

    def test_an_empty_version_falls_back_to_default_when_none_is_active(
        self, monkeypatch
    ):
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="inactive"), version="lending"
        )

        merged = manager.get_merged_configuration("")

        assert merged is not None
        assert merged.notes == "seeded-default-notes"

    def test_a_pinned_revision_is_authoritative_over_the_profile_head(
        self, monkeypatch
    ):
        """A run pinned to r1 must not be processed under r2's configuration."""
        _make_table(with_bucket=True)
        manager = _manager(monkeypatch, with_bucket=True)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="r1-body"), version="lending"
        )
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="r2-body"), version="lending"
        )

        pinned = manager.get_merged_configuration("lending", revision=1)
        head = manager.get_merged_configuration("lending")

        assert pinned is not None and head is not None
        assert pinned.notes == "r1-body"
        assert head.notes == "r2-body"

    def test_a_missing_pinned_revision_raises_rather_than_using_the_head(
        self, monkeypatch
    ):
        _make_table(with_bucket=True)
        manager = _manager(monkeypatch, with_bucket=True)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="head"), version="lending"
        )

        with pytest.raises(ValueError, match="r99"):
            manager.get_merged_configuration("lending", revision=99)


# ---------------------------------------------------------------------------
# save_configuration: dict inputs must be validated as the *right* model.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
class TestSaveConfigurationDictCoercion:
    def test_saving_the_active_profile_keeps_it_active(self, monkeypatch):
        """put_item replaces the item, so IsActive has to be carried forward.

        If it were not, an ordinary config edit would deactivate the profile
        and every subsequent document would silently process under `default`
        instead — with no error anywhere.
        """
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="v1"), version="lending"
        )
        manager.activate_version("lending")

        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="v2"), version="lending"
        )

        record = manager._read_record(CONFIG_TYPE_CONFIG, "lending")
        assert record is not None
        assert record.is_active is True
        listed = {
            v["versionName"]: v["isActive"] for v in manager.list_config_versions()
        }
        assert listed == {"lending": True, DEFAULT_VERSION: False}

    def test_saving_an_inactive_profile_does_not_activate_it(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="v1"), version="lending"
        )

        record = manager._read_record(CONFIG_TYPE_CONFIG, "lending")
        assert record is not None
        assert record.is_active is False

    def test_an_update_preserves_created_at_and_moves_updated_at(self, monkeypatch):
        """CreatedAt is the profile's age; overwriting it on every save would
        make the profile list unsortable by creation time."""
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="v1"), version="lending"
        )
        first = manager._read_record(CONFIG_TYPE_CONFIG, "lending")
        assert first is not None and first.metadata is not None

        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="v2"), version="lending"
        )

        second = manager._read_record(CONFIG_TYPE_CONFIG, "lending")
        assert second is not None and second.metadata is not None
        first_created = first.metadata.created_at
        first_updated = first.metadata.updated_at
        assert isinstance(first_created, str) and isinstance(first_updated, str)
        assert second.metadata.created_at == first_created
        assert isinstance(second.metadata.updated_at, str)
        assert second.metadata.updated_at >= first_updated

    def test_a_save_with_no_description_keeps_the_existing_one(self, monkeypatch):
        """description=None means "not supplied", not "clear it"."""
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG,
            IDPConfig(notes="v1"),
            version="lending",
            description="the original description",
        )

        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="v2"), version="lending"
        )

        record = manager._read_record(CONFIG_TYPE_CONFIG, "lending")
        assert record is not None
        assert record.description == "the original description"

    def test_a_schema_dict_is_validated_as_schemaconfig_and_round_trips(
        self, monkeypatch
    ):
        """Validating a Schema dict as IDPConfig would drop `properties`."""
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_SCHEMA,
            {
                "type": "object",
                "required": ["invoice_id"],
                "properties": {"invoice_id": {"type": "string"}},
            },
        )

        loaded = manager.get_configuration(CONFIG_TYPE_SCHEMA)

        assert isinstance(loaded, SchemaConfig)
        assert loaded.required == ["invoice_id"]
        assert loaded.properties == {"invoice_id": {"type": "string"}}

    @pytest.mark.parametrize(
        "config_type", [CONFIG_TYPE_DEFAULT_PRICING, CONFIG_TYPE_CUSTOM_PRICING]
    )
    def test_a_pricing_dict_is_validated_as_pricingconfig(
        self, monkeypatch, config_type
    ):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            config_type,
            {
                "pricing": [
                    {
                        "name": "bedrock/m",
                        "units": [{"name": "inputTokens", "price": "6.0E-8"}],
                    }
                ]
            },
        )

        loaded = manager.get_configuration(config_type)

        assert isinstance(loaded, PricingConfig)
        assert loaded.config_type == config_type
        assert loaded.pricing[0].units[0].price == "6.0E-8", (
            "a scientific-notation price must survive storage as a string"
        )

    @pytest.mark.parametrize(
        "config_type",
        [
            CONFIG_TYPE_DEFAULT_MODEL_CONFIG_LIMITS,
            CONFIG_TYPE_CUSTOM_MODEL_CONFIG_LIMITS,
        ],
    )
    def test_a_limits_dict_is_validated_as_modelconfiglimitsconfig(
        self, monkeypatch, config_type
    ):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            config_type,
            {"model_limits": [{"pattern": "nova", "max_output_tokens": 5000}]},
        )

        loaded = manager.get_configuration(config_type)

        assert isinstance(loaded, ModelConfigLimitsConfig)
        assert loaded.config_type == config_type
        assert loaded.model_limits[0].max_output_tokens == 5000

    def test_a_bare_dict_defaults_to_idpconfig_for_the_config_type(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG,
            {"notes": "from-dict", "_config_format": "full"},
            version="p",
        )

        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "p")

        assert isinstance(loaded, IDPConfig)
        assert loaded.notes == "from-dict"

    def test_the_marker_is_stripped_before_validation_not_stored_as_a_field(
        self, monkeypatch
    ):
        """IDPConfig would reject or absorb `_config_format` as a config field."""
        _make_table()
        manager = _manager(monkeypatch)
        incoming = {"notes": "n", "_config_format": "full"}
        manager.save_configuration(CONFIG_TYPE_CONFIG, incoming, version="p")

        assert "_config_format" not in incoming, "the caller's dict is popped in place"
        raw = manager.get_raw_configuration(CONFIG_TYPE_CONFIG, "p")
        assert raw is not None
        # _write_record re-adds the marker; it must be the marker value, not a
        # config field carried through from the caller.
        assert raw["_config_format"] == "full"


# ---------------------------------------------------------------------------
# Pricing: DefaultPricing + CustomPricing
# ---------------------------------------------------------------------------


def _pricing(*names_and_prices) -> PricingConfig:
    return PricingConfig(
        pricing=[
            {"name": name, "units": [{"name": "pages", "price": price}]}
            for name, price in names_and_prices
        ]
    )


@pytest.mark.unit
@mock_aws
class TestMergedPricing:
    def test_no_default_pricing_returns_none(self, monkeypatch):
        """None, not an empty PricingConfig: an empty list prices everything
        at zero, which reports a cost of $0 for every document."""
        _make_table()
        manager = _manager(monkeypatch)
        assert manager.get_merged_pricing() is None

    def test_default_only_is_returned_verbatim(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_DEFAULT_PRICING, _pricing(("textract/a", "1.5"))
        )

        merged = manager.get_merged_pricing()

        assert merged is not None
        assert [e.name for e in merged.pricing] == ["textract/a"]
        assert merged.pricing[0].units[0].price == "1.5"

    def test_a_custom_entry_overrides_the_default_price(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_DEFAULT_PRICING, _pricing(("textract/a", "1.5"))
        )
        manager.save_custom_pricing(_pricing(("textract/a", "9.9")))

        merged = manager.get_merged_pricing()

        assert merged is not None
        assert merged.pricing[0].units[0].price == "9.9"

    def test_a_partial_custom_list_replaces_the_default_list_wholesale(
        self, monkeypatch
    ):
        """Pinned behaviour, and a sharp edge worth knowing.

        `deep_update` assigns lists rather than merging them, so CustomPricing
        is a full replacement of `pricing` despite the docstring calling it
        "deltas". A custom record naming one service drops the price of every
        other service, and the cost report then shows those services as free
        rather than erroring.
        """
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_DEFAULT_PRICING,
            _pricing(("textract/a", "1.5"), ("bedrock/b", "0.2")),
        )
        manager.save_custom_pricing(_pricing(("bedrock/b", "0.9")))

        merged = manager.get_merged_pricing()

        assert merged is not None
        assert [e.name for e in merged.pricing] == ["bedrock/b"]
        assert "textract/a" not in [e.name for e in merged.pricing]

    def test_an_empty_custom_pricing_record_erases_all_default_prices(
        self, monkeypatch
    ):
        """An empty CustomPricing is not the same as no CustomPricing."""
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_DEFAULT_PRICING, _pricing(("textract/a", "1.5"))
        )
        manager.save_custom_pricing(PricingConfig(pricing=[]))

        merged = manager.get_merged_pricing()

        assert merged is not None
        assert merged.pricing == []

    def test_the_merge_is_a_new_object_and_writes_nothing_back(self, monkeypatch):
        """Reading the effective pricing must be a pure read.

        Returning the stored default object would lose the override, and
        persisting the merge would make DefaultPricing unrecoverable after the
        first read — the custom values would have become the defaults.
        """
        _make_table()
        manager = _manager(monkeypatch)
        stored_default = _pricing(("textract/a", "1.5"))
        stored_custom = _pricing(("textract/a", "9.9"))
        manager.save_configuration(CONFIG_TYPE_DEFAULT_PRICING, stored_default)
        manager.save_custom_pricing(stored_custom)

        merged = manager.get_merged_pricing()

        assert merged is not None
        assert merged is not stored_default
        assert merged is not stored_custom
        default_again = manager.get_configuration(CONFIG_TYPE_DEFAULT_PRICING)
        custom_again = manager.get_configuration(CONFIG_TYPE_CUSTOM_PRICING)
        assert isinstance(default_again, PricingConfig)
        assert isinstance(custom_again, PricingConfig)
        assert default_again.pricing[0].units[0].price == "1.5"
        assert custom_again.pricing[0].units[0].price == "9.9"

    def test_a_wrong_typed_default_record_returns_none(self, monkeypatch):
        """A DefaultPricing key holding something else must not be merged."""
        _make_table()
        manager = _manager(monkeypatch)
        with patch.object(manager, "get_configuration", return_value=SchemaConfig()):
            assert manager.get_merged_pricing() is None

    def test_a_wrong_typed_custom_record_falls_back_to_the_default(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        default = _pricing(("textract/a", "1.5"))

        def fake_get(config_type, version=None):
            return (
                default
                if config_type == CONFIG_TYPE_DEFAULT_PRICING
                else SchemaConfig()
            )

        with patch.object(manager, "get_configuration", side_effect=fake_get):
            merged = manager.get_merged_pricing()

        assert merged is default, "the default must be returned unmodified"

    def test_save_custom_pricing_accepts_a_dict_and_round_trips(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        assert (
            manager.save_custom_pricing(
                {
                    "pricing": [
                        {
                            "name": "bedrock/x",
                            "units": [{"name": "pages", "price": "3"}],
                        }
                    ]
                }
            )
            is True
        )

        loaded = manager.get_configuration(CONFIG_TYPE_CUSTOM_PRICING)
        assert isinstance(loaded, PricingConfig)
        assert loaded.pricing[0].name == "bedrock/x"

    def test_delete_custom_pricing_restores_the_default_prices(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_DEFAULT_PRICING, _pricing(("textract/a", "1.5"))
        )
        manager.save_custom_pricing(_pricing(("textract/a", "9.9")))

        assert manager.delete_custom_pricing() is True

        merged = manager.get_merged_pricing()
        assert merged is not None
        assert merged.pricing[0].units[0].price == "1.5"
        assert manager.get_configuration(CONFIG_TYPE_CUSTOM_PRICING) is None

    def test_delete_custom_pricing_is_idempotent_when_the_table_is_gone(self):
        """ResourceNotFoundException means "already reset" — return True."""
        mock_table = Mock()
        mock_table.delete_item.side_effect = _client_error(
            "ResourceNotFoundException", "DeleteItem"
        )
        manager = _mock_table_manager(mock_table)
        assert manager.delete_custom_pricing() is True

    def test_delete_custom_pricing_propagates_any_other_failure(self):
        """An access-denied delete must not report success."""
        mock_table = Mock()
        mock_table.delete_item.side_effect = _client_error(
            "AccessDeniedException", "DeleteItem"
        )
        manager = _mock_table_manager(mock_table)
        with pytest.raises(ClientError):
            manager.delete_custom_pricing()


# ---------------------------------------------------------------------------
# Model config limits: Custom is a FULL replacement, unlike pricing.
# ---------------------------------------------------------------------------


def _limits(*patterns, config_type=None) -> ModelConfigLimitsConfig:
    kwargs = {
        "model_limits": [
            {"pattern": p, "max_output_tokens": 1000 + i}
            for i, p in enumerate(patterns)
        ]
    }
    if config_type:
        kwargs["config_type"] = config_type
    return ModelConfigLimitsConfig(**kwargs)


@pytest.mark.unit
@mock_aws
class TestMergedModelConfigLimits:
    def test_no_default_returns_none(self, monkeypatch):
        """None, not an empty list: an empty list matches no model, and the
        bedrock client then has no max_output_tokens for any model."""
        _make_table()
        manager = _manager(monkeypatch)
        assert manager.get_merged_model_config_limits() is None

    def test_default_only_is_returned(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_DEFAULT_MODEL_CONFIG_LIMITS, _limits("nova", "claude")
        )

        merged = manager.get_merged_model_config_limits()

        assert merged is not None
        assert [e.pattern for e in merged.model_limits] == ["nova", "claude"]

    def test_custom_fully_replaces_the_default_list_and_keeps_its_order(
        self, monkeypatch
    ):
        """The documented asymmetry with pricing.

        model_limits is first-match-wins, so a merge would silently change
        which entry matches. Custom must be the whole list, in its own order,
        with no default entry appended or prepended.
        """
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_DEFAULT_MODEL_CONFIG_LIMITS, _limits("nova", "claude", "titan")
        )
        manager.save_custom_model_config_limits(_limits("claude-opus", "claude"))

        merged = manager.get_merged_model_config_limits()

        assert merged is not None
        assert [e.pattern for e in merged.model_limits] == ["claude-opus", "claude"]
        assert "nova" not in [e.pattern for e in merged.model_limits]
        assert "titan" not in [e.pattern for e in merged.model_limits]

    def test_an_empty_custom_list_is_honoured_not_treated_as_absent(self, monkeypatch):
        """Distinguishes "no CustomModelConfigLimits record" from "a record
        whose list is empty": the first returns the default, the second
        returns nothing matchable."""
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_DEFAULT_MODEL_CONFIG_LIMITS, _limits("nova")
        )
        manager.save_custom_model_config_limits(
            ModelConfigLimitsConfig(model_limits=[])
        )

        merged = manager.get_merged_model_config_limits()

        assert merged is not None
        assert merged.model_limits == []
        assert merged.config_type == "CustomModelConfigLimits"

    def test_a_wrong_typed_default_record_returns_none(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        with patch.object(manager, "get_configuration", return_value=SchemaConfig()):
            assert manager.get_merged_model_config_limits() is None

    def test_a_wrong_typed_custom_record_falls_back_to_the_default(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        default = _limits("nova")

        def fake_get(config_type, version=None):
            if config_type == CONFIG_TYPE_DEFAULT_MODEL_CONFIG_LIMITS:
                return default
            return SchemaConfig()

        with patch.object(manager, "get_configuration", side_effect=fake_get):
            assert manager.get_merged_model_config_limits() is default

    def test_saving_custom_limits_from_a_dict_stores_them_under_the_custom_key(
        self, monkeypatch
    ):
        """A payload with no config_type must not land as DefaultModelConfigLimits."""
        _make_table()
        manager = _manager(monkeypatch)
        assert (
            manager.save_custom_model_config_limits(
                {"model_limits": [{"pattern": "nova", "max_output_tokens": 4096}]}
            )
            is True
        )

        loaded = manager.get_configuration(CONFIG_TYPE_CUSTOM_MODEL_CONFIG_LIMITS)
        assert isinstance(loaded, ModelConfigLimitsConfig)
        assert loaded.config_type == "CustomModelConfigLimits"
        assert loaded.model_limits[0].max_output_tokens == 4096
        assert (
            manager.get_configuration(CONFIG_TYPE_DEFAULT_MODEL_CONFIG_LIMITS) is None
        )

    def test_delete_custom_limits_restores_the_default_list(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_DEFAULT_MODEL_CONFIG_LIMITS, _limits("nova")
        )
        manager.save_custom_model_config_limits(_limits("claude"))

        assert manager.delete_custom_model_config_limits() is True

        merged = manager.get_merged_model_config_limits()
        assert merged is not None
        assert [e.pattern for e in merged.model_limits] == ["nova"]

    def test_delete_custom_limits_is_idempotent_when_the_table_is_gone(self):
        mock_table = Mock()
        mock_table.delete_item.side_effect = _client_error(
            "ResourceNotFoundException", "DeleteItem"
        )
        manager = _mock_table_manager(mock_table)
        assert manager.delete_custom_model_config_limits() is True

    def test_delete_custom_limits_propagates_any_other_failure(self):
        mock_table = Mock()
        mock_table.delete_item.side_effect = _client_error(
            "AccessDeniedException", "DeleteItem"
        )
        manager = _mock_table_manager(mock_table)
        with pytest.raises(ClientError):
            manager.delete_custom_model_config_limits()


# ---------------------------------------------------------------------------
# BDA project tracking: head-item attributes maintained by update_item, which
# a subsequent put_item must not drop.
# ---------------------------------------------------------------------------

ARN = "arn:aws:bedrock:us-east-1:123456789012:data-automation-project/abc"


@pytest.mark.unit
@mock_aws
class TestBdaProjectTracking:
    def test_set_then_get_round_trips_the_arn_and_stamps_a_sync_time(self, monkeypatch):
        table = _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="p")

        manager.set_bda_project_arn("p", ARN, sync_status="creating")

        assert manager.get_bda_project_arn("p") == ARN
        item = table.get_item(Key={"Configuration": f"{CONFIG_TYPE_CONFIG}#p"})["Item"]
        assert item["BdaSyncStatus"] == "creating"
        assert item["BdaLastSyncedAt"].endswith("Z")

    def test_the_default_sync_status_is_synced(self, monkeypatch):
        table = _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="p")

        manager.set_bda_project_arn("p", ARN)

        item = table.get_item(Key={"Configuration": f"{CONFIG_TYPE_CONFIG}#p"})["Item"]
        assert item["BdaSyncStatus"] == "synced"

    def test_get_returns_none_for_a_profile_with_no_linked_project(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="p")
        assert manager.get_bda_project_arn("p") is None

    def test_get_returns_none_for_a_profile_that_does_not_exist(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        assert manager.get_bda_project_arn("ghost") is None

    def test_get_returns_none_rather_than_raising_on_a_dynamodb_failure(self):
        """BDA linkage is advisory; a throttle must not fail the caller."""
        mock_table = Mock()
        mock_table.get_item.side_effect = _client_error("ProvisionedThroughputExceeded")
        manager = _mock_table_manager(mock_table)
        assert manager.get_bda_project_arn("p") is None

    def test_setting_the_arn_does_not_disturb_the_stored_configuration(
        self, monkeypatch
    ):
        """update_item, not put_item: the config body must survive."""
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="keep-me"), version="p"
        )

        manager.set_bda_project_arn("p", ARN)

        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "p")
        assert isinstance(loaded, IDPConfig)
        assert loaded.notes == "keep-me"

    def test_a_later_configuration_save_preserves_the_linked_project(self, monkeypatch):
        """The head-field preservation this module exists to guarantee.

        save_configuration issues a put_item, which replaces the whole item. If
        BdaProjectArn were not read back and re-attached, saving an unrelated
        config edit would silently unlink the BDA project and the next document
        would be processed with no blueprint.
        """
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="p")
        manager.set_bda_project_arn("p", ARN, sync_status="out-of-sync")

        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="edited"), version="p"
        )

        assert manager.get_bda_project_arn("p") == ARN
        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "p")
        assert isinstance(loaded, IDPConfig)
        assert loaded.notes == "edited"

    def test_clear_removes_the_arn_and_the_status_together(self, monkeypatch):
        """A cleared link must not leave a stale "synced" status behind."""
        table = _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="keep-me"), version="p"
        )
        manager.set_bda_project_arn("p", ARN)

        manager.clear_bda_project_arn("p")

        item = table.get_item(Key={"Configuration": f"{CONFIG_TYPE_CONFIG}#p"})["Item"]
        assert "BdaProjectArn" not in item
        assert "BdaSyncStatus" not in item
        assert "BdaLastSyncedAt" not in item
        assert manager.get_bda_project_arn("p") is None
        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "p")
        assert isinstance(loaded, IDPConfig)
        assert loaded.notes == "keep-me", "unlinking must not erase the config"

    def test_set_sync_status_changes_only_the_status(self, monkeypatch):
        table = _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="p")
        manager.set_bda_project_arn("p", ARN, sync_status="synced")
        before = table.get_item(Key={"Configuration": f"{CONFIG_TYPE_CONFIG}#p"})[
            "Item"
        ]

        manager.set_bda_sync_status("p", "out-of-sync")

        after = table.get_item(Key={"Configuration": f"{CONFIG_TYPE_CONFIG}#p"})["Item"]
        assert after["BdaSyncStatus"] == "out-of-sync"
        assert after["BdaProjectArn"] == ARN
        assert after["BdaLastSyncedAt"] == before["BdaLastSyncedAt"], (
            "the sync timestamp records the last actual sync, not a status edit"
        )

    @pytest.mark.parametrize(
        "method,args",
        [
            ("set_bda_project_arn", ("p", ARN)),
            ("clear_bda_project_arn", ("p",)),
            ("set_bda_sync_status", ("p", "synced")),
        ],
    )
    def test_writes_propagate_a_dynamodb_failure(self, method, args):
        """Unlike the read, a failed write must not be reported as done."""
        mock_table = Mock()
        mock_table.update_item.side_effect = _client_error(
            "AccessDeniedException", "UpdateItem"
        )
        manager = _mock_table_manager(mock_table)
        with pytest.raises(ClientError):
            getattr(manager, method)(*args)


# ---------------------------------------------------------------------------
# delete_configuration
# ---------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
class TestDeleteConfiguration:
    def test_a_config_delete_without_a_version_is_refused(self, monkeypatch):
        """Without the guard the bare key "Config" would be deleted instead."""
        _make_table()
        manager = _manager(monkeypatch)
        with pytest.raises(ValueError, match="Version is required"):
            manager.delete_configuration(CONFIG_TYPE_CONFIG)

    @pytest.mark.parametrize("name", ["default", "DEFAULT", "Default"])
    def test_the_default_profile_cannot_be_deleted_in_any_casing(
        self, monkeypatch, name
    ):
        """The comparison is case-insensitive, so `DEFAULT` is refused too.

        Deleting it would leave every sparse profile unmergeable and every
        reset-to-default a silent no-op.
        """
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)

        with pytest.raises(ValueError, match="Cannot delete"):
            manager.delete_configuration(CONFIG_TYPE_CONFIG, name)

        assert (
            manager.get_configuration(CONFIG_TYPE_CONFIG, DEFAULT_VERSION) is not None
        )

    def test_deleting_an_unknown_profile_raises(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        with pytest.raises(ValueError, match="not found in configurations"):
            manager.delete_configuration(CONFIG_TYPE_CONFIG, "ghost")

    def test_the_active_profile_cannot_be_deleted_and_survives_the_attempt(
        self, monkeypatch
    ):
        """Assert the record is still there: a refusal that deleted anyway
        would leave the stack with an active profile pointing at nothing."""
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="live"), version="lending"
        )
        manager.activate_version("lending")

        with pytest.raises(ValueError, match="Cannot delete active version"):
            manager.delete_configuration(CONFIG_TYPE_CONFIG, "lending")

        still = manager.get_configuration(CONFIG_TYPE_CONFIG, "lending")
        assert isinstance(still, IDPConfig)
        assert still.notes == "live"

    def test_an_inactive_profile_is_deleted(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="x"), version="lending"
        )

        manager.delete_configuration(CONFIG_TYPE_CONFIG, "lending")

        assert manager.get_configuration(CONFIG_TYPE_CONFIG, "lending") is None

    def test_deleting_a_profile_also_drops_its_revision_history(self, monkeypatch):
        """A later profile of the same name must not inherit old revisions."""
        _make_table(with_bucket=True)
        manager = _manager(monkeypatch, with_bucket=True)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="first"), version="lending"
        )
        assert manager.list_revisions("lending") != []

        manager.delete_configuration(CONFIG_TYPE_CONFIG, "lending")

        assert manager.list_revisions("lending") == []

    def test_the_record_is_still_deleted_if_history_cleanup_fails(self, monkeypatch):
        """Orphaned S3 bodies are a cost problem; a profile that refuses to
        delete is a user-facing one."""
        _make_table(with_bucket=True)
        manager = _manager(monkeypatch, with_bucket=True)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="x"), version="lending"
        )

        with patch.object(
            manager.revisions, "delete_profile", side_effect=RuntimeError("s3 down")
        ):
            manager.delete_configuration(CONFIG_TYPE_CONFIG, "lending")

        assert manager.get_configuration(CONFIG_TYPE_CONFIG, "lending") is None

    def test_a_non_config_type_is_deleted_under_its_bare_key(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_CUSTOM_PRICING, _pricing(("textract/a", "1"))
        )

        manager.delete_configuration(CONFIG_TYPE_CUSTOM_PRICING)

        assert manager.get_configuration(CONFIG_TYPE_CUSTOM_PRICING) is None

    def test_a_dynamodb_delete_failure_is_reraised(self):
        mock_table = Mock()
        mock_table.delete_item.side_effect = _client_error(
            "AccessDeniedException", "DeleteItem"
        )
        manager = _mock_table_manager(mock_table)
        with pytest.raises(ClientError):
            manager.delete_configuration(CONFIG_TYPE_SCHEMA)


# ---------------------------------------------------------------------------
# handle_update_custom_configuration
# ---------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
class TestHandleUpdateCustomConfiguration:
    def test_reset_to_default_copies_the_default_body_into_the_profile(
        self, monkeypatch
    ):
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="drifted"), version="lending"
        )

        assert manager.handle_update_custom_configuration(
            {"resetToDefault": True}, version="lending"
        )

        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "lending")
        assert isinstance(loaded, IDPConfig)
        assert loaded.notes == "seeded-default-notes"
        assert loaded.classification.model == SEEDED_MODEL

    def test_reset_to_default_with_no_default_leaves_the_profile_untouched(
        self, monkeypatch
    ):
        """Returns True while writing nothing — the profile must not be
        blanked to IDPConfig's own defaults."""
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="mine"), version="lending"
        )

        assert manager.handle_update_custom_configuration(
            {"resetToDefault": True}, version="lending"
        )

        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "lending")
        assert isinstance(loaded, IDPConfig)
        assert loaded.notes == "mine"

    def test_save_as_default_writes_both_the_default_and_the_source_profile(
        self, monkeypatch
    ):
        """Both writes must land. Promoting to `default` while leaving the
        profile on its old body (or the reverse) leaves the table internally
        inconsistent, and the UI then shows the profile as differing from a
        default it just became.
        """
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)

        assert manager.handle_update_custom_configuration(
            {"saveAsDefault": True, "notes": "promoted", "classes": []},
            version="lending",
        )

        promoted = manager.get_configuration(CONFIG_TYPE_CONFIG, DEFAULT_VERSION)
        profile = manager.get_configuration(CONFIG_TYPE_CONFIG, "lending")
        assert isinstance(promoted, IDPConfig) and isinstance(profile, IDPConfig)
        assert promoted.notes == "promoted"
        assert profile.notes == "promoted"

    def test_save_as_default_records_the_promotion_on_both_revisions(self, monkeypatch):
        _make_table(with_bucket=True)
        manager = _manager(monkeypatch, with_bucket=True)
        _seed_default(manager)

        manager.handle_update_custom_configuration(
            {"saveAsDefault": True, "notes": "promoted"},
            version="lending",
            created_by="admin@example.test",
        )

        default_notes = [r["notes"] for r in manager.list_revisions(DEFAULT_VERSION)]
        profile_notes = [r["notes"] for r in manager.list_revisions("lending")]
        assert "Promoted from profile 'lending'" in default_notes
        assert "Saved as default" in profile_notes
        assert manager.list_revisions("lending")[0]["createdBy"] == "admin@example.test"

    def test_save_as_default_does_not_take_the_incoming_body_from_the_default(
        self, monkeypatch
    ):
        """The frontend sends the complete config; the stored default is
        replaced by it, not merged into it."""
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)

        manager.handle_update_custom_configuration(
            {"saveAsDefault": True, "notes": "promoted"}, version="lending"
        )

        promoted = manager.get_configuration(CONFIG_TYPE_CONFIG, DEFAULT_VERSION)
        assert isinstance(promoted, IDPConfig)
        assert promoted.classification.model == PYDANTIC_DEFAULT_MODEL, (
            "saveAsDefault replaces the default wholesale; unsent keys revert "
            "to IDPConfig's field defaults rather than being preserved"
        )

    def test_save_as_version_merges_the_payload_onto_the_default(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)

        assert manager.handle_update_custom_configuration(
            {"saveAsVersion": True, "notes": "new-profile"}, version="fresh"
        )

        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "fresh")
        assert isinstance(loaded, IDPConfig)
        assert loaded.notes == "new-profile"
        assert loaded.classification.model == SEEDED_MODEL, (
            "a new profile inherits the stack default for keys it did not send"
        )

    def test_save_as_version_without_a_default_saves_the_payload_alone(
        self, monkeypatch
    ):
        _make_table()
        manager = _manager(monkeypatch)

        assert manager.handle_update_custom_configuration(
            {"saveAsVersion": True, "notes": "new-profile"}, version="fresh"
        )

        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "fresh")
        assert isinstance(loaded, IDPConfig)
        assert loaded.notes == "new-profile"
        assert loaded.classification.model == PYDANTIC_DEFAULT_MODEL

    def test_a_json_string_payload_is_parsed(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)

        manager.handle_update_custom_configuration(
            json.dumps({"saveAsVersion": True, "notes": "from-json"}), version="fresh"
        )

        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "fresh")
        assert isinstance(loaded, IDPConfig)
        assert loaded.notes == "from-json"

    def test_an_idpconfig_payload_is_accepted(self, monkeypatch):
        """An IDPConfig instance carries no flags, so it is a normal update."""
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)

        assert manager.handle_update_custom_configuration(
            IDPConfig(notes="from-model"), version="lending"
        )

        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "lending")
        assert isinstance(loaded, IDPConfig)
        assert loaded.notes == "from-model"

    def test_a_legacy_pricing_key_is_dropped_rather_than_stored(self, monkeypatch):
        """Pricing lives in its own records now; a `pricing` key in a config
        payload must not be persisted into the profile body."""
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)

        manager.handle_update_custom_configuration(
            {"saveAsVersion": True, "notes": "n", "pricing": [{"name": "x"}]},
            version="fresh",
        )

        raw = manager.get_raw_configuration(CONFIG_TYPE_CONFIG, "fresh")
        assert raw is not None
        assert "pricing" not in raw

    def test_an_empty_payload_with_no_description_change_writes_nothing(
        self, monkeypatch
    ):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG,
            IDPConfig(notes="unchanged"),
            version="lending",
            description="desc",
        )

        with patch.object(manager, "save_configuration") as spy:
            assert manager.handle_update_custom_configuration(
                {}, version="lending", description="desc"
            )
        spy.assert_not_called()

        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "lending")
        assert isinstance(loaded, IDPConfig)
        assert loaded.notes == "unchanged"

    def test_an_empty_payload_with_a_new_description_still_saves(self, monkeypatch):
        """A description-only edit must not be swallowed by the no-op check,
        and it must not blank the configuration body either."""
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG,
            IDPConfig(notes="keep"),
            version="lending",
            description="old",
        )

        assert manager.handle_update_custom_configuration(
            {}, version="lending", description="new"
        )

        record = manager._read_record(CONFIG_TYPE_CONFIG, "lending")
        assert record is not None
        assert record.description == "new"
        assert isinstance(record.config, IDPConfig)
        assert record.config.notes == "keep"

    def test_an_update_to_an_unknown_profile_starts_from_the_default(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)

        assert manager.handle_update_custom_configuration(
            {"notes": "brand-new"}, version="fresh"
        )

        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "fresh")
        assert isinstance(loaded, IDPConfig)
        assert loaded.notes == "brand-new"
        assert loaded.classification.model == SEEDED_MODEL

    def test_an_update_with_neither_profile_nor_default_starts_from_empty(
        self, monkeypatch
    ):
        """Nothing to inherit: the payload plus IDPConfig's own defaults."""
        _make_table()
        manager = _manager(monkeypatch)

        assert manager.handle_update_custom_configuration(
            {"notes": "brand-new"}, version="fresh"
        )

        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "fresh")
        assert isinstance(loaded, IDPConfig)
        assert loaded.notes == "brand-new"
        assert loaded.classification.model == PYDANTIC_DEFAULT_MODEL

    def test_a_normal_update_leaves_untouched_sections_alone(self, monkeypatch):
        """The delta is applied over the profile's *current* full config, not
        over the default, so a previous customisation is not reverted."""
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG,
            IDPConfig(notes="mine", summarization={"model": "pinned-summ-model"}),
            version="lending",
        )

        manager.handle_update_custom_configuration(
            {"classification": {"temperature": 0.25}}, version="lending"
        )

        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "lending")
        assert isinstance(loaded, IDPConfig)
        assert loaded.classification.temperature == pytest.approx(0.25)
        assert loaded.summarization.model == "pinned-summ-model"
        assert loaded.notes == "mine"

    def test_the_flag_keys_are_never_persisted_as_configuration(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)

        manager.handle_update_custom_configuration(
            {"saveAsVersion": True, "notes": "n"}, version="fresh"
        )

        raw = manager.get_raw_configuration(CONFIG_TYPE_CONFIG, "fresh")
        assert raw is not None
        for flag in ("saveAsVersion", "saveAsDefault", "resetToDefault"):
            assert flag not in raw


# ---------------------------------------------------------------------------
# _apply_deltas_with_default_restore: absent vs empty vs null.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
class TestNullMeansRestoreToDefault:
    def test_a_null_restores_the_field_from_the_default(self, monkeypatch):
        """The operator clears a field in the editor and expects the stack
        default back, not a null."""
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        target = {"notes": "customised"}

        manager._apply_deltas_with_default_restore(target, {"notes": None}, "lending")

        assert target["notes"] == "seeded-default-notes"
        assert target["notes"] is not None

    def test_a_null_for_a_key_absent_from_the_default_leaves_the_value_alone(
        self, monkeypatch
    ):
        """No default to restore, so the field must not be nulled out."""
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        target = {"not_a_config_field": "keep"}

        manager._apply_deltas_with_default_restore(
            target, {"not_a_config_field": None}, "lending"
        )

        assert target["not_a_config_field"] == "keep"

    def test_a_null_with_no_default_record_leaves_the_value_alone(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        target = {"notes": "customised"}

        manager._apply_deltas_with_default_restore(target, {"notes": None}, "lending")

        assert target["notes"] == "customised"

    def test_restoring_a_section_replaces_the_whole_block_not_just_one_key(
        self, monkeypatch
    ):
        """A null on a section means "give me the default section".

        The customised keys inside it must all go, and the default's other keys
        must all arrive — a shallow or partial restore would leave a hybrid
        section that matches neither the default nor what the user had.
        """
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        default = manager.get_configuration(CONFIG_TYPE_CONFIG, DEFAULT_VERSION)
        assert isinstance(default, IDPConfig)
        expected = default.model_dump(mode="python")["classification"]
        target = {"classification": {"model": "custom", "temperature": 0.99}}

        manager._apply_deltas_with_default_restore(
            target, {"classification": None}, "lending"
        )

        assert target["classification"] == expected
        assert target["classification"]["model"] == SEEDED_MODEL
        assert target["classification"]["temperature"] != 0.99

    @pytest.mark.parametrize("falsy", [False, 0, "", []])
    def test_a_falsy_value_is_applied_and_is_not_treated_as_a_restore(
        self, monkeypatch, falsy
    ):
        """`if value is None`, never `if not value`.

        Turning a boolean off, setting a count to 0 or clearing a list are
        real edits. Treating them as "restore to default" would silently put
        the stack default back and the operator's change would vanish on the
        next page load.
        """
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        target = {"a_field": "previous"}

        manager._apply_deltas_with_default_restore(
            target, {"a_field": falsy}, "lending"
        )

        assert target["a_field"] == falsy
        assert target["a_field"] != "seeded-default-notes"

    def test_an_empty_dict_delta_is_a_no_op_not_a_restore(self, monkeypatch):
        """Absent, empty and null are three different instructions."""
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        target = {"classification": {"model": "custom", "temperature": 0.9}}

        manager._apply_deltas_with_default_restore(
            target, {"classification": {}}, "lending"
        )

        assert target["classification"] == {"model": "custom", "temperature": 0.9}

    def test_updates_and_restores_in_one_delta_are_both_honoured(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        target = {"notes": "customised", "use_bda": False}

        manager._apply_deltas_with_default_restore(
            target, {"notes": None, "use_bda": True}, "lending"
        )

        assert target["notes"] == "seeded-default-notes"
        assert target["use_bda"] is True

    def test_a_null_restore_survives_an_end_to_end_update(self, monkeypatch):
        """The same thing through handle_update_custom_configuration, which is
        how a null actually arrives from the UI."""
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="customised"), version="lending"
        )

        manager.handle_update_custom_configuration(
            {"notes": None, "use_bda": True}, version="lending"
        )

        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "lending")
        assert isinstance(loaded, IDPConfig)
        assert loaded.notes == "seeded-default-notes"
        assert loaded.use_bda is True

    def test_setting_use_bda_false_through_an_update_is_persisted_as_false(
        self, monkeypatch
    ):
        """`use_bda` defaults to False, so a False that were mistaken for a
        restore would look correct here — pin it against a default of True."""
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(use_bda=True), version=DEFAULT_VERSION
        )
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(use_bda=True), version="lending"
        )

        manager.handle_update_custom_configuration(
            {"use_bda": False}, version="lending"
        )

        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "lending")
        assert isinstance(loaded, IDPConfig)
        assert loaded.use_bda is False


# ---------------------------------------------------------------------------
# _get_full_config_for_version
# ---------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
class TestGetFullConfigForVersion:
    def test_an_absent_profile_returns_none(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        assert manager._get_full_config_for_version("ghost") is None

    def test_a_sparse_profile_is_merged_with_the_default(self, monkeypatch):
        """A legacy sparse profile must resolve to the stack's default plus its
        own deltas, not to IDPConfig's field defaults plus its deltas."""
        table = _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        table.put_item(
            Item={
                "Configuration": f"{CONFIG_TYPE_CONFIG}#legacy",
                "notes": "delta-only",
            }
        )

        result = manager._get_full_config_for_version("legacy")

        assert result is not None
        assert result.notes == "delta-only"
        assert result.classification.model == SEEDED_MODEL
        assert result.classification.model != PYDANTIC_DEFAULT_MODEL

    def test_a_full_marked_body_that_cannot_be_validated_returns_none(
        self, monkeypatch
    ):
        """Neither the direct parse nor the merge with the default can rescue
        an invalid stored value, and None is the right answer: the caller would
        otherwise persist a config it never validated."""
        table = _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        table.put_item(
            Item={
                "Configuration": f"{CONFIG_TYPE_CONFIG}#broken",
                "_config_format": "full",
                "ocr": {},
                "classification": {},
                "extraction": {},
                "classes": "not-a-list",
            }
        )

        assert manager._get_full_config_for_version("broken") is None

    def test_an_unparseable_body_with_no_default_returns_none(self, monkeypatch):
        table = _make_table()
        manager = _manager(monkeypatch)
        table.put_item(
            Item={
                "Configuration": f"{CONFIG_TYPE_CONFIG}#broken",
                "_config_format": "full",
                "classes": "not-a-list",
            }
        )

        assert manager._get_full_config_for_version("broken") is None

    def test_a_body_that_fails_even_after_merging_returns_none(self, monkeypatch):
        """None rather than a partially valid config: the caller must not
        persist something it could not validate."""
        table = _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        table.put_item(
            Item={
                "Configuration": f"{CONFIG_TYPE_CONFIG}#broken",
                "classification": {"temperature": "not-a-number"},
            }
        )

        assert manager._get_full_config_for_version("broken") is None


# ---------------------------------------------------------------------------
# save_raw_configuration (legacy entry point)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
class TestSaveRawConfiguration:
    @pytest.mark.parametrize("empty", [None, {}])
    def test_an_empty_body_resets_the_profile_to_the_default(self, monkeypatch, empty):
        """Not "write an empty config": that would blank the profile."""
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="drifted"), version="lending"
        )

        manager.save_raw_configuration(CONFIG_TYPE_CONFIG, empty, "lending")

        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "lending")
        assert isinstance(loaded, IDPConfig)
        assert loaded.notes == "seeded-default-notes"
        assert loaded.classification.model == SEEDED_MODEL

    def test_an_empty_body_with_no_default_writes_nothing(self, monkeypatch):
        """No default to copy, so the profile must be left as it was rather
        than replaced by a defaults-only config."""
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="mine"), version="lending"
        )

        manager.save_raw_configuration(CONFIG_TYPE_CONFIG, None, "lending")

        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "lending")
        assert isinstance(loaded, IDPConfig)
        assert loaded.notes == "mine"

    def test_a_full_body_is_stored_as_given_and_not_merged(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)

        manager.save_raw_configuration(
            CONFIG_TYPE_CONFIG,
            {
                "_config_format": "full",
                "notes": "uploaded",
                "ocr": {},
                "classification": {},
                "extraction": {},
                "classes": [],
            },
            "lending",
        )

        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "lending")
        assert isinstance(loaded, IDPConfig)
        assert loaded.notes == "uploaded"
        assert loaded.classification.model == PYDANTIC_DEFAULT_MODEL, (
            "a full upload is authoritative; it does not inherit the default"
        )

    def test_a_sparse_body_is_merged_with_the_default_before_storage(self, monkeypatch):
        """The silent-loss case: storing the sparse dict as the whole config
        would replace every unsent section with IDPConfig's field defaults."""
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)

        manager.save_raw_configuration(
            CONFIG_TYPE_CONFIG, {"notes": "delta-only"}, "lending"
        )

        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "lending")
        assert isinstance(loaded, IDPConfig)
        assert loaded.notes == "delta-only"
        assert loaded.classification.model == SEEDED_MODEL
        assert loaded.summarization.model == SEEDED_MODEL

    def test_what_is_stored_is_the_full_merged_config_not_the_delta(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)

        manager.save_raw_configuration(
            CONFIG_TYPE_CONFIG, {"notes": "delta-only"}, "lending"
        )

        raw = manager.get_raw_configuration(CONFIG_TYPE_CONFIG, "lending")
        assert raw is not None
        assert _is_full_config(raw) is True

    def test_a_sparse_body_with_no_default_is_stored_on_its_own(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)

        manager.save_raw_configuration(
            CONFIG_TYPE_CONFIG, {"notes": "delta-only"}, "lending"
        )

        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "lending")
        assert isinstance(loaded, IDPConfig)
        assert loaded.notes == "delta-only"

    def test_an_invalid_sparse_body_with_no_default_raises(self, monkeypatch):
        """It must not silently store a config that failed validation."""
        _make_table()
        manager = _manager(monkeypatch)

        with pytest.raises(Exception):
            manager.save_raw_configuration(
                CONFIG_TYPE_CONFIG, {"classification": "not-a-section"}, "lending"
            )

        assert manager.get_configuration(CONFIG_TYPE_CONFIG, "lending") is None

    def test_the_description_is_carried_through(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        _seed_default(manager)

        manager.save_raw_configuration(
            CONFIG_TYPE_CONFIG, {"notes": "n"}, "lending", description="a description"
        )

        record = manager._read_record(CONFIG_TYPE_CONFIG, "lending")
        assert record is not None
        assert record.description == "a description"


# ---------------------------------------------------------------------------
# Active-version pointer and profile listing
# ---------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
class TestActiveVersionResolution:
    def test_activating_a_version_writes_the_pointer_and_deactivates_the_rest(
        self, monkeypatch
    ):
        table = _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="a")
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="b")
        manager.activate_version("a")

        manager.activate_version("b")

        assert (
            table.get_item(Key={"Configuration": ACTIVE_POINTER_KEY})["Item"][
                "ActiveVersion"
            ]
            == "b"
        )
        assert (
            table.get_item(Key={"Configuration": f"{CONFIG_TYPE_CONFIG}#a"})["Item"][
                "IsActive"
            ]
            is False
        )
        assert manager.resolve_active_version() == "b"

    def test_activation_succeeds_and_still_resolves_when_the_pointer_write_fails(
        self, monkeypatch
    ):
        """The pointer is a cache; IsActive is the source of truth. A failed
        pointer write must not leave the stack unable to resolve a version."""
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="a")

        with patch.object(
            manager.table,
            "put_item",
            side_effect=_client_error("AccessDeniedException", "PutItem"),
        ):
            manager.activate_version("a")

        assert manager.resolve_active_version() == "a"

    def test_the_pointer_sentinel_never_appears_as_a_profile(self, monkeypatch):
        """A sentinel leaking into this list shows up in the UI dropdown as a
        profile with no configuration."""
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="a")
        manager.activate_version("a")

        names = [v["versionName"] for v in manager.list_config_versions()]

        assert names == ["a"]

    def test_a_stale_pointer_is_preferred_over_the_scan(self, monkeypatch):
        """Documents the cache's failure mode: the pointer wins even when
        IsActive disagrees, so a hand-edited IsActive is not picked up until
        the next activation rewrites the pointer."""
        table = _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="a")
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="b")
        manager.activate_version("a")
        table.update_item(
            Key={"Configuration": f"{CONFIG_TYPE_CONFIG}#b"},
            UpdateExpression="SET IsActive = :t",
            ExpressionAttributeValues={":t": True},
        )

        assert manager.resolve_active_version() == "a"

    def test_an_empty_pointer_value_falls_through_to_the_scan(self, monkeypatch):
        table = _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="a")
        manager.activate_version("a")
        table.put_item(Item={"Configuration": ACTIVE_POINTER_KEY, "ActiveVersion": ""})

        assert manager.resolve_active_version() == "a"

    def test_a_failing_pointer_read_falls_back_to_the_scan(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="a")
        manager.activate_version("a")

        real_get_item = manager.table.get_item

        def flaky(**kwargs):
            if kwargs.get("Key", {}).get("Configuration") == ACTIVE_POINTER_KEY:
                raise _client_error("ProvisionedThroughputExceeded")
            return real_get_item(**kwargs)

        with patch.object(manager.table, "get_item", side_effect=flaky):
            assert manager.resolve_active_version() == "a"

    def test_a_failing_scan_falls_back_to_default_rather_than_raising(self):
        """A document must never fail because the active version is unreadable."""
        mock_table = Mock()
        mock_table.get_item.return_value = {}
        manager = _mock_table_manager(mock_table)
        with patch.object(
            manager, "list_config_versions", side_effect=RuntimeError("boom")
        ):
            assert manager.resolve_active_version() == DEFAULT_VERSION

    def test_activate_version_reraises_a_dynamodb_failure(self):
        mock_table = Mock()
        mock_table.get_item.return_value = {"Item": {"Configuration": "Config#a"}}
        mock_table.scan.return_value = {"Items": []}
        mock_table.update_item.side_effect = _client_error(
            "AccessDeniedException", "UpdateItem"
        )
        manager = _mock_table_manager(mock_table)
        with pytest.raises(ClientError):
            manager.activate_version("a")

    def test_a_failing_scan_lists_no_versions_rather_than_raising(self):
        mock_table = Mock()
        mock_table.scan.side_effect = _client_error("ProvisionedThroughputExceeded")
        manager = _mock_table_manager(mock_table)
        assert manager.list_config_versions() == []

    def test_revision_counters_are_reported_as_ints_or_none(self, monkeypatch):
        """DynamoDB returns Decimal. `None` (no history) and an integer are
        different answers to "which revision is published" and both must
        survive the conversion."""
        table = _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="a")
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="b")
        table.update_item(
            Key={"Configuration": f"{CONFIG_TYPE_CONFIG}#b"},
            UpdateExpression="SET LatestRevision = :l, PublishedRevision = :p",
            ExpressionAttributeValues={":l": 7, ":p": 6},
        )

        by_name = {v["versionName"]: v for v in manager.list_config_versions()}

        assert by_name["a"]["latestRevision"] is None
        assert by_name["a"]["publishedRevision"] is None
        assert by_name["b"]["latestRevision"] == 7
        assert isinstance(by_name["b"]["latestRevision"], int)
        assert by_name["b"]["publishedRevision"] == 6


# ---------------------------------------------------------------------------
# Revision helpers on the manager
# ---------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
class TestRevisionHelpers:
    def test_resolve_published_revision_returns_none_without_history(self, monkeypatch):
        """None means "fall back to the profile head" — the pre-revision
        behaviour. Returning 0 instead would pin documents to a revision that
        does not exist."""
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="a")
        assert manager.resolve_published_revision("a") is None

    def test_resolve_published_revision_returns_an_int(self, monkeypatch):
        _make_table(with_bucket=True)
        manager = _manager(monkeypatch, with_bucket=True)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="x"), version="a"
        )

        published = manager.resolve_published_revision("a")

        assert published == 1
        assert isinstance(published, int)
        assert not isinstance(published, Decimal)

    def test_a_stored_zero_is_reported_as_zero_not_as_absent(self, monkeypatch):
        """Absent and 0 are different stored states; conflating them is the
        `int(None)` class of bug this table has seen before."""
        table = _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="a")
        table.update_item(
            Key={"Configuration": f"{CONFIG_TYPE_CONFIG}#a"},
            UpdateExpression="SET PublishedRevision = :p",
            ExpressionAttributeValues={":p": 0},
        )

        assert manager.resolve_published_revision("a") == 0

    def test_resolve_published_revision_returns_none_on_a_read_failure(self):
        """Not a raise: the caller is stamping a document at queue time."""
        mock_table = Mock()
        mock_table.get_item.side_effect = _client_error("ProvisionedThroughputExceeded")
        manager = _mock_table_manager(mock_table)
        assert manager.resolve_published_revision("a") is None

    def test_label_revision_with_notes_only_is_recorded(self, monkeypatch):
        _make_table(with_bucket=True)
        manager = _manager(monkeypatch, with_bucket=True)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="x"), version="a"
        )

        assert manager.label_revision("a", 1, notes="why I changed it") is True

        entry = manager.list_revisions("a")[0]
        assert entry["notes"] == "why I changed it"
        assert entry["label"] is None

    def test_label_revision_truncates_an_overlong_label_and_notes(self, monkeypatch):
        _make_table(with_bucket=True)
        manager = _manager(monkeypatch, with_bucket=True)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="x"), version="a"
        )

        manager.label_revision("a", 1, label="L" * 250, notes="N" * 900)

        entry = manager.list_revisions("a")[0]
        assert len(entry["label"]) == 100
        assert len(entry["notes"]) == 500

    def test_an_empty_string_label_clears_the_label(self, monkeypatch):
        """`label[:100] or None` — an empty string is a clear, not a no-op,
        and clearing the label also removes the revision's pruning exemption."""
        _make_table(with_bucket=True)
        manager = _manager(monkeypatch, with_bucket=True)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="x"), version="a"
        )
        manager.label_revision("a", 1, label="keepme")

        assert manager.label_revision("a", 1, label="") is True

        assert manager.list_revisions("a")[0]["label"] is None

    def test_label_revision_with_nothing_to_change_reports_false_and_writes_nothing(
        self, monkeypatch
    ):
        _make_table(with_bucket=True)
        manager = _manager(monkeypatch, with_bucket=True)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="x"), version="a"
        )

        with patch.object(manager.revisions, "update_entry") as spy:
            assert manager.label_revision("a", 1) is False
        spy.assert_not_called()

    def test_deleting_the_published_revision_is_refused_and_it_survives(
        self, monkeypatch
    ):
        """Deleting the revision the head reflects would leave the profile
        with a configuration that has no recoverable history entry."""
        _make_table(with_bucket=True)
        manager = _manager(monkeypatch, with_bucket=True)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="only"), version="a"
        )
        published = manager.resolve_published_revision("a")
        assert published is not None

        with pytest.raises(ValueError, match="current configuration"):
            manager.delete_revision("a", published)

        assert manager.get_revision("a", published) is not None

    def test_an_unpublished_revision_can_be_deleted(self, monkeypatch):
        _make_table(with_bucket=True)
        manager = _manager(monkeypatch, with_bucket=True)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="r1"), version="a"
        )
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="r2"), version="a"
        )

        assert manager.delete_revision("a", 1) is True
        assert manager.get_revision("a", 1) is None
        assert manager.get_revision("a", 2) is not None

    def test_restore_returns_the_new_revision_number_not_the_restored_one(
        self, monkeypatch
    ):
        """Restoring never rewrites history: r1's body becomes r3."""
        _make_table(with_bucket=True)
        manager = _manager(monkeypatch, with_bucket=True)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="r1"), version="a"
        )
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="r2"), version="a"
        )

        new_rev = manager.restore_revision("a", 1, created_by="admin@example.test")

        assert new_rev == 3
        head = manager.get_configuration(CONFIG_TYPE_CONFIG, "a")
        assert isinstance(head, IDPConfig)
        assert head.notes == "r1"
        assert manager.get_revision("a", 2) is not None, "r2 must remain inspectable"
        assert manager.list_revisions("a")[0]["notes"] == "Restored from r1"

    def test_restoring_a_missing_revision_raises(self, monkeypatch):
        _make_table(with_bucket=True)
        manager = _manager(monkeypatch, with_bucket=True)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="x"), version="a"
        )

        with pytest.raises(ValueError, match="no longer available"):
            manager.restore_revision("a", 99)

    def test_mark_revision_pinned_is_delegated_to_the_store(self, monkeypatch):
        _make_table(with_bucket=True)
        manager = _manager(monkeypatch, with_bucket=True)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="x"), version="a"
        )

        assert manager.mark_revision_pinned("a", 1) is True
        assert manager.list_revisions("a")[0]["pinned"] is True

    def test_no_revision_is_cut_when_history_is_disabled(self, monkeypatch):
        """A deployment with no configuration bucket must still save."""
        _make_table()
        manager = _manager(monkeypatch)
        assert manager.revisions.enabled is False

        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="x"), version="a"
        )

        assert manager.list_revisions("a") == []
        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "a")
        assert isinstance(loaded, IDPConfig)
        assert loaded.notes == "x"

    def test_a_save_still_succeeds_when_the_revision_cut_fails(self, monkeypatch):
        """History is best-effort: losing an entry is recoverable, refusing
        the save is an outage."""
        _make_table(with_bucket=True)
        manager = _manager(monkeypatch, with_bucket=True)

        with patch.object(
            manager.revisions, "cut", side_effect=RuntimeError("s3 denied")
        ):
            manager.save_configuration(
                CONFIG_TYPE_CONFIG, IDPConfig(notes="x"), version="a"
            )

        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "a")
        assert isinstance(loaded, IDPConfig)
        assert loaded.notes == "x"

    def test_a_failed_backfill_does_not_prevent_the_new_revision(self, monkeypatch):
        """The pre-history backfill is separate from cutting this save's own
        revision; a failure in the first must not lose the second."""
        table = _make_table(with_bucket=True)
        manager = _manager(monkeypatch, with_bucket=True)
        # A profile that exists but has no LatestRevision: the pre-history state.
        table.put_item(
            Item={
                "Configuration": f"{CONFIG_TYPE_CONFIG}#a",
                "_config_format": "full",
                "notes": "pre-history",
            }
        )

        real_cut = manager.revisions.cut
        calls = []

        def cut(profile, body, **kwargs):
            calls.append(kwargs.get("notes"))
            if len(calls) == 1:
                raise RuntimeError("backfill failed")
            return real_cut(profile, body, **kwargs)

        with patch.object(manager.revisions, "cut", side_effect=cut):
            manager.save_configuration(
                CONFIG_TYPE_CONFIG, IDPConfig(notes="new"), version="a"
            )

        assert len(calls) == 2
        bodies = [r["revision"] for r in manager.list_revisions("a")]
        assert bodies == [1]
        assert manager.get_revision("a", 1)["notes"] == "new"

    def test_config_to_dict_copies_the_input_and_drops_the_discriminator(self):
        """Mutating the caller's dict here would strip config_type from an
        object the caller is about to validate."""
        incoming = {"config_type": "Config", "notes": "x"}

        body = ConfigurationManager._config_to_dict(incoming)

        assert body == {"notes": "x"}
        assert incoming == {"config_type": "Config", "notes": "x"}

    def test_config_to_dict_serialises_a_model_without_its_discriminator(self):
        body = ConfigurationManager._config_to_dict(IDPConfig(notes="x"))
        assert "config_type" not in body
        assert body["notes"] == "x"


# ---------------------------------------------------------------------------
# _write_record / compression edges
# ---------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
class TestWriteRecordAndCompression:
    def test_the_save_proceeds_when_reading_head_metadata_fails(self, monkeypatch):
        """A failed preservation read loses the BDA link but must not lose the
        configuration edit."""
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="p")
        manager.set_bda_project_arn("p", ARN)

        real_get_item = manager.table.get_item

        def flaky(**kwargs):
            if "ProjectionExpression" in kwargs and "BdaProjectArn" in kwargs.get(
                "ProjectionExpression", ""
            ):
                raise _client_error("ProvisionedThroughputExceeded")
            return real_get_item(**kwargs)

        with patch.object(manager.table, "get_item", side_effect=flaky):
            manager.save_configuration(
                CONFIG_TYPE_CONFIG, IDPConfig(notes="edited"), version="p"
            )

        loaded = manager.get_configuration(CONFIG_TYPE_CONFIG, "p")
        assert isinstance(loaded, IDPConfig)
        assert loaded.notes == "edited"
        assert manager.get_bda_project_arn("p") is None, (
            "the head field could not be read back, so it is genuinely lost"
        )

    def test_an_explicit_log_identifier_is_accepted(self, monkeypatch, caplog):
        _make_table()
        manager = _manager(monkeypatch)
        record = cm.ConfigurationRecord(
            configuration_type=CONFIG_TYPE_CONFIG,
            version="p",
            config=IDPConfig(notes="x"),
        )

        with caplog.at_level("INFO", logger=cm.__name__):
            manager._write_record(record, identifier="custom-log-id")

        assert "custom-log-id" in caplog.text

    def test_a_config_too_large_to_compress_is_refused_not_truncated(self, monkeypatch):
        """Silently storing a truncated item would corrupt the config; the
        write must fail so the caller sees it."""
        monkeypatch.setattr(cm, "_DYNAMODB_ITEM_SIZE_LIMIT", 256)
        item = {
            "Configuration": "Config#big",
            "notes": base64.b64encode(os.urandom(4096)).decode(),
        }

        with pytest.raises(ValueError, match="too large even after compression"):
            ConfigurationManager._compress_item(item)

    def test_a_config_approaching_the_limit_warns_but_is_still_written(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(cm, "_DYNAMODB_ITEM_SIZE_WARNING", 256)
        monkeypatch.setattr(cm, "_DYNAMODB_ITEM_SIZE_LIMIT", 10_000_000)
        item = {
            "Configuration": "Config#big",
            "notes": base64.b64encode(os.urandom(4096)).decode(),
        }

        with caplog.at_level("WARNING", logger=cm.__name__):
            compact = ConfigurationManager._compress_item(item)

        assert "approaching" in caplog.text
        assert compact["_config_storage"] == "compressed"

    def test_the_real_thresholds_are_below_the_dynamodb_400kb_limit(self):
        assert cm._DYNAMODB_ITEM_SIZE_LIMIT == 400 * 1024
        assert cm._DYNAMODB_ITEM_SIZE_WARNING < cm._DYNAMODB_ITEM_SIZE_LIMIT

    def test_compression_of_an_empty_body_does_not_divide_by_zero(self):
        compact = ConfigurationManager._compress_item({})
        assert compact["_config_storage"] == "compressed"
        assert ConfigurationManager._decompress_item(compact) == {}


@pytest.mark.unit
class TestDecompressFailureModes:
    def test_a_marked_item_with_a_non_binary_payload_is_returned_unchanged(
        self, caplog
    ):
        """The silent-corruption path worth naming.

        _decompress_item returns the raw item, so the caller's config body is
        empty and from_dynamodb_item then builds a configuration of pure
        Pydantic defaults with no exception raised. The only trace is this log
        line, so the assertion is that the marker is still present (the item
        was NOT expanded) rather than that some config appeared.
        """
        item = {
            "Configuration": "Config#p",
            "_config_storage": "compressed",
            "_compressed_config": "a string, not bytes",
        }

        with caplog.at_level("ERROR", logger=cm.__name__):
            result = ConfigurationManager._decompress_item(item)

        assert result == item
        assert "_config_storage" in result
        assert "notes" not in result
        assert "Unexpected compressed data type" in caplog.text

    def test_corrupt_gzip_bytes_are_returned_unchanged(self, caplog):
        item = {
            "Configuration": "Config#p",
            "_config_storage": "compressed",
            "_compressed_config": b"not-gzip-at-all",
        }

        with caplog.at_level("ERROR", logger=cm.__name__):
            result = ConfigurationManager._decompress_item(item)

        assert result == item
        assert "Failed to decompress" in caplog.text

    def test_valid_gzip_holding_non_json_is_returned_unchanged(self, caplog):
        item = {
            "Configuration": "Config#p",
            "_config_storage": "compressed",
            "_compressed_config": gzip.compress(b"{not json"),
        }

        with caplog.at_level("ERROR", logger=cm.__name__):
            result = ConfigurationManager._decompress_item(item)

        assert result == item
        assert "Failed to decompress" in caplog.text

    def test_metadata_survives_decompression_and_config_keys_are_restored(self):
        """Head metadata stays top-level; config keys come from the blob."""
        item = {
            "Configuration": "Config#p",
            "Description": "d",
            "IsActive": True,
            "BdaProjectArn": ARN,
            "_config_storage": "compressed",
            "_compressed_config": gzip.compress(
                json.dumps({"notes": "from-blob"}).encode()
            ),
        }

        result = ConfigurationManager._decompress_item(item)

        assert result["notes"] == "from-blob"
        assert result["Description"] == "d"
        assert result["IsActive"] is True
        assert result["BdaProjectArn"] == ARN
        assert "_config_storage" not in result, (
            "the storage marker is not part of the reconstructed item"
        )

    def test_an_unmarked_item_is_passed_through_for_legacy_inline_storage(self):
        item = {"Configuration": "Config#p", "notes": "inline"}
        assert ConfigurationManager._decompress_item(item) is item


# ---------------------------------------------------------------------------
# Legacy no-op kept for signature compatibility
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSyncCustomWithNewDefault:
    def test_the_custom_config_is_returned_unchanged_and_not_merged(self):
        """Profiles are independent snapshots; this must not resurrect the
        old auto-sync behaviour that overwrote a user's profile on deploy."""
        with patch("idp_common.config.configuration_manager.boto3"):
            manager = ConfigurationManager(table_name=TABLE)
        old_default = IDPConfig(notes="old-default")
        new_default = IDPConfig(notes="new-default")
        old_custom = IDPConfig(notes="mine")

        result = manager.sync_custom_with_new_default(
            old_default, new_default, old_custom
        )

        assert result is old_custom
        assert result.notes == "mine"
