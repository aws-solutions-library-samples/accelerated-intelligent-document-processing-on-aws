// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Test helpers for the edit screen's bar: its plain buttons, and its two menus (Reports,
// More Actions) and their items. Shared by the edit screen suites, so each reads the bar
// the same way.
import { act } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

export const MENUS = ['Reports', 'More Actions'] as const;
export type MenuName = (typeof MENUS)[number];

/** Each menu item's id in the bar, by its label. */
const ITEM_IDS: Record<string, string> = {
  History: 'history',
  Summary: 'summary',
  'Processing Report': 'report',
  'Edit Page Grouping': 'regroup',
  'Use As Evaluation Baseline': 'baseline',
};

const actions = () => createWrapper(document.body).findHeader()!.findActions()!;

/** A button's own label: a disabled reason renders inside the button, after it. */
export const buttonLabel = (b: HTMLElement): string | null => b.querySelector('[class*="content"]')?.textContent ?? b.textContent;

const dropdowns = () => actions().findAllButtonDropdowns();
const triggerLabel = (d: ReturnType<typeof dropdowns>[number]) => buttonLabel(d.findNativeButton().getElement());

/** The bar's plain buttons, in order, without the menus' triggers. */
export const barButtons = (): HTMLElement[] => {
  const triggers = new Set(dropdowns().map((d) => d.findNativeButton().getElement()));
  return actions()
    .findAllButtons()
    .map((b) => b.getElement())
    .filter((b) => !triggers.has(b) && !b.closest('[role="menu"]'));
};
export const barButtonLabels = (): (string | null)[] => barButtons().map(buttonLabel);

/** The bar's menus, in order. */
export const menuNames = (): (string | null)[] => dropdowns().map(triggerLabel);

export const findMenu = (name: MenuName) => dropdowns().find((d) => triggerLabel(d) === name);

export interface MenuItemState {
  text: string;
  disabled: boolean;
  disabledReason: string | null;
}

/** A menu's items: their labels, whether each is disabled, and why. */
export const menuItems = (name: MenuName): MenuItemState[] => {
  const menu = findMenu(name);
  if (!menu) return [];
  act(() => menu.openDropdown());
  const items = Object.entries(ITEM_IDS)
    .map(([text, id]) => ({ text, item: menu.findItemById(id) }))
    .filter(({ item }) => !!item)
    .map(({ text, item }) => {
      // The reason is what a screen reader reads: the element the item's aria-describedby names.
      const menuItem = item!.getElement().querySelector('[role="menuitem"]') ?? item!.getElement();
      const disabled = menuItem.getAttribute('aria-disabled') === 'true';
      const describedBy = menuItem.getAttribute('aria-describedby');
      const reason = describedBy ? (document.getElementById(describedBy)?.textContent ?? null) : null;
      return { text, disabled, disabledReason: disabled ? reason : null };
    })
    // In the menu's own order.
    .sort((a, b) => {
      const ea = menu.findItemById(ITEM_IDS[a.text])!.getElement();
      const eb = menu.findItemById(ITEM_IDS[b.text])!.getElement();
      return ea.compareDocumentPosition(eb) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
    });
  act(() => menu.openDropdown());
  return items;
};

/** Choose a menu item by its label. */
export const chooseMenuItem = (name: MenuName, text: string): void => {
  const menu = findMenu(name);
  if (!menu) throw new Error(`no ${name} menu`);
  act(() => menu.openDropdown());
  const item = menu.findItemById(ITEM_IDS[text]);
  if (!item) throw new Error(`no ${text} in ${name}`);
  act(() => item.click());
};
