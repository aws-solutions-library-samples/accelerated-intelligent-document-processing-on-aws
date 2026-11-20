// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import { render, screen } from '@testing-library/react';
import { IntlProvider } from 'react-intl';
import { describe, expect, it, vi } from 'vitest';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import PolicyGroupChangeField from '../PolicyGroupChangeField';
import { findPolicyGroupClass, type PolicyGroupMode } from '../policyGroup';

const found = findPolicyGroupClass(
  [{ 'x-aws-idp-policy-group': 'NCCI Manual', rule_properties: { visit_limit: {}, modifier_25: {} } }],
  'NCCI Manual',
);

const renderField = (props: Partial<React.ComponentProps<typeof PolicyGroupChangeField>> = {}) => {
  const onModeChange = vi.fn();
  const onSupersededRulesChange = vi.fn();
  const view = render(
    <IntlProvider locale="en" defaultLocale="en">
      <PolicyGroupChangeField
        lookup={found}
        mode={'append' as PolicyGroupMode}
        onModeChange={onModeChange}
        supersededRules={[]}
        onSupersededRulesChange={onSupersededRulesChange}
        {...props}
      />
    </IntlProvider>,
  );
  return { ...view, onModeChange, onSupersededRulesChange, wrapper: createWrapper(view.container) };
};

describe('PolicyGroupChangeField', () => {
  it('offers the three changes for a group the profile holds, with the rule count', () => {
    const { wrapper, onModeChange } = renderField();
    expect(screen.getByText('Add to group')).toBeTruthy();
    expect(screen.getByText("Replace this group's rules")).toBeTruthy();
    expect(screen.getByText('Supersede chosen rules')).toBeTruthy();
    expect(screen.getByText(/replace all 2 rules in the group/)).toBeTruthy();
    expect(wrapper.findMultiselect()).toBeNull();
    wrapper.findRadioGroup()!.findInputByValue('replace')!.click();
    expect(onModeChange).toHaveBeenCalledWith('replace');
  });

  it("lists exactly the group's current rules to supersede, and reports the choice", () => {
    const { wrapper, onSupersededRulesChange } = renderField({ mode: 'supersede' });
    const multiselect = wrapper.findMultiselect()!;
    multiselect.openDropdown();
    const labels = multiselect
      .findDropdown()
      .findOptions()
      .map((o) => o.getElement().textContent);
    expect(labels).toEqual(['visit_limit', 'modifier_25']);
    multiselect.selectOptionByValue('modifier_25');
    expect(onSupersededRulesChange).toHaveBeenCalledWith(['modifier_25']);
    expect(screen.getByText(/Choose at least one rule to supersede/)).toBeTruthy();
  });

  it('shows no error once a rule is chosen', () => {
    renderField({ mode: 'supersede', supersededRules: ['visit_limit'] });
    expect(screen.queryByText(/Choose at least one rule to supersede/)).toBeNull();
  });

  it('says a new group will be created, and offers no change, when the profile has none', () => {
    const { wrapper } = renderField({ lookup: { status: 'none' } });
    expect(screen.getByText(/has no policy group with this name yet/)).toBeTruthy();
    expect(wrapper.findRadioGroup()).toBeNull();
  });

  it('names the classes when the group is ambiguous', () => {
    renderField({ lookup: { status: 'ambiguous', classNames: ['one', 'two'] } });
    expect(screen.getByText(/More than one policy class carries this group \(one, two\)/)).toBeTruthy();
  });

  it('reports a profile it could not read', () => {
    renderField({ loadError: 'denied' });
    expect(screen.getByText("Could not read the profile's policy groups: denied")).toBeTruthy();
  });

  it('does not let a disabled form change the mode', () => {
    const { container } = renderField({ disabled: true });
    const inputs = container.querySelectorAll('input[type="radio"]');
    expect(inputs.length).toBe(3);
    inputs.forEach((i) => expect((i as HTMLInputElement).disabled).toBe(true));
  });
});
