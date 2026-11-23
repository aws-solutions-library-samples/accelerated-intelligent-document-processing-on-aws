// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// The Reprocess dialog, for the document page and for a selection in the document
// list: the configuration profile and revision to run under, and the step the run
// starts at. Which steps can be chosen is the server's answer for the selection under
// that profile and revision (getReprocessOptions): a step is offered when every
// selected document can start there. When the reprocess queued no run for some
// documents, the dialog stays open and lists each document's result and reason.
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { SelectProps, TableProps } from '@cloudscape-design/components';
import { Alert, Box, Button, FormField, Modal, Select, SpaceBetween, StatusIndicator, Table } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';
import type { ReprocessDocumentResult, ReprocessOption } from '../../graphql/generated/operation-types';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import ConfigRevisionSelector from './ConfigRevisionSelector';
import useConfiguration from '../../hooks/use-configuration';
import { isBdaModeConfig } from '../../utils/processing-mode';
import {
  DEFAULT_START_STEP,
  FAILED_STATUS,
  RUN_QUEUED,
  canStartAt,
  combineReprocessOptions,
  notQueued,
} from '../run-plan/reprocess-options';
import { getReprocessOptions } from '../run-plan/run-plan-api';
import StartStepPicker, { reasonMessage } from '../run-plan/StartStepPicker';

const logger = new ConsoleLogger('ReprocessDocumentModal');

const messages = defineMessages<{
  stepsLoading: NoMessageValues;
  stepsErrorHeader: NoMessageValues;
  stepsErrorBody: NoMessageValues;
  retry: NoMessageValues;
  resultsHeader: NoMessageValues;
  notQueuedHeader: { readonly count: number; readonly total: number };
  resultsLabel: NoMessageValues;
  documentColumn: NoMessageValues;
  resultColumn: NoMessageValues;
  reasonColumn: NoMessageValues;
  queued: NoMessageValues;
  refused: NoMessageValues;
  failed: NoMessageValues;
  close: NoMessageValues;
}>({
  stepsLoading: {
    id: 'common.reprocessModal.stepsLoading',
    defaultMessage: 'Checking which steps the run can start at',
    description: 'Shown in the Reprocess dialog while the server is asked which processing steps the selected documents can start at.',
  },
  stepsErrorHeader: {
    id: 'common.reprocessModal.stepsErrorHeader',
    defaultMessage: "Couldn't check the start steps",
    description: 'Heading of the warning in the Reprocess dialog when the steps the selection can start at could not be read.',
  },
  stepsErrorBody: {
    id: 'common.reprocessModal.stepsErrorBody',
    defaultMessage: 'The steps these documents can start at could not be read, so the run starts at the beginning.',
    description: 'Text of the warning in the Reprocess dialog when the steps the selection can start at could not be read.',
  },
  retry: {
    id: 'common.reprocessModal.retry',
    defaultMessage: 'Retry',
    description: 'Button in the Reprocess dialog that asks for the start steps again.',
  },
  resultsHeader: {
    id: 'common.reprocessModal.resultsHeader',
    defaultMessage: 'Reprocess results',
    description: 'Header of the Reprocess dialog once it lists what happened to each document.',
  },
  notQueuedHeader: {
    id: 'common.reprocessModal.notQueuedHeader',
    defaultMessage: '{count, plural, one {# document was} other {# documents were}} not queued, of {total, number}',
    description:
      'Heading of the warning above the per-document results of a reprocess. {count} of the {total} documents asked for were not queued to run again.',
  },
  resultsLabel: {
    id: 'common.reprocessModal.resultsLabel',
    defaultMessage: 'Result for each document',
    description: 'Accessible name of the table of per-document reprocess results.',
  },
  documentColumn: {
    id: 'common.reprocessModal.documentColumn',
    defaultMessage: 'Document',
    description: 'Column header: the document a reprocess was asked for.',
  },
  resultColumn: {
    id: 'common.reprocessModal.resultColumn',
    defaultMessage: 'Result',
    description: 'Column header: whether the document was queued to run again.',
  },
  reasonColumn: {
    id: 'common.reprocessModal.reasonColumn',
    defaultMessage: 'Reason',
    description: 'Column header: why a document was not queued to run again.',
  },
  queued: {
    id: 'common.reprocessModal.queued',
    defaultMessage: 'Queued',
    description: 'Reprocess result of a document: a run was queued for it.',
  },
  refused: {
    id: 'common.reprocessModal.refused',
    defaultMessage: 'Not queued',
    description: 'Reprocess result of a document: the request was refused for it, and nothing about it changed.',
  },
  failed: {
    id: 'common.reprocessModal.failed',
    defaultMessage: 'Failed',
    description: 'Reprocess result of a document: queueing it failed.',
  },
  close: {
    id: 'common.reprocessModal.close',
    defaultMessage: 'Close',
    description: 'Button that closes the Reprocess dialog after it has listed the results.',
  },
});

