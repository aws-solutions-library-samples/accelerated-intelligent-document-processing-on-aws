// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * GenerateDraftLabelsModal — choose what to label, and with which configuration profile.
 *
 * Documents carrying authored ground truth (uploaded or generated) are listed but
 * not selectable, because the server refuses to overwrite them. Prior machine
 * drafts are selectable: replacing a draft is the point of re-running.
 */

import React, { useState, useEffect, useCallback, useMemo } from 'react';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  FormField,
  Header,
  Modal,
  Select,
  SpaceBetween,
  Pagination,
  Spinner,
  Table,
} from '@cloudscape-design/components';
import ConfigRevisionSelector from '../common/ConfigRevisionSelector';
import type { SelectProps } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from '../../api/client-shim';
import { getConfigVersions, getTestSetDocuments } from '../../graphql/generated';
import { renderConfidence, renderLabelSource } from './TestSetDetail';
import type { TestSetDocumentItem } from './TestSetDetail';

const client = generateClient();
const logger = new ConsoleLogger('GenerateDraftLabelsModal');

const ACTIVE_CONFIG = '__active__';

/**
 * Documents per page inside this dialog.
 *
 * Deliberately its own paging rather than borrowing the caller's page: which
 * documents you can *choose* should not depend on where the list behind the
 * dialog happens to be scrolled to. Before this, picking a specific document
 * meant it had to already be on the caller's page — document 75 of 100 was
 * simply unreachable.
 */
const MODAL_PAGE_SIZE = 50;

/** Ground truth the server will not overwrite, so it must not be selectable. */
const isProtected = (doc: TestSetDocumentItem): boolean => Boolean(doc.labelSource) && doc.labelSource !== 'draft-machine';

interface Props {
  visible: boolean;
  testSetId: string;
  /**
   * Documents in the whole set, from the server — used for the header count and
   * to decide whether paging is needed. The dialog fetches its own pages, so it
   * deliberately does NOT take the caller's page: what you can select must not
   * depend on where the list behind the dialog is scrolled to.
   */
  setTotalCount?: number | null;
  onDismiss: () => void;
  onSubmit: (configVersion: string | undefined, objectKeys: string[] | undefined, configRevision?: number) => void;
  submitting?: boolean;
}

