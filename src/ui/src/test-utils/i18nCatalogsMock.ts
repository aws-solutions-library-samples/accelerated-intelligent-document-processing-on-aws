// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * A stand-in for the build's `virtual:i18n-catalogs` module, for tests that render a locale
 * from a fixture catalog. Wired in with one line at the top of the test file:
 *
 *   vi.mock('virtual:i18n-catalogs', () => import('../../test-utils/i18nCatalogsMock'));
 *
 * Vitest hoists that call above the file's imports, so every module in the test's graph
 * (`locales.ts`, `loadCatalog.ts` and through them `LocaleProvider`) gets this module instead
 * of the one `scripts/i18n/vite-plugin-catalogs.mjs` serves from `src/i18n/*.json`. Each loader
 * resolves to whatever `setFixtureCatalog` registered for its locale, which is what
 * `renderWithIntl({ messages })` calls, through the same `loadCatalog` path the browser takes.
 * Nothing under `src/i18n` has a test-only branch.
 *
 * `availableLocales` lists `en` only, which is what today's build serves, so `ENABLED_LOCALES`
 * (derived from it) is unchanged under the mock: a test widens the allow-list explicitly,
 * through `renderWithIntl`'s `enabledLocales`, the way the app will when a catalog lands.
 *
 * Imports nothing from `src/i18n` at run time (types only): `locales.ts` imports the module
 * this one replaces, so a value import here would be circular.
 */
import { parse, type MessageFormatElement } from '@formatjs/icu-messageformat-parser';

/** A fixture catalog: message id → ICU source text, or an already parsed message. */
export type FixtureMessages = Readonly<Record<string, string | MessageFormatElement[]>>;

type CompiledMessages = Record<string, MessageFormatElement[]>;

const fixtures = new Map<string, CompiledMessages>();

/**
 * Parse the fixture's strings the way the build parses a catalog (`parseMessage` in
 * scripts/i18n/validate-message.mjs is `parse` with its defaults). A malformed ICU string
 * throws here, in the test that wrote it.
 */
const compileFixture = (messages: FixtureMessages): CompiledMessages =>
  Object.fromEntries(Object.entries(messages).map(([id, message]) => [id, typeof message === 'string' ? parse(message) : message]));

/** Register the catalog the `locale` loader resolves to from now on. */
export const setFixtureCatalog = (locale: string, messages: FixtureMessages): void => {
  fixtures.set(locale, compileFixture(messages));
};

/** Forget every fixture: each loader resolves to an empty catalog again (English by fallback). */
export const clearFixtureCatalogs = (): void => {
  fixtures.clear();
};

const fixtureLoader = (locale: string) => async (): Promise<{ default: CompiledMessages }> => ({ default: fixtures.get(locale) ?? {} });

export const availableLocales: readonly string[] = ['en'];

/**
 * One loader per member of `KNOWN_LOCALES` other than `en`, frozen like the build's. The
 * helper's test checks the two lists agree, so a locale added to `locales.ts` is added here.
 */
export const loaders = Object.freeze({
  'fr-CA': fixtureLoader('fr-CA'),
  'en-XA': fixtureLoader('en-XA'),
});
