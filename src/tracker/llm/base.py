"""The one interface the summarizer depends on."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class ProviderError(Exception):
    """The provider could not produce a response (network, API error, refusal)."""


@runtime_checkable
class SummaryProvider(Protocol):
    name: str  # e.g. 'ollama:<model>'

    def complete_json(self, system: str, user: str, schema: dict) -> str: ...
