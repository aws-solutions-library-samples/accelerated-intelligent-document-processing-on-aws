# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Section classification by embedding similarity.

``split_then_classify.section_classification.method: embeddings``. Each section's
page images (the pages ``section_classification.pages`` selects) are embedded with a
Bedrock multimodal embeddings model, averaged into one unit vector, and compared by
cosine similarity with reference vectors for each configured class:

* ``references: examples`` — the class's few-shot example images
  (``x-aws-idp-examples`` entries with an ``x-aws-idp-image-path``). A class with no
  loadable example image has no reference and is never chosen by embeddings.
* ``references: descriptions`` — the class's name and description, embedded as text.

A class's similarity is that of its best-matching reference. The similarities are
turned into probabilities by a softmax at ``embeddings.temperature``; the top one is
the section's confidence, and the full distribution is its
``classification_candidates`` in the shape the LLM path writes. A softmax combines
the top-1 similarity with how far ahead of the runner-up it is, so a section that
is 0.86 like an invoice and 0.85 like a shipping invoice is not confident.

The embeddings label a section only when they are sure of it. A section whose best
similarity is below ``embeddings.min_similarity``, or whose confidence is below
``low_confidence.min_confidence``, is classified by the LLM block, exactly as the
``llm`` method would have classified it; the configuration validator requires
``low_confidence.on_low_confidence: llm`` and a threshold above 0. So is a section
none of whose selected pages has a loadable image, since there is nothing to embed.
Which of the two labelled a section is the first words of its
``classification_reason``: ``Embeddings ...`` or ``Embeddings ... so labeled by the
LLM: ...``.

