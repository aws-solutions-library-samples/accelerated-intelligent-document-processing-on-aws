// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';

const DOCS_BASE_URL = 'https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws';

const ToolsPanel = (): React.JSX.Element => (
  <HelpPanel
    header={<h2>Configuration</h2>}
    footer={
      <div>
        <h3>
          Learn more <Icon name="external" />
        </h3>
        <ul>
          <li>
            <a href={`${DOCS_BASE_URL}/configuration/`} target="_blank" rel="noopener noreferrer">
              Configuration
            </a>
          </li>
          <li>
            <a href={`${DOCS_BASE_URL}/configuration-versions/`} target="_blank" rel="noopener noreferrer">
              Configuration Profiles
            </a>
          </li>
          <li>
            <a href={`${DOCS_BASE_URL}/idp-configuration-best-practices/`} target="_blank" rel="noopener noreferrer">
              Configuration Best Practices
            </a>
          </li>
          <li>
            <a href={`${DOCS_BASE_URL}/json-schema-migration/`} target="_blank" rel="noopener noreferrer">
              JSON Schema Migration
            </a>
          </li>
        </ul>
      </div>
    }
  >
    <div>
      <p>
        View and edit the configuration that drives document processing — models, prompts, document classes, and each pipeline stage (OCR,
        Classification, Extraction, and optional steps like Summarization and Evaluation).
      </p>

      <h3>Versions</h3>
      <p>
        Each configuration is a self-contained <strong>profile</strong>. The <strong>default</strong> profile is managed by the stack and is
        read-only — it is overwritten on stack upgrades. To customize it, click <strong>Create profile</strong> in the{' '}
        <strong>Configuration Profiles</strong> table and copy <strong>default</strong> (or any other profile) into a new, editable one. Use
        that same table to open, activate, compare, import, or delete profiles.
      </p>

      <h3>Editing</h3>
      <ul>
        <li>
          Switch between <strong>Form View</strong> (guided fields with inline help), <strong>JSON View</strong>, and{' '}
          <strong>YAML View</strong> (raw editing) using the view toggle.
        </li>
        <li>
          Use the <strong>Document Schema</strong> and <strong>Policy Schema</strong> tabs to edit document classes and validation rules,
          and the <strong>Prompt Preview</strong> tab to see the exact prompts sent to the model.
        </li>
        <li>
          Edited fields show an orange dot; a per-field <strong>Restore default</strong> resets a single value. An unsaved-changes banner
          offers <strong>Discard changes</strong>, and the app warns you before navigating away with unsaved edits.
        </li>
        <li>
          <strong>Save changes</strong> persists your edits to an editable version. <strong>Refresh</strong> reloads the saved configuration
          from the server. Additional actions (Export, Save as default, Restore default (All), and BDA sync) are in the{' '}
          <strong>Actions</strong> menu.
        </li>
      </ul>
    </div>
  </HelpPanel>
);

export default ToolsPanel;
