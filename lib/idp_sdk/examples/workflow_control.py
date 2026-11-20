#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
IDP SDK Example: Workflow Control Operations

Demonstrates stopping workflows, rerunning documents, and batch management.
"""

import argparse
import sys
from pathlib import Path

# Add parent to path for development
sys.path.insert(0, str(Path(__file__).parent.parent))

from idp_sdk import IDPClient, RerunStep


def main():
    parser = argparse.ArgumentParser(description="IDP SDK Workflow Control Example")
    parser.add_argument(
        "--stack-name",
        type=str,
        required=True,
        help="CloudFormation stack name",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # List batches subcommand
    list_parser = subparsers.add_parser("list", help="List recent batches")
    list_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of batches to show",
    )

    # Status subcommand
    status_parser = subparsers.add_parser("status", help="Get batch/document status")
    status_parser.add_argument(
        "--batch-id",
        type=str,
        help="Batch ID to check",
    )
    status_parser.add_argument(
        "--document-id",
        type=str,
        help="Document ID to check",
    )

    # Rerun subcommand
    rerun_parser = subparsers.add_parser("rerun", help="Rerun documents from a step")
    rerun_parser.add_argument(
        "--batch-id",
        type=str,
        help="Batch ID to rerun",
    )
    rerun_parser.add_argument(
        "--document-ids",
        type=str,
        nargs="+",
        help="Document IDs to rerun",
    )
    rerun_parser.add_argument(
        "--step",
        type=str,
        choices=["classification", "extraction"],
        default="extraction",
        help="Step to rerun from",
    )

    # Stop subcommand
    stop_parser = subparsers.add_parser("stop", help="Stop all running workflows")
    stop_parser.add_argument(
        "--skip-purge",
        action="store_true",
        help="Don't purge the SQS queue",
    )
    stop_parser.add_argument(
        "--skip-stop",
        action="store_true",
        help="Don't stop Step Function executions",
    )

    # Resources subcommand
    subparsers.add_parser("resources", help="Show stack resources")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Create client with stack
    client = IDPClient(stack_name=args.stack_name)

    if args.command == "list":
        print(f"Listing recent batches from: {args.stack_name}")
        print(f"  Limit: {args.limit}")

        batches = client.batch.list(limit=args.limit)

        print(f"\nFound {batches.count} batches:")
        for batch in batches.batches:
            print(f"\n  Batch: {batch.batch_id}")
            print(f"    Documents: {len(batch.document_ids)}")
            print(f"    Queued: {batch.queued}")
            print(f"    Failed: {batch.failed}")
            print(f"    Timestamp: {batch.timestamp}")

        return 0

    elif args.command == "status":
        if not args.batch_id and not args.document_id:
            print("Error: Must specify --batch-id or --document-id")
            return 1

        if args.batch_id:
            print(f"Getting status for batch: {args.batch_id}")
            status = client.batch.get_status(batch_id=args.batch_id)
        else:
            print(f"Getting status for document: {args.document_id}")
            status = client.document.get_status(document_id=args.document_id)
            # Convert single document status to batch-like format for display
            from idp_sdk import BatchStatus, DocumentBucket, classify_document_state

            # Count the document under the same progress bucket `batch.get_status`
            # uses. A document mid-pipeline carries its current step (OCR,
            # EXTRACTING, ...) as its status, never "PROCESSING", so comparing the
            # status with a hand-picked list of names misses most of them.
            bucket = classify_document_state(status.status)
            status = BatchStatus(
                batch_id="single-doc",
                documents=[status],
                total=1,
                completed=int(bucket == DocumentBucket.COMPLETED),
                failed=int(bucket == DocumentBucket.FAILED),
                in_progress=int(bucket == DocumentBucket.RUNNING),
                queued=int(bucket == DocumentBucket.QUEUED),
                awaiting_review=int(bucket == DocumentBucket.AWAITING_REVIEW),
                success_rate=1.0 if bucket == DocumentBucket.COMPLETED else 0.0,
                all_complete=bucket
                in (
                    DocumentBucket.COMPLETED,
                    DocumentBucket.FAILED,
                    DocumentBucket.AWAITING_REVIEW,
                ),
            )

        print("\nBatch Status:")
        print(f"  Total: {status.total}")
        print(f"  Completed: {status.completed}")
        print(f"  Failed: {status.failed}")
        print(f"  In Progress: {status.in_progress}")
        print(f"  Queued: {status.queued}")
        print(f"  Success Rate: {status.success_rate:.1%}")
        print(f"  All Complete: {status.all_complete}")

        if status.documents:
            print(f"\nDocuments ({len(status.documents)}):")
            for doc in status.documents[:10]:
                print(f"  - {doc.document_id}")
                print(f"      Status: {doc.status.value}")
                if doc.duration_seconds:
                    print(f"      Duration: {doc.duration_seconds:.1f}s")
                if doc.error:
                    print(f"      Error: {doc.error}")

            if len(status.documents) > 10:
                print(f"  ... ({len(status.documents) - 10} more)")

        return 0

    elif args.command == "rerun":
        if not args.batch_id and not args.document_ids:
            print("Error: Must specify --batch-id or --document-ids")
            return 1

        print(f"Rerunning documents from step: {args.step}")
        if args.batch_id:
            print(f"  Batch: {args.batch_id}")
        else:
            print(f"  Documents: {len(args.document_ids)}")

        result = client.batch.reprocess(
            step=RerunStep(args.step),
            batch_id=args.batch_id,
            document_ids=args.document_ids,
        )

        print("\nRerun Result:")
        print(f"  Documents Queued: {result.documents_queued}")
        print(f"  Documents Failed: {result.documents_failed}")

        if result.failed_documents:
            print("\n  Failed Documents:")
            for doc in result.failed_documents[:5]:
                print(f"    - {doc.get('document_id')}: {doc.get('error')}")

        return 0

    elif args.command == "stop":
        print(f"Stopping workflows for stack: {args.stack_name}")
        print(f"  Skip Purge: {args.skip_purge}")
        print(f"  Skip Stop: {args.skip_stop}")

        result = client.batch.stop_workflows(
            skip_purge=args.skip_purge,
            skip_stop=args.skip_stop,
        )

        print("\nStop Result:")
        print(f"  Queue Purged: {result.queue_purged}")
        if result.executions_stopped:
            print(f"  Executions Stopped: {result.executions_stopped}")
        if result.documents_aborted:
            print(f"  Documents Aborted: {result.documents_aborted}")

        return 0

    elif args.command == "resources":
        print(f"Getting resources for stack: {args.stack_name}")

        resources = client.stack.get_resources()

        print("\nStack Resources:")
        print(f"  Input Bucket: {resources.input_bucket}")
        print(f"  Output Bucket: {resources.output_bucket}")
        if resources.configuration_bucket:
            print(f"  Configuration Bucket: {resources.configuration_bucket}")
        if resources.evaluation_baseline_bucket:
            print(
                f"  Evaluation Baseline Bucket: {resources.evaluation_baseline_bucket}"
            )
        if resources.test_set_bucket:
            print(f"  Test Set Bucket: {resources.test_set_bucket}")
        if resources.document_queue_url:
            print(f"  Document Queue URL: {resources.document_queue_url}")
        if resources.state_machine_arn:
            print(f"  State Machine ARN: {resources.state_machine_arn}")
        if resources.documents_table:
            print(f"  Documents Table: {resources.documents_table}")

        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
