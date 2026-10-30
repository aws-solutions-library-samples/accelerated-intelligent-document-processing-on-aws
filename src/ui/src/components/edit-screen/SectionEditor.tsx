// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * SectionEditor — the edit page's Document Editor panel, the same in every mode.
 *
 * A Section dropdown at its top left (for more than one section); page images on the
 * left and, on the right, tabs with a visual form over the section result's
 * `inference_result` and a raw JSON editor. The field edits are under the page's
 * History, and its Save and Cancel are in the page's bar (through `controllerRef`).
 *
 * The editor owns the field editing and change tracking. How a section is loaded and
 * saved, and anything mode-specific (a Test Studio item's notices, Re-extract and
 * pages field, from test-set-editor-slots.tsx), comes in through props and slots, so
 * there is one editor rather than one per mode.
 */

import React, { useState, useEffect, useMemo, useRef } from 'react';
import {
  Alert,
  Box,
  Button,
  Container,
  CopyToClipboard,
  FormField,
  Header,
  Select,
  SpaceBetween,
  Spinner,
  Tabs,
  Toggle,
} from '@cloudscape-design/components';
import type { SelectProps } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { getErrorMessage } from '../../utils/errorUtils';
import useUnsavedChangesGuard from '../../hooks/use-unsaved-changes-guard';
import PageImageViewer from '../common/PageImageViewer';
import FormFieldRenderer from '../document-viewer/FormFieldRenderer';
import JSONEditorTab from '../document-viewer/JSONEditorTab';
import { SectionClassEvaluation } from '../common/ClassMismatchIndicator';
import { buildEditEntry, diffLeafPaths } from './document-edit-save';
import type { LoadedSection, ReviewPage, ReviewSectionRef } from './types';
import type { SectionEvaluation } from './use-section-evaluation';

const logger = new ConsoleLogger('SectionEditor');

/** What the slots see of the editor's state for the open section. */
export interface SectionEditorContext {
  /** The open section's working copy, or null while nothing is loaded. */
  data: Record<string, unknown> | null;
  /** Replace the working copy (for example, to change the document class). */
  setData: (data: Record<string, unknown>) => void;
  selectedSectionId: string;
  hasChanges: boolean;
}

type Slot = (ctx: SectionEditorContext) => React.ReactNode;

/**
 * What a parent that places the Save and Discard actions itself (the unified editor's
 * top bar) calls. `save` saves the open section when it has changes and resolves true
 * when there was nothing to save or the save succeeded.
 */
export interface SectionEditorController {
  save: () => Promise<boolean>;
  discard: () => void;
  isDirty: () => boolean;
}

