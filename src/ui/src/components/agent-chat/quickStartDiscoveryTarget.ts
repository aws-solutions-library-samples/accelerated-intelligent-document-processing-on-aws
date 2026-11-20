// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

interface ProfileSummary {
  versionName: string;
  isActive?: boolean | null;
}

const pad = (value: number): string => String(value).padStart(2, '0');

/**
 * A new profile name for the documents a Quick Start conversation attaches, so Discovery
 * never merges them into an existing profile: not the active one, which may be
 * stack-managed or another user's, and not a profile shared between conversations.
 */
export const newQuickStartProfileName = (now: Date): string =>
  `quickstart-${now.getUTCFullYear()}${pad(now.getUTCMonth() + 1)}${pad(now.getUTCDate())}-` +
  `${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}${pad(now.getUTCSeconds())}`;

/**
 * Whether Quick Start makes its profile active without asking: when the stack has no
 * configuration of its own yet (nothing is active, or only 'default' is), or when that
 * profile is already the active one. Otherwise the user is asked first, because
 * activating changes what every new document on the stack is processed with.
 */
export const activatesWithoutAsking = (versions: ReadonlyArray<ProfileSummary>, target: string): boolean => {
  const active = versions.find((version) => version.isActive);
  return !active || active.versionName === 'default' || active.versionName === target;
};
