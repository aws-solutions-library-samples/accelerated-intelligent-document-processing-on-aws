// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The unified editor's History. What has to hold: Field Edits and Review Activity
 * are always tabs, and Review Activity lists the item's history; and
 * both tabs have the same fixed height.
 */

import React from 'react';
import { render } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { describe, expect, it } from 'vitest';

import EditorHistoryModal from '../EditorHistoryModal';

const tabs = () =>
  createWrapper(document.body)
    .findTabs()!
    .findTabLinks()
    .map((t) => t.getElement().textContent);

describe('EditorHistoryModal', () => {
  it('always offers Field Edits, Review Activity and Processing Report, with an empty state when there is no activity', () => {
    render(<EditorHistoryModal visible onDismiss={() => undefined} sectionData={{ inference_result: {} }} />);
    expect(createWrapper(document.body).findModal()!.findHeader().getElement().textContent).toBe('History');
    expect(tabs()).toEqual(['Field Edits', 'Review Activity', 'Processing Report']);
    createWrapper(document.body).findTabs()!.findTabLinkByIndex(2)!.click();
    expect(createWrapper(document.body).findModal()!.findContent().getElement().textContent).toContain(
      'Nothing has happened to this item yet.',
    );
  });

  it('gives both tabs the same fixed height, whatever their content', () => {
    render(
      <EditorHistoryModal
        visible
        onDismiss={() => undefined}
        sectionData={{ inference_result: {} }}
        reviewHistory={[{ at: '2026-10-22T10:00:00Z', event: 'claimed', actor: 'ann@example.com', details: null }]}
      />,
    );
    const tabGroup = createWrapper(document.body).findTabs()!;
    const height = () => (tabGroup.findTabContent()!.getElement().firstElementChild as HTMLElement).style.height;
    const first = height();
    tabGroup.findTabLinkByIndex(2)!.click();
    expect(height()).toBe(first);
    expect(first).not.toBe('');
  });

  it('adds Review Activity with the item history', () => {
    render(
      <EditorHistoryModal
        visible
        onDismiss={() => undefined}
        sectionData={{ inference_result: {} }}
        reviewHistory={[{ at: '2026-10-22T10:00:00Z', event: 'claimed', actor: 'ann@example.com', details: null }]}
      />,
    );
    expect(tabs()).toEqual(['Field Edits', 'Review Activity', 'Processing Report']);
    const modal = createWrapper(document.body).findModal()!;
    createWrapper(document.body).findTabs()!.findTabLinkByIndex(2)!.click();
    expect(modal.findContent().getElement().textContent).toContain('ann@example.com');
    expect(modal.findContent().getElement().textContent).toContain('Claimed');
  });
});
