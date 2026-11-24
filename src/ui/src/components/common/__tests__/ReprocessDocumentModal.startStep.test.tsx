// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Reprocess dialog's start step. What has to hold: the steps offered are the
 * server's answer for the selection under the chosen profile and revision, asked again
 * when either changes; a step is offered when every selected document can start there,
 * and otherwise says how many cannot and why; resuming is offered only when every
 * document failed; Reprocess sends the chosen step; when every document was queued the
 * dialog closes, and when some were not it lists each document's result and reason.
 */

import React from 'react';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { renderWithIntl } from '../../../test-utils/renderWithIntl';
import type { ReprocessDocumentResult, ReprocessOption } from '../../../graphql/generated/operation-types';
import { stepsForMode } from '../../run-plan/run-steps';

const getReprocessOptions = vi.fn();
vi.mock('../../run-plan/run-plan-api', () => ({ getReprocessOptions: (...a: unknown[]) => getReprocessOptions(...a) }));
vi.mock('../../../hooks/use-configuration-versions', () => ({
  default: () => ({
    versions: [{ versionName: 'claims', isActive: true }, { versionName: 'lending' }],
    getVersionOptions: () => [
      { label: 'claims (Active)', value: 'claims' },
      { label: 'lending', value: 'lending' },
    ],
  }),
}));
vi.mock('../../../hooks/use-configuration', () => ({ default: () => ({ mergedConfig: null, loading: false, error: null }) }));
// The revision selector, as a button that picks revision 2.
vi.mock('../ConfigRevisionSelector', () => ({
  default: ({ onChange }: { onChange: (revision: number | null) => void }) => (
    <button type="button" onClick={() => onChange(2)}>
      pick r2
    </button>
  ),
}));

import ReprocessDocumentModal from '../ReprocessDocumentModal';

const PIPELINE = stepsForMode('pipeline').map((step) => step.id);
const BDA = stepsForMode('bda').map((step) => step.id);
const answer = (
  objectKey: string,
  mode: 'pipeline' | 'bda' = 'pipeline',
  refused: Record<string, string | null> = {},
): ReprocessOption => ({
  objectKey,
  mode,
  defaultStep: 'beginning',
  steps: (mode === 'bda' ? BDA : PIPELINE).map((id) => ({
    id,
    available: !(id in refused),
    skippable: false,
    reason: refused[id] ?? null,
  })),
});

type Item = { objectKey: string; objectStatus?: string };
const ONE: Item[] = [{ objectKey: 'in/a.pdf', objectStatus: 'COMPLETED' }];
const TWO: Item[] = [...ONE, { objectKey: 'in/b.pdf', objectStatus: 'COMPLETED' }];

const open = (selectedItems: Item[] = ONE, onConfirm: (...a: unknown[]) => unknown = vi.fn().mockResolvedValue(null)) => {
  const onDismiss = vi.fn();
  const element = (visible: boolean, items: Item[] = selectedItems) => (
    <ReprocessDocumentModal
      visible={visible}
      onDismiss={onDismiss}
      onConfirm={onConfirm as React.ComponentProps<typeof ReprocessDocumentModal>['onConfirm']}
      selectedItems={items}
    />
  );
  const { rerender } = renderWithIntl(element(true));
  return { onDismiss, onConfirm, show: (visible: boolean, items?: Item[]) => rerender(element(visible, items)) };
};

const modal = () => createWrapper(document.body).findModal()!;
const picker = () => modal().findContent().findRadioGroup();
const steps = () => picker()!.findButtons();
const stepIds = () => steps().map((b) => b.findNativeInput().getElement().value);
const step = (id: string) => steps().find((b) => b.findNativeInput().getElement().value === id)!;
const enabledSteps = () =>
  steps()
    .filter((b) => !b.findNativeInput().getElement().disabled)
    .map((b) => b.findNativeInput().getElement().value);
