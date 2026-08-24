# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Models for criteria validation using LLMs.

This module provides data models for validation inputs and results.
"""

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class BedrockInput:
    """Input model for Bedrock LLM criteria validation."""

    rule: str
    prompt: str
    system_prompt: str
    policy_type: str
    recommendation: str
    user_history: Optional[str] = None
    txt_file_uri: Optional[str] = None
    initial_response: Optional[List] = None

    def __post_init__(self):
        """Validate and clean input data."""
        # Strip whitespace from string fields
        if isinstance(self.rule, str):
            self.rule = self.rule.strip()
        if isinstance(self.prompt, str):
            self.prompt = self.prompt.strip()
        if isinstance(self.system_prompt, str):
            self.system_prompt = self.system_prompt.strip()
        if isinstance(self.policy_type, str):
            self.policy_type = self.policy_type.strip()
        if isinstance(self.recommendation, str):
            self.recommendation = self.recommendation.strip()
        if self.user_history and isinstance(self.user_history, str):
            self.user_history = self.user_history.strip()
        if self.txt_file_uri and isinstance(self.txt_file_uri, str):
            self.txt_file_uri = self.txt_file_uri.strip()

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)


@dataclass
class FactExtractionResponse:
    """Response model from LLM for fact extraction step."""

    policy_type: str
    rule: str
    extracted_facts: List[Dict[str, str]] = field(default_factory=list)
    extraction_summary: str = ""
    z3_parameters: Optional[Dict[str, Any]] = field(default=None)

    def __init__(self, **kwargs):
        """Custom init to enforce validation."""
        expected_fields = {
            "policy_type",
            "rule",
            "extracted_facts",
            "extraction_summary",
            "z3_parameters",
        }

        # Check for extra fields
        extra_fields = set(kwargs.keys()) - expected_fields
        if extra_fields:
            raise TypeError(f"Unexpected keyword arguments: {extra_fields}")

        # Set fields
        self.policy_type = kwargs.get("policy_type")
        self.rule = kwargs.get("rule")
        self.extracted_facts = kwargs.get("extracted_facts", [])
        self.extraction_summary = kwargs.get("extraction_summary", "")
        self.z3_parameters = kwargs.get("z3_parameters")

        # Call post_init for validation
        self.__post_init__()

    def __post_init__(self):
        """Validate and clean input data."""
        if isinstance(self.policy_type, str):
            self.policy_type = self.policy_type.strip()
        if isinstance(self.rule, str):
            self.rule = self.rule.strip()
        if isinstance(self.extraction_summary, str):
            self.extraction_summary = self.extraction_summary.strip()

        # Validate extracted_facts is a list
        if self.extracted_facts is None:
            self.extracted_facts = []
        elif not isinstance(self.extracted_facts, list):
            raise TypeError("extracted_facts must be a list")

    def dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)


@dataclass
class LLMResponse:
    """Response model from LLM for criteria validation."""

    policy_type: str
    rule: str
    recommendation: str
    reasoning: str
    supporting_pages: List[str] = field(default_factory=list)

    def __init__(self, **kwargs):
        """Custom init to enforce extra="forbid" behavior."""
        # Get the expected field names
        expected_fields = {
            "policy_type",
            "rule",
            "recommendation",
            "reasoning",
            "supporting_pages",
        }

        # Check for extra fields
        extra_fields = set(kwargs.keys()) - expected_fields
        if extra_fields:
            raise TypeError(f"Unexpected keyword arguments: {extra_fields}")

        # Set defaults for missing fields
        self.policy_type = kwargs.get("policy_type")
        self.rule = kwargs.get("rule")
        self.recommendation = kwargs.get("recommendation")
        self.reasoning = kwargs.get("reasoning")
        self.supporting_pages = kwargs.get("supporting_pages", [])

        # Call post_init for validation
        self.__post_init__()

    def __post_init__(self):
        """Validate and clean input data."""
        # Strip whitespace from string fields
        if isinstance(self.policy_type, str):
            self.policy_type = self.policy_type.strip()
        if isinstance(self.rule, str):
            self.rule = self.rule.strip()

        # Validate and clean recommendation
        if isinstance(self.recommendation, str):
            self.recommendation = self.recommendation.strip()
            # Note: Validation against config options should be done at service level
            # where config is available, not in the model

        # Clean reasoning
        if self.reasoning:
            if isinstance(self.reasoning, str):
                # Remove line breaks and extra spaces
                self.reasoning = " ".join(self.reasoning.split())

                # Remove or replace problematic characters
                self.reasoning = re.sub(
                    r"[^\x20-\x7E]", "", self.reasoning
                )  # Remove non-printable characters

                # Remove markdown-style bullets and numbers
                self.reasoning = re.sub(r"^\s*[-*•]\s*", "", self.reasoning)
                self.reasoning = re.sub(r"^\s*\d+\.\s*", "", self.reasoning)

        # Validate supporting pages
        if self.supporting_pages is None:
            self.supporting_pages = []
        elif isinstance(self.supporting_pages, list):
            # Ensure all pages are strings (page numbers)
            self.supporting_pages = [str(page) for page in self.supporting_pages]

    def dict(self) -> Dict[str, Any]:
        """Convert to dictionary (compatibility method for Pydantic migration)."""
        return asdict(self)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)


@dataclass
class PolicyClass:
    """Policy class definition with type name and optional regex patterns."""

    policy_type: str
    """The policy type identifier."""

    document_name_regex: Optional[str] = None
    """Optional regex pattern to match against document ID/name."""

    document_page_content_regex: Optional[str] = None
    """Optional regex pattern to match against page content text."""

    # Private compiled regex patterns (not included in init)
    _compiled_name_regex: Optional[re.Pattern] = field(default=None, init=False)
    """Compiled regex pattern for document name matching."""

    _compiled_content_regex: Optional[re.Pattern] = field(default=None, init=False)
    """Compiled regex pattern for page content matching."""

    def __post_init__(self):
        """Compile regex patterns after initialization."""
        import logging

        logger = logging.getLogger(__name__)

        if self.document_name_regex:
            try:
                self._compiled_name_regex = re.compile(
                    self.document_name_regex, re.IGNORECASE
                )
                logger.debug(
                    f"Compiled document name regex for policy '{self.policy_type}': {self.document_name_regex}"
                )
            except re.error as e:
                logger.error(
                    f"Invalid document name regex for policy '{self.policy_type}': {e}"
                )
                self._compiled_name_regex = None

        if self.document_page_content_regex:
            try:
                self._compiled_content_regex = re.compile(
                    self.document_page_content_regex, re.IGNORECASE
                )
                logger.debug(
                    f"Compiled page content regex for policy '{self.policy_type}': {self.document_page_content_regex}"
                )
            except re.error as e:
                logger.error(
                    f"Invalid page content regex for policy '{self.policy_type}': {e}"
                )
                self._compiled_content_regex = None


@dataclass
class PolicyClassificationResult:
    """Result of policy classification for a document."""

    matched_policy_types: List[str] = field(default_factory=list)
    """List of matched policy type identifiers."""

    matched_page_ids: Dict[str, str] = field(default_factory=dict)
    """Map of policy_type -> page_id for page content matches."""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return asdict(self)
