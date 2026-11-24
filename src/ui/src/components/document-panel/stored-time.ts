// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import type { Formatters } from '../../i18n/format';

/**
 * A stored timestamp, a UTC ISO string, as a date and time in the UI locale and the browser's
 * time zone, as the document list shows it. No value, or one `Date` cannot parse, is shown as
 * stored, so an empty time stays empty. Display only. One place, because both document panels
 * show these times and a second copy is how the two would drift.
 */
export const formatStoredTime = (value: string | undefined, formatDateTime: Formatters['formatDateTime']): string | undefined => {
  const date = new Date(value ?? '');
  return Number.isNaN(date.getTime()) ? value : formatDateTime(date);
};
