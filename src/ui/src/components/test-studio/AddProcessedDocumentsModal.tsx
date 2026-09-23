// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * AddProcessedDocumentsModal — the "From processed documents" source of a test
 * set's Add documents menu. Lists completed Production documents newest first,
 * lets the user pick some, and copies them into the set by key. Documents
 * without ground truth are added unlabeled rather than skipped; the dialog names
 * them before anything is submitted, and warns when they would turn a labeled
 * set unlabeled.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Header,
  Modal,
  Pagination,
  SpaceBetween,
  StatusIndicator,
  Table,
  TextFilter,
} from '@cloudscape-design/components';
import type { TableProps } from '@cloudscape-design/components';
import { useCollection } from '@cloudscape-design/collection-hooks';
import { ConsoleLogger } from 'aws-amplify/utils';

import { generateClient } from '../../api/client-shim';
import { addDocumentsToTestSetByKey, getTestSets, listDocuments } from '../../graphql/generated';
import { getErrorMessage } from '../../utils/errorUtils';
import { isBaselineAvailable } from '../document-panel/document-export';
import type { AddDocumentsResult, AddDocumentsTarget } from './AddDocumentsModals';

const logger = new ConsoleLogger('AddProcessedDocumentsModal');
const client = generateClient();

const FETCH_LIMIT = 200;
const PAGE_SIZE = 10;
/** Unlabeled documents named in full before the list collapses to a count. */
const MAX_LISTED = 10;

const LABEL_STATE_TEXT: Record<string, string> = {
  labeled: 'labeled',
  draft: 'draft-labeled',
};

export interface ProcessedDocument {
  objectKey: string;
  submitted: string;
  evaluationStatus: string;
}

interface TargetState {
  fileCount: number;
  labelState: string;
}

interface AddProcessedDocumentsModalProps {
  visible: boolean;
  testSet: AddDocumentsTarget | null;
  onDismiss: () => void;
  onSubmitted: (result: AddDocumentsResult) => void;
}

const plural = (count: number, one: string, many: string): string => (count === 1 ? one : many);

export const hasGroundTruth = (doc: ProcessedDocument): boolean => isBaselineAvailable(doc);

const COLUMNS: TableProps.ColumnDefinition<ProcessedDocument>[] = [
  { id: 'objectKey', header: 'Document', cell: (d) => d.objectKey, sortingField: 'objectKey', isRowHeader: true },
  {
    id: 'groundTruth',
    header: 'Ground truth',
    cell: (d) =>
      hasGroundTruth(d) ? (
        <StatusIndicator type="success">Available</StatusIndicator>
      ) : (
        <StatusIndicator type="info">None yet</StatusIndicator>
      ),
  },
  { id: 'submitted', header: 'Submitted', cell: (d) => d.submitted, sortingField: 'submitted' },
];

