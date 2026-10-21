// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import Avatar from '@cloudscape-design/chat-components/avatar';
import { Box, SpaceBetween } from '@cloudscape-design/components';

import { formatDateTime, formatRelativeTime } from '../../utils/date-time';

const initials = (email: string): string =>
  email
    .split('@')[0]
    .split(/[._-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((p) => p[0]?.toUpperCase())
    .join('') || '?';

interface ReviewPresenceProps {
  /** The Annotator the item is assigned to; nothing renders without one. */
  assignee: string | null | undefined;
  /** Whether the assignee is the signed-in user. */
  isMe: boolean;
  /** When the item was assigned. */
  since?: string | null;
}

/** "Assigned to <email> since <time>" for an assigned production or Test Studio item. */
const ReviewPresence = ({ assignee, isMe, since }: ReviewPresenceProps): React.JSX.Element | null => {
  if (!assignee) return null;
  const who = isMe ? `you (${assignee})` : assignee;
  const when = since ? ` since ${formatRelativeTime(since)}` : '';
  return (
    <SpaceBetween direction="horizontal" size="xs" alignItems="center">
      <Avatar
        ariaLabel={`Annotator ${assignee}`}
        tooltipText={assignee}
        initials={initials(assignee)}
        color={isMe ? 'default' : 'gen-ai'}
      />
      <Box variant="small" color="text-body-secondary">
        <span title={since ? formatDateTime(since) : undefined}>{`Assigned to ${who}${when}`}</span>
      </Box>
    </SpaceBetween>
  );
};

export default ReviewPresence;
