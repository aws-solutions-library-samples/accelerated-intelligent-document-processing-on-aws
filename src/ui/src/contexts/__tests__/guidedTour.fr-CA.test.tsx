// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The guided tour's text comes from the catalog of the active locale: under `fr-CA` with a
 * small fixture catalog the first step's popover reads the French header, content, step counter
 * and Next button, and its close button carries the French dismiss label. Clicking that button
 * exits the tour (the popover and the hotspot marker both go), which proves the label and the
 * click-capture that watches for it still agree in a language other than English.
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { createIntl, IntlProvider } from 'react-intl';
import { describe, expect, it } from 'vitest';

import { onIntlError } from '../../i18n/LocaleProvider';
import { GuidedTourProvider, buildTutorial } from '../guidedTour';
import { TourHotspot } from '../tourHotspots';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'guidedTour.tutorial.title': 'Premiers pas avec GenAI IDP',
  'guidedTour.task.findYourWay': 'Se repérer',
  'guidedTour.step.documents.title': 'Documents',
  'guidedTour.step.documents.content': 'Vos documents traités se trouvent ici.',
  'guidedTour.i18n.stepCounter': 'Étape {stepNumber} de {totalStepCount}',
  'guidedTour.i18n.taskTitle': 'Tâche {taskNumber} : {taskTitle}',
  'guidedTour.i18n.closeAnnotation': "Fermer l'annotation",
  'guidedTour.i18n.openAnnotation': "Ouvrir l'annotation",
  'guidedTour.i18n.next': 'Suivant',
  'guidedTour.i18n.dismiss': 'Fermer la visite',
};

const French = ({ children }: { children: React.ReactNode }) => (
  <IntlProvider locale="fr-CA" defaultLocale="en" messages={FR} onError={onIntlError}>
    {children}
  </IntlProvider>
);

const flags = { customModels: false, capacityPlanning: true, quickStart: true };

/** The steps whose hotspot is on the page; the tour keeps only those. Two, so the first step has a Next button. */
const MOUNTED = new Set(['nav-documents', 'nav-upload']);

/** The number of steps the tour has with `flags` and these hotspots, read from the tutorial itself. */
const stepCount = (): number => buildTutorial(createIntl({ locale: 'en', defaultLocale: 'en' }), flags, MOUNTED).tasks[0].steps.length;

/** Mount the provider around the first two steps' hotspots and start the tour as the Welcome page does. */
const startTour = (): void => {
  render(
    <MemoryRouter>
      <GuidedTourProvider>
        {[...MOUNTED].map((id) => (
          <TourHotspot key={id} hotspotId={id} />
        ))}
      </GuidedTourProvider>
    </MemoryRouter>,
    { wrapper: French },
  );
  act(() => {
    window.dispatchEvent(new CustomEvent('startTutorial', { detail: flags }));
  });
};

describe('the guided tour under fr-CA', () => {
  it('reads the first step and the annotation strings from the French catalog', async () => {
    startTour();

    // Awaited: the tour opens 350 ms after it is started, on the first hotspot in the tree.
    expect(await screen.findByText('Vos documents traités se trouvent ici.')).toBeInTheDocument();
    expect(screen.getByText('Tâche 1 : Se repérer')).toBeInTheDocument();
    expect(screen.getByText(`Étape 1 de ${stepCount()}`)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Suivant' })).toBeInTheDocument();
    expect(screen.getByLabelText("Fermer l'annotation")).toBeInTheDocument();
    expect(screen.queryByText(/^Step 1 of \d+$/)).toBeNull();
    expect(screen.queryByLabelText('Dismiss tour')).toBeNull();
  });

  it('exits the tour when its French dismiss label is clicked', async () => {
    startTour();

    const dismiss = await screen.findByLabelText('Fermer la visite');
    fireEvent.click(dismiss);

    // Exiting empties the tutorial, so the hotspot renders no marker at all. Cloudscape's own
    // dismiss merely closes the popover and would leave the closed marker ("Ouvrir l'annotation")
    // behind, which is what a selector that no longer matches the label would produce.
    await waitFor(() => {
      expect(screen.queryByText(/^Étape 1 de \d+$/)).toBeNull();
      expect(screen.queryByLabelText("Ouvrir l'annotation")).toBeNull();
    });
  });
});
