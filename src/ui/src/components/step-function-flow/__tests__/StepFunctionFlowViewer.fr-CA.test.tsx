// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Reports › Processing Flow under `fr-CA`: with a fixture catalog the dialog's title, the
 * Execution Overview's labels, the execution's and the steps' statuses, a Map step's iterations
 * badge and the diagram's legend read French, and so do the Malware Scan step's name and, once
 * the step is chosen, the explanation of its verdict in Output Data, which its copy button
 * copies. The execution is requested once: neither the catalog's arrival nor a language switch
 * requests it again, while the labels reword and the chosen Malware Scan step stays chosen.
 * Under `en` the same renders read today's English. State names stay as the execution reports
 * them.
 */
import React from 'react';
import { act, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { I18n } from 'aws-amplify/utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

// One value per mocked hook for every render. The viewer calls generateClient() at import time,
// so the client's graphql function is hoisted.
const state = vi.hoisted(() => ({ graphql: vi.fn(), writeText: vi.fn() }));

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => state.graphql(...a) }) }));

import StepFunctionFlowViewer from '../StepFunctionFlowViewer';

const ARN = 'arn:aws:states:us-east-1:123456789012:execution:idp-workflow:doc-1';
/** A fixed instant `seconds` after the execution started, as the API returns it: the UTC ISO string. */
const at = (seconds: number) => new Date(1_700_000_000_000 + seconds * 1000).toISOString();

// A failed execution: OCR, a Map step that ran one iteration, then a summarization step that
// failed. The viewer prepends the Malware Scan step, built from the document's clean verdict.
const EXECUTION = {
  executionArn: ARN,
  status: 'FAILED',
  startDate: at(0),
  stopDate: at(90),
  error: 'States.TaskFailed',
  steps: [
    {
      name: 'OCRStep',
      type: 'Task',
      status: 'SUCCEEDED',
      startDate: at(1),
      stopDate: at(20),
      input: '{"page":1}',
      output: '{"pages":1}',
    },
    {
      name: 'ProcessSections',
      type: 'Map',
      status: 'SUCCEEDED',
      startDate: at(21),
      stopDate: at(50),
      mapIterations: 1,
      mapIterationDetails: [
        {
          name: 'ProcessSections (Iteration 1)',
          type: 'MapIteration',
          status: 'SUCCEEDED',
          startDate: at(22),
          stopDate: at(49),
        },
      ],
    },
    {
      name: 'SummarizationStep',
      type: 'Task',
      status: 'FAILED',
      startDate: at(51),
      stopDate: at(90),
      error: 'Lambda.Unknown: model call failed',
    },
  ],
};

const CLEAN = {
  objectStatus: 'COMPLETED',
  malwareScanStatus: 'COMPLETED',
  malwareScanResult: 'NO_THREATS_FOUND',
  malwareScanTime: at(-2),
};

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'documents.processingFlow.title': 'Flux de traitement du document',
  'documents.processingFlow.overviewTitle': 'Aperçu de l’exécution',
  'documents.processingFlow.field.status': 'Statut',
  'documents.processingFlow.field.duration': 'Durée',
  'documents.processingFlow.field.started': 'Début',
  'documents.processingFlow.field.completed': 'Fin',
  'documents.processingFlow.status.succeeded': 'Réussi',
  'documents.processingFlow.status.failed': 'Échec',
  'documents.processingFlow.iterations': '{count, plural, one {# itération} other {# itérations}}',
  'documents.processingFlow.diagram.legendCompleted': 'Terminé',
  'documents.processingFlow.diagram.legendRunning': 'En cours',
  'documents.processingFlow.diagram.legendFailed': 'Échoué',
  'documents.processingFlow.diagram.legendPending': 'En attente',
  'documents.processingFlow.malwareScanStep': 'Analyse antimalware',
  'documents.processingFlow.step.outputData': 'Données de sortie',
  'documents.malwareScan.clean.detail': 'Amazon GuardDuty a analysé ce fichier et n’y a trouvé aucune menace.',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

const viewer = <StepFunctionFlowViewer executionArn={ARN} visible onDismiss={() => undefined} malwareScan={CLEAN} />;

const texts = (selector: string) => Array.from(document.querySelectorAll(selector), (element) => element.textContent);
const dialog = () => createWrapper().findModal()!;
/** The dialog's title: the heading of the Header the viewer passes, which the dialog wraps in a heading of its own. */
const dialogTitle = () => {
  const headings = dialog().findHeader().findAll('h2');
  return headings[headings.length - 1].getElement().textContent;
};
/** The first section of the dialog: the Execution Overview. */
const overview = () => dialog().findContent().findContainer()!.getElement().textContent;
/** The names the diagram's step nodes show, in order. */
const diagramNames = () => texts('.flow-step .step-name > span:first-child');
/** The diagram's step node that shows this name. */
const diagramStep = (name: string) =>
  Array.from(document.querySelectorAll<HTMLElement>('.flow-step')).find(
    (node) => node.querySelector('.step-name > span:first-child')?.textContent === name,
  )!;
const stepDetails = () => createWrapper(document.querySelector<HTMLElement>('.step-details')!);
/** Opens the chosen step's Output Data and returns its header and what it shows. */
const openOutputData = () => {
  const section = stepDetails().findExpandableSection()!;
  act(() => section.findExpandButton().click());
  return { header: section.findHeaderText()!.getElement().textContent, content: section.findExpandedContent()!.getElement().textContent };
};

