#!/usr/bin/env python3
"""
Nova Fine-tuning Job Creation Script

This script creates and monitors Nova fine-tuning jobs using Amazon Bedrock. It can:
1. Set up IAM roles for Bedrock fine-tuning (optional)
2. Create fine-tuning jobs with custom parameters
3. Monitor job progress and status
4. Support both separate validation data and automatic splitting
5. Save job details for later use

Prerequisites:
- Set up AWS CLI: `aws configure`
- Install required packages: `pip install boto3 python-dotenv`
- Have training data prepared in S3 (use prepare_nova_finetuning_data.py)
- Appropriate AWS permissions for Bedrock and IAM

Example usage:
    python create_finetuning_job.py \
        --training-data-uri s3://my-bucket/data/train.jsonl \
        --validation-data-uri s3://my-bucket/data/validation.jsonl \
        --output-uri s3://my-bucket/output/ \
        --job-name my-finetuning-job \
        --model-name my-finetuned-model

    python create_finetuning_job.py \
        --training-data-uri s3://my-bucket/data/train.jsonl \
        --output-uri s3://my-bucket/output/ \
        --job-name my-auto-split-job \
        --validation-split 0.2 \
        --create-role
"""

import argparse
import json
import logging
import time
from datetime import datetime
from typing import Dict, Optional

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

from idp_common.model_finetuning import (
    FinetuningJobConfig,
    FinetuningJobResult,
    JobStatus,
    ModelFinetuningService,
)

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class IAMRoleManager:
    """Manages IAM roles for Bedrock fine-tuning."""

    def __init__(self, region: str = "us-east-1"):
        """Initialize IAM role manager."""
        self.region = region
        self.iam_client = boto3.client("iam", region_name=region)

    def create_or_update_model_customization_role(
        self, role_name_base: str = "IDPModelCustomizationRole"
    ) -> str:
        """
        Creates or updates an IAM role with permissions to access S3 buckets
        for use with Amazon Bedrock fine-tuning.

        Args:
            role_name_base: The base name for the IAM role

        Returns:
            The ARN of the IAM role
        """
        # Add region suffix to role name for regional isolation
        region_suffix = self.region.replace("-", "")
        role_name = f"{role_name_base}{region_suffix}"

        # Partition for the ARNs built below, derived from the caller identity so
        # they resolve outside the commercial partition. Falls back to "aws" if
        # STS is unreachable rather than failing role creation outright.
        try:
            caller_arn = (
                boto3.client("sts", region_name=self.region)
                .get_caller_identity()
                .get("Arn")
            )
            partition = (
                (caller_arn or "arn:aws:").split(":")[1] or "aws"
            )  # arn-partition-ok: fallback used only to PARSE the partition out
        except Exception:  # noqa: BLE001
            partition = "aws"

        # Define the trust policy - allows Bedrock service to assume this role
        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "bedrock.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }

        # Define the S3 access policy with access to any bucket
        s3_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
                    # Partition-aware: a hardcoded arn:aws: S3 resource
                    # matches nothing in GovCloud/China.
                    "Resource": [
                        f"arn:{partition}:s3:::*",
                        f"arn:{partition}:s3:::*/*",
                    ],
                }
            ],
        }

        try:
            # Check if the role already exists
            try:
                role = self.iam_client.get_role(RoleName=role_name)
                logger.info(f"Role {role_name} already exists")

                # Update the policy
                policy_name = f"{role_name}S3AccessPolicy"

                # Check if policy exists and get its ARN
                try:
                    policies = self.iam_client.list_attached_role_policies(
                        RoleName=role_name
                    )
                    policy_exists = False
                    policy_arn = None

                    for policy in policies["AttachedPolicies"]:
                        if policy["PolicyName"] == policy_name:
                            policy_arn = policy["PolicyArn"]
                            policy_exists = True
                            break

                    if policy_exists:
                        # Detach and delete the existing policy
                        self.iam_client.detach_role_policy(
                            RoleName=role_name, PolicyArn=policy_arn
                        )

                        # AWS requires a delay when dealing with IAM
                        time.sleep(2)

                        self.iam_client.delete_policy(PolicyArn=policy_arn)
                        logger.info(f"Deleted existing policy: {policy_name}")

                except ClientError as e:
                    logger.warning(f"Error checking policies: {e}")

                # Create a new policy
                policy_response = self.iam_client.create_policy(
                    PolicyName=policy_name,
                    PolicyDocument=json.dumps(s3_policy),
                    Description="Policy for S3 access for Bedrock fine-tuning (any bucket)",
                )
                policy_arn = policy_response["Policy"]["Arn"]

                # Attach the policy to the role
                self.iam_client.attach_role_policy(
                    RoleName=role_name, PolicyArn=policy_arn
                )
                logger.info(f"Updated role {role_name} with new S3 access policy")

                # Return full role ARN
                return role["Role"]["Arn"]

            except ClientError as e:
                # Role doesn't exist, create it
                if e.response["Error"]["Code"] == "NoSuchEntity":
                    logger.info(f"Role {role_name} doesn't exist. Creating...")

                    # Set the path for service roles
                    path = "/service-role/"

                    # Create the role with trust policy
                    response = self.iam_client.create_role(
                        Path=path,
                        RoleName=role_name,
                        AssumeRolePolicyDocument=json.dumps(trust_policy),
                        Description="Role for Amazon Bedrock fine-tuning with S3 access",
                    )
                    role_arn = response["Role"]["Arn"]

                    # Create a policy for S3 access
                    policy_name = f"{role_name}S3AccessPolicy"
                    policy_response = self.iam_client.create_policy(
                        PolicyName=policy_name,
                        PolicyDocument=json.dumps(s3_policy),
                        Description="Policy for S3 access for Bedrock fine-tuning (any bucket)",
                    )
                    policy_arn = policy_response["Policy"]["Arn"]

                    # Attach the policy to the role
                    self.iam_client.attach_role_policy(
                        RoleName=role_name, PolicyArn=policy_arn
                    )

                    logger.info(f"Created role {role_name} with S3 access policy")
                    logger.info("Allow some time for the role to propagate in AWS")

                    return role_arn
                else:
                    raise

        except ClientError as e:
            logger.error(f"Error creating/updating role: {e}")
            raise


