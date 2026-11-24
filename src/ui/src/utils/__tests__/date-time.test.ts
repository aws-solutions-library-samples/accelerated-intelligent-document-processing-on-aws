// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { createIntl } from 'react-intl';
import { describe, expect, it } from 'vitest';

import { formatDateTime as formatAbsoluteDateTime } from '../../i18n/format';
import { onIntlError } from '../../i18n/LocaleProvider';
import { formatDateTime, formatRelativeTime, relativeTimeMessages } from '../date-time';

const NOW = Date.UTC(2026, 9, 10, 12, 0, 0);
const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

// The `en` path passes `'en'` itself to the relative formatter, so the words are English
// regardless of the machine's default locale (which jsdom follows).
describe('formatRelativeTime', () => {
  it('returns "just now" under 45 seconds', () => {
    expect(formatRelativeTime(NOW - 10 * 1000, 'en', NOW)).toBe('just now');
    expect(formatRelativeTime(NOW, 'en', NOW)).toBe('just now');
  });

  it('formats minutes', () => {
    expect(formatRelativeTime(NOW - 5 * MINUTE, 'en', NOW)).toBe('5 minutes ago');
  });

  it('formats hours', () => {
    expect(formatRelativeTime(new Date(NOW - 3 * HOUR), 'en', NOW)).toBe('3 hours ago');
  });

  it('formats one day as "yesterday"', () => {
    expect(formatRelativeTime(new Date(NOW - DAY - HOUR).toISOString(), 'en', NOW)).toBe('yesterday');
  });

  it('switches to the absolute date/time at 7 days or older', () => {
    const eightDaysAgo = NOW - 8 * DAY;
    expect(formatRelativeTime(eightDaysAgo, 'en', NOW)).toBe(new Date(eightDaysAgo).toLocaleString());
  });

  it('returns "" for an invalid value', () => {
    expect(formatRelativeTime('not a date', 'en', NOW)).toBe('');
    expect(formatRelativeTime(new Date(NaN), 'en', NOW)).toBe('');
  });

  it('treats a future timestamp (clock skew) as "just now"', () => {
    expect(formatRelativeTime(NOW + 5 * MINUTE, 'en', NOW)).toBe('just now');
  });

  it('formats the pseudo-locale with the English words', () => {
    expect(formatRelativeTime(NOW - 5 * MINUTE, 'en-XA', NOW)).toBe('5 minutes ago');
  });

  describe('under fr-CA', () => {
    it('formats minutes', () => {
      expect(formatRelativeTime(NOW - 5 * MINUTE, 'fr-CA', NOW)).toBe('il y a 5 minutes');
    });

    it('formats hours', () => {
      expect(formatRelativeTime(new Date(NOW - 3 * HOUR), 'fr-CA', NOW)).toBe('il y a 3 heures');
    });

    it('formats one day as "hier"', () => {
      expect(formatRelativeTime(NOW - DAY - HOUR, 'fr-CA', NOW)).toBe('hier');
    });

    it("returns the caller's just-now text", () => {
      const intl = createIntl({
        locale: 'fr-CA',
        defaultLocale: 'en',
        messages: { 'common.relativeTime.justNow': 'à l’instant' },
        onError: onIntlError,
      });
      const justNow = intl.formatMessage(relativeTimeMessages.justNow);
      expect(formatRelativeTime(NOW - 10 * 1000, 'fr-CA', NOW, justNow)).toBe('à l’instant');
      expect(formatRelativeTime(NOW + 5 * MINUTE, 'fr-CA', NOW, justNow)).toBe('à l’instant');
    });
  });
});

describe('formatDateTime', () => {
  it('formats a valid value with toLocaleString', () => {
    expect(formatDateTime(NOW, 'en')).toBe(new Date(NOW).toLocaleString());
    expect(formatDateTime(new Date(NOW).toISOString(), 'en')).toBe(new Date(NOW).toLocaleString());
  });

  it('formats in the locale under fr-CA', () => {
    expect(formatDateTime(NOW, 'fr-CA')).toBe(formatAbsoluteDateTime(new Date(NOW), 'fr-CA'));
  });

  it('returns "" for an invalid value', () => {
    expect(formatDateTime('garbage', 'en')).toBe('');
  });
});
