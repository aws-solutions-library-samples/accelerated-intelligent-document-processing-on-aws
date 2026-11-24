// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The Node half of `make ui-i18n-translate`: what a translation engine is shown of an English ICU
 * message (whole sentences as HTML, arguments and tags as numbered spans) and how its answer is
 * rebuilt into an ICU message that passes the build's parity check, or is refused with a reason.
 */
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { parse, TYPE, type LiteralElement } from '@formatjs/icu-messageformat-parser';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { sourceHash } from '../../../scripts/i18n/catalog.mjs';
import { canadianFrenchTypography, joinMessage, planTranslation, splitMessage } from '../../../scripts/i18n/translate-segments.mjs';
import { validateMessage } from '../../../scripts/i18n/validate-message.mjs';

const CLI = resolve(__dirname, '..', '..', '..', 'scripts', 'i18n', 'translate-segments.mjs');
const EN_JSON = resolve(__dirname, '..', 'en.json');
const NBSP = '\u00a0';

/** A placeholder span as the request writes it. */
const ph = (slot: number, text: string) => `<span translate="no" data-ph="${slot}">${text}</span>`;

/** Rebuild with the engine's answers and return the message, failing the test on errors. */
const rebuilt = (english: string, answers: string[], locale = 'fr-CA') => {
  const result = joinMessage('test.id', english, answers, locale);
  if ('errors' in result) throw new Error(result.errors.join('\n'));
  return result.message;
};

/** Run the typography over one literal and return its text. */
const typeset = (text: string) => {
  const literal: LiteralElement = { type: TYPE.literal, value: text };
  canadianFrenchTypography([literal]);
  return literal.value;
};

describe('splitMessage: what the engine is shown', () => {
  it('sends plain text as escaped HTML', () => {
    expect(splitMessage('Upload documents & files > 5 MB')).toEqual([{ html: 'Upload documents &amp; files &gt; 5 MB', translate: true }]);
  });

  it('protects every argument and `#` in a numbered translate="no" span', () => {
    expect(splitMessage('Delete {name} from {queue}?')).toEqual([
      { html: `Delete ${ph(0, '{name}')} from ${ph(1, '{queue}')}?`, translate: true },
    ]);
    expect(splitMessage('{total, number} pages')[0].html).toBe(`${ph(0, '{total, number}')} pages`);
  });

  it('hoists a plural so each branch is a whole sentence', () => {
    expect(splitMessage('You have {count, plural, one {# file} other {# files}} in {folder}.')).toEqual([
      { html: `You have ${ph(0, '#')} file in ${ph(1, '{folder}')}.`, translate: true },
      { html: `You have ${ph(0, '#')} files in ${ph(1, '{folder}')}.`, translate: true },
    ]);
  });

  it('marks a sentence with nothing to translate, such as a bare `#`', () => {
    expect(splitMessage('{count, plural, one {#} other {# items}}')).toEqual([
      { html: ph(0, '#'), translate: false },
      { html: `${ph(0, '#')} items`, translate: true },
    ]);
  });

  it('turns a rich-text tag into a span whose content is translated', () => {
    expect(splitMessage('Read the <link>guide for {product}</link>.')).toEqual([
      { html: `Read the <span data-tag="0">guide for ${ph(1, '{product}')}</span>.`, translate: true },
    ]);
  });

  it('refuses a plural inside a tag, which cannot be hoisted', () => {
    expect(() => splitMessage('<b>{count, plural, one {# file} other {# files}}</b>')).toThrow(/Cannot hoist/);
  });
});

