// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The queue dialog in Review Studio › Review Management. What has to hold: creating reads
 * "Create Queue"; the fields in the two columns each carry a one-line description so
 * the inputs line up; a production queue has no confidence threshold, and its Enabled
 * switch is prefilled and sent on save; and a Test Studio queue, which has no switch,
 * saves without it. The dialog has no reject-reasons
 * editor: an existing queue's stored list is sent back unchanged and a new queue gets
 * the backend's defaults.
 */

import React from 'react';
import { act, fireEvent, render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import type { FormFieldWrapper } from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const createReviewQueue = vi.fn();
const updateReviewQueue = vi.fn();
const updateTestSetQueue = vi.fn();
const listReviewQueues = vi.fn();
vi.mock('../reviewer-assignment', () => ({
  createReviewQueue: (...a: unknown[]) => createReviewQueue(...a),
  listReviewQueues: (...a: unknown[]) => listReviewQueues(...a),
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
  skipReasons: ['Out of time'],
  timeLimitMinutes: 60,
  eligibleAnnotators: [],
  isDefaultQueue: false,
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
  listReviewQueues.mockReset().mockResolvedValue([]);
});

/** What the REST client throws for the API's refusal of a save over a newer version. */
const queueChanged = {
  errors: [
    {
      message: "QUEUE_CHANGED: queue 'claims' was changed by someone else after you opened it; reload it and make your change again",
      errorType: 'BadRequest',
    },
  ],
};

const alerts = () => createWrapper(document.body).findAllAlerts();
const changedAlert = () => alerts().find((a) => /changed by someone else/.test(a.getElement().textContent ?? ''));

describe("a save over someone else's change (#1727)", () => {
  it('sends the version the form was filled from, with the stored fields', async () => {
    const onSaved = open({ queue: queue({ version: 4, description: 'Note', eligibleAnnotators: ['a@example.com'] }) });
    clickSave();
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(updateReviewQueue.mock.calls[0][0]).toMatchObject({
      id: 'claims',
      version: 4,
      description: 'Note',
      eligibleAnnotators: ['a@example.com'],
    });
  });

  it('sends no version when creating', async () => {
    const onCreated = open();
    setInput('Queue id', 'new-q');
    setInput('Name', 'New');
    clickSave();
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect((createReviewQueue.mock.calls[0][0] as Record<string, unknown>).version).toBeUndefined();
  });

  it('sends no version when the API reported none (an older backend)', async () => {
    const onSaved = open({ queue: queue() });
    clickSave();
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect((updateReviewQueue.mock.calls[0][0] as Record<string, unknown>).version).toBeUndefined();
  });

  it('says the queue changed, keeps the dialog open and offers a reload', async () => {
    updateReviewQueue.mockRejectedValueOnce(queueChanged);
    const onSaved = open({ queue: queue({ version: 1 }) });
    clickSave();
    await waitFor(() => expect(changedAlert()).toBeTruthy());
    expect(onSaved).not.toHaveBeenCalled();
    const text = changedAlert()!.getElement().textContent ?? '';
    expect(text).toMatch(/your changes were not saved/i);
    expect(text).not.toMatch(/QUEUE_CHANGED/);
    expect(changedAlert()!.findActionSlot()!.findButton()!.getElement().textContent).toBe('Reload queue');
    // The generic failure banner is not shown as well.
    expect(modal().findContent().getElement().textContent).not.toMatch(/Failed to save the queue/);
  });

  it('reloads the stored queue into the form, and the next save sends its version', async () => {
    updateReviewQueue.mockRejectedValueOnce(queueChanged);
    listReviewQueues.mockResolvedValue([
      queue({ id: 'other', version: 9 }),
      queue({
        name: 'Claims (renamed)',
        priority: 7,
        description: 'Their note',
        eligibleAnnotators: ['b@example.com'],
        skipReasons: ['Busy'],
        timeLimitMinutes: 15,
        enabled: true,
        version: 2,
      }),
    ]);
    const onSaved = open({ queue: queue({ version: 1 }) });
    setInput('Priority', '3');
    clickSave();
    await waitFor(() => expect(changedAlert()).toBeTruthy());
    fireEvent.click(changedAlert()!.findActionSlot()!.findButton()!.getElement());
    await waitFor(() => expect(changedAlert()).toBeFalsy());
    expect(field('Name').findControl()!.findInput()!.getInputValue()).toBe('Claims (renamed)');
    expect(field('Priority').findControl()!.findInput()!.getInputValue()).toBe('7');
    clickSave();
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    // Every field comes from the reloaded definition, so the save keeps the other Admin's change.
    expect(updateReviewQueue.mock.calls[1][0]).toEqual(
      expect.objectContaining({
        id: 'claims',
        name: 'Claims (renamed)',
        description: 'Their note',
        priority: 7,
        timeLimitMinutes: 15,
        eligibleAnnotators: ['b@example.com'],
        skipReasons: ['Busy'],
        enabled: true,
        version: 2,
      }),
    );
  });

  it("keeps what was typed, and the warning, when the page's poll hands it a newer copy", async () => {
    // ReviewQueuePage passes the definition from its polled list, a new object every poll.
    updateReviewQueue.mockRejectedValueOnce(queueChanged);
    const props = {
      visible: true,
      existingIds: [],
      annotators: [],
      annotatorsLoading: false,
      onDismiss: () => undefined,
      onSaved: vi.fn(),
    };
    const { rerender } = render(<QueueDefinitionModal {...props} queue={queue({ version: 1 })} />);
    setInput('Priority', '3');
    clickSave();
    await waitFor(() => expect(changedAlert()).toBeTruthy());
    act(() => rerender(<QueueDefinitionModal {...props} queue={queue({ version: 2, name: 'Polled' })} />));
    expect(changedAlert()).toBeTruthy();
    expect(field('Priority').findControl()!.findInput()!.getInputValue()).toBe('3');
    // Pointed at another queue, the dialog does refill.
    act(() => rerender(<QueueDefinitionModal {...props} queue={queue({ id: 'other', name: 'Other', version: 5 })} />));
    expect(changedAlert()).toBeFalsy();
    expect(field('Name').findControl()!.findInput()!.getInputValue()).toBe('Other');
  });

  it('says so when the queue was deleted in the meantime', async () => {
    updateReviewQueue.mockRejectedValueOnce(queueChanged);
    listReviewQueues.mockResolvedValue([]);
    open({ queue: queue({ version: 1 }) });
    clickSave();
    await waitFor(() => expect(changedAlert()).toBeTruthy());
    fireEvent.click(changedAlert()!.findActionSlot()!.findButton()!.getElement());
    await waitFor(() => expect(changedAlert()!.getElement().textContent).toMatch(/no longer exists/));
  });

  it('shows any other failure as before', async () => {
    updateReviewQueue.mockRejectedValueOnce({ errors: [{ message: 'BAD_ARGUMENT: priority', errorType: 'BadRequest' }] });
    open({ queue: queue({ version: 1 }) });
    clickSave();
    await waitFor(() => expect(modal().findContent().getElement().textContent).toMatch(/Failed to save the queue: BAD_ARGUMENT/));
    expect(changedAlert()).toBeFalsy();
  });
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

  it('pairs Queue id/Name and Priority/Time limit, then Enabled, each with a one-line description', () => {
    open();
    const all = createWrapper(document.body).findAllFormFields();
    const fields = all.slice(0, 5);
    // The five share one two-column layout, which the full-width fields below are outside.
    const first = fields[0].getElement();
    let layout: HTMLElement | null = first.parentElement;
    while (layout && !layout.contains(fields[4].getElement())) layout = layout.parentElement;
    expect(layout?.contains(field('Description').getElement())).toBe(false);
    expect(fields.map((f) => f.findLabel()?.getElement().textContent)).toEqual([
      'Queue id',
      'Name',
      'Priority',
      'Time limit (minutes)',
      'Enabled',
    ]);
    for (const f of fields) {
      const description = f.findDescription()?.getElement().textContent ?? '';
      expect(description, `${f.findLabel()?.getElement().textContent} description`).not.toBe('');
      expect(description.length, `${f.findLabel()?.getElement().textContent} description stays one line`).toBeLessThanOrEqual(50);
    }
    expect(field('Queue id').findDescription()!.getElement().textContent).not.toMatch(/hitl\.review_queues/);
  });

  it('has no confidence threshold field', () => {
    open({ queue: queue() });
    const labels = createWrapper(document.body)
      .findAllFormFields()
      .map((f) => f.findLabel()?.getElement().textContent);
    expect(labels).not.toContain('Confidence threshold');
  });

  it('describes the Default queue as where documents with confidence alerts arrive', () => {
    open({ queue: queue({ id: 'default', name: 'Default', isDefaultQueue: true }) });
    expect(modal().findContent().getElement().textContent).toContain(
      'Documents with confidence alerts arrive here when HITL is enabled in their configuration and this queue is enabled.',
    );
  });

  it('sends the Enabled switch on create, defaulting to on, and no threshold', async () => {
    const onSaved = open();
    setInput('Queue id', 'claims');
    setInput('Name', 'Claims');
    clickSave();
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(createReviewQueue).toHaveBeenCalledWith(expect.objectContaining({ id: 'claims', enabled: true }));
    expect(createReviewQueue.mock.calls[0][0]).not.toHaveProperty('confidenceThreshold');
  });

  it('prefills the stored switch on edit and sends the edited value', async () => {
    const onSaved = open({ queue: queue() });
    const toggle = field('Enabled').findControl()!.findToggle()!;
    expect(toggle.findNativeInput().getElement().checked).toBe(false);
    fireEvent.click(toggle.findNativeInput().getElement());
    clickSave();
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(updateReviewQueue).toHaveBeenCalledWith(expect.objectContaining({ id: 'claims', enabled: true }));
    expect(updateReviewQueue.mock.calls[0][0]).not.toHaveProperty('confidenceThreshold');
  });

  it('leaves the switch off a Test Studio queue, which has none', async () => {
    const onSaved = open({
      testSet: { testSetId: 'ts-1', name: 'Invoices', priority: 0, claimLeaseMinutes: 30, skipReasons: ['Out of time'] },
    });
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
    // A Test Studio queue has skip reasons too, saved with its settings.
    expect(sent.skipReasons).toEqual(['Out of time']);
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

  it('edits the skip reasons one per line, trimmed and without duplicates', async () => {
    const onSaved = open({ queue: queue({ skipReasons: ['Out of time'] }) });
    const textarea = field('Skip reasons').findControl()!.findTextarea()!;
    expect(textarea.getTextareaValue()).toBe('Out of time');
    textarea.setTextareaValue(' Out of time \n\nNeeds a specialist\nOut of time');
    clickSave();
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(updateReviewQueue).toHaveBeenCalledWith(expect.objectContaining({ skipReasons: ['Out of time', 'Needs a specialist'] }));
  });

  it('refuses to save an existing queue with no skip reasons', async () => {
    open({ queue: queue() });
    field('Skip reasons').findControl()!.findTextarea()!.setTextareaValue('  \n ');
    clickSave();
    await waitFor(() => expect(field('Skip reasons').findError()?.getElement().textContent).toBe('Enter at least one reason.'));
    expect(updateReviewQueue).not.toHaveBeenCalled();
  });

  it('sends no skip reasons for a new queue left empty, so it gets the defaults', async () => {
    const onCreated = open();
    setInput('Queue id', 'claims');
    setInput('Name', 'Claims');
    clickSave();
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect((createReviewQueue.mock.calls[0][0] as Record<string, unknown>).skipReasons).toBeUndefined();
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
