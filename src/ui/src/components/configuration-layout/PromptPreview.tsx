// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * PromptPreview component for the Configuration page.
 *
 * Renders a preview of the actual prompts sent to the LLM for each processing step
 * (Classification, Extraction, Confidence Assessment, Summarization) with config-derived
 * placeholders filled in and document-specific placeholders shown as highlighted markers.
 * The Extraction and Confidence previews COMPOSE the template that will actually run given
 * confidence.mode + geometry.mode (mirroring idp_common.extraction.prompt_assembly).
 *
 * This helps users understand what the LLM actually sees, enabling better optimization
 * of document class schemas and prompt templates.
 */

import React, { useState, useMemo, useCallback } from 'react';
import {
  Box,
  SpaceBetween,
  FormField,
  Select,
  Container,
  Header,
  ColumnLayout,
  Badge,
  Tabs,
  CopyToClipboard,
  Alert,
} from '@cloudscape-design/components';
import { DEFS_FIELD, ID_FIELD, REF_FIELD, SCHEMA_FIELD, X_AWS_IDP_MULTI_INSTANCE } from '../../constants/schemaConstants';

// ─── Types ───────────────────────────────────────────────────────────────────

interface ClassSchema {
  $id?: string;
  'x-aws-idp-document-type'?: string;
  type?: string;
  description?: string;
  properties?: Record<string, PropertySchema>;
  [key: string]: unknown;
}

interface PropertySchema {
  type?: string;
  description?: string;
  properties?: Record<string, PropertySchema>;
  items?: PropertySchema;
  [key: string]: unknown;
}

interface StepConfig {
  system_prompt?: string;
  task_prompt?: string;
  model?: string;
  [key: string]: unknown;
}

interface PromptPreviewProps {
  /** Merged configuration values (complete config: default + custom) */
  formValues: Record<string, unknown>;
}

// ─── Constants ───────────────────────────────────────────────────────────────

/** Processing steps available for prompt preview */
const STEPS = [
  { value: 'classification', label: 'Classification' },
  { value: 'extraction', label: 'Extraction' },
  { value: 'confidence', label: 'Confidence Assessment' },
  { value: 'summarization', label: 'Summarization' },
] as const;

type StepName = (typeof STEPS)[number]['value'];

// Geometry modes in which the model is asked to emit bounding boxes (mirrors
// idp_common.extraction.prompt_assembly.geometry_requires_llm_boxes).
const LLM_BOX_GEOMETRY_MODES = ['llm', 'llm_grounded'];

// Splice the bbox block before the first document/cache-point marker so runtime
// document sections stay after it (mirrors the Python _append_bbox_block).
const BBOX_SPLICE_MARKERS = ['<<CACHEPOINT>>', '<document-image>', '{DOCUMENT_IMAGE}'];

function appendBboxBlock(core: string, bboxBlock: string): string {
  if (!core || !bboxBlock) return core;
  if (core.includes('spatial-localization')) return core;
  const block = bboxBlock.replace(/^\n+|\n+$/g, '');
  for (const marker of BBOX_SPLICE_MARKERS) {
    const idx = core.indexOf(marker);
    if (idx !== -1) return `${core.slice(0, idx)}${block}\n\n${core.slice(idx)}`;
  }
  return `${core.replace(/\s+$/, '')}\n\n${block}\n`;
}

// ─── Tool-use constants (mirror the Python that puts them on the wire) ───────

/**
 * The forced-tool name. Mirrors
 * ``idp_common.extraction.forced_tool.EXTRACTION_TOOL_NAME`` — stable there
 * because it is part of the prompt-cache prefix, so it is safe to pin here.
 */
export const EXTRACTION_TOOL_NAME = 'emit_extracted_fields';

/**
 * The tool description sent with the toolSpec. Copied verbatim from
 * ``idp_common.extraction.forced_tool._TOOL_DESCRIPTION``: it is prompt text the
 * model reads, so showing an approximation of it would defeat the point of the
 * preview.
 */
export const EXTRACTION_TOOL_DESCRIPTION =
  "Return the extracted values as the TOP-LEVEL properties of this tool's input, " +
  'exactly as the schema declares them. Do NOT nest them under a wrapper key ' +
  "such as 'fields', 'data' or 'result'. Every value must come from the document " +
  'itself; use null for a field the document does not contain. Do not invent ' +
  'values and do not add properties the schema does not define.';

/**
 * Schema-DOCUMENT keywords stripped before a schema may travel as a
 * ``toolSpec.inputSchema``. Mirrors
 * ``idp_common.bedrock.tool_schema._DOCUMENT_METADATA_KEYS``, which exists
 * because Bedrock meta-validates ``$id`` as a URI-reference and an IDP class
 * sets it to the class NAME — so ``$id: "Policy Application Form"`` rejects the
 * whole request.
 */
const TOOL_DOCUMENT_METADATA_KEYS: readonly string[] = [ID_FIELD, SCHEMA_FIELD, '$anchor', '$comment', 'id'];

/**
 * The property-name pattern Bedrock enforces on ``toolSpec.inputSchema`` keys.
 * Mirrors ``idp_common.bedrock.tool_schema.TOOL_PROPERTY_NAME_PATTERN``.
 */
const TOOL_PROPERTY_NAME_PATTERN = /^[a-zA-Z0-9_.-]{1,64}$/;

/** The label the backend prefixes to the restated schema (agentic_idp._build_system_prompt). */
const EXPECTED_SCHEMA_LABEL = 'Expected Schema:';

/** Map of placeholder → human-readable description shown in preview */
const DOCUMENT_PLACEHOLDER_LABELS: Record<string, string> = {
  DOCUMENT_TEXT: '📄 [Document OCR text will be inserted here at runtime]',
  DOCUMENT_IMAGE: '🖼️ [Document page image(s) will be inserted here at runtime]',
  EXTRACTION_RESULTS: '📊 [Extraction results JSON will be inserted here at runtime]',
  OCR_TEXT_CONFIDENCE: '🔍 [OCR text with confidence scores will be inserted here at runtime]',
  EXPECTED_VALUE: '✅ [Expected value will be inserted here at runtime]',
  ACTUAL_VALUE: '📝 [Actual extracted value will be inserted here at runtime]',
  FEW_SHOT_EXAMPLES: '📚 [Few-shot examples from class configuration will be inserted here at runtime]',
};

// ─── Utility functions ───────────────────────────────────────────────────────

/**
 * Get the class identifier from a schema object.
 */
function getClassId(schema: ClassSchema): string {
  return schema.$id || schema['x-aws-idp-document-type'] || 'unknown';
}

/**
 * Clean JSON Schema by removing IDP custom fields (x-aws-idp-*) for display.
 * Mirrors the Python _clean_schema_for_prompt() logic in extraction/service.py.
 */
