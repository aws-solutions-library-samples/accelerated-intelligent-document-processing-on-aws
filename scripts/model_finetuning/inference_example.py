#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Nova Fine-tuned Model Inference Example Script

This script demonstrates how to run inference on fine-tuned Nova models. It can:
1. Run inference on both base and fine-tuned models
2. Compare performance between models
3. Process single images or batches
4. Output results in readable format
5. Calculate accuracy metrics when ground truth is provided

Prerequisites:
- Set up AWS CLI: `aws configure`
- Install required packages: `pip install boto3 pillow python-dotenv`
- Have a fine-tuned model (base model or provisioned throughput)
- Appropriate AWS permissions for Bedrock

Example usage:
    python inference_example.py \
        --model-id us.amazon.nova-lite-v1:0 \
        --image-path /path/to/document.png \
        --system-prompt-file system_prompt.txt

    python inference_example.py \
        --provisioned-model-arn arn:aws:bedrock:us-east-1:123456789012:provisioned-model/... \
        --image-path /path/to/document.png \
        --compare-with-base

    python inference_example.py \
        --model-id us.amazon.nova-lite-v1:0 \
        --image-directory /path/to/images/ \
        --ground-truth-file labels.json \
        --output-file results.json
"""

import argparse
import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from PIL import Image

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Default prompts for document classification
DEFAULT_SYSTEM_PROMPT = """You are a document classification expert who can analyze and identify document types from images. Your task is to determine the document type based on its visual appearance, layout, and content, using the provided document type definitions. Your output must be valid JSON according to the requested format."""

DEFAULT_TASK_PROMPT = """The <document-types> XML tags contain a markdown table of known document types for detection.

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


