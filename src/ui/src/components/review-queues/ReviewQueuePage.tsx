// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// One production queue: its progress and every item in it, with the Annotator each
// one is assigned to. An Admin (Review Studio › Review Management) selects items to assign,
// unassign or move several at once, or uses ⋮ on a row. An Annotator listed on the
// queue (Home › Queues) claims items — one, the selected ones, or the next one —
// which assigns them to the caller in Home › Tasks. Edit opens an item's
// edit screen without changing its assignment (read-only unless it is yours).
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ConsoleLogger } from 'aws-amplify/utils';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';
import {
  Alert,
  Box,
  Button,
  ButtonDropdown,
  Container,
  ContentLayout,
  Header,
  KeyValuePairs,
  SpaceBetween,
} from '@cloudscape-design/components';
import type { ButtonDropdownProps } from '@cloudscape-design/components';

import type { ReviewItem, ReviewQueueDefinition, ReviewQueueHealth, ReviewQueueSummary } from '../../graphql/generated/operation-types';
import useAppContext from '../../contexts/app';
import usePolling from '../../hooks/use-polling';
import { useOptionalReviewWork } from '../../hooks/use-review-work';
import useUserRole from '../../hooks/use-user-role';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { formatDateTime, formatRelativeTime } from '../../utils/date-time';
import QueueAnnotatorsTable, { queueAnnotatorRows } from './QueueAnnotatorsTable';
import QueueProgress from './QueueProgress';
import useSavedQueueDefinitions from './use-saved-queue-definitions';
import WorkItemsTable from './WorkItemsTable';
import useSupervisorActions, { isAssignable, isRemovable, isSupervisorAction, isUnassignable } from './use-supervisor-actions';
import useWorkItemActions from './use-work-item-actions';
import { QueueItemState } from '../common/review-status';
import { claimItem, claimNextItem, documentName, fetchMyReviewQueues, fetchQueueItems, fetchQueuesOverview } from './review-api';
import QueueDefinitionModal from './QueueDefinitionModal';
import { listAnnotatorEmails, listReviewQueues } from './reviewer-assignment';
import { fromReviewItem } from './work-items';
import BulkSkipModal from '../review-bulk/BulkSkipModal';
import type { BulkSkipMode } from '../review-bulk/BulkSkipModal';
import BulkResultsModal from '../review-bulk/BulkResultsModal';
import { isSkippableQueueItem } from '../review-bulk/review-menu-items';
import { NO_SKIPPABLE_SELECTION_REASON } from '../document-actions/action-reasons';
import { jobDetailPath } from '../../routes/constants';
import type { WorkItem } from './work-items';

const logger = new ConsoleLogger('ReviewQueuePage');
const POLL_MS = 20_000;
const PAGE = 100;

/** Why claimNextReviewItem claimed nothing, in words. */
const CLAIM_NEXT_REASONS: Record<string, string> = {
  EMPTY: 'No item in this queue is waiting to be claimed.',
  CONTENDED: 'Other Annotators claimed the waiting items first. Try again.',
  CAP_REACHED: 'You hold as many items from this queue as you may. Finish or skip one first.',
};

