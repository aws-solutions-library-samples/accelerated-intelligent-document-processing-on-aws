// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The "Learn more" footer of an info panel (HelpPanel): links into the published docs.
import React from 'react';
import { Icon } from '@cloudscape-design/components';

import { DOCS_BASE_URL } from '../../constants/github';

export interface DocLink {
  text: string;
  /** Docs-site path, e.g. `human-review/` or `web-ui/#inbox`. */
  path: string;
}

/** A docs-site URL for ``path`` (no leading slash). */
export const docsUrl = (path: string): string => `${DOCS_BASE_URL}/${path}`;

const LearnMoreLinks = ({ links }: { links: DocLink[] }): React.JSX.Element => (
  <div>
    <h3>
      Learn more <Icon name="external" />
    </h3>
    <ul>
      {links.map((link) => (
        <li key={link.path}>
          <a href={docsUrl(link.path)} target="_blank" rel="noopener noreferrer">
            {link.text}
          </a>
        </li>
      ))}
    </ul>
  </div>
);

export default LearnMoreLinks;
