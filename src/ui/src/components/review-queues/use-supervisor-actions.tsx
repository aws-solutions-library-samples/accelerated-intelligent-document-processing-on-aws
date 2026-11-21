// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The Admin actions on production review items — Assign, Unassign, Move, Remove from
// queue — for one item (row menu) or several (the selection), plus the modals they open.
import React, { useCallback, useState } from 'react';
import type { ButtonDropdownProps } from '@cloudscape-design/components';
import { defineMessages, useIntl, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';

import type { ReviewItem, ReviewQueueDefinition } from '../../graphql/generated/operation-types';
import useAppContext from '../../contexts/app';
import { QueueItemState } from '../common/review-status';
import { describeApiError } from '../../hooks/utils/graphql-error';
import AssignModal from './AssignModal';
import { MoveItemModal, RemoveItemModal, UnassignModal } from './SupervisorModals';
import { documentName, forceReleaseItem, moveItem, reassignItem, removeItem } from './review-api';
import { eligibleAnnotators, listAnnotatorEmails } from './reviewer-assignment';

export type SupervisorAction = 'assign' | 'unassign' | 'move' | 'remove';
const SUPERVISOR_ACTIONS = new Set<string>(['assign', 'unassign', 'move', 'remove']);

export const isSupervisorAction = (id: string): id is SupervisorAction => SUPERVISOR_ACTIONS.has(id);

/** Whether an item can be given to an Annotator (it is waiting, or assigned to someone else). */
export const isAssignable = (item: ReviewItem): boolean => item.state === QueueItemState.OPEN || item.state === QueueItemState.CLAIMED;

/** Whether an item is assigned and can be taken back. */
export const isUnassignable = (item: ReviewItem): boolean => item.state === QueueItemState.CLAIMED;

/** Whether an item can be taken out of review (it is waiting, or assigned). */
export const isRemovable = isAssignable;

const ELIGIBLE: Record<SupervisorAction, (item: ReviewItem) => boolean> = {
  assign: isAssignable,
  unassign: isUnassignable,
  move: () => true,
  remove: isRemovable,
};

type Count = { readonly count: number };
type Failures = { readonly count: number; readonly failures: string };

const messages = defineMessages<{
  group: NoMessageValues;
  reassign: NoMessageValues;
  assign: NoMessageValues;
  notWaiting: NoMessageValues;
  unassign: NoMessageValues;
  notAssigned: NoMessageValues;
  move: NoMessageValues;
  noOtherQueue: NoMessageValues;
  remove: NoMessageValues;
  loadAnnotatorsAction: NoMessageValues;
  assignItemAction: NoMessageValues;
  unassignItemAction: NoMessageValues;
  moveItemAction: NoMessageValues;
  removeItemAction: NoMessageValues;
  assigned: { readonly count: number; readonly email: string };
  unassigned: Count;
  moved: { readonly count: number; readonly queue: string };
  removed: Count;
  assignFailed: Failures;
  unassignFailed: Failures;
  moveFailed: Failures;
  removeFailed: Failures;
}>({
  group: {
    id: 'reviewQueues.supervisorActions.group',
    defaultMessage: 'Admin',
    description:
      "Group heading in a review item's row menu on a queue's page, holding the actions only an Admin has. Admin is a user role.",
  },
  reassign: {
    id: 'reviewQueues.supervisorActions.reassign',
    defaultMessage: 'Reassign…',
    description: 'Row menu item of an assigned review item that assigns it to another Annotator (opens a dialog).',
  },
  assign: {
    id: 'reviewQueues.supervisorActions.assign',
    defaultMessage: 'Assign…',
    description: 'Row menu item of a waiting review item that assigns it to an Annotator (opens a dialog).',
  },
  notWaiting: {
    id: 'reviewQueues.supervisorActions.notWaiting',
    defaultMessage: 'This item is not waiting for review',
    description:
      "Why the Assign… or Remove from queue… item of a review item's row menu is disabled: the item is neither waiting nor assigned.",
  },
  unassign: {
    id: 'reviewQueues.supervisorActions.unassign',
    defaultMessage: 'Unassign',
    description: 'Row menu item of an assigned review item that takes it back from its Annotator (opens a dialog).',
  },
  notAssigned: {
    id: 'reviewQueues.supervisorActions.notAssigned',
    defaultMessage: 'This item is not assigned',
    description: "Why the Unassign item of a review item's row menu is disabled.",
  },
  move: {
    id: 'reviewQueues.supervisorActions.move',
    defaultMessage: 'Move to another queue…',
    description: 'Row menu item of a review item that moves it to another review queue (opens a dialog).',
  },
  noOtherQueue: {
    id: 'reviewQueues.supervisorActions.noOtherQueue',
    defaultMessage: 'There is no other queue',
    description: "Why the Move to another queue… item of a review item's row menu is disabled: there is no other review queue.",
  },
  remove: {
    id: 'reviewQueues.supervisorActions.remove',
    defaultMessage: 'Remove from queue…',
    description: 'Row menu item of a review item that takes it out of review (opens a dialog).',
  },
  loadAnnotatorsAction: {
    id: 'reviewQueues.supervisorActions.loadAnnotatorsAction',
    defaultMessage: 'load the Annotators',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when the Annotators an Admin can assign items to cannot be loaded. Annotator is a user role.',
  },
  assignItemAction: {
    id: 'reviewQueues.supervisorActions.assignItemAction',
    defaultMessage: 'assign it',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when one review item cannot be assigned; "it" is the item.',
  },
  unassignItemAction: {
    id: 'reviewQueues.supervisorActions.unassignItemAction',
    defaultMessage: 'unassign it',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when one review item cannot be taken back from its Annotator; "it" is the item.',
  },
  moveItemAction: {
    id: 'reviewQueues.supervisorActions.moveItemAction',
    defaultMessage: 'move it',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when one review item cannot be moved to another queue; "it" is the item.',
  },
  removeItemAction: {
    id: 'reviewQueues.supervisorActions.removeItemAction',
    defaultMessage: 'remove it',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when one review item cannot be taken out of review; "it" is the item.',
  },
  assigned: {
    id: 'reviewQueues.supervisorActions.assigned',
    defaultMessage: 'Assigned {count, plural, one {# item} other {# items}} to {email}.',
    description:
      "Notice after an Admin assigned review items. {count} is how many were assigned and {email} is the Annotator's e-mail address.",
  },
  unassigned: {
    id: 'reviewQueues.supervisorActions.unassigned',
    defaultMessage: 'Unassigned {count, plural, one {# item} other {# items}}.',
    description: 'Notice after an Admin took review items back from their Annotators. {count} is how many.',
  },
  moved: {
    id: 'reviewQueues.supervisorActions.moved',
    defaultMessage: 'Moved {count, plural, one {# item} other {# items}} to {queue}.',
    description:
      "Notice after an Admin moved review items to another queue. {count} is how many and {queue} is the queue's name, as stored.",
  },
  removed: {
    id: 'reviewQueues.supervisorActions.removed',
    defaultMessage: 'Removed {count, plural, one {# item} other {# items}} from review.',
    description: 'Notice after an Admin took review items out of review. {count} is how many.',
  },
  assignFailed: {
    id: 'reviewQueues.supervisorActions.assignFailed',
    defaultMessage: "Couldn't assign {count, plural, one {# item} other {# items}}: {failures}",
    description:
      'Error after an Admin assigned review items and some failed. {count} is how many failed; {failures} lists each one as its file name followed by the reason in parentheses, separated by semicolons.',
  },
  unassignFailed: {
    id: 'reviewQueues.supervisorActions.unassignFailed',
    defaultMessage: "Couldn't unassign {count, plural, one {# item} other {# items}}: {failures}",
    description:
      'Error after an Admin unassigned review items and some failed. {count} is how many failed; {failures} lists each one as its file name followed by the reason in parentheses, separated by semicolons.',
  },
  moveFailed: {
    id: 'reviewQueues.supervisorActions.moveFailed',
    defaultMessage: "Couldn't move {count, plural, one {# item} other {# items}}: {failures}",
    description:
      'Error after an Admin moved review items to another queue and some failed. {count} is how many failed; {failures} lists each one as its file name followed by the reason in parentheses, separated by semicolons.',
  },
  removeFailed: {
    id: 'reviewQueues.supervisorActions.removeFailed',
    defaultMessage: "Couldn't remove {count, plural, one {# item} other {# items}}: {failures}",
    description:
      'Error after an Admin removed review items from review and some failed. {count} is how many failed; {failures} lists each one as its file name followed by the reason in parentheses, separated by semicolons.',
  },
});

/** The error summary of each action, when some of its items failed. */
const failureMessages: Record<SupervisorAction, TypedMessageDescriptor<Failures>> = {
  assign: messages.assignFailed,
  unassign: messages.unassignFailed,
  move: messages.moveFailed,
  remove: messages.removeFailed,
};

/** Each action's phrase in the error sentence of one failed item. */
const itemActionMessages: Record<SupervisorAction, TypedMessageDescriptor<NoMessageValues>> = {
  assign: messages.assignItemAction,
  unassign: messages.unassignItemAction,
  move: messages.moveItemAction,
  remove: messages.removeItemAction,
};

interface Options {
  /** Every system-wide queue definition: the targets Move offers and each queue's eligible Annotators. */
  queues: readonly ReviewQueueDefinition[];
  onChanged: () => void | Promise<void>;
}

const useSupervisorActions = ({ queues, onChanged }: Options) => {
  const intl = useIntl();
  const { setErrorMessage, setSuccessMessage } = useAppContext();
  const [target, setTarget] = useState<{ action: SupervisorAction; items: ReviewItem[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [annotators, setAnnotators] = useState<string[] | null>(null);
  const [annotatorsLoading, setAnnotatorsLoading] = useState(false);

  // Rethrows, so the handler that asked for the list words the error: this load never reads `intl`.
  const loadAnnotators = useCallback(async () => {
    setAnnotatorsLoading(true);
    try {
      setAnnotators(await listAnnotatorEmails());
    } finally {
      setAnnotatorsLoading(false);
    }
  }, []);

  const open = useCallback(
    (action: SupervisorAction, items: readonly ReviewItem[]) => {
      const eligible = items.filter(ELIGIBLE[action]);
      if (eligible.length === 0) return;
      setTarget({ action, items: eligible });
      if (action === 'assign' && annotators == null) {
        loadAnnotators().catch((err: unknown) => setErrorMessage(describeApiError(err, messages.loadAnnotatorsAction, intl)));
      }
    },
    [annotators, loadAnnotators, setErrorMessage, intl],
  );

  /** Row menu entries for an item, as one "Admin" group. */
  const menuItems = useCallback(
    (item: ReviewItem | undefined): ButtonDropdownProps.ItemOrGroup[] => {
      if (!item) return [];
      const assignable = isAssignable(item);
      const unassignable = isUnassignable(item);
      const removable = isRemovable(item);
      const others = queues.filter((q) => q.id !== item.queue);
      return [
        {
          text: intl.formatMessage(messages.group),
          items: [
            {
              id: 'assign',
              text: intl.formatMessage(item.state === QueueItemState.CLAIMED ? messages.reassign : messages.assign),
              disabled: !assignable,
              disabledReason: assignable ? undefined : intl.formatMessage(messages.notWaiting),
            },
            {
              id: 'unassign',
              text: intl.formatMessage(messages.unassign),
              disabled: !unassignable,
              disabledReason: unassignable ? undefined : intl.formatMessage(messages.notAssigned),
            },
            {
              id: 'move',
              text: intl.formatMessage(messages.move),
              disabled: others.length === 0,
              disabledReason: others.length === 0 ? intl.formatMessage(messages.noOtherQueue) : undefined,
            },
            {
              id: 'remove',
              text: intl.formatMessage(messages.remove),
              disabled: !removable,
              disabledReason: removable ? undefined : intl.formatMessage(messages.notWaiting),
            },
          ],
        },
      ];
    },
    [queues, intl],
  );

  /** Run `fn` on every target item, one at a time, then report once; `done` words the success summary. */
  const runAll = async (action: SupervisorAction, fn: (item: ReviewItem) => Promise<unknown>, done: (count: number) => string) => {
    if (!target) return;
    setBusy(true);
    let ok = 0;
    const failed: string[] = [];
    for (const item of target.items) {
      try {
        await fn(item);
        ok += 1;
      } catch (err) {
        failed.push(`${documentName(item.objectKey)} (${describeApiError(err, itemActionMessages[action], intl)})`);
      }
    }
    setBusy(false);
    setTarget(null);
    if (ok > 0) setSuccessMessage(done(ok));
    if (failed.length > 0)
      setErrorMessage(intl.formatMessage(failureMessages[action], { count: failed.length, failures: failed.join('; ') }));
    await onChanged();
  };

  const items = target?.items ?? [];
  const firstQueue = queues.find((q) => q.id === items[0]?.queue) ?? null;
  const single = items.length === 1 ? items[0] : null;

  const modals = (
    <>
      <AssignModal
        visible={target?.action === 'assign'}
        count={items.length}
        documentLabel={single ? documentName(single.objectKey) : undefined}
        currentAssignee={single?.ownerEmail}
        candidates={eligibleAnnotators(annotators ?? [], firstQueue)}
        candidatesLoading={annotatorsLoading}
        loading={busy}
        onDismiss={() => setTarget(null)}
        onConfirm={(email) =>
          void runAll(
            'assign',
            (item) => reassignItem(item, email),
            (count) => intl.formatMessage(messages.assigned, { count, email }),
          )
        }
      />
      <UnassignModal
        items={target?.action === 'unassign' ? items : []}
        loading={busy}
        onDismiss={() => setTarget(null)}
        onConfirm={() => void runAll('unassign', forceReleaseItem, (count) => intl.formatMessage(messages.unassigned, { count }))}
      />
      <MoveItemModal
        items={target?.action === 'move' ? items : []}
        loading={busy}
        queues={queues.map((q) => ({ id: q.id, name: q.name }))}
        onDismiss={() => setTarget(null)}
        onConfirm={(queueId) =>
          void runAll(
            'move',
            (item) => moveItem(item, queueId),
            (count) => intl.formatMessage(messages.moved, { count, queue: queues.find((q) => q.id === queueId)?.name ?? queueId }),
          )
        }
      />
      <RemoveItemModal
        items={target?.action === 'remove' ? items : []}
        loading={busy}
        onDismiss={() => setTarget(null)}
        onConfirm={(note) =>
          void runAll(
            'remove',
            (item) => removeItem(item, note),
            (count) => intl.formatMessage(messages.removed, { count }),
          )
        }
      />
    </>
  );

  return { open, menuItems, modals };
};

export default useSupervisorActions;
