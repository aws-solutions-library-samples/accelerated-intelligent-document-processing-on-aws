// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { useMemo } from 'react';
import type { AppLayoutProps, PaginationProps } from '@cloudscape-design/components';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';

/**
 * The AppLayout aria labels every page layout passes, declared here and not per layout so
 * the side navigation has one name across the app. Every one of them is set under every
 * locale: with no label of the app's own, the layout's landmarks would go unnamed in English.
 */
const appLayoutMessages = defineMessages<{
  navigation: NoMessageValues;
  navigationToggle: NoMessageValues;
  navigationClose: NoMessageValues;
  notifications: NoMessageValues;
  tools: NoMessageValues;
  toolsToggle: NoMessageValues;
  toolsClose: NoMessageValues;
}>({
  navigation: {
    id: 'appLayout.ariaLabels.navigation',
    defaultMessage: 'Side navigation',
    description: 'Screen-reader name of the side navigation landmark of every page.',
  },
  navigationToggle: {
    id: 'appLayout.ariaLabels.navigationToggle',
    defaultMessage: 'Open side navigation',
    description: 'Screen-reader label of the button that opens the side navigation.',
  },
  navigationClose: {
    id: 'appLayout.ariaLabels.navigationClose',
    defaultMessage: 'Close side navigation',
    description: 'Screen-reader label of the button that closes the side navigation.',
  },
  notifications: {
    id: 'appLayout.ariaLabels.notifications',
    defaultMessage: 'Notifications',
    description: 'Screen-reader name of the region at the top of every page that holds the flash messages.',
  },
  tools: {
    id: 'appLayout.ariaLabels.tools',
    defaultMessage: 'Help panel',
    description: 'Screen-reader name of the info panel on the right of a page.',
  },
  toolsToggle: {
    id: 'appLayout.ariaLabels.toolsToggle',
    defaultMessage: 'Open help panel',
    description: 'Screen-reader label of the button that opens the info panel.',
  },
  toolsClose: {
    id: 'appLayout.ariaLabels.toolsClose',
    defaultMessage: 'Close help panel',
    description: 'Screen-reader label of the button that closes the info panel.',
  },
});

/** The AppLayout aria labels in the UI's language: one object per locale, safe in a dependency list. */
export const useAppLayoutLabels = (): AppLayoutProps.Labels => {
  const intl = useIntl();
  return useMemo(
    () => ({
      navigation: intl.formatMessage(appLayoutMessages.navigation),
      navigationToggle: intl.formatMessage(appLayoutMessages.navigationToggle),
      navigationClose: intl.formatMessage(appLayoutMessages.navigationClose),
      notifications: intl.formatMessage(appLayoutMessages.notifications),
      tools: intl.formatMessage(appLayoutMessages.tools),
      toolsToggle: intl.formatMessage(appLayoutMessages.toolsToggle),
      toolsClose: intl.formatMessage(appLayoutMessages.toolsClose),
    }),
    [intl],
  );
};

/**
 * The Pagination aria labels every table passes. Set under every locale: Cloudscape's own
 * bundle has them in French but not under `en`, where the provider carries no messages.
 */
const paginationMessages = defineMessages<{
  next: NoMessageValues;
  previous: NoMessageValues;
  page: { readonly pageNumber: number };
}>({
  next: {
    id: 'common.pagination.next',
    defaultMessage: 'Next page',
    description: "Screen-reader label of the next-page button of a table's pagination.",
  },
  previous: {
    id: 'common.pagination.previous',
    defaultMessage: 'Previous page',
    description: "Screen-reader label of the previous-page button of a table's pagination.",
  },
  page: {
    id: 'common.pagination.page',
    defaultMessage: 'Page {pageNumber, number} of all pages',
    description: "Screen-reader label of a page button of a table's pagination. {pageNumber} is the page number.",
  },
});

/** The Pagination aria labels in the UI's language: one object per locale, safe in a dependency list. */
export const usePaginationLabels = (): PaginationProps.Labels => {
  const intl = useIntl();
  return useMemo(
    () => ({
      nextPageLabel: intl.formatMessage(paginationMessages.next),
      previousPageLabel: intl.formatMessage(paginationMessages.previous),
      pageLabel: (pageNumber: number): string => intl.formatMessage(paginationMessages.page, { pageNumber }),
    }),
    [intl],
  );
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
