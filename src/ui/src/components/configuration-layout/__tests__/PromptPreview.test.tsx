// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Tests for PromptPreview's schema-walking helper.
 *
 * `getAttributeNamesForClass` is a deliberate port of
 * `ClassificationService._get_attribute_names_for_class`, and the preview's
 * entire purpose is to show what the model will actually receive. So the cases
 * pinned here are the ones where the two implementations could silently drift —
 * every expectation below is the output the Python test suite asserts for the
 * same schema (see
 * `lib/idp_common_pkg/tests/unit/classification/test_class_and_attribute_names_placeholder.py`).
 */

import { describe, it, expect } from 'vitest';

import { getAttributeNamesForClass } from '../PromptPreview';

describe('getAttributeNamesForClass', () => {
  it('walks flat scalars, nested objects and arrays of objects', () => {
    expect(
      getAttributeNamesForClass({
        properties: {
          borrower: {
            type: 'object',
            properties: {
              name: { type: 'string' },
              address: {
                type: 'object',
                properties: { street: { type: 'string' }, zip: { type: 'string' } },
              },
            },
          },
          loan_amount: { type: 'number' },
          findings: { type: 'array', items: { type: 'string' } },
        },
      }),
    ).toEqual(['borrower.name', 'borrower.address.street', 'borrower.address.zip', 'loan_amount', 'findings']);
  });

  it('surfaces $ref group children exactly like an inline group', () => {
    // The schema editor emits every group as {"$ref": "#/$defs/Name"}, which
    // carries no `type`. Before dereferencing, such a group was emitted as a
    // bare leaf here while the backend walked its children — so the preview
    // showed `Signatures` and the real prompt contained the child names.
    expect(
      getAttributeNamesForClass({
        properties: {
          TaxpayerName: { type: 'string' },
          Signatures: { $ref: '#/$defs/Signatures' },
          InlineGroup: { type: 'object', properties: { Child: { type: 'string' } } },
        },
        $defs: {
          Signatures: {
            type: 'object',
            properties: {
              'Signature-of-taxpayer1': { type: 'boolean' },
              'Signature-of-taxpayer2': { type: 'boolean' },
            },
          },
        },
      }),
    ).toEqual(['TaxpayerName', 'Signatures.Signature-of-taxpayer1', 'Signatures.Signature-of-taxpayer2', 'InlineGroup.Child']);
  });

  it('resolves $ref item shapes, $ref array containers and $ref chains', () => {
    expect(
      getAttributeNamesForClass({
        properties: {
          Transactions: { type: 'array', items: { $ref: '#/$defs/Txn' } },
          Fees: { $ref: '#/$defs/FeeList' },
          Holder: { $ref: '#/$defs/HolderAlias' },
        },
        $defs: {
          Txn: { type: 'object', properties: { date: { type: 'string' }, amount: { type: 'number' } } },
          FeeList: { type: 'array', items: { type: 'object', properties: { label: { type: 'string' } } } },
          HolderAlias: { $ref: '#/$defs/Holder' },
          Holder: { type: 'object', properties: { name: { type: 'string' } } },
        },
      }),
    ).toEqual(['Transactions.date', 'Transactions.amount', 'Fees.label', 'Holder.name']);
  });

  it('terminates on a self-recursive $defs definition', () => {
    expect(
      getAttributeNamesForClass({
        properties: { root: { $ref: '#/$defs/Node' } },
        $defs: {
          Node: {
            type: 'object',
            properties: {
              label: { type: 'string' },
              child: { $ref: '#/$defs/Node' },
              kids: { type: 'array', items: { $ref: '#/$defs/Node' } },
            },
          },
        },
      }),
    ).toEqual(['root.label', 'root.child', 'root.kids']);
  });

  it('degrades on dangling, remote and non-object properties instead of throwing', () => {
    expect(
      getAttributeNamesForClass({
        properties: {
          dangling: { $ref: '#/$defs/Nope' },
          remote: { $ref: 'https://example.com/schema.json' },
          notadict: 'oops' as unknown as Record<string, never>,
          plain: { type: 'string' },
        },
        $defs: {},
      }),
    ).toEqual(['dangling', 'remote', 'plain']);
  });

  it('does not read keys off draft-07 tuple-form items', () => {
    expect(
      getAttributeNamesForClass({
        properties: {
          pair: { type: 'array', items: [{ type: 'string' }, { type: 'number' }] as unknown as Record<string, never> },
          plain: { type: 'string' },
        },
      }),
    ).toEqual(['pair', 'plain']);
  });

  it('bounds a combinatorially expanding $defs DAG', () => {
    // active_refs is per-BRANCH, so a non-cyclic DAG is legitimately re-entered
    // on every sibling branch. Unbounded, a ~2 KB schema expands to hundreds of
    // thousands of names; the result-level soft cap never sees it.
    const depth = 12;
    const fanout = 3;
    const defs: Record<string, unknown> = {};
    for (let i = 0; i < depth; i += 1) {
      const props: Record<string, unknown> = { leaf: { type: 'string' } };
      if (i + 1 < depth) {
        for (let k = 0; k < fanout; k += 1) props[`c${k}`] = { $ref: `#/$defs/L${i + 1}` };
      }
      defs[`L${i}`] = { type: 'object', properties: props };
    }

    const names = getAttributeNamesForClass({
      properties: { root: { $ref: '#/$defs/L0' } },
      $defs: defs,
    });

    expect(names.length).toBe(500); // 10 * MAX_ATTRIBUTES_PER_CLASS
  });

  it('returns an empty list for a class with no properties', () => {
    expect(getAttributeNamesForClass({})).toEqual([]);
  });
});
