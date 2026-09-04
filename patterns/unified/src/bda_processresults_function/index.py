# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import datetime
import io
import json
import logging
import os
from decimal import Decimal

import boto3
import pypdfium2 as pdfium
from botocore.exceptions import ClientError
from idp_common import metrics
from idp_common.config import get_config
from idp_common.docs_service import create_document_service
from idp_common.models import Document, HitlMetadata, Page, Section, Status
from idp_common.s3 import get_s3_client, write_content
from idp_common.utils import build_s3_uri, parse_confidence, parse_s3_uri

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger("idp_common.bedrock.client").setLevel(
    os.environ.get("BEDROCK_LOG_LEVEL", "INFO")
)
# Get LOG_LEVEL from environment variable with INFO as default

# Use the common S3 client
s3_client = get_s3_client()
ssm_client = boto3.client("ssm")
bedrock_client = boto3.client("bedrock-data-automation")


def is_hitl_enabled(config_version=None, config_revision=None):
    """Check if HITL is enabled from configuration."""
    try:
        config = get_config(as_model=True, version=config_version)
        hitl_enabled = config.hitl.enabled
        logger.info(f"HITL enabled from config: {hitl_enabled}")
        return hitl_enabled
    except Exception as e:
        logger.warning(f"Failed to get HITL config: {e}", exc_info=True)
        return False  # Default to disabled if config unavailable


def is_rule_validation_enabled(config_version=None, config_revision=None):
    """Check if rule validation is enabled and has rules configured."""
    try:
        config = get_config(as_model=True, version=config_version)
        if hasattr(config, 'rule_validation') and config.rule_validation:
            enabled = config.rule_validation.enabled
            if enabled:
                policy_classes = getattr(config, 'policy_classes', None) or []
                if len(policy_classes) == 0:
                    logger.info("Rule validation enabled but no policy_classes configured - skipping")
                    return False
            logger.info(f"Rule validation enabled: {enabled}")
            return enabled
        return False
    except Exception as e:
        logger.warning(f"Failed to get rule validation config: {e}", exc_info=True)
        return False


def create_metadata_file(file_uri, class_type, file_type=None):
    """
    Creates a metadata file alongside the given URI file with the same name plus '.metadata.json'

    Args:
        file_uri (str): The S3 URI of the file
        class_type (str): The class type to include in the metadata
        file_type (str, optional): Type of file ('section' or 'page')
    """
    try:
        # Parse the S3 URI to get bucket and key. Use parse_s3_uri (plain
        # string split) rather than urllib.parse.urlparse: object keys may
        # contain '#', which urlparse treats as a URL fragment delimiter and
        # silently truncates, producing a wrong metadata key.
        bucket, key = parse_s3_uri(file_uri)

        # Create the metadata key by adding '.metadata.json' to the original key
        metadata_key = f"{key}.metadata.json"

        # Determine the file type if not provided
        if file_type is None:
            if key.endswith(".json"):
                file_type = "section"
            else:
                file_type = "page"

        # Create metadata content
        metadata_content = {
            "metadataAttributes": {
                "DateTime": datetime.datetime.now().isoformat(),
                "Class": class_type,
                "FileType": file_type,
            }
        }

        # Use the common library to write to S3
        write_content(
            metadata_content, bucket, metadata_key, content_type="application/json"
        )

        logger.info(f"Created metadata file at s3://{bucket}/{metadata_key}")
    except Exception as e:
        logger.error(f"Error creating metadata file for {file_uri}: {str(e)}")


def copy_s3_objects(bda_result_bucket, bda_result_prefix, output_bucket, object_key):
    """
    Copy objects from a source S3 location to a destination S3 location.
    """
    copied_files = 0
    try:
        # List all objects in source location using pagination
        paginator = s3_client.get_paginator("list_objects_v2")
        page_iterator = paginator.paginate(
            Bucket=bda_result_bucket, Prefix=bda_result_prefix
        )

        # Process each object in the pages
        for page in page_iterator:
            if not page.get("Contents"):
                continue

            for obj in page["Contents"]:
                bda_result_key = obj["Key"]
                relative_path = bda_result_key[len(bda_result_prefix) :].lstrip("/")
                dest_key = f"{object_key}/{relative_path}"
                s3_client.copy_object(
                    CopySource={"Bucket": bda_result_bucket, "Key": bda_result_key},
                    Bucket=output_bucket,
                    Key=dest_key,
                    ContentType="application/json",
                    MetadataDirective="REPLACE",
                )
                copied_files += 1

        logger.info(f"Successfully copied {copied_files} files")
        return copied_files

    except Exception as e:
        logger.error(f"Error copying files: {str(e)}")
        raise


def create_pdf_page_images(bda_result_bucket, output_bucket, object_key):
    """
    Create images for each page of a PDF document and upload them to S3.
    """
    try:
        # Download the PDF from S3
        pdf_content = s3_client.get_object(Bucket=bda_result_bucket, Key=object_key)[
            "Body"
        ].read()

        # Open the PDF using pypdfium2
        pdf_document = pdfium.PdfDocument(pdf_content)
        # Initialize form rendering engine so fillable PDF form fields
        # (text inputs, checkboxes, etc.) appear in rendered page images.
        # Without this, may_draw_forms=True in render() has no effect.
        pdf_document.init_forms()

        # Process each page
        for page_num in range(len(pdf_document)):
            # Render page to a PIL image
            page = pdf_document[page_num]
            # Flatten form fields into page content before rendering.
            # Many fillable PDFs (e.g., government forms) lack appearance
            # streams for form fields — flatten() forces PDFium to generate
            # them and merge into page content so render() can display them.
            # Only applies when PDF has form fields (formenv is set by init_forms).
            if page.formenv is not None:
                page.flatten()
            pil_img = page.render(scale=150 / 72).to_pil()

            # Save the image to a BytesIO object as JPEG
            img_buffer = io.BytesIO()
            pil_img.save(img_buffer, format="JPEG", quality=95)
            img_bytes = img_buffer.getvalue()

            # Upload the image to S3 using the common library
            # Use 1-based page numbering for consistency with pattern-2 and ground truth
            page_id_1based = page_num + 1
            image_key = f"{object_key}/pages/{page_id_1based}/image.jpg"
            s3_client.upload_fileobj(
                io.BytesIO(img_bytes),
                output_bucket,
                image_key,
                ExtraArgs={"ContentType": "image/jpeg"},
            )

        logger.info(
            f"Successfully created and uploaded {len(pdf_document)} images to S3"
        )
        return len(pdf_document)

    except Exception as e:
        logger.error(f"Error creating page images: {str(e)}")
        raise


