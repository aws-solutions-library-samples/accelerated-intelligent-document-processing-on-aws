// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { Link } from '@cloudscape-design/components';

export interface ConfigVersion {
  version?: string;
  versionName: string;
  description?: string;
  created?: string;
  isActive?: boolean;
  managed?: boolean;
  [key: string]: unknown;
}

/**
 * Suffix naming the pinned revision, e.g. " r7".
 *
 * Empty when nothing was pinned: a profile with no revision history is the normal
 * state on a deployment that has not saved a configuration since revisions were
 * introduced, and "r?" would imply information that does not exist.
 */
const revisionSuffix = (configRevision: number | null | undefined): string =>
  configRevision === null || configRevision === undefined ? '' : ` r${configRevision}`;

export const formatConfigVersionLink = (
  configVersion: string | null | undefined,
  versions: ConfigVersion[],
  _maxDescLength = 10,
  configRevision?: number | null,
): React.JSX.Element | string => {
  if (!configVersion) return 'N/A';

  const versionFromList = versions.find((v) => v.versionName === configVersion);
  const suffix = revisionSuffix(configRevision);

  // If version not found in current versions list, show as deleted
  if (!versionFromList) {
    return (
      <span style={{ textDecoration: 'line-through', color: '#687078' }}>
        {configVersion}
        {suffix}
      </span>
    );
  }

  return (
    <Link href={`#/documents/config?version=${configVersion}`}>
      {configVersion}
      {suffix}
    </Link>
  );
};

export const formatConfigVersionText = (
  configVersion: string | null | undefined,
  versions: ConfigVersion[],
  configRevision?: number | null,
): string => {
  if (!configVersion) return 'N/A';

  const versionFromList = versions.find((v) => v.versionName === configVersion);
  const suffix = revisionSuffix(configRevision);

  // If version not found in current versions list, show as deleted
  if (!versionFromList) {
    return `${configVersion}${suffix} (deleted)`;
  }

  return `${configVersion}${suffix}`;
};
