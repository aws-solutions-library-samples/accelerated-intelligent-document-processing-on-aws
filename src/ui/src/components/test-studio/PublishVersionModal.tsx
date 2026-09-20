// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * PublishVersionModal — record a numbered version of a test set's current documents
 * and labels.
 *
 * It sits on the set's own page, beside the label and annotation controls, because
 * publishing is the last step of the labelling pass those two start: generate draft
 * labels, review them, then mark the result as a version.
 *
 * The dialog exists because the outcome is not self-evident from a menu item.
 * Publishing writes a numbered version and, by default, also moves the set's active
 * reference — the version the Test Sets table reports as the set's reference point,
 * and one a run can be pinned to by choosing it in the runner. It collects the label
 * and notes the mutation accepts, which is the only way a reader of the version list
 * later knows what a version was for.
 *
 * ⚠️ Do **not** describe the active reference as what test runs are scored against.
 * Nothing scores against it: `test_runner` never reads it, and its version picker
 * defaults to the set's *current* labels precisely so the ordinary loop scores the
 * corrections just made rather than the last published state.
 *
 * Publishing does copy bytes: the resolver writes the version row *and* copies the set's
 * labels to `{testSetId}/versions/{n}/baseline/`, which is why the dialog can say the
 * version's content is settled and why it warns that a large set takes a moment. The copy
 * is bounded — an oversize set is refused with that as the reason rather than recorded as
 * a version it cannot back with bytes — so the mutation can fail for a reason that is not
 * the caller's fault, and the error is surfaced rather than swallowed.
 *
 * ⚠️ A failure message does not prove nothing happened. The copy can outlast the request
 * budget the dispatcher allows, so the caller can be told it failed after it succeeded.
 * `TestSetDetail` therefore sends a `clientToken` that survives a retry, which is what stops
 * the retry creating a second version and a second copy — do not drop it from the call.
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
  /** Documents the version will cover. `null` while the set's size is unknown. */
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
          Records a numbered version of {documentCount === null ? 'this test set' : `this test set's ${documentCount} document(s)`} and
          copies the ground truth they currently carry, so a test run can name the state of the labels it was scored against. Later
          annotation and later draft-labelling runs do not change what this version holds. Publishing a large set takes a moment while the
          labels are copied.
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
          description="The version the Test Sets table reports as this set's reference point. It does not decide what a test run is scored against — the runner picks that, defaulting to the set's current labels."
        >
          Make this the active reference
        </Checkbox>

        {!setAsActiveReference && (
          <Alert type="info">
            The set&apos;s reference point is left where it is, so the Test Sets table keeps reporting the version it already names.
          </Alert>
        )}

        {documentCount === 0 && <Alert type="warning">This test set has no documents. Add documents before publishing a version.</Alert>}
      </SpaceBetween>
    </Modal>
  );
};

export default PublishVersionModal;