def process_bda_sections(
    bda_result_bucket,
    bda_result_prefix,
    output_bucket,
    object_key,
    document,
    confidence_threshold=0.8,
    config=None,
):
    """
    Process BDA sections and build sections for the Document object

    Args:
        bda_result_bucket (str): The BDA result bucket
        bda_result_prefix (str): The BDA result prefix
        output_bucket (str): The output bucket
        object_key (str): The object key
        document (Document): The document object to update
        confidence_threshold (float): Confidence threshold to add to explainability data
        config: IDPConfig model instance for schema-aware threshold resolution

    Returns:
        Document: The updated document
    """
    # Source path for BDA custom output files
    bda_custom_output_prefix = f"{bda_result_prefix}/custom_output/"
    # Target path for section files
    sections_output_prefix = f"{object_key}/sections/"

    try:
        # List all section folders in the BDA result bucket
        response = s3_client.list_objects_v2(
            Bucket=bda_result_bucket, Prefix=bda_custom_output_prefix, Delimiter="/"
        )

        # Process each section folder
        for prefix in response.get("CommonPrefixes", []):
            section_path = prefix.get("Prefix")
            if not section_path:
                continue

            # Extract section ID from path (BDA uses 0-based, convert to 1-based)
            raw_section_id = section_path.rstrip("/").split("/")[-1]
            section_id = str(int(raw_section_id) + 1)  # Convert to 1-based
            target_section_path = f"{sections_output_prefix}{section_id}/"

            # List all files in the section folder
            section_files = s3_client.list_objects_v2(
                Bucket=bda_result_bucket, Prefix=section_path
            )

            # Copy each file to the output bucket
            for file_obj in section_files.get("Contents", []):
                src_key = file_obj["Key"]
                file_name = src_key.split("/")[-1]
                target_key = f"{target_section_path}{file_name}"

                # Special handling for result.json files to add confidence thresholds
                if file_name == "result.json":
                    try:
                        # Download the result.json file
                        result_obj = s3_client.get_object(
                            Bucket=bda_result_bucket, Key=src_key
                        )
                        result_data = json.loads(
                            result_obj["Body"].read().decode("utf-8")
                        )

                        # Note: page_indices remain 0-based in result.json (consistent with BDA and pattern-2)
                        # Only page_ids and S3 paths use 1-based numbering

                        # Add confidence thresholds to explainability_info if present
                        if "explainability_info" in result_data:
                            result_data["explainability_info"] = (
                                add_confidence_thresholds_to_explainability_schema_aware(
                                    result_data["explainability_info"],
                                    result_data,
                                    confidence_threshold,
                                    config,
                                )
                            )
                            logger.info(
                                f"Added schema-aware confidence thresholds to explainability_info in section {section_id}"
                            )

                        # Write the modified result.json to the target location
                        write_content(
                            result_data,
                            output_bucket,
                            target_key,
                            content_type="application/json",
                        )
                        logger.info(f"Processed and copied {src_key} to {target_key}")

                    except Exception as e:
                        logger.error(
                            f"Error processing result.json {src_key}: {str(e)}"
                        )
                        # Fallback to regular copy if processing fails
                        s3_client.copy_object(
                            CopySource={"Bucket": bda_result_bucket, "Key": src_key},
                            Bucket=output_bucket,
                            Key=target_key,
                            ContentType="application/json",
                            MetadataDirective="REPLACE",
                        )
                        logger.info(f"Fallback copied {src_key} to {target_key}")
                else:
                    # Regular copy for non-result.json files
                    s3_client.copy_object(
                        CopySource={"Bucket": bda_result_bucket, "Key": src_key},
                        Bucket=output_bucket,
                        Key=target_key,
                        ContentType="application/json"
                        if file_name.endswith(".json")
                        else "application/octet-stream",
                        MetadataDirective="REPLACE",
                    )
                    logger.info(f"Copied {src_key} to {target_key}")

            # Get the result.json file
            result_path = f"{target_section_path}result.json"
            try:
                result_obj = s3_client.get_object(Bucket=output_bucket, Key=result_path)
                result_data = json.loads(result_obj["Body"].read().decode("utf-8"))

                # Extract required fields
                doc_class = result_data.get("document_class", {}).get("type", "")
                page_indices = result_data.get("split_document", {}).get(
                    "page_indices", []
                )
                # page_indices are 0-based, convert to 1-based page_ids
                page_ids = [str(idx + 1) for idx in (page_indices or [])]

                # Create the OutputJSONUri using the utility function
                extraction_result_uri = build_s3_uri(output_bucket, result_path)

                # Create Section object and add to document.
                #
                # Confidence is left unscored here and filled in later from BDA's
                # matched-blueprint confidence, which is the real signal for the
                # class this section was given (see the segment loop in
                # update_hitl_status_in_dynamodb). A section whose blueprint did
                # not MATCH therefore stays unscored rather than claiming the 1.0
                # this used to hardcode.
                section = Section(
                    section_id=section_id,
                    classification=doc_class,
                    confidence=None,
                    page_ids=page_ids,
                    extraction_result_uri=extraction_result_uri,
                )
                document.sections.append(section)

                # Create metadata file for the extraction result URI
                create_metadata_file(extraction_result_uri, doc_class, "section")

            except ClientError as e:
                logger.error(
                    f"Failed to retrieve result.json for section {section_id}: {e}"
                )
                continue
            except json.JSONDecodeError as e:
                logger.error(
                    f"Invalid JSON in result.json for section {section_id}: {e}"
                )
                continue

        logger.info(
            f"Processed {len(document.sections)} sections for document {object_key}"
        )
        return document

    except ClientError as e:
        logger.error(f"Failed to list sections in S3: {e}")
        document.errors.append(f"Failed to list sections: {str(e)}")
        return document


def extract_markdown_from_json(raw_json):
    """
    Extract markdown content from BDA result JSON

    Args:
        raw_json (dict): The BDA result JSON

    Returns:
        str: Concatenated markdown text
    """
    markdown_texts = []

    # Extract from pages
    if "pages" in raw_json:
        for page in raw_json["pages"]:
            if "representation" in page and "markdown" in page["representation"]:
                markdown_texts.append(page["representation"]["markdown"])

    # Join with horizontal rule
    if markdown_texts:
        return "\n\n---\n\nPAGE BREAK\n\n---\n\n".join(markdown_texts)
    return ""


def add_confidence_thresholds_to_explainability(
    explainability_data, confidence_threshold
):
    """
    Add confidence thresholds to explainability data recursively.

    Args:
        explainability_data: The explainability data (dict, list, or other)
        confidence_threshold: The confidence threshold to add

    Returns:
        The modified explainability data with confidence thresholds added
    """
    if isinstance(explainability_data, dict):
        # Create a copy to avoid modifying the original
        result = explainability_data.copy()

        # If this dict has a confidence field, add the confidence_threshold
        if "confidence" in result and isinstance(result["confidence"], (int, float)):
            result["confidence_threshold"] = confidence_threshold

        # Recursively process nested dictionaries
        for key, value in result.items():
            result[key] = add_confidence_thresholds_to_explainability(
                value, confidence_threshold
            )

        return result
    elif isinstance(explainability_data, list):
        # Recursively process list items
        return [
            add_confidence_thresholds_to_explainability(item, confidence_threshold)
            for item in explainability_data
        ]
    else:
        # Return primitive values as-is
        return explainability_data


def resolve_class_schema(doc_class, config):
    """Look up the JSON Schema for a document class from the config.

    Args:
        doc_class (str): The document class name (e.g. "w2").
        config: IDPConfig model instance (``config.classes`` is a list of dicts).

    Returns:
        dict: The class schema, or None when not found.

    Thin wrapper over ``idp_common.assessment.threshold_resolver.find_class_schema``
    — the lookup itself (including the non-string ``x-aws-idp-document-type``
    guard) lives there and is unit-tested, so this path and the standalone
    assessment service cannot drift apart.
    """
    # Imported lazily: this Lambda installs idp_common[core,docs_service,image],
    # not [assessment], so a module-level import would risk a cold-start failure
    # if the assessment package ever grows a hard third-party dependency.
    from idp_common.assessment.threshold_resolver import find_class_schema

    if config is None or not hasattr(config, "classes"):
        return None
    return find_class_schema(doc_class, config.classes)


