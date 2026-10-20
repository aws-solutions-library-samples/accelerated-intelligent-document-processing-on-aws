# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every row of the backward-compatibility table in #1328, as a test.

`granularity` + `modality` + `strategy` replaced `classificationMethod`, and the
promise made alongside them is that **an existing configuration produces
identical sections and identical prompts with no edit**. These tests hold the
service to each row of that table, and the two rows most easily broken are not
the obvious ones:

- ⚠️ **A legacy holistic configuration must keep its own hand-written
  `task_prompt`.** `task_prompt` was a single key shared by both old methods and
  no packet-shaped default has ever existed in this repository — not in
  `base-classification.yaml` and not in any preset — so the only way holistic
  ever worked was a hand-written packet-shaped prompt. Handing such a
  configuration the new shipped packet default would silently replace the
  operator's own prompt with ours.
- ⚠️ **An unset modality flag must be *inferred*, not defaulted to true.** The
  documented way to get text-only classification was to delete
  `{DOCUMENT_IMAGE}` from the prompt. Defaulting `images` to `true` would start
  sending images to every configuration that used that workaround, which is a
  cost and behaviour change for the users who followed the documentation.

A fourth test pins the prompt byte-for-byte rather than asserting properties of
it, because the whole claim is "identical prompts" and a property test would
pass on a prompt that merely still contains the right placeholders.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest
import yaml

from idp_common.classification.service import ClassificationService
from idp_common.models import Document, Page

DEFAULTS = (
    __import__("pathlib").Path(__file__).resolve().parents[3]
    / "idp_common"
    / "config"
    / "system_defaults"
    / "base-classification.yaml"
)


def _classes(*names: str) -> List[Dict[str, Any]]:
    return [
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": name,
            "x-aws-idp-document-type": name,
            "type": "object",
            "description": f"A {name}",
            "properties": {},
        }
        for name in names
    ]


def _shipped_classification() -> Dict[str, Any]:
    """The classification block exactly as shipped, so these tests move when it does."""
    return yaml.safe_load(DEFAULTS.read_text())["classification"]


def _service(classification: Dict[str, Any], classes: Optional[List[str]] = None):
    return ClassificationService(
        region="us-west-2",
        config={
            "classes": _classes(*(classes or ["invoice", "receipt"])),
            "classification": classification,
        },
    )


def _document(page_count: int) -> Document:
    return Document(
        id="d",
        input_key="d.pdf",
        pages={
            str(i): Page(
                page_id=str(i),
                image_uri=f"s3://b/{i}.jpg",
                parsed_text_uri=f"s3://b/{i}.txt",
            )
            for i in range(1, page_count + 1)
        },
    )


