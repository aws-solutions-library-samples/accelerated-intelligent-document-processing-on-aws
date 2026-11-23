// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/** The shared UTC date-range rules (document lists and bulk Skip All Reviews). */

import { createIntl } from 'react-intl';
import { describe, expect, it } from 'vitest';

import { lastDaysRange, toIsoRange, validateDateRange } from '../DateRangeFields';

const intl = createIntl({ locale: 'en', defaultLocale: 'en' });
const range = (startDate: string, endDate: string) => ({ startDate, startTime: '00:00:00', endDate, endTime: '23:59:59' });

describe('DateRangeFields rules', () => {
  it('accepts a range of up to 365 days', () => {
    expect(validateDateRange(range('2026-01-01', '2026-12-31'), intl)).toBeNull();
  });

  it('refuses more than 365 days, a reversed range and missing dates', () => {
    expect(validateDateRange(range('2025-01-01', '2026-12-31'), intl)).toMatch(/365 days/);
    expect(validateDateRange(range('2026-02-01', '2026-01-01'), intl)).toMatch(/before/);
    expect(validateDateRange(range('', '2026-01-01'), intl)).toMatch(/required/);
  });

  it('builds UTC ISO strings', () => {
    expect(toIsoRange(range('2026-01-01', '2026-01-02'))).toEqual({
      startDateTime: '2026-01-01T00:00:00.000Z',
      endDateTime: '2026-01-02T23:59:59.000Z',
    });
    expect(lastDaysRange(7, new Date('2026-03-08T12:00:00Z')).startDate).toBe('2026-03-01');
  });
});
