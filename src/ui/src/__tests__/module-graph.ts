// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Test helper: the UI's static import graph, from one entry module. Every kind of
// module reference counts — static and type-only imports, re-exports, side-effect
// imports, `import()` (including `React.lazy`) and `require` — read with TypeScript's
// own pre-processor. Specifiers resolve as Vite does here: the `@/` alias to
// `src/ui/src`, relative paths, the `.ts .tsx .js .jsx` extensions and a directory's
// `index.*`. Non-code targets (styles, images, JSON, `?raw`/`?react` assets) and
// packages are not followed.
import { existsSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import ts from 'typescript';

/** `src/ui/src`. */
export const UI_SRC = join(__dirname, '..');

export const CODE_EXTENSIONS = ['.ts', '.tsx', '.js', '.jsx'] as const;

/** Every module specifier a source text names. */
export const moduleSpecifiers = (text: string): string[] => ts.preProcessFile(text, true, true).importedFiles.map((f) => f.fileName);

const asFile = (base: string): string | null => {
  if (existsSync(base) && statSync(base).isFile()) return CODE_EXTENSIONS.some((e) => base.endsWith(e)) ? base : null;
  for (const ext of CODE_EXTENSIONS) if (existsSync(base + ext)) return base + ext;
  if (existsSync(base) && statSync(base).isDirectory()) {
    for (const ext of CODE_EXTENSIONS) if (existsSync(join(base, `index${ext}`))) return join(base, `index${ext}`);
  }
  return null;
};

/** The module a specifier names from `fromFile`, or null for a package or a non-code target. */
export const resolveSpecifier = (fromFile: string, specifier: string): string | null => {
  const bare = specifier.split('?')[0];
  if (bare !== specifier) return null; // ?raw, ?react, ?url: an asset, not a module
  if (bare.startsWith('@/')) return asFile(join(UI_SRC, bare.slice(2)));
  if (bare.startsWith('./') || bare.startsWith('../')) return asFile(resolve(dirname(fromFile), bare));
  return null;
};

/** Every module reachable from `entry`, the entry included. */
export const buildImportGraph = (entry: string): Set<string> => {
  const seen = new Set<string>();
  const pending = [resolve(entry)];
  while (pending.length > 0) {
    const file = pending.pop() as string;
    if (seen.has(file)) continue;
    seen.add(file);
    for (const specifier of moduleSpecifiers(readFileSync(file, 'utf-8'))) {
      const target = resolveSpecifier(file, specifier);
      if (target && !seen.has(target)) pending.push(target);
    }
  }
  return seen;
};
