// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { createIntl } from 'react-intl';
import { describe, expect, it } from 'vitest';
import {
  findPolicyGroupClass,
  groupChangeMessages,
  groupChangeProblem,
  groupChangeVariables,
  policyGroupKey,
  readPolicyUploads,
  uploadGroupVariables,
  uploadHistoryMessages,
} from '../policyGroup';

// The form's copy of the server rules (normalize_policy_group_change and
// check_policy_group_change in rules_discovery.py, tested in
// test_rules_discovery_policy_group_supersede.py).
const groupClass = (group: string, rules: string[], type = 'NCCI_Manual') => ({
  'x-aws-idp-policy-type': type,
  'x-aws-idp-policy-group': group,
  rule_properties: Object.fromEntries(rules.map((r) => [r, { type: 'string', description: `${r}?` }])),
});

describe('policyGroupKey', () => {
  it('ignores case and spacing, as the server does', () => {
    expect(policyGroupKey('  NCCI   Manual ')).toBe(policyGroupKey('ncci manual'));
  });
});

describe('findPolicyGroupClass', () => {
  it("finds the group's class and lists its rule names in order", () => {
    const lookup = findPolicyGroupClass([{ 'x-aws-idp-policy-type': 'other' }, groupClass('NCCI Manual', ['b', 'a'])], ' ncci  MANUAL');
    expect(lookup.status).toBe('found');
    expect(lookup.status === 'found' && lookup.ruleNames).toEqual(['b', 'a']);
  });

  it('reports none for an unknown group, no group, or an unreadable list', () => {
    expect(findPolicyGroupClass([groupClass('A', ['r'])], 'B').status).toBe('none');
    expect(findPolicyGroupClass([groupClass('A', ['r'])], '   ').status).toBe('none');
    expect(findPolicyGroupClass(undefined, 'A').status).toBe('none');
    expect(findPolicyGroupClass('junk', 'A').status).toBe('none');
  });

  it('reports a group carried by two classes as ambiguous, naming both', () => {
    const lookup = findPolicyGroupClass([groupClass('A', ['r'], 'one'), groupClass('a', ['s'], 'two')], 'A');
    expect(lookup).toEqual({ status: 'ambiguous', classNames: ['one', 'two'] });
  });
});

describe('groupChangeProblem', () => {
  const found = findPolicyGroupClass([groupClass('A', ['r'])], 'A');
  const none = findPolicyGroupClass([], 'A');
  const ambiguous = findPolicyGroupClass([groupClass('A', ['r'], 'x'), groupClass('A', ['s'], 'y')], 'A');

  it('accepts an append whether or not the group exists', () => {
    expect(groupChangeProblem('append', found, [])).toBeNull();
    expect(groupChangeProblem('append', none, [])).toBeNull();
  });

  it('refuses a replace or supersede of a group the profile does not hold', () => {
    expect(groupChangeProblem('replace', none, [])).toBe('groupMissing');
    expect(groupChangeProblem('supersede', none, ['r'])).toBe('groupMissing');
  });

  it('refuses a supersede with no rules chosen', () => {
    expect(groupChangeProblem('supersede', found, [])).toBe('noRulesChosen');
    expect(groupChangeProblem('supersede', found, ['r'])).toBeNull();
    expect(groupChangeProblem('replace', found, [])).toBeNull();
  });

  it('refuses every mode for a group two classes carry', () => {
    expect(groupChangeProblem('append', ambiguous, [])).toBe('groupAmbiguous');
  });
});

describe('groupChangeVariables', () => {
  it('sends nothing for an append, so the request reads as before', () => {
    expect(groupChangeVariables('append', ['r'])).toEqual({});
  });

  it('sends the mode, and the rules only for a supersede', () => {
    expect(groupChangeVariables('replace', ['r'])).toEqual({ policyGroupMode: 'replace' });
    expect(groupChangeVariables('supersede', ['r', 's'])).toEqual({ policyGroupMode: 'supersede', supersededRules: ['r', 's'] });
  });
});

describe('readPolicyUploads', () => {
  it('reads the record the server writes and drops malformed entries', () => {
    const entries = readPolicyUploads([
      'junk',
      {
        uploadId: 'u2',
        document: 'rider.pdf',
        savedAt: '2026-11-20T10:00:00+00:00',
        mode: 'supersede',
        rules: ['limit'],
        superseded: [{ rule: 'limit', uploadId: 'u1', document: 'ch1.pdf' }, 'junk'],
      },
      { rules: 'not a list' },
    ]);
    expect(entries).toHaveLength(2);
    expect(entries[0]).toEqual({
      uploadId: 'u2',
      document: 'rider.pdf',
      savedAt: '2026-11-20T10:00:00+00:00',
      mode: 'supersede',
      rules: ['limit'],
      superseded: [{ rule: 'limit', uploadId: 'u1', document: 'ch1.pdf' }],
    });
    expect(entries[1]).toMatchObject({ mode: 'append', rules: [], superseded: [], document: null });
    expect(readPolicyUploads(undefined)).toEqual([]);
  });
});

describe('messages', () => {
  it('render with their values filled in', () => {
    const intl = createIntl({ locale: 'en', messages: {}, onError: () => undefined });
    expect(intl.formatMessage(groupChangeMessages.replaceDescription, { count: 1 })).toContain('all 1 rule in the group');
    expect(intl.formatMessage(groupChangeMessages.replaceDescription, { count: 3 })).toContain('all 3 rules in the group');
    expect(intl.formatMessage(groupChangeMessages.groupAmbiguous, { classNames: 'x, y' })).toContain('(x, y)');
    expect(intl.formatMessage(uploadHistoryMessages.supersededFrom, { rule: 'limit', document: 'ch1.pdf' })).toBe('limit (from ch1.pdf)');
  });
});

describe('uploadGroupVariables', () => {
  it('sends the normalised group with the change for a grouped Policy Discovery upload', () => {
    expect(uploadGroupVariables('rules', '  NCCI   Manual ', { policyGroupMode: 'supersede', supersededRules: ['r'] })).toEqual({
      policyGroup: 'NCCI Manual',
      policyGroupMode: 'supersede',
      supersededRules: ['r'],
    });
    expect(uploadGroupVariables('rules', 'NCCI Manual', {})).toEqual({ policyGroup: 'NCCI Manual' });
  });

  it('sends nothing for an ungrouped upload or a classes discovery, whatever the change', () => {
    expect(uploadGroupVariables('rules', '   ', { policyGroupMode: 'replace' })).toEqual({});
    expect(uploadGroupVariables('classes', 'NCCI Manual', { policyGroupMode: 'replace' })).toEqual({});
  });
});
