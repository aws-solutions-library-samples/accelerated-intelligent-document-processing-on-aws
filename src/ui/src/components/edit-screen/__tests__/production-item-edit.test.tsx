// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The edit screen on a production item, by role. What has to hold: an Admin on an
 * unassigned item can edit it and use Reprocess, and the first draft save or Reprocess
 * claims it once (concurrent saves share the one claim) before the save or complete
 * is sent; if an Annotator got there first the screen says so and goes view-only; an
 * Admin on someone else's item sees it view-only with Take over, which calls
 * takeOverReviewItem; Process Changes asks how the review ends (complete it, or send the
 * document back, which discards the corrections and any page regrouping), shows what the
 * server says each ending runs, and asks for no step; Reject asks for
 * a reason from the queue's list; both are for whoever may finish the review; Skip is
 * offered only to the assignee; and the screen gives the breadcrumbs the item's queue. The bar adds Save
 * and Discard Changes (through the editor's controller), Processing Report, the Policy
 * Rule Validation Summary (available during the review), and an Edit Page Grouping for the
 * item's holder that a BDA-mode document does not offer; History has the item's
 * review activity; and there is no "Queue · profile · priority" meta line.
 */

import React, { useCallback, useState } from 'react';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { renderWithIntl as render } from '../../../test-utils/renderWithIntl';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { ReviewItem, ReviewItemDetail } from '../../../graphql/generated/operation-types';

// ---------------------------------------------------------------- the server
const server: { detail: ReviewItemDetail | null; ruleValidationResultUri: string | null } = { detail: null, ruleValidationResultUri: null };
const saveSection = vi.fn();
const complete = vi.fn();
const claimItem = vi.fn();
const releaseItem = vi.fn();
const takeOverItem = vi.fn();
const rejectItem = vi.fn();
const sendBackItem = vi.fn();
const getReviewCompletionPlan = vi.fn();
const setErrorMessage = vi.fn();
const setSuccessMessage = vi.fn();
const exit = vi.fn();
const refreshDocument = vi.fn();
const saveDocumentGrouping = vi.fn();
const role = {
  isAdmin: true,
  isAnnotator: false,
  isReviewer: false,
  canWrite: true,
  // As useUserRole derives it: an Annotator or a Reviewer works Production queues.
  get isQueueWorker() {
    return this.isAnnotator || this.isReviewer;
  },
};
/** The props the editor was last rendered with. */
let editor: {
  isReadOnly: boolean;
  onSave: (id: string, data: Record<string, unknown>) => Promise<unknown>;
  controllerRef?: { current: unknown };
  body?: unknown;
  defaultFilter?: string;
  focusRequest?: unknown;
} | null = null;
const controller = { save: vi.fn(), discard: vi.fn(), isDirty: vi.fn() };

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../production-item-edit-adapter', () => ({
  AUTOSAVE_MS: 2000,
  useProductionItemEdit: () => {
    const [detail, setDetail] = useState<ReviewItemDetail | null>(server.detail);
    const reload = useCallback(async () => {
      setDetail(server.detail);
      return server.detail;
    }, []);
    return {
      detail,
      loadError: null,
      reload,
      pages: [],
      pagesLoading: false,
      sections: [{ sectionId: '1', documentClass: 'W2', resultUri: 's3://b/r', draftUri: null, pageIds: [] }],
      // The whole document: the section under review and one that isn't.
      documentSections: [
        { sectionId: '1', documentClass: 'W2', resultUri: 's3://b/r', pageIds: ['1'] },
        { sectionId: '2', documentClass: 'W2', resultUri: 's3://b/r2', pageIds: ['2'] },
      ],
      refreshDocument: (...a: unknown[]) => refreshDocument(...a),
      summaryReportUri: 's3://out/in/w2.pdf/summary/summary.md',
      ruleValidationResultUri: server.ruleValidationResultUri,
      evaluationStatus: null,
      reloadToken: 0,
      loadSection: vi.fn(),
      saveSection: (...a: unknown[]) => saveSection(...a),
      complete: (...a: unknown[]) => complete(...a),
    };
  },
}));
/** The breadcrumbs the page gives its layout. */
let layoutTrail: unknown = null;
/** Every breadcrumb root the page gave, in order, and the navigation entry it highlights. */
const layoutTrails: unknown[] = [];
let layoutActiveHref: unknown;
const ADMIN_TRAIL = [
  { text: 'Review Studio', href: '#/review-queues' },
  { text: 'Review Management', href: '#/review-queues' },
  { text: 'Default', href: '#/review-queues/queues/default' },
  { text: 'w2.pdf', href: '#' },
];
vi.mock('../../review-queues/ReviewQueuesLayout', () => ({
  default: ({ children, trail, navActiveHref }: { children: React.ReactNode; trail?: unknown; navActiveHref?: unknown }) => {
    layoutTrail = trail;
    layoutTrails.push(trail);
    layoutActiveHref = navActiveHref;
    return <>{children}</>;
  },
}));
vi.mock('../SectionEditor', () => ({
  default: (props: NonNullable<typeof editor>) => {
    editor = props;
    if (props.controllerRef) props.controllerRef.current = controller;
    const regroup = (props as unknown as Record<string, unknown>).regroup as { disabledReason?: string; onClick: () => void } | undefined;
    const regroupButton = regroup && (
      <button
        data-testid="editor-regroup"
        disabled={!!regroup.disabledReason}
        data-reason={regroup.disabledReason}
        onClick={regroup.onClick}
      >
        Edit Page Grouping
      </button>
    );
    return (
      <div data-testid="editor" data-readonly={String(props.isReadOnly)}>
        {regroupButton}
      </div>
    );
  },
}));
vi.mock('../../review-queues/review-api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../review-queues/review-api')>()),
  claimItem: (...a: unknown[]) => claimItem(...a),
  releaseItem: (...a: unknown[]) => releaseItem(...a),
  takeOverItem: (...a: unknown[]) => takeOverItem(...a),
  rejectItem: (...a: unknown[]) => rejectItem(...a),
  sendBackItem: (...a: unknown[]) => sendBackItem(...a),
}));
// What each ending of the review runs, as the server answers it.
vi.mock('../../run-plan/run-plan-api', () => ({
  getReviewCompletionPlan: (...a: unknown[]) => getReviewCompletionPlan(...a),
}));
// The report dialog's content reads the report from the server; here it shows what it was given.
vi.mock('../../document-viewer/MarkdownViewer', () => ({
  MarkdownReport: ({ reportUri, documentId }: { reportUri: string; documentId: string }) => (
    <div data-testid="markdown-report">{[documentId, reportUri].join('|')}</div>
  ),
}));
/** The props the Review Reason panel was last rendered with. */
let contextProps: { onReasonClick?: (r: { sectionId: string; field?: string | null }) => void } | null = null;
vi.mock('../../review-queues/ReviewTaskContext', () => ({
  default: (props: typeof contextProps) => {
    contextProps = props;
    return null;
  },
}));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => role }));
vi.mock('../../../hooks/use-review-work', () => ({ useOptionalReviewWork: () => null }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ setErrorMessage, setSuccessMessage }) }));
vi.mock('../use-edit-exit', () => ({ default: () => exit, useLeavePath: () => (queuePath: string) => queuePath }));
vi.mock('../../../hooks/use-page-thumbnails', () => ({ default: () => ({ urls: {}, failure: null }) }));
// The model saves through saveDocumentGroupingOrThrow, which reads saveDocumentGrouping's
// result the same way: a refusal throws with the server's message, a transport failure
// with the model's own wording.
vi.mock('../../common/document-grouping', () => ({
  saveDocumentGrouping: (...a: unknown[]) => saveDocumentGrouping(...a),
  saveDocumentGroupingOrThrow: async (objectKey: string, sections: unknown, describeError: (err: unknown) => string) => {
    let result: { success: boolean; message: string | null };
    try {
      result = await saveDocumentGrouping(objectKey, sections);
    } catch (err) {
      throw new Error(describeError(err));
    }
    if (!result.success) throw new Error(result.message ?? 'The page grouping could not be saved.');
    return result.message;
  },
}));
/** The grouping board's props, while it is open. */
let board: { sections: { sectionId: string }[]; consequence?: React.ReactNode; onSave: (next: unknown[]) => Promise<void> } | null = null;
vi.mock('../../common/PageGroupingEditor', () => ({
  default: (props: NonNullable<typeof board>) => {
    board = props;
    return <div data-testid="grouping-board">{props.consequence}</div>;
  },
}));

