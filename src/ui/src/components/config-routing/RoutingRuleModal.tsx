// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The dialog for one routing rule, in three groups: the routing rule (its id and label,
// whether it is enabled), its conditions, and its outcome with the reason the reviewer
// reads. The dialog edits a copy; the table changes when the dialog is saved, and the
// profile changes when the configuration page is saved. The server checks the routing
// rule then, and what it refuses is shown here (`serverError`) with the edits kept.
// Under the reason, the dialog shows the reason of the document last tested on the tab,
// when that test matched this routing rule.
//
// The dialog is shown to every role. For anyone but an Admin it is read-only: the same
// groups, nothing to change, and Close in place of Cancel and Save.
import React, { useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  ColumnLayout,
  Container,
  Form,
  FormField,
  Header,
  Input,
  Modal,
  RadioGroup,
  Select,
  SpaceBetween,
  Toggle,
  type SelectProps,
} from '@cloudscape-design/components';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';

import type { RoutingTestResult } from '../../graphql/generated/operation-types';
import ReasonField, { byteLength } from './ReasonField';
import RoutingConditionsEditor, { conditionProblems } from './RoutingConditionsEditor';
import { TestedReason } from './RoutingTestPanel';
import {
  DEFAULT_QUEUE_ID,
  MAX_CONDITIONS,
  MAX_REASON_BYTES,
  RULE_ID_PATTERN,
  queueOf,
  withQueue,
  type OutcomeKind,
  type RoutingRule,
} from './routing-types';

