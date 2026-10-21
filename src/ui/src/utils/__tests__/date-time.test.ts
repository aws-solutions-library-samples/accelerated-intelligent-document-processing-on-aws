// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { describe, expect, it } from 'vitest';

import { formatDateTime, formatRelativeTime } from '../date-time';

const NOW = Date.UTC(2026, 9, 10, 12, 0, 0);
const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

describe('formatRelativeTime', () => {
  it('returns "just now" under 45 seconds', () => {
    expect(formatRelativeTime(NOW - 10 * 1000, NOW)).toBe('just now');
    expect(formatRelativeTime(NOW, NOW)).toBe('just now');
  });

  it('formats minutes', () => {
    expect(formatRelativeTime(NOW - 5 * MINUTE, NOW)).toBe('5 minutes ago');
  });

  it('formats hours', () => {
    expect(formatRelativeTime(new Date(NOW - 3 * HOUR), NOW)).toBe('3 hours ago');
  });

  it('formats one day as "yesterday"', () => {
    expect(formatRelativeTime(new Date(NOW - DAY - HOUR).toISOString(), NOW)).toBe('yesterday');
  });

  it('switches to the absolute date/time at 7 days or older', () => {
    const eightDaysAgo = NOW - 8 * DAY;
    expect(formatRelativeTime(eightDaysAgo, NOW)).toBe(new Date(eightDaysAgo).toLocaleString());
  });

  it('returns "" for an invalid value', () => {
    expect(formatRelativeTime('not a date', NOW)).toBe('');
    expect(formatRelativeTime(new Date(NaN), NOW)).toBe('');
  });

  it('treats a future timestamp (clock skew) as "just now"', () => {
    expect(formatRelativeTime(NOW + 5 * MINUTE, NOW)).toBe('just now');
  });
});

describe('formatDateTime', () => {
  it('formats a valid value with toLocaleString', () => {
    expect(formatDateTime(NOW)).toBe(new Date(NOW).toLocaleString());
    expect(formatDateTime(new Date(NOW).toISOString())).toBe(new Date(NOW).toLocaleString());
  });

  it('returns "" for an invalid value', () => {
    expect(formatDateTime('garbage')).toBe('');
  });
});
