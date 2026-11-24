// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useState } from 'react';
import { Alert, Box, Button, Modal, SpaceBetween } from '@cloudscape-design/components';
import type { TopNavigationProps } from '@cloudscape-design/components';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';
import useActiveTenant from '../../hooks/use-active-tenant';

const messages = defineMessages<{
  menuWithTenant: { readonly tenant: string };
  menu: NoMessageValues;
  switchTenant: NoMessageValues;
  close: NoMessageValues;
  cancel: NoMessageValues;
  confirm: NoMessageValues;
  bodyWithCurrent: { readonly tenant: string; readonly current: string };
  bodyNoCurrent: { readonly tenant: string };
  note: NoMessageValues;
  errorHeader: NoMessageValues;
}>({
  menuWithTenant: {
    id: 'tenantSelection.switcher.menuWithTenant',
    defaultMessage: 'Tenant: {tenant}',
    description: 'Text of the tenant menu button in the top navigation. {tenant} is the active tenant id.',
  },
  menu: {
    id: 'tenantSelection.switcher.menu',
    defaultMessage: 'Tenant',
    description: 'Text of the tenant menu button in the top navigation when no tenant is active.',
  },
  switchTenant: {
    id: 'tenantSelection.switcher.switchTenant',
    defaultMessage: 'Switch tenant',
    description: 'Tooltip and screen-reader label of the tenant menu button, and the heading of its confirmation dialog.',
  },
  close: {
    id: 'tenantSelection.switcher.close',
    defaultMessage: 'Close',
    description: 'Screen-reader label of the close button of the switch tenant dialog.',
  },
  cancel: {
    id: 'tenantSelection.switcher.cancel',
    defaultMessage: 'Cancel',
    description: 'Button that closes the switch tenant dialog without switching.',
  },
  confirm: {
    id: 'tenantSelection.switcher.confirm',
    defaultMessage: 'Confirm switch',
    description: 'Button of the switch tenant dialog that switches to the chosen tenant.',
  },
  bodyWithCurrent: {
    id: 'tenantSelection.switcher.bodyWithCurrent',
    defaultMessage: 'Switch to {tenant}? You will see the documents of {tenant} instead of those of {current}.',
    description: 'Question in the switch tenant dialog. {tenant} is the tenant to switch to, {current} the tenant that is active now.',
  },
  bodyNoCurrent: {
    id: 'tenantSelection.switcher.bodyNoCurrent',
    defaultMessage: 'Switch to {tenant}? You will see the documents of {tenant}.',
    description: 'Question in the switch tenant dialog when no tenant is active now. {tenant} is the tenant to switch to.',
  },
  note: {
    id: 'tenantSelection.switcher.note',
    defaultMessage: 'This applies to every session you have open, and can take up to a minute to take effect everywhere.',
    description: 'Note in the switch tenant dialog: the choice is stored per user, and the server caches it briefly.',
  },
  errorHeader: {
    id: 'tenantSelection.switcher.errorHeader',
    defaultMessage: 'Could not switch tenant',
    description: 'Heading of the error shown in the switch tenant dialog when the server refuses the switch. The reason follows it.',
  },
});

interface TenantSwitcherInput {
  eligibleTenants?: string[];
  activeTenant?: string | null;
  subject?: string | null;
}

/**
 * The navigation bar's tenant switcher (`TenantSelection=UserSelect`).
 *
 * Returns the menu to add to the top navigation and the confirmation dialog to render
 * beside it; both are `null` for a user with fewer than two tenants to switch between,
 * which is everyone but a user the identity provider granted several.
 *
 * Switching reloads the page, and changes the tenant for every session of the user,
 * not just this tab. Resolvers cache a user's scope for up to a minute, so the first
 * requests after a switch can still show the previous tenant; the dialog says so.
 */
const useTenantSwitcher = ({ eligibleTenants: offered, activeTenant: active, subject: sub }: TenantSwitcherInput) => {
  const intl = useIntl();
  const eligibleTenants = offered ?? [];
  const activeTenant = active ?? null;
  const { select, selecting, error } = useActiveTenant(sub ?? null);
  const [pending, setPending] = useState<string | null>(null);

  if (eligibleTenants.length < 2) return { utility: null, modal: null };

  const utility: TopNavigationProps.Utility = {
    type: 'menu-dropdown',
    iconName: 'folder',
    text: activeTenant ? intl.formatMessage(messages.menuWithTenant, { tenant: activeTenant }) : intl.formatMessage(messages.menu),
    title: intl.formatMessage(messages.switchTenant),
    ariaLabel: intl.formatMessage(messages.switchTenant),
    items: eligibleTenants.map((tenantId) => ({
      id: `tenant-${tenantId}`,
      text: tenantId,
      iconName: tenantId === activeTenant ? 'check' : undefined,
      disabled: tenantId === activeTenant,
    })),
    onItemClick: ({ detail }) => {
      const tenantId = detail.id.replace(/^tenant-/, '');
      if (eligibleTenants.includes(tenantId) && tenantId !== activeTenant) setPending(tenantId);
    },
  };

  const modal = (
    <Modal
      visible={pending !== null}
      onDismiss={() => !selecting && setPending(null)}
      closeAriaLabel={intl.formatMessage(messages.close)}
      header={intl.formatMessage(messages.switchTenant)}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={() => setPending(null)} disabled={selecting}>
              {intl.formatMessage(messages.cancel)}
            </Button>
            <Button variant="primary" loading={selecting} onClick={() => pending && void select(pending)}>
              {intl.formatMessage(messages.confirm)}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="s">
        <Box variant="p">
          {activeTenant
            ? intl.formatMessage(messages.bodyWithCurrent, { tenant: pending ?? '', current: activeTenant })
            : intl.formatMessage(messages.bodyNoCurrent, { tenant: pending ?? '' })}
        </Box>
        <Box variant="p" color="text-body-secondary">
          {intl.formatMessage(messages.note)}
        </Box>
        {error && (
          <Alert type="error" header={intl.formatMessage(messages.errorHeader)}>
            {error}
          </Alert>
        )}
      </SpaceBetween>
    </Modal>
  );

  return { utility, modal };
};

export default useTenantSwitcher;
