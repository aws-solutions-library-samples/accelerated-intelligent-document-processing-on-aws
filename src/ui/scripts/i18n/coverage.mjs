#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Translation coverage of every locale catalog against `en.json`: the advisory report.
 *
 *   node scripts/i18n/coverage.mjs [<catalog-dir>] [--json] [--locale <tag>]
 *
 * For every `<locale>.json` in the catalog directory other than `en.json` (file presence,
 * the rule the build follows; the `en-XA` pseudo-locale is generated from `en.json` in memory
 * on a flagged build, is never a file, and so is never reported on) the English ids and the
 * catalog's entries are sorted into:
 *
 *   - `missing`: ids `en.json` has and the catalog does not. react-intl shows the English.
 *   - `stale`: entries whose recorded `sourceHash` is not `sourceHash(defaultMessage)` of the
 *     English as it is now. Ids are stable, so an edit to the English leaves the old translation
 *     in place, compiling and shown; only the hash tells that it now says something else.
 *   - `invalid`: entries `compileCatalog` rejects (malformed ICU, argument or plural/select
 *     parity, entry shape), with the build's own wording in `problems`.
 *   - `dead`: entries whose id `en.json` no longer has.
 *   - `translated`: entries that are present, valid and current.
 *
 * Every English id lands in exactly one of missing, stale, invalid or translated (an entry that
 * is both stale and invalid is listed as invalid, which is what the build rejects it for), so
 * `translated + missing + stale + invalid = sourceKeys` and `coveragePercent` is
 * `translated / sourceKeys`, to one decimal.
 *
 * This is the advisory counterpart of the build-time parity check (decision D17 of #1770).
 * `make ui-i18n-coverage` runs it through `scripts/ui_i18n_coverage.py`; it is not part of
 * `lint`, `fastlint` or `lint-cicd`, and it exits 0 whatever it finds. Invalid entries already
 * fail `vite build` (and `make check-ui-i18n-catalog` before it), so here they are listed, not
 * enforced; a missing or stale translation is an English fallback on screen, which this report
 * makes visible without turning every English-only string into a red gate. The percentage is
 * what each release's notes quote once `fr-CA` is enabled.
 *
 * Exit codes: 0 the report was produced, whatever it says; 2 usage, or a tool error (an
 * unreadable catalog directory or file, malformed JSON, a malformed `en.json`, a `--locale` with
 * no catalog file).
 */
import { existsSync, realpathSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { compileCatalog, readCatalog, sourceHash } from './catalog.mjs';
import { PSEUDO_LOCALE, listCatalogLocales } from './vite-plugin-catalogs.mjs';

const DEFAULT_CATALOG_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', 'src', 'i18n');

/** The locale whose catalog is the source: extracted, never translated, so never measured. */
const SOURCE_LOCALE = 'en';

/** @param {unknown} value */
const isPlainObject = (value) => typeof value === 'object' && value !== null && !Array.isArray(value);

/**
 * @typedef {object} LocaleCoverage
 * @property {number} translated entries that are present, valid and current
 * @property {string[]} missing ids `en.json` has and the catalog does not, sorted
 * @property {string[]} stale ids whose recorded `sourceHash` is not the current English's, sorted
 * @property {string[]} invalid ids the build would reject, sorted
 * @property {string[]} problems why, one line per problem, in the build's wording
 * @property {string[]} dead ids `en.json` does not have, sorted
 * @property {number} coveragePercent `translated / sourceKeys`, to one decimal; 0 when there are no source keys
 */

/**
 * @typedef {object} CoverageReport
 * @property {number} sourceKeys the number of ids in `en.json`
 * @property {Record<string, LocaleCoverage>} locales one entry per translation catalog, keyed by locale
 */

/**
 * The English `defaultMessage` per id, sorted by id. `en.json` is written by `formatjs extract`,
 * so an entry without a string `defaultMessage` is a tool problem, not a coverage finding.
 *
 * @param {Record<string, unknown>} enJson
 * @returns {Map<string, string>}
 */
const sourceMessages = (enJson) => {
  if (!isPlainObject(enJson)) throw new TypeError('en.json is not a JSON object keyed by message id');
  const messages = new Map();
  for (const id of Object.keys(enJson).sort()) {
    const source = enJson[id];
    if (!isPlainObject(source) || typeof source.defaultMessage !== 'string') {
      throw new TypeError(`en.json: "${id}" has no string defaultMessage`);
    }
    messages.set(id, source.defaultMessage);
  }
  return messages;
};

/**
 * Coverage of one translation catalog against the English source catalog.
 *
 * The verdict on each entry is `compileCatalog`'s, not a second set of rules: an entry it did
 * not compile is invalid, and `problems` is its wording. Dead ids are kept out of that run so
 * `problems` speaks only about ids `en.json` has.
 *
 * @param {Record<string, unknown>} enJson the parsed `en.json`
 * @param {Record<string, unknown>} localeJson the parsed `<locale>.json`
 * @param {string} locale the locale being measured, for the problem texts
 * @returns {LocaleCoverage}
 */
export const localeCoverage = (enJson, localeJson, locale) => {
  const source = sourceMessages(enJson);
  if (!isPlainObject(localeJson)) throw new TypeError(`${locale}.json is not a JSON object keyed by message id`);

  const live = Object.fromEntries(Object.entries(localeJson).filter(([id]) => source.has(id)));
  const { messages, problems } = compileCatalog(enJson, live, locale);

  /** @type {string[]} */
  const missing = [];
  /** @type {string[]} */
  const stale = [];
  /** @type {string[]} */
  const invalid = [];
  let translated = 0;
  for (const [id, defaultMessage] of source) {
    if (!Object.hasOwn(live, id)) {
      missing.push(id);
    } else if (!Object.hasOwn(messages, id)) {
      invalid.push(id);
    } else if (/** @type {{ sourceHash: string }} */ (live[id]).sourceHash !== sourceHash(defaultMessage)) {
      // Compiled, so the entry's shape (a 16-hex sourceHash included) has been checked.
      stale.push(id);
    } else {
      translated += 1;
    }
  }
  const dead = Object.keys(localeJson)
    .filter((id) => !source.has(id))
    .sort();
  const coveragePercent = source.size === 0 ? 0 : Math.round((translated / source.size) * 1000) / 10;
  return { translated, missing, stale, invalid, problems, dead, coveragePercent };
};

/**
 * The report over a catalog directory: `en.json` against every other `<locale>.json` in it, or
 * against the one named by `locale`.
 *
 * Throws when the directory has no `en.json`, a catalog is unreadable or malformed, or the
 * requested locale has no catalog file: those are tool errors, and the CLI exits 2 on them.
 *
 * @param {string} catalogDir
 * @param {{ locale?: string | null }} [options]
 * @returns {CoverageReport}
 */
export const coverageReport = (catalogDir, { locale = null } = {}) => {
  const enPath = join(catalogDir, `${SOURCE_LOCALE}.json`);
  if (!existsSync(enPath)) throw new Error(`${enPath}: no such catalog (en.json is the source every locale is measured against)`);
  const enJson = readCatalog(enPath);
  const sourceKeys = sourceMessages(enJson).size;

  let locales = listCatalogLocales(catalogDir).filter((tag) => tag !== SOURCE_LOCALE);
  if (locale !== null) {
    if (locale === SOURCE_LOCALE)
      throw new Error('en.json is the source catalog: it is extracted, never translated, so it has no coverage');
    if (!locales.includes(locale)) {
      const hint =
        locale === PSEUDO_LOCALE
          ? ' (the pseudo-locale is generated from en.json at build time, so there is no catalog file to measure)'
          : '';
      throw new Error(`${join(catalogDir, `${locale}.json`)}: no such catalog${hint}`);
    }
    locales = [locale];
  }

  /** @type {Record<string, LocaleCoverage>} */
  const perLocale = {};
  for (const tag of locales) {
    perLocale[tag] = localeCoverage(enJson, readCatalog(join(catalogDir, `${tag}.json`)), tag);
  }
  return { sourceKeys, locales: perLocale };
};

/**
 * The report as text: one block per locale, the totals first, then the ids in each list.
 *
 * @param {CoverageReport} report
 * @param {string} catalogDir named when there is nothing to report on, so the reader knows where was looked
 * @returns {string}
 */
export const formatReport = (report, catalogDir) => {
  const keys = `${report.sourceKeys} source key${report.sourceKeys === 1 ? '' : 's'} in en.json`;
  const tags = Object.keys(report.locales);
  if (tags.length === 0) return `${keys}, no translation catalogs (only en.json is in ${catalogDir})\n`;

  const lines = [keys];
  const section = (
    /** @type {string} */ name,
    /** @type {string} */ meaning,
    /** @type {string[]} */ ids,
    /** @type {string[]} */ details = [],
  ) => {
    lines.push(`  ${name} (${ids.length}): ${meaning}`);
    for (const id of ids) lines.push(`    ${id}`);
    for (const detail of details) lines.push(`      ${detail}`);
  };
  for (const tag of tags) {
    const c = report.locales[tag];
    lines.push('');
    lines.push(`${tag}.json: ${c.translated} of ${report.sourceKeys} translated (${c.coveragePercent}%)`);
    section('missing', 'no entry, so the English is shown', c.missing);
    section('stale', 'the English changed since the translation was made (sourceHash differs)', c.stale);
    section('invalid', 'the build rejects these, with the wording below', c.invalid, c.problems);
    section('dead', 'not in en.json', c.dead);
  }
  lines.push('');
  return lines.join('\n');
};

const usage = () => {
  console.error('usage: node scripts/i18n/coverage.mjs [<catalog-dir>] [--json] [--locale <tag>]');
  return 2;
};

/**
 * @param {string[]} argv
 * @returns {number}
 */
const main = (argv) => {
  let catalogDir = null;
  let json = false;
  let locale = null;
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--json') {
      json = true;
    } else if (argv[i] === '--locale') {
      if (i + 1 >= argv.length) return usage();
      locale = argv[i + 1];
      i += 1;
    } else if (catalogDir === null && !argv[i].startsWith('-')) {
      catalogDir = resolve(argv[i]);
    } else {
      return usage();
    }
  }
  const dir = catalogDir ?? DEFAULT_CATALOG_DIR;

  let report;
  try {
    report = coverageReport(dir, { locale });
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    return 2;
  }
  process.stdout.write(json ? `${JSON.stringify(report, null, 2)}\n` : formatReport(report, dir));
  return 0;
};

/**
 * Whether this file is the script Node was started with. The Vitest suite imports the functions
 * above, and must not run the CLI against its own argv when it does.
 */
const isEntryPoint = () => {
  const entry = process.argv[1];
  if (!entry) return false;
  try {
    return realpathSync(entry) === fileURLToPath(import.meta.url);
  } catch {
    return false;
  }
};

if (isEntryPoint()) process.exitCode = main(process.argv.slice(2));
