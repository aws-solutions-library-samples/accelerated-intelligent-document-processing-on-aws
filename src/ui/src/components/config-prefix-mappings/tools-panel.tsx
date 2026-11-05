// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';

const DOCS_BASE_URL = 'https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws';

export const PREFIX_MAPPINGS_HELP_LINKS = [
  {
    href: `${DOCS_BASE_URL}/configuration-profiles/#prefix-mappings-assigning-a-profile-by-where-a-document-lands`,
    label: 'Prefix Mappings',
  },
  { href: `${DOCS_BASE_URL}/multi-tenancy/`, label: 'Multi-tenancy' },
  { href: `${DOCS_BASE_URL}/multi-tenancy/#strict-mode`, label: 'Strict tenant isolation' },
  { href: `${DOCS_BASE_URL}/multi-tenancy/#who-grants-a-tenant`, label: 'Who grants a tenant' },
  { href: `${DOCS_BASE_URL}/rbac/`, label: 'Role-Based Access Control (RBAC)' },
];

const ToolsPanel = (): React.JSX.Element => (
  <HelpPanel
    header={<h2>Prefix Mappings and Tenants</h2>}
    footer={
      <div>
        <h3>
          Learn more <Icon name="external" />
        </h3>
        <ul>
          {PREFIX_MAPPINGS_HELP_LINKS.map(({ href, label }) => (
            <li key={href}>
              <a href={href} target="_blank" rel="noopener noreferrer">
                {label}
              </a>
            </li>
          ))}
        </ul>
      </div>
    }
  >
    <div>
      <h3>Prefix mappings</h3>
      <p>
        A prefix mapping assigns a Configuration Profile to every document that lands under an Input-bucket prefix, so the folder a document
        arrives in decides how it is processed, whoever wrote it there.
      </p>
      <ul>
        <li>The most specific match wins: a sub-folder mapping beats the parent it sits under.</li>
        <li>An exact object key outranks any prefix.</li>
        <li>Matching is case-sensitive, a disabled mapping never matches, and the bucket root cannot be mapped.</li>
      </ul>
      <p>
        This page is Admin-only because a document&apos;s profile decides which scoped users can see it: whoever writes a mapping decides
        who sees the documents under that prefix.
      </p>

      <h3>Tenants</h3>
      <p>
        A tenant declaration names the prefixes a tenant owns and a profile name pattern, for example <code>clients/acme/</code> and{' '}
        <code>acme-*</code>. Declarations may not overlap. Once tenants are declared, a mapping that sends one tenant&apos;s prefix to
        another tenant&apos;s profile is refused, and so is a user scope that spans two tenants.
      </p>
      <p>
        <strong>Granted by</strong> lists the claim or entitlement values (for example an Entra app role) that grant the tenant when the
        stack&apos;s <code>TenantGrantSource</code> is <code>IdpClaim</code> or <code>Api</code>. Under <code>Manual</code> an Admin sets
        each user&apos;s scope in User Management instead.
      </p>

      <h3>Isolation mode</h3>
      <p>
        With <code>TenantIsolationMode</code> <strong>Off</strong>, declarations prevent misconfiguration but a non-admin user with no scope
        still sees every document. <strong>Strict</strong> makes isolation fail closed: an unscoped user sees nothing, ingest refuses a
        document whose key and profile belong to different tenants, and features that read across tenants are limited to Admins.
      </p>

      <h3>Check a deployment</h3>
      <p>
        <code>idp-cli tenancy audit</code> reports boundary-crossing mappings, unscoped or multi-tenant users and mismatched documents.{' '}
        <code>idp-cli tenancy verify</code> runs a live cross-tenant test and cleans up after itself.
      </p>
    </div>
  </HelpPanel>
);

export default ToolsPanel;
