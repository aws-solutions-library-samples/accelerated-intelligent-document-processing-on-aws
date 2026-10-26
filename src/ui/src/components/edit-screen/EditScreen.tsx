// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit screen, the same for a direct edit of a document, a production queue item
// and a Test Studio item: one action bar, the title "<document> (<status>)", the
// object key under it, the notices, the task context (a queue item's Review Reason)
// and the Document Editor panel.
//
// The bar always comes in the order Edit | View (the source document, where it can be
// shown), History, Edit Page Grouping, Skip, Cancel, Save and Reprocess, each shown
// only where it applies. An Annotator viewing an unassigned item sees the view switch,
// History and Claim instead. Every disabled action says why on
// hover. The mode components decide which actions apply; the layout is decided here.
import React from 'react';
import { Box, Button, Container, Header, SegmentedControl, SpaceBetween } from '@cloudscape-design/components';

import EditScreenLayout from './EditScreenLayout';
import type { EditView } from './use-edit-view';

export type EditAction = 'reprocess' | 'skip' | 'claim' | 'cancel' | 'save' | 'regroup';

/** Hover text of Skip while it is available. */
export const SKIP_HINT = 'Give this item back to its queue, unassigned and not finished.';

export interface EditActionsProps {
  /** The action running now; every button is disabled meanwhile. */
  busy: EditAction | null;
  onAction: (action: EditAction) => void;
  /** Offer History (field edits, review activity and the processing report). */
  onShowHistory?: () => void;
  /**
   * Offer Claim (an Annotator viewing an unassigned item): assigns the item to them,
   * after which it can be worked. Shown with History only.
   */
  showClaim?: boolean;
  /** Offer Edit Page Grouping. */
  showRegroup?: boolean;
  regroupDisabledReason?: string;
  /** Offer Skip: give the item back to its queue, unassigned and not finished. */
  showSkip?: boolean;
  skipDisabledReason?: string;
  /** Offer Cancel: discard the unsaved edits and leave the screen. */
  showCancel?: boolean;
  cancelDisabledReason?: string;
  /** Offer Save: save the edits without finishing. */
  showSave?: boolean;
  saveDisabledReason?: string;
  /** Why the primary action is unavailable; undefined when it is available. */
  primaryDisabledReason?: string;
  /** Hover text while the primary action is available. */
  primaryHint?: string;
  /**
   * Offer the Edit | View switch (the source document), first in the bar: `view` is the one
   * shown. Omitted where the source document cannot be shown.
   */
  view?: EditView;
  onViewChange?: (view: EditView) => void;
}

const EditActions = ({
  busy,
  onAction,
  onShowHistory,
  showClaim = false,
  showRegroup = false,
  regroupDisabledReason,
  showSkip = false,
  skipDisabledReason,
  showCancel = false,
  cancelDisabledReason,
  showSave = false,
  saveDisabledReason,
  primaryDisabledReason,
  primaryHint,
  view,
  onViewChange,
}: EditActionsProps): React.JSX.Element => {
  const working = busy !== null ? 'Working…' : undefined;
  const history = onShowHistory && (
    <span title={working ? undefined : 'The field edits made to this document, who worked the item, and the processing report'}>
      <Button disabled={!!working} onClick={onShowHistory}>
        History
      </Button>
    </span>
  );
  const viewSwitch = view && onViewChange && (
    <SegmentedControl
      label="Edit the document or view its source"
      selectedId={view}
      onChange={({ detail }) => onViewChange(detail.selectedId as EditView)}
      options={[
        { id: 'edit', text: 'Edit' },
        { id: 'source', text: 'View' },
      ]}
    />
  );
  if (showClaim) {
    return (
      <SpaceBetween direction="horizontal" size="xs">
        {viewSwitch}
        {history}
        <span title={working ? undefined : 'Assign this item to you, then work it here or from Home › Tasks'}>
          <Button variant="primary" disabled={!!working && busy !== 'claim'} loading={busy === 'claim'} onClick={() => onAction('claim')}>
            Claim
          </Button>
        </span>
      </SpaceBetween>
    );
  }
  const regroupReason = working ?? regroupDisabledReason;
  const skipReason = working ?? skipDisabledReason;
  const cancelReason = working ?? cancelDisabledReason;
  const saveReason = working ?? saveDisabledReason;
  const primaryReason = working ?? primaryDisabledReason;
  return (
    <SpaceBetween direction="horizontal" size="xs">
      {viewSwitch}
      {history}
      {showRegroup && (
        <Button iconName="edit" disabled={!!regroupReason} disabledReason={regroupReason} onClick={() => onAction('regroup')}>
          Edit Page Grouping
        </Button>
      )}
      {showSkip && (
        <span title={skipReason ? undefined : SKIP_HINT}>
          <Button disabled={!!skipReason} disabledReason={skipReason} loading={busy === 'skip'} onClick={() => onAction('skip')}>
            Skip
          </Button>
        </span>
      )}
      {showCancel && (
        <span title={cancelReason ? undefined : 'Discard your unsaved changes and leave'}>
          <Button disabled={!!cancelReason} disabledReason={cancelReason} onClick={() => onAction('cancel')}>
            Cancel
          </Button>
        </span>
      )}
      {showSave && (
        <Button disabled={!!saveReason} disabledReason={saveReason} loading={busy === 'save'} onClick={() => onAction('save')}>
          Save
        </Button>
      )}
      <span title={primaryReason ? undefined : primaryHint}>
        <Button
          variant="primary"
          disabled={!!primaryReason}
          disabledReason={primaryReason}
          loading={busy === 'reprocess'}
          onClick={() => onAction('reprocess')}
        >
          Reprocess
        </Button>
      </span>
    </SpaceBetween>
  );
};

/**
 * The Document Editor panel when there is nothing to edit, titled as it is otherwise:
 * `body` (the source document) when it is being viewed, or the message.
 */
export const EditDocumentEmpty = ({ children, body }: { children: React.ReactNode; body?: React.ReactNode }): React.JSX.Element => (
  <Container header={<Header variant="h2">Document Editor</Header>}>{body ?? <Box color="text-body-secondary">{children}</Box>}</Container>
);

interface EditScreenProps {
  /** The document's name. */
  title: string;
  /** Shown after the title in parentheses, as text: the item's or the document's status. */
  status?: string | null;
  /** The full object key, shown under the title. */
  objectKey: string;
  actions: EditActionsProps;
  /** Alerts about the item's state (one EditorNotices). */
  notices?: React.ReactNode;
  /** The task context: a queue item's Review Reason. */
  context?: React.ReactNode;
  /** Dialogs the actions open (the skip reason, History, page grouping). */
  dialogs?: React.ReactNode;
  /** The Document Editor panel. */
  children: React.ReactNode;
}

const EditScreen = ({ title, status, objectKey, actions, notices, context, dialogs, children }: EditScreenProps): React.JSX.Element => (
  <EditScreenLayout
    header={
      <SpaceBetween size="s">
        <Header variant="h1" description={<span title={objectKey}>{objectKey}</span>} actions={<EditActions {...actions} />}>
          {title}
          {status && (
            <>
              {' '}
              {/* The status reads as the subtitle does (font, size and colour), not as the title. */}
              <Box variant="span" fontSize="body-m" fontWeight="normal" color="text-body-secondary">
                ({status})
              </Box>
            </>
          )}
        </Header>
        {notices}
      </SpaceBetween>
    }
    context={context}
  >
    {children}
    {dialogs}
  </EditScreenLayout>
);

export default EditScreen;
