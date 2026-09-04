/**
 * JSON Schema and AWS IDP Extension Constants
 *
 * These constants match the backend lib/idp_common_pkg/idp_common/config/schema_constants.py
 * to ensure consistency between frontend and backend when building and processing document schemas.
 *
 * @see lib/idp_common_pkg/idp_common/config/schema_constants.py
 */

// ============================================================================
// JSON Schema Standard Fields
// ============================================================================
export const SCHEMA_FIELD = '$schema';
export const ID_FIELD = '$id';
export const DEFS_FIELD = '$defs';
export const REF_FIELD = '$ref';

// ============================================================================
// JSON Schema Standard Property Names
// ============================================================================
export const SCHEMA_TYPE = 'type';
export const SCHEMA_PROPERTIES = 'properties';
export const SCHEMA_ITEMS = 'items';
export const SCHEMA_REQUIRED = 'required';
export const SCHEMA_DESCRIPTION = 'description';
export const SCHEMA_EXAMPLES = 'examples';

// ============================================================================
// JSON Schema Type Values
// ============================================================================
export const TYPE_OBJECT = 'object';
export const TYPE_ARRAY = 'array';
export const TYPE_STRING = 'string';
export const TYPE_NUMBER = 'number';
export const TYPE_INTEGER = 'integer';
export const TYPE_BOOLEAN = 'boolean';
export const TYPE_NULL = 'null';

// UI-friendly type options for dropdowns
export const TYPE_OPTIONS = [
  { label: 'String', value: TYPE_STRING },
  { label: 'Number', value: TYPE_NUMBER },
  { label: 'Integer', value: TYPE_INTEGER },
  { label: 'Boolean', value: TYPE_BOOLEAN },
  { label: 'Object', value: TYPE_OBJECT },
  { label: 'Array', value: TYPE_ARRAY },
  { label: 'Null', value: TYPE_NULL },
];

// Type colors for UI visualization
export const TYPE_COLORS = {
  [TYPE_STRING]: 'blue',
  [TYPE_NUMBER]: 'green',
  [TYPE_INTEGER]: 'green',
  [TYPE_BOOLEAN]: 'grey',
  [TYPE_OBJECT]: 'red',
  [TYPE_ARRAY]: 'purple',
  [TYPE_NULL]: 'grey',
};

// ============================================================================
// AWS IDP Document Type Extensions
// ============================================================================
/** Marks a schema as a document type (top-level class) */
export const X_AWS_IDP_DOCUMENT_TYPE = 'x-aws-idp-document-type';

/** Marks a schema as a policy type (for rule validation) */
export const X_AWS_IDP_POLICY_TYPE = 'x-aws-idp-policy-type';

/** Classification metadata for document type */
export const X_AWS_IDP_CLASSIFICATION = 'x-aws-idp-classification';

/** Regex patterns for classification optimization */
export const X_AWS_IDP_DOCUMENT_NAME_REGEX = 'x-aws-idp-document-name-regex';
export const X_AWS_IDP_PAGE_CONTENT_REGEX = 'x-aws-idp-document-page-content-regex';

// Per-class extraction model override (overrides extraction.model)
export const X_AWS_IDP_EXTRACTION_MODEL = 'x-aws-idp-extraction-model';

// Per-class escalation model override. When extraction.agentic.validation
// fail_action is "escalate", failing fields for this class are re-extracted
// with this model (overrides extraction.agentic.validation.escalation_model).
export const X_AWS_IDP_EXTRACTION_ESCALATION_MODEL = 'x-aws-idp-extraction-escalation-model';

/**
 * Bedrock model options for per-class model-override dropdowns in the schema
 * editor (extraction model and escalation model). The leading empty option
 * means "use the global default". Keep in sync with the model enum in
 * patterns/unified/template.yaml.
 */
