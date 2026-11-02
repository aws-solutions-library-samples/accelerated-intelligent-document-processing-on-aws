// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// More Actions › Re-extract Sections: choose sections and extract them again with their
// current class and pages (processChanges), without regrouping. A section with no class,
// or one its configuration skips, can't be chosen.
import React, { useState } from 'react';
import { Alert, Box, Button, FormField, Modal, Multiselect, SpaceBetween } from '@cloudscape-design/components';
import type { MultiselectProps } from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import { getErrorMessage } from '../../utils/errorUtils';
import { submitProcessChanges } from '../common/process-changes';
import { documentName } from '../review-queues/review-api';
import { CHOOSE_SECTIONS_REASON, SECTION_NO_CLASS_REASON, SECTION_SKIPPED_REASON } from './action-reasons';

interface DialogSection {
  Id: string | number;
  Class?: string | null;
  PageIds?: (string | number)[] | null;
  Excluded?: boolean | null;
}

interface ReextractSectionsDialogProps {
  objectKey: string;
  sections: DialogSection[];
  /** Why re-extracting can't run now (the live gate); the commit button shows it. */
  disabledReason?: string;
  /** After the request was accepted: re-read the document. */
  onDone: () => void;
  onDismiss: () => void;
}

const plural = (n: number) => (n > 1 ? 's' : '');

const ReextractSectionsDialog = ({
  objectKey,
  sections,
  disabledReason,
  onDone,
  onDismiss,
}: ReextractSectionsDialogProps): React.JSX.Element => {
  const { setSuccessMessage } = useAppContext();
  const [chosen, setChosen] = useState<readonly MultiselectProps.Option[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const options: MultiselectProps.Option[] = sections.map((s) => ({
    value: String(s.Id),
    label: `Section ${s.Id}`,
    description: `${s.Class ?? 'No class'} · pages ${(s.PageIds ?? []).join(', ')}`,
    disabled: !s.Class || !!s.Excluded,
    disabledReason: !s.Class ? SECTION_NO_CLASS_REASON : s.Excluded ? SECTION_SKIPPED_REASON : undefined,
  }));
  const chosenSections = sections.filter((s) => chosen.some((o) => o.value === String(s.Id)));
  const ids = chosenSections.map((s) => String(s.Id));
  const reason = disabledReason ?? (chosenSections.length === 0 ? CHOOSE_SECTIONS_REASON : undefined);

  const reextract = async () => {
    setBusy(true);
    setError(null);
    try {
      await submitProcessChanges(
        objectKey,
        chosenSections.map((s) => ({
          sectionId: String(s.Id),
          classification: s.Class as string,
          pageIds: (s.PageIds ?? []).map(Number),
          isNew: false,
          isDeleted: false,
        })),
      );
      setSuccessMessage(`Re-extracting section${plural(ids.length)} ${ids.join(', ')} of ${documentName(objectKey)}.`);
      onDone();
      onDismiss();
    } catch (err) {
      setError(`Could not re-extract the sections: ${getErrorMessage(err)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      visible
      header="Re-extract Sections"
      onDismiss={onDismiss}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={busy}>
              Cancel
            </Button>
            <Button variant="primary" loading={busy} disabled={!!reason} disabledReason={reason} onClick={() => void reextract()}>
              Re-extract
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {error && <Alert type="error">{error}</Alert>}
        <FormField label="Sections to re-extract" description="Each section is extracted again with its current class and pages.">
          <Multiselect
            selectedOptions={chosen}
            options={options}
            placeholder="Choose sections"
            onChange={({ detail }) => setChosen(detail.selectedOptions)}
          />
        </FormField>
        {chosenSections.length > 0 && (
          <Alert type="warning">
            These sections are extracted again: {ids.map((id) => `Section ${id}`).join(', ')}. Their extracted values, including any
            corrected by hand, are replaced, and their classes are recorded as confirmed. If the document is waiting for review, its review
            is marked as completed, and its rule validation results are cleared. Other sections keep their values; OCR and classification
            are not repeated.
          </Alert>
        )}
      </SpaceBetween>
    </Modal>
  );
};

export default ReextractSectionsDialog;
