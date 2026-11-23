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
  ButtonDropdown,
  Container,
  CopyToClipboard,
  FormField,
  Header,
  SpaceBetween,
  Spinner,
  Tabs,
  Toggle,
} from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import {
  defineMessages,
  FormattedMessage,
  useIntl,
  type IntlShape,
  type MessageTag,
  type NoMessageValues,
  type TypedMessageDescriptor,
} from 'react-intl';
import useUnsavedChangesGuard from '../../hooks/use-unsaved-changes-guard';
import PageImageViewer from '../common/PageImageViewer';
import FormFieldRenderer from '../document-viewer/FormFieldRenderer';
import JSONEditorTab from '../document-viewer/JSONEditorTab';
import { valueSourceLabel } from '../common/labels';
import { SectionClassEvaluation } from '../common/ClassMismatchIndicator';
import { buildEditEntry, diffLeafPaths } from './document-edit-save';
import { collapseKeysAbove, focusFieldNode } from './field-focus';
import type { FieldFocusRequest } from './field-focus';
import type { LoadedSection, ReviewPage, ReviewSectionRef } from './types';
import type { SectionEvaluation } from './use-section-evaluation';
import { editPageErrorText, editScreenSharedMessages } from './edit-messages';

const logger = new ConsoleLogger('SectionEditor');

type ErrorText = { readonly error: string };
type Bold = { readonly b: MessageTag };