import ProductionItemEdit from '../ProductionItemEdit';
import type { ReviewCompletionPlan } from '../../../graphql/generated/operation-types';
import { BDA_REGROUP_REASON } from '../document-edit-status';
import { CLAIM_TO_REGROUP_REASON } from '../production-item-edit-model';
import { barButtonLabels, chooseMenuItem, menuItems, menuNames } from './edit-bar';
import { chooseRegroup, regroupItem } from './regroup-stand-in';

const ADMIN = 'admin@example.com';
const ANN = 'ann@example.com';

const detailFor = (over: Partial<ReviewItem>): ReviewItemDetail =>
  ({
    item: {
      objectKey: 'in/w2.pdf',
      cycleId: 'c1',
      profile: 'default',
      queue: 'default',
      priority: 50,
      state: 'OPEN',
      ownerEmail: null,
      isMine: false,
      claimedAt: null,
      ...over,
    },
    queueName: 'Default',
    sections: [],
    checklist: [],
    rejectReasons: ['Illegible'],
    claimCap: 10,
    myClaimCount: 0,
    history: [],
    canClaim: true,
  }) as unknown as ReviewItemDetail;

const mine = () => detailFor({ state: 'CLAIMED', ownerEmail: ADMIN, isMine: true });
const annotators = () => detailFor({ state: 'CLAIMED', ownerEmail: ANN, isMine: false });

const renderScreen = (path = '/documents/in%2Fw2.pdf/edit') =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <ProductionItemEdit objectKey="in/w2.pdf" />
    </MemoryRouter>,
  );

/** A button's label, without a disabled button's reason (rendered for screen readers). */
const label = (b: ReturnType<ReturnType<typeof createWrapper>['findAllButtons']>[number]) =>
  b.findTextRegion()?.getElement().textContent ?? b.getElement().textContent;
const buttons = () => createWrapper(document.body).findAllButtons().map(label);
const button = (text: string) => {
  const b = createWrapper(document.body)
    .findAllButtons()
    .find((w) => label(w) === text);
  if (!b) throw new Error(`no ${text} button`);
  return b;
};
const readOnly = () => screen.getByTestId('editor').getAttribute('data-readonly') === 'true';
/** An open dialog, by its header. */
const openDialog = (header: string) =>
  createWrapper(document.body)
    .findAllModals()
    .find((m) => m.isVisible() && m.findHeader().getElement().textContent === header);
/** The dialog Process Changes opens on a review item. */
const completion = () => openDialog('Finish the review');
const completionButton = (text: string) =>
  completion()!
    .findFooter()!
    .findAllButtons()
    .find((b) => label(b) === text)!;
/** Press Process Changes and wait for the dialog that asks how the review ends, and for what it says each ending runs. */
const openCompletion = async () => {
  fireEvent.click(button('Process Changes').getElement());
  await waitFor(() => expect(completion()).toBeDefined());
  await waitFor(() => expect(completion()!.findContent().findStatusIndicator('[class*="status-loading"]')).toBeNull());
};
/** The steps the dialog lists for the ending chosen, in order: each one's name and what the run does with it. */
const plannedSteps = () =>
  completion()!
    .findContent()
    .findList()!
    .findItems()
    .map((i) => [i.findContent().getElement().textContent, i.findActions()!.getElement().textContent]);

/** What the server says each ending runs: completing applies the corrections; sending back starts at classification. */
const COMPLETION_PLAN: ReviewCompletionPlan = {
  complete: {
    mode: 'pipeline',
    startStep: 'apply_review_changes',
    steps: [
      { id: 'beginning', label: 'Beginning', state: 'skipped' },
      { id: 'classification', label: 'Classification', state: 'skipped' },
      { id: 'extraction', label: 'Extraction', state: 'skipped' },
      { id: 'assessment', label: 'Assessment', state: 'skipped' },
      { id: 'apply_review_changes', label: 'Apply Review Changes', state: 'run' },
      { id: 'policy_rule_validation', label: 'Policy Rule Validation', state: 'run' },
      { id: 'review_routing', label: 'Review Routing', state: 'run' },
      { id: 'summarization', label: 'Summarization', state: 'run' },
      { id: 'evaluation', label: 'Evaluation', state: 'not_enabled' },
    ],
    sections: [],
  },
  sendBack: {
    mode: 'pipeline',
    startStep: 'classification',
    steps: [
      { id: 'beginning', label: 'Beginning', state: 'skipped' },
      { id: 'classification', label: 'Classification', state: 'run' },
      { id: 'extraction', label: 'Extraction', state: 'run' },
      { id: 'assessment', label: 'Assessment', state: 'run' },
      { id: 'policy_rule_validation', label: 'Policy Rule Validation', state: 'run' },
      { id: 'review_routing', label: 'Review Routing', state: 'run' },
      { id: 'summarization', label: 'Summarization', state: 'run' },
      { id: 'evaluation', label: 'Evaluation', state: 'not_enabled' },
    ],
    sections: [],
  },
};

