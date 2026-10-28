// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Document Pages table, its own actions only. What has to hold: no Edit Mode,
 * Process Changes or Confirm & Process control (editing is the page's More Actions);
 * each row has one ⋮ menu named for its page, holding View Page Text, which opens the
 * text read-only, and Edit Page Text only where the page offers it, with the reason it
 * is disabled passed through; and the data columns have no width while Actions is last
 * and narrow.
 */

import React from 'react';
import { act, cleanup, render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../../../hooks/use-page-thumbnails', () => ({
  default: () => ({ urls: {}, failure: null }),
  THUMBNAIL_FAILURE_MESSAGES: { 'access-denied': 'access denied', failed: 'failed' },
}));
vi.mock('../PageTextEditorModal', () => ({
  default: ({ visible, isReadOnly, initialPageId }: { visible: boolean; isReadOnly?: boolean; initialPageId?: string }) =>
    visible ? <div data-testid="text-viewer" data-readonly={String(isReadOnly ?? true)} data-page={initialPageId} /> : null,
}));

import PagesPanel, { pageColumnDefinitions } from '../PagesPanel';
import { EMPTY_CLASSIFICATION_INDEX } from '../../common/classification-comparison-utils';
import { PAGE_NO_TEXT_REASON } from '../../document-actions/action-reasons';

const PAGES = [
  { Id: '1', Class: 'W2', TextUri: 's3://out/1.json' },
  { Id: '2', Class: 'W2' },
];

const rowMenu = (id: string) =>
  createWrapper(document.body)
    .findAllButtonDropdowns()
    .find((d) => d.findNativeButton().getElement().getAttribute('aria-label') === `Actions for page ${id}`)!;
const itemsOf = (id: string) => {
  const menu = rowMenu(id);
  act(() => menu.openDropdown());
  const items = menu.findItems().map((i) => {
    const el = i.getElement().querySelector('[role="menuitem"]') ?? i.getElement();
    // A disabled item carries its reason inside it; the label is the text without it.
    const describedBy = el.getAttribute('aria-describedby');
    const reason = describedBy ? (document.getElementById(describedBy)?.textContent ?? '') : '';
    const text = i.getElement().textContent ?? '';
    return {
      text: reason && text.endsWith(reason) ? text.slice(0, -reason.length) : text,
      disabled: el.getAttribute('aria-disabled') === 'true',
    };
  });
  act(() => menu.openDropdown());
  return items;
};

beforeEach(() => {
  cleanup();
});

describe('the Document Pages table', () => {
  it('has none of the retired table controls', () => {
    render(<PagesPanel pages={PAGES as never} />);
    const labels = createWrapper(document.body)
      .findAllButtons()
      .map((b) => b.getElement().textContent);
    for (const retired of ['Edit Mode', 'Process Changes', 'Confirm & Process']) expect(labels).not.toContain(retired);
    expect(createWrapper(document.body).findHeader()!.findActions()).toBeNull();
  });

  it('holds View Page Text only, without the page offering Edit Page Text', () => {
    render(<PagesPanel pages={PAGES as never} />);
    expect(rowMenu('1').findNativeButton().getElement().getAttribute('aria-label')).toBe('Actions for page 1');
    expect(itemsOf('1')).toEqual([{ text: 'View Page Text', disabled: false }]);
    expect(itemsOf('2')).toEqual([{ text: 'View Page Text', disabled: true }]);
  });

  it('View Page Text opens the text read-only, at that page', async () => {
    render(<PagesPanel pages={PAGES as never} />);
    const menu = rowMenu('1');
    act(() => menu.openDropdown());
    act(() => menu.findItemById('view-text')!.click());
    const viewer = await waitFor(() => document.querySelector('[data-testid="text-viewer"]')!);
    expect(viewer.getAttribute('data-readonly')).toBe('true');
    expect(viewer.getAttribute('data-page')).toBe('1');
  });

  it('holds Edit Page Text where the page offers it, opening it at that page', () => {
    const open = vi.fn();
    render(<PagesPanel pages={PAGES as never} pageTextEdit={{ open }} />);
    expect(itemsOf('1')).toEqual([
      { text: 'View Page Text', disabled: false },
      { text: 'Edit Page Text', disabled: false },
    ]);
    const menu = rowMenu('1');
    act(() => menu.openDropdown());
    act(() => menu.findItemById('edit-text')!.click());
    expect(open).toHaveBeenCalledWith('1');
  });

  it('passes the reason Edit Page Text is disabled through, and disables it for a page with no text', () => {
    render(<PagesPanel pages={PAGES as never} pageTextEdit={{ open: vi.fn(), disabledReason: 'Not now' }} />);
    expect(itemsOf('1')[1]).toEqual({ text: 'Edit Page Text', disabled: true });
    const menu = rowMenu('1');
    act(() => menu.openDropdown());
    expect(document.body.textContent).toContain('Not now');
    act(() => menu.openDropdown());
    cleanup();
    render(<PagesPanel pages={PAGES as never} pageTextEdit={{ open: vi.fn() }} />);
    expect(itemsOf('2')[1]).toEqual({ text: 'Edit Page Text', disabled: true });
    const second = rowMenu('2');
    act(() => second.openDropdown());
    expect(document.body.textContent).toContain(PAGE_NO_TEXT_REASON);
  });

  it('starts the data columns even and puts a narrow Actions column last', () => {
    const columns = pageColumnDefinitions({}, vi.fn(), EMPTY_CLASSIFICATION_INDEX);
    expect(columns[columns.length - 1]).toMatchObject({ id: 'actions', header: 'Actions', width: 60 });
    for (const c of columns.slice(0, -1)) {
      expect(c, c.id).not.toHaveProperty('width');
      expect(c, c.id).not.toHaveProperty('minWidth');
    }
  });
});
