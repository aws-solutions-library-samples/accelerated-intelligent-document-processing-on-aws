// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The document page reads its key from the route once: the router has already decoded
 * it, so decoding again either throws (a key with a bare `%`) or changes it (a key
 * that contains `%25`). What has to hold: the page asks for, and names in its
 * breadcrumb, the key exactly as stored.
 */

import React from 'react';
import { render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const getDocumentDetailsFromIds = vi.fn();
vi.mock('../../contexts/documents', () => ({
  default: () => ({
    documents: [],
    getDocumentDetailsFromIds: (...a: unknown[]) => getDocumentDetailsFromIds(...a),
    setToolsOpen: vi.fn(),
  }),
}));
vi.mock('../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../hooks/use-user-role', () => ({ default: () => ({ canWrite: true }) }));
vi.mock('../../components/document-panel', () => ({ default: () => null }));

import DocumentDetails from '../../components/document-details/DocumentDetails';
import DocumentDetailsBreadcrumbs from '../../components/document-details/breadcrumbs';

const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/documents/:objectKey" element={<DocumentDetailsBreadcrumbs />} />
      </Routes>
      <Routes>
        <Route path="/documents/*" element={<DocumentDetails />} />
      </Routes>
    </MemoryRouter>,
  );

beforeEach(() => {
  document.body.innerHTML = '';
  getDocumentDetailsFromIds.mockReset().mockResolvedValue([]);
});

describe('the document page’s key', () => {
  it.each([
    ['100%/off.pdf', '/documents/100%25%2Foff.pdf'],
    ['100%25off.pdf', '/documents/100%2525off.pdf'],
  ])('opens %s as stored', async (key, path) => {
    expect(() => renderAt(path)).not.toThrow();
    await waitFor(() => expect(getDocumentDetailsFromIds).toHaveBeenCalledWith([key]));
    const crumbs = createWrapper(document.body).findBreadcrumbGroup()!.findBreadcrumbLinks();
    expect(crumbs[crumbs.length - 1].getElement().textContent).toBe(key);
  });
});
