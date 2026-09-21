#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Nova Provisioned Throughput Creation Script

This script creates and manages provisioned throughput for fine-tuned Nova models. It can:
1. Create provisioned throughput for completed fine-tuning jobs
2. Monitor provisioning status
3. Delete provisioned throughput to avoid costs
4. Load job details from previous fine-tuning runs

Prerequisites:
- Set up AWS CLI: `aws configure`
- Install required packages: `pip install boto3 python-dotenv`
- Have a completed fine-tuning job
- Appropriate AWS permissions for Bedrock

Example usage:
    python create_provisioned_throughput.py \
        --model-id arn:aws:bedrock:us-east-1:123456789012:custom-model/amazon.nova-lite-v1... \
        --provisioned-model-name my-provisioned-model \
        --model-units 1

    python create_provisioned_throughput.py \
        --job-details-file finetuning_job_20241201_120000.json \
        --provisioned-model-name my-provisioned-model \
        --model-units 2

    python create_provisioned_throughput.py \
        --delete \
        --provisioned-model-arn arn:aws:bedrock:us-east-1:123456789012:provisioned-model/...
"""

import argparse
import json
import logging
from datetime import datetime
from typing import Dict, Optional

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

from idp_common.model_finetuning import (
    ModelFinetuningService,
    ProvisionedThroughputConfig,
    ProvisionedThroughputResult,
)

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class ProvisionedThroughputManager:
    """Manages Nova provisioned throughput."""

    def __init__(self, region: str = "us-east-1"):
        """Initialize provisioned throughput manager."""
        self.region = region
        self.service = ModelFinetuningService(region=region)
        self.bedrock_client = boto3.client("bedrock", region_name=region)

    def create_provisioned_throughput(
        self, config: ProvisionedThroughputConfig
    ) -> ProvisionedThroughputResult:
        """Create provisioned throughput."""
        logger.info(f"Creating provisioned throughput: {config.provisioned_model_name}")
        logger.info(f"Model ID: {config.model_id}")
        logger.info(f"Model units: {config.model_units}")

        result = self.service.create_provisioned_throughput(config)
        logger.info(
            f"Provisioned throughput created successfully: {result.provisioned_model_arn}"
        )

        return result

    def monitor_provisioning(
        self,
        provisioned_model_arn: str,
        polling_interval: int = 30,
        max_wait_time: Optional[int] = None,
    ) -> ProvisionedThroughputResult:
        """
        Monitor provisioning progress.

        Args:
            provisioned_model_arn: Provisioned model ARN to monitor
            polling_interval: Time between status checks (seconds)
            max_wait_time: Maximum time to wait (seconds)

        Returns:
            Final provisioning result
        """
        logger.info(f"Monitoring provisioning: {provisioned_model_arn}")

        result = self.service.wait_for_provisioning_completion(
            provisioned_model_arn,
            model_type="nova",
            polling_interval=polling_interval,
            max_wait_time=max_wait_time,
        )

        logger.info(f"Provisioning completed with status: {result.status}")
        return result

    def get_provisioning_status(
        self, provisioned_model_arn: str
    ) -> ProvisionedThroughputResult:
        """Get current provisioning status."""
        return self.service.get_provisioned_throughput_status(
            provisioned_model_arn, model_type="nova"
        )

    def delete_provisioned_throughput(self, provisioned_model_arn: str) -> Dict:
        """Delete provisioned throughput."""
        logger.info(f"Deleting provisioned throughput: {provisioned_model_arn}")

        response = self.service.delete_provisioned_throughput(
            provisioned_model_arn, model_type="nova"
        )
        logger.info("Provisioned throughput deletion initiated successfully")

        return response

    def list_provisioned_models(self) -> list:
        """List all provisioned models."""
        try:
            response = self.bedrock_client.list_provisioned_model_throughputs()
            return response.get("provisionedModelSummaries", [])
        except ClientError as e:
            logger.error(f"Error listing provisioned models: {e}")
            return []

    def save_provisioning_details(
        self, result: ProvisionedThroughputResult, output_file: str = None
    ):
        """Save provisioning details to JSON file."""
        if output_file is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = f"provisioned_throughput_{timestamp}.json"

        provisioning_data = {
            "provisioned_model_arn": result.provisioned_model_arn,
            "provisioned_model_id": result.provisioned_model_id,
            "status": result.status,
            "creation_time": result.creation_time,
            "failure_reason": result.failure_reason,
            "model_type": result.model_type,
        }

        with open(output_file, "w") as f:
            json.dump(provisioning_data, f, indent=2)

        logger.info(f"Provisioning details saved to: {output_file}")
        return output_file


def load_job_details(job_details_file: str) -> Dict:
    """Load job details from JSON file."""
    try:
        with open(job_details_file, "r") as f:
            job_data = json.load(f)
        logger.info(f"Loaded job details from: {job_details_file}")
        return job_data
    except FileNotFoundError:
        logger.error(f"Job details file not found: {job_details_file}")
        raise
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing job details file: {e}")
        raise


def get_model_id_from_job_arn(job_arn: str, region: str = "us-east-1") -> str:
    """Get model ID from completed fine-tuning job ARN."""
    bedrock_client = boto3.client("bedrock", region_name=region)

    try:
        response = bedrock_client.get_model_customization_job(jobIdentifier=job_arn)
        model_id = response.get("outputModelArn")

        if not model_id:
            raise ValueError(f"No output model found for job: {job_arn}")

        logger.info(f"Retrieved model ID from job: {model_id}")
        return model_id

    except ClientError as e:
        logger.error(f"Error retrieving job details: {e}")
        raise


def main():
    """Main function to create and manage provisioned throughput."""
    parser = argparse.ArgumentParser(
        description="Create and manage Nova provisioned throughput",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create provisioned throughput with model ID
  python create_provisioned_throughput.py \\
    --model-id arn:aws:bedrock:us-east-1:123456789012:custom-model/amazon.nova-lite-v1... \\
    --provisioned-model-name my-provisioned-model \\
    --model-units 1
  
  # Create provisioned throughput from job details file
  python create_provisioned_throughput.py \\
    --job-details-file finetuning_job_20241201_120000.json \\
    --provisioned-model-name my-provisioned-model \\
    --model-units 2
  
  # Create provisioned throughput from job ARN
  python create_provisioned_throughput.py \\
    --job-arn arn:aws:states:us-east-1:123456789012:execution:... \\
    --provisioned-model-name my-provisioned-model
  
  # Check status of provisioned throughput
  python create_provisioned_throughput.py \\
    --status-only \\
    --provisioned-model-arn arn:aws:bedrock:us-east-1:123456789012:provisioned-model/...
  
  # Delete provisioned throughput
  python create_provisioned_throughput.py \\
    --delete \\
    --provisioned-model-arn arn:aws:bedrock:us-east-1:123456789012:provisioned-model/...
  
  # List all provisioned models
  python create_provisioned_throughput.py --list-models
        """,  # arn-partition-ok: argparse epilog — commercial example ARNs in help text
    )

    # Mutually exclusive group for model identification
    model_group = parser.add_mutually_exclusive_group()
    model_group.add_argument("--model-id", help="Fine-tuned model ID/ARN")
    model_group.add_argument(
        "--job-details-file", help="JSON file with fine-tuning job details"
    )
    model_group.add_argument("--job-arn", help="Fine-tuning job ARN")

    # Required for creation (unless using other modes)
    parser.add_argument(
        "--provisioned-model-name", help="Name for the provisioned model"
    )
    parser.add_argument(
        "--model-units", type=int, default=1, help="Number of model units (default: 1)"
    )

    # Optional arguments
    parser.add_argument(
        "--region", default="us-east-1", help="AWS region (default: us-east-1)"
    )

    # Monitoring options
    parser.add_argument(
        "--no-wait", action="store_true", help="Don't wait for provisioning completion"
    )
    parser.add_argument(
        "--polling-interval",
        type=int,
        default=30,
        help="Status check interval in seconds (default: 30)",
    )
    parser.add_argument(
        "--max-wait-time",
        type=int,
        default=1800,
        help="Maximum time to wait in seconds (default: 1800)",
    )

    # Output options
    parser.add_argument(
        "--output-file",
        help="File to save provisioning details (default: auto-generated)",
    )

    # Action modes
    parser.add_argument(
        "--status-only",
        action="store_true",
        help="Only check status of existing provisioned throughput",
    )
    parser.add_argument(
        "--delete", action="store_true", help="Delete existing provisioned throughput"
    )
    parser.add_argument(
        "--list-models", action="store_true", help="List all provisioned models"
    )
    parser.add_argument(
        "--provisioned-model-arn",
        help="Provisioned model ARN for status/delete operations",
    )

    args = parser.parse_args()

    # Validate arguments
    if args.status_only or args.delete:
        if not args.provisioned_model_arn:
            parser.error("--status-only and --delete require --provisioned-model-arn")

    if args.list_models:
        # List models mode - no other arguments needed
        pass
    elif args.status_only or args.delete:
        # Status/delete modes - no model ID needed
        pass
    elif not (args.model_id or args.job_details_file or args.job_arn):
        parser.error(
            "Must specify --model-id, --job-details-file, or --job-arn for creation"
        )

    if (
        not (args.status_only or args.delete or args.list_models)
        and not args.provisioned_model_name
    ):
        parser.error("--provisioned-model-name is required for creation")

    try:
        # Initialize manager
        manager = ProvisionedThroughputManager(args.region)

        # List models mode
        if args.list_models:
            logger.info("Listing all provisioned models...")
            models = manager.list_provisioned_models()

            if not models:
                print("No provisioned models found.")
                return 0

            print(f"\nFound {len(models)} provisioned model(s):")
            print("-" * 80)
            for model in models:
                print(f"Name: {model.get('provisionedModelName', 'N/A')}")
                print(f"ARN: {model.get('provisionedModelArn', 'N/A')}")
                print(f"Status: {model.get('status', 'N/A')}")
                print(f"Model Units: {model.get('modelUnits', 'N/A')}")
                print(f"Foundation Model: {model.get('foundationModelArn', 'N/A')}")
                print(f"Creation Time: {model.get('creationTime', 'N/A')}")
                print("-" * 80)

            return 0

        # Status-only mode
        if args.status_only:
            logger.info(
                f"Checking status for provisioned throughput: {args.provisioned_model_arn}"
            )
            result = manager.get_provisioning_status(args.provisioned_model_arn)

            print(f"Provisioning Status: {result.status}")
            print(f"Provisioned Model ARN: {result.provisioned_model_arn}")
            print(f"Creation Time: {result.creation_time}")
            if result.failure_reason:
                print(f"Failure Reason: {result.failure_reason}")

            return 0

        # Delete mode
        if args.delete:
            logger.info(
                f"Deleting provisioned throughput: {args.provisioned_model_arn}"
            )

            # Confirm deletion
            response = input(
                f"Are you sure you want to delete provisioned throughput '{args.provisioned_model_arn}'? (yes/no): "
            )
            if response.lower() not in ["yes", "y"]:
                logger.info("Deletion cancelled.")
                return 0

            manager.delete_provisioned_throughput(args.provisioned_model_arn)
            logger.info("✅ Provisioned throughput deletion initiated successfully!")
            logger.info("Note: It may take a few minutes for the deletion to complete.")

            return 0

        # Creation mode - determine model ID
        model_id = None

        if args.model_id:
            model_id = args.model_id
        elif args.job_details_file:
            job_data = load_job_details(args.job_details_file)

            if job_data.get("model_id"):
                model_id = job_data["model_id"]
            elif job_data.get("job_arn"):
                model_id = get_model_id_from_job_arn(job_data["job_arn"], args.region)
            else:
                logger.error("Job details file does not contain model_id or job_arn")
                return 1
        elif args.job_arn:
            model_id = get_model_id_from_job_arn(args.job_arn, args.region)

        if not model_id:
            logger.error("Could not determine model ID")
            return 1

        # Create provisioned throughput configuration
        config = ProvisionedThroughputConfig(
            model_id=model_id,
            provisioned_model_name=args.provisioned_model_name,
            model_units=args.model_units,
            model_type="nova",
        )

        # Create provisioned throughput
        result = manager.create_provisioned_throughput(config)

        # Save provisioning details
        output_file = manager.save_provisioning_details(result, args.output_file)

        # Monitor provisioning if requested
        if not args.no_wait:
            logger.info("Monitoring provisioning progress...")
            final_result = manager.monitor_provisioning(
                result.provisioned_model_arn, args.polling_interval, args.max_wait_time
            )

            # Update saved provisioning details
            manager.save_provisioning_details(final_result, output_file)

            if final_result.status == "InService":
                logger.info("✅ Provisioned throughput is ready!")
                logger.info(
                    f"Provisioned Model ARN: {final_result.provisioned_model_arn}"
                )
                logger.info("You can now use this model for inference.")
            elif final_result.status == "Failed":
                logger.error("❌ Provisioning failed!")
                if final_result.failure_reason:
                    logger.error(f"Failure reason: {final_result.failure_reason}")
                return 1
            else:
                logger.info(f"Provisioning finished with status: {final_result.status}")
        else:
            logger.info(
                "Provisioning initiated successfully. Use --status-only to check progress later."
            )

        logger.info(f"Provisioning details saved to: {output_file}")
        logger.info("\n" + "=" * 60)
        logger.info("💡 IMPORTANT: Provisioned throughput incurs costs!")
        logger.info("💡 Use --delete to remove when no longer needed.")
        logger.info("=" * 60)

        return 0

    except Exception as e:
        logger.error(f"Error during provisioned throughput operation: {e}")
        return 1


if __name__ == "__main__":
    exit(main())
