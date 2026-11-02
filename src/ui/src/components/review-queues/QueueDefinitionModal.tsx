// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Review Studio › Review Management: edit one system-wide production queue — its name,
// priority, reminder time limit, enabled switch and eligible Annotators — or a Test
// Studio queue's settings (the same, less the id, name and switch, which a test set
// doesn't have: its documents come from the set, not from the pipeline). Queues have
// no confidence threshold: documents are routed by their confidence alerts. Create
// mode stays in the component for the API, but Review Management doesn't open it. Every queue's skip reasons, which an Annotator
// picks from when giving an item back, are edited here one per line. A queue's stored
// checklist and reject reasons are kept as they are; the dialog doesn't edit them.
//
// A save sends back the version of the definition the form was filled from. If another
// Admin saved the queue in the meantime the API refuses it (QUEUE_CHANGED) rather than
// overwriting their change; the dialog then says so and offers to reload the queue.
import React, { useEffect, useState } from 'react';
import type { MultiselectProps } from '@cloudscape-design/components';
import {
  Alert,
  Box,
  Button,
  ColumnLayout,
  Form,
  FormField,
  Input,
  Modal,
  Multiselect,
  SpaceBetween,
  Textarea,
  Toggle,
} from '@cloudscape-design/components';

import type { ReviewQueueDefinition, ReviewQueueDefinitionInput } from '../../graphql/generated/operation-types';
import { describeApiError, extractGraphQLErrorMessage } from '../../hooks/utils/graphql-error';
import { createReviewQueue, listReviewQueues, updateReviewQueue, updateTestSetQueue } from './reviewer-assignment';

/** The server's limit on a queue's skip list. */
const MAX_SKIP_REASONS = 20;

/** A Test Studio queue (one test set) whose settings are edited. */
export interface TestSetQueueTarget {
  testSetId: string;
  name: string;
  description?: string | null;
  priority?: number | null;
  claimLeaseMinutes?: number | null;
  eligibleAnnotators?: readonly string[] | null;
  checklist?: readonly string[] | null;
  skipReasons?: readonly string[] | null;
}

/** The backend's queue id rule. */
export const QUEUE_ID_PATTERN = /^[a-z0-9][a-z0-9_.-]{0,63}$/;
const DEFAULT_TIME_LIMIT_MINUTES = 60;
const MAX_TIME_LIMIT_MINUTES = 10080;

/** True when the API refused a save because the queue changed after it was read. */
export const isQueueChangedError = (err: unknown): boolean => /\bQUEUE_CHANGED\b/.test(extractGraphQLErrorMessage(err));

interface QueueDefinitionModalProps {
  visible: boolean;
  /** The queue being edited; null to create one. */
  queue: ReviewQueueDefinition | null;
  /** A Test Studio queue's settings to edit instead of a production queue. */
  testSet?: TestSetQueueTarget | null;
  /** Ids already in use, refused when creating. */
  existingIds: readonly string[];
  annotators: readonly string[];
  annotatorsLoading: boolean;
  onDismiss: () => void;
  /** The saved production queue, or the saved settings of a Test Studio queue. */
  onSaved: (queue?: ReviewQueueDefinition, testSetSettings?: TestSetQueueTarget) => void;
}

