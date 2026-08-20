"""Клиент к Claude (Anthropic Messages API).

Отдельный протокол от OpenAI-совместимого /chat/completions в llm.py:
свой заголовок авторизации, свой формат тела и ответа. Интерфейс (`.complete`,
`.model`) специально повторяет LLMClient, чтобы Publisher мог использовать
любой из двух клиентов не меняя остальной код — см. Publisher._active_llm.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp

from .llm import LLMEmpty, LLMError

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class ClaudeClient:
    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-5",
        *,
        timeout: int = 90,
        max_tokens: int = 1024,
        temperature: float = 0.3,
        retries: int = 2,
    ):
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.retries = retries
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }

    async def complete(self, prompt: str, system: str | None = None) -> str:
        if not self.api_key:
            raise LLMError("CLAUDE_API_KEY не задан")

        payload: dict[str, object] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system

        last_error = "неизвестная ошибка"
        for attempt in range(1, self.retries + 2):
            try:
                session = await self._get_session()
                async with session.post(
                    API_URL, json=payload, headers=self._headers
                ) as resp:
                    body = await resp.text()
                    if resp.status == 200:
                        try:
                            return self._extract(body)
                        except LLMEmpty as exc:
                            last_error = str(exc)
                    else:
                        last_error = f"HTTP {resp.status}: {body[:300]}"
                        # 429 и 5xx стоит повторить, остальные 4xx — нет.
                        if resp.status < 500 and resp.status != 429:
                            break
            except asyncio.TimeoutError:
                last_error = "таймаут запроса к Claude"
            except aiohttp.ClientError as exc:
                last_error = f"{type(exc).__name__}: {exc}"

            if attempt <= self.retries:
                delay = 3 * attempt
                log.warning("Claude попытка %s не удалась (%s), повтор через %ss",
                            attempt, last_error, delay)
                await asyncio.sleep(delay)

        raise LLMError(last_error)

    @staticmethod
    def _extract(body: str) -> str:
        import json

        try:
            data = json.loads(body)
        except ValueError as exc:
            raise LLMError(f"не JSON в ответе: {exc}") from exc

        if data.get("type") == "error":
            err = data.get("error") or {}
            raise LLMError(str(err.get("message") or body[:300]))

        parts = data.get("content") or []
        text = "".join(
            p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"
        ).strip()
        if not text:
            stop = str(data.get("stop_reason") or "")
            raise LLMEmpty(f"Claude вернул пустой ответ, stop_reason={stop}", stop)
        return text
