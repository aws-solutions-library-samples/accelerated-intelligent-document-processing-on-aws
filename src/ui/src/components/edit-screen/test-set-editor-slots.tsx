// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// What the edit page's editor shows only for a Test Studio document: page images rendered
// from the source document (test-set documents have no processed page images), the
// baselines as the sections, the classes of the config the labels were written under,
// the page-class board over the set's 0-based page indices, and the notices after a
// regroup or a class change (with Re-extract). The shared SectionEditor does the field
// editing; this hook supplies its slots and the board.
import React, { useMemo, useState } from 'react';
import { Alert, Button, FormField, Input } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';

import { generateClient } from '../../api/client-shim';
import { getDraftLabelJob, reextractTestSetDocument, updateTestSetDocumentSections } from '../../graphql/generated';
import useConfiguration from '../../hooks/use-configuration';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import useTestDocPages from '../../hooks/use-test-doc-pages';
import useUserRole from '../../hooks/use-user-role';
import { getErrorMessage } from '../../utils/errorUtils';
import { isProfileReadable } from '../../utils/readable-profile';
import { getConfigClassOptions } from '../common/config-class-options';
import type { ConfigClassOption } from '../common/config-class-options';
import { pageIdsToIndices, pageIndicesToIds } from '../common/section-grouping';
import type { GroupedSection } from '../common/section-grouping';
import { resolveClassConfigVersion } from '../test-studio/classConfigVersion';
import type { EditBoard, EditorSlot } from './edit-model';
import { fetchSectionResult } from './section-result';
import type { LoadedSection, ReviewPage, ReviewSectionRef } from './types';

const client = generateClient();
const logger = new ConsoleLogger('TestSetEditorSlots');

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
   * Lets the page-regrouping board show every section's grouping without fetching each
   * `result.json` again — the editor otherwise loads only the section being viewed.
   * Optional because the resolver omits it when a section's file could not be read,
   * which is not the same as the section having no pages.
   */
  pageIndices?: number[] | null;
}

export interface TestSetEditorSlotsOptions {
  bucket: string;
  inputKey: string;
  objectKey: string;
  /** The owning set: re-extraction and the grouping mutation are keyed on it. */
  testSetId: string;
  sections: TestSetDocumentSectionRef[];
  /**
   * Config version whose classes to offer when the baseline carries no stamp of its
   * own — the test set's declared version, if known. Without it the active config is
   * used; see resolveClassConfigVersion.
   */
  configVersion?: string;
  /** Whether the caller's role may change this document's class (see mayChangeClassFor). */
  mayChangeClass: boolean;
  openSectionId: string | null;
  /** The open section's working copy, from the editor. */
  openData: Record<string, unknown> | null;
  /** Section ids, classes or labels moved on the server: the caller re-reads its refs. */
  onChanged: () => void;
  /**
   * Where Edit Page Grouping is: `external` (the default) is the edit page's More Actions;
   * `structure` is a button beside the section selector. Only the pages field's
   * description differs.
   */
  regroupPlacement?: 'external' | 'structure';
}

export interface TestSetEditorSlots {
  pages: ReviewPage[];
  pagesLoading: boolean;
  pagesNotice: React.ReactNode;
  reviewSections: ReviewSectionRef[];
  loadSection: (sectionId: string) => Promise<LoadedSection>;
  /** Bumped to re-read the open section after a regroup or re-extract rewrites it (its key is unchanged). */
  reloadToken: number;
  onLoaded: () => void;
  classOptions: ConfigClassOption[];
  classConfig: ReturnType<typeof resolveClassConfigVersion>;
  headerExtras: EditorSlot;
  classSlot: EditorSlot;
  emptyFieldsSlot: EditorSlot;
  board: EditBoard;
  /** There is a grouping to edit: the board has pages and sections to show. */
  canRegroup: boolean;
}

