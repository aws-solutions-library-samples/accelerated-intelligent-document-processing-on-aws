// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// HITL › Queue Management › one production queue (Admin): its progress and every
// item in it, with the Annotator each one is assigned to. Select items to assign,
// unassign or move several at once, or use ⋮ on a row. Annotate opens an item's
// annotation screen without changing its assignment.
import React, { useCallback, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import { ConsoleLogger } from 'aws-amplify/utils';
import { Alert, Box, Button, Container, ContentLayout, Header, KeyValuePairs, SpaceBetween } from '@cloudscape-design/components';

import type { ReviewQueueDefinition, ReviewQueueHealth } from '../../graphql/generated/operation-types';
import useAppContext from '../../contexts/app';
import usePolling from '../../hooks/use-polling';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { formatDateTime, formatRelativeTime } from '../../utils/date-time';
import WorkItemsTable from './WorkItemsTable';
import useSupervisorActions, { isAssignable, isSupervisorAction, isUnassignable } from './use-supervisor-actions';
import useWorkItemActions from './use-work-item-actions';
import { fetchQueueItems, fetchQueuesOverview } from './review-api';
import QueueDefinitionModal from './QueueDefinitionModal';
import { listAnnotatorEmails, listReviewQueues } from './reviewer-assignment';
import { fromReviewItem } from './work-items';
import type { WorkItem } from './work-items';

const logger = new ConsoleLogger('ReviewQueuePage');
const POLL_MS = 20_000;
const PAGE = 100;

const ReviewQueuePage = (): React.JSX.Element => {
  const { queue: rawQueue = '' } = useParams();
  const queueId = decodeURIComponent(rawQueue);
  const { setErrorMessage, setSuccessMessage } = useAppContext();
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

  const load = useCallback(async () => {
    try {
      const [page, defs, ov] = await Promise.all([
        fetchQueueItems({ queueName: queueId, claimable: false, limit: PAGE }),
        listReviewQueues(),
        fetchQueuesOverview().catch((err: unknown) => {
          logger.warn('getReviewQueuesOverview failed', err);
          return null;
        }),
      ]);
      setDefinitions(defs);
      setHealth(ov?.queues.find((q) => q.summary.name === queueId) ?? null);
      const loaded = page.items.map(fromReviewItem);
      setItems(loaded);
      setSelected((prev) => prev.filter((s) => loaded.some((i) => i.id === s.id)));
      setNextToken(page.nextToken ?? null);
      setError(null);
    } catch (err) {
      logger.error('queue load failed', err);
      setError(err);
    }
  }, [queueId]);

  usePolling(load, { enabled: true, intervalMs: POLL_MS, immediate: true });

  const definition = useMemo(() => definitions.find((d) => d.id === queueId) ?? null, [definitions, queueId]);
  const summary = health?.summary ?? null;
  const { run: runRowAction } = useWorkItemActions();
  const supervisor = useSupervisorActions({ queues: definitions, onChanged: load });

  const refreshNow = async () => {
    setRefreshing(true);
    try {
      await load();
    } finally {
      setRefreshing(false);
    }
  };

  const handleRowAction = (item: WorkItem, action: string) => {
    if (isSupervisorAction(action)) {
      if (item.reviewItem) supervisor.open(action, [item.reviewItem]);
      return;
    }
    runRowAction(item, action);
  };

  const selectedItems = selected.flatMap((s) => (s.reviewItem ? [s.reviewItem] : []));
  const assignable = selectedItems.filter(isAssignable);
  const unassignable = selectedItems.filter(isUnassignable);
  const counted = (label: string, n: number) => (n > 0 ? `${label} (${n})` : label);

  const loadMore = async () => {
    if (!nextToken) return;
    setLoadingMore(true);
    try {
      const page = await fetchQueueItems({ queueName: queueId, claimable: false, limit: PAGE, nextToken });
      setItems((prev) => [...(prev ?? []), ...page.items.map(fromReviewItem)]);
      setNextToken(page.nextToken ?? null);
    } catch (err) {
      setErrorMessage(describeApiError(err, 'load more items'));
    } finally {
      setLoadingMore(false);
    }
  };

  const title = definition?.name || summary?.displayName || queueId;
  const eligible = definition?.eligibleAnnotators ?? [];

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
          description={definition?.description || 'Production review queue.'}
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button iconName="settings" disabled={!definition} onClick={() => void openSettings()}>
                Settings
              </Button>
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
        <Container header={<Header variant="h2">Queue Progress</Header>}>
          {summary || definition ? (
            <KeyValuePairs
              columns={4}
              items={[
                { label: 'Pending', value: summary?.unclaimed ?? '—' },
                {
                  label: 'Assigned',
                  value: summary ? Math.max(0, summary.claimed - (summary.inProgress ?? 0)) : '—',
                },
                { label: 'In progress', value: summary?.inProgress ?? '—' },
                { label: 'Annotators', value: summary?.annotators ?? '—' },
                {
                  label: 'Oldest waiting',
                  value: summary?.oldestEnqueuedAt ? (
                    <span title={formatDateTime(summary.oldestEnqueuedAt)}>{formatRelativeTime(summary.oldestEnqueuedAt)}</span>
                  ) : (
                    '—'
                  ),
                },
                { label: 'Priority', value: definition?.priority ?? summary?.priority ?? '—' },
                { label: 'Time limit', value: definition ? `${definition.timeLimitMinutes} min (then a reminder)` : '—' },
                { label: 'Eligible Annotators', value: eligible.length === 0 ? 'Any Annotator' : eligible.join(', ') },
                {
                  label: 'Assigned per Annotator',
                  value:
                    !health || health.reviewers.length === 0 ? (
                      '—'
                    ) : (
                      <SpaceBetween size="xxxs">
                        {health.reviewers.map((r) => (
                          <Box key={r.email} variant="small">{`${r.email}: ${r.claimed}`}</Box>
                        ))}
                      </SpaceBetween>
                    ),
                },
              ]}
            />
          ) : (
            <Box color="text-body-secondary">{items ? 'This queue is not defined in Queue Management.' : 'Loading…'}</Box>
          )}
        </Container>
        <WorkItemsTable
          items={items ?? []}
          loading={(items == null && error == null) || (refreshing && (items ?? []).length === 0)}
          title="Items"
          onRowAction={handleRowAction}
          extraRowActions={(item) => supervisor.menuItems(item.reviewItem)}
          showStatusFilter
          selectionType="multi"
          selectedItems={selected}
          onSelectionChange={setSelected}
          actions={
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
              <span title={selectedItems.length === 0 ? 'Select items to move' : undefined}>
                <Button disabled={selectedItems.length === 0} onClick={() => supervisor.open('move', selectedItems)}>
                  {counted('Move…', selectedItems.length)}
                </Button>
              </span>
            </SpaceBetween>
          }
          description="Select items to assign them to an Annotator, unassign them or move them to another queue. Annotate opens an item without changing its assignment."
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
        visible={settingsOpen && !!definition}
        queue={definition}
        existingIds={[]}
        annotators={annotators ?? []}
        annotatorsLoading={annotators == null}
        onDismiss={() => setSettingsOpen(false)}
        onSaved={(q) => {
          if (q) setSuccessMessage(`Saved the queue ${q.name}.`);
          setSettingsOpen(false);
          void load();
        }}
      />
    </ContentLayout>
  );
};

export default ReviewQueuePage;
