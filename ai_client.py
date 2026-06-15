"""
ai_client.py — thin Claude wrapper with a hard guarantee: it NEVER raises into
the pipeline. If the key is missing, the SDK is absent, or the API errors, every
method returns None and the caller falls back to deterministic templates.

Usage:
    from ai_client import ai
    if ai.available:
        text = ai.complete("...", system="...")   # -> str | None
        data = ai.json("...", system="...")        # -> dict/list | None
"""
from __future__ import annotations

from typing import Any

from settings import settings
from utils import first_json_block, get_logger, retry

log = get_logger("ai_client")


class AIClient:
    def __init__(self) -> None:
        self._client = None
        self._init_attempted = False

    # -- lazy init so import never fails even if anthropic isn't installed --
    def _ensure(self) -> None:
        if self._init_attempted:
            return
        self._init_attempted = True
        if not settings.has_claude:
            log.info("ai_client: no ANTHROPIC_API_KEY — running in template/offline mode")
            return
        try:
            import anthropic

            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            log.info("ai_client: Claude ready (model=%s)", settings.claude_model)
        except Exception as exc:  # pragma: no cover
            log.warning("ai_client: failed to init Anthropic SDK (%s) — template mode", exc)
            self._client = None

    @property
    def available(self) -> bool:
        self._ensure()
        return self._client is not None

    def complete(
        self,
        prompt: str,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.9,
    ) -> str | None:
        """Return assistant text, or None on any failure (caller falls back)."""
        self._ensure()
        if self._client is None:
            return None

        def _call() -> str:
            msg = self._client.messages.create(  # type: ignore[union-attr]
                model=settings.claude_model,
                max_tokens=max_tokens or settings.claude_max_tokens,
                temperature=temperature,
                system=system or "You are a viral short-form sports content strategist.",
                messages=[{"role": "user", "content": prompt}],
            )
            parts = [b.text for b in msg.content if getattr(b, "type", None) == "text"]
            return "\n".join(parts).strip()

        try:
            return retry(_call, attempts=3, backoff=(1, 4, 10))
        except Exception as exc:  # noqa: BLE001
            log.warning("ai_client: completion failed after retries (%s) — falling back", exc)
            return None

    def json(
        self,
        prompt: str,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.8,
    ) -> Any | None:
        """Ask for JSON and parse it leniently. None on failure."""
        sys = (system or "") + (
            "\n\nRespond with ONLY valid JSON. No prose, no markdown fences."
        )
        text = self.complete(prompt, system=sys.strip(), max_tokens=max_tokens, temperature=temperature)
        if not text:
            return None
        return first_json_block(text)


ai = AIClient()