interface ReprocessDocumentItem {
  objectKey: string;
  /** The document's status; a run can resume only when every selected document failed. */
  objectStatus?: string | null;
}

interface ReprocessDocumentModalProps {
  visible: boolean;
  onDismiss: () => void;
  /**
   * Reprocess the selection. Resolves each document's result, or null when the request
   * itself failed (the caller reports that). The dialog closes when every document was
   * queued, and lists the results when some were not.
   */
  onConfirm: (
    versionName?: string,
    revision?: number,
    startStep?: string,
  ) => Promise<readonly ReprocessDocumentResult[] | null> | readonly ReprocessDocumentResult[] | null;
  selectedItems?: readonly ReprocessDocumentItem[];
  isLoading?: boolean;
}

const ReprocessDocumentModal = ({
  visible,
  onDismiss,
  onConfirm,
  selectedItems = [],
  isLoading = false,
}: ReprocessDocumentModalProps): React.JSX.Element => {
  const intl = useIntl();
  const [selectedVersion, setSelectedVersion] = useState<SelectProps.Option | null>(null);
  const [selectedRevision, setSelectedRevision] = useState<number | null>(null);
  const [startStep, setStartStep] = useState(DEFAULT_START_STEP);
  // The server's answer for the selection under the chosen profile and revision.
  const [answers, setAnswers] = useState<ReprocessOption[] | null>(null);
  const [optionsError, setOptionsError] = useState(false);
  // Each document's result, once a reprocess queued no run for some of them.
  const [results, setResults] = useState<readonly ReprocessDocumentResult[] | null>(null);
  const { versions, getVersionOptions } = useConfigurationVersions();
  // BDA mode is a property of the selected configuration profile (`use_bda`), not of
  // the stack: every stack deploys the unified pattern. `useConfiguration` keeps the
  // previous profile's configuration while a new one loads and after a failed read,
  // so the answer is used only once a load for the selected profile has finished.
  const configVersionName = visible ? ((selectedVersion?.value as string | undefined) ?? null) : null;
  const { mergedConfig: selectedConfig, loading: configLoading, error: configError } = useConfiguration(configVersionName);
  const loadingFor = useRef<string | null>(null);
  const [configLoadedFor, setConfigLoadedFor] = useState<string | null>(null);
  useEffect(() => {
    if (configLoading) {
      loadingFor.current = configVersionName;
    } else if (!configError) {
      setConfigLoadedFor(loadingFor.current);
    }
  }, [configLoading, configError, configVersionName]);
  const isBdaProfile =
    configVersionName !== null && configLoadedFor === configVersionName && !configError && isBdaModeConfig(selectedConfig);

  // Reset selected version when modal closes so it picks up the correct version next time
  useEffect(() => {
    if (!visible) {
      setSelectedVersion(null);
      setSelectedRevision(null);
      setStartStep(DEFAULT_START_STEP);
      setAnswers(null);
      setOptionsError(false);
      setResults(null);
    }
  }, [visible]);

  // The selection as one string, so a new array with the same documents is not a change.
  const selectionKey = selectedItems.map((item) => item.objectKey).join('\n');
  const objectKeys = useMemo(() => (selectionKey ? selectionKey.split('\n') : []), [selectionKey]);
  const versionName = (selectedVersion?.value as string | undefined) ?? null;
  // Only the newest request may answer: an earlier profile's must not.
  const newestOptionsRequest = useRef(0);

  // Ask which steps the selection can start at: when the dialog opens, and again when
  // the profile or the revision changes, since a later start is refused under some.
  const loadOptions = useCallback(async () => {
    newestOptionsRequest.current += 1;
    const mine = newestOptionsRequest.current;
    setAnswers(null);
    setOptionsError(false);
    try {
      const loaded = await getReprocessOptions(objectKeys, versionName ?? undefined, selectedRevision ?? undefined);
      if (mine === newestOptionsRequest.current) setAnswers(loaded);
    } catch (err) {
      logger.error('getReprocessOptions failed', err);
      if (mine === newestOptionsRequest.current) setOptionsError(true);
    }
  }, [objectKeys, versionName, selectedRevision]);

  // There is a profile (none while the dialog is closed) and a selection to ask about.
  const choosing = versionName !== null && objectKeys.length > 0;
  useEffect(() => {
    if (choosing) void loadOptions();
  }, [choosing, loadOptions]);

  const everyDocumentFailed = selectedItems.length > 0 && selectedItems.every((item) => item.objectStatus === FAILED_STATUS);
  const combined = useMemo(
    () => (answers ? combineReprocessOptions(objectKeys, answers, everyDocumentFailed) : null),
    [answers, objectKeys, everyDocumentFailed],
  );
  // A step chosen under one profile may not be available under the next: go back to the beginning.
  useEffect(() => {
    if ((combined && !canStartAt(combined.options, startStep)) || optionsError) setStartStep(DEFAULT_START_STEP);
  }, [combined, optionsError, startStep]);
  // With an answer, the chosen step must be one every document can start at.
  const stepUnavailable = combined !== null && !canStartAt(combined.options, startStep);

  // Set default version when modal opens: prefer document's configVersion, then active, then first scoped
  useEffect(() => {
    if (visible && versions.length > 0 && !selectedVersion) {
      const versionOptions = getVersionOptions();

      // Try to use the document's current ConfigVersion first
      if (selectedItems.length > 0) {
        const docVersion = ((selectedItems[0] as unknown as Record<string, unknown>).configVersion ||
          (selectedItems[0] as unknown as Record<string, unknown>).ConfigVersion) as string | undefined;
        if (docVersion) {
          const docVersionOption = versionOptions.find((option) => option.value === docVersion);
          if (docVersionOption) {
            setSelectedVersion(docVersionOption);
            return;
          }
        }
      }

      // Fallback: active version
      const activeVersion = versions.find((v) => v.isActive);
      if (activeVersion) {
        const activeVersionOption = versionOptions.find((option) => option.value === activeVersion.versionName);
        if (activeVersionOption) {
          setSelectedVersion(activeVersionOption);
          return;
        }
      }

      // Fallback: first available (scoped) version
      if (versionOptions.length > 0) {
        setSelectedVersion(versionOptions[0]);
      }
    }
  }, [visible, versions, selectedVersion, selectedItems, getVersionOptions]);

  let title = 'Reprocess document';
  let message = 'Are you sure you want to reprocess this document?';

  if (selectedItems.length > 1) {
    title = `Reprocess ${selectedItems.length} documents`;
    message = `Are you sure you want to reprocess ${selectedItems.length} documents?`;
  }

  const handleConfirm = async () => {
    logger.debug('Reprocessing documents', selectedItems, 'with version', selectedVersion?.value, 'r', selectedRevision, 'from', startStep);
    const outcome = await onConfirm(selectedVersion?.value, selectedRevision ?? undefined, startStep);
    // Every document queued, or the request failed and the caller said so: nothing more to show.
    if (outcome && notQueued(outcome).length > 0) setResults(outcome);
    else onDismiss();
  };

  if (results) {
    const resultText = (status: string): React.JSX.Element => {
      if (status === RUN_QUEUED) return <StatusIndicator type="success">{intl.formatMessage(messages.queued)}</StatusIndicator>;
      // The server refused the document (REFUSED), or queueing it failed.
      return status === 'REFUSED' ? (
        <StatusIndicator type="warning">{intl.formatMessage(messages.refused)}</StatusIndicator>
      ) : (
        <StatusIndicator type="error">{intl.formatMessage(messages.failed)}</StatusIndicator>
      );
    };
    // The sentence for the server's code; else the server's own message; else the code.
    const reasonText = (result: ReprocessDocumentResult): string => {
      if (result.status === RUN_QUEUED) return '—';
      const sentence = result.reason ? reasonMessage(result.reason) : undefined;
      return sentence ? intl.formatMessage(sentence) : result.message || result.reason || '—';
    };
    const columns: TableProps.ColumnDefinition<ReprocessDocumentResult>[] = [
      { id: 'document', header: intl.formatMessage(messages.documentColumn), cell: (result) => result.objectKey, isRowHeader: true },
      { id: 'result', header: intl.formatMessage(messages.resultColumn), cell: (result) => resultText(result.status) },
      { id: 'reason', header: intl.formatMessage(messages.reasonColumn), cell: reasonText },
    ];
    return (
      <Modal
        visible={visible}
        onDismiss={onDismiss}
        size="large"
        header={intl.formatMessage(messages.resultsHeader)}
        footer={
          <Box float="right">
            <Button variant="primary" onClick={onDismiss}>
              {intl.formatMessage(messages.close)}
            </Button>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Alert
            type="warning"
            header={intl.formatMessage(messages.notQueuedHeader, { count: notQueued(results).length, total: results.length })}
          />
          <Table<ReprocessDocumentResult>
            variant="embedded"
            ariaLabels={{ tableLabel: intl.formatMessage(messages.resultsLabel) }}
            items={results}
            trackBy="objectKey"
            columnDefinitions={columns}
            wrapLines
          />
        </SpaceBetween>
      </Modal>
    );
  }

  return (
    <Modal
      visible={visible}
      onDismiss={isLoading ? undefined : onDismiss}
      header={title}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={isLoading}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={() => void handleConfirm()}
              loading={isLoading}
              disabled={!selectedVersion || stepUnavailable}
            >
              Reprocess
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <p>{message}</p>

        {isBdaProfile && (
          <Alert type="info">
            <strong>NOTE:</strong> To ensure that BDA project blueprints are aligned with your selected configuration profile, be sure to
            execute &quot;Sync To BDA&quot; for your configuration profile from the View/Edit Configuration page.
          </Alert>
        )}

        <FormField label="Configuration Profile" description="Select which configuration profile to use for reprocessing these documents">
          <Select
            selectedOption={selectedVersion}
            onChange={({ detail }) => setSelectedVersion(detail.selectedOption)}
            options={getVersionOptions()}
            placeholder={versions.length === 0 ? 'Loading profiles...' : 'Select configuration profile'}
            disabled={isLoading || versions.length === 0}
            loadingText="Loading profiles..."
          />
        </FormField>

        <ConfigRevisionSelector
          profileName={selectedVersion?.value}
          value={selectedRevision}
          onChange={setSelectedRevision}
          description="Defaults to the profile’s current configuration. Pick an earlier revision to reproduce what an earlier run produced."
          disabled={isLoading}
        />

        {optionsError ? (
          <Alert
            type="warning"
            header={intl.formatMessage(messages.stepsErrorHeader)}
            action={<Button onClick={() => void loadOptions()}>{intl.formatMessage(messages.retry)}</Button>}
          >
            {intl.formatMessage(messages.stepsErrorBody)}
          </Alert>
        ) : combined ? (
          <StartStepPicker
            mode={combined.mode}
            value={startStep}
            onChange={setStartStep}
            options={combined.options}
            includeResume={everyDocumentFailed}
            disabled={isLoading}
          />
        ) : (
          choosing && <StatusIndicator type="loading">{intl.formatMessage(messages.stepsLoading)}</StatusIndicator>
        )}

        <p>This will trigger workflow reprocessing for the following {selectedItems.length > 1 ? 'documents' : 'document'}:</p>
        <ul>
          {selectedItems.map((item) => (
            <li key={item.objectKey}>{item.objectKey}</li>
          ))}
        </ul>
      </SpaceBetween>
    </Modal>
  );
};

export default ReprocessDocumentModal;
