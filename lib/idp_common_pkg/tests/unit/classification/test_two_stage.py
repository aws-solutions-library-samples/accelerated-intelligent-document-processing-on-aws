# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``classification.strategy: two_stage`` — segmentation, then labeling (#1329).

A single-pass call does two different jobs at once and pays for it twice. Every
boundary decision carries the whole class vocabulary — the class list, every
description and every few-shot example — although finding a boundary does not
depend on any of it, and at page granularity that prefix is paid N times. And
labeling scales with pages rather than documents: a 40-page statement is labeled
forty times to produce one answer.

Two-stage splits them. Stage A reuses granularity, modality, windowing and
context pages with a prompt that asks only where documents start, returning
segments with **no class** — which is the whole reason ``Segment.doc_class`` is
optional. Stage B makes one call per segment.

Three properties here are the ones a refactor could lose without any structural
test noticing:

- **The two halves are metered separately**, under ``Segmentation`` and
  ``Labeling``, while single-pass stays ``Classification`` with unchanged units.
  That split is what makes "is this actually cheaper on my documents?"
  answerable from the cost table rather than from arithmetic.
- **Stage A sends no class vocabulary.** If a prompt or substitution change
  quietly reintroduces it, two-stage still works and costs what single-pass
  cost, which is the one failure mode with no symptom.
- **A forced class skips Stage B entirely.** The document name already asserts
  the class, so there is nothing to label.
