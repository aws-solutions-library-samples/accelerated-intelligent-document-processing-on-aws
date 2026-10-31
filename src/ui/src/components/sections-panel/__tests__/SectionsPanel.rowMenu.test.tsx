// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Document Sections table, its own actions only. What has to hold: no Edit Mode, Add
 * Section, Process Changes, Edit Page Grouping or Skip All Reviews control (those are the
 * page's More Actions); each row has one ⋮ menu named for its section, holding Edit Data
 * (or View Data, for a role that can't edit) at the section's edit page, Download Data,
 * and Download Baseline only when the document has a baseline; a failed download is
 * reported through the shared notifications, not `alert`; and the data columns have no
 * a narrow fixed width while Actions is last and takes the rest of the table, its header on one line.
 */

import React from 'react';
import { act, cleanup, render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  graphql: vi.fn(),
  role: { canWrite: true, canReview: true } as Record<string, unknown>,
  setErrorMessage: vi.fn(),
}));

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => state.graphql(...a) }) }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ currentCredentials: {}, setErrorMessage: state.setErrorMessage }) }));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: { OutputBucket: 'out', EvaluationBaselineBucket: 'base' } }) }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => state.role }));

import SectionsPanel, { sectionColumnDefinitions } from '../SectionsPanel';
import { documentEditHref } from '../../../routes/constants';
import { EMPTY_CLASSIFICATION_INDEX } from '../../common/classification-comparison-utils';

const SECTIONS = [
  { Id: '1', Class: 'W2', PageIds: [1], OutputJSONUri: 's3://out/in/a.pdf/sections/1/result.json' },
  { Id: '2', Class: 'Invoice', PageIds: [2] },
];
const DOC = { objectKey: 'in/a.pdf', objectStatus: 'COMPLETED', evaluationStatus: 'COMPLETED' };

const renderPanel = (documentItem: Record<string, unknown> = DOC) =>
  render(<SectionsPanel sections={SECTIONS as never} documentItem={documentItem as never} mergedConfig={{}} />);

const rowMenu = (id: string) =>
  createWrapper(document.body)
    .findAllButtonDropdowns()
    .find((d) => d.findNativeButton().getElement().getAttribute('aria-label') === `Actions for section ${id}`);
const menuOf = (id: string) => {
  const menu = rowMenu(id)!;
  act(() => menu.openDropdown());
  return menu;
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
  state.role = { canWrite: true, canReview: true };
  state.graphql.mockReset();
  state.setErrorMessage.mockReset();
});

describe('the Document Sections table', () => {
  it('has none of the retired table controls', () => {
    renderPanel();
    const labels = createWrapper(document.body)
      .findAllButtons()
      .map((b) => b.getElement().textContent);
    for (const retired of ['Edit Mode', 'Add Section', 'Process Changes', 'Edit Page Grouping', 'Skip All Reviews', 'Save and Reprocess']) {
      expect(labels).not.toContain(retired);
    }
    expect(createWrapper(document.body).findHeader()!.findActions()).toBeNull();
  });

  it('gives each row one ⋮ menu named for its section', () => {
    renderPanel();
    expect(rowMenu('1')).toBeDefined();
    expect(rowMenu('2')).toBeDefined();
    expect(rowMenu('1')!.findNativeButton().getElement().getAttribute('aria-label')).toBe('Actions for section 1');
  });

  it('holds Edit Data at the section’s edit page, Download Data and Download Baseline', () => {
    renderPanel();
    const menu = menuOf('1');
    expect(menu.findItems().map((i) => i.getElement().textContent)).toEqual(['Edit Data', 'Download Data', 'Download Baseline']);
    expect(menu.findItemById('edit-data')!.find('a')!.getElement().getAttribute('href')).toBe(
      documentEditHref('in/a.pdf', { section: '1', from: 'document' }),
    );
  });

  it('says View Data to a role that can’t edit, and disables it for a section with no data yet', () => {
    state.role = { canWrite: false, canReview: false };
    renderPanel();
    expect(menuOf('1').findItems()[0].getElement().textContent).toBe('View Data');
    const second = menuOf('2').findItemById('edit-data')!.getElement();
    expect((second.querySelector('[role="menuitem"]') ?? second).getAttribute('aria-disabled')).toBe('true');
  });

  it('offers Download Baseline only when the document has a baseline', () => {
    renderPanel({ ...DOC, evaluationStatus: 'BASELINE_ERROR' });
    expect(
      menuOf('1')
        .findItems()
        .map((i) => i.getElement().textContent),
    ).toEqual(['Edit Data', 'Download Data']);
  });

  it('reports a failed download through the notifications', async () => {
    const alert = vi.spyOn(window, 'alert').mockImplementation(() => undefined);
    state.graphql.mockRejectedValue(new Error('denied'));
    renderPanel();
    const menu = menuOf('1');
    act(() => menu.findItemById('prediction')!.click());
    await waitFor(() => expect(state.setErrorMessage).toHaveBeenCalledWith('Failed to download prediction data: denied'));
    expect(alert).not.toHaveBeenCalled();
  });

  it('puts Actions last at 100px, the data columns sharing the rest of the width evenly', () => {
    renderPanel();
    const share = css('calc((100% - 100px) / 6)');
    expect(columnWidths()).toEqual([
      ['Section ID', share],
      ['Class/Type', share],
      ['Page IDs', share],
      ['Low Confidence Fields', share],
      ['Missing Required Fields', share],
      ['Status', share],
      ['Actions', '100px'],
    ]);
    expect(sectionColumnDefinitions(DOC as never, {}, EMPTY_CLASSIFICATION_INDEX).map((c) => c.id)).toEqual([
      'id',
      'class',
      'pageIds',
      'confidenceAlerts',
      'missingRequiredFields',
      'status',
      'actions',
    ]);
  });

  it('shows the columns chosen in its preferences, remembered, Actions always', () => {
    window.localStorage.setItem('document-sections-columns', JSON.stringify({ visibleContent: ['status'] }));
    renderPanel();
    expect(columnWidths()).toEqual([
      ['Status', css('calc((100% - 100px) / 1)')],
      ['Actions', '100px'],
    ]);
    expect(createWrapper(document.body).findTable()!.findCollectionPreferences()).not.toBeNull();
    window.localStorage.removeItem('document-sections-columns');
  });
});
