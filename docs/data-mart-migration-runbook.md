# Data-mart rollup migration — operations runbook

This runbook covers the `DataMartMigrationStateMachine` that repopulates the four per-document rollup tables (`metering_hourly`, `metering_daily`, `metering_docs_hourly`, `metering_docs_daily`) at the widened `document_class` grain. See [reporting-sql-layer.md](reporting-sql-layer.md) for the reporting-lake design context.

## What runs when

**A regular stack update with an unchanged `MigrationVersion`** does nothing on the migration side — CFN sees no property change on `DataMartRollupMigrationCustomResource` and doesn't fire the dispatcher. The state machine is dormant. The scheduled hourly rollups and reconciler keep writing new partitions at the widened grain (raw `metering` has carried `document_class` since 0.6.10, so those writes come with the column populated).

**A stack update with a bumped `MigrationVersion`** fires the dispatcher, which starts a state-machine execution. On the state-machine side:

1. **CheckMarker** — Lambda reads the SSM marker `/idp/<stack>/data-mart-rollup/migration-complete` and routes:
   - `state=completed;days=<current>` → **short-circuit**, migration is a no-op.
   - `state=in_progress;days=<current>` → **skip purge**, resume backfill from where the prior attempt stopped (HeadObject-skip handles already-written chunks).
   - Absent / unrecognised / different `days` → **full flow** (purge + backfill from scratch).
2. **InitialPurge** — deletes every S3 object under the four rollup prefixes.
3. **WriteInProgressMarker** — writes `state=in_progress` BEFORE any chunk runs. Load-bearing: a state-machine restart mid-execution reads this and skips the purge, preserving prior chunk writes.
4. **PlanChunks** — computes `days ÷ chunk_hours` chunk ranges (30 ÷ 1 = 720 chunks by default).
5. **MigrateChunks (Map, MaxConcurrency=8)** — each chunk invokes the rollup Lambda in `mode: backfill` over its slice of hours with `arms=["metering_hourly", "metering_docs_hourly"]` (the two schema-widened tables; `control_plane_hourly` and `data_plane_lambda_hourly` are populated by the routine `:05` cron and skipped here). 8 chunks fan out in parallel against the rollup Lambda's `ReservedConcurrentExecutions: 12` (8 migration chunks + 3 for scheduled crons + 1 headroom). Total concurrent Athena queries = 8 chunks × 2 arms = 16, deliberately under Athena's default 20-DML-per-workgroup ceiling. SFN retries each chunk up to 3 times on `Lambda.ServiceException` / `Lambda.AWSLambdaException` / `States.Timeout` with exponential backoff. On idle load a 720-hour migration finishes in ~20-30 min.
6. **BackfillDailyRange** — after MigrateChunks completes, iterates each day in `[anchor - days, anchor)` and calls `_run_daily` to write `metering_daily` + `metering_docs_daily` for each day, reading from the now-populated hourly rollups. Single sequential Lambda invocation, ~30 s per day, so ~10 min for a 30-day window.
7. **CheckMigrationSuccess** — aggregates per-chunk results **plus** the daily-backfill result. `all_hours_clean=true` iff every chunk reported `hours_failed=0` **and** `hours_partial=0` (partial hours block completion because the migration IS the reconciler for these tables — treating a partial as clean would leave rows silently missing).
8. **WriteCompletedMarker** — writes `state=completed`. Migration done.

**`ForceFresh: "true"`** on the CustomResource makes the dispatcher delete the SSM marker before starting the state machine, so CheckMarker takes the full-flow branch regardless of any prior marker state. Use ONLY when deliberately wiping rollup data.

## Signals to watch

| Signal | Location | Means |
|---|---|---|
| CFN `UPDATE_COMPLETE` on the stack | CloudFormation console | Stack is updated. Does **NOT** mean migration is done — the dispatcher returns success after starting SFN, and the migration runs in the background for tens of minutes to hours. |
| SSM marker value | Parameter Store, `/idp/<stack>/data-mart-rollup/migration-complete` | The migration's live state signal. See [Marker states](#marker-states) below. |
| SFN execution status | Step Functions console, state machine `<stack>-data-mart-migration` | Per-execution status: RUNNING, SUCCEEDED, FAILED, TIMED_OUT, ABORTED. |
| Alarm `<stack>-data-mart-migration-failure` | CloudWatch Alarms | Fires on any FAILED/TIMED_OUT/ABORTED SFN execution. Points to this runbook. |
| Alarm `<stack>-data-mart-rollup-dlq-depth` | CloudWatch Alarms | Fires on Lambda DLQ arrivals. Unrelated to the state machine — covers scheduled hourly / reconciler / daily rollup failures. |
| Alarm `<stack>-data-mart-rollup-absence` | CloudWatch Alarms | Fires if the rollup Lambda records zero invocations for 2 hours. Unrelated to migration state. |

