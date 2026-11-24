// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The start-step picker shows the steps of the document's processing mode, from the
 * generated catalogue, and lets the user choose one the server allows.
 */

import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { IntlProvider } from 'react-intl';
import { describe, expect, it, vi } from 'vitest';

import catalog from '../run-steps.json';
import StartStepPicker, { reasonMessage, stepMessage, type StartStepOption } from '../StartStepPicker';
import { DISPLAY_STEPS, RESUME, RUN_STEPS, stepsForMode, stepStates } from '../run-steps';

const renderPicker = (props: Partial<React.ComponentProps<typeof StartStepPicker>> = {}) => {
  const onChange = vi.fn();
  render(
    <IntlProvider locale="en">
      <StartStepPicker mode="pipeline" value="beginning" onChange={onChange} {...props} />
    </IntlProvider>,
  );
  return onChange;
};

/** The label of each radio, in order: Cloudscape names a radio through aria-labelledby. */
const labels = (): string[] =>
  screen.getAllByRole('radio').map((radio) =>
    (radio.getAttribute('aria-labelledby') ?? '')
      .split(' ')
      .map((id) => document.getElementById(id)?.textContent ?? '')
      .join(' ')
      .trim(),
  );
const radio = (name: string | RegExp): HTMLInputElement => screen.getByRole('radio', { name }) as HTMLInputElement;

describe('the steps offered', () => {
  it('shows the seven steps of Pipeline mode, the first as OCR', () => {
    renderPicker({ mode: 'pipeline' });

    expect(screen.getAllByRole('radio')).toHaveLength(7);
    expect(labels()[0]).toContain('OCR (beginning)');
    expect(labels().map((label) => label.trim())).toEqual([
      'OCR (beginning)',
      'Classification',
      'Extraction',
      'Assessment',
      'Policy Rule Validation',
      'Summarization',
      'Evaluation',
    ]);
  });

  it('shows the four steps of BDA mode, the first as the beginning', () => {
    renderPicker({ mode: 'bda' });

    expect(labels().map((label) => label.trim())).toEqual(['Beginning', 'Policy Rule Validation', 'Summarization', 'Evaluation']);
  });

  it('offers resume only when asked to', () => {
    renderPicker();
    expect(screen.queryByRole('radio', { name: /Resume from the failed step/ })).toBeNull();
  });

  it('offers resume last when asked to', () => {
    renderPicker({ includeResume: true });

    expect(screen.getAllByRole('radio')).toHaveLength(8);
    expect(labels()[7]).toContain('Resume from the failed step');
  });

  it('marks the chosen step', () => {
    renderPicker({ value: 'extraction' });

    expect(radio('Extraction').checked).toBe(true);
    expect(radio('OCR (beginning)').checked).toBe(false);
  });
});

describe('what the server allows', () => {
  const options: StartStepOption[] = [
    { id: 'beginning', available: true },
    { id: 'classification', available: true },
    { id: 'extraction', available: true },
    { id: 'assessment', available: false, reason: 'OUTPUT_MISSING' },
    { id: 'policy_rule_validation', available: false, reason: 'SOMETHING_NEW' },
    { id: 'summarization', available: false },
    // evaluation is not listed
  ];

  it('disables a step that is not available and says why', () => {
    renderPicker({ options });

    expect(radio(/Assessment/).disabled).toBe(true);
    expect(screen.getByText('The document does not have the output of the earlier steps.')).toBeTruthy();
    expect(radio('Extraction').disabled).toBe(false);
  });

  it('shows the code of a reason it has no sentence for', () => {
    renderPicker({ options });

    expect(radio(/Policy Rule Validation/).disabled).toBe(true);
    expect(screen.getByText('Not available (SOMETHING_NEW).')).toBeTruthy();
  });

  it('says a step is unavailable when the server gave no reason', () => {
    renderPicker({ options });

    expect(radio(/Summarization/).disabled).toBe(true);
    expect(screen.getAllByText('Not available for this document.').length).toBe(2);
  });

  it('says how many of several documents cannot start at a step, before the reason', () => {
    renderPicker({
      options: [
        { id: 'beginning', available: true },
        { id: 'assessment', available: false, reason: 'OUTPUT_MISSING', blocked: { count: 2, total: 5 } },
        { id: 'summarization', available: false, reason: 'SOMETHING_NEW', blocked: { count: 1, total: 1200 } },
        { id: 'evaluation', available: false, blocked: { count: 3, total: 5 } },
      ],
    });

    expect(
      screen.getByText('Not available for 2 of 5 documents. The document does not have the output of the earlier steps.'),
    ).toBeTruthy();
    expect(screen.getByText('Not available for 1 of 1,200 documents. Not available (SOMETHING_NEW).')).toBeTruthy();
    expect(screen.getByText('Not available for 3 of 5 documents. Not available for this document.')).toBeTruthy();
  });

  it('gives the reason alone when the selection is one document', () => {
    renderPicker({
      options: [
        { id: 'beginning', available: true },
        { id: 'assessment', available: false, reason: 'OUTPUT_MISSING', blocked: { count: 1, total: 1 } },
      ],
    });

    expect(screen.getByText('The document does not have the output of the earlier steps.')).toBeTruthy();
    expect(screen.queryByText(/Not available for 1 of 1/)).toBeNull();
  });

  it('does not offer a step the server did not list', () => {
    renderPicker({ options });

    expect(radio(/Evaluation/).disabled).toBe(true);
  });

  it('lets every step be chosen when there is no answer from the server', () => {
    renderPicker();

    expect(screen.getAllByRole('radio').every((input) => !(input as HTMLInputElement).disabled)).toBe(true);
  });

  it('applies the answer to resume as well', () => {
    renderPicker({ includeResume: true, options: [...options, { id: 'resume', available: false, reason: 'NOT_FAILED' }] });

    expect(radio(/Resume from the failed step/).disabled).toBe(true);
    expect(screen.getByText('The document did not fail, so there is no failed run to resume.')).toBeTruthy();
  });

  it('disables every step when the picker is disabled', () => {
    renderPicker({ disabled: true });

    expect(screen.getAllByRole('radio').every((input) => (input as HTMLInputElement).disabled)).toBe(true);
  });
});