describe('joinMessage: rebuilding the answer', () => {
  it('rebuilds arguments, tags and plural branches from their span numbers', () => {
    const english = 'You have {count, plural, one {# file} other {# files}} in <b>{folder}</b>.';
    const message = rebuilt(english, [
      `Vous avez ${ph(0, '#')} fichier dans <span data-tag="1">${ph(2, '{folder}')}</span>.`,
      `Vous avez ${ph(0, '#')} fichiers dans <span data-tag="1">${ph(2, '{folder}')}</span>.`,
    ]);
    expect(message).toBe('{count,plural,one{Vous avez # fichier dans <b>{folder}</b>.} other{Vous avez # fichiers dans <b>{folder}</b>.}}');
    expect(validateMessage('test.id', english, message)).toEqual({ ok: true, errors: [] });
  });

  it('keeps the English sentence’s own leading and trailing spaces, whatever the engine did to them', () => {
    expect(rebuilt('Hi {n}', [` Salut ${ph(0, '{n}')} `])).toBe('Salut {n}');
    expect(rebuilt('Hi {n} ', [`Salut ${ph(0, '{n}')}`])).toBe('Salut {n} ');
  });

  it('keeps an entity that stands for no character as it is, so a reviewer sees it', () => {
    expect(rebuilt('Hi {n}', [`Salut ${ph(0, '{n}')}&#0;&#xD800;`], 'en')).toBe('Salut {n}&#0;&#xD800;');
  });

  it('applies no typography for a locale that has none, prototype names included', () => {
    expect(rebuilt('Hi {n}: ok', [`Salut ${ph(0, '{n}')} : ok`], 'constructor')).toBe('Salut {n} : ok');
  });

  it('accepts the span attributes in another order or quoting', () => {
    expect(rebuilt('Hello {name}', [`Bonjour <span data-ph='0' translate='no'>{name}</span>`])).toBe('Bonjour {name}');
  });

  it('keeps a < the engine gave back bare as text when it opens no tag, as the English had it', () => {
    const english = 'Low confidence: {score, number, ::.00} < {threshold, number, ::.00}';
    expect(splitMessage(english)[0].html).toBe(
      `Low confidence: ${ph(0, '{score, number, ::.00}')} &lt; ${ph(1, '{threshold, number, ::.00}')}`,
    );
    const message = rebuilt(english, [`Faible confiance : ${ph(0, '{score, number, ::.00}')} < ${ph(1, '{threshold, number, ::.00}')}`]);
    expect(message).toBe(`Faible confiance${NBSP}: {score, number, ::.00} < {threshold, number, ::.00}`);
    expect(validateMessage('test.id', english, message)).toEqual({ ok: true, errors: [] });
    expect(rebuilt('Score < 80%', ['Note < 80 %'])).toBe('Note < 80 %');
    expect(rebuilt('Below <{threshold}', [`Sous <${ph(0, '{threshold}')}`])).toBe('Sous <{threshold}');
  });

  it('decodes the HTML entities of the answer and lets the printer quote ICU syntax', () => {
    const message = rebuilt("Type '{name}' & go", ['Tapez {name} &amp; allez'], 'en');
    expect(message).toBe("Tapez '{name}' & allez");
    expect(parse(message)).toEqual([{ type: TYPE.literal, value: 'Tapez {name} & allez' }]);
  });

  it.each([
    ['a lost placeholder', ['Bonjour'], /lost span\(s\) 0/],
    ['a repeated placeholder', [`${ph(0, '{name}')} ${ph(0, '{name}')}`], /repeats span 0/],
    ['a span the request did not have', [`${ph(3, '{name}')}`], /span 3, which the request did not have/],
    ['a span with no number', ['<span>Bonjour</span>'], /a <span> the request did not/],
    ['other markup', [`Bonjour <b>${ph(0, '{name}')}</b>`], /markup the request did not: <b>/],
    ['a line break the request did not have', [`Bonjour<br>${ph(0, '{name}')}`], /markup the request did not: <br>/],
    ['an escaped < the engine turned into a tag', [`<No content> ${ph(0, '{name}')}`], /markup the request did not: <No content>/],
    ['an unclosed span', [`Bonjour <span translate="no" data-ph="0">{name}`], /leaves a <span> open/],
    ['a closing span with no opening', [`Bonjour ${ph(0, '{name}')}</span>`], /closes a <span> it never opened/],
    ['a placeholder turned into a tag', ['Bonjour <span data-tag="0">x</span>'], /changed what span 0 is/],
    ['text written into a protected span', ['Bonjour <span translate="no" data-ph="0">Marie</span>'], /changed span 0/],
    ['an emptied protected span', ['Bonjour <span translate="no" data-ph="0"></span>'], /changed span 0/],
    ['too few answers', [], /no answer for sentence 1/],
    ['too many answers', [`Bonjour ${ph(0, '{name}')}`, 'extra'], /2 answers for 1 sentence/],
  ])('refuses %s, naming the message', (_, answers, reason) => {
    const result = joinMessage('test.id', 'Hello {name}', answers, 'fr-CA');
    expect(result).not.toHaveProperty('message');
    expect('errors' in result && result.errors.join('\n')).toMatch(reason);
    expect('errors' in result && result.errors[0]).toMatch(/^test\.id: /);
  });
});

