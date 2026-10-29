// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The shared action bar. What has to hold: controls render in the order given; a menu
 * with no items and a `false` entry are left out; a button's hint wraps it in a span
 * carrying the hint (and no hint, no span); a disabled reason disables the control and is
 * what a screen reader reads; a menu's groups keep their items in order; an item with an
 * href is a link; and choosing an item reports its id.
 */

import React from 'react';
import { render } from '@testing-library/react';
import { Header } from '@cloudscape-design/components';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { describe, expect, it, vi } from 'vitest';

import ActionBar from '../ActionBar';
import type { ActionBarControl } from '../ActionBar';
import { barHelpers } from './action-bar-helpers';

const bar = barHelpers(['Menu', 'Empty'] as const, { One: 'one', Two: 'two', Three: 'three', Link: 'link' });

const renderBar = (controls: (ActionBarControl | false)[]) => render(<Header actions={<ActionBar controls={controls} />}>Title</Header>);

const menu = (over: Partial<Extract<ActionBarControl, { kind: 'menu' }>> = {}): ActionBarControl => ({
  kind: 'menu',
  id: 'menu',
  text: 'Menu',
  items: [{ id: 'one', text: 'One' }],
  onItemClick: vi.fn(),
  ...over,
});

describe('ActionBar', () => {
  it('leaves out a menu with no items and a false entry, keeping the order given', () => {
    renderBar([
      { kind: 'button', id: 'a', text: 'A' },
      false,
      menu({ id: 'empty', text: 'Empty', items: [{ kind: 'group', items: [] }] }),
      menu(),
      { kind: 'button', id: 'b', text: 'B' },
    ]);
    expect(bar.menuNames()).toEqual(['Menu']);
    expect(bar.barButtonLabels()).toEqual(['A', 'B']);
  });

  it('wraps a button with a hint in a span carrying the hint, and no other', () => {
    renderBar([
      { kind: 'button', id: 'a', text: 'A', hint: { title: 'Does A' } },
      { kind: 'button', id: 'b', text: 'B' },
    ]);
    const [a, b] = bar.barButtons();
    expect(a.parentElement!.tagName).toBe('SPAN');
    expect(a.parentElement!.getAttribute('title')).toBe('Does A');
    expect(b.parentElement!.tagName).not.toBe('SPAN');
  });

  it('disables a button with its reason, which a screen reader reads', () => {
    renderBar([{ kind: 'button', id: 'a', text: 'A', disabledReason: 'Not now' }]);
    const a = createWrapper(document.body).findHeader()!.findActions()!.findButton()!;
    expect(a.getElement().getAttribute('aria-disabled')).toBe('true');
    a.focus();
    expect(a.findDisabledReason()?.getElement().textContent).toBe('Not now');
  });

  it('keeps a disabled item in its menu with the reason, and groups in order', () => {
    renderBar([
      menu({
        items: [
          { id: 'one', text: 'One', disabledReason: 'Not now' },
          {
            kind: 'group',
            items: [
              { id: 'two', text: 'Two' },
              { id: 'three', text: 'Three' },
            ],
          },
        ],
      }),
    ]);
    expect(bar.menuItems('Menu')).toEqual([
      { text: 'One', disabled: true, disabledReason: 'Not now' },
      { text: 'Two', disabled: false, disabledReason: null },
      { text: 'Three', disabled: false, disabledReason: null },
    ]);
    const m = bar.findMenu('Menu')!;
    m.openDropdown();
    expect(m.findAll('[role="group"]')).toHaveLength(1);
  });

  it('renders an item with an href as a link', () => {
    renderBar([menu({ items: [{ id: 'link', text: 'Link', href: '#/somewhere' }] })]);
    const m = bar.findMenu('Menu')!;
    m.openDropdown();
    expect(m.findItemById('link')!.find('a')!.getElement().getAttribute('href')).toBe('#/somewhere');
  });

  it('reports the id of the item chosen, and a button its click', () => {
    const onItemClick = vi.fn();
    const onClick = vi.fn();
    renderBar([
      menu({
        items: [
          { id: 'one', text: 'One' },
          { id: 'two', text: 'Two' },
        ],
        onItemClick,
      }),
      { kind: 'button', id: 'a', text: 'A', onClick },
    ]);
    bar.chooseMenuItem('Menu', 'Two');
    expect(onItemClick).toHaveBeenCalledWith('two');
    bar.barButtons()[0].click();
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('renders a view switch with its segments and reports the one chosen', () => {
    const onChange = vi.fn();
    renderBar([
      {
        kind: 'switch',
        id: 'v',
        label: 'Pick',
        selectedId: 'x',
        options: [
          { id: 'x', text: 'X' },
          { id: 'y', text: 'Y' },
        ],
        onChange,
      },
    ]);
    const control = createWrapper(document.body).findHeader()!.findActions()!.findSegmentedControl()!;
    expect(control.findSegments().map((s) => s.getElement().textContent)).toEqual(['X', 'Y']);
    control.findSegments()[1].click();
    expect(onChange).toHaveBeenCalledWith('y');
  });
});
