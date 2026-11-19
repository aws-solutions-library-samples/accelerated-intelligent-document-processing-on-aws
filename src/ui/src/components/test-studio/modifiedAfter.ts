// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The "Modified after" filter of the file-pattern flows (the create-test-set
 * wizard and the add-documents dialog), resolved to the ISO 8601 cutoff the
 * listBucketFiles, addTestSet and addDocumentsToTestSet operations take. Both
 * flows go through this one helper so the same input gives the same cutoff.
 */

import { defineMessages } from 'react-intl';

export const modifiedAfterMessages = defineMessages({
  customTimeIsUtc: {
    id: 'testStudio.modifiedAfter.customTimeIsUtc',
    defaultMessage: 'Date and time are in UTC.',
    description:
      'Help text beside the custom date and time of the "Modified after" filter when picking files for a test set: the entered date and time are read as UTC, not as the browser\'s local time.',
  },
});

/**
 * The cutoff for a time-filter option value: '' is no filter, 'custom' is the
 * entered date and time read as UTC, and any other value is a number of hours
 * before `now`.
 *
 * A custom date that does not parse is passed through unchanged rather than
 * dropped, so the server refuses it instead of the filter silently vanishing.
 */
export const resolveModifiedAfter = (
  filterValue: string | undefined,
  customDate: string,
  customTime: string,
  now: number = Date.now(),
): string | undefined => {
  if (!filterValue) return undefined;
  if (filterValue === 'custom') {
    if (!customDate) return undefined;
    const entered = `${customDate}T${customTime || '00:00:00'}`;
    const parsed = new Date(`${entered}Z`);
    return Number.isNaN(parsed.getTime()) ? `${entered}Z` : parsed.toISOString();
  }
  return new Date(now - parseInt(filterValue, 10) * 3600 * 1000).toISOString();
};