# --------------------------------------------------------------------------
# The alias table
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestAliasTable:
    def test_the_shipped_defaults_resolve_to_page_granularity_single_pass(self):
        service = _service(_shipped_classification())
        assert service.granularity == ClassificationService.GRANULARITY_PAGE
        assert service.strategy == ClassificationService.STRATEGY_ONE_STAGE

    def test_multimodal_page_level_maps_to_page_with_text_and_images(self):
        config = _shipped_classification()
        config.pop("granularity", None)
        config["classificationMethod"] = "multimodalPageLevelClassification"
        service = _service(config)
        assert service.granularity == ClassificationService.GRANULARITY_PAGE
        assert service._resolve_modality() == (True, True)

    def test_textbased_holistic_maps_to_packet_with_text_only(self):
        config = _shipped_classification()
        # A pre-0.8 config carried no `granularity` at all — that key did not
        # exist. Removing it is what makes this a legacy config rather than a
        # current one with a stale key bolted on, where "explicit granularity
        # wins" correctly keeps `page`.
        config.pop("granularity", None)
        config["classificationMethod"] = "textbasedHolisticClassification"
        # A real legacy holistic deployment: a hand-written packet-shaped prompt
        # in the flat key, because that was the only way it worked. The
        # migration relocates it into `packet`, which is where packet
        # granularity reads it from — so the modality INFERENCE reads it there
        # too, and this config stays text-only exactly as it was.
        config["task_prompt"] = (
            "Return segments with ordinal_start_page.\n"
            "{CLASS_NAMES_AND_DESCRIPTIONS}\n{DOCUMENT_TEXT}\n"
        )
        service = _service(config)
        assert service.granularity == ClassificationService.GRANULARITY_PACKET
        assert service._resolve_modality() == (True, False)
        # And it is the operator's prompt, not the shipped packet default.
        assert service._raw_task_prompt() == config["task_prompt"]

    def test_a_prompt_without_the_image_placeholder_means_images_off(self):
        config = _shipped_classification()
        config["task_prompt"] = config["task_prompt"].replace("{DOCUMENT_IMAGE}", "")
        service = _service(config)
        assert service._resolve_modality()[1] is False

    def test_a_prompt_without_the_text_placeholder_means_text_off(self):
        config = _shipped_classification()
        config["task_prompt"] = config["task_prompt"].replace("{DOCUMENT_TEXT}", "")
        service = _service(config)
        assert service._resolve_modality()[0] is False

    @pytest.mark.parametrize("splitting", ["disabled", "page", "llm_determined"])
    def test_section_splitting_values_are_unchanged(self, splitting):
        config = _shipped_classification()
        config["sectionSplitting"] = splitting
        service = _service(config)
        assert service.config.classification.sectionSplitting == splitting

    def test_max_pages_for_classification_still_overrides_splitting(self):
        """Documented pre-existing behaviour, deliberately NOT changed: a number
        here collapses the file into one section whatever splitting says. It is
        also deliberately still VISIBLE in the panel rather than gated, because
        an option that is hidden while still in force is worse than one shown
        while inert."""
        config = _shipped_classification()
        config["maxPagesForClassification"] = "2"
        config["sectionSplitting"] = "llm_determined"
        service = _service(config)

        reply = {
            "response": {
                "output": {
                    "message": {
                        "content": [
                            {
                                "text": json.dumps(
                                    {
                                        "class": "invoice",
                                        "document_boundary": "start",
                                    }
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
            patch("idp_common.image.prepare_image", return_value=b""),
            patch.object(service, "_invoke_bedrock_model", return_value=reply),
        ):
            result = service.classify_document(_document(5))
        assert len(result.sections) == 1
        assert result.sections[0].page_ids == ["1", "2", "3", "4", "5"]


# --------------------------------------------------------------------------
# Identical prompts
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestIdenticalPrompts:
    def test_the_shipped_page_prompt_is_passed_through_unchanged(self):
        """Byte-for-byte, not property-wise: the claim is "identical prompts",
        and a property assertion would pass on a prompt that merely still holds
        the right placeholders."""
        config = _shipped_classification()
        config["confidence"] = {"mode": "off"}
        service = _service(config)
        assert (
            service._get_classification_config()["task_prompt"] == config["task_prompt"]
        )
        assert (
            service._get_classification_config()["system_prompt"]
            == config["system_prompt"]
        )

    def test_a_legacy_holistic_config_keeps_its_hand_written_prompt(self):
        """⚠️ The row most easily broken. Preferring the new shipped packet
        default here would silently replace the operator's own prompt."""
        hand_written = (
            "MY OWN HOLISTIC PROMPT: return segments with ordinal_start_page "
            "{CLASS_NAMES_AND_DESCRIPTIONS} {DOCUMENT_TEXT}"
        )
        config = _shipped_classification()
        config.pop("granularity", None)
        config["classificationMethod"] = "textbasedHolisticClassification"
        config["task_prompt"] = hand_written
        config["confidence"] = {"mode": "off"}
        service = _service(config)

        resolved = service._get_classification_config()["task_prompt"]
        assert resolved == hand_written
        # And specifically NOT the shipped packet prompt.
        assert "Analyse the document packet below" not in resolved

    def test_setting_granularity_packet_opts_into_the_shipped_packet_prompt(self):
        """The other half: once granularity is explicit, the panel shows the
        packet prompt field and the shipped default is what it holds."""
        config = _shipped_classification()
        config["granularity"] = "packet"
        config["confidence"] = {"mode": "off"}
        service = _service(config)
        resolved = service._get_classification_config()["task_prompt"]
        assert resolved == config["packet"]["task_prompt"]

    def test_an_unset_modality_leaves_a_custom_prompt_untouched(self):
        """⚠️ The documented workaround: deleting a placeholder to get one
        modality. Defaulting the flag to true would start sending images to
        every configuration that followed the documentation."""
        text_only = "classify this {CLASS_NAMES_AND_DESCRIPTIONS} {DOCUMENT_TEXT}"
        config = _shipped_classification()
        config["task_prompt"] = text_only
        config["confidence"] = {"mode": "off"}
        service = _service(config)
        assert service._get_classification_config()["task_prompt"] == text_only
        assert service._resolve_modality() == (True, False)

    def test_the_confidence_splice_is_unchanged_for_page_granularity(self):
        config = _shipped_classification()
        service = _service(config)
        prompt = service._get_classification_config()["task_prompt"]
        # Default mode is topk, so the page block is spliced as it always was.
        assert "<class-confidence>" in prompt
        assert "most likely for this page" in prompt or "for this page" in prompt


# --------------------------------------------------------------------------
# Identical sections
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestIdenticalSections:
    """The default page-level path end to end, over the three splitting
    strategies, so a refactor of the section builder cannot change the shape of
    the output for an untouched configuration."""

    def _run(self, splitting: str, boundaries: Dict[str, str]):
        config = _shipped_classification()
        config["sectionSplitting"] = splitting
        service = _service(config)

        def fake_classify_page(page_id, *_args, **_kwargs):
            from idp_common.classification.models import (
                DocumentClassification,
                PageClassification,
            )

            return PageClassification(
                page_id=page_id,
                classification=DocumentClassification(
                    doc_type="invoice",
                    confidence=0.9,
                    metadata={"document_boundary": boundaries[page_id]},
                ),
            )

        with patch.object(service, "classify_page", side_effect=fake_classify_page):
            return service.classify_document(_document(len(boundaries)))

    def test_llm_determined_splits_on_a_start_verdict(self):
        result = self._run(
            "llm_determined", {"1": "start", "2": "continue", "3": "start"}
        )
        assert [s.page_ids for s in result.sections] == [["1", "2"], ["3"]]
        assert [s.section_id for s in result.sections] == ["1", "2"]

    def test_page_gives_one_section_per_page_with_its_own_score(self):
        result = self._run("page", {"1": "start", "2": "continue"})
        assert [s.page_ids for s in result.sections] == [["1"], ["2"]]
        assert [s.confidence for s in result.sections] == [0.9, 0.9]

    def test_disabled_gives_one_section_over_every_page(self):
        result = self._run("disabled", {"1": "start", "2": "start"})
        assert len(result.sections) == 1
        assert result.sections[0].page_ids == ["1", "2"]
        assert result.sections[0].classification == "invoice"

    def test_the_boundary_signal_is_still_persisted_on_each_page(self):
        result = self._run("llm_determined", {"1": "start", "2": "continue"})
        assert result.pages["1"].document_boundary == "start"
        assert result.pages["2"].document_boundary == "continue"

    def test_metering_still_lands_under_the_classification_context(self):
        """#1328's acceptance criterion: unchanged context and units, so a cost
        comparison across this change is a straight read."""
        config = _shipped_classification()
        service = _service(config)
        assert (
            service._get_classification_config()["metering_context"] == "Classification"
        )
