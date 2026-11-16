// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The catalog format and its compiler. A translation catalog entry is
 * `{ message, sourceHash }`; compiling it against `en.json` yields react-intl's AST and never
 * drops anything silently: every malformed, mismatched, mis-shaped or dead entry is a problem.
 */
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { TYPE } from '@formatjs/icu-messageformat-parser';
import { afterEach, describe, expect, it } from 'vitest';
import { SOURCE_HASH_LENGTH, compileCatalog, readCatalog, sourceHash } from '../../../scripts/i18n/catalog.mjs';
import { parseMessage } from '../../../scripts/i18n/validate-message.mjs';

const EN = {
  'nav.documents': { defaultMessage: 'Documents', description: 'Side navigation link' },
  'nav.count': { defaultMessage: '{count, plural, one {# document} other {# documents}}', description: 'Document count' },
  'nav.greeting': { defaultMessage: 'Hello {name}', description: 'Greeting' },
};

const entry = (id: keyof typeof EN, message: string) => ({ message, sourceHash: sourceHash(EN[id].defaultMessage) });

describe('sourceHash', () => {
  it('is 16 lower-case hex characters, stable for the same text', () => {
    const hash = sourceHash('Documents');
    expect(hash).toMatch(/^[0-9a-f]{16}$/);
    expect(hash).toHaveLength(SOURCE_HASH_LENGTH);
    expect(sourceHash('Documents')).toBe(hash);
    // Pinned so a change of algorithm shows up here rather than as every translation reading stale.
    expect(hash).toBe('b4e929d8bcfe0418'); // printf 'Documents' | sha256sum | cut -c1-16
  });

  it('differs for different text', () => {
    expect(sourceHash('Documents')).not.toBe(sourceHash('Document'));
  });

  it('normalises to NFC first, so composed and decomposed accents hash alike', () => {
    expect(sourceHash('caf\u00e9')).toBe(sourceHash('cafe\u0301'));
  });
});

