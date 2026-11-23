// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * A review queue item's edit page under `fr-CA`: with a fixture catalog the reasons Process
 * Changes and Edit Page Grouping are unavailable (one whole sentence per finished state, and an
 * Annotator's reason to claim first), the notices of an item being prepared and of one whose
 * corrections failed, a failed Claim's error (the shared error sentence around the action's
 * French phrase) and the notice a save raises when it finds a newer draft read French. The
 * item is read once: not again when the catalog arrives, nor on a switch to English. The
 * section editor's save alert words the refusal a document's save gets once the document is in
 * a review queue. Under `en` the same renders read today's English.
 */
import React from 'react';
import { act, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { I18n } from 'aws-amplify/utils';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

const state = vi.hoisted(() => ({
  graphql: vi.fn(),
  fetchReviewItem: vi.fn(),
  claimItem: vi.fn(),
  saveDraft: vi.fn(),
  app: { setErrorMessage: vi.fn(), setSuccessMessage: vi.fn() },
  exit: vi.fn(),
  leavePath: (queuePath: string) => queuePath,
  thumbnails: { urls: {}, failure: null },
  role: { isAdmin: false, isAnnotator: true, isReviewer: false, canWrite: false, isQueueWorker: true },
  /** The props the stand-in section editor was last rendered with. */
  editor: null as { onSave: (sectionId: string, data: Record<string, unknown>) => Promise<unknown> } | null,
}));

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => state.graphql(...a) }) }));
vi.mock('../../review-queues/review-api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../review-queues/review-api')>()),
  fetchReviewItem: (...a: unknown[]) => state.fetchReviewItem(...a),
  claimItem: (...a: unknown[]) => state.claimItem(...a),
  saveDraft: (...a: unknown[]) => state.saveDraft(...a),
}));
vi.mock('../../../contexts/app', () => ({ default: () => state.app }));
vi.mock('../use-edit-exit', () => ({ default: () => state.exit, useLeavePath: () => state.leavePath }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => state.role }));
vi.mock('../../../hooks/use-review-work', () => ({ useOptionalReviewWork: () => null }));
vi.mock('../../../hooks/use-page-thumbnails', () => ({ default: () => state.thumbnails }));
vi.mock('../../review-queues/ReviewQueuesLayout', () => ({ default: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
vi.mock('../../review-queues/ReviewTaskContext', () => ({ default: () => null }));
vi.mock('../../common/PageGroupingEditor', () => ({ default: () => null }));
vi.mock('../SectionEditor', () => ({
  default: (props: NonNullable<typeof state.editor> & { regroup?: { disabledReason?: string } }) => {
    state.editor = props;
    return <button data-testid="editor-regroup" disabled={!!props.regroup?.disabledReason} data-reason={props.regroup?.disabledReason} />;
  },
}));
// The section editor itself renders the save alert; its page image and field views are not under test.
vi.mock('../../common/PageImageViewer', () => ({ default: () => null }));
vi.mock('../../document-viewer/FormFieldRenderer', () => ({ default: () => null }));
vi.mock('../../document-viewer/JSONEditorTab', () => ({ default: () => null }));

import type { ReviewItem, ReviewItemDetail } from '../../../graphql/generated/operation-types';
import { saveDirectSectionEdits } from '../document-edit-save';
import ProductionItemEdit from '../ProductionItemEdit';
import type { SectionEditorContext, SectionEditorController } from '../SectionEditor';
import type { LoadedSection } from '../types';
import { barButtons, buttonLabel } from './edit-bar';
import { regroupItem } from './regroup-stand-in';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'common.apiError.failed': 'Impossible de {action} : {error}',
  'common.labels.processChanges': 'Traiter les modifications',
  'editScreen.bar.claim': 'Réclamer',
  'editScreen.productionItem.action.claim': 'réclamer l’élément',
  'editScreen.productionItem.locked.complete': 'Cet élément est terminé',
  'editScreen.productionItem.locked.rejected': 'Cet élément est rejeté',
  'editScreen.productionItem.claimToRegroup': 'Réclamez d’abord cet élément pour modifier son regroupement de pages',
  'editScreen.productionItem.holdingNotice': 'Cet élément est en préparation. Il pourra être modifié une fois dans sa file.',
  'editScreen.productionItem.finalizeFailedNotice':
    'L’application des corrections de cette révision a échoué : il ne peut plus être modifié ici. Un administrateur le voit dans Gestion des révisions.',
  'editScreen.productionItem.staleDraft':
    'Quelqu’un a enregistré un brouillon plus récent de cette section. Il a été rechargé; refaites votre dernière modification.',
  'editScreen.sectionEditor.saveFailed': 'Échec de l’enregistrement : {error}',
  'editScreen.directEdit.inReviewSaveRefused':
    'Ce document est maintenant dans une file de révision : il ne peut pas être modifié ici. Rechargez la page pour le traiter dans sa file.',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;
type IntlOptions = Parameters<typeof renderWithIntl>[1];

const OWNER = 'annotator@example.com';
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
      reasonCount: 0,
      ...over,
    },
    queueName: 'Default',
    sections: [{ sectionId: '1', documentClass: 'W2', resultUri: 's3://b/r', draftUri: null, pageIds: [] }],
    checklist: [],
    rejectReasons: [],
    skipReasons: [],
    claimCap: 10,
    myClaimCount: 0,
    history: [],
    canClaim: true,
  }) as unknown as ReviewItemDetail;