beforeEach(() => {
  document.body.innerHTML = '';
  editor = null;
  role.isAdmin = true;
  role.isAnnotator = false;
  role.isReviewer = false;
  role.canWrite = true;
  for (const f of [
    saveSection,
    complete,
    claimItem,
    releaseItem,
    takeOverItem,
    rejectItem,
    sendBackItem,
    getReviewCompletionPlan,
    setErrorMessage,
    setSuccessMessage,
    exit,
  ])
    f.mockReset();
  controller.save.mockReset().mockResolvedValue(true);
  refreshDocument.mockReset().mockResolvedValue(undefined);
  saveDocumentGrouping.mockReset().mockResolvedValue({ success: true, message: 'Page grouping saved' });
  board = null;
  controller.discard.mockReset();
  controller.isDirty.mockReset().mockReturnValue(false);
  saveSection.mockResolvedValue(undefined);
  complete.mockResolvedValue(undefined);
  getReviewCompletionPlan.mockResolvedValue(COMPLETION_PLAN);
  server.ruleValidationResultUri = null;
  // A claim succeeds: the next read shows the item assigned to the Admin.
  claimItem.mockImplementation(async () => {
    server.detail = mine();
    return server.detail.item;
  });
  takeOverItem.mockImplementation(async () => {
    server.detail = mine();
    return server.detail.item;
  });
  server.detail = detailFor({});
});

describe('finishing a review', () => {
  const chooseSendBack = () => {
    act(() => completion()!.findContent().findRadioGroup()!.findInputByValue('send-back')!.click());
  };

  it('asks whether to complete the review or send the document back, and does nothing on Cancel', async () => {
    server.detail = mine();
    renderScreen();
    await openCompletion();

    const outcomes = completion()!.findContent().findRadioGroup()!;
    // Each option is its label followed by what it does.
    expect(outcomes.findButtons().map((b) => b.findLabel().getElement().textContent)).toEqual([
      'Complete the reviewYour corrections are kept and processing continues with them.',
      'Send back for reprocessingThe document is processed again without this review. It can return to a review queue afterwards.',
    ]);
    // Completing is the default, and it has no warning and no step to choose.
    expect(outcomes.findInputByValue('complete')!.getElement().checked).toBe(true);
    expect(completion()!.findContent().findAlert()).toBeNull();
    expect(completion()!.findContent().findAllRadioGroups()).toHaveLength(1);

    completionButton('Cancel').click();
    await waitFor(() => expect(completion()).toBeUndefined());
    expect(complete).not.toHaveBeenCalled();
    expect(sendBackItem).not.toHaveBeenCalled();
    expect(exit).not.toHaveBeenCalled();
  });

  it('shows what each ending runs, as the server says for this item and review cycle', async () => {
    server.detail = mine();
    renderScreen();
    await openCompletion();

    expect(getReviewCompletionPlan).toHaveBeenCalledTimes(1);
    expect(getReviewCompletionPlan).toHaveBeenCalledWith('in/w2.pdf', 'c1');
    expect(completion()!.findContent().getElement().textContent).toContain('Starts at Apply Review Changes');
    expect(plannedSteps()).toEqual([
      ['OCR (beginning)', 'Skipped'],
      ['Classification', 'Skipped'],
      ['Extraction', 'Skipped'],
      ['Assessment', 'Skipped'],
      ['Apply Review Changes', 'Runs'],
      ['Policy Rule Validation', 'Runs'],
      ['Review Routing', 'Runs'],
      ['Summarization', 'Runs'],
      ['Evaluation', 'Not enabled'],
    ]);

    chooseSendBack();
    expect(completion()!.findContent().getElement().textContent).toContain('Starts at Classification');
    expect(plannedSteps()).toEqual([
      ['OCR (beginning)', 'Skipped'],
      ['Classification', 'Runs'],
      ['Extraction', 'Runs'],
      ['Assessment', 'Runs'],
      ['Policy Rule Validation', 'Runs'],
      ['Review Routing', 'Runs'],
      ['Summarization', 'Runs'],
      ['Evaluation', 'Not enabled'],
    ]);
    // One read serves both endings.
    expect(getReviewCompletionPlan).toHaveBeenCalledTimes(1);
  });

  it('sends the document back with the note and no step, saying the corrections and the regrouping are discarded', async () => {
    server.detail = detailFor({ state: 'CLAIMED', ownerEmail: ADMIN, isMine: true, processingMode: 'pipeline' } as Partial<ReviewItem>);
    sendBackItem.mockResolvedValue(undefined);
    renderScreen();
    await openCompletion();
    chooseSendBack();

    expect(completion()!.findContent().findAlert()!.getElement().textContent).toContain(
      'The corrections and any page regrouping are discarded, and the document is processed again from classification (from the beginning in BDA mode).',
    );
    // The ending is the only choice: the server decides where the run starts.
    expect(completion()!.findContent().findAllRadioGroups()).toHaveLength(1);
    act(() => completion()!.findContent().findTextarea()!.setTextareaValue('  wrong extraction prompt  '));
    completionButton('Send back').click();

    await waitFor(() => expect(sendBackItem).toHaveBeenCalledTimes(1));
    // The item and the note, and nothing else.
    expect(sendBackItem.mock.calls[0]).toHaveLength(2);
    expect(sendBackItem.mock.calls[0][0]).toMatchObject({ objectKey: 'in/w2.pdf', cycleId: 'c1' });
    expect(sendBackItem.mock.calls[0][1]).toBe('wrong extraction prompt');
    // The review is not completed, and its corrections are not sent.
    expect(complete).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(exit).toHaveBeenCalledWith(
        expect.objectContaining({ to: '/review-queues/queues/default', message: 'w2.pdf was sent back and is being processed again.' }),
      ),
    );
  });

  it('sends no note with a send-back unless one is written', async () => {
    server.detail = mine();
    sendBackItem.mockResolvedValue(undefined);
    renderScreen();
    await openCompletion();
    chooseSendBack();
    completionButton('Send back').click();

    await waitFor(() => expect(sendBackItem).toHaveBeenCalledTimes(1));
    expect(sendBackItem.mock.calls[0].slice(1)).toEqual([undefined]);
  });

  it('lets an Admin send an unassigned item back without assigning it to them', async () => {
    sendBackItem.mockResolvedValue(undefined);
    renderScreen();
    await openCompletion();
    chooseSendBack();
    completionButton('Send back').click();

    await waitFor(() => expect(sendBackItem).toHaveBeenCalledTimes(1));
    expect(claimItem).not.toHaveBeenCalled();
  });

  it.each([['pipeline'], ['bda'], [null]])('offers no step to choose, whatever mode the item records (%s)', async (processingMode) => {
    server.detail = detailFor({ state: 'CLAIMED', ownerEmail: ADMIN, isMine: true, processingMode } as Partial<ReviewItem>);
    renderScreen();
    await openCompletion();
    chooseSendBack();

    // The one group of options is the ending; the steps are a list to read.
    expect(completion()!.findContent().findAllRadioGroups()).toHaveLength(1);
    expect(completion()!.findContent().findSelect()).toBeNull();
    expect(plannedSteps().length).toBeGreaterThan(0);
  });

  it('still completes the review when what it runs cannot be read, saying so with a Retry', async () => {
    server.detail = mine();
    getReviewCompletionPlan.mockRejectedValue(new Error('offline'));
    renderScreen();
    await openCompletion();

    const warning = completion()!.findContent().findAlert()!;
    expect(warning.findHeader()!.getElement().textContent).toBe('The steps of the run could not be loaded');
    expect(warning.findActionSlot()!.findButton()!.getElement().textContent).toBe('Retry');
    completionButton('Complete review').click();

    await waitFor(() => expect(complete).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(exit).toHaveBeenCalled());
    // Not being able to read the summary is not a failure of the review.
    expect(setErrorMessage).not.toHaveBeenCalled();
  });

  it('keeps the dialog open with the reason when the server refuses the send-back', async () => {
    const { SendBackRefused } = await vi.importActual<typeof import('../../review-queues/review-api')>('../../review-queues/review-api');
    server.detail = mine();
    sendBackItem.mockRejectedValue(new SendBackRefused('CYCLE_CHANGED', 'the document has a newer review cycle'));
    renderScreen();
    await openCompletion();
    chooseSendBack();
    completionButton('Send back').click();

    await waitFor(() => expect(completion()!.findContent().findAllAlerts().length).toBe(2));
    const error = completion()!
      .findContent()
      .findAllAlerts()
      .find((a) => a.findHeader()?.getElement().textContent === 'The document was not sent back')!;
    expect(error.findContent().getElement().textContent).toBe('Failed to send the document back: the document has a newer review cycle');
    // Shown in the dialog, not as a page notification, and the reviewer stays on the item.
    expect(setErrorMessage).not.toHaveBeenCalled();
    expect(exit).not.toHaveBeenCalled();

    // A second try clears the error as soon as it starts, before the server answers.
    let answer: () => void = () => undefined;
    sendBackItem.mockReturnValue(new Promise<void>((resolve) => (answer = resolve)));
    completionButton('Send back').click();
    await waitFor(() => expect(completion()!.findContent().findAllAlerts().length).toBe(1));
    expect(completion()!.findContent().getElement().textContent).not.toContain('The document was not sent back');
    await act(async () => answer());
    await waitFor(() => expect(exit).toHaveBeenCalled());
  });

  it('is offered to a Reviewer who holds the item, and not to one who does not', async () => {
    role.isAdmin = false;
    role.isReviewer = true;
    server.detail = mine();
    renderScreen();
    expect(buttons()).toEqual(expect.arrayContaining(['Reject', 'Process Changes']));
    expect(button('Process Changes').isDisabled()).toBe(false);

    document.body.innerHTML = '';
    server.detail = annotators();
    renderScreen();
    // Neither ending is within reach: no Reject, and Process Changes cannot open the dialog.
    expect(buttons()).not.toContain('Reject');
    expect(buttons().includes('Process Changes') ? button('Process Changes').isDisabled() : true).toBe(true);
  });
});

