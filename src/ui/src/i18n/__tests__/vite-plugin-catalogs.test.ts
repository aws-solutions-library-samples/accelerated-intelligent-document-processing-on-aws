// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The Vite plugin's `resolveId`/`load` hooks, driven directly against a temporary catalog
 * directory, so each behaviour the build relies on is pinned without running a build:
 * availability is file presence, `en` is listed but never compiled, a locale compiles to the
 * AST, and a broken catalog throws with the id and the reason.
 */
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { Plugin, ResolvedConfig } from 'vite';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sourceHash } from '../../../scripts/i18n/catalog.mjs';
import i18nCatalogs, {
  CATALOGS_MODULE_ID,
  CATALOG_MODULE_PREFIX,
  listCatalogLocales,
} from '../../../scripts/i18n/vite-plugin-catalogs.mjs';
import { parseMessage } from '../../../scripts/i18n/validate-message.mjs';

const EN = {
  'nav.documents': { defaultMessage: 'Documents', description: 'Side navigation link' },
  'nav.count': { defaultMessage: '{count, plural, one {# document} other {# documents}}', description: 'Document count' },
};

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