def add_confidence_thresholds_to_explainability_schema_aware(
    explainability_data, result_data, default_confidence_threshold, config
):
    """Add confidence thresholds to explainability data using per-field schema thresholds.

    Unlike ``add_confidence_thresholds_to_explainability`` which applies a flat
    threshold, this function resolves per-field ``x-aws-idp-confidence-threshold``
    values from the class schema — including resolving ``$ref`` → ``$defs`` for
    array item sub-fields.

    Falls back to the flat-threshold approach if the class schema cannot be
    resolved.

    Args:
        explainability_data: The explainability data (typically a list where [0]
            is the per-field assessment dict).
        result_data: The full result.json dict (contains ``document_class.type``).
        default_confidence_threshold: Fallback threshold (from hitl.confidence_threshold).
        config: The IDPConfig model instance.

    Returns:
        The modified explainability data with per-field confidence thresholds.
    """
    # Determine the document class from result_data
    doc_class = (result_data.get("document_class") or {}).get("type", "")
    class_schema = resolve_class_schema(doc_class, config)

    if not class_schema:
        logger.debug(
            f"No class schema found for '{doc_class}', using flat threshold {default_confidence_threshold}"
        )
        return add_confidence_thresholds_to_explainability(
            explainability_data, default_confidence_threshold
        )

    def _enrich(node):
        from idp_common.assessment.batching import enrich_assessment_with_thresholds

        enriched, _alerts = enrich_assessment_with_thresholds(
            node, class_schema, default_confidence_threshold
        )
        return enriched

    # explainability_info is typically a list of assessment dicts — enrich ALL of them
    if isinstance(explainability_data, list) and len(explainability_data) > 0:
        try:
            return [_enrich(item) if isinstance(item, dict) else item for item in explainability_data]
        except Exception as e:
            logger.warning(
                f"Schema-aware threshold enrichment failed, falling back to flat: {e}"
            )
            return add_confidence_thresholds_to_explainability(
                explainability_data, default_confidence_threshold
            )
    if isinstance(explainability_data, dict):
        # Direct dict (no wrapping list) — enrich directly
        try:
            return _enrich(explainability_data)
        except Exception as e:
            logger.warning(
                f"Schema-aware threshold enrichment failed, falling back to flat: {e}"
            )
    return add_confidence_thresholds_to_explainability(
        explainability_data, default_confidence_threshold
    )


def extract_page_from_multipage_json(raw_json, page_index, confidence_threshold=None):
    """
    Extract a single page from a multi-page result JSON

    Args:
        raw_json (dict): The BDA result JSON
        page_index (int): The page index to extract
        confidence_threshold (float, optional): Confidence threshold to add to explainability data

    Returns:
        dict: A new result JSON with only the specified page
    """
    # Create a copy of the JSON with just metadata
    single_page_json = {"metadata": raw_json.get("metadata", {})}

    # Update metadata to reflect single page (keep 0-based indexing for consistency)
    if "metadata" in single_page_json:
        single_page_json["metadata"]["start_page_index"] = page_index  # 0-based
        single_page_json["metadata"]["end_page_index"] = page_index    # 0-based
        single_page_json["metadata"]["number_of_pages"] = 1

    # Include document level info
    if "document" in raw_json:
        single_page_json["document"] = raw_json["document"]

    # Add the single page from the pages array
    single_page_json["pages"] = []
    if "pages" in raw_json:
        for page in raw_json["pages"]:
            if page.get("page_index") == page_index:
                single_page_json["pages"].append(page)
                break

    # Filter elements for only this page
    single_page_json["elements"] = []
    if "elements" in raw_json:
        for element in raw_json["elements"]:
            page_indices = element.get("page_indices", [])
            if page_index in page_indices:
                # Create a copy of the element with only this page
                element_copy = element.copy()
                element_copy["page_indices"] = [page_index]
                single_page_json["elements"].append(element_copy)

    # Include explainability_info if present and add confidence thresholds
    if "explainability_info" in raw_json:
        explainability_info = raw_json["explainability_info"]
        if confidence_threshold is not None:
            # Add confidence thresholds to the explainability data
            single_page_json["explainability_info"] = (
                add_confidence_thresholds_to_explainability(
                    explainability_info, confidence_threshold
                )
            )
            logger.info(
                f"Added confidence threshold {confidence_threshold} to explainability_info for page {page_index}"
            )
        else:
            single_page_json["explainability_info"] = explainability_info

    return single_page_json


def extract_markdown_from_single_page_json(raw_json):
    """
    Extract markdown content from a single page BDA result JSON

    Args:
        raw_json (dict): The BDA result JSON

    Returns:
        str: Markdown text for the page
    """
    if "pages" in raw_json and len(raw_json["pages"]) > 0:
        page = raw_json["pages"][0]
        if "representation" in page and "markdown" in page["representation"]:
            return page["representation"]["markdown"]
    return ""


