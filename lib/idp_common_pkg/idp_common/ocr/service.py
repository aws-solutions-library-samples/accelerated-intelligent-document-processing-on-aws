# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
OCR Service for document processing with AWS Textract or Amazon Bedrock.

This module provides a service for extracting text from PDF documents
using either AWS Textract or Amazon Bedrock LLMs, with support for concurrent
processing of multiple pages.
"""

from __future__ import annotations

import concurrent.futures
import io
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import boto3
import pypdfium2 as pdfium
from botocore.config import Config

from idp_common import bedrock, image, s3, utils
from idp_common.config.models import IDPConfig
from idp_common.models import Document, Page, Status
from idp_common.ocr.document_converter import DocumentConverter

logger = logging.getLogger(__name__)

# Render resolution used when ocr.image.dpi is not configured. Textract's ability
# to detect small, faint or skewed glyphs (page numbers, box numbers, hand-filled
# values) degrades sharply below ~200 dpi: at 150 dpi it silently drops them from
# its response entirely, with no signal to the caller. See issue #729.
DEFAULT_DPI = 300

# Ceiling on the rendered page image, applied when ocr.image.target_width /
# target_height are not configured. This is an out-of-memory guard, NOT a cost
# control: it is deliberately set high enough that it does not bind for A4 or US
# Letter at DEFAULT_DPI, so those render at full 300 dpi.
#
# Raising the render resolution costs almost nothing downstream. Bedrock
# downscales images to its own long-edge ceiling before tokenizing, so the LLM
# token spend saturates - measured end to end, moving from 897x1269 to 2000x2829
# changed total input tokens by 1.4% (22,598 -> 22,914). What it does buy is OCR
# accuracy, which improves consistently with resolution.
DEFAULT_TARGET_WIDTH = 2600
DEFAULT_TARGET_HEIGHT = 3600


class OcrService:
    """Service for OCR processing of documents using AWS Textract or Amazon Bedrock."""

    def __init__(
        self,
        region: Optional[str] = None,
        config: Optional[Union[Dict[str, Any], "IDPConfig"]] = None,
        backend: Optional[str] = None,
        max_workers: Optional[int] = None,
        # Deprecated parameters for backward compatibility
        enhanced_features: Optional[Union[bool, List[str]]] = None,
        dpi: Optional[int] = None,
        resize_config: Optional[Dict[str, Any]] = None,
        bedrock_config: Optional[Dict[str, Any]] = None,
        preprocessing_config: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize the OCR service.

        Args:
            region: AWS region for services
            config: Configuration dictionary or IDPConfig model containing all OCR settings
            backend: OCR backend to use ("textract", "bedrock", or "none")
            max_workers: Maximum number of concurrent workers for page processing

            Deprecated parameters (use config instead):
            enhanced_features: Controls Textract FeatureTypes for analyze_document API
            dpi: DPI (dots per inch) for image generation from PDF pages
            resize_config: Image resizing configuration
            bedrock_config: Bedrock configuration if backend is "bedrock"
            preprocessing_config: Preprocessing configuration

        Raises:
            ValueError: If invalid features are specified or if an invalid backend is specified
        """
        # Handle backward compatibility
        if config is None and any(
            [
                enhanced_features is not None,
                dpi is not None,
                resize_config is not None,
                bedrock_config is not None,
                preprocessing_config is not None,
            ]
        ):
            logger.warning(
                "Using deprecated parameter pattern. Please migrate to using 'config' parameter. "
                "See OCR README for migration guide."
            )
            # Use old parameters
            self.region = region or os.environ.get("AWS_REGION", "us-east-1")
            self.max_workers = max_workers or 20
            self.dpi = dpi
            self.resize_config = resize_config
            self.backend = (backend or "textract").lower()
            self.bedrock_config = bedrock_config
            self.preprocessing_config = preprocessing_config
            self.enhanced_features = enhanced_features
        else:
            # Convert dict to IDPConfig if needed
            if config is not None and isinstance(config, dict):
                config_model: IDPConfig = IDPConfig(**config)
            elif config is None:
                config_model = IDPConfig()
            else:
                config_model = config

            # New pattern - extract from typed config (type-safe access!)
            self.region = region or os.environ.get("AWS_REGION", "us-east-1")
            self.config = config_model

            # Extract backend (type-safe, no .get() needed)
            self.backend = (backend or self.config.ocr.backend).lower()

            # Extract max_workers (automatic int conversion)
            self.max_workers = max_workers or self.config.ocr.max_workers

            # Extract DPI from image configuration (Pydantic handles type conversion!)
            self.dpi = self.config.ocr.image.dpi

            # Extract enhanced features (type-safe access)
            features_config = self.config.ocr.features
            if features_config:
                self.enhanced_features = [feature.name for feature in features_config]
            else:
                self.enhanced_features = False

            # Extract resize configuration (type-safe access)
            target_width = self.config.ocr.image.target_width
            target_height = self.config.ocr.image.target_height

            # Normalize None and empty strings to None for consistent handling
            if isinstance(target_width, str) and not target_width.strip():
                target_width = None
            if isinstance(target_height, str) and not target_height.strip():
                target_height = None

            # Apply sizing configuration logic
            if target_width is None and target_height is None:
                # No sizing configuration provided (None or empty strings) - apply sensible defaults
                self.resize_config = {
                    "target_width": DEFAULT_TARGET_WIDTH,
                    "target_height": DEFAULT_TARGET_HEIGHT,
                }
                logger.info(
                    f"No image sizing configured, applying default ceiling: "
                    f"{DEFAULT_TARGET_WIDTH}x{DEFAULT_TARGET_HEIGHT} (out-of-memory guard; "
                    f"does not bind for A4/Letter at {DEFAULT_DPI} dpi)"
                )
            else:
                # Handle empty strings by converting to None for validation
                if isinstance(target_width, str) and not target_width.strip():
                    target_width = None
                if isinstance(target_height, str) and not target_height.strip():
                    target_height = None

                # If after handling empty strings we still have values, use them
                if target_width is not None or target_height is not None:
                    # Explicit configuration provided - validate and use it
                    try:
                        self.resize_config = {
                            "target_width": (
                                int(target_width) if target_width is not None else None
                            ),
                            "target_height": (
                                int(target_height)
                                if target_height is not None
                                else None
                            ),
                        }
                        logger.info(
                            f"Using configured image sizing: {target_width}x{target_height}"
                        )
                    except (ValueError, TypeError):
                        logger.warning(
                            f"Invalid resize configuration values: width={target_width}, height={target_height}. "
                            f"Falling back to defaults: {DEFAULT_TARGET_WIDTH}x{DEFAULT_TARGET_HEIGHT}"
                        )
                        self.resize_config = {
                            "target_width": DEFAULT_TARGET_WIDTH,
                            "target_height": DEFAULT_TARGET_HEIGHT,
                        }
                else:
                    # After handling empty strings, we have None values - apply defaults
                    self.resize_config = {
                        "target_width": DEFAULT_TARGET_WIDTH,
                        "target_height": DEFAULT_TARGET_HEIGHT,
                    }
                    logger.info(
                        f"Invalid image sizing configuration provided, applying default ceiling: "
                        f"{DEFAULT_TARGET_WIDTH}x{DEFAULT_TARGET_HEIGHT} (out-of-memory guard)"
                    )

            # Extract preprocessing configuration (type-safe)
            preprocessing_value = self.config.ocr.image.preprocessing
            if preprocessing_value is True or (
                isinstance(preprocessing_value, str)
                and preprocessing_value.lower() == "true"
            ):
                self.preprocessing_config = {"enabled": True}
            else:
                self.preprocessing_config = None

            # Extract Bedrock configuration (type-safe)
            if self.backend == "bedrock":
                # Check if bedrock config has required fields
                if (
                    self.config.ocr.model_id
                    and self.config.ocr.system_prompt
                    and self.config.ocr.task_prompt
                ):
                    self.bedrock_config = {
                        "model_id": self.config.ocr.model_id,
                        "system_prompt": self.config.ocr.system_prompt,
                        "task_prompt": self.config.ocr.task_prompt,
                    }
                else:
                    self.bedrock_config = None
            else:
                self.bedrock_config = None

        # Log DPI and sizing configuration together for clarity
        if self.resize_config:
            logger.info(
                f"OCR Service initialized - DPI: {self.dpi}, "
                f"Image sizing: {self.resize_config['target_width']}x{self.resize_config['target_height']}"
            )
        else:
            logger.info(
                f"OCR Service initialized - DPI: {self.dpi}, No image sizing limits"
            )

        # Validate backend
        if self.backend not in ["textract", "bedrock", "bda", "none"]:
            raise ValueError(
                f"Invalid backend: {backend}. Must be 'textract', 'bedrock', 'bda', or 'none'"
            )

        # Initialize clients based on backend
        if self.backend == "textract":
            # Define valid Textract feature types
            VALID_FEATURES = ["TABLES", "FORMS", "SIGNATURES", "LAYOUT"]

            # Validate features if provided as a list
            if isinstance(self.enhanced_features, list):
                # Check for invalid features
                invalid_features = [
                    feature
                    for feature in self.enhanced_features
                    if feature not in VALID_FEATURES
                ]
                if invalid_features:
                    error_msg = f"Invalid Textract feature(s) specified: {invalid_features}. Valid features are: {VALID_FEATURES}"
                    logger.error(error_msg)
                    raise ValueError(error_msg)

                # Log the validated features
                logger.info(
                    f"OCR Service initialized with features: {self.enhanced_features}"
                )

            # Initialize Textract client with adaptive retries
            adaptive_config = Config(
                retries={"max_attempts": 100, "mode": "adaptive"},
                max_pool_connections=self.max_workers * 3,
            )
            self.textract_client = boto3.client(
                "textract", region_name=self.region, config=adaptive_config
            )

            logger.info("OCR Service initialized with Textract backend")
        elif self.backend == "bedrock":
            # Enhanced features not used with Bedrock
            self.enhanced_features = False

            # Validate bedrock_config is provided
            if not self.bedrock_config:
                raise ValueError(
                    "bedrock_config is required when using 'bedrock' backend"
                )

            # Validate required bedrock_config fields
            required_fields = ["model_id", "system_prompt", "task_prompt"]
            missing_fields = [
                field for field in required_fields if not self.bedrock_config.get(field)
            ]
            if missing_fields:
                raise ValueError(
                    f"Missing required bedrock_config fields: {missing_fields}"
                )

            logger.info(
                f"OCR Service initialized with Bedrock backend, config: {self.bedrock_config}"
            )
        elif self.backend == "bda":
            # Bedrock Data Automation standard-output OCR. Enhanced features are
            # not applicable (BDA always returns tables + layout + word geometry).
            self.enhanced_features = False

            adaptive_config = Config(
                retries={"max_attempts": 100, "mode": "adaptive"},
                max_pool_connections=self.max_workers * 3,
            )
            self.bda_runtime_client = boto3.client(
                "bedrock-data-automation-runtime",
                region_name=self.region,
                config=adaptive_config,
            )

            # Resolve project + profile ARNs. Project ARN may be supplied via
            # config; otherwise a standard-output SYNC OCR project is
            # auto-created and reused. Resolution is deferred to first use to
            # keep __init__ side-effect-free when possible.
            # The stack-scoped BDA OCR project is provisioned at deploy time by
            # the BDA OCR project CloudFormation custom resource and delivered
            # via the BDA_OCR_PROJECT_ARN env var. An explicit config value
            # overrides it. We never create the project at runtime.
            self.bda_project_arn = (
                (self.config.ocr.bda_project_arn if hasattr(self, "config") else None)
                or os.environ.get("BDA_OCR_PROJECT_ARN")
                or None
            )
            self._bda_profile_arn: Optional[str] = None
            # Guards lazy profile-ARN resolution so parallel page workers build
            # the profile ARN exactly once instead of racing.
            self._bda_arn_lock = threading.Lock()
            logger.info(
                "OCR Service initialized with BDA backend"
                + (
                    f", project {self.bda_project_arn}"
                    if self.bda_project_arn
                    else " (no project ARN provided)"
                )
            )
        elif self.backend == "none":
            # No OCR processing - image-only mode
            self.enhanced_features = False
            logger.info(
                "OCR Service initialized with 'none' backend - image-only processing"
            )

        # Initialize S3 client with connection pool matching max_workers
        s3_config = Config(
            retries={"max_attempts": 10, "mode": "adaptive"},
            max_pool_connections=max(self.max_workers, 10),
        )
        self.s3_client = boto3.client("s3", config=s3_config)
        logger.info(
            f"S3 client initialized with {max(self.max_workers, 10)} connection pool size"
        )

        # Initialize document converter for non-PDF formats
        self.document_converter = DocumentConverter(dpi=self.dpi or DEFAULT_DPI)

    def process_document(self, document: Document) -> Document:
        """
        Process a document with OCR and update the Document model.
        Supports PDF, images, text, CSV, Excel, and Word documents.

        Args:
            document: Document model object to update with OCR results

        Returns:
            Updated Document object with OCR results
        """
        t0 = time.time()

        # Get the document from S3
        try:
            response = self.s3_client.get_object(
                Bucket=document.input_bucket, Key=document.input_key
            )
            file_content = response["Body"].read()
            t1 = time.time()
            logger.debug(f"Time taken for S3 GetObject: {t1 - t0:.6f} seconds")
        except Exception as e:
            import traceback

            error_msg = f"Error retrieving document from S3: {str(e)}"
            stack_trace = traceback.format_exc()
            logger.error(f"{error_msg}\nStack trace:\n{stack_trace}")
            document.errors.append(f"{error_msg} (see logs for full trace)")
            document.status = Status.FAILED
            return document

        # Detect file type and process accordingly
        try:
            file_type = self._detect_file_type(document.input_key, file_content)
            logger.info(f"Detected file type: {file_type}")

            if file_type in ["txt", "csv", "xlsx", "docx"]:
                # Process non-PDF documents
                pages_data = self._process_non_pdf_document(file_type, file_content)
                document.num_pages = len(pages_data)

                # Process each page
                for page_index, (image_bytes, page_text) in enumerate(pages_data):
                    page_id = str(page_index + 1)
                    try:
                        ocr_result, page_metering = self._process_converted_page(
                            page_index,
                            image_bytes,
                            page_text,
                            document.output_bucket,
                            document.input_key,
                        )

                        # Create Page object and add to document
                        document.pages[page_id] = Page(
                            page_id=page_id,
                            image_uri=ocr_result["image_uri"],
                            raw_text_uri=ocr_result["raw_text_uri"],
                            parsed_text_uri=ocr_result["parsed_text_uri"],
                            text_confidence_uri=ocr_result["text_confidence_uri"],
                            ocr_page_data_uri=ocr_result.get("ocr_page_data_uri"),
                        )

                        # Merge metering data
                        document.metering = utils.merge_metering_data(
                            document.metering, page_metering
                        )

                    except Exception as e:
                        import traceback

                        error_msg = f"Error processing page {page_index + 1}: {str(e)}"
                        stack_trace = traceback.format_exc()
                        logger.error(f"{error_msg}\nStack trace:\n{stack_trace}")
                        document.errors.append(f"{error_msg} (see logs for full trace)")
            else:
                # Process PDF/image documents
                is_pdf = file_type == "pdf"

                if is_pdf:
                    # Determine which pages need processing (retry-safe: skip completed pages)
                    pdf_document = pdfium.PdfDocument(file_content)
                    # Initialize form rendering engine so fillable PDF form fields
                    # (text inputs, checkboxes, etc.) appear in rendered page images.
                    # Without this, may_draw_forms=True in render() has no effect.
                    pdf_document.init_forms()
                    num_pages = len(pdf_document)
                    document.num_pages = num_pages

                    pages_to_render = []
                    pages_skipped = 0
                    for i in range(num_pages):
                        page_id = str(i + 1)
                        existing_page = document.pages.get(page_id)
                        if (
                            existing_page
                            and existing_page.image_uri
                            and existing_page.raw_text_uri
                            and existing_page.parsed_text_uri
                        ):
                            pages_skipped += 1
                        else:
                            pages_to_render.append(i)

                    if pages_skipped > 0:
                        logger.info(
                            f"Retry-safe OCR: skipping {pages_skipped} already-completed pages, "
                            f"rendering and processing {len(pages_to_render)} remaining pages"
                        )

                    # Phase 1: Render ONLY needed page images SEQUENTIALLY
                    # pypdfium2 (PDFium) is not thread-safe for concurrent access
                    # to the same PdfDocument, so we render pages first, then
                    # process OCR in parallel (which is the I/O-bound bottleneck).
                    logger.info(
                        f"Rendering {len(pages_to_render)} of {num_pages} page images sequentially (pypdfium2 is not thread-safe)"
                    )
                    page_images: Dict[int, bytes] = {}
                    for i in pages_to_render:
                        page = pdf_document[i]
                        # Flatten form fields into page content before rendering.
                        # Many fillable PDFs (e.g., government forms) lack appearance
                        # streams for form fields — flatten() forces PDFium to generate
                        # them and merge into page content so render() can display them.
                        # Only applies when PDF has form fields (formenv is set by init_forms).
                        if page.formenv is not None:
                            page.flatten()
                        page_images[i] = self._extract_page_image(page, True, i + 1)

                    pdf_document.close()
                    logger.info(f"Rendered {len(page_images)} page images")

                    # Phase 2: Process OCR in PARALLEL (Textract/Bedrock API calls are I/O-bound)
                    with concurrent.futures.ThreadPoolExecutor(
                        max_workers=self.max_workers
                    ) as executor:
                        future_to_page = {
                            executor.submit(
                                self._process_page_with_image,
                                i,
                                page_images[i],
                                document.output_bucket,
                                document.input_key,
                            ): i
                            for i in page_images
                        }

                        # Start memory monitoring in background thread
                        memory_monitor_shutdown = self._start_memory_monitoring()
                        completed_pages = 0

                        try:
                            for future in concurrent.futures.as_completed(
                                future_to_page
                            ):
                                page_index = future_to_page[future]
                                page_id = str(page_index + 1)
                                try:
                                    ocr_result, page_metering = future.result()

                                    document.pages[page_id] = Page(
                                        page_id=page_id,
                                        image_uri=ocr_result["image_uri"],
                                        raw_text_uri=ocr_result["raw_text_uri"],
                                        parsed_text_uri=ocr_result["parsed_text_uri"],
                                        text_confidence_uri=ocr_result[
                                            "text_confidence_uri"
                                        ],
                                        ocr_page_data_uri=ocr_result.get(
                                            "ocr_page_data_uri"
                                        ),
                                    )

                                    document.metering = utils.merge_metering_data(
                                        document.metering, page_metering
                                    )
                                    completed_pages += 1

                                except Exception as e:
                                    import traceback

                                    error_msg = f"Error processing page {page_index + 1}: {str(e)}"
                                    stack_trace = traceback.format_exc()
                                    logger.error(
                                        f"{error_msg}\nStack trace:\n{stack_trace}"
                                    )
                                    document.errors.append(
                                        f"{error_msg} (see logs for full trace)"
                                    )
                        finally:
                            memory_monitor_shutdown.set()

                    # Free page images after OCR processing
                    page_images.clear()

                else:
                    # Image files - single page, no threading needed
                    document.num_pages = 1
                    ocr_result, page_metering = self._process_image_file_direct(
                        document.output_bucket,
                        document.input_key,
                        file_content,
                    )
                    document.pages["1"] = Page(
                        page_id="1",
                        image_uri=ocr_result["image_uri"],
                        raw_text_uri=ocr_result["raw_text_uri"],
                        parsed_text_uri=ocr_result["parsed_text_uri"],
                        text_confidence_uri=ocr_result["text_confidence_uri"],
                        ocr_page_data_uri=ocr_result.get("ocr_page_data_uri"),
                    )
                    document.metering = utils.merge_metering_data(
                        document.metering, page_metering
                    )

            # Sort the pages dictionary by ascending page number
            logger.info(f"Sorting {len(document.pages)} pages by page number")

            # Create a new ordered dictionary with sorted pages
            sorted_pages = {}
            # Convert page_id to int for sorting, then back to string for the keys
            for page_id in sorted(document.pages.keys(), key=lambda x: int(x)):
                sorted_pages[page_id] = document.pages[page_id]

            # Replace the original pages dictionary with the sorted one
            document.pages = sorted_pages

            if document.errors:
                document.status = Status.FAILED

        except Exception as e:
            import traceback

            error_msg = f"Error processing document: {str(e)}"
            stack_trace = traceback.format_exc()
            logger.error(f"{error_msg}\nStack trace:\n{stack_trace}")
            document.errors.append(f"{error_msg} (see logs for full trace)")
            document.status = Status.FAILED

        t2 = time.time()
        logger.info(f"OCR processing completed in {t2 - t0:.2f} seconds")
        logger.info(
            f"Processed {len(document.pages)} pages, with {len(document.errors)} errors"
        )
        return document

    def _feature_combo(self):
        """Return the pricing feature combination string based on enhanced_features.

        Returns one of: "Tables", "Forms", "Tables+Forms", "Signatures", "Layout", or ""

        Note:
        - Layout feature is included free with any combination of Forms, Tables
        - Signatures feature is included free with Forms, Tables, and Layout

        Both "free in combination" rules mean the returned key omits the free
        feature, so it contributes nothing to metered cost. Per the Amazon Textract
        pricing page (https://aws.amazon.com/textract/pricing/): "Signatures feature
        is included free of cost with any combination of Forms, Tables, Queries, and
        Layout", and correspondingly for Layout with Forms/Tables/Queries. AWS emits
        no usage type at all for a feature that is free in combination, which is
        what our bills show (long stretches of ``SyncTablesPagesProcessed`` with no
        ``SyncLayoutPagesProcessed`` and no ``SyncSignaturesPagesProcessed`` despite
        both features being enabled).

        Signatures used ALONE is billed (~$0.0035/page), which is why the
        ``-Signatures`` key exists. Revisit alongside
        ``config_library/pricing.yaml`` if Textract's usage types or footnotes
        change.
        """
        # TODO: Uncomment this when needed
        # Define valid Textract feature types
        # VALID_FEATURES = ["TABLES", "FORMS", "SIGNATURES", "LAYOUT"]

        # We assume features have already been validated in _analyze_document
        # This is just a safety check
        if not isinstance(self.enhanced_features, list) or not self.enhanced_features:
            return ""

        # All features should be valid at this point
        features = set(self.enhanced_features)

        # Check for feature combinations
        has_tables = "TABLES" in features
        has_forms = "FORMS" in features
        has_layout = "LAYOUT" in features
        has_signatures = "SIGNATURES" in features

        # Tables + Forms
        if has_tables and has_forms:
            return "-Tables+Forms"
        # Tables only
        elif has_tables:
            return "-Tables"
        # Forms only
        elif has_forms:
            return "-Forms"
        # Layout (only charged if not with Forms/Tables)
        elif has_layout:
            return "-Layout"
        # Signatures (only charged if used alone)
        elif has_signatures:
            return "-Signatures"
        return ""

    def _process_single_page(
        self,
        page_index: int,
        pdf_document: Optional[pdfium.PdfDocument],
        is_pdf: bool,
        output_bucket: str,
        prefix: str,
        original_file_content: Optional[bytes] = None,
    ) -> Tuple[Dict[str, str], Dict[str, Any]]:
        """
        Process a single page of a document (PDF or image).

        Args:
            page_index: Zero-based index of the page
            pdf_document: pypdfium2 document object (None for image files)
            is_pdf: Whether the document is a PDF
            output_bucket: S3 bucket to store results
            prefix: S3 prefix for storing results
            original_file_content: Original file content for image files

        Returns:
            Tuple of (page_result_dict, metering_data)
        """
        # Check if this is an image file (not a PDF)
        if not is_pdf and page_index == 0:
            # This is an image file - process it directly
            return self._process_image_file_direct(
                output_bucket, prefix, original_file_content
            )

        # Use the appropriate backend for PDFs
        if self.backend == "none":
            return self._process_single_page_none(
                page_index, pdf_document, output_bucket, prefix
            )
        elif self.backend == "bedrock":
            return self._process_single_page_bedrock(
                page_index, pdf_document, output_bucket, prefix
            )
        elif self.backend == "bda":
            # BDA renders + OCRs per page from the page image, using the same
            # image-based path as the parallel PDF workflow.
            page = pdf_document[page_index]
            img_bytes = self._extract_page_image(page, True, page_index + 1)
            return self._process_page_with_image(
                page_index, img_bytes, output_bucket, prefix
            )
        else:
            # Textract backend (default)
            return self._process_single_page_textract(
                page_index, pdf_document, output_bucket, prefix
            )

    # Formats supported by Amazon Bedrock multimodal APIs
    BEDROCK_COMPATIBLE_FORMATS = {"jpeg", "jpg", "png", "gif", "webp"}

    def _process_image_file_direct(
        self,
        output_bucket: str,
        prefix: str,
        original_file_content: Optional[bytes] = None,
    ) -> Tuple[Dict[str, str], Dict[str, Any]]:
        """
        Process an image file directly.

        Args:
            output_bucket: S3 bucket to store results
            prefix: S3 prefix for storing results
            original_file_content: Original file content bytes

        Returns:
            Tuple of (page_result_dict, metering_data)
        """
        t0 = time.time()
        page_id = 1

        # Use the original file content directly
        if original_file_content:
            import io

            from PIL import Image as PILImage

            # Use the original file content directly
            img_data = original_file_content

            # Detect format from the original content
            pil_img = PILImage.open(io.BytesIO(img_data))
            img_format = pil_img.format.lower() if pil_img.format else "jpeg"
            img_ext = img_format if img_format != "jpeg" else "jpg"

            # Get dimensions for logging
            original_width, original_height = pil_img.size
            logger.debug(
                f"Using original file content: {original_width}x{original_height} {img_format}"
            )

            # Check if format needs conversion for Bedrock compatibility
            # TIFF, BMP, and other non-Bedrock formats must be converted to JPEG
            if img_format not in self.BEDROCK_COMPATIBLE_FORMATS:
                logger.info(
                    f"Converting {img_format.upper()} (mode={pil_img.mode}) to JPEG for Bedrock compatibility"
                )
                # Handle high bit-depth modes that need scaling (common in TIFF)
                # These modes have values outside 0-255 range and need normalization
                if pil_img.mode in ("I", "I;16", "I;16B", "I;16L", "I;16N"):
                    # 16/32-bit integer grayscale - scale to 8-bit
                    import numpy as np

                    arr = np.array(pil_img, dtype=np.float64)
                    if arr.max() > arr.min():
                        arr = (arr - arr.min()) / (arr.max() - arr.min()) * 255
                    arr = arr.astype(np.uint8)
                    pil_img = PILImage.fromarray(arr, mode="L")
                    logger.debug(
                        "Scaled high bit-depth integer image to 8-bit grayscale"
                    )
                elif pil_img.mode == "F":
                    # 32-bit floating point - normalize to 0-255
                    import numpy as np

                    arr = np.array(pil_img, dtype=np.float64)
                    if arr.max() > arr.min():
                        arr = (arr - arr.min()) / (arr.max() - arr.min()) * 255
                    arr = arr.astype(np.uint8)
                    pil_img = PILImage.fromarray(arr, mode="L")
                    logger.debug("Scaled floating point image to 8-bit grayscale")
                elif pil_img.mode not in ("RGB", "L"):
                    # Handle other modes (RGBA, CMYK, P, LA, etc.)
                    pil_img = pil_img.convert("RGB")
                    logger.debug(f"Converted {pil_img.mode} to RGB")
                # Save as JPEG
                img_buffer = io.BytesIO()
                pil_img.save(img_buffer, format="JPEG", quality=95, optimize=True)
                img_data = img_buffer.getvalue()
                img_ext = "jpg"
                img_format = "jpeg"

            # Determine content type
            content_type_map = {
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "gif": "image/gif",
                "bmp": "image/bmp",
                "tiff": "image/tiff",
                "tif": "image/tiff",
                "webp": "image/webp",
            }
            original_content_type = content_type_map.get(img_ext, "image/jpeg")

            # Check if we need to resize
            needs_resize = False
            if self.resize_config and (
                self.resize_config.get("target_width")
                or self.resize_config.get("target_height")
            ):
                target_width = self.resize_config.get("target_width")
                target_height = self.resize_config.get("target_height")

                if target_width or target_height:
                    # Only check fit if both dimensions are provided (type-safe comparison)
                    if target_width is not None and target_height is not None:
                        # Check if image already fits within target dimensions
                        if (
                            original_width <= target_width
                            and original_height <= target_height
                        ):
                            logger.debug(
                                f"Image {original_width}x{original_height} already fits within "
                                f"{target_width}x{target_height}, using original"
                            )
                            needs_resize = False
                        else:
                            logger.debug(
                                f"Image {original_width}x{original_height} needs resizing to fit "
                                f"{target_width}x{target_height}"
                            )
                            needs_resize = True
                    else:
                        # Partial config - always resize to calculate missing dimension
                        logger.debug(
                            "Partial dimension config detected, will resize to calculate missing dimension"
                        )
                        needs_resize = True

            # Apply resize only if needed
            if needs_resize:
                img_data = image.resize_image(img_data, target_width, target_height)

                # Check if format changed after resize
                resized_img = PILImage.open(io.BytesIO(img_data))
                if resized_img.format and resized_img.format.lower() != img_ext:
                    new_format = resized_img.format.lower()
                    img_ext = new_format if new_format != "jpeg" else "jpg"
                    content_type = content_type_map.get(img_ext, "image/jpeg")
                    logger.debug(f"Image format changed during resize to {img_ext}")
                else:
                    content_type = original_content_type
            else:
                content_type = original_content_type
                logger.debug("No resize needed, using original image")

        else:
            # No original content provided - this should not happen for image files
            # since we always pass original_file_content, but handle gracefully
            raise ValueError(
                "Image file processing requires original_file_content to be provided"
            )

        # Store image with appropriate format
        image_key = f"{prefix}/pages/{page_id}/image.{img_ext}"
        s3.write_content(img_data, output_bucket, image_key, content_type=content_type)

        t1 = time.time()
        logger.debug(
            f"Time for image processing (page {page_id}): {t1 - t0:.6f} seconds"
        )

        # Process with OCR based on backend
        if self.backend == "none":
            # No OCR processing
            metering = {}

            # Create empty OCR response structure for compatibility
            empty_ocr_response = {"DocumentMetadata": {"Pages": 1}, "Blocks": []}

            # Store empty raw OCR response
            raw_text_key = f"{prefix}/pages/{page_id}/rawText.json"
            s3.write_content(
                empty_ocr_response,
                output_bucket,
                raw_text_key,
                content_type="application/json",
            )

            # Generate minimal text confidence data
            text_confidence_data = {
                "text": "| Text | Confidence |\n|:-----|:------------|\n| *No OCR performed* | N/A |"
            }

            text_confidence_key = f"{prefix}/pages/{page_id}/textConfidence.json"
            s3.write_content(
                text_confidence_data,
                output_bucket,
                text_confidence_key,
                content_type="application/json",
            )

            # Store empty parsed text result
            parsed_result = {"text": ""}
            parsed_text_key = f"{prefix}/pages/{page_id}/result.json"
            s3.write_content(
                parsed_result,
                output_bucket,
                parsed_text_key,
                content_type="application/json",
            )

            page_data_raw = empty_ocr_response
            page_data_text = ""
            page_data_provider = "none"

        elif self.backend == "bedrock":
            # Process with Bedrock
            # Apply preprocessing if enabled
            ocr_img_data = img_data
            if self.preprocessing_config and self.preprocessing_config.get("enabled"):
                from idp_common.image import apply_adaptive_binarization

                ocr_img_data = apply_adaptive_binarization(ocr_img_data)
                logger.debug(
                    "Applied adaptive binarization preprocessing for Bedrock OCR"
                )

            # Prepare image for Bedrock
            image_content = image.prepare_bedrock_image_attachment(ocr_img_data)

            # Prepare content for Bedrock
            content = [{"text": self.bedrock_config["task_prompt"]}, image_content]

            # Invoke Bedrock
            response_with_metering = bedrock.invoke_model(
                model_id=self.bedrock_config["model_id"],
                system_prompt=self.bedrock_config["system_prompt"],
                content=content,
                temperature=0.0,
                top_p=0.1,
                top_k=5,
                # None => Bedrock client resolves the model's max output tokens
                # (model_config_limits.yaml); Bedrock's default-when-omitted truncates.
                max_tokens=None,
                reasoning_effort=getattr(self.config.ocr, "reasoning_effort", None)
                if hasattr(self, "config")
                else None,
                context="OCR",
                model_lambda_hook_arn=getattr(
                    self.config.ocr, "model_lambda_hook_arn", None
                )
                if hasattr(self, "config")
                else None,
            )

            # Extract text from response
            extracted_text = bedrock.extract_text_from_response(response_with_metering)
            metering = response_with_metering.get("metering", {})

            # Persist structured OCR (Textract blocks with confidence + geometry)
            # if the LambdaHook returned it; otherwise store raw text + placeholder.
            raw_ocr_content, text_confidence_data = self._extract_bedrock_ocr_artifacts(
                response_with_metering["response"]
            )

            # Store raw Bedrock response (or Textract blocks)
            raw_text_key = f"{prefix}/pages/{page_id}/rawText.json"
            s3.write_content(
                raw_ocr_content,
                output_bucket,
                raw_text_key,
                content_type="application/json",
            )

            text_confidence_key = f"{prefix}/pages/{page_id}/textConfidence.json"
            s3.write_content(
                text_confidence_data,
                output_bucket,
                text_confidence_key,
                content_type="application/json",
            )

            # Store parsed text result
            parsed_result = {"text": extracted_text}
            parsed_text_key = f"{prefix}/pages/{page_id}/result.json"
            s3.write_content(
                parsed_result,
                output_bucket,
                parsed_text_key,
                content_type="application/json",
            )

            page_data_raw = raw_ocr_content
            page_data_text = extracted_text
            page_data_provider = "bedrock"

        elif self.backend == "bda":
            # BDA reads the image from S3 by s3Uri so extension-based modality
            # routing (jpeg/png -> DOCUMENT) applies. BDA documents only accept
            # PNG/JPG/TIFF and routing is only configured for jpeg/png, so any
            # other stored format (gif, webp, bmp) is transcoded to JPEG and
            # uploaded to a .jpg key for the BDA call. (BDA does its own image
            # processing, so binarization preprocessing is not applied here.)
            if img_ext in ("jpg", "png"):
                bda_image_key = image_key
            else:
                from PIL import Image as _PILImage

                _buf = io.BytesIO()
                _pil = _PILImage.open(io.BytesIO(img_data))
                if _pil.mode not in ("RGB", "L"):
                    _pil = _pil.convert("RGB")
                _pil.save(_buf, format="JPEG", quality=95)
                bda_image_key = f"{prefix}/pages/{page_id}/image_bda.jpg"
                s3.write_content(
                    _buf.getvalue(),
                    output_bucket,
                    bda_image_key,
                    content_type="image/jpeg",
                )

            # Dimensions of the stored page image (image.jpg) the UI overlays on;
            # forwarded so BDA rectification corners map into its coordinate space.
            _bda_img_size = self._image_size_from_bytes(img_data)

            (
                raw_ocr_content,
                extracted_text,
                raw_text_key,
                text_confidence_key,
                parsed_text_key,
                metering,
            ) = self._write_bda_page_artifacts(
                f"s3://{output_bucket}/{bda_image_key}",
                output_bucket,
                prefix,
                page_id,
                original_image_size=_bda_img_size,
            )
            page_data_raw = raw_ocr_content
            page_data_text = extracted_text
            page_data_provider = "bda"

        else:
            # Process with Textract (default)
            # Apply preprocessing if enabled
            ocr_img_data = img_data
            if self.preprocessing_config and self.preprocessing_config.get("enabled"):
                from idp_common.image import apply_adaptive_binarization

                ocr_img_data = apply_adaptive_binarization(ocr_img_data)
                logger.debug("Applied adaptive binarization preprocessing for OCR")

            # Process with OCR
            if isinstance(self.enhanced_features, list) and self.enhanced_features:
                textract_result = self._analyze_document(ocr_img_data, page_id)
            else:
                textract_result = self.textract_client.detect_document_text(
                    Document={"Bytes": ocr_img_data}
                )

            # Extract metering data
            feature_combo = self._feature_combo()
            metering = {
                f"OCR/textract/{self._get_api_name()}{feature_combo}": {
                    "pages": textract_result["DocumentMetadata"]["Pages"]
                }
            }

            # Store raw Textract response
            raw_text_key = f"{prefix}/pages/{page_id}/rawText.json"
            s3.write_content(
                textract_result,
                output_bucket,
                raw_text_key,
                content_type="application/json",
            )

            # Generate and store text confidence data
            text_confidence_data = self._generate_text_confidence_data(textract_result)
            text_confidence_key = f"{prefix}/pages/{page_id}/textConfidence.json"
            s3.write_content(
                text_confidence_data,
                output_bucket,
                text_confidence_key,
                content_type="application/json",
            )

            # Parse and store text content
            parsed_result = self._parse_textract_response(textract_result, page_id)
            parsed_text_key = f"{prefix}/pages/{page_id}/result.json"
            s3.write_content(
                parsed_result,
                output_bucket,
                parsed_text_key,
                content_type="application/json",
            )

            page_data_raw = textract_result
            page_data_text = parsed_result.get("text", "")
            page_data_provider = "textract"

        # Persist consolidated OCR page data (text + confidence + geometry)
        page_data_uri = self._write_page_data(
            page_id,
            output_bucket,
            prefix,
            page_data_raw,
            page_data_text,
            page_data_provider,
        )

        t2 = time.time()
        logger.debug(f"Total processing time for image file: {t2 - t0:.6f} seconds")

        # Create and return page result
        result = {
            "raw_text_uri": f"s3://{output_bucket}/{raw_text_key}",
            "parsed_text_uri": f"s3://{output_bucket}/{parsed_text_key}",
            "text_confidence_uri": f"s3://{output_bucket}/{text_confidence_key}",
            "ocr_page_data_uri": page_data_uri,
            "image_uri": f"s3://{output_bucket}/{image_key}",
        }

        return result, metering

    def _process_page_with_image(
        self,
        page_index: int,
        img_bytes: bytes,
        output_bucket: str,
        prefix: str,
    ) -> Tuple[Dict[str, str], Dict[str, Any]]:
        """
        Process a single page using pre-rendered image bytes.

        This method is used in the thread-safe two-phase approach: page images are
        rendered sequentially (pypdfium2 is not thread-safe), then this method is
        called in parallel threads for the I/O-bound OCR processing.

        Args:
            page_index: Zero-based index of the page
            img_bytes: Pre-rendered page image bytes (JPEG)
            output_bucket: S3 bucket to store results
            prefix: S3 prefix for storing results

        Returns:
            Tuple of (page_result_dict, metering_data)
        """
        t0 = time.time()
        page_id = page_index + 1

        # Upload pre-rendered image to S3
        image_key = f"{prefix}/pages/{page_id}/image.jpg"
        s3.write_content(img_bytes, output_bucket, image_key, content_type="image/jpeg")

        t1 = time.time()
        logger.debug(f"Time for image upload (page {page_id}): {t1 - t0:.6f} seconds")

        # Use the image for OCR processing
        ocr_img_bytes = img_bytes

        # Apply preprocessing if enabled
        if self.preprocessing_config and self.preprocessing_config.get("enabled"):
            from idp_common.image import apply_adaptive_binarization

            ocr_img_bytes = apply_adaptive_binarization(ocr_img_bytes)
            logger.debug(
                f"Applied adaptive binarization preprocessing (page {page_id})"
            )

        if self.backend == "none":
            # No OCR processing - create empty results
            metering: Dict[str, Any] = {}
            empty_ocr_response = {"DocumentMetadata": {"Pages": 1}, "Blocks": []}
            raw_text_key = f"{prefix}/pages/{page_id}/rawText.json"
            s3.write_content(
                empty_ocr_response,
                output_bucket,
                raw_text_key,
                content_type="application/json",
            )

            text_confidence_data = {
                "text": "| Text | Confidence |\n|:-----|:------------|\n| *No OCR performed* | N/A |"
            }
            text_confidence_key = f"{prefix}/pages/{page_id}/textConfidence.json"
            s3.write_content(
                text_confidence_data,
                output_bucket,
                text_confidence_key,
                content_type="application/json",
            )

            parsed_result = {"text": ""}
            parsed_text_key = f"{prefix}/pages/{page_id}/result.json"
            s3.write_content(
                parsed_result,
                output_bucket,
                parsed_text_key,
                content_type="application/json",
            )

            page_data_raw = empty_ocr_response
            page_data_text = ""
            page_data_provider = "none"

        elif self.backend == "bedrock":
            # Process with Bedrock
            image_content = image.prepare_bedrock_image_attachment(ocr_img_bytes)
            content = [{"text": self.bedrock_config["task_prompt"]}, image_content]

            response_with_metering = bedrock.invoke_model(
                model_id=self.bedrock_config["model_id"],
                system_prompt=self.bedrock_config["system_prompt"],
                content=content,
                temperature=0.0,
                top_p=0.1,
                top_k=5,
                # None => Bedrock client resolves the model's max output tokens
                # (model_config_limits.yaml); Bedrock's default-when-omitted truncates.
                max_tokens=None,
                reasoning_effort=getattr(self.config.ocr, "reasoning_effort", None)
                if hasattr(self, "config")
                else None,
                context="OCR",
                model_lambda_hook_arn=getattr(
                    self.config.ocr, "model_lambda_hook_arn", None
                )
                if hasattr(self, "config")
                else None,
            )

            extracted_text = bedrock.extract_text_from_response(response_with_metering)
            metering = response_with_metering.get("metering", {})

            # Persist structured OCR (Textract blocks with confidence + geometry)
            # if the LambdaHook returned it; otherwise store raw text + placeholder.
            raw_ocr_content, text_confidence_data = self._extract_bedrock_ocr_artifacts(
                response_with_metering["response"]
            )

            raw_text_key = f"{prefix}/pages/{page_id}/rawText.json"
            s3.write_content(
                raw_ocr_content,
                output_bucket,
                raw_text_key,
                content_type="application/json",
            )

            text_confidence_key = f"{prefix}/pages/{page_id}/textConfidence.json"
            s3.write_content(
                text_confidence_data,
                output_bucket,
                text_confidence_key,
                content_type="application/json",
            )

            parsed_result = {"text": extracted_text}
            parsed_text_key = f"{prefix}/pages/{page_id}/result.json"
            s3.write_content(
                parsed_result,
                output_bucket,
                parsed_text_key,
                content_type="application/json",
            )

            page_data_raw = raw_ocr_content
            page_data_text = extracted_text
            page_data_provider = "bedrock"

        elif self.backend == "bda":
            # BDA reads the page image already uploaded to S3 (image_key) by
            # s3Uri so extension-based modality routing applies. Pass the stored
            # image's dimensions so BDA rectification corners map into its space.
            (
                raw_ocr_content,
                extracted_text,
                raw_text_key,
                text_confidence_key,
                parsed_text_key,
                metering,
            ) = self._write_bda_page_artifacts(
                f"s3://{output_bucket}/{image_key}",
                output_bucket,
                prefix,
                page_id,
                original_image_size=self._image_size_from_bytes(img_bytes),
            )
            page_data_raw = raw_ocr_content
            page_data_text = extracted_text
            page_data_provider = "bda"

        else:
            # Process with Textract (default)
            if isinstance(self.enhanced_features, list) and self.enhanced_features:
                textract_result = self._analyze_document(ocr_img_bytes, page_id)
            else:
                textract_result = self.textract_client.detect_document_text(
                    Document={"Bytes": ocr_img_bytes}
                )

            feature_combo = self._feature_combo()
            metering = {
                f"OCR/textract/{self._get_api_name()}{feature_combo}": {
                    "pages": textract_result["DocumentMetadata"]["Pages"]
                }
            }

            raw_text_key = f"{prefix}/pages/{page_id}/rawText.json"
            s3.write_content(
                textract_result,
                output_bucket,
                raw_text_key,
                content_type="application/json",
            )

            text_confidence_data = self._generate_text_confidence_data(textract_result)
            text_confidence_key = f"{prefix}/pages/{page_id}/textConfidence.json"
            s3.write_content(
                text_confidence_data,
                output_bucket,
                text_confidence_key,
                content_type="application/json",
            )

            parsed_result = self._parse_textract_response(textract_result, page_id)
            parsed_text_key = f"{prefix}/pages/{page_id}/result.json"
            s3.write_content(
                parsed_result,
                output_bucket,
                parsed_text_key,
                content_type="application/json",
            )

            page_data_raw = textract_result
            page_data_text = parsed_result.get("text", "")
            page_data_provider = "textract"

        # Persist consolidated OCR page data (text + confidence + geometry)
        page_data_uri = self._write_page_data(
            page_id,
            output_bucket,
            prefix,
            page_data_raw,
            page_data_text,
            page_data_provider,
        )

        # Memory cleanup - delete references to free large image buffers
        del img_bytes
        del ocr_img_bytes

        t2 = time.time()
        logger.debug(
            f"Total page processing time (page {page_id}): {t2 - t0:.6f} seconds"
        )

        result = {
            "raw_text_uri": f"s3://{output_bucket}/{raw_text_key}",
            "parsed_text_uri": f"s3://{output_bucket}/{parsed_text_key}",
            "text_confidence_uri": f"s3://{output_bucket}/{text_confidence_key}",
            "ocr_page_data_uri": page_data_uri,
            "image_uri": f"s3://{output_bucket}/{image_key}",
        }

        return result, metering

    def _start_memory_monitoring(self):
        """
        Start background memory monitoring that logs usage every 5 seconds.

        Returns:
            Event object that can be set to stop monitoring
        """
        import threading

        shutdown_event = threading.Event()

        def monitor_memory():
            while not shutdown_event.is_set():
                try:
                    import os

                    import psutil

                    process = psutil.Process(os.getpid())
                    memory_info = process.memory_info()
                    memory_mb = memory_info.rss / (1024 * 1024)  # Convert to MB

                    logger.info(f"Memory usage: {memory_mb:.1f} MB")

                    # Warning if memory usage is getting high
                    if memory_mb > 3500:
                        logger.warning(
                            f"HIGH memory usage detected: {memory_mb:.1f} MB"
                        )

                except ImportError:
                    logger.debug("psutil not available, skipping memory monitoring")
                    break
                except Exception as e:
                    logger.debug(f"Error monitoring memory: {str(e)}")

                # Wait 5 seconds or until shutdown
                shutdown_event.wait(5.0)

        # Start monitoring thread
        monitor_thread = threading.Thread(target=monitor_memory, daemon=True)
        monitor_thread.start()

        return shutdown_event

    def _process_single_page_textract(
        self,
        page_index: int,
        pdf_document: pdfium.PdfDocument,
        output_bucket: str,
        prefix: str,
    ) -> Tuple[Dict[str, str], Dict[str, Any]]:
        """
        Process a single page using AWS Textract.

        Args:
            page_index: Zero-based index of the page
            pdf_document: pypdfium2 document object
            output_bucket: S3 bucket to store results
            prefix: S3 prefix for storing results

        Returns:
            Tuple of (page_result_dict, metering_data)
        """
        t0 = time.time()
        page_id = page_index + 1

        # Extract page image - now returns image at optimal size directly
        page = pdf_document[page_index]
        img_bytes = self._extract_page_image(page, True, page_id)

        # Upload processed image to S3 (already at target size if resize config exists)
        image_key = f"{prefix}/pages/{page_id}/image.jpg"
        s3.write_content(img_bytes, output_bucket, image_key, content_type="image/jpeg")

        t1 = time.time()
        logger.debug(
            f"Time for image processing (page {page_id}): {t1 - t0:.6f} seconds"
        )

        # Use the extracted image directly for OCR (no additional resize needed)
        ocr_img_bytes = img_bytes

        # Apply preprocessing if enabled (only for OCR processing, not saved image)
        if self.preprocessing_config and self.preprocessing_config.get("enabled"):
            from idp_common.image import apply_adaptive_binarization

            ocr_img_bytes = apply_adaptive_binarization(ocr_img_bytes)
            logger.debug(
                f"Applied adaptive binarization preprocessing for OCR processing (page {page_id})"
            )

        # Process with OCR using potentially resized image
        if isinstance(self.enhanced_features, list) and self.enhanced_features:
            textract_result = self._analyze_document(ocr_img_bytes, page_id)
        else:
            textract_result = self.textract_client.detect_document_text(
                Document={"Bytes": ocr_img_bytes}
            )

        # Aggressive memory cleanup - clear large image variables immediately after OCR
        img_bytes = None
        ocr_img_bytes = None

        # Force garbage collection after processing large images
        import gc

        gc.collect()

        # Extract metering data
        feature_combo = self._feature_combo()
        metering = {
            f"OCR/textract/{self._get_api_name()}{feature_combo}": {
                "pages": textract_result["DocumentMetadata"]["Pages"]
            }
        }

        # Store raw Textract response
        raw_text_key = f"{prefix}/pages/{page_id}/rawText.json"
        s3.write_content(
            textract_result,
            output_bucket,
            raw_text_key,
            content_type="application/json",
        )

        # Generate and store text confidence data for efficient assessment
        text_confidence_data = self._generate_text_confidence_data(textract_result)
        text_confidence_key = f"{prefix}/pages/{page_id}/textConfidence.json"
        s3.write_content(
            text_confidence_data,
            output_bucket,
            text_confidence_key,
            content_type="application/json",
        )

        # Parse and store text content with markdown
        parsed_result = self._parse_textract_response(textract_result, page_id)
        parsed_text_key = f"{prefix}/pages/{page_id}/result.json"
        s3.write_content(
            parsed_result,
            output_bucket,
            parsed_text_key,
            content_type="application/json",
        )

        # Persist consolidated OCR page data (text + confidence + geometry)
        page_data_uri = self._write_page_data(
            page_id,
            output_bucket,
            prefix,
            textract_result,
            parsed_result.get("text", ""),
            "textract",
        )

        t2 = time.time()
        logger.debug(f"Time for Textract (page {page_id}): {t2 - t1:.6f} seconds")

        # Create and return page result
        result = {
            "raw_text_uri": f"s3://{output_bucket}/{raw_text_key}",
            "parsed_text_uri": f"s3://{output_bucket}/{parsed_text_key}",
            "text_confidence_uri": f"s3://{output_bucket}/{text_confidence_key}",
            "ocr_page_data_uri": page_data_uri,
            "image_uri": f"s3://{output_bucket}/{image_key}",
        }

        return result, metering

    def _extract_page_image(
        self, page: pdfium.PdfPage, is_pdf: bool, page_id: int
    ) -> bytes:
        """
        Extract image bytes from a page at optimal size to prevent memory issues.

        If resize config is provided, images are extracted directly at target dimensions
        to avoid creating oversized images that cause OutOfMemory errors.

        Args:
            page: pypdfium2 page object
            is_pdf: Whether the document is a PDF file
            page_id: Page number for logging

        Returns:
            Image bytes in JPEG format (at target size if resize config exists)
        """
        pil_img = None
        try:
            # Check if we should extract at target size to avoid memory issues
            if self.resize_config:
                target_width = self.resize_config.get("target_width")
                target_height = self.resize_config.get("target_height")

                if target_width and target_height:
                    # Get page dimensions to calculate scaling
                    # pypdfium2 page dimensions are in PDF points (1/72 inch)
                    page_width = page.get_width()
                    page_height = page.get_height()

                    if is_pdf:
                        # For PDF files, calculate dimensions at specified DPI
                        dpi = self.dpi or DEFAULT_DPI
                        original_width = int(page_width * (dpi / 72))
                        original_height = int(page_height * (dpi / 72))
                    else:
                        # For image files, use actual dimensions
                        original_width = int(page_width)
                        original_height = int(page_height)

                    # Apply same logic as image.resize_image - preserve aspect ratio, never upscale
                    width_ratio = target_width / original_width
                    height_ratio = target_height / original_height
                    scale_factor = min(
                        width_ratio, height_ratio
                    )  # Preserve aspect ratio

                    # Only resize if scale_factor < 1.0 (never upscale)
                    if scale_factor < 1.0:
                        # Extract at reduced size using matrix transformation
                        if is_pdf:
                            # For PDF, combine DPI scaling with size reduction
                            dpi = self.dpi or DEFAULT_DPI
                            base_scale = dpi / 72  # Convert PDF points to pixels
                            final_scale = base_scale * scale_factor
                            matrix = final_scale
                        else:
                            # For images, just apply the scale factor
                            matrix = scale_factor

                        pil_img = page.render(scale=matrix).to_pil()  # type: ignore[attr-defined]

                        actual_width, actual_height = pil_img.size
                        logger.info(
                            f"Extracted page {page_id} at target size: {actual_width}x{actual_height} (scale: {scale_factor:.3f})"
                        )

                    else:
                        # No resize needed - image is already smaller than targets
                        if is_pdf:
                            dpi = self.dpi or DEFAULT_DPI
                            pil_img = page.render(scale=dpi / 72).to_pil()  # type: ignore[attr-defined]
                        else:
                            pil_img = page.render().to_pil()  # type: ignore[attr-defined]

                        # Log actual extracted dimensions
                        actual_width, actual_height = pil_img.size
                        logger.info(
                            f"Page {page_id} already fits target size, extracted at: {actual_width}x{actual_height}"
                        )
                else:
                    # No valid target dimensions - use original extraction
                    if is_pdf:
                        dpi = self.dpi or DEFAULT_DPI
                        pil_img = page.render(scale=dpi / 72).to_pil()  # type: ignore[attr-defined]
                    else:
                        pil_img = page.render().to_pil()  # type: ignore[attr-defined]

                    # Log actual extracted dimensions
                    actual_width, actual_height = pil_img.size
                    logger.info(
                        f"Page {page_id} extracted at original size: {actual_width}x{actual_height}"
                    )
            else:
                # No resize config - extract at original size
                if is_pdf:
                    dpi = self.dpi or DEFAULT_DPI
                    pil_img = page.render(scale=dpi / 72).to_pil()  # type: ignore[attr-defined]
                else:
                    pil_img = page.render().to_pil()  # type: ignore[attr-defined]

                # Log actual extracted dimensions
                actual_width, actual_height = pil_img.size
                logger.info(
                    f"Page {page_id} extracted at original size: {actual_width}x{actual_height}"
                )

            img_buffer = io.BytesIO()
            pil_img.save(img_buffer, format="JPEG", quality=95)
            image_bytes = img_buffer.getvalue()
            return image_bytes
        finally:
            # Cleanup pypdfium2 page rendering resources
            if pil_img is not None:
                pil_img = None

    def _process_single_page_bedrock(
        self,
        page_index: int,
        pdf_document: pdfium.PdfDocument,
        output_bucket: str,
        prefix: str,
    ) -> Tuple[Dict[str, str], Dict[str, Any]]:
        """
        Process a single page using Amazon Bedrock LLM.

        Args:
            page_index: Zero-based index of the page
            pdf_document: pypdfium2 document object
            output_bucket: S3 bucket to store results
            prefix: S3 prefix for storing results

        Returns:
            Tuple of (page_result_dict, metering_data)
        """
        t0 = time.time()
        page_id = page_index + 1

        # Extract page image - now returns image at optimal size directly
        page = pdf_document[page_index]
        img_bytes = self._extract_page_image(page, True, page_id)

        # Upload processed image to S3 (already at target size if resize config exists)
        image_key = f"{prefix}/pages/{page_id}/image.jpg"
        s3.write_content(img_bytes, output_bucket, image_key, content_type="image/jpeg")

        t1 = time.time()
        logger.debug(
            f"Time for image processing (page {page_id}): {t1 - t0:.6f} seconds"
        )

        # Use the extracted image directly for OCR (no additional resize needed)
        ocr_img_bytes = img_bytes

        # Apply preprocessing if enabled (only for OCR processing, not saved image)
        if self.preprocessing_config and self.preprocessing_config.get("enabled"):
            from idp_common.image import apply_adaptive_binarization

            ocr_img_bytes = apply_adaptive_binarization(ocr_img_bytes)
            logger.debug(
                f"Applied adaptive binarization preprocessing for Bedrock OCR processing (page {page_id})"
            )

        # Prepare image for Bedrock
        image_content = image.prepare_bedrock_image_attachment(ocr_img_bytes)

        # Prepare content for Bedrock
        content = [{"text": self.bedrock_config["task_prompt"]}, image_content]

        # Invoke Bedrock
        response_with_metering = bedrock.invoke_model(
            model_id=self.bedrock_config["model_id"],
            system_prompt=self.bedrock_config["system_prompt"],
            content=content,
            temperature=0.0,  # Use lowest temperature for OCR accuracy
            top_p=0.1,
            top_k=5,
            # None => Bedrock client resolves the model's max output tokens
            # (model_config_limits.yaml); Bedrock's default-when-omitted truncates.
            max_tokens=None,
            reasoning_effort=getattr(self.config.ocr, "reasoning_effort", None)
            if hasattr(self, "config")
            else None,
            context="OCR",
            model_lambda_hook_arn=getattr(
                self.config.ocr, "model_lambda_hook_arn", None
            )
            if hasattr(self, "config")
            else None,
        )

        # Extract text from response
        extracted_text = bedrock.extract_text_from_response(response_with_metering)
        metering = response_with_metering.get("metering", {})

        t2 = time.time()
        logger.debug(f"Time for Bedrock OCR (page {page_id}): {t2 - t1:.6f} seconds")

        # Persist structured OCR (Textract blocks with confidence + geometry) if
        # the LambdaHook returned it; otherwise store raw text + placeholder.
        # LLM OCR via plain Bedrock models doesn't provide real confidence scores.
        raw_ocr_content, text_confidence_data = self._extract_bedrock_ocr_artifacts(
            response_with_metering["response"]
        )

        # Store raw Bedrock response (or Textract blocks)
        raw_text_key = f"{prefix}/pages/{page_id}/rawText.json"
        s3.write_content(
            raw_ocr_content,
            output_bucket,
            raw_text_key,
            content_type="application/json",
        )

        text_confidence_key = f"{prefix}/pages/{page_id}/textConfidence.json"
        s3.write_content(
            text_confidence_data,
            output_bucket,
            text_confidence_key,
            content_type="application/json",
        )

        # Store parsed text result
        parsed_result = {"text": extracted_text}
        parsed_text_key = f"{prefix}/pages/{page_id}/result.json"
        s3.write_content(
            parsed_result,
            output_bucket,
            parsed_text_key,
            content_type="application/json",
        )

        # Persist consolidated OCR page data (text + confidence + geometry)
        page_data_uri = self._write_page_data(
            page_id,
            output_bucket,
            prefix,
            raw_ocr_content,
            extracted_text,
            "bedrock",
        )

        # Create and return page result
        result = {
            "raw_text_uri": f"s3://{output_bucket}/{raw_text_key}",
            "parsed_text_uri": f"s3://{output_bucket}/{parsed_text_key}",
            "text_confidence_uri": f"s3://{output_bucket}/{text_confidence_key}",
            "ocr_page_data_uri": page_data_uri,
            "image_uri": f"s3://{output_bucket}/{image_key}",
        }

        return result, metering

    def _process_single_page_none(
        self,
        page_index: int,
        pdf_document: pdfium.PdfDocument,
        output_bucket: str,
        prefix: str,
    ) -> Tuple[Dict[str, str], Dict[str, Any]]:
        """
        Process a single page with no OCR (image-only processing).

        Args:
            page_index: Zero-based index of the page
            pdf_document: pypdfium2 document object
            output_bucket: S3 bucket to store results
            prefix: S3 prefix for storing results

        Returns:
            Tuple of (page_result_dict, metering_data)
        """
        t0 = time.time()
        page_id = page_index + 1

        # Extract page image at specified DPI (consistent with other backends)
        page = pdf_document[page_index]
        img_bytes = self._extract_page_image(page, True, page_id)

        # Upload image to S3
        image_key = f"{prefix}/pages/{page_id}/image.jpg"
        s3.write_content(img_bytes, output_bucket, image_key, content_type="image/jpeg")

        t1 = time.time()
        logger.debug(
            f"Time for image conversion (page {page_id}): {t1 - t0:.6f} seconds"
        )

        # Create empty OCR response structure for compatibility
        empty_ocr_response = {"DocumentMetadata": {"Pages": 1}, "Blocks": []}

        # Store empty raw OCR response
        raw_text_key = f"{prefix}/pages/{page_id}/rawText.json"
        s3.write_content(
            empty_ocr_response,
            output_bucket,
            raw_text_key,
            content_type="application/json",
        )

        # Generate minimal text confidence data (empty markdown table)
        text_confidence_data = {
            "text": "| Text | Confidence |\n|:-----|:------------|\n| *No OCR performed* | N/A |"
        }

        text_confidence_key = f"{prefix}/pages/{page_id}/textConfidence.json"
        s3.write_content(
            text_confidence_data,
            output_bucket,
            text_confidence_key,
            content_type="application/json",
        )

        # Store empty parsed text result
        parsed_result = {"text": ""}
        parsed_text_key = f"{prefix}/pages/{page_id}/result.json"
        s3.write_content(
            parsed_result,
            output_bucket,
            parsed_text_key,
            content_type="application/json",
        )

        # Persist consolidated OCR page data (empty - no OCR performed)
        page_data_uri = self._write_page_data(
            page_id,
            output_bucket,
            prefix,
            empty_ocr_response,
            "",
            "none",
        )

        t2 = time.time()
        logger.debug(
            f"Time for image-only processing (page {page_id}): {t2 - t1:.6f} seconds"
        )

        # No metering data for image-only processing
        metering = {}

        # Create and return page result
        result = {
            "raw_text_uri": f"s3://{output_bucket}/{raw_text_key}",
            "parsed_text_uri": f"s3://{output_bucket}/{parsed_text_key}",
            "text_confidence_uri": f"s3://{output_bucket}/{text_confidence_key}",
            "ocr_page_data_uri": page_data_uri,
            "image_uri": f"s3://{output_bucket}/{image_key}",
        }

        return result, metering

    def _analyze_document(
        self, document_bytes: bytes, page_id: int = None
    ) -> Dict[str, Any]:
        """
        Analyze document using enhanced Textract features.

        Args:
            document_bytes: Binary content of the document image
            page_id: Optional page number for logging purposes

        Returns:
            Textract API response
        """
        # Use specified feature types
        # Valid types are TABLES, FORMS, SIGNATURES, and LAYOUT
        # Note: QUERIES is not supported as it requires additional parameters

        # Features are already validated in __init__, so we can use them directly
        page_info = f" for page {page_id}" if page_id else ""
        logger.debug(
            f"Analyzing document{page_info} with features: {self.enhanced_features}"
        )

        try:
            response = self.textract_client.analyze_document(
                Document={"Bytes": document_bytes}, FeatureTypes=self.enhanced_features
            )

            # Log the types of response blocks received
            if logger.isEnabledFor(logging.DEBUG):
                block_types = {}
                for block in response.get("Blocks", []):
                    block_type = block.get("BlockType")
                    if block_type not in block_types:
                        block_types[block_type] = 0
                    block_types[block_type] += 1
                logger.debug(f"Received response with block types: {block_types}")

            return response

        except Exception as e:
            import traceback

            page_info = f" for page {page_id}" if page_id else ""
            logger.error(
                f"Error in _analyze_document{page_info} with features {self.enhanced_features}: {str(e)}\nStack trace:\n{traceback.format_exc()}"
            )
            raise

    def _get_api_name(self) -> str:
        """Get the name of the Textract API being used."""
        # If enhanced_features is a non-empty list, we're using analyze_document
        # Otherwise, we're using detect_document_text
        return (
            "analyze_document"
            if isinstance(self.enhanced_features, list) and self.enhanced_features
            else "detect_document_text"
        )

    def _extract_bedrock_ocr_artifacts(
        self, response_payload: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Determine what to persist as rawText.json and textConfidence.json for a
        Bedrock/LambdaHook OCR response.

        A LambdaHook OCR function may optionally return structured OCR results in
        Amazon Textract response format under a top-level "textractBlocks" key
        (an object with a "Blocks" list). When present, this carries per-line/word
        OCR confidence scores and bounding-box geometry, so we persist the
        Textract blocks as the raw OCR result and generate a real text-confidence
        table from them. This lets downstream assessment (the
        {OCR_TEXT_CONFIDENCE} placeholder) and the UI (geometry highlighting) use
        the same data as the native Textract backend.

        Hooks (or Bedrock models) that return only text fall back to the previous
        behavior: the raw response is stored as-is and a placeholder confidence
        table is written.

        Args:
            response_payload: The "response" object from invoke_model

        Returns:
            Tuple of (raw_ocr_content_to_store, text_confidence_data)
        """
        textract_blocks = (
            response_payload.get("textractBlocks")
            if isinstance(response_payload, dict)
            else None
        )
        if textract_blocks and textract_blocks.get("Blocks"):
            return textract_blocks, self._generate_text_confidence_data(textract_blocks)

        placeholder = {
            "text": "| Text | Confidence |\n|:-----|:------------|\n| *No confidence data available from LLM OCR* | N/A |"
        }
        return response_payload, placeholder

    def _ensure_bda_arns(self) -> None:
        """Lazily build the BDA profile ARN on first use.

        The project ARN must have been provided via config or the
        ``BDA_OCR_PROJECT_ARN`` env var (the stack provisions a per-stack BDA
        OCR project via a CloudFormation custom resource). We never create the
        project at runtime; if no ARN is available we raise a clear error.
        Serialized with a lock so parallel page workers build the profile ARN
        exactly once rather than racing.
        """
        if self.bda_project_arn and self._bda_profile_arn:
            return
        from idp_common.bda import bda_ocr

        with self._bda_arn_lock:
            if not self.bda_project_arn:
                raise ValueError(
                    "BDA OCR backend selected but no project ARN is available. "
                    "Set ocr.bda_project_arn in config or the BDA_OCR_PROJECT_ARN "
                    "environment variable (provisioned by the stack's BDA OCR "
                    "project custom resource). Use the Textract OCR backend "
                    "instead where this project cannot be provisioned: regions "
                    "without Bedrock Data Automation, and the GovCloud/China "
                    "partitions — which do offer BDA, but reject the SYNC "
                    "document-modality project this backend requires, so the "
                    "stack deliberately does not create it there."
                )
            if not self._bda_profile_arn:
                identity = boto3.client(
                    "sts", region_name=self.region
                ).get_caller_identity()
                account_id = identity["Account"]
                # Derive the partition from the caller ARN (arn:<partition>:...)
                # so the profile ARN is correct in GovCloud / China partitions.
                partition = identity["Arn"].split(":")[1]
                self._bda_profile_arn = bda_ocr.build_profile_arn(
                    self.region, account_id, partition
                )

    @staticmethod
    def _image_size_from_bytes(
        img_bytes: Optional[bytes],
    ) -> Optional[Tuple[int, int]]:
        """Return ``(width, height)`` of image bytes, or None if undecodable.

        Used to give the BDA converter the original page-image dimensions so it
        can rescale rectification corners into original-image space.
        """
        if not img_bytes:
            return None
        try:
            from PIL import Image as _PILImage

            with _PILImage.open(io.BytesIO(img_bytes)) as im:
                return im.size
        except Exception:
            logger.warning("Could not determine page image size for BDA geometry")
            return None

    def _run_bda_ocr(
        self,
        image_s3_uri: str,
        original_image_size: Optional[Tuple[int, int]] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any], str, Dict[str, Any]]:
        """Run BDA standard-output OCR on a single page image.

        Invokes the synchronous ``InvokeDataAutomation`` API on the page image
        already uploaded to S3 (sync is capped at ~10 pages, so we always send a
        single page), converts the standard output to Textract-format blocks,
        and derives the confidence table.

        The image is passed by ``s3Uri`` (not inline ``bytes``) deliberately: the
        project's modality-routing override (``jpeg``/``png`` -> ``DOCUMENT``)
        keys off the file extension, so an extension-bearing S3 URI guarantees
        document text extraction. Inline bytes carry no extension and BDA falls
        back to content-based classification, which under concurrent load
        misclassifies some pages as IMAGE and yields empty OCR.

        Args:
            image_s3_uri: S3 URI of the page image to OCR.
            original_image_size: ``(width, height)`` of that image, passed to the
                converter so BDA's rectification corners (normalized against the
                rectified crop) are rescaled into original-image space. Without
                it, boxes are misplaced when BDA rectifies to a page sub-region.

        Returns:
            Tuple of (raw_ocr_blocks, text_confidence_data, parsed_text, metering).
        """
        from idp_common.bda import bda_ocr

        self._ensure_bda_arns()

        response = self.bda_runtime_client.invoke_data_automation(
            inputConfiguration={"s3Uri": image_s3_uri},
            dataAutomationConfiguration={
                "dataAutomationProjectArn": self.bda_project_arn,
                "stage": "LIVE",
            },
            dataAutomationProfileArn=self._bda_profile_arn,
        )

        segments = response.get("outputSegments", [])
        standard_output = segments[0]["standardOutput"] if segments else {}
        if isinstance(standard_output, str):
            standard_output = json.loads(standard_output)

        raw_ocr_blocks = bda_ocr.bda_standard_output_to_textract_blocks(
            standard_output, original_image_size=original_image_size
        )
        parsed_text = bda_ocr.extract_markdown(standard_output)
        text_confidence_data = self._generate_text_confidence_data(raw_ocr_blocks)
        # Metering key maps to the pricing.yaml entry `bda/documents-standard`
        # ($0.01/page) via reporting/save_reporting_data.py::_get_unit_cost.
        metering = {"OCR/bda/documents-standard": {"pages": 1}}

        return raw_ocr_blocks, text_confidence_data, parsed_text, metering

    def _write_bda_page_artifacts(
        self,
        image_s3_uri: str,
        output_bucket: str,
        prefix: str,
        page_id: int,
        original_image_size: Optional[Tuple[int, int]] = None,
    ) -> Tuple[Dict[str, Any], str, str, str, str, Dict[str, Any]]:
        """Run BDA OCR for a page image (in S3) and persist artifacts.

        ``original_image_size`` (``(width, height)`` of the page image) is
        forwarded to the converter so BDA rectification corners are rescaled into
        original-image space (see ``_run_bda_ocr``).

        Returns (page_data_raw, page_data_text, raw_text_key, text_confidence_key,
        parsed_text_key, metering) so callers can build page_data + result dict.
        """
        raw_ocr_blocks, text_confidence_data, parsed_text, metering = self._run_bda_ocr(
            image_s3_uri, original_image_size=original_image_size
        )

        raw_text_key = f"{prefix}/pages/{page_id}/rawText.json"
        s3.write_content(
            raw_ocr_blocks, output_bucket, raw_text_key, content_type="application/json"
        )

        text_confidence_key = f"{prefix}/pages/{page_id}/textConfidence.json"
        s3.write_content(
            text_confidence_data,
            output_bucket,
            text_confidence_key,
            content_type="application/json",
        )

        parsed_text_key = f"{prefix}/pages/{page_id}/result.json"
        s3.write_content(
            {"text": parsed_text},
            output_bucket,
            parsed_text_key,
            content_type="application/json",
        )

        return (
            raw_ocr_blocks,
            parsed_text,
            raw_text_key,
            text_confidence_key,
            parsed_text_key,
            metering,
        )

    def _generate_text_confidence_data(
        self, raw_ocr_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Generate text confidence data from raw OCR to reduce token usage while preserving essential information.

        This method transforms verbose Textract output into a markdown table format containing:
        - Essential text content (LINE blocks only)
        - OCR confidence scores (rounded to 1 decimal point)

        Removes geometric data, relationships, block IDs, and other verbose metadata
        that aren't needed for assessment purposes.

        Args:
            raw_ocr_data: Raw Textract API response

        Returns:
            Text confidence data as markdown table with ~80-90% token reduction
        """
        # Start building the markdown table with explicit left alignment
        markdown_lines = ["| Text | Confidence |", "|:-----|:-----------|"]

        blocks = raw_ocr_data.get("Blocks", [])

        for block in blocks:
            if block.get("BlockType") == "LINE" and block.get("Text"):
                text = block.get("Text", "").replace(
                    "|", "\\|"
                )  # Escape pipe characters
                confidence = round(block.get("Confidence", 0.0), 1)

                # Add text type indicator if it's handwriting
                if block.get("TextType") == "HANDWRITING":
                    markdown_lines.append(f"| {text} (HANDWRITING) | {confidence} |")
                else:
                    markdown_lines.append(f"| {text} | {confidence} |")

        # Join all lines into a single markdown string
        markdown_table = "\n".join(markdown_lines)

        # Signature detections are their own block type with no Text, so the
        # LINE-only loop above skips them entirely — which meant the SIGNATURES
        # feature's output (and, critically, its confidence) never reached the
        # confidence/assessment prompt that consumes this artifact.
        signature_summary = self._format_signature_summary(
            self._extract_signature_detections(blocks)
        )
        if signature_summary:
            markdown_table = f"{markdown_table}\n\n{signature_summary}"

        return {"text": markdown_table}

    @staticmethod
    def _extract_signature_detections(
        blocks: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Pull SIGNATURE detections out of Textract-format blocks.

        Emitted only when the OCR backend was asked for the ``SIGNATURES``
        feature. Each detection carries a confidence and geometry but no text, so
        every LINE-oriented consumer drops it unless handled explicitly.

        Args:
            blocks: Textract-format block list.

        Returns:
            ``[{"id", "confidence", "geometry"}, ...]`` in block order, where
            ``confidence`` is 0-100 rounded to 1dp (or None) and ``geometry`` is
            the normalized 0-1 dict from :meth:`_extract_geometry` (or None).
        """
        signatures: List[Dict[str, Any]] = []
        for index, block in enumerate(blocks or [], start=1):
            if not isinstance(block, dict) or block.get("BlockType") != "SIGNATURE":
                continue
            confidence = block.get("Confidence")
            if confidence is not None:
                confidence = round(confidence, 1)
            signatures.append(
                {
                    "id": block.get("Id") or f"sig{index}",
                    "confidence": confidence,
                    "geometry": OcrService._extract_geometry(block),
                }
            )
        return signatures

    # Detection-confidence bands used to word the signature summary. Textract's
    # signature confidence is a detection score, not a "how much like a signature"
    # score, so a very low value routinely means a stray pen mark or scan artifact.
    _SIGNATURE_CONFIDENCE_BANDS = (
        (25.0, "very low"),
        (50.0, "low"),
        (75.0, "moderate"),
    )

    @staticmethod
    def _describe_signature_position(box: Dict[str, Any]) -> str:
        """
        Describe a normalized box in the left/right, upper/lower terms schemas use.

        Field descriptions say things like "the taxpayer 1 signature on the left
        lower side of the page", so a bare ``left=0.572`` forces the consumer to do
        the spatial mapping itself — which is exactly where it goes wrong on a form
        with two identically-labelled signature cells.
        """
        center_x = box.get("left", 0.0) + box.get("width", 0.0) / 2
        center_y = box.get("top", 0.0) + box.get("height", 0.0) / 2

        half = "left half" if center_x < 0.5 else "right half"
        if center_y < 0.33:
            band = "upper area"
        elif center_y < 0.66:
            band = "middle area"
        else:
            band = "lower area"

        return f"{half}, {band} (x={center_x:.0%}, y={center_y:.0%})"

    @classmethod
    def _format_signature_summary(cls, signatures: List[Dict[str, Any]]) -> str:
        """
        Render signature detections as a compact, self-describing text block.

        Appended to both the page text and the text-confidence artifact so the
        extraction and confidence prompts see *which* signature regions were
        detected, *where*, and with *what* confidence.

        The inline ``[SIGNATURE]`` token the linearizer emits carries none of that,
        and is worse than silent: its position comes from reading order, so on an
        IRS Form 4549 it landed immediately after the taxpayer-1 date while the
        detection itself was in the taxpayer-2 cell. Both the extraction and the
        confidence model then attributed the mark to the wrong taxpayer ("the OCR
        detected a signature region in the first (left) signature box" for a box at
        x=57%). Hence: plain-language position, an explicit total, and a caveat that
        the inline token's placement is not evidence of field membership.

        WHAT IS DELIBERATELY *NOT* HERE: an earlier version also named the OCR text
        surrounding each detection ("at: 'Signature of taxpayer'; right: 'Date'").
        It reads as more helpful and measured worse — naming a signature *label*
        beside the mark biases the model toward true. On the issue-#634 document with
        a threshold rule in the field descriptions, the entry below passed 9/9 while
        the same entry plus that context passed 2/5. Do not re-add it without
        measuring.

        Deliberately NOT a markdown table either — page text is scanned by the
        agentic extraction table parser, which would otherwise pick this up as a
        document table.

        Args:
            signatures: Output of :meth:`_extract_signature_detections`.

        Returns:
            The summary block, or "" when there are no detections.
        """
        if not signatures:
            return ""

        total = len(signatures)
        plural = "" if total == 1 else "s"
        lines = [
            "--- OCR signature detections ---",
            f"The OCR engine flagged {total} region{plural} on this page as "
            f"containing a signature, listed below with its page position. No "
            f"signature was detected anywhere else on the page. Confidence is the engine's signature-DETECTION confidence "
            f"(0-100): a low value means a faint or ambiguous mark, which may be a "
            f"stray pen mark or scan artifact rather than a signature. NOTE: the "
            f"inline [SIGNATURE] token in the text above is placed by reading "
            f"order, NOT at the detected location — use the positions here to "
            f"decide which field a signature belongs to.",
        ]
        for index, sig in enumerate(signatures, start=1):
            confidence = sig.get("confidence")
            if confidence is None:
                confidence_text = "unknown"
            else:
                band = next(
                    (
                        label
                        for limit, label in cls._SIGNATURE_CONFIDENCE_BANDS
                        if confidence < limit
                    ),
                    "high",
                )
                confidence_text = f"{confidence} ({band})"

            box = (sig.get("geometry") or {}).get("boundingBox") or {}
            position = cls._describe_signature_position(box) if box else "unknown"
            lines.append(
                f"signature {index}: confidence={confidence_text} "
                f"— page position: {position}"
            )
        return "\n".join(lines)

    # Current consolidated OCR page-data schema version. Bump when the shape of
    # pageData.json changes in a backward-incompatible way.
    PAGE_DATA_SCHEMA_VERSION = 1

    @staticmethod
    def _extract_geometry(block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Extract normalized (0-1) geometry from a Textract-format block.

        Returns a dict with a ``boundingBox`` ({left, top, width, height}) and an
        optional ``polygon`` ([{x, y}, ...]), or None when the block carries no
        geometry (e.g. Mistral WORD blocks, LLM OCR).
        """
        geom = block.get("Geometry")
        if not isinstance(geom, dict):
            return None

        bbox = geom.get("BoundingBox")
        if not isinstance(bbox, dict):
            return None

        result: Dict[str, Any] = {
            "boundingBox": {
                "left": bbox.get("Left", 0.0),
                "top": bbox.get("Top", 0.0),
                "width": bbox.get("Width", 0.0),
                "height": bbox.get("Height", 0.0),
            }
        }

        polygon = geom.get("Polygon")
        if isinstance(polygon, list) and polygon:
            result["polygon"] = [
                {"x": pt.get("X", 0.0), "y": pt.get("Y", 0.0)}
                for pt in polygon
                if isinstance(pt, dict)
            ]

        return result

    def _build_page_data(
        self,
        raw_ocr_content: Dict[str, Any],
        parsed_text: str,
        provider_hint: str,
    ) -> Dict[str, Any]:
        """
        Build a consolidated, backend-agnostic per-page OCR data structure.

        Consolidates text + confidence + geometry (which today live in three
        separate artifacts at backend-dependent granularities) into a single
        LINE-primary schema with optional WORD children. ``confidence`` and
        ``geometry`` are *independently optional* on every unit, so each backend
        contributes whatever it has:

        - Textract: per-LINE and per-WORD confidence + geometry (box + polygon).
        - Mistral LambdaHook: per-LINE/WORD confidence; geometry is paragraph-level
          (sibling lines share a box, flagged via ``geometrySource: "paragraph"``);
          WORD blocks have no geometry.
        - Chandra / plain Bedrock LLM / none: text only (lines synthesized from
          the parsed markdown), no confidence or geometry.

        Geometry is normalized 0-1 (Textract convention), matching what the UI
        bounding-box renderer already consumes.

        Signature detections (Textract ``SIGNATURES`` feature) are carried in a
        separate ``signatures`` list rather than as pseudo-lines: they have
        confidence and geometry but no text, so mixing them into ``lines`` would
        expose them to the value-matching geometry grounder as if they were text.

        Args:
            raw_ocr_content: Textract-format dict ({"Blocks": [...]}) when the
                backend produced structured OCR, otherwise the raw response.
            parsed_text: The parsed markdown/plain text for this page (used to
                synthesize lines when no structured blocks are available).
            provider_hint: One of "textract", "bedrock", "none", "converted".

        Returns:
            The consolidated pageData dict.
        """
        blocks = (
            raw_ocr_content.get("Blocks", [])
            if isinstance(raw_ocr_content, dict)
            else []
        )
        has_blocks = isinstance(blocks, list) and len(blocks) > 0

        # Resolve the provenance tag. Bedrock splits into LambdaHook (structured
        # blocks present) vs. plain LLM OCR (text only).
        if provider_hint == "bedrock":
            provider = "bedrock-lambdahook" if has_blocks else "bedrock-llm"
        else:
            provider = provider_hint

        lines: List[Dict[str, Any]] = []
        confidence_available = False
        geometry_available = False
        words_available = False

        if has_blocks:
            # Index blocks by Id so LINE -> WORD CHILD relationships resolve.
            blocks_by_id = {b.get("Id"): b for b in blocks if b.get("Id")}

            # Detect paragraph-shared geometry: a bounding box reused by more than
            # one LINE indicates the backend (e.g. Mistral) only has paragraph-level
            # geometry, so we flag those lines as geometrySource="paragraph".
            box_counts: Dict[tuple, int] = {}
            for b in blocks:
                if b.get("BlockType") == "LINE":
                    g = self._extract_geometry(b)
                    if g:
                        bb = g["boundingBox"]
                        key = (bb["left"], bb["top"], bb["width"], bb["height"])
                        box_counts[key] = box_counts.get(key, 0) + 1

            line_idx = 0
            for block in blocks:
                if block.get("BlockType") != "LINE" or not block.get("Text"):
                    continue
                line_idx += 1

                geometry = self._extract_geometry(block)
                confidence = block.get("Confidence")
                if confidence is not None:
                    confidence = round(confidence, 1)
                    confidence_available = True

                if geometry:
                    geometry_available = True
                    bb = geometry["boundingBox"]
                    key = (bb["left"], bb["top"], bb["width"], bb["height"])
                    geometry_source = (
                        "paragraph" if box_counts.get(key, 0) > 1 else "line"
                    )
                else:
                    geometry_source = "none"

                # Resolve WORD children via CHILD relationships.
                words: List[Dict[str, Any]] = []
                for rel in block.get("Relationships", []) or []:
                    if rel.get("Type") != "CHILD":
                        continue
                    for child_id in rel.get("Ids", []) or []:
                        child = blocks_by_id.get(child_id)
                        if not child or child.get("BlockType") != "WORD":
                            continue
                        word_geom = self._extract_geometry(child)
                        word_conf = child.get("Confidence")
                        if word_conf is not None:
                            word_conf = round(word_conf, 1)
                        if word_geom:
                            geometry_available = True
                        words.append(
                            {
                                "text": child.get("Text", ""),
                                "confidence": word_conf,
                                "geometry": word_geom,
                            }
                        )
                if words:
                    words_available = True

                lines.append(
                    {
                        "id": block.get("Id") or f"l{line_idx}",
                        "text": block.get("Text", ""),
                        "confidence": confidence,
                        "geometry": geometry,
                        "geometrySource": geometry_source,
                        "textType": block.get("TextType"),
                        "words": words or None,
                    }
                )
        else:
            # No structured blocks (LLM/Chandra/none): synthesize text-only lines
            # from the parsed markdown so the viewer can still list page text.
            for idx, raw_line in enumerate((parsed_text or "").split("\n"), start=1):
                if not raw_line.strip():
                    continue
                lines.append(
                    {
                        "id": f"l{idx}",
                        "text": raw_line,
                        "confidence": None,
                        "geometry": None,
                        "geometrySource": "none",
                        "textType": None,
                        "words": None,
                    }
                )

        signatures = self._extract_signature_detections(blocks)
        if signatures and any(sig.get("geometry") for sig in signatures):
            geometry_available = True

        return {
            "schemaVersion": self.PAGE_DATA_SCHEMA_VERSION,
            "provider": provider,
            "page": None,
            "geometryAvailable": geometry_available,
            "confidenceAvailable": confidence_available,
            "wordsAvailable": words_available,
            "signaturesAvailable": bool(signatures),
            "lines": lines,
            "signatures": signatures,
        }

    def _write_page_data(
        self,
        page_id: int,
        output_bucket: str,
        prefix: str,
        raw_ocr_content: Dict[str, Any],
        parsed_text: str,
        provider_hint: str,
    ) -> str:
        """
        Build and persist the consolidated pageData.json artifact for a page.

        Returns the s3:// URI of the written artifact. Page processors add this to
        their result dict as ``ocr_page_data_uri``.
        """
        page_data = self._build_page_data(raw_ocr_content, parsed_text, provider_hint)
        page_data_key = f"{prefix}/pages/{page_id}/pageData.json"
        s3.write_content(
            page_data,
            output_bucket,
            page_data_key,
            content_type="application/json",
        )
        return f"s3://{output_bucket}/{page_data_key}"

    def _parse_textract_response(
        self, response: Dict[str, Any], page_id: int = None
    ) -> Dict[str, str]:
        """
        Parse Textract response into text.

        Args:
            response: Raw Textract API response
            page_id: Optional page number for logging purposes

        Returns:
            Dictionary with 'text' key containing extracted text
        """
        from textractor.parsers import response_parser  # type: ignore[import-untyped]

        # Create page identifier for logging
        page_info = f" for page {page_id}" if page_id else ""

        # Log enhanced features at debug level
        logger.debug(f"Enhanced features{page_info}: {self.enhanced_features}")

        try:
            # Parse the response with textractor - debug level
            logger.debug(f"Parsing Textract response{page_info} with textractor")
            parsed_response = response_parser.parse(response)

            try:
                # First try to convert to markdown
                text = parsed_response.to_markdown()
                logger.info(f"Successfully extracted markdown text{page_info}")
            except Exception as e:
                # If markdown conversion fails, use plain text instead
                logger.warning(f"Markdown conversion failed{page_info}: {str(e)}")

                # Identify if it's a known issue - keep these as warnings
                if "reading_order" in str(e):
                    if "Signature" in str(e):
                        logger.warning(
                            f"Detected Signature object error{page_info} with SIGNATURES feature"
                        )
                    elif "KeyValue" in str(e):
                        logger.warning(
                            f"Detected KeyValue object error{page_info} with FORMS feature"
                        )

                # Use plain text instead
                logger.warning(f"Falling back to plain text extraction{page_info}")
                text = parsed_response.text
                logger.info(f"Successfully extracted plain text{page_info}")

        except Exception as e:
            # If parsing completely fails, extract text directly from blocks
            logger.warning(f"Textractor parsing failed{page_info}: {str(e)}")

            # Simple extraction from LINE blocks as final fallback
            logger.warning(
                f"Falling back to basic text extraction from blocks{page_info}"
            )
            blocks = response.get("Blocks", [])

            text_lines = []
            for block in blocks:
                if block.get("BlockType") == "LINE" and "Text" in block:
                    text_lines.append(block["Text"])

            text = "\n".join(text_lines)
            if not text:
                text = f"Error extracting text from document{page_info}. No text content found."
                logger.error(f"No text content found in document{page_info}")
            else:
                logger.info(f"Successfully extracted basic text{page_info}")

        # Append the signature detections. The linearizer renders each one as a
        # bare inline "[SIGNATURE]" token — placed by reading order, with no
        # confidence — so on its own it cannot be attributed to a specific field
        # (on a two-column signature block it lands beside the wrong one), and a
        # 10%-confidence smudge is indistinguishable from a real signature. The
        # summary supplies position, neighbouring text, and confidence instead.
        page_blocks = response.get("Blocks", [])
        signatures = self._extract_signature_detections(page_blocks)
        summary = self._format_signature_summary(signatures)
        if summary:
            logger.info(
                f"Appended {len(signatures)} signature detection(s){page_info} "
                f"to page text"
            )
            text = f"{text}\n\n{summary}"

        return {"text": text}

    def _detect_file_type(self, filename: str, content: bytes) -> str:
        """
        Detect file type based on filename extension and content.

        Args:
            filename: Name of the file
            content: File content bytes

        Returns:
            File type string
        """
        # Get file extension
        ext = filename.lower().split(".")[-1] if "." in filename else ""

        # Check for specific document types
        if ext == "txt":
            return "txt"
        elif ext == "csv":
            return "csv"
        elif ext in ["xlsx", "xls"]:
            return "xlsx"
        elif ext in ["docx", "doc"]:
            return "docx"
        elif ext in ["pdf"]:
            return "pdf"
        elif ext in ["jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif", "webp"]:
            return ext
        else:
            # Try to detect based on content
            if content.startswith(b"%PDF"):
                return "pdf"
            elif content.startswith(b"PK"):
                # Could be Excel or Word (both are ZIP-based)
                if b"xl/" in content[:1000]:
                    return "xlsx"
                elif b"word/" in content[:1000]:
                    return "docx"

            # Default to treating as text if we can decode it
            try:
                content.decode("utf-8")
                return "txt"
            except UnicodeDecodeError:
                pass

            # Default to PDF for unknown binary files
            return "pdf"

    def _ocr_image_bytes(self, img_bytes: bytes) -> str:
        """OCR raw image bytes and return extracted text.

        Used as a callback for DOCX image extraction so that images embedded
        in Word documents are processed through the configured OCR backend
        (Textract or Bedrock).

        Args:
            img_bytes: Raw image bytes (PNG, JPEG, etc.)

        Returns:
            Extracted text from the image.
        """
        if self.backend == "none":
            return "[Image]"

        try:
            if self.backend == "bedrock":
                # Prepare image for Bedrock
                image_content = image.prepare_bedrock_image_attachment(img_bytes)
                content = [
                    {"text": self.bedrock_config["task_prompt"]},
                    image_content,
                ]
                response_with_metering = bedrock.invoke_model(
                    model_id=self.bedrock_config["model_id"],
                    system_prompt=self.bedrock_config["system_prompt"],
                    content=content,
                    temperature=0.0,
                    top_p=0.1,
                    top_k=5,
                    # None => Bedrock client resolves the model's max output tokens
                    # (model_config_limits.yaml); Bedrock's default-when-omitted truncates.
                    max_tokens=None,
                    reasoning_effort=getattr(self.config.ocr, "reasoning_effort", None)
                    if hasattr(self, "config")
                    else None,
                    context="OCR",
                )
                return bedrock.extract_text_from_response(response_with_metering)

            else:
                # Textract backend (default)
                textract_result = self.textract_client.detect_document_text(
                    Document={"Bytes": img_bytes}
                )
                # Extract text from LINE blocks
                lines = []
                for block in textract_result.get("Blocks", []):
                    if block.get("BlockType") == "LINE" and "Text" in block:
                        lines.append(block["Text"])
                return "\n".join(lines)

        except Exception as e:
            logger.warning(f"Failed to OCR embedded image: {e}")
            return "[Image - OCR failed]"

    def _process_non_pdf_document(
        self, file_type: str, content: bytes
    ) -> List[Tuple[bytes, str]]:
        """
        Process non-PDF documents and convert to pages.

        Args:
            file_type: Type of the file
            content: File content bytes

        Returns:
            List of tuples (image_bytes, page_text)
        """
        try:
            if file_type == "txt":
                text_content = content.decode("utf-8")
                return self.document_converter.convert_text_to_pages(text_content)

            elif file_type == "csv":
                text_content = content.decode("utf-8")
                return self.document_converter.convert_csv_to_pages(text_content)

            elif file_type == "xlsx":
                return self.document_converter.convert_excel_to_pages(content)

            elif file_type == "docx":
                return self.document_converter.convert_word_to_pages(
                    content, ocr_image_callback=self._ocr_image_bytes
                )

            else:
                # Fallback to text
                try:
                    text_content = content.decode("utf-8")
                    return self.document_converter.convert_text_to_pages(text_content)
                except UnicodeDecodeError:
                    return [
                        (
                            self.document_converter._create_empty_page(),
                            "Error: Unable to process file",
                        )
                    ]

        except Exception as e:
            logger.error(f"Error processing {file_type} document: {str(e)}")
            return [
                (
                    self.document_converter._create_empty_page(),
                    f"Error processing {file_type} document",
                )
            ]

    def _process_converted_page(
        self,
        page_index: int,
        image_bytes: bytes,
        page_text: str,
        output_bucket: str,
        prefix: str,
    ) -> Tuple[Dict[str, str], Dict[str, Any]]:
        """
        Process a converted page (from non-PDF document).

        Args:
            page_index: Zero-based index of the page
            image_bytes: Page image bytes
            page_text: Extracted text for the page
            output_bucket: S3 bucket to store results
            prefix: S3 prefix for storing results

        Returns:
            Tuple of (page_result_dict, metering_data)
        """
        t0 = time.time()
        page_id = page_index + 1

        # Upload image to S3
        image_key = f"{prefix}/pages/{page_id}/image.jpg"
        s3.write_content(
            image_bytes, output_bucket, image_key, content_type="image/jpeg"
        )

        # Create OCR response structure for compatibility
        ocr_response = {
            "DocumentMetadata": {"Pages": 1},
            "Blocks": [
                {
                    "BlockType": "LINE",
                    "Text": line,
                    "Confidence": 99.0,
                    "TextType": "PRINTED",
                }
                for line in page_text.split("\n")
                if line.strip()
            ],
        }

        # Store raw OCR response
        raw_text_key = f"{prefix}/pages/{page_id}/rawText.json"
        s3.write_content(
            ocr_response,
            output_bucket,
            raw_text_key,
            content_type="application/json",
        )

        # Generate text confidence data as markdown table with explicit left alignment
        markdown_lines = ["| Text | Confidence |", "|:-----|:-----------|"]
        for line in page_text.split("\n"):
            if line.strip():
                # Escape pipe characters in text
                escaped_line = line.replace("|", "\\|")
                markdown_lines.append(f"| {escaped_line} | 99.0 |")

        markdown_table = "\n".join(markdown_lines)
        text_confidence_data = {"text": markdown_table}

        text_confidence_key = f"{prefix}/pages/{page_id}/textConfidence.json"
        s3.write_content(
            text_confidence_data,
            output_bucket,
            text_confidence_key,
            content_type="application/json",
        )

        # Store parsed text result
        parsed_result = {"text": page_text}
        parsed_text_key = f"{prefix}/pages/{page_id}/result.json"
        s3.write_content(
            parsed_result,
            output_bucket,
            parsed_text_key,
            content_type="application/json",
        )

        # Persist consolidated OCR page data (per-line placeholder confidence,
        # no geometry — built from the synthetic Textract-format blocks above)
        page_data_uri = self._write_page_data(
            page_id,
            output_bucket,
            prefix,
            ocr_response,
            page_text,
            "converted",
        )

        t1 = time.time()
        logger.debug(
            f"Time for converted page processing (page {page_id}): {t1 - t0:.6f} seconds"
        )

        # Minimal metering for converted documents
        metering = {"OCR/converted/document_conversion": {"pages": 1}}

        # Create and return page result
        result = {
            "raw_text_uri": f"s3://{output_bucket}/{raw_text_key}",
            "parsed_text_uri": f"s3://{output_bucket}/{parsed_text_key}",
            "text_confidence_uri": f"s3://{output_bucket}/{text_confidence_key}",
            "ocr_page_data_uri": page_data_uri,
            "image_uri": f"s3://{output_bucket}/{image_key}",
        }

        return result, metering
