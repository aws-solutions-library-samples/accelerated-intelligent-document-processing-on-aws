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
from idp_common.classification.models import (
    ClassificationResult,
    DocumentClassification,
    DocumentSection,
    DocumentType,
    PageClassification,
)
from idp_common.config.models import IDPConfig
from idp_common.config.schema_constants import (
    REF_FIELD,
    SCHEMA_ITEMS,
    SCHEMA_PROPERTIES,
    SCHEMA_TYPE,
    TYPE_ARRAY,
    TYPE_OBJECT,
    X_AWS_IDP_CLASSIFICATION,
    X_AWS_IDP_DOCUMENT_NAME_REGEX,
    X_AWS_IDP_DOCUMENT_TYPE,
    X_AWS_IDP_EXCLUDE_FROM_PROCESSING,
    X_AWS_IDP_EXCLUSION_REASON,
    X_AWS_IDP_PAGE_CONTENT_REGEX,
)
from idp_common.config.schema_utils import deref_schema
from idp_common.models import Document, Section, Status
from idp_common.utils import extract_json_from_text, extract_structured_data_from_text
from idp_common.utils.few_shot_example_builder import build_few_shot_examples_content

logger = logging.getLogger(__name__)


@dataclass
class PageContextData:
    """Data class for page context information."""

    page_id: str
    text_content: Optional[str] = None
    image_content: Optional[bytes] = None


