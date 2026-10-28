// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Reads what /documents/:key/edit needs to choose its mode: the document, then its
// review item only when the document is queue-managed and the caller can work queues
// (at most one extra read). The choice itself is decideEditMode (edit-mode.ts).
import { useCallback, useEffect, useRef, useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';

import { generateClient } from '../../api/client-shim';
import { getDocument as getDocumentQuery } from '../../graphql/generated';
import type { GetDocumentQuery } from '../../graphql/generated/operation-types';
import useUserRole from '../../hooks/use-user-role';
import { fetchReviewItem } from '../review-queues/review-api';
import { decideEditMode, needsItemRead } from './edit-mode';
import type { EditModeDecision, ItemRead } from './edit-mode';

const client = generateClient();
const logger = new ConsoleLogger('EditMode');

export type RawDocument = NonNullable<GetDocumentQuery['getDocument']>;

export type EditModeState =
  | { status: 'loading' }
  | { status: 'document-missing' }
  | { status: 'document-failed'; error: unknown; retry: () => void }
  | { status: 'resolved'; decision: EditModeDecision; document: RawDocument; retry: () => void };

const useEditMode = (objectKey: string): EditModeState => {
  const { loading: roleLoading, canWorkReviewQueues, isAnnotatorOnly } = useUserRole();
  const [state, setState] = useState<EditModeState>({ status: 'loading' });
  // The latest read wins: a retry supersedes a read still in flight.
  const readId = useRef(0);

  const read = useCallback(async () => {
    const id = ++readId.current;
    setState({ status: 'loading' });
    const retry = () => void read();
    let document: RawDocument | null | undefined;
    try {
      const result = await client.graphql({ query: getDocumentQuery, variables: { objectKey } });
      document = result.data?.getDocument;
    } catch (error) {
      logger.error('getDocument failed', error);
      if (id === readId.current) setState({ status: 'document-failed', error, retry });
      return;
    }
    if (id !== readId.current) return;
    if (!document) {
      setState({ status: 'document-missing' });
      return;
    }
    let item: ItemRead = { kind: 'not-read' };
    if (needsItemRead(document.HITLStatus, canWorkReviewQueues)) {
      try {
        item = { kind: 'read', detail: await fetchReviewItem(objectKey) };
      } catch (error) {
        logger.warn('getReviewItem failed', error);
        item = { kind: 'failed', error };
      }
      if (id !== readId.current) return;
    }
    const decision = decideEditMode({ hitlStatus: document.HITLStatus, canWorkReviewQueues, annotatorOnly: isAnnotatorOnly, item });
    setState({ status: 'resolved', decision, document, retry });
  }, [objectKey, canWorkReviewQueues, isAnnotatorOnly]);

  useEffect(() => {
    // Wait for the role: it decides whether the item is read at all.
    if (roleLoading || !objectKey) return undefined;
    void read();
    return () => {
      readId.current += 1;
    };
  }, [roleLoading, objectKey, read]);

  return state;
};

export default useEditMode;