export interface SectionEditorProps {
  objectKey: string;
  sections: ReviewSectionRef[];
  pages: ReviewPage[];
  isReadOnly: boolean;
  /** Fetch one section's result. */
  loadSection: (sectionId: string) => Promise<LoadedSection>;
  /**
   * Persist one section. May return the object actually stored (for example, with
   * provenance the adapter added), which becomes the new unchanged state.
   */
  onSave: (sectionId: string, data: Record<string, unknown>) => Promise<void | Record<string, unknown>>;
  /** Called after a successful save. */
  onSaved?: (sectionId: string) => void;
  /** Called after each successful load, with the parsed result. */
  onLoaded?: (sectionId: string, data: Record<string, unknown>) => void;
  /** Called whenever the working copy changes, so an adapter can derive state from it. */
  onDataChange?: (data: Record<string, unknown> | null) => void;
  onDirtyChange?: (dirty: boolean) => void;
  /**
   * Edit Page Grouping, the last of the widget's buttons: opens the page-grouping board.
   * Omitted where the mode offers no regrouping.
   */
  regroup?: { disabledReason?: string; loading?: boolean; onClick: () => void };
  /** Canonical path of a field to select on open ("LineItems[0].Rate"). */
  focusFieldPath?: string | null;
  /** Builds a shareable link to one field; omitted hides the affordance. */
  buildFieldLink?: ((fieldPath: string) => string) | null;
  /** Bumped by the adapter to re-read the open section (its key is unchanged). */
  reloadToken?: number;
  /** Under the section dropdown: notices about the document's sections. */
  headerExtras?: Slot;
  /** At the top of the visual tab: the class control and section facts. */
  classSlot?: Slot;
  /** What to say when the open section has no field values. */
  emptyFieldsSlot?: Slot;
  /** Above the page images: loading and availability notices. */
  pagesNotice?: React.ReactNode;
  pagesLoading?: boolean;
  /** Save automatically this long after the last edit (live drafts). */
  autosaveMs?: number;
  /** Ask before switching sections with unsaved changes (off when autosaving). */
  confirmSectionSwitch?: boolean;
  unsavedChangesMessage?: string;
  /** Filled with the editor's save, discard and dirty check, for the page's bar. */
  controllerRef?: React.MutableRefObject<SectionEditorController | null>;
  /**
   * Append an `_editHistory` entry (who, when, which fields) to every save that changes
   * a field, for a save path that doesn't record one itself (a review draft).
   */
  recordEditHistory?: { editedBy: string; source: string };
  onSavingChange?: (saving: boolean) => void;
  onSelectedSectionChange?: (sectionId: string | null) => void;
  /** The document's configuration, for the field renderer's per-field thresholds. */
  mergedConfig?: Record<string, unknown>;
  /** The evaluation baseline to compare with and edit (direct edit). */
  evaluation?: SectionEvaluation;
  /** The section to open first; defaults to the first section. */
  initialSectionId?: string | null;
  /**
   * Shown in place of the panes (the source document). The editor stays
   * mounted meanwhile, so switching back keeps the unsaved edits.
   */
  body?: React.ReactNode;
  /** The "Fields to show" choice to open with: a queue item with review reasons opens on its alerts. */
  defaultFilter?: 'none' | 'confidence-alerts';
}

const FILTER_ALL: SelectProps.Option = { label: 'All fields', value: 'none' };
const FILTER_ALERTS: SelectProps.Option = { label: 'Confidence alerts only', value: 'confidence-alerts' };
/** The widget's panes fill the viewport below the screen's header, but never shrink below a usable height. */
const WIDGET_PANE_HEIGHT = 'max(480px, calc(100vh - 260px))';

/**
 * A section tab's label, naming the class — or naming its absence.
 *
 * An unclassified section used to read as a bare `Section 1` beside a classified
 * `Section 2 (Invoice)`, so the missing class was expressed only as *absent*
 * parenthetical text. That reads as "this tab just shows the number", not "this section
 * has no class" — and it is the reason the section has no extracted fields, so it is
 * worth saying out loud.
 */
const sectionTabLabel = (sectionId: string, documentClass: string | null | undefined): string =>
  documentClass ? `Section ${sectionId} (${documentClass})` : `Section ${sectionId} (no class)`;

