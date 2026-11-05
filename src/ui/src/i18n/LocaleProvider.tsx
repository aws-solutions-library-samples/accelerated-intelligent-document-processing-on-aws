// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { IntlProvider, ReactIntlErrorCode, type IntlConfig } from 'react-intl';
import { ConsoleLogger } from 'aws-amplify/utils';
import { DEFAULT_LOCALE, ENABLED_LOCALES, LOCALE_STORAGE_KEY, isEnabledLocale, type Locale } from './locales';
import { readResolvedLocale } from './resolveLocale';

const logger = new ConsoleLogger('LocaleProvider');

export interface LocaleContextValue {
  locale: Locale;
  /** Switch the UI locale and remember it. A value outside `enabledLocales` is ignored. */
  setLocale: (next: string) => void;
  enabledLocales: readonly Locale[];
}

const LocaleContext = createContext<LocaleContextValue | null>(null);

/**
 * No catalog yet: English stays inline in each message's `defaultMessage`, so every lookup
 * misses and react-intl renders that default. A later commit in this PR adds the per-locale
 * catalog loader; until then this frozen object is the message table for every locale.
 */
const EMPTY_MESSAGES: Record<string, string> = Object.freeze({});

/** Errors already logged, by code and message, so a defect that re-renders costs one line. */
const reported = new Set<string>();

/**
 * react-intl's error policy. MISSING_TRANSLATION is swallowed outright: falling back to the
 * English `defaultMessage` is the design, and the coverage report, not the console, tracks the
 * gaps. Every other code (a malformed ICU message, missing Intl data, an invalid config) is a
 * defect, logged once per distinct error. Never throws: a bad message costs one string, not the
 * screen.
 */
export const onIntlError: NonNullable<IntlConfig['onError']> = (error) => {
  if (error.code === ReactIntlErrorCode.MISSING_TRANSLATION) return;
  const key = `${error.code}:${error.message}`;
  if (reported.has(key)) return;
  reported.add(key);
  logger.warn(`${error.code}: ${error.message}`, error);
};

export interface LocaleProviderProps {
  children: React.ReactNode;
  /** The allow-list in force. Defaults to `ENABLED_LOCALES`; tests pass a wider list. */
  enabledLocales?: readonly Locale[];
}

/**
 * Owns the UI locale: resolves it once from storage and the browser (never the URL), exposes
 * it with a validated `setLocale`, keeps `<html lang>` in step, and mounts react-intl's
 * `IntlProvider`. `textComponent` is left at its default (`React.Fragment`) so English output
 * is byte-identical to the pre-i18n markup.
 */
export const LocaleProvider = ({ children, enabledLocales = ENABLED_LOCALES }: LocaleProviderProps): React.JSX.Element => {
  const [locale, setLocaleState] = useState<Locale>(() => readResolvedLocale(enabledLocales));

  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  const setLocale = useCallback(
    (next: string): void => {
      if (!isEnabledLocale(next, enabledLocales)) return;
      try {
        window.localStorage.setItem(LOCALE_STORAGE_KEY, next);
      } catch {
        // Storage disabled or full: the choice still applies to this page.
      }
      setLocaleState(next);
    },
    [enabledLocales],
  );

  const value = useMemo<LocaleContextValue>(() => ({ locale, setLocale, enabledLocales }), [locale, setLocale, enabledLocales]);

  return (
    <LocaleContext.Provider value={value}>
      <IntlProvider locale={locale} defaultLocale={DEFAULT_LOCALE} messages={EMPTY_MESSAGES} onError={onIntlError}>
        {children}
      </IntlProvider>
    </LocaleContext.Provider>
  );
};

export const useLocale = (): LocaleContextValue => {
  const ctx = useContext(LocaleContext);
  if (!ctx) throw new Error('useLocale must be used within LocaleProvider');
  return ctx;
};
