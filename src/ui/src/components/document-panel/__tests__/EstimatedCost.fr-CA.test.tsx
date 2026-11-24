// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Reports › Estimated Cost under `fr-CA`: with a fixture catalog the dialog's title and Close
 * button, the cost table's headers, the None and N/A cells of an unpriced line, a phase's
 * subtotal row, the total row and each phase's prompt-cache note read French, with the note's
 * counts and share in French digits. Metering contexts, services, units and model ids stay as
 * metered. The table reads the stack's pricing into its lookup once: neither the catalog's
 * arrival nor a language switch reads it again. Under `en` the same renders read today's
 * English, with a request count in the singular or the plural.
 */
import React from 'react';
import { screen, waitFor, within } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { I18n } from 'aws-amplify/utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { formatUsDollars } from '../../../i18n/format';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';
import { formatUnitCostUsd } from '../../test-studio/formatCost';

// One value per mocked hook for every render: the table re-derives its price lookup whenever
// `pricing` changes identity, so a fresh object per call never settles. `lookups` records each
// time a panel reads the pricing into its lookup.
const state = vi.hoisted(() => {
  const ready = {
    // 0.000082 per input token of the extraction model; nothing for the classification model.
    pricing: { pricing: [{ name: 'bedrock/us.anthropic.claude-sonnet-4-6', units: [{ name: 'inputTokens', price: '0.000082' }] }] },
    nonChargeableUnits: {},
    loading: false,
  };
  const loadingPricing = { pricing: null, nonChargeableUnits: {}, loading: true };
  return { ready, loadingPricing, pricing: ready as typeof ready | typeof loadingPricing, lookups: vi.fn() };
});

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../hooks/use-pricing', () => ({ default: () => state.pricing }));
vi.mock('../../pricing-layout/price-format', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../pricing-layout/price-format')>();
  return {
    ...actual,
    toPricingLookup: (pricing: unknown) => {
      state.lookups(pricing);
      return actual.toPricingLookup(pricing);
    },
  };
});

import EstimatedCostModal from '../EstimatedCostModal';
import MeteringTable from '../MeteringTable';

const PRICED_MODEL = 'us.anthropic.claude-sonnet-4-6';
const UNPRICED_MODEL = 'us.anthropic.claude-sonnet-4-5';

const METERING = {
  // No price for this model, so each of its lines is unpriced. Nothing read from or written to
  // the cache, over one request: the phase's note says the cache point never cached.
  [`Classification/bedrock/${UNPRICED_MODEL}`]: { inputTokens: 949, cacheReadInputTokens: 0, requests: 1 },
  // 150 input tokens at 0.000082 each, 0.0123, the document's only cost. 1,234 of its 1,384 input
  // tokens were read from the cache, over two requests.
  [`Extraction/bedrock/${PRICED_MODEL}`]: { inputTokens: 150, cacheReadInputTokens: 1234, requests: 2 },
};

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'documents.estimatedCost.title': 'Coût estimé (par page : {cost})',
  'common.reportModal.close': 'Fermer',
  'documents.estimatedCost.loadingPricing': 'Chargement des tarifs...',
  'documents.estimatedCost.column.context': 'Contexte',
  'documents.estimatedCost.column.serviceApi': 'Service/API',
  'documents.estimatedCost.column.unit': 'Unité',
  'documents.estimatedCost.column.value': 'Valeur',
  'documents.estimatedCost.column.unitCost': 'Coût unitaire',
  'documents.estimatedCost.column.cost': 'Coût estimé',
  'documents.estimatedCost.unpricedUnitCost': 'Aucun',
  'documents.estimatedCost.unpricedCost': 'S. O.',
  'documents.estimatedCost.subtotal': 'Sous-total {context}',
  'documents.estimatedCost.total': 'Total général',
  'documents.estimatedCost.costCell': '{label} : {cost}',
  'common.promptCache.headline.caching': 'Cache d’invite : actif ({share, number, ::percent} de l’entrée lue depuis le cache)',
  'common.promptCache.headline.neverCached': 'Cache d’invite : jamais utilisé — le point de cache était inerte',
  'common.promptCache.countsOverRequests':
    '{read, number} lus · {written, number} écrits · {uncached, number} jetons d’entrée hors cache sur {requests, plural, one {# requête} other {# requêtes}}',
  'common.promptCache.detail.neverCachedPhase':
    '{counts}. Soit aucun point de cache n’a atteint le modèle pour cette phase, soit le préfixe de l’invite est inférieur au {floor}.',
  'common.promptCache.floorWithMinimum': 'préfixe minimal pouvant être mis en cache de {models}, soit {min, number} jetons',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

