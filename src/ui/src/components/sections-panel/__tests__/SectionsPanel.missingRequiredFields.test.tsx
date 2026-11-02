// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Missing required fields (#1527) where a user sees them: a column in the
 * Document Sections table, a field in Document Details, and an optional column in
 * the document list that shows a dash when the row cannot know the count.
 */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ currentCredentials: {} }) }));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => ({ isReviewerOnly: false, canWrite: true, canReview: true }) }));

import SectionsPanel from '../SectionsPanel';
import { DocumentAttributes } from '../../document-panel/DocumentPanel';
import { COLUMN_DEFINITIONS_MAIN } from '../../document-list/documents-table-config';

const missing = (name: string) => ({ attributeName: name, confidence: null, confidenceThreshold: null });

describe('Document Sections table', () => {
  it('has a Missing Required Fields column holding each section count', () => {
    render(
      <SectionsPanel
        sections={[{ Id: '1', Class: 'W2', PageIds: [1], MissingRequiredFields: [missing('Doi'), missing('Issn')] }] as never}
        documentItem={{ objectKey: 'd', objectStatus: 'COMPLETED' } as never}
        mergedConfig={{}}
      />,
    );
    const headers = [...document.querySelectorAll('table thead th')].map((th) => (th.textContent ?? '').trim());
    const index = headers.indexOf('Missing Required Fields');
    expect(index).toBeGreaterThan(-1);
    const cell = document.querySelectorAll('table tbody tr')[0].querySelectorAll('td')[index];
    expect((cell.textContent ?? '').trim()).toBe('2');
  });
});

describe('Document Details', () => {
  it('shows the stored count beside Confidence Alerts', () => {
    render(<DocumentAttributes item={{ objectKey: 'd', sections: [], missingRequiredFieldCount: 4 } as never} versions={[]} />);
    const labels = [...document.querySelectorAll('[class*="key-label"]')];
    const label = labels.find((e) => e.textContent === 'Missing Required Fields')!;
    expect(label).toBeDefined();
    expect(labels[labels.indexOf(label) - 1].textContent).toBe('Confidence Alerts');
    expect(label.parentElement!.textContent).toContain('4');
  });
});

describe('document list', () => {
  const column = COLUMN_DEFINITIONS_MAIN().find((c) => c.id === 'missingRequiredFieldCount')!;

  it('shows a dash when the row does not carry the count', () => {
    expect(column.cell({ missingRequiredFieldCount: null } as never)).toBe('-');
    expect(column.cell({} as never)).toBe('-');
  });

  it('shows the count, including a known zero', () => {
    expect(column.cell({ missingRequiredFieldCount: 3 } as never)).toBe(3);
    expect(column.cell({ missingRequiredFieldCount: 0 } as never)).toBe(0);
  });
});
