// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The routing rules that send documents to a review queue, as an Admin sees them in two
// places: before deleting the queue (Review Management), and on the queue's page. A
// reference is a routing rule, or a table's default outcome, in a configuration profile: in the
// configuration new documents run ("published"), or in a newer one that is not
// published yet ("current").
import React from 'react';
import { Alert, Box, Button, Container, Header, Link, SpaceBetween, StatusIndicator, Table } from '@cloudscape-design/components';
import type { TableProps } from '@cloudscape-design/components';
import { defineMessages, useIntl, type IntlShape, type NoMessageValues } from 'react-intl';

import type { QueueReference } from '../../graphql/generated/operation-types';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { CONFIGURATION_PATH, configurationRoutingHref } from '../../routes/constants';
import { TableEmptyState } from '../common/table';
import type { QueueReferencesState } from './use-queue-references';

const messages = defineMessages<{
  profileColumn: NoMessageValues;
  revisionColumn: NoMessageValues;
  ruleColumn: NoMessageValues;
  openProfile: { readonly profile: string };
  current: NoMessageValues;
  published: NoMessageValues;
  publishedRevision: { readonly revision: number };
  defaultOutcome: NoMessageValues;
  ruleWithLabel: { readonly label: string; readonly id: string };
  tableLabel: NoMessageValues;
  loading: NoMessageValues;
  checkAction: NoMessageValues;
  retry: NoMessageValues;
  deleteLoading: NoMessageValues;
  deleteErrorHeader: NoMessageValues;
  deleteErrorNote: NoMessageValues;
  deleteHeader: { readonly count: number };
  deleteFallback: NoMessageValues;
  containerHeader: NoMessageValues;
  containerDescription: NoMessageValues;
  containerErrorHeader: NoMessageValues;
  refresh: NoMessageValues;
  emptyTitle: NoMessageValues;
  emptyDescription: NoMessageValues;
  openConfiguration: NoMessageValues;
}>({
  profileColumn: {
    id: 'reviewQueues.references.profileColumn',
    defaultMessage: 'Configuration profile',
    description: 'Column header: the configuration profile whose routing rule sends documents to a review queue.',
  },
  revisionColumn: {
    id: 'reviewQueues.references.revisionColumn',
    defaultMessage: 'Revision',
    description: 'Column header: which configuration of the profile holds the routing rule (the published revision, or the current one).',
  },
  ruleColumn: {
    id: 'reviewQueues.references.ruleColumn',
    defaultMessage: 'Routing rule',
    description: 'Column header: the routing rule that sends documents to a review queue.',
  },
  openProfile: {
    id: 'reviewQueues.references.openProfile',
    defaultMessage: 'Open the routing rules of {profile}',
    description: 'Accessible name of the link from a routing rule reference to the Routing Rules tab of its configuration profile.',
  },
  current: {
    id: 'reviewQueues.references.current',
    defaultMessage: 'current',
    description: 'Revision of a routing rule reference: the newest configuration of the profile, which is not published yet.',
  },
  published: {
    id: 'reviewQueues.references.published',
    defaultMessage: 'published',
    description: 'Revision of a routing rule reference: the configuration new documents run, when it has no revision number.',
  },
  publishedRevision: {
    id: 'reviewQueues.references.publishedRevision',
    defaultMessage: 'published r{revision, number}',
    description: 'Revision of a routing rule reference: the configuration new documents run. {revision} is its revision number.',
  },
  defaultOutcome: {
    id: 'reviewQueues.references.defaultOutcome',
    defaultMessage: 'Default outcome',
    description: 'Shown instead of a routing rule when the default outcome of a routing table is what sends documents to the review queue.',
  },
  ruleWithLabel: {
    id: 'reviewQueues.references.ruleWithLabel',
    defaultMessage: '{label} ({id})',
    description: 'A routing rule that has a label. {label} is its label and {id} its id.',
  },
  tableLabel: {
    id: 'reviewQueues.references.tableLabel',
    defaultMessage: 'Routing rules that use this queue',
    description: 'Accessible name of the table of routing rules that send documents to a review queue.',
  },
  loading: {
    id: 'reviewQueues.references.loading',
    defaultMessage: 'Loading routing rules',
    description: 'Loading text of the table of routing rules that send documents to a review queue.',
  },
  checkAction: {
    id: 'reviewQueues.references.checkAction',
    defaultMessage: 'read the routing rules that use this queue',
    description:
      'Completes the sentences "Failed to …:" and "You do not have permission to …" when the routing rules that use a review queue cannot be read.',
  },
  retry: {
    id: 'reviewQueues.references.retry',
    defaultMessage: 'Retry',
    description: 'Button that reads the routing rules that use a review queue again.',
  },
  deleteLoading: {
    id: 'reviewQueues.references.deleteLoading',
    defaultMessage: 'Checking which routing rules use this queue',
    description: 'Shown in the Delete queue dialog while the routing rules that use the queue are read.',
  },
  deleteErrorHeader: {
    id: 'reviewQueues.references.deleteErrorHeader',
    defaultMessage: "Couldn't check which routing rules use this queue",
    description: 'Heading of the warning in the Delete queue dialog when the routing rules that use the queue cannot be read.',
  },
  deleteErrorNote: {
    id: 'reviewQueues.references.deleteErrorNote',
    defaultMessage: 'You can still delete the queue. Documents a routing rule sends to it will go to the Default queue instead.',
    description: 'Text of the warning in the Delete queue dialog when the routing rules that use the queue cannot be read.',
  },
  deleteHeader: {
    id: 'reviewQueues.references.deleteHeader',
    defaultMessage: '{count, plural, one {# routing rule sends} other {# routing rules send}} documents to this queue',
    description: 'Heading of the warning in the Delete queue dialog that lists the routing rules that use the queue. {count} is how many.',
  },
  deleteFallback: {
    id: 'reviewQueues.references.deleteFallback',
    defaultMessage: 'Documents these routing rules send to this queue will go to the Default queue instead.',
    description: 'Text under the list of routing rules in the Delete queue dialog.',
  },
  containerHeader: {
    id: 'reviewQueues.references.containerHeader',
    defaultMessage: 'Routing rules that use this queue',
    description: "Heading of the section of a review queue's page that lists the routing rules that send documents to it.",
  },
  containerDescription: {
    id: 'reviewQueues.references.containerDescription',
    defaultMessage:
      'The routing rules, in every configuration profile, that send documents to this queue. Change them on the Routing Rules tab of the profile.',
    description: "Text under the heading of the routing rules section of a review queue's page.",
  },
  containerErrorHeader: {
    id: 'reviewQueues.references.containerErrorHeader',
    defaultMessage: "Couldn't load the routing rules that use this queue",
    description: "Heading of the error in the routing rules section of a review queue's page.",
  },
  refresh: {
    id: 'reviewQueues.references.refresh',
    defaultMessage: 'Refresh routing rules',
    description: "Accessible name of the button that reads the routing rules of a review queue's page again.",
  },
  emptyTitle: {
    id: 'reviewQueues.references.emptyTitle',
    defaultMessage: 'No routing rule sends documents to this queue.',
    description: "Shown in the routing rules section of a review queue's page when no routing rule names the queue.",
  },
  emptyDescription: {
    id: 'reviewQueues.references.emptyDescription',
    defaultMessage:
      'To send documents here, add a routing rule on the Routing Rules tab of a configuration profile and choose this queue as its outcome.',
    description: "What to do next, under the empty state of the routing rules section of a review queue's page.",
  },
  openConfiguration: {
    id: 'reviewQueues.references.openConfiguration',
    defaultMessage: 'Open Configuration',
    description: "Button in the empty routing rules section of a review queue's page that opens the configuration page.",
  },
});

