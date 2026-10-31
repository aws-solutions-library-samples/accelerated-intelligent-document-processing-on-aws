// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Classification config (format 0.9) keeps every model setting inside the call
 * that uses it. These helpers answer "which call assigns the class?" in one
 * place, mirroring `class_assigning_llm_path` in
 * `lib/idp_common_pkg/idp_common/config/models.py`.
 */

/** Path under `classification` of the call that assigns the class, per strategy. */
export const CLASS_ASSIGNING_LLM_PATHS: Record<string, readonly string[]> = {
  combined: ['combined', 'llm'],
  split_then_classify: ['split_then_classify', 'section_classification', 'llm'],
};

/** The class-assigning call's path for a classification block; `combined` by default. */
export const classAssigningLlmPath = (classification: unknown): readonly string[] => {
  const strategy =
    classification && typeof classification === 'object'
      ? String((classification as Record<string, unknown>).strategy ?? '')
          .trim()
          .toLowerCase()
      : '';
  return CLASS_ASSIGNING_LLM_PATHS[strategy] ?? CLASS_ASSIGNING_LLM_PATHS.combined;
};

/** The model that assigns the class under the active strategy, or undefined. */
export const classificationModelOf = (classification: unknown): string | undefined => {
  let node: unknown = classification;
  for (const key of classAssigningLlmPath(classification)) {
    node = node && typeof node === 'object' ? (node as Record<string, unknown>)[key] : undefined;
  }
  const model = node && typeof node === 'object' ? (node as Record<string, unknown>).model : undefined;
  return typeof model === 'string' && model ? model : undefined;
};

/**
 * Write `model` into the class-assigning call of `classification`, creating the
 * path as needed. The splitting call keeps its own model: it only finds boundaries.
 */
export const setClassificationModel = (classification: Record<string, unknown>, model: string): void => {
  let node = classification;
  for (const key of classAssigningLlmPath(classification)) {
    const child = node[key];
    if (!child || typeof child !== 'object' || Array.isArray(child)) {
      node[key] = {};
    }
    node = node[key] as Record<string, unknown>;
  }
  node.model = model;
};

/**
 * Keys config format 0.9 moved out of `classification` (mirrors `FLAT_KEYS` in
 * `lib/idp_common_pkg/idp_common/config/migrations/v08_to_v09.py`). A file
 * holding any of them was written for v0.6.12 or earlier; the server converts it
 * when it is saved.
 */
export const FLAT_CLASSIFICATION_KEYS = [
  'classificationMethod',
  'model',
  'model_lambda_hook_arn',
  'system_prompt',
  'task_prompt',
  'temperature',
  'top_p',
  'top_k',
  'reasoning_effort',
  'max_tokens',
  'maxPagesForClassification',
  'granularity',
  'packet',
  'segmentation',
  'labeling',
  'sectionSplitting',
  'contextPagesCount',
  'enforceValidClasses',
  'maxValidationRetries',
  'invalidClassFallback',
  'confidence',
  'image',
  'modality',
] as const;

/** The pre-0.9 classification keys a configuration still carries. */
export const flatClassificationKeysIn = (config: unknown): string[] => {
  const classification = config && typeof config === 'object' ? (config as Record<string, unknown>).classification : undefined;
  if (!classification || typeof classification !== 'object') return [];
  return FLAT_CLASSIFICATION_KEYS.filter((key) => Object.hasOwn(classification as object, key));
};

/**
 * The three shapes a file can be declared to have. Mirrors
 * `SECTION_SPLITTING_VALUES` in `lib/idp_common_pkg/idp_common/config/models.py`;
 * `scripts/tests/test_classification_flat_keys_ui_parity.py` keeps the two in step.
 */
export const SECTION_SPLITTING_VALUES = ['llm_determined', 'disabled', 'page'] as const;

/** What an unset `sectionSplitting` means: detect the boundaries. */
export const DEFAULT_SECTION_SPLITTING = 'llm_determined';

/**
 * The recognised spelling of a raw stored `sectionSplitting`, or `null` when there
 * is not one. Mirrors `_known_section_splitting` in `v08_to_v09.py`, including the
 * distinction that matters there: an absent, null or blank value means "take the
 * default" and is NOT the same as an unrecognised one.
 */
