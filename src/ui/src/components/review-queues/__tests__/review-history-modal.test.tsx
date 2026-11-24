// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * History, from the edit screen, shows why an item was skipped or rejected, newest first,
 * which routing rule routed the document to which queue and the reason it gave, the step
 * the run after a completed review starts at with the sections it extracts again, and
 * the step a send-back starts at.
 */

import React from 'react';
import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { createIntl, IntlProvider } from 'react-intl';

import createWrapper from '@cloudscape-design/components/test-utils/dom';

import ReviewHistoryModal, { entryDetailLines, entryDetails, eventLabel, eventText } from '../ReviewHistoryModal';

const intl = createIntl({ locale: 'en' });
const entry = (event: string, details: Record<string, unknown> | null) => ({
  at: '2026-10-20T10:00:00.000Z',
  event,
  actor: null,
  details: details ? JSON.stringify(details) : null,
});

const English = ({ children }: { children: React.ReactNode }) => (
  <IntlProvider locale="en" defaultLocale="en">
    {children}
  </IntlProvider>
);

const HISTORY = [
  { at: '2026-10-20T09:00:00.000Z', event: 'claimed', actor: 'ann@example.com', details: null },
  {
    at: '2026-10-20T09:30:00.000Z',
    event: 'released',
    actor: 'ann@example.com',
    details: JSON.stringify({ reason: 'Needs expert review', note: 'tax schedule' }),
  },
];

describe('ReviewHistoryModal', () => {
  it('shows a skip with its reason and note, newest first', () => {
    render(<ReviewHistoryModal history={HISTORY} onDismiss={() => undefined} />, { wrapper: English });
    expect(screen.getByText('Review history (2)')).toBeInTheDocument();
    const rows = screen.getAllByRole('row').slice(1);
    expect(within(rows[0]).getByText('Skipped')).toBeInTheDocument();
    expect(within(rows[0]).getByText('Needs expert review')).toBeInTheDocument();
    expect(within(rows[0]).getByText('tax schedule')).toBeInTheDocument();
    expect(within(rows[1]).getByText('Claimed')).toBeInTheDocument();
  });

  it('reads details defensively', () => {
    expect(entryDetails({ at: 'x', event: 'moved', details: '{"from":"a","to":"b"}' })).toEqual({});
    expect(entryDetails({ at: 'x', event: 'moved', details: 'not json' })).toEqual({});
  });

  it('labels an event it does not know readably', () => {
    expect(eventLabel('something_new')).toBe('Something new');
    expect(eventText(intl, 'something_new')).toBe('Something new');
    expect(eventText(intl, 'released')).toBe('Skipped');
  });
});

