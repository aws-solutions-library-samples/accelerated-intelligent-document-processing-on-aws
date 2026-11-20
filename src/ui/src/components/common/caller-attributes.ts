// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Caller-supplied document attributes (an account id, a claim number) as the
 * tracking item stores them in `DocumentAttributes` (#1851).
 *
 * They are untrusted caller input: validated at intake, and rendered here only
 * as text. Nothing returned from this module is ever HTML; React escapes it.
 */

export type CallerAttributeEntry = { key: string; value: string };

const formatScalar = (value: unknown): string => {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  // Intake accepts nothing else; anything that arrives anyway is shown as JSON
  // text rather than dropped, so a stored value is never silently hidden.
  return JSON.stringify(value) ?? '';
};

/**
 * The attributes as display rows, sorted by key. Accepts the stored map, or the
 * same map as a JSON string; anything else (absent, malformed) yields no rows.
 */
export const parseCallerAttributes = (raw: unknown): CallerAttributeEntry[] => {
  let value: unknown = raw;
  if (typeof value === 'string') {
    try {
      value = JSON.parse(value);
    } catch {
      return [];
    }
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return [];
  return Object.entries(value as Record<string, unknown>)
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([key, v]) => ({
      key,
      value: Array.isArray(v) ? v.map(formatScalar).join(', ') : formatScalar(v),
    }));
};
