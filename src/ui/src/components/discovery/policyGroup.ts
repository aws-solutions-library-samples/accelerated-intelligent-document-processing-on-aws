// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The optional policy group of a Policy Discovery upload. Uploads naming the same
// group append their rules to one policy class instead of each creating a class of
// their own (#608). The rule here mirrors normalize_policy_group in
// idp_common/discovery/rules_discovery.py, which the upload resolver applies; this
// copy exists only so the form can say what is wrong before anything is uploaded.
import { defineMessages, type NoMessageValues } from 'react-intl';

export const POLICY_GROUP_MAX_LENGTH = 64;

const POLICY_GROUP_PATTERN = /^[A-Za-z0-9][A-Za-z0-9 _.-]*$/;

/** Surrounding whitespace removed and inner runs collapsed, as the server does. */
export const normalizePolicyGroup = (value: string): string => value.split(/\s+/).filter(Boolean).join(' ');

export type PolicyGroupProblem = 'tooLong' | 'invalidCharacters';

/** Why the server would refuse this group name, or null when it would accept it (empty means no group). */
export const policyGroupProblem = (value: string): PolicyGroupProblem | null => {
  const normalized = normalizePolicyGroup(value);
  if (!normalized) return null;
  if (normalized.length > POLICY_GROUP_MAX_LENGTH) return 'tooLong';
  if (!POLICY_GROUP_PATTERN.test(normalized)) return 'invalidCharacters';
  return null;
};

export const policyGroupMessages = defineMessages<{
  label: NoMessageValues;
  description: NoMessageValues;
  placeholder: NoMessageValues;
  constraint: { readonly maxLength: number };
  tooLong: { readonly maxLength: number };
  invalidCharacters: NoMessageValues;
  replaceWithGroup: NoMessageValues;
}>({
  label: {
    id: 'discovery.policyGroup.label',
    defaultMessage: 'Policy group (optional)',
    description: 'Label of the optional policy group name field on the Policy Discovery upload form.',
  },
  description: {
    id: 'discovery.policyGroup.description',
    defaultMessage:
      'Uploads with the same policy group add their rules to one policy class, so one regex matches them all. Use it for chapters of one manual. Leave empty to save this upload as its own policy class.',
    description: 'Help text of the policy group field on the Policy Discovery upload form.',
  },
  placeholder: {
    id: 'discovery.policyGroup.placeholder',
    defaultMessage: 'e.g. NCCI Policy Manual',
    description: 'Placeholder of the policy group field on the Policy Discovery upload form. Keep the example name untranslated.',
  },
  constraint: {
    id: 'discovery.policyGroup.constraint',
    defaultMessage:
      'Up to {maxLength} characters: letters, digits, spaces, underscores, hyphens and dots. Rules are added to the group, never replaced.',
    description: 'Constraint text under the policy group field. {maxLength} is the maximum length (64).',
  },
  tooLong: {
    id: 'discovery.policyGroup.tooLong',
    defaultMessage: 'Policy group must be at most {maxLength} characters.',
    description: 'Validation error of a policy group name that is too long. {maxLength} is the maximum length (64).',
  },
  invalidCharacters: {
    id: 'discovery.policyGroup.invalidCharacters',
    defaultMessage:
      'Policy group must start with a letter or digit and contain only letters, digits, spaces, underscores, hyphens and dots.',
    description: 'Validation error of a policy group name with characters that are not allowed.',
  },
  replaceWithGroup: {
    id: 'discovery.policyGroup.replaceWithGroup',
    defaultMessage:
      'A policy group cannot be used with Replace existing schema, because Replace removes every policy class in the profile, including the group, before discovery runs. Choose Add to existing schema, or clear the policy group.',
    description: 'Error shown on the Policy Discovery form when a policy group is entered while the save mode is Replace.',
  },
});

/** True when a group is named together with Replace, which the server refuses: Replace would empty the group. */
export const isGroupWithReplace = (policyGroup: string, saveMode: string): boolean =>
  saveMode === 'replace' && normalizePolicyGroup(policyGroup) !== '';