/** Which configuration of the profile holds the rule: "published r3", or "current" for a newer, unpublished one. */
export const referenceRevisionText = (intl: IntlShape, reference: Pick<QueueReference, 'published' | 'revision'>): string => {
  if (!reference.published) return intl.formatMessage(messages.current);
  return reference.revision == null
    ? intl.formatMessage(messages.published)
    : intl.formatMessage(messages.publishedRevision, { revision: reference.revision });
};

/** The routing rule as people know it: its label with its id, its id, or "Default outcome" when the table's default names the queue. */
export const referenceRuleText = (intl: IntlShape, reference: Pick<QueueReference, 'ruleId' | 'ruleLabel'>): string => {
  if (!reference.ruleId) return intl.formatMessage(messages.defaultOutcome);
  return reference.ruleLabel
    ? intl.formatMessage(messages.ruleWithLabel, { label: reference.ruleLabel, id: reference.ruleId })
    : reference.ruleId;
};

const referenceKey = (reference: QueueReference): string =>
  `${reference.profile}|${reference.published ? 'published' : 'current'}|${reference.ruleId}`;

interface QueueReferencesTableProps {
  references: readonly QueueReference[];
  loading?: boolean;
  empty?: React.ReactNode;
}

/** Profile (a link to its Routing Rules tab), revision and routing rule, in the order the server returns them. */
export const QueueReferencesTable = ({ references, loading = false, empty }: QueueReferencesTableProps): React.JSX.Element => {
  const intl = useIntl();
  const columnDefinitions: TableProps.ColumnDefinition<QueueReference>[] = [
    {
      id: 'profile',
      header: intl.formatMessage(messages.profileColumn),
      isRowHeader: true,
      cell: (reference) => (
        <Link
          href={configurationRoutingHref(reference.profile)}
          ariaLabel={intl.formatMessage(messages.openProfile, { profile: reference.profile })}
        >
          {reference.profile}
        </Link>
      ),
    },
    { id: 'revision', header: intl.formatMessage(messages.revisionColumn), cell: (reference) => referenceRevisionText(intl, reference) },
    { id: 'rule', header: intl.formatMessage(messages.ruleColumn), cell: (reference) => referenceRuleText(intl, reference) },
  ];
  return (
    <Table<QueueReference>
      variant="embedded"
      ariaLabels={{ tableLabel: intl.formatMessage(messages.tableLabel) }}
      items={references}
      trackBy={referenceKey}
      columnDefinitions={columnDefinitions}
      loading={loading}
      loadingText={intl.formatMessage(messages.loading)}
      wrapLines
      empty={empty}
    />
  );
};