function cleanSchemaForPrompt(schema: Record<string, unknown>): Record<string, unknown> {
  const cleaned: Record<string, unknown> = {};

  for (const [key, value] of Object.entries(schema)) {
    if (key.startsWith('x-aws-idp-')) continue;

    if (value && typeof value === 'object' && !Array.isArray(value)) {
      cleaned[key] = cleanSchemaForPrompt(value as Record<string, unknown>);
    } else if (Array.isArray(value)) {
      cleaned[key] = value.map((item) => (item && typeof item === 'object' ? cleanSchemaForPrompt(item as Record<string, unknown>) : item));
    } else {
      cleaned[key] = value;
    }
  }

  return cleaned;
}

/**
 * Recursively drop the schema-DOCUMENT metadata keys the backend removes before
 * sending a schema as a toolSpec. Mirrors
 * ``idp_common.bedrock.tool_schema.strip_non_wire_keywords`` — including its one
 * subtlety: keys INSIDE ``properties`` are user-authored field names, so a field
 * legitimately named ``id`` must survive.
 */
function stripToolDocumentMetadata(node: unknown): unknown {
  if (Array.isArray(node)) return node.map((item) => stripToolDocumentMetadata(item));
  if (!node || typeof node !== 'object') return node;

  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
    if (TOOL_DOCUMENT_METADATA_KEYS.includes(key)) continue;
    if (key === 'properties' && value && typeof value === 'object' && !Array.isArray(value)) {
      const props: Record<string, unknown> = {};
      for (const [propName, propSchema] of Object.entries(value as Record<string, unknown>)) {
        props[propName] = stripToolDocumentMetadata(propSchema);
      }
      out[key] = props;
      continue;
    }
    out[key] = stripToolDocumentMetadata(value);
  }
  return out;
}

/**
 * The ``inputSchema.json`` a forced extraction tool would carry for this class.
 *
 * Mirrors the two transforms the backend applies that CAN be reproduced in the
 * browser — the ``x-aws-idp-*`` strip (shared with the prose schema) and the
 * document-metadata strip — plus ``build_extraction_tool_config``'s explicit
 * ``type: "object"`` at the root.
 *
 * Deliberately does NOT reproduce ``sanitize_tool_schema``'s property RENAMING.
 * Porting that algorithm (illegal chars → ``_``, 64-char truncation,
 * deterministic collision suffixes) would put a second implementation of a
 * reversible mapping in a second language, and a preview that showed the wrong
 * sanitized spelling would be worse than one that shows the authored name and
 * says renaming happens. So the caller states it instead — see
 * ``invalidToolPropertyNames``.
 */
export function toolInputSchemaFor(schema: ClassSchema): Record<string, unknown> {
  const cleaned = stripToolDocumentMetadata(cleanSchemaForPrompt(schema as Record<string, unknown>)) as Record<string, unknown>;
  return cleaned.type === 'object' ? cleaned : { ...cleaned, type: 'object' };
}

/**
 * Dotted paths of property names Bedrock would reject as toolSpec keys, so the
 * preview can say HOW MANY names get rewritten without claiming to know what
 * they are rewritten to.
 *
 * Mirrors ``idp_common.bedrock.tool_schema.find_invalid_property_names``: a
 * read-only predicate over the same subschema keywords, at every depth (Bedrock
 * checks only the top level today, but the backend sanitizes recursively and
 * depending on a validator staying shallow is not a contract).
 */
export function invalidToolPropertyNames(schema: unknown, path = ''): string[] {
  if (!schema || typeof schema !== 'object' || Array.isArray(schema)) return [];
  const bad: string[] = [];
  for (const [key, value] of Object.entries(schema as Record<string, unknown>)) {
    if (key === 'properties' && value && typeof value === 'object' && !Array.isArray(value)) {
      for (const [propName, propSchema] of Object.entries(value as Record<string, unknown>)) {
        const here = path ? `${path}.${propName}` : propName;
        if (!TOOL_PROPERTY_NAME_PATTERN.test(propName)) bad.push(here);
        bad.push(...invalidToolPropertyNames(propSchema, here));
      }
    } else if (key === 'items') {
      bad.push(...invalidToolPropertyNames(value, `${path}[]`));
    } else if (key === DEFS_FIELD && value && typeof value === 'object' && !Array.isArray(value)) {
      for (const [defName, defSchema] of Object.entries(value as Record<string, unknown>)) {
        bad.push(...invalidToolPropertyNames(defSchema, `${DEFS_FIELD}/${defName}`));
      }
    } else if (['anyOf', 'allOf', 'oneOf', 'prefixItems'].includes(key) && Array.isArray(value)) {
      for (const branch of value) bad.push(...invalidToolPropertyNames(branch, path));
    }
  }
  return bad;
}

/**
 * Format class names and descriptions for classification prompts.
 * Mirrors the Python _format_classes_list() logic in classification/service.py.
 */
function formatClassNamesAndDescriptions(classes: ClassSchema[]): string {
  return classes
    .map((cls) => {
      const name = getClassId(cls);
      const description = cls.description || '';
      return `${name}  \t[ ${description} ]`;
    })
    .join('\n');
}

/**
 * Soft cap on the number of schema attribute names emitted per class.
 * Must mirror ``ClassificationService.MAX_ATTRIBUTES_PER_CLASS`` in the
 * Python backend.
 */
const MAX_ATTRIBUTES_PER_CLASS = 50;

/**
 * Hard ceiling on names the walk itself builds. Must mirror
 * ``ClassificationService._MAX_WALK_NAMES``: dereferencing lets a non-cyclic
 * ``$defs`` DAG be re-entered on every sibling branch, so a small schema can
 * expand combinatorially and the soft cap above (applied to the RESULT) never
 * sees it.
 */
const MAX_WALK_NAMES = 10 * MAX_ATTRIBUTES_PER_CLASS;

/**
 * Resolve a local ``#/$defs/<name>`` ``$ref`` against the class schema.
 *
 * Mirrors ``deref_schema`` in ``idp_common/config/schema_utils.py``: sibling
 * keys on the referencing node win over the definition's, ``$ref`` chains are
 * followed, and anything unresolvable (remote ``$ref``, dangling name, cycle)
 * is returned as-is so the preview degrades rather than throwing.
 */
function derefSchema(node: PropertySchema, root: ClassSchema, seen: Set<string> = new Set()): PropertySchema {
  if (!node || typeof node !== 'object') return {};
  const ref = node[REF_FIELD];
  if (typeof ref !== 'string') return node;

  const prefix = `#/${DEFS_FIELD}/`;
  if (!ref.startsWith(prefix)) return node;
  if (seen.has(ref)) return node;
  seen.add(ref);

  const defs = root[DEFS_FIELD] as Record<string, PropertySchema> | undefined;
  const target = defs?.[ref.slice(prefix.length)];
  if (!target || typeof target !== 'object') return node;

  const { [REF_FIELD]: _dropped, ...siblings } = node;
  const merged = { ...target, ...siblings } as PropertySchema;
  return REF_FIELD in target ? derefSchema(merged, root, seen) : merged;
}

