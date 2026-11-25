// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Reading a profile's `routing` section for the tab (the outcome as a list of actions,
 * the reason), the generated input catalogue, and what a condition's path can name in a
 * profile.
 */

import { describe, expect, it } from 'vitest';

import catalog from '../routing-inputs.json';
import { aggregateMessage, inputMessage } from '../RoutingConditionsEditor';
import { classNames, fieldPathsByClass, fieldType, pathsFor, policyRuleIds, policyTypes } from '../routing-paths';
import {
  AGGREGATES,
  MAX_CONDITIONS,
  MAX_REASON_BYTES,
  MAX_REASON_PLACEHOLDERS,
  ROUTING_INPUTS,
  RULE_ID_PATTERN,
  isBuiltIn,
  missingBuiltInRules,
  placeholderInput,
  policyInput,
  queueOf,
  readRouting,
  readsPolicyResult,
  reasonPlaceholders,
  refusedRuleId,
  routingInput,
  routingRefusal,
  tableOf,
  withQueue,
  withRules,
} from '../routing-types';

const SECTION = {
  tables: [
    {
      id: 'review-routing',
      stage: 'after_policy_rule_validation',
      hit_policy: 'FIRST',
      engine: 'builtin',
      rules: [
        {
          id: 'builtin-confidence-alerts',
          label: 'Confidence alerts',
          enabled: true,
          conditions: [{ input: 'confidence_alert_count', path: '', aggregate: 'any', test: '> 0' }],
          actions: [{ type: 'review_queue', queue: 'default' }],
          reason: 'Fields below the confidence threshold: {confidence_alert_count}.',
        },
        { id: 'custom', label: '', enabled: false, conditions: [], actions: [], reason: '' },
      ],
      default_outcome: { actions: [], reason: '' },
    },
  ],
  max_consecutive_placements: 5,
};

describe('readRouting', () => {
  it('reads a section as it is', () => {
    const routing = readRouting(SECTION);

    expect(routing).toEqual(SECTION);
    expect(tableOf(routing).rules.map((rule) => rule.id)).toEqual(['builtin-confidence-alerts', 'custom']);
  });

  it.each([undefined, null, 'x', [], {}, { tables: [] }])('reads %j as one empty table with no review by default', (value) => {
    const routing = readRouting(value);

    expect(routing.tables).toHaveLength(1);
    expect(tableOf(routing)).toMatchObject({
      id: 'review-routing',
      hit_policy: 'FIRST',
      rules: [],
      default_outcome: { actions: [], reason: '' },
    });
    expect(routing.max_consecutive_placements).toBe(3);
  });

  it('fills what a rule leaves out and keeps what it has', () => {
    const routing = readRouting({
      tables: [{ rules: [{ id: 'r', conditions: [{ input: 'page_count', test: '> 1' }], actions: [{ queue: 'q' }] }] }],
    });

    expect(tableOf(routing).rules[0]).toEqual({
      id: 'r',
      label: '',
      enabled: true,
      conditions: [{ input: 'page_count', path: '', aggregate: 'any', test: '> 1' }],
      // An action with no type is a review queue action, as it is on the server.
      actions: [{ type: 'review_queue', queue: 'q' }],
      reason: '',
    });
  });

  it('reads a rule with no actions as not reviewed', () => {
    const routing = readRouting({
      tables: [{ rules: [{ id: 'none' }, { id: 'empty', actions: [] }, { id: 'not-a-list', actions: 'x' }] }],
    });

    expect(tableOf(routing).rules.map((rule) => rule.actions)).toEqual([[], [], []]);
    expect(tableOf(routing).rules.map((rule) => queueOf(rule.actions))).toEqual(['', '', '']);
  });

  it('keeps an action as it was read, whatever its type and keys, so a save loses nothing', () => {
    const actions = [
      { type: 'review_queue', queue: 'claims' },
      { type: 'notify', queue: '', channel: 'ops' },
    ];
    const routing = readRouting({ tables: [{ rules: [{ id: 'r', actions }], default_outcome: { actions, reason: 'Check it.' } }] });

    expect(tableOf(routing).rules[0].actions).toEqual(actions);
    expect(tableOf(routing).default_outcome).toEqual({ actions, reason: 'Check it.' });
  });

  it('reads the reason of a rule and of the default outcome', () => {
    const routing = readRouting({
      tables: [
        {
          rules: [
            { id: 'r', actions: [{ type: 'review_queue', queue: 'q' }], reason: 'Total {field_value:Invoice:Total}' },
            { id: 'n', reason: 7 },
          ],
          default_outcome: { actions: [{ type: 'review_queue', queue: 'default' }], reason: 'No routing rule matched.' },
        },
      ],
    });

    expect(tableOf(routing).rules.map((rule) => rule.reason)).toEqual(['Total {field_value:Invoice:Total}', '']);
    expect(tableOf(routing).default_outcome.reason).toBe('No routing rule matched.');
  });

  it('replaces the rules and nothing else', () => {
    const routing = readRouting(SECTION);
    const next = withRules(routing, []);

    expect(tableOf(next).rules).toEqual([]);
    expect(next.max_consecutive_placements).toBe(5);
    expect(tableOf(next).default_outcome).toEqual(tableOf(routing).default_outcome);
    expect(tableOf(routing).rules).toHaveLength(2);
  });
});

