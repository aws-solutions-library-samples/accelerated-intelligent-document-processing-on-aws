#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Compile one translation catalog against `en.json` and report every problem, as the build would.
 *
 *   node scripts/i18n/check-catalog.mjs <locale> [--catalog-dir <dir>]
 *
 * `make check-ui-i18n-catalog` (`scripts/check_ui_i18n_catalog.py`) runs this for every
 * `<locale>.json` other than `en.json`, so a translation that would fail `vite build` fails
 * `make lint` first, with the same wording. Check-only: it writes nothing.
 *
 * Exit codes: 0 every message compiles; 1 at least one problem (each printed on its own line);
 * 2 usage or an unreadable catalog.
 */
import { existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { compileCatalog, readCatalog } from './catalog.mjs';

const DEFAULT_CATALOG_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', 'src', 'i18n');

const usage = () => {
  console.error('usage: node scripts/i18n/check-catalog.mjs <locale> [--catalog-dir <dir>]');
  return 2;
};

/**
 * @param {string[]} argv
 * @returns {number}
 */
const main = (argv) => {
  let locale = null;
  let catalogDir = DEFAULT_CATALOG_DIR;
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--catalog-dir') {
      if (i + 1 >= argv.length) return usage();
      catalogDir = resolve(argv[i + 1]);
      i += 1;
    } else if (locale === null && !argv[i].startsWith('-')) {
      locale = argv[i];
    } else {
      return usage();
    }
  }
  if (locale === null) return usage();
  if (locale === 'en') {
    console.error('en.json is the source catalog: it is extracted, never compiled, so there is nothing to check it against.');
    return 2;
  }

  const enPath = join(catalogDir, 'en.json');
  const localePath = join(catalogDir, `${locale}.json`);
  for (const path of [enPath, localePath]) {
    if (!existsSync(path)) {
      console.error(`${path}: no such catalog`);
      return 2;
    }
  }
  let enJson;
  let localeJson;
  try {
    enJson = readCatalog(enPath);
    localeJson = readCatalog(localePath);
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    return 2;
  }

  const { messages, problems } = compileCatalog(enJson, localeJson, locale);
  if (problems.length > 0) {
    for (const problem of problems) console.error(problem);
    console.error(`${locale}.json: ${problems.length} problem(s); a build with this catalog would fail`);
    return 1;
  }
  console.log(`${locale}.json: ${Object.keys(messages).length} message(s) compile against en.json`);
  return 0;
};

process.exitCode = main(process.argv.slice(2));
