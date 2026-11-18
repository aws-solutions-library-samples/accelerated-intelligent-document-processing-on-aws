// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The configuration editor's tabs: Processing Settings (id `configuration`, so existing
 * `?tab=` links still work) and Document Schema. The prompt preview is not a tab; it is
 * the page's Preview prompts button.
 */
import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { MemoryRouter } from 'react-router-dom';

vi.mock('../../../hooks/use-synthetic-data-generator', () => ({ default: () => ({ available: false }) }));

import ConfigBuilder from '../ConfigBuilder';

describe('the configuration editor tabs', () => {
  it('are Processing Settings and Document Schema, with no Prompt Preview tab', () => {
    render(
      <MemoryRouter>
        <ConfigBuilder schema={{ type: 'object', properties: {} }} formValues={{}} />
      </MemoryRouter>,
    );
    const tabs = createWrapper(document.body).findTabs()!;
    expect(tabs.findTabLinks().map((t) => t.getElement().textContent)).toEqual(['Processing Settings', 'Document Schema']);
    expect(tabs.findTabLinkById('configuration')).not.toBeNull();
  });
});
