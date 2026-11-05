// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { I18nProvider } from '@cloudscape-design/components/i18n';
import { IntlProvider, ReactIntlErrorCode, type IntlConfig } from 'react-intl';
import { ConsoleLogger } from 'aws-amplify/utils';
import { applyAmplifyLanguage, registerAmplifyVocabularies } from './amplify';
import { useCloudscapeMessages } from './cloudscape';
import { EMPTY_CATALOG, loadCatalog, type CatalogMessages } from './loadCatalog';
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

/** Locales whose catalog failed to load, so the failure is reported once. */
const failedCatalogs = new Set<string>();

interface LoadedCatalog {
  locale: Locale;
  messages: CatalogMessages;
}

/**
 * The compiled message catalog for `locale`, loaded lazily so each locale ships as its own
 * chunk. Under `en` nothing is loaded and the table is `EMPTY_CATALOG`: every lookup misses
 * and react-intl renders the inline English `defaultMessage`, exactly as before catalogs
 * existed. For any other locale the children render at once with that same English while the
 * catalog loads, a catalog that arrives after the locale changed again is ignored, and a load
 * that fails is logged once per locale and leaves English in place: a missing catalog costs
 * translations, never the screen.
 */
const useCatalogMessages = (locale: Locale): CatalogMessages => {
  const [loaded, setLoaded] = useState<LoadedCatalog | null>(null);

  useEffect(() => {
    if (locale === DEFAULT_LOCALE) return undefined;
    let current = true;
    loadCatalog(locale).then(
      (messages) => {
        if (current) setLoaded({ locale, messages });
      },
      (error: unknown) => {
        if (failedCatalogs.has(locale)) return;
        failedCatalogs.add(locale);
        logger.warn(`Message catalog for ${locale} did not load; English stays`, error);
      },
    );
    return () => {
      current = false;
    };
  }, [locale]);

  // The state remembers which locale it holds, so the render right after a switch already
  // returns the right table: `en` never sees another locale's messages.
  return loaded !== null && loaded.locale === locale ? loaded.messages : EMPTY_CATALOG;
};

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
 * it with a validated `setLocale`, keeps `<html lang>` in step, points Amplify UI at it, and
 * mounts react-intl's `IntlProvider` (with the locale's compiled catalog) and Cloudscape's
 * `I18nProvider`. `textComponent` is left at its default (`React.Fragment`), and under `en`
 * neither provider gets any messages, so English output is byte-identical to the pre-i18n
 * markup.
 */
export const LocaleProvider = ({ children, enabledLocales = ENABLED_LOCALES }: LocaleProviderProps): React.JSX.Element => {
  const [locale, setLocaleState] = useState<Locale>(() => {
    const resolved = readResolvedLocale(enabledLocales);
    // Amplify UI reads `I18n` while it renders, so its language is set here, before the
    // first child renders, not in an effect (which would run after the sign-in form has
    // already rendered once in the browser's language).
    registerAmplifyVocabularies();
    applyAmplifyLanguage(resolved);
    return resolved;
  });
  const messages = useCatalogMessages(locale);
  const cloudscapeMessages = useCloudscapeMessages(locale);

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
      applyAmplifyLanguage(next);
      setLocaleState(next);
    },
    [enabledLocales],
  );

  const value = useMemo<LocaleContextValue>(() => ({ locale, setLocale, enabledLocales }), [locale, setLocale, enabledLocales]);

  // Cloudscape's provider reads `<html lang>` when `locale` is omitted; it is passed
  // explicitly so the two can never disagree.
  return (
    <LocaleContext.Provider value={value}>
      <IntlProvider locale={locale} defaultLocale={DEFAULT_LOCALE} messages={messages} onError={onIntlError}>
        <I18nProvider locale={locale} messages={cloudscapeMessages}>
          {children}
        </I18nProvider>
      </IntlProvider>
    </LocaleContext.Provider>
  );
};

export const useLocale = (): LocaleContextValue => {
  const ctx = useContext(LocaleContext);
  if (!ctx) throw new Error('useLocale must be used within LocaleProvider');
  return ctx;
};
