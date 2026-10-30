// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Test helpers for Edit Page Grouping on the Document Editor panel, where the tests mock
// SectionEditor: the mock renders its `regroup` prop as a plain button
// (data-testid="editor-regroup", the reason in data-reason), read and pressed here.
import { act } from '@testing-library/react';

const button = (): HTMLButtonElement | null => document.querySelector('[data-testid="editor-regroup"]');

/** Edit Page Grouping as the panel offers it, in the shape the bar helpers report; undefined when not offered. */
export const regroupItem = (): { text: string; disabled: boolean; disabledReason: string | null } | undefined => {
  const b = button();
  return b ? { text: 'Edit Page Grouping', disabled: b.disabled, disabledReason: b.getAttribute('data-reason') } : undefined;
};

/** Press Edit Page Grouping on the panel. */
export const chooseRegroup = (): void => {
  const b = button();
  if (!b) throw new Error('Edit Page Grouping is not offered');
  act(() => b.click());
};
