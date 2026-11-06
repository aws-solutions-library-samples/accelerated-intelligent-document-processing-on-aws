// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Config Pairing wizard — the primary UX for enabling PII redaction.
 *
 * An admin picks an EXISTING working configuration profile and a mode; the wizard
 * derives a matched PAIR by cloning that base:
 *   - initiating profile  (<base>__pii_stop | <base>__pii_go):
 *       a copy of the base + a `preprocessing` block (mode, model, redaction,
 *       companion pointer) and this feature's preprocessing.preHook entry.
 *   - companion profile   (<base>__pii_target):
 *       a copy of the base with NO preprocessing block/hook — the redacted copy
 *       is processed under this profile.
 *
 * Both are written NON-ACTIVE via the host updateConfiguration mutation. The
 * admin then activates the initiating profile (optionally in one click here).
 *
 * This keeps the customer's real extraction settings authoritative and layers
 * redaction on top, instead of forking a whole config they must keep in sync.
 */

import React, { useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Container,
  FormField,
  Header,
  Select,
  SpaceBetween,
  Spinner,
  StatusIndicator,
} from '@cloudscape-design/components';

import {
  activateVersion,
  getConfig,
  graphqlErrorMessage,
  listConfigVersions,
  saveConfigVersion,
  type ConfigVersion,
} from './hostGraphql';
const HOOK_FEATURE_ID = 'pii-anonymizer';

const MODE_OPTIONS = [
  {
    value: 'redactcopy_and_stop',
    label: 'Redact copy and stop',
    description:
      'Write the de-identified copy, then DELETE the original entirely so only the redacted version remains — the processing pipeline sees no PII (the detection pass itself does see the original).',
  },
  {
    value: 'redactcopy_and_continue',
    label: 'Redact copy and continue',
    description:
      'Write the de-identified copy AND keep processing the original as a separate document. Scope each to different users via allowedConfigVersions RBAC.',
  },
];

const MODEL_OPTIONS = [
  { value: 'us.anthropic.claude-haiku-4-5-20251001-v1:0', label: 'Claude Haiku 4.5 (recommended — large output, strong recall)' },
  { value: 'us.anthropic.claude-sonnet-5', label: 'Claude Sonnet 5 (best quality, highest cost)' },
  { value: 'us.amazon.nova-lite-v1:0', label: 'Amazon Nova Lite (cheapest — may truncate on dense docs)' },
];

const REDACTION_OPTIONS = [
  { value: 'synthetic', label: 'Synthetic (structure-preserving fake values — keeps extraction accuracy)' },
  { value: 'blackout', label: 'Blackout (solid boxes / [REDACTED])' },
];

interface Opt {
  value: string;
  label: string;
  description?: string;
}