const reprocess = () => modal().findFooter()!.findAllButtons().at(-1)!;
const loaded = () => waitFor(() => expect(picker()).not.toBeNull());

beforeEach(() => {
  document.body.innerHTML = '';
  getReprocessOptions.mockReset().mockImplementation(async (keys: string[]) => keys.map((key) => answer(key)));
});

describe('the steps offered', () => {
  it('are asked for the selection under the profile the dialog opens on', async () => {
    getReprocessOptions.mockReturnValue(new Promise(() => undefined));
    open(TWO);

    await waitFor(() => expect(getReprocessOptions).toHaveBeenCalledTimes(1));
    expect(getReprocessOptions).toHaveBeenCalledWith(['in/a.pdf', 'in/b.pdf'], 'claims', undefined);
    // Until the answer arrives the dialog says it is checking, and offers no step.
    expect(modal().findContent().findStatusIndicator()!.getElement().textContent).toBe('Checking which steps the run can start at');
    expect(picker()).toBeNull();
  });

  it('are the steps of the documents’ mode, starting at the beginning', async () => {
    open();
    await loaded();

    expect(stepIds()).toEqual(PIPELINE);
    expect(enabledSteps()).toEqual(PIPELINE);
    expect(step('beginning').findNativeInput().getElement().checked).toBe(true);
    expect(step('beginning').findLabel().getElement().textContent).toBe('OCR (beginning)');
  });

  it('are BDA mode’s when every document is in BDA mode', async () => {
    getReprocessOptions.mockImplementation(async (keys: string[]) => keys.map((key) => answer(key, 'bda')));
    open(TWO);
    await loaded();

    expect(stepIds()).toEqual(BDA);
  });

  it('are asked again when the revision changes, and when the profile changes', async () => {
    open();
    await loaded();

    fireEvent.click(screen.getByRole('button', { name: 'pick r2' }));
    await waitFor(() => expect(getReprocessOptions).toHaveBeenCalledTimes(2));
    expect(getReprocessOptions).toHaveBeenLastCalledWith(['in/a.pdf'], 'claims', 2);

    const profile = modal().findContent().findSelect()!;
    act(() => profile.openDropdown());
    act(() => profile.selectOptionByValue('lending'));
    await waitFor(() => expect(getReprocessOptions).toHaveBeenCalledTimes(3));
    expect(getReprocessOptions).toHaveBeenLastCalledWith(['in/a.pdf'], 'lending', 2);
  });

  it('are those of the newest request: an earlier answer that arrives late is dropped', async () => {
    let answerFirst: (value: ReprocessOption[]) => void = () => undefined;
    getReprocessOptions
      .mockReturnValueOnce(new Promise<ReprocessOption[]>((resolve) => (answerFirst = resolve)))
      .mockResolvedValueOnce([answer('in/a.pdf', 'pipeline', { extraction: 'OTHER_PROFILE_BEGINNING_ONLY' })]);
    open();
    await waitFor(() => expect(getReprocessOptions).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole('button', { name: 'pick r2' }));
    await loaded();
    expect(enabledSteps()).not.toContain('extraction');

    await act(async () => answerFirst([answer('in/a.pdf')]));
    expect(enabledSteps()).not.toContain('extraction');
  });

  it('are not asked for while the dialog is closed, and start over when it reopens', async () => {
    const { show } = open();
    await loaded();
    act(() => step('summarization').findNativeInput().click());
    expect(step('summarization').findNativeInput().getElement().checked).toBe(true);

    show(false);
    await act(async () => {});
    expect(getReprocessOptions).toHaveBeenCalledTimes(1);

    show(true);
    await loaded();
    expect(getReprocessOptions).toHaveBeenCalledTimes(2);
    expect(step('beginning').findNativeInput().getElement().checked).toBe(true);
  });

  it('are not asked again when the page hands the dialog the same documents in a new array', async () => {
    const { show } = open(TWO);
    await loaded();

    show(true, [...TWO.map((item) => ({ ...item }))]);
    await act(async () => {});

    expect(getReprocessOptions).toHaveBeenCalledTimes(1);
  });
});

