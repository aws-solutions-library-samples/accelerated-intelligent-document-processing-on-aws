# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Process a document under a configuration supplied by S3 URI.

An input object carrying ``config-uri`` metadata names a JSON (or YAML) config in
the input bucket. The queue sender turns that into a **snapshot** with
:func:`prepare_config_snapshot` — validated, merged onto the system defaults, and
written once to the working bucket under a content-addressed key — and every step
after it reads the snapshot with :func:`load_config_snapshot`. No step reads the
configuration table for such a document.

Why a snapshot rather than reading the caller's object at every step: the caller
can overwrite or delete their object while the document is in flight, and a result
must correspond to exactly one configuration. A content-addressed key is also
idempotent, so a retried invocation that stages the same config again writes the
same object.
"""

import hashlib
import json
import logging
from typing import Any, Dict, Optional, Union

import boto3
import yaml
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

#: Working-bucket prefix holding config snapshots. The pipeline-hooks dispatcher
#: and the reporting Lambda are granted s3:GetObject on exactly this prefix, so it
#: is mirrored in template.yaml and patterns/unified/template.yaml.
SNAPSHOT_PREFIX = "config_snapshots"

#: S3 error codes meaning the caller's object cannot be read no matter how often
#: we retry. Anything else (throttling, 5xx) propagates so the message is retried.
_PERMANENT_S3_ERRORS = {"NoSuchKey", "NoSuchBucket", "AccessDenied", "404", "403"}


#: One S3 client per region for load_config_snapshot, which a Lambda may call more
#: than once per invocation and on every warm invocation.
_snapshot_clients: Dict[Optional[str], Any] = {}


class ConfigUriError(ValueError):
    """The supplied configuration cannot be used, so the document is rejected.

    The message is written to the document's ``errors`` and is what the caller
    sees, so it names what to fix.
    """


def parse_config_document(body: bytes, key: str) -> Dict[str, Any]:
    """Parse a supplied configuration: YAML for ``.yaml``/``.yml`` keys, else JSON.

    ``yaml.safe_load`` only — it constructs plain data and never arbitrary
    objects, which is the whole reason it is used rather than ``yaml.load``.
    """
    try:
        text = body.decode("utf-8")
        if key.lower().endswith((".yaml", ".yml")):
            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError) as e:
        raise ConfigUriError(f"Configuration at {key} could not be parsed: {e}") from e
    if not isinstance(data, dict):
        raise ConfigUriError(
            f"Configuration at {key} must be an object, not {type(data).__name__}"
        )
    return data


def prepare_config_snapshot(
    config_uri: str,
    *,
    allowed_bucket: Optional[str],
    working_bucket: str,
    s3_client: Any = None,
) -> str:
    """Validate the configuration at ``config_uri`` and snapshot it.

    Args:
        config_uri: ``s3://bucket/key`` of the supplied configuration.
        allowed_bucket: The only bucket a supplied configuration may live in (the
            stack's input bucket). None disables the check.
        working_bucket: Destination for the snapshot.
        s3_client: Optional boto3 S3 client.

    Returns:
        The snapshot's ``s3://`` URI, to store on ``Document.config_uri``.

    Raises:
        ConfigUriError: For anything retrying cannot fix — a malformed URI, a
            bucket other than ``allowed_bucket``, a missing or unreadable object,
            unparseable content, validation errors, or ``use_bda: true``.
        ClientError: For a transient S3 failure, so the caller can retry.
    """
    # Imported here, not at module level: this module is imported by the config
    # package's __init__, and both idp_common.utils (which imports the config
    # models) and merge_utils (which imports the bedrock client) import back into
    # it. At module level either one is a circular import for whichever of them a
    # Lambda happens to import first.
    from ..utils import parse_s3_uri
    from .merge_utils import validate_config
    from .migration import is_legacy_format, migrate_legacy_to_schema

    s3 = s3_client or boto3.client("s3")
    try:
        bucket, key = parse_s3_uri(config_uri)
    except ValueError as e:
        raise ConfigUriError(str(e)) from e
    if allowed_bucket and bucket != allowed_bucket:
        raise ConfigUriError(
            f"config-uri must name an object in the input bucket {allowed_bucket}, "
            f"not {bucket}"
        )

    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in _PERMANENT_S3_ERRORS:
            raise ConfigUriError(
                f"Configuration {config_uri} could not be read ({code})"
            ) from e
        raise

    supplied = parse_config_document(body, key)
    # The same legacy-class migration a stored profile gets when it is read
    # (ConfigurationRecord.from_dynamodb_item). The models accept either shape, so
    # without it a legacy-format class list validates and is then misread.
    for section in ("classes", "policy_classes"):
        if supplied.get(section) and is_legacy_format(supplied[section]):
            supplied[section] = migrate_legacy_to_schema(supplied[section])
    result = validate_config(supplied, pattern="pattern-2")
    if not result["valid"]:
        raise ConfigUriError(
            f"Configuration {config_uri} failed validation: "
            + "; ".join(result["errors"])
        )
    for warning in result["warnings"]:
        logger.warning(f"Configuration {config_uri}: {warning}")

    merged = result["merged_config"]
    # BDA mode runs against a BDA project linked to a STORED profile; a supplied
    # configuration has none, so it cannot be honoured. Refused rather than
    # silently run in pipeline mode, which would not be the configuration asked for.
    if merged.get("use_bda"):
        raise ConfigUriError(
            f"Configuration {config_uri} sets use_bda: true, which is not supported "
            f"for a supplied configuration (BDA mode needs a stored profile linked "
            f"to a BDA project)"
        )

    snapshot = json.dumps(merged, sort_keys=True, default=str).encode("utf-8")
    snapshot_key = f"{SNAPSHOT_PREFIX}/{hashlib.sha256(snapshot).hexdigest()}.json"
    s3.put_object(
        Bucket=working_bucket,
        Key=snapshot_key,
        Body=snapshot,
        ContentType="application/json",
    )
    snapshot_uri = f"s3://{working_bucket}/{snapshot_key}"
    logger.info(f"Snapshotted configuration {config_uri} to {snapshot_uri}")
    return snapshot_uri


def load_config_snapshot(
    config_uri: str,
    *,
    as_model: bool = True,
    region: Optional[str] = None,
) -> Union[Any, Dict[str, Any]]:
    """Load a snapshot written by :func:`prepare_config_snapshot`.

    The snapshot is already merged onto the system defaults and validated, so this
    is a read and a ``model_validate`` — no merging, no table.

    Returns:
        ``IDPConfig`` when ``as_model`` is True, else its ``model_dump``.
    """
    from ..utils import parse_s3_uri
    from .models import IDPConfig

    bucket, key = parse_s3_uri(config_uri)
    s3 = _snapshot_clients.get(region)
    if s3 is None:
        s3 = boto3.client("s3", region_name=region) if region else boto3.client("s3")
        _snapshot_clients[region] = s3
    data = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    config = IDPConfig.model_validate(data)
    logger.info(f"Loaded configuration from {config_uri}")
    return config if as_model else config.model_dump(mode="python")
