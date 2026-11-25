// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState, useEffect } from 'react';
import { AppLayout, ContentLayout, Header, SpaceBetween } from '@cloudscape-design/components';
import { useLocation } from 'react-router-dom';

import Navigation from '../genaiidp-layout/navigation';
import TestSets from './TestSets';
import TestExecutions from './TestExecutions';
import TestResults from './TestResults';
import TestComparison from './TestComparison';
import ToolsPanel from './tools-panel';
import { useAppLayoutLabels } from '../common/labels';
import useAppContext from '../../contexts/app';

interface TestRunItem {
  testRunId: string;
  testSetName: string;
  status: string;
  isActive?: boolean;
  progress?: number;
  filesCount: number;
  createdAt: string;
  completedAt: string | null;
  context: string;
  configVersion?: string | null;
}

const TestStudioLayout = (): React.JSX.Element => {
  const { navigationOpen, setNavigationOpen, activeTestRuns, addTestRun, removeTestRun } = useAppContext();
  const appLayoutLabels = useAppLayoutLabels();
  const location = useLocation();
  const [activeTabId, setActiveTabId] = useState('sets');
  const [timePeriodHours, setTimePeriodHours] = useState(336); // Default: 2 weeks
  const [selectedTestItems, setSelectedTestItems] = useState<TestRunItem[]>([]);
  const [toolsOpen, setToolsOpen] = useState(false);

  // Handle URL tab parameter
  useEffect(() => {
    const urlParams = new URLSearchParams(location.search);
    const tab = urlParams.get('tab');
    if (tab && ['sets', 'executions', 'results', 'comparison'].includes(tab)) {
      setActiveTabId(tab);
    }
  }, [location.search]);

  const handleTestStart = (testRunId: string, testSetName: string, context: string, filesCount: number, configVersion?: string): void => {
    addTestRun(testRunId, testSetName, context, filesCount, configVersion ?? '');
  };

  const handleTestComplete = (testRunId: string): void => {
    removeTestRun(testRunId);
  };

  const renderContent = (): React.JSX.Element => {
    switch (activeTabId) {
      case 'sets':
        return <TestSets />;
      case 'executions': {
        const urlParams = new URLSearchParams(location.search);
        const testRunId = urlParams.get('testRunId');
        return (
          <TestExecutions
            timePeriodHours={timePeriodHours}
            setTimePeriodHours={setTimePeriodHours}
            selectedItems={selectedTestItems}
            setSelectedItems={setSelectedTestItems}
            preSelectedTestRunId={testRunId}
            activeTestRuns={activeTestRuns}
            onTestStart={handleTestStart}
            onTestComplete={handleTestComplete}
          />
        );
      }
      case 'results': {
        const urlParams = new URLSearchParams(location.search);
        const testRunId = urlParams.get('testRunId');
        return <TestResults testRunId={testRunId ?? ''} />;
      }
      case 'comparison': {
        const urlParams = new URLSearchParams(location.search);
        const testIds = urlParams.get('testIds');
        const testRunIds = testIds ? testIds.split(',') : [];
        return <TestComparison preSelectedTestRunIds={testRunIds} />;
      }
      default:
        return <TestSets />;
    }
  };

  return (
    <AppLayout
      headerSelector="#top-navigation"
      ariaLabels={appLayoutLabels}
      navigation={<Navigation />}
      navigationOpen={navigationOpen}
      onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
      tools={<ToolsPanel />}
      toolsOpen={toolsOpen}
      onToolsChange={({ detail }) => setToolsOpen(detail.open)}
      content={
        <ContentLayout
          header={
            <Header variant="h1" description="Run tests, view results, and compare test outcomes">
              Test Studio
            </Header>
          }
        >
          <SpaceBetween size="l">{renderContent()}</SpaceBetween>
        </ContentLayout>
      }
    />
  );
};

export default TestStudioLayout;
