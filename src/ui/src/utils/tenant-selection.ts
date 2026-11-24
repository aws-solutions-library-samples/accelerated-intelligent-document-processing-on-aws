// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Choosing an active tenant (`TenantSelection=UserSelect`).
 *
 * A user the identity provider grants two or more tenants is granted none at
 * sign-in, and the tenants are recorded for them to choose from. This module holds
 * the two decisions the UI makes about that, kept free of React so they are tested
 * directly:
 *
 * - when the user must choose, and
 * - the remembered choice ("hint") that lets a reload or a new tab skip the dialog.
 *
 * ⚠ The hint is a convenience, never an authority. The server decides which tenants a
 * user may have, from the list the sign-in trigger wrote, and checks the choice on
 * every call. A hint someone edited, or one left by an earlier session, can only
 * cause a request the server refuses.
 */

import { NO_TENANT_GRANTED } from '../hooks/use-tenants';

/** Where the last choice is remembered. `sessionStorage`: per tab, gone when it closes. */
export const TENANT_HINT_KEY = 'idp_active_tenant_hint';

interface StoredHint {
  sub: string;
  tenantId: string;
}

/**
 * Whether the user must choose a tenant before the app can mount.
 *
 * True only when the stored scope is exactly the "no tenant granted" marker and the
 * server offered a choice of two or more. A user with one tenant, an Admin, and a
 * user granted nothing (no list) all answer false: there is nothing for them to pick.
 */
export const needsTenantChoice = (
  allowedConfigVersions: string[] | null | undefined,
  eligibleTenants: string[] | null | undefined,
): boolean =>
  (eligibleTenants?.length ?? 0) >= 2 &&
  !!allowedConfigVersions &&
  allowedConfigVersions.length === 1 &&
  allowedConfigVersions[0] === NO_TENANT_GRANTED;

const safeStorage = (): Storage | null => {
  try {
    return typeof sessionStorage === 'undefined' ? null : sessionStorage;
  } catch {
    return null;
  }
};

/** The remembered tenant for this user, or `null`. Another user's hint is not returned. */
export const readTenantHint = (subject: string | undefined | null, storage: Storage | null = safeStorage()): string | null => {
  if (!subject || !storage) return null;
  try {
    const raw = storage.getItem(TENANT_HINT_KEY);
    if (!raw) return null;
    const hint = JSON.parse(raw) as Partial<StoredHint>;
    if (hint.sub !== subject || typeof hint.tenantId !== 'string' || !hint.tenantId) return null;
    return hint.tenantId;
  } catch {
    return null;
  }
};

export const writeTenantHint = (subject: string | undefined | null, tenantId: string, storage: Storage | null = safeStorage()): void => {
  if (!subject || !storage) return;
  try {
    storage.setItem(TENANT_HINT_KEY, JSON.stringify({ sub: subject, tenantId } satisfies StoredHint));
  } catch {
    /* storage unavailable or full: the user is simply asked again */
  }
};

export const clearTenantHint = (storage: Storage | null = safeStorage()): void => {
  try {
    storage?.removeItem(TENANT_HINT_KEY);
  } catch {
    /* nothing to clear */
  }
};

/**
 * The tenant to apply without asking: the remembered one, if it is still one of the
 * tenants the server offers. Otherwise `null`, and the dialog is shown.
 */
export const tenantToAutoSelect = (hint: string | null, eligibleTenants: string[]): string | null =>
  hint && eligibleTenants.includes(hint) ? hint : null;
