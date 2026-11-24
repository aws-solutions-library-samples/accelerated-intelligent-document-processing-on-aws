// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState, useMemo } from 'react';
import type { MultiselectProps } from '@cloudscape-design/components';
import {
  Box,
  SpaceBetween,
  Container,
  Header,
  Button,
  ColumnLayout,
  Alert,
  Textarea,
  Badge,
  Toggle,
  Spinner,
  Multiselect,
} from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { defineMessages, FormattedMessage, useIntl, type MessageTag, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';

import { valueSourceLabel } from '../common/labels';

const messages = defineMessages<{
  sectionInferenceResult: NoMessageValues;
  sectionDocumentClass: NoMessageValues;
  sectionSplitDocument: NoMessageValues;
  sectionExplainabilityInfo: NoMessageValues;
  sectionMetadata: NoMessageValues;
  sectionEditHistory: NoMessageValues;
  invalidJson: { readonly detail: string };
  cannotFormat: { readonly detail: string };
  titleViewer: { readonly strong: MessageTag };
  titleEditor: { readonly strong: MessageTag };
  selectSections: NoMessageValues;
  showEvaluation: NoMessageValues;
  lines: { readonly count: number };
  copyPrediction: NoMessageValues;
  copyBaseline: NoMessageValues;
  format: NoMessageValues;
  copied: NoMessageValues;
  noPrediction: NoMessageValues;
  noBaseline: NoMessageValues;
  expectedBaseline: NoMessageValues;
  noBaselineTitle: NoMessageValues;
  noBaselineBody: NoMessageValues;
}>({
  sectionInferenceResult: {
    id: 'documents.jsonEditor.section.inferenceResult',
    defaultMessage: 'Extracted data',
    description:
      "Description of the inference_result option of the section picker in the JSON Editor tab of the edit page's section editor (inference_result is the JSON key, shown as is).",
  },
  sectionDocumentClass: {
    id: 'documents.jsonEditor.section.documentClass',
    defaultMessage: 'Classification result',
    description:
      "Description of the document_class option of the section picker in the JSON Editor tab of the edit page's section editor (document_class is the JSON key, shown as is).",
  },
  sectionSplitDocument: {
    id: 'documents.jsonEditor.section.splitDocument',
    defaultMessage: 'Splitting info',
    description:
      "Description of the split_document option of the section picker in the JSON Editor tab of the edit page's section editor (split_document is the JSON key, shown as is).",
  },
  sectionExplainabilityInfo: {
    id: 'documents.jsonEditor.section.explainabilityInfo',
    defaultMessage: 'Confidence & bounding boxes',
    description:
      "Description of the explainability_info option of the section picker in the JSON Editor tab of the edit page's section editor (explainability_info is the JSON key, shown as is).",
  },
  sectionMetadata: {
    id: 'documents.jsonEditor.section.metadata',
    defaultMessage: 'Document metadata',
    description:
      "Description of the metadata option of the section picker in the JSON Editor tab of the edit page's section editor (metadata is the JSON key, shown as is).",
  },
  sectionEditHistory: {
    id: 'documents.jsonEditor.section.editHistory',
    defaultMessage: 'Edit history',
    description:
      "Description of the _editHistory option of the section picker in the JSON Editor tab of the edit page's section editor (_editHistory is the JSON key, shown as is).",
  },
  invalidJson: {
    id: 'documents.jsonEditor.invalidJson',
    defaultMessage: 'Invalid JSON: {detail}',
    description:
      "Error in a panel of the JSON Editor tab of the edit page's section editor when the panel's text is not valid JSON. {detail} is the JSON parser's own message, shown as is.",
  },
  cannotFormat: {
    id: 'documents.jsonEditor.cannotFormat',
    defaultMessage: 'Cannot format: {detail}',
    description:
      "Error in a panel of the JSON Editor tab of the edit page's section editor when Format cannot read the panel's text as JSON. {detail} is the JSON parser's own message, shown as is.",
  },
  titleViewer: {
    id: 'documents.jsonEditor.titleViewer',
    defaultMessage: '<strong>JSON Viewer</strong> - View the raw JSON data.',
    description:
      "Line at the top of the JSON Editor tab of the edit page's section editor when the section is read-only. The <strong> tags mark the bold name.",
  },
  titleEditor: {
    id: 'documents.jsonEditor.titleEditor',
    defaultMessage: '<strong>JSON Editor</strong> - View and edit the raw JSON data.',
    description:
      "Line at the top of the JSON Editor tab of the edit page's section editor when the section can be edited. The <strong> tags mark the bold name.",
  },
  selectSections: {
    id: 'documents.jsonEditor.selectSections',
    defaultMessage: 'Select JSON sections',
    description:
      "Placeholder of the picker, in the JSON Editor tab of the edit page's section editor, that chooses which parts of the JSON to show.",
  },
  showEvaluation: {
    id: 'documents.jsonEditor.showEvaluation',
    defaultMessage: 'Show Evaluation',
    description:
      "Toggle in the JSON Editor tab of the edit page's section editor; when on, the evaluation baseline's JSON shows beside the values.",
  },
  lines: {
    id: 'documents.jsonEditor.lines',
    defaultMessage: '{count, plural, one {# line} other {# lines}}',
    description:
      "Badge in the header of a panel of the JSON Editor tab of the edit page's section editor. {count} is the number of lines of the panel's text.",
  },
  copyPrediction: {
    id: 'documents.jsonEditor.copyPrediction',
    defaultMessage: 'Copy prediction JSON',
    description:
      "Screen-reader label of the button that copies the values panel's JSON, in the JSON Editor tab of the edit page's section editor.",
  },
  copyBaseline: {
    id: 'documents.jsonEditor.copyBaseline',
    defaultMessage: 'Copy baseline JSON',
    description:
      "Screen-reader label of the button that copies the evaluation baseline panel's JSON, in the JSON Editor tab of the edit page's section editor.",
  },
  format: {
    id: 'documents.jsonEditor.format',
    defaultMessage: 'Format',
    description:
      "Button above a panel of the JSON Editor tab of the edit page's section editor that lays out the panel's JSON with indentation.",
  },
  copied: {
    id: 'documents.jsonEditor.copied',
    defaultMessage: '✓ Copied',
    description:
      "Shown beside a panel's title in the JSON Editor tab of the edit page's section editor for two seconds after its JSON was copied.",
  },
  noPrediction: {
    id: 'documents.jsonEditor.noPrediction',
    defaultMessage: 'No prediction data available',
    description: "Placeholder of the values panel's text area in the JSON Editor tab of the edit page's section editor, when it is empty.",
  },
  noBaseline: {
    id: 'documents.jsonEditor.noBaseline',
    defaultMessage: 'No baseline data available',
    description:
      "Placeholder of the evaluation baseline panel's text area in the JSON Editor tab of the edit page's section editor, when it is empty.",
  },
  expectedBaseline: {
    id: 'documents.jsonEditor.expectedBaseline',
    defaultMessage: 'Expected (Baseline)',
    description: "Title of the evaluation baseline panel in the JSON Editor tab of the edit page's section editor.",
  },
  noBaselineTitle: {
    id: 'documents.jsonEditor.noBaselineTitle',
    defaultMessage: 'No Baseline Data',
    description:
      "Heading in the evaluation baseline panel of the JSON Editor tab of the edit page's section editor when the panel is on and the open section has no baseline.",
  },
  noBaselineBody: {
    id: 'documents.jsonEditor.noBaselineBody',
    defaultMessage:
      'Baseline data is not available for this section. Use "Use as Evaluation Baseline" to create baseline data from current predictions.',
    description:
      "Text under No Baseline Data in the JSON Editor tab of the edit page's section editor. The quoted name is the More Actions item that copies the document's results into its evaluation baseline.",
  },
});

/** A JSON panel's error: what failed, and the JSON parser's own message. */
interface JsonError {
  kind: 'invalid' | 'cannotFormat';
  detail: string;
}

/** The sentence of each error a JSON panel shows. */
const jsonErrorMessages: Record<JsonError['kind'], TypedMessageDescriptor<{ readonly detail: string }>> = {
  invalid: messages.invalidJson,
  cannotFormat: messages.cannotFormat,
};

/** Renders a `<strong>...</strong>` element value: the name at the start of the tab's top line. */
const strong = (chunks: React.ReactNode[]): React.JSX.Element => <strong>{chunks}</strong>;

interface JSONEditorTabProps {
  predictionData: Record<string, unknown> | null;
  baselineData: Record<string, unknown> | null;
  isReadOnly: boolean;
  onPredictionChange?: (data: Record<string, unknown>) => void;
  onBaselineChange?: (data: Record<string, unknown>) => void;
  showBaseline: boolean;
  onShowBaselineChange?: (checked: boolean) => void;
  isBaselineAvailable: boolean;
  loadingEvaluation: boolean;
  /** The values panel title (prediction wording unless the values are ground truth). */
  predictionTitle?: string;
}

const logger = new ConsoleLogger('JSONEditorTab');

/**
 * JSONEditorTab - Full-width JSON editor with side-by-side view of predictions and baseline
 */
const JSONEditorTab = ({
  predictionData,
  baselineData,
  isReadOnly,
  onPredictionChange,
  onBaselineChange,
  showBaseline,
  onShowBaselineChange,
  isBaselineAvailable,
  loadingEvaluation,
  predictionTitle,
}: JSONEditorTabProps): React.JSX.Element => {
  const intl = useIntl();
  const [predictionError, setPredictionError] = useState<JsonError | null>(null);
  const [baselineError, setBaselineError] = useState<JsonError | null>(null);
  const [copySuccess, setCopySuccess] = useState<string | null>(null);

  // Section selection state - default to inference_result only
  const [selectedSections, setSelectedSections] = useState<MultiselectProps.Option[]>([
    { label: 'inference_result', value: 'inference_result' },
  ]);

  // Determine which sections are available in the data
  const availableSections = useMemo((): MultiselectProps.Option[] => {
    const available: MultiselectProps.Option[] = [];
    const data = (predictionData || {}) as Record<string, unknown>;

    // Check for each possible section (handle both naming conventions)
    if (data.inference_result || data.inferenceResult) {
      available.push({
        label: 'inference_result',
        value: 'inference_result',
        description: intl.formatMessage(messages.sectionInferenceResult),
      });
    }
    if (data.document_class || data.documentClass) {
      available.push({ label: 'document_class', value: 'document_class', description: intl.formatMessage(messages.sectionDocumentClass) });
    }
    if (data.split_document || data.splitDocument) {
      available.push({ label: 'split_document', value: 'split_document', description: intl.formatMessage(messages.sectionSplitDocument) });
    }
    if (data.explainability_info || data.explainabilityInfo) {
      available.push({
        label: 'explainability_info',
        value: 'explainability_info',
        description: intl.formatMessage(messages.sectionExplainabilityInfo),
      });
    }
    if (data.metadata) {
      available.push({ label: 'metadata', value: 'metadata', description: intl.formatMessage(messages.sectionMetadata) });
    }
    if (data._editHistory) {
      available.push({ label: '_editHistory', value: '_editHistory', description: intl.formatMessage(messages.sectionEditHistory) });
    }

    return available;
  }, [predictionData, intl]);

  // Format JSON for display based on selected sections
  const formattedPrediction = useMemo((): string => {
    try {
      const data = (predictionData || {}) as Record<string, unknown>;
      const selectedValues = selectedSections.map((s) => s.value);

      // If only one section selected, show just that section's content
      if (selectedValues.length === 1) {
        const section = selectedValues[0] ?? '';
        // Handle both naming conventions
        const sectionData =
          data[section] ||
          data[section.replace(/_/g, '')] || // Try camelCase variant
          (section === 'inference_result' ? data.inferenceResult : null) ||
          (section === 'document_class' ? data.documentClass : null) ||
          (section === 'split_document' ? data.splitDocument : null) ||
          (section === 'explainability_info' ? data.explainabilityInfo : null);
        return JSON.stringify(sectionData || data, null, 2);
      }

      // Multiple sections selected - build combined object
      const combinedData: Record<string, unknown> = {};
      selectedValues.forEach((section) => {
        const sectionData =
          data[section as string] ||
          (section === 'inference_result' ? data.inferenceResult : null) ||
          (section === 'document_class' ? data.documentClass : null) ||
          (section === 'split_document' ? data.splitDocument : null) ||
          (section === 'explainability_info' ? data.explainabilityInfo : null);
        if (sectionData !== undefined) {
          combinedData[section as string] = sectionData;
        }
      });

      return JSON.stringify(combinedData, null, 2);
    } catch (e) {
      return JSON.stringify(predictionData, null, 2);
    }
  }, [predictionData, selectedSections]);

  const formattedBaseline = useMemo((): string => {
    if (!baselineData) return '';
    try {
      const data = (baselineData || {}) as Record<string, unknown>;
      const selectedValues = selectedSections.map((s) => s.value);

      // If only one section selected, show just that section's content
      if (selectedValues.length === 1) {
        const section = selectedValues[0] ?? '';
        const sectionData =
          data[section] ||
          (section === 'inference_result' ? data.inferenceResult : null) ||
          (section === 'document_class' ? data.documentClass : null) ||
          (section === 'split_document' ? data.splitDocument : null) ||
          (section === 'explainability_info' ? data.explainabilityInfo : null);
        return JSON.stringify(sectionData || data, null, 2);
      }

      // Multiple sections - build combined object
      const combinedData: Record<string, unknown> = {};
      selectedValues.forEach((section) => {
        const sectionData =
          data[section as string] ||
          (section === 'inference_result' ? data.inferenceResult : null) ||
          (section === 'document_class' ? data.documentClass : null) ||
          (section === 'split_document' ? data.splitDocument : null) ||
          (section === 'explainability_info' ? data.explainabilityInfo : null);
        if (sectionData !== undefined) {
          combinedData[section as string] = sectionData;
        }
      });

      return JSON.stringify(combinedData, null, 2);
    } catch (e) {
      return JSON.stringify(baselineData, null, 2);
    }
  }, [baselineData, selectedSections]);

  // Local state for editing
  const [localPrediction, setLocalPrediction] = useState(formattedPrediction);
  const [localBaseline, setLocalBaseline] = useState(formattedBaseline);

  // Update local state when props change
  React.useEffect(() => {
    setLocalPrediction(formattedPrediction);
  }, [formattedPrediction]);

  React.useEffect(() => {
    setLocalBaseline(formattedBaseline);
  }, [formattedBaseline]);

  // Handle prediction JSON change
  const handlePredictionChange = (value: string) => {
    setLocalPrediction(value);
    setPredictionError(null);

    try {
      const parsed = JSON.parse(value);
      if (onPredictionChange) {
        onPredictionChange(parsed);
      }
    } catch (e) {
      setPredictionError({ kind: 'invalid', detail: (e as Error).message });
    }
  };

  // Handle baseline JSON change
  const handleBaselineChange = (value: string) => {
    setLocalBaseline(value);
    setBaselineError(null);

    try {
      const parsed = JSON.parse(value);
      if (onBaselineChange) {
        onBaselineChange(parsed);
      }
    } catch (e) {
      setBaselineError({ kind: 'invalid', detail: (e as Error).message });
    }
  };

  // Copy to clipboard
  const handleCopy = async (text: string, label: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopySuccess(label);
      setTimeout(() => setCopySuccess(null), 2000);
    } catch (e) {
      logger.error('Failed to copy:', e);
    }
  };

  // Format JSON button
  const handleFormat = (type: 'prediction' | 'baseline') => {
    try {
      if (type === 'prediction') {
        const parsed = JSON.parse(localPrediction);
        setLocalPrediction(JSON.stringify(parsed, null, 2));
        setPredictionError(null);
      } else {
        const parsed = JSON.parse(localBaseline);
        setLocalBaseline(JSON.stringify(parsed, null, 2));
        setBaselineError(null);
      }
    } catch (e) {
      if (type === 'prediction') {
        setPredictionError({ kind: 'cannotFormat', detail: (e as Error).message });
      } else {
        setBaselineError({ kind: 'cannotFormat', detail: (e as Error).message });
      }
    }
  };

  // Calculate line counts for display
  const predictionLines = localPrediction.split('\n').length;
  const baselineLines = localBaseline.split('\n').length;

  return (
    <div style={{ height: 'calc(100vh - 280px)', padding: '16px', overflow: 'auto' }}>
      <SpaceBetween size="m">
        {/* Header with section selector and toggle */}
        <Box>
          <SpaceBetween direction="horizontal" size="m" alignItems="center">
            <Box>
              {isReadOnly ? (
                <FormattedMessage {...messages.titleViewer} values={{ strong }} />
              ) : (
                <FormattedMessage {...messages.titleEditor} values={{ strong }} />
              )}
            </Box>
            <Box {...({ style: { marginLeft: 'auto' } } as Record<string, unknown>)}>
              <SpaceBetween direction="horizontal" size="m" alignItems="center">
                {/* Section selector */}
                <div style={{ minWidth: '280px' }}>
                  <Multiselect
                    selectedOptions={selectedSections}
                    onChange={({ detail }) => {
                      // Ensure at least one section is selected
                      if (detail.selectedOptions.length > 0) {
                        setSelectedSections(detail.selectedOptions as MultiselectProps.Option[]);
                      }
                    }}
                    options={availableSections}
                    placeholder={intl.formatMessage(messages.selectSections)}
                    tokenLimit={2}
                    hideTokens={false}
                  />
                </div>
                {/* Evaluation toggle */}
                {loadingEvaluation && <Spinner {...({ size: 'small' } as Record<string, unknown>)} />}
                {(isBaselineAvailable || loadingEvaluation) && (
                  <Toggle
                    checked={showBaseline}
                    onChange={({ detail }) => {
                      if (onShowBaselineChange) {
                        onShowBaselineChange(detail.checked);
                      }
                    }}
                    disabled={loadingEvaluation || !baselineData}
                  >
                    <FormattedMessage {...messages.showEvaluation} />
                  </Toggle>
                )}
              </SpaceBetween>
            </Box>
          </SpaceBetween>
        </Box>

        <ColumnLayout columns={showBaseline && baselineData ? 2 : 1} variant="text-grid">
          {/* Predictions Panel */}
          <Container
            header={
              <Header
                variant="h3"
                actions={
                  <SpaceBetween direction="horizontal" size="xs">
                    <Badge color={predictionError ? 'red' : 'green'}>
                      <FormattedMessage {...messages.lines} values={{ count: predictionLines }} />
                    </Badge>
                    <Button
                      variant="icon"
                      iconName="copy"
                      onClick={() => handleCopy(localPrediction, 'Prediction')}
                      ariaLabel={intl.formatMessage(messages.copyPrediction)}
                    />
                    {!isReadOnly && (
                      <Button variant="normal" onClick={() => handleFormat('prediction')}>
                        <FormattedMessage {...messages.format} />
                      </Button>
                    )}
                  </SpaceBetween>
                }
              >
                {predictionTitle ?? valueSourceLabel(intl, null).panelTitle}
                {copySuccess === 'Prediction' && (
                  <Box variant="span" color="text-status-success" margin={{ left: 's' }}>
                    <FormattedMessage {...messages.copied} />
                  </Box>
                )}
              </Header>
            }
          >
            {predictionError && (
              <Alert type="error" dismissible onDismiss={() => setPredictionError(null)}>
                <FormattedMessage {...jsonErrorMessages[predictionError.kind]} values={{ detail: predictionError.detail }} />
              </Alert>
            )}
            <div className={isReadOnly ? 'json-editor-readonly' : ''}>
              <Textarea
                value={localPrediction}
                onChange={({ detail }) => handlePredictionChange(detail.value)}
                readOnly={isReadOnly}
                rows={Math.min(Math.max(predictionLines, 20), 40)}
                spellcheck={false}
                placeholder={intl.formatMessage(messages.noPrediction)}
              />
              {isReadOnly && (
                <style>{`
                  .json-editor-readonly textarea {
                    color: #16191f !important;
                    background-color: #e9ebed !important;
                  }
                `}</style>
              )}
            </div>
          </Container>

          {/* Baseline Panel - Only show if baseline is enabled and data exists */}
          {showBaseline && baselineData && (
            <Container
              header={
                <Header
                  variant="h3"
                  actions={
                    <SpaceBetween direction="horizontal" size="xs">
                      <Badge color={baselineError ? 'red' : 'blue'}>
                        <FormattedMessage {...messages.lines} values={{ count: baselineLines }} />
                      </Badge>
                      <Button
                        variant="icon"
                        iconName="copy"
                        onClick={() => handleCopy(localBaseline, 'Baseline')}
                        ariaLabel={intl.formatMessage(messages.copyBaseline)}
                      />
                      {!isReadOnly && (
                        <Button variant="normal" onClick={() => handleFormat('baseline')}>
                          <FormattedMessage {...messages.format} />
                        </Button>
                      )}
                    </SpaceBetween>
                  }
                >
                  <FormattedMessage {...messages.expectedBaseline} />
                  {copySuccess === 'Baseline' && (
                    <Box variant="span" color="text-status-success" margin={{ left: 's' }}>
                      <FormattedMessage {...messages.copied} />
                    </Box>
                  )}
                </Header>
              }
            >
              {baselineError && (
                <Alert type="error" dismissible onDismiss={() => setBaselineError(null)}>
                  <FormattedMessage {...jsonErrorMessages[baselineError.kind]} values={{ detail: baselineError.detail }} />
                </Alert>
              )}
              <div className={isReadOnly ? 'json-editor-readonly' : ''}>
                <Textarea
                  value={localBaseline}
                  onChange={({ detail }) => handleBaselineChange(detail.value)}
                  readOnly={isReadOnly}
                  rows={Math.min(Math.max(baselineLines, 20), 40)}
                  spellcheck={false}
                  placeholder={intl.formatMessage(messages.noBaseline)}
                />
              </div>
            </Container>
          )}

          {/* Placeholder when baseline not enabled */}
          {showBaseline && !baselineData && (
            <Container
              header={
                <Header variant="h3">
                  <FormattedMessage {...messages.expectedBaseline} />
                </Header>
              }
            >
              <Box padding="xl" textAlign="center" color="text-body-secondary">
                <SpaceBetween size="s">
                  <Box variant="h4">
                    <FormattedMessage {...messages.noBaselineTitle} />
                  </Box>
                  <Box>
                    <FormattedMessage {...messages.noBaselineBody} />
                  </Box>
                </SpaceBetween>
              </Box>
            </Container>
          )}
        </ColumnLayout>
      </SpaceBetween>
    </div>
  );
};

export default JSONEditorTab;
