// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Read-only record of the Policy Discovery uploads that built a policy class (#608):
// for each upload, the rules it supplied and the earlier rules it superseded, with
// the document that had supplied each. Shown in the Policy Schema class inspector.
import React from 'react';
import { ExpandableSection, Table } from '@cloudscape-design/components';
import { useIntl } from 'react-intl';
import { readPolicyUploads, uploadHistoryMessages, type PolicyUploadEntry } from '../discovery/policyGroup';
import { useFormatters } from '../../i18n/format';

interface PolicyUploadHistoryProps {
  /** The class's x-aws-idp-policy-uploads value. */
  uploads: unknown;
}

const PolicyUploadHistory = ({ uploads }: PolicyUploadHistoryProps): React.JSX.Element | null => {
  const intl = useIntl();
  const { formatDateTime } = useFormatters();
  const m = uploadHistoryMessages;
  const entries = readPolicyUploads(uploads);
  if (entries.length === 0) return null;

  const modeLabel = (mode: string): string => {
    if (mode === 'replace') return intl.formatMessage(m.modeReplace);
    if (mode === 'supersede') return intl.formatMessage(m.modeSupersede);
    return intl.formatMessage(m.modeAppend);
  };
  const savedAt = (value: string | null): string => {
    const date = value ? new Date(value) : null;
    return date && !Number.isNaN(date.getTime()) ? formatDateTime(date) : intl.formatMessage(m.none);
  };
  const list = (items: string[]): string => (items.length ? items.join(', ') : intl.formatMessage(m.none));

  return (
    <ExpandableSection headerText={intl.formatMessage(m.header)} headerDescription={intl.formatMessage(m.description)} defaultExpanded>
      <Table<PolicyUploadEntry>
        variant="embedded"
        wrapLines
        items={entries}
        columnDefinitions={[
          { id: 'document', header: intl.formatMessage(m.document), cell: (e) => e.document ?? intl.formatMessage(m.unknownDocument) },
          { id: 'savedAt', header: intl.formatMessage(m.savedAt), cell: (e) => savedAt(e.savedAt) },
          { id: 'change', header: intl.formatMessage(m.change), cell: (e) => modeLabel(e.mode) },
          { id: 'rules', header: intl.formatMessage(m.rules), cell: (e) => list(e.rules) },
          {
            id: 'superseded',
            header: intl.formatMessage(m.superseded),
            cell: (e) =>
              list(
                e.superseded.map((s) =>
                  intl.formatMessage(m.supersededFrom, { rule: s.rule, document: s.document ?? intl.formatMessage(m.unknownDocument) }),
                ),
              ),
          },
        ]}
      />
    </ExpandableSection>
  );
};

export default PolicyUploadHistory;
