# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Policy classification service for rule validation.

This module classifies which policy classes apply to a document based on:
1. Single policy class - No regex required, proceed with that policy class
2. Multiple policy classes - Requires regex patterns to filter:
   - Document name regex (x-aws-idp-document-name-regex)
   - Page content regex (x-aws-idp-page-content-regex)
"""

import logging
from typing import Any, Dict, List, Union

from idp_common import s3
from idp_common.config.models import IDPConfig
from idp_common.config.schema_constants import (
    X_AWS_IDP_DOCUMENT_NAME_REGEX,
    X_AWS_IDP_PAGE_CONTENT_REGEX,
    X_AWS_IDP_POLICY_TYPE,
    X_AWS_IDP_RULE_TYPE,
)
from idp_common.models import Document
from idp_common.rule_validation.models import PolicyClass, PolicyClassificationResult
from idp_common.utils.transient_errors import reraise_if_transient

logger = logging.getLogger(__name__)


class PolicyClassificationService:
    """Service for classifying which policy classes apply to a document."""

    def __init__(
        self,
        config: Union[Dict[str, Any], IDPConfig] = None,
        max_pages_for_content_check: int = None,
    ):
        if config is not None and isinstance(config, dict):
            config_model: IDPConfig = IDPConfig(**config)
        elif config is None:
            config_model = IDPConfig()
        else:
            config_model = config

        self.config = config_model
        self.max_pages_for_content_check = max_pages_for_content_check
        self.policy_classes = self._load_policy_classes()

    def _load_policy_classes(self) -> List[PolicyClass]:
        """Load policy classes from configuration."""
        policy_classes = []
        config_dict = self.config.to_dict()
        raw_policy_classes = config_dict.get("policy_classes", [])

        for policy_config in raw_policy_classes:
            # Accept the legacy 'x-aws-idp-rule-type' as a fallback. Raw
            # rules-discovery output and older hand-written configs carry that
            # name; keying only on the current one meant such a class was
            # skipped here AND invisible in the UI, so rule validation silently
            # never fired with nothing logged to explain why.
            policy_type = policy_config.get(X_AWS_IDP_POLICY_TYPE)
            if not policy_type:
                policy_type = policy_config.get(X_AWS_IDP_RULE_TYPE)
                if policy_type:
                    logger.warning(
                        "Policy class '%s' uses the legacy '%s' key; it is being "
                        "honored, but rename it to '%s' — the Policy Schema UI "
                        "reads only the current key and will not display it.",
                        policy_type,
                        X_AWS_IDP_RULE_TYPE,
                        X_AWS_IDP_POLICY_TYPE,
                    )
            if not policy_type:
                continue

            policy_class = PolicyClass(
                policy_type=policy_type,
                document_name_regex=policy_config.get(X_AWS_IDP_DOCUMENT_NAME_REGEX),
                document_page_content_regex=policy_config.get(
                    X_AWS_IDP_PAGE_CONTENT_REGEX
                ),
            )
            policy_classes.append(policy_class)

        logger.info(f"Loaded {len(policy_classes)} policy class(es)")
        return policy_classes

    def _has_any_regex_configured(self) -> bool:
        """Check if any policy class has any regex pattern configured."""
        for pc in self.policy_classes:
            if pc.document_name_regex or pc.document_page_content_regex:
                return True
        return False

    def _has_page_content_regex_configured(self) -> bool:
        """Check if any policy class has page content regex configured."""
        return any(pc._compiled_content_regex for pc in self.policy_classes)

    def _check_document_name_regex(self, document_id: str) -> List[str]:
        """Check if document name matches any policy class regex. Returns all matches."""
        matched = []
        if not document_id:
            logger.debug("No document ID provided for name regex check")
            return matched

        policies_with_name_regex = [
            pc for pc in self.policy_classes if pc._compiled_name_regex
        ]

        if not policies_with_name_regex:
            logger.info(
                "Document name regex: Not configured on any policy class - skipping"
            )
            return matched

        logger.info(
            f"Document name regex: Checking '{document_id}' against {len(policies_with_name_regex)} pattern(s)"
        )

        for policy_class in policies_with_name_regex:
            pattern = policy_class.document_name_regex
            if policy_class._compiled_name_regex.search(document_id):
                logger.info(
                    f"MATCH: '{document_id}' matched pattern '{pattern}' -> policy '{policy_class.policy_type}'"
                )
                matched.append(policy_class.policy_type)
            else:
                logger.debug(f"No match: '{document_id}' vs pattern '{pattern}'")

        logger.info(f"Document name regex: {len(matched)} match(es)")
        return matched

    def _check_page_content_regex(
        self, page_text: str, page_id: str, already_matched: List[str]
    ) -> List[str]:
        """Check if page content matches any policy class regex. Returns new matches."""
        matched = []
        if not page_text:
            return matched

        for policy_class in self.policy_classes:
            if policy_class.policy_type in already_matched:
                continue
            if not policy_class._compiled_content_regex:
                continue
            pattern = policy_class.document_page_content_regex
            if policy_class._compiled_content_regex.search(page_text):
                logger.info(
                    f"MATCH: Page {page_id} matched pattern '{pattern}' -> policy '{policy_class.policy_type}'"
                )
                matched.append(policy_class.policy_type)
        return matched

    def classify_document(self, document: Document) -> PolicyClassificationResult:
        """
        Classify which policy classes apply to a document.

        Logic:
        - Single policy class: Regex optional, proceed with that policy class
        - Multiple policy classes: Regex required, skip if not configured or no match
        """
        if not self.policy_classes:
            logger.info("No policy classes configured")
            return PolicyClassificationResult(matched_policy_types=[])

        # Single policy class - regex is optional
        if len(self.policy_classes) == 1:
            single_policy = self.policy_classes[0]
            logger.info(f"Single policy class: '{single_policy.policy_type}'")

            if self._has_any_regex_configured():
                logger.info("Regex configured - checking for match")
                result = self._run_regex_checks(document)
                if result.matched_policy_types:
                    return result
                logger.info(
                    "Regex did not match, but single policy class - proceeding anyway"
                )
            else:
                logger.info("No regex configured - proceeding with single policy class")

            return PolicyClassificationResult(
                matched_policy_types=[single_policy.policy_type],
                matched_page_ids={},
            )

        # Multiple policy classes - regex is required
        logger.info(f"Multiple policy classes: {len(self.policy_classes)}")

        if not self._has_any_regex_configured():
            logger.warning(
                "Multiple policy classes but no regex configured. "
                "Configure x-aws-idp-document-name-regex or x-aws-idp-page-content-regex to filter."
            )
            return PolicyClassificationResult(matched_policy_types=[])

        return self._run_regex_checks(document)

    def _run_regex_checks(self, document: Document) -> PolicyClassificationResult:
        """Run document name and page content regex checks."""
        matched_policy_types = []
        matched_page_ids = {}

        has_doc_name_regex = any(pc._compiled_name_regex for pc in self.policy_classes)
        has_page_content_regex = self._has_page_content_regex_configured()

        # Step 1: Document name regex (only if configured)
        if has_doc_name_regex:
            logger.info("--- Step 1: Document Name Regex ---")
            matched_policy_types.extend(self._check_document_name_regex(document.id))
        else:
            logger.info(
                "--- Step 1: Document Name Regex - SKIPPED (not configured) ---"
            )

        # Step 2: Page content regex (only if configured AND not all policies already matched)
        if has_page_content_regex:
            # Check if all policy classes with page content regex are already matched
            policies_needing_page_check = [
                pc
                for pc in self.policy_classes
                if pc._compiled_content_regex
                and pc.policy_type not in matched_policy_types
            ]

            if not policies_needing_page_check:
                logger.info(
                    "--- Step 2: Page Content Regex - SKIPPED (all policies already matched) ---"
                )
            else:
                logger.info(
                    f"--- Step 2: Page Content Regex ({len(policies_needing_page_check)} policy class(es) to check) ---"
                )
                sorted_page_ids = sorted(
                    document.pages.keys(),
                    key=lambda x: int(x) if x.isdigit() else float("inf"),
                )

                pages_checked = 0
                for page_id in sorted_page_ids:
                    if (
                        self.max_pages_for_content_check is not None
                        and pages_checked >= self.max_pages_for_content_check
                    ):
                        logger.info(
                            f"Reached max pages limit ({self.max_pages_for_content_check})"
                        )
                        break

                    page = document.pages.get(page_id)
                    if not page or not page.parsed_text_uri:
                        continue

                    try:
                        page_text = s3.get_text_content(page.parsed_text_uri)
                        new_matches = self._check_page_content_regex(
                            page_text, page_id, matched_policy_types
                        )
                        for policy_type in new_matches:
                            matched_page_ids[policy_type] = page_id
                        matched_policy_types.extend(new_matches)
                        pages_checked += 1
                    except Exception as e:
                        logger.warning(f"Failed to read page {page_id}: {e}")
                        # #1101: a page this loop could not read is a page whose
                        # regexes never ran, so a policy type evidenced only on that
                        # page goes unmatched and none of its rules are ever
                        # validated. For a MISSING or unparseable object that is the
                        # right answer — skip it and classify on what is there. For a
                        # transient S3 fault it silently changes the classification,
                        # and re-reading the same key would have worked, so surface
                        # it under the name PolicyClassificationStep retries.
                        reraise_if_transient(
                            e, where=f"policy classification page {page_id}"
                        )

                logger.info(f"Page content regex: Checked {pages_checked} page(s)")
        else:
            logger.info("--- Step 2: Page Content Regex - SKIPPED (not configured) ---")

        # Result
        if matched_policy_types:
            logger.info(
                f"=== RESULT: Matched {len(matched_policy_types)} policy type(s): {matched_policy_types} ==="
            )
        else:
            logger.info("=== RESULT: No policy types matched ===")

        return PolicyClassificationResult(
            matched_policy_types=matched_policy_types,
            matched_page_ids=matched_page_ids,
        )

    def get_all_policy_types(self) -> List[str]:
        """Get all configured policy types."""
        return [pc.policy_type for pc in self.policy_classes]

    def has_regex_patterns(self) -> bool:
        """Check if any policy class has regex patterns configured."""
        for pc in self.policy_classes:
            if pc.document_name_regex or pc.document_page_content_regex:
                return True
        return False

    def create_no_policy_classes_result(self, document: Document) -> Dict[str, Any]:
        """
        Create consolidated result when no policy classes are configured.
        """
        from datetime import datetime

        return {
            "document_id": document.id,
            "overall_status": "NO_POLICY_CLASSES",
            "total_policy_types": 0,
            "message": "No policy classes configured. Define policy classes with rule questions in the configuration to enable rule validation.",
            "rule_summary": {},
            "overall_statistics": {
                "total_rules": 0,
                "pass_count": 0,  # nosec B105 - statistics key, not a password
                "fail_count": 0,
                "information_not_found_count": 0,
                "pass_percentage": 0.0,  # nosec B105 - statistics key, not a password
            },
            "supporting_pages": [],
            "rule_details": {},
            "generated_at": datetime.now().isoformat(),
        }

    def create_no_match_result(self, document: Document) -> Dict[str, Any]:
        """
        Create consolidated result when no policy regex matches.
        """
        from datetime import datetime

        return {
            "document_id": document.id,
            "overall_status": "NO_POLICY_MATCH",
            "total_policy_types": 0,
            "message": f"No matching policy class found for document '{document.id}'. Ensure the document name or page content matches the patterns defined in your policy classes configuration.",
            "rule_summary": {},
            "overall_statistics": {
                "total_rules": 0,
                "pass_count": 0,  # nosec B105 - statistics key, not a password
                "fail_count": 0,
                "information_not_found_count": 0,
                "pass_percentage": 0.0,  # nosec B105 - statistics key, not a password
            },
            "supporting_pages": [],
            "rule_details": {},
            "generated_at": datetime.now().isoformat(),
        }

    def format_skip_result_as_markdown(self, result_data: Dict[str, Any]) -> str:
        """Format skip result as markdown for UI display - matches orchestrator style."""
        doc_id = result_data.get("document_id", "Document")
        status = result_data.get("overall_status", "SKIPPED")
        message = result_data.get("message", "Rule validation was skipped")
        generated_at = result_data.get("generated_at", "N/A")

        # Match orchestrator markdown format
        md = f"""# Rule Validation Summary: {doc_id}

## Status: {status}

{message}

## Overall Statistics

| Metric | Value |
|--------|-------|
| Rules Evaluated (Pass / Fail / Info Not Found) | 0 (0 / 0 / 0) |
| Pass Percentage | 0.0% |

---

*Generated at: {generated_at}*
"""
        return md
