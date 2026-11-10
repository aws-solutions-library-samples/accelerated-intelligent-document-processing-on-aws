// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Bulk Skip All Reviews: selected documents, or a configuration profile and a
// submitted-date range. A dry run shows what will happen first; Confirm runs it
// inline (results shown at once) or as a background job (Home › Jobs).
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Box, Checkbox, FormField, Select, SpaceBetween, Spinner } from '@cloudscape-design/components';
import type { SelectProps } from '@cloudscape-design/components';

import DateRangeFields, { lastDaysRange, toIsoRange, validateDateRange } from '../common/DateRangeFields';
import type { DateRangeValue } from '../common/DateRangeFields';
import ReasonModal from '../common/ReasonModal';
import { SKIP_ALL_REASONS } from '../common/review-status';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import { getErrorMessage } from '../../utils/errorUtils';
import type { BulkSkipAllReviewsResult } from '../../graphql/generated/schema-types';
import { dryRunBulkSkip, runBulkSkip } from './bulk-review-api';
import type { BulkSkipScope } from './bulk-review-api';
import OutcomeCounts from './OutcomeCounts';

export type BulkSkipMode = 'SELECTED' | 'PROFILE';

interface BulkSkipModalProps {
  mode: BulkSkipMode;
  objectKeys?: string[];
  onDismiss: () => void;
  /** An inline run finished: its results. */
  onFinished: (result: BulkSkipAllReviewsResult) => void;
  /** A background job was created. */
  onJobStarted: (jobId: string) => void;
}

const ProfileSelect = ({
  value,
  onChange,
}: {
  value: SelectProps.Option | null;
  onChange: (option: SelectProps.Option) => void;
}): React.JSX.Element => {
  const { getVersionOptions, loading } = useConfigurationVersions();
  return (
    <FormField label="Configuration profile" description="Documents processed with this profile.">
      <Select
        selectedOption={value}
        onChange={({ detail }) => onChange(detail.selectedOption)}
        options={getVersionOptions()}
        statusType={loading ? 'loading' : 'finished'}
        loadingText="Loading profiles..."
        placeholder="Choose a configuration profile"
        empty="No configuration profiles in your scope"
        ariaLabel="Configuration profile"
      />
    </FormField>
  );
};

const BulkSkipModal = ({ mode, objectKeys = [], onDismiss, onFinished, onJobStarted }: BulkSkipModalProps): React.JSX.Element => {
  const [profile, setProfile] = useState<SelectProps.Option | null>(null);
  const [range, setRange] = useState<DateRangeValue>(() => lastDaysRange(7));
  const [dryRun, setDryRun] = useState<BulkSkipAllReviewsResult | null>(null);
  const [counting, setCounting] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resubmit, setResubmit] = useState(true);
  const request = useRef(0);

  const rangeProblem = mode === 'PROFILE' ? validateDateRange(range) : null;
  const scope = useMemo<BulkSkipScope | null>(() => {
    if (mode === 'SELECTED') return objectKeys.length > 0 ? { mode, objectKeys } : null;
    if (!profile?.value || rangeProblem) return null;
    const { startDateTime, endDateTime } = toIsoRange(range);
    return { mode, profile: profile.value, submittedFrom: startDateTime, submittedTo: endDateTime };
  }, [mode, objectKeys, profile, range, rangeProblem]);

  useEffect(() => {
    if (!scope) {
      setDryRun(null);
      return undefined;
    }
    const id = ++request.current;
    setCounting(true);
    setError(null);
    const timer = setTimeout(
      () => {
        dryRunBulkSkip(scope)
          .then((result) => {
            if (id === request.current) setDryRun(result);
          })
          .catch((e) => {
            if (id === request.current) setError(`Could not count pending reviews: ${getErrorMessage(e)}`);
          })
          .finally(() => {
            if (id === request.current) setCounting(false);
          });
      },
      mode === 'PROFILE' ? 500 : 0,
    );
    return () => clearTimeout(timer);
  }, [scope]);

  const confirm = async (reason: string, note: string | undefined) => {
    if (!scope) return;
    setRunning(true);
    setError(null);
    try {
      const result = await runBulkSkip(scope, { reason, note, resubmit });
      if (result.runMode === 'ASYNC' && result.jobId) onJobStarted(result.jobId);
      else onFinished(result);
    } catch (e) {
      setError(`Skip All Reviews failed: ${getErrorMessage(e)}`);
    } finally {
      setRunning(false);
    }
  };

  const eligible = dryRun?.eligibleCount ?? 0;
  const disabledReason =
    mode === 'PROFILE' && !profile
      ? 'Choose a configuration profile'
      : rangeProblem
        ? rangeProblem
        : counting
          ? 'Counting pending reviews…'
          : error && !dryRun
            ? 'The pending reviews could not be counted'
            : dryRun && eligible === 0
              ? 'No pending production reviews to skip'
              : null;

  return (
    <ReasonModal
      visible
      header={mode === 'PROFILE' ? 'Skip All Reviews by profile' : `Skip All Reviews (${objectKeys.length} selected)`}
      reasons={SKIP_ALL_REASONS}
      confirmLabel="Skip All Reviews"
      size="medium"
      loading={running}
      dismissDisabled={running}
      confirmDisabledReason={disabledReason}
      onDismiss={onDismiss}
      onConfirm={(reason, note) => void confirm(reason, note)}
      extra={
        <Checkbox
          checked={resubmit}
          onChange={({ detail }) => setResubmit(detail.checked)}
          description="Applies to reviews outside a review queue; queue items always finish through their finalize run."
        >
          Resubmit for downstream processing (Summarization, Evaluation, post-processing hook)
        </Checkbox>
      }
    >
      <SpaceBetween size="m">
        {mode === 'PROFILE' && (
          <>
            <ProfileSelect value={profile} onChange={setProfile} />
            <FormField label="Submitted between (UTC)" errorText={rangeProblem ?? undefined}>
              <DateRangeFields value={range} onChange={setRange} />
            </FormField>
          </>
        )}
        {error && <Alert type="error">{error}</Alert>}
        {counting && (
          <Box>
            <Spinner /> Counting pending reviews…
          </Box>
        )}
        {!counting && dryRun && (
          <SpaceBetween size="s">
            {eligible === 0 ? (
              <Box>
                {mode === 'PROFILE'
                  ? 'No pending production reviews for this profile and range. Try a wider range or another profile.'
                  : 'None of the selected documents has a pending production review.'}
              </Box>
            ) : (
              <Box>
                <b>{`${eligible.toLocaleString()}${dryRun.approximate ? '+' : ''}`}</b> pending review
                {eligible === 1 ? '' : 's'} will be skipped.{' '}
                {dryRun.runMode === 'SYNC' ? 'Runs now.' : 'Runs as a background job (results in Home › Jobs).'}
              </Box>
            )}
            <OutcomeCounts counts={dryRun.counts} preview />
            {dryRun.limitReached && (
              <Alert type="warning">More than 10,000 documents match; only the first 10,000 are processed. Narrow the date range.</Alert>
            )}
            <Alert type="warning">
              Skipped reviews are finished without anyone checking them; each is recorded with your reason in the document&apos;s review
              history.
            </Alert>
          </SpaceBetween>
        )}
      </SpaceBetween>
    </ReasonModal>
  );
};

export default BulkSkipModal;
