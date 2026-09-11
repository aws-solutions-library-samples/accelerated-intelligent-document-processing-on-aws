# RBAC — Dynamic API Authorization Tests

Live tests against a deployed stack: temporary Cognito users (one per group + a config-version-scoped Author + a second user for IDOR) exercise every API op across all roles, unauthenticated, and with malformed/expired tokens, plus the AppSec mandatory-cases checklist (IDOR, token lifecycle, TLS, input validation, deleted-resource).

## Summary

- **Gate (hard failures):** PASS ✅
- **Checks:** 556 (555 passed, 0 hard fail, 1 known-gap warning)
- **Ran against:** stack `<REDACTED>` in region `us-west-2` (account `<ACCOUNT_ID>`)
- **Source git SHA:** `720f3052a`

## Test suites executed

Each suite maps to the AppSec "Minimum Mandatory Security Focused Test Cases for APIs" checklist item (see the [api-rbac-test skill](../../../.claude/skills/api-rbac-test.md)).

| Suite | Checklist | Checks | Pass | Hard fail | Known-gap |
|-------|:---------:|-------:|-----:|----------:|----------:|
| Token negatives (missing/garbage/tampered/empty) | 1 | 4 | 4 | 0 ✅ | 0 |
| Unauthenticated access denied | 1 | 106 | 106 | 0 ✅ | 0 |
| IDOR / BOLA (cross-user resource access) | 2.1 | 2 | 2 | 0 ✅ | 0 |
| Token lifecycle (expiry + logout revocation) | 2.3/2.4 | 2 | 1 | 0 ✅ | 1 |
| Deleted-resource inaccessibility | 2.5 | 1 | 1 | 0 ✅ | 0 |
| Authorization matrix (positive: role allowed) | 2/2.2 | 424 | 424 | 0 ✅ | 0 |
| Config-version scope enforcement | 2/2.2 | 6 | 6 | 0 ✅ | 0 |
| Input validation (malformed arguments) | 3 | 7 | 7 | 0 ✅ | 0 |
| TLS protocol (1.0/1.1/cleartext refused, 1.2+ accepted) | 4 | 4 | 4 | 0 ✅ | 0 |

## ⚠️ Known-gap findings (accepted risk)

| Op | Principal | Status | Gap | Detail |
|----|-----------|-------:|-----|--------|
| `listDocuments` | token:post-logout | 200 | GAP-SEC-LOGOUT | SEC-2.4-LOGOUT-REVOCATION: token before-logout=200, after-logout=200. STILL ACCEPTED (stateless JWT — see gap) |

## Authorization matrix — 106 operations × 5 roles

<details><summary>Full op × role matrix (530 checks) — HTTP status, ✅ pass / ❌ fail</summary>

