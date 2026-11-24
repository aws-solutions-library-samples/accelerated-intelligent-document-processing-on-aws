// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// What the edit page's editor shows only for a Test Studio document: page images rendered
// from the source document (test-set documents have no processed page images), the
// baselines as the sections, the classes of the config the labels were written under,
// the page-class board over the set's 0-based page indices, and the notices after a
// regroup or a class change (with Re-extract: the whole document when it has one section,
// the open section only when it has several). The shared SectionEditor does the field
// editing; this hook supplies its slots and the board.
import React, { useEffect, useMemo, useState } from 'react';
import { Alert, Button, FormField, Input } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { defineMessages, FormattedMessage, useIntl, type MessageTag, type NoMessageValues } from 'react-intl';

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

type Bold = { readonly b: MessageTag };
type Classes = { readonly b: MessageTag; readonly previousClass: string; readonly documentClass: string };
type Failure = { readonly error: string };

/** Renders a `<b>...</b>` element value: a control's or a class's name inside a sentence. */
const bold = (chunks: React.ReactNode[]): React.JSX.Element => <b>{chunks}</b>;

const messages = defineMessages<{
  reextractTimeout: NoMessageValues;
  classCorrectedSection: { readonly documentClass: string; readonly sectionId: string };
  classCorrectedDocument: { readonly documentClass: string };
  reextractedSection: { readonly documentClass: string; readonly sectionId: string };
  reextractedDocument: { readonly documentClass: string };
  reextractFailedSection: Failure;
  reextractFailedDocument: Failure;
  saveGroupingFailed: Failure;
  pagesMovedHeader: { readonly count: number; readonly ids: string };
  pagesMovedBody: Bold;
  pagesMovedBodyPerSection: Bold;
  previousClassHeader: NoMessageValues;
  regroupPending: NoMessageValues;
  reextractSectionAria: { readonly sectionId: string };
  reextracting: NoMessageValues;
  reextract: NoMessageValues;
  previousClassSection: Classes & { readonly others: number };
  sectionTailReviewed: NoMessageValues;
  sectionTailDraft: NoMessageValues;
  sectionTailAuthored: NoMessageValues;
  mergeWarning: { readonly count: number };
  previousClassDocument: Classes;
  documentTailReviewed: NoMessageValues;
  documentTailDraft: NoMessageValues;
  documentTailAuthored: NoMessageValues;
  pagesInSection: NoMessageValues;
  pagesInSectionHelp: NoMessageValues;
  emptyHeader: NoMessageValues;
  emptyNoClass: NoMessageValues;
  emptyEmptyResult: NoMessageValues;
  emptyNoInference: NoMessageValues;
  emptyOtherSections: NoMessageValues;
  emptyHintPerSection: Bold;
  emptyHintSingle: Bold;
  emptyHintMerge: Bold;
  tiffPreview: NoMessageValues;
  regroupConsequence: Bold;
}>({
  reextractTimeout: {
    id: 'editScreen.testSetSlots.reextractTimeout',
    defaultMessage:
      'Re-extraction is taking longer than expected. It is still running — reopen this document shortly to see the new fields.',
    description:
      "Notice in the section editor of a test-set item's edit page when a re-extract started with Re-extract has not finished after five minutes and is still running.",
  },
  classCorrectedSection: {
    id: 'editScreen.testSetSlots.classCorrectedSection',
    defaultMessage:
      'Class corrected to {documentClass} on section {sectionId}. This section has authored ground truth, so its field values were kept rather than re-extracted; if they need to change, edit them here or in the JSON Editor tab.',
    description:
      "Notice in the section editor of a test-set item's edit page after Re-extract on one section of a document with several sections, when the section's labels are authored ground truth: its class changed and its field values stayed. {documentClass} is the section's document class and {sectionId} its number, both shown as is. JSON Editor is a tab of the section editor.",
  },
  classCorrectedDocument: {
    id: 'editScreen.testSetSlots.classCorrectedDocument',
    defaultMessage:
      'Class corrected to {documentClass}. This document has authored ground truth, so its field values were kept rather than re-extracted; if they need to change, edit them here or in the JSON Editor tab.',
    description:
      "Notice in the section editor of a test-set item's edit page after Re-extract on a document with one section, when its labels are authored ground truth: its class changed and its field values stayed. {documentClass} is the document class, shown as is. JSON Editor is a tab of the section editor.",
  },
  reextractedSection: {
    id: 'editScreen.testSetSlots.reextractedSection',
    defaultMessage: 'Re-extracted section {sectionId} as {documentClass}. The other sections were not changed.',
    description:
      "Notice in the section editor of a test-set item's edit page after Re-extract replaced the fields of one section of a document with several sections. {sectionId} is the section's number; {documentClass} is the document class it was extracted as, shown as is.",
  },
  reextractedDocument: {
    id: 'editScreen.testSetSlots.reextractedDocument',
    defaultMessage: 'Re-extracted as {documentClass}.',
    description:
      "Notice in the section editor of a test-set item's edit page after Re-extract replaced the fields of a document with one section. {documentClass} is the document class it was extracted as, shown as is.",
  },
  reextractFailedSection: {
    id: 'editScreen.testSetSlots.reextractFailedSection',
    defaultMessage: 'Could not re-extract this section: {error}',
    description:
      "Error in the section editor of a test-set item's edit page when Re-extract failed on one section of a document with several sections. {error} is why, as the request reported it, shown as is.",
  },
  reextractFailedDocument: {
    id: 'editScreen.testSetSlots.reextractFailedDocument',
    defaultMessage: 'Could not re-extract this document: {error}',
    description:
      "Error in the section editor of a test-set item's edit page when Re-extract failed on a document with one section. {error} is why, as the request reported it, shown as is.",
  },
  saveGroupingFailed: {
    id: 'editScreen.testSetSlots.saveGroupingFailed',
    defaultMessage: 'Could not save the page grouping: {error}',
    description:
      "Error in the Edit Page Grouping dialog of a test-set item's edit page when the page grouping could not be saved. {error} is why, as the request reported it, shown as is.",
  },
  pagesMovedHeader: {
    id: 'editScreen.testSetSlots.pagesMovedHeader',
    defaultMessage: '{count, plural, one {Pages moved in section {ids}} other {Pages moved in sections {ids}}}',
    description:
      "Heading of the warning in the section editor of a test-set item's edit page after Edit Page Grouping saved a grouping that changed the pages of some sections. {count} is how many sections; {ids} lists their numbers, separated by commas.",
  },
  pagesMovedBody: {
    id: 'editScreen.testSetSlots.pagesMovedBody',
    defaultMessage:
      "The grouping is saved and your field values are untouched — but those sections were extracted from a different set of pages, so their values may no longer match. Check them, and change the section's class and use <b>Re-extract</b> if you would rather the model redo it.",
    description:
      "Text of the warning, in the section editor of a test-set item's edit page, that a saved grouping changed the pages of some sections, on a document with one section. <b>…</b> marks bold text; Re-extract is a button of the section editor.",
  },
  pagesMovedBodyPerSection: {
    id: 'editScreen.testSetSlots.pagesMovedBodyPerSection',
    defaultMessage:
      "The grouping is saved and your field values are untouched — but those sections were extracted from a different set of pages, so their values may no longer match. Check them, and change the section's class and use <b>Re-extract</b> if you would rather the model redo it (on this document it re-extracts that one section).",
    description:
      "Text of the warning, in the section editor of a test-set item's edit page, that a saved grouping changed the pages of some sections, on a document with several sections, where Re-extract works on one section. <b>…</b> marks bold text; Re-extract is a button of the section editor.",
  },
  previousClassHeader: {
    id: 'editScreen.testSetSlots.previousClassHeader',
    defaultMessage: 'Fields still reflect the previous class',
    description:
      "Heading of the notice in the section editor of a test-set item's edit page when Edit Page Grouping changed the open section's class: its fields were extracted under the class it had before.",
  },
  regroupPending: {
    id: 'editScreen.testSetSlots.regroupPending',
    defaultMessage: 'Loading the saved grouping…',
    description:
      "Why Re-extract, in the section editor of a test-set item's edit page, is unavailable: a page grouping was just saved and the page is still re-reading the saved sections.",
  },
  reextractSectionAria: {
    id: 'editScreen.testSetSlots.reextractSectionAria',
    defaultMessage: 'Re-extract section {sectionId}',
    description:
      "Accessible name of Re-extract, in the section editor of a test-set item's edit page, on a document with several sections. {sectionId} is the number of the section it extracts again.",
  },
  reextracting: {
    id: 'editScreen.testSetSlots.reextracting',
    defaultMessage: 'Re-extracting…',
    description: "Text of Re-extract, in the section editor of a test-set item's edit page, while the re-extract runs.",
  },
  reextract: {
    id: 'editScreen.testSetSlots.reextract',
    defaultMessage: 'Re-extract',
    description:
      "Button of the notice that the open section's fields still reflect its previous class, in the section editor of a test-set item's edit page: extracts the fields again under the section's class (the whole document's when it has one section).",
  },
  previousClassSection: {
    id: 'editScreen.testSetSlots.previousClassSection',
    defaultMessage:
      "These fields were extracted as <b>{previousClass}</b>. Re-extract replaces this section's fields with ones the <b>{documentClass}</b> schema produces; the other {others, plural, one {# section keeps its} other {# sections keep their}} pages, class and fields.",
    description:
      "Text of the notice that the open section's fields still reflect its previous class, on a document with several sections, in the section editor of a test-set item's edit page. <b>…</b> marks bold text. {previousClass} is the class the fields were extracted under and {documentClass} the section's class now, both shown as is; {others} is how many other sections the document has. Re-extract is the notice's button.",
  },
  sectionTailReviewed: {
    id: 'editScreen.testSetSlots.sectionTailReviewed',
    defaultMessage: 'This section was already marked reviewed; re-extracting discards its confirmed values.',
    description:
      "Sentence of the notice that the open section's fields still reflect its previous class, on a document with several sections, in the section editor of a test-set item's edit page: the section's labels are marked reviewed.",
  },
  sectionTailDraft: {
    id: 'editScreen.testSetSlots.sectionTailDraft',
    defaultMessage: "This section's draft labels are replaced.",
    description:
      "Sentence of the notice that the open section's fields still reflect its previous class, on a document with several sections, in the section editor of a test-set item's edit page: the section's labels are draft labels.",
  },
  sectionTailAuthored: {
    id: 'editScreen.testSetSlots.sectionTailAuthored',
    defaultMessage: 'The class is corrected, but this section has authored ground truth, so its field values are kept.',
    description:
      "Sentence of the notice that the open section's fields still reflect its previous class, on a document with several sections, in the section editor of a test-set item's edit page: the section's labels are authored ground truth, which Re-extract does not replace.",
  },
  mergeWarning: {
    id: 'editScreen.testSetSlots.mergeWarning',
    defaultMessage:
      'Changing the class for the whole document would merge all {count} sections into one and discard their ground truth, so Re-extract here works on this section only.',
    description:
      "Last sentence of the notice that the open section's fields still reflect its previous class, on a document with several sections, in the section editor of a test-set item's edit page. {count} is how many sections the document has, two or more. Re-extract is the notice's button.",
  },
  previousClassDocument: {
    id: 'editScreen.testSetSlots.previousClassDocument',
    defaultMessage:
      'These fields were extracted as <b>{previousClass}</b>. Re-extract to replace them with ones the <b>{documentClass}</b> schema produces. This re-runs the document through the pipeline and usually takes under a minute; you can leave this page and come back.',
    description:
      "Text of the notice that the fields still reflect the previous class, on a document with one section, in the section editor of a test-set item's edit page. <b>…</b> marks bold text. {previousClass} is the class the fields were extracted under and {documentClass} the document's class now, both shown as is. Re-extract is the notice's button.",
  },
  documentTailReviewed: {
    id: 'editScreen.testSetSlots.documentTailReviewed',
    defaultMessage: 'This document was already marked reviewed; re-extracting discards those confirmed values.',
    description:
      "Last sentence of the notice that the fields still reflect the previous class, on a document with one section, in the section editor of a test-set item's edit page: the document's labels are marked reviewed.",
  },
  documentTailDraft: {
    id: 'editScreen.testSetSlots.documentTailDraft',
    defaultMessage: 'The current draft labels for this document are replaced.',
    description:
      "Last sentence of the notice that the fields still reflect the previous class, on a document with one section, in the section editor of a test-set item's edit page: the document's labels are draft labels.",
  },
  documentTailAuthored: {
    id: 'editScreen.testSetSlots.documentTailAuthored',
    defaultMessage: 'The class is corrected, but this document has authored ground truth, so its field values are kept.',
    description:
      "Last sentence of the notice that the fields still reflect the previous class, on a document with one section, in the section editor of a test-set item's edit page: the document's labels are authored ground truth, which Re-extract does not replace.",
  },
  pagesInSection: {
    id: 'editScreen.testSetSlots.pagesInSection',
    defaultMessage: 'Pages in this section',
    description:
      "Label of the field, in the section editor of a test-set item's edit page, that lists the page numbers the open section covers.",
  },
  pagesInSectionHelp: {
    id: 'editScreen.testSetSlots.pagesInSectionHelp',
    defaultMessage:
      "Which pages of the document this section covers, in reading order. Use Edit Page Grouping at the top to move or reorder pages, or change a section's class.",
    description:
      "Help text of the field that lists the open section's page numbers, in the section editor of a test-set item's edit page. Edit Page Grouping is a button in the section editor's header.",
  },
  emptyHeader: {
    id: 'editScreen.testSetSlots.emptyHeader',
    defaultMessage: 'No field values for this section',
    description: "Heading of the notice in the section editor of a test-set item's edit page when the open section has no field values.",
  },
  emptyNoClass: {
    id: 'editScreen.testSetSlots.emptyNoClass',
    defaultMessage:
      'This section has no document class, so there is no schema to extract against — that is why it has no fields. Set its class in Edit Page Grouping first; re-extracting before that will not produce anything.',
    description:
      "Text of the notice that the open section has no field values, in the section editor of a test-set item's edit page, when the section has no document class. Edit Page Grouping is a button in the section editor's header.",
  },
  emptyEmptyResult: {
    id: 'editScreen.testSetSlots.emptyEmptyResult',
    defaultMessage:
      'This section has an empty result: extraction ran but produced no fields, or the section was added when the pages were re-grouped and nothing has extracted it as a group yet.',
    description:
      "Text of the notice that the open section has no field values, in the section editor of a test-set item's edit page, when the section's extraction result is empty.",
  },
  emptyNoInference: {
    id: 'editScreen.testSetSlots.emptyNoInference',
    defaultMessage: 'This section has no inference_result at all.',
    description:
      "Text of the notice that the open section has no field values, in the section editor of a test-set item's edit page, when the section's file has no extraction result. inference_result is the name of that field in the file, shown as is.",
  },
  emptyOtherSections: {
    id: 'editScreen.testSetSlots.emptyOtherSections',
    defaultMessage: 'Other sections of this document may still have values — choose one from the Section list above.',
    description:
      "Sentence of the notice that the open section has no field values, in the section editor of a test-set item's edit page, on a document with several sections. The Section list is the section editor's section selector.",
  },
  emptyHintPerSection: {
    id: 'editScreen.testSetSlots.emptyHintPerSection',
    defaultMessage:
      'To have the model populate it, change its class in <b>Edit Page Grouping</b> and choose <b>Re-extract</b>, which re-extracts this section only and leaves the others as they are, or use the <b>JSON Editor</b> tab to enter values by hand.',
    description:
      "Last sentence of the notice that the open section has no field values, in the section editor of a test-set item's edit page, when the section has a class, on a document with several sections. <b>…</b> marks bold text: Edit Page Grouping is a button in the section editor's header, Re-extract a button of the notice the editor then shows and JSON Editor a tab of the section editor.",
  },
  emptyHintSingle: {
    id: 'editScreen.testSetSlots.emptyHintSingle',
    defaultMessage:
      'To have the model populate it, change its class in <b>Edit Page Grouping</b> and choose <b>Re-extract</b>, or use the <b>JSON Editor</b> tab to enter values by hand.',
    description:
      "Last sentence of the notice that the open section has no field values, in the section editor of a test-set item's edit page, when the section has a class, on a document with one section. <b>…</b> marks bold text: Edit Page Grouping is a button in the section editor's header, Re-extract a button of the notice the editor then shows and JSON Editor a tab of the section editor.",
  },
  emptyHintMerge: {
    id: 'editScreen.testSetSlots.emptyHintMerge',
    defaultMessage:
      'If the pages belong with another section instead, <b>Edit Page Grouping</b> can merge them rather than classifying this one separately.',
    description:
      "Last sentence of the notice that the open section has no field values, in the section editor of a test-set item's edit page, when the section has no document class. <b>…</b> marks bold text; Edit Page Grouping is a button in the section editor's header.",
  },
  tiffPreview: {
    id: 'editScreen.testSetSlots.tiffPreview',
    defaultMessage: 'Preview is not available for TIFF documents — use View Source Document to download. Ground truth editing still works.',
    description:
      "Notice above the page images of a test-set item's edit page when its document is a TIFF file, whose pages the page does not show.",
  },
  regroupConsequence: {
    id: 'editScreen.testSetSlots.regroupConsequence',
    defaultMessage:
      "Moving pages rewrites this document's <b>section grouping</b> — the ground truth for how the packet splits. Your extracted field values, their <b>Reviewed</b> provenance and the edit history are all kept. Sections are renumbered so their ids follow page order.",
    description:
      "What saving a new page grouping does, in Edit Page Grouping opened from a test-set item's edit page. <b>…</b> marks bold text; Reviewed is the label source of labels marked reviewed.",
  },
});