export const EXTRACTION_MODEL_OVERRIDE_OPTIONS = [
  { label: '(Use global default)', value: '' },
  { label: 'us.amazon.nova-lite-v1:0', value: 'us.amazon.nova-lite-v1:0' },
  { label: 'us.amazon.nova-pro-v1:0', value: 'us.amazon.nova-pro-v1:0' },
  { label: 'us.amazon.nova-premier-v1:0', value: 'us.amazon.nova-premier-v1:0' },
  { label: 'us.amazon.nova-2-lite-v1:0', value: 'us.amazon.nova-2-lite-v1:0' },
  { label: 'us.anthropic.claude-haiku-4-5-20251001-v1:0', value: 'us.anthropic.claude-haiku-4-5-20251001-v1:0' },
  { label: 'us.anthropic.claude-sonnet-4-5-20250929-v1:0', value: 'us.anthropic.claude-sonnet-4-5-20250929-v1:0' },
  { label: 'us.anthropic.claude-sonnet-4-6', value: 'us.anthropic.claude-sonnet-4-6' },
  { label: 'us.anthropic.claude-sonnet-4-6:1m', value: 'us.anthropic.claude-sonnet-4-6:1m' },
  { label: 'us.anthropic.claude-sonnet-5', value: 'us.anthropic.claude-sonnet-5' },
  { label: 'us.anthropic.claude-sonnet-5:1m', value: 'us.anthropic.claude-sonnet-5:1m' },
  { label: 'us.anthropic.claude-opus-4-5-20251101-v1:0', value: 'us.anthropic.claude-opus-4-5-20251101-v1:0' },
  { label: 'us.anthropic.claude-opus-4-6-v1', value: 'us.anthropic.claude-opus-4-6-v1' },
  { label: 'us.anthropic.claude-opus-4-6-v1:1m', value: 'us.anthropic.claude-opus-4-6-v1:1m' },
  { label: 'us.anthropic.claude-opus-4-7', value: 'us.anthropic.claude-opus-4-7' },
  { label: 'us.anthropic.claude-opus-4-7:1m', value: 'us.anthropic.claude-opus-4-7:1m' },
  { label: 'us.anthropic.claude-opus-4-8', value: 'us.anthropic.claude-opus-4-8' },
  { label: 'us.anthropic.claude-opus-4-8:1m', value: 'us.anthropic.claude-opus-4-8:1m' },
  { label: 'us.anthropic.claude-opus-5', value: 'us.anthropic.claude-opus-5' },
  { label: 'us.anthropic.claude-opus-5:1m', value: 'us.anthropic.claude-opus-5:1m' },
  // xAI Grok (Converse). Cross-region inference profiles only — there is no
  // in-region or eu. form, so EU deployments use the global. entry below.
  { label: 'us.xai.grok-4.6', value: 'us.xai.grok-4.6' },
  // OpenAI GPT-5.x (bedrock-mantle Responses API) - US regions only
  { label: 'openai.gpt-5.4', value: 'openai.gpt-5.4' },
  { label: 'openai.gpt-5.5', value: 'openai.gpt-5.5' },
  { label: 'openai.gpt-5.6-sol', value: 'openai.gpt-5.6-sol' },
  { label: 'openai.gpt-5.6-terra', value: 'openai.gpt-5.6-terra' },
  { label: 'openai.gpt-5.6-luna', value: 'openai.gpt-5.6-luna' },
  { label: 'eu.amazon.nova-lite-v1:0', value: 'eu.amazon.nova-lite-v1:0' },
  { label: 'eu.amazon.nova-pro-v1:0', value: 'eu.amazon.nova-pro-v1:0' },
  { label: 'eu.amazon.nova-2-lite-v1:0', value: 'eu.amazon.nova-2-lite-v1:0' },
  { label: 'eu.anthropic.claude-haiku-4-5-20251001-v1:0', value: 'eu.anthropic.claude-haiku-4-5-20251001-v1:0' },
  { label: 'eu.anthropic.claude-sonnet-4-5-20250929-v1:0', value: 'eu.anthropic.claude-sonnet-4-5-20250929-v1:0' },
  { label: 'eu.anthropic.claude-sonnet-4-6', value: 'eu.anthropic.claude-sonnet-4-6' },
  { label: 'eu.anthropic.claude-sonnet-4-6:1m', value: 'eu.anthropic.claude-sonnet-4-6:1m' },
  { label: 'eu.anthropic.claude-sonnet-5', value: 'eu.anthropic.claude-sonnet-5' },
  { label: 'eu.anthropic.claude-sonnet-5:1m', value: 'eu.anthropic.claude-sonnet-5:1m' },
  { label: 'eu.anthropic.claude-opus-4-5-20251101-v1:0', value: 'eu.anthropic.claude-opus-4-5-20251101-v1:0' },
  { label: 'eu.anthropic.claude-opus-4-6-v1', value: 'eu.anthropic.claude-opus-4-6-v1' },
  { label: 'eu.anthropic.claude-opus-4-6-v1:1m', value: 'eu.anthropic.claude-opus-4-6-v1:1m' },
  { label: 'eu.anthropic.claude-opus-4-7', value: 'eu.anthropic.claude-opus-4-7' },
  { label: 'eu.anthropic.claude-opus-4-7:1m', value: 'eu.anthropic.claude-opus-4-7:1m' },
  { label: 'eu.anthropic.claude-opus-4-8', value: 'eu.anthropic.claude-opus-4-8' },
  { label: 'eu.anthropic.claude-opus-4-8:1m', value: 'eu.anthropic.claude-opus-4-8:1m' },
  { label: 'eu.anthropic.claude-opus-5', value: 'eu.anthropic.claude-opus-5' },
  { label: 'eu.anthropic.claude-opus-5:1m', value: 'eu.anthropic.claude-opus-5:1m' },
  { label: 'global.amazon.nova-2-lite-v1:0', value: 'global.amazon.nova-2-lite-v1:0' },
  {
    label: 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
    value: 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
  },
  {
    label: 'global.anthropic.claude-sonnet-4-5-20250929-v1:0',
    value: 'global.anthropic.claude-sonnet-4-5-20250929-v1:0',
  },
  { label: 'global.anthropic.claude-sonnet-4-6', value: 'global.anthropic.claude-sonnet-4-6' },
  { label: 'global.anthropic.claude-sonnet-4-6:1m', value: 'global.anthropic.claude-sonnet-4-6:1m' },
  { label: 'global.anthropic.claude-sonnet-5', value: 'global.anthropic.claude-sonnet-5' },
  { label: 'global.anthropic.claude-sonnet-5:1m', value: 'global.anthropic.claude-sonnet-5:1m' },
  {
    label: 'global.anthropic.claude-opus-4-5-20251101-v1:0',
    value: 'global.anthropic.claude-opus-4-5-20251101-v1:0',
  },
  { label: 'global.anthropic.claude-opus-4-6-v1', value: 'global.anthropic.claude-opus-4-6-v1' },
  { label: 'global.anthropic.claude-opus-4-6-v1:1m', value: 'global.anthropic.claude-opus-4-6-v1:1m' },
  { label: 'global.anthropic.claude-opus-4-7', value: 'global.anthropic.claude-opus-4-7' },
  { label: 'global.anthropic.claude-opus-4-7:1m', value: 'global.anthropic.claude-opus-4-7:1m' },
  { label: 'global.anthropic.claude-opus-4-8', value: 'global.anthropic.claude-opus-4-8' },
  { label: 'global.anthropic.claude-opus-4-8:1m', value: 'global.anthropic.claude-opus-4-8:1m' },
  { label: 'global.anthropic.claude-opus-5', value: 'global.anthropic.claude-opus-5' },
  { label: 'global.anthropic.claude-opus-5:1m', value: 'global.anthropic.claude-opus-5:1m' },
  { label: 'global.xai.grok-4.6', value: 'global.xai.grok-4.6' },
];

