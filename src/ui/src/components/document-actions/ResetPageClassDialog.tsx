// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// More Actions › Reset Page Class: choose pages whose class is reset, then reprocess
// (processChanges with `classReset`). A reset classifies the whole document again and
// rebuilds and re-extracts every section, which the dialog says before anything is sent.
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
  pageLabel: { readonly id: string };
  unclassified: NoMessageValues;
  resetting: { readonly count: number; readonly ids: string; readonly name: string };
  failed: { readonly error: string };
  header: NoMessageValues;
  cancel: NoMessageValues;
  confirm: NoMessageValues;
  fieldLabel: NoMessageValues;
  placeholder: NoMessageValues;
  intro: NoMessageValues;
  everyPage: NoMessageValues;
  everySection: NoMessageValues;
  reviewCompleted: NoMessageValues;
}>({
  pageLabel: {
    id: 'documentActions.resetPageClass.pageLabel',
    defaultMessage: 'Page {id}',
    description: "A page of the document in the Reset Page Class dialog's list. {id} is the page's number.",
  },
  unclassified: {
    id: 'documentActions.resetPageClass.unclassified',
    defaultMessage: 'Unclassified',
    description: "Shown in the Reset Page Class dialog's list in place of the class of a page that has none.",
  },
  resetting: {
    id: 'documentActions.resetPageClass.resetting',
    defaultMessage:
      'Resetting the class of {count, plural, one {page} other {pages}} {ids} of {name}: the document is being classified and extracted again.',
    description:
      'Success notification after the Reset Page Class dialog sent its request. {count} is how many pages; {ids} lists their numbers, separated by commas; {name} is the document name.',
  },
  failed: {
    id: 'documentActions.resetPageClass.failed',
    defaultMessage: 'Could not reset the page class: {error}',
    description: "Error shown inside the Reset Page Class dialog when its request failed. {error} is the server's reason.",
  },
  header: {
    id: 'documentActions.resetPageClass.header',
    defaultMessage: 'Reset Page Class',
    description:
      'Title of the dialog that resets the class of chosen pages and classifies the document again, opened from the More Actions menu.',
  },
  cancel: {
    id: 'documentActions.resetPageClass.cancel',
    defaultMessage: 'Cancel',
    description: 'Button of the Reset Page Class dialog that closes it without sending anything.',
  },
  confirm: {
    id: 'documentActions.resetPageClass.confirm',
    defaultMessage: 'Reset Page Class',
    description: 'Button of the Reset Page Class dialog that resets the class of the chosen pages.',
  },
  fieldLabel: {
    id: 'documentActions.resetPageClass.fieldLabel',
    defaultMessage: 'Pages to reset',
    description: "Label of the Reset Page Class dialog's list, where the pages whose class is reset are chosen.",
  },
  placeholder: {
    id: 'documentActions.resetPageClass.placeholder',
    defaultMessage: 'Choose pages',
    description: "Placeholder of the Reset Page Class dialog's list before any page is chosen.",
  },
  intro: {
    id: 'documentActions.resetPageClass.intro',
    defaultMessage: "Resetting a page's class classifies the whole document again:",
    description: 'Opening line of the warning in the Reset Page Class dialog, followed by a list of what a reset does.',
  },
  everyPage: {
    id: 'documentActions.resetPageClass.everyPage',
    defaultMessage: 'Every page is classified again, so pages can move to other sections and sections can be added or removed.',
    description: "Item of the list in the Reset Page Class dialog's warning: what a reset does to the pages.",
  },
  everySection: {
    id: 'documentActions.resetPageClass.everySection',
    defaultMessage:
      'Every section is rebuilt and extracted again. Values corrected by hand in any section, not only the ones holding these pages, are replaced.',
    description: "Item of the list in the Reset Page Class dialog's warning: what a reset does to the sections and their values.",
  },
  reviewCompleted: {
    id: 'documentActions.resetPageClass.reviewCompleted',
    defaultMessage:
      'If the document is waiting for review, its review is marked as completed, and its rule validation results are cleared.',
    description: "Item of the list in the Reset Page Class dialog's warning: what a reset does to the document's review.",
  },
});

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

const ResetPageClassDialog = ({ objectKey, pages, disabledReason, onDone, onDismiss }: ResetPageClassDialogProps): React.JSX.Element => {
  const intl = useIntl();
  const { setSuccessMessage } = useAppContext();
  // The chosen pages' ids, in the order chosen; their options are built at render.
  const [ids, setIds] = useState<readonly string[]>([]);
  const [busy, setBusy] = useState(false);
  // What the request threw, worded at render.
  const [error, setError] = useState<{ cause: unknown } | null>(null);

  const pageLabel = (id: string | number) => intl.formatMessage(messages.pageLabel, { id: String(id) });
  const options: MultiselectProps.Option[] = pages.map((p) => ({
    value: String(p.Id),
    label: pageLabel(p.Id),
    description: p.Class || intl.formatMessage(messages.unclassified),
  }));
  // A chosen page that is no longer listed keeps its token.
  const selectedOptions = ids.map((value) => options.find((o) => o.value === value) ?? { value, label: pageLabel(value) });
  const reason = disabledReason ?? (ids.length === 0 ? intl.formatMessage(actionReasonMessages.choosePages) : undefined);

  const reset = async () => {
    setBusy(true);
    setError(null);
    try {
      await submitProcessChanges(
        objectKey,
        [],
        ids.map((id) => ({ pageId: Number(id), classReset: true, textModified: false })),
      );
      setSuccessMessage(intl.formatMessage(messages.resetting, { count: ids.length, ids: ids.join(', '), name: documentName(objectKey) }));
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
            <Button variant="primary" loading={busy} disabled={!!reason} disabledReason={reason} onClick={() => void reset()}>
              <FormattedMessage {...messages.confirm} />
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
        <FormField label={intl.formatMessage(messages.fieldLabel)}>
          <Multiselect
            selectedOptions={selectedOptions}
            options={options}
            placeholder={intl.formatMessage(messages.placeholder)}
            onChange={({ detail }) => setIds(detail.selectedOptions.map((o) => o.value as string))}
          />
        </FormField>
        <Alert type="warning">
          <SpaceBetween size="xs">
            <Box>
              <FormattedMessage {...messages.intro} />
            </Box>
            <ul>
              <li>
                <FormattedMessage {...messages.everyPage} />
              </li>
              <li>
                <FormattedMessage {...messages.everySection} />
              </li>
              <li>
                <FormattedMessage {...messages.reviewCompleted} />
              </li>
            </ul>
          </SpaceBetween>
        </Alert>
      </SpaceBetween>
    </Modal>
  );
};

export default ResetPageClassDialog;
