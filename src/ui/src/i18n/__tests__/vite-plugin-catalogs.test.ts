// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The Vite plugin's `resolveId`/`load` hooks, driven directly against a temporary catalog
 * directory, so each behaviour the build relies on is pinned without running a build:
 * availability is file presence, `en` is listed but never compiled, a locale compiles to the
 * AST, and a broken catalog throws with the id and the reason. The pseudo-locale (`en-XA`,
 * decision D21 of #1770) is built from `en.json` by the installed FormatJS CLI only when the
 * plugin is flagged, and a CLI or parity failure on a flagged build is an error, never English.
 */
import { spawnSync, type SpawnSyncReturns } from 'node:child_process';
import { mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { MessageFormatElement } from '@formatjs/icu-messageformat-parser';
import type { Plugin, ResolvedConfig } from 'vite';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { readCatalog, sourceHash } from '../../../scripts/i18n/catalog.mjs';
import i18nCatalogs, {
  CATALOGS_MODULE_ID,
  CATALOG_MODULE_PREFIX,
  PSEUDO_LOCALE,
  PSEUDO_LOCALE_ENV,
  PSEUDO_LOCALE_MARKER,
  detectPseudoLocale,
  listCatalogLocales,
} from '../../../scripts/i18n/vite-plugin-catalogs.mjs';
import { parseMessage, validateMessageAst } from '../../../scripts/i18n/validate-message.mjs';

// The real CLI runs by default; one test at a time replaces a single call to drive a failure.
vi.mock('node:child_process', async (importOriginal) => {
  const actual = await importOriginal<typeof import('node:child_process')>();
  return { ...actual, spawnSync: vi.fn(actual.spawnSync) };
});

const EN = {
  'nav.documents': { defaultMessage: 'Documents', description: 'Side navigation link' },
  'nav.count': { defaultMessage: '{count, plural, one {# document} other {# documents}}', description: 'Document count' },
};

/** The committed English catalog: the flagged build compiles exactly this. */
const REAL_EN_JSON = join(__dirname, '..', 'en.json');

/** Letters outside ASCII: what the pseudo-locale adds and English never has. */
const ACCENTED = /[^ -~]/;

/** Every literal text of an AST, concatenated (branches and tag children included). */
const textOf = (elements: MessageFormatElement[]): string =>
  elements
    .map((element) => {
      if (element.type === 0) return element.value;
      if (element.type === 5 || element.type === 6) {
        return Object.values(element.options)
          .map((option) => textOf(option.value))
          .join('');
      }
      if (element.type === 8) return textOf(element.children);
      return '';
    })
    .join('');

type ResolveId = (id: string) => string | null;
type Load = (this: { addWatchFile: (path: string) => void }, id: string) => string | null;

/** The two hooks as plain functions (Vite also accepts `{ handler }` objects; this plugin uses functions). */
const hooks = (plugin: Plugin): { resolveId: ResolveId; load: Load } => ({
  resolveId: plugin.resolveId as unknown as ResolveId,
  load: plugin.load as unknown as Load,
});

/** `load`, bound to a minimal plugin context, with the module's exports evaluated. */
const evaluate = async (load: Load, id: string, addWatchFile = vi.fn()): Promise<Record<string, unknown>> => {
  const code = load.call({ addWatchFile }, id);
  if (code === null) throw new Error(`load returned null for ${id}`);
  // The generated code is plain ESM; a data: URL import evaluates it without touching disk.
  return import(/* @vite-ignore */ `data:text/javascript,${encodeURIComponent(code)}`);
};

describe('vite-plugin-catalogs', () => {
  let dir: string;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'i18n-plugin-'));
    writeFileSync(join(dir, 'en.json'), JSON.stringify(EN));
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it('resolves exactly the two virtual module shapes, with the \\0 convention, and nothing else', () => {
    const { resolveId } = hooks(i18nCatalogs({ catalogDir: dir }));
    expect(resolveId(CATALOGS_MODULE_ID)).toBe(`\0${CATALOGS_MODULE_ID}`);
    expect(resolveId(`${CATALOG_MODULE_PREFIX}fr-CA`)).toBe(`\0${CATALOG_MODULE_PREFIX}fr-CA`);
    expect(resolveId('react')).toBeNull();
    expect(resolveId('./locales')).toBeNull();
    expect(resolveId('virtual:i18n-catalog')).toBeNull();
  });

  it('lists available locales by file presence, en included, and loads nothing for en', async () => {
    const { resolveId, load } = hooks(i18nCatalogs({ catalogDir: dir }));
    let index = await evaluate(load, resolveId(CATALOGS_MODULE_ID) as string);
    expect(index.availableLocales).toEqual(['en']);
    expect(Object.keys(index.loaders as object)).toEqual([]);

    writeFileSync(join(dir, 'fr-CA.json'), '{}');
    writeFileSync(join(dir, 'en-XA.json'), '{}');
    writeFileSync(join(dir, 'glossary.fr-CA.csv'), 'term,translation');
    writeFileSync(join(dir, 'README.md'), '# not a catalog');
    writeFileSync(join(dir, 'notes.json'), '{}');
    index = await evaluate(load, resolveId(CATALOGS_MODULE_ID) as string);
    expect(index.availableLocales).toEqual(['en', 'en-XA', 'fr-CA']);
    expect(Object.keys(index.loaders as object).sort()).toEqual(['en-XA', 'fr-CA']);
    expect(Object.isFrozen(index.loaders)).toBe(true);
    expect(listCatalogLocales(dir)).toEqual(['en', 'en-XA', 'fr-CA']);
  });

  it('builds each loader as a static import() of that locale’s own module, so each is its own chunk', () => {
    writeFileSync(join(dir, 'fr-CA.json'), '{}');
    const { resolveId, load } = hooks(i18nCatalogs({ catalogDir: dir }));
    const code = load.call({ addWatchFile: vi.fn() }, resolveId(CATALOGS_MODULE_ID) as string) as string;
    expect(code).toContain(`"fr-CA": () => import("${CATALOG_MODULE_PREFIX}fr-CA")`);
    expect(code).not.toContain(`import("${CATALOG_MODULE_PREFIX}en")`);
  });

  it('treats a missing catalog directory as no catalogs at all', async () => {
    const { resolveId, load } = hooks(i18nCatalogs({ catalogDir: join(dir, 'absent') }));
    const index = await evaluate(load, resolveId(CATALOGS_MODULE_ID) as string);
    expect(index.availableLocales).toEqual([]);
    expect(listCatalogLocales(join(dir, 'absent'))).toEqual([]);
  });

  it('compiles a locale catalog to react-intl’s AST, with sourceHash stripped, and watches both files', async () => {
    const fr = {
      'nav.documents': { message: 'Documents', sourceHash: sourceHash(EN['nav.documents'].defaultMessage) },
      'nav.count': {
        message: '{count, plural, one {# document} other {# documents}}',
        sourceHash: sourceHash(EN['nav.count'].defaultMessage),
      },
    };
    writeFileSync(join(dir, 'fr-CA.json'), JSON.stringify(fr));
    const { resolveId, load } = hooks(i18nCatalogs({ catalogDir: dir }));
    const addWatchFile = vi.fn();
    const compiled = await evaluate(load, resolveId(`${CATALOG_MODULE_PREFIX}fr-CA`) as string, addWatchFile);
    expect(compiled.default).toEqual({
      'nav.documents': parseMessage('Documents'),
      'nav.count': parseMessage('{count, plural, one {# document} other {# documents}}'),
    });
    expect(JSON.stringify(compiled.default)).not.toContain('sourceHash');
    expect(addWatchFile.mock.calls.map(([path]) => path).sort()).toEqual([join(dir, 'en.json'), join(dir, 'fr-CA.json')]);
  });

  it('throws on a broken catalog, naming the id and the reason, so the build fails', () => {
    const fr = {
      'nav.documents': { message: 'Documents', sourceHash: sourceHash('Documents') },
      'nav.count': { message: '{count, plural, one {# document}}', sourceHash: sourceHash(EN['nav.count'].defaultMessage) },
      'nav.gone': { message: 'Parti', sourceHash: sourceHash('Gone') },
    };
    writeFileSync(join(dir, 'fr-CA.json'), JSON.stringify(fr));
    const { resolveId, load } = hooks(i18nCatalogs({ catalogDir: dir }));
    const id = resolveId(`${CATALOG_MODULE_PREFIX}fr-CA`) as string;
    let thrown: unknown;
    try {
      load.call({ addWatchFile: vi.fn() }, id);
    } catch (error) {
      thrown = error;
    }
    expect(thrown).toBeInstanceOf(Error);
    const text = String((thrown as Error).message);
    expect(text).toContain('[i18n] fr-CA.json has 2 problem(s), none of which may ship');
    expect(text).toContain('nav.count: the translation does not parse as ICU: MISSING_OTHER_CLAUSE');
    expect(text).toContain('"nav.gone" is not in en.json');
    expect(text).not.toContain('nav.documents');
  });

  it('throws when the catalog file is missing, when it is not JSON, and for en itself', () => {
    const { resolveId, load } = hooks(i18nCatalogs({ catalogDir: dir }));
    const context = { addWatchFile: vi.fn() };
    expect(() => load.call(context, resolveId(`${CATALOG_MODULE_PREFIX}fr-CA`) as string)).toThrow(
      /no catalog .*fr-CA\.json for locale fr-CA/,
    );
    writeFileSync(join(dir, 'fr-CA.json'), '{ not json');
    expect(() => load.call(context, resolveId(`${CATALOG_MODULE_PREFIX}fr-CA`) as string)).toThrow(SyntaxError);
    expect(() => load.call(context, resolveId(`${CATALOG_MODULE_PREFIX}en`) as string)).toThrow(
      /English stays inline and en\.json is never compiled/,
    );
    expect(() => load.call(context, resolveId(`${CATALOG_MODULE_PREFIX}..`) as string)).toThrow(
      /not a locale a catalog file can be named for/,
    );
  });

  it('ignores ids that are not its own', () => {
    const { load } = hooks(i18nCatalogs({ catalogDir: dir }));
    expect(load.call({ addWatchFile: vi.fn() }, '/src/i18n/locales.ts')).toBeNull();
    expect(load.call({ addWatchFile: vi.fn() }, 'virtual:i18n-catalogs')).toBeNull();
  });

  it('resolves a relative catalogDir against Vite’s root once the config is known', async () => {
    // <root>/catalogs/{en,fr-CA}.json; the root itself holds only the en.json from beforeEach.
    const nested = join(dir, 'catalogs');
    mkdirSync(nested);
    writeFileSync(join(nested, 'en.json'), JSON.stringify(EN));
    writeFileSync(join(nested, 'fr-CA.json'), '{}');
    const plugin = i18nCatalogs({ catalogDir: 'catalogs' });
    const configResolved = plugin.configResolved as unknown as (config: Pick<ResolvedConfig, 'root'>) => void;
    configResolved({ root: dir });
    const { resolveId, load } = hooks(plugin);
    const index = await evaluate(load, resolveId(CATALOGS_MODULE_ID) as string);
    expect(index.availableLocales).toEqual(['en', 'fr-CA']);
  });
});