const SectionEditor = ({
  objectKey,
  sections,
  pages,
  isReadOnly,
  loadSection,
  onSave,
  onSaved,
  onLoaded,
  onDataChange,
  onDirtyChange,
  focusFieldPath = null,
  buildFieldLink = null,
  reloadToken = 0,
  headerExtras,
  classSlot,
  emptyFieldsSlot,
  pagesNotice,
  pagesLoading = false,
  autosaveMs,
  confirmSectionSwitch = true,
  unsavedChangesMessage = 'You have unsaved changes. Leave this document and discard them?',
  controllerRef,
  recordEditHistory,
  onSavingChange,
  onSelectedSectionChange,
  mergedConfig,
  evaluation,
  initialSectionId = null,
  body,
  regroup,
  defaultFilter = 'none',
}: SectionEditorProps): React.JSX.Element => {
  const firstSectionId = (): string =>
    (initialSectionId && sections.some((s) => s.sectionId === initialSectionId) ? initialSectionId : sections[0]?.sectionId) ?? '1';
  const [selectedSectionId, setSelectedSectionId] = useState<string>(firstSectionId);
  const [localData, setLocalData] = useState<Record<string, unknown> | null>(null);
  const [originalJson, setOriginalJson] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeFieldGeometry, setActiveFieldGeometry] = useState<Record<string, unknown> | null>(null);
  // Canonical path of the field the reviewer last clicked, so it can be linked.
  const [selectedFieldPath, setSelectedFieldPath] = useState<string | null>(null);
  const [collapsedPaths, setCollapsedPaths] = useState<Set<string>>(new Set());
  const [filterMode, setFilterMode] = useState<SelectProps.Option>(defaultFilter === 'confidence-alerts' ? FILTER_ALERTS : FILTER_ALL);
  const [activeTabId, setActiveTabId] = useState('visual');
  const [showEvaluation, setShowEvaluation] = useState(false);
  // Latest callbacks, so the load effect does not re-run when a parent re-renders.
  const loadRef = useRef(loadSection);
  loadRef.current = loadSection;
  const onLoadedRef = useRef(onLoaded);
  onLoadedRef.current = onLoaded;

  const selectedSection = sections.find((s) => s.sectionId === selectedSectionId) ?? sections[0];

  // Reset to the first section when switching documents.
  useEffect(() => {
    setSelectedSectionId(firstSectionId());
  }, [objectKey, sections]);

  useEffect(() => {
    if (onSelectedSectionChange) onSelectedSectionChange(selectedSection?.sectionId ?? null);
  }, [selectedSection?.sectionId]);

  useEffect(() => {
    if (!selectedSection) {
      setLocalData(null);
      setOriginalJson(null);
      return undefined;
    }
    let cancelled = false;
    const load = async () => {
      setIsLoading(true);
      setError(null);
      setActiveFieldGeometry(null);
      try {
        const loaded = await loadRef.current(selectedSection.sectionId);
        if (cancelled) return;
        const parsed = loaded.data as Record<string, unknown>;
        setLocalData(parsed);
        setOriginalJson(loaded.raw);
        if (onLoadedRef.current) onLoadedRef.current(selectedSection.sectionId, parsed);
      } catch (err) {
        logger.error('Error loading section:', err);
        if (!cancelled) setError(`Failed to load ground truth: ${getErrorMessage(err)}`);
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    };
    load();
    return () => {
      cancelled = true;
    };
  }, [selectedSection?.sectionId, selectedSection?.resultUri, selectedSection?.draftUri, reloadToken]);

  useEffect(() => {
    if (onDataChange) onDataChange(localData);
  }, [localData]);

  const hasChanges = useMemo(() => {
    if (!localData || originalJson === null) return false;
    try {
      return JSON.stringify(localData) !== JSON.stringify(JSON.parse(originalJson));
    } catch {
      return true;
    }
  }, [localData, originalJson]);

  // Leaf paths whose value differs from what was loaded. The renderer uses this to
  // relabel the field as the reviewer's own and to drop the model's confidence,
  // which otherwise stayed attached to hand-typed text.
  const predictionChanges = useMemo(() => {
    // A Set, not a Map: only membership is consulted, and the renderer's prop is
    // a set of edited paths.
    const changes = new Set<string>();
    if (!localData || originalJson === null) return changes;
    let original: Record<string, unknown>;
    try {
      original = JSON.parse(originalJson) as Record<string, unknown>;
    } catch {
      return changes;
    }
    const walk = (now: unknown, before: unknown, trail: string[]) => {
      if (now !== null && typeof now === 'object') {
        const beforeObj = (before ?? {}) as Record<string, unknown>;
        if (Array.isArray(now)) {
          // Indices ARE kept: an edit to LineItems[3].Rate must mark that row and
          // no other. Dropping them produced the key LineItems.Rate, which every
          // row computes too, so a single corrected cell relabelled the entire
          // column "Your value:" and suppressed the model's confidence on values
          // the reviewer never touched. The renderer looks these up with a
          // matching index-preserving key (editedFieldPaths).
          now.forEach((item, i) => walk(item, Array.isArray(before) ? before[i] : undefined, [...trail, String(i)]));
        } else {
          Object.entries(now as Record<string, unknown>).forEach(([k, v]) => walk(v, beforeObj[k], [...trail, k]));
        }
        return;
      }
      if (now !== before) changes.add(trail.join('.'));
    };
    walk(localData.inference_result ?? {}, original.inference_result ?? {}, []);
    return changes;
  }, [localData, originalJson]);

  const explainabilityInfo = (localData?.explainability_info as Record<string, unknown> | Record<string, unknown>[] | null) ?? null;
  const inferenceResult =
    (localData?.inference_result as Record<string, unknown> | undefined) ??
    (localData?.inferenceResult as Record<string, unknown> | undefined) ??
    null;
  /**
   * Whether there is anything to render, as opposed to an object being present.
   *
   * `{}` is truthy, so an empty result took the render path and produced a "Document
   * Data" heading with no children and no explanation — visually identical to the
   * renderer having failed.
   */
  const hasFieldValues = Boolean(inferenceResult && Object.keys(inferenceResult).length > 0);

  // Packet-splitting results carry the section's document-absolute page indices
  // (0-based). Use them to restrict the image pane to this section's pages; else the
  // adapter's page ids for the section; otherwise show the whole document.
  const splitPageIndices = (localData?.split_document as Record<string, unknown> | undefined)?.page_indices as number[] | undefined;
  const sectionPageIds = selectedSection?.pageIds;
  const sectionPages = useMemo(() => {
    if (pages.length === 0) return pages;
    if (splitPageIndices?.length) return splitPageIndices.map((idx) => pages[idx]).filter(Boolean);
    if (sectionPageIds?.length) {
      const wanted = new Set(sectionPageIds);
      const subset = pages.filter((p) => wanted.has(p.Id));
      if (subset.length > 0) return subset;
    }
    return pages;
  }, [pages, splitPageIndices, sectionPageIds]);
  const pageIds = useMemo(() => sectionPages.map((p) => p.Id), [sectionPages]);

  const documentClassType = (localData?.document_class as Record<string, unknown> | undefined)?.type as string | undefined;

  const updateInferenceResult = (newValue: Record<string, unknown>) => {
    if (isReadOnly || !localData) return;
    const updated = { ...localData };
    if (updated.inference_result !== undefined) {
      updated.inference_result = newValue;
    } else if (updated.inferenceResult !== undefined) {
      updated.inferenceResult = newValue;
    } else {
      updated.inference_result = newValue;
    }
    setLocalData(updated);
  };

  /** Save the open section; resolves true on success. */
  const handleSave = async (): Promise<boolean> => {
    if (!selectedSection || !localData) return false;
    setIsSaving(true);
    setError(null);
    try {
      let dataToSave: Record<string, unknown> = { ...localData };
      if (recordEditHistory && originalJson !== null) {
        const changes = diffLeafPaths(JSON.parse(originalJson), localData);
        if (changes.size > 0) {
          const entry = { ...buildEditEntry(recordEditHistory.editedBy, changes, new Map()), source: recordEditHistory.source };
          dataToSave = { ...dataToSave, _editHistory: [...((localData._editHistory as unknown[]) ?? []), entry] };
        }
      }
      const stored = await onSave(selectedSection.sectionId, dataToSave);
      const saved = (stored as Record<string, unknown> | undefined) ?? dataToSave;
      setLocalData(saved);
      setOriginalJson(JSON.stringify(saved, null, 2));
      if (onSaved) onSaved(selectedSection.sectionId);
      return true;
    } catch (err) {
      logger.error('Error saving section:', err);
      setError(`Failed to save: ${getErrorMessage(err)}`);
      return false;
    } finally {
      setIsSaving(false);
    }
  };

  useEffect(() => {
    if (onSavingChange) onSavingChange(isSaving);
  }, [isSaving]);

  // Autosave: a debounce, not a poll — it fires once, autosaveMs after the last edit.
  const saveRef = useRef(handleSave);
  saveRef.current = handleSave;
  useEffect(() => {
    if (!autosaveMs || !hasChanges || isReadOnly || isSaving) return undefined;
    const timer = window.setTimeout(() => {
      void saveRef.current();
    }, autosaveMs);
    return () => window.clearTimeout(timer);
  }, [autosaveMs, hasChanges, isReadOnly, isSaving, localData]);

  const handleDiscard = () => {
    if (originalJson !== null) {
      setLocalData(JSON.parse(originalJson));
    }
    if (evaluation?.originalBaseline && evaluation.canEditBaseline) evaluation.setBaseline(evaluation.originalBaseline);
  };

  // The baseline edits made here, compared with the baseline as loaded or last saved.
  const baselineChanges = useMemo(
    () => (evaluation?.baseline ? diffLeafPaths(evaluation.originalBaseline, evaluation.baseline) : new Map()),
    [evaluation?.baseline, evaluation?.originalBaseline],
  );
  const isDirty = hasChanges || baselineChanges.size > 0;

  useEffect(() => {
    if (onDirtyChange) onDirtyChange(isDirty);
  }, [isDirty]);

  // Covers tab close AND in-app navigation. Only the former was handled, and the
  // latter is how edits were actually lost: a route change is not a page unload,
  // so clicking a nav link discarded everything with no prompt at all.
  useUnsavedChangesGuard(isDirty, unsavedChangesMessage);

  // The parent's buttons read the latest state through these.
  const latest = useRef({ handleSave, handleDiscard, isDirty });
  latest.current = { handleSave, handleDiscard, isDirty };
  useEffect(() => {
    if (!controllerRef) return undefined;
    controllerRef.current = {
      save: async () => (latest.current.isDirty ? latest.current.handleSave() : true),
      discard: () => latest.current.handleDiscard(),
      isDirty: () => latest.current.isDirty,
    };
    return () => {
      controllerRef.current = null;
    };
  }, [controllerRef]);

  const handleFieldFocus = (geometry: Record<string, unknown> | null) => {
    setActiveFieldGeometry(geometry ?? null);
  };

  /**
   * Bring a deep-linked field on screen and select it.
   *
   * Runs after the section's data has rendered, hence the dependency on `localData`
   * rather than on mount.
   */
  useEffect(() => {
    if (!focusFieldPath || !localData) return;
    setSelectedFieldPath(focusFieldPath);
    // Defer to the paint that renders the fields; the node does not exist yet on
    // the tick that localData lands.
    const timer = window.setTimeout(() => {
      const node = document.querySelector(`[data-field-path="${CSS.escape(focusFieldPath)}"]`);
      if (node) node.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }, 100);
    return () => window.clearTimeout(timer);
  }, [focusFieldPath, localData]);

  const handleToggleCollapse = (pathKey: string) => {
    setCollapsedPaths((prev) => {
      const next = new Set(prev);
      if (next.has(pathKey)) next.delete(pathKey);
      else next.add(pathKey);
      return next;
    });
  };

  const handleSectionChange = async (sectionId: string) => {
    if (confirmSectionSwitch && isDirty && !window.confirm('You have unsaved ground truth changes. Discard them and switch sections?')) {
      return;
    }
    setSelectedSectionId(sectionId);
  };

  // The evaluation baseline (direct edit): shown only where the document has one.
  const baselineData = evaluation?.available ? evaluation.baseline : null;
  const canEditBaseline = !!evaluation?.canEditBaseline && !isReadOnly && !!baselineData;
  const baselineInference = baselineData
    ? ((baselineData.inference_result ?? baselineData.inferenceResult ?? baselineData) as Record<string, unknown>)
    : null;
  const updateBaselineInference = (next: Record<string, unknown>) => {
    if (!canEditBaseline || !baselineData || !evaluation) return;
    const updated = { ...baselineData };
    if (updated.inferenceResult !== undefined && updated.inference_result === undefined) updated.inferenceResult = next;
    else updated.inference_result = next;
    evaluation.setBaseline(updated);
  };
  const sectionPageNumbers = useMemo(
    () => (selectedSection?.pageIds ?? []).map((id) => Number(id)).filter((n) => !Number.isNaN(n)),
    [selectedSection?.pageIds],
  );
  const comparing = showEvaluation && !!baselineData;

  const ctx: SectionEditorContext = {
    data: localData,
    setData: (next) => setLocalData(next),
    selectedSectionId: selectedSection?.sectionId ?? selectedSectionId,
    hasChanges,
  };

  /* The open section's class comes from its loaded result, which may carry an unsaved
     edit; the others come from the payload. Falling back to the payload while the
     result is still loading keeps an entry from flashing "(no class)" at a section
     that has one. */
  const sectionLabel = (s: ReviewSectionRef): string =>
    sectionTabLabel(s.sectionId, s.sectionId === selectedSectionId && localData ? documentClassType : s.documentClass);
  // The widget's buttons, right-aligned in its header: Show Evaluation (where the document
  // has a baseline), the section, the fields to show, then Edit Page Grouping. They act
  // on the editor, so they are hidden while `body` (the source document) is shown.
  // The dropdown holds any number of sections in one line, and is shown for one too.
  const sectionOptions = sections.map((s) => ({ value: s.sectionId, label: sectionLabel(s) }));
  const fieldsShown = activeTabId === 'visual' && !isLoading && hasFieldValues;
  const widgetButtons = (
    <SpaceBetween direction="horizontal" size="xs" alignItems="center">
      {evaluation?.available && (
        <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
          <Toggle
            checked={showEvaluation}
            disabled={evaluation.loading || !baselineData}
            onChange={({ detail }) => setShowEvaluation(detail.checked)}
          >
            Show Evaluation
          </Toggle>
          {evaluation.loading && <Spinner />}
        </SpaceBetween>
      )}
      <Select
        inlineLabelText="Section"
        selectedOption={sectionOptions.find((o) => o.value === selectedSectionId) ?? null}
        onChange={({ detail }) => void handleSectionChange(detail.selectedOption.value ?? selectedSectionId)}
        options={sectionOptions}
        disabled={sections.length < 2}
      />
      {/* The filter applies to the Visual Editor's fields, so it is disabled on the JSON tab. */}
      <Select
        inlineLabelText="Show Fields"
        selectedOption={filterMode}
        onChange={({ detail }) => setFilterMode(detail.selectedOption)}
        disabled={!fieldsShown}
        options={[
          FILTER_ALL,
          FILTER_ALERTS,
          ...(evaluation?.available ? [{ label: 'Evaluation mismatches only', value: 'eval-mismatches', disabled: !showEvaluation }] : []),
        ]}
      />
      {regroup && (
        <Button
          disabled={!!regroup.disabledReason}
          disabledReason={regroup.disabledReason}
          loading={regroup.loading}
          onClick={regroup.onClick}
        >
          Edit Page Grouping
        </Button>
      )}
    </SpaceBetween>
  );
  const noGeometry =
    !explainabilityInfo && localData ? 'No field geometry available for this baseline — bounding-box highlighting is disabled.' : null;

  // The page image stays in view while the fields scroll, and the fields pane is sized
  // to the viewport rather than a fixed height.
  const panes = (
    <div style={{ display: 'flex', gap: '16px', alignItems: 'flex-start' }}>
      <div style={{ flex: 1, minWidth: 0, position: 'sticky', top: 0 }}>
        {pagesLoading && (
          <Box textAlign="center" padding="xl">
            <Spinner /> Rendering document pages…
          </Box>
        )}
        {pagesNotice}
        {!pagesLoading && sectionPages.length > 0 && (
          <PageImageViewer pageIds={pageIds} documentPages={sectionPages} activeFieldGeometry={activeFieldGeometry} />
        )}
        {noGeometry && (
          <Box variant="small" color="text-body-secondary">
            {noGeometry}
          </Box>
        )}
      </div>

      <div style={{ flex: 1, minWidth: 0, maxHeight: WIDGET_PANE_HEIGHT, overflowY: 'auto' }}>
        {isLoading && (
          <Box textAlign="center" padding="xl">
            <Spinner /> Loading the section…
          </Box>
        )}
        {!isLoading && localData && (
          <Tabs
            activeTabId={activeTabId}
            onChange={({ detail }) => setActiveTabId(detail.activeTabId)}
            tabs={[
              {
                id: 'visual',
                label: 'Visual Editor',
                content: (
                  <SpaceBetween size="s">
                    {classSlot && classSlot(ctx)}
                    {/* The class comparison is shown whenever a baseline exists: the
                          per-field comparison stays behind Show Evaluation. */}
                    {baselineData && evaluation && (
                      <SectionClassEvaluation
                        index={evaluation.classificationIndex}
                        pageNumbers={sectionPageNumbers}
                        predictedClass={documentClassType}
                        baselineClass={(baselineData.document_class as Record<string, unknown> | undefined)?.type as string | undefined}
                      />
                    )}
                    {comparing && (
                      <Alert type="info" header="Evaluation Comparison Mode">
                        Showing predicted values with evaluation baseline. Fields with mismatches are highlighted with evaluation scores and
                        reasons.
                      </Alert>
                    )}
                    {baselineData && !showEvaluation && (
                      <Box variant="small" color="text-body-secondary">
                        This document has an evaluation baseline. Turn on <b>Show Evaluation</b> to compare every field against it, not just
                        the class.
                      </Box>
                    )}
                    {hasFieldValues ? (
                      <>
                        {/* One affordance for the selected field rather than a button on
                              every one of them: a bank statement section runs to hundreds of
                              fields, and the reviewer has already clicked the one they mean
                              in order to look at it. */}
                        {buildFieldLink && selectedFieldPath && (
                          <FormField
                            label="Ask someone about this field"
                            description={
                              <>
                                Copies a link that opens this document with <b>{selectedFieldPath}</b> selected. Share it when you need a
                                second opinion on a value.
                              </>
                            }
                          >
                            <CopyToClipboard
                              variant="button"
                              copyButtonText="Copy link to field"
                              textToCopy={buildFieldLink(selectedFieldPath)}
                              copySuccessText="Field link copied"
                              copyErrorText="Could not copy the field link"
                            />
                          </FormField>
                        )}
                        <FormFieldRenderer
                          fieldKey="Document Data"
                          value={inferenceResult}
                          onChange={updateInferenceResult}
                          isReadOnly={isReadOnly}
                          onFieldFocus={handleFieldFocus}
                          onFieldDoubleClick={handleFieldFocus}
                          onFieldPathSelect={setSelectedFieldPath}
                          editedFieldPaths={predictionChanges}
                          path={[]}
                          explainabilityInfo={explainabilityInfo}
                          mergedConfig={mergedConfig ?? null}
                          baselineValue={comparing ? baselineInference : null}
                          showComparison={comparing}
                          evaluationResults={comparing ? evaluation?.evaluationResults : null}
                          sectionId={selectedSection?.sectionId ?? null}
                          onBaselineChange={canEditBaseline ? updateBaselineInference : undefined}
                          baselineChanges={baselineChanges}
                          collapsedPaths={collapsedPaths}
                          onToggleCollapse={handleToggleCollapse}
                          filterMode={filterMode.value}
                          displayPath={[]}
                        />
                      </>
                    ) : emptyFieldsSlot ? (
                      emptyFieldsSlot(ctx)
                    ) : (
                      <Alert type="info" header="No field values for this section">
                        {inferenceResult
                          ? 'This section has an empty result: extraction ran but produced no fields.'
                          : 'This section has no inference_result at all.'}
                      </Alert>
                    )}
                  </SpaceBetween>
                ),
              },
              {
                id: 'json',
                label: 'JSON Editor',
                content: (
                  <JSONEditorTab
                    predictionData={localData}
                    baselineData={baselineData}
                    isReadOnly={isReadOnly}
                    onPredictionChange={(data) => setLocalData(data)}
                    onBaselineChange={canEditBaseline && evaluation ? (data) => evaluation.setBaseline(data) : undefined}
                    showBaseline={comparing}
                    isBaselineAvailable={!!evaluation?.available}
                    loadingEvaluation={!!evaluation?.loading}
                  />
                ),
              },
            ]}
          />
        )}
      </div>
    </div>
  );

  return (
    <Container
      header={
        <Header variant="h2" actions={body ? undefined : widgetButtons}>
          Document Editor
        </Header>
      }
    >
      <SpaceBetween size="s">
        {!body && headerExtras && headerExtras(ctx)}
        {error && <Alert type="error">{error}</Alert>}
        {body ?? panes}
      </SpaceBetween>
    </Container>
  );
};

export default SectionEditor;