| Operation | Admin | Author | Viewer | Reviewer | unauth |
|-----------|---|---|---|---|---|
| `abortTestRuns` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `abortWorkflow` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `addDocumentsToTestSet` | 500 ✅ | 500 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `addDocumentsToTestSetFromUpload` | 400 ✅ | 400 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `addTestSet` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `addTestSetFromUpload` | 400 ✅ | 400 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `autoDetectSections` | 500 ✅ | 500 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `calculateCapacity` | 200 ✅ | 200 ✅ | 200 ✅ | 403 ✅ | 401 ✅ |
| `checkFeatureEntitlement` | 200 ✅ | 200 ✅ | 200 ✅ | 200 ✅ | 401 ✅ |
| `claimReview` | 400 ✅ | 403 ✅ | 403 ✅ | 400 ✅ | 401 ✅ |
| `clearDraftLabels` | 500 ✅ | 500 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `compareDocumentVersions` | 400 ✅ | 400 ✅ | 400 ✅ | 400 ✅ | 401 ✅ |
| `compareTestRuns` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `completeSectionReview` | 400 ✅ | 403 ✅ | 403 ✅ | 400 ✅ | 401 ✅ |
| `copyToBaseline` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `createFinetuningJob` | 400 ✅ | 400 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `createUser` | 400 ✅ | 403 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `deleteAgentJob` | 200 ✅ | 200 ✅ | 200 ✅ | 200 ✅ | 401 ✅ |
| `deleteChatSession` | 200 ✅ | 200 ✅ | 200 ✅ | 200 ✅ | 401 ✅ |
| `deleteConfigVersion` | 200 ✅ | 403 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `deleteDiscoveryJob` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `deleteDocument` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `deleteDocumentVersion` | 400 ✅ | 403 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `deleteFinetuningJob` | 400 ✅ | 400 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `deleteTestSets` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `deleteTests` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `deleteUser` | 400 ✅ | 403 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `estimateReviewEffort` | 500 ✅ | 500 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `generateDraftLabels` | 400 ✅ | 400 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `generateRuleJson` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `getAgentJobStatus` | 200 ✅ | 200 ✅ | 200 ✅ | 403 ✅ | 401 ✅ |
| `getAnnotationQueue` | 500 ✅ | 500 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `getChatMessages` | 500 ✅ | 500 ✅ | 500 ✅ | 500 ✅ | 401 ✅ |
| `getCircuitBreakerStatus` | 200 ✅ | 200 ✅ | 200 ✅ | 200 ✅ | 401 ✅ |
| `getConfigVersion` | 200 ✅ | 200 ✅ | 200 ✅ | 403 ✅ | 401 ✅ |
| `getConfigVersions` | 200 ✅ | 200 ✅ | 200 ✅ | 403 ✅ | 401 ✅ |
| `getConfigurationLibraryFile` | 200 ✅ | 200 ✅ | 200 ✅ | 403 ✅ | 401 ✅ |
| `getDocument` | 200 ✅ | 200 ✅ | 200 ✅ | 200 ✅ | 401 ✅ |
| `getDocumentCount` | 200 ✅ | 200 ✅ | 200 ✅ | 200 ✅ | 401 ✅ |
| `getDocumentVersion` | 200 ✅ | 200 ✅ | 200 ✅ | 200 ✅ | 401 ✅ |
| `getDraftLabelJob` | 500 ✅ | 500 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `getFeatureLaunchUrl` | 500 ✅ | 403 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `getFileContents` | 500 ✅ | 500 ✅ | 500 ✅ | 500 ✅ | 401 ✅ |
| `getFilePresignedUrl` | 500 ✅ | 500 ✅ | 500 ✅ | 500 ✅ | 401 ✅ |
| `getFinetuningJob` | 200 ✅ | 200 ✅ | 200 ✅ | 200 ✅ | 401 ✅ |
| `getLatestPublishedVersion` | 200 ✅ | 200 ✅ | 200 ✅ | 200 ✅ | 401 ✅ |
| `getModelConfigLimits` | 200 ✅ | 200 ✅ | 200 ✅ | 403 ✅ | 401 ✅ |
| `getMyProfile` | 200 ✅ | 200 ✅ | 200 ✅ | 200 ✅ | 401 ✅ |
| `getPricing` | 200 ✅ | 200 ✅ | 200 ✅ | 403 ✅ | 401 ✅ |
| `getSampleDocumentUrl` | 400 ✅ | 400 ✅ | 400 ✅ | 403 ✅ | 401 ✅ |
| `getStepFunctionExecution` | 200 ✅ | 200 ✅ | 200 ✅ | 200 ✅ | 401 ✅ |
| `getTestRun` | 400 ✅ | 400 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `getTestRunStatus` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `getTestRuns` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `getTestSetDocuments` | 500 ✅ | 500 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `getTestSetVersions` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `getTestSets` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `listAgentJobs` | 200 ✅ | 200 ✅ | 200 ✅ | 403 ✅ | 401 ✅ |
| `listAvailableAgents` | 200 ✅ | 200 ✅ | 200 ✅ | 403 ✅ | 401 ✅ |
| `listBucketFiles` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `listCatalogFeatures` | 200 ✅ | 200 ✅ | 200 ✅ | 200 ✅ | 401 ✅ |
| `listChatSessions` | 200 ✅ | 200 ✅ | 200 ✅ | 200 ✅ | 401 ✅ |
| `listConfigurationLibrary` | 200 ✅ | 200 ✅ | 200 ✅ | 403 ✅ | 401 ✅ |
| `listDiscoveryJobs` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `listDocumentVersions` | 200 ✅ | 200 ✅ | 200 ✅ | 200 ✅ | 401 ✅ |
| `listDocuments` | 200 ✅ | 200 ✅ | 200 ✅ | 200 ✅ | 401 ✅ |
| `listDocumentsByDateRange` | 200 ✅ | 200 ✅ | 200 ✅ | 200 ✅ | 401 ✅ |
| `listDocumentsDateHour` | 200 ✅ | 200 ✅ | 200 ✅ | 200 ✅ | 401 ✅ |
| `listDocumentsDateShard` | 200 ✅ | 200 ✅ | 200 ✅ | 200 ✅ | 401 ✅ |
| `listFinetuningJobs` | 200 ✅ | 200 ✅ | 200 ✅ | 200 ✅ | 401 ✅ |
| `listInstalledFeatures` | 200 ✅ | 200 ✅ | 200 ✅ | 200 ✅ | 401 ✅ |
| `listSampleDocuments` | 200 ✅ | 200 ✅ | 200 ✅ | 403 ✅ | 401 ✅ |
| `listUsers` | 200 ✅ | 403 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `processChanges` | 200 ✅ | 403 ✅ | 403 ✅ | 200 ✅ | 401 ✅ |
| `publishTestSetVersion` | 400 ✅ | 400 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `queryKnowledgeBase` | 200 ✅ | 200 ✅ | 200 ✅ | 200 ✅ | 401 ✅ |
| `reextractTestSetDocument` | 500 ✅ | 500 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `releaseReview` | 400 ✅ | 403 ✅ | 403 ✅ | 400 ✅ | 401 ✅ |
| `removeDocumentsFromTestSet` | 500 ✅ | 500 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `reprocessDocument` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `resetTestSetLabels` | 500 ✅ | 403 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `restoreDefaultModelConfigLimits` | 200 ✅ | 403 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `restoreDefaultPricing` | 200 ✅ | 403 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `sendAgentChatMessage` | SKIP ✅ | SKIP ✅ | SKIP ✅ | 403 ✅ | 401 ✅ |
| `sendChatDocumentMessage` | 500 ✅ | 500 ✅ | 500 ✅ | 500 ✅ | 401 ✅ |
| `sendTestRunToReview` | 400 ✅ | 400 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `setActiveVersion` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `skipAllSectionsReview` | 400 ✅ | 403 ✅ | 403 ✅ | 400 ✅ | 401 ✅ |
| `startMultiDocDiscovery` | 400 ✅ | 400 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `startTestRun` | 400 ✅ | 400 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `submitAgentQuery` | 200 ✅ | 200 ✅ | 200 ✅ | 403 ✅ | 401 ✅ |
| `subscribeFeature` | 500 ✅ | 403 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `syncBdaIdp` | 200 ✅ | 200 ✅ | 200 ✅ | 200 ✅ | 401 ✅ |
| `unsubscribeFeature` | 500 ✅ | 403 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `updateAgentJobStatus` | 403 ✅ | 403 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `updateConfiguration` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `updateDiscoveryJobStatus` | 403 ✅ | 403 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `updateModelConfigLimits` | 200 ✅ | 403 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `updatePricing` | 200 ✅ | 403 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `updateTestSet` | 400 ✅ | 400 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `updateUser` | 400 ✅ | 403 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `uploadDiscoveryDocument` | 400 ✅ | 400 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `uploadDocument` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `uploadMultiDocDiscoveryZip` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `uploadSampleDocument` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |
| `validateTestFileName` | 200 ✅ | 200 ✅ | 403 ✅ | 403 ✅ | 401 ✅ |

</details>

> The per-check **request IDs** stay in the gitignored raw report (`report.md`); they are environment-specific and not published. Everything above (gate, suites, failures, and the status matrix) is the auditable record.