const AddProcessedDocumentsModal = ({ visible, testSet, onDismiss, onSubmitted }: AddProcessedDocumentsModalProps): React.JSX.Element => {
  const [documents, setDocuments] = useState<ProcessedDocument[]>([]);
  const [nextToken, setNextToken] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [target, setTarget] = useState<TargetState | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  const fetchPage = useCallback(async (token: string | null) => {
    setLoading(true);
    setLoadError('');
    try {
      const variables: Record<string, unknown> = { limit: FETCH_LIMIT, view: 'PRODUCTION' };
      if (token) variables.nextToken = token;
      const response = await client.graphql({ query: listDocuments, variables });
      const page = response.data?.listDocuments;
      const rows = (page?.Documents ?? [])
        .filter((d): d is NonNullable<typeof d> => d != null && !!d.ObjectKey && d.ObjectStatus === 'COMPLETED')
        .map((d) => ({
          objectKey: d.ObjectKey as string,
          submitted: d.InitialEventTime ?? '',
          evaluationStatus: d.EvaluationStatus ?? '',
        }));
      setDocuments((current) => {
        const seen = new Set(current.map((d) => d.objectKey));
        return [...current, ...rows.filter((r) => !seen.has(r.objectKey))];
      });
      setNextToken(page?.nextToken ?? null);
    } catch (err) {
      logger.error('Could not load processed documents', err);
      setLoadError(getErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  const testSetId = testSet?.id;

  useEffect(() => {
    if (!visible || !testSetId) return;
    let cancelled = false;
    void fetchPage(null);
    client
      .graphql({ query: getTestSets })
      .then((result) => {
        if (cancelled) return;
        const match = (result.data.getTestSets || []).find((t) => t?.id === testSetId);
        if (match) setTarget({ fileCount: match.fileCount ?? 0, labelState: match.labelState ?? 'unlabeled' });
      })
      .catch((err) => logger.warn('Could not read the test set label state', err));
    return () => {
      cancelled = true;
    };
  }, [visible, testSetId, fetchPage]);

  const { items, collectionProps, filterProps, paginationProps, filteredItemsCount } = useCollection(documents, {
    filtering: {
      empty: loading ? 'Loading documents' : 'No completed documents',
      noMatch: 'No documents match the filter',
    },
    pagination: { pageSize: PAGE_SIZE },
    sorting: { defaultState: { sortingColumn: COLUMNS[2], isDescending: true } },
    selection: { keepSelection: true, trackBy: 'objectKey' },
  });

  const selected = collectionProps.selectedItems ?? [];
  const selectedCount = selected.length;
  const unlabeled = useMemo(() => selected.filter((d) => !hasGroundTruth(d)).map((d) => d.objectKey), [selected]);
  const unlabeledCount = unlabeled.length;
  const listedUnlabeled = unlabeled.slice(0, MAX_LISTED);
  const unlistedUnlabeled = unlabeledCount - listedUnlabeled.length;
  const losesLabels = !!target && target.fileCount > 0 && target.labelState !== 'unlabeled' && unlabeledCount > 0;
  const targetName = testSet?.name ?? '';
  const noun = plural(selectedCount, 'document', 'documents');

  const handleSubmit = async () => {
    if (!testSet || selectedCount === 0) return;
    setSubmitting(true);
    setError('');
    try {
      const response = await client.graphql({
        query: addDocumentsToTestSetByKey,
        variables: { testSetId: testSet.id, objectKeys: selected.map((d) => d.objectKey) },
      });
      const updated = response.data?.addDocumentsToTestSetByKey;
      const unlabeledNote =
        unlabeledCount === 0
          ? ''
          : ` ${unlabeledCount} ${plural(unlabeledCount, 'has', 'have')} no ground truth and will read as unlabeled until draft labels are generated.`;
      onSubmitted({
        kind: 'documents',
        message: `Adding ${selectedCount} ${noun} to test set "${targetName}".${unlabeledNote}`,
        testSet: {
          id: testSet.id,
          status: 'UPDATING',
          ...(updated?.fileCount != null ? { fileCount: updated.fileCount } : {}),
        },
      });
    } catch (err) {
      logger.error('Add processed documents failed', err);
      setError(getErrorMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      size="large"
      header={`Add processed documents to "${targetName}"`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={submitting}>
              Cancel
            </Button>
            <Button variant="primary" onClick={handleSubmit} loading={submitting} disabled={selectedCount === 0 || submitting}>
              {selectedCount === 0 ? 'Add documents' : `Add ${selectedCount} ${noun}`}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {error ? (
          <Alert type="error" dismissible onDismiss={() => setError('')}>
            {error}
          </Alert>
        ) : null}
        <Box>
          Pick documents that have finished processing. Each one is copied into the set together with any ground truth already saved for it;
          one without ground truth is added unlabeled so it can be draft-labeled here afterwards.
        </Box>
        {loadError ? <Alert type="error">Could not load documents: {loadError}</Alert> : null}
        <Table
          {...collectionProps}
          items={items}
          columnDefinitions={COLUMNS}
          selectionType="multi"
          trackBy="objectKey"
          variant="embedded"
          loading={loading && documents.length === 0}
          loadingText="Loading documents"
          ariaLabels={{
            selectionGroupLabel: 'Document selection',
            allItemsSelectionLabel: () => 'select all documents on this page',
            itemSelectionLabel: (_, item) => `select ${item.objectKey}`,
          }}
          header={
            <Header
              counter={selectedCount > 0 ? `(${selectedCount}/${documents.length})` : `(${documents.length})`}
              actions={
                nextToken ? (
                  <Button onClick={() => void fetchPage(nextToken)} loading={loading}>
                    Load older documents
                  </Button>
                ) : undefined
              }
            >
              Completed documents
            </Header>
          }
          filter={
            <TextFilter
              {...filterProps}
              filteringPlaceholder="Find documents"
              filteringAriaLabel="Find documents"
              countText={`${filteredItemsCount ?? 0} ${plural(filteredItemsCount ?? 0, 'match', 'matches')}`}
            />
          }
          pagination={<Pagination {...paginationProps} />}
        />
        {unlabeledCount > 0 ? (
          <Alert type="info" header={`${unlabeledCount} of ${selectedCount} ${plural(unlabeledCount, 'has', 'have')} no ground truth yet`}>
            <SpaceBetween size="xs">
              <Box>{plural(unlabeledCount, 'It is', 'They are')} added unlabeled. Generate draft labels for the set afterwards.</Box>
              <ul aria-label="Documents without ground truth">
                {listedUnlabeled.map((key) => (
                  <li key={key}>{key}</li>
                ))}
                {unlistedUnlabeled > 0 ? <li>and {unlistedUnlabeled} more</li> : null}
              </ul>
            </SpaceBetween>
          </Alert>
        ) : null}
        {losesLabels && target ? (
          <Alert type="warning" header={`"${targetName}" will read as unlabeled`}>
            This set is {LABEL_STATE_TEXT[target.labelState] ?? target.labelState}. Adding {unlabeledCount}{' '}
            {plural(unlabeledCount, 'document', 'documents')} without ground truth changes its label state to Unlabeled until{' '}
            {plural(unlabeledCount, 'it is', 'they are')} draft-labeled. Its existing labels are kept.
          </Alert>
        ) : null}
      </SpaceBetween>
    </Modal>
  );
};

export default AddProcessedDocumentsModal;
