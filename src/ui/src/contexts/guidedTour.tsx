// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { createContext, useContext, useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { defineMessages, useIntl, type IntlShape, type NoMessageValues } from 'react-intl';
import { ConsoleLogger } from 'aws-amplify/utils';
import AnnotationContext from '@cloudscape-design/components/annotation-context';
import type { AnnotationContextProps } from '@cloudscape-design/components/annotation-context';
import { HotspotRegistry, type RegisterHotspot } from './tourHotspots';
import { DOCUMENTS_PATH } from '../routes/constants';
import './guidedTour.css';

const messages = defineMessages<{
  tutorialTitle: NoMessageValues;
  tutorialDescription: NoMessageValues;
  completedWithQuickStart: NoMessageValues;
  completed: NoMessageValues;
  taskFindYourWay: NoMessageValues;
  documentsTitle: NoMessageValues;
  documentsContent: NoMessageValues;
  uploadTitle: NoMessageValues;
  uploadContent: NoMessageValues;
  knowledgeBaseTitle: NoMessageValues;
  knowledgeBaseContent: NoMessageValues;
  agentChatTitle: NoMessageValues;
  agentChatContent: NoMessageValues;
  configurationTitle: NoMessageValues;
  configurationContent: NoMessageValues;
  discoveryTitle: NoMessageValues;
  discoveryContent: NoMessageValues;
  customModelsTitle: NoMessageValues;
  customModelsContent: NoMessageValues;
  capacityPlanningTitle: NoMessageValues;
  capacityPlanningContent: NoMessageValues;
  userManagementTitle: NoMessageValues;
  userManagementContent: NoMessageValues;
  pricingTitle: NoMessageValues;
  pricingContent: NoMessageValues;
  testSetsTitle: NoMessageValues;
  testSetsContent: NoMessageValues;
  testExecutionsTitle: NoMessageValues;
  testExecutionsContent: NoMessageValues;
  extensionsTitle: NoMessageValues;
  extensionsContent: NoMessageValues;
  getStartedTitle: NoMessageValues;
  getStartedContent: NoMessageValues;
  stepCounter: { readonly stepNumber: number; readonly totalStepCount: number };
  taskTitle: { readonly taskNumber: number; readonly taskTitle: string };
  closeAnnotation: NoMessageValues;
  openAnnotation: NoMessageValues;
  next: NoMessageValues;
  previous: NoMessageValues;
  finish: NoMessageValues;
  dismiss: NoMessageValues;
}>({
  tutorialTitle: {
    id: 'guidedTour.tutorial.title',
    defaultMessage: 'Get started with GenAI IDP',
    description: 'Title of the guided tour of the console. "GenAI IDP" is the product name and may stay as it is in the translation.',
  },
  tutorialDescription: {
    id: 'guidedTour.tutorial.description',
    defaultMessage: 'A quick walkthrough of the main areas of the console.',
    description: 'One-sentence description of the guided tour, shown under its title.',
  },
  completedWithQuickStart: {
    id: 'guidedTour.tutorial.completedWithQuickStart',
    defaultMessage: "That's the tour! Open the Quick Start assistant any time to build a configuration or get help.",
    description:
      'Text shown when the guided tour is completed on a deployment where the Quick Start assistant is enabled. "Quick Start" is the name of the assistant.',
  },
  completed: {
    id: 'guidedTour.tutorial.completed',
    defaultMessage: "That's the tour!",
    description: 'Text shown when the guided tour is completed on a deployment where the Quick Start assistant is turned off.',
  },
  taskFindYourWay: {
    id: 'guidedTour.task.findYourWay',
    defaultMessage: 'Find your way around',
    description:
      'Title of the single task of the guided tour, which walks through the side navigation. Shown in the header of every tour step.',
  },
  documentsTitle: {
    id: 'guidedTour.step.documents.title',
    defaultMessage: 'Documents',
    description: 'Title of the guided tour step that points at the Documents entry of the side navigation.',
  },
  documentsContent: {
    id: 'guidedTour.step.documents.content',
    defaultMessage: 'Your processed documents live here — each with status, extracted fields, and confidence scores.',
    description: 'Text of the guided tour step that points at the Documents entry of the side navigation.',
  },
  uploadTitle: {
    id: 'guidedTour.step.upload.title',
    defaultMessage: 'Upload documents',
    description: 'Title of the guided tour step that points at the Upload documents entry of the side navigation.',
  },
  uploadContent: {
    id: 'guidedTour.step.upload.content',
    defaultMessage: 'Upload one or more documents to run them through your active configuration.',
    description: 'Text of the guided tour step that points at the Upload documents entry of the side navigation.',
  },
  knowledgeBaseTitle: {
    id: 'guidedTour.step.knowledgeBase.title',
    defaultMessage: 'Knowledge Base',
    description: 'Title of the guided tour step that points at the document Knowledge Base entry of the side navigation.',
  },
  knowledgeBaseContent: {
    id: 'guidedTour.step.knowledgeBase.content',
    defaultMessage: 'Ask natural-language questions about your processed documents using the document knowledge base.',
    description: 'Text of the guided tour step that points at the document Knowledge Base entry of the side navigation.',
  },
  agentChatTitle: {
    id: 'guidedTour.step.agentChat.title',
    defaultMessage: 'Agent Chat',
    description: 'Title of the guided tour step that points at the Agent Chat entry of the side navigation.',
  },
  agentChatContent: {
    id: 'guidedTour.step.agentChat.content',
    defaultMessage: 'Chat with the IDP assistant for help with analytics, errors, and questions about your documents and the system.',
    description: 'Text of the guided tour step that points at the Agent Chat entry of the side navigation.',
  },
  configurationTitle: {
    id: 'guidedTour.step.configuration.title',
    defaultMessage: 'View / edit configuration',
    description: 'Title of the guided tour step that points at the configuration entry of the side navigation.',
  },
  configurationContent: {
    id: 'guidedTour.step.configuration.content',
    defaultMessage: 'Review, edit, and activate configuration profiles — the document classes and fields IDP extracts.',
    description: 'Text of the guided tour step that points at the configuration entry of the side navigation.',
  },
  discoveryTitle: {
    id: 'guidedTour.step.discovery.title',
    defaultMessage: 'Discovery',
    description: 'Title of the guided tour step that points at the Discovery entry of the side navigation.',
  },
  discoveryContent: {
    id: 'guidedTour.step.discovery.content',
    defaultMessage: 'Infer document classes and a schema automatically from a set of example documents.',
    description: 'Text of the guided tour step that points at the Discovery entry of the side navigation.',
  },
  customModelsTitle: {
    id: 'guidedTour.step.customModels.title',
    defaultMessage: 'Custom Models',
    description: 'Title of the guided tour step that points at the Custom Models entry of the side navigation.',
  },
  customModelsContent: {
    id: 'guidedTour.step.customModels.content',
    defaultMessage: 'Fine-tune specialized classification models on your own document types for higher accuracy.',
    description: 'Text of the guided tour step that points at the Custom Models entry of the side navigation.',
  },
  capacityPlanningTitle: {
    id: 'guidedTour.step.capacityPlanning.title',
    defaultMessage: 'Capacity Planning',
    description: 'Title of the guided tour step that points at the Capacity Planning entry of the side navigation.',
  },
  capacityPlanningContent: {
    id: 'guidedTour.step.capacityPlanning.content',
    defaultMessage: 'Analyze throughput, predict resource needs, and get AWS service-quota recommendations for your workload.',
    description: 'Text of the guided tour step that points at the Capacity Planning entry of the side navigation.',
  },
  userManagementTitle: {
    id: 'guidedTour.step.userManagement.title',
    defaultMessage: 'User Management',
    description: 'Title of the guided tour step that points at the User Management entry of the side navigation.',
  },
  userManagementContent: {
    id: 'guidedTour.step.userManagement.content',
    defaultMessage: 'Manage users and their roles (Admin, Author, Reviewer, Viewer) for the console.',
    description:
      'Text of the guided tour step that points at the User Management entry of the side navigation. The role names in parentheses are the console roles.',
  },
  pricingTitle: {
    id: 'guidedTour.step.pricing.title',
    defaultMessage: 'View / Edit Pricing',
    description: 'Title of the guided tour step that points at the pricing entry of the side navigation.',
  },
  pricingContent: {
    id: 'guidedTour.step.pricing.content',
    defaultMessage: 'Review and adjust the per-service pricing used for document cost estimates.',
    description: 'Text of the guided tour step that points at the pricing entry of the side navigation.',
  },
  testSetsTitle: {
    id: 'guidedTour.step.testSets.title',
    defaultMessage: 'Test sets',
    description: 'Title of the guided tour step that points at the Test sets entry of the side navigation.',
  },
  testSetsContent: {
    id: 'guidedTour.step.testSets.content',
    defaultMessage: 'Manage labeled test sets to measure extraction accuracy.',
    description: 'Text of the guided tour step that points at the Test sets entry of the side navigation.',
  },
  testExecutionsTitle: {
    id: 'guidedTour.step.testExecutions.title',
    defaultMessage: 'Test executions',
    description: 'Title of the guided tour step that points at the Test executions entry of the side navigation.',
  },
  testExecutionsContent: {
    id: 'guidedTour.step.testExecutions.content',
    defaultMessage: 'Run a configuration against a test set and review accuracy and confidence results here.',
    description: 'Text of the guided tour step that points at the Test executions entry of the side navigation.',
  },
  extensionsTitle: {
    id: 'guidedTour.step.extensions.title',
    defaultMessage: 'Extensions',
    description: 'Title of the guided tour step that points at the Extensions entry of the side navigation.',
  },
  extensionsContent: {
    id: 'guidedTour.step.extensions.content',
    defaultMessage:
      'IDP is extensible. Browse and install optional add-ons here — this list grows as new extensions become available, so capabilities can be added without redeploying.',
    description: 'Text of the guided tour step that points at the Extensions entry of the side navigation.',
  },
  getStartedTitle: {
    id: 'guidedTour.step.getStarted.title',
    defaultMessage: 'Get started',
    description: 'Title of the last guided tour step, which points at the Quick Start assistant button.',
  },
  getStartedContent: {
    id: 'guidedTour.step.getStarted.content',
    defaultMessage: 'Open the Quick Start assistant any time to set up a configuration or ask questions. Click Finish to open it now.',
    description:
      'Text of the last guided tour step, which points at the Quick Start assistant button. "Finish" is the label of the button that ends the tour (guidedTour.i18n.finish).',
  },
  stepCounter: {
    id: 'guidedTour.i18n.stepCounter',
    defaultMessage: 'Step {stepNumber} of {totalStepCount}',
    description:
      'Step counter in the footer of a guided tour step. {stepNumber} is the 1-based number of the current step, {totalStepCount} the number of steps in the tour.',
  },
  taskTitle: {
    id: 'guidedTour.i18n.taskTitle',
    defaultMessage: 'Task {taskNumber}: {taskTitle}',
    description:
      'Header of a guided tour step. {taskNumber} is the 1-based number of the task, {taskTitle} its title (guidedTour.task.findYourWay).',
  },
  closeAnnotation: {
    id: 'guidedTour.i18n.closeAnnotation',
    defaultMessage: 'Close annotation',
    description:
      'Screen-reader label of the marker of the current guided tour step while its popover is open; clicking it closes the popover.',
  },
  openAnnotation: {
    id: 'guidedTour.i18n.openAnnotation',
    defaultMessage: 'Open annotation',
    description: 'Screen-reader label of the marker of a guided tour step while its popover is closed; clicking it opens the popover.',
  },
  next: {
    id: 'guidedTour.i18n.next',
    defaultMessage: 'Next',
    description: 'Button of a guided tour step that moves to the next step.',
  },
  previous: {
    id: 'guidedTour.i18n.previous',
    defaultMessage: 'Previous',
    description: 'Button of a guided tour step that moves back to the previous step.',
  },
  finish: {
    id: 'guidedTour.i18n.finish',
    defaultMessage: 'Finish',
    description: 'Button of the last guided tour step that ends the tour.',
  },
  dismiss: {
    id: 'guidedTour.i18n.dismiss',
    defaultMessage: 'Dismiss tour',
    description: 'Screen-reader label of the close (X) button of a guided tour step, which exits the tour.',
  },
});

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

