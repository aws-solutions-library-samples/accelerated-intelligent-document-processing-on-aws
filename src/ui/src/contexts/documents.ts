// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useContext, createContext } from 'react';
import { Document } from '../types/documents';

export interface DateRange {
  startDateTime: string;
  endDateTime: string;
}

export interface DocumentsContextValue {
  documents: Document[];
  getDocumentDetailsFromIds: (objectKeys: string[]) => Promise<Document[]>;
  isDocumentsListLoading: boolean;
  hasListBeenLoaded: boolean;
  selectedItems: Document[];
  setIsDocumentsListLoading: React.Dispatch<React.SetStateAction<boolean>>;
  setPeriodsToLoad: React.Dispatch<React.SetStateAction<number>>;
  setToolsOpen: React.Dispatch<React.SetStateAction<boolean>>;
  setSelectedItems: React.Dispatch<React.SetStateAction<Document[]>>;
  periodsToLoad: number;
  customDateRange: DateRange | null;
  setCustomDateRange: React.Dispatch<React.SetStateAction<DateRange | null>>;
  toolsOpen: boolean;
  deleteDocuments: (objectKeys: string[]) => Promise<unknown>;
  reprocessDocuments: (objectKeys: string[], version?: string, revision?: number) => Promise<unknown>;
  abortWorkflows: (objectKeys: string[]) => Promise<unknown>;
}

export const DocumentsContext = createContext<DocumentsContextValue | null>(null);

const useDocumentsContext = (): DocumentsContextValue => {
  const ctx = useContext(DocumentsContext);
  if (!ctx) throw new Error('useDocumentsContext must be used within DocumentsContext.Provider');
  return ctx;
};

export default useDocumentsContext;