def process_bda_pages(
    bda_result_bucket,
    bda_result_prefix,
    output_bucket,
    object_key,
    document,
    confidence_threshold=0.8,
):
    """
    Process BDA page outputs and build pages for the Document object

    Args:
        bda_result_bucket (str): The BDA result bucket
        bda_result_prefix (str): The BDA result prefix
        output_bucket (str): The output bucket
        object_key (str): The object key
        document (Document): The document object to update
        confidence_threshold (float): Confidence threshold to add to explainability data

    Returns:
        Document: The updated document
    """
    # Source path for standard output result.json files
    standard_output_prefix = f"{bda_result_prefix}/standard_output/"
    # Target path for page files
    pages_output_prefix = f"{object_key}/pages/"

    # Create a mapping of page_id to class from sections
    page_to_class_map = {}
    for section in document.sections:
        section_class = section.classification
        for page_id in section.page_ids:
            page_to_class_map[page_id] = section_class

    try:
        # List all objects in the standard output directory using pagination
        logger.info(f"Listing standard output objects at s3://{bda_result_bucket}/{standard_output_prefix}")
        paginator = s3_client.get_paginator("list_objects_v2")
        all_contents = []
        for page_response in paginator.paginate(Bucket=bda_result_bucket, Prefix=standard_output_prefix):
            all_contents.extend(page_response.get("Contents", []))
        
        logger.info(f"Found {len(all_contents)} objects in standard output directory")
        result_json_files = [obj["Key"] for obj in all_contents if obj["Key"].endswith("result.json")]
        logger.info(f"Found {len(result_json_files)} result.json files: {result_json_files}")

        # Process all standard_output result.json files which may contain multiple pages
        for obj in all_contents:
            obj_key = obj["Key"]

            # Only process result.json files
            if not obj_key.endswith("result.json"):
                continue

            try:
                # Get the raw JSON result from the BDA result bucket
                result_obj = s3_client.get_object(Bucket=bda_result_bucket, Key=obj_key)
                raw_json = json.loads(result_obj["Body"].read().decode("utf-8"))
                
                # Log top-level keys for debugging
                logger.info(f"Standard output result.json keys: {list(raw_json.keys())}")
                pages_in_json = raw_json.get("pages", [])
                logger.info(f"Standard output has {len(pages_in_json)} pages in 'pages' array")

                # Check if this contains pages (PAGE granularity format)
                if "pages" in raw_json and len(raw_json["pages"]) > 0:
                    # Process each page in the multi-page result (PAGE/ELEMENT granularity)
                    for page_data in raw_json["pages"]:
                        page_index = page_data.get("page_index")
                        if page_index is None:
                            logger.warning(f"Page in {obj_key} has no page_index, page keys: {list(page_data.keys())}")
                            continue

                        # Convert from 0-based (BDA) to 1-based (consistency with pattern-2)
                        page_id = str(page_index + 1)

                        # Extract a single page result.json for this page with confidence threshold
                        single_page_json = extract_page_from_multipage_json(
                            raw_json, page_index, confidence_threshold
                        )

                        # Determine page directory path in output bucket
                        page_path = f"{pages_output_prefix}{page_id}/"
                        page_result_path = f"{page_path}result.json"

                        # Write the single page result.json to the page directory
                        write_content(
                            single_page_json,
                            output_bucket,
                            page_result_path,
                            content_type="application/json",
                        )

                        # Create raw text URI
                        raw_text_uri = build_s3_uri(output_bucket, page_result_path)

                        # Define image path
                        image_path = f"{page_path}image.jpg"

                        # Check if image exists
                        try:
                            s3_client.head_object(Bucket=output_bucket, Key=image_path)
                            image_uri = build_s3_uri(output_bucket, image_path)
                        except ClientError:
                            image_uri = None
                            logger.warning(f"image.jpg not found for page {page_id}")

                        # Get the class from the section mapping
                        doc_class = page_to_class_map.get(page_id, "")

                        # Extract markdown content for this page
                        markdown_text = extract_markdown_from_single_page_json(
                            single_page_json
                        )

                        # Create parsedResult.json
                        parsed_result = {"text": markdown_text}

                        # Write parsedResult.json to S3
                        parsed_result_path = f"{page_path}parsedResult.json"
                        write_content(
                            parsed_result,
                            output_bucket,
                            parsed_result_path,
                            content_type="application/json",
                        )

                        # Create S3 URI for parsed result
                        parsed_result_uri = build_s3_uri(
                            output_bucket, parsed_result_path
                        )

                        logger.info(f"Created parsedResult.json for page {page_id}")

                        # Create metadata file for the parsed result URI
                        create_metadata_file(parsed_result_uri, doc_class, "page")

                        # Create Page object and add to document
                        page_obj = Page(
                            page_id=page_id,
                            image_uri=image_uri,
                            raw_text_uri=raw_text_uri,
                            parsed_text_uri=parsed_result_uri,
                            classification=doc_class,
                        )
                        document.pages[page_id] = page_obj

                    logger.info(f"Processed multi-page result file {obj_key}")

                elif "metadata" in raw_json:
                    # DOCUMENT granularity format: metadata has page index, text in companion result.txt
                    metadata = raw_json.get("metadata", {})
                    start_page = metadata.get("start_page_index", 0)
                    end_page = metadata.get("end_page_index", start_page)
                    logger.info(f"Processing DOCUMENT-granularity standard output {obj_key}: pages {start_page}-{end_page}")

                    # Try to read companion result.txt for the actual text content
                    result_txt_key = obj_key.replace("result.json", "result.txt")
                    page_text = ""
                    try:
                        txt_obj = s3_client.get_object(Bucket=bda_result_bucket, Key=result_txt_key)
                        page_text = txt_obj["Body"].read().decode("utf-8")
                        logger.info(f"Read {len(page_text)} chars from {result_txt_key}")
                    except ClientError:
                        # Fallback to document.representation.text if result.txt doesn't exist
                        doc_repr = raw_json.get("document", {}).get("representation", {})
                        page_text = doc_repr.get("text", "") or doc_repr.get("markdown", "")
                        logger.warning(f"result.txt not found for {obj_key}, using document.representation ({len(page_text)} chars)")

                    # Create pages for each page index in this segment
                    for page_index in range(start_page, end_page + 1):
                        page_id = str(page_index + 1)  # Convert to 1-based

                        page_path = f"{pages_output_prefix}{page_id}/"
                        page_result_path = f"{page_path}result.json"

                        # Write the result.json to the page directory
                        write_content(
                            raw_json,
                            output_bucket,
                            page_result_path,
                            content_type="application/json",
                        )

                        raw_text_uri = build_s3_uri(output_bucket, page_result_path)

                        # Define image path
                        image_path = f"{page_path}image.jpg"
                        try:
                            s3_client.head_object(Bucket=output_bucket, Key=image_path)
                            image_uri = build_s3_uri(output_bucket, image_path)
                        except ClientError:
                            image_uri = None
                            logger.warning(f"image.jpg not found for page {page_id}")

                        doc_class = page_to_class_map.get(page_id, "")

                        # Create parsedResult.json with the text from result.txt
                        parsed_result = {"text": page_text}
                        parsed_result_path = f"{page_path}parsedResult.json"
                        write_content(
                            parsed_result,
                            output_bucket,
                            parsed_result_path,
                            content_type="application/json",
                        )
                        parsed_result_uri = build_s3_uri(output_bucket, parsed_result_path)

                        logger.info(f"Created parsedResult.json for page {page_id} (DOCUMENT granularity, {len(page_text)} chars)")

                        create_metadata_file(parsed_result_uri, doc_class, "page")

                        page_obj = Page(
                            page_id=page_id,
                            image_uri=image_uri,
                            raw_text_uri=raw_text_uri,
                            parsed_text_uri=parsed_result_uri,
                            classification=doc_class,
                        )
                        document.pages[page_id] = page_obj

                    logger.info(f"Processed DOCUMENT-granularity result file {obj_key} (pages {start_page}-{end_page})")
                else:
                    logger.warning(f"Standard output result.json at {obj_key} has unrecognized format. Keys: {list(raw_json.keys())}")

            except Exception as e:
                logger.error(f"Error processing result file {obj_key}: {str(e)}", exc_info=True)
                document.errors.append(
                    f"Error processing result file {obj_key}: {str(e)}"
                )

        # Fallback: if no pages were created from standard output, create pages from sections
        # This ensures summarization always has pages to work with
        if len(document.pages) == 0 and len(document.sections) > 0:
            logger.warning(
                f"No pages created from standard output. Creating fallback pages from "
                f"{len(document.sections)} sections with page_ids."
            )
            # Collect all unique page_ids from sections
            all_page_ids = set()
            for section in document.sections:
                for page_id in section.page_ids:
                    all_page_ids.add(page_id)
            
            for page_id in sorted(all_page_ids, key=lambda x: int(x)):
                page_path = f"{pages_output_prefix}{page_id}/"
                image_path = f"{page_path}image.jpg"
                
                # Check if image exists (created by create_pdf_page_images earlier)
                try:
                    s3_client.head_object(Bucket=output_bucket, Key=image_path)
                    image_uri = build_s3_uri(output_bucket, image_path)
                except ClientError:
                    image_uri = None
                    logger.warning(f"Fallback: image.jpg not found for page {page_id}")
                
                doc_class = page_to_class_map.get(page_id, "")
                
                # Create a minimal parsedResult.json with empty text
                # The summarization step will still work but with limited text
                parsed_result = {"text": f"[Page {page_id}]"}
                parsed_result_path = f"{page_path}parsedResult.json"
                write_content(
                    parsed_result,
                    output_bucket,
                    parsed_result_path,
                    content_type="application/json",
                )
                parsed_result_uri = build_s3_uri(output_bucket, parsed_result_path)
                
                page_obj = Page(
                    page_id=page_id,
                    image_uri=image_uri,
                    parsed_text_uri=parsed_result_uri,
                    classification=doc_class,
                )
                document.pages[page_id] = page_obj
                logger.info(f"Created fallback page {page_id} with image_uri={image_uri}")
            
            logger.info(f"Created {len(document.pages)} fallback pages from section page_ids")

        # Update document page count
        document.num_pages = len(document.pages)
        logger.info(f"Processed {document.num_pages} pages for document {object_key}")
        return document

    except ClientError as e:
        logger.error(f"Failed to list pages in S3: {e}")
        document.errors.append(f"Failed to list pages: {str(e)}")
        return document


