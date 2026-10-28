// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// /documents/:objectKey/edit, as routed in both app shells: the full one (under
// Documents) and the Annotator-only one, whose users reach a document's edit page from
// Tasks and Inbox messages but have no documents app.
import React from 'react';

import DocumentEditPage from '../components/edit-screen/DocumentEditPage';
import GenAIIDPTopNavigation from '../components/genai-idp-top-navigation';

const DocumentEditRoute = (): React.JSX.Element => (
  <div>
    <GenAIIDPTopNavigation />
    <DocumentEditPage />
  </div>
);

export default DocumentEditRoute;
