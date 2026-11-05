// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The right-side help panel is chosen by route. A page with no entry falls through
 * to the `:objectKey` catch-all and shows the document-details help, which is what
 * the Prefix Mappings page did. Pinned here so it cannot regress.
 */

import React from 'react';
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import ToolsPanel from '../tools-panel';

const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/documents/*" element={<ToolsPanel />} />
      </Routes>
    </MemoryRouter>,
  );

describe('ToolsPanel routing', () => {
  it('shows the Prefix Mappings and Tenants help on the prefix-mappings page', () => {
    renderAt('/documents/prefix-mappings');
    expect(screen.getByRole('heading', { name: 'Prefix Mappings and Tenants' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Multi-tenancy' })).toHaveAttribute('href', expect.stringContaining('/multi-tenancy/'));
  });
});
