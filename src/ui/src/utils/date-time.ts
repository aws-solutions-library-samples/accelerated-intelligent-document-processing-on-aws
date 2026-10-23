// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Shared date/time display helpers. Relative times use Intl.RelativeTimeFormat so
 * "yesterday" and pluralisation come from the platform rather than hand-built strings.
 */

const SECOND_MS = 1000;
const MINUTE_MS = 60 * SECOND_MS;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;
/** Anything younger than this (or in the future, e.g. clock skew) reads as "just now". */
const JUST_NOW_MS = 45 * SECOND_MS;
/** From this age on, show the absolute date/time instead of a relative one. */
const ABSOLUTE_AFTER_MS = 7 * DAY_MS;

const relativeTimeFormat = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });

const toTimestamp = (value: string | number | Date): number => {
  const time = value instanceof Date ? value.getTime() : new Date(value).getTime();
  return Number.isFinite(time) ? time : NaN;
};

/** Locale-formatted absolute date and time; '' for an invalid value. */
export const formatDateTime = (value: string | number | Date): string => {
  const time = toTimestamp(value);
  return Number.isNaN(time) ? '' : new Date(time).toLocaleString();
};

/**
 * Relative time such as "just now", "5 minutes ago", "yesterday"; absolute
 * (formatDateTime) at 7 days or older; '' for an invalid value.
 */
export const formatRelativeTime = (value: string | number | Date, now: number = Date.now()): string => {
  const time = toTimestamp(value);
  if (Number.isNaN(time)) return '';
  const age = now - time;
  if (age < JUST_NOW_MS) return 'just now';
  if (age >= ABSOLUTE_AFTER_MS) return formatDateTime(time);
  if (age < HOUR_MS) return relativeTimeFormat.format(-Math.max(1, Math.floor(age / MINUTE_MS)), 'minute');
  if (age < DAY_MS) return relativeTimeFormat.format(-Math.floor(age / HOUR_MS), 'hour');
  return relativeTimeFormat.format(-Math.floor(age / DAY_MS), 'day');
};