class FinetuningJobManager:
    """Manages Nova fine-tuning jobs."""

    def __init__(self, region: str = "us-east-1"):
        """Initialize fine-tuning job manager."""
        self.region = region
        self.service = ModelFinetuningService(region=region)

    def create_job(self, config: FinetuningJobConfig) -> FinetuningJobResult:
        """Create a fine-tuning job."""
        logger.info(f"Creating fine-tuning job: {config.job_name}")
        logger.info(f"Base model: {config.base_model}")
        logger.info(f"Training data: {config.training_data_uri}")

        if config.validation_data_uri:
            logger.info(f"Validation data: {config.validation_data_uri}")
        else:
            logger.info(
                f"Using automatic data splitting with ratio: {config.validation_split}"
            )

        result = self.service.create_finetuning_job(config)
        logger.info(f"Fine-tuning job created successfully: {result.job_arn}")

        return result

    def monitor_job(
        self,
        job_arn: str,
        polling_interval: int = 60,
        max_wait_time: Optional[int] = None,
    ) -> FinetuningJobResult:
        """
        Monitor fine-tuning job progress.

        Args:
            job_arn: Job ARN to monitor
            polling_interval: Time between status checks (seconds)
            max_wait_time: Maximum time to wait (seconds)

        Returns:
            Final job result
        """
        logger.info(f"Monitoring fine-tuning job: {job_arn}")

        result = self.service.wait_for_job_completion(
            job_arn,
            model_type="nova",
            polling_interval=polling_interval,
            max_wait_time=max_wait_time,
        )

        logger.info(f"Job completed with status: {result.status.value}")
        if result.model_id:
            logger.info(f"Fine-tuned model ID: {result.model_id}")

        return result

    def get_job_status(self, job_arn: str) -> FinetuningJobResult:
        """Get current job status."""
        return self.service.get_job_status(job_arn, model_type="nova")

    def save_job_details(
        self, job_result: FinetuningJobResult, output_file: str = None
    ):
        """Save job details to JSON file."""
        if output_file is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = f"finetuning_job_{timestamp}.json"

        job_data = {
            "job_arn": job_result.job_arn,
            "job_name": job_result.job_name,
            "status": job_result.status.value,
            "model_id": job_result.model_id,
            "creation_time": job_result.creation_time,
            "end_time": job_result.end_time,
            "failure_reason": job_result.failure_reason,
            "model_type": job_result.model_type,
        }

        with open(output_file, "w") as f:
            json.dump(job_data, f, indent=2)

        logger.info(f"Job details saved to: {output_file}")
        return output_file


