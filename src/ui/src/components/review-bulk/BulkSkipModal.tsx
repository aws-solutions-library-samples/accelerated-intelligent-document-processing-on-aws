// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Bulk Skip All Reviews: selected documents, or a configuration profile and a
// submitted-date range. A dry run shows what will happen first; Confirm runs it
// inline (results shown at once) or as a background job (Home › Jobs).
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Box, Checkbox, FormField, Select, SpaceBetween, Spinner } from '@cloudscape-design/components';
import type { SelectProps } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type MessageTag, type NoMessageValues } from 'react-intl';

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

const messages = defineMessages<{
  profileLabel: NoMessageValues;
  profileDescription: NoMessageValues;
  profilesLoading: NoMessageValues;
  profilePlaceholder: NoMessageValues;
  profilesEmpty: NoMessageValues;
  countFailed: { readonly error: string };
  runFailed: { readonly error: string };
  chooseProfile: NoMessageValues;
  counting: NoMessageValues;
  notCounted: NoMessageValues;
  nothingToSkip: NoMessageValues;
  headerProfile: NoMessageValues;
  headerSelected: { readonly count: number };
  confirm: NoMessageValues;
  resubmitDescription: NoMessageValues;
  resubmit: NoMessageValues;
  submittedBetween: NoMessageValues;
  noneForProfile: NoMessageValues;
  noneSelected: NoMessageValues;
  willBeSkipped: { readonly count: number; readonly approximate: string; readonly b: MessageTag };
  runsNow: NoMessageValues;
  runsAsJob: NoMessageValues;
  limitReached: NoMessageValues;
  skippedWarning: NoMessageValues;
}>({
  profileLabel: {
    id: 'reviewBulk.bulkSkipModal.profileLabel',
    defaultMessage: 'Configuration profile',
    description: 'Label (and screen-reader label) of the configuration profile select in the bulk Skip All Reviews by profile dialog.',
  },
  profileDescription: {
    id: 'reviewBulk.bulkSkipModal.profileDescription',
    defaultMessage: 'Documents processed with this profile.',
    description: 'Description under the configuration profile select of bulk Skip All Reviews by profile.',
  },
  profilesLoading: {
    id: 'reviewBulk.bulkSkipModal.profilesLoading',
    defaultMessage: 'Loading profiles...',
    description: 'Loading text of the configuration profile select of bulk Skip All Reviews by profile.',
  },
  profilePlaceholder: {
    id: 'reviewBulk.bulkSkipModal.profilePlaceholder',
    defaultMessage: 'Choose a configuration profile',
    description: 'Placeholder of the configuration profile select of bulk Skip All Reviews by profile.',
  },
  profilesEmpty: {
    id: 'reviewBulk.bulkSkipModal.profilesEmpty',
    defaultMessage: 'No configuration profiles in your scope',
    description: 'Shown in the configuration profile select when the user may use no profile.',
  },
  countFailed: {
    id: 'reviewBulk.bulkSkipModal.countFailed',
    defaultMessage: 'Could not count pending reviews: {error}',
    description: 'Error when the dry run of bulk Skip All Reviews fails. {error} is the error message from the server.',
  },
  runFailed: {
    id: 'reviewBulk.bulkSkipModal.runFailed',
    defaultMessage: 'Skip All Reviews failed: {error}',
    description: 'Error when a bulk Skip All Reviews run fails. {error} is the error message from the server.',
  },
  chooseProfile: {
    id: 'reviewBulk.bulkSkipModal.chooseProfile',
    defaultMessage: 'Choose a configuration profile',
    description: 'Why the Skip All Reviews confirm button is disabled: no configuration profile is chosen yet.',
  },
  counting: {
    id: 'reviewBulk.bulkSkipModal.counting',
    defaultMessage: 'Counting pending reviews…',
    description:
      'Shown (and used as the disabled reason of the confirm button) while the dry run of bulk Skip All Reviews counts the pending reviews.',
  },
  notCounted: {
    id: 'reviewBulk.bulkSkipModal.notCounted',
    defaultMessage: 'The pending reviews could not be counted',
    description: 'Why the Skip All Reviews confirm button is disabled: the dry run failed.',
  },
  nothingToSkip: {
    id: 'reviewBulk.bulkSkipModal.nothingToSkip',
    defaultMessage: 'No pending production reviews to skip',
    description: 'Why the Skip All Reviews confirm button is disabled: the dry run found no review to skip.',
  },
  headerProfile: {
    id: 'reviewBulk.bulkSkipModal.headerProfile',
    defaultMessage: 'Skip All Reviews by profile',
    description: 'Title of the bulk Skip All Reviews dialog that chooses documents by configuration profile and submitted dates.',
  },
  headerSelected: {
    id: 'reviewBulk.bulkSkipModal.headerSelected',
    defaultMessage: 'Skip All Reviews ({count} selected)',
    description:
      'Title of the bulk Skip All Reviews dialog for selected documents. {count} is the number of selected documents it applies to.',
  },
  confirm: {
    id: 'reviewBulk.bulkSkipModal.confirm',
    defaultMessage: 'Skip All Reviews',
    description: 'Confirm button of the bulk Skip All Reviews dialog.',
  },
  resubmitDescription: {
    id: 'reviewBulk.bulkSkipModal.resubmitDescription',
    defaultMessage: 'Applies to reviews outside a review queue; queue items always finish through their finalize run.',
    description: 'Description of the Resubmit checkbox of bulk Skip All Reviews.',
  },
  resubmit: {
    id: 'reviewBulk.bulkSkipModal.resubmit',
    defaultMessage: 'Resubmit for downstream processing (Summarization, Evaluation, post-processing hook)',
    description: 'Checkbox of bulk Skip All Reviews: run the downstream steps (named in parentheses) after skipping.',
  },
  submittedBetween: {
    id: 'reviewBulk.bulkSkipModal.submittedBetween',
    defaultMessage: 'Submitted between (UTC)',
    description: 'Label of the submitted-date range of bulk Skip All Reviews by profile; times are in UTC.',
  },
  noneForProfile: {
    id: 'reviewBulk.bulkSkipModal.noneForProfile',
    defaultMessage: 'No pending production reviews for this profile and range. Try a wider range or another profile.',
    description: 'Dry-run result of bulk Skip All Reviews by profile when nothing would be skipped.',
  },
  noneSelected: {
    id: 'reviewBulk.bulkSkipModal.noneSelected',
    defaultMessage: 'None of the selected documents has a pending production review.',
    description: 'Dry-run result of bulk Skip All Reviews for selected documents when nothing would be skipped.',
  },
  willBeSkipped: {
    id: 'reviewBulk.bulkSkipModal.willBeSkipped',
    defaultMessage:
      '{count, plural, one {<b>#{approximate}</b> pending review} other {<b>#{approximate}</b> pending reviews}} will be skipped.',
    description:
      'Dry-run result of bulk Skip All Reviews. {count} is the number of reviews that will be skipped, shown in bold; {approximate} is "+" when the count is a lower bound, otherwise empty.',
  },
  runsNow: {
    id: 'reviewBulk.bulkSkipModal.runsNow',
    defaultMessage: 'Runs now.',
    description: 'Dry-run result of bulk Skip All Reviews: the run is small enough to happen at once.',
  },
  runsAsJob: {
    id: 'reviewBulk.bulkSkipModal.runsAsJob',
    defaultMessage: 'Runs as a background job (results in Home › Jobs).',
    description:
      'Dry-run result of bulk Skip All Reviews: the run is large and becomes a background job. "Home › Jobs" is the navigation path of the Jobs page.',
  },
  limitReached: {
    id: 'reviewBulk.bulkSkipModal.limitReached',
    defaultMessage: 'More than 10,000 documents match; only the first 10,000 are processed. Narrow the date range.',
    description: 'Warning in the bulk Skip All Reviews dry run when the match limit of 10,000 documents is reached.',
  },
  skippedWarning: {
    id: 'reviewBulk.bulkSkipModal.skippedWarning',
    defaultMessage:
      "Skipped reviews are finished without anyone checking them; each is recorded with your reason in the document's review history.",
    description: 'Warning in the bulk Skip All Reviews dialog about what skipping a review means.',
  },
});

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
  const intl = useIntl();
  const { getVersionOptions, loading } = useConfigurationVersions();
  return (
    <FormField label={intl.formatMessage(messages.profileLabel)} description={intl.formatMessage(messages.profileDescription)}>
      <Select
        selectedOption={value}
        onChange={({ detail }) => onChange(detail.selectedOption)}
        options={getVersionOptions()}
        statusType={loading ? 'loading' : 'finished'}
        loadingText={intl.formatMessage(messages.profilesLoading)}
        placeholder={intl.formatMessage(messages.profilePlaceholder)}
        empty={intl.formatMessage(messages.profilesEmpty)}
        ariaLabel={intl.formatMessage(messages.profileLabel)}
      />
    </FormField>
  );
};

