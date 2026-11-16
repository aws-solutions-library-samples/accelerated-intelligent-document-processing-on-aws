// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Types of ./vite-plugin-catalogs.mjs for the TypeScript callers (the Vitest suite).
import type { Plugin } from 'vite';

/** The virtual module listing the catalogs: `availableLocales` and `loaders`. */
export declare const CATALOGS_MODULE_ID: 'virtual:i18n-catalogs';
/** Prefix of the per-locale compiled catalog modules, followed by the locale. */
export declare const CATALOG_MODULE_PREFIX: 'virtual:i18n-catalog/';

/** The pseudo-locale a flagged build generates from `en.json`. */
export declare const PSEUDO_LOCALE: 'en-XA';
/** The environment variable that flags a local build when set to `1`. */
export declare const PSEUDO_LOCALE_ENV: 'IDP_UI_PSEUDO_LOCALE';
/** The empty marker file under `src/ui` that flags a build from a source zip. */
export declare const PSEUDO_LOCALE_MARKER: '.pseudo-locale';

/** The locales with a `<locale>.json` catalog file in `dir`, sorted; a missing directory is `[]`. */
export declare const listCatalogLocales: (dir: string) => string[];

/** True when `env[PSEUDO_LOCALE_ENV] === '1'` or `<uiRoot>/<PSEUDO_LOCALE_MARKER>` exists. */
export declare const detectPseudoLocale: (input: { env: Record<string, string | undefined>; uiRoot: string }) => boolean;

export interface I18nCatalogsOptions {
  /** Where the `<locale>.json` files live; relative paths resolve against Vite's `root`. Default `src/i18n`. */
  catalogDir?: string;
  /** Build and list the `en-XA` pseudo-locale. Default: `detectPseudoLocale` over `process.env` and `src/ui`. */
  pseudoLocale?: boolean;
}

/** The plugin. See the module header of vite-plugin-catalogs.mjs for the modules it serves. */
export default function i18nCatalogs(options?: I18nCatalogsOptions): Plugin;