class NovaInferenceService:
    """Service for running inference on Nova models."""

    def __init__(self, region: str = "us-east-1"):
        """Initialize the inference service."""
        self.region = region
        self.bedrock_client = boto3.client("bedrock-runtime", region_name=region)
        logger.info(f"Initialized Nova inference service in region {region}")

    def encode_image(self, image_path: str) -> str:
        """
        Encode image to base64.

        Args:
            image_path: Path to the image file

        Returns:
            Base64 encoded image
        """
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode("utf-8")

    def preprocess_image(
        self, image_path: str, max_size: Tuple[int, int] = (2048, 2048)
    ) -> str:
        """
        Preprocess image for optimal inference.

        Args:
            image_path: Path to the image file
            max_size: Maximum image dimensions

        Returns:
            Path to processed image
        """
        try:
            with Image.open(image_path) as img:
                # Convert to RGB if necessary
                if img.mode != "RGB":
                    img = img.convert("RGB")

                # Resize if too large
                if img.size[0] > max_size[0] or img.size[1] > max_size[1]:
                    img.thumbnail(max_size, Image.Resampling.LANCZOS)

                    # Save processed image
                    processed_path = f"temp_processed_{os.path.basename(image_path)}"
                    img.save(processed_path, "JPEG", quality=85)
                    return processed_path

                return image_path

        except Exception as e:
            logger.warning(f"Error preprocessing image {image_path}: {e}")
            return image_path

    def invoke_model(
        self,
        model_id: str,
        system_prompt: str,
        task_prompt: str,
        image_path: str,
        temperature: float = 0.0,
        top_k: int = 5,
        max_tokens: int = 1000,
    ) -> Dict:
        """
        Invoke Nova model for inference.

        Args:
            model_id: Model ID or ARN
            system_prompt: System prompt
            task_prompt: Task prompt
            image_path: Path to image file
            temperature: Sampling temperature
            top_k: Top-k sampling
            max_tokens: Maximum tokens to generate

        Returns:
            Model response
        """
        # Preprocess image
        processed_image_path = self.preprocess_image(image_path)

        try:
            # Encode image
            with open(processed_image_path, "rb") as image_file:
                image_bytes = image_file.read()

            # Prepare request
            system = [{"text": system_prompt}]
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"text": task_prompt},
                        {"image": {"format": "jpeg", "source": {"bytes": image_bytes}}},
                    ],
                }
            ]

            inference_config = {
                "maxTokens": max_tokens,
                "topP": 0.1,
                "temperature": temperature,
                "topK": top_k,
            }

            # Make request with retry logic
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    response = self.bedrock_client.converse(
                        modelId=model_id,
                        messages=messages,
                        system=system,
                        inferenceConfig=inference_config,
                    )

                    # Clean up processed image if it was created
                    if processed_image_path != image_path and os.path.exists(
                        processed_image_path
                    ):
                        os.remove(processed_image_path)

                    return response

                except ClientError as e:
                    if attempt < max_retries - 1 and e.response["Error"]["Code"] in [
                        "ThrottlingException",
                        "ServiceQuotaExceededException",
                    ]:
                        wait_time = (2**attempt) * 2
                        logger.warning(
                            f"Throttled, waiting {wait_time} seconds before retry..."
                        )
                        time.sleep(wait_time)
                    else:
                        raise

        finally:
            # Clean up processed image
            if processed_image_path != image_path and os.path.exists(
                processed_image_path
            ):
                os.remove(processed_image_path)

    def parse_response(self, response: Dict) -> Tuple[str, str, Dict]:
        """
        Parse model response to extract prediction.

        Args:
            response: Model response

        Returns:
            Tuple of (status, prediction, raw_response)
        """
        try:
            # Defensive: Handle case where LLM returns empty content array
            content_array = response["output"]["message"].get("content", [])
            if not content_array or len(content_array) == 0:
                logger.error(
                    "LLM returned empty content array",
                    extra={"response": response},
                )
                return "error", "Empty response from model", response

            content = content_array[0]["text"]

            # Try to extract JSON from response
            import re

            json_match = re.search(r'\{[^\{\}]*"type"\s*:\s*"[^"]+"[^\{\}]*\}', content)
            if json_match:
                json_content = json.loads(json_match.group(0))
                if "type" in json_content:
                    return "success", json_content["type"].lower().strip(), response

            # If JSON parsing fails, try to extract type with regex
            match = re.search(r'"type"\s*:\s*"([^"]+)"', content)
            if match:
                return "success", match.group(1).lower().strip(), response

            return "unknown", content, response

        except Exception as e:
            logger.error(f"Error parsing response: {e}")
            return "error", str(e), response

    def run_inference(
        self,
        model_id: str,
        image_path: str,
        system_prompt: str = None,
        task_prompt: str = None,
        **kwargs,
    ) -> Dict:
        """
        Run inference on a single image.

        Args:
            model_id: Model ID or ARN
            image_path: Path to image file
            system_prompt: Optional system prompt
            task_prompt: Optional task prompt
            **kwargs: Additional inference parameters

        Returns:
            Inference result
        """
        if system_prompt is None:
            system_prompt = DEFAULT_SYSTEM_PROMPT
        if task_prompt is None:
            task_prompt = DEFAULT_TASK_PROMPT

        start_time = time.time()

        try:
            response = self.invoke_model(
                model_id, system_prompt, task_prompt, image_path, **kwargs
            )

            status, prediction, raw_response = self.parse_response(response)

            inference_time = time.time() - start_time

            # Extract token usage
            usage = response.get("usage", {})
            input_tokens = usage.get("inputTokens", 0)
            output_tokens = usage.get("outputTokens", 0)

            return {
                "image_path": image_path,
                "model_id": model_id,
                "status": status,
                "prediction": prediction,
                "confidence": 1.0 if status == "success" else 0.0,
                "inference_time_seconds": inference_time,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "raw_response": raw_response,
            }

        except Exception as e:
            logger.error(f"Error during inference for {image_path}: {e}")
            return {
                "image_path": image_path,
                "model_id": model_id,
                "status": "error",
                "prediction": str(e),
                "confidence": 0.0,
                "inference_time_seconds": time.time() - start_time,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "raw_response": None,
            }

    def batch_inference(
        self,
        model_id: str,
        image_paths: List[str],
        ground_truth: Optional[Dict] = None,
        **kwargs,
    ) -> List[Dict]:
        """
        Run inference on multiple images.

        Args:
            model_id: Model ID or ARN
            image_paths: List of image file paths
            ground_truth: Optional dictionary mapping image paths to true labels
            **kwargs: Additional inference parameters

        Returns:
            List of inference results
        """
        results = []

        for i, image_path in enumerate(image_paths):
            logger.info(
                f"Processing image {i + 1}/{len(image_paths)}: {os.path.basename(image_path)}"
            )

            result = self.run_inference(model_id, image_path, **kwargs)

            # Add ground truth if available
            if ground_truth and image_path in ground_truth:
                result["ground_truth"] = ground_truth[image_path]
                result["correct"] = result["prediction"] == ground_truth[image_path]

            results.append(result)

        return results

    def compare_models(
        self,
        model_ids: List[str],
        image_paths: List[str],
        model_names: Optional[List[str]] = None,
        **kwargs,
    ) -> Dict:
        """
        Compare multiple models on the same images.

        Args:
            model_ids: List of model IDs/ARNs
            image_paths: List of image file paths
            model_names: Optional list of human-readable model names
            **kwargs: Additional inference parameters

        Returns:
            Comparison results
        """
        if model_names is None:
            model_names = [f"Model_{i + 1}" for i in range(len(model_ids))]

        comparison_results = {}

        for model_id, model_name in zip(model_ids, model_names):
            logger.info(f"Running inference with {model_name} ({model_id})")
            results = self.batch_inference(model_id, image_paths, **kwargs)
            comparison_results[model_name] = results

        return comparison_results


