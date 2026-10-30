# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Embedding service for multi-document discovery.

Provides batch multimodal embedding generation using Bedrock embedding models
(Cohere Embed v4, Amazon Titan Multimodal) with concurrency control and
throttle handling via the existing BedrockClient retry logic.
"""

import io
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from idp_common.bedrock.client import BedrockClient

logger = logging.getLogger(__name__)


@dataclass
class EmbeddingResult:
    """Result of batch embedding generation."""

    embeddings: np.ndarray
    """2D array of embeddings (n_valid_documents x embedding_dim)."""

    valid_keys: List[str]
    """S3 keys that were successfully embedded (in same order as embeddings)."""

    failed_keys: List[str]
    """S3 keys that failed embedding generation."""

    embedding_dim: int
    """Dimensionality of the embeddings."""

    def to_serializable(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dict for Step Functions state passing."""
        return {
            "embeddings_shape": list(self.embeddings.shape),
            "valid_keys": self.valid_keys,
            "failed_keys": self.failed_keys,
            "embedding_dim": self.embedding_dim,
            "num_valid": len(self.valid_keys),
            "num_failed": len(self.failed_keys),
        }


class EmbeddingService:
    """
    Batch multimodal embedding generation for document images.

    Uses BedrockClient's built-in retry/throttle handling for robust
    high-volume embedding generation. Supports Cohere Embed v4 and
    Amazon Titan Multimodal Embedding models.

    Example:
        >>> from idp_common.bedrock.client import BedrockClient
        >>> client = BedrockClient(region="us-east-1")
        >>> service = EmbeddingService(client, model_id="cohere.embed-english-v3")
        >>> result = service.embed_document_images(
        ...     bucket="my-bucket",
        ...     s3_keys=["doc1.pdf", "doc2.png"],
        ...     progress_callback=lambda done, total: print(f"{done}/{total}")
        ... )
    """

    # Default embedding model — must support multimodal (image) embeddings.
    # Cohere Embed v4 is preferred (best quality, multimodal).
    # Alternative: amazon.titan-embed-image-v1 (lower quality but simpler API)
    DEFAULT_MODEL_ID = "us.cohere.embed-v4:0"

    # Maximum image size for embedding (bytes) - 5MB
    MAX_IMAGE_SIZE = 5 * 1024 * 1024

    # Maximum image pixel dimension for Titan Embed Image (2048x2048)
    MAX_IMAGE_DIMENSION = 2048

    def __init__(
        self,
        bedrock_client: BedrockClient,
        model_id: Optional[str] = None,
        max_concurrent: int = 5,
        input_type: str = "search_document",
    ):
        """
        Initialize the embedding service.

        Args:
            bedrock_client: BedrockClient instance with retry/throttle handling
            model_id: Bedrock embedding model ID (default: cohere.embed-english-v3)
            max_concurrent: Maximum concurrent embedding requests
            input_type: Input type for Cohere models (search_document, search_query,
                       classification, clustering)
        """
        self.client = bedrock_client
        self.model_id = model_id or self.DEFAULT_MODEL_ID
        self.max_concurrent = max_concurrent
        self.input_type = input_type

    def embed_document_images(
        self,
        bucket: str,
        s3_keys: List[str],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> EmbeddingResult:
        """
        Generate embeddings for document images stored in S3.

        Downloads each image from S3, compresses it if needed, and generates
        embeddings using the configured Bedrock model.

        Args:
            bucket: S3 bucket name
            s3_keys: List of S3 keys for document images
            progress_callback: Optional callable(completed, total) for progress updates

        Returns:
            EmbeddingResult with embeddings matrix and metadata
        """
        logger.info(
            f"Generating embeddings for {len(s3_keys)} documents "
            f"using model {self.model_id}"
        )

        # Prepare embedding items by downloading and compressing images
        items: List[Dict[str, Any]] = []
        key_mapping: List[str] = []  # Track which key maps to which item index

        for key in s3_keys:
            try:
                image_bytes = self._download_and_prepare_image(bucket, key)
                if image_bytes:
                    items.append({"image_bytes": image_bytes})
                    key_mapping.append(key)
                else:
                    logger.warning(f"Skipping {key}: could not prepare image")
            except Exception as e:
                logger.warning(f"Skipping {key}: {e}")

        if not items:
            logger.warning("No valid documents to embed")
            return EmbeddingResult(
                embeddings=np.array([]),
                valid_keys=[],
                failed_keys=list(s3_keys),
                embedding_dim=0,
            )

        logger.info(f"Prepared {len(items)} images for embedding")

        # Generate embeddings using batch method
        raw_embeddings = self.client.generate_embeddings_batch(
            items=items,
            model_id=self.model_id,
            max_concurrent=self.max_concurrent,
            input_type=self.input_type,
            progress_callback=progress_callback,
        )

        # Separate successful and failed embeddings
        valid_embeddings: List[List[float]] = []
        valid_keys: List[str] = []
        failed_keys: List[str] = []

        for i, embedding in enumerate(raw_embeddings):
            if embedding is not None and len(embedding) > 0:
                valid_embeddings.append(embedding)
                valid_keys.append(key_mapping[i])
            else:
                failed_keys.append(key_mapping[i])

        # Add keys that couldn't even be downloaded
        downloaded_set = set(key_mapping)
        for key in s3_keys:
            if key not in downloaded_set:
                failed_keys.append(key)

        if not valid_embeddings:
            logger.warning("No successful embeddings generated")
            return EmbeddingResult(
                embeddings=np.array([]),
                valid_keys=[],
                failed_keys=list(s3_keys),
                embedding_dim=0,
            )

        embeddings_array = np.array(valid_embeddings)
        embedding_dim = embeddings_array.shape[1]

        logger.info(
            f"Generated {len(valid_embeddings)} embeddings "
            f"({embedding_dim} dimensions), "
            f"{len(failed_keys)} failed"
        )

        return EmbeddingResult(
            embeddings=embeddings_array,
            valid_keys=valid_keys,
            failed_keys=failed_keys,
            embedding_dim=embedding_dim,
        )

    def embed_images_from_bytes(
        self,
        images: List[bytes],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> Tuple[np.ndarray, List[int]]:
        """
        Generate embeddings for in-memory image bytes.

        Args:
            images: List of image bytes
            progress_callback: Optional callable(completed, total) for progress updates

        Returns:
            Tuple of (embeddings_array, valid_indices) where valid_indices maps
            rows in the embeddings array back to the original images list.
        """
        items = []
        index_mapping: List[int] = []

        for i, img_bytes in enumerate(images):
            compressed = self._compress_image_bytes(img_bytes)
            if compressed:
                items.append({"image_bytes": compressed})
                index_mapping.append(i)

        if not items:
            return np.array([]), []

        raw_embeddings = self.client.generate_embeddings_batch(
            items=items,
            model_id=self.model_id,
            max_concurrent=self.max_concurrent,
            input_type=self.input_type,
            progress_callback=progress_callback,
        )

        valid_embeddings: List[List[float]] = []
        valid_indices: List[int] = []

        for i, embedding in enumerate(raw_embeddings):
            if embedding is not None and len(embedding) > 0:
                valid_embeddings.append(embedding)
                valid_indices.append(index_mapping[i])

        if not valid_embeddings:
            return np.array([]), []

        return np.array(valid_embeddings), valid_indices

    def _download_and_prepare_image(self, bucket: str, key: str) -> Optional[bytes]:
        """
        Download a document from S3 and prepare it for embedding.

        For PDFs, renders the first page as an image.
        For images, compresses to meet size constraints.

        Args:
            bucket: S3 bucket name
            key: S3 object key

        Returns:
            Prepared image bytes, or None if preparation fails
        """
        import boto3

        try:
            s3_client = boto3.client("s3", region_name=self.client.region)
            response = s3_client.get_object(Bucket=bucket, Key=key)
            raw_bytes = response["Body"].read()

            key_lower = key.lower()
            if key_lower.endswith(".pdf"):
                return self._render_pdf_first_page(raw_bytes)
            elif key_lower.endswith(
                (".png", ".jpg", ".jpeg", ".tiff", ".tif", ".webp")
            ):
                return self._compress_image_bytes(raw_bytes)
            else:
                logger.warning(f"Unsupported file type for embedding: {key}")
                return None

        except Exception as e:
            logger.warning(f"Failed to download/prepare {key}: {e}")
            return None

    def _render_pdf_first_page(self, pdf_bytes: bytes) -> Optional[bytes]:
        """
        Render the first page of a PDF as a JPEG image.

        Args:
            pdf_bytes: Raw PDF bytes

        Returns:
            JPEG image bytes of the first page, or None on failure
        """
        try:
            import pypdfium2 as pdfium

            pdf = pdfium.PdfDocument(pdf_bytes)
            if len(pdf) == 0:
                return None

            page = pdf[0]
            # Render at 150 DPI for good quality without being too large
            bitmap = page.render(scale=150 / 72)
            pil_image = bitmap.to_pil()
            pdf.close()

            return self._compress_pil_image(pil_image)

        except Exception as e:
            logger.warning(f"Failed to render PDF first page: {e}")
            return None

    def _compress_image_bytes(
        self, image_bytes: bytes, max_size: Optional[int] = None
    ) -> Optional[bytes]:
        """
        Compress image bytes to meet size and pixel dimension constraints.

        Ensures images are within both the file size limit and the pixel
        dimension limit required by embedding models (e.g., Titan Embed Image
        has a 2048x2048 max pixel limit).

        Args:
            image_bytes: Raw image bytes
            max_size: Maximum size in bytes (default: MAX_IMAGE_SIZE)

        Returns:
            Compressed image bytes, or None on failure
        """
        max_size = max_size or self.MAX_IMAGE_SIZE

        try:
            from PIL import Image

            img = Image.open(io.BytesIO(image_bytes))
            width, height = img.size

            # Check if image needs resizing for pixel dimensions
            needs_resize = (
                width > self.MAX_IMAGE_DIMENSION or height > self.MAX_IMAGE_DIMENSION
            )

            if not needs_resize and len(image_bytes) <= max_size:
                return image_bytes

            if needs_resize:
                # Resize to fit within MAX_IMAGE_DIMENSION while maintaining aspect ratio
                img.thumbnail(
                    (self.MAX_IMAGE_DIMENSION, self.MAX_IMAGE_DIMENSION),
                    Image.Resampling.LANCZOS,
                )
                logger.debug(
                    f"Resized image from {width}x{height} to {img.size[0]}x{img.size[1]}"
                )

            return self._compress_pil_image(img, max_size)
        except Exception as e:
            logger.warning(f"Failed to compress image: {e}")
            return None

    def _compress_pil_image(
        self, img: Any, max_size: Optional[int] = None
    ) -> Optional[bytes]:
        """
        Compress a PIL Image to JPEG bytes within size constraints.

        Args:
            img: PIL Image object
            max_size: Maximum size in bytes

        Returns:
            Compressed JPEG bytes
        """
        max_size = max_size or self.MAX_IMAGE_SIZE

        # Convert to RGB if necessary (for PNG with alpha, etc.)
        if img.mode != "RGB":
            img = img.convert("RGB")

        # Try progressively lower quality until within size
        for quality in [85, 70, 50, 30]:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            img_bytes = buf.getvalue()
            if len(img_bytes) <= max_size:
                return img_bytes

        # If still too large, resize the image
        width, height = img.size
        for scale in [0.75, 0.5, 0.25]:
            new_size = (int(width * scale), int(height * scale))
            resized = img.resize(new_size)
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=50)
            img_bytes = buf.getvalue()
            if len(img_bytes) <= max_size:
                return img_bytes

        logger.warning("Could not compress image within size constraints")
        return None
