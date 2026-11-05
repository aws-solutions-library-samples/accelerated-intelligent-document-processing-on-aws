// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Types of ./vite-plugin-catalogs.mjs for the TypeScript callers (the Vitest suite).
import type { Plugin } from 'vite';

/** The virtual module listing the catalogs: `availableLocales` and `loaders`. */
export declare const CATALOGS_MODULE_ID: 'virtual:i18n-catalogs';
/** Prefix of the per-locale compiled catalog modules, followed by the locale. */
export declare const CATALOG_MODULE_PREFIX: 'virtual:i18n-catalog/';

/** The locales with a `<locale>.json` catalog file in `dir`, sorted; a missing directory is `[]`. */
export declare const listCatalogLocales: (dir: string) => string[];

export interface I18nCatalogsOptions {
  /** Where the `<locale>.json` files live; relative paths resolve against Vite's `root`. Default `src/i18n`. */
  catalogDir?: string;
}

/** The plugin. See the module header of vite-plugin-catalogs.mjs for the modules it serves. */
export default function i18nCatalogs(options?: I18nCatalogsOptions): Plugin;
