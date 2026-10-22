// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The unified editor's History: the open section's field edits (its `_editHistory`,
// prediction and baseline), and, for a production queue item, the item's review
// activity (who claimed, skipped or completed it, and why).
import React from 'react';
import { Box, Button, Modal, Tabs } from '@cloudscape-design/components';

import type { ReviewItemDetail } from '../../graphql/generated/operation-types';
import EditHistoryTab from '../document-viewer/EditHistoryTab';
import { ReviewHistoryTable } from '../review-queues/ReviewHistoryModal';

/** Both tabs share one height, so switching tabs doesn't resize the dialog. */
const TAB_HEIGHT = '420px';
const tabBody = (content: React.ReactNode): React.ReactNode => <div style={{ height: TAB_HEIGHT, overflow: 'auto' }}>{content}</div>;

interface EditorHistoryModalProps {
  visible: boolean;
  onDismiss: () => void;
  /** The open section's result, whose `_editHistory` is listed. */
  sectionData: Record<string, unknown> | null;
  baselineData?: Record<string, unknown> | null;
  /** The review item's activity; the Review activity tab is shown only with it. */
  reviewHistory?: ReviewItemDetail['history'];
}

const EditorHistoryModal = ({
  visible,
  onDismiss,
  sectionData,
  baselineData,
  reviewHistory,
}: EditorHistoryModalProps): React.JSX.Element => (
  <Modal
    visible={visible}
    size="large"
    onDismiss={onDismiss}
    header="History"
    footer={
      <Box float="right">
        <Button variant="primary" onClick={onDismiss}>
          Close
        </Button>
      </Box>
    }
  >
    <Tabs
      tabs={[
        {
          id: 'fields',
          label: 'Field Edits',
          content: tabBody(
            sectionData ? (
              <EditHistoryTab predictionData={sectionData} baselineData={baselineData ?? null} height="auto" />
            ) : (
              <Box color="text-body-secondary">Open a section to see its field edits.</Box>
            ),
          ),
        },
        ...(reviewHistory
          ? [{ id: 'review', label: 'Review Activity', content: tabBody(<ReviewHistoryTable history={reviewHistory} />) }]
          : []),
      ]}
    />
  </Modal>
);

export default EditorHistoryModal;
