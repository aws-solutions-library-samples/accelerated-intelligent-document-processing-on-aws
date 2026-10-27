// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The HITL pages' layout and its info panel. What has to hold: given a panel, the
 * layout has the info toggle the document pages have, closed until asked for, and opens
 * the panel; without one there is no toggle; and the queue and edit routes each pass
 * their own panel.
 */

import React from 'react';
import { act, render } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../../../contexts/app', () => ({ default: () => ({ navigationOpen: false, setNavigationOpen: vi.fn() }) }));
vi.mock('../../../hooks/use-notifications', () => ({ default: () => [] }));
vi.mock('../../genaiidp-layout/navigation', () => ({ default: () => null }));

import ReviewQueuesLayout from '../ReviewQueuesLayout';

const renderLayout = (tools?: React.ReactNode) =>
  render(
    <MemoryRouter>
      <ReviewQueuesLayout tools={tools}>
        <div />
      </ReviewQueuesLayout>
    </MemoryRouter>,
  );

describe('the HITL layout info panel', () => {
  it('has the info toggle when given a panel, closed until asked for', async () => {
    renderLayout(<div data-testid="panel">Help</div>);
    const layout = createWrapper(document.body).findAppLayout()!;
    const toggle = layout.findToolsToggle();
    expect(toggle).not.toBeNull();
    // Cloudscape keeps the panel mounted while it is closed; the toggle says which it is.
    expect(toggle!.getElement().getAttribute('aria-expanded')).toBe('false');
    await act(async () => toggle!.click());
    expect(layout.findToolsToggle()!.getElement().getAttribute('aria-expanded')).toBe('true');
    expect(document.querySelector('[data-testid="panel"]')).not.toBeNull();
  });

  it('has no info toggle without a panel', () => {
    renderLayout();
    expect(createWrapper(document.body).findAppLayout()!.findToolsToggle()).toBeNull();
  });

  it('is given a panel by Queue Management, both queue pages, both item pages and the direct edit', () => {
    const routes = readFileSync(join(__dirname, '..', '..', '..', 'routes', 'ReviewQueuesRoutes.tsx'), 'utf-8');
    expect(routes.match(/tools=\{<EditScreenToolsPanel \/>\}/g) ?? []).toHaveLength(2);
    expect(routes.match(/tools=\{<QueueToolsPanel \/>\}/g) ?? []).toHaveLength(2);
    expect(routes.match(/tools=\{<QueueManagementToolsPanel \/>\}/g) ?? []).toHaveLength(1);
    const direct = readFileSync(join(__dirname, '..', '..', 'edit-screen', 'DocumentEditPage.tsx'), 'utf-8');
    expect(direct).toMatch(/tools=\{<EditScreenToolsPanel \/>\}/);
  });
});