describe('an Admin on an unassigned item', () => {
  it('can edit it, and says editing or reprocessing assigns it to them', () => {
    renderScreen();
    expect(readOnly()).toBe(false);
    expect(document.body.textContent).toContain('Not assigned yet. Editing it, or choosing Process Changes, assigns it to you.');
    expect(button('Process Changes').isDisabled()).toBe(false);
  });

  it('claims the item, then completes the review', async () => {
    renderScreen();
    await openCompletion();
    // Nothing is claimed or completed until the review's ending is confirmed.
    expect(claimItem).not.toHaveBeenCalled();
    expect(complete).not.toHaveBeenCalled();
    completionButton('Complete review').click();
    await waitFor(() => expect(complete).toHaveBeenCalled());
    expect(claimItem).toHaveBeenCalledTimes(1);
    expect(claimItem.mock.calls[0][0]).toMatchObject({ objectKey: 'in/w2.pdf', cycleId: 'c1' });
    expect(claimItem.mock.invocationCallOrder[0]).toBeLessThan(complete.mock.invocationCallOrder[0]);
    await waitFor(() => expect(exit).toHaveBeenCalled());
    expect(setErrorMessage).not.toHaveBeenCalled();
  });

  it('claims once on the first edit, however many saves are waiting', async () => {
    let release: () => void = () => undefined;
    claimItem.mockImplementation(
      () =>
        new Promise((resolve) => {
          release = () => {
            server.detail = mine();
            resolve(server.detail.item);
          };
        }),
    );
    renderScreen();
    const first = editor!.onSave('1', { a: 1 });
    const second = editor!.onSave('1', { a: 2 });
    expect(saveSection).not.toHaveBeenCalled();
    await act(async () => {
      release();
      await Promise.all([first, second]);
    });
    expect(claimItem).toHaveBeenCalledTimes(1);
    expect(saveSection).toHaveBeenCalledTimes(2);
    // A later save finds the item already the Admin's and doesn't claim again.
    await act(async () => {
      await editor!.onSave('1', { a: 3 });
    });
    expect(claimItem).toHaveBeenCalledTimes(1);
    expect(saveSection).toHaveBeenCalledTimes(3);
  });

  it('goes view-only, saying who was assigned, when an Annotator got there first', async () => {
    claimItem.mockImplementation(async () => {
      server.detail = annotators();
      throw { errors: [{ message: `ALREADY_CLAIMED: claimed by ${ANN}` }] };
    });
    renderScreen();
    let error: unknown = null;
    await act(async () => {
      await editor!.onSave('1', { a: 1 }).catch((e: unknown) => {
        error = e;
      });
    });
    expect(error).toBeInstanceOf(Error);
    expect(saveSection).not.toHaveBeenCalled();
    await waitFor(() => expect(document.body.textContent).toContain(`Assigned to ${ANN} just now.`));
    expect(readOnly()).toBe(true);
    expect(buttons()).toContain('Take over');
    expect(setErrorMessage).not.toHaveBeenCalled();
  });
});

