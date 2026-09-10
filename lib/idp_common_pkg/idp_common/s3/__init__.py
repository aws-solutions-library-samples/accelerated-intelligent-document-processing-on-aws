# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import logging
import os
from typing import Any, Dict, List, Optional, Union

import boto3

from ..utils import parse_s3_uri

logger = logging.getLogger(__name__)

# Initialize clients
_s3_client = None


def get_s3_client():
    """
    Get or initialize the S3 client

    Returns:
        boto3 S3 client
    """
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def get_text_content(s3_uri: str) -> str:
    """
    Read text content from an S3 URI

    Args:
        s3_uri: The S3 URI in format s3://bucket/key

    Returns:
        Text content from the S3 object
    """
    try:
        bucket, key = parse_s3_uri(s3_uri)
        s3 = get_s3_client()
        response = s3.get_object(Bucket=bucket, Key=key)
        content_str = response["Body"].read().decode("utf-8")

        # Check if the content is JSON or plain text
        if s3_uri.endswith(".json"):
            try:
                content = json.loads(content_str)
                return content.get("text", content_str)
            except json.JSONDecodeError:
                logger.warning(
                    f"File has .json extension but content is not valid JSON: {s3_uri}"
                )
                return content_str
        else:
            # For non-JSON files (like .md), return the content directly
            return content_str
    except Exception as e:
        logger.error(f"Error reading text from {s3_uri}: {e}")
        raise


def get_json_content(s3_uri: str) -> Dict[str, Any]:
    """
    Read JSON content from an S3 URI

    Args:
        s3_uri: The S3 URI in format s3://bucket/key

    Returns:
        Parsed JSON content
    """
    try:
        bucket, key = parse_s3_uri(s3_uri)
        s3 = get_s3_client()
        response = s3.get_object(Bucket=bucket, Key=key)
        return json.loads(response["Body"].read().decode("utf-8"))
    except Exception as e:
        logger.error(f"Error reading JSON from {s3_uri}: {e}")
        raise


def get_binary_content(s3_uri: str) -> bytes:
    """
    Read binary content from an S3 URI

    Args:
        s3_uri: The S3 URI in format s3://bucket/key

    Returns:
        Binary content from the S3 object
    """
    try:
        bucket, key = parse_s3_uri(s3_uri)
        s3 = get_s3_client()
        response = s3.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()
    except Exception as e:
        logger.error(f"Error reading binary content from {s3_uri}: {e}")
        raise


def write_content(
    content: Union[str, bytes, Dict[str, Any], List[Any]],
    bucket: str,
    key: str,
    content_type: Optional[str] = None,
) -> None:
    """
    Write content to S3

    Args:
        content: The content to write (string, bytes, or dict that will be converted to JSON)
        bucket: The S3 bucket
        key: The S3 key
        content_type: Optional content type for the S3 object
    """
    try:
        s3 = get_s3_client()

        # Handle different content types
        if isinstance(content, (dict, list)):
            body = json.dumps(content).encode("utf-8")
            if content_type is None:
                content_type = "application/json"
        elif isinstance(content, str):
            body = content
            if content_type is None:
                content_type = "text/plain"
        else:
            body = content
            if content_type is None:
                content_type = "application/octet-stream"

        # Upload to S3
        extra_args = {}
        if content_type:
            extra_args["ContentType"] = content_type

        s3.put_object(Bucket=bucket, Key=key, Body=body, **extra_args)
        logger.info(f"Successfully wrote to s3://{bucket}/{key}")
    except Exception as e:
        logger.error(f"Error writing to s3://{bucket}/{key}: {e}")
        raise


def list_images_from_path(image_path: str) -> List[str]:
    """
    List all image files from an S3 prefix or local directory.
    Returns image URIs/paths sorted by filename.

    Args:
        image_path: S3 URI (s3://bucket/prefix/) or local directory path

    Returns:
        List of image file paths/URIs sorted by filename
    """
    image_extensions = {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".tiff",
        ".tif",
        ".webp",
    }

    if image_path.startswith("s3://"):
        return _list_s3_images(image_path, image_extensions)
    else:
        return _list_local_images(image_path, image_extensions)


