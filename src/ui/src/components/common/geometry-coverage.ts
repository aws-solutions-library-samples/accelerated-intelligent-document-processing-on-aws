// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Whether a field has a bounding box, why not when it has none, and how many of
 * a section do.
 *
 * A field is located by matching its extracted value to the document — under the
 * default `extraction.geometry.mode: ocr_only` by matching it to a line of OCR
 * text — so a field the model returned nothing for cannot be located at all.
 * On a real run that is the whole of the difference: of ten fields in one form
 * section, the six carrying a value each had a box and the four carrying none had
 * no box ([#1327]).
 *
 * Every row therefore carries a control: the locate action where there is a box,
 * and otherwise the reason there is none. An empty slot would leave the reader to
 * infer the rule, or to conclude the boxes are unreliable.
 */

/** A field's relationship to the page, as the viewer can determine it. */
export type FieldGeometryState =
  /** Has a box, so it can be highlighted on the page image. */
  | 'located'
  /** Nothing was extracted, so there is nothing to point at. */
  | 'no-value'
  /** A value, but it could not be tied to anything on the page. */
  | 'unmatched'
  /**
   * This field has a value, and nothing in the section has a box — so its own
   * value explains nothing and the cause is shared by every row.
   *
   * ⚠️ Scoped to the **section**, and asserts no cause. A packet section can
   * come back with no boxes while its siblings have them under the same
   * configuration, so neither "this document" nor `geometry.mode` can be
   * claimed from here; saying either sent a reader to the configuration for a
   * section-level outcome.
   */
  | 'none-produced'
  /**
   * The field is displayed but `explainability_info` holds no entry for it, so
   * there is neither a confidence nor a box — nothing was recorded about it
   * rather than a match having been attempted and missed.
   *
   * Real and common: one form section of a packet showed ten fields with
   * entries for three. It must not be reported as `unmatched`, which tells the
   * reader to check the value against the page — advice that cannot reveal a
   * cause lying in whether the field was assessed at all.
   */
  | 'no-data';

/**
 * Values that mean "the model returned nothing", beyond the empty ones.
 *
 * ⚠️ `'None'` is here because the pipeline emits Python's `str(None)` for an
 * absent value rather than JSON `null`, so the field arrives as the four-character
 * string and renders as text. Recognising it is a display decision only — it
 * changes which sentence this file offers and nothing about the data. The
 * underlying artifact is worth fixing at its source, since such a value copied
 * into a test-set baseline becomes literal ground truth and would be scored as the
 * string `"None"` against a correctly-empty field.
 *
 * Deliberately a short, explicit list rather than a pattern: a document genuinely
 * containing the word "None" in a field is possible, and the cost of being wrong
 * is one sentence reading "no value was extracted" where "not found on the page"
 * was meant. A regex over anything null-ish would make that misread common.
 *
 * Only these two, because only these two have a producer. `-`, `--` and `NA` were
 * considered and rejected: a single hyphen is an ordinary extracted cell value and
 * "NA" is a real value in several document vocabularies, so for those the row
 * would claim nothing was extracted while displaying the value that was.
 */
const ABSENT_VALUE_SENTINELS = new Set(['none', 'null']);

/** True when the model returned nothing for this field. */
export const isAbsentValue = (value: unknown): boolean => {
  if (value === null || value === undefined) return true;
  if (typeof value === 'string') {
    const trimmed = value.trim();
    return trimmed === '' || ABSENT_VALUE_SENTINELS.has(trimmed.toLowerCase());
  }
  // An empty array or object is the structural equivalent: nothing was extracted.
  if (Array.isArray(value)) return value.length === 0;
  if (typeof value === 'object') return Object.keys(value as object).length === 0;
  return false;
};

/**
 * Whether one `geometry` entry is a box this viewer can draw.
 *
 * The single definition of "has a box", exported so the count and the per-field
 * control cannot each have their own: the renderer builds a highlight only from an
 * entry carrying both `boundingBox` and `page`, so an entry missing either is not
 * a box — and a count that accepted it would report a field as locatable whose row
 * offers no way to locate it.
 */
export const isDrawableGeometry = (entry: unknown): boolean => {
  if (!entry || typeof entry !== 'object') return false;
  const record = entry as Record<string, unknown>;
  return Boolean(record.boundingBox) && record.page !== undefined;
};

/**
 * Whether a field's `geometry` value yields a box this viewer will draw.
 *
 * ⚠️ The **first** entry only, because that is the one the renderer takes
 * (`fieldInfo.geometry[0]`) and the overlay draws. `some()` reads as the more
 * generous and therefore safer choice and is the wrong one here: a field whose
 * first entry lacks a `boundingBox` while a later entry has one would be counted
 * in the caption and offered no control on its row — the caption and the rows
 * disagreeing, which is the one thing this count exists to avoid.
 */
export const hasDrawableGeometry = (geometry: unknown): boolean => isDrawableGeometry(Array.isArray(geometry) ? geometry[0] : geometry);

/**
 * What the viewer can say about one field.
 *
 * `hasGeometry` is passed in rather than derived, so this answers for exactly the
 * geometry the row was given — the alternative is a second traversal of
 * `explainability_info` that can disagree with the one driving the control.
 *
 * `geometryAvailable` is about the document, not the field: with nothing located
 * anywhere there is no per-field conclusion to draw, and offering one would be
 * wrong on every row at once.
 */
export const classifyFieldGeometry = (
  value: unknown,
  hasGeometry: boolean,
  geometryAvailable = true,
  hasFieldData = true,
): FieldGeometryState => {
  if (hasGeometry) return 'located';
  // A field's own absent value is consulted before any section-wide fact, so
  // the reason names the field rather than the section. A section that
  // extracted nothing at all is the case that makes the order matter: every
  // field is then absent, and "no value extracted" is both true and actionable
  // where a section-wide statement is neither.
  if (isAbsentValue(value)) return 'no-value';
  if (!geometryAvailable) return 'none-produced';
  // `unmatched` is a claim that matching ran and found nothing, so it requires
  // an entry to have existed. Without one, nothing ran.
  return hasFieldData ? 'unmatched' : 'no-data';
};

/**
 * One short line per state, for the row that carries no locate control.
 *
 * Kept to a phrase because it renders in a popover beside a field: the reader
 * needs which of the four causes applies, not an explanation of geometry. None
 * of them says anything about the value, which is displayed either way — what is
 * missing in every case is its *position*.
 */
export const describeFieldGeometry = (state: FieldGeometryState): string => {
  switch (state) {
    case 'no-value':
      return 'No value extracted, so no bounding box.';
    case 'unmatched':
      return 'Value not found in the page text, so no bounding box.';
    case 'none-produced':
      return 'No field in this section has a bounding box.';
    case 'no-data':
      return 'No bounding box recorded — the assessment step skipped this field.';
    default:
      return 'Highlight this field on the page image.';
  }
};

/**
 * Whether anything in the section carries a drawable box.
 *
 * Scoped to the section, because `explainability_info` is the section's: callers
 * pass the one belonging to the section being viewed, and a packet's sections
 * differ from each other.
 *
 * Read from `explainability_info` alone, and that is the point: the renderer
 * recurses, and every level recomputes what it needs from the props it holds.
 * `explainability_info` is handed down whole, so this answers the same at every
 * level — while anything derived from the local `value` does not, because at a
 * leaf the value is a string and a count over it is zero.
 */
export const hasAnyDrawableGeometry = (explainabilityInfo: unknown): boolean => {
  let found = false;
  const walk = (node: unknown): void => {
    if (found || !node || typeof node !== 'object') return;
    if (Array.isArray(node)) {
      node.forEach(walk);
      return;
    }
    const record = node as Record<string, unknown>;
    if (hasDrawableGeometry(record.geometry)) {
      found = true;
      return;
    }
    Object.values(record).forEach(walk);
  };
  walk(explainabilityInfo);
  return found;
};

export interface GeometryCoverage {
  /** Fields carrying a bounding box. */
  located: number;
  /** Fields the extraction reported at all, box or no box. */
  total: number;
}

/**
 * How many of a section’s fields have a bounding box.
 *
 * ⚠️ **The denominator is the fields displayed, which means the value tree.**
 * Counting `explainability_info` leaves instead looks right — it is where the
 * boxes live — and is wrong whenever the two differ, which is common: one form
 * section of a packet displayed ten fields and carried entries for three, so the
 * caption read "3 of 3" above ten rows. Every displayed field gets a row and a
 * control whether or not anything was recorded about it, so the count has to come
 * from the same place the rows do.
 *
 * `explainability_info` is still the source for the numerator, looked up per field
 * along the path the row itself uses, so "located" means the same thing in the
 * caption and on the row.
 *
 * A displayed field is a leaf of the value tree: objects and arrays are containers
 * and are walked through, so a packet's sub-objects contribute their own leaves
 * rather than counting as one field each. An empty object or array has no leaf and
 * so contributes nothing, matching a renderer that draws no row for it.
 */
export const summariseGeometryCoverage = (value: unknown, explainabilityInfo: unknown): GeometryCoverage => {
  let located = 0;
  let total = 0;

  const root = Array.isArray(explainabilityInfo) ? explainabilityInfo[0] : explainabilityInfo;

  /** The explainability entry for a path, by the navigation the rows use. */
  const entryAt = (path: (string | number)[]): Record<string, unknown> | null => {
    let node: unknown = root;
    for (const part of path) {
      if (!node || typeof node !== 'object') return null;
      node = (node as Record<string | number, unknown>)[part];
    }
    return node && typeof node === 'object' ? (node as Record<string, unknown>) : null;
  };

  const walk = (node: unknown, path: (string | number)[]): void => {
    if (Array.isArray(node)) {
      node.forEach((item, index) => walk(item, [...path, index]));
      return;
    }
    if (node !== null && typeof node === 'object') {
      Object.entries(node as Record<string, unknown>).forEach(([key, child]) => walk(child, [...path, key]));
      return;
    }
    // A leaf: one displayed field.
    total += 1;
    if (hasDrawableGeometry(entryAt(path)?.geometry)) located += 1;
  };

  // The root itself must be a container; a bare primitive is not a section.
  if (value !== null && typeof value === 'object') walk(value, []);
  return { located, total };
};