describe('an Admin on an item assigned to someone else', () => {
  beforeEach(() => {
    server.detail = annotators();
  });

  it('is view-only with Take over, and no Skip', () => {
    renderScreen();
    expect(readOnly()).toBe(true);
    expect(document.body.textContent).toContain(`Assigned to ${ANN}. Take over to edit it and process its changes.`);
    // The reason and the action that unlocks it are one notice.
    const notice = createWrapper(document.body)
      .findAllAlerts()
      .find((a) => a.getElement().textContent?.includes('Take over to edit it'))!;
    expect(notice.findActionSlot()!.findButton()!.getElement().textContent).toBe('Take over');
    expect(button('Process Changes').isDisabled()).toBe(true);
    expect(buttons()).not.toContain('Skip');
  });

  it('takes the item over, after which it can be edited', async () => {
    renderScreen();
    fireEvent.click(button('Take over').getElement());
    await waitFor(() => expect(takeOverItem).toHaveBeenCalledTimes(1));
    expect(takeOverItem.mock.calls[0][0]).toMatchObject({ objectKey: 'in/w2.pdf', cycleId: 'c1' });
    await waitFor(() => expect(readOnly()).toBe(false));
    expect(buttons()).not.toContain('Take over');
    expect(claimItem).not.toHaveBeenCalled();
  });
});