def _list_s3_images(s3_prefix: str, image_extensions: set) -> List[str]:
    """
    List image files from an S3 prefix.

    Args:
        s3_prefix: S3 URI prefix (s3://bucket/prefix/)
        image_extensions: Set of valid image file extensions

    Returns:
        List of S3 URIs for image files sorted by filename
    """
    try:
        bucket, prefix = parse_s3_uri(s3_prefix)

        # Ensure prefix ends with / if it's meant to be a directory
        if not prefix.endswith("/") and prefix:
            prefix += "/"

        s3 = get_s3_client()
        paginator = s3.get_paginator("list_objects_v2")

        image_files = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            if "Contents" in page:
                for obj in page["Contents"]:
                    key = obj["Key"]
                    # Skip the prefix itself if it's a "directory" key
                    if key == prefix:
                        continue

                    # Check if file has image extension
                    file_ext = os.path.splitext(key)[1].lower()
                    if file_ext in image_extensions:
                        image_files.append(f"s3://{bucket}/{key}")

        # Sort by filename (not full path)
        image_files.sort(key=lambda x: os.path.basename(x))
        logger.info(f"Found {len(image_files)} image files in S3 prefix: {s3_prefix}")
        return image_files

    except Exception as e:
        logger.error(f"Error listing images from S3 prefix {s3_prefix}: {e}")
        raise


def _list_local_images(directory_path: str, image_extensions: set) -> List[str]:
    """
    List image files from a local directory.

    Args:
        directory_path: Local directory path
        image_extensions: Set of valid image file extensions

    Returns:
        List of local file paths for image files sorted by filename
    """
    try:
        if not os.path.exists(directory_path):
            logger.warning(f"Local directory does not exist: {directory_path}")
            return []

        if not os.path.isdir(directory_path):
            logger.warning(f"Path is not a directory: {directory_path}")
            return []

        image_files = []
        for filename in os.listdir(directory_path):
            file_path = os.path.join(directory_path, filename)

            # Skip directories
            if os.path.isdir(file_path):
                continue

            # Check if file has image extension
            file_ext = os.path.splitext(filename)[1].lower()
            if file_ext in image_extensions:
                image_files.append(file_path)

        # Sort by filename
        image_files.sort(key=os.path.basename)
        logger.info(
            f"Found {len(image_files)} image files in local directory: {directory_path}"
        )
        return image_files

    except Exception as e:
        logger.error(f"Error listing images from local directory {directory_path}: {e}")
        raise


class _GlobMatcher:
    """Match S3 keys against a glob in time linear in the key length.

    ``**/`` matches zero or more folders, ``**`` matches anything including ``/``,
    ``*`` matches within one folder level, ``?`` matches one non-``/`` character.
    Every other character is literal: a ``.`` in a name is a dot, and ``(``, ``+``
    or ``[`` in a document name neither mis-match nor raise. Matching is
    case-insensitive. The previous translation had none of this — ``*`` could not
    cross ``/``, ``**`` degraded to two single-segment wildcards, and literals
    reached ``re.compile`` unescaped — so a pattern like ``folder/**/*.pdf``
    matched nothing nested (#808).

    This is deliberately not a regex. A backtracking regex for the same rules
    (``(?:.*/)?`` per ``**/``) is exponential in the number of wildcard groups
    against a key that does not match: measured at 4 s per key for four ``**/``
    groups on a 600-character key, and the pattern comes straight from the
    ``listBucketFiles`` API while any user can upload a document with a deep key.
    Here the pattern is compiled once into a token list and each key is walked
    once with the set of live token positions (an NFA simulation), so the cost is
    bounded by ``len(pattern) * len(key)`` whatever the pattern says.
    """

    _LIT, _ONE, _STAR, _ANY, _FOLDERS, _FOLDERS_BODY = range(6)

    def __init__(self, pattern: str) -> None:
        tokens: list[tuple[int, str]] = []
        i = 0
        while i < len(pattern):
            if pattern.startswith("**/", i):
                # Entry token: skip the whole group (zero folders) or enter its
                # body, which consumes anything and leaves on a ``/``. Entry is
                # epsilon-only so a body that has already consumed characters
                # can never fall through without its closing ``/``.
                tokens.append((self._FOLDERS, ""))
                tokens.append((self._FOLDERS_BODY, ""))
                i += 3
            elif pattern.startswith("**", i):
                tokens.append((self._ANY, ""))
                i += 2
            elif pattern[i] == "*":
                tokens.append((self._STAR, ""))
                i += 1
            elif pattern[i] == "?":
                tokens.append((self._ONE, ""))
                i += 1
            else:
                tokens.append((self._LIT, pattern[i].casefold()))
                i += 1
        self._tokens = tokens
        self._end = len(tokens)
        # The positions reachable from each position without consuming a
        # character never change, so they are computed once here rather than
        # re-walked for every character of every key.
        self._eps = [self._closure(i) for i in range(self._end + 1)]

    def _closure(self, start: int) -> frozenset[int]:
        """Every position reachable from ``start`` without consuming a character."""
        seen = {start}
        stack = [start]
        while stack:
            i = stack.pop()
            if i >= self._end:
                continue
            kind = self._tokens[i][0]
            if kind in (self._STAR, self._ANY):
                nxt = (i + 1,)
            elif kind == self._FOLDERS:
                nxt = (i + 1, i + 2)
            else:
                continue
            for n in nxt:
                if n not in seen:
                    seen.add(n)
                    stack.append(n)
        return frozenset(seen)

    def match(self, key: str) -> bool:
        eps = self._eps
        live: frozenset[int] | set[int] = eps[0]
        for ch in key:
            ch = ch.casefold()
            nxt: set[int] = set()
            for i in live:
                if i >= self._end:
                    continue
                kind, lit = self._tokens[i]
                if kind == self._LIT:
                    if ch == lit:
                        nxt.add(i + 1)
                elif kind == self._ONE:
                    if ch != "/":
                        nxt.add(i + 1)
                elif kind == self._STAR:
                    if ch != "/":
                        nxt.add(i)
                elif kind == self._ANY:
                    nxt.add(i)
                elif kind == self._FOLDERS_BODY:
                    nxt.add(i)
                    if ch == "/":
                        nxt.add(i + 1)
            if not nxt:
                return False
            live = set().union(*(eps[n] for n in nxt))
        return self._end in live


