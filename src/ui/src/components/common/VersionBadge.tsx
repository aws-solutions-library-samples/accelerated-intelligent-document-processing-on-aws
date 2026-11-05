// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The `vN → vM` badge of an open test-set version transition, with what it means on
// hover. The test set's queue page and the edit page's header both show it.
import React from 'react';
import { Badge, Popover } from '@cloudscape-design/components';

interface VersionBadgeProps {
  /** The badge text, e.g. `v1 → v2`. */
  text: string;
  /** Shown on hover: which version the corrections go into, and which is preserved. */
  hover: string;
}

const VersionBadge = ({ text, hover }: VersionBadgeProps): React.JSX.Element => (
  <Popover dismissButton={false} position="bottom" size="medium" triggerType="custom" content={hover}>
    <Badge color="blue">{text}</Badge>
  </Popover>
);

export default VersionBadge;
