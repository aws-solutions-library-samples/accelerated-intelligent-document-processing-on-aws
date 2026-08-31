// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Shared state + fields for synthetic test-set generation, rendered identically by
 * the create-test-set wizard and by the standalone modal that serves the Schema
 * Builder deep-link (`?generate=1&version=…&className=…`).
 *
 * Returns the fields plus validity, the live cost estimate and submit. Submit
 * lives here, not in the containers, so the two paths cannot diverge on what they
 * send to the generator.
 */

import React, { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  FormField,
  Input,
  RadioGroup,
  SegmentedControl,
  Select,
  SpaceBetween,
  Textarea,
  Tiles,
} from '@cloudscape-design/components';
import type { SelectProps } from '@cloudscape-design/components';
import useSyntheticDataGenerator from '../../hooks/use-synthetic-data-generator';
import type { CostEstimate } from '../../hooks/use-synthetic-data-generator';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import { generateClient } from '../../api/client-shim';
import { getTestSets } from '../../graphql/generated';
import { getErrorMessage } from '../../utils/errorUtils';

const client = generateClient();

const NAME_RE = /^[a-zA-Z0-9\s_-]+$/;
export const toTestSetId = (name: string): string => name.replace(/ /g, '-').toLowerCase();

export const MIN_COUNT = 1;
export const MAX_COUNT = 50;
export const FAST_THRESHOLD = 7;
export const QUALITY_THRESHOLD = 9;

// Document-class names from a fetched configuration profile. Configs arrive as AWSJSON
// strings; classes live under `.classes[]`, identified by `$id` /
// `x-aws-idp-document-type` as on the backend. A version's own classes win, and
// the default config is consulted only when the version defines none: otherwise
// default classes leak into a version that deliberately scopes down to a subset.
const _parse = (v: unknown): Record<string, unknown> => {
  if (typeof v === 'string' && v) {
    try {
      return JSON.parse(v) as Record<string, unknown>;
    } catch {
      return {};
    }
  }
  return (v as Record<string, unknown>) || {};
};

const _classNamesOf = (cfg: Record<string, unknown>): string[] => {
  const classes = (cfg.classes as Array<Record<string, unknown>> | undefined) || [];
  const names: string[] = [];
  for (const c of classes) {
    const id = (c['x-aws-idp-document-type'] || c.$id || c.title || c.name) as string | undefined;
    if (id) names.push(id);
  }
  return names;
};

const extractClassNames = (custom: unknown, def: unknown): string[] => {
  const customNames = _classNamesOf(_parse(custom));
  const names = customNames.length > 0 ? customNames : _classNamesOf(_parse(def));
  return Array.from(new Set(names)).sort();
};

const usd = (n: number): string => (Number.isFinite(n) ? `$${Math.max(1, Math.round(n))}` : '—');
const mins = (n: number): number => (Number.isFinite(n) ? Math.max(1, Math.ceil(n)) : 1);

export interface GenerateFormOptions {
  /** Only load data and estimate while the form is on screen. */
  active: boolean;
  initialMode?: 'prompt' | 'config';
  initialVersion?: string;
  initialClassName?: string;
}

export interface GenerateFormApi {
  /** The form fields, ready to drop into a Modal body or a Wizard step. */
  fields: React.JSX.Element;
  /** Summary rows for a review step (label/value pairs). */
  summary: { label: string; value: string }[];
  /** True when every required field is valid. */
  canSubmit: boolean;
  /** In-flight state from the generator hook. */
  submitting: boolean;
  /** Human-readable cost/time estimate, or null before it resolves. */
  estimateText: string | null;
  estimate: CostEstimate | null;
  error: string;
  /**
   * Start the job. Resolves with the job id, a display label and the resolved
   * destination test-set id, or null when the request failed — in which case the
   * reason is on `error`.
   */
  submit: () => Promise<{ jobId: string; label: string; testSetId: string } | null>;
  reset: () => void;
}

