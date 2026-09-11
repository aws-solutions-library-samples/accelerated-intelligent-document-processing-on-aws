# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Summarization service for documents using LLMs.

This module provides a service for summarizing documents using various backends:
1. Bedrock LLMs with text support

The service includes advanced markdown formatting capabilities for generated summaries,
including table of contents, citation formatting, and navigation aids.
"""

import concurrent.futures
import copy
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple, Union

from idp_common import bedrock, s3, utils
from idp_common.config.models import IDPConfig
from idp_common.models import Document, Status
from idp_common.summarization.markdown_formatter import SummaryMarkdownFormatter
from idp_common.summarization.models import DocumentSummarizationResult, DocumentSummary
from idp_common.utils import extract_json_from_text

logger = logging.getLogger(__name__)

# Rough chars-per-token estimate, matching idp_common.utils.check_token_limit and
# idp_common.extraction.sharding. Deliberately conservative (English prose is
# ~4 chars/token; dense numeric/tabular text can be lower, so this slightly
# *under*-counts — we compensate with a context-window safety buffer below).
_CHARS_PER_TOKEN = 4.0

# Fraction of the model's input window reserved for the system prompt, the
# static task-prompt boilerplate, and generation headroom. We only allow the
# variable payload (DOCUMENT_TEXT + EXTRACTION_RESULTS) to occupy the rest.
_CONTEXT_SAFETY_FRACTION = 0.85

# Fallback input window when model_config_limits.yaml can't resolve the model
# (unknown/misconfigured id). Nova/Claude base windows are >=200K, so 180K is a
# safe conservative floor.
_DEFAULT_MAX_INPUT_TOKENS = 180_000

# How many head/tail items to keep when eliding a large array.
_ELIDE_HEAD = 3
_ELIDE_TAIL = 2


def _estimate_tokens(text: str) -> int:
    """Estimate the token count of a string using a conservative chars/token ratio."""
    if not text:
        return 0
    return int(len(text) / _CHARS_PER_TOKEN)


def _elide_large_arrays(obj: Any, max_items: int) -> Any:
    """Recursively replace arrays longer than ``max_items`` with a head/tail
    sample plus a marker string.

    A statement/document summary needs counts and totals, not every row. The
    full pretty-printed extraction list is the dominant token term that
    overflows the model context window on large documents (e.g. a 6400-row
    table). This keeps enough example rows for the model to understand the
    shape of the data while collapsing the bulk.

    ``max_items <= 0`` disables elision (returns the object unchanged).
    """
    if max_items and max_items > 0:
        if isinstance(obj, list):
            if len(obj) > max_items:
                head = [_elide_large_arrays(x, max_items) for x in obj[:_ELIDE_HEAD]]
                tail = (
                    [_elide_large_arrays(x, max_items) for x in obj[-_ELIDE_TAIL:]]
                    if _ELIDE_TAIL
                    else []
                )
                marker = f"... ({len(obj)} items total, {len(obj) - _ELIDE_HEAD - _ELIDE_TAIL} elided)"
                return head + [marker] + tail
            return [_elide_large_arrays(x, max_items) for x in obj]
        if isinstance(obj, dict):
            return {k: _elide_large_arrays(v, max_items) for k, v in obj.items()}
    return obj


def _is_input_token_overflow(error: Exception) -> bool:
    """Bedrock input/context overflow — the shared matcher in ``bedrock_utils``."""
    from idp_common.utils.bedrock_utils import is_input_token_overflow

    return is_input_token_overflow(error)


class SummarizationService:
    """Service for summarizing documents using various backends."""

    def __init__(
        self,
        region: str = None,
        config: Union[Dict[str, Any], IDPConfig] = None,
        backend: str = "bedrock",
    ):
        """
        Initialize the summarization service.

        Args:
            region: AWS region for backend services
            config: Configuration dictionary or IDPConfig model
            backend: Summarization backend to use ('bedrock')
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
        self.backend = backend.lower()

        # Validate backend choice
        if self.backend != "bedrock":
            logger.warning(f"Invalid backend '{backend}', falling back to 'bedrock'")
            self.backend = "bedrock"

        # Initialize backend-specific clients
        if self.backend == "bedrock":
            # Get model_id from typed config for logging
            model_id = self.config.summarization.model
            if not model_id:
                raise ValueError("No model ID specified in configuration for Bedrock")
            self.bedrock_model = model_id
            logger.info(
                f"Initialized summarization service with Bedrock backend using model {model_id}"
            )
        else:
            raise ValueError(f"Unsupported backend: {self.backend}")

    def _get_summarization_config(self) -> Dict[str, Any]:
        """
        Get and validate the summarization configuration.

        Returns:
            Dict with validated summarization configuration parameters

        Raises:
            ValueError: If required configuration values are missing
        """
        # Type-safe access to summarization config (Pydantic handles conversions)
        config = {
            "model_id": self.bedrock_model,
            "temperature": self.config.summarization.temperature,
            "top_k": self.config.summarization.top_k,
            "top_p": self.config.summarization.top_p,
            "max_tokens": self.config.summarization.max_tokens,
            "reasoning_effort": self.config.summarization.reasoning_effort,
        }

        # Validate system prompt
        system_prompt = self.config.summarization.system_prompt
        if not system_prompt:
            raise ValueError("No system_prompt found in summarization configuration")

        config["system_prompt"] = system_prompt

        # Validate task prompt
        task_prompt = self.config.summarization.task_prompt
        if not task_prompt:
            raise ValueError("No task_prompt found in summarization configuration")

        config["task_prompt"] = task_prompt

        return config

    def _invoke_bedrock_model(
        self, content: List[Dict[str, Any]], config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Invoke Bedrock model with standard parameters.

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
            context="Summarization",
            model_lambda_hook_arn=self.config.summarization.model_lambda_hook_arn,
            reasoning_effort=config.get("reasoning_effort"),
        )

    def _create_error_summary(self, error_message: str) -> DocumentSummary:
        """
        Create a standard error summary with error information.

        Args:
            error_message: Error message to include in metadata

        Returns:
            DocumentSummary with error result
        """
        return DocumentSummary(
            content={"error": "Error generating summary"},
            metadata={"error": error_message},
        )

    def _get_max_input_tokens(self) -> int:
        """Resolve the model's input (context-window) token limit.

        Falls back to a conservative default if the model id can't be resolved
        against model_config_limits.yaml.
        """
        try:
            from idp_common.bedrock.model_utils import get_model_max_input_tokens

            return get_model_max_input_tokens(self.bedrock_model)
        except Exception as e:
            logger.warning(
                "Could not resolve max_input_tokens for model %s (%s); "
                "falling back to %d.",
                self.bedrock_model,
                e,
                _DEFAULT_MAX_INPUT_TOKENS,
            )
            return _DEFAULT_MAX_INPUT_TOKENS

    def _build_placeholders(
        self, text: str, extraction_results: Dict[str, Any], config: Dict[str, Any]
    ) -> Tuple[Dict[str, str], bool]:
        """Build the prompt placeholders, applying compaction/elision and a
        fit-or-truncate guard against the model's input window.

        Fix A: EXTRACTION_RESULTS is compact (no indent) and large arrays are
        elided to head/tail samples so the dominant token term is removed.

        Fix B: if the estimated prompt would still overflow the model window,
        DOCUMENT_TEXT is truncated (and EXTRACTION_RESULTS further collapsed)
        so the call fits instead of failing.

        Returns:
            (placeholders, truncated) where ``truncated`` is True if the payload
            had to be reduced to fit.
        """
        array_cap = self.config.summarization.max_extraction_array_items

        extraction_json = ""
        if extraction_results:
            elided = _elide_large_arrays(extraction_results, array_cap)
            # Compact (no indent=2) — Fix A removes the pretty-print token bloat.
            extraction_json = json.dumps(elided, default=str)

        # Fix B: fit-or-truncate guard. Reserve a fraction of the window for the
        # system prompt, task-prompt boilerplate, and output generation.
        max_input_tokens = self._get_max_input_tokens()
        budget_tokens = int(max_input_tokens * _CONTEXT_SAFETY_FRACTION)

        # Account for the static task-prompt/system-prompt boilerplate.
        boilerplate_tokens = _estimate_tokens(config["task_prompt"]) + _estimate_tokens(
            config.get("system_prompt", "")
        )
        payload_budget = max(0, budget_tokens - boilerplate_tokens)

        truncated = False
        extraction_tokens = _estimate_tokens(extraction_json)
        text_tokens = _estimate_tokens(text)

        if extraction_tokens + text_tokens > payload_budget:
            truncated = True
            logger.warning(
                "Summarization prompt (~%d text + ~%d extraction tokens) exceeds "
                "payload budget (~%d of %d window); truncating to fit.",
                text_tokens,
                extraction_tokens,
                payload_budget,
                max_input_tokens,
            )
            # First, hard-cap the extraction JSON to at most a third of the budget
            # (it is supplemental — the document text is primary for a summary).
            extraction_cap_tokens = max(0, payload_budget // 3)
            if extraction_tokens > extraction_cap_tokens:
                extraction_char_cap = int(extraction_cap_tokens * _CHARS_PER_TOKEN)
                if extraction_char_cap <= 0:
                    extraction_json = ""
                else:
                    extraction_json = (
                        extraction_json[:extraction_char_cap]
                        + "\n... (extraction results truncated to fit model context window)"
                    )
                extraction_tokens = _estimate_tokens(extraction_json)

            # Then truncate DOCUMENT_TEXT to whatever remains.
            text_budget_tokens = max(0, payload_budget - extraction_tokens)
            text_char_cap = int(text_budget_tokens * _CHARS_PER_TOKEN)
            if text_char_cap < len(text):
                notice = (
                    "\n\n[NOTE: Document text was truncated to fit the model "
                    "context window. The summary is based on the leading portion "
                    "of the document.]"
                )
                keep = max(0, text_char_cap - len(notice))
                text = text[:keep] + notice

        placeholders = {"DOCUMENT_TEXT": text}
        if extraction_json:
            placeholders["EXTRACTION_RESULTS"] = extraction_json
        return placeholders, truncated

    def process_text(
        self, text: str, extraction_results: Dict[str, Any] = None
    ) -> DocumentSummary:
        """
        Summarize text content using the configured backend.

        Args:
            text: Text content to summarize
            extraction_results: Optional extraction results to include in the summary

        Returns:
            DocumentSummary: Summary of the text content with flexible structure
        """
        if not text:
            logger.warning("Empty text provided for summarization")
            return self._create_error_summary("Empty text provided")

        # Get summarization configuration
        config = self._get_summarization_config()

        # Build placeholders for the prompt (Fix A: compact/elide extraction JSON;
        # Fix B: fit-or-truncate DOCUMENT_TEXT/EXTRACTION_RESULTS to the window).
        placeholders, _truncated = self._build_placeholders(
            text, extraction_results, config
        )

        # Use common function to prepare prompt with required placeholder validation
        task_prompt = bedrock.format_prompt(
            config["task_prompt"],
            placeholders,
            required_placeholders=[
                "DOCUMENT_TEXT"
            ],  # Keep DOCUMENT_TEXT as only required
        )

        content = [{"text": task_prompt}]

        logger.info("Summarizing text with Bedrock")

        # Invoke Bedrock model
        try:
            response_with_metering = self._invoke_bedrock_model(
                content=content, config=config
            )

            response = response_with_metering["response"]
            metering = response_with_metering["metering"]

            # Extract summarization result
            # Defensive: Handle case where LLM returns empty content array
            content = response["output"]["message"].get("content", [])
            if not content or len(content) == 0:
                logger.error(
                    "LLM returned empty content array in summarization response",
                    extra={"response": response},
                )
                raise ValueError("Summarization failed: LLM returned empty response")

            # Reasoning models (Claude Sonnet 5 / 4.6+, extended thinking on) emit
            # one or more `reasoningContent` blocks before the answer `text` block,
            # so content[0] may not be the text. Concatenate all `text` blocks.
            summary_text = "".join(
                item["text"]
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            )

            # Try to extract JSON from the response
            try:
                summary_json = extract_json_from_text(summary_text)
                summary_data = json.loads(summary_json)

                # If the summary is in the expected format with a "summary" field containing markdown
                if "summary" in summary_data:
                    # TODO: Uncomment this when needed
                    # The summary field contains the markdown content
                    # markdown_summary = summary_data["summary"]

                    # Create summary with the parsed data
                    return DocumentSummary(
                        content=summary_data, metadata={"metering": metering}
                    )
                else:
                    # Create summary with whatever fields were returned
                    return DocumentSummary(
                        content=summary_data, metadata={"metering": metering}
                    )

            except Exception as e:
                logger.warning(f"Failed to parse JSON from response: {e}")
                # Fallback to using the raw text as a single content field
                error_content = {
                    "error": "Summary parsing failed",
                    "content": summary_text,
                }

                return DocumentSummary(
                    content=error_content,
                    metadata={"error": str(e), "metering": metering},
                )

        except Exception as e:
            # Fix B: graceful degradation. If Bedrock still rejects the prompt as
            # too large for the input window (despite the fit-or-truncate guard —
            # e.g. our char/token estimate under-counted dense numeric text),
            # return a partial/skipped summary stub instead of raising. A
            # summarization overflow must NEVER fail the whole document when the
            # (expensive) extraction already succeeded.
            if _is_input_token_overflow(e):
                logger.warning(
                    "Summarization input exceeded the model context window even "
                    "after truncation (%s); returning a partial summary stub so "
                    "the document completes with extraction intact.",
                    e,
                )
                return DocumentSummary(
                    content={
                        "summary": (
                            "## Summary Unavailable\n\n"
                            "This section could not be summarized because its "
                            "content exceeded the summarization model's context "
                            "window. Extraction results for this section are "
                            "available and unaffected."
                        )
                    },
                    metadata={
                        "summarization_skipped": True,
                        "skip_reason": "input_token_overflow",
                        "error": str(e),
                    },
                )
            logger.error(f"Error summarizing text: {str(e)}")
            raise

    def process_document_section(
        self, document: Document, section_id: str
    ) -> Tuple[Document, Dict[str, Any]]:
        """
        Summarize a specific section of a document and update the Document object with the summary.

        Args:
            document: Document object containing the section to summarize
            section_id: ID of the section to summarize

        Returns:
            Tuple[Document, Dict[str, Any]]: Updated Document object with section summary and section-specific metering data
        """
        # Validate input document
        if not document:
            logger.error("No document provided")
            return document, {}

        if not document.sections:
            logger.error("Document has no sections to process")
            document.errors.append("Document has no sections to process")
            return document, {}

        # Find the section with the given ID
        section = None
        for s in document.sections:
            if s.section_id == section_id:
                section = s
                break

        if not section:
            error_msg = f"Section {section_id} not found in document"
            logger.error(error_msg)
            document.errors.append(error_msg)
            return document, {}

        # Short-circuit: skip sections whose class is marked as excluded
        # (e.g., static instruction pages). Writes a small skipped-stub
        # summary so the UI/reporting can display a meaningful message
        # instead of an empty summary pane.
        from idp_common.section_exclusion import (
            is_section_excluded,
            write_skipped_stub,
        )

        if is_section_excluded(section):
            output_bucket = document.output_bucket
            output_key = (
                f"{document.input_key}/sections/{section.section_id}/summary.json"
                if document.input_key
                else None
            )
            write_skipped_stub(
                document,
                section,
                stage="summarization",
                output_bucket=output_bucket,
                output_key=output_key,
            )
            logger.info(
                "Summarization skipped for excluded section %s (class=%s, reason=%s)",
                section.section_id,
                section.classification,
                section.exclusion_reason or "excluded",
            )
            return document, {}

        # Extract information about the section
        class_label = section.classification
        output_bucket = document.output_bucket
        output_prefix = document.input_key
        output_key = f"{output_prefix}/sections/{section.section_id}/summary.json"
        output_md_key = f"{output_prefix}/sections/{section.section_id}/summary.md"
        output_uri = f"s3://{output_bucket}/{output_key}"
        output_md_uri = f"s3://{output_bucket}/{output_md_key}"

        # Check if the section has required pages
        if not section.page_ids:
            error_msg = f"Section {section_id} has no page IDs"
            logger.error(error_msg)
            document.errors.append(error_msg)
            return document, {}

        # Sort pages by page number
        sorted_page_ids = sorted(section.page_ids, key=int)
        start_page = int(sorted_page_ids[0])
        end_page = int(sorted_page_ids[-1])
        logger.info(
            f"Summarizing section {section_id}, class {class_label}: pages {start_page}-{end_page}"
        )

        try:
            # TODO: Uncomment this when needed
            # Start timing
            # start_time = time.time()

            # Read extraction results if available
            extraction_results = {}
            if section.extraction_result_uri:
                try:
                    extraction_data = s3.get_json_content(section.extraction_result_uri)
                    extraction_results = extraction_data.get("inference_result", {})
                    logger.info(f"Loaded extraction results for section {section_id}")
                except Exception as e:
                    logger.warning(
                        f"Failed to load extraction results for section {section_id}: {e}"
                    )

            # Read document text from all pages in order
            all_text = ""
            for page_id in sorted_page_ids:
                if page_id not in document.pages:
                    error_msg = f"Page {page_id} not found in document"
                    logger.error(error_msg)
                    document.errors.append(error_msg)
                    continue

                page = document.pages[page_id]
                text_path = page.parsed_text_uri
                page_text = s3.get_text_content(text_path)
                all_text += f"<page-number>{page_id}</page-number>\n{page_text}\n\n"

            if not all_text:
                logger.warning(f"No text content found in section {section_id}")
                document = self._update_document_status(
                    document,
                    success=False,
                    error_message=f"No text content found in section {section_id}",
                )
                return document, {}

            # Generate summary with extraction results
            summary = self.process_text(all_text, extraction_results)

            # TODO: Uncomment this when needed
            # Calculate execution time
            # execution_time = time.time() - start_time

            # TODO: Uncomment this when needed
            # Create summarization result object
            # summarization_result = DocumentSummarizationResult(
            #     document_id=document.id, summary=summary, execution_time=execution_time
            # )

            # Store results in S3
            # Store JSON result
            s3.write_content(
                content=summary.content,
                bucket=output_bucket,
                key=output_key,
                content_type="application/json",
            )

            # Generate and store markdown report using our custom formatter
            # Create a single-section document for the formatter
            single_section = {section_id: summary.content}
            formatter = SummaryMarkdownFormatter(
                document, single_section, is_section=True, include_toc=True
            )
            markdown_report = formatter.format_all()

            s3.write_content(
                content=markdown_report,
                bucket=output_bucket,
                key=output_md_key,
                content_type="text/markdown",
            )

            # Update section with summary URI
            # Initialize attributes if it's None
            if section.attributes is None:
                section.attributes = {}

            section.attributes["summary_uri"] = output_uri
            section.attributes["summary_md_uri"] = output_md_uri

            # Extract metering data to return separately
            section_metering = {}
            if "metering" in summary.metadata:
                section_metering = summary.metadata["metering"]

            logger.info(
                f"Section {section_id} summarized successfully. Summary stored at: {output_uri}"
            )

        except Exception as e:
            error_msg = f"Error summarizing section {section_id}: {str(e)}"
            logger.error(error_msg)
            document.errors.append(error_msg)
            # Re-raise exception to propagate error to Step Functions
            # This ensures workflow failures are properly reported instead of silently completing
            raise

        return document, section_metering

    def process_document(
        self, document: Document, store_results: bool = True
    ) -> Document:
        """
        Summarize a document and update the Document object with the summary.

        This method processes each section in parallel using ThreadPoolExecutor with 20 workers
        and then combines the results into a single document summary.

        If no sections are defined, falls back to summarizing the entire document at once.

        Args:
            document: Document object to summarize
            store_results: Whether to store results in S3 (default: True)

        Returns:
            Document: Updated Document object with summary and summarization_result
        """
        # Check if summarization is enabled in typed configuration
        enabled = self.config.summarization.enabled
        if not enabled:
            logger.info(
                f"Summarization is disabled in configuration for document {document.id}, skipping processing"
            )
            # Update document status to completed if not already failed
            if document.status != Status.FAILED:
                document.status = Status.COMPLETED
            return document

        if not document.pages:
            logger.warning("Document has no pages to summarize")
            return self._update_document_status(
                document,
                success=False,
                error_message="Document has no pages to summarize",
            )

        # If no sections are defined, fall back to summarizing the entire document at once
        if not document.sections:
            logger.info("No sections defined, summarizing entire document at once")
            return self._process_document_as_whole(document, store_results)

        try:
            # Start timing
            start_time = time.time()

            # Initialize data structures for results
            combined_content = {}
            combined_metadata = {"section_summaries": {}}
            section_markdowns = {}  # Use dictionary instead of list for section markdowns

            # Create a thread pool with 20 workers for parallel processing
            max_workers = 20
            logger.info(
                f"Processing document sections in parallel with {max_workers} workers"
            )

            # Initialize a dictionary to collect all section-specific metering data
            all_section_metering = {}

            # Track exceptions from failed sections for proper error propagation
            section_exceptions = {}

            # Process sections in parallel using ThreadPoolExecutor
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers
            ) as executor:
                # Create a dictionary to track futures
                future_to_section = {}

                # Submit all section processing tasks to the executor
                for section in document.sections:
                    logger.info(
                        f"Submitting section {section.section_id} with classification {section.classification} for processing"
                    )
                    # Create a deep copy of the document for thread safety, excluding metering data
                    thread_document = copy.deepcopy(document)
                    # Reset metering data in the copy to avoid double-counting
                    thread_document.metering = {}

                    future = executor.submit(
                        self.process_document_section,
                        thread_document,
                        section.section_id,
                    )
                    future_to_section[future] = section

                # Process results as they complete
                for future in concurrent.futures.as_completed(future_to_section):
                    section = future_to_section[future]
                    try:
                        # Get the result (updated document with processed section and section-specific metering)
                        updated_document, section_metering = future.result()

                        # Store section-specific metering data
                        if section_metering:
                            section_key = f"section_{section.section_id}"
                            all_section_metering[section_key] = section_metering

                        # Find the processed section in the updated document
                        processed_section = None
                        for s in updated_document.sections:
                            if s.section_id == section.section_id:
                                processed_section = s
                                break

                        if (
                            processed_section
                            and processed_section.attributes
                            and "summary_uri" in processed_section.attributes
                        ):
                            # Get the section summary from S3
                            summary_uri = processed_section.attributes["summary_uri"]
                            summary_md_uri = processed_section.attributes.get(
                                "summary_md_uri"
                            )

                            # Update the original document's section with the processed section's attributes
                            for s in document.sections:
                                if s.section_id == section.section_id:
                                    s.attributes = processed_section.attributes
                                    break

                            # Merge any errors from the processed document
                            for error in updated_document.errors:
                                if error not in document.errors:
                                    document.errors.append(error)

                            # Load the summary content
                            try:
                                summary_content = s3.get_json_content(summary_uri)

                                # Add to combined content under a unique key that includes section ID
                                section_key = (
                                    f"{section.classification}_{section.section_id}"
                                    if section.classification
                                    else f"section_{section.section_id}"
                                )
                                combined_content[section_key] = summary_content

                                # Store section summary reference in metadata
                                combined_metadata["section_summaries"][section_key] = {
                                    "section_id": section.section_id,
                                    "classification": section.classification,
                                    "summary_uri": summary_uri,
                                    "summary_md_uri": summary_md_uri,
                                }

                                # Get markdown content for combined markdown report
                                if summary_md_uri:
                                    try:
                                        # Generate clean markdown directly from the summary content
                                        section_title = (
                                            section.classification
                                            or f"Section {section.section_id}"
                                        )
                                        # Store section content with metadata
                                        section_markdowns[section.section_id] = {
                                            "content": summary_content,
                                            "title": section_title,
                                        }
                                    except Exception as e:
                                        logger.warning(
                                            f"Failed to generate markdown for section {section.section_id}: {e}"
                                        )
                            except Exception as e:
                                logger.warning(
                                    f"Failed to load section summary from {summary_uri}: {e}"
                                )
                                document.errors.append(
                                    f"Failed to load section summary: {str(e)}"
                                )
                    except Exception as e:
                        error_msg = (
                            f"Error processing section {section.section_id}: {str(e)}"
                        )
                        logger.error(error_msg)
                        document.errors.append(error_msg)
                        # Store exception for later re-raising to ensure Step Functions workflow fails properly
                        section_exceptions[section.section_id] = e

            # Calculate execution time
            execution_time = time.time() - start_time

            # Check if any sections failed and re-raise the first exception
            # This ensures Step Functions workflow properly reports failures (GitHub Issue #166)
            if section_exceptions:
                first_failed_section = next(iter(section_exceptions.keys()))
                first_exception = section_exceptions[first_failed_section]
                logger.error(
                    f"Summarization failed for {len(section_exceptions)} section(s). "
                    f"Re-raising exception from section {first_failed_section} to fail the workflow."
                )
                raise first_exception

            # Merge all section-specific metering data into the document's metering data
            for section_metering in all_section_metering.values():
                document.metering = utils.merge_metering_data(
                    document.metering, section_metering
                )

            # Create a combined summary from all section summaries
            summary = DocumentSummary(
                content=combined_content, metadata=combined_metadata
            )

            # Create summarization result object
            summarization_result = DocumentSummarizationResult(
                document_id=document.id, summary=summary, execution_time=execution_time
            )

            # Attach summarization result to document for immediate use
            document.summarization_result = summarization_result

            # Store results if requested
            if store_results:
                output_bucket = document.output_bucket

                # Store the combined JSON summary
                json_key = f"{document.input_key}/summary/summary.json"
                s3.write_content(
                    content=summary.to_dict(),
                    bucket=output_bucket,
                    key=json_key,
                    content_type="application/json",
                )

                # Store the full text for chat
                all_text = self._get_all_text(document)
                fulltext_key = f"{document.input_key}/summary/fulltext.txt"
                s3.write_content(
                    content=all_text,
                    bucket=output_bucket,
                    key=fulltext_key,
                    content_type="text/plain",
                )

                # Create and store the combined markdown summary
                md_key = f"{document.input_key}/summary/summary.md"

                # Create a complete markdown document that combines all section summaries
                if section_markdowns:
                    # Create our custom formatter with the document object for section ordering
                    formatter = SummaryMarkdownFormatter(
                        document, section_markdowns, is_section=False, include_toc=True
                    )
                    combined_markdown = formatter.format_all()

                    # Execution time line removed

                    # Write the combined markdown
                    s3.write_content(
                        content=combined_markdown,
                        bucket=output_bucket,
                        key=md_key,
                        content_type="text/markdown",
                    )
                else:
                    # If no section markdown parts, generate a markdown report directly from the summary content
                    # Create a single-section document for the formatter
                    single_section = {"full_document": summary.content}
                    formatter = SummaryMarkdownFormatter(document, single_section)
                    markdown_report = formatter.format_all()

                    # Add execution time
                    markdown_report += (
                        f"\n\nExecution time: {execution_time:.2f} seconds"
                    )

                    s3.write_content(
                        content=markdown_report,
                        bucket=output_bucket,
                        key=md_key,
                        content_type="text/markdown",
                    )

                # Update document and summarization result with summary URIs
                document.summary_report_uri = f"s3://{output_bucket}/{md_key}"
                summarization_result.output_uri = f"s3://{output_bucket}/{json_key}"

            # Update document status
            document = self._update_document_status(document)

            if store_results:
                logger.info(
                    f"Document summarized successfully. Summary report stored at: {document.summary_report_uri}"
                )
            else:
                logger.info(
                    "Document summarized successfully. No summary report stored."
                )

        except Exception as e:
            error_msg = f"Error summarizing document: {str(e)}"
            logger.error(error_msg)
            document = self._update_document_status(
                document, success=False, error_message=error_msg
            )

        return document

    def _get_all_text(self, document: Document) -> str:
        """
        Retrieve all text content from a document's pages.

        Args:
            document: Document object to process

        Returns:
            str: Combined text content from all pages
        """
        all_text = ""
        for page_id, page in sorted(document.pages.items()):
            if page.parsed_text_uri:
                try:
                    page_text = s3.get_text_content(page.parsed_text_uri)
                    all_text += f"<page-number>{page_id}</page-number>\n{page_text}\n\n"
                except Exception as e:
                    logger.warning(
                        f"Failed to load text content from {page.parsed_text_uri}: {e}"
                    )
                    # Continue with other pages

        return all_text

    def _process_document_as_whole(
        self, document: Document, store_results: bool = True
    ) -> Document:
        """
        Summarize a document as a whole (without sections).

        This method implements the original behavior of summarizing the entire document at once.

        Args:
            document: Document object to summarize
            store_results: Whether to store results in S3

        Returns:
            Document: Updated Document object with summary
        """
        try:
            # Start timing
            start_time = time.time()

            # Read extraction results if available (when document has sections with extraction results)
            extraction_results = {}
            if document.sections:
                # Combine extraction results from all sections
                for section in document.sections:
                    if section.extraction_result_uri:
                        try:
                            extraction_data = s3.get_json_content(
                                section.extraction_result_uri
                            )
                            section_results = extraction_data.get(
                                "inference_result", {}
                            )
                            # Merge section results into overall extraction_results
                            # Prefix keys with section classification if available
                            if section.classification:
                                for key, value in section_results.items():
                                    extraction_results[
                                        f"{section.classification}_{key}"
                                    ] = value
                            else:
                                extraction_results.update(section_results)
                            logger.info(
                                f"Loaded extraction results from section {section.section_id}"
                            )
                        except Exception as e:
                            logger.warning(
                                f"Failed to load extraction results from section {section.section_id}: {e}"
                            )

            # Combine text from all pages
            all_text = self._get_all_text(document)

            if not all_text:
                logger.warning("No text content found in document pages")
                return self._update_document_status(
                    document,
                    success=False,
                    error_message="No text content found in document pages",
                )

            # Generate summary with extraction results
            summary = self.process_text(all_text, extraction_results)

            # Calculate execution time
            execution_time = time.time() - start_time

            # Create summarization result object
            summarization_result = DocumentSummarizationResult(
                document_id=document.id, summary=summary, execution_time=execution_time
            )

            # Attach summarization result to document for immediate use
            document.summarization_result = summarization_result

            # Store results if requested
            if store_results:
                output_bucket = document.output_bucket

                # Store the JSON summary
                json_key = f"{document.input_key}/summary/summary.json"
                s3.write_content(
                    content=summary.to_dict(),
                    bucket=output_bucket,
                    key=json_key,
                    content_type="application/json",
                )

                # Store the full text for chat
                fulltext_key = f"{document.input_key}/summary/fulltext.txt"
                s3.write_content(
                    content=all_text,
                    bucket=output_bucket,
                    key=fulltext_key,
                    content_type="text/plain",
                )

                # Generate and store markdown report
                md_key = f"{document.input_key}/summary/summary.md"
                # Create a single-section document for the formatter with metadata
                single_section = {
                    "full_document": {
                        "content": summary.content,
                        "title": "Document Summary",
                    }
                }
                formatter = SummaryMarkdownFormatter(
                    document, single_section, is_section=False, include_toc=True
                )
                markdown_report = formatter.format_all()

                # Execution time line removed

                s3.write_content(
                    content=markdown_report,
                    bucket=output_bucket,
                    key=md_key,
                    content_type="text/markdown",
                )

                # Update document and summarization result with summary URIs
                document.summary_report_uri = f"s3://{output_bucket}/{md_key}"
                summarization_result.output_uri = f"s3://{output_bucket}/{json_key}"

            # Update document metering
            if "metering" in summary.metadata:
                document.metering = utils.merge_metering_data(
                    document.metering, summary.metadata["metering"]
                )

            # Update document status
            document = self._update_document_status(document)

            if store_results:
                logger.info(
                    f"Document summarized successfully. Summary report stored at: {document.summary_report_uri}"
                )
            else:
                logger.info(
                    "Document summarized successfully. No summary report stored."
                )

        except Exception as e:
            error_msg = f"Error summarizing document: {str(e)}"
            logger.error(error_msg)
            document = self._update_document_status(
                document, success=False, error_message=error_msg
            )
            raise

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
                    f"Document summarized with {len(document.errors)} errors"
                )

        return document