A failed embedding call fails its section, as a failed LLM labelling call does. A
throttle or another transient error that the Bedrock client's own retries could not
absorb retries the classification step, through the section pool's transient
handling (#2034); neither is ever converted into an LLM call.

Embedding calls are metered under ``SectionClassification/bedrock/<model>`` with the
input-token count Bedrock reports, so their cost is reported separately from the
LLM's. Reference vectors are cached for the life of the process, keyed on the model,
the input type and a hash of the image bytes or text, so a warm Lambda embeds each
example image once rather than once per document. The first document after a cold
start pays for every reference.
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from idp_common import image, utils
from idp_common.discovery.regional_defaults import default_embedding_model_id

if TYPE_CHECKING:
    from idp_common.models import Document

logger = logging.getLogger(__name__)

#: The metering context, beside ``SectionClassification/bedrock/<llm model>``.
METERING_CONTEXT = "SectionClassification"

#: Cohere input types. A section is the query; a reference is the document it is
#: matched against. Titan ignores the field.
QUERY_INPUT_TYPE = "search_query"
REFERENCE_INPUT_TYPE = "search_document"

#: How many vectors the process-wide reference cache holds before it evicts the
#: least recently used. A 1024-dimension vector is about 8 KB as Python floats, so
#: the cap bounds the cache near 16 MB.
REFERENCE_CACHE_SIZE = 2048

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

_cache_lock = threading.Lock()
_reference_cache: "OrderedDict[Tuple[str, str, str], List[float]]" = OrderedDict()


def clear_reference_cache() -> None:
    """Empty the process-wide reference vector cache (for tests)."""
    with _cache_lock:
        _reference_cache.clear()


def _normalise(vector: List[float]) -> List[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if not norm:
        raise ValueError("the embeddings model returned a zero vector")
    return [x / norm for x in vector]


def mean_unit_vector(vectors: List[List[float]]) -> List[float]:
    """The unit vector along the mean of ``vectors``' unit vectors."""
    if not vectors:
        raise ValueError("no vectors to average")
    dims = {len(v) for v in vectors}
    if len(dims) != 1:
        raise ValueError(f"vectors of different dimensions: {sorted(dims)}")
    units = [_normalise(v) for v in vectors]
    return _normalise([sum(column) / len(units) for column in zip(*units)])


def class_similarities(
    query: List[float], references: Dict[str, List[List[float]]]
) -> Dict[str, float]:
    """Each class's best cosine similarity to ``query``.

    ``query`` and every reference must be unit vectors of one dimension. A class
    with no reference is absent from the result.
    """
    scores: Dict[str, float] = {}
    for name, vectors in references.items():
        best: Optional[float] = None
        for vector in vectors:
            if len(vector) != len(query):
                raise ValueError(
                    f"a reference for class {name!r} has {len(vector)} dimensions "
                    f"and the section {len(query)}: vectors from different models "
                    "cannot be compared"
                )
            similarity = sum(a * b for a, b in zip(query, vector))
            best = similarity if best is None else max(best, similarity)
        if best is not None:
            scores[name] = best
    return scores


def softmax_probabilities(
    similarities: Dict[str, float], temperature: float
) -> Dict[str, float]:
    """The softmax of ``similarities`` at ``temperature``."""
    if temperature <= 0:
        raise ValueError(f"temperature must be above 0, got {temperature}")
    if not similarities:
        return {}
    top = max(similarities.values())
    weights = {k: math.exp((v - top) / temperature) for k, v in similarities.items()}
    total = sum(weights.values())
    return {k: w / total for k, w in weights.items()}


@dataclass
class SectionScore:
    """What the embeddings say about one section, before any threshold applies."""

    similarities: Dict[str, float]
    probabilities: Dict[str, float]
    metering: Dict[str, Any] = field(default_factory=dict)

    @property
    def ranked(self) -> List[Tuple[str, float]]:
        return sorted(self.probabilities.items(), key=lambda kv: kv[1], reverse=True)

    @property
    def top_class(self) -> str:
        return self.ranked[0][0]

    @property
    def confidence(self) -> float:
        return self.ranked[0][1]

    @property
    def top_similarity(self) -> float:
        return self.similarities[self.top_class]

    @property
    def margin(self) -> float:
        """Top-1 similarity less the runner-up's (the top's own when it is alone)."""
        others = sorted(
            (v for k, v in self.similarities.items() if k != self.top_class),
            reverse=True,
        )
        return self.top_similarity - (others[0] if others else self.top_similarity)


class EmbeddingSectionLabeler:
    """Labels split-then-classify sections by embedding similarity to references."""

    def __init__(self, service: Any, section_cfg: Any):
        self.service = service
        self.cfg = section_cfg.embeddings
        self.low_confidence = section_cfg.low_confidence
        self.model_id = (self.cfg.model or "").strip() or default_embedding_model_id(
            service.region
        )
        self._references: Optional[Dict[str, List[List[float]]]] = None
        self._references_metering: Dict[str, Any] = {}
        self._metering_lock = threading.Lock()
        # Two locks: ``references()`` holds the first while it makes embedding
        # calls, and each call takes the second to build the client.
        self._lock = threading.Lock()
        self._client_lock = threading.Lock()
        self._client = None

    # ── embedding calls ──────────────────────────────────────────────────────
    @property
    def client(self):
        with self._client_lock:
            if self._client is None:
                from idp_common.bedrock.client import BedrockClient

                self._client = BedrockClient(
                    region=self.service.region, metrics_enabled=False
                )
            return self._client

    def _metering_for(self, input_tokens: Optional[int]) -> Dict[str, Any]:
        units: Dict[str, Any] = {"requests": 1}
        if input_tokens is not None:
            units["inputTokens"] = input_tokens
            units["totalTokens"] = input_tokens
        from idp_common.bedrock.model_utils import metering_model_id

        return {f"{METERING_CONTEXT}/bedrock/{metering_model_id(self.model_id)}": units}

    def _embed(
        self,
        *,
        text: Optional[str] = None,
        image_bytes: Optional[bytes] = None,
        input_type: str,
        where: str,
    ) -> Tuple[List[float], Dict[str, Any]]:
        """One embedding call: the unit vector and its metering."""
        usage: Dict[str, int] = {}
        # The client retries what it retries. A failure it still raises propagates
        # to the section pool, which retries the step for a transient one (#2034)
        # and fails the section for any other; neither becomes an LLM call.
        vector = self.client.generate_embedding(
            text=text,
            image_bytes=image_bytes,
            model_id=self.model_id,
            input_type=input_type,
            usage=usage,
        )
        if not vector:
            raise ValueError(f"the embeddings model returned no vector for {where}")
        return _normalise(list(vector)), self._metering_for(usage.get("inputTokens"))

    def _embed_cached(
        self, *, text: Optional[str], image_bytes: Optional[bytes], where: str
    ) -> Tuple[List[float], Dict[str, Any]]:
        """A reference vector, from the process cache when this one has been seen."""
        digest = hashlib.sha256(
            image_bytes if image_bytes is not None else (text or "").encode("utf-8")
        ).hexdigest()
        key = (self.model_id, REFERENCE_INPUT_TYPE, digest)
        with _cache_lock:
            if key in _reference_cache:
                _reference_cache.move_to_end(key)
                return list(_reference_cache[key]), {}
        vector, metering = self._embed(
            text=text,
            image_bytes=image_bytes,
            input_type=REFERENCE_INPUT_TYPE,
            where=where,
        )
        with _cache_lock:
            _reference_cache[key] = vector
            while len(_reference_cache) > REFERENCE_CACHE_SIZE:
                _reference_cache.popitem(last=False)
        return vector, metering

    def _prepare_image(self, data: bytes) -> bytes:
        side = int(self.cfg.max_image_dimension)
        return image.resize_image(data, side, side)

    # ── references ───────────────────────────────────────────────────────────
    def _class_names(self) -> List[str]:
        return [d.type_name for d in self.service.document_types if d.type_name]

    def _example_images(self) -> Dict[str, List[Tuple[str, bytes]]]:
        """Each class's few-shot example images, as (path, bytes)."""
        from idp_common import s3
        from idp_common.config.schema_constants import (
            LEGACY_IMAGE_PATH,
            X_AWS_IDP_DOCUMENT_TYPE,
            X_AWS_IDP_EXAMPLES,
            X_AWS_IDP_IMAGE_PATH,
        )
        from idp_common.utils.few_shot_example_builder import (
            _example_field,
            _get_image_files_from_path,
        )

        found: Dict[str, List[Tuple[str, bytes]]] = {}
        for schema in self.service.config.classes or []:
            # The same key ``ClassificationService`` names its classes by.
            name = schema.get(X_AWS_IDP_DOCUMENT_TYPE)
            if not name:
                continue
            for example in schema.get(X_AWS_IDP_EXAMPLES) or []:
                path = _example_field(example, X_AWS_IDP_IMAGE_PATH, LEGACY_IMAGE_PATH)
                if not path:
                    continue
                try:
                    files = _get_image_files_from_path(str(path))
                except Exception as e:
                    logger.warning(
                        "Embeddings: could not list example images %s for class %r: %s",
                        path,
                        name,
                        e,
                    )
                    continue
                for file_path in files:
                    try:
                        if file_path.startswith("s3://"):
                            data = s3.get_binary_content(file_path)
                        else:
                            with open(file_path, "rb") as f:
                                data = f.read()
                    except Exception as e:
                        logger.warning(
                            "Embeddings: could not read example image %s for class "
                            "%r: %s",
                            file_path,
                            name,
                            e,
                        )
                        continue
                    found.setdefault(str(name), []).append((file_path, data))
        return found

    def _build_references(self) -> Dict[str, List[List[float]]]:
        names = set(self._class_names())
        references: Dict[str, List[List[float]]] = {}
        if self.cfg.references == "descriptions":
            for doc_type in self.service.document_types:
                if not doc_type.type_name:
                    continue
                text = doc_type.type_name
                if doc_type.description:
                    text = f"{text}: {doc_type.description}"
                vector, call_metering = self._embed_cached(
                    text=text,
                    image_bytes=None,
                    where=f"the description of class {doc_type.type_name!r}",
                )
                references[doc_type.type_name] = [vector]
                self._record_unreported_metering(call_metering)
        else:
            for name, images in self._example_images().items():
                for file_path, data in images:
                    vector, call_metering = self._embed_cached(
                        text=None,
                        image_bytes=self._prepare_image(data),
                        where=f"example image {file_path}",
                    )
                    references.setdefault(name, []).append(vector)
                    self._record_unreported_metering(call_metering)
            missing = sorted(names - set(references))
            if missing:
                logger.warning(
                    "Embeddings: %d class(es) have no example image and can be "
                    "assigned only by the LLM fallback: %s",
                    len(missing),
                    ", ".join(missing),
                )
        if len(references) < 2:
            raise ValueError(
                "section_classification.embeddings needs references for at least two "
                f"classes and has {len(references)}: with references "
                f"'{self.cfg.references}', "
                + (
                    "add x-aws-idp-examples with an x-aws-idp-image-path to the classes"
                    if self.cfg.references == "examples"
                    else "configure at least two classes"
                )
            )
        return references

    def references(self) -> Dict[str, List[List[float]]]:
        """The reference vectors, built once per labeler.

        Their metering is not returned here: a section that fails after building
        them would take it down with it. It accumulates on the labeler, call by
        call, and the section pool collects it once with
        :meth:`take_unreported_metering` whatever the sections' outcomes.
        """
        with self._lock:
            if self._references is None:
                self._references = self._build_references()
            return self._references

    def take_unreported_metering(self) -> Dict[str, Any]:
        """Embedding calls no section result carries, and mark them reported.

        The reference calls (including those of a build that failed part-way) and
        the calls of a section that then failed: made and billed either way.
        """
        with self._metering_lock:
            metering, self._references_metering = self._references_metering, {}
            return metering

    def _record_unreported_metering(self, metering: Dict[str, Any]) -> None:
        if not metering:
            return
        with self._metering_lock:
            self._references_metering = utils.merge_metering_data(
                self._references_metering, metering
            )

    # ── labelling ────────────────────────────────────────────────────────────
    def _section_images(
        self, document: "Document", page_ids: List[str]
    ) -> List[Tuple[str, bytes]]:
        loaded: List[Tuple[str, bytes]] = []
        for page_id in page_ids:
            page = document.pages.get(page_id)
            if page is None or not page.image_uri:
                continue
            try:
                data = image.prepare_image(page.image_uri)
            except Exception as e:
                logger.warning(
                    "Embeddings: could not load the image of page %s: %s", page_id, e
                )
                continue
            loaded.append((page_id, self._prepare_image(data)))
        return loaded

    def score(
        self, document: "Document", page_ids: List[str], label: str
    ) -> Optional[SectionScore]:
        """The section's similarities and probabilities, or None with no image."""
        references = self.references()
        images = self._section_images(document, page_ids)
        if not images:
            return None
        metering: Dict[str, Any] = {}
        vectors = []
        try:
            for page_id, data in images:
                vector, call_metering = self._embed(
                    image_bytes=data,
                    input_type=QUERY_INPUT_TYPE,
                    where=f"page {page_id} of {label}",
                )
                vectors.append(vector)
                metering = utils.merge_metering_data(metering, call_metering)
            similarities = class_similarities(mean_unit_vector(vectors), references)
        except BaseException:
            # The pages embedded before the failure were billed.
            self._record_unreported_metering(metering)
            raise
        return SectionScore(
            similarities,
            softmax_probabilities(similarities, float(self.cfg.temperature)),
            metering,
        )

    def label(
        self,
        document: "Document",
        pages_content: Dict[str, str],
        page_ids: List[str],
        label: str,
        llm_label: Callable[[], LabelResult],
    ) -> LabelResult:
        """Label one section; ``llm_label`` labels what the embeddings are unsure of."""
        names = self._class_names()
        if len(names) < 2:
            only = names[0] if names else self.service._fallback_class()
            return only, None, [], "Only one class is configured.", {}, None

        scored = self.score(document, page_ids, label)
        if scored is None:
            why = (
                f"Embeddings ({self.model_id}): no image could be loaded for pages "
                f"{', '.join(page_ids)}"
            )
            return self._by_llm(why, {}, llm_label, label)

        choice = scored.top_class
        probability = scored.confidence
        candidates = [
            {"class": name, "probability": round(float(p), 6)}
            for name, p in scored.ranked
        ]
        reason = (
            f"Embeddings ({self.model_id}): {choice} (similarity "
            f"{scored.top_similarity:.3f}, margin {scored.margin:.3f}, probability "
            f"{probability:.3f})"
        )
        min_similarity = float(self.cfg.min_similarity or 0.0)
        min_confidence = float(self.low_confidence.min_confidence or 0.0)
        if min_similarity > 0 and scored.top_similarity < min_similarity:
            return self._by_llm(
                f"{reason}; similarity below min_similarity {min_similarity:.3f}",
                scored.metering,
                llm_label,
                label,
            )
        if min_confidence > 0 and probability < min_confidence:
            return self._by_llm(
                f"{reason}; probability below min_confidence {min_confidence:.3f}",
                scored.metering,
                llm_label,
                label,
            )
        return choice, probability, candidates, reason, scored.metering, None

    def _by_llm(
        self,
        why: str,
        metering: Dict[str, Any],
        llm_label: Callable[[], LabelResult],
        label: str,
    ) -> LabelResult:
        logger.info("%s for %s; labeling it with the LLM", why, label)
        try:
            llm = llm_label()
        except BaseException:
            # The section's embedding calls were made; the pool collects them.
            self._record_unreported_metering(metering)
            raise
        return (
            llm[0],
            llm[1],
            llm[2],
            f"{why}, so labeled by the LLM: {llm[3] or ''}".rstrip(": "),
            utils.merge_metering_data(metering, llm[4]),
            llm[5],
        )
