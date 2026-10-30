// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
export const LOGIN_PATH = '/login';
export const LOGOUT_PATH = '/logout';
export const DOCUMENTS_PATH = '/documents';
export const TEST_STUDIO_PATH = '/test-studio';
export const DEFAULT_PATH = DOCUMENTS_PATH;
export const REDIRECT_URL_PARAM = 'redirect';
export const DOCUMENTS_KB_QUERY_PATH = `${DOCUMENTS_PATH}/query`;
export const DOCUMENTS_ANALYTICS_PATH = `${DOCUMENTS_PATH}/agents`;
export const CONFIGURATION_PATH = `${DOCUMENTS_PATH}/config`;
export const PRICING_PATH = `${DOCUMENTS_PATH}/pricing`;
export const MODEL_CONFIG_LIMITS_PATH = `${DOCUMENTS_PATH}/model-limits`;
export const UPLOAD_DOCUMENT_PATH = `${DOCUMENTS_PATH}/upload`;
export const DISCOVERY_PATH = `${DOCUMENTS_PATH}/discovery`;
export const DISCOVERY_JOB_PATH = `${DOCUMENTS_PATH}/discovery/job`;
export const USER_MANAGEMENT_PATH = `${DOCUMENTS_PATH}/users`;
export const CONFIG_PREFIX_MAPPINGS_PATH = `${DOCUMENTS_PATH}/prefix-mappings`;
export const AGENT_CHAT_PATH = '/agentchat';
export const WELCOME_PATH = '/welcome';
/** localStorage key: set when the user dismisses the welcome landing page. */
export const WELCOME_DISMISSED_KEY = 'idp-welcome-dismissed';
export const CAPACITY_PLANNING_PATH = `${DOCUMENTS_PATH}/capacity-planning`;
/** Route pattern base: /test-studio/sets/:testSetId */
export const TEST_SET_DETAIL_PATH = `${TEST_STUDIO_PATH}/sets`;
/** Hash-link helper: href for a test set's document browser. */
export const testSetDetailHref = (testSetId: string): string => `#${TEST_SET_DETAIL_PATH}/${encodeURIComponent(testSetId)}`;
/** HITL: one test set's queue page (route pattern base). */
export const TEST_SET_QUEUE_PATH = '/review-queues/test-sets';
/** Route path of a test set's queue page. */
export const testSetQueuePath = (testSetId: string): string => `${TEST_SET_QUEUE_PATH}/${encodeURIComponent(testSetId)}`;
/** Route path of one test-set document's annotation workspace. */
export const testSetItemPath = (testSetId: string, objectKey: string): string =>
  `${testSetQueuePath(testSetId)}/items/${encodeURIComponent(objectKey)}`;
/**
 * Hash-link helper: href for a test set's queue under Review Studio › Review Management, or
 * straight to one document's edit screen. This link only navigates; access is enforced
 * server-side against the user's allowedTestSets, so it grants nothing on its own
 * and is safe to share with a scoped annotator.
 */
export const testSetAnnotateHref = (testSetId: string, objectKey?: string, from?: EditOrigin): string =>
  `#${objectKey ? testSetItemPath(testSetId, objectKey) : testSetQueuePath(testSetId)}${objectKey && from ? `?from=${from}` : ''}`;
/**
 * Landing page for an annotator assigned to more than one test set (or before
 * their scope has resolved). Lists their queues; a single-set annotator is sent
 * straight to the queue instead.
 */
export const ANNOTATE_LANDING_PATH = `${TEST_STUDIO_PATH}/annotate`;
/**
 * Hash-link helper: href for one test run's results tab. Test Studio is a single
 * route with tabs selected by ?tab=, so a deep link needs both params.
 */
export const testRunResultsHref = (testRunId: string): string =>
  `#${TEST_STUDIO_PATH}?tab=results&testRunId=${encodeURIComponent(testRunId)}`;
/** Hash-link helper: href for the Test Executions tab. */
export const testExecutionsHref = (): string => `#${TEST_STUDIO_PATH}?tab=executions`;
export const CUSTOM_MODELS_PATH = `${DOCUMENTS_PATH}/custom-models`;

// --- Feature Platform ---
export const FEATURES_PATH_PREFIX = '/features';
/** Route pattern: /features/:featureId */
export const FEATURE_DETAIL_PATH = `${FEATURES_PATH_PREFIX}/:featureId`;
/** Hash-link helper: href to pass to nav items & internal links. */
export const featureDetailHref = (featureId: string): string => `#${FEATURES_PATH_PREFIX}/${featureId}`;

// --- Inbox (host notifications) ---
export const INBOX_PATH = '/inbox';
/** Hash-link helper: href for the inbox, optionally preselecting a tab. */
export const inboxHref = (tab?: 'messages' | 'preferences'): string => (tab ? `#${INBOX_PATH}?tab=${tab}` : `#${INBOX_PATH}`);
/**
 * Hash-link helper: href for one document's detail page. The whole object key is
 * encoded as one segment, matching the documents table and the Python
 * `idp_common.notifications.document_detail_link`.
 */
