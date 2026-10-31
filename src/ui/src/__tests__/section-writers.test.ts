// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Only the edit page writes a section's results.
 *
 * A browser-side label writer and three copies of the direct save once lived outside
 * the edit page, each with its own provenance, and two of them wrote labels the server
 * never saw. The guard is mechanical: every module that imports a section-writing
 * operation from the generated client, or the edit page's direct save
 * (`edit-screen/document-edit-save`), must be in the allowlist below with its reason,
 * and every allowlist entry must still import what it is listed for. A new writer, or a
 * writer moved without its entry, fails here.
 *
 * It reads source text: which module may send an operation is a property of the
 * source, not of anything observable at run time. Tests and the generated client are
 * not application code and are skipped.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { describe, expect, it } from 'vitest';

/** Root of the UI sources, resolved from this test's own location. */
const UI_SRC = join(__dirname, '..');

/** The operations that write a section's results or labels. */
const SECTION_WRITERS = [
  'completeSectionReview',
  'saveSectionReviewEdits',
  'saveTestSetDocumentSection',
  'saveReviewDraft',
  'saveTestSetDocumentSectionDraft',
  'uploadDocument',
] as const;
type SectionWriter = (typeof SECTION_WRITERS)[number];

/** Who may import each operation, and why. */
const ALLOWED: Record<string, { operations: SectionWriter[]; reason: string }> = {
  [join('components', 'edit-screen', 'document-edit-save.ts')]: {
    operations: ['uploadDocument', 'saveSectionReviewEdits'],
    reason: 'the edit page’s direct-edit Save',
  },
  [join('components', 'edit-screen', 'test-set-item-edit-adapter.ts')]: {
    operations: ['completeSectionReview', 'saveTestSetDocumentSection', 'saveTestSetDocumentSectionDraft'],
    reason: 'the edit page’s Test Studio item',
  },
  [join('components', 'review-queues', 'review-api.ts')]: {
    operations: ['saveReviewDraft'],
    reason: 'the review API helper the edit page’s Production item calls',
  },
  [join('components', 'pages-panel', 'PageTextEditorModal.tsx')]: {
    operations: ['uploadDocument'],
    reason: 'a page’s text, from the document page’s Edit Page Text dialog, which then reprocesses it',
  },
  [join('components', 'upload-document', 'UploadDocumentPanel.tsx')]: {
    operations: ['uploadDocument'],
    reason: 'document upload, not section data',
  },
};

/** The edit page's direct save, which may be imported only from the edit page. */
const DIRECT_SAVE = join(UI_SRC, 'components', 'edit-screen', 'document-edit-save');
const EDIT_PAGE_DIR = join('components', 'edit-screen') + sep;

const SOURCE_EXTENSIONS = /\.(tsx?|jsx?)$/;
const isExcluded = (relativePath: string): boolean =>
  relativePath.split(sep).includes('__tests__') ||
  /\.(test|spec)\.(tsx?|jsx?)$/.test(relativePath) ||
  relativePath.startsWith(join('graphql', 'generated') + sep);

const sourceFiles = (dir: string): string[] =>
  readdirSync(dir).flatMap((entry) => {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) return sourceFiles(full);
    return SOURCE_EXTENSIONS.test(entry) ? [full] : [];
  });

/** Every `import { … } from '…'` and `export { … } from '…'`: the names and the specifier. */
const NAMED_IMPORTS = /\b(?:import|export)\s+(?:type\s+)?\{([^}]*)\}\s*from\s*['"]([^'"]+)['"]/g;
/** Every module specifier, for the direct-save check (a default or namespace import counts too). */
const SPECIFIERS = /\bfrom\s*['"]([^'"]+)['"]|\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)|\bimport\s*['"]([^'"]+)['"]/g;

export const importedNames = (source: string, fromGenerated: (specifier: string) => boolean): string[] =>
  [...source.matchAll(NAMED_IMPORTS)]
    .filter((m) => fromGenerated(m[2]))
    .flatMap((m) =>
      m[1]
        .split(',')
        .map(
          (n) =>
            n
              .trim()
              .replace(/^type\s+/, '')
              .split(/\s+as\s+/)[0],
        )
        .filter(Boolean),
    );

const isGenerated = (specifier: string): boolean => /(^|\/)graphql\/generated$/.test(specifier) || specifier === '@/graphql/generated';

const resolvesTo = (file: string, specifier: string, target: string): boolean => {
  const resolved = specifier.startsWith('@/') ? join(UI_SRC, specifier.slice(2)) : resolve(dirname(file), specifier);
  return resolved.replace(/\.(tsx?|jsx?)$/, '') === target;
};

/** For every application module, the section-writing operations it imports. */
const writersByFile = (): Map<string, Set<SectionWriter>> => {
  const result = new Map<string, Set<SectionWriter>>();
  for (const file of sourceFiles(UI_SRC)) {
    const rel = relative(UI_SRC, file);
    if (isExcluded(rel)) continue;
    const names = importedNames(readFileSync(file, 'utf-8'), isGenerated).filter((n): n is SectionWriter =>
      (SECTION_WRITERS as readonly string[]).includes(n),
    );
    if (names.length > 0) result.set(rel, new Set(names));
  }
  return result;
};

describe('section writers', () => {
  const found = writersByFile();

  it('sees the files it polices', () => {
    // Guards against a walk or a pattern that matches nothing, which would pass vacuously.
    for (const op of SECTION_WRITERS) {
      expect(
        [...found.values()].some((ops) => ops.has(op)),
        `no module imports ${op}`,
      ).toBe(true);
    }
  });

  it('lets only the allowlisted modules import a section-writing operation', () => {
    const unlisted = [...found.entries()].flatMap(([file, ops]) =>
      [...ops].filter((op) => !ALLOWED[file]?.operations.includes(op)).map((op) => `${file}: ${op}`),
    );
    expect(unlisted, 'add an entry, with its reason, only for a module the edit page relies on').toEqual([]);
  });

  it('has no stale allowlist entry', () => {
    const stale = Object.entries(ALLOWED).flatMap(([file, { operations }]) =>
      operations.filter((op) => !found.get(file)?.has(op)).map((op) => `${file}: ${op}`),
    );
    expect(stale).toEqual([]);
  });

  it('gives every allowlist entry a reason', () => {
    for (const { reason } of Object.values(ALLOWED)) expect(reason.length).toBeGreaterThan(0);
  });

  it('lets only the edit page import its direct save', () => {
    const importers = sourceFiles(UI_SRC)
      .filter((file) => !isExcluded(relative(UI_SRC, file)))
      .filter((file) =>
        [...readFileSync(file, 'utf-8').matchAll(SPECIFIERS)].some((m) => resolvesTo(file, m[1] ?? m[2] ?? m[3], DIRECT_SAVE)),
      )
      .map((file) => relative(UI_SRC, file));
    expect(importers.length).toBeGreaterThan(0);
    expect(importers.filter((f) => !f.startsWith(EDIT_PAGE_DIR))).toEqual([]);
  });

  it('reads named imports, renames and type imports', () => {
    const source = [
      "import { a, completeSectionReview as csr } from '../../graphql/generated';",
      "import type { saveReviewDraft } from '@/graphql/generated';",
      "import { uploadDocument } from './elsewhere';",
    ].join('\n');
    expect(importedNames(source, isGenerated)).toEqual(['a', 'completeSectionReview', 'saveReviewDraft']);
  });
});