// Per-class extraction prompt overrides (override extraction.system_prompt /
// extraction.task_prompt for this class). Absent => use the global prompts.
export const X_AWS_IDP_EXTRACTION_SYSTEM_PROMPT = 'x-aws-idp-extraction-system-prompt';
export const X_AWS_IDP_EXTRACTION_TASK_PROMPT = 'x-aws-idp-extraction-task-prompt';

/**
 * Mark a class as excluded from downstream processing. When true, sections
 * classified as this class are skipped by extraction, assessment,
 * summarization, rule validation, and evaluation. Useful for static
 * boilerplate (instructions, legal warnings, cover pages, etc.).
 */
export const X_AWS_IDP_EXCLUDE_FROM_PROCESSING = 'x-aws-idp-exclude-from-processing';

/**
 * Optional short category for why a class is excluded
 * (e.g. "instructions", "legal", "cover-page"). Shown in UI section badges
 * and in evaluation report annotations.
 */
export const X_AWS_IDP_EXCLUSION_REASON = 'x-aws-idp-exclusion-reason';

/**
 * Class-level: names the top-level array property whose length is the
 * section's instance count, for a class already modelled as a packet of
 * records (one element per document). Purely a signal - it does not change
 * the extraction shape. Must stay in sync with schema_constants.py.
 */
