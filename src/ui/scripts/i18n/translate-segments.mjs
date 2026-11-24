#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * What a machine-translation engine may see of an English ICU message, and how its answer
 * becomes an ICU message again: the Node half of `make ui-i18n-translate`
 * (`scripts/ui_i18n_translate.py`, which calls the engine).
 *
 *   node scripts/i18n/translate-segments.mjs plan <catalog-dir> <locale> [--limit <n>]
 *   node scripts/i18n/translate-segments.mjs join <locale>      (a JSON request on stdin)
 *
 * An engine translates prose. It does not know that `{count}` is an argument, that `#` is a
 * plural's number, that `one {…} other {…}` are branches to keep apart, or that `<link>…</link>`
 * is a react-intl tag, and a translation that renames, drops or reorders any of those breaks
 * the screen. So the engine never sees ICU:
 *
 *   - **Selectors become whole sentences.** `hoistSelectors` lifts every plural and select to
 *     the front, so `You have {count, plural, one {# file} other {# files}}.` becomes one plural
 *     whose branches are `You have # file.` and `You have # files.`. Each branch is translated
 *     as a full sentence, never as the fragment `# file`.
 *   - **Each sentence becomes HTML.** Every argument and `#` is a
 *     `<span translate="no" data-ph="N">` the engine copies untouched; every rich-text tag is a
 *     `<span data-tag="N">` whose content it translates; text is HTML-escaped.
 *   - **The answer is rebuilt as an AST and printed**, so ICU quoting of `'`, `{`, `}` and `#` is
 *     the printer's and never the engine's. A span the request did not hold, a placeholder lost,
 *     repeated or rewritten, markup the request did not send, or a tag nested wrongly is an error
 *     for that message, never a guess. The English sentence's own leading and trailing spaces are
 *     kept, since engines trim them.
 *   - **Every rebuilt message passes `validateMessage`** against its English, the check the build
 *     applies, before it is returned as a result.
 *
 * `plan` lists the catalog's entries that need a translation (missing, or stale: the English
 * changed since its `sourceHash` was recorded; `coverage.mjs` decides which) with the sentences
 * to send. `join` re-derives the same sentences from the English and fills in the engine's
 * answers. The locale's typography rules (`TYPOGRAPHY`) apply to the rebuilt text.
 *
 * Exit codes: 0 the JSON answer was written; 2 usage, or a tool error (an unreadable catalog
 * directory or file, malformed JSON, a malformed request).
 */
