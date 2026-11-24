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
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';
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

type Max = { readonly max: number };
type Name = { readonly name: string };

const messages = defineMessages<{
  goneQueue: NoMessageValues;
  reloadQueueAction: NoMessageValues;
  saveSettingsAction: NoMessageValues;
  createQueueAction: NoMessageValues;
  saveQueueAction: NoMessageValues;
  idRule: NoMessageValues;
  idTaken: NoMessageValues;
  nameRequired: NoMessageValues;
  priorityWhole: NoMessageValues;
  timeLimitRange: Max;
  tooManyReasons: Max;
  reasonRequired: NoMessageValues;
  createTitle: NoMessageValues;
  settingsTitle: Name;
  cancel: NoMessageValues;
  create: NoMessageValues;
  save: NoMessageValues;
  changedTitle: NoMessageValues;
  reload: NoMessageValues;
  changedBody: NoMessageValues;
  defaultQueueNote: NoMessageValues;
  testSetNote: Name;
  idLabel: NoMessageValues;
  idDescription: NoMessageValues;
  example: { readonly example: string };
  nameLabel: NoMessageValues;
  nameDescription: NoMessageValues;
  priorityLabel: NoMessageValues;
  priorityDescription: NoMessageValues;
  priorityConstraint: NoMessageValues;
  timeLimitLabel: NoMessageValues;
  timeLimitDescription: NoMessageValues;
  timeLimitConstraint: Max;
  enabledLabel: NoMessageValues;
  enabledDescription: NoMessageValues;
  enabledOn: NoMessageValues;
  enabledOff: NoMessageValues;
  descriptionLabel: NoMessageValues;
  skipReasonsLabel: NoMessageValues;
  skipReasonsDescription: NoMessageValues;
  skipReasonsConstraint: NoMessageValues;
  annotatorsLabel: NoMessageValues;
  annotatorsTestSetDescription: NoMessageValues;
  annotatorsProductionDescription: NoMessageValues;
  annotatorsLoading: NoMessageValues;
  annotatorsPlaceholder: NoMessageValues;
  annotatorsEmpty: NoMessageValues;
}>({
  goneQueue: {
    id: 'reviewQueues.queueSettings.goneQueue',
    defaultMessage: 'This queue no longer exists. It may have been deleted. Close this dialog.',
    description: "Error in the Queue Settings dialog's changed-queue warning when Reload queue finds the queue gone.",
  },
  reloadQueueAction: {
    id: 'reviewQueues.queueSettings.reloadQueueAction',
    defaultMessage: 'reload the queue',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when Reload queue, in the Queue Settings dialog, cannot read the queue.',
  },
  saveSettingsAction: {
    id: 'reviewQueues.queueSettings.saveSettingsAction',
    defaultMessage: 'save the queue settings',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when the settings of a Test Studio queue cannot be saved.',
  },
  createQueueAction: {
    id: 'reviewQueues.queueSettings.createQueueAction',
    defaultMessage: 'create the queue',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when a new production queue cannot be created.',
  },
  saveQueueAction: {
    id: 'reviewQueues.queueSettings.saveQueueAction',
    defaultMessage: 'save the queue',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when the settings of a production queue cannot be saved.',
  },
  idRule: {
    id: 'reviewQueues.queueSettings.idRule',
    defaultMessage: 'Lowercase letters, digits, ".", "_" and "-", starting with a letter or digit (up to 64 characters).',
    description:
      'Error under the Queue id field when the id breaks the rule. The three quoted characters are the only punctuation an id may hold: keep them as they are.',
  },
  idTaken: {
    id: 'reviewQueues.queueSettings.idTaken',
    defaultMessage: 'A queue with this id already exists.',
    description: 'Error under the Queue id field when another queue already has that id.',
  },
  nameRequired: {
    id: 'reviewQueues.queueSettings.nameRequired',
    defaultMessage: 'Enter a name.',
    description: 'Error under the Name field of the Queue Settings dialog when it is empty.',
  },
  priorityWhole: {
    id: 'reviewQueues.queueSettings.priorityWhole',
    defaultMessage: 'Enter a whole number.',
    description: 'Error under the Priority field of the Queue Settings dialog when it is not a whole number.',
  },
  timeLimitRange: {
    id: 'reviewQueues.queueSettings.timeLimitRange',
    defaultMessage: 'Enter a whole number of minutes from 1 to {max}.',
    description: 'Error under the Time limit field of the Queue Settings dialog. {max} is the longest time limit allowed, in minutes.',
  },
  tooManyReasons: {
    id: 'reviewQueues.queueSettings.tooManyReasons',
    defaultMessage: 'Keep it to {max} reasons.',
    description: 'Error under the Skip reasons field of the Queue Settings dialog. {max} is how many reasons a queue may have.',
  },
  reasonRequired: {
    id: 'reviewQueues.queueSettings.reasonRequired',
    defaultMessage: 'Enter at least one reason.',
    description: 'Error under the Skip reasons field of the Queue Settings dialog when an existing queue would have none.',
  },
  createTitle: {
    id: 'reviewQueues.queueSettings.createTitle',
    defaultMessage: 'Create Queue',
    description: 'Title of the queue dialog when it creates a production review queue.',
  },
  settingsTitle: {
    id: 'reviewQueues.queueSettings.settingsTitle',
    defaultMessage: 'Queue Settings: {name}',
    description: "Title of the Queue Settings dialog. {name} is the queue's or the test set's name, as stored.",
  },
  cancel: {
    id: 'reviewQueues.queueSettings.cancel',
    defaultMessage: 'Cancel',
    description: 'Button of the Queue Settings dialog that closes it without saving.',
  },
  create: {
    id: 'reviewQueues.queueSettings.create',
    defaultMessage: 'Create',
    description: 'Button of the queue dialog that creates the queue.',
  },
  save: {
    id: 'reviewQueues.queueSettings.save',
    defaultMessage: 'Save',
    description: "Button of the Queue Settings dialog that saves the queue's settings.",
  },
  changedTitle: {
    id: 'reviewQueues.queueSettings.changed.title',
    defaultMessage: 'This queue was changed by someone else',
    description: 'Header of the warning in the Queue Settings dialog when a save was refused because another Admin saved the queue first.',
  },
  reload: {
    id: 'reviewQueues.queueSettings.changed.reload',
    defaultMessage: 'Reload queue',
    description: "Button of the Queue Settings dialog's changed-queue warning that fills the form from the queue as stored now.",
  },
  changedBody: {
    id: 'reviewQueues.queueSettings.changed.body',
    defaultMessage:
      'Another Admin saved this queue after you opened it, so your changes were not saved. Reload the queue to see its current settings, then make your changes again. Reloading replaces what you have entered here.',
    description: "Body of the Queue Settings dialog's changed-queue warning. Admin is a user role.",
  },
  defaultQueueNote: {
    id: 'reviewQueues.queueSettings.defaultQueueNote',
    defaultMessage: 'Documents with confidence alerts arrive here when HITL is enabled in their configuration and this queue is enabled.',
    description: "Note at the top of the Default queue's Settings dialog. HITL (human in the loop) is the name of a configuration setting.",
  },
  testSetNote: {
    id: 'reviewQueues.queueSettings.testSetNote',
    defaultMessage: 'This Test Studio queue is the test set {name}; its documents come from the set.',
    description: "Note at the top of a Test Studio queue's Settings dialog. {name} is the test set's name, as stored.",
  },
  idLabel: {
    id: 'reviewQueues.queueSettings.id.label',
    defaultMessage: 'Queue id',
    description: "Label of the Queue Settings dialog's field for the queue's id.",
  },
  idDescription: {
    id: 'reviewQueues.queueSettings.id.description',
    defaultMessage: 'Used in links. It cannot be changed later.',
    description: 'Description of the Queue id field. Keep it to one short line.',
  },
  example: {
    id: 'reviewQueues.queueSettings.example',
    defaultMessage: 'e.g. {example}',
    description:
      'Placeholder of the Queue id and Name fields: an example of what to type. {example} is the example, shown as it is, so the example id stays a valid id.',
  },
  nameLabel: {
    id: 'reviewQueues.queueSettings.name.label',
    defaultMessage: 'Name',
    description: "Label of the Queue Settings dialog's field for the queue's name.",
  },
  nameDescription: {
    id: 'reviewQueues.queueSettings.name.description',
    defaultMessage: 'Shown to Admins and Annotators.',
    description: 'Description of the Name field. Admin and Annotator are user roles. Keep it to one short line.',
  },
  priorityLabel: {
    id: 'reviewQueues.queueSettings.priority.label',
    defaultMessage: 'Priority',
    description: "Label of the Queue Settings dialog's field for the queue's priority, a whole number.",
  },
  priorityDescription: {
    id: 'reviewQueues.queueSettings.priority.description',
    defaultMessage: 'Higher numbers are worked first.',
    description: 'Description of the Priority field. Keep it to one short line.',
  },
  priorityConstraint: {
    id: 'reviewQueues.queueSettings.priority.constraint',
    defaultMessage:
      'A whole number. For information: orders the queues and Next item. Kept for when there is more than one queue; with only the Default queue it changes nothing yet.',
    description: 'Text under the Priority field. Next item is the label of a button; Default is the name of a queue.',
  },
  timeLimitLabel: {
    id: 'reviewQueues.queueSettings.timeLimit.label',
    defaultMessage: 'Time limit (minutes)',
    description: "Label of the Queue Settings dialog's field for how long an assigned item may wait before its Annotator is reminded.",
  },
  timeLimitDescription: {
    id: 'reviewQueues.queueSettings.timeLimit.description',
    defaultMessage: "When an assigned item's Annotator is reminded.",
    description: 'Description of the Time limit field. Annotator is a user role. Keep it to one short line.',
  },
  timeLimitConstraint: {
    id: 'reviewQueues.queueSettings.timeLimit.constraint',
    defaultMessage: "For information: one reminder is sent, and the item stays assigned (assignments don't expire). 1 to {max}.",
    description: 'Text under the Time limit field. {max} is the longest time limit allowed, in minutes.',
  },
  enabledLabel: {
    id: 'reviewQueues.queueSettings.enabled.label',
    defaultMessage: 'Enabled',
    description: "Label of the Queue Settings dialog's switch that turns a production queue on or off.",
  },
  enabledDescription: {
    id: 'reviewQueues.queueSettings.enabled.description',
    defaultMessage: 'When off, no documents are added to this queue.',
    description: 'Description of the Enabled switch. Keep it to one short line.',
  },
  enabledOn: {
    id: 'reviewQueues.queueSettings.enabled.on',
    defaultMessage: 'Enabled',
    description: 'Text beside the Enabled switch of the Queue Settings dialog when the switch is on.',
  },
  enabledOff: {
    id: 'reviewQueues.queueSettings.enabled.off',
    defaultMessage: 'Disabled',
    description: 'Text beside the Enabled switch of the Queue Settings dialog when the switch is off.',
  },
  descriptionLabel: {
    id: 'reviewQueues.queueSettings.description.label',
    defaultMessage: 'Description',
    description: "Label of the Queue Settings dialog's field for the queue's description.",
  },
  skipReasonsLabel: {
    id: 'reviewQueues.queueSettings.skipReasons.label',
    defaultMessage: 'Skip reasons',
    description: "Label of the Queue Settings dialog's field for the reasons an Annotator can give when handing an item back.",
  },
  skipReasonsDescription: {
    id: 'reviewQueues.queueSettings.skipReasons.description',
    defaultMessage: "An Annotator who gives an item back unfinished chooses one; it is kept in the item's review history. One per line.",
    description: 'Description of the Skip reasons field. Annotator is a user role.',
  },
  skipReasonsConstraint: {
    id: 'reviewQueues.queueSettings.skipReasons.constraint',
    defaultMessage: 'Leave empty to use the default reasons.',
    description: 'Text under the Skip reasons field when a queue is being created.',
  },
  annotatorsLabel: {
    id: 'reviewQueues.queueSettings.annotators.label',
    defaultMessage: 'Eligible Annotators',
    description: "Label of the Queue Settings dialog's field for the Annotators who work the queue. Annotator is a user role.",
  },
  annotatorsTestSetDescription: {
    id: 'reviewQueues.queueSettings.annotators.testSetDescription',
    defaultMessage:
      'The Annotators who work this queue: they see it in Home › Queues, claim its items and can be assigned them. Reviewers work every test set their configuration-profile scope admits without being listed, and claim their own items.',
    description:
      'Description of the Eligible Annotators field for a Test Studio queue. Annotator and Reviewer are user roles; Home › Queues is the navigation path to a page.',
  },
  annotatorsProductionDescription: {
    id: 'reviewQueues.queueSettings.annotators.productionDescription',
    defaultMessage:
      'The Annotators who work this queue: they see it in Home › Queues, claim its items and can be assigned them. Reviewers work every Production queue without being listed, and claim their own items.',
    description:
      'Description of the Eligible Annotators field for a production queue. Annotator and Reviewer are user roles; Home › Queues is the navigation path to a page.',
  },
  annotatorsLoading: {
    id: 'reviewQueues.queueSettings.annotators.loading',
    defaultMessage: 'Loading Annotators',
    description: 'Shown in the Eligible Annotators list while the Annotator users load.',
  },
  annotatorsPlaceholder: {
    id: 'reviewQueues.queueSettings.annotators.placeholder',
    defaultMessage: 'None (Admins and Reviewers only)',
    description: 'Placeholder of the Eligible Annotators list when no Annotator is chosen. Admin and Reviewer are user roles.',
  },
  annotatorsEmpty: {
    id: 'reviewQueues.queueSettings.annotators.empty',
    defaultMessage: 'No Annotator users. Add one in User Management.',
    description: 'Shown in the Eligible Annotators list when there is no Annotator user. User Management is the name of a page.',
  },
});

