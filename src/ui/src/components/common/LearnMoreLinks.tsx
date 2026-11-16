// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The "Learn more" footer of an info panel (HelpPanel): links into the published docs.
import React from 'react';
import { Icon } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, type NoMessageValues } from 'react-intl';

import { DOCS_BASE_URL } from '../../constants/github';

const messages = defineMessages<{ heading: NoMessageValues }>({
  heading: {
    id: 'common.learnMoreLinks.heading',
    defaultMessage: 'Learn more',
    description: 'Heading of the footer of an info (help) panel, above a list of links into the product documentation.',
  },
});

export interface DocLink {
  /** The link label: a `<FormattedMessage>` (or a string the caller already formatted). */
  text: React.ReactNode;
  /** Docs-site path, e.g. `human-review/` or `web-ui/#inbox`. */
  docsPath: string;
}

/** A docs-site URL for ``path`` (no leading slash). */
export const docsUrl = (path: string): string => `${DOCS_BASE_URL}/${path}`;

const LearnMoreLinks = ({ links }: { links: DocLink[] }): React.JSX.Element => (
  <div>
    <h3>
      <FormattedMessage {...messages.heading} /> <Icon name="external" />
    </h3>
    <ul>
      {links.map((link) => (
        <li key={link.docsPath}>
          <a href={docsUrl(link.docsPath)} target="_blank" rel="noopener noreferrer">
            {link.text}
          </a>
        </li>
      ))}
    </ul>
  </div>
);

export default LearnMoreLinks;
