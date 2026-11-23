// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Exporting the routing table as DMN and importing one, as the tab's menu asks for them:
 * the version asked for, the file that is downloaded, what an import replaces and when,
 * how a refusal is shown, and when the menu is told the controls are busy.
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { IntlProvider } from 'react-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
const exportToTextFile = vi.fn();
vi.mock('../../common/download-func', () => ({ exportToTextFile: (...a: unknown[]) => exportToTextFile(...a) }));

import RoutingDmnControls, { MAX_DMN_BYTES, type RoutingDmnControlsRef } from '../RoutingDmnControls';
import type { DmnVersion } from '../routing-api';
import { readRouting, tableOf } from '../routing-types';

const ROUTING = readRouting({
  tables: [{ rules: [{ id: 'old', conditions: [], actions: [{ type: 'review_queue', queue: 'default' }], reason: 'Check it.' }] }],
});
const IMPORTED = { tables: [{ id: 'review-routing', rules: [{ id: 'a' }, { id: 'b' }] }], max_consecutive_placements: 4 };

// What the tab's menu holds: the ref it starts an import and an export through.
const controls = React.createRef<RoutingDmnControlsRef>();
const onBusyChange = vi.fn();

const renderControls = () => {
  const onImport = vi.fn();
  onBusyChange.mockReset();
  render(
    <IntlProvider locale="en">
      <RoutingDmnControls ref={controls} routing={ROUTING} profileName="claims" onImport={onImport} onBusyChange={onBusyChange} />
    </IntlProvider>,
  );
  return onImport;
};
const exportAs = async (version: DmnVersion): Promise<void> => {
  await act(async () => {
    controls.current!.exportDmn(version);
  });
};

const variablesOf = (call: number) => (graphql.mock.calls[call][0] as { variables: Record<string, unknown> }).variables;
const fileInput = (): HTMLInputElement => screen.getByLabelText('DMN file to import') as HTMLInputElement;
const choose = async (file: File): Promise<void> => {
  await act(async () => {
    fireEvent.change(fileInput(), { target: { files: [file] } });
  });
};
const dmnFile = (text = '<definitions/>', name = 'routing.dmn'): File => new File([text], name, { type: 'application/xml' });

describe('exporting', () => {
  beforeEach(() => {
    graphql.mockReset();
    exportToTextFile.mockReset();
  });

  it('sends the table as edited, actions and reasons included, and downloads a .dmn named after the profile', async () => {
    graphql.mockResolvedValue({ data: { convertRoutingDmn: { xml: '<definitions/>', routing: null } } });
    renderControls();

    await exportAs('1.5');

    expect(variablesOf(0)).toMatchObject({ direction: 'to_dmn', dmnVersion: '1.5' });
    expect(JSON.parse(variablesOf(0).content as string)).toEqual(ROUTING);
    expect(exportToTextFile).toHaveBeenCalledWith('<definitions/>', 'routing-claims', 'dmn', 'application/xml');
  });

  it('asks for the version the menu names', async () => {
    graphql.mockResolvedValue({ data: { convertRoutingDmn: { xml: '<definitions/>', routing: null } } });
    renderControls();

    await exportAs('1.3');

    expect(variablesOf(0).dmnVersion).toBe('1.3');
  });

  it('shows why an export failed and downloads nothing', async () => {
    graphql.mockRejectedValue(new Error('INVALID_ROUTING: the content is not a routing section'));
    renderControls();

    await exportAs('1.5');

    expect(screen.getByText('The table could not be exported')).toBeTruthy();
    expect(screen.getByText('INVALID_ROUTING: the content is not a routing section')).toBeTruthy();
    expect(exportToTextFile).not.toHaveBeenCalled();
  });

  it('says it is busy while the server converts, and that it is done after, failed or not', async () => {
    graphql.mockResolvedValueOnce({ data: { convertRoutingDmn: { xml: '<definitions/>', routing: null } } });
    graphql.mockRejectedValueOnce(new Error('boom'));
    renderControls();

    await exportAs('1.5');
    expect(onBusyChange.mock.calls).toEqual([[true], [false]]);
    await exportAs('1.5');
    expect(onBusyChange.mock.calls).toEqual([[true], [false], [true], [false]]);
  });

  it('shows no button of its own: the menu of the tab starts an export and an import', () => {
    renderControls();

    expect(screen.queryByRole('button', { name: /Export/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /Import/ })).toBeNull();
    expect(createWrapper(document.body).findSelect()).toBeNull();
  });
});

