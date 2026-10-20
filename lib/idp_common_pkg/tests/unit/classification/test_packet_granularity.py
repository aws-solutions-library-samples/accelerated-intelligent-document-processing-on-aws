# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``classification.granularity: packet`` — one call per packet, with images (#1328).

Packet-level classification existed as ``classificationMethod:
textbasedHolisticClassification`` and three things were impossible with it:

- **It could not see images.** The call sent one text block and nothing else, so
  no setting produced packet-level classification that could read page layout —
  and layout is most of what tells you where a document starts.
- **It never substituted ``{FEW_SHOT_EXAMPLES}``**, the same defect the
  context-pages path had, so a configured example reached the model as a literal
  placeholder string.
- **It had no windowing.** A packet larger than the context window failed
  outright.

⚠️ **And it was unreachable from the UI.** ``task_prompt`` was a single key
shared by both methods and there has never been a packet-shaped default anywhere
in the tree — not in ``base-classification.yaml``, not in any preset. Selecting
holistic in the dropdown therefore sent the page prompt (which asks for
``{"class", "document_boundary"}``) to the segments parser, and the document
failed with "No segments found in the classification result". That is why
``granularity`` gets its own prompt keys under ``classification.packet`` rather
than reusing the flat ones, and why a *legacy* holistic configuration — method
set, granularity empty — must keep using its own hand-written flat prompt
instead of being handed the new default.
"""

from __future__ import annotations

import io
import json
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from idp_common.classification.models import Segment
from idp_common.classification.service import ClassificationService
from idp_common.models import Document, Page

PACKET_PROMPT = (
    "<document-types>\n{CLASS_NAMES_AND_DESCRIPTIONS}\n</document-types>\n"
    "<few-shot-examples>\n{FEW_SHOT_EXAMPLES}\n</few-shot-examples>\n"
    "Return a segments array.\n"
    "<document-text>\n{DOCUMENT_TEXT}\n</document-text>\n"
    "<document-images>\n{DOCUMENT_IMAGE}\n</document-images>\n"
)


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _classes(*names: str, examples: bool = False) -> List[Dict[str, Any]]:
    out = []
    for name in names:
        entry: Dict[str, Any] = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": name,
            "x-aws-idp-document-type": name,
            "type": "object",
            "description": f"A {name}",
            "properties": {},
        }
        if examples:
            entry["x-aws-idp-examples"] = [
                {
                    "x-aws-idp-name": f"{name}-example",
                    "x-aws-idp-class-prompt": f"A {name} looks like this.",
                }
            ]
        out.append(entry)
    return out


def _service(
    *,
    granularity: str = "packet",
    classes: Optional[List[Dict[str, Any]]] = None,
    packet: Optional[Dict[str, Any]] = None,
    **classification: Any,
) -> ClassificationService:
    cfg: Dict[str, Any] = {
        "model": "us.amazon.nova-2-lite-v1:0",
        "system_prompt": "You classify pages.",
        "task_prompt": "page prompt {DOCUMENT_TEXT} {DOCUMENT_IMAGE}",
        "granularity": granularity,
        "confidence": {"mode": "off"},
        "packet": {
            "system_prompt": "You split packets.",
            "task_prompt": PACKET_PROMPT,
            "window_pages": "0",
            "overlap_pages": "2",
            **(packet or {}),
        },
    }
    cfg.update(classification)
    return ClassificationService(
        region="us-west-2",
        config={
            "classes": classes
            if classes is not None
            else _classes("invoice", "receipt"),
            "classification": cfg,
        },
    )


def _document(page_count: int, *, with_images: bool = True) -> Document:
    return Document(
        id="doc-1",
        input_key="doc.pdf",
        pages={
            str(i): Page(
                page_id=str(i),
                image_uri=f"s3://b/{i}.jpg" if with_images else None,
                parsed_text_uri=f"s3://b/{i}.txt",
            )
            for i in range(1, page_count + 1)
        },
    )


def _bedrock_reply(segments: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "response": {
            "output": {
                "message": {"content": [{"text": json.dumps({"segments": segments})}]}
            }
        },
        "metering": {"Classification/bedrock/nova": {"inputTokens": 100}},
    }


# --------------------------------------------------------------------------
# Granularity resolution and back-compatibility
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestGranularityResolution:
    """`granularity` is now the only setting: `classificationMethod` was removed
    in config format 0.8 and is converted to it on every config read by
    `migrations.v07_to_v08`. The conversion — including the prompt relocation a
    holistic config needs — is covered in
    `tests/unit/config/test_v07_to_v08_migration.py`; what is left here is that
    the service reads the resolved value."""

    def test_the_default_is_page(self):
        service = _service(granularity="")
        assert service.granularity == ClassificationService.GRANULARITY_PAGE

    @pytest.mark.parametrize("value", ["page", "packet"])
    def test_the_configured_value_is_what_the_service_uses(self, value):
        assert _service(granularity=value).granularity == value

    def test_a_legacy_config_is_migrated_before_the_service_sees_it(self):
        """End to end through the real config path, so the service and the
        migration cannot disagree about what a stored config means."""
        service = _service(
            granularity="",
            classificationMethod="textbasedHolisticClassification",
        )
        assert service.granularity == ClassificationService.GRANULARITY_PACKET

    def test_an_unknown_granularity_is_refused_rather_than_defaulted(self):
        """The two values produce different prompts, different parsers and an
        N-fold difference in call count, so picking one silently would be a cost
        and accuracy decision the operator did not make."""
        with pytest.raises(Exception, match="granularity must be"):
            _service(granularity="holistic")

    def test_the_deprecated_field_is_gone_from_the_model(self):
        """Not hidden, removed. A dropdown saying "deprecated, ignored when
        granularity is set" is a worse UI than no dropdown, and two readable
        homes for one setting needs a precedence rule every future reader has to
        know about."""
        service = _service(granularity="page")
        assert not hasattr(service.config.classification, "classificationMethod")
        assert not hasattr(service, "classification_method")


@pytest.mark.unit
class TestPromptResolution:
    def test_packet_granularity_uses_the_packet_prompts(self):
        service = _service(granularity="packet")
        config = service._get_classification_config()
        assert "Return a segments array." in config["task_prompt"]
        assert config["system_prompt"] == "You split packets."

    def test_page_granularity_uses_the_flat_prompts(self):
        service = _service(granularity="page")
        config = service._get_classification_config()
        assert config["task_prompt"].startswith("page prompt")
        assert config["system_prompt"] == "You classify pages."

    def test_a_legacy_holistic_config_keeps_its_own_hand_written_prompt(self):
        """⚠️ The back-compat case that matters most, and it is now the
        migration that delivers it: the only way holistic ever worked was a
        hand-written packet-shaped flat task_prompt, so `granularity: packet`
        alone would have the service read the shipped `packet.task_prompt`
        instead — ours, not theirs. `migrations.v07_to_v08` relocates the flat
        prompt into `packet` for exactly this reason.

        The config here carries NO `packet` block, which is the real legacy
        shape; a config that already had one is the "existing packet prompt
        wins" case covered in the migration suite.
        """
        service = ClassificationService(
            region="us-west-2",
            config={
                "classes": _classes("invoice", "receipt"),
                "classification": {
                    "model": "us.amazon.nova-2-lite-v1:0",
                    "classificationMethod": "textbasedHolisticClassification",
                    "system_prompt": "You split packets, by hand.",
                    "task_prompt": "my own holistic prompt {DOCUMENT_TEXT}",
                    "confidence": {"mode": "off"},
                },
            },
        )
        assert service.granularity == ClassificationService.GRANULARITY_PACKET
        config = service._get_classification_config()
        assert config["task_prompt"] == "my own holistic prompt {DOCUMENT_TEXT}"
        assert config["system_prompt"] == "You split packets, by hand."

    def test_an_empty_packet_prompt_falls_back_and_warns(self, caplog):
        import logging

        service = _service(granularity="packet", packet={"task_prompt": ""})
        with caplog.at_level(logging.WARNING):
            config = service._get_classification_config()
        assert config["task_prompt"].startswith("page prompt")
        assert "written for PAGE granularity" in caplog.text


@pytest.mark.unit
class TestPacketConfidenceBlocks:
    def test_packet_granularity_splices_the_per_segment_block(self):
        service = _service(
            granularity="packet",
            confidence={
                "mode": "topk",
                "top_k_candidates": 3,
                "task_prompt_topk": "PAGE BLOCK for this page",
                "task_prompt_topk_packet": "PACKET BLOCK per segment "
                "{TOP_K_CANDIDATES}",
            },
        )
        prompt = service._get_classification_config()["task_prompt"]
        assert "PACKET BLOCK per segment 2" in prompt
        assert "PAGE BLOCK" not in prompt

    def test_an_absent_packet_block_falls_back_to_the_page_block(self):
        """Keeps a custom configuration written before packet granularity
        working, rather than silently dropping the confidence request."""
        service = _service(
            granularity="packet",
            confidence={
                "mode": "verbalized",
                "task_prompt_verbalized": "PAGE BLOCK",
                "task_prompt_verbalized_packet": "",
            },
        )
        assert "PAGE BLOCK" in service._get_classification_config()["task_prompt"]

    def test_a_migrated_legacy_config_gets_the_per_segment_block(self):
        """A legacy holistic config used to get NO composed block, with a
        warning, because its prompt was hand-written and there was no output
        shape to describe. The migration relocates that prompt into
        `classification.packet`, so there is no longer a packet config without a
        packet prompt — and the per-segment block is always the right one."""
        service = ClassificationService(
            region="us-west-2",
            config={
                "classes": _classes("invoice", "receipt"),
                "classification": {
                    "model": "us.amazon.nova-2-lite-v1:0",
                    "classificationMethod": "textbasedHolisticClassification",
                    "system_prompt": "You split packets, by hand.",
                    "task_prompt": "my own prompt {DOCUMENT_TEXT}",
                    "confidence": {
                        "mode": "topk",
                        "task_prompt_topk": "PAGE BLOCK",
                        "task_prompt_topk_packet": "PACKET BLOCK per segment",
                    },
                },
            },
        )
        prompt = service._get_classification_config()["task_prompt"]
        assert "PACKET BLOCK per segment" in prompt
        assert "PAGE BLOCK" not in prompt
        # And it is still the operator's own prompt that was composed into.
        assert "my own prompt" in prompt


# --------------------------------------------------------------------------
# Windowing
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestWindowing:
    def test_zero_window_pages_never_windows(self):
        """Legacy holistic behaviour, kept reachable: the only way to guarantee
        the model sees the packet as one whole."""
        service = _service(packet={"window_pages": "0"})
        pages = [str(i) for i in range(1, 51)]
        assert service._packet_windows(pages) == [pages]

    def test_a_packet_smaller_than_the_window_is_one_window(self):
        service = _service(packet={"window_pages": "20", "overlap_pages": "2"})
        pages = [str(i) for i in range(1, 11)]
        assert service._packet_windows(pages) == [pages]

    def test_a_packet_exactly_the_window_size_is_one_window(self):
        service = _service(packet={"window_pages": "10", "overlap_pages": "2"})
        pages = [str(i) for i in range(1, 11)]
        assert service._packet_windows(pages) == [pages]

    def test_a_larger_packet_is_split_with_overlap(self):
        service = _service(packet={"window_pages": "5", "overlap_pages": "2"})
        windows = service._packet_windows([str(i) for i in range(1, 13)])
        assert windows == [
            ["1", "2", "3", "4", "5"],
            ["4", "5", "6", "7", "8"],
            ["7", "8", "9", "10", "11"],
            ["10", "11", "12"],
        ]

    def test_every_page_appears_in_at_least_one_window(self):
        service = _service(packet={"window_pages": "7", "overlap_pages": "3"})
        pages = [str(i) for i in range(1, 41)]
        covered = {pid for window in service._packet_windows(pages) for pid in window}
        assert covered == set(pages)

    def test_zero_overlap_produces_disjoint_windows(self):
        service = _service(packet={"window_pages": "4", "overlap_pages": "0"})
        windows = service._packet_windows([str(i) for i in range(1, 10)])
        assert windows == [
            ["1", "2", "3", "4"],
            ["5", "6", "7", "8"],
            ["9"],
        ]

    def test_an_overlap_at_least_as_large_as_the_window_is_refused(self):
        """Each window would cover no new pages, so a long packet would cost one
        call per page while claiming to be the cost-saving granularity."""
        with pytest.raises(Exception, match="must be smaller than window_pages"):
            _service(packet={"window_pages": "4", "overlap_pages": "4"})


# --------------------------------------------------------------------------
# Merging windows
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestMergeWindowSegments:
    def test_a_single_window_is_used_directly(self):
        """Identical to the unwindowed path that preceded this, which is the
        common case and the one with years of behaviour behind it."""
        service = _service()
        segments = [Segment(page_ids=["1", "2"], doc_class="invoice", confidence=0.7)]
        assert service._merge_window_segments([segments], ["1", "2"]) is segments

    def test_the_earlier_window_wins_for_an_overlapping_page(self):
        """The overlap pages are window k's trailing pages and window k+1's
        leading pages. Window k saw everything before them; window k+1 sees only
        what follows. 'Does a new document start here?' is answered mostly by
        what came before, so window k's verdict is taken."""
        service = _service()
        merged = service._merge_window_segments(
            [
                [Segment(page_ids=["1", "2", "3"], doc_class="invoice")],
                [Segment(page_ids=["3", "4"], doc_class="receipt")],
            ],
            ["1", "2", "3", "4"],
        )
        # Page 3 keeps invoice from the first window.
        assert [(s.page_ids, s.doc_class) for s in merged] == [
            (["1", "2", "3"], "invoice"),
            (["4"], "receipt"),
        ]

    def test_a_boundary_inside_a_later_window_survives_the_merge(self):
        service = _service()
        merged = service._merge_window_segments(
            [
                [Segment(page_ids=["1", "2", "3"], doc_class="invoice")],
                [
                    Segment(page_ids=["3", "4"], doc_class="invoice"),
                    Segment(page_ids=["5", "6"], doc_class="invoice"),
                ],
            ],
            ["1", "2", "3", "4", "5", "6"],
        )
        # Same class throughout, so only the reported start at page 5 splits it.
        assert [s.page_ids for s in merged] == [["1", "2", "3", "4"], ["5", "6"]]

    def test_merged_segments_are_in_document_page_order(self):
        service = _service()
        merged = service._merge_window_segments(
            [
                [Segment(page_ids=["1", "2"], doc_class="invoice")],
                [Segment(page_ids=["2", "3"], doc_class="receipt")],
            ],
            ["1", "2", "3"],
        )
        assert merged[0].start_page == "1"
        assert merged[-1].end_page == "3"

    def test_a_page_no_window_covered_is_reported_and_omitted(self, caplog):
        import logging

        service = _service()
        with caplog.at_level(logging.WARNING):
            merged = service._merge_window_segments(
                [
                    [Segment(page_ids=["1"], doc_class="invoice")],
                    [Segment(page_ids=["3"], doc_class="invoice")],
                ],
                ["1", "2", "3"],
            )
        assert "covered by no window" in caplog.text
        covered = [pid for s in merged for pid in s.page_ids]
        assert covered == ["1", "3"]

    def test_a_segment_confidence_survives_the_merge(self):
        service = _service()
        merged = service._merge_window_segments(
            [
                [Segment(page_ids=["1", "2"], doc_class="invoice", confidence=0.8)],
                [Segment(page_ids=["3"], doc_class="receipt", confidence=0.6)],
            ],
            ["1", "2", "3"],
        )
        assert [s.confidence for s in merged] == [0.8, 0.6]


