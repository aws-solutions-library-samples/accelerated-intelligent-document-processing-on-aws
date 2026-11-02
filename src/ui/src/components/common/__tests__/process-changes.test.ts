// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * What submitProcessChanges sends. What has to hold: the sections as given; the pages
 * only when there are some (so a call without them is the same request it always was);
 * and the server's refusal as a throw with its message.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));

import { processChanges } from '../../../graphql/generated';
import { submitProcessChanges } from '../process-changes';

beforeEach(() => {
  graphql.mockReset().mockResolvedValue({ data: { processChanges: { success: true } } });
});

describe('submitProcessChanges', () => {
  it('sends the sections, and no pages when there are none', async () => {
    const sections = [{ sectionId: '1', classification: 'W2', pageIds: [1], isNew: false, isDeleted: false }];
    await submitProcessChanges('in/a.pdf', sections);
    expect(graphql).toHaveBeenCalledWith({ query: processChanges, variables: { objectKey: 'in/a.pdf', modifiedSections: sections } });
  });

  it('sends the pages when there are some', async () => {
    const pages = [{ pageId: 3, classReset: true, textModified: false }];
    await submitProcessChanges('in/a.pdf', [], pages);
    expect(graphql).toHaveBeenCalledWith({
      query: processChanges,
      variables: { objectKey: 'in/a.pdf', modifiedSections: [], modifiedPages: pages },
    });
  });

  it('throws with the server’s message when it refuses', async () => {
    graphql.mockResolvedValue({ data: { processChanges: { success: false, message: 'In review' } } });
    await expect(submitProcessChanges('in/a.pdf')).rejects.toThrow('In review');
  });
});
