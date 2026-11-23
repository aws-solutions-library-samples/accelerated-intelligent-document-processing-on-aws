// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The optional policy group input of the Policy Discovery upload form (#608).
import React from 'react';
import { FormField, Input } from '@cloudscape-design/components';
import { useIntl } from 'react-intl';
import { policyGroupMessages, policyGroupProblem, POLICY_GROUP_MAX_LENGTH } from './policyGroup';

interface PolicyGroupFieldProps {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}

const PolicyGroupField = ({ value, onChange, disabled = false }: PolicyGroupFieldProps): React.JSX.Element => {
  const intl = useIntl();
  const problem = policyGroupProblem(value);
  const values = { maxLength: POLICY_GROUP_MAX_LENGTH };
  let errorText: string | undefined;
  if (problem === 'tooLong') errorText = intl.formatMessage(policyGroupMessages.tooLong, values);
  else if (problem === 'invalidCharacters') errorText = intl.formatMessage(policyGroupMessages.invalidCharacters);

  return (
    <FormField
      label={intl.formatMessage(policyGroupMessages.label)}
      description={intl.formatMessage(policyGroupMessages.description)}
      constraintText={intl.formatMessage(policyGroupMessages.constraint, values)}
      errorText={errorText}
    >
      <Input
        value={value}
        onChange={({ detail }) => onChange(detail.value)}
        placeholder={intl.formatMessage(policyGroupMessages.placeholder)}
        disabled={disabled}
        ariaLabel={intl.formatMessage(policyGroupMessages.label)}
      />
    </FormField>
  );
};

export default PolicyGroupField;
