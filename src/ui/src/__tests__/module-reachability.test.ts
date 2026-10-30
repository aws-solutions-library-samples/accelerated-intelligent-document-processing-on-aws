// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Every UI module is reachable from the app's entry.
 *
 * Replaced screens stayed in the tree after their replacements shipped — a Visual
 * Editor dialog nothing opened, a Test Studio landing page nothing routed to — and
 * kept being read, copied and tested as if they were live. This fails on any non-test
 * module under `src/ui/src` that `src/index.tsx` (the entry `src/ui/index.html` loads)
 * does not reach, through any kind of import, and lists them.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import { describe, expect, it } from 'vitest';

import { UI_SRC, buildImportGraph, moduleSpecifiers, resolveSpecifier } from './module-graph';

const ENTRY = join(UI_SRC, 'index.tsx');

/** Not application modules: tests, test helpers, generated code, declarations and the test setup. */
const isExcluded = (rel: string): boolean =>
  rel.split(sep).includes('__tests__') ||
  /\.(test|spec)\.(tsx?|jsx?)$/.test(rel) ||
  rel.endsWith('.d.ts') ||
  rel === 'setupTests.ts' ||
  rel.startsWith(join('graphql', 'generated') + sep);

const sourceFiles = (dir: string): string[] =>
  readdirSync(dir).flatMap((entry) => {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) return sourceFiles(full);
    return /\.(tsx?|jsx?)$/.test(entry) ? [full] : [];
  });

describe('the module graph', () => {
  it('reads every kind of module reference', () => {
    const text = [
      "import a from './a';",
      "import type { B } from './b';",
      "export * from './c';",
      "export { d } from './d';",
      "import './e';",
      "const f = import('./f');",
      "const G = React.lazy(() => import('./g'));",
      "const h = require('./h');",
      "import i from '@/i';",
      "import pkg from 'react';",
    ].join('\n');
    expect(moduleSpecifiers(text)).toEqual(['./a', './b', './c', './d', './e', './f', './g', './h', '@/i', 'react']);
  });

  it('resolves the alias, extensions and directory indexes, and skips assets and packages', () => {
    const from = join(UI_SRC, 'App.tsx');
    expect(resolveSpecifier(from, '@/routes/constants')).toBe(join(UI_SRC, 'routes', 'constants.ts'));
    expect(resolveSpecifier(from, './routes/constants')).toBe(join(UI_SRC, 'routes', 'constants.ts'));
    expect(resolveSpecifier(from, './components/document-details')).toBe(join(UI_SRC, 'components', 'document-details', 'index.ts'));
    expect(resolveSpecifier(from, './App.css')).toBeNull();
    expect(resolveSpecifier(from, './logo.svg?react')).toBeNull();
    expect(resolveSpecifier(from, 'react')).toBeNull();
  });

  it('reaches every UI module from the entry', () => {
    expect(readFileSync(join(UI_SRC, '..', 'index.html'), 'utf-8')).toContain('src="/src/index.tsx"');
    const reachable = buildImportGraph(ENTRY);
    expect(reachable.size).toBeGreaterThan(100);
    const unreachable = sourceFiles(UI_SRC)
      .filter((file) => !isExcluded(relative(UI_SRC, file)))
      .filter((file) => !reachable.has(file))
      .map((file) => relative(UI_SRC, file))
      .sort();
    expect(unreachable, 'delete these, with their tests, or route to them').toEqual([]);
  });
});