const BulkSkipModal = ({ mode, objectKeys = [], onDismiss, onFinished, onJobStarted }: BulkSkipModalProps): React.JSX.Element => {
  const intl = useIntl();
  const [profile, setProfile] = useState<SelectProps.Option | null>(null);
  const [range, setRange] = useState<DateRangeValue>(() => lastDaysRange(7));
  const [dryRun, setDryRun] = useState<BulkSkipAllReviewsResult | null>(null);
  const [counting, setCounting] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resubmit, setResubmit] = useState(true);
  const request = useRef(0);

  const rangeProblem = mode === 'PROFILE' ? validateDateRange(range, intl) : null;
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
            if (id === request.current) setError(intl.formatMessage(messages.countFailed, { error: getErrorMessage(e) }));
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
      setError(intl.formatMessage(messages.runFailed, { error: getErrorMessage(e) }));
    } finally {
      setRunning(false);
    }
  };

  const eligible = dryRun?.eligibleCount ?? 0;
  const disabledReason =
    mode === 'PROFILE' && !profile
      ? intl.formatMessage(messages.chooseProfile)
      : rangeProblem
        ? rangeProblem
        : counting
          ? intl.formatMessage(messages.counting)
          : error && !dryRun
            ? intl.formatMessage(messages.notCounted)
            : dryRun && eligible === 0
              ? intl.formatMessage(messages.nothingToSkip)
              : null;

  return (
    <ReasonModal
      visible
      header={
        mode === 'PROFILE'
          ? intl.formatMessage(messages.headerProfile)
          : intl.formatMessage(messages.headerSelected, { count: objectKeys.length })
      }
      reasons={SKIP_ALL_REASONS}
      confirmLabel={intl.formatMessage(messages.confirm)}
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
          description={intl.formatMessage(messages.resubmitDescription)}
        >
          <FormattedMessage {...messages.resubmit} />
        </Checkbox>
      }
    >
      <SpaceBetween size="m">
        {mode === 'PROFILE' && (
          <>
            <ProfileSelect value={profile} onChange={setProfile} />
            <FormField label={intl.formatMessage(messages.submittedBetween)} errorText={rangeProblem ?? undefined}>
              <DateRangeFields value={range} onChange={setRange} />
            </FormField>
          </>
        )}
        {error && <Alert type="error">{error}</Alert>}
        {counting && (
          <Box>
            <Spinner /> <FormattedMessage {...messages.counting} />
          </Box>
        )}
        {!counting && dryRun && (
          <SpaceBetween size="s">
            {eligible === 0 ? (
              <Box>
                <FormattedMessage {...(mode === 'PROFILE' ? messages.noneForProfile : messages.noneSelected)} />
              </Box>
            ) : (
              <Box>
                <FormattedMessage
                  {...messages.willBeSkipped}
                  values={{ count: eligible, approximate: dryRun.approximate ? '+' : '', b: (chunks) => <b>{chunks}</b> }}
                />{' '}
                <FormattedMessage {...(dryRun.runMode === 'SYNC' ? messages.runsNow : messages.runsAsJob)} />
              </Box>
            )}
            <OutcomeCounts counts={dryRun.counts} preview />
            {dryRun.limitReached && (
              <Alert type="warning">
                <FormattedMessage {...messages.limitReached} />
              </Alert>
            )}
            <Alert type="warning">
              <FormattedMessage {...messages.skippedWarning} />
            </Alert>
          </SpaceBetween>
        )}
      </SpaceBetween>
    </ReasonModal>
  );
};

export default BulkSkipModal;
