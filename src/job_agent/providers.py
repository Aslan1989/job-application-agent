"""Registry for LLM matching providers.

The point of this module is separation: job discovery never imports a provider
and never learns a provider's name. Discovery produces a `NormalizedJob`, the
LangGraph matcher asks a `CompletionProvider` for a structured assessment, and
the two sides only meet through that protocol. Adding Anthropic or OpenAI later
means registering a factory here — no collector, no filter and no pipeline code
changes.

`CompletionProvider` is the whole contract:

    class MyProvider:
        def complete(self, messages: list[dict[str, str]]) -> Mapping[str, Any]:
            '''Return the parsed JSON assessment. Raise on failure.'''

Requirements for any new provider:
- Request JSON output and temperature 0, so the result is checked by
  `MatchResult.from_dict` rather than trusted.
- Never put the API key in a URL, and never interpolate a request or response
  body into an exception message.
- Expose no tools to the model. It returns an assessment; it does not act.

Gemini is the default, as configured. Ollama remains available for local,
zero-cost runs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol


# Statuses that are worth another attempt. A 400/401/403/404 is a
# configuration problem: retrying it only burns quota and wall-clock time.
RETRYABLE_STATUS = (408, 425, 429, 500, 502, 503, 504)
# Statuses that will fail identically for every remaining job in the run.
PERMANENT_STATUS = (400, 401, 403, 404)


class ProviderError(RuntimeError):
    """A matching call failed, classified so the caller can decide what next.

    Carries the HTTP status and nothing else from the exchange: no request
    body, no response body and never the API key. The pipeline needs to tell
    "rate limited, try again" from "the key is wrong, stop" without any of
    that detail leaking into a log.
    """

    def __init__(self, message: str, *, provider: str = "", status: int | None = None,
                 kind: str = "unknown") -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status
        self.kind = kind

    @property
    def retryable(self) -> bool:
        """Transient: a bounded retry is reasonable."""
        if self.kind == "invalid_response":
            return False
        if self.status is None:
            return self.kind in {"network", "timeout"}
        return self.status in RETRYABLE_STATUS

    @property
    def permanent(self) -> bool:
        """Will fail the same way for every remaining job, so stop the stage."""
        return self.status in PERMANENT_STATUS if self.status is not None else False

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.status is not None:
            parts.append(f"HTTP {self.status}")
        return " (".join(parts) + ")" if len(parts) > 1 else parts[0]


def classify_exception(error: BaseException, *, provider: str) -> ProviderError:
    """Wrap any provider failure as a ProviderError, preserving only status."""
    if isinstance(error, ProviderError):
        return error
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    status = status if isinstance(status, int) else None
    name = type(error).__name__
    if "Timeout" in name:
        kind = "timeout"
    elif "Connection" in name:
        kind = "network"
    elif status is not None:
        kind = "http"
    else:
        kind = "invalid_response"
    return ProviderError(f"{provider} matching call failed: {name}",
                         provider=provider, status=status, kind=kind)


class CompletionProvider(Protocol):
    """A provider turns prompt messages into a parsed JSON assessment."""

    def complete(self, messages: list[dict[str, str]]) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ProviderInfo:
    """What a provider is and what calling it costs."""

    key: str
    label: str
    factory: Callable[..., CompletionProvider]
    remote: bool
    env_keys: tuple[str, ...] = ()
    cost_note: str = ""

    @property
    def available(self) -> bool:
        """True when the environment holds whatever this provider needs."""
        return all(os.getenv(name, "").strip() for name in self.env_keys)


def _gemini(**kwargs) -> CompletionProvider:
    from .ai_matching import GeminiProvider
    return GeminiProvider(**kwargs)


def _ollama(**kwargs) -> CompletionProvider:
    from .ai_matching import OllamaProvider
    return OllamaProvider(**kwargs)


# Gemini first: it is the configured default matching provider.
_REGISTRY: dict[str, ProviderInfo] = {
    "gemini": ProviderInfo(
        key="gemini", label="Google Gemini (remote API)", factory=_gemini,
        remote=True, env_keys=("GEMINI_API_KEY",),
        cost_note="Billed per token by Google. gemini-2.5-flash is the cheap "
                  "default; a daily run is capped at --max-matches calls.",
    ),
    "ollama": ProviderInfo(
        key="ollama", label="Ollama (local, no API cost)", factory=_ollama,
        remote=False, env_keys=(),
        cost_note="Free. Runs on this machine; needs `ollama serve` and a "
                  "pulled model. Smaller models follow the strict JSON schema "
                  "less reliably than Gemini.",
    ),
}

DEFAULT_PROVIDER = "gemini"


def register_provider(info: ProviderInfo, *, replace: bool = False) -> None:
    """Add a provider. Discovery code is unaffected by whatever is registered."""
    if info.key in _REGISTRY and not replace:
        raise ValueError(f"provider {info.key!r} is already registered")
    _REGISTRY[info.key] = info


def available_providers() -> tuple[ProviderInfo, ...]:
    return tuple(_REGISTRY.values())


def provider_info(name: str) -> ProviderInfo:
    key = str(name or "").strip().lower()
    if key not in _REGISTRY:
        raise ValueError(f"MATCH_LLM_PROVIDER must be one of: "
                         f"{', '.join(sorted(_REGISTRY))}")
    return _REGISTRY[key]


def make_provider(name: str | None = None, **kwargs: Any) -> CompletionProvider:
    """Build the selected provider. Explicit name wins over the environment."""
    chosen = name or os.getenv("MATCH_LLM_PROVIDER", DEFAULT_PROVIDER)
    return provider_info(chosen).factory(**kwargs)
