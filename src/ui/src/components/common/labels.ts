// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
export const appLayoutLabels = {
  navigation: 'Side navigation',
  navigationToggle: 'Open side navigation',
  navigationClose: 'Close side navigation',
  notifications: 'Notifications',
  tools: 'Help panel',
  toolsToggle: 'Open help panel',
  toolsClose: 'Close help panel',
} as const;

export const paginationLabels = {
  nextPageLabel: 'Next page',
  previousPageLabel: 'Previous page',
  pageLabel: (pageNumber: number): string => `Page ${pageNumber} of all pages`,
};

/**
 * Shared label-provenance vocabulary. Machine-drafted labels must never render
 * like human-verified ones, so every surface that shows labels uses this map.
 */
export const LABEL_SOURCE_BADGES: Record<string, { color: 'blue' | 'green' | 'grey' | 'severity-neutral'; text: string }> = {
  'draft-machine': { color: 'blue', text: 'Draft (machine)' },
  'reviewed-human': { color: 'green', text: 'Reviewed' },
  synthetic: { color: 'grey', text: 'Synthetic' },
  uploaded: { color: 'grey', text: 'Uploaded' },
};

/** How a field value and the JSON panel are labelled, by where the values come from. */
export interface ValueSourceLabel {
  /** Before an unchanged value. */
  value: string;
  /** Before a value the user changed. */
  changed: string;
  /** The JSON tab's panel title. */
  panelTitle: string;
}

/**
 * The value labels for a document's values: model predictions (no label source), or
 * test-set ground truth (any label source: draft, reviewed, synthetic or uploaded).
 */
export const valueSourceLabel = (labelSource?: string | null): ValueSourceLabel =>
  labelSource
    ? { value: 'Ground truth:', changed: 'Your value:', panelTitle: 'Ground truth' }
    : { value: 'Predicted:', changed: 'Your value:', panelTitle: 'Predicted Results' };

/**
 * The edit screen's action that applies the changes: the button, its confirmation, the
 * banner and alert copy, and the help panel all name it with this one label.
 */
export const PROCESS_CHANGES_LABEL = 'Process Changes';

/**
 * Opening a test set's ground truth for editing, the same on the Test Sets list and the
 * test set's page: both size the review first (ReviewEffortModal).
 */
export const EDIT_GROUND_TRUTH_LABEL = 'Edit ground truth';

/** The control that moves the edit screen to the next low-confidence field. */
export const NEXT_ALERT_LABEL = 'Next alert';

/** The marker of an unread Inbox message (the table badge and the bell's recent list). */
export const UNREAD_LABEL = 'Unread';
