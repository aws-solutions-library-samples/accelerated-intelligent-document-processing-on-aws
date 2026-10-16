// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// HITL › Queue Management: create or edit one system-wide production queue — its
// name, priority, reminder time limit, eligible Annotators and reject reasons — or
// edit a Test Studio queue's settings (the same, less the id, name and reject
// reasons, which a test set doesn't have). A queue's stored checklist is kept as it
// is; the dialog doesn't edit it.
import React, { useEffect, useState } from 'react';
import type { MultiselectProps } from '@cloudscape-design/components';
import {
  Alert,
  AttributeEditor,
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
} from '@cloudscape-design/components';

import type { ReviewQueueDefinition, ReviewQueueDefinitionInput } from '../../graphql/generated/operation-types';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { createReviewQueue, updateReviewQueue, updateTestSetQueue } from './reviewer-assignment';

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

interface ListEntry {
  value: string;
}

const toEntries = (values: readonly string[] | undefined): ListEntry[] => (values ?? []).map((value) => ({ value }));
const fromEntries = (entries: readonly ListEntry[]): string[] => entries.map((e) => e.value.trim()).filter(Boolean);

const ListEditor = ({
  label,
  description,
  addLabel,
  entries,
  onChange,
  placeholder,
}: {
  label: string;
  description: string;
  addLabel: string;
  entries: ListEntry[];
  onChange: (entries: ListEntry[]) => void;
  placeholder: string;
}): React.JSX.Element => (
  <FormField label={label} description={description} stretch>
    <AttributeEditor<ListEntry>
      items={entries}
      addButtonText={addLabel}
      removeButtonText="Remove"
      empty="None."
      onAddButtonClick={() => onChange([...entries, { value: '' }])}
      onRemoveButtonClick={({ detail }) => onChange(entries.filter((_, i) => i !== detail.itemIndex))}
      definition={[
        {
          control: (entry, index) => (
            <Input
              value={entry.value}
              placeholder={placeholder}
              ariaLabel={`${label} ${index + 1}`}
              onChange={({ detail }) => onChange(entries.map((e, i) => (i === index ? { value: detail.value } : e)))}
            />
          ),
        },
      ]}
    />
  </FormField>
);

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
  const [eligible, setEligible] = useState<readonly MultiselectProps.Option[]>([]);
  // Not edited here; saved back unchanged.
  const [checklist, setChecklist] = useState<string[]>([]);
  const [rejectReasons, setRejectReasons] = useState<ListEntry[]>([]);
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
    setEligible((testSet?.eligibleAnnotators ?? queue?.eligibleAnnotators ?? []).map((e) => ({ value: e, label: e })));
    setChecklist([...(testSet?.checklist ?? queue?.checklist ?? [])]);
    setRejectReasons(toEntries(queue?.rejectReasons));
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

  const annotatorOptions: MultiselectProps.Option[] = [
    ...annotators.map((e) => ({ value: e, label: e })),
    // Keep a saved email that is no longer an Annotator visible, so it can be removed.
    ...eligible.filter((o) => !annotators.includes(o.value ?? '')),
  ];

  const save = async () => {
    setSubmitted(true);
    if (idError || nameError || priorityError || timeLimitError) return;
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
      rejectReasons: fromEntries(rejectReasons),
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
      header={creating ? 'Create queue' : `Queue Settings: ${testSet?.name ?? queue?.name ?? ''}`}
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
          {queue?.isDefaultQueue && <Alert type="info">This is the default queue: items with no queue of their own go here.</Alert>}
          {queue?.isEscalationQueue && <Alert type="info">This is the escalation queue: items moved for escalation go here.</Alert>}
          {testSet && <Alert type="info">This Test Studio queue is the test set {testSet.name}; its documents come from the set.</Alert>}
          <ColumnLayout columns={2}>
            {!testSet && (
              <FormField
                label="Queue id"
                description={creating ? 'Used in the configuration (hitl.review_queues) and links. It cannot be changed later.' : undefined}
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
              <FormField label="Name" errorText={submitted ? nameError : undefined}>
                <Input value={name} placeholder="e.g. High-value claims" onChange={({ detail }) => setName(detail.value)} />
              </FormField>
            )}
            {/* One-line descriptions keep the two inputs level; the detail is under each input. */}
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
          </ColumnLayout>
          <FormField label="Description" stretch>
            <Textarea value={description} rows={2} onChange={({ detail }) => setDescription(detail.value)} />
          </FormField>
          <FormField
            label="Eligible Annotators"
            description="Who this queue's items may be assigned to. Leave empty to allow every Annotator."
            stretch
          >
            <Multiselect
              selectedOptions={eligible}
              onChange={({ detail }) => setEligible(detail.selectedOptions)}
              options={annotatorOptions}
              filteringType="auto"
              statusType={annotatorsLoading ? 'loading' : 'finished'}
              loadingText="Loading Annotators"
              placeholder="Any Annotator"
              empty="No Annotator users. Add one in User Management."
            />
          </FormField>
          {!testSet && (
            <ListEditor
              label="Reject reasons"
              description="The reasons an Annotator can choose when rejecting an item."
              addLabel="Add reject reason"
              entries={rejectReasons}
              onChange={setRejectReasons}
              placeholder="e.g. Illegible document"
            />
          )}
        </SpaceBetween>
      </Form>
    </Modal>
  );
};

export default QueueDefinitionModal;
