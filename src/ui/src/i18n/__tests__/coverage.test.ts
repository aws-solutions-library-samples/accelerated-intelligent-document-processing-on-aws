// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The advisory coverage report (decision D17 of #1770). Against a catalog directory it sorts every
 * English id into translated / missing / stale / invalid, lists dead entries, and gives a percentage;
 * its CLI prints that as text or JSON and exits 0 whatever it finds, 2 only when it cannot measure.
 */
import { spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { sourceHash } from '../../../scripts/i18n/catalog.mjs';
import { coverageReport, formatReport, localeCoverage } from '../../../scripts/i18n/coverage.mjs';

const CLI = resolve(__dirname, '..', '..', '..', 'scripts', 'i18n', 'coverage.mjs');

const EN = {
  'nav.documents': { defaultMessage: 'Documents', description: 'Side navigation link' },
  'nav.count': { defaultMessage: '{count, plural, one {# document} other {# documents}}', description: 'Document count' },
  'nav.greeting': { defaultMessage: 'Hello {name}', description: 'Greeting' },
  'nav.upload': { defaultMessage: 'Upload documents', description: 'Upload button' },
};

/** One of each: current, stale (another sourceHash), invalid (argument renamed), dead; `nav.upload` is missing. */
const FR = {
  'nav.documents': { message: 'Documents', sourceHash: sourceHash(EN['nav.documents'].defaultMessage) },
  'nav.count': { message: '{count, plural, one {# document} other {# documents}}', sourceHash: '0123456789abcdef' },
  'nav.greeting': { message: 'Bonjour {nom}', sourceHash: sourceHash(EN['nav.greeting'].defaultMessage) },
  'nav.gone': { message: 'Parti', sourceHash: sourceHash('Gone') },
};

const FR_COVERAGE = {
  translated: 1,
  missing: ['nav.upload'],
  stale: ['nav.count'],
  invalid: ['nav.greeting'],
  problems: [
    'fr-CA.json: nav.greeting: the translation is missing argument(s) {name}',
    'fr-CA.json: nav.greeting: the translation has argument(s) {nom} that the English does not',
  ],
  dead: ['nav.gone'],
  coveragePercent: 25,
};

const run = (...args: string[]) => spawnSync(process.execPath, [CLI, ...args], { encoding: 'utf8' });

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'i18n-coverage-'));
  writeFileSync(join(dir, 'en.json'), JSON.stringify(EN));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