/**
 * Recursively walk a JSON Schema ``properties`` object and yield a flat
 * list of dotted-path attribute names.
 *
 * Mirrors ``ClassificationService._get_attribute_names_for_class`` in the
 * Python backend:
 *   - Flat scalars surface by their property name.
 *   - Nested ``object`` properties are flattened to dotted paths
 *     (e.g. ``borrower.address.zip``).
 *   - Arrays of objects are unwrapped — item-properties are surfaced as
 *     ``parent.child`` (no ``[]`` indexing).
 *   - Scalar arrays surface by their parent name only.
 *   - Groups and list-item shapes behind a local ``$ref`` into ``$defs``
 *     (what the schema editor emits) are dereferenced first, so a ``$ref``
 *     group contributes its child names exactly like an inline one. Without
 *     this the preview shows a bare ``Signatures`` while the prompt the model
 *     actually receives contains ``Signatures.Signature-of-taxpayer1``.
 *   - A ``$ref`` already entered on the current branch is emitted as a leaf
 *     rather than re-entered, so a recursive ``$defs`` terminates.
 */
/** String spellings pydantic coerces to a bool, so a hand-edited YAML `enabled: yes` is read the same way here. */
const PYDANTIC_TRUE_STRINGS = ['true', 'yes', 'on', '1', 't', 'y'];
const PYDANTIC_FALSE_STRINGS = ['false', 'no', 'off', '0', 'f', 'n'];

/**
 * Read a config flag the way pydantic would, with an explicit default for an
 * ABSENT key.
 *
 * `whenAbsent` is a required argument on purpose: `forced_tool.enabled` defaults
 * FALSE while `agentic.restate_schema_in_system_prompt` defaults TRUE, so a
 * missing key means the OPPOSITE thing in the two cases. A single truthiness
 * test — which is what this replaced — silently gets the second one wrong, and
 * the wrong answer there is "the preview under-reports the prompt by ~40%".
 * A value that is neither a recognised true nor a recognised false spelling
 * falls back to the default rather than to `false`, for the same reason.
 */
const boolish = (value: unknown, whenAbsent: boolean): boolean => {
  if (value === undefined || value === null) return whenAbsent;
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  if (typeof value === 'string') {
    const s = value.trim().toLowerCase();
    if (PYDANTIC_TRUE_STRINGS.includes(s)) return true;
    if (PYDANTIC_FALSE_STRINGS.includes(s)) return false;
    return whenAbsent;
  }
  return whenAbsent;
};

/**
 * The effective extraction mode. Mirrors
 * ``ExtractionConfig.reconcile_mode_and_agentic``: ``mode`` is authoritative
 * when present, and a legacy config that omits it has the mode inferred from
 * ``agentic.enabled``. Everything mode-dependent below goes through this, so
 * "Simple" and "Advanced" mean in the preview exactly what they mean server-side.
 */
export const extractionModeOf = (formValues: Record<string, unknown> | null | undefined): 'simple' | 'advanced' => {
  const extraction = (formValues?.extraction as Record<string, unknown>) || {};
  const raw = extraction.mode;
  if (typeof raw === 'string' && raw.trim()) return raw.trim().toLowerCase() === 'advanced' ? 'advanced' : 'simple';
  const agentic = (extraction.agentic as Record<string, unknown>) || {};
  return boolish(agentic.enabled, false) ? 'advanced' : 'simple';
};

/**
 * The server-side schema transforms this browser-side preview cannot show, plus
 * the two config-derived blocks it now reconstructs.
 *
 * Exported and pure so it can be tested without mounting the whole preview. The
 * alert's only job is to be *accurate* about what the model receives, so a wrong
 * bullet is worse than no bullet — and the step- and mode-dependence below is
 * exactly the kind of detail that drifts silently:
 *
 * - `wrapped` applies to BOTH stages: `ExtractionService._get_class_schema` and
 *   `AssessmentService._get_class_schema` each wrap independently from config.
 * - `probe` applies to the EXTRACTION step ONLY, and to Simple mode only. It is
 *   added in `ExtractionService._build_wire_schema`, which returns the schema
 *   untouched when `agentic.enabled` — so claiming it in Advanced mode, or on the
 *   confidence step (assessment wraps but never augments), made this alert state
 *   something false. It applies to every class when detection is on, not just a
 *   flagged one, which is why it comes from the config rather than the class.
 * - `forcedTool` is Simple-only: the forced toolConfig is built in the non-agentic
 *   branch of `_invoke_extraction_model`, and Advanced already sends the schema as
 *   the Strands tool. Assessment never sends a forced tool at all.
 * - `restatesSchema` is Advanced-only and DEFAULTS TRUE — see `boolish`.
 */
export const schemaDivergenceFor = (
  formValues: Record<string, unknown> | null | undefined,
  selectedClass: Record<string, unknown> | null | undefined,
  selectedStep: string,
): { wrapped: boolean; probe: boolean; forcedTool: boolean; restatesSchema: boolean } => {
  const extraction = (formValues?.extraction as Record<string, unknown>) || {};
  const detection = (extraction.multi_instance_detection as Record<string, unknown>) || {};
  const forcedTool = (extraction.forced_tool as Record<string, unknown>) || {};
  const agentic = (extraction.agentic as Record<string, unknown>) || {};
  const mode = extractionModeOf(formValues);
  const isExtraction = selectedStep === 'extraction';
  return {
    wrapped: Boolean(selectedClass?.[X_AWS_IDP_MULTI_INSTANCE]),
    probe: boolish(detection.enabled, false) && mode === 'simple' && isExtraction,
    forcedTool: boolish(forcedTool.enabled, false) && mode === 'simple' && isExtraction,
    restatesSchema: boolish(agentic.restate_schema_in_system_prompt, true) && mode === 'advanced' && isExtraction,
  };
};

export function getAttributeNamesForClass(cls: ClassSchema): string[] {
  const properties = cls.properties;
  if (!properties || typeof properties !== 'object') return [];

  const names: string[] = [];
  const seen = new Set<string>();

  const walk = (props: Record<string, PropertySchema>, parentPath = '', activeRefs: ReadonlySet<string> = new Set()): void => {
    for (const [propName, rawSchema] of Object.entries(props)) {
      if (names.length >= MAX_WALK_NAMES) return;
      if (!rawSchema || typeof rawSchema !== 'object') continue;
      const fullPath = parentPath ? `${parentPath}.${propName}` : propName;

      const emit = (): void => {
        if (!seen.has(fullPath)) {
          seen.add(fullPath);
          names.push(fullPath);
        }
      };

      const ref = rawSchema[REF_FIELD];
      if (typeof ref === 'string' && activeRefs.has(ref)) {
        emit();
        continue;
      }
      const branchRefs = typeof ref === 'string' ? new Set([...activeRefs, ref]) : activeRefs;

      const propSchema = derefSchema(rawSchema, cls);
      const propType = propSchema.type;

      if (propType === 'object') {
        const nested = propSchema.properties;
        if (nested && typeof nested === 'object' && Object.keys(nested).length > 0) {
          walk(nested, fullPath, branchRefs);
          continue;
        }
        // Object with no declared properties — emit the parent name itself.
      } else if (propType === 'array') {
        // ``items`` may legally be a LIST (draft-07 tuple form), so only a
        // plain object is safe to read keys off.
        const itemsRaw =
          propSchema.items && typeof propSchema.items === 'object' && !Array.isArray(propSchema.items) ? propSchema.items : {};
        const itemsRef = itemsRaw[REF_FIELD];
        if (!(typeof itemsRef === 'string' && branchRefs.has(itemsRef))) {
          const itemsSchema = derefSchema(itemsRaw, cls);
          if (itemsSchema.type === 'object') {
            const nested = itemsSchema.properties;
            if (nested && typeof nested === 'object' && Object.keys(nested).length > 0) {
              const itemRefs = typeof itemsRef === 'string' ? new Set([...branchRefs, itemsRef]) : branchRefs;
              walk(nested, fullPath, itemRefs);
              continue;
            }
          }
        }
        // Scalar array (or array with no item properties) — emit the parent.
      }

      emit();
    }
  };

  walk(properties);
  return names;
}

