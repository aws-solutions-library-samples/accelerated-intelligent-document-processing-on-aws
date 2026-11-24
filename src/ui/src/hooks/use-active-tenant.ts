// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';
import { generateClient } from '../api/client-shim';
import { selectActiveTenant } from '../graphql/generated';
import { resetSharedProfileScope } from './use-user-role';
import { writeTenantHint } from '../utils/tenant-selection';

const logger = new ConsoleLogger('useActiveTenant');

const messages = defineMessages<{ selectFailed: NoMessageValues }>({
  selectFailed: {
    id: 'tenantSelection.hook.selectFailed',
    defaultMessage: 'Could not select the tenant. Try again.',
    description: 'Error shown when choosing the active tenant failed and the server gave no reason of its own.',
  },
});

/** The server's own message for a refused selection, or `null` when it gave none. */
export const serverErrorMessage = (err: unknown): string | null => {
  const first = (err as { errors?: { message?: string }[] } | null)?.errors?.[0]?.message;
  return first || null;
};

/**
 * Choose the active tenant (`TenantSelection=UserSelect`).
 *
 * On success the remembered choice is written and the page is reloaded, because the
 * rest of the app holds the previous scope in memory: `use-user-role` caches the
 * profile in a module-level promise that is documented as not changing while the page
 * is open, and pages hold their own lists. A reload resets all of that at once, which
 * is simpler and safer than invalidating each cache.
 *
 * ⚠ Resolvers cache a user's scope for up to a minute, so the first requests after
 * the reload can still answer for the previous scope. The switcher says so.
 *
 * A refusal's reason is the server's own text and is shown as it came; only the
 * fallback, used when the server gave none, is a translated message.
 */
const useActiveTenant = (subject: string | null) => {
  const intl = useIntl();
  const [selecting, setSelecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const select = useCallback(
    async (tenantId: string): Promise<boolean> => {
      setSelecting(true);
      setError(null);
      try {
        const client = generateClient();
        await client.graphql({ query: selectActiveTenant, variables: { tenantId } });
        writeTenantHint(subject, tenantId);
        resetSharedProfileScope();
        window.location.reload();
        return true;
      } catch (err) {
        logger.error('Error selecting the active tenant', err);
        setError(serverErrorMessage(err) ?? intl.formatMessage(messages.selectFailed));
        setSelecting(false);
        return false;
      }
    },
    [subject, intl],
  );

  return { select, selecting, error };
};

export default useActiveTenant;
