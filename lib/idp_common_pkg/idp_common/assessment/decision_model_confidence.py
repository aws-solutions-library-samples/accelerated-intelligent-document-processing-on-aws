# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Extraction confidence by a decision model.

``extraction.confidence.method: decision_model``. Every extracted value becomes one
``noul`` question, asked against each page of the section. Each page is one
request, with that page as the state and every field's question in it, because the
state is encoded once and each question reads that encoding.

* A **value** is asked "the system says <field> is <value>; is that the correct
  value?". Its confidence is its **highest P(yes) over the pages**: a value is
  supported by the document if some page supports it.
* An **empty value** is asked ``empty_value_instructions``: whether the page shows
  a value for the field. Its confidence is **1 - the highest P(yes) over the
  pages**: "no value" is right only if no page shows one, so a field the
  extraction missed scores low.

``decision_model.input`` picks the state: the page image (``images``), the page's
OCR text (``text``), or the page image with that page's own text (``both``). Pages
keep their position in the section, and at most ``max_pages`` are read. A page's
text is cut from the end so its request fits ``max_input_tokens`` (an estimate; see
``idp_common.decision_model.budget``). Text past the cut is never sent, so the
decision model never reads it: a value only there is not seen, and scores low, and
an empty value is not checked against it. A page whose text would be cut to nothing
(the question and image already fill the limit) is not sent: the pass is refused,
because a request with no text would score an empty value from no evidence.

The output has the shape the LLM confidence call's parsed response has, so the
threshold, alert, explainability and HITL code that follows is unchanged. Extra
keys on each leaf (the page that supported it best) pass through into
``explainability_info``.

Errors propagate. A decision-model failure fails the confidence pass exactly as a
Bedrock failure does, and is never converted into an LLM call. Each refused request
is logged with its page first, and the error names every page that was refused.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from idp_common.assessment.presence import has_extracted_value
from idp_common.config.schema_constants import (
    SCHEMA_ITEMS,
    SCHEMA_PROPERTIES,
    SCHEMA_TYPE,
    TYPE_ARRAY,
)
from idp_common.config.schema_utils import deref_schema
from idp_common.decision_model import (
    DecisionModelError,
    DecisionModelRefused,
    DecisionResult,
    NoulAnswer,
    NoulQuestion,
    SystemOneRequest,
    client_from_config,
    encode_image,
    estimate_tokens,
    fit_text,
    merge_metering,
    state_budget,
)

logger = logging.getLogger(__name__)

#: The metering context, beside ``Assessment/bedrock/...``.
METERING_CONTEXT = "Assessment"


@dataclass
class _Leaf:
    """One extracted value: where it sits, and what is asked about it."""

    path: Tuple[Any, ...]
    display: str
    value: Any
    description: str

    @property
    def empty(self) -> bool:
        """No value was extracted: null, blank, or an empty container."""
        return not has_extracted_value(self.value)


def _sub_schema(schema: Dict[str, Any], root: Dict[str, Any]) -> Dict[str, Any]:
    return deref_schema(schema or {}, root) if schema else {}


def _collect_leaves(
    value: Any,
    schema: Dict[str, Any],
    root: Dict[str, Any],
    path: Tuple[Any, ...],
    display: str,
    out: List[_Leaf],
) -> None:
    schema = _sub_schema(schema, root)
    if isinstance(value, dict):
        props = schema.get(SCHEMA_PROPERTIES, {}) if schema else {}
        for key, sub in value.items():
            _collect_leaves(
                sub, props.get(key, {}), root, path + (key,), f"{display}.{key}", out
            )
        return
    if isinstance(value, list) and (
        schema.get(SCHEMA_TYPE) == TYPE_ARRAY or not schema
    ):
        items = schema.get(SCHEMA_ITEMS, {}) if schema else {}
        for index, item in enumerate(value):
            _collect_leaves(
                item, items, root, path + (index,), f"{display}[{index}]", out
            )
        return
    out.append(
        _Leaf(
            path=path,
            display=display,
            value=value,
            description=str(schema.get("description", "") or "").strip(),
        )
    )


def _render_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _skeleton(value: Any, schema: Dict[str, Any], root: Dict[str, Any]) -> Any:
    """An empty assessment in the shape of ``value``, for the leaves to fill."""
    schema = _sub_schema(schema, root)
    if isinstance(value, dict):
        props = schema.get(SCHEMA_PROPERTIES, {}) if schema else {}
        return {k: _skeleton(v, props.get(k, {}), root) for k, v in value.items()}
    if isinstance(value, list) and (
        schema.get(SCHEMA_TYPE) == TYPE_ARRAY or not schema
    ):
        items = schema.get(SCHEMA_ITEMS, {}) if schema else {}
        return [_skeleton(v, items, root) for v in value]
    return None


