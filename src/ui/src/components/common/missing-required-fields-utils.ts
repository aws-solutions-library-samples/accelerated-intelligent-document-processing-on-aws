// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Helpers for the per-section "missing required fields" list (#1527).
 *
 * The backend (`idp_common.assessment.presence.collect_missing_required_fields`)
 * lists, per section, the fields its class schema marks `required` that
 * extraction left absent, in the same shape as a confidence alert. The document
 * carries the sum as `MissingRequiredFieldCount`. An absent field raises no
 * confidence alert, so these are counted here instead — separately, and
 * informationally: nothing routes on them yet.
 */

export interface MissingRequiredField {
  attributeName?: string | null;
  confidence?: number | null;
  confidenceThreshold?: number | null;
}

export interface SectionWithMissingRequiredFields {
  MissingRequiredFields?: (MissingRequiredField | null)[] | null;
}

/** The section's missing required fields, with null entries dropped. */
export const getSectionMissingRequiredFields = (section: SectionWithMissingRequiredFields | null | undefined): MissingRequiredField[] => {
  const entries = section?.MissingRequiredFields;
  if (!Array.isArray(entries)) {
    return [];
  }
  return entries.filter((entry): entry is MissingRequiredField => !!entry && typeof entry === 'object');
};

/** Sum of the sections' missing-required-field lists. */
export const getDocumentMissingRequiredFieldCount = (sections: SectionWithMissingRequiredFields[] | null | undefined): number =>
  Array.isArray(sections) ? sections.reduce((total, s) => total + getSectionMissingRequiredFields(s).length, 0) : 0;

/**
 * The document's count, preferring the stored figure.
 *
 * The stored `MissingRequiredFieldCount` is the true total; the per-section
 * lists in the tracking item are capped, so a sum over them can undercount a
 * long table. Returns `null` when neither is available — the document list's
 * fast path does not carry the attribute, and a zero there would be a claim the
 * row cannot support.
 */
export const resolveMissingRequiredFieldCount = (
  storedCount: number | null | undefined,
  sections: SectionWithMissingRequiredFields[] | null | undefined,
): number | null => {
  if (typeof storedCount === 'number' && Number.isFinite(storedCount)) {
    return storedCount;
  }
  if (Array.isArray(sections)) {
    return getDocumentMissingRequiredFieldCount(sections);
  }
  return null;
};