export const useGenerateSyntheticForm = ({
  active,
  initialMode,
  initialVersion,
  initialClassName,
}: GenerateFormOptions): GenerateFormApi => {
  const { submitting, generateFromPrompt, generateFromConfig, suggestScenario, getEstimate } = useSyntheticDataGenerator();
  const { versions, fetchVersion } = useConfigurationVersions();

  const [mode, setMode] = useState<'prompt' | 'config'>('prompt');
  const [prompt, setPrompt] = useState('');
  // Prompt mode has no version to derive classes from, so its class is free text;
  // config mode selects from the chosen version's classes.
  const [promptClassName, setPromptClassName] = useState('');
  const [count, setCount] = useState('5');
  const [augment, setAugment] = useState(false);
  const [threshold, setThreshold] = useState(FAST_THRESHOLD);
  const [scenario, setScenario] = useState('');
  const [suggesting, setSuggesting] = useState(false);
  const [scenarioSuggestions, setScenarioSuggestions] = useState<string[]>([]);
  const [estimate, setEstimate] = useState<CostEstimate | null>(null);
  const [selectedVersion, setSelectedVersion] = useState<SelectProps.Option | null>(null);
  const [selectedClass, setSelectedClass] = useState<SelectProps.Option | null>(null);
  const [classOptions, setClassOptions] = useState<SelectProps.Option[]>([]);
  const [classesLoading, setClassesLoading] = useState(false);
  const [error, setError] = useState('');

  // Destination: create a new test set (by name) or append to an existing one.
  const [destMode, setDestMode] = useState<'new' | 'existing'>('new');
  const [newTestSetName, setNewTestSetName] = useState('');
  const [existingTestSet, setExistingTestSet] = useState<SelectProps.Option | null>(null);
  const [testSetOptions, setTestSetOptions] = useState<SelectProps.Option[]>([]);
  const [allTestSetIds, setAllTestSetIds] = useState<Set<string>>(new Set());
  const [testSetsLoading, setTestSetsLoading] = useState(false);
  const [testSetsError, setTestSetsError] = useState(false);

  const versionOptions = useMemo<SelectProps.Option[]>(
    () => versions.map((v) => ({ label: v.versionName, value: v.versionName })),
    [versions],
  );

  // Seed initial values when opened from a deep-link.
  useEffect(() => {
    if (!active) return;
    if (initialMode) setMode(initialMode);
    if (initialVersion) setSelectedVersion({ label: initialVersion, value: initialVersion });
    // initialClassName is applied once the version's classes load (below).
  }, [active, initialMode, initialVersion]);

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    setTestSetsLoading(true);
    setTestSetsError(false);
    client
      .graphql({ query: getTestSets })
      .then((result) => {
        if (cancelled) return;
        const all = (result.data.getTestSets || []).filter((t): t is NonNullable<typeof t> => t != null);
        setAllTestSetIds(new Set(all.map((t) => t.id)));
        setTestSetOptions(all.filter((t) => t.status === 'COMPLETED').map((t) => ({ label: t.name, value: t.id })));
      })
      .catch(() => {
        if (!cancelled) {
          setTestSetOptions([]);
          setAllTestSetIds(new Set());
          setTestSetsError(true);
        }
      })
      .finally(() => {
        if (!cancelled) setTestSetsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [active]);

  // Depends on the version name string only: fetchVersion is not memoized, so
  // listing it here would re-fire this effect every render (an infinite
  // getConfigVersion loop).
  const versionName = selectedVersion?.value;
  useEffect(() => {
    if (!versionName) {
      setClassOptions([]);
      setSelectedClass(null);
      return;
    }
    let cancelled = false;
    setClassesLoading(true);
    setSelectedClass(null);
    fetchVersion(versionName)
      .then((cfg) => {
        if (cancelled) return;
        const names = extractClassNames(cfg.custom, cfg.default);
        setClassOptions(names.map((n) => ({ label: n, value: n })));
        if (initialClassName && names.includes(initialClassName)) {
          setSelectedClass({ label: initialClassName, value: initialClassName });
        }
      })
      .finally(() => {
        if (!cancelled) setClassesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [versionName]);

  const parsedCount = Number(count);
  const countValid = Number.isInteger(parsedCount) && parsedCount >= MIN_COUNT && parsedCount <= MAX_COUNT;

  // Live cost/time estimate, refreshed when count or quality change.
  useEffect(() => {
    if (!active || !countValid) {
      setEstimate(null);
      return;
    }
    let cancelled = false;
    getEstimate(parsedCount, threshold)
      .then((e) => {
        if (!cancelled) setEstimate(e);
      })
      .catch(() => {
        if (!cancelled) setEstimate(null);
      });
    return () => {
      cancelled = true;
    };
  }, [active, parsedCount, threshold, countValid]);

  const trimmedNewName = newTestSetName.trim();
  const nameFormatValid = trimmedNewName.length > 0 && trimmedNewName.length <= 50 && NAME_RE.test(trimmedNewName);
  // A new name whose derived id matches an existing set would silently append to
  // that set, so it is rejected instead.
  const newNameCollides = nameFormatValid && allTestSetIds.has(toTestSetId(trimmedNewName));
  const newNameValid = nameFormatValid && !newNameCollides;
  const destValid = destMode === 'new' ? newNameValid : Boolean(existingTestSet);
  const canSubmit =
    countValid && destValid && (mode === 'prompt' ? prompt.trim().length > 0 : Boolean(selectedVersion) && Boolean(selectedClass));

  const reset = () => {
    setMode('prompt');
    setPrompt('');
    setPromptClassName('');
    setCount('5');
    setAugment(false);
    setThreshold(FAST_THRESHOLD);
    setScenario('');
    setScenarioSuggestions([]);
    setEstimate(null);
    setSelectedVersion(null);
    setSelectedClass(null);
    setClassOptions([]);
    setDestMode('new');
    setNewTestSetName('');
    setExistingTestSet(null);
    setError('');
  };

  const handleSuggestScenario = async () => {
    setSuggesting(true);
    setScenarioSuggestions([]);
    try {
      const suggestions = await suggestScenario({
        className: mode === 'config' ? (selectedClass?.value as string) : promptClassName.trim() || undefined,
        versionName: mode === 'config' ? (selectedVersion?.value as string) : undefined,
        prompt: mode === 'prompt' ? prompt.trim() || undefined : undefined,
      });
      setScenarioSuggestions(suggestions);
      if (suggestions.length > 0 && !scenario.trim()) {
        setScenario(suggestions[0]);
      }
    } finally {
      setSuggesting(false);
    }
  };

  const submit = async (): Promise<{ jobId: string; label: string; testSetId: string } | null> => {
    setError('');
    const dest = destMode === 'new' ? { testSetName: trimmedNewName } : { testSetId: existingTestSet?.value as string };
    const resolvedId = destMode === 'new' ? toTestSetId(trimmedNewName) : (existingTestSet?.value as string);
    const label = destMode === 'new' ? trimmedNewName : (existingTestSet?.label as string) || resolvedId;
    try {
      const jobId =
        mode === 'prompt'
          ? await generateFromPrompt({
              prompt: prompt.trim(),
              count: parsedCount,
              className: promptClassName.trim() || undefined,
              augment,
              threshold,
              scenario: scenario.trim() || undefined,
              ...dest,
            })
          : await generateFromConfig({
              configVersion: selectedVersion?.value as string,
              className: selectedClass?.value as string,
              count: parsedCount,
              augment,
              threshold,
              scenario: scenario.trim() || undefined,
              ...dest,
            });
      reset();
      return { jobId, label, testSetId: resolvedId };
    } catch (err) {
      setError(getErrorMessage(err));
      return null;
    }
  };

  const estimateText = estimate
    ? `${usd(estimate.estimated_usd_low)}–${usd(estimate.estimated_usd_high)} · ~${mins(estimate.estimated_minutes_low)}–${mins(estimate.estimated_minutes_high)} min`
    : null;

  const summary: { label: string; value: string }[] = [
    { label: 'Source', value: mode === 'prompt' ? 'From a description' : 'From a configuration' },
    ...(mode === 'prompt'
      ? [{ label: 'Description', value: prompt.trim() || '—' }]
      : [
          { label: 'Configuration profile', value: (selectedVersion?.label as string) || '—' },
          { label: 'Document class', value: (selectedClass?.label as string) || '—' },
        ]),
    {
      label: 'Destination',
      value: destMode === 'new' ? `New test set "${trimmedNewName || '—'}"` : `Add to "${existingTestSet?.label ?? '—'}"`,
    },
    { label: 'Documents', value: countValid ? String(parsedCount) : '—' },
    { label: 'Quality', value: threshold === QUALITY_THRESHOLD ? 'Higher quality' : 'Faster' },
    { label: 'Scan/fax effects', value: augment ? 'On' : 'Off' },
    { label: 'Estimated cost and time', value: estimateText ?? 'calculating…' },
  ];

  const fields = (
    <SpaceBetween size="m">
      {/* Tiles, not Tabs: mutually exclusive modes read as a decision, and this
          matches the wizard's source step. */}
      <FormField label="What should the generator base documents on?" stretch>
        <Tiles
          value={mode}
          onChange={({ detail }) => setMode(detail.value as 'prompt' | 'config')}
          items={[
            {
              value: 'prompt',
              label: 'From a description',
              description: 'Describe the document type in words. Best when you have no configuration for it yet.',
            },
            {
              value: 'config',
              label: 'From a configuration',
              description: "Use an existing configuration profile's document class, so labels match your extraction schema.",
            },
          ]}
        />
      </FormField>

      {mode === 'prompt' ? (
        <>
          <FormField label="Document type description" description="Describe the document type and the fields to extract.">
            <Textarea
              value={prompt}
              onChange={({ detail }) => setPrompt(detail.value)}
              placeholder="e.g. Employee payslips with employee name, pay period, gross pay, and net pay"
              rows={3}
            />
          </FormField>
          <FormField label="Document class name — optional" description="Defaults to a name inferred from the description.">
            <Input value={promptClassName} onChange={({ detail }) => setPromptClassName(detail.value)} placeholder="Payslip" />
          </FormField>
        </>
      ) : (
        <>
          <FormField label="Configuration profile" description="The profile whose document class defines the fields to generate.">
            <Select
              selectedOption={selectedVersion}
              onChange={({ detail }) => setSelectedVersion(detail.selectedOption)}
              options={versionOptions}
              placeholder="Select a configuration profile"
              filteringType="auto"
            />
          </FormField>
          <FormField label="Document class" description="Which class from that profile to generate documents for.">
            <Select
              selectedOption={selectedClass}
              onChange={({ detail }) => setSelectedClass(detail.selectedOption)}
              options={classOptions}
              placeholder={selectedVersion ? 'Select a document class' : 'Select a configuration profile first'}
              disabled={!selectedVersion}
              empty="No document classes in this profile"
              statusType={classesLoading ? 'loading' : 'finished'}
              loadingText="Loading document classes"
              filteringType="auto"
            />
          </FormField>
        </>
      )}

      <FormField
        label="Scenario — optional"
        description="A high-level theme the generator diversifies into distinct documents (e.g. small-business owners in retail, or travel-heavy expense reports)."
        secondaryControl={
          <Button iconName="gen-ai" loading={suggesting} onClick={handleSuggestScenario}>
            Suggest
          </Button>
        }
      >
        <Textarea
          value={scenario}
          onChange={({ detail }) => setScenario(detail.value)}
          placeholder="Leave blank for a general mix, or describe a theme to focus the documents."
          rows={2}
        />
      </FormField>
      {scenarioSuggestions.length > 1 && (
        <SpaceBetween size="xs">
          <Box variant="small" color="text-body-secondary">
            Suggestions (click to use):
          </Box>
          <SpaceBetween size="xxs">
            {scenarioSuggestions.map((s) => (
              <Button key={s} variant="inline-link" onClick={() => setScenario(s)}>
                {s}
              </Button>
            ))}
          </SpaceBetween>
        </SpaceBetween>
      )}

      <FormField label="Test set destination" description="Create a new test set, or add the generated documents to an existing one.">
        <SpaceBetween size="xs">
          <RadioGroup
            value={destMode}
            onChange={({ detail }) => setDestMode(detail.value as 'new' | 'existing')}
            items={[
              { value: 'new', label: 'Create new test set' },
              {
                value: 'existing',
                label: 'Add to existing test set',
                disabled: testSetsLoading || testSetOptions.length === 0,
              },
            ]}
          />
          {testSetsError && (
            <Alert type="warning">Could not load existing test sets. You can still create a new one; retry by reopening this form.</Alert>
          )}
          {destMode === 'new' ? (
            <FormField
              errorText={
                newTestSetName && newNameCollides
                  ? 'A test set with this name already exists. Choose a different name, or use "Add to existing".'
                  : newTestSetName && !nameFormatValid
                    ? 'Letters, numbers, spaces, hyphens, and underscores only (max 50 chars)'
                    : undefined
              }
            >
              <Input
                value={newTestSetName}
                onChange={({ detail }) => setNewTestSetName(detail.value)}
                placeholder="New test set name (e.g. W2 Synthetic)"
              />
            </FormField>
          ) : (
            <Select
              selectedOption={existingTestSet}
              onChange={({ detail }) => setExistingTestSet(detail.selectedOption)}
              options={testSetOptions}
              placeholder="Select a test set"
              empty="No completed test sets"
              statusType={testSetsLoading ? 'loading' : 'finished'}
              loadingText="Loading test sets"
              filteringType="auto"
            />
          )}
        </SpaceBetween>
      </FormField>

      <FormField
        label="Number of documents"
        description={`Between ${MIN_COUNT} and ${MAX_COUNT}.`}
        errorText={count !== '' && !countValid ? `Enter a whole number from ${MIN_COUNT} to ${MAX_COUNT}` : undefined}
      >
        <Input type="number" value={count} onChange={({ detail }) => setCount(detail.value)} />
      </FormField>

      <FormField label="Quality" description="Higher quality runs more generation/critique passes — slower and more expensive.">
        <SegmentedControl
          selectedId={String(threshold)}
          onChange={({ detail }) => setThreshold(Number(detail.selectedId))}
          options={[
            { id: String(FAST_THRESHOLD), text: 'Faster' },
            { id: String(QUALITY_THRESHOLD), text: 'Higher quality' },
          ]}
        />
      </FormField>

      <FormField
        label="Image augmentation"
        description="Ages documents with scan/fax/photocopy artifacts (noise, skew, ink bleed) to test how your pipeline handles low-quality inputs. Leave off for clean, digital-native documents; adds time and cost."
      >
        <Checkbox checked={augment} onChange={({ detail }) => setAugment(detail.checked)}>
          Apply scan/fax-style effects
        </Checkbox>
      </FormField>

      <Alert type="info">
        Generation uses Amazon Bedrock and incurs cost proportional to the document count and quality.
        {estimateText ? ` Estimated ${estimateText}.` : ''}
      </Alert>

      {error && (
        <Alert type="error" header="Generation failed">
          {error}
        </Alert>
      )}
    </SpaceBetween>
  );

  return { fields, summary, canSubmit, submitting, estimateText, estimate, error, submit, reset };
};

export default useGenerateSyntheticForm;
