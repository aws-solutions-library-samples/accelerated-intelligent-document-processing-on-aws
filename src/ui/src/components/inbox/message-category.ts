// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The short category the Inbox table shows in its Type column ("Task", "Queue"), so
// the title alone says what happened. Inbox settings keep each type's full name.

const TASK_TYPES = /^review\.(task_|claim_|item_reassigned$)/;
const QUEUE_TYPES = /^review\.(queue_|item_escalated$)/;

/** "Task" for your claimed work, "Queue" for queue-level news, else the type's own name. */
export const messageCategory = (typeId: string, displayName: string): string => {
  if (TASK_TYPES.test(typeId)) return 'Task';
  if (QUEUE_TYPES.test(typeId)) return 'Queue';
  return displayName;
};
