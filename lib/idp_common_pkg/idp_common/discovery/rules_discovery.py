# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Rules Discovery module for extracting rules from policy documents.

This module provides a service for analyzing policy documents and extracting
structured rules that can be used by the RuleValidationService to validate
transactions against those rules.

The extracted rules are stored in the `policy_classes` configuration field,
which is consumed by RuleValidationService at runtime. Each entry carries the
`x-aws-idp-policy-type` discriminator — the only key the runtime and the UI
read. (The LLM emits `x-aws-idp-rule-type`; `to_policy_class` converts it. The
`rule_classes` config key was renamed to `policy_classes` in v0.5.9.)
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional, cast

from idp_common import bedrock, image
from idp_common.bedrock.client import document_blocks_unsupported_reason
from idp_common.config import ConfigurationReader
from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.models import IDPConfig
from idp_common.utils import extract_json_from_text
from idp_common.utils.s3util import S3Util

logger = logging.getLogger(__name__)

# Conditional import for agentic rule discovery (requires Python 3.10+ dependencies).
# If unavailable, the traditional (non-agentic) discovery path still works; the
# agentic path raises an explicit ImportError at call time in _extract_rules_agentic.
try:
    from idp_common.extraction.agentic_idp import structured_output

    AGENTIC_AVAILABLE = True
except ImportError as _agentic_import_error:
    AGENTIC_AVAILABLE = False
    logger.info(
        "Agentic rule discovery unavailable (missing dependencies: %s). "
        "Traditional rule discovery will still work; set discovery.rules.agentic.enabled=False to suppress this message.",
        _agentic_import_error,
    )


def to_policy_class(rule_class: Dict[str, Any], unique_type: str) -> Dict[str, Any]:
    """Reshape a discovered rule class into the persisted ``policy_classes`` shape.

    The LLM emits ``x-aws-idp-rule-type`` and bare ``rule_properties``, but the
    runtime (``PolicyClassificationService``) and the UI's Policy Schema tab both
    key exclusively off ``x-aws-idp-policy-type``, and expect a JSON-Schema-shaped
    object. This is the single place that conversion happens, so the S3 path and
    the local/notebook path cannot drift: previously the reshape was inlined in
    ``_save_rules_to_config``, so ``discovery_rules_from_document_local`` returned
    raw LLM output that silently never matched anything.

    Args:
        rule_class: A discovered rule class as emitted by the LLM.
        unique_type: The (already de-duplicated) policy type name to assign.

    Returns:
        A policy class dict ready to persist under ``config.policy_classes``.
    """
    reshaped_props: Dict[str, Dict[str, Any]] = {}
    for prop_name, prop_def in (rule_class.get("rule_properties") or {}).items():
        if isinstance(prop_def, dict):
            reshaped_props[prop_name] = {
                "type": "string",
                "description": prop_def.get("description", ""),
                **{
                    k: v
                    for k, v in prop_def.items()
                    if k not in ("type", "description")
                },
            }
        else:
            reshaped_props[prop_name] = {
                "type": "string",
                "description": str(prop_def),
            }

    reshaped_class: Dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": unique_type,
        "type": "object",
        "x-aws-idp-policy-type": unique_type,
        "rule_properties": reshaped_props,
    }
    if rule_class.get("description"):
        reshaped_class["description"] = rule_class["description"]
    return reshaped_class


def _is_max_tokens_error(exc: Exception) -> bool:
    """Detect Strands' hard failure on a response truncated at max output tokens.

    Strands raises ``MaxTokensReachedException`` when the model stops with
    ``stopReason == "max_tokens"``, and deliberately lets it bubble up rather
    than wrapping it. Match on type name and message text rather than importing
    the Strands exception type, mirroring ``_is_context_overflow_error`` in
    ``extraction.agentic_idp`` — this module must stay importable when the
    agentic extras are not installed.
    """
    if "maxtokensreached" in type(exc).__name__.lower():
        return True
    msg = str(exc).lower()
    return "max_tokens limit" in msg or "due to max_tokens" in msg


class RulesTruncatedError(Exception):
    """Rule extraction was truncated at the model's output token limit.

    Raised instead of returning a structurally-valid but incomplete ruleset.
    A rule missing from a compliance validation set fails silently — the
    document is validated against a subset of the policy and reported as
    compliant — so a failed job is the better outcome. The message carries the
    remediation (split the document, or raise ``discovery.rules.max_tokens``)
    and reaches the UI via the discovery job's status message.
    """


