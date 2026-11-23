// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState } from 'react';
import { Box, Button, Spinner, Header, Grid, Container, SpaceBetween, Input, Link } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';
import SafeMarkdown from '../common/SafeMarkdown';

import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';

import { queryKnowledgeBase } from '../../graphql/generated';
import { DOCUMENTS_PATH } from '../../routes/constants';
import useSettingsContext from '../../contexts/settings';
import { describeApiError } from '../../hooks/utils/graphql-error';

const client = generateClient();
const logger = new ConsoleLogger('queryKnowledgeBase');

const messages = defineMessages<{
  question: { readonly question: string };
  queryAction: NoMessageValues;
  placeholder: NoMessageValues;
  placeholderDisabled: NoMessageValues;
  intro: NoMessageValues;
  introDisabled: NoMessageValues;
  title: NoMessageValues;
  submit: NoMessageValues;
}>({
  question: {
    id: 'knowledgeBase.query.question',
    defaultMessage: 'Q: {question}',
    description: 'Bold label above each answer on the Knowledge Base page. {question} is the question as the user typed it.',
  },
  queryAction: {
    id: 'knowledgeBase.query.queryAction',
    defaultMessage: 'query the knowledge base',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when the knowledge base cannot be queried.',
  },
  placeholder: {
    id: 'knowledgeBase.query.placeholder',
    defaultMessage: 'Enter a question to query your document knowledge base.',
    description: 'Placeholder of the question box on the Knowledge Base page.',
  },
  placeholderDisabled: {
    id: 'knowledgeBase.query.placeholderDisabled',
    defaultMessage: 'Document Knowledge Base is set to DISABLED for this GenAIIDP deployment.',
    description:
      'Placeholder of the question box when the deployment has no knowledge base. Keep DISABLED (the deployment setting value) and GenAIIDP (the product name) as they are.',
  },
  intro: {
    id: 'knowledgeBase.query.intro',
    defaultMessage: 'Ask a question below.',
    description: 'Text of the Knowledge Base page before the first question.',
  },
  introDisabled: {
    id: 'knowledgeBase.query.introDisabled',
    defaultMessage:
      'Document Knowledge Base queries are not enabled. Document Knowledge Base is set to DISABLED for this GenAIIDP deployment.',
    description:
      'Text of the Knowledge Base page when the deployment has no knowledge base. Keep DISABLED (the deployment setting value) and GenAIIDP (the product name) as they are.',
  },
  title: {
    id: 'knowledgeBase.query.title',
    defaultMessage: 'Documents Knowledge Base Query Tool',
    description: 'Heading of the Knowledge Base page.',
  },
  submit: {
    id: 'knowledgeBase.query.submit',
    defaultMessage: 'Submit',
    description: 'Button that sends the typed question to the knowledge base.',
  },
});

interface ValueWithLabelProps {
  label: string;
  index: number;
  children: React.ReactNode;
}

interface KbQuery {
  label: string;
  value: string;
}

const ValueWithLabel = ({ label, index, children }: ValueWithLabelProps): React.JSX.Element => {
  const intl = useIntl();
  return (
    <>
      <Box variant="awsui-key-label">
        <span tabIndex={index}>
          {/* The label is markdown source: the bold markers stay outside the message. */}
          <SafeMarkdown rawHtml={false}>{label ? `**${intl.formatMessage(messages.question, { question: label })}**` : ''}</SafeMarkdown>
        </span>
      </Box>
      {children}
    </>
  );
};

interface CustomLinkProps {
  href?: string;
  children: React.ReactNode;
}