export const documentDetailHref = (objectKey: string): string => `#${DOCUMENTS_PATH}/${encodeURIComponent(objectKey)}`;
/**
 * Route path of a document's edit page: the direct edit, or its review-queue item while it
 * is in review. The whole object key is one encoded segment, byte-identical to the Python
 * `idp_common.notifications.document_edit_link`.
 */
export const documentEditPath = (objectKey: string): string => `${DOCUMENTS_PATH}/${encodeURIComponent(objectKey)}/edit`;
/**
 * Where the user opened the edit page from (`?from=`), which decides its breadcrumbs:
 * the document's page, Review Management, an Annotator's Queues or Tasks, Test Studio,
 * or the Inbox. A link without it (a bookmark, a copied field link) gets the role's
 * usual trail.
 */
export const EDIT_ORIGINS = ['document', 'review', 'queues', 'tasks', 'test-studio', 'inbox'] as const;
export type EditOrigin = (typeof EDIT_ORIGINS)[number];
/** The `from=` of an edit page's query string, or undefined when it names no origin. */
export const parseEditOrigin = (search: string): EditOrigin | undefined => {
  const from = new URLSearchParams(search).get('from');
  return (EDIT_ORIGINS as readonly string[]).includes(from ?? '') ? (from as EditOrigin) : undefined;
};
/**
 * An internal route with `from=` added when it is an edit page (a document's, or a
 * Test Studio item's), so the page's breadcrumbs lead back to where it was opened.
 * Any other route is returned unchanged.
 */
export const withEditOrigin = (route: string, from: EditOrigin): string => {
  const [path, query = ''] = route.split('?');
  const isEdit =
    (path.startsWith(`${DOCUMENTS_PATH}/`) && path.endsWith('/edit')) || /^\/review-queues\/test-sets\/[^/]+\/items\/[^/]+$/.test(path);
  if (!isEdit) return route;
  const params = new URLSearchParams(query);
  params.set('from', from);
  return `${path}?${params.toString()}`;
};
/** What the edit page opens on: a section (`?section=`), a field (`?field=`) or the source document (`?view=source`); and where from. */
export interface DocumentEditQuery {
  section?: string;
  field?: string;
  view?: 'source';
  from?: EditOrigin;
}
/** Hash-link helper: href for a document's edit page, with only the query keys given, in that order. */
export const documentEditHref = (objectKey: string, query: DocumentEditQuery = {}): string => {
  const params = (['section', 'field', 'view', 'from'] as const)
    .filter((k) => query[k] !== undefined)
    .map((k) => `${k}=${encodeURIComponent(query[k] as string)}`);
  return `#${documentEditPath(objectKey)}${params.length ? `?${params.join('&')}` : ''}`;
};

const MAX_INTERNAL_ROUTE_LENGTH = 1024;

const hasWhitespaceOrControl = (value: string): boolean =>
  Array.from(value).some((ch) => {
    const code = ch.codePointAt(0) ?? 0;
    return code <= 0x20 || (code >= 0x7f && code <= 0x9f) || /\s/u.test(ch);
  });

/**
 * True only for an in-app route: starts with `/` (not `//`), has no `\`, whitespace
 * or control characters, and contains no `://`. Mirrors the Python
 * `idp_common.notifications.validate_link`, so a link the backend accepted is
 * treated as internal here and anything else is never followed.
 */
export const isInternalRoute = (link?: string | null): link is string =>
  typeof link === 'string' &&
  link.length <= MAX_INTERNAL_ROUTE_LENGTH &&
  link.startsWith('/') &&
  !link.startsWith('//') &&
  !link.includes('\\') &&
  !link.includes('://') &&
  !hasWhitespaceOrControl(link);
/** Review Studio › Review Management: production review queues and Test Studio queues (Admin). */
export const REVIEW_QUEUES_PATH = '/review-queues';
/** Which table Review Management shows (`?view=`): the Production queues, or the Test Studio queues. */
export type ReviewManagementView = 'production' | 'test';
/** Hash-link helper: Review Management, on the Production (default) or Test Studio queues. */
export const reviewManagementHref = (view?: ReviewManagementView): string => `#${REVIEW_QUEUES_PATH}${view === 'test' ? '?view=test' : ''}`;
/** Home: every user's Inbox, and an Annotator's Tasks (assigned items) and Queues. */
export const MY_WORK_PATH = '/my-work';
export const MY_TASKS_PATH = `${MY_WORK_PATH}/tasks`;
/** The queues that list the caller as an Annotator (production and Test Studio). */
export const MY_QUEUES_PATH = `${MY_WORK_PATH}/queues`;
/** The former My Tasks location; redirects to MY_TASKS_PATH. */
export const LEGACY_MY_TASKS_PATH = `${REVIEW_QUEUES_PATH}/my-tasks`;
/** The former Production review-item route; it redirects to the document's edit page, keeping the query. */
export const LEGACY_REVIEW_ITEM_PATH = `${REVIEW_QUEUES_PATH}/items`;
/** The former Queue health page, now part of Review Management; redirects there. */
export const REVIEW_SUPERVISOR_PATH = `${REVIEW_QUEUES_PATH}/supervise`;
/** Route path of one system-wide production review queue's page, by queue id. */
export const productionQueuePath = (queue: string): string => `${REVIEW_QUEUES_PATH}/queues/${encodeURIComponent(queue)}`;
