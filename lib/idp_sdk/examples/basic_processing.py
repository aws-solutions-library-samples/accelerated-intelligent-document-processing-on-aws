#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Basic Document Processing Example

This example demonstrates how to use the IDP SDK for basic document processing:
1. Process documents from a local directory
2. Monitor batch progress
3. Download results

Usage:
    python basic_processing.py --stack-name my-idp-stack --directory ./samples/
"""

import argparse
import sys
import time
from pathlib import Path

# Add parent to path for development
sys.path.insert(0, str(Path(__file__).parent.parent))

from idp_sdk import IDPClient


def main():
    parser = argparse.ArgumentParser(description="Basic IDP document processing")
    parser.add_argument(
        "--stack-name", required=True, help="IDP CloudFormation stack name"
    )
    parser.add_argument("--region", default="us-west-2", help="AWS region")
    parser.add_argument(
        "--directory", required=True, help="Directory containing documents"
    )
    parser.add_argument(
        "--output-dir", default="./results", help="Output directory for results"
    )
    parser.add_argument(
        "--number-of-files",
        type=int,
        default=None,
        help="Limit number of files to process",
    )
    args = parser.parse_args()

    # Initialize client with stack
    client = IDPClient(stack_name=args.stack_name, region=args.region)

    print(f"Processing documents from: {args.directory}")

    # Submit batch for processing
    batch_result = client.batch.process(
        source=args.directory,
        batch_prefix="sdk-example",
        file_pattern="*.pdf",
        number_of_files=args.number_of_files,
    )

    print(f"Batch submitted: {batch_result.batch_id}")
    print(f"Documents queued: {batch_result.queued}")
    print(f"Document IDs: {batch_result.document_ids[:5]}...")  # Show first 5

    # Monitor progress
    print("\nMonitoring progress...")
    while True:
        status = client.batch.get_status(batch_id=batch_result.batch_id)

        print(
            f"  Completed: {status.completed}/{status.total} "
            f"(Failed: {status.failed}, In Progress: {status.in_progress})"
        )

        # `all_complete` means nothing is queued or running. A document held for
        # human review counts as settled, so check `awaiting_review` before
        # treating the batch as finished.
        if status.all_complete:
            if status.awaiting_review:
                print(
                    f"\nProcessing finished; {status.awaiting_review} document(s) "
                    "await human review in the web UI's review queues"
                )
            else:
                print(f"\nBatch complete! Success rate: {status.success_rate:.1%}")
            break

        time.sleep(10)  # Poll every 10 seconds

    # Download results
    print(f"\nDownloading results to: {args.output_dir}")
    download_result = client.batch.download_results(
        batch_id=batch_result.batch_id,
        output_dir=args.output_dir,
        file_types=["summary", "sections"],
    )

    print(
        f"Downloaded {download_result.files_downloaded} files "
        f"for {download_result.documents_downloaded} documents"
    )

    # Show any failed documents
    if status.failed > 0:
        print("\nFailed documents:")
        for doc in status.documents:
            if doc.status.value == "FAILED":
                print(f"  - {doc.document_id}: {doc.error}")


if __name__ == "__main__":
    main()