/**
 * What the Delete queue dialog says about the queue's routing rules: nothing when no
 * rule names it, the rules and where their documents will go when some do. A lookup that
 * is running or has failed says so, and does not hold the deletion back.
 */
export const DeleteQueueReferences = ({ state }: { state: QueueReferencesState }): React.JSX.Element | null => {
  const intl = useIntl();
  if (state.error != null) {
    return (
      <Alert
        type="warning"
        header={intl.formatMessage(messages.deleteErrorHeader)}
        action={
          <Button loading={state.loading} onClick={state.reload}>
            {intl.formatMessage(messages.retry)}
          </Button>
        }
      >
        <SpaceBetween size="xs">
          <Box>{describeApiError(state.error, messages.checkAction, intl)}</Box>
          <Box>{intl.formatMessage(messages.deleteErrorNote)}</Box>
        </SpaceBetween>
      </Alert>
    );
  }
  if (state.references == null) {
    return state.loading ? <StatusIndicator type="loading">{intl.formatMessage(messages.deleteLoading)}</StatusIndicator> : null;
  }
  if (state.references.length === 0) return null;
  return (
    <Alert type="warning" header={intl.formatMessage(messages.deleteHeader, { count: state.references.length })}>
      <SpaceBetween size="s">
        <QueueReferencesTable references={state.references} />
        <Box>{intl.formatMessage(messages.deleteFallback)}</Box>
      </SpaceBetween>
    </Alert>
  );
};

/** The "Routing rules that use this queue" section of a queue's page (Admins). */
export const QueueReferencesContainer = ({ state }: { state: QueueReferencesState }): React.JSX.Element => {
  const intl = useIntl();
  return (
    <Container
      header={
        <Header
          variant="h2"
          counter={state.references ? `(${state.references.length})` : undefined}
          description={intl.formatMessage(messages.containerDescription)}
          actions={
            <Button
              iconName="refresh"
              variant="icon"
              ariaLabel={intl.formatMessage(messages.refresh)}
              loading={state.loading}
              onClick={state.reload}
            />
          }
        >
          {intl.formatMessage(messages.containerHeader)}
        </Header>
      }
    >
      {state.error != null ? (
        <Alert
          type="error"
          header={intl.formatMessage(messages.containerErrorHeader)}
          action={<Button onClick={state.reload}>{intl.formatMessage(messages.retry)}</Button>}
        >
          {describeApiError(state.error, messages.checkAction, intl)}
        </Alert>
      ) : (
        <QueueReferencesTable
          references={state.references ?? []}
          loading={state.references == null}
          empty={
            <TableEmptyState
              title={intl.formatMessage(messages.emptyTitle)}
              description={intl.formatMessage(messages.emptyDescription)}
              action={<Button href={`#${CONFIGURATION_PATH}`}>{intl.formatMessage(messages.openConfiguration)}</Button>}
            />
          }
        />
      )}
    </Container>
  );
};
