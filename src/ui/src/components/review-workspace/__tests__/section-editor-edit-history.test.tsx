// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Field edits are recorded on save. What has to hold: with `recordEditHistory`, a save
 * that changed a field appends one `_editHistory` entry naming who changed which field
 * from what to what (the shape History › Field Edits lists); a save with no field
 * change appends nothing; and without the prop nothing is appended (a save path that
 * records its own entry).
 */

import React from 'react';
import { act, render, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../../common/PageImageViewer', () => ({ default: () => null }));
vi.mock('../../document-viewer/FormFieldRenderer', () => ({ default: () => null }));
vi.mock('../../document-viewer/JSONEditorTab', () => ({ default: () => null }));

import SectionEditor from '../SectionEditor';
import type { SectionEditorContext, SectionEditorController } from '../SectionEditor';

const ORIGINAL = { inference_result: { Name: 'Ann', Total: 10 }, _editHistory: [{ timestamp: 't0', editedBy: 'x' }] };

const setup = async (record?: { editedBy: string; source: string }) => {
  const onSave = vi.fn(async (_id: string, _data: Record<string, unknown>) => undefined);
  const controller: { current: SectionEditorController | null } = { current: null };
  let ctx: SectionEditorContext | null = null;
  render(
    <SectionEditor
      objectKey="in/doc.pdf"
      sections={[{ sectionId: '1', resultUri: 's3://b/r', pageIds: [] }]}
      pages={[]}
      isReadOnly={false}
      loadSection={async () => ({ data: JSON.parse(JSON.stringify(ORIGINAL)), raw: JSON.stringify(ORIGINAL) })}
      onSave={onSave}
      controllerRef={controller}
      actionsPlacement="external"
      recordEditHistory={record}
      headerExtras={(c) => {
        ctx = c;
        return null;
      }}
    />,
  );
  await waitFor(() => expect(ctx?.data).not.toBeNull());
  return { onSave, controller, ctx: () => ctx! };
};

describe('SectionEditor edit history', () => {
  it('appends one entry for a save that changed a field', async () => {
    const { onSave, controller, ctx } = await setup({ editedBy: 'alice', source: 'review-queue' });
    act(() => ctx().setData({ ...ctx().data!, inference_result: { Name: 'Bob', Total: 10 } }));
    await act(async () => {
      await controller.current!.save();
    });
    const saved = onSave.mock.calls[0][1] as { _editHistory: Record<string, unknown>[] };
    expect(saved._editHistory).toHaveLength(2);
    expect(saved._editHistory[1]).toMatchObject({
      editedBy: 'alice',
      source: 'review-queue',
      predictionEdits: { changedFields: ['Name'], changeCount: 1, diffs: { Name: { originalValue: 'Ann', newValue: 'Bob' } } },
    });
  });

  it('appends nothing without the prop', async () => {
    const { onSave, controller, ctx } = await setup();
    act(() => ctx().setData({ ...ctx().data!, inference_result: { Name: 'Bob', Total: 10 } }));
    await act(async () => {
      await controller.current!.save();
    });
    expect((onSave.mock.calls[0][1] as { _editHistory: unknown[] })._editHistory).toHaveLength(1);
  });

  it('saves nothing when no field changed', async () => {
    const { onSave, controller } = await setup({ editedBy: 'alice', source: 'review-queue' });
    await act(async () => {
      await controller.current!.save();
    });
    expect(onSave).not.toHaveBeenCalled();
  });
});
