// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import type { LinkProps } from '@cloudscape-design/components';
import { Link } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, type NoMessageValues } from 'react-intl';

const messages = defineMessages<{ info: NoMessageValues }>({
  info: {
    id: 'common.infoLink.info',
    defaultMessage: 'Info',
    description: "Link beside the Documents table's title that opens the help panel.",
  },
});

interface InfoLinkProps {
  id?: string;
  onFollow?: LinkProps['onFollow'];
}

export const InfoLink = ({ id, onFollow }: InfoLinkProps): React.JSX.Element => (
  <Link variant="info" id={id} onFollow={onFollow}>
    <FormattedMessage {...messages.info} />
  </Link>
);

export default InfoLink;
