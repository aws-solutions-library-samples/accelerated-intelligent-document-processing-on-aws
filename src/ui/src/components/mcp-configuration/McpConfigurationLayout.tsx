// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { defineMessages, useIntl, type IntlShape, type NoMessageValues } from 'react-intl';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Container,
  CopyToClipboard,
  Form,
  FormField,
  Header,
  Input,
  KeyValuePairs,
  Modal,
  Multiselect,
  Pagination,
  Select,
  SpaceBetween,
  StatusIndicator,
  Table,
  Tabs,
  TextFilter,
} from '@cloudscape-design/components';
import { useCollection } from '@cloudscape-design/collection-hooks';
import { ConsoleLogger } from 'aws-amplify/utils';

import { generateClient } from '../../api/client-shim';
import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';
import useUserRole from '../../hooks/use-user-role';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import useTenants from '../../hooks/use-tenants';
import {
  createMcpCredential,
  listMcpCredentials,
  revokeMcpCredential,
  rotateMcpCredential,
  updateMcpCredential,
} from '../../graphql/generated';
import { getErrorMessage } from '../../utils/errorUtils';

const logger = new ConsoleLogger('McpConfigurationLayout');

export interface McpCredential {
  clientId: string;
  name?: string | null;
  role?: string | null;
  allowedConfigVersions?: (string | null)[] | null;
  tenant?: string | null;
  writeTools?: boolean | null;
  enabled?: boolean | null;
  status?: string | null;
  kind?: string | null;
  createdBy?: string | null;
  createdAt?: string | null;
  expiresAt?: string | null;
  lastUsedAt?: string | null;
  replacedBy?: string | null;
  secretArn?: string | null;
}

export interface McpCredentialIssued {
  clientId: string;
  clientSecret: string;
  tokenUrl?: string | null;
  gatewayUrl?: string | null;
  secretArn?: string | null;
  oauthScopes?: (string | null)[] | null;
  credential?: McpCredential | null;
}

type Option = { label: string; value: string; description?: string };

