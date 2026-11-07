// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The UI message catalogs and how one compiles.
 *
 * Catalogs live at `src/ui/src/i18n/<locale>.json`:
 *
 *   - `en.json` is the source of truth, written by `npm run i18n:extract` (`formatjs extract`
 *     over the `defineMessages` / `<FormattedMessage>` calls in `src/`) and committed:
 *       { "<id>": { "defaultMessage": "<English ICU>", "description": "<note for translators>" } }
 *     `make check-ui-i18n-catalog` fails when it is stale.
 *
 *   - every other locale is a translation of it:
 *       { "<id>": { "message": "<translated ICU>", "sourceHash": "<sourceHash(defaultMessage)>" } }
 *     `sourceHash` is the first 16 hex characters of the SHA-256 of the English `defaultMessage`
 *     the entry was translated from (UTF-8, NFC-normalised). It is not checked here: the
 *     coverage report compares it with the current English to list translations whose source
 *     changed. The build strips it.
 *
 * `compileCatalog` turns a translation catalog into the `{ id: MessageFormatElement[] }` table
 * react-intl consumes, judging every entry with `validateMessage`. Nothing is dropped silently:
 * a malformed or mismatched message, an entry of the wrong shape, or an id that `en.json` no
 * longer has (a dead translation) is a problem the caller treats as a hard error. A message
 * `en.json` has and the translation lacks is fine: react-intl falls back to the inline English.
 */
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { parseMessage, validateMessage } from './validate-message.mjs';

/** Length of the `sourceHash` recorded in a translation entry. */
export const SOURCE_HASH_LENGTH = 16;

const SOURCE_HASH_SHAPE = new RegExp(`^[0-9a-f]{${SOURCE_HASH_LENGTH}}$`);

/**
 * The hash a translation entry records for the English it was translated from.
 *
 * @param {string} text the English `defaultMessage`
 * @returns {string} the first 16 hex characters of SHA-256 over the NFC-normalised UTF-8 text
 */
export const sourceHash = (text) => createHash('sha256').update(text.normalize('NFC'), 'utf8').digest('hex').slice(0, SOURCE_HASH_LENGTH);

/** @param {unknown} value */
const isPlainObject = (value) => typeof value === 'object' && value !== null && !Array.isArray(value);

/**
 * Read a catalog file. Throws on unreadable or malformed JSON, or when the file is not a JSON
 * object; the caller decides how to report that.
 *
 * @param {string} path
 * @returns {Record<string, unknown>}
 */
export const readCatalog = (path) => {
  const parsed = JSON.parse(readFileSync(path, 'utf8'));
  if (!isPlainObject(parsed)) throw new TypeError(`${path}: a catalog is a JSON object keyed by message id`);
  return parsed;
};

/**
 * Compile one translation catalog against the English source catalog.
 *
 * @param {Record<string, unknown>} enJson the parsed `en.json`
 * @param {Record<string, unknown>} localeJson the parsed `<locale>.json`
 * @param {string} locale the locale being compiled, for the problem texts
 * @returns {{ messages: Record<string, import('@formatjs/icu-messageformat-parser').MessageFormatElement[]>, problems: string[] }}
 *   `messages` holds every entry that passed; `problems` is empty when all of them did
 */
export const compileCatalog = (enJson, localeJson, locale) => {
  /** @type {Record<string, import('@formatjs/icu-messageformat-parser').MessageFormatElement[]>} */
  const messages = {};
  /** @type {string[]} */
  const problems = [];
  if (!isPlainObject(enJson)) return { messages, problems: ['en.json is not a JSON object keyed by message id'] };
  if (!isPlainObject(localeJson)) return { messages, problems: [`${locale}.json is not a JSON object keyed by message id`] };

  for (const id of Object.keys(localeJson).sort()) {
    const entry = localeJson[id];
    const source = enJson[id];
    if (source === undefined) {
      problems.push(`${locale}.json: "${id}" is not in en.json (a dead translation: delete it, or re-extract if the message exists)`);
      continue;
    }
    if (!isPlainObject(source) || typeof source.defaultMessage !== 'string') {
      problems.push(`en.json: "${id}" has no string defaultMessage`);
      continue;
    }
    if (!isPlainObject(entry) || typeof entry.message !== 'string') {
      problems.push(`${locale}.json: "${id}" must be { "message": "<ICU>", "sourceHash": "<16 hex>" }`);
      continue;
    }
    if (typeof entry.sourceHash !== 'string' || !SOURCE_HASH_SHAPE.test(entry.sourceHash)) {
      problems.push(`${locale}.json: "${id}" needs a sourceHash of ${SOURCE_HASH_LENGTH} hex characters (sourceHash of the English defaultMessage)`);
      continue;
    }
    const verdict = validateMessage(id, source.defaultMessage, entry.message);
    if (!verdict.ok) {
      problems.push(...verdict.errors.map((error) => `${locale}.json: ${error}`));
      continue;
    }
    messages[id] = parseMessage(entry.message);
  }
  return { messages, problems };
};