describe('detectPseudoLocale', () => {
  let uiRoot: string;

  beforeEach(() => {
    uiRoot = mkdtempSync(join(tmpdir(), 'i18n-ui-root-'));
  });

  afterEach(() => {
    rmSync(uiRoot, { recursive: true, force: true });
  });

  it('is true when the environment variable is exactly "1"', () => {
    expect(detectPseudoLocale({ env: { [PSEUDO_LOCALE_ENV]: '1' }, uiRoot })).toBe(true);
  });

  it('is false with no variable and no marker', () => {
    expect(detectPseudoLocale({ env: {}, uiRoot })).toBe(false);
    expect(detectPseudoLocale({ env: { [PSEUDO_LOCALE_ENV]: undefined }, uiRoot })).toBe(false);
    expect(detectPseudoLocale({ env: {}, uiRoot: join(uiRoot, 'absent') })).toBe(false);
  });

  it('is true when the empty marker file exists under the UI root', () => {
    writeFileSync(join(uiRoot, PSEUDO_LOCALE_MARKER), '');
    expect(detectPseudoLocale({ env: {}, uiRoot })).toBe(true);
    expect(PSEUDO_LOCALE_MARKER).toBe('.pseudo-locale');
  });

  it.each(['0', 'true', 'yes', '', ' 1'])('treats the variable set to %j as unset, unless the marker is there', (value) => {
    expect(detectPseudoLocale({ env: { [PSEUDO_LOCALE_ENV]: value }, uiRoot })).toBe(false);
    writeFileSync(join(uiRoot, PSEUDO_LOCALE_MARKER), '');
    expect(detectPseudoLocale({ env: { [PSEUDO_LOCALE_ENV]: value }, uiRoot })).toBe(true);
  });
});