describe('the one queue of an outcome', () => {
  it('is the queue of its review queue action, and none when it has no such action', () => {
    expect(queueOf([{ type: 'review_queue', queue: 'claims' }])).toBe('claims');
    expect(queueOf([])).toBe('');
    expect(queueOf([{ type: 'notify', queue: 'ops' }])).toBe('');
    // The first one, as on the server; a second is refused when the profile is saved.
    expect(
      queueOf([
        { type: 'notify', queue: 'ops' },
        { type: 'review_queue', queue: 'a' },
        { type: 'review_queue', queue: 'b' },
      ]),
    ).toBe('a');
  });

  it('is written as one review queue action, and as no action for no review', () => {
    expect(withQueue('claims')).toEqual([{ type: 'review_queue', queue: 'claims' }]);
    expect(withQueue('')).toEqual([]);
    expect(queueOf(withQueue('claims'))).toBe('claims');
  });
});

describe('the placeholders of a reason', () => {
  it('are what is written in single braces, in order', () => {
    expect(reasonPlaceholders('Total {field_value:Invoice:Total} is over { condition_1.value } ({confidence_threshold}).')).toEqual([
      'field_value:Invoice:Total',
      'condition_1.value',
      'confidence_threshold',
    ]);
  });

  it('are not doubled braces, which are literal', () => {
    expect(reasonPlaceholders('Use {{braces}} around {page_count}')).toEqual(['page_count']);
    expect(reasonPlaceholders('{{{page_count}}}')).toEqual(['page_count']);
    expect(reasonPlaceholders('No value here.')).toEqual([]);
  });

  it('name a routing input, or a condition value or the threshold, which are not inputs', () => {
    expect(placeholderInput('page_count')).toBe('page_count');
    expect(placeholderInput('field_value:Invoice:Total')).toBe('field_value');
    expect(placeholderInput('policy_rule_verdict : prior_auth.coverage')).toBe('policy_rule_verdict');
    expect(placeholderInput('condition_2.value')).toBeUndefined();
    expect(placeholderInput('confidence_threshold')).toBeUndefined();
  });
});

describe('reading a policy rule result', () => {
  const rule = (conditions: { input: string }[], reason = '') => ({
    conditions: conditions.map((condition) => ({ path: '', aggregate: 'any', test: 'x', ...condition })),
    reason,
  });

  it('is what the two policy inputs do', () => {
    expect(ROUTING_INPUTS.filter((input) => policyInput(input.name)).map((input) => input.name)).toEqual([
      'policy_rule_verdict',
      'failed_policy_rule_count',
    ]);
    expect(policyInput('no_such_input')).toBe(false);
  });

  it('is found in a condition and in a placeholder of the reason', () => {
    expect(readsPolicyResult(rule([{ input: 'page_count' }, { input: 'policy_rule_verdict' }]))).toBe(true);
    expect(readsPolicyResult(rule([{ input: 'page_count' }], 'Failed: {failed_policy_rule_count:prior_auth}'))).toBe(true);
    expect(readsPolicyResult(rule([{ input: 'page_count' }], 'Pages: {page_count}, over {condition_1.value}'))).toBe(false);
    // A doubled brace is text.
    expect(readsPolicyResult(rule([], '{{failed_policy_rule_count}}'))).toBe(false);
    expect(readsPolicyResult(rule([]))).toBe(false);
  });
});

