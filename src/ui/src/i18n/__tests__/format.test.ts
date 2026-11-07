// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The display-only formatting helpers (decisions D13 to D15 of #1770). jsdom's `Intl` is Node's
 * ICU and follows the process locale (`C.UTF-8` in CI, possibly `fr-CA` on a developer's machine),
 * so every pinned string here passes an explicit locale and the `en` path is asserted against the
 * bare built-in it must reproduce, which holds on any machine. Where ICU emits a space whose code
 * point has changed between releases (the narrow no-break space in `12,5 %`, the space before
 * `PM`), the assertion matches `\s` rather than one code point.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  formatDate,
  formatDateTime,
  formatFixed,
  formatNumber,
  formatPercent,
  formatTime,
  formatUsDollars,
  formattingLocale,
  type FractionDigits,
} from '../format';
import type { Locale } from '../locales';

/** 2026-10-30 17:40:05 UTC; every pinned date string also passes `timeZone: 'UTC'`. */
const DATE = new Date(Date.UTC(2026, 9, 30, 17, 40, 5));
const UTC = { timeZone: 'UTC' } as const;

// Real Intl tags outside the UI's allow-list: the helpers special-case no locale, and a pinned
// literal needs a locale the machine's default cannot change.
const EN_US = 'en-US' as Locale;
const EN_GB = 'en-GB' as Locale;
const DE_DE = 'de-DE' as Locale;

describe('formattingLocale', () => {
  it('hands Intl no locale under en, so the runtime default applies as for a bare toLocaleString()', () => {
    expect(formattingLocale('en')).toBeUndefined();
  });

  it('treats the pseudo-locale as English', () => {
    expect(formattingLocale('en-XA')).toBeUndefined();
  });

  it('passes every other locale through as its own tag', () => {
    expect(formattingLocale('fr-CA')).toBe('fr-CA');
  });
});

describe('under en (D14)', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('formats dates and times byte-identically to the bare built-ins, in the browser time zone', () => {
    expect(formatDateTime(DATE, 'en')).toBe(DATE.toLocaleString());
    expect(formatDate(DATE, 'en')).toBe(DATE.toLocaleDateString());
    expect(formatTime(DATE, 'en')).toBe(DATE.toLocaleTimeString());
    expect(formatDateTime(DATE, 'en', UTC)).toBe(DATE.toLocaleString(undefined, UTC));
  });

  it.each([1234.56, 0.0123, 1e6, -42.5, 0])('formats %d byte-identically to Number#toLocaleString()', (value) => {
    expect(formatNumber(value, 'en')).toBe(value.toLocaleString());
  });

  it('calls the built-ins with no locale under en and with the tag under fr-CA', () => {
    // The outcome above holds on an en-US machine even if `en` were mapped to `'en-US'`; the
    // mechanism is what D14 promises, so it is asserted directly.
    const dateSpy = vi.spyOn(Date.prototype, 'toLocaleString');
    const numberSpy = vi.spyOn(Number.prototype, 'toLocaleString');
    formatDateTime(DATE, 'en');
    formatDateTime(DATE, 'en-XA', UTC);
    formatDateTime(DATE, 'fr-CA');
    formatNumber(1234.56, 'en');
    formatNumber(1234.56, 'fr-CA', { maximumFractionDigits: 1 });
    expect(dateSpy.mock.calls).toEqual([
      [undefined, undefined],
      [undefined, UTC],
      ['fr-CA', undefined],
    ]);
    expect(numberSpy.mock.calls).toEqual([
      [undefined, undefined],
      ['fr-CA', { maximumFractionDigits: 1 }],
    ]);
  });

  it('pins the en-US output the English UI shows on an en-US browser', () => {
    expect(formatDateTime(DATE, EN_US, UTC)).toMatch(/^10\/30\/2026, 5:40:05\sPM$/);
    expect(formatDate(DATE, EN_US, UTC)).toBe('10/30/2026');
    expect(formatTime(DATE, EN_US, UTC)).toMatch(/^5:40:05\sPM$/);
    expect(formatNumber(1234.56, EN_US)).toBe('1,234.56');
    expect(formatNumber(1e6, EN_US)).toBe('1,000,000');
    expect(formatNumber(1234.5678, EN_US, { maximumFractionDigits: 1 })).toBe('1,234.6');
    expect(formatPercent(0.125, EN_US, { fractionDigits: 1 })).toBe('12.5%');
  });
});

