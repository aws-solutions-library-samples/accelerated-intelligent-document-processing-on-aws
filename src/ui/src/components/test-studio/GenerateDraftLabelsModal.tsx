// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * GenerateDraftLabelsModal — choose what to label, and with which config version.
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
  Spinner,
  Table,
} from '@cloudscape-design/components';
import type { SelectProps } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from '../../api/client-shim';
import { getConfigVersions } from '../../graphql/generated';
import { renderConfidence, renderLabelSource } from './TestSetDetail';
import type { TestSetDocumentItem } from './TestSetDetail';

const client = generateClient();
const logger = new ConsoleLogger('GenerateDraftLabelsModal');

const ACTIVE_CONFIG = '__active__';

/** Ground truth the server will not overwrite, so it must not be selectable. */
const isProtected = (doc: TestSetDocumentItem): boolean => Boolean(doc.labelSource) && doc.labelSource !== 'draft-machine';

interface Props {
  visible: boolean;
  testSetId: string;
  documents: TestSetDocumentItem[];
  /**
   * Documents in the whole set, from the server. `documents` is only the page the
   * caller has loaded, so counts derived from it understate a paginated set — a
   * 100-document set was described here as 50.
   */
  setTotalCount?: number | null;
  onDismiss: () => void;
  onSubmit: (configVersion: string | undefined, objectKeys: string[] | undefined) => void;
  submitting?: boolean;
}

const GenerateDraftLabelsModal = ({
  visible,
  testSetId,
  documents,
  setTotalCount,
  onDismiss,
  onSubmit,
  submitting,
}: Props): React.JSX.Element => {
  const [configVersion, setConfigVersion] = useState<SelectProps.Option>({
    label: 'Active configuration',
    value: ACTIVE_CONFIG,
  });
  const [versionOptions, setVersionOptions] = useState<SelectProps.Option[]>([]);
  const [loadingVersions, setLoadingVersions] = useState(false);
  const [selected, setSelected] = useState<TestSetDocumentItem[]>([]);
  const [selectAll, setSelectAll] = useState(true);

  const labelable = useMemo(() => documents.filter((d) => !isProtected(d)), [documents]);
  const protectedCount = documents.length - labelable.length;
  // True when the loaded page is only part of the set, in which case no count
  // derived from `documents` describes what select-all will actually do.
  const isPartialView = typeof setTotalCount === 'number' && setTotalCount > documents.length;
  // Must track what will actually be submitted: in select-all mode `selected` is
  // empty, so keying the replace-warning on it would hide the warning.
  const targeted = selectAll ? labelable : selected;
  const redoCount = useMemo(() => targeted.filter((d) => d.labelSource === 'draft-machine').length, [targeted]);

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
      logger.error('Could not load config versions:', err);
      setVersionOptions([{ label: 'Active configuration', value: ACTIVE_CONFIG }]);
    } finally {
      setLoadingVersions(false);
    }
  }, []);

  useEffect(() => {
    if (!visible) return;
    loadVersions();
    setSelectAll(true);
    setSelected([]);
  }, [visible, loadVersions]);

  const effectiveKeys = selectAll ? undefined : selected.map((d) => d.objectKey);
  const targetCount = selectAll ? labelable.length : selected.length;
  // Option.value is `string | undefined`, so the type guard is required: a
  // non-string reaching the API pins the run to a bogus config version.
  const rawConfigVersion = configVersion.value;
  const selectedConfigVersion = typeof rawConfigVersion === 'string' && rawConfigVersion !== ACTIVE_CONFIG ? rawConfigVersion : undefined;

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      size="large"
      header={
        <Header variant="h2" description={`${testSetId} · ${setTotalCount ?? documents.length} document(s)`}>
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
              onClick={() => onSubmit(selectedConfigVersion, effectiveKeys)}
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
        {labelable.length === 0 && (
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
          label="Configuration version"
          description="Which configuration to label with. Pick a different version to compare, or to redo a run that used the wrong settings."
        >
          <Select
            selectedOption={configVersion}
            onChange={({ detail }) => setConfigVersion(detail.selectedOption)}
            options={versionOptions}
            loadingText="Loading configuration versions"
            statusType={loadingVersions ? 'loading' : 'finished'}
            filteringType="auto"
          />
        </FormField>

        <FormField label="Documents to label">
          <SpaceBetween size="s">
            {/* The count is shown only when this page IS the set. Otherwise the
                server decides the scope — select-all sends no object keys and it
                walks the whole set — so a number from the page would be wrong in
                the one direction that matters: too small. */}
            <Checkbox checked={selectAll} onChange={({ detail }) => setSelectAll(detail.checked)}>
              {isPartialView
                ? `Extract labels for every document in the set that needs them (of ${setTotalCount})`
                : `Extract labels for every document that needs them (${labelable.length})`}
              {!isPartialView && protectedCount > 0 ? ` — skipping ${protectedCount} with existing ground truth` : ''}
            </Checkbox>
            {selectAll && isPartialView && (
              <Box variant="small" color="text-body-secondary">
                Documents already carrying reviewed or uploaded ground truth are skipped. The exact number is counted server-side when the
                job starts, and reported on the progress banner.
              </Box>
            )}

            {!selectAll && isPartialView && (
              <Alert type="info">
                This list is the {documents.length} document(s) currently loaded, of {setTotalCount} in the set. To pick a document that is
                not here, page to it first — or leave the box above checked to cover the whole set.
              </Alert>
            )}

            {!selectAll && (
              <Table
                variant="embedded"
                items={documents}
                trackBy="objectKey"
                selectionType="multi"
                selectedItems={selected}
                onSelectionChange={({ detail }) =>
                  setSelected((detail.selectedItems as TestSetDocumentItem[]).filter((d) => !isProtected(d)))
                }
                isItemDisabled={isProtected}
                empty={<Box textAlign="center">No documents.</Box>}
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
            <Spinner /> Loading configuration versions…
          </Box>
        )}
      </SpaceBetween>
    </Modal>
  );
};

export default GenerateDraftLabelsModal;
