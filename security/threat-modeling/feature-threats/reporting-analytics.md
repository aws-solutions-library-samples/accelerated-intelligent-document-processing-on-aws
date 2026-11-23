# Reporting & Analytics — Threat Analysis

## Document Information

| Field | Value |
|-------|-------|
| **Document Version** | 3.1 |
| **Last Updated** | 2026-11-17 |
| **Applies to release** | v0.6.3 |
| **Feature** | Reporting Database, Analytics, Evaluation |
| **Classification** | Internal |

## 1. Feature Overview

The reporting and analytics subsystem provides:
- **Reporting Database**: Document processing results written as Parquet files to S3, cataloged by AWS Glue, and queryable via Amazon Athena
- **Evaluation Framework**: Automated accuracy comparison against ground truth documents, with enhanced reporting and metrics
- **Cost Calculator**: Token-based cost estimation for processing operations
- **Dashboard Metrics**: Processing volume, success rates, costs, and performance metrics displayed in the Web UI
- **Test Studio**: Interactive document testing with real-time feedback
- **Discovery**: AI-assisted configuration generation from sample document analysis

## 2. Architecture

```mermaid
flowchart TD
    subgraph Data Collection
        ReportLambda[Reporting Lambda] -->|Parquet| S3Report[S3 Reporting Bucket]
        EvalLambda[Evaluation Lambda] -->|Results| S3Report
        MeterLambda[Metering Lambda] -->|Usage Data| S3Report
    end

    subgraph Data Catalog
        Glue[AWS Glue Crawler] -->|Catalog| S3Report
        Glue --> GlueCatalog[Glue Data Catalog]
    end

    subgraph Query Layer
        Athena[Amazon Athena] --> GlueCatalog
        Athena --> S3Report
        Agent[Analytics Agent] --> Athena
    end

    subgraph UI
        Dashboard[Dashboard] -->|REST /op| Lambda[Status/Metrics Lambda]
        Lambda --> DDB[DynamoDB]
        TestStudio[Test Studio] -->|REST /op| TestLambda[Test Lambda]
        Discovery[Discovery] -->|REST /op| DiscLambda[Discovery Lambda]
        DiscLambda --> Bedrock[Amazon Bedrock]
    end
```

## 3. Threat Analysis

### RPT.T01: Reporting Data Tampering

| Attribute | Value |
|-----------|-------|
| **Threat ID** | RPT.T01 |
| **Category** | STRIDE: Tampering, Repudiation |
| **Description** | Parquet files in the reporting S3 bucket could be modified or deleted, altering historical processing records and analytics. The bucket holds raw `metering` and `document_sections_*` data, and the four rollup tables derived from it (`metering_hourly`, `metering_daily`, `metering_docs_hourly`, `metering_docs_daily`) that the dashboard's cost widgets and the analytics agent read |
| **Attack Vector** | Direct S3 access with compromised credentials, or a Lambda bug that deletes or overwrites existing reporting data |
| **Impact** | Corrupted analytics and cost reporting, unreliable evaluation metrics, loss of audit trail |
| **Likelihood** | Low |
| **Severity** | Medium |
| **Affected Components** | S3 Reporting Bucket, Parquet files, `DataMartRollupFunction` (`src/lambda/data_mart_rollup/index.py`), `DataMartMigrationStateMachine`, `MeteringHourMigrationFunction` |
| **Mitigations** | The stack-created `ReportingBucket` has versioning enabled, so an overwritten or deleted object's prior version is recoverable; it is encrypted with the stack's customer-managed key, blocks public access and writes server access logs to the logging bucket. Scheduled rollups are append-only: each partition is written once, and the handler probes for an existing partition and skips the `INSERT` rather than rewriting it. Delete permissions are prefix-scoped: `DataMartRollupFunction` holds `s3:DeleteObject` only on the four rollup prefixes, used only by the migration state machine's `InitialPurge` task, so it cannot delete raw `metering/` or `document_sections_*/` data; `MeteringHourMigrationFunction` holds it only on `metering/*`, for its copy-then-delete partition move. The rollups are derived data and can be rebuilt from raw `metering` |
| **Residual risk** | No S3 Object Lock is configured, and a bucket supplied through `ReportingBucketName` takes whatever versioning its owner set. A rollup migration (a stack update that changes `MigrationVersion`, such as the one that adds `n_runs` to the doc-grain rollups) purges and rebuilds **all four** rollup tables over the replay window, so the cost rollups, the dashboard cost widgets and the analytics agent read low until it finishes; raw `metering` is unaffected, and partitions older than the window keep `n_runs` empty |
| **Evidence** | `lib/idp_common_pkg/tests/unit/lambdas/test_data_mart_rollup.py::TestPurgeRollupPrefixes::test_purge_covers_all_four_prefixes_within_window`; `lib/idp_common_pkg/tests/unit/lambdas/test_data_mart_rollup.py::TestPurgeRollupPrefixes::test_purge_task_does_not_touch_control_plane_prefixes`; `lib/idp_common_pkg/tests/unit/lambdas/test_data_mart_rollup.py::TestPurgeRollupPrefixes::test_purge_bounded_window_preserves_out_of_window_partitions`; UNVERIFIED: bucket versioning and the prefix scope of each `s3:DeleteObject` grant are read from `template.yaml`, not asserted by a test |

