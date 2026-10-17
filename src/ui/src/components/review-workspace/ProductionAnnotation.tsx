// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The annotation screen for a production review item: the shared SectionEditor over
// the item's sections, edits autosaved as drafts, then Complete, Reject or Skip (give
// the item back to its queue; the draft is kept). The assignee edits; an Admin may
// too. Any Annotator listed on the item's queue can open it read-only, and Claim an
// unassigned item to work it.
import React, { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Alert, Box, Button, Container, Link, SpaceBetween, Spinner, StatusIndicator } from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import { useOptionalReviewWork } from '../../hooks/use-review-work';
import useUserRole from '../../hooks/use-user-role';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { MY_QUEUES_PATH, productionQueuePath, reviewItemHref } from '../../routes/constants';
import { QueueItemState, reviewItemStateView } from '../common/review-status';
import FavoriteButton from '../review-queues/FavoriteButton';
import RejectModal from '../review-queues/RejectModal';
import ReviewPresence from '../review-queues/ReviewPresence';
import ReviewTaskContext from '../review-queues/ReviewTaskContext';
import { claimItem, documentName, releaseItem, reviewErrorCode } from '../review-queues/review-api';
import AnnotationFrame from './AnnotationFrame';
import type { AnnotationAction } from './AnnotationFrame';
import SectionEditor from './SectionEditor';
import { AUTOSAVE_MS, useProductionAnnotation } from './production-adapter';
import useAnnotationExit from './use-annotation-exit';

