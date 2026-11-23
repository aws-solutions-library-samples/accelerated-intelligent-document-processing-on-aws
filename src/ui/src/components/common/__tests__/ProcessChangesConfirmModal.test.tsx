// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The confirmation before Save and Re-extract. What has to hold: it lists each section it
 * will extract again, with its class and pages, and says what that does: their values are
 * replaced, their classes recorded as confirmed, a pending review completed, Policy Rule
 * Validation cleared, and the other sections left as they are; it reads no planned run.
 *
 * Before Process Changes on a document outside a review, it shows what the server says
 * the run will do (the step it starts at, each step with its state, the sections
 * extracted again), read for the document while the dialog is open. The dialog holds no
 * step of its own: with no answer it names none. While the steps are read a loading
 * state shows, and when they cannot be read a warning with Retry shows; in both cases
 * the changes can still be processed. The new test-set version reads no planned run.
 */

import React from 'react';
import { act, waitFor } from '@testing-library/react';
import { renderWithIntl as render } from '../../../test-utils/renderWithIntl';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const getProcessChangesPlan = vi.fn();
vi.mock('../../run-plan/run-plan-api', () => ({
  getProcessChangesPlan: (...a: unknown[]) => getProcessChangesPlan(...a),
}));

import type { PlannedRun } from '../../../graphql/generated/operation-types';
import ProcessChangesConfirmModal from '../ProcessChangesConfirmModal';

type Props = React.ComponentProps<typeof ProcessChangesConfirmModal>;

/** Field edits in Pipeline mode: the run starts at policy rule validation, and evaluation is switched off. */
const FIELD_EDITS: PlannedRun = {
  mode: 'pipeline',
  startStep: 'policy_rule_validation',
  steps: [
    { id: 'beginning', label: 'Beginning', state: 'skipped' },
    { id: 'classification', label: 'Classification', state: 'skipped' },
    { id: 'extraction', label: 'Extraction', state: 'skipped' },
    { id: 'assessment', label: 'Assessment', state: 'skipped' },
    { id: 'policy_rule_validation', label: 'Policy Rule Validation', state: 'run' },
    { id: 'review_routing', label: 'Review Routing', state: 'run' },
    { id: 'summarization', label: 'Summarization', state: 'run' },
    { id: 'evaluation', label: 'Evaluation', state: 'not_enabled' },
  ],
  sections: [],
};

/** A document with a section that has no extracted values yet: the run extracts it first. */
const EXTRACTS: PlannedRun = {
  mode: 'pipeline',
  startStep: 'extraction',
  steps: [
    { id: 'beginning', label: 'Beginning', state: 'skipped' },
    { id: 'classification', label: 'Classification', state: 'skipped' },
    { id: 'extraction', label: 'Extraction', state: 'run' },
    { id: 'assessment', label: 'Assessment', state: 'run' },
    { id: 'policy_rule_validation', label: 'Policy Rule Validation', state: 'run' },
    { id: 'review_routing', label: 'Review Routing', state: 'run' },
    { id: 'summarization', label: 'Summarization', state: 'run' },
    { id: 'evaluation', label: 'Evaluation', state: 'run' },
  ],
  sections: [{ sectionId: '3', cause: 'no_result' }],
};

const modal = () => createWrapper(document.body).findModal()!;
const content = () => modal().findContent();
const text = () => content().getElement().textContent ?? '';
const footerButtons = () => modal().findFooter()!.findAllButtons();
const primary = () => footerButtons().at(-1)!;
/** The steps the dialog lists, in order: each one's name and what the run does with it. */
const stepRows = () =>
  content()
    .findList()!
    .findItems()
    .map((item) => [item.findContent().getElement().textContent, item.findActions()!.getElement().textContent]);
/** The warnings the dialog shows, each as its header (when it has one) and its text. */
const alerts = () =>
  content()
    .findAllAlerts()
    .map((alert) => ({
      header: alert.findHeader()?.getElement().textContent ?? null,
      body: alert.findContent().getElement().textContent,
    }));

/** Render the dialog. It reads the plan as soon as it is open; `settled` waits for the answer. */
const open = (props: Partial<Props> = {}) => {
  const handlers = { onConfirm: vi.fn(), onCancel: vi.fn() };
  render(<ProcessChangesConfirmModal visible objectKey="in/w2.pdf" {...handlers} {...props} />);
  return handlers;
};
const settled = () => waitFor(() => expect(content().findStatusIndicator('[class*="status-loading"]')).toBeNull());
/** Render the dialog and wait for the plan (or the failure to read it). */
const opened = async (props: Partial<Props> = {}) => {
  const handlers = open(props);
  await settled();
  return handlers;
};

beforeEach(() => {
  getProcessChangesPlan.mockReset().mockResolvedValue(FIELD_EDITS);
});

