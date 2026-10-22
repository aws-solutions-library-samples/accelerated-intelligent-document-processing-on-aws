// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// /documents/:objectKey/edit[?section=<id>]: the unified editor's direct edit of any
// document, opened from Edit Data on a section row or the document page's Edit button.
// Its breadcrumb is Home › Documents › <document>, the same shape as a queue item's.
import React from 'react';
import { useParams, useSearchParams } from 'react-router-dom';

import { DOCUMENTS_PATH, documentDetailHref } from '../../routes/constants';
import ReviewQueuesLayout from '../review-queues/ReviewQueuesLayout';
import { documentName } from '../review-queues/review-api';
import DirectEditAnnotation from './DirectEditAnnotation';

const DirectEditPage = (): React.JSX.Element => {
  const { objectKey: rawKey = '' } = useParams<{ objectKey: string }>();
  const [searchParams] = useSearchParams();
  const objectKey = decodeURIComponent(rawKey);
  return (
    <ReviewQueuesLayout
      root={{ text: 'Documents', href: `#${DOCUMENTS_PATH}` }}
      crumbs={[{ text: documentName(objectKey), href: documentDetailHref(objectKey) }]}
    >
      <DirectEditAnnotation key={objectKey} objectKey={objectKey} initialSectionId={searchParams.get('section')} />
    </ReviewQueuesLayout>
  );
};

export default DirectEditPage;