describe('choosing', () => {
  it('reports the step that was clicked', () => {
    const onChange = renderPicker();

    fireEvent.click(radio('Extraction'));

    expect(onChange).toHaveBeenCalledWith('extraction');
  });

  it('can be reached and chosen from the keyboard', () => {
    const onChange = renderPicker();
    const extraction = radio('Extraction');

    extraction.focus();
    expect(document.activeElement).toBe(extraction);
    // Space on a focused native radio selects it: the browser turns the key into a click.
    fireEvent.keyDown(extraction, { key: ' ', code: 'Space' });
    fireEvent.click(extraction);

    expect(onChange).toHaveBeenCalledWith('extraction');
    expect(extraction.getAttribute('type')).toBe('radio');
    expect(extraction.tabIndex).not.toBe(-1);
  });

  it('does not report a step that cannot be chosen', () => {
    const onChange = renderPicker({ options: [{ id: 'extraction', available: false, reason: 'OUTPUT_MISSING' }] });

    fireEvent.click(radio(/Extraction/));

    expect(onChange).not.toHaveBeenCalled();
  });

  it('names the group for assistive technology', () => {
    renderPicker();

    expect(screen.getByRole('radiogroup', { name: 'Start at' })).toBeTruthy();
  });
});

describe('the catalogue', () => {
  it('has a message for every step id, and for resume', () => {
    for (const step of catalog.steps) {
      expect(stepMessage(step.id), step.id).toBeDefined();
    }
    expect(stepMessage(catalog.resume)).toBeDefined();
  });

  it('has a message for every display step, spelled as the catalogue spells it', () => {
    expect(catalog.display_steps.map((step) => step.id)).toEqual(['apply_review_changes', 'review_routing']);
    for (const step of catalog.display_steps) {
      expect(stepMessage(step.id)?.defaultMessage, step.id).toBe(step.label);
    }
  });

  it('has no message for something that is not a step', () => {
    expect(stepMessage('ocr')).toBeUndefined();
    expect(stepMessage('rejected')).toBeUndefined();
  });

  it('reads the display steps, which no mode offers as a start', () => {
    expect(DISPLAY_STEPS).toEqual([
      { id: 'apply_review_changes', before: 'policy_rule_validation', reviewCompletedOnly: true },
      { id: 'review_routing', before: 'summarization', reviewCompletedOnly: false },
    ]);
    const offered = [...stepsForMode('pipeline'), ...stepsForMode('bda')].map((step) => step.id);
    for (const step of DISPLAY_STEPS) {
      expect(offered).not.toContain(step.id);
      expect(offered).toContain(step.before);
    }
  });

  it('has a sentence for each refusal code and none for an unknown one', () => {
    for (const code of [
      'MALWARE_GATE',
      'RUN_IN_PROGRESS',
      'NOT_FAILED',
      'UNKNOWN_STEP',
      'STEP_NOT_IN_MODE',
      'OUTPUT_MISSING',
      'OTHER_PROFILE_BEGINNING_ONLY',
      'REVISION_HAS_PREPROCESSING_HOOK',
      'REVISION_CHANGES_MODE',
    ]) {
      expect(reasonMessage(code), code).toBeDefined();
    }
    expect(reasonMessage('SOMETHING_NEW')).toBeUndefined();
  });

  it('is read in pipeline order, with the modes and states of each step', () => {
    expect(RUN_STEPS.map((step) => step.id)).toEqual(catalog.steps.map((step) => step.id));
    expect(stepsForMode('bda').map((step) => step.id)).toEqual(['beginning', 'policy_rule_validation', 'summarization', 'evaluation']);
    expect(stepsForMode('pipeline')).toHaveLength(catalog.steps.length);
    expect(RESUME).toBe('resume');
  });

  it('knows the states that do a step, per mode', () => {
    expect(stepStates('beginning', 'pipeline')).toEqual(['OCRStep']);
    expect(stepStates('beginning', 'bda')).toContain('InvokeDataAutomationStep');
    expect(stepStates('assessment', 'bda')).toEqual([]);
    expect(stepStates('no_such_step', 'pipeline')).toEqual([]);
  });
});