const QueueDefinitionModal = ({
  visible,
  queue,
  testSet = null,
  existingIds,
  annotators,
  annotatorsLoading,
  onDismiss,
  onSaved,
}: QueueDefinitionModalProps): React.JSX.Element => {
  const creating = !queue && !testSet;
  const [id, setId] = useState('');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [priority, setPriority] = useState('0');
  const [timeLimit, setTimeLimit] = useState(String(DEFAULT_TIME_LIMIT_MINUTES));
  const [enabled, setEnabled] = useState(true);
  const [eligible, setEligible] = useState<readonly MultiselectProps.Option[]>([]);
  // Not edited here; saved back unchanged.
  const [checklist, setChecklist] = useState<string[]>([]);
  // One reason per line. Left empty on a new queue, the server's defaults apply.
  const [skipReasonsText, setSkipReasonsText] = useState('');
  const [submitted, setSubmitted] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The production queue the form was filled from: the prop, or a reload after a
  // refused save. Its version is what a save is checked against.
  const [base, setBase] = useState<ReviewQueueDefinition | null>(queue);
  // The last save was refused because someone else changed the queue.
  const [changed, setChanged] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [reloadError, setReloadError] = useState<string | null>(null);

  const fill = (from: ReviewQueueDefinition | null) => {
    setBase(from);
    setId(testSet?.testSetId ?? from?.id ?? '');
    setName(testSet?.name ?? from?.name ?? '');
    setDescription(testSet?.description ?? from?.description ?? '');
    setPriority(String(testSet?.priority ?? from?.priority ?? 0));
    setTimeLimit(String(testSet?.claimLeaseMinutes ?? from?.timeLimitMinutes ?? DEFAULT_TIME_LIMIT_MINUTES));
    setEnabled(from?.enabled ?? true);
    setEligible((testSet?.eligibleAnnotators ?? from?.eligibleAnnotators ?? []).map((e) => ({ value: e, label: e })));
    setChecklist([...(testSet?.checklist ?? from?.checklist ?? [])]);
    setSkipReasonsText((testSet?.skipReasons ?? from?.skipReasons ?? []).join('\n'));
    setSubmitted(false);
    setError(null);
    setChanged(false);
    setReloadError(null);
  };

  // Refilled when the dialog opens or is pointed at another queue, not when the parent
  // hands it a fresh copy of the same one (a page's poll does): that would discard what
  // has been typed, and a newer copy is what a refused save and Reload are for.
  const target = testSet ? `testset:${testSet.testSetId}` : queue ? `queue:${queue.id}` : 'new';
  useEffect(() => {
    if (!visible) return;
    fill(queue);
  }, [visible, target]);

  /** Refill the form from the queue as it is stored now, after a refused save. */
  const reload = async () => {
    if (!base) return;
    setReloading(true);
    setReloadError(null);
    try {
      const current = (await listReviewQueues()).find((q) => q.id === base.id);
      if (current) fill(current);
      else setReloadError('This queue no longer exists. It may have been deleted. Close this dialog.');
    } catch (err) {
      setReloadError(describeApiError(err, 'reload the queue'));
    } finally {
      setReloading(false);
    }
  };

  const idError = !creating
    ? undefined
    : !QUEUE_ID_PATTERN.test(id)
      ? 'Lowercase letters, digits, ".", "_" and "-", starting with a letter or digit (up to 64 characters).'
      : existingIds.includes(id)
        ? 'A queue with this id already exists.'
        : undefined;
  const nameError = name.trim() ? undefined : 'Enter a name.';
  const priorityNumber = Number(priority);
  const priorityError = Number.isInteger(priorityNumber) ? undefined : 'Enter a whole number.';
  const timeLimitNumber = Number(timeLimit);
  const timeLimitError =
    Number.isInteger(timeLimitNumber) && timeLimitNumber >= 1 && timeLimitNumber <= MAX_TIME_LIMIT_MINUTES
      ? undefined
      : `Enter a whole number of minutes from 1 to ${MAX_TIME_LIMIT_MINUTES}.`;
  const skipReasons = [
    ...new Set(
      skipReasonsText
        .split('\n')
        .map((r) => r.trim())
        .filter(Boolean),
    ),
  ];
  const skipReasonsError =
    creating || skipReasons.length > 0
      ? skipReasons.length > MAX_SKIP_REASONS
        ? `Keep it to ${MAX_SKIP_REASONS} reasons.`
        : undefined
      : 'Enter at least one reason.';

  const annotatorOptions: MultiselectProps.Option[] = [
    ...annotators.map((e) => ({ value: e, label: e })),
    // Keep a saved email that is no longer an Annotator visible, so it can be removed.
    ...eligible.filter((o) => !annotators.includes(o.value ?? '')),
  ];

  const save = async () => {
    setSubmitted(true);
    if (idError || nameError || priorityError || timeLimitError || skipReasonsError) return;
    setSaving(true);
    setError(null);
    if (testSet) {
      const eligibleAnnotators = eligible.map((o) => o.value as string);
      try {
        await updateTestSetQueue({
          testSetId: testSet.testSetId,
          description: description.trim(),
          priority: priorityNumber,
          timeLimitMinutes: timeLimitNumber,
          eligibleAnnotators,
          checklist,
          skipReasons,
        });
        onSaved(undefined, {
          ...testSet,
          description: description.trim(),
          priority: priorityNumber,
          claimLeaseMinutes: timeLimitNumber,
          eligibleAnnotators,
          skipReasons,
        });
      } catch (err) {
        setError(describeApiError(err, 'save the queue settings'));
      } finally {
        setSaving(false);
      }
      return;
    }
    const input: ReviewQueueDefinitionInput = {
      id: base?.id ?? id,
      name: name.trim(),
      description: description.trim(),
      priority: priorityNumber,
      timeLimitMinutes: timeLimitNumber,
      eligibleAnnotators: eligible.map((o) => o.value as string),
      checklist,
      // Not edited here: an existing queue keeps its list; a new one gets the defaults.
      rejectReasons: base?.rejectReasons ?? undefined,
      skipReasons: skipReasons.length > 0 ? skipReasons : undefined,
      enabled,
      // The version this form was filled from, so a save over someone else's change is
      // refused. Left out when the API did not report one.
      version: creating ? undefined : (base?.version ?? undefined),
    };
    try {
      onSaved(creating ? await createReviewQueue(input) : await updateReviewQueue(input));
    } catch (err) {
      if (!creating && isQueueChangedError(err)) setChanged(true);
      else setError(describeApiError(err, creating ? 'create the queue' : 'save the queue'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      size="large"
      header={creating ? 'Create Queue' : `Queue Settings: ${testSet?.name ?? base?.name ?? ''}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              Cancel
            </Button>
            <Button variant="primary" loading={saving} onClick={() => void save()}>
              {creating ? 'Create' : 'Save'}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <Form errorText={error ?? undefined}>
        <SpaceBetween size="m">
          {changed && (
            <Alert
              type="warning"
              header="This queue was changed by someone else"
              action={
                <Button loading={reloading} onClick={() => void reload()}>
                  Reload queue
                </Button>
              }
            >
              Another Admin saved this queue after you opened it, so your changes were not saved. Reload the queue to see its current
              settings, then make your changes again. Reloading replaces what you have entered here.
              {reloadError && <Box color="text-status-error">{reloadError}</Box>}
            </Alert>
          )}
          {base?.isDefaultQueue && (
            <Alert type="info">
              Documents with confidence alerts arrive here when HITL is enabled in their configuration and this queue is enabled.
            </Alert>
          )}
          {testSet && <Alert type="info">This Test Studio queue is the test set {testSet.name}; its documents come from the set.</Alert>}
          {/* Each pair in the two columns has a one-line description on both fields, so the
              inputs stay level; longer detail goes under the input as constraint text. */}
          <ColumnLayout columns={2}>
            {!testSet && (
              <FormField
                label="Queue id"
                description="Used in links. It cannot be changed later."
                errorText={submitted ? idError : undefined}
              >
                <Input
                  value={id}
                  disabled={!creating}
                  placeholder="e.g. claims-high-value"
                  onChange={({ detail }) => setId(detail.value)}
                />
              </FormField>
            )}
            {!testSet && (
              <FormField label="Name" description="Shown to Admins and Annotators." errorText={submitted ? nameError : undefined}>
                <Input value={name} placeholder="e.g. High-value claims" onChange={({ detail }) => setName(detail.value)} />
              </FormField>
            )}
            <FormField
              label="Priority"
              description="Higher numbers are worked first."
              constraintText="A whole number. For information: orders the queues and Next item. Kept for when there is more than one queue; with only the Default queue it changes nothing yet."
              errorText={submitted ? priorityError : undefined}
            >
              <Input type="number" inputMode="numeric" value={priority} onChange={({ detail }) => setPriority(detail.value)} />
            </FormField>
            <FormField
              label="Time limit (minutes)"
              description="When an assigned item's Annotator is reminded."
              constraintText={`For information: one reminder is sent, and the item stays assigned (assignments don't expire). 1 to ${MAX_TIME_LIMIT_MINUTES}.`}
              errorText={submitted ? timeLimitError : undefined}
            >
              <Input type="number" inputMode="numeric" value={timeLimit} onChange={({ detail }) => setTimeLimit(detail.value)} />
            </FormField>
            {!testSet && (
              <FormField label="Enabled" description="When off, no documents are added to this queue.">
                <Toggle checked={enabled} onChange={({ detail }) => setEnabled(detail.checked)}>
                  {enabled ? 'Enabled' : 'Disabled'}
                </Toggle>
              </FormField>
            )}
          </ColumnLayout>
          <FormField label="Description" stretch>
            <Textarea value={description} rows={2} onChange={({ detail }) => setDescription(detail.value)} />
          </FormField>
          <FormField
            label="Skip reasons"
            description="An Annotator who gives an item back unfinished chooses one; it is kept in the item's review history. One per line."
            constraintText={creating ? 'Leave empty to use the default reasons.' : undefined}
            errorText={submitted ? skipReasonsError : undefined}
            stretch
          >
            <Textarea value={skipReasonsText} rows={4} onChange={({ detail }) => setSkipReasonsText(detail.value)} />
          </FormField>
          <FormField
            label="Eligible Annotators"
            description={
              testSet
                ? 'The Annotators who work this queue: they see it in Home › Queues, claim its items and can be assigned them. Reviewers work every test set their configuration-profile scope admits without being listed, and claim their own items.'
                : 'The Annotators who work this queue: they see it in Home › Queues, claim its items and can be assigned them. Reviewers work every Production queue without being listed, and claim their own items.'
            }
            stretch
          >
            <Multiselect
              selectedOptions={eligible}
              onChange={({ detail }) => setEligible(detail.selectedOptions)}
              options={annotatorOptions}
              filteringType="auto"
              statusType={annotatorsLoading ? 'loading' : 'finished'}
              loadingText="Loading Annotators"
              placeholder="None (Admins and Reviewers only)"
              empty="No Annotator users. Add one in User Management."
            />
          </FormField>
        </SpaceBetween>
      </Form>
    </Modal>
  );
};

export default QueueDefinitionModal;