/**
 * Format the optional ``{CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}``
 * placeholder as an XML-tagged listing for page-level classification
 * prompts. Mirrors
 * ``ClassificationService._format_classes_and_attributes_list()`` in the
 * Python backend, including the soft cap and ``...(+N more)`` truncation
 * marker for classes with > MAX_ATTRIBUTES_PER_CLASS schema attributes.
 */
function formatClassAndAttributeNamesAndDescriptions(classes: ClassSchema[]): string {
  return classes
    .map((cls) => {
      const className = getClassId(cls);
      const description = cls.description || '';
      const attrNames = getAttributeNamesForClass(cls);
      let attrsText: string;
      if (attrNames.length === 0) {
        attrsText = '(no schema)';
      } else if (attrNames.length > MAX_ATTRIBUTES_PER_CLASS) {
        const overflow = attrNames.length - MAX_ATTRIBUTES_PER_CLASS;
        attrsText = `${attrNames.slice(0, MAX_ATTRIBUTES_PER_CLASS).join(', ')}, ...(+${overflow} more)`;
      } else {
        attrsText = attrNames.join(', ');
      }
      return `<class name="${className}">\n  <description>${description}</description>\n  <attributes>${attrsText}</attributes>\n</class>`;
    })
    .join('\n');
}

/**
 * Format a class schema as cleaned JSON for extraction/assessment prompts.
 * Mirrors the Python _format_schema_for_prompt() logic in extraction/service.py.
 */
function formatSchemaForPrompt(schema: ClassSchema): string {
  const cleaned = cleanSchemaForPrompt(schema as Record<string, unknown>);
  return JSON.stringify(cleaned, null, 2);
}

/**
 * The ``toolSpec`` a forced extraction request carries.
 *
 * All three members are billed input, which is the whole reason this is previewed
 * as one object rather than as a bare schema.
 */
function toolSpecFor(schema: ClassSchema): Record<string, unknown> {
  return {
    name: EXTRACTION_TOOL_NAME,
    description: EXTRACTION_TOOL_DESCRIPTION,
    inputSchema: { json: toolInputSchemaFor(schema) },
  };
}

/** The toolSpec pretty-printed, for reading and copying. */
export function formatToolSpecForPreview(schema: ClassSchema): string {
  return JSON.stringify(toolSpecFor(schema), null, 2);
}

/**
 * The toolSpec as it is actually serialized onto the request — compact.
 *
 * Used for the token estimate while the pretty form is displayed, because the
 * indentation is a display choice of this component, not something the model is
 * billed for. On the lending ``Payslip`` class the difference is ~1,250 vs
 * ~1,780 estimated tokens, so counting the pretty form would overstate the cost
 * of Schema Enforcement by ~40% — the opposite of the reason this tab exists.
 * (Contrast the prose schema in the task prompt, which the backend really does
 * indent with ``json.dumps(indent=2)``.)
 */
export function toolSpecWireText(schema: ClassSchema): string {
  return JSON.stringify(toolSpecFor(schema));
}

/**
 * The ``Expected Schema:`` block the agentic path appends to the SYSTEM prompt
 * when ``agentic.restate_schema_in_system_prompt`` is on (the default).
 *
 * Mirrors ``agentic_idp._build_system_prompt``'s concatenation, but the JSON is an
 * APPROXIMATION and the UI says so. The backend restates
 * ``model_json_schema()`` of a Pydantic model generated from the class
 * (``create_pydantic_model_from_json_schema``), and that generated schema is a
 * different document from the class schema in both directions:
 *
 * - larger, dominantly: every field gains a ``title``, every optional field
 *   becomes ``anyOf: [<type>, {"type": "null"}]`` with ``default: null``, and
 *   groups become ``$defs`` entries with their own titles;
 * - smaller, marginally: object-level ``description`` is dropped (the class
 *   description on every shipped preset, plus ``$defs`` group descriptions).
 *
 * Net, on the shipped lending presets, the real block is ~1.7x (Payslip) to ~2.9x
 * (W2) the size of what the browser can render. So this UNDER-states the real
 * restatement — which is still far better than the previous behaviour of omitting
 * it entirely, but it must not be presented as exact. Reproducing the generator
 * in TypeScript is not worth a second implementation of a Pydantic code
 * generator; saying what it does is.
 */
export function expectedSchemaBlockFor(schema: ClassSchema): string {
  return `${EXPECTED_SCHEMA_LABEL}\n${formatSchemaForPrompt(schema)}`;
}

/**
 * Estimate token count from text (rough approximation: ~4 chars per token for English).
 */
function estimateTokens(text: string): number {
  if (!text) return 0;
  return Math.ceil(text.length / 4);
}

/** Delimiters used to mark config-substituted values for highlighting */
const CONFIG_START = '\u00AB'; // «
const CONFIG_END = '\u00BB'; // »

/**
 * Fill config-derived placeholders in a prompt template and mark document-specific ones.
 *
 * Config-derived placeholders (filled with actual values and wrapped in « » markers):
 *   - {CLASS_NAMES_AND_DESCRIPTIONS} → formatted class list
 *   - {ATTRIBUTE_NAMES_AND_DESCRIPTIONS} → cleaned JSON schema
 *   - {DOCUMENT_CLASS} → selected class name
 *
 * Document-specific placeholders (replaced with descriptive markers):
 *   - {DOCUMENT_TEXT}, {DOCUMENT_IMAGE}, {EXTRACTION_RESULTS}, etc.
 *
 * Also strips <<CACHEPOINT>> markers for clean display.
 */