### Marker states

| SSM marker value | Meaning |
|---|---|
| ParameterNotFound | No migration has ever run, or `ForceFresh=true` just cleared it. Next stack update will run the full flow. |
| `days=N;state=in_progress;started_at=<ts>` | Migration purged the rollup S3 data and is running (or was interrupted). A restart of the state machine will SKIP the purge and resume backfill — non-destructive. |
| `days=N;state=completed;completed_at=<ts>` | Migration finished cleanly. Rollup tables are populated at the widened grain. |

## Runbook actions

### The migration succeeded

Verify: SSM marker has `state=completed`, and Athena query returns rows across the expected `document_class` values.

```sql
SELECT COALESCE(document_class, '<NULL>') AS doc_class, COUNT(*) AS row_count
FROM metering_hourly
GROUP BY document_class
ORDER BY row_count DESC
LIMIT 10;
```

No action needed. The idp-monitor `cost_by_document_type` widget will now render from the rollup instead of the 77-table UNION+JOIN — expect sub-second cold latency on wide ranges vs. the pre-widening ~30 s.

### Alarm: `<stack>-data-mart-migration-failure` fired

The SFN execution ended in FAILED (typically the state-machine's `MigrationHadFailures` Fail state). At least one chunk exhausted its 3 retries — usually a persistent Athena error on a specific hour range.

**To diagnose:**
1. Open the state machine's execution history: Step Functions console → `<stack>-data-mart-migration` → most recent FAILED execution → **Execution Input and Output** shows `failing_chunks` from `CheckMigrationSuccess`.
2. Each failing chunk's `failures` array names the exact hour(s) that failed and the underlying error (from `_run_backfill`'s per-hour try/except).

**To recover:**
- SSM marker is left at `state=in_progress` — this is intentional. A restart resumes without re-purging.
- Restart in one of two ways:
  - Bump `MigrationVersion` on the CustomResource (any string change) and re-deploy the stack. CFN fires the dispatcher, which starts a new SFN execution. CheckMarker sees `state=in_progress` and routes to the skip-purge branch. Only failing chunks re-execute; already-written chunks are HeadObject-skipped.
  - Or manually start a new SFN execution: Step Functions console → `<stack>-data-mart-migration` → **Start execution** → input `{"days": 30, "chunk_hours": 1, "version": "v1"}` (match the CustomResource properties currently in the deployed template). All three fields are **required** — the ASL reads `$.version` in `CheckMarker`, `WriteInProgressMarker`, and `WriteCompletedMarker` and raises `States.Runtime` if the field is absent, and omitting it or `chunk_hours` was a common cause of a manual restart failing before doing any work.
- If the underlying failure is Athena-side (e.g. workgroup issue, missing table), fix the root cause first — the restart won't help until the query can succeed.

### `state=in_progress` stuck without alarm firing

If the marker shows `state=in_progress` for far longer than a full migration should take (>4 hours on a 30-day retention, more if `chunk_hours` is smaller or the stack is very high-volume), the state machine may be running normally OR may have hung / been aborted externally.

**To diagnose:**
- Check whether an execution is currently RUNNING: `aws stepfunctions list-executions --state-machine-arn <arn> --status-filter RUNNING`.
- If none, then the machine terminated without writing the completed marker. Check the last execution's status:
  - `SUCCEEDED` but marker not `completed` — inspect the last state's output; `check_hours_failed` probably reported `all_hours_clean=false` but no chunks logged errors (unusual — file a bug).
  - `FAILED` / `ABORTED` — should have triggered the alarm; if it didn't, alarm may be suppressed / misconfigured.
- If an execution IS running, decide whether to wait or abort. Aborting mid-execution leaves the marker `in_progress`; a restart resumes without re-purging.

