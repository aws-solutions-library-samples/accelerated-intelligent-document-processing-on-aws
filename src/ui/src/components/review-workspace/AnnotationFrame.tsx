// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The frame of the unified document editor, the same for a production queue item, a
// Test Studio item and a direct edit: the title row with the item's facts, the action
// bar, notices, the task context and the editor. The actions, each shown where it
// applies, are History, Skip (give the item back to its queue), Discard Changes, Save,
// Edit Page Grouping, Processing Report and Complete, the primary action. A context
// may put its own control first. An Annotator viewing an unassigned item sees Claim
// instead of the editing actions. Every disabled action says why on hover.
import React from 'react';
import { Button, Header, SpaceBetween } from '@cloudscape-design/components';

import ReviewWorkspaceLayout from './ReviewWorkspaceLayout';

export type AnnotationAction = 'process' | 'skip' | 'claim' | 'discard' | 'save' | 'regroup';

/** Hover text of Skip while it is available. */
export const SKIP_HINT = 'Give this item back to its queue, unassigned and not finished.';

export interface AnnotationActionsProps {
  /** The action running now; every button is disabled meanwhile. */
  busy: AnnotationAction | null;
  onAction: (action: AnnotationAction) => void;
  /** Offer History (field edits, and review activity where the item has it). */
  onShowHistory?: () => void;
  /** Offer Processing Report for the open section. */
  onShowReport?: () => void;
  /**
   * Offer Claim (an Annotator viewing an unassigned item): assigns the item to them,
   * after which it can be worked. Shown with History and Processing Report only.
   */
  showClaim?: boolean;
  /** Offer Skip: give the item back to its queue, unassigned and not finished. */
  showSkip?: boolean;
  skipDisabledReason?: string;
  showDiscard?: boolean;
  discardDisabledReason?: string;
  /** Offer Save: save the edits without finishing. */
  showSave?: boolean;
  saveDisabledReason?: string;
  /** Offer Edit Page Grouping. */
  showRegroup?: boolean;
  regroupDisabledReason?: string;
  /** Why Complete is unavailable; undefined when it is available. */
  processDisabledReason?: string;
  /** Hover text while Complete is available. */
  processHint?: string;
  /** A control shown first in the bar (Test Studio's Edit | View switch). */
  leading?: React.ReactNode;
}

const AnnotationActions = ({
  busy,
  onAction,
  onShowHistory,
  onShowReport,
  showClaim = false,
  showSkip = false,
  skipDisabledReason,
  showDiscard = false,
  discardDisabledReason,
  showSave = false,
  saveDisabledReason,
  showRegroup = false,
  regroupDisabledReason,
  processDisabledReason,
  processHint,
  leading,
}: AnnotationActionsProps): React.JSX.Element => {
  const working = busy !== null ? 'Working…' : undefined;
  const history = onShowHistory && (
    <span title={working ? undefined : 'The field edits made to this document, and who worked the item'}>
      <Button disabled={!!working} onClick={onShowHistory}>
        History
      </Button>
    </span>
  );
  const report = onShowReport && (
    <Button disabled={!!working} onClick={onShowReport}>
      Processing Report
    </Button>
  );
  if (showClaim) {
    return (
      <SpaceBetween direction="horizontal" size="xs">
        {leading}
        {history}
        {report}
        <span title={working ? undefined : 'Assign this item to you, then work it here or from My Work › Tasks'}>
          <Button variant="primary" disabled={!!working && busy !== 'claim'} loading={busy === 'claim'} onClick={() => onAction('claim')}>
            Claim
          </Button>
        </span>
      </SpaceBetween>
    );
  }
  const skipReason = working ?? skipDisabledReason;
  const discardReason = working ?? discardDisabledReason;
  const saveReason = working ?? saveDisabledReason;
  const regroupReason = working ?? regroupDisabledReason;
  const processReason = working ?? processDisabledReason;
  return (
    <SpaceBetween direction="horizontal" size="xs">
      {leading}
      {history}
      {showSkip && (
        <span title={skipReason ? undefined : SKIP_HINT}>
          <Button disabled={!!skipReason} disabledReason={skipReason} loading={busy === 'skip'} onClick={() => onAction('skip')}>
            Skip
          </Button>
        </span>
      )}
      {showDiscard && (
        <Button disabled={!!discardReason} disabledReason={discardReason} onClick={() => onAction('discard')}>
          Discard Changes
        </Button>
      )}
      {showSave && (
        <Button disabled={!!saveReason} disabledReason={saveReason} loading={busy === 'save'} onClick={() => onAction('save')}>
          Save
        </Button>
      )}
      {showRegroup && (
        <Button iconName="edit" disabled={!!regroupReason} disabledReason={regroupReason} onClick={() => onAction('regroup')}>
          Edit Page Grouping
        </Button>
      )}
      {report}
      <span title={processReason ? undefined : processHint}>
        <Button
          variant="primary"
          disabled={!!processReason}
          disabledReason={processReason}
          loading={busy === 'process'}
          onClick={() => onAction('process')}
        >
          Complete
        </Button>
      </span>
    </SpaceBetween>
  );
};

interface AnnotationFrameProps {
  /** The document's name. */
  title: string;
  /** The full object key, shown under the title; omitted, no subtitle is shown. */
  objectKey?: string;
  /** Under the title instead of the object key (Test Studio: the document and its ground truth). */
  subtitle?: React.ReactNode;
  /** Beside the title: status and kind badge. */
  badges?: React.ReactNode;
  /** Under the title: where the item comes from and who it is assigned to. */
  meta?: React.ReactNode;
  actions: AnnotationActionsProps;
  /** Alerts about the item's state. */
  notices?: React.ReactNode;
  /** The task context (why the item is here, the version transition). */
  context?: React.ReactNode;
  /** Dialogs the actions open (the skip reason, History, Processing Report, page grouping). */
  dialogs?: React.ReactNode;
  /** The editor. */
  children: React.ReactNode;
}

const AnnotationFrame = ({
  title,
  objectKey,
  subtitle,
  badges,
  meta,
  actions,
  notices,
  context,
  dialogs,
  children,
}: AnnotationFrameProps): React.JSX.Element => (
  <ReviewWorkspaceLayout
    header={
      <SpaceBetween size="s">
        <Header
          variant="h1"
          description={subtitle ?? (objectKey ? <span title={objectKey}>{objectKey}</span> : undefined)}
          actions={<AnnotationActions {...actions} />}
        >
          <SpaceBetween direction="horizontal" size="s" alignItems="center">
            <span>{title}</span>
            {badges}
          </SpaceBetween>
        </Header>
        {meta}
        {notices}
      </SpaceBetween>
    }
    context={context}
  >
    {children}
    {dialogs}
  </ReviewWorkspaceLayout>
);

export default AnnotationFrame;
