# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Extraction confidence by a decision model.

``extraction.confidence.method: decision_model``. Every extracted value becomes one
``noul`` question: "the system says <field> is <value>; is that the correct value?".
Each page of the section is one request, with that page as the state and every
field's question in it, because the state is encoded once and each question reads
that encoding. A value's confidence is its **highest P(yes) over the pages**: a
value is supported by the document if some page supports it.

The output has the shape the LLM confidence call's parsed response has, so the
threshold, alert, explainability and HITL code that follows is unchanged. Extra
keys on each leaf (the page that supported it best) pass through into
``explainability_info``.

Errors propagate. A decision-model failure fails the confidence pass exactly as a
Bedrock failure does, and is never converted into an LLM call.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from idp_common.config.schema_constants import (
    SCHEMA_ITEMS,
    SCHEMA_PROPERTIES,
    SCHEMA_TYPE,
    TYPE_ARRAY,
)
from idp_common.config.schema_utils import deref_schema
from idp_common.decision_model import (
    NoulAnswer,
    NoulQuestion,
    SystemOneRequest,
    client_from_config,
    encode_image,
    merge_metering,
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
    if value is None or value == "":
        return "(empty: no value found)"
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


class DecisionModelConfidenceScorer:
    """Scores extracted values with one ``noul`` question each, per page."""

    def __init__(self, confidence_cfg: Any, region: Optional[str] = None):
        self.cfg = confidence_cfg.decision_model
        self.client = client_from_config(self.cfg, region=region)

    def _question(self, leaf: _Leaf) -> NoulQuestion:
        text = (
            self.cfg.instructions.replace("{FIELD}", leaf.display)
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
    ) -> Tuple[Dict[str, Any], Dict[str, Any], float]:
        """``(assessment_data, metering, duration_seconds)`` for ``extraction_results``.

        ``assessment_data`` mirrors ``extraction_results``: every scalar becomes
        ``{"confidence", "confidence_reason", ...}``, objects and lists keep their
        shape.
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

        states = self._states(document_text, page_images)
        questions = {f"f{i}": self._question(leaf) for i, leaf in enumerate(leaves)}
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

        results = self.client.decide_many(requests, METERING_CONTEXT)

        best: Dict[str, Tuple[float, str]] = {}
        for page_label, result in zip(owners, results):
            for name, answer in result.response.answers.items():
                if not isinstance(answer, NoulAnswer):
                    continue
                p = float(answer.noul)
                if name not in best or p > best[name][0]:
                    best[name] = (p, page_label)
        model = results[0].response.model if results else "decision model"

        for name, leaf in zip(names, leaves):
            if name not in best:
                raise ValueError(
                    f"decision model returned no answer for field {leaf.display}"
                )
            p, page_label = best[name]
            _set(
                assessment,
                leaf.path,
                {
                    "confidence": round(p, 6),
                    "confidence_reason": (
                        f"Decision model {model}: P(correct) {p:.3f}, highest on "
                        f"{page_label} of {len(states)} read"
                    ),
                    "confidence_source": "decision_model",
                },
            )
        # Summed across requests: the billed time, which is what metering and
        # duration_seconds report. The requests run concurrently, so it overstates
        # the wall-clock latency of the pass.
        duration = sum(r.duration_seconds for r in results)
        return assessment, merge_metering(results), duration

    def _states(
        self, document_text: str, page_images: List[Any]
    ) -> List[Tuple[str, str, List[str]]]:
        """``(label, state, images)`` per request group, per ``decision_model.input``."""
        mode = self.cfg.input
        if mode == "text":
            return [("the section text", document_text, [])]
        # The ORIGINAL position is kept, so a page with no image does not shift
        # the page number recorded for every page after it.
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
        limit = int(self.cfg.max_pages)
        if len(images) > limit:
            logger.warning(
                "Section has %d pages; the decision model reads the first %d "
                "(extraction.confidence.decision_model.max_pages). A value only on a "
                "later page scores low and goes to review.",
                len(images),
                limit,
            )
        state = document_text if mode == "both" else ""
        return [
            (f"page {index}", state, [encode_image(bytes(img))])
            for index, img in images[:limit]
        ]
