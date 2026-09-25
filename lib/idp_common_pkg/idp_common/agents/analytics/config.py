# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Configuration management for analytics agents.
"""

import logging
from typing import Any, Dict

from ..common.config import (
    DEFAULT_AGENT_MODEL_ID,
    configure_logging,
    get_environment_config,
)

logger = logging.getLogger(__name__)


def get_analytics_config() -> Dict[str, Any]:
    """
    Get analytics-specific configuration from environment variables.

    Returns:
        Dict containing analytics configuration values

    Raises:
        ValueError: If required environment variables are missing
    """
    # Define required environment variables for analytics
    required_keys = [
        "ATHENA_DATABASE",
        "ATHENA_OUTPUT_LOCATION",
    ]

    # Get base configuration
    config = get_environment_config(required_keys)

    # Add analytics-specific defaults
    config.setdefault(
        "max_polling_attempts", 30
    )  # 2 seconds per attempt, Athena queries can take a while
    config.setdefault("query_timeout_seconds", 300)  # 5 minutes

    # Configure logging based on the configuration
    configure_logging(
        log_level=config.get("log_level"),
        strands_log_level=config.get("strands_log_level"),
    )

    logger.info("Analytics configuration loaded successfully")
    return config


def get_analytics_model_id() -> str:
    """
    Get the analytics agent model ID from configuration.

    Uses the modern configuration system that reads user-changed values from DynamoDB.
    Note: Analytics agents typically use the same model as chat companion.

    Returns:
        Model ID string
    """
    try:
        from ...config import get_config

        # Use the modern configuration system that reads from DynamoDB
        config = get_config(as_model=True)

        # Analytics agents typically use the chat companion model
        # Check if there's a specific analytics model configured, otherwise use chat companion
        if hasattr(config.agents, "analytics") and hasattr(
            config.agents.analytics, "model_id"
        ):
            model_id = config.agents.analytics.model_id
            logger.info(
                f"Using analytics-specific model ID from configuration: {model_id}"
            )
        else:
            model_id = config.agents.chat_companion.model_id
            logger.info(f"Using chat companion model ID for analytics: {model_id}")

        return model_id

    except Exception as e:
        logger.warning(f"Failed to load model ID from configuration: {e}")

        # Final fallback to default
        default_model_id = DEFAULT_AGENT_MODEL_ID
        logger.info(f"Using default analytics model ID: {default_model_id}")
        return default_model_id


def load_python_plot_generation_examples() -> str:
    """
    Load sample python plot generation examples.
    TODO: this is hard coded for now because the assets directory was hard to find in the lambda environment.


    Returns:
        String containing sample python code generation examples
    """

    return """
## Python Libraries and Code Examples

When generating Python code for visualization, you can use the following libraries:
- `json`: For JSON serialization
- `pandas`: For data manipulation
- Standard Python libraries (math, datetime, etc.)

### Example Python Code for Table Generation

```python
import json
import pandas as pd

# Read query results from local "query_results.csv" file into a dataframe
df = pd.read_csv("query_results.csv")

# Create table data
table_data = {
    "responseType": "table",
    "headers": [
        {"id": col, "label": col.replace('_', ' ').title(), "sortable": True}
        for col in df.columns
    ],
    "rows": [
        {
            "id": row.get('document_id', f"row-{i}"),
            "data": row
        }
        for i, row in enumerate(df.to_dict('records'))
    ]
}

# Output as JSON
print(json.dumps(table_data))
```

### Example Python Code for Plot Generation

```python
import json
import pandas as pd
import random

# Read query results from local "query_results.csv" file into a dataframe
df = pd.read_csv("query_results.csv")

# Generate colors
def generate_colors(n):
    colors = []
    for i in range(n):
        r = random.randint(0, 255)  # nosec B311 - chart color generation, not security
        g = random.randint(0, 255)  # nosec B311
        b = random.randint(0, 255)  # nosec B311
        colors.append(f"rgba({r}, {g}, {b}, 0.2)")
    return colors

bg_colors = generate_colors(len(df))
border_colors = [color.replace("0.2", "1") for color in bg_colors]

# Create plot data
plot_data = {
    "responseType": "plotData",
    "data": {
        "datasets": [
            {
                "backgroundColor": bg_colors,
                "borderColor": border_colors,
                "data": df['count'].tolist(),
                "borderWidth": 1,
                "label": "Document Count"
            }
        ],
        "labels": df['document_type'].tolist()
    },
    "options": {
        "scales": {
            "y": {
                "beginAtZero": True
            }
        },
        "responsive": True,
        "title": {
            "display": True,
            "text": "Document Distribution by Type"
        },
        "maintainAspectRatio": False
    },
    "type": "bar"
}

# Output as JSON
print(json.dumps(plot_data))
```
"""
