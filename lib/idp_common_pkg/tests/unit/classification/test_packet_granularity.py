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
from idp_common.models import Document, Page, Status

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

    def test_overlapping_segments_in_one_call_keep_each_page_once(self, caplog):
        """#1654: one call reporting segments that share pages. The rule is the
        windowed one — the segment reported first keeps the page — so the
        answer does not depend on whether the packet was windowed."""
        import logging

        service = _service()
        with caplog.at_level(logging.WARNING):
            merged = service._merge_window_segments(
                [
                    [
                        Segment(page_ids=["1", "2", "3"], doc_class="invoice"),
                        Segment(page_ids=["3", "4"], doc_class="receipt"),
                        Segment(page_ids=["4"], doc_class="receipt"),
                    ]
                ],
                ["1", "2", "3", "4"],
            )
        assert [(s.page_ids, s.doc_class) for s in merged] == [
            (["1", "2", "3"], "invoice"),
            (["4"], "receipt"),
        ]
        assert "overlapping segments" in caplog.text

    def test_an_overlap_does_not_merge_two_documents_of_one_class(self):
        """The later segment lost its first page to an earlier segment of the
        same call, so its first remaining page still opens a document. Without
        that, the fold would join two invoices into one section silently."""
        service = _service()
        merged = service._merge_window_segments(
            [
                [
                    Segment(page_ids=["1", "2", "3"], doc_class="invoice"),
                    Segment(page_ids=["3", "4", "5", "6"], doc_class="invoice"),
                ]
            ],
            ["1", "2", "3", "4", "5", "6"],
        )
        assert [s.page_ids for s in merged] == [["1", "2", "3"], ["4", "5", "6"]]

    def test_an_unlabelled_splitting_overlap_keeps_its_boundary(self):
        """The splitting call returns no class, so every page folds under one
        label and only the boundary separates documents."""
        service = _service()
        merged = service._merge_window_segments(
            [[Segment(page_ids=["1", "2", "3"]), Segment(page_ids=["3", "4"])]],
            ["1", "2", "3", "4"],
        )
        assert [s.page_ids for s in merged] == [["1", "2", "3"], ["4"]]

    def test_a_segment_resuming_after_a_claimed_run_opens_a_document(self):
        service = _service()
        merged = service._merge_window_segments(
            [
                [
                    Segment(page_ids=["2", "3"], doc_class="invoice"),
                    Segment(page_ids=["1", "2", "3", "4", "5"], doc_class="invoice"),
                ]
            ],
            ["1", "2", "3", "4", "5"],
        )
        assert [s.page_ids for s in merged] == [["1"], ["2", "3"], ["4", "5"]]

    def test_a_leading_overlap_lost_to_an_earlier_window_still_continues(self):
        """Across windows the leading overlap is the earlier window's, and the
        pages after it continue that window's document — unchanged."""
        service = _service()
        merged = service._merge_window_segments(
            [
                [Segment(page_ids=["1", "2", "3"], doc_class="invoice")],
                [Segment(page_ids=["3", "4", "5"], doc_class="invoice")],
            ],
            ["1", "2", "3", "4", "5"],
        )
        assert [s.page_ids for s in merged] == [["1", "2", "3", "4", "5"]]

    def test_candidates_survive_where_a_segment_is_one_reported_segment(self):
        service = _service()
        ranked = [{"class": "invoice", "probability": 0.9}]
        other = [{"class": "receipt", "probability": 0.8}]
        merged = service._merge_window_segments(
            [
                [
                    Segment(
                        page_ids=["1", "2"], doc_class="invoice", candidates=ranked
                    ),
                    Segment(page_ids=["2", "3"], doc_class="receipt", candidates=other),
                ]
            ],
            ["1", "2", "3"],
        )
        assert [(s.page_ids, s.candidates) for s in merged] == [
            (["1", "2"], ranked),
            (["3"], other),
        ]

    def test_one_call_and_two_windows_resolve_an_overlap_the_same_way(self):
        """The same page claims, delivered as one call or split across two
        windows, produce the same segments."""
        service = _service()
        first = Segment(page_ids=["1", "2", "3"], doc_class="invoice")
        second = Segment(page_ids=["2", "3", "4"], doc_class="receipt")
        one_call = service._merge_window_segments(
            [[first, second]], ["1", "2", "3", "4"]
        )
        two_windows = service._merge_window_segments(
            [[first], [second]], ["1", "2", "3", "4"]
        )
        assert [(s.page_ids, s.doc_class) for s in one_call] == [
            (s.page_ids, s.doc_class) for s in two_windows
        ]

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

    def test_overlapping_segments_never_put_a_page_in_two_sections(self):
        """#1654, through the real parse and section builder: a wide segment
        followed by narrower ones covering some of the same pages."""
        service = _service()
        document = _document(4)
        with (
            patch("idp_common.s3.get_text_content", return_value="page text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model") as invoke,
        ):
            invoke.return_value = _bedrock_reply(
                [
                    {"ordinal_start_page": 1, "ordinal_end_page": 3, "type": "invoice"},
                    {"ordinal_start_page": 2, "ordinal_end_page": 2, "type": "receipt"},
                    {"ordinal_start_page": 3, "ordinal_end_page": 4, "type": "receipt"},
                ]
            )
            result = service.packet_classify_document(document)

        placed = [pid for s in result.sections for pid in s.page_ids]
        assert sorted(placed) == ["1", "2", "3", "4"]
        assert [(s.page_ids, s.classification) for s in result.sections] == [
            (["1", "2", "3"], "invoice"),
            (["4"], "receipt"),
        ]

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
@pytest.mark.unit
class TestAWindowTooLargeForTheModelIsSplit:
    """#1676: ``window_pages`` is a page count, so a window under it can still
    exceed the model's input once its page images are attached. Such a window
    is split and retried rather than failing the document.

    The fake sits under ``_invoke_bedrock_model`` (at ``bedrock.invoke_model``)
    so the production wrapper, including its error annotation, is on the path,
    and it refuses with the real botocore exception shape and Bedrock wording.
    """

    LIMIT = 4  # page images one request may carry before the fake refuses
    #: What the fake says when it refuses. Overridden by the image-count subclass.
    REFUSAL_MESSAGE = "Input is too long for requested model."
    REFUSAL_CODE = "ValidationException"
    # Ground truth the fake answers from: pages 1-6 are one invoice, 7 onwards one
    # receipt. Each call reports a segment per document it can see.
    TRUTH = {str(p): ("invoice" if p <= 6 else "receipt") for p in range(1, 21)}

    def _fake_bedrock(
        self, calls: List[List[str]], limit: Optional[int] = None, refuse=None
    ):
        import re

        from botocore.exceptions import ClientError

        limit = self.LIMIT if limit is None else limit

        def invoke_model(**kwargs):
            content = kwargs["content"]
            text = "".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
            # Each page is named in its text block and again in its image label.
            page_ids = list(dict.fromkeys(re.findall(r"\(document page (\d+)\)", text)))
            calls.append(page_ids)
            images = sum(1 for block in content if "image" in block)
            if refuse(page_ids) if refuse else images > limit:
                raise ClientError(
                    {
                        "Error": {
                            "Code": self.REFUSAL_CODE,
                            "Message": self.REFUSAL_MESSAGE,
                        }
                    },
                    "Converse",
                )
            segments: List[Dict[str, Any]] = []
            for ordinal, page_id in enumerate(page_ids, start=1):
                doc_class = self.TRUTH[page_id]
                if segments and segments[-1]["type"] == doc_class:
                    segments[-1]["ordinal_end_page"] = ordinal
                else:
                    segments.append(
                        {
                            "ordinal_start_page": ordinal,
                            "ordinal_end_page": ordinal,
                            "type": doc_class,
                        }
                    )
            return _bedrock_reply(segments)

        return invoke_model

    def _run(
        self, *, pages: int = 10, limit: Optional[int] = None, refuse=None, **packet
    ):
        service = _service(
            packet={"window_pages": "20", "overlap_pages": "1", **packet},
            modality={"images": True},
        )
        document = _document(pages)
        calls: List[List[str]] = []
        with (
            patch("idp_common.s3.get_text_content", return_value="page text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch(
                "idp_common.bedrock.invoke_model",
                side_effect=self._fake_bedrock(calls, limit, refuse),
            ),
        ):
            result = service.packet_classify_document(document)
        return result, calls

    def _run_with(self, message: str, code: str = "ValidationException"):
        self.REFUSAL_MESSAGE, self.REFUSAL_CODE = message, code
        return self._run()

    def test_the_document_is_classified_instead_of_failed(self):
        result, calls = self._run()

        assert result.status != Status.FAILED
        assert result.errors == []
        assert [(s.page_ids, s.classification) for s in result.sections] == [
            (["1", "2", "3", "4", "5", "6"], "invoice"),
            (["7", "8", "9", "10"], "receipt"),
        ]
        # The first call was the whole 10-page window, refused.
        assert calls[0] == [str(p) for p in range(1, 11)]
        assert len(calls) > 1

    def test_every_page_lands_in_exactly_one_section(self):
        result, _ = self._run()
        placed = [pid for s in result.sections for pid in s.page_ids]
        assert sorted(placed, key=int) == [str(p) for p in range(1, 11)]

    def test_the_halves_repeat_the_overlap_pages_as_context(self):
        _, calls = self._run(overlap_pages="1")
        # 10 -> [1-5] refused -> [1-2] ok, [2-5] ok ; [5-10] refused -> ...
        assert calls[1] == ["1", "2", "3", "4", "5"]
        assert calls[2] == ["1", "2"]
        assert calls[3] == ["2", "3", "4", "5"]
        assert calls[4] == ["5", "6", "7", "8", "9", "10"]

    def test_a_single_page_that_does_not_fit_is_reported_not_retried_forever(self):
        result, calls = self._run(pages=3, limit=0)

        assert result.status == Status.FAILED
        # Every call is refused. Splitting stops at one page, and each one-page
        # window is reported once rather than retried.
        assert calls == [["1", "2", "3"], ["1"], ["2", "3"], ["2"], ["3"]]
        refused = [e for e in result.errors if e.startswith("Packet window")]
        assert [e.split("(pages ")[1].split(")")[0] for e in refused] == [
            "1-1",
            "2-2",
            "3-3",
        ]

    def test_a_split_that_does_not_shrink_is_not_retried(self):
        """The loop checks termination itself instead of trusting the helper.
        The fake gives up with a non-overflow error after a few calls, so a
        missing check fails this test instead of looping forever."""
        from botocore.exceptions import ClientError

        service = _service(packet={"window_pages": "20"}, modality={"images": True})
        calls = []

        def invoke_model(**kwargs):
            calls.append(1)
            if len(calls) > 5:
                raise RuntimeError("runaway retry loop")
            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationException",
                        "Message": "Input is too long for requested model.",
                    }
                },
                "Converse",
            )

        with (
            patch("idp_common.s3.get_text_content", return_value="page text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch("idp_common.bedrock.invoke_model", side_effect=invoke_model),
            patch.object(
                service,
                "_split_overflowing_window",
                # Distinct from the window, so the de-duplication cannot hide
                # a missing guard; the first half shrinks and the second does
                # not, so a guard that accepts either half shrinking fails too.
                side_effect=lambda ids: (ids[:1], ids[1:] + ids[:1]),
            ),
        ):
            result = service.packet_classify_document(_document(4))

        assert len(calls) == 1
        assert result.status == Status.FAILED

    def test_a_re_prompt_failing_for_another_reason_still_raises(self, monkeypatch):
        """Only an overflowing re-prompt keeps the first answer; a throttle on
        the re-prompt fails the window as it always did."""
        service = _service(packet={"window_pages": "20"})
        bad = {"ordinal_start_page": 1, "ordinal_end_page": 2, "type": "po"}
        replies = [_segments_reply(bad), RuntimeError("ThrottlingException")]

        def invoke(*, content, config):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        monkeypatch.setattr(service, "_invoke_bedrock_model", invoke)
        with patch("idp_common.s3.get_text_content", return_value="page text"):
            merged, _, errors = service._packet_segments(_document(2))

        assert merged == []
        assert any("ThrottlingException" in e for e in errors)

    def test_window_pages_zero_is_never_split(self):
        """``0`` asks for one call over the whole packet and documents that it
        fails outright rather than degrading, so it keeps doing exactly that."""
        result, calls = self._run(window_pages="0")

        assert result.status == Status.FAILED
        assert calls == [[str(p) for p in range(1, 11)]]

    def test_a_model_that_refuses_everything_costs_a_bounded_number_of_calls(self):
        """Every request refused, down to single pages: the calls made for
        split windows stop at twice the page count, and the rest are reported."""
        result, calls = self._run(pages=20, refuse=lambda ids: True, overlap_pages="2")

        assert len(calls) == 1 + 2 * 20
        assert result.status == Status.FAILED
        assert any("used all 40 calls allowed" in e for e in result.errors)

    def test_no_window_is_requested_twice(self):
        """The overlap makes neighbouring halves produce the same smaller
        window; it is requested once."""
        _, calls = self._run(pages=20, refuse=lambda ids: "7" in ids, overlap_pages="2")
        assert len({tuple(c) for c in calls}) == len(calls)

    def test_one_page_too_large_on_its_own_fails_only_that_page(self):
        result, calls = self._run(
            pages=20, refuse=lambda ids: "7" in ids, overlap_pages="2"
        )

        window_errors = [e for e in result.errors if e.startswith("Packet window")]
        assert len(window_errors) == 1 and "(pages 7-7)" in window_errors[0]
        placed = sorted((pid for s in result.sections for pid in s.page_ids), key=int)
        assert placed == [str(p) for p in range(1, 21) if p != 7]
        assert len(calls) <= 1 + 2 * 20

    def test_a_re_prompt_that_overflows_keeps_the_first_answer_and_its_metering(
        self, monkeypatch, caplog
    ):
        """The class-validation re-prompt is longer than the first request, so
        it can be the one refused. The first answer's billed tokens stay in the
        metering, its invalid segment gets the fallback, and the window is not
        split over a request the model already answered."""
        from botocore.exceptions import ClientError

        service = _service(packet={"window_pages": "20"})
        bad = {"ordinal_start_page": 1, "ordinal_end_page": 2, "type": "po"}
        replies = [
            {**_segments_reply(bad), "metering": {"m": {"inputTokens": 700}}},
            ClientError(
                {
                    "Error": {
                        "Code": "ValidationException",
                        "Message": "Input is too long for requested model.",
                    }
                },
                "Converse",
            ),
        ]
        invoked = []

        def invoke(*, content, config):
            invoked.append(1)
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        monkeypatch.setattr(service, "_invoke_bedrock_model", invoke)
        with patch("idp_common.s3.get_text_content", return_value="page text"):
            merged, metering, errors = service._packet_segments(_document(2))

        assert len(invoked) == 2
        assert [(s.page_ids, s.doc_class) for s in merged] == [
            (["1", "2"], "unclassified")
        ]
        assert metering == {"m": {"inputTokens": 700}}
        assert errors == []
        # The fallback line counts the answers actually received.
        assert "invalid class(es) after 1 attempt(s)" in caplog.text

    def test_another_error_is_not_split(self):
        service = _service(packet={"window_pages": "20"}, modality={"images": True})
        document = _document(6)
        with (
            patch("idp_common.s3.get_text_content", return_value="page text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch(
                "idp_common.bedrock.invoke_model",
                side_effect=RuntimeError("ThrottlingException: slow down"),
            ) as invoke,
        ):
            result = service.packet_classify_document(document)

        assert invoke.call_count == 1
        assert result.status == Status.FAILED

    @pytest.mark.parametrize("overlap", [0, 1, 2, 5, 19])
    def test_splitting_always_shrinks_and_covers_the_window(self, overlap):
        service = _service(packet={"window_pages": "20", "overlap_pages": "0"})
        service._page_input().packet.overlap_pages = overlap
        for total in range(2, 31):
            window = [str(p) for p in range(1, total + 1)]
            first, second = service._split_overflowing_window(window)
            assert 0 < len(first) < total
            assert 0 < len(second) < total
            assert first[0] == window[0] and second[-1] == window[-1]
            assert set(first) | set(second) == set(window)
            # Each split must shrink by a fraction of the window, not by one
            # page, or the number of calls grows far faster than the pages.
            if total >= 8:
                assert len(second) <= 3 * total // 4 + 1, (total, overlap)
        assert service._split_overflowing_window(["1"]) is None


@pytest.mark.unit
class TestAWindowWithTooManyImagesIsSplit(TestAWindowTooLargeForTheModelIsSplit):
    """#1705: Bedrock caps how many images one request may carry, so with page
    images on a window above that cap is refused however few tokens it holds —
    a rejection worded as an image rejection, not as an input overflow, which
    the split used not to recognise. Halving the window is the fix for that
    refusal exactly as for an overflow, so every inherited test that goes
    through the fake (``_run``) re-runs with the image-count wording, under the
    same bounds. The few inherited tests that build their own overflow fake
    re-run unchanged."""

    REFUSAL_MESSAGE = "too many images in request"


@pytest.mark.unit
class TestOnlyARefusalAboutImageCountIsSplit:
    """The split is for refusals a smaller request fixes. These are judged
    through the shared matchers in ``bedrock_utils``, the ones extraction uses."""

    @pytest.mark.parametrize(
        "message",
        [
            "too many images in request",
            "image exceed max allowed size for many-image requests: 2000 pixels",
        ],
    )
    def test_a_count_dependent_rejection_is_split(self, message):
        _, calls = TestAWindowWithTooManyImagesIsSplit()._run_with(message)
        assert len(calls) > 1

    @pytest.mark.parametrize(
        ("code", "message"),
        [
            # One image too large on its own: no window is small enough.
            ("ValidationException", "image exceeds maximum size of 5 MB"),
            ("ValidationException", "image dimensions exceed 8000 pixels"),
            # A throttle that happens to mention images.
            ("ThrottlingException", "Rate exceeded: too many images in flight"),
        ],
    )
    def test_any_other_image_error_is_not_split(self, code, message):
        _, calls = TestAWindowWithTooManyImagesIsSplit()._run_with(message, code=code)
        assert len(calls) == 1


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
    goes through: page-level, packet, splitting and section classification. An earlier version
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
        assert "classification.combined.llm.modality.images" in message
        assert "nova-micro" in message
        assert isinstance(raised, ClientError), "the class must be preserved"

    @pytest.mark.parametrize(
        ("stage", "setting"),
        [
            ("classification", "classification.combined.llm.modality.images"),
            (
                "splitting",
                "classification.split_then_classify.splitting.llm.modality.images",
            ),
            (
                "section_classification",
                "classification.split_then_classify.section_classification.llm.modality.images",
            ),
        ],
    )
    def test_the_hint_names_the_setting_of_the_call_that_failed(self, stage, setting):
        """Each call has its own modality (config format 0.9), so the hint must
        name the one that sent the images — pointing a split-then-classify user
        at the combined setting would send them to a block their strategy never runs."""
        from idp_common.classification.service import _annotate_image_refusal

        exc = ValueError(self.REFUSAL)
        _annotate_image_refusal(exc, [{"image": {}}], {**self.CONFIG, "stage": stage})
        assert setting in str(exc)

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
        assert "classification.combined.llm.modality.images" in str(excinfo.value)


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
    apply on combined PACKET granularity, as they already did on page
    granularity and on section classification.

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