const GenerateDraftLabelsModal = ({ visible, testSetId, setTotalCount, onDismiss, onSubmit, submitting }: Props): React.JSX.Element => {
  // null = the profile's current configuration.
  const [configRevision, setConfigRevision] = useState<number | null>(null);
  const [configVersion, setConfigVersion] = useState<SelectProps.Option>({
    label: 'Active configuration',
    value: ACTIVE_CONFIG,
  });
  const [versionOptions, setVersionOptions] = useState<SelectProps.Option[]>([]);
  const [loadingVersions, setLoadingVersions] = useState(false);
  // Selection is keyed by objectKey and kept OUTSIDE the current page, so paging
  // does not silently drop what was already ticked. A Cloudscape Table only knows
  // about the items it is given, so if this lived in the page it would look like
  // the user had deselected everything the moment they turned the page.
  const [selectedByKey, setSelectedByKey] = useState<Map<string, TestSetDocumentItem>>(new Map());
  const [selectAll, setSelectAll] = useState(true);

  // The dialog's own page of documents.
  const [pageDocs, setPageDocs] = useState<TestSetDocumentItem[]>([]);
  const [pageTokens, setPageTokens] = useState<(string | null)[]>([null]);
  const [pageIndex, setPageIndex] = useState(1);
  const [loadingDocs, setLoadingDocs] = useState(false);
  const [docsError, setDocsError] = useState<string | null>(null);

  const selected = useMemo(() => [...selectedByKey.values()], [selectedByKey]);
  // Only the current page can be shown as ticked; the rest of the selection is
  // still counted and still submitted.
  const selectedOnPage = useMemo(() => pageDocs.filter((d) => selectedByKey.has(d.objectKey)), [pageDocs, selectedByKey]);
  const pageLabelable = useMemo(() => pageDocs.filter((d) => !isProtected(d)), [pageDocs]);
  const protectedOnPage = pageDocs.length - pageLabelable.length;
  // True when the dialog is showing part of the set, so no count derived from the
  // page describes what select-all will actually do.
  const isPartialView = typeof setTotalCount === 'number' && setTotalCount > pageDocs.length;
  // S3 continuation tokens are SEQUENTIAL: page 5's token only exists once pages
  // 1-4 have been fetched. So offer one page ahead and no arbitrary jumps —
  // ceil(total / pageSize) would let a user click page 5 and be shown page 1's
  // documents labelled as page 5. Same idiom as the list behind this dialog.
  const hasMorePages = Boolean(pageTokens[pageIndex]);
  // Must track what will actually be submitted: in select-all mode the selection
  // is empty, so keying the replace-warning on it would hide the warning.
  const targeted = selectAll ? pageLabelable : selected;
  const redoCount = useMemo(() => targeted.filter((d) => d.labelSource === 'draft-machine').length, [targeted]);

  const fetchDocPage = useCallback(
    async (index: number, tokens: (string | null)[]) => {
      setLoadingDocs(true);
      setDocsError(null);
      try {
        const response = await client.graphql({
          query: getTestSetDocuments,
          variables: { testSetId, limit: MODAL_PAGE_SIZE, nextToken: tokens[index - 1] ?? undefined },
        });
        const page = response.data?.getTestSetDocuments;
        setPageDocs((page?.documents ?? []) as TestSetDocumentItem[]);
        setPageTokens((prev) => {
          const next = [...prev];
          next[index] = page?.nextToken ?? null;
          return next;
        });
      } catch (err) {
        logger.error('Could not load documents for selection:', err);
        setDocsError('Could not load documents. Close and reopen the dialog to retry.');
      } finally {
        setLoadingDocs(false);
      }
    },
    [testSetId],
  );

  const loadVersions = useCallback(async () => {
    setLoadingVersions(true);
    try {
      const response = await client.graphql({ query: getConfigVersions });
      const versions = response.data?.getConfigVersions?.versions ?? [];
      setVersionOptions([
        { label: 'Active configuration', value: ACTIVE_CONFIG },
        ...versions
          .filter((v): v is NonNullable<typeof v> => Boolean(v?.versionName))
          .map((v) => ({
            label: v.versionName as string,
            value: v.versionName as string,
            description: v.isActive ? 'active' : (v.description ?? undefined),
          })),
      ]);
    } catch (err) {
      logger.error('Could not load configuration profiles:', err);
      setVersionOptions([{ label: 'Active configuration', value: ACTIVE_CONFIG }]);
    } finally {
      setLoadingVersions(false);
    }
  }, []);

  useEffect(() => {
    if (!visible) return;
    loadVersions();
    setSelectAll(true);
    setSelectedByKey(new Map());
    // Always start at page 1: the dialog's paging is its own, so it must not
    // inherit whatever page the list behind it was on.
    setPageTokens([null]);
    setPageIndex(1);
    setPageDocs([]);
    fetchDocPage(1, [null]);
  }, [visible, loadVersions, fetchDocPage]);

  const effectiveKeys = selectAll ? undefined : selected.map((d) => d.objectKey);
  // In select-all mode the SERVER decides the scope, so a count from this page
  // cannot disable the button: on a 100-document set whose first page happens to
  // be entirely protected, pageLabelable.length is 0 and the button read "Nothing
  // to label" while documents 51-100 still needed labels — the same page-vs-set
  // confusion this dialog exists to fix.
  const targetCount = selectAll ? (isPartialView ? (setTotalCount ?? pageLabelable.length) : pageLabelable.length) : selected.length;
  // Option.value is `string | undefined`, so the type guard is required: a
  // non-string reaching the API pins the run to a bogus configuration profile.
  const rawConfigVersion = configVersion.value;
  const selectedConfigVersion = typeof rawConfigVersion === 'string' && rawConfigVersion !== ACTIVE_CONFIG ? rawConfigVersion : undefined;

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      size="large"
      header={
        <Header variant="h2" description={`${testSetId} · ${setTotalCount ?? pageDocs.length} document(s)`}>
          Generate draft labels
        </Header>
      }
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              Cancel
            </Button>
            <Button
              variant="primary"
              loading={submitting}
              disabled={targetCount === 0}
              onClick={() => onSubmit(selectedConfigVersion, effectiveKeys, configRevision ?? undefined)}
            >
              {targetCount === 0
                ? 'Nothing to label'
                : selectAll && isPartialView
                  ? 'Label every document that needs it'
                  : `Label ${targetCount} document(s)`}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="l">
        {pageLabelable.length === 0 && !loadingDocs && !isPartialView && (
          <Alert type="info" header="Every document already has ground truth">
            There is nothing to draft-label. Run a test to score the pipeline against this set instead.
          </Alert>
        )}

        {redoCount > 0 && (
          <Alert type="warning" header={`${redoCount} document(s) will have their draft labels replaced`}>
            These already carry machine-drafted labels. Re-labeling overwrites them — useful for correcting a config mistake, but any draft
            you have not reviewed yet will be discarded. Human-reviewed and uploaded ground truth is never replaced.
          </Alert>
        )}

        <FormField
          label="Configuration profile"
          description="Which configuration to label with. Pick a different profile to compare, or to redo a run that used the wrong settings."
        >
          <Select
            selectedOption={configVersion}
            onChange={({ detail }) => setConfigVersion(detail.selectedOption)}
            options={versionOptions}
            loadingText="Loading configuration profiles"
            statusType={loadingVersions ? 'loading' : 'finished'}
            filteringType="auto"
          />
        </FormField>

        <ConfigRevisionSelector
          profileName={selectedConfigVersion}
          value={configRevision}
          onChange={setConfigRevision}
          description="Defaults to the profile’s current configuration. Labels record which revision drafted them, so a later save cannot change what they were drafted with."
        />

        <FormField label="Documents to label">
          <SpaceBetween size="s">
            {/* The count is shown only when this page IS the set. Otherwise the
                server decides the scope — select-all sends no object keys and it
                walks the whole set — so a number from the page would be wrong in
                the one direction that matters: too small. */}
            <Checkbox checked={selectAll} onChange={({ detail }) => setSelectAll(detail.checked)}>
              {isPartialView
                ? `Extract labels for every document in the set that needs them (of ${setTotalCount})`
                : `Extract labels for every document that needs them (${pageLabelable.length})`}
              {!isPartialView && protectedOnPage > 0 ? ` — skipping ${protectedOnPage} with existing ground truth` : ''}
            </Checkbox>
            {selectAll && isPartialView && (
              <Box variant="small" color="text-body-secondary">
                Documents already carrying reviewed or uploaded ground truth are skipped. The exact number is counted server-side when the
                job starts, and reported on the progress banner.
              </Box>
            )}

            {!selectAll && docsError && <Alert type="error">{docsError}</Alert>}

            {!selectAll && selected.length > 0 && (
              <Box variant="small" color="text-body-secondary">
                {selected.length} document(s) selected
                {isPartialView ? ' across all pages' : ''}. Paging keeps your selection.
              </Box>
            )}

            {!selectAll && (
              <Table
                variant="embedded"
                items={pageDocs}
                loading={loadingDocs}
                loadingText="Loading documents"
                trackBy="objectKey"
                selectionType="multi"
                selectedItems={selectedOnPage}
                onSelectionChange={({ detail }) => {
                  // Merge, never replace: the event reports only THIS page's
                  // ticks, so assigning it would wipe every selection made on
                  // another page.
                  const nowSelected = new Set(
                    (detail.selectedItems as TestSetDocumentItem[]).filter((d) => !isProtected(d)).map((d) => d.objectKey),
                  );
                  setSelectedByKey((prev) => {
                    const next = new Map(prev);
                    pageDocs.forEach((doc) => {
                      if (nowSelected.has(doc.objectKey)) next.set(doc.objectKey, doc);
                      else next.delete(doc.objectKey);
                    });
                    return next;
                  });
                }}
                isItemDisabled={isProtected}
                empty={<Box textAlign="center">No documents.</Box>}
                pagination={
                  hasMorePages || pageIndex > 1 ? (
                    <Pagination
                      currentPageIndex={pageIndex}
                      pagesCount={hasMorePages ? pageIndex + 1 : pageIndex}
                      openEnd={hasMorePages}
                      disabled={loadingDocs}
                      onChange={({ detail }) => {
                        setPageIndex(detail.currentPageIndex);
                        fetchDocPage(detail.currentPageIndex, pageTokens);
                      }}
                    />
                  ) : undefined
                }
                columnDefinitions={[
                  {
                    id: 'name',
                    header: 'Document',
                    cell: (item: TestSetDocumentItem) => item.objectKey,
                  },
                  {
                    id: 'labels',
                    header: 'Extraction labels',
                    cell: (item: TestSetDocumentItem) => renderLabelSource(item.labelSource),
                  },
                  {
                    id: 'confidence',
                    header: 'Confidence',
                    cell: (item: TestSetDocumentItem) => renderConfidence(item.minConfidence, item.confidenceThreshold),
                  },
                  {
                    id: 'note',
                    header: '',
                    cell: (item: TestSetDocumentItem) =>
                      isProtected(item) ? (
                        <Box fontSize="body-s" color="text-body-secondary">
                          Ground truth — not replaceable
                        </Box>
                      ) : item.labelSource === 'draft-machine' ? (
                        <Box fontSize="body-s" color="text-status-warning">
                          Will be replaced
                        </Box>
                      ) : (
                        ''
                      ),
                  },
                ]}
              />
            )}
          </SpaceBetween>
        </FormField>

        {loadingVersions && (
          <Box textAlign="center">
            <Spinner /> Loading configuration profiles…
          </Box>
        )}
      </SpaceBetween>
    </Modal>
  );
};

export default GenerateDraftLabelsModal;
