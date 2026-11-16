// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/** Whether the stack's `EnableQuickStartWidget` parameter leaves Quick Start on. */
export const isQuickStartEnabled = (): boolean => {
  const flag = import.meta.env.VITE_ENABLE_QUICK_START_WIDGET;
  return flag === undefined || flag === 'true' || flag === true;
};
