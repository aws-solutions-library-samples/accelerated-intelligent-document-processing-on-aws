# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Utility functions for configuration merging and manipulation.

This module provides utilities for:
- Deep merging of configuration dictionaries
- Loading system defaults from YAML files (packaged within idp_common)
- Merging user configs with system defaults
"""

import logging
import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

from idp_common.bedrock.model_utils import (
    get_model_max_output_tokens,
    resolve_model_id_from_arn,
)

# Use importlib.resources for Python 3.9+
if sys.version_info >= (3, 9):
    from importlib.resources import (
        files as importlib_files,  # nosemgrep: python.lang.compatibility.python37.python37-compatibility-importlib2
    )
else:
    from importlib_resources import files as importlib_files

logger = logging.getLogger(__name__)

# Valid pattern names
VALID_PATTERNS = ["pattern-1", "pattern-2"]

# Feature sets for create-config command
FEATURE_SETS = {
    "min": ["classification", "extraction", "classes"],
    "core": ["ocr", "classification", "extraction", "assessment", "classes"],
    "all": [
        "ocr",
        "classification",
        "extraction",
        "assessment",
        "summarization",
        "criteria_validation",
        "evaluation",
        "discovery",
        "agents",
        "classes",
    ],
}


def deep_update(target: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
    """
    Recursively update target dict with source dict.

    Nested dictionaries are merged recursively. Other values are deep copied
    to avoid mutation issues.

    Args:
        target: Target dictionary to update
        source: Source dictionary with updates

    Returns:
        Updated target dictionary (modified in place)
    """
    for key, value in source.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            deep_update(target[key], value)
        else:
            target[key] = deepcopy(value)
    return target


def apply_delta_with_deletions(
    target: Dict[str, Any], delta: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Apply delta to target with null values treated as deletions.

    This is used for sparse delta updates where:
    - null values in delta mean "delete this field from target"
    - Other values are merged/updated normally
    - Empty parent objects are cleaned up after deletions

    Args:
        target: Target dictionary to update (modified in place)
        delta: Delta dictionary with updates (null = delete)

    Returns:
        Updated target dictionary (modified in place)
    """
    keys_to_delete = []

    for key, value in delta.items():
        if value is None:
            # null means delete this key
            keys_to_delete.append(key)
        elif (
            isinstance(value, dict) and key in target and isinstance(target[key], dict)
        ):
            # Recurse into nested dicts
            apply_delta_with_deletions(target[key], value)
            # Clean up empty parent after deletions
            if len(target[key]) == 0:
                keys_to_delete.append(key)
        else:
            # Normal update
            target[key] = deepcopy(value)

    # Delete keys marked for deletion
    for key in keys_to_delete:
        if key in target:
            del target[key]

    return target


