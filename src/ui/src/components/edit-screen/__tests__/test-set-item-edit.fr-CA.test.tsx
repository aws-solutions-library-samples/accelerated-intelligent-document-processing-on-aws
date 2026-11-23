// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * A test-set item's edit page under `fr-CA`, over the real model and adapter: with a fixture
 * catalog the notice after Save and the refusal when the server does not count the document
 * reviewed yet read French around the shared Process Changes; the adapter's codes for a newer
 * draft, a document the server refused to read and a read that returned no queue are worded
 * by the page; the alert count reads its plural for 1 and 3 fields beside Next alert. The
 * document is read once: not again when the catalog arrives, nor on a switch to English.
 * Under `en` the same renders read today's English.
 */
import React from 'react';
import { act, screen, waitFor, within } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { I18n } from 'aws-amplify/utils';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

const state = vi.hoisted(() => ({
  graphql: vi.fn(),
  app: { setErrorMessage: vi.fn(), setSuccessMessage: vi.fn(), user: { username: 'annotator' } },
  settings: { settings: { TestSetBucket: 'test-set-bucket' } },
  role: {
    isAdmin: false,
    isAuthor: false,
    isReviewer: false,
    isAnnotator: true,
    canAnnotate: true,
    canWorkTestSetQueues: true,
    loading: false,
    allowedConfigVersions: null,
  },
  pages: { pages: [], isLoading: false, error: null, previewUnavailable: false },
  configuration: { mergedConfig: null },
  versions: { versions: [] },
  controller: { save: vi.fn(), discard: vi.fn(), isDirty: vi.fn() },
  /** The props the stand-in section editor was last rendered with. */
  editor: null as {
    onSave: (sectionId: string, data: Record<string, unknown>) => Promise<unknown>;
    onDirtyChange: (dirty: boolean) => void;
  } | null,
}));

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => state.graphql(...a) }) }));
vi.mock('../../../contexts/app', () => ({ default: () => state.app }));
vi.mock('../../../contexts/settings', () => ({ default: () => state.settings }));
vi.mock('../../../hooks/use-review-work', () => ({ useOptionalReviewWork: () => null }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => state.role }));
vi.mock('../../../hooks/use-test-doc-pages', () => ({ default: () => state.pages }));
vi.mock('../../../hooks/use-configuration', () => ({ default: () => state.configuration }));
vi.mock('../../../hooks/use-configuration-versions', () => ({ default: () => state.versions }));
vi.mock('../../review-queues/ReviewQueuesLayout', () => ({ default: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
vi.mock('../SectionEditor', () => ({
  default: (props: NonNullable<typeof state.editor> & { controllerRef: { current: unknown } }) => {
    state.editor = props;
    props.controllerRef.current = state.controller;
    return <div data-testid="editor" />;
  },
}));

import { completeSectionReview, getAnnotationQueueDocument, saveTestSetDocumentSectionDraft } from '../../../graphql/generated';
import { MY_QUEUES_PATH } from '../../../routes/constants';
import TestSetItemEdit from '../TestSetItemEdit';
import { barButtons, buttonLabel } from './edit-bar';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'common.labels.processChanges': 'Traiter les modifications',
  'common.labels.nextAlert': 'Alerte suivante',
  'editScreen.bar.save': 'Enregistrer',
  'editScreen.shared.couldNotOpenDocument': 'Impossible d’ouvrir ce document',
  'editScreen.shared.documentCouldNotLoad': 'Le document n’a pas pu être chargé.',
  'testStudio.itemEdit.savedDraft':
    'Vos modifications de {name} sont enregistrées comme brouillon. {processChanges} les écrit comme vérité terrain révisée.',
  'testStudio.itemEdit.notReviewed':
    'Les sections de {name} ne sont pas encore toutes marquées comme révisées. Vérifiez ses sections et choisissez de nouveau {processChanges}.',
  'testStudio.itemEdit.staleDraft':
    'Quelqu’un a enregistré des modifications plus récentes de cette section. Elle a été rechargée; refaites votre dernière modification.',
  'testStudio.itemEdit.cannotOpenHeader': 'Vous ne pouvez pas ouvrir ce document',
  'testStudio.itemEdit.cannotOpenBody':
    'Seuls les annotateurs inscrits à la file de cet ensemble de test peuvent ouvrir ses documents. Les files qui vous incluent sont dans <link>Accueil › Files</link>.',
  'testStudio.itemEdit.alertCount':
    '{count, plural, one {# champ est sous le seuil de confiance.} other {# champs sont sous le seuil de confiance.}}',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;
type IntlOptions = Parameters<typeof renderWithIntl>[1];

const DOC_KEY = 'in/doc.pdf';
/** What the server answers: the document's row, whether it counts the document reviewed, and how the read ends. */
const server: { doc: Record<string, unknown>; reviewed: boolean; read: 'ok' | 'not-assigned' | 'no-queue' } = {
  doc: {},
  reviewed: false,
  read: 'ok',
};
const queueReply = () => ({
  data: {
    getAnnotationQueue: {
      testSetId: 'ts-1',
      name: 'Set A',
      skipReasons: ['Out of time'],
      draftVersion: 2,
      baseVersion: 1,
      documents: [
        {
          objectKey: DOC_KEY,
          inputKey: `ts-1/input/${DOC_KEY}`,
          claimedByMe: true,
          assignedTo: 'annotator@example.com',
          reviewed: server.reviewed,
          skipped: false,
          labelSource: 'draft-machine',
          reviewObjectKey: `run/${DOC_KEY}`,
          alertCount: 0,
          sections: [{ sectionId: '1', baselineKey: `ts-1/baseline/${DOC_KEY}/sections/1/result.json`, pageIndices: [0] }],
          draftSections: [],
          ...server.doc,
        },
      ],
    },
  },
});

const queryOf = (call: unknown[]) => (call[0] as { query: unknown }).query;
const documentReads = () => state.graphql.mock.calls.filter((c) => queryOf(c) === getAnnotationQueueDocument).length;

const renderPage = (options: IntlOptions = {}) =>
  renderWithIntl(
    <MemoryRouter>
      <TestSetItemEdit testSetId="ts-1" objectKey={DOC_KEY} />
    </MemoryRouter>,
    options,
  );

const barButton = (label: string) => barButtons().find((b) => buttonLabel(b) === label) ?? null;

/** Choose a bar button once the page offers it enabled. */
const choose = async (label: string) => {
  await waitFor(() => expect(barButton(label)).not.toBeNull());
  await waitFor(() => expect(barButton(label)!.hasAttribute('disabled')).toBe(false));
  await act(async () => {
    barButton(label)!.click();
  });
};

/** Save the open section's changes from the bar. */
const saveFromBar = async (label: string) => {
  await waitFor(() => expect(state.editor).not.toBeNull());
  act(() => state.editor!.onDirtyChange(true));
  await choose(label);
};

/** A save that finds a newer draft of the open section. */
const saveIntoNewerDraft = async () => {
  await waitFor(() => expect(state.editor).not.toBeNull());
  await act(async () => {
    await state.editor!.onSave('1', { inference_result: { Total: '1' } });
  });
};

const alert = () => createWrapper(document.body).findAlert()!;

beforeEach(() => {
  document.body.innerHTML = '';
  state.editor = null;
  server.doc = {};
  server.reviewed = false;
  server.read = 'ok';
  state.graphql.mockReset().mockImplementation(async ({ query }: { query: unknown }) => {
    if (query === getAnnotationQueueDocument) {
      if (server.read === 'not-assigned') throw { errors: [{ message: 'NOT_ASSIGNED: this queue does not list the caller' }] };
      return server.read === 'no-queue' ? { data: { getAnnotationQueue: null } } : queueReply();
    }
    if (query === saveTestSetDocumentSectionDraft) throw { errors: [{ message: 'STALE_DRAFT: a newer draft of this section was saved' }] };
    if (query === completeSectionReview) return { data: { completeSectionReview: {} } };
    return { data: {} };
  });
  state.app.setErrorMessage.mockReset();
  state.app.setSuccessMessage.mockReset();
  state.controller.isDirty.mockReset().mockReturnValue(false);
  state.controller.save.mockReset().mockResolvedValue(true);
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('a test-set item’s edit page under fr-CA', () => {
  it('words the notice after Save around the shared Process Changes', async () => {
    renderPage(FR_CA);
    await saveFromBar('Enregistrer');
    await waitFor(() =>
      expect(state.app.setSuccessMessage).toHaveBeenCalledWith(
        'Vos modifications de doc.pdf sont enregistrées comme brouillon. Traiter les modifications les écrit comme vérité terrain révisée.',
      ),
    );
  });

  it('words the refusal when the server does not count the document reviewed yet', async () => {
    renderPage(FR_CA);
    await choose('Traiter les modifications');
    await waitFor(() =>
      expect(state.app.setErrorMessage).toHaveBeenCalledWith(
        'Les sections de doc.pdf ne sont pas encore toutes marquées comme révisées. Vérifiez ses sections et choisissez de nouveau Traiter les modifications.',
      ),
    );
    expect(state.graphql.mock.calls.filter((c) => queryOf(c) === completeSectionReview)).toHaveLength(1);
  });

  it('words a newer draft from the adapter’s code', async () => {
    renderPage(FR_CA);
    await saveIntoNewerDraft();
    await waitFor(() => expect(state.app.setErrorMessage).toHaveBeenCalledWith(FR['testStudio.itemEdit.staleDraft']));
  });

  it('words a document the server refused to read from the adapter’s code', async () => {
    server.read = 'not-assigned';
    renderPage(FR_CA);
    await waitFor(() => expect(alert()?.findHeader()?.getElement().textContent).toBe(FR['testStudio.itemEdit.cannotOpenHeader']));
    const content = alert().findContent().getElement();
    expect(content.textContent).toBe(
      'Seuls les annotateurs inscrits à la file de cet ensemble de test peuvent ouvrir ses documents. Les files qui vous incluent sont dans Accueil › Files.',
    );
    expect(within(content).getByRole('link', { name: 'Accueil › Files' }).getAttribute('href')).toBe(`#${MY_QUEUES_PATH}`);
  });

  it('words a read that returned no queue from the adapter’s code', async () => {
    server.read = 'no-queue';
    renderPage(FR_CA);
    await waitFor(() => expect(alert()?.findHeader()?.getElement().textContent).toBe(FR['editScreen.shared.couldNotOpenDocument']));
    expect(alert().findContent().getElement().textContent).toBe(FR['editScreen.shared.documentCouldNotLoad']);
  });

  it.each([
    [1, '1 champ est sous le seuil de confiance.'],
    [3, '3 champs sont sous le seuil de confiance.'],
  ])('words the alert count for %i beside Next alert', async (count, text) => {
    server.doc = { alertCount: count };
    renderPage(FR_CA);
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(alert().findActionSlot()!.findButton()!.getElement().textContent).toBe('Alerte suivante');
  });

  it('reads the document once across the catalog’s arrival and a switch to English', async () => {
    server.doc = { alertCount: 1 };
    const view = renderPage(FR_CA);
    expect(await screen.findByText('1 champ est sous le seuil de confiance.')).toBeInTheDocument();
    expect(documentReads()).toBe(1);
    view.setLocale('en');
    expect(await screen.findByText('1 field is below the confidence threshold.')).toBeInTheDocument();
    expect(documentReads()).toBe(1);
  });
});

describe('a test-set item’s edit page under en', () => {
  it('reads today’s English notice after Save', async () => {
    renderPage();
    await saveFromBar('Save');
    await waitFor(() =>
      expect(state.app.setSuccessMessage).toHaveBeenCalledWith(
        'Saved your changes to doc.pdf as a draft. Process Changes writes them as reviewed ground truth.',
      ),
    );
  });

  it('reads today’s English refusal when the server does not count the document reviewed yet', async () => {
    renderPage();
    await choose('Process Changes');
    await waitFor(() =>
      expect(state.app.setErrorMessage).toHaveBeenCalledWith(
        'Not every section of doc.pdf is marked reviewed yet. Check its sections and choose Process Changes again.',
      ),
    );
  });

  it('reads today’s English notice for a newer draft', async () => {
    renderPage();
    await saveIntoNewerDraft();
    await waitFor(() =>
      expect(state.app.setErrorMessage).toHaveBeenCalledWith(
        'Someone saved newer changes to this section. It has been reloaded; reapply your last change.',
      ),
    );
  });

  it('reads today’s English for a document the server refused to read', async () => {
    server.read = 'not-assigned';
    renderPage();
    await waitFor(() => expect(alert()?.findHeader()?.getElement().textContent).toBe("You can't open this document"));
    const content = alert().findContent().getElement();
    expect(content.textContent).toBe(
      "Only the Annotators listed on this test set's queue can open its documents. The queues that list you are in Home › Queues.",
    );
    expect(within(content).getByRole('link', { name: 'Home › Queues' }).getAttribute('href')).toBe(`#${MY_QUEUES_PATH}`);
  });

  it('reads today’s English for a read that returned no queue', async () => {
    server.read = 'no-queue';
    renderPage();
    await waitFor(() => expect(alert()?.findHeader()?.getElement().textContent).toBe("Couldn't open this document"));
    expect(alert().findContent().getElement().textContent).toBe('The document could not be loaded.');
  });

  it.each([
    [1, '1 field is below the confidence threshold.'],
    [3, '3 fields are below the confidence threshold.'],
  ])('reads today’s English alert count for %i beside Next alert', async (count, text) => {
    server.doc = { alertCount: count };
    renderPage();
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(alert().findActionSlot()!.findButton()!.getElement().textContent).toBe('Next alert');
  });
});
