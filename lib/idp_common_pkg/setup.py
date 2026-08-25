#!/usr/bin/env python

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from setuptools import find_packages, setup

# Core dependencies required for all installations
install_requires = [
    "boto3==1.42.0",  # Core dependency for AWS services
]

# Optional dependencies by component
extras_require = {
    # Core utilities only - minimal dependencies
    "core": [],
    # Analytics agent dependencies
    "analytics": [
        "strands-agents==1.14.0",  # Pin to exact working version
        "pandas>=2.0.0",
    ],
    # Code intelligence module dependencies
    "code_intel": [
        "strands-agents-tools>=0.2.2",
        "bedrock-agentcore>=0.1.1",
    ],
    # Image handling dependencies
    "image": [
        "Pillow==12.1.1",
    ],
    # OCR module dependencies
    "ocr": [
        "Pillow==12.1.1",
        "pypdfium2>=5.5.0",
        "amazon-textract-textractor[pandas]==1.9.2",
        "numpy==1.26.4",
        "pandas==2.2.3",
        "openpyxl==3.1.5",
        "python-docx==1.2.0",
    ],
    # Classification module dependencies
    "classification": [
        "Pillow==12.1.1",  # For image handling
    ],
    # Extraction module dependencies
    "extraction": [
        "Pillow==12.1.1",  # For image handling
    ],
    # Assessment module dependencies
    "assessment": [
        "Pillow==12.1.1",  # For image handling
    ],
    # Evaluation module dependencies
    # Note: the ``[llm]`` extra (strands-based LLM comparator) is intentionally
    # NOT installed — IDP provides its own Bedrock-backed comparator via
    # ``idp_common.evaluation.llm_comparator.LLMComparator``.
    "evaluation": [
        "stickler-eval==0.5.0",
        "genson==1.3.0",
        "numpy>=1.26,<3",  # Match stickler-eval's numpy range
    ],
    # Reporting module dependencies
    "reporting": [
        "pyarrow==23.0.1",  # For Parquet conversion
    ],
    # Document service factory dependencies (DynamoDB-only)
    "docs_service": [
        "aws-xray-sdk>=2.14.0",
    ],
    # Testing dependencies
    "test": [
        "pytest>=7.4.0",
        "pytest-cov>=4.1.0",
        "pytest-xdist>=3.3.1",  # For parallel test execution
        "requests>=2.32.3,<3.0.0",
        "pyarrow==23.0.1",
        "PyYAML==6.0.2",
        "openpyxl==3.1.5",
        "python-docx==1.2.0",
    ],
    # Development dependencies
    "dev": [
        "python-dotenv>=1.1.0,<2.0.0",
        "ipykernel>=6.29.5,<7.0.0",
        "jupyter>=1.1.1,<2.0.0",
    ],
    # Agents module dependencies
    "agents": [
        "strands-agents==1.14.0",  # Pin to exact working version
        "strands-agents-tools==0.2.22",  # Pin to exact working version
        "bedrock-agentcore>=0.1.1",  # Specifically for the code interpreter tool
        "regex>=2024.0.0,<2026.0.0",  # Pin regex version to avoid conflicts
        "jsonschema>=4.0.0",  # Quick-Start agent synthesis tools (schema authoring)
    ],
    # Synthesis / cold-start bootstrap. The schema bridge, prompt->schema
    # authoring, catalog match and orchestration use only core deps. The heavy
    # SEED document generator is a separate optional extra (synthesis-generator)
    # so this base capability stays light.
    "synthesis": [
        "jsonschema>=4.0.0",
    ],
    # Optional synthetic-document generator (SEED, published as seed-data). Heavy
    # (Strands + Bedrock + rendering); install only where generation runs. The
    # engine adapter imports it lazily and degrades gracefully when absent.
    # NOTE: seed-data requires numpy 2.x (opencv/scikit-image/numba), which
    # CONFLICTS with the numpy==1.26.4 pin in the ocr/evaluation/all extras.
    # Install this in an isolated environment (the generator's own container /
    # AgentCore runtime), NOT alongside idp_common[ocr|evaluation|all].
    "synthesis-generator": [
        "seed-data>=0.0.6",
    ],
    # Full package with all dependencies
    "all": [
        "stickler-eval==0.5.0",
        "genson==1.3.0",
        "Pillow==12.1.1",
        "pypdfium2>=5.5.0",
        "amazon-textract-textractor[pandas]==1.9.2",
        "numpy==1.26.4",
        "pandas==2.2.3",
        "requests==2.32.4",
        "pyarrow==23.0.1",
        "openpyxl==3.1.5",
        "python-docx==1.2.0",
        "strands-agents==1.14.0",  # Pin to exact working version
        "strands-agents-tools==0.2.22",  # Pin to exact working version
        "bedrock-agentcore>=0.1.1",
        "regex>=2024.0.0,<2026.0.0",
        "jsonschema>=4.0.0",
    ],
}

setup(
    name="idp_common",
    version="0.6.5",
    packages=find_packages(
        exclude=[
            "build",
            "build.*",
            "*build",
            "*build.*",
            "*.build",
            "*.build.*",
            "**build**",
        ]
    ),
    include_package_data=True,
    python_requires=">=3.12,<3.14",
    install_requires=install_requires,
    extras_require=extras_require,
)
