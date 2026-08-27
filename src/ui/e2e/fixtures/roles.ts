// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import path from 'node:path';

/**
 * Cognito groups the UI recognises. scripts/uat/run_uat.py creates one user per
 * role with a PERMANENT password (via scripts/rbac_common.py create_cognito_user),
 * which is what avoids the NEW_PASSWORD_REQUIRED challenge that would otherwise
 * hang the sign-in form on a fresh user.
 */
export type Role = 'Admin' | 'Viewer';

export const STATE_DIR = path.join(import.meta.dirname, '.state');

/**
 * Mirrors WELCOME_DISMISSED_KEY in src/ui/src/routes/constants.ts. When absent,
 * AuthRoutes.tsx shows the "Welcome to GenAI IDP" interstitial instead of the app
 * shell, so a fresh user has no navigation until they click "Enter IDP Console".
 */
export const WELCOME_DISMISSED_KEY = 'idp-welcome-dismissed';

export const statePath = (role: Role): string =>
  path.join(STATE_DIR, `${role.toLowerCase()}.json`);

interface Credentials {
  username: string;
  password: string;
}

/**
 * Credentials come from the environment, never from the repo. run_uat.py exports
 * them; a manual run must export them too.
 */
export function credentials(role: Role): Credentials {
  const upper = role.toUpperCase();
  const username = process.env[`UAT_${upper}_USER`];
  const password = process.env[`UAT_${upper}_PASSWORD`];
  if (!username || !password) {
    throw new Error(
      `Missing UAT_${upper}_USER / UAT_${upper}_PASSWORD. ` +
        'These are exported by `make uat-testing`; for a manual run against an ' +
        'existing stack, create the users first and export the credentials.',
    );
  }
  return { username, password };
}

/** Roles to provision and sign in. Keep in step with run_uat.py ROLES. */
export const ROLES: Role[] = ['Admin', 'Viewer'];
