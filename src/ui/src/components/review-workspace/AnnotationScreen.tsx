// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The unified editor's queue contexts, for a production review item and a Test Studio
// document alike, whether it is opened from My Work › Tasks or from a queue page. (A
// direct edit of any document is DirectEditPage, at /documents/:objectKey/edit.)
//
// Routes:
//   /review-queues/items/:objectKey                      kind="production"
//   /review-queues/test-sets/:testSetId/items/:objectKey kind="test-set"
//
// Assignments do not expire. The assignee finishes the item with Process Changes or
// skips it (gives it back to its queue); an Admin opening it gets the same screen.
import React from 'react';
import { useParams } from 'react-router-dom';

import ProductionAnnotation from './ProductionAnnotation';
import type { ItemQueue } from './ProductionAnnotation';
import TestSetAnnotation from './TestSetAnnotation';
import type { ItemTestSet } from './TestSetAnnotation';

interface AnnotationScreenProps {
  kind: 'production' | 'test-set';
  /** Told the production item's queue once it has loaded (for the breadcrumbs). */
  onItemQueue?: (queue: ItemQueue) => void;
  /** Told the test set's name once it has loaded (for the breadcrumbs). */
  onTestSet?: (testSet: ItemTestSet) => void;
}

const AnnotationScreen = ({ kind, onItemQueue, onTestSet }: AnnotationScreenProps): React.JSX.Element => {
  const { objectKey: rawKey = '', testSetId = '' } = useParams<{ objectKey: string; testSetId?: string }>();
  const objectKey = decodeURIComponent(rawKey);
  // Keyed on the item so moving between items remounts: per-item state (the checklist,
  // the sections saved so far, the opened version transition) never carries over.
  return kind === 'production' ? (
    <ProductionAnnotation key={objectKey} objectKey={objectKey} onItemQueue={onItemQueue} />
  ) : (
    <TestSetAnnotation key={`${testSetId}/${objectKey}`} testSetId={testSetId} objectKey={objectKey} onTestSet={onTestSet} />
  );
};

export default AnnotationScreen;
