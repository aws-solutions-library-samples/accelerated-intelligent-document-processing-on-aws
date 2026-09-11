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
export const AGENT_CHAT_PATH = '/agentchat';
export const WELCOME_PATH = '/welcome';
/** localStorage key: set when the user dismisses the welcome landing page. */
export const WELCOME_DISMISSED_KEY = 'idp-welcome-dismissed';
export const CAPACITY_PLANNING_PATH = `${DOCUMENTS_PATH}/capacity-planning`;
/** Route pattern base: /test-studio/sets/:testSetId */
export const TEST_SET_DETAIL_PATH = `${TEST_STUDIO_PATH}/sets`;
/** Hash-link helper: href for a test set's document browser. */
export const testSetDetailHref = (testSetId: string): string => `#${TEST_SET_DETAIL_PATH}/${encodeURIComponent(testSetId)}`;
/**
 * Hash-link helper: href for one test set document's detail page
 * (/test-studio/sets/:testSetId/doc/<objectKey>, objectKey may contain slashes).
 * Optional view preselects the source-document or ground-truth view.
 */
export const testSetDocumentHref = (testSetId: string, objectKey: string, view?: 'source' | 'ground-truth'): string =>
  `#${TEST_SET_DETAIL_PATH}/${encodeURIComponent(testSetId)}/doc/${objectKey
    .split('/')
    .map(encodeURIComponent)
    .join('/')}${view ? `?view=${view}` : ''}`;
/**
 * Hash-link helper: href for a test set's annotation queue
 * (/test-studio/sets/:testSetId/annotate). This link only navigates; access is
 * enforced server-side against the user's allowedTestSets, so it grants nothing on
 * its own and is safe to share with a scoped annotator.
 */
export const testSetAnnotateHref = (testSetId: string, objectKey?: string): string =>
  `#${TEST_SET_DETAIL_PATH}/${encodeURIComponent(testSetId)}/annotate${objectKey ? `?doc=${encodeURIComponent(objectKey)}` : ''}`;
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
