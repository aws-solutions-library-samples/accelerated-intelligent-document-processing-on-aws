// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Tests for the Configuration-revision picker.
 *
 * The load-bearing properties are about what it does NOT show: it renders
 * nothing when there is no profile selected and nothing when the profile has no
 * revision history, because a dropdown whose only entry is "Current" is noise
 * rather than a choice — and most deployments will be in exactly that state
 * until someone saves a configuration.
 *
 * The other load-bearing property is *when the selection is reset*. The reset
 * belongs to a profile **change** and must not fire on mount or on an unrelated
 * re-render, because consumers hand this component their form state and the
 * write paths behind those forms are full replaces.
 */

import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

import ConfigRevisionSelector from '../ConfigRevisionSelector';
import type { ConfigProfileRevision } from '../../../hooks/use-config-profile-revisions';

const state: {
  revisions: ConfigProfileRevision[];
  loading: boolean;
  error: string | null;
} = { revisions: [], loading: false, error: null };

const loadRevisions = vi.fn();

vi.mock('../../../hooks/use-config-profile-revisions', () => ({
  default: () => ({ ...state, loadRevisions, setError: vi.fn() }),
}));

const REVISIONS: ConfigProfileRevision[] = [
  { revision: 7, published: true, createdAt: '2026-08-29T10:00:00Z', createdBy: 'a@example.com' },
  { revision: 5, published: false, label: 'known good', createdAt: '2026-08-28T10:00:00Z' },
  { revision: 4, published: false, createdAt: '2026-08-27T10:00:00Z' },
];

beforeEach(() => {
  state.revisions = [];
  state.loading = false;
  state.error = null;
  loadRevisions.mockClear();
});

describe('ConfigRevisionSelector', () => {
  it('renders nothing until a profile is chosen', () => {
    state.revisions = REVISIONS;
    const { container } = render(<ConfigRevisionSelector profileName={null} value={null} onChange={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
    expect(loadRevisions).not.toHaveBeenCalled();
  });

  it('renders nothing for a profile with no revision history', () => {
    // The normal state on a deployment that has not saved a configuration since
    // revisions were introduced.
    state.revisions = [];
    const { container } = render(<ConfigRevisionSelector profileName="lending" value={null} onChange={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when the only revision is the current one', () => {
    state.revisions = [REVISIONS[0]];
    const { container } = render(<ConfigRevisionSelector profileName="lending" value={null} onChange={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('loads the revisions of the selected profile', () => {
    state.revisions = REVISIONS;
    render(<ConfigRevisionSelector profileName="lending" value={null} onChange={vi.fn()} />);
    expect(loadRevisions).toHaveBeenCalledWith('lending');
  });

  it('defaults to the current revision, naming it', () => {
    state.revisions = REVISIONS;
    render(<ConfigRevisionSelector profileName="lending" value={null} onChange={vi.fn()} />);
    expect(screen.getByText('Current (r7)')).toBeInTheDocument();
  });

  it('shows the pinned revision when one is selected', () => {
    state.revisions = REVISIONS;
    render(<ConfigRevisionSelector profileName="lending" value={5} onChange={vi.fn()} />);
    expect(screen.getByText('r5')).toBeInTheDocument();
  });

  it('does not clear the selection on mount, because mounting is not a change', () => {
    // The case this exists for: an edit form mounts the selector with the
    // profile AND the revision it has on record. A reset here reaches the
    // parent's form state before the user has touched anything, and because
    // `putConfigPrefixMapping` is a full replace, saving after editing an
    // unrelated field then silently unpinned the revision.
    //
    // There is deliberately no `onChange.mockClear()` before this assertion.
    // One used to sit between the render and the rerender in the case below,
    // which is precisely why the mount-time call went unnoticed: the only call
    // that mattered was the one being discarded.
    state.revisions = REVISIONS;
    const onChange = vi.fn();
    render(<ConfigRevisionSelector profileName="lending" value={5} onChange={onChange} />);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('clears a stale selection when the profile actually changes', () => {
    // A revision number only means something inside one profile: r5 of `lending`
    // is unrelated to r5 of `claims`.
    state.revisions = REVISIONS;
    const onChange = vi.fn();
    const { rerender } = render(<ConfigRevisionSelector profileName="lending" value={5} onChange={onChange} />);
    rerender(<ConfigRevisionSelector profileName="claims" value={5} onChange={onChange} />);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it('does not clear on a re-render that leaves the profile alone', () => {
    // The upload panel re-renders this on every keystroke in its prefix field,
    // so "fires once per render" and "fires on a change" are not the same
    // property and both need pinning.
    state.revisions = REVISIONS;
    const onChange = vi.fn();
    const { rerender } = render(<ConfigRevisionSelector profileName="lending" value={5} onChange={onChange} />);
    rerender(<ConfigRevisionSelector profileName="lending" value={5} onChange={onChange} disabled />);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('surfaces a load error rather than silently offering only "Current"', () => {
    state.revisions = REVISIONS;
    state.error = 'Failed to load revision history';
    render(<ConfigRevisionSelector profileName="lending" value={null} onChange={vi.fn()} />);
    expect(screen.getByText('Failed to load revision history')).toBeInTheDocument();
  });
});
