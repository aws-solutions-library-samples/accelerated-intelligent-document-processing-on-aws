// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { Box, Button, Container, Header, SpaceBetween, Link } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type MessageTag, type NoMessageValues } from 'react-intl';
import {
  DOCUMENTS_PATH,
  DISCOVERY_PATH,
  MY_TASKS_PATH,
  CONFIGURATION_PATH,
  UPLOAD_DOCUMENT_PATH,
  WELCOME_DISMISSED_KEY,
} from '../../routes/constants';
import useSettingsContext from '../../contexts/settings';
import useUserRole from '../../hooks/use-user-role';
import useQuickStartAvailable from '../../hooks/use-quick-start-available';

/** A hint sentence: its opening question in bold, and one phrase that is a link. */
type Hint = { readonly b: MessageTag; readonly link: MessageTag };

const messages = defineMessages<{
  title: NoMessageValues;
  description: NoMessageValues;
  quickStart: NoMessageValues;
  takeTour: NoMessageValues;
  goToQueue: NoMessageValues;
  enterConsole: NoMessageValues;
  helpHeading: NoMessageValues;
  noConfigurationQuickStart: { readonly b: MessageTag; readonly quickStart: MessageTag; readonly discovery: MessageTag };
  noConfigurationDiscovery: Hint;
  haveConfigHint: Hint;
  testDocumentHint: Hint;
  dismiss: NoMessageValues;
}>({
  title: {
    id: 'welcome.page.title',
    defaultMessage: 'Welcome to GenAI IDP',
    description: 'Heading of the Welcome page. "GenAI IDP" is the product name and may stay as it is in the translation.',
  },
  description: {
    id: 'welcome.page.description',
    defaultMessage: 'Get a working configuration in minutes — or jump straight into the console.',
    description: 'Subtitle under the heading of the Welcome page.',
  },
  quickStart: {
    id: 'welcome.page.quickStart',
    defaultMessage: 'Quick Start',
    description:
      'Primary button of the Welcome page that opens the Quick Start chat, which builds a configuration from a description or an example document. The first hint names the same feature in its link.',
  },
  takeTour: {
    id: 'welcome.page.takeTour',
    defaultMessage: 'Take the tour',
    description: 'Button of the Welcome page that starts the guided tour of the console.',
  },
  goToQueue: {
    id: 'welcome.page.goToQueue',
    defaultMessage: 'Go to my annotation queue',
    description: 'Button of the Welcome page for a user who can only annotate; it opens their task queue.',
  },
  enterConsole: {
    id: 'welcome.page.enterConsole',
    defaultMessage: 'Enter IDP Console',
    description:
      'Button of the Welcome page that opens the document list. "IDP" is the product abbreviation and may stay as it is in the translation.',
  },
  helpHeading: {
    id: 'welcome.page.helpHeading',
    defaultMessage: 'Not sure where to begin?',
    description: 'Heading above the hints on the Welcome page.',
  },
  noConfigurationQuickStart: {
    id: 'welcome.page.noConfigurationQuickStart',
    defaultMessage:
      '<b>No configuration yet?</b> Open <quickStart>Quick Start</quickStart> to describe your document type or upload an example. Once you approve, it creates and activates a configuration for you. In the console, Quick Start is also at the top of the side navigation. Or let <discovery>Discovery</discovery> infer classes and fields from example documents.',
    description:
      'First hint on the Welcome page, for a user offered Quick Start. The <b> phrase is the question that opens the hint, shown in bold. The <quickStart> phrase is the name of the Quick Start feature (the same as the Quick Start button) and opens it. The <discovery> phrase is the name of the Discovery page in the side navigation and opens it.',
  },
  noConfigurationDiscovery: {
    id: 'welcome.page.noConfigurationDiscovery',
    defaultMessage: '<b>No configuration yet?</b> <link>Discovery</link> infers classes and fields from example documents.',
    description:
      'First hint on the Welcome page, for a user who can create a configuration but is not offered Quick Start. The <b> phrase is the question that opens the hint, shown in bold. The <link> phrase is the name of the Discovery page in the side navigation and opens it.',
  },
  haveConfigHint: {
    id: 'welcome.page.haveConfigHint',
    defaultMessage: '<b>Already have a config?</b> Go to <link>Configuration > View/Edit Configuration</link> to review or update it.',
    description:
      'Second hint on the Welcome page. The <b> phrase is the question that opens the hint, shown in bold. The <link> phrase is the path to the configuration editor in the side navigation (section > page, with the same names) and opens it.',
  },
  testDocumentHint: {
    id: 'welcome.page.testDocumentHint',
    defaultMessage: '<b>Want to test a document?</b> Head to <link>Upload Documents</link> to run one through your active configuration.',
    description:
      'Third hint on the Welcome page. The <b> phrase is the question that opens the hint, shown in bold. The <link> phrase is the name of the Upload Documents page in the side navigation and opens it.',
  },
  dismiss: {
    id: 'welcome.page.dismiss',
    defaultMessage: "Don't show this again",
    description: 'Link at the bottom of the Welcome page that hides the page on later visits and opens the document list.',
  },
});

