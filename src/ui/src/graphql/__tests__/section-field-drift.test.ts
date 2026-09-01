// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Drift guard for the GraphQL `Section` type (issue #711).
 *
 * Adding a field to `nested/api-resolvers/src/api/schema.graphql` used to change
 * nothing in the UI: the two hand-written Section shapes did not derive from the
 * generated types, the `Sections { … }` selection sets were updated by hand, and
 * nothing failed if either was missed. That is why `InstanceCount` had to be
 * hand-wired into two Section shapes; `Excluded` / `ExclusionReason` had already
 * drifted out of `types/documents.ts`, and the same class of miss once dropped
 * confidence alerts from the historical-version view.
 *
 * Two halves now close that gap:
 *
 *  - Type level: `types/documents.ts` derives `Section` / `EditableSection` from
 *    the generated `Section`, and `SECTION_FIELDS` is a
 *    `Record<keyof GqlSection, true>` — omitting a newly generated field there is
 *    a compile error.
 *
 *  - This test: asserts `SECTION_FIELDS` still matches the generated type
 *    verbatim, and that every `Sections { … }` selection set — in the `.graphql`
 *    sources and in the generated operation strings the app actually ships —
 *    requests all of them. Selection sets are what shape the generated
 *    per-operation types, and no TypeScript type can check them.
 *
 * To see it fail: add a field to `type Section` in schema.graphql, run
 * `npm run codegen`, and this test reports the field as missing from
 * `SECTION_FIELDS` and from every selection set.
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

import { SECTION_FIELD_NAMES } from '../../types/documents';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const GRAPHQL_DIR = path.resolve(HERE, '..');
const SCHEMA_TYPES = path.join(GRAPHQL_DIR, 'generated', 'schema-types.ts');
const GENERATED_OPS = path.join(GRAPHQL_DIR, 'generated', 'index.ts');
const OPERATIONS_DIR = path.join(GRAPHQL_DIR, 'operations');

/** Field names declared on `export type Section = { … }` in the generated types. */
const generatedSectionFields = (): string[] => {
  const src = fs.readFileSync(SCHEMA_TYPES, 'utf8');
  const match = /^export type Section = \{$([\s\S]*?)^\};$/m.exec(src);
  if (!match) {
    throw new Error(`Could not locate "export type Section" in ${SCHEMA_TYPES}`);
  }
  return match[1]
    .split('\n')
    .map((line) => /^\s*([A-Za-z_]\w*)\??:/.exec(line)?.[1])
    .filter((name): name is string => Boolean(name));
};

/**
 * Top-level field names of every `Sections { … }` selection set in a GraphQL
 * document. Walks braces so nested selections (`ConfidenceThresholdAlerts { … }`)
 * are recorded once, by name, without their children leaking in.
 */
const sectionSelectionSets = (text: string): string[][] => {
  const found: string[][] = [];
  const opener = /\bSections\s*\{/g;
  let match: RegExpExecArray | null;
  while ((match = opener.exec(text)) !== null) {
    const fields: string[] = [];
    let depth = 1;
    let token = '';
    let i = match.index + match[0].length;
    while (i < text.length && depth > 0) {
      const ch = text[i];
      if (/[A-Za-z0-9_]/.test(ch)) {
        token += ch;
      } else {
        if (token && depth === 1) fields.push(token);
        token = '';
        if (ch === '{') depth += 1;
        else if (ch === '}') depth -= 1;
      }
      i += 1;
    }
    if (depth !== 0) {
      throw new Error('Unbalanced braces in a Sections selection set');
    }
    found.push(fields);
  }
  return found;
};

/** Every `*.graphql` operation file, relative path -> contents. */
const operationFiles = (): Array<[string, string]> => {
  const walk = (dir: string): string[] =>
    fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) return walk(full);
      return entry.name.endsWith('.graphql') ? [full] : [];
    });
  return walk(OPERATIONS_DIR).map((full) => [path.relative(OPERATIONS_DIR, full), fs.readFileSync(full, 'utf8')]);
};

describe('GraphQL Section field drift', () => {
  it('SECTION_FIELDS mirrors the generated Section type exactly', () => {
    // Fails when schema.graphql gains (or loses) a Section field and the runtime
    // registry in types/documents.ts was not updated with it.
    expect([...SECTION_FIELD_NAMES].sort()).toEqual(generatedSectionFields().sort());
  });

  it('finds the Sections selection sets it is meant to guard', () => {
    // Sanity check on the parser: if the extraction silently matched nothing,
    // every assertion below would pass vacuously.
    const withSections = operationFiles().filter(([, text]) => sectionSelectionSets(text).length > 0);
    expect(withSections.map(([name]) => name).sort()).toEqual([
      'queries/GetDocument.graphql',
      'queries/GetDocumentVersion.graphql',
      'queries/ListDocumentsByDateRange.graphql',
      'subscriptions/OnUpdateDocument.graphql',
    ]);
  });

  it.each(operationFiles().filter(([, text]) => sectionSelectionSets(text).length > 0))(
    'every Section field is requested in %s',
    (_name, text) => {
      for (const selected of sectionSelectionSets(text)) {
        const missing = SECTION_FIELD_NAMES.filter((field) => !selected.includes(field));
        expect(missing).toEqual([]);
      }
    },
  );

  it('every Section field is requested in the generated operation strings', () => {
    // generated/index.ts holds the query text the app actually sends. It is
    // produced from the .graphql sources, so this also fails if codegen was not
    // re-run after editing a selection set.
    const selections = sectionSelectionSets(fs.readFileSync(GENERATED_OPS, 'utf8'));
    expect(selections.length).toBeGreaterThan(0);
    for (const selected of selections) {
      const missing = SECTION_FIELD_NAMES.filter((field) => !selected.includes(field));
      expect(missing).toEqual([]);
    }
  });
});
