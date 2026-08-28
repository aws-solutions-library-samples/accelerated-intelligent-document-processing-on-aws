// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * GroundTruthVisualEditor — edit a test set document's ground truth beside its
 * page images (Document Details' Visual Editor, recomposed for test sets).
 *
 * Left: page images rendered client-side from the source doc (no processed
 * page images exist for test set docs — see useTestDocPages). Right: tabs with
 * a visual form over the baseline's `inference_result` (plus document class /
 * page indices) and a raw JSON editor. Bounding boxes appear when the baseline
 * carries `explainability_info` geometry (i.e. it was minted via Copy to
 * Baseline from a processed doc); hand-built baselines simply have no boxes.
 *
 * Saves write the whole section result.json back to its TestSetBucket
 * baseline key via the uploadDocument presigned POST (Admin/Author only),
 * appending an _editHistory entry for provenance like VisualEditorModal does.
 */

import React, { useState, useEffect, useMemo } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  CopyToClipboard,
  FormField,
  Header,
  Input,
  Select,
  SegmentedControl,
  SpaceBetween,
  Spinner,
  Tabs,
} from '@cloudscape-design/components';
import type { SelectProps } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from '../../api/client-shim';
import { getErrorMessage } from '../../utils/errorUtils';
import { getFilePresignedUrl, uploadDocument, reextractTestSetDocument, getDraftLabelJob } from '../../graphql/generated';
import useAppContext from '../../contexts/app';
import useConfiguration from '../../hooks/use-configuration';
import useUnsavedChangesGuard from '../../hooks/use-unsaved-changes-guard';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import { describeClassConfigSource, resolveClassConfigVersion } from './classConfigVersion';
import { getConfigClassOptions } from '../common/config-class-options';
import PageImageViewer from '../common/PageImageViewer';
import FormFieldRenderer from '../document-viewer/FormFieldRenderer';
import JSONEditorTab from '../document-viewer/JSONEditorTab';
import EditHistoryTab from '../document-viewer/EditHistoryTab';
import useTestDocPages from '../../hooks/use-test-doc-pages';
import { renderLoadedLabelSource } from './TestSetDetail';

const client = generateClient();
const logger = new ConsoleLogger('GroundTruthVisualEditor');

const REEXTRACT_POLL_MS = 3000;
// A re-extract is a full extraction + assessment pass, so the budget is generous;
// timing out early would report failure on a run that is about to succeed.
const REEXTRACT_TIMEOUT_MS = 5 * 60 * 1000;

export interface TestSetDocumentSectionRef {
  sectionId: string;
  baselineKey: string;
  /**
   * 0-based page indices this section covers, from the queue/documents payload.
   *
   * Lets the page-regrouping editor show every section's grouping without fetching
   * each `result.json` again — the editor otherwise loads only the section being
   * viewed. Optional because the resolver omits it when a section's file could not be
   * read, which is not the same as the section having no pages.
   */
  pageIndices?: number[] | null;
}

