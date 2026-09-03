// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Shared option lists and validators for test-set creation, so every entry point
 * validates identically. The rules mirror validate_test_set_name /
 * validate_description in
 * nested/api-resolvers/src/lambda/test_set_resolver/index.py and must be kept in
 * step with them.
 */

import type { SelectProps } from '@cloudscape-design/components';

export const BUCKET_OPTIONS: SelectProps.Option[] = [
  { label: 'Input Bucket', value: 'input' },
  { label: 'Test Set Bucket', value: 'testset' },
];

export const TIME_FILTER_OPTIONS: SelectProps.Option[] = [
  { label: 'No filter', value: '' },
  { label: 'Last 1 hour', value: '1' },
  { label: 'Last 4 hours', value: '4' },
  { label: 'Last 24 hours', value: '24' },
  { label: 'Last 7 days', value: '168' },
  { label: 'Last 30 days', value: '720' },
  { label: 'Custom date/time', value: 'custom' },
];

export const DOCUMENT_CLASS_TYPE_OPTIONS: SelectProps.Option[] = [
  { label: 'Unspecified', value: '' },
  { label: 'Single Class', value: 'SINGLE_CLASS' },
  { label: 'Multi Class', value: 'MULTI_CLASS' },
  { label: 'Packet Splitting', value: 'PACKET_SPLITTING' },
];

/** Mirrors the resolver's validate_test_set_name. */
export const validateTestSetName = (name: string): boolean => /^[a-zA-Z0-9\s_-]+$/.test(name) && name.length <= 50;

/** Mirrors the resolver's validate_description. */
export const validateDescription = (desc: string): boolean => desc.length <= 500;

/**
 * How a test set is created. Each source leaves the set in a materially different
 * state, so the UI names sources by outcome (labeled, needs labeling, synthetic)
 * rather than by mechanism.
 */
export type CreateSource = 'upload-labeled' | 'upload-documents' | 'existing-files' | 'generate';

export interface CreateSourceMeta {
  value: CreateSource;
  label: string;
  description: string;
  /** What you have when the wizard finishes. */
  outcome: string;
}

export const CREATE_SOURCES: CreateSourceMeta[] = [
  {
    value: 'upload-labeled',
    label: 'Upload documents with ground truth',
    description: 'A zip containing input/ and baseline/ folders. Use this when you already have verified labels.',
    outcome: 'Ready to publish',
  },
  {
    value: 'upload-documents',
    label: 'Upload documents only',
    description: 'A zip of documents with no labels. Generate draft labels afterwards and review the least confident.',
    outcome: 'Needs labeling',
  },
  {
    value: 'existing-files',
    label: 'From files in S3',
    description:
      'Match documents already in the input bucket (file you have already processed)or files you have uploaded to the test set bucket, by file pattern.',
    outcome: 'Labeled where baselines exist',
  },
  {
    value: 'generate',
    label: 'Generate synthetic documents',
    description: 'Create documents and matching ground truth from a configuration or a description.',
    outcome: 'Synthetic, labeled',
  },
];
