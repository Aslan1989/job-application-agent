"""The LLM provider registry: Gemini default, pluggable, and isolated.

The separation these tests protect: adding a provider must not require touching
any discovery, filtering or pipeline code.
"""
from __future__ import annotations

from typing import Any, Mapping

import pytest

from job_agent import providers
from job_agent.providers import (DEFAULT_PROVIDER, ProviderInfo, available_providers,
                                 make_provider, provider_info, register_provider)


def test_gemini_is_the_default_matching_provider():
    assert DEFAULT_PROVIDER == "gemini"
    assert provider_info("gemini").remote is True
    assert provider_info("gemini").env_keys == ("GEMINI_API_KEY",)


def test_registry_lists_cost_information_for_every_provider():
    for info in available_providers():
        assert info.label and info.cost_note, f"{info.key} has no documented cost"
    assert {info.key for info in available_providers()} >= {"gemini", "ollama"}
    assert provider_info("ollama").remote is False


def test_environment_selects_the_provider(monkeypatch):
    monkeypatch.setenv("MATCH_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434")
    assert type(make_provider()).__name__ == "OllamaProvider"
    # An explicit argument always wins over the environment.
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-key")
    assert type(make_provider("gemini")).__name__ == "GeminiProvider"


def test_unknown_provider_names_the_valid_options():
    with pytest.raises(ValueError, match="MATCH_LLM_PROVIDER must be one of"):
        make_provider("nonexistent-llm")


def test_availability_reflects_the_environment(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert provider_info("gemini").available is False
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-key")
    assert provider_info("gemini").available is True
    assert provider_info("ollama").available is True, "local provider needs no key"


def test_a_new_provider_plugs_in_without_touching_discovery_code():
    """What a future Anthropic or OpenAI provider has to satisfy."""

    class StubProvider:
        def complete(self, messages: list[dict[str, str]]) -> Mapping[str, Any]:
            return {"score": 70, "summary": "stub", "matched_skills": [],
                    "missing_skills": [], "critical_gaps": [], "language": "English",
                    "track": "backend_java", "eligibility": "eligible"}

    original = dict(providers._REGISTRY)  # noqa: SLF001 - restored below
    try:
        register_provider(ProviderInfo(
            key="stub", label="Stub", factory=lambda **kwargs: StubProvider(),
            remote=False, cost_note="free"))
        with pytest.raises(ValueError, match="already registered"):
            register_provider(ProviderInfo(key="stub", label="x",
                                           factory=lambda **kw: StubProvider(),
                                           remote=False))
        built = make_provider("stub")
        # The matcher only ever needs this one method.
        assert hasattr(built, "complete")

        from job_agent.ai_matching import MatchResult
        result = MatchResult.from_dict(built.complete([]))
        assert result.score == 70

        # The discovery layer must not know this provider exists.
        import job_agent.daily_pipeline as pipeline
        import job_agent.sources as sources
        for module in (pipeline, sources):
            text = open(module.__file__, encoding="utf-8").read()
            assert "stub" not in text
            assert "GeminiProvider" not in text and "OllamaProvider" not in text
    finally:
        providers._REGISTRY.clear()  # noqa: SLF001
        providers._REGISTRY.update(original)  # noqa: SLF001


def test_collectors_never_import_a_provider():
    """Discovery code must stay free of any model dependency."""
    import pkgutil

    import job_agent.collectors as package

    for module in pkgutil.iter_modules(package.__path__):
        path = f"{package.__path__[0]}/{module.name}.py"
        text = open(path, encoding="utf-8").read()
        for forbidden in ("GeminiProvider", "OllamaProvider", "GEMINI_API_KEY",
                          "make_provider", "ai_matching"):
            assert forbidden not in text, f"{module.name} references {forbidden}"