### RPT.T02: Athena Query Data Exposure

| Attribute | Value |
|-----------|-------|
| **Threat ID** | RPT.T02 |
| **Category** | STRIDE: Information Disclosure |
| **Description** | Athena queries over reporting data expose all processed document information. Query results stored in Athena output location could be accessed by unauthorized users |
| **Attack Vector** | Access Athena query results S3 bucket, or submit queries that extract sensitive fields from processed documents |
| **Impact** | Exposure of all processed document data including extracted PII and business-sensitive information |
| **Likelihood** | Medium |
| **Severity** | High |
| **Affected Components** | Amazon Athena, S3 query results, Glue Data Catalog |
| **Mitigations** | Athena workgroup with restricted query result location, S3 lifecycle policies on query results (auto-delete), IAM restrictions on Athena access (Admin/Author only), Athena query logging |
| **Evidence** | `scripts/tests/test_athena_result_lifecycle.py`; UNVERIFIED: the workgroup result location, Athena IAM scoping and query logging are not covered by a test |

### RPT.T03: Glue Catalog Manipulation

| Attribute | Value |
|-----------|-------|
| **Threat ID** | RPT.T03 |
| **Category** | STRIDE: Tampering |
| **Description** | The Glue Data Catalog defines table schemas and S3 data locations. If modified, Athena queries could read wrong data, miss data, or expose additional S3 paths |
| **Attack Vector** | Modify Glue table definitions to point to different S3 locations, or alter column definitions to expose additional data |
| **Impact** | Athena queries return wrong data, or expose data from other S3 paths |
| **Likelihood** | Low |
| **Severity** | Medium |
| **Affected Components** | AWS Glue Data Catalog, Glue Crawler |
| **Mitigations** | IAM restrictions on Glue API operations, Glue Catalog resource policies, CloudTrail logging of Glue operations, periodic catalog validation |
| **Evidence** | UNVERIFIED: no listed mitigation cites a test, gate or make target |

### RPT.T04: Evaluation Data Manipulation

| Attribute | Value |
|-----------|-------|
| **Threat ID** | RPT.T04 |
| **Category** | STRIDE: Tampering |
| **Description** | Ground truth data and evaluation results could be tampered with to hide accuracy degradation or mask the impact of attacks on processing quality |
| **Attack Vector** | Modify ground truth files in S3 to match incorrect extraction results, or tamper with evaluation metrics |
| **Impact** | False confidence in processing accuracy, masked detection of prompt injection or processing attacks |
| **Likelihood** | Low |
| **Severity** | High |
| **Affected Components** | S3 (ground truth and evaluation data), Evaluation Lambda |
| **Mitigations** | RBAC (Admin-only ground truth management), S3 versioning on ground truth data, evaluation result integrity checks, separate evaluation metrics storage |
| **Evidence** | UNVERIFIED: no listed mitigation cites a test, gate or make target |

### RPT.T05: Discovery Feature — Prompt Injection via Sample Documents

| Attribute | Value |
|-----------|-------|
| **Threat ID** | RPT.T05 |
| **Category** | STRIDE: Tampering, Elevation of Privilege |
| **Description** | The Discovery feature analyzes sample documents to auto-generate processing configurations. Adversarial sample documents could manipulate the AI into generating malicious configurations |
| **Attack Vector** | Upload crafted sample documents that contain prompt injection causing Discovery to generate configurations with malicious prompts, extraction schemas, or class definitions |
| **Impact** | Auto-generated configurations that systematically misprocess or exfiltrate data when used for production processing |
| **Likelihood** | Medium |
| **Severity** | High |
| **Affected Components** | Discovery Lambda, Amazon Bedrock, Configuration S3 |
| **Mitigations** | Discovery generates draft configs that require human review before activation, configuration schema validation, RBAC (Admin/Author required), evaluation testing before production use |
| **Evidence** | UNVERIFIED: no listed mitigation cites a test, gate or make target |

