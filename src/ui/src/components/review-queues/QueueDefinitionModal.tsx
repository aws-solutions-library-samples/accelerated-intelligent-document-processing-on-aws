// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// HITL › Queue Management: create or edit one system-wide production queue — its
// name, priority, reminder time limit, confidence threshold, enabled switch and
// eligible Annotators — or edit a Test Studio queue's settings (the same, less the
// id, name, threshold and switch, which a test set doesn't have: its documents come
// from the set, not from the pipeline). A production queue's skip reasons, which an
// Annotator picks from when giving an item back, are edited here one per line. Its
// stored checklist and reject reasons are kept as they are; the dialog doesn't edit
// them.
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
import { describeApiError } from '../../hooks/utils/graphql-error';
import { createReviewQueue, updateReviewQueue, updateTestSetQueue } from './reviewer-assignment';

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
}

/** The backend's queue id rule. */
export const QUEUE_ID_PATTERN = /^[a-z0-9][a-z0-9_.-]{0,63}$/;
const DEFAULT_TIME_LIMIT_MINUTES = 60;
const MAX_TIME_LIMIT_MINUTES = 10080;
/** The backend's default for a new queue. */
export const DEFAULT_CONFIDENCE_THRESHOLD = 0.8;

/** The error for a confidence threshold that isn't a number from 0 to 1, or undefined. */
export const thresholdError = (value: string): string | undefined => {
  const n = Number(value);
  return value.trim() !== '' && Number.isFinite(n) && n >= 0 && n <= 1 ? undefined : 'Enter a number from 0 to 1.';
};

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
  const [threshold, setThreshold] = useState(String(DEFAULT_CONFIDENCE_THRESHOLD));
  const [enabled, setEnabled] = useState(true);
  const [eligible, setEligible] = useState<readonly MultiselectProps.Option[]>([]);
  // Not edited here; saved back unchanged.
  const [checklist, setChecklist] = useState<string[]>([]);
  // One reason per line. Left empty on a new queue, the server's defaults apply.
  const [skipReasonsText, setSkipReasonsText] = useState('');
  const [submitted, setSubmitted] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!visible) return;
    setId(testSet?.testSetId ?? queue?.id ?? '');
    setName(testSet?.name ?? queue?.name ?? '');
    setDescription(testSet?.description ?? queue?.description ?? '');
    setPriority(String(testSet?.priority ?? queue?.priority ?? 0));
    setTimeLimit(String(testSet?.claimLeaseMinutes ?? queue?.timeLimitMinutes ?? DEFAULT_TIME_LIMIT_MINUTES));
    setThreshold(String(queue?.confidenceThreshold ?? DEFAULT_CONFIDENCE_THRESHOLD));
    setEnabled(queue?.enabled ?? true);
    setEligible((testSet?.eligibleAnnotators ?? queue?.eligibleAnnotators ?? []).map((e) => ({ value: e, label: e })));
    setChecklist([...(testSet?.checklist ?? queue?.checklist ?? [])]);
    setSkipReasonsText((queue?.skipReasons ?? []).join('\n'));
    setSubmitted(false);
    setError(null);
  }, [visible, queue, testSet]);

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
  // A Test Studio queue has no threshold or switch, so neither is checked there.
  const confidenceError = testSet ? undefined : thresholdError(threshold);
  const skipReasons = [
    ...new Set(
      skipReasonsText
        .split('\n')
        .map((r) => r.trim())
        .filter(Boolean),
    ),
  ];
  const skipReasonsError =
    testSet || creating || skipReasons.length > 0
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
    if (idError || nameError || priorityError || timeLimitError || confidenceError || skipReasonsError) return;
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
        });
        onSaved(undefined, {
          ...testSet,
          description: description.trim(),
          priority: priorityNumber,
          claimLeaseMinutes: timeLimitNumber,
          eligibleAnnotators,
        });
      } catch (err) {
        setError(describeApiError(err, 'save the queue settings'));
      } finally {
        setSaving(false);
      }
      return;
    }
    const input: ReviewQueueDefinitionInput = {
      id: queue?.id ?? id,
      name: name.trim(),
      description: description.trim(),
      priority: priorityNumber,
      timeLimitMinutes: timeLimitNumber,
      eligibleAnnotators: eligible.map((o) => o.value as string),
      checklist,
      // Not edited here: an existing queue keeps its list; a new one gets the defaults.
      rejectReasons: queue?.rejectReasons ?? undefined,
      skipReasons: skipReasons.length > 0 ? skipReasons : undefined,
      confidenceThreshold: Number(threshold),
      enabled,
    };
    try {
      onSaved(creating ? await createReviewQueue(input) : await updateReviewQueue(input));
    } catch (err) {
      setError(describeApiError(err, creating ? 'create the queue' : 'save the queue'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      size="large"
      header={creating ? 'Create Queue' : `Queue Settings: ${testSet?.name ?? queue?.name ?? ''}`}
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
          {queue?.isDefaultQueue && (
            <Alert type="info">This is the default queue. Low-confidence documents from the pipeline arrive here.</Alert>
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
              constraintText="A whole number."
              errorText={submitted ? priorityError : undefined}
            >
              <Input type="number" inputMode="numeric" value={priority} onChange={({ detail }) => setPriority(detail.value)} />
            </FormField>
            <FormField
              label="Time limit (minutes)"
              description="When the Annotator is reminded."
              constraintText={`An item still open after this long sends one reminder; assignments don't expire. 1 to ${MAX_TIME_LIMIT_MINUTES}.`}
              errorText={submitted ? timeLimitError : undefined}
            >
              <Input type="number" inputMode="numeric" value={timeLimit} onChange={({ detail }) => setTimeLimit(detail.value)} />
            </FormField>
            {!testSet && (
              <FormField
                label="Confidence threshold"
                description="Documents with a field below this are queued."
                constraintText={
                  queue?.isDefaultQueue
                    ? 'A number from 0 to 1.'
                    : 'A number from 0 to 1. Used when documents are routed to this queue; today all routing goes to Default.'
                }
                errorText={submitted ? confidenceError : undefined}
              >
                <Input
                  type="number"
                  inputMode="decimal"
                  step={0.01}
                  value={threshold}
                  onChange={({ detail }) => setThreshold(detail.value)}
                />
              </FormField>
            )}
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
          {!testSet && (
            <FormField
              label="Skip reasons"
              description="An Annotator who gives an item back unfinished chooses one; it is kept in the item's review history. One per line."
              constraintText={creating ? 'Leave empty to use the default reasons.' : undefined}
              errorText={submitted ? skipReasonsError : undefined}
              stretch
            >
              <Textarea value={skipReasonsText} rows={4} onChange={({ detail }) => setSkipReasonsText(detail.value)} />
            </FormField>
          )}
          <FormField
            label="Eligible Annotators"
            description="The Annotators who work this queue: they see it in My Work › Queues, claim its items and can be assigned them. With none listed, only Admins work it."
            stretch
          >
            <Multiselect
              selectedOptions={eligible}
              onChange={({ detail }) => setEligible(detail.selectedOptions)}
              options={annotatorOptions}
              filteringType="auto"
              statusType={annotatorsLoading ? 'loading' : 'finished'}
              loadingText="Loading Annotators"
              placeholder="None (Admins only)"
              empty="No Annotator users. Add one in User Management."
            />
          </FormField>
        </SpaceBetween>
      </Form>
    </Modal>
  );
};

export default QueueDefinitionModal;