describe('the actions', () => {
  it('offer Reject to whoever may finish the review: the holder, and an Admin on an unassigned item', () => {
    for (const [d, offered] of [
      [detailFor({}), true],
      [mine(), true],
      [annotators(), false],
    ] as const) {
      document.body.innerHTML = '';
      server.detail = d;
      renderScreen();
      expect(buttons().includes('Reject')).toBe(offered);
    }
  });

  it('put Reject between Skip and Cancel', () => {
    server.detail = mine();
    renderScreen();
    expect(barButtonLabels()).toEqual(['Refresh', 'Skip', 'Reject', 'Cancel', 'Save', 'Process Changes']);
  });

  it('ask why before rejecting, and reject with that reason and note', async () => {
    server.detail = { ...mine(), rejectReasons: ['Illegible', 'Wrong document'] } as ReviewItemDetail;
    rejectItem.mockResolvedValue({});
    renderScreen();
    button('Reject').click();
    // Nothing is rejected until a reason is chosen.
    expect(rejectItem).not.toHaveBeenCalled();
    const modal = openDialog('Reject this document')!;
    expect(modal.findContent().getElement().textContent).toContain('your corrections are not applied');
    const select = modal.findContent().findSelect()!;
    select.openDropdown();
    expect(
      select
        .findDropdown()
        .findOptions()
        .map((o) => o.getElement().textContent),
    ).toEqual(['Illegible', 'Wrong document']);
    select.selectOptionByValue('Wrong document');
    modal.findContent().findTextarea()!.setTextareaValue('a W-4, not a W-2');
    modal
      .findFooter()!
      .findAllButtons()
      .find((b) => b.getElement().textContent === 'Reject')!
      .click();
    await waitFor(() => expect(rejectItem).toHaveBeenCalledTimes(1));
    expect(rejectItem.mock.calls[0][0]).toMatchObject({ objectKey: 'in/w2.pdf', cycleId: 'c1' });
    expect(rejectItem.mock.calls[0].slice(1)).toEqual(['Wrong document', 'a W-4, not a W-2']);
    // The holder already has the item: nothing is claimed.
    expect(claimItem).not.toHaveBeenCalled();
    await waitFor(() => expect(exit).toHaveBeenCalledWith(expect.objectContaining({ message: 'w2.pdf was rejected.' })));
  });

  it('assign an unassigned item to the Admin before rejecting it, since only a held item can be rejected', async () => {
    rejectItem.mockResolvedValue({});
    renderScreen();
    button('Reject').click();
    const modal = openDialog('Reject this document')!;
    const select = modal.findContent().findSelect()!;
    select.openDropdown();
    select.selectOptionByValue('Illegible');
    modal.findFooter()!.findAllButtons().at(-1)!.click();
    await waitFor(() => expect(rejectItem).toHaveBeenCalledTimes(1));
    expect(claimItem).toHaveBeenCalledTimes(1);
    expect(claimItem.mock.invocationCallOrder[0]).toBeLessThan(rejectItem.mock.invocationCallOrder[0]);
  });

  it('do not reject an item an Annotator was assigned meanwhile', async () => {
    claimItem.mockImplementation(async () => {
      server.detail = annotators();
      throw { errors: [{ message: 'ALREADY_CLAIMED: the item is claimed' }] };
    });
    renderScreen();
    button('Reject').click();
    const modal = openDialog('Reject this document')!;
    const select = modal.findContent().findSelect()!;
    select.openDropdown();
    select.selectOptionByValue('Illegible');
    modal.findFooter()!.findAllButtons().at(-1)!.click();
    await waitFor(() => expect(claimItem).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(readOnly()).toBe(true));
    expect(rejectItem).not.toHaveBeenCalled();
    expect(exit).not.toHaveBeenCalled();
  });

  it('say why Reject is unavailable on a queue that lists no reject reasons', () => {
    server.detail = { ...mine(), rejectReasons: [] } as ReviewItemDetail;
    renderScreen();
    expect(button('Reject').isDisabled()).toBe(true);
    expect(button('Reject').getElement().textContent).toContain('This queue lists no reject reasons');
  });

  it('report a failed reject and stay on the item', async () => {
    server.detail = mine();
    rejectItem.mockRejectedValue(new Error('boom'));
    renderScreen();
    button('Reject').click();
    const modal = openDialog('Reject this document')!;
    const select = modal.findContent().findSelect()!;
    select.openDropdown();
    select.selectOptionByValue('Illegible');
    modal.findFooter()!.findAllButtons().at(-1)!.click();
    await waitFor(() => expect(setErrorMessage).toHaveBeenCalledWith('Failed to reject the document: boom'));
    expect(exit).not.toHaveBeenCalled();
  });

  it('offer Skip only on an item assigned to me, describing what it does', () => {
    server.detail = mine();
    renderScreen();
    expect(buttons()).toContain('Skip');
    expect(button('Skip').getElement().closest('span[title]')?.getAttribute('title')).toBe(
      'Give this item back to its queue, unassigned and not finished.',
    );
    expect(buttons()).not.toContain('Take over');
    document.body.innerHTML = '';
    server.detail = detailFor({});
    renderScreen();
    expect(buttons()).not.toContain('Skip');
  });

  it('open History, with the review activity, which is not on the screen otherwise', () => {
    server.detail = {
      ...mine(),
      history: [{ at: '2026-10-20T09:00:00.000Z', event: 'claimed', actor: ADMIN, details: null }],
    } as ReviewItemDetail;
    renderScreen();
    expect(menuItems('Reports').map((i) => i.text)).toContain('History');
    expect(createWrapper(document.body).findModal()).toBeNull();
    chooseMenuItem('Reports', 'History');
    const modal = createWrapper(document.body).findModal()!;
    expect(modal.findHeader().getElement().textContent).toBe('History');
    const tabs = createWrapper(document.body).findTabs()!;
    expect(tabs.findTabLinks().map((t) => t.getElement().textContent)).toEqual(['Field Edits', 'Review Activity']);
    tabs.findTabLinkByIndex(2)!.click();
    expect(modal.findContent().getElement().textContent).toContain('Claimed');
  });

  it("offer Edit | View first in the bar, showing the source in the editor's place", async () => {
    server.detail = mine();
    renderScreen();
    const control = createWrapper(document.body).findHeader()!.findActions()!.findSegmentedControl()!;
    expect(control.findSegments().map((s) => s.getElement().textContent)).toEqual(['Edit', 'View']);
    expect(editor!.body).toBeUndefined();
    await act(async () => control.findSegments()[1].click());
    expect(editor!.body).toBeTruthy();
  });

  it('move the editor to a review reason’s field when its chip or Next alert is chosen', async () => {
    server.detail = mine();
    renderScreen();
    await act(async () => contextProps!.onReasonClick!({ sectionId: '2', field: 'LineItems[1].Amount' }));
    expect(editor!.focusRequest).toMatchObject({ sectionId: '2', fieldPath: 'LineItems[1].Amount' });
  });

  it('open the editor on the confidence alerts when the item has review reasons', () => {
    server.detail = { ...mine(), item: { ...mine().item, reasonCount: 2 } } as ReviewItemDetail;
    renderScreen();
    expect(editor!.defaultFilter).toBe('confidence-alerts');
  });

  it('disable Summary and the baseline while the item is being worked, saying why', () => {
    server.detail = mine();
    renderScreen();
    const reason = 'Available once the review is complete';
    expect(menuItems('Reports').find((i) => i.text === 'Summary')).toMatchObject({ disabled: true, disabledReason: reason });
    expect(menuItems('More Actions').find((i) => i.text === 'Use As Evaluation Baseline')).toMatchObject({
      disabled: true,
      disabledReason: reason,
    });
  });

  it('enable Summary and the baseline once the review is complete', () => {
    server.detail = detailFor({ state: 'COMPLETE' });
    renderScreen();
    expect(menuItems('Reports').find((i) => i.text === 'Summary')).toMatchObject({ disabled: false });
    expect(menuItems('More Actions').find((i) => i.text === 'Use As Evaluation Baseline')).toMatchObject({ disabled: false });
  });

  it('show no queue, profile and priority line', () => {
    server.detail = mine();
    renderScreen();
    expect(document.body.textContent).not.toMatch(/priority/i);
  });

  it('title the document with its state, with no assignment line', () => {
    server.detail = { ...mine(), item: { ...mine().item, claimedAt: '2026-10-20T09:00:00.000Z' } } as ReviewItemDetail;
    renderScreen();
    const header = createWrapper(document.body).findHeader()!;
    expect(header.findHeadingText().getElement().textContent).toBe('w2.pdf (Claimed)');
    expect(header.findDescription()?.getElement().textContent).toBe('in/w2.pdf');
    expect(document.body.textContent).not.toMatch(/Assigned to you/);
  });

  it('let the holder regroup the whole document during review, then re-read it', async () => {
    server.detail = mine();
    renderScreen();
    chooseRegroup();
    await waitFor(() => expect(board).not.toBeNull());
    // Every section of the document, not only the one under review.
    expect(board!.sections.map((s) => s.sectionId)).toEqual(['1', '2']);
    const next = [{ sectionId: '1', documentClass: 'W2', pageIds: [1, 2] }];
    await act(async () => board!.onSave(next));
    expect(saveDocumentGrouping).toHaveBeenCalledWith('in/w2.pdf', next);
    expect(refreshDocument).toHaveBeenCalled();
    expect(screen.queryByTestId('grouping-board')).toBeNull();
    expect(claimItem).not.toHaveBeenCalled();
  });

  it('claim an unassigned item for an Admin before regrouping it', async () => {
    renderScreen();
    chooseRegroup();
    await waitFor(() => expect(board).not.toBeNull());
    expect(claimItem).toHaveBeenCalledTimes(1);
  });

  it('keep the board open with the server’s message when it refuses', async () => {
    server.detail = mine();
    saveDocumentGrouping.mockResolvedValue({ success: false, message: 'NOT_LEASE_HOLDER: this item is assigned to someone else' });
    renderScreen();
    chooseRegroup();
    await waitFor(() => expect(board).not.toBeNull());
    await act(async () => board!.onSave([]));
    expect(screen.getByTestId('grouping-board')).toBeTruthy();
    expect(refreshDocument).not.toHaveBeenCalled();
  });

  it('let an Annotator holding the item regroup it, as the server allows its holder', async () => {
    Object.assign(role, { isAdmin: false, isAnnotator: true, canWrite: false });
    server.detail = detailFor({ state: 'CLAIMED', ownerEmail: ANN, isMine: true });
    renderScreen();
    expect(regroupItem()).toMatchObject({ disabled: false });
    chooseRegroup();
    await waitFor(() => expect(board).not.toBeNull());
    const next = [{ sectionId: '1', documentClass: 'W2', pageIds: [1, 2] }];
    await act(async () => board!.onSave(next));
    expect(saveDocumentGrouping).toHaveBeenCalledWith('in/w2.pdf', next);
    // Already theirs: nothing is claimed on the way.
    expect(claimItem).not.toHaveBeenCalled();
  });

  it('ask an Annotator to claim an unassigned item before regrouping it', () => {
    Object.assign(role, { isAdmin: false, isAnnotator: true, canWrite: false });
    server.detail = detailFor({ state: 'OPEN', ownerEmail: null, isMine: false });
    renderScreen();
    expect(regroupItem()).toMatchObject({ disabled: true, disabledReason: CLAIM_TO_REGROUP_REASON });
  });

  it('disable Edit Page Grouping on an item assigned to someone else, saying why', () => {
    server.detail = annotators();
    renderScreen();
    expect(regroupItem()).toMatchObject({
      disabled: true,
      disabledReason: `Assigned to ${ANN}`,
    });
  });

  it('do not offer Edit Page Grouping for a document processed in BDA mode, saying why, even to the holder', () => {
    server.detail = detailFor({ state: 'CLAIMED', ownerEmail: ADMIN, isMine: true, processingMode: 'bda' } as Partial<ReviewItem>);
    renderScreen();
    expect(regroupItem()).toMatchObject({ disabled: true, disabledReason: BDA_REGROUP_REASON });
    // The fields stay editable: only the page grouping is fixed.
    expect(readOnly()).toBe(false);
  });

  it('say BDA mode, not claiming, is why an Annotator cannot regroup an unassigned BDA document', () => {
    Object.assign(role, { isAdmin: false, isAnnotator: true, canWrite: false });
    server.detail = detailFor({ state: 'OPEN', ownerEmail: null, isMine: false, processingMode: 'bda' } as Partial<ReviewItem>);
    renderScreen();
    // Claiming would not make it possible, so the reason is not "claim first".
    expect(regroupItem()).toMatchObject({ disabled: true, disabledReason: BDA_REGROUP_REASON });
  });

  it.each([['pipeline'], [null]])('offer Edit Page Grouping to the holder in any other mode (%s)', (processingMode) => {
    server.detail = detailFor({ state: 'CLAIMED', ownerEmail: ADMIN, isMine: true, processingMode } as Partial<ReviewItem>);
    renderScreen();
    expect(regroupItem()).toMatchObject({ disabled: false });
  });

  it('say, on the grouping board, that changed sections are extracted again at completion and corrections applied afterwards', async () => {
    server.detail = mine();
    renderScreen();
    chooseRegroup();
    await waitFor(() => expect(board).not.toBeNull());
    const consequence = screen.getByTestId('grouping-board');
    expect(consequence.textContent).toBe(
      "Moving pages rewrites this document's section grouping. When you choose Process Changes and complete the review, a section you added or whose pages you changed is extracted again, and the corrections you saved are applied afterwards. The other sections keep their extracted values and your draft edits. A section you remove takes its draft edits with it.",
    );
    expect([...consequence.querySelectorAll('b')].map((b) => b.textContent)).toEqual([
      'section grouping',
      'Process Changes',
      'extracted again',
    ]);
  });

  it('offer the Policy Rule Validation Summary during the review, and open the report', async () => {
    server.detail = mine();
    server.ruleValidationResultUri = 's3://out/in/w2.pdf/rule_validation/consolidated/consolidated_summary.md';
    renderScreen();
    // The review is not complete: Summary waits, the policy report does not.
    expect(menuItems('Reports')).toEqual([
      { text: 'History', disabled: false, disabledReason: null },
      { text: 'Summary', disabled: true, disabledReason: 'Available once the review is complete' },
      { text: 'Policy Rule Validation Summary', disabled: false, disabledReason: null },
      { text: 'Processing Report', disabled: false, disabledReason: null },
    ]);
    chooseMenuItem('Reports', 'Policy Rule Validation Summary');
    const modal = openDialog('Policy Rule Validation Summary')!;
    expect(modal.findContent().getElement().querySelector('[data-testid="markdown-report"]')!.textContent).toBe(
      'in/w2.pdf|s3://out/in/w2.pdf/rule_validation/consolidated/consolidated_summary.md',
    );
  });

  it('keep the Policy Rule Validation Summary in Reports, disabled, for a document with no policy report', () => {
    server.detail = mine();
    renderScreen();
    expect(menuItems('Reports').find((i) => i.text === 'Policy Rule Validation Summary')).toEqual({
      text: 'Policy Rule Validation Summary',
      disabled: true,
      disabledReason: 'This document has no policy rule validation summary',
    });
  });

  it('offer the Policy Rule Validation Summary beside Claim to an Annotator viewing an unassigned item', () => {
    role.isAdmin = false;
    role.isAnnotator = true;
    server.ruleValidationResultUri = 's3://out/in/w2.pdf/rule_validation/consolidated/consolidated_summary.md';
    renderScreen();
    expect(barButtonLabels()).toEqual(['Refresh', 'Claim']);
    expect(menuItems('Reports').find((i) => i.text === 'Policy Rule Validation Summary')).toMatchObject({ disabled: false });
  });

  it('save a draft through the editor, and Cancel discards unsaved edits after asking', async () => {
    server.detail = mine();
    renderScreen();
    // Disabled until the editor reports unsaved edits.
    expect(button('Save').isDisabled()).toBe(true);
    await act(async () => {
      (editor as unknown as { onDirtyChange: (d: boolean) => void }).onDirtyChange(true);
    });
    button('Save').click();
    await waitFor(() => expect(controller.save).toHaveBeenCalledTimes(1));
    controller.isDirty.mockReturnValue(true);
    button('Cancel').click();
    const dialog = await waitFor(() => createWrapper(document.body).findModal()!);
    expect(dialog.findHeader().getElement().textContent).toBe('Discard your unsaved changes?');
    dialog
      .findFooter()!
      .findAllButtons()
      .find((b) => b.getElement().textContent === 'Discard and leave')!
      .click();
    expect(controller.discard).toHaveBeenCalledTimes(1);
  });

  it('save unsaved edits before asking how the review ends, and before completing it', async () => {
    server.detail = mine();
    controller.isDirty.mockReturnValue(true);
    renderScreen();
    await openCompletion();
    expect(controller.save).toHaveBeenCalled();
    expect(complete).not.toHaveBeenCalled();
    completionButton('Complete review').click();
    await waitFor(() => expect(complete).toHaveBeenCalled());
    expect(controller.save.mock.invocationCallOrder[0]).toBeLessThan(complete.mock.invocationCallOrder[0]);
  });

  it('do not ask how the review ends when the unsaved edits could not be saved', async () => {
    server.detail = mine();
    controller.isDirty.mockReturnValue(true);
    controller.save.mockResolvedValue(false);
    renderScreen();
    fireEvent.click(button('Process Changes').getElement());
    await waitFor(() => expect(controller.save).toHaveBeenCalled());
    await act(async () => {});
    expect(completion()).toBeUndefined();
    expect(complete).not.toHaveBeenCalled();
  });

  it('offer History and Refresh beside Claim to an Annotator viewing an unassigned item', () => {
    role.isAdmin = false;
    role.isAnnotator = true;
    renderScreen();
    expect(menuNames()).toEqual(['Reports']);
    expect(barButtonLabels()).toEqual(['Refresh', 'Claim']);
    expect(menuItems('Reports').map((i) => i.text)).toContain('History');
  });

  it('ask why before skipping, and skip with that reason', async () => {
    server.detail = { ...mine(), skipReasons: ['Out of time', 'Needs expert review'] } as ReviewItemDetail;
    releaseItem.mockResolvedValue({});
    renderScreen();
    button('Skip').click();
    // Nothing is released until a reason is chosen.
    expect(releaseItem).not.toHaveBeenCalled();
    const modal = createWrapper(document.body).findModal()!;
    const select = modal.findContent().findSelect()!;
    select.openDropdown();
    select.selectOptionByValue('Needs expert review');
    modal.findContent().findTextarea()!.setTextareaValue('tax schedule');
    modal
      .findFooter()!
      .findAllButtons()
      .find((b) => b.getElement().textContent === 'Skip')!
      .click();
    await waitFor(() => expect(releaseItem).toHaveBeenCalledTimes(1));
    expect(releaseItem.mock.calls[0].slice(1)).toEqual(['Needs expert review', 'tax schedule']);
    await waitFor(() => expect(exit).toHaveBeenCalled());
  });

  it('leave Take over to Admins: an Annotator viewing someone else’s item has neither', () => {
    role.isAdmin = false;
    role.isAnnotator = true;
    server.detail = annotators();
    renderScreen();
    expect(readOnly()).toBe(true);
    expect(buttons()).not.toContain('Take over');
    expect(document.body.textContent).toContain(`View only. This item is assigned to ${ANN}.`);
  });

  it('let an Annotator claim an unassigned item instead of editing it', () => {
    role.isAdmin = false;
    role.isAnnotator = true;
    renderScreen();
    expect(readOnly()).toBe(true);
    expect(buttons()).toContain('Claim');
    expect(document.body.textContent).not.toContain('Editing or completing assigns it to you');
    // The read-only notice says why, and offers Claim beside it.
    const notice = createWrapper(document.body)
      .findAllAlerts()
      .find((a) => a.getElement().textContent?.includes('View only. Claim this item to work on it'))!;
    expect(notice.findActionSlot()!.findButton()!.getElement().textContent).toBe('Claim');
  });

  it('let a Reviewer claim an unassigned item of any queue, with no Admin assignment', async () => {
    Object.assign(role, { isAdmin: false, isAnnotator: false, isReviewer: true, canWrite: false });
    renderScreen();
    expect(buttons()).toContain('Claim');
    act(() => button('Claim').click());
    await waitFor(() => expect(claimItem).toHaveBeenCalledTimes(1));
  });

  it('let a Reviewer holding the item work it: save, Process Changes and Edit Page Grouping', () => {
    Object.assign(role, { isAdmin: false, isAnnotator: false, isReviewer: true, canWrite: false });
    server.detail = detailFor({ state: 'CLAIMED', ownerEmail: ANN, isMine: true });
    renderScreen();
    expect(readOnly()).toBe(false);
    expect(buttons()).toEqual(expect.arrayContaining(['Skip', 'Save', 'Process Changes']));
    expect(regroupItem()).toMatchObject({ disabled: false });
  });
});

