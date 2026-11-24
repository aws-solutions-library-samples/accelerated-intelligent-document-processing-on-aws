// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// More Actions › Re-extract Sections: choose sections and extract them again with their
// current class and pages (processChanges), without regrouping. A section with no class,
// or one its configuration skips, can't be chosen.
import React, { useState } from 'react';
import { Alert, Box, Button, FormField, Modal, Multiselect, SpaceBetween } from '@cloudscape-design/components';
import type { MultiselectProps } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import useAppContext from '../../contexts/app';
import { getErrorMessage } from '../../utils/errorUtils';
import { submitProcessChanges } from '../common/process-changes';
import { documentName } from '../review-queues/review-api';
import { actionReasonMessages } from './action-reasons';

const messages = defineMessages<{
  sectionLabel: { readonly id: string };
  sectionDescription: { readonly class: string; readonly pages: string };
  noClass: NoMessageValues;
  reextracting: { readonly count: number; readonly ids: string; readonly name: string };
  failed: { readonly error: string };
  header: NoMessageValues;
  cancel: NoMessageValues;
  reextract: NoMessageValues;
  fieldLabel: NoMessageValues;
  fieldDescription: NoMessageValues;
  placeholder: NoMessageValues;
  warning: { readonly sections: string };
}>({
  sectionLabel: {
    id: 'documentActions.reextract.sectionLabel',
    defaultMessage: 'Section {id}',
    description: "A section of the document, in the Re-extract Sections dialog's list and its warning. {id} is the section's number.",
  },
  sectionDescription: {
    id: 'documentActions.reextract.sectionDescription',
    defaultMessage: '{class} · pages {pages}',
    description:
      "Second line of a section in the Re-extract Sections dialog's list. {class} is the section's class (a document type from the configuration, or No class); {pages} lists its page numbers, separated by commas.",
  },
  noClass: {
    id: 'documentActions.reextract.noClass',
    defaultMessage: 'No class',
    description: "Shown in the Re-extract Sections dialog's list in place of the class of a section that has none.",
  },
  reextracting: {
    id: 'documentActions.reextract.reextracting',
    defaultMessage: 'Re-extracting {count, plural, one {section} other {sections}} {ids} of {name}.',
    description:
      'Success notification after the Re-extract Sections dialog sent its request. {count} is how many sections; {ids} lists their numbers, separated by commas; {name} is the document name.',
  },
  failed: {
    id: 'documentActions.reextract.failed',
    defaultMessage: 'Could not re-extract the sections: {error}',
    description: "Error shown inside the Re-extract Sections dialog when its request failed. {error} is the server's reason.",
  },
  header: {
    id: 'documentActions.reextract.header',
    defaultMessage: 'Re-extract Sections',
    description: 'Title of the dialog that extracts chosen sections of the document again, opened from the More Actions menu.',
  },
  cancel: {
    id: 'documentActions.reextract.cancel',
    defaultMessage: 'Cancel',
    description: 'Button of the Re-extract Sections dialog that closes it without sending anything.',
  },
  reextract: {
    id: 'documentActions.reextract.reextract',
    defaultMessage: 'Re-extract',
    description: 'Button of the Re-extract Sections dialog that extracts the chosen sections again.',
  },
  fieldLabel: {
    id: 'documentActions.reextract.fieldLabel',
    defaultMessage: 'Sections to re-extract',
    description: "Label of the Re-extract Sections dialog's list, where the sections to extract again are chosen.",
  },
  fieldDescription: {
    id: 'documentActions.reextract.fieldDescription',
    defaultMessage: 'Each section is extracted again with its current class and pages.',
    description: "Help text under the label of the Re-extract Sections dialog's list.",
  },
  placeholder: {
    id: 'documentActions.reextract.placeholder',
    defaultMessage: 'Choose sections',
    description: "Placeholder of the Re-extract Sections dialog's list before any section is chosen.",
  },
  warning: {
    id: 'documentActions.reextract.warning',
    defaultMessage:
      'These sections are extracted again: {sections}. Their extracted values, including any corrected by hand, are replaced, and their classes are recorded as confirmed. If the document is waiting for review, its review is marked as completed, and its rule validation results are cleared. Other sections keep their values; OCR and classification are not repeated.',
    description:
      'Warning in the Re-extract Sections dialog once sections are chosen: what re-extracting replaces. {sections} lists the chosen sections, each worded as "Section <number>", separated by commas. OCR (optical character recognition) and classification are processing steps.',
  },
});

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

const ReextractSectionsDialog = ({
  objectKey,
  sections,
  disabledReason,
  onDone,
  onDismiss,
}: ReextractSectionsDialogProps): React.JSX.Element => {
  const intl = useIntl();
  const { setSuccessMessage } = useAppContext();
  // The chosen sections' ids, in the order chosen; their options are built at render.
  const [chosen, setChosen] = useState<readonly string[]>([]);
  const [busy, setBusy] = useState(false);
  // What the request threw, worded at render.
  const [error, setError] = useState<{ cause: unknown } | null>(null);

  const sectionLabel = (id: string | number) => intl.formatMessage(messages.sectionLabel, { id: String(id) });
  const options: MultiselectProps.Option[] = sections.map((s) => ({
    value: String(s.Id),
    label: sectionLabel(s.Id),
    description: intl.formatMessage(messages.sectionDescription, {
      class: s.Class ?? intl.formatMessage(messages.noClass),
      pages: (s.PageIds ?? []).join(', '),
    }),
    disabled: !s.Class || !!s.Excluded,
    disabledReason: !s.Class
      ? intl.formatMessage(actionReasonMessages.sectionNoClass)
      : s.Excluded
        ? intl.formatMessage(actionReasonMessages.sectionSkipped)
        : undefined,
  }));
  // A chosen section that is no longer listed keeps its token.
  const selectedOptions = chosen.map((value) => options.find((o) => o.value === value) ?? { value, label: sectionLabel(value) });
  const chosenSections = sections.filter((s) => chosen.includes(String(s.Id)));
  const ids = chosenSections.map((s) => String(s.Id));
  const reason = disabledReason ?? (chosenSections.length === 0 ? intl.formatMessage(actionReasonMessages.chooseSections) : undefined);

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
      setSuccessMessage(
        intl.formatMessage(messages.reextracting, { count: ids.length, ids: ids.join(', '), name: documentName(objectKey) }),
      );
      onDone();
      onDismiss();
    } catch (err) {
      setError({ cause: err });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      visible
      header={intl.formatMessage(messages.header)}
      onDismiss={onDismiss}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={busy}>
              <FormattedMessage {...messages.cancel} />
            </Button>
            <Button variant="primary" loading={busy} disabled={!!reason} disabledReason={reason} onClick={() => void reextract()}>
              <FormattedMessage {...messages.reextract} />
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {error && (
          <Alert type="error">
            <FormattedMessage {...messages.failed} values={{ error: getErrorMessage(error.cause) }} />
          </Alert>
        )}
        <FormField label={intl.formatMessage(messages.fieldLabel)} description={intl.formatMessage(messages.fieldDescription)}>
          <Multiselect
            selectedOptions={selectedOptions}
            options={options}
            placeholder={intl.formatMessage(messages.placeholder)}
            onChange={({ detail }) => setChosen(detail.selectedOptions.map((o) => o.value as string))}
          />
        </FormField>
        {chosenSections.length > 0 && (
          <Alert type="warning">
            <FormattedMessage {...messages.warning} values={{ sections: ids.map(sectionLabel).join(', ') }} />
          </Alert>
        )}
      </SpaceBetween>
    </Modal>
  );
};

export default ReextractSectionsDialog;