export const useTestSetEditorSlots = ({
  bucket,
  inputKey,
  objectKey,
  testSetId,
  sections,
  configVersion,
  mayChangeClass,
  openData,
  onChanged,
  regroupPlacement = 'external',
}: TestSetEditorSlotsOptions): TestSetEditorSlots => {
  const { pages, isLoading: pagesLoading, error: pagesError, previewUnavailable } = useTestDocPages(bucket, inputKey);
  const [error, setError] = useState<string | null>(null);
  /**
   * Sections whose pages changed in the last re-grouping.
   *
   * Kept so the warning names them: their field values were extracted from a different
   * set of pages and may no longer match. Deliberately not acted on — re-extracting
   * automatically is the annotation loss this whole feature exists to avoid.
   */
  const [regroupedSectionIds, setRegroupedSectionIds] = useState<string[]>([]);
  const [isReextracting, setIsReextracting] = useState(false);
  const [reextractNote, setReextractNote] = useState<string | null>(null);
  // Sections whose class Edit Page Grouping just changed, by section id, with the class
  // their fields were extracted under: their fields no longer match their class until
  // they are re-extracted.
  const [reclassified, setReclassified] = useState<Record<string, string>>({});
  // Forces a baseline re-read after a re-extract rewrites it: the key is unchanged.
  const [reloadToken, setReloadToken] = useState(0);

  const reviewSections = useMemo<ReviewSectionRef[]>(
    () =>
      sections.map((section) => ({
        sectionId: section.sectionId,
        documentClass: section.documentClass,
        resultUri: `s3://${bucket}/${section.baselineKey}`,
        // The baseline's own split_document.page_indices decides the image pane; an
        // empty list keeps the whole-document fallback.
        pageIds: [],
      })),
    [sections, bucket],
  );

  // A section's baseline result.json, read straight from S3 via a server-issued presigned URL.
  const loadSection = (sectionId: string): Promise<LoadedSection> =>
    fetchSectionResult(`s3://${bucket}/${sections.find((s) => s.sectionId === sectionId)?.baselineKey}`);

  const documentClassType = (openData?.document_class as Record<string, unknown> | undefined)?.type as string | undefined;
  const splitPageIndices = (openData?.split_document as Record<string, unknown> | undefined)?.page_indices as number[] | undefined;

  // Which profile's classes to offer, and why: the profile stamped on the baseline over
  // the deployment's active one, which may have moved on since the labels were written.
  // The fallback order lives in resolveClassConfigVersion (#662).
  const stampedConfigVersion = (openData?.metadata as Record<string, unknown> | undefined)?.config_version as string | undefined;
  const { versions } = useConfigurationVersions();
  const { allowedConfigVersions, loading: roleLoading } = useUserRole();
  const activeConfigVersion = useMemo(() => versions.find((v) => v.isActive)?.versionName, [versions]);
  const classConfig = useMemo(
    () => resolveClassConfigVersion(stampedConfigVersion, configVersion, activeConfigVersion),
    [stampedConfigVersion, configVersion, activeConfigVersion],
  );
  const classConfigVersion = classConfig.version;
  const classConfigReadable = isProfileReadable(classConfigVersion, versions, { allowedConfigVersions, loading: roleLoading });
  const { mergedConfig } = useConfiguration(classConfigReadable ? classConfigVersion : null);
  const classOptions = useMemo(() => getConfigClassOptions(mergedConfig), [mergedConfig]);

  /** Pages for the board, in the 1-based page-id space. */
  const boardPages = useMemo(() => pages.map((page) => ({ id: Number(page.Id), imageUri: page.ImageUri })), [pages]);
  const canRegroup = boardPages.length > 0 && sections.length > 0;

  /**
   * Every section's grouping, in the board's 1-based page-id space, from the queue's
   * payload rather than a fetch (the editor loads only the open section). A section whose
   * file could not be read arrives without `pageIndices` and is shown empty, which the
   * board's validation then blocks: saving it would drop whatever pages it held.
   */
  const boardSections = (openSectionId: string | null, open: Record<string, unknown> | null): GroupedSection[] => {
    const openClass = (open?.document_class as Record<string, unknown> | undefined)?.type as string | undefined;
    return sections.map((section) => ({
      sectionId: section.sectionId,
      // The open section's class may have unsaved edits, so it comes from the editor.
      documentClass: section.sectionId === openSectionId ? (openClass ?? null) : (section.documentClass ?? null),
      pageIds: pageIndicesToIds(section.pageIndices ?? []),
    }));
  };

  const saveGrouping = async (next: GroupedSection[]): Promise<string | null> => {
    const payload = next.map((section) => ({
      sectionId: section.sectionId,
      documentClass: section.documentClass ?? undefined,
      // Back to the 0-based space the baseline stores: the one piece of arithmetic that
      // would corrupt data silently, hence the round-trip tests.
      pageIndices: pageIdsToIndices(section.pageIds),
    }));
    let written;
    try {
      const response = await client.graphql({
        query: updateTestSetDocumentSections,
        variables: { input: { testSetId, objectKey, sections: payload } },
      });
      written = response.data?.updateTestSetDocumentSections?.sections ?? [];
    } catch (err) {
      logger.error('Re-grouping failed:', err);
      throw new Error(`Could not save the page grouping: ${getErrorMessage(err)}`);
    }

    // Which sections actually changed, compared before the reload replaces the refs.
    const before = new Map(sections.map((sec) => [sec.sectionId, JSON.stringify(sec.pageIndices ?? [])]));
    const changed = written
      .filter((sec) => sec && before.get(sec.sectionId) !== JSON.stringify(sec.pageIndices ?? []))
      .map((sec) => (sec as { sectionId: string }).sectionId);
    setRegroupedSectionIds(changed);
    const classByFirstPage = new Map(sections.map((sec) => [sec.pageIndices?.[0], sec.documentClass ?? '']));
    const nextClassByFirstPage = new Map(payload.map((sec) => [sec.pageIndices[0], sec.documentClass ?? '']));
    const moved: Record<string, string> = {};
    written.forEach((sec) => {
      if (!sec) return;
      const first = (sec.pageIndices ?? [])[0];
      const was = classByFirstPage.get(first);
      // What was saved, as the server reports it; what the board sent if it does not say.
      const after = sec.documentClass ?? nextClassByFirstPage.get(first);
      if (was && after && was !== after) moved[sec.sectionId] = was;
    });
    setReclassified(moved);
    setReloadToken((token) => token + 1);
    // Section ids and classes have moved, so the caller's refs are stale.
    onChanged();
    return null;
  };

  /**
   * Re-run extraction under the corrected class, then reload the new labels. Blocks
   * until the labels are replaced rather than returning once the job is queued, because
   * the fields on screen are wrong until then; labels are harvested on read, so this poll
   * drives the write-back.
   */
  const handleReextract = async () => {
    if (!documentClassType || !testSetId) return;
    setIsReextracting(true);
    setError(null);
    setReextractNote(null);
    try {
      const started = await client.graphql({
        query: reextractTestSetDocument,
        variables: { input: { testSetId, objectKey, documentClass: documentClassType, configVersion: classConfigVersion } },
      });
      const job = started.data?.reextractTestSetDocument;
      if (!job?.jobId) throw new Error('No job returned');

      const deadline = Date.now() + REEXTRACT_TIMEOUT_MS;
      let status = job.status;
      while (status === 'RUNNING' && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, REEXTRACT_POLL_MS));
        const polled = await client.graphql({ query: getDraftLabelJob, variables: { testSetId, jobId: job.jobId } });
        status = polled.data?.getDraftLabelJob?.status ?? status;
        if (status === 'FAILED') throw new Error(polled.data?.getDraftLabelJob?.error || 'Re-extraction failed');
      }
      if (status === 'RUNNING') {
        // Not an error: the job is still running and the harvest is idempotent.
        setReextractNote(
          'Re-extraction is taking longer than expected. It is still running — reopen this document shortly to see the new fields.',
        );
        return;
      }
      setReloadToken((token) => token + 1);
      setReclassified({});
      setReextractNote(`Re-extracted as ${documentClassType}.`);
      onChanged();
    } catch (err) {
      logger.error('Re-extraction failed:', err);
      setError(`Could not re-extract this document: ${getErrorMessage(err)}`);
    } finally {
      setIsReextracting(false);
    }
  };

  const headerExtras: EditorSlot = () => (
    <>
      {/* Named sections rather than a count: after a re-group the reviewer needs to know
          WHICH sections to look at. Not acted on automatically — re-extracting would
          regenerate exactly the field values this feature exists to preserve. */}
      {regroupedSectionIds.length > 0 && (
        <Alert
          type="warning"
          header={`Pages moved in section${regroupedSectionIds.length > 1 ? 's' : ''} ${regroupedSectionIds.join(', ')}`}
          dismissible
          onDismiss={() => setRegroupedSectionIds([])}
        >
          The grouping is saved and your field values are untouched — but those sections were extracted from a different set of pages, so
          their values may no longer match. Check them, and change a section&apos;s class and use <b>Re-extract</b> if you would rather the
          model redo it.
        </Alert>
      )}
      {error && <Alert type="error">{error}</Alert>}
    </>
  );

  /* Rendered whenever a baseline is loaded, including when it carries no class. */
  const classSlot: EditorSlot = (ctx) => (
    <>
      {/* A section's class is changed in Edit Page Grouping. Its fields were extracted under
          the previous class, and only a re-extract replaces them. */}
      {ctx.data && reclassified[ctx.selectedSectionId] && testSetId && (
        <Alert
          type="info"
          header="Fields still reflect the previous class"
          action={
            <Button onClick={handleReextract} loading={isReextracting} disabled={!mayChangeClass}>
              {isReextracting ? 'Re-extracting…' : 'Re-extract'}
            </Button>
          }
        >
          These fields were extracted as <b>{reclassified[ctx.selectedSectionId]}</b>. Re-extract to replace them with ones the{' '}
          <b>{documentClassType}</b> schema produces. This re-runs the document through the pipeline and usually takes under a minute; you
          can leave this page and come back.
          {ctx.data?.labelSource === 'reviewed-human'
            ? ' This document was already marked reviewed; re-extracting discards those confirmed values.'
            : ctx.data?.labelSource === 'draft-machine'
              ? ' The current draft labels for this document are replaced.'
              : ' The class is corrected, but this document has authored ground truth, so its field values are kept.'}
        </Alert>
      )}
      {reextractNote && <Alert type="success">{reextractNote}</Alert>}
      {splitPageIndices !== undefined && (
        <FormField
          label="Pages in this section"
          description={
            regroupPlacement === 'external'
              ? "Which pages of the document this section covers, in reading order. Use Edit Page Grouping at the top to move or reorder pages, or change a section's class."
              : "Which pages of the document this section covers, in reading order. Use Edit Page Grouping, above, to move or reorder pages, or change a section's class."
          }
        >
          <Input value={splitPageIndices.map((index) => index + 1).join(', ')} disabled />
        </FormField>
      )}
    </>
  );

  /* Three causes, each with a different remedy. With no class there is no schema, so
     extraction has nothing to extract against and re-running it changes nothing until a
     class is set. */
  const emptyFieldsSlot: EditorSlot = (ctx) => {
    const inferenceResult =
      (ctx.data?.inference_result as Record<string, unknown> | undefined) ??
      (ctx.data?.inferenceResult as Record<string, unknown> | undefined) ??
      null;
    return (
      <Alert type="info" header="No field values for this section">
        {!documentClassType
          ? 'This section has no document class, so there is no schema to extract against — that is why it has no fields. Set its class in Edit Page Grouping first; re-extracting before that will not produce anything.'
          : inferenceResult
            ? 'This section has an empty result: extraction ran but produced no fields, or the section was added when the pages were re-grouped and nothing has extracted it as a group yet.'
            : 'This section has no inference_result at all.'}{' '}
        {sections.length > 1 ? 'Other sections of this document may still have values — choose one from the Section list above. ' : ''}
        {documentClassType ? (
          <>
            To have the model populate it, change its class in <b>Edit Page Grouping</b> and choose <b>Re-extract</b>, or use the{' '}
            <b>JSON Editor</b> tab to enter values by hand.
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

  const pagesNotice = (
    <>
      {previewUnavailable && (
        <Alert type="info">
          Preview is not available for TIFF documents — use View Source Document to download. Ground truth editing still works.
        </Alert>
      )}
      {pagesError && <Alert type="error">{pagesError}</Alert>}
    </>
  );

  const board: EditBoard = {
    pages: { kind: 'rendered', pages: boardPages },
    sections: boardSections,
    classOptions,
    canChangeClass: mayChangeClass,
    consequence: (
      <>
        Moving pages rewrites this document&apos;s <b>section grouping</b> — the ground truth for how the packet splits. Your extracted
        field values, their <b>Reviewed</b> provenance and the edit history are all kept. Sections are renumbered so their ids follow page
        order.
      </>
    ),
    save: saveGrouping,
  };

  return {
    pages,
    pagesLoading,
    pagesNotice,
    reviewSections,
    loadSection,
    reloadToken,
    onLoaded: () => setReextractNote(null),
    classOptions,
    classConfig,
    headerExtras,
    classSlot,
    emptyFieldsSlot,
    board,
    canRegroup,
  };
};
