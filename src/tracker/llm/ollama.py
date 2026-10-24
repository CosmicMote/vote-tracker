"""Local Ollama provider: /api/chat with a JSON schema as the output format, temperature 0."""

from __future__ import annotations

import httpx

from .base import ProviderError


class OllamaProvider:
    def __init__(self, model: str, base_url: str = "http://localhost:11434", timeout: float = 300.0):
        self.model = model
        self.name = f"ollama:{model}"
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def complete_json(self, system: str, user: str, schema: dict) -> str:
        try:
            resp = self._client.post(
                "/api/chat",
                json={
                    "model": self.model,
                    "stream": False,
                    "format": schema,
                    "options": {"temperature": 0},
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
            )
        except httpx.HTTPError as e:
            raise ProviderError(f"Ollama request failed: {e}") from e
        if resp.status_code >= 400:
            # Ollama explains failures in the body, e.g. {"error": "model 'x' not found"}.
            try:
                detail = resp.json().get("error") or resp.text
            except ValueError:
                detail = resp.text
            hint = f" (run `ollama pull {self.model}` or change llm.model)" if "not found" in detail else ""
            raise ProviderError(f"Ollama returned HTTP {resp.status_code}: {detail}{hint}")
        return resp.json().get("message", {}).get("content", "")
