// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { createContext, useContext, useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import AnnotationContext from '@cloudscape-design/components/annotation-context';
import type { AnnotationContextProps } from '@cloudscape-design/components/annotation-context';
import Hotspot, { type HotspotProps } from '@cloudscape-design/components/hotspot';
import { DOCUMENTS_PATH } from '../routes/constants';
import { isQuickStartEnabled } from '../utils/quick-start-flag';
import './guidedTour.css';

interface TourFlags {
  customModels: boolean;
  capacityPlanning: boolean;
  quickStart: boolean;
}

interface GuidedTourContextValue {
  tutorial: AnnotationContextProps.Tutorial | null;
  startTour: (flags?: Partial<TourFlags>) => void;
  exitTour: () => void;
}

const GuidedTourContext = createContext<GuidedTourContextValue | null>(null);

type RegisterHotspot = (hotspotId: string) => () => void;

const HotspotRegistry = createContext<RegisterHotspot | null>(null);

/**
 * A Cloudscape Hotspot the tour can see. A tour step whose hotspot is not on the page
 * cannot be shown, and Cloudscape then holds the tour on the step before it, so the
 * tour keeps only the steps whose hotspot is mounted. Render every hotspot through
 * this component so that it is counted.
 */
export const TourHotspot = (props: HotspotProps): React.JSX.Element => {
  const register = useContext(HotspotRegistry);
  const { hotspotId } = props;
  useEffect(() => register?.(hotspotId), [register, hotspotId]);
  return <Hotspot {...props} />;
};

const DISMISS_LABEL = 'Dismiss tour';

/** How long the tour waits after navigating, so the page under it has rendered. */
const TOUR_DELAY_MS = 350;

/**
 * How long a started tour waits for the side navigation to show a link it points at.
 * The navigation renders no links until it knows the caller's role, which it reads
 * on every mount, and the Welcome page that starts the tour has no navigation.
 */
export const TOUR_WAIT_MS = 10_000;

interface PendingTour {
  flags: TourFlags;
  ready: boolean;
  giveUpAt: number;
}

const allSteps = (flags: TourFlags): AnnotationContextProps.Tutorial => ({
  title: 'Get started with GenAI IDP',
  description: 'A quick walkthrough of the main areas of the console.',
  completed: false,
  completedScreenDescription: flags.quickStart
    ? "That's the tour! Open the Quick Start assistant any time to build a configuration or get help."
    : "That's the tour!",
  tasks: [
    {
      title: 'Find your way around',
      steps: [
        {
          title: 'Documents',
          content: 'Your processed documents live here — each with status, extracted fields, and confidence scores.',
          hotspotId: 'nav-documents',
        },
        {
          title: 'Upload documents',
          content: 'Upload one or more documents to run them through your active configuration.',
          hotspotId: 'nav-upload',
        },
        {
          title: 'Knowledge Base',
          content: 'Ask natural-language questions about your processed documents using the document knowledge base.',
          hotspotId: 'nav-document-kb',
        },
        {
          title: 'Agent Chat',
          content: 'Chat with the IDP assistant for help with analytics, errors, and questions about your documents and the system.',
          hotspotId: 'nav-agent-chat',
        },
        {
          title: 'View / edit configuration',
          content: 'Review, edit, and activate configuration profiles — the document classes and fields IDP extracts.',
          hotspotId: 'nav-configuration',
        },
        {
          title: 'Discovery',
          content: 'Infer document classes and a schema automatically from a set of example documents.',
          hotspotId: 'nav-discovery',
        },
        ...(flags.customModels
          ? [
              {
                title: 'Custom Models',
                content: 'Fine-tune specialized classification models on your own document types for higher accuracy.',
                hotspotId: 'nav-custom-models',
              },
            ]
          : []),
        ...(flags.capacityPlanning
          ? [
              {
                title: 'Capacity Planning',
                content: 'Analyze throughput, predict resource needs, and get AWS service-quota recommendations for your workload.',
                hotspotId: 'nav-capacity-planning',
              },
            ]
          : []),
        {
          title: 'User Management',
          content: 'Manage users and their roles (Admin, Author, Reviewer, Viewer) for the console.',
          hotspotId: 'nav-user-management',
        },
        {
          title: 'View / Edit Pricing',
          content: 'Review and adjust the per-service pricing used for document cost estimates.',
          hotspotId: 'nav-pricing',
        },
        {
          title: 'Test sets',
          content: 'Manage labeled test sets to measure extraction accuracy.',
          hotspotId: 'nav-test-sets',
        },
        {
          title: 'Test executions',
          content: 'Run a configuration against a test set and review accuracy and confidence results here.',
          hotspotId: 'nav-test-executions',
        },
        {
          title: 'Extensions',
          content:
            'IDP is extensible. Browse and install optional add-ons here — this list grows as new extensions become available, ' +
            'so capabilities can be added without redeploying.',
          hotspotId: 'nav-extensions',
        },
        ...(flags.quickStart
          ? [
              {
                title: 'Get started',
                content:
                  'Open the Quick Start assistant any time to set up a configuration or ask questions. ' + 'Click Finish to open it now.',
                hotspotId: 'nav-quick-start',
              },
            ]
          : []),
      ],
    },
  ],
});