beforeEach(() => {
  state.graphql.mockReset();
  state.graphql.mockResolvedValue({ data: { getStepFunctionExecution: EXECUTION } });
  state.writeText.mockReset();
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: state.writeText } });
});

afterEach(() => {
  Reflect.deleteProperty(navigator, 'clipboard');
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('Reports › Processing Flow under fr-CA', () => {
  it('reads its title, the overview, the statuses, the iterations badge and the legend in French', async () => {
    renderWithIntl(viewer, FR_CA);
    // Awaited: the catalog and the execution both arrive asynchronously.
    expect(await screen.findByText('Aperçu de l’exécution')).toBeInTheDocument();
    expect(dialogTitle()).toBe('Flux de traitement du document');
    for (const label of ['Statut', 'Durée', 'Début', 'Fin']) expect(overview()).toContain(label);
    expect(texts('.execution-status')).toEqual(['Échec']);
    expect(texts('.flow-step .step-status-text')).toEqual(['Réussi', 'Réussi', 'Réussi', 'Échec']);
    // The Map step's badge, in the diagram and in the Steps Timeline.
    expect(screen.getAllByText('1 itération')).toHaveLength(2);
    expect(texts('.flow-legend span')).toEqual(['Terminé', 'En cours', 'Échoué', 'En attente']);
    expect(screen.queryByText('Execution Overview')).toBeNull();
    // State names stay as the execution reports them.
    expect(diagramNames()).toEqual(['Analyse antimalware', 'OCRStep', 'ProcessSections', 'SummarizationStep']);
  });

  it('names the Malware Scan step in French, and once it is chosen gives and copies its verdict in French', async () => {
    renderWithIntl(viewer, FR_CA);
    expect(await screen.findByText('Aperçu de l’exécution')).toBeInTheDocument();
    expect(texts('.timeline-step-name')[0]).toBe('Analyse antimalware');
    act(() => diagramStep('Analyse antimalware').click());
    expect(stepDetails().find('h3')!.getElement().textContent).toBe('Analyse antimalware');
    const french = 'Amazon GuardDuty a analysé ce fichier et n’y a trouvé aucune menace.';
    expect(openOutputData()).toEqual({ header: 'Données de sortie', content: french });
    act(() => screen.getByRole('button', { name: 'Copy output data' }).click());
    expect(state.writeText).toHaveBeenCalledWith(french);
  });

  // The catalog arrives after the first render, as a new `intl`; neither that nor a language
  // switch may request the execution again.
  it('requests the execution once, and a language switch rewords it without a new request or a lost choice', async () => {
    const view = renderWithIntl(viewer, FR_CA);
    expect(await screen.findByText('Aperçu de l’exécution')).toBeInTheDocument();
    expect(state.graphql).toHaveBeenCalledTimes(1);
    act(() => diagramStep('Analyse antimalware').click());
    expect(texts('.flow-step.selected .step-name > span:first-child')).toEqual(['Analyse antimalware']);
    view.setLocale('en');
    await waitFor(() => expect(screen.getByText('Execution Overview')).toBeInTheDocument());
    expect(texts('.execution-status')).toEqual(['FAILED']);
    expect(state.graphql).toHaveBeenCalledTimes(1);
    // The chosen step stays chosen, in the diagram, in the Steps Timeline and in Step Details.
    expect(texts('.flow-step.selected .step-name > span:first-child')).toEqual(['Malware Scan']);
    expect(texts('.timeline-step.selected .timeline-step-name')).toEqual(['Malware Scan']);
    expect(stepDetails().find('h3')!.getElement().textContent).toBe('Malware Scan');
  });
});

describe('Reports › Processing Flow under en', () => {
  it('reads the inline English for its title, the overview, the statuses, the iterations badge and the legend', async () => {
    renderWithIntl(viewer);
    expect(await screen.findByText('Execution Overview')).toBeInTheDocument();
    expect(dialogTitle()).toBe('Document Processing Flow');
    for (const label of ['Status', 'Duration', 'Started', 'Completed']) expect(overview()).toContain(label);
    expect(texts('.execution-status')).toEqual(['FAILED']);
    expect(texts('.flow-step .step-status-text')).toEqual(['SUCCEEDED', 'SUCCEEDED', 'SUCCEEDED', 'FAILED']);
    expect(screen.getAllByText('1 iteration')).toHaveLength(2);
    expect(screen.queryByText('1 iterations')).toBeNull();
    expect(texts('.flow-legend span')).toEqual(['Completed', 'Running', 'Failed', 'Pending']);
    expect(diagramNames()).toEqual(['Malware Scan', 'OCRStep', 'ProcessSections', 'SummarizationStep']);
  });

  it('names the Malware Scan step in English, and once it is chosen gives and copies its verdict in English', async () => {
    renderWithIntl(viewer);
    expect(await screen.findByText('Execution Overview')).toBeInTheDocument();
    expect(texts('.timeline-step-name')[0]).toBe('Malware Scan');
    act(() => diagramStep('Malware Scan').click());
    expect(stepDetails().find('h3')!.getElement().textContent).toBe('Malware Scan');
    const english = 'Amazon GuardDuty scanned this file and found no threats.';
    expect(openOutputData()).toEqual({ header: 'Output Data', content: english });
    act(() => screen.getByRole('button', { name: 'Copy output data' }).click());
    expect(state.writeText).toHaveBeenCalledWith(english);
  });
});
