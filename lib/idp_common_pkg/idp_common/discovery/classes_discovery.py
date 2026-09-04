# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import io
import json
import logging
import os
import re
from typing import Any, Dict, Optional, Set, Tuple, cast

import jsonschema
from jsonschema import Draft202012Validator

from idp_common import bedrock, image
from idp_common.bedrock.client import document_blocks_unsupported_reason
from idp_common.config import ConfigurationReader
from idp_common.config.class_names import is_valid_class_name, sanitize_class_name
from idp_common.config.class_settings import carry_forward_authored_settings
from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.models import IDPConfig
from idp_common.utils.s3util import S3Util

logger = logging.getLogger(__name__)


def _reject_model_without_document_blocks(model_id: Optional[str]) -> None:
    """Reject models that cannot accept Converse ``document`` content blocks.

    Discovery ingests whole PDFs via ``document`` blocks. Two families can't take
    them — OpenAI GPT-5.x (bedrock-mantle Responses API) and xAI Grok (rejects
    them outright: "This model doesn't support documents") — and both accept only
    text and image input. Routing either here would silently drop the document
    and hallucinate, so fail loudly instead. (Neither is offered in the discovery
    model picklists.)
    """
    reason = document_blocks_unsupported_reason(model_id)
    if reason:
        raise ValueError(
            f"Model '{model_id}' is not supported for discovery: {reason}. "
            "Discovery sends whole-PDF document blocks. Choose an Anthropic or "
            "Nova model."
        )