/**
 * The `sectionSplitting` a configuration will end up with, read the way the hop
 * reads it: ⚠️ **the FLAT key wins over the nested one.** That is the hop's own
 * hybrid rule (`v08_to_v09.py`: "in a hybrid — flat and nested keys in one dict —
 * the flat key wins"), where `_put` overwrites `document_shape.sectionSplitting` with
 * the flat value. Both helpers below go through this, so they cannot disagree
 * about precedence — two readers that did is the part most likely to be re-broken.
 */
const sectionSplittingOf = (cls: Record<string, unknown>): unknown => {
  if ('sectionSplitting' in cls) return cls.sectionSplitting;
  const nested = cls.document_shape && typeof cls.document_shape === 'object' ? (cls.document_shape as Record<string, unknown>) : undefined;
  return nested && 'sectionSplitting' in nested ? nested.sectionSplitting : undefined;
};

const knownSectionSplitting = (raw: unknown): string | null => {
  if (raw === undefined || raw === null || String(raw).trim() === '') return DEFAULT_SECTION_SPLITTING;
  const text = String(raw).trim().toLowerCase();
  return (SECTION_SPLITTING_VALUES as readonly string[]).includes(text) ? text : null;
};

/**
 * Whether a raw `maxPagesForClassification` really asks for the first N pages.
 * Mirrors `_is_page_limit` in `v08_to_v09.py`, **including its number/string
 * asymmetry**: a real number is truncated, so `2.5` is a two-page limit, while a
 * string must already spell a whole number, because `int('2.5')` raises in Python.
 * Stringifying first collapsed the two and left the dialog silent about an
 * unquoted `2.5` that the migration does convert.
 *
 * The cases both sides must agree on are the shared table in
 * `__tests__/page-limit.fixtures.json`.
 */
const isPageLimit = (raw: unknown): boolean => {
  // A boolean is not a page count, although `Number(true)` is 1 — the same trap
  // `_is_page_limit` guards in Python, where `bool` is a subclass of `int`.
  if (raw === undefined || raw === null || typeof raw === 'boolean') return false;
  if (typeof raw === 'number') return Number.isFinite(raw) && Math.trunc(raw) > 0;
  const text = String(raw).trim().toUpperCase();
  if (text === '' || text === 'ALL') return false;
  const limit = Number(text);
  return Number.isInteger(limit) && limit > 0;
};

/**
 * Whether converting this file turns boundary detection into "one document per
 * file": a page limit combined with a *recognised* section splitting other than
 * `disabled` (mirrors `conversion_notices` in `v08_to_v09.py`).
 *
 * An unrecognised value is deliberately not reported here. The conversion does not
 * happen for one — the value is left for save-time validation, which refuses it —
 * so promising a conversion would describe something the backend will not do. Use
 * `unknownSectionSplittingIn` for that case.
 */
export const pageLimitBecomesOneDocument = (config: unknown): boolean => {
  const classification = config && typeof config === 'object' ? (config as Record<string, unknown>).classification : undefined;
  if (!classification || typeof classification !== 'object') return false;
  const cls = classification as Record<string, unknown>;
  if (!isPageLimit(cls.maxPagesForClassification)) return false;
  const splitting = knownSectionSplitting(sectionSplittingOf(cls));
  return splitting !== null && splitting !== 'disabled';
};

/**
 * The file's `sectionSplitting` value when it is not one the backend accepts, else
 * `null`. Saving such a file is refused, so the import dialog says so rather than
 * describing a conversion that will not run.
 */
export const unknownSectionSplittingIn = (config: unknown): string | null => {
  const classification = config && typeof config === 'object' ? (config as Record<string, unknown>).classification : undefined;
  if (!classification || typeof classification !== 'object') return null;
  const cls = classification as Record<string, unknown>;
  const raw = sectionSplittingOf(cls);
  if (raw === undefined) return null;
  if (knownSectionSplitting(raw) !== null) return null;
  // A malformed nesting in an imported file renders as `[object Object]` through
  // `String`, which tells the operator nothing about the file they are holding.
  return typeof raw === 'object' ? JSON.stringify(raw) : String(raw);
};
