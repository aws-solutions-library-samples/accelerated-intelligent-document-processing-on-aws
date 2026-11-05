// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import type { MessageFormatElement } from '@formatjs/icu-messageformat-parser';
// A Vite virtual module served by scripts/i18n/vite-plugin-catalogs.mjs (typed in
// ./virtual-catalogs.d.ts).
import { loaders } from 'virtual:i18n-catalogs';
import { DEFAULT_LOCALE, type Locale } from './locales';

/** A compiled catalog: message id → the ICU AST react-intl formats. */
export type CatalogMessages = Record<string, MessageFormatElement[]>;

/**
 * The catalog of `en`, and of any locale whose catalog is absent or still loading: every
 * lookup misses and react-intl renders the inline English `defaultMessage`. One frozen
 * object, so `IntlProvider` sees the same `messages` identity across renders.
 */
export const EMPTY_CATALOG: CatalogMessages = Object.freeze({});

/**
 * The compiled catalog for `locale`, loaded lazily so each locale's messages ship as their own
 * chunk, fetched only by a browser running that locale.
 *
 * `en` resolves to `EMPTY_CATALOG` at once and imports nothing: English is inline. A locale
 * without a loader (no `<locale>.json` in the build) also resolves to `EMPTY_CATALOG`, which
 * is English by fallback. A loader that rejects (the chunk unreachable, say) rejects here;
 * the provider logs it and keeps English.
 *
 * The loaders come from the build, one per catalog file the Vite plugin found, so a catalog a
 * build step writes (decision D22 of #1770) is loaded through this same function with no code
 * change.
 */
export const loadCatalog = async (locale: Locale): Promise<CatalogMessages> => {
  if (locale === DEFAULT_LOCALE) return EMPTY_CATALOG;
  if (!Object.hasOwn(loaders, locale)) return EMPTY_CATALOG;
  const compiled = await loaders[locale]();
  return compiled.default;
};
