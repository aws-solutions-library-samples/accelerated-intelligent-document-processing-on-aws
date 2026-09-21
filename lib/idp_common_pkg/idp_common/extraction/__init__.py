# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Extraction module for IDP documents.

This module provides services and models for extracting structured information
from documents using LLMs.
"""

from idp_common.extraction.failure import (
    EXTRACTION_FAILED_CODE,
    persist_section_after_extraction_failure,
    record_section_extraction_failure,
)
from idp_common.extraction.models import ExtractedAttribute, ExtractionResult, PageInfo
from idp_common.extraction.page_type_resolver import (
    PageTypePresence,
    resolve_page_types,
)
from idp_common.extraction.runtime import (
    ExtractionRuntime,
    InProcessRuntime,
    NoopShardPersistence,
    S3ShardPersistence,
    ShardPersistence,
    StepFunctionsRuntime,
    extract_one_shard,
    merge_shard_dicts,
    merge_shard_results,
    select_runtime,
    shard_result_key,
)
from idp_common.extraction.service import ExtractionService
from idp_common.extraction.topk_resolver import (
    is_topk_response,
    resolve_candidates,
)

__all__ = [
    "ExtractionService",
    "ExtractedAttribute",
    "ExtractionResult",
    "PageInfo",
    "PageTypePresence",
    "resolve_page_types",
    # Runtime-agnostic sharding primitives
    "ExtractionRuntime",
    "InProcessRuntime",
    "StepFunctionsRuntime",
    "ShardPersistence",
    "S3ShardPersistence",
    "NoopShardPersistence",
    "extract_one_shard",
    "merge_shard_results",
    "merge_shard_dicts",
    "shard_result_key",
    "select_runtime",
    # 1S-TopK candidate resolution
    "is_topk_response",
    "resolve_candidates",
    # Making a failed section visible in DynamoDB (#1049)
    "EXTRACTION_FAILED_CODE",
    "record_section_extraction_failure",
    "persist_section_after_extraction_failure",
]
