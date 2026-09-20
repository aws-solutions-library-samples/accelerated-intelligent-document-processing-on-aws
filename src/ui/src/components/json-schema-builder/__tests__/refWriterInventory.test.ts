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
 * Three write positions are matched, because an unquoted key literal is not the only way to
 * set one:
 *
 *   `{ $ref: v }`         an object literal
 *   `node.$ref = v`       a property assignment — the form pre-#1024 `SchemaBuilder` used,
 *                         and still the live form for *clearing* a reference, so it is the
 *                         local style a new writer is most likely to copy
 *   `{ [REF_FIELD]: v }`  a computed key, via the exported constant or a string literal
 *
 * And separately, the **string literal** `'$ref'` may not appear in code outside the helper
 * and the constants module. That is what closes the computed-key family for good: a local
 * `const K = '$ref'` followed by `{ [K]: v }` cannot be caught by inspecting the write
 * position, but it cannot be written without the literal. Every such literal in this tree
 * today sits in a comment, and comment lines are skipped.
 *
 * Quoted keys matter because nothing normalises them away: `npm run lint` is eslint only and
 * prettier runs as `format --write` rather than as a check, so `{ "$ref": v }` would survive
 * review formatting.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import { describe, expect, it } from 'vitest';

/** Root of the UI sources, resolved from this test's own location. */
const UI_SRC = join(__dirname, '..', '..', '..');

/** The one module allowed to write a `$ref`. */
const HELPER_MODULE = join('components', 'json-schema-builder', 'utils', 'schemaHelpers.ts');

/** And the one allowed to name the keyword as a string, since `REF_FIELD` is declared there. */
const CONSTANTS_MODULE = join('constants', 'schemaConstants.ts');

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
 * Every position that sets `$ref`. The assignment form excludes `===`, which is a read.
 * `$ref?:` is not matched at all, since `?` is not whitespace.
 */
const WRITE_POSITIONS: RegExp[] = [/\$ref\s*:/g, /\.\$ref\s*=(?!=)/g, /\[\s*(?:REF_FIELD|['"]\$ref['"])\s*\]\s*[:=]/g];

/**
 * Values that make an occurrence a clear rather than a write:
 *   `undefined`         — removing the reference
 *   `_name,` / `_name}` — a destructuring rename, underscore-prefixed by convention. The
 *                         trailing delimiter is what stops `_mk(name)` — a call returning a
 *                         pointer — from being forgiven as one.
 */
const FORGIVEN_VALUE = /^\s*(?:undefined\b|_\w*\s*[,}])/;

/** The string literal, which the computed-key family cannot be written without. */
const REF_LITERAL = /['"]\$ref['"]/;

/** A comment line that only talks about `$ref` is not a write. */
const isProse = (line: string): boolean => /^\s*(?:\/\/|\/\*|\*)/.test(line);

/** Whether one line of code writes a `$ref`, judged per occurrence. */
export const lineWritesRef = (line: string): boolean => {
  if (isProse(line)) return false;
  return WRITE_POSITIONS.some((pattern) =>
    // A clear earlier in the line must not excuse a write later in it, so every occurrence is
    // judged on the value that follows it.
    [...line.matchAll(pattern)].some((match) => !FORGIVEN_VALUE.test(line.slice((match.index ?? 0) + match[0].length))),
  );
};

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
        const namesTheKeyword = rel !== CONSTANTS_MODULE && !isProse(line) && REF_LITERAL.test(line);
        if (lineWritesRef(line) || namesTheKeyword) {
          offenders.push(`${rel}:${index + 1}: ${line.trim()}`);
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
    // pass by scanning nothing. Only the two allowlisted modules are named: pinning any other
    // file would make deleting a dead component break the gate.
    const scanned = sourceFiles(UI_SRC).map((file) => relative(UI_SRC, file));

    expect(scanned.length).toBeGreaterThan(100);
    expect(scanned).toContain(HELPER_MODULE);
    expect(scanned).toContain(CONSTANTS_MODULE);
    expect(scanned.some((path) => /\.jsx?$/.test(path))).toBe(true);
    expect(scanned.filter((path) => !isExcluded(path)).length).toBeGreaterThan(100);
  });

  /**
   * The launderings a whole-line check, or a key-literal-only pattern, let through. Each is a
   * write the gate must flag, run through the same predicate the scan uses.
   */
  it.each([
    ['a write appended to a line that already clears one', 'onUpdate({ $ref: undefined }); onUpdate({ items: { $ref: v } });'],
    ['a single-quoted key', "updates.items = { '$ref': pointer };"],
    ['a double-quoted key', 'updates.items = { "$ref": pointer };'],
    ['a property assignment', 'node.$ref = pointer;'],
    ['a computed key via the exported constant', 'updates.items = { [REF_FIELD]: pointer };'],
    ['a computed key via a string literal', "updates.items = { ['$ref']: pointer };"],
    ['a value from a call, which is not a destructuring rename', 'b = { $ref: _mk(name) };'],
    ['a write after a destructuring rename', 'const { $ref: _drop } = a; b = { $ref: pointer };'],
    ['a plain write', 'onChange({ $ref: `#/$defs/${name}` });'],
  ])('flags %s', (_description, line) => {
    expect(lineWritesRef(line) || REF_LITERAL.test(line)).toBe(true);
  });

  it.each([
    ['an optional type declaration', '  $ref?: string;'],
    ['clearing a reference in an object literal', "        onUpdate({ $ref: undefined, type: selectedAttribute.type || 'object' });"],
    ['clearing a reference by assignment', '    updates.$ref = undefined;'],
    ['comparing a reference', '    if (attrSchema.$ref === `#/$defs/${selectedClass.name}`) {'],
    ['a destructuring rename', 'const { $ref: _drop, ...siblings } = asObj;'],
    ['a comment that names the keyword', '   * A property written as `{"$ref": "#/$defs/Address"}` has no type.'],
  ])('forgives %s', (_description, line) => {
    expect(lineWritesRef(line)).toBe(false);
  });
});
