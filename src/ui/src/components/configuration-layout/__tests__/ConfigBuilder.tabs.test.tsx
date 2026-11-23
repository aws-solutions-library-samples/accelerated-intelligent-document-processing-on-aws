// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The configuration editor's tabs: Processing Settings (id `configuration`, so existing
 * `?tab=` links still work) and Document Schema. The prompt preview is not a tab; it is
 * the page's Preview prompts button. Policy Schema and Routing Rules are added by the
 * page, in that order. The Routing Rules tab is told, from the form's values, whether
 * Review Routing is on and whether policy rule validation runs.
 */
import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { IntlProvider } from 'react-intl';
import { MemoryRouter } from 'react-router-dom';

vi.mock('../../../hooks/use-synthetic-data-generator', () => ({ default: () => ({ available: false }) }));
const listReviewQueues = vi.fn();
vi.mock('../../review-queues/reviewer-assignment', () => ({ listReviewQueues: () => listReviewQueues() }));

import ConfigBuilder from '../ConfigBuilder';

const ROUTING = {
  tables: [
    {
      id: 'review-routing',
      rules: [
        {
          id: 'claims-first',
          label: 'Claims',
          enabled: true,
          conditions: [{ input: 'policy_rule_verdict', path: 'prior_auth.coverage', aggregate: 'any', test: '"fail"' }],
          actions: [{ type: 'review_queue', queue: 'default' }],
          reason: '',
        },
      ],
      default_outcome: { actions: [], reason: '' },
    },
  ],
  max_consecutive_placements: 3,
};
const POLICY_CLASSES = [{ 'x-aws-idp-policy-type': 'prior_auth', rule_properties: { coverage: {} } }];
const ROUTING_OFF = /Review Routing is off for this profile/;
const POLICY_OFF = 'Tests a policy rule result; policy rule validation is off';

const renderBuilder = (props: Partial<React.ComponentProps<typeof ConfigBuilder>> = {}): void => {
  render(
    <IntlProvider locale="en">
      <MemoryRouter>
        <ConfigBuilder schema={{ type: 'object', properties: {} }} formValues={{}} routing={ROUTING} {...props} />
      </MemoryRouter>
    </IntlProvider>,
  );
};
const tabLabels = (): (string | null)[] =>
  createWrapper(document.body)
    .findTabs()!
    .findTabLinks()
    .map((tab) => tab.getElement().textContent);

describe('the configuration editor tabs', () => {
  it('are Processing Settings and Document Schema, with no Prompt Preview tab', () => {
    render(
      <MemoryRouter>
        <ConfigBuilder schema={{ type: 'object', properties: {} }} formValues={{}} />
      </MemoryRouter>,
    );
    const tabs = createWrapper(document.body).findTabs()!;
    expect(tabs.findTabLinks().map((t) => t.getElement().textContent)).toEqual(['Processing Settings', 'Document Schema']);
    expect(tabs.findTabLinkById('configuration')).not.toBeNull();
  });

  it('gain Routing Rules after Policy Schema when the page asks for it', () => {
    renderBuilder({ showRuleSchema: true, showRoutingTab: true });

    expect(tabLabels()).toEqual(['Processing Settings', 'Document Schema', 'Policy Schema', 'Routing Rules']);
    expect(createWrapper(document.body).findTabs()!.findTabLinkById('routing')).not.toBeNull();
  });

  it('do not gain Routing Rules with Policy Schema alone', () => {
    renderBuilder({ showRuleSchema: true });

    expect(tabLabels()).toEqual(['Processing Settings', 'Document Schema', 'Policy Schema']);
  });
});

