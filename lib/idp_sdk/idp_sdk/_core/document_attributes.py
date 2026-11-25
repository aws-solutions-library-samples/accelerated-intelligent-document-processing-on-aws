# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Sending caller-supplied document attributes with an upload (#1851).

The rules (key pattern, value types, bounds) and the transport choice live in
``idp_common.document_attributes``; this module only applies them on the SDK side.
``idp_common`` is imported inside the functions, as elsewhere in the SDK, so that
importing this module does not load it.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)


def validate(attributes: Any) -> Dict[str, Any]:
    """``attributes`` validated, or ``ValueError`` naming every problem."""
    from idp_common.document_attributes import validate_attributes

    return validate_attributes(attributes)


def merge(
    batch_attributes: Optional[Mapping[str, Any]],
    document_attributes: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Batch-level attributes overlaid by a document's own, then validated.

    Validated after the merge because two sets that are each within the bounds
    can exceed them together.
    """
    merged: Dict[str, Any] = dict(batch_attributes or {})
    merged.update(document_attributes or {})
    return validate(merged) if merged else {}


def prepare_upload(
    s3_client: Any,
    bucket: str,
    key: str,
    attributes: Optional[Mapping[str, Any]],
    base_metadata: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """The user metadata to upload ``key`` with, carrying ``attributes``.

    ``attributes`` must already be validated. When they do not fit inline, the
    sidecar is uploaded here, **before** the document, so it exists by the time
    the document's upload event is processed.
    """
    from idp_common.document_attributes import (
        SIDECAR_SUFFIX,
        SIDECAR_URI_METADATA_KEY,
        encode_for_upload,
    )

    metadata, sidecar_body = encode_for_upload(attributes or {}, base_metadata)
    if sidecar_body is not None:
        sidecar_key = key + SIDECAR_SUFFIX
        s3_client.put_object(
            Bucket=bucket,
            Key=sidecar_key,
            Body=sidecar_body,
            ContentType="application/json",
        )
        metadata[SIDECAR_URI_METADATA_KEY] = f"s3://{bucket}/{sidecar_key}"
        logger.info(f"Uploaded document attributes sidecar for {key}")
    return metadata