const messages = defineMessages<{
  snippetLine: { readonly label: string; readonly value: string };
  credentialCreated: NoMessageValues;
  storedSecret: NoMessageValues;
  secretShownOnce: NoMessageValues;
  copyItNow: NoMessageValues;
  clientId: NoMessageValues;
  clientIdCopied: NoMessageValues;
  couldNotCopy: NoMessageValues;
  copyClientId: NoMessageValues;
  clientSecret: NoMessageValues;
  clientSecretCopied: NoMessageValues;
  copyClientSecret: NoMessageValues;
  tokenUrl: NoMessageValues;
  serverEndpoint: NoMessageValues;
  secretsManagerArn: NoMessageValues;
  quickTab: NoMessageValues;
  quickHelp: NoMessageValues;
  copied: NoMessageValues;
  copyFields: NoMessageValues;
  connectorTab: NoMessageValues;
  connectorHelp: NoMessageValues;
  copySettings: NoMessageValues;
  roleViewer: NoMessageValues;
  roleViewerDescription: NoMessageValues;
  roleReviewer: NoMessageValues;
  roleReviewerDescription: NoMessageValues;
  roleAuthor: NoMessageValues;
  roleAuthorDescription: NoMessageValues;
  roleAdmin: NoMessageValues;
  roleAdminDescription: NoMessageValues;
  days: { readonly days: number };
  daysRecommended: { readonly days: number };
  expiryNever: NoMessageValues;
  expiryNeverDescription: NoMessageValues;
  overlapNone: NoMessageValues;
  loadFailed: { readonly error: string };
  emptyTitle: NoMessageValues;
  emptyBody: NoMessageValues;
  createCredential: NoMessageValues;
  noMatch: NoMessageValues;
  createFailed: { readonly error: string };
  saveFailed: { readonly error: string };
  rotateFailed: { readonly error: string };
  revokeFailed: { readonly error: string };
  saved: { readonly name: string };
  revoked: { readonly name: string };
  loading: NoMessageValues;
  adminOnly: NoMessageValues;
  notEnabledHeader: NoMessageValues;
  notEnabledBody: NoMessageValues;
  profiles: NoMessageValues;
  scopeApiGrants: NoMessageValues;
  scopeAdmin: NoMessageValues;
  scopeRequired: NoMessageValues;
  scopeOptional: NoMessageValues;
  chooseProfiles: NoMessageValues;
  allProfiles: NoMessageValues;
  writeTools: NoMessageValues;
  writeToolsDescription: NoMessageValues;
  allowProcess: NoMessageValues;
  cancel: NoMessageValues;
  signingInHeader: NoMessageValues;
  signingInUsers: NoMessageValues;
  signingInServices: NoMessageValues;
  loadingCredentials: NoMessageValues;
  tableDescription: NoMessageValues;
  refresh: NoMessageValues;
  edit: NoMessageValues;
  rotate: NoMessageValues;
  revoke: NoMessageValues;
  tableHeader: NoMessageValues;
  findCredentials: NoMessageValues;
  matches: { readonly count: string };
  name: NoMessageValues;
  role: NoMessageValues;
  all: NoMessageValues;
  yes: NoMessageValues;
  no: NoMessageValues;
  status: NoMessageValues;
  expires: NoMessageValues;
  expiresNever: NoMessageValues;
  lastUsed: NoMessageValues;
  createdBy: NoMessageValues;
  createHeader: NoMessageValues;
  create: NoMessageValues;
  nameDescription: NoMessageValues;
  roleDescription: NoMessageValues;
  adminWarning: NoMessageValues;
  neverWarning: NoMessageValues;
  editHeader: { readonly name: string };
  save: NoMessageValues;
  enabled: NoMessageValues;
  enabledDescription: NoMessageValues;
  enabledCheckbox: NoMessageValues;
  rotateHeader: { readonly name: string };
  rotateBody: NoMessageValues;
  overlapLabel: NoMessageValues;
  revokeHeader: { readonly name: string };
  revokeBody: NoMessageValues;
}>({
  snippetLine: {
    id: 'mcp.configuration.snippetLine',
    defaultMessage: '{label}: {value}',
    description:
      'One line of the Amazon Quick fields copied to the clipboard. {label} is the field name (MCP server endpoint, Client ID, Client secret, Token URL), {value} its value.',
  },
  credentialCreated: {
    id: 'mcp.configuration.credentialCreated',
    defaultMessage: 'Credential created',
    description: 'Header of the dialog that shows a newly created or rotated MCP credential and its secret.',
  },
  storedSecret: {
    id: 'mcp.configuration.storedSecret',
    defaultMessage: 'I have stored the secret',
    description: 'Button that closes the new-credential dialog; the secret is not shown again afterwards.',
  },
  secretShownOnce: {
    id: 'mcp.configuration.secretShownOnce',
    defaultMessage: 'This secret will not be shown again',
    description: 'Warning header in the new-credential dialog.',
  },
  copyItNow: {
    id: 'mcp.configuration.copyItNow',
    defaultMessage: 'Copy it now. It is also stored in AWS Secrets Manager, where an operator with access to that secret can read it.',
    description: 'Warning text in the new-credential dialog, under the header saying the secret will not be shown again.',
  },
  clientId: {
    id: 'mcp.configuration.clientId',
    defaultMessage: 'Client ID',
    description:
      'Label of the OAuth client ID of an MCP credential, in the new-credential dialog and as a column header of the credentials table.',
  },
  clientIdCopied: {
    id: 'mcp.configuration.clientIdCopied',
    defaultMessage: 'Client ID copied',
    description: 'Confirmation after the client ID is copied to the clipboard.',
  },
  couldNotCopy: {
    id: 'mcp.configuration.couldNotCopy',
    defaultMessage: 'Could not copy',
    description: 'Error shown when copying a value to the clipboard fails.',
  },
  copyClientId: {
    id: 'mcp.configuration.copyClientId',
    defaultMessage: 'Copy client ID',
    description: 'Screen-reader label of the button that copies the client ID.',
  },
  clientSecret: {
    id: 'mcp.configuration.clientSecret',
    defaultMessage: 'Client secret',
    description: 'Label of the OAuth client secret of a new MCP credential.',
  },
  clientSecretCopied: {
    id: 'mcp.configuration.clientSecretCopied',
    defaultMessage: 'Client secret copied',
    description: 'Confirmation after the client secret is copied to the clipboard.',
  },
  copyClientSecret: {
    id: 'mcp.configuration.copyClientSecret',
    defaultMessage: 'Copy client secret',
    description: 'Screen-reader label of the button that copies the client secret.',
  },
  tokenUrl: {
    id: 'mcp.configuration.tokenUrl',
    defaultMessage: 'Token URL',
    description: 'Label of the OAuth token endpoint URL MCP clients request tokens from.',
  },
  serverEndpoint: {
    id: 'mcp.configuration.serverEndpoint',
    defaultMessage: 'MCP server endpoint',
    description: 'Label of the URL of the MCP server.',
  },
  secretsManagerArn: {
    id: 'mcp.configuration.secretsManagerArn',
    defaultMessage: 'Secrets Manager ARN',
    description: 'Label of the identifier of the AWS Secrets Manager secret that holds the credential.',
  },
  quickTab: {
    id: 'mcp.configuration.quickTab',
    defaultMessage: 'Amazon Quick (service-to-service OAuth)',
    description: 'Tab in the new-credential dialog with the fields to enter in Amazon Quick. Amazon Quick is a product name.',
  },
  quickHelp: {
    id: 'mcp.configuration.quickHelp',
    defaultMessage: 'Add the IDP server as an MCP integration in Amazon Quick and choose service-to-service OAuth.',
    description: 'Instructions on the Amazon Quick tab of the new-credential dialog.',
  },
  copied: {
    id: 'mcp.configuration.copied',
    defaultMessage: 'Copied',
    description: 'Confirmation after a block of settings is copied to the clipboard.',
  },
  copyFields: {
    id: 'mcp.configuration.copyFields',
    defaultMessage: 'Copy fields',
    description: 'Button that copies the Amazon Quick fields to the clipboard.',
  },
  connectorTab: {
    id: 'mcp.configuration.connectorTab',
    defaultMessage: 'MCP Connector',
    description: 'Tab in the new-credential dialog with a settings entry for the IDP MCP Connector, a product component name.',
  },
  connectorHelp: {
    id: 'mcp.configuration.connectorHelp',
    defaultMessage: 'An MCP client settings entry for the IDP MCP Connector (Cline, Kiro or another stdio MCP client).',
    description: 'Instructions on the MCP Connector tab of the new-credential dialog. Cline and Kiro are product names.',
  },
  copySettings: {
    id: 'mcp.configuration.copySettings',
    defaultMessage: 'Copy settings',
    description: 'Button that copies the MCP Connector settings entry to the clipboard.',
  },
  roleViewer: {
    id: 'mcp.configuration.roleViewer',
    defaultMessage: 'Viewer',
    description: 'Name of the Viewer role, as an option for an MCP credential.',
  },
  roleViewerDescription: {
    id: 'mcp.configuration.roleViewerDescription',
    defaultMessage: 'Reads documents and results; may use search when unscoped',
    description: 'Description of the Viewer role option for an MCP credential.',
  },
  roleReviewer: {
    id: 'mcp.configuration.roleReviewer',
    defaultMessage: 'Reviewer',
    description: 'Name of the Reviewer role, as an option for an MCP credential.',
  },
  roleReviewerDescription: {
    id: 'mcp.configuration.roleReviewerDescription',
    defaultMessage: 'Reads documents and results',
    description: 'Description of the Reviewer role option for an MCP credential.',
  },
  roleAuthor: {
    id: 'mcp.configuration.roleAuthor',
    defaultMessage: 'Author',
    description: 'Name of the Author role, as an option for an MCP credential.',
  },
  roleAuthorDescription: {
    id: 'mcp.configuration.roleAuthorDescription',
    defaultMessage: 'Reads, and with write tools can process and reprocess',
    description: 'Description of the Author role option for an MCP credential.',
  },
  roleAdmin: {
    id: 'mcp.configuration.roleAdmin',
    defaultMessage: 'Admin',
    description: 'Name of the Admin role, as an option for an MCP credential.',
  },
  roleAdminDescription: {
    id: 'mcp.configuration.roleAdminDescription',
    defaultMessage: 'Every tool, every profile',
    description: 'Description of the Admin role option for an MCP credential.',
  },
  days: {
    id: 'mcp.configuration.days',
    defaultMessage: '{days, plural, one {# day} other {# days}}',
    description: 'An expiry or overlap period option for an MCP credential. {days} is the number of days.',
  },
  daysRecommended: {
    id: 'mcp.configuration.daysRecommended',
    defaultMessage: '{days, plural, one {# day} other {# days}} (recommended)',
    description: 'The recommended expiry period option for a new MCP credential. {days} is the number of days.',
  },
  expiryNever: {
    id: 'mcp.configuration.expiryNever',
    defaultMessage: 'Never',
    description: 'Expiry option for an MCP credential that does not expire.',
  },
  expiryNeverDescription: {
    id: 'mcp.configuration.expiryNeverDescription',
    defaultMessage: 'The credential keeps working until it is revoked',
    description: 'Description of the expiry option Never.',
  },
  overlapNone: {
    id: 'mcp.configuration.overlapNone',
    defaultMessage: 'Stop the old credential now',
    description: 'Rotation option: the old credential stops working immediately rather than after an overlap of some days.',
  },
  loadFailed: {
    id: 'mcp.configuration.loadFailed',
    defaultMessage: 'Failed to load MCP credentials: {error}',
    description: 'Error shown when the list of MCP credentials cannot be loaded. {error} is the error text from the server.',
  },
  emptyTitle: {
    id: 'mcp.configuration.emptyTitle',
    defaultMessage: 'No MCP credentials',
    description: 'Title shown in the credentials table when there are none.',
  },
  emptyBody: {
    id: 'mcp.configuration.emptyBody',
    defaultMessage:
      'Create a credential for an MCP client that signs in as a service, such as a shared Amazon Quick workflow or the MCP Connector.',
    description: 'Text shown in the credentials table when there are none.',
  },
  createCredential: {
    id: 'mcp.configuration.createCredential',
    defaultMessage: 'Create credential',
    description: 'Button that opens the dialog to create an MCP credential.',
  },
  noMatch: {
    id: 'mcp.configuration.noMatch',
    defaultMessage: 'No matching credentials',
    description: 'Shown in the credentials table when the filter matches nothing.',
  },
  createFailed: {
    id: 'mcp.configuration.createFailed',
    defaultMessage: 'Creating the credential failed: {error}',
    description: 'Error shown when creating an MCP credential fails. {error} is the error text from the server.',
  },
  saveFailed: {
    id: 'mcp.configuration.saveFailed',
    defaultMessage: 'Saving the credential failed: {error}',
    description: 'Error shown when saving changes to an MCP credential fails. {error} is the error text from the server.',
  },
  rotateFailed: {
    id: 'mcp.configuration.rotateFailed',
    defaultMessage: 'Rotating the credential failed: {error}',
    description: 'Error shown when rotating an MCP credential fails. {error} is the error text from the server.',
  },
  revokeFailed: {
    id: 'mcp.configuration.revokeFailed',
    defaultMessage: 'Revoking the credential failed: {error}',
    description: 'Error shown when revoking an MCP credential fails. {error} is the error text from the server.',
  },
  saved: {
    id: 'mcp.configuration.saved',
    defaultMessage: 'Saved {name}.',
    description: 'Confirmation after an MCP credential is saved. {name} is the credential name, or its client ID when it has none.',
  },
  revoked: {
    id: 'mcp.configuration.revoked',
    defaultMessage: 'Revoked {name}. Its callers are refused from their next request.',
    description: 'Confirmation after an MCP credential is revoked. {name} is the credential name, or its client ID when it has none.',
  },
  loading: {
    id: 'mcp.configuration.loading',
    defaultMessage: 'Loading…',
    description: 'Shown while the user role is being determined, before the MCP Configuration page renders.',
  },
  adminOnly: {
    id: 'mcp.configuration.adminOnly',
    defaultMessage: 'MCP Configuration is available to Admin users only.',
    description: 'Error shown on the MCP Configuration page to a user who is not an Admin.',
  },
  notEnabledHeader: {
    id: 'mcp.configuration.notEnabledHeader',
    defaultMessage: 'MCP is not enabled on this stack',
    description: 'Header shown on the MCP Configuration page when the stack has no MCP server.',
  },
  notEnabledBody: {
    id: 'mcp.configuration.notEnabledBody',
    defaultMessage: 'Update the stack with EnableMCP set to true to use the MCP server and manage its credentials here.',
    description:
      'Text shown on the MCP Configuration page when the stack has no MCP server. EnableMCP is a stack parameter name and stays as is.',
  },
  profiles: {
    id: 'mcp.configuration.profiles',
    defaultMessage: 'Configuration profiles',
    description: 'Label of the configuration profiles an MCP credential may see, in the credential form and as a column header.',
  },
  scopeApiGrants: {
    id: 'mcp.configuration.scopeApiGrants',
    defaultMessage: 'Tenants are granted by your entitlement system on this stack (setMcpCredentialTenant), not here.',
    description:
      'Help under the configuration profiles field when tenants are granted through the API. setMcpCredentialTenant is an API operation name and stays as is.',
  },
  scopeAdmin: {
    id: 'mcp.configuration.scopeAdmin',
    defaultMessage: 'An Admin credential is not limited to profiles.',
    description: 'Help under the configuration profiles field when the Admin role is chosen.',
  },
  scopeRequired: {
    id: 'mcp.configuration.scopeRequired',
    defaultMessage: 'Required: the profiles of one tenant this credential may see.',
    description: 'Help under the configuration profiles field when choosing profiles is required.',
  },
  scopeOptional: {
    id: 'mcp.configuration.scopeOptional',
    defaultMessage: 'Leave empty for every profile, or choose the profiles of one tenant.',
    description: 'Help under the configuration profiles field when choosing profiles is optional.',
  },
  chooseProfiles: {
    id: 'mcp.configuration.chooseProfiles',
    defaultMessage: 'Choose profiles',
    description: 'Placeholder of the configuration profiles field when choosing profiles is required.',
  },
  allProfiles: {
    id: 'mcp.configuration.allProfiles',
    defaultMessage: 'All profiles',
    description: 'Placeholder of the configuration profiles field, and the table cell value, when a credential is not limited to profiles.',
  },
  writeTools: {
    id: 'mcp.configuration.writeTools',
    defaultMessage: 'Write tools',
    description: 'Label of the setting that lets an MCP credential use the process and reprocess tools, and its column header.',
  },
  writeToolsDescription: {
    id: 'mcp.configuration.writeToolsDescription',
    defaultMessage: 'Lets callers start processing with the process and reprocess tools.',
    description: 'Help under the Write tools field.',
  },
  allowProcess: {
    id: 'mcp.configuration.allowProcess',
    defaultMessage: 'Allow process and reprocess',
    description: 'Checkbox under the Write tools field. process and reprocess are MCP tool names.',
  },
  cancel: {
    id: 'mcp.configuration.cancel',
    defaultMessage: 'Cancel',
    description: 'Button that closes a credential dialog without changes.',
  },
  signingInHeader: {
    id: 'mcp.configuration.signingInHeader',
    defaultMessage: 'Signing in to the MCP server',
    description: 'Header of the panel that explains how users and services sign in to the MCP server.',
  },
  signingInUsers: {
    id: 'mcp.configuration.signingInUsers',
    defaultMessage:
      "People sign in to the MCP server as themselves, through your MCP client's user authentication with the MCP client ID from the stack outputs; they then get their own role and configuration profiles, as in this console.",
    description: 'First paragraph of the sign-in panel, about people signing in.',
  },
  signingInServices: {
    id: 'mcp.configuration.signingInServices',
    defaultMessage:
      'Clients that sign in as a service, such as a shared Amazon Quick workflow or the MCP Connector, use a credential from this page. Each credential has its own role, profiles, expiry and secret, and is refused from its next request once revoked.',
    description: 'Second paragraph of the sign-in panel, about services signing in with a credential.',
  },
  loadingCredentials: {
    id: 'mcp.configuration.loadingCredentials',
    defaultMessage: 'Loading MCP credentials',
    description: 'Shown in the credentials table while it loads.',
  },
  tableDescription: {
    id: 'mcp.configuration.tableDescription',
    defaultMessage: 'Service credentials for MCP clients. Secrets are shown once, when a credential is created or rotated.',
    description: 'Description under the credentials table header.',
  },
  refresh: {
    id: 'mcp.configuration.refresh',
    defaultMessage: 'Refresh',
    description: 'Screen-reader label of the button that reloads the credentials table.',
  },
  edit: {
    id: 'mcp.configuration.edit',
    defaultMessage: 'Edit',
    description: 'Button that opens the dialog to edit the selected credential.',
  },
  rotate: {
    id: 'mcp.configuration.rotate',
    defaultMessage: 'Rotate',
    description: 'Button that rotates the selected credential, and the confirm button of the rotate dialog.',
  },
  revoke: {
    id: 'mcp.configuration.revoke',
    defaultMessage: 'Revoke',
    description: 'Button that revokes the selected credential, and the confirm button of the revoke dialog.',
  },
  tableHeader: {
    id: 'mcp.configuration.tableHeader',
    defaultMessage: 'MCP credentials',
    description: 'Header of the credentials table.',
  },
  findCredentials: {
    id: 'mcp.configuration.findCredentials',
    defaultMessage: 'Find credentials',
    description: 'Placeholder of the filter field above the credentials table.',
  },
  matches: {
    id: 'mcp.configuration.matches',
    defaultMessage: '{count} matches',
    description: 'Number of credentials the filter matches. {count} is the number.',
  },
  name: {
    id: 'mcp.configuration.name',
    defaultMessage: 'Name',
    description: 'Label of the credential name, in the form and as a column header.',
  },
  role: {
    id: 'mcp.configuration.role',
    defaultMessage: 'Role',
    description: 'Label of the credential role, in the form and as a column header.',
  },
  all: {
    id: 'mcp.configuration.all',
    defaultMessage: 'All',
    description: 'Table cell value for an Admin credential, which is not limited to configuration profiles.',
  },
  yes: {
    id: 'mcp.configuration.yes',
    defaultMessage: 'Yes',
    description: 'Table cell value: the credential may use the write tools.',
  },
  no: {
    id: 'mcp.configuration.no',
    defaultMessage: 'No',
    description: 'Table cell value: the credential may not use the write tools.',
  },
  status: {
    id: 'mcp.configuration.status',
    defaultMessage: 'Status',
    description: 'Column header for the credential status.',
  },
  expires: {
    id: 'mcp.configuration.expires',
    defaultMessage: 'Expires',
    description: 'Label of the credential expiry, in the form and as a column header.',
  },
  expiresNever: {
    id: 'mcp.configuration.expiresNever',
    defaultMessage: 'Never',
    description: 'Table cell value for a credential that does not expire.',
  },
  lastUsed: {
    id: 'mcp.configuration.lastUsed',
    defaultMessage: 'Last used',
    description: 'Column header for when the credential was last used.',
  },
  createdBy: {
    id: 'mcp.configuration.createdBy',
    defaultMessage: 'Created by',
    description: 'Column header for who created the credential.',
  },
  createHeader: {
    id: 'mcp.configuration.createHeader',
    defaultMessage: 'Create MCP credential',
    description: 'Header of the dialog that creates an MCP credential.',
  },
  create: {
    id: 'mcp.configuration.create',
    defaultMessage: 'Create',
    description: 'Confirm button of the dialog that creates an MCP credential.',
  },
  nameDescription: {
    id: 'mcp.configuration.nameDescription',
    defaultMessage: 'Shown on this page and in the audit log.',
    description: 'Help under the credential name field.',
  },
  roleDescription: {
    id: 'mcp.configuration.roleDescription',
    defaultMessage: 'What its callers may do, as for a user with that role.',
    description: 'Help under the credential role field.',
  },
  adminWarning: {
    id: 'mcp.configuration.adminWarning',
    defaultMessage: 'An Admin credential can use every tool on every profile. Prefer a narrower role.',
    description: 'Warning shown when the Admin role is chosen for a new credential.',
  },
  neverWarning: {
    id: 'mcp.configuration.neverWarning',
    defaultMessage: 'A credential that never expires keeps working until someone revokes it.',
    description: 'Warning shown when the expiry Never is chosen for a new credential.',
  },
  editHeader: {
    id: 'mcp.configuration.editHeader',
    defaultMessage: 'Edit {name}',
    description: 'Header of the dialog that edits a credential. {name} is the credential name, or its client ID when it has none.',
  },
  save: {
    id: 'mcp.configuration.save',
    defaultMessage: 'Save',
    description: 'Confirm button of the dialog that edits a credential.',
  },
  enabled: {
    id: 'mcp.configuration.enabled',
    defaultMessage: 'Enabled',
    description: 'Label of the setting that turns a credential on or off.',
  },
  enabledDescription: {
    id: 'mcp.configuration.enabledDescription',
    defaultMessage: 'A disabled credential is refused until it is enabled again.',
    description: 'Help under the Enabled field.',
  },
  enabledCheckbox: {
    id: 'mcp.configuration.enabledCheckbox',
    defaultMessage: 'Enabled',
    description: 'Checkbox under the Enabled field.',
  },
  rotateHeader: {
    id: 'mcp.configuration.rotateHeader',
    defaultMessage: 'Rotate {name}',
    description: 'Header of the dialog that rotates a credential. {name} is the credential name, or its client ID when it has none.',
  },
  rotateBody: {
    id: 'mcp.configuration.rotateBody',
    defaultMessage:
      'A new credential with the same role and profiles is created and its secret shown once. The current one keeps working for the time you choose, so callers can move across.',
    description: 'Explanation in the rotate dialog.',
  },
  overlapLabel: {
    id: 'mcp.configuration.overlapLabel',
    defaultMessage: 'Keep the current credential working for',
    description: 'Label of the overlap period field in the rotate dialog; the options are periods in days.',
  },
  revokeHeader: {
    id: 'mcp.configuration.revokeHeader',
    defaultMessage: 'Revoke {name}',
    description: 'Header of the dialog that revokes a credential. {name} is the credential name, or its client ID when it has none.',
  },
  revokeBody: {
    id: 'mcp.configuration.revokeBody',
    defaultMessage:
      'Callers using this credential are refused from their next request. Its app client is deleted and its secret is scheduled for deletion. This cannot be undone.',
    description: 'Explanation in the revoke dialog.',
  },
});

