"""Bedrock Converse wrapper. Claude 5.x rejects temperature/topP/topK, so none are sent."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
        )

    def as_dict(self) -> dict:
        return {
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "totalTokens": self.input_tokens + self.output_tokens,
        }


def converse(
    client,
    model_id: str,
    system: str,
    content: list[dict],
    effort: str | None,
    max_tokens: int,
) -> tuple[str, Usage]:
    kwargs = {
        "modelId": model_id,
        "system": [{"text": system}],
        "messages": [{"role": "user", "content": content}],
    }
    additional: dict = {}
    if "anthropic" in model_id.lower():
        # Same as the IDP today: Claude gets the cap as snake_case max_tokens in
        # additionalModelRequestFields and no inferenceConfig at all.
        additional["max_tokens"] = max_tokens
    else:
        kwargs["inferenceConfig"] = {"maxTokens": max_tokens}
    if effort:
        additional["output_config"] = {"effort": effort}
    if additional:
        kwargs["additionalModelRequestFields"] = additional
    resp = client.converse(**kwargs)
    text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
    u = resp.get("usage") or {}
    return text, Usage(int(u.get("inputTokens", 0)), int(u.get("outputTokens", 0)))
