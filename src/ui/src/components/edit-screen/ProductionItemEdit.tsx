// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit page for a production review item: the Production model
// (production-item-edit-model.tsx) over the editor session, rendered by EditPage.
import React from 'react';

import type { ReviewItemDetail } from '../../graphql/generated/operation-types';

import EditPage from './EditPage';
import { useProductionItemEditModel } from './production-item-edit-model';
import type { RawDocument } from './use-edit-mode';
import useEditorSession from './use-editor-session';

interface ProductionItemEditProps {
  objectKey: string;
  /** The item and the document as the edit page read them to choose this mode. */
  seed?: { detail: ReviewItemDetail; document: RawDocument };
}

const ProductionItemEdit = ({ objectKey, seed }: ProductionItemEditProps): React.JSX.Element => {
  const session = useEditorSession();
  return <EditPage model={useProductionItemEditModel(objectKey, session, seed)} session={session} />;
};

export default ProductionItemEdit;
