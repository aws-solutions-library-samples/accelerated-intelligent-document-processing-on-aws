# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Classification service for documents using LLMs or SageMaker UDOP models.

This module provides a service for classifying documents using various backends:
1. Bedrock LLMs with text and image support
2. SageMaker UDOP models for multimodal document classification

Classification methods:
- multimodalPageLevelClassification: Page-by-page classification with document boundary detection
  using a sequence segmentation approach similar to BIO (Begin-Inside-Outside) tagging.
  Each page receives both a document type and a boundary indicator ("start" or "continue")
  to enable accurate segmentation of multi-document packets.
- textbasedHolisticClassification: Holistic document analysis for segment identification
  across the entire document packet at once.
"""

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Union

import boto3
from botocore.exceptions import ClientError

from idp_common import bedrock, image, s3, utils
from idp_common.classification.class_confidence import (
    append_class_confidence_block,
    resolve_class_and_confidence,
    resolve_top_k,
)
from idp_common.classification.models import (
    ClassificationResult,
    DocumentClassification,
    DocumentSection,
    DocumentType,
    PageClassification,
    Segment,
)
from idp_common.config.models import IDPConfig
from idp_common.config.schema_constants import (
    REF_FIELD,
    SCHEMA_ITEMS,
    SCHEMA_PROPERTIES,
    SCHEMA_TYPE,
    TYPE_ARRAY,
    TYPE_OBJECT,
    UNCLASSIFIED_CLASS,
    X_AWS_IDP_CLASSIFICATION,
    X_AWS_IDP_DOCUMENT_NAME_REGEX,
    X_AWS_IDP_DOCUMENT_TYPE,
    X_AWS_IDP_EXCLUDE_FROM_PROCESSING,
    X_AWS_IDP_EXCLUSION_REASON,
    X_AWS_IDP_PAGE_CONTENT_REGEX,
)
from idp_common.config.schema_utils import deref_schema
from idp_common.models import Document, ProcessingIssue, Section, Status
from idp_common.utils import (
    extract_json_from_text,
    extract_structured_data_from_text,
    parse_confidence,
)
from idp_common.utils.few_shot_example_builder import build_few_shot_examples_content

logger = logging.getLogger(__name__)

# Why a page came back ``unclassified``, recorded in the page classification's
# ``metadata["unclassified_reason"]``. The two are not the same event and
# ``_record_unclassified_page_issues`` reports them at different severities: a page
# the service had nothing to send may simply be empty, whereas a classification
# attempt that errored or ran out of retries went wrong.
UNCLASSIFIED_FAILED = "failed"
UNCLASSIFIED_NO_CONTENT = "no_content"

# Which call's configuration is being resolved. These are not three
# classification *methods* — they are the one single-pass call, and the two
# halves of the two-stage strategy, which need different prompts, may use
# different models, and are metered separately so that a two-stage deployment's
# cost table shows where the money went instead of summing both under one line.
STAGE_SINGLE_PASS = "classification"
STAGE_SEGMENTATION = "segmentation"
STAGE_LABELING = "labeling"

# Bounds on what a classification ProcessingIssue's ``root_cause`` may carry. The
# text comes from model output or a service error message, and the issue rides to
# DynamoDB inside the section map, whose 400 KB item ceiling the assessment path
# guards explicitly while ``serialize_processing_issues`` truncates nothing.
_MAX_ISSUE_DETAIL_CHARS = 500
_MAX_ISSUE_DETAILS = 3


@dataclass
class PageContextData:
    """Data class for page context information."""

    page_id: str
    text_content: Optional[str] = None
    image_content: Optional[bytes] = None


def aggregate_page_confidence(values: List[Optional[float]]) -> Optional[float]:
    """Aggregate per-page classification confidence to one section score.

    Returns the MINIMUM, and ``None`` (not scored) if the list is empty or if
    ANY page is unscored.

    Two deliberate choices:

    - **min, not mean.** A mean hides the single page the classifier was unsure
      about, which is exactly the page a reviewer needs to look at; and a
      section's class cannot be more certain than its least certain page.
    - **``None`` absorbs.** If any page in the section has no score, the section
      genuinely has no score — reporting the min of the *scored* subset would
      quietly present a partial aggregate as a whole-section number.
    """
    if not values:
        return None
    if any(v is None for v in values):
        return None
    return min(v for v in values if v is not None)


class ClassificationService:
    """Service for classifying documents using various backends."""

    # Configuration for the SageMaker retry mechanism
    MAX_RETRIES = 7
    INITIAL_BACKOFF = 2  # seconds
    MAX_BACKOFF = 300  # 5 minutes

    # Classification method options (deprecated; see GRANULARITY_* below)
    MULTIMODAL_PAGE_LEVEL = "multimodalPageLevelClassification"
    TEXTBASED_HOLISTIC = "textbasedHolisticClassification"

    # Granularity: how many calls classification makes. Orthogonal to modality
    # (what each call sees) — which is the whole point of replacing the two
    # `classificationMethod` values, each of which bundled one of each.
    GRANULARITY_PAGE = "page"
    GRANULARITY_PACKET = "packet"

    # Strategy: whether boundaries and classes come from one call or two.
    STRATEGY_SINGLE_PASS = "single_pass"
    STRATEGY_TWO_STAGE = "two_stage"

    # Stage names, re-exported on the class for callers and tests.
    STAGE_SINGLE_PASS = STAGE_SINGLE_PASS
    STAGE_SEGMENTATION = STAGE_SEGMENTATION
    STAGE_LABELING = STAGE_LABELING

    # The metering context each stage's tokens are billed under. Single-pass
    # stays `Classification` with unchanged units, so a cost comparison across
    # this change is a straight read; two-stage splits into two lines, which is
    # what makes "is Stage B actually cheaper than labeling every page?"
    # answerable from the cost table.
    STAGE_METERING_CONTEXT = {
        STAGE_SINGLE_PASS: "Classification",
        STAGE_SEGMENTATION: "Segmentation",
        STAGE_LABELING: "Labeling",
    }

    # Soft cap on the number of schema attribute names emitted per class
    # when rendering the optional {CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
    # placeholder. Prevents pathologically large schemas from bloating the
    # classification prompt. If a class exceeds the cap, the rendered list
    # is truncated and a "...(+N more)" suffix is appended, and a warning
    # is logged.
    MAX_ATTRIBUTES_PER_CLASS = 50

    # Hard ceiling on names the schema WALK itself will build, as opposed to the
    # soft cap above which truncates the walk's RESULT. Needed because $ref
    # dereferencing lets a non-cyclic $defs DAG be re-entered on every sibling
    # branch, so a small schema can expand combinatorially (a 2 KB schema with a
    # 3-way fan-out 12 levels deep produced ~265K names, and 16 deep ~14M) —
    # unbounded work in a Lambda that the result-level cap never sees. Set well
    # above MAX_ATTRIBUTES_PER_CLASS so the "...(+N more)" overflow count stays
    # accurate for every schema anyone would legitimately author.
    _MAX_WALK_NAMES = 10 * MAX_ATTRIBUTES_PER_CLASS

    def __init__(
        self,
        region: str | None = None,
        max_workers: int = 20,
        config: dict[str, Any] | IDPConfig | None = None,
        backend: str = "bedrock",
        cache_table: str | None = None,
    ):
        """
        Initialize the classification service.

        Args:
            region: AWS region for backend services
            max_workers: Maximum number of concurrent workers
            config: Configuration dictionary or IDPConfig model
            backend: Classification backend to use ('bedrock' or 'sagemaker')
            cache_table: Optional DynamoDB table name for caching classification results
        """
        # Convert dict to IDPConfig if needed
        if config is not None and isinstance(config, dict):
            config_model: IDPConfig = IDPConfig(**config)
        elif config is None:
            config_model = IDPConfig()
        else:
            config_model = config

        self.config = config_model
        self.region = region or os.environ.get("AWS_REGION")
        self.max_workers = max_workers
        self.document_types = self._load_document_types()
        self.valid_doc_types: Set[str] = {dt.type_name for dt in self.document_types}
        self.has_single_class = len(self.document_types) == 1
        self.single_class_name = (
            self.document_types[0].type_name if self.has_single_class else None
        )
        self.backend = backend.lower()

        # Initialize caching
        self.cache_table_name = cache_table or os.environ.get(
            "CLASSIFICATION_CACHE_TABLE"
        )
        self.cache_table = None
        if self.cache_table_name:
            dynamodb = boto3.resource("dynamodb", region_name=self.region)
            self.cache_table = dynamodb.Table(self.cache_table_name)  # pyright: ignore[reportAttributeAccessIssue]
            logger.info(
                f"Classification caching enabled using table: {self.cache_table_name}"
            )
        else:
            logger.info("Classification caching disabled")

        # Validate backend choice
        if self.backend not in ["bedrock", "sagemaker"]:
            logger.warning(f"Invalid backend '{backend}', falling back to 'bedrock'")
            self.backend = "bedrock"

        # Initialize backend-specific clients
        if self.backend == "bedrock":
            # Get model_id from typed config (type-safe access)
            model_id = self.config.classification.model
            if not model_id:
                raise ValueError("No model ID specified in configuration for Bedrock")
            self.bedrock_model = model_id
            logger.info(
                f"Initialized classification service with Bedrock backend using model {model_id}"
            )
        else:  # sagemaker
            # Note: SageMaker endpoint name not in config models - use env var
            endpoint_name = os.environ.get("SAGEMAKER_ENDPOINT_NAME")
            if not endpoint_name:
                raise ValueError("No SageMaker endpoint name specified in environment")
            self.sm_client = boto3.client("sagemaker-runtime", region_name=self.region)
            self.sagemaker_endpoint = endpoint_name
            logger.info(
                f"Initialized classification service with SageMaker backend using endpoint {endpoint_name}"
            )

        # Get max pages for classification (1 to ALL)
        self.max_pages_for_classification = (
            self.config.classification.maxPagesForClassification
        )

        # Resolve granularity. `granularity` is authoritative when set; when
        # empty it is derived from the deprecated `classificationMethod`, so an
        # existing configuration keeps its behaviour with no edit.
        configured_granularity = self.config.classification.granularity
        self.granularity_is_explicit = bool(configured_granularity)
        self.granularity = configured_granularity or self.GRANULARITY_PAGE

        if self.granularity_is_explicit:
            # Keep the deprecated field consistent with the resolved
            # granularity, so the several places that still branch on it (the
            # extraction and assessment services read it to decide whether a
            # per-page signal exists) agree with what actually ran.
            self._classification_method = (
                self.TEXTBASED_HOLISTIC
                if self.granularity == self.GRANULARITY_PACKET
                else self.MULTIMODAL_PAGE_LEVEL
            )
        else:
            # Assigning through the property derives granularity from it.
            method = self.config.classification.classificationMethod
            if method not in (self.MULTIMODAL_PAGE_LEVEL, self.TEXTBASED_HOLISTIC):
                logger.warning(
                    f"Invalid classification method '{method}', falling back to "
                    f"'{self.MULTIMODAL_PAGE_LEVEL}'"
                )
                method = self.MULTIMODAL_PAGE_LEVEL
            self.classification_method = method

        self.strategy = self.config.classification.strategy

        send_text, send_images = self._resolve_modality()
        if self.strategy == self.STRATEGY_TWO_STAGE:
            labeling = self.config.classification.labeling
            logger.info(
                "Using TWO-STAGE classification: Stage A finds boundaries at %s "
                "granularity (boundary_hints=%s), Stage B labels each segment "
                "with one %s call over its %s page(s). Metered separately as "
                "Segmentation and Labeling.",
                self.granularity,
                self.config.classification.segmentation.boundary_hints,
                labeling.method,
                labeling.k if labeling.pages == "first_k" else labeling.pages,
            )

        if self.granularity == self.GRANULARITY_PACKET:
            packet_cfg = self.config.classification.packet
            logger.info(
                "Using PACKET classification granularity (text=%s, images=%s, "
                "window_pages=%s, overlap_pages=%s)%s",
                send_text,
                send_images,
                packet_cfg.window_pages or "unwindowed",
                packet_cfg.overlap_pages,
                ""
                if self.granularity_is_explicit
                else " derived from classificationMethod",
            )
        else:
            logger.info(
                "Using PAGE classification granularity with document boundary "
                "detection (text=%s, images=%s)",
                send_text,
                send_images,
            )

    @property
    def classification_method(self) -> str:
        """The deprecated ``classificationMethod`` value, kept in sync with granularity.

        A property rather than a plain attribute because assigning it after
        construction is an established way to switch methods — the shipped
        notebooks do it, and so does the test suite. Dispatch now reads
        ``granularity``, so a plain attribute would have left every such
        assignment silently inert: the caller sets holistic, the service runs
        page-level, and nothing says so.

        Assignment is ignored for granularity purposes when ``granularity`` was
        set explicitly in the configuration, since that is the authoritative
        setting and the deprecated one must not override it.
        """
        return self._classification_method

    @classification_method.setter
    def classification_method(self, value: str) -> None:
        self._classification_method = value
        if getattr(self, "granularity_is_explicit", False):
            return
        self.granularity = (
            self.GRANULARITY_PACKET
            if value == self.TEXTBASED_HOLISTIC
            else self.GRANULARITY_PAGE
        )

    def _load_document_types(self) -> List[DocumentType]:
        """Load document types from configuration with regex patterns."""
        doc_types = []

        # Get document types from typed config (type-safe access)
        classes = self.config.classes
        for schema in classes:
            classification_meta = schema.get(X_AWS_IDP_CLASSIFICATION, {})

            # Support both new top-level format and legacy nested format for regex patterns
            document_name_regex = schema.get(
                X_AWS_IDP_DOCUMENT_NAME_REGEX
            ) or classification_meta.get("documentNamePattern")
            document_page_content_regex = schema.get(
                X_AWS_IDP_PAGE_CONTENT_REGEX
            ) or classification_meta.get("pageContentPattern")

            # Excluded-from-processing flag + optional short reason.
            # Accepts both the x-aws-idp-* schema extension and a legacy
            # snake_case key for convenience in handwritten configs.
            excluded = bool(
                schema.get(X_AWS_IDP_EXCLUDE_FROM_PROCESSING)
                or schema.get("exclude_from_processing")
                or classification_meta.get("excludeFromProcessing")
            )
            exclusion_reason = (
                schema.get(X_AWS_IDP_EXCLUSION_REASON)
                or schema.get("exclusion_reason")
                or classification_meta.get("exclusionReason")
            )

            doc_types.append(
                DocumentType(
                    type_name=schema.get(X_AWS_IDP_DOCUMENT_TYPE, ""),
                    description=schema.get("description", ""),
                    document_name_regex=document_name_regex,
                    document_page_content_regex=document_page_content_regex,
                    excluded=excluded,
                    exclusion_reason=exclusion_reason,
                )
            )

        if not doc_types:
            # Add a default type if none are defined
            logger.warning(
                "No document types defined in configuration, using default 'unclassified' type"
            )
            doc_types.append(
                DocumentType(
                    type_name=UNCLASSIFIED_CLASS,
                    description="A document that does not match any known type.",
                )
            )

        return doc_types

    def get_doc_type(self, class_name: Optional[str]) -> Optional[DocumentType]:
        """Return the DocumentType for a given class name, or None."""
        if not class_name:
            return None
        for doc_type in self.document_types:
            if doc_type.type_name == class_name:
                return doc_type
        return None

    def _mark_excluded_sections(self, document: Document) -> Document:
        """
        Populate ``Section.excluded`` / ``Section.exclusion_reason`` for any
        section whose classification corresponds to a class that was
        configured with ``x-aws-idp-exclude-from-processing: true``.

        This runs once after classification completes so all downstream
        services (extraction, assessment, summarization, rule_validation,
        evaluation) can skip excluded sections by inspecting these flags.
        """
        if not document.sections:
            return document
        for section in document.sections:
            doc_type = self.get_doc_type(section.classification)
            if doc_type and doc_type.excluded:
                section.excluded = True
                section.exclusion_reason = doc_type.exclusion_reason
                logger.info(
                    "Section %s classified as excluded class '%s' (reason=%s); "
                    "downstream processing will skip it.",
                    section.section_id,
                    section.classification,
                    doc_type.exclusion_reason or "excluded",
                )
            else:
                # Reset in case of re-classification: ensure no stale flag
                # survives if a section's class no longer maps to an
                # excluded class.
                section.excluded = False
                section.exclusion_reason = None
        return document

    def _check_document_name_regex(self, document: Document) -> Optional[str]:
        """
        Check if document name matches any class regex patterns.

        Args:
            document: Document object to check

        Returns:
            Matched class name if found, None otherwise
        """
        # Check document name against all class regex patterns
        for doc_type in self.document_types:
            if doc_type._compiled_name_regex and doc_type._compiled_name_regex.search(
                document.id
            ):
                logger.info(
                    f"Document name regex match: '{document.id}' matched pattern '{doc_type.document_name_regex}' for class '{doc_type.type_name}'"
                )
                return doc_type.type_name
        return None

    def _limit_pages_for_classification(self, document: Document) -> Document:
        """
        Limit the number of pages used for classification based on maxPagesForClassification setting.

        Args:
            document: Original document

        Returns:
            Document with limited pages for classification
        """
        # "ALL" means use all pages
        if str(self.max_pages_for_classification).upper() == "ALL":
            return document

        try:
            max_pages = int(self.max_pages_for_classification)
        except (ValueError, TypeError):
            logger.warning(
                f"Invalid maxPagesForClassification value: {self.max_pages_for_classification}, using ALL pages"
            )
            return document

        # 0 or negative means ALL pages (backward compatibility)
        if max_pages <= 0:
            return document
        try:
            if len(document.pages) <= max_pages:
                return document

            # Get first N pages
            sorted_page_ids = sorted(
                document.pages.keys(),
                key=lambda x: int(x) if x.isdigit() else float("inf"),
            )
            limited_page_ids = sorted_page_ids[:max_pages]

            # Create limited document
            limited_pages = {pid: document.pages[pid] for pid in limited_page_ids}
            document_id = document.id if document.id else ""
            limited_document = Document(
                id=document_id + f"_limited_{max_pages}",
                pages=limited_pages,
                status=document.status,
                workflow_execution_arn=document.workflow_execution_arn,
            )

            logger.info(
                f"Limited classification to first {max_pages} pages out of {len(document.pages)} total pages"
            )
            return limited_document

        except (ValueError, TypeError):
            logger.warning(
                f"Invalid maxPagesForClassification value: {self.max_pages_for_classification}, using ALL pages"
            )
            return document

    def _apply_limited_classification_to_all_pages(
        self, original_document: Document, classified_document: Document
    ) -> Document:
        """
        Apply classification results from limited pages to all pages in the original document.

        Args:
            original_document: Original document with all pages
            classified_document: Document with classified limited pages

        Returns:
            Original document with classification applied to all pages
        """
        if not classified_document.sections:
            logger.warning("No sections found in classified document")
            return original_document

        # Get the most common classification from the limited pages
        classifications = {}
        for section in classified_document.sections:
            doc_type = section.classification
            classifications[doc_type] = classifications.get(doc_type, 0) + len(
                section.page_ids
            )

        # Use the most frequent classification
        primary_classification = max(
            classifications.keys(), key=lambda k: classifications[k]
        )

        # Apply to all pages in original document.
        #
        # Confidence is deliberately left unscored (None) for EVERY page here,
        # including the pages that were classified: this path exists because
        # maxPagesForClassification stopped the classifier early, so the class is
        # extrapolated from a sample to pages the model never saw. There is no
        # defensible score for those pages, and reporting one for the sampled
        # pages only would attach a per-page number to a document-level guess.
        # (It previously claimed 1.0 for all of them.)
        for page_id, page in original_document.pages.items():
            page.classification = primary_classification
            page.confidence = None

        # Create single section with all pages
        section = self._create_section(
            section_id="1",
            doc_type=primary_classification,
            pages=list(original_document.pages.keys()),
        )
        if isinstance(section, Section):
            original_document.sections = [section]
        else:
            # Handle DocumentSection - convert to Section
            original_document.sections = [
                Section(
                    section_id=section.section_id,
                    classification=section.classification.doc_type,
                    page_ids=[page.page_id for page in section.pages],
                )
            ]
        # Transfer metering data from classified document to original document
        if classified_document.metering:
            original_document.metering = utils.merge_metering_data(
                original_document.metering, classified_document.metering
            )

        # Transfer errors from classification
        if classified_document.errors:
            original_document.errors.extend(classified_document.errors)

        # Transfer metadata from classification
        if classified_document.metadata:
            original_document.metadata.update(classified_document.metadata)

        logger.info(
            f"Applied classification '{primary_classification}' from {len(classified_document.pages)} pages to all {len(original_document.pages)} pages"
        )
        return original_document

    def _preload_page_content(
        self, document: Document, context_size: int
    ) -> Dict[str, PageContextData]:
        """
        Pre-load text and image content for all pages when context is needed.

        Each modality is loaded only when the active configuration will actually
        send it. The image half is the one that matters: this method loads
        **every** page of the document (any page can be some other page's
        context), so an S3 GET plus a Pillow resize per page was being paid for
        images a text-only prompt then discarded.

        Args:
            document: Document with pages to load
            context_size: Number of context pages either side. Zero means the
                caller is not using context at all and nothing is loaded.

        Returns:
            Dictionary mapping page_id to PageContextData with loaded content
        """
        page_content_cache: Dict[str, PageContextData] = {}

        if context_size <= 0:
            return page_content_cache

        # Type-safe access to image config
        target_width = self.config.classification.image.target_width
        target_height = self.config.classification.image.target_height

        send_text, send_images = self._resolve_modality()

        for page_id, page in document.pages.items():
            text_content = None
            image_content = None

            # Load text content
            if page.parsed_text_uri and send_text:
                try:
                    text_content = s3.get_text_content(page.parsed_text_uri)
                except Exception as e:
                    logger.warning(
                        f"Failed to load text content for page {page_id}: {e}"
                    )

            # Load image content
            if page.image_uri and send_images:
                try:
                    image_content = image.prepare_image(
                        page.image_uri, target_width, target_height
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to load image content for page {page_id}: {e}"
                    )

            page_content_cache[page_id] = PageContextData(
                page_id=page_id,
                text_content=text_content,
                image_content=image_content,
            )

        logger.info(
            "Pre-loaded content for %d pages for context-aware classification "
            "(text=%s, images=%s)",
            len(page_content_cache),
            send_text,
            send_images,
        )
        return page_content_cache

    def _get_context_for_page(
        self,
        page_id: str,
        sorted_page_ids: List[str],
        page_content_cache: Dict[str, PageContextData],
        context_size: int,
    ) -> Dict[str, Any]:
        """
        Get context data for a specific page.

        Args:
            page_id: The page being classified
            sorted_page_ids: All page IDs in sorted order
            page_content_cache: Pre-loaded page content
            context_size: Number of pages before/after to include

        Returns:
            Dictionary with before_texts, after_texts, before_images, after_images
        """
        try:
            current_idx = sorted_page_ids.index(page_id)
        except ValueError:
            return {
                "before_texts": [],
                "after_texts": [],
                "before_images": [],
                "after_images": [],
            }

        # Get indices for context pages
        before_start = max(0, current_idx - context_size)
        after_end = min(len(sorted_page_ids), current_idx + context_size + 1)

        before_page_ids = sorted_page_ids[before_start:current_idx]
        after_page_ids = sorted_page_ids[current_idx + 1 : after_end]

        # Gather context content
        before_texts = []
        before_images = []
        for pid in before_page_ids:
            if pid in page_content_cache:
                ctx = page_content_cache[pid]
                if ctx.text_content:
                    before_texts.append(ctx.text_content)
                if ctx.image_content:
                    before_images.append(ctx.image_content)

        after_texts = []
        after_images = []
        for pid in after_page_ids:
            if pid in page_content_cache:
                ctx = page_content_cache[pid]
                if ctx.text_content:
                    after_texts.append(ctx.text_content)
                if ctx.image_content:
                    after_images.append(ctx.image_content)

        return {
            "before_texts": before_texts,
            "after_texts": after_texts,
            "before_images": before_images,
            "after_images": after_images,
        }

    def _pin_page_class(
        self, page_result: PageClassification, class_name: str
    ) -> PageClassification:
        """Overwrite a page result's class with a class already known to be right.

        Used when the document's name matched a class's
        ``x-aws-idp-document-name-regex`` but the model still had to run for the
        per-page ``document_boundary`` signal (GitHub issue #705). The regex match
        is a deterministic assertion by the operator, so it wins over the model's
        class guess and carries confidence 1.0 — the same class/confidence the
        zero-inference short-circuit assigns.

        Pinning happens *before* section splitting so boundaries are derived from
        the boundary signal alone. Otherwise ``_create_llm_determined_sections``
        would also split on class flips that are about to be overwritten anyway.

        Error results are pinned too: ``metadata["error"]`` is left intact, so the
        page's failure is still collected into ``document.errors``.
        """
        if page_result.classification.doc_type != class_name:
            logger.debug(
                "Pinning page %s class '%s' -> '%s' (document name regex match)",
                page_result.page_id,
                page_result.classification.doc_type,
                class_name,
            )
        page_result.classification.doc_type = class_name
        page_result.classification.confidence = 1.0
        return page_result

    @staticmethod
    def _apply_page_result(page: Any, page_result: PageClassification) -> None:
        """Copy one page's classification result onto the DECLARED Page fields.

        The whole metadata dict is also stashed on the page by the callers via
        ``setattr``, but that writes an attribute which is not a dataclass field:
        it is absent from ``Document.to_dict``, so it never survives the Step
        Functions hop or reaches DynamoDB (GitHub #565). Everything that has to
        outlive this invocation — class, confidence, reason, boundary — is copied
        here instead.

        Shared by the cache-hit and fresh-inference branches so a cache hit
        cannot silently produce a page with fewer signals than a miss (before
        this existed, the cached branch dropped ``document_boundary`` entirely).
        """
        classification = page_result.classification
        page.classification = classification.doc_type
        page.confidence = classification.confidence
        reason = classification.metadata.get("classification_reason")
        if reason:
            page.classification_reason = str(reason)
        candidates = classification.metadata.get("classification_candidates")
        if candidates:
            page.classification_candidates = candidates
        boundary = classification.metadata.get("document_boundary")
        if boundary:
            page.document_boundary = str(boundary).lower()

    def _classify_pages_multimodal(
        self, document: Document, forced_class: Optional[str] = None
    ) -> Document:
        """
        Classify pages using multimodal page-level classification.

        Args:
            document: Document object to classify
            forced_class: When set, every page result's class is overwritten with
                this value before sections are built. Set by the
                document-name-regex path, where the name determines the class but
                the per-page boundary signal still has to come from the model
                (GitHub issue #705). See ``_pin_page_class``.
        """
        # Page-level classification with document boundary detection
        t0 = time.time()
        context_size = self.config.classification.contextPagesCount
        logger.info(
            f"Classifying document with {len(document.pages)} pages using multimodal page-level classification with {self.backend} backend"
            + (f" (contextPagesCount={context_size})" if context_size > 0 else "")
        )

        try:
            # Check for cached page classifications
            cached_page_classifications = self._get_cached_page_classifications(
                document
            )
            all_page_results = list(cached_page_classifications.values())
            if forced_class:
                # Cached results were produced by an earlier attempt that may not
                # have had the pin applied; pin them too so every result in the
                # list agrees.
                for cached_result in all_page_results:
                    self._pin_page_class(cached_result, forced_class)
            combined_metering = {}
            errors_lock = threading.Lock()  # Thread safety for error collection
            failed_page_exceptions = {}  # Store original exceptions for failed pages

            # Determine which pages need classification
            pages_to_classify = {}
            for page_id, page in document.pages.items():
                if page_id not in cached_page_classifications:
                    pages_to_classify[page_id] = page
                else:
                    # Update document with cached classification
                    cached_result = cached_page_classifications[page_id]
                    self._apply_page_result(document.pages[page_id], cached_result)

                    setattr(
                        document.pages[page_id],
                        "metadata",
                        cached_result.classification.metadata,
                    )

                    # Merge cached metering data
                    page_metering = cached_result.classification.metadata.get(
                        "metering", {}
                    )
                    combined_metering = utils.merge_metering_data(
                        combined_metering, page_metering
                    )

            if pages_to_classify:
                logger.info(
                    f"Found {len(cached_page_classifications)} cached page classifications, classifying {len(pages_to_classify)} remaining pages"
                )

                # Pre-load page content if context is enabled
                page_content_cache: Dict[str, PageContextData] = {}
                sorted_page_ids: List[str] = []
                if context_size > 0:
                    page_content_cache = self._preload_page_content(
                        document, context_size
                    )
                    sorted_page_ids = sorted(
                        document.pages.keys(),
                        key=lambda x: int(x) if x.isdigit() else float("inf"),
                    )

                with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    futures = {}

                    # Start processing only uncached pages
                    for page_id, page in pages_to_classify.items():
                        if context_size > 0:
                            # Get context for this page
                            context = self._get_context_for_page(
                                page_id,
                                sorted_page_ids,
                                page_content_cache,
                                context_size,
                            )
                            future = executor.submit(
                                self.classify_page_bedrock,
                                page_id=page_id,
                                text_uri=page.parsed_text_uri,
                                image_uri=page.image_uri,
                                raw_text_uri=page.raw_text_uri,
                                before_texts=context["before_texts"],
                                after_texts=context["after_texts"],
                                before_images=context["before_images"],
                                after_images=context["after_images"],
                            )
                        else:
                            future = executor.submit(
                                self.classify_page,
                                page_id=page_id,
                                text_uri=page.parsed_text_uri,
                                image_uri=page.image_uri,
                                raw_text_uri=page.raw_text_uri,
                            )
                        futures[future] = page_id

                    # Process results as they complete
                    for future in as_completed(futures):
                        page_id = futures[future]
                        try:
                            page_result = future.result()
                            if forced_class:
                                self._pin_page_class(page_result, forced_class)
                            all_page_results.append(page_result)

                            # Check if there was an error in the classification
                            if "error" in page_result.classification.metadata:
                                with errors_lock:
                                    error_msg = f"Error classifying page {page_id}: {page_result.classification.metadata['error']}"
                                    document.errors.append(error_msg)

                            # Update the page in the document — class, confidence,
                            # reason and boundary onto DECLARED fields, so they
                            # survive the Step Functions hop and reach DynamoDB.
                            self._apply_page_result(
                                document.pages[page_id], page_result
                            )

                            # Copy metadata (including boundary information) to the page
                            setattr(
                                document.pages[page_id],
                                "metadata",
                                page_result.classification.metadata,
                            )

                            # Merge metering data
                            page_metering = page_result.classification.metadata.get(
                                "metering", {}
                            )
                            combined_metering = utils.merge_metering_data(
                                combined_metering, page_metering
                            )
                        except Exception as e:
                            # Capture exception details in the document object instead of raising
                            error_msg = f"Error classifying page {page_id}: {str(e)}"
                            logger.error(error_msg)
                            with errors_lock:
                                document.errors.append(error_msg)
                                # Store the original exception for later use
                                failed_page_exceptions[page_id] = e

                            # Mark page as unclassified on error
                            if page_id in document.pages:
                                document.pages[
                                    page_id
                                ].classification = "error (backoff/retry)"
                                document.pages[page_id].confidence = 0.0

                # Store failed page exceptions in document metadata for caller to access
                if failed_page_exceptions:
                    logger.info(
                        f"Processing {len(failed_page_exceptions)} failed page exceptions for document {document.id}"
                    )

                    # Store the first encountered exception as the primary failure cause
                    first_exception = next(iter(failed_page_exceptions.values()))
                    document.metadata = document.metadata or {}
                    document.metadata["failed_page_exceptions"] = {
                        page_id: {
                            "exception_type": type(exc).__name__,
                            "exception_message": str(exc),
                            "exception_class": exc.__class__.__module__
                            + "."
                            + exc.__class__.__name__,
                        }
                        for page_id, exc in failed_page_exceptions.items()
                    }
                    # Store the primary exception for easy access by caller
                    document.metadata["primary_exception"] = first_exception

                    # Cache successful page classifications (only when some pages fail - for retry scenarios)
                    successful_results = [
                        r
                        for r in all_page_results
                        if "error" not in r.classification.metadata
                    ]
                    if successful_results:
                        logger.info(
                            f"Caching {len(successful_results)} successful page classifications for document {document.id} due to {len(failed_page_exceptions)} failed pages (retry scenario)"
                        )
                        self._cache_successful_page_classifications(
                            document, successful_results
                        )
                    else:
                        logger.warning(
                            f"No successful page classifications to cache for document {document.id} - all {len(failed_page_exceptions)} pages failed"
                        )
                else:
                    # All pages succeeded - no need to cache since there won't be retries
                    logger.info(
                        f"All pages succeeded for document {document.id} - skipping cache (no retry needed)"
                    )
            else:
                logger.info(
                    f"All {len(cached_page_classifications)} page classifications found in cache"
                )

            # Apply configured section splitting strategy
            document = self._apply_section_splitting_strategy(
                document, all_page_results
            )

            # Sections exist now, so a page classification that failed, found no
            # content, or had its class coerced to the fallback can finally be
            # recorded somewhere a user sees it — the section that contains the
            # page. Runs after splitting for exactly that reason.
            self._record_unclassified_page_issues(document, all_page_results)

            # Update document status and metering
            document = self._update_document_status(document)
            document.metering = utils.merge_metering_data(
                document.metering, combined_metering
            )

            t1 = time.time()
            logger.info(
                f"Document classified with {len(document.sections)} sections in {t1 - t0:.2f} seconds"
            )

        except Exception as e:
            error_msg = f"Error classifying all document pages: {str(e)}"
            document = self._update_document_status(
                document, success=False, error_message=error_msg
            )
            # Store the exception in metadata for caller to access
            document.metadata = document.metadata or {}
            document.metadata["primary_exception"] = e
            # raise exception to enable client retries
            raise

        return document

    def _check_page_content_regex(self, text_content: str) -> Optional[str]:
        """
        Check if page content matches any class regex patterns.

        Args:
            text_content: Page text content to check

        Returns:
            Matched class name if found, None otherwise
        """
        # Only apply page content regex for multi-modal page-level classification
        if self.classification_method != self.MULTIMODAL_PAGE_LEVEL:
            return None

        if not text_content:
            return None

        for doc_type in self.document_types:
            if (
                doc_type._compiled_content_regex
                and doc_type._compiled_content_regex.search(text_content)
            ):
                logger.info(
                    f"Page content regex match: Content matched pattern '{doc_type.document_page_content_regex}' for class '{doc_type.type_name}'"
                )
                return doc_type.type_name
        return None

    def _format_classes_list(self) -> str:
        """Format document classes as a simple list for the prompt."""
        return "\n".join(
            [
                f"{doc_type.type_name}  \t[ {doc_type.description} ]"
                for doc_type in self.document_types
            ]
        )

    def _get_attribute_names_for_class(self, class_name: str) -> List[str]:
        """
        Extract a flat list of dotted-path attribute names from a class's
        JSON Schema ``properties``.

        Walks nested ``object`` properties (joining names with ``.``) and
        unwraps ``array`` ``items`` of object type so list-element fields
        are surfaced (without explicit ``[]`` indexing) in the same flat
        listing. Non-object/non-array properties contribute their own name.

        Groups and list-item shapes are commonly declared as a ``$ref`` into
        the class's ``$defs`` (this is what the UI's schema editor emits), so
        every subschema is dereferenced before its ``type`` is read. Without
        that, a ``$ref`` group carries no ``type`` and is emitted as a bare
        leaf, dropping all of its child names — while an otherwise identical
        inline group is walked correctly.

        Args:
            class_name: The ``x-aws-idp-document-type`` of the class to look up.

        Returns:
            Ordered list of unique dotted-path attribute names. Returns an
            empty list if the class is not found or has no ``properties``.
        """
        # Locate the matching schema (case-sensitive on type name)
        target_schema: Optional[Dict[str, Any]] = None
        for schema in self.config.classes:
            if schema.get(X_AWS_IDP_DOCUMENT_TYPE) == class_name:
                target_schema = schema
                break

        if not target_schema:
            return []

        properties = target_schema.get(SCHEMA_PROPERTIES) or {}
        if not isinstance(properties, dict):
            return []

        names: List[str] = []
        seen: Set[str] = set()

        def _walk(
            props: Dict[str, Any],
            parent_path: str = "",
            active_refs: frozenset[str] = frozenset(),
        ) -> None:
            """Emit leaf names under ``props``.

            ``active_refs`` holds the ``$ref`` targets already entered on this
            branch of the descent. Dereferencing makes recursive definitions
            reachable (``$defs/Node`` with a ``child: {"$ref": "#/$defs/Node"}``
            member), which would otherwise recurse forever — such a property is
            emitted as a leaf instead of re-entered.

            It also stops at ``_MAX_WALK_NAMES``. ``active_refs`` is per-BRANCH,
            so a NON-cyclic ``$defs`` DAG is legitimately re-entered on every
            sibling branch and expands combinatorially: a 2 KB schema nesting a
            3-way fan-out 12 deep yields ~265K names. The caller's
            ``MAX_ATTRIBUTES_PER_CLASS`` cap cannot help — it is applied to the
            RESULT, after this walk has already built the whole list.
            """
            if not isinstance(props, dict):
                return
            for prop_name, prop_schema in props.items():
                if len(names) >= self._MAX_WALK_NAMES:
                    return
                if not isinstance(prop_schema, dict):
                    continue
                full_path = f"{parent_path}.{prop_name}" if parent_path else prop_name

                ref = prop_schema.get(REF_FIELD)
                if isinstance(ref, str) and ref in active_refs:
                    if full_path not in seen:
                        seen.add(full_path)
                        names.append(full_path)
                    continue
                branch_refs = (
                    active_refs | {ref} if isinstance(ref, str) else active_refs
                )

                prop_schema = deref_schema(prop_schema, target_schema)
                prop_type = prop_schema.get(SCHEMA_TYPE)

                if prop_type == TYPE_OBJECT:
                    nested = prop_schema.get(SCHEMA_PROPERTIES) or {}
                    if isinstance(nested, dict) and nested:
                        _walk(nested, full_path, branch_refs)
                        continue
                    # Object with no declared properties — emit the
                    # parent name itself so the model still sees it.
                elif prop_type == TYPE_ARRAY:
                    # ``items`` may legally be a LIST (draft-07 tuple form), so
                    # only a dict is safe to read keys off.
                    items_raw = prop_schema.get(SCHEMA_ITEMS)
                    items_raw = items_raw if isinstance(items_raw, dict) else {}
                    items_ref = items_raw.get(REF_FIELD)
                    if not (isinstance(items_ref, str) and items_ref in branch_refs):
                        items_schema = deref_schema(items_raw, target_schema)
                        if items_schema.get(SCHEMA_TYPE) == TYPE_OBJECT:
                            nested = items_schema.get(SCHEMA_PROPERTIES) or {}
                            if isinstance(nested, dict) and nested:
                                item_refs = (
                                    branch_refs | {items_ref}
                                    if isinstance(items_ref, str)
                                    else branch_refs
                                )
                                _walk(nested, full_path, item_refs)
                                continue
                    # Scalar array (or array with no item properties) —
                    # emit the parent name itself.

                if full_path not in seen:
                    seen.add(full_path)
                    names.append(full_path)

        _walk(properties)
        if len(names) >= self._MAX_WALK_NAMES:
            logger.warning(
                "Class '%s' schema expanded to the %d-name walk ceiling; the "
                "attribute listing is truncated. This usually means a $defs "
                "definition is re-entered across many sibling branches — flatten "
                "the schema or reduce nesting depth.",
                class_name,
                self._MAX_WALK_NAMES,
            )
        return names

    def _format_classes_and_attributes_list(self) -> str:
        """
        Format document classes WITH their schema attribute names as an
        XML-tagged listing for inclusion in custom classification prompts
        via the optional ``{CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}``
        placeholder.

        The output is one ``<class>`` block per document type, containing
        ``<description>`` and (when the class has a JSON Schema) an
        ``<attributes>`` element with a comma-separated list of flat
        dotted-path attribute names. Classes with no schema render
        ``<attributes>(no schema)</attributes>`` to make absence obvious
        for debugging.

        Attribute counts per class are soft-capped at
        ``MAX_ATTRIBUTES_PER_CLASS`` to prevent pathologically large
        schemas from bloating the prompt. When truncation occurs, a
        ``...(+N more)`` suffix is appended and a warning is logged.

        Returns:
            Multi-line XML-tagged string suitable for direct prompt
            substitution.
        """
        blocks: List[str] = []
        for doc_type in self.document_types:
            attr_names = self._get_attribute_names_for_class(doc_type.type_name)
            if attr_names:
                if len(attr_names) > self.MAX_ATTRIBUTES_PER_CLASS:
                    overflow = len(attr_names) - self.MAX_ATTRIBUTES_PER_CLASS
                    logger.warning(
                        "Class '%s' has %d schema attributes; truncating to "
                        "%d for {CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS} "
                        "rendering (overflow=%d).",
                        doc_type.type_name,
                        len(attr_names),
                        self.MAX_ATTRIBUTES_PER_CLASS,
                        overflow,
                    )
                    rendered_names = attr_names[: self.MAX_ATTRIBUTES_PER_CLASS]
                    attrs_text = ", ".join(rendered_names) + f", ...(+{overflow} more)"
                else:
                    attrs_text = ", ".join(attr_names)
            else:
                attrs_text = "(no schema)"

            blocks.append(
                f'<class name="{doc_type.type_name}">\n'
                f"  <description>{doc_type.description}</description>\n"
                f"  <attributes>{attrs_text}</attributes>\n"
                f"</class>"
            )

        return "\n".join(blocks)

    def _format_classes_and_attributes_table(self) -> str:
        """
        Format document classes WITH their schema attribute names as a
        markdown table (``type | description | attributes``) for use by
        the holistic classification path when the optional
        ``{CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}`` placeholder is
        present in a custom prompt.

        Same soft cap and truncation behavior as
        :meth:`_format_classes_and_attributes_list`.

        Returns:
            Markdown table string.
        """
        header = "| type | description | attributes |\n| --- | --- | --- |\n"
        rows: List[str] = []
        for doc_type in self.document_types:
            attr_names = self._get_attribute_names_for_class(doc_type.type_name)
            if attr_names:
                if len(attr_names) > self.MAX_ATTRIBUTES_PER_CLASS:
                    overflow = len(attr_names) - self.MAX_ATTRIBUTES_PER_CLASS
                    logger.warning(
                        "Class '%s' has %d schema attributes; truncating to "
                        "%d for {CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS} "
                        "rendering (overflow=%d).",
                        doc_type.type_name,
                        len(attr_names),
                        self.MAX_ATTRIBUTES_PER_CLASS,
                        overflow,
                    )
                    rendered_names = attr_names[: self.MAX_ATTRIBUTES_PER_CLASS]
                    attrs_text = ", ".join(rendered_names) + f", ...(+{overflow} more)"
                else:
                    attrs_text = ", ".join(attr_names)
            else:
                attrs_text = "(no schema)"

            # Escape pipes inside cell values to avoid breaking the table.
            description = (doc_type.description or "").replace("|", "\\|")
            attrs_text = attrs_text.replace("|", "\\|")
            rows.append(f"| {doc_type.type_name} | {description} | {attrs_text} |")

        return header + "\n".join(rows)

    def _get_classification_config(
        self, stage: str = STAGE_SINGLE_PASS
    ) -> Dict[str, Any]:
        """
        Get and validate the classification configuration.

        Args:
            stage: Which call's configuration to resolve —
                :data:`STAGE_SINGLE_PASS` (the default: one call returns both
                boundaries and classes), :data:`STAGE_SEGMENTATION` (two-stage
                Stage A) or :data:`STAGE_LABELING` (two-stage Stage B). The
                stage decides which prompts are used, which model, and the
                **metering context** the call is billed under, so that a
                two-stage deployment's cost table separates the two halves
                instead of adding them together under one line.

        Returns:
            Dict with validated classification configuration parameters

        Raises:
            ValueError: If required configuration values are missing
        """
        # Type-safe access to classification config (no .get() needed!)
        config = {
            "model_id": self._stage_model(stage),
            "temperature": self.config.classification.temperature,
            "top_k": self.config.classification.top_k,
            "top_p": self.config.classification.top_p,
            "max_tokens": self.config.classification.max_tokens,
            "reasoning_effort": self.config.classification.reasoning_effort,
            "metering_context": self.STAGE_METERING_CONTEXT[stage],
            "stage": stage,
        }

        # Validate system prompt
        system_prompt = self._raw_system_prompt(stage)
        if not system_prompt:
            raise ValueError(
                f"No system_prompt found for the {stage} stage of classification"
            )
        config["system_prompt"] = system_prompt

        # Validate task prompt
        task_prompt = self._raw_task_prompt(stage)
        if not task_prompt:
            raise ValueError(
                f"No task_prompt found for the {stage} stage of classification"
            )
        # Modality normalisation runs BEFORE the class-confidence splice: the
        # splice positions its block relative to the document/cache-point marker,
        # and rewriting placeholders afterwards could move content across it.
        task_prompt = self._apply_modality_to_prompt(task_prompt)
        config["task_prompt"] = self._compose_class_confidence_prompt(
            task_prompt, stage
        )

        return config

    def _stage_model(self, stage: str) -> str:
        """The Bedrock model for one stage, falling back to the shared one.

        Naming a model per stage is most of the point of two-stage: boundary
        detection may want a capable multimodal model looking at a whole packet,
        while labeling a clean segment is often a much easier problem that a
        cheaper model answers just as well.
        """
        if stage == self.STAGE_SEGMENTATION:
            return self.config.classification.segmentation.model or self.bedrock_model
        if stage == self.STAGE_LABELING:
            return self.config.classification.labeling.model or self.bedrock_model
        return self.bedrock_model

    def _compose_class_confidence_prompt(
        self, task_prompt: str, stage: str = STAGE_SINGLE_PASS
    ) -> str:
        """Splice the class-confidence instruction block in, when enabled.

        No-op in the default ``off`` mode, so the shipped prompt stays exactly as
        it was. A custom prompt that already carries a ``<class-confidence>``
        block is left alone (the splice is idempotent), and the response parser
        honours a ``confidence``/``candidates`` key regardless of this setting —
        the mode composes the *instruction*, it does not gate the *parsing*.
        """
        confidence_cfg = self.config.classification.confidence
        mode = confidence_cfg.mode
        if mode == "off":
            return task_prompt

        if stage == self.STAGE_SEGMENTATION:
            # Stage A reports no class, so there is no class to be confident
            # about. Scoring belongs to Stage B, which is where the class is
            # decided — and spending output tokens here would buy a number
            # describing nothing.
            return task_prompt

        # Stage B is always one call about one segment, so it takes the PAGE
        # block shape (one class, one score) regardless of how the boundaries
        # were found. The packet block describes a `segments` array Stage B
        # does not return.
        packet = (
            self.granularity == self.GRANULARITY_PACKET
            and stage != self.STAGE_LABELING
        )

        if packet and not self.granularity_is_explicit:
            # A LEGACY holistic configuration (classificationMethod with no
            # granularity) runs a hand-written packet prompt — the shipped
            # default has always been the page prompt — so there is nothing here
            # whose output shape we can describe. Its per-segment `confidence`
            # IS parsed, so such a deployment can still be scored by asking for
            # it in its own prompt.
            logger.warning(
                "classification.confidence.mode=%s is not composed for a legacy "
                "%s configuration, whose task_prompt is hand-written; request "
                "confidence in that prompt instead (a per-segment 'confidence' "
                "key is parsed if present). Set classification.granularity: "
                "packet to use the composed per-segment block.",
                mode,
                self.TEXTBASED_HOLISTIC,
            )
            return task_prompt

        if mode == "topk":
            top_k = resolve_top_k(
                confidence_cfg.top_k_candidates, len(self.valid_doc_types)
            )
            # The packet variant names the key it goes in and scopes the
            # instruction per segment; the page one speaks about "this page".
            # Falling back to the page block keeps a custom configuration
            # written before packet granularity working rather than silently
            # dropping the request.
            block = (
                confidence_cfg.task_prompt_topk_packet or confidence_cfg.task_prompt_topk
                if packet
                else confidence_cfg.task_prompt_topk
            )
        else:  # verbalized
            top_k = None
            block = (
                confidence_cfg.task_prompt_verbalized_packet
                or confidence_cfg.task_prompt_verbalized
                if packet
                else confidence_cfg.task_prompt_verbalized
            )

        if not block:
            logger.warning(
                "classification.confidence.mode=%s but its prompt block is empty; "
                "no confidence will be requested. Restore it from the system "
                "defaults (base-classification.yaml).",
                mode,
            )
            return task_prompt

        composed = append_class_confidence_block(task_prompt, block, top_k)
        if composed is not task_prompt:
            logger.info(
                "Requesting class confidence in %s mode%s",
                mode,
                f" (top {top_k} candidates)" if top_k else "",
            )
        return composed

    def _raw_task_prompt(self, stage: str = STAGE_SINGLE_PASS) -> str:
        """The configured task prompt for a stage and granularity, un-normalised.

        "Un-normalised" means before the modality rewrite and before the
        class-confidence splice — this is the template whose placeholders the
        modality *inference* reads, so it has to be the operator's own text.

        ⚠️ **The packet prompt is only preferred when granularity was set
        explicitly.** A configuration carrying
        ``classificationMethod: textbasedHolisticClassification`` and no
        ``granularity`` is a pre-existing holistic deployment, and the only way
        that ever worked was a hand-written packet-shaped ``task_prompt`` — the
        shipped default has always been the page prompt, and no preset set that
        method. Preferring the new packet default there would silently replace
        the operator's own prompt with ours. So: legacy holistic keeps the flat
        prompt, and ``granularity: packet`` uses ``packet.task_prompt``, which is
        also the field the configuration panel shows for that granularity.
        """
        return self._stage_prompt(stage, "task_prompt")

    def _raw_system_prompt(self, stage: str = STAGE_SINGLE_PASS) -> str:
        """The configured system prompt for a stage and granularity.

        Same resolution rules as :meth:`_raw_task_prompt`.
        """
        return self._stage_prompt(stage, "system_prompt")

    def _stage_prompt(self, stage: str, field: str) -> str:
        """Resolve one prompt field for a stage and the active granularity.

        Stage B (labeling) is **not** granularity-dependent: it is always one
        call about one segment's pages, whichever way the boundaries were found.
        Stage A and single-pass both are, because both are asked either about
        one page or about a whole packet.
        """
        if stage == self.STAGE_LABELING:
            labeling = self.config.classification.labeling
            value = getattr(labeling, field)
            if value:
                return value
            logger.warning(
                "classification.labeling.%s is empty, so Stage B has no prompt "
                "of its own and is falling back to the single-pass "
                "classification.%s — which asks for a document_boundary this "
                "stage does not need. Restore it from the system defaults "
                "(base-classification.yaml).",
                field,
                field,
            )
            return getattr(self.config.classification, field)

        if stage == self.STAGE_SEGMENTATION:
            segmentation = self.config.classification.segmentation
            source = (
                segmentation.packet
                if self.granularity == self.GRANULARITY_PACKET
                else segmentation
            )
            value = getattr(source, field)
            if value:
                return value
            logger.warning(
                "The %s granularity Stage A %s is empty, so segmentation is "
                "falling back to the single-pass prompt — which carries the "
                "whole class vocabulary, the saving two-stage exists for. "
                "Restore it from the system defaults "
                "(base-classification.yaml).",
                self.granularity,
                field,
            )
            # Fall through to the single-pass resolution below.

        flat = getattr(self.config.classification, field)
        if (
            self.granularity == self.GRANULARITY_PACKET
            and self.granularity_is_explicit
        ):
            packet_prompt = getattr(self.config.classification.packet, field)
            if packet_prompt:
                return packet_prompt
            logger.warning(
                "classification.granularity is 'packet' but "
                "classification.packet.%s is empty; falling back to "
                "classification.%s, which is written for PAGE granularity and "
                "asks for a different output shape. Restore the packet prompt "
                "from the system defaults (base-classification.yaml).",
                field,
                field,
            )
        return flat

    def _resolve_modality(self, task_prompt: Optional[str] = None) -> tuple[bool, bool]:
        """Resolve ``(send_text, send_images)`` for a classification call.

        An unset flag is inferred from the prompt's placeholders, which is
        exactly the behaviour that existed before the setting: a prompt holding
        ``{DOCUMENT_TEXT}`` was sent text, one holding ``{DOCUMENT_IMAGE}`` was
        sent the image, and deleting a placeholder was the documented way to get
        a single modality. Inference is therefore not a compatibility shim
        bolted on beside the real behaviour — it *is* the real behaviour, and an
        explicit flag overrides it.

        Args:
            task_prompt: Template to infer from. Defaults to the configured
                prompt for the active granularity.
        """
        modality = self.config.classification.modality
        template = task_prompt if task_prompt is not None else self._raw_task_prompt()

        send_text = (
            modality.text
            if modality.text is not None
            else ("{DOCUMENT_TEXT}" in template)
        )
        send_images = (
            modality.images
            if modality.images is not None
            else ("{DOCUMENT_IMAGE}" in template)
        )
        return send_text, send_images

    def _apply_modality_to_prompt(self, task_prompt: str) -> str:
        """Rewrite a prompt template so its placeholders match the modality.

        Only an **explicitly set** flag causes a rewrite; an inferred one agrees
        with the template by construction, so the overwhelmingly common path
        returns the operator's prompt untouched.

        Removal takes the placeholder's wrapping element with it when the
        element contains nothing else — ``<document-image>{DOCUMENT_IMAGE}
        </document-image>`` becomes nothing rather than an empty tag pair,
        because an empty element announcing a modality that is switched off is a
        misleading thing to put in a prompt. A placeholder that is not wrapped
        that way is simply deleted.

        Addition appends a wrapped placeholder at the end of the template. The
        position is a guess — the operator's prompt said nothing about where the
        content goes — so it is both the least surprising choice (the content is
        the last thing the model reads) and logged, since a prompt the operator
        did not write is something they need to be able to see.
        """
        modality = self.config.classification.modality
        result = task_prompt

        for flag, placeholder, element in (
            (modality.text, "{DOCUMENT_TEXT}", "document-ocr-data"),
            (modality.images, "{DOCUMENT_IMAGE}", "document-image"),
        ):
            if flag is None:
                continue
            present = placeholder in result
            if flag and not present:
                result = f"{result.rstrip()}\n\n<{element}>\n{placeholder}\n</{element}>\n"
                logger.info(
                    "classification.modality turned %s on but the task prompt had "
                    "no %s placeholder; appended one.",
                    element,
                    placeholder,
                )
            elif not flag and present:
                result = self._strip_placeholder(result, placeholder, element)
                logger.info(
                    "classification.modality turned %s off; removed %s from the "
                    "task prompt so the content is neither sent nor paid for.",
                    element,
                    placeholder,
                )

        return result

    @staticmethod
    def _strip_placeholder(template: str, placeholder: str, element: str) -> str:
        """Remove ``placeholder``, and its wrapping ``<element>`` if that is all it held."""
        import re

        wrapped = re.compile(
            rf"[ \t]*<{re.escape(element)}>\s*{re.escape(placeholder)}\s*"
            rf"</{re.escape(element)}>[ \t]*\n?",
            re.IGNORECASE,
        )
        stripped, count = wrapped.subn("", template)
        if count:
            return stripped
        return template.replace(placeholder, "")

    def _prepare_prompt_from_template(
        self,
        prompt_template: str,
        substitutions: dict[str, str],
        required_placeholders: list[str] | None = None,
    ) -> str:
        """
        Prepare prompt from template by replacing placeholders with values.

        Args:
            prompt_template: The prompt template with placeholders
            substitutions: Dictionary of placeholder values
            required_placeholders: List of placeholder names that must be present in the template

        Returns:
            String with placeholders replaced by values

        Raises:
            ValueError: If a required placeholder is missing from the template
        """
        from idp_common.bedrock import format_prompt

        return format_prompt(prompt_template, substitutions, required_placeholders)

    def _build_classification_substitutions(
        self,
        document_text: str,
        class_names_and_descriptions: str,
    ) -> Dict[str, str]:
        """
        Build the standard placeholder substitution dict for classification
        prompts.

        Always includes ``DOCUMENT_TEXT`` and ``CLASS_NAMES_AND_DESCRIPTIONS``.
        Lazily includes the optional
        ``CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS`` substitution — keys
        not referenced by the template incur no extra cost (the underlying
        :func:`format_prompt` only substitutes referenced placeholders).
        Computing the attribute listing is cheap (in-memory schema walk),
        but we still skip it when the active prompt template does not
        reference it, to keep logs/metrics clean.

        Args:
            document_text: Resolved document/page text to substitute.
            class_names_and_descriptions: The pre-formatted class listing
                (either flat list or markdown table, depending on caller).

        Returns:
            Dictionary of placeholder name -> value.
        """
        return {
            "DOCUMENT_TEXT": document_text,
            "CLASS_NAMES_AND_DESCRIPTIONS": class_names_and_descriptions,
            # Two-stage Stage A only. Renders empty under the default
            # `boundary_hints: none`, which is the point: a boundary call that
            # carries no class vocabulary is the prefix saving two-stage exists
            # for. Included unconditionally because format_prompt substitutes
            # only placeholders the template actually references.
            "BOUNDARY_HINTS": self._format_boundary_hints(),
            # Opt-in placeholder: included unconditionally in the
            # substitutions dict because format_prompt only substitutes
            # placeholders that actually appear in the template, so this
            # is cost-neutral for users who don't reference it.
            "CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS": (
                self._format_classes_and_attributes_list()
            ),
        }

    def _build_content_with_or_without_image_placeholder(
        self,
        prompt_template: str,
        document_text: str,
        class_names_and_descriptions: str,
        image_content: Optional[bytes] = None,
    ) -> List[Dict[str, Any]]:
        """
        Build content array, automatically deciding whether to use image placeholder processing.

        If the prompt contains {DOCUMENT_IMAGE}, the image will be inserted at that location.
        If the prompt does NOT contain {DOCUMENT_IMAGE}, the image will NOT be included at all.

        Args:
            prompt_template: The prompt template that may contain {DOCUMENT_IMAGE}
            document_text: The document text content
            class_names_and_descriptions: Formatted class names and descriptions
            image_content: Optional image content to insert (only used when {DOCUMENT_IMAGE} is present)

        Returns:
            List of content items with text and image content properly ordered based on presence of placeholder
        """
        if "{DOCUMENT_IMAGE}" in prompt_template:
            return self._build_content_with_image_placeholder(
                prompt_template,
                document_text,
                class_names_and_descriptions,
                image_content,
            )
        else:
            return self._build_content_without_image_placeholder(
                prompt_template,
                document_text,
                class_names_and_descriptions,
                image_content,
            )

    def _build_content_with_image_placeholder(
        self,
        prompt_template: str,
        document_text: str,
        class_names_and_descriptions: str,
        image_content: Optional[bytes] = None,
    ) -> List[Dict[str, Any]]:
        """
        Build content array with image inserted at DOCUMENT_IMAGE placeholder if present.

        Args:
            prompt_template: The prompt template that may contain {DOCUMENT_IMAGE}
            document_text: The document text content
            class_names_and_descriptions: Formatted class names and descriptions
            image_content: Optional image content to insert

        Returns:
            List of content items with text and image content properly ordered
        """
        # Check if DOCUMENT_IMAGE placeholder is present
        if "{DOCUMENT_IMAGE}" in prompt_template:
            # Split the prompt at the DOCUMENT_IMAGE placeholder
            parts = prompt_template.split("{DOCUMENT_IMAGE}")

            if len(parts) != 2:
                logger.warning(
                    "Invalid DOCUMENT_IMAGE placeholder usage, falling back to standard processing"
                )
                # Fallback to standard processing
                return self._build_content_without_image_placeholder(
                    prompt_template,
                    document_text,
                    class_names_and_descriptions,
                    image_content,
                )

            substitutions = self._build_classification_substitutions(
                document_text, class_names_and_descriptions
            )

            # Process the parts before and after the image placeholder
            before_image = self._prepare_prompt_from_template(
                parts[0],
                substitutions,
                required_placeholders=[],
            )

            after_image = self._prepare_prompt_from_template(
                parts[1],
                substitutions,
                required_placeholders=[],
            )

            # Build content array with image in the middle
            content = []

            # Add the part before the image
            if before_image.strip():
                content.append({"text": before_image})

            # Add the image if available
            if image_content:
                content.append(image.prepare_bedrock_image_attachment(image_content))

            # Add the part after the image
            if after_image.strip():
                content.append({"text": after_image})

            return content
        else:
            # No DOCUMENT_IMAGE placeholder, use standard processing
            return self._build_content_without_image_placeholder(
                prompt_template,
                document_text,
                class_names_and_descriptions,
                image_content,
            )

    def _build_content_without_image_placeholder(
        self,
        prompt_template: str,
        document_text: str,
        class_names_and_descriptions: str,
        image_content: Optional[bytes] = None,
    ) -> List[Dict[str, Any]]:
        """
        Build content array without DOCUMENT_IMAGE placeholder (standard processing).

        Note: This method does NOT attach the image content when no placeholder is present.

        Args:
            prompt_template: The prompt template
            document_text: The document text content
            class_names_and_descriptions: Formatted class names and descriptions
            image_content: Optional image content (not used when no placeholder is present)

        Returns:
            List of content items with text content only (no image)
        """
        # Prepare the full prompt
        task_prompt = self._prepare_prompt_from_template(
            prompt_template,
            self._build_classification_substitutions(
                document_text, class_names_and_descriptions
            ),
            required_placeholders=[],
        )

        content = [{"text": task_prompt}]

        # No longer adding image content when no placeholder is present

        return content

    def _build_text_with_context(
        self,
        current_text: str,
        before_texts: List[str],
        after_texts: List[str],
    ) -> str:
        """
        Build text content with context pages for classification.

        Args:
            current_text: The text of the page being classified
            before_texts: List of text from pages before the current page
            after_texts: List of text from pages after the current page

        Returns:
            Formatted text with context pages wrapped in descriptive tags
        """
        parts = []

        if before_texts:
            context_text = "\n\n".join(before_texts)
            parts.append(
                f"For context, here is the OCR text for the page(s) immediately prior to the page you should classify:\n"
                f"<context-pages-before>\n{context_text}\n</context-pages-before>"
            )

        parts.append(
            f"Here is the OCR text for the page to classify:\n"
            f"<current-page>\n{current_text}\n</current-page>"
        )

        if after_texts:
            context_text = "\n\n".join(after_texts)
            parts.append(
                f"For context, here is the OCR text for the page(s) immediately after the page you should classify:\n"
                f"<context-pages-after>\n{context_text}\n</context-pages-after>"
            )

        return "\n\n".join(parts)

    def _build_images_with_context(
        self,
        current_image: Optional[bytes],
        before_images: List[bytes],
        after_images: List[bytes],
    ) -> List[Dict[str, Any]]:
        """
        Build image content array with context pages for classification.

        Args:
            current_image: The image of the page being classified
            before_images: List of images from pages before the current page
            after_images: List of images from pages after the current page

        Returns:
            List of content items with labeled images
        """
        content = []

        if before_images:
            content.append(
                {
                    "text": "For context, here are the image(s) for the page(s) immediately prior to the page you should classify:"
                }
            )
            for img in before_images:
                content.append(image.prepare_bedrock_image_attachment(img))

        if current_image:
            content.append({"text": "Here is the image for the page to classify:"})
            content.append(image.prepare_bedrock_image_attachment(current_image))

        if after_images:
            content.append(
                {
                    "text": "For context, here are the image(s) for the page(s) immediately after the page you should classify:"
                }
            )
            for img in after_images:
                content.append(image.prepare_bedrock_image_attachment(img))

        return content

    def _build_content(
        self,
        task_prompt_template: str,
        document_text: str,
        class_names_and_descriptions: str,
        image_content: Optional[bytes] = None,
    ) -> List[Dict[str, Any]]:
        """
        Build content array with support for optional FEW_SHOT_EXAMPLES and DOCUMENT_IMAGE placeholders.

        The four context-page arguments this used to declare
        (``before_texts``/``after_texts``/``before_images``/``after_images``)
        were never referenced by the body — the context path has always been a
        separate builder, now :meth:`_build_context_content` — so accepting them
        advertised a capability this method did not have.

        Args:
            task_prompt_template: The task prompt template that may contain placeholders
            document_text: The document text content
            class_names_and_descriptions: Formatted class names and descriptions
            image_content: Optional image content to insert

        Returns:
            List of content items with text and image content properly ordered
        """
        # Split the task prompt at the FEW_SHOT_EXAMPLES placeholder
        parts = task_prompt_template.split("{FEW_SHOT_EXAMPLES}")

        if len(parts) != 2:
            # Fallback to regular prompt processing if placeholder not found or malformed
            return self._build_content_with_or_without_image_placeholder(
                task_prompt_template,
                document_text,
                class_names_and_descriptions,
                image_content,
            )

        # Process both parts
        before_examples_content = self._build_content_with_or_without_image_placeholder(
            parts[0], document_text, class_names_and_descriptions, image_content
        )
        after_examples_content = self._build_content_with_or_without_image_placeholder(
            parts[1], document_text, class_names_and_descriptions, image_content
        )

        # Build content array
        content = []

        # Add the part before examples
        content.extend(before_examples_content)

        # Add few-shot examples from config
        examples_content = build_few_shot_examples_content(self.config)
        content.extend(examples_content)

        # Add the part after examples
        content.extend(after_examples_content)

        # No longer appending image content when no placeholder is found

        return content

    def _build_context_content(
        self,
        task_prompt_template: str,
        document_text: str,
        image_content: Optional[bytes],
        before_images: List[bytes],
        after_images: List[bytes],
    ) -> List[Dict[str, Any]]:
        """Build the request content for the ``contextPagesCount > 0`` path.

        The context path used to be a separate, simpler content builder, and it
        diverged from :meth:`_build_content` in two ways that were both silent:

        - **``{FEW_SHOT_EXAMPLES}`` was never substituted.** Nothing split the
          template on it and ``_build_classification_substitutions`` has no such
          key, so the placeholder reached the model as the literal eight-word
          string and every configured ``x-aws-idp-examples`` was dropped.
          Examples defined but not sent is exactly the failure the placeholder
          was added to the default prompt to prevent.
        - **Images were attached whether or not the prompt asked for them.** The
          no-placeholder branch appended the page images anyway, so a
          deliberately text-only prompt (the documented way to get text-only
          classification before ``modality.images`` existed) silently paid for
          and sent every context page's image.

        This builder has the same two-level structure as :meth:`_build_content`
        — split on ``{FEW_SHOT_EXAMPLES}`` first, then handle
        ``{DOCUMENT_IMAGE}`` within each part — so the two paths now agree on
        both questions. The only thing it does differently is attach the
        *labelled* context images via :meth:`_build_images_with_context` rather
        than the single current-page image.
        """
        parts = task_prompt_template.split("{FEW_SHOT_EXAMPLES}")

        if len(parts) != 2:
            # No placeholder (or a malformed repeat) — render as one segment,
            # matching _build_content's fallback.
            return self._build_context_content_segment(
                task_prompt_template,
                document_text,
                image_content,
                before_images,
                after_images,
            )

        content: List[Dict[str, Any]] = []
        content.extend(
            self._build_context_content_segment(
                parts[0], document_text, image_content, before_images, after_images
            )
        )
        content.extend(build_few_shot_examples_content(self.config))
        content.extend(
            self._build_context_content_segment(
                parts[1], document_text, image_content, before_images, after_images
            )
        )
        return content

    def _build_context_content_segment(
        self,
        prompt_segment: str,
        document_text: str,
        image_content: Optional[bytes],
        before_images: List[bytes],
        after_images: List[bytes],
    ) -> List[Dict[str, Any]]:
        """Render one segment of a context-path prompt, inserting context images.

        A segment with no ``{DOCUMENT_IMAGE}`` placeholder gets **no images**.
        That is the fix: the caller may hold images for the current page and its
        neighbours, and a prompt that does not ask for them must not receive
        them.

        Blank segments produce no content block at all — Bedrock rejects an empty
        text block, and a segment is legitimately blank when
        ``{FEW_SHOT_EXAMPLES}`` sits at one end of the template.
        """
        substitutions = self._build_classification_substitutions(
            document_text, self._format_classes_list()
        )

        if "{DOCUMENT_IMAGE}" not in prompt_segment:
            rendered = self._prepare_prompt_from_template(
                prompt_segment, substitutions, required_placeholders=[]
            )
            return [{"text": rendered}] if rendered.strip() else []

        image_parts = prompt_segment.split("{DOCUMENT_IMAGE}")
        if len(image_parts) != 2:
            logger.warning(
                "Invalid DOCUMENT_IMAGE placeholder usage in the context-pages "
                "prompt (%d occurrences); appending images after the text instead "
                "of at the placeholder.",
                len(image_parts) - 1,
            )
            rendered = self._prepare_prompt_from_template(
                prompt_segment.replace("{DOCUMENT_IMAGE}", ""),
                substitutions,
                required_placeholders=[],
            )
            content: List[Dict[str, Any]] = (
                [{"text": rendered}] if rendered.strip() else []
            )
            content.extend(
                self._build_images_with_context(
                    current_image=image_content,
                    before_images=before_images,
                    after_images=after_images,
                )
            )
            return content

        before_image = self._prepare_prompt_from_template(
            image_parts[0], substitutions, required_placeholders=[]
        )
        after_image = self._prepare_prompt_from_template(
            image_parts[1], substitutions, required_placeholders=[]
        )

        content = []
        if before_image.strip():
            content.append({"text": before_image})
        content.extend(
            self._build_images_with_context(
                current_image=image_content,
                before_images=before_images,
                after_images=after_images,
            )
        )
        if after_image.strip():
            content.append({"text": after_image})
        return content

    def classify_page_bedrock(
        self,
        page_id: str,
        text_uri: Optional[str] = None,
        image_uri: Optional[str] = None,
        raw_text_uri: Optional[str] = None,
        before_texts: Optional[List[str]] = None,
        after_texts: Optional[List[str]] = None,
        before_images: Optional[List[bytes]] = None,
        after_images: Optional[List[bytes]] = None,
    ) -> PageClassification:
        """
        Classify a single page using Bedrock LLMs.

        Args:
            page_id: ID of the page
            text_uri: URI of the text content
            image_uri: URI of the image content
            raw_text_uri: URI of the raw text content
            before_texts: Optional list of text content from preceding pages (for context)
            after_texts: Optional list of text content from following pages (for context)
            before_images: Optional list of image content from preceding pages (for context)
            after_images: Optional list of image content from following pages (for context)

        Returns:
            PageClassification: Classification result for the page
        """
        # Initialize content variables
        text_content = None
        image_content = None

        # Load text content from URI.
        #
        # Deliberately NOT gated on the text modality, unlike the image load
        # below. The page-content regex shortcut
        # (``x-aws-idp-page-content-regex``) runs against this text and can
        # decide the page's class with no inference at all, so an images-only
        # configuration still needs it read. It is also the cheap half: one S3
        # GET against an image's GET plus a Pillow resize. The modality decides
        # what is *sent*, which the prompt rewrite handles.
        if text_uri:
            try:
                text_content = s3.get_text_content(text_uri)
            except Exception as e:
                logger.warning(f"Failed to load text content from {text_uri}: {e}")
                # Continue without text content

        # Load image content from URI with configurable dimensions.
        # Skipped entirely when the modality says images are not being sent —
        # loading one costs an S3 GET and a Pillow resize per page, and the
        # content builders would then discard it.
        _, send_images = self._resolve_modality()
        if image_uri and send_images:
            try:
                # Type-safe access to image config
                target_width = self.config.classification.image.target_width
                target_height = self.config.classification.image.target_height

                # Just pass the values directly - prepare_image handles empty strings/None
                image_content = image.prepare_image(
                    image_uri, target_width, target_height
                )
            except Exception as e:
                logger.warning(f"Failed to load image content from {image_uri}: {e}")
                # Continue without image content

        # Check for page content regex match (multi-modal page-level classification only)
        if text_content:
            regex_matched_class = self._check_page_content_regex(text_content)
            if regex_matched_class:
                logger.info(
                    f"Page {page_id} classified as '{regex_matched_class}' based on content regex match. Skipping LLM classification."
                )

                # Create and return classification result with regex match
                return PageClassification(
                    page_id=page_id,
                    classification=DocumentClassification(
                        doc_type=regex_matched_class,
                        confidence=1.0,  # High confidence for regex matches
                        metadata={
                            "regex_matched": True,
                            "document_boundary": "continue",  # Default boundary
                        },
                    ),
                    image_uri=image_uri,
                    text_uri=text_uri,
                    raw_text_uri=raw_text_uri,
                )

        # Verify we have at least some content to classify
        if not text_content and not image_content:
            logger.warning(f"No content available for page {page_id}")
            # Return unclassified result
            return self._create_unclassified_result(
                page_id=page_id,
                image_uri=image_uri,
                text_uri=text_uri,
                raw_text_uri=raw_text_uri,
                error_message="No content available for classification",
                reason=UNCLASSIFIED_NO_CONTENT,
            )

        # Get classification configuration
        config = self._get_classification_config()

        # Check if context is being used
        has_context = bool(before_texts or after_texts or before_images or after_images)

        if has_context:
            # Build text content with context
            document_text = self._build_text_with_context(
                current_text=text_content or "",
                before_texts=before_texts or [],
                after_texts=after_texts or [],
            )

            content = self._build_context_content(
                task_prompt_template=config["task_prompt"],
                document_text=document_text,
                image_content=image_content,
                before_images=before_images or [],
                after_images=after_images or [],
            )

            logger.info(
                f"Classifying page {page_id} with context: {len(before_texts or [])} pages before, {len(after_texts or [])} pages after"
            )
        else:
            # Build content without context (original behavior)
            content = self._build_content(
                config["task_prompt"],
                text_content or "",
                self._format_classes_list(),
                image_content,
            )

        logger.info(f"Classifying page {page_id} with Bedrock")

        t0 = time.time()

        # Invoke Bedrock model
        try:
            # Validation/retry loop: re-prompt the model when it returns a class
            # that is not in the configured vocabulary. When enforcement is
            # disabled, the loop runs exactly once and preserves legacy
            # "warn and use anyway" behavior.
            enforce = self.config.classification.enforceValidClasses
            max_retries = (
                self.config.classification.maxValidationRetries if enforce else 0
            )
            attempt_content = content
            metering: Dict[str, Any] = {}
            doc_type = ""
            document_boundary = "continue"
            # Both OPTIONAL outputs, reset per attempt below so a retry's values
            # never leak from the rejected attempt: the confidence the model
            # reported for a class we threw away does not apply to the new one.
            confidence: Optional[float] = None
            classification_reason: Optional[str] = None
            candidates: List[Dict[str, Any]] = []
            validation_error: Optional[str] = None

            for attempt in range(max_retries + 1):
                response_with_metering = self._invoke_bedrock_model(
                    content=attempt_content, config=config
                )

                response = response_with_metering["response"]
                # Accumulate metering across all attempts so token usage from
                # retries is not lost. Assign the first attempt's metering
                # directly (preserving its exact shape) and merge subsequent
                # attempts.
                attempt_metering = response_with_metering.get("metering", {})
                if not metering:
                    metering = attempt_metering
                else:
                    metering = utils.merge_metering_data(metering, attempt_metering)

                # Extract classification result
                # Defensive: Handle case where LLM returns empty content array
                content_array = response["output"]["message"].get("content", [])
                if not content_array or len(content_array) == 0:
                    logger.error(
                        "LLM returned empty content array in classification response",
                        extra={"page_id": page_id, "response": response},
                    )
                    raise ValueError(
                        f"Classification failed for page {page_id}: LLM returned empty response"
                    )

                # Reasoning models (Claude Sonnet 5 / 4.6+, extended thinking on)
                # emit reasoningContent block(s) before the answer text block, so
                # content[0] may not be the text. Concatenate all text blocks.
                classification_text = "".join(
                    item["text"]
                    for item in content_array
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                )

                # Try to extract structured data (JSON or YAML) from the response
                confidence = None
                classification_reason = None
                candidates = []
                try:
                    classification_data, detected_format = (
                        extract_structured_data_from_text(classification_text)
                    )
                    if isinstance(classification_data, dict):
                        doc_type = classification_data.get("class", "")
                        document_boundary = classification_data.get(
                            "document_boundary", "continue"
                        )
                        # All optional. `confidence` has been a documented part
                        # of this response shape for a long time but was
                        # discarded (GitHub #673); `classification_reason` is
                        # asked for by the DEFAULT prompt — those output tokens
                        # were bought on every page and dropped; `candidates` is
                        # what classification.confidence.mode=topk requests.
                        # Parsing is NOT gated on the mode: a custom prompt that
                        # asks for any of these is honoured, and a prompt that
                        # asks for none simply yields None/[].
                        confidence, candidates = resolve_class_and_confidence(
                            reported_class=doc_type,
                            reported_confidence=classification_data.get("confidence"),
                            reported_candidates=classification_data.get("candidates"),
                            valid_classes=self.valid_doc_types,
                        )
                        reason = classification_data.get("classification_reason")
                        if isinstance(reason, str) and reason.strip():
                            classification_reason = reason.strip()
                        logger.info(
                            f"Parsed classification response as {detected_format}: {classification_data}"
                        )
                    else:
                        # If parsing failed, try to extract classification directly from text
                        doc_type = self._extract_class_from_text(classification_text)
                        document_boundary = "continue"
                except Exception as e:
                    logger.warning(
                        f"Failed to parse structured data from response: {e}"
                    )
                    # Try to extract classification directly from text
                    doc_type = self._extract_class_from_text(classification_text)
                    document_boundary = "continue"

                # Validate the predicted class against the configured vocabulary
                if doc_type and doc_type in self.valid_doc_types:
                    break  # Valid prediction - done

                if not enforce:
                    # Legacy behavior: warn and use the prediction as-is.
                    if not doc_type:
                        doc_type = UNCLASSIFIED_CLASS
                        logger.warning(
                            f"Empty classification for page {page_id}, using 'unclassified'"
                        )
                    else:
                        logger.warning(
                            f"Unknown document type '{doc_type}' for page {page_id}, "
                            f"valid types are: {', '.join(self.valid_doc_types)}"
                        )
                        # Still use the classification, it might be a new valid type
                    break

                # Enforcement is on and the prediction is invalid.
                invalid_value = doc_type or "(empty)"
                if attempt < max_retries:
                    logger.warning(
                        f"Invalid class '{invalid_value}' for page {page_id} "
                        f"(attempt {attempt + 1}/{max_retries + 1}); re-prompting "
                        f"with valid classes."
                    )
                    attempt_content = self._build_validation_retry_content(
                        content, invalid_value
                    )
                else:
                    # Retries exhausted - assign configured fallback class.
                    fallback = self.config.classification.invalidClassFallback
                    validation_error = (
                        f"Model returned invalid class '{invalid_value}' after "
                        f"{max_retries + 1} attempt(s); assigned fallback "
                        f"'{fallback}'."
                    )
                    logger.error(f"Page {page_id}: {validation_error}")
                    doc_type = fallback
                    # The class is now ours, not the model's, so neither its
                    # confidence, its reasoning nor its ranked alternatives
                    # describe the stored class.
                    confidence = None
                    classification_reason = None
                    candidates = []

            t1 = time.time()
            logger.info(
                f"Time taken for classification of page {page_id}: {t1 - t0:.2f} seconds"
            )

            logger.info(f"Page {page_id} classified as {doc_type}")

            # Create and return classification result
            metadata: Dict[str, Any] = {
                "metering": metering,
                "document_boundary": str(document_boundary).lower(),
            }
            if validation_error:
                metadata["validation_error"] = validation_error
            # Travels in metadata alongside document_boundary so the cache and
            # the section builders carry it, and is copied onto the declared
            # Page.classification_reason field by _classify_pages_multimodal.
            if classification_reason:
                metadata["classification_reason"] = classification_reason
            # The ranked runner-up classes ("80% W-2, 15% 1099"), which are the
            # actual answer to "what else could this have been?". Travels the
            # same way as the reason above.
            if candidates:
                metadata["classification_candidates"] = candidates

            return PageClassification(
                page_id=page_id,
                classification=DocumentClassification(
                    doc_type=doc_type,
                    # None unless the model was asked for a confidence and
                    # returned a usable one — see parse_confidence.
                    confidence=confidence,
                    metadata=metadata,
                ),
                image_uri=image_uri,
                text_uri=text_uri,
                raw_text_uri=raw_text_uri,
            )
        except Exception as e:
            logger.error(f"Error classifying page {page_id}: {str(e)}")
            raise

    def classify_page_sagemaker(
        self,
        page_id: str,
        image_uri: Optional[str] = None,
        raw_text_uri: Optional[str] = None,
        text_uri: Optional[str] = None,
    ) -> PageClassification:
        """
        Classify a single page using SageMaker UDOP model endpoint.

        Args:
            page_id: ID of the page
            image_uri: URI of the page image
            raw_text_uri: URI of the raw text (Textract output)
            text_uri: URI of the processed text

        Returns:
            PageClassification: Classification result for the page
        """
        # Verify we have the required URIs
        if not image_uri or not raw_text_uri:
            logger.warning(f"Missing required URIs for page {page_id}")
            return self._create_unclassified_result(
                page_id=page_id,
                image_uri=image_uri,
                text_uri=text_uri,
                raw_text_uri=raw_text_uri,
                error_message="Missing required image_uri or raw_text_uri",
            )

        # Use the stored endpoint name
        endpoint_name = self.sagemaker_endpoint

        # Prepare payload
        payload = {
            "input_image": image_uri,
            "input_textract": raw_text_uri,
            "prompt": "",
            "debug": 0,
        }

        # Implement retry logic
        retry_count = 0
        metering = {}

        while retry_count < self.MAX_RETRIES:
            try:
                logger.info(
                    f"Classifying page {page_id} with SageMaker UDOP model. Payload: {json.dumps(payload)}"
                )
                t0 = time.time()

                # Invoke endpoint
                response = self.sm_client.invoke_endpoint(
                    EndpointName=endpoint_name,
                    ContentType="application/json",
                    Body=json.dumps(payload),
                )

                duration = time.time() - t0

                # Parse response
                response_body = json.loads(response["Body"].read().decode())
                doc_type = response_body.get("prediction", UNCLASSIFIED_CLASS)

                # Log success metrics
                logger.info(
                    f"Page {page_id} classification successful in {duration:.2f}s. Response: {response_body}"
                )

                # Add some metering data for consistency with Bedrock
                metering = {
                    "Classification/sagemaker/invoke_endpoint": {
                        "invocations": 1,
                    }
                }

                # Create and return classification result
                return PageClassification(
                    page_id=page_id,
                    classification=DocumentClassification(
                        doc_type=doc_type,
                        # Not scored: the UDOP endpoint returns a prediction with
                        # no score, so there is nothing to report. This used to
                        # claim 1.0, which asserted certainty the endpoint never
                        # expressed.
                        confidence=None,
                        metadata={
                            "metering": metering,
                            "document_boundary": "continue",
                        },
                    ),
                    image_uri=image_uri,
                    text_uri=text_uri,
                    raw_text_uri=raw_text_uri,
                )

            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                error_message = e.response["Error"]["Message"]

                retryable_errors = [
                    "ThrottlingException",
                    "ServiceQuotaExceededException",
                    "RequestLimitExceeded",
                    "TooManyRequestsException",
                ]

                if error_code in retryable_errors:
                    retry_count += 1

                    if retry_count == self.MAX_RETRIES:
                        logger.error(
                            f"Max retries ({self.MAX_RETRIES}) exceeded for page {page_id}"
                        )
                        break

                    backoff = utils.calculate_backoff(
                        retry_count, self.INITIAL_BACKOFF, self.MAX_BACKOFF
                    )
                    logger.warning(
                        f"SageMaker throttling occurred for page {page_id} "
                        f"(attempt {retry_count}/{self.MAX_RETRIES}). "
                        f"Error: {error_message}. "
                        f"Backing off for {backoff:.2f}s"
                    )

                    time.sleep(
                        backoff
                    )  # semgrep-ignore: arbitrary-sleep - Intentional delay backoff/retry. Duration is algorithmic and not user-controlled.
                else:
                    logger.error(
                        f"Non-retryable SageMaker error for page {page_id}: "
                        f"{error_code} - {error_message}"
                    )
                    # Return unclassified with error
                    return self._create_unclassified_result(
                        page_id=page_id,
                        image_uri=image_uri,
                        text_uri=text_uri,
                        raw_text_uri=raw_text_uri,
                        error_message=f"{error_code}: {error_message}",
                    )
            except Exception as e:
                logger.error(f"Unexpected error classifying page {page_id}: {str(e)}")
                # Return unclassified with error
                return self._create_unclassified_result(
                    page_id=page_id,
                    image_uri=image_uri,
                    text_uri=text_uri,
                    raw_text_uri=raw_text_uri,
                    error_message=str(e),
                )

        # If we've reached here after max retries, return error
        return self._create_unclassified_result(
            page_id=page_id,
            image_uri=image_uri,
            text_uri=text_uri,
            raw_text_uri=raw_text_uri,
            error_message="Max retries exceeded for SageMaker classification",
        )

    def classify_page(
        self,
        page_id: str,
        text_uri: Optional[str] = None,
        image_uri: Optional[str] = None,
        raw_text_uri: Optional[str] = None,
    ) -> PageClassification:
        """
        Classify a single page based on its text and/or image content.
        Uses the configured backend (Bedrock or SageMaker).

        Args:
            page_id: ID of the page
            text_uri: URI of the text content
            image_uri: URI of the image content
            raw_text_uri: URI of the raw text content

        Returns:
            PageClassification: Classification result for the page
        """
        if self.backend == "bedrock":
            return self.classify_page_bedrock(
                page_id=page_id,
                text_uri=text_uri,
                image_uri=image_uri,
                raw_text_uri=raw_text_uri,
            )
        else:  # sagemaker
            return self.classify_page_sagemaker(
                page_id=page_id,
                image_uri=image_uri,
                raw_text_uri=raw_text_uri,
                text_uri=text_uri,
            )

    def _build_validation_retry_content(
        self, original_content: List[Dict[str, Any]], invalid_class: str
    ) -> List[Dict[str, Any]]:
        """
        Build the content for a validation retry by appending a correction
        instruction to the original content.

        Because classification typically runs at temperature 0.0, re-sending
        the identical request would return the identical (invalid) answer. The
        appended correction message changes the input so the model is steered
        back to the allowed vocabulary. This is a single-turn re-prompt: we
        re-send the original content plus the correction, rather than threading
        a multi-turn conversation history.

        Args:
            original_content: The content list from the initial invocation.
            invalid_class: The out-of-vocabulary class the model returned.

        Returns:
            A new content list (the original is not mutated) with the
            correction instruction appended.
        """
        valid_classes = ", ".join(sorted(self.valid_doc_types))
        correction = (
            f"\n\nYour previous response classified the document as "  # nosec B608 - LLM reclassification prompt text, not a SQL query
            f"'{invalid_class}', which is NOT a valid class. You MUST choose "
            f"exactly one class from this list: [{valid_classes}]. "
            f"Respond again using the required output format and select only "
            f"from the allowed classes."
        )
        # Shallow-copy the list and append a new text item. The original
        # content dicts are not mutated.
        return list(original_content) + [{"text": correction}]

    def _invoke_bedrock_model(
        self, content: List[Dict[str, Any]], config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Invoke Bedrock model (or LambdaHook) with standard parameters.

        Args:
            content: Content to send to the model
            config: Configuration with model parameters

        Returns:
            Dictionary with response and metering data
        """
        return bedrock.invoke_model(
            model_id=config["model_id"],
            system_prompt=config["system_prompt"],
            content=content,
            temperature=config["temperature"],
            top_k=config["top_k"],
            top_p=config["top_p"],
            max_tokens=config["max_tokens"],
            # Single-pass keeps "Classification" with unchanged units, so a cost
            # comparison across this change is a straight read. Two-stage bills
            # its halves under "Segmentation" and "Labeling".
            context=config.get("metering_context", "Classification"),
            model_lambda_hook_arn=self.config.classification.model_lambda_hook_arn,
            reasoning_effort=config.get("reasoning_effort"),
        )

    def _create_unclassified_result(
        self,
        page_id: str,
        image_uri: Optional[str] = None,
        text_uri: Optional[str] = None,
        raw_text_uri: Optional[str] = None,
        error_message: str = "Unknown error",
        reason: str = UNCLASSIFIED_FAILED,
    ) -> PageClassification:
        """
        Create a standard unclassified result with error information.

        ``reason`` separates the two situations that reach this method, because
        they are not the same event and ``_record_unclassified_page_issues``
        reports them at different severities: a page the service had nothing to
        send (:data:`UNCLASSIFIED_NO_CONTENT`) may simply be empty, whereas a
        classification attempt that errored or exhausted its retries
        (:data:`UNCLASSIFIED_FAILED`) means the page's class is unknown because
        something went wrong. It defaults to the latter so a call site added later
        is loud rather than quiet by omission.

        Args:
            page_id: ID of the page
            image_uri: Optional URI of the image
            text_uri: Optional URI of the text content
            raw_text_uri: Optional URI of the raw text
            error_message: Error message to include in metadata
            reason: :data:`UNCLASSIFIED_FAILED` or :data:`UNCLASSIFIED_NO_CONTENT`

        Returns:
            PageClassification with unclassified result
        """
        return PageClassification(
            page_id=page_id,
            classification=DocumentClassification(
                doc_type=UNCLASSIFIED_CLASS,
                confidence=0.0,
                metadata={"error": error_message, "unclassified_reason": reason},
            ),
            image_uri=image_uri,
            text_uri=text_uri,
            raw_text_uri=raw_text_uri,
        )

    def _record_unclassified_page_issues(
        self, document: Document, all_page_results: List[PageClassification]
    ) -> None:
        """Record on each section why classification could not classify its pages.

        Classification produced no ``ProcessingIssue`` of any kind before this: a
        page whose classification failed after retries became ``unclassified`` with
        the reason in ``DocumentClassification.metadata["error"]``, and a page whose
        class was out of vocabulary after ``maxValidationRetries`` was coerced to
        ``invalidClassFallback`` with the reason in ``metadata["validation_error"]``.
        Neither reached anywhere a user looks. ``metadata`` is copied onto the
        ``Page`` object with ``setattr``, and ``Page`` has no ``metadata`` field, so
        it is absent from ``Document.to_dict`` and never survives the Step Functions
        hop or reaches DynamoDB; the matching ``document.errors`` line is read only
        by ``processresults_function``'s ``Status.FAILED`` branch, which a document
        that completes never enters. The document therefore finished green while a
        page's class was a fallback nobody chose.

        The issue is attached to the SECTION the page belongs to, not to the
        document, because ``ProcessingIssues`` is a Section field in the GraphQL
        schema — a document-level issue would bump ``ProcessingIssueCount`` and
        then have no text to show. One issue per (section, reason) with the page
        ids listed, rather than one per page, so a 50-page section cannot produce
        50 identical rows.

        Severity follows what happened, which is also what keeps this usable:

        * a classification that **failed** or exhausted its retries → ``error``;
        * an out-of-vocabulary class **coerced to the fallback** → ``warning``: a
          class was assigned, it is just not the model's, and it is the commonest
          of the three because it is where a page the model cannot place ends up;
        * a page with **nothing to classify** → ``warning``. Note what that
          actually requires: no usable OCR text AND no loadable page image, since
          ``classify_page_bedrock`` checks both. A blank page normally still has an
          image, so it does not land here — this is either a text-only ingestion of
          an empty page or missing page artifacts. Warning rather than error
          because the message cannot tell the two apart and says so.

        No CloudWatch metric is published from here. Every fleet-level alarm in
        this stack is paired with a threshold parameter a deployer tunes, and none
        of the three cases above has an established base rate to set one from;
        emitting an unalarmed metric would only add cost. The issues reach the
        Sections panel and the document list's ``ProcessingIssueCount`` badge, which
        is what an operator looks at.

        ⚠️ The document list's badge is **severity-blind** — it renders
        ``ProcessingIssueCount``, which counts warnings and errors alike — so a
        deployment whose documents routinely contain pages the classifier cannot
        place will see "Processing Issues" on them. That is the intended reading,
        since those pages produced no extracted data, but it is the cost of the
        signal; the lever is to define a class the model can legitimately choose for
        them (a catch-all "other"), which removes the condition rather than hiding
        it.

        The badge itself clears on reprocessing, because reprocessing goes through
        ``create_document``, which writes the whole item with ``Put`` and so replaces
        ``ProcessingIssueCount`` outright. The incremental ``update_document`` path
        writes that count only when the document object it is given actually carries
        the sections the count is derived from — a document with no sections reports
        ``0`` by absence rather than by measurement, and writing it would erase a
        count a per-section write had just recorded.

        ``HasProcessingIssues`` is written alongside the count but has **no reader**:
        it is not a GSI key, not projected, not queried, not in the UI and not in any
        Glue or Athena schema. It is also only ever ``SET`` and never ``REMOVE``d, so
        a document that once carried an issue keeps the attribute after a later run
        cleared it. Nothing reads it, so nothing is currently wrong — but it is not a
        filter to build on as it stands.
        """
        if not document.sections:
            return

        # reason code -> (page ids, one representative message)
        # code -> {page_id: detail}. Per PAGE, not per code: two pages can fail the
        # same way for different reasons (page 3 AccessDenied, page 40 Throttling),
        # and keeping only the first detail made every section's root_cause blame
        # one of them for all of them — which listing the page ids beside it turns
        # from a vague message into an actively wrong one.
        by_reason: Dict[str, Dict[str, str]] = {}
        for result in all_page_results:
            metadata = result.classification.metadata or {}
            error_message = metadata.get("error")
            validation_error = metadata.get("validation_error")
            if error_message:
                unclassified_reason = metadata.get(
                    "unclassified_reason", UNCLASSIFIED_FAILED
                )
                code = (
                    "classification_page_no_content"
                    if unclassified_reason == UNCLASSIFIED_NO_CONTENT
                    else "classification_failed"
                )
                detail = str(error_message)
            elif validation_error:
                code = "classification_invalid_class_fallback"
                detail = str(validation_error)
            else:
                continue
            # Bounded before it is stored. A validation_error embeds the model's
            # rejected output, which on a parse failure is a whole line of raw
            # generation; these issues ride to DynamoDB inside the section map,
            # which has a 400 KB item ceiling that the assessment path guards
            # explicitly and ``serialize_processing_issues`` does not truncate.
            by_reason.setdefault(code, {})[str(result.page_id)] = detail[
                :_MAX_ISSUE_DETAIL_CHARS
            ]

        if not by_reason:
            return

        severities = {
            "classification_failed": "error",
            "classification_page_no_content": "warning",
            "classification_invalid_class_fallback": "warning",
        }
        messages = {
            "classification_failed": (
                "Classification did not produce a class for {count} page(s) "
                "({pages}), which were labelled '{unclassified}'. No class means no "
                "extraction schema, so those pages' fields were not extracted and "
                "the section holds no data for them."
            ),
            "classification_page_no_content": (
                "{count} page(s) ({pages}) had neither usable OCR text nor a "
                "loadable page image, so they could not be classified and were "
                "labelled '{unclassified}'. No fields were extracted from them. "
                "Check the OCR step for those pages unless they are genuinely empty."
            ),
            "classification_invalid_class_fallback": (
                "The model returned a class outside the configured vocabulary for "
                "{count} page(s) ({pages}) after every retry, so the configured "
                "fallback class was assigned instead. The stored class is not the "
                "model's answer, and extraction ran against the fallback's schema."
            ),
        }

        for code, detail_by_page in by_reason.items():
            for section in document.sections:
                section_pages = sorted(
                    (
                        str(pid)
                        for pid in section.page_ids
                        if str(pid) in detail_by_page
                    ),
                    key=str,
                )
                if not section_pages:
                    continue
                # Only the distinct reasons THIS section's pages actually met, so
                # the root_cause cannot attribute one page's failure to another.
                seen_details: List[str] = []
                for pid in section_pages:
                    detail = detail_by_page[pid]
                    if detail not in seen_details:
                        seen_details.append(detail)
                shown = seen_details[:_MAX_ISSUE_DETAILS]
                root_cause = "; ".join(shown)
                if len(seen_details) > len(shown):
                    root_cause += f"; and {len(seen_details) - len(shown)} more"
                issue = ProcessingIssue(
                    stage="classification",
                    severity=severities[code],
                    code=code,
                    message=messages[code].format(
                        count=len(section_pages),
                        pages=", ".join(section_pages),
                        unclassified=UNCLASSIFIED_CLASS,
                    ),
                    root_cause=f"{root_cause} (pages {', '.join(section_pages)})",
                    section_id=section.section_id,
                    details={"page_ids": section_pages},
                )
                # Replace only this stage's issues: downstream stages write their
                # own onto the same section and the DynamoDB writer replaces the
                # whole section map, so anything else here must survive. Matching
                # on the code as well keeps two different classification issues on
                # one section (a failed page and a fallback page) from evicting
                # each other.
                section.processing_issues = [
                    pi
                    for pi in (section.processing_issues or [])
                    if not (
                        getattr(pi, "stage", None) == "classification"
                        and getattr(pi, "code", None) == code
                    )
                ] + [issue]
                logger.warning(
                    "Recorded classification ProcessingIssue %s on section %s for "
                    "page(s) %s",
                    code,
                    section.section_id,
                    ", ".join(section_pages),
                )

    def _extract_class_from_text(self, text: str) -> str:
        """Extract class name from text if JSON parsing fails."""
        # Check for common patterns
        patterns = [
            "class: ",
            "document type: ",
            "document class: ",
            "classification: ",
            "type: ",
        ]

        text_lower = text.lower()
        for pattern in patterns:
            if pattern in text_lower:
                start_idx = text_lower.find(pattern) + len(pattern)
                end_idx = text_lower.find("\n", start_idx)
                if end_idx == -1:
                    end_idx = len(text_lower)

                return text[start_idx:end_idx].strip().strip("\"'")

        return ""

    def _get_cache_key(self, document: Document) -> str:
        """
        Generate cache key for a document.

        Args:
            document: Document object

        Returns:
            Cache key string
        """
        workflow_id = (
            document.workflow_execution_arn.split(":")[-1]
            if document.workflow_execution_arn
            else "unknown"
        )
        return f"classcache#{document.id}#{workflow_id}"

    def _get_cached_page_classifications(
        self, document: Document
    ) -> Dict[str, PageClassification]:
        """
        Retrieve cached page classifications for a document.

        Args:
            document: Document object

        Returns:
            Dictionary mapping page_id to cached PageClassification, empty dict if no cache
        """

        logger.info(
            f"Attempting to retrieve cached page classifications for document {document.id}"
        )

        if not self.cache_table:
            return {}

        cache_key = self._get_cache_key(document)

        try:
            response = self.cache_table.get_item(Key={"PK": cache_key, "SK": "none"})

            if "Item" not in response:
                logger.info(f"No cache entry found for document {document.id}")
                return {}

            # Parse cached data from JSON
            cached_data = response["Item"]
            logger.debug(f"Cached data keys: {list(cached_data.keys())}")
            page_classifications = {}

            # Extract page classifications from JSON attribute
            if "page_classifications" in cached_data:
                try:
                    page_data_list = json.loads(cached_data["page_classifications"])

                    for page_data in page_data_list:
                        page_id = page_data["page_id"]
                        page_classifications[page_id] = PageClassification(
                            page_id=page_id,
                            classification=DocumentClassification(
                                doc_type=page_data["classification"]["doc_type"],
                                # .get: a cache entry written before confidence
                                # was optional (or by a run that produced none)
                                # has no key, and a missing score is not an
                                # error — it reads back as not scored.
                                confidence=page_data["classification"].get(
                                    "confidence"
                                ),
                                metadata=page_data["classification"]["metadata"],
                            ),
                            image_uri=page_data.get("image_uri"),
                            text_uri=page_data.get("text_uri"),
                            raw_text_uri=page_data.get("raw_text_uri"),
                        )

                    if page_classifications:
                        logger.info(
                            f"Retrieved {len(page_classifications)} cached page classifications for document {document.id} (PK: {cache_key})"
                        )

                except json.JSONDecodeError as e:
                    logger.warning(
                        f"Failed to parse cached page classifications JSON for document {document.id}: {e}"
                    )

            return page_classifications

        except Exception as e:
            logger.warning(
                f"Failed to retrieve cached classifications for document {document.id}: {e}"
            )
            return {}

    def _cache_successful_page_classifications(
        self, document: Document, page_classifications: List[PageClassification]
    ) -> None:
        """
        Cache successful page classifications to DynamoDB as a JSON-serialized list.

        Args:
            document: Document object
            page_classifications: List of successful page classifications
        """
        if not self.cache_table or not page_classifications:
            return

        cache_key = self._get_cache_key(document)

        try:
            # Filter out failed classifications and prepare data for JSON serialization
            successful_pages = []
            for page_result in page_classifications:
                # Only cache if there's no error in the metadata
                if "error" not in page_result.classification.metadata:
                    page_data = {
                        "page_id": page_result.page_id,
                        "classification": {
                            "doc_type": page_result.classification.doc_type,
                            "confidence": page_result.classification.confidence,
                            "metadata": page_result.classification.metadata,
                        },
                        "image_uri": page_result.image_uri,
                        "text_uri": page_result.text_uri,
                        "raw_text_uri": page_result.raw_text_uri,
                    }
                    successful_pages.append(page_data)

            if len(successful_pages) == 0:
                logger.debug(
                    f"No successful page classifications to cache for document {document.id}"
                )
                return

            # Prepare item structure with JSON-serialized page classifications
            item = {
                "PK": cache_key,
                "SK": "none",
                "cached_at": str(int(time.time())),
                "document_id": document.id,
                "workflow_execution_arn": document.workflow_execution_arn,
                "page_classifications": json.dumps(successful_pages),
                "ExpiresAfter": int(
                    (datetime.now(timezone.utc) + timedelta(days=1)).timestamp()
                ),
            }

            # Store in DynamoDB using Table resource with JSON serialization
            self.cache_table.put_item(Item=item)

            logger.info(
                f"Cached {len(successful_pages)} successful page classifications for document {document.id} (PK: {cache_key})"
            )

        except Exception as e:
            logger.warning(
                f"Failed to cache page classifications for document {document.id}: {e}"
            )

    def classify_document(self, document: Document) -> Document:
        """
        Classify a document's pages and update the Document object with sections.
        Uses the configured backend (Bedrock or SageMaker) and classification method.

        The classification method is determined by the 'classificationMethod' setting:
        - multimodalPageLevelClassification (default): Uses page-by-page classification
          with sequence segmentation similar to BIO (Begin-Inside-Outside) tagging.
          Each page receives both a document type and a boundary indicator:
          * "start": Marks the beginning of a new document segment
          * "continue": Indicates continuation of the current segment
          This enables accurate segmentation of multi-document packets where multiple
          documents of the same or different types may be combined in a single file.
        - textbasedHolisticClassification: Processes the entire document as a packet
          to identify document segments across pages using a holistic approach.

        Single-class configurations short-circuit the class decision (no backend
        call) but still honor ``sectionSplitting``: ``disabled`` yields one
        all-pages section and ``page`` yields one section per page.
        ``llm_determined`` runs the normal backend so boundary detection really
        happens — it is the default, and silently degrading it would collapse
        every multi-document packet by construction, which is the #686 bug
        itself. The zero-inference short-circuit therefore applies only when no
        boundary decision is needed: ``disabled``, ``page``, or a single-page
        document. See ``_can_skip_backend_for_single_class`` and
        ``_create_single_class_sections``.

        A document whose *name* matches a class's
        ``x-aws-idp-document-name-regex`` follows the same three-way rule
        (GitHub issue #705): ``disabled`` / ``page`` / a single-page document need
        no model call, while multi-page ``llm_determined`` runs classification for
        the per-page boundary signal with the class pinned to the regex-matched
        one. Knowing *what* a packet contains says nothing about *where* each
        record starts, so the name match cannot substitute for boundary
        detection. See ``_can_skip_backend_for_regex_match``.

        Args:
            document: Document object to classify and update

        Returns:
            Document: Updated Document object with classifications and sections
        """
        if not document.pages:
            logger.warning("Document has no pages to classify")
            return self._update_document_status(
                document,
                success=False,
                error_message="Document has no pages to classify",
            )

        # Check for a document-name regex match. The document's NAME asserts its
        # class, so no inference is needed for the class decision — but section
        # boundaries are a separate question, and skipping them along with the
        # class hard-coded ONE all-pages section regardless of sectionSplitting
        # (GitHub issue #705, the #686 defect reached by a different trigger).
        forced_class: Optional[str] = None
        regex_matched_class = self._check_document_name_regex(document)
        if regex_matched_class:
            if self._can_skip_backend_for_regex_match(document, regex_matched_class):
                logger.info(
                    f"Classifying all pages as '{regex_matched_class}' based on "
                    f"document name regex match, and sectionSplitting needs no "
                    f"boundary detection. Skipping LLM classification."
                )
                document = self._create_single_class_sections(
                    document, class_name=regex_matched_class
                )
                document = self._update_document_status(document)
                return document

            # Multi-page `llm_determined`: the class stays the regex-matched one,
            # but the per-page document_boundary signal it needs only exists if
            # the model runs. Fall through to the normal path with the class
            # pinned, so the boundaries are real and the regex stays
            # authoritative over the class (which is what the feature promises).
            forced_class = regex_matched_class

        # If there's only one document class defined, the class decision is
        # predetermined — but only skip the backend when the configured
        # sectionSplitting strategy genuinely needs no model output.
        if self._can_skip_backend_for_single_class(document):
            logger.info(
                f"Only one document class '{self.single_class_name}' is defined "
                f"and sectionSplitting needs no boundary detection. Classifying "
                f"all pages as this class without calling backend."
            )
            document = self._create_single_class_sections(document)
            document = self._update_document_status(document)
            return document

        # Check for limited page classification
        # "ALL" means use all pages, otherwise parse as int
        max_pages_str = str(self.max_pages_for_classification).upper()
        use_limited_pages = False
        max_pages_int = 0

        if max_pages_str != "ALL":
            try:
                max_pages_int = int(self.max_pages_for_classification)
                if max_pages_int > 0:
                    use_limited_pages = True
            except (ValueError, TypeError):
                pass  # Invalid value, treat as ALL

        if use_limited_pages:
            logger.info(f"Using limited page classification: {max_pages_int} pages")

            # Create limited document for classification
            limited_document = self._limit_pages_for_classification(document)

            if limited_document.id != document.id:  # Pages were actually limited
                # Classify the limited document.
                # NOTE: _apply_limited_classification_to_all_pages collapses the
                # result into one all-pages section for every config, so
                # maxPagesForClassification still overrides sectionSplitting here
                # — a pre-existing limitation of that setting, not specific to the
                # regex path.
                if self.strategy == self.STRATEGY_TWO_STAGE:
                    classified_limited = self.two_stage_classify_document(
                        limited_document, forced_class=forced_class
                    )
                elif self.granularity == self.GRANULARITY_PACKET:
                    logger.info(
                        f"Classifying limited document with "
                        f"{len(limited_document.pages)} pages using packet "
                        f"granularity"
                    )
                    classified_limited = self.packet_classify_document(
                        limited_document, forced_class=forced_class
                    )
                else:
                    classified_limited = self._classify_pages_multimodal(
                        limited_document, forced_class=forced_class
                    )

                # Apply results to all pages in original document
                document = self._apply_limited_classification_to_all_pages(
                    document, classified_limited
                )
                return document

        # Dispatch on strategy first, then on the resolved granularity (see
        # __init__: `granularity` when set, otherwise derived from the
        # deprecated classificationMethod). Two-stage honours granularity
        # inside Stage A, so the two settings compose rather than nest.
        if self.strategy == self.STRATEGY_TWO_STAGE:
            return self.two_stage_classify_document(
                document, forced_class=forced_class
            )

        if self.granularity == self.GRANULARITY_PACKET:
            return self.packet_classify_document(document, forced_class=forced_class)

        return self._classify_pages_multimodal(document, forced_class=forced_class)

    def classify_pages(self, pages: Dict[str, Dict[str, Any]]) -> ClassificationResult:
        """
        Classify multiple pages concurrently.

        Args:
            pages: Dictionary of pages with their data

        Returns:
            ClassificationResult: Result with classified pages grouped into sections
        """
        all_results = []
        futures = []
        metering = {}

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            for page_num, page_data in pages.items():
                future = executor.submit(
                    self.classify_page,
                    page_id=page_num,
                    text_uri=page_data.get("parsedTextUri"),
                    image_uri=page_data.get("imageUri"),
                    raw_text_uri=page_data.get("rawTextUri"),
                )
                futures.append(future)

            for future in as_completed(futures):
                try:
                    page_result = future.result()
                    page_metering = page_result.classification.metadata.get(
                        "metering", {}
                    )
                    all_results.append(page_result)

                    # Merge metering data
                    metering = utils.merge_metering_data(metering, page_metering)
                except Exception as e:
                    logger.error(f"Error in concurrent classification: {str(e)}")
                    raise

        # Group pages into sections
        sections = self._group_consecutive_pages(all_results)

        # Create and return classification result
        return ClassificationResult(metadata={"metering": metering}, sections=sections)

    def _apply_section_splitting_strategy(
        self, document: Document, page_results: List[PageClassification]
    ) -> Document:
        """
        Apply configured section splitting strategy.

        Args:
            document: Document with classified pages
            page_results: List of page classification results

        Returns:
            Document with sections created according to splitting strategy
        """
        segments = self._fold_page_verdicts_to_segments(page_results)
        return self._sections_from_segments(
            document, segments, page_results=page_results
        )

    def _fold_page_verdicts_to_segments(
        self, page_results: List[PageClassification]
    ) -> List[Segment]:
        """Fold per-page class + ``document_boundary`` verdicts into segments.

        This is the BIO-style (Begin-Inside-Outside) fold that page-level
        classification has always performed: walk the pages in order and start a
        new segment whenever the class changes **or** the page reports itself as
        a ``start``. It is now a step in its own right rather than being welded
        to section building, because the result — a list of
        :class:`~idp_common.classification.models.Segment` — is the same thing a
        packet-level call returns directly. One fold, one section builder, both
        granularities.
        """
        sorted_results = self._sort_page_results(page_results)
        if not sorted_results:
            return []

        self._log_boundary_signals(sorted_results)

        segments: List[Segment] = []
        current_type = sorted_results[0].classification.doc_type
        current_pages = [sorted_results[0]]

        def close_group() -> None:
            segments.append(
                Segment(
                    page_ids=[p.page_id for p in current_pages],
                    doc_class=current_type,
                    confidence=aggregate_page_confidence(
                        [p.classification.confidence for p in current_pages]
                    ),
                )
            )

        for result in sorted_results[1:]:
            # str() rather than a bare .lower(): the value comes from model
            # output, and a model that answered with a number or a list used to
            # raise AttributeError here.
            boundary = str(
                result.classification.metadata.get("document_boundary", "continue")
            ).lower()

            if result.classification.doc_type == current_type and boundary != "start":
                current_pages.append(result)
            else:
                close_group()
                current_type = result.classification.doc_type
                current_pages = [result]

        close_group()
        return segments

    def _log_boundary_signals(
        self, sorted_results: List[PageClassification]
    ) -> None:
        """Emit the per-page boundary signals as one line.

        The per-page ``document_boundary`` signal drives every merge decision in
        the fold, and for a long time it was persisted nowhere: the DynamoDB page
        record and the S3 ``document.json`` page dict both carried ``Class``
        only. Emitting the whole map as one line is what lets "why did these two
        documents end up in one section?" be answered without correlating N
        interleaved per-page classification logs from the thread pool.

        An absent key is reported distinctly from a literal ``continue``, because
        the two have very different diagnoses: the model omitting the field (the
        fold defaults to ``continue``) merges pages by accident, whereas an
        explicit ``continue`` is the model's judgement. That distinction is the
        one that was expensive to recover in #565.
        """
        boundary_map = {
            r.page_id: (
                str(r.classification.metadata["document_boundary"]).lower()
                if "document_boundary" in r.classification.metadata
                else "(absent)"
            )
            for r in sorted_results
        }
        # Capped: a 500-page packet would otherwise put every page's signal in one
        # log record. The pages that matter for diagnosing a merge are the ones
        # that said "start" plus the ones where the field was absent, so those are
        # always named; the rest are counted. The full per-page value is persisted
        # on the page record anyway (Page.document_boundary).
        notable = {
            page_id: signal
            for page_id, signal in boundary_map.items()
            if signal != "continue"
        }
        if len(boundary_map) <= 50:
            logger.info(f"Page document_boundary signals: {boundary_map}")
        else:
            logger.info(
                "Page document_boundary signals over %d pages: %d 'continue', "
                "notable (start/absent): %s",
                len(boundary_map),
                len(boundary_map) - len(notable),
                dict(list(notable.items())[:50]),
            )

    def _sections_from_segments(
        self,
        document: Document,
        segments: List[Segment],
        page_results: Optional[List[PageClassification]] = None,
    ) -> Document:
        """Build ``document.sections`` from segments, honouring ``sectionSplitting``.

        **The single section builder.** Both granularities route through it, so
        the three ``sectionSplitting`` strategies have one implementation each
        instead of one per granularity. The duplicate set is what let the two
        paths drift apart — most visibly on ``disabled``, where the page path
        voted across pages while the packet path took the first segment's class
        and assigned it to a section spanning segments the model had labelled
        differently.

        Every strategy also writes each page's class and confidence to agree
        with the section that page ended up in, so a page's own record never
        contradicts its section.

        Args:
            document: Document to attach sections to.
            segments: Segments to build from, in page order.
            page_results: The per-page results the segments were folded from,
                when there are any. Passed explicitly rather than read back off
                ``document.pages`` because only ONE of the two granularities has
                a per-page signal at all, and the difference is load-bearing:
                page-level classification scores every page separately, so
                ``sectionSplitting: page`` must report each page's own score
                rather than its segment's (which is the minimum across the
                segment and would make every page look as uncertain as the
                worst one). A packet-level call has no per-page signal to pass,
                and then the segment's score is the best available.
        """
        strategy = self.config.classification.sectionSplitting.lower()

        if not segments:
            logger.warning(
                "No segments to build sections from; leaving document.sections as-is"
            )
            return document

        verdicts = {
            r.page_id: (r.classification.doc_type, r.classification.confidence)
            for r in (page_results or [])
        }

        if strategy == "disabled":
            return self._single_section_from_segments(document, segments, verdicts)
        if strategy == "page":
            return self._per_page_sections_from_segments(document, segments, verdicts)
        return self._section_per_segment(document, segments, verdicts)

    def _single_section_from_segments(
        self,
        document: Document,
        segments: List[Segment],
        verdicts: Dict[str, tuple[str, Optional[float]]],
    ) -> Document:
        """One section over every page (``sectionSplitting: disabled``).

        The class is chosen by **page-weighted vote** across the segments,
        counting only classes that are in the configured vocabulary, with ties
        broken by earliest page. For page-level classification that is
        identical to the per-page vote this replaced (a folded segment is
        homogeneous in class, so one vote per page and one vote per page of each
        segment are the same count).

        For packet-level classification it is a change, and a deliberate one:
        that path used to take ``segments[0]``'s class, which is an arbitrary
        choice when the model reported several — the first segment may be a
        single cover page and the rest of the packet something else. The vote
        is the same rule the page path already used.

        A page that did **not** predict the winning class ends up labelled with
        a class it did not choose, so its confidence — which was about a
        different class — becomes ``None`` rather than being carried over or
        rewritten to 1.0.
        """
        from collections import Counter

        # Each page's own verdict, so the "did this page vote for the winner?"
        # test below is about what the page actually said. A page with no
        # verdict of its own falls back to its segment's: it did not predict
        # something *else*, so there is nothing for the winning class to
        # contradict. That covers both a packet-level call (no per-page signal
        # exists) and the zero-inference paths, where a single-class
        # configuration or a document-name-regex match asserts the class
        # directly — treating "silent" as "disagreed" would discard the
        # confidence those paths assert.
        segment_by_page = {
            page_id: segment
            for page_id, segment in self._pages_in_segment_order(segments)
        }
        predicted_by_page: Dict[str, Optional[str]] = {}
        confidence_by_page: Dict[str, Optional[float]] = {}
        for page_id in document.pages:
            if page_id in verdicts:
                predicted_by_page[page_id], confidence_by_page[page_id] = verdicts[
                    page_id
                ]
            elif page_id in segment_by_page:
                predicted_by_page[page_id] = segment_by_page[page_id].doc_class
                confidence_by_page[page_id] = segment_by_page[page_id].confidence
            else:
                predicted_by_page[page_id] = None
                confidence_by_page[page_id] = None

        votes: Counter[str] = Counter()
        for segment in segments:
            if segment.doc_class in self.valid_doc_types:
                votes[segment.doc_class] += len(segment.page_ids)

        if votes:
            top_count = max(votes.values())
            tied = [cls for cls, count in votes.items() if count == top_count]
            if len(tied) > 1:
                # Tie-breaker: the class of the earliest segment among the tied.
                winner = next(
                    (s.doc_class for s in segments if s.doc_class in tied), tied[0]
                )
                logger.info(
                    f"Classification tie detected ({sorted(tied)}), using earliest "
                    f"segment's class: '{winner}'"
                )
            else:
                winner = tied[0]
            logger.info(
                f"Classification voting results: {dict(votes)} -> selected '{winner}'"
            )
        else:
            winner = segments[0].doc_class
            logger.warning(
                f"No segment carries a configured class; using the first "
                f"segment's class: '{winner}'"
            )

        for page_id in document.pages:
            document.pages[page_id].classification = winner
            document.pages[page_id].confidence = (
                confidence_by_page.get(page_id)
                if predicted_by_page.get(page_id) == winner
                else None
            )

        document.sections = [
            Section(
                section_id="1",
                classification=winner,
                confidence=aggregate_page_confidence(
                    [document.pages[page_id].confidence for page_id in document.pages]
                ),
                page_ids=list(document.pages.keys()),
            )
        ]
        logger.info(
            f"Created single section with {len(document.pages)} pages, "
            f"class='{winner}' (sectionSplitting=disabled)"
        )
        return document

    def _per_page_sections_from_segments(
        self,
        document: Document,
        segments: List[Segment],
        verdicts: Dict[str, tuple[str, Optional[float]]],
    ) -> Document:
        """One section per page (``sectionSplitting: page``).

        Each page keeps its **own** score where one exists: page-level
        classification scores pages separately, and a segment's score is the
        minimum across its pages, so using the segment's here would report every
        page of a segment as being as uncertain as its worst page — in the one
        strategy whose whole purpose is per-page granularity. A packet-level
        call supplies no per-page verdicts, and then the segment's score is the
        best available and every page of a segment shares it.

        Iterates the pages the segments actually cover, in order, rather than
        every page of the document. A page no segment covers has no classified
        result to put in a section — the packet path used to give it one anyway,
        carrying whatever its ``classification`` field happened to hold, which
        for an uncovered page is nothing.
        """
        document.sections = []

        for idx, (page_id, segment) in enumerate(
            self._pages_in_segment_order(segments), start=1
        ):
            confidence = (
                verdicts[page_id][1] if page_id in verdicts else segment.confidence
            )
            if page_id in document.pages:
                document.pages[page_id].classification = segment.doc_class
                document.pages[page_id].confidence = confidence

            document.sections.append(
                Section(
                    section_id=str(idx),
                    classification=segment.doc_class,
                    confidence=confidence,
                    page_ids=[page_id],
                )
            )

        logger.info(
            f"Created {len(document.sections)} sections (one per page) with "
            f"sectionSplitting=page"
        )
        return document

    def _section_per_segment(
        self,
        document: Document,
        segments: List[Segment],
        verdicts: Dict[str, tuple[str, Optional[float]]],
    ) -> Document:
        """One section per segment (``sectionSplitting: llm_determined``, the default).

        The section's score is the segment's. A page keeps its own score when it
        has one, so a reviewer looking at a merged section can still see which
        page in it the classifier was least sure about.
        """
        document.sections = []

        for idx, segment in enumerate(segments, start=1):
            page_ids = [pid for pid in segment.page_ids if pid in document.pages]
            if not page_ids:
                logger.warning(
                    f"Segment {idx} ({segment.start_page}-{segment.end_page}) "
                    f"covers no page of this document; skipping"
                )
                continue

            for page_id in page_ids:
                document.pages[page_id].classification = segment.doc_class
                document.pages[page_id].confidence = (
                    verdicts[page_id][1] if page_id in verdicts else segment.confidence
                )

            document.sections.append(
                Section(
                    section_id=str(len(document.sections) + 1),
                    classification=segment.doc_class,
                    confidence=segment.confidence,
                    page_ids=page_ids,
                )
            )

        logger.info(
            f"Created {len(document.sections)} sections from "
            f"{len(segments)} segments (sectionSplitting=llm_determined)"
        )
        return document

    @staticmethod
    def _pages_in_segment_order(
        segments: List[Segment],
    ) -> List[tuple[str, Segment]]:
        """Every page the segments cover, in segment then page order, deduplicated.

        Deduplication matters for windowed packet classification, where adjacent
        windows deliberately overlap: the same page can appear in two windows'
        segments, and a page must still produce exactly one section.
        """
        seen: Set[str] = set()
        ordered: List[tuple[str, Segment]] = []
        for segment in segments:
            for page_id in segment.page_ids:
                if page_id in seen:
                    continue
                seen.add(page_id)
                ordered.append((page_id, segment))
        return ordered

    def _needs_boundary_detection(self, document: Document) -> bool:
        """True when the configured ``sectionSplitting`` needs a model signal.

        Shared by both short-circuits — the single-class one (GitHub issue #686)
        and the document-name-regex one (GitHub issue #705). Both predetermine
        the *class*, and neither predetermines the section *boundaries*:

        - ``disabled`` — one section over all pages. Nothing to detect.
        - ``page`` — one section per page. Nothing to detect.
        - **single-page document, any strategy** — one page cannot be split, so
          the result is identical either way and inference would be pure waste.

        ``llm_determined`` on a multi-page document does need the model: that is
        where boundaries come from. It is also the config *default*, so treating
        it as "nothing to detect" is what collapses every multi-document packet
        into one section by construction — the bug both issues report.
        """
        strategy = self.config.classification.sectionSplitting.lower()
        if strategy in ("disabled", "page"):
            return False

        # One page cannot be split, whatever the strategy says.
        return len(document.pages) > 1

    def _can_skip_backend_for_regex_match(
        self, document: Document, class_name: str
    ) -> bool:
        """True when a document-name-regex match needs no backend call at all.

        ``x-aws-idp-document-name-regex`` derives the *class* from the document's
        name, so no inference is needed to make that decision. Section
        boundaries, however, are a separate question — and this path used to skip
        them along with the class, hard-coding ONE section spanning every page
        regardless of ``sectionSplitting`` (GitHub issue #705). A packet holding
        several separate documents of the matched class therefore collapsed into
        a single section and extraction returned only the first record;
        ``sectionSplitting: page`` was silently ignored.

        The same three-way rule as the single-class short-circuit applies (see
        ``_needs_boundary_detection``). Note that "the class came from the
        filename" does not make boundary detection unnecessary: knowing *what* a
        packet contains says nothing about *where* one record ends and the next
        begins. So for a multi-page ``llm_determined`` document this returns
        False and classification runs — with the class pinned to the
        regex-matched one (``forced_class``), so the model's contribution is the
        per-page ``document_boundary`` signal and the regex stays authoritative
        over the class, exactly as the feature documents.

        BEHAVIOUR AND COST CHANGE, deliberate: ``llm_determined`` is the
        *default*, so a regex-matched **multi-page** document now incurs
        classification inference where it previously incurred none — the
        performance shortcut no longer skips the model for those. Single-page
        matches (the common "name the file and skip the LLM" case) keep the
        zero-inference path, and ``sectionSplitting: disabled`` is the explicit
        opt-out for multi-page files that are always one document.
        """
        if self._needs_boundary_detection(document):
            logger.info(
                "Document name regex matched class '%s' but sectionSplitting='%s' "
                "needs boundary detection across %d pages: running classification "
                "for the per-page boundary signal with the class pinned to '%s'. "
                "Set sectionSplitting: disabled (or page) to skip inference "
                "entirely.",
                class_name,
                self.config.classification.sectionSplitting.lower(),
                len(document.pages),
                class_name,
            )
            return False
        return True

    def _can_skip_backend_for_single_class(self, document: Document) -> bool:
        """True when a single-class config needs no backend call at all.

        A single-class configuration makes the *class* decision predetermined, so
        classification used to skip the backend entirely. But section boundaries
        are a separate question, and skipping them along with the class is what
        made ``sectionSplitting`` a no-op for these configs (GitHub issue #686).

        The backend can only be skipped when the configured strategy genuinely
        needs no model output:

        - ``disabled`` — one section over all pages. Nothing to detect.
        - ``page`` — one section per page. Nothing to detect.
        - **any strategy on a single-page document** — there is no boundary
          decision to make on one page, so the result is identical either way and
          an inference call would be pure waste. This keeps the common
          "one document per file" single-class deployment at zero classification
          cost, which is what the original short-circuit was really protecting.

        ``llm_determined`` on a multi-page document is NOT skippable. It asks for
        boundary detection, boundaries come from the model, so the model must
        run. Silently returning one all-pages section instead is the bug.

        Note the cost consequence: ``llm_determined`` is the *default*, so a
        multi-page single-class deployment that never set ``sectionSplitting``
        now performs classification inference where it previously performed none.
        That is the correct trade — the previous behaviour merged every record in
        a multi-record packet into one section and extraction returned only the
        first — and ``sectionSplitting: disabled`` is the explicit opt-out for
        deployments that know one file is always one document.
        """
        if not self.has_single_class:
            return False

        if not self._needs_boundary_detection(document):
            return True

        logger.info(
            "Single-class configuration '%s' with sectionSplitting='%s': running "
            "classification for boundary detection across %d pages. Set "
            "sectionSplitting: disabled to skip it when one file is always one "
            "document.",
            self.single_class_name,
            self.config.classification.sectionSplitting.lower(),
            len(document.pages),
        )
        return False

    def _create_single_class_sections(
        self, document: Document, class_name: Optional[str] = None
    ) -> Document:
        """
        Assign every page to one known class and split into sections according to
        the configured ``sectionSplitting`` strategy.

        This serves both zero-inference short-circuits, which differ only in how
        the class was determined:

        - the configuration defines exactly one document class (GitHub issue
          #686) — ``class_name`` omitted, ``self.single_class_name`` is used;
        - the document's name matched a class's
          ``x-aws-idp-document-name-regex`` (GitHub issue #705) — the matched
          class is passed in as ``class_name``.

        In both cases the *class decision* is predetermined, so no backend call
        is needed to make it. Boundary detection is a separate question, though,
        and used to be skipped along with it — every packet collapsed into one
        section spanning all pages regardless of ``sectionSplitting``. For a "one
        record type per packet" configuration that silently merged every record
        in the packet into a single section, and extraction then returned only
        the first one.

        Only reached when ``_can_skip_backend_for_single_class`` /
        ``_can_skip_backend_for_regex_match`` say the backend is genuinely
        unnecessary, so the strategies handled here are the ones that need no
        model output:

        - ``disabled`` — one section over all pages.
        - ``page`` — one section per page. This is the documented escape hatch
          for multi-record packets and now actually works.
        - any strategy on a **single-page** document, where there is no boundary
          decision to make.

        ``llm_determined`` on a multi-page document does not arrive here at all —
        it falls through to real classification, because boundaries come from the
        model and a knob that asks for boundary detection has to perform it.

        Args:
            document: Document whose pages should all be classified as
                ``class_name``
            class_name: The predetermined class. Defaults to the sole configured
                class; the document-name-regex path passes the matched class.

        Returns:
            Document with pages classified and sections created per strategy
        """
        class_name = class_name or self.single_class_name or "undefined"

        # Synthesize the page-result list the shared section builders expect, so
        # `disabled` and `page` reuse exactly the same code (and the same page
        # sorting) as the multi-class path instead of duplicating it here.
        page_results = [
            PageClassification(
                page_id=page_id,
                classification=DocumentClassification(
                    doc_type=class_name, confidence=1.0
                ),
            )
            for page_id in document.pages
        ]

        # Routed through the shared fold + section builder like every other
        # path. The synthesized results carry no `document_boundary`, so the
        # fold produces exactly ONE segment spanning every page (same class, no
        # page claiming to be a start), and the three strategies then land where
        # this method used to send them by hand: `page` gives one section per
        # page, `disabled` gives one section, and `llm_determined` — which only
        # reaches here for a single-page document — gives one section over that
        # page. Nothing is special-cased on the strategy any more.
        return self._apply_section_splitting_strategy(document, page_results)

    def _sort_page_results(
        self, results: List[PageClassification]
    ) -> List[PageClassification]:
        """
        Sort page results by page ID, trying numeric sort first, falling back to string sort.

        Args:
            results: List of page classification results

        Returns:
            Sorted list of page classification results
        """
        try:
            return sorted(results, key=lambda x: int(x.page_id))
        except (ValueError, TypeError):
            logger.warning("Unable to sort page IDs as integers, using string sort")
            return sorted(results, key=lambda x: x.page_id)

    def _create_section(
        self,
        section_id: str,
        doc_type: str,
        pages: List[Any],
        confidence: Optional[float] = None,
    ) -> Union[DocumentSection, Section]:
        """
        Create a document section based on the input type.

        Args:
            section_id: ID for the section
            doc_type: Document type for the section
            pages: List of pages (either PageClassification or page_ids)
            confidence: Confidence in the section's class, or None for not
                scored (the default — callers that have a score pass it, usually
                from aggregate_page_confidence)

        Returns:
            Either DocumentSection or Section depending on the input pages type
        """
        # Check if we're dealing with page IDs (strings) or PageClassification objects
        if pages and isinstance(pages[0], str):
            # Create a Section with page_ids
            return Section(
                section_id=section_id,
                classification=doc_type,
                confidence=confidence,
                page_ids=pages,
            )
        else:
            # Create a DocumentSection with PageClassification objects
            return DocumentSection(
                section_id=section_id,
                classification=DocumentClassification(
                    doc_type=doc_type, confidence=confidence
                ),
                pages=pages,
            )

    def _group_consecutive_pages(
        self, results: List[PageClassification]
    ) -> List[DocumentSection]:
        """
        Group consecutive pages into sections using sequence segmentation.

        This method implements the BIO-like tagging approach by examining both:
        1. Document type (classification)
        2. Document boundary indicator ("start" or "continue")

        A new section is created when:
        - The document type changes from one page to the next
        - A page has boundary="start", indicating a new document begins

        This enables accurate segmentation of multi-document packets where multiple
        documents of the same type may appear consecutively.

        Args:
            results: List of page classification results

        Returns:
            List of document sections with properly segmented page groups
        """
        sorted_results = self._sort_page_results(results)
        sections = []

        if not sorted_results:
            return sections

        current_group = 1
        current_type = sorted_results[0].classification.doc_type
        current_pages = [sorted_results[0]]

        for result in sorted_results[1:]:
            boundary = result.classification.metadata.get(
                "document_boundary", "continue"
            ).lower()
            if result.classification.doc_type == current_type and boundary != "start":
                current_pages.append(result)
            else:
                # Create a section with the current group
                sections.append(
                    self._create_section(
                        section_id=str(current_group),
                        doc_type=current_type,
                        pages=current_pages,
                        confidence=aggregate_page_confidence(
                            [p.classification.confidence for p in current_pages]
                        ),
                    )
                )
                current_group += 1
                current_type = result.classification.doc_type
                current_pages = [result]

        # Add the last section
        sections.append(
            self._create_section(
                section_id=str(current_group),
                doc_type=current_type,
                pages=current_pages,
                confidence=aggregate_page_confidence(
                    [p.classification.confidence for p in current_pages]
                ),
            )
        )

        return sections

    def _format_classes_and_descriptions(self) -> str:
        """Format document classes and descriptions as a markdown table for classification."""
        # Convert list of DocumentType to list of dicts for markdown table formatting
        classes_dicts = [
            {"type": dt.type_name, "description": dt.description}
            for dt in self.document_types
        ]

        # Create markdown table
        header = "| type | description |\n| --- | --- |\n"
        rows = "\n".join(
            [
                f"| {class_dict['type']} | {class_dict['description']} |"
                for class_dict in classes_dicts
            ]
        )

        return header + rows

    def _calculate_and_store_page_indices(self, document: Document) -> Document:
        """
        Calculate page_indices for all sections and store in section.attributes.

        This ensures consistent page_indices calculation across all sections in a document packet.
        Each section will have its page_indices calculated relative to the global minimum page ID,
        preventing the bug where all sections had page_indices starting from 0.

        Args:
            document: Document with sections to process

        Returns:
            Document with page_indices stored in each section's attributes
        """
        if not document.sections:
            return document

        try:
            # Calculate global minimum page ID across all sections
            all_page_ids = []
            for section in document.sections:
                all_page_ids.extend(section.page_ids)

            if all_page_ids:
                global_min_page_id = min(int(page_id) for page_id in all_page_ids)
                logger.info(
                    f"Calculated global_min_page_id={global_min_page_id} for page_indices calculation"
                )

                # Calculate and store page_indices for each section
                for section in document.sections:
                    page_indices = [
                        int(page_id) - global_min_page_id
                        for page_id in section.page_ids
                    ]
                    section.attributes = section.attributes or {}
                    section.attributes["page_indices"] = page_indices
                    logger.debug(
                        f"Section {section.section_id}: page_ids={section.page_ids} -> page_indices={page_indices}"
                    )

        except (ValueError, TypeError) as e:
            logger.error(
                f"Error calculating page_indices: {e}. Sections will not have page_indices pre-calculated."
            )

        return document

    def _update_document_status(
        self,
        document: Document,
        success: bool = True,
        error_message: Optional[str] = None,
    ) -> Document:
        """
        Update document status based on processing results.

        Args:
            document: Document to update
            success: Whether processing was successful
            error_message: Optional error message to add

        Returns:
            Updated document with appropriate status
        """
        if error_message and error_message not in document.errors:
            document.errors.append(error_message)

        if not success:
            document.status = Status.FAILED
            if error_message:
                logger.error(error_message)
        else:
            if document.errors:
                logger.warning(
                    f"Document classified with {len(document.errors)} errors"
                )

        # Calculate and store page_indices for each section for use during extraction
        document = self._calculate_and_store_page_indices(document)

        # Populate Section.excluded / Section.exclusion_reason based on class
        # configuration. Downstream services use these flags to skip sections
        # containing only static/boilerplate content.
        document = self._mark_excluded_sections(document)

        return document

    def _format_pages(self, document: Document) -> Dict[str, str]:
        """
        Format document pages as text.

        Args:
            document: Document object with pages

        Returns:
            Dictionary mapping page_id to text content
        """
        return self._format_pages_with_coverage(document)[0]

    def _format_pages_with_coverage(
        self, document: Document
    ) -> tuple[Dict[str, str], int]:
        """Format document pages as text, and report how many carry real text.

        The count is what distinguishes "this packet has a few image-only pages"
        from "this packet has no text at all", and only the caller can decide
        which matters. Packet classification needs the distinction because a
        page with no text is substituted with the placeholder string
        ``[No text content for page N]``: readable in a prompt, and indentical
        in shape to real content as far as the model is concerned. With the OCR
        backend set to ``none`` EVERY page is that placeholder, so a text-only
        packet call used to classify the whole packet from nothing at all and
        return a confident, meaningless answer. See the guard in
        :meth:`_assert_packet_text_available`.

        Returns:
            ``(pages_content, pages_with_text)`` where ``pages_with_text``
            counts pages whose text was actually loaded and non-empty.
        """
        pages_content = {}
        pages_with_text = 0

        for page_id, page in document.pages.items():
            # Fetch page text content from S3 if available
            if page.parsed_text_uri:
                try:
                    text = s3.get_text_content(page.parsed_text_uri)
                except Exception as e:
                    logger.warning(
                        f"Failed to load text content from {page.parsed_text_uri}: {e}"
                    )
                    # Continue with empty content
                    pages_content[page_id] = f"[Error loading page {page_id} content]"
                else:
                    pages_content[page_id] = text
                    if text and text.strip():
                        pages_with_text += 1
            else:
                # Page has no text content
                pages_content[page_id] = f"[No text content for page {page_id}]"

        return pages_content, pages_with_text

    def _parse_model_segments(
        self,
        raw_segments: List[Any],
        document: Document,
        forced_class: Optional[str] = None,
        window_page_ids: Optional[List[str]] = None,
        require_class: bool = True,
    ) -> List[Segment]:
        """Turn a packet call's ``segments`` array into :class:`Segment` objects.

        The model reports **ordinal** page numbers — 1 for the first page it was
        shown — which is why ``window_page_ids`` exists: a windowed packet call
        sees a slice of the document, so its ordinal 1 is not the document's
        page 1. Mapping is positional rather than by arithmetic offset, because
        a window's pages are not guaranteed to be a contiguous integer run:
        ``maxPagesForClassification`` already produces documents whose page set
        is a subset.

        With no window given, an ordinal is read as a page ID directly, which is
        what the unwindowed packet path has always assumed.

        A segment missing any required field is skipped with a warning rather
        than failing the document, and a segment whose range covers no page of
        this document is dropped the same way: a model that invents page 40 of a
        12-page packet should not take the other eleven segments down with it.

        Args:
            raw_segments: The parsed ``segments`` array from the model.
            document: Document whose pages the ordinals resolve against.
            forced_class: When set, the model's confidence does not describe the
                class being assigned (the operator's regex match does), so each
                segment is left unscored.
            window_page_ids: The page IDs this call was shown, in the order it
                was shown them. ``None`` for a call that saw the whole document
                with its own page IDs.
            require_class: Whether a segment must carry a ``type``. False for
                two-stage Stage A, which is asked for page ranges only and
                whose segments are deliberately unlabeled — the whole reason
                ``Segment.doc_class`` is optional.

        Returns:
            Segments in the order the model reported them, each covering at
            least one page of ``document``.
        """
        parsed: List[Segment] = []
        required_keys = ("ordinal_start_page", "ordinal_end_page") + (
            ("type",) if require_class else ()
        )

        def resolve(ordinal: int) -> Optional[str]:
            if window_page_ids is None:
                page_id = str(ordinal)
                return page_id if page_id in document.pages else None
            if 1 <= ordinal <= len(window_page_ids):
                return window_page_ids[ordinal - 1]
            return None

        for i, segment in enumerate(raw_segments):
            if not isinstance(segment, dict):
                logger.warning(f"Segment {i} is not an object; skipping")
                continue
            if not all(k in segment for k in required_keys):
                logger.warning(f"Segment {i} is missing required fields")
                continue

            try:
                start_ordinal = int(segment["ordinal_start_page"])
                end_ordinal = int(segment["ordinal_end_page"])
            except (TypeError, ValueError):
                logger.warning(
                    f"Segment {i} has unreadable page ordinals "
                    f"({segment.get('ordinal_start_page')!r}, "
                    f"{segment.get('ordinal_end_page')!r}); skipping"
                )
                continue

            if end_ordinal < start_ordinal:
                logger.warning(
                    f"Segment {i} ends before it starts "
                    f"({start_ordinal}-{end_ordinal}); skipping"
                )
                continue

            doc_type = segment.get("type")
            if doc_type is not None and doc_type not in self.valid_doc_types:
                logger.warning(f"Unknown document type '{doc_type}', using anyway")

            # Optional, exactly as on the page-level path: used when the packet
            # prompt asks each segment for a confidence, None otherwise. A
            # forced class is the operator's own assertion, so the model's
            # number does not describe it.
            confidence = (
                None
                if forced_class
                else parse_confidence(segment.get("confidence"), context=f"segment {i}")
            )

            page_ids = [
                page_id
                for page_id in (
                    resolve(ordinal) for ordinal in range(start_ordinal, end_ordinal + 1)
                )
                if page_id is not None and page_id in document.pages
            ]
            if not page_ids:
                logger.warning(
                    f"Segment {i} (ordinals {start_ordinal}-{end_ordinal}) covers "
                    f"no page of this document; skipping"
                )
                continue

            parsed.append(
                Segment(page_ids=page_ids, doc_class=doc_type, confidence=confidence)
            )

        return parsed

    def _packet_text_unavailable_error(
        self, document: Document, pages_with_text: int
    ) -> Optional[str]:
        """Message to fail a packet call that would be sent with no text at all.

        Packet classification sends text. When no page has any — the shape an
        OCR backend of ``none`` produces — every page is substituted with
        ``[No text content for page N]`` and the request still goes out, so the
        model returns segments and classes derived from nothing. The result is a
        document that completes green with fabricated boundaries, which is worse
        than a failure because nothing downstream can tell it apart from a real
        one.

        Returns ``None`` when the call can legitimately proceed: at least one
        page has text, or (from Phase 2 onwards) images are being sent and the
        packet call therefore has real content to look at even with no OCR.
        """
        if pages_with_text > 0:
            return None
        if not document.pages:
            return None

        _, send_images = self._resolve_modality()
        if send_images:
            # Images carry the content; a packet call with images and no OCR
            # text is a legitimate image-only configuration.
            return None

        return (
            f"Packet classification has no text to classify: none of the "
            f"{len(document.pages)} pages has OCR text, and the classification "
            f"prompt is text-only. This is what an OCR backend of 'none' "
            f"produces. Either set the OCR backend to a real engine "
            f"(Textract/Bedrock), or enable Classification > Modality > images "
            f"so the page images are sent instead."
        )

    def _packet_windows(self, sorted_page_ids: List[str]) -> List[List[str]]:
        """Split a packet's pages into the windows each packet call will see.

        A packet smaller than the window is one window, which is the common case
        and is byte-identical to the unwindowed call that preceded this: the
        model sees every page, its ordinals are the document's page IDs, and its
        segments are used directly.

        Above the window size the packet is split with ``overlap_pages`` repeated
        between neighbours. The overlap exists so the window **after** a junction
        can see the pages before its own first page — without it, a document
        boundary landing on a window's first page has to be judged with no
        preceding context, which is precisely the judgement page-level
        classification is bad at and packet granularity exists to improve.

        ``window_pages: 0`` means never window. That is what legacy holistic
        classification did, and it is kept reachable because it is the only way
        to guarantee the model sees the packet as a single whole — at the cost of
        failing outright on a packet larger than the context window.
        """
        packet_cfg = self.config.classification.packet
        window = packet_cfg.window_pages
        overlap = packet_cfg.overlap_pages
        total = len(sorted_page_ids)

        if window <= 0 or total <= window:
            return [sorted_page_ids]

        # Guaranteed >= 1 by ClassificationPacketConfig.refuse_non_advancing_window,
        # which refuses overlap >= window at config-load time. Clamped anyway so
        # a service constructed from a hand-built dict cannot loop forever.
        step = max(1, window - overlap)

        windows: List[List[str]] = []
        start = 0
        while start < total:
            windows.append(sorted_page_ids[start : start + window])
            if start + window >= total:
                break
            start += step

        logger.info(
            "Packet of %d pages split into %d windows of up to %d pages with %d "
            "page(s) of overlap",
            total,
            len(windows),
            window,
            overlap,
        )
        return windows

    def _merge_window_segments(
        self, window_segments: List[List[Segment]], sorted_page_ids: List[str]
    ) -> List[Segment]:
        """Merge the segments several windows reported into one segment list.

        **The earlier window wins for an overlapping page.** The overlap pages
        are the trailing pages of window *k* and the leading pages of window
        *k+1*: window *k* saw everything before them, window *k+1* sees only
        what follows. "Does a new document start here?" is answered mostly by
        what came before — whether the previous document has ended — so the
        window holding that context is the one whose verdict is taken. Window
        *k+1* still needs those pages in its request, as the context for the
        pages after them, which is the whole reason the overlap is sent.

        The merge projects each window's segments onto per-page
        ``(class, boundary)`` verdicts and then runs them back through
        :meth:`_fold_page_verdicts_to_segments` — the same fold page-level
        classification uses. One fold, so a windowed packet and a page-level
        document cannot disagree about how verdicts become segments, and a
        boundary the model reported inside a window survives as a ``start``.
        """
        if len(window_segments) == 1:
            # Unwindowed: use the model's segments directly, exactly as the
            # unwindowed packet path always has. Going through the fold would
            # give the same answer but is a pointless round trip, and the
            # direct path is the one with years of behaviour behind it.
            return window_segments[0]

        verdicts: Dict[str, PageClassification] = {}
        for window_index, segments in enumerate(window_segments):
            for segment in segments:
                for position, page_id in enumerate(segment.page_ids):
                    if page_id in verdicts:
                        continue
                    verdicts[page_id] = PageClassification(
                        page_id=page_id,
                        classification=DocumentClassification(
                            doc_type=segment.doc_class or UNCLASSIFIED_CLASS,
                            confidence=segment.confidence,
                            metadata={
                                "document_boundary": (
                                    "start" if position == 0 else "continue"
                                ),
                                "window_index": window_index,
                            },
                        ),
                    )

        uncovered = [pid for pid in sorted_page_ids if pid not in verdicts]
        if uncovered:
            # Not fatal: the pages that were classified still form sections, and
            # a page with no segment simply gets none. Worth a line, because a
            # model repeatedly skipping the tail of a window is a prompt problem
            # rather than a data problem.
            logger.warning(
                "%d page(s) were covered by no window's segments and will not "
                "appear in any section: %s",
                len(uncovered),
                uncovered[:20],
            )

        merged = self._fold_page_verdicts_to_segments(
            [verdicts[pid] for pid in sorted_page_ids if pid in verdicts]
        )
        logger.info(
            "Merged %d window(s) into %d segments",
            len(window_segments),
            len(merged),
        )
        return merged

    def _build_packet_content(
        self,
        task_prompt_template: str,
        paged_text: str,
        page_images: List[tuple[str, bytes]],
    ) -> List[Dict[str, Any]]:
        """Build the request content for one packet call.

        Same two-level structure as the page-level builders — split on
        ``{FEW_SHOT_EXAMPLES}``, then handle ``{DOCUMENT_IMAGE}`` within each
        part — so all three paths agree about both placeholders. Before packet
        granularity existed this path sent one text block and nothing else: no
        images, and ``{FEW_SHOT_EXAMPLES}`` left in the prompt as a literal
        string, which is the same defect the context path had.
        """
        parts = task_prompt_template.split("{FEW_SHOT_EXAMPLES}")

        if len(parts) != 2:
            return self._build_packet_content_segment(
                task_prompt_template, paged_text, page_images
            )

        content: List[Dict[str, Any]] = []
        content.extend(
            self._build_packet_content_segment(parts[0], paged_text, page_images)
        )
        content.extend(build_few_shot_examples_content(self.config))
        content.extend(
            self._build_packet_content_segment(parts[1], paged_text, page_images)
        )
        return content

    def _build_packet_content_segment(
        self,
        prompt_segment: str,
        paged_text: str,
        page_images: List[tuple[str, bytes]],
    ) -> List[Dict[str, Any]]:
        """Render one segment of a packet prompt, inserting the page images.

        Each image is preceded by its page number. The model is asked for page
        *ranges*, so it has to be able to say which image it is looking at —
        unlabelled images in a 20-page request leave it counting attachments,
        and a miscount shifts every boundary it reports.
        """
        substitutions = {
            "DOCUMENT_TEXT": paged_text,
            "CLASS_NAMES_AND_DESCRIPTIONS": self._format_classes_and_descriptions(),
            "BOUNDARY_HINTS": self._format_boundary_hints(),
            # The packet path uses the markdown-table variant of the optional
            # schema-attribute placeholder so it matches the surrounding class
            # table format.
            "CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS": (
                self._format_classes_and_attributes_table()
            ),
        }

        if "{DOCUMENT_IMAGE}" not in prompt_segment:
            rendered = self._prepare_prompt_from_template(
                prompt_segment, substitutions, required_placeholders=[]
            )
            return [{"text": rendered}] if rendered.strip() else []

        image_parts = prompt_segment.split("{DOCUMENT_IMAGE}")
        if len(image_parts) != 2:
            logger.warning(
                "Invalid DOCUMENT_IMAGE placeholder usage in the packet prompt "
                "(%d occurrences); appending page images after the text instead "
                "of at the placeholder.",
                len(image_parts) - 1,
            )
            rendered = self._prepare_prompt_from_template(
                prompt_segment.replace("{DOCUMENT_IMAGE}", ""),
                substitutions,
                required_placeholders=[],
            )
            content: List[Dict[str, Any]] = (
                [{"text": rendered}] if rendered.strip() else []
            )
            content.extend(self._packet_image_blocks(page_images))
            return content

        before_images = self._prepare_prompt_from_template(
            image_parts[0], substitutions, required_placeholders=[]
        )
        after_images = self._prepare_prompt_from_template(
            image_parts[1], substitutions, required_placeholders=[]
        )

        content = []
        if before_images.strip():
            content.append({"text": before_images})
        content.extend(self._packet_image_blocks(page_images))
        if after_images.strip():
            content.append({"text": after_images})
        return content

    @staticmethod
    def _packet_image_blocks(
        page_images: List[tuple[str, bytes]],
    ) -> List[Dict[str, Any]]:
        """Labelled image blocks, one pair of blocks per page."""
        content: List[Dict[str, Any]] = []
        for ordinal, (page_id, image_bytes) in enumerate(page_images, start=1):
            content.append(
                {"text": f"<page-number>{ordinal}</page-number> (document page {page_id})"}
            )
            content.append(image.prepare_bedrock_image_attachment(image_bytes))
        return content

    def _load_packet_page_images(
        self, document: Document, page_ids: List[str]
    ) -> List[tuple[str, bytes]]:
        """Load and resize the page images one packet call will carry.

        A page whose image cannot be loaded is omitted rather than failing the
        call: the text for that page is still in the request, and the remaining
        images still tell the model where the other documents start.
        """
        target_width = self.config.classification.image.target_width
        target_height = self.config.classification.image.target_height

        loaded: List[tuple[str, bytes]] = []
        for page_id in page_ids:
            page = document.pages.get(page_id)
            if page is None or not page.image_uri:
                continue
            try:
                loaded.append(
                    (
                        page_id,
                        image.prepare_image(
                            page.image_uri, target_width, target_height
                        ),
                    )
                )
            except Exception as e:
                logger.warning(f"Failed to load image for page {page_id}: {e}")

        if loaded and len(loaded) > image.BEDROCK_MANY_IMAGE_COUNT_THRESHOLD:
            logger.info(
                "This packet call carries %d images, above Bedrock's %d-image "
                "threshold, so each will be downscaled to %dpx. Reduce "
                "classification.packet.window_pages to keep full resolution.",
                len(loaded),
                image.BEDROCK_MANY_IMAGE_COUNT_THRESHOLD,
                image.BEDROCK_MANY_IMAGE_MAX_DIMENSION,
            )
        return loaded

    @staticmethod
    def _format_paged_text(
        pages_content: Dict[str, str], page_ids: List[str]
    ) -> str:
        """Render the packet's per-page text, each page labelled by its ordinal.

        The ordinal is what the model is asked to report, and it is **not** the
        page ID once windowing is on: window 2 of a 40-page packet starts at
        document page 19 but is shown that page as ordinal 1. The document page
        ID is given alongside so a reasoning trace naming either one is
        readable, but the prompt asks for the ordinal.
        """
        rendered = []
        for ordinal, page_id in enumerate(page_ids, start=1):
            text = pages_content.get(page_id, f"[No text content for page {page_id}]")
            rendered.append(
                f"<page-number>{ordinal}</page-number> "
                f"(document page {page_id})\n{text}\n"
            )
        return "\n".join(rendered)

    def _packet_segments(
        self,
        document: Document,
        stage: str = STAGE_SINGLE_PASS,
        forced_class: Optional[str] = None,
    ) -> tuple[List[Segment], Dict[str, Any], List[str]]:
        """Run every packet window and merge the result into one segment list.

        Shared by single-pass packet classification and by two-stage Stage A at
        packet granularity: the two differ only in which prompt and model the
        stage resolves and whether a class comes back, both of which are
        properties of ``stage``.

        Returns:
            ``(segments, metering, errors)``. ``segments`` is empty only when
            every window failed; ``errors`` names each window that did, so a
            partial result is still reported as partial rather than as a clean
            run over fewer pages.
        """
        pages_content, _ = self._format_pages_with_coverage(document)
        config = self._get_classification_config(stage)
        _, send_images = self._resolve_modality()

        sorted_page_ids = sorted(
            document.pages.keys(),
            key=lambda x: int(x) if x.isdigit() else float("inf"),
        )
        windows = self._packet_windows(sorted_page_ids)

        window_segments: List[List[Segment]] = []
        metering: Dict[str, Any] = {}
        errors: List[str] = []

        for window_index, window_page_ids in enumerate(windows, start=1):
            try:
                segments, window_metering = self._classify_one_packet_window(
                    document=document,
                    config=config,
                    pages_content=pages_content,
                    window_page_ids=window_page_ids,
                    window_label=f"{window_index}/{len(windows)}",
                    send_images=send_images,
                    forced_class=forced_class,
                )
            except Exception as e:
                # One window failing must not discard the others: on a long
                # packet that would turn a transient throttle into a total loss
                # of a 40-page document's boundaries.
                error_msg = (
                    f"Packet window {window_index}/{len(windows)} "
                    f"(pages {window_page_ids[0]}-{window_page_ids[-1]}) "
                    f"failed: {e}"
                )
                logger.error(error_msg)
                errors.append(error_msg)
                continue

            window_segments.append(segments)
            metering = (
                window_metering
                if not metering
                else utils.merge_metering_data(metering, window_metering)
            )

        if not window_segments:
            return [], metering, errors

        merged = self._merge_window_segments(window_segments, sorted_page_ids)
        return merged, metering, errors

    def packet_classify_document(
        self, document: Document, forced_class: Optional[str] = None
    ) -> Document:
        """Classify a document with one call per packet (or per window).

        The model is shown the whole packet and returns the segments directly,
        rather than being asked about each page in isolation and having the
        boundaries pieced together from per-page verdicts. Three things that
        were not previously possible:

        - **Images.** The packet call sent text only, so no setting produced
          packet-level classification that could see page layout. Every page's
          image is now attached, labelled with its ordinal, subject to
          ``modality.images``.
        - **Few-shot examples.** ``{FEW_SHOT_EXAMPLES}`` was never substituted
          on this path either, so a configured example reached the model as a
          literal placeholder string.
        - **Windowing.** A packet larger than the context window failed
          outright. It is now split into overlapping windows whose results are
          merged.

        Single-class configurations short-circuit the class decision here too,
        and honour ``sectionSplitting`` the same way — see
        ``_create_single_class_sections`` and GitHub issue #686.

        Args:
            document: Document object to classify
            forced_class: When set, every returned segment's type is overwritten
                with this value, so the model's contribution is the segment
                *ranges* only. Set by the document-name-regex path in
                ``classify_document`` (GitHub issue #705).

        Returns:
            Document: Updated Document object with classifications and sections
        """
        if not document.pages:
            logger.warning("Document has no pages to classify with the packet method")
            return self._update_document_status(
                document,
                success=False,
                error_message="Document has no pages to classify",
            )

        # Same as the page-level path: the class is predetermined, but the
        # section boundaries are not (GitHub issue #686). A packet call returns
        # segment ranges, which IS boundary detection, so llm_determined falls
        # through to it rather than being skipped.
        if self._can_skip_backend_for_single_class(document):
            logger.info(
                f"Only one document class '{self.single_class_name}' is defined "
                f"and sectionSplitting needs no boundary detection. Classifying "
                f"all pages as this class without calling backend."
            )
            document = self._create_single_class_sections(document)
            document = self._update_document_status(document)
            return document

        t0 = time.time()
        send_text, send_images = self._resolve_modality()
        logger.info(
            "Classifying document with %d pages using packet granularity "
            "(text=%s, images=%s)",
            len(document.pages),
            send_text,
            send_images,
        )

        try:
            no_text_error = self._packet_text_unavailable_error(
                document, self._format_pages_with_coverage(document)[1]
            )
            if no_text_error:
                logger.error(no_text_error)
                return self._update_document_status(
                    document, success=False, error_message=no_text_error
                )

            merged, metering, errors = self._packet_segments(
                document,
                stage=self.STAGE_SINGLE_PASS,
                forced_class=forced_class,
            )

            document.metering = utils.merge_metering_data(document.metering, metering)
            document.errors.extend(errors)

            if not merged:
                error_msg = "Packet classification produced no segments: " + (
                    "; ".join(errors) if errors else "the model returned none"
                )
                return self._update_document_status(
                    document, success=False, error_message=error_msg
                )

            # One section builder for both granularities — see
            # _sections_from_segments.
            document = self._sections_from_segments(document, merged)
            document = self._update_document_status(document)

            logger.info(
                "Document classified into %d sections using packet granularity "
                "in %.2f seconds",
                len(document.sections),
                time.time() - t0,
            )

        except Exception as e:
            error_msg = f"Error in packet classification: {str(e)}"
            document = self._update_document_status(
                document, success=False, error_message=error_msg
            )
            raise

        return document

    def _classify_one_packet_window(
        self,
        document: Document,
        config: Dict[str, Any],
        pages_content: Dict[str, str],
        window_page_ids: List[str],
        window_label: str,
        send_images: bool,
        forced_class: Optional[str],
    ) -> tuple[List[Segment], Dict[str, Any]]:
        """Run one packet call and parse its segments."""
        paged_text = self._format_paged_text(pages_content, window_page_ids)
        page_images = (
            self._load_packet_page_images(document, window_page_ids)
            if send_images
            else []
        )

        content = self._build_packet_content(
            config["task_prompt"], paged_text, page_images
        )

        logger.info(
            "Invoking Bedrock for packet window %s (pages %s-%s, %d image(s))",
            window_label,
            window_page_ids[0],
            window_page_ids[-1],
            len(page_images),
        )
        response_with_metering = self._invoke_bedrock_model(
            content=content, config=config
        )

        response = response_with_metering["response"]
        window_metering = response_with_metering.get("metering", {})

        # Defensive: handle the case where the LLM returns an empty content array
        content_array = response["output"]["message"].get("content", [])
        if not content_array:
            logger.error(
                "LLM returned empty content array in packet classification response",
                extra={"response": response},
            )
            raise ValueError(
                "Packet classification failed: LLM returned empty response"
            )

        # Reasoning models (Claude Sonnet 5 / 4.6+, extended thinking on) emit
        # reasoningContent block(s) before the answer text block, so content[0]
        # may not be the text. Concatenate all text blocks.
        classification_text = "".join(
            item["text"]
            for item in content_array
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )

        classification_json = extract_json_from_text(classification_text)
        classification_data = json.loads(classification_json)
        raw_segments = classification_data.get("segments", [])

        if not raw_segments:
            raise ValueError("No segments found in the classification result")

        if forced_class:
            # The document's NAME already asserts the class; the model was
            # invoked only for the segment RANGES (GitHub issue #705). Setting
            # the key unconditionally also rescues a segment whose type the
            # model omitted, which validation would otherwise drop along with
            # its boundary.
            for segment in raw_segments:
                if isinstance(segment, dict):
                    segment["type"] = forced_class

        segments = self._parse_model_segments(
            raw_segments,
            document,
            forced_class=forced_class,
            window_page_ids=window_page_ids,
            # Stage A is asked for ranges only — requiring a type here would
            # reject every segment it correctly returns.
            require_class=config.get("stage") != self.STAGE_SEGMENTATION,
        )
        if not segments:
            raise ValueError(
                "Every segment the model returned was unusable (see warnings above)"
            )
        return segments, window_metering

    def holistic_classify_document(
        self, document: Document, forced_class: Optional[str] = None
    ) -> Document:
        """Deprecated alias for :meth:`packet_classify_document`.

        Kept because it is a public method name that notebooks and external
        callers use. "Holistic" named one specific combination — one call per
        packet, text only — and the combination is now two independent settings,
        so the name no longer describes anything in the configuration.
        """
        return self.packet_classify_document(document, forced_class=forced_class)

    def _format_boundary_hints(self) -> str:
        """Render ``{BOUNDARY_HINTS}`` for two-stage Stage A.

        Empty under the default ``boundary_hints: none``, and that is the point:
        a boundary call carrying no class vocabulary at all is the prefix saving
        two-stage exists for. ``class_names`` sends the **names only** — not the
        descriptions and not the few-shot examples, which are the bulk of the
        prefix — for the case where a boundary is marked by a form number or
        title the class names happen to mention.
        """
        if (
            self.config.classification.segmentation.boundary_hints != "class_names"
            or not self.document_types
        ):
            return ""
        names = ", ".join(dt.type_name for dt in self.document_types)
        return (
            "<document-type-hints>\n"
            "These are the kinds of document this packet may contain. They are "
            "a hint about what a document's opening page may be titled; do NOT "
            "report a type, only where each document starts and ends.\n"
            f"{names}\n"
            "</document-type-hints>"
        )

    def two_stage_classify_document(
        self, document: Document, forced_class: Optional[str] = None
    ) -> Document:
        """Classify by finding boundaries first, then labeling each segment.

        Opt in with ``classification.strategy: two_stage``. Single-pass stays
        the default.

        A single-pass call does two different jobs at once, and pays for it in
        two ways. **Every boundary decision carries the whole class vocabulary**
        — the class list, every description and every few-shot example — even
        though finding a boundary does not depend on any of it; at page
        granularity that prefix is paid N times. And **labeling scales with
        pages rather than documents**: a 40-page statement is labeled forty
        times to produce one answer.

        Two-stage splits them. Stage A reuses the granularity, modality,
        windowing and context-page settings, with a prompt that asks only where
        documents start and returns segments with no class. Stage B makes one
        call per segment, carrying the vocabulary and the examples, over the
        pages ``labeling.pages`` selects.

        The two halves are metered separately, under ``Segmentation`` and
        ``Labeling``, which is what makes "is this actually cheaper on my
        documents?" answerable from the cost table rather than from arithmetic.
        It also separates a wrong boundary from a wrong label, which one
        section-level outcome cannot.

        Args:
            document: Document object to classify
            forced_class: When set, Stage B is **skipped entirely** — the class
                is already known from the document-name regex match, so there is
                nothing to label and the only question was where the documents
                start.

        Returns:
            Document: Updated Document object with classifications and sections
        """
        if not document.pages:
            logger.warning("Document has no pages to classify")
            return self._update_document_status(
                document,
                success=False,
                error_message="Document has no pages to classify",
            )

        # Identical reasoning to both single-pass paths: a predetermined class
        # still needs real boundaries unless sectionSplitting asks for none
        # (GitHub issues #686 and #705).
        if self._can_skip_backend_for_single_class(document):
            logger.info(
                f"Only one document class '{self.single_class_name}' is defined "
                f"and sectionSplitting needs no boundary detection. Classifying "
                f"all pages as this class without calling backend."
            )
            document = self._create_single_class_sections(document)
            return self._update_document_status(document)

        t0 = time.time()
        logger.info(
            "Classifying document with %d pages using two-stage classification "
            "(%s granularity)",
            len(document.pages),
            self.granularity,
        )

        try:
            segments, metering, errors = self._segment_document(document)
            document.metering = utils.merge_metering_data(document.metering, metering)
            document.errors.extend(errors)

            if not segments:
                error_msg = "Two-stage segmentation (Stage A) produced no segments: " + (
                    "; ".join(errors) if errors else "the model returned none"
                )
                return self._update_document_status(
                    document, success=False, error_message=error_msg
                )

            logger.info(
                "Stage A found %d segment(s) across %d pages",
                len(segments),
                len(document.pages),
            )

            if forced_class:
                # The document's NAME asserts the class, so there is nothing for
                # Stage B to decide. Skipping it is the whole saving on this
                # path: a name-matched 40-page packet costs its boundary calls
                # and no labeling calls at all.
                logger.info(
                    "Skipping Stage B: the document name regex already asserts "
                    "the class '%s' for every segment.",
                    forced_class,
                )
                for segment in segments:
                    segment.doc_class = forced_class
                    segment.confidence = None
            else:
                segments, label_metering, label_errors = self._label_segments(
                    document, segments
                )
                document.metering = utils.merge_metering_data(
                    document.metering, label_metering
                )
                document.errors.extend(label_errors)

            document = self._sections_from_segments(document, segments)
            document = self._update_document_status(document)

            logger.info(
                "Document classified into %d sections using two-stage "
                "classification in %.2f seconds",
                len(document.sections),
                time.time() - t0,
            )

        except Exception as e:
            error_msg = f"Error in two-stage classification: {str(e)}"
            document = self._update_document_status(
                document, success=False, error_message=error_msg
            )
            raise

        return document

    def _segment_document(
        self, document: Document
    ) -> tuple[List[Segment], Dict[str, Any], List[str]]:
        """Stage A: find where each document starts, with no class vocabulary.

        Honours ``granularity``: a packet-granularity Stage A is one call (or
        one per window) asking for page ranges, and a page-granularity Stage A
        is one call per page asking only ``start`` or ``continue``. The page
        form still makes N calls, but each one drops the class list, the
        descriptions and the few-shot examples, which is the dominant per-call
        cost at that granularity.
        """
        if self.granularity == self.GRANULARITY_PACKET:
            no_text_error = self._packet_text_unavailable_error(
                document, self._format_pages_with_coverage(document)[1]
            )
            if no_text_error:
                return [], {}, [no_text_error]
            return self._packet_segments(document, stage=self.STAGE_SEGMENTATION)
        return self._segment_pages(document)

    def _segment_pages(
        self, document: Document
    ) -> tuple[List[Segment], Dict[str, Any], List[str]]:
        """Stage A at page granularity: one boundary-only call per page."""
        config = self._get_classification_config(self.STAGE_SEGMENTATION)
        send_text, send_images = self._resolve_modality()
        context_size = self.config.classification.contextPagesCount

        page_content_cache = self._preload_page_content(document, context_size)
        sorted_page_ids = sorted(
            document.pages.keys(),
            key=lambda x: int(x) if x.isdigit() else float("inf"),
        )

        verdicts: Dict[str, PageClassification] = {}
        metering: Dict[str, Any] = {}
        errors: List[str] = []
        lock = threading.Lock()

        def boundary_for(page_id: str) -> None:
            page = document.pages[page_id]
            text_content = None
            if page.parsed_text_uri and send_text:
                try:
                    text_content = s3.get_text_content(page.parsed_text_uri)
                except Exception as e:
                    logger.warning(
                        f"Failed to load text content for page {page_id}: {e}"
                    )
            image_content = None
            if page.image_uri and send_images:
                try:
                    image_content = image.prepare_image(
                        page.image_uri,
                        self.config.classification.image.target_width,
                        self.config.classification.image.target_height,
                    )
                except Exception as e:
                    logger.warning(f"Failed to load image for page {page_id}: {e}")

            if context_size > 0:
                context = self._get_context_for_page(
                    page_id, sorted_page_ids, page_content_cache, context_size
                )
                content = self._build_context_content(
                    task_prompt_template=config["task_prompt"],
                    document_text=self._build_text_with_context(
                        current_text=text_content or "",
                        before_texts=context["before_texts"],
                        after_texts=context["after_texts"],
                    ),
                    image_content=image_content,
                    before_images=context["before_images"],
                    after_images=context["after_images"],
                )
            else:
                content = self._build_content(
                    config["task_prompt"],
                    text_content or "",
                    # Stage A sends no vocabulary unless boundary_hints asks
                    # for the names, so the class listing is deliberately
                    # empty here. A Stage A prompt that references
                    # {CLASS_NAMES_AND_DESCRIPTIONS} anyway gets nothing
                    # rather than silently reintroducing the prefix.
                    "",
                    image_content,
                )

            result = self._invoke_bedrock_model(content=content, config=config)
            response = result["response"]
            content_array = response["output"]["message"].get("content", [])
            text = "".join(
                item["text"]
                for item in content_array
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            )
            data, _ = extract_structured_data_from_text(text)
            boundary = "continue"
            reason = None
            if isinstance(data, dict):
                boundary = str(data.get("document_boundary", "continue")).lower()
                reason = data.get("boundary_reason")

            with lock:
                verdicts[page_id] = PageClassification(
                    page_id=page_id,
                    # Stage A reports no class. The placeholder is not a
                    # classification: Stage B overwrites it, and a segment whose
                    # labeling fails keeps it so the failure is visible rather
                    # than appearing as a confident class.
                    classification=DocumentClassification(
                        doc_type=UNCLASSIFIED_CLASS,
                        confidence=None,
                        metadata={"document_boundary": boundary},
                    ),
                )
                if reason:
                    document.pages[page_id].classification_reason = str(reason)
                document.pages[page_id].document_boundary = boundary
                metering.update(
                    utils.merge_metering_data(metering, result.get("metering", {}))
                )

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(boundary_for, page_id): page_id
                for page_id in sorted_page_ids
            }
            for future in as_completed(futures):
                page_id = futures[future]
                try:
                    future.result()
                except Exception as e:
                    error_msg = f"Stage A failed for page {page_id}: {e}"
                    logger.error(error_msg)
                    with lock:
                        errors.append(error_msg)

        if not verdicts:
            return [], metering, errors

        segments = self._fold_page_verdicts_to_segments(
            [verdicts[pid] for pid in sorted_page_ids if pid in verdicts]
        )
        # The fold carries each page's class through, which for Stage A is the
        # unclassified placeholder. Clear it so the segments really are
        # unlabeled: a Segment with doc_class set is one Stage B would be
        # entitled to leave alone.
        for segment in segments:
            segment.doc_class = None
            segment.confidence = None
        return segments, metering, errors

    def _label_segments(
        self, document: Document, segments: List[Segment]
    ) -> tuple[List[Segment], Dict[str, Any], List[str]]:
        """Stage B: one call per segment to decide its class.

        **Per segment, not per page** — the other half of the two-stage saving.
        Which pages represent the segment is ``labeling.pages``; the default
        ``first_k`` sends the opening two, because a document's identity is
        almost always on its opening pages while ``all`` makes labeling a long
        segment cost what page-level classification cost to classify it.

        A segment whose labeling call fails keeps ``doc_class = None`` and is
        recorded in the returned errors. It is **not** given a fallback class:
        the segment's boundaries are still good, and an invented class would
        propagate into extraction as though it had been decided.
        """
        config = self._get_classification_config(self.STAGE_LABELING)
        _, send_images = self._resolve_modality()
        pages_content, _ = self._format_pages_with_coverage(document)

        metering: Dict[str, Any] = {}
        errors: List[str] = []
        lock = threading.Lock()

        def label(index: int) -> None:
            segment = segments[index]
            page_ids = self._labeling_pages(segment)
            paged_text = self._format_paged_text(pages_content, page_ids)
            page_images = (
                self._load_packet_page_images(document, page_ids)
                if send_images
                else []
            )
            content = self._build_packet_content(
                config["task_prompt"], paged_text, page_images
            )

            doc_type, confidence, candidates, reason, call_metering = (
                self._invoke_with_class_validation(
                    content=content,
                    config=config,
                    label=f"segment {index + 1} (pages {segment.start_page}-{segment.end_page})",
                )
            )

            with lock:
                segment.doc_class = doc_type
                segment.confidence = confidence
                metering.update(
                    utils.merge_metering_data(metering, call_metering)
                )
                for page_id in segment.page_ids:
                    if page_id not in document.pages:
                        continue
                    if reason:
                        document.pages[page_id].classification_reason = str(reason)
                    if candidates:
                        document.pages[page_id].classification_candidates = candidates

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(label, index): index for index in range(len(segments))
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    future.result()
                except Exception as e:
                    segment = segments[index]
                    error_msg = (
                        f"Stage B failed for segment {index + 1} "
                        f"(pages {segment.start_page}-{segment.end_page}): {e}"
                    )
                    logger.error(error_msg)
                    with lock:
                        errors.append(error_msg)

        logger.info(
            "Stage B labeled %d/%d segment(s)",
            sum(1 for s in segments if s.doc_class),
            len(segments),
        )
        return segments, metering, errors

    def _labeling_pages(self, segment: Segment) -> List[str]:
        """Which of a segment's pages Stage B sends, per ``labeling.pages``."""
        labeling = self.config.classification.labeling
        if labeling.pages == "first":
            return segment.page_ids[:1]
        if labeling.pages == "all":
            return list(segment.page_ids)
        return segment.page_ids[: max(1, labeling.k)]

    def _invoke_with_class_validation(
        self,
        content: List[Dict[str, Any]],
        config: Dict[str, Any],
        label: str,
    ) -> tuple[
        str, Optional[float], List[Dict[str, Any]], Optional[str], Dict[str, Any]
    ]:
        """Invoke one labeling call, re-prompting on an out-of-vocabulary class.

        Same contract as the page-level validation loop — ``enforceValidClasses``,
        ``maxValidationRetries`` and ``invalidClassFallback`` all apply, because
        a class outside the vocabulary is exactly as wrong whether a page or a
        segment produced it.
        """
        enforce = self.config.classification.enforceValidClasses
        max_retries = self.config.classification.maxValidationRetries if enforce else 0

        attempt_content = content
        metering: Dict[str, Any] = {}
        doc_type = ""
        confidence: Optional[float] = None
        candidates: List[Dict[str, Any]] = []
        reason: Optional[str] = None
        validation_error: Optional[str] = None

        for attempt in range(max_retries + 1):
            result = self._invoke_bedrock_model(
                content=attempt_content, config=config
            )
            metering = (
                result.get("metering", {})
                if not metering
                else utils.merge_metering_data(metering, result.get("metering", {}))
            )

            content_array = result["response"]["output"]["message"].get("content", [])
            if not content_array:
                raise ValueError(
                    f"Labeling failed for {label}: LLM returned empty response"
                )
            text = "".join(
                item["text"]
                for item in content_array
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            )

            # Reset per attempt so a retry's values never leak from the
            # rejected attempt: the confidence reported for a class we threw
            # away does not apply to the new one.
            confidence = None
            candidates = []
            reason = None

            data, _ = extract_structured_data_from_text(text)
            if isinstance(data, dict):
                doc_type = str(data.get("class", "") or "")
                reason = data.get("classification_reason")
                confidence, candidates = resolve_class_and_confidence(
                    reported_class=doc_type,
                    reported_confidence=data.get("confidence"),
                    reported_candidates=data.get("candidates"),
                    valid_classes=self.valid_doc_types,
                )
            else:
                doc_type = ""

            if not enforce or doc_type in self.valid_doc_types:
                return doc_type, confidence, candidates, reason, metering

            validation_error = (
                f"Model returned invalid class '{doc_type or '(empty)'}' for {label}"
            )
            if attempt < max_retries:
                logger.warning(
                    "%s (attempt %d/%d); re-prompting with valid classes.",
                    validation_error,
                    attempt + 1,
                    max_retries + 1,
                )
                attempt_content = content + [
                    {
                        "text": (
                            f"Your previous answer used the type "
                            f"'{doc_type or '(empty)'}', which is not one of the "
                            f"valid types. Choose exactly one of: "
                            f"{', '.join(sorted(self.valid_doc_types))}. Reply "
                            f"with the same JSON format."
                        )
                    }
                ]

        fallback = self.config.classification.invalidClassFallback
        logger.error(
            "%s after %d attempt(s); assigned fallback '%s'.",
            validation_error,
            max_retries + 1,
            fallback,
        )
        # The fallback is not a prediction, so it carries no score.
        return fallback, None, [], reason, metering
