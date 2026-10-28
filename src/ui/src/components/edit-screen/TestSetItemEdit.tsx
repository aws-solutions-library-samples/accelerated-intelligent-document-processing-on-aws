// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit page for a Test Studio document: the Test Studio model
// (test-set-item-edit-model.tsx) over the editor session, rendered by EditPage.
import React from 'react';

import EditPage from './EditPage';
import { useTestSetItemEditModel } from './test-set-item-edit-model';
import useEditorSession from './use-editor-session';

const TestSetItemEdit = ({ testSetId, objectKey }: { testSetId: string; objectKey: string }): React.JSX.Element => {
  const session = useEditorSession();
  return <EditPage model={useTestSetItemEditModel(testSetId, objectKey, session)} session={session} />;
};

export default TestSetItemEdit;
