// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { defineMessages, FormattedMessage, type MessageTag } from 'react-intl';
import { Box, Button, Container, Header, SpaceBetween, Link } from '@cloudscape-design/components';
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

interface WelcomeContentProps {
  // When true, shows the "Don't show this again" dismissal (landing-page variant).
  showDismiss?: boolean;
  onDismiss?: () => void;
}

const messages = defineMessages<{
  noConfigurationQuickStart: { readonly b: MessageTag; readonly quickStart: MessageTag; readonly discovery: MessageTag };
  noConfigurationDiscovery: { readonly b: MessageTag; readonly link: MessageTag };
}>({
  noConfigurationQuickStart: {
    id: 'welcome.content.noConfigurationQuickStart',
    defaultMessage:
      '<b>No configuration yet?</b> Open <quickStart>Quick Start</quickStart>, here or from the side navigation, and describe your document type or upload an example to have a configuration built for you. Or let <discovery>Discovery</discovery> infer classes and fields from example documents.',
    description:
      'Welcome page hint for a user offered Quick Start. <b> wraps the question; <quickStart> wraps a link that opens the Quick Start panel; <discovery> wraps a link to the Discovery page.',
  },
  noConfigurationDiscovery: {
    id: 'welcome.content.noConfigurationDiscovery',
    defaultMessage: '<b>No configuration yet?</b> <link>Discovery</link> infers classes and fields from example documents.',
    description:
      'Welcome page hint for a user who can create a configuration but is not offered Quick Start. <b> wraps the question; <link> wraps a link to the Discovery page.',
  },
});

const bold = (chunks: React.ReactNode[]): React.JSX.Element => <b>{chunks}</b>;

const WelcomeContent = ({ showDismiss = false, onDismiss }: WelcomeContentProps): React.JSX.Element => {
  const navigate = useNavigate();
  const { settings } = useSettingsContext();
  const { isAnnotatorOnly, canWrite } = useUserRole();
  const quickStart = useQuickStartAvailable();

  const openQuickStart = useCallback(() => {
    navigate(DOCUMENTS_PATH);
    window.dispatchEvent(new CustomEvent('openQuickStart'));
  }, [navigate]);

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
        <Header variant="h1" description="Get a working configuration in minutes — or jump straight into the console.">
          Welcome to GenAI IDP
        </Header>
      }
    >
      <SpaceBetween size="l">
        {/* An Annotator is scoped to reading and annotating their assigned test
            set(s). Quick Start drives discovery and upload, the tour ends at Quick
            Start, and Configuration / Upload Document are out of scope too — so
            for them this page would be a set of dead ends. */}
        <SpaceBetween direction="horizontal" size="s">
          {!isAnnotatorOnly && (
            <>
              {quickStart && (
                <Button variant="primary" iconName="gen-ai" onClick={openQuickStart}>
                  Quick Start
                </Button>
              )}
              <Button iconName="status-info" onClick={startTour}>
                Take the tour
              </Button>
            </>
          )}
          <Button variant={isAnnotatorOnly ? 'primary' : undefined} onClick={enterConsole}>
            {isAnnotatorOnly ? 'Go to my annotation queue' : 'Enter IDP Console'}
          </Button>
        </SpaceBetween>

        {!isAnnotatorOnly && (
          <Box>
            <Box variant="h3">Not sure where to begin?</Box>
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
                  <FormattedMessage
                    {...messages.noConfigurationDiscovery}
                    values={{
                      b: bold,
                      link: openDiscovery,
                    }}
                  />
                </Box>
              )}
              <Box variant="p">
                <b>Already have a config?</b> Go to{' '}
                <Link onFollow={() => navigate(CONFIGURATION_PATH)}>Configuration &gt; View/Edit Configuration</Link> to review or update
                it.
              </Box>
              <Box variant="p">
                <b>Want to test a document?</b> Head to <Link onFollow={() => navigate(UPLOAD_DOCUMENT_PATH)}>Upload Documents</Link> to run
                one through your active configuration.
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
              Don&apos;t show this again
            </Link>
          </Box>
        )}
      </SpaceBetween>
    </Container>
  );
};

export default WelcomeContent;