describe('compileCatalog', () => {
  it('compiles a good catalog to ASTs, keyed by id, with sourceHash stripped', () => {
    const fr = {
      'nav.documents': entry('nav.documents', 'Documents'),
      'nav.count': entry('nav.count', '{count, plural, one {# document} other {# documents}}'),
    };
    const { messages, problems } = compileCatalog(EN, fr, 'fr-CA');
    expect(problems).toEqual([]);
    expect(Object.keys(messages).sort()).toEqual(['nav.count', 'nav.documents']);
    expect(messages['nav.documents']).toEqual([{ type: TYPE.literal, value: 'Documents' }]);
    expect(messages['nav.count']).toEqual(parseMessage('{count, plural, one {# document} other {# documents}}'));
    expect(JSON.stringify(messages)).not.toContain('sourceHash');
  });

  it('leaves out an id the translation lacks: English is the fallback', () => {
    const { messages, problems } = compileCatalog(EN, { 'nav.documents': entry('nav.documents', 'Documents') }, 'fr-CA');
    expect(problems).toEqual([]);
    expect(Object.keys(messages)).toEqual(['nav.documents']);
  });

  it('does not treat a stale sourceHash as a compile problem (the coverage report owns staleness)', () => {
    const { messages, problems } = compileCatalog(
      EN,
      { 'nav.documents': { message: 'Documents', sourceHash: '0123456789abcdef' } },
      'fr-CA',
    );
    expect(problems).toEqual([]);
    expect(Object.keys(messages)).toEqual(['nav.documents']);
  });

  it('reports a malformed translation, naming the catalog and the id, and compiles nothing for it', () => {
    const fr = {
      'nav.documents': entry('nav.documents', 'Documents'),
      'nav.greeting': entry('nav.greeting', 'Bonjour {name'),
    };
    const { messages, problems } = compileCatalog(EN, fr, 'fr-CA');
    expect(problems).toHaveLength(1);
    expect(problems[0]).toMatch(/^fr-CA\.json: nav\.greeting: the translation does not parse as ICU: EXPECT_ARGUMENT_CLOSING_BRACE/);
    expect(Object.keys(messages)).toEqual(['nav.documents']);
  });

  it('reports a translation whose arguments differ from the English', () => {
    const { problems } = compileCatalog(EN, { 'nav.greeting': entry('nav.greeting', 'Bonjour {nom}') }, 'fr-CA');
    expect(problems).toEqual([
      'fr-CA.json: nav.greeting: the translation is missing argument(s) {name}',
      'fr-CA.json: nav.greeting: the translation has argument(s) {nom} that the English does not',
    ]);
  });

  it('reports an id that en.json no longer has (a dead translation)', () => {
    const { messages, problems } = compileCatalog(EN, { 'nav.gone': { message: 'Parti', sourceHash: sourceHash('Gone') } }, 'fr-CA');
    expect(messages).toEqual({});
    expect(problems).toEqual([
      'fr-CA.json: "nav.gone" is not in en.json (a dead translation: delete it, or re-extract if the message exists)',
    ]);
  });

  it('reports an entry that is not { message, sourceHash }', () => {
    const fr: Record<string, unknown> = {
      'nav.documents': 'Documents',
      'nav.greeting': { message: 'Bonjour {name}' },
      'nav.count': { message: '{count, plural, one {# document} other {# documents}}', sourceHash: 'not-hex' },
    };
    const { messages, problems } = compileCatalog(EN, fr, 'fr-CA');
    expect(messages).toEqual({});
    expect(problems).toEqual([
      'fr-CA.json: "nav.count" needs a sourceHash of 16 hex characters (sourceHash of the English defaultMessage)',
      'fr-CA.json: "nav.documents" must be { "message": "<ICU>", "sourceHash": "<16 hex>" }',
      'fr-CA.json: "nav.greeting" needs a sourceHash of 16 hex characters (sourceHash of the English defaultMessage)',
    ]);
  });

  it('reports an English entry without a string defaultMessage', () => {
    const en: Record<string, unknown> = { 'nav.documents': { description: 'no message' } };
    const { problems } = compileCatalog(en, { 'nav.documents': { message: 'Documents', sourceHash: '0123456789abcdef' } }, 'fr-CA');
    expect(problems).toEqual(['en.json: "nav.documents" has no string defaultMessage']);
  });

  it('reports a catalog that is not an object', () => {
    expect(compileCatalog(EN, [] as unknown as Record<string, unknown>, 'fr-CA').problems).toEqual([
      'fr-CA.json is not a JSON object keyed by message id',
    ]);
    expect(compileCatalog(null as unknown as Record<string, unknown>, {}, 'fr-CA').problems).toEqual([
      'en.json is not a JSON object keyed by message id',
    ]);
  });

  it('compiles an empty catalog to no messages and no problems', () => {
    expect(compileCatalog(EN, {}, 'fr-CA')).toEqual({ messages: {}, problems: [] });
  });
});

describe('readCatalog', () => {
  let dir: string;

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it('reads a JSON object and rejects anything else', () => {
    dir = mkdtempSync(join(tmpdir(), 'i18n-catalog-'));
    writeFileSync(join(dir, 'fr-CA.json'), JSON.stringify({ 'nav.documents': entry('nav.documents', 'Documents') }));
    writeFileSync(join(dir, 'list.json'), '[]');
    writeFileSync(join(dir, 'broken.json'), '{');
    expect(readCatalog(join(dir, 'fr-CA.json'))).toEqual({ 'nav.documents': entry('nav.documents', 'Documents') });
    expect(() => readCatalog(join(dir, 'list.json'))).toThrow(/keyed by message id/);
    expect(() => readCatalog(join(dir, 'broken.json'))).toThrow(SyntaxError);
    expect(() => readCatalog(join(dir, 'absent.json'))).toThrow(/ENOENT/);
  });
});