describe('a step some documents cannot start at', () => {
  it('is withheld, saying how many documents cannot and the reason of the first', async () => {
    getReprocessOptions.mockResolvedValue([
      answer('in/a.pdf'),
      answer('in/b.pdf', 'pipeline', { extraction: 'OUTPUT_MISSING', assessment: 'OUTPUT_MISSING' }),
    ]);
    open(TWO);
    await loaded();

    expect(enabledSteps()).toEqual(PIPELINE.filter((id) => !['extraction', 'assessment'].includes(id)));
    const description = step('extraction').findDescription()!.getElement().textContent ?? '';
    expect(description).toMatch(/^Not available for 1 of 2 documents\. \S/);
    // The reason is a sentence, not the server's code.
    expect(description).not.toContain('OUTPUT_MISSING');
  });

  it('gives the reason alone for a single document', async () => {
    getReprocessOptions.mockResolvedValue([answer('in/a.pdf', 'pipeline', { extraction: 'OUTPUT_MISSING' })]);
    open();
    await loaded();

    const description = step('extraction').findDescription()!.getElement().textContent ?? '';
    expect(description).not.toMatch(/Not available for \d+ of/);
    expect(description.length).toBeGreaterThan(0);
  });

  it('includes the steps a BDA document does not have, among Pipeline documents', async () => {
    getReprocessOptions.mockResolvedValue([answer('in/a.pdf'), answer('in/b.pdf', 'bda')]);
    open(TWO);
    await loaded();

    expect(stepIds()).toEqual(PIPELINE);
    expect(enabledSteps()).toEqual(BDA);
    expect(step('classification').findDescription()!.getElement().textContent).toBe(
      "Not available for 1 of 2 documents. This step does not exist in the document's processing mode.",
    );
  });

  it('cannot be the one Reprocess sends: a choice a new answer withholds goes back to the beginning', async () => {
    getReprocessOptions
      .mockResolvedValueOnce([answer('in/a.pdf')])
      .mockResolvedValueOnce([answer('in/a.pdf', 'pipeline', { extraction: 'OTHER_PROFILE_BEGINNING_ONLY' })]);
    const onConfirm = vi.fn().mockResolvedValue(null);
    open(ONE, onConfirm);
    await loaded();
    act(() => step('extraction').findNativeInput().click());

    fireEvent.click(screen.getByRole('button', { name: 'pick r2' }));
    await waitFor(() => expect(step('extraction').findNativeInput().getElement().disabled).toBe(true));
    await waitFor(() => expect(step('beginning').findNativeInput().getElement().checked).toBe(true));

    reprocess().click();
    await waitFor(() => expect(onConfirm).toHaveBeenCalledWith('claims', 2, 'beginning'));
  });

  it('leaves nothing to reprocess when not even the beginning is available', async () => {
    const everyStep = Object.fromEntries(PIPELINE.map((id) => [id, 'RUN_IN_PROGRESS']));
    getReprocessOptions.mockResolvedValue([answer('in/a.pdf', 'pipeline', everyStep)]);
    open();
    await loaded();

    expect(enabledSteps()).toEqual([]);
    expect(reprocess().isDisabled()).toBe(true);
  });
});

describe('resuming a failed run', () => {
  const failed = (key: string): Item => ({ objectKey: key, objectStatus: 'FAILED' });

  it('is offered when every selected document failed', async () => {
    const onConfirm = vi.fn().mockResolvedValue(null);
    open([failed('in/a.pdf'), failed('in/b.pdf')], onConfirm);
    await loaded();

    expect(stepIds()).toEqual([...PIPELINE, 'resume']);
    expect(step('resume').findNativeInput().getElement().disabled).toBe(false);
    act(() => step('resume').findNativeInput().click());
    reprocess().click();

    await waitFor(() => expect(onConfirm).toHaveBeenCalledWith('claims', undefined, 'resume'));
  });

  it.each([
    ['one of them did not fail', [failed('in/a.pdf'), { objectKey: 'in/b.pdf', objectStatus: 'COMPLETED' }]],
    ['the status is not known', [{ objectKey: 'in/a.pdf' }]],
  ])('is not offered when %s', async (_why, items) => {
    open(items as Item[]);
    await loaded();

    expect(stepIds()).toEqual(PIPELINE);
  });
});

