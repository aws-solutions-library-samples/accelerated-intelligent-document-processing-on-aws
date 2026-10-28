// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// /documents/:objectKey/edit[?section=&field=&view=source]: a document's edit page. It
// opens the document's review-queue item while the document is in review and the caller
// can work queues (Admin, Annotator), and the direct edit otherwise (useEditMode,
// decideEditMode). Opened from the document page's Edit, a section's Edit Data, the
// documents list's In Review link, Home › Tasks and Inbox messages.
import React from 'react';
import { useParams, useSearchParams } from 'react-router-dom';

import useUserRole from '../../hooks/use-user-role';
import DocumentEdit from './DocumentEdit';
import EditPage from './EditPage';
import { documentFailedModel, documentMissingModel, itemFailedModel, loadingDocumentModel, notAMemberModel } from './edit-mode-states';
import ProductionItemEdit from './ProductionItemEdit';
import useEditMode from './use-edit-mode';

const DocumentEditPage = (): React.JSX.Element => {
  // useParams has already decoded the key once; decoding it again breaks a key with `%`.
  const { objectKey = '' } = useParams<{ objectKey: string }>();
  const [searchParams] = useSearchParams();
  const { isAnnotatorOnly } = useUserRole();
  const mode = useEditMode(objectKey);

  if (mode.status === 'loading') return <EditPage model={loadingDocumentModel()} />;
  if (mode.status === 'document-missing') return <EditPage model={documentMissingModel(isAnnotatorOnly)} />;
  if (mode.status === 'document-failed') return <EditPage model={documentFailedModel(mode.error, mode.retry, isAnnotatorOnly)} />;

  const { decision, document } = mode;
  if (decision.mode === 'queue')
    return <ProductionItemEdit key={objectKey} objectKey={objectKey} seed={{ detail: decision.detail, document }} />;
  if (decision.mode === 'not-a-member') return <EditPage model={notAMemberModel()} />;
  if (decision.mode === 'item-failed') return <EditPage model={itemFailedModel(decision.error, mode.retry)} />;
  return (
    <DocumentEdit
      key={objectKey}
      objectKey={objectKey}
      initialSectionId={searchParams.get('section')}
      seed={{ document, lock: decision.lock, reviewActivity: decision.reviewActivity }}
    />
  );
};

export default DocumentEditPage;
