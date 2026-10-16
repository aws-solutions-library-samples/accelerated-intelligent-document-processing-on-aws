// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The system-wide review queues as Select options, for the configuration editor's
// `hitl.review_queues.queue` field. Null until loaded, when disabled, or when the
// list can't be read (the editor then falls back to a text input).
import { useEffect, useState } from 'react';
import type { SelectProps } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';

import { listReviewQueues } from '../components/review-queues/reviewer-assignment';

const logger = new ConsoleLogger('useReviewQueueOptions');

/** The configuration path the queue Select replaces the text input for. */
export const REVIEW_QUEUE_CONFIG_PATH = 'hitl.review_queues.queue';

const useReviewQueueOptions = (enabled: boolean): SelectProps.Option[] | null => {
  const [options, setOptions] = useState<SelectProps.Option[] | null>(null);

  useEffect(() => {
    if (!enabled) return undefined;
    let cancelled = false;
    listReviewQueues()
      .then((queues) => {
        if (cancelled) return;
        setOptions(
          [...queues]
            .sort((a, b) => b.priority - a.priority || a.name.localeCompare(b.name))
            .map((q) => ({ value: q.id, label: q.name || q.id, description: q.id })),
        );
      })
      .catch((err: unknown) => logger.warn('listReviewQueues failed; the queue field stays a text input', err));
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  return options;
};

export default useReviewQueueOptions;