function renderPrompt(template: string, configSubstitutions: Record<string, string>): string {
  if (!template) return '';

  let result = template;

  // Fill config-derived placeholders, wrapping substituted values in markers for highlighting
  for (const [key, value] of Object.entries(configSubstitutions)) {
    result = result.replace(new RegExp(`\\{${key}\\}`, 'g'), `${CONFIG_START}${value}${CONFIG_END}`);
  }

  // Replace document-specific placeholders with descriptive markers
  for (const [placeholder, label] of Object.entries(DOCUMENT_PLACEHOLDER_LABELS)) {
    result = result.replace(new RegExp(`\\{${placeholder}\\}`, 'g'), label);
  }

  // Strip <<CACHEPOINT>> markers
  result = result.replace(/<<CACHEPOINT>>/g, '');

  // Clean up excessive blank lines
  result = result.replace(/\n{3,}/g, '\n\n');

  return result.trim();
}

// ─── Styles ──────────────────────────────────────────────────────────────────

const promptPreviewStyles = `
  .prompt-preview-content {
    font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', 'Consolas', monospace;
    font-size: 13px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-wrap: break-word;
    background-color: #fafafa;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 16px;
    min-height: 200px;
    height: 50vh;
    overflow-y: auto;
    overflow-x: auto;
    color: #1a1a1a;
    resize: vertical;
  }

  .prompt-preview-content .runtime-placeholder {
    background-color: #fff3cd;
    border: 1px solid #ffc107;
    border-radius: 4px;
    padding: 2px 6px;
    font-style: italic;
    color: #856404;
    display: inline;
  }

  .prompt-preview-content .config-value {
    background-color: #d4edda;
    border: 1px solid #28a745;
    border-radius: 4px;
    padding: 2px 6px;
    color: #155724;
  }

  .prompt-stats-row {
    display: flex;
    gap: 16px;
    align-items: center;
    flex-wrap: wrap;
  }

  .prompt-stat-item {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 13px;
    color: #545b64;
  }
`;

// ─── Sub-components ──────────────────────────────────────────────────────────

/**
 * Renders a prompt template with syntax highlighting for placeholders.
 * Config-derived values are shown in green, runtime placeholders in yellow.
 */
const HighlightedPrompt = ({ text }: { text: string }): React.JSX.Element => {
  if (!text) {
    return (
      <Box color="text-body-secondary">
        <span style={{ fontStyle: 'italic' }}>No prompt template configured for this step.</span>
      </Box>
    );
  }

  // Parse text into segments: plain text, runtime placeholders (yellow), and config values (green)
  // Config values are wrapped in « » markers by renderPrompt()
  // Runtime placeholders start with emoji prefixes (📄, 🖼️, etc.)
  const segments: React.ReactNode[] = [];
  let keyCounter = 0;

  // Combined regex: match either config-value markers «...» or runtime placeholder markers
  const combinedRegex = /(\u00AB[\s\S]*?\u00BB)|((?:📄|🖼️|📊|🔍|✅|📝|📚)\s*\[.*?\])/gu;
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = combinedRegex.exec(text)) !== null) {
    // Add plain text before this match
    if (match.index > lastIndex) {
      segments.push(text.substring(lastIndex, match.index));
    }

    if (match[1]) {
      // Config-substituted value (green) — strip the « » delimiters
      const configContent = match[1].substring(1, match[1].length - 1);
      segments.push(
        <span key={`cv-${keyCounter++}`} className="config-value">
          {configContent}
        </span>,
      );
    } else if (match[2]) {
      // Runtime placeholder (yellow)
      segments.push(
        <span key={`ph-${keyCounter++}`} className="runtime-placeholder">
          {match[2]}
        </span>,
      );
    }

    lastIndex = match.index + match[0].length;
  }

  // Add any remaining plain text after the last match
  if (lastIndex < text.length) {
    segments.push(text.substring(lastIndex));
  }

  return <div className="prompt-preview-content">{segments}</div>;
};

/**
 * Stats bar showing token estimates and model info.
 *
 * The total must cover everything config puts on the wire, not just the prose
 * prompts: a user who turns Schema Enforcement on and sees the total move by 0
 * has been told, by the one number they came here to read, that forcing is free.
 * `toolSpec` is the empty string when no forced tool is sent, so the stat and its
 * contribution to the total disappear together.
 */
const PromptStats = ({
  systemPrompt,
  taskPrompt,
  toolSpec,
  model,
}: {
  systemPrompt: string;
  taskPrompt: string;
  toolSpec: string;
  model: string;
}): React.JSX.Element => {
  const systemTokens = estimateTokens(systemPrompt);
  const taskTokens = estimateTokens(taskPrompt);
  const toolTokens = estimateTokens(toolSpec);
  const totalTokens = systemTokens + taskTokens + toolTokens;

  return (
    <div className="prompt-stats-row">
      <div className="prompt-stat-item">
        <Badge color="blue">Model</Badge>
        <span>{model || 'Not configured'}</span>
      </div>
      <div className="prompt-stat-item">
        <Badge color="grey">System</Badge>
        <span>~{systemTokens.toLocaleString()} tokens</span>
      </div>
      <div className="prompt-stat-item">
        <Badge color="grey">Task</Badge>
        <span>~{taskTokens.toLocaleString()} tokens</span>
      </div>
      {toolTokens > 0 && (
        <div className="prompt-stat-item">
          <Badge color="grey">Tool schema</Badge>
          <span>~{toolTokens.toLocaleString()} tokens</span>
        </div>
      )}
      <div className="prompt-stat-item">
        <Badge color="green">Total (est.)</Badge>
        <span>~{totalTokens.toLocaleString()} tokens</span>
      </div>
      <div className="prompt-stat-item" style={{ marginLeft: 'auto', fontSize: '12px', color: '#888' }}>
        Covers the system prompt, task prompt{toolTokens > 0 ? ' and tool schema' : ''}. Excludes document text and page images, few-shot
        examples, and the model&apos;s reply.
      </div>
    </div>
  );
};

// ─── Main Component ──────────────────────────────────────────────────────────

