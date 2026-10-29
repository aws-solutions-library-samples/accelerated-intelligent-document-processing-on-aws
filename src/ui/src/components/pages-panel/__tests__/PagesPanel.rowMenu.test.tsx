// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Document Pages table, its own actions only. What has to hold: no Edit Mode,
 * Process Changes or Confirm & Process control (editing is the page's More Actions);
 * each row has one ⋮ menu named for its page, holding View Page Text, which opens the
 * text read-only, and Edit Page Text only where the page offers it, with the reason it
 * is disabled passed through; and the data columns have a narrow fixed width while
 * Actions is last and takes the rest of the table, its header on one line.
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

/** Each rendered column's header and the width it is given (the table's own header row; a sticky header repeats it). */
const columnWidths = () =>
  Array.from(document.querySelector('table')!.querySelectorAll('thead th')).map((th) => [
    (th.textContent ?? '').trim(),
    (th as HTMLElement).style.width,
  ]);
/** A width as the browser keeps it, so `calc()` reads the same however it was written. */
const css = (width: string) => {
  const probe = document.createElement('div');
  probe.style.width = width;
  return probe.style.width;
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

  it('puts Actions last at 100px, the data columns sharing the rest of the width evenly', () => {
    render(<PagesPanel pages={PAGES as never} />);
    const share = css('calc((100% - 100px) / 4)');
    expect(columnWidths()).toEqual([
      ['Page ID', share],
      ['Class/Type', share],
      ['Class Confidence', share],
      ['Thumbnail', share],
      ['Actions', '100px'],
    ]);
    expect(pageColumnDefinitions({}, vi.fn(), EMPTY_CLASSIFICATION_INDEX).map((c) => c.id)).toEqual([
      'id',
      'class',
      'classConfidence',
      'thumbnail',
      'actions',
    ]);
  });

  it('shows the columns chosen in its preferences, remembered, the rest sharing the width', () => {
    window.localStorage.setItem('document-pages-columns', JSON.stringify({ visibleContent: ['id', 'class'] }));
    render(<PagesPanel pages={PAGES as never} />);
    expect(columnWidths()).toEqual([
      ['Page ID', css('calc((100% - 100px) / 2)')],
      ['Class/Type', css('calc((100% - 100px) / 2)')],
      ['Actions', '100px'],
    ]);
    expect(createWrapper(document.body).findTable()!.findCollectionPreferences()).not.toBeNull();
    window.localStorage.removeItem('document-pages-columns');
  });
});
