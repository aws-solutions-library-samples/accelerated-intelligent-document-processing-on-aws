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

// ---------------------------------------------------------------------------
// Superseding inside a group (#608). Mirrors normalize_policy_group_change and
// check_policy_group_change in rules_discovery.py, which the upload resolver
// applies; this copy only lets the form offer what the server will accept.

/** How a grouped upload changes its group: add its rules, replace the group's rules, or supersede named rules. */
export type PolicyGroupMode = 'append' | 'replace' | 'supersede';

/** The identity two group names are compared by, as the server's policy_group_key: case and spacing ignored. */
export const policyGroupKey = (group: string): string => normalizePolicyGroup(group).toLowerCase();

type PolicyClassLike = Record<string, unknown>;

/** What the selected profile holds for a group name. */
export type GroupLookup =
  | { status: 'none' } // no group named, or the profile has no class carrying it
  | { status: 'found'; policyClass: PolicyClassLike; ruleNames: string[] }
  | { status: 'ambiguous'; classNames: string[] }; // more than one class carries it; the server refuses

/** Find the one policy class carrying `group` among a profile's `policy_classes`. */
export const findPolicyGroupClass = (policyClasses: unknown, group: string): GroupLookup => {
  const key = policyGroupKey(group);
  if (!key || !Array.isArray(policyClasses)) return { status: 'none' };
  const matches = policyClasses.filter((pc): pc is PolicyClassLike => {
    if (!pc || typeof pc !== 'object') return false;
    const carried = (pc as PolicyClassLike)['x-aws-idp-policy-group'];
    return typeof carried === 'string' && policyGroupKey(carried) === key;
  });
  if (matches.length > 1) {
    return { status: 'ambiguous', classNames: matches.map((pc) => String(pc['x-aws-idp-policy-type'] ?? '?')) };
  }
  if (matches.length === 0) return { status: 'none' };
  const props = matches[0].rule_properties;
  const ruleNames = props && typeof props === 'object' && !Array.isArray(props) ? Object.keys(props) : [];
  return { status: 'found', policyClass: matches[0], ruleNames };
};

/** Why the server would refuse this change, or null when it would accept it. */
export type GroupChangeProblem = 'groupMissing' | 'groupAmbiguous' | 'noRulesChosen';

export const groupChangeProblem = (
  mode: PolicyGroupMode,
  lookup: GroupLookup,
  supersededRules: readonly string[],
): GroupChangeProblem | null => {
  if (lookup.status === 'ambiguous') return 'groupAmbiguous';
  if (mode === 'append') return null;
  if (lookup.status !== 'found') return 'groupMissing';
  if (mode === 'supersede' && supersededRules.length === 0) return 'noRulesChosen';
  return null;
};

/** The mutation variables for a group change: nothing for an append, so the request reads as it always has. */
export const groupChangeVariables = (
  mode: PolicyGroupMode,
  supersededRules: readonly string[],
): { policyGroupMode?: PolicyGroupMode; supersededRules?: string[] } => {
  if (mode === 'replace') return { policyGroupMode: 'replace' };
  if (mode === 'supersede') return { policyGroupMode: 'supersede', supersededRules: [...supersededRules] };
  return {};
};

/** One entry of a policy class's upload record (x-aws-idp-policy-uploads). */
export interface PolicyUploadEntry {
  uploadId: string | null;
  document: string | null;
  savedAt: string | null;
  mode: string;
  rules: string[];
  superseded: { rule: string; uploadId: string | null; document: string | null }[];
}

const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null);

/** A class's upload record, oldest first, with malformed entries dropped. */
export const readPolicyUploads = (value: unknown): PolicyUploadEntry[] => {
  if (!Array.isArray(value)) return [];
  return value
    .filter((e): e is Record<string, unknown> => !!e && typeof e === 'object' && !Array.isArray(e))
    .map((e) => ({
      uploadId: str(e.uploadId),
      document: str(e.document),
      savedAt: str(e.savedAt),
      mode: str(e.mode) ?? 'append',
      rules: Array.isArray(e.rules) ? e.rules.map(String) : [],
      superseded: (Array.isArray(e.superseded) ? e.superseded : [])
        .filter((s): s is Record<string, unknown> => !!s && typeof s === 'object')
        .map((s) => ({ rule: String(s.rule), uploadId: str(s.uploadId), document: str(s.document) })),
    }));
};