**To recover:**
- Same as the alarm case above — restart via MigrationVersion bump or manual StartExecution. The marker's `in_progress` state guarantees non-destructive resumption.

### Force a clean re-migration (destructive)

Only when the migration definitely needs to run again from scratch — e.g., after a schema change that requires re-aggregation, or to recover from a bug that wrote incorrect rollup data.

1. Set `ForceFresh: "true"` on the CustomResource in the parent template.
2. Bump `MigrationVersion` to a new string.
3. Deploy the stack.

The dispatcher deletes the SSM marker before starting SFN. CheckMarker sees ParameterNotFound and takes the full-flow branch: purge → in_progress marker → chunks → completed marker.

**Flip `ForceFresh` back to `"false"` after the run completes** so a subsequent routine stack update doesn't accidentally re-purge.

### Verify a customer's first-deploy experience

On a fresh customer stack that has never run this migration:

1. Marker is absent (ParameterNotFound). ✓ Full flow will run.
2. Rollup S3 prefixes are empty. ✓ Purge is a no-op.
3. Raw `metering` is empty (no documents processed yet) OR has recent rows with `document_class` populated (write-side is live from 0.6.10). ✓ Chunks process empty hours quickly.
4. State machine reaches WriteCompletedMarker fast (<1 min on empty install). ✓ Marker = `state=completed`.
5. Reconciler at :35 hourly fills any gap when new hours accumulate.

If steps 1-5 fail on install, the failure signal will be either the failure alarm or a stuck `state=in_progress` marker. Follow the diagnosis paths above.

## Tuning knobs

| Property | Default | When to change |
|---|---|---|
| `MigrationVersion` | `"v1"` | Opaque monotonic version identifier (`v1`, `v2`, …). Change **only** when a rollup Lambda code change alters what the migration produces (schema change, aggregate rewrite). The value is persisted into the SSM marker at completion, and a future deploy with a different string routes to full flow. Do NOT encode a release version or a bug-fix suffix here — this string persists into every customer's Parameter Store and drives their next migration decision. Customer operators never touch this; template authors do, once per schema state. |
| `Days` | `30` | Increase (up to `90`) if retention is longer and you need older rollup data. Decrease to skip older hours. |
| `ChunkHours` | `1` | The safest default — worst-case per-chunk work is ~10-30 s of Athena, ~30× under Lambda's 900 s cap. Raise (up to `168` = one week) on nearly-idle stacks to reduce SFN state-transition cost. Do not lower — 1 is the atomic unit and any smaller value is rejected by `_plan_migration_chunks`. |
| `ForceFresh` | `"false"` | Customer-safe default — never destructively re-migrates on a routine version bump. Set to `"true"` explicitly ONLY when deliberately wiping and restarting from a clean slate. Flip back to `"false"` after the run. |
| `ServiceTimeout` | `60` | Dispatcher's own timeout. Don't touch — the dispatcher only starts SFN and returns; it doesn't run the migration. |

**Sizing beyond the defaults.** The rollup Lambda's `ReservedConcurrentExecutions` (12) and the state machine's `MigrateChunks.MaxConcurrency` (8) are paired. If you raise MaxConcurrency, raise Reserved by the same delta plus 4 for cron headroom — and stay under Athena's DML-per-workgroup limit (default 20, so `MaxConcurrency × 2 arms + ~4 cron queries ≤ 20` means practical ceiling is `MaxConcurrency=8`). Raising past 8 requires an Athena workgroup quota increase; do that before, not after, deploying the higher value.

## Design references

- Source: `src/lambda/data_mart_rollup/index.py` (task-mode handlers), `src/statemachine/data_mart_migration.asl.json` (state-machine definition), `template.yaml::DataMartMigrationStateMachine` (CFN wiring).
- Tests: `lib/idp_common_pkg/tests/unit/lambdas/test_data_mart_rollup.py::Test{CheckMarkerState,WriteMarker,PurgeRollupPrefixes,PlanMigrationChunks,CheckHoursFailed,BackfillMigrateDeprecatedNoOp}`.
- History: 2.0 replaces a Lambda-only design (`mode: backfill_migrate`) that couldn't fit real-volume windows in the 45 min budget of Lambda async retries. See the CHANGELOG entry for 2.0 for the incident summary.