const messages = defineMessages<{
  createHeader: NoMessageValues;
  editHeader: { readonly id: string };
  viewHeader: { readonly id: string };
  cancel: NoMessageValues;
  save: NoMessageValues;
  close: NoMessageValues;
  serverErrorHeader: NoMessageValues;
  ruleGroup: NoMessageValues;
  idLabel: NoMessageValues;
  idDescription: NoMessageValues;
  idConstraint: NoMessageValues;
  idRequired: NoMessageValues;
  idInvalid: NoMessageValues;
  idTaken: NoMessageValues;
  labelLabel: NoMessageValues;
  labelDescription: NoMessageValues;
  enabledLabel: NoMessageValues;
  enabledDescription: NoMessageValues;
  enabledOn: NoMessageValues;
  enabledOff: NoMessageValues;
  conditionsGroup: NoMessageValues;
  conditionsCounter: { readonly count: number; readonly max: number };
  conditionsDescription: NoMessageValues;
  outcomeGroup: NoMessageValues;
  outcomeLabel: NoMessageValues;
  outcomeQueue: NoMessageValues;
  outcomeQueueDescription: NoMessageValues;
  outcomeNoReview: NoMessageValues;
  outcomeNoReviewDescription: NoMessageValues;
  queueLabel: NoMessageValues;
  queuePlaceholder: NoMessageValues;
  queueRequired: NoMessageValues;
  queueEmpty: NoMessageValues;
  previewLabel: NoMessageValues;
  previewNote: NoMessageValues;
}>({
  createHeader: {
    id: 'routing.ruleModal.createHeader',
    defaultMessage: 'Add routing rule',
    description: 'Title of the dialog that adds a routing rule.',
  },
  editHeader: {
    id: 'routing.ruleModal.editHeader',
    defaultMessage: 'Edit routing rule {id}',
    description: 'Title of the dialog that edits a routing rule. {id} is the id of the routing rule.',
  },
  viewHeader: {
    id: 'routing.ruleModal.viewHeader',
    defaultMessage: 'Routing rule {id}',
    description: 'Title of the dialog that shows a routing rule to a user who cannot change it. {id} is the id of the routing rule.',
  },
  cancel: {
    id: 'routing.ruleModal.cancel',
    defaultMessage: 'Cancel',
    description: 'Button that closes the routing rule dialog without keeping the edits.',
  },
  save: {
    id: 'routing.ruleModal.save',
    defaultMessage: 'Save routing rule',
    description: 'Button that keeps the edits of the routing rule dialog in the table.',
  },
  close: {
    id: 'routing.ruleModal.close',
    defaultMessage: 'Close',
    description: 'Button that closes the routing rule dialog for a user who cannot change the routing rule.',
  },
  serverErrorHeader: {
    id: 'routing.ruleModal.serverErrorHeader',
    defaultMessage: 'The configuration was not saved',
    description:
      'Heading of the error shown in the routing rule dialog when the server refused the configuration because of this routing rule.',
  },
  ruleGroup: {
    id: 'routing.ruleModal.ruleGroup',
    defaultMessage: 'Routing rule',
    description: 'Heading of the first group of the routing rule dialog: the id, the label and whether the routing rule is enabled.',
  },
  idLabel: {
    id: 'routing.ruleModal.idLabel',
    defaultMessage: 'Routing rule id',
    description: 'Label of the field that holds the id of a routing rule.',
  },
  idDescription: {
    id: 'routing.ruleModal.idDescription',
    defaultMessage: 'Recorded on each document it places.',
    description: 'One-line help under the label of the id field of a routing rule. Kept to one line, so the id and the label stay level.',
  },
  idConstraint: {
    id: 'routing.ruleModal.idConstraint',
    defaultMessage: 'Lower-case letters, digits, hyphens, dots and underscores. It cannot be changed later.',
    description: 'What an id of a routing rule may hold, and that it is fixed once saved, shown under the id field.',
  },
  idRequired: { id: 'routing.ruleModal.idRequired', defaultMessage: 'Enter an id.', description: 'Error under an empty routing rule id.' },
  idInvalid: {
    id: 'routing.ruleModal.idInvalid',
    defaultMessage: 'Use 1 to 64 lower-case letters, digits, hyphens, dots or underscores, starting with a letter or a digit.',
    description: 'Error under a routing rule id that is not in the allowed form.',
  },
  idTaken: {
    id: 'routing.ruleModal.idTaken',
    defaultMessage: 'Another routing rule has this id.',
    description: 'Error under a routing rule id that another routing rule of the table uses.',
  },
  labelLabel: {
    id: 'routing.ruleModal.labelLabel',
    defaultMessage: 'Label',
    description: 'Label of the field that holds the name of a routing rule shown to people.',
  },
  labelDescription: {
    id: 'routing.ruleModal.labelDescription',
    defaultMessage: 'The name reviewers see on a document it placed in a queue.',
    description: 'One-line help under the label of the label field of a routing rule.',
  },
  enabledLabel: {
    id: 'routing.ruleModal.enabledLabel',
    defaultMessage: 'Enabled',
    description: 'Label of the switch that turns a routing rule on or off.',
  },
  enabledDescription: {
    id: 'routing.ruleModal.enabledDescription',
    defaultMessage: 'A disabled routing rule is kept in the table and never evaluated.',
    description: 'Help under the label of the switch that turns a routing rule on or off.',
  },
  enabledOn: {
    id: 'routing.ruleModal.enabledOn',
    defaultMessage: 'Enabled',
    description: 'Text beside the switch of a routing rule that is on.',
  },
  enabledOff: {
    id: 'routing.ruleModal.enabledOff',
    defaultMessage: 'Disabled',
    description: 'Text beside the switch of a routing rule that is off.',
  },
  conditionsGroup: {
    id: 'routing.ruleModal.conditionsGroup',
    defaultMessage: 'Conditions',
    description: 'Heading of the second group of the routing rule dialog: the conditions of the routing rule.',
  },
  conditionsCounter: {
    id: 'routing.ruleModal.conditionsCounter',
    defaultMessage: '({count, number}/{max, number})',
    description:
      'Counter beside the Conditions heading of the routing rule dialog. {count} is the number of conditions and {max} the most a routing rule can have.',
  },
  conditionsDescription: {
    id: 'routing.ruleModal.conditionsDescription',
    defaultMessage: 'The routing rule matches a document when every condition holds.',
    description: 'Help under the Conditions heading of the routing rule dialog.',
  },
  outcomeGroup: {
    id: 'routing.ruleModal.outcomeGroup',
    defaultMessage: 'Outcome and reason',
    description:
      'Heading of the third group of the routing rule dialog: what happens to a matching document, and the reason the reviewer reads.',
  },
  outcomeLabel: {
    id: 'routing.ruleModal.outcomeLabel',
    defaultMessage: 'When the routing rule matches',
    description: 'Label of the choice of what a routing rule does with a document it matches.',
  },
  outcomeQueue: {
    id: 'routing.ruleModal.outcomeQueue',
    defaultMessage: 'Send to a review queue',
    description: 'Outcome of a routing rule: the document is placed in a review queue.',
  },
  outcomeQueueDescription: {
    id: 'routing.ruleModal.outcomeQueueDescription',
    defaultMessage: 'The document waits in the queue until a review is completed.',
    description: 'Help under the outcome that places a document in a review queue.',
  },
  outcomeNoReview: {
    id: 'routing.ruleModal.outcomeNoReview',
    defaultMessage: 'No review',
    description: 'Outcome of a routing rule: the document is not reviewed.',
  },
  outcomeNoReviewDescription: {
    id: 'routing.ruleModal.outcomeNoReviewDescription',
    defaultMessage: 'Processing continues without a review.',
    description: 'Help under the outcome that leaves a document unreviewed.',
  },
  queueLabel: {
    id: 'routing.ruleModal.queueLabel',
    defaultMessage: 'Review queue',
    description: 'Label of the list of review queues a routing rule can send documents to.',
  },
  queuePlaceholder: {
    id: 'routing.ruleModal.queuePlaceholder',
    defaultMessage: 'Choose a queue',
    description: 'Placeholder of the list of review queues of a routing rule.',
  },
  queueRequired: {
    id: 'routing.ruleModal.queueRequired',
    defaultMessage: 'Choose the queue the routing rule sends documents to.',
    description: 'Error under the queue list of a routing rule whose outcome is a queue and none is chosen.',
  },
  queueEmpty: {
    id: 'routing.ruleModal.queueEmpty',
    defaultMessage: 'No review queues. Create one in Review Management.',
    description: 'Shown in the list of review queues of a routing rule when there are none.',
  },
  previewLabel: {
    id: 'routing.ruleModal.previewLabel',
    defaultMessage: 'Reason for the document last tested',
    description:
      'Label, under the reason field of the routing rule dialog, of the reason a reviewer would read on the document last tested on the Routing Rules tab, shown when that test matched this routing rule.',
  },
  previewNote: {
    id: 'routing.ruleModal.previewNote',
    defaultMessage: 'From the last test on this tab, with the routing rule as it was then. Test again to see a change.',
    description: 'Note under the reason of the document last tested, in the routing rule dialog.',
  },
});

