// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Evaluation Method list offered by the attribute inspector, and the `$ref`
 * resolution it depends on (GitHub #906).
 *
 * The bug these guard: a property written as a bare `{"$ref": "#/$defs/Address"}`
 * carries no `type`, the type filter compared `undefined` against every method's
 * `validFor` list, and the dropdown came back with ZERO options — no evaluation
 * method could be set on the field from the UI at all.
 *
 * The filter itself is exercised here rather than through the rendered Cloudscape
 * `Select`, whose portalled dropdown does not open in jsdom; `SchemaInspector.test.tsx`
 * covers the rendered side by asserting a stored method is displayed as selected,
 * which only happens when the method survived the filter.
 */

import { describe, expect, it } from 'vitest';
import { availableEvaluationMethods, isStructuredArrayAttribute } from '../utils/evaluationMethods';
import { resolveAttributeType } from '../utils/schemaHelpers';
import {
  EVALUATION_METHOD_OPTIONS,
  EVALUATION_METHOD_DATE,
  EVALUATION_METHOD_EXACT,
  EVALUATION_METHOD_FUZZY,
  EVALUATION_METHOD_HUNGARIAN,
  EVALUATION_METHOD_LEVENSHTEIN,
  EVALUATION_METHOD_LLM,
  EVALUATION_METHOD_NUMERIC_EXACT,
  EVALUATION_METHOD_SEMANTIC,
} from '../../../constants/schemaConstants';

/** A designer class, as `availableClasses` holds it: body nested under `attributes`. */
const designerClass = (name: string, type?: string) => ({
  id: `class-${name}`,
  name,
  attributes: { ...(type ? { type } : {}), properties: {}, required: [] },
});

const methodsFor = (attribute: Record<string, unknown> | null, classes?: Record<string, unknown>[]): string[] =>
  availableEvaluationMethods(attribute, classes as never).map((opt) => opt.value);

const ALL_METHODS = EVALUATION_METHOD_OPTIONS.map((opt) => opt.value);
/**
 * What the never-empty fallback hands back: every method EXCEPT the one that
 * requires structured items. HUNGARIAN passes the filter whenever the field is a
 * structured array, so the fallback is only ever reached when it is not — and
 * offering it there is a silent no-op (`mapper.py` logs its own `ValueError` and
 * drops the method) and contradicts the documented guarantee in
 * `docs/evaluation.md` that HUNGARIAN cannot be chosen for a non-array field.
 */
const FALLBACK_METHODS = EVALUATION_METHOD_OPTIONS.filter((opt) => !opt.requiresStructuredItems).map((opt) => opt.value);
const STRING_METHODS = [
  EVALUATION_METHOD_EXACT,
  EVALUATION_METHOD_NUMERIC_EXACT,
  EVALUATION_METHOD_FUZZY,
  EVALUATION_METHOD_LEVENSHTEIN,
  EVALUATION_METHOD_SEMANTIC,
  EVALUATION_METHOD_DATE,
  EVALUATION_METHOD_LLM,
];

describe('resolveAttributeType', () => {
  it('returns an inline type unchanged', () => {
    expect(resolveAttributeType({ type: 'string' })).toBe('string');
  });

  it('follows a local $ref into the class list and reads the target type', () => {
    // A $ref to a scalar definition: the pointer is followed rather than blanket
    // -assumed to be an object, so the scalar's own type comes back.
    expect(resolveAttributeType({ $ref: '#/$defs/AccountNumber' }, [designerClass('AccountNumber', 'string')])).toBe('string');
  });

  it('treats a present but typeless target as an object', () => {
    // A `$defs` entry with properties and no declared type is an object, and that is how
    // it is written out; the in-memory class does not always carry the type either.
    expect(resolveAttributeType({ $ref: '#/$defs/Address' }, [designerClass('Address')])).toBe('object');
  });

  it('reads the type off an exported-shape class too (type at the top level)', () => {
    expect(resolveAttributeType({ $ref: '#/$defs/Rows' }, [{ name: 'Rows', type: 'array' }])).toBe('array');
  });

  it('prefers a sibling type over the referenced definition, as $ref composition does', () => {
    expect(resolveAttributeType({ type: 'object', $ref: '#/$defs/AccountNumber' }, [designerClass('AccountNumber', 'string')])).toBe(
      'object',
    );
  });

  it('returns undefined when the ref cannot be resolved', () => {
    // Dangling, no class list at all, a remote ref, and a non-$defs pointer.
    expect(resolveAttributeType({ $ref: '#/$defs/Missing' }, [designerClass('Address')])).toBeUndefined();
    expect(resolveAttributeType({ $ref: '#/$defs/Address' })).toBeUndefined();
    expect(resolveAttributeType({ $ref: 'https://example.com/address.json' }, [designerClass('Address')])).toBeUndefined();
    expect(resolveAttributeType({ $ref: '#/definitions/Address' }, [designerClass('Address')])).toBeUndefined();
    expect(resolveAttributeType(null)).toBeUndefined();
    expect(resolveAttributeType({})).toBeUndefined();
  });
});

describe('isStructuredArrayAttribute', () => {
  it('recognizes both inline object items and $ref items', () => {
    expect(isStructuredArrayAttribute({ type: 'array', items: { type: 'object' } })).toBe(true);
    expect(isStructuredArrayAttribute({ type: 'array', items: { $ref: '#/$defs/Row' } })).toBe(true);
  });

  it('is false for simple arrays, non-arrays and a $ref on the property itself', () => {
    expect(isStructuredArrayAttribute({ type: 'array', items: { type: 'string' } })).toBe(false);
    expect(isStructuredArrayAttribute({ type: 'object' })).toBe(false);
    expect(isStructuredArrayAttribute({ $ref: '#/$defs/Address' })).toBe(false);
    expect(isStructuredArrayAttribute(null)).toBe(false);
  });
});

describe('availableEvaluationMethods', () => {
  it('offers Semantic and LLM for a bare $ref to an object definition (#906)', () => {
    // The reported bug: this list used to be empty.
    expect(methodsFor({ $ref: '#/$defs/Address', description: '' }, [designerClass('Address')])).toEqual([
      EVALUATION_METHOD_SEMANTIC,
      EVALUATION_METHOD_LLM,
    ]);
  });

  it('offers the scalar definition methods for a $ref to a scalar', () => {
    const methods = methodsFor({ $ref: '#/$defs/AccountNumber' }, [designerClass('AccountNumber', 'string')]);
    expect(methods).toEqual(STRING_METHODS);
    expect(methods).not.toContain(EVALUATION_METHOD_HUNGARIAN);
  });

  it('falls back to every non-structured-array method — never empty — when the type is unresolvable', () => {
    // Better an over-broad list than a dropdown with nothing in it and no
    // explanation: an empty one removes the only way to configure the field.
    expect(methodsFor({ $ref: '#/$defs/Missing' }, [designerClass('Address')])).toEqual(FALLBACK_METHODS);
    expect(methodsFor({ $ref: '#/$defs/Address' })).toEqual(FALLBACK_METHODS);
    expect(methodsFor({ description: 'no type at all' })).toEqual(FALLBACK_METHODS);
    expect(methodsFor({ type: 'null' })).toEqual(FALLBACK_METHODS);
    expect(methodsFor({ anyOf: [{ type: 'string' }, { type: 'number' }] })).toEqual(FALLBACK_METHODS);
    expect(methodsFor(null)).toEqual(FALLBACK_METHODS);
  });

  it('never offers Hungarian to a field that is not a structured array, fallback included', () => {
    // The fallback must not hand back the ONE method that is structurally invalid
    // for every field that can reach it: mapper.py would discard it silently.
    expect(FALLBACK_METHODS).toHaveLength(ALL_METHODS.length - 1);
    expect(FALLBACK_METHODS).not.toContain(EVALUATION_METHOD_HUNGARIAN);
    expect(methodsFor({ type: 'null' })).not.toContain(EVALUATION_METHOD_HUNGARIAN);
    expect(methodsFor({ $ref: '#/$defs/Missing' }, [designerClass('Address')])).not.toContain(EVALUATION_METHOD_HUNGARIAN);
  });

  it('is unchanged for a property with an inline type', () => {
    expect(methodsFor({ type: 'string' })).toEqual(STRING_METHODS);
    expect(methodsFor({ type: 'number' })).toEqual([EVALUATION_METHOD_EXACT, EVALUATION_METHOD_NUMERIC_EXACT]);
    expect(methodsFor({ type: 'integer' })).toEqual([EVALUATION_METHOD_EXACT, EVALUATION_METHOD_NUMERIC_EXACT]);
    expect(methodsFor({ type: 'boolean' })).toEqual([EVALUATION_METHOD_EXACT]);
    expect(methodsFor({ type: 'object' })).toEqual([EVALUATION_METHOD_SEMANTIC, EVALUATION_METHOD_LLM]);
  });

  it('is unchanged for structured arrays — Hungarian only', () => {
    expect(methodsFor({ type: 'array', items: { type: 'object' } })).toEqual([EVALUATION_METHOD_HUNGARIAN]);
    expect(methodsFor({ type: 'array', items: { $ref: '#/$defs/Row' } })).toEqual([EVALUATION_METHOD_HUNGARIAN]);
  });

  it('is unchanged for simple arrays — judged on the item type, defaulting to string', () => {
    expect(methodsFor({ type: 'array', items: { type: 'string' } })).toEqual(STRING_METHODS);
    expect(methodsFor({ type: 'array', items: { type: 'number' } })).toEqual([EVALUATION_METHOD_EXACT, EVALUATION_METHOD_NUMERIC_EXACT]);
    expect(methodsFor({ type: 'array' })).toEqual(STRING_METHODS);
  });
});
