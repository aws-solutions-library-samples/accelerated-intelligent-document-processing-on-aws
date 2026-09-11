// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Tests for the "create a profile as a copy of an existing one" modal.
 *
 * The load-bearing properties are the two that lose data if they regress:
 *  - a name that is already taken must NOT be submitted, because the backend's
 *    saveAsVersion path writes straight to the named profile and would overwrite
 *    it; and
 *  - the new profile must be created from the source's Default MERGED WITH its
 *    Custom layer, since the customizations are the reason anyone copies a
 *    profile in the first place.
 */

import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import CreateConfigProfileModal from '../CreateConfigProfileModal';

const PROFILES = [
  { versionName: 'default', isActive: false, managed: true },
  { versionName: 'lending', isActive: true },
];

const fetchVersion = vi.fn();
const saveAsNewVersion = vi.fn();

vi.mock('../../../hooks/use-configuration-versions', () => ({
  default: () => ({
    versions: PROFILES,
    getVersionOptions: () => PROFILES.map((p) => ({ label: p.versionName, value: p.versionName })),
    fetchVersion,
    saveAsNewVersion,
  }),
}));

beforeEach(() => {
  fetchVersion.mockReset();
  saveAsNewVersion.mockReset();
  fetchVersion.mockResolvedValue({
    schema: {},
    default: { extraction: { model: 'base-model', temperature: 0 } },
    custom: { extraction: { model: 'tuned-model' } },
  });
  saveAsNewVersion.mockResolvedValue({ success: true });
});

const typeName = async (name: string) => {
  const user = userEvent.setup();
  await user.type(screen.getByPlaceholderText('e.g., my-custom-config'), name);
  await user.click(screen.getByRole('button', { name: 'Create profile' }));
  return user;
};

describe('CreateConfigProfileModal', () => {
  it('preselects the requested source profile', () => {
    render(<CreateConfigProfileModal visible onDismiss={vi.fn()} defaultSourceVersion="default" onCreated={vi.fn()} />);
    expect(screen.getByText('default')).toBeInTheDocument();
  });

  it('creates the profile from the source Default merged with its Custom layer', async () => {
    const onCreated = vi.fn();
    render(<CreateConfigProfileModal visible onDismiss={vi.fn()} defaultSourceVersion="lending" onCreated={onCreated} />);

    await typeName('lending-experiment');

    await waitFor(() => expect(saveAsNewVersion).toHaveBeenCalled());
    const [config, name, description] = saveAsNewVersion.mock.calls[0];
    // The custom layer wins over the default, and untouched default keys survive.
    expect(config).toMatchObject({ extraction: { model: 'tuned-model', temperature: 0 } });
    expect(name).toBe('lending-experiment');
    expect(description).toBe('Copied from lending');
    expect(onCreated).toHaveBeenCalledWith('lending-experiment');
  });

  it('refuses a name that is already taken instead of overwriting that profile', async () => {
    const onCreated = vi.fn();
    render(<CreateConfigProfileModal visible onDismiss={vi.fn()} defaultSourceVersion="default" onCreated={onCreated} />);

    await typeName('lending');

    expect(await screen.findByText(/already exists/)).toBeInTheDocument();
    expect(saveAsNewVersion).not.toHaveBeenCalled();
    expect(onCreated).not.toHaveBeenCalled();
  });

  it('refuses the reserved name "default"', async () => {
    render(<CreateConfigProfileModal visible onDismiss={vi.fn()} defaultSourceVersion="default" onCreated={vi.fn()} />);

    await typeName('default');

    expect(await screen.findByText(/reserved/)).toBeInTheDocument();
    expect(saveAsNewVersion).not.toHaveBeenCalled();
  });

  it('refuses a name with characters the backend cannot key on', async () => {
    render(<CreateConfigProfileModal visible onDismiss={vi.fn()} defaultSourceVersion="default" onCreated={vi.fn()} />);

    await typeName('my profile!');

    expect(await screen.findByText(/can only contain/)).toBeInTheDocument();
    expect(saveAsNewVersion).not.toHaveBeenCalled();
  });

  it('surfaces a failed create rather than reporting success', async () => {
    saveAsNewVersion.mockResolvedValue({ success: false, error: 'Configuration validation failed' });
    const onCreated = vi.fn();
    render(<CreateConfigProfileModal visible onDismiss={vi.fn()} defaultSourceVersion="default" onCreated={onCreated} />);

    await typeName('new-profile');

    expect(await screen.findByText('Configuration validation failed')).toBeInTheDocument();
    expect(onCreated).not.toHaveBeenCalled();
  });
});
