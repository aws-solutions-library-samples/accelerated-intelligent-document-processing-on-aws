# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Constants for AWS IDP-specific JSON Schema extensions.

These custom fields extend standard JSON Schema to support IDP-specific features
like document types, attribute types, and evaluation methods.
"""

# ============================================================================
# JSON Schema Standard Fields
# ============================================================================
SCHEMA_FIELD = "$schema"
ID_FIELD = "$id"
DEFS_FIELD = "$defs"
REF_FIELD = "$ref"

# ============================================================================
# AWS IDP Document Type Extensions
# ============================================================================
# Marks a schema as a document type (top-level class)
X_AWS_IDP_DOCUMENT_TYPE = "x-aws-idp-document-type"

# Classification metadata for document type
X_AWS_IDP_CLASSIFICATION = "x-aws-idp-classification"

# Regex patterns for classification optimization
X_AWS_IDP_DOCUMENT_NAME_REGEX = "x-aws-idp-document-name-regex"
X_AWS_IDP_PAGE_CONTENT_REGEX = "x-aws-idp-document-page-content-regex"

# Classes whose pages contain only static content (instructions, legal
# boilerplate, cover pages, etc.) can be marked with this extension. When
# true, downstream services (extraction, assessment, summarization,
# rule_validation, evaluation) will skip sections classified as this class.
X_AWS_IDP_EXCLUDE_FROM_PROCESSING = "x-aws-idp-exclude-from-processing"

# Optional short reason/category for why a class is excluded
# (e.g., "instructions", "legal", "cover-page"). Used for UI badges and
# evaluation report annotations.
X_AWS_IDP_EXCLUSION_REASON = "x-aws-idp-exclusion-reason"

# ============================================================================
# AWS IDP Extraction Extensions
# ============================================================================
# Per-class model override for extraction (overrides extraction.model)
X_AWS_IDP_EXTRACTION_MODEL = "x-aws-idp-extraction-model"

# Per-class stronger model used to re-extract when full-schema validation fails
# and extraction.agentic.validation.fail_action == "escalate". Overrides the
# global extraction.agentic.validation.escalation_model for that class.
X_AWS_IDP_EXTRACTION_ESCALATION_MODEL = "x-aws-idp-extraction-escalation-model"

# Per-class prompt overrides for extraction. When present on a class, these
# override the global extraction.system_prompt / extraction.task_prompt for
# that class. The task prompt supports the same placeholders as the global
# task prompt ({DOCUMENT_CLASS}, {ATTRIBUTE_NAMES_AND_DESCRIPTIONS},
# {FEW_SHOT_EXAMPLES}, {DOCUMENT_TEXT}, {DOCUMENT_IMAGE}, <<CACHEPOINT>>).
X_AWS_IDP_EXTRACTION_SYSTEM_PROMPT = "x-aws-idp-extraction-system-prompt"
X_AWS_IDP_EXTRACTION_TASK_PROMPT = "x-aws-idp-extraction-task-prompt"

# Declares the named page sub-types a class can include. Each entry has a
# `name`, optional `description`, and a regex (`x-aws-idp-document-page-content-regex`)
# used to detect the page type from per-page OCR text.
X_AWS_IDP_PAGE_TYPES = "x-aws-idp-page-types"

# On a property: declares which page sub-types contain the property's source
# data. If none of the listed page types are present in the section, the
# property is considered MISSING (vs. blank when the page is present but the
# field is empty).
X_AWS_IDP_SOURCE_PAGE_TYPES = "x-aws-idp-source-page-types"

# On a class whose schema is already modelled as a PACKET of records — i.e. its
# top level declares an array of objects, one per record — names that array as
# the "instance axis". The extraction service then reports
# `Section.instance_count = len(inference_result[<that property>])`, so a section
# holding several documents is visible at a glance in the UI.
#
# This is the zero-cost half of multi-instance support: it changes NOTHING about
# the extraction shape, the prompt, or any downstream consumer — it only tells
# the pipeline which existing array counts as "one document per element". It
# exists so configs that already solved multi-record packets by hand (the
# workaround validated in GitHub #565) get the instance count and the UI badge
# without restructuring their schema or migrating their evaluation baselines.
#
# Must name an existing top-level property whose type is an array of objects.
X_AWS_IDP_INSTANCE_ARRAY = "x-aws-idp-instance-array"

# On a class modelled as ONE record (the normal case): replace its EFFECTIVE
# schema with a List-of-Class wrapper
#
#     {"instances": {"type": "array", "items": <the original class schema>}}
#
# so a section holding several documents of this class extracts every one of them
# instead of silently returning only the first (GitHub #715 / #565).
#
# This is "Synthesize mode", the counterpart to Designate mode above. It is a
# schema TRANSFORM, not an output envelope: because the wrapper is declared in
# the class schema, inference_result stays a valid instance of its own declared
# schema and every consumer that just reads "the class schema" — the prompt, the
# generated Pydantic model, the JSON-Schema validator, the off-schema filter,
# assessment's list branch, Stickler's Hungarian row matching — keeps working.
# The transform itself lives in idp_common.schema.multi_instance and is applied
# by every stage that loads a class schema.
#
# Opt-in per class, NEVER auto-detected: auto-detection would move #565's
# detection problem one stage later and make every single-document section pay
# for a wrapper level. Detection is #753 and only ever warns.
#
# Mutually exclusive with X_AWS_IDP_INSTANCE_ARRAY (that key is for a class
# whose schema is ALREADY a packet of records; this one creates the packet).
#
# ⚠ Changes the shape of inference_result, so evaluation baselines for the class
# must be migrated to the wrapped shape — see docs/extraction-and-confidence.md
# and scripts/migrate_multi_instance_baselines.py.
X_AWS_IDP_MULTI_INSTANCE = "x-aws-idp-multi-instance"

# ============================================================================
# AWS IDP Policy/Rule Type Extensions
# ============================================================================
# Marks a schema as a policy/rule type (for rule validation)
X_AWS_IDP_POLICY_TYPE = "x-aws-idp-policy-type"
# Legacy/raw-discovery discriminator. The rules-discovery LLM emits this name and
# older hand-written configs use it; readers accept it as a fallback so such a
# class is matched rather than silently ignored. Writers emit POLICY_TYPE.
X_AWS_IDP_RULE_TYPE = "x-aws-idp-rule-type"

# Per-rule validation engine selection (values: "llm" or "z3")
X_AWS_IDP_VALIDATION_ENGINE = "x-aws-idp-validation-engine"

# Validation engine identifiers
VALIDATION_ENGINE_LLM = "llm"
VALIDATION_ENGINE_Z3 = "z3"

# Set of valid validation engine values
VALID_VALIDATION_ENGINES = frozenset([VALIDATION_ENGINE_LLM, VALIDATION_ENGINE_Z3])

# Per-rule unique identifier for Z3 rules (maps to RuleJSON file on S3)
X_AWS_IDP_RULE_ID = "x-aws-idp-rule-id"

# Per-rule inline RuleJSON object for Z3 validation (embedded in config)
X_AWS_IDP_RULE_JSON = "x-aws-idp-rule-json"

# Note: Policy classes use the same regex fields as document types
# X_AWS_IDP_DOCUMENT_NAME_REGEX - Pattern to match document name for policy filtering
# X_AWS_IDP_PAGE_CONTENT_REGEX - Pattern to match page content for policy filtering

# ============================================================================
# Legacy Attribute Type Values (for migration only)
# ============================================================================
# These map to standard JSON Schema types:
# - "simple" → type: "string"
# - "group" → type: "object" with properties
# - "list" → type: "array" with items
ATTRIBUTE_TYPE_SIMPLE = "simple"
ATTRIBUTE_TYPE_GROUP = "group"
ATTRIBUTE_TYPE_LIST = "list"

# ============================================================================
# AWS IDP List-Specific Extensions
# ============================================================================
# Description for list items
X_AWS_IDP_LIST_ITEM_DESCRIPTION = "x-aws-idp-list-item-description"

# Original attribute name (preserved from legacy format)
X_AWS_IDP_ORIGINAL_NAME = "x-aws-idp-original-name"


# ============================================================================
# AWS IDP Evaluation Extensions
# ============================================================================
# Evaluation method for attribute comparison (maps to Stickler comparators)
X_AWS_IDP_EVALUATION_METHOD = "x-aws-idp-evaluation-method"

# Evaluation threshold for comparison (0.0 to 1.0)
X_AWS_IDP_EVALUATION_THRESHOLD = "x-aws-idp-evaluation-threshold"

# Evaluation weight for field importance (business criticality)
X_AWS_IDP_EVALUATION_WEIGHT = "x-aws-idp-evaluation-weight"

# Model name for evaluation (document-level configuration)
X_AWS_IDP_EVALUATION_MODEL_NAME = "x-aws-idp-evaluation-model-name"

# Match threshold for overall document evaluation (0.0 to 1.0)
X_AWS_IDP_EVALUATION_MATCH_THRESHOLD = "x-aws-idp-evaluation-match-threshold"

X_AWS_IDP_EXAMPLES = "x-aws-idp-examples"

# Optional per-field config for the evaluation comparator, passed through to the
# underlying Stickler comparator (maps to x-aws-stickler-comparator-config).
# Currently used by the DATE method to configure DateComparator (e.g. dayfirst,
# tolerance, range_mode).
X_AWS_IDP_EVALUATION_METHOD_CONFIG = "x-aws-idp-evaluation-method-config"

# Opt-in escape hatch for an LLM evaluation method on a field INSIDE a structured
# list. Off by default because Hungarian row matching invokes an item field's
# comparator once per (ground-truth row x predicted row) cell, so an LLM call
# there costs O(N^2) Bedrock requests — measured at N^2 + 2N — and times out the
# evaluation Lambda on lists of a few dozen rows. Set true only for very small
# lists where the semantic match genuinely matters and the row count is bounded.
X_AWS_IDP_EVALUATION_LLM_IN_LIST = "x-aws-idp-evaluation-allow-llm-in-list"

# Valid evaluation methods
EVALUATION_METHOD_EXACT = "EXACT"
EVALUATION_METHOD_NUMERIC_EXACT = "NUMERIC_EXACT"
EVALUATION_METHOD_FUZZY = "FUZZY"
EVALUATION_METHOD_LEVENSHTEIN = "LEVENSHTEIN"
EVALUATION_METHOD_SEMANTIC = "SEMANTIC"
EVALUATION_METHOD_DATE = "DATE"
EVALUATION_METHOD_LLM = "LLM"
EVALUATION_METHOD_HUNGARIAN = "HUNGARIAN"

VALID_EVALUATION_METHODS = frozenset(
    [
        EVALUATION_METHOD_EXACT,
        EVALUATION_METHOD_NUMERIC_EXACT,
        EVALUATION_METHOD_FUZZY,
        EVALUATION_METHOD_LEVENSHTEIN,
        EVALUATION_METHOD_SEMANTIC,
        EVALUATION_METHOD_DATE,
        EVALUATION_METHOD_LLM,
        EVALUATION_METHOD_HUNGARIAN,
    ]
)

# Legacy: Confidence threshold for evaluation (0.0 to 1.0)
# Note: This is now superseded by X_AWS_IDP_EVALUATION_THRESHOLD
X_AWS_IDP_CONFIDENCE_THRESHOLD = "x-aws-idp-confidence-threshold"

# Per-class stronger confidence model the assessment self-healing ladder
# escalates to when rows still truncate/come back unscored after token-aware
# shrinking and same-model retries. Mirrors X_AWS_IDP_EXTRACTION_ESCALATION_MODEL;
# overrides the global extraction.confidence.escalation_model for that class.
X_AWS_IDP_CONFIDENCE_ESCALATION_MODEL = "x-aws-idp-confidence-escalation-model"

# ============================================================================
# AWS IDP Prompt Extensions
# ============================================================================
# Custom prompt override for attribute extraction
X_AWS_IDP_PROMPT_OVERRIDE = "x-aws-idp-prompt-override"

# Maximum length for prompt overrides
MAX_PROMPT_OVERRIDE_LENGTH = 10000

# ============================================================================
# AWS IDP Example Extensions
# ============================================================================
# Extensions for few-shot example support
X_AWS_IDP_CLASS_PROMPT = "x-aws-idp-class-prompt"
X_AWS_IDP_ATTRIBUTES_PROMPT = "x-aws-idp-attributes-prompt"
X_AWS_IDP_IMAGE_PATH = "x-aws-idp-image-path"

# ============================================================================
# Legacy Format Field Names
# ============================================================================
# These are the field names used in the legacy format
LEGACY_ATTRIBUTES = "attributes"
LEGACY_NAME = "name"
LEGACY_DESCRIPTION = "description"
LEGACY_ATTRIBUTE_TYPE = "attributeType"
LEGACY_GROUP_ATTRIBUTES = "groupAttributes"
LEGACY_LIST_ITEM_TEMPLATE = "listItemTemplate"
LEGACY_ITEM_ATTRIBUTES = "itemAttributes"
LEGACY_ITEM_DESCRIPTION = "itemDescription"
LEGACY_EVALUATION_METHOD = "evaluation_method"
LEGACY_CONFIDENCE_THRESHOLD = "confidence_threshold"
LEGACY_PROMPT_OVERRIDE = "prompt_override"

# Legacy example fields
LEGACY_EXAMPLES = "examples"
LEGACY_CLASS_PROMPT = "classPrompt"
LEGACY_ATTRIBUTES_PROMPT = "attributesPrompt"
LEGACY_IMAGE_PATH = "imagePath"

# Legacy regex fields (same name in both legacy and new format)
LEGACY_DOCUMENT_NAME_REGEX = "document_name_regex"
LEGACY_DOCUMENT_PAGE_CONTENT_REGEX = "document_page_content_regex"

# ============================================================================
# JSON Schema Standard Property Names
# ============================================================================
SCHEMA_TYPE = "type"
SCHEMA_PROPERTIES = "properties"
SCHEMA_ITEMS = "items"
SCHEMA_REQUIRED = "required"
SCHEMA_DESCRIPTION = "description"

# JSON Schema type values
TYPE_OBJECT = "object"
TYPE_ARRAY = "array"
TYPE_STRING = "string"
TYPE_NUMBER = "number"
TYPE_INTEGER = "integer"
TYPE_BOOLEAN = "boolean"
TYPE_NULL = "null"
