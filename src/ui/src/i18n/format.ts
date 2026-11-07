// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Display-only formatting that follows the UI locale (decisions D13 to D15 of #1770): dates,
 * times, numbers, percentages and US-dollar amounts, each built on `Intl` with the locale
 * `formattingLocale` picks.
 *
 * The one rule about WHICH `Intl` locale is used lives here, in `formattingLocale`: under `en`
 * (and the pseudo-locale `en-XA`, whose text is English) the helpers pass NO locale, so `Intl`
 * uses the runtime default, which is exactly what the UI's bare `toLocaleString()` calls have
 * always used; an English user sees no change. Under any other locale they pass that locale's
 * tag (`fr-CA`). The time zone is never set by a helper: it stays the browser's, as today.
 *
 * Display only. Every helper exists to put a value in front of a reader in their language. A
 * TECHNICAL value, one that is parsed back (a form field, a value compared or sorted as text),
 * exported (CSV, JSON, markdown) or copied to the clipboard, must keep locale-neutral
 * formatting (`toFixed`, `String`, `JSON.stringify`) and must not go through these helpers:
 * `1 234,50` round-trips through `parseFloat` as `1`.
 *
 * Nothing calls these yet. PR 2 of the series moves the existing call sites over, classifying
 * each `toFixed` site as prose or technical (D15), and records the one visible change, `US$`
 * in place of `$`, in the CHANGELOG.
 */
import { useMemo } from 'react';
import type { Locale } from './locales';
import { useLocale } from './LocaleProvider';

/** Locales whose text is English: they format with the runtime default, as today's bare calls do. */
const ENGLISH_LOCALES: readonly Locale[] = ['en', 'en-XA'];

/**
 * The locale argument every helper hands to `Intl` for `locale`: `undefined` for `en` and
 * `en-XA`, which means the runtime default (D14: byte-identical to a bare `toLocaleString()`),
 * and the tag itself for every other locale (`fr-CA` → `'fr-CA'`).
 */
export const formattingLocale = (locale: Locale): string | undefined => (ENGLISH_LOCALES.includes(locale) ? undefined : locale);

/**
 * How many fraction digits to show: one number for exactly that many, or a range. Stated by
 * the caller so each site keeps the precision it has today and nothing rounds by accident.
 */
export type FractionDigits = number | { minimum: number; maximum: number };

const fractionDigitsOptions = (fractionDigits: FractionDigits): Intl.NumberFormatOptions =>
  typeof fractionDigits === 'number'
    ? { minimumFractionDigits: fractionDigits, maximumFractionDigits: fractionDigits }
    : { minimumFractionDigits: fractionDigits.minimum, maximumFractionDigits: fractionDigits.maximum };

/**
 * Date and time for display, as `date.toLocaleString()` shows them: all six fields, numeric,
 * in the browser's time zone, unless `options` says otherwise. Any `Intl.DateTimeFormatOptions`
 * pass straight through, with the built-in's own defaulting (naming any field or style turns the
 * defaults off), so a site that passes `{ month: 'short', day: 'numeric', hour: '2-digit',
 * minute: '2-digit' }` today renders the same. Display only; never for a value parsed back.
 */
export const formatDateTime = (date: Date, locale: Locale, options?: Intl.DateTimeFormatOptions): string =>
  date.toLocaleString(formattingLocale(locale), options);

/**
 * The date alone, as `date.toLocaleDateString()` shows it (year, month and day, numeric) unless
 * `options` says otherwise. Display only; never for a value parsed back or exported.
 */
export const formatDate = (date: Date, locale: Locale, options?: Intl.DateTimeFormatOptions): string =>
  date.toLocaleDateString(formattingLocale(locale), options);

/**
 * The time alone, as `date.toLocaleTimeString()` shows it (hour, minute and second, numeric)
 * unless `options` says otherwise. Display only; never for a value parsed back or exported.
 */
export const formatTime = (date: Date, locale: Locale, options?: Intl.DateTimeFormatOptions): string =>
  date.toLocaleTimeString(formattingLocale(locale), options);

/**
 * A number for prose, as `value.toLocaleString()` shows it (grouped, up to three fraction
 * digits) unless `options` says otherwise. Display only: a value that is parsed back, exported
 * or copied keeps `String(value)`.
 */
export const formatNumber = (value: number, locale: Locale, options?: Intl.NumberFormatOptions): string =>
  value.toLocaleString(formattingLocale(locale), options);

export interface FixedOptions {
  /** `false` drops the thousands separator; the default keeps the locale's grouping. */
  useGrouping?: boolean;
}