/** Each body row of the cost table, as the text of its cells (the header row has none). */
const bodyRows = () =>
  within(screen.getByRole('table'))
    .getAllByRole('row')
    .map((row) =>
      within(row)
        .queryAllByRole('cell')
        .map((cell) => cell.textContent),
    )
    .filter((cells) => cells.length > 0);

const columnHeaders = () =>
  createWrapper()
    .findTable()!
    .findColumnHeaders()
    .map((header) => header.getElement().textContent);

const modal = () => createWrapper().findModal()!;

beforeEach(() => {
  state.lookups.mockClear();
});

afterEach(() => {
  state.pricing = state.ready;
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('Reports › Estimated Cost under fr-CA', () => {
  it('titles the dialog with the cost per page in French digits, and its Close button in French', async () => {
    renderWithIntl(<EstimatedCostModal meteringData={METERING} pageCount={1} onDismiss={() => undefined} />, FR_CA);
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    expect(await screen.findByText('Fermer')).toBeInTheDocument();
    expect(modal().findHeader().getElement().textContent).toMatch(/^Coût estimé \(par page : 0,0123\sUS\$\)$/);
    expect(modal().findFooter()!.findButton()!.getElement().textContent).toBe('Fermer');
  });

  it('reads the headers, the unpriced cells, the subtotal and total rows in French, and the metering as metered', async () => {
    renderWithIntl(<MeteringTable meteringData={METERING} />, FR_CA);
    expect(await screen.findByText('Contexte')).toBeInTheDocument();
    expect(columnHeaders()).toEqual(['Contexte', 'Service/API', 'Unité', 'Valeur', 'Coût unitaire', 'Coût estimé']);
    const rows = bodyRows();
    // An unpriced line: None and N/A in French; its context, service, unit and value as metered.
    expect(rows).toContainEqual(['Classification', `bedrock/${UNPRICED_MODEL}`, 'inputTokens', '949', 'Aucun', 'S. O.']);
    expect(rows).toContainEqual([
      'Extraction',
      `bedrock/${PRICED_MODEL}`,
      'inputTokens',
      '150',
      formatUnitCostUsd(0.000082, 'fr-CA'),
      formatUsDollars(0.0123, 'fr-CA', { fractionDigits: 4 }),
    ]);
    const costCells = rows.map((cells) => cells[5]);
    expect(costCells).toContain(`Sous-total Extraction : ${formatUsDollars(0.0123, 'fr-CA', { fractionDigits: 4 })}`);
    expect(costCells).toContain(`Total général : ${formatUsDollars(0.0123, 'fr-CA', { fractionDigits: 4 })}`);
    expect(screen.queryByText('None')).toBeNull();
    expect(screen.queryByText('N/A')).toBeNull();
  });

  it("words each phase's prompt-cache note in French, with French digits and a French percent", async () => {
    renderWithIntl(<MeteringTable meteringData={METERING} />, FR_CA);
    expect(await screen.findByText('Contexte')).toBeInTheDocument();
    // Extraction read most of its input from the cache.
    expect(screen.getByText(/^Cache d’invite : actif \(89\s% de l’entrée lue depuis le cache\)$/)).toBeInTheDocument();
    expect(screen.getByText(/^1\s234 lus · 0 écrits · 150 jetons d’entrée hors cache sur 2 requêtes$/)).toBeInTheDocument();
    // Classification never cached; the model id stays as metered.
    expect(screen.getByText('Cache d’invite : jamais utilisé — le point de cache était inerte')).toBeInTheDocument();
    expect(
      screen.getByText(
        new RegExp(
          '^0 lus · 0 écrits · 949 jetons d’entrée hors cache sur 1 requête\\. Soit aucun point de cache n’a atteint le modèle pour ' +
            `cette phase, soit le préfixe de l’invite est inférieur au préfixe minimal pouvant être mis en cache de ${UNPRICED_MODEL.replace(/\./g, '\\.')}, ` +
            'soit 1\\s024 jetons\\.$',
        ),
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Prompt cache/)).toBeNull();
  });

  it('says in French that the pricing is loading', async () => {
    state.pricing = state.loadingPricing;
    renderWithIntl(<MeteringTable meteringData={METERING} />, FR_CA);
    expect(await screen.findByText('Chargement des tarifs...')).toBeInTheDocument();
  });

  // The catalog arrives after the first render, as a new `intl`; neither that nor a language
  // switch may read the pricing into the table's lookup again.
  it('reads the pricing once, and a language switch rewords the table without reading it again', async () => {
    const view = renderWithIntl(<MeteringTable meteringData={METERING} />, FR_CA);
    expect(await screen.findByText('Contexte')).toBeInTheDocument();
    expect(screen.getByText(/^Cache d’invite : actif/)).toBeInTheDocument();
    expect(state.lookups).toHaveBeenCalledTimes(1);
    view.setLocale('en');
    await waitFor(() => expect(screen.getByText('Context')).toBeInTheDocument());
    expect(screen.getByText('Prompt cache: caching (89% of input read from cache)')).toBeInTheDocument();
    expect(state.lookups).toHaveBeenCalledTimes(1);
  });
});

describe('Reports › Estimated Cost under en', () => {
  it("titles the dialog with today's English and its Close button", async () => {
    renderWithIntl(<EstimatedCostModal meteringData={METERING} pageCount={1} onDismiss={() => undefined} />);
    expect(await screen.findByText('Close')).toBeInTheDocument();
    expect(modal().findHeader().getElement().textContent).toBe('Estimated Cost (per page: US$0.0123)');
    expect(modal().findFooter()!.findButton()!.getElement().textContent).toBe('Close');
  });

  it("reads today's headers, unpriced cells, subtotal and total rows", async () => {
    renderWithIntl(<MeteringTable meteringData={METERING} />);
    expect(await screen.findByText('Context')).toBeInTheDocument();
    expect(columnHeaders()).toEqual(['Context', 'Service/Api', 'Unit', 'Value', 'Unit Cost', 'Estimated Cost']);
    const rows = bodyRows();
    expect(rows).toContainEqual(['Classification', `bedrock/${UNPRICED_MODEL}`, 'inputTokens', '949', 'None', 'N/A']);
    expect(rows).toContainEqual([
      'Extraction',
      `bedrock/${PRICED_MODEL}`,
      'inputTokens',
      '150',
      formatUnitCostUsd(0.000082, 'en'),
      'US$0.0123',
    ]);
    const costCells = rows.map((cells) => cells[5]);
    expect(costCells).toContain('Classification Subtotal: US$0.0000');
    expect(costCells).toContain('Extraction Subtotal: US$0.0123');
    expect(costCells).toContain('Total: US$0.0123');
  });

  it("words each phase's prompt-cache note in today's English, with a request count in the singular or the plural", async () => {
    renderWithIntl(<MeteringTable meteringData={METERING} />);
    expect(await screen.findByText('Context')).toBeInTheDocument();
    expect(screen.getByText('Prompt cache: caching (89% of input read from cache)')).toBeInTheDocument();
    expect(screen.getByText('1,234 read · 0 written · 150 uncached input tokens over 2 requests')).toBeInTheDocument();
    expect(screen.getByText('Prompt cache: never cached — the cache point was inert')).toBeInTheDocument();
    expect(
      screen.getByText(
        `0 read · 0 written · 949 uncached input tokens over 1 request. Either no cache point reached the model for this phase or the prompt prefix is below ${UNPRICED_MODEL}'s minimum cacheable prefix of 1,024 tokens.`,
      ),
    ).toBeInTheDocument();
  });

  it("says in today's English that the pricing is loading", async () => {
    state.pricing = state.loadingPricing;
    renderWithIntl(<MeteringTable meteringData={METERING} />);
    expect(await screen.findByText('Loading pricing data...')).toBeInTheDocument();
  });
});
