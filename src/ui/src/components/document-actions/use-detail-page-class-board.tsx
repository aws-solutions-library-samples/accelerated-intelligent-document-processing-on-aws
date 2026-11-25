// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The document page's Edit Page Grouping: the edit page's board (use-page-class-board),
// over this document's pages and sections, with Save page grouping
// (updateDocumentSections, which keeps the extracted values) and Save and Re-extract
// (that save, then processChanges for the sections whose class or pages changed).
import React, { useMemo } from 'react';
import { defineMessages, FormattedMessage, useIntl, type MessageTag } from 'react-intl';

import { getConfigClassOptions } from '../common/config-class-options';
import { saveDocumentGroupingOrThrow } from '../common/document-grouping';
import { submitProcessChanges } from '../common/process-changes';
import type { GroupedSection } from '../common/section-grouping';
import type { EditBoard } from '../edit-screen/edit-model';
import { documentName } from '../review-queues/review-api';
import { actionReasonMessages, reextractNoClassReason } from './action-reasons';
import type { DocumentActionGates } from './document-action-gates';

/** Renders a `<b>...</b>` element value: a button's label named inside a sentence. */
const bold = (chunks: React.ReactNode[]): React.JSX.Element => <b>{chunks}</b>;

const messages = defineMessages<{
  consequence: { readonly b: MessageTag };
  reextracting: { readonly name: string; readonly count: number; readonly ids: string };
}>({
  consequence: {
    id: 'documentActions.pageGrouping.consequence',
    defaultMessage:
      "<b>Save page grouping</b> rewrites this document's section grouping and keeps the extracted field values, including any a reviewer corrected; the document is not reprocessed, so they may no longer match their pages. <b>Save and Re-extract</b> saves the grouping, then extracts again the sections whose class or pages changed, replacing their values.",
    description:
      'What the two saves of the Edit Page Grouping board on the document page do. <b>Save page grouping</b> and <b>Save and Re-extract</b> are the labels of its two save buttons, shown in bold.',
  },
  reextracting: {
    id: 'documentActions.pageGrouping.reextracting',
    defaultMessage: 'Saved the page grouping of {name}, and re-extracting {count, plural, one {section} other {sections}} {ids}.',
    description:
      'Success notification after Save and Re-extract on the Edit Page Grouping board saved the grouping and sent the re-extract. {name} is the document name; {count} is how many sections; {ids} lists their numbers, separated by commas.',
  },
});

interface DetailBoardDocument {
  objectKey: string;
  sections?: Record<string, unknown>[];
  pages?: Record<string, unknown>[];
}

interface DetailBoardInput {
  item: DetailBoardDocument;
  gates: Pick<DocumentActionGates, 'canChangeClass' | 'reextractBlockedReason'>;
  mergedConfig: Record<string, unknown> | null | undefined;
  /** Re-read the document, so the page shows the new grouping. */
  refresh: () => Promise<unknown>;
}

/** The sections to re-extract, as processChanges takes them: each with its class and pages, as saved. */
export const toModifiedSections = (sections: GroupedSection[]) =>
  sections.map((s) => ({
    sectionId: s.sectionId,
    classification: s.documentClass ?? '',
    pageIds: s.pageIds,
    isNew: false,
    isDeleted: false,
  }));

export const useDetailPageClassBoard = ({ item, gates, mergedConfig, refresh }: DetailBoardInput): EditBoard => {
  const intl = useIntl();
  const key = item.objectKey;
  const name = documentName(key);
  const sections = useMemo<GroupedSection[]>(
    () =>
      (item.sections ?? []).map((section) => ({
        sectionId: String(section.Id),
        documentClass: (section.Class as string | null | undefined) ?? null,
        pageIds: ((section.PageIds as (string | number)[] | undefined) ?? []).map((id) => Number(id)),
      })),
    [item.sections],
  );
  const pages = useMemo(
    () =>
      (item.pages ?? []).filter((p) => p.Id != null).map((p) => ({ Id: String(p.Id), ImageUri: (p.ImageUri as string | undefined) ?? '' })),
    [item.pages],
  );

  return {
    pages: { kind: 'thumbnails', pages },
    sections: () => sections,
    classOptions: getConfigClassOptions(mergedConfig),
    canChangeClass: gates.canChangeClass,
    consequence: <FormattedMessage {...messages.consequence} values={{ b: bold }} />,
    save: async (next) => {
      const message = await saveDocumentGroupingOrThrow(key, next);
      await refresh();
      return message;
    },
    reextract: {
      disabledReason: (changed) => {
        if (gates.reextractBlockedReason) return gates.reextractBlockedReason;
        const noClass = changed.filter((s) => !s.documentClass).map((s) => s.sectionId);
        if (noClass.length > 0) return reextractNoClassReason(noClass, intl);
        return changed.length === 0 ? intl.formatMessage(actionReasonMessages.reextractOrderOnly) : undefined;
      },
      run: async (changed) => {
        try {
          await submitProcessChanges(key, toModifiedSections(changed));
        } finally {
          void refresh();
        }
        const ids = changed.map((s) => s.sectionId).join(', ');
        return intl.formatMessage(messages.reextracting, { name, count: changed.length, ids });
      },
    },
  };
};

export default useDetailPageClassBoard;
