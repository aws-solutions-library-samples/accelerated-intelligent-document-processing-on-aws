// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * AddProcessedDocumentsModal — the "From processed documents" source of a test
 * set's Add documents menu. Lists completed Production documents newest first,
 * lets the user pick some, and copies them into the set by key. Documents
 * without ground truth are added unlabeled rather than skipped; the dialog names
 * them before anything is submitted, and warns when they would turn a labeled
 * set unlabeled.
 *
 * The Document List's "Add to test set" action opens the same dialog the other
 * way round: the documents are already chosen (`preselected`) and the set is not,
 * so a test-set picker replaces the fixed target and nothing is listed from the
 * API. Both entry points share the warnings and the one mutation.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  FormField,
  Header,
  Modal,
  Pagination,
  SpaceBetween,
  Select,
  StatusIndicator,
  Table,
  TextFilter,
} from '@cloudscape-design/components';
import type { SelectProps, TableProps } from '@cloudscape-design/components';
import { useCollection } from '@cloudscape-design/collection-hooks';
import { ConsoleLogger } from 'aws-amplify/utils';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';

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

const messages = defineMessages<{
  pickerHeader: NoMessageValues;
  pickerIntro: NoMessageValues;
  testSetLabel: NoMessageValues;
  testSetDescription: NoMessageValues;
  testSetPlaceholder: NoMessageValues;
  testSetsLoading: NoMessageValues;
  testSetsEmpty: NoMessageValues;
  testSetsLoadError: { readonly error: string };
  testSetDocumentCount: { readonly count: number };
  testSetUnavailable: { readonly status: string };
  selectedHeader: NoMessageValues;
  notCompletedHeader: { readonly count: number };
  notCompletedBody: NoMessageValues;
}>({
  pickerHeader: {
    id: 'testStudio.addProcessedDocuments.pickerHeader',
    defaultMessage: 'Add documents to a test set',
    description: 'Header of the add-to-test-set dialog opened from the Document List, before a test set is chosen.',
  },
  pickerIntro: {
    id: 'testStudio.addProcessedDocuments.pickerIntro',
    defaultMessage:
      'Each selected document is copied into the test set together with any ground truth already saved for it. One without ground truth is added unlabeled, so it can be draft-labeled in Test Studio afterwards. Documents already in the set are skipped, so labels reviewed there are kept.',
    description: 'Introduction of the add-to-test-set dialog opened from the Document List, where the documents are already chosen.',
  },
  testSetLabel: {
    id: 'testStudio.addProcessedDocuments.testSetLabel',
    defaultMessage: 'Test set',
    description: 'Label of the field that chooses which test set the selected documents are added to.',
  },
  testSetDescription: {
    id: 'testStudio.addProcessedDocuments.testSetDescription',
    defaultMessage: 'The documents are added to an existing test set. To start a new set, create it in Test Studio first.',
    description: 'Help text under the test set field in the add-to-test-set dialog.',
  },
  testSetPlaceholder: {
    id: 'testStudio.addProcessedDocuments.testSetPlaceholder',
    defaultMessage: 'Choose a test set',
    description: 'Placeholder of the test set field before one is chosen.',
  },
  testSetsLoading: {
    id: 'testStudio.addProcessedDocuments.testSetsLoading',
    defaultMessage: 'Loading test sets',
    description: 'Shown in the test set field while the list of test sets loads.',
  },
  testSetsEmpty: {
    id: 'testStudio.addProcessedDocuments.testSetsEmpty',
    defaultMessage: 'There are no test sets yet. Create one in Test Studio first.',
    description: 'Shown in the test set field when no test set exists.',
  },
  testSetsLoadError: {
    id: 'testStudio.addProcessedDocuments.testSetsLoadError',
    defaultMessage: 'Could not load test sets: {error}',
    description: 'Error shown when the list of test sets fails to load. {error} is the error message from the server.',
  },
  testSetDocumentCount: {
    id: 'testStudio.addProcessedDocuments.testSetDocumentCount',
    defaultMessage: '{count, plural, one {# document} other {# documents}}',
    description: 'Description of a test set option: how many documents the set already holds.',
  },
  testSetUnavailable: {
    id: 'testStudio.addProcessedDocuments.testSetUnavailable',
    defaultMessage: 'Unavailable while its status is {status}',
    description: 'Why a test set option cannot be chosen. {status} is the test set status, such as UPDATING.',
  },
  selectedHeader: {
    id: 'testStudio.addProcessedDocuments.selectedHeader',
    defaultMessage: 'Selected documents',
    description: 'Header of the table listing the documents chosen on the Document List.',
  },
  notCompletedHeader: {
    id: 'testStudio.addProcessedDocuments.notCompletedHeader',
    defaultMessage: '{count, plural, one {# selected document is not included} other {# selected documents are not included}}',
    description: 'Header of a notice listing selected documents that are left out because they have not finished processing.',
  },
  notCompletedBody: {
    id: 'testStudio.addProcessedDocuments.notCompletedBody',
    defaultMessage: 'Only documents that finished processing can be added to a test set.',
    description: 'Body of the notice about selected documents that are left out.',
  },
});