const messages = defineMessages<{
  sectionTab: { readonly sectionId: string; readonly documentClass: string };
  sectionTabNoClass: { readonly sectionId: string };
  loadFailed: ErrorText;
  saveFailed: ErrorText;
  unsavedChanges: NoMessageValues;
  switchDiscardConfirm: NoMessageValues;
  showEvaluation: NoMessageValues;
  sectionAriaLabel: { readonly section: string };
  sectionFallback: NoMessageValues;
  filterAriaLabel: { readonly filter: string };
  filterAll: NoMessageValues;
  filterAlerts: NoMessageValues;
  filterEvalMismatches: NoMessageValues;
  filterNeedsEvaluation: NoMessageValues;
  editPageGrouping: NoMessageValues;
  noGeometry: NoMessageValues;
  renderingPages: NoMessageValues;
  loadingSection: NoMessageValues;
  tabVisual: NoMessageValues;
  tabJson: NoMessageValues;
  comparisonHeader: NoMessageValues;
  comparisonBody: NoMessageValues;
  baselineHint: Bold;
  fieldLinkLabel: NoMessageValues;
  fieldLinkDescription: Bold & { readonly path: string };
  fieldLinkCopy: NoMessageValues;
  fieldLinkCopied: NoMessageValues;
  fieldLinkCopyFailed: NoMessageValues;
  noValuesHeader: NoMessageValues;
  noValuesEmpty: NoMessageValues;
  noValuesMissing: NoMessageValues;
}>({
  sectionTab: {
    id: 'editScreen.sectionEditor.sectionTab',
    defaultMessage: 'Section {sectionId} ({documentClass})',
    description:
      "A section's name in the Section menu of the edit page's section editor, and on the menu's button. {sectionId} is the section's id and {documentClass} its document class, as the document holds them.",
  },
  sectionTabNoClass: {
    id: 'editScreen.sectionEditor.sectionTabNoClass',
    defaultMessage: 'Section {sectionId} (no class)',
    description:
      "A section's name in the Section menu of the edit page's section editor, and on the menu's button, for a section with no document class. {sectionId} is the section's id.",
  },
  loadFailed: {
    id: 'editScreen.sectionEditor.loadFailed',
    defaultMessage: 'Failed to load ground truth: {error}',
    description:
      "Error in the edit page's section editor when the open section's results could not be read. {error} is why: the server's error text, shown as is, or, for an error the edit page raises itself, that error's own message in the user's language.",
  },
  saveFailed: {
    id: 'editScreen.sectionEditor.saveFailed',
    defaultMessage: 'Failed to save: {error}',
    description:
      "Error in the edit page's section editor when the open section could not be saved. {error} is why: the server's error text, shown as is, or, for an error the edit page raises itself, that error's own message in the user's language.",
  },
  unsavedChanges: {
    id: 'editScreen.sectionEditor.unsavedChanges',
    defaultMessage: 'You have unsaved changes. Leave this document and discard them?',
    description:
      'Confirmation the browser shows when the user follows a link in the app away from the edit page with unsaved edits in its section editor.',
  },
  switchDiscardConfirm: {
    id: 'editScreen.sectionEditor.switchDiscardConfirm',
    defaultMessage: 'You have unsaved ground truth changes. Discard them and switch sections?',
    description:
      "Confirmation the browser shows when the user picks another section in the Section menu of the edit page's section editor with unsaved edits.",
  },
  showEvaluation: {
    id: 'editScreen.sectionEditor.showEvaluation',
    defaultMessage: 'Show Evaluation',
    description:
      "Toggle in the header of the edit page's section editor that compares every field with the document's evaluation baseline.",
  },
  sectionAriaLabel: {
    id: 'editScreen.sectionEditor.sectionAriaLabel',
    defaultMessage: 'Section: {section}',
    description:
      "Screen-reader name of the Section menu in the header of the edit page's section editor. {section} is the open section's name, as the menu shows it.",
  },
  sectionFallback: {
    id: 'editScreen.sectionEditor.sectionFallback',
    defaultMessage: 'Section',
    description: "Text of the Section menu's button in the header of the edit page's section editor when the document lists no section.",
  },
  filterAriaLabel: {
    id: 'editScreen.sectionEditor.filterAriaLabel',
    defaultMessage: 'Show fields: {filter}',
    description:
      "Screen-reader name of the menu in the header of the edit page's section editor that chooses which fields the Visual Editor tab shows. {filter} is the current choice, as the menu shows it.",
  },
  filterAll: {
    id: 'editScreen.sectionEditor.filter.all',
    defaultMessage: 'All fields',
    description:
      "Choice of the menu in the header of the edit page's section editor that chooses which fields the Visual Editor tab shows: every field.",
  },
  filterAlerts: {
    id: 'editScreen.sectionEditor.filter.alerts',
    defaultMessage: 'Confidence alerts only',
    description:
      "Choice of the menu in the header of the edit page's section editor that chooses which fields the Visual Editor tab shows: the fields whose confidence is below their threshold.",
  },
  filterEvalMismatches: {
    id: 'editScreen.sectionEditor.filter.evalMismatches',
    defaultMessage: 'Evaluation mismatches only',
    description:
      "Choice of the menu in the header of the edit page's section editor that chooses which fields the Visual Editor tab shows: the fields that differ from the evaluation baseline.",
  },
  filterNeedsEvaluation: {
    id: 'editScreen.sectionEditor.filterNeedsEvaluation',
    defaultMessage: 'Turn on Show Evaluation first',
    description:
      "Why the Evaluation mismatches only choice of the edit page's section editor is unavailable. Show Evaluation is the toggle beside the menu.",
  },
  editPageGrouping: {
    id: 'editScreen.sectionEditor.editPageGrouping',
    defaultMessage: 'Edit Page Grouping',
    description:
      "Button in the header of the edit page's section editor that opens the Edit Page Grouping dialog, which groups the document's pages into sections.",
  },
  noGeometry: {
    id: 'editScreen.sectionEditor.noGeometry',
    defaultMessage: 'No field geometry available for this baseline — bounding-box highlighting is disabled.',
    description:
      "Note under the page images of the edit page's section editor when the open section's results hold no explainability_info, so no field can be highlighted on the page images.",
  },
  renderingPages: {
    id: 'editScreen.sectionEditor.renderingPages',
    defaultMessage: 'Rendering document pages…',
    description: "Shown beside a spinner in the edit page's section editor while the document's page images load.",
  },
  loadingSection: {
    id: 'editScreen.sectionEditor.loadingSection',
    defaultMessage: 'Loading the section…',
    description: "Shown beside a spinner in the edit page's section editor while the open section's results are read.",
  },
  tabVisual: {
    id: 'editScreen.sectionEditor.tab.visual',
    defaultMessage: 'Visual Editor',
    description: "Tab of the edit page's section editor that shows the open section's fields as a form.",
  },
  tabJson: {
    id: 'editScreen.sectionEditor.tab.json',
    defaultMessage: 'JSON Editor',
    description: "Tab of the edit page's section editor that shows the open section's results as raw JSON.",
  },
  comparisonHeader: {
    id: 'editScreen.sectionEditor.comparison.header',
    defaultMessage: 'Evaluation Comparison Mode',
    description: "Heading of the note in the Visual Editor tab of the edit page's section editor while Show Evaluation is on.",
  },
  comparisonBody: {
    id: 'editScreen.sectionEditor.comparison.body',
    defaultMessage:
      'Showing predicted values with evaluation baseline. Fields with mismatches are highlighted with evaluation scores and reasons.',
    description: "Text of the note in the Visual Editor tab of the edit page's section editor while Show Evaluation is on.",
  },
  baselineHint: {
    id: 'editScreen.sectionEditor.baselineHint',
    defaultMessage:
      'This document has an evaluation baseline. Turn on <b>Show Evaluation</b> to compare every field against it, not just the class.',
    description:
      "Hint in the Visual Editor tab of the edit page's section editor while Show Evaluation is off and the document has an evaluation baseline. <b>Show Evaluation</b> names the toggle in the editor's header.",
  },
  fieldLinkLabel: {
    id: 'editScreen.sectionEditor.fieldLink.label',
    defaultMessage: 'Ask someone about this field',
    description:
      "Label above the button in the Visual Editor tab of the edit page's section editor that copies a link to the selected field.",
  },
  fieldLinkDescription: {
    id: 'editScreen.sectionEditor.fieldLink.description',
    defaultMessage:
      'Copies a link that opens this document with <b>{path}</b> selected. Share it when you need a second opinion on a value.',
    description:
      "Text under Ask someone about this field in the Visual Editor tab of the edit page's section editor. {path} is the selected field's path in the document's results (such as LineItems[0].Rate), shown as is.",
  },
  fieldLinkCopy: {
    id: 'editScreen.sectionEditor.fieldLink.copy',
    defaultMessage: 'Copy link to field',
    description: "Button in the Visual Editor tab of the edit page's section editor that copies a link to the selected field.",
  },
  fieldLinkCopied: {
    id: 'editScreen.sectionEditor.fieldLink.copied',
    defaultMessage: 'Field link copied',
    description: "Shown after Copy link to field, in the edit page's section editor, copied the link.",
  },
  fieldLinkCopyFailed: {
    id: 'editScreen.sectionEditor.fieldLink.copyFailed',
    defaultMessage: 'Could not copy the field link',
    description: "Shown after Copy link to field, in the edit page's section editor, could not copy the link.",
  },
  noValuesHeader: {
    id: 'editScreen.sectionEditor.noValues.header',
    defaultMessage: 'No field values for this section',
    description:
      "Heading of the note in the Visual Editor tab of the edit page's section editor when the open section's results hold no field.",
  },
  noValuesEmpty: {
    id: 'editScreen.sectionEditor.noValues.empty',
    defaultMessage: 'This section has an empty result: extraction ran but produced no fields.',
    description: "Text of the note in the edit page's section editor when the open section's results hold an empty inference_result.",
  },
  noValuesMissing: {
    id: 'editScreen.sectionEditor.noValues.missing',
    defaultMessage: 'This section has no inference_result at all.',
    description:
      "Text of the note in the edit page's section editor when the open section's results hold no inference_result. inference_result is the results' JSON key, kept as is.",
  },
});