const PromptPreview = ({ formValues }: PromptPreviewProps): React.JSX.Element => {
  const [selectedStep, setSelectedStep] = useState<StepName>('classification');
  const [selectedClassId, setSelectedClassId] = useState<string | null>(null);

  // Extract classes from config
  const classes = useMemo((): ClassSchema[] => {
    const raw = formValues?.classes;
    if (!Array.isArray(raw)) return [];
    return raw as ClassSchema[];
  }, [formValues?.classes]);

  // Build class options for the dropdown
  const classOptions = useMemo(
    () =>
      classes.map((cls) => ({
        value: getClassId(cls),
        label: `${getClassId(cls)}${cls.description ? ` — ${cls.description.substring(0, 60)}` : ''}`,
      })),
    [classes],
  );

  // Auto-select first class when classes change or class selection is cleared
  React.useEffect(() => {
    if (classes.length > 0 && !selectedClassId) {
      setSelectedClassId(getClassId(classes[0]));
    } else if (classes.length === 0) {
      setSelectedClassId(null);
    }
  }, [classes, selectedClassId]);

  // Get the step config (system_prompt, task_prompt, model). For the v0.6
  // 'extraction' and 'confidence' views this composes the actual template that
  // will run given confidence.mode + geometry.mode (mirrors the Python
  // prompt_assembly selectors), so the preview reflects real behavior.
  const stepConfig = useMemo((): StepConfig => {
    const extraction = (formValues?.extraction as Record<string, unknown>) || {};
    const confidence = (extraction.confidence as Record<string, unknown>) || {};
    const geometry = (extraction.geometry as Record<string, unknown>) || {};
    const mode = String(confidence.mode ?? 'separate');
    const geomMode = String(geometry.mode ?? 'ocr_only');
    const needsBbox = LLM_BOX_GEOMETRY_MODES.includes(geomMode);
    // v0.6: bbox block lives under geometry; confidence prompt under confidence.
    const bboxBlock = String(geometry.task_prompt_bbox ?? '');

    if (selectedStep === 'extraction') {
      const integrated = mode === 'integrated';
      let task = String(extraction.task_prompt ?? '');
      if (integrated) {
        task = String(extraction.task_prompt_extraction_with_confidence ?? '') || String(extraction.task_prompt ?? '');
        if (needsBbox) task = appendBboxBlock(task, bboxBlock);
      }
      return {
        system_prompt: String(extraction.system_prompt ?? ''),
        task_prompt: task,
        model: String(extraction.model ?? ''),
      };
    }

    if (selectedStep === 'confidence') {
      let task = String(confidence.task_prompt ?? '');
      if (needsBbox) task = appendBboxBlock(task, bboxBlock);
      return {
        system_prompt: String(confidence.system_prompt ?? ''),
        task_prompt: task,
        model: String(confidence.model ?? ''),
      };
    }

    const cfg = formValues?.[selectedStep];
    if (!cfg || typeof cfg !== 'object') return {};
    return cfg as StepConfig;
  }, [formValues, selectedStep]);

  // Whether this step needs a class selection
  const needsClassSelection = selectedStep === 'extraction' || selectedStep === 'confidence';

  // Get selected class schema
  const selectedClass = useMemo((): ClassSchema | null => {
    if (!selectedClassId) return null;
    return classes.find((cls) => getClassId(cls) === selectedClassId) || null;
  }, [classes, selectedClassId]);

  const schemaDivergence = useMemo(
    () => schemaDivergenceFor(formValues, selectedClass, selectedStep),
    [formValues, selectedClass, selectedStep],
  );

  // Build config-derived substitutions based on the selected step
  const buildSubstitutions = useCallback((): Record<string, string> => {
    const subs: Record<string, string> = {};

    switch (selectedStep) {
      case 'classification':
        subs.CLASS_NAMES_AND_DESCRIPTIONS = formatClassNamesAndDescriptions(classes);
        // Optional opt-in placeholder. Only materialized into the rendered
        // prompt if the template references it; substitutions for
        // unreferenced keys are no-ops.
        subs.CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS = formatClassAndAttributeNamesAndDescriptions(classes);
        break;

      case 'extraction':
        if (selectedClass) {
          subs.DOCUMENT_CLASS = getClassId(selectedClass);
          subs.ATTRIBUTE_NAMES_AND_DESCRIPTIONS = formatSchemaForPrompt(selectedClass);
        } else {
          subs.DOCUMENT_CLASS = '[No class selected]';
          subs.ATTRIBUTE_NAMES_AND_DESCRIPTIONS = '[No class selected]';
        }
        break;

      case 'confidence':
        if (selectedClass) {
          subs.DOCUMENT_CLASS = getClassId(selectedClass);
          subs.ATTRIBUTE_NAMES_AND_DESCRIPTIONS = formatSchemaForPrompt(selectedClass);
        } else {
          subs.DOCUMENT_CLASS = '[No class selected]';
          subs.ATTRIBUTE_NAMES_AND_DESCRIPTIONS = '[No class selected]';
        }
        break;

      case 'summarization':
        // Summarization only has document-specific placeholders
        break;
    }

    return subs;
  }, [selectedStep, classes, selectedClass]);

  // Render the prompts. In Advanced mode the agentic path appends the class
  // schema to the SYSTEM prompt, so the preview appends it too — otherwise the
  // System tab under-reports the real prompt by roughly 40% on a mid-sized class,
  // and toggling the one knob whose entire purpose is token count moves nothing.
  // It is appended AFTER renderPrompt and wrapped in the config-value markers, so
  // it renders as a config-derived block rather than as authored template text —
  // and the Raw System Template tab still shows only what the user typed.
  const { renderedSystemPrompt, renderedTaskPrompt, rawSystemPrompt, rawTaskPrompt } = useMemo(() => {
    const subs = buildSubstitutions();
    const sysTemplate = stepConfig.system_prompt || '';
    const taskTemplate = stepConfig.task_prompt || '';
    let renderedSystem = renderPrompt(sysTemplate, subs);
    if (schemaDivergence.restatesSchema && selectedClass) {
      renderedSystem = `${renderedSystem}\n\n${CONFIG_START}${expectedSchemaBlockFor(selectedClass)}${CONFIG_END}`;
    }

    return {
      renderedSystemPrompt: renderedSystem,
      renderedTaskPrompt: renderPrompt(taskTemplate, subs),
      rawSystemPrompt: sysTemplate,
      rawTaskPrompt: taskTemplate,
    };
  }, [stepConfig, buildSubstitutions, schemaDivergence.restatesSchema, selectedClass]);

  // The forced toolSpec, when one is actually sent. Empty otherwise, which is what
  // keeps it out of both the tab list and the token total. Two forms: the pretty
  // one is displayed and copied, the compact one is what the tokens are counted
  // from — see toolSpecWireText.
  const toolSpecText = useMemo(
    () => (schemaDivergence.forcedTool && selectedClass ? formatToolSpecForPreview(selectedClass) : ''),
    [schemaDivergence.forcedTool, selectedClass],
  );

  const toolSpecWire = useMemo(
    () => (schemaDivergence.forcedTool && selectedClass ? toolSpecWireText(selectedClass) : ''),
    [schemaDivergence.forcedTool, selectedClass],
  );

  // Counted off the CLEANED schema, so an `x-aws-idp-*` extension carrying its own
  // nested payload can never contribute a phantom "renamed" name.
  const renamedToolProperties = useMemo(
    () => (schemaDivergence.forcedTool && selectedClass ? invalidToolPropertyNames(toolInputSchemaFor(selectedClass)) : []),
    [schemaDivergence.forcedTool, selectedClass],
  );

  return (
    <SpaceBetween size="m">
      <style>{promptPreviewStyles}</style>

      {/* Controls row */}
      <Container>
        <ColumnLayout columns={needsClassSelection ? 2 : 1}>
          <FormField label="Processing Step" description="Select a pipeline step to preview its prompts">
            <Select
              selectedOption={STEPS.find((s) => s.value === selectedStep) || null}
              onChange={({ detail }) => setSelectedStep(detail.selectedOption.value as StepName)}
              options={[...STEPS]}
            />
          </FormField>

          {needsClassSelection && (
            <FormField label="Document Class" description="Select a class to see how its schema appears in the prompt">
              <Select
                selectedOption={classOptions.find((o) => o.value === selectedClassId) || null}
                onChange={({ detail }) => setSelectedClassId(detail.selectedOption.value ?? null)}
                options={classOptions}
                placeholder="Select a document class..."
                empty="No document classes configured"
              />
            </FormField>
          )}
        </ColumnLayout>
      </Container>

      {/* Settings-aware note: which composed prompt is shown for this step */}
      {(selectedStep === 'extraction' || selectedStep === 'confidence') &&
        (() => {
          const extraction = (formValues?.extraction as Record<string, unknown>) || {};
          const confidence = (extraction.confidence as Record<string, unknown>) || {};
          const geometry = (extraction.geometry as Record<string, unknown>) || {};
          const mode = String(confidence.mode ?? 'separate'); // off | separate | integrated
          const enabled = mode !== 'off';
          const geomMode = String(geometry.mode ?? 'ocr_only');
          const bbox = LLM_BOX_GEOMETRY_MODES.includes(geomMode)
            ? ` The bounding-box instruction block is appended (Geometry mode: ${geomMode}).`
            : ` No bounding-box block (Geometry mode: ${geomMode}).`;
          let msg: string;
          if (selectedStep === 'extraction') {
            msg =
              mode === 'integrated'
                ? `Integrated confidence mode: showing the extraction + confidence template (one inference emits value and confidence).${bbox}`
                : 'Showing the extraction-only template (confidence scoring is off or runs separately).';
          } else if (!enabled) {
            msg = 'Confidence scoring mode is Off — this prompt is not used.';
          } else if (mode === 'integrated') {
            msg =
              'Integrated confidence mode: confidence is produced inside the Extraction inference — this confidence-only prompt is NOT used. See the Extraction step.';
          } else {
            msg = `Separate confidence mode: this prompt runs as a second inference (Advanced in-shard) or the standalone Assessment step.${bbox}`;
          }
          return <Alert type={selectedStep === 'confidence' && !enabled ? 'warning' : 'info'}>{msg}</Alert>;
        })()}

      {/* The preview is built in the browser from the class schema as stored, so
          some server-side transforms are not reflected exactly. Saying so is much
          cheaper — and much less likely to rot — than duplicating the Python
          transforms in TypeScript and keeping them in sync. Two are not shown at
          all (the instances[] wrap and the detection probe; both steps render the
          class schema and BOTH wrap it server-side, in
          ExtractionService._get_class_schema / AssessmentService._get_class_schema,
          so both previews diverge identically), and two ARE now reconstructed but
          are worth naming anyway: the forced toolSpec, whose property names are
          renamed on the wire, and the agentic system-prompt restatement, whose JSON
          comes from a generated Pydantic model rather than from the class. */}
      {['extraction', 'confidence'].includes(selectedStep) &&
      (schemaDivergence.wrapped || schemaDivergence.probe || schemaDivergence.forcedTool || schemaDivergence.restatesSchema) ? (
        <Alert type="warning" header="The real prompt differs from this preview">
          <SpaceBetween size="xxs">
            {schemaDivergence.forcedTool && (
              <span>
                <strong>Schema Enforcement</strong> is on, so the schema also travels as a required tool — see the{' '}
                <strong>Tool Schema</strong> tab, which is counted in the token total. Property names outside{' '}
                <code>^[a-zA-Z0-9_.-]{'{1,64}'}$</code> are rewritten to a wire-safe spelling for the request and restored to exactly what
                you authored in the stored result, so the tab shows your names, not the sanitized ones
                {renamedToolProperties.length > 0 ? ` (${renamedToolProperties.length} on this class)` : ''}.
              </span>
            )}
            {schemaDivergence.restatesSchema && (
              <span>
                <strong>Repeat the document schema in the agent&apos;s system prompt</strong> is on, so the <strong>System Prompt</strong>{' '}
                tab below ends with the <code>Expected Schema:</code> block the agent receives — an <strong>approximation</strong> of it.
                The backend restates a JSON Schema generated from a Pydantic model built from this class, not the class schema itself: every
                field there gains a <code>title</code>, every optional field becomes an <code>anyOf</code> with{' '}
                <code>{'{"type": "null"}'}</code>, and object-level descriptions are dropped. The real block is therefore substantially
                LARGER than the one shown (on the shipped lending presets, roughly 1.7–2.9x), so treat this as a floor on the cost of
                leaving the restatement on.
              </span>
            )}
            {schemaDivergence.wrapped && (
              <span>
                <strong>Multi-instance Sections</strong> is on for this class, so the schema actually sent wraps the fields below in an{' '}
                <code>instances[]</code> array — one entry per document found in the section.
              </span>
            )}
            {schemaDivergence.probe && (
              <span>
                <strong>Multi-document Section Detection</strong> is on, so one extra property (<code>IDPDocumentInstanceCount</code>) is
                added to the schema sent for every class, asking the model how many documents the pages contain. It is stripped from the
                result and never appears in your extracted data.
              </span>
            )}
            <span>The section&apos;s stored metadata is the authoritative record of what was sent.</span>
          </SpaceBetween>
        </Alert>
      ) : null}

      {/* Info about what's shown */}
      <Alert type="info" header="About Prompt Preview">
        <SpaceBetween size="xxs">
          <span>
            This preview shows the actual prompts sent to the LLM.{' '}
            <span style={{ backgroundColor: '#d4edda', border: '1px solid #28a745', padding: '1px 4px', borderRadius: '3px' }}>
              Green highlighted text
            </span>{' '}
            shows configuration-derived values (class names, attribute schemas) substituted from your config.{' '}
            <span style={{ backgroundColor: '#fff3cd', border: '1px solid #ffc107', padding: '1px 4px', borderRadius: '3px' }}>
              Yellow highlighted text
            </span>{' '}
            indicates runtime placeholders filled with actual document content during processing.
          </span>
        </SpaceBetween>
      </Alert>

      {/* Stats bar */}
      <PromptStats
        systemPrompt={renderedSystemPrompt}
        taskPrompt={renderedTaskPrompt}
        toolSpec={toolSpecWire}
        model={stepConfig.model || ''}
      />

      {/* Prompt display */}
      <Tabs
        tabs={[
          {
            id: 'task',
            label: `Task Prompt (~${estimateTokens(renderedTaskPrompt).toLocaleString()} tokens)`,
            content: (
              <SpaceBetween size="s">
                <Box float="right">
                  <CopyToClipboard
                    copyButtonAriaLabel="Copy task prompt"
                    copySuccessText="Task prompt copied"
                    copyErrorText="Failed to copy"
                    textToCopy={renderedTaskPrompt}
                    variant="icon"
                  />
                </Box>
                <HighlightedPrompt text={renderedTaskPrompt} />
              </SpaceBetween>
            ),
          },
          {
            id: 'system',
            label: `System Prompt (~${estimateTokens(renderedSystemPrompt).toLocaleString()} tokens)`,
            content: (
              <SpaceBetween size="s">
                <Box float="right">
                  <CopyToClipboard
                    copyButtonAriaLabel="Copy system prompt"
                    copySuccessText="System prompt copied"
                    copyErrorText="Failed to copy"
                    textToCopy={renderedSystemPrompt}
                    variant="icon"
                  />
                </Box>
                {schemaDivergence.restatesSchema && (
                  <Box variant="small" color="text-body-secondary">
                    Ends with the config-derived <code>{EXPECTED_SCHEMA_LABEL}</code> block that Advanced (agentic) extraction appends — not
                    part of your template, and shown here as an <strong>approximation</strong>: the real one is generated from a Pydantic
                    model built from this class and runs roughly 1.7–2.9x larger. See the warning above.
                  </Box>
                )}
                <HighlightedPrompt text={renderedSystemPrompt} />
              </SpaceBetween>
            ),
          },
          // Shown only when a forced toolSpec is actually sent — Simple mode with
          // Schema Enforcement on. Advanced deliberately has no tab here: it always
          // sends a tool, but Strands builds that one from a generated Pydantic
          // model, so a "tool schema" rendered from the class schema would be a
          // guess. What Advanced puts in the prompt is covered by the System tab.
          ...(toolSpecText
            ? [
                {
                  id: 'tool-schema',
                  label: `Tool Schema (~${estimateTokens(toolSpecWire).toLocaleString()} tokens)`,
                  content: (
                    <SpaceBetween size="s">
                      <Box float="right">
                        <CopyToClipboard
                          copyButtonAriaLabel="Copy tool schema"
                          copySuccessText="Tool schema copied"
                          copyErrorText="Failed to copy"
                          textToCopy={toolSpecText}
                          variant="icon"
                        />
                      </Box>
                      <Box variant="small" color="text-body-secondary">
                        Sent as a Converse <code>toolConfig</code> alongside the prompts above, with{' '}
                        <code>
                          toolChoice: {'{"tool": {"name": "'}
                          {EXTRACTION_TOOL_NAME}
                          {'"}}'}
                        </code>{' '}
                        so the model must call it. Its tokens are billed as input like any other part of the request, which is why they are
                        in the total above — counted from the compact form actually serialized onto the request, not from the indentation
                        added here for readability. Property names are shown <strong>as you authored them</strong>: names outside{' '}
                        <code>^[a-zA-Z0-9_.-]{'{1,64}'}$</code> are rewritten to a wire-safe spelling for the request and restored in the
                        stored result
                        {renamedToolProperties.length > 0
                          ? ` — ${renamedToolProperties.length} name${renamedToolProperties.length === 1 ? '' : 's'} on this class (${renamedToolProperties.slice(0, 5).join(', ')}${renamedToolProperties.length > 5 ? ', …' : ''})`
                          : ' — none on this class'}
                        . The <code>x-aws-idp-*</code> extensions and the schema-document keywords (<code>$id</code>, <code>$schema</code>)
                        are stripped, as they are on the wire.
                      </Box>
                      <div className="prompt-preview-content" style={{ fontSize: '12px' }}>
                        {toolSpecText}
                      </div>
                    </SpaceBetween>
                  ),
                },
              ]
            : []),
          {
            id: 'raw-task',
            label: 'Raw Task Template',
            content: (
              <SpaceBetween size="s">
                <Box float="right">
                  <CopyToClipboard
                    copyButtonAriaLabel="Copy raw task template"
                    copySuccessText="Raw template copied"
                    copyErrorText="Failed to copy"
                    textToCopy={rawTaskPrompt}
                    variant="icon"
                  />
                </Box>
                <div className="prompt-preview-content" style={{ color: '#555' }}>
                  {rawTaskPrompt || (
                    <Box color="text-body-secondary">
                      <span style={{ fontStyle: 'italic' }}>No task prompt template configured.</span>
                    </Box>
                  )}
                </div>
              </SpaceBetween>
            ),
          },
          {
            id: 'raw-system',
            label: 'Raw System Template',
            content: (
              <SpaceBetween size="s">
                <Box float="right">
                  <CopyToClipboard
                    copyButtonAriaLabel="Copy raw system template"
                    copySuccessText="Raw template copied"
                    copyErrorText="Failed to copy"
                    textToCopy={rawSystemPrompt}
                    variant="icon"
                  />
                </Box>
                <div className="prompt-preview-content" style={{ color: '#555' }}>
                  {rawSystemPrompt || (
                    <Box color="text-body-secondary">
                      <span style={{ fontStyle: 'italic' }}>No system prompt template configured.</span>
                    </Box>
                  )}
                </div>
              </SpaceBetween>
            ),
          },
        ]}
      />

      {/* Legend */}
      {needsClassSelection && selectedClass && (
        <Container header={<Header variant="h3">Substitution Details</Header>}>
          <SpaceBetween size="s">
            <Box variant="h4">
              {'{ATTRIBUTE_NAMES_AND_DESCRIPTIONS}'} → Cleaned JSON Schema for &quot;{getClassId(selectedClass)}&quot;
            </Box>
            <Box variant="small" color="text-body-secondary">
              This is the cleaned version of the class JSON Schema (with x-aws-idp-* custom fields removed) that gets inserted into the
              prompt. This is what the LLM sees when extracting/assessing attributes.
            </Box>
            <div className="prompt-preview-content" style={{ height: '300px', fontSize: '12px' }}>
              {formatSchemaForPrompt(selectedClass)}
            </div>
          </SpaceBetween>
        </Container>
      )}

      {selectedStep === 'classification' && classes.length > 0 && (
        <Container header={<Header variant="h3">Substitution Details</Header>}>
          <SpaceBetween size="s">
            <Box variant="h4">{'{CLASS_NAMES_AND_DESCRIPTIONS}'} → Formatted Class List</Box>
            <Box variant="small" color="text-body-secondary">
              This is the class list with descriptions that gets inserted into the classification prompt. The LLM uses this to classify
              document pages.
            </Box>
            <div className="prompt-preview-content" style={{ height: '200px', fontSize: '12px' }}>
              {formatClassNamesAndDescriptions(classes)}
            </div>

            <Box variant="h4">{'{CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}'} → Class List with Schema Attribute Names (opt-in)</Box>
            <Box variant="small" color="text-body-secondary">
              <strong>Optional placeholder.</strong> Expands to each class&apos;s name, description, <em>and</em> the names of its schema
              attributes (extraction fields). Useful for disambiguating classes with similar names but different extraction schemas. Only
              materialized into the prompt when the template references it — token cost stays unchanged for users who don&apos;t use it.
              Per-class attribute counts are soft-capped at 50 with a <code>...(+N more)</code> truncation marker. See{' '}
              <a
                href="https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/262"
                target="_blank"
                rel="noopener noreferrer"
              >
                issue #262
              </a>{' '}
              for context.
            </Box>
            <div className="prompt-preview-content" style={{ height: '200px', fontSize: '12px' }}>
              {formatClassAndAttributeNamesAndDescriptions(classes)}
            </div>
          </SpaceBetween>
        </Container>
      )}
    </SpaceBetween>
  );
};

export default PromptPreview;
