# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Rule validation service for documents using LLMs.

This module provides a service for validating documents against dynamic
business rules using LLMs, with support for async processing,
chunking, and cost tracking.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

from idp_common import bedrock, s3, utils
from idp_common.config.models import IDPConfig
from idp_common.config.schema_constants import (
    VALIDATION_ENGINE_LLM,
    VALIDATION_ENGINE_Z3,
    X_AWS_IDP_POLICY_TYPE,
    X_AWS_IDP_RULE_ID,
    X_AWS_IDP_RULE_JSON,
    X_AWS_IDP_RULE_TYPE,
    X_AWS_IDP_VALIDATION_ENGINE,
)
from idp_common.models import Document, RuleValidationResult, Status
from idp_common.rule_validation.models import FactExtractionResponse

logger = logging.getLogger(__name__)


def _policy_type_of(policy_class: Dict[str, Any]) -> Optional[str]:
    """Read a policy class's type, tolerating the legacy discriminator.

    Raw rules-discovery output and pre-v0.5.9 configs carry
    ``x-aws-idp-rule-type``. Keying only on the current name made such a class
    contribute no policy type and no rule questions — rule validation produced
    nothing, silently. Kept consistent with
    ``PolicyClassificationService._load_policy_classes``, which must agree with
    this or a class could classify yet yield zero questions.
    """
    return policy_class.get(X_AWS_IDP_POLICY_TYPE) or policy_class.get(
        X_AWS_IDP_RULE_TYPE
    )


