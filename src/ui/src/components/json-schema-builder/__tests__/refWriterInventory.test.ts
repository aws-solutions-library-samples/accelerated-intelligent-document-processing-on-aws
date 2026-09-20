// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Every `$ref` the UI writes goes through one helper.
 *
 * A reference to a shared class can be created from at least eight places — the Add
 * Attribute modal, the inspector's object picker and its array-items picker, the `contains`
 * builder, the composition editor (twice), the conditional editor, and the importer's
 * inline-object extraction. Each wrote its own object literal, in three different
 * conventions (an already-built pointer, a bare class name, a template interpolation), and
 * one of them produced `#/$defs/undefined` for an empty selection.
 *
 * Routing them through `refNode` / `refAttributeUpdates` makes them agree, but agreement is
 * not what keeps them agreeing: a comment enumerating the writers went stale inside one
 * release — it named four when there were eight. So the guard is mechanical. This test fails
 * when a `$ref` is written anywhere but the helper module, which is what a ninth writer
 * added later looks like.
 *
 * It deliberately reads source text rather than behaviour. There is no runtime moment at
 * which "a `$ref` was written by a call site that did not use the helper" is observable —
 * the resulting node is identical, which is the whole point — so the only place the property
 * exists is in the source.
 *
 * ## What it looks for, and what it forgives
 *
 * Matching is **per occurrence**, not per line. Checking a whole line against the allowed
 * forms let any line already containing one launder a real write appended to it — and such a
 * line exists today (`SchemaInspector.tsx`'s `{ $ref: undefined, … }`).
 *
 * The pattern also catches the computed form `{ [REF_FIELD]: … }`, because `REF_FIELD` is an
 * exported constant, so the natural tidy-up is also the bypass.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import { describe, expect, it } from 'vitest';

/** Root of the UI sources, resolved from this test's own location. */
const UI_SRC = join(__dirname, '..', '..', '..');

/** The one module allowed to write a `$ref`. */
const HELPER_MODULE = join('components', 'json-schema-builder', 'utils', 'schemaHelpers.ts');

/**
 * `.js` and `.jsx` are included: ESLint lints them, and `aws-exports.js` shows the extension
 * is live in this tree, so excluding them would leave a whole language variant unpoliced.
 */
const SOURCE_EXTENSIONS = /\.(tsx?|jsx?)$/;

/**
 * Not authoring code. Tests fixture their own schemas — including colocated `*.test.tsx`
 * siblings, which is why the exclusion is by filename as well as by directory — and the
 * generated GraphQL modules restate the API's own shapes.
 */
const isExcluded = (relativePath: string): boolean =>
  relativePath.split(sep).includes('__tests__') ||
  /\.(test|spec)\.(tsx?|jsx?)$/.test(relativePath) ||
  relativePath.startsWith(join('graphql', 'generated') + sep);

/**
 * A write of the `$ref` keyword: the literal key, or the computed `[REF_FIELD]` form, in a
 * position where a value follows.
 */
const REF_WRITE = /(?:\$ref|\[\s*REF_FIELD\s*\])\s*:/g;

/**
 * Occurrences that are not writes, tested against the matched occurrence and what follows it
 * rather than against the whole line:
 *   `$ref?: string`     — a type declaration
 *   `$ref: undefined`   — clearing a reference
 *   `$ref: _dropped`    — a destructuring rename, by convention underscore-prefixed
 */
const NON_WRITE = /^(?:\$ref\s*\?\s*:|(?:\$ref|\[\s*REF_FIELD\s*\])\s*:\s*(?:undefined\b|_))/;

/** A line that only talks about `$ref:` — a comment or a log message — is not a write. */
const isProse = (line: string): boolean => /^\s*(?:\/\/|\/\*|\*)/.test(line);

const sourceFiles = (dir: string): string[] =>
  readdirSync(dir).flatMap((entry) => {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      return sourceFiles(full);
    }
    return SOURCE_EXTENSIONS.test(entry) ? [full] : [];
  });

/** Every `$ref` write outside the helper module, as `path:line: text`. */
const refWritesOutsideHelper = (): string[] => {
  const offenders: string[] = [];

  sourceFiles(UI_SRC).forEach((file) => {
    const rel = relative(UI_SRC, file);
    if (rel === HELPER_MODULE || isExcluded(rel)) return;

    readFileSync(file, 'utf8')
      .split('\n')
      .forEach((line, index) => {
        if (isProse(line)) return;
        // Per occurrence: a type declaration earlier in the line must not excuse a write
        // later in it.
        for (const match of line.matchAll(REF_WRITE)) {
          // `?:` is not part of the match, so re-read the keyword plus what follows it.
          const from = line.slice(match.index);
          if (!NON_WRITE.test(from)) {
            offenders.push(`${rel}:${index + 1}: ${line.trim()}`);
            return;
          }
        }
      });
  });

  return offenders;
};

describe('$ref writers', () => {
  it('writes a $ref only from the shared helper module', () => {
    expect(
      refWritesOutsideHelper(),
      `A $ref was written outside ${HELPER_MODULE}. Use refNode() for a subschema value or ` +
        'refAttributeUpdates() for a partial attribute update, so every route writes one shape.',
    ).toEqual([]);
  });

  it('sees the files it is meant to police', () => {
    // A path typo, a moved directory or a broken traversal would make the assertion above
    // pass by scanning nothing. Only the helper is named: pinning any other file would make
    // deleting a dead component break the gate.
    const scanned = sourceFiles(UI_SRC).map((file) => relative(UI_SRC, file));

    expect(scanned.length).toBeGreaterThan(100);
    expect(scanned).toContain(HELPER_MODULE);
    expect(scanned.some((path) => /\.jsx?$/.test(path))).toBe(true);
    expect(scanned.filter((path) => !isExcluded(path)).length).toBeGreaterThan(100);
  });

  /**
   * The evasions that got past a whole-line check. Each is a write the gate must flag, run
   * through the same matcher the scan uses, so the matcher is tested rather than assumed.
   */
  it.each([
    ['a write appended to a line that already clears a $ref', 'onUpdate({ $ref: undefined }); onUpdate({ items: { $ref: v } });'],
    ['the computed key form', 'updates.items = { [REF_FIELD]: pointer };'],
    ['a write after a destructuring rename', 'const { $ref: _drop } = a; b = { $ref: pointer };'],
    ['a plain write', 'onChange({ $ref: `#/$defs/${name}` });'],
  ])('flags %s', (_description, line) => {
    const flagged = [...line.matchAll(REF_WRITE)].some((match) => !NON_WRITE.test(line.slice(match.index)));
    expect(flagged).toBe(true);
  });

  it.each([
    ['a type declaration', '  $ref?: string;'],
    ['clearing a reference', "        onUpdate({ $ref: undefined, type: selectedAttribute.type || 'object' });"],
    ['a destructuring rename', 'const { $ref: _drop, ...siblings } = asObj;'],
  ])('forgives %s', (_description, line) => {
    const flagged = [...line.matchAll(REF_WRITE)].some((match) => !NON_WRITE.test(line.slice(match.index)));
    expect(flagged).toBe(false);
  });
});
