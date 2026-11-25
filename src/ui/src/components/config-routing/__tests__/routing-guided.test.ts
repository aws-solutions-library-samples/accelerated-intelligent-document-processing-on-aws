// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The guided form of a condition's test: which kind of control a value has, the tests
 * a control can show (each written back as it was read), and the tests it cannot show,
 * which are edited as an expression.
 */

import { describe, expect, it } from 'vitest';

import {
  NUMBER_OPERATORS,
  formatGuided,
  guidedKind,
  isNumberText,
  parseGuided,
  type GuidedKind,
  type GuidedValue,
} from '../routing-guided';
import { ROUTING_INPUTS, routingInput } from '../routing-types';

describe('a test the guided controls show', () => {
  it.each<[GuidedKind, string, GuidedValue]>([
    ['boolean', 'true', { kind: 'boolean', value: true }],
    ['boolean', 'false', { kind: 'boolean', value: false }],
    ['number', '5', { kind: 'number', op: '=', value: '5' }],
    ['number', '!= 5', { kind: 'number', op: '!=', value: '5' }],
    ['number', '> 0', { kind: 'number', op: '>', value: '0' }],
    ['number', '>= 0.8', { kind: 'number', op: '>=', value: '0.8' }],
    ['number', '< -3', { kind: 'number', op: '<', value: '-3' }],
    ['number', '<= 10.25', { kind: 'number', op: '<=', value: '10.25' }],
    ['number', '[10..20]', { kind: 'between', low: '10', high: '20' }],
    ['number', '[-1.5..2]', { kind: 'between', low: '-1.5', high: '2' }],
    ['choice', '"Invoice"', { kind: 'choice', values: ['Invoice'], negated: false }],
    ['choice', '"Invoice","Receipt"', { kind: 'choice', values: ['Invoice', 'Receipt'], negated: false }],
    ['choice', 'not("W2")', { kind: 'choice', values: ['W2'], negated: true }],
    ['choice', 'not("W2","1099")', { kind: 'choice', values: ['W2', '1099'], negated: true }],
    ['choice', '"say \\"hi\\""', { kind: 'choice', values: ['say "hi"'], negated: false }],
    ['choice', '"C:\\\\temp"', { kind: 'choice', values: ['C:\\temp'], negated: false }],
    ['choice', '"a, b"', { kind: 'choice', values: ['a, b'], negated: false }],
    ['choice', '"not(x)"', { kind: 'choice', values: ['not(x)'], negated: false }],
    ['choice', '""', { kind: 'choice', values: [''], negated: false }],
  ])('of kind %s, %s, is read and written back as it was', (kind, test, value) => {
    expect(parseGuided(test, kind)).toEqual(value);
    expect(formatGuided(value)).toBe(test);
  });

  it.each<[GuidedKind, string, string]>([
    ['boolean', ' true ', 'true'],
    ['number', '>1000', '> 1000'],
    ['number', '= 5', '5'],
    ['number', '  >=   2 ', '>= 2'],
    ['number', '[ 1 .. 2 ]', '[1..2]'],
    ['choice', '"A", "B"', '"A","B"'],
    ['choice', ' "A" , "B" ', '"A","B"'],
    ['choice', 'not ( "A" )', 'not("A")'],
  ])('of kind %s, written as %j, is written in the usual form once a control is edited', (kind, test, usual) => {
    const value = parseGuided(test, kind);

    expect(value).not.toBeNull();
    expect(formatGuided(value!)).toBe(usual);
    // The usual form reads as the same value.
    expect(parseGuided(usual, kind)).toEqual(value);
  });

  it('has every comparison of a number', () => {
    for (const op of NUMBER_OPERATORS) {
      expect(parseGuided(formatGuided({ kind: 'number', op, value: '7' }), 'number')).toEqual({ kind: 'number', op, value: '7' });
    }
  });
});

