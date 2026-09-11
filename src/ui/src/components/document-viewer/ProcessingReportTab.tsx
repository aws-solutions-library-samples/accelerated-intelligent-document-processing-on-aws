// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  Box,
  Alert,
  ColumnLayout,
  StatusIndicator,
  ExpandableSection,
} from '@cloudscape-design/components';

import { describePromptCache } from '../common/promptCacheModel';
import type { PromptCacheSummary } from '../common/promptCacheModel';

interface Violation {
  field: string;
  message: string;
  possible_cause: string;
}

interface SchemaAnalysis {
  large_array_fields?: string[];
  max_min_items?: number;
  recommendation_strength?: string;
  recommendation_reason?: string;
}

interface OcrAnalysis {
  tables_detected?: number;
  estimated_row_count?: number;
  recommendation_strength?: string;
  recommendation_reason?: string;
  tool_usage_recommended?: boolean;
}

interface ToolUsageDecision {
  expected?: boolean;
  actual?: boolean;
  mismatch?: boolean;
  explanation?: string;
  tool_enabled?: boolean;
  ocr_had_markdown_tables?: boolean;
}

interface CompletenessCheck {
  schema_constraints_met?: boolean;
  violations?: Violation[];
  summary?: string;
}

interface TableParsingStats {
  tables_parsed?: number;
  rows_parsed?: number;
  rows_mapped?: number;
  invocation_count?: number;
  parse_success_rate?: number;
  avg_confidence?: number;
  confidence_available?: boolean;
  warnings?: string[];
}

interface ValidationError {
  path?: string;
  validator?: string;
  message?: string;
}

interface ValidationInfo {
  valid?: boolean;
  error_count?: number;
  failed_fields?: string[];
  errors?: ValidationError[];
  check_formats?: boolean;
  fail_action?: string;
  escalated?: boolean;
  initial_error_count?: number;
  initial_failed_fields?: string[];
  escalation_model?: string;
  escalation_scope?: string;
  escalation_fields?: string[];
  resolved_by_escalation?: boolean;
  escalation_kept?: boolean;
  escalation_decision?: string;
}

interface PopulationCheck {
  fields_defined?: number;
  fields_populated?: number;
  population_ratio?: number;
  threshold?: number;
  below_threshold?: boolean;
  empty_fields?: string[];
}

interface SizingPlan {
  model_id?: string;
  context_buffer?: number;
  max_input_tokens?: number;
  max_output_tokens?: number;
  shard_token_budget?: number;
  max_pages_per_shard?: number;
  list_batch_size?: number;
  overrides?: Record<string, unknown>;
}

interface AssessmentBatchSplitStats {
  batch_count?: number;
  concurrent_batches?: number;
  derived_batch_size?: number;
  configured_batch_size?: number;
  escalation_model?: string;
  truncated_calls?: number;
  splits?: number;
  rows_recovered_by_retry?: number;
  rows_recovered_by_escalation?: number;
  unrecoverable_rows?: number;
}

interface FlowStage {
  key?: string;
  label?: string;
  detail?: string;
  status?: string; // ok | info | warning | skipped
  fanout?: number;
  model?: string;
}

interface FlowRecovery {
  truncated_calls?: number;
  splits?: number;
  rows_recovered_by_retry?: number;
  rows_recovered_by_escalation?: number;
  escalation_model?: string;
  unrecoverable_rows?: number;
  deadline_reached?: boolean;
}

interface ProcessingFlow {
  stages?: FlowStage[];
  recovery?: FlowRecovery | null;
}

interface ProcessingMetadata {
  extraction_method?: string;
  extraction_time_seconds?: number;
  parsing_succeeded?: boolean;
  extraction_model?: string;
  extraction_model_overridden?: boolean;
  schema_analysis?: SchemaAnalysis;
  ocr_analysis?: OcrAnalysis;
  tool_usage_decision?: ToolUsageDecision;
  completeness_check?: CompletenessCheck;
  table_parsing_tool_used?: boolean;
  table_parsing_stats?: TableParsingStats;
  validation?: ValidationInfo;
  // Scalar cells the extraction agent left null because it could not read them
  // (#782). Recorded regardless of validation.enabled; a nulled LIST is not
  // counted here — that is a defect, surfaced as extraction_incomplete.
  abstained_fields?: { count?: number; paths?: string[] };
  population_check?: PopulationCheck;
  sizing_plan?: SizingPlan;
  assessment_batch_split_stats?: AssessmentBatchSplitStats;
  processing_flow?: ProcessingFlow;
  // Per-section prompt-cache efficiency (#780): caching / write-only /
  // never-cached / disabled / no-cache-data, with the token counts behind it.
  prompt_cache?: PromptCacheSummary;
}