describe('importing', () => {
  beforeEach(() => {
    graphql.mockReset();
  });

  it('opens the file picker when the menu asks', () => {
    renderControls();
    const click = vi.spyOn(fileInput(), 'click').mockImplementation(() => {});

    act(() => controls.current!.importDmn());

    expect(click).toHaveBeenCalledTimes(1);
    expect(fileInput().accept).toBe('.dmn,.xml');
  });

  it('sends the text of the file and asks before replacing the table', async () => {
    graphql.mockResolvedValue({ data: { convertRoutingDmn: { routing: JSON.stringify(IMPORTED), xml: null } } });
    const onImport = renderControls();

    await choose(dmnFile('<definitions id="x"/>'));

    expect(variablesOf(0)).toEqual({ direction: 'from_dmn', content: '<definitions id="x"/>' });
    await waitFor(() => expect(createWrapper(document.body).findModal()!.isVisible()).toBe(true));
    expect(screen.getByText('Replace the routing table?')).toBeTruthy();
    expect(screen.getByText('routing.dmn holds 2 routing rules. They replace every routing rule in the table.')).toBeTruthy();
    expect(screen.getByText('Nothing is saved until you save the configuration.')).toBeTruthy();
    expect(onImport).not.toHaveBeenCalled();
  });

  it('replaces the table when confirmed', async () => {
    graphql.mockResolvedValue({ data: { convertRoutingDmn: { routing: JSON.stringify(IMPORTED), xml: null } } });
    const onImport = renderControls();
    await choose(dmnFile());

    fireEvent.click(await screen.findByRole('button', { name: 'Replace' }));

    expect(onImport).toHaveBeenCalledTimes(1);
    const imported = onImport.mock.calls[0][0];
    expect(tableOf(imported).rules.map((rule: { id: string }) => rule.id)).toEqual(['a', 'b']);
    expect(imported.max_consecutive_placements).toBe(4);
  });

  it('leaves the table alone when cancelled', async () => {
    graphql.mockResolvedValue({ data: { convertRoutingDmn: { routing: JSON.stringify(IMPORTED), xml: null } } });
    const onImport = renderControls();
    await choose(dmnFile());

    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }));

    expect(onImport).not.toHaveBeenCalled();
    expect(createWrapper(document.body).findModal()!.isVisible()).toBe(false);
  });

  it('counts one rule as one rule', async () => {
    graphql.mockResolvedValue({
      data: { convertRoutingDmn: { routing: JSON.stringify({ tables: [{ rules: [{ id: 'a' }] }] }), xml: null } },
    });
    renderControls();

    await choose(dmnFile());

    await waitFor(() =>
      expect(screen.getByText('routing.dmn holds 1 routing rule. They replace every routing rule in the table.')).toBeTruthy(),
    );
  });

  it("shows the server's refusal, with the construct it names, and offers no replacement", async () => {
    graphql.mockRejectedValue(new Error("INVALID_DMN: hit policy 'COLLECT' is not supported; use FIRST"));
    const onImport = renderControls();

    await choose(dmnFile());

    await waitFor(() => expect(screen.getByText('The file could not be imported')).toBeTruthy());
    expect(screen.getByText("INVALID_DMN: hit policy 'COLLECT' is not supported; use FIRST")).toBeTruthy();
    expect(createWrapper(document.body).findModal()!.isVisible()).toBe(false);
    expect(onImport).not.toHaveBeenCalled();
  });

  it('refuses a file larger than the server reads, without sending it', async () => {
    const onImport = renderControls();

    await choose(new File([new Uint8Array(MAX_DMN_BYTES + 1)], 'big.dmn'));

    expect(screen.getByText('The file is larger than 512 KB.')).toBeTruthy();
    expect(graphql).not.toHaveBeenCalled();
    expect(onImport).not.toHaveBeenCalled();
    expect(onBusyChange).not.toHaveBeenCalled();
  });

  it('accepts a file of exactly the largest size', async () => {
    graphql.mockResolvedValue({ data: { convertRoutingDmn: { routing: JSON.stringify(IMPORTED), xml: null } } });
    renderControls();

    await choose(new File([new Uint8Array(MAX_DMN_BYTES)], 'largest.dmn'));

    expect(graphql).toHaveBeenCalledTimes(1);
  });

  it('lets the same file be chosen again', async () => {
    graphql.mockRejectedValue(new Error('INVALID_DMN: no decision table'));
    renderControls();
    const file = dmnFile();

    await choose(file);
    await choose(file);

    expect(graphql).toHaveBeenCalledTimes(2);
    expect(fileInput().value).toBe('');
  });
});
