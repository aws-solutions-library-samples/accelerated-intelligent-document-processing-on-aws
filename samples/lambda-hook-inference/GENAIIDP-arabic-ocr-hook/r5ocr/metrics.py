"""CloudWatch Embedded Metric Format lines (no PutMetricData permission needed)."""

import json
import time


def emit(name: str, value: float = 1, **dims: str) -> None:
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": "IDP/ArabicOCR",
                            "Dimensions": [sorted(dims)],
                            "Metrics": [{"Name": name, "Unit": "Count"}],
                        }
                    ],
                },
                name: value,
                **dims,
            }
        )
    )