describe('coverageReport', () => {
  it('sorts every English id into one list, counts dead entries, and gives the percentage', () => {
    writeFileSync(join(dir, 'fr-CA.json'), JSON.stringify(FR));
    expect(coverageReport(dir)).toEqual({ sourceKeys: 4, locales: { 'fr-CA': FR_COVERAGE } });
  });

  it('reports the key count and no locales when only en.json exists', () => {
    expect(coverageReport(dir)).toEqual({ sourceKeys: 4, locales: {} });
  });

  it('measures every <locale>.json other than en.json, and nothing else in the directory', () => {
    writeFileSync(join(dir, 'fr-CA.json'), JSON.stringify(FR));
    writeFileSync(join(dir, 'es.json'), JSON.stringify({}));
    writeFileSync(join(dir, 'glossary.json'), JSON.stringify({ not: 'a catalog' }));
    writeFileSync(join(dir, 'README.md'), '# not a catalog');
    const { locales } = coverageReport(dir);
    expect(Object.keys(locales)).toEqual(['es', 'fr-CA']);
    expect(locales.es).toEqual({
      translated: 0,
      missing: ['nav.count', 'nav.documents', 'nav.greeting', 'nav.upload'],
      stale: [],
      invalid: [],
      problems: [],
      dead: [],
      coveragePercent: 0,
    });
  });

  it('restricts itself to one locale on request, and refuses one that has no catalog file', () => {
    writeFileSync(join(dir, 'fr-CA.json'), JSON.stringify(FR));
    writeFileSync(join(dir, 'es.json'), JSON.stringify({}));
    expect(Object.keys(coverageReport(dir, { locale: 'fr-CA' }).locales)).toEqual(['fr-CA']);
    expect(() => coverageReport(dir, { locale: 'de' })).toThrow(/de\.json: no such catalog/);
    expect(() => coverageReport(dir, { locale: 'en-XA' })).toThrow(/pseudo-locale is generated from en\.json at build time/);
    expect(() => coverageReport(dir, { locale: 'en' })).toThrow(/en\.json is the source catalog/);
  });

  it('treats a missing or malformed en.json as a tool error, not a finding', () => {
    expect(() => coverageReport(join(dir, 'absent'))).toThrow(/en\.json: no such catalog/);
    writeFileSync(join(dir, 'en.json'), JSON.stringify({ 'nav.documents': { description: 'no message' } }));
    expect(() => coverageReport(dir)).toThrow(/en\.json: "nav\.documents" has no string defaultMessage/);
    writeFileSync(join(dir, 'en.json'), '{');
    expect(() => coverageReport(dir)).toThrow(SyntaxError);
  });

  it('treats an unreadable translation catalog as a tool error too', () => {
    writeFileSync(join(dir, 'fr-CA.json'), '[]');
    expect(() => coverageReport(dir)).toThrow(/fr-CA\.json: a catalog is a JSON object keyed by message id/);
    expect(() => localeCoverage(EN, [] as unknown as Record<string, unknown>, 'fr-CA')).toThrow(
      /fr-CA\.json is not a JSON object keyed by message id/,
    );
  });
});

describe('localeCoverage', () => {
  it('lists an entry that is both stale and invalid as invalid only: the build rejects it first', () => {
    const fr = { 'nav.greeting': { message: 'Bonjour {nom}', sourceHash: '0123456789abcdef' } };
    const coverage = localeCoverage(EN, fr, 'fr-CA');
    expect(coverage.invalid).toEqual(['nav.greeting']);
    expect(coverage.stale).toEqual([]);
    expect(coverage.translated).toBe(0);
  });

  it('counts a mis-shaped entry as invalid, with the build wording', () => {
    const fr: Record<string, unknown> = {
      'nav.documents': 'Documents',
      'nav.count': { message: '{count, plural, one {# d} other {# d}}' },
    };
    const coverage = localeCoverage(EN, fr, 'fr-CA');
    expect(coverage.invalid).toEqual(['nav.count', 'nav.documents']);
    expect(coverage.problems).toEqual([
      'fr-CA.json: "nav.count" needs a sourceHash of 16 hex characters (sourceHash of the English defaultMessage)',
      'fr-CA.json: "nav.documents" must be { "message": "<ICU>", "sourceHash": "<16 hex>" }',
    ]);
  });

  it('keeps the four lists a partition of the English ids: they sum to the key count', () => {
    const { translated, missing, stale, invalid } = localeCoverage(EN, FR, 'fr-CA');
    expect(translated + missing.length + stale.length + invalid.length).toBe(Object.keys(EN).length);
  });

  it('rounds the percentage to one decimal and gives 0 for an empty source', () => {
    const en = { a: { defaultMessage: 'A' }, b: { defaultMessage: 'B' }, c: { defaultMessage: 'C' } };
    const fr = { a: { message: 'A', sourceHash: sourceHash('A') }, b: { message: 'B', sourceHash: sourceHash('B') } };
    expect(localeCoverage(en, fr, 'fr-CA').coveragePercent).toBe(66.7);
    expect(localeCoverage({}, {}, 'fr-CA').coveragePercent).toBe(0);
  });
});

