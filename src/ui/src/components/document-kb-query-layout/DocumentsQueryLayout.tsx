// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { Box, Button, Spinner, Header, Grid, Container, SpaceBetween, Input, Link } from '@cloudscape-design/components';
import SafeMarkdown from '../common/SafeMarkdown';

import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';

import { queryKnowledgeBase } from '../../graphql/generated';
import { DOCUMENTS_PATH } from '../../routes/constants';
import useSettingsContext from '../../contexts/settings';
import { describeApiError } from '../../hooks/utils/graphql-error';

const client = generateClient();
const logger = new ConsoleLogger('queryKnowledgeBase');

interface ValueWithLabelProps {
  label: string;
  index: number;
  children: React.ReactNode;
}

interface KbQuery {
  label: string;
  value: string;
}

const ValueWithLabel = ({ label, index, children }: ValueWithLabelProps): React.JSX.Element => (
  <>
    <Box variant="awsui-key-label">
      <span tabIndex={index}>
        <ReactMarkdown>{label ? `**Q: ${label}**` : ''}</ReactMarkdown>
      </span>
    </Box>
    {children}
  </>
);

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
        replacePlaceholder(describeApiError(err, 'query the knowledge base'));
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
      ? 'Enter a question to query your document knowledge base.'
      : 'Document Knowledge Base is set to DISABLED for this GenAIIDP deployment.';

  const initialMsg =
    (settings as Record<string, unknown>).ShouldUseDocumentKnowledgeBase === 'true'
      ? 'Ask a question below.'
      : 'Document Knowledge Base queries are not enabled. Document Knowledge Base is set to DISABLED for this GenAIIDP deployment.';
  return (
    <Container
      fitHeight={false}
      header={<Header variant="h2">Documents Knowledge Base Query Tool</Header>}
      /* For future use. :) */
      footer={
        <form onSubmit={onSubmit}>
          <Grid gridDefinition={[{ colspan: { default: 12, xxs: 9 } }, { colspan: { default: 12, xxs: 3 } }] as Record<string, unknown>[]}>
            <Input placeholder={`${placeholder}`} onChange={({ detail }) => setInputQuery(detail.value)} value={inputQuery} />
            <Button {...({ type: 'submit' } as Record<string, unknown>)}>Submit</Button>
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
            <ValueWithLabel key="nosummary" label="" index={0}>{`${initialMsg}`}</ValueWithLabel>
          )}
        </SpaceBetween>
      </div>
    </Container>
  );
};

export default DocumentsQueryLayout;