interface ProcessingIssue {
  stage?: string;
  severity?: string; // "error" | "warning" | "info"
  code?: string;
  message?: string;
  // GraphQL delivers camelCase (rootCause); the section result.json metadata
  // delivers snake_case (root_cause). Accept both.
  rootCause?: string;
  root_cause?: string;
}

interface ProcessingReportTabProps {
  metadata?: ProcessingMetadata;
  processingReport?: string;
  inferenceResult?: Record<string, unknown>;
  // Structured self-healing issues for this section (from the backend
  // ProcessingIssue spine), surfaced at the top of the report.
  processingIssues?: ProcessingIssue[];
}

// Count the items the extraction actually produced: total list rows across all
// array fields (e.g. holdings_positions) plus populated scalar fields. This is
// the authoritative result, distinct from the pre-flight OCR row estimate.
function summarizeResult(inferenceResult?: Record<string, unknown>): { listRows: number; scalarFields: number; listFields: string[] } {
  let listRows = 0;
  let scalarFields = 0;
  const listFields: string[] = [];
  if (inferenceResult && typeof inferenceResult === 'object') {
    Object.entries(inferenceResult).forEach(([key, value]) => {
      if (Array.isArray(value)) {
        listRows += value.length;
        listFields.push(`${key} (${value.length})`);
      } else if (value !== null && value !== undefined && value !== '') {
        scalarFields += 1;
      }
    });
  }
  return { listRows, scalarFields, listFields };
}

function pct(n: number | undefined): string {
  if (n === undefined || n === null || Number.isNaN(n)) return 'N/A';
  return `${Math.round(n * 100)}%`;
}

/**
 * Data-driven process-flow visual for the Processing Path section. Renders the
 * backend's `processing_flow.stages` left-to-right (works for both simple and
 * advanced), colors each stage by status, and stacks fanned-out stages (sharded
 * extract / concurrent confidence) to convey parallelism. Uses recorded counts
 * only (no fabricated per-inference timings).
 */
// Per-status flow-stage styling. Uses Cloudscape CSS custom properties (with hex
// fallbacks, the established pattern in this app) so the boxes adapt to dark /
// high-contrast modes, and pairs each status with a text MARK so status is never
// conveyed by color alone (accessibility). `ok` is neutral (no mark).
const STAGE_TONE: Record<string, { border: string; bg: string; mark: string }> = {
  ok: {
    border: 'var(--color-border-divider-default, #b6bec9)',
    bg: 'var(--color-background-container-content, #ffffff)',
    mark: '',
  },
  info: {
    border: 'var(--color-border-status-info, #0972d3)',
    bg: 'var(--color-background-status-info, #f0f8ff)',
    mark: '● ',
  },
  warning: {
    border: 'var(--color-border-status-warning, #f89256)',
    bg: 'var(--color-background-status-warning, #fff7f0)',
    mark: '⚠ ',
  },
  skipped: {
    border: 'var(--color-border-divider-secondary, #d5dbdb)',
    bg: 'var(--color-background-container-content, #fbfbfb)',
    mark: '– ',
  },
};