/**
 * A number with exactly `fractionDigits` decimals, for the prose `toFixed(n)` sites: the same
 * digits, with the locale's decimal mark and grouping (`1 234,50` under fr-CA; pass
 * `{ useGrouping: false }` for `1234,50`). Display only (D15): a `toFixed` whose result is
 * parsed back, exported or copied stays `toFixed`, which is locale-neutral by definition.
 */
export const formatFixed = (value: number, locale: Locale, fractionDigits: number, { useGrouping = true }: FixedOptions = {}): string =>
  new Intl.NumberFormat(formattingLocale(locale), {
    ...fractionDigitsOptions(fractionDigits),
    // Omitted when grouping is wanted, so the locale's own default applies (as `toLocaleString()`).
    ...(useGrouping ? {} : { useGrouping: false }),
  }).format(value);

export interface PercentOptions {
  fractionDigits: FractionDigits;
}

/**
 * A percentage from a RATIO: `0.125` → `12.5%` (en), `12,5 %` (fr-CA, narrow no-break space
 * before the sign). `Intl` does the ×100, so a site that computes `(x * 100).toFixed(1) + '%'`
 * today passes `x`, not `x * 100`, with `{ fractionDigits: 1 }`. Display only.
 */
export const formatPercent = (ratio: number, locale: Locale, { fractionDigits }: PercentOptions): string =>
  new Intl.NumberFormat(formattingLocale(locale), { style: 'percent', ...fractionDigitsOptions(fractionDigits) }).format(ratio);

export interface UsDollarsOptions {
  /**
   * Required: costs show sub-cent amounts, and `Intl`'s currency default of two digits would
   * round `0.0123` to `0.01`. State today's precision at each site.
   */
  fractionDigits: FractionDigits;
}

/** The currency symbol every locale shows (D13), replacing whatever `Intl` would print for USD. */
const US_DOLLARS = 'US$';

/**
 * A US-dollar amount with the literal `US$` in every locale (D13): `US$0.0123` and `US$1,234.50`
 * in English, `0,0123 US$` and `1 234,50 US$` in French, `-US$3.50` for a negative. `Intl`
 * formats the amount as the locale's currency style, and only its `currency` part is replaced:
 * digits, group and decimal marks, the space before a trailing symbol and the minus sign are
 * exactly what the locale produces. No locale is special-cased. Display only: a cost that is
 * exported, copied, or compared or sorted as text keeps its numeric source; `parseFloat` of
 * this string is wrong in every locale.
 */
export const formatUsDollars = (amount: number, locale: Locale, { fractionDigits }: UsDollarsOptions): string =>
  new Intl.NumberFormat(formattingLocale(locale), { style: 'currency', currency: 'USD', ...fractionDigitsOptions(fractionDigits) })
    .formatToParts(amount)
    .map((part) => (part.type === 'currency' ? US_DOLLARS : part.value))
    .join('');

/** The helpers above with the locale already supplied; what `useFormatters` returns. */
export interface Formatters {
  formatDateTime: (date: Date, options?: Intl.DateTimeFormatOptions) => string;
  formatDate: (date: Date, options?: Intl.DateTimeFormatOptions) => string;
  formatTime: (date: Date, options?: Intl.DateTimeFormatOptions) => string;
  formatNumber: (value: number, options?: Intl.NumberFormatOptions) => string;
  formatFixed: (value: number, fractionDigits: number, options?: FixedOptions) => string;
  formatPercent: (ratio: number, options: PercentOptions) => string;
  formatUsDollars: (amount: number, options: UsDollarsOptions) => string;
}

const bindFormatters = (locale: Locale): Formatters => ({
  formatDateTime: (date, options) => formatDateTime(date, locale, options),
  formatDate: (date, options) => formatDate(date, locale, options),
  formatTime: (date, options) => formatTime(date, locale, options),
  formatNumber: (value, options) => formatNumber(value, locale, options),
  formatFixed: (value, fractionDigits, options) => formatFixed(value, locale, fractionDigits, options),
  formatPercent: (ratio, options) => formatPercent(ratio, locale, options),
  formatUsDollars: (amount, options) => formatUsDollars(amount, locale, options),
});

/**
 * The helpers bound to the current UI locale, for components: one object per locale, stable
 * across re-renders (safe in dependency lists), replaced when the locale changes so everything
 * formatted re-renders in the new one. Must be used within `LocaleProvider`.
 */
export const useFormatters = (): Formatters => {
  const { locale } = useLocale();
  return useMemo(() => bindFormatters(locale), [locale]);
};
