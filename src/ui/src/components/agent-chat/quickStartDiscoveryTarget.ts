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
 * Whether Quick Start makes its profile active without asking: when that profile is
 * already the active one, or when the stack has no profile of its own besides it, only
 * 'default'. Otherwise the user is asked first, because activating changes what every
 * new document on the stack is processed with, and another profile may be in use.
 */
export const activatesWithoutAsking = (versions: ReadonlyArray<ProfileSummary>, target: string): boolean =>
  versions.some((version) => version.isActive && version.versionName === target) ||
  versions.every((version) => version.versionName === 'default' || version.versionName === target);
