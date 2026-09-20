// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Opening every schema this repository ships in the Schema Builder and saving it changes
 * nothing.
 *
 * The importer and exporter decide, for a `$defs` entry that declares no type, whether to
 * supply `type: "object"`. That decision has to be narrow in one direction — never invent a
 * type for a body that says it is an array, a string or an alias — and conservative in the
 * other, because **113 of the 232 `$defs` entries shipped here are typeless with
 * properties** and every one of them has been written out as an object since the designer
 * existed. Getting that balance wrong silently rewrites a user's configuration the first
 * time they open a class and save.
 *
 * So this measures rather than argues: every `classes:` list in `config_library/**` plus the
 * standard-class catalog is loaded through the real hook and exported.
 *
 *   * Every `$defs` entry keeps the type it declared, or gains `object` if it declared none.
 *     That is the property that makes this change invisible to shipped configurations —
 *     each of them carries only object-applicable keywords, so none takes the other branch.
 *   * Export is idempotent: re-importing the output and exporting again is identical, so
 *     repeated round trips through the editor cannot drift.
 *
 * A config that ever does need a non-object definition will fail the first assertion, which
 * is the point at which the fixture, not the rule, should change.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { renderHook } from '@testing-library/react';
import yaml from 'js-yaml';
import { describe, expect, it } from 'vitest';
import { useSchemaDesigner } from '../useSchemaDesigner';

const REPO_ROOT = join(__dirname, '..', '..', '..', '..', '..');
const CONFIG_LIBRARY = join(REPO_ROOT, 'config_library');
const STANDARD_CLASSES = join(REPO_ROOT, 'src', 'ui', 'src', 'data', 'standard-classes.json');

type Json = Record<string, unknown>;

const yamlFiles = (dir: string): string[] =>
  readdirSync(dir).flatMap((entry) => {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) return yamlFiles(full);
    return /\.ya?ml$/.test(entry) ? [full] : [];
  });

/** Every shipped list of class schemas, as `[label, schemas]`. */
const shippedClassLists = (): [string, Json[]][] => {
  const lists: [string, Json[]][] = [];

  yamlFiles(CONFIG_LIBRARY).forEach((file) => {
    let parsed: unknown;
    try {
      parsed = yaml.load(readFileSync(file, 'utf8'));
    } catch {
      return; // not every YAML in the tree is a configuration
    }
    const classes = (parsed as Json | null)?.classes;
    if (Array.isArray(classes) && classes.length > 0 && classes.every((entry) => entry && typeof entry === 'object')) {
      lists.push([file.slice(REPO_ROOT.length + 1), classes as Json[]]);
    }
  });

  const standard = JSON.parse(readFileSync(STANDARD_CLASSES, 'utf8'));
  const standardClasses = Array.isArray(standard) ? standard : standard.classes;
  lists.push(['src/ui/src/data/standard-classes.json', standardClasses as Json[]]);

  return lists;
};

const exportOnce = (schemas: Json[]): Json[] => {
  const { result } = renderHook(() => useSchemaDesigner(schemas as never));
  return (result.current.exportSchema() ?? []) as unknown as Json[];
};

/** `$defs` entries across a list of exported document-type schemas. */
const defsOf = (schemas: Json[]): [string, Json][] =>
  schemas.flatMap((schema) => Object.entries((schema.$defs ?? {}) as Record<string, Json>));

describe('every shipped schema survives a load and a save', () => {
  const lists = shippedClassLists();

  it('finds the schemas it is meant to check', () => {
    // A moved directory or a parse change would make the cases below vacuous.
    expect(lists.length).toBeGreaterThanOrEqual(10);
    expect(lists.flatMap(([, schemas]) => schemas).length).toBeGreaterThan(100);
    expect(defsOf(lists.flatMap(([, schemas]) => exportOnce(schemas))).length).toBeGreaterThan(100);
  });

  it.each(shippedClassLists())('%s keeps every $defs entry typed as it was', (_label, schemas) => {
    const declaredTypes = new Map<string, unknown>();
    schemas.forEach((schema) => {
      Object.entries((schema.$defs ?? {}) as Record<string, Json>).forEach(([name, body]) => {
        if (!declaredTypes.has(name)) declaredTypes.set(name, body.type);
      });
    });

    defsOf(exportOnce(schemas)).forEach(([name, body]) => {
      const declared = declaredTypes.get(name);
      // A typeless entry gains `object`; a declared type is kept verbatim. Nothing in this
      // tree takes the third branch — a body describing a non-object shape, which is
      // exported typeless — and if one appears, this is where to notice it.
      expect(body.type, `${name} (declared ${String(declared)})`).toBe(declared ?? 'object');
    });
  });

  it.each(shippedClassLists())('%s exports identically on a second pass', (_label, schemas) => {
    const first = exportOnce(schemas);
    const second = exportOnce(first as unknown as Json[]);

    expect(JSON.stringify(second, null, 2)).toBe(JSON.stringify(first, null, 2));
  });
});
