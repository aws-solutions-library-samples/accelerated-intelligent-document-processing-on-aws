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
 * Options: `catalogDir` (default `src/i18n`, resolved against Vite's `root`); `pseudoLocale`
 * (default: `detectPseudoLocale`, below).
 *
 * The pseudo-locale `en-XA` (decision D21 of #1770). On a flagged build, and only then, the
 * plugin also serves `virtual:i18n-catalog/en-XA`: `en.json` run through the installed FormatJS
 * CLI (`formatjs compile --ast --pseudo-locale en-XA`), which turns every English message into
 * accented, bracket-padded text (`Language` → `[Ŀȧȧƞɠŭŭȧȧɠḗḗ]`) with every argument, plural,
 * select and tag intact. A running stack built this way shows which text bypasses the catalog
 * (it stays plain English) and which layouts clip (the text runs longer, as French does). It
 * is derived from English at build time, in memory: no `en-XA.json` is ever written into
 * `catalogDir`, so the catalog gate and the coverage report, which read `<locale>.json` files,
 * never see it, and it has no `sourceHash` because there is no source it could drift from.
 * Every produced message is still judged against `en.json` with the per-message parity check
 * (`validateMessageAst`), and a CLI failure or a parity failure fails the build naming the
 * cause; a flagged build never falls back to English quietly. `en-XA` is then listed in
 * `availableLocales` with a loader like any translation, which is all `locales.ts` needs to
 * enable it and the switcher needs to offer it. An unflagged build emits nothing of it.
 *
 * How a build is flagged, and why there are two ways. The deployed UI is built by CodeBuild
 * (`template.yaml`, `UICodeBuildProject`) from a source zip of `src/ui` that `publish.py`
 * creates, excluding `node_modules`, `build` and every `.env*` file. The environment of that
 * build is template-authored and `vite.config.js` reads none of it, so an environment
 * variable cannot reach a deployed build without a template change. The marker file
 * `src/ui/.pseudo-locale` (empty, gitignored) can: it is not an `.env*` name, so it travels in
 * the zip, and it changes the zip's content hash, so the stack rebuilds its UI. For a local
 * build `IDP_UI_PSEUDO_LOCALE=1` does the same without touching the tree. Production builds
 * have neither.
 *
 * Extension point (decision D22 of #1770): a build step may write further `<locale>.json`
 * files into `catalogDir` before `vite build` runs, for example a machine-produced translation,
 * and they are picked up through exactly this path: listed in `availableLocales`, compiled and
 * checked message by message, failing the build on any problem. The pseudo-locale is the first
 * build-produced catalog and goes the same way, only already parsed. Only these release-built
 * UI catalogs are ever translation candidates. User-authored configuration content, document
 * data and server responses are never translated and never pass through here; no catalog is
 * fetched at runtime from any URL.
 */
import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { compileCatalog, readCatalog } from './catalog.mjs';
import { validateMessageAst } from './validate-message.mjs';

export const CATALOGS_MODULE_ID = 'virtual:i18n-catalogs';
export const CATALOG_MODULE_PREFIX = 'virtual:i18n-catalog/';

/** The pseudo-locale FormatJS generates: accented letters, doubled vowels, `[ ]` padding. */
export const PSEUDO_LOCALE = 'en-XA';
/** Set to `1` to flag a local build. */
export const PSEUDO_LOCALE_ENV = 'IDP_UI_PSEUDO_LOCALE';
/** The empty marker file, relative to `src/ui`, that flags a build from a source zip. */
export const PSEUDO_LOCALE_MARKER = '.pseudo-locale';

/** The locale whose catalog is the source, kept inline and never compiled. */
const SOURCE_LOCALE = 'en';

/** `src/ui`, from this file's own location (`src/ui/scripts/i18n/`), whatever the cwd. */
const UI_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

/**
 * Whether this build is flagged for the pseudo-locale: the environment variable is `1`, or
 * the marker file exists under `uiRoot`. Pure, so it is tested with a temporary directory.
 *
 * @param {{ env: Record<string, string | undefined>, uiRoot: string }} input
 * @returns {boolean}
 */
export const detectPseudoLocale = ({ env, uiRoot }) => env[PSEUDO_LOCALE_ENV] === '1' || existsSync(join(uiRoot, PSEUDO_LOCALE_MARKER));

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
 * `en.json` turned into the pseudo-locale by the installed FormatJS CLI, as the parsed table
 * react-intl consumes, then judged message by message against the English it came from.
 *
 * The CLI is run by path out of `src/ui/node_modules` with the Node that runs this build (no
 * PATH lookup, no shell, no `npx`, nothing fetched), writing into a temporary file that is
 * removed before returning, so nothing lands in `catalogDir`. Every id of `en.json` must come
 * back, nothing else may, and each message must keep its arguments, plural/select kinds and
 * tags: the pseudo-locale exists to reveal gaps, so a gap in it is an error, never English.
 *
 * @param {string} enPath the English source catalog
 * @param {string} formatjsBin the CLI, `<uiRoot>/node_modules/.bin/formatjs`
 * @returns {Record<string, import('@formatjs/icu-messageformat-parser').MessageFormatElement[]>}
 */
const compilePseudoLocale = (enPath, formatjsBin) => {
  const what = `the ${PSEUDO_LOCALE} pseudo-locale`;
  if (!existsSync(formatjsBin)) {
    throw new Error(`[i18n] cannot build ${what}: ${formatjsBin} is missing (run npm ci in src/ui)`);
  }
  const scratch = mkdtempSync(join(tmpdir(), 'idp-i18n-pseudo-'));
  /** @type {Record<string, unknown>} */
  let produced;
  try {
    const outFile = join(scratch, `${PSEUDO_LOCALE}.json`);
    const args = [formatjsBin, 'compile', enPath, '--ast', '--pseudo-locale', PSEUDO_LOCALE, '--out-file', outFile];
    const run = spawnSync(process.execPath, args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
    if (run.error) {
      throw new Error(`[i18n] cannot build ${what}: formatjs compile could not run: ${run.error.message}`);
    }
    if (run.status !== 0) {
      const output = `${run.stderr ?? ''}${run.stdout ?? ''}`.trim();
      throw new Error(`[i18n] cannot build ${what}: formatjs compile exited with ${run.status}\n${output}`);
    }
    produced = readCatalog(outFile);
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }

  const en = readCatalog(enPath);
  /** @type {Record<string, import('@formatjs/icu-messageformat-parser').MessageFormatElement[]>} */
  const messages = {};
  /** @type {string[]} */
  const problems = [];
  for (const id of Object.keys(en).sort()) {
    const source = en[id];
    if (
      typeof source !== 'object' ||
      source === null ||
      typeof (/** @type {{ defaultMessage?: unknown }} */ (source).defaultMessage) !== 'string'
    ) {
      problems.push(`en.json: "${id}" has no string defaultMessage`);
      continue;
    }
    if (!Object.hasOwn(produced, id)) {
      problems.push(`"${id}" is in en.json but formatjs compile produced no ${PSEUDO_LOCALE} message for it`);
      continue;
    }
    const ast = /** @type {import('@formatjs/icu-messageformat-parser').MessageFormatElement[]} */ (produced[id]);
    const verdict = validateMessageAst(id, /** @type {{ defaultMessage: string }} */ (source).defaultMessage, ast);
    if (!verdict.ok) {
      problems.push(...verdict.errors);
      continue;
    }
    messages[id] = ast;
  }
  for (const id of Object.keys(produced).sort()) {
    if (!Object.hasOwn(en, id)) problems.push(`formatjs compile produced "${id}", which is not in en.json`);
  }
  if (problems.length > 0) {
    throw new Error(`[i18n] ${what} has ${problems.length} problem(s), none of which may ship:\n  - ${problems.join('\n  - ')}`);
  }
  return messages;
};

/**
 * @param {{ catalogDir?: string, pseudoLocale?: boolean }} [options]
 *   `pseudoLocale`: build `en-XA` from `en.json` and list it. Default: `detectPseudoLocale`
 *   over `process.env` and `src/ui`, so the flag is read once, when the build starts.
 * @returns {import('vite').Plugin}
 */
export default function i18nCatalogs({
  catalogDir = 'src/i18n',
  pseudoLocale = detectPseudoLocale({ env: process.env, uiRoot: UI_ROOT }),
} = {}) {
  let dir = catalogDir;
  const formatjsBin = join(UI_ROOT, 'node_modules', '.bin', 'formatjs');

  /** The catalog files, plus the pseudo-locale on a flagged build; sorted, no duplicates. */
  const availableLocales = () => {
    const locales = listCatalogLocales(dir);
    if (pseudoLocale && !locales.includes(PSEUDO_LOCALE)) locales.push(PSEUDO_LOCALE);
    return locales.sort();
  };

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
      // restart to be listed, as does flagging or unflagging the pseudo-locale: the directory
      // and the marker cannot be watched (see `watch`), and the flag is read once.
      if (id === RESOLVED_CATALOGS_ID) return catalogsModule(availableLocales());
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

      if (locale === PSEUDO_LOCALE && pseudoLocale) {
        if (existsSync(localePath)) {
          throw new Error(
            `[i18n] ${localePath} exists while the build is flagged for the pseudo-locale, which is generated from en.json: delete the file or unflag the build`,
          );
        }
        watch(this, enPath);
        return `export default ${JSON.stringify(compilePseudoLocale(enPath, formatjsBin))};\n`;
      }

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