const LABEL_STATE_TEXT: Record<string, string> = {
  labeled: 'labeled',
  draft: 'draft-labeled',
};

export interface ProcessedDocument {
  objectKey: string;
  submitted: string;
  evaluationStatus: string;
  configVersion: string;
}

/** A document chosen before the dialog opened, with the status that decides whether it can be added. */
export interface PreselectedDocument extends ProcessedDocument {
  objectStatus: string;
}

interface TestSetOption {
  id: string;
  name: string;
  fileCount: number;
  labelState: string;
  status: string;
}

interface AddProcessedDocumentsModalProps {
  visible: boolean;
  /** The set to add to. Null together with `preselected` shows a test-set picker instead. */
  testSet: AddDocumentsTarget | null;
  /**
   * Documents already chosen elsewhere (the Document List's selection). When set,
   * nothing is listed from the API: these are offered, all selected, and any that
   * have not finished processing are left out and counted.
   */
  preselected?: readonly PreselectedDocument[];
  onDismiss: () => void;
  onSubmitted: (result: AddDocumentsResult) => void;
}

const plural = (count: number, one: string, many: string): string => (count === 1 ? one : many);

/**
 * Only a finished document can be added: the resolver refuses the whole selection
 * if any key is unfinished, so both entry points filter with this before submitting.
 */
export const isAddableStatus = (status: string | null | undefined): boolean => status === 'COMPLETED';

/** The append guard in the resolver accepts a COMPLETED set only. */
const canTakeDocuments = (set: TestSetOption): boolean => set.status === 'COMPLETED';

export const hasGroundTruth = (doc: ProcessedDocument): boolean => isBaselineAvailable(doc);

export const formatSubmitted = (value: string): string => {
  const date = new Date(value);
  return value && !Number.isNaN(date.getTime()) ? date.toLocaleString() : value || '-';
};

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
  { id: 'configVersion', header: 'Configuration profile', cell: (d) => d.configVersion || '-', sortingField: 'configVersion' },
  { id: 'submitted', header: 'Submitted', cell: (d) => formatSubmitted(d.submitted), sortingField: 'submitted' },
];