def parse_s3_path(s3_uri: str) -> (str, str):
    """Extract bucket and key from s3:// URI.

    Delegates to idp_common.utils.parse_s3_uri, which splits on '/' instead of
    urlparse so keys containing '#' are not truncated at the fragment marker.
    """
    return parse_s3_uri(s3_uri)


def download_json(bucket: str, key: str) -> dict:
    """Download and parse JSON from S3."""
    response = s3_client.get_object(Bucket=bucket, Key=key)
    return json.loads(response["Body"].read())


def download_decimal(bucket: str, key: str) -> dict:
    """Download and parse JSON from S3, converting floats to Decimal."""
    response = s3_client.get_object(Bucket=bucket, Key=key)
    return json.loads(response["Body"].read(), parse_float=Decimal)


def process_keyvalue_details(
    explainability_data: list,
    page_indices: list,
    confidence_threshold: float = 0.8,
    class_schema: dict = None,
) -> dict:
    """
    Process explainability data to extract key-value and bounding box details per page.

    Args:
        explainability_data: List of explainability data from BDA
        page_indices: List of page indices
        confidence_threshold: Default confidence threshold, used when the field has
            no explicit ``x-aws-idp-confidence-threshold`` in the class schema
        class_schema: Optional document-class JSON Schema. When provided, each
            field's threshold is resolved from the schema by path (including
            ``$ref``/``$defs`` array item definitions) instead of applying the
            flat default to every field.
    """
    results = {
        "key_value_details": {str(p): [] for p in page_indices},
        "bounding_box_details": {str(p): [] for p in page_indices},
    }
    last_page = str(page_indices[-1]) if page_indices else "0"

    def get_page(raw_page: int) -> str:
        """Get page number (geometry.page is already 1-based, and we now use 1-based page_indices)."""
        if raw_page is None:
            return last_page
        # No adjustment needed - geometry.page is already 1-based and page_indices are now 1-based
        return str(raw_page) if raw_page in page_indices else last_page

    def resolve_threshold(key_path: list) -> float:
        """Per-field threshold from the schema, falling back to the flat default."""
        if not class_schema:
            return confidence_threshold
        try:
            from idp_common.assessment.threshold_resolver import (
                resolve_threshold_for_path,
            )

            return resolve_threshold_for_path(
                key_path, class_schema, confidence_threshold
            )
        except Exception as e:  # noqa: BLE001 - never fail the pipeline on this
            logger.warning(
                f"Threshold path resolution failed for {key_path}, "
                f"using default {confidence_threshold}: {e}"
            )
            return confidence_threshold

    def process_entry(key_path: list, entry: dict, page: int):
        target_page = get_page(page)
        kv_entry = {
            "key": format_key_path(key_path),
            "value": entry.get("value", ""),
            "confidence": entry.get("confidence", 0.0),
            "confidence_threshold": resolve_threshold(key_path),
        }
        bbox = {}
        if entry.get("geometry"):
            for geom in entry["geometry"]:
                if "boundingBox" in geom:
                    bbox = {
                        k: geom["boundingBox"].get(k, 0)
                        for k in ["top", "left", "width", "height"]
                    }
                    break
        results["key_value_details"][target_page].append(kv_entry)
        results["bounding_box_details"][target_page].append(
            {"key": format_key_path(key_path), "bounding_box": bbox}
        )

    def format_key_path(path_parts: list) -> str:
        """Convert path array to flattened key notation."""
        formatted = []
        for part in path_parts:
            if isinstance(part, int) or (
                isinstance(part, str) and part.startswith("_")
            ):
                formatted[-1] += f"[{part[1:]}]"
            else:
                formatted.append(str(part))
        return ".".join(formatted)

    def traverse(data: dict, path: list = None, current_page: int = None):
        path = path or []
        if isinstance(data, dict):
            page = current_page
            if "geometry" in data and data["geometry"]:
                page = data["geometry"][0].get("page")
            if "value" in data:
                process_entry(path, data, page)
            else:
                for k, v in data.items():
                    traverse(v, path + [k], page)
        elif isinstance(data, list):
            for i, item in enumerate(data):
                traverse(item, path + [f"_{i}"], current_page)

    for entry in explainability_data:
        traverse(entry)
    return results


def create_confidence_threshold_alerts(
    pagespecific_details: dict, confidence_threshold: float
) -> list:
    """
    Create confidence threshold alerts from page-specific key-value details.

    Each key-value entry carries its OWN ``confidence_threshold`` (resolved
    per-field from the class schema by ``process_keyvalue_details``), so the
    comparison uses that value. ``confidence_threshold`` here is only the
    fallback for entries that somehow lack one.

    Args:
        pagespecific_details: Dictionary containing key-value details per page
        confidence_threshold: Fallback threshold when an entry has none

    Returns:
        List of confidence threshold alert dictionaries matching AppSync service expectations
    """
    alerts = []

    # Process key-value details from all pages
    for page_num, kv_details in pagespecific_details.get(
        "key_value_details", {}
    ).items():
        for kv_entry in kv_details:
            confidence = kv_entry.get("confidence", 0.0)
            # Use the entry's per-field threshold (schema-resolved) when present
            entry_threshold = kv_entry.get(
                "confidence_threshold", confidence_threshold
            )
            if entry_threshold is None:
                entry_threshold = confidence_threshold
            if confidence < entry_threshold:
                alert = {
                    "attribute_name": kv_entry.get("key", ""),
                    "confidence": confidence,
                    "confidence_threshold": entry_threshold,
                }
                alerts.append(alert)

    logger.info(f"Created {len(alerts)} confidence threshold alerts")
    return alerts


