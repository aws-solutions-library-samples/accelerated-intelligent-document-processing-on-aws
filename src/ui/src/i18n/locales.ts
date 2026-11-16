// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The UI locale allow-list. Every decision about which language the UI shows goes
 * through these constants: the resolver (`resolveLocale.ts`) returns only members of
 * the enabled list, `<html lang>` is set only from it, and the language switcher
 * offers only `ENABLED_LOCALES`.
 */
// A Vite virtual module served by scripts/i18n/vite-plugin-catalogs.mjs (typed in
// ./virtual-catalogs.d.ts).
import { availableLocales } from 'virtual:i18n-catalogs';

/** Every locale the UI has code for. `en-XA` is the pseudo-locale that reveals clipped layouts. */
export const KNOWN_LOCALES = ['en', 'fr-CA', 'en-XA'] as const;

export type Locale = (typeof KNOWN_LOCALES)[number];

export const DEFAULT_LOCALE: Locale = 'en';

/**
 * Locales a user can get: the known locales whose catalog file (`src/i18n/<locale>.json`)
 * the build found, in `KNOWN_LOCALES` order. `en` is always there because `en.json`,
 * the extracted English source catalog, is committed. `fr-CA` joins the moment its
 * reviewed catalog lands, and `en-XA` only on builds that write the pseudo-locale
 * catalog. A catalog file nobody declared in `KNOWN_LOCALES` enables nothing.
 */
export const ENABLED_LOCALES: readonly Locale[] = KNOWN_LOCALES.filter((locale) => availableLocales.includes(locale));

/** localStorage key: the user's explicit locale choice, always an exact `ENABLED_LOCALES` member. */
export const LOCALE_STORAGE_KEY = 'idp-locale';

/** Switcher labels, each written in its own language so a user can recognise theirs. */
export const LOCALE_DISPLAY_NAMES: Readonly<Record<Locale, string>> = {
  en: 'English',
  'fr-CA': 'Français (Canada)',
  'en-XA': 'Pseudo-locale (test)',
};

/**
 * True when `value` is exactly one of `enabled` (default: `ENABLED_LOCALES`): same string,
 * same case. Callers trim before asking; nothing else is normalised.
 */
export const isEnabledLocale = (value: unknown, enabled: readonly Locale[] = ENABLED_LOCALES): value is Locale =>
  typeof value === 'string' && (enabled as readonly string[]).includes(value);