### RPT.T06: Test Studio — Uncontrolled Processing Costs

| Attribute | Value |
|-----------|-------|
| **Threat ID** | RPT.T06 |
| **Category** | STRIDE: Denial of Service |
| **Description** | Test Studio allows interactive document testing that invokes the full processing pipeline. Excessive testing could consume Bedrock tokens, Textract calls, and Lambda execution time |
| **Attack Vector** | Repeatedly submit documents through Test Studio to generate excessive processing costs |
| **Impact** | Cost escalation, processing resource exhaustion affecting production workloads |
| **Likelihood** | Medium |
| **Severity** | Medium |
| **Affected Components** | Test Studio, processing pipeline, Bedrock, Textract |
| **Mitigations** | RBAC (Admin/Author required for Test Studio), rate limiting on test submissions, separate test processing tracking, CloudWatch alarms on processing costs |
| **Evidence** | UNVERIFIED: no listed mitigation cites a test, gate or make target |

### RPT.T07: Ground-Truth Tampering via the Test Set Visual Editor

| Attribute | Value |
|-----------|-------|
| **Threat ID** | RPT.T07 |
| **Category** | STRIDE: Tampering, Repudiation |
| **Description** | Test Studio's **Edit Ground Truth** visual editor (v0.6.2) writes directly back to a test set's `baseline/.../result.json`. Ground truth is the measuring instrument for accuracy: an attacker who edits baselines to match current (possibly degraded or malicious) model output makes evaluation report high accuracy regardless of real quality — silently disabling the control that RPT.T04 and PM.T03/PM.T07 rely on to detect misprocessing and example poisoning. Because the harm is to *future* measurements rather than to visible data, it is unlikely to be noticed. |
| **Attack Vector** | An Author (or a compromised Author/Admin session — including via a feature UI bundle, FEAT.T01) edits baseline `inference_result` values so a poisoned configuration or biased few-shot set still scores as accurate. |
| **Impact** | Loss of trust in all accuracy metrics; conceals systematic misprocessing, prompt injection effects, and few-shot poisoning; corrupted evaluation history. |
| **Likelihood** | Low |
| **Severity** | High |
| **Affected Components** | `test_set_resolver` (`getTestSetDocuments`), test-set baseline objects in TestSetBucket, evaluation framework |
| **Mitigations** | Edits require the **Admin/Author** group, enforced server-side (`getTestSetDocuments` is Admin/Author; other roles are read-only in the editor). Every save writes an **`_editHistory` provenance entry** to the baseline, so a modification is attributable and reviewable rather than anonymous — the key anti-repudiation control here. TestSetBucket CORS permits only GET/HEAD + ranged reads for the browser's presigned fetches, so the browser cannot write objects directly; writes go through the resolver. S3 versioning on the bucket retains prior baseline bytes for recovery. |
| **Residual risk** | `_editHistory` records that a change happened but nothing *alerts* on baseline mutation, and an Author is legitimately allowed to edit ground truth (that is the feature's purpose), so policy cannot distinguish correction from sabotage. Recommend (1) alerting/reporting on baseline edits for high-value test sets, and (2) treating unexplained accuracy improvements as a signal to diff `_editHistory`. |
| **Evidence** | UNVERIFIED: no listed mitigation cites a test, gate or make target |

### RPT.T08: Extended Data Retention via Document Version History

| Attribute | Value |
|-----------|-------|
| **Threat ID** | RPT.T08 |
| **Category** | STRIDE: Information Disclosure |
| **Description** | Document Versions (v0.6.0) retains **every** successful processing run as an immutable snapshot of pinned S3 object versions, where previously re-processing the same S3 key overwrote prior results. Content an operator believes was superseded — or removed by re-uploading a redacted replacement — remains readable through version history until `DataRetentionInDays` expires. This changes the deployment's data-retention profile in a way that matters for privacy/erasure obligations. |
| **Attack Vector** | A user with access to version history reads a prior run's results for content that was intentionally replaced (e.g. re-uploading a redacted document over an un-redacted one, expecting the original to be gone). Object bytes are fetched with an explicit `versionId` through `getFilePresignedUrl`. |
| **Impact** | Disclosure of superseded document content and extracted data, including data an operator intended to remove; retention beyond what a naive "delete and re-upload" workflow implies. |
| **Likelihood** | Low |
| **Severity** | Medium |
| **Affected Components** | `document_versions_resolver`, versioned S3 output objects, run manifests, `getFilePresignedUrl` |
| **Mitigations** | Snapshots are bounded by **`DataRetentionInDays`**, so retention is finite and operator-configurable rather than indefinite. `deleteDocumentVersion` is **Admin-only** (server-side enforced), giving an explicit purge path for a specific run. The feature is documented, and the UI surfaces a Version History panel so retained versions are **visible** rather than hidden state. |
| **Residual risk** | `listDocumentVersions` requires an **assigned group** (`ANY_GROUP`), so the enumerable index of object keys *and* version ids is not reachable by a caller in no group — but every group reaches all of it, which is the axis that still matters here. Reading the bytes now requires an API operation: `CognitoAuthorizedRole` grants **nothing** on the Output bucket, and **no** `s3:GetObjectVersion` on any bucket, so a `?versionId=` fetch goes through `getFilePresignedUrl` or `getFilePresignedUrls`, where the group floor, the bucket allow-list and `document_scope` apply. `document_scope` matches the owning document's `ConfigVersion` against the caller's `allowedConfigVersions`, which refuses a **scoped** caller another profile's version history — a narrowing this entry did not previously have. It refuses an **unscoped** caller nothing, scoping being opt-in per user, so for the default user row version history still widens the practical impact of UI.T06 and the group floor does not narrow that. Recommend scoping `listDocumentVersions` per document rather than per group, which needs the same document-ownership attribute UI.T06's residual asks for — the group floor is the wrong axis for "may this caller see this document's history", and no setting of it supplies one. For redaction workflows, prefer the PII feature's *redact copy and stop* mode (which deletes the original) over manual replace-and-reupload. Deleting a document, whether from the UI, `idp-cli`, the SDK or *redact copy and stop*, purges every version and delete marker of the exact input key as well as the output version history, so a deleted or superseded upload is not left in the versioned Input bucket as a noncurrent version for `DataRetentionInDays`. Re-uploading a file over the same key without deleting the document is different: the earlier upload stays as a noncurrent Input-bucket version until the lifecycle rule expires it. |
| **Evidence** | UNVERIFIED: no listed mitigation cites a test, gate or make target |

## 4. Security Controls Summary

| Control | Implementation | Threats Mitigated |
|---------|---------------|-------------------|
| **S3 versioning** | Versioning on reporting and ground truth buckets | RPT.T01, RPT.T04, RPT.T07 |
| **Append-only rollups, prefix-scoped deletes** | Scheduled rollups write each partition once; `s3:DeleteObject` limited to the rollup prefixes (migration purge) and `metering/*` (partition move) | RPT.T01 |
| **Athena workgroup** | Restricted query result location, IAM scoping | RPT.T02 |
| **IAM restrictions** | Glue API access restricted to platform roles | RPT.T03 |
| **RBAC** | Role-based access to evaluation, discovery, test studio, ground-truth editing | RPT.T04, RPT.T05, RPT.T06, RPT.T07 |
| **Edit provenance** | `_editHistory` entry written on every ground-truth save | RPT.T07 |
| **Read-only CORS on TestSetBucket** | GET/HEAD + ranged reads only; writes must go through the resolver | RPT.T07 |
| **Admin-only version delete** | `deleteDocumentVersion` restricted to Admin | RPT.T08 |
| **Bounded retention** | Document versions retained for `DataRetentionInDays` | RPT.T08 |
| **Config validation** | Schema validation of discovery-generated configs | RPT.T05 |
| **Rate limiting** | Test Studio submission limits | RPT.T06 |
| **Audit logging** | CloudTrail for S3, Glue, Athena operations | All |

## 5. Open Items

| Item | Threat | Status |
|------|--------|--------|
| No alerting on ground-truth baseline mutation | RPT.T07 | **Open** — `_editHistory` is recorded but not surfaced/alerted |
| `listDocumentVersions` has no per-document scope | RPT.T08 | **Open** — an assigned group is required, so a caller in no group is refused; every group still enumerates every document's keys and version ids. Fetching the bytes those ids name is no longer a no-API path (UI.T06's browser grant is gone) and is refused to a caller whose profile scope excludes the document, but the listing itself applies no document scope at all |
| Version history retains superseded content until retention expiry | RPT.T08 | **Accepted, documented** — use *redact copy and stop* for erasure workflows |
