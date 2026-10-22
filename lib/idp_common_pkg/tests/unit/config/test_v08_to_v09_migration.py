# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Config migration v0.8 -> v0.9, attacked from the shapes real stores hold.

Every pre-0.9 profile, preset, CLI file and pinned revision reaches the current
reader through this hop, and a mistake in it does not raise: a setting that lands
in the wrong block, or in no block, is replaced by a default and the profile keeps
running — on a different model, a different prompt or different sections. So the
assertions here are about **behaviour**: the model, prompts and parameters each
call would send, and the sections a document gets, before and after.

The two frozen fixtures are what upgraded stacks actually contain:
``base-classification-0.6.12.yaml`` (the last release, config format 0.7) and
``base-classification-0.8.yaml`` (the unreleased flat 0.8 shape).
"""

from __future__ import annotations

import copy
import gzip
import json
import logging
from pathlib import Path
from typing import Any, Dict

import boto3
import pytest
import yaml
from moto import mock_aws

from idp_common.classification.service import ClassificationService
from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.constants import CONFIG_TYPE_CONFIG
from idp_common.config.merge_utils import (
    load_system_defaults,
    merge_config_with_defaults,
)
from idp_common.config.migrations import migrate_config
from idp_common.config.migrations.v08_to_v09 import (
    FLAT_KEYS,
    TARGET_VERSION,
    migrate_v08_to_v09,
)
from idp_common.config.models import (
    CONFIG_FORMAT_VERSION,
    ClassificationConfig,
    IDPConfig,
)

FIXTURES = Path(__file__).parent / "fixtures"
CALLS = (
    "one_stage.llm",
    "two_stage.segmentation.llm",
    "two_stage.section_classification.llm",
)
CLASSES = [
    {"$id": "invoice", "description": "An invoice"},
    {"$id": "receipt", "description": "A receipt"},
]


def _fixture(name: str) -> Dict[str, Any]:
    return yaml.safe_load((FIXTURES / name).read_text())


def _at(root: Any, dotted: str) -> Any:
    for key in dotted.split("."):
        root = getattr(root, key) if not isinstance(root, dict) else root[key]
    return root


def _calls(classification: ClassificationConfig):
    return {path: _at(classification, path) for path in CALLS}


def _service(config: Dict[str, Any]) -> ClassificationService:
    return ClassificationService(
        region="us-west-2", config={"classes": CLASSES, **config}
    )


# --------------------------------------------------------------------------
# 1. The shipped defaults: old and new resolve to the same configuration
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestTheShippedDefaultsAreEquivalent:
    def test_the_frozen_0_8_defaults_resolve_to_the_0_9_defaults(self):
        shipped = load_system_defaults("pattern-2")["classification"]
        frozen = _fixture("base-classification-0.8.yaml")["classification"]
        assert (
            IDPConfig(classification=frozen).classification.model_dump()
            == IDPConfig(classification=shipped).classification.model_dump()
        )

    def test_the_0_6_12_defaults_send_what_0_6_12_sent(self):
        """The last release's defaults, read now, send its prompts, its model and
        its parameters from the call single pass makes."""
        old = _fixture("base-classification-0.6.12.yaml")["classification"]
        cls = IDPConfig(classification=copy.deepcopy(old)).classification
        assert cls.strategy == "single_pass"
        assert cls.one_stage.granularity == "page"
        assert cls.one_stage.page.task_prompt == old["task_prompt"]
        assert cls.one_stage.page.system_prompt == old["system_prompt"]
        assert cls.one_stage.llm.model == old["model"]
        assert cls.one_stage.llm.temperature == float(old["temperature"])
        assert cls.one_stage.llm.validation.maxValidationRetries == int(
            old["maxValidationRetries"]
        )
        assert cls.fallback_class == old["invalidClassFallback"]
        assert cls.one_stage.llm.confidence.mode == old["confidence"]["mode"]
        assert cls.file_shape.sectionSplitting == old["sectionSplitting"]

    def test_a_0_6_12_profile_builds_the_same_request(self):
        old = _fixture("base-classification-0.6.12.yaml")
        request = _service(copy.deepcopy(old))._get_classification_config()
        assert request["model_id"] == old["classification"]["model"]
        assert request["system_prompt"] == old["classification"]["system_prompt"]
        # The confidence block is spliced in, as 0.6.12 spliced it.
        assert (
            old["classification"]["task_prompt"].split("<<CACHEPOINT>>")[0][:200]
            in request["task_prompt"]
        )
        assert "<class-confidence>" in request["task_prompt"]


# --------------------------------------------------------------------------
# 2. Sparse deltas and the merge order
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestASparseDelta:
    def test_a_flat_model_alone_reaches_every_call(self):
        """An empty stage model meant "inherit" and the stages had no parameters
        of their own, so a flat model applied to all three calls."""
        merged = merge_config_with_defaults(
            {
                "classification": {
                    "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0"
                }
            },
            validate=False,
        )
        calls = _calls(IDPConfig(**merged).classification)
        assert {c.model for c in calls.values()} == {
            "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        }

    def test_a_flat_temperature_does_not_disturb_the_default_prompts(self):
        merged = merge_config_with_defaults(
            {"classification": {"temperature": "0.4"}}, validate=False
        )
        cls = IDPConfig(**merged).classification
        defaults = IDPConfig(**merge_config_with_defaults({}, validate=False))
        assert {c.temperature for c in _calls(cls).values()} == {0.4}
        assert cls.one_stage.page == defaults.classification.one_stage.page
        assert cls.two_stage == defaults.classification.two_stage.model_copy(
            update={
                "segmentation": cls.two_stage.segmentation,
                "section_classification": cls.two_stage.section_classification,
            }
        )
        assert (
            cls.two_stage.section_classification.llm.task_prompt
            == defaults.classification.two_stage.section_classification.llm.task_prompt
        )

    def test_merging_first_gives_the_same_answer_as_migrating_first(self):
        """A flat delta deep-merged onto the nested defaults is a hybrid. The flat
        key is the newer, deliberate value there, so it must win — otherwise the
        merge-first order would silently keep the default."""
        delta = {
            "classification": {
                "model": "us.amazon.nova-lite-v1:0",
                "invalidClassFallback": "receipt",
                "granularity": "packet",
            }
        }
        migrated_first = IDPConfig(
            **merge_config_with_defaults(copy.deepcopy(delta), validate=False)
        ).classification
        defaults = merge_config_with_defaults({}, validate=False)
        hybrid = copy.deepcopy(defaults)
        hybrid["classification"].update(copy.deepcopy(delta["classification"]))
        merged_first = IDPConfig(**hybrid).classification
        assert merged_first.model_dump() == migrated_first.model_dump()
        assert merged_first.fallback_class == "receipt"
        assert merged_first.one_stage.granularity == "packet"


# --------------------------------------------------------------------------
# 3. Hybrids
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestAHybrid:
    def test_the_flat_key_wins_and_is_consumed(self):
        out = migrate_v08_to_v09(
            {
                "classification": {
                    "model": "flat-model",
                    "one_stage": {"llm": {"model": "nested-model", "temperature": 0.3}},
                }
            }
        )
        cls = out["classification"]
        assert cls["one_stage"]["llm"]["model"] == "flat-model"
        # A nested key the flat side does not mention is kept.
        assert cls["one_stage"]["llm"]["temperature"] == 0.3
        assert not FLAT_KEYS & cls.keys()

    def test_a_flat_sub_block_overrides_key_by_key(self):
        out = migrate_v08_to_v09(
            {
                "classification": {
                    "confidence": {"mode": "off"},
                    "one_stage": {
                        "llm": {"confidence": {"mode": "topk", "top_k_candidates": 4}}
                    },
                }
            }
        )
        conf = out["classification"]["one_stage"]["llm"]["confidence"]
        assert conf == {"mode": "off", "top_k_candidates": 4}


# --------------------------------------------------------------------------
# 4-5. Older formats through the whole chain
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestTheWholeChain:
    def test_a_0_7_holistic_profile_reads_as_one_stage_packet(self):
        prompt = "return segments with ordinal_start_page {DOCUMENT_TEXT}"
        cls = IDPConfig(
            classification={
                "classificationMethod": "textbasedHolisticClassification",
                "task_prompt": prompt,
                "system_prompt": "holistic system",
            }
        ).classification
        assert cls.one_stage.granularity == "packet"
        assert cls.one_stage.packet.task_prompt == prompt
        assert cls.one_stage.packet.system_prompt == "holistic system"

    def test_the_chain_ends_at_the_current_format(self):
        out = migrate_config({"classification": {"model": "m"}})
        assert out["config_format_version"] == CONFIG_FORMAT_VERSION == TARGET_VERSION


# --------------------------------------------------------------------------
# 6. Two-stage stage models and parameters (A7)
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestTwoStageStages:
    def test_an_empty_stage_model_inherits_and_an_explicit_one_is_kept(self):
        cls = IDPConfig(
            classification={
                "strategy": "two_stage",
                "model": "shared",
                "temperature": "0.2",
                "segmentation": {"model": ""},
                "labeling": {"model": "label-model"},
            }
        ).classification
        assert cls.two_stage.segmentation.llm.model == "shared"
        assert cls.two_stage.section_classification.llm.model == "label-model"
        # The parameters reached both stages, including the one with its own model.
        assert cls.two_stage.segmentation.llm.temperature == 0.2
        assert cls.two_stage.section_classification.llm.temperature == 0.2

    def test_the_packet_window_reaches_segmentation_too(self):
        cls = IDPConfig(
            classification={"packet": {"window_pages": 7, "overlap_pages": 1}}
        ).classification
        assert cls.one_stage.packet.window_pages == 7
        assert cls.two_stage.segmentation.llm.packet.window_pages == 7
        assert cls.two_stage.segmentation.llm.packet.overlap_pages == 1

    def test_stage_prompts_land_in_their_own_blocks(self):
        cls = IDPConfig(
            classification={
                "segmentation": {
                    "task_prompt": "seg page",
                    "packet": {"task_prompt": "seg packet"},
                    "boundary_hints": "class_names",
                },
                "labeling": {"task_prompt": "label", "pages": "all"},
            }
        ).classification
        seg = cls.two_stage.segmentation.llm
        assert seg.page.task_prompt == "seg page"
        assert seg.packet.task_prompt == "seg packet"
        assert seg.boundary_hints == "class_names"
        assert cls.two_stage.section_classification.llm.task_prompt == "label"
        assert cls.two_stage.section_classification.pages == "all"

    def test_confidence_reaches_the_class_assigning_calls_only(self):
        cls = IDPConfig(classification={"confidence": {"mode": "verbalized"}})
        c = cls.classification
        assert c.one_stage.llm.confidence.mode == "verbalized"
        assert c.two_stage.section_classification.llm.confidence.mode == "verbalized"
        assert not hasattr(c.two_stage.segmentation.llm, "confidence")


# --------------------------------------------------------------------------
# 7. Modality inference follows the prompt
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestModality:
    TEXT_ONLY = "classify {CLASS_NAMES_AND_DESCRIPTIONS} {DOCUMENT_TEXT}"

    def test_a_text_only_prompt_still_sends_text_only(self):
        service = _service(
            {"classification": {"system_prompt": "s", "task_prompt": self.TEXT_ONLY}}
        )
        assert service._resolve_modality() == (True, False)

    def test_a_two_stage_profile_keeps_what_its_stages_inferred(self):
        """In 0.8 every call inferred from the single-pass prompt; in 0.9 each
        infers from its own. The migration writes the old answer down so the
        stages, whose shipped prompts carry both placeholders, keep text-only."""
        service = _service(
            {
                "classification": {
                    "strategy": "two_stage",
                    "system_prompt": "s",
                    "task_prompt": self.TEXT_ONLY,
                }
            }
        )
        for stage in (service.STAGE_SEGMENTATION, service.STAGE_LABELING):
            assert service._resolve_modality(stage=stage) == (True, False), stage

    def test_an_explicit_flag_is_not_overwritten_by_inference(self):
        cls = IDPConfig(
            classification={
                "strategy": "two_stage",
                "task_prompt": self.TEXT_ONLY,
                "modality": {"images": True},
            }
        ).classification
        assert cls.two_stage.segmentation.llm.modality.images is True


# --------------------------------------------------------------------------
# 8. A page limit implies one document (decision 1)
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestThePageLimit:
    def test_a_limit_with_boundary_detection_becomes_one_document(self, caplog):
        with caplog.at_level(logging.WARNING):
            out = migrate_v08_to_v09(
                {
                    "classification": {
                        "maxPagesForClassification": "3",
                        "sectionSplitting": "llm_determined",
                    }
                }
            )
        shape = out["classification"]["file_shape"]
        assert shape == {
            "sectionSplitting": "disabled",
            "maxPagesForClassification": "3",
        }
        assert "maxPagesForClassification" in caplog.text

    def test_a_limit_with_no_splitting_key_also_becomes_one_document(self):
        out = migrate_v08_to_v09({"classification": {"maxPagesForClassification": 2}})
        assert out["classification"]["file_shape"]["sectionSplitting"] == "disabled"

    @pytest.mark.parametrize("value", ["ALL", "all", "0", 0, "", None, "abc", -1])
    def test_no_limit_leaves_splitting_alone(self, value, caplog):
        with caplog.at_level(logging.WARNING):
            out = migrate_v08_to_v09(
                {
                    "classification": {
                        "maxPagesForClassification": value,
                        "sectionSplitting": "llm_determined",
                    }
                }
            )
        assert (
            out["classification"]["file_shape"]["sectionSplitting"] == "llm_determined"
        )
        assert "maxPagesForClassification" not in caplog.text


# --------------------------------------------------------------------------
# 9. Absent is not cleared
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestAbsentIsNotCleared:
    def test_a_deliberately_empty_prompt_stays_empty(self):
        out = migrate_v08_to_v09({"classification": {"task_prompt": ""}})
        assert out["classification"]["one_stage"]["page"]["task_prompt"] == ""

    def test_an_absent_key_writes_nothing(self):
        out = migrate_v08_to_v09({"classification": {"model": "m"}})
        assert "page" not in out["classification"].get("one_stage", {})
        assert "file_shape" not in out["classification"]


# --------------------------------------------------------------------------
# 10. A pinned revision written by 0.6.12 loads, and is not rewritten
# --------------------------------------------------------------------------

TABLE = "test-config-table"
BUCKET = "test-config-bucket"


def _manager(monkeypatch):
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


@pytest.mark.unit
@mock_aws
class TestAPinnedRevision:
    def test_it_loads_with_its_own_settings_and_its_body_is_untouched(
        self, monkeypatch
    ):
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="p")
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="head"), version="p"
        )

        # Replace r1's body with what 0.6.12 wrote: a full 0.7-format config.
        old = _fixture("base-classification-0.6.12.yaml")
        old["classification"]["model"] = "us.amazon.nova-lite-v1:0"
        old["config_format_version"] = "0.7"
        key = manager.revisions.body_key("p", 1)
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.put_object(
            Bucket=BUCKET, Key=key, Body=gzip.compress(json.dumps(old).encode())
        )
        before = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()

        config = manager.get_merged_configuration("p", revision=1)

        assert config.classification.one_stage.llm.model == "us.amazon.nova-lite-v1:0"
        assert (
            config.classification.one_stage.page.task_prompt
            == old["classification"]["task_prompt"]
        )
        assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == before


# --------------------------------------------------------------------------
# 11-12. Idempotence and the version stamp
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestIdempotence:
    @pytest.mark.parametrize(
        "fixture", ["base-classification-0.6.12.yaml", "base-classification-0.8.yaml"]
    )
    def test_migrating_twice_is_migrating_once(self, fixture):
        once = migrate_config(copy.deepcopy(_fixture(fixture)))
        assert migrate_config(copy.deepcopy(once)) == once

    def test_a_current_config_is_returned_as_is(self):
        current = {
            "config_format_version": TARGET_VERSION,
            "classification": {"one_stage": {"llm": {"model": "m"}}},
        }
        assert migrate_v08_to_v09(current) is current

    def test_a_newer_stamp_is_not_downgraded(self):
        out = migrate_v08_to_v09(
            {"config_format_version": "0.10", "classification": {"model": "m"}}
        )
        assert out["config_format_version"] == "0.10"
        assert out["classification"]["one_stage"]["llm"]["model"] == "m"


# --------------------------------------------------------------------------
# 14. A validated dump reads back to itself, with no flat key in it
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestRoundTrip:
    @pytest.mark.parametrize(
        "fixture", ["base-classification-0.6.12.yaml", "base-classification-0.8.yaml"]
    )
    def test_dump_and_reload_is_stable(self, fixture):
        config = IDPConfig(**copy.deepcopy(_fixture(fixture)))
        dumped = config.model_dump(mode="python")
        assert not FLAT_KEYS & dumped["classification"].keys()
        assert IDPConfig(**copy.deepcopy(dumped)).model_dump(mode="python") == dumped


# --------------------------------------------------------------------------
# Malformed input does not crash the read
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestMalformedInput:
    @pytest.mark.parametrize(
        "classification",
        [
            {"packet": None, "segmentation": "garbage", "labeling": 3},
            {"modality": "true", "image": None, "confidence": []},
            {"granularity": None, "model": None},
        ],
    )
    def test_a_non_dict_sub_block_is_ignored_rather_than_raised(self, classification):
        out = migrate_v08_to_v09({"classification": classification})
        assert not FLAT_KEYS & out["classification"].keys()

    def test_a_non_dict_classification_is_left_alone(self):
        assert migrate_v08_to_v09({"classification": "x"}) == {
            "classification": "x",
            "config_format_version": TARGET_VERSION,
        }


# --------------------------------------------------------------------------
# Sections: the document comes out the same
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestTheSameSections:
    def test_a_limited_flat_profile_still_collapses_to_one_section(self):
        """A 0.6.12 profile with a page limit and boundary detection gave a long
        file one section from its first pages; after migration it still does."""
        from unittest.mock import patch

        from idp_common.models import Document, Page

        service = _service(
            {
                "classification": {
                    "system_prompt": "s",
                    "task_prompt": "{DOCUMENT_TEXT}",
                    "maxPagesForClassification": "2",
                    "sectionSplitting": "llm_determined",
                }
            }
        )
        document = Document(
            id="d",
            input_key="d.pdf",
            pages={
                str(i): Page(page_id=str(i), parsed_text_uri=f"s3://b/{i}.txt")
                for i in range(1, 6)
            },
        )
        reply = {
            "response": {
                "output": {
                    "message": {
                        "content": [
                            {
                                "text": json.dumps(
                                    {"class": "invoice", "document_boundary": "start"}
                                )
                            }
                        ]
                    }
                }
            },
            "metering": {},
        }
        with (
            patch("idp_common.s3.get_text_content", return_value="text"),
            patch.object(service, "_invoke_bedrock_model", return_value=reply),
        ):
            result = service.classify_document(document)
        assert [s.page_ids for s in result.sections] == [["1", "2", "3", "4", "5"]]


# --------------------------------------------------------------------------
# 15. A save from the editor after the upgrade
# --------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
class TestASaveAfterTheUpgrade:
    """The editor sends a nested delta against a profile whose stored head may
    still be in the 0.6.12 shape. The save must keep every old setting, apply the
    edit, and leave a head with no flat key in it."""

    def _plant_0_6_12_head(self, manager, profile):
        old = _fixture("base-classification-0.6.12.yaml")
        old["classification"]["model"] = "us.amazon.nova-lite-v1:0"
        old["classification"]["temperature"] = "0.3"
        old["config_format_version"] = "0.7"
        full = merge_config_with_defaults({}, validate=False)
        full.update({k: v for k, v in old.items()})
        item = {"Configuration": f"{CONFIG_TYPE_CONFIG}#{profile}", **full}
        item["_config_format"] = "full"
        manager.table.put_item(Item=ConfigurationManager._compress_item(item))
        return old

    def _stored(self, manager, profile):
        return manager.get_raw_configuration(CONFIG_TYPE_CONFIG, profile)

    def test_an_edit_keeps_the_old_settings_and_stores_no_flat_key(self, monkeypatch):
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        self._plant_0_6_12_head(manager, "p")

        delta = {
            "classification": {
                "strategy": "two_stage",
                "two_stage": {
                    "section_classification": {
                        "llm": {"model": "us.amazon.nova-pro-v1:0"}
                    }
                },
            }
        }
        assert manager.handle_update_custom_configuration(json.dumps(delta), "p")

        raw = manager.table.get_item(Key={"Configuration": f"{CONFIG_TYPE_CONFIG}#p"})[
            "Item"
        ]
        stored = ConfigurationManager._decompress_item(raw)
        assert stored["config_format_version"] == CONFIG_FORMAT_VERSION
        assert not FLAT_KEYS & stored["classification"].keys()

        cls = manager.get_merged_configuration("p").classification
        assert cls.strategy == "two_stage"
        assert (
            cls.two_stage.section_classification.llm.model == "us.amazon.nova-pro-v1:0"
        )
        # The 0.6.12 settings the edit did not touch are still the profile's.
        assert cls.one_stage.llm.model == "us.amazon.nova-lite-v1:0"
        assert cls.two_stage.segmentation.llm.model == "us.amazon.nova-lite-v1:0"
        assert cls.two_stage.segmentation.llm.temperature == 0.3

    def test_an_old_flat_upload_lands_in_every_call(self, monkeypatch):
        """An `idp-cli config-upload` of a file written for 0.6.12."""
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="p")

        upload = {"classification": {"model": "us.amazon.nova-lite-v1:0"}}
        assert manager.handle_update_custom_configuration(json.dumps(upload), "p")

        cls = manager.get_merged_configuration("p").classification
        assert {c.model for c in _calls(cls).values()} == {"us.amazon.nova-lite-v1:0"}
        assert not FLAT_KEYS & self._stored(manager, "p")["classification"].keys()
