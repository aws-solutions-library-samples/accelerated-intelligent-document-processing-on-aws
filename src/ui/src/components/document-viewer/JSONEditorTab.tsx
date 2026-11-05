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

import { valueSourceLabel } from '../common/labels';

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
// Available JSON section options
const _SECTION_OPTIONS = [
  { label: 'inference_result', value: 'inference_result', description: 'Extracted data from the document' },
  { label: 'document_class', value: 'document_class', description: 'Document classification result' },
  { label: 'split_document', value: 'split_document', description: 'Document splitting information' },
  { label: 'explainability_info', value: 'explainability_info', description: 'Confidence and bounding box data' },
  { label: 'metadata', value: 'metadata', description: 'Document metadata' },
  { label: '_editHistory', value: '_editHistory', description: 'Edit history for this document' },
];

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
  predictionTitle = valueSourceLabel(null).panelTitle,
}: JSONEditorTabProps): React.JSX.Element => {
  const [predictionError, setPredictionError] = useState<string | null>(null);
  const [baselineError, setBaselineError] = useState<string | null>(null);
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
      available.push({ label: 'inference_result', value: 'inference_result', description: 'Extracted data' });
    }
    if (data.document_class || data.documentClass) {
      available.push({ label: 'document_class', value: 'document_class', description: 'Classification result' });
    }
    if (data.split_document || data.splitDocument) {
      available.push({ label: 'split_document', value: 'split_document', description: 'Splitting info' });
    }
    if (data.explainability_info || data.explainabilityInfo) {
      available.push({ label: 'explainability_info', value: 'explainability_info', description: 'Confidence & bounding boxes' });
    }
    if (data.metadata) {
      available.push({ label: 'metadata', value: 'metadata', description: 'Document metadata' });
    }
    if (data._editHistory) {
      available.push({ label: '_editHistory', value: '_editHistory', description: 'Edit history' });
    }

    return available;
  }, [predictionData]);

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
      setPredictionError(`Invalid JSON: ${(e as Error).message}`);
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
      setBaselineError(`Invalid JSON: ${(e as Error).message}`);
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
        setPredictionError(`Cannot format: ${(e as Error).message}`);
      } else {
        setBaselineError(`Cannot format: ${(e as Error).message}`);
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
              <strong>JSON {isReadOnly ? 'Viewer' : 'Editor'}</strong> - {isReadOnly ? 'View' : 'View and edit'} the raw JSON data.
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
                        setSelectedSections(detail.selectedOptions as import('@cloudscape-design/components').MultiselectProps.Option[]);
                      }
                    }}
                    options={availableSections}
                    placeholder="Select JSON sections"
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
                    Show Evaluation
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
                    <Badge color={predictionError ? 'red' : 'green'}>{predictionLines} lines</Badge>
                    <Button
                      variant="icon"
                      iconName="copy"
                      onClick={() => handleCopy(localPrediction, 'Prediction')}
                      ariaLabel="Copy prediction JSON"
                    />
                    {!isReadOnly && (
                      <Button variant="normal" onClick={() => handleFormat('prediction')}>
                        Format
                      </Button>
                    )}
                  </SpaceBetween>
                }
              >
                {predictionTitle}
                {copySuccess === 'Prediction' && (
                  <Box variant="span" color="text-status-success" margin={{ left: 's' }}>
                    ✓ Copied
                  </Box>
                )}
              </Header>
            }
          >
            {predictionError && (
              <Alert type="error" dismissible onDismiss={() => setPredictionError(null)}>
                {predictionError}
              </Alert>
            )}
            <div className={isReadOnly ? 'json-editor-readonly' : ''}>
              <Textarea
                value={localPrediction}
                onChange={({ detail }) => handlePredictionChange(detail.value)}
                readOnly={isReadOnly}
                rows={Math.min(Math.max(predictionLines, 20), 40)}
                spellcheck={false}
                placeholder="No prediction data available"
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
                      <Badge color={baselineError ? 'red' : 'blue'}>{baselineLines} lines</Badge>
                      <Button
                        variant="icon"
                        iconName="copy"
                        onClick={() => handleCopy(localBaseline, 'Baseline')}
                        ariaLabel="Copy baseline JSON"
                      />
                      {!isReadOnly && (
                        <Button variant="normal" onClick={() => handleFormat('baseline')}>
                          Format
                        </Button>
                      )}
                    </SpaceBetween>
                  }
                >
                  Expected (Baseline)
                  {copySuccess === 'Baseline' && (
                    <Box variant="span" color="text-status-success" margin={{ left: 's' }}>
                      ✓ Copied
                    </Box>
                  )}
                </Header>
              }
            >
              {baselineError && (
                <Alert type="error" dismissible onDismiss={() => setBaselineError(null)}>
                  {baselineError}
                </Alert>
              )}
              <div className={isReadOnly ? 'json-editor-readonly' : ''}>
                <Textarea
                  value={localBaseline}
                  onChange={({ detail }) => handleBaselineChange(detail.value)}
                  readOnly={isReadOnly}
                  rows={Math.min(Math.max(baselineLines, 20), 40)}
                  spellcheck={false}
                  placeholder="No baseline data available"
                />
              </div>
            </Container>
          )}

          {/* Placeholder when baseline not enabled */}
          {showBaseline && !baselineData && (
            <Container header={<Header variant="h3">Expected (Baseline)</Header>}>
              <Box padding="xl" textAlign="center" color="text-body-secondary">
                <SpaceBetween size="s">
                  <Box variant="h4">No Baseline Data</Box>
                  <Box>
                    Baseline data is not available for this section. Use &quot;Use as Evaluation Baseline&quot; to create baseline data from
                    current predictions.
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