type CredentialAction = 'create' | 'save' | 'rotate' | 'revoke';

const FAILURE_MESSAGES = {
  create: messages.createFailed,
  save: messages.saveFailed,
  rotate: messages.rotateFailed,
  revoke: messages.revokeFailed,
} as const;

// Role names are the API's values; what a user reads comes from ROLE_MESSAGES.
type CredentialRole = 'Viewer' | 'Reviewer' | 'Author' | 'Admin';
const ADMIN_ROLE: CredentialRole = 'Admin';
const DEFAULT_ROLE: CredentialRole = 'Viewer';
const OFFERED_ROLES: readonly CredentialRole[] = ['Viewer', 'Reviewer', 'Author'];
const WRITE_ROLES: readonly string[] = ['Admin', 'Author'];
const ROLE_MESSAGES = {
  Viewer: { label: messages.roleViewer, description: messages.roleViewerDescription },
  Reviewer: { label: messages.roleReviewer, description: messages.roleReviewerDescription },
  Author: { label: messages.roleAuthor, description: messages.roleAuthorDescription },
  Admin: { label: messages.roleAdmin, description: messages.roleAdminDescription },
} as const;
const DEFAULT_EXPIRY_DAYS = '90';
const DEFAULT_OVERLAP_DAYS = '7';
const OVERLAP_DAYS = [0, 1, 2, 3, 5, 7];

