// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Shared date/time display helpers; both take the UI locale. Relative times use
 * Intl.RelativeTimeFormat so "yesterday" and pluralisation come from the platform rather
 * than hand-built strings.
 */
import { defineMessages, type NoMessageValues } from 'react-intl';

import { formatDateTime as formatAbsoluteDateTime } from '../i18n/format';
import type { Locale } from '../i18n/locales';

/** The one relative time the platform does not word: each caller formats it and passes it in. */
export const relativeTimeMessages = defineMessages<{ justNow: NoMessageValues }>({
  justNow: {
    id: 'common.relativeTime.justNow',
    defaultMessage: 'just now',
    description: 'Relative time of something less than 45 seconds old, where older times read "5 minutes ago" or "yesterday".',
  },
});

const SECOND_MS = 1000;
const MINUTE_MS = 60 * SECOND_MS;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;
/** Anything younger than this (or in the future, e.g. clock skew) reads as "just now". */
const JUST_NOW_MS = 45 * SECOND_MS;
/** From this age on, show the absolute date/time instead of a relative one. */
const ABSOLUTE_AFTER_MS = 7 * DAY_MS;

/**
 * One formatter per locale. The locale TAG is passed, not the runtime default: under `en` the
 * words must stay English whatever the browser's own language, as the hard-coded `'en'` did.
 * This is the one place in the UI where the `en` path hands `Intl` an explicit locale.
 */
const relativeTimeFormats = new Map<Locale, Intl.RelativeTimeFormat>();
const relativeTimeFormat = (locale: Locale): Intl.RelativeTimeFormat => {
  let format = relativeTimeFormats.get(locale);
  if (!format) {
    format = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' });
    relativeTimeFormats.set(locale, format);
  }
  return format;
};

const toTimestamp = (value: string | number | Date): number => {
  const time = value instanceof Date ? value.getTime() : new Date(value).getTime();
  return Number.isFinite(time) ? time : NaN;
};

/** Locale-formatted absolute date and time; '' for an invalid value. */
export const formatDateTime = (value: string | number | Date, locale: Locale): string => {
  const time = toTimestamp(value);
  return Number.isNaN(time) ? '' : formatAbsoluteDateTime(new Date(time), locale);
};

/**
 * Relative time such as "just now", "5 minutes ago", "yesterday"; absolute
 * (formatDateTime) at 7 days or older; '' for an invalid value. `justNow` is the text for an
 * age under 45 seconds, `relativeTimeMessages.justNow` formatted by the caller; without it,
 * the English source text.
 */
export const formatRelativeTime = (
  value: string | number | Date,
  locale: Locale,
  now: number = Date.now(),
  justNow: string = String(relativeTimeMessages.justNow.defaultMessage),
): string => {
  const time = toTimestamp(value);
  if (Number.isNaN(time)) return '';
  const age = now - time;
  if (age < JUST_NOW_MS) return justNow;
  if (age >= ABSOLUTE_AFTER_MS) return formatDateTime(time, locale);
  if (age < HOUR_MS) return relativeTimeFormat(locale).format(-Math.max(1, Math.floor(age / MINUTE_MS)), 'minute');
  if (age < DAY_MS) return relativeTimeFormat(locale).format(-Math.floor(age / HOUR_MS), 'hour');
  return relativeTimeFormat(locale).format(-Math.floor(age / DAY_MS), 'day');
};
