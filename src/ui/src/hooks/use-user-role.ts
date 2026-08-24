// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { useState, useEffect } from 'react';
import { fetchAuthSession } from 'aws-amplify/auth';
import { generateClient } from '../api/client-shim';
import { getMyProfile } from '../graphql/generated';

/**
 * RBAC Role Definitions:
 *   Admin    - Full access to all operations
 *   Author   - Read + write (documents, configuration, tests, discovery)
 *   Reviewer - HITL review operations + limited document list (server-side filtered)
 *   Annotator- Ground-truth annotation of assigned test sets ONLY (least privilege)
 *   Viewer   - Read-only access to documents, config, agent chat, code explorer
 *
 * Users can be in multiple groups (union of permissions applies).
 * Users can optionally have allowedConfigVersions for config-version scoping and
 * allowedTestSets for test-set annotation scoping. These are independent axes:
 * the former limits which config versions' documents a user sees, the latter
 * which test sets they may annotate.
 *
 * Every group name the app understands must appear in APP_GROUPS below: the
 * federated-login refresh path filters against it, so an omitted group makes a real
 * role look like no role at all.
 */

/** Cognito groups this app understands. Keep in sync with the roles above. */
const APP_GROUPS = ['Admin', 'Author', 'Reviewer', 'Annotator', 'Viewer'];
interface UserRoleReturn {
  groups: string[];
  isAdmin: boolean;
  isAuthor: boolean;
  isReviewer: boolean;
  isAnnotator: boolean;
  isViewer: boolean;
  /** True if user is ONLY in the Reviewer group (no Admin/Author/Viewer) */
  isReviewerOnly: boolean;
  /**
   * True if user is ONLY in the Annotator group. These users get a single-link nav
   * into their assigned test set's queue rather than the document list.
   */
  isAnnotatorOnly: boolean;
  /** True if user is ONLY in the Viewer group (no Admin/Author) */
  isViewerOnly: boolean;
  /** True if user can write (Admin or Author) */
  canWrite: boolean;
  /** True if user can manage users (Admin only) */
  canManageUsers: boolean;
  /** True if user can delete config versions (Admin only) */
  canDeleteConfig: boolean;
  /** True if user can perform HITL reviews (Admin or Reviewer) */
  canReview: boolean;
  /** True if user can annotate test-set ground truth (Admin, Author or Annotator) */
  canAnnotate: boolean;
  /** Config versions the user is allowed to access. null/undefined = unrestricted (all versions). */
  allowedConfigVersions: string[] | null;
  /**
   * Test sets an Annotator is scoped to. null = unrestricted for Admin/Author;
   * for an Annotator a null/empty scope means they are assigned nothing and the
   * server denies every test set (the scope check fails closed).
   */
  allowedTestSets: string[] | null;
  loading: boolean;
}

const useUserRole = (): UserRoleReturn => {
  const [groups, setGroups] = useState<string[]>([]);
  const [allowedConfigVersions, setAllowedConfigVersions] = useState<string[] | null>(null);
  const [allowedTestSets, setAllowedTestSets] = useState<string[] | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchUserData = async () => {
      try {
        // Fetch Cognito groups from auth session
        const session = await fetchAuthSession();
        const userGroups = session?.tokens?.idToken?.payload?.['cognito:groups'] || [];
        let groupsArray = Array.isArray(userGroups) ? (userGroups as string[]) : [userGroups as string];

        // For federated users on first login, groups may not be in the initial token.
        // Force a single token refresh to pick up groups assigned by the PreTokenGeneration Lambda.
        // This only runs once (empty deps array) so it won't cause excessive refresh calls.
        const isFederated = (session?.tokens?.idToken?.payload?.['identities'] as string | undefined) !== undefined;
        const appGroups = groupsArray.filter((g) => APP_GROUPS.includes(g));
        if (isFederated && appGroups.length === 0) {
          try {
            const refreshed = await fetchAuthSession({ forceRefresh: true });
            const refreshedGroups = refreshed?.tokens?.idToken?.payload?.['cognito:groups'] || [];
            groupsArray = Array.isArray(refreshedGroups) ? (refreshedGroups as string[]) : [refreshedGroups as string];
          } catch (refreshErr) {
            console.warn('Token refresh for federated group sync failed:', refreshErr);
          }
        }

        setGroups(groupsArray);

        // Fetch user profile for allowedConfigVersions (skip for Admin - always unrestricted)
        if (!groupsArray.includes('Admin')) {
          try {
            const client = generateClient();
            const result = await client.graphql({ query: getMyProfile });
            const profile = result.data.getMyProfile;
            if (profile?.allowedConfigVersions && profile.allowedConfigVersions.length > 0) {
              const versions = profile.allowedConfigVersions.filter((v): v is string => v !== null);
              setAllowedConfigVersions(versions.length > 0 ? versions : null);
            }
            if (profile?.allowedTestSets && profile.allowedTestSets.length > 0) {
              const sets = profile.allowedTestSets.filter((v): v is string => v !== null);
              setAllowedTestSets(sets.length > 0 ? sets : null);
            }
          } catch (profileErr) {
            console.warn('Could not fetch user profile for scope:', profileErr);
            // Non-critical - default to unrestricted
          }
        }
      } catch (error) {
        console.error('Error fetching user role:', error);
        setGroups([]);
      } finally {
        setLoading(false);
      }
    };
    fetchUserData();
  }, []);

  const isAdmin = groups.includes('Admin');
  const isAuthor = groups.includes('Author');
  const isReviewer = groups.includes('Reviewer');
  const isAnnotator = groups.includes('Annotator');
  const isViewer = groups.includes('Viewer');

  // Derived convenience flags
  const isReviewerOnly = isReviewer && !isAdmin && !isAuthor && !isViewer;
  const isAnnotatorOnly = isAnnotator && !isAdmin && !isAuthor && !isReviewer && !isViewer;
  const isViewerOnly = isViewer && !isAdmin && !isAuthor;
  const canWrite = isAdmin || isAuthor;
  const canManageUsers = isAdmin;
  const canDeleteConfig = isAdmin;
  const canReview = isAdmin || isReviewer;
  const canAnnotate = isAdmin || isAuthor || isAnnotator;

  return {
    groups,
    isAdmin,
    isAuthor,
    isReviewer,
    isAnnotator,
    isViewer,
    isReviewerOnly,
    isAnnotatorOnly,
    isViewerOnly,
    canWrite,
    canManageUsers,
    canDeleteConfig,
    canReview,
    canAnnotate,
    allowedConfigVersions,
    allowedTestSets,
    loading,
  };
};

export default useUserRole;