describe('under fr-CA', () => {
  it('formats the date and time the Canadian French way', () => {
    expect(formatDateTime(DATE, 'fr-CA', UTC)).toBe('2026-10-30 17 h 40 min 05 s');
    expect(formatDate(DATE, 'fr-CA', { dateStyle: 'long', ...UTC })).toBe('30 octobre 2026');
    expect(formatTime(DATE, 'fr-CA', { timeStyle: 'short', ...UTC })).toBe('17 h 40');
  });

  it('keeps the built-in defaulting: naming some fields turns the other defaults off', () => {
    // The Discovery screens pass exactly these options today; year and second must not appear.
    const partial = { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', ...UTC } as const;
    expect(formatDateTime(DATE, 'fr-CA', partial)).toBe(DATE.toLocaleString('fr-CA', partial));
    expect(formatDateTime(DATE, 'fr-CA', partial)).not.toContain('2026');
    expect(formatDateTime(DATE, EN_US, partial)).toMatch(/^Oct 30, 05:40\sPM$/);
  });

  it('groups with a no-break space and uses a decimal comma', () => {
    expect(formatNumber(1234.56, 'fr-CA')).toMatch(/^1\s234,56$/);
    expect(formatNumber(-42.5, 'fr-CA')).toBe('-42,5');
  });

  it('formats a ratio as a percentage with a space before the sign', () => {
    expect(formatPercent(0.125, 'fr-CA', { fractionDigits: 1 })).toMatch(/^12,5\s%$/);
    expect(formatPercent(1, 'fr-CA', { fractionDigits: 0 })).toMatch(/^100\s%$/);
  });
});

describe('formatFixed', () => {
  it('shows exactly the requested decimals, like toFixed, in the locale', () => {
    expect(formatFixed(3.14159, 'en', 2)).toBe('3.14');
    expect(formatFixed(3.14159, EN_US, 2)).toBe('3.14');
    expect(formatFixed(2, EN_US, 2)).toBe('2.00');
    expect(formatFixed(1234.5, 'fr-CA', 2)).toMatch(/^1\s234,50$/);
  });

  it('drops grouping on request and keeps the locale default otherwise', () => {
    expect(formatFixed(1234.5, 'fr-CA', 2, { useGrouping: false })).toBe('1234,50');
    expect(formatFixed(1234.5, EN_US, 2, { useGrouping: false })).toBe('1234.50');
    expect(formatFixed(1234.5, EN_US, 2, { useGrouping: true })).toBe('1,234.50');
    expect(formatFixed(1234.5, EN_US, 2, {})).toBe('1,234.50');
  });
});

describe('formatPercent', () => {
  it('takes a 0 to 1 ratio, as the evaluation scores and confidences are stored', () => {
    expect(formatPercent(0.9345, EN_US, { fractionDigits: 1 })).toBe('93.5%');
    expect(formatPercent(0, EN_US, { fractionDigits: 1 })).toBe('0.0%');
    expect(formatPercent(1, EN_US, { fractionDigits: 0 })).toBe('100%');
    expect(formatPercent(0.12345, EN_US, { fractionDigits: { minimum: 0, maximum: 2 } })).toBe('12.35%');
  });
});

describe('formatUsDollars (D13)', () => {
  const USD = { style: 'currency', currency: 'USD' } as const;
  const digits = (fractionDigits: FractionDigits): Intl.NumberFormatOptions =>
    typeof fractionDigits === 'number'
      ? { minimumFractionDigits: fractionDigits, maximumFractionDigits: fractionDigits }
      : { minimumFractionDigits: fractionDigits.minimum, maximumFractionDigits: fractionDigits.maximum };

  /** `Intl`'s own string for the amount, with only its currency symbol replaced by `US$`. */
  const intlWithUsDollars = (locale: string | undefined, amount: number, fractionDigits: FractionDigits): string => {
    const formatter = new Intl.NumberFormat(locale, { ...USD, ...digits(fractionDigits) });
    const symbol = formatter.formatToParts(amount).find((part) => part.type === 'currency');
    expect(symbol).toBeDefined();
    return formatter.format(amount).replace(symbol?.value ?? '', 'US$');
  };

  it('keeps the fraction digits the caller states, so sub-cent costs are not rounded away', () => {
    expect(formatUsDollars(0.0123, EN_US, { fractionDigits: 4 })).toBe('US$0.0123');
    expect(formatUsDollars(1234.5, EN_US, { fractionDigits: 2 })).toBe('US$1,234.50');
    expect(formatUsDollars(0.0123, EN_US, { fractionDigits: { minimum: 2, maximum: 4 } })).toBe('US$0.0123');
    expect(formatUsDollars(1.5, EN_US, { fractionDigits: { minimum: 2, maximum: 4 } })).toBe('US$1.50');
    // What Intl would do on its own, and why the digits are required.
    expect(new Intl.NumberFormat(EN_US, USD).format(0.0123)).toBe('$0.01');
  });

  it('shows US$ in a locale whose own symbol is already US$ (en-GB) without doubling it', () => {
    expect(new Intl.NumberFormat(EN_GB, { ...USD, ...digits(2) }).format(1234.5)).toBe('US$1,234.50');
    expect(formatUsDollars(1234.5, EN_GB, { fractionDigits: 2 })).toBe('US$1,234.50');
  });

  it('keeps the French digits, separators and trailing symbol position', () => {
    expect(formatUsDollars(0.0123, 'fr-CA', { fractionDigits: 4 })).toMatch(/^0,0123\sUS\$$/);
    expect(formatUsDollars(1234.5, 'fr-CA', { fractionDigits: 2 })).toMatch(/^1\s234,50\sUS\$$/);
  });

  it('special-cases no locale: German shows its own digits with US$ where it would print $', () => {
    expect(new Intl.NumberFormat(DE_DE, { ...USD, ...digits(2) }).format(1234.5)).toMatch(/^1\.234,50\s\$$/);
    expect(formatUsDollars(1234.5, DE_DE, { fractionDigits: 2 })).toMatch(/^1\.234,50\sUS\$$/);
  });

  it('keeps the minus sign where the locale puts it', () => {
    expect(formatUsDollars(-3.5, EN_US, { fractionDigits: 2 })).toBe('-US$3.50');
    expect(formatUsDollars(-3.5, 'fr-CA', { fractionDigits: 2 })).toMatch(/^-3,50\sUS\$$/);
  });

  it.each<[Locale, number, FractionDigits]>([
    [EN_US, 0.0123, 4],
    [EN_US, 1234.5, 2],
    [EN_US, -3.5, 2],
    [EN_GB, 1234.5, 2],
    ['fr-CA', 0.0123, 4],
    ['fr-CA', 1234.5, 2],
    ['fr-CA', -3.5, 2],
    [DE_DE, 1234.5, 2],
    [EN_US, 1.5, { minimum: 2, maximum: 4 }],
  ])("alters nothing but the currency part of Intl's own output (%s, %d)", (locale, amount, fractionDigits) => {
    expect(formatUsDollars(amount, locale, { fractionDigits })).toBe(intlWithUsDollars(locale, amount, fractionDigits));
  });

  it('under en formats with the runtime default, whatever it is on this machine', () => {
    // No literal: the default may be en-US in CI and fr-CA on a Québec machine. Either way the
    // result is the default locale's own formatting with its currency symbol swapped for `US$`.
    expect(formatUsDollars(0.0123, 'en', { fractionDigits: 4 })).toBe(intlWithUsDollars(undefined, 0.0123, 4));
    expect(formatUsDollars(1234.5, 'en', { fractionDigits: 2 })).toBe(intlWithUsDollars(undefined, 1234.5, 2));
    expect(formatUsDollars(1234.5, 'en', { fractionDigits: 2 })).toContain('US$');
  });
});
