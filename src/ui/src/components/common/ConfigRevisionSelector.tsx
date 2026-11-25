// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useEffect, useMemo, useRef } from 'react';
import { FormField, Select, SelectProps, StatusIndicator } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';
import useConfigProfileRevisions, { type ConfigProfileRevision } from '../../hooks/use-config-profile-revisions';
import { useFormatters } from '../../i18n/format';

const messages = defineMessages<{
  label: NoMessageValues;
  description: NoMessageValues;
  currentRevision: { readonly revision: number };
  current: NoMessageValues;
  differs: { readonly revision: number };
  followsProfile: NoMessageValues;
  loading: NoMessageValues;
}>({
  label: {
    id: 'common.configRevisionSelector.label',
    defaultMessage: 'Configuration revision',
    description: 'Label of the selector of the configuration profile revision the work runs against.',
  },
  description: {
    id: 'common.configRevisionSelector.description',
    defaultMessage: 'Defaults to the profile’s current configuration. Pick an earlier revision to run against exactly what it recorded.',
    description: 'Help text under the Configuration revision label, on screens that do not supply their own.',
  },
  currentRevision: {
    id: 'common.configRevisionSelector.currentRevision',
    defaultMessage: 'Current (r{revision})',
    description: 'Option for the profile’s current configuration. {revision} is its published revision number, after "r" for revision.',
  },
  current: {
    id: 'common.configRevisionSelector.current',
    defaultMessage: 'Current',
    description: 'Option for the profile’s current configuration when no revision is published, and the selector’s placeholder.',
  },
  differs: {
    id: 'common.configRevisionSelector.differs',
    defaultMessage: "r{revision}, which differs from the profile's configuration in the editor",
    description:
      'Description of the Current option when the profile was changed without recording a revision. {revision} is the published one.',
  },
  followsProfile: {
    id: 'common.configRevisionSelector.followsProfile',
    defaultMessage: 'Whatever the profile holds when the work runs',
    description: 'Description of the Current option: the work uses the profile as it is when the work starts.',
  },
  loading: {
    id: 'common.configRevisionSelector.loading',
    defaultMessage: 'Loading revisions…',
    description: 'Shown in place of the Configuration revision selector while the revisions load.',
  },
});

interface ConfigRevisionSelectorProps {
  /** Configuration Profile whose revisions are offered. */
  profileName?: string | null;
  /** Selected revision, or null for "the profile's current configuration". */
  value: number | null;
  onChange: (revision: number | null) => void;
  label?: string;
  description?: string;
  disabled?: boolean;
}

/** Sentinel for "current" so the Select has a stable non-null option value. */
const CURRENT = '__current__';

interface RevisionSelectProps extends Pick<ConfigRevisionSelectorProps, 'value' | 'onChange' | 'label' | 'description'> {
  revisions: ConfigProfileRevision[];
  profileMatchesPublished: boolean | null;
  error: string | null;
  disabled: boolean;
}

/**
 * The picker itself, rendered only when there is a choice or an error to show.
 * Its react-intl calls sit here, below the early returns of the component that
 * renders it, so a profile with no revision history renders none of them: the
 * screens that embed the selector keep rendering without an IntlProvider in
 * their tests.
 */
const RevisionSelect = ({
  revisions,
  profileMatchesPublished,
  error,
  value,
  onChange,
  label,
  description,
  disabled,
}: RevisionSelectProps): React.JSX.Element => {
  const intl = useIntl();
  const { formatDateTime } = useFormatters();

  const options: SelectProps.Option[] = useMemo(() => {
    const published = revisions.find((r) => r.published);
    const current: SelectProps.Option = {
      value: CURRENT,
      label: published
        ? intl.formatMessage(messages.currentRevision, { revision: published.revision })
        : intl.formatMessage(messages.current),
      // The work runs the published revision. When the profile was changed
      // without recording one, that is not the configuration its editor shows.
      description:
        profileMatchesPublished === false && published
          ? intl.formatMessage(messages.differs, { revision: published.revision })
          : intl.formatMessage(messages.followsProfile),
    };
    const older = revisions
      .filter((r) => !r.published)
      .map((r) => ({
        value: String(r.revision),
        label: `r${r.revision}`,
        description: [r.label, r.notes, r.createdAt ? formatDateTime(new Date(r.createdAt)) : null].filter(Boolean).join(' — '),
      }));
    return [current, ...older];
  }, [revisions, profileMatchesPublished, formatDateTime, intl]);

  const selectedOption = options.find((o) => o.value === (value === null ? CURRENT : String(value))) ?? options[0];

  return (
    <FormField
      label={label ?? intl.formatMessage(messages.label)}
      description={description ?? intl.formatMessage(messages.description)}
      errorText={error ?? undefined}
    >
      <Select
        selectedOption={selectedOption}
        options={options}
        disabled={disabled}
        onChange={({ detail }) => {
          const picked = detail.selectedOption?.value;
          onChange(!picked || picked === CURRENT ? null : Number(picked));
        }}
        placeholder={intl.formatMessage(messages.current)}
      />
    </FormField>
  );
};

/**
 * Picks a revision of a Configuration Profile, defaulting to its current one.
 *
 * Rendered next to a profile picker wherever a profile is chosen for work. It
 * renders nothing when the profile has no revision history (an older deployment,
 * or a profile untouched since the upgrade) — an empty dropdown offering only
 * "Current" is noise, not a choice.
 */
const ConfigRevisionSelector = ({
  profileName,
  value,
  onChange,
  label,
  description,
  disabled = false,
}: ConfigRevisionSelectorProps): React.JSX.Element | null => {
  const { revisions, profileMatchesPublished, loading, error, loadRevisions } = useConfigProfileRevisions();

  useEffect(() => {
    if (profileName) loadRevisions(profileName);
  }, [profileName, loadRevisions]);

  /**
   * Changing profile invalidates any revision chosen under the previous one: a
   * revision number only means something inside one profile, so r5 of `lending`
   * is unrelated to r5 of `claims`.
   *
   * ⚠️ The guard is the load-bearing part. A bare
   * `useEffect(() => onChange(null), [profileName])` also fires on **mount**,
   * which is not a change — and a caller that mounts this with a profile and a
   * revision already chosen (an edit form populated from a stored record) had
   * its stored revision wiped before the user touched anything. Where the write
   * path is a full replace, as `putConfigPrefixMapping` is, editing an unrelated
   * field then silently unpinned the revision. So the previous value is held in
   * a ref seeded with the mount value, and the reset fires only on a genuine
   * transition.
   */
  const previousProfile = useRef<string | null | undefined>(profileName);
  useEffect(() => {
    if (previousProfile.current === profileName) return;
    previousProfile.current = profileName;
    onChange(null);
  }, [profileName]);

  if (!profileName) return null;
  if (loading)
    return (
      <StatusIndicator type="loading">
        <FormattedMessage {...messages.loading} />
      </StatusIndicator>
    );
  // No history: nothing meaningful to choose between.
  if (!error && revisions.length <= 1) return null;

  return (
    <RevisionSelect
      revisions={revisions}
      profileMatchesPublished={profileMatchesPublished}
      error={error}
      value={value}
      onChange={onChange}
      label={label}
      description={description}
      disabled={disabled}
    />
  );
};

export default ConfigRevisionSelector;
