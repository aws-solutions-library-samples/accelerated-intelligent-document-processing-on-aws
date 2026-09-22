// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { useState, useEffect, useCallback } from 'react';
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

/**
 * Cognito groups this app understands — every `AWS::Cognito::UserPoolGroup` in
 * `template.yaml`.
 *
 * ⚠️ Not cosmetic. `hasNoRole` is computed from this list and gates the whole
 * application, so a group missing here is locked out of a UI the server would let
 * it use: the dispatcher's manifest is generated from the template, so a new group
 * is granted the `ANY_GROUP` operations the moment it exists. Exported so
 * `__tests__/use-user-role.appGroups.test.ts` can assert it against the template.
 */
export const APP_GROUPS = ['Admin', 'Author', 'Reviewer', 'Annotator', 'Viewer'];

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
  /**
   * True once loading has finished and the caller is in NONE of `APP_GROUPS`.
   *
   * This is not the same as `groups.length === 0`: the claim can carry a group
   * this app does not recognise (an IdP-mapped name, say), which grants nothing
   * here, so the test is membership of the app's own vocabulary. Self-service
   * sign-up is what produces such an account — with `AllowedSignUpEmailDomain`
   * set, the user pool allows self-registration and the new user is in no group
   * until an administrator assigns one.
   *
   * It matters because the API refuses such a caller the eighteen operations
   * declared `ANY_GROUP` in `scripts/api_rbac_expectations.yaml` — every document
   * read, the chat transcript read, the processing-breaker badge and three
   * mutations — with 403. Not every operation: 8 remain `ANY` (the caller's own
   * profile and own chat sessions, the published release number, the two
   * fine-tuning job reads and the three feature-platform reads), which is why
   * this flag gates the app rather than being consulted per call. The server is
   * the authority either way; the flag only lets the UI say once and clearly what
   * it would otherwise discover one failing page at a time.
   *
   * It requires a **successful** read of the session, not merely a finished one.
   * False while `loading`, and false when `sessionError` is set — "I could not
   * find out what your groups are" is a different statement from "you have none",
   * and only one of them is the user's problem to act on.
   */
  hasNoRole: boolean;
  /**
   * The auth session could not be read, so the caller's groups are unknown.
   *
   * Distinct from `hasNoRole` because the remedies are opposite: this one is
   * usually transient and clears on a reload, and telling its victim to go and
   * ask an administrator for a role sends a perfectly entitled Admin to someone
   * with nothing to fix. `api/auth-session.ts` documents the live case — two
   * byte-identical `GetCredentialsForIdentity` calls in the same second, one 200
   * and one `400 NotAuthorizedException: Invalid login token` on a valid token —
   * and since that module shares one in-flight promise across ~15 consumers, a
   * single rejection reaches every one of them. This effect has an empty
   * dependency array, so nothing retries it for the life of the mount.
   */
  sessionError: boolean;
  /**
   * Re-run the session read after a `sessionError`.
   *
   * Worth offering rather than only a page reload: `fetchSharedAuthSession` clears
   * its in-flight slot in a `finally`, so a rejection is **not** cached and a retry
   * issues a genuinely new `fetchAuthSession` — a reload would work too, but it
   * discards the rest of the app's state to do the same thing.
   */
  retrySession: () => void;
  loading: boolean;
}