/** A review queue a routing rule can send documents to. */
export interface QueueChoice {
  id: string;
  name: string;
}

const NEW_RULE: RoutingRule = {
  id: '',
  label: '',
  enabled: true,
  conditions: [],
  actions: withQueue(DEFAULT_QUEUE_ID),
  reason: '',
};

/** The one choice the dialog offers for an outcome. The stored outcome is a list of actions. */
interface OutcomeChoice {
  kind: OutcomeKind;
  queue: string;
}

const choiceOf = (rule: RoutingRule): OutcomeChoice => {
  const queue = queueOf(rule.actions);
  return { kind: queue ? 'queue' : 'no_review', queue };
};

interface RoutingRuleModalProps {
  visible: boolean;
  /** The routing rule being edited; null adds a new one. */
  rule: RoutingRule | null;
  /** The ids of the other routing rules of the table. */
  otherIds: readonly string[];
  queues: readonly QueueChoice[];
  classes: unknown;
  policyClasses: unknown;
  /** What the server refused about this routing rule on the last save of the configuration. */
  serverError?: string | null;
  /** Show the routing rule and offer no change (anyone but an Admin). */
  readOnly?: boolean;
  /**
   * What the tab's test panel last answered. When that test matched this routing rule,
   * the dialog shows the reason the tested document would be placed with.
   */
  lastTest?: RoutingTestResult | null;
  onDismiss: () => void;
  onSave: (rule: RoutingRule) => void;
}

