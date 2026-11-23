// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * A routing table is first-hit, so a rule an earlier enabled rule always wins over can
 * never match. These cases pin when the tab says so.
 */

import { describe, expect, it } from 'vitest';

import { shadowedRules } from '../routing-shadowing';
import type { RoutingCondition, RoutingRule } from '../routing-types';

const condition = (input: string, test: string, path = '', aggregate = 'any'): RoutingCondition => ({ input, path, aggregate, test });
const rule = (id: string, conditions: RoutingCondition[], enabled = true): RoutingRule => ({
  id,
  label: id,
  enabled,
  conditions,
  actions: [{ type: 'review_queue', queue: 'default' }],
  reason: '',
});

const ALERTS = condition('confidence_alert_count', '> 0');
const INVOICE = condition('document_class', '"Invoice"');

describe('shadowedRules', () => {
  it('reports a rule whose conditions include all of an earlier rule', () => {
    expect(shadowedRules([rule('a', [ALERTS]), rule('b', [INVOICE, ALERTS])])).toEqual([{ ruleId: 'b', shadowedBy: 'a' }]);
  });

  it('reports a rule with the same conditions as an earlier one, in any order', () => {
    expect(shadowedRules([rule('a', [ALERTS, INVOICE]), rule('b', [INVOICE, ALERTS])])).toEqual([{ ruleId: 'b', shadowedBy: 'a' }]);
  });

  it('does not report a rule that asks for less than the earlier one', () => {
    expect(shadowedRules([rule('a', [ALERTS, INVOICE]), rule('b', [ALERTS])])).toEqual([]);
  });

  it('does not report rules with different conditions', () => {
    expect(shadowedRules([rule('a', [ALERTS]), rule('b', [INVOICE])])).toEqual([]);
  });

  it('lets a disabled earlier rule shadow nothing', () => {
    expect(shadowedRules([rule('a', [ALERTS], false), rule('b', [ALERTS])])).toEqual([]);
  });

  it('does not report a disabled rule: it does not run', () => {
    expect(shadowedRules([rule('a', [ALERTS]), rule('b', [ALERTS], false)])).toEqual([]);
  });

  it('reports every rule after one with no conditions', () => {
    expect(shadowedRules([rule('all', []), rule('b', [ALERTS]), rule('c', [])])).toEqual([
      { ruleId: 'b', shadowedBy: 'all' },
      { ruleId: 'c', shadowedBy: 'all' },
    ]);
  });

  it('names the first rule that wins', () => {
    expect(shadowedRules([rule('a', [ALERTS]), rule('b', [ALERTS]), rule('c', [ALERTS])])).toEqual([
      { ruleId: 'b', shadowedBy: 'a' },
      { ruleId: 'c', shadowedBy: 'a' },
    ]);
  });

  it('compares the test as written, apart from the space around it', () => {
    expect(shadowedRules([rule('a', [condition('page_count', '> 5')]), rule('b', [condition('page_count', ' > 5 ')])])).toHaveLength(1);
    expect(shadowedRules([rule('a', [condition('page_count', '> 5')]), rule('b', [condition('page_count', '>5')])])).toEqual([]);
  });

  it('tells conditions apart by path and by how a list is read', () => {
    const total = condition('field_value', '> 1000', 'Invoice:Total');
    const tax = condition('field_value', '> 1000', 'Invoice:Tax');
    expect(shadowedRules([rule('a', [total]), rule('b', [tax])])).toEqual([]);
    const any = condition('section_classes', '"W2"', '', 'any');
    const all = condition('section_classes', '"W2"', '', 'all');
    expect(shadowedRules([rule('a', [any]), rule('b', [all])])).toEqual([]);
  });

  it('finds nothing in an empty table', () => {
    expect(shadowedRules([])).toEqual([]);
  });
});
