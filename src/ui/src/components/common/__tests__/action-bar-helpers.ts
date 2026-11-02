// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Test helpers for a page's ActionBar: its plain buttons, its menus and their items. The
// same reading as edit-screen/__tests__/edit-bar.ts, for any bar: give the menus' names
// and each item's id by its label. The bar is the actions of the first Header in the DOM.
import { act } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

export interface MenuItemState {
  text: string;
  disabled: boolean;
  disabledReason: string | null;
}

/** A button's own label: a disabled reason renders inside the button, after it. */
export const buttonLabel = (b: HTMLElement): string | null => b.querySelector('[class*="content"]')?.textContent ?? b.textContent;

export const barHelpers = <M extends string>(menus: readonly M[], itemIds: Record<string, string>) => {
  const actions = () => createWrapper(document.body).findHeader()!.findActions()!;
  const dropdowns = () => actions().findAllButtonDropdowns();
  const triggerLabel = (d: ReturnType<typeof dropdowns>[number]) => buttonLabel(d.findNativeButton().getElement());

  /** The bar's plain buttons, in order, without the menus' triggers. */
  const barButtons = (): HTMLElement[] => {
    const triggers = new Set(dropdowns().map((d) => d.findNativeButton().getElement()));
    return actions()
      .findAllButtons()
      .map((b) => b.getElement())
      .filter((b) => !triggers.has(b) && !b.closest('[role="menu"]'));
  };
  const barButtonLabels = (): (string | null)[] => barButtons().map(buttonLabel);

  /** The bar's menus, in order. */
  const menuNames = (): (string | null)[] => dropdowns().map(triggerLabel);
  const findMenu = (name: M) => dropdowns().find((d) => triggerLabel(d) === name);

  /** A menu's items, in the menu's order: their labels, whether each is disabled, and why. */
  const menuItems = (name: M): MenuItemState[] => {
    const menu = findMenu(name);
    if (!menu) return [];
    act(() => menu.openDropdown());
    const items = Object.entries(itemIds)
      .map(([text, id]) => ({ text, item: menu.findItemById(id) }))
      .filter(({ item }) => !!item)
      .map(({ text, item }) => {
        // The reason is what a screen reader reads: the element the item's aria-describedby names.
        const el = item!.getElement().querySelector('[role="menuitem"]') ?? item!.getElement();
        const disabled = el.getAttribute('aria-disabled') === 'true';
        const describedBy = el.getAttribute('aria-describedby');
        const reason = describedBy ? (document.getElementById(describedBy)?.textContent ?? null) : null;
        return { text, disabled, disabledReason: disabled ? reason : null, el: item!.getElement() };
      })
      .sort((a, b) => (a.el.compareDocumentPosition(b.el) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1))
      .map(({ text, disabled, disabledReason }) => ({ text, disabled, disabledReason }));
    act(() => menu.openDropdown());
    return items;
  };

  /** Choose a menu item by its label. */
  const chooseMenuItem = (name: M, text: string): void => {
    const menu = findMenu(name);
    if (!menu) throw new Error(`no ${name} menu`);
    act(() => menu.openDropdown());
    const item = menu.findItemById(itemIds[text]);
    if (!item) throw new Error(`no ${text} in ${name}`);
    act(() => item.click());
  };

  return { actions, barButtons, barButtonLabels, menuNames, findMenu, menuItems, chooseMenuItem, menus };
};