export const X_AWS_IDP_INSTANCE_ARRAY = 'x-aws-idp-instance-array';

/**
 * Class-level: replace the class's EFFECTIVE schema with a List-of-Class
 * wrapper, `{instances: {type: array, items: <the original class schema>}}`, so
 * a section holding several documents of this class extracts every one of them
 * instead of silently returning only the first.
 *
 * The counterpart to X_AWS_IDP_INSTANCE_ARRAY above and mutually exclusive with
 * it: that key NAMES an array the class already has, this one CREATES one.
 * Changes the shape of inference_result, so evaluation baselines for the class
 * must be migrated. Must stay in sync with schema_constants.py.
 */
export const X_AWS_IDP_MULTI_INSTANCE = 'x-aws-idp-multi-instance';

/**
 * Declares the named page sub-types a class can include. Each entry has a
 * `name`, optional `description`, and `x-aws-idp-document-page-content-regex`
 * used to detect the page type from per-page OCR text.
 */
export const X_AWS_IDP_PAGE_TYPES = 'x-aws-idp-page-types';

/**
 * On a property: declares which page sub-types contain the property's
 * source data. If none of the listed page types are present in the section,
 * the property is treated as MISSING (vs BLANK when the page is present
 * but the field is empty).
 */
export const X_AWS_IDP_SOURCE_PAGE_TYPES = 'x-aws-idp-source-page-types';

// ============================================================================
// AWS IDP Validation Engine Extensions (Per-Rule Engine Selection)
// ============================================================================
/** Per-rule validation engine selector (values: "llm" or "z3") */
export const X_AWS_IDP_VALIDATION_ENGINE = 'x-aws-idp-validation-engine';

/** Per-rule unique identifier for Z3 rules (maps to RuleJSON file on S3) */
export const X_AWS_IDP_RULE_ID = 'x-aws-idp-rule-id';

/** Per-rule inline RuleJSON object for Z3 validation (embedded in config) */
export const X_AWS_IDP_RULE_JSON = 'x-aws-idp-rule-json';

// UI-friendly validation engine options for dropdowns
export const VALIDATION_ENGINE_OPTIONS = [
  { label: 'Semantic (LLM)', value: 'llm' },
  { label: 'Symbolic (Z3)', value: 'z3' },
];

// ============================================================================
// AWS IDP List-Specific Extensions
// ============================================================================
/** Description for list items */
export const X_AWS_IDP_LIST_ITEM_DESCRIPTION = 'x-aws-idp-list-item-description';

/** Original attribute name (preserved from legacy format) */
export const X_AWS_IDP_ORIGINAL_NAME = 'x-aws-idp-original-name';