def load_ground_truth(file_path: str) -> Dict:
    """Load ground truth labels from JSON file."""
    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading ground truth file: {e}")
        return {}


def calculate_metrics(results: List[Dict]) -> Dict:
    """Calculate accuracy metrics from results."""
    total = len(results)
    if total == 0:
        return {}

    successful = sum(1 for r in results if r["status"] == "success")
    correct = sum(1 for r in results if r.get("correct", False))

    # Calculate average metrics
    avg_inference_time = sum(r["inference_time_seconds"] for r in results) / total
    total_tokens = sum(r["total_tokens"] for r in results)
    avg_tokens = total_tokens / total

    return {
        "total_images": total,
        "successful_inferences": successful,
        "success_rate": successful / total,
        "correct_predictions": correct,
        "accuracy": correct / total if total > 0 else 0.0,
        "average_inference_time_seconds": avg_inference_time,
        "total_tokens_used": total_tokens,
        "average_tokens_per_image": avg_tokens,
    }


def save_results(results: Dict, output_file: str):
    """Save results to JSON file."""
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2, default=str)
    logger.info(f"Results saved to: {output_file}")


def print_results_summary(results: List[Dict], model_name: str = "Model"):
    """Print a summary of inference results."""
    metrics = calculate_metrics(results)

    print(f"\n{model_name} Results Summary:")
    print("=" * 50)
    print(f"Total Images: {metrics.get('total_images', 0)}")
    print(f"Successful Inferences: {metrics.get('successful_inferences', 0)}")
    print(f"Success Rate: {metrics.get('success_rate', 0):.2%}")

    if metrics.get("correct_predictions") is not None:
        print(f"Correct Predictions: {metrics.get('correct_predictions', 0)}")
        print(f"Accuracy: {metrics.get('accuracy', 0):.2%}")

    print(
        f"Average Inference Time: {metrics.get('average_inference_time_seconds', 0):.2f}s"
    )
    print(f"Total Tokens Used: {metrics.get('total_tokens_used', 0):,}")
    print(f"Average Tokens per Image: {metrics.get('average_tokens_per_image', 0):.1f}")


