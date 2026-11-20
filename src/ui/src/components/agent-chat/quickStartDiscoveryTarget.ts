// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

interface ProfileSummary {
  versionName: string;
  isActive?: boolean | null;
  managed?: boolean | null;
}

const pad = (value: number): string => String(value).padStart(2, '0');

const randomSuffix = (): string => crypto.randomUUID().slice(0, 4);

/**
 * A new profile name for the documents a Quick Start conversation attaches, so Discovery
 * never merges them into an existing profile: not the active one, which may be
 * stack-managed or another user's, and not a profile shared between conversations.
 * The random suffix keeps two conversations started in the same second apart.
 */
export const newQuickStartProfileName = (now: Date, suffix: string = randomSuffix()): string =>
  `quickstart-${now.getUTCFullYear()}${pad(now.getUTCMonth() + 1)}${pad(now.getUTCDate())}-` +
  `${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}${pad(now.getUTCSeconds())}-${suffix}`;

/**
 * Whether Quick Start makes its profile active without asking: when that profile is
 * already the active one, or when the stack has no profile of its own yet. That is how a
 * stack is deployed: 'default' active, and the stack-managed presets it was deployed
 * with beside it, inactive. Otherwise the user is asked first, because activating
 * changes what every new document on the stack is processed with: an active preset
 * was chosen by someone, and any other profile may be in use.
 * An empty list is the latter case: every stack has 'default', so a caller who sees
 * none sees a list narrowed by their profile scope.
 */
export const activatesWithoutAsking = (versions: ReadonlyArray<ProfileSummary>, target: string): boolean => {
  if (versions.some((version) => version.isActive && version.versionName === target)) return true;
  const active = versions.find((version) => version.isActive);
  if (active && active.versionName !== 'default') return false;
  return (
    versions.length > 0 &&
    versions.every((version) => version.versionName === 'default' || version.versionName === target || Boolean(version.managed))
  );
};
