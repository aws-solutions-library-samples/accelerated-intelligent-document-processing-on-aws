# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Updated: 2024-10-17 16:31 - OCR quota fix

import json
import os
import time
import boto3
from datetime import datetime, timedelta
from log_sanitizer import sanitize_event_for_logging
from validation import (
    ValidationError,
    get_validated_env_vars,
    sanitize_json_input,
    validate_capacity_input,
)

# Cache for processing times to avoid repeated DynamoDB queries
_processing_times_cache = {}
_cache_expiry = 0
CACHE_DURATION_SECONDS = 300  # 5 minutes
from decimal import Decimal
from typing import Any, Dict

import boto3
from botocore.exceptions import ClientError


def convert_decimal_to_float(obj):
    """Convert DynamoDB Decimal types to Python float/int for JSON serialization and math operations."""
    if isinstance(obj, Decimal):
        # Convert to int if it's a whole number, otherwise float
        if obj % 1 == 0:
            return int(obj)
        return float(obj)
    elif isinstance(obj, dict):
        return {k: convert_decimal_to_float(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_decimal_to_float(item) for item in obj]
    return obj


def retry_with_backoff(func, max_retries=3, base_delay=1):
    """Retry function with exponential backoff for API rate limiting."""
    for attempt in range(max_retries):
        try:
            return func()
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', '')
            if error_code in ['Throttling', 'TooManyRequestsException', 'RequestLimitExceeded']:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    print(f"⏳ Rate limited, retrying in {delay}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                    continue
            raise e
        except Exception as e:
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"⏳ API error, retrying in {delay}s (attempt {attempt + 1}/{max_retries}): {e}")
                time.sleep(delay)
                continue
            raise e
    return None


def get_real_latency_metrics(pattern):
    """Get processing times from recent processed documents' metering data.
    
    Uses the actual metering data structure from DynamoDB with Metering attribute.
    Estimates processing time from gb_seconds (Lambda GB-seconds = memory_gb * time_seconds).
    """
    global _processing_times_cache, _cache_expiry
    
    # Check cache first
    current_time = time.time()
    if current_time < _cache_expiry and pattern in _processing_times_cache:
        print(f"✅ Using cached processing times for {pattern}")
        return _processing_times_cache[pattern]
    
    # Get real processing times from recent documents
    try:
        dynamodb = boto3.resource('dynamodb')
        table_name = os.environ.get('TRACKING_TABLE')
        if not table_name:
            raise ValueError("TRACKING_TABLE environment variable not set - cannot calculate capacity without processed documents")
            
        table = dynamodb.Table(table_name)
        
        # Query recent documents with pagination support
        # Use attribute_exists with correct attribute name (Metering, not meteringData)
        # Time filter: only use recent documents for latency metrics (default 24 hours)
        # Read latencyMetricsHours from input if provided (passed from UI time range selector)
        # Falls back to environment variable, then default 24 hours
        latency_metrics_hours = int(os.environ.get("LATENCY_METRICS_HOURS", "24"))
        min_recent_docs = int(os.environ.get("LATENCY_METRICS_MIN_DOCS", "5"))
        cutoff_time = datetime.utcnow() - timedelta(hours=latency_metrics_hours)
        cutoff_iso = cutoff_time.strftime('%Y-%m-%dT%H:%M:%S')
        print(f"🔍 Scanning DynamoDB table: {table_name} for documents with Metering data (last {latency_metrics_hours}h, cutoff: {cutoff_iso})")

        # Pagination parameters
        MAX_ITEMS_TO_PROCESS = 200  # Process up to 200 documents
        PAGE_SIZE = 100  # Items per page

        items = []
        last_evaluated_key = None
        pages_scanned = 0
        max_pages = (MAX_ITEMS_TO_PROCESS // PAGE_SIZE) + 1

        # Scan with pagination
        while len(items) < MAX_ITEMS_TO_PROCESS and pages_scanned < max_pages:
            scan_kwargs = {
                'FilterExpression': 'attribute_exists(Metering)',
                'Limit': PAGE_SIZE
            }

            if last_evaluated_key:
                scan_kwargs['ExclusiveStartKey'] = last_evaluated_key

            response = table.scan(**scan_kwargs)

            page_items = response.get('Items', [])
            items.extend(page_items)
            pages_scanned += 1

            print(f"📄 Page {pages_scanned}: Found {len(page_items)} items (total: {len(items)})")

            # Check if there are more pages
            last_evaluated_key = response.get('LastEvaluatedKey')
            if not last_evaluated_key:
                print(f"✅ Scanned all available items: {len(items)} total")
                break

        print(f"🔍 Found {len(items)} documents with Metering attribute (from {pages_scanned} pages)")

        if not items:
            # Try alternative attribute name with pagination
            print("⚠️ No items with 'Metering' attribute, trying 'meteringData'...")
            items = []
            last_evaluated_key = None
            pages_scanned = 0

            while len(items) < MAX_ITEMS_TO_PROCESS and pages_scanned < max_pages:
                scan_kwargs = {
                    'FilterExpression': 'attribute_exists(meteringData)',
                    'Limit': PAGE_SIZE
                }

                if last_evaluated_key:
                    scan_kwargs['ExclusiveStartKey'] = last_evaluated_key

                response = table.scan(**scan_kwargs)

                page_items = response.get('Items', [])
                items.extend(page_items)
                pages_scanned += 1

                print(f"📄 Page {pages_scanned}: Found {len(page_items)} items (total: {len(items)})")

                last_evaluated_key = response.get('LastEvaluatedKey')
                if not last_evaluated_key:
                    break

            print(f"🔍 Found {len(items)} documents with meteringData attribute (from {pages_scanned} pages)")
        
        if not items:
            raise ValueError("No processed documents found with metering data. Please process some documents first to enable capacity planning.")
        
        # Apply time filter: only use documents completed within the configured time window
        # This prevents old outliers from skewing latency percentiles
        all_items_count = len(items)
        recent_items = []
        for item in items:
            ct = item.get('CompletionTime') or item.get('LastEventTime') or item.get('InitialEventTime')
            if ct:
                ct_str = str(ct)
                # Simple string comparison works for ISO timestamps (YYYY-MM-DD...)
                if ct_str >= cutoff_iso:
                    recent_items.append(item)
        
        if len(recent_items) >= min_recent_docs:
            print(f"📊 Time filter: Using {len(recent_items)} recent documents (last {latency_metrics_hours}h) out of {all_items_count} total")
            items = recent_items
        else:
            print(f"⚠️ Time filter: Only {len(recent_items)} recent documents found (min {min_recent_docs} required). Using all {all_items_count} documents.")
        
        # Extract processing times from metering data
        # Structure: "OCR/lambda/duration": {"gb_seconds": 61.6}
        # Estimate actual seconds by dividing gb_seconds by ~1 (assume ~1GB Lambda memory)
        processing_times = {"ocr": [], "classification": [], "extraction": [], "assessment": [], "summarization": []}
        total_document_times = []  # Track total end-to-end processing time per document
        actual_queue_delays = []  # Track REAL queue delays from actual documents
        
        # LAMBDA_MEMORY_GB is required - no default
        lambda_memory_gb_env = os.environ.get("LAMBDA_MEMORY_GB")
        if not lambda_memory_gb_env:
            raise ValueError("LAMBDA_MEMORY_GB environment variable not set. Set to Lambda memory size (e.g., '1.0' for 1GB) for accurate processing time calculation from gb_seconds.")
        lambda_memory_gb = float(lambda_memory_gb_env)
        
        print(f"📊 Analyzing metering data from {len(items)} documents (LAMBDA_MEMORY_GB={lambda_memory_gb})")
        
        # Debug: Show available keys from first document
        if items:
            first_item = items[0]
            print(f"🔍 DEBUG - First document keys: {list(first_item.keys())}")
            print(f"🔍 DEBUG - QueuedTime: {first_item.get('QueuedTime')}")
            print(f"🔍 DEBUG - WorkflowStartTime: {first_item.get('WorkflowStartTime')}")
            print(f"🔍 DEBUG - InitialEventTime: {first_item.get('InitialEventTime')}")
            print(f"🔍 DEBUG - CompletionTime: {first_item.get('CompletionTime')}")
        
        for item in items:
            # Try both attribute names
            metering_data = item.get('Metering', item.get('meteringData', {}))
            
            # Parse JSON string if needed
            if isinstance(metering_data, str):
                try:
                    metering_data = json.loads(metering_data)
                except json.JSONDecodeError:
                    print(f"⚠️ Failed to parse metering data as JSON")
                    continue
            
            # Convert Decimal types
            metering_data = convert_decimal_to_float(metering_data)
            
            # First, try to get total document processing time from workflow timestamps
            initial_time = item.get('InitialEventTime')
            completion_time = item.get('CompletionTime') or item.get('LastEventTime')
            
            # Handle different timestamp formats
            def parse_timestamp(ts):
                if ts is None:
                    return None
                if isinstance(ts, (int, float)):
                    return datetime.fromtimestamp(ts)
                ts_str = str(ts)
                # Try formats with timezone offset (+00:00)
                for fmt in [
                    '%Y-%m-%dT%H:%M:%S.%f%z',  # ISO format with microseconds and timezone
                    '%Y-%m-%dT%H:%M:%S%z',      # ISO format with timezone
                    '%Y-%m-%dT%H:%M:%S.%fZ',    # UTC with Z suffix
                    '%Y-%m-%dT%H:%M:%SZ',       # UTC with Z suffix (no microseconds)
                    '%Y-%m-%d %H:%M:%S'         # Simple datetime
                ]:
                    try:
                        return datetime.strptime(ts_str, fmt)
                    except ValueError:
                        continue
                # Handle +00:00 timezone by stripping it if fromisoformat available
                try:
                    # Python 3.7+ has fromisoformat
                    return datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                except (ValueError, AttributeError):
                    pass
                return None
            
            # Extract timestamps - UI shows Duration = CompletionTime - InitialEventTime
            # But we need to separate: Queue Delay vs Base Processing Time
            queued_time = item.get('queued_time') or item.get('QueuedTime')
            workflow_start_time = item.get('start_time') or item.get('WorkflowStartTime')
            completion_time_val = item.get('completion_time') or item.get('CompletionTime') or item.get('LastEventTime')
            
            # Calculate Queue Delay = WorkflowStartTime - QueuedTime (time waiting in SQS)
            if queued_time and workflow_start_time:
                try:
                    queue_start = parse_timestamp(queued_time)
                    workflow_start = parse_timestamp(workflow_start_time)
                    
                    if queue_start and workflow_start:
                        queue_delay_seconds = (workflow_start - queue_start).total_seconds()
                        if 0 <= queue_delay_seconds < 86400:  # Sanity: 0 to 24 hours
                            actual_queue_delays.append(queue_delay_seconds)
                            print(f"📊 Document {item.get('ObjectKey', 'unknown')}: Queue Delay = {queue_delay_seconds:.1f}s (WorkflowStartTime - QueuedTime)")
                except Exception as e:
                    print(f"⚠️ Could not parse queue timestamps: {e}")
            
            # Calculate Base Processing Time = CompletionTime - WorkflowStartTime (actual processing, not queue wait)
            if workflow_start_time and completion_time_val:
                try:
                    workflow_start = parse_timestamp(workflow_start_time)
                    end = parse_timestamp(completion_time_val)
                    
                    if workflow_start and end:
                        base_processing_seconds = (end - workflow_start).total_seconds()
                        if 0 < base_processing_seconds < 3600:  # Sanity check: between 0 and 1 hour
                            total_document_times.append(base_processing_seconds)
                            print(f"📊 Document {item.get('ObjectKey', 'unknown')}: Base Processing = {base_processing_seconds:.1f}s (CompletionTime - WorkflowStartTime)")
                except Exception as e:
                    print(f"⚠️ Could not parse processing timestamps: {e}")
            elif initial_time and completion_time:
                # Fallback: use total end-to-end time if WorkflowStartTime is not available
                # This matches what UI shows as "Duration"
                try:
                    start = parse_timestamp(initial_time)
                    end = parse_timestamp(completion_time)
                    
                    if start and end:
                        total_seconds = (end - start).total_seconds()
                        if 0 < total_seconds < 3600:
                            total_document_times.append(total_seconds)
                            print(f"📊 Document {item.get('ObjectKey', 'unknown')}: Total Duration = {total_seconds:.1f}s (CompletionTime - InitialEventTime, includes queue)")
                except Exception as e:
                    print(f"⚠️ Could not parse timestamps: {e}")
            
            # Extract timing data from /lambda/duration keys (gb_seconds) - for per-step breakdown
            for key, data in metering_data.items():
                if isinstance(data, dict):
                    # Check for gb_seconds in /lambda/duration keys
                    if '/lambda/duration' in key and 'gb_seconds' in data:
                        gb_seconds = data['gb_seconds']
                        # Convert gb_seconds to actual seconds (gb_seconds / memory_gb)
                        processing_time_seconds = gb_seconds / lambda_memory_gb
                        
                        if key.startswith('OCR/'):
                            processing_times["ocr"].append(processing_time_seconds)
                            print(f"📊 OCR processing time: {processing_time_seconds:.1f}s (from {gb_seconds} gb_seconds)")
                        elif key.startswith('Classification/'):
                            processing_times["classification"].append(processing_time_seconds)
                            print(f"📊 Classification processing time: {processing_time_seconds:.1f}s (from {gb_seconds} gb_seconds)")
                        elif key.startswith('Extraction/'):
                            processing_times["extraction"].append(processing_time_seconds)
                            print(f"📊 Extraction processing time: {processing_time_seconds:.1f}s (from {gb_seconds} gb_seconds)")
                        elif key.startswith('Assessment/'):
                            processing_times["assessment"].append(processing_time_seconds)
                            print(f"📊 Assessment processing time: {processing_time_seconds:.1f}s (from {gb_seconds} gb_seconds)")
                        elif key.startswith('GranularAssessment/'):
                            # GranularAssessment is tracked separately if granular assessment is enabled
                            # It will be included in assessment totals only if granular is not disabled
                            # Note: granular_assessment_enabled flag is passed from UI when available
                            processing_times["assessment"].append(processing_time_seconds)
                            print(f"📊 GranularAssessment processing time: {processing_time_seconds:.1f}s (from {gb_seconds} gb_seconds)")
                        elif key.startswith('Summarization/'):
                            processing_times["summarization"].append(processing_time_seconds)
                            print(f"📊 Summarization processing time: {processing_time_seconds:.1f}s (from {gb_seconds} gb_seconds)")
        
        # Calculate median processing times from real data
        base_times = {}
        for step, times in processing_times.items():
            if times:
                # Use median for more stable estimates
                times.sort()
                median_idx = len(times) // 2
                base_times[step] = times[median_idx]
                print(f"📊 {step} median processing time: {base_times[step]:.1f}s from {len(times)} samples")
            else:
                base_times[step] = 0
        
        # Validate we have meaningful processing times
        total_time = sum(base_times.values())
        data_source = "real_lambda_durations"  # Track data source

        # NO ESTIMATION FALLBACK - require real timing data. The check is below,
        # after the timestamp-derived total has been computed, because either
        # source is sufficient on its own: a document carrying usable
        # WorkflowStartTime/CompletionTime timestamps and no per-step gb_seconds
        # is a complete answer for the total, and testing the per-step sum here
        # would refuse it while advising the operator to supply the timestamps it
        # already has.

        # Use total document times if available (most accurate), otherwise use sum of step times
        processing_time_percentiles = {}
        if total_document_times:
            total_document_times.sort()
            n = len(total_document_times)
            median_idx = n // 2
            total_time = total_document_times[median_idx]
            data_source = "document_timestamps"
            
            # Calculate processing time percentiles for latency distribution
            processing_time_percentiles = {
                "p50": total_document_times[n // 2],
                "p75": total_document_times[int(n * 0.75)] if n > 3 else total_document_times[-1],
                "p90": total_document_times[int(n * 0.90)] if n > 9 else total_document_times[-1],
                "p95": total_document_times[int(n * 0.95)] if n > 19 else total_document_times[-1],
                "p99": total_document_times[int(n * 0.99)] if n > 99 else total_document_times[-1],
                "count": n,
            }
            print(f"✅ Using document processing times from timestamps: P50={total_time:.1f}s, P99={processing_time_percentiles['p99']:.1f}s (from {n} documents)")
        else:
            # Final validation: neither source produced a time, so there is
            # nothing to plan from. Both alternatives are named because either one
            # would have been accepted.
            total_time = sum(base_times.values())
            if total_time == 0:
                raise ValueError(
                    "No processing time data found in documents. "
                    "Documents must have either /lambda/duration gb_seconds data in Metering, "
                    "or WorkflowStartTime/CompletionTime timestamps. "
                    "Process documents through the full workflow to generate timing data."
                )
            print(f"✅ Total estimated processing time per document (sum of steps): {total_time:.1f}s")
        
        # If we have total_document_times, scale base_times proportionally
        if total_document_times and sum(base_times.values()) > 0:
            step_total = sum(base_times.values())
            scale_factor = total_time / step_total if step_total > 0 else 1.0
            if scale_factor != 1.0:
                print(f"📊 Scaling step times by {scale_factor:.2f}x to match total document time")
                base_times = {k: v * scale_factor for k, v in base_times.items()}
        
        # Calculate actual queue delay statistics from real data
        actual_queue_delay_stats = {}
        if actual_queue_delays:
            actual_queue_delays.sort()
            n = len(actual_queue_delays)
            actual_queue_delay_stats = {
                "p50": actual_queue_delays[n // 2],
                "p75": actual_queue_delays[int(n * 0.75)] if n > 3 else actual_queue_delays[-1],
                "p90": actual_queue_delays[int(n * 0.90)] if n > 9 else actual_queue_delays[-1],
                "p95": actual_queue_delays[int(n * 0.95)] if n > 19 else actual_queue_delays[-1],
                "p99": actual_queue_delays[int(n * 0.99)] if n > 99 else actual_queue_delays[-1],
                "count": n,
            }
            print(f"✅ Calculated REAL queue delays from {n} documents: P50={actual_queue_delay_stats['p50']:.1f}s, P99={actual_queue_delay_stats['p99']:.1f}s")
        else:
            print("⚠️ No queue delay data found - documents may not have QueuedTime/WorkflowStartTime timestamps")
        
        result = {
            "base_times": base_times,
            "total_processing_time": total_time,  # Store the accurate total time
            "processing_time_percentiles": processing_time_percentiles,  # Processing time P50-P99 from actual documents
            "actual_queue_delays": actual_queue_delay_stats,  # REAL queue delays from documents
            "variance_factor": 1.2,
            "data_source": data_source,
        }
        
        # Cache the result
        _processing_times_cache[pattern] = result
        _cache_expiry = current_time + CACHE_DURATION_SECONDS
        
        print(f"✅ Using real processing times from {len(items)} recent documents")
        return result
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise ValueError(f"Cannot calculate capacity planning without processed documents: {e}")


def generate_adaptive_recommendations(
    latency_distribution,
    quota_requirements,
    total_docs_per_hour,
    pattern,
    document_configs,
):
    """Generate adaptive recommendations based on enhanced analysis.
    
    Uses default thresholds if environment variables are not set.
    """
    recommendations = []

    try:
        # Basic processing info with data source
        data_source = latency_distribution.get("dataSource", "unknown")
        source_text = (
            "environment configuration"
            if data_source == "environment_config"
            else "unknown source"
        )
        recommendations.append(
            f"Processing {int(total_docs_per_hour)} documents/hour using {pattern.upper()} (based on {source_text})"
        )

        # Complexity analysis - requires environment configuration
        complexity_factor = float(
            latency_distribution.get("complexityFactor", "1.0").rstrip("x")
        )
        high_complexity_threshold_env = os.environ.get("RECOMMENDATION_HIGH_COMPLEXITY_THRESHOLD")
        medium_complexity_threshold_env = os.environ.get("RECOMMENDATION_MEDIUM_COMPLEXITY_THRESHOLD")
        if not high_complexity_threshold_env or not medium_complexity_threshold_env:
            raise ValueError("RECOMMENDATION_HIGH_COMPLEXITY_THRESHOLD and RECOMMENDATION_MEDIUM_COMPLEXITY_THRESHOLD environment variables are required.")
        high_complexity_threshold = float(high_complexity_threshold_env)
        medium_complexity_threshold = float(medium_complexity_threshold_env)

        if complexity_factor > high_complexity_threshold:
            recommendations.append(
                "⚠️ High document complexity detected - consider document preprocessing or splitting"
            )
        elif complexity_factor > medium_complexity_threshold:
            recommendations.append(
                "📊 Medium document complexity - monitor processing times and consider optimization"
            )

        # Load analysis - requires environment configuration
        load_factor = float(latency_distribution.get("loadFactor", "1.0").rstrip("x"))
        high_load_threshold_env = os.environ.get("RECOMMENDATION_HIGH_LOAD_THRESHOLD")
        medium_load_threshold_env = os.environ.get("RECOMMENDATION_MEDIUM_LOAD_THRESHOLD")
        if not high_load_threshold_env or not medium_load_threshold_env:
            raise ValueError("RECOMMENDATION_HIGH_LOAD_THRESHOLD and RECOMMENDATION_MEDIUM_LOAD_THRESHOLD environment variables are required.")
        high_load_threshold = float(high_load_threshold_env)
        medium_load_threshold = float(medium_load_threshold_env)

        if load_factor > high_load_threshold:
            recommendations.append(
                "🚨 High system load - increase quotas, add processing capacity, or distribute load across time"
            )
        elif load_factor > medium_load_threshold:
            recommendations.append(
                "⚠️ Moderate system load - monitor for bottlenecks and plan capacity increases"
            )

        # Latency analysis - requires environment configuration
        p99_seconds = float(latency_distribution.get("p99", "0s").rstrip("s"))
        high_latency_threshold_env = os.environ.get("RECOMMENDATION_HIGH_LATENCY_THRESHOLD")
        if not high_latency_threshold_env:
            raise ValueError("RECOMMENDATION_HIGH_LATENCY_THRESHOLD environment variable is required.")
        high_latency_threshold = int(high_latency_threshold_env)

        if p99_seconds > high_latency_threshold:
            recommendations.append(
                f"🐌 High P99 latency ({p99_seconds:.0f}s) - optimize document preprocessing or increase infrastructure capacity"
            )

        # Infrastructure-based recommendations
        if latency_distribution.get("exceedsLimit"):
            recommendations.append(
                "⏰ Processing time exceeds SLA - consider increasing timeouts or reducing document complexity"
            )

        # Quota analysis with specific actions
        quota_warnings = [
            req for req in quota_requirements if req.get("status") == "warning"
        ]
        if quota_warnings:
            model_names = [req.get("modelId", "Unknown") for req in quota_warnings[:3]]
            recommendations.append(
                f"📈 {len(quota_warnings)} quota increases needed for: {', '.join(model_names)}"
            )

        # Bottleneck analysis with specific guidance
        bottlenecks = latency_distribution.get("bottlenecks", [])
        if bottlenecks:
            recommendations.append(
                f"🔍 Performance bottlenecks: {', '.join(bottlenecks)} - consider scaling these services"
            )

        # Document-specific recommendations - requires environment configuration
        if document_configs:
            large_doc_threshold_env = os.environ.get("RECOMMENDATION_LARGE_DOC_THRESHOLD")
            if not large_doc_threshold_env:
                raise ValueError("RECOMMENDATION_LARGE_DOC_THRESHOLD environment variable is required.")
            large_doc_threshold = int(large_doc_threshold_env)
            high_token_docs = [
                doc
                for doc in document_configs
                if (doc.get("ocrTokens", 0) + doc.get("extractionTokens", 0))
                > large_doc_threshold
            ]
            if high_token_docs:
                recommendations.append(
                    f"📄 {len(high_token_docs)} document types exceed {large_doc_threshold} tokens - consider splitting for better performance"
                )

            # Page-based recommendations - requires environment configuration
            high_page_threshold_env = os.environ.get("RECOMMENDATION_HIGH_PAGE_THRESHOLD")
            if not high_page_threshold_env:
                raise ValueError("RECOMMENDATION_HIGH_PAGE_THRESHOLD environment variable is required.")
            high_page_threshold = int(high_page_threshold_env)
            high_page_docs = [
                doc
                for doc in document_configs
                if doc.get("avgPages", 0) > high_page_threshold
            ]
            if high_page_docs:
                recommendations.append(
                    f"📑 {len(high_page_docs)} document types have >{high_page_threshold} pages - consider parallel processing"
                )

        # Pattern-specific recommendations
        if pattern == "pattern-1" and total_docs_per_hour > 100:
            recommendations.append(
                "🔄 High volume BDA processing - ensure adequate BDA quota and consider batch optimization"
            )
        elif pattern == "pattern-3" and total_docs_per_hour > 50:
            recommendations.append(
                "🤖 High volume SageMaker classification - consider auto-scaling endpoint configuration"
            )

        # Infrastructure optimization recommendations
        variance_factor = float(
            latency_distribution.get("varianceFactor", "1.0").rstrip("x")
        )
        if variance_factor > 3.0:
            recommendations.append(
                "📈 High latency variance detected - consider implementing request queuing or load balancing"
            )
    except Exception as e:
        print(f"⚠️ Error generating recommendations: {e}")
        recommendations.append(f"ℹ️ Recommendations could not be fully generated: {str(e)[:100]}")

    return recommendations if recommendations else ["ℹ️ No specific recommendations at this time"]


def generate_rpm_quota_codes(model_ids):
    """Generate RPM quota codes dynamically based on model patterns from environment configuration."""
    rpm_quotas = {}
    
    # Get RPM quota code mappings from environment
    rpm_mapping_env = os.environ.get("BEDROCK_MODEL_RPM_QUOTA_CODES")
    if not rpm_mapping_env:
        raise ValueError("BEDROCK_MODEL_RPM_QUOTA_CODES environment variable not set")
    
    try:
        rpm_mapping = json.loads(rpm_mapping_env)
    except json.JSONDecodeError:
        raise ValueError("BEDROCK_MODEL_RPM_QUOTA_CODES must be valid JSON")
    
    for model_id in model_ids:
        # First try exact match
        if model_id in rpm_mapping:
            rpm_quotas[model_id] = rpm_mapping[model_id]
            continue
            
        # Clean model ID by removing the version suffix only. The region prefix
        # ("us.", "eu.", "global.") is what selects the inference profile, and
        # Service Quotas holds a separate limit per profile, so discarding it
        # would let a "global." model take a "us." quota code.
        clean_model_id = model_id.lower().split(':')[0]  # Remove version suffix like ":1m"

        # Try to match against cleaned mapping keys
        matched = False
        for model_type, quota_code in rpm_mapping.items():
            # Clean the mapping key the same way for comparison
            clean_mapping_key = model_type.lower().split(':')[0]

            # Match if the cleaned keys are equal or one contains the other
            if clean_model_id == clean_mapping_key or clean_model_id in clean_mapping_key or clean_mapping_key in clean_model_id:
                rpm_quotas[model_id] = quota_code
                matched = True
                break
        
        # If no match found, raise error instead of using default
        if not matched:
            raise ValueError(f"No RPM quota code mapping found for model {model_id}. Please add mapping to BEDROCK_MODEL_RPM_QUOTA_CODES environment variable.")
    
    return rpm_quotas


def get_simple_quotas():
    """Get AWS service quotas from live API for both TPM and RPM.
    
    Requires valid quota configuration and API access - no fallback defaults.
    Raises ValueError if quotas cannot be retrieved.
    """
    quotas = {"bedrock": None, "bedrock_models": {}, "bedrock_models_rpm": {}}

    quotas_client = boto3.client("service-quotas")
    region = boto3.Session().region_name
    print(f"🔍 Retrieving quotas from Service Quotas API in region: {region}")

    # Get TPM quota codes - REQUIRED
    tpm_quota_codes_env = os.environ.get("BEDROCK_MODEL_QUOTA_CODES")
    if not tpm_quota_codes_env:
        raise ValueError("BEDROCK_MODEL_QUOTA_CODES environment variable not set. Configure model quota mappings in the Lambda environment.")
    
    try:
        tpm_model_quotas = json.loads(tpm_quota_codes_env)
    except json.JSONDecodeError as e:
        raise ValueError(f"BEDROCK_MODEL_QUOTA_CODES must be valid JSON: {e}")

    if not tpm_model_quotas:
        raise ValueError("BEDROCK_MODEL_QUOTA_CODES is empty. Add model quota code mappings.")

    # Generate RPM quota codes - REQUIRED
    rpm_model_quotas = generate_rpm_quota_codes(tpm_model_quotas.keys())

    # Test API access
    try:
        test_response = quotas_client.list_service_quotas(
            ServiceCode="bedrock", MaxResults=100
        )
        available_quotas = test_response.get("Quotas", [])
        print(f"✅ Service Quotas API accessible, found {len(available_quotas)} bedrock quotas")
    except Exception as e:
        raise ValueError(f"Cannot access Service Quotas API: {type(e).__name__} - {str(e)}. Check IAM permissions for servicequotas:GetServiceQuota.")

    retrieved_count = 0

    # Retrieve TPM quotas - REQUIRED for each configured model
    for model_id, quota_code in tpm_model_quotas.items():
        print(f"🔍 Requesting TPM quota for {model_id} with code {quota_code}...")
        
        def get_tpm_quota():
            return quotas_client.get_service_quota(
                ServiceCode="bedrock", QuotaCode=quota_code
            )
        
        model_quota = retry_with_backoff(get_tpm_quota)
        if not model_quota:
            raise ValueError(f"Failed to retrieve TPM quota for model {model_id} (quota code: {quota_code}). Verify the quota code is correct.")
        
        quota_value = int(model_quota["Quota"]["Value"])
        quotas["bedrock_models"][model_id] = quota_value
        print(f"✅ Retrieved {model_id} TPM quota: {quota_value}")
        retrieved_count += 1
        
        # Small delay to prevent rate limiting
        time.sleep(0.1)

    # Retrieve RPM quotas - REQUIRED for each configured model
    for model_id, quota_code in rpm_model_quotas.items():
        print(f"🔍 Requesting RPM quota for {model_id} with code {quota_code}...")
        
        def get_rpm_quota():
            return quotas_client.get_service_quota(
                ServiceCode="bedrock", QuotaCode=quota_code
            )
        
        model_quota = retry_with_backoff(get_rpm_quota)
        if not model_quota:
            raise ValueError(f"Failed to retrieve RPM quota for model {model_id} (quota code: {quota_code}). Verify the quota code is correct.")
        
        quota_value = int(model_quota["Quota"]["Value"])
        quotas["bedrock_models_rpm"][model_id] = quota_value
        print(f"✅ Retrieved {model_id} RPM quota: {quota_value}")
        retrieved_count += 1
        
        # Small delay to prevent rate limiting
        time.sleep(0.1)

    print(f"📊 Retrieved {retrieved_count} quotas from AWS Service Quotas API")

    # Set bedrock quota from retrieved values
    if quotas["bedrock_models"]:
        quotas["bedrock"] = max(quotas["bedrock_models"].values())
    else:
        raise ValueError("No Bedrock model quotas were retrieved. Check BEDROCK_MODEL_QUOTA_CODES configuration.")

    return quotas


def calculate_document_complexity_factor(document_configs):
    """Calculate complexity factor based on document characteristics.
    
    Uses default thresholds if environment variables are not set.
    """
    if not document_configs:
        return 1.0

    try:
        # Document complexity thresholds from environment - all required
        medium_complexity_threshold_env = os.environ.get("MEDIUM_COMPLEXITY_THRESHOLD")
        high_complexity_threshold_env = os.environ.get("HIGH_COMPLEXITY_THRESHOLD")
        page_complexity_factor_env = os.environ.get("PAGE_COMPLEXITY_FACTOR")
        high_complexity_multiplier_env = os.environ.get("HIGH_COMPLEXITY_MULTIPLIER")
        medium_complexity_multiplier_env = os.environ.get("MEDIUM_COMPLEXITY_MULTIPLIER")
        
        if not all([medium_complexity_threshold_env, high_complexity_threshold_env, page_complexity_factor_env, high_complexity_multiplier_env, medium_complexity_multiplier_env]):
            raise ValueError("All complexity threshold environment variables are required: MEDIUM_COMPLEXITY_THRESHOLD, HIGH_COMPLEXITY_THRESHOLD, PAGE_COMPLEXITY_FACTOR, HIGH_COMPLEXITY_MULTIPLIER, MEDIUM_COMPLEXITY_MULTIPLIER")
        
        medium_complexity_threshold = int(medium_complexity_threshold_env)
        high_complexity_threshold = int(high_complexity_threshold_env)
        page_complexity_factor = float(page_complexity_factor_env)
        high_complexity_multiplier = float(high_complexity_multiplier_env)
        medium_complexity_multiplier = float(medium_complexity_multiplier_env)

        total_complexity = 0
        total_docs = 0

        for doc_config in document_configs:
            docs_count = doc_config.get("docsPerHour", 0)
            if docs_count == 0:
                continue

            # Base complexity factors
            pages = doc_config.get("avgPages", 0)
            if pages == 0:
                raise ValueError(f"No page data for document type. Populate tokens from processed documents or enter avgPages manually.")
            page_factor = 1.0 + (pages - 1) * page_complexity_factor

            # Token density indicates document complexity
            total_tokens = (
                doc_config.get("ocrTokens", 0)
                + doc_config.get("classificationTokens", 0)
                + doc_config.get("extractionTokens", 0)
            )
            tokens_per_page = total_tokens / pages if pages > 0 else 0

            # Complexity based on token density
            if tokens_per_page > high_complexity_threshold:
                complexity_factor = high_complexity_multiplier
            elif tokens_per_page > medium_complexity_threshold:
                complexity_factor = medium_complexity_multiplier
            else:
                complexity_factor = 1.0

            doc_complexity = page_factor * complexity_factor
            total_complexity += doc_complexity * docs_count
            total_docs += docs_count

        if total_docs == 0:
            return 1.0

        return total_complexity / total_docs
    except Exception as e:
        raise ValueError(f"Error calculating complexity factor: {e}")


def calculate_latency_distribution(
    docs_per_hour,
    pages_per_hour,
    tokens_per_hour,
    pattern,
    max_allowed_latency,
    quotas,
    document_configs=None,
):
    """
    Calculate latency distribution using simplified approach.
    Quota capacity determines processing speed - insufficient quota causes throttling and delays.
    """

    # max_allowed_latency is in seconds (from frontend), convert to minutes for internal calculations
    max_allowed_minutes = max_allowed_latency / 60

    # Get processing capacity from quotas - REQUIRED, no defaults
    bedrock_quota_tpm = quotas.get("bedrock")
    if not bedrock_quota_tpm:
        raise ValueError("Bedrock TPM quota not available. Ensure BEDROCK_MODEL_QUOTA_CODES environment variable is configured and Service Quotas API is accessible.")
    
    bedrock_quota_rpm = quotas.get("bedrock_models_rpm", {})
    if not bedrock_quota_rpm:
        raise ValueError("Bedrock RPM quotas not available. Ensure BEDROCK_MODEL_RPM_QUOTA_CODES environment variable is configured and Service Quotas API is accessible.")
    
    # Calculate effective processing capacity with realistic token estimation
    total_tokens = sum(tokens_per_hour.values()) if isinstance(tokens_per_hour, dict) else tokens_per_hour
    min_tokens_per_request_env = os.environ.get("MIN_TOKENS_PER_REQUEST")
    if not min_tokens_per_request_env:
        raise ValueError("MIN_TOKENS_PER_REQUEST environment variable is required for accurate capacity calculation.")
    min_tokens_per_request = int(min_tokens_per_request_env)
    
    # Calculate actual average tokens per request
    actual_avg_tokens = total_tokens / max(docs_per_hour, 1) if docs_per_hour > 0 else 0
    
    # Use actual tokens if reasonable, otherwise use minimum for safety
    if actual_avg_tokens >= min_tokens_per_request:
        avg_tokens_per_request = actual_avg_tokens
        print(f"🔍 Using actual token average: {actual_avg_tokens:.0f} tokens/request")
    else:
        avg_tokens_per_request = min_tokens_per_request
        print(f"⚠️ Using minimum token safety: {min_tokens_per_request} tokens/request (actual: {actual_avg_tokens:.0f})")
        
    # Add warning if using minimum significantly differs from actual
    if actual_avg_tokens > 0 and actual_avg_tokens < min_tokens_per_request * 0.5:
        print(f"⚠️ WARNING: Document tokens ({actual_avg_tokens:.0f}) much lower than minimum ({min_tokens_per_request}). Consider reviewing document configuration.")
    
    # Capacity limited by either tokens or requests with realistic calculations
    token_limited_capacity = bedrock_quota_tpm / avg_tokens_per_request  # docs/min
    request_limited_capacity = min(bedrock_quota_rpm.values()) if bedrock_quota_rpm else 0
    
    effective_capacity = min(token_limited_capacity, request_limited_capacity)
    
    print(f"🔍 Capacity Analysis:")
    print(f"  - Token-limited capacity: {token_limited_capacity:.1f} docs/min ({bedrock_quota_tpm} TPM ÷ {avg_tokens_per_request:.0f} tokens)")
    print(f"  - Request-limited capacity: {request_limited_capacity:.1f} docs/min")
    print(f"  - Effective capacity: {effective_capacity:.1f} docs/min")
    
    # Convert hourly demand to per-minute with validation
    docs_per_minute = docs_per_hour / 60
    
    if docs_per_minute <= 0:
        print("⚠️ WARNING: No document processing demand configured")
        return {
            "p50": "0s", "p75": "0s", "p90": "0s", "p95": "0s", "p99": "0s",
            "maxAllowed": f"{max_allowed_minutes * 60:.1f}s",
            "baseLatency": "0s", "queueLatency": "0s", "totalLatency": "0s",
            "loadFactor": "0x", "complexityFactor": "1.0x", "varianceFactor": "1.0x",
            "pattern": pattern, "exceedsLimit": False, "dataSource": "no_demand",
            "processingRate": f"{effective_capacity:.0f} docs/min",
            "demandRate": "0 docs/min", "quotaUtilization": "0%",
            "warningMessage": "No processing demand configured"
        }
    
    # Capacity analysis - NO ESTIMATES, only real measured values
    # When demand > capacity, we show the overload ratio but DO NOT estimate queue delay
    # (Queue delay depends on how long overload lasts - we cannot predict this)
    
    utilization = docs_per_minute / effective_capacity if effective_capacity > 0 else 0.0
    is_overloaded = docs_per_minute > effective_capacity
    
    if is_overloaded:
        # Calculate overload rate (how fast queue grows) - this IS measurable
        queue_buildup_rate = docs_per_minute - effective_capacity  # docs/min added to queue
        
        print(f"📊 Capacity Analysis (OVERLOAD DETECTED):")
        print(f"  - Demand: {docs_per_minute:.1f} docs/min")
        print(f"  - Capacity: {effective_capacity:.1f} docs/min")
        print(f"  - Utilization: {utilization:.1%} (EXCEEDS 100%)")
        print(f"  - Queue growth rate: {queue_buildup_rate:.1f} docs/min accumulating")
        print(f"  ⚠️ Demand exceeds capacity - documents WILL queue")
        
        bottleneck_services = [f"Bedrock Quota ({utilization:.1%} utilization - OVERLOADED)"]
        
        # Queue delay is NOT estimated - we report 0 and flag overload separately
        # User should reduce demand or increase quota
        queue_latency_minutes = 0.0  # We don't estimate - overload flag tells the story
    else:
        # Demand <= capacity: system can keep up, no queueing
        queue_latency_minutes = 0.0
        queue_buildup_rate = 0.0
        bottleneck_services = []
        
        print(f"📊 Capacity Analysis (Within Capacity):")
        print(f"  - Demand: {docs_per_minute:.1f} docs/min")
        print(f"  - Capacity: {effective_capacity:.1f} docs/min")
        print(f"  - Utilization: {utilization:.1%}")
        print(f"  ✅ System can keep up - no queueing expected")

    # Get ACTUAL processing time and queue delays from real documents
    try:
        latency_data = get_real_latency_metrics(pattern)
        base_times = latency_data["base_times"]
        
        # Use total_processing_time if available (from timestamps), otherwise sum of steps
        if "total_processing_time" in latency_data:
            actual_processing_seconds = latency_data["total_processing_time"]
        else:
            actual_processing_seconds = float(sum(base_times.values()))
        
        actual_processing_minutes = actual_processing_seconds / 60
        
        # Get REAL queue delays and processing time percentiles from processed documents
        actual_queue_delays = latency_data.get("actual_queue_delays", {})
        processing_percentiles = latency_data.get("processing_time_percentiles", {})
        data_source = latency_data.get("data_source", "unknown")
        
        print(f"📊 Actual processing time from documents: {actual_processing_seconds:.1f}s ({actual_processing_minutes:.2f}min)")
        if actual_queue_delays:
            print(f"📊 Real queue delays from documents: P50={actual_queue_delays.get('p50', 0):.1f}s, P99={actual_queue_delays.get('p99', 0):.1f}s")
        if processing_percentiles:
            print(f"📊 Processing time percentiles: P50={processing_percentiles.get('p50', 0):.1f}s, P99={processing_percentiles.get('p99', 0):.1f}s")
    except Exception as e:
        raise ValueError(f"Unable to get processing time metrics: {e}")

    # Get processing time percentiles from actual documents (for latency distribution variation)
    if processing_percentiles and processing_percentiles.get("count", 0) > 0:
        # Use actual measured processing time percentiles - 100% real data!
        proc_p50 = processing_percentiles.get("p50", actual_processing_seconds)
        proc_p75 = processing_percentiles.get("p75", actual_processing_seconds)
        proc_p90 = processing_percentiles.get("p90", actual_processing_seconds)
        proc_p95 = processing_percentiles.get("p95", actual_processing_seconds)
        proc_p99 = processing_percentiles.get("p99", actual_processing_seconds)
        data_source = "document_timestamps"
        print(f"✅ Using REAL processing time percentiles from {processing_percentiles.get('count', 0)} documents")
    else:
        # Use median for all percentiles (no variation data available)
        proc_p50 = actual_processing_seconds
        proc_p75 = actual_processing_seconds
        proc_p90 = actual_processing_seconds
        proc_p95 = actual_processing_seconds
        proc_p99 = actual_processing_seconds
        print(f"⚠️ No processing time percentiles - using median {actual_processing_seconds:.1f}s for all")

    # Base processing time is ALWAYS the actual measured P50 time
    base_latency_seconds = proc_p50

    # Use REAL queue delays from actual documents if available
    if actual_queue_delays and actual_queue_delays.get("count", 0) > 0:
        # Use actual measured percentiles - 100% real data!
        queue_p50 = actual_queue_delays.get("p50", 0)
        queue_p75 = actual_queue_delays.get("p75", 0)
        queue_p90 = actual_queue_delays.get("p90", 0)
        queue_p95 = actual_queue_delays.get("p95", 0)
        queue_p99 = actual_queue_delays.get("p99", 0)
        
        # Total latency = base processing + actual queue delay (use P50 for "typical")
        queue_latency_seconds = queue_p50
        
        print(f"✅ Using REAL queue delays from {actual_queue_delays.get('count', 0)} documents")
    else:
        # No queue data available - report 0 but warn user
        queue_p50 = 0
        queue_p75 = 0
        queue_p90 = 0
        queue_p95 = 0
        queue_p99 = 0
        queue_latency_seconds = 0
        
        print(f"⚠️ No queue delay data available - documents may not have QueuedTime/WorkflowStartTime timestamps")

    total_latency_seconds = base_latency_seconds + queue_latency_seconds
    total_latency_minutes = total_latency_seconds / 60
    
    # Calculate complexity factor
    complexity_factor = calculate_document_complexity_factor(document_configs or [])
    
    # Create percentile distribution from REAL DATA
    # Total latency = processing time percentile + queue delay percentile
    p50_seconds = proc_p50 + queue_p50
    p75_seconds = proc_p75 + queue_p75
    p90_seconds = proc_p90 + queue_p90
    p95_seconds = proc_p95 + queue_p95
    p99_seconds = proc_p99 + queue_p99

    # Check if latency exceeds limits
    exceeds_limit = total_latency_minutes > max_allowed_minutes
    warning_message = None

    if exceeds_limit:
        warning_message = f"Processing time ({total_latency_minutes:.1f}min) exceeds SLA ({max_allowed_minutes:.1f}min) due to insufficient quota capacity"

    # Calculate factors for display
    load_factor = utilization
    variance_factor = complexity_factor * (1.0 + max(0, utilization - 1.0) * 0.5)

    # Calculate actual processing time in seconds (before SLA adjustment)
    actual_processing_seconds = actual_processing_minutes * 60

    result = {
        "p50": f"{p50_seconds:.1f}s",
        "p75": f"{p75_seconds:.1f}s", 
        "p90": f"{p90_seconds:.1f}s",
        "p95": f"{p95_seconds:.1f}s",
        "p99": f"{p99_seconds:.1f}s",
        # Separate processing time percentiles (for stacked bar UI)
        "procP50": f"{proc_p50:.1f}s",
        "procP75": f"{proc_p75:.1f}s",
        "procP90": f"{proc_p90:.1f}s",
        "procP95": f"{proc_p95:.1f}s",
        "procP99": f"{proc_p99:.1f}s",
        # Separate queue delay percentiles (for stacked bar UI)
        "queueP50": f"{queue_p50:.1f}s",
        "queueP75": f"{queue_p75:.1f}s",
        "queueP90": f"{queue_p90:.1f}s",
        "queueP95": f"{queue_p95:.1f}s",
        "queueP99": f"{queue_p99:.1f}s",
        "maxAllowed": f"{max_allowed_minutes * 60:.1f}s",
        "baseLatency": f"{base_latency_seconds:.1f}s",
        "actualProcessingTime": f"{actual_processing_seconds:.1f}s",
        "queueLatency": f"{queue_latency_seconds:.2f}s",
        "totalLatency": f"{total_latency_seconds:.1f}s",
        "loadFactor": f"{load_factor:.2f}x",
        "complexityFactor": f"{complexity_factor:.2f}x", 
        "varianceFactor": f"{variance_factor:.2f}x",
        "pattern": pattern,
        "exceedsLimit": exceeds_limit,
        "dataSource": data_source,
        "processingRate": f"{effective_capacity:.0f} docs/min",
        "demandRate": f"{docs_per_minute:.1f} docs/min",
        "quotaUtilization": f"{utilization:.1%}",
        "quotaOverloaded": is_overloaded,
    }

    if warning_message:
        result["warningMessage"] = warning_message

    if bottleneck_services:
        result["bottlenecks"] = bottleneck_services

    return result


def _lookup_quota(quotas_dict, model_id):
    """Look up quota for a model ID, handling :1m suffix fallback.
    
    The ':1m' suffix is a local convention indicating 1M context window support.
    It's not a real model variant - it shares the same AWS Service Quotas as the base model.
    When looking up quotas, we try exact match first, then fall back to the base model ID.
    """
    # Try exact match first
    quota = quotas_dict.get(model_id)
    if quota is not None:
        return quota
    
    # If model ID ends with :1m, try base model ID and :0 variant
    if model_id.endswith(":1m"):
        base_id = model_id[:-3]  # Strip ":1m"
        quota = quotas_dict.get(base_id)
        if quota is not None:
            print(f"🔍 Quota lookup: mapped {model_id} → {base_id}")
            return quota
        quota = quotas_dict.get(base_id + ":0")
        if quota is not None:
            print(f"🔍 Quota lookup: mapped {model_id} → {base_id}:0")
            return quota
    
    return None


def build_simple_quota_requirements(
    pages_per_hour,
    tokens_per_hour,
    docs_per_hour,
    quotas,
    max_allowed_latency,
    pattern,
    model_config,
    hourly_breakdown,
    latency_distribution=None,
    document_configs=None,
    granular_assessment_enabled=True,
):
    """Build quota requirements analysis using peak hour demand per inference type.
    
    Args:
        granular_assessment_enabled: When False, GranularAssessment requests are excluded from Assessment RPM calculation.
    """
    requirements = []

    print(f"Starting quota requirements build for {pattern}")
    print(f"Model config: {model_config}")
    print(f"🔍 Granular assessment enabled (in quota builder): {granular_assessment_enabled}")

    # Calculate peak hour demand for each inference type (no artificial factors)
    peak_ocr_tpm = 0
    peak_classification_tpm = 0
    peak_extraction_tpm = 0
    peak_assessment_tpm = 0
    peak_summarization_tpm = 0

    # Add 10% buffer to base demand for safety margin
    BUFFER_FACTOR = 1.1  # 10% buffer
    
    for hour_data in hourly_breakdown:
        ocr_tpm = hour_data.get("ocrTokensPerHour", 0) / 60 * BUFFER_FACTOR
        classification_tpm = hour_data["classificationTokensPerHour"] / 60 * BUFFER_FACTOR
        extraction_tpm = hour_data["extractionTokensPerHour"] / 60 * BUFFER_FACTOR
        assessment_tpm = hour_data["assessmentTokensPerHour"] / 60 * BUFFER_FACTOR
        summarization_tpm = hour_data["summarizationTokensPerHour"] / 60 * BUFFER_FACTOR

        peak_ocr_tpm = max(peak_ocr_tpm, ocr_tpm)
        peak_classification_tpm = max(peak_classification_tpm, classification_tpm)
        peak_extraction_tpm = max(peak_extraction_tpm, extraction_tpm)
        peak_assessment_tpm = max(peak_assessment_tpm, assessment_tpm)
        peak_summarization_tpm = max(peak_summarization_tpm, summarization_tpm)

    print(
        f"🔍 Peak demands (10% buffer applied) - OCR: {peak_ocr_tpm:.0f}, Classification: {peak_classification_tpm:.0f}, Extraction: {peak_extraction_tpm:.0f}, Assessment: {peak_assessment_tpm:.0f}, Summarization: {peak_summarization_tpm:.0f}"
    )
    
    # Map inference types to their peak demands and models
    inference_demands = {}
    if pattern == "pattern-1":
        # Use only configured models - no defaults
        if model_config.get("summarization_model"):
            inference_demands["Summarization"] = (
                peak_summarization_tpm,
                model_config.get("summarization_model"),
            )
    elif pattern == "pattern-2":
        # Use only configured models - no defaults
        if model_config.get("classification_model"):
            inference_demands["Classification"] = (
                peak_classification_tpm,
                model_config.get("classification_model"),
            )
        if model_config.get("extraction_model"):
            inference_demands["Extraction"] = (peak_extraction_tpm, model_config.get("extraction_model"))
        if model_config.get("assessment_model"):
            inference_demands["Assessment"] = (peak_assessment_tpm, model_config.get("assessment_model"))
        if model_config.get("summarization_model"):
            inference_demands["Summarization"] = (
                peak_summarization_tpm,
                model_config.get("summarization_model"),
            )
        # OCR model only included if configured (null means Textract, not Bedrock)
        ocr_model = model_config.get("ocr_model")
        if ocr_model and ocr_model.strip():
            inference_demands["OCR"] = (peak_ocr_tpm, ocr_model.strip())
    else:  # pattern-3
        # Use only configured models - no defaults
        if model_config.get("extraction_model"):
            inference_demands["Extraction"] = (peak_extraction_tpm, model_config.get("extraction_model"))
        if model_config.get("assessment_model"):
            inference_demands["Assessment"] = (peak_assessment_tpm, model_config.get("assessment_model"))
        if model_config.get("summarization_model"):
            inference_demands["Summarization"] = (
                peak_summarization_tpm,
                model_config.get("summarization_model"),
            )
        # OCR model only included if configured (null means Textract, not Bedrock)
        ocr_model = model_config.get("ocr_model")
        if ocr_model and ocr_model.strip():
            inference_demands["OCR"] = (peak_ocr_tpm, ocr_model.strip())

    # Build requirements for each inference type
    print(f"Processing inference demands: {inference_demands}")

    for step_name, (peak_tpm, model_id) in inference_demands.items():
        print(f"🔍 Processing {step_name}: peak_tpm={peak_tpm}, model_id='{model_id}'")

        # Validate model configuration
        if not model_id:
            print(f"⚠️ Skipping {step_name} - no model configured")
            continue

        # Get TPM model quota - REQUIRED, no defaults
        # Uses _lookup_quota to handle :1m suffix fallback (shares quota with base model)
        model_quota_tpm = _lookup_quota(quotas.get("bedrock_models", {}), model_id)
        if model_quota_tpm is None:
            raise ValueError(f"TPM quota not available for model {model_id} ({step_name}). Add model to BEDROCK_MODEL_QUOTA_CODES environment variable.")

        # Get RPM model quota - REQUIRED, no defaults
        # Uses _lookup_quota to handle :1m suffix fallback (shares quota with base model)
        model_quota_rpm = _lookup_quota(quotas.get("bedrock_models_rpm", {}), model_id)
        if model_quota_rpm is None:
            raise ValueError(f"RPM quota not available for model {model_id} ({step_name}). Add model to BEDROCK_MODEL_RPM_QUOTA_CODES environment variable.")

        print(f"🔍 Retrieved quotas for {model_id} ({step_name}): {model_quota_tpm} TPM, {model_quota_rpm} RPM")

        # Try to get actual metering data from DynamoDB table, with fallback to estimation
        dynamodb = boto3.resource('dynamodb')
        metering_table_name = os.environ.get('METERING_TABLE_NAME')
        
        requests_per_doc = 0  # Average requests per document for this step
        actual_pages_per_doc = None
        metering_data_available = False
        
        if metering_table_name:
            try:
                table = dynamodb.Table(metering_table_name)

                # Query recent metering data with pagination support
                MAX_METERING_ITEMS = 100  # Sample up to 100 documents for RPM calculation
                PAGE_SIZE = 50

                items = []
                last_evaluated_key = None
                pages_scanned = 0
                max_pages = (MAX_METERING_ITEMS // PAGE_SIZE) + 1

                while len(items) < MAX_METERING_ITEMS and pages_scanned < max_pages:
                    scan_kwargs = {
                        'FilterExpression': boto3.dynamodb.conditions.Attr('Metering').exists(),
                        'Limit': PAGE_SIZE
                    }

                    if last_evaluated_key:
                        scan_kwargs['ExclusiveStartKey'] = last_evaluated_key

                    response = table.scan(**scan_kwargs)

                    page_items = response.get('Items', [])
                    items.extend(page_items)
                    pages_scanned += 1

                    last_evaluated_key = response.get('LastEvaluatedKey')
                    if not last_evaluated_key:
                        break

                print(f"🔍 Found {len(items)} metering records (from {pages_scanned} pages)")
                
                # Debug: Show all metering keys from first record
                if items:
                    first_metering = items[0].get('Metering', {})
                    if isinstance(first_metering, str):
                        import json as json_module
                        first_metering = json_module.loads(first_metering)
                    first_metering = convert_decimal_to_float(first_metering)
                    print(f"🔍 Metering keys: {list(first_metering.keys())}")
                
                # Calculate AVERAGE requests per document from metering data
                total_requests = 0
                doc_count = 0
                
                for item in items:
                    metering_data = item.get('Metering', {})
                    if isinstance(metering_data, str):
                        import json as json_module
                        metering_data = json_module.loads(metering_data)
                    # Convert Decimal types to float/int for math operations
                    metering_data = convert_decimal_to_float(metering_data)
                    
                    # Extract actual page count from metering data
                    if 'number_of_pages' in item:
                        # float() so the running average below is well-typed:
                        # convert_decimal_to_float() is recursive and so is inferred
                        # as returning a scalar/dict/list union, which the "+" and "/"
                        # below cannot accept. A page count is always scalar.
                        pages = float(convert_decimal_to_float(item['number_of_pages']))
                        if actual_pages_per_doc is None:
                            actual_pages_per_doc = pages
                        else:
                            actual_pages_per_doc = (actual_pages_per_doc + pages) / 2  # Running average
                    
                    # Requests this document contributes to this step. A step can
                    # issue several Bedrock calls under distinct metering keys —
                    # agentic extraction records one entry per model or tool
                    # invocation, and Assessment records regular and granular
                    # entries separately — so every matching key is accumulated and
                    # the document counts once towards the average however many it
                    # carried. One accumulator serves both branches below: which of
                    # them runs is fixed by step_name for the whole scan, and
                    # counting per-key in one and per-document in the other is the
                    # defect this replaced.
                    requests_for_doc = 0
                    found_data_for_doc = False

                    for key, value in metering_data.items():
                        # Look for bedrock entries that match the processing step
                        if isinstance(value, dict) and 'bedrock' in key.lower():
                            key_lower = key.lower()
                            
                            # Special handling for Assessment
                            if step_name == "Assessment":
                                is_assessment = key_lower.startswith('assessment/')
                                is_granular = key_lower.startswith('granularassessment/')
                                
                                if is_granular:
                                    # GranularAssessment entry found
                                    if granular_assessment_enabled:
                                        # Count granular requests when enabled
                                        requests = value.get('requests', 0)
                                        if requests > 0:
                                            requests_for_doc += requests
                                            found_data_for_doc = True
                                            print(f"🔍 Document {item.get('ObjectKey', 'unknown')}: GranularAssessment = {requests} requests (granular enabled)")
                                    else:
                                        # Skip GranularAssessment when disabled
                                        print(f"🔍 Skipping GranularAssessment (disabled): {key}")
                                elif is_assessment:
                                    # Regular Assessment entry - always use actual requests
                                    requests = value.get('requests', 0)
                                    if requests > 0:
                                        requests_for_doc += requests
                                        found_data_for_doc = True
                                        print(f"🔍 Document {item.get('ObjectKey', 'unknown')}: Assessment = {requests} requests (from {key})")
                                # Continue iterating to find all assessment-related entries
                                continue
                            else:
                                # For other steps, use standard substring matching
                                if step_name.lower() not in key_lower:
                                    continue
                                
                                # Use actual requests from metering data, continuing
                                # through the remaining keys rather than stopping at
                                # the first match.
                                requests = value.get('requests', 0)
                                if requests > 0:
                                    requests_for_doc += requests
                                    found_data_for_doc = True
                                    print(f"🔍 Document {item.get('ObjectKey', 'unknown')}: {step_name} = {requests} requests (from {key})")

                    if found_data_for_doc:
                        total_requests += requests_for_doc
                        doc_count += 1
                        metering_data_available = True
                
                # Calculate average requests per document
                if doc_count > 0:
                    requests_per_doc = total_requests / doc_count
                    print(f"✅ {step_name}: Average {requests_per_doc:.1f} requests/doc from {doc_count} documents")
                        
            except Exception as e:
                print(f"⚠️ Could not read metering data for {step_name}: {e}")
        else:
            print("⚠️ METERING_TABLE_NAME not configured - using estimation")
        
        # Calculate actual requests per hour based on scheduled docs/hour and avg requests/doc
        actual_requests_per_hour = 0
        if requests_per_doc > 0:
            # Take the busiest hour's docs/hour and multiply by avg requests per doc.
            # RPM is a per-minute limit, so it has to be scaled from one hour, as the
            # peak_*_tpm figures above are: summing all 24 would report a whole day's
            # volume as a per-minute rate, 24x over for a load spread evenly across
            # the day. This maximum is over docsPerHour where TPM's is over that
            # step's tokensPerHour, so the two can land on different hours; that is
            # the conservative answer, since each quota must cover its own worst hour.
            peak_scheduled_docs_per_hour = max(
                (h.get("docsPerHour", 0) for h in hourly_breakdown), default=0
            )
            actual_requests_per_hour = requests_per_doc * peak_scheduled_docs_per_hour
            print(f"🔍 {step_name} RPM calc: {requests_per_doc:.1f} req/doc × {peak_scheduled_docs_per_hour} docs/hour (peak hour) = {actual_requests_per_hour:.1f} requests/hour")
        
        # If no metering data available and no demand, skip this step quietly
        if (not metering_data_available or actual_requests_per_hour == 0) and peak_tpm == 0:
            print(f"ℹ️ Skipping {step_name} - no demand and no metering data")
            continue
        
        # If there IS demand but no metering data, require metering data
        if not metering_data_available or actual_requests_per_hour == 0:
            # For OCR specifically, if tokens are 0, it means OCR is not being used
            if step_name == "OCR" and peak_tpm == 0:
                print(f"ℹ️ Skipping OCR - no OCR tokens configured (OCR not in use)")
                continue
            
            raise ValueError(
                f"No request count data found for {step_name}. "
                f"Documents must have metering data with '{step_name.lower()}/bedrock' entries. "
                f"Process documents through the full workflow to generate metering data."
            )
        
        peak_rpm = (actual_requests_per_hour / 60) * BUFFER_FACTOR
        
        print(f"🔍 RPM calculation: {actual_requests_per_hour} req/hour / 60 * {BUFFER_FACTOR:.2f}x buffer = {peak_rpm:.1f} RPM")
        
        # Log actual page count if found
        if actual_pages_per_doc is not None:
            print(f"📄 Actual pages per document from metering: {actual_pages_per_doc:.1f}")
        else:
            print("ℹ️ Using configured page values (no metering data)")

        # Include configured inference types with demand
        should_include = peak_tpm > 0 or peak_rpm > 1.0  # Include if there's meaningful demand

        if should_include:
            print(
                f"✅ Including {step_name} with {peak_tpm} TPM ({peak_rpm:.1f} RPM), quotas: {model_quota_tpm} TPM, {model_quota_rpm} RPM"
            )

            # TPM requirement
            tpm_quota_display = f"{model_quota_tpm:,}"
            tpm_status = "success" if peak_tpm <= model_quota_tpm else "warning"
            if peak_tpm == 0:
                tpm_status_text = "✅ No Demand"
            else:
                tpm_status_text = (
                    "✅ Sufficient" if peak_tpm <= model_quota_tpm else "⚠️ Increase Needed"
                )
            tpm_utilization_percent = (
                min((peak_tpm / model_quota_tpm) * 100, 100)
                if peak_tpm > 0 and model_quota_tpm > 0
                else 0
            )

            # Extract readable model name from model ID
            model_display_name = model_id.split('.')[-1].split(':')[0]  # e.g., "claude-3-haiku-20240307-v1"
            
            tpm_requirement = {
                "service": f"{step_name} ({model_display_name}) - TPM",
                "category": "Bedrock Models TPM",
                "currentQuota": tpm_quota_display,
                "requiredQuota": f"{round(peak_tpm):,}",
                "status": tpm_status,
                "statusText": tpm_status_text,
                "utilizationPercent": tpm_utilization_percent,
                "usedFor": step_name,
                "modelId": model_id,
                "quotaType": "TPM",
            }
            requirements.append(tpm_requirement)

            # RPM requirement
            rpm_quota_display = f"{model_quota_rpm:,}"
            rpm_status = "success" if peak_rpm <= model_quota_rpm else "warning"
            if peak_rpm == 0:
                rpm_status_text = "✅ No Demand"
            else:
                rpm_status_text = (
                    "✅ Sufficient" if peak_rpm <= model_quota_rpm else "⚠️ Increase Needed"
                )
            rpm_utilization_percent = (
                min((peak_rpm / model_quota_rpm) * 100, 100)
                if peak_rpm > 0 and model_quota_rpm > 0
                else 0
            )

            rpm_requirement = {
                "service": f"{step_name} ({model_display_name}) - RPM",
                "category": "Bedrock Models RPM",
                "currentQuota": rpm_quota_display,
                "requiredQuota": f"{round(peak_rpm):,}",
                "status": rpm_status,
                "statusText": rpm_status_text,
                "utilizationPercent": rpm_utilization_percent,
                "usedFor": step_name,
                "modelId": model_id,
                "quotaType": "RPM",
            }
            requirements.append(rpm_requirement)
        else:
            print(f"❌ Skipping {step_name} - no demand (peak_tpm={peak_tpm})")

    print(f"Built {len(requirements)} quota requirements")
    return requirements


def lambda_handler(event: Dict[str, Any], context) -> Dict[str, Any]:
    """Simplified capacity calculation function with proper JSON handling."""

    try:
        # Validate environment variables on cold start (cached globally)
        try:
            env_vars = get_validated_env_vars()
            print("✅ Environment variables validated successfully")
        except ValidationError as e:
            print(f"❌ Environment validation failed: {e}")
            return {
                "success": False,
                "errorMessage": f"Configuration error: {str(e)}",
                "metrics": [{"label": "Status", "value": "Configuration Error"}],
                "quotaRequirements": [],
                "latencyDistribution": {
                    "p50": "0s", "p75": "0s", "p90": "0s", "p95": "0s", "p99": "0s",
                    "baseLatency": "0s", "queueLatency": "0s", "totalLatency": "0s",
                    "exceedsLimit": False, "maxAllowed": "0s",
                },
                "calculationDetails": {"quotasUsed": {"bedrock_models": {}}},
                "recommendations": [f"❌ Configuration error: {str(e)[:200]}"],
            }

        # The 1000-character cap bounds log volume; it does not redact anything,
        # so the sanitizer still has to run first. This function is reachable from
        # the resolver below, which forwards the caller's AppSync event in full.
        print(
            "Received event: "
            f"{json.dumps(sanitize_event_for_logging(event), default=str)[:1000]}"
        )

        # Handle different event formats (direct call vs GraphQL resolver)
        if "body" in event:
            # Called from HTTP API Gateway
            body_data = event["body"]
            if isinstance(body_data, str):
                # Validate and parse JSON with size limits
                try:
                    input_data = sanitize_json_input(body_data, max_size_bytes=1_000_000)
                except ValidationError as e:
                    print(f"❌ Input validation error: {e}")
                    return {
                        "statusCode": 400,
                        "body": json.dumps(
                            {
                                "success": False,
                                "errorMessage": f"Invalid input: {str(e)}",
                            }
                        ),
                    }
            else:
                input_data = body_data
        elif "arguments" in event:
            # Called from AppSync GraphQL resolver - input is a JSON string
            raw_input = event.get("arguments", {}).get("input", "{}")
            print(f"🔍 GraphQL resolver input (type={type(raw_input).__name__}): {str(raw_input)[:500]}")

            if isinstance(raw_input, str):
                try:
                    input_data = sanitize_json_input(raw_input, max_size_bytes=1_000_000)
                except ValidationError as e:
                    print(f"❌ Input validation error from GraphQL: {e}")
                    return {
                        "success": False,
                        "errorMessage": f"Invalid input: {str(e)}",
                    }
            elif isinstance(raw_input, dict):
                input_data = raw_input
            else:
                return {
                    "success": False,
                    "errorMessage": f"Unexpected input type: {type(raw_input).__name__}",
                }
        else:
            # Direct invocation with input at root level
            input_data = event

        # Validate capacity calculation input parameters
        try:
            validate_capacity_input(input_data)
            print("✅ Input parameters validated successfully")
        except ValidationError as e:
            print(f"❌ Input parameter validation failed: {e}")
            error_result = {
                "success": False,
                "errorMessage": f"Invalid input parameters: {str(e)}",
                "metrics": [{"label": "Status", "value": "Validation Error"}],
                "quotaRequirements": [],
                "latencyDistribution": {
                    "p50": "0s", "p75": "0s", "p90": "0s", "p95": "0s", "p99": "0s",
                    "baseLatency": "0s", "queueLatency": "0s", "totalLatency": "0s",
                    "exceedsLimit": False, "maxAllowed": "0s",
                },
                "calculationDetails": {"quotasUsed": {"bedrock_models": {}}},
                "recommendations": [f"❌ {str(e)[:200]}"],
            }
            if "body" in event:
                return {"statusCode": 400, "body": json.dumps(error_result)}
            else:
                return error_result

        # Parse input for capacity calculation
        document_configs = input_data.get("documentConfigs", [])
        pattern = input_data.get("pattern", "pattern-2")

        print(f"🔍 DEBUG: input_data keys: {list(input_data.keys())}")
        print(
            f"🔍 DEBUG: maxAllowedLatency value: {input_data.get('maxAllowedLatency')}"
        )

        # Normalize pattern format and validate supported patterns
        if pattern.startswith("PATTERN-"):
            pattern = pattern.lower()

        # Unified pattern uses the same Bedrock pipeline as Pattern-2
        if pattern == "unified":
            print(f"ℹ️ Unified pattern detected, treating as pattern-2 for capacity planning")
            pattern = "pattern-2"

        if pattern != "pattern-2":
            print(f"❌ Unsupported pattern: {pattern}")
            error_result = {
                "success": False,
                "errorMessage": f"Only Pattern 2 and Unified patterns are supported for capacity planning. Received: {pattern}",
                "metrics": [
                    {"label": "Status", "value": "Unsupported Pattern"},
                    {"label": "Pattern", "value": pattern},
                ],
                "quotaRequirements": [],
                "latencyDistribution": {
                    "p50": "0s", "p75": "0s", "p90": "0s", "p95": "0s", "p99": "0s",
                    "baseLatency": "0s", "queueLatency": "0s", "totalLatency": "0s",
                    "exceedsLimit": False, "maxAllowed": "0s",
                },
                "calculationDetails": {"quotasUsed": {"bedrock_models": {}}},
                "recommendations": [f"❌ Only Pattern 2 and Unified patterns are supported. Received: {pattern}"],
            }
            if "body" in event:
                return {"statusCode": 400, "body": json.dumps(error_result)}
            else:
                return error_result

        max_allowed_latency = input_data.get("maxAllowedLatency") or input_data.get(
            "max_allowed_latency"
        )
        if max_allowed_latency:
            max_allowed_latency = float(max_allowed_latency)
        else:
            raise ValueError("maxAllowedLatency or max_allowed_latency is required")

        if not document_configs:
            error_result = {
                "success": False,
                "errorMessage": "No document configurations provided",
                "metrics": [
                    {"label": "Status", "value": "Missing Configuration"},
                    {"label": "Details", "value": "Add document types first"},
                ],
                "quotaRequirements": [],
                "latencyDistribution": {
                    "p50": "0s", "p75": "0s", "p90": "0s", "p95": "0s", "p99": "0s",
                    "baseLatency": "0s", "queueLatency": "0s", "totalLatency": "0s",
                    "exceedsLimit": False, "maxAllowed": "0s",
                },
                "calculationDetails": {"quotasUsed": {"bedrock_models": {}}},
                "recommendations": ["❌ No document configurations provided. Add document types in the Document Processing section."],
            }
            if "body" in event:
                return {"statusCode": 400, "body": json.dumps(error_result)}
            else:
                return error_result

        # Parse user config with proper error handling
        user_config_str = input_data.get("userConfig", "{}")
        user_config = {}
        if isinstance(user_config_str, str):
            if user_config_str.strip() and user_config_str.strip() != "{}":
                try:
                    user_config = json.loads(user_config_str)
                except json.JSONDecodeError as e:
                    print(f"⚠️ Error parsing user config: {e}, using empty config")
                    user_config = {}
        elif isinstance(user_config_str, dict):
            user_config = user_config_str

        # Get granular assessment enabled flag - defaults to True if not specified
        granular_assessment_enabled = input_data.get("granularAssessmentEnabled", True)
        print(f"🔍 Granular assessment enabled: {granular_assessment_enabled}")

        # Get model configuration
        model_config = {
            "classification_model": user_config.get("classification_model", ""),
            "extraction_model": user_config.get("extraction_model", ""),
            "assessment_model": user_config.get("assessment_model", ""),
            "summarization_model": user_config.get("summarization_model", ""),
            "ocr_model": user_config.get("ocr_model", ""),
        }

        print(f"Model config for quota builder: {model_config}")

        # Get quotas with Applied account-level values
        quotas = get_simple_quotas()

        # Process time slots with proper JSON handling
        time_slots = []
        if "timeSlots" in input_data:
            time_slots_json = input_data["timeSlots"]
            if isinstance(time_slots_json, str):
                try:
                    time_slots = json.loads(time_slots_json)
                except json.JSONDecodeError as e:
                    print(f"⚠️ Error parsing time slots: {e}")
                    time_slots = []
            else:
                time_slots = time_slots_json

        # Initialize all 24 hours with zero values
        hourly_breakdown = {}
        for hour in range(24):
            hourly_breakdown[hour] = {
                "hour": hour,
                "docsPerHour": 0,
                "pagesPerHour": 0,
                "tokensPerHour": 0,
                "ocrTokensPerHour": 0,  # Add OCR tokens tracking
                "classificationTokensPerHour": 0,
                "extractionTokensPerHour": 0,
                "assessmentTokensPerHour": 0,
                "summarizationTokensPerHour": 0,
                "documentType": "No processing scheduled",
            }

        # Process each time slot
        for slot in time_slots:
            hour = int(slot.get("hour", 0))
            doc_type = slot.get("documentType", "Unknown")
            docs_per_hour = int(slot.get("docsPerHour", 0))

            if docs_per_hour > 0:
                # Find document config for this type
                doc_config = next(
                    (dc for dc in document_configs if dc.get("type") == doc_type), {}
                )
                avg_pages = doc_config.get("avgPages", 0)
                if avg_pages == 0:
                    raise ValueError(f"No page data for document type '{doc_type}'. Populate tokens from processed documents or enter avgPages manually.")
                ocr_tokens = doc_config.get("ocrTokens", 0)  # Add OCR tokens
                classification_tokens = doc_config.get("classificationTokens", 0)
                extraction_tokens = doc_config.get("extractionTokens", 0)
                summarization_tokens = doc_config.get("summarizationTokens", 0)
                assessment_tokens = doc_config.get("assessmentTokens", 0)

                print(
                    f"🔍 Hour {hour}, DocType: {doc_type}, DocsPerHour: {docs_per_hour}, OCR tokens: {ocr_tokens}"
                )

                # Update hourly breakdown
                hourly_breakdown[hour]["docsPerHour"] += docs_per_hour
                hourly_breakdown[hour]["pagesPerHour"] += docs_per_hour * avg_pages
                hourly_breakdown[hour]["ocrTokensPerHour"] += (
                    ocr_tokens * docs_per_hour
                )  # Add OCR tokens
                hourly_breakdown[hour]["classificationTokensPerHour"] += (
                    classification_tokens * docs_per_hour
                )
                hourly_breakdown[hour]["extractionTokensPerHour"] += (
                    extraction_tokens * docs_per_hour
                )
                hourly_breakdown[hour]["assessmentTokensPerHour"] += (
                    assessment_tokens * docs_per_hour
                )
                hourly_breakdown[hour]["summarizationTokensPerHour"] += (
                    summarization_tokens * docs_per_hour
                )

                # Pattern-1 (BDA) only uses summarization tokens
                if pattern == "pattern-1":
                    total_tokens_for_slot = summarization_tokens * docs_per_hour
                else:
                    total_tokens_for_slot = (
                        ocr_tokens
                        + classification_tokens
                        + extraction_tokens
                        + assessment_tokens
                        + summarization_tokens
                    ) * docs_per_hour
                hourly_breakdown[hour]["tokensPerHour"] += total_tokens_for_slot
                hourly_breakdown[hour]["documentType"] = doc_type

        # Convert to list for all 24 hours
        hourly_breakdown_list = []
        for hour in range(24):
            hourly_breakdown_list.append(hourly_breakdown[hour])

        # Calculate totals
        total_docs_per_hour = 0
        total_pages_per_hour = 0
        total_tokens_per_hour = 0

        for doc_config in document_configs:
            docs_per_hour_config = doc_config.get("docsPerHour", 0)
            avg_pages = doc_config.get("avgPages", 0)
            if avg_pages == 0 and docs_per_hour_config > 0:
                raise ValueError(f"No page data for document type '{doc_config.get('type', 'Unknown')}'. Populate tokens from processed documents or enter avgPages manually.")

            ocr_tokens_per_doc = doc_config.get("ocrTokens", 0)
            classification_tokens_per_doc = doc_config.get("classificationTokens", 0)
            extraction_tokens_per_doc = doc_config.get("extractionTokens", 0)
            summarization_tokens_per_doc = doc_config.get("summarizationTokens", 0)
            assessment_tokens_per_doc = doc_config.get("assessmentTokens", 0)

            # Pattern-1 (BDA) only uses summarization tokens
            if pattern == "pattern-1":
                doc_total_tokens = summarization_tokens_per_doc
            else:
                doc_total_tokens = (
                    ocr_tokens_per_doc
                    + classification_tokens_per_doc
                    + extraction_tokens_per_doc
                    + assessment_tokens_per_doc
                    + summarization_tokens_per_doc
                )

            total_docs_per_hour += docs_per_hour_config
            total_pages_per_hour += docs_per_hour_config * avg_pages
            total_tokens_per_hour += doc_total_tokens * docs_per_hour_config

        # Override LATENCY_METRICS_HOURS from UI input if provided
        latency_metrics_hours_input = input_data.get("latencyMetricsHours")
        if latency_metrics_hours_input:
            os.environ["LATENCY_METRICS_HOURS"] = str(int(latency_metrics_hours_input))
            # Clear cache so new time range takes effect
            global _processing_times_cache, _cache_expiry
            _processing_times_cache = {}
            _cache_expiry = 0
            print(f"🔍 Using latency metrics time range from UI: {latency_metrics_hours_input}h")

        # Calculate latency distribution based on processing pattern and load
        latency_distribution = calculate_latency_distribution(
            total_docs_per_hour,
            total_pages_per_hour,
            total_tokens_per_hour,
            pattern,
            max_allowed_latency,
            quotas,
            document_configs,
        )

        # Build quota requirements using Applied account-level quota values
        # Pass granular_assessment_enabled directly (not via latency_distribution)
        quota_requirements = build_simple_quota_requirements(
            total_pages_per_hour,
            total_tokens_per_hour,
            total_docs_per_hour,
            quotas,
            max_allowed_latency,
            pattern,
            model_config,
            hourly_breakdown_list,
            latency_distribution,  # Pass latency distribution for concurrency calculation
            document_configs,  # Pass document configs for request count data
            granular_assessment_enabled,  # Pass granular assessment flag directly
        )

        print(f"Returning {len(quota_requirements)} quota requirements")

        # Build result - only include fields that exist in GraphQL CapacityResult schema
        result = {
            "success": True,
            "metrics": [
                {"label": "Total Docs", "value": f"{int(total_docs_per_hour):,}"},
                {"label": "Total Pages", "value": f"{int(total_pages_per_hour):,}"},
                {
                    "label": "Total Tokens",
                    "value": f"{total_tokens_per_hour / 1000000:.2f}M",
                },
            ],
            "quotaRequirements": quota_requirements,
            "latencyDistribution": latency_distribution,
            "calculationDetails": {
                "quotasUsed": {
                    "bedrock_models": quotas.get("bedrock_models", {})
                }
            },
            "recommendations": generate_adaptive_recommendations(
                latency_distribution,
                quota_requirements,
                total_docs_per_hour,
                pattern,
                document_configs,
            ),
            "errorMessage": None,
        }

        # Return in format expected by GraphQL resolver if called from resolver
        if "body" in event:
            return {"statusCode": 200, "body": json.dumps(result)}
        else:
            return result

    except Exception as e:
        print(f"❌ Error in lambda_handler: {str(e)}")
        import traceback

        traceback.print_exc()

        # Return complete error response with all expected GraphQL fields
        # to prevent AppSync from returning null
        # Note: Must match GraphQL CapacityResult schema exactly
        error_result = {
            "success": False,
            "errorMessage": f"Capacity calculation failed: {str(e)}",
            "metrics": [
                {"label": "Status", "value": "Error"},
                {"label": "Details", "value": str(e)[:100]},  # Truncate long errors
            ],
            "quotaRequirements": [],
            "latencyDistribution": {
                "p50": "0s",
                "p75": "0s",
                "p90": "0s",
                "p95": "0s",
                "p99": "0s",
                "baseLatency": "0s",
                "queueLatency": "0s",
                "totalLatency": "0s",
                "exceedsLimit": False,
                "maxAllowed": "0s",
            },
            "calculationDetails": {
                "quotasUsed": {
                    "bedrock_models": {}
                }
            },
            "recommendations": [f"❌ Error: {str(e)}"],
        }

        if "body" in event:
            return {"statusCode": 500, "body": json.dumps(error_result)}
        else:
            return error_result
