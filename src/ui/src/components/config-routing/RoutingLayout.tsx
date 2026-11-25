// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The Routing Rules tab of a configuration profile: the profile's one routing table, in
// the order it is evaluated. After policy rule validation the first enabled routing rule
// whose conditions all hold decides whether a document is reviewed, and in which queue.
//
// The tab edits the profile's `routing` section in the configuration form and nothing
// else: saving, revision history and the unsaved-changes guard are the configuration
// page's. Only an Admin can change the table (the server refuses anyone else), so for
// other roles the tab shows the routing rules, opens each one read-only and offers no
// action.
//
// Routing rules are evaluated only while Review Routing is on for the profile and the
// Default review queue is on; the tab warns when either is off.
import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  ButtonDropdown,
  Container,
  FormField,
  Header,
  Icon,
  Input,
  Link,
  Modal,
  RadioGroup,
  Select,
  SpaceBetween,
  StatusIndicator,
  Table,
  Toggle,
  type SelectProps,
  type TableProps,
} from '@cloudscape-design/components';
import { DndContext, KeyboardSensor, PointerSensor, closestCenter, useSensor, useSensors, type DragEndEvent } from '@dnd-kit/core';
import { SortableContext, arrayMove, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { ConsoleLogger } from 'aws-amplify/utils';
import { defineMessages, FormattedMessage, useIntl, type MessageTag, type NoMessageValues } from 'react-intl';

import type { RoutingTestResult } from '../../graphql/generated/operation-types';
import { reviewManagementHref } from '../../routes/constants';
import { TableEmptyState } from '../common/table';
import { listReviewQueues } from '../review-queues/reviewer-assignment';
import ReasonField from './ReasonField';
import { aggregateMessage, inputMessage } from './RoutingConditionsEditor';
import RoutingDmnControls, { type RoutingDmnControlsRef } from './RoutingDmnControls';
import RoutingRuleModal, { type QueueChoice } from './RoutingRuleModal';
import RoutingTestPanel from './RoutingTestPanel';
import { shadowedRules } from './routing-shadowing';
import {
  BUILT_IN_PREFIX,
  DEFAULT_AGGREGATE,
  DEFAULT_QUEUE_ID,
  MAX_PLACEMENTS,
  MAX_REASON_BYTES,
  MAX_RULES,
  MIN_PLACEMENTS,
  RULE_ID_PATTERN,
  isBuiltIn,
  missingBuiltInRules,
  queueOf,
  readRouting,
  readsPolicyResult,
  refusedRuleId,
  tableOf,
  withQueue,
  withRules,
  type RoutingCondition,
  type RoutingConfig,
  type RoutingDefaultOutcome,
  type RoutingRule,
} from './routing-types';

const logger = new ConsoleLogger('RoutingLayout');

const messages = defineMessages<{
  header: NoMessageValues;
  counter: { readonly count: number };
  description: NoMessageValues;
  readOnly: NoMessageValues;
  addRule: NoMessageValues;
  moreActions: NoMessageValues;
  importDmn: NoMessageValues;
  exportDmn15: NoMessageValues;
  exportDmn13: NoMessageValues;
  exportDmn13Hint: NoMessageValues;
  restoreBuiltIn: NoMessageValues;
  restoreBuiltInNone: NoMessageValues;
  ruleLimit: { readonly max: number };
  serverErrorHeader: NoMessageValues;
  editNamedRule: { readonly id: string };
  shadowedHeader: NoMessageValues;
  shadowedItem: { readonly rule: string; readonly earlier: string };
  unknownQueueHeader: NoMessageValues;
  unknownQueueItem: { readonly rule: string; readonly queue: string };
  unknownDefaultQueue: { readonly queue: string };
  queuesError: NoMessageValues;
  reorderColumn: NoMessageValues;
  reorderAria: { readonly id: string };
  positionColumn: NoMessageValues;
  idColumn: NoMessageValues;
  builtIn: NoMessageValues;
  labelColumn: NoMessageValues;
  openAria: { readonly id: string };
  conditionsColumn: NoMessageValues;
  noConditions: NoMessageValues;
  outcomeColumn: NoMessageValues;
  outcomeQueue: { readonly queue: string };
  notReviewed: NoMessageValues;
  enabledColumn: NoMessageValues;
  enabledAria: { readonly id: string };
  actionsColumn: NoMessageValues;
  actionsAria: { readonly id: string };
  edit: NoMessageValues;
  duplicate: NoMessageValues;
  moveUp: NoMessageValues;
  moveDown: NoMessageValues;
  remove: NoMessageValues;
  deleteHeader: { readonly id: string };
  deleteBody: NoMessageValues;
  deleteCancel: NoMessageValues;
  deleteConfirm: NoMessageValues;
  emptyTitle: NoMessageValues;
  emptyDescription: NoMessageValues;
  defaultOutcomeLabel: NoMessageValues;
  defaultOutcomeDescription: NoMessageValues;
  defaultNoReview: NoMessageValues;
  defaultQueue: NoMessageValues;
  defaultQueueLabel: NoMessageValues;
  queuePlaceholder: NoMessageValues;
  placementsLabel: NoMessageValues;
  placementsDescription: NoMessageValues;
  placementsError: { readonly min: number; readonly max: number };
}>({
  header: {
    id: 'routing.layout.header',
    defaultMessage: 'Routing Rules',
    description: 'Heading of the Routing Rules tab of a configuration profile.',
  },
  counter: {
    id: 'routing.layout.counter',
    defaultMessage: '({count, number})',
    description: 'Counter beside the heading of the Routing Rules tab. {count} is the number of routing rules in the table.',
  },
  description: {
    id: 'routing.layout.description',
    defaultMessage:
      'After policy rule validation, the first enabled routing rule whose conditions all hold decides whether a document is reviewed and in which queue. Routing rules are evaluated in the order shown.',
    description: 'Text under the heading of the Routing Rules tab.',
  },
  readOnly: {
    id: 'routing.layout.readOnly',
    defaultMessage: 'Only Admins can change routing rules.',
    description: 'Notice on the Routing Rules tab for a user who is not an Admin.',
  },
  addRule: {
    id: 'routing.layout.addRule',
    defaultMessage: 'Add routing rule',
    description: 'Button that opens the dialog for a new routing rule.',
  },
  moreActions: {
    id: 'routing.layout.moreActions',
    defaultMessage: 'More actions',
    description: 'Menu beside the Add routing rule button: importing and exporting the table as DMN, restoring the built-in routing rules.',
  },
  importDmn: {
    id: 'routing.layout.importDmn',
    defaultMessage: 'Import DMN',
    description: 'Menu item that opens a file picker to read a routing table from a DMN file.',
  },
  exportDmn15: {
    id: 'routing.layout.exportDmn15',
    defaultMessage: 'Export DMN 1.5',
    description: 'Menu item that downloads the routing table as a DMN version 1.5 file.',
  },
  exportDmn13: {
    id: 'routing.layout.exportDmn13',
    defaultMessage: 'Export DMN 1.3',
    description: 'Menu item that downloads the routing table as a DMN version 1.3 file.',
  },
  exportDmn13Hint: {
    id: 'routing.layout.exportDmn13Hint',
    defaultMessage: 'The version Camunda Modeler opens',
    description: 'Second line of the Export DMN 1.3 menu item: why this older version is offered.',
  },
  restoreBuiltIn: {
    id: 'routing.layout.restoreBuiltIn',
    defaultMessage: 'Restore built-in routing rules',
    description: 'Menu item, and button of an empty table, that adds the built-in routing rules the table no longer has, at the end.',
  },
  restoreBuiltInNone: {
    id: 'routing.layout.restoreBuiltInNone',
    defaultMessage: 'The table has every built-in routing rule.',
    description: 'Why the Restore built-in routing rules menu item is disabled.',
  },
  ruleLimit: {
    id: 'routing.layout.ruleLimit',
    defaultMessage: 'A table has at most {max, number} routing rules.',
    description: 'Why a routing rule cannot be added or duplicated. {max} is the largest number of routing rules.',
  },
  serverErrorHeader: {
    id: 'routing.layout.serverErrorHeader',
    defaultMessage: 'The configuration was not saved: the routing table was refused',
    description: 'Heading of the error on the Routing Rules tab when the server refused the configuration because of its routing table.',
  },
  editNamedRule: {
    id: 'routing.layout.editNamedRule',
    defaultMessage: 'Edit routing rule {id}',
    description:
      'Button in the error on the Routing Rules tab that opens the routing rule the server named. {id} is the id of the routing rule.',
  },
  shadowedHeader: {
    id: 'routing.layout.shadowedHeader',
    defaultMessage: 'Routing rules that can never match',
    description: 'Heading of the warning that lists routing rules an earlier routing rule always wins over.',
  },
  shadowedItem: {
    id: 'routing.layout.shadowedItem',
    defaultMessage: '{rule} never matches: every document it would match is taken by {earlier}, which comes first.',
    description: 'One line of the warning about unreachable routing rules. {rule} and {earlier} are rule ids.',
  },
  unknownQueueHeader: {
    id: 'routing.layout.unknownQueueHeader',
    defaultMessage: 'Queues that do not exist',
    description: 'Heading of the warning that lists routing rules whose review queue is not in the queue list.',
  },
  unknownQueueItem: {
    id: 'routing.layout.unknownQueueItem',
    defaultMessage:
      '{rule} sends documents to {queue}, which is not a review queue. Create the queue in Review Management, or choose another.',
    description: 'One line of the warning about missing queues. {rule} is a rule id and {queue} a queue id.',
  },
  unknownDefaultQueue: {
    id: 'routing.layout.unknownDefaultQueue',
    defaultMessage: 'The default outcome sends documents to {queue}, which is not a review queue.',
    description: 'Line of the warning about missing queues for the default outcome of the table. {queue} is a queue id.',
  },
  queuesError: {
    id: 'routing.layout.queuesError',
    defaultMessage: 'The review queues could not be loaded, so queues are shown by id and cannot be checked.',
    description: 'Warning on the Routing Rules tab when the list of review queues fails to load.',
  },
  reorderColumn: {
    id: 'routing.layout.reorderColumn',
    defaultMessage: 'Reorder',
    description: 'Accessible header of the column of drag handles that reorder routing rules.',
  },
  reorderAria: {
    id: 'routing.layout.reorderAria',
    defaultMessage: 'Reorder routing rule {id}. Press space to pick it up, the arrow keys to move it and space to drop it.',
    description: 'Accessible name of the drag handle of a routing rule. {id} is the id of the rule.',
  },
  positionColumn: {
    id: 'routing.layout.positionColumn',
    defaultMessage: 'Order',
    description: 'Column header: the position of a routing rule in the evaluation order.',
  },
  idColumn: { id: 'routing.layout.idColumn', defaultMessage: 'Id', description: 'Column header: the id of a routing rule.' },
  builtIn: {
    id: 'routing.layout.builtIn',
    defaultMessage: 'Built-in',
    description: 'Badge on a routing rule that ships with the product.',
  },
  labelColumn: { id: 'routing.layout.labelColumn', defaultMessage: 'Label', description: 'Column header: the name of a routing rule.' },
  openAria: {
    id: 'routing.layout.openAria',
    defaultMessage: 'Routing rule {id}',
    description:
      'Accessible name of the link that opens a routing rule that has no label, where the link shows a dash. {id} is the id of the routing rule.',
  },
  conditionsColumn: {
    id: 'routing.layout.conditionsColumn',
    defaultMessage: 'Conditions',
    description: 'Column header: the conditions of a routing rule.',
  },
  noConditions: {
    id: 'routing.layout.noConditions',
    defaultMessage: 'Every document',
    description: 'Shown in the conditions column for a routing rule with no conditions.',
  },
  outcomeColumn: {
    id: 'routing.layout.outcomeColumn',
    defaultMessage: 'Outcome',
    description: 'Column header: what a routing rule does with a document it matches.',
  },
  outcomeQueue: {
    id: 'routing.layout.outcomeQueue',
    defaultMessage: 'Review queue: {queue}',
    description: 'Outcome of a routing rule in the table: the review queue a matching document goes to. {queue} is the name of the queue.',
  },
  notReviewed: {
    id: 'routing.layout.notReviewed',
    defaultMessage: 'Not reviewed',
    description: 'Outcome of a routing rule in the table: a matching document is not reviewed.',
  },
  enabledColumn: {
    id: 'routing.layout.enabledColumn',
    defaultMessage: 'Enabled',
    description: 'Column header: whether a routing rule is switched on.',
  },
  enabledAria: {
    id: 'routing.layout.enabledAria',
    defaultMessage: 'Routing rule {id} enabled',
    description: 'Accessible name of the switch that turns a routing rule on or off. {id} is the id of the rule.',
  },
  actionsColumn: {
    id: 'routing.layout.actionsColumn',
    defaultMessage: 'Actions',
    description: 'Column header: the actions on a routing rule.',
  },
  actionsAria: {
    id: 'routing.layout.actionsAria',
    defaultMessage: 'Actions for routing rule {id}',
    description: 'Accessible name of the menu of actions on a routing rule. {id} is the id of the rule.',
  },
  edit: { id: 'routing.layout.edit', defaultMessage: 'Edit', description: 'Menu item that opens a routing rule in its dialog.' },
  duplicate: {
    id: 'routing.layout.duplicate',
    defaultMessage: 'Duplicate',
    description: 'Menu item that adds a copy of a routing rule, under a new id, right after it.',
  },
  moveUp: {
    id: 'routing.layout.moveUp',
    defaultMessage: 'Move up',
    description: 'Menu item that moves a routing rule one place earlier in the order.',
  },
  moveDown: {
    id: 'routing.layout.moveDown',
    defaultMessage: 'Move down',
    description: 'Menu item that moves a routing rule one place later in the order.',
  },
  remove: {
    id: 'routing.layout.remove',
    defaultMessage: 'Delete',
    description: 'Menu item that removes a routing rule from the table, after a confirmation.',
  },
  deleteHeader: {
    id: 'routing.layout.deleteHeader',
    defaultMessage: 'Delete routing rule {id}?',
    description: 'Title of the dialog that confirms deleting a routing rule. {id} is the id of the routing rule.',
  },
  deleteBody: {
    id: 'routing.layout.deleteBody',
    defaultMessage: 'The routing rule is removed from the table. Nothing is saved until you save the configuration.',
    description: 'Text of the dialog that confirms deleting a routing rule.',
  },
  deleteCancel: {
    id: 'routing.layout.deleteCancel',
    defaultMessage: 'Cancel',
    description: 'Button that closes the confirmation of deleting a routing rule and keeps the routing rule.',
  },
  deleteConfirm: {
    id: 'routing.layout.deleteConfirm',
    defaultMessage: 'Delete',
    description: 'Button that confirms deleting a routing rule.',
  },
  emptyTitle: {
    id: 'routing.layout.emptyTitle',
    defaultMessage: 'No routing rules',
    description: 'Title shown in the routing table when it has no routing rule.',
  },
  emptyDescription: {
    id: 'routing.layout.emptyDescription',
    defaultMessage: 'Every document gets the default outcome.',
    description: 'Text shown in the routing table when it has no routing rule.',
  },
  defaultOutcomeLabel: {
    id: 'routing.layout.defaultOutcomeLabel',
    defaultMessage: 'Default outcome',
    description: 'Label of the choice of what happens to a document no routing rule matches.',
  },
  defaultOutcomeDescription: {
    id: 'routing.layout.defaultOutcomeDescription',
    defaultMessage: 'What happens to a document that no routing rule matches.',
    description: 'Help under the label of the default outcome of the routing table.',
  },
  defaultNoReview: {
    id: 'routing.layout.defaultNoReview',
    defaultMessage: 'No review',
    description: 'Default outcome option: a document no routing rule matches is not reviewed.',
  },
  defaultQueue: {
    id: 'routing.layout.defaultQueue',
    defaultMessage: 'Send to a review queue',
    description: 'Default outcome option: a document no routing rule matches is placed in a review queue.',
  },
  defaultQueueLabel: {
    id: 'routing.layout.defaultQueueLabel',
    defaultMessage: 'Queue for the default outcome',
    description: 'Label of the list of review queues for the default outcome of the routing table.',
  },
  queuePlaceholder: {
    id: 'routing.layout.queuePlaceholder',
    defaultMessage: 'Choose a queue',
    description: 'Placeholder of the list of review queues for the default outcome.',
  },
  placementsLabel: {
    id: 'routing.layout.placementsLabel',
    defaultMessage: 'Maximum placements in a row',
    description: 'Label of the field that limits how many times in a row a document can be placed in a review queue.',
  },
  placementsDescription: {
    id: 'routing.layout.placementsDescription',
    defaultMessage:
      'How many times in a row a document can be placed in a review queue: the first time, and each time a routing rule sends a reviewed document to a queue again. Above it, processing continues without another review.',
    description: 'Help under the field that limits consecutive placements in a review queue.',
  },
  placementsError: {
    id: 'routing.layout.placementsError',
    defaultMessage: 'Enter a whole number from {min, number} to {max, number}.',
    description: 'Error under the field that limits consecutive placements. {min} and {max} are the smallest and largest values allowed.',
  },
});

/** What the tab says when routing rules are not evaluated, or read something that is off. */
const warningMessages = defineMessages<{
  reviewRoutingOff: NoMessageValues;
  openProcessingSettings: NoMessageValues;
  defaultQueueOff: { readonly link: MessageTag };
  policyOff: NoMessageValues;
}>({
  reviewRoutingOff: {
    id: 'routing.warnings.reviewRoutingOff',
    defaultMessage: 'Review Routing is off for this profile, so no document is held for review and these routing rules are not evaluated.',
    description: 'Warning on the Routing Rules tab of a profile whose Review Routing switch (in Processing Settings) is off.',
  },
  openProcessingSettings: {
    id: 'routing.warnings.openProcessingSettings',
    defaultMessage: 'Open Processing Settings',
    description: 'Button in the warning that Review Routing is off: shows the Processing Settings tab, where the switch is.',
  },
  defaultQueueOff: {
    id: 'routing.warnings.defaultQueueOff',
    defaultMessage:
      'The Default review queue is off, so routing rules are not evaluated and documents use the document-page review flow. Turn it on in <link>Review Management</link>.',
    description:
      'Warning on the Routing Rules tab, for an Admin, when the Default review queue is disabled. <link> marks the name of the page where queues are managed.',
  },
  policyOff: {
    id: 'routing.warnings.policyOff',
    defaultMessage: 'Tests a policy rule result; policy rule validation is off',
    description:
      'Warning on a routing rule whose condition or reason reads a policy rule result, in a profile where policy rule validation is off or has no policies.',
  },
});

/** The handle a routing rule is dragged by; also the element the keyboard sensor listens on. */
const DragHandle = ({ sortableId, label }: { sortableId: string; label: string }): React.JSX.Element => {
  const { attributes, listeners, setNodeRef, transform, transition } = useSortable({ id: sortableId });
  return (
    <span
      ref={setNodeRef}
      style={{ cursor: 'grab', display: 'inline-flex', transform: CSS.Transform.toString(transform), transition }}
      {...attributes}
      {...listeners}
      aria-label={label}
    >
      <Icon name="drag-indicator" />
    </span>
  );
};

interface RoutingLayoutProps {
  /** The profile's `routing` section, as the configuration form holds it. */
  routing: unknown;
  onChange: (routing: RoutingConfig) => void;
  /** True for anyone but an Admin. */
  readOnly: boolean;
  /** True when Review Routing is on for the profile (`hitl.enabled` in the form). */
  reviewRoutingEnabled: boolean;
  /** True when policy rule validation runs for the profile: it is on and the profile has policies. */
  policyRuleValidationEnabled: boolean;
  /** Shows the Processing Settings tab, where the Review Routing switch is. */
  onOpenProcessingSettings: () => void;
  /** The document classes of the profile (for field paths). */
  classes?: unknown;
  /** The policy classes of the profile (for policy rule ids and types). */
  policyClasses?: unknown;
  /** The `routing` section of the system defaults, which holds the built-in routing rules. */
  defaults?: unknown;
  /** The profile being edited; it names an exported DMN file. */
  profileName?: string;
  /** What the server refused about the routing table on the last save, as it said it. */
  serverError?: string | null;
}

interface Row {
  key: string;
  index: number;
  rule: RoutingRule;
}

const RoutingLayout = ({
  routing,
  onChange,
  readOnly,
  reviewRoutingEnabled,
  policyRuleValidationEnabled,
  onOpenProcessingSettings,
  classes = null,
  policyClasses = null,
  defaults = null,
  profileName = 'profile',
  serverError = null,
}: RoutingLayoutProps): React.JSX.Element => {
  const intl = useIntl();
  const value = useMemo(() => readRouting(routing), [routing]);
  const table = tableOf(value);
  const rules = table.rules;

  // Queue names, and whether the Default queue is on, are an Admin's to read (the queue
  // list is an Admin operation). The list is not loaded for another role, which
  // therefore sees ids, no queue warning and no Default queue warning.
  const [queues, setQueues] = useState<QueueChoice[] | null>(null);
  const [queuesFailed, setQueuesFailed] = useState(false);
  const [defaultQueueOff, setDefaultQueueOff] = useState(false);
  useEffect(() => {
    if (readOnly) return undefined;
    let current = true;
    listReviewQueues()
      .then((loaded) => {
        if (!current) return;
        setQueues(loaded.map((queue) => ({ id: queue.id, name: queue.name })));
        setDefaultQueueOff(loaded.some((queue) => queue.isDefaultQueue && queue.enabled === false));
      })
      .catch((err: unknown) => {
        logger.error('listReviewQueues failed', err);
        if (current) setQueuesFailed(true);
      });
    return () => {
      current = false;
    };
  }, [readOnly]);

  const [editing, setEditing] = useState<{ index: number | null; serverError: string | null } | null>(null);
  const [deleting, setDeleting] = useState<number | null>(null);
  // What the test panel last answered; the routing rule dialog is given it.
  const [lastTest, setLastTest] = useState<RoutingTestResult | null>(null);
  const dmn = useRef<RoutingDmnControlsRef>(null);
  const [dmnBusy, setDmnBusy] = useState(false);
  const [placements, setPlacements] = useState(String(value.max_consecutive_placements));
  useEffect(() => setPlacements(String(value.max_consecutive_placements)), [value.max_consecutive_placements]);

  const sensors = useSensors(useSensor(PointerSensor), useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }));

  // A routing rule's id is its key when the ids are usable as keys; a table read from a
  // file can repeat one or leave one empty, and then the position is.
  const idsAreKeys = new Set(rules.map((rule) => rule.id)).size === rules.length && rules.every((rule) => rule.id);
  const rows: Row[] = rules.map((rule, index) => ({ key: idsAreKeys ? rule.id : String(index), index, rule }));

  const setRules = (next: RoutingRule[]): void => onChange(withRules(value, next));
  const move = (from: number, to: number): void => {
    if (to < 0 || to >= rules.length || from === to) return;
    setRules(arrayMove(rules, from, to));
  };
  const onDragEnd = ({ active, over }: DragEndEvent): void => {
    if (!over || active.id === over.id) return;
    const from = rows.findIndex((row) => row.key === active.id);
    const to = rows.findIndex((row) => row.key === over.id);
    if (from >= 0 && to >= 0) move(from, to);
  };

  /** An id no routing rule has, for a copy: the id with `-copy`, numbered when that is taken. */
  const copyId = (id: string): string => {
    const taken = new Set(rules.map((rule) => rule.id));
    // A copy of a built-in routing rule is not one of the built-in routing rules.
    const stem = isBuiltIn({ id }) ? id.slice(BUILT_IN_PREFIX.length) : id;
    for (let copy = 1; ; copy += 1) {
      const suffix = copy === 1 ? '-copy' : `-copy-${copy}`;
      // An id has a largest length, so a long one is cut from its end to make room.
      let candidate = `${stem}${suffix}`;
      for (let kept = stem.length - 1; kept > 0 && !RULE_ID_PATTERN.test(candidate); kept -= 1) {
        candidate = `${stem.slice(0, kept)}${suffix}`;
      }
      if (!taken.has(candidate)) return candidate;
    }
  };
  const duplicate = (index: number): void => {
    const source = rules[index];
    const copy: RoutingRule = {
      ...source,
      id: copyId(source.id),
      conditions: source.conditions.map((condition) => ({ ...condition })),
      actions: source.actions.map((action) => ({ ...action })),
    };
    setRules([...rules.slice(0, index + 1), copy, ...rules.slice(index + 1)]);
  };

  const queueName = (queueId: string): string => queues?.find((queue) => queue.id === queueId)?.name || queueId;
  const queueKnown = (queueId: string): boolean => queueId === DEFAULT_QUEUE_ID || !queues || queues.some((queue) => queue.id === queueId);

  const conditionText = (condition: RoutingCondition): string => {
    const input = inputMessage(condition.input);
    const aggregate = condition.aggregate && condition.aggregate !== DEFAULT_AGGREGATE ? aggregateMessage(condition.aggregate) : undefined;
    return [
      input ? intl.formatMessage(input) : condition.input,
      condition.path,
      aggregate ? `(${intl.formatMessage(aggregate)})` : '',
      condition.test,
    ]
      .filter(Boolean)
      .join(' ');
  };

  const outcomeText = (rule: RoutingRule): string => {
    const queue = queueOf(rule.actions);
    return queue ? intl.formatMessage(messages.outcomeQueue, { queue: queueName(queue) }) : intl.formatMessage(messages.notReviewed);
  };

  const missingBuiltIns = missingBuiltInRules(value, defaults);
  const restoreBuiltIns = (): void => setRules([...rules, ...missingBuiltIns]);

  const shadowed = shadowedRules(rules);
  const unknownQueues = rules.filter((rule) => {
    const queue = queueOf(rule.actions);
    return queue !== '' && !queueKnown(queue);
  });
  const defaultQueue = queueOf(table.default_outcome.actions);
  const defaultQueueUnknown = defaultQueue !== '' && !queueKnown(defaultQueue);

  const namedRule = refusedRuleId(serverError);
  const namedRuleIndex = namedRule ? rules.findIndex((rule) => rule.id === namedRule) : -1;

  const setDefaultOutcome = (outcome: RoutingDefaultOutcome): void =>
    onChange({ ...value, tables: [{ ...table, default_outcome: outcome }] });

  const queueOptions: SelectProps.Option[] = [
    ...(defaultQueue && !(queues ?? []).some((queue) => queue.id === defaultQueue) ? [{ value: defaultQueue, label: defaultQueue }] : []),
    ...(queues ?? []).map((queue) => ({ value: queue.id, label: queue.name || queue.id })),
  ];

  const placementsNumber = Number(placements);
  const placementsValid = Number.isInteger(placementsNumber) && placementsNumber >= MIN_PLACEMENTS && placementsNumber <= MAX_PLACEMENTS;

  const atRuleLimit = rules.length >= MAX_RULES;
  const ruleLimitReason = intl.formatMessage(messages.ruleLimit, { max: MAX_RULES });

  const columnDefinitions: TableProps.ColumnDefinition<Row>[] = [
    ...(readOnly
      ? []
      : [
          {
            id: 'reorder',
            header: intl.formatMessage(messages.reorderColumn),
            width: 60,
            cell: (row: Row) => <DragHandle sortableId={row.key} label={intl.formatMessage(messages.reorderAria, { id: row.rule.id })} />,
          },
        ]),
    { id: 'position', header: intl.formatMessage(messages.positionColumn), width: 80, cell: (row) => row.index + 1 },
    {
      id: 'id',
      header: intl.formatMessage(messages.idColumn),
      cell: (row) => (
        <SpaceBetween direction="horizontal" size="xs">
          <span>{row.rule.id}</span>
          {isBuiltIn(row.rule) && <Badge>{intl.formatMessage(messages.builtIn)}</Badge>}
        </SpaceBetween>
      ),
    },
    {
      id: 'label',
      header: intl.formatMessage(messages.labelColumn),
      // The label opens the routing rule: to edit it for an Admin, to read it for anyone else.
      cell: (row) => (
        <Link
          ariaLabel={row.rule.label ? undefined : intl.formatMessage(messages.openAria, { id: row.rule.id })}
          onFollow={() => setEditing({ index: row.index, serverError: null })}
        >
          {row.rule.label || '—'}
        </Link>
      ),
    },
    {
      id: 'conditions',
      header: intl.formatMessage(messages.conditionsColumn),
      cell: (row) => (
        <SpaceBetween size="xxxs">
          {row.rule.conditions.length === 0 ? (
            <Box color="text-body-secondary">{intl.formatMessage(messages.noConditions)}</Box>
          ) : (
            <span>{intl.formatList(row.rule.conditions.map(conditionText), { type: 'conjunction' })}</span>
          )}
          {!policyRuleValidationEnabled && readsPolicyResult(row.rule) && (
            <StatusIndicator type="warning">{intl.formatMessage(warningMessages.policyOff)}</StatusIndicator>
          )}
        </SpaceBetween>
      ),
    },
    { id: 'outcome', header: intl.formatMessage(messages.outcomeColumn), cell: (row) => outcomeText(row.rule) },
    {
      id: 'enabled',
      header: intl.formatMessage(messages.enabledColumn),
      width: 110,
      cell: (row) => (
        <Toggle
          checked={row.rule.enabled}
          disabled={readOnly}
          ariaLabel={intl.formatMessage(messages.enabledAria, { id: row.rule.id })}
          onChange={({ detail }) =>
            setRules(rules.map((rule, index) => (index === row.index ? { ...rule, enabled: detail.checked } : rule)))
          }
        />
      ),
    },
    ...(readOnly
      ? []
      : [
          {
            id: 'actions',
            header: intl.formatMessage(messages.actionsColumn),
            width: 100,
            cell: (row: Row) => (
              <ButtonDropdown
                variant="icon"
                expandToViewport
                ariaLabel={intl.formatMessage(messages.actionsAria, { id: row.rule.id })}
                items={[
                  { id: 'edit', text: intl.formatMessage(messages.edit) },
                  {
                    id: 'duplicate',
                    text: intl.formatMessage(messages.duplicate),
                    disabled: atRuleLimit,
                    disabledReason: ruleLimitReason,
                  },
                  { id: 'up', text: intl.formatMessage(messages.moveUp), disabled: row.index === 0 },
                  { id: 'down', text: intl.formatMessage(messages.moveDown), disabled: row.index === rules.length - 1 },
                  { id: 'delete', text: intl.formatMessage(messages.remove) },
                ]}
                onItemClick={({ detail }) => {
                  if (detail.id === 'edit') setEditing({ index: row.index, serverError: null });
                  if (detail.id === 'duplicate') duplicate(row.index);
                  if (detail.id === 'up') move(row.index, row.index - 1);
                  if (detail.id === 'down') move(row.index, row.index + 1);
                  if (detail.id === 'delete') setDeleting(row.index);
                }}
              />
            ),
          },
        ]),
  ];

  const editingRule = editing && editing.index !== null ? rules[editing.index] : null;
  const deletingRule = deleting !== null ? rules[deleting] : undefined;

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h2"
            counter={intl.formatMessage(messages.counter, { count: rules.length })}
            description={intl.formatMessage(messages.description)}
            actions={
              readOnly ? undefined : (
                <SpaceBetween direction="horizontal" size="xs">
                  <ButtonDropdown
                    loading={dmnBusy}
                    items={[
                      { id: 'import', text: intl.formatMessage(messages.importDmn) },
                      { id: 'export-1.5', text: intl.formatMessage(messages.exportDmn15) },
                      {
                        id: 'export-1.3',
                        text: intl.formatMessage(messages.exportDmn13),
                        secondaryText: intl.formatMessage(messages.exportDmn13Hint),
                      },
                      {
                        id: 'restore',
                        text: intl.formatMessage(messages.restoreBuiltIn),
                        disabled: missingBuiltIns.length === 0,
                        disabledReason: intl.formatMessage(messages.restoreBuiltInNone),
                      },
                    ]}
                    onItemClick={({ detail }) => {
                      if (detail.id === 'import') dmn.current?.importDmn();
                      if (detail.id === 'export-1.5') dmn.current?.exportDmn('1.5');
                      if (detail.id === 'export-1.3') dmn.current?.exportDmn('1.3');
                      if (detail.id === 'restore') restoreBuiltIns();
                    }}
                  >
                    {intl.formatMessage(messages.moreActions)}
                  </ButtonDropdown>
                  <Button
                    variant="primary"
                    disabled={atRuleLimit}
                    disabledReason={atRuleLimit ? ruleLimitReason : undefined}
                    onClick={() => setEditing({ index: null, serverError: null })}
                  >
                    {intl.formatMessage(messages.addRule)}
                  </Button>
                </SpaceBetween>
              )
            }
          >
            {intl.formatMessage(messages.header)}
          </Header>
        }
      >
        {!readOnly && (
          <RoutingDmnControls ref={dmn} routing={value} profileName={profileName} onImport={onChange} onBusyChange={setDmnBusy} />
        )}
        <SpaceBetween size="m">
          {readOnly && <Alert type="info">{intl.formatMessage(messages.readOnly)}</Alert>}
          {!reviewRoutingEnabled && (
            <Alert
              type="warning"
              action={<Button onClick={onOpenProcessingSettings}>{intl.formatMessage(warningMessages.openProcessingSettings)}</Button>}
            >
              {intl.formatMessage(warningMessages.reviewRoutingOff)}
            </Alert>
          )}
          {defaultQueueOff && (
            <Alert type="warning">
              <FormattedMessage
                {...warningMessages.defaultQueueOff}
                values={{ link: (chunks) => <Link href={reviewManagementHref()}>{chunks}</Link> }}
              />
            </Alert>
          )}
          {serverError && (
            <Alert
              type="error"
              header={intl.formatMessage(messages.serverErrorHeader)}
              action={
                !readOnly && namedRuleIndex >= 0 ? (
                  <Button onClick={() => setEditing({ index: namedRuleIndex, serverError })}>
                    {intl.formatMessage(messages.editNamedRule, { id: namedRule ?? '' })}
                  </Button>
                ) : undefined
              }
            >
              {serverError}
            </Alert>
          )}
          {queuesFailed && <Alert type="warning">{intl.formatMessage(messages.queuesError)}</Alert>}
          {shadowed.length > 0 && (
            <Alert type="warning" header={intl.formatMessage(messages.shadowedHeader)}>
              <ul>
                {shadowed.map((entry) => (
                  <li key={entry.ruleId}>{intl.formatMessage(messages.shadowedItem, { rule: entry.ruleId, earlier: entry.shadowedBy })}</li>
                ))}
              </ul>
            </Alert>
          )}
          {(unknownQueues.length > 0 || defaultQueueUnknown) && (
            <Alert type="warning" header={intl.formatMessage(messages.unknownQueueHeader)}>
              <ul>
                {unknownQueues.map((rule) => (
                  <li key={rule.id}>{intl.formatMessage(messages.unknownQueueItem, { rule: rule.id, queue: queueOf(rule.actions) })}</li>
                ))}
                {defaultQueueUnknown && <li>{intl.formatMessage(messages.unknownDefaultQueue, { queue: defaultQueue })}</li>}
              </ul>
            </Alert>
          )}

          <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
            <SortableContext items={rows.map((row) => row.key)} strategy={verticalListSortingStrategy}>
              <Table
                variant="embedded"
                trackBy="key"
                items={rows}
                columnDefinitions={columnDefinitions}
                wrapLines
                empty={
                  <TableEmptyState
                    title={intl.formatMessage(messages.emptyTitle)}
                    description={intl.formatMessage(messages.emptyDescription)}
                    action={
                      readOnly || missingBuiltIns.length === 0 ? undefined : (
                        <Button onClick={restoreBuiltIns}>{intl.formatMessage(messages.restoreBuiltIn)}</Button>
                      )
                    }
                  />
                }
              />
            </SortableContext>
          </DndContext>

          <FormField
            label={intl.formatMessage(messages.defaultOutcomeLabel)}
            description={intl.formatMessage(messages.defaultOutcomeDescription)}
          >
            <RadioGroup
              value={defaultQueue ? 'queue' : 'no_review'}
              onChange={({ detail }) =>
                setDefaultOutcome(
                  detail.value === 'queue'
                    ? { ...table.default_outcome, actions: withQueue(defaultQueue || DEFAULT_QUEUE_ID) }
                    : // A reason is for a reviewer: an outcome with no queue has none.
                      { actions: [], reason: '' },
                )
              }
              items={[
                { value: 'no_review', label: intl.formatMessage(messages.defaultNoReview), disabled: readOnly },
                { value: 'queue', label: intl.formatMessage(messages.defaultQueue), disabled: readOnly },
              ]}
            />
          </FormField>
          {defaultQueue &&
            (readOnly ? (
              <FormField label={intl.formatMessage(messages.defaultQueueLabel)}>
                <Input value={defaultQueue} disabled />
              </FormField>
            ) : (
              <FormField label={intl.formatMessage(messages.defaultQueueLabel)}>
                <Select
                  selectedOption={queueOptions.find((option) => option.value === defaultQueue) ?? null}
                  options={queueOptions}
                  placeholder={intl.formatMessage(messages.queuePlaceholder)}
                  onChange={({ detail }) =>
                    setDefaultOutcome({ ...table.default_outcome, actions: withQueue(detail.selectedOption.value ?? defaultQueue) })
                  }
                />
              </FormField>
            ))}
          {defaultQueue && (
            <ReasonField
              value={table.default_outcome.reason}
              onChange={(reason) => setDefaultOutcome({ ...table.default_outcome, reason })}
              readOnly={readOnly}
              conditions={[]}
              maxBytes={MAX_REASON_BYTES}
            />
          )}
          <FormField
            label={intl.formatMessage(messages.placementsLabel)}
            description={intl.formatMessage(messages.placementsDescription)}
            errorText={
              placementsValid ? undefined : intl.formatMessage(messages.placementsError, { min: MIN_PLACEMENTS, max: MAX_PLACEMENTS })
            }
          >
            <Input
              type="number"
              inputMode="numeric"
              value={placements}
              disabled={readOnly}
              onChange={({ detail }) => {
                setPlacements(detail.value);
                const parsed = Number(detail.value);
                if (detail.value.trim() && Number.isInteger(parsed) && parsed >= MIN_PLACEMENTS && parsed <= MAX_PLACEMENTS) {
                  onChange({ ...value, max_consecutive_placements: parsed });
                }
              }}
            />
          </FormField>

          {!readOnly && <RoutingTestPanel routing={value} queueName={queueName} onResult={setLastTest} />}
        </SpaceBetween>
      </Container>

      <RoutingRuleModal
        visible={editing !== null}
        rule={editingRule}
        otherIds={rules.filter((_rule, index) => index !== editing?.index).map((rule) => rule.id)}
        queues={queues ?? []}
        classes={classes}
        policyClasses={policyClasses}
        serverError={editing?.serverError ?? null}
        readOnly={readOnly}
        lastTest={lastTest}
        onDismiss={() => setEditing(null)}
        onSave={(rule) => {
          setRules(
            editing?.index === null || editing === null
              ? [...rules, rule]
              : rules.map((existing, index) => (index === editing.index ? rule : existing)),
          );
          setEditing(null);
        }}
      />

      {!readOnly && (
        <Modal
          visible={deletingRule !== undefined}
          onDismiss={() => setDeleting(null)}
          header={intl.formatMessage(messages.deleteHeader, { id: deletingRule?.id ?? '' })}
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={() => setDeleting(null)}>
                  {intl.formatMessage(messages.deleteCancel)}
                </Button>
                <Button
                  variant="primary"
                  onClick={() => {
                    setRules(rules.filter((_rule, index) => index !== deleting));
                    setDeleting(null);
                  }}
                >
                  {intl.formatMessage(messages.deleteConfirm)}
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          {intl.formatMessage(messages.deleteBody)}
        </Modal>
      )}
    </SpaceBetween>
  );
};

export default RoutingLayout;