const STATUS_TYPE: Record<string, 'success' | 'stopped' | 'error' | 'warning'> = {
  active: 'success',
  disabled: 'stopped',
  expired: 'warning',
  revoked: 'error',
};

const formatDate = (value?: string | null): string => (value ? new Date(value).toLocaleString() : '—');

/**
 * The Quick "Service-to-service OAuth" fields for one issued credential, one
 * `<field>: <value>` line each, with the field names in the UI's language.
 */
export const quickSnippet = (issued: McpCredentialIssued, intl: IntlShape): string =>
  [
    intl.formatMessage(messages.snippetLine, { label: intl.formatMessage(messages.serverEndpoint), value: issued.gatewayUrl ?? '' }),
    intl.formatMessage(messages.snippetLine, { label: intl.formatMessage(messages.clientId), value: issued.clientId }),
    intl.formatMessage(messages.snippetLine, { label: intl.formatMessage(messages.clientSecret), value: issued.clientSecret }),
    intl.formatMessage(messages.snippetLine, { label: intl.formatMessage(messages.tokenUrl), value: issued.tokenUrl ?? '' }),
  ].join('\n');

/** An MCP Connector entry (Cline, Kiro, Claude Desktop) for one issued credential. */
export const connectorSnippet = (issued: McpCredentialIssued): string =>
  JSON.stringify(
    {
      mcpServers: {
        'idp-mcp-server': {
          command: 'python3',
          args: ['-m', 'idp_mcp_connector'],
          env: {
            IDP_MCP_ENDPOINT: issued.gatewayUrl ?? '',
            IDP_MCP_TOKEN_URL: issued.tokenUrl ?? '',
            IDP_MCP_CLIENT_ID: issued.clientId,
            IDP_MCP_CLIENT_SECRET: issued.clientSecret,
          },
        },
      },
    },
    null,
    2,
  );

