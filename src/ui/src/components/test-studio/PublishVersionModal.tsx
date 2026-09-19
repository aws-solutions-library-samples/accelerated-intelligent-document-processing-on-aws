// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * PublishVersionModal — freeze a test set's current documents and labels into a
 * numbered, immutable version.
 *
 * It sits on the set's own page, beside the label and annotation controls, because
 * publishing is the last step of the labelling pass those two start: generate draft
 * labels, review them, then freeze the result.
 *
 * The dialog exists because the outcome is not self-evident from a menu item.
 * Publishing writes a version that is never rewritten, and by default it also
 * repoints the set's active reference — the baseline every later test run is scored
 * against. It collects the label and notes the mutation accepts, which is the only
 * way a reader of the version list later knows what a version was for.
 */

import React, { useEffect, useState } from 'react';
import { Alert, Box, Button, Checkbox, FormField, Input, Modal, SpaceBetween, Textarea } from '@cloudscape-design/components';

export interface PublishVersionInput {
  label?: string;
  notes?: string;
  setAsActiveReference: boolean;
}

interface PublishVersionModalProps {
  visible: boolean;
  testSetId: string;
  /** Documents the version will freeze. `null` while the set's size is unknown. */
  documentCount: number | null;
  /**
   * Highest version already published: `0` when none has been, `null` while it is
   * not known. The server assigns the real number, so this is context rather than
   * an input — the dialog stays usable without it.
   */
  latestVersion: number | null;
  submitting: boolean;
  onDismiss: () => void;
  onConfirm: (input: PublishVersionInput) => void;
}

const PublishVersionModal = ({
  visible,
  testSetId,
  documentCount,
  latestVersion,
  submitting,
  onDismiss,
  onConfirm,
}: PublishVersionModalProps): React.JSX.Element => {
  const [label, setLabel] = useState('');
  const [notes, setNotes] = useState('');
  const [setAsActiveReference, setSetAsActiveReference] = useState(true);

  // A dialog reopened after a publish must not still hold the previous entry, and
  // the active-reference choice is per-publish rather than per-session.
  useEffect(() => {
    if (visible) {
      setLabel('');
      setNotes('');
      setSetAsActiveReference(true);
    }
  }, [visible]);

  const nextVersion = latestVersion === null ? null : latestVersion + 1;

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={nextVersion === null ? `Publish a version of ${testSetId}` : `Publish version ${nextVersion} of ${testSetId}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={submitting}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={() => onConfirm({ label: label.trim() || undefined, notes: notes.trim() || undefined, setAsActiveReference })}
              loading={submitting}
              disabled={documentCount === 0}
            >
              Publish version
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <Box>
          Freezes {documentCount === null ? 'this test set' : `these ${documentCount} document(s)`} and the ground truth they currently
          carry into a numbered version. A published version is never rewritten, so later edits to the set do not change it and test runs
          scored against it stay comparable.
        </Box>

        <FormField label="Label (optional)" description="A short name for this version, shown wherever versions are listed.">
          <Input
            value={label}
            onChange={({ detail }) => setLabel(detail.value)}
            placeholder={nextVersion === null ? 'e.g. reviewed by finance' : `Defaults to v${nextVersion}`}
            disabled={submitting}
          />
        </FormField>

        <FormField label="Notes (optional)" description="What changed in this version, for whoever reads the list later.">
          <Textarea value={notes} onChange={({ detail }) => setNotes(detail.value)} rows={3} disabled={submitting} />
        </FormField>

        <Checkbox
          checked={setAsActiveReference}
          onChange={({ detail }) => setSetAsActiveReference(detail.checked)}
          disabled={submitting}
          description="The active reference is the baseline new test runs are scored against."
        >
          Make this the active reference
        </Checkbox>

        {!setAsActiveReference && (
          <Alert type="info">
            The set&apos;s active reference is left as it is, so new test runs continue to be scored against the version it already points
            at rather than against this one.
          </Alert>
        )}

        {documentCount === 0 && <Alert type="warning">This test set has no documents. Add documents before publishing a version.</Alert>}
      </SpaceBetween>
    </Modal>
  );
};

export default PublishVersionModal;