interface WelcomeContentProps {
  // When true, shows the "Don't show this again" dismissal (landing-page variant).
  showDismiss?: boolean;
  onDismiss?: () => void;
}

const WelcomeContent = ({ showDismiss = false, onDismiss }: WelcomeContentProps): React.JSX.Element => {
  const intl = useIntl();
  const navigate = useNavigate();
  const { settings } = useSettingsContext();
  const { isAnnotatorOnly, isReviewerOnly, canWrite } = useUserRole();
  const quickStart = useQuickStartAvailable();

  const openQuickStart = useCallback(() => {
    navigate(DOCUMENTS_PATH);
    window.dispatchEvent(new CustomEvent('openQuickStart'));
  }, [navigate]);

  const bold = (chunks: React.ReactNode[]): React.JSX.Element => <b>{chunks}</b>;
  const openDiscovery = (chunks: React.ReactNode[]): React.JSX.Element => <Link onFollow={() => navigate(DISCOVERY_PATH)}>{chunks}</Link>;

  const startTour = useCallback(() => {
    const pattern = (settings?.IDPPattern as string | undefined)?.toLowerCase();
    const detail = {
      customModels: (import.meta.env.VITE_AWS_REGION as string | undefined) === 'us-east-1',
      capacityPlanning: !pattern || /pattern[\s\-_]?2/.test(pattern) || pattern.includes('unified'),
      quickStart,
    };
    window.dispatchEvent(new CustomEvent('startTutorial', { detail }));
  }, [settings?.IDPPattern, quickStart]);

  const enterConsole = useCallback(() => {
    // An Annotator cannot read the document list, so sending them there would be
    // a dead end. Their queue is the equivalent destination.
    navigate(isAnnotatorOnly ? MY_TASKS_PATH : DOCUMENTS_PATH);
  }, [navigate, isAnnotatorOnly]);

  return (
    <Container
      header={
        <Header variant="h1" description={intl.formatMessage(messages.description)}>
          <FormattedMessage {...messages.title} />
        </Header>
      }
    >
      <SpaceBetween size="l">
        {/* An Annotator is scoped to reading and annotating their assigned test
            set(s). Quick Start drives discovery and upload, the tour ends at Quick
            Start, and Configuration / Upload Document are out of scope too — so
            for them this page would be a set of dead ends. A Reviewer-only user's
            side navigation is the review queues alone, which the tour has no step
            for, so they are not offered it. */}
        <SpaceBetween direction="horizontal" size="s">
          {!isAnnotatorOnly && (
            <>
              {quickStart && (
                <Button variant="primary" iconName="gen-ai" onClick={openQuickStart}>
                  <FormattedMessage {...messages.quickStart} />
                </Button>
              )}
              {!isReviewerOnly && (
                <Button iconName="status-info" onClick={startTour}>
                  <FormattedMessage {...messages.takeTour} />
                </Button>
              )}
            </>
          )}
          <Button variant={isAnnotatorOnly ? 'primary' : undefined} onClick={enterConsole}>
            {isAnnotatorOnly ? <FormattedMessage {...messages.goToQueue} /> : <FormattedMessage {...messages.enterConsole} />}
          </Button>
        </SpaceBetween>

        {!isAnnotatorOnly && (
          <Box>
            <Box variant="h3">
              <FormattedMessage {...messages.helpHeading} />
            </Box>
            <SpaceBetween size="xs">
              {quickStart && (
                <Box variant="p">
                  <FormattedMessage
                    {...messages.noConfigurationQuickStart}
                    values={{
                      b: bold,
                      quickStart: (chunks: React.ReactNode[]) => <Link onFollow={openQuickStart}>{chunks}</Link>,
                      discovery: openDiscovery,
                    }}
                  />
                </Box>
              )}
              {!quickStart && canWrite && (
                <Box variant="p">
                  <FormattedMessage {...messages.noConfigurationDiscovery} values={{ b: bold, link: openDiscovery }} />
                </Box>
              )}
              <Box variant="p">
                <FormattedMessage
                  {...messages.haveConfigHint}
                  values={{
                    b: (chunks) => <b>{chunks}</b>,
                    link: (chunks) => <Link onFollow={() => navigate(CONFIGURATION_PATH)}>{chunks}</Link>,
                  }}
                />
              </Box>
              <Box variant="p">
                <FormattedMessage
                  {...messages.testDocumentHint}
                  values={{
                    b: (chunks) => <b>{chunks}</b>,
                    link: (chunks) => <Link onFollow={() => navigate(UPLOAD_DOCUMENT_PATH)}>{chunks}</Link>,
                  }}
                />
              </Box>
            </SpaceBetween>
          </Box>
        )}

        {showDismiss && (
          <Box>
            <Link
              onFollow={() => {
                try {
                  localStorage.setItem(WELCOME_DISMISSED_KEY, 'true');
                } catch {
                  /* ignore */
                }
                onDismiss?.();
                navigate(DOCUMENTS_PATH);
              }}
            >
              <FormattedMessage {...messages.dismiss} />
            </Link>
          </Box>
        )}
      </SpaceBetween>
    </Container>
  );
};

export default WelcomeContent;