"""

from __future__ import annotations

import io
import json
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest
from idp_common.classification.service import ClassificationService
from idp_common.models import Document, Page

SEG_PAGE_PROMPT = (
    "Does a document start here?\n{BOUNDARY_HINTS}\n"
    "<document-ocr-data>\n{DOCUMENT_TEXT}\n</document-ocr-data>\n"
    "<document-image>\n{DOCUMENT_IMAGE}\n</document-image>\n"
)
SEG_PACKET_PROMPT = (
    "Split this packet into ranges.\n{BOUNDARY_HINTS}\n"
    "<document-text>\n{DOCUMENT_TEXT}\n</document-text>\n"
    "<document-images>\n{DOCUMENT_IMAGE}\n</document-images>\n"
)
LABEL_PROMPT = (
    "<document-types>\n{CLASS_NAMES_AND_DESCRIPTIONS}\n</document-types>\n"
    "<few-shot-examples>\n{FEW_SHOT_EXAMPLES}\n</few-shot-examples>\n"
    "<document-ocr-data>\n{DOCUMENT_TEXT}\n</document-ocr-data>\n"
    "<document-image>\n{DOCUMENT_IMAGE}\n</document-image>\n"
)


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _classes(*names: str) -> List[Dict[str, Any]]:
    return [
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": name,
            "x-aws-idp-document-type": name,
            "type": "object",
            "description": f"A {name} document with distinctive layout",
            "properties": {},
        }
        for name in names
    ]


def _service(
    *,
    granularity: str = "page",
    strategy: str = "two_stage",
    classes: Optional[List[str]] = None,
    segmentation: Optional[Dict[str, Any]] = None,
    labeling: Optional[Dict[str, Any]] = None,
    **classification: Any,
) -> ClassificationService:
    cfg: Dict[str, Any] = {
        "model": "us.amazon.nova-2-lite-v1:0",
        "system_prompt": "single pass system",
        "task_prompt": "single pass {CLASS_NAMES_AND_DESCRIPTIONS} {DOCUMENT_TEXT}",
        "granularity": granularity,
        "strategy": strategy,
        "confidence": {"mode": "off"},
        "packet": {
            "system_prompt": "packet system",
            "task_prompt": "packet {CLASS_NAMES_AND_DESCRIPTIONS} {DOCUMENT_TEXT}",
            "window_pages": "0",
        },
        "segmentation": {
            "system_prompt": "stage A system",
            "task_prompt": SEG_PAGE_PROMPT,
            "packet": {
                "system_prompt": "stage A packet system",
                "task_prompt": SEG_PACKET_PROMPT,
            },
            **(segmentation or {}),
        },
        "labeling": {
            "system_prompt": "stage B system",
            "task_prompt": LABEL_PROMPT,
            **(labeling or {}),
        },
    }
    cfg.update(classification)
    return ClassificationService(
        region="us-west-2",
        config={
            "classes": _classes(*(classes or ["invoice", "receipt"])),
            "classification": cfg,
        },
    )


def _document(page_count: int) -> Document:
    return Document(
        id="doc-1",
        input_key="doc.pdf",
        pages={
            str(i): Page(
                page_id=str(i),
                image_uri=f"s3://b/{i}.jpg",
                parsed_text_uri=f"s3://b/{i}.txt",
            )
            for i in range(1, page_count + 1)
        },
    )


def _reply(payload: Dict[str, Any], context: str, model: str = "nova") -> Dict[str, Any]:
    return {
        "response": {
            "output": {"message": {"content": [{"text": json.dumps(payload)}]}}
        },
        "metering": {f"{context}/bedrock/{model}": {"inputTokens": 10}},
    }


class _Router:
    """Answer each call according to the stage its config carries.

    The stage is on the config dict rather than inferred from the prompt,
    because that is exactly what the production code routes on — the metering
    context, the model and the prompts all come from it.
    """

    def __init__(
        self,
        *,
        boundaries: Optional[Dict[str, str]] = None,
        packet_segments: Optional[List[Dict[str, Any]]] = None,
        labels: Optional[List[str]] = None,
    ):
        self.boundaries = boundaries or {}
        self.packet_segments = packet_segments or []
        self.labels = list(labels or [])
        self.calls: List[tuple[str, List[Dict[str, Any]]]] = []

    def __call__(self, content, config):
        stage = config.get("stage")
        self.calls.append((stage, content))
        text = "".join(b["text"] for b in content if "text" in b)

        if stage == ClassificationService.STAGE_SEGMENTATION:
            if self.packet_segments:
                return _reply({"segments": self.packet_segments}, "Segmentation")
            # Page-granularity Stage A: find which page this call is about.
            for page_id, boundary in self.boundaries.items():
                if f"(document page {page_id})" in text or f"PAGE-{page_id}" in text:
                    return _reply({"document_boundary": boundary}, "Segmentation")
            return _reply({"document_boundary": "continue"}, "Segmentation")

        if stage == ClassificationService.STAGE_LABELING:
            label = self.labels.pop(0) if self.labels else "invoice"
            return _reply({"class": label}, "Labeling")

        return _reply({"class": "invoice", "document_boundary": "start"}, "Classification")


def _page_texts():
    """Text per page that the router can attribute back to a page id.

    Stage A at page granularity makes one call per page and the calls are
    indistinguishable from their stage alone, so the page's own text is what the
    router keys on.
    """

    def side_effect(uri):
        page_id = uri.rsplit("/", 1)[-1].split(".")[0]
        return f"PAGE-{page_id} content"

    return side_effect


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestStrategyConfig:
    def test_single_pass_is_the_default(self):
        from idp_common.config.models import IDPConfig

        assert IDPConfig().classification.strategy == "single_pass"

    def test_an_unknown_strategy_is_refused(self):
        with pytest.raises(Exception, match="strategy must be"):
            _service(strategy="three_stage")

    def test_an_unimplemented_labeling_method_is_refused_by_name(self):
        """'embeddings' and 'classifier' are the other two methods #1329
        describes; both need a measurement before their defaults can be set
        honestly, so the enum ships with one value and says so."""
        with pytest.raises(Exception, match="1329"):
            _service(labeling={"method": "embeddings"})

    def test_an_unknown_labeling_pages_value_is_refused(self):
        with pytest.raises(Exception, match="labeling.pages must be"):
            _service(labeling={"pages": "last"})

    def test_an_unknown_boundary_hints_value_is_refused(self):
        with pytest.raises(Exception, match="boundary_hints must be"):
            _service(segmentation={"boundary_hints": "descriptions"})


@pytest.mark.unit
class TestStagePromptResolution:
    def test_stage_a_uses_the_page_segmentation_prompt_at_page_granularity(self):
        service = _service(granularity="page")
        config = service._get_classification_config(service.STAGE_SEGMENTATION)
        assert config["task_prompt"].startswith("Does a document start here?")
        assert config["system_prompt"] == "stage A system"
        assert config["metering_context"] == "Segmentation"

    def test_stage_a_uses_the_packet_segmentation_prompt_at_packet_granularity(self):
        service = _service(granularity="packet")
        config = service._get_classification_config(service.STAGE_SEGMENTATION)
        assert config["task_prompt"].startswith("Split this packet into ranges.")
        assert config["system_prompt"] == "stage A packet system"

    def test_stage_b_is_not_granularity_dependent(self):
        """Stage B is always one call about one segment's pages, whichever way
        the boundaries were found."""
        for granularity in ("page", "packet"):
            service = _service(granularity=granularity)
            config = service._get_classification_config(service.STAGE_LABELING)
            assert config["system_prompt"] == "stage B system"
            assert config["metering_context"] == "Labeling"

    def test_single_pass_keeps_the_classification_metering_context(self):
        """Unchanged units, so a cost comparison across this change is a
        straight read."""
        service = _service(strategy="single_pass")
        config = service._get_classification_config()
        assert config["metering_context"] == "Classification"

    def test_each_stage_can_name_its_own_model(self):
        service = _service(
            segmentation={"model": "us.anthropic.claude-sonnet-5"},
            labeling={"model": "us.amazon.nova-2-lite-v1:0"},
        )
        assert (
            service._get_classification_config(service.STAGE_SEGMENTATION)["model_id"]
            == "us.anthropic.claude-sonnet-5"
        )
        assert (
            service._get_classification_config(service.STAGE_LABELING)["model_id"]
            == "us.amazon.nova-2-lite-v1:0"
        )

    def test_an_unset_stage_model_falls_back_to_the_shared_one(self):
        service = _service()
        for stage in (service.STAGE_SEGMENTATION, service.STAGE_LABELING):
            assert (
                service._get_classification_config(stage)["model_id"]
                == "us.amazon.nova-2-lite-v1:0"
            )

    def test_stage_a_asks_for_no_confidence(self):
        """Stage A reports no class, so there is no class to be confident
        about, and output tokens spent here buy a number describing nothing."""
        service = _service(confidence={"mode": "topk", "task_prompt_topk": "BLOCK"})
        prompt = service._get_classification_config(service.STAGE_SEGMENTATION)[
            "task_prompt"
        ]
        assert "BLOCK" not in prompt

    def test_stage_b_uses_the_per_page_shaped_confidence_block(self):
        """Stage B returns one class, not a segments array, so the packet block
        would describe the wrong shape."""
        service = _service(
            granularity="packet",
            confidence={
                "mode": "topk",
                "task_prompt_topk": "PAGE BLOCK {TOP_K_CANDIDATES}",
                "task_prompt_topk_packet": "PACKET BLOCK",
            },
        )
        prompt = service._get_classification_config(service.STAGE_LABELING)[
            "task_prompt"
        ]
        assert "PAGE BLOCK 2" in prompt
        assert "PACKET BLOCK" not in prompt


@pytest.mark.unit
class TestBoundaryHints:
    def test_the_default_sends_no_class_vocabulary_at_all(self):
        """⚠️ The property with no symptom if it breaks: reintroducing the
        vocabulary leaves two-stage working and costing what single-pass cost."""
        service = _service()
        assert service._format_boundary_hints() == ""

    def test_class_names_sends_names_only(self):
        service = _service(segmentation={"boundary_hints": "class_names"})
        hints = service._format_boundary_hints()
        assert "invoice" in hints and "receipt" in hints
        # Not the descriptions, which are the bulk of the prefix.
        assert "distinctive layout" not in hints

    def test_the_stage_a_request_carries_no_class_descriptions(self):
        service = _service(granularity="packet")
        document = _document(2)
        router = _Router(
            packet_segments=[{"ordinal_start_page": 1, "ordinal_end_page": 2}],
            labels=["invoice"],
        )
        with (
            patch("idp_common.s3.get_text_content", return_value="text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=router),
        ):
            service.two_stage_classify_document(document)

        stage_a = [c for stage, c in router.calls if stage == "segmentation"]
        assert stage_a
        for content in stage_a:
            text = "".join(b["text"] for b in content if "text" in b)
            assert "distinctive layout" not in text


# --------------------------------------------------------------------------
# Stage A at packet granularity
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestTwoStagePacket:
    def _run(self, service, document, router):
        with (
            patch("idp_common.s3.get_text_content", return_value="text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=router),
        ):
            return service.two_stage_classify_document(document)

    def test_one_segmentation_call_then_one_labeling_call_per_segment(self):
        service = _service(granularity="packet")
        router = _Router(
            packet_segments=[
                {"ordinal_start_page": 1, "ordinal_end_page": 2},
                {"ordinal_start_page": 3, "ordinal_end_page": 4},
            ],
            labels=["invoice", "receipt"],
        )
        result = self._run(service, _document(4), router)

        stages = [stage for stage, _ in router.calls]
        assert stages.count("segmentation") == 1
        assert stages.count("labeling") == 2
        assert [s.page_ids for s in result.sections] == [["1", "2"], ["3", "4"]]
        assert [s.classification for s in result.sections] == ["invoice", "receipt"]

    def test_stage_a_segments_need_no_type_in_the_response(self):
        """Stage A is asked for ranges only; requiring a type would reject every
        segment it correctly returns."""
        service = _service(granularity="packet")
        router = _Router(
            packet_segments=[{"ordinal_start_page": 1, "ordinal_end_page": 3}],
            labels=["invoice"],
        )
        result = self._run(service, _document(3), router)
        assert result.sections[0].page_ids == ["1", "2", "3"]

    def test_the_two_stages_are_metered_separately(self):
        service = _service(granularity="packet")
        router = _Router(
            packet_segments=[
                {"ordinal_start_page": 1, "ordinal_end_page": 1},
                {"ordinal_start_page": 2, "ordinal_end_page": 2},
            ],
            labels=["invoice", "receipt"],
        )
        result = self._run(service, _document(2), router)

        assert "Segmentation/bedrock/nova" in result.metering
        assert "Labeling/bedrock/nova" in result.metering
        assert "Classification/bedrock/nova" not in result.metering
        # One Stage A call, two Stage B calls.
        assert result.metering["Segmentation/bedrock/nova"]["inputTokens"] == 10
        assert result.metering["Labeling/bedrock/nova"]["inputTokens"] == 20

    def _router_for_two_segments(self) -> _Router:
        return _Router(
            packet_segments=[
                {"ordinal_start_page": 1, "ordinal_end_page": 1},
                {"ordinal_start_page": 2, "ordinal_end_page": 2},
            ],
            labels=["invoice", "receipt"],
        )

    def test_without_a_forced_class_every_segment_is_labeled(self):
        """The baseline the next test is measured against."""
        service = _service(granularity="packet")
        router = self._router_for_two_segments()
        self._run(service, _document(2), router)
        assert [stage for stage, _ in router.calls].count("labeling") == 2

    def test_a_forced_class_skips_stage_b_entirely(self):
        """The document's NAME asserts the class, so there is nothing to label —
        and a name-matched 40-page packet then costs its boundary calls and no
        labeling calls at all."""
        service = _service(granularity="packet")
        router = self._router_for_two_segments()

        with (
            patch("idp_common.s3.get_text_content", return_value="text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=router),
        ):
            result = service.two_stage_classify_document(
                _document(2), forced_class="receipt"
            )

        assert [stage for stage, _ in router.calls].count("labeling") == 0
        assert all(s.classification == "receipt" for s in result.sections)
        # The operator's assertion is not the model's, so it carries no score.
        assert all(s.confidence is None for s in result.sections)

    def test_stage_a_producing_nothing_fails_the_document(self):
        service = _service(granularity="packet")

        def boom(content, config):  # noqa: ARG001 - content unused, stage decides
            if config.get("stage") == "segmentation":
                raise RuntimeError("throttled")
            return _reply({"class": "invoice"}, "Labeling")

        with (
            patch("idp_common.s3.get_text_content", return_value="text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=boom),
        ):
            result = service.two_stage_classify_document(_document(2))
        assert any("Stage A" in e for e in result.errors)

    def test_a_segment_whose_labeling_fails_is_reported_not_invented(self):
        """Its boundaries are still good, and an invented class would propagate
        into extraction as though it had been decided."""
        service = _service(granularity="packet")

        def flaky(content, config):
            if config.get("stage") == "segmentation":
                return _reply(
                    {
                        "segments": [
                            {"ordinal_start_page": 1, "ordinal_end_page": 1},
                            {"ordinal_start_page": 2, "ordinal_end_page": 2},
                        ]
                    },
                    "Segmentation",
                )
            if "(document page 2)" in "".join(
                b["text"] for b in content if "text" in b
            ):
                raise RuntimeError("throttled")
            return _reply({"class": "invoice"}, "Labeling")

        with (
            patch("idp_common.s3.get_text_content", return_value="text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=flaky),
        ):
            result = service.two_stage_classify_document(_document(2))

        assert any("Stage B failed" in e for e in result.errors)
        assert result.sections[0].classification == "invoice"
        assert result.sections[1].classification is None


# --------------------------------------------------------------------------
# Stage A at page granularity
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestTwoStagePage:
    def _run(self, service, document, router):
        with (
            patch("idp_common.s3.get_text_content", side_effect=_page_texts()),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=router),
        ):
            return service.two_stage_classify_document(document)

    def test_one_boundary_call_per_page_then_one_label_call_per_segment(self):
        boundaries = {"1": "start", "2": "continue", "3": "start"}
        service = _service(granularity="page")
        router = _Router(boundaries=boundaries, labels=["invoice", "receipt"])
        result = self._run(service, _document(3), router)

        stages = [stage for stage, _ in router.calls]
        assert stages.count("segmentation") == 3
        # Two segments: pages 1-2 and page 3.
        assert stages.count("labeling") == 2
        assert [s.page_ids for s in result.sections] == [["1", "2"], ["3"]]

    def test_labeling_is_per_segment_not_per_page(self):
        """The saving: a long document is labeled once, not once per page."""
        boundaries = {str(i): ("start" if i == 1 else "continue") for i in range(1, 9)}
        service = _service(granularity="page")
        router = _Router(boundaries=boundaries, labels=["invoice"])
        result = self._run(service, _document(8), router)

        stages = [stage for stage, _ in router.calls]
        assert stages.count("segmentation") == 8
        assert stages.count("labeling") == 1
        assert result.sections[0].page_ids == [str(i) for i in range(1, 9)]

    def test_the_boundary_signal_is_persisted_on_the_page(self):
        boundaries = {"1": "start", "2": "continue"}
        service = _service(granularity="page")
        router = _Router(boundaries=boundaries, labels=["invoice"])
        result = self._run(service, _document(2), router)
        assert result.pages["1"].document_boundary == "start"
        assert result.pages["2"].document_boundary == "continue"

    def test_pages_carry_the_class_stage_b_decided(self):
        boundaries = {"1": "start", "2": "continue"}
        service = _service(granularity="page")
        router = _Router(boundaries=boundaries, labels=["receipt"])
        result = self._run(service, _document(2), router)
        assert all(p.classification == "receipt" for p in result.pages.values())


# --------------------------------------------------------------------------
# Which pages represent a segment
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestLabelingPages:
    def _segment(self, count: int):
        from idp_common.classification.models import Segment

        return Segment(page_ids=[str(i) for i in range(1, count + 1)])

    def test_first_sends_only_the_opening_page(self):
        service = _service(labeling={"pages": "first"})
        assert service._labeling_pages(self._segment(5)) == ["1"]

    def test_first_k_is_the_default_and_sends_k_pages(self):
        service = _service()
        assert service._labeling_pages(self._segment(5)) == ["1", "2"]

    def test_k_is_configurable(self):
        service = _service(labeling={"pages": "first_k", "k": "3"})
        assert service._labeling_pages(self._segment(5)) == ["1", "2", "3"]

    def test_all_sends_every_page(self):
        service = _service(labeling={"pages": "all"})
        assert service._labeling_pages(self._segment(4)) == ["1", "2", "3", "4"]

    def test_a_segment_shorter_than_k_sends_what_it_has(self):
        service = _service(labeling={"pages": "first_k", "k": "5"})
        assert service._labeling_pages(self._segment(2)) == ["1", "2"]


# --------------------------------------------------------------------------
# Class validation in Stage B
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestStageBClassValidation:
    """``enforceValidClasses`` applies here too: a class outside the vocabulary
    is exactly as wrong whether a page or a segment produced it."""

    def _run(self, service, replies):
        document = _document(2)

        calls = {"n": 0}

        def router(content, config):
            if config.get("stage") == "segmentation":
                return _reply(
                    {"segments": [{"ordinal_start_page": 1, "ordinal_end_page": 2}]},
                    "Segmentation",
                )
            index = min(calls["n"], len(replies) - 1)
            calls["n"] += 1
            return _reply({"class": replies[index]}, "Labeling")

        with (
            patch("idp_common.s3.get_text_content", return_value="text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=router),
        ):
            return service.two_stage_classify_document(document), calls["n"]

    def test_an_invalid_class_is_re_prompted(self):
        service = _service(
            granularity="packet", enforceValidClasses=True, maxValidationRetries=2
        )
        result, call_count = self._run(service, ["mystery", "invoice"])
        assert call_count == 2
        assert result.sections[0].classification == "invoice"

    def test_exhausted_retries_use_the_fallback_without_a_score(self):
        service = _service(
            granularity="packet",
            enforceValidClasses=True,
            maxValidationRetries=1,
            invalidClassFallback="unclassified",
        )
        result, call_count = self._run(service, ["mystery"])
        assert call_count == 2
        assert result.sections[0].classification == "unclassified"
        assert result.sections[0].confidence is None

    def test_validation_can_be_turned_off(self):
        service = _service(granularity="packet", enforceValidClasses=False)
        result, call_count = self._run(service, ["mystery"])
        assert call_count == 1
        assert result.sections[0].classification == "mystery"


# --------------------------------------------------------------------------
# Composition with the rest of the settings
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestComposition:
    def test_a_single_class_configuration_still_skips_both_stages(self):
        service = _service(classes=["invoice"], sectionSplitting="disabled")
        with patch.object(service, "_invoke_bedrock_model") as invoke:
            result = service.two_stage_classify_document(_document(3))
        invoke.assert_not_called()
        assert result.sections[0].classification == "invoice"

    def test_classify_document_dispatches_to_two_stage(self):
        service = _service(granularity="packet")
        router = _Router(
            packet_segments=[{"ordinal_start_page": 1, "ordinal_end_page": 2}],
            labels=["invoice"],
        )
        with (
            patch("idp_common.s3.get_text_content", return_value="text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=router),
        ):
            service.classify_document(_document(2))
        assert {stage for stage, _ in router.calls} == {"segmentation", "labeling"}

    def test_section_splitting_still_applies_to_the_labeled_segments(self):
        service = _service(granularity="packet", sectionSplitting="page")
        router = _Router(
            packet_segments=[{"ordinal_start_page": 1, "ordinal_end_page": 3}],
            labels=["invoice"],
        )
        with (
            patch("idp_common.s3.get_text_content", return_value="text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=router),
        ):
            result = service.two_stage_classify_document(_document(3))
        assert [s.page_ids for s in result.sections] == [["1"], ["2"], ["3"]]
        assert all(s.classification == "invoice" for s in result.sections)

    def test_modality_images_off_applies_to_both_stages(self):
        service = _service(granularity="packet", modality={"images": False})
        router = _Router(
            packet_segments=[{"ordinal_start_page": 1, "ordinal_end_page": 2}],
            labels=["invoice"],
        )
        with (
            patch("idp_common.s3.get_text_content", return_value="text"),
            patch("idp_common.image.prepare_image") as prepare,
            patch.object(service, "_invoke_bedrock_model", side_effect=router),
        ):
            service.two_stage_classify_document(_document(2))
        prepare.assert_not_called()
        for _, content in router.calls:
            assert not any("image" in block for block in content)

    def test_stage_a_windows_a_long_packet(self):
        service = _service(
            granularity="packet",
            packet={
                "system_prompt": "packet system",
                "task_prompt": "packet {DOCUMENT_TEXT}",
                "window_pages": "3",
                "overlap_pages": "1",
            },
        )
        router = _Router(
            packet_segments=[{"ordinal_start_page": 1, "ordinal_end_page": 3}],
            labels=["invoice"] * 10,
        )
        with (
            patch("idp_common.s3.get_text_content", return_value="text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=router),
        ):
            service.two_stage_classify_document(_document(7))
        # Windows over 7 pages at 3/1: [1-3], [3-5], [5-7].
        assert [stage for stage, _ in router.calls].count("segmentation") == 3

    def test_a_document_with_no_pages_fails_cleanly(self):
        service = _service()
        result = service.two_stage_classify_document(
            Document(id="d", input_key="d.pdf", pages={})
        )
        assert any("no pages" in e for e in result.errors)