interface GroundTruthVisualEditorProps {
  bucket: string;
  inputKey: string;
  objectKey: string;
  sections: TestSetDocumentSectionRef[];
  isReadOnly: boolean;
  /** Called after a successful save (e.g. to show a flash message). */
  onSaved?: (baselineKey: string) => void;
  /**
   * Optional replacement for how a save is persisted.
   *
   * The default writes the baseline object straight to S3 via a presigned POST,
   * which bypasses the HITL review API: no lock claim, no `reviewed-human` tag, no
   * confidence-curve observation. Callers that need those supply this to route
   * saves through `completeSectionReview` instead.
   */
  onSave?: (sectionId: string, data: Record<string, unknown>) => Promise<void>;
  /** Label for the save button. */
  saveButtonText?: string;
  /** Called after a re-extract completes, so the caller can refresh its queue. */
  onReextracted?: () => void;
  /**
   * Owning test set. Required only to offer re-extraction after a class
   * correction, since reextractTestSetDocument is keyed on the set.
   */
  testSetId?: string;
  /**
   * Config version whose classes to offer when the baseline carries no stamp of
   * its own — the test set's declared version, if the caller knows it. Without
   * it the active config is used; see the fallback chain in the component.
   */
  configVersion?: string;
  /**
   * Whether the caller's role may change this document's CLASS, which is a
   * different capability from editing its fields and is deliberately wider.
   *
   * A class correction persists through `reextractTestSetDocument`
   * (`Admin, Author, Annotator` — schema.graphql:1333-1334), which stamps the
   * baseline server-side and needs no review record. Field edits persist through
   * whichever save path the caller wired, and those accept different groups. Gating
   * the class dropdown on `isReadOnly` therefore denied the class to roles the
   * server accepts for it.
   *
   * Defaults to `!isReadOnly`, so a caller that does not distinguish them keeps
   * today's behaviour.
   */
  canChangeClass?: boolean;
  /**
   * Canonical path of a field to select on open ("LineItems[0].Rate"), from a
   * shared deep link. Ancestors are expanded so the field is actually on screen.
   */
  focusFieldPath?: string | null;
  /**
   * Builds a shareable link to one field. Supplied by callers that have a URL to
   * share (the annotation queue); omitted elsewhere, which hides the affordance
   * rather than offering a link that goes nowhere.
   */
  buildFieldLink?: ((fieldPath: string) => string) | null;
}

const getSectionLabel = (sectionId: string, data: Record<string, unknown> | null): string => {
  const docClass = (data?.document_class as Record<string, unknown> | undefined)?.type;
  return docClass ? `Section ${sectionId} (${String(docClass)})` : `Section ${sectionId}`;
};