const ConfigPairingView: React.FC<{ enabled: boolean; hookFunctionArn: string | null }> = ({
  enabled,
  hookFunctionArn,
}) => {
  const [versions, setVersions] = useState<ConfigVersion[]>([]);
  const [loading, setLoading] = useState(false);
  const [base, setBase] = useState<Opt | null>(null);
  const [mode, setMode] = useState<Opt>(MODE_OPTIONS[0]);
  const [model, setModel] = useState<Opt>(MODEL_OPTIONS[0]);
  const [redaction, setRedaction] = useState<Opt>(REDACTION_OPTIONS[0]);
  const [storeMapping, setStoreMapping] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ initiating: string; companion: string } | null>(
    null,
  );

  const refresh = React.useCallback(() => {
    if (!enabled) return;
    setLoading(true);
    setError(null);
    listConfigVersions()
      .then((v) => setVersions(v))
      .catch((e) => setError(graphqlErrorMessage(e)))
      .finally(() => setLoading(false));
  }, [enabled]);

  useEffect(refresh, [refresh]);

  // Short, intuitive suffixes (profile names are capped at 50 chars):
  //   __pii_stop    initiating, redactcopy_and_stop
  //   __pii_go      initiating, redactcopy_and_continue
  //   __pii_target  companion (processes the redacted copy)
  const suffix = mode.value === 'redactcopy_and_stop' ? '__pii_stop' : '__pii_go';
  // Truncate the base so the full name (with the longest suffix) stays <= 50.
  const maxBase = 50 - '__pii_target'.length;
  const baseTrunc = base ? base.value.slice(0, maxBase) : '';
  const initiatingName = base ? `${baseTrunc}${suffix}` : '';
  const companionName = base ? `${baseTrunc}__pii_target` : '';

  async function createPair() {
    if (!base) return;
    if (!hookFunctionArn) {
      setError(
        'The feature API did not report the hook function ARN. Reload the page; if it persists, check the feature stack deployed cleanly.',
      );
      return;
    }
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      // Fetch the base config once; both derived versions start from it.
      const baseConfig = await getConfig(base.value);

      // Companion: base minus any preprocessing block/hook (redacted copy is
      // processed normally under this version).
      const companion = { ...baseConfig };
      delete (companion as Record<string, unknown>).preprocessing;
      await saveConfigVersion(
        companionName,
        companion,
        `PII companion of ${base.value} — processes the redacted copy (no preprocessing hook).`,
      );

      // Initiating: base + a GENERIC preprocessing hook (single, flat: arn/args
      // live directly on the preprocessing section). The hook reads all its PII
      // settings from `args` (keeping the step reusable for any job). onError=fail
      // for stop mode (fail closed — never leak PII), else continue. The hook ARN
      // comes from the feature API's /config (GET).
      const args = [
        { key: 'mode', value: mode.value },
        { key: 'companion_config_version', value: companionName },
        { key: 'model_id', value: model.value },
        {
          key: 'model_provider',
          value: model.value.includes('nova') ? 'amazon' : 'anthropic',
        },
        { key: 'redaction_mode', value: redaction.value },
        { key: 'store_mapping', value: storeMapping ? 'true' : 'false' },
      ];
      const initiating = {
        ...baseConfig,
        preprocessing: {
          enabled: true,
          featureId: HOOK_FEATURE_ID,
          arn: hookFunctionArn,
          onError: mode.value === 'redactcopy_and_stop' ? 'fail' : 'continue',
          args,
        },
      };
      await saveConfigVersion(
        initiatingName,
        initiating,
        `PII ${mode.label} of ${base.value} — redacts before processing.`,
      );

      setResult({ initiating: initiatingName, companion: companionName });
      refresh();
    } catch (e) {
      setError(graphqlErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function activate() {
    if (!result) return;
    setBusy(true);
    setError(null);
    try {
      await activateVersion(result.initiating);
      refresh();
    } catch (e) {
      setError(graphqlErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  if (!enabled) {
    return (
      <Alert type="info" header="Subscription not active">
        Activate the subscription to configure PII redaction.
      </Alert>
    );
  }

  const versionOptions: Opt[] = versions
    // Don't offer feature-derived versions as a base (avoid nesting).
    .filter((v) => !/__pii_(stop|go|target)$/.test(v.versionName))
    .map((v) => ({
      value: v.versionName,
      label: v.versionName + (v.isActive ? ' (active)' : ''),
      description: v.description,
    }));

  return (
    <Container
      header={
        <Header
          variant="h2"
          description="Create a matched pair of configuration profiles that redact PII before processing, cloned from one of your existing profiles."
          actions={
            <Button
              iconName="refresh"
              onClick={refresh}
              disabled={busy}
              ariaLabel="Refresh configuration profiles"
            />
          }
        >
          Config Pairing
        </Header>
      }
    >
      <SpaceBetween size="l">
        {error && (
          <Alert type="error" header="Could not complete" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        )}

        {loading ? (
          <Spinner />
        ) : (
          <SpaceBetween size="l">
            <FormField
              label="Base configuration profile"
              description="Your existing working config. The pair is cloned from this so your extraction settings are preserved."
            >
              <Select
                selectedOption={base}
                onChange={({ detail }) => setBase(detail.selectedOption as Opt)}
                options={versionOptions}
                placeholder="Choose a configuration profile to protect"
                empty="No eligible configuration profiles found"
              />
            </FormField>

            <FormField label="Mode">
              <Select
                selectedOption={mode}
                onChange={({ detail }) => setMode(detail.selectedOption as Opt)}
                options={MODE_OPTIONS}
              />
            </FormField>

            <FormField
              label="PII detection model"
              description="Runs a detection pass per page before processing. Claude Haiku is the default (large output budget for dense forms)."
            >
              <Select
                selectedOption={model}
                onChange={({ detail }) => setModel(detail.selectedOption as Opt)}
                options={MODEL_OPTIONS}
              />
            </FormField>

            <FormField label="Redaction style">
              <Select
                selectedOption={redaction}
                onChange={({ detail }) => setRedaction(detail.selectedOption as Opt)}
                options={REDACTION_OPTIONS}
              />
            </FormField>

            <FormField
              label="Store PII mapping (sensitive)"
              description="When synthetic redaction is used, save the original→fake value map so it can be viewed in the Redaction Report. The mapping IS real PII (a re-identification key); it is encrypted and shown only to users with access to the original's configuration profile. Off by default."
            >
              <Checkbox
                checked={storeMapping}
                onChange={({ detail }) => setStoreMapping(detail.checked)}
                disabled={redaction.value !== 'synthetic'}
              >
                Store original→synthetic mapping for this pair
              </Checkbox>
            </FormField>

            {base && (
              <Box variant="p" color="text-body-secondary">
                Will create <b>{initiatingName}</b> (initiating, with redaction) and{' '}
                <b>{companionName}</b> (companion, processes the redacted copy). Both
                start non-active.
              </Box>
            )}

            <SpaceBetween size="xs" direction="horizontal">
              <Button variant="primary" onClick={createPair} loading={busy} disabled={!base}>
                Create config pair
              </Button>
            </SpaceBetween>

            {result && (
              <Alert type="success" header="Config pair created (non-active)">
                <SpaceBetween size="s">
                  <div>
                    <StatusIndicator type="success" /> Initiating:{' '}
                    <b>{result.initiating}</b>
                    <br />
                    <StatusIndicator type="success" /> Companion:{' '}
                    <b>{result.companion}</b>
                  </div>
                  <Box>
                    Activate the initiating profile to start redacting. Documents
                    uploaded under it are redacted first; the redacted copy is
                    processed under the companion profile.
                  </Box>
                  <Button onClick={activate} loading={busy}>
                    Activate {result.initiating}
                  </Button>
                </SpaceBetween>
              </Alert>
            )}
          </SpaceBetween>
        )}
      </SpaceBetween>
    </Container>
  );
};

export default ConfigPairingView;
