// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit screen's History, in every mode: the open section's Field Edits (its
// `_editHistory`, prediction and baseline) and the Review Activity (who claimed, was
// assigned, skipped or completed it, and why). Processing Report is its own button.
import React from 'react';
import { Box, Button, Modal, Tabs } from '@cloudscape-design/components';

import type { ReviewItemDetail } from '../../graphql/generated/operation-types';
import { ReviewHistoryTable } from '../review-queues/ReviewHistoryModal';
import FieldEditsTable from './FieldEditsTable';

/** Every tab shares one height, so switching tabs doesn't resize the dialog. */
const TAB_HEIGHT = '420px';
const tabBody = (content: React.ReactNode): React.ReactNode => <div style={{ height: TAB_HEIGHT, overflow: 'auto' }}>{content}</div>;

interface EditorHistoryModalProps {
  visible: boolean;
  onDismiss: () => void;
  /** The open section's result, whose `_editHistory` is shown. */
  sectionData: Record<string, unknown> | null;
  baselineData?: Record<string, unknown> | null;
  /** The review activity (claims, assignments, skips, completions); empty shows an empty state. */
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
          // The same table as Review Activity: one row per changed field, newest first.
          content: tabBody(<FieldEditsTable sectionData={sectionData} baselineData={baselineData} />),
        },
        { id: 'review', label: 'Review Activity', content: tabBody(<ReviewHistoryTable history={reviewHistory ?? []} />) },
      ]}
    />
  </Modal>
);

export default EditorHistoryModal;