const GroundTruthVisualEditor = ({
  bucket,
  inputKey,
  objectKey,
  sections,
  isReadOnly,
  onSaved,
  onSave,
  saveButtonText,
  onReextracted,
  testSetId,
  configVersion,
  canChangeClass,
  focusFieldPath = null,
  buildFieldLink = null,
}: GroundTruthVisualEditorProps): React.JSX.Element => {
  const { user } = useAppContext();
  const { pages, isLoading: pagesLoading, error: pagesError, previewUnavailable } = useTestDocPages(bucket, inputKey);

  const [selectedSectionId, setSelectedSectionId] = useState<string>(sections[0]?.sectionId ?? '1');
  const [localData, setLocalData] = useState<Record<string, unknown> | null>(null);
  const [originalJson, setOriginalJson] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeFieldGeometry, setActiveFieldGeometry] = useState<Record<string, unknown> | null>(null);
  // Canonical path of the field the reviewer last clicked, so it can be linked.
  const [selectedFieldPath, setSelectedFieldPath] = useState<string | null>(null);
  // See the prop's doc comment: the class is a wider capability than field editing.
  const mayChangeClass = canChangeClass ?? !isReadOnly;
  const [collapsedPaths, setCollapsedPaths] = useState<Set<string>>(new Set());
  const [filterMode, setFilterMode] = useState<SelectProps.Option>({ label: 'Show all fields', value: 'none' });
  const [activeTabId, setActiveTabId] = useState('visual');
  const [isReextracting, setIsReextracting] = useState(false);
  const [reextractNote, setReextractNote] = useState<string | null>(null);
  // The class the loaded baseline was extracted under; differing from the current
  // selection is what says the fields no longer match the class.
  const [savedClassType, setSavedClassType] = useState<string | undefined>(undefined);
  // Forces a baseline re-read after a re-extract rewrites it: the key is unchanged.
  const [reloadToken, setReloadToken] = useState(0);

  const selectedSection = sections.find((s) => s.sectionId === selectedSectionId) ?? sections[0];

  // Reset to the first section when switching documents.
  useEffect(() => {
    setSelectedSectionId(sections[0]?.sectionId ?? '1');
  }, [inputKey, sections]);

  // Load the selected section's baseline result.json. Bytes are fetched
  // straight from S3 via a server-issued presigned URL (same rationale as
  // JSONViewer: no Lambda 6MB cap, and the resolver's bucket allow-list
  // covers the TestSetBucket).
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
        const s3Uri = `s3://${bucket}/${selectedSection.baselineKey}`;
        const response = await client.graphql({
          query: getFilePresignedUrl,
          variables: { s3Uri },
        });
        const presignedUrl = response.data?.getFilePresignedUrl?.presignedUrl;
        if (!presignedUrl) throw new Error('No presigned URL returned by server');
        const s3Response = await fetch(presignedUrl);
        if (!s3Response.ok) throw new Error(`S3 fetch failed: ${s3Response.status}`);
        const text = await s3Response.text();
        if (cancelled) return;
        const parsed = JSON.parse(text) as Record<string, unknown>;
        setLocalData(parsed);
        setOriginalJson(text);
        setSavedClassType((parsed.document_class as Record<string, unknown> | undefined)?.type as string | undefined);
        setReextractNote(null);
      } catch (err) {
        logger.error('Error loading baseline:', err);
        if (!cancelled) setError(`Failed to load ground truth: ${getErrorMessage(err)}`);
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    };
    load();
    return () => {
      cancelled = true;
    };
  }, [bucket, selectedSection?.baselineKey, reloadToken]);

  const hasChanges = useMemo(() => {
    if (!localData || originalJson === null) return false;
    try {
      return JSON.stringify(localData) !== JSON.stringify(JSON.parse(originalJson));
    } catch {
      return true;
    }
  }, [localData, originalJson]);

  // Covers tab close AND in-app navigation. Only the former was handled, and the
  // latter is how edits were actually lost: a route change is not a page unload,
  // so clicking a nav link discarded everything with no prompt at all.
  useUnsavedChangesGuard(hasChanges, 'You have unsaved ground truth changes. Leave this document and discard them?');

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

  // Packet-splitting baselines carry the section's document-absolute page
  // indices (0-based). Use them to restrict the image pane to this section's
  // pages; otherwise show the whole document.
  const splitPageIndices = (localData?.split_document as Record<string, unknown> | undefined)?.page_indices as number[] | undefined;
  const sectionPages = useMemo(() => {
    if (!splitPageIndices?.length || pages.length === 0) return pages;
    return splitPageIndices.map((idx) => pages[idx]).filter(Boolean);
  }, [pages, splitPageIndices]);
  const pageIds = useMemo(() => sectionPages.map((p) => p.Id), [sectionPages]);

  const documentClassType = (localData?.document_class as Record<string, unknown> | undefined)?.type as string | undefined;

  // Which config's classes to offer, and why. See classConfigVersion.ts — the
  // fallback order is the whole substance of #662, so it lives in a tested
  // function rather than inline here.
  const stampedConfigVersion = (localData?.metadata as Record<string, unknown> | undefined)?.config_version as string | undefined;
  const { versions } = useConfigurationVersions();
  const activeConfigVersion = useMemo(() => versions.find((v) => v.isActive)?.versionName, [versions]);
  const classConfig = useMemo(
    () => resolveClassConfigVersion(stampedConfigVersion, configVersion, activeConfigVersion),
    [stampedConfigVersion, configVersion, activeConfigVersion],
  );
  const classConfigVersion = classConfig.version;
  const { mergedConfig, loading: configLoading, error: configError } = useConfiguration(classConfigVersion);
  const classOptions = useMemo(() => getConfigClassOptions(mergedConfig), [mergedConfig]);
  /**
   * True when the class list could not be read, as opposed to being genuinely empty.
   *
   * `getConfigVersion` is `Admin, Author, Viewer` (schema.graphql:1557-1558) and
   * excludes **Annotator** — the role this screen exists for. So an annotator's
   * config fetch is denied, `classOptions` comes back empty, and the editor used to
   * fall through to its free-text branch: the one role most in need of a constrained
   * vocabulary got an unconstrained box, and the only visible difference was three
   * words dropping out of the description.
   *
   * That matters more since the class became editable for annotators: a typed class
   * that no config defines produces a section with no schema, which extracts nothing.
   * The resolver bounds the characters but deliberately not the membership, because
   * it has no config-table grant either.
   */
  const classListUnavailable = classOptions.length === 0 && (Boolean(configError) || configLoading);
  // A class the config no longer lists stays selectable; otherwise a document whose
  // class was since renamed would silently blank the field.
  const classOptionsWithCurrent = useMemo(() => {
    if (!documentClassType || classOptions.some((o) => o.value === documentClassType)) return classOptions;
    return [{ label: documentClassType, value: documentClassType, description: 'Not defined in this config version' }, ...classOptions];
  }, [classOptions, documentClassType]);
  const classChanged = Boolean(savedClassType) && documentClassType !== savedClassType;
  const editHistoryCount = Array.isArray(localData?._editHistory) ? (localData._editHistory as unknown[]).length : 0;

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

  const updateDocumentClass = (newType: string) => {
    // mayChangeClass, not isReadOnly: guarding on the narrower flag would accept the
    // dropdown's change event and silently discard it, which is how the original
    // class-correction bug behaved.
    if (!mayChangeClass || !localData) return;
    const docClass = { ...((localData.document_class as Record<string, unknown>) ?? {}), type: newType };
    setLocalData({ ...localData, document_class: docClass });
  };

  /**
   * Re-run extraction under the corrected class, then reload the new labels.
   *
   * Blocks until the labels are replaced rather than returning once the job is
   * queued, because the fields on screen are wrong until then. Labels are
   * harvested on read, so this poll loop is what drives the write-back; it is not
   * merely observing.
   */
  const handleReextract = async () => {
    if (!documentClassType || !testSetId) return;
    setIsReextracting(true);
    setError(null);
    setReextractNote(null);
    try {
      const started = await client.graphql({
        query: reextractTestSetDocument,
        variables: {
          input: { testSetId, objectKey, documentClass: documentClassType, configVersion: classConfigVersion },
        },
      });
      const job = started.data?.reextractTestSetDocument;
      if (!job?.jobId) throw new Error('No job returned');

      const deadline = Date.now() + REEXTRACT_TIMEOUT_MS;
      let status = job.status;
      while (status === 'RUNNING' && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, REEXTRACT_POLL_MS));
        const polled = await client.graphql({
          query: getDraftLabelJob,
          variables: { testSetId, jobId: job.jobId },
        });
        status = polled.data?.getDraftLabelJob?.status ?? status;
        if (status === 'FAILED') {
          throw new Error(polled.data?.getDraftLabelJob?.error || 'Re-extraction failed');
        }
      }
      if (status === 'RUNNING') {
        // Not an error: the job is still running and the harvest is idempotent.
        setReextractNote(
          'Re-extraction is taking longer than expected. It is still running — reopen this document shortly to see the new fields.',
        );
        return;
      }

      setReloadToken((token) => token + 1);
      setReextractNote(`Re-extracted as ${documentClassType}.`);
      if (onReextracted) onReextracted();
    } catch (err) {
      logger.error('Re-extraction failed:', err);
      setError(`Could not re-extract this document: ${getErrorMessage(err)}`);
    } finally {
      setIsReextracting(false);
    }
  };

  const handleSave = async () => {
    if (!selectedSection || !localData) return;
    setIsSaving(true);
    setError(null);
    try {
      const dataToSave: Record<string, unknown> = { ...localData };
      const fullPath = selectedSection.baselineKey;

      // No client-side _editHistory entry on this path: the review API writes one
      // server-side with token-derived identity and a field-level diff, so appending
      // here would double-record every review with the weaker entry.
      if (onSave) {
        await onSave(selectedSection.sectionId, dataToSave);
        setLocalData(dataToSave);
        setOriginalJson(JSON.stringify(dataToSave, null, 2));
        logger.info('Saved ground truth via caller-supplied handler for', fullPath);
        if (onSaved) onSaved(fullPath);
        return;
      }

      // Direct-to-S3 path: nothing server-side records provenance, so the editor
      // writes its own entry (same convention as VisualEditorModal).
      const editHistory = (dataToSave._editHistory as unknown[]) || [];
      editHistory.push({
        timestamp: new Date().toISOString(),
        editedBy: (user as { username?: string } | undefined)?.username || 'unknown',
        source: 'test-set-ground-truth-editor',
      });
      dataToSave._editHistory = editHistory;
      const editedContent = JSON.stringify(dataToSave, null, 2);

      const fileName = fullPath.split('/').pop() ?? fullPath;
      const prefix = fullPath.substring(0, fullPath.lastIndexOf('/'));

      const response = await client.graphql({
        query: uploadDocument,
        variables: { fileName, contentType: 'application/json', prefix, bucket },
      });
      const { presignedUrl, usePostMethod } = response.data.uploadDocument;
      if (usePostMethod?.toLowerCase() !== 'true') {
        throw new Error('Server returned PUT method which is not supported');
      }
      const presignedPostData = JSON.parse(presignedUrl);
      const formData = new FormData();
      Object.entries(presignedPostData.fields as Record<string, string>).forEach(([key, value]) => {
        formData.append(key, value);
      });
      formData.append('file', new Blob([editedContent], { type: 'application/json' }), fileName);
      const uploadResponse = await fetch(presignedPostData.url, { method: 'POST', body: formData });
      if (!uploadResponse.ok) {
        const errorText = await uploadResponse.text().catch(() => 'Could not read error response');
        throw new Error(`Upload failed: ${errorText}`);
      }

      setLocalData(dataToSave);
      setOriginalJson(editedContent);
      logger.info('Saved ground truth to', fullPath);
      if (onSaved) onSaved(fullPath);
    } catch (err) {
      logger.error('Error saving ground truth:', err);
      setError(`Failed to save: ${getErrorMessage(err)}`);
    } finally {
      setIsSaving(false);
    }
  };

  const handleDiscard = () => {
    if (originalJson !== null) {
      setLocalData(JSON.parse(originalJson));
    }
  };

  const handleFieldFocus = (geometry: Record<string, unknown> | null) => {
    setActiveFieldGeometry(geometry ?? null);
  };

  /**
   * Bring a deep-linked field on screen and select it.
   *
   * Runs after the section's data has rendered, hence the dependency on
   * `localData` rather than on mount. Nothing collapses fields by default here,
   * so no ancestor expansion is needed — if that ever changes, this is where the
   * expansion belongs, because a link to a field inside a collapsed object would
   * otherwise scroll to nothing.
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

  const handleSectionChange = (sectionId: string) => {
    if (hasChanges && !window.confirm('You have unsaved ground truth changes. Discard them and switch sections?')) {
      return;
    }
    setSelectedSectionId(sectionId);
  };

  if (sections.length === 0) {
    return <Alert type="warning">No ground truth (baseline) sections found for {objectKey}.</Alert>;
  }

  return (
    <SpaceBetween size="s">
      <Header
        variant="h3"
        actions={
          !isReadOnly && (
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={handleDiscard} disabled={!hasChanges || isSaving}>
                Discard changes
              </Button>
              <Button variant="primary" onClick={handleSave} loading={isSaving} disabled={!hasChanges}>
                {saveButtonText ?? 'Save changes'}
              </Button>
            </SpaceBetween>
          )
        }
      >
        Ground truth — {objectKey}
      </Header>

      {/* Provenance is shown where the label is edited, so a machine draft cannot
          be mistaken on screen for confirmed work. */}
      {localData && (
        <SpaceBetween direction="horizontal" size="xs">
          {/* localData is only ever set from a baseline that loaded and parsed, so
              a missing labelSource here means uploaded ground truth — not the
              absence of labels. */}
          {renderLoadedLabelSource(localData.labelSource as string | undefined)}
          {/* Always shown, including the fallback cases. Hiding it whenever the
              version resolved to 'default' is exactly what let #662 go unnoticed:
              the classes on offer were the built-in preset's and nothing said so. */}
          <Badge color={classConfig.source === 'baseline' ? 'grey' : 'blue'}>Classes from: {describeClassConfigSource(classConfig)}</Badge>
          {editHistoryCount > 0 && (
            <Box fontSize="body-s" color="text-body-secondary">
              {editHistoryCount} revision{editHistoryCount === 1 ? '' : 's'} — see Revision History
            </Box>
          )}
        </SpaceBetween>
      )}

      {sections.length > 1 && (
        <SegmentedControl
          selectedId={selectedSectionId}
          onChange={({ detail }) => handleSectionChange(detail.selectedId)}
          options={sections.map((s) => ({
            id: s.sectionId,
            text: s.sectionId === selectedSectionId ? getSectionLabel(s.sectionId, localData) : `Section ${s.sectionId}`,
          }))}
        />
      )}

      {error && <Alert type="error">{error}</Alert>}
      {!explainabilityInfo && localData && (
        <Alert type="info">No field geometry available for this baseline — bounding-box highlighting is disabled.</Alert>
      )}

      <div style={{ display: 'flex', gap: '16px', alignItems: 'stretch' }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          {pagesLoading && (
            <Box textAlign="center" padding="xl">
              <Spinner /> Rendering document pages…
            </Box>
          )}
          {previewUnavailable && (
            <Alert type="info">
              Preview is not available for TIFF documents — use View Source Document to download. Ground truth editing still works.
            </Alert>
          )}
          {pagesError && <Alert type="error">{pagesError}</Alert>}
          {!pagesLoading && sectionPages.length > 0 && (
            <PageImageViewer pageIds={pageIds} documentPages={sectionPages} activeFieldGeometry={activeFieldGeometry} />
          )}
        </div>

        <div style={{ flex: 1, minWidth: 0, maxHeight: '760px', overflowY: 'auto' }}>
          {isLoading && (
            <Box textAlign="center" padding="xl">
              <Spinner /> Loading ground truth…
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
                      {documentClassType !== undefined && (
                        <FormField
                          label="Class label"
                          description={
                            classOptions.length > 0
                              ? 'What this section is classified as, from this config version. Distinct from the extraction labels below.'
                              : classListUnavailable
                                ? 'What this section is classified as. The list of valid classes could not be loaded, so it cannot be changed here.'
                                : 'What this section is classified as. Distinct from the extraction labels below.'
                          }
                          constraintText={
                            isReextracting
                              ? 'Locked while the re-extraction runs.'
                              : classListUnavailable
                                ? 'Your role cannot read the configuration this set was labelled with, so the valid classes are unknown. An Admin or Author can change the class.'
                                : !mayChangeClass
                                  ? 'You do not have permission to change this class.'
                                  : undefined
                          }
                        >
                          <SpaceBetween size="xs">
                            {/* Constrained to the config's classes: a class with no
                                schema cannot be extracted against, so the correction
                                could never take effect. Free text only when no
                                config resolves. */}
                            {classListUnavailable ? (
                              <Input value={documentClassType ?? ''} disabled />
                            ) : classOptions.length > 0 ? (
                              <Select
                                selectedOption={
                                  documentClassType
                                    ? (classOptionsWithCurrent.find((o) => o.value === documentClassType) ?? {
                                        label: documentClassType,
                                        value: documentClassType,
                                      })
                                    : null
                                }
                                onChange={({ detail }) => updateDocumentClass(detail.selectedOption.value ?? '')}
                                options={classOptionsWithCurrent}
                                disabled={!mayChangeClass || isReextracting}
                                placeholder="Choose a document class"
                              />
                            ) : (
                              <Input
                                value={documentClassType ?? ''}
                                onChange={({ detail }) => updateDocumentClass(detail.value)}
                                disabled={!mayChangeClass}
                              />
                            )}
                            {/* Correcting the class is only half the fix: the fields
                                were extracted against the previous schema and only a
                                re-extract replaces them. */}
                            {classChanged && testSetId && (
                              <Alert
                                type="info"
                                header="Fields still reflect the previous class"
                                action={
                                  <Button onClick={handleReextract} loading={isReextracting} disabled={!mayChangeClass}>
                                    {isReextracting ? 'Re-extracting…' : 'Change class & re-extract'}
                                  </Button>
                                }
                              >
                                These fields were extracted as <b>{savedClassType}</b>. Re-extract to replace them with ones the{' '}
                                <b>{documentClassType}</b> schema produces. This re-runs the document through the pipeline and usually takes
                                under a minute; you can leave this page and come back.
                                {localData?.labelSource === 'reviewed-human'
                                  ? ' This document was already marked reviewed; re-extracting discards those confirmed values.'
                                  : localData?.labelSource === 'draft-machine'
                                    ? ' The current draft labels for this document are replaced.'
                                    : ' The class is corrected, but this document has authored ground truth, so its field values are kept.'}
                              </Alert>
                            )}
                            {classChanged && !testSetId && (
                              <Alert type="warning">
                                The class will be saved, but this document has no processing run to re-extract from, so its fields will
                                still reflect the previous class.
                              </Alert>
                            )}
                            {reextractNote && <Alert type="success">{reextractNote}</Alert>}
                          </SpaceBetween>
                        </FormField>
                      )}
                      {splitPageIndices !== undefined && (
                        <FormField label="Page indices" description="0-based pages of this section within the document (read-only)">
                          <Input value={JSON.stringify(splitPageIndices)} disabled />
                        </FormField>
                      )}
                      {inferenceResult ? (
                        <>
                          <FormField label="Fields to show">
                            <Select
                              selectedOption={filterMode}
                              onChange={({ detail }) => setFilterMode(detail.selectedOption)}
                              options={[
                                { label: 'Show all fields', value: 'none' },
                                { label: 'Confidence alerts only', value: 'confidence-alerts' },
                              ]}
                            />
                          </FormField>
                          {/* One affordance for the selected field rather than a button on
                              every one of them: a bank statement section runs to hundreds of
                              fields, and the reviewer has already clicked the one they mean
                              in order to look at it. */}
                          {buildFieldLink && selectedFieldPath && (
                            <FormField
                              label="Ask someone about this field"
                              description={
                                <>
                                  Copies a link that opens this document with <b>{selectedFieldPath}</b> selected. Paste it in Slack when
                                  you need a second opinion on a value.
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
                            collapsedPaths={collapsedPaths}
                            onToggleCollapse={handleToggleCollapse}
                            filterMode={filterMode.value}
                            displayPath={[]}
                          />
                        </>
                      ) : (
                        <Alert type="warning">This baseline has no inference_result — use the JSON editor tab.</Alert>
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
                      baselineData={null}
                      isReadOnly={isReadOnly}
                      onPredictionChange={(data) => setLocalData(data)}
                      showBaseline={false}
                      isBaselineAvailable={false}
                      loadingEvaluation={false}
                    />
                  ),
                },
                {
                  id: 'history',
                  label: 'Revision History',
                  content: <EditHistoryTab predictionData={localData} baselineData={null} />,
                },
              ]}
            />
          )}
        </div>
      </div>
    </SpaceBetween>
  );
};

export default GroundTruthVisualEditor;
