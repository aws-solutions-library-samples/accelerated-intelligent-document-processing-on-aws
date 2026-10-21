// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The Admin actions on production review items — Assign, Unassign, Move, Remove from
// queue — for one item (row menu) or several (the selection), plus the modals they open.
import React, { useCallback, useState } from 'react';
import type { ButtonDropdownProps } from '@cloudscape-design/components';

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

interface Options {
  /** Every system-wide queue definition: the targets Move offers and each queue's eligible Annotators. */
  queues: readonly ReviewQueueDefinition[];
  onChanged: () => void | Promise<void>;
}

const useSupervisorActions = ({ queues, onChanged }: Options) => {
  const { setErrorMessage, setSuccessMessage } = useAppContext();
  const [target, setTarget] = useState<{ action: SupervisorAction; items: ReviewItem[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [annotators, setAnnotators] = useState<string[] | null>(null);
  const [annotatorsLoading, setAnnotatorsLoading] = useState(false);

  const loadAnnotators = useCallback(async () => {
    setAnnotatorsLoading(true);
    try {
      setAnnotators(await listAnnotatorEmails());
    } catch (err) {
      setErrorMessage(describeApiError(err, 'load the Annotators'));
    } finally {
      setAnnotatorsLoading(false);
    }
  }, [setErrorMessage]);

  const open = useCallback(
    (action: SupervisorAction, items: readonly ReviewItem[]) => {
      const eligible = items.filter(ELIGIBLE[action]);
      if (eligible.length === 0) return;
      setTarget({ action, items: eligible });
      if (action === 'assign' && annotators == null) void loadAnnotators();
    },
    [annotators, loadAnnotators],
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
          text: 'Admin',
          items: [
            {
              id: 'assign',
              text: item.state === QueueItemState.CLAIMED ? 'Reassign…' : 'Assign…',
              disabled: !assignable,
              disabledReason: assignable ? undefined : 'This item is not waiting for review',
            },
            {
              id: 'unassign',
              text: 'Unassign',
              disabled: !unassignable,
              disabledReason: unassignable ? undefined : 'This item is not assigned',
            },
            {
              id: 'move',
              text: 'Move to another queue…',
              disabled: others.length === 0,
              disabledReason: others.length === 0 ? 'There is no other queue' : undefined,
            },
            {
              id: 'remove',
              text: 'Remove from queue…',
              disabled: !removable,
              disabledReason: removable ? undefined : 'This item is not waiting for review',
            },
          ],
        },
      ];
    },
    [queues],
  );

  /** Run `fn` on every target item, one at a time, then report once. */
  const runAll = async (fn: (item: ReviewItem) => Promise<unknown>, done: (n: number) => string, failure: string) => {
    if (!target) return;
    setBusy(true);
    let ok = 0;
    const failed: string[] = [];
    for (const item of target.items) {
      try {
        await fn(item);
        ok += 1;
      } catch (err) {
        failed.push(`${documentName(item.objectKey)} (${describeApiError(err, `${failure} it`)})`);
      }
    }
    setBusy(false);
    setTarget(null);
    if (ok > 0) setSuccessMessage(done(ok));
    if (failed.length > 0)
      setErrorMessage(`Couldn't ${failure} ${failed.length === 1 ? '1 item' : `${failed.length} items`}: ${failed.join('; ')}`);
    await onChanged();
  };

  const noun = (n: number) => (n === 1 ? '1 item' : `${n} items`);
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
            (item) => reassignItem(item, email),
            (n) => `Assigned ${noun(n)} to ${email}.`,
            'assign',
          )
        }
      />
      <UnassignModal
        items={target?.action === 'unassign' ? items : []}
        loading={busy}
        onDismiss={() => setTarget(null)}
        onConfirm={() => void runAll(forceReleaseItem, (n) => `Unassigned ${noun(n)}.`, 'unassign')}
      />
      <MoveItemModal
        items={target?.action === 'move' ? items : []}
        loading={busy}
        queues={queues.map((q) => ({ id: q.id, name: q.name }))}
        onDismiss={() => setTarget(null)}
        onConfirm={(queueId) =>
          void runAll(
            (item) => moveItem(item, queueId),
            (n) => `Moved ${noun(n)} to ${queues.find((q) => q.id === queueId)?.name ?? queueId}.`,
            'move',
          )
        }
      />
      <RemoveItemModal
        items={target?.action === 'remove' ? items : []}
        loading={busy}
        onDismiss={() => setTarget(null)}
        onConfirm={(note) =>
          void runAll(
            (item) => removeItem(item, note),
            (n) => `Removed ${noun(n)} from review.`,
            'remove',
          )
        }
      />
    </>
  );

  return { open, menuItems, modals };
};

export default useSupervisorActions;
