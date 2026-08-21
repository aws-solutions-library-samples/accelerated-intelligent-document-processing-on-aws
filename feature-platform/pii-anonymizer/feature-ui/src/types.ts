// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Local mirror of the host's FeatureContext type. Keep in sync with
 *   src/ui/src/types/feature-platform.ts (in the main IDP UI).
 */
export interface FeatureContext {
  featureId: string;
  installedVersion: string;
  featureApiEndpoint: string | null;
  getAuthToken: () => Promise<string>;
  mainStackName: string;
  uiAccessAllowed: boolean;
}

export interface FeatureRegistration {
  Component: React.ComponentType<FeatureContext>;
  version: string;
  displayName: string;
}

declare global {
  interface Window {
    IdpFeatures?: {
      register: (featureId: string, registration: FeatureRegistration) => void;
    };
  }
}

// ---- Redaction report types (feature API) ----------------------------------

export interface RedactionRow {
  documentId: string;
  createdAt: string;
  sourceKey?: string;
  redactedKey?: string;
  mode?: string;
  companionConfigVersion?: string;
  originalConfigVersion?: string;
  piiCount?: number;
  replacements?: number;
  halted?: boolean;
  mappingStored?: boolean;
  executionArn?: string;
}

export interface RedactionReportResponse {
  rows: RedactionRow[];
  total: number;
  totalPiiRedacted: number;
  window: string;
  asOf: string;
}

// ---- Config-pairing wizard types -------------------------------------------

export type RedactionMode = 'redactcopy_and_stop' | 'redactcopy_and_continue';

export interface ConfigVersionSummary {
  versionName: string;
  isActive?: boolean;
  description?: string;
}

export interface PairPlan {
  baseVersion: string;
  initiatingVersion: string; // <base>__pii_stop | <base>__pii_go
  companionVersion: string; // <base>__pii_target
  mode: RedactionMode;
  detectionModelId: string;
  redactionMode: 'synthetic' | 'blackout';
}
