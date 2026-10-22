// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Skipping a Test Studio document asks why, as a Production item does: a reason from
 * the set's skip list (its queue settings), and an optional note, sent with the skip.
 * Its History holds the field edits only.
 */

import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const skip = vi.fn();
const exit = vi.fn();

vi.mock('../test-set-adapter', () => ({
  NOT_ASSIGNED_MESSAGE: 'not assigned',
  useTestSetAnnotation: () => ({
    queue: { testSetId: 'ts-1', skipReasons: ['Out of time', 'Needs expert review'], documents: [] },
    doc: {
      objectKey: 'a.pdf',
      inputKey: 'ts-1/input/a.pdf',
      claimedByMe: true,
      assignedTo: 'ann@example.com',
      reviewed: false,
      skipped: false,
      labelSource: 'draft-machine',
      sections: [{ sectionId: '1', baselineKey: 'b1', documentClass: 'Invoice', pageIndices: [0] }],
    },
    draft: null,
    loading: false,
    loadError: null,
    reload: vi.fn(),
    openDraft: vi.fn(),
    saveSection: vi.fn(),
    confirmSections: vi.fn(),
    skip: (...a: unknown[]) => skip(...a),
  }),
}));
vi.mock('../use-annotation-exit', () => ({ default: () => exit }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ setErrorMessage: vi.fn(), setSuccessMessage: vi.fn() }) }));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../../hooks/use-review-work', () => ({ useOptionalReviewWork: () => null }));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isAdmin: false, isAuthor: false, isReviewer: false, isAnnotator: true, canAnnotate: true, loading: false }),
}));
vi.mock('../../review-queues/ReviewPresence', () => ({ default: () => null }));

import TestSetAnnotation from '../TestSetAnnotation';

beforeEach(() => {
  skip.mockReset().mockResolvedValue(undefined);
  exit.mockReset();
  document.body.innerHTML = '';
});

const buttonByText = (text: string) =>
  createWrapper(document.body)
    .findAllButtons()
    .find((b) => b.getElement().textContent === text && !b.getElement().closest('[role="dialog"]'));

describe('Skip on a Test Studio document', () => {
  it('asks for a reason from the set and skips with it', async () => {
    render(
      <MemoryRouter>
        <TestSetAnnotation testSetId="ts-1" objectKey="a.pdf" />
      </MemoryRouter>,
    );
    buttonByText('Skip')!.click();
    expect(skip).not.toHaveBeenCalled();
    const modal = createWrapper(document.body).findModal()!;
    const select = modal.findContent().findSelect()!;
    select.openDropdown();
    select.selectOptionByValue('Needs expert review');
    modal.findContent().findTextarea()!.setTextareaValue('tax schedule');
    modal
      .findFooter()!
      .findAllButtons()
      .find((b) => b.getElement().textContent === 'Skip')!
      .click();
    await waitFor(() => expect(skip).toHaveBeenCalledWith('Needs expert review', 'tax schedule'));
    await waitFor(() => expect(exit).toHaveBeenCalled());
  });

  it('has a History button with field edits only: a Test Studio document has no review activity', () => {
    render(
      <MemoryRouter>
        <TestSetAnnotation testSetId="ts-1" objectKey="a.pdf" />
      </MemoryRouter>,
    );
    buttonByText('History')!.click();
    const tabs = createWrapper(document.body).findTabs()!;
    expect(tabs.findTabLinks().map((t) => t.getElement().textContent)).toEqual(['Field Edits']);
  });
});