# --------------------------------------------------------------------------
# Request content
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestPacketRequestContent:
    def test_the_packet_call_sends_page_images(self):
        """The capability the old holistic path did not have at all: it sent one
        text block and nothing else."""
        service = _service()
        document = _document(3)
        with (
            patch("idp_common.s3.get_text_content", return_value="page text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model") as invoke,
        ):
            invoke.return_value = _bedrock_reply(
                [{"ordinal_start_page": 1, "ordinal_end_page": 3, "type": "invoice"}]
            )
            service.packet_classify_document(document)
            content = invoke.call_args.kwargs["content"]

        assert sum(1 for block in content if "image" in block) == 3

    def test_each_image_is_labelled_with_its_ordinal(self):
        """The model is asked for page RANGES, so it has to be able to say which
        image it is looking at. Unlabelled images in a 20-page request leave it
        counting attachments, and a miscount shifts every boundary."""
        service = _service()
        document = _document(2)
        with (
            patch("idp_common.s3.get_text_content", return_value="page text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model") as invoke,
        ):
            invoke.return_value = _bedrock_reply(
                [{"ordinal_start_page": 1, "ordinal_end_page": 2, "type": "invoice"}]
            )
            service.packet_classify_document(document)
            text = "".join(
                block["text"]
                for block in invoke.call_args.kwargs["content"]
                if "text" in block
            )
        assert "<page-number>1</page-number> (document page 1)" in text
        assert "<page-number>2</page-number> (document page 2)" in text

    def test_images_off_sends_none_and_loads_none(self):
        service = _service(modality={"images": False})
        document = _document(3)
        with (
            patch("idp_common.s3.get_text_content", return_value="page text"),
            patch("idp_common.image.prepare_image") as prepare,
            patch.object(service, "_invoke_bedrock_model") as invoke,
        ):
            invoke.return_value = _bedrock_reply(
                [{"ordinal_start_page": 1, "ordinal_end_page": 3, "type": "invoice"}]
            )
            service.packet_classify_document(document)
            content = invoke.call_args.kwargs["content"]
        prepare.assert_not_called()
        assert not any("image" in block for block in content)

    def test_the_packet_call_substitutes_few_shot_examples(self):
        """The same defect the context path had: the placeholder reached the
        model as a literal string and every configured example was dropped."""
        service = _service(classes=_classes("invoice", "receipt", examples=True))
        document = _document(1)
        with (
            patch("idp_common.s3.get_text_content", return_value="page text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model") as invoke,
        ):
            invoke.return_value = _bedrock_reply(
                [{"ordinal_start_page": 1, "ordinal_end_page": 1, "type": "invoice"}]
            )
            service.packet_classify_document(document)
            text = "".join(
                block["text"]
                for block in invoke.call_args.kwargs["content"]
                if "text" in block
            )
        assert "{FEW_SHOT_EXAMPLES}" not in text
        assert "A invoice looks like this." in text

    def test_the_per_page_text_is_labelled_by_ordinal_and_page_id(self):
        service = _service()
        rendered = service._format_paged_text({"6": "six", "7": "seven"}, ["6", "7"])
        assert "<page-number>1</page-number> (document page 6)" in rendered
        assert "<page-number>2</page-number> (document page 7)" in rendered
        assert "six" in rendered and "seven" in rendered

    def test_a_page_with_no_text_renders_the_placeholder(self):
        service = _service()
        rendered = service._format_paged_text({}, ["1"])
        assert "[No text content for page 1]" in rendered


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestPacketClassifyDocument:
    def test_one_window_produces_sections_from_the_models_segments(self):
        service = _service()
        document = _document(4)
        with (
            patch("idp_common.s3.get_text_content", return_value="page text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model") as invoke,
        ):
            invoke.return_value = _bedrock_reply(
                [
                    {
                        "ordinal_start_page": 1,
                        "ordinal_end_page": 2,
                        "type": "invoice",
                    },
                    {
                        "ordinal_start_page": 3,
                        "ordinal_end_page": 4,
                        "type": "receipt",
                    },
                ]
            )
            result = service.packet_classify_document(document)

        assert invoke.call_count == 1
        assert [s.page_ids for s in result.sections] == [["1", "2"], ["3", "4"]]
        assert [s.classification for s in result.sections] == ["invoice", "receipt"]

    def test_a_long_packet_makes_one_call_per_window_and_merges(self):
        service = _service(packet={"window_pages": "4", "overlap_pages": "1"})
        document = _document(8)

        # Windows: [1-4], [4-7], [7-8].
        replies = [
            _bedrock_reply(
                [{"ordinal_start_page": 1, "ordinal_end_page": 4, "type": "invoice"}]
            ),
            _bedrock_reply(
                [
                    {
                        "ordinal_start_page": 1,
                        "ordinal_end_page": 1,
                        "type": "invoice",
                    },
                    {
                        "ordinal_start_page": 2,
                        "ordinal_end_page": 4,
                        "type": "receipt",
                    },
                ]
            ),
            _bedrock_reply(
                [{"ordinal_start_page": 1, "ordinal_end_page": 2, "type": "receipt"}]
            ),
        ]
        with (
            patch("idp_common.s3.get_text_content", return_value="page text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(
                service, "_invoke_bedrock_model", side_effect=replies
            ) as invoke,
        ):
            result = service.packet_classify_document(document)

        assert invoke.call_count == 3
        assert [s.page_ids for s in result.sections] == [
            ["1", "2", "3", "4"],
            ["5", "6", "7", "8"],
        ]
        assert [s.classification for s in result.sections] == ["invoice", "receipt"]

    def test_metering_is_accumulated_across_windows(self):
        service = _service(packet={"window_pages": "2", "overlap_pages": "0"})
        document = _document(4)
        reply = _bedrock_reply(
            [{"ordinal_start_page": 1, "ordinal_end_page": 2, "type": "invoice"}]
        )
        with (
            patch("idp_common.s3.get_text_content", return_value="page text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", return_value=reply),
        ):
            result = service.packet_classify_document(document)

        assert result.metering["Classification/bedrock/nova"]["inputTokens"] == 200

    def test_one_window_failing_does_not_discard_the_others(self):
        """On a long packet that would turn a transient throttle into a total
        loss of the document's boundaries."""
        service = _service(packet={"window_pages": "2", "overlap_pages": "0"})
        document = _document(4)
        replies = [
            _bedrock_reply(
                [{"ordinal_start_page": 1, "ordinal_end_page": 2, "type": "invoice"}]
            ),
            RuntimeError("throttled"),
        ]
        with (
            patch("idp_common.s3.get_text_content", return_value="page text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=replies),
        ):
            result = service.packet_classify_document(document)

        assert [s.page_ids for s in result.sections] == [["1", "2"]]
        assert any("throttled" in e for e in result.errors)

    def test_every_window_failing_fails_the_document_with_the_reasons(self):
        service = _service(packet={"window_pages": "2", "overlap_pages": "0"})
        document = _document(4)
        with (
            patch("idp_common.s3.get_text_content", return_value="page text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(
                service,
                "_invoke_bedrock_model",
                side_effect=RuntimeError("throttled"),
            ),
        ):
            result = service.packet_classify_document(document)

        assert any("produced no segments" in e for e in result.errors)

    def test_the_deprecated_alias_still_works(self):
        """``holistic_classify_document`` is a public method name that notebooks
        and external callers use."""
        service = _service()
        document = _document(1)
        with (
            patch("idp_common.s3.get_text_content", return_value="page text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model") as invoke,
        ):
            invoke.return_value = _bedrock_reply(
                [{"ordinal_start_page": 1, "ordinal_end_page": 1, "type": "invoice"}]
            )
            result = service.holistic_classify_document(document)
        assert result.sections[0].classification == "invoice"

    def test_classify_document_dispatches_on_granularity(self):
        service = _service(granularity="packet")
        document = _document(2)
        with (
            patch("idp_common.s3.get_text_content", return_value="page text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model") as invoke,
            patch.object(service, "classify_page") as page_call,
        ):
            invoke.return_value = _bedrock_reply(
                [{"ordinal_start_page": 1, "ordinal_end_page": 2, "type": "invoice"}]
            )
            service.classify_document(document)
        page_call.assert_not_called()
        assert invoke.call_count == 1

    def test_a_single_class_configuration_still_skips_the_backend(self):
        service = _service(classes=_classes("invoice"), sectionSplitting="disabled")
        with patch.object(service, "_invoke_bedrock_model") as invoke:
            result = service.packet_classify_document(_document(3))
        invoke.assert_not_called()
        assert result.sections[0].classification == "invoice"

    def test_a_document_with_no_pages_fails_cleanly(self):
        service = _service()
        result = service.packet_classify_document(
            Document(id="d", input_key="d.pdf", pages={})
        )
        assert any("no pages" in e for e in result.errors)


# --------------------------------------------------------------------------
# The OCR-none trap
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestPacketTextUnavailable:
    """A packet call with no OCR text anywhere used to send every page as the
    literal string ``[No text content for page N]`` and return a confident class
    derived from nothing — a document that completes green with fabricated
    boundaries, which is worse than a failure because nothing downstream can
    tell it apart from a real one."""

    def test_no_text_anywhere_with_a_text_only_prompt_fails(self):
        service = _service(packet={"task_prompt": "text only {DOCUMENT_TEXT}"})
        document = _document(3, with_images=False)
        with patch.object(service, "_invoke_bedrock_model") as invoke:
            result = service.packet_classify_document(document)
        invoke.assert_not_called()
        assert any("no text to classify" in e for e in result.errors)
        assert any("ocr backend of 'none'" in e.lower() for e in result.errors)

    def test_the_message_names_both_remedies(self):
        service = _service(packet={"task_prompt": "text only {DOCUMENT_TEXT}"})
        result = service.packet_classify_document(_document(1, with_images=False))
        message = " ".join(result.errors)
        assert "OCR backend" in message
        assert "images" in message

    def test_no_text_but_images_on_proceeds(self):
        """A packet call with images and no OCR is a legitimate image-only
        configuration, not the trap."""
        service = _service(modality={"images": True})
        document = _document(2)
        with (
            patch("idp_common.s3.get_text_content", side_effect=Exception("no ocr")),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model") as invoke,
        ):
            invoke.return_value = _bedrock_reply(
                [{"ordinal_start_page": 1, "ordinal_end_page": 2, "type": "invoice"}]
            )
            result = service.packet_classify_document(document)
        assert invoke.call_count == 1
        assert result.sections[0].classification == "invoice"

    def test_one_page_with_text_is_enough_to_proceed(self):
        """A packet with a few image-only pages is ordinary; the guard is about
        a packet with no text at all."""
        service = _service(modality={"images": False})
        document = _document(3)
        texts = ["", "real text", ""]
        with (
            patch("idp_common.s3.get_text_content", side_effect=texts),
            patch.object(service, "_invoke_bedrock_model") as invoke,
        ):
            invoke.return_value = _bedrock_reply(
                [{"ordinal_start_page": 1, "ordinal_end_page": 3, "type": "invoice"}]
            )
            result = service.packet_classify_document(document)
        assert invoke.call_count == 1
        assert result.sections[0].page_ids == ["1", "2", "3"]


# --------------------------------------------------------------------------
# Per-segment confidence
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestPacketConfidence:
    """⚠️ A live defect, found by running this against Nova 2 Lite on a 6-page
    packet and not by any test here.

    In the default `topk` mode the packet prompt asks each segment for ranked
    candidates. The model answered with them — `candidates: [{class, probability}
    ...]` on every segment — and every segment still came back **unscored**,
    because the parse read only a `confidence` key and nothing looked at the key
    the request had asked for. Output tokens bought and discarded, which is the
    same shape as the page-path defect in GitHub #673.

    The fix routes the packet parse through `resolve_class_and_confidence`, the
    same function the page path uses, so the two cannot disagree about what
    counts as a reported confidence.
    """

    def _segment(self, **extra):
        base = {"ordinal_start_page": 1, "ordinal_end_page": 2, "type": "invoice"}
        base.update(extra)
        return base

    def test_a_candidates_list_yields_a_confidence(self):
        service = _service()
        segments = service._parse_model_segments(
            [
                self._segment(
                    candidates=[
                        {"class": "invoice", "probability": 0.95},
                        {"class": "receipt", "probability": 0.03},
                    ]
                )
            ],
            _document(2),
        )
        assert segments[0].confidence == pytest.approx(0.95)

    def test_the_candidates_are_carried_on_the_segment(self):
        """They are what makes the score interpretable — '95% invoice, 3%
        receipt' says what else the pages could have been."""
        service = _service()
        segments = service._parse_model_segments(
            [
                self._segment(
                    candidates=[
                        {"class": "invoice", "probability": 0.8},
                        {"class": "receipt", "probability": 0.2},
                    ]
                )
            ],
            _document(2),
        )
        assert [c["class"] for c in segments[0].candidates] == ["invoice", "receipt"]

    def test_an_explicit_confidence_still_wins(self):
        """Precedence matches the page path: a prompt that asks for a number
        means it."""
        service = _service()
        segments = service._parse_model_segments(
            [
                self._segment(
                    confidence=0.42,
                    candidates=[{"class": "invoice", "probability": 0.95}],
                )
            ],
            _document(2),
        )
        assert segments[0].confidence == pytest.approx(0.42)

    def test_neither_key_leaves_the_segment_unscored(self):
        service = _service()
        segments = service._parse_model_segments([self._segment()], _document(2))
        assert segments[0].confidence is None
        assert segments[0].candidates == []

    def test_a_forced_class_ignores_both(self):
        service = _service()
        segments = service._parse_model_segments(
            [
                self._segment(
                    confidence=0.9,
                    candidates=[{"class": "invoice", "probability": 0.9}],
                )
            ],
            _document(2),
            forced_class="invoice",
        )
        assert segments[0].confidence is None
        assert segments[0].candidates == []

    def test_the_confidence_reaches_the_section_and_the_candidates_the_pages(self):
        service = _service()
        document = _document(2)
        with (
            patch("idp_common.s3.get_text_content", return_value="page text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model") as invoke,
        ):
            invoke.return_value = _bedrock_reply(
                [
                    {
                        "ordinal_start_page": 1,
                        "ordinal_end_page": 2,
                        "type": "invoice",
                        "candidates": [
                            {"class": "invoice", "probability": 0.88},
                            {"class": "receipt", "probability": 0.1},
                        ],
                    }
                ]
            )
            result = service.packet_classify_document(document)

        assert result.sections[0].confidence == pytest.approx(0.88)
        # Persisted on a DECLARED Page field, so it survives the Step Functions
        # hop and reaches DynamoDB (GitHub #565 is that distinction).
        for page_id in ("1", "2"):
            persisted = result.pages[page_id].classification_candidates
            # Asserted non-None separately: the field is Optional, and `or []`
            # would quietly turn "nothing was persisted" into a passing empty
            # comparison, which is the failure this test exists to catch.
            assert persisted is not None
            assert [c["class"] for c in persisted] == ["invoice", "receipt"]

    def test_an_out_of_vocabulary_candidate_is_dropped_from_the_list(self):
        """Shared with the page path via resolve_class_and_confidence: a model
        that invents a class to fill the list must not have it persisted."""
        service = _service()
        segments = service._parse_model_segments(
            [
                self._segment(
                    candidates=[
                        {"class": "invoice", "probability": 0.7},
                        {"class": "mystery", "probability": 0.3},
                    ]
                )
            ],
            _document(2),
        )
        assert [c["class"] for c in segments[0].candidates] == ["invoice"]