describe('the Routing Rules tab in the configuration editor', () => {
  it('shows the routing section of the form, read-only unless the page says otherwise', () => {
    listReviewQueues.mockReset();
    renderBuilder({ showRuleSchema: true, showRoutingTab: true, activeTabId: 'routing', onTabChange: vi.fn(), onRoutingChange: vi.fn() });

    expect(screen.getByText('claims-first')).toBeTruthy();
    expect(screen.getByText('Only Admins can change routing rules.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Add routing rule' })).toBeNull();
    expect(listReviewQueues).not.toHaveBeenCalled();
  });

  it('is read-only when the page gives it no way to change the form', () => {
    renderBuilder({ showRoutingTab: true, activeTabId: 'routing', onTabChange: vi.fn(), routingReadOnly: false });

    expect(screen.queryByRole('button', { name: 'Add routing rule' })).toBeNull();
  });

  it('passes a change to the page, and offers the built-in rules of the system defaults', async () => {
    listReviewQueues.mockReset().mockResolvedValue([{ id: 'default', name: 'Default' }]);
    const onRoutingChange = vi.fn();
    const builtIn = {
      id: 'builtin-confidence-alerts',
      label: 'Confidence alerts',
      enabled: true,
      conditions: [],
      actions: [{ type: 'review_queue', queue: 'default' }],
      reason: 'Fields below the confidence threshold: {confidence_alert_count}.',
    };
    renderBuilder({
      showRoutingTab: true,
      activeTabId: 'routing',
      onTabChange: vi.fn(),
      routingReadOnly: false,
      onRoutingChange,
      defaultConfig: { routing: { tables: [{ id: 'review-routing', rules: [builtIn] }] } },
    });
    await act(async () => {});

    expect(screen.getByRole('button', { name: 'Add routing rule' })).toBeTruthy();
    const more = createWrapper(document.body).findContainer()!.findHeader()!.findButtonDropdown()!;
    act(() => more.openDropdown());
    act(() => more.findItemById('restore')!.click());

    const rules = onRoutingChange.mock.calls.at(-1)![0].tables[0].rules as { id: string; reason: string }[];
    expect(rules.map((rule) => rule.id)).toEqual(['claims-first', 'builtin-confidence-alerts']);
    expect(rules[1].reason).toBe(builtIn.reason);
  });
});

describe('what the Routing Rules tab is told from the form', () => {
  const onTab = (props: Partial<React.ComponentProps<typeof ConfigBuilder>>): void =>
    renderBuilder({ showRoutingTab: true, activeTabId: 'routing', onTabChange: vi.fn(), ...props });

  it.each([
    ['is not set', {}],
    ['is off', { hitl: { enabled: false } }],
    ['is off, as text', { hitl: { enabled: 'false' } }],
  ])('warns that Review Routing is off when the switch %s', (_state, formValues) => {
    onTab({ formValues });

    expect(screen.getByText(ROUTING_OFF)).toBeTruthy();
  });

  it.each([
    ['is on', { hitl: { enabled: true } }],
    ['is on, as text', { hitl: { enabled: 'true' } }],
  ])('does not warn when the switch %s', (_state, formValues) => {
    onTab({ formValues });

    expect(screen.queryByText(ROUTING_OFF)).toBeNull();
  });

  it('shows Processing Settings, where the switch is, from the warning', () => {
    const onTabChange = vi.fn();
    onTab({ formValues: {}, onTabChange });

    fireEvent.click(screen.getByRole('button', { name: 'Open Processing Settings' }));

    expect(onTabChange).toHaveBeenCalledWith('configuration');
  });

  it('shows Processing Settings from the warning when the editor holds the tab itself', () => {
    renderBuilder({ showRoutingTab: true, formValues: {} });
    const tabs = createWrapper(document.body).findTabs()!;
    act(() => tabs.findTabLinkById('routing')!.click());
    expect(tabs.findActiveTab()!.getElement().textContent).toBe('Routing Rules');

    fireEvent.click(screen.getByRole('button', { name: 'Open Processing Settings' }));

    expect(tabs.findActiveTab()!.getElement().textContent).toBe('Processing Settings');
  });

  // The rule of idp_common.config.feature_flags.policy_rule_validation_enabled.
  it.each([
    ['is on and the profile has policies', { rule_validation: { enabled: true }, policy_classes: POLICY_CLASSES }, false],
    ['is not set and the profile has policies', { policy_classes: POLICY_CLASSES }, false],
    ['is off', { rule_validation: { enabled: false }, policy_classes: POLICY_CLASSES }, true],
    ['is off, as text', { rule_validation: { enabled: 'false' }, policy_classes: POLICY_CLASSES }, true],
    ['is on and the profile has no policies', { rule_validation: { enabled: true }, policy_classes: [] }, true],
    ['is on and the profile has no policy section', { rule_validation: { enabled: true } }, true],
  ])('when policy rule validation %s, a routing rule that reads a policy rule result is marked: %s', (_state, formValues, marked) => {
    onTab({ formValues: { hitl: { enabled: true }, ...formValues } });

    expect(screen.queryByText(POLICY_OFF) !== null).toBe(marked);
  });
});
