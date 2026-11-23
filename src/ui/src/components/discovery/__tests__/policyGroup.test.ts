// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { createIntl } from 'react-intl';
import { describe, expect, it } from 'vitest';
import { isGroupWithReplace, normalizePolicyGroup, policyGroupMessages, policyGroupProblem, POLICY_GROUP_MAX_LENGTH } from '../policyGroup';

// The form's copy of the server rule (normalize_policy_group in rules_discovery.py).
// Each case below is one the server decides the same way; the Python side is
// tested in test_rules_discovery_policy_group.py.
describe('policyGroupProblem', () => {
  it('treats empty and whitespace as no group', () => {
    expect(policyGroupProblem('')).toBeNull();
    expect(policyGroupProblem('   ')).toBeNull();
  });

  it('accepts letters, digits, spaces, underscores, hyphens and dots', () => {
    expect(policyGroupProblem('NCCI Policy Manual v2.1_final-draft')).toBeNull();
  });

  it('measures the length after normalising whitespace', () => {
    const name = 'a'.repeat(POLICY_GROUP_MAX_LENGTH);
    expect(policyGroupProblem(`  ${name}  `)).toBeNull();
    expect(policyGroupProblem(`${name}b`)).toBe('tooLong');
  });

  it('refuses a name that does not start with a letter or digit', () => {
    expect(policyGroupProblem('-manual')).toBe('invalidCharacters');
    expect(policyGroupProblem('_manual')).toBe('invalidCharacters');
  });

  it('refuses characters outside the alphabet', () => {
    expect(policyGroupProblem('manual/ch1')).toBe('invalidCharacters');
    expect(policyGroupProblem('<script>')).toBe('invalidCharacters');
  });
});

describe('normalizePolicyGroup', () => {
  it('trims and collapses inner whitespace', () => {
    expect(normalizePolicyGroup('  NCCI   Policy\tManual ')).toBe('NCCI Policy Manual');
  });
});

describe('policyGroupMessages', () => {
  it('renders every message, with the length filled in', () => {
    const intl = createIntl({ locale: 'en', messages: {}, onError: () => undefined });
    expect(intl.formatMessage(policyGroupMessages.tooLong, { maxLength: POLICY_GROUP_MAX_LENGTH })).toContain('64');
    expect(intl.formatMessage(policyGroupMessages.constraint, { maxLength: POLICY_GROUP_MAX_LENGTH })).toContain('64');
    expect(intl.formatMessage(policyGroupMessages.label)).toBe('Policy group (optional)');
  });
});

describe('isGroupWithReplace', () => {
  it('flags a group named with Replace, which would empty the group', () => {
    expect(isGroupWithReplace('NCCI Manual', 'replace')).toBe(true);
  });

  it('allows Replace with no group, and a group with Add', () => {
    expect(isGroupWithReplace('   ', 'replace')).toBe(false);
    expect(isGroupWithReplace('NCCI Manual', 'augment')).toBe(false);
  });
});
