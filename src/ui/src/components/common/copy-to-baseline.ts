// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Use As Evaluation Baseline: copies a processed document's results into the evaluation
// baseline bucket (copyToBaseline, Admin and Author). Shared by More Actions on the
// document page and on the edit page. The copy runs in the background; the document's
// evaluation status moves to BASELINE_COPYING, then BASELINE_AVAILABLE or BASELINE_ERROR.
import { generateClient } from '../../api/client-shim';
import { copyToBaseline } from '../../graphql/generated';

const client = generateClient();

/** Start the copy. Resolves with the server's message; throws with it when the copy cannot start. */
export const copyDocumentToBaseline = async (objectKey: string): Promise<string> => {
  const result = await client.graphql({ query: copyToBaseline, variables: { objectKey } });
  const response = result.data?.copyToBaseline as { success?: boolean; message?: string | null } | null | undefined;
  if (!response?.success) throw new Error(response?.message || 'The copy to the evaluation baseline could not be started');
  return response.message ?? '';
};

/** Why Use as Evaluation Baseline is unavailable for a document's evaluation status, if it is. */
export const baselineCopyBlockedReason = (evaluationStatus: string | null | undefined): string | undefined =>
  evaluationStatus === 'BASELINE_COPYING' ? 'A copy to the evaluation baseline is in progress' : undefined;
