# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The one way the SDK and CLI generate a timestamped identifier."""

import secrets
from datetime import datetime, timezone

# Four random bytes, rendered as eight lowercase hex characters.
_SUFFIX_BYTES = 4


def generate_timestamped_id(prefix: str) -> str:
    """``{prefix}-{YYYYMMDD-HHMMSS}-{8 hex}``, unique without relying on the clock.

    The UTC timestamp keeps the id readable and sortable by creation time. It
    resolves to the second, so on its own two ids generated with the same prefix
    in the same second are equal; a generated batch id is the S3 key prefix its
    documents are copied under, so two such batches overwrote each other's
    documents and shared status, results and downloads. The random suffix is what
    makes the id unique: it does not depend on the clock, so it also separates two
    processes generating ids at the same instant.

    Nothing in the SDK, CLI or web UI parses the timestamp back out of an id, so
    the suffix changes only the shape a user sees. An id a caller passes in
    explicitly is never rewritten.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{timestamp}-{secrets.token_hex(_SUFFIX_BYTES)}"
