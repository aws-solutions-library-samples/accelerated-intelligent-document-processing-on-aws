// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// A page's action bar: one row of controls for a Header's `actions`, left to right as
// given. A control is a view switch (SegmentedControl), a menu (ButtonDropdown) or a
// button. The edit page and the document page both render their bars through it, so
// the two read the same; each page decides which controls apply and in what order.
//
// A menu with no items is left out. A menu item or button that applies but can't be used
// now is disabled with its reason (`disabledReason`). A button with a `hint` is wrapped
// in a span carrying the hint as its hover text.
import React from 'react';
import { Button, ButtonDropdown, SegmentedControl, SpaceBetween } from '@cloudscape-design/components';
import type { ButtonDropdownProps, ButtonProps, IconProps } from '@cloudscape-design/components';

export interface ActionBarMenuItem {
  id: string;
  text: string;
  /** Why the item can't be used now; undefined when it can. */
  disabledReason?: string;
  href?: string;
  external?: boolean;
  iconName?: IconProps.Name;
  ariaLabel?: string;
}

export interface ActionBarMenuGroup {
  kind: 'group';
  text?: string;
  items: ActionBarMenuItem[];
}

export interface ActionBarSwitch {
  kind: 'switch';
  id: string;
  label: string;
  selectedId: string;
  options: { id: string; text: string }[];
  onChange: (id: string) => void;
}

export interface ActionBarMenu {
  kind: 'menu';
  id: string;
  text: string;
  items: (ActionBarMenuItem | ActionBarMenuGroup)[];
  disabled?: boolean;
  loading?: boolean;
  variant?: ButtonDropdownProps.Variant;
  expandToViewport?: boolean;
  ariaLabel?: string;
  onItemClick: (id: string) => void;
}

export interface ActionBarButton {
  kind: 'button';
  id: string;
  text: string;
  variant?: ButtonProps.Variant;
  iconName?: IconProps.Name;
  ariaLabel?: string;
  href?: string;
  loading?: boolean;
  /** Defaults to whether there is a `disabledReason`. */
  disabled?: boolean;
  disabledReason?: string;
  /** Wrap the button in a span with this hover text (none when `title` is undefined). */
  hint?: { title?: string };
  onClick?: () => void;
}

export type ActionBarControl = ActionBarSwitch | ActionBarMenu | ActionBarButton;

/** A menu item: available, or disabled with the reason on hover. */
export const actionMenuItem = (id: string, text: string, disabledReason?: string): ButtonDropdownProps.Item => ({
  id,
  text,
  disabled: !!disabledReason,
  disabledReason,
});

const toItem = (i: ActionBarMenuItem): ButtonDropdownProps.Item => ({
  ...actionMenuItem(i.id, i.text, i.disabledReason),
  ...(i.href !== undefined ? { href: i.href } : {}),
  ...(i.external !== undefined ? { external: i.external } : {}),
  ...(i.iconName !== undefined ? { iconName: i.iconName } : {}),
  ...(i.ariaLabel !== undefined ? { ariaLabel: i.ariaLabel } : {}),
});

const toItems = (items: ActionBarMenu['items']): ButtonDropdownProps.Items =>
  items.map((i) =>
    'kind' in i ? { items: i.items.map(toItem), ...(i.text ? { text: i.text } : {}) } : toItem(i),
  ) as ButtonDropdownProps.Items;

const itemCount = (items: ActionBarMenu['items']): number => items.reduce((n, i) => n + ('kind' in i ? i.items.length : 1), 0);

const renderControl = (c: ActionBarControl): React.ReactElement | null => {
  if (c.kind === 'switch') {
    return (
      <SegmentedControl
        key={c.id}
        label={c.label}
        selectedId={c.selectedId}
        onChange={({ detail }) => c.onChange(detail.selectedId)}
        options={c.options}
      />
    );
  }
  if (c.kind === 'menu') {
    if (itemCount(c.items) === 0) return null;
    return (
      <ButtonDropdown
        key={c.id}
        items={toItems(c.items)}
        disabled={c.disabled}
        loading={c.loading}
        variant={c.variant}
        expandToViewport={c.expandToViewport}
        ariaLabel={c.ariaLabel}
        onItemClick={({ detail }) => c.onItemClick(detail.id)}
      >
        {c.text}
      </ButtonDropdown>
    );
  }
  const button = (
    <Button
      key={c.hint ? undefined : c.id}
      variant={c.variant}
      iconName={c.iconName}
      ariaLabel={c.ariaLabel}
      href={c.href}
      disabled={c.disabled ?? !!c.disabledReason}
      disabledReason={c.disabledReason}
      loading={c.loading}
      onClick={c.onClick}
    >
      {c.text}
    </Button>
  );
  return c.hint ? (
    <span key={c.id} title={c.hint.title}>
      {button}
    </span>
  ) : (
    button
  );
};

const ActionBar = ({ controls }: { controls: readonly (ActionBarControl | false | null | undefined)[] }): React.JSX.Element => (
  <SpaceBetween direction="horizontal" size="xs" alignItems="center">
    {controls
      .filter((c): c is ActionBarControl => !!c)
      .map(renderControl)
      .filter((e): e is React.ReactElement => e !== null)}
  </SpaceBetween>
);

export default ActionBar;