describe('the routing events', () => {
  it.each([
    ['routed', 'Routed to a queue'],
    ['rerouted', 'Routed to a queue again after review'],
    ['sent_back', 'Sent back for reprocessing'],
    ['placement_limit_reached', 'Not routed again: placement limit reached'],
    ['routing_evaluation_failed', 'Routing rules could not be evaluated'],
  ])('%s is labelled %s', (event, label) => {
    expect(eventText(intl, event)).toBe(label);
  });

  it('show the queue and the rule a document was routed by', () => {
    const routed = entry('routed', { queue: 'claims', rule_id: 'claims-over-1000', rule_label: 'Claims over 1000', fallback_reason: '' });

    expect(entryDetailLines(intl, routed)).toEqual(['Queue: claims', 'Routing rule: Claims over 1000']);
  });

  it('show the reason the routing rule gave, after the queue and the routing rule', () => {
    const routed = entry('routed', {
      queue: 'claims',
      rule_id: 'claims-over-1000',
      rule_label: 'Claims over 1000',
      fallback_reason: '',
      routing_reason: 'Invoice total 1250.00 is over 1000. Check the total against the line items.',
      reason_source: 'admin',
    });

    expect(entryDetails(routed)).toMatchObject({
      routingReason: 'Invoice total 1250.00 is over 1000. Check the total against the line items.',
      // The routing reason is not something a person chose.
      reason: undefined,
    });
    expect(entryDetailLines(intl, routed)).toEqual([
      'Queue: claims',
      'Routing rule: Claims over 1000',
      'Routing reason: Invoice total 1250.00 is over 1000. Check the total against the line items.',
    ]);
  });

  it('show the reason of the default outcome, and of a placement in the Default queue instead', () => {
    expect(
      entryDetailLines(
        intl,
        entry('rerouted', { queue: 'default', rule_id: '', rule_label: '', routing_reason: 'No routing rule matched.' }),
      ),
    ).toEqual(['Queue: default', 'Default outcome', 'Routing reason: No routing rule matched.']);
    expect(
      entryDetailLines(
        intl,
        entry('routed', { queue: 'default', rule_id: 'claims', fallback_reason: 'queue_disabled', routing_reason: 'Over 1000.' }),
      ),
    ).toEqual([
      'Queue: default',
      'Routing rule: claims',
      'The queue the routing rule names is disabled. The document is in the Default queue instead.',
      'Routing reason: Over 1000.',
    ]);
  });

  it.each([[''], ['   '], [null], [7]])('show no reason line for a placement recorded with the reason %j', (routing_reason) => {
    expect(entryDetailLines(intl, entry('routed', { queue: 'claims', rule_id: 'claims', routing_reason }))).toEqual([
      'Queue: claims',
      'Routing rule: claims',
    ]);
  });

  it('name a rule with no label by its id, and the default outcome when no rule decided', () => {
    expect(entryDetailLines(intl, entry('rerouted', { by_cycle: 'c2', queue: 'claims', rule_id: 'plain' }))).toEqual([
      'Queue: claims',
      'Routing rule: plain',
    ]);
    expect(entryDetailLines(intl, entry('routed', { queue: 'default', rule_id: '', rule_label: '', fallback_reason: '' }))).toEqual([
      'Queue: default',
      'Default outcome',
    ]);
  });

  it('say why a routed document is in the Default queue instead', () => {
    const routed = entry('routed', { queue: 'default', rule_id: 'claims', rule_label: 'Claims', fallback_reason: 'queue_missing' });

    expect(entryDetailLines(intl, routed)).toEqual([
      'Queue: default',
      'Routing rule: Claims',
      'The queue the routing rule names does not exist. The document is in the Default queue instead.',
    ]);
  });

  it('show the step a send-back starts at by its name, and not the event name as a reason', () => {
    const sentBack = entry('sent_back', { reason: 'sent_back', start_step: 'policy_rule_validation', note: 'policy changed' });

    expect(entryDetails(sentBack)).toMatchObject({ reason: undefined, note: 'policy changed', startStep: 'policy_rule_validation' });
    expect(entryDetailLines(intl, sentBack)).toEqual(['Start step: Policy Rule Validation']);
    // A step this UI has no name for is shown as the server sent it.
    expect(entryDetailLines(intl, entry('sent_back', { reason: 'sent_back', start_step: 'something_new', note: '' }))).toEqual([
      'Start step: something_new',
    ]);
  });

  it('show the limit, the rule and the queue of a placement that was not made', () => {
    const limit = entry('placement_limit_reached', { limit: 3, rule_id: 'claims', queue: 'claims' });

    expect(entryDetailLines(intl, limit)).toEqual(['Queue: claims', 'Routing rule: claims', 'Limit: 3 in a row']);
  });

  it('keep a chosen reason for the events that have one, after any routing lines', () => {
    expect(entryDetailLines(intl, entry('withdrawn', { reason: 'reprocessed' }))).toEqual(['reprocessed']);
    expect(entryDetailLines(intl, entry('claimed', null))).toEqual([]);
    expect(entryDetailLines(intl, entry('routing_evaluation_failed', { table_id: 'review-routing', error: 'ValueError' }))).toEqual([]);
  });

  it('are shown in the history table with their details and note', () => {
    render(
      <ReviewHistoryModal
        history={[
          entry('routed', {
            queue: 'claims',
            rule_id: 'claims-over-1000',
            rule_label: 'Claims over 1000',
            fallback_reason: '',
            routing_reason: 'Vendor <b>Acme</b> is not on the approved list.',
          }),
          {
            ...entry('sent_back', { reason: 'sent_back', start_step: 'extraction', note: 'wrong prompt' }),
            at: '2026-10-20T11:00:00.000Z',
            actor: 'rev@example.com',
          },
        ]}
        onDismiss={() => undefined}
      />,
      { wrapper: English },
    );
    const rows = screen.getAllByRole('row').slice(1);

    expect(within(rows[0]).getByText('Sent back for reprocessing')).toBeInTheDocument();
    expect(within(rows[0]).getByText('rev@example.com')).toBeInTheDocument();
    expect(within(rows[0]).getByText('Start step: Extraction')).toBeInTheDocument();
    expect(within(rows[0]).getByText('wrong prompt')).toBeInTheDocument();
    expect(within(rows[0]).queryByText('sent_back')).toBeNull();
    expect(within(rows[1]).getByText('Routed to a queue')).toBeInTheDocument();
    expect(within(rows[1]).getByText('Queue: claims')).toBeInTheDocument();
    expect(within(rows[1]).getByText('Routing rule: Claims over 1000')).toBeInTheDocument();
    // The reason is text: what looks like markup in it is shown, not rendered.
    expect(within(rows[1]).getByText('Routing reason: Vendor <b>Acme</b> is not on the approved list.')).toBeInTheDocument();
    expect(rows[1].querySelector('b')).toBeNull();
  });

  it('wrap in the history table, so a long reason is shown whole', () => {
    const long = Array.from({ length: 12 }, (_, i) => `Sentence ${i + 1} of the reason says what to check.`).join(' ');
    render(
      <ReviewHistoryModal
        history={[entry('routed', { queue: 'claims', rule_id: 'claims', routing_reason: long })]}
        onDismiss={() => undefined}
      />,
      { wrapper: English },
    );
    const table = createWrapper(document.body).findModal()!.findContent().findTable()!;

    expect(table.findBodyCell(1, 4)!.getElement().textContent).toContain(`Routing reason: ${long}`);
    // Cloudscape marks the content of a cell that wraps; without the mark the cell is one line, cut off.
    expect(table.findBodyCell(1, 4)!.getElement().querySelector('[class*="body-cell-wrap"]')).not.toBeNull();
  });
});