// ============================================================================
// AWS IDP Evaluation Extensions (Stickler-based Baseline Accuracy)
// ============================================================================
/** Evaluation method for baseline comparison */
export const X_AWS_IDP_EVALUATION_METHOD = 'x-aws-idp-evaluation-method';

/** Evaluation threshold for baseline match (0.0 to 1.0) */
export const X_AWS_IDP_EVALUATION_THRESHOLD = 'x-aws-idp-evaluation-threshold';

/** Evaluation weight for field importance (business criticality) */
export const X_AWS_IDP_EVALUATION_WEIGHT = 'x-aws-idp-evaluation-weight';

/** Overall match threshold for document-level evaluation (0.0 to 1.0) */
export const X_AWS_IDP_EVALUATION_MATCH_THRESHOLD = 'x-aws-idp-evaluation-match-threshold';

/** Evaluation model name (optional, defaults to document type name) */
export const X_AWS_IDP_EVALUATION_MODEL_NAME = 'x-aws-idp-evaluation-model-name';

// Valid evaluation methods
export const EVALUATION_METHOD_EXACT = 'EXACT';
export const EVALUATION_METHOD_NUMERIC_EXACT = 'NUMERIC_EXACT';
export const EVALUATION_METHOD_FUZZY = 'FUZZY';
export const EVALUATION_METHOD_LEVENSHTEIN = 'LEVENSHTEIN';
export const EVALUATION_METHOD_SEMANTIC = 'SEMANTIC';
export const EVALUATION_METHOD_DATE = 'DATE';
export const EVALUATION_METHOD_LLM = 'LLM';
export const EVALUATION_METHOD_HUNGARIAN = 'HUNGARIAN';

export const VALID_EVALUATION_METHODS = [
  EVALUATION_METHOD_EXACT,
  EVALUATION_METHOD_NUMERIC_EXACT,
  EVALUATION_METHOD_FUZZY,
  EVALUATION_METHOD_LEVENSHTEIN,
  EVALUATION_METHOD_SEMANTIC,
  EVALUATION_METHOD_DATE,
  EVALUATION_METHOD_LLM,
  EVALUATION_METHOD_HUNGARIAN,
];

// UI-friendly evaluation method options with descriptions and validation metadata
export const EVALUATION_METHOD_OPTIONS = [
  {
    label: 'Exact',
    value: EVALUATION_METHOD_EXACT,
    description: 'Character-by-character match after normalization',
    validFor: [TYPE_STRING, TYPE_NUMBER, TYPE_INTEGER, TYPE_BOOLEAN],
  },
  {
    label: 'Numeric Exact',
    value: EVALUATION_METHOD_NUMERIC_EXACT,
    description: 'Numeric comparison (currency/format normalized)',
    validFor: [TYPE_NUMBER, TYPE_INTEGER, TYPE_STRING],
  },
  {
    label: 'Fuzzy',
    value: EVALUATION_METHOD_FUZZY,
    description: 'Allows minor formatting variations',
    validFor: [TYPE_STRING],
  },
  {
    label: 'Levenshtein',
    value: EVALUATION_METHOD_LEVENSHTEIN,
    description: 'String distance-based matching',
    validFor: [TYPE_STRING],
  },
  {
    label: 'Semantic',
    value: EVALUATION_METHOD_SEMANTIC,
    description: 'Embedding-based meaning comparison',
    validFor: [TYPE_STRING, TYPE_OBJECT],
  },
  {
    label: 'Date',
    value: EVALUATION_METHOD_DATE,
    description: 'Format-insensitive date matching (e.g. 01/05/2024 = 2024-01-05)',
    validFor: [TYPE_STRING],
  },
  {
    label: 'LLM',
    value: EVALUATION_METHOD_LLM,
    description: 'AI-powered functional equivalence',
    validFor: [TYPE_STRING, TYPE_OBJECT],
  },
  {
    label: 'Hungarian',
    value: EVALUATION_METHOD_HUNGARIAN,
    description: 'Optimal matching for arrays of objects (List[Object] only)',
    validFor: [TYPE_ARRAY],
    requiresStructuredItems: true, // Only for arrays with object items
  },
];

