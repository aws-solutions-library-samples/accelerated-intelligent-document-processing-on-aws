// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The one annotation screen, for a production review item and a Test Studio document
// alike, whether it is opened from My Work › Tasks or from a queue page.
//
// Routes:
//   /review-queues/items/:objectKey                      kind="production"
//   /review-queues/test-sets/:testSetId/items/:objectKey kind="test-set"
//
// An Admin assigns items to Annotators and assignments do not expire, so the screen
// has no claim, lease or release. The Annotator completes the item (and, for a
// production item, may reject it); an Admin opening it gets the same screen.
import React from 'react';
import { useParams } from 'react-router-dom';

import ProductionAnnotation from './ProductionAnnotation';
import TestSetAnnotation from './TestSetAnnotation';

interface AnnotationScreenProps {
  kind: 'production' | 'test-set';
}

const AnnotationScreen = ({ kind }: AnnotationScreenProps): React.JSX.Element => {
  const { objectKey: rawKey = '', testSetId = '' } = useParams<{ objectKey: string; testSetId?: string }>();
  const objectKey = decodeURIComponent(rawKey);
  // Keyed on the item so moving between items remounts: per-item state (the checklist,
  // the sections saved so far, the opened version transition) never carries over.
  return kind === 'production' ? (
    <ProductionAnnotation key={objectKey} objectKey={objectKey} />
  ) : (
    <TestSetAnnotation key={`${testSetId}/${objectKey}`} testSetId={testSetId} objectKey={objectKey} />
  );
};

export default AnnotationScreen;