import { existsSync, readFileSync, realpathSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { TYPE, parse } from '@formatjs/icu-messageformat-parser';
import { hoistSelectors } from '@formatjs/icu-messageformat-parser/manipulator.js';
import { printAST } from '@formatjs/icu-messageformat-parser/printer.js';
import { readCatalog, sourceHash } from './catalog.mjs';
import { localeCoverage } from './coverage.mjs';
import { validateMessage } from './validate-message.mjs';

/** @typedef {import('@formatjs/icu-messageformat-parser').MessageFormatElement} MessageFormatElement */

/** @param {MessageFormatElement} element */
const isSelector = (element) => element.type === TYPE.plural || element.type === TYPE.select;

/**
 * Visit every sentence of a hoisted message in a fixed order. After `hoistSelectors` a list of
 * elements is either one selector whose branches are hoisted lists again, or a sentence with no
 * selector at all (it throws for a selector inside a tag rather than produce one).
 *
 * @param {MessageFormatElement[]} elements
 * @param {(sentence: MessageFormatElement[]) => MessageFormatElement[]} visit returns the sentence to keep
 * @returns {MessageFormatElement[]}
 */
const mapSentences = (elements, visit) => {
  if (elements.length === 1 && isSelector(elements[0])) {
    const selector = /** @type {import('@formatjs/icu-messageformat-parser').PluralElement} */ (elements[0]);
    const options = {};
    for (const [key, option] of Object.entries(selector.options)) {
      options[key] = { ...option, value: mapSentences(option.value, visit) };
    }
    return [{ ...selector, options }];
  }
  return visit(elements);
};

/** @param {string} text */
const escapeHtml = (text) => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

const NAMED_ENTITIES = /** @type {Record<string, string>} */ ({ amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0' });

/** A code point a decoded entity may stand for: not NUL, not a lone surrogate, inside Unicode. */
const isCharacter = (/** @type {number} */ code) => code > 0 && code <= 0x10ffff && (code < 0xd800 || code > 0xdfff);

/** @param {string} text */
const unescapeHtml = (text) =>
  text.replace(/&(#[xX][0-9a-fA-F]+|#\d+|[a-zA-Z]+);/g, (entity, name) => {
    if (name[0] !== '#') return NAMED_ENTITIES[name.toLowerCase()] ?? entity;
    const code = name[1] === 'x' || name[1] === 'X' ? Number.parseInt(name.slice(2), 16) : Number.parseInt(name.slice(1), 10);
    return isCharacter(code) ? String.fromCodePoint(code) : entity;
  });

/**
 * A sentence as the HTML the engine receives. `slots` collects, in order, the element behind
 * every span, so the answer can be mapped back by number.
 *
 * @param {MessageFormatElement[]} sentence
 * @param {MessageFormatElement[]} slots
 * @returns {string}
 */
const sentenceToHtml = (sentence, slots) =>
  sentence
    .map((element) => {
      if (element.type === TYPE.literal) return escapeHtml(element.value);
      if (isSelector(element)) throw new Error('a plural or select survived hoisting');
      const slot = slots.push(element) - 1;
      if (element.type === TYPE.tag) return `<span data-tag="${slot}">${sentenceToHtml(element.children, slots)}</span>`;
      return `<span translate="no" data-ph="${slot}">${escapeHtml(printAST([element]))}</span>`;
    })
    .join('');

/** Any span, its closing tag, any other markup, text, or a bare `<` that opens no tag. */
const HTML_TOKEN = /<\s*span\b([^>]*)>|<\s*\/\s*span\s*>|<[a-zA-Z/!][^>]*>|[^<]+|</g;

/**
 * @typedef {object} SpanNode
 * @property {number} slot
 * @property {'ph' | 'tag'} kind
 * @property {Array<string | SpanNode>} children
 */

/**
 * The engine's HTML for one sentence, as message elements again.
 *
 * @param {string} html
 * @param {MessageFormatElement[]} slots what `sentenceToHtml` recorded for the same sentence
 * @returns {MessageFormatElement[]}
 */
const htmlToSentence = (html, slots) => {
  /** @type {{ children: Array<string | SpanNode> }} */
  const root = { children: [] };
  /** @type {Array<{ children: Array<string | SpanNode> }>} */
  const open = [root];
  const seen = new Set();
  for (const [token, attributes] of html.matchAll(HTML_TOKEN)) {
    const parent = open[open.length - 1];
    if (attributes !== undefined) {
      const placeholder = /\bdata-ph\s*=\s*["']?(\d+)/i.exec(attributes);
      const tag = /\bdata-tag\s*=\s*["']?(\d+)/i.exec(attributes);
      const found = placeholder ?? tag;
      if (!found) throw new Error(`the answer has a <span> the request did not: ${token}`);
      const slot = Number(found[1]);
      const element = slots[slot];
      if (element === undefined) throw new Error(`the answer names span ${slot}, which the request did not have`);
      if (seen.has(slot)) throw new Error(`the answer repeats span ${slot}`);
      if ((element.type === TYPE.tag) !== (tag !== null)) throw new Error(`the answer changed what span ${slot} is`);
      seen.add(slot);
      /** @type {SpanNode} */
      const node = { slot, kind: tag ? 'tag' : 'ph', children: [] };
      parent.children.push(node);
      open.push(node);
    } else if (/^<\s*\/\s*span/i.test(token)) {
      if (open.length === 1) throw new Error('the answer closes a <span> it never opened');
      const closed = /** @type {SpanNode} */ (open.pop());
      // A protected span must come back exactly as it was sent: the request escapes every `<`,
      // so its content is one run of text, the printed placeholder.
      if (closed.kind === 'ph') {
        const sent = printAST([slots[closed.slot]]);
        if (closed.children.length !== 1 || closed.children[0] !== sent) throw new Error(`the answer changed span ${closed.slot}`);
      }
    } else if (token !== '<' && token.startsWith('<')) {
      // The request escapes every `<` of the English, so any markup other than its spans is the engine's.
      throw new Error(`the answer has markup the request did not: ${token}`);
    } else {
      // Text. An engine may give an escaped `<` back bare; one that opens no tag is that text again.
      parent.children.push(unescapeHtml(token));
    }
  }
  if (open.length !== 1) throw new Error('the answer leaves a <span> open');
  const lost = slots.map((_, slot) => slot).filter((slot) => !seen.has(slot));
  if (lost.length > 0) throw new Error(`the answer lost span(s) ${lost.join(', ')}`);

  /** @param {Array<string | SpanNode>} nodes @returns {MessageFormatElement[]} */
  const toElements = (nodes) => {
    /** @type {MessageFormatElement[]} */
    const elements = [];
    for (const node of nodes) {
      if (typeof node === 'string') {
        const last = elements[elements.length - 1];
        if (last?.type === TYPE.literal) last.value += node;
        else elements.push({ type: TYPE.literal, value: node });
      } else if (node.kind === 'ph') {
        elements.push(structuredClone(slots[node.slot]));
      } else {
        const tag = /** @type {import('@formatjs/icu-messageformat-parser').TagElement} */ (slots[node.slot]);
        elements.push({ type: TYPE.tag, value: tag.value, children: toElements(node.children) });
      }
    }
    return elements;
  };
  return toElements(root.children);
};

/**
 * A sentence in reading order, tags opened in place: each literal, and `null` for anything else
 * the reader sees there (an argument, `#`).
 *
 * @param {MessageFormatElement[]} sentence
 * @returns {Array<import('@formatjs/icu-messageformat-parser').LiteralElement | null>}
 */
const readingOrder = (sentence) =>
  sentence.flatMap((element) => {
    if (element.type === TYPE.literal) return [element];
    if (element.type === TYPE.tag) return readingOrder(element.children);
    return [null];
  });

/** Space characters an engine puts before punctuation: space, no-break space, narrow no-break space. */
const SPACES = '[ \\u00a0\\u202f]';

/**
 * Canadian French typography, applied to the rebuilt text of one sentence: the typographic
 * apostrophe; no space before `;`, `?` and `!`; a no-break space before a colon that ends a
 * phrase, that is one followed by a space or by the end of the sentence (so `https://`,
 * `10:30` and `{h}:{m}` keep theirs); French quotation marks with no-break spaces inside.
 * Straight double quotes are paired across the sentence and converted only when they pair up.
 *
 * @param {MessageFormatElement[]} sentence changed in place
 */
export const canadianFrenchTypography = (sentence) => {
  const order = readingOrder(sentence);
  const literals = order.filter((item) => item !== null);
  // Whether the text after each literal starts a new phrase, read before anything changes.
  const phraseEndsAfter = new Map(
    order.map((item, index) => {
      const next = order[index + 1];
      return [item, next === undefined || (next !== null && /^\s/.test(next.value))];
    }),
  );
  const doubleQuotes = literals.reduce((count, literal) => count + (literal.value.match(/["“”]/g)?.length ?? 0), 0);
  let opening = true;
  for (const literal of literals) {
    let text = literal.value.replace(/'/g, '’');
    if (doubleQuotes % 2 === 0) {
      text = text.replace(/["“”]/g, () => {
        const mark = opening ? '«\u00a0' : '\u00a0»';
        opening = !opening;
        return mark;
      });
    }
    text = text
      .replace(new RegExp(`«${SPACES}*`, 'g'), '«\u00a0')
      .replace(new RegExp(`${SPACES}*»`, 'g'), '\u00a0»')
      .replace(new RegExp(`${SPACES}+([;?!])`, 'g'), '$1')
      .replace(new RegExp(`${SPACES}*:(?=\\s)`, 'g'), '\u00a0:');
    if (phraseEndsAfter.get(literal)) text = text.replace(new RegExp(`${SPACES}*:$`), '\u00a0:');
    literal.value = text;
  }
};

/** Typography applied to a locale's rebuilt text; a locale with no entry keeps the engine's. */
export const TYPOGRAPHY = Object.freeze(
  /** @type {Record<string, (sentence: MessageFormatElement[]) => void>} */ ({ 'fr-CA': canadianFrenchTypography }),
);

/** Whether a sentence's HTML has anything to translate: a letter outside the protected spans. */
const hasText = (/** @type {string} */ html) => /\p{L}/u.test(unescapeHtml(html.replace(/<span translate="no"[^>]*>[^<]*<\/span>/g, '').replace(/<[^>]*>/g, '')));

/**
 * The sentences of one English message, as the engine receives them. Throws when the message does
 * not parse, or holds a plural or select inside a rich-text tag (which cannot be hoisted).
 *
 * @param {string} english
 * @returns {{ html: string, translate: boolean }[]} `translate` is false for a sentence with no text (such as `#`), which is kept as it is
 */
export const splitMessage = (english) => {
  /** @type {{ html: string, translate: boolean }[]} */
  const sentences = [];
  mapSentences(hoistSelectors(parse(english)), (sentence) => {
    const html = sentenceToHtml(sentence, []);
    sentences.push({ html, translate: hasText(html) });
    return sentence;
  });
  return sentences;
};

/**
 * One message rebuilt from the engine's answers, one per sentence of `splitMessage(english)`, in
 * the same order.
 *
 * @param {string} id
 * @param {string} english the English `defaultMessage`
 * @param {string[]} answers
 * @param {string} locale picks the typography (`TYPOGRAPHY`)
 * @returns {{ id: string, message: string } | { id: string, errors: string[] }}
 */
export const joinMessage = (id, english, answers, locale) => {
  const typography = Object.hasOwn(TYPOGRAPHY, locale) ? TYPOGRAPHY[locale] : undefined;
  let next = 0;
  try {
    const ast = mapSentences(hoistSelectors(parse(english)), (sentence) => {
      const slots = [];
      const sent = sentenceToHtml(sentence, slots);
      const answer = answers[next];
      if (typeof answer !== 'string') throw new Error(`there is no answer for sentence ${next + 1}`);
      next += 1;
      // Engines trim or pad the edges of a text; the English sentence's own edges are kept.
      const [, lead, trail] = /^(\s*)[\s\S]*?(\s*)$/.exec(sent) ?? ['', '', ''];
      const rebuilt = htmlToSentence(`${lead}${answer.trim()}${trail}`, slots);
      typography?.(rebuilt);
      return rebuilt;
    });
    if (next !== answers.length) throw new Error(`there are ${answers.length} answers for ${next} sentence(s)`);
    const message = printAST(ast);
    const verdict = validateMessage(id, english, message);
    return verdict.ok ? { id, message } : { id, errors: verdict.errors };
  } catch (error) {
    return { id, errors: [`${id}: ${error instanceof Error ? error.message : String(error)}`] };
  }
};

/** @param {unknown} value */
const isPlainObject = (value) => typeof value === 'object' && value !== null && !Array.isArray(value);

/**
 * The entries of `<locale>.json` that need a translation, with the sentences to send.
 *
 * @param {Record<string, unknown>} enJson the parsed `en.json`
 * @param {Record<string, unknown>} localeJson the parsed `<locale>.json`, `{}` when there is none yet
 * @param {string} locale
 * @param {{ limit?: number | null }} [options] keep only the first `limit` entries to translate
 */
export const planTranslation = (enJson, localeJson, locale, { limit = null } = {}) => {
  const coverage = localeCoverage(enJson, localeJson, locale);
  const stale = new Set(coverage.stale);
  const pendingIds = [...coverage.missing, ...coverage.stale].sort();
  const pending = [];
  const unsplittable = [];
  for (const id of pendingIds) {
    const english = /** @type {{ defaultMessage: string }} */ (enJson[id]).defaultMessage;
    try {
      pending.push({ id, english, sourceHash: sourceHash(english), stale: stale.has(id), sentences: splitMessage(english) });
    } catch (error) {
      unsplittable.push({ id, reason: error instanceof Error ? error.message : String(error) });
    }
  }
  return {
    locale,
    sourceKeys: Object.keys(enJson).length,
    pending: limit === null ? pending : pending.slice(0, limit),
    deferred: limit === null ? 0 : Math.max(0, pending.length - limit),
    unsplittable,
    invalid: coverage.invalid,
    problems: coverage.problems,
    dead: coverage.dead,
  };
};

const usage = () => {
  console.error('usage: node scripts/i18n/translate-segments.mjs plan <catalog-dir> <locale> [--limit <n>]');
  console.error('       node scripts/i18n/translate-segments.mjs join <locale>   (request JSON on stdin)');
  return 2;
};

/** A translation's locale: the catalog file-name rule of `vite-plugin-catalogs.mjs`, minus the source `en`. */
const isTargetLocale = (/** @type {string | undefined} */ tag) =>
  typeof tag === 'string' && tag !== 'en' && /^[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-(?:[A-Z]{2}|\d{3}))?$/.test(tag);

/**
 * @param {string[]} argv
 * @returns {number}
 */
const main = (argv) => {
  const [command, ...rest] = argv;
  try {
    if (command === 'plan') {
      const [dir, locale, flag, value] = rest;
      const limitOk = flag === undefined || (flag === '--limit' && /^\d+$/.test(value ?? '') && rest.length === 4);
      if (!dir || !isTargetLocale(locale) || !limitOk || (flag === undefined && rest.length !== 2)) return usage();
      const catalogDir = resolve(dir);
      const enJson = readCatalog(join(catalogDir, 'en.json'));
      const localePath = join(catalogDir, `${locale}.json`);
      const localeJson = existsSync(localePath) ? readCatalog(localePath) : {};
      const plan = planTranslation(enJson, localeJson, locale, { limit: flag ? Number(value) : null });
      process.stdout.write(`${JSON.stringify(plan, null, 2)}\n`);
      return 0;
    }
    if (command === 'join') {
      const [locale] = rest;
      if (!isTargetLocale(locale) || rest.length !== 1) return usage();
      const request = JSON.parse(readFileSync(0, 'utf8'));
      if (!isPlainObject(request) || !Array.isArray(request.entries)) throw new TypeError('the request is { "entries": [{ "id", "english", "answers" }] }');
      const results = request.entries.map((/** @type {any} */ entry) => {
        if (!isPlainObject(entry) || typeof entry.id !== 'string' || typeof entry.english !== 'string' || !Array.isArray(entry.answers)) {
          throw new TypeError('each entry is { "id": string, "english": string, "answers": string[] }');
        }
        return joinMessage(entry.id, entry.english, entry.answers, locale);
      });
      process.stdout.write(`${JSON.stringify({ results }, null, 2)}\n`);
      return 0;
    }
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    return 2;
  }
  return usage();
};

/** Whether this file is the script Node was started with (the Vitest suite imports it). */
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