def strip_matching_defaults(
    custom_dict: Dict[str, Any], default_dict: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Remove fields from custom_dict that match their default_dict equivalents.

    This implements "auto-cleanup" for the sparse delta pattern:
    - If a Custom value equals its Default counterpart, remove it from Custom
    - When Custom doesn't have a field, Default is used (by design)
    - This keeps Custom minimal (only true customizations)

    Benefits:
    - Self-healing: automatically cleans up redundant entries
    - Simpler frontend: just send values, backend optimizes
    - Resilient: works even if someone manually edits DynamoDB

    Args:
        custom_dict: Custom config dictionary (modified in place)
        default_dict: Default config dictionary (read-only reference)

    Returns:
        Modified custom_dict with matching defaults removed

    Example:
        custom = {"classification": {"model": "nova-lite", "temperature": 0.0}}
        default = {"classification": {"model": "nova-lite", "temperature": 0.5}}
        # Result: {"classification": {"temperature": 0.0}}
        # (model removed because it matches default)
    """
    keys_to_remove = []

    for key, custom_value in list(custom_dict.items()):
        default_value = default_dict.get(key)

        if isinstance(custom_value, dict) and isinstance(default_value, dict):
            # Recurse into nested dicts
            strip_matching_defaults(custom_value, default_value)
            # Remove empty dicts after cleanup
            if len(custom_value) == 0:
                keys_to_remove.append(key)
        elif custom_value == default_value:
            # Value matches default - remove it (not needed in Custom)
            keys_to_remove.append(key)

    # Remove matching/empty keys
    for key in keys_to_remove:
        del custom_dict[key]

    return custom_dict


def get_diff_dict(base: Dict[str, Any], modified: Dict[str, Any]) -> Dict[str, Any]:
    """
    Get a dictionary containing only the fields that differ between base and modified.

    This creates a "diff dict" that when applied to base (via deep_update) would
    produce modified. Recursively compares nested dictionaries.

    Args:
        base: Base/default dictionary
        modified: Modified/custom dictionary

    Returns:
        Dictionary containing only the differences (values from modified that differ from base)

    Example:
        base = {"a": 1, "b": {"c": 2, "d": 3}}
        modified = {"a": 1, "b": {"c": 5, "d": 3}, "e": 6}
        result = {"b": {"c": 5}, "e": 6}  # Only changed/added fields
    """
    diff = {}

    # Check for added or changed keys in modified
    for key, value in modified.items():
        if key not in base:
            # New key - include it
            diff[key] = deepcopy(value)
        elif isinstance(value, dict) and isinstance(base[key], dict):
            # Both are dicts - recurse
            nested_diff = get_diff_dict(base[key], value)
            if nested_diff:  # Only include if there are differences
                diff[key] = nested_diff
        elif value != base[key]:
            # Value changed
            diff[key] = deepcopy(value)

    # Note: We don't track deletions (keys in base but not in modified)
    # This is intentional - Custom should always be a complete config

    return diff


def get_system_defaults_dir() -> Path:
    """
    Get the path to the system_defaults directory.

    The system_defaults directory is now bundled within the idp_common package
    at idp_common/config/system_defaults/. This ensures the defaults are available
    in all environments (Lambda, notebooks, pip-installed, etc.)

    Returns:
        Path to the system_defaults directory

    Priority order:
        1. Package resources (idp_common.config.system_defaults)
        2. Environment variable IDP_SYSTEM_DEFAULTS_DIR
        3. Legacy: config_library/system_defaults from project root
    """
    # Priority 1: Use package resources (works in Lambda, pip installed, etc.)
    try:
        # Get the package resource directory
        defaults_resource = importlib_files("idp_common.config.system_defaults")
        # For directories, we can use the traversable directly as a path
        # This works because the package includes the directory
        defaults_path = Path(str(defaults_resource))
        if defaults_path.exists() and defaults_path.is_dir():
            logger.debug(f"Using package system_defaults: {defaults_path}")
            return defaults_path
    except (ModuleNotFoundError, TypeError, AttributeError) as e:
        logger.debug(f"Package resources not available: {e}")

    # Priority 2: Environment variable override
    env_path = os.environ.get("IDP_SYSTEM_DEFAULTS_DIR")
    if env_path:
        env_defaults_dir = Path(env_path)
        if env_defaults_dir.exists():
            logger.debug(f"Using env var system_defaults: {env_defaults_dir}")
            return env_defaults_dir

    # Priority 3: Legacy - relative to this file or project root
    current_file = Path(__file__)

    # Check relative to this file (system_defaults is sibling directory)
    sibling_dir = current_file.parent / "system_defaults"
    if sibling_dir.exists():
        logger.debug(f"Using sibling system_defaults: {sibling_dir}")
        return sibling_dir

    # Legacy fallback: config_library/system_defaults from various roots
    possible_roots = [
        current_file.parent.parent.parent.parent.parent.parent,  # From package
        Path.cwd(),  # Current working directory
        Path(os.environ.get("IDP_PROJECT_ROOT", "")),  # Environment variable
    ]

    for root in possible_roots:
        defaults_dir = root / "config_library" / "system_defaults"
        if defaults_dir.exists():
            logger.debug(f"Using legacy system_defaults: {defaults_dir}")
            return defaults_dir

    raise FileNotFoundError(
        "Could not locate system_defaults directory. "
        "The idp_common package should include this directory. "
        "If running from source, ensure you're in the project root or set IDP_SYSTEM_DEFAULTS_DIR."
    )


def load_yaml_file(path: Path) -> Dict[str, Any]:
    """
    Load a YAML file and return its contents as a dictionary.

    Args:
        path: Path to the YAML file

    Returns:
        Dictionary containing the YAML contents

    Raises:
        FileNotFoundError: If the file doesn't exist
        yaml.YAMLError: If the file contains invalid YAML
    """
    if not path.exists():
        raise FileNotFoundError(f"YAML file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        content = yaml.safe_load(f)

    return content if content is not None else {}


def _resolve_inheritance(
    config: Dict[str, Any],
    defaults_dir: Path,
    resolved_files: Optional[set] = None,
) -> Dict[str, Any]:
    """
    Recursively resolve _inherits directive in a config.

    Supports both string (single file) and list (multiple files) inheritance.

    Args:
        config: Configuration with potential _inherits directive
        defaults_dir: Directory containing the defaults files
        resolved_files: Set of already resolved files (to prevent cycles)

    Returns:
        Merged configuration with all inheritance resolved
    """
    if resolved_files is None:
        resolved_files = set()

    # Get _inherits directive (can be string or list)
    inherits = config.pop("_inherits", None)

    if inherits is None:
        return config

    # Normalize to list
    if isinstance(inherits, str):
        inherits_list = [inherits]
    else:
        inherits_list = inherits

    # Start with empty config
    result: Dict[str, Any] = {}

    # Process each inherited file in order
    for inherit_file in inherits_list:
        if inherit_file in resolved_files:
            logger.warning(f"Circular inheritance detected: {inherit_file}")
            continue

        resolved_files.add(inherit_file)
        inherit_path = defaults_dir / inherit_file

        if not inherit_path.exists():
            raise FileNotFoundError(f"Inherited file not found: {inherit_path}")

        # Load inherited config and recursively resolve its inheritance
        inherited_config = load_yaml_file(inherit_path)
        inherited_config = _resolve_inheritance(
            inherited_config, defaults_dir, resolved_files.copy()
        )

        # Merge inherited config into result
        deep_update(result, inherited_config)

    # Finally, merge the current config on top (it has highest priority)
    deep_update(result, config)

    return result


def load_system_defaults(pattern: str = "pattern-2") -> Dict[str, Any]:
    """
    Load system defaults for a specific pattern.

    This function loads the pattern file and recursively resolves all
    inheritance directives. Patterns can inherit from:
    - A single base file: _inherits: base.yaml
    - Multiple modules: _inherits: [base-notes.yaml, base-classes.yaml, base-ocr.yaml, ...]

    Args:
        pattern: Pattern name (pattern-1, pattern-2)

    Returns:
        Dictionary containing the merged system defaults

    Raises:
        ValueError: If pattern is not valid
        FileNotFoundError: If defaults files don't exist
    """
    if pattern not in VALID_PATTERNS:
        raise ValueError(
            f"Invalid pattern '{pattern}'. Valid patterns: {VALID_PATTERNS}"
        )

    defaults_dir = get_system_defaults_dir()

    # Load pattern-specific defaults
    pattern_path = defaults_dir / f"{pattern}.yaml"
    pattern_config = load_yaml_file(pattern_path)

    # Recursively resolve all inheritance
    result = _resolve_inheritance(pattern_config, defaults_dir)

    return result


def merge_config_with_defaults(
    user_config: Dict[str, Any],
    pattern: str = "pattern-2",
    validate: bool = False,
    migrate: bool = True,
) -> Dict[str, Any]:
    """
    Merge a user's config with system defaults.

    User values take precedence over defaults. Missing fields in user config
    are populated from system defaults.

    Args:
        user_config: User's configuration dictionary (may be partial)
        pattern: Pattern to use for defaults (pattern-1, pattern-2)
        validate: If True, validate the merged config with Pydantic
        migrate: If True (default), migrate ``user_config`` to the current config
            format (e.g. v0.5 -> v0.6) BEFORE merging onto the defaults.

    Returns:
        Complete configuration dictionary with defaults applied

    Example:
        user_config = {
            "classification": {"model": "us.amazon.nova-lite-v1:0"},
            "classes": [...]
        }
        result = merge_config_with_defaults(user_config, "pattern-2")
        # Result has all fields populated from defaults, with user's model override

    IMPORTANT -- migrate BEFORE merge:
        The system defaults are already in the current (v0.6) shape. If a v0.5-shaped
        ``user_config`` (e.g. a top-level ``assessment`` block, or
        ``extraction.assessment_integration``) is merged onto the v0.6 defaults FIRST,
        the merged dict carries BOTH the v0.6 default keys and the legacy keys. A later
        migration then applies its "explicit v0.6 keys win over migrated legacy values"
        rule and the v0.6 DEFAULTS silently beat the user's migrated legacy
        customizations (a pinned assessment model / list_batch_size / geometry_mode
        reverts to default). Migrating ``user_config`` first keeps the merge a clean
        v0.6-over-v0.6 deep_update where user values win. The migration is idempotent,
        so this is a no-op for already-v0.6 input.
    """
    # Migrate the user config to the current format BEFORE merging (see docstring).
    if migrate:
        from idp_common.config.migrations import migrate_config

        user_config = migrate_config(deepcopy(user_config))

    # Load system defaults
    defaults = load_system_defaults(pattern)

    # Deep merge user config on top of defaults
    result = deepcopy(defaults)
    deep_update(result, user_config)

    if validate:
        # Import here to avoid circular imports
        from idp_common.config.models import IDPConfig

        IDPConfig.model_validate(result)

    return result


def generate_config_template(
    features: Union[str, List[str]] = "min",
    pattern: str = "pattern-2",
    include_prompts: bool = False,
    include_comments: bool = True,
) -> str:
    """
    Generate a configuration template YAML string.

    Args:
        features: Feature set name ("min", "core", "all") or list of section names
        pattern: Pattern to base the template on
        include_prompts: If True, include full prompt templates
        include_comments: If True, include helpful comments

    Returns:
        YAML string for the configuration template
    """
    # Determine which sections to include
    if isinstance(features, str):
        if features not in FEATURE_SETS:
            raise ValueError(
                f"Invalid feature set '{features}'. Valid: {list(FEATURE_SETS.keys())}"
            )
        sections = FEATURE_SETS[features]
    else:
        sections = features

    # Load full defaults
    defaults = load_system_defaults(pattern)

    # Build template with only requested sections
    template: Dict[str, Any] = {}

    # Always include notes
    template["notes"] = "My IDP Configuration - customize as needed"

    for section in sections:
        if section in defaults:
            section_data = deepcopy(defaults[section])

            # Optionally strip prompts for cleaner output
            if not include_prompts:
                section_data = _strip_prompts(section_data)

            template[section] = section_data

    # Add disabled flags for sections not in the template
    if features == "min":
        template["summarization"] = {"enabled": False}
        template["assessment"] = {"enabled": False}
        template["evaluation"] = {"enabled": False}

    # Convert to YAML
    yaml_str = yaml.dump(
        template,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=120,
    )

    # Add header comments if requested
    if include_comments:
        header = f"""# IDP Configuration Template
# Generated for: {pattern}
# Feature set: {features}
#
# This is a minimal configuration - unspecified fields use system defaults.
# Edit only the values you need to customize.
#
# Documentation: https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/docs/configuration.md

"""
        yaml_str = header + yaml_str

    return yaml_str


def _strip_prompts(data: Any) -> Any:
    """
    Recursively strip prompt fields from a config section.

    Replaces long prompt strings with placeholder comments.
    """
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            if key in ("system_prompt", "task_prompt", "user_prompt"):
                # Replace prompts with placeholder
                if value and len(str(value)) > 50:
                    result[key] = "# Uses system default prompt"
                else:
                    result[key] = value
            else:
                result[key] = _strip_prompts(value)
        return result
    elif isinstance(data, list):
        return [_strip_prompts(item) for item in data]
    else:
        return data


def validate_config(
    config: Dict[str, Any],
    pattern: str = "pattern-2",
) -> Dict[str, Any]:
    """
    Validate a configuration against system defaults and Pydantic models.

    Args:
        config: Configuration dictionary to validate
        pattern: Pattern to validate against

    Returns:
        Dictionary with validation results:
        - valid: bool - whether config is valid
        - errors: List[str] - validation errors if any
        - warnings: List[str] - validation warnings
        - merged_config: Dict - the merged config (if valid)
    """
    result = {
        "valid": True,
        "errors": [],
        "warnings": [],
        "merged_config": None,
    }

    # Check pattern is valid
    if pattern not in VALID_PATTERNS:
        result["valid"] = False
        result["errors"].append(
            f"Invalid pattern '{pattern}'. Valid patterns: {VALID_PATTERNS}"
        )
        return result

    # Try to merge with defaults
    try:
        merged = merge_config_with_defaults(config, pattern, validate=False)
        result["merged_config"] = merged
    except Exception as e:
        result["valid"] = False
        result["errors"].append(f"Failed to merge with defaults: {str(e)}")
        return result

    # Validate with Pydantic
    try:
        from idp_common.config.models import IDPConfig

        IDPConfig.model_validate(merged)
    except Exception as e:
        result["valid"] = False
        result["errors"].append(f"Pydantic validation failed: {str(e)}")
        return result

    # Check for common issues (warnings)
    if not config.get("classes"):
        result["warnings"].append(
            "No document classes defined - you must add at least one class"
        )

    # Enhanced validation checks
    _validate_model_ids(merged, result)
    _validate_max_tokens(merged, result)
    _validate_task_prompt_placeholders(merged, result)
    _validate_schema_fields(config.get("classes", []), result)
    _validate_agentic_openai(merged, result)
    _validate_discovery_openai(merged, result)

    return result


def _load_valid_bedrock_models() -> set:
    """
    Load valid Bedrock model IDs from pricing.yaml.

    Returns:
        Set of valid model IDs, or empty set if pricing.yaml not found
    """
    try:
        # Try to find pricing.yaml relative to this file
        # merge_utils.py is at: lib/idp_common_pkg/idp_common/config/merge_utils.py
        # Need to walk up 5 parents to reach repo root
        pricing_paths = [
            # Development: from lib/idp_common_pkg/idp_common/config/ (5 parents to repo root)
            Path(__file__).parent.parent.parent.parent.parent
            / "config_library"
            / "pricing.yaml",
            # Fallback: use IDP_PROJECT_ROOT or current working directory
            Path(os.environ.get("IDP_PROJECT_ROOT", "."))
            / "config_library"
            / "pricing.yaml",
        ]

        pricing_file = None
        for path in pricing_paths:
            if path.exists():
                pricing_file = path
                break

        if not pricing_file:
            logger.warning(
                "pricing.yaml not found - model ID validation disabled. "
                "Ensure idp-cli is run from repository root or set IDP_PROJECT_ROOT environment variable."
            )
            return set()

        with open(pricing_file) as f:
            pricing_config = yaml.safe_load(f)

        model_ids = set()
        for entry in pricing_config.get("pricing", []):
            name = entry.get("name", "")
            if name.startswith("bedrock/"):
                model_id = name.replace("bedrock/", "")
                model_ids.add(model_id)

        # Add special cases
        model_ids.add("LambdaHook")

        # Also add base model IDs (without region prefix) for GovCloud compatibility
        # e.g., "us.amazon.nova-pro-v1:0" -> also allow "amazon.nova-pro-v1:0"
        base_models = set()
        for model_id in list(model_ids):
            if "." in model_id:
                # Extract base model (remove region prefix)
                parts = model_id.split(".", 1)
                if len(parts) == 2:
                    base_model = parts[1]
                    base_models.add(base_model)

        model_ids.update(base_models)

        logger.debug(
            "Loaded %d valid Bedrock model IDs from pricing.yaml", len(model_ids)
        )
        return model_ids

    except Exception as e:
        logger.warning("Could not load pricing.yaml for model validation: %s", e)
        return set()


def _validate_model_ids(merged_config: Dict[str, Any], result: Dict[str, Any]) -> None:
    """
    Validate Bedrock model IDs against pricing.yaml.

    Checks that model IDs in config are valid and recognized.

    A model may be named by ARN rather than by bare model ID — GovCloud
    requires an account-scoped inference-profile ARN, and provisioned-throughput
    or application-inference-profile deployments are ARN-only everywhere. ARNs
    are reduced to the model ID they name before the catalog lookup.

    Three outcomes for a value that doesn't match the catalog:

    - **Bare model ID → error.** That's a typo; nothing else it could be.
    - **ARN whose resolved resource has model-ID *shape*** (contains a ``.``
      and isn't a UUID) **→ error.** It looks like a model ID and still doesn't
      match, so the likely cause is a typo in the ARN's resource-id, partition
      or account. It is also, by construction, a model with no
      ``config_library/pricing.yaml`` entry — so cost reporting would be broken
      for it regardless. To use a model newer than this release, add it to
      ``config_library/pricing.yaml`` (see ``.claude/skills/add-model.md``,
      which requires that entry anyway).
    - **ARN whose resolved resource is opaque** (an application-inference-profile
      UUID, a provisioned-model name — no ``.``, or UUID-shaped) **→ warning.**
      Its underlying model can't be determined without a Bedrock API call, so
      it's unverifiable, not wrong.

    Note this whole function is a no-op when ``config_library/pricing.yaml``
    isn't on disk: ``_load_valid_bedrock_models`` returns an empty set and we
    return early. So the strict paths above only fire from a repository
    checkout — the CLI/SDK run from source, and CI — not from a
    pip-installed ``idp_common`` in a Lambda.
    """
    valid_models = _load_valid_bedrock_models()
    if not valid_models:
        # Can't validate - skip silently
        return

    sections_with_models = {
        "ocr": "model_id",
        "classification": "model",
        "extraction": "model",
        "assessment": "model",
        "summarization": "model",
    }

    for section, field_name in sections_with_models.items():
        if section not in merged_config:
            continue

        model_id = merged_config[section].get(field_name)
        if not model_id:
            continue

        resolved = resolve_model_id_from_arn(model_id)
        if resolved in valid_models:
            continue

        if model_id.startswith("arn:"):
            # Distinguish an unverifiable opaque resource (a legitimate
            # application-inference-profile UUID / provisioned-throughput ARN
            # whose underlying model can only be known via a Bedrock API call)
            # from a probably-typo'd ARN whose resource DOES look like a
            # model ID but doesn't match anything in valid_models. Round-7
            # review fix; round-9 cleanup: dropped dead
            # ``resolved.startswith("application-inference-profile/")`` and
            # ``"provisioned-model/"`` guards — ``resolve_model_id_from_arn``
            # strips those type prefixes, so ``resolved`` never carries
            # them. UUIDs (which lack dots) fall through to the warning
            # path naturally via the ``"." in resolved`` check.
            _looks_like_model_id_shape = "." in resolved and not _looks_like_uuid(
                resolved
            )
            if _looks_like_model_id_shape:
                # The ARN resolved to a string that looks like a model ID
                # (has family.name shape) but doesn't match — treat as a typo.
                result["valid"] = False
                result["errors"].append(
                    f"{section}.{field_name} names a Bedrock ARN whose resolved "
                    f"model ID ({resolved!r}) is not a known Bedrock model. This is "
                    f"most likely a typo in the ARN's resource-id, partition, or "
                    f"account. Full ARN: {model_id}"
                )
                continue
            result["warnings"].append(
                f"{section}.{field_name} names a Bedrock ARN whose underlying "
                f"model can't be determined offline: {model_id}. Cost reporting "
                f"needs a matching entry in config_library/pricing.yaml."
            )
            continue

        result["valid"] = False
        result["errors"].append(
            f"{section}.{field_name} has invalid model ID: {model_id}. "
            f"Verify the model name is correct and ensure it's enabled in the Bedrock console. "
            f"Check config_library/pricing.yaml for valid model IDs."
        )


def _looks_like_uuid(value: str) -> bool:
    """Heuristic — reject values that look like a UUID (opaque application
    inference profile IDs) from being treated as typo'd model IDs."""
    import re

    return bool(
        re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            value,
            re.I,
        )
    )


def _validate_agentic_openai(
    merged_config: Dict[str, Any], result: Dict[str, Any]
) -> None:
    """Error when an OpenAI GPT-5.x model is paired with agentic extraction.

    OpenAI Responses models (openai.gpt-5.*) are served via the bedrock-mantle
    endpoint and are incompatible with the Converse-based agentic (Strands)
    extraction path. This is a hard validation error (rather than a silent
    runtime fallback) so the misconfiguration surfaces at config time instead
    of failing obscurely mid-processing.
    """
    from idp_common.bedrock.openai_responses import is_openai_responses_model
    from idp_common.config.schema_constants import X_AWS_IDP_EXTRACTION_MODEL

    extraction = merged_config.get("extraction", {})
    if not isinstance(extraction, dict):
        return

    agentic_enabled = bool(extraction.get("agentic", {}).get("enabled"))
    if not agentic_enabled:
        return

    # Global extraction model
    global_model = extraction.get("model")
    if global_model and is_openai_responses_model(global_model):
        result["valid"] = False
        result["errors"].append(
            f"extraction.model '{global_model}' is an OpenAI Responses model, which "
            "is NOT compatible with agentic extraction (extraction.agentic.enabled=true). "
            "Set agentic.enabled=false or choose a non-OpenAI model."
        )

    # Per-class extraction model overrides
    for cls in merged_config.get("classes", []) or []:
        if not isinstance(cls, dict):
            continue
        override = cls.get(X_AWS_IDP_EXTRACTION_MODEL)
        if override and is_openai_responses_model(override):
            class_name = cls.get("x-aws-idp-document-type", "<unnamed>")
            result["valid"] = False
            result["errors"].append(
                f"Class '{class_name}' overrides extraction with OpenAI Responses "
                f"model '{override}', which is NOT compatible with agentic extraction "
                "(extraction.agentic.enabled=true). Set agentic.enabled=false or "
                "choose a non-OpenAI model for this class."
            )


def _validate_discovery_openai(
    merged_config: Dict[str, Any], result: Dict[str, Any]
) -> None:
    """Error when a model that can't take ``document`` blocks is set for discovery.

    Discovery ingests whole PDFs via Converse ``document`` content blocks. OpenAI
    GPT-5.x (bedrock-mantle Responses API) and xAI Grok (rejects them outright)
    both accept text + image only, so routing either here would silently drop the
    document. Reject at config time. (Neither is offered in the discovery
    picklists.)
    """
    from idp_common.bedrock.client import document_blocks_unsupported_reason

    discovery = merged_config.get("discovery", {})
    if not isinstance(discovery, dict):
        return

    # Sub-sections that carry a per-section model: model_id for the class/auto
    # discovery sections, model for rules discovery.
    checks = [
        (
            "discovery.without_ground_truth.model_id",
            discovery.get("without_ground_truth", {}),
            "model_id",
        ),
        (
            "discovery.with_ground_truth.model_id",
            discovery.get("with_ground_truth", {}),
            "model_id",
        ),
        ("discovery.auto_split.model_id", discovery.get("auto_split", {}), "model_id"),
        ("discovery.rules.model", discovery.get("rules", {}), "model"),
    ]
    for label, section, field in checks:
        if not isinstance(section, dict):
            continue
        model_id = section.get(field)
        reason = document_blocks_unsupported_reason(model_id) if model_id else None
        if reason:
            result["valid"] = False
            result["errors"].append(
                f"{label} is set to '{model_id}', which is NOT supported for "
                f"discovery — {reason}, but discovery sends whole-PDF document "
                "blocks. Choose an Anthropic or Nova model."
            )


def _validate_task_prompt_placeholders(
    merged_config: Dict[str, Any], result: Dict[str, Any]
) -> None:
    """
    Validate task_prompt placeholders for all pipeline sections.

    Checks for missing required placeholders that would cause silent failures
    when custom task_prompts are provided.

    Required placeholders by section:
    - ocr: {DOCUMENT_IMAGE}
    - classification: {DOCUMENT_TEXT} or {DOCUMENT_IMAGE}
    - extraction: {DOCUMENT_TEXT} or {DOCUMENT_IMAGE}
    - assessment: {DOCUMENT_IMAGE}, {OCR_TEXT_CONFIDENCE}, {EXTRACTION_RESULTS}
    - summarization: {DOCUMENT_TEXT}, {EXTRACTION_RESULTS}
    """
    # Validation rules: section -> (required_placeholders, require_all_flag)
    # require_all_flag: True = ALL placeholders required, False = ANY one required
    validation_rules = {
        "ocr": (
            ["{DOCUMENT_IMAGE}"],
            True,  # Must have DOCUMENT_IMAGE
            "ocr.task_prompt must include {DOCUMENT_IMAGE} placeholder. "
            "Without this placeholder, the OCR model will not receive the document image, "
            "resulting in OCR failures. "
            "Either add the required placeholder or remove task_prompt to use system defaults.",
        ),
        "classification": (
            ["{DOCUMENT_TEXT}", "{DOCUMENT_IMAGE}"],
            False,  # Must have at least one
            "classification.task_prompt must include {DOCUMENT_TEXT} or {DOCUMENT_IMAGE} placeholder. "
            "Without these placeholders, the classification model will not receive document content, "
            "resulting in classification failures. "
            "Either add at least one required placeholder or remove task_prompt to use system defaults.",
        ),
        "extraction": (
            ["{DOCUMENT_TEXT}", "{DOCUMENT_IMAGE}"],
            False,  # Must have at least one
            "extraction.task_prompt must include {DOCUMENT_TEXT} or {DOCUMENT_IMAGE} placeholder. "
            "Without these placeholders, the LLM will not receive document content from OCR, "
            "resulting in silent extraction failures. "
            "Either add at least one required placeholder or remove task_prompt to use system defaults.",
        ),
        "assessment": (
            ["{DOCUMENT_IMAGE}", "{OCR_TEXT_CONFIDENCE}", "{EXTRACTION_RESULTS}"],
            True,  # Must have all three
            "assessment.task_prompt must include {DOCUMENT_IMAGE}, {OCR_TEXT_CONFIDENCE}, and {EXTRACTION_RESULTS} placeholders. "
            "Without these placeholders, the assessment model cannot evaluate extraction quality or provide bounding boxes. "
            "Either add all required placeholders or remove task_prompt to use system defaults.",
        ),
        "summarization": (
            ["{DOCUMENT_TEXT}", "{EXTRACTION_RESULTS}"],
            True,  # Must have both
            "summarization.task_prompt must include {DOCUMENT_TEXT} and {EXTRACTION_RESULTS} placeholders. "
            "Without these placeholders, the summarization model cannot generate accurate summaries. "
            "Either add all required placeholders or remove task_prompt to use system defaults.",
        ),
    }

    # Check each section that has validation rules
    for section_name, (
        required_placeholders,
        require_all,
        error_message,
    ) in validation_rules.items():
        section = merged_config.get(section_name, {})
        task_prompt = section.get("task_prompt", "")

        # Special case: OCR task_prompt is only used when backend is 'bedrock'
        if section_name == "ocr":
            ocr_backend = section.get("backend", "textract")
            if ocr_backend != "bedrock":
                # Skip OCR validation for non-bedrock backends (Textract doesn't use prompts)
                continue

        # Skip validation for disabled sections (assessment, summarization)
        if section_name in ["assessment", "summarization"]:
            if not section.get("enabled", True):
                # Section is explicitly disabled, skip validation
                continue

        if task_prompt:
            # Check which placeholders are present
            present_placeholders = [
                placeholder
                for placeholder in required_placeholders
                if placeholder in task_prompt
            ]

            # Determine if validation passes
            if require_all:
                # ALL placeholders must be present
                validation_passed = len(present_placeholders) == len(
                    required_placeholders
                )
            else:
                # At least ONE placeholder must be present
                validation_passed = len(present_placeholders) > 0

            if not validation_passed:
                result["valid"] = False
                result["errors"].append(error_message)


def _validate_schema_fields(
    classes: List[Dict[str, Any]], result: Dict[str, Any]
) -> None:
    """
    Validate JSON Schema fields in document class definitions.

    Checks for:
    - Non-standard JSON Schema keywords that may be ignored
    - Use of 'data_type' field (not in JSON Schema spec)
    """
    # Standard JSON Schema keywords (Draft 2020-12)
    ALLOWED_KEYWORDS = {
        # Core keywords
        "$schema",
        "$id",
        "$ref",
        "$defs",
        "$anchor",
        "$dynamicRef",
        "$dynamicAnchor",
        "$vocabulary",
        "$comment",
        # Type keywords
        "type",
        "enum",
        "const",
        # Structure keywords
        "properties",
        "patternProperties",
        "additionalProperties",
        "propertyNames",
        "items",
        "prefixItems",
        "contains",
        "additionalItems",
        "unevaluatedProperties",
        "unevaluatedItems",
        # Validation keywords
        "required",
        "dependentRequired",
        "dependentSchemas",
        "if",
        "then",
        "else",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        # String validation
        "minLength",
        "maxLength",
        "pattern",
        "format",
        # Numeric validation
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        # Array validation
        "minItems",
        "maxItems",
        "uniqueItems",
        "minContains",
        "maxContains",
        # Object validation
        "minProperties",
        "maxProperties",
        # Metadata
        "title",
        "description",
        "default",
        "examples",
        "readOnly",
        "writeOnly",
        "deprecated",
        # Content keywords
        "contentMediaType",
        "contentEncoding",
        "contentSchema",
    }

    # AWS IDP-specific extensions (allowed)
    IDP_EXTENSIONS = {
        "x-aws-idp-confidence-threshold",
        "x-aws-idp-evaluation-method",
        "x-aws-idp-evaluation-threshold",
        "x-aws-idp-evaluation-weight",
        "x-aws-idp-document-type",
        "x-aws-idp-list-item-description",
        "x-aws-idp-page-types",
        "x-aws-idp-source-page-types",
        "x-aws-idp-instance-array",
    }

    non_standard_fields = {}

    for class_idx, doc_class in enumerate(classes):
        _check_schema_properties(
            doc_class.get("properties", {}),
            f"classes[{class_idx}]",
            ALLOWED_KEYWORDS,
            IDP_EXTENSIONS,
            non_standard_fields,
        )

        # Also check $defs
        for def_name, def_schema in doc_class.get("$defs", {}).items():
            _check_schema_properties(
                def_schema.get("properties", {}),
                f"classes[{class_idx}].$defs.{def_name}",
                ALLOWED_KEYWORDS,
                IDP_EXTENSIONS,
                non_standard_fields,
            )

    # Report non-standard fields as warnings (they're harmless but non-standard)
    if non_standard_fields:
        for field, locations in non_standard_fields.items():
            result["warnings"].append(
                f"Non-standard field '{field}' found in {len(locations)} properties. "
                f"This field is not part of the JSON Schema specification and will be ignored. "
                f"Locations: {', '.join(locations[:3])}"
                + (f" (and {len(locations) - 3} more)" if len(locations) > 3 else "")
            )


def _check_schema_properties(
    properties: Dict[str, Any],
    path: str,
    allowed_keywords: set,
    idp_extensions: set,
    non_standard_fields: Dict[str, List[str]],
) -> None:
    """
    Recursively check schema properties for non-standard fields.

    Args:
        properties: Properties dictionary from JSON Schema
        path: Current path in schema (for error reporting)
        allowed_keywords: Set of allowed JSON Schema keywords
        idp_extensions: Set of allowed AWS IDP extensions
        non_standard_fields: Dictionary to collect non-standard fields and their locations
    """
    for prop_name, prop_def in properties.items():
        if not isinstance(prop_def, dict):
            continue

        prop_path = f"{path}.{prop_name}"

        for key in prop_def.keys():
            # Skip allowed JSON Schema keywords
            if key in allowed_keywords:
                continue

            # Skip known IDP extensions explicitly
            if key in idp_extensions:
                continue

            # Skip any other x-* extensions (custom extensions allowed by JSON Schema spec)
            # This catches x-* fields not in our explicit IDP_EXTENSIONS set
            if key.startswith("x-"):
                continue

            # Found non-standard field
            if key not in non_standard_fields:
                non_standard_fields[key] = []
            non_standard_fields[key].append(prop_path)

        # Recurse into nested objects
        if "properties" in prop_def:
            _check_schema_properties(
                prop_def["properties"],
                prop_path,
                allowed_keywords,
                idp_extensions,
                non_standard_fields,
            )

        # Recurse into array items if they have properties
        if "items" in prop_def and isinstance(prop_def["items"], dict):
            items_def = prop_def["items"]
            if "properties" in items_def:
                _check_schema_properties(
                    items_def["properties"],
                    f"{prop_path}[]",
                    allowed_keywords,
                    idp_extensions,
                    non_standard_fields,
                )


def _validate_max_tokens(merged_config: Dict[str, Any], result: Dict[str, Any]) -> None:
    """
    Validate max_tokens against model-specific limits.

    Checks that max_tokens in each service config does not exceed the
    maximum output tokens supported by the configured model.

    This prevents runtime failures when the Bedrock API rejects requests
    with max_tokens values that exceed model limits.
    """
    # Services that still expose a max_tokens knob. extraction + confidence
    # (assessment) no longer do — their output is always requested at the model
    # maximum and clamped at request time — so they cannot be over-limit and are
    # excluded here.
    sections_with_tokens = {
        "classification": {"model_field": "model", "max_tokens_field": "max_tokens"},
        "summarization": {"model_field": "model", "max_tokens_field": "max_tokens"},
    }

    for section_name, config in sections_with_tokens.items():
        section = merged_config.get(section_name, {})

        # Skip disabled sections
        if section_name in ["assessment", "summarization"]:
            if not section.get("enabled", True):
                continue

        model_id = section.get(config["model_field"])
        max_tokens_raw = section.get(config["max_tokens_field"])

        # Skip if the model is missing. max_tokens is an OPTIONAL cap: empty
        # string / None / a value that coerces to <= 0 all mean "unset" (use the
        # model's max output), so there is nothing to validate.
        if not model_id or max_tokens_raw in (None, ""):
            continue

        # Config stores numbers as strings (DynamoDB); coerce before comparing
        # against the int model limit. A non-numeric value is treated as unset.
        try:
            max_tokens = int(max_tokens_raw)
        except (TypeError, ValueError):
            continue
        if max_tokens <= 0:
            continue

        # Get model's max output tokens limit
        try:
            model_limit = get_model_max_output_tokens(model_id)

            if max_tokens > model_limit:
                result["valid"] = False
                result["errors"].append(
                    f"{section_name}.max_tokens ({max_tokens}) exceeds model limit ({model_limit:,}) "
                    f"for {model_id}. "
                    f"Reduce max_tokens to {model_limit:,} or less, or choose a different model. "
                    f"Model limits: Claude 4.x=64,000, Nova=10,000, Claude 3.x=8,192"
                )
        except Exception as e:
            logger.warning(
                "Could not validate max_tokens for %s: %s", section_name, str(e)
            )
