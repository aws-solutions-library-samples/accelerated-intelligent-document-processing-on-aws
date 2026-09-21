#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Nova Lite Fine-tuning Dataset Preparation Script

This script prepares datasets for fine-tuning Nova Lite models. It can:
1. Load datasets from Hugging Face or local sources
2. Sample data per label 
3. Save images in PNG format to S3
4. Create train.jsonl and validation.jsonl files in Bedrock format
5. Upload all data to S3 buckets

Prerequisites:
- Set up AWS CLI: `aws configure`
- Install required packages: `pip install datasets boto3 pillow tqdm python-dotenv`
- Set environment variables or use command line arguments for S3 configuration

Example usage:
    python prepare_nova_finetuning_data.py \
        --bucket-name my-finetuning-bucket \
        --directory rvl-cdip-sampled \
        --samples-per-label 100 \
        --dataset chainyo/rvl-cdip \
        --split train

    python prepare_nova_finetuning_data.py \
        --bucket-name my-finetuning-bucket \
        --directory custom-data \
        --samples-per-label 50 \
        --local-dataset-path /path/to/local/dataset \
        --validation-split 0.2
"""

import argparse
import concurrent.futures
import json
import logging
import os
import random
import shutil
import uuid
from typing import Dict, List, Optional, Tuple

import boto3
import numpy as np
from datasets import Dataset, load_dataset
from dotenv import load_dotenv
from PIL import Image
from tqdm import tqdm

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Set random seed for reproducibility
random.seed(42)
np.random.seed(42)

# Default label mapping for RVL-CDIP dataset
DEFAULT_LABEL_MAPPING = {
    0: "advertissement",
    1: "budget",
    2: "email",
    3: "file_folder",
    4: "form",
    5: "handwritten",
    6: "invoice",
    7: "letter",
    8: "memo",
    9: "news_article",
    10: "presentation",
    11: "questionnaire",
    12: "resume",
    13: "scientific_publication",
    14: "scientific_report",
    15: "specification",
}

# Default system and task prompts for document classification
DEFAULT_SYSTEM_PROMPT = """You are a document classification expert who can analyze and identify document types from images. Your task is to determine the document type based on its visual appearance, layout, and content, using the provided document type definitions. Your output must be valid JSON according to the requested format."""

DEFAULT_TASK_PROMPT_TEMPLATE = """The <document-types> XML tags contain a markdown table of known document types for detection.

<document-types>
| Document Type | Description |
|---------------|-------------|
| advertissement | Marketing or promotional material with graphics, product information, and calls to action |
| budget | Financial document with numerical data, calculations, and monetary figures organized in tables or lists |
| email | Electronic correspondence with header information, sender/recipient details, and message body |
| file_folder | Document with tabs, labels, or folder-like structure used for organizing other documents |
| form | Structured document with fields to be filled in, checkboxes, or data collection sections |
| handwritten | Document containing primarily handwritten text rather than typed or printed content |
| invoice | Billing document with itemized list of goods/services, costs, payment terms, and company information |
| letter | Formal correspondence with letterhead, date, recipient address, salutation, and signature |
| memo | Internal business communication with brief, direct message and minimal formatting |
| news_article | Journalistic content with headlines, columns, and reporting on events or topics |
| presentation | Slides or visual aids with bullet points, graphics, and concise information for display |
| questionnaire | Document with series of questions designed to collect information from respondents |
| resume | Professional summary of a person's work experience, skills, and qualifications |
| scientific_publication | Academic paper with abstract, methodology, results, and references in formal structure |
| scientific_report | Technical document presenting research findings, data, and analysis in structured format |
| specification | Detailed technical document outlining requirements, standards, or procedures |
</document-types>

CRITICAL: You must ONLY use document types explicitly listed in the <document-types> section. Do not create, invent, or use any document type not found in this list. If a document doesn't clearly match any listed type, assign it to the most similar listed type.

Follow these steps when classifying the document image:
1. Examine the document image carefully, noting its layout, content, and visual characteristics.
2. Identify visual cues that indicate the document type (e.g., tables for budgets, letterhead for letters).
3. Match the document with one of the document types from the provided list ONLY.
4. Before finalizing, verify that your selected document type exactly matches one from the <document-types> list.