const StageBox: React.FC<{ label: string; sub?: string; status?: string; fanout?: number }> = ({ label, sub, status = 'ok', fanout }) => {
  const tone = STAGE_TONE[status] || STAGE_TONE.ok;
  const parallel = fanout && fanout > 1;
  return (
    <div style={{ position: 'relative' }}>
      {/* Stacked "shadow" cards convey fan-out (sharded extract / concurrent confidence). */}
      {parallel && (
        <>
          <div
            style={{
              position: 'absolute',
              top: 4,
              left: 4,
              right: -4,
              bottom: -4,
              border: `1px solid ${tone.border}`,
              borderRadius: 8,
              background: tone.bg,
              opacity: 0.5,
            }}
          />
          <div
            style={{
              position: 'absolute',
              top: 2,
              left: 2,
              right: -2,
              bottom: -2,
              border: `1px solid ${tone.border}`,
              borderRadius: 8,
              background: tone.bg,
              opacity: 0.75,
            }}
          />
        </>
      )}
      <div
        style={{
          position: 'relative',
          border: `1px solid ${tone.border}`,
          borderRadius: 8,
          padding: '6px 10px',
          background: tone.bg,
          minWidth: 96,
          textAlign: 'center',
          opacity: status === 'skipped' ? 0.6 : 1,
        }}
      >
        <div style={{ fontSize: 12, fontWeight: 600 }}>
          {tone.mark}
          {label}
          {parallel ? ` ×${fanout}` : ''}
        </div>
        {sub && <div style={{ fontSize: 11, color: '#5f6b7a' }}>{sub}</div>}
      </div>
    </div>
  );
};

const Arrow: React.FC = () => <div style={{ alignSelf: 'center', color: '#5f6b7a' }}>→</div>;

// Data-driven flow: renders whatever stages the backend recorded (works for both
// simple and advanced), coloring each by status and stacking fanned-out stages.
const ProcessFlow: React.FC<{ stages: FlowStage[] }> = ({ stages }) => (
  <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'stretch', paddingTop: 4 }}>
    {stages.map((st, i) => (
      <React.Fragment key={st.key || i}>
        {i > 0 && <Arrow />}
        <StageBox label={st.label || '?'} sub={st.detail} status={st.status} fanout={st.fanout} />
      </React.Fragment>
    ))}
  </div>
);