def validate_hyperparameters(hyperparameters: Dict[str, str]) -> None:
    """Validate Nova hyperparameters."""
    if "epochCount" in hyperparameters:
        epoch_count = int(hyperparameters["epochCount"])
        if epoch_count < 1 or epoch_count > 5:
            raise ValueError("epochCount must be between 1 and 5")

    if "learningRate" in hyperparameters:
        learning_rate = float(hyperparameters["learningRate"])
        if learning_rate < 1e-6 or learning_rate > 1e-4:
            raise ValueError("learningRate must be between 1e-6 and 1e-4")

    if "batchSize" in hyperparameters:
        batch_size = int(hyperparameters["batchSize"])
        if batch_size < 1:
            raise ValueError("batchSize must be >= 1")


def main():
    """Main function to create and monitor fine-tuning jobs."""
    parser = argparse.ArgumentParser(
        description="Create and monitor Nova fine-tuning jobs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create job with separate validation data
  python create_finetuning_job.py \\
    --training-data-uri s3://my-bucket/data/train.jsonl \\
    --validation-data-uri s3://my-bucket/data/validation.jsonl \\
    --output-uri s3://my-bucket/output/ \\
    --job-name my-finetuning-job
  
  # Create job with automatic data splitting
  python create_finetuning_job.py \\
    --training-data-uri s3://my-bucket/data/train.jsonl \\
    --output-uri s3://my-bucket/output/ \\
    --job-name my-auto-split-job \\
    --validation-split 0.2
  
  # Create IAM role and job with custom hyperparameters
  python create_finetuning_job.py \\
    --training-data-uri s3://my-bucket/data/train.jsonl \\
    --output-uri s3://my-bucket/output/ \\
    --job-name custom-job \\
    --create-role \\
    --epoch-count 3 \\
    --learning-rate 0.0001 \\
    --batch-size 1
        """,
    )

    # Required arguments
    parser.add_argument(
        "--training-data-uri", required=True, help="S3 URI for training data JSONL file"
    )
    parser.add_argument(
        "--job-name", required=True, help="Name for the fine-tuning job"
    )

    # Optional arguments
    parser.add_argument(
        "--validation-data-uri", help="S3 URI for validation data JSONL file"
    )
    parser.add_argument("--output-uri", help="S3 URI for output directory")
    parser.add_argument("--model-name", help="Name for the fine-tuned model")
    parser.add_argument(
        "--base-model",
        default="arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0:300k",  # arn-partition-ok: commercial-only CLI default; override --base-model elsewhere
        help="Base model ARN (default: Nova Lite)",
    )
    parser.add_argument(
        "--validation-split",
        type=float,
        default=0.2,
        help="Validation split ratio when not using separate validation data (default: 0.2)",
    )
    parser.add_argument(
        "--region", default="us-east-1", help="AWS region (default: us-east-1)"
    )

    # IAM role options
    parser.add_argument("--role-arn", help="IAM role ARN for Bedrock fine-tuning")
    parser.add_argument(
        "--create-role", action="store_true", help="Create IAM role automatically"
    )
    parser.add_argument(
        "--role-name-base",
        default="IDPModelCustomizationRole",
        help="Base name for IAM role (default: IDPModelCustomizationRole)",
    )

    # Hyperparameters
    parser.add_argument(
        "--epoch-count",
        type=int,
        default=2,
        help="Number of training epochs (1-5, default: 2)",
    )
    parser.add_argument(
        "--learning-rate",
        type=float,
        default=0.00001,
        help="Learning rate (1e-6 to 1e-4, default: 0.00001)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=1, help="Batch size (default: 1)"
    )

    # Monitoring options
    parser.add_argument(
        "--no-wait", action="store_true", help="Don't wait for job completion"
    )
    parser.add_argument(
        "--polling-interval",
        type=int,
        default=60,
        help="Status check interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--max-wait-time", type=int, help="Maximum time to wait in seconds"
    )

    # Output options
    parser.add_argument(
        "--output-file", help="File to save job details (default: auto-generated)"
    )
    parser.add_argument(
        "--status-only",
        action="store_true",
        help="Only check status of existing job (requires --job-arn)",
    )
    parser.add_argument("--job-arn", help="Job ARN for status checking")

    args = parser.parse_args()

    # Validate arguments
    if args.status_only and not args.job_arn:
        parser.error("--status-only requires --job-arn")

    if not args.role_arn and not args.create_role:
        parser.error("Either --role-arn or --create-role must be specified")

    # Validate hyperparameters
    hyperparameters = {
        "epochCount": str(args.epoch_count),
        "learningRate": str(args.learning_rate),
        "batchSize": str(args.batch_size),
    }

    try:
        validate_hyperparameters(hyperparameters)
    except ValueError as e:
        parser.error(f"Invalid hyperparameters: {e}")

    try:
        # Initialize managers
        job_manager = FinetuningJobManager(args.region)

        # Status-only mode
        if args.status_only:
            logger.info(f"Checking status for job: {args.job_arn}")
            result = job_manager.get_job_status(args.job_arn)

            print(f"Job Status: {result.status.value}")
            if result.model_id:
                print(f"Model ID: {result.model_id}")
            if result.failure_reason:
                print(f"Failure Reason: {result.failure_reason}")

            return 0

        # Create or get IAM role ARN
        role_arn = args.role_arn
        if args.create_role:
            logger.info("Creating IAM role for Bedrock fine-tuning...")
            role_manager = IAMRoleManager(args.region)
            role_arn = role_manager.create_or_update_model_customization_role(
                args.role_name_base
            )
            logger.info(f"Using role ARN: {role_arn}")

        # Create job configuration
        job_config = FinetuningJobConfig(
            base_model=args.base_model,
            training_data_uri=args.training_data_uri,
            validation_data_uri=args.validation_data_uri,
            output_uri=args.output_uri,
            role_arn=role_arn,
            job_name=args.job_name,
            model_name=args.model_name,
            hyperparameters=hyperparameters,
            validation_split=args.validation_split,
            model_type="nova",
        )

        # Create fine-tuning job
        job_result = job_manager.create_job(job_config)

        # Save job details
        output_file = job_manager.save_job_details(job_result, args.output_file)

        # Monitor job if requested
        if not args.no_wait:
            logger.info("Monitoring job progress...")
            final_result = job_manager.monitor_job(
                job_result.job_arn, args.polling_interval, args.max_wait_time
            )

            # Update saved job details
            job_manager.save_job_details(final_result, output_file)

            if final_result.status == JobStatus.COMPLETED:
                logger.info("✅ Fine-tuning job completed successfully!")
                logger.info(f"Fine-tuned model ID: {final_result.model_id}")
            elif final_result.status == JobStatus.FAILED:
                logger.error("❌ Fine-tuning job failed!")
                if final_result.failure_reason:
                    logger.error(f"Failure reason: {final_result.failure_reason}")
                return 1
            else:
                logger.info(f"Job finished with status: {final_result.status.value}")
        else:
            logger.info(
                "Job created successfully. Use --status-only to check progress later."
            )

        logger.info(f"Job details saved to: {output_file}")
        return 0

    except Exception as e:
        logger.error(f"Error during fine-tuning job creation: {e}")
        return 1


if __name__ == "__main__":
    exit(main())
