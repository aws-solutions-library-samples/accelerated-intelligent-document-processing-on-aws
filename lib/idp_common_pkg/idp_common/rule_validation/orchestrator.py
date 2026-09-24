# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Rule validation summarization service extracted from existing service.py.
Contains only existing summarization methods, no new functionality.
"""

import json
import logging
import numbers
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from idp_common import bedrock, s3, utils
from idp_common.models import Document, RuleValidationResult
from idp_common.rule_validation.concurrency import resolve_semaphore
from idp_common.rule_validation.models import LLMResponse
from idp_common.utils.transient_errors import reraise_if_transient

logger = logging.getLogger(__name__)

#: Sort key for a page reference that is not a decimal integer. Non-numeric
#: references sort after every numeric one, and among themselves by their own text,
#: so the order is total and does not depend on set iteration order.
_NON_NUMERIC_PAGE_RANK = 1
_NUMERIC_PAGE_RANK = 0


def _normalize_page_reference(page: Any) -> Optional[Tuple[str, Tuple[int, int, str]]]:
    """Canonicalise one ``supporting_pages`` element, or reject it.

    Returns ``(canonical_text, sort_key)``, or ``None`` for a value that is not a
    page reference at all.

    Page references reach the consolidated summary from two engines. The solver
    path builds them with ``str(citation).split(",")`` and so always yields ``str``;
    the model path passes a JSON array through unchanged and can yield ``int``. A
    document routing some rules to each therefore mixes the two shapes by
    construction, and a set holding both counts page 1 twice. Canonicalising to
    ``str`` here — once, where the set is populated — is what makes the members
    comparable, and it agrees with ``LLMResponse.supporting_pages``, which is
    declared ``List[str]`` and already coerces its own elements.

    The ordering integer is parsed here as well, rather than in the sort key, for
    two reasons. ``str.isdigit()`` is true for characters ``int()`` refuses —
    ``'²'``, ``'₂'``, ``'②'`` — and a decimal string longer than CPython's
    conversion limit raises even though ``str.isdecimal()`` is true, so the parse
    needs a guard wherever it happens; doing it once at populate time means a
    rejected value is reported next to the response it came from, and leaves the
    sort with nothing left to raise on.

    A value that is not text and not a number (a list, a dict) is **not** silently
    stringified into the report: the raw per-rule list is preserved verbatim under
    ``rule_details[...]["rules"][...]["supporting_pages"]``, so dropping it from the
    aggregate loses nothing a reader cannot recover, whereas ``"{'page': 1}"``
    sitting in a page list is indistinguishable from a real reference.
    """
    if isinstance(page, str):
        text = page.strip()
    elif isinstance(page, bool):
        # bool is an int subclass, so it would otherwise render as "True".
        logger.warning(
            "Ignoring boolean supporting_pages entry %r: not a page reference", page
        )
        return None
    elif isinstance(page, numbers.Number):
        text = str(page).strip()
    else:
        logger.warning(
            "Ignoring supporting_pages entry of type %s (%.80r): not a page "
            "reference. The rule's own supporting_pages is unchanged.",
            type(page).__name__,
            page,
        )
        return None

    if not text:
        return None

    if text.isdecimal():
        try:
            return text, (_NUMERIC_PAGE_RANK, int(text), text)
        except ValueError:
            # A decimal string above sys.get_int_max_str_digits(). Orderable as
            # text, which is all this key is for.
            logger.warning(
                "supporting_pages entry of %d digits is too long to read as a "
                "number; ordering it as text.",
                len(text),
            )

    return text, (_NON_NUMERIC_PAGE_RANK, 0, text)


def is_section_results_key(key: str) -> bool:
    """Is ``key`` one of the per-section rule-validation result objects?

    One predicate rather than one per call site. The loader required both halves —
    ``_responses.json`` **and** ``section_`` — while the section count checked only the
    suffix, so a key the loader skipped was still counted: measured with two keys under
    the right prefix where one lacked ``section_``, one object was read and the count
    came back 2, taking the LLM summarization branch for a single-section document.
    That is the same wrong-branch cost #1143 was about, arriving through a different
    dropped key.

    Not reachable from the pipeline, which always writes
    ``section_<id>_responses.json``. It is reachable through ``section_uris``, which is
    a documented parameter, and a count that disagrees with what was read is worth
    removing rather than documenting.
    """
    return key.endswith("_responses.json") and "section_" in key


class RuleValidationOrchestratorService:
    """Service containing existing summarization methods from service.py."""

    # Declared on the class so the `semaphore` property below resolves on an
    # instance built without __init__ as well: several suites construct one with
    # `__new__` and assign `_semaphore` themselves to pin a limit.
    _semaphore = None
    _semaphore_loop = None

    def __init__(self, config: Dict[str, Any] = None):
        # Convert dict to IDPConfig if needed (same as extraction/service pattern)
        if config is not None and isinstance(config, dict):
            from idp_common.config.models import IDPConfig

            config_model = IDPConfig(**config)
        elif config is None:
            from idp_common.config.models import IDPConfig

            config_model = IDPConfig()
        else:
            config_model = config

        self.config = config_model
        # Initialize token tracking (following extraction/rule validation service pattern)
        self.token_metrics = {}
        # Initialize semaphore for async concurrency control (Pydantic already converted string to int)
        self.semaphore_limit = self.config.rule_validation.semaphore
        self._semaphore = None
        self._semaphore_loop = None

    @property
    def semaphore(self):
        """
        The one semaphore bounding this service's concurrent Bedrock calls.

        Built lazily so it binds to the loop that runs the work, and cached, so
        that the ``async with self.semaphore:`` at both call sites contends a
        single semaphore rather than one per task. See
        :mod:`idp_common.rule_validation.concurrency`, which both rule-validation
        services share.
        """
        self._semaphore, self._semaphore_loop = resolve_semaphore(
            self._semaphore, self._semaphore_loop, lambda: self.semaphore_limit
        )
        return self._semaphore

    def _generate_consolidated_summary(
        self, all_responses: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Generate a consolidated summary from all rule validation responses.

        This method does not raise: the caller writes whatever it returns to S3 as
        the document's compliance report. It therefore keeps a broad ``except``, but
        what that ``except`` returns is the summary built **so far** with the error
        attached, not a summary stripped of its statistics — a report showing zero
        rules is indistinguishable from a document where nothing was evaluated,
        which is what made the page-sort crash in issue #1052 expensive to diagnose.
        Nothing re-raises here, so no ``ProcessingIssue`` is recorded either (the
        ``rule_validation_not_consolidated`` code is attached by the orchestration
        Lambda, and only when the handler itself raises); the surviving statistics,
        the ``error`` field, the banner ``_format_summary_as_markdown`` renders from
        it and a logged traceback are what make an instance visible.
        """
        # Built before the `try` so the failure path below has something to return,
        # and deliberately with nothing in it that can raise: `all_responses` is
        # measured as the first statement *inside* the try, because it is caller
        # input and `len()` of the wrong type must be caught like any other defect
        # here rather than escaping a method that does not raise.
        summary = {
            "document_id": None,  # Will be set when we have access to document
            "overall_status": "COMPLETE",
            "total_policy_types": 0,
            "rule_summary": {},
            "overall_statistics": {
                "total_rules": 0,
                "recommendation_counts": {},
            },
            "supporting_pages": [],
            "rule_details": {},
        }

        # Canonical page text -> sort key, so a page is deduplicated by its
        # canonical form and the ordering is decided once, where the value arrives.
        all_supporting_pages: Dict[str, Tuple[int, int, str]] = {}
        total_rules = 0
        recommendation_counts = {}

        try:
            summary["total_policy_types"] = len(all_responses)

            # Process each policy type
            for policy_type, responses in all_responses.items():
                rule_stats = {
                    "total_rules": 0,
                    "recommendation_counts": {},
                    "rules": [],
                }

                # Handle both single section (list) and multiple section (dict) formats
                if isinstance(responses, list):
                    response_list = responses
                else:
                    # Flatten dictionary format to list
                    response_list = []
                    for rule_responses in responses.values():
                        if isinstance(rule_responses, list):
                            response_list.extend(rule_responses)
                        else:
                            response_list.append(rule_responses)

                # Registered before the per-response loop and mutated in place
                # below, so a failure part way through one policy type's responses
                # still leaves the rules already read in the report rather than
                # dropping the whole policy type.
                summary["rule_details"][policy_type] = rule_stats

                # Process each response
                for response in response_list:
                    # Read the response first, and count it only once it has been
                    # read: incrementing above these four meant a response that
                    # raised here was counted while contributing no recommendation
                    # and no rule, so the report claimed more rules than it
                    # detailed and understated `pass_percentage` against the
                    # inflated denominator.
                    recommendation = response.get("recommendation", "Unknown")
                    rule = response.get("rule", "Unknown rule")
                    supporting_pages = response.get("supporting_pages", [])
                    reasoning = response.get("reasoning", "No reasoning provided")

                    rule_stats["total_rules"] += 1
                    total_rules += 1

                    # Count recommendations dynamically
                    recommendation_counts[recommendation] = (
                        recommendation_counts.get(recommendation, 0) + 1
                    )
                    rule_stats["recommendation_counts"][recommendation] = (
                        rule_stats["recommendation_counts"].get(recommendation, 0) + 1
                    )

                    # Collect supporting pages, canonicalised on the way in.
                    # `supporting_pages` is model output, so its own shape is not
                    # guaranteed either: a bare int is not iterable and a bare
                    # string iterates into characters, and neither should decide
                    # what the rest of this report contains.
                    if isinstance(supporting_pages, (list, tuple, set, frozenset)):
                        page_values = supporting_pages
                    elif not supporting_pages:
                        # None, "", or another empty value: nothing to collect.
                        page_values = []
                    else:
                        logger.warning(
                            "Ignoring supporting_pages of type %s for rule %.80r: "
                            "expected a list of page references.",
                            type(supporting_pages).__name__,
                            rule,
                        )
                        page_values = []

                    for page in page_values:
                        normalized = _normalize_page_reference(page)
                        if normalized is not None:
                            text, sort_key = normalized
                            all_supporting_pages[text] = sort_key

                    # Add rule summary
                    rule_stats["rules"].append(
                        {
                            "rule": rule,
                            "recommendation": recommendation,
                            "supporting_pages": supporting_pages,
                            "reasoning": reasoning,
                        }
                    )

                # Derived counts for this policy type
                self._apply_derived_counts(rule_stats)

                # Create rule summary
                summary["rule_summary"][policy_type] = {
                    "status": "COMPLETE",
                    "total_rules": rule_stats["total_rules"],
                    **rule_stats["recommendation_counts"],
                }

            # Calculate overall statistics
            self._apply_overall_statistics(summary, total_rules, recommendation_counts)

            # Order the pages. Both the canonical text and its ordering key were
            # decided when the value was collected, so nothing here can raise.
            summary["supporting_pages"] = sorted(
                all_supporting_pages, key=all_supporting_pages.__getitem__
            )

            # Add generation timestamp
            summary["generated_at"] = datetime.now().isoformat()

            logger.info(
                f"Generated consolidated summary with {total_rules} total rules across {len(all_responses)} rule types"
            )

            return summary

        except Exception as e:
            # Keep every statistic that was computed before the failure. Returning a
            # five-key stub instead reads exactly like a document on which no rule
            # was ever evaluated, which is the difference between a defect that is
            # noticed and one that is not.
            logger.error(
                f"Error generating consolidated summary: {str(e)}", exc_info=True
            )
            summary["overall_status"] = "ERROR"
            summary["error"] = str(e)
            self._apply_overall_statistics(summary, total_rules, recommendation_counts)
            # A policy type interrupted part way through has counted rules but no
            # derived counts yet, and the markdown formatter reads those with a
            # default of 0 — which would render "5 rules, 0 pass" for a policy type
            # whose counts are right there in recommendation_counts.
            for rule_stats in summary["rule_details"].values():
                self._apply_derived_counts(rule_stats)
            summary["supporting_pages"] = sorted(
                all_supporting_pages, key=all_supporting_pages.__getitem__
            )
            summary["generated_at"] = datetime.now().isoformat()
            return summary

    @staticmethod
    def _apply_derived_counts(statistics: Dict[str, Any]) -> None:
        """Fill the explicit count fields from ``recommendation_counts``.

        Used for both the document-level statistics and each policy type's, and
        called from the success and failure paths alike, so the counts a report
        shows always agree with the responses it actually counted: the denominator
        of ``pass_percentage`` is the number of rules counted, not the number seen.
        """
        counts = statistics.get("recommendation_counts") or {}
        total_rules = statistics.get("total_rules", 0)

        # Explicit count fields, for easier access in the UI
        statistics["pass_count"] = counts.get("Pass", 0)
        statistics["fail_count"] = counts.get("Fail", 0)
        statistics["information_not_found_count"] = counts.get(
            "Information Not Found", 0
        )

        statistics["pass_percentage"] = (
            round((statistics["pass_count"] / total_rules) * 100, 2)
            if total_rules > 0
            else 0.0
        )

    def _apply_overall_statistics(
        self,
        summary: Dict[str, Any],
        total_rules: int,
        recommendation_counts: Dict[str, int],
    ) -> None:
        """Write the document-level counts into ``summary["overall_statistics"]``.

        Shared by the success and failure paths of
        ``_generate_consolidated_summary`` so a report that failed part way through
        still carries the same statistics fields, filled from however many responses
        had been counted.
        """
        statistics = summary["overall_statistics"]
        statistics["total_rules"] = total_rules
        statistics["recommendation_counts"] = recommendation_counts
        self._apply_derived_counts(statistics)

    async def _summarize_responses(
        self, responses: Dict[str, Any], config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        EXISTING METHOD: Summarize validation responses across multiple files.
        Extracted from service.py without changes.
        """
        from idp_common.config.models import IDPConfig

        # Convert dict to Pydantic Config to leverage validators (same as extraction service)
        config_obj = IDPConfig(**config) if isinstance(config, dict) else config
        summary_config = config_obj.rule_validation.rule_validation_orchestrator

        if not summary_config:
            return responses

        try:
            import asyncio

            final_responses = {}

            # Collect all tasks for parallel execution
            tasks = []
            task_metadata = []

            for policy_type, rule_content in responses.items():
                # rule_content is a list of responses, group by rule
                rule_groups = {}
                for response in rule_content:
                    rule = response.get("rule", "unknown")
                    if rule not in rule_groups:
                        rule_groups[rule] = []
                    rule_groups[rule].append(response)

                for rule, rule_responses in rule_groups.items():
                    # Skip rules already resolved by Z3 — they have a final verdict
                    if any(r.get("_z3_validated") for r in rule_responses):
                        if policy_type not in final_responses:
                            final_responses[policy_type] = []
                        final_responses[policy_type].extend(rule_responses)
                        continue

                    # Prepare summary prompt
                    prompt = self._prepare_prompt(
                        summary_config.task_prompt,
                        {
                            "extracted_evidence": json.dumps(rule_responses),
                            "rule": rule,
                            "policy_class": policy_type,
                            "recommendation_options": config_obj.rule_validation.recommendation_options
                            or "",
                        },
                    )

                    # Get model ID from summarization config
                    model_id = summary_config.model

                    logger.info(
                        f"Rule validation summarization using model: {model_id}"
                    )

                    # Create task for parallel execution
                    task = self._summarize_single_rule(
                        model_id=model_id,
                        system_prompt=summary_config.system_prompt,
                        prompt=prompt,
                        temperature=summary_config.temperature,
                        top_p=summary_config.top_p,
                        top_k=summary_config.top_k,
                        max_tokens=summary_config.max_tokens,
                    )
                    tasks.append(task)
                    task_metadata.append({"policy_type": policy_type, "rule": rule})

            # Execute all tasks in parallel with semaphore control
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Organize results by policy_type
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(f"Error in summarization task: {str(result)}")
                    # #1101: `return_exceptions=True` turns a failed rule into an
                    # object in this list, and `continue` drops that rule from
                    # `final_responses` entirely — it gets no verdict and no error,
                    # it simply is not in the consolidated summary. For a transient
                    # fault the rule's answer is recoverable, so surface it.
                    reraise_if_transient(result, where="rule validation summarization")
                    continue

                metadata = task_metadata[i]
                policy_type = metadata["policy_type"]
                rule = metadata["rule"]

                if policy_type not in final_responses:
                    final_responses[policy_type] = []

                if isinstance(result, dict) and result:
                    # Add policy_type and rule to the result
                    result["policy_type"] = policy_type
                    result["rule"] = rule
                    final_responses[policy_type].append(result)

            return final_responses

        except Exception as e:
            logger.error(f"Error in summarization: {str(e)}")
            # #1101: returning `responses` substitutes the raw per-section
            # fact-extraction dicts for the orchestrator's verdicts, which is a
            # plausible degradation for a deterministic fault and a recoverable one
            # for a throttle.
            reraise_if_transient(e, where="rule validation summarization")
            return responses

    async def _summarize_single_rule(
        self,
        model_id: str,
        system_prompt: str,
        prompt: str,
        temperature: float,
        top_p: float = 0.01,
        top_k: float = 20.0,
        max_tokens: int = 4096,
    ) -> Optional[dict]:
        """
        Summarize a single rule with semaphore control.

        Returns None when the model's response cannot be parsed. The caller
        gathers these and skips anything that is not a dict.
        """
        async with self.semaphore:
            response = await self._invoke_model_async(
                model_id=model_id,
                system_prompt=system_prompt,
                content=prompt,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                max_tokens=max_tokens,
                context="RuleValidation",
            )

            # Parse response
            response_text = bedrock.extract_text_from_response(response)
            try:
                # First try to extract from <response> XML tags
                if "<response>" in response_text and "</response>" in response_text:
                    start_idx = response_text.find("<response>") + 10
                    end_idx = response_text.find("</response>")
                    response_text = response_text[start_idx:end_idx].strip()
                # Fall back to ```json format
                elif "```json" in response_text:
                    start_idx = response_text.find("```json") + 7
                    end_idx = response_text.find("```", start_idx)
                    response_text = response_text[start_idx:end_idx].strip()

                summary_dict = json.loads(response_text)
                validated_summary = LLMResponse(**summary_dict)
                return validated_summary.dict()
            except Exception as e:
                logger.error(f"Error parsing summary response: {str(e)}")
                return None

    def _prepare_prompt(
        self,
        template: str,
        substitutions: Dict[str, str],
        required_placeholders: List[str] = None,
    ) -> str:
        """
        Prepare prompt from template by replacing placeholders.
        """
        from idp_common.bedrock import format_prompt

        return format_prompt(template, substitutions, required_placeholders)

    async def _invoke_model_async(
        self,
        model_id: str,
        system_prompt: str,
        content: str,
        temperature: float = 0.0,
        top_k: int = 5,
        top_p: float = 0.1,
        max_tokens: Optional[int] = None,
        context: str = "RuleValidation",
    ) -> Dict[str, Any]:
        """
        Async wrapper for bedrock.invoke_model with metering tracking.
        """
        import asyncio

        from idp_common import utils

        loop = asyncio.get_event_loop()

        response = await loop.run_in_executor(
            None,
            bedrock.invoke_model,
            model_id,
            system_prompt,
            [{"text": content}],
            temperature,
            top_k,
            top_p,
            max_tokens,
            None,
            context,
        )

        # Track metering (following extraction/rule validation service pattern)
        metering = response.get("metering", {})
        self.token_metrics = utils.merge_metering_data(
            self.token_metrics, metering or {}
        )

        return response

    @staticmethod
    def _section_keys_from_uris(
        section_uris: List[str], output_bucket: str
    ) -> List[str]:
        """Convert this run's section URIs to keys in ``output_bucket``.

        A URI naming a DIFFERENT bucket is dropped with a warning rather than
        stripped down to something that happens to parse. Blind prefix-stripping
        would leave ``s3://other/key`` unchanged, and the loader would then read
        ``s3://<output_bucket>/s3://other/key`` -- a miss that looks exactly like a
        section this run never wrote, which is the failure mode this whole change is
        about not having.
        """
        keys: List[str] = []
        for uri in section_uris:
            if not uri.startswith("s3://"):
                # Already a key.
                keys.append(uri)
                continue
            bucket, _, key = uri[len("s3://") :].partition("/")
            if bucket != output_bucket or not key:
                logger.warning(
                    "Skipping section result %s: it does not name a key in the "
                    "document's output bucket %s",
                    uri,
                    output_bucket,
                )
                continue
            keys.append(key)
        return keys

    def load_section_results(
        self,
        document_input_key: str,
        output_bucket: str,
        section_uris: Optional[List[str]] = None,
    ) -> tuple[Dict[str, Any], bool]:
        """
        Load this run's section results from S3.
        Returns: (all_responses, chunking_occurred)

        ``section_uris`` are the objects THIS run wrote, as reported by the
        per-section Map. Pass them. Globbing the prefix instead reads whatever is
        there, and on a reprocessed document that includes the previous run's
        verdicts for the same document — plausible enough to be consolidated and
        acted on, so a rule whose verdict changed from ``Fail`` to ``Pass`` between
        runs can be reported as ``Fail`` (#1143).

        The glob survives for callers that genuinely have no list — a notebook, a
        manual re-consolidation of an existing prefix — so the distinction is
        ``None`` (no list available, read the prefix) versus ``[]`` (this run wrote
        nothing, so there is nothing to consolidate). Those two must not collapse:
        treating an empty list as "fall back to the prefix" would read the previous
        run's objects in a case where this run produced none of its own, which is the
        defect at its worst rather than an edge of it.

        The reachable shape of that case is a **Map over zero sections**. It is not a
        run whose sections all failed: ``ProcessRuleValidationSections`` carries no
        ``Catch`` and no tolerated-failure setting, and neither does
        ``RuleValidationStep`` inside it, so one failed iteration fails the Map and
        ``RuleValidationOrchestration`` never runs at all.
        """
        try:
            if section_uris is None:
                # No caller-supplied list: read the prefix. See the note above for
                # what this cannot distinguish.
                prefix = f"{document_input_key}/rule_validation/sections/"
                pattern = f"{prefix}section_*_responses.json"
                section_files = s3.find_matching_files(output_bucket, pattern)
            else:
                section_files = self._section_keys_from_uris(
                    section_uris, output_bucket
                )

            all_responses = {}
            chunking_occurred = False

            for file_key in section_files:
                if is_section_results_key(file_key):
                    logger.debug(f"Loading section results from: {file_key}")

                    # Load section responses
                    section_responses = s3.get_json_content(
                        f"s3://{output_bucket}/{file_key}"
                    )

                    # Check if chunking occurred in this section
                    if section_responses.get("chunking_occurred", False):
                        chunking_occurred = True

                    # Extract responses from section result structure
                    if "responses" in section_responses:
                        for policy_type, responses in section_responses[
                            "responses"
                        ].items():
                            if policy_type not in all_responses:
                                all_responses[policy_type] = []

                            if isinstance(responses, list):
                                all_responses[policy_type].extend(responses)
                            else:
                                all_responses[policy_type].append(responses)

            logger.info(f"Loaded results from {len(section_files)} section files")
            return all_responses, chunking_occurred

        except Exception as e:
            logger.error(f"Error loading section results: {str(e)}")
            # #1101: an empty mapping here is indistinguishable from "there was
            # nothing to consolidate" — the caller logs exactly that and returns the
            # document unchanged, so a transient S3 fault finishes the document with
            # no rule-validation verdicts at all and no error recorded.
            reraise_if_transient(e, where="rule validation section results")
            return {}, False

    def _get_rule_json_from_config(
        self, rule_id: str, config: Dict[str, Any], policy_type: str = None
    ) -> Optional[Dict[str, Any]]:
        """
        Load RuleJSON from the config's policy_classes (embedded inline).

        Looks for the x-aws-idp-rule-json field on the rule property that
        matches the given rule_id, scoped to the specified policy_type to
        avoid collisions when multiple policy classes share a rule_id.

        Args:
            rule_id: The unique rule identifier
            config: Configuration dictionary containing policy_classes
            policy_type: If provided, only search within this policy class

        Returns:
            Parsed RuleJSON dict, or None if not found.
        """
        from idp_common.config.schema_constants import (
            X_AWS_IDP_RULE_ID,
            X_AWS_IDP_RULE_JSON,
        )

        policy_classes = config.get("policy_classes", [])
        if not policy_classes:
            policy_classes = config.get("rule_validation", {}).get("policy_classes", [])

        for policy_class in policy_classes:
            # Scope to specified policy_type if provided
            if policy_type:
                pc_type = policy_class.get("x-aws-idp-policy-type") or policy_class.get(
                    "x-aws-idp-rule-type"
                )
                if pc_type != policy_type:
                    continue
            rule_properties = policy_class.get("rule_properties", {})
            for prop in rule_properties.values():
                if prop.get(X_AWS_IDP_RULE_ID) == rule_id:
                    rule_json = prop.get(X_AWS_IDP_RULE_JSON)
                    if rule_json and isinstance(rule_json, dict):
                        logger.info(
                            f"Loaded RuleJSON from config for rule_id='{rule_id}'"
                        )
                        return rule_json
                    else:
                        logger.warning(
                            f"Rule rule_id='{rule_id}' has no embedded RuleJSON "
                            f"(x-aws-idp-rule-json is missing or not a dict)"
                        )
                        return None

        logger.warning(f"Rule rule_id='{rule_id}' not found in config policy_classes")
        return None

    def _collect_facts_across_sections(
        self, z3_responses: List[Dict[str, Any]]
    ) -> List[Dict[str, str]]:
        """
        Collect all extracted facts from multiple sections for a Z3 rule.

        Args:
            z3_responses: List of section responses tagged with z3_parameters

        Returns:
            Combined list of extracted_facts from all sections
        """
        all_facts = []
        for response in z3_responses:
            facts = response.get("extracted_facts", [])
            if isinstance(facts, list):
                all_facts.extend(facts)
        return all_facts

    async def _extract_z3_values_from_facts(
        self,
        rule_json_data: Dict[str, Any],
        all_facts: List[Dict[str, str]],
        rule_description: str,
    ) -> Dict[str, Any]:
        """
        Use LLM to extract typed parameter values from collected facts.

        This is the Z3-specific value extraction step in the orchestrator.
        It takes the text facts gathered across all sections and the RuleJSON
        parameter definitions, then asks the LLM to produce typed values
        for each parameter.

        Args:
            rule_json_data: The RuleJSON dict with parameter definitions
            all_facts: Combined extracted facts from all sections
            rule_description: The natural language rule text

        Returns:
            Dict of {param_name: typed_value} ready for Z3 solver
        """
        parameters = rule_json_data.get("parameters", [])

        # Build parameter descriptions
        param_lines = []
        for param in parameters:
            name = param.get("name", "unknown")
            param_type = param.get("type", "String")
            description = param.get("description", "")
            required = param.get("required", True)
            req_str = "REQUIRED" if required else "OPTIONAL"
            param_lines.append(
                f"- {name} (type: {param_type}, {req_str}): {description}"
            )
        parameters_text = "\n".join(param_lines)

        # Format facts as text
        facts_text = json.dumps(all_facts, indent=2)

        system_prompt = (
            "You are a Value Extraction Specialist. Your task is to extract "
            "specific typed parameter values from a set of extracted facts. "
            "These values will be used for formal constraint validation.\n\n"
            "## Guidelines\n"
            "1. Return values in their correct type: Int → integer number, "
            "Real → decimal number, Bool → true/false, String → text\n"
            "2. If a parameter value cannot be determined from the facts, "
            "return null\n"
            "3. Use only information present in the provided facts\n"
            "4. When multiple facts provide conflicting values for the same "
            "parameter, prefer the fact with higher relevance or more specific data"
        )

        task_prompt = (
            "Given the following extracted facts from a document and the "
            "parameter definitions for a rule, extract the typed value for "
            "each parameter.\n\n"
            f"<rule>\n{rule_description}\n</rule>\n\n"
            f"<parameters>\n{parameters_text}\n</parameters>\n\n"
            f"<extracted-facts>\n{facts_text}\n</extracted-facts>\n\n"
            "For each parameter, determine its value from the facts above. "
            "Return the value in its correct type.\n\n"
            "JSON RESPONSE FORMAT:\n"
            "{\n"
            '  "values": {\n'
            '    "parameter_name": <typed value or null>\n'
            "  }\n"
            "}\n\n"
            "CRITICAL: Respond ONLY with the JSON format inside "
            "<response></response> XML tags."
        )

        config_obj = self.config
        cv_config = config_obj.rule_validation.fact_extraction
        model_id = cv_config.model

        async with self.semaphore:
            response = await self._invoke_model_async(
                model_id=model_id,
                system_prompt=system_prompt,
                content=task_prompt,
                temperature=0,
                top_p=0,
                top_k=cv_config.top_k,
                max_tokens=cv_config.max_tokens,
                context="Z3ValueExtraction",
            )

        response_text = bedrock.extract_text_from_response(response)

        try:
            if "<response>" in response_text and "</response>" in response_text:
                start_idx = response_text.find("<response>") + 10
                end_idx = response_text.find("</response>")
                response_text = response_text[start_idx:end_idx].strip()
            elif "```json" in response_text:
                start_idx = response_text.find("```json") + 7
                end_idx = response_text.find("```", start_idx)
                response_text = response_text[start_idx:end_idx].strip()

            response_dict = json.loads(response_text)
            return response_dict.get("values", {})
        except json.JSONDecodeError:
            logger.error(
                f"Failed to parse Z3 value extraction response: {response_text[:200]}"
            )
            return {}

    def _run_z3_validation(
        self,
        rule_json_data: Dict[str, Any],
        extracted_values: Dict[str, Any],
        supporting_pages: List[str] = None,
    ) -> Dict[str, Any]:
        """
        Run Z3 constraint validation with extracted parameter values.

        Uses Z3Validator directly (no LLM translator needed since we already
        have the RuleJSON and extracted parameter values).

        Args:
            rule_json_data: The RuleJSON dict loaded from S3
            extracted_values: Flat dict of {param_name: typed_value} from LLM extraction
            supporting_pages: Page citations collected from the extracted facts

        Returns:
            Z3 validation result dict with recommendation, reasoning, supporting_pages
        """
        from idp_common.rule_validation.z3.models import RuleJSON
        from idp_common.rule_validation.z3.z3_validator import Z3Validator

        try:
            # Parse RuleJSON
            rule_json = RuleJSON.from_dict(rule_json_data)

            # Run Z3 validation directly (no translator/LLM needed)
            timeout_ms = self.config.rule_validation.z3_timeout_ms
            validator = Z3Validator(timeout_ms=timeout_ms)
            result = validator.validate(rule_json, extracted_values)

            # Determine recommendation from result
            if result.passes():
                recommendation = "Pass"
            elif result.fails():
                recommendation = "Fail"
            else:
                recommendation = "Information Not Found"

            # Build reasoning
            param_summary = ", ".join(
                f"{k}={v}" for k, v in extracted_values.items() if v is not None
            )
            if result.passes():
                reasoning = (
                    f"Z3 formal verification: All constraints satisfied. "
                    f"Parameters: {param_summary}"
                )
            elif result.fails():
                details = (
                    getattr(result, "details", None)
                    or getattr(result, "error_message", None)
                    or "constraint unsatisfied"
                )
                reasoning = (
                    f"Z3 formal verification: Constraint violation detected. "
                    f"Parameters: {param_summary}. "
                    f"Details: {details}"
                )
            else:
                reasoning = (
                    f"Z3 formal verification: Unable to determine outcome. "
                    f"Result: {result.outcome}"
                )

            return {
                "recommendation": recommendation,
                "reasoning": reasoning,
                "supporting_pages": supporting_pages or [],
                "_z3_validated": True,
            }

        except Exception as e:
            logger.error(f"Z3 validation failed in orchestrator: {e}")
            return {
                "recommendation": "Information Not Found",
                "reasoning": f"Z3 validation error in orchestrator: {e}",
                "supporting_pages": supporting_pages or [],
                "_z3_validated": True,
                "_z3_error": True,
            }

    async def _process_single_z3_rule(
        self,
        compound_key,
        rule_id: str,
        policy_type: str,
        rule_description: str,
        section_responses: list,
        config: dict,
    ) -> dict:
        """Process a single Z3 rule with error handling. Returns a verdict dict."""
        try:
            logger.info(
                f"Processing Z3 rule_id='{rule_id}' with "
                f"{len(section_responses)} section responses"
            )

            # Collect all extracted facts from all sections
            all_facts = self._collect_facts_across_sections(section_responses)
            logger.info(
                f"Z3 rule_id='{rule_id}': collected {len(all_facts)} facts "
                f"from {len(section_responses)} sections"
            )

            if not all_facts:
                logger.error(
                    f"Z3 rule_id='{rule_id}': no facts extracted from any section. "
                    f"Cannot validate (strict mode)."
                )
                return {
                    "policy_type": policy_type,
                    "rule": rule_description,
                    "recommendation": "Information Not Found",
                    "reasoning": (
                        "Z3 validation error: no facts were extracted from any "
                        "document section for this rule. Ensure the document "
                        "contains relevant data for the rule parameters."
                    ),
                    "supporting_pages": [],
                    "_z3_validated": True,
                }

            # Load RuleJSON from config (embedded inline)
            rule_json_data = self._get_rule_json_from_config(
                rule_id, config, policy_type
            )
            if not rule_json_data:
                logger.error(
                    f"RuleJSON not found for rule_id='{rule_id}' in config. "
                    f"Generate RuleJSON in the Config Editor before using Z3 engine."
                )
                return {
                    "policy_type": policy_type,
                    "rule": rule_description,
                    "recommendation": "Information Not Found",
                    "reasoning": (
                        f"Z3 configuration error: RuleJSON (x-aws-idp-rule-json) "
                        f"is missing for rule_id='{rule_id}'. Use the 'Generate "
                        f"RuleJSON' button in the Config Editor to create it."
                    ),
                    "supporting_pages": [],
                    "_z3_validated": True,
                }

            # LLM value extraction — convert facts to typed parameter values
            extracted_values = await self._extract_z3_values_from_facts(
                rule_json_data, all_facts, rule_description
            )
            logger.info(
                f"Z3 rule_id='{rule_id}': LLM extracted values: {extracted_values}"
            )

            # Check if all required parameters have non-null values
            required_params = [
                p
                for p in rule_json_data.get("parameters", [])
                if p.get("required", True)
            ]
            missing_params = [
                p.get("name")
                for p in required_params
                if extracted_values.get(p.get("name")) is None
            ]

            if missing_params:
                logger.error(
                    f"Z3 rule_id='{rule_id}': missing required parameters "
                    f"{missing_params} after LLM value extraction. "
                    f"Cannot complete Z3 validation (strict mode)."
                )
                return {
                    "policy_type": policy_type,
                    "rule": rule_description,
                    "recommendation": "Information Not Found",
                    "reasoning": (
                        f"Z3 validation incomplete: could not extract values for "
                        f"required parameters {missing_params} from the document. "
                        f"The document may not contain the necessary data."
                    ),
                    "supporting_pages": [],
                    "_z3_validated": True,
                }

            # Collect supporting pages from facts
            supporting_pages = []
            for fact in all_facts:
                citation = fact.get("citation", "")
                if citation:
                    pages = str(citation).split(",")
                    supporting_pages.extend([p.strip() for p in pages if p.strip()])
            supporting_pages = sorted(
                list(set(supporting_pages)),
                key=lambda x: int(x) if x.isdigit() else 0,
            )

            # Run Z3 solver
            verdict = self._run_z3_validation(
                rule_json_data, extracted_values, supporting_pages
            )
            verdict["policy_type"] = policy_type
            verdict["rule"] = rule_description

            logger.info(
                f"Z3 rule_id='{rule_id}' verdict: {verdict.get('recommendation')}"
            )
            return verdict

        except Exception as e:
            logger.error(
                f"Unhandled error processing Z3 rule_id='{rule_id}': {e}",
                exc_info=True,
            )
            # #1101: the `try` above includes `_extract_z3_values_from_facts`, which
            # invokes Bedrock, so a throttle lands here and is answered with the
            # verdict below — one of the configured `recommendation_options`, written
            # to S3 and counted in the summary as though the solver had run.
            # Deterministic faults (an unsolvable rule, missing parameters) keep it.
            reraise_if_transient(e, where=f"rule validation z3 rule '{rule_id}'")
            return {
                "policy_type": policy_type,
                "rule": rule_description,
                "recommendation": "Information Not Found",
                "reasoning": f"Z3 validation error: {e}",
                "supporting_pages": [],
                "_z3_validated": True,
            }

    async def _process_z3_cross_section_rules(
        self,
        all_responses: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Process Z3 rules using collected facts from all sections.

        The orchestrator determines which rules are Z3 by reading the
        policy_classes config (x-aws-idp-validation-engine + x-aws-idp-rule-id),
        NOT from any tagging in the per-section responses.

        Flow:
        1. Build lookup from config: which rules are Z3 and their rule_ids
        2. Match responses by (policy_type, rule_description) against the lookup
        3. For each Z3 rule: collect facts from all sections
        4. Call LLM to extract typed parameter values from the collected facts
        5. If all required parameters have values: run Z3 solver
        6. If incomplete: return "Information Not Found" (strict mode)
        7. Replace matched responses with Z3 verdicts

        Args:
            all_responses: All section responses grouped by policy_type
            config: Configuration dictionary (contains policy_classes)

        Returns:
            Updated all_responses with Z3 rules resolved to Pass/Fail/Info Not Found
        """
        from idp_common.config.schema_constants import (
            VALIDATION_ENGINE_Z3,
            X_AWS_IDP_RULE_ID,
            X_AWS_IDP_VALIDATION_ENGINE,
        )

        # Step 1: Build Z3 rule lookup from config
        # {(policy_type, rule_description): rule_id}
        z3_rule_lookup = {}
        policy_classes = config.get("policy_classes", [])
        if not policy_classes:
            # Try nested under rule_validation
            policy_classes = config.get("rule_validation", {}).get("policy_classes", [])

        for policy_class in policy_classes:
            policy_type = policy_class.get("x-aws-idp-policy-type") or policy_class.get(
                "x-aws-idp-rule-type"
            )
            if not policy_type:
                continue
            rule_properties = policy_class.get("rule_properties", {})
            for prop in rule_properties.values():
                engine = prop.get(X_AWS_IDP_VALIDATION_ENGINE, "llm")
                rule_id = prop.get(X_AWS_IDP_RULE_ID)
                description = prop.get("description")
                if engine == VALIDATION_ENGINE_Z3 and rule_id and description:
                    z3_rule_lookup[(policy_type, description)] = rule_id

        if not z3_rule_lookup:
            logger.debug("No Z3 rules found in config")
            return all_responses

        logger.info(
            f"Found {len(z3_rule_lookup)} Z3 rules in config: "
            f"{list(z3_rule_lookup.values())}"
        )

        # Step 2: Match responses against the Z3 lookup
        # Group by (policy_type, rule_id) to avoid collisions across policy types
        z3_rules_found = {}  # {(policy_type, rule_id): {"policy_type": ..., "rule": ..., "responses": [...]}}

        for policy_type, responses in all_responses.items():
            if not isinstance(responses, list):
                continue
            for response in responses:
                rule_text = response.get("rule", "")
                key = (policy_type, rule_text)
                if key in z3_rule_lookup:
                    rule_id = z3_rule_lookup[key]
                    compound_key = (policy_type, rule_id)
                    if compound_key not in z3_rules_found:
                        z3_rules_found[compound_key] = {
                            "policy_type": policy_type,
                            "rule": rule_text,
                            "rule_id": rule_id,
                            "responses": [],
                        }
                    z3_rules_found[compound_key]["responses"].append(response)

        if not z3_rules_found:
            logger.debug(
                "No Z3 rule responses matched in section results "
                "(rules may not have been processed yet)"
            )
            return all_responses

        logger.info(
            f"Matched {len(z3_rules_found)} Z3 rules in section results: "
            f"{list(z3_rules_found.keys())}"
        )

        # Step 3: Process each Z3 rule
        z3_verdicts = {}  # {(policy_type, rule_id): verdict_dict}

        for compound_key, rule_info in z3_rules_found.items():
            policy_type = rule_info["policy_type"]
            rule_id = rule_info["rule_id"]
            rule_description = rule_info["rule"]
            section_responses = rule_info["responses"]

            verdict = await self._process_single_z3_rule(
                compound_key,
                rule_id,
                policy_type,
                rule_description,
                section_responses,
                config,
            )
            z3_verdicts[compound_key] = verdict

        # Step 4: Update all_responses — replace Z3 rule responses with verdicts
        # Build a reverse lookup: (policy_type, rule_text) → rule_id for quick matching
        for policy_type in list(all_responses.keys()):
            responses = all_responses[policy_type]
            if not isinstance(responses, list):
                continue

            updated_responses = []
            z3_verdicts_added = set()  # Track which Z3 verdicts we've already added

            for response in responses:
                rule_text = response.get("rule", "")
                key = (policy_type, rule_text)

                if key in z3_rule_lookup:
                    rule_id = z3_rule_lookup[key]
                    compound_key = (policy_type, rule_id)
                    verdict = z3_verdicts.get(compound_key)

                    if verdict is not None:
                        # Z3 produced a verdict — add it once (skip duplicates
                        # from other sections for the same rule)
                        if compound_key not in z3_verdicts_added:
                            updated_responses.append(verdict)
                            z3_verdicts_added.add(compound_key)
                    else:
                        # Should not happen in strict mode — all paths produce
                        # a verdict. But if it does, hard fail.
                        updated_responses.append(
                            {
                                "policy_type": policy_type,
                                "rule": rule_text,
                                "recommendation": "Information Not Found",
                                "reasoning": "Z3 engine error: no verdict produced.",
                                "supporting_pages": [],
                                "_z3_validated": True,
                            }
                        )
                else:
                    # Regular LLM response — keep as-is
                    updated_responses.append(response)

            all_responses[policy_type] = updated_responses

        return all_responses

    def save_policy_type_responses(
        self, all_responses: Dict[str, Any], document_input_key: str, output_bucket: str
    ) -> List[str]:
        """
        Save responses by policy type to S3.
        """
        output_uris = []

        # Filter out metadata fields, only save actual rule type responses
        metadata_fields = {
            "section_id",
            "chunking_occurred",
            "chunks_created",
            "responses",
        }

        for policy_type, responses in all_responses.items():
            if policy_type not in metadata_fields:
                # Strip internal markers before persisting to S3
                clean_responses = responses
                if isinstance(responses, list):
                    clean_responses = [
                        {k: v for k, v in r.items() if not k.startswith("_")}
                        if isinstance(r, dict)
                        else r
                        for r in responses
                    ]
                output_key = f"{document_input_key}/rule_validation/consolidated/{policy_type}_responses.json"
                output_uri = f"s3://{output_bucket}/{output_key}"

                # Save to S3
                s3.write_content(
                    clean_responses,
                    output_bucket,
                    output_key,
                    content_type="application/json",
                )
                output_uris.append(output_uri)

        return output_uris

    def _format_summary_as_markdown(self, consolidated_summary: Dict[str, Any]) -> str:
        """
        Format consolidated summary as markdown with Table of Contents and improved table styling.
        """
        md_parts = []

        # Add CSS styling for responsive tables with word wrap and specific column widths
        md_parts.append("""<style>
table {
    width: 100%;
    border-collapse: collapse;
    margin: 16px 0;
}

th, td {
    border: 1px solid #ddd;
    padding: 12px;
    text-align: left;
    vertical-align: top;
    word-wrap: break-word;
    overflow-wrap: break-word;
    white-space: normal;
}

th {
    background-color: #f1f1f1;
    font-weight: bold;
}

tr:nth-child(even) {
    background-color: #f9f9f9;
}

tr:hover {
    background-color: #f5f5f5;
}

/* Use colgroup to define column widths */
.rules-table {
    table-layout: fixed;
}

.rules-table col.rule-col {
    width: 18%;
}

.rules-table col.recommendation-col {
    width: 12%;
}

.rules-table col.reasoning-col {
    width: 60%;
}

.rules-table col.pages-col {
    width: 10%;
}
</style>

""")

        # Title
        doc_id = consolidated_summary.get("document_id", "Document")
        md_parts.append(f"# Rule Validation Summary: {doc_id}\n\n")

        # A consolidation that failed part way through still has statistics worth
        # showing, but they describe only the rules counted before the failure. Say
        # so here: this markdown is the report an operator reads, and without the
        # banner a partial count is indistinguishable from a complete one.
        error = consolidated_summary.get("error")
        if error:
            escaped_error = (
                str(error)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("\n", " ")
            )
            md_parts.append(
                "> ⚠️ **Consolidation did not complete.** The statistics below cover "
                "only the rules counted before it failed, so they may be "
                "incomplete. Each policy type's own section is unaffected.\n>\n"
                f"> Reason: {escaped_error}\n\n"
            )

        # Overall Statistics as compact table with color coding
        overall_stats = consolidated_summary.get("overall_statistics", {})
        total = overall_stats.get("total_rules", 0)
        pass_count = overall_stats.get("pass_count", 0)
        fail_count = overall_stats.get("fail_count", 0)
        info_not_found = overall_stats.get("information_not_found_count", 0)

        # Color code the counts: Pass=green, Fail=red, Info Not Found=black
        pass_colored = (
            f'<span style="color: #16ab39; font-weight: bold;">{pass_count}</span>'
        )
        fail_colored = (
            f'<span style="color: #d13212; font-weight: bold;">{fail_count}</span>'
        )
        info_colored = f"{info_not_found}"

        md_parts.append("## Overall Statistics\n\n")
        md_parts.append("| Metric | Value |\n")
        md_parts.append("|--------|-------|\n")
        md_parts.append(
            f"| Rules Evaluated (Pass / Fail / Info Not Found) | {total} ({pass_colored} / {fail_colored} / {info_colored}) |\n"
        )
        md_parts.append(
            f"| Pass Percentage | {overall_stats.get('pass_percentage', 0.0)}% |\n\n"
        )

        # Table of Contents
        rule_details = consolidated_summary.get("rule_details", {})
        if rule_details:
            md_parts.append("## Table of Contents\n\n")
            for idx, policy_type in enumerate(rule_details.keys(), 1):
                formatted_name = policy_type.replace("_", " ").title()
                anchor = policy_type.lower().replace("_", "-")
                md_parts.append(f"{idx}. [{formatted_name}](#{anchor})\n")
            md_parts.append("\n")

        # Rule Details by Type
        for idx, (policy_type, details) in enumerate(rule_details.items(), 1):
            formatted_name = policy_type.replace("_", " ").title()
            anchor = policy_type.lower().replace("_", "-")

            md_parts.append(f'## {idx}. {formatted_name} <a id="{anchor}"></a>\n\n')

            # Rule type statistics as compact table with color coding
            rule_total = details.get("total_rules", 0)
            rule_pass = details.get("pass_count", 0)
            rule_fail = details.get("fail_count", 0)
            rule_info_not_found = details.get("information_not_found_count", 0)

            # Color code the counts: Pass=green, Fail=red, Info Not Found=black
            rule_pass_colored = (
                f'<span style="color: #16ab39; font-weight: bold;">{rule_pass}</span>'
            )
            rule_fail_colored = (
                f'<span style="color: #d13212; font-weight: bold;">{rule_fail}</span>'
            )
            rule_info_colored = f"{rule_info_not_found}"

            md_parts.append("### Summary\n\n")
            md_parts.append("| Metric | Value |\n")
            md_parts.append("|--------|-------|\n")
            md_parts.append(
                f"| Rules Evaluated (Pass / Fail / Info Not Found) | {rule_total} ({rule_pass_colored} / {rule_fail_colored} / {rule_info_colored}) |\n"
            )
            md_parts.append(
                f"| Pass Percentage | {details.get('pass_percentage', 0.0)}% |\n\n"
            )

            # Rule details table with specific column widths using inline styles
            rules = details.get("rules", [])
            if rules:
                md_parts.append("### Rules\n\n")
                md_parts.append(
                    '<table style="width: 100%; border-collapse: collapse; table-layout: fixed;">\n'
                )
                md_parts.append("  <colgroup>\n")
                md_parts.append('    <col style="width: 18%;">\n')
                md_parts.append('    <col style="width: 12%;">\n')
                md_parts.append('    <col style="width: 60%;">\n')
                md_parts.append('    <col style="width: 10%;">\n')
                md_parts.append("  </colgroup>\n")
                md_parts.append("  <thead>\n")
                md_parts.append("    <tr>\n")
                md_parts.append(
                    '      <th style="border: 1px solid #ddd; padding: 12px; background-color: #f1f1f1; font-weight: bold; text-align: left; vertical-align: top; word-wrap: break-word;">Rule</th>\n'
                )
                md_parts.append(
                    '      <th style="border: 1px solid #ddd; padding: 12px; background-color: #f1f1f1; font-weight: bold; text-align: left; vertical-align: top; word-wrap: break-word;">Recommendation</th>\n'
                )
                md_parts.append(
                    '      <th style="border: 1px solid #ddd; padding: 12px; background-color: #f1f1f1; font-weight: bold; text-align: left; vertical-align: top; word-wrap: break-word;">Reasoning</th>\n'
                )
                md_parts.append(
                    '      <th style="border: 1px solid #ddd; padding: 12px; background-color: #f1f1f1; font-weight: bold; text-align: left; vertical-align: top; word-wrap: break-word;">Supporting Pages</th>\n'
                )
                md_parts.append("    </tr>\n")
                md_parts.append("  </thead>\n")
                md_parts.append("  <tbody>\n")

                for rule_item in rules:
                    # Properly escape HTML entities (not markdown pipes)
                    rule = (
                        rule_item.get("rule", "")
                        .replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;")
                        .replace('"', "&quot;")
                    )
                    recommendation = rule_item.get("recommendation", "")
                    pages = (
                        ", ".join(map(str, rule_item.get("supporting_pages", [])))
                        or "N/A"
                    )
                    reasoning = (
                        rule_item.get("reasoning", "")
                        .replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;")
                        .replace('"', "&quot;")
                        .replace("\n", " ")
                    )

                    # Add status emoji
                    if recommendation == "Pass":
                        status_icon = "✅"
                    elif recommendation == "Fail":
                        status_icon = "❌"
                    else:
                        status_icon = "ℹ️"

                    md_parts.append("    <tr>\n")
                    md_parts.append(
                        f'      <td style="border: 1px solid #ddd; padding: 12px; text-align: left; vertical-align: top; word-wrap: break-word; overflow-wrap: break-word; white-space: normal;">{rule}</td>\n'
                    )
                    md_parts.append(
                        f'      <td style="border: 1px solid #ddd; padding: 12px; text-align: left; vertical-align: top; word-wrap: break-word; overflow-wrap: break-word; white-space: normal;">{status_icon} {recommendation}</td>\n'
                    )
                    md_parts.append(
                        f'      <td style="border: 1px solid #ddd; padding: 12px; text-align: left; vertical-align: top; word-wrap: break-word; overflow-wrap: break-word; white-space: normal;">{reasoning}</td>\n'
                    )
                    md_parts.append(
                        f'      <td style="border: 1px solid #ddd; padding: 12px; text-align: left; vertical-align: top; word-wrap: break-word; overflow-wrap: break-word; white-space: normal;">{pages}</td>\n'
                    )
                    md_parts.append("    </tr>\n")

                md_parts.append("  </tbody>\n")
                md_parts.append("</table>\n\n")

            # Back to top link
            md_parts.append("\n[Back to Top](#table-of-contents)\n\n")

            # Section separator (except for last section)
            if idx < len(rule_details):
                md_parts.append("---\n\n")

        # Footer with generation timestamp
        generated_at = consolidated_summary.get("generated_at", "")
        if generated_at:
            md_parts.append(f"\n---\n\n*Report generated at: {generated_at}*\n")

        return "".join(md_parts)

    def save_consolidated_summary(
        self,
        consolidated_summary: Dict[str, Any],
        document_input_key: str,
        output_bucket: str,
    ) -> str:
        """
        Save consolidated summary to S3 in both JSON and Markdown formats.
        Returns the Markdown URI for UI display.
        """
        # Save JSON version (for debugging and programmatic access)
        summary_output_key = f"{document_input_key}/rule_validation/consolidated/consolidated_summary.json"

        s3.write_content(
            consolidated_summary,
            output_bucket,
            summary_output_key,
            content_type="application/json",
        )

        # Generate and save markdown version (for UI display)
        markdown_content = self._format_summary_as_markdown(consolidated_summary)
        markdown_output_key = (
            f"{document_input_key}/rule_validation/consolidated/consolidated_summary.md"
        )
        markdown_output_uri = f"s3://{output_bucket}/{markdown_output_key}"

        s3.write_content(
            markdown_content,
            output_bucket,
            markdown_output_key,
            content_type="text/markdown",
        )

        logger.info("Saved rule validation summary as JSON and Markdown")

        # Return markdown URI (this is what the UI will display)
        return markdown_output_uri

    async def consolidate_and_save_all(
        self,
        document: Document,
        config: Dict[str, Any],
        multiple_sections: bool = None,
        section_uris: Optional[List[str]] = None,
    ) -> Document:
        """
        Complete consolidation workflow: load, merge, summarize, and save all results.

        ``section_uris`` is this run's section output list; see
        :meth:`load_section_results` for why passing it matters.
        """
        try:
            # Resolved ONCE, here, and used for both the load and the section count.
            # `_section_keys_from_uris` logs a warning per URI it drops, so calling it
            # twice reported the same bad URI twice and read as two bad objects.
            # `load_section_results` passes a bare key through unchanged, so handing
            # it keys rather than URIs is idempotent.
            section_keys = (
                None
                if section_uris is None
                else self._section_keys_from_uris(section_uris, document.output_bucket)
            )

            # Load all section results and check if chunking occurred
            all_responses, chunking_occurred = self.load_section_results(
                document.input_key, document.output_bucket, section_keys
            )

            if not all_responses:
                logger.warning("No section results found to consolidate")
                return document

            # Process Z3 cross-section rules: merge parameter values from all
            # sections and run Z3 validation. This must happen BEFORE the LLM
            # orchestrator summarization so that Z3 verdicts are included in
            # the final consolidated results.
            all_responses = await self._process_z3_cross_section_rules(
                all_responses, config
            )

            # Determine if summarization is needed: multiple sections OR chunking
            # occurred. This counts THIS run's sections; the prefix is only listed
            # when the caller supplied no list, for the same reason as in
            # `load_section_results` -- a stale object left by a previous run would
            # otherwise push the count past 1 and route a single-section document
            # through LLM summarization, which is a cost and latency difference on
            # top of the wrong verdicts (#1143).
            if section_keys is None:
                prefix = f"{document.input_key}/rule_validation/sections/"
                pattern = f"{prefix}section_*_responses.json"
                section_files = s3.find_matching_files(document.output_bucket, pattern)
                num_sections = len(
                    [f for f in section_files if is_section_results_key(f)]
                )
            else:
                # Counted from the keys the loader actually READ, not from the raw
                # list. A URI naming another bucket is dropped, so counting the raw
                # list reads one object and reports two — which takes the LLM
                # summarization branch for a single-section document. That is the same
                # defect this change exists to remove, reintroduced on the defensive
                # path.
                num_sections = len(
                    [key for key in section_keys if is_section_results_key(key)]
                )

            needs_summarization = (num_sections > 1) or chunking_occurred

            if not needs_summarization:
                logger.info(
                    f"Single section ({num_sections}) with no chunking - storing results directly"
                )
                # For single section with no chunking, just store the section results directly
                output_uris = self.save_policy_type_responses(
                    all_responses, document.input_key, document.output_bucket
                )

                # Generate basic summary without LLM
                consolidated_summary = self._generate_consolidated_summary(
                    all_responses
                )
                consolidated_summary["document_id"] = document.id
                summary_output_uri = self.save_consolidated_summary(
                    consolidated_summary, document.input_key, document.output_bucket
                )

                # Store consolidated result in document
                document.rule_validation_result = (
                    RuleValidationResult.for_consolidation(
                        document.id, output_uris, summary_output_uri, len(all_responses)
                    )
                )

                # Merge summarization metering into document
                document.metering = utils.merge_metering_data(
                    document.metering, self.token_metrics
                )

                return document

            logger.info(
                f"Summarization needed: {num_sections} sections, chunking_occurred: {chunking_occurred}"
            )

            # Check if we have multiple sections (for LLM summarization)
            if multiple_sections is None:
                # Auto-detect if not provided by Lambda
                total_sections = sum(
                    len(responses) if isinstance(responses, list) else 1
                    for responses in all_responses.values()
                )
                multiple_sections = total_sections > len(
                    all_responses
                )  # More responses than rule types

            # Always run orchestrator if config exists (fact extraction needs orchestrator)
            if config.get("rule_validation", {}).get("rule_validation_orchestrator"):
                logger.info("Running LLM summarization for multiple sections")
                all_responses = await self._summarize_responses(all_responses, config)

            # Save policy type responses
            output_uris = self.save_policy_type_responses(
                all_responses, document.input_key, document.output_bucket
            )

            # Generate and save consolidated summary
            consolidated_summary = self._generate_consolidated_summary(all_responses)
            consolidated_summary["document_id"] = document.id
            summary_output_uri = self.save_consolidated_summary(
                consolidated_summary, document.input_key, document.output_bucket
            )

            logger.info(
                f"Consolidation complete. Saved {len(output_uris)} rule type files and consolidated summary"
            )

            # Calculate sections processed from responses
            sections_processed = (
                max(
                    len(responses) if isinstance(responses, list) else 1
                    for responses in all_responses.values()
                )
                if all_responses
                else 0
            )

            # Preserve matched_policy_types and matched_page_ids from policy classification
            existing_matched_policy_types = None
            existing_matched_page_ids = None
            if document.rule_validation_result:
                existing_matched_policy_types = (
                    document.rule_validation_result.matched_policy_types
                )
                existing_matched_page_ids = (
                    document.rule_validation_result.matched_page_ids
                )

            # Store consolidated result in document
            document.rule_validation_result = RuleValidationResult.for_consolidation(
                document_id=document.id,
                policy_type_uris=output_uris,
                summary_uri=summary_output_uri,
                sections_processed=sections_processed,
            )

            # Restore matched_policy_types and matched_page_ids from policy classification
            if existing_matched_policy_types is not None:
                document.rule_validation_result.matched_policy_types = (
                    existing_matched_policy_types
                )
            if existing_matched_page_ids is not None:
                document.rule_validation_result.matched_page_ids = (
                    existing_matched_page_ids
                )

            # Merge summarization metering into document
            document.metering = utils.merge_metering_data(
                document.metering, self.token_metrics
            )

            return document

        except Exception as e:
            logger.error(f"Error in consolidation workflow: {str(e)}")
            # #1101: this is the swallow that made the orchestration handler's own
            # failure path nearly unreachable. It wraps the whole consolidation —
            # loading every section's results, the cross-section Z3 rules, the
            # summarization LLM calls, both S3 writes — and then returns the document
            # NORMALLY carrying an empty result. So the handler saw success, wrote
            # the document, and returned a success response: a transient Bedrock or
            # S3 fault finished the document with no verdicts, no failed status and
            # no diagnosis anywhere, which is a worse outcome than the failure this
            # issue was raised about.
            reraise_if_transient(e, where="rule validation consolidation")
            # Store error result in document
            document.rule_validation_result = RuleValidationResult.for_consolidation(
                document.id, [], "", 0
            )
            return document

    def consolidate_and_save(
        self,
        document: Document,
        config: Dict[str, Any],
        multiple_sections: bool = None,
        section_uris: Optional[List[str]] = None,
    ) -> Document:
        """
        Synchronous wrapper for consolidate_and_save_all.
        Handles both regular Python scripts and Jupyter notebook environments.

        ``section_uris`` is forwarded unchanged; see :meth:`load_section_results`.
        """
        import asyncio
        import concurrent.futures

        try:
            # Try to get the current event loop
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're in an environment with a running event loop (like Jupyter)
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        asyncio.run,
                        self.consolidate_and_save_all(
                            document,
                            config,
                            multiple_sections,
                            section_uris,
                        ),
                    )
                    return future.result()
            else:
                # Event loop exists but not running, we can use it
                return loop.run_until_complete(
                    self.consolidate_and_save_all(
                        document,
                        config,
                        multiple_sections,
                        section_uris,
                    )
                )
        except RuntimeError:
            # No event loop exists, create a new one
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(
                    self.consolidate_and_save_all(
                        document,
                        config,
                        multiple_sections,
                        section_uris,
                    )
                )
            finally:
                loop.close()
