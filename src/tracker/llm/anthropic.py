"""Anthropic Messages API provider, through the official SDK.

The Messages API no longer accepts sampling parameters such as temperature, so determinism
comes from structured output (a JSON schema) and the summary cache rather than temperature 0.
"""

from __future__ import annotations

import anthropic

from .base import ProviderError

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept the server-side `fallbacks: "default"` refusal fallback.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


# Structured outputs do not support these constraints; the SDK's parse() helper strips them too.
UNSUPPORTED_SCHEMA_KEYS = {"minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum", "multipleOf"}


def strip_unsupported(schema):
    if isinstance(schema, dict):
        return {k: strip_unsupported(v) for k, v in schema.items() if k not in UNSUPPORTED_SCHEMA_KEYS}
    if isinstance(schema, list):
        return [strip_unsupported(v) for v in schema]
    return schema


class AnthropicProvider:
    def __init__(
        self,
        model: str = "claude-opus-5-5",
        api_key: str | None = None,
        effort: str = "low",
        fallbacks: str = "default",
        client: anthropic.Anthropic | None = None,
    ):
        self.model = model
        self.name = f"anthropic:{model}"
        self.effort = effort
        self.fallbacks = fallbacks if model in FALLBACK_MODELS else ""
        if client is None:
            # Without an explicit key the SDK resolves credentials itself (env var or `ant auth login`).
            client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self._client = client

    def complete_json(self, system: str, user: str, schema: dict) -> str:
        extra: dict = {}
        if self.fallbacks:
            extra = {"betas": [FALLBACK_BETA], "fallbacks": self.fallbacks}
        try:
            response = self._client.beta.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": strip_unsupported(schema)}},
                **extra,
            )
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"Anthropic connection error: {e}") from e
        except anthropic.RateLimitError as e:
            raise ProviderError(f"Anthropic rate limit: {e}") from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"Anthropic API error {e.status_code}: {e.message}") from e
        if response.stop_reason == "refusal":
            raise ProviderError("Anthropic declined the request (refusal)")
        if response.stop_reason == "max_tokens":
            raise ProviderError("Anthropic response hit max_tokens")
        return next((b.text for b in response.content if b.type == "text"), "")