def _literal_prefix(pattern: str) -> str:
    """The folder path before the first wildcard, for listing under an S3 Prefix.

    Cut back to the last ``/`` so a partial name is never used as a prefix. Empty
    when the pattern starts with a wildcard or has no ``/`` before one. S3
    prefixes are exact-case, which is why the folder path is the one part of a
    pattern that is; the matcher takes care of the rest.
    """
    wildcard_at = min(
        (pos for pos in (pattern.find("*"), pattern.find("?")) if pos != -1),
        default=len(pattern),
    )
    return pattern[: pattern.rfind("/", 0, wildcard_at) + 1]


def find_matching_files(
    bucket: str, pattern: str, modified_after: str | None = None
) -> List[str]:
    """
    Find files in S3 bucket that match a given pattern.

    Args:
        bucket: S3 bucket name
        pattern: Glob over the full key. ``**`` matches any depth (``**/`` is zero
            or more folders), ``*`` matches within one folder level, ``?`` matches
            one character; other characters are literal. The folder path before
            the first wildcard must match exactly; the rest is case-insensitive.
        modified_after: Optional ISO 8601 timestamp to filter files modified after this time

    Returns:
        List of matching file keys
    """
    from datetime import datetime, timezone

    try:
        s3 = get_s3_client()
        paginator = s3.get_paginator("list_objects_v2")

        matcher = _GlobMatcher(pattern)
        # Only the folder the pattern names is listed, not the whole bucket: the
        # matcher requires that same literal path at the start of every key, so
        # this cannot change what matches, only how much is read to find it.
        list_kwargs = {"Bucket": bucket}
        prefix = _literal_prefix(pattern)
        if prefix:
            list_kwargs["Prefix"] = prefix

        # Parse modified_after filter if provided
        cutoff_time = None
        if modified_after:
            cutoff_time = datetime.fromisoformat(modified_after.replace("Z", "+00:00"))
            if cutoff_time.tzinfo is None:
                cutoff_time = cutoff_time.replace(tzinfo=timezone.utc)
            logger.info(f"Filtering files modified after {cutoff_time.isoformat()}")

        matching_files = []

        for page in paginator.paginate(**list_kwargs):
            if "Contents" in page:
                for obj in page["Contents"]:
                    key = obj["Key"]
                    # Skip S3 "folder" pseudo-objects (zero-byte keys ending
                    # in '/' created by the S3 console). They are not documents
                    # and must never be pulled into a test set.
                    if key.endswith("/"):
                        continue
                    if matcher.match(key):
                        if cutoff_time and obj.get("LastModified"):
                            last_modified = obj["LastModified"]
                            if last_modified.tzinfo is None:
                                last_modified = last_modified.replace(
                                    tzinfo=timezone.utc
                                )
                            if last_modified < cutoff_time:
                                continue
                        matching_files.append(key)

        logger.info(
            f"Found {len(matching_files)} files matching pattern '{pattern}' in bucket '{bucket}'"
            + (f" (modified after {modified_after})" if modified_after else "")
        )
        return sorted(matching_files)

    except Exception as e:
        logger.error(
            f"Error finding matching files in bucket {bucket} with pattern {pattern}: {e}"
        )
        raise
