import logging
from idp_common import image, s3
from typing import Any, Dict, List

from idp_common.config.models import IDPConfig
from idp_common.config.schema_constants import (
    LEGACY_ATTRIBUTES_PROMPT,
    LEGACY_CLASS_PROMPT,
    LEGACY_IMAGE_PATH,
    X_AWS_IDP_ATTRIBUTES_PROMPT,
    X_AWS_IDP_CLASS_PROMPT,
    X_AWS_IDP_CLASSIFICATION,
    X_AWS_IDP_EXAMPLES,
    X_AWS_IDP_IMAGE_PATH,
)

logger = logging.getLogger(__name__)


def _example_field(example: Dict[str, Any], *keys: str) -> Any:
    """
    Read the first present key from a few-shot example.

    Example entries carry their fields under either the ``x-aws-idp-*`` names
    (what `docs/few-shot-examples.md` documents, and what a hand-authored or
    migrated v0.6 config uses) or the legacy camelCase names (what the UI's
    schema editor writes and what `config_library` presets contain). Both forms
    are accepted so examples are never silently ignored because of which spelling
    the config happens to use.

    Returns:
        The first non-None value found, or None.
    """
    for key in keys:
        value = example.get(key)
        if value is not None:
            return value
    return None


def _get_image_files_from_path(image_path: str) -> List[str]:
    """
    Get list of image files from a path that could be a single file, directory, or S3 prefix.

    Args:
        image_path: Path to image file, directory, or S3 prefix

    Returns:
        List of image file paths/URIs sorted by filename
    """
    import os

    from idp_common import s3

    # Handle S3 URIs
    if image_path.startswith("s3://"):
        # Check if it's a direct file or a prefix
        if image_path.endswith(
            (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp")
        ):
            # Direct S3 file
            return [image_path]
        else:
            # S3 prefix - list all images
            return s3.list_images_from_path(image_path)
    else:
        # Handle local paths
        config_bucket = os.environ.get("CONFIGURATION_BUCKET")
        root_dir = os.environ.get("ROOT_DIR")

        if config_bucket:
            # Use environment bucket with imagePath as key
            s3_uri = f"s3://{config_bucket}/{image_path}"

            # Check if it's a direct file or a prefix
            if image_path.endswith(
                (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp")
            ):
                # Direct S3 file
                return [s3_uri]
            else:
                # S3 prefix - list all images
                return s3.list_images_from_path(s3_uri)
        elif root_dir:
            # Use relative path from ROOT_DIR
            full_path = os.path.join(root_dir, image_path)
            full_path = os.path.normpath(full_path)

            if os.path.isfile(full_path):
                # Single local file
                return [full_path]
            elif os.path.isdir(full_path):
                # Local directory - list all images
                return s3.list_images_from_path(full_path)
            else:
                # Path doesn't exist
                logger.warning(f"Image path does not exist: {full_path}")
                return []
        else:
            raise ValueError(
                "No CONFIGURATION_BUCKET or ROOT_DIR set. Cannot read example images from local filesystem."
            )


def build_few_shot_examples_content(config: IDPConfig) -> List[Dict[str, Any]]:
    """
    Build content items for few-shot examples from the configuration.

    Returns:
        List of content items containing text and image content for examples
    """
    content = []
    classes = config.classes or []

    for schema in classes:
        # Examples are stored directly on the schema object
        examples = schema.get(X_AWS_IDP_EXAMPLES, [])
        for example in examples:
            class_prompt = _example_field(
                example, X_AWS_IDP_CLASS_PROMPT, LEGACY_CLASS_PROMPT
            )

            # Only process this example if it has a non-empty class_prompt
            if not class_prompt or not class_prompt.strip():
                logger.info(
                    f"Skipping example with empty classPrompt: {example.get('name')}"
                )
                continue

            content.append({"text": class_prompt})

            image_path = _example_field(
                example, X_AWS_IDP_IMAGE_PATH, LEGACY_IMAGE_PATH
            )
            if image_path:
                try:
                    # Load image content from the path

                    # Get list of image files from the path (supports directories/prefixes)
                    image_files = _get_image_files_from_path(image_path)

                    # Process each image file
                    for image_file_path in image_files:
                        try:
                            # Load image content
                            if image_file_path.startswith("s3://"):
                                # Direct S3 URI
                                image_content = s3.get_binary_content(image_file_path)
                            else:
                                # Local file
                                with open(image_file_path, "rb") as f:
                                    image_content = f.read()

                            # Prepare image content for Bedrock
                            image_attachment = image.prepare_bedrock_image_attachment(
                                image_content
                            )
                            content.append(image_attachment)

                        except Exception as e:
                            logger.warning(
                                f"Failed to load image {image_file_path}: {e}"
                            )
                            continue

                except Exception as e:
                    # Degrade to a text-only example rather than failing the whole
                    # inference. An example's imagePath can easily be unreachable
                    # (config copied between accounts/partitions, bucket deleted,
                    # missing s3:GetObject), and a prompt that merely *offers* the
                    # {FEW_SHOT_EXAMPLES} placeholder must not turn that into a
                    # failed document. Per-file failures already warn-and-continue
                    # inside the loop; this covers listing/resolution failures.
                    logger.warning(
                        f"Failed to load example images from {image_path}: {e} — "
                        f"using example '{example.get('name')}' without images"
                    )

    return content


def build_few_shot_extraction_examples_content(
    target_class: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Build content items for few-shot examples for extraction from a specific class schema.
    Uses attributesPrompt from examples.

    Args:
        target_class: The JSON Schema for the target document class

    Returns:
        List of content items containing text and image content for examples
    """
    content = []

    # Get examples from the schema
    examples = target_class.get(X_AWS_IDP_EXAMPLES, [])

    for example in examples:
        # For extraction, use attributesPrompt instead of classPrompt
        attributes_prompt = _example_field(
            example, X_AWS_IDP_ATTRIBUTES_PROMPT, LEGACY_ATTRIBUTES_PROMPT
        )

        # Only process this example if it has a non-empty attributesPrompt
        if not attributes_prompt or not attributes_prompt.strip():
            logger.info(
                f"Skipping example with empty attributesPrompt: {example.get('name')}"
            )
            continue

        content.append({"text": attributes_prompt})

        image_path = _example_field(example, X_AWS_IDP_IMAGE_PATH, LEGACY_IMAGE_PATH)
        if image_path:
            try:
                # Get list of image files from the path (supports directories/prefixes)
                image_files = _get_image_files_from_path(image_path)

                # Process each image file
                for image_file_path in image_files:
                    try:
                        # Load image content
                        if image_file_path.startswith("s3://"):
                            # Direct S3 URI
                            image_content = s3.get_binary_content(image_file_path)
                        else:
                            # Local file
                            with open(image_file_path, "rb") as f:
                                image_content = f.read()

                        # Prepare image content for Bedrock
                        image_attachment = image.prepare_bedrock_image_attachment(
                            image_content
                        )
                        content.append(image_attachment)

                    except Exception as e:
                        logger.warning(f"Failed to load image {image_file_path}: {e}")
                        continue

            except Exception as e:
                # Degrade to a text-only example rather than failing extraction —
                # see the matching comment in build_few_shot_examples_content.
                logger.warning(
                    f"Failed to load example images from {image_path}: {e} — "
                    f"using example '{example.get('name')}' without images"
                )

    return content
