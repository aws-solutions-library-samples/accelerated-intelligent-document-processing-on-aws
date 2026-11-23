// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Welcome page's text comes from the catalog of the active locale: under `fr-CA` with a
 * small fixture catalog the heading, a button and a hint read French, and the hint keeps its
 * bold question and its link; under `en` the hint reads the inline English, character for
 * character, with the `>` of the navigation path and the spaces around its bold and link parts.
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { IntlProvider } from 'react-intl';
import { describe, expect, it, vi } from 'vitest';

import WelcomeContent from '../WelcomeContent';

vi.mock('../../../contexts/settings', () => ({
  default: () => ({ settings: {} }),
}));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isAnnotatorOnly: false }),
}));

const FR = {
  'welcome.page.title': 'Bienvenue dans GenAI IDP',
  'welcome.page.takeTour': 'Faire la visite',
  'welcome.page.enterConsole': 'Ouvrir la console IDP',
  'welcome.page.haveConfigHint':
    '<b>Vous avez déjà une configuration?</b> Allez à <link>Configuration > Afficher/modifier la configuration</link> pour la revoir ou la mettre à jour.',
  'welcome.page.dismiss': 'Ne plus afficher',
};

const English = ({ children }: { children: React.ReactNode }) => (
  <IntlProvider locale="en" defaultLocale="en">
    {children}
  </IntlProvider>
);

const French = ({ children }: { children: React.ReactNode }) => (
  <IntlProvider locale="fr-CA" defaultLocale="en" messages={FR}>
    {children}
  </IntlProvider>
);

const renderWelcome = (wrapper: React.ComponentType<{ children: React.ReactNode }>) =>
  render(
    <MemoryRouter>
      <WelcomeContent />
    </MemoryRouter>,
    { wrapper },
  );

/** The hint paragraph that opens with `question` in bold. */
const hintOpening = (question: string): HTMLElement => {
  const bold = screen.getByText(question);
  expect(bold.tagName).toBe('B');
  return bold.parentElement as HTMLElement;
};

describe('the Welcome page under a locale', () => {
  it('reads its text from the French catalog under fr-CA', () => {
    renderWelcome(French);

    expect(screen.getByRole('heading', { name: 'Bienvenue dans GenAI IDP' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Faire la visite' })).toBeInTheDocument();

    const hint = hintOpening('Vous avez déjà une configuration?');
    const link = within(hint).getByText('Configuration > Afficher/modifier la configuration');
    expect(link.closest('a')).not.toBeNull();
    expect(hint.textContent).toBe(
      'Vous avez déjà une configuration? Allez à Configuration > Afficher/modifier la configuration pour la revoir ou la mettre à jour.',
    );
  });

  it('reads the inline English under en, whitespace and ">" included', () => {
    renderWelcome(English);

    expect(hintOpening('Already have a config?').textContent).toBe(
      'Already have a config? Go to Configuration > View/Edit Configuration to review or update it.',
    );
  });
});
