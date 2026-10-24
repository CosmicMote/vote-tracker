import json
from types import SimpleNamespace

import pytest
import respx

from tracker.llm import make_provider
from tracker.llm.anthropic import FALLBACK_BETA, AnthropicProvider
from tracker.llm.base import ProviderError, SummaryProvider
from tracker.llm.ollama import OllamaProvider
from tracker.llm.prompts import OUTPUT_SCHEMA


@respx.mock
def test_ollama_sends_schema_and_temperature_zero():
    route = respx.post("http://localhost:11434/api/chat").respond(
        200, json={"message": {"role": "assistant", "content": '{"ok": true}'}}
    )
    p = OllamaProvider("llama3.1:8b")
    assert p.complete_json("sys", "user", OUTPUT_SCHEMA) == '{"ok": true}'
    body = json.loads(route.calls[0].request.content)
    assert body["format"] == OUTPUT_SCHEMA
    assert body["format"]["properties"]["what_it_does"]["maxItems"] == 4
    assert body["options"]["temperature"] == 0 and body["stream"] is False
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert p.name == "ollama:llama3.1:8b"


@respx.mock
def test_ollama_errors_become_provider_errors():
    respx.post("http://localhost:11434/api/chat").respond(500)
    with pytest.raises(ProviderError):
        OllamaProvider("m").complete_json("s", "u", {})


@respx.mock
def test_ollama_missing_model_explains_fix():
    respx.post("http://localhost:11434/api/chat").respond(404, json={"error": "model 'llama3.1:8b' not found"})
    with pytest.raises(ProviderError, match=r"model 'llama3.1:8b' not found.*ollama pull llama3.1:8b"):
        OllamaProvider("llama3.1:8b").complete_json("s", "u", {})


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def fake_client(stop_reason="end_turn", text='{"ok": true}'):
    messages = FakeMessages(SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)]))
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def test_anthropic_uses_structured_output_and_no_sampling_params():
    client, messages = fake_client()
    p = AnthropicProvider("claude-opus-5-5", client=client)
    assert p.complete_json("sys", "user", OUTPUT_SCHEMA) == '{"ok": true}'
    kw = messages.kwargs
    sent = kw["output_config"]["format"]["schema"]
    assert kw["output_config"]["effort"] == "low" and kw["output_config"]["format"]["type"] == "json_schema"
    assert "maxItems" not in json.dumps(sent) and sent["required"] == OUTPUT_SCHEMA["required"]
    assert sent["additionalProperties"] is False
    assert kw["system"] == "sys" and kw["messages"] == [{"role": "user", "content": "user"}]
    assert "temperature" not in kw
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"


def test_anthropic_skips_fallbacks_for_models_without_them():
    client, messages = fake_client()
    AnthropicProvider("claude-haiku-5-5", client=client).complete_json("s", "u", {})
    assert "fallbacks" not in messages.kwargs and "betas" not in messages.kwargs


def test_anthropic_refusal_raises():
    client, _ = fake_client(stop_reason="refusal", text="")
    with pytest.raises(ProviderError):
        AnthropicProvider("claude-opus-5-5", client=client).complete_json("s", "u", {})


def test_provider_switch_is_config_only(settings, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    settings.llm.provider = "ollama"
    assert isinstance(make_provider(settings), OllamaProvider)
    settings.llm.provider = "anthropic"
    settings.llm.model = "claude-opus-5-5"
    p = make_provider(settings)
    assert isinstance(p, AnthropicProvider) and p.name == "anthropic:claude-opus-5-5"
    assert isinstance(p, SummaryProvider)