def process_segments(
    input_bucket: str,
    output_bucket: str,
    object_key: str,
    segment_metadata: list,
    confidence_threshold: float,
    execution_id: str,
    document,
    config_version: str = None,
    config=None,
    config_revision: int = None,
):
    """
    Process each segment, extract key-value details, and invoke human review if needed.
    
    Args:
        confidence_threshold: Default threshold, used when a field has no explicit
            ``x-aws-idp-confidence-threshold`` in its class schema
        config: IDPConfig model instance, used to resolve per-field thresholds
            from the segment's document-class schema
    """
    dynamodb = boto3.resource("dynamodb")
    table_name = os.environ.get("DB_NAME", "")

    if table_name:
        table = dynamodb.Table(table_name)
    else:
        logger.warning(
            "DB_NAME environment variable not set, skipping DynamoDB operations"
        )

    now = datetime.datetime.now().isoformat()
    hitl_triggered = False
    overall_hitl_triggered = False

    for record_number, segment in enumerate(segment_metadata, start=1):
        logger.info(f"Processing segment for execution id: {execution_id}")
        item = {
            "execution_id": execution_id,
            "record_number": record_number,
            "input_bucket": input_bucket,
            "output_bucket": output_bucket,
            "object_key": object_key,
            "timestamp": now,
            "bp_match": segment.get("custom_output_status"),
            "hitl_bp_change": None,
        }

        if segment.get("custom_output_status") == "MATCH":
            custom_bucket, custom_key = parse_s3_path(segment["custom_output_path"])
            custom_output = download_json(custom_bucket, custom_key)
            custom_decimal_output = download_decimal(custom_bucket, custom_key)
            explainability_data = custom_output.get("explainability_info", [])
            page_indices = custom_output.get("split_document", {}).get(
                "page_indices", []
            )
            # Convert page_indices from 0-based (BDA) to 1-based for consistency
            page_indices_1based = [idx + 1 for idx in page_indices]
            # Resolve this segment's class schema so per-field thresholds
            # (including $ref/$defs array items) are honored instead of a flat one
            segment_doc_class = (custom_output.get("document_class") or {}).get(
                "type", ""
            )
            segment_class_schema = resolve_class_schema(segment_doc_class, config)
            pagespecific_details = process_keyvalue_details(
                explainability_data,
                page_indices_1based,
                confidence_threshold,
                segment_class_schema,
            )

            # Create confidence threshold alerts for UI display
            confidence_threshold_alerts = create_confidence_threshold_alerts(
                pagespecific_details, confidence_threshold
            )

            blueprint_name = custom_output["matched_blueprint"]["name"]
            bp_confidence = custom_output["matched_blueprint"]["confidence"]

            # Update the corresponding document section with confidence alerts
            # Find the section that contains these page indices (now 1-based)
            page_ids_str = [str(idx) for idx in page_indices_1based]
            for section in document.sections:
                # Check if this section's pages match the current segment's pages
                if set(section.page_ids) == set(page_ids_str):
                    section.confidence_threshold_alerts = confidence_threshold_alerts
                    # BDA's matched-blueprint confidence IS the classification
                    # confidence in this mode: the matched blueprint is what
                    # determines the section's class. It was already read below
                    # for the HITL decision but never stored, so the section
                    # carried a hardcoded 1.0 (GitHub #673). Note this makes BDA
                    # mode scored by default, with no extra inference and no
                    # prompt change — unlike the pipeline mode, where the model
                    # has to be asked.
                    section.confidence = parse_confidence(
                        bp_confidence, context=f"BDA blueprint {blueprint_name}"
                    )
                    logger.info(
                        f"Updated section {section.section_id} with {len(confidence_threshold_alerts)} confidence alerts, "
                        f"class confidence {section.confidence}"
                    )
                    break

            # Check if any key-value or blueprint confidence is below threshold
            # Use confidence_threshold_alerts to determine if HITL should be triggered
            if is_hitl_enabled(config_version, config_revision):
                low_confidence = (
                    len(confidence_threshold_alerts) > 0
                    or float(bp_confidence) < confidence_threshold
                )
                logger.info(
                    f"HITL enabled - confidence_threshold_alerts: {len(confidence_threshold_alerts)}, "
                    f"bp_confidence: {bp_confidence}, threshold: {confidence_threshold}, "
                    f"low_confidence: {low_confidence}"
                )
            else:
                low_confidence = None
                logger.info("HITL disabled in configuration")

            logger.info(f"low_confidence: {low_confidence}")

            item.update(
                {
                    "page_array": page_indices,
                    "hitl_triggered": low_confidence,
                    "extraction_bp_name": blueprint_name,
                    "extracted_result": custom_decimal_output,
                }
            )

            hitl_metadata = HitlMetadata(
                execution_id=execution_id,
                record_number=record_number,
                bp_match=segment.get("custom_output_status"),
                extraction_bp_name=blueprint_name,
                hitl_triggered=low_confidence,
                page_array=page_indices,
            )

            if low_confidence:
                hitl_triggered = low_confidence
                metrics.put_metric("HITLTriggered", 1)
                overall_hitl_triggered = True
                # HITL review will be handled via portal, not A2I
                item.update({"hitl_corrected_result": custom_decimal_output})
        else:
            if is_hitl_enabled(config_version, config_revision):
                std_hitl = "true"
                # std_hitl = None # HITL for standard output blueprint match is disabled until we have option to choose Blueprint in A2I
            else:
                std_hitl = None
            # Process standard output if no custom output match
            std_bucket, std_key = parse_s3_path(segment["standard_output_path"])
            std_output = download_decimal(std_bucket, std_key)
            metadata = std_output.get("metadata", {})
            start_page = metadata.get("start_page_index", 0)
            end_page = metadata.get("end_page_index", 0)
            # page_array stays 0-based (for internal use), page_ids are 1-based for display/S3 paths
            page_array = list(range(start_page, end_page + 1))
            item.update(
                {
                    "page_array": page_array,
                    # "hitl_triggered": std_hitl,
                    "hitl_triggered": None,
                    "extraction_bp_name": "None",
                    "extracted_result": std_output,
                }
            )

            hitl_metadata = HitlMetadata(
                execution_id=execution_id,
                record_number=record_number,
                bp_match=segment.get("custom_output_status"),
                extraction_bp_name="None",
                hitl_triggered=None,
                page_array=page_array,
            )

            hitl_triggered = None
            # if enable_hitl == 'true':
            # # if std_hitl: # HITL for standard output blueprint match is disabled until we have option to choose Blueprint in A2I
            #     for page_number in range(start_page, end_page + 1):
            #         ImageUri = f"s3://{output_bucket}/{object_key}/pages/{page_number}/image.jpg"
            #         try:
            #             human_loop_response = start_human_loop(
            #                 execution_id=execution_id,
            #                 kv_pairs=[],
            #                 source_image_uri=ImageUri,
            #                 bounding_boxes=[],
            #                 blueprintName="",
            #                 bp_confidence=0.00,
            #                 confidenceThreshold=confidence_threshold,
            #                 page_id=page_number,
            #                 page_indices=page_array,
            #                 record_number=record_number
            #             )
            #             logger.info(f"Triggered human loop for page {page_number}: {human_loop_response}")
            #         except Exception as e:
            #             logger.error(f"Failed to start human loop for page {page_number}: {str(e)}")

        document.hitl_metadata.append(hitl_metadata)

        if table_name:
            logger.info(f"Saving to DynamoDB: {json.dumps(item, default=str)}")
            try:
                table.put_item(Item=item)
            except Exception as e:
                logger.error(f"Error saving to DynamoDB: {str(e)}")

    return document, overall_hitl_triggered