describe('canadianFrenchTypography', () => {
  it('uses the typographic apostrophe', () => {
    expect(typeset("l'extraction d'un document")).toBe('l’extraction d’un document');
  });

  it('puts no space before ; ? and !', () => {
    expect(typeset(`Supprimer ? Oui ! Non ;${NBSP}ici\u202f?`)).toBe(`Supprimer? Oui! Non;${NBSP}ici?`);
  });

  it('puts a no-break space before a colon that ends a phrase, and only there', () => {
    expect(typeset('Statut: prêt')).toBe(`Statut${NBSP}: prêt`);
    expect(typeset('Statut : prêt')).toBe(`Statut${NBSP}: prêt`);
    expect(typeset('Statut:')).toBe(`Statut${NBSP}:`);
    expect(typeset('https://example.com à 10:30')).toBe('https://example.com à 10:30');
  });

  it('reads a colon between two arguments as part of a value, not the end of a phrase', () => {
    expect(rebuilt('At {h}:{m} today', [`À ${ph(0, '{h}')}:${ph(1, '{m}')} aujourd'hui`])).toBe('À {h}:{m} aujourd’hui');
    expect(rebuilt('{name}: {value}', [`${ph(0, '{name}')}: ${ph(1, '{value}')}`])).toBe(`{name}${NBSP}: {value}`);
  });

  it('sees the end of a phrase across a tag boundary', () => {
    const english = '<b>Status:</b> {value}';
    expect(rebuilt(english, [`<span data-tag="0">Statut:</span> ${ph(1, '{value}')}`])).toBe(`<b>Statut${NBSP}:</b> {value}`);
  });

  it('pairs straight double quotes into French quotation marks across a sentence', () => {
    const sentence = [
      { type: TYPE.literal, value: 'Le profil "' } as LiteralElement,
      { type: TYPE.argument, value: 'name' } as const,
      { type: TYPE.literal, value: '" est enregistré.' } as LiteralElement,
    ];
    canadianFrenchTypography(sentence);
    expect([sentence[0].value, sentence[2].value]).toEqual([`Le profil «${NBSP}`, `${NBSP}» est enregistré.`]);
  });

  it('leaves an unpaired double quote alone and fixes the spaces inside « »', () => {
    expect(typeset('5" écran')).toBe('5" écran');
    expect(typeset('« oui »')).toBe(`«${NBSP}oui${NBSP}»`);
  });

  it('applies only to the locale it belongs to', () => {
    expect(rebuilt('Status: {value}', [`Statut : ${ph(0, '{value}')}`], 'en')).toBe('Statut : {value}');
    expect(rebuilt('Status: {value}', [`Statut : ${ph(0, '{value}')}`], 'fr-CA')).toBe(`Statut${NBSP}: {value}`);
  });
});

const EN = {
  'nav.upload': { defaultMessage: 'Upload documents', description: 'Upload button' },
  'nav.count': { defaultMessage: '{count, plural, one {# document} other {# documents}}', description: 'Document count' },
  'nav.greeting': { defaultMessage: 'Hello {name}', description: 'Greeting' },
  'nav.bold': { defaultMessage: '<b>{count, plural, one {# file} other {# files}}</b>', description: 'A plural in a tag' },
  'nav.documents': { defaultMessage: 'Documents', description: 'Side navigation link' },
};

const FR = {
  'nav.documents': { message: 'Documents', sourceHash: sourceHash('Documents') },
  'nav.count': { message: '{count, plural, one {# document} other {# documents}}', sourceHash: '0123456789abcdef' },
  'nav.greeting': { message: 'Bonjour {nom}', sourceHash: sourceHash('Hello {name}') },
  'nav.gone': { message: 'Parti', sourceHash: sourceHash('Gone') },
};

