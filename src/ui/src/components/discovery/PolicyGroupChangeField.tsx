// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// How a Policy Discovery upload changes an existing policy group (#608): add its
// rules, replace the group's rules, or supersede rules the user chooses from the
// group's current rules. Shown only when the typed group exists in the selected
// profile; rule identity is never inferred.
import React from 'react';
import { Alert, FormField, Multiselect, RadioGroup, SpaceBetween, StatusIndicator } from '@cloudscape-design/components';
import { useIntl } from 'react-intl';
import { groupChangeMessages, groupChangeProblem, type GroupLookup, type PolicyGroupMode } from './policyGroup';

interface PolicyGroupChangeFieldProps {
  lookup: GroupLookup;
  loading?: boolean;
  loadError?: string | null;
  mode: PolicyGroupMode;
  onModeChange: (mode: PolicyGroupMode) => void;
  supersededRules: readonly string[];
  onSupersededRulesChange: (rules: string[]) => void;
  disabled?: boolean;
}

const PolicyGroupChangeField = ({
  lookup,
  loading = false,
  loadError = null,
  mode,
  onModeChange,
  supersededRules,
  onSupersededRulesChange,
  disabled = false,
}: PolicyGroupChangeFieldProps): React.JSX.Element | null => {
  const intl = useIntl();
  const m = groupChangeMessages;

  if (loading) return <StatusIndicator type="loading">{intl.formatMessage(m.loading)}</StatusIndicator>;
  if (loadError) return <Alert type="error">{intl.formatMessage(m.loadError, { message: loadError })}</Alert>;
  if (lookup.status === 'ambiguous') {
    return <Alert type="error">{intl.formatMessage(m.groupAmbiguous, { classNames: lookup.classNames.join(', ') })}</Alert>;
  }
  if (lookup.status === 'none') return <Alert type="info">{intl.formatMessage(m.newGroup)}</Alert>;

  const problem = groupChangeProblem(mode, lookup, supersededRules);
  return (
    <SpaceBetween size="s">
      <FormField label={intl.formatMessage(m.modeLabel)} description={intl.formatMessage(m.modeDescription)}>
        <RadioGroup
          value={mode}
          onChange={({ detail }) => onModeChange(detail.value as PolicyGroupMode)}
          ariaLabel={intl.formatMessage(m.modeLabel)}
          items={[
            { value: 'append', label: intl.formatMessage(m.append), description: intl.formatMessage(m.appendDescription), disabled },
            {
              value: 'replace',
              label: intl.formatMessage(m.replace),
              description: intl.formatMessage(m.replaceDescription, { count: lookup.ruleNames.length }),
              disabled,
            },
            {
              value: 'supersede',
              label: intl.formatMessage(m.supersede),
              description: intl.formatMessage(m.supersedeDescription),
              disabled,
            },
          ]}
        />
      </FormField>
      {mode === 'supersede' && (
        <FormField
          label={intl.formatMessage(m.rulesLabel)}
          description={intl.formatMessage(m.rulesDescription)}
          errorText={problem === 'noRulesChosen' ? intl.formatMessage(m.noRulesChosen) : undefined}
        >
          <Multiselect
            selectedOptions={supersededRules.map((r) => ({ value: r, label: r }))}
            onChange={({ detail }) => onSupersededRulesChange(detail.selectedOptions.map((o) => o.value as string))}
            options={lookup.ruleNames.map((r) => ({ value: r, label: r }))}
            placeholder={intl.formatMessage(m.rulesPlaceholder)}
            filteringType="auto"
            disabled={disabled}
            ariaLabel={intl.formatMessage(m.rulesLabel)}
          />
        </FormField>
      )}
    </SpaceBetween>
  );
};

export default PolicyGroupChangeField;