describe('vite-plugin-catalogs: the pseudo-locale', () => {
  let dir: string;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'i18n-pseudo-'));
    writeFileSync(join(dir, 'en.json'), JSON.stringify(EN));
    vi.mocked(spawnSync).mockClear();
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it('lists en-XA with a loader of its own when flagged, and writes no file into the catalog directory', async () => {
    const { resolveId, load } = hooks(i18nCatalogs({ catalogDir: dir, pseudoLocale: true }));
    const code = load.call({ addWatchFile: vi.fn() }, resolveId(CATALOGS_MODULE_ID) as string) as string;
    const index = await evaluate(load, resolveId(CATALOGS_MODULE_ID) as string);
    expect(index.availableLocales).toEqual(['en', PSEUDO_LOCALE]);
    expect(Object.keys(index.loaders as object)).toEqual([PSEUDO_LOCALE]);
    expect(code).toContain(`"${PSEUDO_LOCALE}": () => import("${CATALOG_MODULE_PREFIX}${PSEUDO_LOCALE}")`);
    expect(readdirSync(dir)).toEqual(['en.json']);
  });

  it('lists en-XA once when a catalog file of that name also exists', async () => {
    writeFileSync(join(dir, `${PSEUDO_LOCALE}.json`), '{}');
    const { resolveId, load } = hooks(i18nCatalogs({ catalogDir: dir, pseudoLocale: true }));
    const index = await evaluate(load, resolveId(CATALOGS_MODULE_ID) as string);
    expect(index.availableLocales).toEqual(['en', PSEUDO_LOCALE]);
  });

  it('serves en.json run through the CLI: accented text, every argument and plural kept, watching en.json', async () => {
    const { resolveId, load } = hooks(i18nCatalogs({ catalogDir: dir, pseudoLocale: true }));
    const addWatchFile = vi.fn();
    const served = await evaluate(load, resolveId(`${CATALOG_MODULE_PREFIX}${PSEUDO_LOCALE}`) as string, addWatchFile);
    const table = served.default as Record<string, MessageFormatElement[]>;
    expect(Object.keys(table).sort()).toEqual(['nav.count', 'nav.documents']);
    for (const [id, ast] of Object.entries(table)) {
      const english = EN[id as keyof typeof EN].defaultMessage;
      expect(validateMessageAst(id, english, ast)).toEqual({ ok: true, errors: [] });
      expect(textOf(ast)).toMatch(ACCENTED);
      expect(textOf(ast)).not.toContain(english);
    }
    // `[` and `]` padding around the message, `#` still inside the plural, `count` still a plural.
    expect(textOf(table['nav.documents'])).toMatch(/^\[.+]$/);
    const plural = table['nav.count'].find((element) => element.type === 6);
    expect(plural).toBeDefined();
    if (plural?.type !== 6) throw new Error('unreachable');
    expect(plural.value).toBe('count');
    expect(Object.keys(plural.options)).toEqual(['one', 'other']);
    expect(plural.options.other.value[0].type).toBe(7);
    expect(JSON.stringify(table)).not.toContain('sourceHash');
    expect(addWatchFile.mock.calls.map(([path]) => path)).toEqual([join(dir, 'en.json')]);
    expect(readdirSync(dir)).toEqual(['en.json']);

    // Run by path out of src/ui/node_modules with this Node, writing to a temporary file.
    expect(spawnSync).toHaveBeenCalledTimes(1);
    const [command, args] = vi.mocked(spawnSync).mock.calls[0] as [string, string[]];
    expect(command).toBe(process.execPath);
    expect(args[0]).toMatch(/[/\\]node_modules[/\\]\.bin[/\\]formatjs$/);
    expect(args.slice(1, 6)).toEqual(['compile', join(dir, 'en.json'), '--ast', '--pseudo-locale', PSEUDO_LOCALE]);
    expect(args[6]).toBe('--out-file');
    expect(args[7]).not.toContain(dir);
  });

  it('builds the committed en.json: the switcher’s messages keep {language} and lose their English', async () => {
    const real = join(dir, 'real');
    mkdirSync(real);
    writeFileSync(join(real, 'en.json'), JSON.stringify(readCatalog(REAL_EN_JSON)));
    const { resolveId, load } = hooks(i18nCatalogs({ catalogDir: real, pseudoLocale: true }));
    const served = await evaluate(load, resolveId(`${CATALOG_MODULE_PREFIX}${PSEUDO_LOCALE}`) as string);
    const table = served.default as Record<string, MessageFormatElement[]>;
    expect(Object.keys(table).sort()).toEqual(Object.keys(readCatalog(REAL_EN_JSON)).sort());
    const ariaLabel = table['i18n.switcher.ariaLabel'];
    expect(ariaLabel.some((element) => element.type === 1 && element.value === 'language')).toBe(true);
    expect(textOf(ariaLabel)).toMatch(ACCENTED);
    expect(textOf(ariaLabel)).not.toContain('Language');
    expect(textOf(table['i18n.switcher.label'])).not.toBe('Language');
  });

  it('fails the build when the CLI exits non-zero, quoting its output', () => {
    vi.mocked(spawnSync).mockReturnValueOnce({
      status: 1,
      signal: null,
      pid: 0,
      output: [],
      stdout: '',
      stderr: 'Error: SyntaxError: MISSING_OTHER_CLAUSE at line 1, column 26: {count, plural, one {# x}',
    } as SpawnSyncReturns<string>);
    const { resolveId, load } = hooks(i18nCatalogs({ catalogDir: dir, pseudoLocale: true }));
    expect(() => load.call({ addWatchFile: vi.fn() }, resolveId(`${CATALOG_MODULE_PREFIX}${PSEUDO_LOCALE}`) as string)).toThrow(
      /\[i18n\] cannot build the en-XA pseudo-locale: formatjs compile exited with 1\nError: SyntaxError: MISSING_OTHER_CLAUSE/,
    );
    expect(readdirSync(dir)).toEqual(['en.json']);
  });

  it('fails the build when the CLI cannot be started, naming the cause', () => {
    vi.mocked(spawnSync).mockReturnValueOnce({
      error: Object.assign(new Error('spawnSync node ENOENT'), { code: 'ENOENT' }),
      status: null,
      signal: null,
      pid: 0,
      output: [],
      stdout: '',
      stderr: '',
    } as SpawnSyncReturns<string>);
    const { resolveId, load } = hooks(i18nCatalogs({ catalogDir: dir, pseudoLocale: true }));
    expect(() => load.call({ addWatchFile: vi.fn() }, resolveId(`${CATALOG_MODULE_PREFIX}${PSEUDO_LOCALE}`) as string)).toThrow(
      /cannot build the en-XA pseudo-locale: formatjs compile could not run: spawnSync node ENOENT/,
    );
  });

  it('fails the build when the CLI’s output drops a message or breaks parity, instead of shipping English for it', () => {
    const outFileOf = (call: unknown[]): string => (call[1] as string[])[7];
    // Drops `nav.count`.
    vi.mocked(spawnSync).mockImplementationOnce((...call) => {
      writeFileSync(outFileOf(call), JSON.stringify({ 'nav.documents': parseMessage('[Ḓǿǿƈŭŭḿḗḗƞŧş]') }));
      return { status: 0, signal: null, pid: 0, output: [], stdout: '', stderr: '' } as SpawnSyncReturns<string>;
    });
    const { resolveId, load } = hooks(i18nCatalogs({ catalogDir: dir, pseudoLocale: true }));
    const id = resolveId(`${CATALOG_MODULE_PREFIX}${PSEUDO_LOCALE}`) as string;
    expect(() => load.call({ addWatchFile: vi.fn() }, id)).toThrow(
      /the en-XA pseudo-locale has 1 problem\(s\), none of which may ship:\n {2}- "nav\.count" is in en\.json but formatjs compile produced no en-XA message for it/,
    );

    // Flattens the plural and invents a message.
    vi.mocked(spawnSync).mockImplementationOnce((...call) => {
      writeFileSync(
        outFileOf(call),
        JSON.stringify({
          'nav.documents': parseMessage('[Ḓǿǿƈŭŭḿḗḗƞŧş]'),
          'nav.count': parseMessage('[{count} ḓǿǿƈŭŭḿḗḗƞŧş]'),
          'nav.extra': parseMessage('[ḗḗẋŧřȧȧ]'),
        }),
      );
      return { status: 0, signal: null, pid: 0, output: [], stdout: '', stderr: '' } as SpawnSyncReturns<string>;
    });
    let thrown: unknown;
    try {
      load.call({ addWatchFile: vi.fn() }, id);
    } catch (error) {
      thrown = error;
    }
    const text = String((thrown as Error).message);
    expect(text).toContain('the en-XA pseudo-locale has 2 problem(s)');
    expect(text).toContain('nav.count: argument {count} is a plural in the English and a plain argument in the translation');
    expect(text).toContain('formatjs compile produced "nav.extra", which is not in en.json');
    expect(text).not.toContain('nav.documents');
  });

  it('refuses a hand-written en-XA.json while flagged, rather than silently shadowing one with the other', () => {
    writeFileSync(join(dir, `${PSEUDO_LOCALE}.json`), '{}');
    const { resolveId, load } = hooks(i18nCatalogs({ catalogDir: dir, pseudoLocale: true }));
    expect(() => load.call({ addWatchFile: vi.fn() }, resolveId(`${CATALOG_MODULE_PREFIX}${PSEUDO_LOCALE}`) as string)).toThrow(
      /en-XA\.json exists while the build is flagged for the pseudo-locale/,
    );
    expect(spawnSync).not.toHaveBeenCalled();
  });

  it('emits nothing of it when not flagged: no locale, no loader, no module, no CLI run', async () => {
    const { resolveId, load } = hooks(i18nCatalogs({ catalogDir: dir, pseudoLocale: false }));
    const index = await evaluate(load, resolveId(CATALOGS_MODULE_ID) as string);
    expect(index.availableLocales).toEqual(['en']);
    expect(Object.keys(index.loaders as object)).toEqual([]);
    expect(() => load.call({ addWatchFile: vi.fn() }, resolveId(`${CATALOG_MODULE_PREFIX}${PSEUDO_LOCALE}`) as string)).toThrow(
      /no catalog .*en-XA\.json for locale en-XA/,
    );
    expect(spawnSync).not.toHaveBeenCalled();
  });
});
