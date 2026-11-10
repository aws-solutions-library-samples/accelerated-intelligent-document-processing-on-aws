# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Section classification by a decision model.

``split_then_classify.section_classification.method: decision_model``. Each section
becomes one ``choice`` question whose options are the configured classes, each
described by its ``description``. The answer comes back as a distribution over the
classes. Its top probability is the section's confidence and the whole distribution
is its ``classification_candidates``, in the shape the LLM path writes, so section
forming, HITL and extraction do not change.

A failed call fails its section: the segment keeps no class and the error is
recorded on it, exactly as an LLM labeling call that fails. It is never converted
into an LLM call. ``low_confidence`` is the only route to another labeler. It
applies to an answer below ``min_confidence``, never to an error, and is off at
its default of 0.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from idp_common import utils
from idp_common.decision_model import (
    ChoiceAnswer,
    ChoiceQuestion,
    SystemOneRequest,
    client_from_config,
    encode_image,
)

if TYPE_CHECKING:
    from idp_common.models import Document

logger = logging.getLogger(__name__)

#: The metering context, beside ``SectionClassification/bedrock/...``.
METERING_CONTEXT = "SectionClassification"

#: The question's name in the request and the answer.
QUESTION_NAME = "doc_class"

#: (class, confidence, candidates, reason, metering, validation_error): the tuple
#: ``ClassificationService._invoke_with_class_validation`` returns.
LabelResult = Tuple[
    str,
    Optional[float],
    List[Dict[str, Any]],
    Optional[str],
    Dict[str, Any],
    Optional[str],
]


class DecisionModelSectionLabeler:
    """Labels split-then-classify sections with one ``choice`` question each."""

    def __init__(self, service: Any, section_cfg: Any):
        self.service = service
        self.cfg = section_cfg.decision_model
        self.low_confidence = section_cfg.low_confidence
        self.criteria: Dict[str, Optional[str]] = {
            doc_type.type_name: (doc_type.description or None)
            for doc_type in service.document_types
            if doc_type.type_name
        }
        self.label_map: Dict[str, str] = dict(self.cfg.label_map or {})
        self._client = None
        self._client_lock = threading.Lock()

    @property
    def client(self):
        # Built on first use, so a configuration with a single class (which never
        # calls the model) needs no endpoint.
        with self._client_lock:
            if self._client is None:
                self._client = client_from_config(self.cfg, region=self.service.region)
            return self._client

    def build_request(
        self,
        document: "Document",
        pages_content: Dict[str, str],
        page_ids: List[str],
    ) -> SystemOneRequest:
        """The request for one section: its pages as the state, one choice question."""
        page_ids = page_ids[: self.cfg.max_pages]
        mode = self.cfg.input
        state = ""
        if mode in ("text", "both"):
            state = "\n\n".join(
                f"Page {ordinal}:\n{pages_content.get(page_id, '')}"
                for ordinal, page_id in enumerate(page_ids, start=1)
            )
        images: List[str] = []
        if mode in ("images", "both"):
            loaded = self.service._load_packet_page_images(
                document, page_ids, stage=self.service.STAGE_SECTION_CLASSIFICATION
            )
            if not loaded and mode == "images":
                raise ValueError(
                    "decision_model.input is 'images' but no page image could be "
                    f"loaded for pages {', '.join(page_ids)}"
                )
            images = [encode_image(image_bytes) for _, image_bytes in loaded]
        return SystemOneRequest(
            state=state,
            images=images,
            questions={
                QUESTION_NAME: ChoiceQuestion(
                    instructions=self.cfg.instructions, criteria=self.criteria
                )
            },
        )

    def label(
        self,
        document: "Document",
        pages_content: Dict[str, str],
        page_ids: List[str],
        label: str,
        llm_label: Callable[[], LabelResult],
    ) -> LabelResult:
        """Label one section. ``llm_label`` is called only for ``low_confidence: llm``."""
        if len(self.criteria) < 2:
            # A choice needs two options; with one class there is nothing to decide.
            only = next(iter(self.criteria), self.service._fallback_class())
            return only, None, [], "Only one class is configured.", {}, None

        request = self.build_request(document, pages_content, page_ids)
        result = self.client.decide(request, METERING_CONTEXT)
        answer = result.response.answers.get(QUESTION_NAME)
        if not isinstance(answer, ChoiceAnswer):
            raise ValueError(
                f"decision model returned no choice answer for {label}: "
                f"{result.response.answers!r}"
            )

        candidates = self._candidates(answer.probabilities)
        choice = self.label_map.get(answer.choice, answer.choice)
        probability = float(answer.probabilities.get(answer.choice, 0.0))
        reason = (
            f"Decision model {result.response.model}: {choice} "
            f"(probability {probability:.3f})"
        )
        metering = result.metering

        if choice not in self.service.valid_doc_types:
            fallback = self.service._fallback_class()
            invalid = (
                f"Decision model returned '{answer.choice}' for {label}, which "
                f"maps to no configured class; assigned fallback '{fallback}'"
            )
            logger.error(invalid)
            return fallback, None, candidates, reason, metering, invalid

        threshold = float(self.low_confidence.min_confidence or 0.0)
        if threshold > 0 and probability < threshold:
            if self.low_confidence.on_low_confidence == "llm":
                logger.info(
                    "Decision model probability %.3f for %s is below "
                    "min_confidence %.3f; labeling it with the LLM",
                    probability,
                    label,
                    threshold,
                )
                llm = llm_label()
                return (
                    llm[0],
                    llm[1],
                    llm[2],
                    f"{reason}; below min_confidence {threshold:.3f}, so labeled "
                    f"by the LLM: {llm[3] or ''}".rstrip(": "),
                    utils.merge_metering_data(metering, llm[4]),
                    llm[5],
                )
            fallback = self.service._fallback_class()
            low = (
                f"Decision model probability {probability:.3f} for {label} is below "
                f"min_confidence {threshold:.3f}; assigned fallback '{fallback}'"
            )
            logger.warning(low)
            return fallback, None, candidates, reason, metering, low

        return choice, probability, candidates, reason, metering, None

    def _candidates(self, probabilities: Dict[str, float]) -> List[Dict[str, Any]]:
        """The distribution, most likely first, in the LLM path's candidate shape."""
        ranked = sorted(probabilities.items(), key=lambda kv: kv[1], reverse=True)
        return [
            {"class": self.label_map.get(name, name), "probability": round(float(p), 6)}
            for name, p in ranked
        ]
