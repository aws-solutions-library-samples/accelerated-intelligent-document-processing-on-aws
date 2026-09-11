// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { createContext, useContext, useState, useEffect, useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import AnnotationContext from '@cloudscape-design/components/annotation-context';
import type { AnnotationContextProps } from '@cloudscape-design/components/annotation-context';
import { DOCUMENTS_PATH } from '../routes/constants';
import './guidedTour.css';

interface TourFlags {
  customModels: boolean;
  capacityPlanning: boolean;
}

interface GuidedTourContextValue {
  tutorial: AnnotationContextProps.Tutorial | null;
  startTour: (flags?: Partial<TourFlags>) => void;
  exitTour: () => void;
}

const GuidedTourContext = createContext<GuidedTourContextValue | null>(null);

const DISMISS_LABEL = 'Dismiss tour';

const buildTutorial = (flags: TourFlags): AnnotationContextProps.Tutorial => ({
  title: 'Get started with GenAI IDP',
  description: 'A quick walkthrough of the main areas of the console.',
  completed: false,
  completedScreenDescription: "That's the tour! Open the Quick Start assistant any time to build a configuration or get help.",
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
          title: 'Document KB',
          content: 'Ask natural-language questions about your processed documents using the document knowledge base.',
          hotspotId: 'nav-document-kb',
        },
        {
          title: 'Agent Companion Chat',
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
        {
          title: 'Get started',
          content: 'Open the Quick Start assistant any time to set up a configuration or ask questions. ' + 'Click Finish to open it now.',
          hotspotId: 'nav-quick-start',
        },
      ],
    },
  ],
});

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

  const startTour = useCallback(
    (flags?: Partial<TourFlags>) => {
      const resolved: TourFlags = {
        customModels: flags?.customModels ?? false,
        capacityPlanning: flags?.capacityPlanning ?? true,
      };
      navigate(DOCUMENTS_PATH);
      window.setTimeout(() => setTutorial(buildTutorial(resolved)), 350);
    },
    [navigate],
  );
  const exitTour = useCallback(() => setTutorial(null), []);
  const finishTour = useCallback(() => {
    setTutorial(null);
    window.dispatchEvent(new CustomEvent('openQuickStart'));
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
      <AnnotationContext
        currentTutorial={tutorial}
        onStartTutorial={() => startTour()}
        onExitTutorial={() => exitTour()}
        onFinish={() => finishTour()}
        i18nStrings={i18nStrings}
      >
        {children}
      </AnnotationContext>
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
