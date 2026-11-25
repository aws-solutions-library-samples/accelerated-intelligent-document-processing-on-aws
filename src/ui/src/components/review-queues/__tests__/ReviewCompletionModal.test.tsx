// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The dialog that asks how a review ends. What has to hold: completing is the default
 * and carries no warning; sending back says the corrections and any page regrouping are
 * discarded and where the document is processed again from, and takes an optional note;
 * neither ending asks for a step, because the server decides where the run starts; for
 * the ending chosen the dialog shows what the server says the run will do (the start
 * step, each step with its state, the sections extracted again), read once for both
 * endings; while that is read a loading state shows, and when it cannot be read a
 * warning with Retry shows and the review can still be completed; the one confirm
 * button does what was chosen and nothing else; while an outcome runs the dialog cannot
 * be changed or closed; and each opening starts from Complete again.
 */

import React from 'react';
import { act, fireEvent, render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { IntlProvider } from 'react-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const getReviewCompletionPlan = vi.fn();
vi.mock('../../run-plan/run-plan-api', () => ({
  getReviewCompletionPlan: (...a: unknown[]) => getReviewCompletionPlan(...a),
}));

import type { PlannedRun, ReviewCompletionPlan } from '../../../graphql/generated/operation-types';
import { MAX_NOTE_LENGTH } from '../../common/ReasonModal';
import ReviewCompletionModal from '../ReviewCompletionModal';

type Props = React.ComponentProps<typeof ReviewCompletionModal>;

/** Completing a review that regrouped section 2: it is extracted again, then the corrections are applied. */
const COMPLETE: PlannedRun = {
  mode: 'pipeline',
  startStep: 'extraction',
  steps: [
    { id: 'beginning', label: 'Beginning', state: 'skipped' },
    { id: 'classification', label: 'Classification', state: 'skipped' },
    { id: 'extraction', label: 'Extraction', state: 'run' },
    { id: 'assessment', label: 'Assessment', state: 'run' },
    { id: 'apply_review_changes', label: 'Apply Review Changes', state: 'run' },
    { id: 'policy_rule_validation', label: 'Policy Rule Validation', state: 'not_enabled' },
    { id: 'review_routing', label: 'Review Routing', state: 'run' },
    { id: 'summarization', label: 'Summarization', state: 'run' },
    { id: 'evaluation', label: 'Evaluation', state: 'run' },
  ],
  sections: [{ sectionId: '2', cause: 'pages_changed' }],
};

/** Sending the same document back: processed again from classification, with no review to apply. */
const SEND_BACK: PlannedRun = {
  mode: 'pipeline',
  startStep: 'classification',
  steps: [
    { id: 'beginning', label: 'Beginning', state: 'skipped' },
    { id: 'classification', label: 'Classification', state: 'run' },
    { id: 'extraction', label: 'Extraction', state: 'run' },
    { id: 'assessment', label: 'Assessment', state: 'run' },
    { id: 'policy_rule_validation', label: 'Policy Rule Validation', state: 'not_enabled' },
    { id: 'review_routing', label: 'Review Routing', state: 'run' },
    { id: 'summarization', label: 'Summarization', state: 'run' },
    { id: 'evaluation', label: 'Evaluation', state: 'run' },
  ],
  sections: [],
};

const PLAN: ReviewCompletionPlan = { complete: COMPLETE, sendBack: SEND_BACK };

const modal = () => createWrapper(document.body).findModal()!;
const content = () => modal().findContent();
const outcomes = () => content().findRadioGroup()!;
const footerButtons = () => modal().findFooter()!.findAllButtons();
const primary = () => footerButtons().at(-1)!;
const chooseSendBack = () => act(() => outcomes().findInputByValue('send-back')!.click());
const chooseComplete = () => act(() => outcomes().findInputByValue('complete')!.click());
/** The steps the dialog lists, in order: each one's name and what the run does with it. */
const stepRows = () =>
  content()
    .findList()!
    .findItems()
    .map((item) => [item.findContent().getElement().textContent, item.findActions()!.getElement().textContent]);
const text = () => content().getElement().textContent ?? '';
/** The warnings and errors the dialog shows, each as its header (when it has one) and its text. */
const alerts = () =>
  content()
    .findAllAlerts()
    .map((alert) => ({
      header: alert.findHeader()?.getElement().textContent ?? null,
      body: alert.findContent().getElement().textContent,
    }));

/** Render the dialog. It reads the plan as soon as it is open; `settled` waits for the answer. */
const open = (props: Partial<Props> = {}) => {
  const handlers = { onComplete: vi.fn(), onSendBack: vi.fn(), onCancel: vi.fn() };
  const element = (more: Partial<Props> = {}) => (
    <IntlProvider locale="en">
      <ReviewCompletionModal visible objectKey="in/w2.pdf" cycleId="c1" canSendBack busy={null} {...handlers} {...props} {...more} />
    </IntlProvider>
  );
  const { rerender } = render(element());
  return { ...handlers, rerender: (more: Partial<Props>) => rerender(element(more)) };
};
const settled = () => waitFor(() => expect(content().findStatusIndicator('[class*="status-loading"]')).toBeNull());
/** Render the dialog and wait for the plan (or the failure to read it). */
const opened = async (props: Partial<Props> = {}) => {
  const result = open(props);
  await settled();
  return result;
};

beforeEach(() => {
  getReviewCompletionPlan.mockReset().mockResolvedValue(PLAN);
});

describe('completing a review', () => {
  it('is the default: no warning and no note, and the button says Complete review', async () => {
    const { onComplete, onSendBack } = await opened();

    expect(modal().findHeader().getElement().textContent).toBe('Finish the review');
    expect(outcomes().findInputByValue('complete')!.getElement().checked).toBe(true);
    expect(content().findAlert()).toBeNull();
    expect(content().findTextarea()).toBeNull();
    expect(primary().getElement().textContent).toBe('Complete review');

    primary().click();

    expect(onComplete).toHaveBeenCalledTimes(1);
    expect(onSendBack).not.toHaveBeenCalled();
  });

  it('is the only choice when the document cannot be sent back', async () => {
    await opened({ canSendBack: false });

    expect(
      outcomes()
        .findButtons()
        .map((b) => b.findNativeInput().getElement().value),
    ).toEqual(['complete']);
  });
});

describe('what the run will do', () => {
  it('shows, for completing, the step the run starts at, each step with its state, and the sections extracted again', async () => {
    await opened();

    expect(text()).toContain('Starts at Extraction');
    expect(stepRows()).toEqual([
      ['OCR (beginning)', 'Skipped'],
      ['Classification', 'Skipped'],
      ['Extraction', 'Runs'],
      ['Assessment', 'Runs'],
      ['Apply Review Changes', 'Runs'],
      ['Policy Rule Validation', 'Not enabled'],
      ['Review Routing', 'Runs'],
      ['Summarization', 'Runs'],
      ['Evaluation', 'Runs'],
    ]);
    expect(text()).toContain('Sections extracted again: 2 (pages changed)');
  });

  it('shows the send-back run when that ending is chosen, and the completion run again when it is not', async () => {
    await opened();
    chooseSendBack();

    expect(text()).toContain('Starts at Classification');
    expect(stepRows()).toEqual([
      ['OCR (beginning)', 'Skipped'],
      ['Classification', 'Runs'],
      ['Extraction', 'Runs'],
      ['Assessment', 'Runs'],
      ['Policy Rule Validation', 'Not enabled'],
      ['Review Routing', 'Runs'],
      ['Summarization', 'Runs'],
      ['Evaluation', 'Runs'],
    ]);
    expect(text()).not.toContain('Sections extracted again');

    chooseComplete();
    expect(text()).toContain('Starts at Extraction');
    expect(stepRows()).toHaveLength(COMPLETE.steps.length);
  });

  it('reads both endings once, for the item and its review cycle', async () => {
    await opened({ objectKey: 'in/claim.pdf', cycleId: 'c7' });
    chooseSendBack();
    chooseComplete();

    expect(getReviewCompletionPlan).toHaveBeenCalledTimes(1);
    expect(getReviewCompletionPlan).toHaveBeenCalledWith('in/claim.pdf', 'c7');
  });

  it('asks for no step: the ending is the only choice, for both endings', async () => {
    await opened();
    expect(content().findAllRadioGroups()).toHaveLength(1);

    chooseSendBack();
    expect(content().findAllRadioGroups()).toHaveLength(1);
    expect(content().findSelect()).toBeNull();
  });

  it('shows a loading state while the run is read, and completing does not wait for it', async () => {
    let answer: (plan: ReviewCompletionPlan) => void = () => undefined;
    getReviewCompletionPlan.mockReturnValue(new Promise<ReviewCompletionPlan>((resolve) => (answer = resolve)));
    const { onComplete } = open();

    expect(content().findStatusIndicator()!.getElement().textContent).toBe('Loading the steps of the run');
    expect(content().findList()).toBeNull();
    expect(primary().isDisabled()).toBe(false);
    primary().click();
    expect(onComplete).toHaveBeenCalledTimes(1);

    await act(async () => answer(PLAN));
    expect(stepRows()).toHaveLength(COMPLETE.steps.length);
  });

  it('says so when the run cannot be read, offers Retry, and still completes the review', async () => {
    getReviewCompletionPlan.mockRejectedValueOnce({ errors: [{ message: 'NOT_LEASE_HOLDER: this item is assigned to someone else' }] });
    const { onComplete } = await opened();

    expect(alerts()).toEqual([
      { header: 'The steps of the run could not be loaded', body: 'NOT_LEASE_HOLDER: this item is assigned to someone else' },
    ]);
    expect(content().findList()).toBeNull();
    // The summary is information: without it the review can still be finished.
    expect(primary().isDisabled()).toBe(false);
    primary().click();
    expect(onComplete).toHaveBeenCalledTimes(1);

    // Retry reads again, and the steps replace the warning.
    act(() => content().findAlert()!.findActionSlot()!.findButton()!.click());
    await settled();
    expect(getReviewCompletionPlan).toHaveBeenCalledTimes(2);
    expect(alerts()).toEqual([]);
    expect(stepRows()).toHaveLength(COMPLETE.steps.length);
  });

  it('still sends the document back when the run cannot be read', async () => {
    getReviewCompletionPlan.mockRejectedValue(new Error('offline'));
    const { onSendBack } = await opened();
    chooseSendBack();

    expect(alerts().map((alert) => alert.header)).toEqual([null, 'The steps of the run could not be loaded']);
    primary().click();
    expect(onSendBack).toHaveBeenCalledTimes(1);
  });
});

describe('sending a document back', () => {
  it('warns that the corrections and the page regrouping are discarded and says where processing starts again, and the button says Send back', async () => {
    await opened();
    chooseSendBack();

    expect(alerts()).toEqual([
      {
        header: null,
        body: 'The corrections and any page regrouping are discarded, and the document is processed again from classification (from the beginning in BDA mode).',
      },
    ]);
    expect(primary().getElement().textContent).toBe('Send back');
  });

  it('says in the option that the document is processed again without this review, with no step to choose', async () => {
    await opened();

    expect(
      outcomes()
        .findButtons()
        .map((b) => b.findLabel().getElement().textContent),
    ).toEqual([
      'Complete the reviewYour corrections are kept and processing continues with them.',
      'Send back for reprocessingThe document is processed again without this review. It can return to a review queue afterwards.',
    ]);
    expect(text()).not.toMatch(/step you choose/);
  });

  it('sends no step and no note unless a note is written', async () => {
    const { onSendBack, onComplete } = await opened();
    chooseSendBack();

    primary().click();

    expect(onSendBack.mock.calls).toEqual([[undefined]]);
    expect(onComplete).not.toHaveBeenCalled();
  });

  it('sends the note, trimmed, and nothing else', async () => {
    const { onSendBack } = await opened();
    chooseSendBack();
    act(() => content().findTextarea()!.setTextareaValue('  split is wrong \n'));

    primary().click();

    expect(onSendBack.mock.calls).toEqual([['split is wrong']]);
  });

  it('sends no note for one that is only spaces', async () => {
    const { onSendBack } = await opened();
    chooseSendBack();
    act(() => content().findTextarea()!.setTextareaValue('   '));

    primary().click();

    expect(onSendBack.mock.calls).toEqual([[undefined]]);
  });

  it('labels the note, so it can be reached and named without a pointer', async () => {
    await opened();
    chooseSendBack();

    const field = content()
      .findAllFormFields()
      .find((f) => f.findLabel()?.getElement().textContent === 'Note (optional)')!;
    const textarea = field.findControl()!.findTextarea()!.findNativeTextarea().getElement();
    const label = field.findLabel()!.getElement();
    expect(textarea.getAttribute('aria-labelledby')).toContain(label.id);
  });

  it('refuses a note longer than the review history keeps', async () => {
    const { onSendBack } = await opened();
    chooseSendBack();
    act(() =>
      content()
        .findTextarea()!
        .setTextareaValue('x'.repeat(MAX_NOTE_LENGTH + 1)),
    );

    expect(text()).toContain(`Keep the note to ${MAX_NOTE_LENGTH.toLocaleString('en')} characters.`);
    expect(primary().isDisabled()).toBe(true);
    fireEvent.click(primary().getElement());
    expect(onSendBack).not.toHaveBeenCalled();

    // A note of exactly the limit is accepted.
    act(() => content().findTextarea()!.setTextareaValue('x'.repeat(MAX_NOTE_LENGTH)));
    expect(primary().isDisabled()).toBe(false);
  });

  it('shows why the last send-back did not go through', async () => {
    await opened({ error: 'Failed to send the document back: the document has a newer review cycle' });

    expect(alerts()).toEqual([
      { header: 'The document was not sent back', body: 'Failed to send the document back: the document has a newer review cycle' },
    ]);
  });
});

describe('while an outcome is carried out', () => {
  it.each(['complete', 'send-back'] as const)('cannot be changed, cancelled or dismissed (%s)', async (busy) => {
    const { onCancel, onComplete, onSendBack, rerender } = await opened();
    chooseSendBack();
    rerender({ busy });

    expect(
      outcomes()
        .findButtons()
        .map((b) => b.findNativeInput().getElement().disabled),
    ).toEqual([true, true]);
    expect(content().findTextarea()!.findNativeTextarea().getElement().disabled).toBe(true);
    expect(footerButtons()[0].isDisabled()).toBe(true);
    expect(primary().findLoadingIndicator()).not.toBeNull();

    modal().findDismissButton().click();
    fireEvent.click(primary().getElement());

    expect(onCancel).not.toHaveBeenCalled();
    expect(onComplete).not.toHaveBeenCalled();
    expect(onSendBack).not.toHaveBeenCalled();
  });
});

describe('closing and reopening', () => {
  it('cancels from the button and from the dismiss control', async () => {
    const { onCancel } = await opened();

    footerButtons()[0].click();
    modal().findDismissButton().click();

    expect(onCancel).toHaveBeenCalledTimes(2);
  });

  it('starts from Complete again, with an empty note, and reads the run again', async () => {
    const { rerender } = await opened();
    chooseSendBack();
    act(() => content().findTextarea()!.setTextareaValue('a note'));

    rerender({ visible: false });
    rerender({ visible: true });
    await settled();

    expect(outcomes().findInputByValue('complete')!.getElement().checked).toBe(true);
    expect(text()).toContain('Starts at Extraction');
    // The document may have been regrouped meanwhile: the run is read at each opening.
    expect(getReviewCompletionPlan).toHaveBeenCalledTimes(2);
    chooseSendBack();
    expect(content().findTextarea()!.findNativeTextarea().getElement().value).toBe('');
  });
});