const keepMountedSteps = (tutorial: AnnotationContextProps.Tutorial, mounted: ReadonlySet<string>): AnnotationContextProps.Tutorial => ({
  ...tutorial,
  tasks: tutorial.tasks
    .map((task) => ({ ...task, steps: task.steps.filter((step) => mounted.has(step.hotspotId)) }))
    .filter((task) => task.steps.length > 0),
});

/**
 * The tour for these flags. Given the hotspots on the page, it keeps only the steps
 * that point at one, so a role whose navigation lacks a page skips that page's step.
 * Without them, it keeps every step these flags select.
 */
export const buildTutorial = (flags: TourFlags, mounted?: ReadonlySet<string>): AnnotationContextProps.Tutorial => {
  const tutorial = allSteps(flags);
  return mounted ? keepMountedSteps(tutorial, mounted) : tutorial;
};

const i18nStrings: AnnotationContextProps.I18nStrings = {
  stepCounterText: (stepIndex, totalStepCount) => `Step ${stepIndex + 1} of ${totalStepCount}`,
  taskTitle: (taskIndex, taskTitle) => `Task ${taskIndex + 1}: ${taskTitle}`,
  labelHotspot: (openState) => (openState ? 'Close annotation' : 'Open annotation'),
  nextButtonText: 'Next',
  previousButtonText: 'Previous',
  finishButtonText: 'Finish',
  labelDismissAnnotation: DISMISS_LABEL,
};

export const GuidedTourProvider = ({ children }: { children: React.ReactNode }): React.JSX.Element => {
  const [tutorial, setTutorial] = useState<AnnotationContextProps.Tutorial | null>(null);
  const navigate = useNavigate();
  const mountedHotspots = useRef(new Map<string, number>());
  const pendingTour = useRef<PendingTour | null>(null);
  const [hotspotsMounted, setHotspotsMounted] = useState(0);

  const registerHotspot = useCallback<RegisterHotspot>((hotspotId) => {
    const counts = mountedHotspots.current;
    counts.set(hotspotId, (counts.get(hotspotId) ?? 0) + 1);
    if (pendingTour.current) setHotspotsMounted((mounted) => mounted + 1);
    return () => {
      const left = (counts.get(hotspotId) ?? 1) - 1;
      if (left > 0) counts.set(hotspotId, left);
      else counts.delete(hotspotId);
    };
  }, []);

  const startPendingTour = useCallback(() => {
    const pending = pendingTour.current;
    if (!pending?.ready) return;
    if (Date.now() > pending.giveUpAt) {
      pendingTour.current = null;
      return;
    }
    const next = buildTutorial(pending.flags, new Set(mountedHotspots.current.keys()));
    if (next.tasks.length === 0) return;
    pendingTour.current = null;
    setTutorial(next);
  }, []);

  useEffect(() => {
    startPendingTour();
  }, [hotspotsMounted, startPendingTour]);

  const startTour = useCallback(
    (flags?: Partial<TourFlags>) => {
      const resolved: TourFlags = {
        customModels: flags?.customModels ?? false,
        capacityPlanning: flags?.capacityPlanning ?? true,
        quickStart: isQuickStartEnabled(),
      };
      navigate(DOCUMENTS_PATH);
      const pending: PendingTour = { flags: resolved, ready: false, giveUpAt: Date.now() + TOUR_WAIT_MS };
      pendingTour.current = pending;
      window.setTimeout(() => {
        pending.ready = true;
        startPendingTour();
      }, TOUR_DELAY_MS);
    },
    [navigate, startPendingTour],
  );
  const exitTour = useCallback(() => setTutorial(null), []);
  const finishTour = useCallback(() => {
    setTutorial(null);
    if (isQuickStartEnabled()) {
      window.dispatchEvent(new CustomEvent('openQuickStart'));
    }
  }, []);

  useEffect(() => {
    const handler = (e: Event) => startTour((e as CustomEvent<Partial<TourFlags>>).detail);
    window.addEventListener('startTutorial', handler);
    return () => window.removeEventListener('startTutorial', handler);
  }, [startTour]);

  useEffect(() => {
    if (!tutorial) return undefined;
    const onClickCapture = (e: MouseEvent) => {
      const target = e.target as HTMLElement | null;
      if (target?.closest(`[aria-label="${DISMISS_LABEL}"]`)) {
        exitTour();
      }
    };
    document.addEventListener('click', onClickCapture, true);
    return () => document.removeEventListener('click', onClickCapture, true);
  }, [tutorial, exitTour]);

  const value = useMemo(() => ({ tutorial, startTour, exitTour }), [tutorial, startTour, exitTour]);

  return (
    <GuidedTourContext.Provider value={value}>
      <HotspotRegistry.Provider value={registerHotspot}>
        <AnnotationContext
          currentTutorial={tutorial}
          onStartTutorial={() => startTour()}
          onExitTutorial={() => exitTour()}
          onFinish={() => finishTour()}
          i18nStrings={i18nStrings}
        >
          {children}
        </AnnotationContext>
      </HotspotRegistry.Provider>
    </GuidedTourContext.Provider>
  );
};

export const useGuidedTour = (): GuidedTourContextValue => {
  const ctx = useContext(GuidedTourContext);
  if (!ctx) {
    throw new Error('useGuidedTour must be used within a GuidedTourProvider');
  }
  return ctx;
};

export default GuidedTourContext;
