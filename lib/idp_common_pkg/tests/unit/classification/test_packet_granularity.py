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
from idp_common.config.merge_utils import merge_config_with_defaults
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

        ⚠️ **Two things have to be right for this to test the relocation at
        all**, and getting either wrong leaves a passing test that proves
        nothing. The prompt must be packet-shaped — naming both `segments` and
        `ordinal_start_page`, the keys the parser reads — or `_is_packet_shaped`
        refuses it and nothing is copied. And the config must be MERGED with the
        shipped defaults, because those always supply `packet.task_prompt`: on an
        unmerged config that key defaults to `""` and `_stage_prompt` falls back
        to the flat prompt, so the assertion below passes with the relocation
        removed entirely. A real deployment is always merged.
        """
        hand_written = (
            "my own holistic prompt: return segments with ordinal_start_page "
            "and ordinal_end_page {DOCUMENT_TEXT}"
        )
        service = ClassificationService(
            region="us-west-2",
            config=merge_config_with_defaults(
                {
                    "classes": _classes("invoice", "receipt"),
                    "classification": {
                        "model": "us.amazon.nova-2-lite-v1:0",
                        "classificationMethod": "textbasedHolisticClassification",
                        "system_prompt": "You split packets, by hand.",
                        "task_prompt": hand_written,
                        "confidence": {"mode": "off"},
                    },
                },
                validate=False,
            ),
        )
        assert service.granularity == ClassificationService.GRANULARITY_PACKET
        config = service._get_classification_config()
        assert config["task_prompt"] == hand_written
        assert config["system_prompt"] == "You split packets, by hand."

    def test_an_empty_packet_prompt_uses_the_shipped_one_and_warns(self, caplog):
        """⚠️ This asserted the opposite until a live stack showed why it was
        wrong: an empty packet prompt used to fall back to the PAGE prompt, and
        that fallback cannot work. The page prompt asks for one class plus a
        document_boundary, so the model answers in page shape and the segments
        parser finds nothing — every document failed with "No segments found".

        An empty packet prompt is the ordinary state of a profile saved before
        `classification.packet` existed, so this was reachable by switching an
        existing profile to `granularity: packet`. The read path now backfills
        the field; this is the service-level net underneath it, and the shipped
        default is the only fallback with the right output shape.
        """
        import logging

        service = _service(granularity="packet", packet={"task_prompt": ""})
        with caplog.at_level(logging.WARNING):
            config = service._get_classification_config()
        assert not config["task_prompt"].startswith("page prompt"), (
            "fell back to the page prompt, which the segments parser cannot read"
        )
        assert "segments" in config["task_prompt"]
        assert "SHIPPED default" in caplog.text


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
        packet prompt — and the per-segment block is always the right one.

        Merged with the defaults, and packet-shaped, for the reason spelled out
        in `test_a_legacy_holistic_config_keeps_its_own_hand_written_prompt`:
        unmerged, this passes whether or not the relocation happened."""
        service = ClassificationService(
            region="us-west-2",
            config=merge_config_with_defaults(
                {
                    "classes": _classes("invoice", "receipt"),
                    "classification": {
                        "model": "us.amazon.nova-2-lite-v1:0",
                        "classificationMethod": "textbasedHolisticClassification",
                        "system_prompt": "You split packets, by hand.",
                        "task_prompt": (
                            "my own prompt: return segments with "
                            "ordinal_start_page {DOCUMENT_TEXT}"
                        ),
                        "confidence": {
                            "mode": "topk",
                            "task_prompt_topk": "PAGE BLOCK",
                            "task_prompt_topk_packet": "PACKET BLOCK per segment",
                        },
                    },
                },
                validate=False,
            ),
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


@pytest.mark.unit
class TestAModelRefusingImagesNamesTheSetting:
    """⚠️ A model that does not accept images refuses the request, and its own
    message does not mention the setting that caused it.

    Issue #1328 asked for the combination to be refused when a configuration is
    saved. It is annotated at the call instead: deciding it up front needs a
    per-model vision list this repository does not have —
    `model_config_limits.yaml` carries token limits only — which would go stale
    every time a model is added, and a stale entry refuses a configuration for a
    model that **does** accept images. Bedrock already knows the answer; the only
    thing missing was the pointer to `modality.images`.

    ⚠️ **These drive `_invoke_bedrock_model` with `bedrock.invoke_model` patched,
    not the other way round**, because that is where the annotation lives — and it
    lives there because it is the one choke point every classification request
    goes through: page-level, packet, Stage A and Stage B. An earlier version
    annotated only the packet call site, which would have missed page
    granularity, the default. Stubbing `_invoke_bedrock_model` instead, as these
    tests first did, bypasses the code under test entirely.
    """

    CONFIG = {
        "model_id": "us.amazon.nova-micro-v1:0",
        "system_prompt": "s",
        "temperature": 0.0,
        "top_k": 5,
        "top_p": 0.1,
        "max_tokens": 100,
    }
    REFUSAL = "This model does not support image input"

    def _invoke(self, monkeypatch, exc, content):
        from idp_common import bedrock as bedrock_mod

        service = _service(granularity="packet", modality={"images": True})
        monkeypatch.setattr(
            bedrock_mod,
            "invoke_model",
            lambda *a, **k: (_ for _ in ()).throw(exc),
        )
        with pytest.raises(type(exc)) as excinfo:
            service._invoke_bedrock_model(content=content, config=dict(self.CONFIG))
        return excinfo.value

    def test_the_matcher_accepts_the_real_refusal_wordings(self):
        from idp_common.classification.service import _looks_like_an_image_refusal

        for message in (
            "ValidationException: This model does not support image input",
            "ValidationException: does not support multimodal content",
            "ValidationException: vision is not supported for this model",
            "ValidationException: unsupported modality: image",
        ):
            assert _looks_like_an_image_refusal(Exception(message)), message

    def test_the_matcher_rejects_a_throttle_a_timeout_or_a_sizing_error(self):
        """⚠️ These can mention an image and are not capability refusals. "Turn
        images off" is the wrong remedy for an image that was too large on a model
        that does accept them."""
        from idp_common.classification.service import _looks_like_an_image_refusal

        for message in (
            "ThrottlingException: Too many requests",
            "ThrottlingException: image request throttled, retry later",
            "Read timeout on endpoint URL",
            "ValidationException: image exceeds maximum size of 5 MB",
            "ValidationException: invalid base64 image data",
            "ValidationException: malformed image",
        ):
            assert not _looks_like_an_image_refusal(Exception(message)), message

    def test_image_blocks_are_counted_at_any_nesting(self):
        from idp_common.classification.service import _count_image_blocks

        assert _count_image_blocks([{"text": "x"}]) == 0
        assert _count_image_blocks([{"image": {}}, {"image": {"format": "png"}}]) == 2
        # The builders nest image blocks inside a few-shot example's content.
        assert _count_image_blocks([{"content": [{"image": {}}, {"text": "t"}]}]) == 1

    def test_it_survives_the_real_botocore_exception_shape(self, monkeypatch):
        """⚠️ The shape production raises, and the one a naive implementation
        fails on. The non-retryable Bedrock branch re-raises the original
        `ClientError`, whose `__init__` takes `(error_response, operation_name)`,
        so `raise type(exc)(message)` throws `TypeError` inside the handler and
        the operator ends up with LESS information than before. A test raising
        `ValueError` cannot see that, because `ValueError` takes one argument."""
        from botocore.exceptions import ClientError

        err = ClientError(
            {"Error": {"Code": "ValidationException", "Message": self.REFUSAL}},
            "Converse",
        )
        raised = self._invoke(monkeypatch, err, [{"image": {}}])
        message = str(raised)
        assert self.REFUSAL in message, "the original error must survive"
        assert "classification.modality.images" in message
        assert "nova-micro" in message
        assert isinstance(raised, ClientError), "the class must be preserved"

    def test_the_model_name_comes_from_the_key_production_sets(self, monkeypatch):
        """`model_id`, not `model`: `_get_classification_config` builds
        `model_id` and `_invoke_bedrock_model` reads it, so reading `model` always
        fell through to a placeholder — invisible to a test that passed `model`."""
        service = _service(granularity="packet", modality={"images": True})
        assert "model_id" in service._get_classification_config()
        raised = self._invoke(
            monkeypatch,
            ValueError(f"ValidationException: {self.REFUSAL}"),
            [{"image": {}}],
        )
        assert "nova-micro" in str(raised)
        assert "this model" not in str(raised)

    def test_an_unrelated_failure_is_re_raised_untouched(self, monkeypatch):
        raised = self._invoke(
            monkeypatch, ValueError("ThrottlingException"), [{"image": {}}]
        )
        assert "modality.images" not in str(raised)

    def test_a_sizing_error_gets_no_capability_hint(self, monkeypatch):
        raised = self._invoke(
            monkeypatch,
            ValueError("ValidationException: image exceeds maximum size of 5 MB"),
            [{"image": {}}],
        )
        assert "modality.images" not in str(raised)

    def test_no_hint_when_the_request_carried_no_images(self, monkeypatch):
        """A text-only request cannot have been refused for images it did not
        send, so attributing it to `modality.images` would be wrong."""
        raised = self._invoke(
            monkeypatch,
            ValueError(f"ValidationException: {self.REFUSAL}"),
            [{"text": "page text"}],
        )
        assert "modality.images" not in str(raised)

    def test_the_hint_covers_the_page_path_not_only_packet(self, monkeypatch):
        """⚠️ The reason this lives in `_invoke_bedrock_model`. Page granularity
        is the DEFAULT, so a hint on the packet call site alone would miss the
        common case — fixing the instance and not the class."""
        service = _service(granularity="page", modality={"images": True})
        from idp_common import bedrock as bedrock_mod

        monkeypatch.setattr(
            bedrock_mod,
            "invoke_model",
            lambda *a, **k: (_ for _ in ()).throw(
                ValueError(f"ValidationException: {self.REFUSAL}")
            ),
        )
        with pytest.raises(ValueError) as excinfo:
            service._invoke_bedrock_model(
                content=[{"image": {}}], config=dict(self.CONFIG)
            )
        assert "classification.modality.images" in str(excinfo.value)


def _segments_reply(*segments: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "response": {
            "output": {
                "message": {
                    "content": [{"text": json.dumps({"segments": list(segments)})}]
                }
            }
        },
        "metering": {},
    }


@pytest.mark.unit
class TestPacketGranularityHonoursClassValidation:
    """⚠️ `enforceValidClasses`, `maxValidationRetries` and the fallback class now
    apply on single-pass PACKET granularity, as they already did on page
    granularity and on Stage B.

    They used not to. An out-of-vocabulary segment class was logged "using
    anyway" and kept, so the three settings were silently inert on one of the
    class-assigning paths while the configuration panel showed them as in force.
    """

    def _run(self, monkeypatch, replies, **classification):
        service = _service(classes=_classes("invoice", "receipt"), **classification)
        calls: List[List[Dict[str, Any]]] = []
        queue = list(replies)

        def invoke(*, content, config):
            calls.append(content)
            return queue.pop(0)

        monkeypatch.setattr(service, "_invoke_bedrock_model", invoke)
        segments, _ = service._classify_one_packet_window(
            document=_document(2),
            config=service._get_classification_config(),
            pages_content={"1": "a", "2": "b"},
            window_page_ids=["1", "2"],
            window_label="1/1",
            send_images=False,
            forced_class=None,
        )
        return segments, calls

    BAD = {"ordinal_start_page": 1, "ordinal_end_page": 2, "type": "purchase_order"}
    GOOD = {"ordinal_start_page": 1, "ordinal_end_page": 2, "type": "invoice"}

    def test_an_invalid_class_is_re_prompted_and_corrected(self, monkeypatch):
        segments, calls = self._run(
            monkeypatch, [_segments_reply(self.BAD), _segments_reply(self.GOOD)]
        )
        assert [s.doc_class for s in segments] == ["invoice"]
        assert len(calls) == 2, "one retry"
        retry_text = json.dumps(calls[1])
        assert "purchase_order" in retry_text, "the retry names the rejected class"
        assert "invoice" in retry_text and "receipt" in retry_text

    def test_retries_exhausted_assign_the_fallback_unscored(self, monkeypatch):
        segments, calls = self._run(
            monkeypatch,
            [_segments_reply(self.BAD)] * 3,
            maxValidationRetries=2,
        )
        assert len(calls) == 3
        assert segments[0].doc_class == "unclassified"
        assert segments[0].confidence is None, "a fallback is not a prediction"
        assert segments[0].candidates == []

    def test_a_configured_fallback_class_is_used(self, monkeypatch):
        segments, _ = self._run(
            monkeypatch,
            [_segments_reply(self.BAD)] * 3,
            invalidClassFallback="receipt",
        )
        assert segments[0].doc_class == "receipt"

    def test_enforcement_off_keeps_the_class_and_makes_one_call(self, monkeypatch):
        """Legacy behaviour, preserved exactly when the operator opts out."""
        segments, calls = self._run(
            monkeypatch, [_segments_reply(self.BAD)], enforceValidClasses=False
        )
        assert len(calls) == 1
        assert segments[0].doc_class == "purchase_order"

    def test_only_the_invalid_segment_is_replaced(self, monkeypatch):
        """A valid segment in the same window keeps its class and its score."""
        two = (
            {"ordinal_start_page": 1, "ordinal_end_page": 1, "type": "invoice"},
            {"ordinal_start_page": 2, "ordinal_end_page": 2, "type": "purchase_order"},
        )
        segments, _ = self._run(
            monkeypatch, [_segments_reply(*two)] * 3, maxValidationRetries=2
        )
        assert [s.doc_class for s in segments] == ["invoice", "unclassified"]

    def test_a_regex_forced_class_is_not_validated(self, monkeypatch):
        """The operator's document-name match already decided the class."""
        service = _service(classes=_classes("invoice"))
        calls = []

        def invoke(*, content, config):
            calls.append(1)
            return _segments_reply(self.BAD)

        monkeypatch.setattr(service, "_invoke_bedrock_model", invoke)
        segments, _ = service._classify_one_packet_window(
            document=_document(2),
            config=service._get_classification_config(),
            pages_content={"1": "a", "2": "b"},
            window_page_ids=["1", "2"],
            window_label="1/1",
            send_images=False,
            forced_class="invoice",
        )
        assert len(calls) == 1
        assert segments[0].doc_class == "invoice"
