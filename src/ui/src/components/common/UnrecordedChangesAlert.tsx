// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { Alert } from '@cloudscape-design/components';

interface UnrecordedChangesAlertProps {
  profileName: string;
  /** The revision new documents are pinned to. */
  publishedRevision?: number | null;
  /** From `listConfigProfileRevisions.profileMatchesPublished`; renders only on `false`. */
  profileMatchesPublished: boolean | null;
}

/**
 * Says so when a profile's configuration is not the revision documents run.
 *
 * New documents are pinned to a profile's published revision. A save that
 * recorded no revision (discovery's saves did, before #1778) leaves the
 * configuration the editor shows different from that revision, and without this
 * the editor labels it with the revision's number as if they were the same.
 * Renders nothing unless the server has established the difference: `null`
 * (no history, or nothing to compare) is not a warning.
 */
const UnrecordedChangesAlert = ({
  profileName,
  publishedRevision,
  profileMatchesPublished,
}: UnrecordedChangesAlertProps): React.JSX.Element | null => {
  if (profileMatchesPublished !== false) return null;
  const revision = publishedRevision ? `r${publishedRevision}` : 'its published revision';
  return (
    <Alert type="warning" header="New documents do not run this configuration">
      The configuration of <strong>{profileName}</strong> was changed without recording a revision, so documents processed with this profile
      still run <strong>{revision}</strong>. Make any change and save the profile to record a revision that matches what is shown here;
      saving it unchanged does not.
    </Alert>
  );
};

export default UnrecordedChangesAlert;