describe('the built-in rules', () => {
  it('are the ones whose id starts with builtin-', () => {
    expect(isBuiltIn({ id: 'builtin-confidence-alerts' })).toBe(true);
    expect(isBuiltIn({ id: 'custom' })).toBe(false);
  });

  it('are restored from the system defaults when the table does not have them', () => {
    const defaults = {
      tables: [
        {
          rules: [
            { id: 'builtin-extraction-failed' },
            { id: 'builtin-confidence-alerts' },
            { id: 'builtin-missing-required' },
            { id: 'not-built-in' },
          ],
        },
      ],
    };

    expect(missingBuiltInRules(readRouting(SECTION), defaults).map((rule) => rule.id)).toEqual([
      'builtin-extraction-failed',
      'builtin-missing-required',
    ]);
    expect(missingBuiltInRules(readRouting({}), defaults).map((rule) => rule.id)).toEqual([
      'builtin-extraction-failed',
      'builtin-confidence-alerts',
      'builtin-missing-required',
    ]);
    expect(missingBuiltInRules(readRouting({}), undefined)).toEqual([]);
  });
});

describe('a refusal from the server', () => {
  const REFUSAL = "Routing table 'review-routing', rule 'claims-over-1000', input 'field_value Invoice:Total': 'Invoice' is not a class";

  it('is read out of the error a failed save reports', () => {
    expect(routingRefusal(`Error: ${REFUSAL}`)).toBe(REFUSAL);
    expect(routingRefusal(REFUSAL)).toBe(REFUSAL);
  });

  it.each([
    [null],
    [undefined],
    [''],
    ['Cannot save: Configuration contains validation errors'],
    ['Error: Network error'],
    // Another section's error that only quotes the words is not the routing table's.
    ["Error: classes: the description mentions Routing table 'x'"],
  ])('is not read out of %j', (saveError) => {
    expect(routingRefusal(saveError)).toBeNull();
  });

  it('names the rule it is about, when it is about one', () => {
    expect(refusedRuleId(REFUSAL)).toBe('claims-over-1000');
    expect(refusedRuleId("Routing table 'review-routing': the table has 101 rules; the limit is 100")).toBeUndefined();
    // A rule id quoted later in the message is not the rule the refusal is about.
    expect(refusedRuleId("Routing table 'review-routing': two rules share the id rule 'a'")).toBeUndefined();
    expect(refusedRuleId(null)).toBeUndefined();
  });
});

describe('the input catalogue', () => {
  it('is the generated file, in its order', () => {
    expect(ROUTING_INPUTS.map((input) => input.name)).toEqual(catalog.inputs.map((input) => input.name));
    expect(AGGREGATES).toEqual(catalog.aggregates);
    expect(MAX_CONDITIONS).toBe(catalog.limits.max_conditions);
    expect(MAX_REASON_BYTES).toBe(catalog.limits.max_reason_bytes);
    expect(MAX_REASON_PLACEHOLDERS).toBe(catalog.limits.max_reason_placeholders);
  });

  it('carries the values a guided control offers', () => {
    expect(routingInput('policy_rule_verdict')?.choices).toEqual(['pass', 'fail', 'not_found', 'unknown']);
    expect(routingInput('processing_mode')?.choices).toEqual(['pipeline', 'bda']);
    expect(routingInput('document_class')?.choices).toEqual([]);
  });

  it('has a message for every input and every way of reading a list', () => {
    for (const input of catalog.inputs) expect(inputMessage(input.name), input.name).toBeDefined();
    for (const aggregate of catalog.aggregates) expect(aggregateMessage(aggregate), aggregate).toBeDefined();
    expect(inputMessage('no_such_input')).toBeUndefined();
  });

  it('says which inputs read a path and which read a list', () => {
    expect(routingInput('field_value')).toMatchObject({ needs_path: true, path_kind: 'field' });
    expect(routingInput('section_classes')).toMatchObject({ is_list: true, needs_path: false });
    expect(routingInput('failed_policy_rule_count')).toMatchObject({ path_kind: 'policy_type', path_optional: true });
    expect(routingInput('class_present')).toMatchObject({ type: 'boolean', needs_path: true, path_kind: 'class' });
    expect(routingInput('nope')).toBeUndefined();
  });

  it('accepts and refuses rule ids as the server does', () => {
    for (const id of ['a', 'claims-over-1000', 'builtin-confidence-alerts', 'v1.2_x']) expect(RULE_ID_PATTERN.test(id), id).toBe(true);
    for (const id of ['', 'Claims', '-x', 'a b', 'x'.repeat(65)]) expect(RULE_ID_PATTERN.test(id), id).toBe(false);
  });
});

