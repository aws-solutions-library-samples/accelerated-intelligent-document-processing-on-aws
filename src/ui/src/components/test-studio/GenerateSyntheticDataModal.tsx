// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Modal shell for synthetic generation, serving the Schema Builder deep-link
 * (`?generate=1&version=…&className=…`) that lands directly on the form with a
 * preselected configuration profile and class. The create-test-set wizard is the normal
 * entry point; both render the same fields via useGenerateSyntheticForm.
 */

import React from 'react';
import { Box, Button, Modal, SpaceBetween } from '@cloudscape-design/components';
import useGenerateSyntheticForm from './useGenerateSyntheticForm';

interface GenerateSyntheticDataModalProps {
  visible: boolean;
  onDismiss: () => void;
  /** The testSetId is the resolved destination, for keying an optimistic row. */
  onStarted: (jobId: string, label: string, testSetId: string) => void;
  initialTab?: 'prompt' | 'config';
  initialVersion?: string;
  initialClassName?: string;
}

const GenerateSyntheticDataModal = ({
  visible,
  onDismiss,
  onStarted,
  initialTab,
  initialVersion,
  initialClassName,
}: GenerateSyntheticDataModalProps): React.JSX.Element => {
  const form = useGenerateSyntheticForm({
    active: visible,
    initialMode: initialTab,
    initialVersion,
    initialClassName,
  });

  const handleDismiss = () => {
    if (form.submitting) return;
    form.reset();
    onDismiss();
  };

  const handleGenerate = async () => {
    const started = await form.submit();
    if (started) onStarted(started.jobId, started.label, started.testSetId);
  };

  return (
    <Modal
      visible={visible}
      onDismiss={handleDismiss}
      header="Generate synthetic documents"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={handleDismiss} disabled={form.submitting}>
              Cancel
            </Button>
            <Button variant="primary" loading={form.submitting} disabled={!form.canSubmit} onClick={handleGenerate}>
              Generate
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <Box variant="p" color="text-body-secondary">
          Generate labeled synthetic documents (PDF + ground-truth JSON) with the Test Set Generator. This starts a background job; the
          resulting test set appears here when it completes.
        </Box>
        {form.fields}
      </SpaceBetween>
    </Modal>
  );
};

export default GenerateSyntheticDataModal;