describe('formatReport', () => {
  it('names the lists with their counts, the ids under them, and the build wording under invalid', () => {
    const text = formatReport({ sourceKeys: 4, locales: { 'fr-CA': FR_COVERAGE } }, dir);
    expect(text).toContain('4 source keys in en.json');
    expect(text).toContain('fr-CA.json: 1 of 4 translated (25%)');
    expect(text).toMatch(/ {2}missing \(1\): .*\n {4}nav\.upload\n/);
    expect(text).toMatch(/ {2}stale \(1\): .*sourceHash.*\n {4}nav\.count\n/);
    expect(text).toMatch(
      / {2}invalid \(1\): .*\n {4}nav\.greeting\n {6}fr-CA\.json: nav\.greeting: the translation is missing argument\(s\) \{name\}\n/,
    );
    expect(text).toMatch(/ {2}dead \(1\): .*\n {4}nav\.gone\n/);
  });

  it('says there is nothing to measure when only en.json exists, naming the directory', () => {
    expect(formatReport({ sourceKeys: 2, locales: {} }, '/x/i18n')).toBe(
      '2 source keys in en.json, no translation catalogs (only en.json is in /x/i18n)\n',
    );
    expect(formatReport({ sourceKeys: 1, locales: {} }, '/x/i18n')).toMatch(/^1 source key in en\.json,/);
  });
});

describe('the CLI', () => {
  it('exits 0 with findings and prints the text report', () => {
    writeFileSync(join(dir, 'fr-CA.json'), JSON.stringify(FR));
    const result = run(dir);
    expect(result.status).toBe(0);
    expect(result.stderr).toBe('');
    expect(result.stdout).toBe(formatReport(coverageReport(dir), dir));
    for (const list of ['missing (1)', 'stale (1)', 'invalid (1)', 'dead (1)']) expect(result.stdout).toContain(list);
  });

  it('prints the report as JSON with --json, and the same report', () => {
    writeFileSync(join(dir, 'fr-CA.json'), JSON.stringify(FR));
    const result = run(dir, '--json');
    expect(result.status).toBe(0);
    expect(JSON.parse(result.stdout)).toEqual(coverageReport(dir));
    expect(JSON.parse(result.stdout)).toEqual({ sourceKeys: 4, locales: { 'fr-CA': FR_COVERAGE } });
  });

  it('restricts the report with --locale, in either output', () => {
    writeFileSync(join(dir, 'fr-CA.json'), JSON.stringify(FR));
    writeFileSync(join(dir, 'es.json'), JSON.stringify({}));
    expect(Object.keys(JSON.parse(run(dir, '--json', '--locale', 'es').stdout).locales)).toEqual(['es']);
    const text = run(dir, '--locale', 'fr-CA').stdout;
    expect(text).toContain('fr-CA.json:');
    expect(text).not.toContain('es.json:');
  });

  it('reports the key count and no catalogs when only en.json exists, still exiting 0', () => {
    const result = run(dir);
    expect(result.status).toBe(0);
    expect(result.stdout).toBe(`4 source keys in en.json, no translation catalogs (only en.json is in ${dir})\n`);
    expect(JSON.parse(run(dir, '--json').stdout)).toEqual({ sourceKeys: 4, locales: {} });
  });

  it('exits 2 on a tool error or a usage error, saying what went wrong on stderr', () => {
    const absent = run(join(dir, 'absent'));
    expect(absent.status).toBe(2);
    expect(absent.stdout).toBe('');
    expect(absent.stderr).toMatch(/en\.json: no such catalog/);

    const unknown = run(dir, '--locale', 'de');
    expect(unknown.status).toBe(2);
    expect(unknown.stderr).toMatch(/de\.json: no such catalog/);

    writeFileSync(join(dir, 'fr-CA.json'), '{');
    expect(run(dir).status).toBe(2);

    for (const argv of [
      [dir, '--bogus'],
      [dir, '--locale'],
      [dir, 'second-positional'],
    ]) {
      const usage = run(...argv);
      expect(usage.status).toBe(2);
      expect(usage.stderr).toMatch(/^usage: node scripts\/i18n\/coverage\.mjs/);
    }
  });
});
