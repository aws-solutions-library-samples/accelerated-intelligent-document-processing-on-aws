// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Status column shows the stored value itself, so the web UI, the CLI, the SDK and
 * IDP Monitor all report a document's status with the same word.
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import { renderObjectStatus } from '../object-status-renderer';

describe('renderObjectStatus', () => {
  it('shows a queued review as its stored value', () => {
    render(renderObjectStatus('HITL_IN_PROGRESS'));
    expect(screen.getByText('HITL_IN_PROGRESS')).toBeInTheDocument();
  });

  it('shows a rejected document as REJECTED, like every other raw value', () => {
    render(renderObjectStatus('REJECTED'));
    expect(screen.getByText('REJECTED')).toBeInTheDocument();
  });

  it('keeps pipeline stages as they are', () => {
    render(renderObjectStatus('EXTRACTING'));
    expect(screen.getByText('EXTRACTING')).toBeInTheDocument();
  });
});

describe('a document that is being processed', () => {
  /** The kind of indicator a status is shown with (Cloudscape marks it in a class name). */
  const indicatorType = (status: string): string => {
    const { container, unmount } = render(renderObjectStatus(status));
    const className = createWrapper(container).findStatusIndicator()!.getElement().className;
    unmount();
    return /status-(in-progress|info|success|error|stopped|pending|warning)/.exec(className)?.[1] ?? className;
  };

  it.each([
    'RUNNING',
    'PREPROCESSING',
    'OCR',
    'CLASSIFYING',
    'EXTRACTING',
    'ASSESSING',
    'POSTPROCESSING',
    'RULE_VALIDATION_POLICY_CLASSIFICATION',
    'RULE_VALIDATION',
    'RULE_VALIDATION_ORCHESTRATOR',
    'SUMMARIZING',
    'EVALUATING',
  ])('shows %s as in progress', (status) => {
    expect(indicatorType(status)).toBe('in-progress');
  });

  it('shows a status it does not know as information, not as progress', () => {
    expect(indicatorType('SOMETHING_NEW')).toBe('info');
    expect(indicatorType('COMPLETED')).toBe('success');
  });
});