Return your response as valid JSON according to this format:
```json
{"type": "document_type_name"}
```
where document_type_name is one of the document types listed in the <document-types> section."""


class NovaDataPreparationService:
    """Service for preparing Nova fine-tuning datasets."""

    def __init__(self, bucket_name: str, region: str = "us-east-1"):
        """
        Initialize the data preparation service.

        Args:
            bucket_name: S3 bucket name for storing data
            region: AWS region
        """
        self.bucket_name = bucket_name
        self.region = region
        self.s3_client = boto3.client("s3", region_name=region)
        self.sts_client = boto3.client("sts", region_name=region)

        # Get AWS account ID
        self.account_id = self.sts_client.get_caller_identity().get("Account")

        logger.info(
            f"Initialized Nova data preparation service for bucket: {bucket_name}"
        )

    def load_dataset(
        self,
        dataset_name: Optional[str] = None,
        local_path: Optional[str] = None,
        split: str = "train",
    ) -> Dataset:
        """
        Load dataset from Hugging Face or local path.

        Args:
            dataset_name: Hugging Face dataset name (e.g., "chainyo/rvl-cdip")
            local_path: Path to local dataset
            split: Dataset split to use

        Returns:
            Loaded dataset
        """
        if dataset_name:
            logger.info(f"Loading dataset {dataset_name} (split: {split})")
            # nosec B615 - Sample training script for model fine-tuning preparation
            # This code is not used in production and loads from trusted Hugging Face datasets
            # For production use, implement revision pinning via model_versions.py
            ds = load_dataset(dataset_name, split=split)  # nosec B615
        elif local_path:
            logger.info(f"Loading local dataset from {local_path}")
            ds = Dataset.load_from_disk(local_path)
        else:
            raise ValueError("Either dataset_name or local_path must be provided")

        logger.info(f"Dataset loaded with {len(ds)} samples")
        return ds

    def sample_data_by_label(
        self,
        dataset: Dataset,
        samples_per_label: int,
        label_mapping: Optional[Dict[int, str]] = None,
    ) -> List[Dict]:
        """
        Sample data by label with parallel processing.

        Args:
            dataset: Input dataset
            samples_per_label: Number of samples per label
            label_mapping: Mapping from label indices to names

        Returns:
            List of sampled data
        """
        if label_mapping is None:
            label_mapping = DEFAULT_LABEL_MAPPING

        # Get unique labels
        unique_labels = np.unique(dataset["label"])
        logger.info(f"Found {len(unique_labels)} unique labels: {unique_labels}")

        sampled_data = []

        def process_label(label: int) -> Tuple[List[Dict], str]:
            """Process a single label and return sampled data."""
            label_name = label_mapping.get(label, f"class_{label}")
            result_samples = []

            # Get indices for this label
            indices = [
                i
                for i, sample_label in enumerate(dataset["label"])
                if sample_label == label
            ]

            if len(indices) <= samples_per_label:
                sampled_indices = indices
                message = (
                    f"Label {label} ({label_name}): Using all {len(indices)} samples"
                )
            else:
                sampled_indices = np.random.choice(
                    indices, samples_per_label, replace=False
                )
                sampled_indices = [int(idx) for idx in sampled_indices]
                message = f"Label {label} ({label_name}): Sampled {samples_per_label} out of {len(indices)} samples"

            # Get the actual samples
            for idx in sampled_indices:
                result_samples.append(dataset[idx])

            return result_samples, message

        # Process labels in parallel
        max_workers = min(16, os.cpu_count())
        logger.info(f"Using {max_workers} workers for parallel sampling")

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_label = {
                executor.submit(process_label, label): label for label in unique_labels
            }

            for future in tqdm(
                concurrent.futures.as_completed(future_to_label),
                total=len(future_to_label),
                desc="Sampling labels",
            ):
                label = future_to_label[future]
                try:
                    samples, message = future.result()
                    sampled_data.extend(samples)
                    logger.info(message)
                except Exception as e:
                    logger.error(f"Error processing label {label}: {e}")

        logger.info(f"Total sampled data: {len(sampled_data)}")
        return sampled_data

    def save_and_upload_image(
        self,
        image: Image.Image,
        label: int,
        index: int,
        directory: str,
        label_mapping: Dict[int, str],
    ) -> str:
        """
        Save image locally and upload to S3.

        Args:
            image: PIL Image object
            label: Label index
            index: Sample index
            directory: S3 directory prefix
            label_mapping: Label mapping dictionary

        Returns:
            S3 URI of uploaded image
        """
        # Generate unique filename
        label_name = label_mapping.get(label, f"class_{label}")
        filename = f"{label_name}_{index}_{uuid.uuid4()}.png"
        filename = filename.replace(" ", "_")

        # Create temp directory if it doesn't exist
        os.makedirs("temp_images", exist_ok=True)

        local_path = os.path.join("temp_images", filename)
        s3_path = f"{directory}/images/{filename}"

        # Save image locally as PNG
        image.save(local_path, format="PNG")

        # Upload to S3
        self.s3_client.upload_file(local_path, self.bucket_name, s3_path)

        # Remove local file
        os.remove(local_path)

        return f"s3://{self.bucket_name}/{s3_path}"

    def create_jsonl_record(
        self,
        sample: Dict,
        s3_uri: str,
        label_mapping: Dict[int, str],
        system_prompt: str,
        task_prompt_template: str,
    ) -> Dict:
        """
        Create a JSONL record in Bedrock format.

        Args:
            sample: Dataset sample
            s3_uri: S3 URI of the image
            label_mapping: Label mapping dictionary
            system_prompt: System prompt text
            task_prompt_template: Task prompt template

        Returns:
            JSONL record dictionary
        """
        label = sample["label"]
        mapped_label = label_mapping.get(label, f"class_{label}")

        return {
            "schemaVersion": "bedrock-conversation-2024",
            "system": [{"text": system_prompt}],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"text": task_prompt_template},
                        {
                            "image": {
                                "format": "png",
                                "source": {
                                    "s3Location": {
                                        "uri": s3_uri,
                                        "bucketOwner": self.account_id,
                                    }
                                },
                            }
                        },
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"text": f'```json\n{{"type": "{mapped_label}"}}\n```'}
                    ],
                },
            ],
        }

    def process_samples(
        self,
        sampled_data: List[Dict],
        directory: str,
        label_mapping: Dict[int, str],
        system_prompt: str,
        task_prompt_template: str,
    ) -> List[Dict]:
        """
        Process samples in parallel: upload images and create JSONL records.

        Args:
            sampled_data: List of sampled data
            directory: S3 directory prefix
            label_mapping: Label mapping dictionary
            system_prompt: System prompt text
            task_prompt_template: Task prompt template

        Returns:
            List of JSONL records
        """

        def process_single_sample(sample_and_index: Tuple[Dict, int]) -> Dict:
            """Process a single sample."""
            sample, index = sample_and_index

            # Save and upload image
            s3_uri = self.save_and_upload_image(
                sample["image"], sample["label"], index, directory, label_mapping
            )

            # Create JSONL record
            return self.create_jsonl_record(
                sample, s3_uri, label_mapping, system_prompt, task_prompt_template
            )

        # Process samples in parallel
        max_workers = min(32, (os.cpu_count() or 1) * 2)
        logger.info(f"Using {max_workers} workers for parallel processing")

        jsonl_records = []
        sample_indices = list(enumerate(sampled_data))

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {
                executor.submit(process_single_sample, sample_info): i
                for i, sample_info in enumerate(sample_indices)
            }

            for future in tqdm(
                concurrent.futures.as_completed(future_to_idx),
                total=len(future_to_idx),
                desc="Processing samples",
            ):
                try:
                    record = future.result()
                    jsonl_records.append(record)
                except Exception as e:
                    idx = future_to_idx[future]
                    logger.error(f"Error processing sample {idx}: {e}")

        return jsonl_records

    def split_and_save_data(
        self, jsonl_records: List[Dict], directory: str, validation_split: float = 0.1
    ) -> Tuple[int, int]:
        """
        Split data and save JSONL files to S3.

        Args:
            jsonl_records: List of JSONL records
            directory: S3 directory prefix
            validation_split: Validation split ratio

        Returns:
            Tuple of (train_count, validation_count)
        """
        # Shuffle data
        random.shuffle(jsonl_records)  # nosec B311 - training data shuffle

        # Split data
        split_idx = int(len(jsonl_records) * (1 - validation_split))
        train_records = jsonl_records[:split_idx]
        validation_records = jsonl_records[split_idx:]

        logger.info(f"Training records: {len(train_records)}")
        logger.info(f"Validation records: {len(validation_records)}")

        # Save files locally
        train_path = "train.jsonl"
        validation_path = "validation.jsonl"

        with open(train_path, "w") as f:
            for record in train_records:
                f.write(json.dumps(record) + "\n")

        with open(validation_path, "w") as f:
            for record in validation_records:
                f.write(json.dumps(record) + "\n")

        # Upload to S3
        self.s3_client.upload_file(
            train_path, self.bucket_name, f"{directory}/train.jsonl"
        )
        self.s3_client.upload_file(
            validation_path, self.bucket_name, f"{directory}/validation.jsonl"
        )

        logger.info(
            f"Train JSONL uploaded to s3://{self.bucket_name}/{directory}/train.jsonl"
        )
        logger.info(
            f"Validation JSONL uploaded to s3://{self.bucket_name}/{directory}/validation.jsonl"
        )

        # Clean up local files
        os.remove(train_path)
        os.remove(validation_path)

        return len(train_records), len(validation_records)

    def cleanup_temp_files(self):
        """Clean up temporary files and directories."""
        if os.path.exists("temp_images"):
            shutil.rmtree("temp_images")
        logger.info("Temporary files cleaned up")


def main():
    """Main function to run the data preparation script."""
    parser = argparse.ArgumentParser(
        description="Prepare Nova fine-tuning dataset",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Prepare RVL-CDIP dataset with 100 samples per label
  python prepare_nova_finetuning_data.py --bucket-name my-bucket --samples-per-label 100
  
  # Use custom dataset from Hugging Face
  python prepare_nova_finetuning_data.py --bucket-name my-bucket --dataset custom/dataset --samples-per-label 50
  
  # Use local dataset
  python prepare_nova_finetuning_data.py --bucket-name my-bucket --local-dataset /path/to/data --samples-per-label 75
        """,
    )

    # Required arguments
    parser.add_argument(
        "--bucket-name", required=True, help="S3 bucket name for storing prepared data"
    )

    # Optional arguments
    parser.add_argument(
        "--directory",
        default="nova-finetuning-data",
        help="S3 directory prefix (default: nova-finetuning-data)",
    )
    parser.add_argument(
        "--samples-per-label",
        type=int,
        default=100,
        help="Number of samples per label (default: 100)",
    )
    parser.add_argument(
        "--dataset",
        default="chainyo/rvl-cdip",
        help="Hugging Face dataset name (default: chainyo/rvl-cdip)",
    )
    parser.add_argument(
        "--local-dataset", help="Path to local dataset (overrides --dataset)"
    )
    parser.add_argument(
        "--split", default="train", help="Dataset split to use (default: train)"
    )
    parser.add_argument(
        "--validation-split",
        type=float,
        default=0.1,
        help="Validation split ratio (default: 0.1)",
    )
    parser.add_argument(
        "--region", default="us-east-1", help="AWS region (default: us-east-1)"
    )
    parser.add_argument(
        "--label-mapping-file", help="JSON file with custom label mapping"
    )
    parser.add_argument(
        "--system-prompt-file", help="Text file with custom system prompt"
    )
    parser.add_argument(
        "--task-prompt-file", help="Text file with custom task prompt template"
    )

    args = parser.parse_args()

    # Load custom configurations if provided
    label_mapping = DEFAULT_LABEL_MAPPING
    if args.label_mapping_file:
        with open(args.label_mapping_file, "r") as f:
            custom_mapping = json.load(f)
            # Convert string keys to integers
            label_mapping = {int(k): v for k, v in custom_mapping.items()}
        logger.info(f"Loaded custom label mapping from {args.label_mapping_file}")

    system_prompt = DEFAULT_SYSTEM_PROMPT
    if args.system_prompt_file:
        with open(args.system_prompt_file, "r") as f:
            system_prompt = f.read().strip()
        logger.info(f"Loaded custom system prompt from {args.system_prompt_file}")

    task_prompt_template = DEFAULT_TASK_PROMPT_TEMPLATE
    if args.task_prompt_file:
        with open(args.task_prompt_file, "r") as f:
            task_prompt_template = f.read().strip()
        logger.info(f"Loaded custom task prompt from {args.task_prompt_file}")

    try:
        # Initialize service
        service = NovaDataPreparationService(args.bucket_name, args.region)

        # Load dataset
        dataset = service.load_dataset(
            dataset_name=None if args.local_dataset else args.dataset,
            local_path=args.local_dataset,
            split=args.split,
        )

        # Sample data by label
        sampled_data = service.sample_data_by_label(
            dataset, args.samples_per_label, label_mapping
        )

        if not sampled_data:
            logger.error(
                "No data was sampled. Please check your dataset and parameters."
            )
            return 1

        # Process samples (upload images and create JSONL records)
        logger.info(
            "Processing samples: uploading images and creating JSONL records..."
        )
        jsonl_records = service.process_samples(
            sampled_data,
            args.directory,
            label_mapping,
            system_prompt,
            task_prompt_template,
        )

        # Split and save data
        train_count, val_count = service.split_and_save_data(
            jsonl_records, args.directory, args.validation_split
        )

        # Clean up
        service.cleanup_temp_files()

        # Summary
        logger.info("Dataset preparation completed successfully!")
        logger.info(f"Total samples processed: {len(jsonl_records)}")
        logger.info(f"Training samples: {train_count}")
        logger.info(f"Validation samples: {val_count}")
        logger.info(f"Data uploaded to: s3://{args.bucket_name}/{args.directory}/")
        logger.info(
            f"Training data: s3://{args.bucket_name}/{args.directory}/train.jsonl"
        )
        logger.info(
            f"Validation data: s3://{args.bucket_name}/{args.directory}/validation.jsonl"
        )

        return 0

    except Exception as e:
        logger.error(f"Error during data preparation: {e}")
        return 1


if __name__ == "__main__":
    exit(main())