const ProcessingReportTab: React.FC<ProcessingReportTabProps> = ({ metadata, processingReport, inferenceResult, processingIssues }) => {
  if (!metadata) {
    return (
      <Box padding="l" textAlign="center" color="text-status-inactive">
        Processing report not available
      </Box>
    );
  }

  const isAgentic = (metadata.extraction_method || '').toLowerCase() === 'agentic';
  const succeeded = metadata.parsing_succeeded !== false;
  const validation = metadata.validation;
  const populationCheck = metadata.population_check;
  const completenessCheck = metadata.completeness_check || {};
  const stats = metadata.table_parsing_stats;
  const tableToolUsed = metadata.table_parsing_tool_used === true;

  const { listRows, scalarFields, listFields } = summarizeResult(inferenceResult);

  // Item 3: how the document was sized/split/batched (model-aware auto-sizing).
  const sizing = metadata.sizing_plan;
  const batchStats = metadata.assessment_batch_split_stats;
  const promptCache = metadata.prompt_cache;
  const promptCacheText = promptCache ? describePromptCache(promptCache) : null;
  // Systematic flow (both simple and advanced) + explicit auto-recovery detail.
  const flow = metadata.processing_flow;
  const flowStages = flow?.stages || [];
  const recovery = flow?.recovery;

  // ---- Build the list of issues to surface up top (plain language) ----
  const issues: { label: string; detail: string }[] = [];
  if (!succeeded) {
    issues.push({ label: 'Extraction failed', detail: 'The model output could not be parsed into the expected structure.' });
  }
  const abstained = metadata.abstained_fields;
  if (abstained && (abstained.count || 0) > 0) {
    const sample = (abstained.paths || []).slice(0, 5).join(', ');
    issues.push({
      label: 'Required values left blank',
      detail: `${abstained.count} required value(s) came back null — the extraction agent leaves a cell null rather than guessing when it cannot read it${
        sample ? `: ${sample}${(abstained.paths || []).length > 5 ? ', …' : ''}` : ''
      }.`,
    });
  }
  if (validation && validation.valid === false) {
    const fields = (validation.failed_fields || []).join(', ');
    issues.push({
      label: 'Schema validation failed',
      detail: `${validation.error_count || 0} field(s) did not satisfy the schema${fields ? `: ${fields}` : ''}${
        validation.escalated
          ? validation.resolved_by_escalation
            ? ' (resolved by escalation)'
            : ' (escalation attempted, still invalid)'
          : ''
      }.`,
    });
  }
  if (populationCheck?.below_threshold) {
    issues.push({
      label: 'Possible missing data',
      detail: `Only ${populationCheck.fields_populated}/${populationCheck.fields_defined} schema fields were populated (${pct(
        populationCheck.population_ratio,
      )}, below the ${pct(populationCheck.threshold)} threshold) — review for silent extraction loss.`,
    });
  }
  if (completenessCheck.schema_constraints_met === false && (completenessCheck.violations || []).length > 0) {
    issues.push({
      label: 'Completeness shortfall',
      detail: completenessCheck.summary || 'Some array fields have fewer items than the schema requires.',
    });
  }

  // Structured self-healing issues (backend ProcessingIssue spine). Worst
  // severity drives the block's Alert type.
  const structuredIssues = processingIssues || [];
  const hasStructuredError = structuredIssues.some((i) => (i.severity || 'info').toLowerCase() === 'error');
  const hasStructuredWarning = structuredIssues.some((i) => (i.severity || 'info').toLowerCase() === 'warning');
  const structuredAlertType: 'error' | 'warning' | 'info' = hasStructuredError ? 'error' : hasStructuredWarning ? 'warning' : 'info';

  // Overall verdict
  const allClear = issues.length === 0 && structuredIssues.length === 0 && succeeded;

  return (
    <SpaceBetween size="l">
      {/* ---- Outcome (top-line verdict) ---- */}
      <Container
        header={
          <Header
            variant="h2"
            description={`${isAgentic ? 'Agentic' : 'Standard'} extraction${
              metadata.extraction_time_seconds ? ` · ${metadata.extraction_time_seconds.toFixed(1)}s` : ''
            }`}
          >
            Extraction Outcome
          </Header>
        }
      >
        <SpaceBetween size="m">
          <Box>
            <StatusIndicator type={allClear ? 'success' : succeeded ? 'warning' : 'error'}>
              {allClear ? 'Completed — no issues detected' : succeeded ? `Completed with ${issues.length} item(s) to review` : 'Failed'}
            </StatusIndicator>
          </Box>

          <ColumnLayout columns={4} variant="text-grid">
            {inferenceResult && (
              <div>
                <Box variant="awsui-key-label">Data extracted</Box>
                <Box>
                  {listRows > 0 ? `${listRows.toLocaleString()} row(s)` : `${scalarFields} field(s)`}
                  {listRows > 0 && scalarFields > 0 ? ` + ${scalarFields} field(s)` : ''}
                </Box>
              </div>
            )}
            <div>
              <Box variant="awsui-key-label">Schema validation</Box>
              <Box>
                {validation ? (
                  <StatusIndicator type={validation.valid ? 'success' : 'warning'}>
                    {validation.valid ? 'Valid' : `${validation.error_count || 0} issue(s)`}
                  </StatusIndicator>
                ) : (
                  <Box color="text-status-inactive">Not enabled</Box>
                )}
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Model</Box>
              <Box>
                {metadata.extraction_model || 'N/A'}
                {metadata.extraction_model_overridden ? ' (per-class)' : ''}
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Method</Box>
              <Box>
                {isAgentic ? 'Agentic' : 'Standard'}
                {isAgentic && tableToolUsed ? ' + table parser' : ''}
              </Box>
            </div>
          </ColumnLayout>

          {listFields.length > 0 && (
            <Box fontSize="body-s" color="text-status-inactive">
              Lists: {listFields.join(', ')}
            </Box>
          )}
        </SpaceBetween>
      </Container>

      {/* ---- Processing path: how the doc was sized / split / batched ---- */}
      {(sizing || batchStats || promptCache || flowStages.length > 0) && (
        <Container header={<Header variant="h2">Processing Path</Header>}>
          <SpaceBetween size="m">
            {/* Systematic flow graph (rendered for BOTH simple and advanced):
                OCR → Classify → Extract(→shards) → [Table tool] → [Escalation] →
                Confidence(→batches) → Geometry — each stage colored by status. */}
            {flowStages.length > 0 && <ProcessFlow stages={flowStages} />}
            {/* Explicit "what failed and was recovered by retry" callout. */}
            {recovery && (
              <Box fontSize="body-s" padding="xs" color="text-body-secondary" variant="p">
                <strong>⚠ Confidence auto-recovery:</strong>{' '}
                {(recovery.truncated_calls || 0) > 0
                  ? `${recovery.truncated_calls} confidence call(s) truncated at the model's output limit (batches split ${recovery.splits || 0}×). `
                  : ''}
                Recovered <strong>{(recovery.rows_recovered_by_retry || 0) + (recovery.rows_recovered_by_escalation || 0)}</strong> row(s)
                {(recovery.rows_recovered_by_retry || 0) > 0 ? ` — ${recovery.rows_recovered_by_retry} by same-model retry` : ''}
                {(recovery.rows_recovered_by_escalation || 0) > 0
                  ? `, ${recovery.rows_recovered_by_escalation} by escalation to ${recovery.escalation_model || 'a stronger model'}`
                  : ''}
                .{' '}
                {(recovery.unrecoverable_rows || 0) > 0 ? (
                  <StatusIndicator type="error">{recovery.unrecoverable_rows} row(s) remained unscored</StatusIndicator>
                ) : (
                  <StatusIndicator type="success">All rows scored</StatusIndicator>
                )}
                {recovery.deadline_reached ? ' Stopped early on the Lambda wall-clock guard.' : ''}
              </Box>
            )}
            {sizing && (
              <ColumnLayout columns={4} variant="text-grid">
                <div>
                  <Box variant="awsui-key-label">Model window</Box>
                  <Box>
                    in {((sizing.max_input_tokens || 0) / 1000).toLocaleString()}K / out{' '}
                    {((sizing.max_output_tokens || 0) / 1000).toLocaleString()}K
                  </Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Context buffer</Box>
                  <Box>{Math.round((sizing.context_buffer || 0) * 100)}% kept free</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Shard budget (auto)</Box>
                  <Box>
                    ~{(sizing.shard_token_budget || 0).toLocaleString()} tok · {sizing.max_pages_per_shard} pg/shard
                  </Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Confidence batch (auto)</Box>
                  <Box>{sizing.list_batch_size} rows/batch</Box>
                </div>
              </ColumnLayout>
            )}
            {batchStats && batchStats.batch_count ? (
              <Box fontSize="body-s" color="text-body-secondary">
                Confidence assessment ran in <strong>{batchStats.batch_count}</strong> batch(es)
                {batchStats.concurrent_batches && batchStats.concurrent_batches > 1
                  ? ` — ${batchStats.concurrent_batches}-way concurrent after a cache-warming call`
                  : ' — sequential'}
                {batchStats.derived_batch_size
                  ? `; token-aware batch size ${batchStats.derived_batch_size} (configured ${batchStats.configured_batch_size ?? 'n/a'})`
                  : ''}
                {batchStats.escalation_model ? `; escalated to ${batchStats.escalation_model}` : ''}.
              </Box>
            ) : null}
            {promptCacheText && (
              <Box fontSize="body-s">
                <StatusIndicator type={promptCacheText.indicator}>{promptCacheText.headline}</StatusIndicator>
                <Box fontSize="body-s" color="text-body-secondary">
                  {promptCacheText.detail}
                </Box>
              </Box>
            )}
            {sizing?.overrides && Object.keys(sizing.overrides).length > 0 && (
              <Box fontSize="body-s" color="text-status-inactive">
                Manual size overrides in effect: {JSON.stringify(sizing.overrides)}
              </Box>
            )}
          </SpaceBetween>
        </Container>
      )}

      {/* ---- Structured self-healing issues (ProcessingIssue spine) ---- */}
      {structuredIssues.length > 0 && (
        <Alert type={structuredAlertType} header={`${structuredIssues.length} processing issue(s)`}>
          <SpaceBetween size="xs">
            {structuredIssues.map((iss, idx) => (
              <Box key={`${iss.code ?? 'issue'}-${iss.message?.slice(0, 24) ?? idx}`}>
                <StatusIndicator
                  type={
                    (iss.severity || 'info').toLowerCase() === 'error'
                      ? 'error'
                      : (iss.severity || 'info').toLowerCase() === 'warning'
                        ? 'warning'
                        : 'info'
                  }
                >
                  {iss.code || iss.stage || 'issue'}
                </StatusIndicator>{' '}
                {iss.message}
                {(iss.rootCause || iss.root_cause) && (
                  <Box fontSize="body-s" color="text-body-secondary">
                    Root cause: {iss.rootCause || iss.root_cause}
                  </Box>
                )}
              </Box>
            ))}
          </SpaceBetween>
        </Alert>
      )}

      {/* ---- Issues / all-clear ---- */}
      {issues.length > 0 ? (
        <Alert type={succeeded ? 'warning' : 'error'} header={`${issues.length} item(s) to review`}>
          <SpaceBetween size="xs">
            {issues.map((iss) => (
              <Box key={iss.label}>
                <strong>{iss.label}:</strong> {iss.detail}
              </Box>
            ))}
          </SpaceBetween>
        </Alert>
      ) : (
        <Alert type="success" header="No issues detected">
          Extraction completed and passed all enabled checks.
        </Alert>
      )}

      {/* ---- Details (collapsed by default) ---- */}
      <ExpandableSection variant="container" headerText="Details & diagnostics" defaultExpanded={issues.length > 0}>
        <SpaceBetween size="l">
          {/* Schema validation detail */}
          {validation && (
            <div>
              <Box variant="awsui-key-label">Schema Validation &amp; Escalation</Box>
              <ColumnLayout columns={3} variant="text-grid">
                <div>
                  <Box variant="awsui-key-label">Result</Box>
                  <Box>
                    <StatusIndicator type={validation.valid ? 'success' : 'warning'}>
                      {validation.valid ? 'Valid' : `${validation.error_count || 0} violation(s)`}
                    </StatusIndicator>
                  </Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">On failure</Box>
                  <Box>{validation.fail_action || 'N/A'}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Format checks</Box>
                  <Box>{validation.check_formats ? 'On' : 'Off'}</Box>
                </div>
              </ColumnLayout>
              {validation.escalated && (
                <Box padding={{ top: 'xs' }} fontSize="body-s">
                  Escalated to <strong>{validation.escalation_model || 'stronger model'}</strong> (
                  {validation.escalation_scope === 'field-subset'
                    ? `fields: ${(validation.escalation_fields || []).join(', ') || 'none'}`
                    : 'full section'}
                  ) —{' '}
                  {validation.escalation_kept === false
                    ? 'rejected, original kept'
                    : validation.resolved_by_escalation
                      ? 'resolved'
                      : 'still invalid'}
                  {validation.initial_error_count !== undefined
                    ? `; errors ${validation.initial_error_count} → ${validation.error_count || 0}`
                    : ''}
                  .{validation.escalation_decision ? ` ${validation.escalation_decision}.` : ''}
                </Box>
              )}
              {!validation.valid && validation.errors && validation.errors.length > 0 && (
                <Box padding={{ top: 'xs' }}>
                  <ExpandableSection headerText={`Violations (${validation.error_count || validation.errors.length})`}>
                    <SpaceBetween size="xxs">
                      {validation.errors.map((e) => (
                        <Box key={`verr-${e.path}-${e.validator}-${e.message}`} fontSize="body-s">
                          <strong>{e.path || '(root)'}</strong>: {e.message}
                        </Box>
                      ))}
                    </SpaceBetween>
                  </ExpandableSection>
                </Box>
              )}
            </div>
          )}

          {/* Field population */}
          {populationCheck && populationCheck.fields_defined !== undefined && (
            <div>
              <Box variant="awsui-key-label">Field Population (completeness heuristic)</Box>
              <Box>
                {populationCheck.fields_populated}/{populationCheck.fields_defined} fields populated (
                {pct(populationCheck.population_ratio)}) ·{' '}
                <StatusIndicator type={populationCheck.below_threshold ? 'warning' : 'success'}>
                  {populationCheck.below_threshold ? `below ${pct(populationCheck.threshold)} threshold` : 'OK'}
                </StatusIndicator>
              </Box>
              {populationCheck.empty_fields && populationCheck.empty_fields.length > 0 && (
                <Box padding={{ top: 'xs' }}>
                  <ExpandableSection headerText={`Empty fields (${populationCheck.empty_fields.length})`}>
                    <Box fontSize="body-s">{populationCheck.empty_fields.join(', ')}</Box>
                  </ExpandableSection>
                </Box>
              )}
            </div>
          )}

          {/* Table parsing */}
          {isAgentic && (metadata.ocr_analysis || stats) && (
            <div>
              <Box variant="awsui-key-label">Table Extraction</Box>
              {(() => {
                const decision = metadata.tool_usage_decision;
                const recommended =
                  metadata.ocr_analysis?.tool_usage_recommended || metadata.ocr_analysis?.recommendation_strength === 'MANDATORY';
                // Prefer the backend's explicit, reasoned explanation when the tool
                // was recommended but not used — it states WHY (disabled / no
                // Markdown tables in OCR / agent declined) rather than just "not used".
                let text: string;
                let warn = false;
                if (tableToolUsed) {
                  text =
                    'The deterministic table parser was used (parses tables from OCR text instead of having the model regenerate every row).';
                } else if (recommended && decision?.explanation) {
                  text = decision.explanation;
                  // Only a genuine "agent declined an available tool" is worth a warning tint.
                  warn = decision.tool_enabled !== false && decision.ocr_had_markdown_tables !== false;
                } else if (recommended) {
                  text = 'Large tables were detected but the deterministic table parser was not used for this section.';
                  warn = true;
                } else {
                  text = 'No large tables detected in the OCR text; the deterministic table parser was not needed.';
                }
                return (
                  <Box fontSize="body-s" padding={{ bottom: 'xs' }} color={warn ? 'text-status-warning' : 'text-body-secondary'}>
                    {warn ? '⚠ ' : ''}
                    {text}
                  </Box>
                );
              })()}
              <ColumnLayout columns={3} variant="text-grid">
                {metadata.ocr_analysis?.estimated_row_count !== undefined && (
                  <div>
                    <Box variant="awsui-key-label">Table rows seen in OCR (estimate)</Box>
                    <Box>~{metadata.ocr_analysis.estimated_row_count.toLocaleString()}</Box>
                  </div>
                )}
                {stats && (
                  <>
                    <div>
                      <Box variant="awsui-key-label">Rows parsed by tool</Box>
                      <Box>{(stats.rows_parsed || 0).toLocaleString()}</Box>
                    </div>
                    <div>
                      <Box variant="awsui-key-label">Parse success rate</Box>
                      <Box>{stats.parse_success_rate !== undefined ? pct(stats.parse_success_rate) : 'N/A'}</Box>
                    </div>
                    {stats.confidence_available && stats.avg_confidence !== undefined && (
                      <div>
                        <Box variant="awsui-key-label">Avg OCR confidence</Box>
                        <Box>{stats.avg_confidence.toFixed(1)}%</Box>
                      </div>
                    )}
                  </>
                )}
              </ColumnLayout>
              {inferenceResult && listRows > 0 && metadata.ocr_analysis?.estimated_row_count !== undefined && (
                <Box padding={{ top: 'xs' }} fontSize="body-s" color="text-status-inactive">
                  The OCR estimate (~{metadata.ocr_analysis.estimated_row_count.toLocaleString()}) is a pre-extraction guess and may differ
                  from the {listRows.toLocaleString()} row(s) actually extracted (the authoritative count, shown in Document Data).
                </Box>
              )}
              {stats?.warnings && stats.warnings.length > 0 && (
                <Box padding={{ top: 'xs' }}>
                  <ExpandableSection headerText={`Parser warnings (${stats.warnings.length})`}>
                    <SpaceBetween size="xxs">
                      {stats.warnings.map((w) => (
                        <Box key={`warn-${w}`} fontSize="body-s">
                          • {w}
                        </Box>
                      ))}
                    </SpaceBetween>
                  </ExpandableSection>
                </Box>
              )}
            </div>
          )}

          {/* Completeness violations (minItems shortfalls) */}
          {completenessCheck.violations && completenessCheck.violations.length > 0 && (
            <div>
              <Box variant="awsui-key-label">Completeness Shortfalls</Box>
              <SpaceBetween size="xxs">
                {completenessCheck.violations.map((v) => (
                  <Box key={`cv-${v.field}`} fontSize="body-s">
                    <strong>{v.field}</strong>: {v.message}
                  </Box>
                ))}
              </SpaceBetween>
            </div>
          )}

          {/* Raw processing report text, if present */}
          {processingReport && (
            <ExpandableSection headerText="Raw processing report (text)">
              <Box padding="s" variant="code">
                <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{processingReport}</pre>
              </Box>
            </ExpandableSection>
          )}
        </SpaceBetween>
      </ExpandableSection>
    </SpaceBetween>
  );
};

export default ProcessingReportTab;
