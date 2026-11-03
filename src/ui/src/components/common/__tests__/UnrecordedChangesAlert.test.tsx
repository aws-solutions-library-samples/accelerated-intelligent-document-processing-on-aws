// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The warning that a profile's configuration is not the revision documents run.
 *
 * Only an established difference warns. `null` means the server had nothing to
 * compare (no revision history, or no entry for the published revision), and a
 * warning there would fire on every deployment that has not saved a profile
 * since revisions were introduced.
 */

import React from 'react';
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import UnrecordedChangesAlert from '../UnrecordedChangesAlert';

describe('UnrecordedChangesAlert', () => {
  it('names the profile and the revision documents still run', () => {
    render(<UnrecordedChangesAlert profileName="MyI9" publishedRevision={1} profileMatchesPublished={false} />);
    expect(screen.getByText('New documents do not run this configuration')).toBeInTheDocument();
    expect(screen.getByText('MyI9')).toBeInTheDocument();
    expect(screen.getByText('r1')).toBeInTheDocument();
  });

  it('renders nothing when the profile is its published revision', () => {
    const { container } = render(<UnrecordedChangesAlert profileName="MyI9" publishedRevision={2} profileMatchesPublished={true} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when there is nothing to compare', () => {
    const { container } = render(<UnrecordedChangesAlert profileName="MyI9" publishedRevision={null} profileMatchesPublished={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});
