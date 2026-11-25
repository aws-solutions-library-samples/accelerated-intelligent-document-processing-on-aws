// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The reasons panel shows the five worst alerts and counts the rest.
 *
 * A document with many low-confidence fields used to list every stored reason (up to
 * the server's cap of 20) above the editor. The server sorts worst
 * first, so the first five are the ones to read; "+N more alerts" counts everything
 * else, including alerts beyond what the server stores.
 *
 * The routing notice comes first: the routing rule in its header, then the reason the
 * routing rule gave and the policy rules the document failed. Both are kept short (a
 * long reason behind "Show more", the first three failed policy rules with the rest in
 * a dialog), both are rendered as text, and the counter in the header counts what the
 * panel lists.
 */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { screen, within } from '@testing-library/react';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import userEvent from '@testing-library/user-event';

import ReviewTaskContext, { TOP_REASONS } from '../ReviewTaskContext';
import { POLICY_FAILURE_PREVIEW, POLICY_FAILURE_REASON_CHARS, REASON_PREVIEW_CHARS } from '../RoutedBy';
import type { ReviewItemDetail } from '../../../graphql/generated/operation-types';

const reason = (i: number) => ({
  kind: 'low_confidence',
  sectionId: '1',
  field: `field_${i}`,
  label: `Field ${i}`,
  score: 0.1 + i / 100,
  threshold: 0.8,
});

const detail = (stored: number, total: number) =>
  ({
    item: { reasons: Array.from({ length: stored }, (_, i) => reason(i)), reasonCount: total, escalated: false },
    checklist: [],
  }) as unknown as ReviewItemDetail;

const renderPanel = (stored: number, total: number) => renderWithIntl(<ReviewTaskContext detail={detail(stored, total)} />);

const shownReasons = () => screen.queryAllByRole('button', { name: /^Low confidence: Field \d+/ });
// The yellow chip inside a reason's button (Cloudscape marks a badge's colour in its class).
const chipBadge = (button: HTMLElement) => createWrapper(button).findBadge();

describe('ReviewTaskContext reasons', () => {
  it('is headed Review Reason and shows no checklist', () => {
    renderWithIntl(
      <ReviewTaskContext detail={{ ...detail(3, 3), checklist: ['Check every highlighted field'] } as unknown as ReviewItemDetail} />,
    );
    expect(screen.getByRole('heading', { name: /Review Reason/ })).toBeInTheDocument();
    expect(screen.queryByText('Check every highlighted field')).toBeNull();
    expect(screen.queryByRole('checkbox')).toBeNull();
  });

  it('shows only the top five and counts every other alert, stored or not', () => {
    renderPanel(20, 37);
    expect(shownReasons()).toHaveLength(TOP_REASONS);
    expect(screen.getByText('Low confidence: Field 0', { exact: false })).toBeInTheDocument();
    expect(screen.queryByText('Low confidence: Field 5', { exact: false })).toBeNull();
    expect(screen.getByRole('button', { name: 'Show 32 more alerts' })).toBeInTheDocument();
  });

  it('renders each shown reason as its own yellow severity-medium badge', () => {
    renderPanel(20, 37);
    const buttons = shownReasons();
    expect(buttons).toHaveLength(TOP_REASONS);
    buttons.forEach((button, i) => {
      const badge = chipBadge(button);
      expect(badge).not.toBeNull();
      expect(badge!.getElement().className).toMatch(/severity-medium/);
      expect(within(badge!.getElement()).getByText(`Low confidence: Field ${i} ${(0.1 + i / 100).toFixed(2)} < 0.80`)).toBeInTheDocument();
    });
  });

  it('expands to every stored reason and collapses again', async () => {
    renderPanel(20, 37);
    await userEvent.click(screen.getByRole('button', { name: /more alerts/ }));
    expect(shownReasons()).toHaveLength(20);
    shownReasons().forEach((button) => expect(chipBadge(button)!.getElement().className).toMatch(/severity-medium/));
    // What the server did not store stays a count.
    expect(screen.getByText('+17 more alerts')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Show fewer' }));
    expect(shownReasons()).toHaveLength(TOP_REASONS);
  });

  it('offers no toggle when five or fewer alerts exist', () => {
    renderPanel(3, 3);
    expect(shownReasons()).toHaveLength(3);
    expect(screen.queryByRole('button', { name: /more alert/ })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Show fewer' })).toBeNull();
  });

  it('says one more alert in the singular', () => {
    renderPanel(6, 6);
    expect(screen.getByRole('button', { name: 'Show 1 more alert' }).textContent).toBe('+1 more alert');
  });

  it('says from the keyboard why the alerts beyond the stored ones are not listed', async () => {
    renderPanel(3, 5);
    const more = screen.getByRole('button', { name: '+2 more alerts' });
    more.focus();
    await userEvent.keyboard('{Enter}');
    expect(screen.getByText('Only the 3 lowest-confidence alerts are listed.')).toBeInTheDocument();
  });

  it('says in its popover which section and field a reason is about, and by how much it is below the threshold', async () => {
    renderPanel(1, 1);
    await userEvent.click(screen.getByRole('button', { name: /^Low confidence: Field 0/ }));
    expect(screen.getByText('Section 1')).toBeInTheDocument();
    expect(screen.getByText('Field: field_0')).toBeInTheDocument();
    expect(screen.getByText('Confidence 0.10, threshold 0.80')).toBeInTheDocument();
  });

  it('moves to a reason’s field when its chip is chosen', async () => {
    const onReasonClick = vi.fn();
    renderWithIntl(<ReviewTaskContext detail={detail(3, 3)} onReasonClick={onReasonClick} />);
    await userEvent.click(screen.getByRole('button', { name: /^Low confidence: Field 1/ }));
    expect(onReasonClick).toHaveBeenCalledWith(expect.objectContaining({ sectionId: '1', field: 'field_1' }));
  });

  it('moves to a reason’s field from the keyboard', async () => {
    const onReasonClick = vi.fn();
    renderWithIntl(<ReviewTaskContext detail={detail(3, 3)} onReasonClick={onReasonClick} />);
    screen.getByRole('button', { name: /^Low confidence: Field 2/ }).focus();
    await userEvent.keyboard('{Enter}');
    expect(onReasonClick).toHaveBeenCalledWith(expect.objectContaining({ field: 'field_2' }));
  });

  it('steps through every stored reason in order with Next alert, then starts again', async () => {
    const onReasonClick = vi.fn();
    renderWithIntl(<ReviewTaskContext detail={detail(3, 3)} onReasonClick={onReasonClick} />);
    const next = screen.getByRole('button', { name: 'Next alert' });
    for (let i = 0; i < 4; i += 1) await userEvent.click(next);
    expect(onReasonClick.mock.calls.map(([r]) => (r as { field: string }).field)).toEqual(['field_0', 'field_1', 'field_2', 'field_0']);
  });

  it('offers no Next alert without a handler', () => {
    renderPanel(3, 3);
    expect(screen.queryByRole('button', { name: 'Next alert' })).toBeNull();
  });

  it('names a missing required field as such, and its chip moves to the field', async () => {
    const onReasonClick = vi.fn();
    const missing = { kind: 'missing_required', sectionId: '2', field: 'employer_name', label: 'Employer name', score: 0, threshold: 0.8 };
    const withMissing = {
      item: { reasons: [missing, reason(0)], reasonCount: 2, escalated: false },
      checklist: [],
    } as unknown as ReviewItemDetail;
    renderWithIntl(<ReviewTaskContext detail={withMissing} onReasonClick={onReasonClick} />);
    const chip = screen.getByRole('button', { name: 'Missing required: Employer name' });
    expect(screen.queryByText(/0\.00 < 0\.80/)).toBeNull();
    expect(chipBadge(chip)!.getElement().className).toMatch(/severity-medium/);
    await userEvent.click(chip);
    expect(onReasonClick).toHaveBeenCalledWith(expect.objectContaining({ sectionId: '2', field: 'employer_name' }));
    expect(screen.getByText('No extracted value; the class schema requires this field.')).toBeInTheDocument();
  });

  it('names a section whose extraction failed, with the issue that says why (#2038)', async () => {
    // The label is the section's extraction_failed issue message, as ProcessResults stores it.
    const label = "No fields were extracted from this section: Amazon Bedrock's content filter withheld the model's reply.";
    const failed = { kind: 'extraction_failed', sectionId: '2', field: null, label, score: 0, threshold: 0.8 };
    const withFailed = {
      item: { reasons: [failed, reason(0)], reasonCount: 2, escalated: false },
      checklist: [],
    } as unknown as ReviewItemDetail;
    const onReasonClick = vi.fn();
    renderWithIntl(<ReviewTaskContext detail={withFailed} onReasonClick={onReasonClick} />);
    const chip = screen.getByRole('button', { name: 'Extraction failed' });
    expect(screen.queryByText(/0\.00 < 0\.80/)).toBeNull();
    await userEvent.click(chip);
    expect(screen.getByText('Section 2')).toBeTruthy();
    expect(screen.getByText(/Extraction failed for this section/)).toBeTruthy();
    expect(screen.getByText(label)).toBeTruthy();
    expect(screen.queryByText(/^Confidence /)).toBeNull();
    expect(onReasonClick).toHaveBeenCalledWith(expect.objectContaining({ sectionId: '2', kind: 'extraction_failed' }));
  });
});

describe('ReviewTaskContext sent to review', () => {
  it('shows why and by whom a document was sent to review', () => {
    const sent = {
      item: {
        reasons: [],
        reasonCount: 0,
        escalated: false,
        sentToReview: {
          reason: 'Spot check',
          note: 'customer asked',
          requestedBy: 'admin@example.com',
          requestedAt: '2026-11-01T00:00:00Z',
        },
      },
      checklist: [],
    } as unknown as ReviewItemDetail;
    renderWithIntl(<ReviewTaskContext detail={sent} />);
    expect(screen.getByText('Sent to review: Spot check')).toBeInTheDocument();
    expect(screen.getByText(/By admin@example.com — customer asked/)).toBeInTheDocument();
  });
});

describe('ReviewTaskContext escalation', () => {
  it('shows the note of an escalated item under Escalated', () => {
    const escalated = {
      item: { reasons: [], reasonCount: 0, escalated: true, escalationNote: 'Needs a second pair of eyes' },
      checklist: [],
    } as unknown as ReviewItemDetail;
    renderWithIntl(<ReviewTaskContext detail={escalated} />);
    const [alert] = createWrapper(document.body).findAllAlerts();
    expect(alert.findHeader()!.getElement().textContent).toBe('Escalated');
    expect(alert.findContent().getElement().textContent).toBe('Needs a second pair of eyes');
  });
});

const failure = (i: number, over: Record<string, unknown> = {}) => ({
  ruleId: `prior_auth.rule_${i}`,
  policy: 'prior_auth',
  name: `rule_${i}`,
  reason: `Requirement ${i} is not met.`,
  pages: [i, i + 1],
  ...over,
});

const routed = (routedBy: Record<string, unknown> | null, reasons = 0, item: Record<string, unknown> = {}) =>
  ({
    item: { reasons: Array.from({ length: reasons }, (_, i) => reason(i)), reasonCount: reasons, escalated: false, routedBy, ...item },
    checklist: [],
  }) as unknown as ReviewItemDetail;
const RULE = { ruleId: 'claims-over-1000', ruleLabel: 'Claims over 1000', fallbackReason: '' };
const alerts = () => createWrapper(document.body).findAllAlerts();
// Cloudscape puts a space between a heading and its counter.
const counter = () => createWrapper(document.body).findHeader()!.findCounter()!.getElement().textContent?.trim();

describe('ReviewTaskContext routing rule', () => {
  it('says which rule sent the document to its queue, by its label', () => {
    renderWithIntl(<ReviewTaskContext detail={routed(RULE)} />);

    const [alert] = alerts();
    expect(alert.findHeader()!.getElement().textContent).toBe('Routed by rule: Claims over 1000');
    // The reason a document is in review is the yellow warning notice, whatever placed it.
    expect(alert.findRootElement().getElement().className).toMatch(/type-warning/);
    // An item placed before routing rules had a reason: the header is all there is.
    expect(alert.findContent().getElement().textContent).toBe('');
  });

  it('names a rule with no label by its id, and the default outcome when no rule matched', () => {
    renderWithIntl(<ReviewTaskContext detail={routed({ ruleId: 'plain', ruleLabel: null, fallbackReason: '' })} />);
    expect(alerts()[0].findHeader()!.getElement().textContent).toBe('Routed by rule: plain');

    document.body.innerHTML = '';
    renderWithIntl(<ReviewTaskContext detail={routed({ ruleId: '', ruleLabel: null, fallbackReason: '' })} />);
    expect(alerts()[0].findHeader()!.getElement().textContent).toBe('Routed by the default outcome');
  });

  it('says when the queue the routing rule named could not take the document', () => {
    renderWithIntl(<ReviewTaskContext detail={routed({ ruleId: 'claims', ruleLabel: 'Claims', fallbackReason: 'queue_disabled' })} />);

    const [alert] = alerts();
    expect(alert.findRootElement().getElement().className).toMatch(/type-warning/);
    expect(alert.findContent().getElement().textContent).toBe(
      'The queue the routing rule names is disabled. The document is in the Default queue instead.',
    );
  });

  it('shows the rule beside the confidence alerts, and nothing for an item no rule placed', () => {
    renderWithIntl(
      <ReviewTaskContext detail={routed({ ruleId: 'builtin-confidence-alerts', ruleLabel: 'Confidence alerts', fallbackReason: '' }, 2)} />,
    );
    expect(alerts()).toHaveLength(1);
    expect(shownReasons()).toHaveLength(2);

    document.body.innerHTML = '';
    renderWithIntl(<ReviewTaskContext detail={routed(null, 2)} />);
    expect(alerts()).toHaveLength(0);
  });

  it('comes first, before an escalation note and a send-to-review note', () => {
    renderWithIntl(
      <ReviewTaskContext
        detail={routed({ ...RULE, reason: 'Invoice total 1250.00 is over 1000.' }, 1, {
          escalated: true,
          escalationNote: 'Needs a second pair of eyes',
          sentToReview: { reason: 'Spot check', note: null, requestedBy: 'admin@example.com', requestedAt: '2026-11-01T00:00:00Z' },
        })}
      />,
    );

    expect(alerts().map((alert) => alert.findHeader()!.getElement().textContent)).toEqual([
      'Routed by rule: Claims over 1000',
      'Escalated',
      'Sent to review: Spot check',
    ]);
    // Why the document is in review is a warning notice; the escalation note is an info one.
    expect(alerts().map((alert) => /type-([a-z]+)/.exec(alert.findRootElement().getElement().className)?.[1])).toEqual([
      'warning',
      'info',
      'warning',
    ]);
  });
});

describe('ReviewTaskContext routing reason', () => {
  const SHORT = 'Invoice total 1250.00 is over 1000. Check the total against the line items.';
  const LONG = Array.from({ length: 12 }, (_, i) => `Sentence ${i + 1} of the reason says what to check.`).join(' ');

  it('shows the reason of the routing rule in the notice, under the rule', () => {
    renderWithIntl(<ReviewTaskContext detail={routed({ ...RULE, reason: SHORT, reasonSource: 'admin' })} />);

    const [alert] = alerts();
    expect(alert.findHeader()!.getElement().textContent).toBe('Routed by rule: Claims over 1000');
    expect(alert.findContent().getElement().textContent).toBe(SHORT);
    // A reason that fits is shown whole: there is nothing more to show.
    expect(screen.queryByRole('button', { name: 'Show more' })).toBeNull();
  });

  it('shows the reason before why the document is in the Default queue instead', () => {
    renderWithIntl(<ReviewTaskContext detail={routed({ ...RULE, reason: SHORT, fallbackReason: 'queue_missing' })} />);

    expect(alerts()[0].findContent().getElement().textContent).toBe(
      `${SHORT}The queue the routing rule names does not exist. The document is in the Default queue instead.`,
    );
  });

  it('shortens a long reason, and shows the rest and shortens it again on request', async () => {
    expect(LONG.length).toBeGreaterThan(REASON_PREVIEW_CHARS);
    renderWithIntl(<ReviewTaskContext detail={routed({ ...RULE, reason: LONG })} />);
    const content = () => alerts()[0].findContent().getElement().textContent;
    const preview = `${LONG.slice(0, REASON_PREVIEW_CHARS).trimEnd()}…`;

    const more = screen.getByRole('button', { name: 'Show more' });
    expect(more.getAttribute('aria-expanded')).toBe('false');
    expect(content()).toBe(`${preview} Show more`);
    expect(content()).not.toContain('Sentence 12');

    await userEvent.click(more);
    const less = screen.getByRole('button', { name: 'Show less' });
    expect(less.getAttribute('aria-expanded')).toBe('true');
    expect(content()).toBe(`${LONG} Show less`);

    await userEvent.click(less);
    expect(content()).toBe(`${preview} Show more`);
  });

  it('shows the rest of a long reason from the keyboard', async () => {
    renderWithIntl(<ReviewTaskContext detail={routed({ ...RULE, reason: LONG })} />);

    screen.getByRole('button', { name: 'Show more' }).focus();
    await userEvent.keyboard('{Enter}');

    expect(alerts()[0].findContent().getElement().textContent).toBe(`${LONG} Show less`);
  });

  it('shows what the reason holds as text, not as markup', () => {
    const markup = 'Vendor <img src=x onerror="alert(1)"> **Acme** [site](https://example.com) <b>bold</b>';
    renderWithIntl(<ReviewTaskContext detail={routed({ ...RULE, reason: markup })} />);
    const content = alerts()[0].findContent().getElement();

    expect(content.textContent).toBe(markup);
    expect(content.querySelector('img')).toBeNull();
    expect(content.querySelector('a')).toBeNull();
    expect(content.querySelector('b')).toBeNull();
  });
});

describe('ReviewTaskContext failed policy rules', () => {
  const failures = (n: number) => Array.from({ length: n }, (_, i) => failure(i + 1));
  const lines = () =>
    alerts()[0]
      .findContent()
      .findAll('div')
      .map((line) => line.getElement().textContent ?? '')
      .filter((text) => /^rule_\d+ · /.test(text));
  const unique = (texts: string[]): string[] => [...new Set(texts)];

  it('lists them under the reason, each with its name, its policy and its reason', () => {
    renderWithIntl(
      <ReviewTaskContext
        detail={routed({ ...RULE, reason: 'Policy rules failed.', policyFailures: failures(2), policyFailureCount: 2 })}
      />,
    );
    const content = alerts()[0].findContent().getElement().textContent ?? '';

    expect(screen.getByText('Failed policy rules (2)')).toBeInTheDocument();
    expect(unique(lines())).toEqual(['rule_1 · prior_auth — Requirement 1 is not met.', 'rule_2 · prior_auth — Requirement 2 is not met.']);
    expect(content.indexOf('Policy rules failed.')).toBeLessThan(content.indexOf('Failed policy rules (2)'));
    // Everything is listed: there is no dialog to open.
    expect(screen.queryByRole('button', { name: /failed policy rule/ })).toBeNull();
  });

  it('lists none, and no heading, for a document that failed none', () => {
    renderWithIntl(<ReviewTaskContext detail={routed({ ...RULE, reason: 'Over 1000.', policyFailures: [], policyFailureCount: 0 })} />);

    expect(screen.queryByText(/Failed policy rules/)).toBeNull();
    expect(alerts()[0].findContent().getElement().textContent).toBe('Over 1000.');
  });

  it('lists the first three, and opens all of them in a dialog with their pages', async () => {
    renderWithIntl(<ReviewTaskContext detail={routed({ ...RULE, policyFailures: failures(5), policyFailureCount: 5 })} />);

    expect(screen.getByText('Failed policy rules (5)')).toBeInTheDocument();
    expect(unique(lines())).toHaveLength(POLICY_FAILURE_PREVIEW);
    expect(unique(lines())[2]).toBe('rule_3 · prior_auth — Requirement 3 is not met.');
    expect(createWrapper(document.body).findModal()).toBeNull();

    await userEvent.click(screen.getByRole('button', { name: 'Show all 5 failed policy rules' }));

    const modal = createWrapper(document.body).findModal()!;
    expect(modal.findHeader().getElement().textContent).toBe('Failed policy rules (5)');
    const table = modal.findContent().findTable()!;
    expect(table.findColumnHeaders().map((header) => header.getElement().textContent)).toEqual([
      'Policy rule',
      'Policy',
      'Reason',
      'Pages',
    ]);
    expect(table.findRows()).toHaveLength(5);
    expect([1, 2, 3, 4].map((column) => table.findBodyCell(5, column)!.getElement().textContent)).toEqual([
      'rule_5',
      'prior_auth',
      'Requirement 5 is not met.',
      '5, 6',
    ]);
    // Every failed policy rule is in the table: there is nothing to add.
    expect(modal.findContent().getElement().textContent).not.toContain('Policy Rule Validation Summary');

    await userEvent.click(within(modal.findFooter()!.getElement()).getByRole('button', { name: 'Close' }));
    expect(createWrapper(document.body).findModal()).toBeNull();
  });

  it('opens the dialog from the keyboard', async () => {
    renderWithIntl(<ReviewTaskContext detail={routed({ ...RULE, policyFailures: failures(4), policyFailureCount: 4 })} />);

    screen.getByRole('button', { name: 'Show all 4 failed policy rules' }).focus();
    await userEvent.keyboard('{Enter}');

    expect(createWrapper(document.body).findModal()!.findContent().findTable()!.findRows()).toHaveLength(4);
  });

  it('says in the dialog when the document failed more policy rules than are kept with the review', async () => {
    renderWithIntl(<ReviewTaskContext detail={routed({ ...RULE, policyFailures: failures(10), policyFailureCount: 14 })} />);

    expect(screen.getByText('Failed policy rules (14)')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Show all 14 failed policy rules' }));

    const modal = createWrapper(document.body).findModal()!;
    expect(modal.findContent().findTable()!.findRows()).toHaveLength(10);
    expect(
      within(modal.findContent().getElement()).getByText(
        'The document failed 14 policy rules; 10 are listed here. The Policy Rule Validation Summary has all of them.',
      ),
    ).toBeInTheDocument();
  });

  it('shortens the reason of a failed policy rule, with the whole of it and its pages in a popover that opens from the keyboard', async () => {
    const long = Array.from({ length: 6 }, (_, i) => `Clause ${i + 1} of the policy was not satisfied.`).join(' ');
    expect(long.length).toBeGreaterThan(POLICY_FAILURE_REASON_CHARS);
    renderWithIntl(
      <ReviewTaskContext
        detail={routed({ ...RULE, policyFailures: [failure(1, { reason: long, pages: [2, 7] })], policyFailureCount: 1 })}
      />,
    );
    const short = `${long.slice(0, POLICY_FAILURE_REASON_CHARS).trimEnd()}…`;

    expect(unique(lines())).toEqual([`rule_1 · prior_auth — ${short}`]);
    const popover = alerts()[0].findContent().findPopover()!;
    const trigger = popover.findTrigger().getElement();
    expect(trigger.tagName).toBe('BUTTON');
    expect(trigger.textContent).toBe(short);

    trigger.focus();
    await userEvent.keyboard('{Enter}');

    const shown = popover.findContent()!.getElement().textContent ?? '';
    expect(shown).toContain(long);
    expect(shown).toContain('Pages: 2, 7');
  });

  it('offers no popover for a short reason that cites no page', () => {
    renderWithIntl(<ReviewTaskContext detail={routed({ ...RULE, policyFailures: [failure(1, { pages: [] })], policyFailureCount: 1 })} />);

    expect(unique(lines())).toEqual(['rule_1 · prior_auth — Requirement 1 is not met.']);
    expect(alerts()[0].findContent().findPopover()).toBeNull();
  });

  it('names a policy rule with no name by its id, and says when it has no reason', () => {
    renderWithIntl(
      <ReviewTaskContext
        detail={routed({
          ...RULE,
          policyFailures: [{ ruleId: 'prior_auth.coverage', policy: 'prior_auth', name: '', reason: '', pages: [] }],
          policyFailureCount: 1,
        })}
      />,
    );

    expect(alerts()[0].findContent().getElement().textContent).toContain('prior_auth.coverage · prior_auth — No reason was given.');
  });

  it('shows what a failed policy rule says as text, not as markup', () => {
    const markup = '<script>alert(1)</script> **must** [see](https://example.com)';
    renderWithIntl(
      <ReviewTaskContext
        detail={routed({
          ...RULE,
          policyFailures: [failure(1, { name: '<i>coverage</i>', reason: markup, pages: [] })],
          policyFailureCount: 1,
        })}
      />,
    );
    const content = alerts()[0].findContent().getElement();

    expect(content.textContent).toContain(`<i>coverage</i> · prior_auth — ${markup}`);
    expect(content.querySelector('script')).toBeNull();
    expect(content.querySelector('i')).toBeNull();
    expect(content.querySelector('a')).toBeNull();
  });
});

describe('ReviewTaskContext counter', () => {
  it('counts the alerts of an item no rule placed', () => {
    renderWithIntl(<ReviewTaskContext detail={routed(null, 3)} />);
    expect(counter()).toBe('(3)');
  });

  it('counts the routing notice, so a document a routing rule placed does not show zero', () => {
    renderWithIntl(<ReviewTaskContext detail={routed({ ...RULE, reason: 'Over 1000.' })} />);
    expect(counter()).toBe('(1)');
  });

  it('counts the routing notice, every alert and every failed policy rule', () => {
    const many = Array.from({ length: 10 }, (_, i) => failure(i + 1));
    // 37 alerts in all, of which two are stored; 14 failed policy rules, of which ten are stored.
    const placed = routed({ ...RULE, reason: 'Policy rules failed.', policyFailures: many, policyFailureCount: 14 }, 2, {
      reasonCount: 37,
    });
    renderWithIntl(<ReviewTaskContext detail={placed} />);

    // 1 routing notice + 37 alerts + 14 failed policy rules.
    expect(counter()).toBe('(52)');
  });

  it('counts the failed policy rules that are listed when the count is missing', () => {
    renderWithIntl(<ReviewTaskContext detail={routed({ ...RULE, policyFailures: [failure(1), failure(2)] })} />);
    expect(counter()).toBe('(3)');
  });
});
