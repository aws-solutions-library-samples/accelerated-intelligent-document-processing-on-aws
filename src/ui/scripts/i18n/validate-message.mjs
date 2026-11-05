// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Per-message parity check between an English source message and one translation of it.
 *
 * Pure (no I/O, no process state), so the same function judges a message wherever one
 * is compiled or reviewed: the Vite plugin (`vite-plugin-catalogs.mjs`) at build time,
 * the catalog gate (`check-catalog.mjs`) in `make check-ui-i18n-catalog`, the Vitest
 * suite, the catalog review tooling of a later PR, and any catalog a build step produces
 * (decision D22 of #1770). A message that fails here is never shipped: the caller treats
 * it as a hard error, so a malformed translation fails the build rather than a screen.
 *
 * What "parity" means here:
 *   - both messages parse as ICU MessageFormat, and every plural, selectordinal and
 *     select carries an `other` branch (the parser enforces that);
 *   - the translation uses exactly the source's argument names, no more, no fewer;
 *   - each shared argument keeps its kind: a plural stays a plural (cardinal or ordinal),
 *     a select stays a select, a plain/number/date/time argument stays a plain one;
 *   - rich-text tags (`<b>…</b>`, `<link>…</link>`) match by name.
 *
 * `#` inside a plural branch is a use of that plural's own variable, so `one {# file}` and
 * `one {{count} file}` have the same shape. Plural branch keys may differ between languages
 * (French `one` covers 0 and 1, Arabic has six categories), so only `other` is required.
 */
import { TYPE, parse } from '@formatjs/icu-messageformat-parser';

/**
 * Parse one ICU message into the AST react-intl consumes (`IntlProvider`'s `messages` accepts
 * values that are already parsed). Throws a `SyntaxError` on malformed ICU, including a plural
 * or select without an `other` branch (`MISSING_OTHER_CLAUSE`).
 *
 * @param {string} message
 * @returns {import('@formatjs/icu-messageformat-parser').MessageFormatElement[]}
 */
export const parseMessage = (message) => parse(message);

/**
 * The parser's `SyntaxError` as one readable line: the error kind, the position and the
 * offending text.
 *
 * @param {unknown} error
 * @returns {string}
 */
const describeParseError = (error) => {
  if (!(error instanceof Error)) return String(error);
  const location = /** @type {{ location?: { start?: { line: number, column: number } } }} */ (error).location;
  const where = location?.start ? ` at line ${location.start.line}, column ${location.start.column}` : '';
  return `${error.message}${where}`;
};

/**
 * The argument kind a translation must preserve. Number, date and time formats are "plain"
 * alongside a bare `{name}`: a translator may add or drop a format on a value, but turning
 * a plural into a plain argument (or the reverse) changes what the message can render.
 *
 * @param {import('@formatjs/icu-messageformat-parser').MessageFormatElement} element
 * @returns {'plain argument' | 'plural' | 'selectordinal' | 'select'}
 */
const kindOf = (element) => {
  if (element.type === TYPE.plural) return element.pluralType === 'ordinal' ? 'selectordinal' : 'plural';
  if (element.type === TYPE.select) return 'select';
  return 'plain argument';
};

/**
 * @typedef {object} MessageShape
 * @property {Map<string, string>} args argument name → kind (`plain argument`, `plural`, `selectordinal`, `select`)
 * @property {Set<string>} tags rich-text tag names
 */

/**
 * Record an argument. A variable may appear both as a plural and, inside one of its branches,
 * as a plain `{count}`; the structural kind is the one that matters, so it is never downgraded.
 *
 * @param {MessageShape} shape
 * @param {string} name
 * @param {string} kind
 */
const recordArgument = (shape, name, kind) => {
  const existing = shape.args.get(name);
  if (existing === undefined || existing === 'plain argument') shape.args.set(name, kind);
};

/**
 * Walk an AST and collect its arguments and tags.
 *
 * @param {import('@formatjs/icu-messageformat-parser').MessageFormatElement[]} elements
 * @param {MessageShape} shape
 * @returns {MessageShape}
 */
const collectShape = (elements, shape) => {
  for (const element of elements) {
    switch (element.type) {
      case TYPE.literal:
      case TYPE.pound:
        // The parser emits `#` only directly inside a plural branch, where it stands for that
        // plural's variable, which was recorded when the plural itself was visited.
        break;
      case TYPE.plural:
      case TYPE.select:
        recordArgument(shape, element.value, kindOf(element));
        for (const option of Object.values(element.options)) collectShape(option.value, shape);
        break;
      case TYPE.tag:
        shape.tags.add(element.value);
        collectShape(element.children, shape);
        break;
      default:
        // argument, number, date, time
        recordArgument(shape, element.value, kindOf(element));
    }
  }
  return shape;
};

/** @param {string} message */
const shapeOf = (message) => collectShape(parseMessage(message), { args: new Map(), tags: new Set() });

/** @param {Iterable<string>} names */
const list = (names) => Array.from(names).sort().join(', ');

/**
 * Judge one translated message against its English source.
 *
 * @param {string} id the message id, used in every error so a caller can print them as they are
 * @param {string} sourceMessage the English `defaultMessage` from `en.json`
 * @param {string} translatedMessage the candidate translation
 * @returns {{ ok: boolean, errors: string[] }} `ok` is true only when `errors` is empty
 */
export const validateMessage = (id, sourceMessage, translatedMessage) => {
  const errors = [];
  if (typeof sourceMessage !== 'string') {
    return { ok: false, errors: [`${id}: the English source is not a string`] };
  }
  if (typeof translatedMessage !== 'string') {
    return { ok: false, errors: [`${id}: the translation is not a string`] };
  }

  let source;
  try {
    source = shapeOf(sourceMessage);
  } catch (error) {
    return { ok: false, errors: [`${id}: the English source does not parse as ICU: ${describeParseError(error)}`] };
  }
  let translated;
  try {
    translated = shapeOf(translatedMessage);
  } catch (error) {
    return { ok: false, errors: [`${id}: the translation does not parse as ICU: ${describeParseError(error)}`] };
  }

  const missingArgs = [...source.args.keys()].filter((name) => !translated.args.has(name));
  const extraArgs = [...translated.args.keys()].filter((name) => !source.args.has(name));
  if (missingArgs.length > 0) errors.push(`${id}: the translation is missing argument(s) {${list(missingArgs)}}`);
  if (extraArgs.length > 0) errors.push(`${id}: the translation has argument(s) {${list(extraArgs)}} that the English does not`);
  for (const [name, kind] of source.args) {
    const translatedKind = translated.args.get(name);
    if (translatedKind !== undefined && translatedKind !== kind) {
      errors.push(`${id}: argument {${name}} is a ${kind} in the English and a ${translatedKind} in the translation`);
    }
  }

  const missingTags = [...source.tags].filter((name) => !translated.tags.has(name));
  const extraTags = [...translated.tags].filter((name) => !source.tags.has(name));
  if (missingTags.length > 0) errors.push(`${id}: the translation is missing rich-text tag(s) <${list(missingTags)}>`);
  if (extraTags.length > 0) errors.push(`${id}: the translation has rich-text tag(s) <${list(extraTags)}> that the English does not`);

  return { ok: errors.length === 0, errors };
};
