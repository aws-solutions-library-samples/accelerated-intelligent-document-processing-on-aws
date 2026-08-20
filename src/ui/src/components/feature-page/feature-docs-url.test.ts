// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';

import { resolveFeatureDocsUrl } from './feature-docs-url';
import type { CatalogFeature } from '../../types/feature-platform';

const base = (over: Partial<CatalogFeature>): CatalogFeature => ({
  featureId: 'f',
  displayName: 'F',
  latestVersion: '1.0.0',
  iconUrl: null,
  description: null,
  docsUrl: null,
  showInNav: true,
  source: 'oss',
  productCode: null,
  marketplaceListingUrl: null,
  availableInRegion: true,
  availableRegions: [],
  ...over,
});

describe('resolveFeatureDocsUrl', () => {
  it('returns null when nothing is available', () => {
    expect(resolveFeatureDocsUrl(null)).toBeNull();
    expect(resolveFeatureDocsUrl(base({}))).toBeNull();
  });

  it('resolves an OSS docs-site slug against the docs base', () => {
    const url = resolveFeatureDocsUrl(base({ docsUrl: 'extensions/sample-document-status' }));
    expect(url).toBe(
      'https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws/extensions/sample-document-status/',
    );
  });

  it('tolerates a leading slash on the slug', () => {
    const url = resolveFeatureDocsUrl(base({ docsUrl: '/extensions/x' }));
    expect(url).toContain('/extensions/x/');
    expect(url).not.toContain('//extensions');
  });

  it('passes an absolute docsUrl through unchanged', () => {
    const abs = 'https://example.com/docs/my-feature';
    expect(resolveFeatureDocsUrl(base({ docsUrl: abs }))).toBe(abs);
  });

  it('falls back to marketplaceListingUrl when docsUrl is empty', () => {
    const listing = 'https://aws.amazon.com/marketplace/pp/prodview-x';
    expect(resolveFeatureDocsUrl(base({ docsUrl: '', marketplaceListingUrl: listing }))).toBe(listing);
  });

  it('prefers docsUrl over marketplaceListingUrl', () => {
    const url = resolveFeatureDocsUrl(base({ docsUrl: 'extensions/x', marketplaceListingUrl: 'https://aws.amazon.com/marketplace/pp/y' }));
    expect(url).toContain('/extensions/x/');
  });
});
