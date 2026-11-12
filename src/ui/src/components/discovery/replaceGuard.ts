// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// The server refuses saveMode "replace" on the active configuration profile: replace
// clears the profile's classes before discovery runs, and documents arriving during
// the run would be classified against an empty list. The panels apply the same rule
// before submitting, so the user is told before anything is uploaded.

export const REPLACE_ON_ACTIVE_MESSAGE =
  "Replace can't be used on the active configuration profile, because documents being processed would be classified against an empty class list while discovery runs. Choose Add to existing schema, or discover into another profile and activate it afterwards.";

interface VersionLike {
  versionName: string;
  isActive?: boolean | null;
}

export function isReplaceOnActive(saveMode: string, selectedProfile: string | null | undefined, versions: readonly VersionLike[]): boolean {
  if (saveMode !== 'replace' || !selectedProfile) return false;
  return versions.some((v) => v.versionName === selectedProfile && !!v.isActive);
}