const CustomLink = ({ href, children }: CustomLinkProps): React.JSX.Element => {
  const handleClick = (): void => {
    // Handle the link click here
    console.log('Link clicked:', href);
    // You can add your custom navigation logic here
  };

  return (
    <Link href={`#${DOCUMENTS_PATH}/${href}`} onClick={handleClick}>
      {children}
    </Link>
  );
};
export const DocumentsQueryLayout = (): React.JSX.Element => {
  const intl = useIntl();
  const [inputQuery, setInputQuery] = useState('');
  const [meetingKbQueries, setMeetingKbQueries] = useState<KbQuery[]>([]);
  const [meetingKbQueryStatus, setMeetingKbQueryStatus] = useState(false);
  const [kbSessionId, setKbSessionId] = useState('');
  const { settings } = useSettingsContext();

  const getElementByIdAsync = (id: string): Promise<HTMLElement> =>
    new Promise((resolve) => {
      const getElement = () => {
        const element = document.getElementById(id);
        if (element) {
          resolve(element);
        } else {
          requestAnimationFrame(getElement);
        }
      };
      getElement();
    });

  const scrollToBottomOfChat = async (): Promise<void> => {
    const chatDiv = await getElementByIdAsync('chatDiv');
    chatDiv.scrollTop = chatDiv.scrollHeight + 200;
  };

  const getDocumentsQueryResponseFromKB = async (input: string, sessionId: string) => {
    const response = await client.graphql({
      query: queryKnowledgeBase,
      variables: { input, sessionId },
    });
    return response;
  };

  const submitQuery = (query: string): void => {
    if (meetingKbQueryStatus === true) {
      return;
    }

    setMeetingKbQueryStatus(true);

    const responseData = {
      label: query,
      value: '...',
    };
    const currentQueries = meetingKbQueries.concat(responseData);
    setMeetingKbQueries(currentQueries);
    scrollToBottomOfChat();

    logger.debug('Submitting GraphQL query:', query);
    const queryResponse = getDocumentsQueryResponseFromKB(query, kbSessionId);

    // The placeholder row is inserted as '...' above and only ever replaced on
    // success, so an unhandled rejection left the answer showing '...' for ever with
    // nothing in the UI and nothing but an unhandled-rejection warning in the
    // console. queryKnowledgeBase now also requires an assigned Cognito group, which
    // makes a refusal a real outcome rather than a hypothetical one — so the
    // rejection replaces the placeholder with what went wrong.
    const replacePlaceholder = (value: string) => {
      setMeetingKbQueries(currentQueries.map((q) => (q.value === '...' ? { label: q.label, value } : q)));
      scrollToBottomOfChat();
    };

    queryResponse
      .then((r) => {
        const kbResponse = JSON.parse((r as { data: Record<string, unknown> }).data.queryKnowledgeBase as string);
        setKbSessionId(kbResponse.sessionId);
        replacePlaceholder(kbResponse.markdown);
      })
      .catch((err) => {
        logger.error('Error querying the knowledge base', err);
        replacePlaceholder(describeApiError(err, intl.formatMessage(messages.queryAction)));
      });
    setMeetingKbQueryStatus(false);
  };

  const onSubmit = (e: React.FormEvent): boolean => {
    submitQuery(inputQuery);
    setInputQuery('');
    e.preventDefault();
    return true;
  };

  const placeholder =
    (settings as Record<string, unknown>).ShouldUseDocumentKnowledgeBase === 'true'
      ? intl.formatMessage(messages.placeholder)
      : intl.formatMessage(messages.placeholderDisabled);

  const initialMsg =
    (settings as Record<string, unknown>).ShouldUseDocumentKnowledgeBase === 'true' ? (
      <FormattedMessage {...messages.intro} />
    ) : (
      <FormattedMessage {...messages.introDisabled} />
    );
  return (
    <Container
      fitHeight={false}
      header={
        <Header variant="h2">
          <FormattedMessage {...messages.title} />
        </Header>
      }
      /* For future use. :) */
      footer={
        <form onSubmit={onSubmit}>
          <Grid gridDefinition={[{ colspan: { default: 12, xxs: 9 } }, { colspan: { default: 12, xxs: 3 } }] as Record<string, unknown>[]}>
            <Input placeholder={`${placeholder}`} onChange={({ detail }) => setInputQuery(detail.value)} value={inputQuery} />
            <Button {...({ type: 'submit' } as Record<string, unknown>)}>
              <FormattedMessage {...messages.submit} />
            </Button>
          </Grid>
        </form>
      }
    >
      <div id="chatDiv" style={{ overflow: 'hidden', overflowY: 'auto', height: '30em' }}>
        <SpaceBetween size="m">
          {meetingKbQueries.length > 0 ? (
            meetingKbQueries.map((entry, i) => (
              // eslint-disable-next-line react/no-array-index-key
              <ValueWithLabel key={i} index={i} label={entry.label}>
                {entry.value === '...' ? (
                  <div style={{ height: '30px' }}>
                    <Spinner />
                  </div>
                ) : (
                  <SafeMarkdown
                    components={
                      {
                        documentid: CustomLink,
                      } as Record<string, unknown>
                    }
                  >
                    {entry.value}
                  </SafeMarkdown>
                )}
              </ValueWithLabel>
            ))
          ) : (
            <ValueWithLabel key="nosummary" label="" index={0}>
              {initialMsg}
            </ValueWithLabel>
          )}
        </SpaceBetween>
      </div>
    </Container>
  );
};

export default DocumentsQueryLayout;
