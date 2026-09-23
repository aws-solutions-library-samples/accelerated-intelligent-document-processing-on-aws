Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Monitoring Module

`idp_common.monitoring` is a shared monitoring foundation library: reusable
building blocks for the IDP system's monitoring and analysis features (agent
analysis, troubleshooting, capacity planning). All public classes and functions
are re-exported from the package, so a single import path is enough:

```python
from idp_common.monitoring import SettingsCache, TimeRange, DocumentRecord
```

## Submodules

| Module | Provides |
|--------|----------|
| `models` | Shared dataclasses: `TimeRange`, `LogEvent`, `LogSearchResult`, `TraceSegment`, `DocumentRecord`, `MonitoringKPIs`. |
| `settings_cache` | `SettingsCache` — TTL-based SSM/DynamoDB configuration cache; helpers `get_setting`, `get_cloudwatch_log_groups`. |
| `stack_utils` | Stack-name resolution and AWS resource discovery: `get_stack_name`, `extract_stack_name_from_arn`, `get_stack_resources`, `get_lambda_function_names`, `get_state_machine_arn`. |
| `stepfunctions_service` | Step Functions execution analysis: `get_execution_arn_from_document`, `get_execution_data`, `analyze_execution_timeline`, `extract_failure_details`. Its two `failed_state` rules match `TaskStateEntered` exactly, which is what keeps them clear of the `Catch`-handler misattribution described in [Naming the state that failed](../README.md#-naming-the-state-that-failed) — broadening either to a `StateEntered` *suffix* match reintroduces it. |
| `xray_service` | X-Ray trace analysis: `get_trace_for_document`, `analyze_trace`, `get_subsegment_details`, `extract_lambda_request_ids`. |
| `cloudwatch_logs_service` | CloudWatch log search: `get_stack_log_groups`, `search_log_group`, `search_by_request_ids`, `search_by_document_fallback`, `search_stack_wide`, `prioritize_performance_log_groups`. |

## Usage

```python
from idp_common.monitoring import (
    get_stack_name,
    get_trace_for_document,
    search_by_request_ids,
    TimeRange,
)

stack_name = get_stack_name()
trace = get_trace_for_document(document_id, stack_name=stack_name)
```

## Related

- [Monitoring](../../../../docs/monitoring.md) — monitoring and logging features.
- [Agent Analysis](../../../../docs/agent-analysis.md) — uses these building blocks.
- [Capacity Planning](../../../../docs/capacity-planning.md)
