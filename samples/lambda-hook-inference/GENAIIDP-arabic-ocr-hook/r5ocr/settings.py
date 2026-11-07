import os
from dataclasses import dataclass
from typing import Optional


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class Settings:
    reader_model_id: str
    reader_effort: Optional[str]
    read_method: str
    safety_margin_s: float
    textract_max_attempts: int
    max_output_tokens: int

    @classmethod
    def from_env(cls) -> "Settings":
        effort = os.environ.get("READER_EFFORT", "medium").strip() or None
        method = os.environ.get("READ_METHOD", "M0").strip().upper()
        if method not in ("M0", "M1", "M2", "M3"):
            raise ValueError(f"READ_METHOD must be M0, M1, M2 or M3, got {method!r}")
        return cls(
            reader_model_id=os.environ["READER_MODEL_ID"],
            reader_effort=None if effort == "none" else effort,
            read_method=method,
            safety_margin_s=float(os.environ.get("SAFETY_MARGIN_S", "60")),
            textract_max_attempts=int(os.environ.get("TEXTRACT_MAX_ATTEMPTS", "3")),
            max_output_tokens=int(os.environ.get("MAX_OUTPUT_TOKENS", "128000")),
        )
