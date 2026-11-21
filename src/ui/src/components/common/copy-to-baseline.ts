// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Use As Evaluation Baseline: copies a processed document's results into the evaluation
// baseline bucket (copyToBaseline, Admin and Author). Shared by More Actions on the
// document page and on the edit page. The copy runs in the background; the document's
// evaluation status moves to BASELINE_COPYING, then BASELINE_AVAILABLE or BASELINE_ERROR.
import { defineMessages, type IntlShape, type NoMessageValues } from 'react-intl';

import { generateClient } from '../../api/client-shim';
import { copyToBaseline } from '../../graphql/generated';

const client = generateClient();

const messages = defineMessages<{ inProgress: NoMessageValues }>({
  inProgress: {
    id: 'common.copyToBaseline.inProgress',
    defaultMessage: 'A copy to the evaluation baseline is in progress',
    description: "Why Use As Evaluation Baseline is unavailable: a copy of the document's results to the evaluation baseline is running.",
  },
});

/** Start the copy. Resolves with the server's message; throws with it when the copy cannot start. */
export const copyDocumentToBaseline = async (objectKey: string): Promise<string> => {
  const result = await client.graphql({ query: copyToBaseline, variables: { objectKey } });
  const response = result.data?.copyToBaseline as { success?: boolean; message?: string | null } | null | undefined;
  if (!response?.success) throw new Error(response?.message || 'The copy to the evaluation baseline could not be started');
  return response.message ?? '';
};

/**
 * Why Use as Evaluation Baseline is unavailable for a document's evaluation status, if it is:
 * in the UI's language with `intl`, the English source text without.
 */
export const baselineCopyBlockedReason = (evaluationStatus: string | null | undefined, intl?: IntlShape): string | undefined => {
  if (evaluationStatus !== 'BASELINE_COPYING') return undefined;
  return intl ? intl.formatMessage(messages.inProgress) : String(messages.inProgress.defaultMessage);
};