describe('the breadcrumbs', () => {
  it('are told the item’s queue id and name, under Review Management for an Admin', () => {
    renderScreen();
    expect(layoutTrail).toEqual(ADMIN_TRAIL);
  });
});

describe('the edit page’s URL and navigation', () => {
  it('opens the ?section= section and copies field links to the edit page', () => {
    server.detail = mine();
    renderScreen('/documents/in%2Fw2.pdf/edit?section=2&field=Box1');
    const props = editor as unknown as { initialSectionId?: string; focusFieldPath?: string; buildFieldLink: (f: string) => string };
    expect(props.initialSectionId).toBe('2');
    expect(props.focusFieldPath).toBe('Box1');
    expect(props.buildFieldLink('Box 1')).toBe(`${window.location.origin}/#/documents/in%2Fw2.pdf/edit?field=Box%201`);
  });

  it('names the queue in the breadcrumbs from the first render, never another root first', () => {
    server.detail = mine();
    layoutTrails.length = 0;
    renderScreen();
    expect(layoutTrails[0]).toEqual(ADMIN_TRAIL);
    expect(new Set(layoutTrails.map((r) => JSON.stringify(r))).size).toBe(1);
  });

  it('highlights Review Management for an Admin and Tasks for an Annotator', () => {
    server.detail = mine();
    renderScreen();
    expect(layoutActiveHref).toBe('#/review-queues');
    document.body.innerHTML = '';
    Object.assign(role, { isAdmin: false, isAnnotator: true, canWrite: false });
    server.detail = annotators();
    renderScreen();
    expect(layoutActiveHref).toBe('#/my-work/tasks');
  });

  it.each([
    ['HOLDING', 'This item is being prepared for review. It can be edited once it reaches its queue.'],
    [
      'FINALIZE_FAILED',
      "Applying this review's corrections failed, so it can no longer be edited here. An Admin can see it in Review Management.",
    ],
  ])('says what happens next on a %s item', (state, notice) => {
    server.detail = detailFor({ state });
    renderScreen();
    expect(document.body.textContent).toContain(notice);
    expect(document.body.textContent).not.toContain('This review is finished');
  });
});