class RuleValidationService:
    """Service for validating documents against rules using LLMs."""

    def __init__(
        self,
        region: str = None,
        config: Union[Dict[str, Any], IDPConfig] = None,
        backend: str = "bedrock",
    ):
        """
        Initialize the rule validation service.

        Args:
            region: AWS region for Bedrock
            config: Configuration dictionary or IDPConfig model
            backend: Summarization backend to use ('bedrock')
        """
        # Convert dict to IDPConfig if needed
        if config is not None and isinstance(config, dict):
            from idp_common.config.models import IDPConfig

            config_model: IDPConfig = IDPConfig(**config)
        elif config is None:
            from idp_common.config.models import IDPConfig

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

        # Get model_id from fact_extraction subsection in typed config
        if not self.config.rule_validation.fact_extraction:
            raise ValueError(
                "No fact_extraction configuration found in rule_validation"
            )
        model_id = self.config.rule_validation.fact_extraction.model
        if not model_id:
            raise ValueError("No model ID specified in fact_extraction configuration")
        logger.info(f"Initialized rule validation service with model {model_id}")

        # Initialize token tracking (will be accumulated using utils.merge_metering_data)
        self.token_metrics = {}
        self.metrics_lock = (
            asyncio.Lock()
        )  # Lock for protecting concurrent token metrics updates

        # Initialize timing metrics
        self.timing_metrics = {
            "start_time": None,
            "end_time": None,
            "total_duration": None,
            "criteria_processing_time": [],
        }

        # Get async processing config from typed configuration
        self.semaphore_limit = self.config.rule_validation.semaphore
        self._semaphore = None
        self.max_chunk_size = self.config.rule_validation.max_chunk_size
        self.token_size = self.config.rule_validation.token_size
        self.overlap_percentage = self.config.rule_validation.overlap_percentage

    @property
    def semaphore(self):
        """Lazy initialization of semaphore in current event loop."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.semaphore_limit)
        return self._semaphore

    def _get_policy_types(self, config: Dict[str, Any]) -> List[str]:
        """
        Extract policy types from policy_classes configuration.

        Args:
            config: Configuration dictionary

        Returns:
            List of policy type strings
        """
        policy_classes = config.get("policy_classes", [])
        policy_types = [
            pt for pt in (_policy_type_of(pc) for pc in policy_classes) if pt
        ]
        logger.debug(
            f"Extracted {len(policy_types)} policy types from policy_classes: {policy_types}"
        )
        return policy_types

    def _get_rule_metadata(
        self, config: Dict[str, Any], policy_type: str
    ) -> List[Dict[str, str]]:
        """
        Extract rule metadata (description + engine field) for a specific policy type.

        Each entry contains:
          - "description": the rule question text
          - "engine": the validation engine ("llm" or "z3"), defaults to "llm"

        Args:
            config: Configuration dictionary
            policy_type: Policy type to get metadata for

        Returns:
            List of dicts with "description" and "engine" keys
        """
        policy_classes = config.get("policy_classes", [])
        for policy_class in policy_classes:
            if _policy_type_of(policy_class) == policy_type:
                rule_properties = policy_class.get("rule_properties", {})
                metadata = []
                for prop in rule_properties.values():
                    description = prop.get("description")
                    if description:
                        engine = prop.get(
                            X_AWS_IDP_VALIDATION_ENGINE, VALIDATION_ENGINE_LLM
                        )
                        rule_id = prop.get(X_AWS_IDP_RULE_ID)
                        rule_json = prop.get(X_AWS_IDP_RULE_JSON)
                        metadata.append(
                            {
                                "description": description,
                                "engine": engine,
                                "rule_id": rule_id,
                                "rule_json": rule_json,
                            }
                        )
                logger.debug(
                    f"Extracted {len(metadata)} rule metadata entries for "
                    f"policy_type '{policy_type}': "
                    f"{[(m['description'][:40], m['engine']) for m in metadata]}"
                )
                return metadata
        logger.warning(f"No rule metadata found for policy_type '{policy_type}'")
        return []

    def _chunk_text_with_overlap(
        self,
        text: str,
        max_chunk_size: int,
        token_size: int,
        overlap_percentage: int,
    ) -> List[str]:
        """
        Chunk text with overlap for better context preservation.

        Args:
            text: Text to chunk
            max_chunk_size: Maximum chunk size in tokens
            token_size: Average token size
            overlap_percentage: Percentage of overlap between chunks

        Returns:
            List of text chunks
        """
        # Simple token estimation
        estimated_tokens = len(text) // token_size

        if estimated_tokens <= max_chunk_size:
            return [text]

        # Calculate chunk size in characters
        chunk_size_chars = max_chunk_size * token_size
        overlap_chars = int(chunk_size_chars * (overlap_percentage / 100))

        chunks = []
        start = 0

        while start < len(text):
            end = start + chunk_size_chars
            if end > len(text):
                end = len(text)

            chunk = text[start:end]
            chunks.append(chunk)

            # Move start position with overlap
            start = end - overlap_chars
            if start >= len(text):
                break

        return chunks

    def _chunk_pages_with_overlap(
        self,
        text: str,
        max_chunk_size: int,
        token_size: int,
        overlap_percentage: int,
    ) -> List[str]:
        """
        Split text into chunks with page-aware overlap to maintain context while respecting page boundaries.
        Pages are marked with <page-number>X</page-number> tags.

        Dynamic overlap strategy:
        - If previous chunk has multiple complete pages: use complete last page as overlap
        - If previous chunk has only 1 complete page: use 10% of that page as overlap

        Args:
            text: The text to chunk with page markers
            max_chunk_size: Maximum size of each chunk in tokens
            token_size: Estimated characters per token (for rough token estimation)
            overlap_percentage: Percentage of overlap for single-page chunks (0-100)

        Returns:
            List of text chunks with page-aware overlap
        """
        logger.debug(
            f"Page chunk start text_length={len(text)} max_chunk_size={max_chunk_size} token_size={token_size} overlap_percentage={overlap_percentage}"
        )

        if not text.strip():
            logger.debug("Page chunk empty text")
            return []

        # Early return if entire text fits in one chunk
        estimated_tokens = len(text) // token_size
        if estimated_tokens <= max_chunk_size:
            logger.debug(f"Page chunk single chunk estimated_tokens={estimated_tokens}")
            return [text]

        # Parse pages using regex
        page_pattern = r"<page-number>(\d+)</page-number>\s*"
        pages = []

        # Split by page markers and reconstruct page content
        parts = re.split(page_pattern, text)

        # First part might be content before any page marker
        if parts[0].strip():
            pages.append(("0", parts[0].strip()))  # Assign page 0 for pre-page content

        # Parse alternating page numbers and content
        for i in range(1, len(parts), 2):
            if i + 1 < len(parts):
                page_number = parts[i]
                page_content = parts[i + 1].strip()
                if page_content:
                    pages.append((page_number, page_content))

        logger.debug(
            f"Page chunk parsed pages pages_count={len(pages)} page_numbers={[p[0] for p in pages]}"
        )

        if not pages:
            # Fallback to character-based chunking if no pages found
            logger.debug("Page chunk no pages fallback")
            return self._chunk_text_with_overlap(
                text, max_chunk_size, token_size, overlap_percentage
            )

        chunks = []
        current_chunk_pages = []
        current_chunk_tokens = 0
        previous_chunk_pages = []

        def build_chunk_text(page_list: List[Tuple[str, str]]) -> str:
            """Build chunk text from list of (page_number, content) tuples."""
            chunk_parts = []
            for page_num, content in page_list:
                if page_num != "0":  # Don't add marker for pre-page content
                    chunk_parts.append(f"<page-number>{page_num}</page-number>")
                chunk_parts.append(content)
            return "\n\n".join(chunk_parts)

        def get_page_tokens(page_content: str) -> int:
            """Estimate tokens for a page."""
            return len(page_content) // token_size

        def get_overlap_pages(
            prev_pages: List[Tuple[str, str]],
        ) -> List[Tuple[str, str]]:
            """Get overlap pages based on dynamic strategy."""
            if not prev_pages:
                logger.debug("Page chunk overlap no prev pages")
                return []

            page_numbers = [p[0] for p in prev_pages]
            total_prev_content_length = sum(len(p[1]) for p in prev_pages)

            logger.debug(
                f"Page chunk overlap input prev_pages_count={len(prev_pages)} page_numbers={page_numbers} total_content_length={total_prev_content_length}"
            )

            if len(prev_pages) > 1:
                # Multiple pages: use complete last page as overlap
                last_page_num, last_page_content = prev_pages[-1]
                logger.debug(
                    f"Page chunk overlap multi-page prev_pages_count={len(prev_pages)} page_numbers={page_numbers} using_complete_last_page={last_page_num} last_page_length={len(last_page_content)}"
                )
                return [prev_pages[-1]]
            else:
                # Single page: use 10% of page as overlap
                page_num, page_content = prev_pages[0]
                overlap_size = len(page_content) * overlap_percentage // 100  # True 10%
                overlap_content = page_content[-overlap_size:]
                logger.debug(
                    f"Page chunk overlap single-page page={page_num} original_length={len(page_content)} overlap_percentage={overlap_percentage} overlap_size={overlap_size} overlap_length={len(overlap_content)}"
                )
                return [(page_num, overlap_content)]

        i = 0
        chunk_index = 0
        while i < len(pages):
            page_num, page_content = pages[i]
            page_tokens = get_page_tokens(page_content)

            logger.debug(
                f"Page chunk processing chunk_idx={chunk_index} page={page_num} page_tokens={page_tokens} current_chunk_tokens={current_chunk_tokens}"
            )

            # Check if we can add this page to current chunk
            if current_chunk_tokens + page_tokens <= max_chunk_size:
                # Add page to current chunk
                current_chunk_pages.append((page_num, page_content))
                current_chunk_tokens += page_tokens
                logger.debug(
                    f"Page chunk added page chunk_idx={chunk_index} page={page_num} new_total_tokens={current_chunk_tokens}"
                )
                i += 1
            else:
                # Current page doesn't fit, finalize current chunk if it has content
                if current_chunk_pages:
                    # Add overlap from previous chunk if exists
                    overlap_pages = get_overlap_pages(previous_chunk_pages)
                    final_chunk_pages = overlap_pages + current_chunk_pages
                    chunk_text = build_chunk_text(final_chunk_pages)
                    chunks.append(chunk_text)

                    logger.debug(
                        f"Page chunk finalized chunk_idx={chunk_index} pages={[p[0] for p in final_chunk_pages]} overlap_pages_count={len(overlap_pages)} chunk_length={len(chunk_text)}"
                    )

                    # Update previous chunk pages for next iteration - use final chunk pages including overlap
                    previous_chunk_pages = final_chunk_pages.copy()
                    current_chunk_pages = []
                    current_chunk_tokens = 0
                    chunk_index += 1
                else:
                    # Single page is larger than max_chunk_size
                    # Add it as its own chunk with overlap from previous
                    overlap_pages = get_overlap_pages(previous_chunk_pages)
                    single_page_chunk = overlap_pages + [(page_num, page_content)]
                    chunk_text = build_chunk_text(single_page_chunk)
                    chunks.append(chunk_text)

                    logger.debug(
                        f"Page chunk oversized chunk_idx={chunk_index} page={page_num} page_tokens={page_tokens} overlap_pages_count={len(overlap_pages)} chunk_length={len(chunk_text)}"
                    )

                    # Update previous chunk pages
                    previous_chunk_pages = [(page_num, page_content)]
                    chunk_index += 1
                    i += 1

        # Handle remaining pages in current_chunk_pages
        if current_chunk_pages:
            overlap_pages = get_overlap_pages(previous_chunk_pages)
            final_chunk_pages = overlap_pages + current_chunk_pages
            chunk_text = build_chunk_text(final_chunk_pages)
            chunks.append(chunk_text)

            logger.debug(
                f"Page chunk final chunk_idx={chunk_index} pages={[p[0] for p in final_chunk_pages]} overlap_pages_count={len(overlap_pages)} chunk_length={len(chunk_text)}"
            )

        logger.debug(
            f"Page chunk complete total_chunks={len(chunks)} chunk_lengths={[len(c) for c in chunks]}"
        )

        return chunks

    def _prepare_prompt(
        self,
        template: str,
        substitutions: Dict[str, str],
        required_placeholders: List[str] = None,
    ) -> str:
        """
        Prepare prompt from template by replacing placeholders.

        Args:
            template: The prompt template with placeholders
            substitutions: Dictionary of placeholder values
            required_placeholders: List of required placeholders

        Returns:
            String with placeholders replaced
        """
        from idp_common.bedrock import format_prompt

        return format_prompt(template, substitutions, required_placeholders)

    async def _invoke_model_async(
        self,
        model_id: str,
        system_prompt: str,
        content: str,
        temperature: float = 0.0,
        top_k: int = 5,
        top_p: float = 0.1,
        max_tokens: Optional[int] = None,
        context: str = "RuleValidation",
    ) -> Dict[str, Any]:
        """
        Async wrapper for bedrock.invoke_model.

        Since the common bedrock client is synchronous, we run it in an executor
        to maintain async compatibility.
        """
        loop = asyncio.get_running_loop()

        # Run the synchronous bedrock.invoke_model in an executor
        response = await loop.run_in_executor(
            None,
            bedrock.invoke_model,
            model_id,
            system_prompt,
            [{"text": content}],  # content as list
            temperature,
            top_k,
            top_p,
            max_tokens,
            None,
            context,
        )

        return response

    async def _process_rule_question(
        self,
        rule: str,
        user_history: str,
        policy_type: str,
        config: Dict[str, Any],
        extraction_results: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """
        Process a single rule question against user history.

        Args:
            rule: The rule question to evaluate
            user_history: The user history text
            policy_type: Type of policy
            config: Configuration for the validation
            extraction_results: Optional extraction results to include in the prompt

        Returns:
            Validated response dictionary
        """
        async with self.semaphore:
            try:
                # Generate unique task ID for tracking
                task_id = str(uuid.uuid4())[:8]

                # Log start of processing with context
                logger.debug(
                    f"LLM start task_id={task_id} policy_type='{policy_type}' rule='{rule[:60]}...' thread_id={asyncio.current_task()}"
                )

                start_time = time.time()

                # Convert dict to Pydantic Config to leverage validators (same as extraction service)
                from idp_common.config.models import IDPConfig

                config_obj = IDPConfig(**config) if isinstance(config, dict) else config
                cv_config = config_obj.rule_validation.fact_extraction

                # Build placeholders for the prompt
                placeholders = {
                    "DOCUMENT_TEXT": user_history,
                    "rule": rule,
                    "policy_type": policy_type,
                    "recommendation_options": config_obj.rule_validation.recommendation_options
                    or "",
                }

                # Add extraction results if available, otherwise empty JSON (following summarization service pattern)
                if extraction_results:
                    placeholders["EXTRACTION_RESULTS"] = json.dumps(
                        extraction_results, indent=2
                    )
                else:
                    placeholders["EXTRACTION_RESULTS"] = "{}"

                # Prepare the prompt
                prompt = self._prepare_prompt(
                    cv_config.task_prompt,
                    placeholders,
                )

                # Get model ID from the nested config
                model_id = cv_config.model

                # Log before LLM invocation
                logger.debug(
                    f"LLM invoke task_id={task_id} policy_type='{policy_type}' rule='{rule[:60]}...' model='{model_id}'"
                )

                # Invoke the model
                response = await self._invoke_model_async(
                    model_id=model_id,
                    system_prompt=cv_config.system_prompt,
                    content=prompt,
                    temperature=cv_config.temperature,
                    top_k=cv_config.top_k,
                    top_p=cv_config.top_p,
                    max_tokens=cv_config.max_tokens,
                    context="RuleValidation",
                )

                call_duration = time.time() - start_time
                logger.debug(
                    f"LLM response task_id={task_id} policy_type='{policy_type}' rule='{rule[:60]}...' duration={call_duration:.2f}s response_keys={list(response.keys()) if response else 'None'}"
                )

                # Extract and parse response
                response_text = bedrock.extract_text_from_response(response)

                logger.debug(
                    f"LLM parsed task_id={task_id} policy_type='{policy_type}' rule='{rule[:60]}...' response_length={len(response_text)} response_text={response_text[:200]}..."
                )

                # Parse JSON response
                try:
                    # First try to extract from <response> XML tags
                    if "<response>" in response_text and "</response>" in response_text:
                        start_idx = response_text.find("<response>") + 10
                        end_idx = response_text.find("</response>")
                        response_text = response_text[start_idx:end_idx].strip()
                    # Fall back to ```json format
                    elif "```json" in response_text:
                        start_idx = response_text.find("```json") + 7
                        end_idx = response_text.find("```", start_idx)
                        response_text = response_text[start_idx:end_idx].strip()

                    response_dict = json.loads(response_text)
                except json.JSONDecodeError:
                    logger.error(f"Failed to parse response as JSON: {response_text}")
                    response_dict = {
                        "policy_type": policy_type,
                        "rule": rule,
                        "extracted_facts": [],
                        "extraction_summary": f"Failed to parse response: {response_text}",
                    }

                # Add rule context to response
                response_dict["policy_type"] = policy_type
                response_dict["rule"] = rule

                # Validate response with FactExtractionResponse model
                validated_response = FactExtractionResponse(**response_dict)

                # Track metering using the same approach as extraction service
                metering = response.get("metering", {})

                # Add comprehensive logging for debugging
                logger.debug(
                    f"Raw response keys: {list(response.keys()) if response else 'None'}"
                )
                logger.debug(f"Metering data: {metering}")
                logger.debug(f"Token metrics before merge: {self.token_metrics}")

                # Merge metering data using the same utility as extraction service with synchronization
                async with self.metrics_lock:
                    old_metrics = self.token_metrics.copy()
                    self.token_metrics = utils.merge_metering_data(
                        self.token_metrics, metering or {}
                    )
                    logger.debug(f"Token metrics after merge: {self.token_metrics}")
                    logger.debug(
                        f"Metrics changed: {old_metrics != self.token_metrics}"
                    )

                return validated_response.dict()

            except Exception as e:
                logger.error(f"Error processing rule question: {str(e)}")
                return {
                    "policy_type": policy_type,
                    "rule": rule,
                    "supporting_pages": [],
                    "recommendation": "Information Not Found",
                    "reasoning": f"Error during processing: {str(e)}",
                }

    async def _process_z3_fact_extraction(
        self,
        rule_description: str,
        rule_id: str,
        policy_type: str,
        extraction_results: Dict[str, Any],
        document_text: str,
        config: Dict[str, Any],
        rule_json: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Extract facts for a Z3 rule using the standard fact extraction prompt,
        augmented with Z3 parameter definitions for better context.

        This uses the same LLM call as standard LLM rules (_process_rule_question),
        but appends the Z3 parameter names/types/descriptions to the rule text so
        the LLM knows what specific values to look for in the document.

        The output format is identical to LLM rules — no special tagging.
        The orchestrator determines which rules are Z3 by reading the config.

        Args:
            rule_description: The natural language rule text
            rule_id: Unique rule identifier
            policy_type: Type of policy
            extraction_results: Structured extraction results for this section
            document_text: Raw document text for this section
            config: Configuration dictionary
            rule_json: The RuleJSON object from config (x-aws-idp-rule-json)

        Returns:
            FactExtractionResponse dict (same format as LLM rules)
        """
        # Build augmented rule description with parameter context from RuleJSON
        augmented_rule = rule_description
        if rule_json:
            parameters = rule_json.get("parameters", [])
            if parameters:
                param_lines = []
                for param in parameters:
                    name = param.get("name", "unknown")
                    param_type = param.get("type", "String")
                    description = param.get("description", "")
                    param_lines.append(f"  - {name} ({param_type}): {description}")
                params_context = "\n".join(param_lines)
                augmented_rule = (
                    f"{rule_description}\n\n"
                    f"[Key parameters to extract evidence for:\n"
                    f"{params_context}]"
                )

        # Use standard fact extraction with augmented rule text
        result = await self._process_rule_question(
            rule=augmented_rule,
            user_history=document_text,
            policy_type=policy_type,
            config=config,
            extraction_results=extraction_results,
        )

        # Restore the original rule description (without augmentation)
        # so the orchestrator can match it against config
        result["rule"] = rule_description

        return result

    async def _process_policy_type(
        self,
        policy_type: str,
        user_history: str,
        config: Dict[str, Any],
        extraction_results: Dict[str, Any] = None,
    ) -> List[Dict[str, Any]]:
        """
        Process all rule questions for a specific policy type.

        Routes each rule to the appropriate engine based on its
        x-aws-idp-validation-engine field:
          - "z3" → fact extraction augmented with Z3 parameter context
          - "llm" or absent → LLM engine (semantic reasoning)

        All rules share the same semaphore pool for concurrency control.
        Partial failures don't block other rules — all results are aggregated.

        Args:
            policy_type: The policy type to process
            user_history: The user history text (document content)
            config: Configuration for the validation
            extraction_results: Optional extraction results to include in the prompt

        Returns:
            List of validation responses
        """
        start_time = time.time()

        try:
            # Get rule metadata (description + engine) from policy_classes
            rule_metadata = self._get_rule_metadata(config, policy_type)
            if not rule_metadata:
                raise ValueError(
                    f"No rule questions found for policy type: {policy_type}"
                )

            # Process all rules concurrently, dispatching to the correct engine
            tasks = []
            for rule_meta in rule_metadata:
                rule_description = rule_meta["description"]
                engine = rule_meta["engine"]
                rule_id = rule_meta.get("rule_id")
                rule_json = rule_meta.get("rule_json")

                if engine == VALIDATION_ENGINE_Z3 and rule_id:
                    # Z3 rules: use standard fact extraction (same as LLM rules)
                    # augmented with Z3 parameter context from the embedded RuleJSON.
                    # The orchestration step will later use these extracted facts
                    # to do LLM-based value extraction + Z3 solver.
                    task = self._process_z3_fact_extraction(
                        rule_description=rule_description,
                        rule_id=rule_id,
                        policy_type=policy_type,
                        extraction_results=extraction_results,
                        document_text=user_history,
                        config=config,
                        rule_json=rule_json,
                    )
                elif engine == VALIDATION_ENGINE_Z3 and not rule_id:
                    # Strict mode: Z3 rule without rule_id is a configuration error
                    logger.error(
                        f"Z3 rule missing rule_id — cannot validate. "
                        f"Rule: '{rule_description[:60]}...'"
                    )
                    # Return a hard fail — do not silently fall back to LLM

                    async def _z3_config_error(rd=rule_description, pt=policy_type):
                        return {
                            "policy_type": pt,
                            "rule": rd,
                            "extracted_facts": [],
                            "extraction_summary": (
                                "Z3 configuration error: rule_id is missing. "
                                "Set x-aws-idp-rule-id in the rule property."
                            ),
                            "z3_parameters": None,
                        }

                    task = _z3_config_error()
                else:
                    # Default to LLM engine (engine == "llm" or absent)
                    task = self._process_rule_question(
                        rule=rule_description,
                        user_history=user_history,
                        policy_type=policy_type,
                        config=config,
                        extraction_results=extraction_results,
                    )
                tasks.append(task)

            # Wait for all tasks to complete — partial failures don't block
            # other rules since each task handles its own errors internally
            responses = await asyncio.gather(*tasks)

            # Track timing
            duration = time.time() - start_time
            self.timing_metrics["criteria_processing_time"].append(
                {"policy_type": policy_type, "duration": duration}
            )

            logger.info(
                f"Processed policy type {policy_type} in {duration:.2f} seconds"
            )

            return responses

        except Exception as e:
            logger.error(f"Error processing policy type {policy_type}: {str(e)}")
            raise

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
                    f"Document criteria validation completed with {len(document.errors)} errors"
                )

        return document

    async def validate_document_async(
        self, document: Document, config: Dict[str, Any]
    ) -> Document:
        """
        Validate a document against rules asynchronously.

        Args:
            document: The Document object to validate
            config: Configuration for the validation

        Returns:
            Document with updated rule_validation_result
        """
        doc_validation_id = str(uuid.uuid4())[:8]

        logger.debug(
            f"Document validation start doc_id={doc_validation_id} document_id={document.id if document else 'None'} task_id={asyncio.current_task()} sections={len(document.sections) if document and document.sections else 0}"
        )

        self.timing_metrics["start_time"] = datetime.now()

        try:
            # Validate input document
            if not document:
                error_msg = "No document provided"
                logger.error(error_msg)
                return self._update_document_status(
                    document, success=False, error_message=error_msg
                )

            if not document.sections:
                error_msg = "Document has no sections to process"
                logger.error(error_msg)
                document.errors.append(error_msg)
                return self._update_document_status(
                    document, success=False, error_message=error_msg
                )

            # Process sections in parallel instead of sequentially
            all_responses = {}
            multiple_sections = len(document.sections) > 1
            chunking_occurred = False  # Initialize chunking flag

            async def process_one_section(section):
                """Process a single section - same logic as before, just wrapped in async function."""
                section_responses = {}
                nonlocal chunking_occurred  # Allow modification of outer scope variable

                # Short-circuit: skip sections whose class is marked as
                # excluded (e.g., static instruction pages). No rule
                # validation is meaningful for boilerplate content.
                from idp_common.section_exclusion import is_section_excluded

                if is_section_excluded(section):
                    logger.info(
                        "Rule validation skipped for excluded section %s "
                        "(class=%s, reason=%s)",
                        section.section_id,
                        section.classification,
                        section.exclusion_reason or "excluded",
                    )
                    return section_responses, 0, False

                # Read text from section pages (following summarization service pattern)
                sorted_page_ids = sorted(section.page_ids, key=int)
                all_text = ""
                for page_id in sorted_page_ids:
                    if page_id not in document.pages:
                        error_msg = f"Page {page_id} not found in document"
                        logger.error(error_msg)
                        document.errors.append(error_msg)
                        continue

                    page = document.pages[page_id]
                    page_text = s3.get_text_content(page.parsed_text_uri)
                    all_text += f"<page-number>{page_id}</page-number>\n{page_text}\n\n"

                if not all_text:
                    logger.warning(
                        f"No text content found in section {section.section_id}"
                    )
                    return section_responses

                # Read extraction results if available (following summarization service pattern)
                extraction_results = {}
                logger.debug(
                    f"Section {section.section_id} extraction_result_uri: {section.extraction_result_uri}"
                )
                if section.extraction_result_uri:
                    try:
                        extraction_data = s3.get_json_content(
                            section.extraction_result_uri
                        )
                        extraction_results = extraction_data.get("inference_result", {})
                        logger.debug(
                            f"Loaded extraction results for section {section.section_id}"
                        )
                        logger.debug(
                            f"Extraction data keys: {list(extraction_data.keys()) if extraction_data else 'None'}"
                        )
                        logger.debug(
                            f"Inference result keys: {list(extraction_results.keys()) if extraction_results else 'Empty dict'}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to load extraction results for section {section.section_id}: {e}"
                        )
                else:
                    logger.debug(
                        f"No extraction_result_uri found for section {section.section_id}"
                    )

                content = all_text

                # Check if chunking is needed - using page-aware chunking
                chunks = self._chunk_pages_with_overlap(
                    content,
                    self.max_chunk_size,
                    self.token_size,
                    self.overlap_percentage,
                )

                # Track if chunking occurred
                if len(chunks) > 1:
                    chunking_occurred = True

                # Return 0 if no chunking, otherwise return actual chunk count
                chunks_count = len(chunks) if len(chunks) > 1 else 0

                # Create section URI for reference (removed unused variable)

                for chunk_idx, chunk in enumerate(chunks):
                    # Process each policy type concurrently
                    # Filter by matched_policy_types - skip if None or empty
                    all_policy_types = self._get_policy_types(config)
                    if (
                        document.rule_validation_result
                        and document.rule_validation_result.matched_policy_types
                    ):
                        policy_types_to_process = [
                            pt
                            for pt in all_policy_types
                            if pt
                            in document.rule_validation_result.matched_policy_types
                        ]
                        logger.info(
                            f"Filtering policy types by matched_policy_types: {policy_types_to_process}"
                        )
                    elif (
                        document.rule_validation_result
                        and document.rule_validation_result.matched_policy_types
                        is not None
                    ):
                        # matched_policy_types is empty list - skip processing
                        logger.info(
                            "matched_policy_types is empty - skipping rule validation"
                        )
                        policy_types_to_process = []
                    else:
                        # No rule_validation_result set - skip processing
                        logger.info(
                            "No matched_policy_types set - skipping rule validation"
                        )
                        policy_types_to_process = []

                    tasks = []
                    for policy_type in policy_types_to_process:
                        task = self._process_policy_type(
                            policy_type=policy_type,
                            user_history=chunk,
                            config=config,
                            extraction_results=extraction_results,
                        )
                        tasks.append(task)

                    # Wait for all policy types to complete
                    responses = await asyncio.gather(*tasks)

                    # Organize responses for this section
                    for policy_idx, policy_type in enumerate(policy_types_to_process):
                        if policy_type not in section_responses:
                            section_responses[policy_type] = (
                                {} if multiple_sections else []
                            )

                        if multiple_sections:
                            # For multiple sections, organize by rule
                            for response in responses[policy_idx]:
                                rule = response["rule"]
                                if rule not in section_responses[policy_type]:
                                    section_responses[policy_type][rule] = []
                                section_responses[policy_type][rule].append(response)
                        else:
                            # For single section, just append
                            section_responses[policy_type].extend(responses[policy_idx])

                return section_responses, chunks_count, chunking_occurred

            # Create tasks for all sections and run them in parallel
            section_tasks = [
                process_one_section(section) for section in document.sections
            ]
            section_results = await asyncio.gather(*section_tasks)

            # Merge results from all sections
            total_chunks_created = 0
            for section_result in section_results:
                section_responses, chunks_count, section_chunking = section_result
                total_chunks_created += chunks_count
                if section_chunking:
                    chunking_occurred = True

                for policy_type, responses in section_responses.items():
                    if policy_type not in all_responses:
                        all_responses[policy_type] = {} if multiple_sections else []

                    if multiple_sections:
                        # Merge responses from multiple sections
                        for rule, rule_responses in responses.items():
                            if rule not in all_responses[policy_type]:
                                all_responses[policy_type][rule] = []
                            all_responses[policy_type][rule].extend(rule_responses)
                    else:
                        # Extend responses for single section
                        all_responses[policy_type].extend(responses)

            # Save section results to S3 with chunking metadata
            output_bucket = document.output_bucket
            section_output_key = f"{document.input_key}/rule_validation/sections/section_{document.sections[0].section_id}_responses.json"
            section_output_uri = f"s3://{output_bucket}/{section_output_key}"

            # Add chunking metadata to the response
            section_result = {
                "section_id": document.sections[0].section_id,
                "chunking_occurred": chunking_occurred,
                "chunks_created": total_chunks_created,
                "responses": all_responses,
            }

            # Save all responses for this section to S3
            s3.write_content(
                section_result,
                output_bucket,
                section_output_key,
                content_type="application/json",
            )

            # Calculate timing
            self.timing_metrics["end_time"] = datetime.now()
            self.timing_metrics["total_duration"] = (
                self.timing_metrics["end_time"] - self.timing_metrics["start_time"]
            ).total_seconds()

            # Create result using factory method
            result = RuleValidationResult.for_section(
                document_id=document.id,
                section_uri=section_output_uri,
                timing_metrics=self.timing_metrics,
                chunking_occurred=chunking_occurred,
                chunks_created=total_chunks_created,
            )

            # Store result for this section (like extraction stores in section.extraction_result_uri)
            document.rule_validation_result = result

            # Merge metering into document separately (following extraction service pattern)
            document.metering = utils.merge_metering_data(
                document.metering, self.token_metrics
            )

            # Update document status to success
            return self._update_document_status(document, success=True)

        except Exception as e:
            error_msg = f"Error validating document {document.id}: {str(e)}"
            logger.error(error_msg)
            document.errors.append(error_msg)
            return self._update_document_status(
                document, success=False, error_message=error_msg
            )

    def validate_document(self, document: Document) -> Document:
        """
        Synchronous wrapper for validate_document_async.
        Handles both regular Python scripts and Jupyter notebook environments.

        Args:
            document: The Document object to validate

        Returns:
            Document with updated rule_validation_result
        """
        import concurrent.futures

        try:
            # Try to get the current event loop
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're in an environment with a running event loop (like Jupyter)
                # Use ThreadPoolExecutor to run the async function in a separate thread
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        asyncio.run,
                        self.validate_document_async(document, self.config.to_dict()),
                    )
                    return future.result()
            else:
                # Event loop exists but not running, we can use it
                return loop.run_until_complete(
                    self.validate_document_async(document, self.config.to_dict())
                )
        except RuntimeError:
            # No event loop exists, create a new one
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(
                    self.validate_document_async(document, self.config.to_dict())
                )
            finally:
                loop.close()