const ProductionAnnotation = ({ objectKey }: { objectKey: string }): React.JSX.Element => {
  const adapter = useProductionAnnotation(objectKey);
  const { detail, loadError, reload } = adapter;
  const { setErrorMessage, setSuccessMessage } = useAppContext();
  const { isAdmin, isAnnotator } = useUserRole();
  const reviewWork = useOptionalReviewWork();
  const exit = useAnnotationExit();
  const [searchParams] = useSearchParams();
  const focusField = searchParams.get('field');
  const [checklist, setChecklist] = useState<boolean[]>([]);
  const [busy, setBusy] = useState<AnnotationAction | null>(null);
  const [rejecting, setRejecting] = useState(false);
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    if (detail && checklist.length !== detail.checklist.length) setChecklist(detail.checklist.map(() => false));
  }, [detail, checklist.length]);

  const buildFieldLink = useCallback(
    (fieldPath: string) => `${window.location.origin}/${reviewItemHref(objectKey)}?field=${encodeURIComponent(fieldPath)}`,
    [objectKey],
  );

  if (loadError && !detail) {
    const code = reviewErrorCode(loadError);
    const notAssigned = code === 'NOT_ASSIGNED' || code === 'NOT_A_MEMBER';
    return (
      <Alert
        type={notAssigned ? 'warning' : 'error'}
        header={notAssigned ? "You can't open this review item" : "Couldn't open this review item"}
        action={!notAssigned && <Button onClick={() => void reload()}>Retry</Button>}
      >
        {notAssigned ? (
          <>
            Only the Annotators listed on this item&apos;s queue can open it. The queues that list you are in{' '}
            <Link href={`#${MY_QUEUES_PATH}`}>My Work › Queues</Link>.
          </>
        ) : (
          describeApiError(loadError, 'load the review item')
        )}
      </Alert>
    );
  }
  if (!detail) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" /> Loading the review item…
      </Box>
    );
  }

  const { item } = detail;
  const name = documentName(item.objectKey);
  const stateView = reviewItemStateView(item.state);
  const assigned = item.state === QueueItemState.CLAIMED;
  const terminal = !assigned && item.state !== QueueItemState.OPEN;
  const assignee = assigned ? item.ownerEmail : null;
  const assignedToMe = assigned && item.isMine;
  const canEdit = !terminal && (assignedToMe || isAdmin);
  // An Annotator (not an Admin, who assigns from the queue) claims an item waiting for review.
  const canClaim = isAnnotator && !isAdmin && item.state === QueueItemState.OPEN;

  let lockedReason: string | undefined;
  if (terminal) lockedReason = `This item is ${stateView.label.toLowerCase()}`;
  else if (!canEdit) lockedReason = assignee ? `Assigned to ${assignee}` : 'This item is not assigned to you';
  else if (dirty) lockedReason = 'Saving your latest edits…';

  const unticked = detail.checklist.filter((_, i) => !checklist[i]);

  const run = async (action: AnnotationAction, fn: () => Promise<void>, failure: string): Promise<boolean> => {
    setBusy(action);
    try {
      await fn();
      return true;
    } catch (err) {
      setErrorMessage(describeApiError(err, failure));
      void reload();
      return false;
    } finally {
      setBusy(null);
    }
  };

  const queuePath = productionQueuePath(item.queue);

  const handleAction = async (action: AnnotationAction) => {
    if (action === 'reject') {
      setRejecting(true);
      return;
    }
    if (action === 'claim') {
      reviewWork?.beginMutation();
      const ok = await run(
        'claim',
        async () => {
          await claimItem(item);
        },
        'claim the item',
      );
      if (ok) {
        setSuccessMessage(`Claimed ${name}. It is in My Work › Tasks.`);
        await reload();
      }
      void reviewWork?.refresh();
      return;
    }
    if (action === 'skip') {
      const ok = await run(
        'skip',
        async () => {
          await releaseItem(item);
        },
        'skip the item',
      );
      if (ok) exit(queuePath, `${name} returned to the queue.`, `p:${objectKey}`);
      return;
    }
    const ok = await run('complete', () => adapter.complete(detail.checklist.length ? checklist : undefined), 'complete the review');
    if (ok) exit(queuePath, `Completed ${name}. Your corrections are being applied.`, `p:${objectKey}`);
  };

  const confirmReject = async (reason: string, note: string | undefined) => {
    const ok = await run('reject', () => adapter.reject(reason, note), 'reject the document');
    if (ok) {
      setRejecting(false);
      exit(queuePath, `Rejected ${name}.`, `p:${objectKey}`);
    }
  };

  const notices = (
    <>
      {terminal && (
        <Alert type="info" header="This review is finished">
          {item.state === 'FINALIZING'
            ? 'The review was submitted and its corrections are being applied.'
            : `This item is ${stateView.label.toLowerCase()}. It can no longer be edited.`}
        </Alert>
      )}
      {!terminal && !assignedToMe && isAdmin && (
        <Alert type="info">
          {assignee
            ? `Assigned to ${assignee}. As an Admin you can edit and complete it too.`
            : 'Not assigned to an Annotator yet. As an Admin you can edit and complete it, or assign it from its queue.'}
        </Alert>
      )}
      {!terminal && !canEdit && (
        <Alert type="info">
          {assignee
            ? `View only. This item is assigned to ${assignee}.`
            : canClaim
              ? 'View only. Claim this item to work on it; it is then assigned to you and shows in My Work › Tasks.'
              : 'View only.'}
        </Alert>
      )}
    </>
  );

  return (
    <>
      <AnnotationFrame
        title={name}
        objectKey={item.objectKey}
        badges={
          <>
            <StatusIndicator type={stateView.type}>{stateView.label}</StatusIndicator>
            <FavoriteButton
              favorite={{
                id: `review-item:${item.objectKey}`,
                kind: 'review-item',
                name,
                href: reviewItemHref(item.objectKey),
                context: `Queue ${item.queue}`,
              }}
            />
          </>
        }
        meta={
          <SpaceBetween direction="horizontal" size="l" alignItems="center">
            <Box variant="small" color="text-body-secondary">{`Queue ${item.queue} · ${item.profile} · priority ${item.priority}`}</Box>
            <ReviewPresence assignee={assignee} isMe={assignedToMe} since={item.claimedAt} />
          </SpaceBetween>
        }
        actions={{
          completeDisabledReason: lockedReason,
          completeHint: unticked.length > 0 ? `Not yet ticked: ${unticked.join('; ')}` : 'Apply your corrections and finish the review',
          showReject: true,
          rejectDisabledReason: lockedReason ?? (detail.rejectReasons.length === 0 ? 'This queue defines no reject reasons' : undefined),
          // Skip gives the item back to its queue: only its holder can.
          showSkip: assignedToMe,
          skipDisabledReason: dirty ? 'Saving your latest edits…' : undefined,
          showClaim: canClaim,
          busy,
          onAction: (action) => void handleAction(action),
        }}
        notices={notices}
        context={<ReviewTaskContext detail={detail} checklist={checklist} onChecklistChange={setChecklist} disabled={!canEdit} />}
      >
        {adapter.sections.length === 0 ? (
          <Container>
            <Box color="text-body-secondary">This item has no sections to review.</Box>
          </Container>
        ) : (
          <SectionEditor
            key={objectKey}
            objectKey={objectKey}
            sections={adapter.sections}
            pages={adapter.pages}
            pagesLoading={adapter.pagesLoading}
            isReadOnly={!canEdit}
            loadSection={adapter.loadSection}
            onSave={adapter.saveSection}
            onDirtyChange={setDirty}
            reloadToken={adapter.reloadToken}
            autosaveMs={canEdit ? AUTOSAVE_MS : undefined}
            confirmSectionSwitch={false}
            focusFieldPath={focusField}
            buildFieldLink={buildFieldLink}
            saveButtonText="Save draft"
            title="Extracted fields"
          />
        )}
      </AnnotationFrame>
      <RejectModal
        visible={rejecting}
        reasons={detail.rejectReasons}
        loading={busy === 'reject'}
        onDismiss={() => setRejecting(false)}
        onConfirm={(reason, note) => void confirmReject(reason, note)}
      />
    </>
  );
};

export default ProductionAnnotation;
