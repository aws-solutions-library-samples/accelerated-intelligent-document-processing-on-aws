// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState, useEffect, useCallback, useMemo } from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  Box,
  Button,
  Alert,
  Table,
  Modal,
  Form,
  FormField,
  Input,
  Select,
  Multiselect,
  StatusIndicator,
  Badge,
} from '@cloudscape-design/components';
import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';

import useUserRole from '../../hooks/use-user-role';
import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import {
  listUsers,
  createUser as createUserMutation,
  deleteUser as deleteUserMutation,
  updateUser as updateUserMutation,
  getTestSets,
} from '../../graphql/generated';
import { getErrorMessage } from '../../utils/errorUtils';

const logger = new ConsoleLogger('UserManagementLayout');

interface User {
  userId: string;
  email: string;
  persona: string;
  status?: string;
  createdAt?: string;
  allowedConfigVersions?: (string | null)[] | null;
  allowedTestSets?: (string | null)[] | null;
}

const UserManagementLayout = (): React.JSX.Element => {
  const { awsConfig } = useAppContext();
  const { settings } = useSettingsContext();
  const { isAdmin, loading: roleLoading } = useUserRole();
  const { versions, fetchVersions } = useConfigurationVersions();
  const [users, setUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showEditScopeModal, setShowEditScopeModal] = useState(false);
  const [editingUser, setEditingUser] = useState<User | null>(null);
  const [email, setEmail] = useState('');
  const [persona, setPersona] = useState('Reviewer');
  const [selectedConfigVersions, setSelectedConfigVersions] = useState<readonly { label: string; value: string }[]>([]);
  const [editScopeVersions, setEditScopeVersions] = useState<readonly { label: string; value: string }[]>([]);
  // Test-set scope for Annotators — a separate axis from config-version scope.
  const [selectedTestSets, setSelectedTestSets] = useState<readonly { label: string; value: string }[]>([]);
  const [editScopeTestSets, setEditScopeTestSets] = useState<readonly { label: string; value: string }[]>([]);
  const [testSetOptions, setTestSetOptions] = useState<{ label: string; value: string }[]>([]);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [emailError, setEmailError] = useState('');

  const allowedDomains = useMemo(() => {
    const domains = ((settings as Record<string, unknown>)?.AllowedSignUpEmailDomains as string) || '';
    return domains
      ? domains
          .split(',')
          .map((d) => d.trim().toLowerCase())
          .filter(Boolean)
      : [];
  }, [settings]);

  const personaOptions = [
    { label: 'Admin', value: 'Admin', description: 'Full access to all operations including user management' },
    { label: 'Author', value: 'Author', description: 'Read + write access to documents, configuration, tests, discovery' },
    { label: 'Reviewer', value: 'Reviewer', description: 'HITL review operations with filtered document visibility' },
    {
      label: 'Annotator',
      value: 'Annotator',
      description: 'Ground-truth annotation of assigned test sets only — cannot see other sets or run configurations',
    },
    { label: 'Viewer', value: 'Viewer', description: 'Read-only access to documents, configuration, and agent chat' },
  ];

  const configVersionOptions = useMemo(() => {
    return [...versions]
      .sort((a, b) => a.versionName.localeCompare(b.versionName, undefined, { numeric: true, sensitivity: 'base' }))
      .map((v) => ({
        label: v.versionName + (v.isActive ? ' (active)' : ''),
        value: v.versionName,
      }));
  }, [versions]);

  const validateEmail = useCallback(
    (emailValue: string): string => {
      if (!emailValue) {
        return '';
      }
      const emailPattern = /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/;
      if (!emailPattern.test(emailValue)) {
        return 'Invalid email format';
      }
      if (allowedDomains.length > 0) {
        const domain = emailValue.split('@')[1]?.toLowerCase();
        if (!allowedDomains.includes(domain)) {
          return `Email domain must be one of: ${allowedDomains.join(', ')}`;
        }
      }
      return '';
    },
    [allowedDomains],
  );

  const handleEmailChange = ({ detail }: { detail: { value: string } }): void => {
    setEmail(detail.value);
    setEmailError(validateEmail(detail.value));
  };

  /**
   * Test sets available to assign as an Annotator's scope. Loaded when the
   * create/edit modal opens rather than on mount, since only the Annotator persona
   * needs it.
   */
  const loadTestSets = useCallback(async () => {
    if (!awsConfig) return;
    try {
      const client = generateClient();
      const result = await client.graphql({ query: getTestSets });
      const sets = (result.data?.getTestSets ?? []) as Array<{ id?: string | null; name?: string | null } | null>;
      setTestSetOptions(
        sets.filter((t): t is { id: string; name?: string | null } => Boolean(t?.id)).map((t) => ({ label: t.name || t.id, value: t.id })),
      );
    } catch (err) {
      logger.warn('Could not load test sets for annotator scope:', err);
    }
  }, [awsConfig]);

  const loadUsers = useCallback(
    async (showRefreshing = false) => {
      if (!awsConfig) {
        logger.debug('AWS config not ready, skipping loadUsers');
        return;
      }

      if (showRefreshing) {
        setRefreshing(true);
      } else {
        setLoading(true);
      }
      setError('');

      try {
        const client = generateClient();
        logger.debug('Loading users...');
        const result = await client.graphql({ query: listUsers });
        const usersList =
          (((result as { data: Record<string, unknown> }).data?.listUsers as Record<string, unknown>)?.users as User[]) || [];
        logger.debug(`Loaded ${usersList.length} users`);
        setUsers(usersList);
      } catch (err) {
        logger.error('Failed to load users:', err);
        setError(`Failed to load users: ${getErrorMessage(err)}`);
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [awsConfig],
  );

  const createUser = async () => {
    if (!email) {
      setError('Email is required');
      return;
    }

    const validationError = validateEmail(email);
    if (validationError) {
      setEmailError(validationError);
      return;
    }

    // An Annotator with no assigned test set is denied every set by the server's
    // scope check, so the account could do nothing. Fail before the invite email.
    if (persona === 'Annotator' && selectedTestSets.length === 0) {
      setError('Assign at least one test set — an annotator with no assigned set cannot access anything');
      return;
    }

    if (!awsConfig) {
      setError('Configuration not ready');
      return;
    }

    setLoading(true);
    setError('');
    setSuccess('');

    try {
      const client = generateClient();
      const allowedConfigVersions = selectedConfigVersions.length > 0 ? selectedConfigVersions.map((opt) => opt.value) : undefined;
      const allowedTestSets = selectedTestSets.length > 0 ? selectedTestSets.map((opt) => opt.value) : undefined;
      logger.debug('Creating user:', { email, persona, allowedConfigVersions, allowedTestSets });
      await client.graphql({
        query: createUserMutation,
        variables: { email, persona, allowedConfigVersions, allowedTestSets },
      });

      logger.debug('User created successfully');
      setSuccess(`User ${email} created successfully`);
      setShowCreateModal(false);
      setEmail('');
      setPersona('Reviewer');
      setSelectedConfigVersions([]);
      setSelectedTestSets([]);
      await loadUsers();
    } catch (err) {
      logger.error('Failed to create user:', err);
      setError(`Failed to create user: ${getErrorMessage(err)}`);
    } finally {
      setLoading(false);
    }
  };

  const handleEditScope = (user: User) => {
    setEditingUser(user);
    // Pre-populate with current scope
    const currentScope = user.allowedConfigVersions?.filter((v): v is string => v !== null) || [];
    setEditScopeVersions(
      currentScope.map((v) => ({
        label: v,
        value: v,
      })),
    );
    const currentTestSets = user.allowedTestSets?.filter((v): v is string => v !== null) || [];
    setEditScopeTestSets(currentTestSets.map((v) => ({ label: v, value: v })));
    setShowEditScopeModal(true);
    fetchVersions();
    if (user.persona === 'Annotator') loadTestSets();
  };

  const saveEditScope = async () => {
    if (!editingUser || !awsConfig) return;

    if (editingUser.persona === 'Annotator' && editScopeTestSets.length === 0) {
      setError('Assign at least one test set — an annotator with no assigned set cannot access anything');
      return;
    }

    setLoading(true);
    setError('');
    setSuccess('');

    try {
      const client = generateClient();
      const allowedConfigVersions = editScopeVersions.length > 0 ? editScopeVersions.map((opt) => opt.value) : null;
      // Only send the test-set axis for Annotators: the resolver treats an
      // argument's *presence* as "change this axis", so sending null for a
      // non-annotator would clear a scope that was never shown.
      const variables: Record<string, unknown> = { userId: editingUser.userId, allowedConfigVersions };
      if (editingUser.persona === 'Annotator') {
        variables.allowedTestSets = editScopeTestSets.length > 0 ? editScopeTestSets.map((opt) => opt.value) : null;
      }
      logger.debug('Updating user scope:', variables);
      await client.graphql({
        query: updateUserMutation,
        variables,
      });

      logger.debug('User scope updated successfully');
      setSuccess(`Scope updated for ${editingUser.email}`);
      setShowEditScopeModal(false);
      setEditingUser(null);
      setEditScopeVersions([]);
      setEditScopeTestSets([]);
      await loadUsers();
    } catch (err) {
      logger.error('Failed to update user scope:', err);
      setError(`Failed to update scope: ${getErrorMessage(err)}`);
    } finally {
      setLoading(false);
    }
  };

  const deleteUser = async (userId: string, userEmail: string): Promise<void> => {
    if (!window.confirm(`Are you sure you want to delete user ${userEmail}?`)) {
      return;
    }

    if (!awsConfig) {
      setError('Configuration not ready');
      return;
    }

    setLoading(true);
    setError('');
    setSuccess('');

    try {
      const client = generateClient();
      logger.debug('Deleting user:', userId);
      await client.graphql({
        query: deleteUserMutation,
        variables: { userId },
      });

      logger.debug('User deleted successfully');
      setSuccess(`User ${userEmail} deleted successfully`);
      await loadUsers();
    } catch (err) {
      logger.error('Failed to delete user:', err);
      setError(`Failed to delete user: ${getErrorMessage(err)}`);
    } finally {
      setLoading(false);
    }
  };

  const handleCreateModalClose = () => {
    setShowCreateModal(false);
    setEmail('');
    setPersona('Reviewer');
    setSelectedConfigVersions([]);
    setError('');
    setEmailError('');
  };

  const handleCreateModalOpen = () => {
    setShowCreateModal(true);
    fetchVersions();
    loadTestSets();
  };

  const handleEditScopeModalClose = () => {
    setShowEditScopeModal(false);
    setEditingUser(null);
    setEditScopeVersions([]);
  };

  const handleRefresh = () => {
    loadUsers(true);
  };

  // Load users when awsConfig becomes available and user is admin
  useEffect(() => {
    if (awsConfig && isAdmin && !roleLoading) {
      loadUsers();
    }
  }, [awsConfig, isAdmin, roleLoading, loadUsers]);

  // Show loading if AWS config or role is not ready
  if (!awsConfig || roleLoading) {
    return (
      <Container>
        <Box textAlign="center" padding="xxl">
          <StatusIndicator type="loading">Loading user management...</StatusIndicator>
        </Box>
      </Container>
    );
  }

  if (!isAdmin) {
    return (
      <Container>
        <Alert type="error">Access Denied: You must be an administrator to access User Management.</Alert>
      </Container>
    );
  }

  const formatConfigVersions = (userVersions: (string | null)[] | null | undefined): React.ReactNode => {
    if (!userVersions || userVersions.length === 0) {
      return (
        <Box color="text-body-secondary">
          <em>All versions</em>
        </Box>
      );
    }
    const validVersions = userVersions.filter((v): v is string => v !== null);
    return (
      <SpaceBetween direction="horizontal" size="xxs">
        {validVersions.map((v) => (
          <Badge key={v} color="blue">
            {v}
          </Badge>
        ))}
      </SpaceBetween>
    );
  };

  const columnDefinitions = [
    {
      id: 'email',
      header: 'Email',
      cell: (item: User) => item.email,
      sortingField: 'email',
    },
    {
      id: 'persona',
      header: 'Role',
      cell: (item: User) => {
        const colorMap: Record<string, string> = {
          Admin: 'text-status-info',
          Author: 'text-status-success',
          Reviewer: 'text-body-default',
          Viewer: 'text-body-secondary',
        };
        return <Box {...({ color: colorMap[item.persona] || 'text-body-default' } as Record<string, unknown>)}>{item.persona}</Box>;
      },
      sortingField: 'persona',
    },
    {
      id: 'allowedConfigVersions',
      header: 'Config Profile Scope',
      cell: (item: User) => formatConfigVersions(item.allowedConfigVersions),
    },
    {
      id: 'allowedTestSets',
      header: 'Test Set Scope',
      cell: (item: User) => {
        // Only meaningful for Annotators; for every other role the axis is unused,
        // and rendering "All test sets" would wrongly imply they can annotate.
        if (item.persona !== 'Annotator') {
          return (
            <Box color="text-body-secondary">
              <em>—</em>
            </Box>
          );
        }
        const sets = (item.allowedTestSets ?? []).filter((v): v is string => v !== null);
        if (sets.length === 0) {
          return <StatusIndicator type="warning">None assigned</StatusIndicator>;
        }
        return (
          <SpaceBetween direction="horizontal" size="xxs">
            {sets.map((t) => (
              <Badge key={t} color="green">
                {t}
              </Badge>
            ))}
          </SpaceBetween>
        );
      },
    },
    {
      id: 'status',
      header: 'Status',
      cell: (item: User) => (
        <StatusIndicator type={item.status === 'active' ? 'success' : 'stopped'}>{item.status || 'active'}</StatusIndicator>
      ),
      sortingField: 'status',
    },
    {
      id: 'createdAt',
      header: 'Created',
      cell: (item: User) => (item.createdAt ? new Date(item.createdAt).toLocaleDateString() : 'N/A'),
      sortingField: 'createdAt',
    },
    {
      id: 'actions',
      header: 'Actions',
      cell: (item: User) => (
        <SpaceBetween direction="horizontal" size="xs">
          {item.persona !== 'Admin' && (
            <Button variant="link" onClick={() => handleEditScope(item)} disabled={loading || refreshing}>
              Edit scope
            </Button>
          )}
          <Button variant="link" onClick={() => deleteUser(item.userId, item.email)} disabled={loading || refreshing}>
            Delete
          </Button>
        </SpaceBetween>
      ),
    },
  ];

  return (
    <Container
      header={
        <Header
          variant="h1"
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button iconName="refresh" onClick={handleRefresh} loading={refreshing} disabled={loading}>
                Refresh
              </Button>
              <Button variant="primary" onClick={handleCreateModalOpen} disabled={loading || refreshing}>
                Create User
              </Button>
            </SpaceBetween>
          }
        >
          User Management
        </Header>
      }
    >
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

        <Table
          columnDefinitions={columnDefinitions}
          items={users}
          loading={loading}
          loadingText="Loading users..."
          sortingDisabled={loading || refreshing}
          empty={
            <Box textAlign="center" color="inherit">
              <Box variant="strong" textAlign="center" color="inherit">
                No users found
              </Box>
              <Box variant="p" padding={{ bottom: 's' }} textAlign="center" color="inherit">
                Create your first user to get started.
              </Box>
              <Button onClick={handleCreateModalOpen}>Create User</Button>
            </Box>
          }
          header={
            <Header counter={`(${users.length})`} description="Manage users and their roles in the system">
              Users
            </Header>
          }
        />

        {/* Create User Modal */}
        <Modal
          visible={showCreateModal}
          onDismiss={handleCreateModalClose}
          header="Create New User"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={handleCreateModalClose}>
                  Cancel
                </Button>
                <Button variant="primary" onClick={createUser} loading={loading}>
                  Create User
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <Form>
            <SpaceBetween size="l">
              <FormField
                label="Email Address"
                errorText={emailError}
                description={
                  allowedDomains.length > 0
                    ? `Allowed domains: ${allowedDomains.join(', ')}`
                    : 'User will receive an email with temporary password'
                }
                constraintText={allowedDomains.length > 0 ? 'Email must use an allowed domain' : ''}
              >
                <Input value={email} onChange={handleEmailChange} placeholder="user@example.com" type="email" />
              </FormField>
              <FormField label="Role" description="Select the role that defines what this user can access and modify">
                <Select
                  selectedOption={personaOptions.find((opt) => opt.value === persona) ?? null}
                  onChange={({ detail }) => setPersona(detail.selectedOption.value ?? '')}
                  options={personaOptions}
                />
              </FormField>
              <FormField
                label={
                  <span>
                    Configuration Profile Scope <em>- optional</em>
                  </span>
                }
                description="Restrict this user to specific configuration profiles. Leave empty for unrestricted access to all versions."
              >
                <Multiselect
                  selectedOptions={selectedConfigVersions}
                  onChange={({ detail }) => setSelectedConfigVersions(detail.selectedOptions as { label: string; value: string }[])}
                  options={configVersionOptions}
                  placeholder="All profiles (unrestricted)"
                  filteringType="auto"
                  tokenLimit={3}
                />
              </FormField>
              {/* Required rather than optional: an annotator with no assigned set is
                  denied every set by the server, so the account could do nothing. */}
              {persona === 'Annotator' && (
                <FormField
                  label="Assigned test sets"
                  description="The test set(s) this annotator may open and annotate. They will not see any other test set."
                  errorText={selectedTestSets.length === 0 ? 'An annotator must be assigned at least one test set' : ''}
                >
                  <Multiselect
                    selectedOptions={selectedTestSets}
                    onChange={({ detail }) => setSelectedTestSets(detail.selectedOptions as { label: string; value: string }[])}
                    options={testSetOptions}
                    placeholder="Choose test sets"
                    filteringType="auto"
                    tokenLimit={3}
                    empty="No test sets found"
                  />
                </FormField>
              )}
            </SpaceBetween>
          </Form>
        </Modal>

        {/* Edit Scope Modal */}
        <Modal
          visible={showEditScopeModal}
          onDismiss={handleEditScopeModalClose}
          header={`Edit access scope — ${editingUser?.email ?? ''}`}
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={handleEditScopeModalClose}>
                  Cancel
                </Button>
                <Button variant="primary" onClick={saveEditScope} loading={loading}>
                  Save Scope
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <Form>
            <SpaceBetween size="l">
              <FormField
                label="Configuration Profile Scope"
                description="Select which configuration profiles this user can access. Clear all to give unrestricted access."
              >
                <Multiselect
                  selectedOptions={editScopeVersions}
                  onChange={({ detail }) => setEditScopeVersions(detail.selectedOptions as { label: string; value: string }[])}
                  options={configVersionOptions}
                  placeholder="All profiles (unrestricted)"
                  filteringType="auto"
                  tokenLimit={3}
                />
              </FormField>
              {editingUser?.persona === 'Annotator' && (
                <FormField
                  label="Assigned test sets"
                  description="Test sets this annotator may open. Clearing this revokes access to every set — it does not grant access to all of them."
                  errorText={editScopeTestSets.length === 0 ? 'Clearing this leaves the annotator unable to access anything' : ''}
                >
                  <Multiselect
                    selectedOptions={editScopeTestSets}
                    onChange={({ detail }) => setEditScopeTestSets(detail.selectedOptions as { label: string; value: string }[])}
                    options={testSetOptions}
                    placeholder="Choose test sets"
                    filteringType="auto"
                    tokenLimit={3}
                    empty="No test sets found"
                  />
                </FormField>
              )}
            </SpaceBetween>
          </Form>
        </Modal>
      </SpaceBetween>
    </Container>
  );
};

export default UserManagementLayout;
