# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Example usage of the DynamoDB module for document tracking.

This example demonstrates how to use the DocumentDynamoDBService
to create, update, and retrieve documents directly from DynamoDB.
"""

import os
from datetime import datetime

from idp_common.dynamodb import DocumentDynamoDBService
from idp_common.models import Document, Page, Section, Status


def example_basic_usage():
    """
    Basic example of using DocumentDynamoDBService.
    """
    print("=== Basic DynamoDB Service Usage ===")

    # Initialize service (requires TRACKING_TABLE and AWS_REGION env vars)
    service = DocumentDynamoDBService()

    # Create a new document
    document = Document(
        input_key="example-document.pdf",
        status=Status.QUEUED,
        queued_time=datetime.now().isoformat() + "Z",
        initial_event_time=datetime.now().isoformat() + "Z",
    )

    print(f"Creating document: {document.input_key}")

    # Create document in DynamoDB
    object_key = service.create_document(document)
    print(f"Document created with key: {object_key}")

    # Update document status
    document.status = Status.PROCESSING
    document.start_time = datetime.now().isoformat() + "Z"

    print("Updating document status to PROCESSING")
    updated_doc = service.update_document(document)
    print(f"Document updated. Status: {updated_doc.status}")

    # Retrieve document
    print("Retrieving document from DynamoDB")
    retrieved_doc = service.get_document(object_key)
    if retrieved_doc:
        print(
            f"Retrieved document: {retrieved_doc.input_key}, Status: {retrieved_doc.status}"
        )
    else:
        print("Document not found")


def example_with_pages_and_sections():
    """
    Example with pages and sections data.
    """
    print("\n=== Document with Pages and Sections ===")

    service = DocumentDynamoDBService()

    # Create document with pages and sections
    document = Document(
        input_key="complex-document.pdf",
        status=Status.PROCESSING,
        queued_time=datetime.now().isoformat() + "Z",
        initial_event_time=datetime.now().isoformat() + "Z",
        num_pages=3,
    )

    # Add pages
    for i in range(1, 4):
        page = Page(
            page_id=str(i),
            image_uri=f"s3://bucket/images/page-{i}.png",
            raw_text_uri=f"s3://bucket/text/page-{i}.txt",
            classification=f"page-type-{i}",
        )
        document.pages[str(i)] = page

    # Add sections
    section = Section(
        section_id="section-1",
        classification="form",
        page_ids=["1", "2"],
        extraction_result_uri="s3://bucket/results/section-1.json",
        confidence_threshold_alerts=[
            {
                "attribute_name": "customer_name",
                "confidence": 0.75,
                "confidence_threshold": 0.8,
            }
        ],
    )
    document.sections.append(section)

    print(f"Creating complex document: {document.input_key}")

    # Update with completion
    document.status = Status.COMPLETED
    document.completion_time = datetime.now().isoformat() + "Z"
    document.metering = {
        "textract_pages": 3,
        "bedrock_input_tokens": 1500,
        "bedrock_output_tokens": 300,
    }

    print("Updating document with completion data")
    updated_doc = service.update_document(document)
    print(
        f"Document completed. Pages: {len(updated_doc.pages)}, Sections: {len(updated_doc.sections)}"
    )


def example_factory_usage():
    """
    Example of using the DocumentServiceFactory.
    """
    print("\n=== Using DocumentServiceFactory ===")

    from idp_common.docs_service import DocumentServiceFactory, create_document_service

    # Create service using factory (defaults to environment variable)
    service = DocumentServiceFactory.create_service()
    print(f"Created service using factory. Type: {type(service).__name__}")

    # Create service with explicit mode
    try:
        dynamodb_service = DocumentServiceFactory.create_service(mode="dynamodb")
        print(f"Created DynamoDB service: {type(dynamodb_service).__name__}")
    except Exception as e:
        print(f"Could not create DynamoDB service: {e}")

    # Use convenience function
    service = create_document_service()
    print(f"Created service using convenience function: {type(service).__name__}")

    # Check current mode
    current_mode = DocumentServiceFactory.get_current_mode()
    print(f"Current tracking mode: {current_mode}")


def example_environment_switching():
    """
    Example of switching between modes using environment variables.
    """
    print("\n=== Environment-based Mode Switching ===")

    from idp_common.docs_service import (
        create_document_service,
        get_document_tracking_mode,
    )

    # Show current mode
    current_mode = get_document_tracking_mode()
    print(f"Current mode from environment: {current_mode}")

    # Temporarily set environment variable
    original_mode = os.environ.get("DOCUMENT_TRACKING_MODE")

    # Test DynamoDB mode
    os.environ["DOCUMENT_TRACKING_MODE"] = "dynamodb"
    service = create_document_service()
    print(f"With DOCUMENT_TRACKING_MODE=dynamodb: {type(service).__name__}")

    # Restore original environment
    if original_mode:
        os.environ["DOCUMENT_TRACKING_MODE"] = original_mode
    else:
        os.environ.pop("DOCUMENT_TRACKING_MODE", None)


if __name__ == "__main__":
    print("DynamoDB Module Example")
    print("=" * 50)

    # Note: These examples require proper AWS credentials and environment variables
    print("Note: These examples require:")
    print("- AWS credentials configured")
    print("- TRACKING_TABLE environment variable set")
    print("- AWS_REGION environment variable set")
    print()

    try:
        # Run examples (will fail without proper environment setup)
        example_factory_usage()
        example_environment_switching()

        # These require actual AWS resources
        # example_basic_usage()
        # example_with_pages_and_sections()

    except Exception as e:
        print(f"Example failed (expected without proper AWS setup): {e}")
        print("\nTo run the full examples, ensure you have:")
        print("1. AWS credentials configured (aws configure or IAM role)")
        print("2. TRACKING_TABLE environment variable set to your DynamoDB table name")
        print("3. AWS_REGION environment variable set to your AWS region")
