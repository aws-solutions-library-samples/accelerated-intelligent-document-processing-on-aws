// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useEffect, useRef, useState } from 'react';
import { Alert, Box, Button, Container, FormField, Header, RadioGroup, SpaceBetween, Spinner } from '@cloudscape-design/components';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';
import useActiveTenant from '../../hooks/use-active-tenant';
import { clearTenantHint, readTenantHint, tenantToAutoSelect } from '../../utils/tenant-selection';

const messages = defineMessages<{
  applying: NoMessageValues;
  title: NoMessageValues;
  description: NoMessageValues;
  errorHeader: NoMessageValues;
  fieldLabel: NoMessageValues;
  fieldDescription: NoMessageValues;
  confirm: NoMessageValues;
  signOut: NoMessageValues;
}>({
  applying: {
    id: 'tenantSelection.gate.applying',
    defaultMessage: 'Applying your tenant…',
    description: 'Shown under a spinner while the tenant this browser tab remembers is applied automatically.',
  },
  title: {
    id: 'tenantSelection.gate.title',
    defaultMessage: 'Choose a tenant',
    description: 'Heading of the page shown after sign-in to a user whose account has access to several tenants.',
  },
  description: {
    id: 'tenantSelection.gate.description',
    defaultMessage: 'Your account has access to more than one tenant. Choose the one to work in.',
    description: 'Explanation under the heading of the tenant choice page.',
  },
  errorHeader: {
    id: 'tenantSelection.gate.errorHeader',
    defaultMessage: 'Could not select the tenant',
    description: 'Heading of the error shown when the server refuses the chosen tenant. The reason follows it.',
  },
  fieldLabel: {
    id: 'tenantSelection.gate.fieldLabel',
    defaultMessage: 'Tenant',
    description: 'Label of the list of tenants to choose from, and its screen-reader label.',
  },
  fieldDescription: {
    id: 'tenantSelection.gate.fieldDescription',
    defaultMessage: 'You can switch later from the navigation bar.',
    description: 'Hint under the list of tenants: the choice can be changed after signing in.',
  },
  confirm: {
    id: 'tenantSelection.gate.confirm',
    defaultMessage: 'Confirm tenant',
    description: 'Button that applies the chosen tenant and opens the application.',
  },
  signOut: {
    id: 'tenantSelection.gate.signOut',
    defaultMessage: 'Sign out',
    description: 'Button that signs the user out instead of choosing a tenant.',
  },
});

interface TenantSelectionGateProps {
  /** The tenants the identity provider grants this user, two or more. */
  eligibleTenants: string[];
  /** The signed-in user's Cognito `sub`, which the remembered choice is keyed on. */
  subject: string | null;
  onSignOut: () => void;
}

/**
 * Shown in place of the app when the signed-in user was granted several tenants and
 * has not chosen one (`TenantSelection=UserSelect`).
 *
 * A page, not a modal: Cloudscape modals always carry a close button, and closing this
 * one would leave the user in an app that shows them nothing. The only ways out are
 * choosing a tenant or signing out.
 *
 * If this tab remembers a choice that is still on offer, it is applied without asking.
 * That is an attempt, made once: the server checks it like any other selection, so a
 * stale or edited hint costs one refused request and then the page asks.
 *
 * Tenant ids are shown as they are declared: they are identifiers an administrator
 * chose, not text this application writes, so they are not translated.
 */
const TenantSelectionGate = ({ eligibleTenants, subject, onSignOut }: TenantSelectionGateProps): React.JSX.Element => {
  const intl = useIntl();
  const { select, selecting, error } = useActiveTenant(subject);
  const [choice, setChoice] = useState<string | null>(null);
  const [autoApplying, setAutoApplying] = useState(false);
  const attempted = useRef(false);

  useEffect(() => {
    if (attempted.current) return;
    attempted.current = true;
    const remembered = tenantToAutoSelect(readTenantHint(subject), eligibleTenants);
    if (!remembered) return;
    setAutoApplying(true);
    void select(remembered).then((ok) => {
      if (!ok) {
        // The server refused it: forget it, and ask.
        clearTenantHint();
        setAutoApplying(false);
      }
    });
  }, [subject, eligibleTenants, select]);

  if (autoApplying) {
    return (
      <Box padding="xxl" textAlign="center">
        <SpaceBetween size="s" alignItems="center">
          <Spinner size="large" />
          <Box variant="p" color="text-body-secondary">
            {intl.formatMessage(messages.applying)}
          </Box>
        </SpaceBetween>
      </Box>
    );
  }

  return (
    <Box padding="xxl">
      <div style={{ maxWidth: 560, margin: '0 auto' }} data-testid="tenant-selection-gate">
        <Container
          header={
            <Header variant="h2" description={intl.formatMessage(messages.description)}>
              {intl.formatMessage(messages.title)}
            </Header>
          }
        >
          <SpaceBetween size="m">
            {error && (
              <Alert type="error" header={intl.formatMessage(messages.errorHeader)}>
                {error}
              </Alert>
            )}
            <FormField label={intl.formatMessage(messages.fieldLabel)} description={intl.formatMessage(messages.fieldDescription)}>
              <RadioGroup
                value={choice}
                onChange={({ detail }) => setChoice(detail.value)}
                items={eligibleTenants.map((tenantId) => ({ value: tenantId, label: tenantId }))}
                ariaLabel={intl.formatMessage(messages.fieldLabel)}
              />
            </FormField>
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="primary" disabled={!choice} loading={selecting} onClick={() => choice && void select(choice)}>
                {intl.formatMessage(messages.confirm)}
              </Button>
              <Button variant="link" onClick={onSignOut} disabled={selecting}>
                {intl.formatMessage(messages.signOut)}
              </Button>
            </SpaceBetween>
          </SpaceBetween>
        </Container>
      </div>
    </Box>
  );
};

export default TenantSelectionGate;