const logger = new ConsoleLogger('GuidedTour');

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
}

const allSteps = (intl: IntlShape, flags: TourFlags): AnnotationContextProps.Tutorial => ({
  title: intl.formatMessage(messages.tutorialTitle),
  description: intl.formatMessage(messages.tutorialDescription),
  completed: false,
  completedScreenDescription: flags.quickStart
    ? intl.formatMessage(messages.completedWithQuickStart)
    : intl.formatMessage(messages.completed),
  tasks: [
    {
      title: intl.formatMessage(messages.taskFindYourWay),
      steps: [
        {
          title: intl.formatMessage(messages.documentsTitle),
          content: intl.formatMessage(messages.documentsContent),
          hotspotId: 'nav-documents',
        },
        {
          title: intl.formatMessage(messages.uploadTitle),
          content: intl.formatMessage(messages.uploadContent),
          hotspotId: 'nav-upload',
        },
        {
          title: intl.formatMessage(messages.knowledgeBaseTitle),
          content: intl.formatMessage(messages.knowledgeBaseContent),
          hotspotId: 'nav-document-kb',
        },
        {
          title: intl.formatMessage(messages.agentChatTitle),
          content: intl.formatMessage(messages.agentChatContent),
          hotspotId: 'nav-agent-chat',
        },
        {
          title: intl.formatMessage(messages.configurationTitle),
          content: intl.formatMessage(messages.configurationContent),
          hotspotId: 'nav-configuration',
        },
        {
          title: intl.formatMessage(messages.discoveryTitle),
          content: intl.formatMessage(messages.discoveryContent),
          hotspotId: 'nav-discovery',
        },
        ...(flags.customModels
          ? [
              {
                title: intl.formatMessage(messages.customModelsTitle),
                content: intl.formatMessage(messages.customModelsContent),
                hotspotId: 'nav-custom-models',
              },
            ]
          : []),
        ...(flags.capacityPlanning
          ? [
              {
                title: intl.formatMessage(messages.capacityPlanningTitle),
                content: intl.formatMessage(messages.capacityPlanningContent),
                hotspotId: 'nav-capacity-planning',
              },
            ]
          : []),
        {
          title: intl.formatMessage(messages.userManagementTitle),
          content: intl.formatMessage(messages.userManagementContent),
          hotspotId: 'nav-user-management',
        },
        {
          title: intl.formatMessage(messages.pricingTitle),
          content: intl.formatMessage(messages.pricingContent),
          hotspotId: 'nav-pricing',
        },
        {
          title: intl.formatMessage(messages.testSetsTitle),
          content: intl.formatMessage(messages.testSetsContent),
          hotspotId: 'nav-test-sets',
        },
        {
          title: intl.formatMessage(messages.testExecutionsTitle),
          content: intl.formatMessage(messages.testExecutionsContent),
          hotspotId: 'nav-test-executions',
        },
        {
          title: intl.formatMessage(messages.extensionsTitle),
          content: intl.formatMessage(messages.extensionsContent),
          hotspotId: 'nav-extensions',
        },
        ...(flags.quickStart
          ? [
              {
                title: intl.formatMessage(messages.getStartedTitle),
                content: intl.formatMessage(messages.getStartedContent),
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
export const buildTutorial = (intl: IntlShape, flags: TourFlags, mounted?: ReadonlySet<string>): AnnotationContextProps.Tutorial => {
  const tutorial = allSteps(intl, flags);
  return mounted ? keepMountedSteps(tutorial, mounted) : tutorial;
};

/**
 * The guided tour. Whether it shows and opens Quick Start is decided by whoever
 * starts it, through the `quickStart` flag. This provider sits above the routes and
 * mounts before the app has configured the session, so a role check here would
 * read no session; the Welcome page starts the tour from inside the signed-in
 * routes, where the role is known.
 */
export const GuidedTourProvider = ({ children }: { children: React.ReactNode }): React.JSX.Element => {
  const intl = useIntl();
  const [tutorial, setTutorial] = useState<AnnotationContextProps.Tutorial | null>(null);
  const opensQuickStartOnFinish = useRef(false);
  const navigate = useNavigate();
  // Formatted once: the aria label of the popover's close button and the click-capture
  // below that watches for it must agree in every language.
  const dismissLabel = intl.formatMessage(messages.dismiss);

  const i18nStrings = useMemo<AnnotationContextProps.I18nStrings>(
    () => ({
      stepCounterText: (stepIndex, totalStepCount) =>
        intl.formatMessage(messages.stepCounter, { stepNumber: stepIndex + 1, totalStepCount }),
      taskTitle: (taskIndex, taskTitle) => intl.formatMessage(messages.taskTitle, { taskNumber: taskIndex + 1, taskTitle }),
      labelHotspot: (openState) => intl.formatMessage(openState ? messages.closeAnnotation : messages.openAnnotation),
      nextButtonText: intl.formatMessage(messages.next),
      previousButtonText: intl.formatMessage(messages.previous),
      finishButtonText: intl.formatMessage(messages.finish),
      labelDismissAnnotation: dismissLabel,
    }),
    [intl, dismissLabel],
  );

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
    const next = buildTutorial(intl, pending.flags, new Set(mountedHotspots.current.keys()));
    if (next.tasks.length === 0) return;
    pendingTour.current = null;
    setTutorial(next);
  }, [intl]);

  useEffect(() => {
    startPendingTour();
  }, [hotspotsMounted, startPendingTour]);

  const startTour = useCallback(
    (flags?: Partial<TourFlags>) => {
      const resolved: TourFlags = {
        customModels: flags?.customModels ?? false,
        capacityPlanning: flags?.capacityPlanning ?? true,
        quickStart: flags?.quickStart ?? false,
      };
      opensQuickStartOnFinish.current = resolved.quickStart;
      navigate(DOCUMENTS_PATH);
      const pending: PendingTour = { flags: resolved, ready: false };
      pendingTour.current = pending;
      window.setTimeout(() => {
        pending.ready = true;
        startPendingTour();
      }, TOUR_DELAY_MS);
      window.setTimeout(() => {
        if (pendingTour.current !== pending) return;
        pendingTour.current = null;
        logger.warn('The guided tour did not start: no step it points at was on the page in time');
      }, TOUR_WAIT_MS);
    },
    [navigate, startPendingTour],
  );
  const exitTour = useCallback(() => setTutorial(null), []);
  const finishTour = useCallback(() => {
    setTutorial(null);
    if (opensQuickStartOnFinish.current) {
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
      if (target?.closest('button')?.getAttribute('aria-label') === dismissLabel) {
        exitTour();
      }
    };
    document.addEventListener('click', onClickCapture, true);
    return () => document.removeEventListener('click', onClickCapture, true);
  }, [tutorial, exitTour, dismissLabel]);

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
