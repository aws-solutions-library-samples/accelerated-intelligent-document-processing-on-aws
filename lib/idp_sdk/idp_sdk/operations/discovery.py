# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Discovery operations for IDP SDK."""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from idp_sdk._core.naming import resolve_config_profile
from idp_sdk.exceptions import IDPConfigurationError, IDPResourceNotFoundError
from idp_sdk.models.discovery import (
    AutoDetectResult,
    AutoDetectSection,
    DiscoveredClassResult,
    DiscoveryBatchResult,
    DiscoveryResult,
    MultiDocDiscoveryResult,
)

logger = logging.getLogger(__name__)


class DiscoveryOperation:
    """Document class discovery operations.

    Provides programmatic access to IDP Discovery, which analyzes documents
    using Amazon Bedrock to automatically generate JSON Schema definitions
    for document classes.

    Two operating modes:

    **Stack-connected mode** (with ``stack_name``):
        Uses the stack's discovery configuration (model, prompts) from DynamoDB
        and saves the discovered schema to the stack's configuration.

    **Local mode** (without ``stack_name``):
        Uses system default discovery settings, calls Bedrock directly with
        local file bytes, and returns the schema without saving anywhere.
        The schema is printed to stdout and/or written to a file via CLI.

    Example:
        Stack-connected:
        >>> client = IDPClient(stack_name="my-idp-stack")
        >>> result = client.discovery.run("./samples/w2/w2-sample.pdf")

        Local (no stack needed):
        >>> client = IDPClient()
        >>> result = client.discovery.run("./invoice.pdf")
        >>> print(json.dumps(result.json_schema, indent=2))
    """

    def __init__(self, client):
        self._client = client

    def run(
        self,
        document_path: str,
        ground_truth_path: Optional[str] = None,
        config_version: Optional[str] = None,
        stack_name: Optional[str] = None,
        page_range: Optional[str] = None,
        class_name_hint: Optional[str] = None,
        auto_detect: bool = False,
        model_id: Optional[str] = None,
        *,
        config_profile: Optional[str] = None,
        **kwargs,
    ) -> "DiscoveryResult | DiscoveryBatchResult":
        """Run discovery on a single document to generate a document class schema.

        If a stack_name is available (via client or parameter), operates in
        stack-connected mode: uses the stack's discovery config and saves the
        schema to DynamoDB. If no stack_name, operates in local mode: uses
        system defaults and returns the schema without saving.

        Args:
            document_path: Local path to the document file (PDF, PNG, JPG, TIFF)
            ground_truth_path: Optional local path to a JSON ground truth file.
            config_version: Configuration profile to save to (stack mode only).
            config_profile: Configuration profile (the current name for
                config_version; either may be given, not both with different values).
            stack_name: Optional stack name override.
            page_range: Optional page range string (e.g., "1-3") to extract
                specific pages from a PDF before discovery.
            class_name_hint: Optional hint for the document class name. When
                provided, the LLM will use this as the $id value.
            auto_detect: If True, auto-detect section boundaries first, then
                discover each section. Returns DiscoveryBatchResult.
            model_id: Optional Bedrock model ID override (e.g.,
                ``us.anthropic.claude-opus-4-6-v1``). When provided, this
                replaces the discovery ``model_id`` from the system defaults
                or stack config for this call only. Applies to both
                with-ground-truth and without-ground-truth modes, and to
                auto-detect when combined with ``auto_detect=True``.
            **kwargs: Additional parameters.

        Returns:
            DiscoveryResult for single document discovery, or
            DiscoveryBatchResult when auto_detect=True.

        Raises:
            FileNotFoundError: If the document or ground truth file doesn't exist.
        """
        config_version = resolve_config_profile(config_profile, config_version)
        doc_path = Path(document_path)
        if not doc_path.exists():
            raise FileNotFoundError(f"Document not found: {document_path}")

        # If auto_detect is True, detect sections first then discover each
        if auto_detect:
            return self._run_auto_detect_and_discover(
                doc_path, config_version, stack_name, model_id=model_id
            )

        gt_data = None
        if ground_truth_path:
            gt_path = Path(ground_truth_path)
            if not gt_path.exists():
                raise FileNotFoundError(
                    f"Ground truth file not found: {ground_truth_path}"
                )
            gt_data = json.loads(gt_path.read_text(encoding="utf-8"))

        # Read file bytes locally — never upload to S3
        file_bytes = doc_path.read_bytes()

        # Determine mode: stack-connected or local
        resolved_stack = stack_name or self._client._stack_name
        if resolved_stack:
            return self._run_with_stack(
                resolved_stack,
                doc_path,
                file_bytes,
                gt_data,
                config_version,
                page_range=page_range,
                class_name_hint=class_name_hint,
                model_id=model_id,
            )
        else:
            return self._run_local(
                doc_path,
                file_bytes,
                gt_data,
                page_range=page_range,
                class_name_hint=class_name_hint,
                model_id=model_id,
            )

    def auto_detect_sections(
        self,
        document_path: str,
        stack_name: Optional[str] = None,
        model_id: Optional[str] = None,
    ) -> AutoDetectResult:
        """Detect document section boundaries using LLM analysis.

        Sends the full PDF to Amazon Bedrock and asks it to identify where
        different document types begin and end within a multi-page package.

        Args:
            document_path: Local path to a PDF document.
            stack_name: Optional stack name override.
            model_id: Optional Bedrock model ID override for section detection.
                When provided, replaces the configured ``auto_split.model_id``.

        Returns:
            AutoDetectResult with detected section boundaries.

        Raises:
            FileNotFoundError: If the document doesn't exist.
        """
        doc_path = Path(document_path)
        if not doc_path.exists():
            raise FileNotFoundError(f"Document not found: {document_path}")

        file_bytes = doc_path.read_bytes()
        resolved_stack = stack_name or self._client._stack_name

        if resolved_stack:
            return self._auto_detect_with_stack(
                resolved_stack, doc_path, file_bytes, model_id=model_id
            )
        else:
            return self._auto_detect_local(doc_path, file_bytes, model_id=model_id)

    def run_multi_section(
        self,
        document_path: str,
        page_ranges: List[Dict[str, Any]],
        config_version: Optional[str] = None,
        stack_name: Optional[str] = None,
        model_id: Optional[str] = None,
        *,
        config_profile: Optional[str] = None,
    ) -> DiscoveryBatchResult:
        """Discover multiple document classes from page ranges in a single PDF.

        Each page range produces an independent discovery job. Page extraction
        uses pypdfium2 to create sub-PDFs before sending to Bedrock.

        Args:
            document_path: Local path to a multi-page PDF.
            page_ranges: List of dicts with keys: 'start' (int), 'end' (int),
                and optional 'label' (str) for class name hint.
                Example: [{"start": 1, "end": 2, "label": "W2 Form"},
                          {"start": 3, "end": 5, "label": "Invoice"}]
            config_version: Configuration profile to save to (stack mode only).
            config_profile: Configuration profile (the current name for
                config_version; either may be given, not both with different values).
            stack_name: Optional stack name override.
            model_id: Optional Bedrock model ID override. Applied to every
                per-range discovery call.

        Returns:
            DiscoveryBatchResult with one result per page range.
        """
        config_version = resolve_config_profile(config_profile, config_version)
        doc_path = Path(document_path)
        if not doc_path.exists():
            raise FileNotFoundError(f"Document not found: {document_path}")

        results: List[DiscoveryResult] = []
        for pr in page_ranges:
            start = pr.get("start", 1)
            end = pr.get("end", start)
            label = pr.get("label")
            range_str = f"{start}-{end}"

            result = self.run(
                document_path=document_path,
                config_version=config_version,
                stack_name=stack_name,
                page_range=range_str,
                class_name_hint=label,
                model_id=model_id,
            )
            # Annotate result with page range info
            result.page_range = range_str
            results.append(result)

        succeeded = sum(1 for r in results if r.status == "SUCCESS")
        failed = sum(1 for r in results if r.status != "SUCCESS")

        return DiscoveryBatchResult(
            total=len(results),
            succeeded=succeeded,
            failed=failed,
            results=results,
        )

    def _run_auto_detect_and_discover(
        self,
        doc_path: Path,
        config_version: Optional[str],
        stack_name: Optional[str],
        model_id: Optional[str] = None,
    ) -> DiscoveryBatchResult:
        """Auto-detect sections then discover each one."""
        detect_result = self.auto_detect_sections(
            document_path=str(doc_path), stack_name=stack_name, model_id=model_id
        )

        if detect_result.status != "SUCCESS" or not detect_result.sections:
            return DiscoveryBatchResult(total=0, succeeded=0, failed=0, results=[])

        page_ranges = [
            {
                "start": s.start,
                "end": s.end,
                "label": s.type,
            }
            for s in detect_result.sections
        ]

        return self.run_multi_section(
            document_path=str(doc_path),
            page_ranges=page_ranges,
            config_version=config_version,
            stack_name=stack_name,
            model_id=model_id,
        )

    def _auto_detect_with_stack(
        self,
        stack_name: str,
        doc_path: Path,
        file_bytes: bytes,
        model_id: Optional[str] = None,
    ) -> AutoDetectResult:
        """Auto-detect sections using stack config."""
        try:
            config_table = self._get_config_table(stack_name)
            os.environ["CONFIGURATION_TABLE_NAME"] = config_table

            from idp_common.discovery.classes_discovery import ClassesDiscovery

            discovery = ClassesDiscovery(
                input_bucket="local",
                input_prefix=doc_path.name,
                region=self._client._region,
            )

            sections = discovery.auto_detect_sections(
                input_bucket="local",
                input_prefix=doc_path.name,
                file_bytes=file_bytes,
                model_id=model_id,
            )

            return AutoDetectResult(
                status="SUCCESS",
                sections=[
                    AutoDetectSection(
                        start=s.get("start", 1),
                        end=s.get("end", 1),
                        type=s.get("type"),
                    )
                    for s in sections
                ],
                document_path=str(doc_path),
            )

        except Exception as e:
            logger.error(f"Auto-detect failed for {doc_path}: {e}")
            return AutoDetectResult(
                status="FAILED",
                document_path=str(doc_path),
                error=str(e),
            )

    def _auto_detect_local(
        self,
        doc_path: Path,
        file_bytes: bytes,
        model_id: Optional[str] = None,
    ) -> AutoDetectResult:
        """Auto-detect sections using system defaults (no stack needed)."""
        try:
            from idp_common import bedrock
            from idp_common.config.merge_utils import load_system_defaults

            defaults = load_system_defaults("pattern-2")
            discovery_cfg = defaults.get("discovery", {})
            auto_cfg = discovery_cfg.get("auto_split", {})

            # Caller override wins over system default
            model_id = model_id or auto_cfg.get("model_id", "us.amazon.nova-pro-v1:0")

            # Grok and GPT-5.x cannot accept the `document` content block built
            # below (text + image input only), and would silently drop the PDF
            # and hallucinate. The stack path is guarded by config validation and
            # the discovery picklists; this local path takes a caller-supplied
            # model_id, so guard it here too.
            from idp_common.bedrock.client import document_blocks_unsupported_reason

            reason = document_blocks_unsupported_reason(model_id)
            if reason:
                raise IDPConfigurationError(
                    f"Model '{model_id}' is not supported for discovery: {reason}. "
                    "Discovery sends whole-PDF document blocks. Choose an "
                    "Anthropic or Nova model."
                )

            system_prompt = auto_cfg.get(
                "system_prompt",
                "You are an expert document analyst. Your task is to identify "
                "distinct document sections within a multi-page document package.",
            )
            user_prompt = auto_cfg.get(
                "user_prompt",
                "Analyze this multi-page document package. Identify the page boundaries "
                "where different document types or sections begin and end.\n\n"
                "For each distinct document section, provide:\n"
                '- "start": the first page number (1-based)\n'
                '- "end": the last page number (1-based)\n'
                '- "type": a short label for the document type\n\n'
                "Return ONLY a JSON array:\n"
                '[{"start": 1, "end": 2, "type": "Letter"}, {"start": 3, "end": 3, "type": "Invoice"}]',
            )
            top_p = auto_cfg.get("top_p", 0.1)
            max_tokens = auto_cfg.get("max_tokens", 4096)

            content = [
                {
                    "document": {
                        "format": "pdf",
                        "name": "document_messages",
                        "source": {"bytes": file_bytes},
                    }
                },
                {"text": user_prompt},
            ]

            region = self._client._region or os.environ.get("AWS_REGION", "us-west-2")
            bedrock_client = bedrock.BedrockClient(region=region)

            response = bedrock_client.invoke_model(
                model_id=model_id,
                system_prompt=system_prompt,
                content=content,
                temperature=0.0,
                top_p=top_p,
                max_tokens=max_tokens,
                context="AutoDetectSectionsLocal",
            )

            content_text = bedrock.extract_text_from_response(response)
            sections_raw = json.loads(_extract_json(content_text))

            if not isinstance(sections_raw, list):
                raise ValueError(
                    f"Expected JSON array, got {type(sections_raw).__name__}"
                )

            return AutoDetectResult(
                status="SUCCESS",
                sections=[
                    AutoDetectSection(
                        start=s.get("start", 1),
                        end=s.get("end", 1),
                        type=s.get("type"),
                    )
                    for s in sections_raw
                ],
                document_path=str(doc_path),
            )

        except Exception as e:
            logger.error(f"Local auto-detect failed for {doc_path}: {e}")
            return AutoDetectResult(
                status="FAILED",
                document_path=str(doc_path),
                error=str(e),
            )

    def _run_with_stack(
        self,
        stack_name: str,
        doc_path: Path,
        file_bytes: bytes,
        gt_data: Optional[dict],
        config_version: Optional[str],
        page_range: Optional[str] = None,
        class_name_hint: Optional[str] = None,
        model_id: Optional[str] = None,
    ) -> DiscoveryResult:
        """Stack-connected mode: uses stack config, saves schema to DynamoDB."""
        try:
            # Get config table from stack resources
            config_table = self._get_config_table(stack_name)
            os.environ["CONFIGURATION_TABLE_NAME"] = config_table

            from idp_common.discovery.classes_discovery import ClassesDiscovery

            # Try loading the requested config version; fall back to active
            # config if the version doesn't exist yet (user wants to create it).
            try:
                discovery = ClassesDiscovery(
                    input_bucket="local",
                    input_prefix=doc_path.name,
                    region=self._client._region,
                    version=config_version,
                )
            except Exception:
                if config_version is None:
                    raise
                logger.warning(
                    f"Config version '{config_version}' not found, "
                    f"reading from active config and saving to '{config_version}'"
                )
                discovery = ClassesDiscovery(
                    input_bucket="local",
                    input_prefix=doc_path.name,
                    region=self._client._region,
                    version=None,
                )
                discovery.version = config_version

            # Only save to config if a version was explicitly specified
            save = config_version is not None

            if gt_data:
                result = discovery.discovery_classes_with_document_and_ground_truth(
                    input_bucket="local",
                    input_prefix=doc_path.name,
                    file_bytes=file_bytes,
                    ground_truth_data=gt_data,
                    save_to_config=save,
                    page_range=page_range,
                    model_id=model_id,
                )
            else:
                result = discovery.discovery_classes_with_document(
                    input_bucket="local",
                    input_prefix=doc_path.name,
                    file_bytes=file_bytes,
                    save_to_config=save,
                    page_range=page_range,
                    class_name_hint=class_name_hint,
                    model_id=model_id,
                )

            schema = result.get("schema")
            doc_class = None
            if schema:
                doc_class = schema.get("$id") or schema.get("x-aws-idp-document-type")

            return DiscoveryResult(
                status="SUCCESS",
                document_class=doc_class,
                json_schema=schema,
                config_version=config_version,
                document_path=str(doc_path),
                page_range=page_range,
            )

        except Exception as e:
            logger.error(f"Discovery failed for {doc_path}: {e}")
            return DiscoveryResult(
                status="FAILED",
                document_path=str(doc_path),
                error=str(e),
            )

    def _run_local(
        self,
        doc_path: Path,
        file_bytes: bytes,
        gt_data: Optional[dict],
        max_retries: int = 3,
        page_range: Optional[str] = None,
        class_name_hint: Optional[str] = None,
        model_id: Optional[str] = None,
    ) -> DiscoveryResult:
        """Local mode: uses system defaults, no stack needed, no config save."""
        try:
            from idp_common import bedrock, image
            from idp_common.config.merge_utils import load_system_defaults

            # If a page range is specified and the file is a PDF, extract only those pages
            file_extension = doc_path.suffix.lower().lstrip(".")
            if page_range and file_extension == "pdf":
                from idp_common.discovery.classes_discovery import ClassesDiscovery

                start_page, end_page = ClassesDiscovery.parse_page_range(page_range)
                file_bytes = ClassesDiscovery.extract_pdf_pages(
                    file_bytes, start_page, end_page
                )

            # Load system defaults to get discovery prompts and model settings
            defaults = load_system_defaults("pattern-2")
            discovery_cfg = defaults.get("discovery", {})
            mode_cfg = (
                discovery_cfg.get("with_ground_truth", {})
                if gt_data
                else discovery_cfg.get("without_ground_truth", {})
            )

            # Caller override wins over system default
            model_id = model_id or mode_cfg.get("model_id", "us.amazon.nova-pro-v1:0")

            # Grok and GPT-5.x cannot accept the `document` content block built
            # below (text + image input only), and would silently drop the PDF
            # and hallucinate. The stack path is guarded by config validation and
            # the discovery picklists; this local path takes a caller-supplied
            # model_id, so guard it here too.
            from idp_common.bedrock.client import document_blocks_unsupported_reason

            reason = document_blocks_unsupported_reason(model_id)
            if reason:
                raise IDPConfigurationError(
                    f"Model '{model_id}' is not supported for discovery: {reason}. "
                    "Discovery sends whole-PDF document blocks. Choose an "
                    "Anthropic or Nova model."
                )

            system_prompt = mode_cfg.get(
                "system_prompt",
                "You are an expert in processing forms. Extracting data from images and documents",
            )
            temperature = mode_cfg.get("temperature", 0.0)
            top_p = mode_cfg.get("top_p", 0.0)
            max_tokens = mode_cfg.get("max_tokens", 10000)

            # Build prompt
            sample_format = _sample_output_format()
            if gt_data:
                user_prompt = mode_cfg.get("user_prompt") or _prompt_with_gt(gt_data)
                if "{ground_truth_json}" in user_prompt:
                    user_prompt = user_prompt.replace(
                        "{ground_truth_json}", json.dumps(gt_data, indent=2)
                    )
            else:
                user_prompt = mode_cfg.get("user_prompt") or _prompt_without_gt()

            # Add class name hint to prompt if provided
            if class_name_hint:
                user_prompt += (
                    f'\nIMPORTANT: Use "{class_name_hint}" as the document class name. '
                    f'Set "$id" and "x-aws-idp-document-type" to "{class_name_hint}".'
                )

            # Create content with file bytes
            if file_extension == "pdf":
                content = [
                    {
                        "document": {
                            "format": "pdf",
                            "name": "document_messages",
                            "source": {"bytes": file_bytes},
                        }
                    },
                ]
            else:
                content = [image.prepare_bedrock_image_attachment(file_bytes)]

            # Call Bedrock with retry/validation loop
            region = self._client._region or os.environ.get("AWS_REGION", "us-west-2")
            bedrock_client = bedrock.BedrockClient(region=region)

            validation_feedback = ""
            for attempt in range(max_retries):
                try:
                    retry_prompt = ""
                    if attempt > 0 and validation_feedback:
                        retry_prompt = (
                            f"\n\nPREVIOUS ATTEMPT FAILED: {validation_feedback}\n"
                            f"Please fix the issue and generate a valid JSON Schema.\n\n"
                        )

                    full_prompt = (
                        f"{retry_prompt}{user_prompt}\n"
                        f"Format the extracted data using the below JSON format:\n{sample_format}"
                    )

                    response = bedrock_client.invoke_model(
                        model_id=model_id,
                        system_prompt=system_prompt,
                        content=content + [{"text": full_prompt}],
                        temperature=temperature,
                        top_p=top_p,
                        max_tokens=max_tokens,
                        context="ClassesDiscoveryLocal",
                    )

                    content_text = bedrock.extract_text_from_response(response)
                    schema = json.loads(_extract_json(content_text))

                    is_valid, error_msg = _validate_json_schema(schema)
                    if is_valid:
                        logger.info(
                            f"Successfully generated valid JSON Schema on attempt {attempt + 1}"
                        )
                        doc_class = schema.get("$id") or schema.get(
                            "x-aws-idp-document-type"
                        )
                        return DiscoveryResult(
                            status="SUCCESS",
                            document_class=doc_class,
                            json_schema=schema,
                            document_path=str(doc_path),
                        )
                    else:
                        validation_feedback = error_msg
                        logger.warning(
                            f"Invalid schema on attempt {attempt + 1}: {error_msg}"
                        )

                except json.JSONDecodeError as e:
                    validation_feedback = f"Invalid JSON format: {str(e)}"
                    logger.warning(f"JSON parse error on attempt {attempt + 1}: {e}")
                except Exception as e:
                    logger.error(f"Error on attempt {attempt + 1}: {e}")
                    if attempt == max_retries - 1:
                        raise

            return DiscoveryResult(
                status="FAILED",
                document_path=str(doc_path),
                error=f"Failed to generate valid schema after {max_retries} attempts",
            )

        except Exception as e:
            logger.error(f"Local discovery failed for {doc_path}: {e}")
            return DiscoveryResult(
                status="FAILED",
                document_path=str(doc_path),
                error=str(e),
            )

    def run_multi_doc(
        self,
        document_dir: Optional[str] = None,
        document_paths: Optional[List[str]] = None,
        embedding_model_id: Optional[str] = None,
        analysis_model_id: Optional[str] = None,
        save_to_config: bool = False,
        config_version: Optional[str] = None,
        output_dir: Optional[str] = None,
        progress_callback=None,
        stack_name: Optional[str] = None,
        region: Optional[str] = None,
        *,
        config_profile: Optional[str] = None,
    ) -> "MultiDocDiscoveryResult":
        """Run multi-document discovery on a collection of documents.

        Analyzes a directory of documents to automatically discover document
        classes using embedding → clustering → agentic analysis. Requires the
        ``multi_document_discovery`` extra for ``idp-common``::

            make setup    (or: pip install -e 'lib/idp_common_pkg[multi_document_discovery]')

        Args:
            document_dir: Directory containing documents to analyze (recursive).
            document_paths: Explicit list of document file paths. Provide either
                ``document_dir`` or ``document_paths``, not both.
            embedding_model_id: Bedrock embedding model for document similarity.
                Default: ``us.cohere.embed-v4:0``.
            analysis_model_id: Bedrock LLM for cluster analysis and schema
                generation. Default: ``us.anthropic.claude-sonnet-4-6``.
            save_to_config: If True, save discovered schemas to the stack's
                config. Requires ``stack_name`` and ``config_version``.
            config_version: Configuration profile to save schemas to.
            config_profile: Configuration profile (the current name for
                config_version; either may be given, not both with different values).
            output_dir: Directory to write discovered JSON schemas to.
            progress_callback: Optional callable(step_name, step_data) for
                pipeline status updates.
            stack_name: Optional stack name override.
            region: Optional AWS region override.

        Returns:
            MultiDocDiscoveryResult with discovered classes, reflection report,
            and pipeline statistics.

        Raises:
            ImportError: If multi_document_discovery dependencies are not installed.
            ValueError: If neither document_dir nor document_paths is provided.

        Example:
            Local mode (no stack needed)::

                >>> client = IDPClient()
                >>> result = client.discovery.run_multi_doc(
                ...     document_dir="./samples/",
                ...     output_dir="./discovered-schemas/",
                ... )
                >>> for cls in result.discovered_classes:
                ...     print(f"{cls.classification}: {cls.document_count} docs")

            Save to stack config::

                >>> client = IDPClient(stack_name="IDP")
                >>> result = client.discovery.run_multi_doc(
                ...     document_dir="./samples/",
                ...     save_to_config=True,
                ...     config_version="v2",
                ... )
        """
        config_version = resolve_config_profile(config_profile, config_version)
        # Validate parameters before attempting heavy import
        if not document_dir and not document_paths:
            raise ValueError("Either document_dir or document_paths must be provided.")

        # Determine config_version for save
        save_version = None
        resolved_stack = None
        if save_to_config:
            resolved_stack = stack_name or self._client._stack_name
            if not resolved_stack:
                raise IDPConfigurationError(
                    "stack_name is required when save_to_config=True"
                )
            if not config_version:
                raise IDPConfigurationError(
                    "config_version is required when save_to_config=True"
                )
            save_version = config_version

        try:
            from idp_common.discovery.multi_document_discovery import (
                MultiDocumentDiscovery,
            )
        except ImportError:
            return MultiDocDiscoveryResult(
                status="FAILED",
                error="Multi-document discovery requires additional dependencies. "
                "Install them with: make setup (or: pip install -e 'lib/idp_common_pkg[multi_document_discovery]')",
            )

        resolved_region = (
            region or self._client._region or os.environ.get("AWS_REGION", "us-west-2")
        )

        # Build config overrides
        config: Dict[str, Any] = {}
        if embedding_model_id:
            config["embedding_model_id"] = embedding_model_id
        if analysis_model_id:
            config["analysis_model_id"] = analysis_model_id

        if save_to_config and resolved_stack:
            # Set CONFIGURATION_TABLE_NAME for ClassesDiscovery
            config_table = self._get_config_table(resolved_stack)
            os.environ["CONFIGURATION_TABLE_NAME"] = config_table
            save_version = config_version

        try:
            discovery = MultiDocumentDiscovery(
                region=resolved_region,
                config=config,
            )

            raw_result = discovery.run_local_pipeline(
                document_dir=document_dir,
                document_paths=document_paths,
                config_version=save_version,
                progress_callback=progress_callback,
            )

            # Convert raw dataclass result to SDK Pydantic model
            discovered_classes = []
            for dc_dict in raw_result.discovered_classes:
                raw_ids = dc_dict.get("sample_doc_ids", [])
                discovered_classes.append(
                    DiscoveredClassResult(
                        cluster_id=dc_dict.get("cluster_id", -1),
                        classification=dc_dict.get("classification"),
                        json_schema=dc_dict.get("json_schema"),
                        document_count=dc_dict.get("document_count", 0),
                        sample_doc_ids=[str(x) for x in raw_ids] if raw_ids else [],
                        error=dc_dict.get("error"),
                    )
                )

            # Determine overall status
            num_successful = raw_result.num_successful_schemas
            num_failed = raw_result.num_failed_schemas
            if num_successful == 0 and num_failed > 0:
                status = "FAILED"
            elif num_failed > 0:
                status = "PARTIAL"
            else:
                status = "SUCCESS"

            result = MultiDocDiscoveryResult(
                status=status,
                discovered_classes=discovered_classes,
                reflection_report=raw_result.reflection_report,
                total_documents=raw_result.total_documents,
                total_clusters=raw_result.num_clusters,
                noise_documents=raw_result.num_failed_embeddings,
                config_version=save_version,
            )

            # Write schemas to output directory if requested
            if output_dir:
                self._write_schemas_to_dir(output_dir, discovered_classes)

            return result

        except Exception as e:
            logger.error(f"Multi-document discovery failed: {e}")
            return MultiDocDiscoveryResult(
                status="FAILED",
                error=str(e),
            )

    def _write_schemas_to_dir(
        self,
        output_dir: str,
        discovered_classes: List["DiscoveredClassResult"],
    ) -> None:
        """Write discovered JSON schemas to an output directory."""
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        for dc in discovered_classes:
            if dc.error or not dc.json_schema:
                continue
            class_name = dc.classification or dc.json_schema.get(
                "$id", f"cluster-{dc.cluster_id}"
            )
            # Sanitize filename
            safe_name = re.sub(r"[^\w\-.]", "_", class_name)
            schema_path = out_path / f"{safe_name}.json"
            schema_path.write_text(
                json.dumps(dc.json_schema, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info(f"Wrote schema to {schema_path}")

    def run_batch(
        self,
        document_paths: List[str],
        ground_truth_paths: Optional[List[Optional[str]]] = None,
        config_version: Optional[str] = None,
        stack_name: Optional[str] = None,
        model_id: Optional[str] = None,
        *,
        config_profile: Optional[str] = None,
        **kwargs,
    ) -> DiscoveryBatchResult:
        """Run discovery on multiple documents sequentially.

        Args:
            document_paths: List of local paths to document files.
            ground_truth_paths: Optional list of ground truth file paths.
            config_version: Configuration profile to save to.
            config_profile: Configuration profile (the current name for
                config_version; either may be given, not both with different values).
            stack_name: Optional stack name override.
            model_id: Optional Bedrock model ID override. Applied to every
                document in the batch.

        Returns:
            DiscoveryBatchResult with overall stats and per-document results.
        """
        config_version = resolve_config_profile(config_profile, config_version)
        if ground_truth_paths and len(ground_truth_paths) != len(document_paths):
            raise IDPConfigurationError(
                f"ground_truth_paths length ({len(ground_truth_paths)}) "
                f"must match document_paths length ({len(document_paths)})"
            )

        results: List[DiscoveryResult] = []
        for i, doc_path in enumerate(document_paths):
            gt_path = (
                ground_truth_paths[i]
                if ground_truth_paths and ground_truth_paths[i]
                else None
            )
            result = self.run(
                document_path=doc_path,
                ground_truth_path=gt_path,
                config_version=config_version,
                stack_name=stack_name,
                model_id=model_id,
            )
            results.append(result)

        succeeded = sum(1 for r in results if r.status == "SUCCESS")
        failed = sum(1 for r in results if r.status != "SUCCESS")

        return DiscoveryBatchResult(
            total=len(results),
            succeeded=succeeded,
            failed=failed,
            results=results,
        )

    def _get_config_table(self, stack_name: str) -> str:
        """Get ConfigurationTable from stack resources."""
        import boto3

        cfn = boto3.client("cloudformation", region_name=self._client._region)
        paginator = cfn.get_paginator("list_stack_resources")

        for page in paginator.paginate(StackName=stack_name):
            for resource in page.get("StackResourceSummaries", []):
                if resource.get("LogicalResourceId") == "ConfigurationTable":
                    return resource.get("PhysicalResourceId", "")

        raise IDPResourceNotFoundError("ConfigurationTable not found in stack.")


# --- Helpers for local mode ---


def _extract_json(text: str) -> str:
    """Strip markdown code fences from LLM response before JSON parsing."""
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if match:
        return match.group(1)
    return text


def _validate_json_schema(schema: Dict[str, Any]) -> tuple:
    """Validate that the response is a valid JSON Schema."""
    required_fields = ["$schema", "$id", "type", "properties"]
    for field in required_fields:
        if field not in schema:
            return False, f"Missing required field: {field}"
    if "x-aws-idp-document-type" not in schema:
        return False, "Missing x-aws-idp-document-type field"
    if schema.get("type") != "object":
        return False, "Root type must be 'object'"
    if not isinstance(schema.get("properties"), dict):
        return False, "Properties must be an object"
    return True, ""


# --- Standalone prompt helpers for local mode ---


def _sample_output_format() -> str:
    return """{
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "Form-1040",
    "x-aws-idp-document-type": "Form-1040",
    "type": "object",
    "description": "Brief summary of the document",
    "properties": {
        "PersonalInformation": {
            "type": "object",
            "description": "Personal information of Tax payer",
            "properties": {
                "FirstName": {"type": "string", "description": "First Name of Taxpayer"},
                "Age": {"type": "number", "description": "Age of Taxpayer"}
            }
        },
        "Dependents": {
            "type": "array",
            "description": "Dependents of taxpayer",
            "items": {
                "type": "object",
                "properties": {
                    "FirstName": {"type": "string", "description": "Dependent first name"},
                    "Age": {"type": "number", "description": "Dependent Age"}
                }
            }
        }
    }
}"""


def _prompt_without_gt() -> str:
    return """This image contains forms data. Analyze the form line by line.
Image may contain multiple pages, process all the pages.
Form may contain multiple name value pair in one line.
Extract all the names in the form including the name value pair which doesn't have value.

Generate a JSON Schema that describes the document structure:
- Use "$schema": "https://json-schema.org/draft/2020-12/schema"
- Set "$id" to a short document class name (e.g., "W4", "I-9", "Paystub")
- Set "x-aws-idp-document-type" to the same document class name
- Set "type": "object"
- Add "description" with a brief summary of the document (less than 50 words)

For the "properties" object:
- Group related fields as objects (type: "object") with their own "properties"
- For repeating/table data, use type: "array" with "items" containing object schema
- Each field should have "type" (string, number, boolean, etc.) and "description"

Do not extract the actual values, only the schema structure."""


def _prompt_with_gt(ground_truth_data: dict) -> str:
    gt_json = json.dumps(ground_truth_data, indent=2)
    return f"""This image contains unstructured data. Analyze the data line by line using the provided ground truth as reference.
<GROUND_TRUTH_REFERENCE>
{gt_json}
</GROUND_TRUTH_REFERENCE>

Generate a JSON Schema that describes the document structure using the ground truth as reference:
- Use "$schema": "https://json-schema.org/draft/2020-12/schema"
- Set "$id" to a short document class name (e.g., "W4", "I-9", "Paystub")
- Set "x-aws-idp-document-type" to the same document class name
- Set "type": "object"
- Add "description" with a brief summary of the document (less than 50 words)

For the "properties" object:
- Preserve the exact field names and groupings from ground truth
- Use nested objects (type: "object") for grouped fields with their own "properties"
- For repeating/table data, use type: "array" with "items" containing object schema

Match field names, data types, and structure from the ground truth reference.
Do not extract the actual values, only the schema structure."""