describe('a completed review', () => {
  it('shows the step the run that follows starts at, by its name', () => {
    const completed = entry('completed', { restart_step: 'apply_review_changes', restart_sections: [] });

    expect(entryDetails(completed)).toMatchObject({ restartStep: 'apply_review_changes', restartSections: undefined });
    expect(entryDetailLines(intl, completed)).toEqual(['Run starts at: Apply Review Changes']);
  });

  it('shows the sections the run extracts again when the reviewer regrouped pages', () => {
    const completed = entry('completed', { restart_step: 'extraction', restart_sections: ['2', '4', '5'] });

    expect(entryDetailLines(intl, completed)).toEqual(['Run starts at: Extraction', 'Sections extracted again: 2, 4, and 5']);
    expect(entryDetailLines(intl, entry('completed', { restart_step: 'extraction', restart_sections: ['3'] }))).toEqual([
      'Run starts at: Extraction',
      'Sections extracted again: 3',
    ]);
  });

  it('shows a step this UI has no name for as the server sent it, and reads the sections defensively', () => {
    expect(entryDetailLines(intl, entry('completed', { restart_step: 'something_new', restart_sections: 'all' }))).toEqual([
      'Run starts at: something_new',
    ]);
    expect(entryDetailLines(intl, entry('completed', { restart_sections: [2, '', null, '7'] }))).toEqual(['Sections extracted again: 7']);
  });

  it('shows nothing more for a review completed before the step was recorded', () => {
    expect(entryDetailLines(intl, entry('completed', null))).toEqual([]);
    expect(entryDetailLines(intl, entry('completed', { disposition: 'completed' }))).toEqual([]);
  });

  it('is shown in the history table with the step and the sections', () => {
    render(
      <ReviewHistoryModal
        history={[{ ...entry('completed', { restart_step: 'extraction', restart_sections: ['2', '4'] }), actor: 'rev@example.com' }]}
        onDismiss={() => undefined}
      />,
      { wrapper: English },
    );
    const [row] = screen.getAllByRole('row').slice(1);

    expect(within(row).getByText('Completed')).toBeInTheDocument();
    expect(within(row).getByText('Run starts at: Extraction')).toBeInTheDocument();
    expect(within(row).getByText('Sections extracted again: 2 and 4')).toBeInTheDocument();
  });
});
