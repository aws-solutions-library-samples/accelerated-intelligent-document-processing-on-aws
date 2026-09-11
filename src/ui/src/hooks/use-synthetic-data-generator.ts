// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { useCallback, useMemo, useState } from 'react';
import { fetchSharedAuthSession } from '../api/auth-session';
import { ConsoleLogger } from 'aws-amplify/utils';
import useInstalledFeatures from './use-installed-features';

const logger = new ConsoleLogger('useSyntheticDataGenerator');

// The Test Set Generator (SEED) Feature Platform extension. When installed it
// registers a feature API exposing POST /generate and /generate-from-config.
export const DATA_GENERATOR_FEATURE_ID = 'idp-data-generator';

// Destination test set for the generated docs: either a new set (by name) or an
// existing one (by id, appended to).
export interface TestSetDestination {
  testSetName?: string;
  testSetId?: string;
}

export interface GenerateFromPromptArgs extends TestSetDestination {
  prompt: string;
  count: number;
  className?: string;
  augment?: boolean;
  threshold?: number;
  scenario?: string;
}

export interface GenerateFromConfigArgs extends TestSetDestination {
  configVersion: string;
  className: string;
  count: number;
  augment?: boolean;
  threshold?: number;
  scenario?: string;
}

export interface TestSetSummary {
  id: string;
  name: string;
  status?: string;
}

export interface SuggestScenarioArgs {
  className?: string;
  versionName?: string;
  prompt?: string;
}

export interface CostEstimate {
  documents: number;
  estimated_usd_low: number;
  estimated_usd_high: number;
  estimated_minutes_low: number;
  estimated_minutes_high: number;
  note?: string;
}

interface GenerateResponse {
  jobId?: string;
  error?: string;
}

export interface JobStatus {
  jobId: string;
  status: string;
  statusMessage?: string;
  errorMessage?: string;
  testSetId?: string;
  configVersion?: string;
}

async function _authToken(): Promise<string> {
  const session = await fetchSharedAuthSession();
  const jwt = session.tokens?.idToken?.toString();
  if (!jwt) throw new Error('No Cognito idToken available');
  return jwt;
}

/**
 * Discovers the Test Set Generator extension and calls its generation API.
 *
 * `available` is false when the extension is not installed (or exposes no API
 * endpoint), so callers can hide/disable the entry point and degrade gracefully
 * — schema authoring and manual test sets work without the generator.
 */
const useSyntheticDataGenerator = () => {
  const { byId, loading: featuresLoading } = useInstalledFeatures();
  const [submitting, setSubmitting] = useState(false);

  const feature = byId(DATA_GENERATOR_FEATURE_ID);
  const endpoint = feature?.featureApiEndpoint || null;
  const available = Boolean(endpoint);

  const _post = useCallback(
    async (path: string, body: Record<string, unknown>): Promise<string> => {
      if (!endpoint) {
        throw new Error('The Test Set Generator extension is not installed');
      }
      setSubmitting(true);
      try {
        const token = await _authToken();
        const resp = await fetch(`${endpoint.replace(/\/$/, '')}${path}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: token },
          body: JSON.stringify(body),
        });
        const data = (await resp.json().catch(() => ({}))) as GenerateResponse;
        if (!resp.ok || data.error) {
          throw new Error(data.error || `Generation request failed (${resp.status})`);
        }
        if (!data.jobId) {
          throw new Error('Generation request did not return a job id');
        }
        return data.jobId;
      } catch (err) {
        logger.error('Synthetic data generation request failed', err);
        throw err;
      } finally {
        setSubmitting(false);
      }
    },
    [endpoint],
  );

  const listActiveJobs = useCallback(async (): Promise<JobStatus[]> => {
    if (!endpoint) return [];
    try {
      const token = await _authToken();
      const resp = await fetch(`${endpoint.replace(/\/$/, '')}/jobs`, {
        headers: { Authorization: token },
      });
      if (!resp.ok) return [];
      const data = (await resp.json().catch(() => ({}))) as { jobs?: JobStatus[] };
      return data.jobs || [];
    } catch (err) {
      logger.warn('Active jobs poll failed', err);
      return [];
    }
  }, [endpoint]);

  const getJobStatus = useCallback(
    async (jobId: string): Promise<JobStatus | null> => {
      if (!endpoint) return null;
      try {
        const token = await _authToken();
        const resp = await fetch(`${endpoint.replace(/\/$/, '')}/jobs/${encodeURIComponent(jobId)}`, {
          headers: { Authorization: token },
        });
        if (!resp.ok) return null;
        const data = (await resp.json().catch(() => ({}))) as { job?: JobStatus };
        return data.job || null;
      } catch (err) {
        logger.warn('Job status poll failed', err);
        return null;
      }
    },
    [endpoint],
  );

  const generateFromPrompt = useCallback(
    (args: GenerateFromPromptArgs): Promise<string> =>
      _post('/generate', {
        prompt: args.prompt,
        className: args.className || undefined,
        docCount: args.count,
        augment: Boolean(args.augment),
        threshold: args.threshold,
        scenario: args.scenario || undefined,
        testSetName: args.testSetName || undefined,
        testSetId: args.testSetId || undefined,
      }),
    [_post],
  );

  const generateFromConfig = useCallback(
    (args: GenerateFromConfigArgs): Promise<string> =>
      _post('/generate-from-config', {
        versionName: args.configVersion,
        className: args.className,
        docCount: args.count,
        augment: Boolean(args.augment),
        threshold: args.threshold,
        scenario: args.scenario || undefined,
        testSetName: args.testSetName || undefined,
        testSetId: args.testSetId || undefined,
      }),
    [_post],
  );

  const suggestScenario = useCallback(
    async (args: SuggestScenarioArgs): Promise<string[]> => {
      if (!endpoint) return [];
      try {
        const token = await _authToken();
        const resp = await fetch(`${endpoint.replace(/\/$/, '')}/suggest-scenario`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: token },
          body: JSON.stringify({ className: args.className, versionName: args.versionName, prompt: args.prompt }),
        });
        if (!resp.ok) return [];
        const data = (await resp.json().catch(() => ({}))) as { suggestions?: string[] };
        return data.suggestions || [];
      } catch (err) {
        logger.warn('Scenario suggestion failed', err);
        return [];
      }
    },
    [endpoint],
  );

  const getEstimate = useCallback(
    async (count: number, threshold: number): Promise<CostEstimate | null> => {
      if (!endpoint) return null;
      try {
        const token = await _authToken();
        const resp = await fetch(`${endpoint.replace(/\/$/, '')}/estimate-cost`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: token },
          body: JSON.stringify({ docCount: count, threshold }),
        });
        if (!resp.ok) return null;
        const data = (await resp.json().catch(() => ({}))) as { estimate?: CostEstimate };
        return data.estimate || null;
      } catch (err) {
        logger.warn('Cost estimate failed', err);
        return null;
      }
    },
    [endpoint],
  );

  return useMemo(
    () => ({
      available,
      featuresLoading,
      submitting,
      generateFromPrompt,
      generateFromConfig,
      getJobStatus,
      listActiveJobs,
      suggestScenario,
      getEstimate,
    }),
    [
      available,
      featuresLoading,
      submitting,
      generateFromPrompt,
      generateFromConfig,
      getJobStatus,
      listActiveJobs,
      suggestScenario,
      getEstimate,
    ],
  );
};

export default useSyntheticDataGenerator;
