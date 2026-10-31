// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit page's direct edit of a document: the direct-edit model (direct-edit-model.tsx)
// over the editor session, rendered by EditPage.
import React from 'react';

import { useDirectEditModel } from './direct-edit-model';
import type { DirectEditOptions } from './direct-edit-model';
import EditPage from './EditPage';
import useEditorSession from './use-editor-session';

const DocumentEdit = (props: DirectEditOptions): React.JSX.Element => {
  const session = useEditorSession();
  return <EditPage model={useDirectEditModel(props, session)} session={session} />;
};

export default DocumentEdit;
