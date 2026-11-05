// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Vite plugin: compile the UI message catalogs at build time and expose them as virtual modules.
 *
 * Registered in `vite.config.js`, so `vite build`, the dev server and Vitest (which loads the
 * same config) all see the same two modules:
 *
 *   `virtual:i18n-catalogs`
 *       export const availableLocales: string[]   // every `<locale>.json` in `catalogDir`, sorted
 *       export const loaders: Record<locale, () => Promise<{ default: Record<id, MessageFormatElement[]> }>>
 *     `availableLocales` is file presence and nothing else (`en` is listed because `en.json`
 *     exists), and `locales.ts` derives the enabled-locale allow-list from it, so a locale
 *     enables itself when its catalog lands. `loaders` holds one entry per locale other than
 *     `en`, each a static `import()` of the module below, so every catalog is its own async
 *     chunk that only a browser running that locale ever fetches.
 *
 *   `virtual:i18n-catalog/<locale>`
 *       export default Record<id, MessageFormatElement[]>
 *     `<locale>.json` compiled through `compileCatalog`: every message parsed to the AST
 *     react-intl consumes, after the per-message parity check against `en.json`. A problem
 *     (malformed ICU, argument or plural/select mismatch, an entry of the wrong shape, an id
 *     `en.json` no longer has) throws here, so the build fails naming the id and the reason;
 *     nothing is dropped and nothing degraded ships. `en` has no compiled module: the English
 *     text stays inline in each message's `defaultMessage`.
 *
 * Options: `catalogDir` (default `src/i18n`, resolved against Vite's `root`).
 *
 * Extension point (decision D22 of #1770): a build step may write further `<locale>.json`
 * files into `catalogDir` before `vite build` runs, for example the pseudo-locale on a
 * flagged build or a machine-produced translation, and they are picked up through exactly
 * this path: listed in `availableLocales`, compiled and checked message by message, failing
 * the build on any problem. Only these release-built UI catalogs are ever translation
 * candidates. User-authored configuration content, document data and server responses are
 * never translated and never pass through here; no catalog is fetched at runtime from any URL.
 */
import { existsSync, readdirSync } from 'node:fs';
import { isAbsolute, join, resolve } from 'node:path';
import { compileCatalog, readCatalog } from './catalog.mjs';

export const CATALOGS_MODULE_ID = 'virtual:i18n-catalogs';
export const CATALOG_MODULE_PREFIX = 'virtual:i18n-catalog/';

/** The locale whose catalog is the source, kept inline and never compiled. */
const SOURCE_LOCALE = 'en';

/** Rollup convention: a resolved virtual id starts with `\0` so no other plugin processes it. */
const RESOLVED_CATALOGS_ID = `\0${CATALOGS_MODULE_ID}`;
const RESOLVED_CATALOG_PREFIX = `\0${CATALOG_MODULE_PREFIX}`;

/**
 * A catalog file is `<language>[-<Script>][-<REGION>].json`: `en.json`, `fr-CA.json`,
 * `en-XA.json`, `zh-Hant-TW.json`. Anything else in the directory is not a catalog.
 */
const CATALOG_FILE = /^([a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-(?:[A-Z]{2}|\d{3}))?)\.json$/;

/**
 * The locales with a catalog file in `dir`, sorted. A missing directory is an empty list.
 *
 * @param {string} dir
 * @returns {string[]}
 */
export const listCatalogLocales = (dir) => {
  if (!existsSync(dir)) return [];
  return readdirSync(dir)
    .map((name) => CATALOG_FILE.exec(name))
    .filter((match) => match !== null)
    .map((match) => match[1])
    .sort();
};

/**
 * The source of `virtual:i18n-catalogs` for the given locales.
 *
 * @param {string[]} locales
 * @returns {string}
 */
const catalogsModule = (locales) => {
  const loaders = locales
    .filter((locale) => locale !== SOURCE_LOCALE)
    .map((locale) => `  ${JSON.stringify(locale)}: () => import(${JSON.stringify(`${CATALOG_MODULE_PREFIX}${locale}`)}),`);
  return [`export const availableLocales = ${JSON.stringify(locales)};`, 'export const loaders = Object.freeze({', ...loaders, '});', ''].join(
    '\n',
  );
};

/**
 * Tell Vite/Rollup a file decides this module's content, when a hook context is available
 * (the hooks are also called bare from the unit tests). Files only: Vite's dev-time import
 * analysis resolves every watched path as a module, and a directory is not one.
 *
 * @param {unknown} context
 * @param {string} path
 */
const watch = (context, path) => {
  if (context && typeof (/** @type {{ addWatchFile?: unknown }} */ (context).addWatchFile) === 'function') {
    /** @type {{ addWatchFile: (path: string) => void }} */ (context).addWatchFile(path);
  }
};

/**
 * @param {{ catalogDir?: string }} [options]
 * @returns {import('vite').Plugin}
 */
export default function i18nCatalogs({ catalogDir = 'src/i18n' } = {}) {
  let dir = catalogDir;
  return {
    name: 'idp-i18n-catalogs',

    configResolved(config) {
      dir = isAbsolute(catalogDir) ? catalogDir : resolve(config.root, catalogDir);
    },

    resolveId(id) {
      if (id === CATALOGS_MODULE_ID) return RESOLVED_CATALOGS_ID;
      if (id.startsWith(CATALOG_MODULE_PREFIX)) return `\0${id}`;
      return null;
    },

    load(id) {
      // A catalog file added or removed while a dev server or Vitest watch runs needs a
      // restart to be listed: the directory itself cannot be watched (see `watch`).
      if (id === RESOLVED_CATALOGS_ID) return catalogsModule(listCatalogLocales(dir));
      if (!id.startsWith(RESOLVED_CATALOG_PREFIX)) return null;

      const locale = id.slice(RESOLVED_CATALOG_PREFIX.length);
      if (locale === SOURCE_LOCALE) {
        throw new Error(`[i18n] ${CATALOG_MODULE_PREFIX}${locale} does not exist: English stays inline and en.json is never compiled`);
      }
      if (!CATALOG_FILE.test(`${locale}.json`)) {
        throw new Error(`[i18n] ${JSON.stringify(locale)} is not a locale a catalog file can be named for`);
      }
      const enPath = join(dir, `${SOURCE_LOCALE}.json`);
      const localePath = join(dir, `${locale}.json`);
      if (!existsSync(localePath)) throw new Error(`[i18n] no catalog ${localePath} for locale ${locale}`);
      watch(this, enPath);
      watch(this, localePath);

      const { messages, problems } = compileCatalog(readCatalog(enPath), readCatalog(localePath), locale);
      if (problems.length > 0) {
        throw new Error(`[i18n] ${locale}.json has ${problems.length} problem(s), none of which may ship:\n  - ${problems.join('\n  - ')}`);
      }
      return `export default ${JSON.stringify(messages)};\n`;
    },
  };
}