/**
 * What the last re-extract did, as codes with their values: `target` is the section a
 * per-section run re-extracted (null for the whole document), and `documentClass` the class
 * it ran under. Worded when the note shows.
 */
type ReextractNote = { sectionId: string } & (
  | { kind: 'timeout' }
  | { kind: 'authored' | 'reextracted'; target: string | null; documentClass: string }
);

const client = generateClient();
const logger = new ConsoleLogger('TestSetEditorSlots');

const REEXTRACT_POLL_MS = 3000;
/** Where a section's file records the class its fields were extracted under (#1816). */
const EXTRACTED_AS_KEY = 'extractedAsClass';

/** The class to name in the note, or null: none recorded, or the section is that class again. */
const extractedAsNote = (stamp: unknown, documentClass: unknown): string | null =>
  typeof stamp === 'string' && stamp !== '' && stamp !== documentClass ? stamp : null;
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
  /**
   * Section ids, classes or labels moved on the server: the caller re-reads its refs. A
   * returned promise settles once they are re-read.
   */
  onChanged: () => void | Promise<unknown>;
}

export interface TestSetEditorSlots {
  pages: ReviewPage[];
  pagesLoading: boolean;
  pagesNotice: React.ReactNode;
  reviewSections: ReviewSectionRef[];
  loadSection: (sectionId: string) => Promise<LoadedSection>;
  /** Bumped to re-read the open section after a regroup or re-extract rewrites it (its key is unchanged). */
  reloadToken: number;
  /** The open section was read; `data` is what was read, which may carry `extractedAsClass`. */
  onLoaded: (sectionId: string, data?: Record<string, unknown>) => void;
  classOptions: ConfigClassOption[];
  classConfig: ReturnType<typeof resolveClassConfigVersion>;
  /** The configuration the set was labelled with (null until read, or unreadable for this role). */
  mergedConfig: ReturnType<typeof useConfiguration>['mergedConfig'];
  headerExtras: EditorSlot;
  classSlot: EditorSlot;
  emptyFieldsSlot: EditorSlot;
  board: EditBoard;
  /** There is a grouping to edit: the board has pages and sections, and the refs are not behind a save. */
  canRegroup: boolean;
  /** The last saved grouping has not been re-read yet: section ids on the page may be stale. */
  refsPending: boolean;
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
}: TestSetEditorSlotsOptions): TestSetEditorSlots => {
  const intl = useIntl();
  const { pages, isLoading: pagesLoading, error: pagesError, previewUnavailable } = useTestDocPages(bucket, inputKey);
  /** The last re-extract that failed: which section it ran on (null for the whole document) and the reason it gave. */
  const [reextractFailure, setReextractFailure] = useState<{ target: string | null; error: string } | null>(null);
  /**
   * Sections whose pages changed in the last re-grouping.
   *
   * Kept so the warning names them: their field values were extracted from a different
   * set of pages and may no longer match. Deliberately not acted on — re-extracting
   * automatically is the annotation loss this whole feature exists to avoid.
   */
  const [regroupedSectionIds, setRegroupedSectionIds] = useState<string[]>([]);
  const [isReextracting, setIsReextracting] = useState(false);
  /**
   * What the last re-extract did, and the section it did it to. Kept while that section
   * is re-read after the re-extract — the re-read is how its new fields reach the screen,
   * so clearing on any load hid the note before it was ever seen — and cleared once a
   * different section is opened, where it would describe something else.
   */
  const [reextractNote, setReextractNote] = useState<ReextractNote | null>(null);
  // Sections whose class Edit Page Grouping changed, by section id, with the class their
  // fields were extracted under: their fields no longer match their class until they are
  // re-extracted. The server records that class on the section as `extractedAsClass`
  // (#1816): a save returns it for every section, and opening a section reads it from the
  // section's file, which is how the note survives a reload.
  const [reclassified, setReclassified] = useState<Record<string, string>>({});
  // How many sections the last saved grouping wrote, until the caller's refs catch up.
  const [savedSectionCount, setSavedSectionCount] = useState<number | null>(null);
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
  /**
   * Which Re-extract this document gets. A whole-document re-extract forces one class
   * onto every page as one section, so on a document with several sections it would
   * merge them all and discard their ground truth (#1672); the server refuses it there.
   * Such a document re-extracts the open section only (#1699): the run keeps every
   * other section's pages and class, and only that section's label is replaced.
   */
  // Also counts the grouping just saved: the caller's refs lag the save until its reload
  // lands, and a split into several sections must already be treated as several. Once
  // new refs arrive they are the truth and the saved count is dropped. Keyed on the
  // array's identity, which the caller replaces on each reload: a content key would miss
  // a re-read that lands on the same section ids the save started from.
  const sectionCount = Math.max(sections.length, savedSectionCount ?? 0);
  const perSection = sectionCount > 1;
  // A save renumbers sections in page order, so until the refs catch up the open
  // section's id may name a different section on the server: a per-section re-extract
  // waits for them.
  const refsPending = savedSectionCount !== null;
  useEffect(() => setSavedSectionCount(null), [sections]);
  // The board is built from the refs, so while they lag a save it would send ids that now
  // name other sections on the server, and the save would move their fields (#1895).
  const canRegroup = boardPages.length > 0 && sections.length > 0 && !refsPending;

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
      // Worded with this render's intl when the board's Save runs this: the board shows the
      // text of the error it catches.
      throw new Error(intl.formatMessage(messages.saveGroupingFailed, { error: getErrorMessage(err) }));
    }

    // Which sections actually changed, compared before the reload replaces the refs.
    const before = new Map(sections.map((sec) => [sec.sectionId, JSON.stringify(sec.pageIndices ?? [])]));
    const changed = written
      .filter((sec) => sec && before.get(sec.sectionId) !== JSON.stringify(sec.pageIndices ?? []))
      .map((sec) => (sec as { sectionId: string }).sectionId);
    setRegroupedSectionIds(changed);
    // Which sections' fields no longer match their class. The server says, for every
    // section, including ones not opened in this visit; what follows is the same rule
    // worked out here, for a response that does not carry it. A section's fields follow the
    // section through a save, not its first page: the server keeps each sent section's
    // fields and only renumbers it, so a written section is the sent one with the same
    // pages (no page is in two sections). The class those fields were extracted under is
    // the one recorded when an earlier save first changed it, if one did — they have not
    // been re-extracted since, so a second change (Invoice → W2 → Payslip) still names
    // Invoice — and otherwise the section's class before this save. A section the reviewer
    // added has no fields, so no class to name: the board never gives it an id a ref has,
    // even one deleted in the same edit (#1895).
    const sentByPages = new Map(payload.map((sec) => [JSON.stringify(sec.pageIndices), sec]));
    const refById = new Map(sections.map((sec) => [sec.sectionId, sec]));
    const refByFirstPage = new Map(sections.map((sec) => [sec.pageIndices?.[0], sec]));
    const moved: Record<string, string> = {};
    written.forEach((sec) => {
      if (!sec) return;
      if (sec.extractedAsClass !== undefined) {
        const stamp = extractedAsNote(sec.extractedAsClass, sec.documentClass);
        if (stamp) moved[sec.sectionId] = stamp;
        return;
      }
      const sent = sentByPages.get(JSON.stringify(sec.pageIndices ?? []));
      // A written section matching none sent is not what this save wrote; the section
      // that held its first page is the best remaining guess.
      const ref = sent ? refById.get(sent.sectionId) : refByFirstPage.get((sec.pageIndices ?? [])[0]);
      if (!ref) return;
      const was = reclassified[ref.sectionId] ?? ref.documentClass ?? '';
      // What was saved, as the server reports it; what the board sent if it does not say.
      const after = sec.documentClass ?? sent?.documentClass ?? '';
      if (was && after && was !== after) moved[sec.sectionId] = was;
    });
    setReclassified(moved);
    setSavedSectionCount(written.filter(Boolean).length);
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
  const handleReextract = async (sectionId: string, labelSource?: unknown) => {
    if (!documentClassType || !testSetId) return;
    // Captured now: the open section can change while the run is in flight.
    const target = perSection ? sectionId : null;
    // Authored ground truth (neither a draft nor a reviewed draft) is never overwritten by
    // the run: the class is corrected and the values stay, so "Re-extracted" would be untrue.
    const authored = labelSource !== 'reviewed-human' && labelSource !== 'draft-machine';
    setIsReextracting(true);
    setReextractFailure(null);
    setReextractNote(null);
    try {
      const started = await client.graphql({
        query: reextractTestSetDocument,
        variables: {
          input: {
            testSetId,
            objectKey,
            documentClass: documentClassType,
            configVersion: classConfigVersion,
            // Present only on a document with several sections: the server refuses a
            // whole-document class there, and a single-section document keeps the
            // whole-document form it always had.
            ...(target ? { sectionId: target } : {}),
          },
        },
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
        setReextractNote({ kind: 'timeout', sectionId });
        return;
      }
      // The write-back deleted the section's draft, so the caller's refs still name an
      // object that is gone: re-read them before the section is read again.
      await onChanged();
      setReloadToken((token) => token + 1);
      // Only the re-extracted section's fields now match its class; every other section's
      // still reflect the class they were extracted under.
      setReclassified((prev) => Object.fromEntries(Object.entries(prev).filter(([id]) => id !== sectionId)));
      setReextractNote({ kind: authored ? 'authored' : 'reextracted', sectionId, target, documentClass: documentClassType });
    } catch (err) {
      logger.error('Re-extraction failed:', err);
      setReextractFailure({ target, error: getErrorMessage(err) });
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
          header={intl.formatMessage(messages.pagesMovedHeader, {
            count: regroupedSectionIds.length,
            ids: regroupedSectionIds.join(', '),
          })}
          dismissible
          onDismiss={() => setRegroupedSectionIds([])}
        >
          <FormattedMessage {...(perSection ? messages.pagesMovedBodyPerSection : messages.pagesMovedBody)} values={{ b: bold }} />
        </Alert>
      )}
      {reextractFailure && (
        <Alert type="error">
          <FormattedMessage
            {...(reextractFailure.target ? messages.reextractFailedSection : messages.reextractFailedDocument)}
            values={{ error: reextractFailure.error }}
          />
        </Alert>
      )}
    </>
  );

  /** The note the last re-extract left, worded at render. */
  const reextractNoteText = (note: ReextractNote): string => {
    if (note.kind === 'timeout') return intl.formatMessage(messages.reextractTimeout);
    const { target, documentClass } = note;
    if (note.kind === 'authored') {
      return target
        ? intl.formatMessage(messages.classCorrectedSection, { documentClass, sectionId: target })
        : intl.formatMessage(messages.classCorrectedDocument, { documentClass });
    }
    return target
      ? intl.formatMessage(messages.reextractedSection, { documentClass, sectionId: target })
      : intl.formatMessage(messages.reextractedDocument, { documentClass });
  };

  /* Rendered whenever a baseline is loaded, including when it carries no class. */
  const classSlot: EditorSlot = (ctx) => (
    <>
      {/* A section's class is changed in Edit Page Grouping. Its fields were extracted under
          the previous class, and only a re-extract replaces them. */}
      {ctx.data && reclassified[ctx.selectedSectionId] && testSetId && perSection && (
        <Alert
          type="info"
          header={intl.formatMessage(messages.previousClassHeader)}
          action={
            <Button
              onClick={() => void handleReextract(ctx.selectedSectionId, ctx.data?.labelSource)}
              loading={isReextracting}
              disabled={!mayChangeClass || refsPending}
              disabledReason={refsPending ? intl.formatMessage(messages.regroupPending) : undefined}
              ariaLabel={intl.formatMessage(messages.reextractSectionAria, { sectionId: ctx.selectedSectionId })}
            >
              <FormattedMessage {...(isReextracting ? messages.reextracting : messages.reextract)} />
            </Button>
          }
        >
          <FormattedMessage
            {...messages.previousClassSection}
            values={{
              b: bold,
              previousClass: reclassified[ctx.selectedSectionId],
              documentClass: documentClassType ?? '',
              others: sectionCount - 1,
            }}
          />{' '}
          <FormattedMessage
            {...(ctx.data?.labelSource === 'reviewed-human'
              ? messages.sectionTailReviewed
              : ctx.data?.labelSource === 'draft-machine'
                ? messages.sectionTailDraft
                : messages.sectionTailAuthored)}
          />{' '}
          <FormattedMessage {...messages.mergeWarning} values={{ count: sectionCount }} />
        </Alert>
      )}
      {ctx.data && reclassified[ctx.selectedSectionId] && testSetId && !perSection && (
        <Alert
          type="info"
          header={intl.formatMessage(messages.previousClassHeader)}
          action={
            <Button
              onClick={() => void handleReextract(ctx.selectedSectionId, ctx.data?.labelSource)}
              loading={isReextracting}
              disabled={!mayChangeClass}
            >
              <FormattedMessage {...(isReextracting ? messages.reextracting : messages.reextract)} />
            </Button>
          }
        >
          <FormattedMessage
            {...messages.previousClassDocument}
            values={{ b: bold, previousClass: reclassified[ctx.selectedSectionId], documentClass: documentClassType ?? '' }}
          />{' '}
          <FormattedMessage
            {...(ctx.data?.labelSource === 'reviewed-human'
              ? messages.documentTailReviewed
              : ctx.data?.labelSource === 'draft-machine'
                ? messages.documentTailDraft
                : messages.documentTailAuthored)}
          />
        </Alert>
      )}
      {reextractNote && <Alert type={reextractNote.kind === 'authored' ? 'info' : 'success'}>{reextractNoteText(reextractNote)}</Alert>}
      {splitPageIndices !== undefined && (
        <FormField label={intl.formatMessage(messages.pagesInSection)} description={intl.formatMessage(messages.pagesInSectionHelp)}>
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
      <Alert type="info" header={intl.formatMessage(messages.emptyHeader)}>
        <FormattedMessage
          {...(!documentClassType ? messages.emptyNoClass : inferenceResult ? messages.emptyEmptyResult : messages.emptyNoInference)}
        />{' '}
        {sections.length > 1 && (
          <>
            <FormattedMessage {...messages.emptyOtherSections} />{' '}
          </>
        )}
        <FormattedMessage
          {...(documentClassType && perSection
            ? messages.emptyHintPerSection
            : documentClassType
              ? messages.emptyHintSingle
              : messages.emptyHintMerge)}
          values={{ b: bold }}
        />
      </Alert>
    );
  };

  const pagesNotice = (
    <>
      {previewUnavailable && (
        <Alert type="info">
          <FormattedMessage {...messages.tiffPreview} />
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
    consequence: <FormattedMessage {...messages.regroupConsequence} values={{ b: bold }} />,
    save: saveGrouping,
  };

  return {
    pages,
    pagesLoading,
    pagesNotice,
    reviewSections,
    loadSection,
    reloadToken,
    onLoaded: (sectionId: string, data?: Record<string, unknown>) => {
      setReextractNote((note) => (note && note.sectionId !== sectionId ? null : note));
      // What the section's file records wins over what this visit worked out: it is the
      // class the server recorded, and the only record of it after a reload. A file that
      // records none leaves the visit's note alone, so a server that does not record it
      // keeps the behaviour it had.
      if (!data || !(EXTRACTED_AS_KEY in data)) return;
      const docClass = (data.document_class as Record<string, unknown> | undefined)?.type;
      const stamp = extractedAsNote(data[EXTRACTED_AS_KEY], docClass);
      setReclassified((prev) => {
        if (stamp) return prev[sectionId] === stamp ? prev : { ...prev, [sectionId]: stamp };
        if (!(sectionId in prev)) return prev;
        return Object.fromEntries(Object.entries(prev).filter(([id]) => id !== sectionId));
      });
    },
    classOptions,
    classConfig,
    mergedConfig,
    headerExtras,
    classSlot,
    emptyFieldsSlot,
    board,
    canRegroup,
    refsPending,
  };
};
