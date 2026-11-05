// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Moving the editor to one field: a review reason's chip, Next alert, or a `?field=`
// link. The request names the section and the canonical field path; the editor opens
// the section, expands the groups above the field, scrolls it to the centre and focuses
// its input.

/** A request to bring one field on screen and focus it; a new nonce repeats the same request. */
export interface FieldFocusRequest {
  sectionId: string;
  /** Canonical field path ("LineItems[2].Amount"); empty = the section only. */
  fieldPath: string;
  nonce: number;
}

/**
 * The collapse keys of the groups that contain a field, outermost first. The field
 * renderer keys a group by its display path joined with dots: the root "Document Data",
 * then each object key, and `[i]` for a list item.
 */
export const collapseKeysAbove = (fieldPath: string): string[] => {
  const segments = fieldPath.match(/\[\d+\]|[^.[\]]+/g) ?? [];
  const keys: string[] = [];
  let key = 'Document Data';
  keys.push(key);
  for (const segment of segments.slice(0, -1)) {
    key = `${key}.${segment}`;
    keys.push(key);
  }
  return keys;
};

/**
 * Scroll the field's node to the centre and focus its first input (or the node itself
 * when it has none, as a read-only field does). False when the field is not on screen.
 */
export const focusFieldNode = (fieldPath: string): boolean => {
  const node = document.querySelector<HTMLElement>(`[data-field-path="${CSS.escape(fieldPath)}"]`);
  if (!node) return false;
  node.scrollIntoView({ block: 'center', behavior: 'smooth' });
  const input = node.querySelector<HTMLElement>('input, textarea, select');
  const target = input ?? node;
  if (!input && !node.hasAttribute('tabindex')) node.setAttribute('tabindex', '-1');
  target.focus({ preventScroll: true });
  return true;
};