def handle_skip_bda(event):
    """
    Handle the skip_bda scenario where document already has pages/sections data.
    This is used for reprocessing documents to re-run summarization and evaluation
    without re-invoking BDA.

    Args:
        event: Event containing document data

    Returns:
        Dict containing the processed document ready for summarization/evaluation
    """
    logger.info("Handling skip_bda scenario - using existing document data")
    
    # Load the document from the event
    working_bucket = os.environ.get("WORKING_BUCKET")
    document = Document.load_document(event.get("document"), working_bucket, logger)
    
    # Load configuration - use document's version if specified, otherwise use active version
    config_version = getattr(document, 'config_version', None)
    config_revision = getattr(document, 'config_revision', None)
    config = get_config(as_model=True, version=config_version, revision=config_revision)
    
    # Update document status to POSTPROCESSING
    document.status = Status.POSTPROCESSING
    document.workflow_execution_arn = event.get("execution_arn")
    
    # Update document in AppSync
    document_service = create_document_service()
    
    # Fetch current HITL status from DynamoDB (may have been updated by reviewer)
    current_doc = document_service.get_document(document.input_key)
    if current_doc:
        document.hitl_status = current_doc.hitl_status
        logger.info(f"Current HITL status from DynamoDB: {document.hitl_status}")
    
    logger.info(f"Updating document status to {document.status} for skip_bda scenario")
    document_service.update_document(document)
    
    # Get confidence threshold from configuration for potential HITL checks
    confidence_threshold = config.hitl.confidence_threshold
    logger.info(f"Using confidence threshold: {confidence_threshold}")
    
    # Check if HITL should be triggered based on existing confidence alerts
    hitl_triggered = False
    if is_hitl_enabled(config_version, config_revision):
        # Check each section for confidence alerts below threshold
        for section in document.sections:
            alerts = section.confidence_threshold_alerts or []
            if len(alerts) > 0:
                logger.info(f"Section {section.section_id} has {len(alerts)} confidence alerts")
                hitl_triggered = True
                break
    
    logger.info(f"Skip BDA - hitl_triggered: {hitl_triggered}")
    
    # Add metering information for skip scenario
    document.metering = document.metering or {}
    document.metering["BDAProject/bda/documents-skip"] = {"documents": 1}
    
    # Prepare response using serialization method
    output_bucket = event.get("output_bucket") or os.environ.get("OUTPUT_BUCKET")
    if not working_bucket:
        logger.warning("WORKING_BUCKET not set, using output_bucket for compression")
        working_bucket = output_bucket
    
    # Check if rule validation is enabled for this config version
    rule_validation_enabled = is_rule_validation_enabled(config_version, config_revision)

    response = {
        "document": document.serialize_document(working_bucket, "processresults_skip", logger),
        "hitl_triggered": hitl_triggered,
        "rule_validation_enabled": rule_validation_enabled,
        "bda_response_count": 0,
        "skip_bda": True
    }
    
    logger.info(f"Skip BDA response: {json.dumps(response, default=str)}")
    return response


