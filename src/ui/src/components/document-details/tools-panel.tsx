// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';

const DOCS_BASE_URL = 'https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws';

const ToolsPanel = (): React.JSX.Element => (
  <HelpPanel
    header={<h2>Document Details</h2>}
    footer={
      <div>
        <h3>
          Learn more <Icon name="external" />
        </h3>
        <ul>
          <li>
            <a href={`${DOCS_BASE_URL}/web-ui/`} target="_blank" rel="noopener noreferrer">
              Web UI
            </a>
          </li>
          <li>
            <a href={`${DOCS_BASE_URL}/classification/`} target="_blank" rel="noopener noreferrer">
              Classification
            </a>
          </li>
          <li>
            <a href={`${DOCS_BASE_URL}/extraction/`} target="_blank" rel="noopener noreferrer">
              Extraction
            </a>
          </li>
          <li>
            <a href={`${DOCS_BASE_URL}/assessment/`} target="_blank" rel="noopener noreferrer">
              Assessment
            </a>
          </li>
          <li>
            <a href={`${DOCS_BASE_URL}/human-review/`} target="_blank" rel="noopener noreferrer">
              Human Review
            </a>
          </li>
        </ul>
      </div>
    }
  >
    <div>
      <p>Inspect a document&apos;s processing results, and act on it from one bar at the top of the page.</p>
      <h3>The action bar</h3>
      <ul>
        <li>
          <b>Details | Source</b> switches between the page&apos;s panels and the original file.
        </li>
        <li>
          <b>Reports</b>: the Summary, Evaluation Report, Rule Validation Summary, Estimated Cost and Processing Flow, each in a dialog.
        </li>
        <li>
          <b>More Actions</b>: Edit Page Grouping, Re-extract Sections, Edit Page Text, Reset Page Class, Use As Evaluation Baseline, Skip
          All Reviews; then the downloads (the document&apos;s outputs as a ZIP); then Reprocess, Abort and Delete.
        </li>
        <li>
          <b>Refresh</b> the page, <b>Troubleshoot</b> what went wrong, and <b>Edit</b> the document&apos;s extracted values.
        </li>
      </ul>
      <p>
        Actions your role can&apos;t perform are not shown. One that doesn&apos;t apply now, for example while you view a past version,
        stays in place, disabled; hover it to see why.
      </p>
      <h3>On the page</h3>
      <ul>
        <li>The document&apos;s fields and status</li>
        <li>Each section&apos;s class, pages and status; its ⋮ menu opens or downloads its data</li>
        <li>Each page&apos;s class, confidence and thumbnail; its ⋮ menu opens the page text</li>
        <li>The ⚙ icon on each table chooses which columns it shows</li>
        <li>Version History, to view (from each row&apos;s ⋮ menu) or compare past processing runs, and Chat with the document</li>
      </ul>
    </div>
  </HelpPanel>
);

export default ToolsPanel;
