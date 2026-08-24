// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * GraphQL operation strings for the feature platform.
 *
 * These are raw strings rather than generated operations because the
 * feature-platform schema fragment is only merged into the main AppSync
 * schema when `EnableFeaturePlatform=true`, and codegen runs against a
 * single schema snapshot. Using raw strings lets the UI compile regardless
 * of whether the feature platform is turned on at build time.
 *
 * Use with: `import { generateClient } from '../api/client-shim';`
 *           `client.graphql({ query: LIST_INSTALLED_FEATURES });`
 */

export const LIST_CATALOG_FEATURES = /* GraphQL */ `
  query ListCatalogFeatures {
    listCatalogFeatures {
      featureId
      displayName
      latestVersion
      iconUrl
      description
      docsUrl
      showInNav
      source
      productCode
      marketplaceListingUrl
      availableInRegion
      availableRegions
    }
  }
`;

export const LIST_INSTALLED_FEATURES = /* GraphQL */ `
  query ListInstalledFeatures {
    listInstalledFeatures {
      featureId
      displayName
      installedVersion
      latestVersion
      updateAvailable
      stackName
      stackRegion
      stackId
      uiBundlePath
      featureApiEndpoint
      iconUrl
      installedAt
      installedBy
    }
  }
`;

export const CHECK_FEATURE_ENTITLEMENT = /* GraphQL */ `
  query CheckFeatureEntitlement($featureId: String!) {
    checkFeatureEntitlement(featureId: $featureId) {
      featureId
      state
      expiresAt
      customerIdentifier
      productCode
      source
      licenseMode
      declaredLicenseMode
      catalogLicenseMode
      licenseModeMismatch
    }
  }
`;

export const GET_FEATURE_LAUNCH_URL = /* GraphQL */ `
  query GetFeatureLaunchUrl($featureId: String!, $version: String) {
    getFeatureLaunchUrl(featureId: $featureId, version: $version) {
      featureId
      version
      launchUrl
      templateUrl
      stackName
      parameters
    }
  }
`;

export const SUBSCRIBE_FEATURE = /* GraphQL */ `
  mutation SubscribeFeature($featureId: String!, $returnUrl: String) {
    subscribeFeature(featureId: $featureId, returnUrl: $returnUrl) {
      featureId
      state
      expiresAt
      customerIdentifier
      productCode
      source
      marketplaceUrl
    }
  }
`;

export const UNSUBSCRIBE_FEATURE = /* GraphQL */ `
  mutation UnsubscribeFeature($featureId: String!) {
    unsubscribeFeature(featureId: $featureId) {
      featureId
      state
      expiresAt
      customerIdentifier
      productCode
      source
    }
  }
`;
