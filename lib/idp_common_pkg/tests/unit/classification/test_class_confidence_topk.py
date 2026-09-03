# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CLASS confidence: prompt assembly + candidate resolution (#673).

`classification.confidence.mode` composes an instruction block into the
classification prompt and resolves what comes back. It ships as `topk`; four
properties matter:

1. The shipped default really is `topk` AND its prompt block is populated — a
   default that asks for nothing would score nothing while looking enabled.
2. `off` changes the prompt by exactly nothing, so opting out really is free.
3. The block lands INSIDE the prompt-cache prefix — classification runs per page,
   so a block after the cache point is re-read on every page of every document.
4. A model that reports a ranked candidate list is scored on the probability of
   the class actually STORED, and nothing is invented when it does not give one.
"""

import json
from textwrap import dedent
from unittest.mock import patch

import pytest

from idp_common.classification.class_confidence import (
    TOP_K_PLACEHOLDER,
    append_class_confidence_block,
    confidence_from_candidates,
    parse_candidates,
    resolve_class_and_confidence,
    resolve_top_k,
)
from idp_common.classification.service import ClassificationService

CLASSES = {"invoice", "receipt", "letter"}


@pytest.mark.unit
class TestResolveTopK:
    def test_never_asks_for_more_candidates_than_there_are_classes(self):
        """Asking for 5 from a 3-class vocabulary invites invented classes."""
        assert resolve_top_k(5, 3) == 3

    def test_uses_the_configured_value_when_the_vocabulary_is_larger(self):
        assert resolve_top_k(3, 20) == 3

    def test_never_drops_below_two(self):
        # One candidate is a verbalized confidence with extra syntax; the
        # calibration benefit comes from having to rank alternatives.
        assert resolve_top_k(3, 1) == 2

    def test_unknown_vocabulary_size_falls_back_to_the_configured_value(self):
        assert resolve_top_k(4, 0) == 4


@pytest.mark.unit
class TestAppendBlock:
    PROMPT = dedent("""\
        <output-format>
        {"class": "..."}
        </output-format>

        <<CACHEPOINT>>

        <document-ocr-data>
        {DOCUMENT_TEXT}
        </document-ocr-data>""")

    def test_block_lands_before_the_cache_point(self):
        """After the cache point the block would be re-read for every page."""
        out = append_class_confidence_block(
            self.PROMPT, "<class-confidence>ask</class-confidence>"
        )
        assert out.index("<class-confidence>") < out.index("<<CACHEPOINT>>")

    def test_appends_at_the_end_when_there_is_no_marker(self):
        out = append_class_confidence_block(
            "Classify this.", "<class-confidence>ask</class-confidence>"
        )
        assert out.strip().endswith("</class-confidence>")

    def test_top_k_is_rendered_into_the_block(self):
        out = append_class_confidence_block(
            self.PROMPT,
            "<class-confidence>give {TOP_K_CANDIDATES}</class-confidence>",
            4,
        )
        assert "give 4" in out
        assert "{TOP_K_CANDIDATES}" not in out

    def test_idempotent_for_a_prompt_that_already_asks(self):
        """A custom prompt that spells the instruction out is not double-instructed."""
        custom = self.PROMPT + "\n<class-confidence>my own wording</class-confidence>"
        assert (
            append_class_confidence_block(
                custom, "<class-confidence>x</class-confidence>"
            )
            == custom
        )

    def test_empty_block_is_a_no_op(self):
        assert append_class_confidence_block(self.PROMPT, "") == self.PROMPT


@pytest.mark.unit
class TestParseCandidates:
    def test_orders_most_likely_first(self):
        parsed = parse_candidates(
            [
                {"class": "receipt", "probability": 0.2},
                {"class": "invoice", "probability": 0.7},
            ],
            CLASSES,
        )
        assert [c["class"] for c in parsed] == ["invoice", "receipt"]

    def test_drops_classes_outside_the_vocabulary(self):
        """A hallucinated class cannot be stored, so it cannot be reported."""
        parsed = parse_candidates(
            [
                {"class": "invoice", "probability": 0.6},
                {"class": "form_1099", "probability": 0.4},
            ],
            CLASSES,
        )
        assert [c["class"] for c in parsed] == ["invoice"]

    def test_tolerates_percentages_and_strings(self):
        parsed = parse_candidates([{"class": "invoice", "probability": "80%"}], CLASSES)
        assert parsed[0]["probability"] == pytest.approx(0.8)

    def test_rescales_only_when_the_sum_exceeds_one(self):
        """A distribution cannot exceed 1, so 1.4 makes every value meaningless."""
        parsed = parse_candidates(
            [
                {"class": "invoice", "probability": 0.9},
                {"class": "receipt", "probability": 0.5},
            ],
            CLASSES,
        )
        assert sum(c["probability"] for c in parsed) == pytest.approx(1.0)
        assert parsed[0]["probability"] == pytest.approx(0.9 / 1.4)

    def test_leaves_a_sum_below_one_alone(self):
        """The missing mass is 'possibly some other class' — real information.

        Inflating the top candidate to absorb it would manufacture confidence.
        """
        parsed = parse_candidates(
            [
                {"class": "invoice", "probability": 0.6},
                {"class": "receipt", "probability": 0.2},
            ],
            CLASSES,
        )
        assert parsed[0]["probability"] == 0.6
        assert sum(c["probability"] for c in parsed) == pytest.approx(0.8)

    def test_deduplicates_keeping_the_highest(self):
        parsed = parse_candidates(
            [
                {"class": "invoice", "probability": 0.3},
                {"class": "invoice", "probability": 0.5},
            ],
            CLASSES,
        )
        assert parsed == [{"class": "invoice", "probability": 0.5}]

    @pytest.mark.parametrize(
        "raw",
        [None, "not a list", [], [{"probability": 0.9}], [{"class": "invoice"}], [42]],
    )
    def test_unusable_input_degrades_to_no_candidates(self, raw):
        assert parse_candidates(raw, CLASSES) == []


@pytest.mark.unit
class TestConfidenceFromCandidates:
    CANDIDATES = [
        {"class": "invoice", "probability": 0.7},
        {"class": "receipt", "probability": 0.2},
    ]

    def test_uses_the_probability_of_the_class_being_stored(self):
        assert confidence_from_candidates(self.CANDIDATES, "invoice") == 0.7

    def test_not_the_top_candidate_when_the_model_disagrees_with_itself(self):
        """The stored class is `receipt`, so 0.7 would describe a different class."""
        assert confidence_from_candidates(self.CANDIDATES, "receipt") == 0.2

    def test_unscored_when_the_stored_class_is_absent_from_the_list(self):
        # Inferring a probability from the leftover mass would be an invention.
        assert confidence_from_candidates(self.CANDIDATES, "letter") is None

    def test_empty_candidates(self):
        assert confidence_from_candidates([], "invoice") is None


@pytest.mark.unit
class TestResolveClassAndConfidence:
    def test_explicit_confidence_wins_over_the_candidate_list(self):
        confidence, candidates = resolve_class_and_confidence(
            reported_class="invoice",
            reported_confidence=0.55,
            reported_candidates=[{"class": "invoice", "probability": 0.9}],
            valid_classes=CLASSES,
        )
        assert confidence == 0.55
        assert candidates[0]["probability"] == 0.9  # still preserved for audit

    def test_falls_back_to_the_candidate_probability(self):
        confidence, _ = resolve_class_and_confidence(
            reported_class="invoice",
            reported_confidence=None,
            reported_candidates=[{"class": "invoice", "probability": 0.9}],
            valid_classes=CLASSES,
        )
        assert confidence == 0.9

    def test_nothing_reported_is_not_scored(self):
        confidence, candidates = resolve_class_and_confidence(
            reported_class="invoice",
            reported_confidence=None,
            reported_candidates=None,
            valid_classes=CLASSES,
        )
        assert confidence is None
        assert candidates == []


def _config(mode="off", method="multimodalPageLevelClassification", **confidence):
    """Config whose task prompt carries the cache-point/document markers."""
    cfg = {
        "classes": [
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": name,
                "x-aws-idp-document-type": name,
                "type": "object",
                "description": f"A {name}",
                "properties": {},
            }
            for name in ("invoice", "receipt", "letter")
        ],
        "classification": {
            "model": "us.amazon.nova-2-lite-v1:0",
            "system_prompt": "classify",
            "task_prompt": (
                "{CLASS_NAMES_AND_DESCRIPTIONS}\n<<CACHEPOINT>>\n{DOCUMENT_TEXT}"
            ),
            "classificationMethod": method,
            "confidence": {
                "mode": mode,
                "top_k_candidates": "3",
                "task_prompt_topk": (
                    "<class-confidence>top {TOP_K_CANDIDATES}</class-confidence>"
                ),
                "task_prompt_verbalized": (
                    "<class-confidence>one number</class-confidence>"
                ),
                **confidence,
            },
        },
    }
    return cfg


def _service(cfg):
    with patch("boto3.Session"):
        return ClassificationService(region="us-west-2", config=cfg, backend="bedrock")


@pytest.mark.unit
class TestShippedDefault:
    """`topk` is ON by default, and that costs money — pin it deliberately.

    The default was chosen on measured evidence (~0.5 % of total document cost,
    no consistent accuracy change, 43 % of the default model's own errors caught
    from 8 % of pages — see docs/benchmarking/classification-confidence.md), so a
    silent flip in either direction should fail a test rather than surprise a
    deployment's bill or quietly stop scoring.
    """

    def test_model_default_is_topk(self):
        from idp_common.config.models import IDPConfig

        cfg = IDPConfig()
        assert cfg.classification.confidence.mode == "topk"
        assert cfg.classification.confidence.top_k_candidates == 3

    def test_system_defaults_agree_with_the_model_default(self):
        """The stored default and the Pydantic default must not drift apart."""
        import os

        import yaml as _yaml

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "..",
            "idp_common",
            "config",
            "system_defaults",
            "base-classification.yaml",
        )
        stored = _yaml.safe_load(open(os.path.normpath(path)))
        assert stored["classification"]["confidence"]["mode"] == "topk"

    def test_the_shipped_prompt_block_is_present_and_asks_for_candidates(self):
        """A default of `topk` with an empty block would silently ask nothing."""
        from idp_common.config.merge_utils import merge_config_with_defaults

        merged = merge_config_with_defaults({}, validate=False)
        block = merged["classification"]["confidence"]["task_prompt_topk"]
        assert "<class-confidence>" in block
        assert "candidates" in block
        assert TOP_K_PLACEHOLDER in block


@pytest.mark.unit
class TestPromptComposition:
    def test_off_leaves_the_prompt_untouched(self):
        """Opting out must cost exactly nothing — not a cheaper ask, nothing."""
        service = _service(_config("off"))
        composed = service._get_classification_config()["task_prompt"]
        assert "<class-confidence>" not in composed
        assert composed == service.config.classification.task_prompt

    def test_topk_splices_the_block_with_the_resolved_count(self):
        service = _service(_config("topk"))
        composed = service._get_classification_config()["task_prompt"]
        assert "<class-confidence>top 3</class-confidence>" in composed
        assert composed.index("<class-confidence>") < composed.index("<<CACHEPOINT>>")

    def test_verbalized_splices_its_own_block(self):
        service = _service(_config("verbalized"))
        composed = service._get_classification_config()["task_prompt"]
        assert "one number" in composed
        assert "top 3" not in composed

    def test_holistic_is_left_alone_and_warns(self, caplog):
        """The holistic prompt returns segments, so a per-page block is wrong."""
        service = _service(_config("topk", method="textbasedHolisticClassification"))
        with caplog.at_level("WARNING"):
            composed = service._get_classification_config()["task_prompt"]
        assert "<class-confidence>" not in composed
        assert "only composed for" in caplog.text

    def test_empty_block_warns_instead_of_silently_asking_for_nothing(self, caplog):
        service = _service(_config("topk", task_prompt_topk=""))
        with caplog.at_level("WARNING"):
            composed = service._get_classification_config()["task_prompt"]
        assert "<class-confidence>" not in composed
        assert "prompt block is empty" in caplog.text

    def test_candidate_count_is_capped_by_the_vocabulary(self):
        cfg = _config("topk")
        cfg["classes"] = cfg["classes"][:2]  # invoice, receipt
        composed = _service(cfg)._get_classification_config()["task_prompt"]
        assert "top 2</class-confidence>" in composed


@pytest.mark.unit
class TestEndToEndTopKResponse:
    """A topk response through classify_page_bedrock."""

    @staticmethod
    def _response(body):
        return {
            "response": {
                "output": {"message": {"content": [{"text": json.dumps(body)}]}}
            },
            "metering": {"bedrock": {"inputTokens": 100, "outputTokens": 20}},
        }

    def _classify(self, body, mode="topk"):
        service = _service(_config(mode))
        with (
            patch("idp_common.s3.get_text_content", return_value="text"),
            patch("idp_common.image.prepare_image", return_value=b"img"),
            patch(
                "idp_common.image.prepare_bedrock_image_attachment",
                return_value={"image": "b64"},
            ),
            patch.object(
                service, "_invoke_bedrock_model", return_value=self._response(body)
            ),
        ):
            return service.classify_page_bedrock(
                page_id="1",
                text_uri="s3://b/t.txt",
                image_uri="s3://b/i.jpg",
            )

    def test_candidates_become_the_confidence_and_are_kept_for_audit(self):
        result = self._classify(
            {
                "class": "invoice",
                "candidates": [
                    {"class": "invoice", "probability": 0.75},
                    {"class": "receipt", "probability": 0.2},
                ],
            }
        )
        assert result.classification.confidence == 0.75
        assert result.classification.metadata["classification_candidates"] == [
            {"class": "invoice", "probability": 0.75},
            {"class": "receipt", "probability": 0.2},
        ]

    def test_a_confident_answer_and_an_uncertain_one_are_distinguishable(self):
        """The whole point of the issue: 0.95 and 0.45 must not both read as 1.0."""
        confident = self._classify(
            {
                "class": "invoice",
                "candidates": [
                    {"class": "invoice", "probability": 0.95},
                    {"class": "receipt", "probability": 0.05},
                ],
            }
        )
        ambiguous = self._classify(
            {
                "class": "invoice",
                "candidates": [
                    {"class": "invoice", "probability": 0.45},
                    {"class": "receipt", "probability": 0.4},
                ],
            }
        )
        assert confident.classification.confidence == 0.95
        assert ambiguous.classification.confidence == 0.45

    def test_hallucinated_candidate_class_is_dropped(self):
        result = self._classify(
            {
                "class": "invoice",
                "candidates": [
                    {"class": "invoice", "probability": 0.6},
                    {"class": "form_1099", "probability": 0.4},
                ],
            }
        )
        assert [
            c["class"]
            for c in result.classification.metadata["classification_candidates"]
        ] == ["invoice"]

    def test_no_candidates_returned_leaves_the_page_unscored(self):
        """topk mode asks, but a model that ignores the ask invents nothing."""
        result = self._classify({"class": "invoice"})
        assert result.classification.confidence is None
        assert "classification_candidates" not in result.classification.metadata

    def test_candidates_reach_the_page_record(self):
        from idp_common.models import Page

        result = self._classify(
            {
                "class": "invoice",
                "candidates": [{"class": "invoice", "probability": 0.8}],
            }
        )
        page = Page(page_id="1")
        ClassificationService._apply_page_result(page, result)
        assert page.classification_candidates == [
            {"class": "invoice", "probability": 0.8}
        ]