export const IssuedSecretModal = ({ issued, onDismiss }: { issued: McpCredentialIssued; onDismiss: () => void }): React.JSX.Element => {
  const intl = useIntl();
  const couldNotCopy = intl.formatMessage(messages.couldNotCopy);
  return (
    <Modal
      visible
      size="large"
      onDismiss={onDismiss}
      header={intl.formatMessage(messages.credentialCreated)}
      footer={
        <Box float="right">
          <Button variant="primary" onClick={onDismiss}>
            {intl.formatMessage(messages.storedSecret)}
          </Button>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <Alert type="warning" header={intl.formatMessage(messages.secretShownOnce)}>
          {intl.formatMessage(messages.copyItNow)}
        </Alert>
        <KeyValuePairs
          columns={1}
          items={[
            {
              label: intl.formatMessage(messages.clientId),
              value: (
                <CopyToClipboard
                  variant="inline"
                  textToCopy={issued.clientId}
                  copySuccessText={intl.formatMessage(messages.clientIdCopied)}
                  copyErrorText={couldNotCopy}
                  copyButtonAriaLabel={intl.formatMessage(messages.copyClientId)}
                />
              ),
            },
            {
              label: intl.formatMessage(messages.clientSecret),
              value: (
                <CopyToClipboard
                  variant="inline"
                  textToCopy={issued.clientSecret}
                  copySuccessText={intl.formatMessage(messages.clientSecretCopied)}
                  copyErrorText={couldNotCopy}
                  copyButtonAriaLabel={intl.formatMessage(messages.copyClientSecret)}
                />
              ),
            },
            { label: intl.formatMessage(messages.tokenUrl), value: issued.tokenUrl ?? '—' },
            { label: intl.formatMessage(messages.serverEndpoint), value: issued.gatewayUrl ?? '—' },
            { label: intl.formatMessage(messages.secretsManagerArn), value: issued.secretArn ?? '—' },
          ]}
        />
        <Tabs
          tabs={[
            {
              id: 'quick',
              label: intl.formatMessage(messages.quickTab),
              content: (
                <SpaceBetween size="xs">
                  <Box variant="p">{intl.formatMessage(messages.quickHelp)}</Box>
                  <CopyToClipboard
                    variant="button"
                    textToCopy={quickSnippet(issued, intl)}
                    copySuccessText={intl.formatMessage(messages.copied)}
                    copyErrorText={couldNotCopy}
                    copyButtonText={intl.formatMessage(messages.copyFields)}
                  />
                </SpaceBetween>
              ),
            },
            {
              id: 'connector',
              label: intl.formatMessage(messages.connectorTab),
              content: (
                <SpaceBetween size="xs">
                  <Box variant="p">{intl.formatMessage(messages.connectorHelp)}</Box>
                  <CopyToClipboard
                    variant="button"
                    textToCopy={connectorSnippet(issued)}
                    copySuccessText={intl.formatMessage(messages.copied)}
                    copyErrorText={couldNotCopy}
                    copyButtonText={intl.formatMessage(messages.copySettings)}
                  />
                </SpaceBetween>
              ),
            },
          ]}
        />
      </SpaceBetween>
    </Modal>
  );
};

const McpConfigurationLayout = (): React.JSX.Element => {
  const intl = useIntl();
  const { awsConfig } = useAppContext();
  const { settings } = useSettingsContext();
  const { isAdmin, loading: roleLoading } = useUserRole();
  const { isolationMode, grantSource, loadTenants } = useTenants();
  const { versions, fetchVersions } = useConfigurationVersions();
  const [items, setItems] = useState<McpCredential[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [selected, setSelected] = useState<McpCredential[]>([]);
  const [issued, setIssued] = useState<McpCredentialIssued | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [showEdit, setShowEdit] = useState(false);
  const [showRotate, setShowRotate] = useState(false);
  const [showRevoke, setShowRevoke] = useState(false);
  const [busy, setBusy] = useState(false);

  // Form state, shared by create and edit. The selects hold option values; the
  // options themselves are built at render time so their labels follow the locale.
  const [name, setName] = useState('');
  const [roleValue, setRoleValue] = useState<string>(DEFAULT_ROLE);
  const [scope, setScope] = useState<readonly Option[]>([]);
  const [writeTools, setWriteTools] = useState(false);
  const [expiryValue, setExpiryValue] = useState(DEFAULT_EXPIRY_DAYS);
  const [enabled, setEnabled] = useState(true);
  const [overlapValue, setOverlapValue] = useState(DEFAULT_OVERLAP_DAYS);

  const mcpEnabled = Boolean((settings as Record<string, unknown>)?.McpServerEndpoint);
  // Admin credentials are cross-tenant, so they are offered only where an unscoped
  // caller is unrestricted. The server refuses one anywhere else.
  const adminAllowed = isolationMode === 'Off' && grantSource === 'Manual';
  const apiGrants = grantSource === 'Api';
  const scopeRequired = isolationMode === 'Strict' || grantSource === 'IdpClaim';

  const roleOptions: Option[] = useMemo(
    () =>
      (adminAllowed ? [...OFFERED_ROLES, ADMIN_ROLE] : OFFERED_ROLES).map((value) => ({
        label: intl.formatMessage(ROLE_MESSAGES[value].label),
        value,
        description: intl.formatMessage(ROLE_MESSAGES[value].description),
      })),
    [adminAllowed, intl],
  );
  // A credential may hold a role this stack no longer offers (Admin once isolation
  // is on); it is shown under its stored name rather than dropped.
  const role: Option = useMemo(() => {
    const offered = roleOptions.find((o) => o.value === roleValue);
    return offered ? { label: offered.label, value: offered.value } : { label: roleValue, value: roleValue };
  }, [roleOptions, roleValue]);

  const expiryOptions: Option[] = useMemo(
    () => [
      { label: intl.formatMessage(messages.days, { days: 30 }), value: '30' },
      { label: intl.formatMessage(messages.daysRecommended, { days: 90 }), value: '90' },
      { label: intl.formatMessage(messages.days, { days: 180 }), value: '180' },
      { label: intl.formatMessage(messages.days, { days: 365 }), value: '365' },
      { label: intl.formatMessage(messages.expiryNever), value: '0', description: intl.formatMessage(messages.expiryNeverDescription) },
    ],
    [intl],
  );
  const expiry = expiryOptions.find((o) => o.value === expiryValue) ?? expiryOptions[1];

  const overlapOptions: Option[] = useMemo(
    () =>
      OVERLAP_DAYS.map((d) => ({
        label: d === 0 ? intl.formatMessage(messages.overlapNone) : intl.formatMessage(messages.days, { days: d }),
        value: String(d),
      })),
    [intl],
  );
  const overlap = overlapOptions.find((o) => o.value === overlapValue) ?? overlapOptions[overlapOptions.length - 1];

  const profileOptions: Option[] = useMemo(
    () =>
      [...versions]
        .sort((a, b) => a.versionName.localeCompare(b.versionName, undefined, { numeric: true, sensitivity: 'base' }))
        .map((v) => ({ label: v.versionName, value: v.versionName })),
    [versions],
  );

  const load = useCallback(async () => {
    if (!awsConfig) return;
    setLoading(true);
    setError('');
    try {
      const result = await generateClient().graphql({ query: listMcpCredentials });
      setItems(((result.data?.listMcpCredentials ?? []) as (McpCredential | null)[]).filter((c): c is McpCredential => Boolean(c)));
    } catch (err) {
      logger.error('Failed to load MCP credentials', err);
      setError(intl.formatMessage(messages.loadFailed, { error: getErrorMessage(err) }));
    } finally {
      setLoading(false);
    }
  }, [awsConfig, intl]);

  useEffect(() => {
    if (isAdmin && mcpEnabled) {
      load();
      fetchVersions();
      loadTenants();
    }
  }, [isAdmin, mcpEnabled, load]);

  const {
    items: shown,
    collectionProps,
    filterProps,
    paginationProps,
    filteredItemsCount,
  } = useCollection(items, {
    filtering: {
      empty: (
        <Box textAlign="center" color="inherit">
          <SpaceBetween size="xs">
            <b>{intl.formatMessage(messages.emptyTitle)}</b>
            <Box variant="p" color="inherit">
              {intl.formatMessage(messages.emptyBody)}
            </Box>
            <Button onClick={() => openCreate()}>{intl.formatMessage(messages.createCredential)}</Button>
          </SpaceBetween>
        </Box>
      ),
      noMatch: (
        <Box textAlign="center" color="inherit">
          <b>{intl.formatMessage(messages.noMatch)}</b>
        </Box>
      ),
    },
    pagination: { pageSize: 20 },
    sorting: {},
    selection: {},
  });

  const openCreate = () => {
    setName('');
    setRoleValue(roleOptions[0].value);
    setScope([]);
    setWriteTools(false);
    setExpiryValue(DEFAULT_EXPIRY_DAYS);
    setShowCreate(true);
  };

  const openEdit = () => {
    const c = selected[0];
    if (!c) return;
    setRoleValue(c.role ?? DEFAULT_ROLE);
    setScope((c.allowedConfigVersions ?? []).filter((v): v is string => Boolean(v)).map((v) => ({ label: v, value: v })));
    setWriteTools(Boolean(c.writeTools));
    setEnabled(Boolean(c.enabled));
    setShowEdit(true);
  };

  const run = async (action: CredentialAction, fn: () => Promise<void>) => {
    setBusy(true);
    setError('');
    setSuccess('');
    try {
      await fn();
    } catch (err) {
      logger.error(`Failed to ${action} the MCP credential`, err);
      setError(intl.formatMessage(FAILURE_MESSAGES[action], { error: getErrorMessage(err) }));
    } finally {
      setBusy(false);
    }
  };

  const submitCreate = () =>
    run('create', async () => {
      const result = await generateClient().graphql({
        query: createMcpCredential,
        variables: {
          name: name.trim(),
          role: role.value,
          allowedConfigVersions: apiGrants ? null : scope.map((s) => s.value),
          writeTools: WRITE_ROLES.includes(role.value) ? writeTools : false,
          expiresInDays: Number(expiry.value),
        },
      });
      setShowCreate(false);
      setIssued((result.data?.createMcpCredential ?? null) as McpCredentialIssued | null);
      await load();
    });

  const submitEdit = () =>
    run('save', async () => {
      const c = selected[0];
      await generateClient().graphql({
        query: updateMcpCredential,
        variables: {
          clientId: c.clientId,
          role: role.value,
          allowedConfigVersions: apiGrants || scope.length === 0 ? null : scope.map((s) => s.value),
          clearScope: !apiGrants && scope.length === 0,
          writeTools: WRITE_ROLES.includes(role.value) ? writeTools : false,
          enabled,
        },
      });
      setShowEdit(false);
      setSuccess(intl.formatMessage(messages.saved, { name: c.name ?? c.clientId }));
      await load();
    });

  const submitRotate = () =>
    run('rotate', async () => {
      const c = selected[0];
      const result = await generateClient().graphql({
        query: rotateMcpCredential,
        variables: { clientId: c.clientId, overlapDays: Number(overlap.value) },
      });
      setShowRotate(false);
      setIssued((result.data?.rotateMcpCredential ?? null) as McpCredentialIssued | null);
      await load();
    });

  const submitRevoke = () =>
    run('revoke', async () => {
      const c = selected[0];
      await generateClient().graphql({ query: revokeMcpCredential, variables: { clientId: c.clientId } });
      setShowRevoke(false);
      setSelected([]);
      setSuccess(intl.formatMessage(messages.revoked, { name: c.name ?? c.clientId }));
      await load();
    });

  if (roleLoading) {
    return <StatusIndicator type="loading">{intl.formatMessage(messages.loading)}</StatusIndicator>;
  }
  if (!isAdmin) {
    return <Alert type="error">{intl.formatMessage(messages.adminOnly)}</Alert>;
  }
  if (!mcpEnabled) {
    return (
      <Alert type="info" header={intl.formatMessage(messages.notEnabledHeader)}>
        {intl.formatMessage(messages.notEnabledBody)}
      </Alert>
    );
  }

  const one = selected.length === 1 ? selected[0] : null;
  const changeable = one && one.status !== 'revoked';
  const scopeField = (
    <FormField
      label={intl.formatMessage(messages.profiles)}
      description={intl.formatMessage(
        apiGrants
          ? messages.scopeApiGrants
          : role.value === ADMIN_ROLE
            ? messages.scopeAdmin
            : scopeRequired
              ? messages.scopeRequired
              : messages.scopeOptional,
      )}
    >
      <Multiselect
        selectedOptions={scope}
        onChange={({ detail }) => setScope(detail.selectedOptions as Option[])}
        options={profileOptions}
        disabled={apiGrants || role.value === ADMIN_ROLE}
        placeholder={intl.formatMessage(scopeRequired ? messages.chooseProfiles : messages.allProfiles)}
        filteringType="auto"
      />
    </FormField>
  );
  const writeField = WRITE_ROLES.includes(role.value) ? (
    <FormField label={intl.formatMessage(messages.writeTools)} description={intl.formatMessage(messages.writeToolsDescription)}>
      <Checkbox checked={writeTools} onChange={({ detail }) => setWriteTools(detail.checked)}>
        {intl.formatMessage(messages.allowProcess)}
      </Checkbox>
    </FormField>
  ) : null;
  // A refusal is shown inside the open dialog too: the page-level alert sits under
  // it, where an Admin working in the dialog cannot see it.
  const modalError = error ? <Alert type="error">{error}</Alert> : null;
  const formFooter = (onCancel: () => void, onSubmit: () => void, label: string, disabled = false) => (
    <Box float="right">
      <SpaceBetween direction="horizontal" size="xs">
        <Button variant="link" onClick={onCancel}>
          {intl.formatMessage(messages.cancel)}
        </Button>
        <Button variant="primary" loading={busy} disabled={disabled} onClick={onSubmit}>
          {label}
        </Button>
      </SpaceBetween>
    </Box>
  );

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError('')}>
          {error}
        </Alert>
      )}
      {success && (
        <Alert type="success" dismissible onDismiss={() => setSuccess('')}>
          {success}
        </Alert>
      )}
      <Container header={<Header variant="h2">{intl.formatMessage(messages.signingInHeader)}</Header>}>
        <SpaceBetween size="s">
          <Box variant="p">{intl.formatMessage(messages.signingInUsers)}</Box>
          <Box variant="p">{intl.formatMessage(messages.signingInServices)}</Box>
          <KeyValuePairs
            columns={2}
            items={[
              {
                label: intl.formatMessage(messages.serverEndpoint),
                value: String((settings as Record<string, unknown>)?.McpServerEndpoint ?? ''),
              },
              { label: intl.formatMessage(messages.tokenUrl), value: String((settings as Record<string, unknown>)?.McpTokenUrl ?? '—') },
            ]}
          />
        </SpaceBetween>
      </Container>
      <Table
        {...collectionProps}
        selectionType="single"
        selectedItems={selected}
        onSelectionChange={({ detail }) => setSelected(detail.selectedItems)}
        trackBy="clientId"
        loading={loading}
        loadingText={intl.formatMessage(messages.loadingCredentials)}
        items={shown}
        header={
          <Header
            variant="h2"
            counter={`(${items.length})`}
            description={intl.formatMessage(messages.tableDescription)}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button iconName="refresh" ariaLabel={intl.formatMessage(messages.refresh)} onClick={() => load()} />
                <Button disabled={!changeable} onClick={openEdit}>
                  {intl.formatMessage(messages.edit)}
                </Button>
                <Button disabled={!changeable || one?.status !== 'active'} onClick={() => setShowRotate(true)}>
                  {intl.formatMessage(messages.rotate)}
                </Button>
                <Button disabled={!changeable} onClick={() => setShowRevoke(true)}>
                  {intl.formatMessage(messages.revoke)}
                </Button>
                <Button variant="primary" onClick={openCreate}>
                  {intl.formatMessage(messages.createCredential)}
                </Button>
              </SpaceBetween>
            }
          >
            {intl.formatMessage(messages.tableHeader)}
          </Header>
        }
        filter={
          <TextFilter
            {...filterProps}
            filteringPlaceholder={intl.formatMessage(messages.findCredentials)}
            countText={intl.formatMessage(messages.matches, { count: String(filteredItemsCount ?? 0) })}
          />
        }
        pagination={<Pagination {...paginationProps} />}
        columnDefinitions={[
          { id: 'name', header: intl.formatMessage(messages.name), cell: (c) => c.name ?? '—', sortingField: 'name' },
          { id: 'clientId', header: intl.formatMessage(messages.clientId), cell: (c) => c.clientId },
          { id: 'role', header: intl.formatMessage(messages.role), cell: (c) => c.role ?? '—', sortingField: 'role' },
          {
            id: 'scope',
            header: intl.formatMessage(messages.profiles),
            cell: (c) =>
              (c.allowedConfigVersions ?? []).filter(Boolean).join(', ') ||
              intl.formatMessage(c.role === ADMIN_ROLE ? messages.all : messages.allProfiles),
          },
          {
            id: 'write',
            header: intl.formatMessage(messages.writeTools),
            cell: (c) => intl.formatMessage(c.writeTools ? messages.yes : messages.no),
          },
          {
            id: 'status',
            header: intl.formatMessage(messages.status),
            cell: (c) => <StatusIndicator type={STATUS_TYPE[c.status ?? ''] ?? 'info'}>{c.status ?? 'unknown'}</StatusIndicator>,
          },
          {
            id: 'expires',
            header: intl.formatMessage(messages.expires),
            cell: (c) => (c.expiresAt ? formatDate(c.expiresAt) : intl.formatMessage(messages.expiresNever)),
          },
          { id: 'lastUsed', header: intl.formatMessage(messages.lastUsed), cell: (c) => formatDate(c.lastUsedAt) },
          { id: 'createdBy', header: intl.formatMessage(messages.createdBy), cell: (c) => c.createdBy ?? '—' },
        ]}
      />

      {showCreate && (
        <Modal
          visible
          onDismiss={() => setShowCreate(false)}
          header={intl.formatMessage(messages.createHeader)}
          footer={formFooter(
            () => setShowCreate(false),
            submitCreate,
            intl.formatMessage(messages.create),
            !name.trim() || (scopeRequired && role.value !== ADMIN_ROLE && scope.length === 0 && !apiGrants),
          )}
        >
          <Form>
            <SpaceBetween size="m">
              {modalError}
              <FormField label={intl.formatMessage(messages.name)} description={intl.formatMessage(messages.nameDescription)}>
                <Input value={name} onChange={({ detail }) => setName(detail.value)} placeholder="nightly-quick-workflow" />
              </FormField>
              <FormField label={intl.formatMessage(messages.role)} description={intl.formatMessage(messages.roleDescription)}>
                <Select
                  selectedOption={role}
                  onChange={({ detail }) => setRoleValue(detail.selectedOption.value ?? DEFAULT_ROLE)}
                  options={roleOptions}
                />
              </FormField>
              {role.value === ADMIN_ROLE && <Alert type="warning">{intl.formatMessage(messages.adminWarning)}</Alert>}
              {scopeField}
              {writeField}
              <FormField label={intl.formatMessage(messages.expires)}>
                <Select
                  selectedOption={expiry}
                  onChange={({ detail }) => setExpiryValue(detail.selectedOption.value ?? DEFAULT_EXPIRY_DAYS)}
                  options={expiryOptions}
                />
              </FormField>
              {expiry.value === '0' && <Alert type="warning">{intl.formatMessage(messages.neverWarning)}</Alert>}
            </SpaceBetween>
          </Form>
        </Modal>
      )}

      {showEdit && one && (
        <Modal
          visible
          onDismiss={() => setShowEdit(false)}
          header={intl.formatMessage(messages.editHeader, { name: one.name ?? one.clientId })}
          footer={formFooter(() => setShowEdit(false), submitEdit, intl.formatMessage(messages.save))}
        >
          <SpaceBetween size="m">
            {modalError}
            <FormField label={intl.formatMessage(messages.role)}>
              <Select
                selectedOption={role}
                onChange={({ detail }) => setRoleValue(detail.selectedOption.value ?? DEFAULT_ROLE)}
                options={roleOptions}
              />
            </FormField>
            {scopeField}
            {writeField}
            <FormField label={intl.formatMessage(messages.enabled)} description={intl.formatMessage(messages.enabledDescription)}>
              <Checkbox checked={enabled} onChange={({ detail }) => setEnabled(detail.checked)}>
                {intl.formatMessage(messages.enabledCheckbox)}
              </Checkbox>
            </FormField>
          </SpaceBetween>
        </Modal>
      )}

      {showRotate && one && (
        <Modal
          visible
          onDismiss={() => setShowRotate(false)}
          header={intl.formatMessage(messages.rotateHeader, { name: one.name ?? one.clientId })}
          footer={formFooter(() => setShowRotate(false), submitRotate, intl.formatMessage(messages.rotate))}
        >
          <SpaceBetween size="m">
            {modalError}
            <Box variant="p">{intl.formatMessage(messages.rotateBody)}</Box>
            <FormField label={intl.formatMessage(messages.overlapLabel)}>
              <Select
                selectedOption={overlap}
                onChange={({ detail }) => setOverlapValue(detail.selectedOption.value ?? DEFAULT_OVERLAP_DAYS)}
                options={overlapOptions}
              />
            </FormField>
          </SpaceBetween>
        </Modal>
      )}

      {showRevoke && one && (
        <Modal
          visible
          onDismiss={() => setShowRevoke(false)}
          header={intl.formatMessage(messages.revokeHeader, { name: one.name ?? one.clientId })}
          footer={formFooter(() => setShowRevoke(false), submitRevoke, intl.formatMessage(messages.revoke))}
        >
          {modalError}
          {intl.formatMessage(messages.revokeBody)}
        </Modal>
      )}

      {issued && <IssuedSecretModal issued={issued} onDismiss={() => setIssued(null)} />}
    </SpaceBetween>
  );
};

export default McpConfigurationLayout;