/** A refused call: its action phrase and what it threw, worded at render. */
type ApiFailure = { readonly action: TypedMessageDescriptor<NoMessageValues>; readonly cause: unknown };

/** Why Reload queue failed: a message worded at render, or a refused call. */
type ReloadError = { readonly message: TypedMessageDescriptor<NoMessageValues> } | ApiFailure;

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
  const intl = useIntl();
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
  const [error, setError] = useState<ApiFailure | null>(null);
  // The production queue the form was filled from: the prop, or a reload after a
  // refused save. Its version is what a save is checked against.
  const [base, setBase] = useState<ReviewQueueDefinition | null>(queue);
  // The last save was refused because someone else changed the queue.
  const [changed, setChanged] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [reloadError, setReloadError] = useState<ReloadError | null>(null);

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
      else setReloadError({ message: messages.goneQueue });
    } catch (err) {
      setReloadError({ action: messages.reloadQueueAction, cause: err });
    } finally {
      setReloading(false);
    }
  };

  const idError = !creating
    ? undefined
    : !QUEUE_ID_PATTERN.test(id)
      ? intl.formatMessage(messages.idRule)
      : existingIds.includes(id)
        ? intl.formatMessage(messages.idTaken)
        : undefined;
  const nameError = name.trim() ? undefined : intl.formatMessage(messages.nameRequired);
  const priorityNumber = Number(priority);
  const priorityError = Number.isInteger(priorityNumber) ? undefined : intl.formatMessage(messages.priorityWhole);
  const timeLimitNumber = Number(timeLimit);
  const timeLimitError =
    Number.isInteger(timeLimitNumber) && timeLimitNumber >= 1 && timeLimitNumber <= MAX_TIME_LIMIT_MINUTES
      ? undefined
      : intl.formatMessage(messages.timeLimitRange, { max: MAX_TIME_LIMIT_MINUTES });
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
        ? intl.formatMessage(messages.tooManyReasons, { max: MAX_SKIP_REASONS })
        : undefined
      : intl.formatMessage(messages.reasonRequired);

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
        setError({ action: messages.saveSettingsAction, cause: err });
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
      else setError({ action: creating ? messages.createQueueAction : messages.saveQueueAction, cause: err });
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      size="large"
      header={
        creating
          ? intl.formatMessage(messages.createTitle)
          : intl.formatMessage(messages.settingsTitle, { name: testSet?.name ?? base?.name ?? '' })
      }
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              <FormattedMessage {...messages.cancel} />
            </Button>
            <Button variant="primary" loading={saving} onClick={() => void save()}>
              <FormattedMessage {...(creating ? messages.create : messages.save)} />
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <Form errorText={error ? describeApiError(error.cause, error.action, intl) : undefined}>
        <SpaceBetween size="m">
          {changed && (
            <Alert
              type="warning"
              header={intl.formatMessage(messages.changedTitle)}
              action={
                <Button loading={reloading} onClick={() => void reload()}>
                  <FormattedMessage {...messages.reload} />
                </Button>
              }
            >
              <FormattedMessage {...messages.changedBody} />
              {reloadError && (
                <Box color="text-status-error">
                  {'action' in reloadError ? (
                    describeApiError(reloadError.cause, reloadError.action, intl)
                  ) : (
                    <FormattedMessage {...reloadError.message} />
                  )}
                </Box>
              )}
            </Alert>
          )}
          {base?.isDefaultQueue && (
            <Alert type="info">
              <FormattedMessage {...messages.defaultQueueNote} />
            </Alert>
          )}
          {testSet && (
            <Alert type="info">
              <FormattedMessage {...messages.testSetNote} values={{ name: testSet.name }} />
            </Alert>
          )}
          {/* Each pair in the two columns has a one-line description on both fields, so the
              inputs stay level; longer detail goes under the input as constraint text. */}
          <ColumnLayout columns={2}>
            {!testSet && (
              <FormField
                label={intl.formatMessage(messages.idLabel)}
                description={intl.formatMessage(messages.idDescription)}
                errorText={submitted ? idError : undefined}
              >
                <Input
                  value={id}
                  disabled={!creating}
                  placeholder={intl.formatMessage(messages.example, { example: 'claims-high-value' })}
                  onChange={({ detail }) => setId(detail.value)}
                />
              </FormField>
            )}
            {!testSet && (
              <FormField
                label={intl.formatMessage(messages.nameLabel)}
                description={intl.formatMessage(messages.nameDescription)}
                errorText={submitted ? nameError : undefined}
              >
                <Input
                  value={name}
                  placeholder={intl.formatMessage(messages.example, { example: 'High-value claims' })}
                  onChange={({ detail }) => setName(detail.value)}
                />
              </FormField>
            )}
            <FormField
              label={intl.formatMessage(messages.priorityLabel)}
              description={intl.formatMessage(messages.priorityDescription)}
              constraintText={intl.formatMessage(messages.priorityConstraint)}
              errorText={submitted ? priorityError : undefined}
            >
              <Input type="number" inputMode="numeric" value={priority} onChange={({ detail }) => setPriority(detail.value)} />
            </FormField>
            <FormField
              label={intl.formatMessage(messages.timeLimitLabel)}
              description={intl.formatMessage(messages.timeLimitDescription)}
              constraintText={intl.formatMessage(messages.timeLimitConstraint, { max: MAX_TIME_LIMIT_MINUTES })}
              errorText={submitted ? timeLimitError : undefined}
            >
              <Input type="number" inputMode="numeric" value={timeLimit} onChange={({ detail }) => setTimeLimit(detail.value)} />
            </FormField>
            {!testSet && (
              <FormField label={intl.formatMessage(messages.enabledLabel)} description={intl.formatMessage(messages.enabledDescription)}>
                <Toggle checked={enabled} onChange={({ detail }) => setEnabled(detail.checked)}>
                  <FormattedMessage {...(enabled ? messages.enabledOn : messages.enabledOff)} />
                </Toggle>
              </FormField>
            )}
          </ColumnLayout>
          <FormField label={intl.formatMessage(messages.descriptionLabel)} stretch>
            <Textarea value={description} rows={2} onChange={({ detail }) => setDescription(detail.value)} />
          </FormField>
          <FormField
            label={intl.formatMessage(messages.skipReasonsLabel)}
            description={intl.formatMessage(messages.skipReasonsDescription)}
            constraintText={creating ? intl.formatMessage(messages.skipReasonsConstraint) : undefined}
            errorText={submitted ? skipReasonsError : undefined}
            stretch
          >
            <Textarea value={skipReasonsText} rows={4} onChange={({ detail }) => setSkipReasonsText(detail.value)} />
          </FormField>
          <FormField
            label={intl.formatMessage(messages.annotatorsLabel)}
            description={intl.formatMessage(testSet ? messages.annotatorsTestSetDescription : messages.annotatorsProductionDescription)}
            stretch
          >
            <Multiselect
              selectedOptions={eligible}
              onChange={({ detail }) => setEligible(detail.selectedOptions)}
              options={annotatorOptions}
              filteringType="auto"
              statusType={annotatorsLoading ? 'loading' : 'finished'}
              loadingText={intl.formatMessage(messages.annotatorsLoading)}
              placeholder={intl.formatMessage(messages.annotatorsPlaceholder)}
              empty={intl.formatMessage(messages.annotatorsEmpty)}
            />
          </FormField>
        </SpaceBetween>
      </Form>
    </Modal>
  );
};

export default QueueDefinitionModal;
