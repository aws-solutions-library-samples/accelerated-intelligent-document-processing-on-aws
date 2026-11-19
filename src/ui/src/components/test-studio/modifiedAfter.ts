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
  invalidCustomDateTime: {
    id: 'testStudio.modifiedAfter.invalidCustomDateTime',
    defaultMessage: 'Enter a real date and a time as HH:mm:ss.',
    description:
      'Error beside the custom date and time of the "Modified after" filter when they are not a real date and time, for example 30 February or 24:00:00. Keep HH:mm:ss as it is.',
  },
});

const DATE_SHAPE = /^(\d{4})-(\d{2})-(\d{2})$/;
const TIME_SHAPE = /^(\d{2}):(\d{2})(?::(\d{2}))?$/;

/**
 * A custom date (YYYY-MM-DD) and time (HH:mm or HH:mm:ss; empty is midnight)
 * read as UTC, as an ISO 8601 string, or null when they are not a real date and
 * time. A date JavaScript would roll over (30 February, 24:00) is null too: the
 * server's parser refuses those, and a silently moved cutoff is worse than an
 * error beside the field.
 */
export const customCutoff = (customDate: string, customTime: string): string | null => {
  const date = DATE_SHAPE.exec(customDate);
  const time = TIME_SHAPE.exec(customTime || '00:00:00');
  if (!date || !time) return null;
  const [year, month, day] = [Number(date[1]), Number(date[2]), Number(date[3])];
  const [hours, minutes, seconds] = [Number(time[1]), Number(time[2]), Number(time[3] ?? '0')];
  if (hours > 23 || minutes > 59 || seconds > 59) return null;
  const parsed = new Date(Date.UTC(year, month - 1, day, hours, minutes, seconds));
  if (parsed.getUTCFullYear() !== year || parsed.getUTCMonth() !== month - 1 || parsed.getUTCDate() !== day) return null;
  return parsed.toISOString();
};

/** Which custom field is at fault: the date when it is not a real date on its own, otherwise the time. */
export const invalidCustomField = (customDate: string): 'date' | 'time' => (customCutoff(customDate, '') === null ? 'date' : 'time');

/** True when the custom filter is chosen with a date entered that is not a real date and time. */
export const customCutoffIsInvalid = (filterValue: string | undefined, customDate: string, customTime: string): boolean =>
  filterValue === 'custom' && customDate !== '' && customCutoff(customDate, customTime) === null;

/**
 * The cutoff for a time-filter option value: '' is no filter, 'custom' is the
 * entered date and time read as UTC (no date entered is no filter), and any
 * other value is a number of hours before `now`.
 *
 * Throws for a custom date and time that are not valid, so an unchecked caller
 * can never send one; check {@link customCutoffIsInvalid} first and say so
 * beside the field.
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
    const cutoff = customCutoff(customDate, customTime);
    if (cutoff === null) throw new Error(`Not a valid date and time: ${customDate} ${customTime}`);
    return cutoff;
  }
  return new Date(now - parseInt(filterValue, 10) * 3600 * 1000).toISOString();
};