class ClassesDiscovery:
    def __init__(
        self,
        input_bucket: str,
        input_prefix: str,
        region: Optional[str] = None,
        version: Optional[str] = None,
    ):
        self.input_bucket = input_bucket
        self.input_prefix = input_prefix
        self.region = region or os.environ.get("AWS_REGION")
        self.version = version
        try:
            self.config_reader = ConfigurationReader()
            self.config_manager = ConfigurationManager()
            try:
                self.config: IDPConfig = cast(
                    IDPConfig,
                    self.config_reader.get_merged_configuration(
                        as_model=True, version=self.version
                    ),
                )
            except ValueError:
                # The target version doesn't exist yet (e.g. a fresh "quickstart"
                # version that will be created when we save the discovered class).
                # Discovery only needs the discovery settings, which come from the
                # active/default config, so load that for settings while keeping
                # self.version as the write target.
                logger.info(
                    f"Version '{self.version}' not found; loading active/default "
                    "config for discovery settings and creating it on save."
                )
                self.config = cast(
                    IDPConfig,
                    self.config_reader.get_merged_configuration(
                        as_model=True, version=None
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to load configuration from DynamoDB: {e}")
            raise Exception(f"Failed to load configuration from DynamoDB: {str(e)}")

        # Get discovery configuration from IDPConfig model
        self.discovery_config = self.config.discovery

        # Get model configuration for both scenarios
        self.without_gt_config = self.discovery_config.without_ground_truth
        self.with_gt_config = self.discovery_config.with_ground_truth

        # Initialize Bedrock client using the common pattern
        self.bedrock_client = bedrock.BedrockClient(region=self.region)

        return

    @staticmethod
    def parse_page_range(page_range: str) -> Tuple[int, int]:
        """
        Parse a page range string like "3-5" into (start, end) 1-based page numbers.

        Args:
            page_range: String in format "start-end" (e.g., "1-3", "5-5")

        Returns:
            Tuple of (start_page, end_page), both 1-based inclusive

        Raises:
            ValueError: If the format is invalid
        """
        page_range = page_range.strip()
        match = re.match(r"^(\d+)\s*-\s*(\d+)$", page_range)
        if not match:
            raise ValueError(
                f"Invalid page range format: '{page_range}'. Expected 'start-end' (e.g., '1-3')"
            )
        start = int(match.group(1))
        end = int(match.group(2))
        if start < 1:
            raise ValueError(f"Page numbers must be >= 1, got start={start}")
        if end < start:
            raise ValueError(f"End page ({end}) must be >= start page ({start})")
        return start, end

    @staticmethod
    def extract_pdf_pages(pdf_bytes: bytes, start_page: int, end_page: int) -> bytes:
        """
        Extract a range of pages from a PDF and return as new PDF bytes.

        Uses pypdfium2 to read the source PDF and create a new PDF containing
        only the specified page range.

        Args:
            pdf_bytes: The full PDF document as bytes
            start_page: 1-based start page number (inclusive)
            end_page: 1-based end page number (inclusive)

        Returns:
            bytes: A new PDF containing only the specified pages

        Raises:
            ValueError: If page range is out of bounds
        """
        import pypdfium2 as pdfium  # Lazy import — not available in all Lambda environments

        pdf_doc = pdfium.PdfDocument(pdf_bytes)
        try:
            num_pages = len(pdf_doc)
            if start_page < 1 or end_page > num_pages:
                raise ValueError(
                    f"Page range {start_page}-{end_page} is out of bounds. "
                    f"Document has {num_pages} pages."
                )

            # Convert to 0-based indices for pypdfium2
            page_indices = list(range(start_page - 1, end_page))

            # Create a new PDF with only the selected pages
            new_pdf = pdfium.PdfDocument.new()
            new_pdf.import_pages(pdf_doc, page_indices)

            # Write to bytes
            output_buffer = io.BytesIO()
            new_pdf.save(output_buffer)
            new_pdf.close()

            result_bytes = output_buffer.getvalue()
            logger.info(
                f"Extracted pages {start_page}-{end_page} from {num_pages}-page PDF "
                f"({len(pdf_bytes)} bytes -> {len(result_bytes)} bytes)"
            )
            return result_bytes
        finally:
            pdf_doc.close()

    def auto_detect_sections(
        self,
        input_bucket: str,
        input_prefix: str,
        file_bytes: Optional[bytes] = None,
        model_id: Optional[str] = None,
    ) -> list:
        """
        Use an LLM to automatically detect document section boundaries in a multi-page PDF.

        Sends the full document to Bedrock and asks it to identify where different
        document types begin and end, returning a list of page ranges with type labels.

        Args:
            input_bucket: S3 bucket name
            input_prefix: S3 key for the PDF document
            file_bytes: Optional document bytes. If provided, skips S3 read.
            model_id: Optional Bedrock model ID override. When provided, this
                replaces the ``auto_split.model_id`` from the discovery config
                for this call only. Useful for callers (e.g. ``idp-cli``) that
                want to use a more capable model than the configured default.

        Returns:
            list of dicts: [{"start": 1, "end": 3, "type": "W2-Form"}, ...]

        Raises:
            Exception: If auto-detection fails
        """
        logger.info(f"Auto-detecting sections in: s3://{input_bucket}/{input_prefix}")

        try:
            if file_bytes is not None:
                file_in_bytes = file_bytes
            else:
                file_in_bytes = S3Util.get_bytes(bucket=input_bucket, key=input_prefix)

            file_extension = os.path.splitext(input_prefix)[1].lower()[1:]

            # Use the auto_split config (falls back to defaults from base-discovery.yaml)
            auto_split_config = self.discovery_config.auto_split
            # Caller-supplied override takes precedence over configured model_id
            model_id = model_id or auto_split_config.model_id
            _reject_model_without_document_blocks(model_id)
            top_p = auto_split_config.top_p
            max_tokens = auto_split_config.max_tokens

            system_prompt = (
                auto_split_config.system_prompt
                or "You are an expert document analyst. Your task is to identify "
                "distinct document sections within a multi-page document package."
            )

            user_prompt = (
                auto_split_config.user_prompt
                or "Analyze this multi-page document package. Identify the page boundaries "
                "where different document types or sections begin and end.\n\n"
                "For each distinct document section, provide:\n"
                '- "start": the first page number (1-based)\n'
                '- "end": the last page number (1-based)\n'
                '- "type": a short label for the document type, using only letters, '
                "digits, hyphens and underscores — no spaces "
                '(e.g., "W2-Form", "Letter", "Invoice")\n\n'
                "Return ONLY a JSON array, no other text:\n"
                '[{"start": 1, "end": 2, "type": "Letter"}, {"start": 3, "end": 3, "type": "Invoice"}]'
            )

            content = self._create_content_list(
                prompt=user_prompt,
                document_content=file_in_bytes,
                file_extension=file_extension,
            )

            response = self.bedrock_client.invoke_model(
                model_id=model_id,
                system_prompt=system_prompt,
                content=content,
                temperature=0.0,  # Low temperature for consistent results
                top_p=top_p,
                max_tokens=max_tokens,
                context="AutoDetectSections",
            )

            from idp_common import bedrock as bedrock_mod

            content_text = bedrock_mod.extract_text_from_response(response)
            logger.info(f"Auto-detect response: {content_text}")

            # Parse the JSON response
            sections = json.loads(self._extract_json(content_text))

            if not isinstance(sections, list):
                raise ValueError(
                    f"Expected a JSON array, got: {type(sections).__name__}"
                )

            # Validate each section
            for section in sections:
                if not isinstance(section, dict):
                    raise ValueError(
                        f"Each section must be a dict, got: {type(section).__name__}"
                    )
                if "start" not in section or "end" not in section:
                    raise ValueError(f"Section missing 'start' or 'end': {section}")

            logger.info(f"Auto-detected {len(sections)} sections")
            return sections

        except Exception as e:
            logger.error(f"Error auto-detecting sections: {e}", exc_info=True)
            raise Exception(f"Failed to auto-detect sections: {str(e)}")

    def discovery_classes_with_document(
        self,
        input_bucket: str,
        input_prefix: str,
        file_bytes: Optional[bytes] = None,
        save_to_config: bool = True,
        page_range: Optional[str] = None,
        class_name_hint: Optional[str] = None,
        model_id: Optional[str] = None,
    ):
        """
        Create blueprint for document discovery.
        Process document/image:
            1. Extract labels from document
            2. Create Blueprint for the document.
            3. Create/Update blueprint with BDA project.

        Args:
            input_bucket: S3 bucket name
            input_prefix: S3 prefix (also used to determine file extension)
            file_bytes: Optional document bytes. If provided, skips S3 read.
            save_to_config: If True (default), saves schema to DynamoDB config.
                If False, returns the schema without saving.
            page_range: Optional page range string (e.g., "1-3") to extract
                specific pages from a PDF before discovery. If None, the
                entire document is used.
            class_name_hint: Optional hint for the document class name. When provided,
                the LLM will use this as the $id and x-aws-idp-document-type values.
            model_id: Optional Bedrock model ID override. When provided, this
                replaces the ``without_ground_truth.model_id`` from the discovery
                config for this call only.

        Returns:
            dict with status and optionally the discovered schema

        Raises:
            Exception: If blueprint creation fails
        """
        range_label = f" (pages {page_range})" if page_range else ""
        logger.info(
            f"Creating blueprint for document discovery: "
            f"s3://{input_bucket}/{input_prefix}{range_label}"
        )

        try:
            if file_bytes is not None:
                file_in_bytes = file_bytes
            else:
                file_in_bytes = S3Util.get_bytes(bucket=input_bucket, key=input_prefix)

            # Extract labels
            file_extension = os.path.splitext(input_prefix)[1].lower()
            # remove the .
            file_extension = file_extension[1:]

            # If a page range is specified and the file is a PDF, extract only those pages
            if page_range and file_extension == "pdf":
                start_page, end_page = self.parse_page_range(page_range)
                file_in_bytes = self.extract_pdf_pages(
                    file_in_bytes, start_page, end_page
                )

            logger.info(f" document len: {len(file_in_bytes)}")

            model_response = self._extract_data_from_document(
                file_in_bytes,
                file_extension,
                class_name_hint=class_name_hint,
                model_id=model_id,
            )
            logger.info(f"Extracted data from document: {model_response}")

            if model_response is None:
                raise Exception("Failed to extract data from document")

            # Model response is now a JSON Schema
            # No need to transform - it's already in the right format
            current_class = model_response

            if save_to_config:
                # Merge the new class with existing Default + Custom classes
                # and save to Custom config
                self._merge_and_save_class(current_class)

            return {"status": "SUCCESS", "schema": current_class}

        except Exception as e:
            logger.error(
                f"Error processing document {input_prefix}: {e}", exc_info=True
            )
            # Re-raise the exception to be handled by the caller
            raise Exception(f"Failed to process document {input_prefix}: {str(e)}")

    def discovery_classes_with_document_and_ground_truth(
        self,
        input_bucket: str,
        input_prefix: str,
        ground_truth_key: str = "",
        file_bytes: Optional[bytes] = None,
        ground_truth_data: Optional[dict] = None,
        save_to_config: bool = True,
        page_range: Optional[str] = None,
        model_id: Optional[str] = None,
    ):
        """
        Create optimized blueprint using ground truth data.

        Args:
            input_bucket: S3 bucket name
            input_prefix: S3 prefix for document (also used to determine file extension)
            ground_truth_key: S3 key for ground truth JSON file
            file_bytes: Optional document bytes. If provided, skips S3 read.
            ground_truth_data: Optional ground truth dict. If provided, skips S3 read for GT.
            save_to_config: If True (default), saves schema to DynamoDB config.
                If False, returns the schema without saving.
            page_range: Optional page range string (e.g., "1-3") to extract
                specific pages from a PDF before discovery. If None, the
                entire document is used.
            model_id: Optional Bedrock model ID override. When provided, this
                replaces the ``with_ground_truth.model_id`` from the discovery
                config for this call only.

        Returns:
            dict with status and optionally the discovered schema

        Raises:
            Exception: If blueprint creation fails
        """
        range_label = f" (pages {page_range})" if page_range else ""
        logger.info(
            f"Creating optimized blueprint with ground truth: "
            f"s3://{input_bucket}/{input_prefix}{range_label}"
        )

        try:
            # Load ground truth data from S3 or use provided data
            if ground_truth_data is None:
                ground_truth_data = self._load_ground_truth(
                    input_bucket, ground_truth_key
                )

            if file_bytes is not None:
                file_in_bytes = file_bytes
            else:
                file_in_bytes = S3Util.get_bytes(bucket=input_bucket, key=input_prefix)

            file_extension = os.path.splitext(input_prefix)[1].lower()[1:]

            # If a page range is specified and the file is a PDF, extract only those pages
            if page_range and file_extension == "pdf":
                start_page, end_page = self.parse_page_range(page_range)
                file_in_bytes = self.extract_pdf_pages(
                    file_in_bytes, start_page, end_page
                )

            model_response = self._extract_data_from_document_with_ground_truth(
                file_in_bytes,
                file_extension,
                ground_truth_data,
                model_id=model_id,
            )

            if model_response is None:
                raise Exception("Failed to extract data from document")

            # Model response is now a JSON Schema
            # No need to transform - it's already in the right format
            current_class = model_response

            if save_to_config:
                # Merge the new class with existing Default + Custom classes
                # and save to Custom config
                self._merge_and_save_class(current_class)

            return {"status": "SUCCESS", "schema": current_class}

        except Exception as e:
            logger.error(
                f"Error processing document with ground truth {input_prefix}: {e}",
                exc_info=True,
            )
            raise Exception(f"Failed to process document {input_prefix}: {str(e)}")

    def _merge_and_save_class(self, new_class: Dict[str, Any]) -> None:
        """
        Merge a new discovered class into the target version's configuration.

        This method only adds/updates the discovered class within the target version's
        own class list. It does NOT pull in classes from the default configuration version,
        keeping the target version's classes exactly as the user curated them, plus the
        newly discovered class.

        Steps:
        1. Read existing classes from the target version
        2. Add/update the new discovered class (deduplicate by $id), carrying
           forward any class-level setting discovery did not itself produce
        3. Save back to the target version

        Args:
            new_class: The newly discovered class schema to add/update
        """
        # The class id is generated by the LLM, so it can arrive with spaces or
        # punctuation despite the prompt asking for neither. Normalize it here —
        # the single write path for both discovery flows — because the id is
        # later composed into downstream resource names (BDA blueprint names
        # must match [a-zA-Z0-9-_]+), where an invalid character fails the call
        # rather than degrading. Prompt instructions are guidance; this is the
        # guarantee.
        synthesized = self._normalize_class_id(new_class)

        # Get class identifier for the new class
        new_class_id = new_class.get("$id") or new_class.get("x-aws-idp-document-type")
        logger.info(f"Merging discovered class: {new_class_id}")

        # Read existing config for the target version (raw, no Pydantic defaults)
        existing_custom = (
            self.config_manager.get_raw_configuration("Config", version=self.version)
            or {}
        )
        custom_classes = list(existing_custom.get("classes", []))
        logger.info(
            f"Found {len(custom_classes)} existing classes in version '{self.version}'"
        )

        # Build class list from existing version classes, deduplicating by ID
        classes_by_id: Dict[str, Dict[str, Any]] = {}
        for cls in custom_classes:
            cls_id = cls.get("$id") or cls.get("x-aws-idp-document-type")
            if cls_id:
                classes_by_id[cls_id] = cls

        # Add/update the new discovered class
        if new_class_id:
            # A version written before class ids were normalized can still hold
            # the un-normalized spelling of this very class ("Task cards"), so
            # match on the normalized form as well — otherwise re-discovering it
            # adds a near-duplicate beside the stale entry, and the two would
            # then compose the same BDA blueprint name prefix and fight over one
            # blueprint. Only an id that normalizes to *this* one is replaced;
            # unrelated classes keep their own ids untouched.
            stale_ids = sorted(
                cls_id
                for cls_id in classes_by_id
                if cls_id != new_class_id
                and sanitize_class_name(cls_id) == new_class_id
            )
            # Whose settings are carried across: the exact-id entry if there is
            # one, else the first stale spelling. Sorted, so the choice does not
            # depend on the order DynamoDB happened to return the classes in.
            existing_class = classes_by_id.get(new_class_id)
            settings_source = existing_class or (
                classes_by_id[stale_ids[0]] if stale_ids else None
            )
            for stale_id in stale_ids:
                logger.info(
                    "Replacing existing class %r with its normalized id %r.",
                    stale_id,
                    new_class_id,
                )
                if classes_by_id[stale_id] is not settings_source:
                    # More than one spelling normalizes to this id (or an exact
                    # entry already existed). Only one can supply the settings, so
                    # name the ones being dropped rather than losing them quietly.
                    logger.warning(
                        "Class %r also normalizes to %r; its class-level settings "
                        "are NOT carried forward (settings taken from %r). "
                        "Re-apply anything it alone had set.",
                        stale_id,
                        new_class_id,
                        settings_source.get("$id") if settings_source else None,
                    )
                del classes_by_id[stale_id]
            existing_class = settings_source

            if existing_class is not None:
                # Discovery owns `properties` and the keys it emits; every
                # other class-level setting on the existing class was authored
                # by a human and used to be erased here without a trace.
                carry_forward_authored_settings(existing_class, new_class, synthesized)

            classes_by_id[new_class_id] = new_class

        # Convert back to list
        merged_classes = list(classes_by_id.values())
        logger.info(f"Saving {len(merged_classes)} classes to version '{self.version}'")

        # Save to version config
        existing_custom["classes"] = merged_classes
        self.config_manager.save_raw_configuration(
            "Config", existing_custom, version=self.version
        )

    @staticmethod
    def _normalize_class_id(new_class: Dict[str, Any]) -> Set[str]:
        """Rewrite a discovered class's id in place to a portable form.

        Both ``$id`` and ``x-aws-idp-document-type`` are normalized so they
        stay equal to each other; the original text is kept in ``description``
        (when the class has none) so the human-readable name is not lost.
        A class id that is already valid is left untouched.

        Returns the keys this method filled in itself. They are not discovery
        output, so ``carry_forward_authored_settings`` lets an existing
        authored value override them.
        """
        original = new_class.get("$id") or new_class.get("x-aws-idp-document-type")
        if not original or is_valid_class_name(original):
            return set()

        sanitized = sanitize_class_name(original)
        if not sanitized:
            # Nothing usable to build a name from. Keep the id as-is rather
            # than inventing one — a placeholder would look like a real class.
            logger.warning(
                "Discovered class id %r contains no characters valid in a class "
                "name ([a-zA-Z0-9-_]); leaving it unchanged. Rename it before "
                "using features that derive resource names from the class id "
                "(e.g. BDA sync).",
                original,
            )
            return set()

        logger.info(
            "Renaming discovered class %r to %r so it is usable by downstream "
            "features (BDA blueprint names allow only [a-zA-Z0-9-_]).",
            original,
            sanitized,
        )
        if "$id" in new_class:
            new_class["$id"] = sanitized
        if "x-aws-idp-document-type" in new_class:
            new_class["x-aws-idp-document-type"] = sanitized
        if not new_class.get("description"):
            new_class["description"] = original
            return {"description"}
        return set()

    @staticmethod
    def _extract_json(text: str) -> str:
        """Strip markdown code fences from LLM response before JSON parsing."""
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
        if match:
            return match.group(1)
        return text

    def _validate_json_schema(self, schema: Dict[str, Any]) -> tuple[bool, str]:
        """
        Validate that the response is a valid JSON Schema.

        Args:
            schema: The schema to validate

        Returns:
            tuple: (is_valid, error_message)
        """
        try:
            # Check required fields for our document schema
            required_fields = ["$schema", "$id", "type", "properties"]
            for field in required_fields:
                if field not in schema:
                    return False, f"Missing required field: {field}"

            # Validate it's a proper JSON Schema
            Draft202012Validator.check_schema(schema)

            # Check our AWS IDP specific requirements
            if "x-aws-idp-document-type" not in schema:
                return False, "Missing x-aws-idp-document-type field"

            if schema.get("type") != "object":
                return False, "Root type must be 'object'"

            if not isinstance(schema.get("properties"), dict):
                return False, "Properties must be an object"

            return True, ""

        except jsonschema.SchemaError as e:
            return False, f"Invalid JSON Schema: {str(e)}"
        except Exception as e:
            return False, f"Validation error: {str(e)}"

    def _remove_duplicates(self, groups):
        for group in groups:
            groupAttributes = []
            groupAttributesArray = []
            if "groupAttributes" not in group:
                continue
            for groupAttribute in group["groupAttributes"]:
                groupAttributeName = groupAttribute.get("name")
                if groupAttributeName in groupAttributes:
                    logger.info(
                        f"ignoring the duplicate attribute {groupAttributeName}"
                    )
                    continue
                groupAttributes.append(groupAttributeName)
                groupAttributesArray.append(groupAttribute)
            group["groupAttributes"] = groupAttributesArray
        return groups

    def _load_ground_truth(self, bucket: str, key: str):
        """Load ground truth JSON data from S3."""
        try:
            ground_truth_bytes = S3Util.get_bytes(bucket=bucket, key=key)
            return json.loads(ground_truth_bytes.decode("utf-8"))
        except Exception as e:
            logger.error(f"Failed to load ground truth from s3://{bucket}/{key}: {e}")
            raise

    def _extract_data_from_document(
        self,
        document_content,
        file_extension,
        max_retries: int = 3,
        class_name_hint: Optional[str] = None,
        model_id: Optional[str] = None,
    ):
        """Extract data from document with retry logic for invalid schemas."""
        # Get configuration for without ground truth
        # Caller-supplied override takes precedence over configured model_id
        model_id = model_id or self.without_gt_config.model_id
        _reject_model_without_document_blocks(model_id)
        system_prompt = (
            self.without_gt_config.system_prompt
            or "You are an expert in processing forms. Extracting data from images and documents"
        )
        temperature = self.without_gt_config.temperature
        top_p = self.without_gt_config.top_p
        max_tokens = self.without_gt_config.max_tokens

        # Create user prompt with sample format
        user_prompt = (
            self.without_gt_config.user_prompt or self._prompt_classes_discovery()
        )
        sample_format = self._sample_output_format()
        logger.info(f"config prompt is : {self.without_gt_config.user_prompt}")
        logger.info(f"prompt is : {user_prompt}")
        logger.info(f"sample format is : {sample_format}")

        validation_feedback = ""
        for attempt in range(max_retries):
            try:
                # Add validation feedback if this is a retry
                retry_prompt = ""
                if attempt > 0 and validation_feedback:
                    retry_prompt = f"\n\nPREVIOUS ATTEMPT FAILED: {validation_feedback}\nPlease fix the issue and generate a valid JSON Schema.\n\n"

                # If class_name_hint is provided, instruct the LLM to use it as the class name.
                # The hint originates from auto-detected section labels, so it is
                # sanitized before injection — otherwise it overrides the schema
                # prompt's "no spaces" rule with a name BDA sync cannot use.
                class_hint_instruction = ""
                if class_name_hint:
                    hint = sanitize_class_name(class_name_hint) or class_name_hint
                    class_hint_instruction = (
                        f'\nIMPORTANT: Use "{hint}" as the document class name. '
                        f'Set "$id" and "x-aws-idp-document-type" to "{hint}".\n'
                    )

                full_prompt = f"{retry_prompt}{user_prompt}{class_hint_instruction}\nFormat the extracted data using the below JSON format:\n{sample_format}"
                # Create content for the user message
                content = self._create_content_list(
                    prompt=full_prompt,
                    document_content=document_content,
                    file_extension=file_extension,
                )

                # Use the configured parameters
                response = self.bedrock_client.invoke_model(
                    model_id=model_id,
                    system_prompt=system_prompt,
                    content=content,
                    temperature=temperature,
                    top_p=top_p,
                    max_tokens=max_tokens,
                    context="ClassesDiscovery",
                )

                # Extract text from response using the common pattern
                content_text = bedrock.extract_text_from_response(response)
                logger.debug(
                    f"Bedrock response (attempt {attempt + 1}): {content_text}"
                )

                # Parse JSON response
                schema = json.loads(self._extract_json(content_text))

                # Validate the schema
                is_valid, error_msg = self._validate_json_schema(schema)
                if is_valid:
                    logger.info(
                        f"Successfully generated valid JSON Schema on attempt {attempt + 1}"
                    )
                    return schema
                else:
                    validation_feedback = error_msg
                    logger.warning(
                        f"Invalid schema on attempt {attempt + 1}: {error_msg}"
                    )
                    if attempt == max_retries - 1:
                        logger.error(
                            f"Failed to generate valid schema after {max_retries} attempts"
                        )
                        return None

            except json.JSONDecodeError as e:
                validation_feedback = f"Invalid JSON format: {str(e)}"
                logger.warning(f"JSON parse error on attempt {attempt + 1}: {e}")
                if attempt == max_retries - 1:
                    logger.error(
                        f"Failed to generate valid JSON after {max_retries} attempts"
                    )
                    return None
            except Exception as e:
                logger.error(
                    f"Error extracting data with Bedrock on attempt {attempt + 1}: {e}"
                )
                if attempt == max_retries - 1:
                    return None

        return None

    def _create_content_list(self, prompt, document_content, file_extension):
        """Create content list for BedrockClient API."""
        if file_extension == "pdf":
            content = [
                {
                    "document": {
                        "format": "pdf",
                        "name": "document_messages",
                        "source": {"bytes": document_content},
                    }
                },
                {"text": prompt},
            ]
        else:
            # Prepare image for Bedrock
            image_content = image.prepare_bedrock_image_attachment(document_content)
            content = [
                image_content,
                {"text": prompt},
            ]

        return content

    def _extract_data_from_document_with_ground_truth(
        self,
        document_content,
        file_extension,
        ground_truth_data,
        max_retries: int = 3,
        model_id: Optional[str] = None,
    ):
        """Extract data from document using ground truth as reference with retry logic."""
        # Get configuration for with ground truth
        # Caller-supplied override takes precedence over configured model_id
        model_id = model_id or self.with_gt_config.model_id
        _reject_model_without_document_blocks(model_id)
        system_prompt = (
            self.with_gt_config.system_prompt
            or "You are an expert in processing forms. Extracting data from images and documents"
        )
        temperature = self.with_gt_config.temperature
        top_p = self.with_gt_config.top_p
        max_tokens = self.with_gt_config.max_tokens

        # Create enhanced prompt with ground truth
        user_prompt = (
            self.with_gt_config.user_prompt
            or self._prompt_classes_discovery_with_ground_truth(ground_truth_data)
        )

        # If user_prompt contains placeholder, replace it with ground truth
        if "{ground_truth_json}" in user_prompt:
            ground_truth_json = json.dumps(ground_truth_data, indent=2)
            base_prompt = user_prompt.replace("{ground_truth_json}", ground_truth_json)
        else:
            base_prompt = self._prompt_classes_discovery_with_ground_truth(
                ground_truth_data
            )

        sample_format = self._sample_output_format()

        validation_feedback = ""
        for attempt in range(max_retries):
            try:
                # Add validation feedback if this is a retry
                retry_prompt = ""
                if attempt > 0 and validation_feedback:
                    retry_prompt = f"\n\nPREVIOUS ATTEMPT FAILED: {validation_feedback}\nPlease fix the issue and generate a valid JSON Schema.\n\n"

                full_prompt = f"{retry_prompt}{base_prompt}\nFormat the extracted data using the below JSON format:\n{sample_format}"

                # Create content for the user message
                content = self._create_content_list(
                    prompt=full_prompt,
                    document_content=document_content,
                    file_extension=file_extension,
                )

                # Use the configured parameters
                response = self.bedrock_client.invoke_model(
                    model_id=model_id,
                    system_prompt=system_prompt,
                    content=content,
                    temperature=temperature,
                    top_p=top_p,
                    max_tokens=max_tokens,
                    context="ClassesDiscoveryWithGroundTruth",
                )

                # Extract text from response using the common pattern
                content_text = bedrock.extract_text_from_response(response)
                logger.debug(
                    f"Bedrock response with ground truth (attempt {attempt + 1}): {content_text}"
                )

                # Parse JSON response
                schema = json.loads(self._extract_json(content_text))

                # Validate the schema
                is_valid, error_msg = self._validate_json_schema(schema)
                if is_valid:
                    logger.info(
                        f"Successfully generated valid JSON Schema with ground truth on attempt {attempt + 1}"
                    )
                    return schema
                else:
                    validation_feedback = error_msg
                    logger.warning(
                        f"Invalid schema with ground truth on attempt {attempt + 1}: {error_msg}"
                    )
                    if attempt == max_retries - 1:
                        logger.error(
                            f"Failed to generate valid schema with ground truth after {max_retries} attempts"
                        )
                        return None

            except json.JSONDecodeError as e:
                validation_feedback = f"Invalid JSON format: {str(e)}"
                logger.warning(
                    f"JSON parse error with ground truth on attempt {attempt + 1}: {e}"
                )
                if attempt == max_retries - 1:
                    logger.error(
                        f"Failed to generate valid JSON with ground truth after {max_retries} attempts"
                    )
                    return None
            except Exception as e:
                logger.error(
                    f"Error extracting data with ground truth on attempt {attempt + 1}: {e}"
                )
                if attempt == max_retries - 1:
                    return None

        return None

    def _prompt_classes_discovery_with_ground_truth(self, ground_truth_data):
        ground_truth_json = json.dumps(ground_truth_data, indent=2)
        sample_output_format = self._sample_output_format()
        return f"""
                        This image contains unstructured data. Analyze the data line by line using the provided ground truth as reference.
                        <GROUND_TRUTH_REFERENCE>
                        {ground_truth_json}
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
                        - Each field should have appropriate "type" based on ground truth values
                        - Add "description" for each field with extraction instructions and location hints
                        
                        Nesting Groups:
                        - Do not nest the groups i.e. groups within groups.
                        - All groups should be directly associated under main "properties".
                        

                        Match field names, data types, and structure from the ground truth reference.
                        Image may contain multiple pages, process all pages.
                        Do not extract the actual values, only the schema structure.

                        Return the extracted schema in the exact JSON Schema format below:
                        {sample_output_format}
                        """

    def _prompt_classes_discovery(self):
        sample_output_format = self._sample_output_format()
        return f"""
                        This image contains forms data. Analyze the form line by line.
                        Image may contains multiple pages, process all the pages.
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
                        - Field names should be less than 30 characters, use camelCase or snake_case, name should not start with number and name should not have special characters.
                        - Field descriptions should include location hints (box number, line number, section)

                        Nesting Groups:
                        - Do not nest the groups i.e. groups within groups.
                        - All groups should be directly associated under main "properties".
                        
                        Do not extract the actual values, only the schema structure.
                        Return the extracted schema in the exact JSON Schema format below:
                        {sample_output_format}
                    """

    def _sample_output_format(self):
        return """
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id" : "Form-1040",
            "x-aws-idp-document-type" : "Form-1040",
            "type": "object",
            "description" : "Brief summary of the document",
            "properties" : {
                "PersonalInformation": {
                    "type": "object",
                    "description" : "Personal information of Tax payer",
                    "properties" : {
                        "FirstName": {
                            "type": "string",
                            "description" : "First Name of Taxpayer"
                        },
                        "Age": {
                            "type": "number",
                            "description" : "Age of Taxpayer"
                        }
                    }
                },
                "Dependents": {
                    "type": "array",
                    "description" : "Dependents of taxpayer",
                    "items": {
                        "type": "object",
                        "properties" : {
                            "FirstName": {
                                "type": "string",
                                "description" : "Dependent first name"
                            },
                            "Age": {
                                "type": "number",
                                "description" : "Dependent Age"
                            }
                        }
                    }
                }
            }
        }
        """