const server: { detail: ReviewItemDetail } = { detail: detailFor({}) };

const queryOf = (call: unknown[]) => String((call[0] as { query: unknown }).query);
const documentReads = () => state.graphql.mock.calls.filter((c) => queryOf(c).includes('getDocument')).length;

const renderPage = (options: IntlOptions = {}) =>
  renderWithIntl(
    <MemoryRouter initialEntries={['/documents/in%2Fw2.pdf/edit']}>
      <ProductionItemEdit objectKey="in/w2.pdf" />
    </MemoryRouter>,
    options,
  );

/** A bar button by its label, and the reason it is disabled, as a screen reader reads it on focus. */
const barButton = (label: string) => barButtons().find((b) => buttonLabel(b) === label) ?? null;
const disabledReason = (label: string) => {
  const b = createWrapper(document.body)
    .findHeader()!
    .findActions()!
    .findAllButtons()
    .find((w) => buttonLabel(w.getElement()) === label)!;
  b.focus();
  return b.findDisabledReason()?.getElement().textContent;
};

const asAnnotator = () => Object.assign(state.role, { isAdmin: false, isAnnotator: true, canWrite: false, isQueueWorker: true });
const asAdmin = () => Object.assign(state.role, { isAdmin: true, isAnnotator: false, canWrite: true, isQueueWorker: false });

/** Save the open section through the real section editor, wired to a document's direct save, refused as the server refuses it. */
const renderRefusedSave = async (options: IntlOptions = {}) => {
  state.graphql.mockImplementation(async ({ query }: { query: unknown }) => {
    if (String(query).includes('uploadDocument')) {
      throw { errors: [{ message: 'QUEUE_MANAGED: this document is in a review queue', errorType: 'BadRequest' }] };
    }
    return { data: {} };
  });
  const { default: SectionEditor } = await vi.importActual<typeof import('../SectionEditor')>('../SectionEditor');
  const controllerRef: { current: SectionEditorController | null } = { current: null };
  let ctx: SectionEditorContext | null = null;
  const original = { inference_result: { Total: '100.00' } };
  const loadSection = async (): Promise<LoadedSection> => ({ data: JSON.parse(JSON.stringify(original)), raw: JSON.stringify(original) });
  // As the direct edit's adapter saves a section: the stored prediction is the editor's new original.
  const onSave = async (sectionId: string, edited: Record<string, unknown>) => {
    const saved = await saveDirectSectionEdits({
      objectKey: 'in/doc.pdf',
      sectionId,
      outputUri: 's3://out/in/doc.pdf/sections/1/result.json',
      username: 'reviewer',
      reviewerOnly: false,
      prediction: edited,
      originalPrediction: original,
      baseline: null,
      originalBaseline: null,
    });
    return saved.prediction;
  };
  renderWithIntl(
    <SectionEditor
      objectKey="in/doc.pdf"
      sections={[{ sectionId: '1', resultUri: 's3://out/in/doc.pdf/sections/1/result.json', pageIds: [] }]}
      pages={[]}
      isReadOnly={false}
      loadSection={loadSection}
      onSave={onSave}
      controllerRef={controllerRef}
      headerExtras={(c) => {
        ctx = c;
        return null;
      }}
    />,
    options,
  );
  await waitFor(() => expect(ctx?.data).toBeTruthy());
  act(() => ctx!.setData({ inference_result: { Total: '105.00' } }));
  await act(async () => {
    await controllerRef.current!.save();
  });
};

