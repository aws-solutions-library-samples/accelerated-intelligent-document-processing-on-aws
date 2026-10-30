// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// More Actions › Reset Page Class: choose pages whose class is reset, then reprocess
// (processChanges with `classReset`). A reset classifies the whole document again and
// rebuilds and re-extracts every section, which the dialog says before anything is sent.
import React, { useState } from 'react';
import { Alert, Box, Button, FormField, Modal, Multiselect, SpaceBetween } from '@cloudscape-design/components';
import type { MultiselectProps } from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import { getErrorMessage } from '../../utils/errorUtils';
import { submitProcessChanges } from '../common/process-changes';
import { documentName } from '../review-queues/review-api';
import { CHOOSE_PAGES_REASON } from './action-reasons';

interface DialogPage {
  Id: string | number;
  Class?: string | null;
}

interface ResetPageClassDialogProps {
  objectKey: string;
  pages: DialogPage[];
  /** Why resetting can't run now (the live gate); the commit button shows it. */
  disabledReason?: string;
  onDone: () => void;
  onDismiss: () => void;
}

const plural = (n: number) => (n > 1 ? 's' : '');

const ResetPageClassDialog = ({ objectKey, pages, disabledReason, onDone, onDismiss }: ResetPageClassDialogProps): React.JSX.Element => {
  const { setSuccessMessage } = useAppContext();
  const [chosen, setChosen] = useState<readonly MultiselectProps.Option[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const options: MultiselectProps.Option[] = pages.map((p) => ({
    value: String(p.Id),
    label: `Page ${p.Id}`,
    description: p.Class || 'Unclassified',
  }));
  const ids = chosen.map((o) => o.value as string);
  const reason = disabledReason ?? (ids.length === 0 ? CHOOSE_PAGES_REASON : undefined);

  const reset = async () => {
    setBusy(true);
    setError(null);
    try {
      await submitProcessChanges(
        objectKey,
        [],
        ids.map((id) => ({ pageId: Number(id), classReset: true, textModified: false })),
      );
      setSuccessMessage(
        `Resetting the class of page${plural(ids.length)} ${ids.join(', ')} of ${documentName(objectKey)}: the document is being classified and extracted again.`,
      );
      onDone();
      onDismiss();
    } catch (err) {
      setError(`Could not reset the page class: ${getErrorMessage(err)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      visible
      header="Reset Page Class"
      onDismiss={onDismiss}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={busy}>
              Cancel
            </Button>
            <Button variant="primary" loading={busy} disabled={!!reason} disabledReason={reason} onClick={() => void reset()}>
              Reset Page Class
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {error && <Alert type="error">{error}</Alert>}
        <FormField label="Pages to reset">
          <Multiselect
            selectedOptions={chosen}
            options={options}
            placeholder="Choose pages"
            onChange={({ detail }) => setChosen(detail.selectedOptions)}
          />
        </FormField>
        <Alert type="warning">
          <SpaceBetween size="xs">
            <Box>Resetting a page&apos;s class classifies the whole document again:</Box>
            <ul>
              <li>Every page is classified again, so pages can move to other sections and sections can be added or removed.</li>
              <li>
                Every section is rebuilt and extracted again. Values corrected by hand in any section, not only the ones holding these
                pages, are replaced.
              </li>
              <li>
                If the document is waiting for review, its review is marked as completed, and its rule validation results are cleared.
              </li>
            </ul>
          </SpaceBetween>
        </Alert>
      </SpaceBetween>
    </Modal>
  );
};

export default ResetPageClassDialog;
