// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Which view the edit screen shows: the Document Editor, or the source document in its
// place. `?view=source` (a queue's View action) opens on the source.
import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';

export type EditView = 'edit' | 'source';

const useEditView = (): [EditView, (view: EditView) => void] => {
  const [searchParams] = useSearchParams();
  return useState<EditView>(searchParams.get('view') === 'source' ? 'source' : 'edit');
};

export default useEditView;