describe('what a path can name', () => {
  const classes = [
    {
      'x-aws-idp-document-type': 'Invoice',
      type: 'object',
      properties: {
        Total: { type: 'number' },
        Vendor: {
          type: 'object',
          properties: { Name: { type: 'string' }, Address: { type: 'object', properties: { City: { type: 'string' } } } },
        },
        Lines: { type: 'array', items: { type: 'object', properties: { Amount: { type: 'number' } } } },
        Paid: { type: 'boolean' },
        DueDate: { type: 'string', format: 'date' },
        Notes: { type: ['null', 'string'] },
        Pages: { type: 'integer' },
      },
    },
    { 'x-aws-idp-document-type': 'W2', properties: { Wages: { type: 'number' } } },
    { properties: { Orphan: { type: 'string' } } },
    'not a class',
  ];
  const policyClasses = [
    { 'x-aws-idp-policy-type': 'prior_auth', rule_properties: { coverage: {}, medical_necessity: {} } },
    { 'x-aws-idp-rule-type': 'billing', rule_properties: { codes: {} } },
    { rule_properties: { ignored: {} } },
  ];

  it('lists the scalar fields of each class, nested ones by a dotted path, and no list', () => {
    expect(fieldPathsByClass(classes)).toEqual([
      {
        className: 'Invoice',
        paths: [
          'Invoice:Total',
          'Invoice:Vendor.Name',
          'Invoice:Vendor.Address.City',
          'Invoice:Paid',
          'Invoice:DueDate',
          'Invoice:Notes',
          'Invoice:Pages',
        ],
      },
      { className: 'W2', paths: ['W2:Wages'] },
    ]);
  });

  it('lists policy rule ids as type.rule and the policy types, with the older type key', () => {
    expect(policyRuleIds(policyClasses)).toEqual(['prior_auth.coverage', 'prior_auth.medical_necessity', 'billing.codes']);
    expect(policyTypes(policyClasses)).toEqual(['prior_auth', 'billing']);
  });

  it('offers the paths of the kind an input reads', () => {
    expect(pathsFor('field', classes, policyClasses)).toContain('W2:Wages');
    expect(pathsFor('policy_rule', classes, policyClasses)).toEqual(policyRuleIds(policyClasses));
    expect(pathsFor('policy_type', classes, policyClasses)).toEqual(['prior_auth', 'billing']);
    expect(pathsFor('class', classes, policyClasses)).toEqual(['Invoice', 'W2']);
    expect(pathsFor('', classes, policyClasses)).toEqual([]);
  });

  it.each([
    ['Invoice:Total', 'number'],
    ['Invoice:Pages', 'integer'],
    ['Invoice:Vendor.Name', 'string'],
    ['Invoice:Vendor.Address.City', 'string'],
    ['Invoice:Paid', 'boolean'],
    // A string that holds a date is compared as a date.
    ['Invoice:DueDate', 'date'],
    // A field that may be null has the type of its values.
    ['Invoice:Notes', 'string'],
    ['W2:Wages', 'number'],
    ['Invoice:Vendor', 'object'],
    ['Invoice:Lines', 'array'],
  ])('says the type of the field %s is %s', (path, type) => {
    expect(fieldType(classes, path)).toBe(type);
  });

  it.each(['Invoice:Nope', 'Invoice:Lines[0].Amount', 'Nope:Total', 'Invoice', '', 'Invoice:Vendor.Nope'])(
    'knows no type for %j, which the profile does not describe',
    (path) => {
      expect(fieldType(classes, path)).toBeUndefined();
      expect(fieldType(undefined, path)).toBeUndefined();
    },
  );

  it('offers nothing for a profile with no classes', () => {
    expect(fieldPathsByClass(undefined)).toEqual([]);
    expect(classNames(undefined)).toEqual([]);
    expect(classNames([null, 'x', { 'x-aws-idp-document-type': 'W2' }])).toEqual(['W2']);
    expect(policyRuleIds(null)).toEqual([]);
    expect(policyTypes('x')).toEqual([]);
  });
});