describe('planTranslation', () => {
  it('lists the missing and stale entries in id order and leaves current, invalid and dead ones alone', () => {
    const plan = planTranslation(EN, FR, 'fr-CA');
    expect(plan.pending.map(({ id, stale }) => [id, stale])).toEqual([
      ['nav.count', true],
      ['nav.upload', false],
    ]);
    expect(plan.pending[1]).toEqual({
      id: 'nav.upload',
      english: 'Upload documents',
      sourceHash: sourceHash('Upload documents'),
      stale: false,
      sentences: [{ html: 'Upload documents', translate: true }],
    });
    expect(plan.unsplittable).toEqual([{ id: 'nav.bold', reason: expect.stringMatching(/Cannot hoist/) }]);
    expect(plan.invalid).toEqual(['nav.greeting']);
    expect(plan.dead).toEqual(['nav.gone']);
    expect(plan.sourceKeys).toBe(5);
  });

  it('keeps the first entries under a limit and counts the rest as deferred', () => {
    const plan = planTranslation(EN, {}, 'fr-CA', { limit: 1 });
    expect(plan.pending.map(({ id }) => id)).toEqual(['nav.count']);
    expect(plan.deferred).toBe(3);
  });
});

describe('every message of the real en.json', () => {
  it('splits, and rebuilds from unchanged answers into a message that passes the parity check', () => {
    const en = JSON.parse(readFileSync(EN_JSON, 'utf8')) as Record<string, { defaultMessage: string }>;
    const failures: string[] = [];
    for (const [id, { defaultMessage }] of Object.entries(en)) {
      const answers = splitMessage(defaultMessage).map(({ html }) => html);
      const result = joinMessage(id, defaultMessage, answers, 'en');
      if ('errors' in result) failures.push(...result.errors);
    }
    expect(failures).toEqual([]);
    expect(Object.keys(en).length).toBeGreaterThan(0);
  });
});

describe('the CLI', () => {
  let dir: string;
  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'translate-segments-'));
    writeFileSync(join(dir, 'en.json'), JSON.stringify(EN));
  });
  afterEach(() => rmSync(dir, { recursive: true, force: true }));

  const cli = (args: string[], input?: string) => spawnSync(process.execPath, [CLI, ...args], { encoding: 'utf8', input });

  it('plans a locale that has no catalog yet: every entry is missing', () => {
    const result = cli(['plan', dir, 'fr-CA', '--limit', '2']);
    expect(result.status).toBe(0);
    const plan = JSON.parse(result.stdout);
    expect(plan.pending.map((entry: { id: string }) => entry.id)).toEqual(['nav.count', 'nav.documents']);
    expect(plan.deferred).toBe(2);
  });

  it('joins a request read from stdin', () => {
    const request = { entries: [{ id: 'nav.greeting', english: 'Hello {name}', answers: [`Bonjour ${ph(0, '{name}')} !`] }] };
    const result = cli(['join', 'fr-CA'], JSON.stringify(request));
    expect(result.status).toBe(0);
    expect(JSON.parse(result.stdout)).toEqual({ results: [{ id: 'nav.greeting', message: 'Bonjour {name}!' }] });
  });

  it.each([
    ['no command', []],
    ['a plan without a locale', ['plan', '.']],
    ['a malformed limit', ['plan', '.', 'fr-CA', '--limit', 'all']],
    ['a plan with an extra argument', ['plan', '.', 'fr-CA', '--limit', '3', 'junk']],
    ['the source locale', ['plan', '.', 'en']],
    ['a locale that is not a catalog name', ['plan', '.', '../fr-CA']],
    ['a join without a locale', ['join']],
  ])('exits 2 on %s', (_, args) => {
    expect(cli(args).status).toBe(2);
  });

  it('exits 2 on a malformed join request', () => {
    expect(cli(['join', 'fr-CA'], '{"entries": [{"id": 1}]}').status).toBe(2);
  });
});
