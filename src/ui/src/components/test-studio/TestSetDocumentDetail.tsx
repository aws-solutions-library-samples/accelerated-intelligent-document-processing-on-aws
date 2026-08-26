// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * TestSetDocumentDetail — one test set document's detail page.
 * Route: /test-studio/sets/:testSetId/doc/<objectKey> (objectKey may contain
 * slashes; captured via the route's wildcard). Optional ?view=source|ground-truth
 * preselects the view (deep-linked from the document list's row actions).
 *
 * Mirrors the app's Document List -> Document Details structure: breadcrumbs
 * back to the set, a segmented view switcher, the source-document viewer, and
 * the ground-truth visual editor (which itself handles multi-section
 * navigation and per-section page scoping).
 */

import React, { useState, useEffect } from 'react';
import { useParams, useLocation } from 'react-router-dom';
import {
  Alert,
  AppLayout,
  Box,
  BreadcrumbGroup,
  ContentLayout,
  Flashbar,
  Header,
  SegmentedControl,
  SpaceBetween,
  Spinner,
} from '@cloudscape-design/components';
import type { FlashbarProps } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from '../../api/client-shim';
import { getTestSetDocuments } from '../../graphql/generated';
import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';
import useUserRole from '../../hooks/use-user-role';
import Navigation from '../genaiidp-layout/navigation';
import { appLayoutLabels } from '../common/labels';
import FileViewer from '../document-viewer/FileViewer';
import GroundTruthVisualEditor from './GroundTruthVisualEditor';
import { TEST_STUDIO_PATH, testSetDetailHref } from '../../routes/constants';
import { formatSize } from './TestSetDetail';
import type { TestSetDocumentItem } from './TestSetDetail';

const client = generateClient();
const logger = new ConsoleLogger('TestSetDocumentDetail');

type DocView = 'ground-truth' | 'source';

const TestSetDocumentDetail = (): React.JSX.Element => {
  const { testSetId } = useParams<{ testSetId: string }>();
  const location = useLocation();
  // The objectKey is everything after /doc/ (wildcard route segment).
  const objectKey = decodeURIComponent(location.pathname.split('/doc/')[1] ?? '');
  const initialView = new URLSearchParams(location.search).get('view') === 'source' ? 'source' : 'ground-truth';

  const { navigationOpen, setNavigationOpen } = useAppContext();
  const { settings } = useSettingsContext();
  const { canWrite } = useUserRole();
  const testSetBucket = (settings as Record<string, unknown>).TestSetBucket as string | undefined;

  const [doc, setDoc] = useState<TestSetDocumentItem | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [docView, setDocView] = useState<DocView>(initialView);
  const [flashItems, setFlashItems] = useState<FlashbarProps.MessageDefinition[]>([]);

  useEffect(() => {
    if (!testSetId || !objectKey) return;
    let cancelled = false;
    const load = async () => {
      setIsLoading(true);
      setError(null);
      try {
        const response = await client.graphql({
          query: getTestSetDocuments,
          variables: { testSetId, objectKey, limit: 1 },
        });
        const docs = (response.data?.getTestSetDocuments?.documents ?? []) as TestSetDocumentItem[];
        if (!cancelled) {
          if (docs.length === 0) {
            setError(`Document "${objectKey}" was not found in test set "${testSetId}".`);
          } else {
            setDoc(docs[0]);
          }
        }
      } catch (err) {
        logger.error('Error loading test set document:', err);
        if (!cancelled) setError('Failed to load document details. Please try again.');
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    };
    load();
    return () => {
      cancelled = true;
    };
  }, [testSetId, objectKey]);

  const handleSaved = (_baselineKey: string) => {
    setFlashItems([
      {
        type: 'success',
        // A storage path is not a confirmation. It used to print the full key —
        // "opus-conf-bank-statement/baseline/152aa83d_doc_0001.pdf/sections/1/result.json"
        // — which told the reviewer where the bytes went and nothing about what
        // they had just done. State the consequence instead.
        content: 'Ground truth saved — this document is now part of the reference that future runs are scored against.',
        dismissible: true,
        onDismiss: () => setFlashItems([]),
        id: 'gt-saved',
      },
    ]);
  };

  return (
    <AppLayout
      headerSelector="#top-navigation"
      ariaLabels={appLayoutLabels}
      navigation={<Navigation />}
      navigationOpen={navigationOpen}
      onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
      toolsHide
      notifications={<Flashbar items={flashItems} />}
      content={
        <ContentLayout
          header={
            <SpaceBetween size="xs">
              <BreadcrumbGroup
                items={[
                  { text: 'Test Studio', href: `#${TEST_STUDIO_PATH}?tab=sets` },
                  { text: testSetId ?? '', href: testSetDetailHref(testSetId ?? '') },
                  { text: objectKey, href: '' },
                ]}
              />
              <Header
                variant="h1"
                description={
                  doc
                    ? `${formatSize(doc.size)} · ${doc.sections.length} ground truth section${doc.sections.length === 1 ? '' : 's'}`
                    : undefined
                }
              >
                {objectKey}
              </Header>
            </SpaceBetween>
          }
        >
          <SpaceBetween size="l">
            {!testSetBucket && <Alert type="error">TestSetBucket is not configured in settings.</Alert>}
            {error && <Alert type="error">{error}</Alert>}
            {isLoading && (
              <Box textAlign="center" padding="xl">
                <Spinner /> Loading document…
              </Box>
            )}

            {doc && testSetBucket && (
              <SpaceBetween size="s">
                <SegmentedControl
                  selectedId={docView}
                  onChange={({ detail }) => setDocView(detail.selectedId as DocView)}
                  options={[
                    { id: 'ground-truth', text: canWrite ? 'Edit Ground Truth' : 'View Ground Truth' },
                    { id: 'source', text: 'View Source Document' },
                  ]}
                />
                {docView === 'source' ? (
                  <FileViewer objectKey={doc.inputKey} bucket={testSetBucket} presignVia="server" />
                ) : (
                  <GroundTruthVisualEditor
                    bucket={testSetBucket}
                    inputKey={doc.inputKey}
                    objectKey={doc.objectKey}
                    sections={doc.sections}
                    isReadOnly={!canWrite}
                    onSaved={handleSaved}
                    testSetId={testSetId}
                  />
                )}
              </SpaceBetween>
            )}
          </SpaceBetween>
        </ContentLayout>
      }
    />
  );
};

export default TestSetDocumentDetail;
