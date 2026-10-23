// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The editor's notices. What has to hold: every info and warning notice shares one
 * alert (a warning when any is one) with all their actions; an error keeps its own
 * alert; and nothing renders when there is nothing to say.
 */

import React from 'react';
import { render } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { describe, expect, it } from 'vitest';

import EditorNotices from '../EditorNotices';

const alerts = () => createWrapper(document.body).findAllAlerts();

describe('EditorNotices', () => {
  it('puts the info and warning notices in one alert, with their actions', () => {
    render(
      <EditorNotices
        notices={[
          { id: 'a', type: 'info', content: 'First', action: <button type="button">Do A</button> },
          false,
          { id: 'b', type: 'warning', content: 'Second' },
          { id: 'c', type: 'info', content: 'Third', action: <button type="button">Do C</button> },
        ]}
      />,
    );
    expect(alerts()).toHaveLength(1);
    const alert = alerts()[0];
    expect(alert.getElement().textContent).toContain('First');
    expect(alert.getElement().textContent).toContain('Second');
    expect(alert.getElement().textContent).toContain('Third');
    expect(alert.findActionSlot()!.getElement().textContent).toBe('Do ADo C');
    expect(alert.findRootElement().getElement().className).toMatch(/warning/);
  });

  it('keeps an error in its own alert', () => {
    render(
      <EditorNotices
        notices={[
          { id: 'e', type: 'error', content: 'Broken' },
          { id: 'i', type: 'info', content: 'Note' },
        ]}
      />,
    );
    expect(alerts().map((a) => a.findContent().getElement().textContent)).toEqual(['Broken', 'Note']);
  });

  it('renders nothing without notices', () => {
    const { container } = render(<EditorNotices notices={[false, null, undefined]} />);
    expect(container.innerHTML).toBe('');
  });
});
