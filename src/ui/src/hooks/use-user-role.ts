// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { useState, useEffect } from 'react';
import { fetchSharedAuthSession } from '../api/auth-session';
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
 * the former limits which configuration profiles' documents a user sees, the latter
 * which test sets they may annotate.
 *
 * Every group name the app understands must appear in APP_GROUPS below: the
 * federated-login refresh path filters against it, so an omitted group makes a real
 * role look like no role at all.
 */

/** Cognito groups this app understands. Keep in sync with the roles above. */
const APP_GROUPS = ['Admin', 'Author', 'Reviewer', 'Annotator', 'Viewer'];

/** The scope half of the profile — the only part this hook reads. */
interface ProfileScope {
  allowedConfigVersions: string[] | null;
  allowedTestSets: string[] | null;
}

/**
 * One in-flight `getMyProfile` per signed-in user, shared by every mounted consumer.
 *
 * This hook is called by the navigation, the annotation landing page, the annotation
 * workspace, each feature page and more — all mounted at once, each previously running
 * its own effect, so one page load issued six identical profile calls (measured on a
 * live stack). The answer is per-user and does not change while the page is open.
 *
 * Keyed by user, not a bare boolean, because signing out and back in as someone else
 * is a normal thing to do here (it is how the annotator role gets tested) and a
 * key-less cache would hand the new session the previous user's scope — failing OPEN
 * for the least-privileged role, which is the one this scope exists to constrain.
 */
let profileScopeCache: { key: string; promise: Promise<ProfileScope> } | null = null;

/**
 * Drop the cached scope. For tests, mirroring `resetSharedAuthSession` — module state
 * otherwise leaks between cases and the second one asserts against the first's answer.
 */
const fetchProfileScopeUncached = async (): Promise<ProfileScope> => {
  const client = generateClient();
  const result = await client.graphql({ query: getMyProfile });
  const profile = result.data.getMyProfile;
  const versions = (profile?.allowedConfigVersions ?? []).filter((v): v is string => v !== null);
  const sets = (profile?.allowedTestSets ?? []).filter((v): v is string => v !== null);
  return {
    allowedConfigVersions: versions.length > 0 ? versions : null,
    allowedTestSets: sets.length > 0 ? sets : null,
  };
};

export const resetSharedProfileScope = (): void => {
  profileScopeCache = null;
};

const fetchProfileScopeShared = (key: string): Promise<ProfileScope> => {
  if (profileScopeCache?.key !== key) {
    const promise = fetchProfileScopeUncached().catch((err) => {
      // Do not cache a failure: the next mount should be able to retry rather than
      // inherit a permanent "unrestricted" default from one transient error.
      if (profileScopeCache?.key === key) profileScopeCache = null;
      throw err;
    });
    profileScopeCache = { key, promise };
  }
  return profileScopeCache.promise;
};
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
  /** True if user can delete configuration profiles (Admin only) */
  canDeleteConfig: boolean;
  /** True if user can perform HITL reviews (Admin or Reviewer) */
  canReview: boolean;
  /** True if user can annotate test-set ground truth (Admin, Author or Annotator) */
  canAnnotate: boolean;
  /** Config profiles the user is allowed to access. null/undefined = unrestricted (all versions). */
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
        const session = await fetchSharedAuthSession();
        const userGroups = session?.tokens?.idToken?.payload?.['cognito:groups'] || [];
        let groupsArray = Array.isArray(userGroups) ? (userGroups as string[]) : [userGroups as string];

        // For federated users on first login, groups may not be in the initial token.
        // Force a single token refresh to pick up groups assigned by the PreTokenGeneration Lambda.
        // This only runs once (empty deps array) so it won't cause excessive refresh calls.
        const isFederated = (session?.tokens?.idToken?.payload?.['identities'] as string | undefined) !== undefined;
        const appGroups = groupsArray.filter((g) => APP_GROUPS.includes(g));
        if (isFederated && appGroups.length === 0) {
          try {
            const refreshed = await fetchSharedAuthSession({ forceRefresh: true });
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
            // Shared across every mounted consumer of this hook — see
            // fetchProfileScopeShared. Keyed on the token's subject so a different
            // signed-in user never reads the previous one's scope.
            const subject = session?.tokens?.idToken?.payload?.sub as string | undefined;
            // A token with no subject cannot be keyed safely, so it is not cached at
            // all: a constant key would pool every such session into one bucket.
            const scope = subject ? await fetchProfileScopeShared(subject) : await fetchProfileScopeUncached();
            if (scope.allowedConfigVersions) setAllowedConfigVersions(scope.allowedConfigVersions);
            if (scope.allowedTestSets) setAllowedTestSets(scope.allowedTestSets);
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
