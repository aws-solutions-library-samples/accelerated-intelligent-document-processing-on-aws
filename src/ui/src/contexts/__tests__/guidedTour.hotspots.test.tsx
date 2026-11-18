// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The guided tour shows only the steps whose hotspot is on the page.
 *
 * Cloudscape cannot show a step whose hotspot is not mounted, and holds the tour on
 * the step before it, so a role whose navigation lacks a page (an Author has no
 * Knowledge Base link) used to stop at step 2. Hotspots register with the tour
 * provider through TourHotspot, and the tour keeps the steps that point at one. The
 * side navigation shows no links until it knows the caller's role, so a started tour
 * waits for them, for a bounded time.
 */

import React from 'react';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { act, render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { IntlProvider, createIntl } from 'react-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AnnotationContextProps } from '@cloudscape-design/components/annotation-context';

import { GuidedTourProvider, TOUR_WAIT_MS, buildTutorial, useGuidedTour } from '../guidedTour';
import { TourHotspot } from '../tourHotspots';

vi.mock('@cloudscape-design/components/annotation-context', () => ({
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
const { hotspotProps } = vi.hoisted(() => ({ hotspotProps: [] as Array<Record<string, unknown>> }));
vi.mock('@cloudscape-design/components/hotspot', () => ({
  default: ({ children, ...props }: { children?: React.ReactNode }) => {
    hotspotProps.push(props);
    return <>{children}</>;
  },
}));

const FLAGS = { customModels: false, capacityPlanning: true, quickStart: false };
const stepIds = (tutorial: AnnotationContextProps.Tutorial | null | undefined): string[] =>
  (tutorial?.tasks ?? []).flatMap((task) => task.steps.map((step) => step.hotspotId));
const intl = createIntl({ locale: 'en', defaultLocale: 'en' });
const EVERY_STEP = stepIds(buildTutorial(intl, FLAGS));

const shown: { tutorial?: AnnotationContextProps.Tutorial | null } = {};
const Probe = (): null => {
  shown.tutorial = useGuidedTour().tutorial;
  return null;
};

const byCopy = (hotspots: string[]): Array<{ id: string; key: string }> => {
  const copies = new Map<string, number>();
  return hotspots.map((id) => {
    const copy = (copies.get(id) ?? 0) + 1;
    copies.set(id, copy);
    return { id, key: `${id}#${copy}` };
  });
};

const Page = ({ hotspots }: { hotspots: string[] }): React.JSX.Element => (
  <IntlProvider locale="en" defaultLocale="en">
    <MemoryRouter>
      <GuidedTourProvider>
        {byCopy(hotspots).map(({ id, key }) => (
          <TourHotspot key={key} hotspotId={id} />
        ))}
        <Probe />
      </GuidedTourProvider>
    </MemoryRouter>
  </IntlProvider>
);

const startTour = (): void => {
  act(() => {
    window.dispatchEvent(new CustomEvent('startTutorial', { detail: FLAGS }));
    vi.advanceTimersByTime(350);
  });
};

describe('the guided tour and the hotspots on the page', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_ENABLE_QUICK_START_WIDGET', 'false');
    vi.useFakeTimers();
    shown.tutorial = undefined;
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllEnvs();
  });

  it('skips the step for a page the navigation does not show', () => {
    const withoutKnowledgeBase = EVERY_STEP.filter((id) => id !== 'nav-document-kb');
    render(<Page hotspots={withoutKnowledgeBase} />);
    startTour();
    expect(stepIds(shown.tutorial)).toEqual(withoutKnowledgeBase);
  });

  it('keeps every step when every hotspot is on the page', () => {
    render(<Page hotspots={EVERY_STEP} />);
    startTour();
    expect(stepIds(shown.tutorial)).toEqual(EVERY_STEP);
  });

  it('forgets a hotspot once the last copy of it leaves the page', () => {
    const { rerender } = render(<Page hotspots={[...EVERY_STEP, 'nav-pricing']} />);
    rerender(<Page hotspots={EVERY_STEP} />);
    startTour();
    expect(stepIds(shown.tutorial)).toContain('nav-pricing');

    rerender(<Page hotspots={EVERY_STEP.filter((id) => id !== 'nav-pricing')} />);
    startTour();
    expect(stepIds(shown.tutorial)).not.toContain('nav-pricing');
  });

  it('waits for the side navigation to show its links, then starts', () => {
    const { rerender } = render(<Page hotspots={[]} />);
    startTour();
    expect(shown.tutorial).toBeNull();

    rerender(<Page hotspots={EVERY_STEP} />);
    expect(stepIds(shown.tutorial)).toEqual(EVERY_STEP);
  });

  it('gives up when no link it points at appears in time', () => {
    const { rerender } = render(<Page hotspots={[]} />);
    startTour();
    act(() => {
      vi.advanceTimersByTime(TOUR_WAIT_MS);
    });

    rerender(<Page hotspots={EVERY_STEP} />);
    expect(shown.tutorial).toBeNull();
  });

  it('passes what it is given on to the Cloudscape hotspot', () => {
    hotspotProps.length = 0;
    render(<TourHotspot hotspotId="nav-pricing" side="left" />);
    expect(hotspotProps).toEqual([{ hotspotId: 'nav-pricing', side: 'left' }]);
  });

  it('starts no tour when no step has a hotspot on the page', () => {
    render(<Page hotspots={[]} />);
    startTour();
    expect(shown.tutorial).toBeNull();
  });

  it('keeps only the steps it is given a hotspot for, and every step without a list', () => {
    expect(stepIds(buildTutorial(intl, FLAGS, new Set(['nav-documents', 'nav-pricing'])))).toEqual(['nav-documents', 'nav-pricing']);
    expect(EVERY_STEP).toContain('nav-document-kb');
  });
});

describe('every hotspot is one the tour can see', () => {
  const SRC = join(__dirname, '..', '..');
  const files = (readdirSync(SRC, { recursive: true }) as string[])
    .filter((file) => /\.tsx?$/.test(file) && !file.includes('__tests__') && file !== join('contexts', 'tourHotspots.tsx'))
    .map((file) => [file, readFileSync(join(SRC, file), 'utf-8')] as const);

  it('reads the source tree', () => {
    expect(files.some(([file]) => file === join('components', 'genaiidp-layout', 'navigation.tsx'))).toBe(true);
  });

  it.each(files.filter(([, source]) => source.includes('Hotspot')))('%s renders hotspots through TourHotspot', (_file, source) => {
    expect(source).not.toMatch(/import\s*\{[^}]*\bHotspot\b[^}]*\}\s*from\s*'@cloudscape-design\/components'/);
    expect(source).not.toMatch(/from\s*'@cloudscape-design\/components\/hotspot'/);
  });
});