describe('a test the guided controls cannot show', () => {
  it.each<[GuidedKind, string]>([
    ['boolean', ''],
    ['boolean', '-'],
    ['boolean', 'null'],
    ['boolean', 'True'],
    ['boolean', '"true"'],
    ['boolean', 'true,false'],
    ['boolean', 'not(true)'],
    ['boolean', '1'],
    ['number', ''],
    ['number', '-'],
    ['number', 'null'],
    ['number', '>'],
    ['number', '> x'],
    ['number', '> = 5'],
    ['number', '=='],
    ['number', '1e5'],
    ['number', '.5'],
    ['number', '5.'],
    ['number', '1,000'],
    ['number', '> 1, < 5'],
    ['number', '(1..2)'],
    ['number', '[1..2)'],
    ['number', ']1..2['],
    ['number', '[1..]'],
    ['number', '[1.2]'],
    ['number', 'not(5)'],
    ['number', '"5"'],
    ['number', 'true'],
    ['number', 'date("2026-01-01")'],
    ['choice', ''],
    ['choice', '-'],
    ['choice', 'null'],
    ['choice', 'Invoice'],
    ['choice', '"Invoice'],
    ['choice', '"Invoice",'],
    ['choice', ',"Invoice"'],
    ['choice', '"Invoice" "Receipt"'],
    ['choice', '"A", 5'],
    ['choice', '5'],
    ['choice', '!= "A"'],
    ['choice', 'not()'],
    ['choice', 'not("A"'],
    ['choice', 'not("A"),"B"'],
    ['choice', '"a\\n"'],
    ['choice', '"a\\"'],
    ['choice', '["a".."b"]'],
    ['choice', 'date("2026-01-01")'],
  ])('of kind %s: %j', (kind, test) => {
    expect(parseGuided(test, kind)).toBeNull();
  });
});

describe('a number as the server reads one', () => {
  it.each(['0', '5', '-5', '10.25', '-0.5'])('%s is one', (text) => {
    expect(isNumberText(text)).toBe(true);
  });

  it.each(['', ' ', '5.', '.5', '1e5', '+5', '1,000', 'five', '5 '])('%j is not one', (text) => {
    expect(isNumberText(text)).toBe(false);
  });
});

describe('the kind of control a value has', () => {
  it.each<[string, string, string | undefined, GuidedKind | null]>([
    ['document_class', 'any', undefined, 'choice'],
    ['section_classes', 'any', undefined, 'choice'],
    ['section_classes', 'all', undefined, 'choice'],
    ['class_present', 'any', undefined, 'boolean'],
    ['page_count', 'any', undefined, 'number'],
    ['confidence_alert_count', 'any', undefined, 'number'],
    ['extraction_failed_count', 'any', undefined, 'number'],
    ['route_missing_required_fields', 'any', undefined, 'boolean'],
    ['policy_rule_verdict', 'any', undefined, 'choice'],
    ['failed_policy_rule_count', 'any', undefined, 'number'],
    ['reviewed', 'any', undefined, 'boolean'],
    ['processing_mode', 'any', undefined, 'choice'],
    // A field value has the control of the type of its field.
    ['field_value', 'any', 'number', 'number'],
    ['field_value', 'all', 'integer', 'number'],
    ['field_value', 'any', 'string', 'choice'],
    ['field_value', 'any', 'boolean', 'boolean'],
    // A date, and a field the profile does not describe, are expressions only.
    ['field_value', 'any', 'date', null],
    ['field_value', 'any', 'object', null],
    ['field_value', 'any', undefined, null],
    // The number of values of a list is a number, whatever the values are.
    ['section_classes', 'count', undefined, 'number'],
    ['field_value', 'count', 'string', 'number'],
    ['field_value', 'count', undefined, 'number'],
    // An input that is not a list has no number of values.
    ['document_class', 'count', undefined, 'choice'],
  ])('%s read by %s, field type %s: %s', (name, aggregate, fieldType, kind) => {
    expect(guidedKind(routingInput(name), aggregate, fieldType)).toBe(kind);
  });

  it('is none for an input the catalogue does not have', () => {
    expect(guidedKind(undefined, 'any')).toBeNull();
    expect(guidedKind(routingInput('no_such_input'), 'any', 'number')).toBeNull();
  });

  it('is known for every input of the catalogue but a field value, which depends on its field', () => {
    for (const input of ROUTING_INPUTS) {
      expect(guidedKind(input, 'any') === null, input.name).toBe(input.type === 'field');
    }
  });
});
