"""Summary providers, selected from config."""

from __future__ import annotations

from ..config import Settings
from .base import ProviderError, SummaryProvider


def make_provider(settings: Settings) -> SummaryProvider:
    llm = settings.llm
    if llm.provider == "ollama":
        from .ollama import OllamaProvider

        return OllamaProvider(llm.model, llm.base_url)
    if llm.provider == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider(
            llm.model, settings.secrets.anthropic_api_key, effort=llm.effort, fallbacks=llm.fallbacks
        )
    raise ValueError(f"unknown llm.provider {llm.provider!r}")


__all__ = ["ProviderError", "SummaryProvider", "make_provider"]