const messages = defineMessages<{
  moreActions: NoMessageValues;
  queueGroup: NoMessageValues;
  move: NoMessageValues;
  noOtherQueue: NoMessageValues;
  selectToMove: NoMessageValues;
  removeFromQueue: NoMessageValues;
  selectToRemove: NoMessageValues;
  skipGroup: NoMessageValues;
  skipAllReviews: NoMessageValues;
  skipByProfile: NoMessageValues;
  adminDescription: NoMessageValues;
  skipJobStarted: NoMessageValues;
  skipResultsHeader: NoMessageValues;
}>({
  moreActions: {
    id: 'reviewQueues.queuePage.moreActions',
    defaultMessage: 'More actions',
    description: "Menu button in the header of a review queue's Items table, holding the queue and skip actions for the selected items.",
  },
  queueGroup: {
    id: 'reviewQueues.queuePage.queueGroup',
    defaultMessage: 'Queue',
    description: 'Group heading in the More actions menu of a review queue: moving or removing the selected items.',
  },
  move: {
    id: 'reviewQueues.queuePage.move',
    defaultMessage: 'Move…',
    description:
      'More actions item that moves the selected items to another review queue (opens a dialog). The count of selected items is appended in parentheses.',
  },
  noOtherQueue: {
    id: 'reviewQueues.queuePage.noOtherQueue',
    defaultMessage: 'There is no other queue',
    description: 'Why the Move… item is disabled: there is no other review queue to move items to.',
  },
  selectToMove: {
    id: 'reviewQueues.queuePage.selectToMove',
    defaultMessage: 'Select items to move',
    description: 'Why the Move… item is disabled: no item is selected.',
  },
  removeFromQueue: {
    id: 'reviewQueues.queuePage.removeFromQueue',
    defaultMessage: 'Remove from queue…',
    description:
      'More actions item that takes the selected items out of the review queues (opens a dialog). The count of removable items is appended in parentheses.',
  },
  selectToRemove: {
    id: 'reviewQueues.queuePage.selectToRemove',
    defaultMessage: 'Select items waiting for review or assigned',
    description: 'Why the Remove from queue… item is disabled: no selected item is waiting for review or assigned.',
  },
  skipGroup: {
    id: 'reviewQueues.queuePage.skipGroup',
    defaultMessage: 'Skip reviews',
    description: 'Group heading in the More actions menu of a review queue: skipping the reviews of documents.',
  },
  skipAllReviews: {
    id: 'reviewQueues.queuePage.skipAllReviews',
    defaultMessage: 'Skip All Reviews',
    description: 'More actions item that skips all reviews of the selected items. The count of skippable items is appended in parentheses.',
  },
  skipByProfile: {
    id: 'reviewQueues.queuePage.skipByProfile',
    defaultMessage: 'Skip All Reviews by profile…',
    description: 'More actions item that opens the dialog to skip all reviews of the documents matching a profile (filters).',
  },
  adminDescription: {
    id: 'reviewQueues.queuePage.adminDescription',
    defaultMessage:
      'Select items to assign them to an Annotator or unassign them. More actions moves them to another queue, removes them from the queues or skips their reviews. Edit opens an item without changing its assignment.',
    description:
      'Description under the Items table header of a review queue, for an Admin. "More actions" and "Edit" name the table\'s menu and row action.',
  },
  skipJobStarted: {
    id: 'reviewQueues.queuePage.skipJobStarted',
    defaultMessage: 'Skip All Reviews is running as a background job.',
    description: 'Success notification when a bulk Skip All Reviews started as a background job; the page then opens the job.',
  },
  skipResultsHeader: {
    id: 'reviewQueues.queuePage.skipResultsHeader',
    defaultMessage: 'Skip All Reviews results',
    description: 'Header of the dialog listing the per-document outcomes of a bulk Skip All Reviews that ran inline.',
  },
});

const isClaimable = (item: ReviewItem): boolean => item.state === QueueItemState.OPEN;

/** The Items table: Document, Source, Queue, Priority and State, then the row Actions menu. */
// Annotator: who each document is assigned to (sortable), so a queue's work can be read by person.
const ITEM_COLUMNS = ['document', 'source', 'queue', 'priority', 'state', 'owner'] as const;

interface ReviewQueuePageProps {
  /** Told the queue's name whenever it loads or is saved (for the breadcrumbs). */
  onName?: (name: string) => void;
}