const useUserRole = (): UserRoleReturn => {
  const [groups, setGroups] = useState<string[]>([]);
  const [allowedConfigVersions, setAllowedConfigVersions] = useState<string[] | null>(null);
  const [allowedTestSets, setAllowedTestSets] = useState<string[] | null>(null);
  const [sessionError, setSessionError] = useState(false);
  const [loading, setLoading] = useState(true);
  // Bumped by `retrySession`, and the effect's only dependency. The effect is still
  // once-per-mount for every caller that never retries.
  const [attempt, setAttempt] = useState(0);

  const retrySession = useCallback(() => {
    setSessionError(false);
    setLoading(true);
    setAttempt((n) => n + 1);
  }, []);

  useEffect(() => {
    const fetchUserData = async () => {
      try {
        // Fetch Cognito groups from auth session
        const session = await fetchSharedAuthSession();
        // A resolved session with no ID token is "the groups are UNKNOWN", not
        // "there are none". Reachable in Amplify v6 — cached Identity Pool
        // credentials can still be valid while the ID token is no longer
        // refreshable — and it RESOLVES rather than rejecting, so the outer catch
        // never sees it and `sessionError` would otherwise stay false while
        // `hasNoRole` went true.
        if (!session?.tokens?.idToken) {
          console.warn('Auth session carries no ID token; the caller groups are unknown');
          setSessionError(true);
          setLoading(false);
          return;
        }
        const userGroups = session.tokens.idToken.payload?.['cognito:groups'] || [];
        let groupsArray = Array.isArray(userGroups) ? (userGroups as string[]) : [userGroups as string];

        // A fallback for a federated token that arrives with no app group, not a
        // routine first-login step: the PreTokenGeneration trigger writes the groups
        // into the FIRST token (it emits both `claimsOverrideDetails` and
        // `claimsAndScopeOverrideDetails`, so the override is honoured whichever
        // LambdaVersion the pool is on — see the trigger in `template.yaml`). What
        // still reaches this branch is a deployment with `ExternalIdPGroupMapping`
        // off, a mapping that assigned nothing, or a group granted after the token
        // was issued. Force a single refresh, once per mount (the effect re-runs only
        // on an explicit retry), so this will not cause excessive refresh calls.
        const isFederated = (session?.tokens?.idToken?.payload?.['identities'] as string | undefined) !== undefined;
        const appGroups = groupsArray.filter((g) => APP_GROUPS.includes(g));
        if (isFederated && appGroups.length === 0) {
          try {
            const refreshed = await fetchSharedAuthSession({ forceRefresh: true });
            const refreshedGroups = refreshed?.tokens?.idToken?.payload?.['cognito:groups'] || [];
            groupsArray = Array.isArray(refreshedGroups) ? (refreshedGroups as string[]) : [refreshedGroups as string];
          } catch (refreshErr) {
            // This branch is reached only when the caller IS federated and holds no
            // app group yet — precisely the state where "no group" and "the group
            // claim has not arrived in the token yet" are indistinguishable, so it
            // is the last place to assume the former. The forced refresh shares one
            // in-flight slot across every consumer of this hook, so a single
            // `400 NotAuthorizedException` reaches all of them at once, and the
            // empty dependency array means nothing retries for the life of the
            // mount. Report it as unknown, like the outer catch.
            console.warn('Token refresh for federated group sync failed:', refreshErr);
            setSessionError(true);
            setLoading(false);
            return;
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
        // The only statement in the `try` above that can reach here is
        // `fetchSharedAuthSession()` — everything after it is null-safe or has its
        // own inner catch. So this means "the caller's groups are UNKNOWN", not
        // "the caller has none", and the two must not be conflated: `hasNoRole`
        // now drives a full-screen message telling the user to ask an
        // administrator for a role, which is the wrong thing to say to an Admin
        // whose session read lost a race at Cognito.
        console.error('Error fetching user role:', error);
        setSessionError(true);
        setGroups([]);
      } finally {
        setLoading(false);
      }
    };
    fetchUserData();
    // `attempt` only changes when the user explicitly retries after a
    // `sessionError`, so this remains one read per mount in the normal case.
  }, [attempt]);

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
  // Membership of APP_GROUPS, not `groups.length`: an unrecognised group name
  // grants nothing in this app, so it must not read as a role. Gated on
  // `!loading` so the flag is never true merely because the session has not
  // resolved yet, and on `!sessionError` so a failed read is never reported as an
  // empty one — see the `sessionError` note above.
  //
  // ⚠️ Because this gates the whole application, APP_GROUPS must list every group
  // `template.yaml` creates. A sixth `AWS::Cognito::UserPoolGroup` omitted here
  // would be granted the `ANY_GROUP` operations by the server (the dispatcher's
  // manifest is generated from the template) and then be shown "your account has
  // not been granted access yet" by this flag — worse than the pre-existing
  // fall-through to the Viewer navigation. `use-user-role.appGroups.test.ts`
  // asserts the list against the template so that cannot happen silently.
  const hasNoRole = !loading && !sessionError && !groups.some((g) => APP_GROUPS.includes(g));

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
    hasNoRole,
    sessionError,
    retrySession,
    loading,
  };
};

export default useUserRole;