class ClassificationService:
    """Service for classifying documents using various backends."""

    # Configuration for the SageMaker retry mechanism
    MAX_RETRIES = 7
    INITIAL_BACKOFF = 2  # seconds
    MAX_BACKOFF = 300  # 5 minutes

    # Classification method options
    MULTIMODAL_PAGE_LEVEL = "multimodalPageLevelClassification"
    TEXTBASED_HOLISTIC = "textbasedHolisticClassification"

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

        # Get classification method from typed config
        self.classification_method = self.config.classification.classificationMethod

        # Get max pages for classification (1 to ALL)
        self.max_pages_for_classification = (
            self.config.classification.maxPagesForClassification
        )

        # Log classification method
        if self.classification_method == self.TEXTBASED_HOLISTIC:
            logger.info("Using textbased holistic packet classification method")
        else:
            # Default to multimodal page-level classification if value is invalid
            if self.classification_method != self.MULTIMODAL_PAGE_LEVEL:
                logger.warning(
                    f"Invalid classification method '{self.classification_method}', falling back to '{self.MULTIMODAL_PAGE_LEVEL}'"
                )
                self.classification_method = self.MULTIMODAL_PAGE_LEVEL
            logger.info(
                "Using multimodal page-level classification method with document boundary detection"
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
                    type_name="unclassified",
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

        # Apply to all pages in original document
        for page_id, page in original_document.pages.items():
            page.classification = primary_classification
            page.confidence = 1.0  # High confidence for applied classification

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

        Args:
            document: Document with pages to load
            context_size: Number of context pages (determines if images should be loaded)

        Returns:
            Dictionary mapping page_id to PageContextData with loaded content
        """
        page_content_cache: Dict[str, PageContextData] = {}

        # Type-safe access to image config
        target_width = self.config.classification.image.target_width
        target_height = self.config.classification.image.target_height

        for page_id, page in document.pages.items():
            text_content = None
            image_content = None

            # Load text content
            if page.parsed_text_uri:
                try:
                    text_content = s3.get_text_content(page.parsed_text_uri)
                except Exception as e:
                    logger.warning(
                        f"Failed to load text content for page {page_id}: {e}"
                    )

            # Load image content
            if page.image_uri:
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
            f"Pre-loaded content for {len(page_content_cache)} pages for context-aware classification"
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

    def _classify_pages_multimodal(self, document: Document) -> Document:
        """
        Classify pages using multimodal page-level classification.
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
                    document.pages[
                        page_id
                    ].classification = cached_result.classification.doc_type
                    document.pages[
                        page_id
                    ].confidence = cached_result.classification.confidence

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
                            all_page_results.append(page_result)

                            # Check if there was an error in the classification
                            if "error" in page_result.classification.metadata:
                                with errors_lock:
                                    error_msg = f"Error classifying page {page_id}: {page_result.classification.metadata['error']}"
                                    document.errors.append(error_msg)

                            # Update the page in the document
                            document.pages[
                                page_id
                            ].classification = page_result.classification.doc_type
                            document.pages[
                                page_id
                            ].confidence = page_result.classification.confidence

                            # Copy metadata (including boundary information) to the page
                            setattr(
                                document.pages[page_id],
                                "metadata",
                                page_result.classification.metadata,
                            )

                            # Persist the boundary signal on a DECLARED field.
                            # The setattr above stashes the whole metadata dict on
                            # an attribute that is not a dataclass field, so it is
                            # absent from Document.to_dict and never survives the
                            # Step Functions hop or reaches DynamoDB — which is why
                            # an unexpected section merge could only be diagnosed
                            # from Lambda logs (GitHub #565). Copy the one value
                            # that needs to outlive this invocation.
                            boundary = page_result.classification.metadata.get(
                                "document_boundary"
                            )
                            if boundary:
                                document.pages[page_id].document_boundary = str(
                                    boundary
                                ).lower()

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

    def _get_classification_config(self) -> Dict[str, Any]:
        """
        Get and validate the classification configuration.

        Returns:
            Dict with validated classification configuration parameters

        Raises:
            ValueError: If required configuration values are missing
        """
        # Type-safe access to classification config (no .get() needed!)
        config = {
            "model_id": self.bedrock_model,
            "temperature": self.config.classification.temperature,
            "top_k": self.config.classification.top_k,
            "top_p": self.config.classification.top_p,
            "max_tokens": self.config.classification.max_tokens,
            "reasoning_effort": self.config.classification.reasoning_effort,
        }

        # Validate system prompt
        system_prompt = self.config.classification.system_prompt
        if not system_prompt:
            raise ValueError("No system_prompt found in classification configuration")
        config["system_prompt"] = system_prompt

        # Validate task prompt
        task_prompt = self.config.classification.task_prompt
        if not task_prompt:
            raise ValueError("No task_prompt found in classification configuration")
        config["task_prompt"] = task_prompt

        return config

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
        before_texts: Optional[List[str]] = None,
        after_texts: Optional[List[str]] = None,
        before_images: Optional[List[bytes]] = None,
        after_images: Optional[List[bytes]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Build content array with support for optional FEW_SHOT_EXAMPLES and DOCUMENT_IMAGE placeholders.

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

        # Load text content from URI
        if text_uri:
            try:
                text_content = s3.get_text_content(text_uri)
            except Exception as e:
                logger.warning(f"Failed to load text content from {text_uri}: {e}")
                # Continue without text content

        # Load image content from URI with configurable dimensions
        if image_uri:
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

            # Build content array - start with text prompt
            task_prompt = self._prepare_prompt_from_template(
                config["task_prompt"],
                self._build_classification_substitutions(
                    document_text, self._format_classes_list()
                ),
                required_placeholders=[],
            )

            # Check if prompt uses DOCUMENT_IMAGE placeholder
            if "{DOCUMENT_IMAGE}" in config["task_prompt"]:
                # Split around image placeholder and insert context images
                parts = task_prompt.split("{DOCUMENT_IMAGE}")
                if len(parts) == 2:
                    content = []
                    if parts[0].strip():
                        content.append({"text": parts[0]})

                    # Add images with context
                    content.extend(
                        self._build_images_with_context(
                            current_image=image_content,
                            before_images=before_images or [],
                            after_images=after_images or [],
                        )
                    )

                    if parts[1].strip():
                        content.append({"text": parts[1]})
                else:
                    # Fallback - add text then images
                    content = [{"text": task_prompt}]
                    content.extend(
                        self._build_images_with_context(
                            current_image=image_content,
                            before_images=before_images or [],
                            after_images=after_images or [],
                        )
                    )
            else:
                # No image placeholder - just use text
                content = [{"text": task_prompt}]
                # If images exist, append them with context
                if image_content or before_images or after_images:
                    content.extend(
                        self._build_images_with_context(
                            current_image=image_content,
                            before_images=before_images or [],
                            after_images=after_images or [],
                        )
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
                try:
                    classification_data, detected_format = (
                        extract_structured_data_from_text(classification_text)
                    )
                    if isinstance(classification_data, dict):
                        doc_type = classification_data.get("class", "")
                        document_boundary = classification_data.get(
                            "document_boundary", "continue"
                        )
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
                        doc_type = "unclassified"
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

            return PageClassification(
                page_id=page_id,
                classification=DocumentClassification(
                    doc_type=doc_type,
                    confidence=1.0,  # Default confidence
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
                doc_type = response_body.get("prediction", "unclassified")

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
                        confidence=1.0,  # Default confidence since SageMaker doesn't provide it
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
            context="Classification",
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
    ) -> PageClassification:
        """
        Create a standard unclassified result with error information.

        Args:
            page_id: ID of the page
            image_uri: Optional URI of the image
            text_uri: Optional URI of the text content
            raw_text_uri: Optional URI of the raw text
            error_message: Error message to include in metadata

        Returns:
            PageClassification with unclassified result
        """
        return PageClassification(
            page_id=page_id,
            classification=DocumentClassification(
                doc_type="unclassified",
                confidence=0.0,
                metadata={"error": error_message},
            ),
            image_uri=image_uri,
            text_uri=text_uri,
            raw_text_uri=raw_text_uri,
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
                                confidence=page_data["classification"]["confidence"],
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
        ``llm_determined`` degrades to ``disabled`` with a warning, because
        boundary detection requires the per-page inference call the
        short-circuit exists to avoid. See ``_create_single_class_sections``
        and GitHub issue #686.

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

        # Check for document name regex match (single-class configurations only)
        regex_matched_class = self._check_document_name_regex(document)
        if regex_matched_class:
            logger.info(
                f"Classifying all pages as '{regex_matched_class}' based on document name regex match. Skipping LLM classification."
            )

            # Set all pages to the regex-matched class
            for page_id, page in document.pages.items():
                page.classification = regex_matched_class
                page.confidence = 1.0

            # Create a single section containing all pages
            page_ids = list(document.pages.keys())
            section = self._create_section(
                section_id="1",
                doc_type=regex_matched_class,
                pages=page_ids,
                confidence=1.0,
            )

            if isinstance(section, Section):
                document.sections = [section]
            else:
                document.sections = [
                    Section(
                        section_id=section.section_id,
                        classification=section.classification.doc_type,
                        page_ids=[page.page_id for page in section.pages],
                    )
                ]

            # Update document status
            document = self._update_document_status(document)

            return document

        # If there's only one document class defined, automatically classify all pages as that class
        # without calling any backend service
        if self.has_single_class:
            logger.info(
                f"Only one document class '{self.single_class_name}' is defined. Automatically classifying all pages as this class without calling backend."
            )

            # The class decision is predetermined, but section boundaries are
            # not - honor sectionSplitting rather than hard-coding one
            # all-pages section (GitHub issue #686).
            document = self._create_single_class_sections(document)

            # Update document status
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
                # Classify the limited document
                if self.classification_method == self.TEXTBASED_HOLISTIC:
                    logger.info(
                        f"Classifying limited document with {len(limited_document.pages)} pages using holistic packet method"
                    )
                    classified_limited = self.holistic_classify_document(
                        limited_document
                    )
                else:
                    classified_limited = self._classify_pages_multimodal(
                        limited_document
                    )

                # Apply results to all pages in original document
                document = self._apply_limited_classification_to_all_pages(
                    document, classified_limited
                )
                return document

        # Use the appropriate classification method based on configuration
        if self.classification_method == self.TEXTBASED_HOLISTIC:
            logger.info(
                f"Classifying document with {len(document.pages)} pages using holistic packet method"
            )
            return self.holistic_classify_document(document)

        return self._classify_pages_multimodal(document)

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
        strategy = self.config.classification.sectionSplitting.lower()

        if strategy == "disabled":
            return self._create_single_section(document, page_results)
        elif strategy == "page":
            return self._create_per_page_sections(document, page_results)
        else:  # llm_determined (default)
            return self._create_llm_determined_sections(document, page_results)

    def _create_single_class_sections(self, document: Document) -> Document:
        """
        Assign every page to the sole configured class and split into sections
        according to the configured ``sectionSplitting`` strategy.

        This is the "single-class short-circuit": when the configuration defines
        exactly one document class the *class decision* is predetermined, so no
        backend call is needed to make it. Boundary detection is a separate
        question, though, and used to be skipped along with it — every packet
        collapsed into one section spanning all pages regardless of
        ``sectionSplitting`` (GitHub issue #686). For a "one record type per
        packet" configuration that silently merged every record in the packet
        into a single section, and extraction then returned only the first one.

        Strategy handling:

        - ``disabled`` — one section over all pages. Unchanged.
        - ``page`` — one section per page. This is the documented escape hatch
          for multi-record packets and now actually works.
        - ``llm_determined`` — **cannot** be honoured here. Boundaries come from
          the model's per-page ``document_boundary`` signal, which only exists if
          we make a per-page inference call — precisely the cost this
          short-circuit exists to avoid. Rather than silently reintroducing that
          cost for every single-class deployment, we degrade to ``disabled`` and
          log an actionable warning naming the config key and the alternatives.

        Args:
            document: Document whose pages should all be classified as the single
                configured class

        Returns:
            Document with pages classified and sections created per strategy
        """
        class_name = self.single_class_name or "undefined"

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

        strategy = self.config.classification.sectionSplitting.lower()

        if strategy == "page":
            return self._create_per_page_sections(document, page_results)

        if strategy != "disabled":
            # llm_determined (the config default) — see the docstring above.
            logger.warning(
                f"sectionSplitting='{strategy}' cannot be honored for the "
                f"single-class configuration '{class_name}': boundary detection "
                f"needs the model's per-page document_boundary signal, and the "
                f"single-class path makes no backend call. Degrading to "
                f"'disabled' — one section spanning all {len(document.pages)} "
                f"pages. If a packet can hold several separate '{class_name}' "
                f"documents, set classification.sectionSplitting: page for one "
                f"section per page, or define a second document class so full "
                f"classification (and real boundary detection) runs. "
                f"See GitHub issue #686."
            )

        return self._create_single_section(document, page_results)

    def _create_single_section(
        self, document: Document, page_results: List[PageClassification]
    ) -> Document:
        """
        Create single section containing all pages using majority voting.

        Uses voting/mode strategy to determine the document classification:
        - Counts occurrences of each classification across all pages
        - Selects the most common classification (mode)
        - If tied, uses the first page's classification for determinism
        - Excludes unclassifiable/blank pages from voting to prevent them
          from dominating when they complete processing first

        This addresses GitHub Issue #167 where blank pages could incorrectly
        determine the document classification.

        Args:
            document: Document to update
            page_results: List of page classification results

        Returns:
            Document with single section containing all pages
        """
        from collections import Counter

        if not page_results:
            return document

        # Sort results by page ID for consistent ordering
        sorted_results = self._sort_page_results(page_results)

        # Only include classifications that match valid document types from config
        # This automatically excludes any classification not defined in the config:
        # - blank pages (unclassifiable_blank_page, blank, etc.)
        # - errors (error (backoff/retry), unclassified)
        # - LLM hallucinations or typos
        votable_classifications = [
            r.classification.doc_type
            for r in sorted_results
            if r.classification.doc_type in self.valid_doc_types
        ]

        if votable_classifications:
            # Use voting: most common classification wins
            classification_counts = Counter(votable_classifications)
            most_common = classification_counts.most_common()

            # Check for ties - if tied, use the classification from the earliest page
            top_count = most_common[0][1]
            tied_classes = [cls for cls, count in most_common if count == top_count]

            if len(tied_classes) > 1:
                # Tie-breaker: use first occurrence in page order
                for result in sorted_results:
                    if result.classification.doc_type in tied_classes:
                        first_classification = result.classification.doc_type
                        logger.info(
                            f"Classification tie detected ({tied_classes}), using first page's class: '{first_classification}'"
                        )
                        break
                else:
                    first_classification = most_common[0][0]
            else:
                first_classification = most_common[0][0]

            logger.info(
                f"Classification voting results: {dict(classification_counts)} -> selected '{first_classification}'"
            )
        else:
            # All pages are unclassifiable, use first page's classification
            first_classification = sorted_results[0].classification.doc_type
            logger.warning(
                f"All pages are unclassifiable types, using first page's class: '{first_classification}'"
            )

        # Set all pages to this classification
        for page_id in document.pages:
            document.pages[page_id].classification = first_classification
            document.pages[page_id].confidence = 1.0

        # Create single section with all pages
        section = Section(
            section_id="1",
            classification=first_classification,
            confidence=1.0,
            page_ids=list(document.pages.keys()),
        )
        document.sections = [section]

        logger.info(
            f"Created single section with {len(document.pages)} pages, class='{first_classification}' (sectionSplitting=disabled)"
        )
        return document

    def _create_per_page_sections(
        self, document: Document, page_results: List[PageClassification]
    ) -> Document:
        """
        Create one section per page, preventing any joining of same-type documents.

        Args:
            document: Document to update
            page_results: List of page classification results

        Returns:
            Document with one section per page
        """
        document.sections = []

        sorted_results = self._sort_page_results(page_results)

        for idx, page_result in enumerate(sorted_results, start=1):
            page_id = page_result.page_id
            doc_type = page_result.classification.doc_type

            # Update page classification
            if page_id in document.pages:
                document.pages[page_id].classification = doc_type
                document.pages[
                    page_id
                ].confidence = page_result.classification.confidence

            # Create individual section for this page
            section = Section(
                section_id=str(idx),
                classification=doc_type,
                confidence=page_result.classification.confidence,
                page_ids=[page_id],
            )
            document.sections.append(section)

        logger.info(
            f"Created {len(document.sections)} sections (one per page) with sectionSplitting=page"
        )
        return document

    def _create_llm_determined_sections(
        self, document: Document, page_results: List[PageClassification]
    ) -> Document:
        """
        Create sections using LLM boundary detection (current default behavior).

        Uses document_boundary metadata ("start" or "continue") to determine
        section boundaries. This is the BIO-like tagging approach.

        Args:
            document: Document to update
            page_results: List of page classification results

        Returns:
            Document with sections created using LLM boundary detection
        """
        document.sections = []
        sorted_results = self._sort_page_results(page_results)

        if not sorted_results:
            return document

        # The per-page document_boundary signal drives every merge decision
        # below, but it is persisted nowhere: the DynamoDB page record and the
        # S3 document.json page dict both carry `Class` only. Emit the whole map
        # as one line so "why did these two documents end up in one section?"
        # can be answered without correlating N interleaved per-page
        # classification logs from the thread pool.
        #
        # An absent key is reported distinctly from a literal "continue",
        # because the two have very different diagnoses: the model omitting the
        # field (we default to "continue" below and at :1681) merges pages by
        # accident, whereas an explicit "continue" is the model's judgement.
        # That distinction is the one that was expensive to recover in #565.
        boundary_map = {
            r.page_id: (
                str(r.classification.metadata["document_boundary"]).lower()
                if "document_boundary" in r.classification.metadata
                else "(absent)"
            )
            for r in sorted_results
        }
        logger.info(f"Page document_boundary signals: {boundary_map}")

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
                # Create section with current group
                section = self._create_section(
                    section_id=str(current_group),
                    doc_type=current_type,
                    pages=[p.page_id for p in current_pages],
                )
                if isinstance(section, Section):
                    document.sections.append(section)
                else:
                    document.sections.append(
                        Section(
                            section_id=section.section_id,
                            classification=section.classification.doc_type,
                            page_ids=[page.page_id for page in section.pages],
                        )
                    )

                # Start new group
                current_group += 1
                current_type = result.classification.doc_type
                current_pages = [result]

        # Add final section
        section = self._create_section(
            section_id=str(current_group),
            doc_type=current_type,
            pages=[p.page_id for p in current_pages],
        )
        if isinstance(section, Section):
            document.sections.append(section)
        else:
            document.sections.append(
                Section(
                    section_id=section.section_id,
                    classification=section.classification.doc_type,
                    page_ids=[page.page_id for page in section.pages],
                )
            )

        logger.info(
            f"Created {len(document.sections)} sections using LLM boundary detection (sectionSplitting=llm_determined)"
        )
        return document

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
        self, section_id: str, doc_type: str, pages: List[Any], confidence: float = 1.0
    ) -> Union[DocumentSection, Section]:
        """
        Create a document section based on the input type.

        Args:
            section_id: ID for the section
            doc_type: Document type for the section
            pages: List of pages (either PageClassification or page_ids)
            confidence: Confidence score for the classification

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
        pages_content = {}

        for page_id, page in document.pages.items():
            # Fetch page text content from S3 if available
            if page.parsed_text_uri:
                try:
                    pages_content[page_id] = s3.get_text_content(page.parsed_text_uri)
                except Exception as e:
                    logger.warning(
                        f"Failed to load text content from {page.parsed_text_uri}: {e}"
                    )
                    # Continue with empty content
                    pages_content[page_id] = f"[Error loading page {page_id} content]"
            else:
                # Page has no text content
                pages_content[page_id] = f"[No text content for page {page_id}]"

        return pages_content

    def holistic_classify_document(self, document: Document) -> Document:
        """
        Classify a document using holistic packet classification.

        This method uses an LLM to analyze the entire document and identify page ranges
        that belong to specific document types. Unlike page-by-page classification,
        this method can handle documents where individual pages might not be clearly
        classifiable on their own.

        Single-class configurations short-circuit the class decision here too,
        and honor ``sectionSplitting`` the same way — see
        ``_create_single_class_sections`` and GitHub issue #686.

        Args:
            document: Document object to classify

        Returns:
            Document: Updated Document object with classifications and sections
        """
        if not document.pages:
            logger.warning("Document has no pages to classify with holistic method")
            return self._update_document_status(
                document,
                success=False,
                error_message="Document has no pages to classify",
            )

        # If there's only one document class defined, automatically classify all pages as that class
        # without calling any backend service
        if self.has_single_class:
            logger.info(
                f"Only one document class '{self.single_class_name}' is defined. Automatically classifying all pages as this class without calling backend."
            )

            # Same as the page-level path: the class is predetermined, the
            # section boundaries are not (GitHub issue #686).
            document = self._create_single_class_sections(document)

            # Update document status
            document = self._update_document_status(document)

            return document

        t0 = time.time()
        logger.info(
            f"Classifying document with {len(document.pages)} pages using holistic packet method"
        )

        try:
            # Format document pages as text
            pages_content = self._format_pages(document)

            # Get classification configuration
            config = self._get_classification_config()

            # Prepare paged document text
            doc_text = ""
            for page_id, page_text in sorted(
                pages_content.items(),
                key=lambda x: int(x[0]) if x[0].isdigit() else float("inf"),
            ):
                doc_text += f"<page-number>{page_id}</page-number>\n{page_text}\n\n"

            # Prepare document classes and descriptions as a table
            classes_table = self._format_classes_and_descriptions()

            # Prepare prompt using common function. The holistic path uses
            # the markdown-table variant of the optional schema-attribute
            # placeholder so it matches the surrounding class table format.
            prepared_prompt = self._prepare_prompt_from_template(
                config["task_prompt"],
                {
                    "DOCUMENT_TEXT": doc_text,
                    "CLASS_NAMES_AND_DESCRIPTIONS": classes_table,
                    "CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS": (
                        self._format_classes_and_attributes_table()
                    ),
                },
                required_placeholders=[],
            )

            # Invoke Bedrock to get the holistic classification
            logger.info("Invoking Bedrock for holistic packet classification")

            response_with_metering = self._invoke_bedrock_model(
                content=[{"text": prepared_prompt}], config=config
            )

            t1 = time.time()
            logger.info(
                f"Time taken for holistic classification: {t1 - t0:.2f} seconds"
            )

            response = response_with_metering["response"]
            metering = response_with_metering["metering"]

            # Extract classification result
            # Defensive: Handle case where LLM returns empty content array
            content_array = response["output"]["message"].get("content", [])
            if not content_array or len(content_array) == 0:
                logger.error(
                    "LLM returned empty content array in holistic classification response",
                    extra={"response": response},
                )
                raise ValueError(
                    "Holistic classification failed: LLM returned empty response"
                )

            # Reasoning models (Claude Sonnet 5 / 4.6+, extended thinking on) emit
            # reasoningContent block(s) before the answer text block, so content[0]
            # may not be the text. Concatenate all text blocks.
            classification_text = "".join(
                item["text"]
                for item in content_array
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            )

            # Try to extract JSON from the response
            try:
                classification_json = extract_json_from_text(classification_text)
                classification_data = json.loads(classification_json)
                segments = classification_data.get("segments", [])

                if not segments:
                    raise ValueError("No segments found in the classification result")

                # Update page classifications based on segments
                for i, segment in enumerate(segments):
                    # Validate segment data
                    if not all(
                        k in segment
                        for k in ["ordinal_start_page", "ordinal_end_page", "type"]
                    ):
                        logger.warning(f"Segment {i} is missing required fields")
                        continue

                    # Normalize page IDs (convert from 1-based to actual page IDs in the document)
                    start_page = segment["ordinal_start_page"]
                    end_page = segment["ordinal_end_page"]
                    doc_type = segment["type"]

                    # Check if the doc_type is valid
                    if doc_type not in self.valid_doc_types:
                        logger.warning(
                            f"Unknown document type '{doc_type}', using anyway"
                        )

                    # Update page classifications
                    try:
                        for page_idx in range(start_page, end_page + 1):
                            page_id = str(page_idx)
                            if page_id in document.pages:
                                # Update page classification
                                document.pages[page_id].classification = doc_type
                                document.pages[page_id].confidence = 1.0
                    except Exception as e:
                        logger.error(f"Error processing segment {i}: {e}")
                        continue

                # Apply section splitting strategy based on configuration
                strategy = self.config.classification.sectionSplitting.lower()

                if strategy == "disabled":
                    # Create single section with all pages using first segment's classification
                    if segments:
                        first_classification = segments[0]["type"]
                        document.sections = [
                            Section(
                                section_id="1",
                                classification=first_classification,
                                confidence=1.0,
                                page_ids=list(document.pages.keys()),
                            )
                        ]
                        logger.info(
                            f"Created single section with all pages, class='{first_classification}' (sectionSplitting=disabled)"
                        )
                elif strategy == "page":
                    # Create one section per page with its assigned classification
                    document.sections = []
                    sorted_page_ids = sorted(
                        document.pages.keys(),
                        key=lambda x: int(x) if x.isdigit() else float("inf"),
                    )
                    for idx, page_id in enumerate(sorted_page_ids, start=1):
                        page = document.pages[page_id]
                        document.sections.append(
                            Section(
                                section_id=str(idx),
                                classification=page.classification,
                                confidence=page.confidence,
                                page_ids=[page_id],
                            )
                        )
                    logger.info(
                        f"Created {len(document.sections)} sections (one per page) with sectionSplitting=page"
                    )
                else:  # llm_determined (default)
                    # Use LLM-determined segments as sections
                    document.sections = []
                    for i, segment in enumerate(segments):
                        if not all(
                            k in segment
                            for k in ["ordinal_start_page", "ordinal_end_page", "type"]
                        ):
                            continue

                        start_page = segment["ordinal_start_page"]
                        end_page = segment["ordinal_end_page"]
                        doc_type = segment["type"]

                        # Find corresponding page IDs
                        page_ids = []
                        try:
                            for page_idx in range(start_page, end_page + 1):
                                page_id = str(page_idx)
                                if page_id in document.pages:
                                    page_ids.append(page_id)
                        except Exception as e:
                            logger.error(f"Error processing segment {i}: {e}")
                            continue

                        if not page_ids:
                            logger.warning(f"No valid pages found for segment {i}")
                            continue

                        # Create and add the section
                        document.sections.append(
                            Section(
                                section_id=str(i + 1),
                                classification=doc_type,
                                confidence=1.0,
                                page_ids=page_ids,
                            )
                        )
                    logger.info(
                        f"Created {len(document.sections)} sections using LLM-determined segments (sectionSplitting=llm_determined)"
                    )

                # Update document metering and status
                document.metering = utils.merge_metering_data(
                    document.metering, metering
                )
                document = self._update_document_status(document)

                logger.info(
                    f"Document classified with {len(document.sections)} sections using holistic method"
                )

            except Exception as e:
                error_msg = f"Error parsing holistic classification result: {str(e)}"
                document = self._update_document_status(
                    document, success=False, error_message=error_msg
                )

        except Exception as e:
            error_msg = f"Error in holistic classification: {str(e)}"
            document = self._update_document_status(
                document, success=False, error_message=error_msg
            )
            raise

        return document
