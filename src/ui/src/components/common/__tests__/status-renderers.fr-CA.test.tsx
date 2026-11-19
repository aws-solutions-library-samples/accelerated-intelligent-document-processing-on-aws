// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Status and Review Status labels under `fr-CA`: with an `intl` for that locale they come
 * from its catalog (an id the fixture lacks falls back to the English), a stored value the UI
 * does not know is shown as it is in every language, and without `intl` both renderers show
 * the English, which for the Status column is the stored value.
 */

import { render, screen } from '@testing-library/react';
import { createIntl } from 'react-intl';
import { afterEach, describe, expect, it } from 'vitest';

import { onIntlError } from '../../../i18n/LocaleProvider';
import { renderHitlStatus } from '../hitl-status-renderer';
import { renderObjectStatus, statusLabel } from '../object-status-renderer';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'documents.status.hitlInProgress': 'Révision humaine en cours',
  'documents.status.completed': 'Terminé',
  'documents.status.unknown': 'Inconnu',
  'documents.reviewStatus.inReview': 'En révision',
  'documents.reviewStatus.inReviewLinkAriaLabel': 'En révision : ouvrir l’élément de la file de révision',
  'documents.reviewStatus.completed': 'Révision terminée',
};

const intl = createIntl({ locale: 'fr-CA', defaultLocale: 'en', messages: FR, onError: onIntlError });

const inReview = { hitlStatus: 'in_review', hitlTriggered: true };

afterEach(() => {
  document.body.innerHTML = '';
});

describe('the document status labels under fr-CA', () => {
  it('spell a stored value out from the French catalog', () => {
    expect(statusLabel('HITL_IN_PROGRESS', intl)).toBe('Révision humaine en cours');
    expect(statusLabel('COMPLETED', intl)).toBe('Terminé');
    expect(statusLabel('', intl)).toBe('Inconnu');
    expect(statusLabel(null, intl)).toBe('Inconnu');
  });

  it('fall back to the stored value for an id the catalog lacks', () => {
    expect(statusLabel('QUEUED', intl)).toBe('QUEUED');
  });

  it('show a value the UI does not know as it is, in every language', () => {
    expect(statusLabel('SOMETHING_NEW', intl)).toBe('SOMETHING_NEW');
    expect(statusLabel('SOMETHING_NEW')).toBe('SOMETHING_NEW');
  });

  it('are the stored value without intl', () => {
    expect(statusLabel('HITL_IN_PROGRESS')).toBe('HITL_IN_PROGRESS');
    expect(statusLabel('')).toBe('Unknown');
  });

  it('render the French label inside the status indicator', () => {
    render(renderObjectStatus('HITL_IN_PROGRESS', intl));
    expect(screen.getByText('Révision humaine en cours')).toBeInTheDocument();
    expect(screen.queryByText('HITL_IN_PROGRESS')).toBeNull();
  });
});

describe('the review status labels under fr-CA', () => {
  it('render the French label, linked or not', () => {
    render(renderHitlStatus(inReview, undefined, intl));
    expect(screen.getByText('En révision')).toBeInTheDocument();
    expect(screen.queryByRole('link')).toBeNull();
    render(renderHitlStatus({ hitlStatus: 'complete', hitlTriggered: true }, undefined, intl));
    expect(screen.getByText('Révision terminée')).toBeInTheDocument();
  });

  it('keep the link to the review queue item under the French label', () => {
    render(renderHitlStatus(inReview, '#/documents/doc.pdf/edit', intl));
    const link = screen.getByRole('link', { name: /^En révision : ouvrir/ });
    expect(link).toHaveAttribute('href', '#/documents/doc.pdf/edit');
    expect(screen.getByText('En révision')).toBeInTheDocument();
  });

  it('are the English without intl', () => {
    render(renderHitlStatus(inReview));
    expect(screen.getByText('In Review')).toBeInTheDocument();
  });
});
