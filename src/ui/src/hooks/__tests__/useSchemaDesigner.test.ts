// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Regression tests for useSchemaDesigner.
 *
 * Critical contract: partial updates passed via updateClass / updateAttribute
 * MUST preserve unknown keys on the underlying object. This is what allows
 * users to author extension fields (e.g. `x-aws-idp-page-types`) in the YAML
 * tab and edit other class properties via the form without silently dropping
 * the YAML-only extensions. Several `x-aws-idp-*` fields don't yet have form
 * widgets — if a future refactor switches to spread-from-form-state, this
 * contract would silently break.
 */

import { act, renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { useSchemaDesigner } from '../useSchemaDesigner';

describe('useSchemaDesigner unknown-extension preservation', () => {
  it('updateClass preserves arbitrary x-aws-idp-* keys not touched by the update', () => {
    const { result } = renderHook(() => useSchemaDesigner());

    let classId = '';
    act(() => {
      const cls = result.current.addClass('TestClass');
      classId = cls.id;
    });

    // Seed a class with an unknown extension (as if loaded from YAML).
    act(() => {
      result.current.updateClass(classId, {
        'x-aws-idp-page-types': [{ name: 'AccountSummary' }],
        'x-aws-idp-future-extension': { foo: 'bar' },
      });
    });

    // Now do a partial update that touches a different field. The unknown
    // keys must remain.
    act(() => {
      result.current.updateClass(classId, { description: 'updated' });
    });

    const cls = result.current.classes.find((c) => c.id === classId);
    expect(cls).toBeDefined();
    expect(cls!.description).toBe('updated');
    expect(cls!['x-aws-idp-page-types']).toEqual([{ name: 'AccountSummary' }]);
    expect(cls!['x-aws-idp-future-extension']).toEqual({ foo: 'bar' });
  });

  it('updateAttribute preserves arbitrary x-aws-idp-* keys not touched by the update', () => {
    const { result } = renderHook(() => useSchemaDesigner());

    let classId = '';
    act(() => {
      const cls = result.current.addClass('TestClass');
      classId = cls.id;
    });

    act(() => {
      result.current.addAttribute(classId, 'AccountNumber', 'string');
    });

    // Seed unknown extensions on the attribute.
    act(() => {
      result.current.updateAttribute(classId, 'AccountNumber', {
        'x-aws-idp-source-page-types': ['AccountSummary'],
        'x-aws-idp-future-attr-extension': 42,
      });
    });

    // Touch an unrelated field.
    act(() => {
      result.current.updateAttribute(classId, 'AccountNumber', { description: 'primary id' });
    });

    const cls = result.current.classes.find((c) => c.id === classId);
    const attr = cls?.attributes.properties.AccountNumber;
    expect(attr).toBeDefined();
    expect(attr!.description).toBe('primary id');
    expect(attr!['x-aws-idp-source-page-types']).toEqual(['AccountSummary']);
    expect(attr!['x-aws-idp-future-attr-extension']).toBe(42);
  });

  it('exportSchema preserves x-aws-idp-instance-array on a document class', () => {
    // Regression: the export allow-list is hand-maintained per key, so a
    // class-level extension missing from it is SILENTLY ERASED the first time a
    // user opens and saves that class in the Document Schema editor. The key
    // would work fine via YAML/CLI and then vanish on the next UI edit.
    const { result } = renderHook(() => useSchemaDesigner());

    let classId = '';
    act(() => {
      const cls = result.current.addClass('PatientPacket');
      classId = cls.id;
    });

    act(() => {
      result.current.updateClass(classId, {
        'x-aws-idp-document-type': true,
        'x-aws-idp-instance-array': 'records',
      });
    });

    const exported = result.current.exportSchema();
    expect(exported).not.toBeNull();
    const cls = exported!.find((c) => c['x-aws-idp-instance-array'] !== undefined);
    expect(cls).toBeDefined();
    expect(cls!['x-aws-idp-instance-array']).toBe('records');
  });

  it('exportSchema preserves x-aws-idp-multi-instance on a document class', () => {
    // Same regression as the instance-array test above. There are THREE
    // hand-maintained allow-lists (two import paths + export); missing any one
    // silently erases the flag the first time a user opens and saves the class,
    // and a class that silently loses the flag reverts to extracting ONE record
    // out of N with no error anywhere.
    const { result } = renderHook(() => useSchemaDesigner());

    let classId = '';
    act(() => {
      const cls = result.current.addClass('PayStatement');
      classId = cls.id;
    });

    act(() => {
      result.current.updateClass(classId, {
        'x-aws-idp-document-type': true,
        'x-aws-idp-multi-instance': true,
      });
    });

    // Touch an unrelated field, the way the editor does on any edit.
    act(() => {
      result.current.updateClass(classId, { description: 'a pay statement' });
    });

    const exported = result.current.exportSchema();
    expect(exported).not.toBeNull();
    const cls = exported!.find((c) => c['x-aws-idp-multi-instance'] !== undefined);
    expect(cls).toBeDefined();
    expect(cls!['x-aws-idp-multi-instance']).toBe(true);
  });

  it('exportSchema preserves x-aws-idp-allow-integrated-lists and the evaluation match threshold', () => {
    // The Prompt Preview alert tells the user to set the flag; the Schema
    // Builder must not erase it on the next unrelated edit (it did — the three
    // allow-lists did not know the key, and the same loss hit the evaluation
    // match threshold).
    const { result } = renderHook(() => useSchemaDesigner());

    let classId = '';
    act(() => {
      const cls = result.current.addClass('Invoice');
      classId = cls.id;
    });
    act(() => {
      result.current.updateClass(classId, {
        'x-aws-idp-document-type': true,
        'x-aws-idp-allow-integrated-lists': true,
        'x-aws-idp-evaluation-match-threshold': 0.8,
      });
    });
    act(() => {
      result.current.updateClass(classId, { description: 'an invoice' });
    });

    const exported = result.current.exportSchema();
    expect(exported).not.toBeNull();
    const cls = exported!.find((c) => c.$id === 'Invoice');
    expect(cls).toBeDefined();
    expect(cls!['x-aws-idp-allow-integrated-lists']).toBe(true);
    expect(cls!['x-aws-idp-evaluation-match-threshold']).toBe(0.8);
  });

  it('updateAttribute removes a key when the update value is undefined', () => {
    // Documents the existing semantics so we don't accidentally regress them
    // when changing the preservation behavior above.
    const { result } = renderHook(() => useSchemaDesigner());

    let classId = '';
    act(() => {
      const cls = result.current.addClass('TestClass');
      classId = cls.id;
    });

    act(() => {
      result.current.addAttribute(classId, 'Field', 'string');
      result.current.updateAttribute(classId, 'Field', {
        'x-aws-idp-source-page-types': ['A'],
      });
    });

    act(() => {
      result.current.updateAttribute(classId, 'Field', {
        'x-aws-idp-source-page-types': undefined,
      });
    });

    const attr = result.current.classes.find((c) => c.id === classId)?.attributes.properties.Field;
    expect(attr).toBeDefined();
    expect('x-aws-idp-source-page-types' in attr!).toBe(false);
  });
});

/**
 * Importing a schema with an inline object extracts that object into a shared
 * class and points the original property at it. The property becomes a bare
 * `$ref` — the same shape both reference-picking routes in the UI write, so an
 * imported reference and a hand-built one are indistinguishable afterwards
 * (GitHub #957). `type` belongs to the extracted `$defs` entry; left beside the
 * `$ref` it would be what `resolveAttributeType` reads instead of the pointer.
 */
describe('useSchemaDesigner inline-object extraction', () => {
  const schemaWithInlineObject = {
    $schema: 'https://json-schema.org/draft/2020-12/schema',
    $id: 'Invoice',
    'x-aws-idp-document-type': 'Invoice',
    type: 'object',
    properties: {
      shipsTo: {
        type: 'object',
        description: 'Where the goods go',
        properties: { street: { type: 'string' } },
        required: ['street'],
      },
    },
  };

  it('leaves the extracted reference as a bare $ref, with no sibling type', () => {
    const { result } = renderHook(() => useSchemaDesigner(schemaWithInlineObject));

    const invoice = result.current.classes.find((c) => c.name === 'Invoice');
    expect(invoice).toBeDefined();
    expect(invoice!.attributes.properties.shipsTo).toEqual({
      $ref: '#/$defs/shipsTo',
      description: 'Where the goods go',
    });

    // The shape moved to the extracted class rather than being lost.
    const extracted = result.current.classes.find((c) => c.name === 'shipsTo');
    expect(extracted).toBeDefined();
    expect(extracted!.attributes.properties.street).toEqual({ type: 'string' });
    expect(extracted!.attributes.required).toEqual(['street']);
  });
});

/**
 * A `$defs` entry declares its own type, and it does not have to be `object`.
 * `{"type": "string", "enum": [...]}` is a legal definition, one the backend reads
 * correctly by following the pointer, and one a configuration can be authored with by
 * hand or by CLI. Building every entry as `{type: 'object', properties}` and writing
 * every one back the same way destroyed such a definition the first time its class was
 * opened in the Schema Builder and saved: its type, its enum and any constraint keywords
 * were gone and it was rewritten as an empty object.
 */
describe('useSchemaDesigner $defs definitions that are not objects', () => {
  const STATE_CODE = { type: 'string', enum: ['CA', 'NY'], pattern: '^[A-Z]{2}$' };

  const schemaWithScalarDef = {
    $schema: 'https://json-schema.org/draft/2020-12/schema',
    $id: 'Invoice',
    'x-aws-idp-document-type': 'Invoice',
    type: 'object',
    properties: { state: { $ref: '#/$defs/StateCode' } },
    $defs: { StateCode: STATE_CODE },
  };

  it('round-trips a scalar enum definition through a load and a save', () => {
    const { result } = renderHook(() => useSchemaDesigner(schemaWithScalarDef));

    const exported = result.current.exportSchema();

    expect(exported).toHaveLength(1);
    expect(exported![0].$defs!.StateCode).toEqual(STATE_CODE);
  });

  it('holds the definition on the class, so editing the class does not flatten it', () => {
    const { result } = renderHook(() => useSchemaDesigner(schemaWithScalarDef));

    const stateCode = result.current.classes.find((c) => c.name === 'StateCode');
    expect(stateCode).toBeDefined();
    expect(stateCode!.attributes.type).toBe('string');
    expect(stateCode!.attributes.enum).toEqual(['CA', 'NY']);

    // An unrelated edit to the class still exports the definition intact.
    act(() => {
      result.current.updateClass(stateCode!.id, { description: 'Two-letter state code' });
    });

    expect(result.current.exportSchema()![0].$defs!.StateCode).toEqual({
      ...STATE_CODE,
      description: 'Two-letter state code',
    });
  });

  /**
   * A definition that declares no type but does describe its own shape gets no type
   * invented for it. `{"enum": ["A","B"]}` is such a definition, and `{type: 'object',
   * enum: [...], properties: {}}` matches nothing: the enum's members are strings.
   *
   * The body is a schema node like any other, so it is sanitized: a designer key that a
   * saved schema or a hand edit left on it does not reach `$defs`.
   */
  it('invents no type for a typeless enum definition, and strips designer keys from it', () => {
    const { result } = renderHook(() =>
      useSchemaDesigner({
        ...schemaWithScalarDef,
        properties: { grade: { $ref: '#/$defs/Grade' } },
        $defs: { Grade: { enum: ['A', 'B'], schemaId: 99, id: 'zz' } },
      }),
    );

    expect(result.current.exportSchema()![0].$defs!.Grade).toEqual({ enum: ['A', 'B'] });
  });

  /**
   * An alias — a definition that is only a pointer at another one — keeps the bare `$ref`
   * that every other reference in the schema uses, and the class it points at is emitted
   * into `$defs`. A `$ref` published beside a `type` is the contradiction
   * `refAttributeUpdates` exists to prevent; a `$ref` published with its target missing is
   * a pointer that resolves to nothing, which `config/schema_utils.py` logs as dangling
   * and then hands to the model as an untyped leaf.
   */
  it('keeps an alias definition bare and emits what it points at', () => {
    const { result } = renderHook(() =>
      useSchemaDesigner({
        ...schemaWithScalarDef,
        properties: { holder: { $ref: '#/$defs/HolderAlias' } },
        $defs: {
          HolderAlias: { $ref: '#/$defs/Holder' },
          Holder: { type: 'object', properties: { name: { type: 'string' } } },
        },
      }),
    );

    const defs = result.current.exportSchema()![0].$defs!;
    expect(defs.HolderAlias).toEqual({ $ref: '#/$defs/Holder' });
    expect(defs.Holder).toEqual({ type: 'object', properties: { name: { type: 'string' } } });
  });

  it('emits the item target of an array definition', () => {
    // The reference sits on the definition body's own `items`, not on a property, so a
    // walk seeded only from `properties` never reaches it.
    const { result } = renderHook(() =>
      useSchemaDesigner({
        ...schemaWithScalarDef,
        properties: { rows: { $ref: '#/$defs/LineItems' } },
        $defs: {
          LineItems: { type: 'array', items: { $ref: '#/$defs/LineItem' } },
          LineItem: { type: 'object', properties: { sku: { type: 'string' } } },
        },
      }),
    );

    const defs = result.current.exportSchema()![0].$defs!;
    expect(defs.LineItems).toEqual({ type: 'array', items: { $ref: '#/$defs/LineItem' } });
    expect(Object.keys(defs)).toContain('LineItem');
  });

  it('converts a scalar definition to an object rather than contradicting itself', () => {
    // The designer renders a scalar class as "0 attribute(s)" with a live Add-first-attribute
    // button, so this is reachable from the UI: adding a property to a definition that says
    // `type: 'string'` would otherwise re-create the contradiction from the editor side.
    const { result } = renderHook(() => useSchemaDesigner(schemaWithScalarDef));

    const stateCode = result.current.classes.find((c) => c.name === 'StateCode')!;
    act(() => {
      result.current.addAttribute(stateCode.id, 'line1', 'string');
    });

    expect(result.current.exportSchema()![0].$defs!.StateCode).toEqual({
      type: 'object',
      properties: { line1: { type: 'string', description: '' } },
    });
  });

  it('still writes an object definition as an object, with its properties', () => {
    const { result } = renderHook(() =>
      useSchemaDesigner({
        ...schemaWithScalarDef,
        properties: { shipsTo: { $ref: '#/$defs/Address' } },
        $defs: { Address: { type: 'object', properties: { street: { type: 'string' } }, required: ['street'] } },
      }),
    );

    expect(result.current.exportSchema()![0].$defs!.Address).toEqual({
      type: 'object',
      properties: { street: { type: 'string' } },
      required: ['street'],
    });
  });
});

/**
 * A subschema branch is part of the schema, so the export path has to treat it as one.
 *
 * Sanitization and reference discovery both walked `items` and `properties` and nothing
 * else, so a class a branch pointed at was never emitted into `$defs` and the pointer
 * resolved to nothing, and a designer-internal key written into a branch went out as
 * written. The reachable route is the `contains` builder, covered below;
 * `SchemaCompositionEditor` has no importers, so its `schemaId` cannot be in anyone's
 * configuration — the `oneOf` case is here because the walk should not depend on which
 * editors are wired up.
 */
describe('useSchemaDesigner composition branches', () => {
  const schemaWithComposition = {
    $schema: 'https://json-schema.org/draft/2020-12/schema',
    $id: 'Invoice',
    'x-aws-idp-document-type': 'Invoice',
    type: 'object',
    properties: {
      payer: {
        oneOf: [
          { type: 'string', schemaId: 0 },
          { $ref: '#/$defs/Address', schemaId: 1 },
        ],
      },
    },
    $defs: { Address: { type: 'object', properties: { street: { type: 'string' } } } },
  };

  it('exports the branches without the designer-internal list key', () => {
    const { result } = renderHook(() => useSchemaDesigner(schemaWithComposition));

    const exported = result.current.exportSchema();

    expect(exported![0].properties!.payer).toEqual({
      oneOf: [{ type: 'string' }, { $ref: '#/$defs/Address' }],
    });
  });

  it('emits a class a branch references into $defs', () => {
    const { result } = renderHook(() => useSchemaDesigner(schemaWithComposition));

    const exported = result.current.exportSchema();

    expect(Object.keys(exported![0].$defs || {})).toContain('Address');
  });

  it('emits a class the contains builder references into $defs', () => {
    // The reachable case: `ArrayConstraints` → `ContainsSchemaBuilder` points `contains` at
    // a shared class, and nothing walked `contains`, so the pointer was published with its
    // target missing.
    const { result } = renderHook(() =>
      useSchemaDesigner({
        $schema: 'https://json-schema.org/draft/2020-12/schema',
        $id: 'Invoice',
        'x-aws-idp-document-type': 'Invoice',
        type: 'object',
        properties: { rows: { type: 'array', contains: { $ref: '#/$defs/Paid' } } },
        $defs: { Paid: { type: 'object', properties: { status: { const: 'PAID' } } } },
      }),
    );

    const exported = result.current.exportSchema();

    expect(exported![0].properties!.rows).toEqual({ type: 'array', contains: { $ref: '#/$defs/Paid' } });
    expect(Object.keys(exported![0].$defs || {})).toContain('Paid');
  });
});