// Default threshold values per evaluation method (for regular fields)
// Only methods that use similarity thresholds have defaults here
export const EVALUATION_THRESHOLD_DEFAULTS = {
  [EVALUATION_METHOD_FUZZY]: 0.85,
  [EVALUATION_METHOD_LEVENSHTEIN]: 0.8,
  [EVALUATION_METHOD_SEMANTIC]: 0.7,
  // No defaults for EXACT (binary), NUMERIC_EXACT (uses tolerance), LLM (binary)
};

// Default match_threshold values per evaluation method (for structured arrays)
// Note: Only HUNGARIAN uses match_threshold. LLM evaluates arrays semantically without match_threshold.
export const EVALUATION_MATCH_THRESHOLD_DEFAULTS = {
  [EVALUATION_METHOD_HUNGARIAN]: 0.8,
};

// Methods that require threshold configuration (for non-array fields)
// Only methods that use similarity-based scoring require thresholds
export const METHODS_REQUIRING_THRESHOLD = [
  EVALUATION_METHOD_FUZZY,
  EVALUATION_METHOD_LEVENSHTEIN,
  EVALUATION_METHOD_SEMANTIC,
  // EXACT, NUMERIC_EXACT, and LLM do not use thresholds
];

// Methods that require match_threshold configuration (for structured arrays)
// Note: Only HUNGARIAN uses match_threshold for item-by-item optimal matching.
// LLM evaluates arrays semantically as a whole and does not use match_threshold.
export const METHODS_REQUIRING_MATCH_THRESHOLD = [EVALUATION_METHOD_HUNGARIAN];

// ============================================================================
// AWS IDP Assessment Extensions (Extraction Confidence Alerts)
// ============================================================================
/** Confidence threshold for extraction quality alerts (0.0 to 1.0) */
export const X_AWS_IDP_CONFIDENCE_THRESHOLD = 'x-aws-idp-confidence-threshold';

// ============================================================================
// AWS IDP Prompt Extensions
// ============================================================================
/** Custom prompt override for attribute extraction */
export const X_AWS_IDP_PROMPT_OVERRIDE = 'x-aws-idp-prompt-override';

/** Maximum length for prompt overrides */
export const MAX_PROMPT_OVERRIDE_LENGTH = 10000;

// ============================================================================
// AWS IDP Example Extensions
// ============================================================================
/** Extensions for few-shot example support */
export const X_AWS_IDP_EXAMPLES = 'x-aws-idp-examples';
export const X_AWS_IDP_CLASS_PROMPT = 'x-aws-idp-class-prompt';
export const X_AWS_IDP_ATTRIBUTES_PROMPT = 'x-aws-idp-attributes-prompt';
export const X_AWS_IDP_IMAGE_PATH = 'x-aws-idp-image-path';

// ============================================================================
// Legacy Format Field Names (for migration reference)
// ============================================================================
export const LEGACY_ATTRIBUTES = 'attributes';
export const LEGACY_NAME = 'name';
export const LEGACY_DESCRIPTION = 'description';
export const LEGACY_ATTRIBUTE_TYPE = 'attributeType';
export const LEGACY_GROUP_ATTRIBUTES = 'groupAttributes';
export const LEGACY_LIST_ITEM_TEMPLATE = 'listItemTemplate';
export const LEGACY_ITEM_ATTRIBUTES = 'itemAttributes';
export const LEGACY_ITEM_DESCRIPTION = 'itemDescription';

// Legacy attribute type values (deprecated - use JSON Schema types instead)
export const ATTRIBUTE_TYPE_SIMPLE = 'simple';
export const ATTRIBUTE_TYPE_GROUP = 'group';
export const ATTRIBUTE_TYPE_LIST = 'list';

