// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { DEFAULT_LOCALE, ENABLED_LOCALES, LOCALE_STORAGE_KEY, isEnabledLocale, type Locale } from './locales';

export interface ResolveLocaleInput {
  /** The user's stored choice, or null when there is none or storage is unreadable. */
  stored: string | null;
  /** The browser's preferred languages, most preferred first (`navigator.languages`). */
  browserLanguages: readonly string[];
  /** The allow-list every result is drawn from. */
  enabled: readonly Locale[];
  /** Returned when nothing matches. */
  fallback: Locale;
}

/** `fr-FR` → `fr`, `en` → `en`. Input is already lower-cased by the callers. */
const languageSubtag = (tag: string): string => tag.split('-')[0];

/**
 * Pick the UI locale from an allow-list (decision D19 of #1770):
 *
 * 1. A stored choice counts only when it exactly equals an enabled locale after `trim()`:
 *    same case, no subtag matching. The switcher wrote it from that same list, so any other
 *    value is stale or tampered and is ignored.
 * 2. Otherwise the browser's languages are walked in order. Each is tried first as an exact
 *    (case-insensitive) match against the enabled list, then by language subtag, so `fr-FR`
 *    and `fr` resolve to `fr-CA` once `fr-CA` is enabled and `en-GB` resolves to `en`. The
 *    first language with a hit wins.
 * 3. Otherwise `fallback`.
 *
 * The URL (`location.search`, `location.hash`) is never consulted. The locale is a closed
 * allow-list, so a URL parameter could only ever repeat a value the browser or storage
 * already supplies, and reading one would add an injection surface for nothing.
 */
export const resolveLocale = ({ stored, browserLanguages, enabled, fallback }: ResolveLocaleInput): Locale => {
  if (stored !== null) {
    const choice = stored.trim();
    if (isEnabledLocale(choice, enabled)) return choice;
  }
  for (const language of browserLanguages) {
    const wanted = language.toLowerCase();
    const exact = enabled.find((locale) => locale.toLowerCase() === wanted);
    if (exact) return exact;
    const subtag = languageSubtag(wanted);
    const sameLanguage = enabled.find((locale) => languageSubtag(locale.toLowerCase()) === subtag);
    if (sameLanguage) return sameLanguage;
  }
  return fallback;
};

const readStoredLocale = (): string | null => {
  try {
    return window.localStorage.getItem(LOCALE_STORAGE_KEY);
  } catch {
    // Storage disabled or throwing (private mode, blocked storage): no stored choice.
    return null;
  }
};

const readBrowserLanguages = (): readonly string[] => {
  if (navigator.languages && navigator.languages.length > 0) return navigator.languages;
  return navigator.language ? [navigator.language] : [];
};

/** The locale for this browser: stored choice, else browser languages, else `DEFAULT_LOCALE`. */
export const readResolvedLocale = (enabled: readonly Locale[] = ENABLED_LOCALES): Locale =>
  resolveLocale({ stored: readStoredLocale(), browserLanguages: readBrowserLanguages(), enabled, fallback: DEFAULT_LOCALE });
