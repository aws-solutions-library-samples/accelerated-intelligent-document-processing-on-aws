// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Status column shows the stored value itself, so the web UI, the CLI, the SDK and
 * IDP Monitor all report a document's status with the same word.
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

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
