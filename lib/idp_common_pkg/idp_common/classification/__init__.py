# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Classification module for IDP Common Package.

Provides a service for classifying documents using LLMs.
"""

from idp_common.classification.forced_class import apply_forced_document_class
from idp_common.classification.models import (
    ClassificationResult,
    DocumentClassification,
)
from idp_common.classification.service import ClassificationService

__all__ = [
    "ClassificationService",
    "DocumentClassification",
    "ClassificationResult",
    "apply_forced_document_class",
]
