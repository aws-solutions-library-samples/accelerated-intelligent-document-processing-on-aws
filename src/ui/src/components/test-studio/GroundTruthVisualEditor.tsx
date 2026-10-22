// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * GroundTruthVisualEditor — edit a test set document's ground truth beside its
 * page images (Document Details' Visual Editor, recomposed for test sets).
 *
 * The test-set adapter over the shared review workspace's SectionEditor: this file
 * owns what is specific to test sets (loading baselines, page images rendered from
 * the source document, the class control and re-extract, page regrouping, and the
 * direct-to-S3 save); the field editing and change tracking live in SectionEditor.
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

import React, { useState, useMemo } from 'react';
import { Alert, Badge, Box, Button, FormField, Input, Select, SpaceBetween } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from '../../api/client-shim';
import { getErrorMessage } from '../../utils/errorUtils';
import {
  getFilePresignedUrl,
  uploadDocument,
  reextractTestSetDocument,
  getDraftLabelJob,
  updateTestSetDocumentSections,
} from '../../graphql/generated';
import useAppContext from '../../contexts/app';
import useConfiguration from '../../hooks/use-configuration';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import { describeClassConfigSource, resolveClassConfigVersion } from './classConfigVersion';
import { getConfigClassOptions } from '../common/config-class-options';
import useTestDocPages from '../../hooks/use-test-doc-pages';
import { renderLoadedLabelSource } from './TestSetDetail';
import PageGroupingEditor from '../common/PageGroupingEditor';
import { pageIdsToIndices, pageIndicesToIds } from '../common/section-grouping';
import type { GroupedSection } from '../common/section-grouping';
import SectionEditor from '../review-workspace/SectionEditor';
import type { SectionEditorContext, SectionEditorController } from '../review-workspace/SectionEditor';
import type { LoadedSection, ReviewSectionRef } from '../review-workspace/types';
import {
  CLASS_LIST_UNAVAILABLE_CONSTRAINT,
  CLASS_LIST_UNAVAILABLE_DESCRIPTION,
  isClassListUnavailable,
  mayChangeClassFor,
} from './annotation-edit-flags';

const client = generateClient();
const logger = new ConsoleLogger('GroundTruthVisualEditor');

/**
 * Must match `LABEL_SOURCE_HUMAN` in `test_set_resolver/index.py`: the server derives
 * `reviewed` from an exact comparison against it, so a typo here would silently stop
 * annotation progress from counting rather than fail.
 */
const LABEL_SOURCE_REVIEWED_HUMAN = 'reviewed-human';

const REEXTRACT_POLL_MS = 3000;
// A re-extract is a full extraction + assessment pass, so the budget is generous;
// timing out early would report failure on a run that is about to succeed.
const REEXTRACT_TIMEOUT_MS = 5 * 60 * 1000;

export interface TestSetDocumentSectionRef {
  sectionId: string;
  baselineKey: string;
  /** This section's class, so the regrouping board can show one per section. */
  documentClass?: string | null;
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
   * The default writes the baseline object straight to S3 via a presigned POST, which
   * bypasses the HITL review API: no lock claim and no confidence-curve observation.
   * It does still tag the label `reviewed-human` — the editor writes that itself, since
   * on this path nothing server-side will, and the annotation progress metric keys on
   * it. Callers that want the lock and the calibration signal supply this to route
   * saves through `completeSectionReview` instead, which tags it server-side.
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
  /** Called when the open section gains or loses unsaved changes. */
  onDirtyChange?: (dirty: boolean) => void;
  /** Passed to SectionEditor: the unified editor's top bar drives save and discard. */
  controllerRef?: React.MutableRefObject<SectionEditorController | null>;
  actionsPlacement?: 'header' | 'external';
  showHistoryTab?: boolean;
  onSavingChange?: (saving: boolean) => void;
  saveOnSectionSwitch?: boolean;
  onDataChange?: (data: Record<string, unknown> | null) => void;
  /**
   * `structure` (the default) puts Edit Page Grouping beside the section selector and
   * shows the board inline. `external` leaves the button to the caller, which opens the
   * board as a modal through `regroupOpen`.
   */
  regroupPlacement?: 'structure' | 'external';
  regroupOpen?: boolean;
  onRegroupOpenChange?: (open: boolean) => void;
}

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
  onDirtyChange,
  controllerRef,
  actionsPlacement,
  showHistoryTab,
  onSavingChange,
  saveOnSectionSwitch,
  onDataChange,
  regroupPlacement = 'structure',
  regroupOpen = false,
  onRegroupOpenChange,
}: GroundTruthVisualEditorProps): React.JSX.Element => {
  const externalRegroup = regroupPlacement === 'external';
  const { user } = useAppContext();
  const { pages, isLoading: pagesLoading, error: pagesError, previewUnavailable } = useTestDocPages(bucket, inputKey);

  // The editor's working copy of the open section, mirrored so the class config can be
  // derived from it at the top level (hooks cannot run inside the editor's slots).
  const [localData, setLocalData] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isRegrouping, setIsRegrouping] = useState(false);
  const [isSavingGrouping, setIsSavingGrouping] = useState(false);
  /**
   * Sections whose pages changed in the last re-grouping.
   *
   * Kept so the warning names them: their field values were extracted from a different
   * set of pages and may no longer match. Deliberately not acted on — re-extracting
   * automatically is the annotation loss this whole feature exists to avoid.
   */
  const [regroupedSectionIds, setRegroupedSectionIds] = useState<string[]>([]);
  // See the prop's doc comment: the class is a wider capability than field editing.
  const mayChangeClass = mayChangeClassFor(canChangeClass, isReadOnly);
  const [isReextracting, setIsReextracting] = useState(false);
  const [reextractNote, setReextractNote] = useState<string | null>(null);
  // The class the loaded baseline was extracted under; differing from the current
  // selection is what says the fields no longer match the class.
  const [savedClassType, setSavedClassType] = useState<string | undefined>(undefined);
  // Forces a baseline re-read after a re-extract rewrites it: the key is unchanged.
  const [reloadToken, setReloadToken] = useState(0);

  const reviewSections = useMemo<ReviewSectionRef[]>(
    () =>
      sections.map((section) => ({
        sectionId: section.sectionId,
        documentClass: section.documentClass,
        resultUri: `s3://${bucket}/${section.baselineKey}`,
        // The baseline's own split_document.page_indices decides the image pane, as
        // before; an empty list keeps the whole-document fallback.
        pageIds: [],
      })),
    [sections, bucket],
  );
  const baselineKeyFor = (sectionId: string): string | undefined => sections.find((s) => s.sectionId === sectionId)?.baselineKey;

  // Load a section's baseline result.json. Bytes are fetched straight from S3 via a
  // server-issued presigned URL (same rationale as JSONViewer: no Lambda 6MB cap, and
  // the resolver's bucket allow-list covers the TestSetBucket).
  const loadSection = async (sectionId: string): Promise<LoadedSection> => {
    const s3Uri = `s3://${bucket}/${baselineKeyFor(sectionId)}`;
    const response = await client.graphql({
      query: getFilePresignedUrl,
      variables: { s3Uri },
    });
    const presignedUrl = response.data?.getFilePresignedUrl?.presignedUrl;
    if (!presignedUrl) throw new Error('No presigned URL returned by server');
    const s3Response = await fetch(presignedUrl);
    if (!s3Response.ok) throw new Error(`S3 fetch failed: ${s3Response.status}`);
    const text = await s3Response.text();
    return { data: JSON.parse(text) as Record<string, unknown>, raw: text };
  };

  const handleLoaded = (_sectionId: string, parsed: Record<string, unknown>) => {
    setSavedClassType((parsed.document_class as Record<string, unknown> | undefined)?.type as string | undefined);
    setReextractNote(null);
  };

  const documentClassType = (localData?.document_class as Record<string, unknown> | undefined)?.type as string | undefined;
  const splitPageIndices = (localData?.split_document as Record<string, unknown> | undefined)?.page_indices as number[] | undefined;

  // Which profile's classes to offer, and why. Preferring the profile stamped on the
  // baseline over the deployment's active one is develop's point — it may have moved on
  // since the labels were written — but a bare `|| 'default'` fallback is what #662 was:
  // the classes on offer silently became the built-in preset's and nothing said so. The
  // fallback order is the whole substance of that fix, so it lives in a tested function
  // (classConfigVersion.ts) rather than inline, and `classConfig.source` is what lets the
  // badge below name where the classes came from.
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
   * `getConfigVersion` grants **Annotator** alongside `Admin, Author, Viewer`, and a
   * caller with no other entitlement receives only the class vocabulary — the resolver
   * reduces the payload (`_class_vocabulary_only` in `configuration_resolver/index.py`,
   * whose `_FULL_CONFIG_GROUPS` is the boundary). Do not "tidy" either half: the grant
   * exists *because* of that reduction, and dropping the reduction would hand prompts
   * and model ids to the lowest-privilege role.
   *
   * The flag stays because a denied or failed fetch is still reachable — a role outside
   * all four groups, or a config error — and it must not be mistaken for a config that
   * genuinely defines no classes. See isClassListUnavailable.
   */
  const classListUnavailable = isClassListUnavailable(classOptions.length, configError, configLoading);
  // A class the config no longer lists stays selectable; otherwise a document whose
  // class was since renamed would silently blank the field.
  const classOptionsWithCurrent = useMemo(() => {
    if (!documentClassType || classOptions.some((o) => o.value === documentClassType)) return classOptions;
    return [
      { label: documentClassType, value: documentClassType, description: 'Not defined in this configuration profile' },
      ...classOptions,
    ];
  }, [classOptions, documentClassType]);
  const classChanged = Boolean(savedClassType) && documentClassType !== savedClassType;
  const editHistoryCount = Array.isArray(localData?._editHistory) ? (localData._editHistory as unknown[]).length : 0;

  const updateDocumentClass = (ctx: SectionEditorContext, newType: string) => {
    // mayChangeClass, not isReadOnly: guarding on the narrower flag would accept the
    // dropdown's change event and silently discard it, which is how the original
    // class-correction bug behaved.
    if (!mayChangeClass || !ctx.data) return;
    const docClass = { ...((ctx.data.document_class as Record<string, unknown>) ?? {}), type: newType };
    ctx.setData({ ...ctx.data, document_class: docClass });
  };

  /**
   * Every section's grouping, in the board's 1-based page-id space.
   *
   * Comes from the queue/documents payload rather than a fetch: the editor only ever
   * loads the section being viewed, and the board needs all of them at once. A section
   * whose file could not be read arrives without `pageIndices` and is shown as empty,
   * which the board's validation then blocks — correct, because saving it would drop
   * whatever pages it held.
   */
  const groupingForBoard = (selectedSectionId: string): GroupedSection[] =>
    sections.map((section) => ({
      sectionId: section.sectionId,
      documentClass:
        // The open section's class may have unsaved edits, so prefer local state for
        // it; the rest come from the payload.
        section.sectionId === selectedSectionId ? (documentClassType ?? null) : (section.documentClass ?? null),
      pageIds: pageIndicesToIds(section.pageIndices ?? []),
    }));

  /** Pages for the board, in the same 1-based space. */
  const boardPages = useMemo(() => pages.map((page) => ({ id: Number(page.Id), imageUri: page.ImageUri })), [pages]);

  /**
   * Whether there is a grouping to edit at all.
   *
   * Both are what `PageGroupingEditor` renders from, so this is true exactly when the board
   * would have something to show — rather than keying on the open section's fetched
   * `result.json`, which says nothing about the other sections.
   */
  const canRegroup = boardPages.length > 0 && sections.length > 0;

  const handleSaveGrouping = async (next: GroupedSection[]) => {
    if (!testSetId) return;
    setIsSavingGrouping(true);
    setError(null);
    try {
      const payload = next.map((section) => ({
        sectionId: section.sectionId,
        documentClass: section.documentClass ?? undefined,
        // Back to the 0-based space the baseline stores. The only arithmetic in the
        // feature, and the one thing that would corrupt data silently — hence the
        // round-trip tests on these two helpers.
        pageIndices: pageIdsToIndices(section.pageIds),
      }));

      const response = await client.graphql({
        query: updateTestSetDocumentSections,
        variables: { input: { testSetId, objectKey, sections: payload } },
      });
      const written = response.data?.updateTestSetDocumentSections?.sections ?? [];

      // Which sections actually changed, compared BEFORE the reload replaces the refs.
      const before = new Map(sections.map((sec) => [sec.sectionId, JSON.stringify(sec.pageIndices ?? [])]));
      const changed = written
        .filter((sec) => sec && before.get(sec.sectionId) !== JSON.stringify(sec.pageIndices ?? []))
        .map((sec) => (sec as { sectionId: string }).sectionId);

      setRegroupedSectionIds(changed);
      setIsRegrouping(false);
      if (onRegroupOpenChange) onRegroupOpenChange(false);
      setReloadToken((token) => token + 1);
      // The caller refreshes its queue: section ids and classes have moved, so its
      // cached refs are stale.
      if (onReextracted) onReextracted();
    } catch (err) {
      logger.error('Re-grouping failed:', err);
      setError(`Could not save the page grouping: ${getErrorMessage(err)}`);
    } finally {
      setIsSavingGrouping(false);
    }
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

  /**
   * Persist one section. Returns what was stored, which becomes the editor's
   * unchanged state.
   */
  const handleSave = async (sectionId: string, data: Record<string, unknown>): Promise<Record<string, unknown>> => {
    const dataToSave: Record<string, unknown> = { ...data };
    const fullPath = baselineKeyFor(sectionId) ?? '';

    // No client-side _editHistory entry on this path: the review API writes one
    // server-side with token-derived identity and a field-level diff, so appending
    // here would double-record every review with the weaker entry.
    if (onSave) {
      await onSave(sectionId, dataToSave);
      logger.info('Saved ground truth via caller-supplied handler for', fullPath);
      return dataToSave;
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
    // And the label source, for the same reason: on this path there is no review API
    // to tag it. Without this a reviewer could correct every document in an
    // authored-ground-truth set and the queue would still report "0 of 73 reviewed",
    // because the server derives `reviewed` from labelSource == 'reviewed-human' —
    // while the toast said the document had been marked reviewed.
    //
    // Only on this branch. The onSave branch routes through completeSectionReview,
    // which tags it server-side with token-derived identity; doing both would
    // double-record. And this is a claim about *this* reviewer having checked the
    // document, not about who authored the label originally.
    dataToSave.labelSource = LABEL_SOURCE_REVIEWED_HUMAN;
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
    logger.info('Saved ground truth to', fullPath);
    return dataToSave;
  };

  if (sections.length === 0) {
    return <Alert type="warning">No ground truth (baseline) sections found for {objectKey}.</Alert>;
  }

  const headerExtras = (ctx: SectionEditorContext): React.ReactNode => (
    <>
      {/* Provenance is shown where the label is edited, so a machine draft cannot
          be mistaken on screen for confirmed work. */}
      {ctx.data && (
        <SpaceBetween direction="horizontal" size="xs">
          {/* data is only ever set from a baseline that loaded and parsed, so a missing
              labelSource here means uploaded ground truth — not the absence of labels. */}
          {renderLoadedLabelSource(ctx.data.labelSource as string | undefined)}
          {/* Always shown, including the fallback cases. Hiding it whenever the
              version resolved to 'default' is exactly what let #662 go unnoticed:
              the classes on offer were the built-in preset's and nothing said so. */}
          <Badge color={classConfig.source === 'baseline' ? 'grey' : 'blue'}>Classes from: {describeClassConfigSource(classConfig)}</Badge>
          {editHistoryCount > 0 && (
            <Box fontSize="body-s" color="text-body-secondary">
              {editHistoryCount} edit{editHistoryCount === 1 ? '' : 's'} — see Edit history
            </Box>
          )}
        </SpaceBetween>
      )}

      {/* Named sections rather than a count: after a re-group the reviewer needs to
          know WHICH sections to look at. Not acted on automatically — re-extracting
          would regenerate exactly the field values this feature exists to preserve. */}
      {regroupedSectionIds.length > 0 && (
        <Alert
          type="warning"
          header={`Pages moved in section${regroupedSectionIds.length > 1 ? 's' : ''} ${regroupedSectionIds.join(', ')}`}
          dismissible
          onDismiss={() => setRegroupedSectionIds([])}
        >
          The grouping is saved and your field values are untouched — but those sections were extracted from a different set of pages, so
          their values may no longer match. Check them, and use <b>Change class &amp; re-extract</b> on a section if you would rather the
          model redo it.
        </Alert>
      )}

      {(externalRegroup ? regroupOpen && canRegroup : isRegrouping) && (
        <PageGroupingEditor
          presentation={externalRegroup ? 'modal' : 'inline'}
          pages={boardPages}
          sections={groupingForBoard(ctx.selectedSectionId)}
          classOptions={classOptions}
          canChangeClass={mayChangeClass}
          consequence={
            <>
              Moving pages rewrites this document&apos;s <b>section grouping</b> — the ground truth for how the packet splits. Your
              extracted field values, their <b>Reviewed</b> provenance and the edit history are all kept. Sections are renumbered so their
              ids follow page order.
            </>
          }
          saveLabel="Save page grouping"
          isSaving={isSavingGrouping}
          onSave={handleSaveGrouping}
          onCancel={() => (externalRegroup ? onRegroupOpenChange?.(false) : setIsRegrouping(false))}
        />
      )}
      {error && <Alert type="error">{error}</Alert>}
    </>
  );

  /* Gated on what the board itself consumes, not on the open section's fetched baseline:
     the button should exist exactly when the board it opens would work. Deliberately NOT
     gated on `sections.length > 1` — splitting a single section into two is a legitimate
     fix, and pairing this with the pills' own condition would have removed the only
     route to it. */
  const structureSlot = (): React.ReactNode =>
    !externalRegroup &&
    canRegroup && (
      <Button iconName="edit" disabled={!mayChangeClass || !testSetId} onClick={() => setIsRegrouping(true)}>
        Edit Page Grouping
      </Button>
    );

  /* Rendered whenever a baseline is loaded, including when it carries no class. The gate
     was `documentClassType !== undefined`, which showed the control only if a class was
     ALREADY set — backwards for the one case that needs it. */
  const classSlot = (ctx: SectionEditorContext): React.ReactNode => (
    <>
      {ctx.data && (
        <FormField
          label="Class label"
          description={
            classOptions.length > 0
              ? 'What this section is classified as, from this configuration profile. Distinct from the extraction labels below.'
              : classListUnavailable
                ? CLASS_LIST_UNAVAILABLE_DESCRIPTION
                : 'What this section is classified as. Distinct from the extraction labels below.'
          }
          constraintText={
            isReextracting
              ? 'Locked while the re-extraction runs.'
              : classListUnavailable
                ? CLASS_LIST_UNAVAILABLE_CONSTRAINT
                : !mayChangeClass
                  ? 'You do not have permission to change this class.'
                  : undefined
          }
        >
          <SpaceBetween size="xs">
            {/* Constrained to the config's classes: a class with no schema cannot be
                extracted against, so the correction could never take effect. Free text
                only when no config resolves. */}
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
                onChange={({ detail }) => updateDocumentClass(ctx, detail.selectedOption.value ?? '')}
                options={classOptionsWithCurrent}
                disabled={!mayChangeClass || isReextracting}
                placeholder="Choose a document class"
              />
            ) : (
              <Input
                value={documentClassType ?? ''}
                onChange={({ detail }) => updateDocumentClass(ctx, detail.value)}
                disabled={!mayChangeClass}
              />
            )}
            {/* Correcting the class is only half the fix: the fields were extracted
                against the previous schema and only a re-extract replaces them. */}
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
                These fields were extracted as <b>{savedClassType}</b>. Re-extract to replace them with ones the <b>{documentClassType}</b>{' '}
                schema produces. This re-runs the document through the pipeline and usually takes under a minute; you can leave this page
                and come back.
                {ctx.data?.labelSource === 'reviewed-human'
                  ? ' This document was already marked reviewed; re-extracting discards those confirmed values.'
                  : ctx.data?.labelSource === 'draft-machine'
                    ? ' The current draft labels for this document are replaced.'
                    : ' The class is corrected, but this document has authored ground truth, so its field values are kept.'}
              </Alert>
            )}
            {classChanged && !testSetId && (
              <Alert type="warning">
                The class will be saved, but this document has no processing run to re-extract from, so its fields will still reflect the
                previous class.
              </Alert>
            )}
            {reextractNote && <Alert type="success">{reextractNote}</Alert>}
          </SpaceBetween>
        </FormField>
      )}
      {splitPageIndices !== undefined && (
        <FormField
          label="Pages in this section"
          /* Informational now. The control that changes it lives up beside the section
             pills, because it rewrites the document's structure rather than this
             section's field data — so the description says where to find it instead of
             implying it is here. */
          description={
            externalRegroup
              ? 'Which pages of the document this section covers, in reading order. Use Edit Page Grouping at the top to move or reorder pages.'
              : 'Which pages of the document this section covers, in reading order. Use Edit Page Grouping, above, to move or reorder pages.'
          }
        >
          <Input value={splitPageIndices.map((index) => index + 1).join(', ')} disabled />
        </FormField>
      )}
    </>
  );

  /* Three causes, each with a different remedy, which is why they are distinguished
     rather than covered by one sentence. The unclassified case is the most actionable:
     with no class there is no schema, so extraction has nothing to extract against and
     re-running it changes nothing until a class is set. */
  const emptyFieldsSlot = (ctx: SectionEditorContext): React.ReactNode => {
    const inferenceResult =
      (ctx.data?.inference_result as Record<string, unknown> | undefined) ??
      (ctx.data?.inferenceResult as Record<string, unknown> | undefined) ??
      null;
    return (
      <Alert type="info" header="No field values for this section">
        {!documentClassType
          ? 'This section has no document class, so there is no schema to extract against — that is why it has no fields. Set the class above first; re-extracting before that will not produce anything.'
          : inferenceResult
            ? 'This section has an empty result: extraction ran but produced no fields, or the section was added when the pages were re-grouped and nothing has extracted it as a group yet.'
            : 'This section has no inference_result at all.'}{' '}
        {sections.length > 1 ? 'Other sections of this document may still have values — check the section tabs above. ' : ''}
        {documentClassType ? (
          <>
            Use <b>Change class &amp; re-extract</b> to have the model populate it, or the <b>JSON Editor</b> tab to enter values by hand.
          </>
        ) : (
          <>
            If the pages belong with another section instead, <b>Edit Page Grouping</b> can merge them rather than classifying this one
            separately.
          </>
        )}
      </Alert>
    );
  };

  return (
    <SectionEditor
      objectKey={objectKey}
      sections={reviewSections}
      pages={pages}
      isReadOnly={isReadOnly}
      loadSection={loadSection}
      onLoaded={handleLoaded}
      onDataChange={(data) => {
        setLocalData(data);
        if (onDataChange) onDataChange(data);
      }}
      onDirtyChange={onDirtyChange}
      controllerRef={controllerRef}
      actionsPlacement={actionsPlacement}
      showHistoryTab={showHistoryTab}
      onSavingChange={onSavingChange}
      saveOnSectionSwitch={saveOnSectionSwitch}
      onSave={handleSave}
      onSaved={(sectionId) => {
        if (onSaved) onSaved(baselineKeyFor(sectionId) ?? '');
      }}
      focusFieldPath={focusFieldPath}
      buildFieldLink={buildFieldLink}
      reloadToken={reloadToken}
      title={`Ground truth — ${objectKey}`}
      saveButtonText={saveButtonText}
      headerExtras={headerExtras}
      classSlot={classSlot}
      structureSlot={structureSlot}
      hideSectionRow={!externalRegroup && isRegrouping}
      emptyFieldsSlot={emptyFieldsSlot}
      pagesLoading={pagesLoading}
      pagesNotice={
        <>
          {previewUnavailable && (
            <Alert type="info">
              Preview is not available for TIFF documents — use View Source Document to download. Ground truth editing still works.
            </Alert>
          )}
          {pagesError && <Alert type="error">{pagesError}</Alert>}
        </>
      }
      unsavedChangesMessage="You have unsaved ground truth changes. Leave this document and discard them?"
    />
  );
};

export default GroundTruthVisualEditor;
