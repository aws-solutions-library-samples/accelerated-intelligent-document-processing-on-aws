# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Stop workflows module for IDP CLI.

Provides functionality to stop running Step Function executions and purge SQS queues.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn

from idp_common.dynamodb import DocumentDynamoDBService
from idp_common.models import Status
from idp_sdk._core.stack_info import StackInfo

logger = logging.getLogger(__name__)
console = Console()


class WorkflowStopper:
    """Stop running workflows for an IDP stack."""

    def __init__(self, stack_name: str, region: Optional[str] = None):
        """Initialize workflow stopper.

        Args:
            stack_name: CloudFormation stack name
            region: AWS region (optional, uses default if not provided)
        """
        self.stack_name = stack_name
        self.region = region

        # Get stack resources
        stack_info = StackInfo(stack_name, region)
        self.resources = stack_info.get_resources()

        # Initialize clients with larger connection pool
        session = boto3.Session(region_name=region)
        config = Config(max_pool_connections=100)
        self.sqs = session.client("sqs", config=config)
        self.sfn = session.client("stepfunctions", config=config)

        # Get resource ARNs (using friendly names from StackInfo)
        self.queue_url = self.resources.get("DocumentQueueUrl")
        self.state_machine_arn = self.resources.get("StateMachineArn")
        self.documents_table = self.resources.get("DocumentsTable")

    def purge_queue(self) -> dict:
        """Purge all messages from the SQS queue.

        Returns:
            Dict with purge results
        """
        if not self.queue_url:
            return {
                "success": False,
                "error": "SQS queue URL not found in stack outputs",
            }

        try:
            console.print("[yellow]Purging SQS queue...[/yellow]")
            self.sqs.purge_queue(QueueUrl=self.queue_url)
            console.print("[green]✓ Queue purged successfully[/green]")
            return {"success": True, "queue_url": self.queue_url}
        except Exception as e:
            logger.error(f"Failed to purge queue: {e}")
            return {"success": False, "error": str(e)}

    def count_running_executions(self) -> int:
        """Count currently running executions."""
        count = 0
        try:
            paginator = self.sfn.get_paginator("list_executions")
            for page in paginator.paginate(
                stateMachineArn=self.state_machine_arn, statusFilter="RUNNING"
            ):
                count += len(page.get("executions", []))
        except Exception as e:
            logger.error(f"Error counting executions: {e}")
        return count

    def stop_executions(self, max_workers: int = 50, max_retries: int = 5) -> dict:
        """Stop all running Step Function executions.

        Args:
            max_workers: Maximum concurrent stop operations
            max_retries: Maximum retry passes to ensure all stopped

        Returns:
            Dict with stop results
        """
        if not self.state_machine_arn:
            return {
                "success": False,
                "error": "State machine ARN not found in stack outputs",
            }

        # Count initial running executions
        initial_count = self.count_running_executions()
        console.print(f"[yellow]Found {initial_count} running executions[/yellow]")

        if initial_count == 0:
            console.print("[green]✓ No running executions to stop[/green]")
            return {
                "success": True,
                "total_stopped": 0,
                "total_failed": 0,
                "remaining": 0,
            }

        total_stopped = 0
        total_failed = 0
        start_time = time.time()
        retry_count = 0

        def stop_single_execution(execution_arn: str) -> bool:
            """Stop a single execution."""
            try:
                self.sfn.stop_execution(
                    executionArn=execution_arn,
                    error="UserAborted",
                    cause="Stopped by idp-cli stop-workflows command",
                )
                return True
            except ClientError as e:
                # An execution that no longer exists needs no stopping, so that
                # is a success. The error code is matched rather than the
                # generated exception class: an `except` clause evaluates its
                # expression when an error is raised, so a misspelled
                # `self.sfn.exceptions.<Name>` raises AttributeError from inside
                # the handler and escapes past every `except` below it.
                if e.response.get("Error", {}).get("Code") == "ExecutionDoesNotExist":
                    return True
                logger.debug(f"Failed to stop {execution_arn}: {e}")
                return False
            except Exception as e:
                logger.debug(f"Failed to stop {execution_arn}: {e}")
                return False

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed} stopped"),
            console=console,
        ) as progress:
            task = progress.add_task("Stopping executions...", total=initial_count)

            while retry_count < max_retries:
                retry_count += 1
                batch_stopped = 0

                # Use paginator to get ALL running executions
                try:
                    paginator = self.sfn.get_paginator("list_executions")
                    all_executions = []
                    for page in paginator.paginate(
                        stateMachineArn=self.state_machine_arn, statusFilter="RUNNING"
                    ):
                        all_executions.extend(page.get("executions", []))
                except Exception as e:
                    logger.error(f"Failed to list executions: {e}")
                    break

                if not all_executions:
                    # No more running - we're done
                    break

                progress.update(
                    task, description=f"Stopping executions (pass {retry_count})..."
                )

                # Stop executions in parallel
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(stop_single_execution, ex["executionArn"]): ex[
                            "executionArn"
                        ]
                        for ex in all_executions
                    }

                    for future in as_completed(futures):
                        if future.result():
                            total_stopped += 1
                            batch_stopped += 1
                        else:
                            total_failed += 1
                        progress.update(task, completed=total_stopped)

                # Brief pause to let state machine catch up
                if batch_stopped > 0:
                    time.sleep(0.5)

        elapsed = time.time() - start_time
        rate = total_stopped / (elapsed / 60) if elapsed > 0 else 0

        # Verify final count
        final_running = self.count_running_executions()

        return {
            "success": final_running == 0,
            "total_stopped": total_stopped,
            "total_failed": total_failed,
            "elapsed_seconds": elapsed,
            "rate_per_minute": rate,
            "remaining": final_running,
        }

    def abort_queued_documents(self) -> dict:
        """Abort all documents with QUEUED status in DynamoDB.

        Uses the idp_common DocumentDynamoDBService for proper abstraction.
        Updates status to ABORTED per the models.Status enum.

        Returns:
            Dict with abort results
        """
        if not self.documents_table:
            return {
                "success": False,
                "error": "DocumentsTable not found in stack resources",
            }

        try:
            console.print("[yellow]Aborting queued documents in database...[/yellow]")

            # Initialize the document service with the table name
            doc_service = DocumentDynamoDBService(table_name=self.documents_table)

            # Scan for documents with QUEUED status
            # The DynamoDB schema uses ObjectStatus field
            queued_docs = []
            scan_kwargs = {
                "filter_expression": "ObjectStatus = :status",
                "expression_attribute_values": {":status": Status.QUEUED.value},
            }

            # Paginate through all queued documents using the client directly
            last_evaluated_key = None
            while True:
                if last_evaluated_key:
                    scan_kwargs["exclusive_start_key"] = last_evaluated_key

                response = doc_service.client.scan(**scan_kwargs)
                queued_docs.extend(response.get("Items", []))

                last_evaluated_key = response.get("LastEvaluatedKey")
                if not last_evaluated_key:
                    break

            if not queued_docs:
                console.print("[green]✓ No queued documents to abort[/green]")
                return {"success": True, "documents_aborted": 0}

            console.print(f"[yellow]Found {len(queued_docs)} queued documents[/yellow]")

            # Update each document to ABORTED status using the service
            aborted_count = 0
            failed_count = 0

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total}"),
                console=console,
            ) as progress:
                task = progress.add_task(
                    "Aborting documents...", total=len(queued_docs)
                )

                for item in queued_docs:
                    try:
                        object_key = item.get("ObjectKey")
                        if not object_key:
                            failed_count += 1
                            continue

                        # Get the document using the service
                        doc = doc_service.get_document(object_key)
                        if doc and doc.status == Status.QUEUED:
                            # Update status to ABORTED
                            doc.status = Status.ABORTED
                            doc_service.update_document(doc)
                            aborted_count += 1
                        else:
                            # Document already changed status
                            aborted_count += 1

                    except Exception as e:
                        logger.debug(
                            f"Failed to abort document {item.get('ObjectKey')}: {e}"
                        )
                        failed_count += 1

                    progress.update(task, advance=1)

            console.print(f"[green]✓ Aborted {aborted_count} documents[/green]")
            if failed_count > 0:
                console.print(f"[yellow]  {failed_count} failed to update[/yellow]")

            return {
                "success": True,
                "documents_aborted": aborted_count,
                "documents_failed": failed_count,
            }

        except Exception as e:
            logger.error(f"Failed to abort queued documents: {e}")
            return {"success": False, "error": str(e)}

    def stop_all(self, skip_purge: bool = False, skip_stop: bool = False) -> dict:
        """Stop all workflows - purge queue and stop executions.

        Args:
            skip_purge: Skip queue purge step
            skip_stop: Skip stopping executions

        Returns:
            Dict with combined results
        """
        results = {
            "queue_purge": None,
            "executions_stopped": None,
            "documents_aborted": None,
        }

        if not skip_purge:
            results["queue_purge"] = self.purge_queue()

        if not skip_stop:
            results["executions_stopped"] = self.stop_executions()

        # Always abort queued documents after purge
        if not skip_purge:
            results["documents_aborted"] = self.abort_queued_documents()

        return results