beforeEach(() => {
  document.body.innerHTML = '';
  state.editor = null;
  asAnnotator();
  server.detail = detailFor({});
  state.fetchReviewItem.mockReset().mockImplementation(async () => server.detail);
  state.claimItem.mockReset().mockRejectedValue(new Error('Item limit reached'));
  state.saveDraft.mockReset().mockRejectedValue({ errors: [{ message: 'STALE_DRAFT: a newer draft was saved' }] });
  state.graphql.mockReset().mockResolvedValue({ data: { getDocument: { ObjectKey: 'in/w2.pdf', Pages: [], Sections: [] } } });
  state.app.setErrorMessage.mockReset();
  state.app.setSuccessMessage.mockReset();
  state.exit.mockReset();
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('a review queue item’s edit page under fr-CA', () => {
  it.each([
    ['COMPLETE', 'Cet élément est terminé'],
    ['REJECTED', 'Cet élément est rejeté'],
  ])('words why Process Changes and Edit Page Grouping are unavailable on a %s item, one sentence per state', async (itemState, reason) => {
    asAdmin();
    server.detail = detailFor({ state: itemState });
    renderPage(FR_CA);
    await waitFor(() => expect(regroupItem()?.disabledReason).toBe(reason));
    expect(disabledReason('Traiter les modifications')).toBe(reason);
  });

  it.each([
    ['HOLDING', FR['editScreen.productionItem.holdingNotice']],
    ['FINALIZE_FAILED', FR['editScreen.productionItem.finalizeFailedNotice']],
  ])('words the read-only notice of a %s item', async (itemState, notice) => {
    server.detail = detailFor({ state: itemState });
    renderPage(FR_CA);
    expect(await screen.findByText(notice)).toBeInTheDocument();
  });

  it('asks an Annotator to claim an unassigned item before regrouping it', async () => {
    renderPage(FR_CA);
    await waitFor(() => expect(regroupItem()?.disabledReason).toBe(FR['editScreen.productionItem.claimToRegroup']));
  });

  it('words a failed Claim with the shared error sentence around the French phrase', async () => {
    renderPage(FR_CA);
    await waitFor(() => expect(barButton('Réclamer')).not.toBeNull());
    act(() => barButton('Réclamer')!.click());
    await waitFor(() => expect(state.app.setErrorMessage).toHaveBeenCalledWith('Impossible de réclamer l’élément : Item limit reached'));
  });

  it('words the notice a save raises when it finds a newer draft', async () => {
    server.detail = detailFor({ state: 'CLAIMED', ownerEmail: OWNER, isMine: true });
    renderPage(FR_CA);
    await waitFor(() => expect(state.editor).not.toBeNull());
    await act(async () => {
      await state.editor!.onSave('1', { inference_result: { Total: '1' } });
    });
    await waitFor(() => expect(state.app.setErrorMessage).toHaveBeenCalledWith(FR['editScreen.productionItem.staleDraft']));
  });

  it('reads the item and its document once across the catalog’s arrival and a switch to English', async () => {
    const view = renderPage(FR_CA);
    await waitFor(() => expect(regroupItem()?.disabledReason).toBe(FR['editScreen.productionItem.claimToRegroup']));
    expect(state.fetchReviewItem).toHaveBeenCalledTimes(1);
    expect(documentReads()).toBe(1);
    view.setLocale('en');
    await waitFor(() => expect(regroupItem()?.disabledReason).toBe('Claim this item first to change its page grouping'));
    expect(state.fetchReviewItem).toHaveBeenCalledTimes(1);
    expect(documentReads()).toBe(1);
  });

  it('words, in the section editor’s save alert, the refusal of a save once the document is in a review queue', async () => {
    await renderRefusedSave(FR_CA);
    expect(await screen.findByText(`Échec de l’enregistrement : ${FR['editScreen.directEdit.inReviewSaveRefused']}`)).toBeInTheDocument();
  });
});

describe('a review queue item’s edit page under en', () => {
  it.each([
    ['COMPLETE', 'This item is complete'],
    ['REJECTED', 'This item is rejected'],
  ])('reads today’s English for why actions are unavailable on a %s item', async (itemState, reason) => {
    asAdmin();
    server.detail = detailFor({ state: itemState });
    renderPage();
    await waitFor(() => expect(regroupItem()?.disabledReason).toBe(reason));
    expect(disabledReason('Process Changes')).toBe(reason);
  });

  it.each([
    ['HOLDING', 'This item is being prepared for review. It can be edited once it reaches its queue.'],
    [
      'FINALIZE_FAILED',
      "Applying this review's corrections failed, so it can no longer be edited here. An Admin can see it in Review Management.",
    ],
  ])('reads today’s English notice of a %s item', async (itemState, notice) => {
    server.detail = detailFor({ state: itemState });
    renderPage();
    expect(await screen.findByText(notice)).toBeInTheDocument();
  });

  it('reads today’s English for claiming before regrouping, and for a failed Claim', async () => {
    renderPage();
    await waitFor(() => expect(regroupItem()?.disabledReason).toBe('Claim this item first to change its page grouping'));
    act(() => barButton('Claim')!.click());
    await waitFor(() => expect(state.app.setErrorMessage).toHaveBeenCalledWith('Failed to claim the item: Item limit reached'));
  });

  it('reads today’s English notice for a newer draft', async () => {
    server.detail = detailFor({ state: 'CLAIMED', ownerEmail: OWNER, isMine: true });
    renderPage();
    await waitFor(() => expect(state.editor).not.toBeNull());
    await act(async () => {
      await state.editor!.onSave('1', { inference_result: { Total: '1' } });
    });
    await waitFor(() =>
      expect(state.app.setErrorMessage).toHaveBeenCalledWith(
        'Someone saved a newer draft of this section. It has been reloaded; reapply your last change.',
      ),
    );
  });

  it('reads today’s English for a save refused once the document is in a review queue', async () => {
    await renderRefusedSave();
    expect(
      await screen.findByText(
        "Failed to save: This document is now in a review queue, so it can't be edited here. Reload the page to work on it in its queue.",
      ),
    ).toBeInTheDocument();
  });
});
