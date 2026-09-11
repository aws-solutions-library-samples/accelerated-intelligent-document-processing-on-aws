// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The remove confirmation says what is about to happen, in the terms that matter:
 * which documents, whether any carry reviewed labels (work that cannot be recovered),
 * and whether the set will be left empty.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import React from 'react';
import { describe, expect, it, vi } from 'vitest';

import RemoveDocumentsModal from '../RemoveDocumentsModal';
import type { TestSetDocumentItem } from '../TestSetDetail';

const doc = (name: string, labelSource: string | null = null): TestSetDocumentItem => ({
  objectKey: name,
  inputKey: `set/input/${name}`,
  sections: [],
  labelSource,
});

const renderModal = (documents: TestSetDocumentItem[], remaining = 5, onConfirm = vi.fn()) => {
  render(
    <RemoveDocumentsModal
      visible
      documents={documents}
      remaining={remaining}
      submitting={false}
      onDismiss={vi.fn()}
      onConfirm={onConfirm}
    />,
  );
  return onConfirm;
};

describe('RemoveDocumentsModal', () => {
  it('names the documents and counts them on the button', () => {
    const onConfirm = renderModal([doc('a.pdf'), doc('b.pdf')]);
    expect(screen.getByText('a.pdf')).toBeTruthy();
    expect(screen.getByText('b.pdf')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /Remove 2 documents/ }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it('lists ten and collapses the rest to a count', () => {
    renderModal(Array.from({ length: 13 }, (_, i) => doc(`doc${i}.pdf`)));
    expect(screen.getByText('doc9.pdf')).toBeTruthy();
    expect(screen.queryByText('doc10.pdf')).toBeNull();
    expect(screen.getByText('+ 3 more')).toBeTruthy();
  });

  it('warns about reviewed labels only when the selection carries some', () => {
    const { unmount } = render(
      <RemoveDocumentsModal
        visible
        documents={[doc('a.pdf'), doc('b.pdf', 'draft-machine')]}
        remaining={3}
        submitting={false}
        onDismiss={vi.fn()}
        onConfirm={vi.fn()}
      />,
    );
    expect(screen.queryByText(/reviewed labels/)).toBeNull();
    unmount();

    renderModal([doc('a.pdf', 'reviewed-human'), doc('b.pdf')]);
    expect(screen.getByText(/1 of these carries reviewed labels/)).toBeTruthy();
  });

  it('says when the set will be left empty, and only then', () => {
    const { unmount } = render(
      <RemoveDocumentsModal visible documents={[doc('a.pdf')]} remaining={1} submitting={false} onDismiss={vi.fn()} onConfirm={vi.fn()} />,
    );
    expect(screen.queryByText(/removes every document/)).toBeNull();
    unmount();

    renderModal([doc('a.pdf')], 0);
    expect(screen.getByText(/This removes every document\. The set stays, empty/)).toBeTruthy();
  });

  it('keeps published versions out of the blast radius, in words', () => {
    renderModal([doc('a.pdf')]);
    expect(screen.getByText(/Versions you have already published are not affected/)).toBeTruthy();
  });
});