describe('Reprocess', () => {
  it('sends the profile, the revision and the chosen step', async () => {
    const onConfirm = vi.fn().mockResolvedValue(null);
    open(ONE, onConfirm);
    await loaded();
    fireEvent.click(screen.getByRole('button', { name: 'pick r2' }));
    await waitFor(() => expect(getReprocessOptions).toHaveBeenCalledTimes(2));
    await loaded();
    act(() => step('policy_rule_validation').findNativeInput().click());

    reprocess().click();

    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1));
    expect(onConfirm).toHaveBeenCalledWith('claims', 2, 'policy_rule_validation');
  });

  it('closes the dialog when every document was queued', async () => {
    const results: ReprocessDocumentResult[] = [
      { objectKey: 'in/a.pdf', status: 'QUEUED', startStep: 'beginning' },
      { objectKey: 'in/b.pdf', status: 'QUEUED', startStep: 'beginning' },
    ];
    const { onDismiss } = open(TWO, vi.fn().mockResolvedValue(results));
    await loaded();

    reprocess().click();

    await waitFor(() => expect(onDismiss).toHaveBeenCalledTimes(1));
    expect(modal().findHeader().getElement().textContent).not.toBe('Reprocess results');
  });

  it('closes the dialog when the request itself failed, which the page reports', async () => {
    const { onDismiss } = open(ONE, vi.fn().mockResolvedValue(null));
    await loaded();

    reprocess().click();

    await waitFor(() => expect(onDismiss).toHaveBeenCalledTimes(1));
  });

  it('lists each document’s result and reason when some were not queued', async () => {
    const results: ReprocessDocumentResult[] = [
      { objectKey: 'in/a.pdf', status: 'QUEUED', startStep: 'extraction' },
      { objectKey: 'in/b.pdf', status: 'REFUSED', startStep: 'extraction', reason: 'RUN_IN_PROGRESS', message: 'a run is in progress' },
      { objectKey: 'in/c.pdf', status: 'REFUSED', reason: 'SOMETHING_NEW', message: 'the server said so' },
      { objectKey: 'in/d.pdf', status: 'FAILED', reason: 'ClientError', message: 'Could not queue in/d.pdf to run again.' },
      { objectKey: 'in/e.pdf', status: 'FAILED' },
    ];
    const { onDismiss } = open(TWO, vi.fn().mockResolvedValue(results));
    await loaded();

    reprocess().click();
    await waitFor(() => expect(modal().findHeader().getElement().textContent).toBe('Reprocess results'));

    expect(onDismiss).not.toHaveBeenCalled();
    expect(modal().findContent().findAlert()!.findHeader()!.getElement().textContent).toBe('4 documents were not queued, of 5');
    const table = modal().findContent().findTable()!;
    expect(table.findColumnHeaders().map((h) => h.getElement().textContent)).toEqual(['Document', 'Result', 'Reason']);
    const rows = table.findRows().map((row) => row.findAll('td, th').map((cell) => cell.getElement().textContent));
    expect(rows.map((row) => row.slice(0, 2))).toEqual([
      ['in/a.pdf', 'Queued'],
      ['in/b.pdf', 'Not queued'],
      ['in/c.pdf', 'Not queued'],
      ['in/d.pdf', 'Failed'],
      ['in/e.pdf', 'Failed'],
    ]);
    const reasons = rows.map((row) => row[2]);
    expect(reasons[0]).toBe('—');
    // A code this dialog has a sentence for is shown as the sentence, not as the server's text.
    expect(reasons[1]).not.toContain('RUN_IN_PROGRESS');
    expect(reasons[1]).not.toBe('a run is in progress');
    expect((reasons[1] ?? '').length).toBeGreaterThan(0);
    // Any other code is shown with the server's message, or the code, or a dash.
    expect(reasons.slice(2)).toEqual(['the server said so', 'Could not queue in/d.pdf to run again.', '—']);

    // Nothing is left to choose, and the steps are not asked for again.
    expect(picker()).toBeNull();
    expect(getReprocessOptions).toHaveBeenCalledTimes(1);

    modal().findFooter()!.findButton()!.click();
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('counts one document that was not queued in the singular', async () => {
    const results: ReprocessDocumentResult[] = [
      { objectKey: 'in/a.pdf', status: 'QUEUED' },
      { objectKey: 'in/b.pdf', status: 'REFUSED', reason: 'RUN_IN_PROGRESS' },
    ];
    open(TWO, vi.fn().mockResolvedValue(results));
    await loaded();

    reprocess().click();

    await waitFor(() => expect(modal().findContent().findAlert()).not.toBeNull());
    expect(modal().findContent().findAlert()!.findHeader()!.getElement().textContent).toBe('1 document was not queued, of 2');
  });

  it('keeps showing the results when the page clears its selection, and starts over when reopened', async () => {
    const results: ReprocessDocumentResult[] = [{ objectKey: 'in/a.pdf', status: 'REFUSED', reason: 'RUN_IN_PROGRESS' }];
    const { show } = open(ONE, vi.fn().mockResolvedValue(results));
    await loaded();
    reprocess().click();
    await waitFor(() => expect(modal().findHeader().getElement().textContent).toBe('Reprocess results'));

    show(true, []);
    expect(modal().findContent().findTable()!.findRows()).toHaveLength(1);

    show(false, []);
    show(true, ONE);
    await loaded();
    expect(modal().findHeader().getElement().textContent).toBe('Reprocess document');
  });
});

