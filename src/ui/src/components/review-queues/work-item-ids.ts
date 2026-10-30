// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The ids of Home › Tasks' work items, so a finished item is dropped by the same id it
// was listed under.

/** A Production review item's WorkItem.id. */
export const productionWorkItemId = (objectKey: string): string => `p:${objectKey}`;

/** A Test Studio document's WorkItem.id. */
export const testSetWorkItemId = (testSetId: string, objectKey: string): string => `t:${testSetId}:${objectKey}`;