def _set(tree: Any, path: Tuple[Any, ...], leaf: Dict[str, Any]) -> Any:
    if not path:
        return leaf
    tree[path[0]] = _set(tree[path[0]], path[1:], leaf)
    return tree


def _pages(count: int) -> str:
    return "1 page" if count == 1 else f"{count} pages"


class DecisionModelConfidenceScorer:
    """Scores extracted values with one ``noul`` question each, per page."""

    def __init__(self, confidence_cfg: Any, region: Optional[str] = None):
        self.cfg = confidence_cfg.decision_model
        self.client = client_from_config(self.cfg, region=region)

    def _question(self, leaf: _Leaf) -> NoulQuestion:
        template = (
            self.cfg.empty_value_instructions if leaf.empty else self.cfg.instructions
        )
        text = (
            template.replace("{FIELD}", leaf.display)
            .replace("{DESCRIPTION}", leaf.description or leaf.display)
            .replace("{VALUE}", _render_value(leaf.value))
        )
        return NoulQuestion(instructions=text)

    def score(
        self,
        extraction_results: Dict[str, Any],
        class_schema: Dict[str, Any],
        document_text: str,
        page_images: List[Any],
        page_texts: Optional[List[str]] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any], float]:
        """``(assessment_data, metering, duration_seconds)`` for ``extraction_results``.

        ``assessment_data`` mirrors ``extraction_results``: every scalar becomes
        ``{"confidence", "confidence_reason", ...}``, objects and lists keep their
        shape.

        ``page_texts`` is the section's OCR text per page, in the order of
        ``page_images``. ``input: text`` and ``input: both`` read it; without it,
        ``text`` reads ``document_text`` (the pages joined) as one text, and
        ``both`` is refused, because it cannot pair an image with its own text.
        """
        props = class_schema.get(SCHEMA_PROPERTIES, {})
        leaves: List[_Leaf] = []
        for name, value in extraction_results.items():
            _collect_leaves(
                value, props.get(name, {}), class_schema, (name,), name, leaves
            )
        assessment: Dict[str, Any] = {
            name: _skeleton(value, props.get(name, {}), class_schema)
            for name, value in extraction_results.items()
        }
        if not leaves:
            return assessment, {}, 0.0

        questions = {f"f{i}": self._question(leaf) for i, leaf in enumerate(leaves)}
        # The longest question has to fit beside the state: the window holds the
        # state and one question at a time.
        question_tokens = max(
            estimate_tokens(str(q.instructions)) for q in questions.values()
        )
        states, pages_read = self._states(
            document_text, page_images, page_texts, question_tokens
        )
        names = list(questions)
        chunk = max(1, int(self.cfg.max_questions_per_request))
        requests: List[SystemOneRequest] = []
        owners: List[str] = []
        for page_label, state, images in states:
            for start in range(0, len(names), chunk):
                requests.append(
                    SystemOneRequest(
                        state=state,
                        images=images,
                        questions={
                            n: questions[n] for n in names[start : start + chunk]
                        },
                    )
                )
                owners.append(page_label)

        answered = self._answered(
            owners, self.client.decide_each(requests, METERING_CONTEXT)
        )

        best: Dict[str, Tuple[float, str]] = {}
        for page_label, result in answered:
            for name, answer in result.response.answers.items():
                if not isinstance(answer, NoulAnswer):
                    continue
                p = float(answer.noul)
                if name not in best or p > best[name][0]:
                    best[name] = (p, page_label)
        model = answered[0][1].response.model if answered else "decision model"
        read = _pages(pages_read)

        for name, leaf in zip(names, leaves):
            if name not in best:
                raise ValueError(
                    f"decision model returned no answer for field {leaf.display}"
                )
            p, page_label = best[name]
            if leaf.empty:
                # Asked whether a page shows a value: "empty" is right only when
                # none does, so its confidence is the complement of the best page.
                confidence = 1.0 - p
                reason = (
                    f"Decision model {model}: no value was extracted, and "
                    f"P(a value is shown) is at most {p:.3f}, highest on "
                    f"{page_label} ({read} read)"
                )
            else:
                confidence = p
                reason = (
                    f"Decision model {model}: P(correct) {p:.3f}, highest on "
                    f"{page_label} ({read} read)"
                )
            _set(
                assessment,
                leaf.path,
                {
                    "confidence": round(confidence, 6),
                    "confidence_reason": reason,
                    "confidence_source": "decision_model",
                },
            )
        results = [result for _, result in answered]
        # Summed across requests: the billed time, which is what metering and
        # duration_seconds report. The requests run concurrently, so it overstates
        # the wall-clock latency of the pass.
        duration = sum(r.duration_seconds for r in results)
        return assessment, merge_metering(results), duration

    @staticmethod
    def _answered(
        owners: List[str], results: List[Any]
    ) -> List[Tuple[str, DecisionResult]]:
        """``(page label, result)`` per request, or the failure, raised.

        A refusal is deterministic (most often a state over the context window), so
        each refused request is logged with the page it carried, and the error that
        fails the pass names every refused page. It is raised before any other
        failure: retrying the section cannot get past it.
        """
        refused = [
            (label, result)
            for label, result in zip(owners, results)
            if isinstance(result, DecisionModelRefused)
        ]
        for label, error in refused:
            logger.warning(
                "Decision model refused the confidence request for %s: %s",
                label,
                error,
            )
        if refused:
            labels = ", ".join(dict.fromkeys(label for label, _ in refused))
            raise DecisionModelRefused(
                f"The decision model refused {len(refused)} of {len(results)} "
                f"confidence requests, for {labels}, so the section has no "
                f"decision-model confidence. First refusal: {refused[0][1]}"
            )
        for result in results:
            if isinstance(result, DecisionModelError):
                raise result
        return [
            (label, result)
            for label, result in zip(owners, results)
            if isinstance(result, DecisionResult)
        ]

    def _budget(self, question_tokens: float, images: int) -> float:
        """The text one request may carry beside its question and images."""
        return state_budget(int(self.cfg.max_input_tokens), question_tokens, images)

    def _first_pages(self, pages: List[Any], what: str) -> List[Any]:
        """The first ``max_pages`` of ``pages``, with a warning when that drops some."""
        limit = int(self.cfg.max_pages)
        if len(pages) > limit:
            logger.warning(
                "Section has %d pages %s; the decision model reads the first %d "
                "(extraction.confidence.decision_model.max_pages). A value only on a "
                "later page scores low and goes to review, and an empty value is "
                "checked against the pages read only.",
                len(pages),
                what,
                limit,
            )
        return pages[:limit]

    def _states(
        self,
        document_text: str,
        page_images: List[Any],
        page_texts: Optional[List[str]],
        question_tokens: float,
    ) -> Tuple[List[Tuple[str, str, List[str]]], int]:
        """``([(label, state, images)], pages read)``, per ``decision_model.input``.

        Every page keeps its ORIGINAL position, so a page with no image or no text
        does not shift the page number recorded for every page after it.
        """
        mode = self.cfg.input
        if mode == "text":
            if page_texts is None:
                named = (
                    [("the section text", document_text)]
                    if (document_text or "").strip()
                    else []
                )
            else:
                named = [
                    (f"page {index}", text)
                    for index, text in enumerate(page_texts, start=1)
                    if isinstance(text, str) and text.strip()
                ]
            if not named:
                raise ValueError(
                    "extraction.confidence.decision_model.input is 'text' but the "
                    "section has no OCR text"
                )
            named = self._first_pages(named, "with OCR text")
            budget = self._budget(question_tokens, images=0)
            states = [
                (name, fit_text(text, budget, f"{name}'s OCR text"), [])
                for name, text in named
            ]
            return states, len(named)

        images = [
            (index, img)
            for index, img in enumerate(page_images, start=1)
            if isinstance(img, (bytes, bytearray))
        ]
        if not images:
            raise ValueError(
                f"extraction.confidence.decision_model.input is {mode!r} but the "
                "section has no page images"
            )
        images = self._first_pages(images, "with an image")
        if mode != "both":
            return [
                (f"page {index}", "", [encode_image(bytes(img))])
                for index, img in images
            ], len(images)

        # Each page image with THAT page's text, never the whole section's: the
        # section's text overflows the window on a long section.
        if page_texts is None:
            raise ValueError(
                "extraction.confidence.decision_model.input is 'both', which pairs "
                "each page image with that page's OCR text, but no per-page text "
                "was passed"
            )
        budget = self._budget(question_tokens, images=1)
        states = []
        for index, img in images:
            text = page_texts[index - 1] if index <= len(page_texts) else ""
            name = f"page {index}"
            states.append(
                (
                    name,
                    fit_text(text or "", budget, f"{name}'s OCR text"),
                    [encode_image(bytes(img))],
                )
            )
        return states, len(images)