describe('when the steps cannot be read', () => {
  it('says so, starts at the beginning, and reads them again on Retry', async () => {
    getReprocessOptions.mockRejectedValueOnce(new Error('boom'));
    const onConfirm = vi.fn().mockResolvedValue(null);
    open(ONE, onConfirm);

    await waitFor(() => expect(modal().findContent().findAlert()).not.toBeNull());
    const alert = modal().findContent().findAlert()!;
    expect(alert.findHeader()!.getElement().textContent).toBe("Couldn't check the start steps");
    expect(alert.findContent().getElement().textContent).toBe(
      'The steps these documents can start at could not be read, so the run starts at the beginning.',
    );
    expect(picker()).toBeNull();
    // Reprocessing from the beginning is still possible.
    expect(reprocess().isDisabled()).toBe(false);
    reprocess().click();
    await waitFor(() => expect(onConfirm).toHaveBeenCalledWith('claims', undefined, 'beginning'));

    act(() => alert.findActionSlot()!.findButton()!.click());
    await loaded();
    expect(getReprocessOptions).toHaveBeenCalledTimes(2);
    expect(modal().findContent().findAlert()).toBeNull();
  });

  it('forgets a step chosen before the read failed', async () => {
    getReprocessOptions.mockResolvedValueOnce([answer('in/a.pdf')]).mockRejectedValueOnce(new Error('boom'));
    const onConfirm = vi.fn().mockResolvedValue(null);
    open(ONE, onConfirm);
    await loaded();
    act(() => step('extraction').findNativeInput().click());

    fireEvent.click(screen.getByRole('button', { name: 'pick r2' }));
    await waitFor(() => expect(modal().findContent().findAlert()).not.toBeNull());
    reprocess().click();

    await waitFor(() => expect(onConfirm).toHaveBeenCalledWith('claims', 2, 'beginning'));
  });
});
