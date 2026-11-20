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

import contextlib
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, cast

from idp_common import bedrock, image
from idp_common.bedrock.client import document_blocks_unsupported_reason
from idp_common.config import ConfigurationReader
from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.models import IDPConfig
from idp_common.config.schema_constants import (
    X_AWS_IDP_POLICY_GROUP,
    X_AWS_IDP_POLICY_UPLOADS,
)
from idp_common.discovery.discovered_text import clean_rule_classes
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


#: Raised when rules would be saved with no configuration profile named.
MISSING_VERSION_MESSAGE = (
    "Rules discovery needs the configuration profile to save into: pass version=. "
    "There is no fallback to the active profile."
)


#: Longest policy group name accepted, after surrounding whitespace is removed.
POLICY_GROUP_MAX_LENGTH = 64

# A group name starts with a letter or digit and holds letters, digits, spaces,
# underscores, hyphens and dots. It is shown back to the user and stored on the
# policy class, and the class name is derived from it, so the alphabet is kept to
# what survives that derivation recognisably.
_POLICY_GROUP_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]*$")

#: How long one rules-discovery save may hold a profile's save lease before
#: another save may take it over. A save is one configuration read and one write
#: (plus its revision), so this is far above what one needs; it bounds how long a
#: save that died while holding the lease can block the next one.
POLICY_SAVE_LEASE_SECONDS = 300

#: How long a save waits for another save into the same profile to finish.
POLICY_SAVE_LEASE_WAIT_SECONDS = 60


