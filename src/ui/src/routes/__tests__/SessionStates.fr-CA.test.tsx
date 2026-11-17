// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The notices shown around the app read the catalog of the active locale: under `fr-CA` with a
 * small fixture catalog the session error, the no-role notice and the access-denied notice of
 * RequireRole read French, the reload button of both session notices among them, and the
 * denied notice's link still points home; under `en` the no-role paragraph reads the inline
 * English in one piece, character for character.
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { IntlProvider } from 'react-intl';
import { describe, expect, it, vi } from 'vitest';

import { onIntlError } from '../../i18n/LocaleProvider';
import RequireRole from '../RequireRole';
import { NoRoleAssigned, SessionError } from '../SessionStates';

// A Viewer, so a page gated on Admin is refused.
vi.mock('../../hooks/use-user-role', () => ({ default: () => ({ groups: ['Viewer'], loading: false }) }));

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'routes.session.noRoleHeader': 'Votre compte n’a pas encore reçu d’accès',
  'routes.session.reloadPage': 'Recharger la page',
  'routes.session.signOut': 'Se déconnecter',
  'routes.session.errorHeader': 'Impossible d’établir votre session',
  'routes.requireRole.header': 'Vous n’avez pas accès à cette page',
  'routes.requireRole.homeLink': 'Aller à votre page d’accueil',
};

const English = ({ children }: { children: React.ReactNode }) => (
  <IntlProvider locale="en" defaultLocale="en">
    {children}
  </IntlProvider>
);

const French = ({ children }: { children: React.ReactNode }) => (
  <IntlProvider locale="fr-CA" defaultLocale="en" messages={FR} onError={onIntlError}>
    {children}
  </IntlProvider>
);

/** The alert whose header reads `header`. */
const alertHeaded = (header: string): HTMLElement => {
  const alert = createWrapper(document.body)
    .findAllAlerts()
    .find((a) => a.findHeader()?.getElement().textContent === header);
  expect(alert).toBeDefined();
  return alert!.getElement();
};

describe('the session and access notices under a locale', () => {
  it('read their text from the French catalog under fr-CA', () => {
    render(
      <>
        <SessionError onRetry={() => undefined} />
        <NoRoleAssigned onSignOut={() => undefined} />
        <RequireRole roles={['Admin']}>
          <div>the gated page</div>
        </RequireRole>
      </>,
      { wrapper: French },
    );

    expect(screen.getByText('Impossible d’établir votre session')).toBeInTheDocument();
    expect(screen.getByText('Votre compte n’a pas encore reçu d’accès')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Se déconnecter' })).toBeInTheDocument();
    expect(screen.getByText('Vous n’avez pas accès à cette page')).toBeInTheDocument();
    expect(screen.queryByText('Could not establish your session')).toBeNull();
    expect(screen.queryByText('Your account has not been granted access yet')).toBeNull();
    expect(screen.queryByText("You don't have access to this page")).toBeNull();

    // Both session notices offer the reload, through the one message.
    expect(
      within(alertHeaded('Impossible d’établir votre session')).getByRole('button', { name: 'Recharger la page' }),
    ).toBeInTheDocument();
    expect(
      within(alertHeaded('Votre compte n’a pas encore reçu d’accès')).getByRole('button', { name: 'Recharger la page' }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Recharger la page' })).toHaveLength(2);

    // The denied notice still sends the user home, and the page stays unmounted.
    expect(screen.getByRole('link', { name: 'Aller à votre page d’accueil' })).toHaveAttribute('href', '#/');
    expect(screen.queryByText('the gated page')).toBeNull();
  });

  it('reads the inline English under en, the no-role paragraph in one piece', () => {
    render(<NoRoleAssigned />, { wrapper: English });

    expect(createWrapper(document.body).findAlert()!.findContent().getElement().textContent).toBe(
      'Your sign-in worked, but your account has not been given a role yet, so there is nothing for you to view. Ask an administrator to grant you one — Admin, Author, Reviewer, Annotator or Viewer. If your organization signs you in through its own identity provider, the role comes from your group membership there rather than from this application. Reload this page once it has been granted.',
    );
  });
});