def main():
    """Main function to run inference examples."""
    parser = argparse.ArgumentParser(
        description="Run inference on Nova fine-tuned models",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single image inference with base model
  python inference_example.py \\
    --model-id us.amazon.nova-lite-v1:0 \\
    --image-path document.png
  
  # Single image inference with provisioned model
  python inference_example.py \\
    --provisioned-model-arn arn:aws:bedrock:us-east-1:123456789012:provisioned-model/... \\
    --image-path document.png
  
  # Batch inference with ground truth
  python inference_example.py \\
    --model-id us.amazon.nova-lite-v1:0 \\
    --image-directory /path/to/images/ \\
    --ground-truth-file labels.json \\
    --output-file results.json
  
  # Compare base model with fine-tuned model
  python inference_example.py \\
    --provisioned-model-arn arn:aws:bedrock:us-east-1:123456789012:provisioned-model/... \\
    --image-directory /path/to/images/ \\
    --compare-with-base \\
    --output-file comparison.json
        """,  # arn-partition-ok: argparse epilog — commercial example ARNs in help text
    )

    # Model specification (mutually exclusive)
    model_group = parser.add_mutually_exclusive_group(required=True)
    model_group.add_argument(
        "--model-id", help="Base model ID (e.g., us.amazon.nova-lite-v1:0)"
    )
    model_group.add_argument("--provisioned-model-arn", help="Provisioned model ARN")

    # Input specification (mutually exclusive)
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--image-path", help="Path to single image file")
    input_group.add_argument(
        "--image-directory", help="Directory containing image files"
    )

    # Optional arguments
    parser.add_argument(
        "--ground-truth-file", help="JSON file with ground truth labels"
    )
    parser.add_argument(
        "--system-prompt-file", help="Text file with custom system prompt"
    )
    parser.add_argument("--task-prompt-file", help="Text file with custom task prompt")
    parser.add_argument("--output-file", help="JSON file to save results")
    parser.add_argument(
        "--compare-with-base",
        action="store_true",
        help="Compare with base Nova Lite model",
    )
    parser.add_argument(
        "--region", default="us-east-1", help="AWS region (default: us-east-1)"
    )

    # Inference parameters
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="Sampling temperature (default: 0.0)",
    )
    parser.add_argument(
        "--top-k", type=int, default=5, help="Top-k sampling (default: 5)"
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=1000,
        help="Maximum tokens to generate (default: 1000)",
    )

    # Output options
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument(
        "--no-summary", action="store_true", help="Skip printing results summary"
    )

    args = parser.parse_args()

    # Set logging level
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        # Initialize service
        service = NovaInferenceService(args.region)

        # Load custom prompts if provided
        system_prompt = DEFAULT_SYSTEM_PROMPT
        if args.system_prompt_file:
            with open(args.system_prompt_file, "r") as f:
                system_prompt = f.read().strip()
            logger.info(f"Loaded custom system prompt from {args.system_prompt_file}")

        task_prompt = DEFAULT_TASK_PROMPT
        if args.task_prompt_file:
            with open(args.task_prompt_file, "r") as f:
                task_prompt = f.read().strip()
            logger.info(f"Loaded custom task prompt from {args.task_prompt_file}")

        # Load ground truth if provided
        ground_truth = {}
        if args.ground_truth_file:
            ground_truth = load_ground_truth(args.ground_truth_file)
            logger.info(f"Loaded ground truth for {len(ground_truth)} images")

        # Determine model(s) to use
        primary_model_id = args.model_id or args.provisioned_model_arn
        model_ids = [primary_model_id]
        model_names = [
            "Fine-tuned Model" if args.provisioned_model_arn else "Base Model"
        ]

        if args.compare_with_base and args.provisioned_model_arn:
            model_ids.append("us.amazon.nova-lite-v1:0")
            model_names = ["Fine-tuned Model", "Base Model"]

        # Prepare image paths
        image_paths = []
        if args.image_path:
            image_paths = [args.image_path]
        elif args.image_directory:
            image_dir = Path(args.image_directory)
            supported_extensions = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif"}
            image_paths = [
                str(p)
                for p in image_dir.iterdir()
                if p.is_file() and p.suffix.lower() in supported_extensions
            ]

            if not image_paths:
                logger.error(
                    f"No supported image files found in {args.image_directory}"
                )
                return 1

            logger.info(f"Found {len(image_paths)} images to process")

        # Run inference
        inference_kwargs = {
            "system_prompt": system_prompt,
            "task_prompt": task_prompt,
            "temperature": args.temperature,
            "top_k": args.top_k,
            "max_tokens": args.max_tokens,
            "ground_truth": ground_truth,
        }

        if len(model_ids) == 1:
            # Single model inference
            logger.info(f"Running inference with {model_names[0]}")
            results = service.batch_inference(
                model_ids[0], image_paths, **inference_kwargs
            )

            if not args.no_summary:
                print_results_summary(results, model_names[0])

            # Save results if requested
            if args.output_file:
                output_data = {
                    "model_id": model_ids[0],
                    "model_name": model_names[0],
                    "results": results,
                    "metrics": calculate_metrics(results),
                    "inference_config": {
                        "temperature": args.temperature,
                        "top_k": args.top_k,
                        "max_tokens": args.max_tokens,
                    },
                }
                save_results(output_data, args.output_file)

        else:
            # Model comparison
            logger.info("Running model comparison...")
            comparison_results = service.compare_models(
                model_ids, image_paths, model_names, **inference_kwargs
            )

            if not args.no_summary:
                for model_name, results in comparison_results.items():
                    print_results_summary(results, model_name)

            # Save comparison results if requested
            if args.output_file:
                output_data = {
                    "comparison_type": "model_comparison",
                    "models": {
                        name: {
                            "model_id": model_id,
                            "results": results,
                            "metrics": calculate_metrics(results),
                        }
                        for (name, results), model_id in zip(
                            comparison_results.items(), model_ids
                        )
                    },
                    "inference_config": {
                        "temperature": args.temperature,
                        "top_k": args.top_k,
                        "max_tokens": args.max_tokens,
                    },
                }
                save_results(output_data, args.output_file)

        logger.info("✅ Inference completed successfully!")
        return 0

    except Exception as e:
        logger.error(f"Error during inference: {e}")
        return 1


if __name__ == "__main__":
    exit(main())
