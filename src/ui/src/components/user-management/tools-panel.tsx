// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';

const DOCS_BASE_URL = 'https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws';

const ToolsPanel = (): React.JSX.Element => (
  <HelpPanel
    header={<h2>User Management</h2>}
    footer={
      <div>
        <h3>
          Learn more <Icon name="external" />
        </h3>
        <ul>
          <li>
            <a href={`${DOCS_BASE_URL}/rbac/`} target="_blank" rel="noopener noreferrer">
              Role-Based Access Control (RBAC)
            </a>
          </li>
          <li>
            <a href={`${DOCS_BASE_URL}/multi-tenancy/#who-grants-a-tenant`} target="_blank" rel="noopener noreferrer">
              Multi-tenancy: who grants a tenant
            </a>
          </li>
        </ul>
      </div>
    }
  >
    <div>
      <p>Manage user access and permissions with role-based access control (RBAC).</p>
      <h3>Features</h3>
      <ul>
        <li>View and manage registered users and their roles</li>
        <li>Assign roles such as Admin, Reviewer, or Viewer to control access levels</li>
        <li>Enable or disable user accounts</li>
        <li>Role assignments control access to features like configuration editing, document review, and user management</li>
        <li>
          Set each user&apos;s configuration-profile scope, which confines them to one tenant&apos;s documents once tenants are declared
        </li>
      </ul>
    </div>
  </HelpPanel>
);

export default ToolsPanel;
