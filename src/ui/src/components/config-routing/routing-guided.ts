// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The guided form of a condition's test. A test is stored as DMN unary-test text
// (`> 1000`, `"Invoice","Receipt"`, `[10..20]`); the rule dialog shows the common
// shapes as controls and writes the same text back. `parseGuided` reads a test into
// what a control shows, and answers null for a test the controls cannot show, which is
// then edited as an expression. `formatGuided` writes what a control holds as a test.
//
// The grammar is the server's (idp_common/decision_tables/sfeel.py): a number is
// `-?digits(.digits)?`, and a string is in double quotes with `\"` and `\\` as its only
// escapes. Nothing here decides what is valid: the server parses a saved test again.
import type { RoutingInput } from './routing-types';

/** The kinds of control a condition's value has. */
export type GuidedKind = 'boolean' | 'choice' | 'number';

export type NumberOperator = '=' | '!=' | '>' | '>=' | '<' | '<=';

/** The comparisons a number control offers, in the order it lists them. */
export const NUMBER_OPERATORS: readonly NumberOperator[] = ['=', '!=', '>', '>=', '<', '<='];

export type GuidedValue =
  | { kind: 'boolean'; value: boolean }
  | { kind: 'choice'; values: string[]; negated: boolean }
  | { kind: 'number'; op: NumberOperator; value: string }
  | { kind: 'between'; low: string; high: string };

const NUMBER = String.raw`-?\d+(?:\.\d+)?`;
const NUMBER_PATTERN = new RegExp(`^${NUMBER}$`);
const COMPARISON_PATTERN = new RegExp(String.raw`^(=|!=|>=|<=|>|<)?\s*(${NUMBER})$`);
const BETWEEN_PATTERN = new RegExp(String.raw`^\[\s*(${NUMBER})\s*\.\.\s*(${NUMBER})\s*\]$`);
const NEGATION_PATTERN = /^not\s*\(([\s\S]*)\)$/;
const STRING_PATTERN = /^"((?:[^"\\]|\\["\\])*)"/;

/** True for text the server reads as a number. */
export const isNumberText = (text: string): boolean => NUMBER_PATTERN.test(text);

/**
 * The kind of control the value of a condition has, or null when it is edited as an
 * expression only. `fieldType` is the type of the field a `field_value` condition
 * names (see `fieldType` in routing-paths.ts); a date and an unknown type have no
 * control. A list read by its number of values is a number, whatever the values are.
 */
export const guidedKind = (input: RoutingInput | undefined, aggregate: string, fieldType?: string): GuidedKind | null => {
  if (!input) return null;
  if (input.is_list && aggregate === 'count') return 'number';
  const type = input.type === 'field' ? fieldType : input.type;
  if (type === 'boolean') return 'boolean';
  if (type === 'number' || type === 'integer') return 'number';
  if (type === 'string') return 'choice';
  return null;
};

const parseChoice = (text: string): GuidedValue | null => {
  const negation = NEGATION_PATTERN.exec(text);
  let rest = (negation ? negation[1] : text).trim();
  const values: string[] = [];
  for (;;) {
    const literal = STRING_PATTERN.exec(rest);
    if (!literal) return null;
    values.push(literal[1].replace(/\\(["\\])/g, '$1'));
    rest = rest.slice(literal[0].length).trimStart();
    if (!rest) return { kind: 'choice', values, negated: negation !== null };
    if (!rest.startsWith(',')) return null;
    rest = rest.slice(1).trimStart();
  }
};

/**
 * What the control of a kind shows for a test, or null when the control cannot show
 * it (an empty test, `-`, a date, a mix of tests, a test of another kind).
 */
export const parseGuided = (test: string, kind: GuidedKind): GuidedValue | null => {
  const text = test.trim();
  if (kind === 'boolean') return text === 'true' || text === 'false' ? { kind: 'boolean', value: text === 'true' } : null;
  if (kind === 'choice') return parseChoice(text);
  const comparison = COMPARISON_PATTERN.exec(text);
  if (comparison) return { kind: 'number', op: (comparison[1] ?? '=') as NumberOperator, value: comparison[2] };
  const between = BETWEEN_PATTERN.exec(text);
  return between ? { kind: 'between', low: between[1], high: between[2] } : null;
};

/** A list of tests, negated: the test holds when none of them does. DMN syntax, not text. */
const NEGATED = (list: string): string => `not(${list})`;

/** A string as a literal, escaped as the server's `format_literal` does. */
const formatString = (value: string): string => `"${value.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;

/** The test text for what a control holds. */
export const formatGuided = (value: GuidedValue): string => {
  if (value.kind === 'boolean') return value.value ? 'true' : 'false';
  if (value.kind === 'number') return value.op === '=' ? value.value : `${value.op} ${value.value}`;
  if (value.kind === 'between') return `[${value.low}..${value.high}]`;
  const list = value.values.map(formatString).join(',');
  return value.negated ? NEGATED(list) : list;
};
