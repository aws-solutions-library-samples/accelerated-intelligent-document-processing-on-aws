# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Search Tracking Table module for IDP CLI.

Provides functionality to search the DynamoDB tracking table by PK and ObjectStatus.
"""

import json
import logging
import statistics
from datetime import datetime
from typing import Optional

import boto3
from botocore.config import Config
from rich.console import Console
from rich.table import Table

from idp_sdk import IDPClient

logger = logging.getLogger(__name__)
console = Console()


class TrackingTableSearcher:
    """Search the DynamoDB tracking table for documents."""

    def __init__(self, stack_name: str, region: Optional[str] = None):
        """Initialize tracking table searcher.

        Args:
            stack_name: CloudFormation stack name
            region: AWS region (optional, uses default if not provided)
        """
        self.stack_name = stack_name
        self.region = region

        # Get stack resources via IDPClient
        client = IDPClient(stack_name=stack_name, region=region)
        self.resources = client.stack.get_resources()

        # Initialize DynamoDB client
        session = boto3.Session(region_name=region)
        config = Config(max_pool_connections=50)
        self.dynamodb = session.client("dynamodb", config=config)

        # Get table name
        self.table_name = self.resources.documents_table

    def search_by_pk_and_status(self, pk: str, object_status: str) -> dict:
        """Search for documents by PK substring and ObjectStatus.

        Args:
            pk: Primary key substring to search for (uses contains match)
            object_status: Object status to filter by (e.g., COMPLETED, FAILED, QUEUED)

        Returns:
            Dict with search results
        """
        if not self.table_name:
            return {
                "success": False,
                "error": "DocumentsTable not found in stack resources",
            }

        try:
            console.print(
                f"[yellow]Searching tracking table for PK containing '{pk}' and ObjectStatus='{object_status}'...[/yellow]"
            )

            # Build scan parameters with filter expression using contains()
            scan_kwargs = {
                "TableName": self.table_name,
                "FilterExpression": "contains(PK, :pk) AND ObjectStatus = :status",
                "ExpressionAttributeValues": {
                    ":pk": {"S": pk},
                    ":status": {"S": object_status},
                },
            }

            # Paginate through all matching documents
            matching_items = []
            last_evaluated_key = None

            while True:
                if last_evaluated_key:
                    scan_kwargs["ExclusiveStartKey"] = last_evaluated_key

                response = self.dynamodb.scan(**scan_kwargs)
                items = response.get("Items", [])
                matching_items.extend(items)

                last_evaluated_key = response.get("LastEvaluatedKey")
                if not last_evaluated_key:
                    break

            count = len(matching_items)
            console.print(f"[green]✓ Found {count} matching documents[/green]")

            return {
                "success": True,
                "count": count,
                "items": matching_items,
                "pk": pk,
                "object_status": object_status,
            }

        except Exception as e:
            logger.error(f"Failed to search tracking table: {e}")
            return {"success": False, "error": str(e)}

    def display_results(self, results: dict, show_details: bool = False):
        """Display search results in a formatted table.

        Args:
            results: Results dictionary from search_by_pk_and_status
            show_details: Whether to show detailed item information
        """
        if not results.get("success"):
            console.print(f"[red]✗ Error: {results.get('error')}[/red]")
            return

        count = results.get("count", 0)
        pk = results.get("pk", "")
        object_status = results.get("object_status", "")

        console.print()
        console.print("[bold]Search Results:[/bold]")
        console.print(f"  PK: [cyan]{pk}[/cyan]")
        console.print(f"  ObjectStatus: [cyan]{object_status}[/cyan]")
        console.print(f"  Count: [green]{count}[/green]")

        if show_details and count > 0:
            items = results.get("items", [])

            # Create a table to display item details
            table = Table(title="\nMatching Documents (showing first 50)")
            table.add_column("ObjectKey", style="cyan")
            table.add_column("Status", style="yellow")
            table.add_column("PK", style="magenta")

            # Show first 50 items
            for item in items[:50]:
                object_key = item.get("ObjectKey", {}).get("S", "N/A")
                status = item.get("ObjectStatus", {}).get("S", "N/A")
                pk_value = item.get("PK", {}).get("S", "N/A")

                table.add_row(object_key, status, pk_value)

            console.print(table)

            if count > 50:
                console.print(
                    f"\n[yellow]Note: Showing first 50 of {count} results[/yellow]"
                )

    def calculate_timing_statistics(
        self, results: dict, include_metering: bool = True
    ) -> dict:
        """Calculate timing statistics from search results.

        Args:
            results: Results dictionary from search_by_pk_and_status
            include_metering: Whether to include Lambda metering statistics

        Returns:
            Dict with timing statistics and optionally metering data
        """
        if not results.get("success") or results.get("count", 0) == 0:
            return {
                "success": False,
                "error": "No results to analyze",
            }

        items = results.get("items", [])
        processing_data = []  # List of (duration, object_key) tuples
        queue_data = []
        total_data = []

        # Metering data: stage -> list of (gb_seconds, object_key) tuples
        metering_data = {
            "Assessment": [],
            "OCR": [],
            "Classification": [],
            "Extraction": [],
            "Summarization": [],
        }

        valid_count = 0
        missing_data_count = 0
        metering_count = 0

        for item in items:
            try:
                # Extract timestamps and ObjectKey from DynamoDB format
                workflow_start = item.get("WorkflowStartTime", {}).get("S")
                completion_time = item.get("CompletionTime", {}).get("S")
                queued_time = item.get("QueuedTime", {}).get("S")
                object_key = item.get("ObjectKey", {}).get("S", "Unknown")

                # Parse timestamps
                if workflow_start and completion_time:
                    start_dt = datetime.fromisoformat(workflow_start)
                    complete_dt = datetime.fromisoformat(completion_time)

                    # Processing time: WorkflowStartTime to CompletionTime
                    processing_duration = (complete_dt - start_dt).total_seconds()
                    processing_data.append((processing_duration, object_key))

                    # Total time: QueuedTime to CompletionTime
                    if queued_time:
                        queued_dt = datetime.fromisoformat(queued_time)
                        total_duration = (complete_dt - queued_dt).total_seconds()
                        total_data.append((total_duration, object_key))

                        # Queue time: QueuedTime to WorkflowStartTime
                        queue_duration = (start_dt - queued_dt).total_seconds()
                        queue_data.append((queue_duration, object_key))

                    valid_count += 1
                else:
                    missing_data_count += 1

            except Exception as e:
                logger.debug(f"Error parsing timestamps for item: {e}")
                missing_data_count += 1
                continue

            # Extract metering data if requested.
            #
            # Deliberately outside the timestamp `try` above. A malformed metering
            # reading used to raise into that handler, which incremented
            # `missing_data_count` for an item whose timestamps had parsed fine and
            # had already incremented `valid_count` — so one document was reported as
            # both valid and missing, and the two numbers no longer added up to the
            # search count. A reading this function cannot read says nothing about
            # whether the document's timestamps were there.
            if include_metering:
                metering = self._metering_readings(item)

                # Whether *this* item contributed a reading. The count used to be
                # `if any(metering_data.values())`, which asks about the accumulator
                # rather than about this item: once any earlier item had put a
                # reading in, every later item carrying a `Metering` attribute was
                # counted even when it contributed nothing — and it was
                # order-dependent, so the same set of documents gave a different
                # count depending on which came first.
                contributed = False

                for stage in metering_data.keys():
                    reading = metering.get(f"{stage}/lambda/duration")
                    if not isinstance(reading, dict):
                        if reading is not None:
                            # A bare number where an object belongs. Reported rather
                            # than dropped at debug level, because this is a reading
                            # the run paid for and nothing else will mention it.
                            logger.warning(
                                "Ignoring malformed %s metering reading for %s: "
                                "expected an object with gb_seconds, got %r",
                                stage,
                                object_key,
                                reading,
                            )
                        continue

                    gb_seconds = reading.get("gb_seconds", 0)
                    if isinstance(gb_seconds, bool) or not isinstance(
                        gb_seconds, (int, float)
                    ):
                        logger.warning(
                            "Ignoring non-numeric %s gb_seconds for %s: %r",
                            stage,
                            object_key,
                            gb_seconds,
                        )
                        continue

                    if gb_seconds > 0:
                        metering_data[stage].append((gb_seconds, object_key))
                        contributed = True

                if contributed:
                    metering_count += 1

        if not processing_data:
            return {
                "success": False,
                "error": f"No valid timing data found. {missing_data_count} items missing required timestamps.",
            }

        # Helper function to calculate stats with min/max tracking
        def calc_stats(data_list):
            times = [d[0] for d in data_list]
            min_item = min(data_list, key=lambda x: x[0])
            max_item = max(data_list, key=lambda x: x[0])

            return {
                "average": statistics.mean(times),
                "median": statistics.median(times),
                "min": min_item[0],
                "min_key": min_item[1],
                "max": max_item[0],
                "max_key": max_item[1],
                "stdev": statistics.stdev(times) if len(times) > 1 else 0,
                "total": sum(times),
            }

        # Calculate statistics
        stats = {
            "success": True,
            "valid_count": valid_count,
            "missing_data_count": missing_data_count,
            "processing_time": calc_stats(processing_data),
        }

        # Add queue time statistics if available
        if queue_data:
            stats["queue_time"] = calc_stats(queue_data)

        # Add total time statistics if available
        if total_data:
            stats["total_time"] = calc_stats(total_data)

        # Add metering statistics if available
        if include_metering and metering_count > 0:
            stats["metering_count"] = metering_count
            stats["metering"] = {}

            for stage, data in metering_data.items():
                if data:
                    stats["metering"][stage] = calc_stats(data)

        return stats

    def _metering_readings(self, item: dict) -> dict:
        """The `Metering` attribute of one tracking-table item, as a plain dict.

        Two storage shapes are in the same table — a JSON string under `S` on items
        the pipeline wrote one way and a native DynamoDB map under `M` on the other —
        and a reader that handles only one silently reports no metering for half the
        table.

        Returns an empty dict for anything it cannot read, and never raises: a
        malformed metering blob is a lost reading, not a reason to stop counting the
        document's timestamps or to abandon the rest of the batch.

        Args:
            item: One item in DynamoDB attribute-value form.

        Returns:
            The parsed readings, or `{}`.
        """
        metering_raw = item.get("Metering")
        if not isinstance(metering_raw, dict):
            return {}

        try:
            if metering_raw.get("S"):
                # `json.loads` will happily return a number or a list for a blob that
                # is valid JSON but not an object, and the caller does `.get` on this.
                parsed = json.loads(metering_raw["S"])
                return parsed if isinstance(parsed, dict) else {}
            if metering_raw.get("M"):
                return self._parse_dynamodb_map(metering_raw["M"])
        except Exception as e:
            logger.warning(f"Could not read the Metering attribute of an item: {e}")
            return {}

        return {}

    def _parse_dynamodb_map(self, dynamodb_map: dict) -> dict:
        """Parse DynamoDB Map format to Python dict.

        Args:
            dynamodb_map: DynamoDB Map structure with type descriptors

        Returns:
            Parsed Python dictionary
        """
        result = {}
        for key, value in dynamodb_map.items():
            if "S" in value:
                result[key] = value["S"]
            elif "N" in value:
                result[key] = float(value["N"])
            elif "M" in value:
                result[key] = self._parse_dynamodb_map(value["M"])
            elif "L" in value:
                result[key] = [self._parse_dynamodb_value(item) for item in value["L"]]
            elif "BOOL" in value:
                result[key] = value["BOOL"]
            elif "NULL" in value:
                result[key] = None
        return result

    def _parse_dynamodb_value(self, value: dict):
        """Parse a single DynamoDB value."""
        if "S" in value:
            return value["S"]
        elif "N" in value:
            return float(value["N"])
        elif "M" in value:
            return self._parse_dynamodb_map(value["M"])
        elif "L" in value:
            return [self._parse_dynamodb_value(item) for item in value["L"]]
        elif "BOOL" in value:
            return value["BOOL"]
        elif "NULL" in value:
            return None
        return None

    def display_timing_statistics(self, stats: dict):
        """Display timing statistics in a formatted table.

        Args:
            stats: Statistics dictionary from calculate_timing_statistics
        """
        if not stats.get("success"):
            console.print(f"[red]✗ Error: {stats.get('error')}[/red]")
            return

        console.print()
        console.print("[bold]Timing Statistics:[/bold]")
        console.print(f"  Valid documents: [green]{stats['valid_count']}[/green]")

        if stats.get("missing_data_count", 0) > 0:
            console.print(
                f"  Missing data: [yellow]{stats['missing_data_count']}[/yellow]"
            )

        # Helper function to format seconds to human-readable
        def format_duration(seconds: float) -> str:
            if seconds < 60:
                return f"{seconds:.2f}s"
            elif seconds < 3600:
                minutes = seconds / 60
                return f"{minutes:.2f}m ({seconds:.1f}s)"
            else:
                hours = seconds / 3600
                return f"{hours:.2f}h ({seconds:.1f}s)"

        # Processing Time Statistics
        if "processing_time" in stats:
            console.print()
            console.print(
                "[bold cyan]Processing Time (WorkflowStartTime → CompletionTime):[/bold cyan]"
            )
            pt = stats["processing_time"]

            table = Table(show_header=True, header_style="bold magenta")
            table.add_column("Metric", style="cyan")
            table.add_column("Value", style="yellow")
            table.add_column("ObjectKey", style="dim")

            table.add_row("Average", format_duration(pt["average"]), "")
            table.add_row("Median", format_duration(pt["median"]), "")
            table.add_row("Minimum", format_duration(pt["min"]), pt.get("min_key", ""))
            table.add_row("Maximum", format_duration(pt["max"]), pt.get("max_key", ""))
            if pt["stdev"] > 0:
                table.add_row("Std Dev", format_duration(pt["stdev"]), "")
            table.add_row("Total", format_duration(pt["total"]), "")

            console.print(table)

        # Queue Time Statistics
        if "queue_time" in stats:
            console.print()
            console.print(
                "[bold cyan]Queue Time (QueuedTime → WorkflowStartTime):[/bold cyan]"
            )
            qt = stats["queue_time"]

            table = Table(show_header=True, header_style="bold magenta")
            table.add_column("Metric", style="cyan")
            table.add_column("Value", style="yellow")
            table.add_column("ObjectKey", style="dim")

            table.add_row("Average", format_duration(qt["average"]), "")
            table.add_row("Median", format_duration(qt["median"]), "")
            table.add_row("Minimum", format_duration(qt["min"]), qt.get("min_key", ""))
            table.add_row("Maximum", format_duration(qt["max"]), qt.get("max_key", ""))
            if qt["stdev"] > 0:
                table.add_row("Std Dev", format_duration(qt["stdev"]), "")
            table.add_row("Total", format_duration(qt["total"]), "")

            console.print(table)

        # Total Time Statistics
        if "total_time" in stats:
            console.print()
            console.print(
                "[bold cyan]Total Time (QueuedTime → CompletionTime):[/bold cyan]"
            )
            tt = stats["total_time"]

            table = Table(show_header=True, header_style="bold magenta")
            table.add_column("Metric", style="cyan")
            table.add_column("Value", style="yellow")
            table.add_column("ObjectKey", style="dim")

            table.add_row("Average", format_duration(tt["average"]), "")
            table.add_row("Median", format_duration(tt["median"]), "")
            table.add_row("Minimum", format_duration(tt["min"]), tt.get("min_key", ""))
            table.add_row("Maximum", format_duration(tt["max"]), tt.get("max_key", ""))
            if tt["stdev"] > 0:
                table.add_row("Std Dev", format_duration(tt["stdev"]), "")
            table.add_row("Total", format_duration(tt["total"]), "")

            console.print(table)

        # Lambda Metering Statistics
        if "metering" in stats and stats["metering"]:
            console.print()
            console.print(
                "[bold green]Lambda Metering (GB-seconds by Stage):[/bold green]"
            )
            console.print(
                f"  Documents with metering: [green]{stats.get('metering_count', 0)}[/green]"
            )

            for stage, stage_stats in stats["metering"].items():
                console.print()
                console.print(f"[bold yellow]{stage}:[/bold yellow]")

                table = Table(show_header=True, header_style="bold magenta")
                table.add_column("Metric", style="cyan")
                table.add_column("GB-seconds", style="yellow")
                table.add_column("ObjectKey", style="dim")

                table.add_row("Average", f"{stage_stats['average']:.2f}", "")
                table.add_row("Median", f"{stage_stats['median']:.2f}", "")
                table.add_row(
                    "Minimum",
                    f"{stage_stats['min']:.2f}",
                    stage_stats.get("min_key", ""),
                )
                table.add_row(
                    "Maximum",
                    f"{stage_stats['max']:.2f}",
                    stage_stats.get("max_key", ""),
                )
                if stage_stats["stdev"] > 0:
                    table.add_row("Std Dev", f"{stage_stats['stdev']:.2f}", "")
                table.add_row("Total", f"{stage_stats['total']:.2f}", "")

                console.print(table)

                # Add cost estimate (AWS Lambda pricing: $0.0000166667 per GB-second)
                cost_per_gb_second = 0.0000166667
                total_cost = stage_stats["total"] * cost_per_gb_second
                avg_cost = stage_stats["average"] * cost_per_gb_second
                console.print(
                    f"  [dim]Estimated cost: ${total_cost:.4f} total, ${avg_cost:.6f} avg per document[/dim]"
                )