def handler(event, context):
    """
    Process the BDA results and build a Document object with pages and sections.
    Can handle both single BDA response and arrays of BDA responses from blueprint changes.
    Also handles skip_bda scenario for reprocessing with existing document data.

    Args:
        event: Event containing BDA response information (single or array) or skip_bda flag
        context: Lambda context

    Returns:
        Dict containing the processed document
    """
    logger.info(f"Processing event: {json.dumps(event)}")
    
    # Check if this is a skip_bda scenario (reprocessing with existing data)
    if event.get("skip_bda"):
        return handle_skip_bda(event)

    # Check if we have a single BDA response or an array of responses
    bda_responses = []

    if isinstance(event, list):
        # We have an array of BDA responses (from blueprint change)
        logger.info(f"Processing array of {len(event)} BDA responses")
        bda_responses = event
    else:
        # We have a single BDA response (from initial processing)
        logger.info("Processing single BDA response")
        bda_responses = [event]

    # Extract required information from the first response
    first_response = bda_responses[0]
    
    # Handle skipped BDA case (HITL reprocessing)
    if first_response.get("metadata", {}).get("skipped"):
        logger.info("BDA was skipped - document already has extraction data (HITL reprocessing)")
        working_bucket = first_response["metadata"]["working_bucket"]
        document = Document.load_document(first_response.get("document", {}), working_bucket, logger)
        
        # Check if HITL review is needed
        config_version = getattr(document, 'config_version', None)
        config_revision = getattr(document, 'config_revision', None)
        hitl_triggered = is_hitl_enabled(config_version, config_revision) and any(
            section.confidence_threshold_alerts for section in document.sections
        )
        
        return {
            "document": document.serialize_document(working_bucket, "processresults_skip", logger),
            "hitl_triggered": hitl_triggered,
            "rule_validation_enabled": is_rule_validation_enabled(
                config_version, config_revision
            ),
        }
    
    output_bucket = first_response.get("output_bucket")

    # Handle different response formats
    if "BDAResponse" in first_response:
        # Standard initial processing format
        object_key = first_response["BDAResponse"]["job_detail"]["input_s3_object"][
            "name"
        ]
        input_bucket = first_response["BDAResponse"]["job_detail"]["input_s3_object"][
            "s3_bucket"
        ]
    elif "metadata" in first_response:
        # Blueprint change format
        object_key = first_response["metadata"]["object_key"]
        input_bucket = first_response["metadata"]["input_bucket"]
        output_bucket = first_response["metadata"]["working_bucket"]
    else:
        logger.error("Unknown response format")
        raise ValueError("Could not determine document information from response")

    logger.info(f"Input bucket: {input_bucket}, prefix: {object_key}")
    logger.info(f"Output bucket: {output_bucket}, base path: {object_key}")

    # Create a new Document object
    document = Document(
        id=object_key,
        input_bucket=input_bucket,
        input_key=object_key,
        output_bucket=output_bucket,
        status=Status.POSTPROCESSING,
        workflow_execution_arn=first_response.get("execution_arn"),
    )

    # Get existing document record to extract config version
    document_service = create_document_service()
    existing_document = document_service.get_document(object_key)
    input_config_version = getattr(existing_document, 'config_version', None) if existing_document else None
    # Reprocessing keeps the revision the document was originally pinned to, so a
    # re-run reproduces the original output rather than silently adopting whatever
    # the profile looks like now.
    input_config_revision = getattr(existing_document, 'config_revision', None) if existing_document else None

    # Set config version on the new document object
    document.config_version = input_config_version
    document.config_revision = input_config_revision

    # Load configuration using the input document's version
    config = get_config(
        as_model=True, version=input_config_version, revision=input_config_revision
    )

    # Get confidence threshold from configuration
    # Used for both creating confidence alerts and triggering HITL
    confidence_threshold = config.hitl.confidence_threshold
    logger.info(f"Using confidence threshold: {confidence_threshold}")

    # Update document status
    document_service = create_document_service()
    
    # Fetch current HITL status from DynamoDB (may have been updated by reviewer during reprocess)
    current_doc = document_service.get_document(document.input_key)
    if current_doc and current_doc.hitl_status:
        document.hitl_status = current_doc.hitl_status
        logger.info(f"Current HITL status from DynamoDB: {document.hitl_status}")
    
    logger.info(f"Updating document status to {document.status}")
    document_service.update_document(document)

    # Create page images (only need to do this once)
    # Note: Page image creation is non-critical (cosmetic for UI thumbnails).
    # Failure should not propagate as a document error since downstream steps
    # (e.g., evaluation) treat document.errors as indicators of processing failure.
    try:
        page_count = create_pdf_page_images(input_bucket, output_bucket, object_key)
        logger.info(f"Successfully created and uploaded {page_count} page images to S3")
    except Exception as e:
        logger.warning(f"Error creating page images (non-critical): {str(e)}")

    # Process each BDA response

    for response_idx, bda_response in enumerate(bda_responses):
        logger.info(
            f"Processing BDA response {response_idx + 1} of {len(bda_responses)}"
        )

        # Extract BDA result information
        bda_result_bucket = None
        bda_result_prefix = None

        if "BDAResponse" in bda_response:
            # Standard response format
            bda_result_bucket = bda_response["BDAResponse"]["job_detail"][
                "output_s3_location"
            ]["s3_bucket"]
            bda_result_prefix = bda_response["BDAResponse"]["job_detail"][
                "output_s3_location"
            ]["name"]
        elif "bda_response" in bda_response:
            # Blueprint change response format
            job_id = bda_response["bda_response"].get("jobId")
            if job_id:
                # Need to look up the job details to get output location
                bda_runtime_client = boto3.client("bedrock-data-automation-runtime")
                try:
                    job_details = bda_runtime_client.get_data_automation_job(
                        jobId=job_id
                    )
                    bda_result_bucket = job_details["job"]["outputS3Location"][
                        "s3Bucket"
                    ]
                    bda_result_prefix = job_details["job"]["outputS3Location"]["name"]
                except Exception as e:
                    logger.error(
                        f"Error getting job details for job {job_id}: {str(e)}"
                    )
                    continue

        if not bda_result_bucket or not bda_result_prefix:
            logger.error(
                f"Could not determine BDA result location for response {response_idx}"
            )
            continue

        logger.info(
            f"BDA Result bucket: {bda_result_bucket}, prefix: {bda_result_prefix}"
        )

        # Process sections and pages from BDA output
        document = process_bda_sections(
            bda_result_bucket,
            bda_result_prefix,
            output_bucket,
            object_key,
            document,
            confidence_threshold,
            config,
        )
        document = process_bda_pages(
            bda_result_bucket,
            bda_result_prefix,
            output_bucket,
            object_key,
            document,
            confidence_threshold,
        )

    # Calculate metrics
    page_ids_in_sections = set()
    for section in document.sections:
        for page_id in section.page_ids:
            page_ids_in_sections.add(page_id)

    custom_pages_count = len(page_ids_in_sections)
    total_pages = document.num_pages
    standard_pages_count = total_pages - custom_pages_count
    if standard_pages_count < 0:
        standard_pages_count = 0

    # Process HITL if enabled
    hitl_triggered = False

    try:
        # Use the confidence threshold already calculated above
        metdatafile_path = "/".join(bda_result_prefix.split("/")[:-1])
        job_metadata_key = f"{metdatafile_path}/job_metadata.json"
        execution_id = event.get("execution_arn", "").split(":")[-1]
        logger.info(f"HITL processing - bda_result_bucket: {bda_result_bucket}, job_metadata_key: {job_metadata_key}")
        logger.info(f"HITL execution ID: {execution_id}")

        try:
            jobmetadata_file = s3_client.get_object(
                Bucket=bda_result_bucket, Key=job_metadata_key
            )
            job_metadata = json.loads(jobmetadata_file["Body"].read())
            logger.info(f"job_metadata keys: {list(job_metadata.keys())}")
            if "output_metadata" in job_metadata:
                output_metadata = job_metadata["output_metadata"]
                logger.info(f"output_metadata type: {type(output_metadata)}, content preview: {str(output_metadata)[:500]}")
                if isinstance(output_metadata, list):
                    for asset in output_metadata:
                        document, hitl_result = process_segments(
                            input_bucket,
                            output_bucket,
                            object_key,
                            asset.get("segment_metadata", []),
                            confidence_threshold,
                            execution_id,
                            document,
                            input_config_version,
                            config,
                            input_config_revision,
                        )
                        logger.info(f"process_segments returned hitl_result: {hitl_result}")
                        if hitl_result or hitl_triggered:
                            hitl_triggered = True
                elif isinstance(output_metadata, dict):
                    for asset_id, asset in output_metadata.items():
                        document, hitl_result = process_segments(
                            input_bucket,
                            output_bucket,
                            object_key,
                            asset.get("segment_metadata", []),
                            confidence_threshold,
                            execution_id,
                            document,
                            input_config_version,
                            config,
                            input_config_revision,
                        )
                        logger.info(f"process_segments returned hitl_result: {hitl_result}")
                        if hitl_result or hitl_triggered:
                            hitl_triggered = True
                else:
                    logger.error(
                        "Unexpected output_metadata format in job_metadata.json"
                    )
            else:
                logger.warning("No output_metadata found in job_metadata.json")
        except Exception as e:
            logger.error(f"Error processing job_metadata.json: {str(e)}", exc_info=True)
    except Exception as e:
        logger.error(f"Error in HITL processing: {str(e)}", exc_info=True)

    logger.info(f"Final hitl_triggered value: {hitl_triggered}")

    # Record metrics for processed pages
    metrics.put_metric("ProcessedDocuments", 1)
    metrics.put_metric("ProcessedPages", total_pages)
    metrics.put_metric("ProcessedCustomPages", custom_pages_count)
    metrics.put_metric("ProcessedStandardPages", standard_pages_count)

    # Add metering information
    document.metering = {
        "BDAProject/bda/documents-custom": {"pages": custom_pages_count},
        "BDAProject/bda/documents-standard": {"pages": standard_pages_count},
    }

    # Set Review Status on document model if HITL review is needed
    # Only set to PendingReview if not already reviewed (preserve completed/skipped status on reprocess)
    if hitl_triggered and document.sections:
        existing_status = document.hitl_status
        if existing_status not in ("Review Completed", "Review Skipped", "Completed", "Skipped"):
            hitl_sections_pending = [section.section_id for section in document.sections]
            document.hitl_status = "PendingReview"
            document.hitl_triggered = True
            document.hitl_sections_pending = hitl_sections_pending
            document.hitl_sections_completed = []
            logger.info(f"Document requires human review. Sections pending: {hitl_sections_pending}")
        else:
            logger.info(f"Document already reviewed (status: {existing_status}), preserving HITL status on reprocess")

    # Compute confidence alert count from all sections
    document.confidence_alert_count = sum(
        len(section.confidence_threshold_alerts)
        for section in document.sections
    )
    logger.info(f"Total confidence alert count: {document.confidence_alert_count}")

    # Update document (includes Review Status)
    document_service.update_document(document)

    # Prepare response using new serialization method
    # Use working bucket for document compression
    working_bucket = os.environ.get("WORKING_BUCKET")
    if not working_bucket:
        logger.warning(
            "WORKING_BUCKET environment variable not set, using output_bucket for compression"
        )
        working_bucket = output_bucket

    # Check if rule validation is enabled for this config version
    rule_validation_enabled = is_rule_validation_enabled(
        input_config_version, input_config_revision
    )

    response = {
        "document": document.serialize_document(
            working_bucket, "processresults", logger
        ),
        "hitl_triggered": hitl_triggered,
        "rule_validation_enabled": rule_validation_enabled,
        "bda_response_count": len(bda_responses),
    }

    logger.info(f"Response: {json.dumps(response, default=str)}")
    return response
