// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Test helpers for the document page's bar: its menus (Reports, More Actions) and their
// items, by label, over the shared ActionBar helpers.
import { barHelpers } from '../../common/__tests__/action-bar-helpers';

export const DETAIL_MENUS = ['Reports', 'More Actions'] as const;

/** Each menu item's id in the bar, by its label. */
export const DETAIL_ITEM_IDS: Record<string, string> = {
  Summary: 'summary',
  'Evaluation Report': 'evaluation',
  'Rule Validation Summary': 'rule-validation',
  'Estimated Cost': 'cost',
  'Processing Flow': 'flow',
  'Edit Page Grouping': 'regroup',
  'Re-extract Sections': 'reextract',
  'Edit Page Text': 'page-text',
  'Reset Page Class': 'reset-class',
  'Use As Evaluation Baseline': 'baseline',
  'Skip All Reviews': 'skip-all',
  Reprocess: 'reprocess',
  Abort: 'abort',
  Delete: 'delete',
  'Download All (ZIP)': 'all',
  'Download Predictions (ZIP)': 'predictions',
  'Download Baselines (ZIP)': 'baselines',
};

export const detailBar = barHelpers(DETAIL_MENUS, DETAIL_ITEM_IDS);
