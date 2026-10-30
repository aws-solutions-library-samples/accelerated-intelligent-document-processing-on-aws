# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The v0.9 -> v0.10 hop raises a stored ``max_log_events: 5`` to 10, and nothing else.

``parameters.max_log_events`` was ignored by the error analyzer's tools, which
applied 10 events per log group, while the setting's default was 5 and every saved
profile stores that 5. v0.10 makes the setting take effect with a default of 10, so
the hop rewrites a pre-v0.10 5 to keep each profile's effective limit (#1555).

The "touches nothing else" half is measured over real configurations: every
bundled preset, validated and dumped the way a profile head stores it (numbers as
strings), stamped v0.9 with the old default written back in, and diffed leaf by leaf
against the hop's output.
"""

import copy
import json
from pathlib import Path
from typing import Any, Dict, Iterator, Tuple

import boto3
import pytest
import yaml
from moto import mock_aws

from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.constants import CONFIG_TYPE_CONFIG
from idp_common.config.merge_utils import merge_config_with_defaults
from idp_common.config.migrations import migrate_config
from idp_common.config.migrations.v09_to_v010 import (
    PARAMETER_SECTIONS,
    TARGET_VERSION,
    migrate_v09_to_v010,
)
from idp_common.config.models import (
    CONFIG_FORMAT_VERSION,
    ConfigurationRecord,
    ErrorAnalyzerParameters,
    IDPConfig,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[5]
TABLE = "v010-config-table"
BUCKET = "v010-config-bucket"

#: The two trees under ``config_library/`` that hold configuration presets. Every
#: YAML file under them is a configuration, so presets are selected by location and
#: nothing has to be filtered out.
_PRESET_ROOTS = ("unified", "managed_config")


def _presets():
    paths = sorted(
        p
        for root in _PRESET_ROOTS
        for p in (_REPO_ROOT / "config_library" / root).rglob("*.yaml")
    )
    return [pytest.param(p, id=str(p.relative_to(_REPO_ROOT))) for p in paths]


def _profile(stamp: Any, **sections: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "agents": {
            name: {"parameters": {"max_log_events": value}}
            for name, value in sections.items()
        }
    }
    if stamp is not None:
        data["config_format_version"] = stamp
    return data


def _leaves(node: Any, path: Tuple[Any, ...] = ()) -> Iterator[Tuple[Tuple, Any]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _leaves(value, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _leaves(value, path + (index,))
    else:
        yield path, node


def _changed_paths(before: Any, after: Any) -> set:
    a, b = dict(_leaves(before)), dict(_leaves(after))
    return {p for p in a.keys() | b.keys() if a.get(p, object()) != b.get(p, object())}


def test_the_current_format_is_this_hop():
    assert CONFIG_FORMAT_VERSION == TARGET_VERSION


def test_the_old_default_is_the_one_the_hop_rewrites():
    # The hop's premise: the shipped default is now 10. If it moves again, a new
    # hop is needed rather than an edit to this one.
    assert ErrorAnalyzerParameters().max_log_events == 10


class TestRewrite:
    @pytest.mark.parametrize("stamp", ["0.9", "0.8", "0.6", None, ""])
    @pytest.mark.parametrize(
        ("stored", "expected"), [(5, 10), ("5", "10"), (" 5 ", "10")]
    )
    def test_a_pre_v010_five_becomes_ten_in_both_sections(
        self, stamp, stored, expected
    ):
        out = migrate_v09_to_v010(
            _profile(stamp, error_analyzer=stored, chat_companion=stored)
        )
        for section in PARAMETER_SECTIONS:
            assert out["agents"][section]["parameters"]["max_log_events"] == expected
        assert out["config_format_version"] == TARGET_VERSION

    @pytest.mark.parametrize("stored", [1, 3, "3", 10, "10", 50, "50", True, None, ""])
    def test_any_other_value_is_kept(self, stored):
        out = migrate_v09_to_v010(_profile("0.9", error_analyzer=stored))
        kept = out["agents"]["error_analyzer"]["parameters"]["max_log_events"]
        assert kept == stored and type(kept) is type(stored)
        assert out["config_format_version"] == TARGET_VERSION

    @pytest.mark.parametrize("stamp", ["0.10", "0.11", "1.0"])
    def test_a_configuration_at_or_after_v010_is_returned_untouched(self, stamp):
        cfg = _profile(stamp, error_analyzer="5", chat_companion=5)
        before = copy.deepcopy(cfg)
        out = migrate_v09_to_v010(cfg)
        assert out is cfg
        assert out == before

    def test_the_input_is_not_mutated(self):
        cfg = _profile("0.9", error_analyzer=5)
        before = copy.deepcopy(cfg)
        migrate_v09_to_v010(cfg)
        assert cfg == before

    def test_idempotent(self):
        once = migrate_v09_to_v010(_profile("0.9", error_analyzer=5))
        assert migrate_v09_to_v010(once) is once

    @pytest.mark.parametrize(
        "cfg",
        [
            {"config_format_version": "0.9"},
            {"config_format_version": "0.9", "agents": None},
            {"config_format_version": "0.9", "agents": {"error_analyzer": None}},
            {
                "config_format_version": "0.9",
                "agents": {"error_analyzer": {"parameters": None}},
            },
            {
                "config_format_version": "0.9",
                "agents": {"error_analyzer": {"parameters": {}}},
            },
        ],
    )
    def test_absent_sections_only_get_the_stamp(self, cfg):
        out = migrate_v09_to_v010(cfg)
        assert _changed_paths(cfg, out) == {("config_format_version",)}

    def test_not_a_dict_passes_through(self):
        assert migrate_v09_to_v010(["x"]) == ["x"]

    def test_the_chain_applies_it(self):
        out = migrate_config(_profile("0.5", error_analyzer="5"))
        assert out["agents"]["error_analyzer"]["parameters"]["max_log_events"] == "10"
        assert out["config_format_version"] == CONFIG_FORMAT_VERSION

    def test_a_stored_profile_loads_with_ten(self):
        # The loading path every reader of a stored profile takes.
        config = IDPConfig.model_validate(
            _profile("0.9", error_analyzer="5", chat_companion="5")
        )
        assert config.agents.error_analyzer is not None
        assert config.agents.chat_companion is not None
        assert config.agents.error_analyzer.parameters.max_log_events == 10
        assert config.agents.chat_companion.parameters.max_log_events == 10


class TestBundledPresets:
    def test_discovery_found_presets(self):
        assert len(_presets()) >= 20

    @pytest.mark.parametrize("path", _presets())
    def test_the_hop_changes_only_max_log_events_and_the_stamp(self, path: Path):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        pattern = "pattern-1" if raw.get("use_bda") else "pattern-2"
        merged = merge_config_with_defaults(raw, pattern=pattern, validate=False)
        item = ConfigurationRecord(
            configuration_type="Config",
            version="p",
            config=IDPConfig.model_validate(merged),
        ).to_dynamodb_item()
        stored = {
            k: v for k, v in item.items() if k not in ("Configuration", "Managed")
        }
        stored = _as_pre_v010(stored)

        out = migrate_v09_to_v010(stored)

        expected = {("config_format_version",)} | {
            ("agents", s, "parameters", "max_log_events")
            for s in PARAMETER_SECTIONS
            if isinstance(stored.get("agents", {}).get(s), dict)
        }
        assert _changed_paths(stored, out) == expected
        for section in PARAMETER_SECTIONS:
            if isinstance(stored.get("agents", {}).get(section), dict):
                params = out["agents"][section]["parameters"]
                assert params["max_log_events"] == "10"


def _as_pre_v010(stored: Dict[str, Any]) -> Dict[str, Any]:
    """The same profile as a pre-v0.10 release stored it: stamped 0.9, events "5"."""
    old = copy.deepcopy(stored)
    old["config_format_version"] = "0.9"
    for section in PARAMETER_SECTIONS:
        section_cfg = old.get("agents", {}).get(section)
        if isinstance(section_cfg, dict):
            section_cfg.setdefault("parameters", {})["max_log_events"] = "5"
    return old


class TestAConfigurationBeingSaved:
    """``unstamped_is_current``: an editor delta or uploaded file is current input.

    An editor delta carries no ``config_format_version``. Read as stored data, a 5
    in it would be taken for the pre-v0.10 default and saved as 10.
    """

    def test_an_unstamped_five_is_kept(self):
        out = migrate_config(
            _profile(None, error_analyzer=5, chat_companion="5"),
            unstamped_is_current=True,
        )
        assert out["agents"]["error_analyzer"]["parameters"]["max_log_events"] == 5
        assert out["agents"]["chat_companion"]["parameters"]["max_log_events"] == "5"
        assert out["config_format_version"] == CONFIG_FORMAT_VERSION

    def test_a_file_stamped_older_still_has_its_five_raised(self):
        out = migrate_config(
            _profile("0.9", error_analyzer=5), unstamped_is_current=True
        )
        assert out["agents"]["error_analyzer"]["parameters"]["max_log_events"] == 10

    def test_the_key_moving_hops_still_run(self):
        legacy = {"extraction": {"agentic": {"validation": {"fail_action": "reject"}}}}
        out = migrate_config(copy.deepcopy(legacy), unstamped_is_current=True)
        assert out["extraction"]["validation"] == {"fail_action": "reject"}
        assert "agentic" not in out["extraction"]

    def test_the_input_is_not_mutated(self):
        cfg = _profile(None, error_analyzer=5)
        before = copy.deepcopy(cfg)
        migrate_config(cfg, unstamped_is_current=True)
        assert cfg == before

    @pytest.mark.parametrize(
        "legacy",
        [
            {"assessment": {"enabled": True}},
            {"extraction": {"assessment_integration": {}}},
            {"extraction": {"agentic": {"validation": {"fail_action": "reject"}}}},
            {"classification": {"classificationMethod": "multimodalPageLevel"}},
            {"classification": {"model": "us.amazon.nova-lite-v1:0"}},
        ],
        ids=["v05-assessment", "v05-integration", "v06", "v07", "v08"],
    )
    def test_an_unstamped_input_written_for_an_earlier_format_is_raised(self, legacy):
        # A full download from a release before v0.6 carries no stamp but does
        # carry the retired `assessment` block, and holds the old default 5.
        cfg = {**copy.deepcopy(legacy), **_profile(None, error_analyzer=5)}
        out = migrate_config(cfg, unstamped_is_current=True)
        assert out["agents"]["error_analyzer"]["parameters"]["max_log_events"] == 10

    @pytest.mark.parametrize("stamp", ["", "  ", "garbage", 0.9])
    def test_an_unreadable_stamp_is_not_taken_for_current(self, stamp):
        cfg = _profile(None, error_analyzer=5)
        cfg["config_format_version"] = stamp
        out = migrate_config(cfg, unstamped_is_current=True)
        assert out["agents"]["error_analyzer"]["parameters"]["max_log_events"] == 10

    def test_a_deploy_time_file_is_read_as_stored_data(self):
        # The stack's CustomConfigPath file and `idp-cli config-validate` go
        # through merge_config_with_defaults, which does not pass the flag.
        merged = merge_config_with_defaults(
            _profile(None, error_analyzer=5), pattern="pattern-2", validate=False
        )
        assert merged["agents"]["error_analyzer"]["parameters"]["max_log_events"] == 10

    def test_without_it_an_unstamped_five_is_read_as_stored_data(self):
        out = migrate_config(_profile(None, error_analyzer=5))
        assert out["agents"]["error_analyzer"]["parameters"]["max_log_events"] == 10


def _manager(monkeypatch) -> ConfigurationManager:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", TABLE)
    monkeypatch.setenv("CONFIGURATION_BUCKET", BUCKET)
    boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "Configuration", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    return ConfigurationManager()


def _events(config: IDPConfig) -> Tuple[int, int]:
    assert config.agents.error_analyzer is not None
    assert config.agents.chat_companion is not None
    return (
        config.agents.error_analyzer.parameters.max_log_events,
        config.agents.chat_companion.parameters.max_log_events,
    )


@mock_aws
class TestSavesAndReadsThroughTheManager:
    """The save path (editor, ``idp-cli config-upload``, SDK) and the read path."""

    def _plant_pre_v010_head(
        self, manager: ConfigurationManager, profile: str, stamp: Any = "0.9"
    ):
        """A profile head as an earlier release stored it: stamped 0.9, "5"."""
        item = ConfigurationRecord(
            configuration_type=CONFIG_TYPE_CONFIG,
            version=profile,
            config=IDPConfig(),
        ).to_dynamodb_item()
        item = _as_pre_v010(item)
        if stamp is None:
            item.pop("config_format_version")
        else:
            item["config_format_version"] = stamp
        item["_config_format"] = "full"
        manager.table.put_item(Item=ConfigurationManager._compress_item(item))

    def test_an_unstamped_head_reads_as_ten(self, monkeypatch):
        # A head saved by a release before the stamp existed carries none.
        manager = _manager(monkeypatch)
        self._plant_pre_v010_head(manager, "p", stamp=None)

        assert _events(manager.get_merged_configuration("p")) == (10, 10)
        raw = manager.get_raw_configuration(CONFIG_TYPE_CONFIG, "p")
        assert raw is not None
        assert raw["agents"]["error_analyzer"]["parameters"]["max_log_events"] == "10"

    def test_a_pre_v06_download_uploaded_again_keeps_ten(self, monkeypatch):
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="p")

        upload = {
            "assessment": {"enabled": True},
            **_profile(None, error_analyzer=5, chat_companion=5),
        }
        assert manager.handle_update_custom_configuration(json.dumps(upload), "p")

        assert _events(manager.get_merged_configuration("p")) == (10, 10)

    def test_an_editor_delta_of_five_is_saved_as_five(self, monkeypatch):
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="p")

        delta = _profile(None, error_analyzer=5, chat_companion="5")
        assert manager.handle_update_custom_configuration(json.dumps(delta), "p")

        assert _events(manager.get_merged_configuration("p")) == (5, 5)

    def test_an_upload_stamped_older_is_raised_on_save(self, monkeypatch):
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="p")

        upload = _profile("0.9", error_analyzer="5")
        assert manager.handle_update_custom_configuration(json.dumps(upload), "p")

        assert _events(manager.get_merged_configuration("p"))[0] == 10

    def test_a_pre_v010_head_reads_as_ten(self, monkeypatch):
        manager = _manager(monkeypatch)
        self._plant_pre_v010_head(manager, "p")

        assert _events(manager.get_merged_configuration("p")) == (10, 10)

    def test_an_unrelated_edit_to_a_pre_v010_head_keeps_ten(self, monkeypatch):
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        self._plant_pre_v010_head(manager, "p")

        assert manager.handle_update_custom_configuration(
            json.dumps({"notes": "edited"}), "p"
        )

        assert _events(manager.get_merged_configuration("p")) == (10, 10)
        # The head the save wrote, read without the migration a read applies.
        item = manager.table.get_item(Key={"Configuration": f"{CONFIG_TYPE_CONFIG}#p"})
        stored = ConfigurationManager._decompress_item(item["Item"])
        assert stored["config_format_version"] == CONFIG_FORMAT_VERSION
        params = stored["agents"]["error_analyzer"]["parameters"]
        assert str(params["max_log_events"]) == "10"