const ReviewQueuePage = ({ onName }: ReviewQueuePageProps): React.JSX.Element => {
  // The router has already decoded the queue id once.
  const { queue: queueId = '' } = useParams();
  const intl = useIntl();
  const { setErrorMessage, setSuccessMessage } = useAppContext();
  const { isAdmin, canReview, loading: roleLoading } = useUserRole();
  const navigate = useNavigate();
  // Bulk Skip All Reviews from the queue: the open dialog, and the last inline results.
  const [bulkSkip, setBulkSkip] = useState<{ mode: BulkSkipMode; keys: string[] } | null>(null);
  const [bulkResults, setBulkResults] = useState<{
    counts: { outcome: string; count: number }[];
    results: { objectKey: string; outcome: string; kind?: string | null; detail?: string | null }[];
    jobId?: string | null;
  } | null>(null);
  const reviewWork = useOptionalReviewWork();
  const [mySummary, setMySummary] = useState<ReviewQueueSummary | null>(null);
  const [claiming, setClaiming] = useState<string | null>(null);
  const [definitions, setDefinitions] = useState<ReviewQueueDefinition[]>([]);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [annotators, setAnnotators] = useState<string[] | null>(null);
  const [health, setHealth] = useState<ReviewQueueHealth | null>(null);
  const [selected, setSelected] = useState<WorkItem[]>([]);
  const [items, setItems] = useState<WorkItem[] | null>(null);
  const [nextToken, setNextToken] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const savedDefinitions = useSavedQueueDefinitions();

  /** The queue definitions and health are Admin-only; an Annotator reads its own summary of the queue. */
  const loadContext = useCallback(async () => {
    if (isAdmin) {
      const [defs, ov] = await Promise.all([
        listReviewQueues(),
        fetchQueuesOverview().catch((err: unknown) => {
          logger.warn('getReviewQueuesOverview failed', err);
          return null;
        }),
      ]);
      setDefinitions(savedDefinitions.apply(defs));
      setHealth(ov?.queues.find((q) => q.summary.name === queueId) ?? null);
      return;
    }
    const mine = await fetchMyReviewQueues(1).catch((err: unknown) => {
      logger.warn('getMyReviewQueues failed', err);
      return null;
    });
    if (mine) setMySummary(mine.queues.find((q) => q.name === queueId) ?? null);
  }, [isAdmin, queueId, savedDefinitions]);

  const load = useCallback(async () => {
    if (roleLoading) return;
    try {
      const [page] = await Promise.all([fetchQueueItems({ queueName: queueId, claimable: false, limit: PAGE }), loadContext()]);
      const loaded = page.items.map(fromReviewItem(isAdmin ? 'review' : 'queues'));
      setItems(loaded);
      setSelected((prev) => prev.filter((s) => loaded.some((i) => i.id === s.id)));
      setNextToken(page.nextToken ?? null);
      setError(null);
    } catch (err) {
      logger.error('queue load failed', err);
      setError(err);
    }
  }, [queueId, roleLoading, loadContext]);

  usePolling(load, { enabled: !roleLoading, intervalMs: POLL_MS, immediate: true });

  const definition = useMemo(() => definitions.find((d) => d.id === queueId) ?? null, [definitions, queueId]);
  // The last definition this queue was read with, which the Settings dialog edits. A
  // refresh that briefly returns none must not close the dialog (and lose what was
  // typed); if the queue really is gone, saving says so.
  const [lastDefinition, setLastDefinition] = useState<ReviewQueueDefinition | null>(null);
  if (definition && definition !== lastDefinition) setLastDefinition(definition);
  const settingsQueue = definition ?? (lastDefinition?.id === queueId ? lastDefinition : null);
  const summary = health?.summary ?? mySummary;
  const { run: runRowAction } = useWorkItemActions();
  const supervisor = useSupervisorActions({ queues: definitions, onChanged: load });
  // Move targets, computed the way the row menu computes them.
  const otherQueues = useMemo(() => definitions.filter((d) => d.id !== queueId), [definitions, queueId]);
  const annotatorRows = useMemo(
    () => queueAnnotatorRows(definition?.eligibleAnnotators ?? [], health?.reviewers ?? []),
    [definition?.eligibleAnnotators, health?.reviewers],
  );

  const refreshNow = async () => {
    setRefreshing(true);
    try {
      await load();
    } finally {
      setRefreshing(false);
    }
  };

  /** After a claim: the item is the caller's now, so reload the queue and Home › Tasks. */
  const afterClaim = async () => {
    await Promise.all([load(), reviewWork?.refresh()]);
  };

  const claim = async (targets: ReviewItem[], busyId: string) => {
    if (targets.length === 0) return;
    setClaiming(busyId);
    reviewWork?.beginMutation();
    let ok = 0;
    const failed: string[] = [];
    for (const item of targets) {
      try {
        await claimItem(item);
        ok += 1;
      } catch (err) {
        failed.push(`${documentName(item.objectKey)} (${describeApiError(err, 'claim it')})`);
      }
    }
    if (ok > 0)
      setSuccessMessage(
        `Claimed ${targets.length === 1 ? documentName(targets[0].objectKey) : `${ok} items`}. Work ${ok === 1 ? 'it' : 'them'} in Home › Tasks.`,
      );
    if (failed.length > 0)
      setErrorMessage(`Couldn't claim ${failed.length === 1 ? '1 item' : `${failed.length} items`}: ${failed.join('; ')}`);
    setSelected([]);
    try {
      await afterClaim();
    } finally {
      setClaiming(null);
    }
  };

  const claimNext = async () => {
    setClaiming('next');
    reviewWork?.beginMutation();
    try {
      const result = await claimNextItem(queueId);
      if (result.item) {
        setSuccessMessage(`Claimed ${documentName(result.item.objectKey)}. Work it in Home › Tasks.`);
      } else {
        setErrorMessage(CLAIM_NEXT_REASONS[result.reason ?? ''] ?? 'No item was claimed.');
      }
      await afterClaim();
    } catch (err) {
      setErrorMessage(describeApiError(err, 'claim the next item'));
    } finally {
      setClaiming(null);
    }
  };

  /** An Annotator's row menu entry: Claim, for an item waiting for review. */
  const claimMenuItems = (item: WorkItem) => {
    const claimable = !!item.reviewItem && isClaimable(item.reviewItem);
    return [
      {
        id: 'claim',
        text: 'Claim',
        disabled: !claimable,
        disabledReason: claimable
          ? undefined
          : item.assignedToMe
            ? 'This item is assigned to you'
            : item.assignedTo
              ? `Assigned to ${item.assignedTo}`
              : 'This item is not waiting for review',
      },
    ];
  };

  const handleRowAction = (item: WorkItem, action: string) => {
    if (action === 'claim') {
      if (item.reviewItem) void claim([item.reviewItem], item.id);
      return;
    }
    if (isAdmin && isSupervisorAction(action)) {
      if (item.reviewItem) supervisor.open(action, [item.reviewItem]);
      return;
    }
    runRowAction(item, action);
  };

  const selectedItems = selected.flatMap((s) => (s.reviewItem ? [s.reviewItem] : []));
  const assignable = selectedItems.filter(isAssignable);
  const unassignable = selectedItems.filter(isUnassignable);
  const removable = selectedItems.filter(isRemovable);
  const claimable = selectedItems.filter(isClaimable);
  const counted = (label: string, n: number) => (n > 0 ? `${label} (${n})` : label);
  // Skip All Reviews: an Admin any OPEN or CLAIMED item; a Reviewer an OPEN item or their own claim.
  const skippable = selectedItems.filter((i) => isSkippableQueueItem(i, !!isAdmin));
  // The Skip All Reviews group of the table's More actions menu.
  const skipGroup: ButtonDropdownProps.ItemGroup = {
    text: intl.formatMessage(messages.skipGroup),
    items: [
      {
        id: 'skip-selected',
        text: counted(intl.formatMessage(messages.skipAllReviews), skippable.length),
        disabled: skippable.length === 0,
        disabledReason: NO_SKIPPABLE_SELECTION_REASON,
      },
      { id: 'skip-by-profile', text: intl.formatMessage(messages.skipByProfile) },
    ],
  };
  // The Admin's queue group: move the selection to another queue, or take it out of the queues.
  const queueGroup: ButtonDropdownProps.ItemGroup = {
    text: intl.formatMessage(messages.queueGroup),
    items: [
      {
        id: 'move',
        text: counted(intl.formatMessage(messages.move), selectedItems.length),
        disabled: selectedItems.length === 0 || otherQueues.length === 0,
        disabledReason: otherQueues.length === 0 ? intl.formatMessage(messages.noOtherQueue) : intl.formatMessage(messages.selectToMove),
      },
      {
        id: 'remove',
        text: counted(intl.formatMessage(messages.removeFromQueue), removable.length),
        disabled: removable.length === 0,
        disabledReason: intl.formatMessage(messages.selectToRemove),
      },
    ],
  };
  const onMoreAction = (id: string) => {
    if (id === 'skip-selected') setBulkSkip({ mode: 'SELECTED', keys: skippable.map((i) => i.objectKey) });
    else if (id === 'skip-by-profile') setBulkSkip({ mode: 'PROFILE', keys: [] });
    else if (id === 'move') supervisor.open('move', selectedItems);
    else if (id === 'remove') supervisor.open('remove', removable);
  };
  const moreActions = (groups: ButtonDropdownProps.ItemGroup[]) => (
    <ButtonDropdown items={groups} onItemClick={({ detail }) => onMoreAction(detail.id)}>
      <FormattedMessage {...messages.moreActions} />
    </ButtonDropdown>
  );
  const afterBulkSkip = async () => {
    reviewWork?.beginMutation();
    setSelected([]);
    await load();
  };

  const loadMore = async () => {
    if (!nextToken) return;
    setLoadingMore(true);
    try {
      const page = await fetchQueueItems({ queueName: queueId, claimable: false, limit: PAGE, nextToken });
      setItems((prev) => [...(prev ?? []), ...page.items.map(fromReviewItem(isAdmin ? 'review' : 'queues'))]);
      setNextToken(page.nextToken ?? null);
    } catch (err) {
      setErrorMessage(describeApiError(err, 'load more items'));
    } finally {
      setLoadingMore(false);
    }
  };

  const title = definition?.name || summary?.displayName || queueId;
  useEffect(() => {
    if (title) onName?.(title);
  }, [title, onName]);
  const eligible = definition?.eligibleAnnotators ?? [];
  const description = definition?.description || summary?.description || 'Production review queue.';

  const openSettings = async () => {
    setSettingsOpen(true);
    if (annotators != null) return;
    try {
      setAnnotators(await listAnnotatorEmails());
    } catch (err) {
      logger.warn('listAnnotators failed', err);
      setAnnotators([]);
    }
  };

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description={description}
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              {isAdmin && (
                <Button iconName="settings" disabled={!definition} onClick={() => void openSettings()}>
                  Settings
                </Button>
              )}
              <Button iconName="refresh" ariaLabel="Refresh" loading={refreshing} onClick={() => void refreshNow()}>
                Refresh
              </Button>
            </SpaceBetween>
          }
        >
          {title}
        </Header>
      }
    >
      <SpaceBetween size="l">
        {error != null && (
          <Alert
            type={items ? 'warning' : 'error'}
            header={items ? 'Showing the last loaded queue' : "Couldn't load this queue"}
            action={<Button onClick={() => void load()}>Retry</Button>}
          >
            {describeApiError(error, 'load this queue')}
          </Alert>
        )}
        {summary && (
          <QueueProgress
            // A finished item leaves its queue, so progress here is the work underway.
            count={summary.claimed}
            total={summary.unclaimed + summary.claimed}
            countLabel="assigned or in progress"
            description="The share of this queue's documents an Annotator is working on"
          />
        )}
        <Container header={<Header variant="h2">Key Metrics</Header>}>
          {!isAdmin ? (
            mySummary ? (
              <KeyValuePairs
                columns={4}
                items={[
                  { label: 'Pending', value: mySummary.unclaimed },
                  { label: 'Assigned', value: mySummary.claimed },
                  { label: 'Assigned to you', value: mySummary.claimedByMe },
                  {
                    label: 'Oldest waiting',
                    value: mySummary.oldestEnqueuedAt ? (
                      <span title={formatDateTime(mySummary.oldestEnqueuedAt)}>{formatRelativeTime(mySummary.oldestEnqueuedAt)}</span>
                    ) : (
                      '—'
                    ),
                  },
                ]}
              />
            ) : (
              <Box color="text-body-secondary">{items ? 'This queue does not list you as an Annotator.' : 'Loading…'}</Box>
            )
          ) : summary || definition ? (
            <KeyValuePairs
              columns={5}
              items={[
                { label: 'Pending', value: summary?.unclaimed ?? '—' },
                {
                  label: 'Assigned',
                  value: summary ? Math.max(0, summary.claimed - (summary.inProgress ?? 0)) : '—',
                },
                { label: 'In Progress', value: summary?.inProgress ?? '—' },
                { label: 'Annotators', value: definition ? eligible.length : (summary?.annotators ?? '—') },
                { label: 'Priority', value: definition?.priority ?? summary?.priority ?? '—' },
              ]}
            />
          ) : (
            <Box color="text-body-secondary">{items ? 'This queue is not defined in Review Management.' : 'Loading…'}</Box>
          )}
        </Container>
        {isAdmin && <QueueAnnotatorsTable rows={annotatorRows} loading={definition == null && health == null && error == null} />}
        <WorkItemsTable
          items={items ?? []}
          columns={ITEM_COLUMNS}
          loading={(items == null && error == null) || (refreshing && (items ?? []).length === 0)}
          title="Documents"
          onRowAction={handleRowAction}
          extraRowActions={(item) => (isAdmin ? supervisor.menuItems(item.reviewItem) : claimMenuItems(item))}
          busyRowId={claiming}
          showStatusFilter
          selectionType="multi"
          selectedItems={selected}
          onSelectionChange={setSelected}
          actions={
            isAdmin ? (
              <SpaceBetween direction="horizontal" size="xs">
                <span title={assignable.length === 0 ? 'Select items waiting for review' : undefined}>
                  <Button disabled={assignable.length === 0} onClick={() => supervisor.open('assign', assignable)}>
                    {counted('Assign to…', assignable.length)}
                  </Button>
                </span>
                <span title={unassignable.length === 0 ? 'Select assigned items' : undefined}>
                  <Button disabled={unassignable.length === 0} onClick={() => supervisor.open('unassign', unassignable)}>
                    {counted('Unassign', unassignable.length)}
                  </Button>
                </span>
                {moreActions([queueGroup, skipGroup])}
              </SpaceBetween>
            ) : (
              <SpaceBetween direction="horizontal" size="xs">
                <span title={claimable.length === 0 ? 'Select items waiting for review' : undefined}>
                  <Button
                    disabled={claimable.length === 0 || claiming !== null}
                    loading={claiming === 'selected'}
                    onClick={() => void claim(claimable, 'selected')}
                  >
                    {counted('Claim', claimable.length)}
                  </Button>
                </span>
                <Button
                  variant="primary"
                  disabled={claiming !== null && claiming !== 'next'}
                  loading={claiming === 'next'}
                  onClick={() => void claimNext()}
                >
                  Next item
                </Button>
                {canReview && moreActions([skipGroup])}
              </SpaceBetween>
            )
          }
          description={
            isAdmin
              ? intl.formatMessage(messages.adminDescription)
              : 'Open any item to view it. Claim an item waiting for review to work it in Home › Tasks, or choose Next item to claim the next one.'
          }
          footer={
            nextToken ? (
              <Box textAlign="center">
                <Button loading={loadingMore} onClick={() => void loadMore()}>
                  Load more
                </Button>
              </Box>
            ) : undefined
          }
        />
      </SpaceBetween>
      {supervisor.modals}
      <QueueDefinitionModal
        visible={settingsOpen && !!settingsQueue}
        queue={settingsQueue}
        existingIds={[]}
        annotators={annotators ?? []}
        annotatorsLoading={annotators == null}
        onDismiss={() => setSettingsOpen(false)}
        onSaved={(q) => {
          if (q) {
            setSuccessMessage(`Saved the queue ${q.name}.`);
            // Show the saved definition at once (its name in the title and breadcrumb), and
            // keep it through reads that still return the old one.
            savedDefinitions.remember(q);
            setDefinitions((defs) => savedDefinitions.apply(defs));
          }
          setSettingsOpen(false);
          void load();
        }}
      />
      {bulkSkip && (
        <BulkSkipModal
          mode={bulkSkip.mode}
          objectKeys={bulkSkip.keys}
          onDismiss={() => setBulkSkip(null)}
          onJobStarted={(jobId) => {
            setBulkSkip(null);
            setSuccessMessage(intl.formatMessage(messages.skipJobStarted));
            navigate(jobDetailPath(jobId));
          }}
          onFinished={(result) => {
            setBulkSkip(null);
            setBulkResults(result);
            void afterBulkSkip();
          }}
        />
      )}
      {bulkResults && (
        <BulkResultsModal
          header={intl.formatMessage(messages.skipResultsHeader)}
          counts={bulkResults.counts}
          results={bulkResults.results}
          exportName={`skip-all-reviews-${bulkResults.jobId ?? 'results'}`}
          onDismiss={() => setBulkResults(null)}
        />
      )}
    </ContentLayout>
  );
};

export default ReviewQueuePage;