export const groupChangeMessages = defineMessages<{
  modeLabel: NoMessageValues;
  modeDescription: NoMessageValues;
  append: NoMessageValues;
  appendDescription: NoMessageValues;
  replace: NoMessageValues;
  replaceDescription: { readonly count: number };
  supersede: NoMessageValues;
  supersedeDescription: NoMessageValues;
  rulesLabel: NoMessageValues;
  rulesDescription: NoMessageValues;
  rulesPlaceholder: NoMessageValues;
  noRulesChosen: NoMessageValues;
  newGroup: NoMessageValues;
  groupAmbiguous: { readonly classNames: string };
  loadError: { readonly message: string };
  loading: NoMessageValues;
}>({
  modeLabel: {
    id: 'discovery.policyGroupChange.modeLabel',
    defaultMessage: 'How this upload changes the group',
    description: 'Label of the choice of how a Policy Discovery upload changes an existing policy group.',
  },
  modeDescription: {
    id: 'discovery.policyGroupChange.modeDescription',
    defaultMessage:
      'Rules are matched only by the names you choose here, never guessed. Superseded rules are removed from the group; earlier revisions of the profile still hold them.',
    description: 'Help text of the policy group change choice.',
  },
  append: {
    id: 'discovery.policyGroupChange.append',
    defaultMessage: 'Add to group',
    description: 'Option: add the upload’s rules to the policy group, keeping every existing rule.',
  },
  appendDescription: {
    id: 'discovery.policyGroupChange.appendDescription',
    defaultMessage: 'Keep every existing rule. A rule whose name the group already holds is added under a new name.',
    description: 'Explanation of the Add to group option.',
  },
  replace: {
    id: 'discovery.policyGroupChange.replace',
    defaultMessage: "Replace this group's rules",
    description: 'Option: the upload’s rules become the policy group’s whole ruleset (for an amended manual).',
  },
  replaceDescription: {
    id: 'discovery.policyGroupChange.replaceDescription',
    defaultMessage:
      "For an amended manual: this upload's rules replace all {count, plural, one {# rule} other {# rules}} in the group. The class keeps its name, description and regexes. Other policy classes are not touched.",
    description: 'Explanation of the Replace this group’s rules option. {count} is the number of rules the group holds now.',
  },
  supersede: {
    id: 'discovery.policyGroupChange.supersede',
    defaultMessage: 'Supersede chosen rules',
    description: 'Option: remove the rules the user chooses and add the upload’s rules (for a rider or amendment page).',
  },
  supersedeDescription: {
    id: 'discovery.policyGroupChange.supersedeDescription',
    defaultMessage:
      "For a rider: the rules you choose are removed and this upload's rules are added. A new rule with a superseded rule's name takes its place; the rest are added alongside.",
    description: 'Explanation of the Supersede chosen rules option.',
  },
  rulesLabel: {
    id: 'discovery.policyGroupChange.rulesLabel',
    defaultMessage: 'Rules this upload supersedes',
    description: 'Label of the multiselect of existing group rules a rider supersedes.',
  },
  rulesDescription: {
    id: 'discovery.policyGroupChange.rulesDescription',
    defaultMessage: 'Choose from the rules the group holds now in the selected configuration profile.',
    description: 'Help text of the superseded rules multiselect.',
  },
  rulesPlaceholder: {
    id: 'discovery.policyGroupChange.rulesPlaceholder',
    defaultMessage: 'Choose rules',
    description: 'Placeholder of the superseded rules multiselect.',
  },
  noRulesChosen: {
    id: 'discovery.policyGroupChange.noRulesChosen',
    defaultMessage: 'Choose at least one rule to supersede, or choose Add to group.',
    description: 'Validation error when Supersede is chosen with no rules selected.',
  },
  newGroup: {
    id: 'discovery.policyGroupChange.newGroup',
    defaultMessage: 'The selected profile has no policy group with this name yet, so this upload creates it.',
    description: 'Shown when the typed policy group does not exist in the selected profile.',
  },
  groupAmbiguous: {
    id: 'discovery.policyGroupChange.groupAmbiguous',
    defaultMessage:
      'More than one policy class carries this group ({classNames}). Merge them, or remove the group from all but one, in Configuration → Policy Schema.',
    description: 'Error when several policy classes carry the typed group. {classNames} lists their names.',
  },
  loadError: {
    id: 'discovery.policyGroupChange.loadError',
    defaultMessage: "Could not read the profile's policy groups: {message}",
    description: 'Error when the selected configuration profile could not be read. {message} is the reason.',
  },
  loading: {
    id: 'discovery.policyGroupChange.loading',
    defaultMessage: "Reading the profile's policy groups…",
    description: 'Shown while the selected configuration profile is being read.',
  },
});

