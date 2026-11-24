// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The short category the Inbox table shows in its Type column ("Task", "Queue"), so
// the title alone says what happened. Inbox settings keep each type's full name.
import { defineMessages, type IntlShape, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';

const TASK_TYPES = /^review\.(task_|claim_|item_reassigned$)/;
const QUEUE_TYPES = /^review\.queue_/;

const messages = defineMessages<{ task: NoMessageValues; queue: NoMessageValues }>({
  task: {
    id: 'inbox.category.task',
    defaultMessage: 'Task',
    description:
      "Type column of the Inbox table for a message about the reader's own review work: an assignment, a claim released or expiring.",
  },
  queue: {
    id: 'inbox.category.queue',
    defaultMessage: 'Queue',
    description: 'Type column of the Inbox table for a message about a review queue the reader works, such as items waiting in it.',
  },
});

const labelOf = (message: TypedMessageDescriptor<NoMessageValues>, intl?: IntlShape): string =>
  intl ? intl.formatMessage(message) : String(message.defaultMessage);

/**
 * "Task" for your claimed work, "Queue" for queue-level news, else the type's own name, as
 * the backend writes it. In the UI's language with `intl`; without it, the English source text.
 */
export const messageCategory = (typeId: string, displayName: string, intl?: IntlShape): string => {
  if (TASK_TYPES.test(typeId)) return labelOf(messages.task, intl);
  if (QUEUE_TYPES.test(typeId)) return labelOf(messages.queue, intl);
  return displayName;
};