const AddProcessedDocumentsModal = ({
  visible,
  testSet,
  preselected,
  onDismiss,
  onSubmitted,
}: AddProcessedDocumentsModalProps): React.JSX.Element => {
  const intl = useIntl();
  // Computed once: the dialog is mounted when it opens, so the selection it was
  // opened with is the one it works on.
  const [initialDocuments] = useState<ProcessedDocument[]>(() =>
    (preselected ?? [])
      .filter((d) => isAddableStatus(d.objectStatus))
      .map(({ objectKey, submitted, evaluationStatus, configVersion }) => ({ objectKey, submitted, evaluationStatus, configVersion })),
  );
  const [documents, setDocuments] = useState<ProcessedDocument[]>(initialDocuments);
  const [nextToken, setNextToken] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [testSets, setTestSets] = useState<TestSetOption[] | null>(null);
  const [testSetsError, setTestSetsError] = useState('');
  const [pickedId, setPickedId] = useState<string | null>(null);
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
        .filter((d): d is NonNullable<typeof d> => d != null && !!d.ObjectKey && isAddableStatus(d.ObjectStatus))
        .map((d) => ({
          objectKey: d.ObjectKey as string,
          submitted: d.InitialEventTime ?? '',
          evaluationStatus: d.EvaluationStatus ?? '',
          configVersion: d.ConfigVersion ?? '',
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

  const isPicking = !testSet && preselected !== undefined;
  const fixedTestSetId = testSet?.id;
  const targetId = fixedTestSetId ?? pickedId;

  useEffect(() => {
    if (!visible || (!fixedTestSetId && !isPicking)) return;
    let cancelled = false;
    if (preselected === undefined) void fetchPage(null);
    client
      .graphql({ query: getTestSets })
      .then((result) => {
        if (cancelled) return;
        setTestSets(
          (result.data.getTestSets || [])
            .filter((t): t is NonNullable<typeof t> => t != null && !!t.id)
            .map((t) => ({
              id: t.id,
              name: t.name,
              fileCount: t.fileCount ?? 0,
              labelState: t.labelState ?? 'unlabeled',
              status: t.status ?? '',
            })),
        );
      })
      .catch((err) => {
        logger.warn('Could not read the test sets', err);
        if (!cancelled) setTestSetsError(getErrorMessage(err));
      });
    return () => {
      cancelled = true;
    };
  }, [visible, fixedTestSetId, isPicking, fetchPage]);

  const { items, collectionProps, filterProps, paginationProps, filteredItemsCount } = useCollection(documents, {
    filtering: {
      empty: loading ? 'Loading documents' : 'No completed documents',
      noMatch: 'No documents match the filter',
    },
    pagination: { pageSize: PAGE_SIZE },
    sorting: { defaultState: { sortingColumn: COLUMNS[3], isDescending: true } },
    selection: { keepSelection: true, trackBy: 'objectKey', defaultSelectedItems: initialDocuments },
  });

  const target = useMemo(() => testSets?.find((t) => t.id === targetId) ?? null, [testSets, targetId]);
  const notCompletedCount = (preselected ?? []).length - initialDocuments.length;
  const testSetOptions: SelectProps.Option[] = (testSets ?? []).map((t) =>
    canTakeDocuments(t)
      ? { value: t.id, label: t.name, description: intl.formatMessage(messages.testSetDocumentCount, { count: t.fileCount }) }
      : { value: t.id, label: t.name, disabled: true, description: intl.formatMessage(messages.testSetUnavailable, { status: t.status }) },
  );
  const pickedOption = testSetOptions.find((o) => o.value === pickedId) ?? null;

  const selected = collectionProps.selectedItems ?? [];
  const selectedCount = selected.length;
  const unlabeled = useMemo(() => selected.filter((d) => !hasGroundTruth(d)).map((d) => d.objectKey), [selected]);
  const unlabeledCount = unlabeled.length;
  const listedUnlabeled = unlabeled.slice(0, MAX_LISTED);
  const unlistedUnlabeled = unlabeledCount - listedUnlabeled.length;
  const losesLabels = !!target && target.fileCount > 0 && target.labelState !== 'unlabeled' && unlabeledCount > 0;
  const targetName = target?.name ?? testSet?.name ?? '';
  const noun = plural(selectedCount, 'document', 'documents');

  const handleSubmit = async () => {
    if (!targetId || selectedCount === 0) return;
    setSubmitting(true);
    setError('');
    try {
      const response = await client.graphql({
        query: addDocumentsToTestSetByKey,
        variables: { testSetId: targetId, objectKeys: selected.map((d) => d.objectKey) },
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
          id: targetId,
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
      header={isPicking ? intl.formatMessage(messages.pickerHeader) : `Add processed documents to "${targetName}"`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={submitting}>
              Cancel
            </Button>
            <Button variant="primary" onClick={handleSubmit} loading={submitting} disabled={selectedCount === 0 || !targetId || submitting}>
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
        {isPicking ? (
          <Box>{intl.formatMessage(messages.pickerIntro)}</Box>
        ) : (
          <Box>
            Pick documents that have finished processing. Each one is copied into the set together with any ground truth already saved for
            it; one without ground truth is added unlabeled so it can be draft-labeled here afterwards. Documents already in this set are
            skipped, so labels reviewed here are kept.
          </Box>
        )}
        {isPicking ? (
          <FormField label={intl.formatMessage(messages.testSetLabel)} description={intl.formatMessage(messages.testSetDescription)}>
            <Select
              selectedOption={pickedOption}
              onChange={({ detail }) => setPickedId(detail.selectedOption.value ?? null)}
              options={testSetOptions}
              placeholder={intl.formatMessage(messages.testSetPlaceholder)}
              statusType={testSetsError ? 'error' : testSets === null ? 'loading' : 'finished'}
              loadingText={intl.formatMessage(messages.testSetsLoading)}
              errorText={testSetsError ? intl.formatMessage(messages.testSetsLoadError, { error: testSetsError }) : undefined}
              empty={intl.formatMessage(messages.testSetsEmpty)}
              disabled={submitting}
            />
          </FormField>
        ) : null}
        {notCompletedCount > 0 ? (
          <Alert type="info" header={intl.formatMessage(messages.notCompletedHeader, { count: notCompletedCount })}>
            {intl.formatMessage(messages.notCompletedBody)}
          </Alert>
        ) : null}
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
              {preselected !== undefined ? intl.formatMessage(messages.selectedHeader) : 'Completed documents'}
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
          <Alert
            type={losesLabels ? 'warning' : 'info'}
            header={
              losesLabels
                ? `"${targetName}" will read as unlabeled`
                : `${unlabeledCount} of ${selectedCount} ${plural(unlabeledCount, 'has', 'have')} no ground truth yet`
            }
          >
            <SpaceBetween size="xxs">
              {losesLabels && target ? (
                <Box>
                  This set is {LABEL_STATE_TEXT[target.labelState] ?? target.labelState}. {unlabeledCount} of {selectedCount} selected{' '}
                  {plural(unlabeledCount, 'document has', 'documents have')} no ground truth, and adding{' '}
                  {plural(unlabeledCount, 'it', 'them')} changes the set&apos;s label state to Unlabeled until{' '}
                  {plural(unlabeledCount, 'it is', 'they are')} draft-labeled. Its existing labels are kept.
                </Box>
              ) : (
                <Box>{plural(unlabeledCount, 'It is', 'They are')} added unlabeled. Generate draft labels for the set afterwards.</Box>
              )}
              <Box variant="small">Without ground truth:</Box>
              <Box>
                <ul aria-label="Documents without ground truth" style={{ margin: 0 }}>
                  {listedUnlabeled.map((key) => (
                    <li key={key}>{key}</li>
                  ))}
                  {unlistedUnlabeled > 0 ? <li>and {unlistedUnlabeled} more</li> : null}
                </ul>
              </Box>
            </SpaceBetween>
          </Alert>
        ) : null}
      </SpaceBetween>
    </Modal>
  );
};

export default AddProcessedDocumentsModal;