def normalize_policy_group(value: Any) -> Optional[str]:
    """Validate a user-named policy group, returning it normalised or None.

    None, an empty string and whitespace mean "no group": the upload keeps the
    per-upload class naming it has always had. Anything else must be a string of
    at most :data:`POLICY_GROUP_MAX_LENGTH` characters matching
    ``_POLICY_GROUP_PATTERN``; runs of whitespace are collapsed to one space.
    Raises ``ValueError`` naming the rule otherwise. This is the one definition:
    the upload resolver calls it to refuse a bad name before anything is
    uploaded, and :class:`RulesDiscovery` calls it again before saving.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("policyGroup must be a string")
    normalized = " ".join(value.split())
    if not normalized:
        return None
    if len(normalized) > POLICY_GROUP_MAX_LENGTH:
        raise ValueError(
            f"policyGroup must be at most {POLICY_GROUP_MAX_LENGTH} characters"
        )
    if not _POLICY_GROUP_PATTERN.match(normalized):
        raise ValueError(
            "policyGroup must start with a letter or digit and contain only "
            "letters, digits, spaces, underscores, hyphens and dots"
        )
    return normalized


def policy_group_key(group: str) -> str:
    """The identity two group names are compared by: case and spacing ignored.

    ``"NCCI Manual"`` and ``"ncci  manual"`` are the same group, so a chapter
    uploaded with a differently-typed name still lands in the class the earlier
    chapters built rather than silently starting a second one.
    """
    return " ".join(group.split()).casefold()


def policy_group_class_name(group: str) -> str:
    """The policy class name a group's first upload creates.

    Deterministic, unlike ``_derive_class_name_from_key``'s per-upload random
    suffix, so every upload in the group names the same class. The class is found
    again by its stored group (``x-aws-idp-policy-group``), not by this name, so a
    collision with an unrelated class only changes the name the group's class is
    created under (``_2``), never which class later uploads append to.
    """
    sanitized = re.sub(r"[^A-Za-z0-9]+", "_", group).strip("_")
    return sanitized or "policy_group"


#: How a grouped upload changes its group's rules. ``append`` adds them (the
#: default, and the only behaviour before superseding existed); ``replace``
#: makes them the group's whole ruleset; ``supersede`` removes the rules the
#: uploader named and adds the upload's. Rule identity is never inferred.
POLICY_GROUP_MODE_APPEND = "append"
POLICY_GROUP_MODE_REPLACE = "replace"
POLICY_GROUP_MODE_SUPERSEDE = "supersede"
POLICY_GROUP_MODES = (
    POLICY_GROUP_MODE_APPEND,
    POLICY_GROUP_MODE_REPLACE,
    POLICY_GROUP_MODE_SUPERSEDE,
)


class PolicyGroupChangeRefused(ValueError):
    """A grouped upload that cannot be applied to the profile as it stands.

    The group is missing or carried by several classes, a superseded rule is
    not in the group, or a replace or supersede extracted no rules. Raised by
    :func:`check_policy_group_change` and the save. A ``ValueError``, so the
    upload resolver answers it with a 400. It is deterministic, so the
    discovery processor marks the job FAILED and does not hand the message back
    to SQS: a retry would repeat the extraction and could apply a replace or
    supersede the user has already seen fail, once the group changed.
    """


def normalize_policy_group_change(
    policy_group: Optional[str], mode: Any, superseded_rules: Any
) -> "tuple[str, List[str]]":
    """Validate the shape of a group change, returning ``(mode, superseded_rules)``.

    ``policy_group`` is already normalised (see :func:`normalize_policy_group`).
    A missing or empty ``mode`` is ``append``. ``replace`` and ``supersede``
    need a group; ``supersede`` needs at least one rule name and the others take
    none. Rule names are exact (no trimming or case folding, because they are
    matched against the group's rule names exactly) and duplicates are dropped
    in order. Raises ``ValueError`` naming the argument otherwise. Whether the
    group and the names exist is :func:`check_policy_group_change`'s question.
    """
    if mode is None or mode == "":
        mode = POLICY_GROUP_MODE_APPEND
    if not isinstance(mode, str) or mode not in POLICY_GROUP_MODES:
        raise ValueError(
            f"policyGroupMode must be one of {', '.join(POLICY_GROUP_MODES)}"
        )
    if superseded_rules is None:
        superseded_rules = []
    if not isinstance(superseded_rules, list) or not all(
        isinstance(r, str) and r for r in superseded_rules
    ):
        raise ValueError("supersededRules must be a list of rule names")
    names = list(dict.fromkeys(superseded_rules))
    if mode != POLICY_GROUP_MODE_APPEND and not policy_group:
        raise ValueError(f"policyGroupMode '{mode}' needs a policyGroup")
    if mode == POLICY_GROUP_MODE_SUPERSEDE and not names:
        raise ValueError(
            "policyGroupMode 'supersede' needs supersededRules: name the group's "
            "rules this upload supersedes"
        )
    if mode != POLICY_GROUP_MODE_SUPERSEDE and names:
        raise ValueError("supersededRules applies only to policyGroupMode 'supersede'")
    return mode, names


def find_policy_group_class(
    policy_classes: List[Dict[str, Any]], group: str
) -> Optional[Dict[str, Any]]:
    """The one policy class carrying ``group`` (by :func:`policy_group_key`), or None.

    Raises ``ValueError`` when more than one class carries it, because there is
    then no one class an upload into the group could change.
    """
    key = policy_group_key(group)
    matches = [
        pc
        for pc in policy_classes
        if isinstance(pc, dict)
        and isinstance(pc.get(X_AWS_IDP_POLICY_GROUP), str)
        and policy_group_key(pc[X_AWS_IDP_POLICY_GROUP]) == key
    ]
    if len(matches) > 1:
        names = ", ".join(str(pc.get("x-aws-idp-policy-type") or "?") for pc in matches)
        raise PolicyGroupChangeRefused(
            f"Policy group '{group}' is carried by {len(matches)} policy classes "
            f"({names}), so there is no one class to add this upload's rules to. "
            f"Merge them, or remove the group from all but one, in Configuration "
            f"→ Policy Schema, then run the upload again."
        )
    return matches[0] if matches else None


def _rule_names(policy_class: Optional[Dict[str, Any]]) -> List[str]:
    props = (policy_class or {}).get("rule_properties")
    return list(props) if isinstance(props, dict) else []


def check_policy_group_change(
    policy_classes: List[Dict[str, Any]],
    group: Optional[str],
    mode: str,
    superseded_rules: List[str],
    version: str = "",
) -> None:
    """Refuse a ``replace`` or ``supersede`` that has nothing to act on.

    Both need the group's class to exist, and every name in a ``supersede``
    must be a rule the class holds now. A declared supersession that finds no
    target would leave the user believing a contradiction was resolved when it
    was not, so it is an error, never a no-op. The upload resolver calls this
    before anything is uploaded (an HTTP 400), and the save calls it again under
    its lease, because the group can change between the two. Raises
    :class:`PolicyGroupChangeRefused`.
    """
    if mode == POLICY_GROUP_MODE_APPEND or not group:
        return
    where = f" in configuration profile '{version}'" if version else ""
    target = find_policy_group_class(policy_classes, group)
    if target is None:
        raise PolicyGroupChangeRefused(
            f"Policy group '{group}' does not exist{where}, so there are no rules "
            f"to {mode}. Upload with Add to group to create it."
        )
    if mode == POLICY_GROUP_MODE_SUPERSEDE:
        held = set(_rule_names(target))
        missing = [r for r in superseded_rules if r not in held]
        if missing:
            raise PolicyGroupChangeRefused(
                f"Policy group '{group}'{where} has no rule named "
                f"{', '.join(repr(r) for r in missing)}, so nothing would supersede "
                f"it. Choose the superseded rules from the group's current rules."
            )


def find_recorded_upload(
    policy_classes: List[Dict[str, Any]], upload_id: Optional[str]
) -> "Optional[tuple[Dict[str, Any], Dict[str, Any]]]":
    """The ``(policy class, upload entry)`` that already records ``upload_id``.

    A discovery job's id is its upload id, so a job whose save already landed
    finds itself here when SQS delivers its message again; the save is then
    skipped instead of adding the same rules a second time.
    """
    if not upload_id:
        return None
    for pc in policy_classes:
        uploads = pc.get(X_AWS_IDP_POLICY_UPLOADS) if isinstance(pc, dict) else None
        for entry in uploads if isinstance(uploads, list) else []:
            if isinstance(entry, dict) and entry.get("uploadId") == upload_id:
                return pc, entry
    return None


def policy_rule_sources(policy_class: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Which recorded upload supplied each rule of ``policy_class``.

    Replays the class's upload record (``x-aws-idp-policy-uploads``) oldest
    first: an upload's superseded rules stop being attributed to anyone, then its
    own rules are attributed to it. Returns ``{rule name: {"uploadId",
    "document", "savedAt"}}`` for the rules the class holds now; a rule with no
    entry was not added by a recorded upload (added in the Policy Schema tab,
    or saved before uploads were recorded).
    """
    sources: Dict[str, Dict[str, Any]] = {}
    uploads = policy_class.get(X_AWS_IDP_POLICY_UPLOADS)
    for entry in uploads if isinstance(uploads, list) else []:
        if not isinstance(entry, dict):
            continue
        for s in entry.get("superseded") or []:
            if isinstance(s, dict):
                sources.pop(str(s.get("rule")), None)
        source = {k: entry.get(k) for k in ("uploadId", "document", "savedAt")}
        for name in entry.get("rules") or []:
            sources[str(name)] = source
    held = set(_rule_names(policy_class))
    return {name: src for name, src in sources.items() if name in held}


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
    # The persisted shape is cleaned here too, so the local/notebook path that
    # calls this directly gets the same plain text as the S3 path.
    clean_rule_classes([reshaped_class])
    return reshaped_class


def _is_max_tokens_error(exc: Exception) -> bool:
    """Detect Strands' hard failure on a response truncated at max output tokens.

    Thin delegate to :func:`idp_common.utils.bedrock_utils.is_max_tokens_agent_error`,
    which is the single implementation: the agentic extraction loop asks the same
    question for a different reason (salvaging an already-validated extraction, see
    ``extraction.agentic_idp._invoke_agent_for_extraction``) and two copies of a
    matcher drift. Kept as a module-local name because this module's tests and the
    two call sites below already reach for it.
    """
    from idp_common.utils.bedrock_utils import is_max_tokens_agent_error

    return is_max_tokens_agent_error(exc)


def truncation_remedy(max_tokens: Optional[int]) -> str:
    """What a user can do about a ruleset truncated at the output-token limit.

    ``discovery.rules.max_tokens`` ships empty, which requests the selected model's
    maximum output, and a value above that maximum is refused. So "raise
    ``max_tokens``" is an action only for a configuration that set a cap; for every
    other one the call already had the whole output budget the model offers, and
    what is left is a smaller document or a model with a larger output limit.
    """
    split = (
        "Split the policy document into smaller uploads (e.g. by chapter), or set "
        "discovery.rules.model to a model with a larger output limit"
    )
    if max_tokens:
        return (
            f"{split}. discovery.rules.max_tokens is set to {max_tokens}, which caps "
            "the response below the model's maximum; clearing it lets the call use "
            "the model's full output limit."
        )
    return (
        f"{split}. discovery.rules.max_tokens is already empty, so the call "
        "already requested the model's full output limit."
    )


class RulesTruncatedError(Exception):
    """Rule extraction was truncated at the model's output token limit.

    Raised instead of returning a structurally-valid but incomplete ruleset.
    A rule missing from a compliance validation set fails silently — the
    document is validated against a subset of the policy and reported as
    compliant — so a failed job is the better outcome. The message carries the
    remediation (:func:`truncation_remedy`) and reaches the UI via the discovery
    job's status message.
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
        # With S3, saving into a named configuration profile (required: there is
        # no fallback to the active profile)
        discovery = RulesDiscovery(
            input_bucket="my-bucket",
            input_prefix="policies/ncci-policy.pdf",
            version="my-profile",
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
            version: Config version to save extracted rules into. Required
                whenever the rules are saved (that is, when ``config`` is not
                given); there is no fallback to the active version.
        """
        self.input_bucket = input_bucket
        self.input_prefix = input_prefix
        self.region = region or os.environ.get("AWS_REGION")
        self.version = version
        # Non-fatal advisories raised by the last save (regex-less classes,
        # duplicated rule names). Surfaced in the discovery job's statusMessage.
        self._save_warnings: List[str] = []
        # What the last grouped save did to its group: the mode, the rules it
        # added and the rules it superseded (with who supplied them). Surfaced in
        # the discovery job's statusMessage.
        self._group_change: Dict[str, Any] = {}

        if config is not None:
            self.config = config
            self.config_reader = None
            self.config_manager = None
        else:
            try:
                # self.region must reach these: this class SAVES the extracted
                # rules back to the configuration table. Same defect and same fix
                # as ClassesDiscovery.
                self.config_reader = ConfigurationReader(region=self.region)
                self.config_manager = ConfigurationManager(region=self.region)
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
        self,
        input_bucket: str,
        input_prefix: str,
        policy_group: Optional[str] = None,
        policy_group_mode: Optional[str] = None,
        superseded_rules: Optional[List[str]] = None,
        upload_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Extract rules from a policy document stored in S3.

        Analyzes the policy document using an LLM and extracts structured rules
        in the rule_classes format consumed by RuleValidationService.

        Args:
            input_bucket: S3 bucket name
            input_prefix: S3 key for the policy document
            policy_group: Optional user-named policy group. Uploads naming the
                same group accumulate into one policy class: the first creates
                it, later ones append their rules to it (see
                ``_save_rules_to_config``). Without one, each upload creates its
                own class, as it always has.
            policy_group_mode: How a grouped upload changes the group's rules:
                ``append`` (default), ``replace`` or ``supersede``. See
                :data:`POLICY_GROUP_MODES`.
            superseded_rules: For ``supersede``, the exact names of the group's
                rules this upload supersedes.
            upload_id: Identifies this upload in the class's upload record
                (the discovery job id). A fresh id when omitted.

        Returns:
            Dict with status and extracted rules

        Raises:
            Exception: If rule extraction fails
        """
        logger.info(
            f"Extracting rules from policy document: s3://{input_bucket}/{input_prefix}"
        )
        # Refuse before the model runs rather than after: the save is what needs it.
        if self.config_manager and not self.version:
            raise ValueError(MISSING_VERSION_MESSAGE)
        group = normalize_policy_group(policy_group)
        mode, superseded = normalize_policy_group_change(
            group, policy_group_mode, superseded_rules
        )

        try:
            # Before the model runs: a job already saved (a repeated delivery)
            # or a change the profile cannot take costs no extraction. The save
            # checks both again under its lease.
            if self.config_manager and self.version:
                current = self._current_policy_classes(self.version)
                recorded = find_recorded_upload(current, upload_id)
                if recorded:
                    return self._already_saved_result(*recorded)
                if group:
                    check_policy_group_change(
                        current, group, mode, superseded, self.version
                    )

            file_in_bytes = S3Util.get_bytes(bucket=input_bucket, key=input_prefix)
            file_extension = os.path.splitext(input_prefix)[1].lower()[1:]

            logger.info(f"Document size: {len(file_in_bytes)} bytes")

            extracted_rules = self._extract_rules(file_in_bytes, file_extension)

            if extracted_rules is None:
                raise Exception("Failed to extract rules from document")

            # Rule text is saved into the profile, shown in the UI and later read
            # by rule validation, so it is kept to plain text before either.
            clean_rule_classes(extracted_rules)

            # Override the model-assigned rule type with a filename-derived
            # name so each uploaded document produces a distinguishable policy
            # class in the UI (e.g. "NCCI_Medicare_Policy_ab12cd34").
            # A grouped upload is named after its group instead, so every upload
            # in the group names the same class.
            derived_name = (
                policy_group_class_name(group)
                if group
                else self._derive_class_name_from_key(input_prefix)
            )
            if derived_name:
                for rc in extracted_rules:
                    rc["x-aws-idp-rule-type"] = derived_name

            # Save to configuration if config_manager is available
            self._save_warnings = []
            self._group_change = {}
            if self.config_manager:
                self._save_rules_to_config(
                    extracted_rules,
                    policy_group=group,
                    policy_group_mode=mode,
                    superseded_rules=superseded,
                    upload_id=upload_id,
                    document=input_prefix,
                )

            # Warnings ride along on the result so the calling Lambda can put
            # them in the job's statusMessage. A CloudWatch WARNING alone does
            # not reach the person who ran the discovery.
            return {
                "status": "SUCCESS",
                "rules": extracted_rules,
                "warnings": list(self._save_warnings),
                "groupChange": dict(self._group_change),
            }

        except PolicyGroupChangeRefused as e:
            # Kept as its own type, not re-wrapped: the caller must be able to
            # tell a refusal (final) from a failure (worth a retry).
            logger.warning(f"Refusing the policy group change for {input_prefix}: {e}")
            raise
        except Exception as e:
            logger.error(
                f"Error extracting rules from {input_prefix}: {e}", exc_info=True
            )
            raise Exception(f"Failed to extract rules from {input_prefix}: {str(e)}")

    def _current_policy_classes(self, version: str) -> List[Dict[str, Any]]:
        item = cast(
            Optional[IDPConfig],
            self.config_manager.get_configuration("Config", version=version),
        )
        return list(item.policy_classes) if item and item.policy_classes else []

    def _already_saved_result(
        self, policy_class: Dict[str, Any], entry: Dict[str, Any]
    ) -> Dict[str, Any]:
        """The result of a job whose save already landed: what it saved, unchanged."""
        class_name = str(policy_class.get("x-aws-idp-policy-type") or "")
        rules = [str(r) for r in entry.get("rules") or []]
        message = (
            f"This upload's rules were already saved to policy class '{class_name}' "
            f"(upload {entry.get('uploadId')}); a repeated delivery of the job saved "
            f"nothing again."
        )
        logger.warning(message)
        self._save_warnings = [message]
        self._group_change = {
            "mode": entry.get("mode") or POLICY_GROUP_MODE_APPEND,
            "rules": rules,
            "superseded": list(entry.get("superseded") or []),
        }
        return {
            "status": "SUCCESS",
            "rules": [
                {
                    "x-aws-idp-policy-type": class_name,
                    "x-aws-idp-rule-type": class_name,
                    "rule_properties": {r: {} for r in rules},
                }
            ],
            "warnings": [message],
            "groupChange": dict(self._group_change),
            "alreadySaved": True,
        }

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

            # Rule text is saved into the profile, shown in the UI and later read
            # by rule validation, so it is kept to plain text before either.
            clean_rule_classes(extracted_rules)

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
                        "this persists: %s",
                        attempt + 1,
                        partial_count,
                        truncation_remedy(max_tokens),
                    )
                    if attempt == max_retries - 1:
                        logger.error(
                            "Rules extraction truncated at the output token limit "
                            "on all %d attempts. Refusing to persist a partial "
                            "ruleset — %d of an unknown total rule(s) were "
                            "recovered. %s",
                            max_retries,
                            partial_count,
                            truncation_remedy(max_tokens),
                        )
                        raise RulesTruncatedError(
                            f"The model's response was truncated at its output token "
                            f"limit on all {max_retries} attempts, so the extracted "
                            f"ruleset is incomplete "
                            f"(~{partial_count} rule(s) recovered). "
                            f"A partial ruleset is not saved, because rules missing "
                            f"from a compliance validation set fail silently. "
                            f"{truncation_remedy(max_tokens)}"
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
        max_tokens: Optional[int],
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
                    f"validation set fail silently. {truncation_remedy(max_tokens)}"
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
                    "compliance validation set fail silently. "
                    f"{truncation_remedy(max_tokens)} (underlying error: {e})"
                ) from e
            logger.error(f"Agentic rule discovery failed: {e}", exc_info=True)
            raise

    @staticmethod
    def _response_was_truncated(response: Dict[str, Any]) -> bool:
        """Did the model stop because it ran out of output tokens?

        Recognizes the Bedrock Converse signal (``stopReason == "max_tokens"``)
        and the OpenAI Responses adapter's mapped status, so the check holds
        across model families. One matcher
        (``utils.bedrock_utils.is_output_token_truncation``) for the three callers
        that ask this question, so the set of stop reasons cannot drift between
        them.
        """
        from idp_common.utils.bedrock_utils import is_output_token_truncation

        return is_output_token_truncation(response)

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

            # Validate each rule property has a NON-EMPTY description.
            #
            # Truthiness, not presence: ``RuleValidationService._get_rule_metadata``
            # keeps a rule only ``if description``, so a rule emitted as
            # ``{"description": ""}`` satisfied this gate, was persisted, and was
            # then dropped at evaluation time with nothing reporting it — one rule
            # short of the policy, reported as a clean validation. Refusing it here
            # routes the model through the existing retry-with-feedback loop
            # instead, which is the same trade ``RulesTruncatedError`` makes: a
            # failed job beats a silently incomplete compliance ruleset.
            for rule_id, rule_def in rule_props.items():
                if not isinstance(rule_def, dict):
                    return False, f"Rule '{rule_id}' must be an object"
                if not rule_def.get("description"):
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

    @contextlib.contextmanager
    def _policy_save_lease(self, version: str) -> Iterator[None]:
        """Hold the profile's rules-discovery save lease for one read-modify-write.

        ``_save_rules_to_config`` reads the profile, adds to ``policy_classes`` and
        writes the whole profile back, so two discovery jobs saving into one profile
        at once would each write a list missing the other's rules — for two
        chapters of one policy group, a chapter's rules silently gone. The lease is
        one item in the configuration table, created with a conditional put that
        succeeds only when no unexpired lease exists, and deleted on exit only by
        its holder. A save that cannot take it within
        :data:`POLICY_SAVE_LEASE_WAIT_SECONDS` fails the job rather than writing
        unguarded. A holder that died is taken over once its lease is older than
        :data:`POLICY_SAVE_LEASE_SECONDS`.

        It serialises rules-discovery saves only. The profile's other writers do
        not take it — a Policy Schema edit saved from the UI, a classes-discovery
        save, and the upload resolver's Replace clear — so one of those overlapping
        a rules save into the same profile can still overwrite one with the other.
        """
        from botocore.exceptions import ClientError

        table = self.config_manager.table
        key = {"Configuration": f"PolicyDiscoverySaveLease#{version}"}
        token = secrets.token_hex(16)
        deadline = time.monotonic() + POLICY_SAVE_LEASE_WAIT_SECONDS
        delay = 0.05
        while True:
            now = int(time.time())
            try:
                table.put_item(
                    Item={
                        **key,
                        "LeaseToken": token,
                        "LeaseExpiresAt": now + POLICY_SAVE_LEASE_SECONDS,
                    },
                    ConditionExpression=(
                        "attribute_not_exists(Configuration) OR LeaseExpiresAt < :now"
                    ),
                    ExpressionAttributeValues={":now": now},
                )
                break
            except ClientError as e:
                if (
                    e.response.get("Error", {}).get("Code")
                    != "ConditionalCheckFailedException"
                ):
                    raise
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Configuration profile '{version}' has been held by another "
                    f"Policy Discovery save for over {POLICY_SAVE_LEASE_WAIT_SECONDS}s, "
                    f"so this job's rules were not saved. Either another job is still "
                    f"saving, or one stopped while saving and its hold expires "
                    f"{POLICY_SAVE_LEASE_SECONDS}s after it began. Run the upload again."
                )
            time.sleep(delay)
            delay = min(delay * 2, 2.0)
        try:
            yield
        finally:
            try:
                table.delete_item(
                    Key=key,
                    ConditionExpression="LeaseToken = :token",
                    ExpressionAttributeValues={":token": token},
                )
            except ClientError as e:
                # Expired and taken over by another save: that save owns it now.
                logger.warning(f"Could not release the policy save lease: {e}")

    def _save_rules_to_config(
        self,
        rules: List[Dict[str, Any]],
        policy_group: Optional[str] = None,
        policy_group_mode: Optional[str] = None,
        superseded_rules: Optional[List[str]] = None,
        upload_id: Optional[str] = None,
        document: Optional[str] = None,
    ) -> List[str]:
        """
        Save extracted rules to the configuration as policy_classes.

        Saves to the versioned configuration record ("Config#<version>") so the
        UI's version-aware Policy Schema tab displays them.

        Without ``policy_group`` the save is append-only by class: each rule class
        becomes a new policy class, its type name suffixed (``_2``, ...) if it
        collides with an existing one.

        With ``policy_group``, every rule from every rule class goes into the ONE
        policy class carrying that group (``x-aws-idp-policy-group``, compared by
        :func:`policy_group_key`). The first upload creates it. What a later
        upload does to it is ``policy_group_mode``:

        * ``append`` (default): the upload's rules are added; a rule whose name
          the class already holds is added under a suffixed name (``_2``, ...)
          and reported, never merged into or replacing the earlier one.
        * ``replace``: the upload's rules become the class's whole ruleset.
        * ``supersede``: the rules named in ``superseded_rules`` are removed and
          the upload's rules are added; one with a superseded rule's name takes
          that name, others append as above.

        In every mode the class keeps its name, description, regexes and other
        keys. Superseded rules are removed from the active ruleset (earlier
        revisions of the profile still hold them), and the class's upload record
        (``x-aws-idp-policy-uploads``) gains an entry naming this upload, the
        rules it supplied and the rules it superseded with the upload that had
        supplied each.

        Args:
            rules: List of rule_class objects to save into policy_classes
            policy_group: Normalised policy group name, or None
            policy_group_mode: ``append``, ``replace`` or ``supersede``
            superseded_rules: Exact rule names a ``supersede`` removes
            upload_id: This upload's id in the upload record (a fresh one if None)
            document: The uploaded document's key, recorded by file name
        """
        version = getattr(self, "version", None)
        if not version:
            # No fallback to the active profile: that would write discovered rules
            # into whichever profile documents are currently processed under, which
            # is not a profile anyone chose for this run.
            raise ValueError(MISSING_VERSION_MESSAGE)
        group = normalize_policy_group(policy_group)
        mode, superseded = normalize_policy_group_change(
            group, policy_group_mode, superseded_rules
        )
        upload = {
            "uploadId": upload_id or secrets.token_hex(8),
            "document": os.path.basename(document or "") or None,
            "savedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mode": mode,
        }

        with self._policy_save_lease(version):
            return self._save_rules_to_config_locked(
                rules, version, group, mode, superseded, upload
            )

    def _save_rules_to_config_locked(
        self,
        rules: List[Dict[str, Any]],
        version: str,
        group: Optional[str],
        mode: str = POLICY_GROUP_MODE_APPEND,
        superseded: Optional[List[str]] = None,
        upload: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """The read-modify-write of ``_save_rules_to_config``, under its lease."""
        upload = dict(upload or {"mode": mode})
        existing_item_raw = self.config_manager.get_configuration(
            "Config", version=version
        )
        existing_item = cast(Optional[IDPConfig], existing_item_raw)

        existing_policy_classes = []
        if existing_item and existing_item.policy_classes:
            existing_policy_classes = list(existing_item.policy_classes)

        # A delivery of a job whose save already landed writes nothing: without
        # this, SQS redelivering a saved job adds its rules (as `_2` copies) and
        # its record entry a second time.
        # A save without a job id records a fresh random id, which never matches.
        recorded = find_recorded_upload(existing_policy_classes, upload.get("uploadId"))
        if recorded:
            self._already_saved_result(*recorded)
            name = str(recorded[0].get("x-aws-idp-policy-type") or "")
            for rule in rules:
                rule["x-aws-idp-policy-type"] = name
                rule["x-aws-idp-rule-type"] = name
            return [name] * len(rules)

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
        group_warnings: List[str] = []
        self._group_change = {}
        if group:
            # Checked again here, under the lease, because the group can have
            # changed since the upload resolver checked it.
            check_policy_group_change(
                existing_policy_classes, group, mode, superseded or [], version
            )
            assigned_types, group_warnings, self._group_change = self._merge_into_group(
                rules,
                group,
                existing_policy_classes,
                existing_types,
                mode,
                superseded or [],
                upload,
            )
        else:
            assigned_types = []
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

                new_class = to_policy_class(new_rule, unique_type)
                new_class[X_AWS_IDP_POLICY_UPLOADS] = [
                    {**upload, "rules": _rule_names(new_class), "superseded": []}
                ]
                existing_policy_classes.append(new_class)
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
            f"(total: {len(existing_policy_classes)}, assigned: {assigned_types}"
            + (f", policy group: {group!r}, mode: {mode}" if group else "")
            + ")"
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
                *group_warnings,
            )
            if w
        ]
        return assigned_types

    @staticmethod
    def _merge_into_group(
        rules: List[Dict[str, Any]],
        group: str,
        policy_classes: List[Dict[str, Any]],
        existing_types: set,
        mode: str = POLICY_GROUP_MODE_APPEND,
        superseded: Optional[List[str]] = None,
        upload: Optional[Dict[str, Any]] = None,
    ) -> "tuple[List[str], List[str], Dict[str, Any]]":
        """Put every rule in ``rules`` into the group's one policy class.

        Mutates ``policy_classes`` in place: the group's class is updated, or
        created and appended when no class carries the group yet (``append``
        only; :func:`check_policy_group_change` has refused the other modes).
        Returns the class name assigned to each rule class (the same name for
        all of them), any warnings to report, and a summary of the change for
        the job's status: ``{"mode", "rules", "superseded"}``.
        """
        superseded = list(superseded or [])
        upload = dict(upload or {"mode": mode})
        target = find_policy_group_class(policy_classes, group)

        incoming: List[tuple] = []  # (rule name, reshaped rule definition)
        for rc in rules:
            reshaped = to_policy_class(rc, "_")
            incoming.extend((reshaped["rule_properties"]).items())

        if mode != POLICY_GROUP_MODE_APPEND and not incoming:
            # Replacing or superseding with nothing would delete the group's
            # rules on the strength of an extraction that found none, which is
            # far likelier to be a failed extraction than an intended repeal.
            raise PolicyGroupChangeRefused(
                f"This upload produced no rules, so policy group '{group}' was "
                f"left unchanged rather than having rules {mode}d by nothing."
            )

        if target is not None:
            props = target.get("rule_properties")
            if not isinstance(props, dict):
                props = {}
            target["rule_properties"] = props
            class_name = str(
                target.get("x-aws-idp-policy-type")
                or target.get("x-aws-idp-rule-type")
                or target.get("$id")
            )
        else:
            base = policy_group_class_name(group)
            class_name = base
            suffix = 2
            while class_name in existing_types:
                class_name = f"{base}_{suffix}"
                suffix += 1
            first = next((rc for rc in rules if rc.get("description")), None)
            target = to_policy_class(
                {"description": first.get("description")} if first else {},
                class_name,
            )
            target[X_AWS_IDP_POLICY_GROUP] = group
            props = target["rule_properties"]
            policy_classes.append(target)
            existing_types.add(class_name)

        # Who supplied each rule, read before anything is removed, so the record
        # of what this upload superseded says whose rule it was.
        sources = policy_rule_sources(target)
        if mode == POLICY_GROUP_MODE_REPLACE:
            removing = list(props)
        elif mode == POLICY_GROUP_MODE_SUPERSEDE:
            removing = superseded
        else:
            removing = []
        superseded_record = []
        for name in removing:
            props.pop(name)
            src = sources.get(name) or {}
            superseded_record.append(
                {
                    "rule": name,
                    "uploadId": src.get("uploadId"),
                    "document": src.get("document"),
                }
            )

        renamed: List[str] = []
        added: List[str] = []
        for name, definition in incoming:
            unique = name
            suffix = 2
            while unique in props:
                unique = f"{name}_{suffix}"
                suffix += 1
            if unique != name:
                renamed.append(f"{name} → {unique}")
            props[unique] = definition
            added.append(unique)

        uploads = target.get(X_AWS_IDP_POLICY_UPLOADS)
        if not isinstance(uploads, list):
            uploads = []
        uploads.append({**upload, "rules": added, "superseded": superseded_record})
        target[X_AWS_IDP_POLICY_UPLOADS] = uploads

        warnings: List[str] = []
        if renamed:
            shown = ", ".join(renamed[:10])
            more = f" (and {len(renamed) - 10} more)" if len(renamed) > 10 else ""
            message = (
                f"{len(renamed)} rule name(s) from this upload already existed in "
                f"policy group '{group}' and were added alongside the existing rule "
                f"under a new name, not in place of it: {shown}{more}. Each copy is "
                f"evaluated on every document. If they are the same rule, remove one "
                f"in Configuration → Policy Schema, or upload again superseding it."
            )
            logger.warning(message)
            warnings.append(message)
        change = {"mode": mode, "rules": added, "superseded": superseded_record}
        return [class_name] * len(rules), warnings, change

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