// UI-friendly legacy attribute options (for migration UI)
export const ATTRIBUTE_TYPE_OPTIONS = [
  { label: 'Simple', value: ATTRIBUTE_TYPE_SIMPLE, description: 'Single value field' },
  { label: 'Group', value: ATTRIBUTE_TYPE_GROUP, description: 'Nested object with properties' },
  { label: 'List', value: ATTRIBUTE_TYPE_LIST, description: 'Array of items' },
];

// ============================================================================
// JSON Schema Format Options (with examples for UI display)
// See: https://json-schema.org/understanding-json-schema/reference/string#built-in-formats
// ============================================================================
export const FORMAT_OPTIONS = [
  { label: 'None', value: '', description: 'No format validation' },
  { label: 'Date (e.g., 2024-12-25)', value: 'date', description: 'ISO 8601 date: YYYY-MM-DD' },
  { label: 'Time (e.g., 14:30:00)', value: 'time', description: 'ISO 8601 time: HH:MM:SS' },
  { label: 'Date-Time (e.g., 2024-12-25T14:30:00Z)', value: 'date-time', description: 'ISO 8601 date-time' },
  { label: 'Duration (e.g., P3Y6M4D)', value: 'duration', description: 'ISO 8601 duration' },
  { label: 'Email (e.g., user@example.com)', value: 'email', description: 'RFC 5321 email address' },
  { label: 'IDN Email (e.g., user@example.com)', value: 'idn-email', description: 'Internationalized email' },
  { label: 'Hostname (e.g., example.com)', value: 'hostname', description: 'RFC 1123 hostname' },
  { label: 'IDN Hostname (e.g., example.com)', value: 'idn-hostname', description: 'Internationalized hostname' },
  { label: 'IPv4 (e.g., 192.168.1.1)', value: 'ipv4', description: 'IPv4 address' },
  { label: 'IPv6 (e.g., 2001:db8::1)', value: 'ipv6', description: 'IPv6 address' },
  { label: 'URI (e.g., https://example.com/path)', value: 'uri', description: 'RFC 3986 URI' },
  { label: 'URI Reference (e.g., /path/to/resource)', value: 'uri-reference', description: 'URI or relative reference' },
  { label: 'IRI (e.g., https://example.com/path)', value: 'iri', description: 'Internationalized URI' },
  { label: 'IRI Reference', value: 'iri-reference', description: 'IRI or relative reference' },
  { label: 'URI Template (e.g., /users/{id})', value: 'uri-template', description: 'RFC 6570 URI template' },
  { label: 'JSON Pointer (e.g., /foo/bar)', value: 'json-pointer', description: 'RFC 6901 JSON pointer' },
  { label: 'Relative JSON Pointer (e.g., 1/foo)', value: 'relative-json-pointer', description: 'Relative JSON pointer' },
  { label: 'Regex (e.g., ^[a-z]+$)', value: 'regex', description: 'ECMA-262 regular expression' },
  { label: 'UUID (e.g., 550e8400-e29b-41d4-a716-446655440000)', value: 'uuid', description: 'RFC 4122 UUID' },
];

// ============================================================================
// Content Encoding Options
// ============================================================================
export const CONTENT_ENCODING_OPTIONS = [
  { label: 'None', value: '' },
  { label: 'Base64', value: 'base64' },
  { label: '7bit', value: '7bit' },
  { label: '8bit', value: '8bit' },
  { label: 'Binary', value: 'binary' },
  { label: 'Quoted-Printable', value: 'quoted-printable' },
];

// ============================================================================
// Default Values
// ============================================================================
/** Default JSON Schema version */
export const DEFAULT_SCHEMA_VERSION = 'https://json-schema.org/draft/2020-12/schema';

/** Default confidence threshold */
export const DEFAULT_CONFIDENCE_THRESHOLD = 0.9;