class RulesDiscovery:
    """
    Discovers and extracts rules from policy documents using LLMs.

    This is a one-time setup process analogous to ClassesDiscovery:
    - ClassesDiscovery: sample document → JSON Schema (field definitions) → config.classes
    - RulesDiscovery: policy document → rule definitions → config.policy_classes

    The extracted policy_classes are then consumed by RuleValidationService
    to validate transaction documents against the discovered rules.

    The two entry points differ in what they do with the result, so note which
    one you are using:

    - ``discovery_rules_from_document`` (S3) **persists** the rules itself, via
      ``_save_rules_to_config`` → ``to_policy_class``. Its return value is for
      reporting (it carries the assigned type names, not the full reshape).
    - ``discovery_rules_from_document_local`` persists nothing, so it returns
      rules already in the ``policy_classes`` shape — keyed on
      ``x-aws-idp-policy-type`` — for the caller to write into a config as-is.

    NOTE: rule validation additionally requires a document-matching regex on
    each policy class once a config holds more than one — discovery does not
    set one, and a save that creates that state emits a warning (see
    ``_warn_if_no_regex``).

    Usage:
        # With S3
        discovery = RulesDiscovery(
            input_bucket="my-bucket",
            input_prefix="policies/ncci-policy.pdf",
        )
        result = discovery.discovery_rules_from_document(
            input_bucket="my-bucket",
            input_prefix="policies/ncci-policy.pdf",
        )

        # Local file (no S3/DynamoDB required)
        discovery = RulesDiscovery(
            input_bucket="unused",
            input_prefix="unused",
            config=my_idp_config,
        )
        result = discovery.discovery_rules_from_document_local(
            file_path="path/to/policy.pdf",
        )
    """

    def __init__(
        self,
        input_bucket: str,
        input_prefix: str,
        region: Optional[str] = None,
        config: Optional[IDPConfig] = None,
        version: Optional[str] = None,
    ):
        """
        Initialize RulesDiscovery.

        Args:
            input_bucket: S3 bucket containing the policy document
            input_prefix: S3 key prefix for the policy document
            region: AWS region (defaults to AWS_REGION env var)
            config: Optional IDPConfig. If not provided, loads from DynamoDB.
            version: Config version to save extracted rules into. Defaults to
                the active version if omitted.
        """
        self.input_bucket = input_bucket
        self.input_prefix = input_prefix
        self.region = region or os.environ.get("AWS_REGION")
        self.version = version
        # Non-fatal advisories raised by the last save (regex-less classes,
        # duplicated rule names). Surfaced in the discovery job's statusMessage.
        self._save_warnings: List[str] = []

        if config is not None:
            self.config = config
            self.config_reader = None
            self.config_manager = None
        else:
            try:
                self.config_reader = ConfigurationReader()
                self.config_manager = ConfigurationManager()
                self.config = cast(
                    IDPConfig,
                    self.config_reader.get_merged_configuration(as_model=True),
                )
            except Exception as e:
                logger.error(f"Failed to load configuration from DynamoDB: {e}")
                raise Exception(f"Failed to load configuration from DynamoDB: {str(e)}")

        # Get rules discovery model configuration
        self.rules_config = self.config.discovery.rules

        # Initialize Bedrock client
        self.bedrock_client = bedrock.BedrockClient(region=self.region)

    def discovery_rules_from_document(
        self, input_bucket: str, input_prefix: str
    ) -> Dict[str, Any]:
        """
        Extract rules from a policy document stored in S3.

        Analyzes the policy document using an LLM and extracts structured rules
        in the rule_classes format consumed by RuleValidationService.

        Args:
            input_bucket: S3 bucket name
            input_prefix: S3 key for the policy document

        Returns:
            Dict with status and extracted rules

        Raises:
            Exception: If rule extraction fails
        """
        logger.info(
            f"Extracting rules from policy document: s3://{input_bucket}/{input_prefix}"
        )

        try:
            file_in_bytes = S3Util.get_bytes(bucket=input_bucket, key=input_prefix)
            file_extension = os.path.splitext(input_prefix)[1].lower()[1:]

            logger.info(f"Document size: {len(file_in_bytes)} bytes")

            extracted_rules = self._extract_rules(file_in_bytes, file_extension)

            if extracted_rules is None:
                raise Exception("Failed to extract rules from document")

            # Override the model-assigned rule type with a filename-derived
            # name so each uploaded document produces a distinguishable policy
            # class in the UI (e.g. "NCCI_Medicare_Policy_ab12cd34").
            derived_name = self._derive_class_name_from_key(input_prefix)
            if derived_name:
                for rc in extracted_rules:
                    rc["x-aws-idp-rule-type"] = derived_name

            # Save to configuration if config_manager is available
            self._save_warnings = []
            if self.config_manager:
                self._save_rules_to_config(extracted_rules)

            # Warnings ride along on the result so the calling Lambda can put
            # them in the job's statusMessage. A CloudWatch WARNING alone does
            # not reach the person who ran the discovery.
            return {
                "status": "SUCCESS",
                "rules": extracted_rules,
                "warnings": list(self._save_warnings),
            }

        except Exception as e:
            logger.error(
                f"Error extracting rules from {input_prefix}: {e}", exc_info=True
            )
            raise Exception(f"Failed to extract rules from {input_prefix}: {str(e)}")

    def discovery_rules_from_document_local(self, file_path: str) -> Dict[str, Any]:
        """
        Extract rules from a local policy document (no S3 required).

        Convenience method for notebook/local usage without needing S3.

        Returns rules in the SAME shape the S3 path persists — i.e. carrying
        ``x-aws-idp-policy-type``, ``$id``, ``$schema`` and ``type: object`` — so
        a caller can write the result straight into ``config.policy_classes`` and
        have it work. This method previously returned raw LLM output keyed on
        ``x-aws-idp-rule-type``, which no consumer reads: rule validation then
        silently never fired, with no error anywhere.

        Args:
            file_path: Local path to the policy document (PDF or image)

        Returns:
            Dict with status and extracted rules, ready to persist under
            ``policy_classes``.
        """
        logger.info(f"Extracting rules from local document: {file_path}")

        try:
            with open(file_path, "rb") as f:
                file_in_bytes = f.read()

            file_extension = os.path.splitext(file_path)[1].lower()[1:]

            logger.info(f"Document size: {len(file_in_bytes)} bytes")

            extracted_rules = self._extract_rules(file_in_bytes, file_extension)

            if extracted_rules is None:
                raise Exception("Failed to extract rules from document")

            # Reshape to the persisted contract. Names are de-duplicated within
            # this batch only; there is no existing config to collide with here.
            seen: set = set()
            reshaped: List[Dict[str, Any]] = []
            for rc in extracted_rules:
                base = (
                    rc.get("x-aws-idp-rule-type")
                    or rc.get("x-aws-idp-policy-type")
                    or "policy_rules"
                )
                unique_type = base
                suffix = 2
                while unique_type in seen:
                    unique_type = f"{base}_{suffix}"
                    suffix += 1
                seen.add(unique_type)
                reshaped.append(to_policy_class(rc, unique_type))

            return {"status": "SUCCESS", "rules": reshaped}

        except Exception as e:
            logger.error(f"Error extracting rules from {file_path}: {e}", exc_info=True)
            raise Exception(f"Failed to extract rules from {file_path}: {str(e)}")

    def _extract_rules(
        self, document_content: bytes, file_extension: str, max_retries: int = 3
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Extract rules from a document with retry logic and validation.

        Supports two modes:
        - **Agentic** (when config.discovery.rules.agentic.enabled=True):
          Uses Strands Agent with structured_output() and Pydantic models for
          self-correcting, schema-enforced rule extraction.
        - **Traditional** (default):
          Uses standard Bedrock invoke_model with JSON parsing and retry logic.

        Args:
            document_content: Raw document bytes
            file_extension: File extension (pdf, png, jpg, etc.)
            max_retries: Maximum retry attempts

        Returns:
            List of validated rule_class dicts, or None on failure
        """
        model_id = self.rules_config.model
        reason = document_blocks_unsupported_reason(model_id)
        if reason:
            raise ValueError(
                f"Model '{model_id}' is not supported for rule discovery: "
                f"{reason}. Rule discovery sends whole-PDF document blocks. "
                "Choose an Anthropic or Nova model."
            )
        system_prompt = (
            self.rules_config.system_prompt
            or "You are an expert in analyzing policy documents and extracting business rules, regulations, and compliance requirements."
        )
        temperature = self.rules_config.temperature
        top_p = self.rules_config.top_p
        max_tokens = self.rules_config.max_tokens

        user_prompt = self.rules_config.task_prompt or self._prompt_rules_discovery()

        logger.info(f"Rules discovery using model: {model_id}")
        logger.debug(f"Rules discovery prompt: {user_prompt[:200]}...")

        # ── Agentic path ──────────────────────────────────────────────
        if self.rules_config.agentic.enabled:
            return self._extract_rules_agentic(
                document_content=document_content,
                file_extension=file_extension,
                model_id=model_id,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=max_tokens,
            )

        # ── Traditional path ──────────────────────────────────────────
        validation_feedback = ""
        for attempt in range(max_retries):
            try:
                retry_prompt = ""
                if attempt > 0 and validation_feedback:
                    retry_prompt = (
                        f"\n\nPREVIOUS ATTEMPT FAILED: {validation_feedback}\n"
                        f"Please fix the issue and generate valid rule definitions.\n\n"
                    )

                full_prompt = f"{retry_prompt}{user_prompt}"

                content = self._create_content_list(
                    prompt=full_prompt,
                    document_content=document_content,
                    file_extension=file_extension,
                )

                response = self.bedrock_client.invoke_model(
                    model_id=model_id,
                    system_prompt=system_prompt,
                    content=content,
                    temperature=temperature,
                    top_p=top_p,
                    max_tokens=max_tokens,
                    context="RulesDiscovery",
                )

                content_text = bedrock.extract_text_from_response(response)
                logger.debug(
                    f"Bedrock response (attempt {attempt + 1}): {content_text[:500]}"
                )

                # Did the model stop because it hit its output-token ceiling? A
                # truncated response USUALLY fails to parse, but not always: a
                # response cut off after rule 60 of 140 that happens to close its
                # braces — or that extract_json_from_text recovers to a valid
                # prefix — passes every structural check below, because those
                # validate shape, not completeness. Persisting that quietly means
                # a compliance ruleset silently missing most of its rules, which
                # is worse than a failed job. Recognize both the Bedrock Converse
                # signal and the OpenAI Responses adapter's mapped status, as
                # assessment/service.py does.
                if self._response_was_truncated(response):
                    partial_count = self._count_rules(
                        self._safe_parse_rules(content_text)
                    )
                    validation_feedback = (
                        "Your previous response was cut off at the output token "
                        "limit, so the rule list was incomplete. Return the "
                        "complete rule set, using more concise rule descriptions "
                        "if necessary so the response fits."
                    )
                    logger.warning(
                        "Rules extraction hit the output token limit on attempt %d "
                        "(~%d rule(s) recovered, ruleset INCOMPLETE). Retrying. If "
                        "this persists, split the policy document by chapter or "
                        "raise discovery.rules.max_tokens.",
                        attempt + 1,
                        partial_count,
                    )
                    if attempt == max_retries - 1:
                        logger.error(
                            "Rules extraction truncated at the output token limit "
                            "on all %d attempts. Refusing to persist a partial "
                            "ruleset — %d of an unknown total rule(s) were "
                            "recovered. Split the policy document into smaller "
                            "uploads or raise discovery.rules.max_tokens.",
                            max_retries,
                            partial_count,
                        )
                        raise RulesTruncatedError(
                            f"The model's response was truncated at its output token "
                            f"limit on all {max_retries} attempts, so the extracted "
                            f"ruleset is incomplete "
                            f"(~{partial_count} rule(s) recovered). "
                            f"A partial ruleset is not saved, because rules missing "
                            f"from a compliance validation set fail silently. Split "
                            f"the policy document into smaller uploads (e.g. by "
                            f"chapter), or raise discovery.rules.max_tokens."
                        )
                    continue

                # Extract JSON from LLM response (handles markdown fencing, preamble text, etc.)
                json_text = extract_json_from_text(content_text)
                parsed = json.loads(json_text)

                # Normalize and validate
                rules = self._normalize_rules_response(parsed)
                is_valid, error_msg = self._validate_rules_response(rules)

                if is_valid:
                    logger.info(
                        f"Successfully extracted {len(rules)} rule classes "
                        f"on attempt {attempt + 1}"
                    )
                    return rules
                else:
                    validation_feedback = error_msg
                    logger.warning(
                        f"Invalid rules on attempt {attempt + 1}: {error_msg}"
                    )
                    if attempt == max_retries - 1:
                        logger.error(
                            f"Failed to generate valid rules after {max_retries} attempts"
                        )
                        return None

            except json.JSONDecodeError as e:
                validation_feedback = f"Invalid JSON format: {str(e)}"
                logger.warning(f"JSON parse error on attempt {attempt + 1}: {e}")
                if attempt == max_retries - 1:
                    logger.error(
                        f"Failed to generate valid JSON after {max_retries} attempts"
                    )
                    return None
            except RulesTruncatedError:
                # Deliberate refusal to persist a knowingly-partial ruleset —
                # must reach the caller, not be flattened into a None return by
                # the catch-all below (which would read as an ordinary failure
                # and lose the actionable remediation in the message).
                raise
            except Exception as e:
                logger.error(f"Error extracting rules on attempt {attempt + 1}: {e}")
                if attempt == max_retries - 1:
                    return None

        return None

    def _extract_rules_agentic(
        self,
        document_content: bytes,
        file_extension: str,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Extract rules using the agentic approach with Strands Agent.

        Uses structured_output() from agentic_idp.py with a Pydantic model
        (RuleDiscoveryOutput) to enforce schema compliance. The agent
        self-corrects via tool-based validation and JSON patches.

        Args:
            document_content: Raw document bytes
            file_extension: File extension (pdf, png, jpg, etc.)
            model_id: Bedrock model ID
            system_prompt: System prompt for the agent
            user_prompt: Task prompt / custom instruction
            max_tokens: Maximum output tokens

        Returns:
            List of validated rule_class dicts, or None on failure
        """
        if not AGENTIC_AVAILABLE:
            raise ImportError(
                "Agentic rule discovery requires Python 3.10+ and strands-agents dependencies. "
                "Install with: pip install -e 'lib/idp_common_pkg[agentic-extraction]' or use agentic.enabled=False"
            )

        from idp_common.discovery.models import RuleDiscoveryOutput

        logger.info("Using agentic rule discovery with structured_output()")

        # Build prompt content blocks for the agent
        content: list[dict[str, Any]] = []

        if file_extension == "pdf":
            content.append(
                {
                    "document": {
                        "format": "pdf",
                        "name": "policy_document",
                        "source": {"bytes": document_content},
                    }
                }
            )
        else:
            content.append(image.prepare_bedrock_image_attachment(document_content))

        content.append({"text": user_prompt})

        message_prompt = {"role": "user", "content": content}

        # structured_output() reads inference params and review agent settings from
        # config.extraction.* — mirror the rules discovery config into a copy so the
        # agent uses the correct temperature, top_p, and review agent settings without
        # modifying agentic_idp.py.
        import copy

        rules_config_override = copy.deepcopy(self.config)
        rules_config_override.extraction.temperature = self.rules_config.temperature
        rules_config_override.extraction.top_p = self.rules_config.top_p
        rules_config_override.extraction.agentic.enabled = True
        rules_config_override.extraction.agentic.review_agent = (
            self.rules_config.agentic.review_agent
        )
        rules_config_override.extraction.agentic.review_agent_model = (
            self.rules_config.agentic.review_agent_model
        )

        try:
            structured_data, response_with_metering = structured_output(
                model_id=model_id,
                data_format=RuleDiscoveryOutput,
                prompt=message_prompt,
                page_images=None,  # PDF sent as document content, not page images
                config=rules_config_override,
                context="RulesDiscovery",
                custom_instruction=user_prompt,
                system_prompt=system_prompt,
            )

            # Convert Pydantic models → list of dicts (existing rule_classes format)
            rules = [
                rc.model_dump(mode="json", by_alias=True)
                for rc in structured_data.rule_classes
            ]

            total_individual_rules = sum(
                len(rc.get("rule_properties", {})) for rc in rules
            )
            logger.info(
                f"Agentic rule discovery extracted {len(rules)} rule classes "
                f"with {total_individual_rules} individual rules"
            )

            # Same completeness concern as the traditional path: schema
            # enforcement proves the SHAPE is right, not that the model finished
            # listing rules. A response cut off at the token ceiling can still
            # deserialize into a valid RuleDiscoveryOutput holding a fraction of
            # the policy's rules. Refuse it rather than persisting silently.
            #
            # NOTE: on this path the primary protection is Strands raising
            # MaxTokensReachedException (translated in the handler below) — it
            # fails hard on stopReason == "max_tokens" before returning. This
            # check is a belt-and-braces fallback: structured_output() currently
            # synthesizes its envelope WITHOUT a stopReason, so it does not fire
            # today, but it will start working if that envelope ever carries the
            # real stop reason. Keeping both means neither a Strands behavior
            # change nor an envelope change can reopen the silent-persist hole.
            if self._response_was_truncated(response_with_metering):
                logger.error(
                    "Agentic rules extraction hit the output token limit — "
                    "%d rule(s) recovered but the ruleset is INCOMPLETE. "
                    "Refusing to persist a partial ruleset.",
                    total_individual_rules,
                )
                raise RulesTruncatedError(
                    f"The model's response was truncated at its output token limit, "
                    f"so the extracted ruleset is incomplete "
                    f"(~{total_individual_rules} rule(s) recovered). A partial ruleset "
                    f"is not saved, because rules missing from a compliance "
                    f"validation set fail silently. Split the policy document into "
                    f"smaller uploads (e.g. by chapter), or raise "
                    f"discovery.rules.max_tokens."
                )

            # Log metering info
            metering = response_with_metering.get("metering", {})
            if metering:
                logger.info(f"Agentic rule discovery metering: {metering}")

            return rules

        except RulesTruncatedError:
            # Already logged with remediation above; don't re-log as a generic
            # agentic failure.
            raise
        except Exception as e:
            # Strands fails hard on truncation, raising MaxTokensReachedException
            # rather than returning a partial result (and special-cases it to
            # bubble up rather than be wrapped). So the agentic path already
            # refuses to persist a partial ruleset — but it surfaces as an opaque
            # "Agent has reached an unrecoverable state" with no mention of what
            # to do about it. Translate it into the same actionable error the
            # traditional path raises, so both paths give the user the same
            # remedies instead of one of them requiring Strands knowledge.
            if _is_max_tokens_error(e):
                logger.error(
                    "Agentic rules extraction hit the output token limit "
                    "(Strands MaxTokensReachedException). Refusing to persist a "
                    "partial ruleset."
                )
                raise RulesTruncatedError(
                    "The model's response was truncated at its output token "
                    "limit, so the extracted ruleset is incomplete. A partial "
                    "ruleset is not saved, because rules missing from a "
                    "compliance validation set fail silently. Split the policy "
                    "document into smaller uploads (e.g. by chapter), or raise "
                    f"discovery.rules.max_tokens. (underlying error: {e})"
                ) from e
            logger.error(f"Agentic rule discovery failed: {e}", exc_info=True)
            raise

    @staticmethod
    def _response_was_truncated(response: Dict[str, Any]) -> bool:
        """Did the model stop because it ran out of output tokens?

        Recognizes the Bedrock Converse signal (``stopReason == "max_tokens"``)
        and the OpenAI Responses adapter's mapped status, so the check holds
        across model families.
        """
        raw = response.get("response", response) if isinstance(response, dict) else None
        stop_reason = raw.get("stopReason") if isinstance(raw, dict) else None
        return stop_reason in ("max_tokens", "max_output_tokens", "incomplete")

    def _safe_parse_rules(self, content_text: str) -> List[Dict[str, Any]]:
        """Best-effort parse of a possibly-truncated response, for reporting only.

        Used to tell the user roughly how many rules were recovered before the
        cutoff. Never raises: the input is expected to be malformed.
        """
        try:
            return self._normalize_rules_response(
                json.loads(extract_json_from_text(content_text))
            )
        except Exception:
            return []

    @staticmethod
    def _count_rules(rules: List[Dict[str, Any]]) -> int:
        """Total individual rules across all rule classes."""
        return sum(len(rc.get("rule_properties") or {}) for rc in rules)

    def _normalize_rules_response(self, model_response: Any) -> List[Dict[str, Any]]:
        """
        Normalize the LLM response into a list of rule_class objects.

        Handles cases where the LLM returns:
        - A list of rule_class objects (ideal)
        - A single rule_class object
        - A wrapper object with a "rule_classes" key

        Args:
            model_response: Raw parsed JSON from LLM

        Returns:
            List of rule_class dicts in the expected format
        """
        if isinstance(model_response, list):
            return model_response
        elif isinstance(model_response, dict):
            # Check if it's a wrapper with rule_classes key
            if "rule_classes" in model_response:
                return model_response["rule_classes"]
            # Single rule_class object
            if "x-aws-idp-rule-type" in model_response:
                return [model_response]
            # Might be wrapped in another key
            for key, value in model_response.items():
                if isinstance(value, list) and len(value) > 0:
                    if isinstance(value[0], dict) and "x-aws-idp-rule-type" in value[0]:
                        return value
            # If we can't find the expected format, wrap the whole response
            logger.warning(
                "LLM response doesn't match expected rule_classes format, "
                "wrapping as single rule class"
            )
            return [model_response]
        else:
            raise ValueError(
                f"Unexpected response type from LLM: {type(model_response)}"
            )

    def _validate_rule_class(self, rule_class: Dict[str, Any]) -> tuple:
        """
        Validate that a rule_class object has the required fields for
        RuleValidationService consumption.

        Required fields:
        - x-aws-idp-rule-type: str (rule category identifier)
        - rule_properties: dict (mapping of rule_id -> {description: str})

        Args:
            rule_class: The rule class dict to validate

        Returns:
            tuple: (is_valid, error_message)
        """
        try:
            if "x-aws-idp-rule-type" not in rule_class:
                return False, "Missing required field: x-aws-idp-rule-type"

            if not isinstance(rule_class.get("x-aws-idp-rule-type"), str):
                return False, "x-aws-idp-rule-type must be a string"

            if "rule_properties" not in rule_class:
                return False, "Missing required field: rule_properties"

            rule_props = rule_class["rule_properties"]
            if not isinstance(rule_props, dict):
                return False, "rule_properties must be an object"

            if len(rule_props) == 0:
                return False, "rule_properties must contain at least one rule"

            # Validate each rule property has a description
            for rule_id, rule_def in rule_props.items():
                if not isinstance(rule_def, dict):
                    return False, f"Rule '{rule_id}' must be an object"
                if "description" not in rule_def:
                    return False, f"Rule '{rule_id}' missing 'description' field"

            return True, ""

        except Exception as e:
            return False, f"Validation error: {str(e)}"

    def _validate_rules_response(self, rules: List[Dict[str, Any]]) -> tuple:
        """
        Validate the complete rules response.

        Args:
            rules: List of rule_class objects

        Returns:
            tuple: (is_valid, error_message)
        """
        if not isinstance(rules, list):
            return False, "Rules response must be a list"

        if len(rules) == 0:
            return False, "Rules response must contain at least one rule class"

        for i, rule_class in enumerate(rules):
            is_valid, error_msg = self._validate_rule_class(rule_class)
            if not is_valid:
                return False, f"Rule class {i}: {error_msg}"

        return True, ""

    @staticmethod
    def _derive_class_name_from_key(s3_key: str, max_chars: int = 20) -> str:
        """
        Build a policy-class name from an S3 key.

        Takes the filename, strips the upload-timestamp prefix (YYYYMMDD_HHMMSS_)
        and the file extension, keeps the first ``max_chars`` characters of the
        remaining stem, sanitizes them to [a-zA-Z0-9_], and appends a random
        8-hex-char suffix so re-uploads of the same document always produce a
        fresh name (the append-only merge logic in ``_save_rules_to_config``
        then appends a new policy class entry per upload).
        """
        import re
        import secrets

        filename = os.path.basename(s3_key or "")
        stem = os.path.splitext(filename)[0]
        stem = re.sub(r"^\d{8}_\d{6}_", "", stem)
        # secrets.token_hex is used for uniqueness across repeated uploads of
        # the same filename, not as a security primitive. random.randint would
        # work equally well here; secrets is used only because it's already
        # imported elsewhere in this package.
        suffix = secrets.token_hex(4)
        if not stem:
            return f"policy_{suffix}"
        truncated = stem[:max_chars]
        sanitized = re.sub(r"[^A-Za-z0-9_]+", "_", truncated).strip("_")
        if not sanitized:
            return f"policy_{suffix}"
        return f"{sanitized}_{suffix}"

    def _save_rules_to_config(self, rules: List[Dict[str, Any]]) -> List[str]:
        """
        Save extracted rules to the configuration as policy_classes.

        Saves to the versioned configuration record ("Config#<version>") so the
        UI's version-aware Policy Schema tab displays them. Merges with
        existing policy_classes by x-aws-idp-rule-type, replacing rules of the
        same type.

        Args:
            rules: List of rule_class objects to save into policy_classes
        """
        version = getattr(self, "version", None)
        if not version:
            for v in self.config_manager.list_config_versions():
                if v.get("isActive"):
                    version = v.get("versionName")
                    break
            if not version:
                version = "default"

        existing_item_raw = self.config_manager.get_configuration(
            "Config", version=version
        )
        existing_item = cast(Optional[IDPConfig], existing_item_raw)

        existing_policy_classes = []
        if existing_item and existing_item.policy_classes:
            existing_policy_classes = list(existing_item.policy_classes)

        # Reshape each discovered rule class into the JSON-Schema-compatible
        # shape the UI's Policy Schema tab expects:
        #   - x-aws-idp-policy-type  (discriminator the UI reads)
        #   - $schema, $id, type: object
        #   - rule_properties[*] must have type:"string" and description
        # Append-only: each upload adds a distinct entry so results accumulate.
        # Disambiguate by suffixing the policy type if it collides.
        existing_types = {
            rc.get("x-aws-idp-policy-type") for rc in existing_policy_classes
        }
        assigned_types: List[str] = []
        for new_rule in rules:
            new_type = (
                new_rule.get("x-aws-idp-rule-type")
                or new_rule.get("x-aws-idp-policy-type")
                or "policy_rules"
            )
            unique_type = new_type
            suffix = 2
            while unique_type in existing_types:
                unique_type = f"{new_type}_{suffix}"
                suffix += 1
            assigned_types.append(unique_type)

            existing_policy_classes.append(to_policy_class(new_rule, unique_type))
            existing_types.add(unique_type)

        if not existing_item:
            existing_item = IDPConfig()

        existing_item.policy_classes = existing_policy_classes
        self.config_manager.save_configuration("Config", existing_item, version=version)

        # Reflect the final assigned types back onto the caller's rules list
        # so downstream consumers (e.g. the discovery Lambda) can surface the
        # real class name (policy_rules_2, etc.) in job status messages.
        for rule, assigned in zip(rules, assigned_types):
            rule["x-aws-idp-policy-type"] = assigned
            rule["x-aws-idp-rule-type"] = assigned

        logger.info(
            f"Saved {len(rules)} rule classes to Config#{version}.policy_classes "
            f"(total: {len(existing_policy_classes)}, assigned: {assigned_types})"
        )

        # Report the two states this save can silently create. Both are checked
        # here, at the job that CREATES them, rather than only at the runtime
        # that suffers them — a WARNING in a downstream Lambda log an hour later
        # is not a signal anyone connects back to a discovery run.
        self._save_warnings = [
            w
            for w in (
                self._warn_if_no_regex(existing_policy_classes, version),
                self._warn_if_rules_duplicated(existing_policy_classes, rules),
            )
            if w
        ]
        return assigned_types

    @staticmethod
    def _warn_if_no_regex(
        policy_classes: List[Dict[str, Any]], version: Optional[str]
    ) -> Optional[str]:
        """Warn about policy classes that cannot be matched once there are ≥2.

        Discovery never sets a regex, and ``PolicyClassificationService``
        requires one as soon as there are ≥2 classes. Two distinct bad states
        follow, and both are silent (see #601):

        1. **No class has a regex** — rule validation fires for NOTHING, so a
           second discovery run switches off the FIRST policy's rules too.
        2. **Some classes have one and some don't** — the regex-less classes are
           simply never evaluated while the others work. This is the likelier
           state *after* a user acts on warning (1): they add a regex to policy
           A, run discovery again, and new class B silently can't fire. Warning
           only on the all-or-nothing case would leave the follow-up
           uncaught — which is the stricter rule the ``config_library`` preset
           gate already enforces, so the two now agree.
        """
        if len(policy_classes) <= 1:
            return None

        def has_regex(pc: Dict[str, Any]) -> bool:
            return bool(
                pc.get("x-aws-idp-document-name-regex")
                or pc.get("x-aws-idp-page-content-regex")
            )

        unmatched = [
            pc.get("x-aws-idp-policy-type") or pc.get("x-aws-idp-rule-type") or "?"
            for pc in policy_classes
            if not has_regex(pc)
        ]
        if not unmatched:
            return None

        total = len(policy_classes)
        if len(unmatched) == total:
            message = (
                f"Config#{version} now has {total} policy classes and NONE has a "
                f"document-matching regex. Rule validation requires a regex when "
                f"multiple policy classes exist, so it will NOT fire for ANY of "
                f"them until you add one. Set a Document Name Regex (or Page "
                f"Content Regex) on each policy class in Configuration → Policy "
                f"Schema."
            )
        else:
            shown = ", ".join(sorted(unmatched)[:10])
            more = f" (and {len(unmatched) - 10} more)" if len(unmatched) > 10 else ""
            message = (
                f"Config#{version} has {total} policy classes, and {len(unmatched)} "
                f"of them have no document-matching regex: {shown}{more}. With "
                f"multiple policy classes only regex-matched classes are "
                f"evaluated, so THESE classes' rules will never fire while the "
                f"others do. Add a Document Name Regex (or Page Content Regex) to "
                f"each in Configuration → Policy Schema."
            )
        logger.warning(message)
        return message

    @staticmethod
    def _warn_if_rules_duplicated(
        policy_classes: List[Dict[str, Any]], newly_added: List[Dict[str, Any]]
    ) -> Optional[str]:
        """Warn when incoming rule names already exist in other policy classes.

        A rule duplicated across policies is answered by an LLM once per copy for
        every document, forever — a standing per-document cost multiplier — and
        the differently-worded copies can return contradictory Pass/Fail verdicts
        with nothing indicating they were the same rule. Name matching (no
        embeddings) catches the common case, since the extraction prompt asks for
        descriptive snake_case identifiers which converge. See #602.
        """
        incoming_names = {
            name for rc in newly_added for name in (rc.get("rule_properties") or {})
        }
        if not incoming_names:
            return None

        newly_added_types = {rc.get("x-aws-idp-policy-type") for rc in newly_added} | {
            rc.get("x-aws-idp-rule-type") for rc in newly_added
        }
        preexisting_names = {
            name
            for rc in policy_classes
            if rc.get("x-aws-idp-policy-type") not in newly_added_types
            for name in (rc.get("rule_properties") or {})
        }

        overlap = sorted(preexisting_names & incoming_names)
        if not overlap:
            return None

        shown = ", ".join(overlap[:10])
        more = f" (and {len(overlap) - 10} more)" if len(overlap) > 10 else ""
        message = (
            f"{len(overlap)} rule name(s) already exist in other policy classes "
            f"and will now be evaluated more than once per document, at full LLM "
            f"cost each, possibly with conflicting answers: {shown}{more}. Remove "
            f"the duplicates in Configuration → Policy Schema if they are the same "
            f"rule."
        )
        logger.warning(message)
        return message

    def _create_content_list(
        self, prompt: str, document_content: bytes, file_extension: str
    ) -> list:
        """
        Create content list for Bedrock API.

        Args:
            prompt: Text prompt
            document_content: Raw document bytes
            file_extension: File extension

        Returns:
            List of content items for Bedrock
        """
        if file_extension == "pdf":
            content = [
                {
                    "document": {
                        "format": "pdf",
                        "name": "policy_document",
                        "source": {"bytes": document_content},
                    }
                },
                {"text": prompt},
            ]
        else:
            image_content = image.prepare_bedrock_image_attachment(document_content)
            content = [
                image_content,
                {"text": prompt},
            ]

        return content

    def _prompt_rules_discovery(self) -> str:
        """Default prompt for extracting rules from a policy document."""
        sample_output = self._sample_rule_output_format()
        return f"""
Analyze this policy document thoroughly, page by page. Extract ALL business rules,
regulations, compliance requirements, and validation criteria contained in the document.

For each category of rules found:
1. Identify the rule category/type (e.g., "bundling_rules", "modifier_usage",
   "billing_requirements", "eligibility_criteria", etc.)
2. Extract each individual rule as a clear, actionable statement
3. Include specific codes, numbers, thresholds, or conditions mentioned
4. Note any exceptions or special cases for each rule
5. Reference the page number where each rule was found

IMPORTANT GUIDELINES:
- Extract rules VERBATIM from the document where possible
- Each rule should be a self-contained, evaluable statement
- Rules should be specific enough to validate a transaction against
- Group related rules under meaningful category names
- Use snake_case for category names (x-aws-idp-rule-type)
- Each rule's description should be phrased as a validation question or checkable statement
- Do NOT interpret or infer rules not explicitly stated in the document
- Process ALL pages of the document

The output must be a JSON array of rule class objects.
Each rule class represents a category of rules with individual rules as properties.

Return the extracted rules in the exact JSON format below:
{sample_output}
"""

    def _sample_rule_output_format(self) -> str:
        """
        Sample output format that matches what RuleValidationService expects.

        RuleValidationService reads:
        - x-aws-idp-rule-type: to get rule categories
        - rule_properties[*].description: to get individual rule questions
        """
        return """
[
    {
        "x-aws-idp-rule-type": "billing_rules",
        "description": "Rules governing billing and coding procedures",
        "rule_properties": {
            "rule_1": {
                "description": "Is the CPT code within the valid range (30000-39999) for respiratory procedures?",
                "page": "V-3"
            },
            "rule_2": {
                "description": "Are bundled services (exploration, closure, anesthesia) NOT reported separately from the primary procedure?",
                "page": "V-5"
            },
            "rule_3": {
                "description": "Is modifier 59 used only when procedures are performed at distinctly different anatomic sites?",
                "page": "V-8"
            }
        }
    },
    {
        "x-aws-idp-rule-type": "eligibility_rules",
        "description": "Rules for determining patient or service eligibility",
        "rule_properties": {
            "rule_1": {
                "description": "Does the patient meet the minimum age requirement of 18 years for this procedure?",
                "page": "III-2"
            },
            "rule_2": {
                "description": "Has prior authorization been obtained for procedures requiring pre-approval?",
                "page": "III-4"
            }
        }
    },
    {
        "x-aws-idp-rule-type": "documentation_requirements",
        "description": "Required documentation for compliance",
        "rule_properties": {
            "rule_1": {
                "description": "Is medical necessity documentation included to support the procedure performed?",
                "page": "IV-1"
            }
        }
    }
]
"""
