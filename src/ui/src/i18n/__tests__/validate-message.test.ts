// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The per-message parity function every catalog goes through (plan §3 of #1770): the build,
 * `make check-ui-i18n-catalog` and the later catalog review all call this one function, so
 * each thing it claims to catch is driven here and the error is checked to name the id.
 */
import { TYPE } from '@formatjs/icu-messageformat-parser';
import { describe, expect, it } from 'vitest';
import { parseMessage, validateMessage } from '../../../scripts/i18n/validate-message.mjs';

const PLURAL_EN = '{count, plural, one {# document} other {# documents}}';

describe('validateMessage', () => {
  it('accepts a translation with the same arguments, plural structure and tags', () => {
    expect(validateMessage('x.plain', 'Hello {name}', 'Bonjour {name}')).toEqual({ ok: true, errors: [] });
    expect(validateMessage('x.plural', PLURAL_EN, '{count, plural, one {# document} other {# documents}}')).toEqual({
      ok: true,
      errors: [],
    });
    expect(validateMessage('x.tag', 'Open <link>the guide</link>', 'Ouvrir <link>le guide</link>')).toEqual({ ok: true, errors: [] });
    expect(validateMessage('x.none', 'Language', 'Langue')).toEqual({ ok: true, errors: [] });
  });

  it('lets a translation reorder arguments and change plural branches, which languages legitimately do', () => {
    expect(validateMessage('x.order', '{a} before {b}', '{b} avant {a}').ok).toBe(true);
    // Arabic has six plural categories; only `other` is mandatory.
    expect(validateMessage('x.branches', PLURAL_EN, '{count, plural, zero {لا شيء} one {#} two {#} few {#} many {#} other {#}}').ok).toBe(
      true,
    );
    // A number format on a plain argument, or none, is the translator's call.
    expect(validateMessage('x.format', '{total, number} in total', '{total} au total').ok).toBe(true);
  });

  it('rejects malformed ICU in the translation, naming the id and the position', () => {
    const verdict = validateMessage('x.broken', 'Hello {name}', 'Bonjour {name');
    expect(verdict.ok).toBe(false);
    expect(verdict.errors).toHaveLength(1);
    expect(verdict.errors[0]).toMatch(
      /^x\.broken: the translation does not parse as ICU: EXPECT_ARGUMENT_CLOSING_BRACE at line 1, column \d+$/,
    );
  });

  it('rejects a malformed English source rather than trusting it', () => {
    const verdict = validateMessage('x.source', 'Hello {name', 'Bonjour {name}');
    expect(verdict.ok).toBe(false);
    expect(verdict.errors[0]).toContain('x.source: the English source does not parse as ICU');
  });

  it('rejects a missing argument and an extra argument, each named', () => {
    const missing = validateMessage('x.missing', 'Hello {first} {last}', 'Bonjour {first}');
    expect(missing.ok).toBe(false);
    expect(missing.errors).toEqual(['x.missing: the translation is missing argument(s) {last}']);

    const extra = validateMessage('x.extra', 'Hello {name}', 'Bonjour {name} {title}');
    expect(extra.ok).toBe(false);
    expect(extra.errors).toEqual(['x.extra: the translation has argument(s) {title} that the English does not']);

    const renamed = validateMessage('x.renamed', 'Hello {name}', 'Bonjour {nom}');
    expect(renamed.errors).toEqual([
      'x.renamed: the translation is missing argument(s) {name}',
      'x.renamed: the translation has argument(s) {nom} that the English does not',
    ]);
  });

  it('rejects a plural or select without an `other` branch', () => {
    const plural = validateMessage('x.other', PLURAL_EN, '{count, plural, one {# document}}');
    expect(plural.ok).toBe(false);
    expect(plural.errors[0]).toContain('x.other: the translation does not parse as ICU: MISSING_OTHER_CLAUSE');

    const select = validateMessage('x.select', '{kind, select, pdf {a PDF} other {a file}}', '{kind, select, pdf {un PDF}}');
    expect(select.ok).toBe(false);
    expect(select.errors[0]).toContain('MISSING_OTHER_CLAUSE');
  });

  it('rejects a plural turned into a plain argument, and a selectordinal turned into a plural', () => {
    const flattened = validateMessage('x.flat', PLURAL_EN, '{count} documents');
    expect(flattened.ok).toBe(false);
    expect(flattened.errors).toEqual(['x.flat: argument {count} is a plural in the English and a plain argument in the translation']);

    const ordinal = validateMessage(
      'x.ordinal',
      '{n, selectordinal, one {#st} two {#nd} few {#rd} other {#th}}',
      '{n, plural, one {#er} other {#e}}',
    );
    expect(ordinal.errors).toEqual(['x.ordinal: argument {n} is a selectordinal in the English and a plural in the translation']);
  });

  it('rejects rich-text tags that differ', () => {
    const verdict = validateMessage('x.tags', 'Read <b>this</b> and <link>that</link>', 'Lisez <i>ceci</i> et <link>cela</link>');
    expect(verdict.ok).toBe(false);
    expect(verdict.errors).toEqual([
      'x.tags: the translation is missing rich-text tag(s) <b>',
      'x.tags: the translation has rich-text tag(s) <i> that the English does not',
    ]);
  });

  it('treats `#` inside a plural as that plural’s own variable', () => {
    // Spelling the count as `{count}` in one branch and `#` in another is the same shape.
    expect(validateMessage('x.pound', PLURAL_EN, '{count, plural, one {{count} document} other {# documents}}').ok).toBe(true);
    // `#` does not introduce an argument of its own, so a translation using it where the
    // English only ever wrote `{count}` inside the plural is still at parity.
    expect(
      validateMessage(
        'x.pound2',
        '{count, plural, one {{count} file} other {{count} files}}',
        '{count, plural, one {# fichier} other {# fichiers}}',
      ).ok,
    ).toBe(true);
    // Outside a plural, `#` is just text.
    expect(validateMessage('x.hash', 'Item #', 'Article #').ok).toBe(true);
  });

  it('rejects non-string inputs instead of throwing', () => {
    expect(validateMessage('x.type', 'Hello', 42 as unknown as string).errors).toEqual(['x.type: the translation is not a string']);
    expect(validateMessage('x.type', undefined as unknown as string, 'Bonjour').errors).toEqual([
      'x.type: the English source is not a string',
    ]);
  });
});

describe('parseMessage', () => {
  it('returns the AST react-intl formats, with `#` as a pound element inside the plural', () => {
    const ast = parseMessage(PLURAL_EN);
    expect(ast).toHaveLength(1);
    const plural = ast[0];
    expect(plural.type).toBe(TYPE.plural);
    if (plural.type !== TYPE.plural) throw new Error('unreachable');
    expect(plural.value).toBe('count');
    expect(Object.keys(plural.options)).toEqual(['one', 'other']);
    expect(plural.options.other.value[0].type).toBe(TYPE.pound);
  });

  it('throws on malformed ICU', () => {
    expect(() => parseMessage('{count, plural, one {# x}')).toThrow(SyntaxError);
  });
});
