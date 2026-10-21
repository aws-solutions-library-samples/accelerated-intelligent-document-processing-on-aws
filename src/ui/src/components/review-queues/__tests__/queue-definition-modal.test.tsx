// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The queue dialog in HITL › Queue Management. What has to hold: creating reads
 * "Create Queue"; the fields paired in the two columns each carry a one-line
 * description so the inputs line up; a production queue's confidence threshold and
 * Enabled switch are prefilled, validated (0 to 1) and sent on save; and a Test Studio
 * queue, which has neither, saves without them. The dialog has no reject-reasons
 * editor: an existing queue's stored list is sent back unchanged and a new queue gets
 * the backend's defaults.
 */

import React from 'react';
import { fireEvent, render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import type { FormFieldWrapper } from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const createReviewQueue = vi.fn();
const updateReviewQueue = vi.fn();
const updateTestSetQueue = vi.fn();
vi.mock('../reviewer-assignment', () => ({
  createReviewQueue: (...a: unknown[]) => createReviewQueue(...a),
  updateReviewQueue: (...a: unknown[]) => updateReviewQueue(...a),
  updateTestSetQueue: (...a: unknown[]) => updateTestSetQueue(...a),
}));

import QueueDefinitionModal from '../QueueDefinitionModal';
import type { ReviewQueueDefinition } from '../../../graphql/generated/operation-types';

const queue = (over: Partial<ReviewQueueDefinition> = {}): ReviewQueueDefinition => ({
  id: 'claims',
  name: 'Claims',
  description: '',
  priority: 1,
  checklist: [],
  rejectReasons: [],
  timeLimitMinutes: 60,
  eligibleAnnotators: [],
  isDefaultQueue: false,
  confidenceThreshold: 0.65,
  enabled: false,
  ...over,
});

const open = (props: Partial<React.ComponentProps<typeof QueueDefinitionModal>> = {}) => {
  const onSaved = vi.fn();
  render(
    <QueueDefinitionModal
      visible
      queue={null}
      existingIds={[]}
      annotators={[]}
      annotatorsLoading={false}
      onDismiss={() => undefined}
      onSaved={onSaved}
      {...props}
    />,
  );
  return onSaved;
};

const modal = () => {
  const m = createWrapper(document.body).findModal();
  if (!m) throw new Error('modal not rendered');
  return m;
};

const field = (label: string): FormFieldWrapper => {
  const match = createWrapper(document.body)
    .findAllFormFields()
    .find((w) => w.findLabel()?.getElement().textContent === label);
  if (!match) throw new Error(`no field ${label}`);
  return match;
};

const setInput = (label: string, value: string) => field(label).findControl()!.findInput()!.setInputValue(value);
/** The footer's last button: Create or Save. */
const primary = () => modal().findFooter()!.findAllButtons().at(-1)!;
const clickSave = () => fireEvent.click(primary().getElement());

beforeEach(() => {
  document.body.innerHTML = '';
  createReviewQueue.mockReset().mockImplementation(async (input: unknown) => input);
  updateReviewQueue.mockReset().mockImplementation(async (input: unknown) => input);
  updateTestSetQueue.mockReset().mockResolvedValue({});
});

describe('the queue dialog', () => {
  it('is titled "Create Queue" when creating, with a Create button', () => {
    open();
    expect(modal().findHeader().getElement().textContent).toBe('Create Queue');
    expect(primary().getElement().textContent).toBe('Create');
  });

  it('keeps the settings title when editing', () => {
    open({ queue: queue() });
    expect(modal().findHeader().getElement().textContent).toBe('Queue Settings: Claims');
  });

  it('pairs Queue id/Name, Priority/Time limit and Confidence threshold/Enabled, each with a one-line description', () => {
    open();
    const all = createWrapper(document.body).findAllFormFields();
    const fields = all.slice(0, 6);
    // The six share one two-column layout, which the full-width fields below are outside.
    const first = fields[0].getElement();
    let layout: HTMLElement | null = first.parentElement;
    while (layout && !layout.contains(fields[5].getElement())) layout = layout.parentElement;
    expect(layout?.contains(field('Description').getElement())).toBe(false);
    expect(fields.map((f) => f.findLabel()?.getElement().textContent)).toEqual([
      'Queue id',
      'Name',
      'Priority',
      'Time limit (minutes)',
      'Confidence threshold',
      'Enabled',
    ]);
    for (const f of fields) {
      const description = f.findDescription()?.getElement().textContent ?? '';
      expect(description, `${f.findLabel()?.getElement().textContent} description`).not.toBe('');
      expect(description.length, `${f.findLabel()?.getElement().textContent} description stays one line`).toBeLessThanOrEqual(50);
    }
    expect(field('Queue id').findDescription()!.getElement().textContent).not.toMatch(/hitl\.review_queues/);
  });

  it('explains that routing goes to Default under a non-Default queue threshold', () => {
    open({ queue: queue() });
    expect(field('Confidence threshold').findConstraint()!.getElement().textContent).toContain(
      'Used when documents are routed to this queue; today all routing goes to Default.',
    );
  });

  it('describes the Default queue as where low-confidence documents arrive', () => {
    open({ queue: queue({ id: 'default', name: 'Default', isDefaultQueue: true }) });
    expect(modal().findContent().getElement().textContent).toContain('Low-confidence documents from the pipeline arrive here.');
    expect(field('Confidence threshold').findConstraint()!.getElement().textContent).not.toContain('today all routing');
  });

  it('sends the threshold and Enabled switch on create, defaulting to 0.8 and on', async () => {
    const onSaved = open();
    setInput('Queue id', 'claims');
    setInput('Name', 'Claims');
    clickSave();
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(createReviewQueue).toHaveBeenCalledWith(expect.objectContaining({ id: 'claims', confidenceThreshold: 0.8, enabled: true }));
  });

  it('prefills the stored threshold and switch on edit and sends the edited values', async () => {
    const onSaved = open({ queue: queue() });
    expect(field('Confidence threshold').findControl()!.findInput()!.getInputValue()).toBe('0.65');
    const toggle = field('Enabled').findControl()!.findToggle()!;
    expect(toggle.findNativeInput().getElement().checked).toBe(false);
    setInput('Confidence threshold', '0.9');
    fireEvent.click(toggle.findNativeInput().getElement());
    clickSave();
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(updateReviewQueue).toHaveBeenCalledWith(expect.objectContaining({ id: 'claims', confidenceThreshold: 0.9, enabled: true }));
  });

  it.each(['1.5', '-0.1', '', 'abc'])('refuses the threshold %j', (value) => {
    open({ queue: queue() });
    setInput('Confidence threshold', value);
    clickSave();
    expect(field('Confidence threshold').findError()?.getElement().textContent).toBe('Enter a number from 0 to 1.');
    expect(updateReviewQueue).not.toHaveBeenCalled();
  });

  it.each(['0', '1', '0.05'])('accepts the threshold %j', async (value) => {
    const onSaved = open({ queue: queue() });
    setInput('Confidence threshold', value);
    clickSave();
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(updateReviewQueue).toHaveBeenCalledWith(expect.objectContaining({ confidenceThreshold: Number(value) }));
  });

  it('leaves the threshold and switch off a Test Studio queue, which has neither', async () => {
    const onSaved = open({ testSet: { testSetId: 'ts-1', name: 'Invoices', priority: 0, claimLeaseMinutes: 30 } });
    const labels = createWrapper(document.body)
      .findAllFormFields()
      .map((f) => f.findLabel()?.getElement().textContent);
    expect(labels).not.toContain('Confidence threshold');
    expect(labels).not.toContain('Enabled');
    clickSave();
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    const sent = updateTestSetQueue.mock.calls[0][0] as Record<string, unknown>;
    expect(sent).not.toHaveProperty('confidenceThreshold');
    expect(sent).not.toHaveProperty('enabled');
  });

  it('has no reject-reasons editor, and an edit keeps the stored list', async () => {
    const onSaved = open({ queue: queue({ rejectReasons: ['Illegible', 'Wrong document'] }) });
    const labels = createWrapper(document.body)
      .findAllFormFields()
      .map((f) => f.findLabel()?.getElement().textContent);
    expect(labels).not.toContain('Reject reasons');
    expect(modal().findContent().getElement().textContent).not.toMatch(/reject reason/i);
    clickSave();
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(updateReviewQueue).toHaveBeenCalledWith(expect.objectContaining({ rejectReasons: ['Illegible', 'Wrong document'] }));
  });

  it('sends no reject reasons for a new queue, so it gets the defaults', async () => {
    const onCreated = open();
    setInput('Queue id', 'claims');
    setInput('Name', 'Claims');
    clickSave();
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect((createReviewQueue.mock.calls[0][0] as Record<string, unknown>).rejectReasons).toBeUndefined();
  });
});