/** Renders a `<b>...</b>` element value. */
const bold = (chunks: React.ReactNode[]): React.JSX.Element => <b>{chunks}</b>;

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
  /** Canonical path of a field to select on open ("LineItems[0].Rate"): the first focus request. */
  focusFieldPath?: string | null;
  /** Move to a field: open its section, expand the groups above it, scroll to it and focus it. */
  focusRequest?: FieldFocusRequest | null;
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

/** Which fields the Visual Editor tab shows: the values the field renderer filters by. */
type FilterValue = 'none' | 'confidence-alerts' | 'eval-mismatches';
const filterMessages: Record<FilterValue, TypedMessageDescriptor<NoMessageValues>> = {
  none: messages.filterAll,
  'confidence-alerts': messages.filterAlerts,
  'eval-mismatches': messages.filterEvalMismatches,
};
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
const sectionTabLabel = (sectionId: string, documentClass: string | null | undefined, intl: IntlShape): string =>
  documentClass
    ? intl.formatMessage(messages.sectionTab, { sectionId, documentClass })
    : intl.formatMessage(messages.sectionTabNoClass, { sectionId });

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
  focusRequest = null,
  buildFieldLink = null,
  reloadToken = 0,
  headerExtras,
  classSlot,
  emptyFieldsSlot,
  pagesNotice,
  pagesLoading = false,
  autosaveMs,
  confirmSectionSwitch = true,
  unsavedChangesMessage,
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
  const intl = useIntl();
  const firstSectionId = (): string =>
    (initialSectionId && sections.some((s) => s.sectionId === initialSectionId) ? initialSectionId : sections[0]?.sectionId) ?? '1';
  const [selectedSectionId, setSelectedSectionId] = useState<string>(firstSectionId);
  const [localData, setLocalData] = useState<Record<string, unknown> | null>(null);
  const [originalJson, setOriginalJson] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  // Held as the message and the error, and worded where it shows.
  const [error, setError] = useState<{ message: TypedMessageDescriptor<ErrorText>; cause: unknown } | null>(null);
  const [activeFieldGeometry, setActiveFieldGeometry] = useState<Record<string, unknown> | null>(null);
  // Canonical path of the field the reviewer last clicked, so it can be linked.
  const [selectedFieldPath, setSelectedFieldPath] = useState<string | null>(null);
  const [collapsedPaths, setCollapsedPaths] = useState<Set<string>>(new Set());
  const [filterMode, setFilterMode] = useState<FilterValue>(defaultFilter);
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
        if (!cancelled) setError({ message: messages.loadFailed, cause: err });
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

  // Test-set values carry a label source and are ground truth, not predictions.
  const labelSource = localData?.labelSource;
  const valueLabels = useMemo(() => valueSourceLabel(intl, typeof labelSource === 'string' ? labelSource : null), [intl, labelSource]);
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
      setError({ message: messages.saveFailed, cause: err });
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
  useUnsavedChangesGuard(isDirty, unsavedChangesMessage ?? intl.formatMessage(messages.unsavedChanges));

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

  /*
   * Bring a requested field on screen, select it and focus it: the `?field=` link first,
   * then each review reason or Next alert (a new nonce repeats the same field). The
   * section switches first; the field is focused once that section's data has rendered,
   * hence the dependency on `localData` and `isLoading` rather than on the request alone.
   */
  const initialFocus = useMemo<FieldFocusRequest | null>(
    () => (focusFieldPath ? { sectionId: '', fieldPath: focusFieldPath, nonce: 0 } : null),
    [focusFieldPath],
  );
  const request = focusRequest ?? initialFocus;
  const [pendingFocus, setPendingFocus] = useState<FieldFocusRequest | null>(null);
  useEffect(() => {
    if (!request) return;
    setPendingFocus(request);
    if (request.sectionId && request.sectionId !== selectedSectionId && sections.some((s) => s.sectionId === request.sectionId)) {
      setSelectedSectionId(request.sectionId);
    }
  }, [request?.nonce, request?.sectionId, request?.fieldPath]);
  useEffect(() => {
    if (!pendingFocus || !localData || isLoading) return undefined;
    if (pendingFocus.sectionId && pendingFocus.sectionId !== selectedSection?.sectionId) return undefined;
    const { fieldPath } = pendingFocus;
    if (!fieldPath) {
      setPendingFocus(null);
      return undefined;
    }
    setSelectedFieldPath(fieldPath);
    setActiveTabId('visual');
    const above = collapseKeysAbove(fieldPath);
    setCollapsedPaths((prev) => (above.some((k) => prev.has(k)) ? new Set([...prev].filter((k) => !above.includes(k))) : prev));
    // Defer to the paint that renders the fields; the node does not exist yet on the
    // tick that localData lands.
    const timer = window.setTimeout(() => {
      focusFieldNode(fieldPath);
      setPendingFocus(null);
    }, 100);
    return () => window.clearTimeout(timer);
  }, [pendingFocus, localData, isLoading, selectedSection?.sectionId]);

  const handleToggleCollapse = (pathKey: string) => {
    setCollapsedPaths((prev) => {
      const next = new Set(prev);
      if (next.has(pathKey)) next.delete(pathKey);
      else next.add(pathKey);
      return next;
    });
  };

  const handleSectionChange = async (sectionId: string) => {
    if (confirmSectionSwitch && isDirty && !window.confirm(intl.formatMessage(messages.switchDiscardConfirm))) {
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
    sectionTabLabel(s.sectionId, s.sectionId === selectedSectionId && localData ? documentClassType : s.documentClass, intl);
  // The widget's buttons, right-aligned in its header: Show Evaluation (where the document
  // has a baseline), the section, the fields to show, then Edit Page Grouping. They act
  // on the editor, so they are hidden while `body` (the source document) is shown.
  // The section and the fields to show are dropdown buttons, as More Actions is, each
  // reading the current choice, which the open menu marks; the section one holds any
  // number of sections, and is shown for one too.
  const sectionOptions = sections.map((s) => ({ value: s.sectionId, label: sectionLabel(s) }));
  const currentSection = sectionOptions.find((o) => o.value === selectedSectionId) ?? sectionOptions[0];
  const fieldsShown = activeTabId === 'visual' && !isLoading && hasFieldValues;
  const filterValues: FilterValue[] = ['none', 'confidence-alerts', ...(evaluation?.available ? (['eval-mismatches'] as const) : [])];
  const widgetButtons = (
    <SpaceBetween direction="horizontal" size="xs" alignItems="center">
      {evaluation?.available && (
        <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
          <Toggle
            checked={showEvaluation}
            disabled={evaluation.loading || !baselineData}
            onChange={({ detail }) => setShowEvaluation(detail.checked)}
          >
            <FormattedMessage {...messages.showEvaluation} />
          </Toggle>
          {evaluation.loading && <Spinner />}
        </SpaceBetween>
      )}
      <ButtonDropdown
        ariaLabel={intl.formatMessage(messages.sectionAriaLabel, { section: currentSection?.label ?? '' })}
        disabled={sections.length < 2}
        expandToViewport
        items={sectionOptions.map((o) => ({
          id: o.value,
          text: o.label,
          itemType: 'checkbox' as const,
          checked: o.value === currentSection?.value,
        }))}
        onItemClick={({ detail }) => {
          if (detail.id !== currentSection?.value) void handleSectionChange(detail.id);
        }}
      >
        {currentSection?.label ?? <FormattedMessage {...messages.sectionFallback} />}
      </ButtonDropdown>
      {/* The filter applies to the Visual Editor's fields, so it is disabled on the JSON tab. */}
      <ButtonDropdown
        ariaLabel={intl.formatMessage(messages.filterAriaLabel, { filter: intl.formatMessage(filterMessages[filterMode]) })}
        disabled={!fieldsShown}
        expandToViewport
        items={filterValues.map((value) => ({
          id: value,
          text: intl.formatMessage(filterMessages[value]),
          itemType: 'checkbox' as const,
          checked: value === filterMode,
          ...(value === 'eval-mismatches' && !showEvaluation
            ? { disabled: true, disabledReason: intl.formatMessage(messages.filterNeedsEvaluation) }
            : {}),
        }))}
        onItemClick={({ detail }) => {
          const next = filterValues.find((value) => value === detail.id);
          if (next) setFilterMode(next);
        }}
      >
        <FormattedMessage {...filterMessages[filterMode]} />
      </ButtonDropdown>
      {regroup && (
        <Button
          disabled={!!regroup.disabledReason}
          disabledReason={regroup.disabledReason}
          loading={regroup.loading}
          onClick={regroup.onClick}
        >
          <FormattedMessage {...messages.editPageGrouping} />
        </Button>
      )}
    </SpaceBetween>
  );
  const noGeometry = !explainabilityInfo && localData ? intl.formatMessage(messages.noGeometry) : null;

  // The page image stays in view while the fields scroll, and the fields pane is sized
  // to the viewport rather than a fixed height.
  const panes = (
    <div style={{ display: 'flex', gap: '16px', alignItems: 'flex-start' }}>
      <div style={{ flex: 1, minWidth: 0, position: 'sticky', top: 0 }}>
        {pagesLoading && (
          <Box textAlign="center" padding="xl">
            <Spinner /> <FormattedMessage {...messages.renderingPages} />
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
            <Spinner /> <FormattedMessage {...messages.loadingSection} />
          </Box>
        )}
        {!isLoading && localData && (
          <Tabs
            activeTabId={activeTabId}
            onChange={({ detail }) => setActiveTabId(detail.activeTabId)}
            tabs={[
              {
                id: 'visual',
                label: intl.formatMessage(messages.tabVisual),
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
                      <Alert type="info" header={intl.formatMessage(messages.comparisonHeader)}>
                        <FormattedMessage {...messages.comparisonBody} />
                      </Alert>
                    )}
                    {baselineData && !showEvaluation && (
                      <Box variant="small" color="text-body-secondary">
                        <FormattedMessage {...messages.baselineHint} values={{ b: bold }} />
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
                            label={intl.formatMessage(messages.fieldLinkLabel)}
                            description={
                              <FormattedMessage {...messages.fieldLinkDescription} values={{ path: selectedFieldPath, b: bold }} />
                            }
                          >
                            <CopyToClipboard
                              variant="button"
                              copyButtonText={intl.formatMessage(messages.fieldLinkCopy)}
                              textToCopy={buildFieldLink(selectedFieldPath)}
                              copySuccessText={intl.formatMessage(messages.fieldLinkCopied)}
                              copyErrorText={intl.formatMessage(messages.fieldLinkCopyFailed)}
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
                          filterMode={filterMode}
                          displayPath={[]}
                          valueLabel={valueLabels}
                        />
                      </>
                    ) : emptyFieldsSlot ? (
                      emptyFieldsSlot(ctx)
                    ) : (
                      <Alert type="info" header={intl.formatMessage(messages.noValuesHeader)}>
                        {inferenceResult ? (
                          <FormattedMessage {...messages.noValuesEmpty} />
                        ) : (
                          <FormattedMessage {...messages.noValuesMissing} />
                        )}
                      </Alert>
                    )}
                  </SpaceBetween>
                ),
              },
              {
                id: 'json',
                label: intl.formatMessage(messages.tabJson),
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
                    predictionTitle={valueLabels.panelTitle}
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
          <FormattedMessage {...editScreenSharedMessages.documentEditor} />
        </Header>
      }
    >
      <SpaceBetween size="s">
        {!body && headerExtras && headerExtras(ctx)}
        {error && (
          <Alert type="error">
            <FormattedMessage {...error.message} values={{ error: editPageErrorText(error.cause, intl) }} />
          </Alert>
        )}
        {body ?? panes}
      </SpaceBetween>
    </Container>
  );
};

export default SectionEditor;