const RoutingRuleModal = ({
  visible,
  rule,
  otherIds,
  queues,
  classes,
  policyClasses,
  serverError = null,
  readOnly = false,
  lastTest = null,
  onDismiss,
  onSave,
}: RoutingRuleModalProps): React.JSX.Element => {
  const intl = useIntl();
  const [draft, setDraft] = useState<RoutingRule>(rule ?? NEW_RULE);
  const [choice, setChoice] = useState<OutcomeChoice>(choiceOf(rule ?? NEW_RULE));
  const [submitted, setSubmitted] = useState(false);
  // Counts the times the dialog opened: the conditions editor starts afresh each time.
  const [opened, setOpened] = useState(0);

  // A fresh copy each time the dialog opens, for the routing rule it opens on.
  useEffect(() => {
    if (visible) {
      setDraft(rule ?? NEW_RULE);
      setChoice(choiceOf(rule ?? NEW_RULE));
      setSubmitted(false);
      setOpened((count) => count + 1);
    }
  }, [visible, rule]);

  const isNew = rule === null;
  const id = draft.id.trim();
  let idError: string | undefined;
  if (!id) idError = intl.formatMessage(messages.idRequired);
  else if (!RULE_ID_PATTERN.test(id)) idError = intl.formatMessage(messages.idInvalid);
  else if (otherIds.includes(id)) idError = intl.formatMessage(messages.idTaken);

  const toQueue = choice.kind === 'queue';
  const queueError = toQueue && !choice.queue ? intl.formatMessage(messages.queueRequired) : undefined;
  const conditionsValid = draft.conditions.every((condition) => Object.keys(conditionProblems(condition)).length === 0);
  const reasonValid = !toQueue || byteLength(draft.reason) <= MAX_REASON_BYTES;
  const valid = !idError && !queueError && conditionsValid && reasonValid;

  // The last test, when it matched this routing rule and its answer holds a reason.
  const tested =
    rule !== null && lastTest !== null && lastTest.matchedRuleId === rule.id && (lastTest.reason ?? '').trim() ? lastTest : null;

  const queueOptions: SelectProps.Option[] = [
    // A queue the routing rule names that the list does not have stays choosable, so
    // opening the dialog does not silently change the routing rule.
    ...(choice.queue && !queues.some((queue) => queue.id === choice.queue) ? [{ value: choice.queue, label: choice.queue }] : []),
    ...queues.map((queue) => ({ value: queue.id, label: queue.name || queue.id, description: queue.name ? queue.id : undefined })),
  ];

  const submit = (): void => {
    setSubmitted(true);
    if (!valid) return;
    const queue = toQueue ? choice.queue : '';
    onSave({
      ...draft,
      id,
      label: draft.label.trim(),
      conditions: draft.conditions.map((condition) => ({ ...condition, path: condition.path.trim(), test: condition.test.trim() })),
      // The actions are kept as they were read unless the choice changed, so opening and
      // saving a routing rule changes nothing the dialog does not show.
      actions: queue === queueOf(draft.actions) ? draft.actions : withQueue(queue),
      // A reason is for a reviewer: an outcome with no queue has none.
      reason: toQueue ? draft.reason : '',
    });
  };

  let header = intl.formatMessage(messages.createHeader);
  if (rule) header = intl.formatMessage(readOnly ? messages.viewHeader : messages.editHeader, { id: rule.id });

  return (
    <Modal
      visible={visible}
      size="large"
      onDismiss={onDismiss}
      header={header}
      footer={
        <Box float="right">
          {readOnly ? (
            <Button variant="primary" onClick={onDismiss}>
              {intl.formatMessage(messages.close)}
            </Button>
          ) : (
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={onDismiss}>
                {intl.formatMessage(messages.cancel)}
              </Button>
              <Button variant="primary" onClick={submit}>
                {intl.formatMessage(messages.save)}
              </Button>
            </SpaceBetween>
          )}
        </Box>
      }
    >
      <Form>
        <SpaceBetween size="l">
          {serverError && (
            <Alert type="error" header={intl.formatMessage(messages.serverErrorHeader)}>
              {serverError}
            </Alert>
          )}

          <Container header={<Header variant="h3">{intl.formatMessage(messages.ruleGroup)}</Header>}>
            <SpaceBetween size="m">
              {/* Both fields have a one-line description, so the two inputs stay level; the
                  longer detail goes under the input as constraint text. */}
              <ColumnLayout columns={2}>
                <FormField
                  label={intl.formatMessage(messages.idLabel)}
                  description={intl.formatMessage(messages.idDescription)}
                  constraintText={intl.formatMessage(messages.idConstraint)}
                  errorText={submitted ? idError : undefined}
                >
                  <Input
                    value={draft.id}
                    disabled={!isNew && !readOnly}
                    readOnly={readOnly}
                    onChange={({ detail }) => setDraft((current) => ({ ...current, id: detail.value }))}
                  />
                </FormField>
                <FormField label={intl.formatMessage(messages.labelLabel)} description={intl.formatMessage(messages.labelDescription)}>
                  <Input
                    value={draft.label}
                    readOnly={readOnly}
                    onChange={({ detail }) => setDraft((current) => ({ ...current, label: detail.value }))}
                  />
                </FormField>
              </ColumnLayout>
              <FormField label={intl.formatMessage(messages.enabledLabel)} description={intl.formatMessage(messages.enabledDescription)}>
                <Toggle
                  checked={draft.enabled}
                  readOnly={readOnly}
                  onChange={({ detail }) => setDraft((current) => ({ ...current, enabled: detail.checked }))}
                >
                  {intl.formatMessage(draft.enabled ? messages.enabledOn : messages.enabledOff)}
                </Toggle>
              </FormField>
            </SpaceBetween>
          </Container>

          <Container
            header={
              <Header
                variant="h3"
                counter={intl.formatMessage(messages.conditionsCounter, { count: draft.conditions.length, max: MAX_CONDITIONS })}
                description={intl.formatMessage(messages.conditionsDescription)}
              >
                {intl.formatMessage(messages.conditionsGroup)}
              </Header>
            }
          >
            <RoutingConditionsEditor
              key={opened}
              conditions={draft.conditions}
              onChange={(conditions) => setDraft((current) => ({ ...current, conditions }))}
              classes={classes}
              policyClasses={policyClasses}
              showProblems={submitted}
              readOnly={readOnly}
            />
          </Container>

          <Container header={<Header variant="h3">{intl.formatMessage(messages.outcomeGroup)}</Header>}>
            <SpaceBetween size="m">
              <FormField label={intl.formatMessage(messages.outcomeLabel)}>
                <RadioGroup
                  value={choice.kind}
                  readOnly={readOnly}
                  onChange={({ detail }) => setChoice((current) => ({ ...current, kind: detail.value as OutcomeKind }))}
                  items={[
                    {
                      value: 'queue',
                      label: intl.formatMessage(messages.outcomeQueue),
                      description: intl.formatMessage(messages.outcomeQueueDescription),
                    },
                    {
                      value: 'no_review',
                      label: intl.formatMessage(messages.outcomeNoReview),
                      description: intl.formatMessage(messages.outcomeNoReviewDescription),
                    },
                  ]}
                />
              </FormField>
              {toQueue && (
                <FormField label={intl.formatMessage(messages.queueLabel)} errorText={submitted ? queueError : undefined}>
                  <Select
                    selectedOption={queueOptions.find((option) => option.value === choice.queue) ?? null}
                    options={queueOptions}
                    readOnly={readOnly}
                    placeholder={intl.formatMessage(messages.queuePlaceholder)}
                    empty={intl.formatMessage(messages.queueEmpty)}
                    onChange={({ detail }) => setChoice({ kind: 'queue', queue: detail.selectedOption.value ?? '' })}
                  />
                </FormField>
              )}
              {toQueue && (
                <ReasonField
                  value={draft.reason}
                  onChange={(reason) => setDraft((current) => ({ ...current, reason }))}
                  readOnly={readOnly}
                  conditions={draft.conditions}
                  maxBytes={MAX_REASON_BYTES}
                />
              )}
              {toQueue && tested && (
                <div>
                  <Box variant="awsui-key-label">{intl.formatMessage(messages.previewLabel)}</Box>
                  <TestedReason result={tested} />
                  <Box variant="small" color="text-body-secondary">
                    {intl.formatMessage(messages.previewNote)}
                  </Box>
                </div>
              )}
            </SpaceBetween>
          </Container>
        </SpaceBetween>
      </Form>
    </Modal>
  );
};

export default RoutingRuleModal;