export const uploadHistoryMessages = defineMessages<{
  header: NoMessageValues;
  description: NoMessageValues;
  document: NoMessageValues;
  savedAt: NoMessageValues;
  change: NoMessageValues;
  rules: NoMessageValues;
  superseded: NoMessageValues;
  unknownDocument: NoMessageValues;
  modeAppend: NoMessageValues;
  modeReplace: NoMessageValues;
  modeSupersede: NoMessageValues;
  supersededFrom: { readonly rule: string; readonly document: string };
  none: NoMessageValues;
}>({
  header: {
    id: 'policySchema.uploadHistory.header',
    defaultMessage: 'Policy Discovery uploads',
    description: 'Header of the read-only list of uploads that built a policy class, in the Policy Schema class inspector.',
  },
  description: {
    id: 'policySchema.uploadHistory.description',
    defaultMessage: 'Oldest first: the rules each upload supplied, and the earlier rules it superseded.',
    description: 'Help text of the upload history in the Policy Schema class inspector.',
  },
  document: {
    id: 'policySchema.uploadHistory.document',
    defaultMessage: 'Document',
    description: 'Column header: the uploaded policy document.',
  },
  savedAt: {
    id: 'policySchema.uploadHistory.savedAt',
    defaultMessage: 'Saved',
    description: 'Column header: when the upload’s rules were saved.',
  },
  change: {
    id: 'policySchema.uploadHistory.change',
    defaultMessage: 'Change',
    description: 'Column header: how the upload changed the group (added, replaced, superseded).',
  },
  rules: {
    id: 'policySchema.uploadHistory.rules',
    defaultMessage: 'Rules supplied',
    description: 'Column header: the rule names the upload added.',
  },
  superseded: {
    id: 'policySchema.uploadHistory.superseded',
    defaultMessage: 'Rules superseded',
    description: 'Column header: the earlier rules the upload removed.',
  },
  unknownDocument: {
    id: 'policySchema.uploadHistory.unknownDocument',
    defaultMessage: 'an unrecorded upload',
    description: 'Stands in for the document name of a rule that no recorded upload supplied.',
  },
  modeAppend: {
    id: 'policySchema.uploadHistory.modeAppend',
    defaultMessage: 'Added',
    description: 'Change column value for an upload that added rules.',
  },
  modeReplace: {
    id: 'policySchema.uploadHistory.modeReplace',
    defaultMessage: 'Replaced the group',
    description: 'Change column value for an upload that replaced the group’s rules.',
  },
  modeSupersede: {
    id: 'policySchema.uploadHistory.modeSupersede',
    defaultMessage: 'Superseded chosen rules',
    description: 'Change column value for an upload that superseded chosen rules.',
  },
  supersededFrom: {
    id: 'policySchema.uploadHistory.supersededFrom',
    defaultMessage: '{rule} (from {document})',
    description: 'A superseded rule and the document that had supplied it.',
  },
  none: {
    id: 'policySchema.uploadHistory.none',
    defaultMessage: '–',
    description: 'Shown in a cell with nothing to list.',
  },
});

/**
 * The policy-group part of the uploadDiscoveryDocument variables: nothing for a
 * classes discovery or an ungrouped upload, else the normalised group plus the
 * change (see groupChangeVariables).
 */
export const uploadGroupVariables = (
  discoveryType: string,
  policyGroup: string,
  change: ReturnType<typeof groupChangeVariables>,
): { policyGroup?: string } & ReturnType<typeof groupChangeVariables> => {
  const group = normalizePolicyGroup(policyGroup);
  if (discoveryType !== 'rules' || !group) return {};
  return { policyGroup: group, ...change };
};