describe('ProcessChangesConfirmModal for Save and Re-extract', () => {
  it('lists the sections and what re-extracting them does', () => {
    render(
      <ProcessChangesConfirmModal
        visible
        reextractSections={[
          { sectionId: '1', documentClass: 'FieldTicket', pageIds: [1, 2, 3] },
          { sectionId: '3', documentClass: 'Invoice', pageIds: [5] },
        ]}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(modal().findHeader().getElement().textContent).toBe('Confirm Reprocessing');
    const items = [...content().getElement().querySelectorAll('li')].map((li) => li.textContent);
    expect(items).toEqual([
      'Section 1: FieldTicket, pages 1, 2, 3',
      'Section 3: Invoice, pages 5',
      'Replace their extracted values, including any corrected by hand',
      'Record their classes as confirmed (class confidence 1.0)',
      "Mark this document's pending review as completed, if it has one",
      "Clear the document's Policy Rule Validation results",
      "Leave the other sections' values as they are; OCR and classification are not repeated",
    ]);
    expect(text()).toContain('You are about to save the new page grouping, then extract these sections again:');
    expect(footerButtons().map((b) => b.getElement().textContent)).toEqual(['Cancel', 'Confirm & Process']);
  });

  it('reads no planned run: it says itself which sections are extracted again', () => {
    open({ reextractSections: [{ sectionId: '1', documentClass: 'Invoice', pageIds: [1] }] });

    expect(getProcessChangesPlan).not.toHaveBeenCalled();
    expect(text()).not.toContain('Steps of the run');
  });
});

describe('ProcessChangesConfirmModal for Process Changes on a document', () => {
  it('shows the step the run starts at and each step with its state, as the server answers them', async () => {
    const { onConfirm } = await opened();

    expect(modal().findHeader().getElement().textContent).toBe('Confirm Reprocessing');
    expect(text()).toContain(
      'The document is processed again with your saved changes. The steps before the one the run starts at are not repeated: what they produced is kept.',
    );
    expect(text()).toContain('Steps of the run');
    expect(text()).toContain('Starts at Policy Rule Validation');
    expect(stepRows()).toEqual([
      ['OCR (beginning)', 'Skipped'],
      ['Classification', 'Skipped'],
      ['Extraction', 'Skipped'],
      ['Assessment', 'Skipped'],
      ['Policy Rule Validation', 'Runs'],
      ['Review Routing', 'Runs'],
      ['Summarization', 'Runs'],
      ['Evaluation', 'Not enabled'],
    ]);
    expect(text()).not.toContain('Sections extracted again');
    expect(alerts()).toEqual([]);
    expect(footerButtons().map((b) => b.getElement().textContent)).toEqual(['Cancel', 'Confirm & Process']);

    primary().click();
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it('reads the steps once, for the document on the page', async () => {
    await opened({ objectKey: 'in/claim.pdf' });

    expect(getProcessChangesPlan.mock.calls).toEqual([['in/claim.pdf']]);
  });

  it('holds no step of its own: the start and the sections are whatever the server answers', async () => {
    getProcessChangesPlan.mockResolvedValue(EXTRACTS);
    await opened();

    expect(text()).toContain('Starts at Extraction');
    expect(text()).not.toContain('Starts at Policy Rule Validation');
    expect(stepRows().filter(([, state]) => state === 'Runs')).toHaveLength(6);
    expect(text()).toContain('Sections extracted again: 3 (no result)');
  });

  it('names no step until the server has answered, and none of the text it replaced', async () => {
    getProcessChangesPlan.mockReturnValue(new Promise(() => undefined));
    open();

    expect(text()).not.toMatch(/restarts at|Starts at|Policy Rule Validation/);
    expect(text()).not.toMatch(/evaluation and summarization|No section structure changes/);
    expect(content().findList()).toBeNull();
  });

  it('shows a loading state while the steps are read, and confirming does not wait for it', async () => {
    let answer: (plan: PlannedRun) => void = () => undefined;
    getProcessChangesPlan.mockReturnValue(new Promise<PlannedRun>((resolve) => (answer = resolve)));
    const { onConfirm } = open();

    expect(content().findStatusIndicator()!.getElement().textContent).toBe('Loading the steps of the run');
    expect(content().findList()).toBeNull();
    expect(primary().isDisabled()).toBe(false);
    primary().click();
    expect(onConfirm).toHaveBeenCalledTimes(1);

    await act(async () => answer(FIELD_EDITS));
    expect(stepRows()).toHaveLength(FIELD_EDITS.steps.length);
  });

  it('says so when the steps cannot be read, offers Retry, and the changes can still be processed', async () => {
    getProcessChangesPlan.mockRejectedValueOnce({
      errors: [{ message: 'QUEUE_MANAGED: this document is in a review queue; work it from Review Queues' }],
    });
    const { onConfirm } = await opened();

    expect(alerts()).toEqual([
      {
        header: 'The steps of the run could not be loaded',
        body: 'QUEUE_MANAGED: this document is in a review queue; work it from Review Queues',
      },
    ]);
    expect(content().findList()).toBeNull();
    // The list is information: without it the changes can still be processed.
    expect(primary().isDisabled()).toBe(false);
    primary().click();
    expect(onConfirm).toHaveBeenCalledTimes(1);

    // Retry reads again, and the steps replace the warning.
    act(() => content().findAlert()!.findActionSlot()!.findButton()!.click());
    await settled();
    expect(content().findAlert()).toBeNull();
    expect(getProcessChangesPlan).toHaveBeenCalledTimes(2);
    expect(stepRows()).toHaveLength(FIELD_EDITS.steps.length);
  });

  it('reads nothing and lists nothing without a document', () => {
    open({ objectKey: undefined });

    expect(getProcessChangesPlan).not.toHaveBeenCalled();
    expect(text()).toContain('The document is processed again with your saved changes.');
    expect(text()).not.toContain('Steps of the run');
    expect(primary().isDisabled()).toBe(false);
  });

  it('shows the confirm button working while the changes are processed', async () => {
    await opened({ busy: true });

    expect(primary().findLoadingIndicator()).not.toBeNull();
  });
});

describe('ProcessChangesConfirmModal for a new test-set version', () => {
  it('says what opening the version does, and reads no planned run', () => {
    open({ variant: 'version' });

    expect(modal().findHeader().getElement().textContent).toBe('Open a new version and save ground truth');
    expect(text()).toContain('opens the next version of this test set');
    expect(text()).not.toContain('Steps of the run');
    expect(getProcessChangesPlan).not.toHaveBeenCalled();
    expect(footerButtons().map((b) => b.getElement().textContent)).toEqual(['Cancel', 'Process Changes']);
  });
});
